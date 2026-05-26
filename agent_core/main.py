"""桌面 AI 智能体 —— FastAPI 服务器入口"""
import json
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
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
from tools import file_tools, code_tools, system_tools, web_tools
from monitoring.usage_tracker import get_tracker
from skills.registry import get_registry
import session_store

# ---------- FastAPI ----------
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    print("🔄 服务启动中...（Agent 将在首次请求时初始化）")
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
    
    # 注册所有工具
    all_tools = []
    all_tools.extend(file_tools.TOOLS)
    all_tools.extend(code_tools.TOOLS)
    all_tools.extend(system_tools.TOOLS)
    all_tools.extend(web_tools.TOOLS)
    
    # 初始化 Agent
    agent = DesktopAgent(config)
    agent.set_tools(all_tools)
    
    # 加载 Skills
    skills_count = get_registry().load_from(Path(config.skills_dir))
    
    print(f"✅ Agent 初始化完成")
    print(f"  模型: {config.model}")
    print(f"  工作区: {config.workspace}")
    print(f"  Skills 目录: {config.skills_dir}")
    print(f"  已加载技能: {skills_count} 个")

# ---------- API 模型 ----------

class RunRequest(BaseModel):
    message: str
    thread_id: str = "default"


class RunResponse(BaseModel):
    result: str
    steps: list[dict] = []


class SkillInfo(BaseModel):
    name: str
    description: str
    triggers: list[str] = []
    has_instructions: bool = False


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


# ---------- 桌面 UI 路由 ----------

from fastapi.responses import HTMLResponse, StreamingResponse

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def serve_ui():
    """提供桌面 UI"""
    if _html_content:
        return HTMLResponse(_html_content)
    return HTMLResponse("<h1>Desktop Agent API</h1><p>UI not found. Use /docs for API docs.</p>")


# ---------- API 路由 ----------

@app.post("/run", response_model=RunResponse)
async def run_agent(req: RunRequest):
    """发送消息给 Agent 并获取回复"""
    if not agent:
        init_agent()
    if not agent:
        raise HTTPException(503, "Agent 初始化失败，请检查 API Key 设置")
    
    session_id = req.thread_id
    
    # 确保会话存在
    session = session_store.get_session(session_id)
    if session is None:
        session = session_store.create_session(title=f"会话 {session_id}")
        # 修正 id 为用户传入的 id
        import os, json
        # 直接创建指定 id 的会话
        meta_path = Path.home() / ".desktop_agent" / "sessions" / f"{session_id}.json"
        msgs_path = Path.home() / ".desktop_agent" / "sessions" / f"{session_id}_messages.json"
        if not meta_path.exists():
            now = __import__('datetime').datetime.now().isoformat()
            meta = {"id": session_id, "title": f"会话 {session_id[:8]}", "created_at": now, "updated_at": now, "message_count": 0}
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(__import__('json').dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            msgs_path.write_text("[]", encoding="utf-8")
        session = session_store.get_session(session_id)
    
    history_messages = (session or {}).get("messages", [])

    # 保存用户消息
    session_store.add_message(session_id, "user", req.message)
    
    agent.switch_thread(session_id)
    result, steps = await agent.run(req.message, history=history_messages)
    
    # 保存 AI 回复
    session_store.add_message(session_id, "assistant", result)
    
    # 更新会话标题（取第一条用户消息的前30字）
    session = session_store.get_session(session_id)
    if session and session.get("message_count", 0) <= 2:
        title = req.message[:30] + ("..." if len(req.message) > 30 else "")
        session_store.rename_session(session_id, title)
    
    return RunResponse(result=result, steps=steps)


@app.post("/run/stream")
async def run_agent_stream(req: RunRequest):
    """流式处理消息（SSE）"""
    if not agent:
        init_agent()
    if not agent:
        raise HTTPException(503, "Agent 初始化失败")
    
    session_id = req.thread_id
    # 确保会话存在
    session = session_store.get_session(session_id)
    if session is None:
        meta_path = Path.home() / ".desktop_agent" / "sessions" / f"{session_id}.json"
        msgs_path = Path.home() / ".desktop_agent" / "sessions" / f"{session_id}_messages.json"
        if not meta_path.exists():
            now = __import__('datetime').datetime.now().isoformat()
            meta = {"id": session_id, "title": f"会话 {session_id[:8]}", "created_at": now, "updated_at": now, "message_count": 0}
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            msgs_path.write_text("[]", encoding="utf-8")
        session = session_store.get_session(session_id)
    
    history_messages = (session or {}).get("messages", [])

    # 保存用户消息
    session_store.add_message(session_id, "user", req.message)
    
    agent.switch_thread(session_id)
    
    async def event_stream():
        final_content = ""
        async for sse_event in agent.stream_run(req.message, history=history_messages):
            yield sse_event
            # 收集最终内容
            if '"type": "done"' in sse_event:
                try:
                    import re
                    m = re.search(r'data: ({.*})', sse_event)
                    if m:
                        data = json.loads(m.group(1))
                        final_content = data.get("content", "")
                except Exception:
                    pass
        
        # 保存 AI 回复
        if final_content:
            session_store.add_message(session_id, "assistant", final_content)
            # 更新会话标题
            session = session_store.get_session(session_id)
            if session and session.get("message_count", 0) <= 2:
                title = req.message[:30] + ("..." if len(req.message) > 30 else "")
                session_store.rename_session(session_id, title)
    
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/skills", response_model=list[SkillInfo])
def list_skills():
    """列出所有已加载的技能"""
    registry = get_registry()
    # 如果尚未加载技能，尝试加载
    if not registry.list_all():
        skills_dir = Path(__file__).parent / "samples"
        if skills_dir.exists():
            registry.load_from(skills_dir)
    return [
        SkillInfo(
            name=s.name,
            description=s.description,
            triggers=s.triggers,
            has_instructions=bool(s.instructions),
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
def list_sessions():
    """列出所有会话"""
    raw = session_store.list_sessions()
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
    return SessionListResponse(sessions=sessions, current_id=agent._thread_id if agent else "default")


@app.get("/sessions/{session_id}", response_model=SessionMessagesResponse)
def get_session(session_id: str):
    """获取单个会话的消息列表"""
    session = session_store.get_session(session_id)
    if not session:
        raise HTTPException(404, "会话不存在")
    return SessionMessagesResponse(
        id=session["id"],
        title=session.get("title", "未命名"),
        messages=session.get("messages", []),
    )


@app.post("/sessions", response_model=CreateSessionResponse)
def create_session():
    """创建新会话"""
    session = session_store.create_session()
    return CreateSessionResponse(id=session["id"], title=session["title"])


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    """删除会话"""
    ok = session_store.delete_session(session_id)
    if not ok:
        raise HTTPException(404, "会话不存在")
    return {"status": "ok", "message": f"已删除会话 {session_id}"}


@app.put("/sessions/{session_id}/rename")
def rename_session(session_id: str, req: RenameRequest):
    """重命名会话"""
    ok = session_store.rename_session(session_id, req.title)
    if not ok:
        raise HTTPException(404, "会话不存在")
    return {"status": "ok", "message": f"已重命名为 {req.title}"}


@app.get("/usage", response_model=UsageStats)
def get_usage():
    """获取今日模型使用量统计"""
    tracker = get_tracker()
    return UsageStats(**tracker.get_today_stats())


@app.get("/usage/session", response_model=SessionStats)
def get_session_usage(thread_id: str = ""):
    """获取当前会话的模型使用量"""
    tracker = get_tracker()
    return SessionStats(**tracker.get_session_stats(thread_id=thread_id or None))


@app.get("/usage/history")
def get_usage_history(days: int = 7):
    """获取最近 N 天的使用历史"""
    tracker = get_tracker()
    return tracker.get_history(days=days)


# ---------- 设置 / 配置路由 ----------

class SettingsRequest(BaseModel):
    """设置请求体"""
    active_provider: str = "openai"
    provider_name: str = ""
    api_key: str = ""
    model: str = ""
    base_url: str = ""
    recursion_limit: int = 60


@app.get("/settings")
def get_settings():
    """获取当前设置"""
    cfg = AgentConfig.load()
    return cfg.to_api_dict()


@app.post("/settings")
def save_settings(req: SettingsRequest):
    """保存设置并重启 Agent"""
    cfg = AgentConfig.load()
    
    cfg.update_provider(
        provider_id=req.active_provider,
        provider_name=req.provider_name,
        api_key=req.api_key,
        model=req.model,
        base_url=req.base_url,
    )
    cfg.recursion_limit = max(1, int(req.recursion_limit or 60))
    
    # 持久化到文件（现在包含 API Key）
    cfg.save()
    
    # 也设到环境变量（当前进程生效）
    os.environ["LLM_API_KEY"] = cfg.api_key
    os.environ["OPENAI_API_KEY"] = cfg.api_key
    os.environ["LLM_MODEL"] = cfg.model
    os.environ["LLM_PROVIDER"] = cfg.active_provider
    os.environ["AGENT_RECURSION_LIMIT"] = str(cfg.recursion_limit)
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
