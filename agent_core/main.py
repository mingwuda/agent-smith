"""桌面 AI 智能体 —— FastAPI 服务器入口

Slim entry point: app 创建、middleware、startup/shutdown、agent 全局实例。
所有路由已拆分到 api/routes/ 各模块，使用 app.include_router() 注册。
"""
import asyncio
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

# 确保能找到 agent_core 内的模块
sys.path.insert(0, str(Path(__file__).parent))

from logger import setup_logging, get_logger, set_log_context, clear_log_context

logger = get_logger(__name__)

from config import AgentConfig
from agent import DesktopAgent
from tools import (
    file_tools, code_tools, system_tools, web_tools, memory_tools,
    git_tools, database_tool, shell_tools, browser_tools,
)
import subagents
from monitoring.usage_tracker import get_tracker
from skills.registry import get_registry
from memory.local_memory import get_memory
import session_store
import user_manager
# 在导入 wechat_bot（会加载 httpx）之前静默其日志
import logging
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
from wechat_bot import WeChatBot


# ---------- 全局变量 ----------

agent: Optional[DesktopAgent] = None


# ---------- 工具函数 ----------

def _app_base_dir() -> Path:
    """Return project root in source mode and PyInstaller resource root when frozen."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).parent.parent


# ---------- FastAPI ----------

@asynccontextmanager
async def lifespan(app):
    setup_logging()
    logger.info("🔄 服务启动中...（Agent 将在首次请求时初始化）")
    _init_default_users()
    # 保存主事件循环引用，供 sync 线程调度 async 任务
    app.state.main_loop = asyncio.get_running_loop()
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


# ---------- 认证 middleware ----------

from api.deps import AUTH_COOKIE_NAME, _auth_exempt_path, _is_authenticated, _wants_html

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


# ---------- 静态文件 ----------

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


# ---------- Agent 生命周期 ----------

def init_agent():
    global agent
    
    config = AgentConfig.load()
    
    # 初始化工作区
    file_tools.set_workspace(Path(config.workspace))
    git_tools.set_workspace(Path(config.workspace))
    shell_tools.set_workspace(Path(config.workspace))
    browser_tools.set_workspace(Path(config.workspace))
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
    all_tools.extend(shell_tools.TOOLS)
    all_tools.extend(browser_tools.TOOLS)
    subagents.manager.configure(config, all_tools)
    
    # 先加载 Skills，再构建 Agent graph
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

    # 初始化微信 Bot（按用户懒加载 + 启动时主动拉起所有已登录用户的 Bot）
    app.state.wechat_bots: dict[str, WeChatBot] = {}
    _start_all_wechat_bots()


def _get_all_users_with_bot() -> list[str]:
    """获取所有可能有所属微信 Bot 的用户 ID。"""
    users = ["admin"]
    try:
        from user_manager import list_users
        for u in list_users():
            uid = u.get("id", "")
            if uid and uid not in users:
                users.append(uid)
    except Exception:
        pass
    try:
        data_root = Path.home() / ".desktop_agent"
        for p in data_root.glob("wechat_*"):
            uid = p.name[len("wechat_"):]
            if uid and uid not in users:
                users.append(uid)
    except Exception:
        pass
    return users


def _start_all_wechat_bots():
    """为所有有 token 的用户自动启动微信 Bot 轮询。"""
    loop = getattr(app.state, "main_loop", None)
    started = 0
    for uid in _get_all_users_with_bot():
        try:
            bot = _get_wechat_bot(uid)
            if bot.is_logged_in and not bot.is_running:
                if loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(bot.start(), loop)
                    started += 1
                    logger.info("[微信Bot] 用户 %s 的 Bot 已自动启动轮询", uid)
        except Exception as e:
            logger.warning("[微信Bot] 用户 %s 的 Bot 启动失败: %s", uid, e)
    if started:
        logger.info("[微信Bot] 共自动启动 %d 个微信 Bot", started)


def _get_wechat_bot(uid: str) -> WeChatBot:
    """获取或创建当前用户的微信 Bot（懒加载）。"""
    bots: dict[str, WeChatBot] = app.state.wechat_bots
    bot = bots.get(uid)
    if bot is None:
        bot = WeChatBot(agent, user_id=uid)
        bots[uid] = bot
        if bot.is_logged_in:
            try:
                asyncio.create_task(bot.start())
            except Exception:
                pass
    return bot


# ---------- 默认用户初始化 ----------

def _init_default_users():
    AUTH_FILE = Path.home() / ".desktop_agent" / "auth.json"
    secret = os.getenv("DESKTOP_AGENT_AUTH_SECRET") or ""
    users: dict[str, str] = {}
    file_data: dict = {}
    if AUTH_FILE.exists():
        try:
            file_data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            file_data = {}
    old_username = file_data.get("username") or os.getenv("DESKTOP_AGENT_AUTH_USER") or ""
    old_password = file_data.get("password") or os.getenv("DESKTOP_AGENT_AUTH_PASSWORD") or ""
    if old_username and old_password:
        users[old_username] = old_password
    file_users = file_data.get("users", {})
    if isinstance(file_users, dict):
        users.update(file_users)
    env_users = os.getenv("AGENT_USERS", "").strip()
    if env_users:
        for pair in env_users.split(";"):
            pair = pair.strip()
            if ":" in pair:
                u, p = pair.split(":", 1)
                users[u.strip()] = p.strip()
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


# ---------- 注册路由模块 ----------

from api.auth import router as auth_router
from api.routes.agent import router as agent_router
from api.routes.sessions import router as sessions_router
from api.routes.skills import router as skills_router
from api.routes.artifacts import router as artifacts_router
from api.routes.db import router as db_router
from api.routes.system import router as system_router
from api.routes.wechat import router as wechat_router
from api.routes.monitoring import router as monitoring_router

app.include_router(auth_router)
app.include_router(agent_router)
app.include_router(sessions_router)
app.include_router(skills_router)
app.include_router(artifacts_router)
app.include_router(db_router)
app.include_router(system_router)
app.include_router(wechat_router)
app.include_router(monitoring_router)


# ---------- 入口 ----------

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("DESKTOP_AGENT_PORT", "8899"))
    logger.info("🚀 Desktop Agent 启动中: http://127.0.0.1:%d", port)
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info",
        loop="asyncio",
    )
