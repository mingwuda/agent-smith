"""compact_messages_report 结构化报告回归测试。

锁定重构后的契约：
- compact_messages 返回类型不变（list[BaseMessage]），旧调用点不受影响；
- compact_messages_report 返回 (messages, CompactionReport | None)；
- 低于阈值时不压缩，报告为 None；
- 触发压缩时报告携带压缩前后大小（tokens / 条数）、摘要文本、触发来源，
  且 to_dict() 可直接作为 `context_compacted` SSE 事件载荷（前端卡片 + 历史回放）。
"""
from agent_core.main import app  # noqa: F401  触发 sys.path 注入
from agent_core.context_manager import (
    CompactionReport,
    compact_messages,
    compact_messages_report,
)
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage


def _long_history(rounds: int = 10) -> list:
    msgs = [SystemMessage(content="sys")]
    for i in range(rounds):
        msgs.append(HumanMessage(content=f"第{i}轮问题 " + "x" * 300))
        msgs.append(AIMessage(content=f"第{i}轮回答 " + "x" * 300))
    return msgs


def test_compact_messages_backward_compatible():
    """旧入口 compact_messages 仍只返回消息列表。"""
    result = compact_messages(_long_history(), "deepseek-chat", configured_window=3000)
    assert isinstance(result, list)
    assert all(not isinstance(m, CompactionReport) for m in result)


def test_report_none_when_below_threshold():
    """未达阈值：不压缩，返回 (原消息, None)。"""
    short = [SystemMessage(content="sys"), HumanMessage(content="hi"), AIMessage(content="hello")]
    messages, report = compact_messages_report(short, "deepseek-chat", configured_window=3000)
    assert report is None
    assert messages == short


def test_report_fields_and_dict():
    """触发压缩：报告携带压缩前后大小 / 摘要 / 触发来源，to_dict 可作 SSE 载荷。"""
    messages, report = compact_messages_report(
        _long_history(), "deepseek-chat", configured_window=3000, trigger="before_tool",
    )
    assert report is not None
    # 压缩后必须显著小于阈值（防抖动底线 ≤90%）
    assert report.after_tokens <= report.threshold_tokens * 0.9
    # 大小统计：条数与 token 都应下降
    assert report.before_count > report.after_count
    assert report.before_tokens > report.after_tokens
    assert report.shadowed_count == report.before_count - report.after_count
    assert report.saved_tokens == report.before_tokens - report.after_tokens
    assert report.reduction_pct > 0
    # 摘要文本 = 压缩后注入的 AIMessage 内容（压缩后的上下文本身）
    summary_msg = [m for m in messages if getattr(m, "type", "") == "ai" and m.content]
    assert summary_msg, "压缩结果必须包含摘要消息"
    assert report.summary and report.summary in summary_msg[0].content
    # 触发来源透传
    assert report.trigger == "before_tool"
    # to_dict：SSE 事件载荷所需的全部字段（type 由调用方拼接，见 agent_run）
    d = report.to_dict()
    for key in ("compaction_id", "trigger", "summary", "before_tokens", "after_tokens",
                "threshold_tokens", "before_count", "after_count", "shadowed_count",
                "recent_verbatim", "medium_groups", "old_groups", "saved_tokens",
                "reduction_pct", "reason"):
        assert key in d, f"to_dict 缺少字段 {key}"
    assert isinstance(d["reduction_pct"], float)
