"""进化审计管理端 API 测试（DESIGN §4.7.3/4.7.6）：鉴权、过滤、状态流转。"""
import time

import pytest
from fastapi.testclient import TestClient

from agent_core.main import app  # noqa: F401  sys.path 注入
from agent_core.api.deps import _sign_session
from agent_core.evolution.audit_store import get_audit_store


def _cookie(username="admin"):
    return {"desktop_agent_session": _sign_session(username, int(time.time()) + 3600)}


@pytest.fixture(autouse=True)
def _clear_audit():
    """审计库是进程级单例，每个用例前清空，避免跨用例 id/计数累积。"""
    store = get_audit_store()
    with store._connect() as conn:
        conn.execute("DELETE FROM evolution_audit")
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def seeded():
    store = get_audit_store()
    ids = []
    ids.append(store.log(source="patrol", category="pitfall", severity="warn",
                         summary="ValueError 重复 3 次", outcome="pending"))
    ids.append(store.log(source="guardian", category="escalation", severity="fatal",
                         summary="主 app 不健康", outcome="escalated"))
    ids.append(store.log(source="patrol", category="quarantine", severity="error",
                         summary="已隔离坏技能 x", outcome="auto_fixed",
                         artifacts=["skills/.quarantine/x"]))
    return ids


def test_non_admin_forbidden(client, seeded):
    # 未登录 → 中间件拦截（302/401/403 都算拒绝，只要不是 200）
    r = client.get("/admin/evolution/audit")
    assert r.status_code != 200
    # 普通/未知用户 → 401（用户不存在）或 403（存在但非 admin），均为拒绝
    r = client.get("/admin/evolution/audit", cookies=_cookie("test"))
    assert r.status_code in (401, 403)


def test_admin_list_and_filter(client, seeded):
    r = client.get("/admin/evolution/audit?limit=50", cookies=_cookie("admin"))
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 3
    assert rows[0]["id"] > rows[-1]["id"]  # 新→旧

    r = client.get("/admin/evolution/audit?source=guardian", cookies=_cookie("admin"))
    assert len(r.json()) == 1 and r.json()[0]["source"] == "guardian"

    r = client.get("/admin/evolution/audit?outcome=escalated", cookies=_cookie("admin"))
    assert len(r.json()) == 1 and r.json()[0]["outcome"] == "escalated"


def test_get_detail_404(client, seeded):
    r = client.get("/admin/evolution/audit/999999", cookies=_cookie("admin"))
    assert r.status_code == 404
    r = client.get(f"/admin/evolution/audit/{seeded[0]}", cookies=_cookie("admin"))
    assert r.status_code == 200 and r.json()["id"] == seeded[0]


def test_health_counts(client, seeded):
    r = client.get("/admin/evolution/health", cookies=_cookie("admin"))
    assert r.status_code == 200
    c = r.json()
    assert c["total"] == 3
    assert c["open_escalations"] == 1
    assert c["auto_fixed"] == 1


def test_action_flow(client, seeded):
    aid = seeded[1]  # escalated
    r = client.post(f"/admin/evolution/audit/{aid}/action",
                    json={"action": "approve", "note": "已知问题"},
                    cookies=_cookie("admin"))
    assert r.status_code == 200 and r.json()["outcome"] == "approved"
    rec = client.get(f"/admin/evolution/audit/{aid}", cookies=_cookie("admin")).json()
    assert rec["outcome"] == "approved" and rec["actor"] == "admin"
    assert rec["detail"]["action_notes"][0]["note"] == "已知问题"

    # 非法 action
    r = client.post(f"/admin/evolution/audit/{aid}/action",
                    json={"action": "nope"}, cookies=_cookie("admin"))
    assert r.status_code == 400
    # 不存在的记录
    r = client.post("/admin/evolution/audit/999999/action",
                    json={"action": "ignore"}, cookies=_cookie("admin"))
    assert r.status_code == 404


def test_action_requires_admin(client, seeded):
    r = client.post(f"/admin/evolution/audit/{seeded[0]}/action",
                    json={"action": "ignore"}, cookies=_cookie("test"))
    assert r.status_code in (401, 403)


def test_artifacts_endpoint(client):
    r = client.get("/admin/evolution/artifacts", cookies=_cookie("admin"))
    assert r.status_code == 200
    body = r.json()
    assert body["quarantine_dir"].endswith(".quarantine")
    assert isinstance(body["items"], list)
