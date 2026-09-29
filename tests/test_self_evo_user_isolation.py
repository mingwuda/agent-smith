"""自进化链路的「用户隔离」与「skills 目录解析」守卫（2026-09-29 修复）。

M4（跨用户串写）：
    反思/蒸馏跑在 `asyncio.create_task(_async_reflect(...))` 的后台任务里，
    但 `maybe_generate_skill` 用的是**全局单例 agent** 上的可变字段
    （`self._user_id` / 并发的 `_last_user_message` / `_last_tool_steps`）。
    并发请求下 set_user() 一旦覆盖，Case 与 _skill_ 指针就会写进**别的用户**的记忆。
    → 现在 uid / user_message / tool_steps 一律由调用方显式传入。

M5（skills_dir 兜底失效）：
    `skills_dir = Path(getattr(config, "skills_dir", ""))` 后判真值 —— `Path("") == Path(".")`
    恒为真、`.exists()` 也恒为真 → 兜底分支永不触发，蒸馏产物会写进**进程 CWD**；
    且兜底名 `sample_skills` 与实际目录 `samples` 不符。
    → 现在统一走 `AgentConfig.skills_root()`（唯一解析入口）。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_core"))

import pytest

import user_manager
from memory.local_memory import _memories, get_memory
from config import AgentConfig, _bundled_samples_dir
from agent_chat import AgentChatMixin

ROOT = Path(__file__).resolve().parents[1]
PATTERN = {"t": "technique", "v": "git 冲突|先看冲突文件再逐个手动合并"}


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(user_manager, "USERS_DIR", tmp_path / "users")
    _memories.clear()
    return tmp_path


class _StubSelf:
    """只提供 maybe_generate_skill 真正用到的 self.config。"""

    def __init__(self, config, singleton_user_id="alice"):
        self.config = config
        # 模拟"单例上残留的、已被并发请求覆盖"的当前用户
        self._user_id = singleton_user_id


def _cfg(tmp_path: Path) -> AgentConfig:
    skills = tmp_path / "skills"
    skills.mkdir(exist_ok=True)
    return AgentConfig(skills_dir=str(skills), enable_self_evolution=True)


def _cases(uid):
    return [i for i in get_memory(uid).list_items() if i["key"].startswith("_case_")]


# ---------- M4：必须写进传入的 uid ----------

def test_writes_case_to_passed_uid_not_singleton(isolated):
    cfg = _cfg(isolated)
    stub = _StubSelf(cfg, singleton_user_id="alice")
    ret = AgentChatMixin.maybe_generate_skill(
        stub, "bob", dict(PATTERN),
        user_message="帮我合并 git 冲突", tool_steps=[{"tool": "git_status"}],
    )
    assert ret == "case-accumulated", ret
    assert _cases("bob"), "Case 应写进传入的 uid(bob)"
    assert not _cases("alice"), "绝不应写进单例上的 _user_id(alice)（并发会串用户）"


def test_distilled_candidate_lands_in_resolved_skills_dir(isolated):
    """累积到阈值后，候选 SKILL.md 必须落在 skills_root() 指定的目录里。"""
    cfg = _cfg(isolated)
    stub = _StubSelf(cfg)
    for _ in range(3):  # CASE_PROMOTE_THRESHOLD = 3
        AgentChatMixin.maybe_generate_skill(
            stub, "bob", dict(PATTERN),
            user_message="帮我合并 git 冲突", tool_steps=[{"tool": "git_status"}],
        )
    pending = cfg.skills_root() / "pending"
    drafts = list(pending.glob("*/SKILL.md"))
    assert drafts, f"未在 {pending} 下起草候选技能"
    # 且不得污染进程 CWD
    assert not (Path.cwd() / "pending").exists() or str(cfg.skills_root()) != str(Path.cwd())


def test_no_uid_is_noop(isolated):
    cfg = _cfg(isolated)
    assert AgentChatMixin.maybe_generate_skill(_StubSelf(cfg), "", dict(PATTERN)) is None


def _strip_docstring(s: str) -> str:
    """剥掉首个三引号 docstring —— 注释里会引用旧写法做说明，不能算违规。"""
    i = s.find('"""')
    if i < 0:
        return s
    j = s.find('"""', i + 3)
    return (s[:i] + s[j + 3:]) if j > 0 else s


def test_source_no_longer_reads_singleton_user_id():
    """源码级守卫：该方法不得再读 self._user_id / _last_*（防回归）。"""
    src = (ROOT / "agent_core" / "agent_chat.py").read_text(encoding="utf-8")
    i = src.index("def maybe_generate_skill")
    body = _strip_docstring(src[i:src.index("\n    def ", i + 10)])
    for banned in ("self._user_id", "_last_user_message", "_last_tool_steps", "sample_skills"):
        assert banned not in body, f"maybe_generate_skill 仍在用 {banned}（并发下会串用户）"
    assert "def maybe_generate_skill(self, uid" in src


# ---------- M5：skills_root 是唯一解析入口 ----------

def test_skills_root_empty_falls_back_to_bundled_samples():
    root = AgentConfig(skills_dir="").skills_root()
    assert root == _bundled_samples_dir()
    assert root != Path("."), "空配置被解析成了进程 CWD"
    assert root.is_absolute()


def test_skills_root_handles_blank_and_multipath():
    assert AgentConfig(skills_dir="   ").skills_root() == _bundled_samples_dir()
    assert str(AgentConfig(skills_dir=f"/tmp/a{os.pathsep}/tmp/b").skills_root()) == "/tmp/a"
    assert str(AgentConfig(skills_dir="/tmp/only").skills_root()) == "/tmp/only"


def test_skills_dir_is_normalized_by_load():
    """load() 会把空 skills_dir 填成内置 samples，这也是旧 bug 未爆发的原因。"""
    assert AgentConfig(skills_dir="").skills_root().name == "samples"


def test_routes_use_canonical_resolver():
    """路由侧不得再出现 `Path(config.skills_dir)` 这种真值判断。"""
    src = (ROOT / "agent_core" / "api" / "routes" / "skills.py").read_text(encoding="utf-8")
    i = src.index("def _resolve_skills_dir")
    body = _strip_docstring(src[i:i + 900])
    assert "skills_root()" in body
    assert "base = Path(config.skills_dir)" not in body
