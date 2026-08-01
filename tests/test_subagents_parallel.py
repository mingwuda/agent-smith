"""子代理并行改造测试：预构建 graph 缓存 / 单事件循环 gather 真正并发 / 批量截断。

只覆盖不依赖真实 LLM/LangGraph 执行的调度与状态机逻辑（ponytail）。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_core"))

import subagents as subagents_mod  # noqa: E402
from subagents import SubagentManager, SubagentTask, _run_all_parallel, delegate_tasks_parallel  # noqa: E402


def test_configure_prebuilds_graph_cache():
    """configure 后每个 agent_type 都有预构建 graph（LLM/工具集一次成型，运行期复用）。"""
    from config import AgentConfig
    from langchain_core.tools import tool

    @tool
    def _noop(x: str = "") -> str:
        """空操作工具，仅供建图测试。"""
        return x

    m = SubagentManager()
    m.configure(AgentConfig(), [_noop])
    assert set(m._graph_cache.keys()) == set(subagents_mod.SUBAGENT_PROMPTS.keys())
    assert all(g is not None for g in m._graph_cache.values())


def test_run_all_parallel_true_concurrency(monkeypatch):
    """单事件循环 gather：多个任务同时处于执行态（I/O 真正并行，而非排队串行）。"""
    peak: list[int] = []
    max_seen: list[int] = [0]

    async def _fake_run_sync(self, task, agent_type="coder", context="", wall_timeout=180.0, idle_timeout=60.0, item=None):
        if item is None:
            item = SubagentTask(id="x", agent_type=agent_type, task=task, context=context)
        peak.append(1)
        max_seen[0] = max(max_seen[0], len(peak))
        await asyncio.sleep(0.05)  # 让出事件循环：若串行执行，这里会逐个完成
        item.status = "done"
        item.result = f"ok:{item.task}"
        peak.pop()
        return item

    monkeypatch.setattr(SubagentManager, "run_sync", _fake_run_sync)
    items = [SubagentTask(id=f"t{i}", agent_type="coder", task=f"task{i}") for i in range(4)]
    ret = asyncio.run(_run_all_parallel(items))
    assert len(ret) == 4
    assert all(i.status == "done" for i in ret)
    assert max_seen[0] >= 4  # 峰值并发 ≥ 4：4 个任务确实同时运行


def test_delegate_tasks_parallel_caps_at_four(monkeypatch):
    """批量入口最多并行 4 个：6 个任务只创建 4 个 item（与工具描述一致）。"""
    created: list[str] = []

    async def _fake_run_sync(self, task, agent_type="coder", context="", wall_timeout=180.0, idle_timeout=60.0, item=None):
        created.append(item.id)
        item.status = "done"
        item.result = "ok"
        return item

    monkeypatch.setattr(SubagentManager, "run_sync", _fake_run_sync)

    def _fake_run_in_thread(coro, timeout=60.0):
        return asyncio.run(coro)

    monkeypatch.setattr(subagents_mod, "_run_coro_in_thread", _fake_run_in_thread)

    tasks = [{"task": f"t{i}"} for i in range(6)]
    # delegate_tasks_parallel 是 @tool 包装的 StructuredTool，.func 才是原始函数
    out = delegate_tasks_parallel.func(json.dumps(tasks))
    assert "4/6" in out  # 成功 4 / 总数 6（截断到 4）
    assert len(created) == 4


def test_delegate_tasks_parallel_bad_input():
    """非法 JSON / 空列表直接报错，不进入调度。"""
    assert "解析失败" in delegate_tasks_parallel.func("not json")
    assert "至少一个任务" in delegate_tasks_parallel.func("[]")
