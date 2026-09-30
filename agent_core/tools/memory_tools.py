"""长期记忆工具"""
import json
from typing import Any

from langchain_core.tools import tool

from memory.local_memory import get_memory


def _value_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


@tool
def remember(key: str, value: str, ttl: int = 0) -> str:
    """显式保存一条长期记忆。仅当用户明确要求“记住/以后记得/保存为偏好”时使用。不要保存密码、API Key、Cookie、Token 等敏感信息。ttl 为可选有效期（秒），0 表示永不过期。"""
    if not key.strip():
        return "❌ 记忆 key 不能为空"
    if not value.strip():
        return "❌ 记忆内容不能为空"
    blocked = ["api key", "apikey", "password", "cookie", "token", "secret", "密码", "密钥", "令牌"]
    text = f"{key} {value}".lower()
    if any(word in text for word in blocked):
        return "❌ 这看起来像敏感凭据，不会写入长期记忆"
    return get_memory().set(key.strip(), value.strip(), ttl=ttl if ttl and ttl > 0 else None)


@tool
def recall_memory(query: str, project: str = "", track: str = "", min_score: int = 0) -> str:
    """搜索长期记忆。需要查找用户偏好、长期约定、项目事实或常用环境信息时使用。

    可选正交维度（不传则全局搜索）：
    - project: 仅在某项目下检索（agent 调用时传当前 project_id，实现「按项目精准命中」）
    - track: "user"（用户偏好/经验）或 "agent"（agent 完成任务沉淀的 Case/技能）；默认全局
    - min_score: 相关性阈值（>=0）。>0 时过滤掉仅因「子串偶然重合」命中的弱相关项，降低噪音。
      默认 0 表示不过滤（返回全部命中，由 LLM 自行判断）。建议在有大量弱命中时调大（如 30）。

    本工具默认排除压缩归档（_ctx_old_）——那些是上下文压缩暂存的早期用户指令，
    不是经验/偏好/技能，避免模糊回忆时与真实记忆并列、淹没 agent 判断（P0-2）。
    """
    query = (query or "").strip()
    track = (track or "").strip().lower()
    if not project and track not in ("user", "agent"):
        return get_memory().search(query, min_score=min_score, exclude_archive=True)
    return get_memory().search_scoped(
        query,
        track=track if track in ("user", "agent") else None,
        project=project or None,
        min_score=min_score,
        exclude_archive=True,
    )


@tool
def forget_memory(key: str) -> str:
    """删除一条长期记忆。仅当用户明确要求忘记/删除某条记忆时使用。"""
    if not key.strip():
        return "❌ 记忆 key 不能为空"
    return get_memory().delete(key.strip())


@tool
def list_memories() -> str:
    """列出所有长期记忆 key。"""
    keys = get_memory().list_keys()
    return "\n".join(keys) if keys else "暂无长期记忆"


TOOLS = [remember, recall_memory, forget_memory, list_memories]
