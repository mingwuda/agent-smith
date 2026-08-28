"""回归测试：微信 Bot 发送链路（send channel）核心机制。

拆分 wechat_bot.py（A 计划）前的安全网 —— 覆盖发送链路里
还没有测试保护的三个点：

1. _throttle_send：发送节流（与上一条消息保持最小间隔，避免短时高频触发频控）；
2. _send_step_msg：频控冷却中跳过 / 防御预算耗尽跳过 / 成功计数 / 失败容错（不重试）；
3. _parse_sendmessage_response：sendmessage 响应的解析边界
   （决定"这次发送算不算成功"，是整条必达链路的判定核心）。

风格与既有 wechat 测试一致：object.__new__ 绕过 __init__、asyncio.run 直跑
async 方法、可控时钟（patch 模块级 time.time，与实现解耦）。
"""
import sys
import asyncio
import time as _real_time
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, Mock

_HERE = Path(__file__).resolve().parent.parent
_AGENT_CORE = _HERE / "agent_core"
for _p in (str(_AGENT_CORE), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_core.wechat_bot import WeChatBot


# ── _parse_sendmessage_response：成功/失败判定边界 ────────────

def test_parse_empty_object_is_success():
    assert WeChatBot._parse_sendmessage_response("{}") == {"ret": 0}


def test_parse_ret0_is_success():
    assert WeChatBot._parse_sendmessage_response('{"ret":0}') == {"ret": 0, "message_id": ""}


def test_parse_ret0_with_message_id():
    r = WeChatBot._parse_sendmessage_response('{"ret":0,"message_id":"m-1"}')
    assert r["ret"] == 0 and r["message_id"] == "m-1"


def test_parse_message_id_only_counts_as_success():
    """iLink 成功响应可能只返回 message_id 不带 ret=0，必须视为成功。"""
    r = WeChatBot._parse_sendmessage_response('{"message_id":"m-2"}')
    assert r["ret"] == 0 and r["message_id"] == "m-2"


def test_parse_prepare_failed_detected():
    """频控失败（ret=-2 prepare failed）必须被识别，才能进入冷却窗口。"""
    r = WeChatBot._parse_sendmessage_response('{"ret":-2,"errmsg":"prepare failed"}')
    assert r["ret"] == -2


def test_parse_invalid_json_is_failure():
    r = WeChatBot._parse_sendmessage_response("not json")
    assert r["ret"] == -1 and "raw" in r.get("detail", {})


def test_parse_non_dict_is_failure():
    r = WeChatBot._parse_sendmessage_response("[1,2,3]")
    assert r["ret"] == -1


def test_parse_whitespace_ok():
    assert WeChatBot._parse_sendmessage_response("  {}  ") == {"ret": 0}


# ── _throttle_send：发送节流 ────────────────────────────────

class _Clock:
    def __init__(self, start: float = 1_000_000.0):
        self.t = start

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _make_throttle_bot(clock: _Clock) -> WeChatBot:
    bot = object.__new__(WeChatBot)
    bot.user_id = "test"
    bot._last_send_at = 0.0
    return bot


def test_throttle_first_send_no_wait(monkeypatch):
    """首次发送（_last_send_at=0）：不等待，直接记录发送时间。"""
    clock = _Clock()
    monkeypatch.setattr("agent_core.wechat_bot.time.time", clock.now)
    bot = _make_throttle_bot(clock)

    async def run():
        t0 = clock.now()
        await bot._throttle_send()
        return clock.now() - t0

    elapsed = asyncio.run(run())
    assert elapsed == 0.0
    assert bot._last_send_at == clock.now()


def test_throttle_waits_to_reach_min_interval(monkeypatch):
    """距上一条消息 0.2s：必须等到满 1.5s 最小间隔才放行。"""
    clock = _Clock()
    monkeypatch.setattr("agent_core.wechat_bot.time.time", clock.now)

    async def fake_sleep(delay: float) -> None:
        clock.advance(delay)

    # ⚠️ patch 的是全局 asyncio.sleep（wechat_bot 与测试共用同一模块对象），
    # 与 test_wechat_retry_cooldown._patch_clock 同样的处理，仅测试函数期间生效。
    monkeypatch.setattr("agent_core.wechat_bot.asyncio.sleep", fake_sleep)

    bot = _make_throttle_bot(clock)
    bot._last_send_at = clock.now()  # 刚发过一条

    async def run():
        t0 = clock.now()
        await bot._throttle_send()  # fake sleep 推进时钟
        return clock.now() - t0

    elapsed = asyncio.run(run())
    assert elapsed >= 1.5, f"应等待满 1.5s 最小间隔，实际 {elapsed:.2f}s"
    assert bot._last_send_at == clock.now()


def test_throttle_no_wait_when_enough_time_passed(monkeypatch):
    """距上一条 5s：已超过最小间隔，不等待。"""
    clock = _Clock()
    monkeypatch.setattr("agent_core.wechat_bot.time.time", clock.now)
    bot = _make_throttle_bot(clock)
    bot._last_send_at = clock.now() - 5.0

    async def run():
        t0 = clock.now()
        await bot._throttle_send()
        return clock.now() - t0

    elapsed = asyncio.run(run())
    assert elapsed == 0.0
    assert bot._last_send_at == clock.now()


# ── _send_step_msg：step 过程消息的发送纪律 ─────────────────

def _make_step_bot(clock: _Clock) -> WeChatBot:
    bot = object.__new__(WeChatBot)
    bot.user_id = "test"
    bot.step_msg_budget = 30
    bot._step_sent_count = 0
    bot._rate_limited_until = 0.0
    bot._step_pending_queue = deque()  # 新增：暂存队列
    bot._step_pending_draining = False  # 新增：防递归标志
    bot._throttle_send = AsyncMock()
    bot._rate_limit_send = AsyncMock()
    bot.send_message = AsyncMock(return_value={"ret": 0, "message_id": "m-1"})
    return bot


def test_step_msg_skipped_during_cooldown(monkeypatch):
    """频控冷却中：step 消息暂存到 _step_pending_queue，绝不调用 send_message（不踩频控）。"""
    clock = _Clock()
    monkeypatch.setattr("agent_core.wechat_bot.time.time", clock.now)
    bot = _make_step_bot(clock)
    bot._rate_limited_until = clock.now() + 60.0

    asyncio.run(bot._send_step_msg("u1", "tok", "正在执行…"))
    bot.send_message.assert_not_called()
    assert bot._step_sent_count == 0
    assert len(bot._step_pending_queue) == 1
    assert bot._step_pending_queue[0] == "正在执行…"


def test_step_msg_skipped_when_budget_exhausted(monkeypatch):
    """防御预算耗尽：step 消息暂存到 _step_pending_queue，绝不调用 send_message。"""
    clock = _Clock()
    monkeypatch.setattr("agent_core.wechat_bot.time.time", clock.now)
    bot = _make_step_bot(clock)
    bot.step_msg_budget = 3
    bot._step_sent_count = 3

    asyncio.run(bot._send_step_msg("u1", "tok", "正在执行…"))
    bot.send_message.assert_not_called()
    assert len(bot._step_pending_queue) == 1
    assert bot._step_pending_queue[0] == "正在执行…"


def test_step_msg_success_increments_count(monkeypatch):
    """发送成功：预算计数 +1，节流与限速均被调用。"""
    clock = _Clock()
    monkeypatch.setattr("agent_core.wechat_bot.time.time", clock.now)
    bot = _make_step_bot(clock)

    asyncio.run(bot._send_step_msg("u1", "tok", "正在执行…"))
    bot.send_message.assert_awaited_once_with("u1", "tok", "正在执行…", max_retries=0)
    bot._throttle_send.assert_awaited_once()
    bot._rate_limit_send.assert_awaited_once()
    assert bot._step_sent_count == 1


def test_step_msg_failure_no_retry_no_crash(monkeypatch):
    """发送失败：不重试（max_retries=0 由 send_message 侧保证）、不抛异常、计数不变。"""
    clock = _Clock()
    monkeypatch.setattr("agent_core.wechat_bot.time.time", clock.now)
    bot = _make_step_bot(clock)
    bot.send_message = AsyncMock(return_value={"ret": -2, "detail": {"errmsg": "prepare failed"}})

    asyncio.run(bot._send_step_msg("u1", "tok", "正在执行…"))
    bot.send_message.assert_awaited_once()
    assert bot._step_sent_count == 0  # 失败不计入预算（后续 step 仍可发）


def test_step_msg_exception_is_swallowed(monkeypatch):
    """send_message 抛异常：_send_step_msg 吞掉异常（step 是过程消息，失败不阻塞主流程）。"""
    clock = _Clock()
    monkeypatch.setattr("agent_core.wechat_bot.time.time", clock.now)
    bot = _make_step_bot(clock)
    bot.send_message = AsyncMock(side_effect=RuntimeError("boom"))

    asyncio.run(bot._send_step_msg("u1", "tok", "正在执行…"))  # 不应抛
    assert bot._step_sent_count == 0
