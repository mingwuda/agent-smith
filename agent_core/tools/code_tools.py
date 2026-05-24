"""代码执行工具"""
import sys
from io import StringIO
from langchain_core.tools import tool


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
    return output if output else "（代码执行完成，无输出）"


TOOLS = [run_python]
