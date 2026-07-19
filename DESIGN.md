# Desktop-Agent 自进化机制设计（DESIGN.md）

> 状态：Phase 1 设计定稿，待实现。系统守护层作为贯穿性基础设施（第 0 层）先于各 Phase 做实。
> 最后更新：2026-07-19

---

## 0. 背景与目标

让 Agent 在运行中**持续从经验中学习**：记住可复用做法、避开踩过的坑、吸收用户偏好，并在更后期（P3）具备"改写自身技能"的能力。

核心约束（ponytail 视角）：
- **复用现有钩子，不造新抽象**：记忆已有 `LocalMemory` + `_learned_` 注入链、反思已有 `reflect_on_task` / `_async_reflect`、热加载已有 `reload_skills`。
- **可用性优先于正确性**：任何进化机制都不能让 Agent 起不来。"先让它活过来"是铁律。
- **最小改动 + 向后兼容**：结构化记忆必须兼容现有纯字符串 `_learned_`，旧数据不丢。
- **所有进化逻辑门控于 feature flag**：默认关，开启才生效。

---

## 1. 现状：已有的"初级自进化闭环"

代码里已经存在一个单向、只记成功、无反馈、无验证的雏形：

| 能力 | 现状 | 落点 |
|---|---|---|
| 经验捕获 | ✅ 任务后 LLM 总结"可复用模式"，产出 `关键词\|一句话` | `agent.py:917 reflect_on_task` |
| 经验存储 | ✅ 后台以 `_learned_<hash>` 为 key 写进 `LocalMemory`（按用户隔离） | `services/agent_service.py:216 _async_reflect` |
| 经验注入 | ✅ 每次构建系统提示时把 `_learned_` 注入 | `agent.py:845 _load_learned_patterns` |
| 技能热加载 | ✅ 可重载技能，但无"运行时生成技能"路径 | `agent.py:1865 reload_skills` |
| feature-flag 范式 | ✅ 现成的 `enable_loop_guard` 全套 | `config.py:113` + `env_map`(`:216`) + `save()`(`:365`) + `to_api_dict()`(`:376`) + `api/routes/system.py`(`/settings` `:109`/`POST :123`/`env :150`) |

**缺的五块**：反馈捕获、结果判定语义、失败反思、行为改写、安全验证/度量。

---

## 2. 总体路线图

目标力度（已确认）：**全自主自改写**，但地基按"能平滑跑到全自主"的方式铺设。

| 层/Phase | 内容 | 状态 |
|---|---|---|
| **第 0 层 系统守护** | 启动自愈 + **运行时巡检自愈** + Apply 闸门，防止 Agent 起不来 / 跑着跑着坏掉 | **P1 即做实（自愈常开）**，闸门 P3 接 |
| **Phase 1 加固闭环** | 反馈捕获 + 失败/反馈反思 + 负向注入 + 结构化记忆 | **待实现（本设计范围）** |
| Phase 2 记忆结构化与去重 | 三类经验(technique/preference/pitfall)排序截断、总量上限 | 后续 |
| Phase 3 行为改写 | 高价值 `technique` → 起草 `SKILL.md` → `reload_skills` 热加载（人工在环/全自主） | 后续 |
| Phase 4 安全应用 | 沙箱 dry-run + 审批闸 + 版本回滚 | 后续 |
| Phase 5 元度量 | 某条经验被注入后对应任务成败关联；反模式降权/过期 | 后续 |

---

## 3. Phase 1 详细设计（后端优先）

### 3.1 数据流

```
任务结束(done/error)
  └─ event_stream → asyncio.create_task(_async_reflect(...))   [现有]
        └─ reflect_on_task(outcome, feedback=None) → 产出 {t, v}
              └─ 存 LocalMemory: _learned_<hash> 或 _avoid_<hash>
用户事后点 👎/填纠错
  └─ POST /sessions/{id}/feedback
        └─ 存 LocalMemory: _feedback_<session>_<ts>
        └─ asyncio.create_task(_reflect_from_feedback(...))
              └─ reflect_on_task(outcome="feedback", feedback=correction) → {t:pitfall|preference, v}
                    └─ 存 _avoid_ / _learned_
下次构建 system prompt
  └─ _build_system_prompt → _load_learned_patterns()
        └─ 注入正向经验 + "用户纠正/踩坑：不要 X"
```

### 3.2 数据模型（复用 `LocalMemory`，按用户隔离，不新开表）

`_learned_` 现有值为纯字符串。升级为结构化值，**向后兼容纯字符串**：

```
# 正向/可复用（technique）与偏好（preference）用 _learned_ 前缀
"_learned_<md5[:12]>" : {"t": "technique"|"preference", "v": "一句话", "w": 1}
# 负向/踩坑用 _avoid_ 前缀
"_avoid_<md5[:12]>"   : {"t": "pitfall", "v": "不要 X", "w": 1}
# 反馈原始记录（不进 prompt，仅审计/触发反思）
"_feedback_<session>_<ts>" : {"rating": -1|0|1, "correction": "..."}
```

- `_load_learned_patterns` 读取：`_learned_` 取 `v` 进"经验"段；`_avoid_` 取 `v` 进"反模式"段；纯字符串旧值按 `technique` 处理；`_feedback_` 忽略。
- 命中率 `w`（P5 用，P1 先恒 1，预留字段）。TTL 现有 10 天保留。

### 3.3 各模块改动

#### 3.3.1 配置开关 `config.py`
- `AgentConfig` 加字段（紧挨 `enable_loop_guard`，`:113`）：
  `enable_self_evolution: bool = False`
- `env_map`（`:216` 附近）加：
  `"AGENT_SELF_EVOLUTION": ("enable_self_evolution", _env_bool)`
- `save()`（`:365` 附近）、`to_api_dict()`（`:376`/`:413` 附近）各加一行该字段。
- `api/routes/system.py` 仿 `enable_loop_guard` 全套（`/settings` GET/POST、`:123` 赋值、`:150` 写 env）加 `enable_self_evolution` 读写。
- 所有新进化逻辑用 `if not config.enable_self_evolution: return` 门控；**默认关**。

#### 3.3.2 反思升级 `agent.py:917 reflect_on_task`
签名改为：
```python
async def reflect_on_task(self, user_message, steps, final_result,
                          outcome: str = "success",
                          feedback: Optional[str] = None) -> Optional[dict]:
    # 返回 {"t": "technique"|"preference"|"pitfall", "v": "..."} 或 None
```
- 无工具调用 → 仍返回 `None`。
- 选模型：若 `config.review_model`（`:98`）配置则用 review LLM，否则主 LLM；`request_timeout = 15`。
- 三套提示词分支：
  - **成功、无反馈**（现有）：总结可复用模式 → `{t:"technique", v:"关键词|一句话"}`，无价值回 `None`。
  - **失败/error**（outcome="error"）：根因分析 → `{t:"pitfall", v:"不要 X / 应改 Y"}`。
  - **带用户反馈**（feedback 非空）：归纳用户偏好/纠正 → `{t:"preference"|"pitfall", v:"..."}`。

#### 3.3.3 注入 `agent.py:845 _load_learned_patterns`
现有只处理 `_learned_` 且要求 `isinstance(val, str)`。改为：
- 遍历 `list_items()`（local_memory.py:58），按 key 前缀分流。
- 兼容旧纯字符串（当 `technique`）；dict 值取 `v` 字段。
- 正向 → `经验` 列表；负向(`_avoid_`) → `反模式/用户纠正` 列表，注入 system prompt 时独立成段：
  `## 历史踩坑与用户纠正（务必避免）\n- 不要 X`
- 现有 10 天 TTL 清理逻辑保留（对两类 key 都生效）。

#### 3.3.4 反思调度 `services/agent_service.py:216 _async_reflect`
- 现有签名 `（uid, user_message, steps, result）` 改为增加 `outcome="success"`。
- 存储：`reflection` 现在是 dict；key 用 `md5(v + t)` 防碰撞；`mem.set(key, reflection)`（local_memory.py:39）。
- 新增同文件 `_reflect_from_feedback(uid, session_id, rating, correction)`：
  - 从 `session_store` 取该 session 最后一条 user 消息 + 最后一条 assistant 结果。
  - `await agent.reflect_on_task(user_msg, steps=[], result=assistant_content, outcome="feedback", feedback=correction)`。
  - 按返回 `t` 存 `_learned_`/`_avoid_`。

#### 3.3.5 反馈端点 `api/routes/agent.py`
- 现有两处 `_async_reflect` 调用（`:160`、`:373`）补 `outcome`（`:373` 处 `error_content` 非空传 `"error"`）。
- 新增：
```
POST /sessions/{session_id}/feedback
Body: {"rating": -1|0|1, "correction": str|null}
Auth: 现有登录中间件（按 uid 隔离）
行为:
  1. 校验 session 归属当前 uid（防越权，仿 delete_session_message）
  2. mem = get_memory(uid); mem.set(f"_feedback_{session_id}_{int(time.time())}", {"rating":..,"correction":..})
  3. asyncio.create_task(_reflect_from_feedback(uid, session_id, rating, correction or ""))
  4. return {"ok": true}
```
（前端 👍/👎 按钮 P1 不做，仅留 API；后续单独补。）

#### 3.3.6 惰性基建（为全自主埋桩，P1 不激活）
- 仓库 `skills/` 下建 `.generated/` 目录（可写，P3 起草 `SKILL.md` 落这里）。
- `agent.py` 新增空函数占位（带开关判断，便于 P3 填实）：
```python
def maybe_generate_skill(self, pattern: dict) -> Optional[str]:
    # ponytail: P3 占位——高价值 technique 起草 SKILL.md 并 reload_skills()
    return None

def _approval_gate(self, candidate) -> bool:
    # ponytail: P3/P4 占位——人工审批/沙箱校验，当前恒 False（不激活）
    return False
```
- 不接任何调用方，纯预留，不影响现有行为。

### 3.4 测试策略 `tests/test_self_evolution.py`
- `test_feedback_stored_and_triggers_reflection`：调 feedback 端点 → `get_memory(uid).list_items()` 含 `_feedback_`；mock `reflect_on_task` 断言被调用且带 correction。
- `test_reflect_failure_returns_pitfall`：`reflect_on_task(outcome="error", ...)` 返回 `{"t":"pitfall", ...}`。
- `test_learned_patterns_includes_avoid`：注入一条 `_avoid_` dict → `_load_learned_patterns()` 结果含"不要 X"段落；旧纯字符串 `_learned_` 仍按经验处理。
- 运行：`.venv/bin/python -m pytest tests/test_self_evolution.py`。改动文件 `py_compile` 校验。

### 3.7 实现备注（与原始设计的偏差）
- **门控范围调整**：原始设计写"所有进化逻辑门控、默认关"。实现时为**不回归既有行为**，改为：既有成功路径反思（`technique`）**始终启用**（保持已上线的 `_learned_` 学习闭环）；仅**新类型** `pitfall` / `preference`（失败反思、用户反馈反思、负向注入）受 `enable_self_evolution` 门控、默认关。负向注入因 `_avoid_` 只由受控路径产生，开关关闭时天然 inert。
- **失败反思触发**：`api/routes/agent.py` 流式结束处的 `_async_reflect` 调用补 `outcome="error" if error_content else "success"`，使流式失败能产出 pitfall（受开关门控）。
- 其余（config 开关全链路、结构化记忆、负向注入、反馈端点、惰性基建占位）均按 §3.1–§3.6 落地。已通过 `tests/test_self_evolution.py`（4 例）+ `tests/test_loop_guard.py`（11 例，无回归）。

---

## 4. 系统守护层（第 0 层，贯穿性基础设施）

### 4.1 为什么必须有

自改写（P3）要让 Agent 生成 `SKILL.md` 或写 config 补丁。一旦产物语法错误/逻辑坏，下次启动 `init_agent()`（`main.py:229`）抛异常 → `agent` 变 `None` → 整个服务废掉。**必须先有自愈兜底：让它先活过来，再谈进化。**

决策（已确认）：**`boot 自愈`始终开启**（纯 stdlib、零成本、只保护"能不能启动"）；**`apply 闸门`仅由后续进化代码（P3）调用**。

### 4.2 两层职责

**① Apply 闸门（预防，进化产物落地前）**
进化产物（SKILL.md / config 补丁 / prompt 覆盖）生效前，先过一层纯静态校验——**不调 LLM、零成本、快**：
- SKILL.md：frontmatter YAML 可解析 + 必填字段（`name`/`description`）齐全 + 用 `skills/loader.py` 同样方式 dry-parse 不报错。
- config 补丁：只含已知 `AgentConfig` 字段、类型正确、套用后 `load()` 仍成功。
- 先写 `.staging` 再 dry-验证，PASS 才落盘并记 manifest；FAIL 直接拒绝，**永不应用**。

**② Boot 自愈（恢复，启动失败时）**
在 `main.py:103 lifespan` 里包住 `init_agent()`（含 skills 加载）：
- 成功 → 标 `.boot_ok`、记 manifest 哈希、正常服务。
- 抛异常 → 读 `skills/.generated/manifest.json`（LIFO）→ 把**最近一次**进化产物移入 `skills/.quarantine/`（保留待查、**不删**，可人工回看）→ 重试 `init_agent()` → 循环回退直到 manifest 空 → 仍失败则移除全部 `.generated/` 再试一次 → 还失败：服务存活但 `agent=None`，记 FATAL 等人工。

### 4.3 关键设计约束（ponytail 视角）
- **守护层自身绝不能成为启动失败源**：只用纯 stdlib（`shutil`/`json`/`traceback`），不 import 任何业务模块、不调 LLM。挂得越轻越保命。
- **复用现有模式不造新轮子**：镜像 `main.py:30 _apply_pending_update_at_boot()` 的原子 swap + try/except + `.old` 回退；复用 `config.load()` 已有的 try/except 容错（坏 JSON 回退默认，不会崩）。
- **可用性优先于正确性**：任何怀疑 → 退到 last-known-good。
- **回退可逆**：产物进 `quarantine/` 而非删除，人工可审计、可恢复。

### 4.4 落点
- 新模块 `agent_core/guardian.py`（纯 stdlib）暴露：
  - `validate_artifact(kind, content) -> bool`（apply 闸门，P1 为 inert 占位，P3 填实）
  - `self_heal_on_boot(boot_fn) -> agent | None`（boot 自愈，P1 做实）
  - `record_evolution(manifest_entry)`（记录进化产物清单）
- `main.py:103 lifespan` 把 `init_agent()` 包进 `guardian.self_heal_on_boot(...)`；现有"首次请求重试"逻辑升级为"重试 + 回退"。
- 状态文件：`skills/.generated/manifest.json`、`skills/.quarantine/`、`config.json.bak`、`.boot_ok`。

### 4.5 测试 `tests/test_guardian.py`
- 放一个畸形 SKILL.md 进 `.generated/` + 写 manifest → 包 `init_agent()` 启动 → 断言**成功启动**且坏技能进了 `quarantine/`。
- apply 闸门拒绝非法 frontmatter 的 SKILL.md（返回 FAIL，未落盘）。
- config 补丁含未知字段 → 拒绝。

### 4.6 运行时巡检与自愈（Runtime Patrol）

继"启动自愈"之后，守护层的**运行时**一半：定期观察系统运行日志与遥测，分析异常，**主动**发现并修复运行期问题（而非等下次启动或等用户反馈）。

#### 4.6.1 数据源（复用现有，不新造）
- **结构化遥测** `monitoring/usage_tracker.py:17 UsageTracker`：`record_model_call` / `record_tool_call` / `record` 已落 SQLite。巡检直接查"最近窗口"的失败计数、错误率、延迟分位、retry 触发次数 → 量化信号（比解析文本日志更稳）。
- **文本日志** `agent.log`（`logger.py:32 TimedRotatingFileHandler` 写，按时间滚动归档）：tail 最近 N 行，抓 `ERROR`/`CRITICAL`/`Traceback` 与异常类型聚类 → 定性信号（栈信息、根因线索）。
- 两者互补：遥测给"发生了什么、频率多少"，日志给"为什么"。

#### 4.6.2 调度（复用 `main.py:124` 后台任务范式）
- 在 `main.py:103 lifespan` 里 `asyncio.create_task(_patrol_loop(config))`，与现有 `_load_mcp_tools_background` 同构。
- 循环体：`while True: await _run_patrol(); await asyncio.sleep(interval)`，`interval` 取自新增配置 `self_healing_interval_seconds`（默认 600=10min）。
- **自愈任务自身必须自吞异常**：整轮 `try/except`，任何错误只记日志、绝不抛回事件循环（守护层不能成为新的不稳定源）。

#### 4.6.3 分析器（两阶段，省钱）
- **Stage 1 启发式（零 LLM，每轮必跑）**：对遥测+日志套规则，命中才进 Stage 2。例：
  - 同一 session/tool 连续 N 次 `error`；
  - 窗口内错误率 > 阈值；
  - 同一异常类型（Traceback class）反复出现；
  - 某工具失败 M 次 / 模型调用 p95 延迟或 retry 尖刺；
  - 多次启动失败（与 boot 自愈呼应）。
- **Stage 2 LLM（仅当 Stage 1 命中，且用 `review_model`）**：把命中的日志片段+遥测汇总喂给 review LLM，产出结构化 finding：`{symptom, root_cause_hypothesis, severity, candidate_healers}`。稳态下（无异常）不调 LLM，零成本。

#### 4.6.4 自愈器注册表（healers）
每个 healer = `{name, can_handle(finding)->bool, heal(finding)->result}`。内置：
- `quarantine_bad_skill`：finding 指向某"生成的技能"在报错 → 委托 guardian 把该技能移入 `quarantine/` 并 `reload_skills()`。
- `revert_config_patch`：某次 config 改动与错误相关 → 经 apply 闸门回退到 `config.json.bak`。
- `clean_zombie_sessions`：标记/清理卡死会话（`session_store`）。
- `tune_runtime_param`：调大超时等运行时参数（走安全 config 补丁 + apply 闸门 + 审计）。
- `write_pitfall_memory`：把 finding 以 `pitfall` 写入 `LocalMemory`（`_avoid_` 前缀）→ **闭环回 Phase 1/5 的负向注入**。
- `trigger_reflect`：对某失败 session 调 `reflect_on_task(outcome="error")` 挖坑记忆。
- `escalate_to_human`：无 healer 匹配、或动作属高风险 → 写告警报告到固定位置，**不**自动执行，等人工。

#### 4.6.5 安全与门控（ponytail 视角）
- **新开关 `enable_self_healing: bool = False`**（仿 `enable_loop_guard`，`config.py:113` 范式）：所有巡检逻辑门控，默认关；可与 `enable_self_evolution` 并列或合并，P1 实现时定。
- **风险分级**：低风险自愈（写记忆、清理会话、隔离已标记的坏技能）可全自动；**高风险自愈**（重启进程、删数据、改行为的 config 回退）必须过 `_approval_gate`（复用 §4.2/§3.3.6 的占位），当前恒 `False` → 不自动执行，只 `escalate_to_human`。
- **审计日志**：每轮巡检通过 §4.7 的 `EvolutionAuditStore.audit()` 写一条结构化记录（不再散落 `LocalMemory`）。既可观测、可复盘，也是 Phase 5 度量"进化有没有用"的原始信号源（见 §4.7）。
- **成本有界**：周期长（默认 10min）+ LLM 仅异常时触发；巡检任务异常自吞，不影响主流程。
- **隔离**：与记忆/反馈同套按用户隔离机制；巡检是全局运维任务，作用于进程级，不串用户会话状态。

#### 4.6.6 落点
- 新模块 `agent_core/patrol.py`：`_patrol_loop(config)`、`_run_patrol()`、`analyze(telemetry, logs)`、`HEALERS` 注册表、`heal(finding)`。
- 复用：`monitoring/usage_tracker.py`（遥测查询）、`logger.py` 的 log 路径、`guardian.quarantine_*`（坏技能隔离）、`agent.reload_skills()`、记忆注入链路。
- `main.py:103 lifespan` 加 `asyncio.create_task(_patrol_loop(config))`。
- `config.py` 加 `self_healing_interval_seconds` + `enable_self_healing`（含 env_map/save/to_api_dict）。

#### 4.6.7 测试 `tests/test_patrol.py`
- 注入一段含 `Traceback` 的日志 + 高错误率遥测 → `_run_patrol()` 命中 Stage 1 → 不调 LLM 也能产出 finding 并触发 `write_pitfall_memory`（断言 `LocalMemory` 出现 `_avoid_`）。
- `quarantine_bad_skill`：给定一个报错技能 → 断言进 `quarantine/` 且 `reload_skills` 被调用。
- 高风险 healer（如 revert_config_patch）在 `_approval_gate` 返回 `False` 时不执行、只 `escalate_to_human`（断言未改 config、产生告警记录）。
- 巡检循环 `try/except` 自吞：mock 一个 healer 抛异常 → 断言循环继续、进程未崩。

### 4.7 进化审计视图（管理员可观测，跨层）

用户要求：巡检发现的坑、修复的问题、改动了哪些内容、产物是什么——**管理员要能在一个地方看到**。这是覆盖全部进化相关事件的统一可观测层（不止巡检，也含反馈、守护回退、P3 技能生成）。

#### 4.7.1 问题：审计信息现在散落
- 巡检记录：原 §4.6.5 拟写 `LocalMemory`（但 LocalMemory 是**按用户隔离**的 KV，而巡检/进化是**进程级 admin 视角**，放错地方）。
- 守护产物：`skills/.generated/manifest.json`、`skills/.quarantine/`、`.boot_ok`、`config.json.bak`（文件系统，无统一索引）。
- 记忆/反馈：`_learned_` / `_avoid_` / `_feedback_`（per-user LocalMemory，散）。
- 管理员需要一份**跨用户、时序、可查询、带 diff 与产物链接**的聚合记录。

#### 4.7.2 统一审计存储 `EvolutionAuditStore`
- 落点：新建 `agent_core/evolution/audit_store.py`，**复用 `monitoring/usage_tracker.py:17` 的 SQLite 范式**（同一进程已有 SQLite 遥测库，最省、最一致）。单表 `evolution_audit`：
  - `id` INTEGER PK
  - `ts` TEXT（ISO 时间）
  - `source` TEXT（`patrol` | `feedback` | `guardian` | `skill_gen` | `manual`）
  - `category` TEXT（`pitfall` | `fix` | `config_change` | `skill_change` | `quarantine` | `escalation` | `feedback`）
  - `severity` TEXT（`info` | `warn` | `error` | `fatal`）
  - `summary` TEXT（给管理员看的一句话）
  - `detail` TEXT（JSON：改了什么、`before`/`after` diff、根因）
  - `artifacts` TEXT（JSON 数组：产物路径/key，如 `skills/.quarantine/foo.md`、`config.json.bak`、`_avoid_<hash>`）
  - `outcome` TEXT（`auto_fixed` | `escalated` | `auto_reverted` | `pending` | `approved`）
  - `actor` TEXT（`auto` | `admin`）
- **单一写入点**：所有进化相关代码（patrol healers、feedback 处理、guardian 回退、P3 技能生成）在动作发生时调用 `audit.log(...)` 写一条。不散落。

#### 4.7.3 管理端 API（复用 `api/routes/` 结构与鉴权）
新增 `api/routes/admin_evolution.py`（或并入 `system.py`，其已有 `/users/me` 与角色概念）：
- `GET /admin/evolution/audit?source=&category=&since=&limit=` → 时序列表（分页、过滤）。
- `GET /admin/evolution/audit/{id}` → 详情（完整 diff + 产物链接）。
- `GET /admin/evolution/artifacts` → 列出当前留存产物（隔离技能、config 备份、记忆条目），可查看/下载。
- `GET /admin/evolution/health` → 汇总计数（待处理升级数、已自动修复数、生成技能数、当前激活坑记忆数）。
- `POST /admin/evolution/audit/{id}/action` → 对 `escalated`/`pending` 项执行**批准/回退/忽略**（接 §3.3.6 `_approval_gate` 与 §4.2 回退机制）。
- 全部 **admin 角色鉴权**（复用现有登录中间件 + 角色判断，仿 `system.py` 鉴权方式）。

#### 4.7.4 管理员看到的具体内容映射
- **巡检发现的坑**：`source=patrol` 记录，`detail` 含 symptom / root_cause（来自 §4.6.3 finding）。
- **修复的问题**：`category=fix` 记录，`summary` 列"做了什么"（隔离某技能 / 清理某会话 / 写了某条 pitfall 记忆）。
- **改动的内容**：`detail.before`/`detail.after` 字段级 diff——config 改动给字段 diff；技能改动给文件路径；记忆改动给 key+value。
- **产物是什么**：`artifacts` 数组列出具体文件/key：
  - 隔离的坏技能：`skills/.quarantine/<name>.md`
  - 配置备份：`config.json.bak`
  - 新增坑记忆：`_avoid_<hash>`（值）
  - 生成技能（P3）：`skills/.generated/<name>/SKILL.md`
  - 用户反馈：`_feedback_<session>_<ts>`

#### 4.7.5 前端（后续，先看 API）
- `desktop/` 新增"进化/自愈"管理页：时间线卡片（来源/严重度/摘要）、点开看 diff + 产物、对 `escalated` 项有"批准/回退/忽略"按钮。P1 先把 API 做出来，UI 后续单独补（与反馈按钮同策略）。

#### 4.7.6 测试 `tests/test_evolution_audit.py`
- patrol healer 执行后断言 `evolution_audit` 出现对应记录且 `artifacts` 含预期路径。
- `GET /admin/evolution/audit` 按 `source=patrol` 过滤返回正确子集。
- 非 admin 角色访问被拒（鉴权）。

#### 4.7.7 落点
- 新模块 `agent_core/evolution/audit_store.py`（复用 `usage_tracker.py` 的 SQLite 范式）。
- 改 `agent_core/patrol.py` 各 healer → 改调 `audit.log(...)`（替代原 `_patrol_<ts>` 入 LocalMemory 写法）。
- 新路由 `api/routes/admin_evolution.py`（或并入 `system.py`）。
- 复用：`monitoring/usage_tracker.py` 的 SQLite 模式、`api/routes/system.py` 鉴权、`skills/.quarantine/` 与 `config.json.bak` 状态文件。

---

## 5. 已确认决策（评审拍板）

1. **进化力度**：目标 = 全自主自改写；第一版 = Phase 1 加固闭环起步，地基按"能平滑跑到全自主"设计。
2. **守护层激活范围**：`boot 自愈`常开（与进化开关无关，零成本保命）；`apply 闸门`仅由进化代码（P3）调用。
3. **设计存档**：本设计落为仓库根目录 `DESIGN.md`。
4. **落地方式**：后端优先，前端 👍/👎 按钮后续单独补。
5. **反思模型**：优先用 `review_model`（config:97-98），未配则回退主模型（P1 接受）。
6. **运行时巡检自愈**（用户新增要求）：守护层扩展出"运行时一半"——定时读 `agent.log` + `UsageTracker` 遥测，启发式+（异常时）LLM 分析，自动自愈或升级人工。复用 `main.py:124` 后台任务范式 + `guardian` 隔离机制，新增 `enable_self_healing` 开关默认关，高风险动作过 `_approval_gate`（当前恒 False，仅升级人工）。详见 §4.6。
7. **进化审计视图（管理员可观测）**（用户新增要求）：巡检发现的坑 / 修复的问题 / 改动内容 / 产物，要能让管理员在一个地方看到。新增统一 `EvolutionAuditStore`（SQLite，复用 `usage_tracker.py` 范式），覆盖 patrol/feedback/guardian/skill_gen 全部事件，带 `before/after` diff + `artifacts` 产物链接；管理端 API `GET /admin/evolution/audit` 等，admin 角色鉴权。详见 §4.7。

---

## 6. 风险与约束

- **成本**：反思 = 额外 LLM 调用，必须异步 + 15s 超时 + 优先 review_model，绝不阻塞主流程（现有 `_async_reflect` 已是 background task）。
- **隔离**：记忆按用户隔离（`LocalMemory`），反馈按 session 校验归属，不串用户。
- **最小改动**：全程复用 `LocalMemory` / `_async_reflect` / `reload_skills`，不新造抽象；结构化值向后兼容旧纯字符串。
- **开关**：默认关，门控所有新逻辑；开启后才生效。
- **守护层零依赖**：纯 stdlib，不 import 业务模块，自身不可能是启动失败源。

---

## 7. 落点索引（file:line）

| 模块 | 符号 | 行号 |
|---|---|---|
| agent.py | `_build_system_prompt` | 804 |
| agent.py | `_load_learned_patterns` | 845 |
| agent.py | `reflect_on_task` | 917 |
| agent.py | `reload_skills` | 1865 |
| services/agent_service.py | `_async_reflect` | 216 |
| memory/local_memory.py | `LocalMemory` / `get` / `set` / `list_items` | 15 / 33 / 39 / 58 |
| config.py | `review_provider_id` / `review_model` | 97 / 98 |
| config.py | `enable_loop_guard`（flag 范式锚点） | 113 |
| config.py | `env_map`（含 enable_loop_guard） | 216 |
| config.py | `save()` | 365 |
| config.py | `to_api_dict()` | 376 / 413 |
| api/routes/system.py | `/settings` GET/POST、flag 读写 | 19 / 109 / 123 / 150 |
| api/routes/agent.py | `_async_reflect` 调用点 | 160 / 373 |
| main.py | `_apply_pending_update_at_boot`（原子 swap 范式） | 30 |
| main.py | `lifespan` | 103 |
| main.py | `init_agent` | 229 |
| logger.py | `TimedRotatingFileHandler` / `agent.log` 路径 | 32 / 121 |
| monitoring/usage_tracker.py | `UsageTracker` / `record_model_call` / `record_tool_call` | 17 / 116 / 153 |
| agent_core/patrol.py | `_patrol_loop` / `analyze` / `HEALERS`（新增模块） | — |
| agent_core/evolution/audit_store.py | `EvolutionAuditStore.audit`（新增模块，复用 usage_tracker SQLite 范式） | — |
| api/routes/admin_evolution.py | `GET /admin/evolution/audit` 等管理端审计端点（新增） | — |
