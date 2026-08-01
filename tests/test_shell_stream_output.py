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


def test_no_residue_after_run():
    """run_shell 结束后队列被 finally 清空，不残留到下一次调用。"""
    run_shell.invoke({"command": "echo x"})
    assert drain_shell_output() == ""
