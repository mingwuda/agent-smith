"""Context budgeting and compaction helpers for long-running agent sessions."""
from __future__ import annotations

import logging
from typing import Iterable

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 上下文压缩策略
# 当前采用内置的「最近轮次保留 + 旧轮摘要」方案（见 compact_messages 的 Phase 1 与
# _summarize_messages）。早期接入的第三方库 ContextPilot（contextpilot-ai，关键词抽取式
# 摘要）已回退：其对中文+代码混合历史的摘要质量不可靠，且会引入未测试代码路径与事件
# 循环阻塞风险。若未来要做压缩 A/B 实验，可在此处重新接入，但须补齐依赖锁定、异步包装
# 与工具链完整性校验。
# ---------------------------------------------------------------------------


DEFAULT_CONTEXT_WINDOW_TOKENS = 64000
COMPACTION_RATIO = 0.4
MIN_RECENT_MESSAGES = 8
MAX_RECENT_MESSAGES = 36
SUMMARY_MAX_CHARS = 6000


MODEL_CONTEXT_WINDOWS = {
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
    "gpt-4.1": 1000000,
    "gpt-4.1-mini": 1000000,
    "deepseek-chat": 64000,
    "deepseek-reasoner": 64000,
    "qwen-long": 1000000,
    "qwen-plus": 128000,
    "qwen-max": 128000,
    "qwen-turbo": 128000,
    "mimo-v2.5-pro": 1000000,
    "mimo": 1000000,
    "claude-sonnet-4-20250514": 200000,
    "claude-opus-4-20250514": 200000,
    "claude-haiku-4-20250414": 200000,
    "claude-3-5-sonnet-20241022": 200000,
}


def context_window_tokens(model: str, configured: int = 0) -> int:
    if configured and configured > 0:
        return int(configured)
    model_name = (model or "").lower()
    for key, value in MODEL_CONTEXT_WINDOWS.items():
        if key in model_name:
            return value
    return DEFAULT_CONTEXT_WINDOW_TOKENS


def compaction_threshold_tokens(model: str, configured: int = 0) -> int:
    return int(context_window_tokens(model, configured) * COMPACTION_RATIO)


def estimate_tokens_for_text(text: str) -> int:
    if not text:
        return 0
    ascii_count = sum(1 for ch in text if ord(ch) < 128)
    non_ascii_count = len(text) - ascii_count
    # 英文 ~4 字符/token；中文/CJK 在主流模型（DeepSeek/Qwen/GLM/GPT 系）下约 1.5~1.8 token/字，
    # 这里取保守的 1.6。旧实现把每个非 ASCII 字符算 1 token，会严重低估中文上下文，导致长中文
    # 会话在真正逼近窗口上限前都不触发压缩。统一改用同一估算后，should_compact 与日志更贴近真实。
    return max(1, ascii_count // 4 + int(non_ascii_count * 1.6))


def estimate_message_tokens(message: BaseMessage) -> int:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        text = content
    else:
        text = str(content)
    overhead = 16
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        overhead += estimate_tokens_for_text(str(tool_calls))
    return estimate_tokens_for_text(text) + overhead


def estimate_messages_tokens(messages: Iterable[BaseMessage]) -> int:
    return sum(estimate_message_tokens(msg) for msg in messages)


def should_compact(messages: list[BaseMessage], model: str, configured_window: int = 0) -> bool:
    return estimate_messages_tokens(messages) >= compaction_threshold_tokens(model, configured_window)


def compact_messages(messages: list[BaseMessage], model: str, configured_window: int = 0) -> list[BaseMessage]:
    """Return a compacted message list while keeping recent interaction detail."""
    if not messages:
        return []

    threshold = compaction_threshold_tokens(model, configured_window)
    # 初始 recent 窗口：消息数的 1/3，封顶 36、保底 8。
    # 从后往前逐步收紧 recent（每次砍最老两轮，并重做 tool 边界对齐），目标是让
    # 「recent(verbatim) + 旧轮摘要」总 token 明显低于阈值（< 85%，留余量防抖动），
    # 或 recent 已收紧到最小保底（避免全部变成摘要、丢失近期细节）。极长历史单轮压不动时
    # 返回尽力结果，交由后续轮次逐步消化，而非无限循环。
    recent_count = min(MAX_RECENT_MESSAGES, max(MIN_RECENT_MESSAGES, len(messages) // 3))
    start = max(0, len(messages) - recent_count)
    # 边界对齐：recent 不以 tool 消息开头——其父 AIMessage(tool_calls) 会落入 older 被摘要，
    # 留下的 tool 结果就成了孤儿，触发 INVALID_CHAT_HISTORY。
    while start < len(messages) and getattr(messages[start], "type", "") == "tool":
        start += 1
    min_recent = min(MIN_RECENT_MESSAGES, len(messages))
    while True:
        recent = messages[start:]
        older = messages[:start]
        if not older:
            return recent
        summary = _summarize_messages(older)
        compressed = [AIMessage(content=summary), *recent]
        if estimate_messages_tokens(compressed) <= threshold * 0.85:
            return compressed
        # 仍超阈值：继续砍最老两轮并重对齐（近期细节的丢失，以「压缩幅度」换「不抖动」）。
        if len(recent) <= max(4, min_recent):
            return compressed  # 触底，返回尽力结果
        start = min(len(messages) - 1, start + 2)
        while start < len(messages) and getattr(messages[start], "type", "") == "tool":
            start += 1


def checkpoint_replacement(messages: list[BaseMessage]) -> list[BaseMessage]:
    return [RemoveMessage(id=REMOVE_ALL_MESSAGES), *messages]


def _summarize_messages(messages: list[BaseMessage]) -> str:
    lines = [
        "【历史上下文摘要】",
        "以下内容由系统为控制上下文长度自动压缩。完整历史仍保存在 SQLite 会话记录中，必要时可按会话历史重新查看。",
    ]
    facts = []
    for msg in messages:
        role = getattr(msg, "type", "message")
        name = getattr(msg, "name", "") or ""
        content = getattr(msg, "content", "") or ""
        if not isinstance(content, str):
            content = str(content)
        content = _squash(content)
        if not content:
            continue
        if role == "tool":
            prefix = f"tool:{name}" if name else "tool"
            facts.append(f"- {prefix}: {_clip_middle(content, 360)}")
        elif role == "human":
            facts.append(f"- 用户: {_clip_middle(content, 420)}")
        elif role == "ai":
            facts.append(f"- 助手: {_clip_middle(content, 420)}")
        else:
            facts.append(f"- {role}: {_clip_middle(content, 360)}")

    text = "\n".join(lines + facts)
    if len(text) > SUMMARY_MAX_CHARS:
        text = text[:SUMMARY_MAX_CHARS] + "\n...（历史摘要过长，已截断；完整历史在 SQLite 中）"
    return text


def _squash(text: str) -> str:
    return " ".join(text.replace("\r", "\n").split())


def _clip_middle(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head - 20
    return text[:head] + " ...（省略）... " + text[-tail:]
