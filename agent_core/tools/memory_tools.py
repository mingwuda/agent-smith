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
def remember(key: str, value: str) -> str:
    """显式保存一条长期记忆。仅当用户明确要求“记住/以后记得/保存为偏好”时使用。不要保存密码、API Key、Cookie、Token 等敏感信息。"""
    if not key.strip():
        return "❌ 记忆 key 不能为空"
    if not value.strip():
        return "❌ 记忆内容不能为空"
    blocked = ["api key", "apikey", "password", "cookie", "token", "secret", "密码", "密钥", "令牌"]
    text = f"{key} {value}".lower()
    if any(word in text for word in blocked):
        return "❌ 这看起来像敏感凭据，不会写入长期记忆"
    return get_memory().set(key.strip(), value.strip())


@tool
def recall_memory(query: str) -> str:
    """搜索长期记忆。需要查找用户偏好、长期约定、项目事实或常用环境信息时使用。"""
    return get_memory().search(query.strip())


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
