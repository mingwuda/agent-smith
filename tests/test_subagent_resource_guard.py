"""回归测试：子代理资源治理（P2 gather 防御 + P3 日志/TTL 上限）。

P2：_run_all_parallel 用 gather(return_exceptions=True) 兜住 run_sync 漏抛的异常，
    不让单个任务异常取消整批、丢弃其它成功结果。
P3：
  - 单任务日志行数有上限（MAX_LOG_LINES），超过丢弃最旧行，防内存无界增长；
  - _tasks 里的已终态任务超过 TTL 会被惰性清理，防无界增长。
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_core"))

import subagents as subagents_mod
from subagents import (
    SubagentManager, SubagentTask, MAX_LOG_LINES, _run_all_parallel,
)


def _make_manager():
    m = SubagentManager()
    m._config = object()  # 绕过 configure 检查（不真跑 LLM）
    return m


# ---------- P3-1：日志行数上限 ----------

def test_append_log_caps_at_max():
    m = _make_manager()
    item = SubagentTask(id="subagent-cap", agent_type="coder", task="t")
    for i in range(MAX_LOG_LINES + 500):
        item.append_log(f"line-{i}", "info")
    lines, total = item.get_logs_since(0)
    assert total == MAX_LOG_LINES
    assert total <= MAX_LOG_LINES
    # 丢弃的是最旧行，保留最新行：共插入 MAX+500，丢掉最早 500 行
    # → 第一行应是 line-500（line-499 已被丢弃），最后一行是 line-{MAX+499}
    assert lines[0]["text"] == "line-500"
    assert "line-499" not in [l["text"] for l in lines]
    assert f"line-{MAX_LOG_LINES + 499}" in lines[-1]["text"]  # 最新保留


def test_log_cap_keeps_final_state_events():
    """截断后 done/error 最后写入的事件必然保留。"""
    m = _make_manager()
    item = SubagentTask(id="subagent-cap2", agent_type="coder", task="t")
    for i in range(MAX_LOG_LINES + 100):
        item.append_log(f"tool-{i}", "tool")
    item.append_log("✅ coder 完成", "done")
    lines, _ = item.get_logs_since(0)
    assert lines[-1]["cat"] == "done"


# ---------- P3-2：_tasks TTL 清理 ----------

def test_prune_tasks_removes_stale_finished():
    m = _make_manager()
    done_old = SubagentTask(id="old-done", agent_type="coder", task="t",
                            status="done", finished_at=0)  # 很早结束
    run_now = SubagentTask(id="run-now", agent_type="coder", task="t",
                           status="running")
    err_fresh = SubagentTask(id="err-fresh", agent_type="coder", task="t",
                             status="error", finished_at=time.time())
    m._tasks = {"old-done": done_old, "run-now": run_now, "err-fresh": err_fresh}
    m._prune_tasks()
    # 过期终态被清；运行中保留；新鲜终态保留（未超 TTL）
    assert "old-done" not in m._tasks
    assert "run-now" in m._tasks
    assert "err-fresh" in m._tasks


def test_prune_keeps_recent_finished():
    m = _make_manager()
    fresh = SubagentTask(id="fresh-done", agent_type="coder", task="t",
                         status="done", finished_at=time.time())
    m._tasks = {"fresh-done": fresh}
    m._prune_tasks()
    assert "fresh-done" in m._tasks


def test_run_sync_triggers_prune(monkeypatch):
    """run_sync 注册新任务时顺带清理过期终态任务。"""
    mono_calls = []

    async def _fake_run_agent(self, item):
        await asyncio.sleep(0.001)
        return "ok"
    monkeypatch.setattr(SubagentManager, "_run_agent", _fake_run_agent)
    m = _make_manager()
    m._prune_tasks = lambda: mono_calls.append(1)  # 打桩记录是否被调
    asyncio.run(m.run_sync(task="t", agent_type="coder"))
    assert mono_calls, "run_sync 应触发 _prune_tasks"


# ---------- P2：gather 防御 ----------

def test_run_all_parallel_handles_exception_item(monkeypatch):
    """某个 run_sync 漏抛异常：不取消整批，异常项兜成 error，其余保留。"""
    async def _fake_run_sync(self, task, agent_type="coder", context="",
                             wall_timeout=180.0, idle_timeout=60.0, item=None):
        await asyncio.sleep(0.01)
        if item.task == "boom":
            raise RuntimeError("内部未捕获异常")  # 模拟 run_sync 漏吞
        item.status = "done"
        item.result = f"ok:{item.task}"
        return item

    monkeypatch.setattr(SubagentManager, "run_sync", _fake_run_sync)
    items = [
        SubagentTask(id="ok-1", agent_type="coder", task="good"),
        SubagentTask(id="boom", agent_type="coder", task="boom"),
        SubagentTask(id="ok-2", agent_type="coder", task="also-ok"),
    ]
    ret = asyncio.run(_run_all_parallel(items))
    # 三个任务都返回（异常项不再让 gather 整体抛出丢弃其余）
    assert len(ret) == 3
    by_id = {it.id: it for it in ret}
    assert by_id["ok-1"].status == "done"
    assert by_id["ok-2"].status == "done"
    boom = by_id["boom"]
    assert boom.status == "error"
    assert "未捕获异常" in boom.error