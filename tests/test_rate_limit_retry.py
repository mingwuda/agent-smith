"""限流(429)就地重试 + 空闲超时重试的守护测试。

覆盖 RetryableLLM 的关键路径：
  1. 429：固定等待后就地重试本次 LLM 调用，并通过 on_retry 上报 wait 秒数；
     等待期间 llm_waiting() 为真（供外层空闲看门狗豁免，避免被误判卡死）。
  2. 429 超过 max_rate_limit_retries：最终上抛（不无限重试）。
  3. 已经产出 chunk 后再 429：不重试（避免重复已流出的内容），直接上抛。
  4. 空闲超时重试仍按原退避工作（回归保护），且不误报为限流。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent_core"))

import agent_helpers as H  # noqa: E402


class _Chunk:
    def __init__(self, content):
        self.content = content
        self.tool_call_chunks = None
        self.additional_kwargs = {}
        self.reasoning_content = ""

    def __add__(self, other):
        return _Chunk(self.content + getattr(other, "content", ""))


class _ScriptedLLM:
    """按脚本依次执行：元素为异常则抛出，为 None 则产出一个 chunk。"""

    def __init__(self, plan):
        self.plan = plan
        self.calls = 0
        self.finalized = 0  # 每个 astream 生成器被终结（正常结束/异常/被 close）的次数

    async def astream(self, input, config=None, **kwargs):
        self.calls += 1
        try:
            err = self.plan[self.calls - 1] if self.calls - 1 < len(self.plan) else None
            if err is not None:
                raise err
            yield _Chunk("ok")
        finally:
            self.finalized += 1


class _PartialThenRaiseLLM:
    """先产出一个 chunk，再抛 429（模拟已流出内容后失败）。"""

    def __init__(self, err):
        self.err = err
        self.calls = 0

    async def astream(self, input, config=None, **kwargs):
        self.calls += 1
        yield _Chunk("partial")
        raise self.err

    async def aclose(self):
        pass


_SLEPT = []
_WAITING_AT_SLEEP = []


def _patch_sleep():
    orig = asyncio.sleep

    async def fake(seconds):
        _SLEPT.append(seconds)
        _WAITING_AT_SLEEP.append(H.llm_waiting())

    asyncio.sleep = fake
    return orig


def _reset():
    _SLEPT.clear()
    _WAITING_AT_SLEEP.clear()
    # 对齐 stream_run 的每请求隔离：未 set 时 _mark_llm_wait 是 no-op，测试需显式建容器
    H._llm_wait_ctx.set([0.0])


def _run(coro_fn):
    return asyncio.run(coro_fn())


def _collect(llm):
    async def run():
        return [c.content async for c in llm.astream({"messages": []})]

    return run


def test_rate_limit_error_classifier():
    # 真实 429 文案（网关返回体）
    assert H.is_rate_limit_error(
        "Error code: 429 - {'error': {'message': 'Rate limit reached, please try again later.', "
        "'type': 'rate_limited'}}"
    )
    assert H.is_rate_limit_error(Exception("429 Too Many Requests"))
    assert H.is_rate_limit_error(Exception("请求过于频繁，请稍后重试"))
    # 类型名
    rate_limit_error = type("RateLimitError", (Exception,), {})
    assert H.is_rate_limit_error(rate_limit_error("x"))

    # 状态码属性
    class _WithCode(Exception):
        status_code = 429

    assert H.is_rate_limit_error(_WithCode("x"))
    # 非限流不应误判
    assert not H.is_rate_limit_error(Exception("image input is not supported"))
    assert not H.is_rate_limit_error(None)
    assert not H.is_rate_limit_error(ValueError("broken pipe"))


def test_429_retries_then_succeeds():
    _reset()
    orig = _patch_sleep()
    try:
        notes = []
        llm = _ScriptedLLM([
            Exception("Error code: 429 - rate_limited"),
            Exception("429 Too Many Requests"),
            None,
        ])
        r = H.RetryableLLM(
            llm, idle_timeout=1.0, max_idle_retries=0,
            on_retry=lambda a, reason, wait=0.0: notes.append((a, reason, wait)),
            rate_limit_wait=30.0, max_rate_limit_retries=2,
        )
        out = _run(_collect(r))
        assert out == ["ok"]
        assert llm.calls == 3, llm.calls
        assert notes == [(1, "rate_limit", 30.0), (2, "rate_limit", 30.0)], notes
        assert _SLEPT == [30.0, 30.0], _SLEPT
        assert all(_WAITING_AT_SLEEP), "等待期间 llm_waiting() 必须为真（外层看门狗豁免）"
        assert llm.finalized == llm.calls, "每次尝试的生成器都应被终结，不留悬挂"
    finally:
        asyncio.sleep = orig


def test_429_exhausted_raises():
    _reset()
    orig = _patch_sleep()
    try:
        notes = []
        llm = _ScriptedLLM([Exception("429 rate limit")] * 4)
        r = H.RetryableLLM(
            llm, idle_timeout=1.0, max_idle_retries=0,
            on_retry=lambda a, reason, wait=0.0: notes.append((a, reason, wait)),
            rate_limit_wait=5.0, max_rate_limit_retries=2,
        )
        try:
            _run(_collect(r))
            raise AssertionError("应当上抛 429")
        except Exception as e:
            assert "rate limit" in str(e)
        assert llm.calls == 3, "初始 1 次 + 重试 2 次"
        assert len(notes) == 2, notes
    finally:
        asyncio.sleep = orig


def test_429_three_retries_allowed():
    """3 次重试（用户要求）：1 初始 + 3 重试，第 4 次成功。"""
    _reset()
    orig = _patch_sleep()
    try:
        notes = []
        llm = _ScriptedLLM([Exception("429 rate limit")] * 3 + [None])
        r = H.RetryableLLM(
            llm, idle_timeout=1.0, max_idle_retries=0,
            on_retry=lambda a, reason, wait=0.0: notes.append((a, reason, wait)),
            rate_limit_wait=30.0, max_rate_limit_retries=3,
        )
        out = _run(_collect(r))
        assert out == ["ok"]
        assert llm.calls == 4, llm.calls
        assert [n[0] for n in notes] == [1, 2, 3], notes
        assert _SLEPT == [30.0, 30.0, 30.0], _SLEPT
        assert all(_WAITING_AT_SLEEP)
    finally:
        asyncio.sleep = orig


def test_default_rate_limit_retries_is_three():
    """锁定需求：默认限流重试次数为 3（config 与 RetryableLLM 两处）。"""
    import inspect

    sig = inspect.signature(H.RetryableLLM.__init__)
    assert sig.parameters["max_rate_limit_retries"].default == 3

    from config import AgentConfig

    assert AgentConfig().llm_rate_limit_max_retries == 3


def test_429_after_partial_output_is_not_retried():
    _reset()
    orig = _patch_sleep()
    try:
        notes = []
        llm = _PartialThenRaiseLLM(Exception("429 rate limit"))
        r = H.RetryableLLM(
            llm, idle_timeout=1.0, max_idle_retries=0,
            on_retry=lambda a, reason, wait=0.0: notes.append((a, reason, wait)),
            rate_limit_wait=5.0, max_rate_limit_retries=2,
        )
        got = []

        async def run():
            async for c in r.astream({"messages": []}):
                got.append(c.content)

        try:
            _run(run)
            raise AssertionError("已产出内容后不应重试，应上抛")
        except Exception as e:
            assert "429" in str(e)
        assert got == ["partial"]
        assert llm.calls == 1, "不得重发（否则内容会重复）"
        assert notes == [], notes
    finally:
        asyncio.sleep = orig


def test_idle_timeout_retry_still_works():
    _reset()
    orig = _patch_sleep()
    try:
        notes = []
        llm = _ScriptedLLM([asyncio.TimeoutError(), None])
        r = H.RetryableLLM(
            llm, idle_timeout=1.0, max_idle_retries=1,
            on_retry=lambda a, reason, wait=0.0: notes.append((a, reason, wait)),
            rate_limit_wait=30.0, max_rate_limit_retries=2,
        )
        out = _run(_collect(r))
        assert out == ["ok"]
        assert llm.calls == 2
        assert notes == [(1, "idle_timeout", 0.0)], notes
        assert _SLEPT == [1.0], "空闲重试退避 min(2**0, 5) = 1s"
        # 本次修复：空闲重试期间必须标记豁免窗口（llm_waiting()==True），
        # 否则外层 90s 空闲看门狗会在重试序列中途无事件 → 误判卡死 → 强杀整轮断连。
        # 这正是「90s 没收到回复就断开、且没重试」的根因。
        assert all(_WAITING_AT_SLEEP), "空闲重试的退避等待期间必须标记豁免窗口（防外层看门狗误杀）"
    finally:
        asyncio.sleep = orig
def test_idle_retry_exemption_window_covers_sequence():
    """空闲重试标记的豁免窗口必须盖住后续可能的最长重试序列（防外层看门狗中途误杀）。

    用户在 idle_timeout 内多次超时（丢弃所有补发重试仍无首 token）时，RetryableLLM
    会连续重试 max_idle_retries 次。等待的余量窗口须 ≥ 剩余潜在空闲时长，
    否则外层空闲看门狗在「重试期间无任何图事件」的空窗内设 timeout 就把整轮强杀断连。
    """
    _reset()
    # 复现修复后的分支标记：_remaining = (3 - 1 + 1) * 8 + 退避(min(2**0,5)=1) = 3*8 + 1 = 25
    H._mark_llm_wait((3 - 1 + 1) * 8 + 1.0)
    # 外层看门狗在重试序列中最长等待 ~25s，全部在豁免窗口内 → 不会误杀
    assert H.llm_waiting() is True
    # 用尽 3 次重试后仍失败 → RetryableLLM 上抛；外层看门狗窗口仍应保持豁免直到重试完全结束
    H._mark_llm_wait(0.0)  # 模拟重试序列结束后窗口自然失效
    assert H.llm_waiting() is False


def test_default_idle_retries_is_three():
    """锁定需求：默认空闲超时重试次数为 3（config 与 RetryableLLM 两处）。"""
    import inspect

    sig = inspect.signature(H.RetryableLLM.__init__)
    assert sig.parameters["max_idle_retries"].default == 3

    from config import AgentConfig

    assert AgentConfig().llm_idle_max_retries == 3