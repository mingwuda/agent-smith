"""会话存储 —— 将对话保存为 JSON 文件"""
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional


SESSION_DIR = Path.home() / ".desktop_agent" / "sessions"


def _ensure_dir():
    SESSION_DIR.mkdir(parents=True, exist_ok=True)


def _session_path(session_id: str) -> Path:
    return SESSION_DIR / f"{session_id}.json"


def _timestamp() -> str:
    return datetime.now().isoformat()


def create_session(title: Optional[str] = None) -> dict:
    """创建一个新会话"""
    _ensure_dir()
    session_id = str(uuid.uuid4())[:8]
    now = _timestamp()
    session = {
        "id": session_id,
        "title": title or f"会话 {now[:16].replace('T', ' ')}",
        "created_at": now,
        "updated_at": now,
        "message_count": 0,
        "messages": [],
    }
    _write_session(session)
    return session


def _write_session(session: dict):
    path = _session_path(session["id"])
    # 不把 messages 写两遍，列表只存元数据
    data = dict(session)
    data.pop("messages", None)
    data["message_count"] = len(session.get("messages", []))
    data["updated_at"] = _timestamp()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    
    # 消息单独存一个文件
    msgs_path = SESSION_DIR / f"{session['id']}_messages.json"
    msgs_path.write_text(
        json.dumps(session.get("messages", []), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _read_session(session_id: str) -> Optional[dict]:
    path = _session_path(session_id)
    if not path.exists():
        return None
    try:
        session = json.loads(path.read_text(encoding="utf-8"))
        # 加载消息
        msgs_path = SESSION_DIR / f"{session_id}_messages.json"
        if msgs_path.exists():
            session["messages"] = json.loads(msgs_path.read_text(encoding="utf-8"))
        else:
            session["messages"] = []
        return session
    except (json.JSONDecodeError, OSError):
        return None


def add_message(session_id: str, role: str, content: str) -> Optional[dict]:
    """追加一条消息到会话"""
    session = _read_session(session_id)
    if session is None:
        return None
    session.setdefault("messages", []).append({
        "role": role,
        "content": content,
        "timestamp": _timestamp(),
    })
    _write_session(session)
    return session


def list_sessions() -> list[dict]:
    """列出所有会话（仅元数据，不含消息列表）"""
    _ensure_dir()
    sessions = []
    for f in sorted(SESSION_DIR.glob("*.json")):
        if f.name.endswith("_messages.json"):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            sessions.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    # 按更新时间倒序
    sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
    return sessions


def get_session(session_id: str) -> Optional[dict]:
    """获取单个会话（含消息）"""
    return _read_session(session_id)


def delete_session(session_id: str) -> bool:
    """删除会话"""
    meta_path = _session_path(session_id)
    msgs_path = SESSION_DIR / f"{session_id}_messages.json"
    deleted = False
    if meta_path.exists():
        meta_path.unlink()
        deleted = True
    if msgs_path.exists():
        msgs_path.unlink()
    return deleted


def rename_session(session_id: str, new_title: str) -> bool:
    """重命名会话"""
    session = _read_session(session_id)
    if session is None:
        return False
    session["title"] = new_title
    _write_session(session)
    return True
