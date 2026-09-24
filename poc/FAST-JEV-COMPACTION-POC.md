# fast-jev-compaction 集成 POC：判别式上下文压缩

> 结论先行：**该方案与当前项目的架构高度契合，集成成本低、收益明确**。
> 本项目已有完整的 Jev 客户端（`agent_core/tools/jev_tools.py`，354 行，零依赖），
> 集成不需要引入任何新依赖，也不需要 npm/TypeScript 运行时。
> POC 已落地：`agent_core/jev_compaction.py` + `tests/test_jev_compaction.py`。

---

## 1. 原方案在做什么（读完源码后的准确描述）

仓库：https://github.com/tamaratran/fast-jev-compaction （TypeScript，MIT，6670 stars）

它是一个 Claude Code 插件，**替换掉 Claude Code 内置的 `/compact` 生成式摘要**。
核心流程（README + `src/` 语义）：

1. 把每个 `tool_use` 与它的 `tool_result` 按 `tool_use_id` 配对；首条消息和最近
   `preserveRecentMessages` 条消息被 pin 住，永不改动。
2. **state** = 到目前为止的整段对话（旧的在前），但每个 tool result 被替换成一句短注
   （`ok, 4213 chars (omitted)`）。tool 输入保留，文本保留，**不做任何摘要**。
3. state 按 `maxStateTokens`（默认 25k）分级裁剪：tool 输入截到 1000/200/60 字符 →
   长文本只留头尾 → 旧的非 pin 消息折叠成 `[… N chars omitted …]` → 旧 tool call 压成一行
   （`t12 Read file_path=src/a.ts → ok 480ch`）。token 用无 tokenizer 的启发式估算。
4. 对每个非 pin 的 call，问 Jev **两个 noul 问题**：
   - 这个 **call** 该不该留（"知道它发生过、带着输入，仍然重要吗"）
   - 这个 **result** 该不该 verbatim 留（"内容仍需要，且重跑工具也拿不到吗"）
5. 问题按 `maxRequestTokens`（30k，低于 Jev 32k 请求上限）分批，**每批都重发完整 state**，
   多批并发请求后合并答案。
6. 按 `keepThreshold`（默认 0.5）决策：
   - `keepResult ≥ 阈值` → call 和 result 都留
   - 否则 `keepCall ≥ 阈值` → 留 call，result 截到前 `truncateHeadChars` 字符 + 一行注
   - 否则 → **连 call 带 result 一起删**
7. 重建消息列表：内容全丢的消息整条移除，未触碰的消息**原对象返回**，
   任何 result 都不会失去它的 call。

**关键性质**：用户/助手的文本消息**永不删除、永不缩短**（只在给 Jev 看的 state 里缩写）。
只有 tool call / tool result 是删除候选。Jev 失败/答案畸形/key 缺失 → 抛错，由调用方决定回退。

---

## 2. 当前项目的压缩逻辑（集成点分析）

`agent_core/context_manager.py`（547 行）已实现一套**分层生成式摘要**：

| 层 | 行为 | 代码位置 |
|---|---|---|
| P0 | system 消息永不压缩 | `compact_messages_report` |
| P1 | 最近轮 verbatim，按 token 预算（阈值×50%）从新往旧累加，**以工具组为原子单位** | 同函数内 `recent_groups` 循环 |
| P2 | 旧段再分 medium（每条 `_clip_middle(content, 100)` 精简到约 100 字）/ old（仅留用户关键指令） | `_split_medium_old` / `_summarize_medium` / `_summarize_old` |
| P3 | 摘要按「角色 + 轮次号」结构化 | `_summarize_medium` |
| P4 | tiktoken 精确计数，失败回退启发式 | `_count_tokens` |
| 防抖动 | 压缩后仍接近阈值则把最老最近轮整组降级为 old 段 | `while True` 循环 |
| 底线保护 | 摘要为空且最近轮无 human → 从 old 段回捞含 human 的组 | 同函数内 `if (not summary_text ...)` |

**关键观察（决定集成方式）**：

1. **本项目已经做了原方案最重要的两件事**：
   - 工具链原子性（`_group_messages` 保证 AI(tool_calls) 与 ToolMessage 同组，绝不切裂）
   - 用户指令不丢失（old 段用户指令归档进长期记忆 `_archive_user_instruction`，可 `recall_memory` 召回全文）
2. **真正的差异只在 P2 的 medium 段**：当前是**无差别机械裁剪**（每条截到 100 字），
   不区分"这条工具结果后面还要用"和"这条早就没用了"。
3. 因此集成点非常明确：**在 P2 分层之前，插入一道 Jev 判别式删除**，
   把"确定没用了"的旧工具组整组删掉，剩下的才走既有 medium/old 分层。

---

## 3. 方案对比：为什么值得做

| 维度 | 当前（生成式/机械裁剪） | fast-jev-compaction（判别式删除） |
|---|---|---|
| 信息保真 | 每条截 100 字，**可能截掉关键路径/错误/约束** | 保留的**逐字 verbatim**，不重写任何内容 |
| 可审计性 | 摘要是一坨文本，无法追溯"什么被删了为什么" | 每个删除都有 Jev 概率 + 理由，可解释 |
| 成本 | 0 额外请求 | 每批 1 次 Jev 请求（noul 极轻，本项目已有客户端） |
| 删除粒度 | 按消息截断 | 按「工具组」整组删 / 截断 / 保留 三档 |
| 风险 | 关键信息静默丢失 | 概率非证明，但**助手可重跑工具**兜底 |

**收益判断**：对本项目（长会话、批量任务、大量工具调用）价值最大的场景是——
早期读过的文件、跑过的命令、查过的数据库结果，在几十轮后往往已无用，却仍占着
verbatim 或 100 字摘要的额度。判别式删除能把它们**整组清掉且不影响后续推理**。

---

## 4. 集成设计（POC 已按此实现）

### 4.1 为什么不直接装 npm 包

- 本项目是 Python 后端（FastAPI + LangGraph），没有 Node 运行时参与 agent 主循环。
- 原方案的 `src/` 是 TypeScript，`hooks/` 依赖 Claude Code 的 function-hook 机制（2.1.274+ early access）。
- **但算法可以完整移植**，且本项目已有 `jev_tools.py` 提供 `noul` 原语 + `JevClient` + 失败静默降级。
- 移植后零新依赖（仅标准库 + 已有 langchain 消息类型）。

### 4.2 落点：`agent_core/jev_compaction.py`

新增一个**纯函数模块**，不改 `context_manager.py` 的任何既有行为：

```
compact_messages_report()  [既有入口，行为不变]
        │
        ├─ before_tok < threshold → 直接返回（不变）
        │
        └─ 触发压缩：
              groups = _group_messages(dialogue)      [复用]
              round_of = _assign_rounds(dialogue)     [复用]
              ┌──────────────────────────────────────┐
              │ 【POC 新增】Jev 判别式删除            │
              │  jev_prune_tool_groups(older_groups)  │
              │   → (kept, pruned, decisions)         │
              │   Jev 不可用 → 原样返回（零影响）      │
              └──────────────────────────────────────┘
              P1 最近轮 verbatim                      [既有]
              P2 medium / old 分层                    [既有，输入已变小]
```

### 4.3 核心算法（对齐原方案，按本项目实际简化）

对每个**非 pin 的旧工具组**，问 Jev 两个 noul 问题（与原方案一致）：

- `keep_call`："知道这个工具调用发生过（含输入），对后续任务仍然重要吗？"
- `keep_result`："这个工具结果的内容仍然需要，且重跑工具也拿不到吗？"

决策（阈值 `keep_threshold`，默认 0.5）：

| 条件 | 动作 |
|---|---|
| `keep_result ≥ t` | 整组 verbatim 保留 |
| 否则 `keep_call ≥ t` | 保留 call，**result 截到前 N 字符** + 一行注 |
| 否则 | **整组删除**（call + result 一起，工具链完整） |

**pin 规则**（与原方案一致，按本项目语义调整）：
- 第一个 human 消息（用户最初指令）永不删
- 最近 `preserve_recent` 个组永不删（默认 6，与原方案 `preserveRecentMessages` 一致）
- system 消息不参与（P0 已保证）

**state 构建**（原方案第 2 步）：整段对话旧的在前，tool result 替换为
`ok, N chars (omitted)`，tool 输入保留，文本保留，**不摘要**。
按 `max_state_tokens`（默认 25k）分级裁剪，与原方案相同的降级阶梯。

**分批**：问题按 `max_request_tokens`（30k）分批，每批重发完整 state。
POC 阶段**串行批处理**（并发是优化，不是正确性前提，留 `ponytail:` 注释标注）。

### 4.4 降级路径（本项目铁律：Jev 绝不阻断主流程）

`jev_tools.py` 的设计原则是"失败静默降级返回 None"。因此：

- Jev 未配置 key / 超时 / 网络失败 / 答案畸形 → **`jev_prune_tool_groups` 原样返回输入**，
  `compact_messages_report` 走既有 P2 逻辑，**行为与今天完全一致**。
- 这意味着集成是**纯增量**：Jev 不可用时零风险；可用时才获得判别式删除收益。

---

## 5. POC 验证方式

### 5.1 单元测试（`tests/test_jev_compaction.py`，28 用例，mock Jev）

全部通过，覆盖：三档决策、pin 规则、工具链完整性、降级路径、state 构建、
收益门槛、文本消息永不删除、分批与工具函数。

### 5.2 保真度对比脚本（`poc/verify_jev_compaction.py`）

构造"早期工具结果含关键路径/错误信息 + 大量噪音工具结果"的长会话，实测结果：

```
会话规模：35 条消息，3655 tokens（14 个噪音工具组 + 2 个关键工具组）
关键信息探针：3 条（文件路径 / 错误信息 / 用户约束）

【当前实现（机械裁剪 100 字）】
  token: 3655 → 849（降幅 77%）
  ❌ 丢失关键信息 2 条: 文件路径、错误信息

【Jev 判别式删除 + 既有分层】
  token: 3655 → 1290（降幅 65%）
  Jev: 可用=True 删 11 组 / 截 0 组 / 留 7 组（字符降幅 72%）
  ✅ 关键信息全部保留（3/3）
```

**结论：判别式删除用高 12 个百分点的 token 占用，换取了零信息丢失。**
当前实现降幅更狠，但把精确定位 bug 的文件路径和确切错误信息裁掉了——
后续 agent 无法继续修复。这正是原方案要解决的核心问题。

> 集成时的一个关键发现（POC 过程中实测得出）：Jev 删除后**必须用原始阈值
> 重新判断是否还需要既有压缩**。若删除后已低于阈值，就应直接采用删除结果，
> 不再走 medium 段 100 字裁剪——否则 Jev 辛苦保留的关键组又会被下游裁掉，
> 判别式删除的收益被既有链路完全吃掉（POC 第一版就踩了这个坑）。

### 5.3 真实 API 验证（需 `TYPESAFE_API_KEY`）

```sh
TYPESAFE_API_KEY=xxx python poc/verify_jev_compaction.py --live
```

`poc/verify_jev_live.py` 已有 Jev 真实调用验证的先例，POC 沿用同样模式。

---

## 6. 落地路径（POC 之后）

| 阶段 | 内容 | 风险 |
|---|---|---|
| **POC（完成）** | 独立模块 + 单测 + 对比脚本，**不接入主流程** | 零 |
| **阶段 2（完成）** | `compact_messages_report` 内接入，**默认关闭**，设置页开关 `jev_compaction_enabled`，经 `AgentConfig` 持久化 | 低（开关兜底） |
| 阶段 3 | 小流量真实会话验证保真度与降幅，调 `keep_threshold` / `preserve_recent` | 中 |
| 阶段 4 | 默认开启，`CompactionReport` 增加 `jev_decisions` 字段供 UI 展示 | 低 |

**不建议**：直接替换 P2 既有逻辑。判别式删除应作为 P2 的**前置过滤器**，
既有的 medium/old 分层 + 记忆归档 + 防抖动 + 底线保护全部保留——
那些是本项目特有的正确性保障，原方案没有对应物。

---

## 7. 已知限制（与原方案一致，POC 不解决）

- token 数是启发式估算，非 tokenizer（本项目 P4 已有 tiktoken，可后续替换）
- 概率不是"删除安全"的证明；兜底是"助手可重跑工具"
- 完整 state 每批重发，接近 state 上限时批数增多
- 只删 tool call/result，**文本消息永不删**（这是特性不是限制）
