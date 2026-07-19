"""Phase 1 自进化：反思分类、结构化记忆与负向注入的单元测试。

只覆盖不依赖网络/真实 LLM 的核心逻辑（ponytail：非平凡逻辑留一个可跑的 check）。
端点级联调需用运行中的 app（TestClient + 真实会话），留作手动/集成验证。
"""
import asyncio

# 必须先导入 main 以触发 agent_core/main.py 顶部的 sys.path 注入，
# 否则 session_store / agent 里的顶层 import 会找不到模块。
from agent_core.main import app  # noqa: F401
from agent_core.agent import DesktopAgent
from agent_core.memory.local_memory import get_memory
from langchain_core.messages import AIMessage


UID = "test_self_evo"


def _make_agent():
    # ponytail: 避免触发重型 __init__（建图/连 LLM），仅分配实例并设置所需属性
    a = DesktopAgent.__new__(DesktopAgent)
    a._user_id = UID
    return a


class _FakeLLM:
    def __init__(self, text):
        self._text = text
        self.request_timeout = None

    async def ainvoke(self, msgs):
        return AIMessage(content=self._text)


def test_classify_reflection_routing():
    a = _make_agent()
    assert a._classify_reflection("zip分析|先解压再分析", outcome="success", feedback=None) == {
        "t": "technique", "v": "zip分析|先解压再分析"}
    assert a._classify_reflection("根因是路径拼错", outcome="error", feedback=None) == {
        "t": "pitfall", "v": "根因是路径拼错"}
    assert a._classify_reflection("不要|重复解压同一文件", outcome="feedback", feedback="x") == {
        "t": "pitfall", "v": "重复解压同一文件"}
    assert a._classify_reflection("偏好|用中文回复", outcome="feedback", feedback="x") == {
        "t": "preference", "v": "用中文回复"}
    assert a._classify_reflection("用户喜欢简洁", outcome="feedback", feedback="x") == {
        "t": "preference", "v": "用户喜欢简洁"}


def test_reflect_failure_returns_pitfall():
    a = _make_agent()
    a._build_review_llm = lambda: _FakeLLM("根因是并发写同一文件")
    a._build_llm = lambda: _FakeLLM("根因是并发写同一文件")
    res = asyncio.run(a.reflect_on_task(
        "做一件事", [{"type": "tool_start", "tool": "run_shell", "args": {}}],
        "执行失败", outcome="error",
    ))
    assert res == {"t": "pitfall", "v": "根因是并发写同一文件"}


def test_reflect_no_tool_calls_returns_none_on_success():
    a = _make_agent()
    res = asyncio.run(a.reflect_on_task("你好", [], "你好", outcome="success"))
    assert res is None


def test_learned_patterns_includes_avoid_and_legacy():
    a = _make_agent()
    mem = get_memory(UID)
    keys = []
    try:
        mem.set("_avoid_abc123", {"t": "pitfall", "v": "重复解压同一文件"})
        mem.set("_learned_def456", {"t": "technique", "v": "zip分析|先解压再分析"})
        mem.set("_learned_old999", "关键词|旧格式一句话")  # 向后兼容旧纯字符串
        keys = ["_avoid_abc123", "_learned_def456", "_learned_old999"]

        out = a._load_learned_patterns()
        assert "不要 重复解压同一文件" in out
        assert "zip分析|先解压再分析" in out
        assert "旧格式一句话" in out
        assert "历史踩坑与用户纠正（务必避免）" in out
        assert "从过往任务中学到的经验" in out
    finally:
        for k in keys:
            try:
                mem.delete(k)
            except Exception:
                pass
