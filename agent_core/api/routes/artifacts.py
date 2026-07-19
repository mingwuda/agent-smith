"""制品路由"""
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from services.workspace import _resolve_artifact_path, _relative_artifact_path, _workspace_for_user
from api.deps import _get_current_user

router = APIRouter(tags=["artifacts"])


# ---------- 用户上传图片下载 ----------

USER_IMG_DIR = Path.home() / ".desktop_agent" / "user_images"


@router.get("/artifacts/download")
def download_artifact(path: str, request: Request):
    """下载当前用户工作区内的文件制品。"""
    uid = _get_current_user(request)
    target = _resolve_artifact_path(uid, path)
    return FileResponse(target, filename=target.name)


@router.get("/user-images/download")
def download_user_image(name: str, request: Request, uid: str = ""):
    """下载用户上传的图片，供图生图模型等工具使用（免认证，uid 由 URL 提供）。"""
    if not uid:
        uid = _get_current_user(request)
    img_path = USER_IMG_DIR / uid / name
    # 路径安全校验：不允许跨目录
    try:
        resolved = img_path.resolve(strict=False)
        resolved.relative_to((USER_IMG_DIR / uid).resolve())
    except (ValueError, RuntimeError, OSError) as exc:
        raise HTTPException(403, "不允许访问该路径") from exc
    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(404, "图片文件不存在")
    return FileResponse(resolved, media_type="image/png")


@router.get("/api/screenshot")
def get_screenshot(token: str = "", path: str = "", request: Request = None):
    """访问浏览器截图文件。
    
    参数:
        token: 截图文件名（不含路径），如 screenshot_1234567890
        path: 截图文件的绝对路径（兼容旧版）
    """
    uid = _get_current_user(request)
    from tools.browser_tools import _workspace_ctx as browser_workspace_ctx
    browser_workspace = browser_workspace_ctx.get()
    
    # 优先使用 token 查找（基于文件名的确定性方案）
    if token:
        target = None
        # 先尝试截图实际保存的路径（可能被会话工作目录覆盖）
        if browser_workspace:
            candidate = browser_workspace / ".browser_screenshots" / f"{token}.png"
            if candidate.exists():
                target = candidate.resolve()
        # 回退到用户默认工作区
        if not target:
            workspace = _workspace_for_user(uid)
            target = (workspace / ".browser_screenshots" / f"{token}.png").resolve()
            try:
                target.relative_to(workspace)
            except ValueError:
                raise HTTPException(403, "不允许访问该路径")
        if not target.exists() or not target.is_file():
            raise HTTPException(404, "截图文件不存在")
        return FileResponse(target, media_type="image/png")
    
    # 兼容旧版 path 参数
    if path:
        try:
            target = Path(path).expanduser().resolve()
            if browser_workspace:
                target.relative_to(browser_workspace)
            else:
                workspace = _workspace_for_user(uid)
                target.relative_to(workspace)
            if ".browser_screenshots" not in target.parts:
                raise HTTPException(403, "只能访问浏览器截图文件")
            if not target.exists() or not target.is_file():
                raise HTTPException(404, "截图文件不存在")
            return FileResponse(target, media_type="image/png")
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(403, "不允许访问该路径") from exc
    
    raise HTTPException(400, "请提供 token 或 path 参数")


def _user_image_urls(uid: str, image_paths: list[str], request: Request) -> list[str]:
    """将本地图片路径转换为可下载的 HTTP URL，供 LLM 工具使用。"""
    base_url = str(request.base_url).rstrip("/")
    urls: list[str] = []
    for fpath in image_paths:
        name = Path(fpath).name
        url = f"{base_url}/user-images/download?name={quote(name)}&uid={quote(uid)}"
        urls.append(url)
    return urls


@router.get("/artifacts/preview")
def preview_artifact(path: str, request: Request):
    """预览当前用户工作区内的 Markdown 文件制品。"""
    uid = _get_current_user(request)
    target = _resolve_artifact_path(uid, path)
    if target.suffix.lower() not in {".md", ".markdown"}:
        raise HTTPException(400, "仅支持预览 Markdown 文件")
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(400, "文件不是 UTF-8 文本，无法预览") from exc
    return {
        "name": target.name,
        "path": target.relative_to(_workspace_for_user(uid)).as_posix(),
        "content": content,
    }
