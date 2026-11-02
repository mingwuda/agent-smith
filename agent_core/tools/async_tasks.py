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

import os
import secrets
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Optional


# 任务在内存中保留的时长（秒）。任务完成后超过此时间会被清理（list 时不再可见）。
TASK_TTL_AFTER_DONE = 600  # 10 min

# 终止进程时 SIGTERM → SIGKILL 的等待窗口（秒）。
_KILL_GRACE_SECONDS = 5.0


@dataclass
class AsyncTask:
    """一个后台任务的运行时快照。"""
    task_id: str
    command: str              # 原始命令
    description: str = ""     # 可选：人类可读意图摘要（供前端步骤卡片展示）
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
            "description": self.description[:200] if self.description else "",
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


def register_task(command: str, description: str = "") -> AsyncTask:
    """新建任务并加入任务表，返回任务对象。调用方负责在 Popen 启动后 set proc。"""
    tid = new_task_id()
    task = AsyncTask(
        task_id=tid,
        command=command,
        description=description,
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


def _kill_process_tree(proc: subprocess.Popen) -> Optional[str]:
    """终止 proc 及其整个进程组。成功返回 None，失败返回错误描述。

    ponytail: run_shell 的命令都经 `sh -c` 启动，直接 proc.kill() 只杀掉 shell 本身，
    命令派生的孙进程（如 `sh -c 'sleep 30 &'` 的 sleep）会被 init 收养变孤儿继续跑
    ——这正是「点停止后后台命令还在耗资源」的根因。故按进程组整组发信号。
    上限：start_new_session 未开启的子进程不在独立进程组，killpg 会打不动 ——
    此时回退到单 proc.kill()，最多少杀孙进程，不会误杀调用方进程组。
    """
    if proc is None:
        return None
    if proc.poll() is not None:
        return None  # 已自行退出，无需再杀
    try:
        # 子进程若非独立进程组，killpg 会连带杀掉本进程所在组，故先确认再发
        if hasattr(os, "getpgid"):
            pgid = os.getpgid(proc.pid)
            if pgid != os.getpgid(0):
                os.killpg(pgid, signal.SIGTERM)
                try:
                    proc.wait(timeout=_KILL_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    os.killpg(pgid, signal.SIGKILL)
                    proc.wait(timeout=_KILL_GRACE_SECONDS)
                return None
    except (ProcessLookupError, PermissionError, OSError):
        pass  # 进程组不可用/无权限 → 回退单进程 kill
    try:
        proc.kill()
        proc.wait(timeout=_KILL_GRACE_SECONDS)
        return None
    except subprocess.TimeoutExpired:
        return "进程未响应 SIGKILL"
    except Exception as e:  # noqa: BLE001 — 需把失败原因原样交给调用方展示
        # 信号发不出去时不能直接判失败：进程可能恰好在此刻自行退出。
        # 再 poll 一次，已退出的按成功处理（否则用户会看到「取消失败」但进程实际不在了）。
        try:
            if proc.poll() is not None:
                return None
        except Exception:  # noqa: BLE001
            pass
        return str(e)


def cancel_task(task_id: str) -> tuple[bool, str]:
    """终止任务。返回 (是否成功, 状态描述)。

    ponytail: 支持 pending 状态——若 Popen 还没启动（极短窗口），标记为 cancelled，
    后台线程检测到 cancelled 后不再 Popen 直接退出。

    返回值语义（P0 修复）：只有进程确实被终止（或本就已退出）才返回成功。
    终止失败时返回 False 并把原因交给调用方 —— 旧实现在 kill 失败时仍返回
    True +「已标记取消」，前端会告诉用户「已停止」但进程还在跑，属误导。
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
        # 先置 cancelled（防后台 reader 把终态覆盖回 done/failed），再真正杀进程
        task.status = "cancelled"
        task.finished_at = time.time()
        task.error = "用户取消"
    # ponytail: 不在锁内调 kill/wait——等进程退出是阻塞调用，会拖住其他任务查询。
    err = _kill_process_tree(proc)
    if err:
        with _TASKS_LOCK:
            if task.task_id in _TASKS:
                task.status = "failed"
                task.error = f"用户取消，但终止进程失败: {err}"
        return False, f"终止进程失败: {err}（进程可能仍在运行，请手动检查 PID {getattr(proc, 'pid', '?')}）"
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
