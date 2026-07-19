"""系统守护层 boot 自愈测试（纯 stdlib，不依赖 LLM / 真实 agent）。"""
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_core.guardian import (  # noqa: E402
    self_heal_on_boot,
    validate_artifact,
    record_evolution,
)


def _make_manifest(manifest_path: Path, entries):
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({"entries": entries}), encoding="utf-8")


def test_heal_rolls_back_latest_evolution(tmp_path):
    """坏进化产物导致首次启动失败 → 被隔离 → 重试成功；manifest 清空、boot_ok 写出。"""
    generated = tmp_path / "skills" / ".generated"
    generated.mkdir(parents=True)
    quarantine = tmp_path / "skills" / ".quarantine"
    manifest = generated / "manifest.json"
    boot_ok = tmp_path / ".boot_ok"

    bad = generated / "bad_skill"
    bad.write_text("garbage", encoding="utf-8")
    _make_manifest(manifest, [{"kind": "skill", "path": str(bad), "created_at": 1}])

    calls = {"n": 0}

    def boot_fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated bad skill crash")
        # 第二次：坏产物已被隔离，启动成功

    ok = self_heal_on_boot(
        boot_fn,
        generated_dir=generated,
        manifest_path=manifest,
        quarantine_dir=quarantine,
        boot_ok_marker=boot_ok,
    )
    assert ok is True
    assert calls["n"] == 2
    # 坏产物进了隔离区（不删除）
    assert bad.exists() is False
    assert list(quarantine.glob("*_bad_skill")), "bad skill should be quarantined"
    # manifest 已清空
    assert json.loads(manifest.read_text())["entries"] == []
    # boot_ok 标记已写
    assert boot_ok.exists()


def test_heal_falls_through_to_full_reset(tmp_path):
    """manifest 空、但 generated_dir 本身有脏产物 → 整体隔离后再试一次。"""
    generated = tmp_path / "skills" / ".generated"
    generated.mkdir(parents=True)
    (generated / "junk.txt").write_text("x", encoding="utf-8")
    quarantine = tmp_path / "skills" / ".quarantine"
    manifest = generated / "manifest.json"
    boot_ok = tmp_path / ".boot_ok"

    calls = {"n": 0}

    def boot_fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("dir-level failure")
        # 第二次：整体隔离 generated 后成功

    ok = self_heal_on_boot(
        boot_fn,
        generated_dir=generated,
        manifest_path=manifest,
        quarantine_dir=quarantine,
        boot_ok_marker=boot_ok,
    )
    assert ok is True
    assert calls["n"] == 2
    # generated_dir 被整体隔离
    assert generated.exists() is False
    assert list(quarantine.glob("*_.generated")), "generated_dir should be quarantined"


def test_heal_full_failure_returns_false(tmp_path):
    """始终无法启动 → 返回 False，且不写 boot_ok 标记。"""
    generated = tmp_path / "skills" / ".generated"
    generated.mkdir(parents=True)
    quarantine = tmp_path / "skills" / ".quarantine"
    manifest = generated / "manifest.json"
    boot_ok = tmp_path / ".boot_ok"

    def boot_fn():
        raise RuntimeError("always broken")

    ok = self_heal_on_boot(
        boot_fn,
        generated_dir=generated,
        manifest_path=manifest,
        quarantine_dir=quarantine,
        boot_ok_marker=boot_ok,
    )
    assert ok is False
    assert boot_ok.exists() is False  # 未恢复则不写标记


def test_validate_skill_frontmatter():
    good = "---\nname: foo\ndescription: bar\n---\n# 指令\n做点事"
    assert validate_artifact("skill", good) is True
    bad = "没有frontmatter\n# 指令\n做点事"
    assert validate_artifact("skill", bad) is False


def test_validate_config_patch_unknown_field_rejected():
    allowed = {"model", "workspace"}
    assert validate_artifact("config_patch", json.dumps({"model": "x"}),
                             allowed_config_keys=allowed) is True
    assert validate_artifact("config_patch", json.dumps({"unknown_field": 1}),
                             allowed_config_keys=allowed) is False
    # 未声明允许集合 → 默认拒绝
    assert validate_artifact("config_patch", json.dumps({"model": "x"})) is False


def test_record_evolution_appends_lifo(tmp_path):
    manifest = tmp_path / "manifest.json"
    record_evolution(kind="skill", path="/a", manifest_path=manifest)
    record_evolution(kind="skill", path="/b", manifest_path=manifest)
    entries = json.loads(manifest.read_text())["entries"]
    assert [e["path"] for e in entries] == ["/a", "/b"]
