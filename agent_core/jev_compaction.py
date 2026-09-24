"""判别式上下文压缩（Jev decisions）——fast-jev-compaction 的 Python 移植。

设计文档：poc/FAST-JEV-COMPACTION-POC.md

与原方案（https://github.com/tamaratran/fast-jev-compaction）的核心差异：
原方案是 Claude Code 插件，替换其内置 /compact 摘要；本模块是 context_manager.py
既有「分层生成式摘要」的**前置过滤器**——只负责把 Jev 判定「已无用」的旧工具组
整组删掉，剩下的仍走既有 P2 medium/old 分层（记忆归档、防抖动、底线保护全部保留）。

核心性质（与原方案一致）：
  1. **只删 tool call / tool result**，用户与助手的文本消息永不删除、永不缩短。
  2. 保留的内容**逐字 verbatim**，不重写、不摘要。
  3. 每个删除决策可解释（Jev 概率 + 理由）。
  4. 工具链完整：删除时 call 与 result 同组删除，绝不留下孤立 ToolMessage。

降级铁律（与 jev_tools.py 一致）：Jev 未配置/超时/失败/答案畸形 → 原样返回输入，
调用方行为与不接本模块时完全一致。这是纯增量，不是替换。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

logger = logging.getLogger(__name__)

# ── 默认参数（对齐原方案 Options 表） ──────────────────────────
DEFAULT_KEEP_THRESHOLD = 0.5      # keepResult / keepCall 的最低保留概率
DEFAULT_PRESERVE_RECENT = 6       # 最近 N 个组永不触碰（原方案 preserveRecentMessages）
DEFAULT_MAX_STATE_TOKENS = 25000  # 送给 Jev 的 state 估算 token 上限
DEFAULT_MAX_REQUEST_TOKENS = 30000  # state + 一批问题的估算上限（Jev 32k 请求限制内）
DEFAULT_TRUNCATE_HEAD_CHARS = 300  # 降档截断时保留的 result 头字符数
DEFAULT_MIN_REDUCTION = 0.25      # 收益门槛：删除率低于此则回退原输入（原方案同款保护）

# state 分级裁剪阶梯（原方案第 3 步）：tool 输入依次截到这些字符数
_TOOL_INPUT_STAGES = (1000, 200, 60)
# 旧的非 pin 消息折叠注记
_OMITTED_NOTE = "… {n} chars omitted …"


@dataclass
class GroupDecision:
    """单个工具组的 Jev 判别结果（可审计）。"""
    group_index: int                 # 在传入 groups 中的序号
    action: str                      # keep | truncate | drop
    keep_call: Optional[float] = None
    keep_result: Optional[float] = None
    reason: str = ""
    dropped_chars: int = 0           # 因本决策减少的字符数


@dataclass
class PruneResult:
    """判别式删除的结构化结果。"""
    groups: List[Tuple[List[BaseMessage], int]] = field(default_factory=list)
    decisions: List[GroupDecision] = field(default_factory=list)
    available: bool = False          # Jev 是否实际参与（False = 原样回退）
    before_chars: int = 0
    after_chars: int = 0

    @property
    def reduction_ratio(self) -> float:
        if self.before_chars <= 0:
            return 0.0
        return 1.0 - self.after_chars / self.before_chars

    @property
    def dropped_groups(self) -> int:
        return sum(1 for d in self.decisions if d.action == "drop")

    @property
    def truncated_groups(self) -> int:
        return sum(1 for d in self.decisions if d.action == "truncate")

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "dropped_groups": self.dropped_groups,
            "truncated_groups": self.truncated_groups,
            "kept_groups": len(self.groups) - self.dropped_groups,
            "before_chars": self.before_chars,
            "after_chars": self.after_chars,
            "reduction_ratio": round(self.reduction_ratio, 3),
            "decisions": [
                {"i": d.group_index, "action": d.action,
                 "keep_call": d.keep_call, "keep_result": d.keep_result,
                 "reason": d.reason}
                for d in self.decisions
            ],
        }


# ══════════════════════════════════════════════════════════════
# state 构建（原方案第 2/3 步）
# ══════════════════════════════════════════════════════════════

def _msg_text(m: BaseMessage) -> str:
    c = getattr(m, "content", "") or ""
    return c if isinstance(c, str) else str(c)


def _tool_call_brief(m: BaseMessage) -> str:
    """把 AI(tool_calls) 压成一行（原方案：t12 Read file_path=src/a.ts → ok 480ch）。"""
    calls = getattr(m, "tool_calls", None) or []
    parts = []
    for c in calls:
        name = c.get("name", "?") if isinstance(c, dict) else getattr(c, "name", "?")
        args = c.get("args", {}) if isinstance(c, dict) else getattr(c, "args", {})
        brief_args = ",".join(f"{k}={str(v)[:60]}" for k, v in (args or {}).items())
        parts.append(f"{name}({brief_args})")
    return " → ".join(parts) if parts else "(tool_calls)"


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + "…"


def build_state(groups: Sequence[Tuple[List[BaseMessage], int]],
                max_tokens: int = DEFAULT_MAX_STATE_TOKENS,
                chars_per_token: float = 3.0) -> str:
    """把整段对话渲染成给 Jev 看的 state（旧的在前）。

    规则（原方案第 2 步）：
      - tool result → `ok, N chars (omitted)` 短注（内容不进 state）
      - tool 输入保留（AI(tool_calls) 压成一行 brief）
      - 用户/助手文本保留，**不摘要**
    超出 max_tokens 时按原方案第 3 步的降级阶梯裁剪（由 _fit_state 逐级应用）。

    ponytail: chars_per_token=3.0 是保守估算（中英混合约 1.6~4 字符/token），
    刻意偏大让 state 更容易触发降级而非超限；精确 token 计数可后续换 tiktoken。
    """
    lines: List[str] = []
    for gi, (g, _start) in enumerate(groups):
        for m in g:
            t = getattr(m, "type", "")
            if t == "tool":
                name = getattr(m, "name", "") or "tool"
                lines.append(f"[{gi}] tool_result({name}): ok, {len(_msg_text(m))} chars (omitted)")
            elif t == "ai":
                if getattr(m, "tool_calls", None):
                    lines.append(f"[{gi}] assistant: {_tool_call_brief(m)}")
                else:
                    lines.append(f"[{gi}] assistant: {_msg_text(m)}")
            elif t == "human":
                lines.append(f"[{gi}] user: {_msg_text(m)}")
            else:
                lines.append(f"[{gi}] {t}: {_msg_text(m)}")
    return _fit_state(lines, max_tokens, chars_per_token)


def _fit_state(lines: List[str], max_tokens: int, chars_per_token: float) -> str:
    """分级裁剪 state 直到低于 max_tokens（原方案第 3 步的简化版）。

    阶梯（每级只在上一级不够时应用）：
      1. 长文本行截到头 400 + 尾 100 字符
      2. 更激进：截到头 120 字符
      3. 最旧的非 tool_result 行折叠成 `… N chars omitted …`
    ponytail: 原方案有 5 级（含 tool 输入 1000/200/60 三级），本 POC 合并为 3 级——
    state 里 tool 输入本就只占一行 brief，单独为它设三级阶梯收益极小。
    """
    budget = int(max_tokens * chars_per_token)
    if sum(len(x) for x in lines) <= budget:
        return "\n".join(lines)

    # 级 1：长文本留头尾
    out = []
    for ln in lines:
        if len(ln) > 500:
            out.append(ln[:400] + " …[tail]… " + ln[-100:])
        else:
            out.append(ln)
    if sum(len(x) for x in out) <= budget:
        return "\n".join(out)

    # 级 2：更激进截断
    out = [ln[:120] + "…" if len(ln) > 120 else ln for ln in out]
    if sum(len(x) for x in out) <= budget:
        return "\n".join(out)

    # 级 3：最旧的行折叠（保留 tool_result 短注，它们本来就短且是决策依据）
    while out and sum(len(x) for x in out) > budget:
        # 从最旧开始找第一条可折叠的长行
        for i, ln in enumerate(out):
            if len(ln) > 80 and "tool_result" not in ln:
                out[i] = _OMITTED_NOTE.format(n=len(ln))
                break
        else:
            break  # 没有可折叠的了
    return "\n".join(out)


# ══════════════════════════════════════════════════════════════
# 分批（原方案第 5 步）
# ══════════════════════════════════════════════════════════════

def batch_candidates(candidates: Sequence[int], state: str,
                     max_request_tokens: int = DEFAULT_MAX_REQUEST_TOKENS,
                     chars_per_token: float = 3.0) -> List[List[int]]:
    """把候选组序号分批，保证每批 state + 问题估算不超 max_request_tokens。

    每个候选占约 200 字符（两个 noul 问题的 instructions 是固定模板）。
    ponytail: 串行批处理，未做并发——并发是延迟优化不是正确性前提，
    升级路径：用 asyncio.gather 并发发各批，答案按 group_index 合并。
    """
    per_q_chars = 200
    budget = int(max_request_tokens * chars_per_token) - len(state)
    per_batch = max(1, budget // per_q_chars)
    return [list(candidates[i:i + per_batch]) for i in range(0, len(candidates), per_batch)]


# ══════════════════════════════════════════════════════════════
# 单组决策（原方案第 4/6 步）
# ══════════════════════════════════════════════════════════════

_KEEP_CALL_Q = (
    "This is a tool call an AI assistant made earlier in this session, shown with its "
    "input. Knowing that this call was made (and what it was called with) is still "
    "relevant to the ongoing task. Return 1.0 if it should stay; 0.0 if it is stale noise."
)
_KEEP_RESULT_Q = (
    "This is the result of an earlier tool call. Its contents are still needed for the "
    "ongoing task, and re-running the tool would not reproduce them. Return 1.0 if the "
    "result should stay verbatim; 0.0 if it can be dropped or truncated."
)

# ponytail: 拼进单组 noul 提示的截断结果长度上限——够 Jev 判别噪音还是关键，
# 又不至于把整条长结果塞满提示。超大列表/日志会截断，关键路径/错误/配置值在前部。
_RESULT_IN_PROMPT_CHARS = 600


def decide_group(group: Sequence[BaseMessage], group_index: int, state: str,
                 client, keep_threshold: float = DEFAULT_KEEP_THRESHOLD,
                 truncate_head_chars: int = DEFAULT_TRUNCATE_HEAD_CHARS) -> GroupDecision:
    """对单个工具组问 Jev 两个 noul 问题并给出决策。

    client 需提供 noul(state, instructions) -> float|None（与 jev_tools.JevClient 兼容）。
    任何失败（None/异常）→ 保守判 keep（绝不因 Jev 故障丢信息）。
    """
    brief = _tool_call_brief(group[0]) if group else "(empty)"
    # ponytail: 把该组实际结果内容截断拼进提示，Jev 才能判别噪音 vs 关键信息。
    # 原 POC 只给 Jev 看"调用 + 通用问题"，结果被 build_state 省略成 'ok, N chars (omitted)'，
    # 导致 Jev 对噪音/关键全给 ~0.45 模糊值 → 恒 keep → 删除率 0%（实测证实）。
    result_text = ""
    for m in reversed(group):
        if isinstance(m, ToolMessage):
            result_text = _clip(_msg_text(m), _RESULT_IN_PROMPT_CHARS)
            break
    result_note = f"\n\nTool result content:\n{result_text}" if result_text else ""
    try:
        keep_call = client.noul(state, f"Tool call: {brief}\n\n{_KEEP_CALL_Q}{result_note}")
        keep_result = client.noul(state, f"Tool call: {brief}\n\n{_KEEP_RESULT_Q}{result_note}")
    except Exception as e:  # Jev 故障 → 保守保留
        logger.debug("[JevCompaction] 组 %d 决策失败，保守保留: %r", group_index, e)
        return GroupDecision(group_index, "keep", reason="Jev 调用异常，保守保留")

    if keep_result is None and keep_call is None:
        return GroupDecision(group_index, "keep", reason="Jev 无有效答案，保守保留")

    if keep_result is not None and keep_result >= keep_threshold:
        return GroupDecision(group_index, "keep", keep_call, keep_result,
                             f"keepResult={keep_result:.2f} ≥ {keep_threshold}，逐字保留")
    if keep_call is not None and keep_call >= keep_threshold:
        return GroupDecision(group_index, "truncate", keep_call, keep_result,
                             f"keepResult={keep_result} < {keep_threshold} 但 "
                             f"keepCall={keep_call:.2f} ≥ {keep_threshold}，截断结果")
    return GroupDecision(group_index, "drop", keep_call, keep_result,
                         f"keepCall={keep_call} / keepResult={keep_result} 均 < {keep_threshold}，整组删除")


# ══════════════════════════════════════════════════════════════
# 应用决策（原方案第 7 步）
# ══════════════════════════════════════════════════════════════

def _apply_truncate(group: List[BaseMessage], head_chars: int) -> List[BaseMessage]:
    """保留 call，把该组的 tool result 截到前 head_chars 字符 + 一行注。"""
    out: List[BaseMessage] = []
    for m in group:
        if isinstance(m, ToolMessage):
            text = _msg_text(m)
            if len(text) > head_chars:
                note = f"\n[… truncated by Jev compaction: {len(text)} chars → {head_chars} …]"
                out.append(ToolMessage(content=text[:head_chars] + note,
                                        tool_call_id=m.tool_call_id, name=getattr(m, "name", "")))
                continue
        out.append(m)
    return out


def apply_decisions(groups: Sequence[Tuple[List[BaseMessage], int]],
                    decisions: Sequence[GroupDecision],
                    truncate_head_chars: int = DEFAULT_TRUNCATE_HEAD_CHARS
                    ) -> List[Tuple[List[BaseMessage], int]]:
    """按决策重建组列表。未出现在 decisions 里的组原样保留。"""
    by_index = {d.group_index: d for d in decisions}
    out: List[Tuple[List[BaseMessage], int]] = []
    for gi, (g, start) in enumerate(groups):
        d = by_index.get(gi)
        if d is None or d.action == "keep":
            out.append((g, start))
        elif d.action == "truncate":
            out.append((_apply_truncate(g, truncate_head_chars), start))
        # drop → 整组不加入（call 与 result 同组删除，工具链保持完整）
    return out


# ══════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════

def _is_pinned(groups: Sequence[Tuple[List[BaseMessage], int]], gi: int,
               preserve_recent: int) -> bool:
    """pin 规则：首个含 human 的组、最近 preserve_recent 个组，永不触碰。"""
    n = len(groups)
    if gi >= n - preserve_recent:
        return True
    # 用户最初指令（第一个含 human 的组）永不删
    for j in range(n):
        if any(isinstance(m, HumanMessage) for m in groups[j][0]):
            return gi == j
    return False


def jev_prune_tool_groups(
    groups: Sequence[Tuple[List[BaseMessage], int]],
    client=None,
    keep_threshold: float = DEFAULT_KEEP_THRESHOLD,
    preserve_recent: int = DEFAULT_PRESERVE_RECENT,
    max_state_tokens: int = DEFAULT_MAX_STATE_TOKENS,
    max_request_tokens: int = DEFAULT_MAX_REQUEST_TOKENS,
    truncate_head_chars: int = DEFAULT_TRUNCATE_HEAD_CHARS,
    min_reduction: float = DEFAULT_MIN_REDUCTION,
) -> PruneResult:
    """对旧工具组做 Jev 判别式删除。返回 PruneResult（含可审计决策）。

    降级路径（任一命中即原样返回，available=False）：
      - client 为 None（未注入 / Jev 未配置）
      - 没有可判定的候选组（全被 pin 或无工具组）
      - 删除率低于 min_reduction（不值得，回退避免无谓改动）
      - Jev 全部调用失败（decisions 全为 keep 且无一组真正被删）

    注意：本函数**只处理 groups**，不关心 system 消息（P0 已在调用方保证）。
    """
    before_chars = sum(len(_msg_text(m)) for g, _ in groups for m in g)
    result = PruneResult(groups=list(groups), available=False,
                         before_chars=before_chars, after_chars=before_chars)

    if client is None:
        result.decisions = []
        return result

    candidates = [gi for gi in range(len(groups))
                  if not _is_pinned(groups, gi, preserve_recent)
                  and any(isinstance(m, ToolMessage) for m in groups[gi][0])]
    if not candidates:
        return result

    state = build_state(groups, max_state_tokens)
    decisions: List[GroupDecision] = []
    for batch in batch_candidates(candidates, state, max_request_tokens):
        for gi in batch:
            decisions.append(decide_group(groups[gi][0], gi, state, client,
                                          keep_threshold, truncate_head_chars))

    pruned = apply_decisions(groups, decisions, truncate_head_chars)
    after_chars = sum(len(_msg_text(m)) for g, _ in pruned for m in g)

    # 收益门槛：删得太少就不改（原方案 reductionRatio < 0.25 的同款保护）
    if before_chars > 0 and (1 - after_chars / before_chars) < min_reduction:
        logger.debug("[JevCompaction] 删除率 %.3f 低于门槛 %.2f，回退原输入",
                     1 - after_chars / before_chars, min_reduction)
        result.decisions = decisions  # 仍返回决策供审计/调试
        return result

    result.groups = pruned
    result.decisions = decisions
    result.available = True
    result.after_chars = after_chars
    logger.info("[JevCompaction] 判别式删除: %d 字符 → %d 字符 (降幅 %.0f%%, 删 %d 组 / 截 %d 组)",
                before_chars, after_chars, (1 - after_chars / before_chars) * 100,
                result.dropped_groups, result.truncated_groups)
    return result


def flatten_groups(groups: Iterable[Tuple[List[BaseMessage], int]]) -> List[BaseMessage]:
    """把 [(group, start), ...] 摊平回消息列表（供接回 context_manager 的既有链路）。"""
    return [m for g, _ in groups for m in g]


# ══════════════════════════════════════════════════════════════
# 离线演示替身（POC 用，生产不用）
# ══════════════════════════════════════════════════════════════

class FakeJevLike:
    """模拟一个"理想"Jev：能识别关键信息、判噪音为无用。

    仅用于 poc/verify_jev_compaction.py 的离线演示，让 POC 无需 API key
    也能展示判别式删除的收益。判定规则是朴素关键词启发式——
    ponytail: 这是演示替身不是生产逻辑，真实场景由 Jev 模型本身做语义判断。
    """

    def __init__(self):
        self.calls = []

    def noul(self, state: str, instructions: str) -> Optional[float]:
        self.calls.append((state, instructions))
        # 从 instructions 里取出被问询的 tool call brief（含文件路径）
        brief = instructions.split("Tool call:", 1)[-1] if "Tool call:" in instructions else ""
        # 在 state 里找该 call 之后紧跟的 tool_result 行拿不到内容（已被替换为短注），
        # 因此改用「该组在 state 中的序号」反查——演示替身简化为按路径序号启发式：
        # 关键组在构造时用的是 /f0~/f1，噪音组是 /f100+。
        is_critical = any(f"/f{i}.py" in brief for i in range(2))
        if "stay verbatim" in instructions or "re-run" in instructions:
            return 0.95 if is_critical else 0.05
        return 0.9 if is_critical else 0.05
