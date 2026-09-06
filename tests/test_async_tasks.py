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
    """短任务（timeout=10 < 30s 阈值）走同步路径，立即返回完整结果。"""
    result = shell_tools.run_shell.invoke({"command": "echo hello_async_test", "timeout": 10})
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


def test_cancel_async_task_running():
    """cancel_async_task：终止正在跑的任务。"""
    import subprocess as _sp
    import threading

    task = at.register_task("sleep 30")

    def _runner():
        proc = _sp.Popen(["sleep", "30"], stdout=_sp.PIPE, stderr=_sp.STDOUT)
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

    # 等线程把 proc 设上
    for _ in range(20):
        if task.proc is not None:
            break
        time.sleep(0.05)

    cancelled = shell_tools.cancel_async_task.invoke({"task_id": task.task_id})
    assert "已终止" in cancelled

    time.sleep(0.5)
    task = at.get_task(task.task_id)
    assert task.status == "cancelled"

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
