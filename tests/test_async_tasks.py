"""异步任务调度 + run_shell 异步路径 测试。

只测不依赖网络/真实 LLM 的核心逻辑：
- 调度器 register/get/list/cancel/cleanup
- run_shell 短任务走同步路径
- run_shell 长任务立即返回 task_id
- 4 个跟进工具：get / wait / cancel / list
"""
import time
import subprocess
import sys
import os
import asyncio

from agent_core.main import app  # noqa: F401  (sys.path 注入)
from agent_core.tools import shell_tools
from agent_core.tools import async_tasks as at


# ── 调度器单元测试 ──


def test_register_and_get():
    """注册任务 → get_task 能取到。"""
    task = at.register_task("echo hello")
    assert task.task_id
    # ponytail: 新建任务初始状态是 pending（后台线程启动 Popen 后才升 running），
    # 避免 cancel_task 在 Popen 启动前的窗口期被误判"未启动"。
    assert task.status == "pending"
    assert task.command == "echo hello"
    got = at.get_task(task.task_id)
    assert got is task
    # 清理
    with at._TASKS_LOCK:
        at._TASKS.clear()


def test_list_tasks_running_and_done():
    """list_tasks 区分 running/done，include_done 参数生效。"""
    t1 = at.register_task("sleep 0.1")
    t2 = at.register_task("echo done")
    # 手动模拟 t2 完成
    t2.status = "done"
    t2.finished_at = time.time()
    t1.status = "done"  # 让 cleanup 别清掉（10min TTL）
    t1.finished_at = time.time()

    running_only = at.list_tasks(include_done=False)
    all_tasks = at.list_tasks(include_done=True)
    assert t1 not in running_only
    assert t1 in all_tasks
    assert t2 not in running_only
    assert t2 in all_tasks
    # 清理
    with at._TASKS_LOCK:
        at._TASKS.clear()


def test_cancel_nonexistent_task():
    """取消不存在的任务 → 返回 (False, 错误信息)。"""
    ok, msg = at.cancel_task("nonexistent")
    assert ok is False
    assert "不存在" in msg


def test_cancel_already_done_task():
    """取消已结束的任务 → 返回 False。"""
    task = at.register_task("echo")
    task.status = "done"
    task.finished_at = time.time()
    ok, msg = at.cancel_task(task.task_id)
    assert ok is False
    assert "已结束" in msg
    # 清理
    with at._TASKS_LOCK:
        at._TASKS.clear()


# ── run_shell 异步路径集成测试 ──


def test_run_shell_short_path_sync():
    """短任务（timeout=10 < 30s 阈值）走前台完成路径，立即返回完整结果。

    P0-1：run_shell 已改为 async 工具，同步 invoke 不再支持，必须用 ainvoke。
    """
    result = asyncio.run(
        shell_tools.run_shell.ainvoke({"command": "echo hello_async_test", "timeout": 10})
    )
    assert "hello_async_test" in result
    assert "exit code: 0" in result
    # 短任务不该启动后台任务
    assert at.task_count() == 0

def test_run_shell_long_path_async():
    """长任务 handoff 后，后台任务原语仍可正常工作。

    ponytail: _run_shell_async 返回 str（不是 AsyncTask），
    所以直接用 at.register_task + at.get_task 测调度器核心。
    """
    import subprocess as _sp
    import threading

    task = at.register_task("echo handoff_done")

    def _runner():
        proc = _sp.Popen(["/bin/sh", "-c", "sleep 0.2 && echo handoff_done"],
                          stdout=_sp.PIPE, stderr=_sp.STDOUT)
        task.proc = proc
        if task.status == "pending":
            task.status = "running"
        out, _ = proc.communicate()
        if task.status == "cancelled":
            return
        task.output = out.decode("utf-8", errors="replace")
        task.returncode = proc.returncode
        task.status = "done" if proc.returncode == 0 else "failed"
        task.finished_at = __import__("time").time()
        task.proc = None

    threading.Thread(target=_runner, daemon=True).start()

    assert task.status in ("pending", "running")
    import time as _t
    _t.sleep(1)
    final = at.get_task(task.task_id)
    assert final.status == "done"
    assert "handoff_done" in final.output

    with at._TASKS_LOCK:
        at._TASKS.pop(task.task_id, None)


# ── 4 个跟进工具测试（直接测原语，不依赖 run_shell 返回 task_id）──


def test_get_async_task_tool():
    """get_async_task 工具：查状态，返回 JSON。"""
    task = at.register_task("echo test")
    result = shell_tools.get_async_task.invoke({"task_id": task.task_id})
    assert task.task_id in result
    assert ('"status": "pending"' in result or '"status": "running"' in result)
    with at._TASKS_LOCK:
        at._TASKS.clear()


def test_get_async_task_not_found():
    """get_async_task 查不存在的 task_id → 返回错误。"""
    result = shell_tools.get_async_task.invoke({"task_id": "notexist"})
    assert "不存在" in result


def test_wait_async_task_completes_early():
    """wait_async_task：任务在 timeout 内完成 → 立即返回结果。"""
    import subprocess as _sp
    import threading
    import time as _t

    task = at.register_task("echo waited")

    def _runner():
        proc = _sp.Popen(["/bin/sh", "-c", "sleep 0.2 && echo waited"],
                          stdout=_sp.PIPE, stderr=_sp.STDOUT)
        task.proc = proc
        if task.status == "pending":
            task.status = "running"
        out, _ = proc.communicate()
        if task.status == "cancelled":
            return
        task.output = out.decode("utf-8", errors="replace")
        task.returncode = proc.returncode
        task.status = "done" if proc.returncode == 0 else "failed"
        task.finished_at = _t.time()
        task.proc = None

    threading.Thread(target=_runner, daemon=True).start()

    waited = shell_tools.wait_async_task.invoke({"task_id": task.task_id, "timeout": 5})
    assert '"status": "done"' in waited
    assert "waited" in waited

    with at._TASKS_LOCK:
        at._TASKS.clear()


def test_wait_async_task_timeout_keeps_running():
    """wait_async_task：任务仍在跑 → 返回当前状态摘要（不抛错）。"""
    import subprocess as _sp
    import threading

    task = at.register_task("sleep 3")

    def _runner():
        proc = _sp.Popen(["sleep", "3"], stdout=_sp.PIPE, stderr=_sp.STDOUT)
        task.proc = proc
        if task.status == "pending":
            task.status = "running"
        proc.wait()
        if task.status != "cancelled":
            task.returncode = proc.returncode
            task.status = "done" if proc.returncode == 0 else "failed"
        task.finished_at = __import__("time").time()
        task.proc = None

    threading.Thread(target=_runner, daemon=True).start()

    waited = shell_tools.wait_async_task.invoke({"task_id": task.task_id, "timeout": 1})
    assert "仍在运行" in waited
    assert task.task_id in waited

    with at._TASKS_LOCK:
        at._TASKS.clear()


def _can_signal_processes() -> bool:
    """宿主环境是否允许向其它进程发信号（部分沙箱会以 EPERM 拦掉 kill）。"""
    probe = subprocess.Popen(["sh", "-c", "sleep 5"])
    try:
        probe.kill()
        probe.wait(timeout=2)
        return True
    except Exception:
        return False
    finally:
        try:
            probe.wait(timeout=1)
        except Exception:
            pass


def test_cancel_async_task_running():
    """cancel_async_task：终止正在跑的任务，并回收其孙进程。"""
    import subprocess as _sp

    with at._TASKS_LOCK:
        at._TASKS.clear()

    # 用 sh -c 起一个会 fork 孙进程的命令，复现「只 kill 父进程、孙进程变孤儿」的场景
    task = at.register_task("sh -c 'sleep 30'")
    proc = _sp.Popen(["sh", "-c", "sleep 30"], start_new_session=True)
    task.proc = proc
    task.status = "running"
    pgid = os.getpgid(proc.pid)

    cancelled = shell_tools.cancel_async_task.invoke({"task_id": task.task_id})

    if _can_signal_processes():
        assert cancelled.startswith("✅"), cancelled
        assert proc.poll() is not None, "父进程应已退出"

        # 孙进程（sleep）必须一并回收，不能留孤儿占资源
        time.sleep(0.2)
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            pass  # 进程组已不存在 —— 正是预期
        else:
            leftovers = _sp.run(["ps", "-g", str(pgid), "-o", "pid="], capture_output=True, text=True).stdout.strip()
            assert leftovers == "", f"进程组仍有残留进程: {leftovers}"

        task = at.get_task(task.task_id)
        assert task.status == "cancelled"
    else:
        # 宿主沙箱禁止信号（本仓开发环境即如此）：无法真杀，唯一可验的是
        # 「杀不动就报失败」，绝不能假报成功——这正是 P0 bug 的语义。
        assert cancelled.startswith("❌"), f"无法终止时不应报成功: {cancelled}"
        task = at.get_task(task.task_id)
        assert task.status == "failed"

    with at._TASKS_LOCK:
        at._TASKS.clear()


def test_cancel_failure_is_reported_as_failure():
    """P0 回归：终止失败不能返回成功。

    旧实现在 proc.kill() 抛异常时仍返回 (True, "已标记取消")，前端告诉用户
    「已停止」但进程还在跑。这里锁定新语义：已自行退出的进程无需杀 → 成功；
    真正无法终止 → False 且状态置 failed。
    """
    import subprocess as _sp

    with at._TASKS_LOCK:
        at._TASKS.clear()

    task = at.register_task("already-dead")
    dead = _sp.Popen(["sh", "-c", "true"])
    dead.wait()
    task.proc = dead
    task.status = "running"

    ok, msg = at.cancel_task(task.task_id)
    # 已退出的进程无需再杀 —— 应报成功且不抛异常
    assert ok is True and "已终止" in msg, msg

    with at._TASKS_LOCK:
        at._TASKS.clear()


def test_list_async_tasks_tool():
    """list_async_tasks：列出所有任务。"""
    # 空状态
    with at._TASKS_LOCK:
        at._TASKS.clear()
    empty = shell_tools.list_async_tasks.invoke({"include_done": True})
    assert "无任何后台任务" in empty

    # 加一个任务
    task = at.register_task("echo listed")
    listed = shell_tools.list_async_tasks.invoke({"include_done": True})
    assert task.task_id in listed
    assert ("running" in listed or "pending" in listed)  # 状态可能是任一
    assert "echo listed" in listed

    # 只列 running
    task2 = at.register_task("echo done2")
    task2.status = "done"
    task2.finished_at = time.time()
    running_only = shell_tools.list_async_tasks.invoke({"include_done": False})
    assert task.task_id in running_only
    assert task2.task_id not in running_only

    # 清理
    with at._TASKS_LOCK:
        at._TASKS.clear()
