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
from collections import defaultdict
from contextvars import ContextVar
from pathlib import Path
from typing import Optional
from langchain_core.tools import tool

# ── 工作区路径（ContextVar：每个 async 请求各自独立）──
_workspace_ctx: ContextVar[Optional[Path]] = ContextVar("shell_tools_workspace", default=None)


def set_workspace(path: Path):
    _workspace_ctx.set(path.expanduser().resolve())


# ── 安全配置 ──
# 始终拒绝的命令/模式（硬编码，不可绕过）
_FORBIDDEN_PATTERNS: list[str] = [
    r'\brm\s+-rf\s+/',                  # rm -rf /（含 /root /etc /home 等任意根下绝对路径，结尾不加 \b 以免 EOL 漏匹配）
    r'\bdd\s+if=',                       # dd 直接写磁盘
    r'\bmkfs\.',                         # 格式化磁盘
    r'\bmkswap\b',                       # swap 操作
    r':\(\)\s*\{.*:\|:.*\};',          # fork bomb
    r'\|\s*shutdown',                    # pipe to shutdown
    r'\bchmod\s+777\s+/',               # chmod 777 /（根下绝对路径，结尾不加 \b）
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


# ── 高危但可确认执行的命令 ──
# 命中后 run_shell 不会真正执行，而是返回 __CONFIRM_NEEDED__ 标记，由前端弹出确认框；
# 用户在前端点「确认执行」后，命令被加入当前用户的已确认集合，代理再次调用时才放行。
# 与 _FORBIDDEN_PATTERNS 的区别：禁止项永不执行，高危项经用户确认后可执行。
_HIGH_RISK_PATTERNS: list[tuple[str, str]] = [
    (r'\brm\b[^|]*\s-[a-z]*r[a-z]*\b', "递归删除 (rm -r/-rf) 会不可恢复地删除文件/目录"),
    (r'\brmdir\b', "删除目录 (rmdir)"),
    (r'\brd\b', "删除目录 (rd)"),
    (r'\bdel\b[^|]*\s/s\b', "Windows 递归删除 (del /s)"),
    (r'\btaskkill\b', "结束进程 (taskkill) 会终止正在运行的程序"),
    (r'\bgit\s+reset\s+--hard\b', "git reset --hard 会丢弃所有未提交改动"),
    (r'\bgit\s+push\b[^|]*--force\b', "git push --force 会覆盖远程历史"),
    (r'\bgit\s+push\b[^|]*\s-f\b', "git push -f 会覆盖远程历史"),
    (r'\bgit\s+clean\b[^|]*-[a-z]*f', "git clean -f 会删除未跟踪文件"),
    (r'\b(shutdown|reboot|halt|poweroff)\b', "关机/重启命令会影响系统运行"),
    (r'\bformat\s+[a-zA-Z]:', "格式化磁盘 (format) 会销毁分区数据"),
    (r'\bdiskpart\b', "diskpart 会修改磁盘分区"),
    (r'\bchmod\s+(-R\s+)?777\b', "chmod 777 会开放任意用户读写执行权限"),
    (r'(curl|wget)\b[^|]*\|\s*(sh|bash)\b', "从网络下载并直接执行脚本存在安全风险"),
]

# 已确认（用户点「确认执行」）的高危命令，按用户隔离，进程内有效。
_approved_commands: dict[str, set[str]] = defaultdict(set)
_current_user: str = "default"


def set_current_user(uid: str) -> None:
    """设置当前执行上下文的用户（用于按用户隔离已确认命令）。"""
    global _current_user
    _current_user = uid


def add_approved_command(uid: str, command: str) -> None:
    """将命令加入指定用户的已确认集合（归一化后存储）。"""
    _approved_commands[uid].add(_normalize_cmd(command))


def _normalize_cmd(cmd: str) -> str:
    """归一化命令：压缩空白、去首尾空格，便于已确认命令的宽松比对。"""
    return re.sub(r"\s+", " ", (cmd or "").strip())


def _is_command_high_risk(command: str) -> tuple[bool, str]:
    """检查命令是否命中高危但可确认执行的模式。返回 (是否高危, 风险说明)。"""
    for pattern, reason in _HIGH_RISK_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return True, reason
    return False, ""


def _is_command_approved(uid: str, command: str) -> bool:
    """命令是否已被当前用户确认过（归一化比对）。"""
    return _normalize_cmd(command) in _approved_commands.get(uid, set())


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

    # ── 高危命令确认闸 ──
    # 命中高危模式且未获当前用户确认时，绝不执行，仅返回确认标记；
    # 前端据此弹出「确认执行」按钮，用户确认后命令进入已确认集合，代理再次调用才放行。
    risky, risk_reason = _is_command_high_risk(cmd)
    if risky and not _is_command_approved(_current_user, command):
        return f"__CONFIRM_NEEDED__::{risk_reason}::__CMD__::{command}"

    # ── 超时上限 ──
    timeout = min(max(1, int(timeout)), 600)

    # ── 选择 shell ──
    shell_cmd = _detect_shell()

    # ── 记录执行前文件元数据快照（仅工作区，用于变更检测）──
    before_files: dict[str, tuple] = {}
    _ws = _workspace_ctx.get()
    if _ws and _ws.is_dir():
        before_files = _snapshot_meta(_ws)

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
            cwd=str(_ws) if _ws else None,
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
    if _ws and _ws.is_dir() and raw_output.strip():
        after_files = _snapshot_meta(_ws)
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
