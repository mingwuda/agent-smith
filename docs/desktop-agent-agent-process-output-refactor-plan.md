# desktop-agent Agent 过程输出改造方案

> 基于 `agnesharness-agent-process-output-analysis.md` 的发现，落到 desktop-agent 的真实代码路径上。
> 现状已有很好的基础（三段式卡片、`getToolLabel` 摘要、`tool_result` 带 `duration_ms/error/diff`），本方案是**补齐缺口、对齐 agnes-harness 的"人类可读过程"标准**，不是推倒重来。

---

## 0. 现状盘点（先认清已有 vs 缺失）

### 已有（不重复造轮子）
| 能力 | 位置 | 说明 |
|---|---|---|
| 三段式卡片（耗时/工具/回答） | `desktop/js/features/streaming.js` `beginRoundRender` ~500 | 结构清晰，分段折叠 |
| 工具名→摘要行 | `streaming.js` `getToolLabel` ~2452 | 已能按 tool+args 出"读取文件内容: xxx" |
| `tool_result` 真实耗时 | `agent_core/agent_run.py` ~1375 `duration_ms` | 已下发，回放不再估算 |
| 错误/ diff 标记 | 同上 `error`/`diff`/`diff_file_path` | 已有基础 |
| 事件落盘回放 | `agent_core/stream_log.py` | 刷新/后台续跑可恢复 |

### 缺失（本方案要补的 4 个缺口）
1. **摘要覆盖面太窄**：`getToolLabel` 只认 `file_path / code / command / query` 四种参数键，浏览器、git、db、memory、todo、subagent 等工具的"对象"无法提炼成一行。
2. **连续步骤不平铺也不分组**：每个工具都是独立一张卡，长任务里几十张卡平铺，噪音大；没有"读取 3 个文件、执行 2 条命令"的聚合组。
3. **状态是状态码，不是短语**：`isError`/`dot.className` 用颜色点，缺少"正在执行 / 等待批准 / 执行失败(退出码1)"这类自然语言短语。
4. **run_shell 无意图摘要**：命令本身可能就是 `python /a/b/c.py --x --y`，没有"这条命令在干什么"的一行说明。

---

## 1. 改造总览（按 ROI 排序，先做高价值低成本）

```
P0  [低成本/高价值] 工具摘要扩面        getToolLabel  → toolSummary()
P0  [低成本/高价值] 状态短语化          dot+颜色       → statusText
P1  [中成本/高价值] 连续步骤分组        平铺卡片       → 分组折叠
P1  [中成本/中价值] run_shell 意图摘要  命令首行       → description 优先
P2  [高成本/中价值] 子任务嵌套          平铺           → PushStack
```

原则（lazy）：**P0 两项改 1 个前端函数 + 1 处后端字段，收益最大，先做**；P1/P2 在 P0 稳定后逐步推进。

---

## 2. P0-A 工具摘要扩面：`getToolLabel` → `toolSummary`

### 2.1 现状问题
`streaming.js:2452` 的 `getToolLabel` 用一个 `labels` 表 + 4 个 if 分支（file_path/code/command/query）。导致：
- `browser_navigate`/`git_status`/`db_query`/`memory_tools`/`manage_todo`/`delegate_task` 等工具拿不到"对象"，只显示"调用工具: xxx"。
- 路径没有 `~` 缩写，长路径吃掉摘要行。

### 2.2 改造：抽成可分类的 `toolSummary(name, args)`
参考 agnes-harness `tool-step.ts` 的 `toolKind` + `TARGET_KEYS` 双表设计。

**前端（推荐，零后端改动）**：`streaming.js` 新增/重写 `getToolLabel`，扩展目标键映射：

```js
// ponytail: 对齐 agnes-harness tool-step.ts —— 按工具名归类 → 取"对象"键
const TARGET_KEYS = {
  read_file:  ['path', 'file_path'],
  write_file: ['path', 'file_path'],
  edit_file:  ['path', 'file_path'],
  search_files:['pattern', 'query'],
  git_status: ['path'],
  db_query:   ['sql'],
  run_shell:  ['command'],          // 见 P1-B：优先 description
  web_search: ['query'],
  browser_navigate: ['url'],
  delegate_task: ['task'],
};
function toolSummary(name, args) {
  const kind = pickKind(name);            // 复用现有 getToolIcon 的分类思路
  const keys = TARGET_KEYS[name] || targetKeysFor(kind);
  let target = keys.map(k => args?.[k]).find(v => typeof v === 'string' && v.trim());
  target = shortenHome(target);           // /Users/zhangming/... → ~/...
  return `${verbOf(kind)} ${target || ''}`.trim();
}
function shortenHome(text){ return text.replace(/^\/(?:Users|home)\/[^/\s]+/, '~'); }
```

**调用点（仅 2 处）**：`streaming.js:1539`（`tool_start` 更新 active line）、`streaming.js:1682`（`tool_result` 完成态）。把 `getToolLabel(...)` 换成 `toolSummary(...)` 即可。

**收益**：所有工具自动获得"动词+对象"摘要，长路径被缩写，行内可读。

---

## 3. P0-B 状态短语化

### 3.1 现状
`streaming.js:1680` 用 `dot.className = 'error'|'done'` + 颜色点表达状态；`tool_result` 文案是"已完成/执行失败"。缺少失败原因（退出码、错误首行）。

### 3.2 改造
后端 `agent_run.py` 在 `tool_result` 事件里**增加 1 个字段**（对齐报告 §4 的 error.kind/message）：

```python
# agent_run.py ~1364 处 yield tool_result 前
error_kind = None
error_msg  = None
if is_error:
    # run_shell 提取退出码；其他工具提取首行错误
    if tool_name == 'run_shell':
        m = re.search(r'exit_code[=:]\s*(\d+)', output_str) or re.search(r'exit(?:\s|_)?code[:\s]+(\d+)', output_str)
        if m: error_kind = f'exit_code_{m.group(1)}'
    error_msg = (output_str.split('\n') or [''])[0][:120]

yield _sse({
    "type": "tool_result", ...,
    "error": is_error,
    "error_kind": error_kind,          # 新增
    "error_msg": error_msg,           # 新增
})
```

前端 `streaming.js:1680` 把状态点旁补一行短语：
```js
var noteEl = _currentActiveLine.querySelector('.active-action-note');
if (noteEl) noteEl.textContent = isError ? (data.error_msg || (data.error_kind || '')) : '';
```
效果：`运行 Shell 命令 · 执行失败 (exit_code_1)` —— 对齐 agnes-harness `ActivityRow.note`。

**落盘**：`stream_log.py` 自动把新字段一起写进 jsonl（它写的是整个 event dict，无需改），回放直接可用。

---

## 4. P1 连续步骤分组

### 4.1 现状
每个 `tool_start` 都新建一张 `.tool-card` 平铺进 `_historyBodyEl`（`streaming.js` `tool_start` 段 ~1510）。长任务 30+ 张卡。

### 4.2 改造（对齐 agnes-harness `activitySummary` + 分组）
在前端历史区加"组"概念：连续同类工具归一组，组头一行统计，展开才逐张卡。

**前端实现要点**（`streaming.js`）：
1. 维护 `_currentGroup = { kind, steps: [] }`。
2. `tool_start` 时：若 `kind` 与上一张卡相同 → 追加进 `_currentGroup`；否则 → 落组（渲染组头）并开新组。
3. 组头渲染（`streaming.js` 新增 `renderGroupHeader`）：
   ```
   ⊕ 读取 3 个文件，执行 2 条命令      ← activitySummary 等价
   ```
   计数文案对齐报告 §2 的"动词+数字"：`读取 3 个文件` / `执行 2 条命令`。
4. 正在运行那一组 → 组头显示**当前步**（"正在执行 npm test"）而非统计；完成后才切回统计。
5. 组默认**收起**（长任务降噪），组内卡保持现有 `.tool-card` 交互不动。

**收益**：30 张卡 → 几行组头 + 可展开明细，过程读起来像叙述。

**边界**：浏览器操作（`browser_*`）会弹出浮动窗（`streaming.js:1555` `showBrowserFloat`），属于"attention"类，**不要并进普通组**，单独高亮（对齐 agnes-harness `attention` 节点）。

---

## 5. P1-B run_shell 意图摘要

### 5.1 现状
`run_shell` 摘要只显示命令首行（`streaming.js:2452` `command` 分支），`python a.py --x` 看不出意图。

### 5.2 改造
1. **后端**：`agent_core/tools/shell_tools.py` 的 `run_shell` 返回值里已有 stdout；但"意图"最好由 agent 在调用时给。最简做法——**在 `tool_start.args` 透传可选 `description`**（agent 调 `run_shell(command, description="安装依赖")` 时带上）。
   - 给 `run_shell` 增加可选参数 `description: str = ""`（不破坏现有签名，纯增强），透传进 SSE。
2. **前端**：`toolSummary` 里 `run_shell` 优先取 `args.description`，没有才取 `command` 首行（P0-A 的 `TARGET_KEYS` 已留了注记）。
   - 效果：`安装依赖` > `pip install -r requirements.txt`。

**lazy 取舍**：不强制改所有调用方，agent 不带 description 就优雅降级到命令首行，零风险。

---

## 6. P2 子任务嵌套（可选，最后做）

### 现状
`delegate_task`/`delegate_tasks_parallel` 的子代理卡片是平铺进 `currentStepsEl`（`streaming.js` `renderSubagentCards` ~1040，用 `data-cap-id`/`sa-card`）。

### 改造（对齐 agnes-harness `delegationView` + `SubtaskPages`）
- 子代理卡收进父 `.tool-card`（父为 `delegate_task` 时）。
- 用 `desktop/js/core/plugin-ui.js` 或新增 `subtask` 面板做"子任务页"，复用现有 `PushStack` 式导航（前进/后退）。
- **成本高、价值中等**，建议 P0/P1 验证后再做；也可维持现状（子代理卡已有折叠，体验可接受）。

---

## 7. 落地顺序与验收

| 阶段 | 改动文件 | 工作量 | 验收 |
|---|---|---|---|
| P0-A | `streaming.js` | 小 | 所有工具摘要可读、路径带 `~` |
| P0-B | `agent_run.py` + `streaming.js` | 小 | 失败步骤显示退出码/错误首行 |
| P1  | `streaming.js`（分组）+ `shell_tools.py`（description） | 中 | 长任务组头统计、run_shell 意图 |
| P2  | `streaming.js` + 新 `subtask` 面板 | 大 | 子任务可嵌套导航 |

**每个 P0/P1 改动留 1 个可跑校验**（ponytail 要求）：
- `toolSummary` 加 1 个 `assert` 自测（`run_shell` 带 description → 出意图；浏览器工具 → 出 url）。
- P1 分组加 1 个最小用例：构造 3 个同 kind 的 `tool_start`，断言组头统计为"读取 3 个文件"。

---

## 8. 风险与不做什么

- **不推倒现有三段式**：P0/P1 都是在现有卡片结构上增字段/加分组，不重构布局。
- **分组不影响回放**：`stream_log.py` 按 event 顺序落盘，分组是纯前端渲染层，回放事件不变，无兼容性风险。
- **description 纯可选**：P1-B 不破坏 `run_shell` 现有调用方。
- **明确不做**：服务端生成摘要（前端 `toolSummary` 已足够，且 i18n 更灵活）；子任务嵌套默认不做，除非 P2 排期。

---

## 9. 一句话总结

desktop-agent 已经有 80% 的骨架，本方案补齐的是**"让工具步骤读起来像人类在说发生了什么"**：
1. 工具摘要扩面（`toolSummary`）
2. 状态短语化（`error_kind/error_msg`）
3. 连续步骤分组（组头统计）
4. run_shell 意图（`description` 优先）

先做 P0（两个改动点），当天可看到全部工具摘要和失败原因的可读化；P1 在下一步把长任务过程"收拢"成组。
