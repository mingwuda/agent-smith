"""Agent 调用 / 会话辅助函数。提取自 main.py"""
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Optional

from fastapi import Request

from .. import session_store
from .. import user_manager
from ..api.deps import _get_current_user, _workspace_for_user
from ..tools import file_tools, browser_tools, shell_tools, git_tools, database_tool
from ..skills.registry import get_registry
from ..memory.local_memory import get_memory


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
    from ..main import agent
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
    # 存储前剥离历史浏览器截图引用，防止旧截图 URL 持久化到 session store
    result = _strip_screenshot_urls(result)
    content = result
    if steps:
        content = json.dumps({"text": result, "steps": steps}, ensure_ascii=False)
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
    from ..main import agent
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


def _apply_session_workspace(uid: str, session_id: str):
    """根据会话设置的工作目录覆盖当前工具的工作区。"""
    ws = session_store.get_session_workspace(uid, session_id)
    if not ws:
        return
    try:
        ws_path = Path(ws).expanduser().resolve()
        if ws_path.is_dir():
            file_tools.set_workspace(ws_path)
            shell_tools.set_workspace(ws_path)
            browser_tools.set_workspace(ws_path)
            try:
                git_tools.set_workspace(ws_path)
            except Exception:
                pass
    except Exception:
        pass


async def _async_reflect(uid: str, user_message: str, steps: list[dict], result: str):
    """后台任务反思，总结可复用模式并存入长期记忆。"""
    try:
        from ..main import agent
        if not agent:
            return
        reflection = await agent.reflect_on_task(user_message, steps, result)
        if reflection:
            key = f"_learned_{hashlib.md5(reflection.encode()).hexdigest()[:12]}"
            mem = get_memory(uid)
            existing = mem.get(key)
            if existing is None:  # 不覆盖已有记录
                mem.set(key, reflection)
    except Exception:
        pass
