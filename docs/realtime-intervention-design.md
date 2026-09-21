# Agent 执行中「实时干预」改造方案（参考 dsh）

## 背景与问题

当前项目（desktop-agent）中，用户在 agent 执行期间发送消息会被**入队，等到当前整个请求跑完才生效**，无法实时干预：

- **Web 端**：`rt.status === 'streaming'` 时，`send()` 把消息推进 `rt.interventionQueue`，显示「已加入队列，当前回复结束后自动发送」，本轮 `finally` 才 drain 发起新一轮。
- **微信端**：`/push <内容>` 入队 `_push_queues[from_user]`，任务结束后统一 flush。

用户诉求：执行过程中能**实时把补充说明喂给正在运行的 agent**，让它立即调整方向。

## dsh 是怎么做的：核心是 Inbox 双桶模型

dsh（`packages/api/session-controller` + `dsh-agent`）把「用户消息」放进一个 **Inbox**，分成**两个桶**：

| 桶 | 语义 | 触发时机 |
|----|------|---------|
| `next-turn` | 排到**下一轮**处理 | agent 空闲 / 当前 turn 结束后 |
| `next-step` | **注入当前正在跑的 turn**（steering）| turn 进行中，实时打断调整 |

关键 API：
- `inbox.append('next-turn', msg)` / `inbox.append('next-step', msg)` —— 入桶
- `inbox.claim('next-step')` —— **agent 循环在每一步/每个 step 前取走待办**，把批次作为 `user/message` 追加进当前对话，模型立即收到并调整
- 每个 session 有持久化的 Inbox 投影，前端实时监听（`control()` SSE projection），Uni-interrupted classication 把「next-step 来的 user 消息」标记为 steering，渲染上区别于普通排队消息

**核心机制 —— claim 时机**：dsh 的 agent 循环在每个 step 开始/结束时 claim `next-step` 桶，把它插入模型上下文，因此**用户消息能在下一个 step 生效，而不是等整个 turn 结束**。`next-turn` 则只在 turn 结束后消费。

## 当前项目架构（与 dsh 的映射）

```
Web前端 streaming.js ──POST /run/stream──▶ API agent.py
                                              │ 创建 _StreamHub + _drive_agent_stream 后台驱动
                                              └─▶ agent.stream_run() ──▶ LangGraph create_react_agent
                                                     │  _stream_events_with_heartbeat 心跳循环
                                                     └─▶ 逐 step: LLM ←→ tool 循环
```

- **后台 driver 与 HTTP 解耦**（刷新/断线任务不中断），`stream_log` 落盘事件允许恢复。
- 用的是 **`create_react_agent`（预置图）**，agent 循环内部是 LangGraph 的 `__start__ → agent → tools → agent …` 的固定循环，**当前没有插入用户消息的中间节点**。
- 前端 `rt.interventionQueue` 只能等 `send()` finally drain，无法实时注入。

## 改造难点

**这是计划的关键前提，务必先评估**：

1. **`create_react_agent` 预置图在 RUN 中途无法插入用户消息。** 它内部循环 `agent(model)→tools→agent…`，没有暴露"每个 step 前检查一次入站消息"的钩子。要做到 real-time steering，必须**放弃预置图**，改用 `StateGraph + 自定义节点`（自定义 `agent_node` 与 `tools_node`、外加一个**检查 inbox 的节点**），或给 `create_react_agent` 注入一个**每次进入 agent 节点前调用**的中间工具/回调。
2. **后台 driver 是独立任务**：前端要"实时"，需要一条**从收到消息 → 写入 inbox → 通知运行中的图**的信号通路，且要与落盘/恢复兼容。
3. **"step 级" vs "turn 级"**：dsh 默认 `next-step` 步级、`next-turn` 轮级。Web 现有交互设计是"整个请求后入队"，改造要区分两种：**打断（现在插入下个step）/ 排队（本轮结束后）**。

## 改造方案（照 dsh 双桶模型，分阶段）

### P0：后端建立 Inbox + 运行中上下文注入（核心、风险最高）

**目标**：agent 在 RUN 期间可收到并响应新输入。

**后端新增** `agent_core/inbox.py`：
- `SessionInbox`: `next_turn: deque`, `next_step: list`，按 `(uid, session_id)` 索引
- `append(target, msg)` / `claim_next_step()` / `claim_next_turn()`，线程安全
- 与 `stream_log` 一致的落盘，刷新/恢复不丢

**改图结构**（`agent_init.py`）：**最终采用 `create_react_agent` 原生 `pre_model_hook`，无需重写 StateGraph**（推翻最初的 "放弃预置图" 评估——实测预置图已支持该钩子）：
- `create_react_agent(..., pre_model_hook=_make_inbox_pre_hook(user_id))`，该钩子是一个普通节点，**每次进入 LLM 的 `agent` 节点前运行**，正好是"LLM 调用边界"。
- 钩子从 `config["configurable"]["thread_id"]` 解析 `(uid, session_id)` → `claim_next_step()` → 如有待办则 `{"messages": 原消息 + 注入的 HumanMessage}` 返回，追加进 graph state（持久化到历史）
- **关键实现细节**：钩子参数名用 `config` 但**不加 Pydantic 类型注解**（LangGraph 会改写带注解的 `config` 参数为严格 RunnableConfig 导致异常），用普通函数即可
- 保持现有 `RetryableLLM` / 压缩 / checkpointer 逻辑不动（零回归）
- 钩子把注入消息同时 `record_injected`，`stream_run` 在每个 `on_chat_model_start` 时 `drain_injected` 产出 `user_message_injected` SSE 事件给前端

**关键决策**：不打断正在执行的**工具**（等工具返回），只在「LLM 调用边界」注入 —— 这与 dsh 的 step 级 steering 等效且最稳。

**改 `agent_run.py` stream_run**：
- 在 step 事件循环里，识别与注入消息对应的新 HumanMessage step，正常用现有 SSE 透传
- 落盘新增 `user_message_injected` 事件类型，恢复时正确回放

### P1：Web 前端实时干预

- `streaming.js`：streaming 态下 `send()` 不再一律入 `interventionQueue`，改为：**POST `POST /run/{sid}/inject`（语义=打断/next-step）**，或保留 Enter=排队，新增 **Ctrl+Enter / 停止旁按钮 = 打断**。
- 新增 `POST /agent/sessions/{sid}/inject` 路由：body 里 `content` + `mode: step|turn`，写入 inbox，若是 step 则**通知正在运行的 driver 立即取**。
- i18n 新增「打断已发送（下个步骤生效）」。

### P2：微信端实时干预

- `/push` 增加可选语义：默认仍 `next-turn`，`/push --now` 走 `next-step`（注入当前任务下一步）；或单独 `/steer <内容>` 命令。
- `wechat_bot.py` 的 `_flush_push_queue` 与 inbox 打通。

## 需要确认的 4 个决策

1. **注入时机**：只在「LLM 调用边界」注入（推荐，不打断工具、最稳），还是也要打断正在执行的长工具？—— 推荐前者。
2. **交互**：Enter = 排队（现状保留），**再加一个显式「打断/steer」入口**（按钮或 Ctrl+Enter），还是一个输入动作同时支持两种？推荐"保留 Enter=排队 + 新增打断按钮"。
3. **是否放弃 `create_react_agent`**：P0 必须改 `StateGraph` 才能 step 级注入。可接受吗？（预置图换自定义图，风险集中在回归测试，现有 344 测试需全过）
4. **范围**：只做 P0（后端），还是 P0+P1（+Web），还是三期全做？

## 建议分期

- **P0**（后端 Inbox + StateGraph 改造 + 单测）—— 先打通，风险隔离
- **P1**（Web 打断入口 + inject 路由）
- **P2**（微信 `/steer`）

每期独立可验证、可回滚。P0 不碰前端，现有 Web 排队交互不受影响。

## 风险与对策

- **预置图→自定义图回归风险**：**已消除**——最终用 `pre_model_hook` 而非重写图，默认空桶时钩子返回 None、图行为零变化（全部 344+10 测试通过）。
- **后台 driver 与注入信号通知**：注入通过「写 inbox + 事件广播给 _StreamHub」两条路，driver 在每个 LLM 边界的 `check_inbox_node` 天然感知，无需额外打断机制。
- **消息丢失/乱序**：inbox 落盘 + claim 幂等设计（injected 消息入 step 序列，结束不丢）。