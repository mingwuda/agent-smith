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
import json
from collections import defaultdict, deque
from contextvars import ContextVar
from pathlib import Path
from typing import Optional
from langchain_core.tools import tool

from .async_tasks import (  # ponytail: 同包内相对导入；stdlib 'async' 是关键字所以包名是 async_tasks 不冲突
    AsyncTask, register_task, get_task, list_tasks, cancel_task,
)

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

# 异步任务阈值：命令预计耗时 > 此值时，run_shell 启动后台任务并立即返回 task_id，
# 避免 agent 主循环被单个长任务阻塞。agent 拿到 task_id 后用 get_async_task / wait_async_task 跟进。
# ponytail: 30s 阈值是经验值——90% shell 命令 < 30s；阈值过小会让简单任务变复杂。
# 上限：若预估不准，命令会"立即返回"但实际还在跑；agent 用 list_async_tasks 自行发现。
# ponytail: 阈值也可由 agent 通过 run_shell(timeout=...) 调整——> 阈值时仍走后台。
_ASYNC_THRESHOLD_SECONDS = 30

# ── 实时输出缓冲（方案B：心跳注入）──
# run_shell 执行期间，_reader() 线程把读取到的输出分块 push 到队列；
# agent_run.py 的心跳循环每 ~2s drain 一次，通过 SSE 事件转发给前端实时展示。
# ponytail: 分块 decode 用 utf-8+replace，多字节字符可能被切到中间产生个别乱码字符，
# 但完整结果仍走 _smart_decode 全量解码，工具返回值不受影响（实时展示允许小瑕疵）。
_SHELL_OUTPUT_QUEUE: deque = deque()
_SHELL_OUTPUT_LOCK = threading.Lock()


def drain_shell_output() -> str:
    """取出并清空实时输出缓冲，返回拼接后的字符串（无新内容返回 ''）。

    线程安全：被 agent_run.py 心跳循环（事件循环线程）与 _reader()（子线程）并发调用。
    """
    with _SHELL_OUTPUT_LOCK:
        if not _SHELL_OUTPUT_QUEUE:
            return ""
        parts = []
        while _SHELL_OUTPUT_QUEUE:
            parts.append(_SHELL_OUTPUT_QUEUE.popleft())
        return "".join(parts)


def _clear_shell_output() -> None:
    """清空实时输出缓冲。每次 run_shell 开始/结束时调用，防止残留混入下一次调用。"""
    with _SHELL_OUTPUT_LOCK:
        _SHELL_OUTPUT_QUEUE.clear()


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


# ── 命令末尾 '| tail -N' 剥离 ──
# LLM 常为控制输出量在命令末尾拼 '2>&1 | tail -N'，而 tail 全缓冲——进程结束前
# 不输出任何字节，会掐死实时输出（reader 线程 read1 读不到数据，队列一直空）。
# 输出量控制由 _truncate 兜底（保留头尾各 8000 字符，信息量比 tail 更全）。
# ponytail: 只剥末尾管道段；'tail -N file'（读文件非管道）与 '| tail | wc'（中间段）
# 语义不同，不误伤。若未来出现 '| tail -N > file' 等变体再扩展正则。
_TAIL_PIPE_RE = re.compile(r"\|\s*tail\s+(?:-n\s*)?-?\d+\s*$")


def _strip_tail_pipe(command: str) -> str:
    """剥离命令末尾的 '| tail -N' 管道段（兼容 '2>&1 | tail -N' 与 '| tail -n N'）。"""
    return _TAIL_PIPE_RE.sub("", command).rstrip()


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
_current_user_ctx: ContextVar[str] = ContextVar("shell_tools_user", default="default")


def set_current_user(uid: str) -> None:
    """设置当前执行上下文的用户（用于按用户隔离已确认命令）。"""
    _current_user_ctx.set(uid)


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


# ── Jev 语义风险门控辅助 ──────────────────────────────────────
def _jev_risk_gate_enabled() -> bool:
    """Jev 风险门控是否开启。默认开启，可用环境变量 JEV_RISK_GATE=0 关闭。"""
    return str(os.getenv("JEV_RISK_GATE", "1")).strip().lower() not in ("0", "false", "off", "no")


def _jev_risk_gate(command: str) -> Optional[dict]:
    """调用 Jev 判断命令是否存在正则未覆盖的语义风险。

    返回 None = Jev 不可用（未配置/失败/超时），由调用方放行；
    返回 dict 含 needs_confirmation/probability/reason/available。

    在独立子线程中加软超时兜底，确保最坏情况也不阻塞 run_shell 主流程
    （urllib 内部已有 timeout，这里防御极端慢 DNS / 代理等）。
    """
    try:
        from .jev_tools import risk_gate, _load_key
        if not _load_key():
            return None  # 未配置 key，静默放行（交给既有正则闸）
    except Exception:
        return None

    result: dict = {}
    # 软超时：最多等 _JEV_GATE_TIMEOUT 秒；超时视为不可用
    timeout_s = float(os.getenv("JEV_RISK_GATE_TIMEOUT", "2.0"))
    try:
        import threading as _threading

        def _target():
            try:
                result.update(risk_gate(command))
            except Exception:
                result["needs_confirmation"] = False
                result["available"] = False

        t = _threading.Thread(target=_target, daemon=True)
        t.start()
        t.join(timeout_s)
        if t.is_alive():
            # 超时：不阻塞，放行
            logger.warning("Jev 语义风险门控超时(>%.0fs)，放行: %.40s", timeout_s, command)
            return None
    except Exception:
        return None
    return result or None


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
    if risky and not _is_command_approved(_current_user_ctx.get(), command):
        return f"__CONFIRM_NEEDED__::{risk_reason}::__CMD__::{command}"

    # ── Jev 语义风险门控（补漏层）──
    # 正则未命中时，用 Jev(系统一决策)判断命令是否存在正则覆盖不到的语义风险
    # （混淆 / 下载即执行 / 编码绕过 / 多条高危叠加等），命中且用户未确认则追加确认闸。
    # 设计：
    #   - 仅当正则令人误放行时才介入，避免重复打扰；
    #   - 未配置 key / 调用失败 / 超时 → 静默放行，绝不阻塞 run_shell；
    #   - 可在设置中通过 JEV_RISK_GATE=0 全局关闭。
    if _jev_risk_gate_enabled() and not risky and not _is_command_approved(_current_user_ctx.get(), command):
        _jev_gate = _jev_risk_gate(cmd)
        if _jev_gate and _jev_gate.get("needs_confirmation"):
            return f"__CONFIRM_NEEDED__::{_jev_gate['reason']}::__CMD__::{command}"

    # ── 超时上限 ──
    timeout = min(max(1, int(timeout)), 600)

    # ── 剥离末尾 '| tail -N' 管道（tail 全缓冲会掐死实时输出，见 _strip_tail_pipe 注释）──
    cmd = _strip_tail_pipe(cmd)

    # ── 选择 shell ──
    shell_cmd = _detect_shell()

    # ── 记录执行前文件元数据快照（仅工作区，用于变更检测）──
    before_files: dict[str, tuple] = {}
    _ws = _workspace_ctx.get()
    if _ws and _ws.is_dir():
        before_files = _snapshot_meta(_ws)

    # ── 选择同步/异步 ──
    # ponytail: P0 修复——所有命令都走 _run_shell_sync，sync 内部在 30s 时自动 handoff
    # 到后台。原来"按 timeout 预判走异步"的逻辑被移除，因为它会过度异步化（默认
    # timeout=120 > 阈值 30，导致 echo 这种 0.1s 命令也被错放后台）。
    start_time = time.time()
    return _run_shell_sync(command, cmd, timeout, shell_cmd, before_files, _ws, start_time)


def _run_shell_sync(command, cmd, timeout, shell_cmd, before_files, _ws, start_time):
    """run_shell 同步执行路径：先前台跑 30s，超时则把进程转后台任务（handoff），
    立即返回 task_id。

    ponytail: 这是 P0 修复——把"按 timeout 预判走异步"改成"先前台真跑，
    30s 真超时再 handoff 到后台"。短命令 (<30s) 行为完全不变（agent 拿到完整结果）；
    长命令 (>=30s) 切后台后立即返回 task_id，agent 拿到 task_id 后用跟进工具查结果。

    ponytail: handoff 设计——同步路径下的 Popen 进程 + reader 线程 + 已累积的 raw_bytes
    全部转移到新 task 上；后台补一个 wait_and_collect 线程读剩余输出并终结 task。
    这种"前台进程→后台任务"的桥接比"kill + 重启"更省：不会丢失已跑的 30s 进度，
    已累积输出也已经写进 task.output。
    """
    raw_bytes = b""
    _clear_shell_output()
    proc: Optional[subprocess.Popen] = None
    reader_thread: Optional[threading.Thread] = None

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
            chunk = proc.stdout.read1(4096)
            if not chunk:
                break
            raw_bytes += chunk
            with _SHELL_OUTPUT_LOCK:
                _SHELL_OUTPUT_QUEUE.append(chunk.decode("utf-8", errors="replace"))

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    try:
        # ponytail: 前台等 30s（阈值），不是 timeout。timeout 仅在"命令真要跑很久"时
        # 决定 handoff 后最多再跑多久（防止忘关的后台进程无限占资源）。
        proc.wait(timeout=_ASYNC_THRESHOLD_SECONDS)
        reader_thread.join(timeout=5)
        raw_output = _smart_decode(raw_bytes)
        elapsed = time.time() - start_time
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        # ponytail: 真超过 30s——把已跑的进程 + 已读的输出 handoff 给后台任务。
        # 不 kill、不重启；让命令继续跑到 timeout 或自然结束，agent 用 task_id 跟进。
        task = register_task(command)
        task.proc = proc
        task.status = "running"  # 已 Popen 启动，立即置 running
        task.output = _smart_decode(raw_bytes)  # 已累积的 30s 输出写进 task
        _raw_so_far = raw_bytes  # 闭包给后台 reader 用

        def _continue_reader():
            """后台线程：继续读剩余 stdout，更新 task.status。"""
            try:
                more = b""
                while True:
                    chunk = proc.stdout.read1(4096)
                    if not chunk:
                        break
                    more += chunk
                    text = chunk.decode("utf-8", errors="replace")
                    with _SHELL_OUTPUT_LOCK:
                        _SHELL_OUTPUT_QUEUE.append(text)
                proc.wait()  # 读到 EOF 后 wait 不阻塞
                decoded_more = _smart_decode(more)
                task.output = task.output + decoded_more if task.output else decoded_more
                if task.status == "cancelled":
                    return
                task.returncode = proc.returncode
                task.status = "done" if proc.returncode == 0 else "failed"
                if proc.returncode != 0:
                    task.error = f"exit code {proc.returncode}"
            except Exception as e:
                task.status = "failed"
                task.error = str(e)
            finally:
                task.finished_at = time.time()
                task.proc = None

        threading.Thread(target=_continue_reader, daemon=True).start()
        # 同步 reader 线程也让它退出（Popen 已没人引用，但它还在 read1 阻塞）
        if reader_thread and reader_thread.is_alive():
            reader_thread.join(timeout=2)  # 等它自然结束（stdout 已被 _continue_reader 持有读）

        elapsed = time.time() - start_time
        return (
            f"⏱️ 命令前台执行 {elapsed:.0f}s 仍未完成，已转后台继续运行。\n"
            f"- task_id: {task.task_id}\n"
            f"- 原 timeout: {timeout}s（后台继续运行至超时）\n"
            f"- 命令: {command[:200]}\n"
            f"\n"
            f"**请用以下工具跟进，不要干等：**\n"
            f"- get_async_task(task_id='{task.task_id}')：查状态\n"
            f"- wait_async_task(task_id='{task.task_id}', timeout=10)：等待完成（短轮询）\n"
            f"- cancel_async_task(task_id='{task.task_id}')：终止\n"
            f"- list_async_tasks()：查看所有在飞任务\n"
            f"\n"
            f"实时输出仍会通过 SSE 推送到前端，无需主动拉取。"
        )
    except Exception as e:
        return f"❌ 执行失败: {e}"

    return _format_shell_result(raw_output, returncode, elapsed, _ws, before_files)


def _run_shell_async(command, cmd, timeout, shell_cmd, before_files, _ws):
    """run_shell 异步执行路径（长任务，>= 30s）：启动后台进程，立即返回 task_id。

    ponytail: 后台线程独立 Popen + 独立 reader，不阻塞主流程。任务调度（get/wait/cancel/list）
    由 agent 用新工具跟进，状态存于 async_tasks 进程级 dict。
    """
    task = register_task(command)
    _clear_shell_output()

    def _runner():
        """后台线程：跑命令、读输出、更新任务状态。"""
        try:
            # ponytail: 进入 Popen 前再 check 一次状态——若 cancel_task 已把 status 标为
            # cancelled（极短窗口内），则不再启动进程，直接退出。
            _cur = get_task(task.task_id)
            if _cur is None or _cur.status == "cancelled":
                return
            if sys.platform == "win32" and shell_cmd[0] == "cmd":
                _async_cmd = f"@chcp 65001 >nul && {cmd}"
            else:
                _async_cmd = cmd
            proc = subprocess.Popen(
                shell_cmd + [_async_cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(_ws) if _ws else None,
            )
            task.proc = proc  # 暴露给 cancel_task
            # ponytail: Popen 启动后立即把 status 升为 running，让 list/wait 看到正确状态。
            # 此时若 cancel_task 来调用，能正常 kill 进程（不是 pending 路径）。
            if task.status == "pending":
                task.status = "running"
            output_chunks: list[str] = []
            while True:
                chunk = proc.stdout.read1(4096)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                output_chunks.append(text)
                task.output += text
                with _SHELL_OUTPUT_LOCK:
                    _SHELL_OUTPUT_QUEUE.append(text)
            proc.wait()
            # ponytail: 若在 proc.wait() 返回前被 cancel_task 标为 cancelled，
            # 不要把 status 覆盖为 done/failed，否则 cancel 测试会看到状态在两者间抖动。
            if task.status == "cancelled":
                return
            task.returncode = proc.returncode
            if proc.returncode == 0:
                task.status = "done"
            else:
                task.status = "failed"
                task.error = f"exit code {proc.returncode}"
        except Exception as e:
            task.status = "failed"
            task.error = str(e)
        finally:
            task.finished_at = time.time()
            task.proc = None  # 释放 fd

    threading.Thread(target=_runner, daemon=True).start()

    return (
        f"⏱️ 长任务已在后台启动。\n"
        f"- task_id: {task.task_id}\n"
        f"- timeout: {timeout}s（后台继续运行至超时）\n"
        f"- 命令: {command[:200]}\n"
        f"\n"
        f"**请用以下工具跟进，不要干等：**\n"
        f"- get_async_task(task_id='{task.task_id}')：查状态\n"
        f"- wait_async_task(task_id='{task.task_id}', timeout=10)：等待完成（短轮询）\n"
        f"- cancel_async_task(task_id='{task.task_id}')：终止\n"
        f"- list_async_tasks()：查看所有在飞任务\n"
        f"\n"
        f"实时输出仍会通过 SSE 推送到前端，无需主动拉取。"
    )


def _format_shell_result(raw_output: str, returncode: int, elapsed: float, _ws, before_files) -> str:
    """格式化 shell 执行结果（同步路径用）。"""
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
        f"⚠️ 输出过长（共 {len(text)} 字符），上下文仅保留头尾各 {_HEAD_CHARS} 字符。\n"
        f"关键信息（如错误堆栈末尾、退出码）通常位于结尾部分。\n\n"
        f"--- 开头 {_HEAD_CHARS} 字符 ---\n"
        f"{text[:_HEAD_CHARS]}\n\n"
        f"--- 结尾 {_TAIL_CHARS} 字符 ---\n"
        f"{text[-_TAIL_CHARS:]}"
    )


# ── 异步任务跟进工具（4 个，配合 run_shell 异步路径使用）──
# ponytail: agent 拿到 run_shell 返回的 task_id 后，必须能用这套工具主动跟进。
# 不再傻等 run_shell 同步返回——那是 agent 频繁卡死的根因。


@tool
def get_async_task(task_id: str) -> str:
    """查询异步任务状态。

    用于跟进 run_shell 启动的后台任务（拿到 task_id 后调用）。
    返回任务的当前状态、已运行时间、已输出字符数、最后 500 字符输出预览。

    参数:
      task_id: run_shell 启动长任务时返回的 12 位 task_id
    """
    task = get_task(task_id)
    if task is None:
        return f"❌ 任务不存在或已过期: {task_id}（完成后 10 分钟会被清理）"
    return json.dumps(task.to_dict(), ensure_ascii=False, indent=2)


@tool
def wait_async_task(task_id: str, timeout: int = 10) -> str:
    """等待异步任务完成（短轮询）。

    在 timeout 秒内每隔 1 秒检查一次任务状态；
    任务完成（done / failed / cancelled / timeout）立即返回结果，
    超时则返回当前状态（不抛错，agent 可选择继续等或做别的）。

    ponytail: 用短轮询而非 signal，是因为跨线程/跨协程的 signal 通知复杂且易泄漏；
    1s 轮询粒度对"等命令完成"这个语义足够，对前台用户也不卡顿。
    ponytail: 仅在 status == "running" 时继续轮询；pending 状态视为"还没起来"也继续等；
    其他终态（done/failed/cancelled/timeout）立即返回。

    参数:
      task_id: 任务 ID
      timeout: 最长等待秒数（默认 10，最大 60）
    """
    timeout = min(max(1, int(timeout)), 60)
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = get_task(task_id)
        if task is None:
            return f"❌ 任务不存在或已过期: {task_id}"
        # ponytail: 仅在"已开始执行"（running）时继续轮询；其他终态立即返回
        if task.status not in ("pending", "running"):
            return json.dumps(task.to_dict(), ensure_ascii=False, indent=2)
        time.sleep(1)
    # 仍 running，返回当前状态摘要，agent 决定下一步
    task = get_task(task_id)
    if task is None:
        return f"❌ 任务不存在或已过期: {task_id}"
    return (
        f"⏳ 任务仍在运行（已等 {timeout}s）。\n"
        f"task_id: {task_id}\n"
        f"elapsed: {task.elapsed:.1f}s\n"
        f"output_chars: {len(task.output)}\n"
        f"last_preview: {task.output[-200:] if task.output else '(无输出)'}\n"
        f"\n可继续 wait_async_task 等更久，或 cancel_async_task 终止，或 list_async_tasks 查看其他任务。"
    )


@tool
def cancel_async_task(task_id: str) -> str:
    """取消正在运行的异步任务（终止后台进程）。

    仅对 running 状态的任务有效；已结束的任务返回错误说明。
    任务被取消后仍可在 list_async_tasks 看到（保留 10 分钟），状态为 cancelled。

    参数:
      task_id: 要终止的任务 ID
    """
    ok, msg = cancel_task(task_id)
    return ("✅ " if ok else "❌ ") + msg


@tool
def list_async_tasks(include_done: bool = True) -> str:
    """列出所有异步任务（默认含已完成）。

    用于盘点本会话期间启动的所有后台命令，检查是否有遗漏未跟进的任务。
    include_done=False 时只列 running（用于快速检查"还有没有卡住的任务"）。

    参数:
      include_done: 是否包含已完成任务（默认 True）
    """
    tasks = list_tasks(include_done=include_done)
    if not tasks:
        return "📭 当前无任何后台任务。"
    lines = [f"📋 异步任务（共 {len(tasks)} 个）:"]
    for t in tasks:
        # ponytail: 进程仍在工作的都视为"在飞"（pending + running）；"无任何后台任务"用 list_tasks 判定
        status_emoji = {
            "pending": "⏳", "running": "⏱️", "done": "✅", "failed": "❌", "cancelled": "🚫", "timeout": "⏰",
        }.get(t.status, "❔")
        lines.append(
            f"  {status_emoji} {t.task_id} | {t.status} | {t.elapsed:.1f}s | {len(t.output)} chars | {t.command[:80]}"
        )
    return "\n".join(lines)


# JSON 用于格式化任务状态输出（json 已在文件顶部 import）


TOOLS = [run_shell, get_async_task, wait_async_task, cancel_async_task, list_async_tasks]
