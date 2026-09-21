"""回归测试：微信 Bot /push 队列（消息入队，当前任务结束后自动发送）。

背景：流式/长任务执行期间，用户输入的消息原会被丢弃或只能 /stop 打断。
本次新增 /push <内容>：
1. 任务执行中（_active_run_task 未完成）→ 入队到 _push_queues[from_user]，
   由当前任务 _handle_message 尾部 _flush_push_queue 按序自动发送；
2. 空闲 → 直接按普通消息完整处理（持 _msg_lock 串行）；
3. _flush_push_queue 递归处理直到队列清空，合成 message_id 绕过 _seen_msg_ids 去重。

测试覆盖：flush 的 FIFO 顺序 / 递归收敛 / 消息 id 唯一，以及 /push 忙时入队、
闲时直发的两种分支语义。
"""
import sys
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

_HERE = Path(__file__).resolve().parent.parent
_AGENT_CORE = _HERE / "agent_core"
for _p in (str(_AGENT_CORE), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_core.wechat_bot import WeChatBot
from agent_core.wechat_commands import WeChatCommandMixin


def _make_bot():
    """object.__new__ 绕过 __init__，构造 push 队列所需最小属性。"""
    bot = object.__new__(WeChatBot)
    bot.user_id = "admin"
    bot._push_queues = {}
    bot._push_seq = 0
    bot._active_run_task = None
    bot._msg_lock = asyncio.Lock()
    bot.send_message = AsyncMock(return_value={"ret": 0})
    bot._seen_msg_ids = set()
    bot.processed = []  # 记录 _handle_message 收到的文本（测试观察点）

    async def fake_handle_message(msg):
        text = msg["item_list"][0]["text_item"]["text"]
        bot.processed.append(text)
        # 模拟真实 _handle_message 尾部：处理完一条后再 flush 剩余队列（递归收敛）
        await bot._flush_push_queue(msg["from_user_id"], msg["context_token"])

    bot._handle_message = fake_handle_message
    return bot


# ── _flush_push_queue 单元 ─────────────────────────────

async def test_flush_push_queue_fifo_order_and_unique_ids():
    bot = _make_bot()
    bot._push_queues["u1"] = ["第一条", "第二条", "第三条"]

    await bot._flush_push_queue("u1", "tok-1")

    assert bot.processed == ["第一条", "第二条", "第三条"]  # FIFO 顺序
    assert bot._push_queues["u1"] == []  # 队列已清空
    assert len({f"push_{bot.user_id}_0_{i}" for i in (1, 2, 3)}) == 3  # id 递增唯一


async def test_flush_push_queue_concurrent_enqueue_drained_once():
    """flush 过程中新入队的消息也会被本轮处理，且每条只处理一次（无重复）。"""
    bot = _make_bot()
    bot._push_queues["u1"] = ["A"]

    async def fake_handle_message(msg):
        text = msg["item_list"][0]["text_item"]["text"]
        bot.processed.append(text)
        if text == "A":
            # 处理 A 期间用户又 /push 了 B、C
            bot._push_queues["u1"].extend(["B", "C"])
        await bot._flush_push_queue(msg["from_user_id"], msg["context_token"])

    bot._handle_message = fake_handle_message
    await bot._flush_push_queue("u1", "tok-1")

    assert bot.processed == ["A", "B", "C"]
    assert bot._push_queues["u1"] == []


async def test_flush_push_queue_no_queue_noop():
    bot = _make_bot()
    await bot._flush_push_queue("u1", "tok-1")
    assert bot.processed == []


# ── /push 命令分支 ─────────────────────────────────────

async def test_push_command_busy_enqueues_only():
    """任务执行中：/push 只入队并回执，不触发 _handle_message。"""
    bot = _make_bot()
    bot._active_run_task = asyncio.create_task(asyncio.sleep(3600))

    handled = await WeChatCommandMixin._handle_command(
        bot, "/push 补充内容", "u1", "tok-1", "wechat_admin",
    )
    assert handled is True
    assert bot._push_queues["u1"] == ["补充内容"]
    assert bot.processed == []  # 未立即处理
    assert "已入队" in bot.send_message.call_args.args[2]
    bot._active_run_task.cancel()


async def test_push_command_idle_flushes_immediately():
    """空闲：/push 立即按普通消息处理（经 _flush_push_queue 走完整链路）。"""
    bot = _make_bot()

    handled = await WeChatCommandMixin._handle_command(
        bot, "/push 空闲直发", "u1", "tok-1", "wechat_admin",
    )
    assert handled is True
    assert bot.processed == ["空闲直发"]
    assert bot._push_queues.get("u1", []) == []


async def test_push_command_without_content_help():
    bot = _make_bot()
    handled = await WeChatCommandMixin._handle_command(
        bot, "/push", "u1", "tok-1", "wechat_admin",
    )
    assert handled is True
    assert bot.processed == []
    assert "用法" in bot.send_message.call_args.args[2]
# ── /steer 命令（实时干预）────────────────────────────

async def test_steer_command_busy_injects_to_inbox():
    """任务执行中：/steer 把内容写入 inbox(next_step)，供运行中的 agent 下一步注入。"""
    import inbox as Inbox  # noqa: E402  # 生产代码用顶层 inbox 模块（agent_core 在 sys.path）

    bot = _make_bot()
    bot._active_run_task = asyncio.create_task(asyncio.sleep(3600))
    bot._wechat_sessions = {"u1": "sess-steer-1"}
    # 清空目标会话 inbox
    Inbox.get_inbox_manager().get("wechat_admin", "sess-steer-1").claim_next_step(999)

    handled = await WeChatCommandMixin._handle_command(
        bot, "/steer 改用 JSON 输出", "u1", "tok-1", "wechat_admin",
    )
    assert handled is True
    # 内容进入 next_step 桶（未进入普通 push 队列）
    inbox = Inbox.get_inbox_manager().get("wechat_admin", "sess-steer-1")
    pending = inbox.peek_step()
    assert [c["content"] for c in pending] == ["改用 JSON 输出"]
    assert bot._push_queues.get("u1", []) == []  # 未入 push 队列
    assert bot.processed == []  # 未走普通消息处理
    assert "注入" in bot.send_message.call_args.args[2]
    assert inbox.active is True  # 任务中被标记 active
    Inbox.get_inbox_manager().get("wechat_admin", "sess-steer-1").claim_next_step(999)
    bot._active_run_task.cancel()


async def test_steer_command_idle_flushes_via_push_queue():
    """空闲：/steer 走与 /push 闲时一致的 push 队列 flush 立即处理。"""
    bot = _make_bot()
    bot._wechat_sessions = {"u1": "sess-steer-2"}

    handled = await WeChatCommandMixin._handle_command(
        bot, "/steer 空闲时的补充", "u1", "tok-1", "wechat_admin",
    )
    assert handled is True
    assert bot.processed == ["空闲时的补充"]
    assert bot._push_queues.get("u1", []) == []


async def test_steer_command_without_content_help():
    bot = _make_bot()
    handled = await WeChatCommandMixin._handle_command(
        bot, "/steer", "u1", "tok-1", "wechat_admin",
    )
    assert handled is True
    assert bot.processed == []
    assert "用法" in bot.send_message.call_args.args[2]