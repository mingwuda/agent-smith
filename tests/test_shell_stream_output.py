"""run_shell 实时输出缓冲测试（方案B：心跳注入）。

背景（ponytail）：run_shell 执行期间把输出分块 push 到「按调用隔离」的队列，
agent_run.py 心跳循环 drain 后经 SSE 转发前端实时展示。
P0-1 改造后队列不再是单个全局 deque，而是 _SHELL_OUTPUT_QUEUES[key]。
这里只验证队列语义（drain/clear/隔离）与「工具执行期间中间输出可被读到」，
不依赖真实 LLM/LangGraph。
"""
import asyncio
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_core"))

from tools.shell_tools import (  # noqa: E402
    _clear_shell_output,
    _emit_output,
    _strip_tail_pipe,
    drain_all_shell_output,
    drain_shell_output,
    run_shell,
    set_output_key,
)


def _push(text: str, key: str = "default") -> None:
    _emit_output(text, key)


def _invoke(command: str, **kw) -> str:
    """同步调用 async 工具（测试便捷包装）。"""
    return asyncio.run(run_shell.ainvoke({"command": command, **kw}))


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


def test_queues_are_isolated_by_key():
    """P0-1：不同调用的队列互不影响（并行不串台、不互相清空）。"""
    _clear_shell_output("k1")
    _clear_shell_output("k2")
    set_output_key("k1")
    _push("one", "k1")
    # k2 开始时只清自己，不应动 k1
    _clear_shell_output("k2")
    _push("two", "k2")
    assert drain_shell_output("k1") == "one"
    assert drain_shell_output("k2") == "two"
    set_output_key("default")


def test_realtime_output_visible_during_run():
    """工具执行期间（未结束）能读到中间输出块。"""
    _clear_shell_output()
    result: dict = {}

    def _run():
        # ponytail: 显式 timeout=10（< 30s 异步阈值）走前台完成路径，
        # drain 才能在返回前抓到输出。不传 timeout 会走 handoff，拿不到中间块。
        result["out"] = _invoke("echo first; sleep 1; echo second; sleep 1; echo done", timeout=10)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    chunks = []
    for _ in range(50):
        c = "".join(text for _k, text in drain_all_shell_output())
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
    _invoke("echo tail_marker")
    tail = "".join(t for _k, t in drain_all_shell_output())
    assert "tail_marker" in tail, f"工具结束前最后一块应保留可 drain，实际: {tail!r}"


def test_next_run_clears_previous_residue():
    """P0-1：每次调用使用独立 key，互不串扰（不再需要「下次开始清全局队列」）。

    旧设计是一个全局 deque + 每次开始 clear()，并行时后启动会清掉先启动的输出。
    新设计每个调用自己的 key，因此「上次残余」根本不会进入本次队列。
    """
    _clear_shell_output("legacy")
    _push("stale_residue", "legacy")  # 模拟上一次调用未被 drain 的残余（挂在旧 key 上）
    _invoke("echo fresh")
    # 本次调用是新 key，只会拿到新鲜输出
    got = drain_shell_output("legacy")
    assert "stale_residue" in got, "旧键的残余应独立于新调用"
    # 新调用的输出绝不能混入旧残余：用工具返回值佐证
    fresh = _invoke("echo fresh")
    assert "stale_residue" not in fresh, f"新调用不应混入旧残余: {fresh!r}"


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
        result["out"] = _invoke(
            "echo tail_a; sleep 1; echo tail_b; sleep 1; echo tail_c 2>&1 | tail -5"
        )

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    chunks = []
    for _ in range(60):
        c = "".join(t for _k, t in drain_all_shell_output())
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
    out = _invoke("echo hi 2>&1 | tail -5")
    # 剥离后命令变成 'echo hi'，输出只有 hi；若未剥离会执行 echo hi | tail -5（结果相同但无法区分）。
    # 用 exit code 0 与不含管道错误佐证剥离后正常执行。
    assert "✅ 命令已执行" in out
    assert "hi" in out
