"""项目（Workspace）管理路由 — CRUD + 会话归属"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import session_store

router = APIRouter(tags=["projects"])


class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="项目名称")
    directory_path: str = Field("", max_length=500, description="项目目录路径")


class UpdateProjectRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    directory_path: Optional[str] = Field(None, max_length=500)


class SetSessionProjectRequest(BaseModel):
    project_id: str = Field("")


@router.get("/projects")
async def list_projects(request: Request):
    """列出所有项目（含每个项目的会话数）"""
    uid = getattr(request.state, "user_id", "default")
    projects = session_store.list_projects(uid)
    return {"projects": projects}


@router.post("/projects")
async def create_project(req: CreateProjectRequest, request: Request):
    """创建新项目"""
    uid = getattr(request.state, "user_id", "default")
    project = session_store.create_project(uid, req.name, req.directory_path.strip())
    return project


@router.get("/projects/{project_id}")
async def get_project(project_id: str, request: Request):
    """获取单个项目详情"""
    uid = getattr(request.state, "user_id", "default")
    project = session_store.get_project(uid, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


@router.put("/projects/{project_id}")
async def update_project(project_id: str, req: UpdateProjectRequest, request: Request):
    """更新项目名称或路径"""
    uid = getattr(request.state, "user_id", "default")
    ok = session_store.update_project(
        uid, project_id,
        name=req.name, directory_path=req.directory_path,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="项目不存在或无变更")
    return session_store.get_project(uid, project_id)


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str, request: Request):
    """删除项目（会话不删除，但 project_id 置空）"""
    uid = getattr(request.state, "user_id", "default")
    ok = session_store.delete_project(uid, project_id)
    if not ok:
        raise HTTPException(status_code=404, detail="项目不存在")
    return {"status": "ok"}


@router.get("/projects/{project_id}/sessions")
async def list_project_sessions(project_id: str, request: Request):
    """列出某项目下的所有会话"""
    uid = getattr(request.state, "user_id", "default")
    sessions = session_store.list_sessions_by_project(uid, project_id)
    return {"sessions": sessions}


@router.put("/projects/{project_id}/sessions/{session_id}")
async def assign_session_to_project(project_id: str, session_id: str):
    """将某个会话归属于指定项目"""
    # 先验证项目存在
    from services.auth import get_user_id_from_request  # 延迟避免循环导入
    uid = "default"
    proj = session_store.get_project(uid, project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="目标项目不存在")
    ok = session_store.set_session_project(uid, session_id, project_id)
    if not ok:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"status": "ok", "project_id": project_id}
