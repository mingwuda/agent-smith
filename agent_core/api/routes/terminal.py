"""交互式终端 WebSocket —— 在已登录用户的工作区（或项目目录）内启动一个 shell。

协议（WebSocket 文本帧，JSON）：
  前端 -> 后端  {"type":"input","data":"..."} | {"type":"resize","cols":80,"rows":24}
  后端 -> 前端  {"type":"ready","cwd":...,"shell":...} | {"type":"output","data":...}
                | {"type":"exit","code":0} | {"type":"error","data":...}

ponytail: 不引入 ptyprocess / paramiko 等依赖。Unix 用标准库 pty.fork() 拿完整
controlling terminal（vim/top/交互式程序可用）；Windows 没有 pty，降级为普通管道
shell，能跑命令但不支持全屏 TUI。两条路径都经 cookie 鉴权，cwd 复用 files.py
同一套「用户 workspace / 项目 directory_path」解析，避免出现第二套路径口径。
"""
import asyncio
import codecs
import json
import logging
import os
import shutil
import signal
import struct
from pathlib import Path

from fastapi import APIRouter, WebSocket

logger = logging.getLogger(__name__)

router = APIRouter(tags=["terminal"])

# 终端是任意命令执行入口，即便有登录态也限制并发，避免误开一堆 shell 拖垮服务
_MAX_SESSIONS = 8
_active_sessions = 0


def _resolve_cwd(uid: str, project_id: str) -> Path:
    from services.workspace import _workspace_for_user

    base = _workspace_for_user(uid)
    if project_id and project_id.strip():
        import session_store

        project = session_store.get_project(uid, project_id)
        if project and project.get("directory_path"):
            base = Path(project["directory_path"])
    base = Path(base).expanduser().resolve()
    if not base.is_dir():
        base = Path.home()
    return base


def _shell_cmd() -> list:
    if os.name == "nt":
        return [os.environ.get("COMSPEC") or "cmd.exe"]
    shell = os.environ.get("SHELL") or shutil.which("zsh") or shutil.which("bash") or "/bin/sh"
    return [shell, "-i"]


def _shell_env() -> dict:
    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env["COLUMNS"] = "80"
    env["LINES"] = "24"
    return env


class _UnixPty:
    """pty.fork() 子进程；主循环用 add_reader 事件驱动读，不在 event loop 里阻塞。"""

    def __init__(self, cwd, args, env, loop, queue):
        self.cwd = cwd
        self.args = args
        self.env = env
        self.loop = loop
        self.queue = queue
        self.pid = 0
        self.master = -1
        self._closed = False

    def start(self):
        import pty

        pid, master = pty.fork()
        if pid == 0:  # 子进程：立刻 exec，不做任何 asyncio 操作
            try:
                os.chdir(str(self.cwd))
                os.execvpe(self.args[0], self.args, self.env)
            except BaseException:
                os._exit(127)
        self.pid = pid
        self.master = master
        self.loop.add_reader(master, self._on_readable)

    def _on_readable(self):
        try:
            data = os.read(self.master, 65536)
        except OSError:
            data = b""  # Linux/macOS 上 slave 关闭时读返回 EIO -> shell 已退出
        self.queue.put_nowait(data)

    def write(self, data: bytes):
        if not self._closed:
            try:
                os.write(self.master, data)
            except OSError:
                pass

    def resize(self, cols: int, rows: int):
        try:
            import fcntl
            import termios

            fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except (ImportError, OSError):
            pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.loop.remove_reader(self.master)
        except Exception:
            pass
        # 放到线程池里回收，避免 waitpid 阻塞 event loop；先 HUP 再 KILL 兜底
        self.loop.run_in_executor(None, self._reap)
        try:
            os.close(self.master)
        except OSError:
            pass

    def _reap(self):
        try:
            try:
                os.killpg(self.pid, signal.SIGHUP)
            except (ProcessLookupError, PermissionError):
                pass
            pid, _ = os.waitpid(self.pid, os.WNOHANG)
            if pid == 0:
                try:
                    os.killpg(self.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                os.waitpid(self.pid, 0)
        except Exception:
            # 在线程池里执行，异常若不吞掉会变成「Future exception was never retrieved」
            pass


class _PipeShell:
    """Windows 降级：普通管道 shell（无 TTY，全屏程序不可用）。"""

    def __init__(self, cwd, args, env, loop, queue):
        self.cwd = cwd
        self.args = args
        self.env = env
        self.loop = loop
        self.queue = queue
        self.proc = None

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            *self.args,
            cwd=str(self.cwd),
            env=self.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        asyncio.create_task(self._pump())

    async def _pump(self):
        while True:
            data = await self.proc.stdout.read(65536)
            self.queue.put_nowait(data)
            if not data:
                break

    def write(self, data: bytes):
        if self.proc and self.proc.stdin:
            self.proc.stdin.write(data)

    def resize(self, cols: int, rows: int):
        pass

    def close(self):
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass


async def _create_session(cwd, args, env, loop, queue):
    if os.name == "nt":
        session = _PipeShell(cwd, args, env, loop, queue)
        await session.start()
    else:
        session = _UnixPty(cwd, args, env, loop, queue)
        session.start()
    return session


@router.websocket("/ws/terminal")
async def terminal_ws(websocket: WebSocket, project_id: str = ""):
    global _active_sessions

    # HTTP middleware 不覆盖 WebSocket，必须在这里显式校验登录态
    from api.deps import AUTH_COOKIE_NAME, _verify_session

    token = websocket.cookies.get(AUTH_COOKIE_NAME, "")
    if not _verify_session(token):
        await websocket.close(code=4401, reason="unauthenticated")
        return
    if _active_sessions >= _MAX_SESSIONS:
        await websocket.close(code=1013, reason="too many terminal sessions")
        return

    await websocket.accept()
    _active_sessions += 1
    uid = token.split(":", 1)[0] or "default"
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()
    session = None
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    try:
        cwd = _resolve_cwd(uid, project_id)
        args = _shell_cmd()
        session = await _create_session(cwd, args, _shell_env(), loop, queue)
        await websocket.send_text(json.dumps({
            "type": "ready", "cwd": str(cwd), "shell": " ".join(args),
        }))

        async def pump_out():
            while True:
                data = await queue.get()
                if not data:
                    await websocket.send_text(json.dumps({"type": "exit", "code": 0}))
                    return
                # 增量解码：多字节 UTF-8 可能被 pty 读切在帧中间
                await websocket.send_text(json.dumps({
                    "type": "output", "data": decoder.decode(data),
                }))

        async def pump_in():
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if msg.get("type") == "input":
                    session.write(str(msg.get("data", "")).encode("utf-8"))
                elif msg.get("type") == "resize":
                    session.resize(int(msg.get("cols", 80)), int(msg.get("rows", 24)))

        tasks = [asyncio.create_task(pump_out()), asyncio.create_task(pump_in())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as exc:  # 建连/启动失败时把原因送回前端，避免只看到一个空黑屏
        logger.warning("terminal session error: %s", exc)
        try:
            await websocket.send_text(json.dumps({"type": "error", "data": str(exc)}))
        except Exception:
            pass
    finally:
        _active_sessions -= 1
        if session is not None:
            session.close()
