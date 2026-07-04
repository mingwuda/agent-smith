"""共享依赖 —— 认证常量、工具函数"""
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from fastapi import HTTPException, Request, Response

from ..services.workspace import _workspace_for_user

# ---------- 认证常量 ----------

AUTH_COOKIE_NAME = "desktop_agent_session"
AUTH_SESSION_SECONDS = 60 * 60 * 24 * 7
AUTH_FILE = Path.home() / ".desktop_agent" / "auth.json"

# URL token 登录：token 有效期（默认 5 分钟）
LOGIN_TOKEN_EXPIRY_SECONDS = 5 * 60


# ---------- 认证辅助函数 ----------


def _load_auth_config() -> dict:
    """加载认证配置，支持多用户。返回格式:
    {"secret": "...", "users": {"admin": "pwd1", "test": "pwd2"}}
    """
    secret = os.getenv("DESKTOP_AGENT_AUTH_SECRET") or ""
    users: dict[str, str] = {}

    file_data: dict = {}
    if AUTH_FILE.exists():
        try:
            file_data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            file_data = {}

    # 兼容旧的单用户格式
    old_username = file_data.get("username") or os.getenv("DESKTOP_AGENT_AUTH_USER") or ""
    old_password = file_data.get("password") or os.getenv("DESKTOP_AGENT_AUTH_PASSWORD") or ""
    if old_username and old_password:
        users[old_username] = old_password

    # 新的多用户格式
    file_users = file_data.get("users", {})
    if isinstance(file_users, dict):
        users.update(file_users)

    # 环境变量 AGENT_USERS 格式: "user1:pass1;user2:pass2"（优先级最高）
    env_users = os.getenv("AGENT_USERS", "").strip()
    if env_users:
        for pair in env_users.split(";"):
            pair = pair.strip()
            if ":" in pair:
                u, p = pair.split(":", 1)
                users[u.strip()] = p.strip()
        # 写回 auth.json 使环境变量配置持久化
        try:
            AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
            AUTH_FILE.write_text(
                json.dumps({"users": users, "secret": secret or file_data.get("secret", "")}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            AUTH_FILE.chmod(0o600)
        except OSError:
            pass

    secret = secret or file_data.get("secret") or ""

    if not users or not secret:
        AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not secret:
            secret = secrets.token_urlsafe(32)
        if not users:
            users["admin"] = "admin123"
        AUTH_FILE.write_text(
            json.dumps({"users": users, "secret": secret}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            AUTH_FILE.chmod(0o600)
        except OSError:
            pass

    return {"users": users, "secret": secret}


def _auth_config() -> dict:
    """缓存属性模式的认证配置加载"""
    if not hasattr(_auth_config, "_cache"):
        setattr(_auth_config, "_cache", _load_auth_config())
    return getattr(_auth_config, "_cache")


def _sign_session(username: str, expires_at: int) -> str:
    secret = _auth_config()["secret"].encode("utf-8")
    payload = f"{username}:{expires_at}"
    signature = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def _verify_session(token: str) -> bool:
    if not token:
        return False
    parts = token.split(":")
    if len(parts) != 3:
        return False
    username, expires_at_raw, signature = parts
    try:
        expires_at = int(expires_at_raw)
    except ValueError:
        return False
    if expires_at < int(time.time()):
        return False
    expected = _sign_session(username, expires_at).rsplit(":", 1)[-1]
    users = _auth_config().get("users", {})
    return hmac.compare_digest(signature, expected) and username in users


def _is_authenticated(request: Request) -> bool:
    return _verify_session(request.cookies.get(AUTH_COOKIE_NAME, ""))


def _auth_exempt_path(path: str) -> bool:
    return path in {"/login", "/auth/login", "/auth/logout", "/auth/token-login", "/health"} or path.startswith("/favicon") or path == "/user-images/download"


def _wants_html(request: Request) -> bool:
    if request.url.path in {"/", "/docs", "/redoc"}:
        return True
    return "text/html" in request.headers.get("accept", "")


# ---------- 用户辅助函数 ----------


def _get_current_user(request: Request) -> str:
    """从认证 cookie 中提取用户名作为 user_id"""
    token = request.cookies.get(AUTH_COOKIE_NAME, "")
    parts = token.split(":")
    uid = parts[0] if parts and parts[0] else ""
    return uid or "default"


def _require_admin(request: Request):
    if _get_current_user(request) != "admin":
        raise HTTPException(403, "只有 admin 用户可以访问设置")
