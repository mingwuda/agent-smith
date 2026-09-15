"""运行时巡检分析器与自愈器注册表（DESIGN.md §4.6）。

由独立守护进程 guardian_daemon（控制面）调用，**不挂主 app**：

    日志/遥测 → Stage1 启发式（零 LLM）→ 命中才进 Stage2（可选 review LLM）
              → healers 注册表处理 → 每个动作写 EvolutionAuditStore

安全铁律（DESIGN §4.6.5）：
- 观察永远执行；healer 动作仅在 enable_self_healing 开启时执行，关时只写观察审计。
- 低风险（隔离已登记的 generated 坏技能）可自动；高风险（config 回退）恒升级人工。
- 本模块所有外部依赖（LLM、文件移动）失败一律自吞，返回 finding 原样，绝不抛给循环。

ponytail: 当前遥测（UsageTracker）无 error 字段，Stage 1 以日志 Traceback 聚类为主
信号；扩展遥测错误率是后续升级点（需扩 usage_records schema，YAGNI 暂不做）。
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("patrol")

# ---------- Stage 1 启发式阈值（默认值，可被 run_analysis 入参覆盖） ----------

DEFAULT_REPEAT_THRESHOLD = 3      # 同类异常出现 ≥3 次 → finding
DEFAULT_ERROR_LINES_THRESHOLD = 10  # 错误行总数 ≥10 → finding
MAX_EVIDENCE_PER_FINDING = 3

# Traceback 末行异常类型，如 ValueError / json.decoder.JSONDecodeError / asyncio.TimeoutError
_EXC_RE = re.compile(r"([A-Za-z_][\w.]*(?:Error|Exception|Warning|Fault|Timeout))\b")
# 进化产物（P3 生成的技能）路径线索：skills/.generated/<name>
_GENERATED_SKILL_RE = re.compile(r"\.generated[\\/]+([\w\-.]+)")


# ---------- 数据结构 ----------

def _finding(*, symptom: str, category: str, severity: str,
             evidence: Optional[list[str]] = None,
             candidate_healers: Optional[list[str]] = None,
             skill_name: Optional[str] = None,
             root_cause_hypothesis: str = "") -> dict:
    return {
        "symptom": symptom,
        "category": category,
        "severity": severity,
        "evidence": evidence or [],
        "candidate_healers": candidate_healers or ["escalate_to_human"],
        "skill_name": skill_name,
        "root_cause_hypothesis": root_cause_hypothesis,
    }


# ---------- Stage 1：零 LLM 启发式 ----------

def analyze_stage1(
    error_lines: list[str],
    *,
    health: Optional[dict] = None,
    repeat_threshold: int = DEFAULT_REPEAT_THRESHOLD,
    error_lines_threshold: int = DEFAULT_ERROR_LINES_THRESHOLD,
) -> list[dict]:
    """对日志错误行 + 健康探针结果跑规则，产出 finding 列表。纯函数、无副作用。"""
    findings: list[dict] = []

    # 规则 0：主 app 不健康（boot 恢复由 daemon 编排，这里只产出升级 finding）
    unhealthy = (health is None) or not (health.get("boot_ok") or health.get("agent_ready"))
    if unhealthy:
        findings.append(_finding(
            symptom="主 app 健康探针异常或未就绪",
            category="app_unhealthy",
            severity="fatal",
            evidence=[json.dumps(health, ensure_ascii=False) if health else "探针不可达"],
            candidate_healers=["escalate_to_human"],
            root_cause_hypothesis="进程不可达或启动自愈未完成，需检查守护回退/重启链路",
        ))

    if not error_lines:
        return findings

    # 规则 1：异常类型聚类，同类反复出现 → finding
    exc_counter: Counter[str] = Counter()
    exc_evidence: dict[str, list[str]] = {}
    for line in error_lines:
        m = _EXC_RE.search(line)
        if not m:
            continue
        exc = m.group(1)
        exc_counter[exc] += 1
        bucket = exc_evidence.setdefault(exc, [])
        if len(bucket) < MAX_EVIDENCE_PER_FINDING:
            bucket.append(line.strip()[:300])

    for exc, count in exc_counter.items():
        if count < repeat_threshold:
            continue
        # 证据文本里若指向 .generated 技能，附带技能名并允许隔离 healer
        joined = "\n".join(error_lines)
        skill_match = _GENERATED_SKILL_RE.search(joined)
        skill_name = skill_match.group(1) if skill_match else None
        healers = ["quarantine_bad_skill", "escalate_to_human"] if skill_name \
            else ["escalate_to_human"]
        findings.append(_finding(
            symptom=f"异常 {exc} 在最近日志中重复出现 {count} 次",
            category="log_error",
            severity="error",
            evidence=exc_evidence.get(exc, []),
            candidate_healers=healers,
            skill_name=skill_name,
            root_cause_hypothesis=f"高频异常 {exc}，疑似复现性故障",
        ))

    # 规则 2：错误总量尖刺（即使异常类型分散）
    has_recurrent = any(f["category"] == "log_error" for f in findings)
    if len(error_lines) >= error_lines_threshold and not has_recurrent:
        findings.append(_finding(
            symptom=f"最近日志出现 {len(error_lines)} 处错误标记（Traceback/CRITICAL/FATAL）",
            category="log_error",
            severity="warn",
            evidence=[ln.strip()[:300] for ln in error_lines[-MAX_EVIDENCE_PER_FINDING:]],
            candidate_healers=["escalate_to_human"],
            root_cause_hypothesis="错误量尖刺，类型分散，需人工聚类判断",
        ))

    return findings


# ---------- Stage 2：LLM 根因分析（可选、省钱、失败自吞） ----------

_STAGE2_PROMPT = """你是运行时巡检分析器。下面是主服务最近日志中的错误片段（已由启发式规则命中）。
请输出严格 JSON（不要 markdown 代码块、不要多余文字）：
{"root_cause_hypothesis": "一句话根因假设", "severity": "warn|error|fatal", "candidate_healers": ["escalate_to_human"]}
可用 healer：quarantine_bad_skill（仅当证据明确指向 skills/.generated/ 下某个生成技能文件报错）、escalate_to_human。
错误片段：
"""


def analyze_stage2(findings: list[dict], cfg, *, llm: Any = None,
                   timeout: float = 15.0) -> list[dict]:
    """用 review LLM 增强 finding 的根因/严重度/healer 选择。

    - 仅当配置了 review_provider_id + review_model（或显式注入 llm）时调用；
    - 任何失败（未配置/超时/JSON 解析失败）都自吞，返回原 findings。
    """
    if not findings:
        return findings
    try:
        if llm is None:
            llm = _build_review_llm(cfg, timeout)
        if llm is None:
            return findings  # 未配置 review 模型：稳态零成本
        for finding in findings:
            if finding["category"] != "log_error":
                continue
            evidence = "\n".join(finding["evidence"]) or finding["symptom"]
            resp = llm.invoke(_STAGE2_PROMPT + evidence[:2000])
            text = getattr(resp, "content", str(resp))
            parsed = _parse_json_loose(text)
            if not parsed:
                continue
            if parsed.get("root_cause_hypothesis"):
                finding["root_cause_hypothesis"] = str(parsed["root_cause_hypothesis"])[:300]
            if parsed.get("severity") in ("warn", "error", "fatal"):
                finding["severity"] = parsed["severity"]
            healers = parsed.get("candidate_healers")
            if isinstance(healers, list) and healers:
                valid = [h for h in healers if h in HEALERS]
                if valid:
                    finding["candidate_healers"] = valid
    except Exception:  # LLM 分析绝不能影响巡检循环
        logger.exception("[巡检] Stage2 LLM 分析失败，保留启发式结论")
    return findings


def _parse_json_loose(text: str) -> Optional[dict]:
    """从 LLM 回复中宽松提取 JSON 对象。"""
    if not text:
        return None
    try:
        return json.loads(text.strip())
    except Exception:
        pass
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fence.group(1) if fence else None
    if candidate is None:
        m = re.search(r"\{.*\}", text, re.S)
        candidate = m.group(0) if m else None
    if candidate:
        try:
            return json.loads(candidate)
        except Exception:
            return None
    return None


def _build_review_llm(cfg, timeout: float):
    """按 review_provider_id/review_model 构建 LLM；未配置返回 None。延迟 import 重依赖。"""
    pid = (getattr(cfg, "review_provider_id", "") or "").strip()
    model = (getattr(cfg, "review_model", "") or "").strip()
    if not pid or not model:
        return None
    prov = (getattr(cfg, "providers", None) or {}).get(pid, {})
    api_key = prov.get("api_key", "") or "sk-no-key-required"
    base_url = prov.get("base_url", "") or None
    if pid == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model, api_key=api_key, base_url=base_url,
                             temperature=0, max_retries=0, timeout=timeout)
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=model, api_key=api_key, base_url=base_url,
                      temperature=0, max_retries=0, timeout=timeout)


# ---------- Healers 注册表（DESIGN §4.6.4） ----------

def quarantine_bad_skill(finding: dict, ctx: dict) -> Optional[dict]:
    """低风险：把指向的 generated 技能移入隔离区。返回动作结果；不适用返回 None。"""
    # daemon 直跑时 agent_core/ 已注入 sys.path（顶层模块）；包内/测试环境走 agent_core 包
    try:
        import guardian  # noqa: PLC0415  守护层纯 stdlib，延迟 import 保持本模块可独立加载
    except ImportError:
        from agent_core import guardian

    skill_name = finding.get("skill_name")
    if not skill_name:
        return None
    generated_dir = Path(ctx["paths"]["generated_dir"])
    quarantine_dir = Path(ctx["paths"]["quarantine_dir"])
    target = generated_dir / skill_name
    if not target.exists():
        # 也可能登记的是文件（SKILL.md）
        target = generated_dir / f"{skill_name}.md"
        if not target.exists():
            return None
    moved = guardian._move_to_quarantine(quarantine_dir, target)
    if not moved:
        return None
    # 找到刚被移动进隔离区的产物（以源名结尾、mtime 最新），作为审计 artifact
    quarantined = ""
    try:
        candidates = [p for p in quarantine_dir.glob(f"*{target.name}") if p.is_file() or p.is_dir()]
        if candidates:
            quarantined = str(max(candidates, key=lambda p: p.stat().st_mtime))
    except OSError:
        pass
    logger.warning("[巡检] 已隔离报错的生成技能: %s", skill_name)
    return {
        "healer": "quarantine_bad_skill",
        "summary": f"已隔离报错的生成技能 {skill_name}（下次重启后主 app 不再加载）",
        "artifacts": [quarantined or str(target)],
    }


def revert_config_patch(finding: dict, ctx: dict) -> Optional[dict]:
    """高风险 healer：P1 恒不自动执行，返回 None 让编排升级人工。

    ponytail: 自动 config 回退需 apply 闸门 + 审批（DESIGN Phase 4），
    当前 _approval_gate 恒 False；升级路径：Phase 4 接入沙箱 dry-run 后在此放行。
    """
    return None


def escalate_to_human(finding: dict, ctx: dict) -> dict:
    """兜底升级：生成人工告警内容（审计写入由编排统一完成）。"""
    return {
        "healer": "escalate_to_human",
        "summary": f"[待人工处理] {finding['symptom']}",
        "artifacts": [],
    }


HEALERS: dict[str, Callable[[dict, dict], Optional[dict]]] = {
    "quarantine_bad_skill": quarantine_bad_skill,
    "revert_config_patch": revert_config_patch,
    "escalate_to_human": escalate_to_human,
}


# ---------- 编排：分析 → 自愈 → 审计 ----------

def _has_open_audit(audit, category: str, summary: str) -> bool:
    """去重：同一 category+summary 已有 pending/escalated 未闭环记录则跳过，避免巡检刷屏。"""
    try:
        recent = audit.list_audit(category=category, limit=20)
        return any(r["summary"] == summary and r["outcome"] in ("pending", "escalated")
                   for r in recent)
    except Exception:
        return False


def heal_findings(findings: list[dict], cfg, paths: dict, *,
                  audit, enabled: bool) -> list[dict]:
    """对 findings 逐个处理并写审计。返回动作摘要列表。

    enabled=False（观察模式）：只写 pitfall 观察记录，不执行任何改动 healer。
    enabled=True：低风险 healer 自动执行；无匹配/高风险 → escalate_to_human。
    """
    actions: list[dict] = []
    for finding in findings:
        # 进程级"坑"记录（DESIGN §4.6.4 write_pitfall_memory 的审计版，见 §4.7.1 归属修正）
        pitfall_summary = f"[巡检发现] {finding['symptom']}"
        if not _has_open_audit(audit, "pitfall", pitfall_summary):
            try:
                audit.log(
                    source="patrol", category="pitfall",
                    severity=finding["severity"], summary=pitfall_summary,
                    detail={"root_cause_hypothesis": finding.get("root_cause_hypothesis", ""),
                            "evidence": finding["evidence"][:MAX_EVIDENCE_PER_FINDING]},
                    outcome="pending",
                )
            except Exception:
                logger.exception("[巡检] pitfall 审计写入失败")

        if not enabled:
            actions.append({"finding": finding["symptom"], "mode": "observe"})
            continue

        result = None
        for name in finding["candidate_healers"]:
            healer = HEALERS.get(name)
            if healer is None:
                continue
            try:
                result = healer(finding, {"paths": paths, "cfg": cfg})
            except Exception:
                logger.exception("[巡检] healer %s 执行异常", name)
                result = None
            if result:
                break

        if result and result["healer"] != "escalate_to_human":
            try:
                audit.log(
                    source="patrol", category="quarantine",
                    severity=finding["severity"], summary=result["summary"],
                    detail={"healer": result["healer"],
                            "root_cause_hypothesis": finding.get("root_cause_hypothesis", "")},
                    artifacts=result.get("artifacts", []),
                    outcome="auto_fixed",
                )
            except Exception:
                logger.exception("[巡检] 自愈动作审计写入失败")
            actions.append({"finding": finding["symptom"], "mode": "auto",
                            "healer": result["healer"]})
        else:
            esc = escalate_to_human(finding, {"paths": paths, "cfg": cfg})
            if not _has_open_audit(audit, "escalation", esc["summary"]):
                try:
                    audit.log(
                        source="patrol", category="escalation",
                        severity=finding["severity"], summary=esc["summary"],
                        detail={"root_cause_hypothesis": finding.get("root_cause_hypothesis", ""),
                                "evidence": finding["evidence"][:MAX_EVIDENCE_PER_FINDING]},
                        outcome="escalated",
                    )
                except Exception:
                    logger.exception("[巡检] 升级审计写入失败")
            actions.append({"finding": finding["symptom"], "mode": "escalated"})
    return actions


def run_analysis(error_lines: list[str], cfg, paths: dict, *,
                 health: Optional[dict] = None, audit=None,
                 enabled: Optional[bool] = None, llm: Any = None,
                 include_health: bool = True) -> dict:
    """单轮完整链路：Stage1 →（命中且配置了 review 模型）Stage2 → heal + 审计。

    audit 为 None 时惰性取进程级单例；enabled 默认取 cfg.enable_self_healing。
    include_health=False 时跳过健康探针规则（daemon 已单独编排健康恢复，避免重复 finding）。
    任何异常自吞，返回 {"findings": [...], "actions": [...], "error": Optional[str]}。
    """
    result: dict[str, Any] = {"findings": [], "actions": [], "error": None}
    try:
        stage1_health = health if include_health else {"boot_ok": True, "agent_ready": True}
        findings = analyze_stage1(error_lines, health=stage1_health)
        if findings:
            findings = analyze_stage2(findings, cfg, llm=llm)
        result["findings"] = findings
        if audit is None:
            try:
                from evolution.audit_store import get_audit_store  # daemon 直跑
            except ImportError:
                from agent_core.evolution.audit_store import get_audit_store
            audit = get_audit_store()
        if enabled is None:
            enabled = bool(getattr(cfg, "enable_self_healing", False))
        result["actions"] = heal_findings(findings, cfg, paths,
                                          audit=audit, enabled=enabled)
    except Exception:
        logger.exception("[巡检] run_analysis 异常（已吞掉，不影响守护循环）")
        result["error"] = "analysis_failed"
    return result