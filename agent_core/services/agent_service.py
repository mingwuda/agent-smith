"""Agent 调用 / 会话辅助函数。提取自 main.py"""
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Optional
import threading

from fastapi import Request

import session_store
import user_manager
from api.deps import _get_current_user, _workspace_for_user
from tools import file_tools, browser_tools, shell_tools, git_tools, database_tool
from skills.registry import get_registry
from memory.local_memory import get_memory
from logger import get_logger

logger = get_logger(__name__)


# ── 从 main.py 导出的函数 ──


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


def _image_model_override(attachments: list[dict]) -> str:
    from main import agent
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


def _save_assistant_result(uid: str, session_id: str, user_message: str, result: str, steps: Optional[list[dict]] = None, todo_list: Optional[dict] = None):
    # 存储前剥离历史浏览器截图引用，防止旧截图 URL 持久化到 session store
    result = _strip_screenshot_urls(result)
    content = result
    if steps or todo_list:
        payload = {"text": result}
        if steps:
            payload["steps"] = steps
        if todo_list:
            payload["todo_list"] = todo_list
        content = json.dumps(payload, ensure_ascii=False)
    session_store.add_message(uid, session_id, "assistant", content)
    title = user_message[:30] + ("..." if len(user_message) > 30 else "")
    session_store.rename_session(uid, session_id, title or f"会话 {session_id[:8]}")


def _strip_screenshot_urls(text: str) -> str:
    """移除文本中的浏览器截图 Markdown 图片引用。"""
    if not text or "/api/screenshot" not in text:
        return text
    cleaned = re.sub(r"!\[([^\]]*)\]\(/api/screenshot\?token=[^)]+\)", "", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _resolve_user(request: Request) -> str:
    """从请求获取当前用户并设置到 agent"""
    uid = _get_current_user(request)
    file_tools.set_workspace(_workspace_for_user(uid))
    shell_tools.set_workspace(_workspace_for_user(uid))
    browser_tools.set_workspace(_workspace_for_user(uid))
    from main import agent
    if agent:
        agent.set_user(uid)
    # 设置数据库交互上下文（角色和用户信息，后续可从用户配置扩展）
    try:
        user = user_manager.get_user(uid) or {}
        role = user.get("role", "")
        user_context = {"user_id": uid, "role": role, **(user.get("context", {}) or {})}
        database_tool.set_db_context(role=role, user_context=user_context)
    except Exception:
        database_tool.set_db_context()  # fallback：无上下文
    return uid


def _apply_session_workspace(uid: str, session_id: str, project_id: str = ""):
    """根据会话/项目设置的工作目录覆盖当前工具的工作区。

    解析优先级（方案B：项目目录优先于会话级 workspace）：
      1. 前端实时传入的 project_id 对应项目目录（用户当前选中的项目，立即生效）
      2. 会话关联项目的 directory_path（get_session_workspace 已反转优先级）
      3. 会话自身 workspace（兜底）
    设置成功后同步告知 Agent 实例当前工作目录，使其能动态修正系统提示。
    """
    ws = ""
    # 1. 前端实时项目目录优先（用户当前选中的项目，无需等会话表同步）
    if project_id:
        dp = session_store.get_project_workspace(uid, project_id)
        if dp:
            ws = dp
    # 2/3. 回退到会话/项目解析
    if not ws:
        ws = session_store.get_session_workspace(uid, session_id)
    # 最终工作目录：明确目录优先，否则回落默认用户工作区
    # （保证 system_prompt 与工具 cwd 始终一致）
    effective = ws or str(_workspace_for_user(uid))
    try:
        ws_path = Path(effective).expanduser().resolve()
        # 即使目录不存在也设置工作目录（保证 system_prompt 与工具 cwd 一致）
        # 工具调用时若目录不存在会自行报错，但 agent 必须知道当前会话的预期工作区
        file_tools.set_workspace(ws_path)
        shell_tools.set_workspace(ws_path)
        browser_tools.set_workspace(ws_path)
        try:
            git_tools.set_workspace(ws_path)
        except Exception:
            pass
        # 同步当前工作目录给 Agent（用于动态修正系统提示里的工作区路径）
        try:
            from main import agent
            if agent:
                agent.set_workspace(str(ws_path))
        except Exception:
            pass
    except Exception:
        pass
    # 触发会话级 MCP 重载（后台线程执行，不阻塞当前请求）
    try:
        _apply_session_mcp(uid, session_id)
    except Exception:
        pass


# ── 会话级 MCP 重载（按工作目录 .mcp.json 合并全局配置） ──

_last_mcp_workspace: Optional[str] = None
_last_mcp_lock = threading.Lock()


def _apply_session_mcp(uid: str, session_id: str):
    """根据会话工作目录的 .mcp.json（合并全局配置）后台重载 MCP 工具。

    同一 workspace 仅在首次/切换时触发一次重载，避免连续消息反复重建连接。
    重载在 daemon 后台线程执行，不阻塞 HTTP 请求线程。
    """
    ws = session_store.get_session_workspace(uid, session_id)
    ws_key = ws or "__global__"
    with _last_mcp_lock:
        if _last_mcp_workspace == ws_key:
            return  # 同一 workspace 已加载，跳过重复 reload
        _last_mcp_workspace = ws_key

    try:
        ws_path = str(Path(ws).expanduser().resolve()) if ws else ""
    except Exception:
        ws_path = ""
    threading.Thread(target=_reload_mcp_in_thread, args=(ws_path,), daemon=True).start()


def _reload_mcp_in_thread(workspace: str):
    """后台线程：计算合并配置 → 重载 MCP 连接 → 替换 agent 工具列表。"""
    try:
        from tools.mcp_tools import build_effective_config, reload_mcp_sync
        configs = build_effective_config(workspace)
        new_tools = reload_mcp_sync(configs)
        # 延迟导入 main，避免循环依赖
        from main import app
        agent = getattr(app.state, "agent", None)
        base = getattr(app.state, "base_tools", None)
        if agent is not None and base is not None:
            agent.set_tools(list(base) + new_tools)
            logger.info("[MCP] 会话级重载完成：%d 个 MCP 工具（workspace=%s）", len(new_tools), workspace or "全局")
    except Exception:
        import traceback
        traceback.print_exc()


async def _async_reflect(uid: str, user_message: str, steps: list[dict], result: str, outcome: str = "success"):
    """后台任务反思，总结可复用模式/踩坑并存入长期记忆。

    outcome="success"：成功路径（既有行为，始终启用，存储为 technique）。
    outcome="error"：失败路径（新能力，仅在 enable_self_evolution 开启时记录 pitfall）。
    reflection 现在为结构化 dict {t, v}（向后兼容旧纯字符串读取）。
    """
    try:
        from main import agent
        if not agent:
            return
        reflection = await agent.reflect_on_task(user_message, steps, result, outcome=outcome)
        if not reflection:
            return
        # 新类型（踩坑/偏好）仅在自进化开关开启时落盘，避免回归既有行为
        if reflection.get("t") in ("pitfall", "preference") and not agent.config.enable_self_evolution:
            return
        t = reflection.get("t", "technique")
        prefix = "_avoid_" if t == "pitfall" else "_learned_"
        key = f"{prefix}{hashlib.md5((reflection.get('v', '') + t).encode()).hexdigest()[:12]}"
        mem = get_memory(uid)
        if mem.get(key) is None:  # 不覆盖已有记录
            mem.set(key, reflection)
    except Exception:
        pass


async def _reflect_from_feedback(uid: str, session_id: str, rating: int, correction: str):
    """根据用户反馈触发一次反思，产出 preference / pitfall 记忆。

    仅在 enable_self_evolution 开启时生效（自进化新能力）。
    """
    try:
        from main import agent
        if not agent or not agent.config.enable_self_evolution:
            return
        session = session_store.get_session(uid, session_id)
        messages = (session or {}).get("messages", [])
        user_msg = ""
        assistant_content = ""
        for m in reversed(messages):
            role = m.get("role")
            content = m.get("content", "")
            if role == "user" and not user_msg:
                user_msg = content
            elif role == "assistant" and not assistant_content:
                assistant_content = content
            if user_msg and assistant_content:
                break
        reflection = await agent.reflect_on_task(
            user_msg, steps=[], result=assistant_content or "",
            outcome="feedback", feedback=correction or "",
        )
        if not reflection:
            return
        t = reflection.get("t", "preference")
        prefix = "_avoid_" if t == "pitfall" else "_learned_"
        key = f"{prefix}{hashlib.md5((reflection.get('v', '') + t).encode()).hexdigest()[:12]}"
        mem = get_memory(uid)
        if mem.get(key) is None:
            mem.set(key, reflection)
    except Exception:
        pass
