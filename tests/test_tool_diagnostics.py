"""工具诊断日志测试：阈值过滤 / 截断 / 轮转 / JSON 可解析。

conftest 已把 HOME 重定向到临时目录，DIAG_LOG_PATH 落在临时路径下，不会污染真实日志。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_core"))

from monitoring import tool_diagnostics as td  # noqa: E402


def _clear(monkeypatch, tmp_path):
    """把诊断日志重定向到临时文件并清空旧文件。"""
    target = tmp_path / "tool_diagnostics.log"
    monkeypatch.setattr(td, "DIAG_LOG_PATH", target)
    target.unlink(missing_ok=True)
    return target


def _read_lines(path):
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_normal_fast_call_not_recorded(monkeypatch, tmp_path):
    target = _clear(monkeypatch, tmp_path)
    assert td.log_tool_event("read_file", 0.5, False, {"path": "a.py"}) is False
    assert not target.exists()


def test_slow_call_recorded(monkeypatch, tmp_path):
    target = _clear(monkeypatch, tmp_path)
    assert td.log_tool_event("run_shell", 45.0, False, {"command": "pytest"}) is True
    rec = _read_lines(target)[0]
    assert rec["tool"] == "run_shell"
    assert rec["slow"] is True and rec["error"] is False
    assert rec["duration_ms"] == 45000


def test_error_call_recorded_even_if_fast(monkeypatch, tmp_path):
    target = _clear(monkeypatch, tmp_path)
    assert td.log_tool_event("run_shell", 0.3, True, {"command": "rm x"}, result_preview="❌ 文件不存在") is True
    rec = _read_lines(target)[0]
    assert rec["error"] is True and rec["slow"] is False
    assert rec["result"] == "❌ 文件不存在"


def test_threshold_override(monkeypatch, tmp_path):
    target = _clear(monkeypatch, tmp_path)
    assert td.log_tool_event("read_file", 5.0, False, {}, slow_threshold_s=3.0) is True
    assert td.log_tool_event("read_file", 1.0, False, {}, slow_threshold_s=3.0) is False
    assert len(_read_lines(target)) == 1


def test_args_truncated_and_internal_keys_skipped(monkeypatch, tmp_path):
    target = _clear(monkeypatch, tmp_path)
    long_val = "x" * 1000
    td.log_tool_event("write_file", 0.1, True, {"path": "a.py", "_secret": "hide", "content": long_val})
    rec = _read_lines(target)[0]
    assert "_secret" not in rec["args"]
    assert len(rec["args"]["content"]) <= 205  # 200 + "..." 后缀
    assert rec["args"]["content"].endswith("...")


def test_rollover_keeps_one_old_copy(monkeypatch, tmp_path):
    target = _clear(monkeypatch, tmp_path)
    monkeypatch.setattr(td, "MAX_BYTES", 300)
    for i in range(50):  # 50 行必然撑爆 300B，触发轮转
        td.log_tool_event(f"tool{i}", 45.0, False, {})
    old = target.with_suffix(target.suffix + ".old")
    assert old.exists(), "超限后应保留一份 .old 历史"
    assert len(_read_lines(old)) > 0
