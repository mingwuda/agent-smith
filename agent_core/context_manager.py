"""Context budgeting and compaction helpers for long-running agent sessions."""
from __future__ import annotations

from typing import Iterable

from langchain_core.messages import AIMessage, BaseMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES


DEFAULT_CONTEXT_WINDOW_TOKENS = 64000
COMPACTION_RATIO = 0.8
MIN_RECENT_MESSAGES = 12
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
    # Conservative mixed-language approximation. Chinese/log-heavy text often tokenizes denser than English.
    return max(1, ascii_count // 4 + non_ascii_count)


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
    recent_count = min(MAX_RECENT_MESSAGES, max(MIN_RECENT_MESSAGES, len(messages) // 3))
    recent = messages[-recent_count:]
    while len(recent) > MIN_RECENT_MESSAGES and estimate_messages_tokens(recent) > threshold * 0.65:
        recent = recent[1:]

    older = messages[: len(messages) - len(recent)]
    if not older:
        return recent

    summary = _summarize_messages(older)
    return [AIMessage(content=summary), *recent]


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
