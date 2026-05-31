"""桌面 AI 智能体 —— FastAPI 服务器入口"""
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, Response, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

# 确保能找到 agent_core 内的模块
sys.path.insert(0, str(Path(__file__).parent))


def _app_base_dir() -> Path:
    """Return project root in source mode and PyInstaller resource root when frozen."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).parent.parent

from config import AgentConfig
from agent import DesktopAgent
from tools import file_tools, code_tools, system_tools, web_tools, memory_tools, git_tools
import subagents
from monitoring.usage_tracker import get_tracker
from skills.registry import get_registry
from memory.local_memory import get_memory
import session_store
import user_manager

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
    return path in {"/login", "/auth/login", "/auth/logout", "/auth/token-login", "/health"} or path.startswith("/favicon")


def _wants_html(request: Request) -> bool:
    if request.url.path in {"/", "/docs", "/redoc"}:
        return True
    return "text/html" in request.headers.get("accept", "")

# ---------- FastAPI ----------
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    print("🔄 服务启动中...（Agent 将在首次请求时初始化）")
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
        print(f"📁 桌面 UI: {UI_DIR / 'index.html'}")

# ---------- Agent 实例 ----------

agent: Optional[DesktopAgent] = None


def init_agent():
    global agent
    
    config = AgentConfig.load()
    
    # 初始化工作区
    file_tools.set_workspace(Path(config.workspace))
    git_tools.set_workspace(Path(config.workspace))
    
    # 注册所有工具
    all_tools = []
    all_tools.extend(file_tools.TOOLS)
    all_tools.extend(code_tools.TOOLS)
    all_tools.extend(system_tools.TOOLS)
    all_tools.extend(web_tools.TOOLS)
    all_tools.extend(memory_tools.TOOLS)
    all_tools.extend(git_tools.TOOLS)
    all_tools.extend(subagents.TOOLS)
    subagents.manager.configure(config, all_tools)
    
    # 先加载 Skills，再构建 Agent graph；set_tools 会把技能块注入 system prompt。
    app_base = _app_base_dir()
    skills_dirs = [
        Path(config.skills_dir),
        app_base / ".opencode" / "skills",
        app_base / ".claude" / "skills",
        app_base / ".agents" / "skills",
    ]
    skills_count = get_registry().load_from(skills_dirs)

    # 初始化 Agent
    agent = DesktopAgent(config)
    agent.set_tools(all_tools)
    
    print(f"✅ Agent 初始化完成")
    print(f"  模型: {config.model}")
    print(f"  工作区: {config.workspace}")
    print(f"  Skills 目录: {', '.join(str(p) for p in skills_dirs)}")
    print(f"  已加载技能: {skills_count} 个")

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
<form class="login" onsubmit="login(event)">
  <h1>Desktop Agent</h1>
  <p>请登录后继续操作</p>
  <label for="username">用户名</label>
  <input id="username" autocomplete="username" value="admin" autofocus>
  <label for="password">密码</label>
  <input id="password" type="password" autocomplete="current-password">
  <button id="submit" type="submit">登录</button>
  <div class="error" id="error"></div>
</form>
<script>
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
      err.textContent = data.detail || '用户名或密码错误';
    }
  } catch {
    err.textContent = '网络错误，请稍后重试';
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
        return HTMLResponse(_html_content)
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
    raw = Path(path or "").expanduser()
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
        except HTTPException:
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
    text = (message or "").strip().lower()
    if not text:
        return False
    skill_terms = ("技能", "skills", "skill", "插件", "能力")
    inventory_terms = ("哪些", "有什么", "有哪些", "列表", "已加载", "加载了", "会什么", "能做什么")
    return any(term in text for term in skill_terms) and any(term in text for term in inventory_terms)


def _safe_attachments(attachments: list[AttachmentRequest]) -> list[dict]:
    safe: list[dict] = []
    for item in attachments[:4]:
        mime_type = (item.mime_type or "").strip().lower()
        data_url = (item.data_url or "").strip()
        if not mime_type.startswith("image/") or not data_url.startswith(f"data:{mime_type};base64,"):
            continue
        if len(data_url) > 8 * 1024 * 1024:
            continue
        safe.append({
            "name": item.name or "pasted-image.png",
            "mime_type": mime_type,
            "data_url": data_url,
        })
    return safe


def _display_user_message(message: str, attachments: list[dict]) -> str:
    if not attachments:
        return message
    suffix = f"\n\n[已附加 {len(attachments)} 张图片，仅用于本轮模型分析，图片原文不写入历史记录。]"
    return (message or "请分析这些图片。") + suffix


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


def _save_assistant_result(uid: str, session_id: str, user_message: str, result: str):
    session_store.add_message(uid, session_id, "assistant", result)
    title = user_message[:30] + ("..." if len(user_message) > 30 else "")
    session_store.rename_session(uid, session_id, title or f"会话 {session_id[:8]}")


def _resolve_user(request: Request) -> str:
    """从请求获取当前用户并设置到 agent"""
    uid = _get_current_user(request)
    file_tools.set_workspace(_workspace_for_user(uid))
    if agent:
        agent.set_user(uid)
    return uid


@app.post("/run", response_model=RunResponse)
async def run_agent(req: RunRequest, request: Request):
    """发送消息给 Agent 并获取回复"""
    if not agent:
        init_agent()
    if not agent:
        raise HTTPException(503, "Agent 初始化失败，请检查 API Key 设置")
    
    uid = _resolve_user(request)
    session_id = req.thread_id
    session = await _ensure_session(uid, session_id)
    history_messages = session.get("messages", [])

    attachments = _safe_attachments(req.attachments)
    session_store.add_message(uid, session_id, "user", _display_user_message(req.message, attachments))
    model_override = _image_model_override(attachments)
    if _is_skill_inventory_query(req.message):
        result = _format_loaded_skills()
        _save_assistant_result(uid, session_id, req.message, result)
        return RunResponse(result=result, steps=[])

    agent.switch_thread(session_id)
    result, steps = await agent.run(
        req.message,
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
    
    return RunResponse(result=result, steps=steps)


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
    session_store.add_message(uid, session_id, "user", _display_user_message(req.message, attachments))
    model_override = _image_model_override(attachments)
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
    
    async def event_stream():
        final_content = ""
        error_content = ""
        forwarded_terminal_event = False
        if model_override:
            yield f"data: {json.dumps({'type': 'model_switch', 'model': model_override, 'reason': '图片输入'}, ensure_ascii=False)}\n\n"
        stream = agent.stream_run(
            req.message,
            history=history_messages,
            attachments=attachments,
            model_override=model_override,
        )
        try:
            async for sse_event in stream:
                if await request.is_disconnected():
                    await stream.aclose()
                    return
                if sse_event.strip() == "data: [DONE]":
                    continue
                if '"type": "tool_start"' in sse_event:
                    try:
                        m = re.search(r'data: ({.*})', sse_event)
                        if m:
                            data = json.loads(m.group(1))
                            args = data.get("args") or {}
                            if data.get("tool") in {"write_file", "append_to_file"} and args.get("path"):
                                artifact_paths.append(str(args["path"]))
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
            raise
        
        if final_content:
            final_content = _append_artifact_links(final_content, uid, artifact_paths)
            yield f"data: {json.dumps({'type': 'done', 'content': final_content}, ensure_ascii=False)}\n\n"
            _save_assistant_result(uid, session_id, req.message, final_content)
        elif error_content:
            _save_assistant_result(uid, session_id, req.message, "❌ " + error_content)
        elif not forwarded_terminal_event:
            fallback = (
                "任务已结束，但模型没有生成最终回答。"
                "这通常发生在接近最大推理步数时，模型仍在继续调用工具。"
                f"当前最大推理步数为 {agent.config.recursion_limit}，可以提高该值，或把任务拆小后重试。"
            )
            yield f"data: {json.dumps({'type': 'done', 'content': fallback}, ensure_ascii=False)}\n\n"
            _save_assistant_result(uid, session_id, req.message, fallback)
        yield "data: [DONE]\n\n"
    
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
            app_base / ".opencode" / "skills",
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
    current_uid = agent._user_id if agent else "default"
    return SessionListResponse(sessions=sessions, current_id=current_uid)


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
    api_timeout_seconds: float = 30.0
    api_host_ips: str = ""
    context_window_tokens: int = 0


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
    cfg.api_timeout_seconds = max(1.0, float(req.api_timeout_seconds or 30.0))
    cfg.api_host_ips = req.api_host_ips or cfg.api_host_ips
    cfg.context_window_tokens = max(0, int(req.context_window_tokens or 0))
    
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
        return {"status": "error", "message": f"设置已保存，但 Agent 初始化失败: {str(e)}"}


# ── 用户管理路由 ──

class UserInfo(BaseModel):
    id: str
    name: str
    created_at: str


class CreateUserRequest(BaseModel):
    user_id: str
    name: str = ""


@app.get("/users", response_model=list[UserInfo])
def list_users():
    """列出所有用户"""
    return [UserInfo(**u) for u in user_manager.list_users()]


@app.post("/users", response_model=UserInfo)
def create_user(req: CreateUserRequest):
    """创建新用户"""
    try:
        user = user_manager.create_user(req.user_id, req.name)
        return UserInfo(**user)
    except ValueError as e:
        raise HTTPException(400, str(e))


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
            user_manager.create_user(uid, uid)
            print(f"  👤 创建用户: {uid}")


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


# ---------- 启动 ----------

if __name__ == "__main__":
    # 从环境变量读取配置
    host = os.getenv("AGENT_HOST", "127.0.0.1")
    port = int(os.getenv("AGENT_PORT", "8899"))
    
    print(f"🚀 启动桌面 AI 智能体服务...")
    print(f"  🔗 地址: http://{host}:{port}")
    print(f"  📖 API 文档: http://{host}:{port}/docs")
    print(f"  🖥 桌面 UI: http://{host}:{port}/")
    print()

    if os.getenv("AGENT_OPEN_BROWSER", "0") == "1":
        def _open_browser():
            time.sleep(1.2)
            webbrowser.open(f"http://{host}:{port}/")

        threading.Thread(target=_open_browser, daemon=True).start()
    
    uvicorn.run(app, host=host, port=port, log_level="info")
