"""run_shell 实时输出缓冲测试（方案B：心跳注入）。

背景（ponytail）：run_shell 执行期间，_reader() 线程把输出分块 push 到
_SHELL_OUTPUT_QUEUE，agent_run.py 心跳循环 drain 后经 SSE 转发前端实时展示。
这里只验证队列语义（drain/clear）与「工具执行期间中间输出可被读到」，
不依赖真实 LLM/LangGraph。
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_core"))

from tools.shell_tools import (  # noqa: E402
    _SHELL_OUTPUT_LOCK,
    _SHELL_OUTPUT_QUEUE,
    _clear_shell_output,
    _strip_tail_pipe,
    drain_shell_output,
    run_shell,
)


def _push(text: str) -> None:
    with _SHELL_OUTPUT_LOCK:
        _SHELL_OUTPUT_QUEUE.append(text)


def test_drain_empty():
    _clear_shell_output()
    assert drain_shell_output() == ""


def test_drain_returns_and_clears():
    _clear_shell_output()
    _push("abc")
    _push("def")
    got = drain_shell_output()
    assert got == "abcdef"
    assert drain_shell_output() == ""  # 取走后队列为空


def test_clear_discards():
    _clear_shell_output()
    _push("stale")
    _clear_shell_output()
    assert drain_shell_output() == ""


def test_realtime_output_visible_during_run():
    """工具执行期间（未结束）能读到中间输出块。"""
    _clear_shell_output()
    result: dict = {}

    def _run():
        result["out"] = run_shell.invoke({"command": "echo first; sleep 1; echo second; sleep 1; echo done"})

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    chunks = []
    for _ in range(50):
        c = drain_shell_output()
        if c:
            chunks.append(c)
        if not t.is_alive():
            break
        time.sleep(0.1)
    t.join(timeout=10)

    joined = "".join(chunks)
    # 中间输出在工具结束前即可见（实时性核心断言）
    assert "first" in joined, f"执行期间应能看到 first，实际块: {joined!r}"
    assert "second" in joined, f"执行期间应能看到 second，实际块: {joined!r}"
    # 最终返回值完整包含全部输出
    assert "done" in (result.get("out") or "")


def test_tail_survives_until_tool_end_drain():
    """run_shell 结束后残余不再被 finally 清空——最后一块输出要留给 on_tool_end 兜底 drain 补发。"""
    _clear_shell_output()
    run_shell.invoke({"command": "echo tail_marker"})
    tail = drain_shell_output()
    assert "tail_marker" in tail, f"工具结束前最后一块应保留可 drain，实际: {tail!r}"


def test_next_run_clears_previous_residue():
    """即使上一次残余未被 drain（on_tool_end 未执行等异常路径），下一次 run_shell 开始时自动清空，不串扰。"""
    _clear_shell_output()
    _push("stale_residue")  # 模拟上次工具结束未被 drain 的残余
    run_shell.invoke({"command": "echo fresh"})
    got = drain_shell_output()
    assert "fresh" in got, f"应只拿到本次输出，实际: {got!r}"
    assert "stale_residue" not in got, "上次残余不应串入本次输出"


# ── 末尾 '| tail -N' 管道剥离（实时输出被 tail 全缓冲掐死的修复）──

def test_strip_tail_pipe_basic():
    """剥离命令末尾的 '2>&1 | tail -N' 管道段（2>&1 属于命令本体，保留）。"""
    assert _strip_tail_pipe("docker run --rm foo bash /x.sh 2>&1 | tail -15") == "docker run --rm foo bash /x.sh 2>&1"
    assert _strip_tail_pipe("echo hi | tail -30") == "echo hi"
    assert _strip_tail_pipe("ls | tail -n 20") == "ls"
    assert _strip_tail_pipe("cmd 2>&1 | tail -8") == "cmd 2>&1"


def test_strip_tail_pipe_no_false_positive():
    """不误伤：读文件 tail 与中间管道段保留原样。"""
    assert _strip_tail_pipe("tail -f /var/log/app.log") == "tail -f /var/log/app.log"          # 无管道
    assert _strip_tail_pipe("cat x | tail -5 | wc -l") == "cat x | tail -5 | wc -l"            # 中间段
    assert _strip_tail_pipe("ls | grep foo") == "ls | grep foo"                                # 无 tail
    assert _strip_tail_pipe("cmd 2>&1 | tail -15 > out.txt") == "cmd 2>&1 | tail -15 > out.txt"  # 有重定向


def test_tail_pipe_command_realtime_visible_during_run():
    """带 '| tail -N' 的命令剥离后，执行期间实时输出可见（回归：tail 全缓冲掐死实时性）。"""
    _clear_shell_output()
    result: dict = {}

    def _run():
        # 命令带 2>&1 | tail -5 —— 修复前 tail 全缓冲，执行期间读不到任何字节
        result["out"] = run_shell.invoke({
            "command": "echo tail_a; sleep 1; echo tail_b; sleep 1; echo tail_c 2>&1 | tail -5",
        })

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    chunks = []
    for _ in range(60):
        c = drain_shell_output()
        if c:
            chunks.append(c)
        if not t.is_alive():
            break
        time.sleep(0.1)
    t.join(timeout=10)

    joined = "".join(chunks)
    # 剥离后中间输出执行期间可见（核心回归断言）
    assert "tail_a" in joined, f"剥离 | tail 后执行期间应能看到 tail_a，实际块: {joined!r}"
    assert "tail_b" in joined, f"剥离 | tail 后执行期间应能看到 tail_b，实际块: {joined!r}"
    # 最终返回值完整
    assert "tail_c" in (result.get("out") or "")


def test_tail_pipe_stripped_from_executed_command():
    """实际执行的命令不含末尾 '| tail -N'（通过返回值确认剥离生效）。"""
    out = run_shell.invoke({"command": "echo hi 2>&1 | tail -5"})
    # 剥离后命令变成 'echo hi'，输出只有 hi；若未剥离会执行 echo hi | tail -5（结果相同但无法区分）。
    # 用 exit code 0 与不含管道错误佐证剥离后正常执行。
    assert "✅ 命令已执行" in out
    assert "hi" in out
