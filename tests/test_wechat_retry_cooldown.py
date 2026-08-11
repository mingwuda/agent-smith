"""回归测试：微信 Bot 最终回复补发队列的「等冷却」策略。

背景（2026-08-03 线上事故）：旧 `_retry_loop` 在频控冷却窗口内也强行发送，
每次失败 send_message 又把 `_rate_limited_until` 往后推 60s（prepare failed），
形成"越试越冷却"的恶性循环——5 次补发全失败、频控持续 11 分钟+，最终回复永久丢失。

本次改动后的策略：
1. 冷却中（now < _rate_limited_until）整队列挂起、绝不发送，不刷新冷却窗口；
2. 每次补发只发一次（max_retries=0），失败不立即重试；
3. 失败后 next 至少取冷却结束时间（max(退避, 冷却结束)）。

测试用可控时钟（fake time.time + fake asyncio.sleep 只推进时钟不真实等待），
把 7 轮 × 60s 冷却的完整放弃路径压缩到毫秒级跑完。
"""
import sys
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

_HERE = Path(__file__).resolve().parent.parent
_AGENT_CORE = _HERE / "agent_core"
for _p in (str(_AGENT_CORE), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agent_core.wechat_bot as wb
from agent_core.wechat_bot import WeChatBot

_CLOCK_START = 1_000_000.0


class _FakeClock:
    """可控时钟：_retry_loop 里的 time.time() 与 asyncio.sleep 都走它。"""

    def __init__(self, start: float = _CLOCK_START):
        self.t = start

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _make_bot(queue_items: list, clock: _FakeClock) -> WeChatBot:
    """object.__new__ 绕过 __init__（避免拉起整个 Bot 依赖），只测补发循环本身。"""
    bot = object.__new__(WeChatBot)
    bot.user_id = "test"
    bot._retry_queue = queue_items
    bot._retry_task = None
    bot._rate_limited_until = 0.0
    bot._last_send_at = 0.0
    bot.send_rate_window = 60.0
    bot.send_rate_max = 10
    bot._send_timestamps = []
    # 不真实节流 / 不真实写盘（有单独测试覆盖持久化）
    # _throttle_send 是 async 方法 → AsyncMock；_persist_retry_queue 是同步方法 → 普通 Mock
    bot._throttle_send = AsyncMock()
    bot._persist_retry_queue = Mock()
    return bot


def _patch_clock(monkeypatch, clock: _FakeClock):
    """把 wechat_bot 模块的 time.time 换成可控时钟；asyncio.sleep 只推进时钟不等待。

    ⚠️ monkeypatch 修改的是全局 asyncio.sleep（wechat_bot 与测试共用同一模块对象），
    仅在单个测试函数期间生效，pytest 会自动恢复；测试内只用 fake sleep，安全。
    """
    monkeypatch.setattr(wb.time, "time", clock.now)

    async def fake_sleep(delay: float) -> None:
        clock.advance(delay)

    monkeypatch.setattr(wb.asyncio, "sleep", fake_sleep)


def test_cooldown_waits_before_send_and_gives_up_after_15(monkeypatch):
    """冷却中绝不发送；每次冷却结束后只试 1 次；15 轮全失败后放弃、队列清空。

    轮数=BACKOFF 长度（15，2026-08-03 事故后加长：微信频控窗口 ≤12 分钟且
    失败会刷新窗口，旧 7 轮 300s 封顶仍在窗口内，45 分钟补发全失败）。
    """
    clock = _FakeClock()
    _patch_clock(monkeypatch, clock)

    item = {"to": "u1", "token": "tok", "text": "hello", "attempts": 0, "next": clock.now()}
    bot = _make_bot([item], clock)
    bot._rate_limited_until = clock.now() + 60.0  # 入队时已在冷却中

    send_calls: list[tuple[int, float]] = []  # (max_retries, 发送时的时钟)

    async def fake_send(to, token, text, max_retries=0):
        # 模拟真实 send_message 的频控行为：失败(ret=-2)并把冷却推到 now+60
        send_calls.append((max_retries, clock.now()))
        bot._rate_limited_until = clock.now() + 60.0
        return {"ret": -1, "detail": {"ret": -2, "message_id": "",
                                      "detail": {"ret": -2, "errmsg": "prepare failed"}}}

    bot.send_message = fake_send
    asyncio.run(bot._retry_loop())

    # 15 轮 BACKOFF 全失败后放弃
    assert len(send_calls) == 15, f"应在每次冷却结束后各试 1 次，实际 {len(send_calls)} 次"
    # 每次都是"失败不立即重试"（max_retries=0）
    assert all(mr == 0 for mr, _ in send_calls), send_calls
    # 相邻两次发送间隔 ≥ 60s 冷却窗口（冷却中绝无发送）
    for i in range(len(send_calls) - 1):
        gap = send_calls[i + 1][1] - send_calls[i][1]
        assert gap >= 60.0, f"第 {i}->{i + 1} 次发送间隔 {gap:.1f}s，应 ≥ 冷却 60s"
    # 总窗口覆盖远超旧 405s（7 轮 × 60s 冷却）
    assert send_calls[-1][1] - _CLOCK_START >= 15 * 60
    # 放弃后队列清空
    assert not bot._retry_queue


def test_retry_succeeds_once_cooldown_expires(monkeypatch):
    """冷却结束后重试成功 → 队列清空、不再继续。"""
    clock = _FakeClock()
    _patch_clock(monkeypatch, clock)

    item = {"to": "u1", "token": "tok", "text": "hello", "attempts": 0, "next": clock.now()}
    bot = _make_bot([item], clock)
    bot._rate_limited_until = clock.now() + 60.0  # 先经历一轮冷却

    send_calls: list[tuple[int, float]] = []

    async def fake_send(to, token, text, max_retries=0):
        send_calls.append((max_retries, clock.now()))
        if len(send_calls) == 1:
            bot._rate_limited_until = clock.now() + 60.0  # 第一次仍被频控
            return {"ret": -1, "detail": {"ret": -2, "message_id": "",
                                          "detail": {"ret": -2, "errmsg": "prepare failed"}}}
        return {"ret": 0, "message_id": "m-123"}  # 冷却结束后成功

    bot.send_message = fake_send
    asyncio.run(bot._retry_loop())

    assert len(send_calls) == 2, f"第一次失败 + 冷却后成功 = 2 次，实际 {len(send_calls)}"
    assert send_calls[0][0] == 0 and send_calls[1][0] == 0
    # 第二次发送在第一次的 60s 冷却之后
    assert send_calls[1][1] - send_calls[0][1] >= 60.0
    assert not bot._retry_queue, "发送成功后队列应清空"


def test_recovery_from_disk_respects_persisted_state(monkeypatch, tmp_path):
    """服务重启恢复时：磁盘队列项原样进内存（attempts/next 保留），不丢不重。

    验证 _load_retry_queue 把持久化的补发状态完整还原（含已失败的 attempts 计数），
    重启后不会从 0 重来、也不会跳过未到期的 next。
    """
    from agent_core.wechat_bot import WeChatBot as W
    bot = object.__new__(W)
    bot.user_id = "test"
    bot._retry_queue = []
    bot._retry_task = None
    bot._retry_queue_path = tmp_path / "retry_queue.json"
    bot._retry_queue_path.write_text(
        '[{"to": "u1", "token": "tok", "text": "hi", "attempts": 3, "next": 123.0}]',
        encoding="utf-8",
    )

    bot._load_retry_queue()

    assert len(bot._retry_queue) == 1
    item = bot._retry_queue[0]
    assert item["to"] == "u1" and item["text"] == "hi"
    assert item["attempts"] == 3  # 已失败的次数保留，不重置
    assert item["next"] == 123.0  # 未到期时间保留


def test_send_message_cooldown_escalates_stepwise(monkeypatch):
    """send_message 频控冷却阶梯递增：60s 起步、每失败 +60s、封顶 900s，成功清零。

    回归（2026-08-03）：冷却曾是固定 60s，连续失败时被反复撞穿；也曾讨论过直接
    300s，但频控可能是短时抖动、一上来就 5 分钟过度惩罚。改为连续失败阶梯递增，
    封顶 900s（15 分钟，> 实测微信频控窗口 ≤12 分钟，见 test_cooldown_caps_at_900）。
    """
    from types import SimpleNamespace

    clock = _FakeClock()
    monkeypatch.setattr(wb.time, "time", clock.now)

    bot = object.__new__(WeChatBot)
    bot.bot_base_url = "http://fake"
    bot._auth_headers = lambda: {}
    bot._rate_limited_until = 0.0
    bot._rate_limit_strikes = 0
    # 解析结果按调用顺序注入：前 5 次频控失败，第 6 次成功
    bot._parse_sendmessage_response = Mock(side_effect=[
        {"ret": -2, "message_id": "", "detail": {"errmsg": "prepare failed"}} for _ in range(5)
    ] + [{"ret": 0, "message_id": "m-ok"}])

    fake_client = AsyncMock()
    fake_client.post.return_value = SimpleNamespace(status_code=200, text="{}")
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = False
    # AsyncClient 是同步构造（返回实例）+ async with 进入：外层用同步 Mock，
    # 内层 post 用 AsyncMock（await 走 return_value）。
    monkeypatch.setattr(wb.httpx, "AsyncClient", Mock(return_value=fake_client))

    snapshots: list[tuple[float, int]] = []  # (冷却结束时间, strikes)，每次失败后记录

    async def run():
        for _ in range(5):
            await bot.send_message("u1", "tok", "hi", max_retries=0)
            snapshots.append((bot._rate_limited_until, bot._rate_limit_strikes))
        return await bot.send_message("u1", "tok", "hi", max_retries=0)

    resp = asyncio.run(run())

    # 连续 5 次失败：冷却 60 → 120 → 180 → 240 → 300，strikes 1→5
    expected = [60, 120, 180, 240, 300]
    for i, want in enumerate(expected):
        until, strikes = snapshots[i]
        got = until - clock.now()
        assert abs(got - want) < 1e-6, f"第 {i + 1} 次失败后冷却应为 {want}s，实际 {got:.1f}s"
        assert strikes == i + 1, f"第 {i + 1} 次失败后 strikes 应为 {i + 1}，实际 {strikes}"
    # 第 6 次成功：计数与冷却清零，回到基线
    assert resp["ret"] == 0 and resp["message_id"] == "m-ok"
    assert bot._rate_limit_strikes == 0
    assert bot._rate_limited_until == 0.0


def test_send_message_cooldown_caps_at_900(monkeypatch):
    """连续失败冷却阶梯递增到 900s 后封顶，不再无限拉长。

    2026-08-03 事故根因之一：旧封顶 300s（5 分钟）< 实测微信频控窗口（≤12 分钟，
    且每次失败刷新窗口），补发 45 分钟全失败。900s（15 分钟）> 窗口，保证
    补发间隔能"撞不中窗口"、让频控自然过期。
    """
    from types import SimpleNamespace

    clock = _FakeClock()
    monkeypatch.setattr(wb.time, "time", clock.now)

    bot = object.__new__(WeChatBot)
    bot.bot_base_url = "http://fake"
    bot._auth_headers = lambda: {}
    bot._rate_limited_until = 0.0
    bot._rate_limit_strikes = 0
    bot._parse_sendmessage_response = Mock(side_effect=lambda *a, **k: {
        "ret": -2, "message_id": "", "detail": {"errmsg": "prepare failed"}})

    fake_client = AsyncMock()
    fake_client.post.return_value = SimpleNamespace(status_code=200, text="{}")
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = False
    monkeypatch.setattr(wb.httpx, "AsyncClient", Mock(return_value=fake_client))

    async def run():
        for _ in range(15):
            await bot.send_message("u1", "tok", "hi", max_retries=0)

    asyncio.run(run())

    # 15 次失败：60 → 120 → ... → 840 → 900（第 15 次封顶）
    assert bot._rate_limit_strikes == 15
    expected_caps = [60 * i for i in range(1, 16)]
    got = bot._rate_limited_until - clock.now()
    assert abs(got - 900) < 1e-6, f"第 15 次失败后冷却应为 900s，实际 {got:.1f}s"
    assert expected_caps[-1] == 900
    # 再失败一次仍封顶 900，不无限拉长
    asyncio.run(run())
    got2 = bot._rate_limited_until - clock.now()
    assert abs(got2 - 900) < 1e-6, f"封顶后冷却仍应为 900s，实际 {got2:.1f}s"


def test_final_reply_deferred_during_cooldown(monkeypatch):
    """最终回复在频控冷却中：不发送、直接入补发队列（2026-08-03 事故修复）。

    事故：18:54:46 最终回复在冷却窗口内强发（_rate_limited_until 尚未过期），
    撞上 prepare failed 刷新微信侧窗口，频控被拉长到 45 分钟。
    修复后：冷却中一律不发，交给 _retry_loop 等冷却结束再补发。
    """
    from unittest.mock import AsyncMock as _AM, Mock as _M

    clock = _FakeClock()
    monkeypatch.setattr(wb.time, "time", clock.now)

    bot = object.__new__(WeChatBot)
    bot.user_id = "test"
    bot._rate_limited_until = clock.now() + 120.0  # 仍在冷却中
    bot._schedule_retry = _M()
    bot._throttle_send = _AM()
    bot.send_message = _AM(return_value={"ret": 0, "message_id": "m-x"})

    asyncio.run(bot._send_final_reply("u1", "tok", "你好，这是最终回复"))

    # 冷却中：不调用 send_message，直接入队
    bot.send_message.assert_not_awaited()
    bot._schedule_retry.assert_called_once()
    assert bot._schedule_retry.call_args[0][2] == "你好，这是最终回复"


def test_final_reply_sends_when_not_in_cooldown(monkeypatch):
    """最终回复不在冷却中：正常发送；失败才入补发队列。"""
    from unittest.mock import AsyncMock as _AM, Mock as _M

    clock = _FakeClock()
    monkeypatch.setattr(wb.time, "time", clock.now)

    bot = object.__new__(WeChatBot)
    bot.user_id = "test"
    bot._rate_limited_until = 0.0  # 无冷却
    bot._schedule_retry = _M()
    bot._throttle_send = _AM()
    bot.send_message = _AM(return_value={"ret": 0, "message_id": "m-ok"})

    asyncio.run(bot._send_final_reply("u1", "tok", "正常回复"))

    bot.send_message.assert_awaited_once()
    assert bot.send_message.call_args[0][0:3] == ("u1", "tok", "正常回复")
    bot._schedule_retry.assert_not_called()

    # 发送失败 → 入队
    bot.send_message = _AM(return_value={"ret": -1, "detail": {"ret": -2}})
    asyncio.run(bot._send_final_reply("u1", "tok", "失败回复"))
    bot._schedule_retry.assert_called_once()
