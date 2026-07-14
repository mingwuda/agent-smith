"""_detect_tool_loop 的单元测试。

验证：探索类工具（web_search / web_fetch / run_shell / run_python）被整体排除，
      不再触发防循环；其余工具的单步重复 / 循环模式 / 接近步数上限 仍被捕获。

运行：python tests/test_loop_guard.py  （仅依赖标准库）
"""
import sys
from pathlib import Path

# 让 tests 能 import 同级的 loop_guard（项目以 agent_core 为导入根）
ROOT = Path(__file__).resolve().parents[1] / "agent_core"
sys.path.insert(0, str(ROOT))

from loop_guard import _detect_tool_loop, _EXPLORATORY_TOOLS  # noqa: E402


def _mk(tool: str, sig: str) -> dict:
    return {"tool": tool, "signature": sig}


# ── 探索类工具：不应被误杀 ──

def test_exploratory_web_search_varied():
    calls = [_mk("web_search", f"web_search:q{i}") for i in range(30)]
    assert _detect_tool_loop(calls, 60) == ""


def test_exploratory_run_shell_varied():
    calls = [_mk("run_shell", f"run_shell:cmd{i}") for i in range(20)]
    assert _detect_tool_loop(calls, 60) == ""


def test_exploratory_run_python_varied():
    calls = [_mk("run_python", f"run_python:code{i}") for i in range(20)]
    assert _detect_tool_loop(calls, 60) == ""


def test_exploratory_heavy_near_step_limit():
    # 全是探索类工具、类型单一且接近步数上限，仍不应触发
    calls = [_mk("web_search", f"web_search:q{i}") for i in range(28)]
    assert _detect_tool_loop(calls, 60) == ""


# ── 非探索类工具：循环仍应被捕获 ──

def test_strict_repeat_non_exploratory():
    calls = [_mk("read_file", "read_file:/tmp/x") for _ in range(25)]
    reason = _detect_tool_loop(calls, 60)
    assert "严格重复" in reason


def test_ab_pattern_non_exploratory():
    calls = []
    for _ in range(6):
        calls.append(_mk("read_file", "read_file:/a"))
        calls.append(_mk("write_file", "write_file:/b"))
    reason = _detect_tool_loop(calls, 60)
    assert "循环模式" in reason


def test_step_limit_repetitive_non_exploratory():
    # 步数上限很低、且非探索类工具高度重复（<12 次以绕过检测2）
    calls = [_mk("read_file", f"read_file:/p{i % 3}") for i in range(8)]
    reason = _detect_tool_loop(calls, 10)
    assert "已接近最大推理步数" in reason


def test_exploratory_set_contains_requested():
    assert {"web_search", "run_shell", "run_python"} <= _EXPLORATORY_TOOLS


# ── current_steps：步数估算用真实图步数（问题6 回归） ──

def test_current_steps_drives_step_limit():
    # 过滤后只有很少的非探索类调用，但真实图步数已接近上限 → 仍应触发（修复前用过滤后 len 估算会漏报）
    calls = [_mk("read_file", "read_file:/p0"), _mk("read_file", "read_file:/p1"),
             _mk("read_file", "read_file:/p0"), _mk("read_file", "read_file:/p1")]
    reason = _detect_tool_loop(calls, 60, current_steps=58)
    assert "已接近最大推理步数" in reason


def test_current_steps_low_no_trigger():
    # 真实步数还很低 → 不触发
    calls = [_mk("read_file", "read_file:/p0") for _ in range(3)]
    assert _detect_tool_loop(calls, 60, current_steps=5) == ""


def test_current_steps_default_fallback():
    # 不传 current_steps 时退化为 len(calls)*2+1，原有行为不变
    calls = [_mk("read_file", "read_file:/p0") for _ in range(8)]
    assert "已接近最大推理步数" in _detect_tool_loop(calls, 10)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failed else 0)
