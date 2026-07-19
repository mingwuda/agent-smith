"""守护进程（控制面）单元测试：回退原语、健康探测、巡检恢复编排。

网络无关：用 monkeypatch 替代 HTTP 探针与重启命令（ponytail：非平凡逻辑留一个可跑的 check）。
"""
import json
import sys
from pathlib import Path

# 注入 agent_core 到 sys.path（不 import 重型 main 模块）
_AGENT_CORE = Path(__file__).resolve().parent.parent / "agent_core"
if str(_AGENT_CORE) not in sys.path:
    sys.path.insert(0, str(_AGENT_CORE))

from guardian import roll_back_latest  # noqa: E402
import agent_core.guardian_daemon as gd  # noqa: E402  (其自身会 bootstrap 路径)
from config import AgentConfig  # noqa: E402


def _make_cfg(self_healing: bool) -> AgentConfig:
    cfg = AgentConfig()
    cfg.enable_self_healing = self_healing
    return cfg


def _write_manifest(manifest_path: Path, entries: list) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({"entries": entries}), encoding="utf-8")


# ---------- 回退原语 ----------

def test_roll_back_latest_lifo(tmp_path):
    gen = tmp_path / "gen"
    gen.mkdir()
    q = tmp_path / "q"
    mk = gen / "manifest.json"
    f1 = gen / "skill_a"
    f1.write_text("a")
    f2 = gen / "skill_b"
    f2.write_text("b")
    _write_manifest(mk, [
        {"kind": "skill", "path": str(f1), "note": "a"},
        {"kind": "skill", "path": str(f2), "note": "b"},
    ])
    moved = roll_back_latest(mk, q)
    assert moved is not None
    assert not f2.exists(), "最近登记(b)应被 LIFO 回退"
    assert f1.exists(), "较早登记(a)应保留"
    remaining = json.loads(mk.read_text())["entries"]
    assert len(remaining) == 1 and remaining[0]["note"] == "a"


def test_roll_back_latest_empty(tmp_path):
    mk = tmp_path / "manifest.json"
    mk.write_text(json.dumps({"entries": []}))
    assert roll_back_latest(mk, tmp_path / "q") is None


# ---------- 健康探测 ----------

def test_check_health_unreachable(monkeypatch):
    monkeypatch.setattr(gd, "_http_get_json", lambda url, timeout=5: None)
    assert gd.check_health("http://x/health") is None


def test_check_health_parses(monkeypatch):
    monkeypatch.setattr(gd, "_http_get_json",
                        lambda url, timeout=5: {"agent_ready": True, "boot_ok": True})
    assert gd.check_health("http://x/health")["agent_ready"] is True


# ---------- 单轮巡检：恢复编排 ----------

def _patrol_paths(tmp_path, notes):
    gen = tmp_path / "gen"
    gen.mkdir()
    q = tmp_path / "q"
    mk = gen / "manifest.json"
    bad = gen / "bad_skill"
    bad.write_text("garbage")
    entries = [{"kind": "skill", "path": str(bad), "note": n} for n in notes]
    _write_manifest(mk, entries)
    return {
        "generated_dir": gen,
        "manifest_path": mk,
        "quarantine_dir": q,
        "boot_ok_marker": tmp_path / ".boot_ok",
        "log_file": tmp_path / "agent.log",
    }, bad


def test_run_patrol_recovers_when_enabled(tmp_path, monkeypatch):
    cfg = _make_cfg(True)
    paths, bad = _patrol_paths(tmp_path, ["bad"])
    monkeypatch.setattr(gd, "check_health",
                        lambda url, timeout=5: {"agent_ready": False, "boot_ok": False})
    restarted = []
    monkeypatch.setattr(gd, "trigger_restart", lambda cmd: restarted.append(cmd))

    summary = gd.run_patrol(cfg, paths, "http://x/health", "systemctl restart desktop-agent")

    assert summary["acted"] is True
    assert not bad.exists(), "不健康且开启自愈 → 坏产物应被回退进隔离区"
    assert len(restarted) == 1, "应触发重启"
    assert json.loads(paths["manifest_path"].read_text())["entries"] == []


def test_run_patrol_observe_only_when_disabled(tmp_path, monkeypatch):
    cfg = _make_cfg(False)
    paths, bad = _patrol_paths(tmp_path, ["bad"])
    monkeypatch.setattr(gd, "check_health",
                        lambda url, timeout=5: {"agent_ready": False, "boot_ok": False})
    restarted = []
    monkeypatch.setattr(gd, "trigger_restart", lambda cmd: restarted.append(cmd))

    summary = gd.run_patrol(cfg, paths, "http://x/health", "systemctl restart desktop-agent")

    assert summary["acted"] is False, "观察模式不应执行任何动作"
    assert bad.exists(), "观察模式不应改动任何文件"
    assert len(restarted) == 0
    assert len(json.loads(paths["manifest_path"].read_text())["entries"]) == 1
