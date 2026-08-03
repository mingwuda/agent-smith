"""回归测试：微信 Bot /stop 指令中断正在执行的 agent 请求。

背景：_poll_loop 原本串行 await _handle_message，agent 执行数十秒期间
轮询被阻塞，用户发送的 /stop 根本无法被收到。本次改动：

1. _poll_loop 并行派发 _dispatch_message；命令消息（含 /stop）即时处理，
   普通对话消息用 _msg_lock 串行排队（避免并发导致 history 乱序）；
2. agent 执行包成 self._active_run_task，/stop 通过 cancel 它中断请求；
3. 被中断的任务在 _handle_message 的 except CancelledError 分支产出
   "⏹️ 任务已中断"最终回复（复用统一保存/发送链路）。

测试覆盖：_cancel_active_run 三种状态、命令即时/普通排队的分派语义、
以及「挂起 agent 被 /stop 中断 → 收到中断确认」的端到端链路。
"""
import sys
import asyncio
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

_HERE = Path(__file__).resolve().parent.parent
_AGENT_CORE = _HERE / "agent_core"
for _p in (str(_AGENT_CORE), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agent_core.wechat_bot as wb
from agent_core.wechat_bot import WeChatBot


def _make_bot() -> WeChatBot:
    """object.__new__ 绕过 __init__，构造 _handle_message 所需最小属性。"""
    bot = object.__new__(WeChatBot)
    bot.user_id = "admin"
    bot._seen_msg_ids = set()
    bot._pending_images = {}
    bot._wechat_sessions = {}
    bot._wechat_session_menu = {}
    bot._wechat_current_project = {}
    bot._wechat_project_menu = {}
    bot._wechat_model_menu = {}
    bot._sessions_migrated = True
    bot._last_activity_at = 0.0
    bot._poll_delay = 0.0
    bot.step_reply_enabled = True
    bot.step_msg_batch = 3
    bot.step_msg_batch_timeout = 8.0
    bot.step_msg_budget = 30
    bot._step_sent_count = 0
    bot._send_timestamps = []
    bot.send_rate_window = 60.0
    bot.send_rate_max = 10
    bot._rate_limited_until = 0.0
    bot._last_send_at = 0.0
    bot._active_run_task = None
    bot._msg_lock = asyncio.Lock()
    bot._handle_tasks = set()
    bot._retry_queue = deque()
    bot._throttle_send = AsyncMock()
    bot.send_message = AsyncMock(return_value={"ret": 0})
    bot.send_typing = AsyncMock()
    bot._send_step_msg = AsyncMock()
    bot._save_current_project = Mock()
    bot._load_current_project = Mock()
    return bot


# ── _cancel_active_run 单元 ─────────────────────────────

def test_cancel_active_run_no_task():
    bot = _make_bot()
    assert bot._cancel_active_run() is False


async def test_cancel_active_run_cancels_pending_task():
    bot = _make_bot()

    async def hang():
        await asyncio.sleep(3600)

    task = asyncio.create_task(hang())
    bot._active_run_task = task
    assert bot._cancel_active_run() is True
    await asyncio.sleep(0)  # 让取消请求在事件循环中生效
    assert task.cancelled()


async def test_cancel_active_run_ignores_done_task():
    bot = _make_bot()

    async def quick():
        return 1

    task = asyncio.create_task(quick())
    await task
    bot._active_run_task = task
    assert bot._cancel_active_run() is False


# ── _dispatch_message 分派语义 ──────────────────────────

def _text_msg(text: str) -> dict:
    return {
        "from_user_id": "u1",
        "context_token": "tok-1",
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }


async def test_dispatch_command_not_blocked_by_msg_lock():
    """命令消息（/stop）不排队：即使 _msg_lock 被占用也立即处理。"""
    bot = _make_bot()
    await bot._msg_lock.acquire()  # 模拟普通消息正在跑（持锁）
    called = []

    async def fake_handle(msg):
        called.append(wb._extract_text(msg))

    bot._handle_message = fake_handle
    await bot._dispatch_message(_text_msg("/stop"))
    assert called == ["/stop"]
    bot._msg_lock.release()


async def test_dispatch_normal_msg_waits_for_msg_lock():
    """普通对话消息排队：锁被占用时等待，释放后才处理。"""
    bot = _make_bot()
    await bot._msg_lock.acquire()
    called = []

    async def fake_handle(msg):
        called.append(1)

    bot._handle_message = fake_handle
    task = asyncio.create_task(bot._dispatch_message(_text_msg("你好")))
    await asyncio.sleep(0.05)
    assert called == []  # 锁被占用，普通消息在排队
    bot._msg_lock.release()
    await task
    assert called == [1]


# ── 端到端：挂起 agent 被 /stop 中断 ────────────────────

async def test_stop_interrupts_running_agent(monkeypatch):
    """agent 执行中（挂起）收到 /stop → 任务被取消 → 收到"任务已中断"最终回复。"""
    import services.agent_service as sas
    import services.workspace as sw
    import agent_core.services.agent_service as acs

    class _FakeAgent:
        def __init__(self):
            self.started = asyncio.Event()
            self.config = SimpleNamespace(workspace="/tmp")

        def set_workspace(self, ws):
            pass

        async def chat_stream_events(self, message, attachments=None, thread_id="", history=None):
            self.started.set()
            yield {"type": "thought", "thought": "开始处理…"}
            await asyncio.sleep(3600)  # 模拟长时间执行

    fake_agent = _FakeAgent()
    bot = _make_bot()
    bot._ensure_agent = lambda: fake_agent

    # 隔离会话存储 / 工作区 / 结果保存，避免污染真实数据
    monkeypatch.setattr(wb.session_store, "get_session", lambda *a, **k: None)
    monkeypatch.setattr(wb.session_store, "list_sessions", lambda *a, **k: [])
    monkeypatch.setattr(wb.session_store, "create_session", lambda *a, **k: None)
    monkeypatch.setattr(wb.session_store, "add_message", lambda *a, **k: 1)
    monkeypatch.setattr(wb.session_store, "get_session_workspace", lambda *a, **k: None)
    monkeypatch.setattr(acs, "_apply_session_workspace", Mock())
    monkeypatch.setattr(sw, "_workspace_for_user", lambda uid: "/tmp")
    monkeypatch.setattr(sas, "_save_assistant_result", Mock())

    msg = {
        "from_user_id": "u1",
        "context_token": "tok-1",
        "message_id": "m-1",
        "item_list": [{"type": 1, "text_item": {"text": "帮我跑一个很长的任务"}}],
    }

    handle_task = asyncio.create_task(bot._handle_message(msg))
    await fake_agent.started.wait()
    # /stop：中断正在执行的任务
    assert bot._cancel_active_run() is True
    await handle_task
    # 收到"任务已中断"最终回复（复用统一发送链路）
    send_calls = bot.send_message.call_args_list
    assert any("任务已中断" in c.args[2] for c in send_calls)
    # 任务结束后 _active_run_task 已复位
    assert bot._active_run_task is None
