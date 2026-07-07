"""Todo 清单管理工具

Agent 在接到复杂任务（≥3 个独立步骤）时使用此工具创建可追踪的任务清单。
Todo 数据通过 yield 事件流式推送到前端，最终随 assistant message 持久化。

持久化：每个 thread_id 的 todo 清单会保存到磁盘文件，
即使进程重启或页面刷新后，用户说"继续"也能恢复。
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool


def _now() -> str:
    return datetime.now().isoformat()


# 单例 todo 清单缓存（避免频繁读盘）
_TODO_CACHE: dict[str, Optional[dict]] = {}


def _todo_dir() -> Path:
    return Path.home() / ".desktop_agent" / "todos"


def _todo_path(thread_id: str) -> Path:
    return _todo_dir() / f"{thread_id}.json"


def get_todo_list(thread_id: str = "") -> Optional[dict]:
    """获取 todo 清单。优先查缓存，缓存未命中时从磁盘加载。"""
    if thread_id:
        # 按 thread_id 查找
        cached = _TODO_CACHE.get(thread_id)
        if cached is not None:
            return cached
        # 尝试从磁盘恢复
        path = _todo_path(thread_id)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                _TODO_CACHE[thread_id] = data
                return data
            except (json.JSONDecodeError, OSError):
                pass
        return None
    
    # 兼容旧调用（无 thread_id）：从缓存中取第一个非 None
    for v in _TODO_CACHE.values():
        if v is not None:
            return v
    return None


def set_todo_list(thread_id: str, todo_list: dict):
    """设置 todo 清单并持久化到磁盘。"""
    _TODO_CACHE[thread_id] = todo_list
    path = _todo_path(thread_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(todo_list, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def pop_todo_list(thread_id: str = "") -> Optional[dict]:
    """取出并移除 todo 清单（仅清理进程内缓存，磁盘文件保留供恢复）。"""
    if not thread_id or thread_id not in _TODO_CACHE:
        return None
    result = _TODO_CACHE.pop(thread_id, None)
    return result


@tool
def manage_todo(
    action: str,
    config: RunnableConfig,
    items: Optional[list[str]] = None,
    todo_id: Optional[str] = None,
    content: Optional[str] = None,
    status: Optional[str] = None,
) -> str:
    """管理任务清单（Todo List）。当任务包含 3 个或更多独立步骤时，必须先创建清单。

    操作说明：
    - create_todo: 初始化清单。items=["步骤1", "步骤2", "步骤3"]
    - update_todo: 更新单项状态。todo_id="todo_1", status="in_progress|done|blocked"
    - add_todo: 动态新增。content="新步骤的描述"
    - complete_todo: 标记完成。todo_id="todo_1"

    Args:
        action: 操作类型（create_todo | update_todo | add_todo | complete_todo）
        items: 创建时的任务列表（仅 create_todo 使用）
        todo_id: 任务项 ID（仅 update_todo / complete_todo 使用）
        content: 新增任务内容（仅 add_todo 使用）
        status: 新状态（仅 update_todo 使用）：in_progress | done | blocked
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default")
    todo_list = get_todo_list(thread_id)
    prefix = "📋 任务清单"

    if action == "create_todo":
        if not items:
            return "❌ create_todo 需要提供 items 参数"
        now = _now()
        todo_items = []
        for i, item_content in enumerate(items, start=1):
            todo_items.append({
                "id": f"todo_{i}",
                "content": item_content,
                "status": "pending",
                "created_at": now,
                "updated_at": now,
            })
        total = len(todo_items)
        todo_list = {
            "type": "todo_list",
            "items": todo_items,
            "summary": f"共 {total} 项，已完成 0 项",
        }
        set_todo_list(thread_id, todo_list)
        return f"{prefix}已创建，共 {total} 项"

    if not todo_list:
        # 尝试从上一轮对话历史恢复（用户说"继续"时）
        return "❌ 当前没有活跃的 todo 清单，请先使用 create_todo 创建一个新的清单"

    items_list = todo_list["items"]

    if action == "add_todo":
        if not content:
            return "❌ add_todo 需要提供 content 参数"
        now = _now()
        next_id = f"todo_{len(items_list) + 1}"
        items_list.append({
            "id": next_id,
            "content": content,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
        })
        total = len(items_list)
        done_count = sum(1 for item in items_list if item["status"] == "done")
        todo_list["summary"] = f"共 {total} 项，已完成 {done_count} 项"
        set_todo_list(thread_id, todo_list)
        return f"{prefix}已新增「{content}」"

    if action == "update_todo":
        if not todo_id or not status:
            return "❌ update_todo 需要提供 todo_id 和 status 参数"
        if status not in ("pending", "in_progress", "done", "blocked"):
            return f"❌ 无效的状态: {status}，允许: pending|in_progress|done|blocked"
        for item in items_list:
            if item["id"] == todo_id:
                item["status"] = status
                item["updated_at"] = _now()
                break
        else:
            return f"❌ 未找到 todo_id={todo_id}"
        total = len(items_list)
        done_count = sum(1 for item in items_list if item["status"] == "done")
        todo_list["summary"] = f"共 {total} 项，已完成 {done_count} 项"
        set_todo_list(thread_id, todo_list)
        return f"{prefix}已更新「{todo_id}」→ {status}"

    if action == "complete_todo":
        if not todo_id:
            return "❌ complete_todo 需要提供 todo_id 参数"
        for item in items_list:
            if item["id"] == todo_id:
                item["status"] = "done"
                item["updated_at"] = _now()
                break
        else:
            return f"❌ 未找到 todo_id={todo_id}"
        total = len(items_list)
        done_count = sum(1 for item in items_list if item["status"] == "done")
        todo_list["summary"] = f"共 {total} 项，已完成 {done_count} 项"
        set_todo_list(thread_id, todo_list)
        return f"{prefix}已完成「{item['content']}」✅"

    return f"❌ 未知操作: {action}，允许: create_todo|update_todo|add_todo|complete_todo"


TOOLS = [manage_todo]
