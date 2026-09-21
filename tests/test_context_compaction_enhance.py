"""上下文压缩增强的守护测试：

P1 per-model 压缩策略路由 + P0-2 上下文溢出(context-overflow)自动压缩重试。

覆盖：
  1. is_context_overflow 分类器：常见中英文 overflow 文案命中，非 overflow 不误判。
  2. RetryableLLM 溢出重试：注入压缩 handler 返回 True → 压缩后重试并成功；
     handler 返回 False / 未注入 handler → 直接上抛（不无限重试）。
  3. resolve_compaction_policy：per-model 策略命中与默认回退。
  4. compaction threshold / retain 随策略变化。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent_core"))

import agent_helpers as H  # noqa: E402
import context_manager as CM  # noqa: E402


class _Chunk:
    def __init__(self, content):
        self.content = content
        self.tool_call_chunks = None
        self.additional_kwargs = {}
        self.reasoning_content = ""

    def __add__(self, other):
        return _Chunk(self.content + getattr(other, "content", ""))


class _ScriptedLLM:
    """按脚本依次执行：异常则抛出，None 则产出一个 chunk。记录调用次数。"""

    def __init__(self, plan):
        self.plan = plan
        self.calls = 0
        self.finalized = 0

    async def astream(self, input, config=None, **kwargs):
        self.calls += 1
        try:
            err = self.plan[self.calls - 1] if self.calls - 1 < len(self.plan) else None
            if err is not None:
                raise err
            yield _Chunk("ok")
        finally:
            self.finalized += 1

    async def aclose(self):
        pass


def _patch_sleep():
    """把 asyncio.sleep 换成 no-op，避免 overflow 分支（本类无需等待）以外拖延测试。"""
    orig = asyncio.sleep

    async def fake(seconds):
        return

    asyncio.sleep = fake
    return orig


def _run(coro_fn):
    return asyncio.run(coro_fn())


def _clear():
    H._overflow_compact_ctx.set(None)


def _reset_ctx():
    # 对齐每请求隔离：默认 handler 为 None
    _clear()


# ---------------------------------------------------------------------------
# 1. is_context_overflow 分类器
# ---------------------------------------------------------------------------
def test_is_context_overflow_hits_common_phrases():
    assert H.is_context_overflow("This model's maximum context length is 128000 tokens")
    assert H.is_context_overflow("Error code: 400 - maximum context length exceeded")
    assert H.is_context_overflow("context_length_exceeded")
    assert H.is_context_overflow("input is too long for this model")
    assert H.is_context_overflow("The input token count exceeds the token limit")
    assert H.is_context_overflow("输入内容超过了模型的上下文长度")
    assert H.is_context_overflow("上下文超长，请精简后再试")


def test_is_context_overflow_negative():
    assert not H.is_context_overflow(None)
    assert not H.is_context_overflow("")
    assert not H.is_context_overflow("rate limit exceeded")  # 限流不误判
    assert not H.is_context_overflow("image input is not supported")
    assert not H.is_context_overflow(ValueError("broken pipe"))


def test_is_context_overflow_accepts_exception_object():
    assert H.is_context_overflow(Exception("maximum context length exceeded"))


# ---------------------------------------------------------------------------
# 2. RetryableLLM 上下文溢出重试
# ---------------------------------------------------------------------------
def test_overflow_retry_success_after_compact():
    _reset_ctx()
    orig = _patch_sleep()
    try:
        notes = []
        # 第一次抛 overflow，第二次成功
        llm = _ScriptedLLM([Exception("context length exceeded"), None])
        compacts = {"n": 0}

        async def handler():
            compacts["n"] += 1
            return True  # 压缩成功 → 允许重试

        H.set_overflow_compact_handler(handler)
        r = H.RetryableLLM(
            llm, idle_timeout=1.0, max_idle_retries=0,
            on_retry=lambda a, reason, wait=0.0: notes.append((a, reason)),
            max_overflow_retries=2,
        )

        async def run():
            return [c.content async for c in r.astream({"messages": []})]

        out = _run(run)
        assert out == ["ok"]
        assert llm.calls == 2, llm.calls
        assert compacts["n"] == 1
        assert notes == [(1, "context_overflow")], notes
    finally:
        asyncio.sleep = orig
        _clear()


def test_overflow_handler_false_raises():
    _reset_ctx()
    orig = _patch_sleep()
    try:
        llm = _ScriptedLLM([Exception("context window exceeded")])

        async def handler():
            return False  # 压缩未生效 → 不应重试

        H.set_overflow_compact_handler(handler)
        r = H.RetryableLLM(llm, idle_timeout=1.0, max_idle_retries=0, max_overflow_retries=2)

        async def run():
            async for _c in r.astream({"messages": []}):
                pass

        try:
            _run(run)
            raise AssertionError("压缩未生效时应当上抛，不得重试")
        except Exception as e:
            assert "context window" in str(e)
        assert llm.calls == 1, "handler 为 False 时不应重试"
    finally:
        asyncio.sleep = orig
        _clear()


def test_overflow_without_handler_raises():
    """未注入压缩 handler 时，overflow 直接上抛（不静默重试）。"""
    _reset_ctx()
    orig = _patch_sleep()
    try:
        llm = _ScriptedLLM([Exception("context length exceeded")])
        r = H.RetryableLLM(llm, idle_timeout=1.0, max_idle_retries=0, max_overflow_retries=2)

        async def run():
            async for _c in r.astream({"messages": []}):
                pass

        try:
            _run(run)
            raise AssertionError("未注入 handler 应上抛")
        except Exception as e:
            assert "context length" in str(e)
        assert llm.calls == 1
    finally:
        asyncio.sleep = orig
        _clear()


# ---------------------------------------------------------------------------
# 3. per-model 压缩策略路由
# ---------------------------------------------------------------------------
def test_resolve_compaction_policy_per_model():
    # 超大窗口模型命中专属策略
    assert CM.resolve_compaction_policy("qwen-long").threshold_ratio == 0.85
    assert CM.resolve_compaction_policy("qwen-long-latest").threshold_ratio == 0.85
    assert CM.resolve_compaction_policy("gpt-4.1").threshold_ratio == 0.85
    # 未命中 → 默认 None（回落 COMPACTION_RATIO=0.4）
    assert CM.resolve_compaction_policy("gpt-4o").threshold_ratio is None
    assert CM.resolve_compaction_policy("my-custom-model").threshold_ratio is None


def test_compaction_threshold_uses_policy():
    # qwen-long 窗口 1000000，策略 0.85 → 850000
    assert CM.compaction_threshold_tokens("qwen-long") == 850000
    # gpt-4o 128000，默认 0.4 → 51200
    assert CM.compaction_threshold_tokens("gpt-4o") == 51200
    # configured 窗口优先于策略窗口，但比例用策略
    assert CM.compaction_threshold_tokens("qwen-long", 1000) == 850


def test_compaction_retain_follows_policy_default():
    # 未配 retain 时用默认 RECENT_BUDGET_RATIO=0.5 × threshold
    assert CM._retain_tokens_for("gpt-4o", 1000) == 500