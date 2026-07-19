"""回归测试：工具层 workspace / current_user 必须按 async 请求隔离（ContextVar）。

验证两件事：
1. 在 /run/stream 的 async 任务里 set_workspace 后，真实 @tool.ainvoke 路径
   （LangChain 内部 asyncio 线程池 + copy_context）能读到该值，而不是退化到默认。
2. 两个并发请求各自 set 不同 workspace，工具执行互不串目录 / 不串用户。

用 asyncio.run 包裹，避免引入 pytest-asyncio 依赖。
"""
import asyncio
from pathlib import Path


def _ws_of(uid: str) -> Path:
    return Path.home() / ".desktop_agent" / "sessions" / uid / "ws"


async def _run_request(uid: str, marker: str):
    from agent_core.tools import file_tools, shell_tools

    ws = _ws_of(uid) / marker
    file_tools.set_workspace(ws)
    shell_tools.set_workspace(ws)
    file_tools.set_current_user(uid)
    shell_tools.set_current_user(uid)

    got_ws = await file_tools.get_workspace_path.ainvoke({})
    got_user = file_tools._current_user_ctx.get()
    shell_user = shell_tools._current_user_ctx.get()
    return str(got_ws), got_user, shell_user


def test_workspace_propagates_into_tool_thread():
    ws = asyncio.run(_run_request("alice", "REQ_A"))
    assert ws[0] == str(_ws_of("alice") / "REQ_A"), f"工作区未传播: {ws[0]}"


def test_concurrent_requests_isolated():
    """用两个独立 asyncio.Task 模拟两个并发请求（每个 Task 有独立 context 拷贝）。"""
    async def _main():
        t_alice = asyncio.create_task(_run_request("alice", "REQ_A"))
        t_bob = asyncio.create_task(_run_request("bob", "REQ_B"))
        return await t_alice, await t_bob

    r_alice, r_bob = asyncio.run(_main())
    assert r_alice[0] == str(_ws_of("alice") / "REQ_A"), r_alice
    assert r_alice[1] == "alice" and r_alice[2] == "alice", r_alice
    assert r_bob[0] == str(_ws_of("bob") / "REQ_B"), r_bob
    assert r_bob[1] == "bob" and r_bob[2] == "bob", r_bob
    assert r_alice != r_bob
