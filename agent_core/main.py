"""桌面 AI 智能体 —— FastAPI 服务器入口"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import webbrowser
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, Response, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn

# 确保能找到 agent_core 内的模块
sys.path.insert(0, str(Path(__file__).parent))


def _app_base_dir() -> Path:
    """Return project root in source mode and PyInstaller resource root when frozen."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).parent.parent

from logger import setup_logging, get_logger

logger = get_logger(__name__)

from config import AgentConfig
from agent import DesktopAgent
from tools import file_tools, code_tools, system_tools, web_tools, memory_tools, git_tools, database_tool
import subagents
from monitoring.usage_tracker import get_tracker
from skills.registry import get_registry
from memory.local_memory import get_memory
import session_store
import user_manager
from wechat_bot import WeChatBot

# ---------- 认证 ----------

AUTH_COOKIE_NAME = "desktop_agent_session"
AUTH_SESSION_SECONDS = 60 * 60 * 24 * 7
AUTH_FILE = Path.home() / ".desktop_agent" / "auth.json"

# URL token 登录：token 有效期（默认 5 分钟）
LOGIN_TOKEN_EXPIRY_SECONDS = 5 * 60


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
        logger.info("检测到 AGENT_USERS 环境变量，正在合并用户配置")
        for pair in env_users.split(";"):
            pair = pair.strip()
            if ":" in pair:
                u, p = pair.split(":", 1)
                users[u.strip()] = p.strip()
        logger.info("当前已加载用户: %s", list(users.keys()))
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

# ---------- FastAPI ----------
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    setup_logging()
    logger.info("🔄 服务启动中...（Agent 将在首次请求时初始化）")
    _init_default_users()
    yield

app = FastAPI(
    title="Desktop Agent",
    description="桌面 AI 智能体 API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 当前活跃的 Python 工具进度 WebSocket 连接
_active_tool_progress_ws: set[WebSocket] = set()


@app.middleware("http")
async def require_login(request: Request, call_next):
    if _auth_exempt_path(request.url.path) or _is_authenticated(request):
        return await call_next(request)
    if _wants_html(request):
        return Response(status_code=302, headers={"Location": "/login"})
    return Response(
        json.dumps({"detail": "未登录或登录已过期"}, ensure_ascii=False),
        status_code=401,
        media_type="application/json",
    )

# 挂载桌面 UI 静态文件（先定义 API 路由，再挂载静态文件）
UI_DIR = _app_base_dir() / "desktop"
_html_content: Optional[str] = None
if UI_DIR.exists():
    ui_index = UI_DIR / "index.html"
    if ui_index.exists():
        _html_content = ui_index.read_text(encoding="utf-8")
        logger.info("📁 桌面 UI: %s", UI_DIR / "index.html")

# 挂载静态文件目录，使 index.html 中的 /static/libs/、/static/styles/、/static/js/ 可访问
app.mount("/static", StaticFiles(directory=str(UI_DIR), html=True), name="static")

# 给静态文件添加 no-cache 头，避免浏览器缓存旧版本 JS/CSS
@app.middleware("http")
async def add_no_cache_static(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

# ---------- Agent 实例 ----------

agent: Optional[DesktopAgent] = None


def init_agent():
    global agent
    
    config = AgentConfig.load()
    
    # 初始化工作区
    file_tools.set_workspace(Path(config.workspace))
    git_tools.set_workspace(Path(config.workspace))
    web_tools.configure_search(
        tavily_search_enabled=config.tavily_search_enabled,
        tavily_api_key=config.tavily_api_key,
        tavily_search_url=config.tavily_search_url,
        anysearch_api_key=config.anysearch_api_key,
    )
    
    # 注册所有工具
    all_tools = []
    all_tools.extend(file_tools.TOOLS)
    all_tools.extend(code_tools.TOOLS)
    all_tools.extend(system_tools.TOOLS)
    all_tools.extend(web_tools.TOOLS)
    all_tools.extend(memory_tools.TOOLS)
    all_tools.extend(git_tools.TOOLS)
    all_tools.extend(subagents.TOOLS)
    all_tools.extend(database_tool.TOOLS)
    subagents.manager.configure(config, all_tools)
    
    # 先加载 Skills，再构建 Agent graph；set_tools 会把技能块注入 system prompt。
    app_base = _app_base_dir()
    skills_dirs = [
        Path(config.skills_dir),
        app_base / "skills",
        app_base / ".claude" / "skills",
        app_base / ".agents" / "skills",
    ]
    skills_count = get_registry().load_from(skills_dirs)

    # 初始化 Agent
    agent = DesktopAgent(config)
    agent.set_tools(all_tools)
    
    logger.info("✅ Agent 初始化完成")
    logger.info("  模型: %s", config.model)
    logger.info("  工作区: %s", config.workspace)
    logger.info("  Skills 目录: %s", ", ".join(str(p) for p in skills_dirs))
    logger.info("  已加载技能: %d 个", skills_count)

    # 初始化微信 Bot（全局单例）
    app.state.wechat_bot = WeChatBot(agent)

# ---------- API 模型 ----------

class AttachmentRequest(BaseModel):
    name: str = "pasted-image.png"
    mime_type: str = "image/png"
    data_url: str


class RunRequest(BaseModel):
    message: str
    thread_id: str = "default"
    attachments: list[AttachmentRequest] = Field(default_factory=list)


class RunResponse(BaseModel):
    result: str
    steps: list[dict] = []


class SkillInfo(BaseModel):
    name: str
    description: str
    triggers: list[str] = []
    has_instructions: bool = False
    format: str = "desktop-agent"
    source: str = ""
    mcp_declared: bool = False


class SkillFileEntry(BaseModel):
    path: str
    size: int = 0
    kind: str = "file"   # file | dir


class SkillDetail(BaseModel):
    name: str
    description: str
    triggers: list[str] = []
    instructions: str = ""
    format: str = "desktop-agent"
    source: str = ""
    mcp_declared: bool = False
    tools_required: list[str] = []
    files: list[SkillFileEntry] = []


class SkillFileContent(BaseModel):
    name: str
    file_path: str
    content: str
    size: int
    truncated: bool = False


class SubagentTaskInfo(BaseModel):
    id: str
    agent_type: str
    task: str
    context: str = ""
    status: str
    result: str = ""
    error: str = ""
    created_at: float
    started_at: float = 0
    finished_at: float = 0


class UsageStats(BaseModel):
    date: str
    total_calls: int
    model_calls: int = 0
    tool_calls: int = 0
    total_input_tokens: int
    total_output_tokens: int
    total_cached_tokens: int = 0
    total_tokens: int
    total_cost: float
    provider_breakdown: dict[str, Any] = {}
    model_breakdown: dict[str, Any] = {}
    tool_breakdown: dict[str, Any] = {}
    session_records: int = 0


class SessionStats(BaseModel):
    session_id: str
    calls: int
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost: float
    provider_breakdown: dict[str, Any] = {}
    tool_breakdown: dict[str, Any] = {}


class ReloadResponse(BaseModel):
    message: str
    count: int


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class MemoryRequest(BaseModel):
    key: str
    value: Any


# ---------- 桌面 UI 路由 ----------

from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse


LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Desktop Agent 登录</title>
<style>
* { box-sizing:border-box; }
html, body { filter:none !important; opacity:1 !important; }
body { margin:0; min-height:100vh; display:grid; place-items:center; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:#f5f5f7 !important; color:#1d1d1f; }
.login { position:relative; z-index:2147483647; width:min(380px, calc(100vw - 32px)); background:#fff; border:1px solid #e5e5ea; border-radius:10px; padding:28px; box-shadow:0 18px 50px rgba(0,0,0,.08); }
.lang { position:fixed; top:16px; right:16px; z-index:2147483647; height:34px; border:1px solid #d2d2d7; border-radius:8px; padding:0 8px; background:#fff; color:#1d1d1f; }
h1 { margin:0 0 6px; font-size:24px; }
p { margin:0 0 22px; color:#6e6e73; font-size:14px; }
label { display:block; margin:14px 0 6px; font-size:13px; color:#515154; }
input { width:100%; height:40px; border:1px solid #d2d2d7; border-radius:8px; padding:0 12px; font-size:14px; outline:none; }
input:focus { border-color:#007aff; box-shadow:0 0 0 3px rgba(0,122,255,.12); }
button { width:100%; height:42px; margin-top:20px; border:0; border-radius:8px; background:#007aff; color:#fff; font-weight:600; font-size:15px; cursor:pointer; }
button:disabled { opacity:.65; cursor:not-allowed; }
.error { min-height:18px; margin-top:12px; color:#d70015; font-size:13px; }
</style>
</head>
<body>
<select class="lang" id="language-select" aria-label="Language">
  <option value="zh-CN">中文</option>
  <option value="en">English</option>
</select>
<form class="login" onsubmit="login(event)">
  <h1>Desktop Agent</h1>
  <p data-i18n="subtitle">请登录后继续操作</p>
  <label for="username" data-i18n="username">用户名</label>
  <input id="username" autocomplete="username" value="admin" autofocus>
  <label for="password" data-i18n="password">密码</label>
  <input id="password" type="password" autocomplete="current-password">
  <button id="submit" type="submit" data-i18n="login">登录</button>
  <div class="error" id="error"></div>
</form>
<script>
const I18N = {
  'zh-CN': {
    title: '智能体助手登录',
    subtitle: '请登录后继续操作',
    username: '用户名',
    password: '密码',
    login: '登录',
    invalid: '用户名或密码错误',
    network: '网络错误，请稍后重试',
  },
  en: {
    title: 'Desktop Agent Login',
    subtitle: 'Sign in to continue',
    username: 'Username',
    password: 'Password',
    login: 'Log in',
    invalid: 'Incorrect username or password',
    network: 'Network error. Please try again later.',
  },
};
let currentLanguage = localStorage.getItem('desktop-agent-language') || ((navigator.language || '').toLowerCase().startsWith('zh') ? 'zh-CN' : 'en');
if (!I18N[currentLanguage]) currentLanguage = 'zh-CN';
function t(key) {
  return (I18N[currentLanguage] && I18N[currentLanguage][key]) || I18N['zh-CN'][key] || key;
}
function applyI18n() {
  document.documentElement.lang = currentLanguage;
  document.title = t('title');
  document.querySelectorAll('[data-i18n]').forEach(el => {
    el.textContent = t(el.dataset.i18n);
  });
  document.getElementById('language-select').value = currentLanguage;
}
document.getElementById('language-select').addEventListener('change', (event) => {
  currentLanguage = event.target.value;
  localStorage.setItem('desktop-agent-language', currentLanguage);
  applyI18n();
});
applyI18n();

function clearOverlays() {
  document.documentElement.style.filter = 'none';
  document.documentElement.style.opacity = '1';
  document.body.style.filter = 'none';
  document.body.style.opacity = '1';
  document.querySelectorAll('.modal-overlay, #sidebar-overlay, .overlay, .backdrop').forEach(el => el.remove());
  Array.from(document.body.children).forEach(el => {
    if (el.classList.contains('login')) return;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    const coversViewport = rect.width >= window.innerWidth * 0.9 && rect.height >= window.innerHeight * 0.9;
    const overlaysPage = ['fixed', 'absolute'].includes(style.position) && coversViewport;
    if (overlaysPage) el.remove();
  });
}
clearOverlays();
window.addEventListener('pageshow', clearOverlays);

async function login(event) {
  event.preventDefault();
  const btn = document.getElementById('submit');
  const err = document.getElementById('error');
  btn.disabled = true;
  err.textContent = '';
  try {
    const res = await fetch('/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        username: document.getElementById('username').value,
        password: document.getElementById('password').value,
      }),
    });
    if (res.ok) {
      location.href = '/';
    } else {
      const data = await res.json().catch(() => ({}));
      err.textContent = currentLanguage === 'en' ? t('invalid') : (data.detail || t('invalid'));
    }
  } catch {
    err.textContent = t('network');
  } finally {
    btn.disabled = false;
  }
}
</script>
</body>
</html>"""


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page():
    return HTMLResponse(LOGIN_HTML)


@app.post("/auth/login")
def auth_login(req: LoginRequest, response: Response):
    auth = _auth_config()
    users = auth.get("users", {})
    expected_pwd = users.get(req.username)
    if not expected_pwd or not hmac.compare_digest(req.password, expected_pwd):
        raise HTTPException(401, "用户名或密码错误")
    expires_at = int(time.time()) + AUTH_SESSION_SECONDS
    response.set_cookie(
        AUTH_COOKIE_NAME,
        _sign_session(req.username, expires_at),
        max_age=AUTH_SESSION_SECONDS,
        httponly=True,
        samesite="lax",
        secure=os.getenv("DESKTOP_AGENT_AUTH_COOKIE_SECURE", "0") == "1",
    )
    return {"status": "ok"}


@app.post("/auth/logout")
def auth_logout(response: Response):
    response.delete_cookie(AUTH_COOKIE_NAME)
    return {"status": "ok"}


@app.post("/auth/change-password")
def auth_change_password(req: "ChangePasswordRequest", request: Request):
    """修改当前登录用户的密码"""
    uid = _get_current_user(request)
    auth = _auth_config()
    users = auth.get("users", {})
    expected = users.get(uid)
    if not expected or not hmac.compare_digest(req.current_password, expected):
        raise HTTPException(403, "当前密码错误")
    if len(req.new_password) < 4:
        raise HTTPException(400, "新密码至少需要 4 个字符")
    users[uid] = req.new_password
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(json.dumps(auth, ensure_ascii=False, indent=2), encoding="utf-8")
    if hasattr(_auth_config, "_cache"):
        try:
            del _auth_config._cache
        except AttributeError:
            pass
    return {"status": "ok"}


# ---------- URL Token 免密登录 ----------

LOGIN_TOKEN_ERROR_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>登录失败 - Desktop Agent</title>
<style>
* { box-sizing:border-box; }
body { margin:0; min-height:100vh; display:grid; place-items:center; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:#f5f5f7; color:#1d1d1f; }
.card { width:min(380px, calc(100vw - 32px)); background:#fff; border:1px solid #e5e5ea; border-radius:10px; padding:28px; text-align:center; box-shadow:0 18px 50px rgba(0,0,0,.08); }
h1 { font-size:22px; margin:0 0 8px; }
p { color:#6e6e73; font-size:14px; margin:0 0 20px; }
a { display:inline-block; padding:10px 28px; border-radius:8px; background:#007aff; color:#fff; text-decoration:none; font-size:14px; }
</style>
</head>
<body>
<div class="card">
  <h1>登录链接无效或已过期</h1>
  <p>请重新获取登录链接，或使用密码登录</p>
  <a href="/login">前往密码登录</a>
</div>
</body>
</html>"""


@app.get("/auth/token-login", include_in_schema=False)
def auth_token_login(token: str = "", response: Response = None):
    """URL token 免密登录：验证 token，设置会话 cookie，跳转至主页"""
    if not token:
        return HTMLResponse(LOGIN_TOKEN_ERROR_HTML, status_code=400)

    if not _verify_session(token):
        return HTMLResponse(LOGIN_TOKEN_ERROR_HTML, status_code=401)

    # token 写入的部分就是 username:expires_at:signature，直接从中提取用户名
    username = token.split(":")[0]
    expires_at = int(time.time()) + AUTH_SESSION_SECONDS
    response.set_cookie(
        AUTH_COOKIE_NAME,
        _sign_session(username, expires_at),
        max_age=AUTH_SESSION_SECONDS,
        httponly=True,
        samesite="lax",
        secure=os.getenv("DESKTOP_AGENT_AUTH_COOKIE_SECURE", "0") == "1",
    )
    response.status_code = 302
    response.headers["Location"] = "/"
    return None


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def serve_ui():
    """提供桌面 UI"""
    if _html_content:
        headers = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
        return HTMLResponse(_html_content, headers=headers)
    return HTMLResponse("<h1>Desktop Agent API</h1><p>UI not found. Use /docs for API docs.</p>")


# ---------- API 路由 ----------

def _get_current_user(request: Request) -> str:
    """从认证 cookie 中提取用户名作为 user_id"""
    token = request.cookies.get(AUTH_COOKIE_NAME, "")
    parts = token.split(":")
    uid = parts[0] if parts and parts[0] else ""
    return uid or "default"


def _require_admin(request: Request):
    if _get_current_user(request) != "admin":
        raise HTTPException(403, "只有 admin 用户可以访问设置")


def _workspace_for_user(uid: str) -> Path:
    return Path(user_manager.user_workspace(uid)).expanduser().resolve()


def _resolve_artifact_path(uid: str, path: str) -> Path:
    workspace = _workspace_for_user(uid)
    try:
        raw = Path(path or "").expanduser()
    except (RuntimeError, OSError):
        # expanduser 可能因 HOME 未设置而失败，此时按绝对路径处理
        raw = Path(path or "")
    target = raw if raw.is_absolute() else workspace / raw
    target = target.resolve(strict=False)
    try:
        target.relative_to(workspace)
    except ValueError as exc:
        raise HTTPException(403, "只能下载当前用户工作区内的文件") from exc
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "文件不存在")
    return target


def _relative_artifact_path(uid: str, path: str) -> str:
    target = _resolve_artifact_path(uid, path)
    return target.relative_to(_workspace_for_user(uid)).as_posix()


def _artifact_link(path: str) -> str:
    name = Path(path).name or path
    encoded = quote(path)
    download = f"[下载](/artifacts/download?path={encoded})"
    if Path(path).suffix.lower() in {".md", ".markdown"}:
        preview = f"[预览](#artifact-preview:{encoded})"
        return f"- {name}: {preview} / {download} (`{path}`)"
    return f"- {name}: {download} (`{path}`)"


def _artifact_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for match in re.finditer(r"`([^`\n]+?\.[A-Za-z0-9]{1,8})`", text or ""):
        candidates.append(match.group(1).strip())
    for token in re.split(r"[\s\n\r\t，。；;：:、（）()\[\]{}<>]+", text or ""):
        token = token.strip("`'\"")
        if re.search(r"\.[A-Za-z0-9]{1,8}$", token):
            candidates.append(token)
    return candidates


def _append_artifact_links(content: str, uid: str, paths: Optional[list[str]] = None) -> str:
    cleaned_content = _strip_existing_artifact_section(content)
    found: list[str] = []
    seen: set[str] = set()
    for candidate in (paths or []) + _artifact_candidates(cleaned_content):
        try:
            rel = _relative_artifact_path(uid, candidate)
        except Exception:
            continue
        if rel not in seen:
            seen.add(rel)
            found.append(rel)
    if not found:
        return content
    links = "\n".join(_artifact_link(path) for path in found)
    return f"{cleaned_content.rstrip()}\n\n---\n\n可下载文件：\n{links}"


def _strip_existing_artifact_section(content: str) -> str:
    """Remove model-generated download sections so the normalized one appears once."""
    lines = (content or "").splitlines()
    result: list[str] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx].strip()
        if re.fullmatch(r"-{3,}", line):
            next_idx = idx + 1
            while next_idx < len(lines) and not lines[next_idx].strip():
                next_idx += 1
            next_line = lines[next_idx].strip() if next_idx < len(lines) else ""
            if re.match(r"^可下载文件[:：]?$", next_line):
                idx = next_idx + 1
                while idx < len(lines):
                    current = lines[idx].strip()
                    if re.fullmatch(r"-{3,}", current):
                        break
                    if current and not current.startswith(("-", "*")) and not re.match(r"^可下载文件[:：]?$", current):
                        break
                    idx += 1
                continue
        if re.match(r"^可下载文件[:：]?$", line):
            idx += 1
            while idx < len(lines):
                current = lines[idx].strip()
                if current and not current.startswith(("-", "*")):
                    break
                idx += 1
            continue
        result.append(lines[idx])
        idx += 1
    return "\n".join(result).rstrip()


async def _ensure_session(uid: str, session_id: str) -> dict:
    session = session_store.get_session(uid, session_id)
    if session is None:
        session = session_store.create_session(
            uid, title=f"会话 {session_id[:8]}",
            session_id=session_id,
        )
    return session or {}


def _is_skill_inventory_query(message: str) -> bool:
    """判断是否为「查询已加载 Skills」的明确请求。只匹配精确短语，避免误触发。"""
    text = (message or "").strip().lower()
    if not text or len(text) > 60:
        return False
    exact_phrases = {
        "你有哪些技能", "技能列表", "你的技能列表", "what skills do you have", "list skills",
        "list your skills", "show skills", "show your skills", "列出技能", "加载了哪些技能",
        "加载了哪些技能", "已加载的技能", "skills list",
    }
    return text.rstrip("?.！。？") in exact_phrases


def _safe_attachments(attachments: list[AttachmentRequest]) -> list[dict]:
    safe: list[dict] = []
    for item in attachments[:4]:
        mime = (item.mime_type or "").strip().lower()
        data_url = (item.data_url or "").strip()
        is_zip = mime in ("application/zip", "application/x-zip-compressed") or item.name.endswith(".zip")
        prefix = "data:application/zip;base64," if is_zip else f"data:{mime};base64,"

        if not data_url.startswith(prefix):
            if not is_zip:
                continue  # 非 ZIP 非图片，跳过
            # ZIP 用更宽松的 base64 检测
            if not data_url.startswith("data:") or ";base64," not in data_url:
                continue
            # 取实际 base64 数据
            base64_data = data_url.split(";base64,", 1)[-1]
            try:
                raw = base64.b64decode(base64_data)
            except Exception:
                continue
        else:
            base64_data = data_url[len(prefix):]
            try:
                raw = base64.b64decode(base64_data)
            except Exception:
                continue

        if is_zip:
            if len(raw) > 50 * 1024 * 1024:  # ZIP 上限 50MB
                continue
            safe.append({
                "name": item.name or "project.zip",
                "mime_type": "application/zip",
                "data_url": data_url,
                "raw": raw,
            })
        else:
            if not mime.startswith("image/") or len(data_url) > 8 * 1024 * 1024:
                continue
            safe.append({
                "name": item.name or "image.png",
                "mime_type": mime,
                "data_url": data_url,
            })
    return safe


def _extract_zip(raw: bytes, zip_name: str) -> tuple[str, str]:
    """解压 ZIP 字节到工作区 .agent_zip/{name}/ 目录，返回 (目录绝对路径, 文件清单文本)"""
    import hashlib
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', Path(zip_name).stem)[:32]
    dest = Path.home() / "agent_workspace" / ".agent_zip" / f"{safe_name}_{hashlib.md5(raw[:1024]).hexdigest()[:8]}"
    dest.mkdir(parents=True, exist_ok=True)

    tree: list[str] = []
    try:
        with zipfile.ZipFile(BytesIO(raw)) as zf:
            for info in zf.infolist():
                fname = info.filename
                if fname.startswith("/") or ".." in fname:
                    continue  # 路径穿越防护
                target = dest / fname
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    tree.append(f"[DIR]  {fname}")
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    zf.extract(info, dest)
                    size = info.file_size
                    size_str = f"{size/1024:.1f}KB" if size < 1024*1024 else f"{size/1024/1024:.1f}MB"
                    tree.append(f"[FILE] {fname}  ({size_str})")
        # 限制文件列表长度
        if len(tree) > 500:
            tree = tree[:500] + [f"...（共 {len(tree)} 个文件，仅展示前 500 个）"]
    except zipfile.BadZipFile:
        return "", "❌ ZIP 文件损坏，无法解压。"
    except Exception as e:
        return "", f"❌ 解压失败: {type(e).__name__}: {e}"

    manifest = (
        f"用户上传了 ZIP 文件「{zip_name}」，已解压到工作区 .agent_zip/ 目录。\n"
        f"解压路径: {dest}\n\n"
        f"文件清单（共 {len(tree)} 个条目）:\n" + "\n".join(tree)
    )
    return str(dest), manifest


def _display_user_message(uid: str, message: str, attachments: list[dict]) -> str:
    """生成用户消息的存储文本。有图片/压缩包时保存到磁盘，返回摘要信息。"""
    if not attachments:
        return message

    # ── 先处理 ZIP ──
    zip_notes = ""
    for item in attachments:
        if item.get("mime_type") == "application/zip" and "raw" in item:
            name = item.get("name", "project.zip")
            raw = item.pop("raw")  # 移除原始字节，不保留
            dest, manifest = _extract_zip(raw, name)
            if dest:
                zip_notes = manifest
            break

    if zip_notes:
        return json.dumps({"text": message or "请分析这个项目。", "zip_manifest": zip_notes}, ensure_ascii=False)

    # ── 图片处理 ──
    image_paths: list[str] = []
    img_dir = Path.home() / ".desktop_agent" / "user_images" / uid
    img_dir.mkdir(parents=True, exist_ok=True)
    for item in attachments:
        data_url = item.get("data_url", "")
        mime_type = item.get("mime_type", "image/png")
        prefix = f"data:{mime_type};base64,"
        if not data_url.startswith(prefix):
            continue
        try:
            raw = base64.b64decode(data_url[len(prefix):])
        except Exception:
            continue
        ext = mime_type.split("/")[-1] or "png"
        h = hashlib.sha1(raw).hexdigest()[:16]
        fname = f"{item.get('name', 'img')}_{h}.{ext}"
        fpath = img_dir / fname
        fpath.write_bytes(raw)
        image_paths.append(str(fpath))
    if not image_paths:
        return message or "请分析这些图片。"
    payload = {
        "text": message or "请分析这些图片。",
        "images": image_paths,
    }
    return json.dumps(payload, ensure_ascii=False)


def _image_model_override(attachments: list[dict]) -> str:
    if not attachments or not agent:
        return ""
    cfg = agent.config
    model = (cfg.model or "").strip().lower()
    if "mimo" in model and model not in {"mimo-v2.5", "mimo-v2-omni"}:
        return "mimo-v2.5"
    return ""


def _format_loaded_skills() -> str:
    skills = sorted(get_registry().list_all(), key=lambda item: item.name)
    if not skills:
        return "当前没有加载任何 Skills。"

    lines = [
        f"当前已加载 {len(skills)} 个 Skills：",
        "",
    ]
    for skill in skills:
        triggers = "、".join(skill.triggers[:8]) if skill.triggers else "未声明"
        mcp_note = "；声明 MCP（当前仅识别，不执行）" if "mcp" in skill.metadata else ""
        lines.append(f"- **{skill.name}**：{skill.description or '无描述'}")
        lines.append(f"  触发词：{triggers}；来源：`{skill.root}`{mcp_note}")
    lines.extend([
        "",
        "另外，我也有文件读写、Python 执行、网页搜索/抓取、系统信息、长期记忆等底层工具能力。",
    ])
    return "\n".join(lines)


def _save_assistant_result(uid: str, session_id: str, user_message: str, result: str, steps: Optional[list[dict]] = None):
    content = result
    if steps:
        content = json.dumps({"text": result, "steps": steps}, ensure_ascii=False)
    session_store.add_message(uid, session_id, "assistant", content)
    title = user_message[:30] + ("..." if len(user_message) > 30 else "")
    session_store.rename_session(uid, session_id, title or f"会话 {session_id[:8]}")


def _resolve_user(request: Request) -> str:
    """从请求获取当前用户并设置到 agent"""
    uid = _get_current_user(request)
    file_tools.set_workspace(_workspace_for_user(uid))
    if agent:
        agent.set_user(uid)
    # 设置数据库交互上下文（角色和用户信息，后续可从用户配置扩展）
    try:
        from user_manager import get_user
        user = get_user(uid) or {}
        role = user.get("role", "")
        user_context = {"user_id": uid, "role": role, **(user.get("context", {}) or {})}
        database_tool.set_db_context(role=role, user_context=user_context)
    except Exception:
        database_tool.set_db_context()  # fallback：无上下文
    return uid


@app.post("/run", response_model=RunResponse)
async def run_agent(req: RunRequest, request: Request):
    """发送消息给 Agent 并获取回复"""
    if not agent:
        init_agent()
    if not agent:
        logger.error("Agent 初始化失败，请检查 API Key 设置")
        raise HTTPException(503, "Agent 初始化失败，请检查 API Key 设置")
    
    uid = _resolve_user(request)
    session_id = req.thread_id
    session = await _ensure_session(uid, session_id)
    history_messages = session.get("messages", [])

    attachments = _safe_attachments(req.attachments)
    display_text = _display_user_message(uid, req.message, attachments)
    session_store.add_message(uid, session_id, "user", display_text)
    model_override = _image_model_override(attachments)
    # ── 解析 ZIP 清单，追加到 LLM 消息中 ──
    agent_message = req.message
    if attachments and any(a.get("mime_type") == "application/zip" for a in attachments):
        try:
            parsed = json.loads(display_text)
            manifest = parsed.get("zip_manifest", "")
            if manifest:
                agent_message = req.message + "\n\n" + manifest
        except (json.JSONDecodeError, TypeError):
            pass
    # ── 解析图片下载地址，追加到 LLM 消息中供图生图模型使用 ──
    if attachments and any(a.get("mime_type", "").startswith("image/") for a in attachments):
        try:
            parsed = json.loads(display_text)
            img_paths = parsed.get("images", [])
            if img_paths:
                img_urls = _user_image_urls(uid, img_paths, request)
                url_lines = "\n".join(f"- {url}" for url in img_urls)
                agent_message += f"\n\n[上传的图片已在服务器保存，以下为图片下载地址可供图生图模型使用：]\n{url_lines}"
        except (json.JSONDecodeError, TypeError):
            pass
    if _is_skill_inventory_query(req.message):
        result = _format_loaded_skills()
        _save_assistant_result(uid, session_id, req.message, result)
        return RunResponse(result=result, steps=[])

    agent.switch_thread(session_id)
    result, steps = await agent.run(
        agent_message,
        history=history_messages,
        attachments=attachments,
        model_override=model_override,
    )
    artifact_paths = [
        str(step.get("args", {}).get("path", ""))
        for step in steps
        if step.get("type") == "tool_call"
        and step.get("tool") in {"write_file", "append_to_file"}
        and isinstance(step.get("args"), dict)
        and step.get("args", {}).get("path")
    ]
    result = _append_artifact_links(result, uid, artifact_paths)
    _save_assistant_result(uid, session_id, req.message, result)
    
    # 后台反思
    asyncio.create_task(_async_reflect(uid, req.message, steps, result))
    
    return RunResponse(result=result, steps=steps)


async def _async_reflect(uid: str, user_message: str, steps: list[dict], result: str):
    """后台任务反思，总结可复用模式并存入长期记忆。"""
    try:
        if not agent:
            return
        reflection = await agent.reflect_on_task(user_message, steps, result)
        if reflection:
            from memory.local_memory import get_memory
            key = f"_learned_{hashlib.md5(reflection.encode()).hexdigest()[:12]}"
            mem = get_memory(uid)
            existing = mem.get(key)
            if existing is None:  # 不覆盖已有记录
                mem.set(key, reflection)
    except Exception:
        pass


@app.post("/run/stream")
async def run_agent_stream(req: RunRequest, request: Request):
    """流式处理消息（SSE）"""
    if not agent:
        init_agent()
    if not agent:
        raise HTTPException(503, "Agent 初始化失败")
    
    uid = _resolve_user(request)
    session_id = req.thread_id
    session = await _ensure_session(uid, session_id)
    history_messages = session.get("messages", [])

    attachments = _safe_attachments(req.attachments)
    display_text = _display_user_message(uid, req.message, attachments)
    session_store.add_message(uid, session_id, "user", display_text)
    model_override = _image_model_override(attachments)

    # ── 解析 ZIP 清单，追加到 LLM 消息中 ──
    agent_message = req.message
    if attachments and any(a.get("mime_type") == "application/zip" for a in attachments):
        try:
            parsed = json.loads(display_text)
            manifest = parsed.get("zip_manifest", "")
            if manifest:
                agent_message = req.message + "\n\n" + manifest
        except (json.JSONDecodeError, TypeError):
            pass
    # ── 解析图片下载地址，追加到 LLM 消息中供图生图模型使用 ──
    if attachments and any(a.get("mime_type", "").startswith("image/") for a in attachments):
        try:
            parsed = json.loads(display_text)
            img_paths = parsed.get("images", [])
            if img_paths:
                img_urls = _user_image_urls(uid, img_paths, request)
                url_lines = "\n".join(f"- {url}" for url in img_urls)
                agent_message += f"\n\n[上传的图片已在服务器保存，以下为图片下载地址可供图生图模型使用：]\n{url_lines}"
        except (json.JSONDecodeError, TypeError):
            pass
    if _is_skill_inventory_query(req.message):
        result = _format_loaded_skills()
        _save_assistant_result(uid, session_id, req.message, result)

        async def skill_inventory_stream():
            yield f"data: {json.dumps({'type': 'done', 'content': result}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            skill_inventory_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    agent.switch_thread(session_id)
    artifact_paths: list[str] = []
    collected_steps: list[dict] = []  # 收集步骤卡片数据，将存入历史
    
    async def event_stream():
        final_content = ""
        error_content = ""
        forwarded_terminal_event = False
        yielded_count = 0
        if model_override:
            yield f"data: {json.dumps({'type': 'model_switch', 'model': model_override, 'reason': '图片输入'}, ensure_ascii=False)}\n\n"
        stream = agent.stream_run(
            agent_message,
            history=history_messages,
            attachments=attachments,
            model_override=model_override,
        )
        try:
            async for sse_event in stream:
                if await request.is_disconnected():
                    logger.info("[run/stream] 客户端断开，停止事件流")
                    await stream.aclose()
                    return
                if sse_event.strip() == "data: [DONE]":
                    continue
                yielded_count += 1
                logger.debug("[run/stream] yield event: %s", sse_event[:80])
                if '"type": "tool_start"' in sse_event:
                    try:
                        m = re.search(r'data: ({.*})', sse_event)
                        if m:
                            data = json.loads(m.group(1))
                            args = data.get("args") or {}
                            if data.get("tool") in {"write_file", "append_to_file"} and args.get("path"):
                                artifact_paths.append(str(args["path"]))
                            collected_steps.append(data)  # 收集步骤
                    except Exception:
                        pass
                elif '"type": "tool_result"' in sse_event:
                    try:
                        m = re.search(r'data: ({.*})', sse_event)
                        if m:
                            collected_steps.append(json.loads(m.group(1)))
                    except Exception:
                        pass
                elif '"type": "thought"' in sse_event:
                    try:
                        m = re.search(r'data: ({.*})', sse_event)
                        if m:
                            collected_steps.append(json.loads(m.group(1)))
                    except Exception:
                        pass
                elif '"type": "subagent_start"' in sse_event or '"type": "subagent_end"' in sse_event:
                    try:
                        m = re.search(r'data: ({.*})', sse_event)
                        if m:
                            collected_steps.append(json.loads(m.group(1)))
                    except Exception:
                        pass
                if '"type": "done"' in sse_event:
                    m = re.search(r'data: ({.*})', sse_event)
                    if m:
                        final_content = json.loads(m.group(1)).get("content", "")
                    forwarded_terminal_event = True
                    continue
                if '"type": "error"' in sse_event:
                    m = re.search(r'data: ({.*})', sse_event)
                    if m:
                        error_content = json.loads(m.group(1)).get("content", "")
                    forwarded_terminal_event = True
                yield sse_event
        except asyncio.CancelledError:
            await stream.aclose()
            return
        except Exception as e:
            logger.exception("SSE 流异常: async for 循环内未捕获的异常")
            await stream.aclose()
            yield f"data: {json.dumps({'type': 'error', 'content': f'服务内部错误: {e}'}, ensure_ascii=False)}\n\n"
            return

        logger.info(
            "[run/stream] stream_run 返回: yielded_count=%d, final_content_len=%d, error_content_len=%d, forwarded_terminal=%s",
            yielded_count,
            len(final_content),
            len(error_content),
            forwarded_terminal_event,
        )
        try:
            final_content = final_content or ""
            if final_content:
                final_content = _append_artifact_links(final_content, uid, artifact_paths)
                logger.info("[run/stream] 发送 done: content_len=%d", len(final_content))
                yield f"data: {json.dumps({'type': 'done', 'content': final_content}, ensure_ascii=False)}\n\n"
                _save_assistant_result(uid, session_id, req.message, final_content, collected_steps)
            elif error_content:
                logger.info("[run/stream] 保存 error 结果: error_len=%d", len(error_content))
                _save_assistant_result(uid, session_id, req.message, "❌ " + error_content, collected_steps)
            elif artifact_paths:
                summary = _append_artifact_links("任务已完成，文件已保存。", uid, artifact_paths)
                logger.info("[run/stream] 发送 artifact 总结: %s", summary)
                yield f"data: {json.dumps({'type': 'done', 'content': summary}, ensure_ascii=False)}\n\n"
                _save_assistant_result(uid, session_id, req.message, summary, collected_steps)
            elif not forwarded_terminal_event:
                fallback = (
                    "任务已结束，但模型没有生成最终回答。"
                    "这通常发生在接近最大推理步数时，模型仍在继续调用工具。"
                    f"当前最大推理步数为 {agent.config.recursion_limit}，可以提高该值，或把任务拆小后重试。"
                )
                logger.info("[run/stream] 发送 fallback: %s", fallback)
                yield f"data: {json.dumps({'type': 'done', 'content': fallback}, ensure_ascii=False)}\n\n"
                _save_assistant_result(uid, session_id, req.message, fallback, collected_steps)
            else:
                logger.info("[run/stream] 已转发 terminal 事件，不再发送兜底")
        except Exception as e:
            logger.exception("SSE 流处理异常")
            err_msg = f"服务内部错误: {e}"
            yield f"data: {json.dumps({'type': 'done', 'content': err_msg}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
        # 后台反思
        asyncio.create_task(_async_reflect(uid, req.message, collected_steps, final_content or ""))
    
    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.get("/skills", response_model=list[SkillInfo])
def list_skills():
    """列出所有已加载的技能"""
    registry = get_registry()
    # 如果尚未加载技能，尝试加载
    if not registry.list_all():
        app_base = _app_base_dir()
        registry.load_from([
            Path(AgentConfig.load().skills_dir),
            app_base / "skills",
            app_base / ".claude" / "skills",
            app_base / ".agents" / "skills",
        ])
    return [
        SkillInfo(
            name=s.name,
            description=s.description,
            triggers=s.triggers,
            has_instructions=bool(s.instructions),
            format=s.format,
            source=str(s.root),
            mcp_declared="mcp" in s.metadata,
        )
        for s in registry.list_all()
    ]


@app.post("/skills/reload", response_model=ReloadResponse)
def reload_skills():
    """热加载所有技能"""
    if not agent:
        raise HTTPException(503, "Agent 尚未初始化")
    count = agent.reload_skills()
    return ReloadResponse(message=f"已重新加载 {count} 个技能", count=count)


# 技能详情读取相关常量
SKILL_FILE_PREVIEW_MAX_BYTES = 256 * 1024  # 单文件预览上限 256KB


def _list_skill_files(skill_root: Path) -> list[SkillFileEntry]:
    """列出技能目录下的所有文件（不含子目录展开），过滤隐藏/常见临时文件。"""
    entries: list[SkillFileEntry] = []
    if not skill_root or not skill_root.exists():
        return entries
    try:
        for item in sorted(skill_root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if item.name.startswith("."):
                continue
            rel = item.relative_to(skill_root).as_posix()
            if item.is_dir():
                entries.append(SkillFileEntry(path=rel + "/", size=0, kind="dir"))
                # 展开一层子目录（常用 scripts/、references/）
                try:
                    for sub in sorted(item.iterdir(), key=lambda p: p.name.lower()):
                        if sub.name.startswith("."):
                            continue
                        sub_rel = sub.relative_to(skill_root).as_posix()
                        if sub.is_file():
                            try:
                                entries.append(SkillFileEntry(
                                    path=sub_rel,
                                    size=min(sub.stat().st_size, 1024 * 1024 * 50),
                                    kind="file",
                                ))
                            except OSError:
                                entries.append(SkillFileEntry(path=sub_rel, size=0, kind="file"))
                except OSError:
                    pass
            else:
                try:
                    entries.append(SkillFileEntry(
                        path=rel,
                        size=min(item.stat().st_size, 1024 * 1024 * 50),
                        kind="file",
                    ))
                except OSError:
                    entries.append(SkillFileEntry(path=rel, size=0, kind="file"))
    except OSError:
        pass
    return entries


@app.get("/skills/{name}", response_model=SkillDetail)
def get_skill_detail(name: str):
    """获取技能详情（含 SKILL.md 正文和文件清单）"""
    registry = get_registry()
    skill = registry.get(name)
    if not skill:
        raise HTTPException(404, f"技能 {name} 不存在")
    return SkillDetail(
        name=skill.name,
        description=skill.description,
        triggers=skill.triggers,
        instructions=skill.instructions,
        format=skill.format,
        source=str(skill.root),
        mcp_declared="mcp" in skill.metadata,
        tools_required=skill.tools_required,
        files=_list_skill_files(skill.root),
    )


@app.get("/skills/{name}/files", response_model=SkillFileContent)
def get_skill_file(name: str, path: str = ""):
    """读取技能目录下的指定文件内容（仅在工作区/技能根目录内可读）。"""
    registry = get_registry()
    skill = registry.get(name)
    if not skill:
        raise HTTPException(404, f"技能 {name} 不存在")

    skill_root: Path = skill.root
    if not path:
        raise HTTPException(400, "path 不能为空")

    # 防止路径穿越
    target = (skill_root / path).resolve()
    try:
        target.relative_to(skill_root.resolve())
    except ValueError:
        raise HTTPException(403, f"路径超出技能目录: {path}")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, f"文件不存在: {path}")

    try:
        raw = target.read_bytes()
    except OSError as e:
        raise HTTPException(500, f"读取失败: {e}")

    truncated = False
    if len(raw) > SKILL_FILE_PREVIEW_MAX_BYTES:
        raw = raw[:SKILL_FILE_PREVIEW_MAX_BYTES]
        truncated = True

    # 文本/二进制区分
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        return SkillFileContent(
            name=skill.name, file_path=path, content=text, size=len(raw), truncated=truncated
        )

    try:
        file_size = target.stat().st_size
    except OSError:
        file_size = len(raw)

    return SkillFileContent(
        name=skill.name,
        file_path=path,
        content=text,
        size=file_size,
        truncated=truncated,
    )


@app.get("/subagents")
def list_subagents():
    """列出可用子代理类型。"""
    return {"items": subagents.manager.list_agent_types()}


@app.get("/subagents/tasks/{task_id}", response_model=SubagentTaskInfo)
def get_subagent_task(task_id: str):
    """查询子代理任务状态。第一版任务为同步执行，后续并行任务会复用该结构。"""
    item = subagents.manager.get_task(task_id)
    if not item:
        raise HTTPException(404, "子代理任务不存在")
    return SubagentTaskInfo(**item.__dict__)


# ---------- 会话路由 ----------

class SessionInfo(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int


class SessionListResponse(BaseModel):
    sessions: list[SessionInfo]
    current_id: str


class SessionMessagesResponse(BaseModel):
    id: str
    title: str
    messages: list[dict]


class RenameRequest(BaseModel):
    title: str


class CreateSessionResponse(BaseModel):
    id: str
    title: str


@app.get("/sessions", response_model=SessionListResponse)
def list_sessions(request: Request):
    """列出当前用户的会话"""
    uid = _get_current_user(request)
    raw = session_store.list_sessions(uid)
    sessions = [
        SessionInfo(
            id=s["id"],
            title=s.get("title", "未命名"),
            created_at=s.get("created_at", ""),
            updated_at=s.get("updated_at", ""),
            message_count=s.get("message_count", 0),
        )
        for s in raw
    ]
    # 返回会话列表中最新的会话 ID，如果没有则返回 "default"
    current_id = sessions[0].id if sessions else "default"
    return SessionListResponse(sessions=sessions, current_id=current_id)


@app.get("/sessions/{session_id}", response_model=SessionMessagesResponse)
def get_session(session_id: str, request: Request):
    """获取当前用户的会话消息"""
    uid = _get_current_user(request)
    session = session_store.get_session(uid, session_id)
    if not session:
        raise HTTPException(404, "会话不存在")
    return SessionMessagesResponse(
        id=session["id"],
        title=session.get("title", "未命名"),
        messages=session.get("messages", []),
    )


@app.post("/sessions", response_model=CreateSessionResponse)
def create_session(request: Request):
    """创建新会话"""
    uid = _get_current_user(request)
    session = session_store.create_session(uid)
    return CreateSessionResponse(id=session["id"], title=session["title"])


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str, request: Request):
    """删除会话"""
    uid = _get_current_user(request)
    ok = session_store.delete_session(uid, session_id)
    if not ok:
        raise HTTPException(404, "会话不存在")
    return {"status": "ok", "message": f"已删除会话 {session_id}"}


@app.put("/sessions/{session_id}/rename")
def rename_session(session_id: str, req: RenameRequest, request: Request):
    """重命名会话"""
    uid = _get_current_user(request)
    ok = session_store.rename_session(uid, session_id, req.title)
    if not ok:
        raise HTTPException(404, "会话不存在")
    return {"status": "ok", "message": f"已重命名为 {req.title}"}


@app.get("/usage", response_model=UsageStats)
def get_usage(request: Request):
    """获取今日模型使用量统计"""
    tracker = get_tracker(_get_current_user(request))
    return UsageStats(**tracker.get_today_stats())


@app.get("/usage/session", response_model=SessionStats)
def get_session_usage(thread_id: str = "", request: Request = None):
    """获取当前会话的模型使用量"""
    tracker = get_tracker(_get_current_user(request) if request else "default")
    return SessionStats(**tracker.get_session_stats(thread_id=thread_id or None))


@app.get("/usage/history")
def get_usage_history(days: int = 7, request: Request = None):
    """获取最近 N 天的使用历史"""
    tracker = get_tracker(_get_current_user(request) if request else "default")
    return tracker.get_history(days=days)


# ---------- 长期记忆路由 ----------

@app.get("/memories")
def list_memories(request: Request, q: str = ""):
    """列出或搜索当前用户的长期记忆"""
    uid = _get_current_user(request)
    memory = get_memory(uid)
    if q:
        return {"items": memory.list_items(), "query": q, "result": memory.search(q)}
    return {"items": memory.list_items()}


@app.post("/memories")
def save_memory(req: MemoryRequest, request: Request):
    """为当前用户保存一条长期记忆"""
    if not req.key.strip():
        raise HTTPException(400, "记忆 key 不能为空")
    uid = _get_current_user(request)
    memory = get_memory(uid)
    memory.set(req.key.strip(), req.value)
    return {"status": "ok", "message": f"已保存记忆 {req.key.strip()}"}


@app.delete("/memories/{key}")
def delete_memory(key: str, request: Request):
    """删除当前用户的一条长期记忆"""
    uid = _get_current_user(request)
    memory = get_memory(uid)
    memory.delete(key)
    return {"status": "ok", "message": f"已删除记忆 {key}"}


# ---------- 工作区文件制品 ----------

@app.get("/artifacts/download")
def download_artifact(path: str, request: Request):
    """下载当前用户工作区内的文件制品。"""
    uid = _get_current_user(request)
    target = _resolve_artifact_path(uid, path)
    return FileResponse(target, filename=target.name)


# ---------- 用户上传图片下载 ----------

USER_IMG_DIR = Path.home() / ".desktop_agent" / "user_images"


@app.get("/user-images/download")
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


def _user_image_urls(uid: str, image_paths: list[str], request: Request) -> list[str]:
    """将本地图片路径转换为可下载的 HTTP URL，供 LLM 工具使用。"""
    base_url = str(request.base_url).rstrip("/")
    urls: list[str] = []
    for fpath in image_paths:
        name = Path(fpath).name
        url = f"{base_url}/user-images/download?name={quote(name)}&uid={quote(uid)}"
        urls.append(url)
    return urls


@app.get("/artifacts/preview")
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


# ---------- 设置 / 配置路由 ----------

class SettingsRequest(BaseModel):
    """设置请求体"""
    active_provider: str = "openai"
    provider_name: str = ""
    api_key: str = ""
    model: str = ""
    base_url: str = ""
    recursion_limit: int = 60
    api_max_retries: int = 3
    api_timeout_seconds: float = 120.0
    api_host_ips: str = ""
    context_window_tokens: int = 0
    tavily_search_enabled: bool = False
    tavily_api_key: str = ""
    tavily_search_url: str = "https://api.tavily.com/search"
    anysearch_api_key: str = ""


@app.get("/settings")
def get_settings(request: Request):
    """获取当前设置"""
    _require_admin(request)
    cfg = AgentConfig.load()
    return cfg.to_api_dict()


@app.post("/settings")
def save_settings(req: SettingsRequest, request: Request):
    """保存设置并重启 Agent"""
    _require_admin(request)
    cfg = AgentConfig.load()
    
    cfg.update_provider(
        provider_id=req.active_provider,
        provider_name=req.provider_name,
        api_key=req.api_key,
        model=req.model,
        base_url=req.base_url,
    )
    cfg.recursion_limit = max(1, int(req.recursion_limit or 60))
    cfg.api_max_retries = max(0, int(req.api_max_retries or 0))
    cfg.api_timeout_seconds = max(60.0, float(req.api_timeout_seconds or 120.0))
    cfg.api_host_ips = req.api_host_ips or cfg.api_host_ips
    cfg.context_window_tokens = max(0, int(req.context_window_tokens or 0))
    cfg.tavily_search_enabled = bool(req.tavily_search_enabled)
    if req.tavily_api_key:
        cfg.tavily_api_key = req.tavily_api_key
    cfg.tavily_search_url = req.tavily_search_url or cfg.tavily_search_url or "https://api.tavily.com/search"
    if req.anysearch_api_key:
        cfg.anysearch_api_key = req.anysearch_api_key
    
    # 持久化到文件（现在包含 API Key）
    cfg.save()
    
    # 也设到环境变量（当前进程生效）
    os.environ["LLM_API_KEY"] = cfg.api_key
    os.environ["OPENAI_API_KEY"] = cfg.api_key
    os.environ["LLM_MODEL"] = cfg.model
    os.environ["LLM_PROVIDER"] = cfg.active_provider
    os.environ["AGENT_RECURSION_LIMIT"] = str(cfg.recursion_limit)
    os.environ["AGENT_API_MAX_RETRIES"] = str(cfg.api_max_retries)
    os.environ["AGENT_API_TIMEOUT_SECONDS"] = str(cfg.api_timeout_seconds)
    if cfg.api_host_ips:
        os.environ["AGENT_API_HOST_IPS"] = cfg.api_host_ips
    else:
        os.environ.pop("AGENT_API_HOST_IPS", None)
    if cfg.context_window_tokens:
        os.environ["AGENT_CONTEXT_WINDOW_TOKENS"] = str(cfg.context_window_tokens)
    else:
        os.environ.pop("AGENT_CONTEXT_WINDOW_TOKENS", None)
    os.environ["TAVILY_SEARCH_ENABLED"] = "1" if cfg.tavily_search_enabled else "0"
    if cfg.tavily_api_key:
        os.environ["TAVILY_API_KEY"] = cfg.tavily_api_key
    else:
        os.environ.pop("TAVILY_API_KEY", None)
    if cfg.tavily_search_url:
        os.environ["TAVILY_SEARCH_URL"] = cfg.tavily_search_url
    if cfg.anysearch_api_key:
        os.environ["ANYSEARCH_API_KEY"] = cfg.anysearch_api_key
    else:
        os.environ.pop("ANYSEARCH_API_KEY", None)
    if cfg.base_url:
        os.environ["LLM_BASE_URL"] = cfg.base_url
    else:
        os.environ.pop("LLM_BASE_URL", None)
    
    # 重启 Agent
    global agent
    agent = None
    try:
        init_agent()
        return {"status": "ok", "message": "设置已保存，Agent 已重新初始化"}
    except Exception as e:
        logger.exception("保存设置后 Agent 重新初始化失败")
        return {"status": "error", "message": f"设置已保存，但 Agent 初始化失败: {str(e)}"}


# ── 用户管理路由 ──

class UserInfo(BaseModel):
    id: str
    name: str
    role: str = ""
    created_at: str


class CreateUserRequest(BaseModel):
    user_id: str
    name: str = ""
    role: str = ""


class UpdateUserRoleRequest(BaseModel):
    role: str = ""


@app.get("/users", response_model=list[UserInfo])
def list_users():
    """列出所有用户"""
    return [UserInfo(**u) for u in user_manager.list_users()]


@app.post("/users", response_model=UserInfo)
def create_user(req: CreateUserRequest):
    """创建新用户"""
    try:
        user = user_manager.create_user(req.user_id, req.name, req.role)
        return UserInfo(**user)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/users/{user_id}/role")
def update_user_role(user_id: str, req: UpdateUserRoleRequest):
    """更新用户角色"""
    import json
    from user_manager import _all_users, _write_users, get_user
    users = _all_users()
    if user_id not in users:
        raise HTTPException(404, "用户不存在")
    users[user_id]["role"] = req.role
    _write_users(users)
    updated = get_user(user_id)
    return UserInfo(**updated) if updated else {"status": "ok"}


@app.delete("/users/{user_id}")
def delete_user(user_id: str):
    """删除用户"""
    ok = user_manager.delete_user(user_id)
    if not ok:
        raise HTTPException(404, "用户不存在")
    return {"status": "ok", "message": f"已删除用户 {user_id}"}


@app.get("/users/me")
def get_my_user(request: Request):
    """获取当前登录用户的信息"""
    uid = _get_current_user(request)
    if agent:
        agent.set_user(uid)
    user = user_manager.get_user(uid)
    if not user:
        # 首次登录时自动创建用户
        user = user_manager.create_user(uid, uid)
    return user






def _init_default_users():
    """初始化默认用户（从 auth 配置同步）"""
    auth = _auth_config()
    users = auth.get("users", {})
    for uid in users:
        if not user_manager.get_user(uid):
            user_manager.create_user(uid, uid, role="")
            logger.info("  👤 创建用户: %s", uid)


@app.get("/health")
def health():
    """健康检查"""
    cfg = None
    initialized = False
    error_msg = None
    
    if not agent:
        try:
            init_agent()
        except Exception as e:
            error_msg = str(e)
    
    if agent:
        initialized = True
        cfg = agent.config
    
    result = {
        "status": "ok" if initialized else "error",
    }
    if cfg:
        result["model"] = cfg.model
        result["provider"] = cfg.active_provider
        result["provider_name"] = cfg.providers.get(cfg.active_provider, {}).get("name", cfg.active_provider)
    else:
        result["model"] = os.getenv("LLM_MODEL") or os.getenv("OPENAI_API_KEY", "未设置") and "gpt-4o" or "未配置"
    if error_msg:
        result["error"] = error_msg
    return result


# ── Python 实时输出流 ──
@app.get("/tool-progress")
async def tool_progress_stream(request: Request):
    """Python 执行时实时输出 SSE 流"""
    from tools import code_tools as _ct

    async def generator():
        index = 0
        while True:
            if await request.is_disconnected():
                break
            lines, idx = _ct.get_progress_since(index)
            for line in lines:
                # 滤掉纯空白/进度条类输出，避免大量碎片
                stripped = line.rstrip()
                if stripped:
                    yield f"data: {json.dumps({'text': stripped}, ensure_ascii=False)}\n\n"
            index = idx
            if not _ct.is_progress_running() and index >= len(lines):
                break
            await asyncio.sleep(0.3)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@app.get("/tool-progress-json")
async def tool_progress_json():
    """Python 执行进度 JSON 接口（供前端 fetch 轮询，保留兼容）"""
    from tools import code_tools as _ct
    lines, total = _ct.get_progress_since(0)
    return {
        "lines": [l.rstrip() for l in lines if l.rstrip()],
        "total": total,
        "running": _ct.is_progress_running(),
    }


@app.websocket("/ws/tool-progress")
async def tool_progress_ws(websocket: WebSocket):
    """Python 执行实时进度 WebSocket（替代 /tool-progress-json 轮询）"""
    await websocket.accept()
    _active_tool_progress_ws.add(websocket)
    last_count = 0
    max_idle_loops = 600  # 0.5s * 600 = 300s = 5 分钟无输出/无工具则关闭
    idle_loops = 0
    try:
        while True:
            await asyncio.sleep(0.5)
            lines, idx = code_tools.get_progress_since(0)
            running = code_tools.is_progress_running()
            filtered_lines = [l.rstrip() for l in lines if l.rstrip()]
            if len(filtered_lines) > last_count:
                await websocket.send_json({
                    "lines": filtered_lines,
                    "total": idx,
                    "running": running,
                })
                last_count = len(filtered_lines)
                idle_loops = 0
            elif not running:
                # 工具已结束，无论是否有输出都发送最终状态
                await websocket.send_json({
                    "lines": filtered_lines,
                    "total": idx,
                    "running": False,
                })
                break
            else:
                idle_loops += 1
                if idle_loops >= max_idle_loops:
                    break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("tool-progress WebSocket 异常: %s", e)
    finally:
        _active_tool_progress_ws.discard(websocket)


# ── 子代理实时日志流 ──
@app.get("/subagent-progress/{capsule_id}")
async def subagent_progress_stream(capsule_id: int, request: Request):
    """子代理执行时实时输出 SSE 流。capsule_id 对应前端胶囊索引（从 1 开始）。"""
    from subagents import manager as _sm

    async def generator():
        seen = 0
        while True:
            if await request.is_disconnected():
                break
            lines, total, done = _sm.get_progress_logs(capsule_id)
            for line in lines[seen:]:
                yield f"data: {json.dumps(line, ensure_ascii=False)}\n\n"
            seen = total
            if done:
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )

# ---------- 数据库管理路由 ----------
# （预留给 UI 管理界面，后续前端对接这些接口即可）

class DBConnectionRequest(BaseModel):
    name: str
    db_type: str = "sqlite"
    path: str = ""
    host: str = ""
    port: int = 0
    database: str = ""
    username: str = ""
    password: str = ""
    readonly: bool = True


class DBQueryRequest(BaseModel):
    sql: str
    connection: str = "local_sqlite"


class PermissionsSaveRequest(BaseModel):
    """权限配置保存请求体（直接接收 JSON，不再嵌套）"""
    global_defaults: dict = {}
    roles: dict = {}
    users: dict = {}


@app.get("/db/default-connection")
def db_get_default_connection(request: Request):
    """获取默认数据库连接名"""
    from dbcli.config import get_default_connection
    return {"default_connection": get_default_connection()}


@app.put("/db/default-connection")
def db_set_default_connection(req: DBConnectionRequest, request: Request):
    """设置默认数据库连接"""
    _require_admin(request)
    from dbcli.config import get_db_configs, save_db_configs
    configs = get_db_configs()
    name = req.name
    if not any(c.name == name for c in configs):
        raise HTTPException(400, f"连接 '{name}' 不存在")
    save_db_configs(configs, default_connection=name)
    return {"status": "ok", "default_connection": name}


@app.get("/db/connections")
def db_list_connections(request: Request):
    """列出所有数据库连接配置（含状态）"""
    _require_admin(request)
    from dbcli.config import get_db_configs
    configs = get_db_configs()
    return [{
        "name": c.name, "db_type": c.db_type, "readonly": c.readonly,
        "enabled": c.enabled,
        "path": c.path, "host": c.host, "port": c.port,
        "database": c.database, "username": c.username,
    } for c in configs]


@app.post("/db/connections")
def db_add_connection(req: DBConnectionRequest, request: Request):
    """添加或更新数据库连接"""
    _require_admin(request)
    from dbcli.config import get_db_configs, save_db_configs, DatabaseConfig
    from dbcli.connection import ConnectionPool
    configs = get_db_configs()
    configs = [c for c in configs if c.name != req.name]
    config = DatabaseConfig(
        name=req.name, db_type=req.db_type, path=req.path,
        host=req.host, port=req.port, database=req.database,
        username=req.username, password=req.password, readonly=req.readonly,
    )
    configs.append(config)
    save_db_configs(configs)
    ConnectionPool.reload(req.name)
    return {"status": "ok", "message": f"已添加/更新连接 {req.name}"}


@app.delete("/db/connections/{name}")
def db_remove_connection(name: str, request: Request):
    """删除数据库连接"""
    _require_admin(request)
    from dbcli.config import get_db_configs, save_db_configs
    from dbcli.connection import ConnectionPool
    configs = get_db_configs()
    configs = [c for c in configs if c.name != name]
    save_db_configs(configs)
    ConnectionPool.remove(name)
    return {"status": "ok", "message": f"已移除连接 {name}"}


@app.post("/db/connections/{name}/test")
def db_test_connection(name: str, request: Request):
    """测试已保存的数据库连接"""
    from dbcli.connection import ConnectionPool
    return ConnectionPool.test_connection(name)


@app.post("/db/test-connection")
def db_test_connection_inline(req: DBConnectionRequest, request: Request):
    """测试未保存的数据库连接（供前端表单预测试用）"""
    try:
        from sqlalchemy import create_engine, text

        if req.db_type == "sqlite":
            if not req.path:
                return {"ok": False, "error": "SQLite 需要指定文件路径"}
            url = f"sqlite:///{req.path}"
        elif req.db_type == "postgresql":
            if not req.host or not req.database:
                return {"ok": False, "error": "PostgreSQL 需要填写主机地址和数据库名"}
            pwd = f":{req.password}" if req.password else ""
            port = f":{req.port}" if req.port else ""
            url = f"postgresql://{req.username}{pwd}@{req.host}{port}/{req.database}"
        elif req.db_type == "mysql":
            if not req.host or not req.database:
                return {"ok": False, "error": "MySQL 需要填写主机地址和数据库名"}
            pwd = f":{req.password}" if req.password else ""
            port = f":{req.port}" if req.port else ""
            url = f"mysql+pymysql://{req.username}{pwd}@{req.host}{port}/{req.database}"
        else:
            return {"ok": False, "error": f"不支持的数据库类型: {req.db_type}"}

        engine = create_engine(url, pool_pre_ping=True)
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        finally:
            engine.dispose()

        return {"ok": True}

    except ImportError as e:
        missing = str(e).replace("No module named ", "").strip("'\" ")
        return {"ok": False, "error": f"缺少数据库驱动: {missing}，请安装对应的 Python 包（参考 requirements.txt）"}
    except Exception as e:
        return {"ok": False, "error": f"连接失败: {type(e).__name__}: {e}"}


@app.get("/db/permissions")
def db_get_permissions(request: Request):
    """获取权限配置"""
    _require_admin(request)
    from dbcli.config import get_permission_config, CONFIG_DIR
    from pathlib import Path
    import yaml
    perm = get_permission_config()
    # 简化返回（不暴露密码等敏感信息）
    output = {"global_defaults": perm.global_defaults, "roles": {}, "users": {}}
    for role_name, role in perm.roles.items():
        output["roles"][role_name] = {"databases": {
            db: [{"table": t.table, "columns_allow": t.columns_allow,
                   "row_filter": t.row_filter, "allow_write": t.allow_write,
                   "max_rows": t.max_rows} for t in tables]
            for db, tables in role.databases.items()
        }}
    for user_id, user in perm.users.items():
        output["users"][user_id] = {"role": user.role, "databases": {
            db: [{"table": t.table, "columns_allow": t.columns_allow,
                   "row_filter": t.row_filter, "allow_write": t.allow_write,
                   "max_rows": t.max_rows} for t in tables]
            for db, tables in user.databases.items()
        }}
    # 返回原始 YAML 文本（供前端编辑器使用）
    yaml_path = CONFIG_DIR / "permissions.yaml"
    if not yaml_path.exists():
        yaml_path = Path(__file__).parent / "dbcli" / "permissions.yaml"
    yaml_text = ""
    if yaml_path.exists():
        try:
            yaml_text = yaml_path.read_text(encoding="utf-8")
        except Exception:
            yaml_text = ""
    output["yaml"] = yaml_text
    return output


@app.put("/db/permissions")
def db_save_permissions(req: PermissionsSaveRequest, request: Request):
    """保存权限配置（后端生成 YAML）"""
    _require_admin(request)
    from dbcli.config import CONFIG_DIR, logger as config_logger
    import yaml
    import traceback
    try:
        logger.info(f"[保存权限] 收到保存请求: roles={len(req.roles or {})}, users={len(req.users or {})}")
        
        # 清理空 key
        roles = {k: v for k, v in (req.roles or {}).items() if k and k.strip()}
        users = {k: v for k, v in (req.users or {}).items() if k and k.strip()}
        data = {
            "global_defaults": req.global_defaults or {},
            "roles": roles,
            "users": users,
        }
        logger.info(f"[保存权限] 清理后数据: roles={list(roles.keys())}, users={list(users.keys())}")

        from dbcli.config import _parse_permission_config, _serialize_permission_config_for_yaml
        config = _parse_permission_config(data)
        logger.info(f"[保存权限] 解析配置成功")

        # 用 PyYAML 生成干净 YAML
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        yaml_path = CONFIG_DIR / "permissions.yaml"
        clean_data = _serialize_permission_config_for_yaml(config)
        logger.info(f"[保存权限] 序列化后的数据: {clean_data.keys()}")
        
        yaml_content = yaml.safe_dump(clean_data, allow_unicode=True, default_flow_style=False, sort_keys=False)
        logger.info(f"[保存权限] YAML内容预览:\n{yaml_content[:500]}")
        
        with open(yaml_path, "w", encoding="utf-8") as f:
            f.write(yaml_content)
        logger.info(f"[保存权限] YAML已写入: {yaml_path}")

        # 热更新权限检查器
        from dbcli.auth import reload_checker
        reload_checker()
        logger.info(f"[保存权限] 权限检查器已热更新")

        return {"status": "ok", "message": "权限配置已保存"}
    except Exception as e:
        error_detail = traceback.format_exc()
        logger.error(f"[保存权限] 保存失败: {e}\n{error_detail}")
        raise HTTPException(400, f"权限配置格式错误: {e}")


@app.post("/db/query")
def db_execute_query(req: DBQueryRequest, request: Request):
    """执行 SQL 查询（含权限检查，供 UI 管理界面使用）"""
    from dbcli.query import execute_query
    uid = _get_current_user(request)
    from user_manager import get_user
    user = get_user(uid) or {}
    role = user.get("role", "")
    user_context = {"user_id": uid, "role": role}
    result = execute_query(req.sql, connection_name=req.connection,
                       role=role, user_context=user_context)
    return result.to_dict()


@app.get("/db/schema/{connection_name}")
def db_get_schema(connection_name: str, table: str = "", request: Request = None):
    """获取数据库 schema（供 UI 管理界面自动补全）"""
    _require_admin(request)
    from dbcli.schema import get_schema
    tables = get_schema(connection_name, table or None)
    return [{"name": t.name, "columns": [
        {"name": c.name, "type": c.type, "primary_key": c.primary_key}
        for c in t.columns
    ]} for t in tables]


# ---------- 微信 Bot 管理 ----------

import base64 as _base64  # 避免与顶部 base64 冲突

@app.get("/wechat/status")
async def wechat_status(request: Request):
    """获取微信 Bot 状态"""
    bot: WeChatBot = request.app.state.wechat_bot
    return {
        "logged_in": bot.is_logged_in,
        "running": bot.is_running,
    }

@app.get("/wechat/qrcode")
async def wechat_qrcode(request: Request):
    """获取微信登录二维码"""
    bot: WeChatBot = request.app.state.wechat_bot
    data = await bot.get_qrcode()
    if "qrcode_img_content" in data:
        data["qrcode_img_base64"] = _base64.b64encode(
            data.pop("qrcode_img_content").encode("utf-8")
        ).decode()
    return data

@app.get("/wechat/qrcode-status")
async def wechat_qrcode_status(qrcode: str, request: Request):
    """轮询扫码状态"""
    bot: WeChatBot = request.app.state.wechat_bot
    return await bot.poll_qrcode_status(qrcode)

@app.post("/wechat/start")
async def wechat_start(request: Request):
    """启动微信 Bot 轮询"""
    bot: WeChatBot = request.app.state.wechat_bot
    if not bot.is_logged_in:
        raise HTTPException(400, "尚未登录，请先扫码")
    await bot.start()
    return {"status": "started"}

@app.post("/wechat/stop")
async def wechat_stop(request: Request):
    """停止微信 Bot 轮询"""
    bot: WeChatBot = request.app.state.wechat_bot
    await bot.stop()
    return {"status": "stopped"}


# ---------- 启动 ----------

if __name__ == "__main__":
    # 从环境变量读取配置
    host = os.getenv("AGENT_HOST", "127.0.0.1")
    port = int(os.getenv("AGENT_PORT", "8899"))
    
    logger.info("🚀 启动桌面 AI 智能体服务...")
    logger.info("  🔗 地址: http://%s:%s", host, port)
    logger.info("  📖 API 文档: http://%s:%s/docs", host, port)
    logger.info("  🖥 桌面 UI: http://%s:%s/", host, port)

    if os.getenv("AGENT_OPEN_BROWSER", "0") == "1":
        def _open_browser():
            time.sleep(1.2)
            webbrowser.open(f"http://{host}:{port}/")

        threading.Thread(target=_open_browser, daemon=True).start()
    
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_config={
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {},
            "handlers": {},
            "loggers": {},
        },
        log_level="info",
    )
