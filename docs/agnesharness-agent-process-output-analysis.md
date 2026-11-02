# agnes-harness Agent 过程输出设计借鉴分析

## 背景

本次分析围绕 `agnes-harness` 的 agent 过程输出设计，重点看它如何把工具调用、过程步骤、审批、错误、子任务、耗时等信息整理成可读的 UI 输出。目标是提取可复用的设计点，供后续改进 agent 类前端展示、事件流结构或工具摘要输出。

## 主要结论

`agnes-harness` 最值得借鉴的不是某个组件，而是一套“从原始节点到人类可读过程”的分层设计：

1. **工具调用被翻译成动词短语**
   - 不再直接展示原始 JSON 参数，而是输出类似 `读取 3 个文件，执行 2 条命令` 的摘要。
   - 这是最值得直接借鉴的一点。

2. **连续步骤被聚合成分组**
   - 同类型工具调用被合并成一组，组标题显示聚合统计。
   - 运行中显示当前步骤，完成后显示总览。

3. **状态被翻译成可读短语**
   - `running / awaiting_approval / failed / done` 被转换为自然语言状态。
   - 让过程输出更像“说明发生了什么”，而不是“堆状态码”。

4. **结构化和散文内容分开渲染**
   - 多行文本、Markdown 输出走 prose 渲染。
   - 对象、JSON、短值走 code 渲染。
   - 这能显著提升长输出的可读性。

5. **子任务采用嵌套而不是平铺**
   - 子 agent 的步骤收进父工具卡片，配合历史导航展示。
   - 对多代理场景尤其有价值。

## 关键设计点详解

### 1. 工具调用 → 人类可读摘要

#### 核心思路
把“工具名 + 参数”转成“动词 + 对象”的简短行。

#### 实现特征
- 通过正则匹配工具名，归类为：
  - `web`
  - `command`
  - `read`
  - `edit`
  - `list`
  - `search`
  - `tool`
- 每类使用不同的“目标字段”：
  - `read / edit` → `path`
  - `search` → `pattern / query`
  - `command` → `description / command`
  - `web` → `url / query`

#### 收益
- 前端不必自己解析参数
- 用户能秒级理解“这一步在干什么”
- 适合把 agent 过程压缩到一两行

#### 可借鉴点
- 给工具事件增加 `toolSummary`
- 在协议层生成摘要，而不是纯靠 UI 推断
- 对 `run_shell` 类工具，优先展示 `description`，没有则展示命令首行

---

### 2. 连续步骤聚合

#### 核心思路
把连续发生的多条工具调用合并成一组，组标题做统计汇总。

#### 示例
```
⊕ 读取 3 个文件，执行 2 条命令
```

展开后才逐条显示：
```
read src/index.ts
read src/app.ts
run npm test
```

#### 收益
- 降低视觉噪音
- 更适合长任务
- 让“过程输出”接近人类叙述

#### 可借鉴点
- 前端可做两级展示：
  - 收起态：组摘要
  - 展开态：子步骤
- 对运行中任务显示当前步骤
- 对已结束任务显示统计摘要

---

### 3. 状态短语化

#### 核心思路
把枚举状态翻译为面向读者的短语，而不是原始状态名。

#### 示例
- `runningTool` → 正在执行工具
- `awaitingToolApproval` → 等待批准
- `replying` → 正在回复
- `thinking` → 正在思考

#### 收益
- 更适合 C 端或桌面端
- 降低理解成本
- 对非技术用户更友好

#### 可借鉴点
- 给 turn 增加 `statusLabel`
- 给 tool 增加 `statusText`
- 由前端统一做 i18n 映射，不要散落各处

---

### 4. 过程步骤与最终回答分离

#### 核心思路
`process step` 属于“过程记录”，不等于最终回答。

#### 实现特征
- 如果某节点是过程记录，就不把它当成 final answer
- 最终回答单独展示
- 避免把中间步骤误当成结论

#### 可借鉴点
- 区分：
  - `process_nodes`
  - `final_answer_nodes`
- 在事件流里显式打标

---

### 5. 结构化输出与 prose 输出分流

#### 核心思路
不同内容形态使用不同渲染：
- 多行文本 / Markdown → prose
- 短值 / 对象 / JSON → code

#### 可借鉴点
- 对 `run_shell` 输出：
  - 短输出直接展示
  - 长输出折叠
  - 可解析 JSON 时提供复制
- 对 `file` 类工具：
  - 展示路径摘要
  - 展开后显示内容或 diff

---

### 6. 子任务嵌套展示

#### 核心思路
不把子 agent 步骤平铺到主对话中，而是收进父步骤里。

#### 可借鉴点
- 适合 `delegate_task`
- 适合并行子任务
- UI 上可以：
  - 父卡片收起时显示状态
  - 展开后进入子任务页
  - 用历史栈支持进退

## 对你当前项目的直接建议

### 建议 1：给 agent 过程输出加一层“摘要协议”
不要只发原始工具事件，建议增加：
- `tool_summary`
- `group_summary`
- `status_text`
- `is_process_record`

这样前端可以少做推测，展示更稳。

### 建议 2：`run_shell` 优先展示命令意图
对 `run_shell` 最适合作为第一个试点：
- 有 `description` → 展示 `description`
- 无 `description` → 展示命令首行
- 长命令自动截断
- 失败时展示 `exit_code` / `stderr` 摘要

### 建议 3：前端按“组”展示长过程
对 agent 连续多次调工具的场景：
- 同组连续步骤折叠
- 组标题用自然语言统计
- 当前运行项高亮

### 建议 4：把“失败原因”单独结构化
建议增加：
- `error.kind`
- `error.message`
- `error.hint`
- `error.retryable`

这比只展示错误文本更适合 UI 和自动化重试。

## 最值得直接抄的 3 个点

1. **工具调用自动摘要**
   - 从 `name + args` 生成一行可读目标
   - 最应该先做

2. **连续步骤分组统计**
   - 提升长任务可读性
   - 最适合 web/桌面端

3. **状态文本化**
   - 成本最低
   - 对体验提升明显

## 结论

`agnes-harness` 的 agent 过程输出设计核心优势在于：
- 把原始 agent 事件变成“人类能读懂的过程”
- 把长过程压缩成可折叠的层级
- 在可观测性和可读性之间做了较好平衡

如果要继续推进，建议优先级：
1. 先做 `run_shell` 的摘要和分组
2. 再补 `status_text` 与错误结构化
3. 最后做子任务嵌套展示

## 参考位置

- `packages/web-ui/src/conversation/messages/turn.tsx`
- `packages/web-ui/src/conversation/messages/tool-step.ts`
- `packages/web-ui/src/conversation/messages/process-step.ts`
- `packages/web-ui/src/conversation/messages/tool-card.tsx`
- `packages/web-ui/src/conversation/messages/activity-row.tsx`
