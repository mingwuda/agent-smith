"""P0-1 工具并行执行测试。

改造前：工具是同步 def，LangGraph 只能丢线程池串行执行，两个 30s 的 run_shell
要等 60s。改造后：run_shell / run_python 是 async def，在同一个事件循环里并发。

验证方式用「计时」——这是唯一不会自欺的证据：串行 4s、并行 2s。
不测具体数值精确到毫秒（CI 机器抖动），只断言「明显短于串行」。
"""
import asyncio
import sys
import time
import warnings
from pathlib import Path

import pytest

from agent_core.main import app  # noqa: F401  (sys.path 注入)

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.prebuilt import create_react_agent

from agent_core.tools.code_tools import run_python
from agent_core.tools.file_tools import set_workspace
from agent_core.tools.shell_tools import run_shell

warnings.filterwarnings("ignore", module="langgraph")

SLEEP = 2


class _Scripted(FakeMessagesListChatModel):
    """按脚本返回固定消息；bind_tools 只记录工具，不改变行为。"""

    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture()
def workspace(tmp_path):
    set_workspace(tmp_path)
    return tmp_path


def test_shell_tools_run_concurrently(workspace):
    """两个 run_shell 并发：总耗时接近单个任务，而非两倍。"""
    async def _go():
        t0 = time.time()
        results = await asyncio.gather(
            run_shell.ainvoke({"command": f"sleep {SLEEP} && echo A"}),
            run_shell.ainvoke({"command": f"sleep {SLEEP} && echo B"}),
        )
        return time.time() - t0, results

    elapsed, results = asyncio.run(_go())
    assert "A" in results[0] and "B" in results[1]
    assert elapsed < SLEEP * 1.6, f"未并行：{elapsed:.1f}s（串行应约 {SLEEP*2}s）"


def test_shell_and_python_run_concurrently(workspace):
    """跨工具类型也能并行：shell 与 python 同时跑。"""
    async def _go():
        t0 = time.time()
        out = await asyncio.gather(
            run_shell.ainvoke({"command": f"sleep {SLEEP}"}),
            run_python.ainvoke({"code": f"import time; time.sleep({SLEEP}); print('py')"}),
        )
        return time.time() - t0, out

    elapsed, out = asyncio.run(_go())
    assert "py" in out[1]
    assert elapsed < SLEEP * 1.6, f"未并行：{elapsed:.1f}s"


def test_graph_executes_tool_calls_in_parallel(workspace):
    """端到端：模型一次返回两个 tool_calls 时，图中两者同时启动。

    这是最接近真实链路的验证：不是直接调工具，而是走 LangGraph 的事件流。
    """
    llm = _Scripted(responses=[
        AIMessage(content="", tool_calls=[
            {"name": "run_shell", "args": {"command": f"sleep {SLEEP} && echo A"}, "id": "c1"},
            {"name": "run_shell", "args": {"command": f"sleep {SLEEP} && echo B"}, "id": "c2"},
        ]),
        AIMessage(content="done"),
    ])
    graph = create_react_agent(llm, [run_shell])

    async def _go():
        starts, t0 = [], time.time()
        async for ev in graph.astream_events(
            {"messages": [("user", "go")]},
            {"configurable": {"thread_id": "p01-test"}},
            version="v2",
        ):
            if ev["event"] == "on_tool_start":
                starts.append(time.time() - t0)
        return starts, time.time() - t0

    starts, total = asyncio.run(_go())
    assert len(starts) == 2, f"应有 2 次工具启动，实际 {starts}"
    # 两个工具几乎同时启动
    assert abs(starts[0] - starts[1]) < 0.5, f"未同时启动: {starts}"
    # 总耗时接近单个，而非两倍
    assert total < SLEEP * 1.6, f"未并行：总耗时 {total:.1f}s"


def test_shell_output_queues_are_isolated(workspace):
    """实时输出队列按调用隔离：并行时一个不会清掉另一个的输出。"""
    from agent_core.tools import shell_tools

    async def _go():
        return await asyncio.gather(
            run_shell.ainvoke({"command": "echo FIRST"}),
            run_shell.ainvoke({"command": "echo SECOND"}),
        )

    results = asyncio.run(_go())
    # 每个调用的返回值必须含自己那条输出，不能串台
    assert "FIRST" in results[0] and "SECOND" not in results[0], results[0]
    assert "SECOND" in results[1] and "FIRST" not in results[1], results[1]
    # 调用结束后保留最后一块供 on_tool_end 兜底 drain（实时预览尾部不丢），
    # 但每个调用的队列必须独立——key 各不相同，不会互相覆盖。
    leftover = shell_tools.drain_all_shell_output()
    keys = [k for k, text in leftover if text]
    assert len(keys) == len(set(keys)), f"key 应唯一: {keys}"
    joined = "".join(text for _k, text in leftover)
    assert "FIRST" in joined and "SECOND" in joined
