"""用户管理 —— 多用户工作空间隔离"""
import json
import os
import shutil
from pathlib import Path
from typing import Optional

# 数据根目录
DATA_DIR = Path.home() / ".desktop_agent"
USERS_DIR = DATA_DIR / "users"
USERS_JSON = USERS_DIR / "users.json"
WORKSPACE_BASE = Path.home() / "agent_workspace"


def _ensure():
    USERS_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACE_BASE.mkdir(parents=True, exist_ok=True)


def _all_users() -> dict:
    """读取 users.json: { "user_id": {"name": ..., "created_at": ...} }"""
    _ensure()
    if USERS_JSON.exists():
        try:
            return json.loads(USERS_JSON.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write_users(users: dict):
    USERS_JSON.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")


def list_users() -> list[dict]:
    """列出所有用户"""
    return [{"id": uid, **info} for uid, info in _all_users().items()]


def get_user(user_id: str) -> Optional[dict]:
    users = _all_users()
    info = users.get(user_id)
    if not info:
        return None
    return {"id": user_id, **info}


def create_user(user_id: str, name: str = "") -> dict:
    """创建用户及其隔离目录"""
    _ensure()
    users = _all_users()
    if user_id in users:
        raise ValueError(f"用户 {user_id} 已存在")

    from datetime import datetime
    now = datetime.now().isoformat()
    info = {"name": name or user_id, "created_at": now}
    users[user_id] = info
    _write_users(users)

    # 创建用户目录结构
    (USERS_DIR / user_id / "sessions").mkdir(parents=True, exist_ok=True)
    (USERS_DIR / user_id / "memory").mkdir(parents=True, exist_ok=True)
    (USERS_DIR / user_id / "usage").mkdir(parents=True, exist_ok=True)
    (WORKSPACE_BASE / user_id).mkdir(parents=True, exist_ok=True)

    return {"id": user_id, **info}


def delete_user(user_id: str) -> bool:
    """删除用户及其所有数据"""
    users = _all_users()
    if user_id not in users:
        return False
    del users[user_id]
    _write_users(users)
    # 删除用户目录
    user_dir = USERS_DIR / user_id
    if user_dir.exists():
        shutil.rmtree(user_dir)
    # 删除工作空间
    ws_dir = WORKSPACE_BASE / user_id
    if ws_dir.exists():
        shutil.rmtree(ws_dir)
    return True


# ── 路径工具 ──

def session_dir(user_id: str) -> Path:
    path = USERS_DIR / user_id / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def usage_dir(user_id: str) -> Path:
    path = USERS_DIR / user_id / "usage"
    path.mkdir(parents=True, exist_ok=True)
    return path


def user_workspace(user_id: str) -> str:
    ws = WORKSPACE_BASE / user_id
    ws.mkdir(parents=True, exist_ok=True)
    return str(ws)
