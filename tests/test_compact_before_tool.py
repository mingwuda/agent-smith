"""工具前压缩的「在飞工具尾块保护」回归测试。

需求：工具调用前也触发压缩（长工具链期间达到阈值即压缩，不等下次 LLM 调用）。
风险点：工具刚启动时，checkpoint 里最新一条是 AI(tool_calls)，其 ToolMessage 尚未
写入。若把该块连同历史一起压缩，会被 dropper 判残缺整块丢弃——当前正在执行的
工具调用凭空消失。`_split_inflight_tail` 把消息切成 [可压缩 head, 在飞 tail]，
工具前压缩只作用于 head、tail 原样拼回。本测试锁定该切分行为契约。
"""
from agent_core.main import app  # noqa: F401  触发 sys.path 注入
from agent_core.agent_helpers import _split_inflight_tail
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage


def _ai_tool_call(cid: str, text: str = "") -> AIMessage:
    return AIMessage(content=text, tool_calls=[{"id": cid, "name": "read_file", "args": {}}])


def _tool(cid: str, content: str = "ok") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=cid)


def test_inflight_tool_is_tail():
    """工具刚启动：最后一条是未配对的 AI(tool_calls) → 整条归 tail，head 只有前面历史。"""
    msgs = [SystemMessage(content="sys"), HumanMessage(content="hi"), _ai_tool_call("t1")]
    head, tail = _split_inflight_tail(msgs)
    assert head == [msgs[0], msgs[1]]
    assert tail == [msgs[2]]  # 在飞工具调用原样保留，不得参与压缩


def test_no_tool_calls_tail_empty():
    """无任何工具调用 → tail 为空，全部可压缩。"""
    msgs = [SystemMessage(content="sys"), HumanMessage(content="hi")]
    head, tail = _split_inflight_tail(msgs)
    assert head == msgs
    assert tail == []


def test_completed_tool_block_is_conservatively_protected():
    """已完成工具链（末尾是 ToolMessage）：最后一条 AI(tool_calls) 的完整块整块归 tail。

    保守语义：即使该块已完整配对，也整块保留不压缩（与「最近轮 verbatim」一致），
    压缩只作用于更早的历史——安全优先，损失一点压缩率可接受。
    """
    msgs = [SystemMessage(content="sys"), HumanMessage(content="hi"),
            _ai_tool_call("t1"), _tool("t1")]
    head, tail = _split_inflight_tail(msgs)
    assert head == [msgs[0], msgs[1]]
    assert tail == [msgs[2], msgs[3]]  # AI(tool_calls)+ToolMessage 完整块保留


def test_inflight_with_partial_tool_results():
    """并行工具部分完成：AI(tool_calls) 后跟了部分 ToolMessage（另一工具仍在执行）。

    从最后一条 AI(tool_calls) 起整块归 tail（含已写入的部分结果），不切裂工具链。
    """
    msgs = [SystemMessage(content="sys"), HumanMessage(content="hi"),
            _ai_tool_call("t1", "并行调用"), _tool("t1")]
    head, tail = _split_inflight_tail(msgs)
    assert head == [msgs[0], msgs[1]]
    assert tail == [msgs[2], msgs[3]]


def test_mid_history_tool_call():
    """历史中部有已完成的工具块、末尾无 tool_calls → 尾部普通消息可压缩，但工具块前的历史保持。"""
    msgs = [SystemMessage(content="sys"), HumanMessage(content="q1"),
            _ai_tool_call("t1"), _tool("t1"),
            HumanMessage(content="q2")]
    head, tail = _split_inflight_tail(msgs)
    # 最后一条 AI(tool_calls) 是 t1 块 → 从它起整块归 tail（保守），head 只有更早的
    assert head == [msgs[0], msgs[1]]
    assert tail == [msgs[2], msgs[3], msgs[4]]
