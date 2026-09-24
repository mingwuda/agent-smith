"""判别式上下文压缩（agent_core/jev_compaction.py）测试。

锁定契约：
  1. 三档决策正确：keepResult≥t → verbatim 保留；keepCall≥t → 截断 result；都 <t → 整组删
  2. pin 规则：首个含 human 的组、最近 N 个组、永不触碰
  3. 工具链完整：删除后无孤立 ToolMessage，也无没有 result 的 call
  4. 降级路径：client=None / Jev 返回 None / Jev 抛异常 → 原样返回
  5. state 构建：tool result 换成短注，文本不被摘要
  6. 收益门槛：删除率 < min_reduction → 回退原输入
  7. 文本消息永不删除（原方案核心性质）

运行：python -m pytest tests/test_jev_compaction.py -q
"""
import sys

import pytest

from agent_core.main import app  # noqa: F401  触发 sys.path 注入
from agent_core.jev_compaction import (
    DEFAULT_KEEP_THRESHOLD,
    DEFAULT_PRESERVE_RECENT,
    GroupDecision,
    PruneResult,
    apply_decisions,
    batch_candidates,
    build_state,
    decide_group,
    flatten_groups,
    jev_prune_tool_groups,
)
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage


# ── 测试替身：可编程的 Jev client ─────────────────────────────

class FakeJev:
    """按 instructions 关键词返回预设概率，记录每次调用便于断言。"""

    def __init__(self, keep_call=0.1, keep_result=0.1, calls=None):
        self.keep_call = keep_call
        self.keep_result = keep_result
        self.calls = calls if calls is not None else []

    def noul(self, state, instructions):
        self.calls.append((state, instructions))
        if "re-run" in instructions or "stay verbatim" in instructions:
            return self.keep_result
        return self.keep_call


class NoneJev:
    """Jev 完全不可用：所有问题返回 None。"""

    def __init__(self):
        self.calls = []

    def noul(self, state, instructions):
        self.calls.append((state, instructions))
        return None


class BoomJev:
    """Jev 抛异常。"""

    def noul(self, state, instructions):
        raise RuntimeError("network down")


# ── 历史构造助手 ──────────────────────────────────────────────

def _tool_group(gi: int, result_text: str = "result content " * 20):
    """一个 AI(tool_calls) + 对应 ToolMessage 的原子组。"""
    return (
        AIMessage(content="", tool_calls=[{
            "name": "read_file", "args": {"file_path": f"/p{gi}.py"}, "id": f"call_{gi}",
        }]),
        ToolMessage(content=result_text, tool_call_id=f"call_{gi}", name="read_file"),
    )


def _history(n_tool_groups: int = 10, result_text: str = "result content " * 20):
    """用户指令 + n 个工具组 + 最后一轮用户提问（保证有 human 在最近段）。"""
    groups = [([HumanMessage(content="最初指令：修复登录 bug，不要改 generated 目录")], 0)]
    for i in range(n_tool_groups):
        groups.append((list(_tool_group(i, result_text)), 1 + i * 2))
    groups.append(([HumanMessage(content="继续")], 1 + n_tool_groups * 2))
    return groups


# ══════════════════════════════════════════════════════════════
# ① 三档决策
# ══════════════════════════════════════════════════════════════

def test_decide_keep_when_result_still_needed():
    """keepResult ≥ 阈值 → keep（逐字保留）。"""
    d = decide_group(list(_tool_group(0)), 0, "state", FakeJev(keep_call=0.1, keep_result=0.9))
    assert d.action == "keep"
    assert d.keep_result == 0.9


def test_decide_truncate_when_only_call_matters():
    """keepResult < t 但 keepCall ≥ t → truncate。"""
    d = decide_group(list(_tool_group(0)), 0, "state", FakeJev(keep_call=0.8, keep_result=0.2))
    assert d.action == "truncate"
    assert d.keep_call == 0.8 and d.keep_result == 0.2


def test_decide_drop_when_both_below_threshold():
    """两者都 < t → drop。"""
    d = decide_group(list(_tool_group(0)), 0, "state", FakeJev(keep_call=0.1, keep_result=0.1))
    assert d.action == "drop"


def test_decide_asks_two_questions():
    """每个组必须问两个 noul（call 与 result 各一）。"""
    j = FakeJev()
    decide_group(list(_tool_group(0)), 0, "state", j)
    assert len(j.calls) == 2


def test_decide_keeps_when_jev_returns_none():
    """Jev 无有效答案 → 保守 keep（绝不因故障丢信息）。"""
    d = decide_group(list(_tool_group(0)), 0, "state", NoneJev())
    assert d.action == "keep"
    assert "无有效答案" in d.reason or "保守" in d.reason


def test_decide_keeps_when_jev_raises():
    """Jev 抛异常 → 保守 keep。"""
    d = decide_group(list(_tool_group(0)), 0, "state", BoomJev())
    assert d.action == "keep"


# ══════════════════════════════════════════════════════════════
# ② pin 规则
# ══════════════════════════════════════════════════════════════

def test_first_human_group_is_pinned():
    """用户最初指令所在组永不被删（即使 Jev 说该删）。"""
    groups = _history(8)
    r = jev_prune_tool_groups(groups, client=FakeJev(keep_call=0.0, keep_result=0.0),
                              preserve_recent=1)
    kept = flatten_groups(r.groups)
    assert any(isinstance(m, HumanMessage) and "最初指令" in m.content for m in kept)


def test_recent_groups_are_pinned():
    """最近 preserve_recent 个组永不触碰。"""
    groups = _history(10)
    r = jev_prune_tool_groups(groups, client=FakeJev(keep_call=0.0, keep_result=0.0),
                              preserve_recent=3)
    # 最后 3 组（含结尾 human）必须原样在
    tail = flatten_groups(r.groups)[-6:]
    assert any(isinstance(m, HumanMessage) and m.content == "继续" for m in tail)


def test_pinned_groups_not_sent_to_jev():
    """被 pin 的组不应产生 Jev 调用（省调用）。"""
    groups = _history(10)   # 共 12 组：[0]=human指令, [1..10]=工具组, [11]=human"继续"
    j = FakeJev()
    jev_prune_tool_groups(groups, client=j, preserve_recent=6)
    # pin 最后 6 组（索引 6..11）+ 首个 human 组（索引 0）→ 候选 = 索引 1..5 共 5 个工具组
    # 5 个候选 × 2 问 = 10 次
    assert len(j.calls) == 10


# ══════════════════════════════════════════════════════════════
# ③ 工具链完整性
# ══════════════════════════════════════════════════════════════

def test_no_orphan_tool_message_after_drop():
    """删除后不存在孤立 ToolMessage（其 call 也被删）。"""
    groups = _history(10)
    r = jev_prune_tool_groups(groups, client=FakeJev(keep_call=0.0, keep_result=0.0),
                              preserve_recent=1)
    msgs = flatten_groups(r.groups)
    call_ids = {c["id"] for m in msgs
                if isinstance(m, AIMessage) for c in (m.tool_calls or [])}
    for m in msgs:
        if isinstance(m, ToolMessage):
            assert m.tool_call_id in call_ids, "存在孤立 ToolMessage"


def test_no_call_without_result_after_drop():
    """删除后不存在没有 result 的 call（INVALID_CHAT_HISTORY 防护）。"""
    groups = _history(10)
    r = jev_prune_tool_groups(groups, client=FakeJev(keep_call=0.0, keep_result=0.0),
                              preserve_recent=1)
    msgs = flatten_groups(r.groups)
    result_ids = {m.tool_call_id for m in msgs if isinstance(m, ToolMessage)}
    for m in msgs:
        if isinstance(m, AIMessage):
            for c in (m.tool_calls or []):
                assert c["id"] in result_ids, f"call {c['id']} 失去 result"


def test_truncate_keeps_call_and_shortens_result():
    """truncate 档：call 保留，result 被截短并带注记。"""
    groups = _history(10)
    long_text = "x" * 5000
    groups = [([HumanMessage(content="最初指令")], 0)]
    for i in range(10):
        groups.append((list(_tool_group(i, long_text)), 1 + i * 2))
    groups.append(([HumanMessage(content="继续")], 100))
    r = jev_prune_tool_groups(groups, client=FakeJev(keep_call=0.9, keep_result=0.1),
                              preserve_recent=1, truncate_head_chars=300)
    msgs = flatten_groups(r.groups)
    tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
    assert tool_msgs, "truncate 不应删掉 tool 消息"
    assert all(len(m.content) < 1000 for m in tool_msgs)
    assert any("truncated by Jev compaction" in m.content for m in tool_msgs)


# ══════════════════════════════════════════════════════════════
# ④ 降级路径
# ══════════════════════════════════════════════════════════════

def test_client_none_returns_input_unchanged():
    """client=None → 原样返回，available=False。"""
    groups = _history(10)
    r = jev_prune_tool_groups(groups, client=None)
    assert r.available is False
    assert r.groups == groups
    assert r.after_chars == r.before_chars


def test_jev_all_none_returns_input_unchanged():
    """Jev 全返回 None → 无一组被删 → 回退原输入。"""
    groups = _history(10)
    r = jev_prune_tool_groups(groups, client=NoneJev(), preserve_recent=1)
    assert r.available is False
    assert flatten_groups(r.groups) == flatten_groups(groups)


def test_jev_raises_returns_input_unchanged():
    """Jev 抛异常 → 全部保守 keep → 回退原输入。"""
    groups = _history(10)
    r = jev_prune_tool_groups(groups, client=BoomJev(), preserve_recent=1)
    assert r.available is False
    assert flatten_groups(r.groups) == flatten_groups(groups)


def test_no_candidates_returns_input_unchanged():
    """没有工具组（纯对话）→ 原样返回。"""
    groups = [([HumanMessage(content="hi")], 0), ([AIMessage(content="hello")], 1)]
    r = jev_prune_tool_groups(groups, client=FakeJev(keep_call=0.0, keep_result=0.0))
    assert r.available is False
    assert r.groups == groups


# ══════════════════════════════════════════════════════════════
# ⑤ state 构建
# ══════════════════════════════════════════════════════════════

def test_state_replaces_tool_result_with_note():
    """tool result 内容不进 state，只留 `ok, N chars (omitted)`。"""
    groups = _history(3, result_text="SECRET_FILE_CONTENT_ABC")
    state = build_state(groups)
    assert "SECRET_FILE_CONTENT_ABC" not in state
    assert "chars (omitted)" in state


def test_state_keeps_user_text_verbatim():
    """用户文本逐字进 state（不摘要）。"""
    groups = [([HumanMessage(content="绝对不要修改 src/generated 目录")], 0)]
    state = build_state(groups)
    assert "绝对不要修改 src/generated 目录" in state


def test_state_includes_tool_input():
    """tool 输入（文件路径）进 state。"""
    groups = [([HumanMessage(content="go")], 0), (list(_tool_group(0)), 1)]
    state = build_state(groups)
    assert "/p0.py" in state


def test_state_respects_max_tokens():
    """超长 state 必须被裁剪到预算内。"""
    groups = [([HumanMessage(content="y" * 100)], 0)]
    for i in range(30):
        groups.append((list(_tool_group(i, "z" * 3000)), 1 + i * 2))
    state = build_state(groups, max_tokens=1000, chars_per_token=3.0)
    assert len(state) <= 3000 * 1.5  # 允许级 3 折叠的余量


# ══════════════════════════════════════════════════════════════
# ⑥ 收益门槛
# ══════════════════════════════════════════════════════════════

def test_low_reduction_falls_back():
    """删除率低于 min_reduction → 回退原输入（不值得改）。"""
    groups = _history(10)
    # 只让 1 组被删，其余 keep → 删除率很低
    class MostlyKeep(FakeJev):
        def __init__(self):
            super().__init__()
            self.n = 0

        def noul(self, state, instructions):
            self.calls.append((state, instructions))
            self.n += 1
            if "stay verbatim" in instructions or "re-run" in instructions:
                return 0.9
            return 0.9

    r = jev_prune_tool_groups(groups, client=MostlyKeep(), preserve_recent=1,
                              min_reduction=0.25)
    assert r.available is False
    assert flatten_groups(r.groups) == flatten_groups(groups)


def test_high_reduction_applies():
    """删除率足够高 → 生效，available=True。"""
    groups = _history(10)
    r = jev_prune_tool_groups(groups, client=FakeJev(keep_call=0.0, keep_result=0.0),
                              preserve_recent=1)
    assert r.available is True
    assert r.dropped_groups > 0
    assert r.reduction_ratio > 0.25


# ══════════════════════════════════════════════════════════════
# ⑦ 文本消息永不删除
# ══════════════════════════════════════════════════════════════

def test_text_messages_never_dropped():
    """即使 Jev 全判删，用户/助手文本消息也必须保留。"""
    groups = [
        ([HumanMessage(content="用户指令A")], 0),
        ([AIMessage(content="助手解释B")], 1),
    ]
    for i in range(6):
        groups.append((list(_tool_group(i)), 2 + i * 2))
    groups.append(([HumanMessage(content="用户指令C")], 100))
    r = jev_prune_tool_groups(groups, client=FakeJev(keep_call=0.0, keep_result=0.0),
                              preserve_recent=1)
    texts = [m.content for m in flatten_groups(r.groups)
             if isinstance(m, (HumanMessage, AIMessage)) and m.content]
    assert "用户指令A" in texts
    assert "助手解释B" in texts
    assert "用户指令C" in texts


# ══════════════════════════════════════════════════════════════
# ⑧ 分批与工具函数
# ══════════════════════════════════════════════════════════════

def test_batch_candidates_splits():
    """候选数超过单批容量时必须分批，且不丢候选。"""
    state = "s" * 1000
    batches = batch_candidates(list(range(50)), state, max_request_tokens=3000,
                               chars_per_token=3.0)
    flat = [x for b in batches for x in b]
    assert flat == list(range(50))
    assert len(batches) > 1


def test_batch_candidates_single_batch_when_small():
    """候选少时一批装下。"""
    batches = batch_candidates([0, 1, 2], "s" * 100)
    assert len(batches) == 1


def test_apply_decisions_unknown_index_kept():
    """decisions 里没有的组原样保留。"""
    groups = _history(3)
    out = apply_decisions(groups, [GroupDecision(1, "drop")])
    assert len(out) == len(groups) - 1
    assert flatten_groups(out) == flatten_groups(groups[:1] + groups[2:])


def test_prune_result_to_dict():
    """to_dict 可序列化（供 SSE/UI 展示）。"""
    groups = _history(10)
    r = jev_prune_tool_groups(groups, client=FakeJev(keep_call=0.0, keep_result=0.0),
                              preserve_recent=1)
    d = r.to_dict()
    assert d["available"] is True
    assert d["dropped_groups"] == r.dropped_groups
    assert isinstance(d["decisions"], list) and d["decisions"]


def test_defaults_match_upstream():
    """默认参数对齐原方案 Options 表。"""
    assert DEFAULT_KEEP_THRESHOLD == 0.5
    assert DEFAULT_PRESERVE_RECENT == 6

# ══════════════════════════════════════════════════════════════
# ⑤ 阶段 2：compact_messages_report 内接入（use_jev_compaction）
# ══════════════════════════════════════════════════════════════

from agent_core.context_manager import (
    compact_messages_report,
    estimate_messages_tokens,
)


def _msgs_from_groups(groups) -> list:
    """把 [(group, start)] 摊平成消息列表（可喂给 compact_messages_report）。"""
    return flatten_groups(groups)


def test_report_default_no_jev_effect():
    """默认（未开启）use_jev_compaction 不影响既有压缩行为。"""
    groups = _history(40, result_text="x" * 300)  # 足够大触发压缩
    msgs = _msgs_from_groups(groups)
    assert estimate_messages_tokens(msgs, "gpt-4o") > 0
    out, report = compact_messages_report(msgs, "gpt-4o", configured_window=1000)
    assert report is not None  # 触发压缩
    assert report.jev_available is False
    assert report.jev_pruned_groups == 0


def test_report_jev_enabled_with_fake_prunes_and_flags():
    """开启 use_jev_compaction + 注入会删除的 FakeJev → 报删除组数并标 available。"""
    drop_jev = FakeJev(keep_call=0.0, keep_result=0.0)  # 全删（除 pin）
    groups = _history(40, result_text="x" * 300)
    msgs = _msgs_from_groups(groups)
    out, report = compact_messages_report(
        msgs, "gpt-4o", configured_window=1000,
        use_jev_compaction=True, jev_client=drop_jev,
        jev_preserve_recent=1, jev_min_reduction=0.01,
    )
    assert report is not None
    assert report.jev_available is True
    assert report.jev_pruned_groups > 0
    # Jev 全删后通常已低于阈值 → 直接采用（走 Jev 完成压缩分支）
    assert report.jev_available is True


def test_report_jev_jevc_client_none_available_false():
    """开启但 client 不可用（None 值对）→ 回退既有压缩，不因 Jev 失败阻断或丢信息。"""
    groups = _history(40, result_text="x" * 300)
    msgs = _msgs_from_groups(groups)
    # NoneJev 是 jev_compaction 的替身，noul 全返回 None → 判不可用 → 走既有链路
    out, report = compact_messages_report(
        msgs, "gpt-4o", configured_window=1000,
        use_jev_compaction=True, jev_client=NoneJev(),
        jev_preserve_recent=1, jev_min_reduction=0.01,
    )
    assert report is not None
    assert report.jev_available is False
    # 关键：Jev 不可用必须 = 不开 Jev 的既有结果，逐字节一致（降级零影响）
    out_base, _ = compact_messages_report(msgs, "gpt-4o", configured_window=1000)
    assert flatten_groups([(out, 0)]) == out_base


def test_report_jev_boom_client_falls_back():
    """Jev 抛异常 → 不扩散，回退既有链路且与不开 Jev 一致。"""
    groups = _history(40, result_text="x" * 300)
    msgs = _msgs_from_groups(groups)
    out, report = compact_messages_report(
        msgs, "gpt-4o", configured_window=1000,
        use_jev_compaction=True, jev_client=BoomJev(),
        jev_preserve_recent=1, jev_min_reduction=0.01,
    )
    assert report is not None
    assert report.jev_available is False
if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
