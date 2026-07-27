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
# 上下文压缩策略（内置，无第三方依赖）
# 采用「分层压缩」：P0 保护 system 消息；P1 按 token 预算取最近轮 verbatim；
# P2 旧段再分 medium（每条精简至约 100 字）/ old（仅保留用户关键指令）；
# P3 摘要按「角色 + 轮次号」结构化呈现；P4 用 tiktoken 做精确 token 计数（回退启发式）。
# 早期接入的第三方库 ContextPilot（contextpilot-ai）已回退，原因见 git 历史 ab7991d。
# ---------------------------------------------------------------------------


DEFAULT_CONTEXT_WINDOW_TOKENS = 64000
COMPACTION_RATIO = 0.4
RECENT_BUDGET_RATIO = 0.5        # P1：最近轮 verbatim 预算 = 阈值的 50%（按 token 计）
MEDIUM_BUDGET_RATIO = 0.3        # P2：中段摘要预算 = 阈值的 30%（超出部分归入 old 段）
RECENT_MAX_MESSAGES = 24         # 最近轮 verbatim 上限（约等于「最近 8-12 轮」）
SUMMARY_MAX_CHARS = 6000         # 摘要文本安全上限（分层后实际远小于此）


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


# ---------------------------------------------------------------------------
# P4: 精确 token 计数（优先 tiktoken，失败回退启发式）
# 注意：tiktoken 首次使用会联网下载 BPE 文件。为避免每次压缩都触发网络（既慢又可能在
# 离线/代理环境下挂起或崩溃），这里只在进程内「惰性加载一次」默认编码器，任何失败都直接
# 禁用 tiktoken、整进程回退启发式估算，之后不再尝试网络。
# ---------------------------------------------------------------------------
try:
    import tiktoken  # type: ignore
    _TIKTOKEN_AVAILABLE = True
except Exception:
    tiktoken = None  # type: ignore
    _TIKTOKEN_AVAILABLE = False


def is_tiktoken_available() -> bool:
    """tiktoken 是否可用（已加载且编码器就绪）。"""
    if not _TIKTOKEN_AVAILABLE:
        return False
    enc = _ensure_default_encoder()
    return enc is not None

_DEFAULT_ENCODER = None
_DEFAULT_ENCODER_TRIED = False
_ENCODERS: dict[str, object] = {}


def _ensure_default_encoder():
    global _DEFAULT_ENCODER, _DEFAULT_ENCODER_TRIED, _TIKTOKEN_AVAILABLE
    if _DEFAULT_ENCODER_TRIED:
        return _DEFAULT_ENCODER
    _DEFAULT_ENCODER_TRIED = True
    try:
        _DEFAULT_ENCODER = tiktoken.get_encoding("cl100k_base")
    except Exception:
        # 离线/代理/无网络：彻底禁用 tiktoken，整进程回退启发式
        _DEFAULT_ENCODER = None
        _TIKTOKEN_AVAILABLE = False
    return _DEFAULT_ENCODER


def _get_encoder(model: str):
    if not _TIKTOKEN_AVAILABLE:
        return None
    key = model or ""
    enc = _ENCODERS.get(key)
    if enc is not None:
        return enc
    _ensure_default_encoder()
    if _DEFAULT_ENCODER is None:
        return None
    enc = _DEFAULT_ENCODER
    if model:
        try:
            enc = tiktoken.encoding_for_model(model)
        except Exception:
            enc = _DEFAULT_ENCODER
    _ENCODERS[key] = enc
    return enc


def estimate_tokens_for_text(text: str) -> int:
    """启发式回退：英文 ~4 字符/token，CJK 约 1.6 token/字。tiktoken 不可用或编码失败时回退。"""
    if not text:
        return 0
    ascii_count = sum(1 for ch in text if ord(ch) < 128)
    non_ascii_count = len(text) - ascii_count
    return max(1, ascii_count // 4 + int(non_ascii_count * 1.6))


def _count_tokens(text: str, model: str = "") -> int:
    if not text:
        return 0
    enc = _get_encoder(model)
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    return estimate_tokens_for_text(text)


def estimate_message_tokens(message: BaseMessage, model: str = "") -> int:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        text = content
    else:
        text = str(content)
    overhead = 16
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        overhead += _count_tokens(str(tool_calls), model)
    return _count_tokens(text, model) + overhead


def estimate_messages_tokens(messages: Iterable[BaseMessage], model: str = "") -> int:
    return sum(estimate_message_tokens(msg, model) for msg in messages)


def should_compact(messages: list[BaseMessage], model: str, configured_window: int = 0) -> bool:
    return estimate_messages_tokens(messages, model) >= compaction_threshold_tokens(model, configured_window)


def _group_messages(messages: list[BaseMessage]):
    """把对话切成「原子组」：AI(tool_calls) 与其跟随的 ToolMessage 必须同组，
    避免压缩切片切裂工具链（INVALID_CHAT_HISTORY）。返回 [(group_list, start_index), ...]"""
    groups: list = []
    cur = None
    for i, m in enumerate(messages):
        t = getattr(m, "type", "")
        if t == "tool":
            if cur is not None:
                cur[0].append(m)
            else:
                groups.append(([m], i))  # 孤立 tool（异常历史），自成一组，下游 dropper 兜底
        else:
            if cur is not None:
                groups.append(cur)
            cur = ([m], i)
    if cur is not None:
        groups.append(cur)
    return groups


def _assign_rounds(dialogue: list[BaseMessage]) -> list[int]:
    """为每个消息标注「用户轮次」序号（每个 HumanMessage 递增），供分层摘要标注轮次。"""
    rounds: list[int] = []
    idx = 0
    for m in dialogue:
        if getattr(m, "type", "") == "human":
            idx += 1
        rounds.append(idx)
    return rounds


def _group_tokens(entry, model: str) -> int:
    return estimate_messages_tokens(entry[0], model)


def _group_round(entry, round_of: list[int]) -> int:
    return round_of[entry[1]]


def _split_medium_old(entries: list, threshold: int, model: str):
    """旧段再分层：medium（预算内，做精简摘要）/ old（超出预算，仅保留用户关键指令）。"""
    medium_budget = int(threshold * MEDIUM_BUDGET_RATIO)
    medium, old = [], []
    used = 0
    for e in entries:
        if medium and used + _group_tokens(e, model) > medium_budget:
            old.append(e)
        else:
            medium.append(e)
            used += _group_tokens(e, model)
    return medium, old


def _summarize_medium(entries: list, round_of: list[int]) -> str:
    lo = _group_round(entries[0], round_of)
    hi = _group_round(entries[-1], round_of)
    lines = [f"【中段摘要 - 第 {lo}~{hi} 轮（每条精简至约 100 字）】"]
    for (g, start) in entries:
        r = round_of[start]
        for m in g:
            role = getattr(m, "type", "message")
            name = getattr(m, "name", "") or ""
            content = getattr(m, "content", "") or ""
            if not isinstance(content, str):
                content = str(content)
            content = _squash(content)
            if not content:
                continue
            if role == "tool":
                prefix = f"助手[工具:{name}]" if name else "助手[工具结果]"
                lines.append(f"  {prefix}: {_clip_middle(content, 100)}")
            elif role == "human":
                lines.append(f"用户(第{r}轮): {_clip_middle(content, 100)}")
            elif role == "ai":
                lines.append(f"助手(第{r}轮): {_clip_middle(content, 100)}")
            else:
                lines.append(f"{role}(第{r}轮): {_clip_middle(content, 100)}")
    return "\n".join(lines)


def _summarize_old(entries: list, round_of: list[int]) -> str:
    # P2: 仅保留用户关键指令，丢弃工具结果与助手回复
    seen = set()
    users = []
    for (g, start) in entries:
        r = round_of[start]
        for m in g:
            if getattr(m, "type", "") != "human":
                continue
            content = getattr(m, "content", "") or ""
            if not isinstance(content, str):
                content = str(content)
            content = _squash(content)
            if content and content not in seen:
                seen.add(content)
                users.append(f"用户(第{r}轮): {_clip_middle(content, 120)}")
    if not users:
        return ""
    lo = _group_round(entries[0], round_of)
    hi = _group_round(entries[-1], round_of)
    return f"【早期摘要 - 仅关键指令（已丢弃工具结果/助手回复）- 第 {lo}~{hi} 轮】\n" + "\n".join(users)


def _build_summary(medium_entries: list, old_entries: list, round_of: list[int], model: str) -> str:
    parts = []
    if medium_entries:
        parts.append(_summarize_medium(medium_entries, round_of))
    if old_entries:
        old_txt = _summarize_old(old_entries, round_of)
        if old_txt:
            parts.append(old_txt)
    text = "\n\n".join(parts)
    if len(text) > SUMMARY_MAX_CHARS:
        text = text[:SUMMARY_MAX_CHARS] + "\n...（历史摘要过长，已截断；完整历史在 SQLite 中）"
    return text


def compact_messages(messages: list[BaseMessage], model: str, configured_window: int = 0) -> list[BaseMessage]:
    """分层上下文压缩：
    - P0: system 消息永远保留，不参与压缩。
    - P1: 最近轮 verbatim，按 token 预算（阈值×50%）从最新向前累加（以工具组为原子单位）。
    - P2: 旧段再分 medium（每条精简至约 100 字摘要）/ old（仅保留用户关键指令）。
    - P3: 摘要按「用户/助手 + 轮次号」结构化呈现，保留时序。
    - 防抖动：若压缩后总 token 仍接近阈值，把最近轮里最老的整组降级为 old 段（仅留用户指令），
      保证总 token 单调下降、不会下一轮立刻再压；整组移动，工具链始终完整。
    """
    if not messages:
        return []
    system_msgs = [m for m in messages if isinstance(m, SystemMessage)]
    dialogue = [m for m in messages if not isinstance(m, SystemMessage)]
    threshold = compaction_threshold_tokens(model, configured_window)
    before_tok = estimate_messages_tokens(messages, model)
    if before_tok < threshold:
        return messages

    groups = _group_messages(dialogue)
    round_of = _assign_rounds(dialogue)

    # P1: 最近轮 verbatim（按 token 预算 + 消息数上限），以工具组为原子单位避免切裂
    recent_budget = int(threshold * RECENT_BUDGET_RATIO)
    recent_groups: list = []
    used = 0
    for g in reversed(groups):
        gt = _group_tokens(g, model)
        if recent_groups and used + gt > recent_budget:
            break
        recent_groups.insert(0, g)
        used += gt
        if sum(len(x[0]) for x in recent_groups) >= RECENT_MAX_MESSAGES:
            break
    older_groups = groups[: len(groups) - len(recent_groups)]

    # 防抖动：逐级把最老的最近轮降级为 old 段，直到总 token 明显低于阈值
    demoted: list = []
    guard = 0
    while True:
        recent = [m for g in recent_groups for m in g[0]]
        all_older = older_groups + demoted
        medium_entries, old_entries = _split_medium_old(all_older, threshold, model)
        summary_text = _build_summary(medium_entries, old_entries, round_of, model)
        result = [*system_msgs, *([AIMessage(content=summary_text)] if summary_text else []), *recent]
        if (estimate_messages_tokens(result, model) <= threshold * 0.9
                or len(recent_groups) <= 1 or guard >= len(recent_groups)):
            after_tok = estimate_messages_tokens(result, model)
            recent_cnt = sum(len(g[0]) for g in recent_groups)
            logger.info(
                "[Context] 压缩完成: %d tok → %d tok (阈值 %d, 降幅 %.0f%%) | 最近轮 verbatim %d 条, 中段摘要 %d 组, 早期摘要 %d 组",
                before_tok, after_tok, threshold,
                (1 - after_tok / before_tok) * 100,
                recent_cnt, len(medium_entries), len(old_entries),
            )
            return result
        demoted.append(recent_groups.pop(0))
        guard += 1


def checkpoint_replacement(messages: list[BaseMessage]) -> list[BaseMessage]:
    return [RemoveMessage(id=REMOVE_ALL_MESSAGES), *messages]


def _squash(text: str) -> str:
    return " ".join(text.replace("\r", "\n").split())


def _clip_middle(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head - 20
    return text[:head] + " ...（省略）... " + text[-tail:]
