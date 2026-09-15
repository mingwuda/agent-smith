"""进化/自愈管理端 API（DESIGN §4.7.3）。

跨用户、进程级的审计视图，全部 admin 鉴权（复用 api.deps._require_admin）。
- GET  /admin/evolution/audit            时序审计列表（过滤/分页）
- GET  /admin/evolution/audit/{id}       单条详情（完整 detail + artifacts）
- GET  /admin/evolution/health           汇总计数
- POST /admin/evolution/audit/{id}/action 批准/忽略/回退（状态流转）
- GET  /admin/evolution/artifacts        当前留存产物（隔离区技能等）
"""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from api.deps import _require_admin
from evolution.audit_store import ACTION_OUTCOMES, get_audit_store

router = APIRouter(prefix="/admin/evolution", tags=["admin-evolution"])

# agent_core/api/routes → 上溯三级到仓库根
_REPO_ROOT = Path(__file__).resolve().parents[3]
_QUARANTINE_DIR = _REPO_ROOT / "skills" / ".quarantine"


class ActionRequest(BaseModel):
    action: str  # approve | ignore | revert
    note: str = ""


@router.get("/audit")
def list_evolution_audit(
    request: Request,
    source: str = Query(default=""),
    category: str = Query(default=""),
    severity: str = Query(default=""),
    outcome: str = Query(default=""),
    since: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    _require_admin(request)
    return get_audit_store().list_audit(
        source=source or None, category=category or None,
        severity=severity or None, outcome=outcome or None,
        since=since or None, limit=limit, offset=offset,
    )


@router.get("/audit/{audit_id}")
def get_evolution_audit(audit_id: int, request: Request):
    _require_admin(request)
    rec = get_audit_store().get_one(audit_id)
    if rec is None:
        raise HTTPException(404, "审计记录不存在")
    return rec


@router.get("/health")
def evolution_health(request: Request):
    _require_admin(request)
    return get_audit_store().health_counts()


@router.post("/audit/{audit_id}/action")
def act_on_evolution_audit(audit_id: int, req: ActionRequest, request: Request):
    _require_admin(request)
    target = ACTION_OUTCOMES.get(req.action)
    if target is None:
        raise HTTPException(400, "action 必须是 approve / ignore / revert 之一")
    from api.deps import _get_current_user
    ok = get_audit_store().set_outcome(
        audit_id, target, actor=_get_current_user(request), note=(req.note or "").strip())
    if not ok:
        raise HTTPException(404, "审计记录不存在")
    # ponytail: P1 只做状态流转；revert 的实际产物恢复待 Phase 4（apply 闸门 + 沙箱），
    # 当前仅标记 auto_reverted 供管理员追踪，升级路径见 DESIGN §4.2/§4.7.3。
    return {"ok": True, "outcome": target}


@router.get("/artifacts")
def list_evolution_artifacts(request: Request):
    """列出隔离区当前留存的进化产物（供管理员查看/下载）。"""
    _require_admin(request)
    items = []
    try:
        if _QUARANTINE_DIR.exists():
            for p in sorted(_QUARANTINE_DIR.rglob("*"),
                            key=lambda x: x.stat().st_mtime, reverse=True):
                if not p.is_file() and not p.is_dir():
                    continue
                stat = p.stat()
                items.append({
                    "path": str(p.relative_to(_REPO_ROOT)),
                    "is_dir": p.is_dir(),
                    "size": stat.st_size,
                    "mtime": int(stat.st_mtime),
                })
    except OSError:
        pass
    return {"quarantine_dir": str(_QUARANTINE_DIR.relative_to(_REPO_ROOT)), "items": items[:200]}
