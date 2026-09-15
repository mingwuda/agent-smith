"""EvolutionAuditStore 单元测试：写入校验、过滤分页、状态流转、汇总计数。"""
import pytest

from agent_core.evolution.audit_store import EvolutionAuditStore


@pytest.fixture
def store(tmp_path):
    return EvolutionAuditStore(db_path=tmp_path / "audit.sqlite3")


def _seed(store, n, **over):
    ids = []
    for i in range(n):
        ids.append(store.log(
            source=over.get("source", "patrol"),
            category=over.get("category", "fix"),
            summary=f"事件 {i}",
            severity=over.get("severity", "warn"),
            detail={"i": i},
            artifacts=[f"skills/.quarantine/skill{i}/SKILL.md"],
            outcome=over.get("outcome", "auto_fixed"),
        ))
    return ids


def test_log_and_get(store):
    aid = store.log(source="patrol", category="quarantine",
                    summary="隔离坏技能 foo", severity="error",
                    detail={"root": "ImportError"}, artifacts=["skills/.quarantine/foo.md"],
                    outcome="auto_fixed")
    rec = store.get_one(aid)
    assert rec["id"] == aid
    assert rec["source"] == "patrol"
    assert rec["category"] == "quarantine"
    assert rec["detail"] == {"root": "ImportError"}
    assert rec["artifacts"] == ["skills/.quarantine/foo.md"]
    assert rec["outcome"] == "auto_fixed"
    assert rec["actor"] == "auto"


def test_invalid_enum_rejected(store):
    with pytest.raises(ValueError):
        store.log(source="nope", category="fix", summary="x")
    with pytest.raises(ValueError):
        store.log(source="patrol", category="nope", summary="x")
    with pytest.raises(ValueError):
        store.log(source="patrol", category="fix", summary="x", severity="nope")


def test_list_filter_and_order(store):
    _seed(store, 3, source="patrol", category="fix", outcome="auto_fixed")
    store.log(source="guardian", category="escalation", summary="升级人工",
              outcome="escalated", severity="error")
    all_rows = store.list_audit(limit=50)
    assert len(all_rows) == 4
    assert all_rows[0]["id"] > all_rows[-1]["id"]  # 新→旧

    only_guard = store.list_audit(source="guardian")
    assert len(only_guard) == 1 and only_guard[0]["category"] == "escalation"

    escalated = store.list_audit(outcome="escalated")
    assert len(escalated) == 1


def test_pagination(store):
    _seed(store, 5)
    page1 = store.list_audit(limit=2, offset=0)
    page2 = store.list_audit(limit=2, offset=2)
    assert len(page1) == 2 and len(page2) == 2
    ids = [r["id"] for r in page1 + page2]
    assert len(set(ids)) == 4  # 两页不重叠
    # limit 上限 200
    assert store.list_audit(limit=99999).__len__() == 5


def test_set_outcome_flow(store):
    aid = _seed(store, 1, outcome="escalated")[0]
    assert store.set_outcome(aid, "approved", actor="admin", note="确认修复有效")
    rec = store.get_one(aid)
    assert rec["outcome"] == "approved"
    assert rec["actor"] == "admin"
    assert rec["detail"]["action_notes"][0]["note"] == "确认修复有效"
    # 不存在的 id
    assert store.set_outcome(99999, "ignored") is False
    # 非法 outcome
    with pytest.raises(ValueError):
        store.set_outcome(aid, "nope")


def test_health_counts(store):
    _seed(store, 2, outcome="auto_fixed", category="fix")
    store.log(source="guardian", category="escalation", summary="待处理",
              outcome="escalated", severity="error")
    store.log(source="patrol", category="pitfall", summary="坑", outcome="pending")
    c = store.health_counts()
    assert c["total"] == 4
    assert c["auto_fixed"] == 2
    assert c["open_escalations"] == 1
    assert c["pending"] == 1
    assert c["pitfalls_24h"] == 1


def test_since_filter(store):
    _seed(store, 1)
    future = "2999-01-01T00:00:00"
    assert store.list_audit(since=future) == []
    assert len(store.list_audit(since="2000-01-01T00:00:00")) == 1
