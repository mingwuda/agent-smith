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
    """长任务（timeout=60 >= 30s 阈值）启动后台任务，立即返回 task_id。"""
    # 启动一个会跑 2 秒的命令，但 timeout=60 触发异步路径
    result = shell_tools.run_shell.invoke({"command": "sleep 0.5 && echo done", "timeout": 60})
    # 返回里应包含 task_id
    assert "task_id:" in result
    assert "长任务已在后台启动" in result
    # 提取 task_id
    import re
    m = re.search(r"task_id:\s*([a-f0-9]+)", result)
    assert m
    task_id = m.group(1)

    # 验证任务表里有这个任务
    task = at.get_task(task_id)
    assert task is not None
    assert task.command == "sleep 0.5 && echo done"

    # 等它完成
    time.sleep(2)
    task = at.get_task(task_id)
    assert task.status == "done"
    assert "done" in task.output

    # 清理
    with at._TASKS_LOCK:
        at._TASKS.clear()


# ── 4 个跟进工具测试 ──


def test_get_async_task_tool():
    """get_async_task 工具：查状态，返回 JSON。"""
    task = at.register_task("echo test")
    result = shell_tools.get_async_task.invoke({"task_id": task.task_id})
    # ponytail: 状态可能是 pending（Popen 未起）或 running（已起），都视为正常
    assert task.task_id in result
    assert ('"status": "pending"' in result or '"status": "running"' in result)
    # 清理
    with at._TASKS_LOCK:
        at._TASKS.clear()


def test_get_async_task_not_found():
    """get_async_task 查不存在的 task_id → 返回错误。"""
    result = shell_tools.get_async_task.invoke({"task_id": "notexist"})
    assert "不存在" in result


def test_wait_async_task_completes_early():
    """wait_async_task：任务在 timeout 内完成 → 立即返回结果。"""
    # 跑 1 秒的命令
    result = shell_tools.run_shell.invoke({"command": "sleep 0.5 && echo waited", "timeout": 60})
    import re
    m = re.search(r"task_id:\s*([a-f0-9]+)", result)
    task_id = m.group(1)

    # wait 5 秒应该能等到（命令实际 0.5s 就完成）
    start = time.time()
    waited = shell_tools.wait_async_task.invoke({"task_id": task_id, "timeout": 5})
    elapsed = time.time() - start
    assert elapsed < 4  # 轮询粒度 1s，<4s 内应返回
    assert '"status": "done"' in waited
    assert "waited" in waited or "elapsed" in waited  # 包含输出或元信息

    # 清理
    with at._TASKS_LOCK:
        at._TASKS.clear()


def test_wait_async_task_timeout_keeps_running():
    """wait_async_task：任务仍在跑 → 返回当前状态摘要（不抛错）。"""
    # 跑 5 秒的命令
    result = shell_tools.run_shell.invoke({"command": "sleep 3", "timeout": 60})
    import re
    m = re.search(r"task_id:\s*([a-f0-9]+)", result)
    task_id = m.group(1)

    # wait 1 秒应超时，但任务仍 running
    waited = shell_tools.wait_async_task.invoke({"task_id": task_id, "timeout": 1})
    assert "仍在运行" in waited
    assert task_id in waited

    # 清理
    with at._TASKS_LOCK:
        at._TASKS.clear()


def test_cancel_async_task_running():
    """cancel_async_task：终止正在跑的任务。"""
    # 跑 30 秒的命令
    result = shell_tools.run_shell.invoke({"command": "sleep 30", "timeout": 60})
    import re
    m = re.search(r"task_id:\s*([a-f0-9]+)", result)
    task_id = m.group(1)

    # ponytail: 等 200ms 让后台线程把 status 从 pending 升为 running（避免 race）。
    # 不等的话 cancel 可能命中 pending 路径，验证的还是同一功能（pending cancel）
    # 但 running 路径才是 Popen.kill 主路径，单独覆盖。
    time.sleep(0.2)
    task_now = at.get_task(task_id)
    assert task_now.status == "running", f"expected running, got {task_now.status}"

    # 取消
    cancelled = shell_tools.cancel_async_task.invoke({"task_id": task_id})
    assert "已终止" in cancelled

    # 任务状态应为 cancelled
    time.sleep(0.5)
    task = at.get_task(task_id)
    assert task.status == "cancelled"

    # 清理
    with at._TASKS_LOCK:
        at._TASKS.clear()


def test_cancel_async_task_pending():
    """cancel_async_task 在 Popen 启动前的极短窗口内被调用——也要能正确标记为 cancelled。"""
    # ponytail: 用 list_async_tasks 的工具间接验证：注册后立刻 cancel，
    # 不等 Popen 起来，验证 cancel 在 pending 状态也能成功。
    task = at.register_task("sleep 30")
    ok, msg = at.cancel_task(task.task_id)
    assert ok is True
    assert "已终止" in msg
    assert task.status == "cancelled"


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
