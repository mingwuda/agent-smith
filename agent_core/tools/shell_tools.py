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


def _detect_shell() -> list[str]:
    """检测当前操作系统并返回 shell 命令。"""
    if sys.platform == "win32":
        # Windows：优先用 PowerShell，降级到 cmd
        try:
            subprocess.run(
                ["powershell", "-Command", "echo 1"],
                capture_output=True, timeout=5, check=False,
            )
            return ["powershell", "-NoProfile", "-Command"]
        except Exception:
            return ["cmd", "/c"]
    else:
        # Unix/Linux/macOS：用 bash，降级到 sh
        for shell_cmd in ["bash", "zsh", "sh"]:
            try:
                subprocess.run(
                    [shell_cmd, "-c", "echo 1"],
                    capture_output=True, timeout=5, check=False,
                )
                return [shell_cmd, "-c"]
            except Exception:
                continue
        return ["sh", "-c"]


def _is_command_forbidden(command: str) -> tuple[bool, str]:
    """检查命令是否包含被禁止的模式。返回 (是否禁止, 原因)。"""
    for pattern in _FORBIDDEN_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            label = pattern.replace(r"\b", "").strip()
            return True, f"禁止的操作: {label}"
    return False, ""


def _clean_command_for_cmd(command: str) -> str:
    """Windows cmd 下替换路径分隔符等。"""
    return (
        command.replace("/", "\\")
        if sys.platform == "win32"
        else command
    )


@tool
def run_shell(command: str, timeout: int = _DEFAULT_TIMEOUT) -> str:
    """执行 Shell 命令并返回 stdout/stderr 输出。
    自动检测操作系统选择合适的 shell（Linux/macOS 用 bash，Windows 用 powershell）。

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

    # ── 记录执行前文件快照（仅工作区） ──
    before_files: dict[str, str] = {}
    if _workspace and _workspace.is_dir():
        for f in sorted(_workspace.rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                try:
                    before_files[str(f.relative_to(_workspace))] = f.read_text(errors="replace")
                except Exception:
                    pass

    # ── 执行 ──
    raw_output = ""
    start_time = time.time()
    try:
        proc = subprocess.Popen(
            shell_cmd + [cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(_workspace) if _workspace else None,
        )

        def _reader():
            nonlocal raw_output
            for line in proc.stdout:
                raw_output += line

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        proc.wait(timeout=timeout)
        reader_thread.join(timeout=5)

        elapsed = time.time() - start_time
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        reader_thread.join(timeout=2)
        elapsed = time.time() - start_time
        return (
            f"❌ 命令执行超时（{elapsed:.0f}s，上限 {timeout}s）。\n"
            f"已输出 {len(raw_output)} 字符:\n{_truncate(raw_output)}"
        )
    except Exception as e:
        return f"❌ 执行失败: {e}"

    # ── 对比工作区文件变更 ──
    workspace_changes = ""
    if _workspace and _workspace.is_dir() and raw_output.strip():
        changed: list[str] = []
        for f in sorted(_workspace.rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                try:
                    rel = str(f.relative_to(_workspace))
                    new_content = f.read_text(errors="replace")
                    if rel not in before_files:
                        changed.append(f"  + {rel}")
                    elif before_files[rel] != new_content:
                        changed.append(f"  ~ {rel}")
                except Exception:
                    pass
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
