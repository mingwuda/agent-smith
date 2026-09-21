"""Context budgeting and compaction helpers for long-running agent sessions."""
from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import Iterable, Optional

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


@dataclass
class CompactionPolicy:
    """单个模型的压缩策略（参考 dsh-compaction 的 modelPolicies 语义）。

    - threshold_ratio: 上下文窗口的多大比例触发压缩（默认 COMPACTION_RATIO）
    - retain_ratio:     压缩后保留最近轮 verbatim 占窗口比例
    - retain_tokens:    若给定，则用绝对 token 数替代 retain_ratio（互斥）
    - summarization_model / summarization_provider: 可选的专属摘要模型（空则复用主模型）
    - max_retries:      单轮压缩后仍超阈值的重试次数上限
    """
    threshold_ratio: Optional[float] = None
    retain_ratio: Optional[float] = None
    retain_tokens: Optional[int] = None
    summarization_model: str = ""
    summarization_provider: str = ""
    max_retries: int = 0


# 按模型名（子串匹配）覆盖的压缩策略。未命中的模型用全局默认（COMPACTION_RATIO 等）。
# key 支持局部匹配（如 "qwen-long" 匹配 "qwen-long-latest"），与 MODEL_CONTEXT_WINDOWS 一致。
MODEL_COMPACTION_POLICIES: dict[str, CompactionPolicy] = {
    # 超大窗口模型：触发阈值可以更高（充分利用长上下文，减少无谓压缩）
    "qwen-long": CompactionPolicy(threshold_ratio=0.85),
    "mimo-v2.5-pro": CompactionPolicy(threshold_ratio=0.85),
    "gpt-4.1": CompactionPolicy(threshold_ratio=0.85),
    # 常规窗口模型：保持默认 0.4，但显式声明供配置参考
    "deepseek-chat": CompactionPolicy(),
    "qwen-plus": CompactionPolicy(),
    "claude": CompactionPolicy(),
}


def resolve_compaction_policy(model: str) -> CompactionPolicy:
    """按模型名解析压缩策略；未命中则返回全默认策略（基于全局 COMPACTION_RATIO）。"""
    model_name = (model or "").lower()
    for key, policy in MODEL_COMPACTION_POLICIES.items():
        if key in model_name:
            return policy
    return CompactionPolicy()


@dataclass
class CompactionReport:
    """一次上下文压缩的结构化结果（对齐 dsh-compaction 的 CompactionResult 语义）。

    供调用方在 UI 上展示「压缩被调用 / 压缩后的上下文 / 压缩后大小」。
    """

    compaction_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    trigger: str = "auto"               # auto | before_tool | tool | 其它调用点自定义
    summary: str = ""                   # 压缩后注入的摘要文本（压缩后的上下文本身）
    before_tokens: int = 0
    after_tokens: int = 0
    threshold_tokens: int = 0
    before_count: int = 0               # 压缩前消息条数（含 system）
    after_count: int = 0                # 压缩后消息条数（含 system）
    shadowed_count: int = 0             # 被合并进摘要的消息条数
    recent_verbatim: int = 0            # P1 最近轮 verbatim 保留条数
    medium_groups: int = 0              # P2 中段摘要组数
    old_groups: int = 0                 # P2 早期归档组数
    reason: str = ""                    # 可选触发原因说明（手动工具传入）

    @property
    def saved_tokens(self) -> int:
        return max(0, self.before_tokens - self.after_tokens)

    @property
    def reduction_pct(self) -> float:
        if self.before_tokens <= 0:
            return 0.0
        return (1 - self.after_tokens / self.before_tokens) * 100

    def to_dict(self) -> dict:
        """序列化为 SSE 事件载荷（前端卡片 + 历史回放共用）。"""
        return {
            "compaction_id": self.compaction_id,
            "trigger": self.trigger,
            "summary": self.summary,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "threshold_tokens": self.threshold_tokens,
            "before_count": self.before_count,
            "after_count": self.after_count,
            "shadowed_count": self.shadowed_count,
            "recent_verbatim": self.recent_verbatim,
            "medium_groups": self.medium_groups,
            "old_groups": self.old_groups,
            "saved_tokens": self.saved_tokens,
            "reduction_pct": round(self.reduction_pct, 1),
            "reason": self.reason,
        }


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
    window = context_window_tokens(model, configured)
    return int(window * _threshold_ratio_for(model))


def _threshold_ratio_for(model: str) -> float:
    """返回模型的触发阈值比例（per-model 策略优先，否则全局 COMPACTION_RATIO）。"""
    return resolve_compaction_policy(model).threshold_ratio or COMPACTION_RATIO


def _retain_tokens_for(model: str, threshold: int) -> int:
    """返回模型的最近轮 verbatim 保留预算（token 数）。

    策略显式指定 retain_tokens 则用之；否则按 retain_ratio（或默认 RECENT_BUDGET_RATIO）
    乘以 threshold 计算。与 compress_messages 的 P1 预算口径保持一致。
    """
    policy = resolve_compaction_policy(model)
    if policy.retain_tokens is not None:
        return max(0, policy.retain_tokens)
    ratio = policy.retain_ratio or RECENT_BUDGET_RATIO
    return int(threshold * ratio)


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


def _archive_user_instruction(memory, content: str) -> str:
    """把用户指令归档进长期记忆（SQLite FTS 可检索），返回摘要行文本。

    - key 由内容 sha1 决定 → 同一指令只归档一次，反复压缩不产生重复记忆；
    - 已归档 → 只写 `[见记忆: key]` 引用 + 60 字预览（帮助模型判断是否值得
      recall_memory 召回全文），不重复写入；
    - memory 为 None（纯函数/测试模式）→ 保持原裁剪行为，不写记忆。

    ponytail: 记忆写入失败绝不阻断压缩——任何异常回退为裁剪预览。
    """
    preview = _clip_middle(content, 60)
    if memory is None:
        return preview
    try:
        key = "_ctx_old_" + hashlib.sha1(content.encode("utf-8")).hexdigest()[:16]
        if memory.get(key) is None:
            memory.set(key, content)
        return f"[见记忆: {key}] {preview}"
    except Exception:
        return preview


def _summarize_old(entries: list, round_of: list[int], memory=None) -> str:
    # P2: 仅保留用户关键指令，丢弃工具结果与助手回复；
    #     提供 memory 时每条指令归档进长期记忆，摘要行改为 [见记忆: key] 引用。
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
                users.append(f"用户(第{r}轮): {_archive_user_instruction(memory, content)}")
    if not users:
        return ""
    lo = _group_round(entries[0], round_of)
    hi = _group_round(entries[-1], round_of)
    return f"【早期摘要 - 已归档用户指令（详见长期记忆，可用 recall_memory 召回）- 第 {lo}~{hi} 轮】\n" + "\n".join(users)


def _build_summary(medium_entries: list, old_entries: list, round_of: list[int], model: str, memory=None) -> str:
    parts = []
    if medium_entries:
        parts.append(_summarize_medium(medium_entries, round_of))
    if old_entries:
        old_txt = _summarize_old(old_entries, round_of, memory)
        if old_txt:
            parts.append(old_txt)
    text = "\n\n".join(parts)
    if len(text) > SUMMARY_MAX_CHARS:
        text = text[:SUMMARY_MAX_CHARS] + "\n...（历史摘要过长，已截断；完整历史在 SQLite 中）"
    return text


def compact_messages(messages: list[BaseMessage], model: str, configured_window: int = 0, memory=None) -> list[BaseMessage]:
    """分层上下文压缩（向后兼容入口，仅返回压缩后的消息列表）。"""
    result, _report = compact_messages_report(messages, model, configured_window, memory)
    return result


def compact_messages_report(messages: list[BaseMessage], model: str, configured_window: int = 0,
                            memory=None, trigger: str = "auto", reason: str = "") -> tuple[list[BaseMessage], Optional[CompactionReport]]:
    """分层上下文压缩，并返回结构化报告（供 UI 卡片展示）。

    与 `compact_messages` 行为完全一致，仅额外产出 CompactionReport：
    - P0: system 消息永远保留，不参与压缩。
    - P1: 最近轮 verbatim，按 token 预算（阈值×50%）从最新向前累加（以工具组为原子单位）。
    - P2: 旧段再分 medium（每条精简至约 100 字摘要）/ old（仅保留用户关键指令）。
    - P3: 摘要按「用户/助手 + 轮次号」结构化呈现，保留时序。
    - memory: 可选。传入 LocalMemory 实例时，old 段用户指令归档进长期记忆，
      摘要行写 [见记忆: key] 引用（详见 _archive_user_instruction）。None 时行为不变。
    - trigger / reason: 记录本次压缩的触发来源（自动阈值 / 工具前 / 手动工具），随报告展示。
    - 防抖动：若压缩后总 token 仍接近阈值，把最近轮里最老的整组降级为 old 段（仅留用户指令），
      保证总 token 单调下降、不会下一轮立刻再压；整组移动，工具链始终完整。
    - 未触发压缩（低于阈值）时返回 (原消息, None)。
    """
    if not messages:
        return [], None
    system_msgs = [m for m in messages if isinstance(m, SystemMessage)]
    dialogue = [m for m in messages if not isinstance(m, SystemMessage)]
    threshold = compaction_threshold_tokens(model, configured_window)
    before_tok = estimate_messages_tokens(messages, model)
    if before_tok < threshold:
        return messages, None

    groups = _group_messages(dialogue)
    round_of = _assign_rounds(dialogue)

    # P1: 最近轮 verbatim（按 token 预算 + 消息数上限），以工具组为原子单位避免切裂
    recent_budget = _retain_tokens_for(model, threshold)
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
        summary_text = _build_summary(medium_entries, old_entries, round_of, model, memory)
        result = [*system_msgs, *([AIMessage(content=summary_text)] if summary_text else []), *recent]
        if (estimate_messages_tokens(result, model) <= threshold * 0.9
                or len(recent_groups) <= 1 or guard >= len(recent_groups)):
            # 底线保护：压缩结果必须保留「可继续」的最小上下文。
            # 摘要为空 且 最近轮无任何 human 消息时（recent 可能只剩残缺工具块，随后还会
            # 被 _drop_dangling_tool_call_messages 整块丢弃），从 old 段回捞最近一条含
            # human 的组原样保留，避免压缩后无历史、用户说"继续"时 agent 失去指代。
            if (not summary_text
                    and not any(isinstance(m, HumanMessage) for m in recent)
                    and all_older):
                for gi in range(len(all_older) - 1, -1, -1):
                    if any(isinstance(m, HumanMessage) for m in all_older[gi][0]):
                        anchor = all_older.pop(gi)
                        recent_groups.insert(0, anchor)
                        recent = [m for g in recent_groups for m in g[0]]
                        medium_entries, old_entries = _split_medium_old(all_older, threshold, model)
                        summary_text = _build_summary(medium_entries, old_entries, round_of, model, memory)
                        result = [*system_msgs, *([AIMessage(content=summary_text)] if summary_text else []), *recent]
                        logger.info(
                            "[Context] 底线保护: 摘要为空且最近轮无 human，回捞第 %d 轮原样保留",
                            _group_round(anchor, round_of),
                        )
                        break
            after_tok = estimate_messages_tokens(result, model)
            recent_cnt = sum(len(g[0]) for g in recent_groups)
            logger.info(
                "[Context] 压缩完成: %d tok → %d tok (阈值 %d, 降幅 %.0f%%) | 最近轮 verbatim %d 条, 中段摘要 %d 组, 早期摘要 %d 组",
                before_tok, after_tok, threshold,
                (1 - after_tok / before_tok) * 100,
                recent_cnt, len(medium_entries), len(old_entries),
            )
            report = CompactionReport(
                trigger=trigger,
                summary=summary_text,
                before_tokens=before_tok,
                after_tokens=after_tok,
                threshold_tokens=threshold,
                before_count=len(messages),
                after_count=len(result),
                shadowed_count=len(messages) - len(result),
                recent_verbatim=recent_cnt,
                medium_groups=len(medium_entries),
                old_groups=len(old_entries),
                reason=reason,
            )
            return result, report
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
