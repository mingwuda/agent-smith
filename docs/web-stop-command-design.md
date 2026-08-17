# Push 队列设计（Web 端 + 微信端）

## 背景
- agent 长任务/流式执行期间，用户输入的消息原会被丢弃（Web）或只能 `/stop` 打断（微信）。
- 用户诉求：执行中输入的补充内容不要打断当前回复，而是**入队**，当前轮结束后按序自动发送。命名为 `/push`（入队之意）。

## Web 端交互
1. **流式执行中按 Enter**：输入内容入队到该会话的 `rt.interventionQueue`，清空输入框，显示系统提示「已加入队列，当前回复结束后自动发送」。不打断当前回复。
2. **本轮结束后**：`send()` 的 `finally` 中按 FIFO 取一条队列消息自动发起新一轮（一次只取一条，剩余留待下一轮）。队列消息不会二次入队。
3. **停止**：红色发送按钮（loading 态）仍为「停止」，行为不变（`stopCurrentRun`）。
4. 队列挂在会话 runtime 上，用户切走后台会话结束也不丢（drain 在 runtime 清理之前）。

## 微信端交互
- `/push <内容>`：
  - 任务执行中 → 入队到 `_push_queues[from_user]`，回复「已入队（当前任务结束后自动发送，队列 N 条）」；
  - 空闲 → 直接按普通消息完整处理（持 `_msg_lock` 串行）。
- 当前任务的 `_handle_message` 尾部调用 `_flush_push_queue`，按序递归处理队列直到清空（合成 `message_id` 绕过 `_seen_msg_ids` 去重）。
- `/stop` 保留原中断语义，未受影响。

## 技术改动
- `desktop/js/features/streaming.js`：`send(queuedText)` 签名 + 流式中入队 + finally drain。
- `desktop/js/core/i18n.js`：新增 `pushQueued` 文案（中/英）。
- `agent_core/wechat_bot.py`：`_push_queues` / `_push_seq` 初始化、`_flush_push_queue`、`_handle_message` 尾部 flush。
- `agent_core/wechat_commands.py`：`/push` 命令分支 + `/help` 更新。

## 兼容性
- 微信 Bot `/stop` 不受影响。
- 移动端/响应式布局无额外改动。
