"""Shell 命令执行工具（跨平台）

支持 Linux / macOS 的 sh/bash/zsh 和 Windows 的 cmd/powershell。
自动检测当前操作系统选择合适的 shell。
"""
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional
from langchain_core.tools import tool

# ── 工作区路径（用于限制文件操作范围）──
_workspace: Optional[Path] = None


def set_workspace(path: Path):
    global _workspace
    _workspace = path.expanduser().resolve()


# ── 安全配置 ──
# 始终拒绝的命令/模式（硬编码，不可绕过）
_FORBIDDEN_PATTERNS: list[str] = [
    r'\brm\s+-rf\s+/\b',               # rm -rf /
    r'\brm\s+-rf\s+/root\b',            # rm -rf /root
    r'\brm\s+-rf\s+/etc\b',             # rm -rf /etc
    r'\brm\s+-rf\s+/home\b',            # rm -rf /home
    r'\bdd\s+if=',                       # dd 直接写磁盘
    r'\bmkfs\.',                         # 格式化磁盘
    r'\bmkswap\b',                       # swap 操作
    r':\(\)\s*\{.*:\|:.*\};',          # fork bomb
    r'\|\s*shutdown',                    # pipe to shutdown
    r'\bchmod\s+777\s+/',               # chmod 777 /
    r'\bsudo\b',                         # 不允许提权
    r'\bsu\b',                           # 切换用户
]

# 输出截断
_MAX_OUTPUT_CHARS = 20000
_HEAD_CHARS = 8000
_TAIL_CHARS = 8000

# 默认超时（秒）
_DEFAULT_TIMEOUT = 120


_SHELL_CMD_CACHE: Optional[list[str]] = None


def _detect_shell() -> list[str]:
    """检测当前操作系统并返回 shell 命令（进程内缓存，避免每次调用都启动子进程探测）。"""
    global _SHELL_CMD_CACHE
    if _SHELL_CMD_CACHE is not None:
        return _SHELL_CMD_CACHE
    result: list[str]
    if sys.platform == "win32":
        # Windows：优先用 cmd（兼容用户常见的 cmd 语法 || && >nul 等）
        # PowerShell 不兼容这些语法，作为降级选项
        try:
            subprocess.run(
                ["cmd", "/c", "echo 1"],
                capture_output=True, timeout=5, check=False,
            )
            result = ["cmd", "/c"]
        except Exception:
            result = ["powershell", "-NoProfile", "-Command"]
    else:
        # Unix/Linux/macOS：用 bash，降级到 sh
        result = ["sh", "-c"]
        for shell_cmd in ["bash", "zsh", "sh"]:
            try:
                subprocess.run(
                    [shell_cmd, "-c", "echo 1"],
                    capture_output=True, timeout=5, check=False,
                )
                result = [shell_cmd, "-c"]
                break
            except Exception:
                continue
    _SHELL_CMD_CACHE = result
    return result


# 变更检测时跳过的目录：点目录一律跳过（含 .venv / .venv-windows-build / .git / .workbuddy），
# 再加上这些非点目录的巨型依赖/缓存目录
_SKIP_DIRS = {"node_modules", "dist", "build", "__pycache__"}
_SNAPSHOT_LIMIT = 5000  # 元数据快照最多记录的文件数（仅 stat，开销极低）


def _snapshot_meta(workspace: Path) -> dict[str, tuple]:
    """扫描工作区，返回 {相对路径: (mtime, size)} 元数据快照。

    ponytail: 只 stat 文件、不读内容；跳过巨型依赖/缓存目录并限制数量，
    避免 run_shell 在含 .venv/node_modules 的工作区上卡死（原实现全量 read_text 读全文）。
    mtime/size 足以检测任意内容变更，且比读全文更快更准。
    """
    snap: dict[str, tuple] = {}
    count = 0
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS]
        for f in files:
            if f.startswith(".") or count >= _SNAPSHOT_LIMIT:
                continue
            fp = os.path.join(root, f)
            try:
                st = os.stat(fp)
                snap[os.path.relpath(fp, workspace)] = (st.st_mtime, st.st_size)
                count += 1
            except OSError:
                pass
    return snap


def _is_command_forbidden(command: str) -> tuple[bool, str]:
    """检查命令是否包含被禁止的模式。返回 (是否禁止, 原因)。"""
    for pattern in _FORBIDDEN_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            label = pattern.replace(r"\b", "").strip()
            return True, f"禁止的操作: {label}"
    return False, ""


# cmd 参数标志：以 / 开头、第二字符为字母，且不含点号与额外斜杠（如 /i /s /c:"x" /d:C:\p）
# ponytail: 旧实现把所有非 URL 片段的 / 都替换成 \，会把 findstr /i、dir /s 等参数
# 标志破坏成 \i、\s，导致命令报错（FINDSTR: Cannot open ...）。现跳过疑似 cmd 标志的片段。
_CMD_FLAG_RE = re.compile(r"^/[a-zA-Z][^./\s]*$")


def _is_cmd_flag(segment: str) -> bool:
    """判定片段是否为 cmd 参数标志（如 /i /s /fi /c:"x"），应原样保留。"""
    return bool(_CMD_FLAG_RE.match(segment))


def _smart_decode(data: bytes) -> str:
    """尝试 UTF-8 解码，失败回退 GBK。

    ponytail: Windows cmd 下 Python 等输出 UTF-8，而 wmic 等系统命令在 chcp 65001
    下仍输出 GBK，单一编码必有一方乱码，故做编码回退。上限：同一流混合两种编码会
    失败，但罕见；真遇此情况最终以 utf-8+replace 兜底。
    """
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _clean_command_for_cmd(command: str) -> str:
    """Windows cmd 下将路径分隔符 / 替换为 \\，但保留 cmd 参数标志（如 /i /s /c:）。"""
    if sys.platform != "win32":
        return command
    parts = []
    for segment in command.split():
        if "://" in segment:
            parts.append(segment)       # URL 原样保留
        elif _is_cmd_flag(segment):
            parts.append(segment)       # cmd 参数标志，原样保留
        else:
            parts.append(segment.replace("/", "\\"))  # 路径等：/ -> \
    return " ".join(parts)


@tool
def run_shell(command: str, timeout: int = _DEFAULT_TIMEOUT) -> str:
    """执行 Shell 命令并返回 stdout/stderr 输出。
    自动检测操作系统选择合适的 shell（Linux/macOS 用 bash，Windows 用 cmd）。

    参数:
      command: 要执行的 shell 命令字符串
      timeout: 超时秒数（默认 120，最大 600）

    允许的命令类型：文件操作、文本处理、网络工具、系统信息等。
    禁止的操作：提权（sudo/su）、格式化、写裸设备、fork bomb 等。

    示例:
      run_shell("ls -la /tmp")
      run_shell("cat /etc/hostname")
      run_shell("find . -name '*.py' | head -20")
    """
    # ── 安全检查 ──
    cmd = _clean_command_for_cmd(command)
    forbidden, reason = _is_command_forbidden(cmd)
    if forbidden:
        return f"❌ {reason}。请使用更安全的命令重试。"

    # ── 超时上限 ──
    timeout = min(max(1, int(timeout)), 600)

    # ── 选择 shell ──
    shell_cmd = _detect_shell()

    # ── 记录执行前文件元数据快照（仅工作区，用于变更检测）──
    before_files: dict[str, tuple] = {}
    if _workspace and _workspace.is_dir():
        before_files = _snapshot_meta(_workspace)

    # ── 执行 ──
    raw_bytes = b""
    start_time = time.time()
    try:
        # Windows 下 cmd 默认使用 GBK/cp936 编码。
        # 强制切到 UTF-8 代码页让 Python 等命令中文不乱码；
        # 但 wmic 等系统命令在该代码页下仍输出 GBK，
        # 故读取后做 utf-8 优先、GBK 回退的智能解码（见 _smart_decode）。
        if sys.platform == "win32" and shell_cmd[0] == "cmd":
            cmd = f"@chcp 65001 >nul && {cmd}"
        proc = subprocess.Popen(
            shell_cmd + [cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(_workspace) if _workspace else None,
        )

        def _reader():
            nonlocal raw_bytes
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                raw_bytes += chunk

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        proc.wait(timeout=timeout)
        reader_thread.join(timeout=5)

        raw_output = _smart_decode(raw_bytes)
        elapsed = time.time() - start_time
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        reader_thread.join(timeout=2)
        elapsed = time.time() - start_time
        raw_output = _smart_decode(raw_bytes)
        return (
            f"❌ 命令执行超时（{elapsed:.0f}s，上限 {timeout}s）。\n"
            f"已输出 {len(raw_output)} 字符:\n{_truncate(raw_output)}"
        )
    except Exception as e:
        return f"❌ 执行失败: {e}"

    # ── 对比工作区文件变更（基于 mtime/size，不读取文件内容）──
    workspace_changes = ""
    if _workspace and _workspace.is_dir() and raw_output.strip():
        after_files = _snapshot_meta(_workspace)
        changed: list[str] = []
        for rel, meta in after_files.items():
            if rel not in before_files:
                changed.append(f"  + {rel}")
            elif before_files[rel] != meta:
                changed.append(f"  ~ {rel}")
        if changed:
            workspace_changes = (
                f"\n\n工作区文件变更（{len(changed)} 个）:\n"
                + "\n".join(changed[:20])
                + ("\n  ..." if len(changed) > 20 else "")
            )

    # ── 格式化输出 ──
    summary = (
        f"✅ 命令已执行 (exit code: {returncode}, 耗时: {elapsed:.1f}s)"
        + workspace_changes
        + "\n\n"
        + _truncate(raw_output)
    )
    return summary


def _truncate(text: str) -> str:
    """截断过长的输出。"""
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return (
        f"[输出过长，共 {len(text)} 字符，仅显示头尾]\n\n"
        f"--- 开头 {_HEAD_CHARS} 字符 ---\n"
        f"{text[:_HEAD_CHARS]}\n\n"
        f"--- 结尾 {_TAIL_CHARS} 字符 ---\n"
        f"{text[-_TAIL_CHARS:]}"
    )


TOOLS = [run_shell]
