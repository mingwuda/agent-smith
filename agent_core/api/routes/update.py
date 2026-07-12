"""更新相关 API。"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from api.deps import admin_required
from config import AgentConfig
from updater import check_update, install_update

router = APIRouter()


class UpdateCheckResponse(BaseModel):
    current_version: str
    latest_version: str
    has_update: bool
    changelog: str
    update_type: str = "none"
    patches: list[dict] = []
    full_url: str = ""
    full_size: int = 0
    full_sha256: str = ""
    error: str


class UpdateInstallRequest(BaseModel):
    patches: list[dict[str, Any]]
    full_url: str = ""
    target_version: str = ""
    full_sha256: str = ""


class UpdateInstallResponse(BaseModel):
    ok: bool
    restart: bool
    pending: bool = False
    applied_patches: list[str] = []
    error: str


@router.get("/update/check", response_model=UpdateCheckResponse)
def api_check_update():
    config = AgentConfig.load()
    return check_update(update_server=config.update_server or "")


@router.post("/update/install", response_model=UpdateInstallResponse)
def api_install_update(req: UpdateInstallRequest, _: str = admin_required()):
    result = install_update(
        patches=req.patches,
        full_url=req.full_url,
        target_version=req.target_version,
        full_sha256=req.full_sha256,
    )
    return UpdateInstallResponse(**result)
