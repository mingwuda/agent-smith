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


def test_run_config_thread_id_is_full_key_so_writes_are_readable():
    """回归：_run_config 必须把「裸会话 ID」转成完整 thread_key 后写入 configurable.thread_id。

    历史 bug：stream_run 里 run_config = _run_config(tid) 直接把裸 tid 当 thread_key，
    于是工具按裸 key 写 _TODO_CACHE，而运行链路用完整 key peek → 恒为 None，
    todo 事件永不发出（前端任务清单面板消失）。同时 inbox 注入钩子要求 key 含 ":",
    裸 key 会被直接跳过 → 实时干预静默失效。
    """
    from types import SimpleNamespace

    from agent_run import AgentRunMixin

    class _Stub(AgentRunMixin):
        def __init__(self):
            self._user_id = "uid"
            self._thread_id = "fallback-sid"
            self.config = SimpleNamespace(recursion_limit=60, enable_loop_guard=True)

    stub = _Stub()

    # 1) 传裸 tid：config 里的 thread_id 必须是完整 key
    cfg = stub._run_config("S1")
    assert cfg["configurable"]["thread_id"] == "uid:S1"

    # 2) 不传参：回落到 self._thread_id，同样要带 uid 前缀
    cfg2 = stub._run_config()
    assert cfg2["configurable"]["thread_id"] == "uid:fallback-sid"

    # 3) 端到端：工具按该 config 写入，运行链路按 _thread_key(tid) 必须能读到
    manage_todo.invoke(
        {"action": "create_todo", "items": ["步骤一"]},
        config=cfg,
    )
    run_link_key = stub._thread_key("S1")
    assert peek_todo_list(run_link_key)["items"][0]["content"] == "步骤一"
    assert pop_todo_list(run_link_key) is not None
    assert peek_todo_list(run_link_key) is None
