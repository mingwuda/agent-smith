"""patrol 巡检分析器与 healers 测试（DESIGN §4.6.3/4.6.4/4.6.7）。

不依赖网络/真实 LLM：Stage2 用注入的假 llm；文件动作用 tmp_path 隔离。
"""
from types import SimpleNamespace

import pytest

from agent_core.main import app  # noqa: F401  触发 agent_core sys.path 注入
from agent_core.evolution.audit_store import EvolutionAuditStore
from agent_core import patrol

_HEALTHY = {"boot_ok": True, "agent_ready": True}


def _cfg(**kw):
    base = dict(enable_self_healing=False, review_provider_id="", review_model="",
                providers={})
    base.update(kw)
    return SimpleNamespace(**base)


def _paths(tmp_path):
    gen = tmp_path / "generated"
    qua = tmp_path / "quarantine"
    gen.mkdir()
    qua.mkdir()
    return {"generated_dir": gen, "quarantine_dir": qua}


TRACEBACK_LINES = [
    "2026-09-15 10:00:01 ERROR Traceback (most recent call last):",
    '  File "x.py", line 1, in <module>',
    "ValueError: bad value",
    "2026-09-15 10:00:02 ERROR ValueError: bad value",
    "2026-09-15 10:00:03 ERROR ValueError: bad value",
]


# ---------- Stage 1 ----------

def test_stage1_clusters_repeated_exception():
    findings = patrol.analyze_stage1(TRACEBACK_LINES, health=_HEALTHY)
    assert len(findings) == 1
    f = findings[0]
    assert f["category"] == "log_error"
    assert "ValueError" in f["symptom"] and "3" in f["symptom"]
    assert f["severity"] == "error"
    assert len(f["evidence"]) >= 1
    # 无 .generated 线索时只能升级人工
    assert f["candidate_healers"] == ["escalate_to_human"]
    assert f["skill_name"] is None


def test_stage1_below_threshold_no_finding():
    findings = patrol.analyze_stage1(TRACEBACK_LINES[:2], health=_HEALTHY)  # 只出现 1 次 ValueError
    assert findings == []


def test_stage1_error_spike_without_cluster():
    # 10 行 CRITICAL 但无重复异常类型 → 总量尖刺 finding
    lines = [f"CRITICAL something broke #{i}" for i in range(10)]
    findings = patrol.analyze_stage1(lines, health=_HEALTHY)
    assert len(findings) == 1
    assert findings[0]["severity"] == "warn"


def test_stage1_generated_skill_route():
    lines = TRACEBACK_LINES + [
        "ImportError: failed loading skills/.generated/my-skill/SKILL.md",
    ] * 3
    findings = patrol.analyze_stage1(lines, health=_HEALTHY)
    skill_findings = [f for f in findings if f["skill_name"]]
    assert skill_findings, findings
    f = skill_findings[0]
    assert f["skill_name"] == "my-skill"
    assert "quarantine_bad_skill" in f["candidate_healers"]


def test_stage1_unhealthy():
    findings = patrol.analyze_stage1([], health=None)
    assert len(findings) == 1 and findings[0]["category"] == "app_unhealthy"
    assert findings[0]["severity"] == "fatal"
    # 健康正常 + 无错误 → 无 finding
    assert patrol.analyze_stage1([], health={"boot_ok": True, "agent_ready": True}) == []


# ---------- Stage 2 ----------

class _FakeResp:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    def __init__(self, content):
        self.content = content
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        return _FakeResp(self.content)


def test_stage2_no_llm_when_unconfigured():
    findings = patrol.analyze_stage1(TRACEBACK_LINES, health=_HEALTHY)
    out = patrol.analyze_stage2(findings, _cfg())
    assert out is findings  # 未配置 review 模型：原样返回，零调用


def test_stage2_parses_llm_json_and_fenced():
    findings = patrol.analyze_stage1(TRACEBACK_LINES, health=_HEALTHY)
    llm = _FakeLLM('```json\n{"root_cause_hypothesis": "配置缺失", "severity": "fatal"}\n```')
    out = patrol.analyze_stage2(findings, _cfg(review_provider_id="p", review_model="m"),
                                llm=llm)
    assert llm.calls == 1
    assert out[0]["root_cause_hypothesis"] == "配置缺失"
    assert out[0]["severity"] == "fatal"


def test_stage2_bad_json_keeps_finding():
    findings = patrol.analyze_stage1(TRACEBACK_LINES, health=_HEALTHY)
    out = patrol.analyze_stage2(findings, _cfg(), llm=_FakeLLM("我不会 JSON"))
    assert out[0]["category"] == "log_error"  # 解析失败保留原结论


def test_stage2_llm_exception_swallowed():
    class _Boom:
        def invoke(self, p):
            raise RuntimeError("network down")
    findings = patrol.analyze_stage1(TRACEBACK_LINES, health=_HEALTHY)
    out = patrol.analyze_stage2(findings, _cfg(), llm=_Boom())
    assert len(out) == 1  # 抛异常不炸，返回原 findings


# ---------- Healers + 审计 ----------

def test_observe_mode_only_writes_pitfall(tmp_path):
    paths = _paths(tmp_path)
    audit = EvolutionAuditStore(tmp_path / "audit.sqlite3")
    findings = patrol.analyze_stage1(TRACEBACK_LINES, health=_HEALTHY)
    actions = patrol.heal_findings(findings, _cfg(), paths, audit=audit, enabled=False)
    assert all(a["mode"] == "observe" for a in actions)
    rows = audit.list_audit(limit=50)
    assert len(rows) == 1 and rows[0]["category"] == "pitfall"
    assert rows[0]["outcome"] == "pending"


def test_quarantine_healer_auto_fixes(tmp_path):
    paths = _paths(tmp_path)
    # 预置一个"生成技能"
    skill_dir = paths["generated_dir"] / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: my-skill\n---\n", encoding="utf-8")
    audit = EvolutionAuditStore(tmp_path / "audit.sqlite3")

    finding = patrol._finding(
        symptom="技能报错", category="log_error", severity="error",
        candidate_healers=["quarantine_bad_skill", "escalate_to_human"],
        skill_name="my-skill")
    actions = patrol.heal_findings([finding], _cfg(enable_self_healing=True), paths,
                                   audit=audit, enabled=True)
    assert actions[0]["mode"] == "auto"
    assert not skill_dir.exists()  # 已从 generated 移走
    assert any(p.name.endswith("my-skill") for p in paths["quarantine_dir"].iterdir())
    rows = audit.list_audit(limit=50)
    cats = {r["category"]: r for r in rows}
    assert "quarantine" in cats and cats["quarantine"]["outcome"] == "auto_fixed"
    assert cats["quarantine"]["artifacts"]


def test_high_risk_finding_escalates(tmp_path):
    paths = _paths(tmp_path)
    audit = EvolutionAuditStore(tmp_path / "audit.sqlite3")
    finding = patrol._finding(
        symptom="ValueError 重复", category="log_error", severity="error",
        candidate_healers=["revert_config_patch", "escalate_to_human"])
    actions = patrol.heal_findings([finding], _cfg(enable_self_healing=True), paths,
                                   audit=audit, enabled=True)
    assert actions[0]["mode"] == "escalated"
    rows = audit.list_audit(category="escalation")
    assert len(rows) == 1 and rows[0]["outcome"] == "escalated"


def test_duplicate_findings_not_spammed(tmp_path):
    paths = _paths(tmp_path)
    audit = EvolutionAuditStore(tmp_path / "audit.sqlite3")
    findings = patrol.analyze_stage1(TRACEBACK_LINES, health=_HEALTHY)
    patrol.heal_findings(findings, _cfg(), paths, audit=audit, enabled=False)
    patrol.heal_findings(findings, _cfg(), paths, audit=audit, enabled=False)
    # 同一 pitfall 未闭环，第二轮不重复写
    assert len(audit.list_audit(category="pitfall")) == 1


def test_run_analysis_swallows_errors(tmp_path):
    # audit 对象是坏的（所有方法抛异常），run_analysis 也不能炸
    class _BoomStore:
        def list_audit(self, *a, **k):
            raise RuntimeError("db down")
        def log(self, *a, **k):
            raise RuntimeError("db down")
    out = patrol.run_analysis(TRACEBACK_LINES, _cfg(), _paths(tmp_path),
                              health=_HEALTHY, audit=_BoomStore(), enabled=False)
    assert out["error"] is None  # 异常在 heal 内部逐条吞掉，链路仍返回 findings
    assert len(out["findings"]) == 1
