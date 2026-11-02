"""资源限制（rlimit）测试。

只测「约定」，不测平台细节：不同 OS 对某项 rlimit 的支持不一样，
断言写死具体值会在 macOS/Linux 之间假红。这里验的是：
1. 限制确实传进了子进程（至少 CPU/文件大小生效）；
2. 超限时进程真的被系统终止，而不是静默跑完；
3. MOSS_RLIMIT=0 能关闭限制（排障逃生门）。

评审修正（P2）：原实现每次跑都往 /tmp 写 600MB 验证 FSIZE，CI 上耗时耗盘。
改为用 MOSS_RLIMIT_FILESIZE_MB=1 起子进程验证，几 KB 就能触发。
"""
import os
import subprocess
import sys

from agent_core.main import app  # noqa: F401  (sys.path 注入)
from agent_core.tools import resource_limits as rl


def _run(code: str, env_extra: dict | None = None) -> tuple[int, str]:
    """在受限制的子进程里跑代码（ulimit 模式走 python_prologue）。"""
    env = {**os.environ, **(env_extra or {})}
    prologue = rl.python_prologue() if not env_extra else _prologue_with(env_extra)
    proc = subprocess.Popen(
        [sys.executable, "-c", prologue + code],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
    )
    out = proc.communicate()[0]
    return proc.returncode, out or ""


def _prologue_with(env_extra: dict) -> str:
    """按临时环境变量生成 prologue（测试需要覆盖默认值）。"""
    saved = {k: os.environ.get(k) for k in env_extra}
    os.environ.update(env_extra)
    try:
        return rl.python_prologue()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_limits_reach_child_process():
    """限制必须真的写进子进程，而不只是构造了个函数。"""
    if sys.platform == "win32":
        return  # Windows 无 rlimit
    rc, out = _run("import resource; print(resource.getrlimit(resource.RLIMIT_CPU)[0])")
    assert rc == 0, out
    if not os.getenv("MOSS_RLIMIT_CPU_SECONDS"):
        assert out.strip() == "600", out


def test_file_size_limit_actually_blocks_writes():
    """真正超限的行为：写超过上限的文件应被 SIGXFSZ 杀死。

    用 1MB 上限 + 写 2MB 验证，避免 CI 上真写 600MB。
    """
    if sys.platform == "win32":
        return
    rc, _ = _run(
        "import tempfile, os;"
        "p=os.path.join(tempfile.gettempdir(),'_rl_probe.bin');"
        "f=open(p,'wb');"
        "f.write(b'x'*(2*1024*1024));"
        "f.close();"
        "print('NOT_KILLED')",
        env_extra={"MOSS_RLIMIT_FILESIZE_MB": "1"},
    )
    assert rc != 0, "超过文件大小上限应被终止"


def test_limits_can_be_disabled():
    """MOSS_RLIMIT=0 必须能关闭限制（排障逃生门）。"""
    old = os.environ.get("MOSS_RLIMIT")
    os.environ["MOSS_RLIMIT"] = "0"
    try:
        assert rl.limits_enabled() is False
        assert rl.ulimit_prefix() == ""
        assert rl.python_prologue() == ""
    finally:
        if old is None:
            os.environ.pop("MOSS_RLIMIT", None)
        else:
            os.environ["MOSS_RLIMIT"] = old


def test_no_preexec_by_default():
    """评审 P1-7：默认不能用 preexec_fn —— 多线程进程 fork 后 exec 前会死锁。

    改为 ulimit / 脚本首部自设（exec 之后施加，与父进程线程状态无关）。
    """
    old = os.environ.get("MOSS_RLIMIT_MODE")
    os.environ.pop("MOSS_RLIMIT_MODE", None)
    try:
        assert rl.popen_kwargs() == {}, "默认不应传 preexec_fn"
        assert rl.ulimit_prefix(), "应改为用 ulimit 前缀施加限制"
        assert rl.python_prologue(), "run_python 应有脚本首部 prologue"
    finally:
        if old is not None:
            os.environ["MOSS_RLIMIT_MODE"] = old


def test_describe_does_not_promise_memory_on_macos():
    """工具描述不能对 macOS 承诺做不到的内存上限（AS 被内核忽略）。"""
    desc = rl.describe_limits()
    if sys.platform == "darwin":
        assert "内存" not in desc, desc
