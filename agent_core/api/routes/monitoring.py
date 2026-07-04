"""监控、健康检查、权限、子代理路由"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..deps import _get_current_user
from logger import get_logger
from monitoring.usage_tracker import get_tracker
from memory.local_memory import get_memory
from config import AgentConfig
from agent import DesktopAgent
import subagents

logger = get_logger(__name__)

router = APIRouter(tags=["monitoring"])

# 当前活跃的 Python 工具进度 WebSocket 连接
_active_tool_progress_ws: set[WebSocket] = set()

# ---------- 全局 Agent 实例（用于健康检查） ----------

agent: Optional[DesktopAgent] = None


def init_agent():
    """初始化 Agent（委托 main 模块，同步本地引用）"""
    global agent
    from ...main import init_agent as _main_init
    _main_init()
    from ...main import agent as _main_agent
    agent = _main_agent


# ---------- API 模型 ----------


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


class MemoryRequest(BaseModel):
    key: str
    value: Any


class GrantPathRequest(BaseModel):
    path: str


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


# ---------- 使用量统计路由 ----------


@router.get("/usage", response_model=UsageStats)
def get_usage(request: Request):
    """获取今日模型使用量统计"""
    tracker = get_tracker(_get_current_user(request))
    return UsageStats(**tracker.get_today_stats())


@router.get("/usage/session", response_model=SessionStats)
def get_session_usage(thread_id: str = "", request: Request = None):
    """获取当前会话的模型使用量"""
    tracker = get_tracker(_get_current_user(request) if request else "default")
    return SessionStats(**tracker.get_session_stats(thread_id=thread_id or None))


@router.get("/usage/history")
def get_usage_history(days: int = 7, request: Request = None):
    """获取最近 N 天的使用历史"""
    tracker = get_tracker(_get_current_user(request) if request else "default")
    return tracker.get_history(days=days)


# ---------- 长期记忆路由 ----------


@router.get("/memories")
def list_memories(request: Request, q: str = ""):
    """列出或搜索当前用户的长期记忆"""
    uid = _get_current_user(request)
    memory = get_memory(uid)
    if q:
        return {"items": memory.list_items(), "query": q, "result": memory.search(q)}
    return {"items": memory.list_items()}


@router.post("/memories")
def save_memory(req: MemoryRequest, request: Request):
    """为当前用户保存一条长期记忆"""
    if not req.key.strip():
        raise HTTPException(400, "记忆 key 不能为空")
    uid = _get_current_user(request)
    memory = get_memory(uid)
    memory.set(req.key.strip(), req.value)
    return {"status": "ok", "message": f"已保存记忆 {req.key.strip()}"}


@router.delete("/memories/{key}")
def delete_memory(key: str, request: Request):
    """删除当前用户的一条长期记忆"""
    uid = _get_current_user(request)
    memory = get_memory(uid)
    memory.delete(key)
    return {"status": "ok", "message": f"已删除记忆 {key}"}


# ---------- 健康检查 ----------


@router.get("/health")
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


# ---------- 工具进度路由 ----------


@router.get("/tool-progress")
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


@router.get("/tool-progress-json")
async def tool_progress_json():
    """Python 执行进度 JSON 接口（供前端 fetch 轮询，保留兼容）"""
    from tools import code_tools as _ct
    lines, total = _ct.get_progress_since(0)
    return {
        "lines": [l.rstrip() for l in lines if l.rstrip()],
        "total": total,
        "running": _ct.is_progress_running(),
    }


@router.websocket("/ws/tool-progress")
async def tool_progress_ws(websocket: WebSocket):
    """Python 执行实时进度 WebSocket（替代 /tool-progress-json 轮询）"""
    from tools import code_tools

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


# ---------- 子代理实时日志流 ----------


@router.get("/subagent-progress/{capsule_id}")
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


# ---------- 子代理路由 ----------


@router.get("/subagents")
def list_subagents():
    """列出可用子代理类型。"""
    return {"items": subagents.manager.list_agent_types()}


@router.get("/subagents/tasks/{task_id}", response_model=SubagentTaskInfo)
def get_subagent_task(task_id: str):
    """查询子代理任务状态。第一版任务为同步执行，后续并行任务会复用该结构。"""
    item = subagents.manager.get_task(task_id)
    if not item:
        raise HTTPException(404, "子代理任务不存在")
    return SubagentTaskInfo(**item.__dict__)


# ---------- 权限路由 ----------


@router.post("/permissions/grant-path")
def grant_path(req: GrantPathRequest, request: Request):
    """授权 AI 在本次会话中对指定路径（或目录）进行编辑。"""
    uid = _get_current_user(request)
    from tools.file_tools import add_outside_auth
    add_outside_auth(uid, req.path)
    return {"status": "ok", "message": f"已授权路径: {req.path}"}


@router.get("/permissions/granted-paths")
def list_granted_paths(request: Request):
    """查看当前用户已授权的路径列表。"""
    uid = _get_current_user(request)
    from tools.file_tools import _outside_auths
    return {"paths": _outside_auths.get(uid, [])}
