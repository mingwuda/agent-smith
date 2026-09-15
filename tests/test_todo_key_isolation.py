"""Todo 清单缓存 key 隔离回归测试

根因：manage_todo 工具按 LangGraph config 的完整 thread_id（"uid:sessionId"）
写入 _TODO_CACHE，而运行链路曾用裸 sessionId 清理、用无参 get_todo_list()
（取缓存里"第一个非 None"）读取，导致旧会话清单永久残留并被新会话误显示。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_core"))

from tools.todo_tools import (  # noqa: E402
    set_todo_list,
    peek_todo_list,
    pop_todo_list,
    get_todo_list,
    manage_todo,
    _TODO_CACHE,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    _TODO_CACHE.clear()
    yield
    _TODO_CACHE.clear()


def _todo(content: str, status: str = "pending") -> dict:
    return {"type": "todo_list", "items": [{"id": "todo_1", "content": content, "status": status}]}


def test_peek_isolates_by_full_thread_key():
    set_todo_list("uid:A", _todo("旧任务A", "done"))
    set_todo_list("uid:B", _todo("新任务B"))

    assert peek_todo_list("uid:B")["items"][0]["content"] == "新任务B"
    assert peek_todo_list("uid:A")["items"][0]["content"] == "旧任务A"


def test_pop_with_full_key_clears_cache_bare_tid_does_not():
    # 完整 key 能清掉（修复后的正确做法）
    set_todo_list("uid:A", _todo("任务A"))
    assert pop_todo_list("uid:A") is not None
    assert peek_todo_list("uid:A") is None

    # 旧 bug：用裸 sessionId 清理 → key 不匹配，清单残留
    set_todo_list("uid:B", _todo("任务B"))
    assert pop_todo_list("B") is None
    assert peek_todo_list("uid:B") is not None, "裸 tid 清不掉完整 key 的缓存（这正是旧残留源）"


def test_no_param_get_returns_stale_other_session():
    # 契约固化：无参 get_todo_list 会取到"第一个非 None"，因此运行链路绝不能用它。
    set_todo_list("uid:A", _todo("旧任务A"))
    set_todo_list("uid:B", _todo("新任务B"))
    assert get_todo_list()["items"][0]["content"] == "旧任务A"


def test_peek_does_not_restore_from_disk():
    # set 会落盘；清掉内存缓存后，peek 不应把磁盘旧清单捞回来
    set_todo_list("uid:C", _todo("上一轮清单"))
    _TODO_CACHE.clear()

    assert peek_todo_list("uid:C") is None
    # 工具内部的 get 仍允许磁盘恢复（用户说"继续"时 update/add 的正当场景）
    assert get_todo_list("uid:C")["items"][0]["content"] == "上一轮清单"


def test_manage_todo_writes_under_full_config_thread_id():
    # 端到端：工具写入用的 key 必须与运行链路 peek/pop 用的完整 key 一致
    cfg = {"configurable": {"thread_id": "uid:S"}}
    manage_todo.invoke(
        {"action": "create_todo", "items": ["步骤一", "步骤二"]},
        config=cfg,
    )
    assert peek_todo_list("uid:S")["items"][0]["content"] == "步骤一"
    assert pop_todo_list("uid:S") is not None
    assert peek_todo_list("uid:S") is None
