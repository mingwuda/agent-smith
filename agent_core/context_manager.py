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
# ContextPilot integration (optional dependency)
# ---------------------------------------------------------------------------
try:
    from contextpilot.config import ContextPilotConfig
    from contextpilot.pipeline import Pipeline

    _HAS_CONTEXTPILOT = True
except ImportError:
    _HAS_CONTEXTPILOT = False

_contextpilot_pipeline: Pipeline | None = None


def _ensure_contextpilot_pipeline() -> Pipeline | None:
    """Lazy singleton — init once, reuse across calls (sub-1ms on warm path)."""
    global _contextpilot_pipeline
    if _contextpilot_pipeline is not None:
        return _contextpilot_pipeline
    if not _HAS_CONTEXTPILOT:
        return None
    try:
        cfg = ContextPilotConfig()
        cfg.compression.level = "balanced"
        cfg.compression.history_window = 6
        cfg.compression.quality_threshold = 68.0  # slightly forgiving for CJK-mixed content
        _contextpilot_pipeline = Pipeline(cfg)
        logger.info("ContextPilot pipeline initialised (balanced mode, window=%d, q_threshold=%.1f)",
                     cfg.compression.history_window, cfg.compression.quality_threshold)
        return _contextpilot_pipeline
    except Exception as exc:
        logger.warning("ContextPilot init failed — falling through to built-in compaction: %s", exc)
        return None


def _messages_to_dicts(messages: list[BaseMessage]) -> list[dict]:
    """Convert LangChain BaseMessage list → OpenAI-format dict list (ContextPilot input).

    Preserves tool_calls so ContextPilot's strategies (which only touch `content`)
    pass them through intact for recent verbatim turns.
    """
    result: list[dict] = []
    for msg in messages:
        role_map = {"human": "user", "ai": "assistant", "tool": "tool", "system": "system"}
        d: dict = {"role": role_map.get(getattr(msg, "type", ""), "user")}
        content = getattr(msg, "content", "") or ""
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") if isinstance(c, dict) else str(c)
                for c in content
            )
        d["content"] = content
        name = getattr(msg, "name", None)
        if name:
            d["name"] = name
        if getattr(msg, "type", "") == "tool":
            d["tool_call_id"] = getattr(msg, "tool_call_id", "")
        # Preserve tool_calls on assistant messages — ContextPilot leaves them alone
        tc = getattr(msg, "tool_calls", None)
        if tc:
            d["tool_calls"] = [
                {
                    "id": t.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": t.get("name", ""),
                        "arguments": str(t.get("args", {})),
                    },
                }
                for t in tc
            ]
        result.append(d)
    return result


def _dicts_to_messages(dicts: list[dict]) -> list[BaseMessage]:
    """Reverse of _messages_to_dicts — reconstruct BaseMessage list."""
    result: list[BaseMessage] = []
    for d in dicts:
        role = d.get("role", "user")
        content = d.get("content", "")
        if role == "system":
            result.append(SystemMessage(content=content))
        elif role == "user":
            result.append(HumanMessage(content=content))
        elif role == "assistant":
            tc_raw = d.get("tool_calls")
            kwargs: dict = {"content": content}
            if tc_raw:
                kwargs["tool_calls"] = [
                    {"id": t["id"], "name": t["function"]["name"],
                     "args": _safe_parse_args(t["function"].get("arguments", "{}"))}
                    for t in tc_raw
                ]
            result.append(AIMessage(**kwargs))
        elif role == "tool":
            result.append(ToolMessage(content=content, tool_call_id=d.get("tool_call_id", "")))
    return result


def _safe_parse_args(raw: str) -> dict:
    """Parse JSON string to dict; return {} on failure."""
    import json
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _contextpilot_compress(messages: list[BaseMessage]) -> list[BaseMessage] | None:
    """Try ContextPilot compression. Returns compressed msgs or None (skipped/fallback)."""
    pipeline = _ensure_contextpilot_pipeline()
    if pipeline is None:
        return None

    dicts = _messages_to_dicts(messages)
    try:
        compressed_dicts, _, event = pipeline.optimize(dicts)
    except Exception as exc:
        logger.debug("ContextPilot.optimize() raised: %s", exc)
        return None

    if event.fallback_triggered:
        return None  # quality gate didn't pass — keep original

    result = _dicts_to_messages(compressed_dicts)

    # Strip orphan tool messages that arose from history_window boundary:
    # ContextPilot summarises older turns — if the boundary left a ToolMessage
    # without its parent AI(tool_calls), drop it to avoid INVALID_CHAT_HISTORY.
    cleaned: list[BaseMessage] = []
    pending_tc_ids: set[str] = set()
    for m in result:
        t = getattr(m, "type", "")
        if t == "ai":
            tc = getattr(m, "tool_calls", None) or []
            if tc:
                pending_tc_ids.update(obj["id"] for obj in tc)
            cleaned.append(m)
        elif t == "tool":
            tid = getattr(m, "tool_call_id", "")
            if tid in pending_tc_ids:
                pending_tc_ids.discard(tid)
                cleaned.append(m)
            # else: orphan → drop silently
        else:
            cleaned.append(m)

    return cleaned


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

    # Phase 0 — ContextPilot multi-strategy compression (≥5% token reduction, quality-gated)
    # Falls through silently on import error, quality miss, or negligible gain.
    if _HAS_CONTEXTPILOT:
        try:
            cp_result = _contextpilot_compress(messages)
            if cp_result is not None:
                orig_tok = estimate_messages_tokens(messages)
                comp_tok = estimate_messages_tokens(cp_result)
                if comp_tok < orig_tok * 0.95:
                    logger.debug("ContextPilot: %d → %d tok (%.0f%%), using compressed",
                                 orig_tok, comp_tok, (1 - comp_tok / orig_tok) * 100)
                    return cp_result
        except Exception as exc:
            logger.debug("ContextPilot phase skipped: %s", exc)

    threshold = compaction_threshold_tokens(model, configured_window)
    recent_count = min(MAX_RECENT_MESSAGES, max(MIN_RECENT_MESSAGES, len(messages) // 3))
    recent = messages[-recent_count:]
    while len(recent) > MIN_RECENT_MESSAGES and estimate_messages_tokens(recent) > threshold * 0.65:
        recent = recent[1:]

    # 边界对齐：recent 不能以 tool 消息开头——其父 AIMessage(tool_calls) 会落入 older 被
    # 摘要成纯文本，留下的 tool 结果就成了孤儿，触发 INVALID_CHAT_HISTORY。把开头的
    # tool 消息推回 older（一并摘要），保证 recent 从一条非 tool 消息开始。
    split = len(messages) - len(recent)
    while split < len(messages) and getattr(messages[split], "type", "") == "tool":
        split += 1
    recent = messages[split:]

    older = messages[:split]
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
