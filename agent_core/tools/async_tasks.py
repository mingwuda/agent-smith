"""进程级异步任务调度器（不依赖 agent / ReAct 图 / LangGraph）。

用途：让 run_shell 等可能耗时的工具"启动即返回"，agent 拿到 task_id 后用
get / wait / cancel / list 工具跟进，避免 agent 主循环被单一工具阻塞。

设计原则：
- ponytail: 进程内存 dict，agent 死任务也死，重启即清空，零持久化成本。
- ponytail: 任务与 thread_id 解耦——agent 切换/重启后旧任务保留一段时间（10 min）
  供历史查询，超时自动清理。
- 复用 shell_tools 的 _SHELL_OUTPUT_QUEUE 心跳机制：实时输出仍在前端可见。
"""
from __future__ import annotations

import secrets
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Optional


# 任务在内存中保留的时长（秒）。任务完成后超过此时间会被清理（list 时不再可见）。
TASK_TTL_AFTER_DONE = 600  # 10 min


@dataclass
class AsyncTask:
    """一个后台任务的运行时快照。"""
    task_id: str
    command: str              # 原始命令
    proc: Optional[subprocess.Popen] = None  # 子进程（done/failed 后保留为 None）
    started_at: float = 0.0
    finished_at: float = 0.0
    status: str = "running"   # running | done | failed | timeout | cancelled
    returncode: int = -1
    output: str = ""          # 累计输出（实时追加，done 时定格）
    error: str = ""           # 错误信息
    # ponytail: 不绑定 thread_id——agent 切换/重启后查询仍可见（直到 TTL 过期）

    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.time()
        return end - self.started_at

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "command": self.command[:200],
            "status": self.status,
            "elapsed": round(self.elapsed, 1),
            "returncode": self.returncode,
            "output_chars": len(self.output),
            "output_preview": self.output[-500:] if self.output else "",
            "error": self.error[:200] if self.error else "",
        }


# 全局任务表：task_id -> AsyncTask。进程内有效，不持久化。
_TASKS: dict[str, AsyncTask] = {}
_TASKS_LOCK = threading.Lock()


def new_task_id() -> str:
    """生成 12 位 task_id（与 LangGraph run_id 同长度）。"""
    return secrets.token_hex(6)


def register_task(command: str) -> AsyncTask:
    """新建任务并加入任务表，返回任务对象。调用方负责在 Popen 启动后 set proc。"""
    tid = new_task_id()
    task = AsyncTask(
        task_id=tid,
        command=command,
        started_at=time.time(),
        # ponytail: status 立即设为 pending，Popen 启动后再置 running。
        # 避免 cancel_task 在 Popen 启动前被调用时认为"任务未启动"。
        status="pending",
    )
    with _TASKS_LOCK:
        _cleanup_locked()
        _TASKS[tid] = task
    return task


def get_task(task_id: str) -> Optional[AsyncTask]:
    """查任务（含已完成）。不存在或已过期 → None。"""
    with _TASKS_LOCK:
        _cleanup_locked()
        return _TASKS.get(task_id)


def list_tasks(include_done: bool = True) -> list[AsyncTask]:
    """列所有任务。include_done=False 时只列未结束（pending + running）。"""
    with _TASKS_LOCK:
        _cleanup_locked()
        if include_done:
            return list(_TASKS.values())
        # ponytail: 未结束 = 还在进程表里活跃的任务（pending 等 Popen + running 已起）。
        # 终态（done/failed/cancelled/timeout）不计入"在飞"。
        return [t for t in _TASKS.values() if t.status in ("pending", "running")]


def cancel_task(task_id: str) -> tuple[bool, str]:
    """终止任务。返回 (是否成功, 状态描述)。

    ponytail: 支持 pending 状态——若 Popen 还没启动（极短窗口），标记为 cancelled，
    后台线程检测到 cancelled 后不再 Popen 直接退出。
    """
    with _TASKS_LOCK:
        task = _TASKS.get(task_id)
        if not task:
            return False, "任务不存在"
        if task.status not in ("running", "pending"):
            return False, f"任务已结束（{task.status}），无需取消"
        proc = task.proc
        if proc is None and task.status == "running":
            return False, "任务未启动进程"
        # 标记为 cancelled（pending 也立即标，防后台线程 Popen 后仍跑）
        task.status = "cancelled"
        task.finished_at = time.time()
        task.error = "用户取消"
    # ponytail: 不在锁内调 Popen.kill——子进程终止是阻塞调用，会拖住其他任务查询。
    # 锁外执行 kill 完再回到锁内更新状态。
    if proc is not None:
        try:
            proc.kill()
        except Exception as e:
            return True, f"已标记取消（终止进程失败: {e}）"
    return True, "已终止"


def _cleanup_locked() -> None:
    """清理已完成且超过 TTL 的任务。必须在 _TASKS_LOCK 持有期间调用。

    ponytail: 只清理"已结束且 finished_at > 0"的任务，避免误删刚刚注册、finished_at 仍为 0
    的新任务（pending 状态）——(now - 0) > TTL 必为真，会让所有新任务一注册就被清。
    """
    now = time.time()
    stale = [
        tid for tid, t in _TASKS.items()
        if t.status not in ("running", "pending")
        and t.finished_at > 0
        and (now - t.finished_at) > TASK_TTL_AFTER_DONE
    ]
    for tid in stale:
        _TASKS.pop(tid, None)


def task_count() -> int:
    """当前任务数（用于测试/debug）。"""
    with _TASKS_LOCK:
        return len(_TASKS)
