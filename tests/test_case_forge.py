"""Case → Skill 离线蒸馏（半自动）单元测试。

覆盖：Case 累积、阈值晋升、候选 SKILL.md 起草、审批生效、拒绝丢弃、去重。
纯本地逻辑，不依赖网络/真实 LLM。
"""
import sys
from pathlib import Path

# 与 agent_core/main.py 一致的 sys.path 注入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_core"))

import pytest

import user_manager
from memory.local_memory import _memories
from case_forge import (
    accumulate_case, find_promotable_cases, draft_skill,
    approve_skill, reject_skill, list_pending_skills, CASE_PROMOTE_THRESHOLD,
)


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """每个测试独立用户目录 + 清理 get_memory 缓存。"""
    monkeypatch.setattr(user_manager, "USERS_DIR", tmp_path / "users")
    _memories.clear()
    return tmp_path


TECH = {"t": "technique", "v": "git 冲突|先看冲突文件再逐个手动合并"}
ACTIONS = [{"tool": "git_status"}, {"tool": "git_show"}, {"tool": "edit_file"}]


def _fill_case(uid, n=CASE_PROMOTE_THRESHOLD, text=None, v="git 冲突|逐文件手动合并"):
    for i in range(n):
        accumulate_case(uid, f"第{i}次提问围观冲突处理",
                        {"t": "technique", "v": v}, actions=ACTIONS)


def test_accumulate_case_aggregates_occurrences(isolated):
    uid = "u1"
    _fill_case(uid, 2)
    from memory.local_memory import get_memory
    mem = get_memory(uid)
    cases = [it for it in mem.list_items() if it["key"].startswith("_case_")]
    assert len(cases) == 1, "同类 technique 应聚合成一个 Case"
    assert cases[0]["value"]["occurrences"] == 2, "出现次数应累加"


def test_not_promotable_below_threshold(isolated):
    uid = "u1"
    _fill_case(uid, CASE_PROMOTE_THRESHOLD - 1)
    assert find_promotable_cases(uid) == [], "未达阈值不应晋升"


def test_promotable_at_threshold_drafts_pending_skill(isolated):
    uid = "u1"
    _fill_case(uid, CASE_PROMOTE_THRESHOLD)
    prom = find_promotable_cases(uid)
    assert len(prom) == 1
    skills_dir = isolated / "skills"
    sr = draft_skill(uid, prom[0]["key"], skills_dir)
    assert sr is not None and not sr["skipped"]
    md = Path(sr["pending_path"])
    assert md.exists(), "候选 SKILL.md 应写入待审批目录"
    content = md.read_text(encoding="utf-8")
    assert "## 技能：" in content and "## 指令" in content
    assert "git_status" in content, "指令应包含工具动作序列"
    assert "待用户确认后生效" in content, "候选应标记为待确认（半自动）"
    # 未审批前不应在技能根目录
    assert not (skills_dir / sr["skill_name"] / "SKILL.md").exists(), "候选未批准不应直接生效"


def test_approve_moves_skill_into_active(isolated):
    uid = "u1"
    _fill_case(uid, CASE_PROMOTE_THRESHOLD)
    prom = find_promotable_cases(uid)
    skills_dir = isolated / "skills"
    sr = draft_skill(uid, prom[0]["key"], skills_dir)
    assert approve_skill(uid, sr["skill_name"], skills_dir).startswith("✅")
    active = skills_dir / sr["skill_name"] / "SKILL.md"
    assert active.exists(), "审批后技能应进入生效目录"
    pending = skills_dir / "pending" / sr["skill_name"]
    assert not pending.exists(), "审批后待审批目录应清理"
    # 生效后不应再重复起草
    assert draft_skill(uid, prom[0]["key"], skills_dir) is None


def test_reject_discards_candidate(isolated):
    uid = "u1"
    _fill_case(uid, CASE_PROMOTE_THRESHOLD)
    prom = find_promotable_cases(uid)
    skills_dir = isolated / "skills"
    sr = draft_skill(uid, prom[0]["key"], skills_dir)
    assert reject_skill(uid, sr["skill_name"], skills_dir).startswith("🗑")
    assert not (skills_dir / "pending" / sr["skill_name"] / "SKILL.md").exists(), "拒绝后候选应删除"
    assert list_pending_skills(uid, skills_dir) == [], "拒绝后不再有待确认候选"


def test_list_pending_skills_shows_candidates(isolated):
    uid = "u1"
    _fill_case(uid, CASE_PROMOTE_THRESHOLD)
    prom = find_promotable_cases(uid)
    skills_dir = isolated / "skills"
    draft_skill(uid, prom[0]["key"], skills_dir)
    pending = list_pending_skills(uid, skills_dir)
    assert len(pending) == 1 and pending[0]["skill_name"]
    assert pending[0]["occurrences"] >= CASE_PROMOTE_THRESHOLD


def test_non_technique_not_accumulated(isolated):
    """非 technique（pitfall/preference）不参与 Case 蒸馏。"""
    uid = "u1"
    accumulate_case(uid, "别踩", {"t": "pitfall", "v": "不要 X"}, actions=[])
    accumulate_case(uid, "偏好", {"t": "preference", "v": "用中文"}, actions=[])
    from memory.local_memory import get_memory
    mem = get_memory(uid)
    cases = [it for it in mem.list_items() if it["key"].startswith("_case_")]
    assert cases == [], "pitfall/preference 不应生成 Case"


def test_markdown_mirror_written_for_case(isolated):
    """需求3：Case 写入时同步生成可读 .md 镜像。"""
    uid = "u1"
    _fill_case(uid, 1)
    from memory.local_memory import get_memory
    mem = get_memory(uid)
    cases = [it for it in mem.list_items() if it["key"].startswith("_case_")]
    md = mem.data_dir / f"{cases[0]['key']}.md"
    assert md.exists(), "应生成 Markdown 可读镜像"
    assert "occurrences" in md.read_text(encoding="utf-8")