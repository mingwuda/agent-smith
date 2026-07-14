"""防循环检测（纯逻辑，仅依赖标准库，便于独立测试）。

在 agent.py 的工具调用流中周期性调用：命中循环模式时返回非空原因字符串，
由调用方提前终止本轮任务。详见 agent.py 中 _detect_tool_loop 的调用点。
"""
from typing import Dict, List


# ponytail: 探索类工具（搜索/执行）天然会被反复调用且参数各异，
# 一律不计入防循环检测，否则正常的调研/执行会话会被误杀。
# 仅对其它工具（文件读写、编辑等）做循环判定。
_EXPLORATORY_TOOLS = {"web_search", "web_fetch", "run_shell", "run_python"}


def _detect_tool_loop(calls: List[Dict], recursion_limit: int, current_steps: int = None) -> str:
    """检测工具调用是否陷入重复循环。

    calls: 工具调用历史，每项含 {"tool": str, "signature": str, ...}
    返回命中原因字符串；未命中返回 ""。
    """
    # 先把探索类工具调用剔除，避免误杀（见 _EXPLORATORY_TOOLS）
    calls = [c for c in calls if c.get("tool") not in _EXPLORATORY_TOOLS]
    if len(calls) < 4:
        return ""

    # ── 检测1：同一工具+同一参数严格重复 ≥20 次（单步循环）──
    latest = calls[-1]
    latest_sig = latest.get("signature", "")
    if latest_sig:
        last30_sigs = [item.get("signature", "") for item in calls[-30:] if item.get("signature")]
        count = last30_sigs.count(latest_sig)
        if count >= 20:
            return f"最近 30 次工具调用中，同一工具和参数严格重复了 {count} 次"

    # ── 检测2：参数循环（A→B→A→B 模式）──
    # 要求连续重复至少 3 轮才中断
    if len(calls) >= 12:
        recent18 = calls[-18:]
        sigs = [c.get("signature", "") for c in recent18 if c.get("signature")]
        if len(sigs) >= 12:
            for window in (2, 3, 4):
                if (
                    len(sigs) >= window * 3
                    and sigs[-window:] == sigs[-window * 2:-window]
                    and sigs[-window * 2:-window] == sigs[-window * 3:-window * 2]
                ):
                    return (
                        f"工具调用出现循环模式：最近 {window * 3} 次的形式为 "
                        + " -> ".join(sigs[-window * 3:])
                    )

    # 真实图步数优先（由调用方按事件累计：模型/工具各计一步），缺失时退化为 len(calls)*2+1 估算。
    # 注意：步数估算仍基于"过滤后的 calls"这一约束（探索类工具按需求不计入预警，见 _EXPLORATORY_TOOLS），
    # 但 current_steps 来自全量事件，能更贴近真实的 graph 步数。
    estimated_graph_steps = current_steps if current_steps is not None else len(calls) * 2 + 1
    if estimated_graph_steps >= max(6, recursion_limit - 3):
        tail = calls[-8:]
        tail_unique = {item.get("tool", "") for item in tail}
        tail_signatures = {item.get("signature", "") for item in tail if item.get("signature")}
        if len(tail_unique) <= 3 and len(tail_signatures) <= 3:
            return f"已接近最大推理步数，且最近工具类型仍高度重复：{', '.join(sorted(tail_unique))}"

    return ""
