"""外层空闲看门狗阈值推导 + 「无事件长任务」忙标记的守护测试。

背景（线上故障）：外层熔断阈值硬编码 90s，小于内层 idle 重试预算 120s，
导致上游完全静默时空闲重试**永远跑不到**就被熔断；且上下文压缩（零图事件、耗时 87s）
被误判为「模型卡死」而强杀整轮。

本测试锁定两个不变式：
  1. resolve_outer_idle_timeout(config) 永不小于「内层 idle × (重试次数+1) + 余量」；
  2. OpBusy/op_busy 计数在异常路径也能正确归零（否则会把看门狗永久关掉）。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent_core"))

import agent_helpers as H  # noqa: E402


def _cfg(**kw):
    base = {"llm_timeout_seconds": 0.0, "llm_idle_timeout_seconds": 60.0, "llm_idle_max_retries": 3}
    base.update(kw)
    return SimpleNamespace(**base)


def test_outer_timeout_auto_derives_from_inner_budget():
    """未配置（0）时按「idle × (retries+1) + 余量」推导。"""
    m = H._LLM_TIMEOUT_MARGIN_SECONDS
    assert H.resolve_outer_idle_timeout(_cfg(llm_idle_timeout_seconds=120.0, llm_idle_max_retries=2)) == 120.0 * 3 + m
    assert H.resolve_outer_idle_timeout(_cfg(llm_idle_timeout_seconds=60.0, llm_idle_max_retries=1)) == 60.0 * 2 + m


def test_outer_timeout_never_below_inner_budget():
    """⚠️ 回归保护：即使把外层阈值配得比内层预算小，也必须被抬到内层预算之上。

    否则外层会抢在内层重试序列跑完前熔断，内层重试实际失效（历史故障根因）。
    """
    m = H._LLM_TIMEOUT_MARGIN_SECONDS
    inner_budget = 120.0 * 3 + m
    # 配 30s（远小于预算）→ 自动抬到预算
    assert H.resolve_outer_idle_timeout(
        _cfg(llm_timeout_seconds=30.0, llm_idle_timeout_seconds=120.0, llm_idle_max_retries=2)
    ) == inner_budget
    # 配 90s（历史硬编码值）→ 同样被抬
    assert H.resolve_outer_idle_timeout(
        _cfg(llm_timeout_seconds=90.0, llm_idle_timeout_seconds=120.0, llm_idle_max_retries=2)
    ) == inner_budget


def test_outer_timeout_respects_larger_configured_value():
    m = H._LLM_TIMEOUT_MARGIN_SECONDS
    assert H.resolve_outer_idle_timeout(
        _cfg(llm_timeout_seconds=900.0, llm_idle_timeout_seconds=120.0, llm_idle_max_retries=2)
    ) == 900.0
    assert 900.0 > 120.0 * 3 + m


def test_outer_timeout_tolerates_missing_attributes():
    """配置对象缺字段时不应抛异常（getattr 兜底），且仍给出安全值。"""
    m = H._LLM_TIMEOUT_MARGIN_SECONDS
    assert H.resolve_outer_idle_timeout(SimpleNamespace()) == 90.0 + m


def test_op_busy_no_container_is_noop():
    """未 set 容器时（如非 stream 路径）应为 no-op，不能报忙。"""
    assert H.op_busy() is False
    with H.OpBusy():
        assert H.op_busy() is False


def test_op_busy_counts_and_resets_on_exception():
    H._op_busy_ctx.set([0])
    assert H.op_busy() is False
    with H.OpBusy():
        assert H.op_busy() is True
        with H.OpBusy():
            assert H.op_busy() is True
        assert H.op_busy() is True, "嵌套内层退出后外层仍应为忙"
    assert H.op_busy() is False
    # 异常路径也必须归零，否则看门狗会被永久关闭
    try:
        with H.OpBusy():
            assert H.op_busy() is True
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert H.op_busy() is False, "异常路径未归零 → 外层空闲看门狗会被永久关掉"


def test_stream_run_uses_derived_timeout_and_busy_marker():
    """静态检查：agent_run 必须用 resolve_outer_idle_timeout，且 is_busy 纳入 op_busy()。"""
    src = (Path(__file__).resolve().parents[1] / "agent_core" / "agent_run.py").read_text(encoding="utf-8")
    assert "llm_timeout = resolve_outer_idle_timeout(self.config)" in src
    assert 'getattr(self.config, "llm_timeout_seconds", 90)' not in src, "不得再回退到硬编码 90s"
    assert "op_busy()" in src
    assert src.count("with OpBusy():") >= 2, "两处 in-loop 压缩都应标忙"
