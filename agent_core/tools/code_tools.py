"""代码执行工具"""
import sys
from io import StringIO
from langchain_core.tools import tool


MAX_PYTHON_OUTPUT_CHARS = 20000
PYTHON_OUTPUT_HEAD_CHARS = 8000
PYTHON_OUTPUT_TAIL_CHARS = 8000


@tool
def run_python(code: str) -> str:
    """执行 Python 代码并返回 stdout 输出。对于计算、数据分析、脚本测试非常有用。"""
    old_stdout = sys.stdout
    sys.stdout = captured = StringIO()
    try:
        exec(code, {"__builtins__": __builtins__})
    except Exception as e:
        return f"❌ 执行出错: {type(e).__name__}: {e}"
    finally:
        sys.stdout = old_stdout
    output = captured.getvalue()
    if not output:
        return "（代码执行完成，无输出）"
    if len(output) <= MAX_PYTHON_OUTPUT_CHARS:
        return output
    return (
        "⚠️ Python 输出较大，未将完整日志放入模型上下文。\n"
        f"输出字符数: {len(output)}\n"
        "请将需要长期保存的日志写入工作区文件，或针对关键片段继续分析。\n\n"
        f"--- 输出开头 {PYTHON_OUTPUT_HEAD_CHARS} 字符 ---\n"
        f"{output[:PYTHON_OUTPUT_HEAD_CHARS]}\n\n"
        f"--- 输出结尾 {PYTHON_OUTPUT_TAIL_CHARS} 字符 ---\n"
        f"{output[-PYTHON_OUTPUT_TAIL_CHARS:]}"
    )


TOOLS = [run_python]
