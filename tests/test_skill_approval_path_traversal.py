"""技能审批接口的路径穿越守卫（2026-09-29 安全修复）。

背景（线上可复现的严重缺陷）：
    `case_forge.reject_skill` 把请求体的 `skill_name` 直接拼成
    `Path(skills_dir)/"pending"/skill_name` 后 `shutil.rmtree`，且
    `approve_skill` 同理会写到 `skills_dir` 之外。实测：
      - skill_name=".."    → 目标解析为 skills_dir 本身 → **清空整个技能目录**
      - skill_name="../.." → 生产（skills_dir=/opt/desktop-agent/agent_core/samples）
                             → **清空 /opt/desktop-agent/agent_core 整棵源码树**
    注意顶层目录名会残留（rmdir 对末段为 `..` 返回 EINVAL），极易被误判成"没事"。
    该接口当时还缺 `_require_admin`，任意登录用户即可触发。

本测试同时钉住「修完之后正常审批流程仍然可用」，避免一刀切把功能打死。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_core"))

import pytest

import user_manager
from memory.local_memory import _memories, get_memory
from case_forge import (
    approve_skill, reject_skill, _checked_skill_name, _within,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(user_manager, "USERS_DIR", tmp_path / "users")
    _memories.clear()
    return tmp_path


def _make_skills_dir(base: Path) -> Path:
    """造一个带待审批候选 + 一个已生效技能的技能目录。"""
    skills = base / "agent_skills"
    (skills / "pending" / "cand-a").mkdir(parents=True)
    (skills / "pending" / "cand-a" / "SKILL.md").write_text("candidate", encoding="utf-8")
    (skills / "critical-skill").mkdir(parents=True)
    (skills / "critical-skill" / "SKILL.md").write_text("CRITICAL", encoding="utf-8")
    return skills


def _snapshot(root: Path) -> set:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


# ---------- 恶意输入必须被拒绝，且不得动任何文件 ----------

@pytest.mark.parametrize("evil", ["..", "../..", "../../..", "/etc", "a/b", ".hidden", ""])
def test_reject_skill_rejects_malicious_name(isolated, evil):
    skills = _make_skills_dir(isolated)
    before = _snapshot(skills)
    msg = reject_skill("u1", evil, skills)
    assert "非法" in msg, msg
    assert skills.exists(), "技能目录本身被删了！"
    assert _snapshot(skills) == before, f"技能目录内容被改动: {msg}"


@pytest.mark.parametrize("evil", ["..", "../..", "../../../tmp", "/etc", "a/b"])
def test_approve_skill_rejects_malicious_name(isolated, evil):
    skills = _make_skills_dir(isolated)
    before = _snapshot(skills)
    msg = approve_skill("u1", evil, skills)
    assert "非法" in msg, msg
    assert _snapshot(skills) == before
    # 绝不能把 SKILL.md 写到技能目录之外
    assert not (isolated / "SKILL.md").exists()
    assert not list(isolated.glob("*/SKILL.md"))


def test_reject_does_not_delete_skills_dir_itself(isolated):
    """最危险的一击：skill_name='..' 曾能递归清空整个技能目录。"""
    skills = _make_skills_dir(isolated)
    reject_skill("u1", "..", skills)
    assert (skills / "critical-skill" / "SKILL.md").read_text(encoding="utf-8") == "CRITICAL"
    assert (skills / "pending" / "cand-a" / "SKILL.md").exists()


def test_reject_does_not_escape_one_level_up(isolated):
    """'../..' 曾能清空上层目录（含兄弟目录）。"""
    skills = _make_skills_dir(isolated)
    (isolated / "user_db").mkdir()
    (isolated / "user_db" / "sessions.sqlite3").write_text("data", encoding="utf-8")
    reject_skill("u1", "../..", skills)
    assert (isolated / "user_db" / "sessions.sqlite3").exists(), "兄弟目录被删了！"
    assert (skills / "critical-skill" / "SKILL.md").exists()


# ---------- 正向流程不能被一刀切打死 ----------

def test_approve_legit_candidate_works(isolated):
    skills = _make_skills_dir(isolated)
    msg = approve_skill("u1", "cand-a", skills)
    assert "已生效" in msg, msg
    assert (skills / "cand-a" / "SKILL.md").exists()
    assert not (skills / "pending" / "cand-a").exists()
    assert get_memory("u1").get("_skill_cand-a")["status"] == "active"


def test_reject_legit_candidate_works(isolated):
    skills = _make_skills_dir(isolated)
    get_memory("u1").set("_skill_cand-a", {"status": "pending", "skill_name": "cand-a"})
    msg = reject_skill("u1", "cand-a", skills)
    assert "已舍弃" in msg, msg
    assert not (skills / "pending" / "cand-a").exists()
    assert get_memory("u1").get("_skill_cand-a") is None
    # 无关技能不受影响
    assert (skills / "critical-skill" / "SKILL.md").exists()


def test_approve_missing_candidate_reports_not_found(isolated):
    skills = _make_skills_dir(isolated)
    msg = approve_skill("u1", "no-such-skill", skills)
    assert "未找到" in msg, msg


# ---------- 校验函数本身 ----------

@pytest.mark.parametrize("good", ["cand-a", "case-skill", "a1", "x.y", "a_b-c"])
def test_checked_skill_name_accepts_legit(good):
    assert _checked_skill_name(good) == good


@pytest.mark.parametrize("bad", ["", "   ", "..", ".", ".hidden", "a/b", "a\\b",
                                 "/abs", "../x", "A-Upper", "a" * 65, "café"])
def test_checked_skill_name_rejects_bad(bad):
    assert _checked_skill_name(bad) is None


def test_within_helper():
    root = Path("/tmp/skillroot")
    assert _within(root / "pending" / "x", root) is True
    assert _within(root, root) is True
    assert _within(root / "..", root.parent) is True  # 只是规范化，调用方需另判 != root
    assert _within(Path("/etc"), root) is False


# ---------- 源码级守卫：权限校验不得被顺手删掉 ----------

def test_pending_endpoints_require_admin():
    src = (ROOT / "agent_core" / "api" / "routes" / "skills.py").read_text(encoding="utf-8")
    for fn in ("def approve_pending_skill", "def reject_pending_skill"):
        i = src.index(fn)
        body = src[i:i + 1200]
        assert "_require_admin(request)" in body, (
            f"{fn} 缺少 _require_admin 校验（审批会全局改变技能集，必须限管理员）"
        )
