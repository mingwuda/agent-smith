"""list_projects 最近活跃排序测试"""
import sys
import tempfile
import time
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "agent_core"
sys.path.insert(0, str(ROOT))

# 隔离 user_data_dir，避免污染真实 DB
_tmp = tempfile.mkdtemp(prefix="test_projects_sort_")
os.environ.setdefault("USER_DATA_DIR", _tmp)

import session_store as ss


def _mk(uid, name, directory):
    p = ss.create_project(uid, name, directory)
    return p["id"]


def test_list_projects_sort_by_last_session_activity():
    """最近活跃的会话所在项目应排在最前。"""
    uid = "sort-test-user-1"
    pid_old = _mk(uid, "old", "/tmp/old")
    pid_mid = _mk(uid, "mid", "/tmp/mid")
    pid_new = _mk(uid, "new", "/tmp/new")

    ss.create_session(uid, "old-session", project_id=pid_old)
    time.sleep(0.05)
    ss.create_session(uid, "new-session", project_id=pid_new)
    time.sleep(0.05)
    ss.create_session(uid, "mid-session", project_id=pid_mid)

    projects = ss.list_projects(uid)
    names = [p["name"] for p in projects]
    assert names[0] == "mid", f"expected 'mid' first, got {names}"
    assert names[1] == "new", f"expected 'new' second, got {names}"
    assert names[2] == "old", f"expected 'old' last, got {names}"


def test_list_projects_no_session_falls_back_to_project_updated_at():
    """没有任何会话的项目，退化用项目自身 updated_at 排。"""
    uid = "sort-test-user-2"
    pid_a = _mk(uid, "alpha", "/tmp/alpha")
    time.sleep(0.05)
    pid_b = _mk(uid, "beta", "/tmp/beta")
    projects = ss.list_projects(uid)
    names = [p["name"] for p in projects]
    assert names[0] == "beta", f"expected 'beta' first, got {names}"
    assert names[1] == "alpha", f"expected 'alpha' second, got {names}"


def test_list_projects_returns_last_active_at_field():
    """每个项目都应有 last_active_at 字段（非 None）。"""
    uid = "sort-test-user-3"
    _mk(uid, "p1", "/tmp/p1")
    projects = ss.list_projects(uid)
    assert len(projects) == 1
    assert "last_active_at" in projects[0]
    assert projects[0]["last_active_at"] is not None


def test_list_projects_session_update_bumps_to_top():
    """更新已有会话（add_message）后，所属项目应升到最前。"""
    uid = "sort-test-user-4"
    pid_a = _mk(uid, "a", "/tmp/a")
    time.sleep(0.05)
    pid_b = _mk(uid, "b", "/tmp/b")
    ss.create_session(uid, "b-sess", project_id=pid_b)
    time.sleep(0.05)
    sess_a = ss.create_session(uid, "a-sess", project_id=pid_a)
    time.sleep(0.05)
    ss.add_message(uid, sess_a["id"], "user", "hello")

    projects = ss.list_projects(uid)
    names = [p["name"] for p in projects]
    assert names[0] == "a", f"expected 'a' first, got {names}"


def test_list_projects_user_isolation():
    """不同用户的项目互不可见。"""
    uid1 = "iso-user-1"
    uid2 = "iso-user-2"
    _mk(uid1, "u1-proj", "/tmp/u1")
    _mk(uid2, "u2-proj", "/tmp/u2")

    p1 = ss.list_projects(uid1)
    p2 = ss.list_projects(uid2)
    assert [p["name"] for p in p1] == ["u1-proj"]
    assert [p["name"] for p in p2] == ["u2-proj"]
