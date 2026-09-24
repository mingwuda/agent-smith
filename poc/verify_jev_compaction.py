"""POC 验证：对比「当前机械裁剪」vs「Jev 判别式删除」的信息保真度。

场景设计（这是本 POC 的核心论点）：
  一个长会话里，早期有若干工具结果包含**后续仍关键的信息**（精确文件路径、
  确切错误信息、用户约束），同时有大量**已无用的噪音工具结果**。
  - 当前实现（context_manager._summarize_medium）对 medium 段每条无差别截到 100 字
    → 关键信息可能被截断丢失。
  - Jev 判别式删除对"仍关键"的组 verbatim 保留，只删/截真正无用的组
    → 关键信息完整保留。

运行：
    python poc/verify_jev_compaction.py            # 离线（mock Jev，无需 key）
    TYPESAFE_API_KEY=xxx python poc/verify_jev_compaction.py --live   # 真实 Jev
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_core.main import app  # noqa: F401  触发 sys.path 注入
from agent_core.context_manager import (
    _group_messages,
    _split_medium_old,
    _summarize_medium,
    compact_messages_report,
    estimate_messages_tokens,
)
from agent_core.jev_compaction import (
    FakeJevLike,
    build_state,
    flatten_groups,
    jev_prune_tool_groups,
)
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

# ── 关键信息探针：这些字符串如果丢了，就是保真度失败 ────────────
CRITICAL_PROBES = [
    "/opt/app/src/auth/login_handler.py",          # 精确定位 bug 的文件路径
    "ValueError: invalid literal for int()",       # 确切错误信息
    "NEVER edit src/generated",                    # 用户明确约束
]


def _tool_group(gi: int, result_text: str):
    return [
        AIMessage(content="", tool_calls=[{
            "name": "read_file", "args": {"file_path": f"/f{gi}.py"}, "id": f"c{gi}",
        }]),
        ToolMessage(content=result_text, tool_call_id=f"c{gi}", name="read_file"),
    ]


def build_session(n_noise: int = 24) -> list:
    """构造会话：1 条用户指令 + 2 个关键工具组 + n_noise 个噪音工具组 + 收尾提问。

    真实场景的形态：长会话里绝大多数工具结果是噪音（列目录、重复 grep、
    读无关文件），只有少数几条含后续关键信息。Jev 的价值正是删掉噪音，
    让那少数关键组能落进 P1 verbatim 区而不是被 medium 段裁掉。
    """
    msgs = [SystemMessage(content="You are a coding agent.")]
    msgs.append(HumanMessage(
        content="修复登录 bug。约束：NEVER edit src/generated 目录。"
    ))

    # 2 个关键组：每条都含一个必须保留的探针。
    # 关键点：探针放在**长结果的中后部**（真实场景——读大文件时关键行在中间），
    # 这样 _clip_middle(content, 100) 的「保留头部 100 字」策略才会真的丢掉它。
    filler = "无关的日志输出行，没有任何信息量。" * 12   # ~300 字前缀
    critical = [
        filler + " 已定位 bug 在 /opt/app/src/auth/login_handler.py 第 88 行，"
        "token 解析处少了一次 None 判断，修复方案是加 guard clause。",
        filler + " 运行 pytest 报错：ValueError: invalid literal for int() with base 10: ''，"
        "根因是空字符串直接 int()，需要先 strip 并判空。",
    ]
    for i, text in enumerate(critical):
        msgs.extend(_tool_group(i, text))

    # 噪音组：大量无用内容（列目录、读 node_modules、重复 grep 等）
    for i in range(n_noise):
        msgs.extend(_tool_group(
            100 + i,
            f"ls -la 输出第 {i} 次：drwxr-xr-x node_modules/__pycache__/ dist/ build/ "
            + "tmpfile.dat " * 40,
        ))

    msgs.append(HumanMessage(content="现在把修复补丁写进去，然后跑测试。"))
    return msgs


def check_probes(text: str) -> list:
    """返回丢失的关键探针列表。"""
    return [p for p in CRITICAL_PROBES if p not in text]


def render(messages: list) -> str:
    return "\n".join(str(getattr(m, "content", "")) for m in messages)


# ══════════════════════════════════════════════════════════════

def run_current_impl(messages: list, model: str = "deepseek-chat",
                     window: int = 4000) -> dict:
    """当前实现：分层生成式摘要（机械裁剪到 100 字）。"""
    out, report = compact_messages_report(messages, model, configured_window=window)
    text = render(out)
    return {
        "name": "当前实现（机械裁剪 100 字）",
        "messages": out,
        "text": text,
        "tokens": estimate_messages_tokens(out, model),
        "before_tokens": report.before_tokens if report else estimate_messages_tokens(messages, model),
        "lost_probes": check_probes(text),
        "report": report,
    }


def run_jev_impl(messages: list, client, model: str = "deepseek-chat",
                 window: int = 4000) -> dict:
    """Jev 判别式删除：先删无用工具组，剩下的走既有 medium/old 分层。

    关键：删除后若已低于**原始阈值**，就不再走既有压缩——Jev 删除本身就是
    这次压缩，关键组因此能 verbatim 保留。若仍超阈值，才让既有链路兜底
    （此时丢信息的风险与当前实现相同，但输入已更小）。
    """
    system = [m for m in messages if isinstance(m, SystemMessage)]
    dialogue = [m for m in messages if not isinstance(m, SystemMessage)]
    groups = _group_messages(dialogue)

    pruned = jev_prune_tool_groups(groups, client=client, preserve_recent=4)
    kept_dialogue = flatten_groups(pruned.groups)
    candidate = [*system, *kept_dialogue]
    orig_tokens = estimate_messages_tokens(messages, model)

    # 用原始 window 判断：删除后已达标 → 直接采用，不再二次压缩
    out, report = compact_messages_report(candidate, model, configured_window=window)
    if report is None:
        out, report = candidate, None   # 未触发压缩 = Jev 删除已足够
    text = render(out)
    return {
        "name": "Jev 判别式删除 + 既有分层",
        "messages": out,
        "text": text,
        "tokens": estimate_messages_tokens(out, model),
        "before_tokens": orig_tokens,
        "lost_probes": check_probes(text),
        "report": report,
        "prune": pruned,
    }


def print_result(r: dict) -> None:
    print(f"\n{'─' * 68}")
    print(f"【{r['name']}】")
    print(f"  token: {r['before_tokens']} → {r['tokens']}"
          f"（降幅 {(1 - r['tokens'] / r['before_tokens']) * 100:.0f}%）")
    if r.get("prune") is not None:
        p = r["prune"]
        print(f"  Jev 判别式删除: 可用={p.available} 删 {p.dropped_groups} 组 / "
              f"截 {p.truncated_groups} 组 / 留 {len(p.groups)} 组"
              f"（字符降幅 {p.reduction_ratio * 100:.0f}%）")
    if r["lost_probes"]:
        print(f"  ❌ 丢失关键信息 {len(r['lost_probes'])} 条:")
        for p in r["lost_probes"]:
            print(f"       - {p}")
    else:
        print(f"  ✅ 关键信息全部保留（{len(CRITICAL_PROBES)}/{len(CRITICAL_PROBES)}）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="使用真实 Jev API（需 TYPESAFE_API_KEY）")
    ap.add_argument("--noise", type=int, default=14, help="噪音工具组数量")
    args = ap.parse_args()

    messages = build_session(args.noise)
    print("=" * 68)
    print("POC：上下文压缩保真度对比")
    print(f"会话规模：{len(messages)} 条消息，"
          f"{estimate_messages_tokens(messages, 'deepseek-chat')} tokens"
          f"（含 {args.noise} 个噪音工具组 + 4 个关键工具组）")
    print(f"关键信息探针：{len(CRITICAL_PROBES)} 条")
    print("=" * 68)

    if args.live:
        from agent_core.tools.jev_tools import JevClient
        client = JevClient()
        if not client.api_key:
            print("❌ --live 需要 TYPESAFE_API_KEY（env 或 ~/.desktop_agent/config.json）")
            return 2
        print(f"\n使用真实 Jev: {client.base_url} model={client.model}")
    else:
        # 离线 mock：模拟一个"理想"Jev——认识关键信息，判噪音为无用
        client = FakeJevLike()
        print("\n使用离线 mock Jev（--live 可切真实 API）")

    cur = run_current_impl(messages)
    jev = run_jev_impl(messages, client)

    print_result(cur)
    print_result(jev)

    # ── 结论 ──
    print(f"\n{'=' * 68}")
    print("结论")
    print("=" * 68)
    cur_lost, jev_lost = len(cur["lost_probes"]), len(jev["lost_probes"])
    if jev_lost < cur_lost:
        print(f"✅ Jev 判别式删除保真度更优：丢失关键信息 {cur_lost} 条 → {jev_lost} 条")
    elif jev_lost == cur_lost == 0:
        print("✅ 两者均未丢失关键信息（本场景区分度不足，可增大 --noise）")
    else:
        print(f"⚠️  当前实现丢失 {cur_lost} 条，Jev 丢失 {jev_lost} 条")

    if jev.get("prune") and jev["prune"].available:
        print(f"✅ Jev 实际参与删除：{jev['prune'].dropped_groups} 组被整组删除，"
              f"决策可审计（to_dict 含每组概率与理由）")
    else:
        print("ℹ️  Jev 未参与（回退路径）：行为与当前实现一致，零风险")

    print("\n判定门槛（来自原方案）：reductionRatio < 0.25 时回退原输入，")
    print("避免为极小收益付出额外请求成本。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
