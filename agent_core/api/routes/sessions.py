"""会话路由"""
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import session_store
from config import AgentConfig
from services.workspace import _workspace_for_user, _strip_existing_artifact_section, _append_artifact_links
from api.deps import _get_current_user
from logger import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["sessions"])


class SessionInfo(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int
    source: str = "web"  # "web" 或 "wechat"
    project_id: str = ""  # 归属的项目 ID（空字符串表示未归属）


class SessionListResponse(BaseModel):
    sessions: list[SessionInfo]
    current_id: str


class SessionMessagesResponse(BaseModel):
    id: str
    title: str
    messages: list[dict]
    source: str = "web"


class RenameRequest(BaseModel):
    title: str


class CreateSessionResponse(BaseModel):
    id: str
    title: str


def _strip_wechat_prefix(title: str) -> str:
    """去掉微信会话标题里的 [微信] 前缀（图标已能区分来源，前缀冗余）"""
    return title.removeprefix("[微信]")


class SetWorkspaceRequest(BaseModel):
    workspace: str


@router.get("/sessions", response_model=SessionListResponse)
def list_sessions(request: Request):
    """列出当前用户的会话（含微信 Bot 会话）"""
    uid = _get_current_user(request)
    web_sessions = list(session_store.list_sessions(uid))
    web_ids = [s["id"] for s in web_sessions]
    sessions = [
        SessionInfo(**s, source="web")
        for s in web_sessions
    ]
    # 合并该用户的微信 Bot 会话
    wechat_uid = f"wechat_{uid}"
    wechat_sessions = list(session_store.list_sessions(wechat_uid))
    logger.info(
        "[会话列表] %s: web=%d个(%s), wechat=%d个(%s)",
        uid, len(web_sessions), web_ids[:5],
        len(wechat_sessions),
        [(s["id"], s.get("message_count", 0)) for s in wechat_sessions[:5]],
    )
    # 不再去重，Web 和微信会话各自独立展示
    sessions: list[SessionInfo] = [
        SessionInfo(**s, source="web") for s in web_sessions
    ]
    for s in wechat_sessions:
        s = dict(s)
        s["title"] = _strip_wechat_prefix(s.get("title", ""))
        sessions.append(SessionInfo(**s, source="wechat"))
    sessions.sort(key=lambda s: s.updated_at, reverse=True)
    current_id = sessions[0].id if sessions else "default"
    return SessionListResponse(sessions=sessions, current_id=current_id)


@router.get("/sessions/{session_id}", response_model=SessionMessagesResponse)
def get_session(session_id: str, request: Request, source: str = "auto"):
    """获取当前用户的会话消息（也支持微信 Bot 会话）

    参数:
      source: "auto"（默认,优先 Web 再降级微信），"web"（只查 Web），"wechat"（只查微信）
    """
    uid = _get_current_user(request)
    is_wechat = False
    session = None

    if source == "wechat":
        # 明确指定查微信
        session = session_store.get_session(f"wechat_{uid}", session_id)
        is_wechat = True
    elif source == "web":
        # 明确指定查 Web
        session = session_store.get_session(uid, session_id)
    else:
        # auto: 优先 Web，Web 不存在时才降级到微信
        session = session_store.get_session(uid, session_id)
        if not session:
            wechat_session = session_store.get_session(f"wechat_{uid}", session_id)
            if wechat_session:
                session = wechat_session
                is_wechat = True

    if not session:
        raise HTTPException(404, "会话不存在")
    raw_title = session.get("title", "未命名")
    if is_wechat:
        raw_title = _strip_wechat_prefix(raw_title)
    return SessionMessagesResponse(
        id=session["id"],
        title=raw_title,
        messages=session.get("messages", []),
        source="wechat" if is_wechat else "web",
    )


class CreateSessionRequest(BaseModel):
    project_id: str = ""


@router.get("/sessions/{session_id}/messages/lite")
def get_session_messages_lite(session_id: str, request: Request, source: str = "auto"):
    """获取会话的轻量消息列表（不含 steps/todo_list/images，用于懒加载）

    参数:
      source: "auto"（默认,优先 Web 再降级微信），"web"（只查 Web），"wechat"（只查微信）
    """
    uid = _get_current_user(request)
    is_wechat = False
    session = None

    if source == "wechat":
        session = session_store.get_session_lite(f"wechat_{uid}", session_id)
        is_wechat = True
    elif source == "web":
        session = session_store.get_session_lite(uid, session_id)
    else:
        session = session_store.get_session_lite(uid, session_id)
        if not session:
            session = session_store.get_session_lite(f"wechat_{uid}", session_id)
            if session:
                is_wechat = True

    if not session:
        raise HTTPException(404, "会话不存在")
    raw_title = session.get("title", "未命名")
    if is_wechat:
        raw_title = _strip_wechat_prefix(raw_title)
    return {
        "id": session["id"],
        "title": raw_title,
        "messages": session.get("messages", []),
        "source": "wechat" if is_wechat else "web",
    }


@router.get("/sessions/{session_id}/messages/{message_index}")
def get_session_message_detail(session_id: str, message_index: int, request: Request, source: str = "auto"):
    """获取单条消息的完整内容（用于懒加载展开）

    参数:
      source: "auto"（默认,优先 Web 再降级微信），"web"（只查 Web），"wechat"（只查微信）
    """
    uid = _get_current_user(request)
    is_wechat = False
    user_id = uid

    if source == "wechat":
        user_id = f"wechat_{uid}"
        is_wechat = True
    elif source == "web":
        user_id = uid
    else:
        # auto: 优先 Web
        detail = session_store.get_message_detail(uid, session_id, message_index)
        if not detail:
            user_id = f"wechat_{uid}"
            detail = session_store.get_message_detail(user_id, session_id, message_index)
            if detail:
                is_wechat = True
        if detail:
            raw_title = session_store.get_session(uid, session_id)
            if raw_title:
                detail["title"] = _strip_wechat_prefix(raw_title.get("title", "未命名")) if is_wechat else raw_title.get("title", "未命名")
            return detail
        raise HTTPException(404, "消息不存在")

    detail = session_store.get_message_detail(user_id, session_id, message_index)
    if not detail:
        raise HTTPException(404, "消息不存在")
    raw_title = session_store.get_session(user_id, session_id)
    if raw_title:
        detail["title"] = _strip_wechat_prefix(raw_title.get("title", "未命名")) if is_wechat else raw_title.get("title", "未命名")
    return detail


@router.post("/sessions", response_model=CreateSessionResponse)
def create_session(req: CreateSessionRequest, request: Request):
    """创建新会话（可指定归属项目）"""
    uid = _get_current_user(request)
    session = session_store.create_session(uid, project_id=req.project_id or None)
    return CreateSessionResponse(id=session["id"], title=session["title"])


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str, request: Request):
    """删除会话（也支持微信 Bot 会话）"""
    uid = _get_current_user(request)
    ok = session_store.delete_session(uid, session_id)
    if not ok:
        ok = session_store.delete_session(f"wechat_{uid}", session_id)
    if not ok:
        raise HTTPException(404, "会话不存在")
    return {"status": "ok", "message": f"已删除会话 {session_id}"}


@router.put("/sessions/{session_id}/rename")
def rename_session(session_id: str, req: RenameRequest, request: Request):
    """重命名会话"""
    uid = _get_current_user(request)
    ok = session_store.rename_session(uid, session_id, req.title)
    if not ok:
        raise HTTPException(404, "会话不存在")
    return {"status": "ok", "message": f"已重命名为 {req.title}"}


@router.put("/sessions/{session_id}/workspace")
def set_session_workspace(session_id: str, req: SetWorkspaceRequest, request: Request):
    """设置当前会话的工作目录。路径可以是绝对路径或相对于工作区根目录的相对路径。"""
    uid = _get_current_user(request)
    ws = req.workspace.strip()

    # 解析路径：绝对路径直接用，相对路径拼接用户工作区
    if os.path.isabs(ws):
        resolved = Path(ws).expanduser().resolve()
    else:
        base = _workspace_for_user(uid)
        resolved = (base / ws).resolve()

    # 确保目录存在
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(400, f"无法创建目录: {e}")

    ws_str = str(resolved)
    ok = session_store.set_session_workspace(uid, session_id, ws_str)
    if not ok:
        raise HTTPException(404, "会话不存在")
    return {"status": "ok", "workspace": ws_str, "message": "工作目录已设置"}


@router.get("/sessions/{session_id}/workspace")
def get_session_workspace(session_id: str, request: Request):
    """获取当前会话的工作目录"""
    uid = _get_current_user(request)
    ws = session_store.get_session_workspace(uid, session_id)
    return {"workspace": ws or ""}
