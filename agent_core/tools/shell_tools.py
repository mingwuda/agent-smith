"""Shell 命令执行工具（跨平台）

支持 Linux / macOS 的 sh/bash/zsh 和 Windows 的 cmd/powershell。
自动检测当前操作系统选择合适的 shell。
"""
import asyncio
import logging
import os
import re
import secrets
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

from tool_result import annotate_for_model
from .async_tasks import (  # ponytail: 同包内相对导入；stdlib 'async' 是关键字所以包名是 async_tasks 不冲突
    AsyncTask, register_task, get_task, list_tasks, cancel_task,
)
from .resource_limits import popen_kwargs, ulimit_prefix

# ponytail: 本模块此前直接使用 logger 却没导入（超时/熔断分支一旦命中就 NameError）。
# 与同目录其它 tools 保持一致，用模块级 stdlib logger。
logger = logging.getLogger(__name__)

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
# run_shell 执行期间，reader 把读取到的输出分块 push 到队列；
# agent_run.py 的心跳循环每 ~2s drain 一次，通过 SSE 事件转发给前端实时展示。
#
# P0-1 并行修正：原来是一个全局队列 + 每次开始 _clear()，两个 run_shell 并行时
# 后启动的会清掉先启动的实时输出，且两路输出混在一起、卡片内容串台。
# 改为「按调用 key 隔离」：每个 run_shell 自己的小队列，drain 时按 key 取。
_SHELL_OUTPUT_QUEUES: "dict[str, deque]" = {}
_SHELL_OUTPUT_LOCK = threading.Lock()

# 当前调用的隔离 key（ContextVar：随 asyncio/线程上下文传递）
_output_key_ctx: ContextVar[str] = ContextVar("shell_output_key", default="default")


def new_output_key() -> str:
    """为一次 run_shell 调用分配唯一输出 key。

    ponytail: 工具内部拿不到 LangGraph 的 run_id（实测 config.get('run_id') 为 None），
    所以由工具自己在入口生成一个随机 key。心跳循环按 key 把输出推出，
    具体落到哪张卡片由 agent_run 结合 running_tools 的 step 决定。
    """
    return "sh-" + secrets.token_hex(4)


def _output_key() -> str:
    """当前 run_shell 调用的隔离 key。

    ponytail: 用 ContextVar 而非线程局部——工具在线程池/事件循环里执行，
    线程不可靠，ContextVar 会随调用传递。未设置时退回 "default"（兼容旧调用点/测试）。
    """
    return _output_key_ctx.get()


def set_output_key(key: str):
    """为本轮工具调用设置隔离 key（由工具层根据 run_id 设置）。"""
    _output_key_ctx.set(key)


def drain_shell_output(key: str = "") -> str:
    """取出并清空指定调用的实时输出缓冲（无新内容返回 ''）。

    key 为空时 drain 默认 key；传具体 key 可定点取某个并行调用的输出。
    线程安全：被 agent_run.py 心跳循环（事件循环线程）与 reader 线程并发调用。
    """
    k = key or _output_key()
    with _SHELL_OUTPUT_LOCK:
        q = _SHELL_OUTPUT_QUEUES.get(k)
        if not q:
            return ""
        parts = []
        while q:
            parts.append(q.popleft())
        return "".join(parts)


def drain_all_shell_output() -> list[tuple[str, str]]:
    """取出所有调用队列的增量输出，返回 [(key, text), ...]。心跳循环并行时需要它。"""
    with _SHELL_OUTPUT_LOCK:
        out: list[tuple[str, str]] = []
        for k, q in _SHELL_OUTPUT_QUEUES.items():
            if not q:
                continue
            parts = []
            while q:
                parts.append(q.popleft())
            out.append((k, "".join(parts)))
        return out


def _emit_output(text: str, key: str = "") -> None:
    """把一段输出追加到指定调用的队列（内部使用）。"""
    if not text:
        return
    k = key or _output_key()
    with _SHELL_OUTPUT_LOCK:
        _SHELL_OUTPUT_QUEUES.setdefault(k, deque()).append(text)


def _clear_shell_output(key: str = "") -> None:
    """清空指定调用的实时输出缓冲（只清自己的，不影响并行中的其它调用）。"""
    k = key or _output_key()
    with _SHELL_OUTPUT_LOCK:
        _SHELL_OUTPUT_QUEUES.pop(k, None)


# 已结束调用的 key：它们的队列保留到最后一块被 drain（on_tool_end 兜底），
# 但最多保留 _FINISHED_KEY_MAX 个，超出的按 FIFO 回收，防止长进程无界增长。
_finished_keys: "deque[str]" = deque()
_FINISHED_KEY_MAX = 32


def _mark_output_finished(key: str) -> None:
    """标记某次调用的输出已结束；队列保留供尾部 drain，但限制总个数。"""
    with _SHELL_OUTPUT_LOCK:
        _finished_keys.append(key)
        while len(_finished_keys) > _FINISHED_KEY_MAX:
            _SHELL_OUTPUT_QUEUES.pop(_finished_keys.popleft(), None)


_SHELL_CMD_CACHE: Optional[list[str]] = None


def _popen_group_kwargs() -> dict:
    """让子进程自成进程组的 Popen 参数（POSIX）。

    ponytail: 配合 async_tasks._kill_process_tree 的 killpg 整组终止。
    `sh -c 'Sleep 30'` 会再 fork 孙子进程，只 kill shell 会留下孤儿，
    开启 start_new_session 后整组可一次回收。Windows 无进程组语义，返回空 dict。
    """
    if os.name == "nt":
        return {}
    return {"start_new_session": True}


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

    返回 None = Jev 不可用（未配置/失败/超时/熔断期内），由调用方放行；
    返回 dict 含 needs_confirmation/probability/reason/available。

    在独立子线程中加软超时兜底，确保最坏情况也不阻塞 run_shell 主流程
    （urllib 内部已有 timeout，这里防御极端慢 DNS / 代理等）。

    注：「Jev 服务端不可用（如 HTTP 451）」的熔断不在本层做，而在
    jev_tools._post 内部统一处理——那里才知道确切的 HTTP 状态码，
    能区分「确定性故障（熔断）」与「单次瞬时异常（仅本次降级）」。
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
async def run_shell(command: str, timeout: int = _DEFAULT_TIMEOUT, description: str = "") -> str:
    """执行 Shell 命令并返回 stdout/stderr 输出。
    自动检测操作系统选择合适的 shell（Linux/macOS 用 bash，Windows 用 cmd）。

    参数:
      command: 要执行的 shell 命令字符串
      timeout: 超时秒数（默认 120，最大 600）
      description: 可选，本次命令的人类可读意图摘要（如 "安装依赖"、"跑测试"），
                    供前端在步骤卡片上优先展示，比命令首行更可读。

    允许的命令类型：文件操作、文本处理、网络工具、系统信息等。
    禁止的操作：提权（sudo/su）、格式化、写裸设备、fork bomb 等。
    资源限制由 resource_limits 统一控制（见 README「子进程资源限制」）。
    超限时进程被操作系统终止，表现为非零退出码。
    注意：非零退出码不一定是失败——grep 无匹配、git diff 有改动都是 1。

    示例:
      run_shell("pip install -r requirements.txt", description="安装依赖")
      run_shell("npm test", description="跑单测")
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
        # P0-1：Jev 门控是同步 HTTP（≤2s），在 async 工具里直接调会阻塞事件循环，
        # 把其它并行工具一起卡住。丢到线程池执行，保持事件循环畅通。
        _jev_gate = await asyncio.to_thread(_jev_risk_gate, cmd)
        if _jev_gate and _jev_gate.get("needs_confirmation"):
            return f"__CONFIRM_NEEDED__::{_jev_gate['reason']}::__CMD__::{command}"

    # ── 超时上限 ──
    timeout = min(max(1, int(timeout)), 600)

    # ── 剥离末尾 '| tail -N' 管道（tail 全缓冲会掐死实时输出，见 _strip_tail_pipe 注释）──
    cmd = _strip_tail_pipe(cmd)

    # ── 资源限制（评审 P1-7）：命令前拼 ulimit，避开 preexec_fn 的多线程 fork 死锁 ──
    cmd = ulimit_prefix() + cmd

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
    # P0-1：每次调用自己的输出 key，并行时各自入各自的队列，不互相清空/串台
    set_output_key(new_output_key())
    return await _run_shell_sync(
        command, cmd, timeout, shell_cmd, before_files, _ws, start_time, description
    )


async def _run_shell_sync(
    command, cmd, timeout, shell_cmd, before_files, _ws, start_time, description=""
):
    """run_shell 主执行路径（async）：先前台跑 30s，超时则把进程转后台任务（handoff）。

    P0-1 改造：从「subprocess.Popen + reader 线程」改为「asyncio subprocess + 协程读」。
    好处：
      - 真正并行：多个 run_shell 在同一事件循环里并发，不再各占一个线程池槽位；
      - 实时输出按调用 key 隔离（见 _SHELL_OUTPUT_QUEUES），并行时不串台。
    保留原语义：短命令直接返回完整结果；≥30s 才 handoff 到后台 task，不 kill 不重跑。

    ponytail: 资源限制默认由命令前的 ulimit 前缀施加（exec 之后，与父进程线程状态无关）；
    MOSS_RLIMIT_MODE=preexec 时改由 popen_kwargs() 传 preexec_fn 回退。
    """
    _key = _output_key()
    _clear_shell_output(_key)
    chunks: list[bytes] = []

    if sys.platform == "win32" and shell_cmd[0] == "cmd":
        cmd = f"@chcp 65001 >nul && {cmd}"

    kwargs = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.STDOUT,
        "cwd": str(_ws) if _ws else None,
        **_popen_group_kwargs(),
        # 默认 {}：限制由命令前的 ulimit 施加。
        # 仅 MOSS_RLIMIT_MODE=preexec 时在这里带 preexec_fn（排障回退路径）——
        # 不回传的话该模式会让 run_shell 完全失去资源限制，与文档承诺不符。
        **popen_kwargs(),
    }
    try:
        proc = await asyncio.create_subprocess_exec(*(shell_cmd + [cmd]), **kwargs)
    except Exception as e:  # noqa: BLE001 — 启动失败要原样告知模型（权限/路径等）
        return f"❌ 执行失败: {e}"

    async def _pump() -> None:
        """持续读 stdout，追加到 chunks + 推实时队列。"""
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            chunks.append(chunk)
            _emit_output(chunk.decode("utf-8", errors="replace"), _key)

    pump_task = asyncio.create_task(_pump())

    try:
        # 前台等 30s（阈值），不是 timeout。
        await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=_ASYNC_THRESHOLD_SECONDS)
        await asyncio.wait_for(pump_task, timeout=5)
        raw_output = _smart_decode(b"".join(chunks))
        elapsed = time.time() - start_time
        returncode = proc.returncode or 0
        # 注意：不在这里清队列。最后一块输出要留给 on_tool_end 的兜底 drain（实时预览尾部），
        # 并且下一次调用开始时 _clear_shell_output(_key) 只清自己的（key 每次调用不同）。
        # 回收交给「调用结束时清理自己」——见 _mark_output_finished。
        _mark_output_finished(_key)
        return _format_shell_result(raw_output, returncode, elapsed, _ws, before_files)
    except asyncio.TimeoutError:
        # 真超过 30s：把已跑的进程 + 已读输出 handoff 给后台任务（不 kill、不重跑）。
        task = register_task(command, description=description)
        task.proc = _AsyncioProcAdapter(proc)
        task.status = "running"
        task.output = _smart_decode(b"".join(chunks))

        async def _continue() -> None:
            """后台：继续读剩余输出直到 EOF，更新 task 终态。"""
            try:
                await pump_task
                await proc.wait()
                if task.status == "cancelled":
                    return
                task.returncode = proc.returncode or 0
                task.status = "done" if proc.returncode == 0 else "failed"
                if proc.returncode:
                    task.error = f"exit code {proc.returncode}"
            except Exception as e:  # noqa: BLE001
                task.status = "failed"
                task.error = str(e)
            finally:
                task.finished_at = time.time()
                task.proc = None
                # handoff 分支原来不回收输出 key，队列字典会随长命令次数无界增长；
                # 在这里标记结束，让 _FINISHED_KEY_MAX 的 FIFO 回收生效。
                _mark_output_finished(_key)

        asyncio.create_task(_continue())

        elapsed = time.time() - start_time
        return (
            f"⏱ 命令前台执行 {elapsed:.0f}s 仍未完成，已转后台继续运行。\n"
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
    except Exception as e:  # noqa: BLE001
        return f"❌ 执行失败: {e}"


class _AsyncioProcAdapter:
    """把 asyncio.subprocess.Process 适配成 async_tasks.cancel_task 能用的形状。

    ponytail: cancel_task 只需要 pid / poll() / kill() / wait(timeout)。
    asyncio.Process 的 wait 是协程，而 cancel_task 在同步线程里调用，
    所以这里用 pid 发信号（配合 start_new_session 的进程组终止），不依赖协程。

    ⚠ 关键（评审 P1-4）：wait() 不能用 `time.sleep` 轮询 `proc.returncode`。
    该属性只由事件循环的 child watcher 更新——若在协程里同步阻塞轮询，
    循环被堵死，returncode 永远不会被写入，于是「进程已死」也会报「未响应 SIGKILL」，
    正好复现了它本来要修的「谎报失败」。故改用 `os.kill(pid, 0)` 探测存活，
    不依赖任何 asyncio 内部状态。
    """

    _POLL_WAIT = 2.0  # 取消路径的等待上限（评审建议：从 10s 降到 2s）

    def __init__(self, proc):
        self._proc = proc
        self.pid = proc.pid
        self.returncode = proc.returncode

    def _alive(self) -> bool:
        """进程是否仍存活（信号 0 只探测，不实际发送）。

        ponytail: 先看 returncode（事件循环已回填时最准），再用 os.kill(pid,0)。
        ESRCH = 进程不存在；EPERM = 存在但无权限（仍算存活）。
        """
        if self._proc.returncode is not None:
            return False
        if self.pid is None:
            return False
        try:
            os.kill(self.pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # 进程在，只是不归我们管
        except OSError:
            return self._proc.returncode is None

    def poll(self):
        if self._proc.returncode is not None:
            return self._proc.returncode
        if not self._alive():
            return -1  # 已退出但事件循环尚未回填
        return None

    def wait(self, timeout=None):
        deadline = time.time() + (timeout if timeout is not None else self._POLL_WAIT)
        while self._alive() and time.time() < deadline:
            time.sleep(0.05)
        self.returncode = self._proc.returncode
        if self._alive():
            raise subprocess.TimeoutExpired(cmd="", timeout=timeout or self._POLL_WAIT)
        return self.returncode if self.returncode is not None else -1

    def kill(self):
        """回退单进程终止（进程组路径由 _kill_process_tree 的 killpg 处理）。"""
        if self._proc.returncode is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass


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

    # 退出码处理（评审 P0-2）：不要用 ❌ 报非零退出。
    # 大量命令的退出码本身就是语义，不是失败：
    #   grep 无匹配=1、diff 有差异=1、[ -f ] 不成立=1、git diff --quiet 有改动=1。
    # 把它们标红会让模型误以为工具坏了而重试或改策略，前端也无谓报红。
    # 故：非零退出用中性的「已执行 (exit code: N)」，只有工具自身硬失败才用 ❌。
    #
    # 例外：126/127 不是「命令跑完后的语义退出码」，而是命令根本没跑起来
    # （127=命令不存在，126=无执行权限）。此时说「已执行」是事实错误，
    # 前端还会显示绿色成功卡 —— 单独按硬失败报。
    if returncode in (126, 127):
        reason = "命令不存在" if returncode == 127 else "命令不可执行（权限不足）"
        mark = f"❌ 命令未执行 (exit code: {returncode}: {reason}, 耗时: {elapsed:.1f}s)"
    elif returncode != 0:
        mark = f"✅ 命令已执行 (exit code: {returncode}, 耗时: {elapsed:.1f}s)"
    elif not raw_output.strip():
        # 明确告知模型：成功但无输出，不是失败（文案由 tool_result 统一维护）
        mark = f"✅ 命令已执行 (exit code: 0, 耗时: {elapsed:.1f}s，无输出) " + annotate_for_model("", "empty")
    else:
        mark = f"✅ 命令已执行 (exit code: 0, 耗时: {elapsed:.1f}s)"
    summary = (
        mark
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
      timeout: 最长等待秒数（默认 10，最大 300）。想要"等它跑完"就直接给足时间
               （如 120），比反复调用 10s 更省往返；超时会返回进度摘要。
    """
    timeout = min(max(1, int(timeout)), 300)
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = get_task(task_id)
        if task is None:
            return f"❌ 任务不存在或已过期: {task_id}"
        # ponytail: 仅在"已开始执行"（running）时继续轮询；其他终态立即返回
        if task.status not in ("pending", "running"):
            return json.dumps(task.to_dict(), ensure_ascii=False, indent=2)
        # 自适应轮询间隔：前 2s 用 50ms（快速命令秒回），随后按已等时长逐步放大到 1s，
        # 避免短任务被固定 1s 粒度拖慢，又不至于长任务空转太频繁。
        waited = timeout - (deadline - time.time())
        time.sleep(0.05 if waited < 2 else min(1.0, 0.2 + waited * 0.05))
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
