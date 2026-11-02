"""子进程资源限制（rlimit）——run_python / run_shell 共用。

为什么需要：run_python 直接跑用户/模型生成的任意代码，run_shell 跑任意命令，
两者此前都没有任何资源上限。一段 `while True: x.append(bytearray(10**8))`
就能把整台机器吃满内存，`os.fork()` 循环能打崩 PID 表。这是文档写「沙箱」
但实现只是黑名单正则的落差。

策略（ponytail）：
- 只做 POSIX rlimit，不做 bwrap/firejail —— 后者要新依赖 + 部署改动。
  当前威胁模型是「模型写错代码把机器跑挂」，不是「恶意租户逃逸」，rlimit 正好治。
- Windows 无 rlimit 语义，直接跳过（代码仍可运行，只是没有资源上限）。
- 所有上限都可用环境变量覆盖，0 表示不限制。

⚠ 注入方式（评审 P1-7 修正）：
不要用 `preexec_fn`。CPython 文档明确警告它在**多线程进程**里不安全：fork 后
子进程只保留调用线程，若它恰好持有 malloc/IO 锁，exec 前就会死锁。本项目有
常驻线程（日志轮转 watcher、微信轮询、守护线程），属真实高风险。

改为两种「exec 之后」的注入方式（默认 ulimit 模式）：
- run_shell：命令前置 `ulimit -f/-t/-n/-u`（由 shell 自己执行）
- run_python：脚本首部插一段 setrlimit 代码（由子进程解释器自己执行）
`MOSS_RLIMIT_MODE=preexec` 可显式回退旧方式（排障用）。

注意：rlimit 只约束「单个进程」。fork 出的孙进程各自重新计数，
故 RLIMIT_NPROC 是按 UID 统计全部进程的——这与直觉不同，见 DEFAULT_NPROC 注释。

平台差异（macOS 实测定，2026-11）：
- Linux：RLIMIT_AS（地址空间）生效，内存上限是真限制。
- macOS：内核对 RLIMIT_AS / RLIMIT_DATA 的 setrlimit 静默忽略（不报错、不生效），
  但 RLIMIT_CPU / FSIZE / NOFILE / NPROC 仍生效。因此 macOS 下靠「CPU 时间到顶被杀」
  兜底，内存瞬间耗尽仍有风险 —— 需要严格内存隔离请用 Docker / Linux 部署。
"""
from __future__ import annotations

import os
import re
import sys
from typing import Callable, Optional

# `from __future__ import ...` 必须位于文件首部，注入 prologue 时要跳过它
_FUTURE_IMPORT_RE = re.compile(r"^\s*from\s+__future__\s+import\b")

# 默认值（可用环境变量覆盖）。单位：MB / 秒 / 个数。
#
# 评审修正（P1-5 / P1-6）：
# - 内存 2GB→4GB：本项目有视频/大 CSV 场景，2GB 会误杀正当任务。
# - NPROC 512→2048：RLIMIT_NPROC 是**按 UID 统计全部进程**，不止本进程树；
#   忙机器上（浏览器/IDE/Electron）已占数百，512 会让 fork 直接 EAGAIN，
#   表现为所有工具莫名失败。
DEFAULT_MEMORY_MB = 4096
DEFAULT_CPU_SECONDS = 600
DEFAULT_FILE_SIZE_MB = 512
DEFAULT_NOFILE = 1024
DEFAULT_NPROC = 2048


def _env_int(name: str, default: int) -> int:
    """读整数环境变量；非法/缺失回退默认值，0 表示不限制。"""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def limits_enabled() -> bool:
    """是否启用资源限制。默认开；MOSS_RLIMIT=0 关闭（排障用）。"""
    return str(os.getenv("MOSS_RLIMIT", "1")).strip().lower() not in ("0", "false", "off", "no")


def _rlimit_mode() -> str:
    """限制注入方式：ulimit（默认）/ preexec（兼容回退）/ off。"""
    return str(os.getenv("MOSS_RLIMIT_MODE", "ulimit")).strip().lower()


def _read_limits() -> dict[str, int]:
    return {
        "memory_mb": _env_int("MOSS_RLIMIT_MEMORY_MB", DEFAULT_MEMORY_MB),
        "cpu_seconds": _env_int("MOSS_RLIMIT_CPU_SECONDS", DEFAULT_CPU_SECONDS),
        "file_size_mb": _env_int("MOSS_RLIMIT_FILESIZE_MB", DEFAULT_FILE_SIZE_MB),
        "nofile": _env_int("MOSS_RLIMIT_NOFILE", DEFAULT_NOFILE),
        "nproc": _env_int("MOSS_RLIMIT_NPROC", DEFAULT_NPROC),
    }


def describe_limits() -> str:
    """人类可读的限制摘要。

    ponytail: 按平台如实描述——macOS 的 RLIMIT_AS 被内核静默忽略，
    不能对模型/用户承诺一个做不到的内存上限。
    """
    if not limits_enabled():
        return "未启用资源限制"
    lim = _read_limits()
    parts = []
    if lim["memory_mb"] and sys.platform != "darwin":
        parts.append(f"内存 {lim['memory_mb']}MB")
    if lim["cpu_seconds"]:
        parts.append(f"CPU {lim['cpu_seconds']}s")
    if lim["file_size_mb"]:
        parts.append(f"单文件 {lim['file_size_mb']}MB")
    if lim["nproc"]:
        parts.append(f"进程数 {lim['nproc']}")
    return "、".join(parts) or "未启用资源限制"


def ulimit_prefix() -> str:
    """生成 shell 命令前置的资源限制片段（供 run_shell 拼在命令前）。

    由 shell 在 exec 后的子进程里执行，与父进程线程状态无关，完全避开 preexec 死锁。
    ponytail: 只设常见 shell 支持的项；不支持时 `2>/dev/null` 吞掉报错，
    不影响命令本体执行。

    ⚠ 分隔符必须用 `;` 而不是 `&&`：个别环境下 ulimit 会失败（例如硬限低于
    目标值、shell 不支持某项），若串成 `&&` 链条，命令本体就被静默跳过了，
    而退出码只剩一个看不出原因的 1。用 `;` 时限制设失败最多是「没限制」，
    命令照常执行。
    """
    if not limits_enabled() or sys.platform == "win32":
        return ""
    if _rlimit_mode() not in ("ulimit",):
        return ""
    lim = _read_limits()
    parts: list[str] = []
    # -f: 文件大小（单位 1024 字节块）; -t: CPU 秒; -n: 打开文件数; -u: 进程数
    # ponytail: -f 的单位是 1024B 块（不是 512B），MB→块 乘 1024。
    if lim["file_size_mb"]:
        parts.append(f"ulimit -f {lim['file_size_mb'] * 1024} 2>/dev/null")
    if lim["cpu_seconds"]:
        parts.append(f"ulimit -t {lim['cpu_seconds']} 2>/dev/null")
    if lim["nofile"]:
        parts.append(f"ulimit -n {lim['nofile']} 2>/dev/null")
    if lim["nproc"] and sys.platform != "darwin":
        # macOS 的 ulimit -u 常有下限限制且易误伤开发机，仅在 Linux 设
        parts.append(f"ulimit -u {lim['nproc']} 2>/dev/null")
    # 内存：sh 的 ulimit -v 在 macOS 被内核忽略，Linux 有效
    if lim["memory_mb"] and sys.platform.startswith("linux"):
        parts.append(f"ulimit -v {lim['memory_mb'] * 1024} 2>/dev/null")
    if not parts:
        return ""
    # 用 `;` 而非 `&&`：限制没设上也要让命令本体继续跑（见函数 docstring）
    return "; ".join(parts) + "; "


def python_prologue() -> str:
    """生成 Python 脚本首部的资源限制代码（供 run_python 拼在用户代码前）。

    ponytail: 在子进程的解释器里执行 setrlimit，完全避开 preexec_fn 的 fork 死锁。
    每一项单独 try：平台不支持某项时静默跳过（macOS 对 AS/DATA 即如此）。
    """
    if not limits_enabled() or sys.platform == "win32":
        return ""
    # 仅 ulimit 模式用脚本首部注入；preexec 模式由 popen_kwargs() 在 exec 前施加，
    # 两者不能叠加（重复设同一个值无害，但会让「用哪个模式」变得不可辨）。
    if _rlimit_mode() != "ulimit":
        return ""
    lim = _read_limits()
    return (
        "import resource as _moss_rl\n"
        "def _moss_set(_attr, _val):\n"
        "    try:\n"
        "        _w = getattr(_moss_rl, _attr, None)\n"
        "        if _w is not None and _val:\n"
        "            _moss_rl.setrlimit(_w, (_val, _val))\n"
        "    except Exception:\n"
        "        pass\n"
        f"_moss_set('RLIMIT_AS', {lim['memory_mb'] * 1024 * 1024})\n"
        f"_moss_set('RLIMIT_CPU', {lim['cpu_seconds']})\n"
        f"_moss_set('RLIMIT_FSIZE', {lim['file_size_mb'] * 1024 * 1024})\n"
        f"_moss_set('RLIMIT_NOFILE', {lim['nofile']})\n"
        f"_moss_set('RLIMIT_NPROC', {lim['nproc']})\n"
        "del _moss_set\n"
    )


def inject_prologue(code: str) -> str:
    """把资源限制 prologue 注入用户代码，**不破坏 `from __future__` 导入**。

    为什么需要单独处理：Python 要求 `from __future__ import ...` 必须是文件中
    的第一条语句（前面只允许 docstring 与注释/空行）。如果直接把 prologue 拼在
    代码最前面，模型写的 `from __future__ import annotations` 会立刻
    SyntaxError —— 模型写现代 Python 时经常带这一行，等于把 run_python 打断。

    故：若开头（前 50 行内）有 future 导入，就插在**最后一个** future 导入之后；
    否则正常前置。prologue 为空时原样返回，不引入多余空行（避免 traceback 行号偏移）。
    """
    prologue = python_prologue()
    if not prologue:
        return code
    lines = code.splitlines(keepends=True)
    insert_at = 0
    for idx, line in enumerate(lines[:50]):
        if _FUTURE_IMPORT_RE.match(line):
            insert_at = idx + 1
    if insert_at:
        head = "".join(lines[:insert_at])
        if not head.endswith("\n"):
            head += "\n"
        return head + prologue + "".join(lines[insert_at:])
    return prologue + code


def make_preexec() -> Optional[Callable[[], None]]:
    """返回可传给 subprocess 的 preexec_fn；仅 MOSS_RLIMIT_MODE=preexec 时启用。

    ⚠ 默认不启用，原因见模块文档（多线程进程 fork 死锁风险）。
    """
    if sys.platform == "win32" or not limits_enabled():
        return None
    if _rlimit_mode() != "preexec":
        return None
    try:
        import resource  # noqa: PLC0415 — 仅 POSIX 有，惰性导入避免 Windows 崩
    except ImportError:
        return None

    lim = _read_limits()

    def _apply() -> None:
        mb = 1024 * 1024
        # 硬上限与软上限取同值，防止子进程自己把 rlimit 调回无限。
        rules = [
            (getattr(resource, "RLIMIT_AS", None), lim["memory_mb"] * mb if lim["memory_mb"] else None),
            (getattr(resource, "RLIMIT_CPU", None), lim["cpu_seconds"] or None),
            (getattr(resource, "RLIMIT_FSIZE", None), lim["file_size_mb"] * mb if lim["file_size_mb"] else None),
            (getattr(resource, "RLIMIT_NOFILE", None), lim["nofile"] or None),
            (getattr(resource, "RLIMIT_NPROC", None), lim["nproc"] or None),
        ]
        for which, value in rules:
            if which is None or value is None:
                continue
            try:
                resource.setrlimit(which, (value, value))
            except (ValueError, OSError):
                continue

    return _apply


def popen_kwargs() -> dict:
    """给 subprocess.Popen 用的资源限制 kwargs。

    默认返回空：限制改由 `ulimit_prefix()` / `python_prologue()` 在 exec 后施加，
    避免 preexec_fn 在多线程进程中 fork 死锁。MOSS_RLIMIT_MODE=preexec 可回退。
    """
    fn = make_preexec()
    return {"preexec_fn": fn} if fn is not None else {}
