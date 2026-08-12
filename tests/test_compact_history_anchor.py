"""压缩「不丢历史」底线保护回归测试。

用户担忧：压缩不能太极端——若压缩后无任何会话历史，用户说"继续"时
agent 会失去指代。风险链（之前 182 tok / 281 tok 极端案例的成因）：
1) 防抖循环把最近轮降到只剩 1 组；
2) 若该组是残缺 AI(tool_calls)（缺 ToolMessage），调用点的
   _drop_dangling_tool_call_messages 会整块丢弃；
3) 若同时摘要为空 → 压缩结果只剩 system → 历史全没。

修复：
- context_manager.compact_messages：摘要为空且最近轮无 human 时，从旧段回捞
  最近一条含 human 的组原样保留（底线保护，纵深防御）；
- agent_run 两个 _compact_* 方法：dropper 后压缩结果无任何对话消息则放弃写回，
  保留原历史（宁可上下文大一点，不可断片）。
本测试锁定压缩结果的「非空不变量」与组合链行为。
"""
from agent_core.main import app  # noqa: F401  触发 sys.path 注入
from agent_core.agent_helpers import _drop_dangling_tool_call_messages
from agent_core.context_manager import compact_messages
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage


def _non_system(msgs):
    return [m for m in msgs if not isinstance(m, SystemMessage)]


def _ai_tool_call(cid: str, text: str = "") -> AIMessage:
    return AIMessage(content=text, tool_calls=[{"id": cid, "name": "read_file", "args": {}}])


def test_compact_regular_keeps_recent_user_and_summary():
    """常规多轮超阈值压缩：结果必须同时保留最近轮 human verbatim 与摘要，组合链后非空。"""
    msgs = [SystemMessage(content="sys")]
    for i in range(10):
        msgs.append(HumanMessage(content=f"第{i}轮问题 " + "x" * 300))
        msgs.append(AIMessage(content=f"第{i}轮回答 " + "x" * 300))
    compacted = compact_messages(msgs, "deepseek-chat", configured_window=3000)
    # 必有 system + 摘要（中段/早期摘要） + 最近轮 human
    assert any(isinstance(m, SystemMessage) for m in compacted)
    assert any(isinstance(m, HumanMessage) for m in compacted), "最近轮 human 必须保留（继续的指代）"
    assert any(getattr(m, "type", "") == "ai" and "摘要" in m.content for m in compacted), "必须有摘要"
    assert any(not isinstance(m, SystemMessage) for m in compacted)
    # 组合链：压缩后过 dropper 也绝不至于清空历史
    repaired, _ = _drop_dangling_tool_call_messages(compacted)
    assert any(not isinstance(m, SystemMessage) for m in repaired), "组合链后历史不得为空"


def test_compact_never_returns_zero_history_on_dangling_tail():
    """极端场景：历史只剩 system + 一条巨大残缺 AI(tool_calls)。

    compact_messages 会保留该残缺块（最近轮），dropper 会清掉它 → 只剩 system。
    这正是 agent_run 兜底拦截的触发条件：证明「无历史」路径存在且已被防线覆盖。
    """
    msgs = [SystemMessage(content="sys"), _ai_tool_call("t1", text="x" * 8000)]
    compacted = compact_messages(msgs, "deepseek-chat", configured_window=5000)
    # 压缩本身保留残缺块（compact 不做配对校验，dropper 才做）
    assert any(getattr(m, "type", "") == "ai" and getattr(m, "tool_calls", None) for m in compacted)
    repaired, changed = _drop_dangling_tool_call_messages(compacted)
    assert changed is True
    # 组合链后只剩 system（无对话历史）→ 触发 agent_run 的「放弃写回」兜底
    assert not _non_system(repaired)
    assert len(repaired) == 1 and isinstance(repaired[0], SystemMessage)


def test_compact_only_tool_history_still_keeps_anchor():
    """历史几乎全是工具消息、最近一条 human 很小：压缩后仍须保留该 human（继续的指代）。"""
    msgs = [SystemMessage(content="sys"), HumanMessage(content="继续执行任务 " + "y" * 50)]
    for i in range(8):
        msgs.append(AIMessage(content="", tool_calls=[{"id": f"t{i}", "name": "run_shell", "args": {"command": "echo hi"}}]))
        msgs.append(HumanMessage(content="x" * 800))  # 大 tool 结果用 human 消息模拟
    compacted = compact_messages(msgs, "deepseek-chat", configured_window=3000)
    assert any(isinstance(m, HumanMessage) for m in compacted), "至少保留一条 human 作为继续的锚点"
    assert any(not isinstance(m, SystemMessage) for m in compacted)
    repaired, _ = _drop_dangling_tool_call_messages(compacted)
    assert any(not isinstance(m, SystemMessage) for m in repaired), "组合链后历史不得为空"


def test_compact_empty_input_returns_empty():
    """空输入返回空（不变量：不抛异常、不产生幻觉消息）。"""
    assert compact_messages([], "deepseek-chat") == []
