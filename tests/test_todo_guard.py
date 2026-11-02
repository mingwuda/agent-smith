"""任务清单收尾守卫（post_model_hook）与结束兜底提示的测试。

背景：模型常在做完活后直接给最终回答，忘了把 todo 标 done，UI 停在「3/4 已完成」。
守卫在「模型无 tool_calls（准备结束）」时注入一次提醒并 Command(goto="agent")，
让模型再走一轮把清单收尾；agent_run 结束时还有一次如实提示兜底。
"""
from agent_core.main import app  # noqa: F401  触发 agent_core 的 sys.path 注入

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.prebuilt import create_react_agent
from langgraph.types import Command

# 必须用运行时的顶层模块名导入（agent_core 已在 sys.path 中）：
# 否则 agent_core.tools.todo_tools 与工具内部 `from tools.todo_tools` 是两份模块、
# _TODO_CACHE 各一份，守卫会读不到测试塞进去的清单。
from agent_helpers import _TODO_GUARD_MARK, _make_todo_guard_post_hook
from tools.todo_tools import (
    _TODO_CACHE,
    manage_todo,
    unfinished_items,
    unfinished_notice,
)

THREAD_KEY = "guard_test_user:sess1"


class _FakeToolModel(FakeMessagesListChatModel):
    """FakeMessagesListChatModel 不实现 bind_tools；create_react_agent 需要它。"""

    def bind_tools(self, tools, **kwargs):  # noqa: D102
        return self


def _todo(statuses):
    return {
        "type": "todo_list",
        "items": [
            {"id": f"todo_{i}", "content": f"步骤{i}", "status": s}
            for i, s in enumerate(statuses, start=1)
        ],
        "summary": "",
    }


def _cfg(key=THREAD_KEY):
    return {"configurable": {"thread_id": key}}


def _hook_state(*messages):
    return {"messages": list(messages)}


# ── 纯函数 helper ─────────────────────────────────────────────

def test_unfinished_items_filters_resolved():
    todo = _todo(["done", "pending", "in_progress", "blocked"])
    assert [i["id"] for i in unfinished_items(todo)] == ["todo_2", "todo_3"]
    assert unfinished_items(None) == []


def test_unfinished_notice_empty_when_all_resolved():
    assert unfinished_notice(_todo(["done", "blocked"])) == ""
    assert unfinished_notice(None) == ""


def test_unfinished_notice_lists_items():
    notice = unfinished_notice(_todo(["done", "pending"]))
    assert "还有 1 项未完成" in notice
    assert "步骤2" in notice


# ── 守卫各分支 ───────────────────────────────────────────────

def test_guard_passes_without_todo():
    _TODO_CACHE.pop(THREAD_KEY, None)
    hook = _make_todo_guard_post_hook()
    assert hook(_hook_state(AIMessage(content="最终回答")), None, _cfg()) is None


def test_guard_passes_when_all_done():
    _TODO_CACHE[THREAD_KEY] = _todo(["done", "done"])
    try:
        hook = _make_todo_guard_post_hook()
        assert hook(_hook_state(AIMessage(content="最终回答")), None, _cfg()) is None
    finally:
        _TODO_CACHE.pop(THREAD_KEY, None)


def test_guard_passes_on_tool_call_turn():
    _TODO_CACHE[THREAD_KEY] = _todo(["pending"])
    try:
        hook = _make_todo_guard_post_hook()
        ai = AIMessage(
            content="",
            tool_calls=[{"name": "manage_todo", "args": {"action": "create_todo"}, "id": "c1"}],
        )
        assert hook(_hook_state(HumanMessage(content="hi"), ai), None, _cfg()) is None
    finally:
        _TODO_CACHE.pop(THREAD_KEY, None)


def test_guard_injects_reminder_and_gotos_agent():
    _TODO_CACHE[THREAD_KEY] = _todo(["done", "pending"])
    try:
        hook = _make_todo_guard_post_hook()
        cmd = hook(_hook_state(HumanMessage(content="hi"), AIMessage(content="最终回答")), None, _cfg())
        assert isinstance(cmd, Command)
        assert cmd.goto == "agent"
        injected = cmd.update["messages"][-1]
        assert _TODO_GUARD_MARK in injected.content
        assert "todo_2" in injected.content
        assert "步骤2" in injected.content
    finally:
        _TODO_CACHE.pop(THREAD_KEY, None)


def test_guard_passes_when_waiting_for_confirm_or_permission():
    """高危命令确认 / 文件授权是「故意停下等用户」，守卫不得逼模型继续。"""
    from langchain_core.messages import ToolMessage
    _TODO_CACHE[THREAD_KEY] = _todo(["pending"])
    try:
        hook = _make_todo_guard_post_hook()
        for marker in (
            "__CONFIRM_NEEDED__::高危命令::__CMD__::rm -rf /tmp/x",
            "__PERMISSION_NEEDED__:/etc/hosts",
        ):
            msgs = [
                HumanMessage(content="hi"),
                ToolMessage(content=marker, tool_call_id="call_1"),
                AIMessage(content="请先确认/授权"),
            ]
            assert hook({"messages": msgs}, None, _cfg()) is None, marker
    finally:
        _TODO_CACHE.pop(THREAD_KEY, None)


def test_guard_only_nudges_once():
    """已有提醒标记时不得再次介入，避免与模型拉锯成死循环。"""
    _TODO_CACHE[THREAD_KEY] = _todo(["pending"])
    try:
        hook = _make_todo_guard_post_hook()
        msgs = [
            HumanMessage(content="hi"),
            AIMessage(content="最终回答"),
            HumanMessage(content=f"{_TODO_GUARD_MARK}\n请收尾"),
            AIMessage(content="还是没改"),
        ]
        assert hook({"messages": msgs}, None, _cfg()) is None
    finally:
        _TODO_CACHE.pop(THREAD_KEY, None)


# ── 集成：守卫真的让模型多跑一轮并调用 manage_todo ──────────────

def test_guard_integration_completes_todo():
    _TODO_CACHE[THREAD_KEY] = _todo(["done", "pending"])
    try:
        model = _FakeToolModel(responses=[
            AIMessage(content="做完了，给你最终回答"),          # 第 1 轮：想收尾 → 被拦截
            AIMessage(content="", tool_calls=[{                   # 第 2 轮：按提醒更新清单
                "name": "manage_todo",
                "args": {"action": "complete_todo", "todo_id": "todo_2"},
                "id": "call_todo",
            }]),
            AIMessage(content="清单已收尾：2/2 完成"),           # 第 3 轮：最终回答
        ])
        agent = create_react_agent(
            model, [manage_todo], post_model_hook=_make_todo_guard_post_hook()
        )
        out = agent.invoke({"messages": [HumanMessage(content="做吧")]}, _cfg())

        contents = [str(m.content) for m in out["messages"] if isinstance(m, AIMessage)]
        assert contents[-1] == "清单已收尾：2/2 完成", contents
        assert len(contents) == 3, f"应发生 3 轮模型调用，实际 {len(contents)}: {contents}"
        assert _TODO_CACHE[THREAD_KEY]["items"][1]["status"] == "done"
    finally:
        _TODO_CACHE.pop(THREAD_KEY, None)
