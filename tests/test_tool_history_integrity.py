"""验证 tool_call ↔ ToolMessage 配对完整性的守护逻辑。

回归防护：修复「读文件/长会话压缩后每个请求都崩 INVALID_CHAT_HISTORY」。
根因是压缩切片在边界残留悬空 AIMessage(tool_calls) 或孤儿 ToolMessage。
本测试证明 dropper 能清理这两类残缺，且 compact_messages 切片不产生孤儿。
"""
from agent_core.main import app  # noqa: F401  触发 sys.path 注入
from agent_core.agent_helpers import _drop_dangling_tool_call_messages
from agent_core.context_manager import (
    compact_messages,
    estimate_messages_tokens,
    _count_tokens,
)
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage


def _find_summary(messages):
    for m in messages:
        if getattr(m, "type", "") == "ai" and isinstance(getattr(m, "content", ""), str) and "摘要" in m.content:
            return m.content
    return ""


def _assert_no_pairing_violation(messages):
    assert not _has_pairing_violation(messages)


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


def test_duplicate_tool_call_id_insufficient():
    # 回归：LLM 网关返回畸形 tool_calls（两个元素 id 相同），只有 1 条 ToolMessage。
    # 旧实现用 set 去重判断配对 → issubset 误判"完整" → 不修复 → API 400
    # insufficient tool messages（按 tool_calls 数量校验）。
    ai = AIMessage(content="思考", tool_calls=[
        {"id": "call_x", "name": "search_files", "args": {"pattern": "a"}},
        {"id": "call_x", "name": "search_files", "args": {"pattern": "b"}},
    ])
    msgs = [HumanMessage(content="hi"), ai, ToolMessage(content="r", tool_call_id="call_x")]
    repaired, changed = _drop_dangling_tool_call_messages(msgs)
    assert changed is True, "2 个相同 id 的 tool_call 只有 1 条 ToolMessage，必须判残缺"
    assert not _has_pairing_violation(repaired)


def test_duplicate_tool_call_id_sufficient():
    # 相同 id 的畸形 tool_calls 若给了等量 ToolMessage（2 条同 id），视为完整保留。
    ai = AIMessage(content="思考", tool_calls=[
        {"id": "call_x", "name": "search_files", "args": {"pattern": "a"}},
        {"id": "call_x", "name": "search_files", "args": {"pattern": "b"}},
    ])
    msgs = [HumanMessage(content="hi"), ai,
            ToolMessage(content="r1", tool_call_id="call_x"),
            ToolMessage(content="r2", tool_call_id="call_x")]
    repaired, changed = _drop_dangling_tool_call_messages(msgs)
    assert changed is False
    assert len(repaired) == 4


def test_compact_reduces_tokens():
    # 行为契约：压缩后总 token 必须严格小于压缩前（旧轮被摘要成远短文本）。
    # 同时验证工具链完整、不以孤儿 tool 消息开头。
    msgs = [HumanMessage(content="开始")]
    chinese = "中文" * 600  # ~1200 字 ≈ 1920 token
    for i in range(15):
        msgs.append(HumanMessage(content=f"用户第{i}轮 {chinese}"))
        msgs.append(AIMessage(content=f"助手回复第{i}轮 {chinese}"))
    before = estimate_messages_tokens(msgs)
    compacted = compact_messages(msgs, model="deepseek-chat")
    after = estimate_messages_tokens(compacted)
    assert compacted, "压缩结果不应为空"
    assert getattr(compacted[0], "type", "") != "tool", "压缩结果不能以孤儿 tool 消息开头"
    assert after < before, f"压缩后 {after} 未小于压缩前 {before}，压缩未生效"
    final, _ = _drop_dangling_tool_call_messages(compacted)
    assert not _has_pairing_violation(final)


def test_p0_system_message_preserved():
    # P0：system 消息必须原样保留，且排在结果最前（不被压缩触碰）。
    sys_msg = SystemMessage(content="你是一个严格遵循指令的桌面助手")
    msgs = [sys_msg]
    for i in range(18):
        msgs.append(HumanMessage(content=f"用户{i}" + "中文" * 400))
        msgs.append(AIMessage(content=f"助手{i}" + "中文" * 400))
    compacted = compact_messages(msgs, model="deepseek-chat")
    # system 消息仍在，且数量/内容不变
    sys_in_result = [m for m in compacted if isinstance(m, SystemMessage)]
    assert len(sys_in_result) == 1 and sys_in_result[0].content == sys_msg.content
    assert isinstance(compacted[0], SystemMessage), "system 消息应排在第一位"


def test_p2_old_section_discards_tool_and_assistant():
    # P2：最早段（old）仅保留用户关键指令，丢弃工具结果与助手回复。
    msgs = [HumanMessage(content="start")]
    for i in range(25):
        msgs.append(HumanMessage(content=f"用户第{i}步任务" + "中文" * 400))
        msgs.append(AIMessage(content="", tool_calls=[{"id": f"c{i}", "name": "read_file", "args": {}}]))
        msgs.append(ToolMessage(content=f"结果{i}" + "X" * 2000, tool_call_id=f"c{i}"))
        msgs.append(AIMessage(content=f"助手回复{i}" + "中文" * 400))
    compacted = compact_messages(msgs, model="deepseek-chat")
    summary = _find_summary(compacted)
    assert summary, "应生成历史摘要"
    # 存在「早期摘要」段
    assert "早期摘要" in summary
    old_part = summary.split("早期摘要", 1)[1]
    # old 段不应出现「助手[工具结果]」行（工具结果已丢弃）
    assert "助手[工具结果]" not in old_part, "old 段不应保留工具结果"
    # old 段只应出现 用户(第…轮) 行
    assert "用户(第" in old_part, "old 段应保留用户关键指令"
    # 工具链完整（old 段已被转成文本，recent 段工具组原子完整）
    final, _ = _drop_dangling_tool_call_messages(compacted)
    assert not _has_pairing_violation(final)


def test_p3_summary_has_round_structure():
    # P3：摘要按「角色 + 轮次号」结构化，保留时序。
    msgs = [HumanMessage(content="start")]
    for i in range(12):
        msgs.append(HumanMessage(content=f"用户第{i}轮" + "中文" * 700))
        msgs.append(AIMessage(content=f"助手第{i}轮" + "中文" * 700))
    compacted = compact_messages(msgs, model="deepseek-chat")
    summary = _find_summary(compacted)
    assert summary, "历史应超过阈值并触发压缩，生成摘要"
    assert "用户(第" in summary and "助手(第" in summary, "摘要应带轮次号与角色"


def test_p4_token_counting_cjk_costs_more():
    # P4：精确计数（tiktoken 或回退启发式）下，中文 token 数应明显多于等长的 ASCII。
    # 无论 tiktoken 是否可用，此单调性都应成立。
    ascii_text = "a" * 400
    cjk_text = "中" * 400
    assert _count_tokens(cjk_text, "deepseek-chat") > _count_tokens(ascii_text, "deepseek-chat")
    assert _count_tokens("hello world", "gpt-4o") >= 1


def test_compact_is_idempotent_no_thrash():
    # 防抖动：对压缩结果再压缩，总 token 不应显著增大（不会每轮都重新压缩）。
    msgs = [HumanMessage(content="start")]
    for i in range(25):
        msgs.append(HumanMessage(content=f"用户{i}" + "中文" * 400))
        msgs.append(AIMessage(content="", tool_calls=[{"id": f"c{i}", "name": "read_file", "args": {}}]))
        msgs.append(ToolMessage(content=f"r{i}" + "X" * 2000, tool_call_id=f"c{i}"))
        msgs.append(AIMessage(content=f"助手{i}" + "中文" * 400))
    c1 = compact_messages(msgs, model="deepseek-chat")
    c2 = compact_messages(c1, model="deepseek-chat")
    a1 = estimate_messages_tokens(c1, "deepseek-chat")
    a2 = estimate_messages_tokens(c2, "deepseek-chat")
    assert a2 <= a1 * 1.2, f"再压缩后 token 暴涨 {a1} -> {a2}，存在抖动"
    final, _ = _drop_dangling_tool_call_messages(c2)
    assert not _has_pairing_violation(final)


if __name__ == "__main__":
    test_drop_dangling_ai_tool_calls()
    test_drop_orphan_tool_message()
    test_keep_valid_tool_block()
    test_partial_tool_results_dropped()
    test_compact_never_produces_orphan_tool()
    test_compact_reduces_tokens()
    test_p0_system_message_preserved()
    test_p2_old_section_discards_tool_and_assistant()
    test_p3_summary_has_round_structure()
    test_p4_token_counting_cjk_costs_more()
    test_compact_is_idempotent_no_thrash()
    print("ALL PASS")
