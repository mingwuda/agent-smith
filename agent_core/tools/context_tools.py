"""上下文压缩工具：模型可在需要时主动压缩当前会话的早期上下文。

对应 dsh-command-compact 的 `/compact` 人类命令语义——压缩被注册为模型
可见的工具：调用后压缩当前线程 checkpoint 的历史 head（在飞工具尾块受保护），
保留最近轮 verbatim、把更早历史归纳为结构化摘要并归档早期用户指令到长期记忆。

压缩报告以工具结果文本返回（前端工具卡片展示「被调用 / 压缩后上下文 / 大小」），
自动压缩路径则通过 `context_compacted` SSE 事件渲染独立卡片，两者不重复。
"""
from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

_agent = None  # 由 main.py 在创建 DesktopAgent 后 bind_agent 注入


def bind_agent(agent) -> None:
    """绑定 DesktopAgent 实例（工具内需要访问其 graph checkpoint）。"""
    global _agent
    _agent = agent


def _get_agent():
    if _agent is None:
        raise RuntimeError("context_tools 未绑定 agent（main.py 未调用 bind_agent）")
    return _agent


@tool
async def compress_context(
    config: RunnableConfig,
    reason: str = "",
) -> str:
    """主动压缩当前会话的早期上下文（历史消息），为后续推理释放上下文窗口。

    适用场景：会话历史已经很长（接近模型上下文上限）、模型开始遗漏早期信息，
    或你希望精简历史让后续回复更聚焦时调用。压缩会：
    - 保留最近几轮消息原样（verbatim）；
    - 把更早的历史归纳为带轮次号的结构化摘要；
    - 把早期用户指令归档进长期记忆（可用 recall_memory 召回全文）；
    - 保证工具调用与其结果永远成对出现，不会切裂工具链。
    返回压缩前后的 token / 消息数统计与压缩后摘要预览。未达压缩阈值时返回
    「无需压缩」，历史保持原样。

    Args:
        reason: 触发压缩的原因说明（可选，随报告记录与展示）。
    """
    agent = _get_agent()
    thread_id = config.get("configurable", {}).get("thread_id", "")
    run_config = {"configurable": {"thread_id": thread_id}}
    # 复用「工具执行前压缩」的完整链路：切出在飞 AI(tool_calls) 尾块只压 head，
    # 保证当前 compress_context 调用本身及其后续 ToolMessage 不会被误删。
    report = await agent._compact_checkpoint_before_tool(
        run_config, trigger="tool", reason=reason,
    )
    if report is None:
        return "当前上下文未达压缩阈值，无需压缩（历史保持原样）。"
    pct = report.get("reduction_pct", 0)
    saved = report.get("saved_tokens", 0)
    summary = (report.get("summary") or "").strip()
    lines = [
        f"🧹 上下文已压缩（原因：{reason or '主动触发'}）",
        f"- 消息：{report.get('before_count')} 条 → {report.get('after_count')} 条"
        f"（合并 {report.get('shadowed_count')} 条）",
        f"- Token：约 {report.get('before_tokens')} → {report.get('after_tokens')}"
        f"（阈值 {report.get('threshold_tokens')}，节省 ~{saved}，降幅 {pct:.0f}%）",
        f"- 保留最近轮 verbatim {report.get('recent_verbatim')} 条"
        f"；中段摘要 {report.get('medium_groups')} 组、早期归档 {report.get('old_groups')} 组",
    ]
    if summary:
        preview = summary if len(summary) <= 1000 else summary[:1000] + "\n…（摘要过长已截断，完整内容见上下文）"
        lines.append(f"- 压缩后的上下文（摘要）：\n{preview}")
    return "\n".join(lines)


TOOLS = [compress_context]
