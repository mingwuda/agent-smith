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
    typesafe_api_key: str = ""
    jev_compaction_enabled: bool = False
    enabled_plugins: list[str] = []
    plugin_dirs: str = ""
    review_provider_id: str = ""
    review_model: str = ""
    update_server: str = ""
    provider_order: list[str] = []
    model_order: dict[str, list[str]] = {}  # {provider_id: [model, ...]}
    llm_idle_timeout_seconds: float = 60.0
    llm_idle_max_retries: int = 2
    llm_hard_timeout_seconds: float = 600.0


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
    from app_state import get_ui_dir, get_html_content
    ui_dir = get_ui_dir()
    ui_index = (ui_dir / "index.html") if ui_dir else None
    content = get_html_content()
    if ui_index is not None and ui_index.exists():
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


@router.get("/plugins")
def list_plugins(request: Request):
    """列出全部可发现的插件（含启用状态与加载状态），供设置页展示开关。

    插件化机制参考 deepseek-harness 的 plugin-manager：贡献能力（工具/钩子）、
    config 层启用、故障隔离。本端点仅作只读侦察，不触发加载。
    """
    _require_admin(request)
    cfg = AgentConfig.load()
    from plugin_loader import PluginRegistry
    from config import _split_path_list
    enabled = list(cfg.enabled_plugins or [])
    reg = PluginRegistry(
        enabled_plugins=enabled,
        plugin_dirs=_split_path_list(cfg.plugin_dirs),
    )
    records = reg.discover()
    result = []
    for rec in records:
        result.append(rec.to_dict())
        result[-1]["enabled"] = rec.id in set(enabled)
    return {
        "plugins": result,
        "enabled_plugins": enabled,
        "plugin_dirs": cfg.plugin_dirs,
    }


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
            _monitoring_init(caller="system.delete_provider", force=True)
            _agent_init(caller="system.delete_provider", force=True)
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
    cfg.llm_idle_timeout_seconds = max(5.0, float(req.llm_idle_timeout_seconds or 60.0))
    cfg.llm_idle_max_retries = max(0, int(req.llm_idle_max_retries or 2))
    cfg.llm_hard_timeout_seconds = max(30.0, float(req.llm_hard_timeout_seconds or 600.0))
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
    if req.typesafe_api_key:
        cfg.typesafe_api_key = req.typesafe_api_key
    cfg.jev_compaction_enabled = bool(req.jev_compaction_enabled)
    cfg.enabled_plugins = list(req.enabled_plugins or [])
    if req.plugin_dirs:
        cfg.plugin_dirs = req.plugin_dirs
    cfg.update_server = req.update_server or cfg.update_server

    # 保存排序
    if req.provider_order:
        cfg.set_provider_order(req.provider_order)
    if req.model_order:
        for pid, order in req.model_order.items():
            cfg.set_model_order(pid, order)

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
    os.environ["AGENT_LLM_IDLE_TIMEOUT_SECONDS"] = str(cfg.llm_idle_timeout_seconds)
    os.environ["AGENT_LLM_IDLE_MAX_RETRIES"] = str(cfg.llm_idle_max_retries)
    os.environ["AGENT_LLM_HARD_TIMEOUT_SECONDS"] = str(cfg.llm_hard_timeout_seconds)
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
    if cfg.typesafe_api_key:
        os.environ["TYPESAFE_API_KEY"] = cfg.typesafe_api_key
    else:
        os.environ.pop("TYPESAFE_API_KEY", None)
    os.environ["AGENT_JEV_COMPACTION_ENABLED"] = "1" if cfg.jev_compaction_enabled else "0"
    os.environ["AGENT_PLUGIN_DIRS"] = cfg.plugin_dirs or ""
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
        _monitoring_init(caller="system.save_settings", force=True)
        _agent_init(caller="system.save_settings", force=True)
        return {"status": "ok", "message": "设置已保存，Agent 已重新初始化"}
    except Exception as e:
        logger.exception("保存设置后 Agent 重新初始化失败")
        return {"status": "error", "message": f"设置已保存，但 Agent 初始化失败: {str(e)}"}


class SettingsOrderRequest(BaseModel):
    """仅用于保存 provider 级轻量元数据（顺序 / 视觉模型标记）。

    不影响 API Key / model / base_url 等其他设置。
    """
    provider_order: list[str] = []
    model_order: dict[str, list[str]] = {}
    vision_models: dict[str, list[str]] = {}  # {provider_id: [model, ...] 支持图片输入的模型}


@router.post("/settings/order")
def save_settings_order(req: SettingsOrderRequest, request: Request):
    """仅持久化 provider / model 的显示顺序。

    与 POST /settings 的区别：本端点不会调用 update_provider，
    因此不会因为缺失字段而清空 active_provider / api_key / model。
    """
    _require_admin(request)
    cfg = AgentConfig.load()
    if req.provider_order:
        cfg.set_provider_order(req.provider_order)
    if req.model_order:
        for pid, order in req.model_order.items():
            cfg.set_model_order(pid, order)
    # 视觉模型标记：始终遍历，空列表表示"该厂商已无视觉模型"，需要能清空
    for pid, models in req.vision_models.items():
        cfg.set_vision_models(pid, models)
    cfg.save()

    # 视觉模型标记变化后，立即把磁盘上的最新 config 重新加载到运行中的 agent 实例，
    # 使"点 👁 即时生效"，无需重启后端或点"保存设置"。
    # 仅轻量替换 config 对象，不触发全量 reinit（避免重启微信 bot）。
    if req.vision_models:
        try:
            from app_state import get_agent, set_agent_config
            refreshed = AgentConfig.load()
            ag = get_agent()
            if ag is not None:
                ag.config = refreshed
            set_agent_config(refreshed)
        except Exception:
            logger.exception("视觉模型标记已保存，但热更新运行中的 Agent 配置失败")

    return {"status": "ok", "message": "排序已保存"}


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
    from app_state import get_agent
    if get_agent():
        get_agent().set_user(uid)
    user = user_manager.get_user(uid)
    if not user:
        # 首次登录时自动创建用户
        user = user_manager.create_user(uid, uid)
    return user


@router.get("/users/me/settings")
def get_my_settings(request: Request):
    """获取当前用户的设置（继承全局配置，支持 per-user 覆盖）"""
    uid = _get_current_user(request)
    if not uid:
        raise HTTPException(401, "未登录")
    from user_config import get_user_effective_config
    return get_user_effective_config(uid)


@router.post("/users/me/settings")
def save_my_settings(req: SettingsRequest, request: Request):
    """保存当前用户的设置（仅保存到用户级配置，不覆盖全局）"""
    uid = _get_current_user(request)
    if not uid:
        raise HTTPException(401, "未登录")
    from user_config import save_user_config
    save_user_config(uid, req.dict(exclude_none=True))
    return {"status": "ok", "message": "用户设置已保存"}


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
