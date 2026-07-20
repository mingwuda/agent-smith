"""系统路由（设置、用户管理、UI）"""
import os
import sys
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import user_manager
from agent import DesktopAgent as agent_class
from config import AgentConfig
from services.workspace import _workspace_for_user
from tools import file_tools, shell_tools, browser_tools
from api.deps import _get_current_user, _require_admin
from logger import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["system"])


class RestartResponse(BaseModel):
    status: str
    message: str


class SettingsRequest(BaseModel):
    """设置请求体"""
    active_provider: str = "openai"
    provider_name: str = ""
    api_key: str = ""
    model: str = ""
    base_url: str = ""
    recursion_limit: int = 60
    enable_loop_guard: bool = True
    enable_self_evolution: bool = False
    enable_self_healing: bool = False
    self_healing_interval_seconds: int = 600
    api_max_retries: int = 3
    api_timeout_seconds: float = 120.0
    api_host_ips: str = ""
    context_window_tokens: int = 0
    tavily_search_enabled: bool = False
    tavily_api_key: str = ""
    tavily_search_url: str = "https://api.tavily.com/search"
    anysearch_api_key: str = ""
    review_provider_id: str = ""
    review_model: str = ""
    update_server: str = ""


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


@router.get("/")
def serve_ui():
    """提供桌面 UI（每次从磁盘读取 index.html，便于开发时热更新，无需重启后端）"""
    from main import UI_DIR, _html_content
    ui_index = UI_DIR / "index.html"
    content = _html_content
    if ui_index.exists():
        try:
            content = ui_index.read_text(encoding="utf-8")
        except OSError:
            pass
    if content:
        headers = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
        return HTMLResponse(content, headers=headers)
    return HTMLResponse("<h1>Moss Agent API</h1><p>UI not found. Use /docs for API docs.</p>")


@router.get("/settings")
def get_settings(request: Request):
    """获取当前设置"""
    _require_admin(request)
    cfg = AgentConfig.load()
    return cfg.to_api_dict()


@router.delete("/settings/provider/{provider_id}")
def delete_settings_provider(provider_id: str, request: Request):
    """删除自定义 Provider"""
    _require_admin(request)
    cfg = AgentConfig.load()
    try:
        cfg.delete_provider(provider_id)
        cfg.save()
        # 重启 Agent（同时同步 monitoring 与 agent 路由引用）
        from api.routes.monitoring import init_agent as _monitoring_init
        from api.routes.agent import init_agent as _agent_init
        try:
            _monitoring_init()
            _agent_init()
        except Exception:
            logger.exception("删除 Provider 后 Agent 重新初始化失败")
        return {"status": "ok", "message": f"已删除 Provider '{provider_id}'"}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError:
        raise HTTPException(404, f"Provider '{provider_id}' 不存在")


@router.post("/settings")
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
    cfg.enable_loop_guard = bool(req.enable_loop_guard)
    cfg.enable_self_evolution = bool(req.enable_self_evolution)
    cfg.enable_self_healing = bool(req.enable_self_healing)
    cfg.self_healing_interval_seconds = max(10, int(req.self_healing_interval_seconds or 600))
    cfg.api_max_retries = max(0, int(req.api_max_retries or 0))
    cfg.api_timeout_seconds = max(60.0, float(req.api_timeout_seconds or 120.0))
    cfg.api_host_ips = req.api_host_ips or cfg.api_host_ips
    cfg.context_window_tokens = max(0, int(req.context_window_tokens or 0))
    # 审核模型：仅在显式提交时更新，避免未提交此字段的请求（如 quickSwitch）清空
    if req.review_provider_id is not None:
        cfg.review_provider_id = req.review_provider_id
    if req.review_model is not None:
        cfg.review_model = req.review_model
    cfg.tavily_search_enabled = bool(req.tavily_search_enabled)
    if req.tavily_api_key:
        cfg.tavily_api_key = req.tavily_api_key
    cfg.tavily_search_url = req.tavily_search_url or cfg.tavily_search_url or "https://api.tavily.com/search"
    if req.anysearch_api_key:
        cfg.anysearch_api_key = req.anysearch_api_key
    cfg.update_server = req.update_server or cfg.update_server
    
    # 持久化到文件（现在包含 API Key）
    cfg.save()
    
    # 也设到环境变量（当前进程生效）
    os.environ["LLM_API_KEY"] = cfg.api_key
    os.environ["OPENAI_API_KEY"] = cfg.api_key
    os.environ["LLM_MODEL"] = cfg.model
    os.environ["LLM_PROVIDER"] = cfg.active_provider
    os.environ["AGENT_RECURSION_LIMIT"] = str(cfg.recursion_limit)
    os.environ["AGENT_ENABLE_LOOP_GUARD"] = "1" if cfg.enable_loop_guard else "0"
    os.environ["AGENT_SELF_EVOLUTION"] = "1" if cfg.enable_self_evolution else "0"
    os.environ["AGENT_SELF_HEALING"] = "1" if cfg.enable_self_healing else "0"
    os.environ["AGENT_SELF_HEALING_INTERVAL"] = str(cfg.self_healing_interval_seconds)
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
    if cfg.update_server:
        os.environ["AGENT_UPDATE_SERVER"] = cfg.update_server
    else:
        os.environ.pop("AGENT_UPDATE_SERVER", None)
    
    # 重启 Agent（同时同步 monitoring 与 agent 路由的引用，
    # 确保 /health 读到新模型、聊天接口用上新的 LLM client）
    from api.routes.monitoring import init_agent as _monitoring_init
    from api.routes.agent import init_agent as _agent_init
    try:
        _monitoring_init()
        _agent_init()
        return {"status": "ok", "message": "设置已保存，Agent 已重新初始化"}
    except Exception as e:
        logger.exception("保存设置后 Agent 重新初始化失败")
        return {"status": "error", "message": f"设置已保存，但 Agent 初始化失败: {str(e)}"}


@router.get("/users", response_model=list[UserInfo])
def list_users():
    """列出所有用户"""
    return [UserInfo(**u) for u in user_manager.list_users()]


@router.post("/users", response_model=UserInfo)
def create_user(req: CreateUserRequest):
    """创建新用户"""
    try:
        user = user_manager.create_user(req.user_id, req.name, req.role)
        return UserInfo(**user)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.put("/users/{user_id}/role")
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


@router.delete("/users/{user_id}")
def delete_user(user_id: str):
    """删除用户"""
    ok = user_manager.delete_user(user_id)
    if not ok:
        raise HTTPException(404, "用户不存在")
    return {"status": "ok", "message": f"已删除用户 {user_id}"}


@router.get("/users/me")
def get_my_user(request: Request):
    """获取当前登录用户的信息"""
    uid = _get_current_user(request)
    from main import agent
    if agent:
        agent.set_user(uid)
    user = user_manager.get_user(uid)
    if not user:
        # 首次登录时自动创建用户
        user = user_manager.create_user(uid, uid)
    return user


@router.post("/system/restart", response_model=RestartResponse)
def restart_backend(request: Request):
    """重启后端服务（仅管理员）"""
    _require_admin(request)
    try:
        # 通过退出进程让外部监管（systemd / guardian / 启动脚本）完成重启
        # 先返回响应，再异步退出，避免连接被重置导致前端拿不到结果
        logger.info("收到重启请求，将在 0.3 秒后退出进程")
        import threading

        def _do_exit():
            try:
                import time
                time.sleep(0.3)
            except Exception:
                pass
            os._exit(0)

        threading.Thread(target=_do_exit, daemon=True).start()
        return RestartResponse(status="ok", message="后端正在重启，请稍候...")
    except Exception as e:
        logger.exception("重启后端失败")
        raise HTTPException(500, f"重启失败: {str(e)}")
