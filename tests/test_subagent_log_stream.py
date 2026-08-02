"""子代理实时日志流回归测试：前端 /subagent-progress SSE 能实时读到执行日志。

背景（ponytail）：
- 之前 delegate_tasks_parallel 预创建 batch item，但 run_sync 内部总是 new 一个新 item，
  执行期间的 append_log 写在新 item 上 → batch item 只有"队列中，等待执行..." → 前端一直显示等待中；
- delegate_task（单发）从不 start_batch → _current_batch 为空 → SSE 轮询永远返回空。
修复：run_sync 支持复用外部传入的 item，两条委派路径都复用 batch item。

只覆盖不依赖真实 LLM/LangGraph 的日志流与状态机逻辑。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_core"))

import subagents as subagents_mod  # noqa: E402
from subagents import SubagentManager, SubagentTask  # noqa: E402


def _make_manager():
    m = SubagentManager()
    m._config = object()  # 绕过 configure 检查（不真跑 LLM）
    return m


async def _fake_run_agent(self, item):
    """模拟子代理执行：写两条日志（思考 + 工具调用）后返回结果。"""
    await asyncio.sleep(0.01)
    item.append_log("我先分析一下代码结构", "ai")
    item.append_log("调用工具: read_file", "tool")
    return "mock result"


def test_run_sync_reuses_external_item(monkeypatch):
    """复用外部 item：执行日志写入外部对象，status/result 落回同一对象。"""
    monkeypatch.setattr(SubagentManager, "_run_agent", _fake_run_agent)
    m = _make_manager()

    pre = SubagentTask(id="subagent-reuse-1", agent_type="coder", task="t")
    pre.append_log("队列中，等待执行...")
    m.start_batch([pre])

    ret = asyncio.run(m.run_sync(task="t", agent_type="coder", item=pre))

    assert ret is pre  # 返回同一对象
    assert pre.status == "done"
    assert pre.result == "mock result"
    texts = [l["text"] for l in pre.get_logs_since(0)[0]]
    assert texts[0] == "队列中，等待执行..."
    assert any("先分析" in t for t in texts)      # 思考日志可见
    assert any("read_file" in t for t in texts)   # 工具调用日志可见


def test_progress_logs_read_from_batch_item(monkeypatch):
    """前端轮询路径：get_progress_logs 能从 batch item 实时读到执行日志并正确判 done。"""
    monkeypatch.setattr(SubagentManager, "_run_agent", _fake_run_agent)
    m = _make_manager()

    pre = SubagentTask(id="subagent-prog-1", agent_type="coder", task="t")
    pre.append_log("队列中，等待执行...")
    m.start_batch([pre])

    # 执行前：只有队列日志，done=False
    lines0, total0, done0 = m.get_progress_logs(1)
    assert done0 is False
    assert len(lines0) == 1 and "队列中" in lines0[0]["text"]

    asyncio.run(m.run_sync(task="t", agent_type="coder", item=pre))

    lines, total, done = m.get_progress_logs(1)
    texts = [l["text"] for l in lines]
    assert any("先分析" in t for t in texts)
    assert any("read_file" in t for t in texts)
    assert done is True


def test_run_sync_creates_new_item_when_none(monkeypatch):
    """向后兼容：不传 item 时内部新建并注册（旧调用方不受影响）。"""
    monkeypatch.setattr(SubagentManager, "_run_agent", _fake_run_agent)
    m = _make_manager()

    item = asyncio.run(m.run_sync(task="t", agent_type="searcher"))

    assert item.id.startswith("subagent-")
    assert item.id in m._tasks
    assert item.status == "done"
    assert item.result == "mock result"


def test_delegate_task_registers_batch(monkeypatch):
    """单发 delegate_task：start_batch 已注册，SSE 轮询能读到日志（修复 Bug B）。"""
    async def fake_run_sync(self, task, agent_type="coder", context="", wall_timeout=180.0,
                            idle_timeout=60.0, item=None):
        item.append_log("🔧 调用工具: web_search", "tool")
        item.status = "done"
        item.result = "ok"
        return item
    monkeypatch.setattr(SubagentManager, "run_sync", fake_run_sync)
    m = SubagentManager()
    m._config = object()
    monkeypatch.setattr(subagents_mod, "manager", m)

    result = subagents_mod.delegate_task.func(task="搜索关键词", agent_type="searcher")

    assert "状态：done" in result
    assert len(m._current_batch) == 1
    batch_item = m._current_batch[0]
    texts = [l["text"] for l in batch_item.get_logs_since(0)[0]]
    assert "队列中" in texts[0]
    assert any("web_search" in t for t in texts)
    # 前端轮询路径可读到日志
    lines, _, done = m.get_progress_logs(1)
    assert any("web_search" in l["text"] for l in lines)
    assert done is True


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))


# ---------- 结构化工具事件（前端渲染工具卡片用） ----------

from types import SimpleNamespace


def _ai_msg(tool_calls):
    return SimpleNamespace(type="ai", content="思考", tool_calls=tool_calls)


def _tool_msg(tool_call_id, name, content, status="success"):
    return SimpleNamespace(type="tool", tool_call_id=tool_call_id, name=name,
                           content=content, status=status)


def test_collect_tool_events_start_and_end():
    """AI tool_calls → tool_start；tool 消息 → tool_end；values 全量重放不重复。"""
    from subagents import _collect_tool_events
    started, ended = set(), set()

    # 第一轮：AI 决定调用 web_search
    evs1 = _collect_tool_events(
        [_ai_msg([{"id": "call-1", "name": "web_search", "args": {"query": "x"}}])],
        started, ended,
    )
    assert len(evs1) == 1 and evs1[0]["event"] == "tool_start"
    assert evs1[0]["name"] == "web_search" and evs1[0]["args"] == {"query": "x"}

    # 第二轮（values 全量重放）：同一 AI 消息 + 工具结果 → 只新增 tool_end
    msgs = [
        _ai_msg([{"id": "call-1", "name": "web_search", "args": {"query": "x"}}]),
        _tool_msg("call-1", "web_search", "结果..."),
    ]
    evs2 = _collect_tool_events(msgs, started, ended)
    assert len(evs2) == 1 and evs2[0]["event"] == "tool_end"
    assert evs2[0]["output"] == "结果..." and evs2[0]["status"] == "success"

    # 第三轮：再次全量重放 → 无新事件
    assert _collect_tool_events(msgs, started, ended) == []


def test_collect_tool_events_error_status_and_unknown_id():
    """工具失败 status=error 透传；无 tool_call_id 或未 start 的 tool 消息忽略。"""
    from subagents import _collect_tool_events
    started, ended = {"call-9"}, set()

    evs = _collect_tool_events(
        [_tool_msg("call-9", "run_shell", "boom", status="error"),
         _tool_msg("orphan", "read_file", "x")],
        started, ended,
    )
    assert len(evs) == 1
    assert evs[0]["event"] == "tool_end" and evs[0]["status"] == "error"
    assert "orphan" not in [e["id"] for e in evs]  # 未 start 的工具消息忽略


def test_append_log_structured_fields():
    """append_log 支持结构化 extra 字段，SSE 推送的 line dict 含 tool_id/tool_args 等。"""
    item = SubagentTask(id="subagent-s1", agent_type="searcher", task="t")
    item.append_log(
        "调用工具: web_search", "tool",
        event="tool_start", tool_id="call-1", tool_name="web_search",
        tool_args={"query": "关键词"},
    )
    line = item.get_logs_since(0)[0][0]
    assert line["event"] == "tool_start"
    assert line["tool_id"] == "call-1"
    assert line["tool_args"] == {"query": "关键词"}


# ---------- 工具白名单（本地任务不联网） ----------


def _fake_tool(name):
    return SimpleNamespace(name=name)


def test_tool_filter_by_type():
    """coder/reviewer/debugger 无联网工具；searcher 仅 web_search/web_fetch。"""
    from subagents import SUBAGENT_TOOL_WHITELIST, SUBAGENT_TOOL_EXCLUDE_PREFIXES

    fake_tools = [
        _fake_tool("read_file"), _fake_tool("write_file"), _fake_tool("edit_file"),
        _fake_tool("list_files"), _fake_tool("search_files"), _fake_tool("run_python"),
        _fake_tool("run_shell"), _fake_tool("git_status"), _fake_tool("web_search"),
        _fake_tool("web_fetch"), _fake_tool("browser_navigate"), _fake_tool("browser_click"),
        _fake_tool("delegate_task"), _fake_tool("delegate_tasks_parallel"),
        _fake_tool("git_command"), _fake_tool("get_system_info"), _fake_tool("db_query"),
        _fake_tool("recall_memory"), _fake_tool("remember"), _fake_tool("manage_todo"),
        _fake_tool("append_to_file"), _fake_tool("delete_file"), _fake_tool("git_push"),
        _fake_tool("git_diff"), _fake_tool("git_log"), _fake_tool("git_show"),
    ]

    m = SubagentManager()
    m.configure(object(), fake_tools)

    for atype in ("coder", "reviewer", "debugger"):
        names = {t.name for t in m._tools_by_type[atype]}
        assert "read_file" in names and "run_shell" in names
        # 委派工具已被排除（configure 入口过滤）
        assert "delegate_task" not in names and "delegate_tasks_parallel" not in names
        # 联网工具全部排除
        for n in list(names):
            assert not n.startswith(SUBAGENT_TOOL_EXCLUDE_PREFIXES), f"{atype} 仍含联网工具 {n}"

    searcher_names = {t.name for t in m._tools_by_type["searcher"]}
    assert searcher_names == {"web_search", "web_fetch"}

    # analysis：只读探查工具集，无任何写/联网/委派工具
    a_names = {t.name for t in m._tools_by_type["analysis"]}
    for read_only in ("read_file", "list_files", "search_files", "run_python", "run_shell",
                      "git_status", "git_diff", "git_log", "git_show", "git_command",
                      "get_system_info", "db_query", "recall_memory"):
        assert read_only in a_names, f"analysis 缺少只读工具 {read_only}"
    for write_tool in ("write_file", "append_to_file", "edit_file", "insert_lines",
                       "replace_lines", "delete_file", "git_add", "git_commit",
                       "git_commit_all", "git_push", "git_revert", "remember", "forget_memory",
                       "web_search", "web_fetch", "browser_navigate", "browser_click",
                       "delegate_task", "delegate_tasks_parallel", "manage_todo"):
        assert write_tool not in a_names, f"analysis 不应含 {write_tool}"


def test_non_searcher_prompt_has_network_constraint():
    """非 searcher 子代理 prompt 明确告知无联网工具，避免误用。

    prompt 拼接逻辑在 _build_subagent_prompt（graph 预构建时绑定，运行期不再拼）。
    """
    import inspect
    import subagents as _mod

    src = inspect.getsource(_mod.SubagentManager._build_subagent_prompt)
    assert "工具使用约束" in src and "web_search" in src


def test_get_capsule_tool_events_persistence_payload():
    """subagent_end 持久化：get_capsule_tool_events 只提取 tool_start/tool_end 结构化事件。

    历史回放依赖 subagent_end 的 capsules 携带 tools 数据重建工具卡片；
    普通日志（ai/info）与无关字段不得混入，字段名与前端渲染约定一致。
    """
    m = _make_manager()
    item = SubagentTask(id="subagent-t1", agent_type="searcher", task="t")
    item.append_log("队列中，等待执行...", "info")
    item.append_log("我先搜索一下", "ai")
    item.append_log(
        "调用工具: web_search", "tool",
        event="tool_start", tool_id="call-1", tool_name="web_search",
        tool_args={"query": "关键词"},
    )
    item.append_log(
        "工具完成: web_search", "tool",
        event="tool_end", tool_id="call-1", tool_name="web_search",
        tool_output="搜索结果...", tool_status="success",
    )
    item.append_log("✅ searcher 完成", "done")
    m.start_batch([item])

    events = m.get_capsule_tool_events(1)
    assert len(events) == 2
    assert [e["event"] for e in events] == ["tool_start", "tool_end"]
    assert events[0]["tool_id"] == "call-1" and events[0]["tool_name"] == "web_search"
    assert events[0]["tool_args"] == {"query": "关键词"}
    assert events[1]["tool_output"] == "搜索结果..." and events[1]["tool_status"] == "success"
    # 不混入普通日志，不携带 ts/text/cat 等无关字段
    for e in events:
        assert "text" not in e and "ts" not in e and "cat" not in e


def test_get_capsule_tool_events_empty_and_out_of_range():
    """无工具事件 / 越界 capsule_id 返回空列表，不抛异常。"""
    m = _make_manager()
    item = SubagentTask(id="subagent-t2", agent_type="coder", task="t")
    item.append_log("只是思考", "ai")
    m.start_batch([item])

    assert m.get_capsule_tool_events(1) == []   # 有任务但无工具事件
    assert m.get_capsule_tool_events(99) == []  # 越界
    assert m.get_capsule_tool_events(0) == []   # 非法（idx=-1）


# ---------- 历史回放持久化时序（clear_batch 时机） ----------


def test_delegate_tasks_parallel_does_not_clear_batch_prematurely(monkeypatch):
    """修复回归：delegate_tasks_parallel 工具返回后 _current_batch 必须仍可读。

    背景：之前工具函数末尾调用 manager.clear_batch()，而 agent_run.on_tool_end
    在工具返回后才执行 get_capsule_logs(cap["id"]) 打包 subagent_end 事件，
    导致历史保存的 capsules 里 logs/tools 全为空，前端回放时子代理思考与
    工具卡片全部丢失。本测试断言工具函数返回后 batch 数据依然可取。
    """
    async def fake_run_all(items):
        for it in items:
            it.append_log("我先搜索一下", "ai")
            it.append_log(
                "调用工具: web_search", "tool",
                event="tool_start", tool_id="call-1", tool_name="web_search",
                tool_args={"query": "关键词"},
            )
            it.status = "done"
            it.result = "ok"
        return items

    m = SubagentManager()
    m._config = object()
    monkeypatch.setattr(subagents_mod, "manager", m)
    monkeypatch.setattr(subagents_mod, "_run_all_parallel", fake_run_all)

    result = subagents_mod.delegate_tasks_parallel.func(
        '[{"task": "搜索A", "agent_type": "searcher"}, {"task": "搜索B", "agent_type": "searcher"}]'
    )

    # 工具已返回，但 batch 必须仍然保留（on_tool_end 此刻还没执行）
    assert len(m._current_batch) == 2, "工具返回后 _current_batch 被提前清空，历史回放会丢日志"
    logs = m.get_capsule_logs(1)
    assert any(l["cat"] == "ai" and "先搜索" in l["text"] for l in logs)
    events = m.get_capsule_tool_events(1)
    assert len(events) == 1 and events[0]["event"] == "tool_start"
    assert "✅" in result


def test_clear_batch_after_packaging(monkeypatch):
    """on_tool_end 打包完成后才允许 clear_batch（agent_run 侧时序语义）。"""
    m = _make_manager()
    item = SubagentTask(id="subagent-t3", agent_type="searcher", task="t")
    item.append_log("队列中，等待执行...", "info")
    m.start_batch([item])

    # 模拟 on_tool_end 打包：先读日志
    logs = m.get_capsule_logs(1)
    assert len(logs) == 1 and logs[0]["cat"] == "info"

    # 打包完 → 清理 batch（这就是 agent_run 里的新时序）
    m.clear_batch()
    assert m.get_capsule_logs(1) == []   # 清理后读取为空（正常语义）
    assert m._current_batch == []
