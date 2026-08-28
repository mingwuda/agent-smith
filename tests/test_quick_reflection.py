"""过程反思（quick_reflection）单元测试。

只覆盖不依赖网络/真实 LLM 的核心逻辑：空历史短路、正常返回、审核异常静默。
"""
import asyncio

from agent_core.main import app  # noqa: F401  (sys.path 注入)
from agent_core.agent import DesktopAgent
from langchain_core.messages import AIMessage


UID = "test_quick_refl"


def _make_agent():
    # ponytail: 避免触发重型 __init__（建图/连 LLM），仅分配实例并设置所需属性
    a = DesktopAgent.__new__(DesktopAgent)
    a._user_id = UID
    return a


class _FakeLLM:
    def __init__(self, text=None, raise_exc=None):
        self._text = text
        self._raise = raise_exc
        self.request_timeout = None

    async def ainvoke(self, msgs):
        if self._raise:
            raise self._raise
        return AIMessage(content=self._text)


def test_no_tool_history_returns_none():
    a = _make_agent()
    assert asyncio.run(a.quick_reflection("做点事", [])) is None


def test_normal_progress_returns_none():
    a = _make_agent()
    a._build_review_llm = lambda: _FakeLLM("正常")
    a._build_llm = lambda: _FakeLLM("正常")
    history = [{"tool": "read_file", "args": {"path": "a.py"}}] * 5
    assert asyncio.run(a.quick_reflection("读文件", history)) is None


def test_off_track_returns_advice():
    a = _make_agent()
    a._build_review_llm = lambda: _FakeLLM("在重复读同一文件，应改用 search_files")
    a._build_llm = lambda: _FakeLLM("不应被调用")
    history = [{"tool": "read_file", "args": {"path": "a.py"}}] * 5
    res = asyncio.run(a.quick_reflection("读文件", history))
    assert res == "在重复读同一文件，应改用 search_files"


def test_review_llm_error_silent_none():
    a = _make_agent()
    a._build_review_llm = lambda: _FakeLLM(raise_exc=RuntimeError("network down"))
    a._build_llm = lambda: _FakeLLM(raise_exc=RuntimeError("also down"))
    history = [{"tool": "read_file", "args": {"path": "a.py"}}] * 5
    assert asyncio.run(a.quick_reflection("读文件", history)) is None
