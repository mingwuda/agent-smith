"""验证 tool_call ↔ ToolMessage 配对完整性的守护逻辑。

回归防护：修复「读文件/长会话压缩后每个请求都崩 INVALID_CHAT_HISTORY」。
根因是压缩切片在边界残留悬空 AIMessage(tool_calls) 或孤儿 ToolMessage。
本测试证明 dropper 能清理这两类残缺，且 compact_messages 切片不产生孤儿。
"""
from agent_core.main import app  # noqa: F401  触发 sys.path 注入
from agent_core.agent_helpers import _drop_dangling_tool_call_messages
from agent_core.context_manager import compact_messages, estimate_message_tokens
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


def _ai_tool_call(cid: str, text: str = "") -> AIMessage:
    return AIMessage(content=text, tool_calls=[{"id": cid, "name": "read_file", "args": {}}])


def _has_pairing_violation(messages: list) -> bool:
    """模拟 LLM 端校验：每个 tool_call 必须紧随对应 ToolMessage；无孤儿 ToolMessage。"""
    pending: set[str] = set()
    for m in messages:
        t = getattr(m, "type", "")
        if t == "tool":
            tid = getattr(m, "tool_call_id", "")
            if tid not in pending:
                return True  # 孤儿 tool 消息
            pending.discard(tid)
        else:
            if pending:
                return True  # 上一个 AI 的 tool_calls 未被 ToolMessage 消费就出现别的消息
            if t == "ai":
                for tc in getattr(m, "tool_calls", None) or []:
                    pending.add(tc["id"])
    return bool(pending)  # 结尾仍有未配对的 tool_call


def test_drop_dangling_ai_tool_calls():
    # AIMessage 带 tool_calls 但没有 ToolMessage（取消/超时留下）→ 整条丢弃
    msgs = [HumanMessage(content="hi"), _ai_tool_call("c1", "调用工具")]
    repaired, changed = _drop_dangling_tool_call_messages(msgs)
    assert changed is True
    assert not _has_pairing_violation(repaired)
    assert all(getattr(m, "type", "") != "ai" or not getattr(m, "tool_calls", None) for m in repaired)


def test_drop_orphan_tool_message():
    # 孤儿 ToolMessage：前面没有匹配 tool_call（压缩把父 AIMessage 切走）→ 丢弃
    msgs = [AIMessage(content="历史摘要"), ToolMessage(content="结果", tool_call_id="gone"),
            HumanMessage(content="继续")]
    repaired, changed = _drop_dangling_tool_call_messages(msgs)
    assert changed is True
    assert not _has_pairing_violation(repaired)


def test_keep_valid_tool_block():
    # 完整的 AI(tool_calls)+ToolMessage 块必须原样保留，不能误删
    msgs = [HumanMessage(content="hi"), _ai_tool_call("c1"),
            ToolMessage(content="文件内容", tool_call_id="c1"),
            AIMessage(content="最终回复")]
    repaired, changed = _drop_dangling_tool_call_messages(msgs)
    assert changed is False
    assert len(repaired) == 4
    assert not _has_pairing_violation(repaired)


def test_partial_tool_results_dropped():
    # AIMessage 有两个 tool_call 但只回来一个 ToolMessage → 整块丢弃（不能留半截）
    ai = AIMessage(content="", tool_calls=[
        {"id": "a", "name": "read_file", "args": {}},
        {"id": "b", "name": "read_file", "args": {}},
    ])
    msgs = [HumanMessage(content="hi"), ai, ToolMessage(content="only a", tool_call_id="a")]
    repaired, changed = _drop_dangling_tool_call_messages(msgs)
    assert changed is True
    assert not _has_pairing_violation(repaired)


def test_compact_never_produces_orphan_tool():
    # 构造一个长会话，其切片边界恰好落在 AI(tool_calls)+ToolMessage 之间，
    # 验证压缩后不会以孤儿 ToolMessage 开头，且整体无配对违规。
    msgs = [HumanMessage(content="start")]
    for i in range(40):
        # 每轮：AI 发起工具调用 + tool 结果（内容很大，撑高 token 触发压缩）
        big = "X" * 4000
        msgs.append(AIMessage(content="", tool_calls=[{"id": f"t{i}", "name": "read_file", "args": {}}]))
        msgs.append(ToolMessage(content=big, tool_call_id=f"t{i}"))
        msgs.append(AIMessage(content="ok"))
    compacted = compact_messages(msgs, model="deepseek-chat")
    assert compacted, "压缩结果不应为空"
    assert getattr(compacted[0], "type", "") != "tool", "压缩结果不能以孤儿 tool 消息开头"
    # 经 dropper 兜底后必须完全合法
    final, _ = _drop_dangling_tool_call_messages(compacted)
    assert not _has_pairing_violation(final)


if __name__ == "__main__":
    test_drop_dangling_ai_tool_calls()
    test_drop_orphan_tool_message()
    test_keep_valid_tool_block()
    test_partial_tool_results_dropped()
    test_compact_never_produces_orphan_tool()
    print("ALL PASS")
