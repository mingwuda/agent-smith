"""定时任务路由 —— 项目级定时任务的 CRUD + 校验"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import session_store
from cron_service import parse_cron_expr, cron_matches
from datetime import datetime

router = APIRouter(tags=["cron"])


class CreateCronRequest(BaseModel):
    project_id: str = Field(..., description="所属项目 id")
    name: str = Field(..., min_length=1, max_length=100, description="任务名称")
    cron: str = Field(..., description="执行时机：中文自然语言或标准 5 段 cron")
    content: str = Field("", max_length=10000, description="执行的内容（发给 Agent 的指令）")


class UpdateCronRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    cron: Optional[str] = Field(None)
    content: Optional[str] = Field(None, max_length=10000)
    enabled: Optional[bool] = None


def _validate_project(uid: str, project_id: str) -> None:
    p = session_store.get_project(uid, project_id)
    if not p:
        raise HTTPException(status_code=404, detail=f"项目不存在: {project_id}")


@router.get("/projects/{project_id}/cron")
async def list_project_cron(project_id: str, request: Request):
    """列出某项目下所有定时任务（附带 cron 可读表达）"""
    uid = getattr(request.state, "user_id", "default")
    tasks = session_store.list_cron_tasks(uid, project_id)
    for t in tasks:
        t["cron_human"] = _humanize_cron(t["cron"])
    return {"tasks": tasks}


@router.get("/cron")
async def list_all_cron(request: Request):
    """列出当前用户全部定时任务（用于全局管理视图）"""
    uid = getattr(request.state, "user_id", "default")
    tasks = session_store.list_cron_tasks(uid)
    for t in tasks:
        t["cron_human"] = _humanize_cron(t["cron"])
    return {"tasks": tasks}


@router.post("/cron")
async def create_cron(req: CreateCronRequest, request: Request):
    """新增定时任务：把自然语言执行时机转成 cron 后存储。"""
    uid = getattr(request.state, "user_id", "default")
    _validate_project(uid, req.project_id)
    try:
        cron = parse_cron_expr(req.cron)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    task = session_store.create_cron_task(
        uid, req.project_id, req.name.strip(), cron, (req.content or "").strip(),
    )
    task["cron_human"] = _humanize_cron(cron)
    return task


@router.put("/cron/{task_id}")
async def update_cron(task_id: str, req: UpdateCronRequest, request: Request):
    """更新定时任务字段；cron 变更时重新解析自然语言。"""
    uid = getattr(request.state, "user_id", "default")
    existing = session_store.get_cron_task(uid, task_id)
    if not existing:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    name = req.name if req.name is not None else existing["name"]
    cron = req.cron if req.cron is not None else existing["cron"]
    content = req.content if req.content is not None else existing["content"]
    try:
        cron = parse_cron_expr(cron)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    ok = session_store.update_cron_task(
        uid, task_id, name=name.strip(), cron=cron,
        content=content, enabled=req.enabled,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="定时任务不存在或无变更")
    t = session_store.get_cron_task(uid, task_id)
    t["cron_human"] = _humanize_cron(cron)
    return t


@router.delete("/cron/{task_id}")
async def delete_cron(task_id: str, request: Request):
    """删除定时任务"""
    uid = getattr(request.state, "user_id", "default")
    ok = session_store.delete_cron_task(uid, task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    return {"status": "ok"}


@router.post("/cron/parse")
async def parse_cron(req: CreateCronRequest, request: Request):
    """校验自然语言转 cron（预览，不存储）"""
    try:
        cron = parse_cron_expr(req.cron)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"cron": cron, "cron_human": _humanize_cron(cron)}


@router.post("/cron/{task_id}/run")
async def run_cron_now(task_id: str, request: Request):
    """手动立即执行一次定时任务（不影响其 cron 排程）"""
    uid = getattr(request.state, "user_id", "default")
    task = session_store.get_cron_task(uid, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    from cron_service import run_task_now
    ok = await run_task_now(uid, task_id)
    if not ok:
        raise HTTPException(status_code=500, detail="触发执行失败")
    t = session_store.get_cron_task(uid, task_id)
    t["cron_human"] = _humanize_cron(t["cron"])
    return t


# ---------- human 展示 ----------

_WEEK_HUMAN = {0: "周日", 1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六"}


def _humanize_cron(cron: str) -> str:
    """把 5 段 cron 转成给用户看的中文描述（尽力而为，覆盖常见场景）。"""
    try:
        parts = cron.split(" ")
        if len(parts) != 5:
            return cron
        minute, hour, mday, month, dow = parts

        def _t(t):  # 单个数字或列表；*/N 归为 None（在下方步进分支处理）
            if t == "*":
                return None
            out = []
            for x in t.split(","):
                base = x.split("/")[0]
                try:
                    out.append(int(base))
                except ValueError:
                    return None
            return out

        m_t, h_t, d_t, mo_t, w_t = map(_t, parts)
        if month and month != "*" and month != "1":
            pass  # 保留原始（低频场景）

        # 组合描述
        desc = ""
        # 时间
        if isinstance(h_t, list) and len(h_t) == 1 and isinstance(m_t, list) and len(m_t) == 1:
            # 每天固定时间
            dow_note = ""
            if isinstance(w_t, list) and w_t:
                names = [_WEEK_HUMAN.get(w % 7, str(w)) for w in sorted(w_t)]
                dow_note = "，" + "、".join(names)
            d_note = ""
            if isinstance(d_t, list) and d_t and set(d_t) != {"*"}:
                d_note = "（每月" + "、".join(str(x) + "号" for x in sorted({int(x) for x in d_t})) + "）"
            if dow_note:
                desc = f"每{dow_note.lstrip('，')} {h_t[0]:02d}:{m_t[0]:02d}"
            elif d_note:
                desc = f"每{d_note.replace('（','').replace('）','')} {h_t[0]:02d}:{m_t[0]:02d}"
            else:
                desc = f"每天 {h_t[0]:02d}:{m_t[0]:02d}"
        elif minute == "*" and isinstance(h_t, list) and len(h_t) == 1:
            desc += f"每小时 {h_t[0]} 点"
        elif re_match_step(minute):
            desc = f"每{_step(minute)}分钟"
        elif re_match_step(hour):
            desc = f"每{_step(hour)}小时"
        else:
            desc += f"{minute} {hour} {mday} {month} {dow}"

        if isinstance(w_t, list):
            names = [_WEEK_HUMAN.get(w % 7, str(w)) for w in sorted(w_t)]
            if desc.startswith("每天"):
                desc = desc.replace("每天", "每" + "、".join(names), 1)
            elif not desc.startswith("每"):
                desc += (" in " + "、".join(names))
        if isinstance(d_t, list) and d_t and set(d_t) != {"*"} and "每月" not in desc:
            desc += f"（每月{ '、'.join(str(x)+'号' for x in sorted(d_t)) }）"
        return desc
    except Exception:
        return cron


def re_match_step(expr: str) -> bool:
    return ("*/" in expr)


def _step(expr: str) -> str:
    return expr.split("/")[1]


if __name__ == "__main__":
    # 自检
    import sys
    for s in ["每天 8:30", "每周一 9:00", "每30分钟", "每月1号 10:00", "每小时"]:
        try:
            print(s, "->", parse_cron_expr(s))
        except Exception as e:
            print(s, "ERR", e)