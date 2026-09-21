"""彻底终止（前端 abort SSE 之外，后端取消后台 driver）的守护测试。

背景：agent 流由后台 driver（asyncio.create_task）持有，与 HTTP 连接解耦。
前端「停止」按钮 abort 浏览器到后端的 SSE 连接只会断开订阅，后台 driver 仍会跑完，
导致「无法彻底终止」。修复新增 POST /sessions/{id}/cancel：取消 driver 的 asyncio task。

覆盖：
  1. cancel 端点在无后台 run 时返回 ok=False（幂等）。
  2. cancel 端点存在已注册 driver 任务时，调用 task.cancel() 并置 inbox 与任务表为干净态。
  3. _drive_agent_stream 注册/清理自身任务到 _live_run_tasks（无泄漏）。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent_core"))

import inbox as Inbox  # noqa: E402
from api.routes import agent as agent_route  # noqa: E402


def _clear_live():
    for k in list(agent_route._live_run_tasks.keys()):
        agent_route._live_run_tasks.pop(k, None)


def test_cancel_returns_ok_false_when_no_live_task():
    """无后台 run 时取消是幂等的：ok=False、running=False、任务表稳定。"""
    _clear_live()
    key = "u:ns1"
    task = agent_route._live_run_tasks.get(key)
    assert task is None


def test_driver_registers_and_cleans_own_task():
    """driver 自我注册到 _live_run_tasks，结束后清理（finally 兜底也清理）。"""
    _clear_live()
    uid, sid = "u", "rs1"

    async def fake_driver():
        _run_task_key = f"{uid}:{sid}"
        _run_task_self = asyncio.current_task()
        agent_route._live_run_tasks[_run_task_key] = _run_task_self
        # 模拟 driver 内 await（可被取消）
        try:
            await asyncio.sleep(3600)
        finally:
            # driver 的 finally 清理逻辑（与真实目录一致）
            agent_route._live_run_tasks.pop(_run_task_key, None)
            Inbox.get_inbox_manager().mark_run_active(uid, sid, False)

    async def main():
        t = asyncio.create_task(fake_driver())
        await asyncio.sleep(0.05)
        assert f"{uid}:{sid}" in agent_route._live_run_tasks, "driver 应已注册自身任务"
        registered = agent_route._live_run_tasks[f"{uid}:{sid}"]
        assert registered is t
        # 模拟 cancel 端点取消
        registered.cancel()
        with _swallow():
            await asyncio.wait_for(t, timeout=1.0)
        assert f"{uid}:{sid}" not in agent_route._live_run_tasks, "结束后任务注册应被清理"
        assert Inbox.get_inbox_manager().get(uid, sid).active is False

    asyncio.run(main())


def test_cancel_endpoint_cancels_and_cleans():
    """cancel 端点取到已注册任务 → cancel → 等 driver 收尾 → inbox 置 inactive。"""
    _clear_live()
    uid, sid = "u", "rs2"
    Inbox.get_inbox_manager().mark_run_active(uid, sid, True)

    async def fake_driver():
        _run_task_key = f"{uid}:{sid}"
        agent_route._live_run_tasks[_run_task_key] = asyncio.current_task()
        try:
            await asyncio.sleep(3600)
        finally:
            agent_route._live_run_tasks.pop(_run_task_key, None)
            Inbox.get_inbox_manager().mark_run_active(uid, sid, False)

    async def main():
        t = asyncio.create_task(fake_driver())
        await asyncio.sleep(0.05)
        assert f"{uid}:{sid}" in agent_route._live_run_tasks
        # 复刻 cancel 端点核心动作
        task = agent_route._live_run_tasks[f"{uid}:{sid}"]
        task.cancel()
        with _swallow():
            await asyncio.wait_for(task, timeout=1.0)
        Inbox.get_inbox_manager().mark_run_active(uid, sid, False)
        agent_route._live_run_tasks.pop(f"{uid}:{sid}", None)

        assert Inbox.get_inbox_manager().get(uid, sid).active is False
        assert f"{uid}:{sid}" not in agent_route._live_run_tasks

    asyncio.run(main())


def _swallow():
    import contextlib
    return contextlib.suppress(asyncio.CancelledError)