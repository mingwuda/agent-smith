"""回归测试：stream_run 的 tool_end 分支必须无条件初始化 is_error。

背景（2026-08-02 线上事故）：perf 提交 1e0fe5b 误删 on_tool_end 里
    is_error = bool(output_str.strip().startswith("❌"))
这一行无条件初始化，只剩条件分支（工具输出含 __DIFF__: marker 时）内赋值；
而 tool_result 事件的 "error": is_error 与 TOOL_END 日志是无条件读取。
工具输出不含 marker 时读取未赋值变量 → UnboundLocalError，
整个 stream_run 崩溃（前端每个工具调用后全线飘红）。

本测试是源码级哨兵：断言 tool_end 分支在 _record_tool_call 锚点之前存在
「缩进不深于锚点」的 is_error 赋值（即无条件执行，不在 if/for/try 块内）。
谁再删初始化行 / 把赋值挪进条件分支，CI 立刻红。

运行：python -m pytest tests/test_stream_run_is_error_init.py -q
"""
import inspect
import re

from agent_core.main import app  # noqa: F401  触发 sys.path 注入（同 test_tool_history_integrity）
from agent_core.agent_run import AgentRunMixin


def _stream_run_source() -> str:
    return inspect.getsource(AgentRunMixin.stream_run)


def test_tool_end_branch_initializes_is_error_unconditionally():
    src = _stream_run_source()
    lines = src.splitlines()

    # 锚点：tool_end 分支中无条件记录工具调用（紧跟在 is_error 初始化之后、读取点之前）
    anchor_idx = next(
        i for i, ln in enumerate(lines)
        if "_record_tool_call(tool_name, thread_id=tid)" in ln
    )
    anchor_indent = len(lines[anchor_idx]) - len(lines[anchor_idx].lstrip())

    # 锚点之前最近的 is_error 赋值
    assign_idx = None
    for i in range(anchor_idx - 1, -1, -1):
        if re.search(r"\bis_error\s*=", lines[i]):
            assign_idx = i
            break
    assert assign_idx is not None, (
        "tool_end 分支在 _record_tool_call 前应存在 is_error 初始化"
    )

    assign_indent = len(lines[assign_idx]) - len(lines[assign_idx].lstrip())
    # 初始化必须与锚点同级或更浅（无条件执行）；缩进更深说明藏在 if/for/try 里，
    # 工具输出不含 marker 时不会执行 → 复现 UnboundLocalError
    assert assign_indent <= anchor_indent, (
        f"is_error 初始化缩进({assign_indent})深于锚点({anchor_indent})，"
        "疑似条件赋值，会复现 UnboundLocalError"
    )

    # 无条件读取点必须存在（读取方依赖上面的初始化）
    assert '"error": is_error' in src, "tool_result 事件的 error 字段应读取 is_error"
