"""pytest 全局数据隔离

把整个测试过程的 HOME 重定向到一次性临时目录，使所有会话 / 记忆 / 认证 /
工作区数据落到临时 HOME 下的 ~/.desktop_agent，绝不触碰真实用户数据
（如 /root/.desktop_agent/users/admin/...）。

必须在本文件顶层、任何 agent_core / user_manager / session_store 导入
之前设置：这些模块在 import 时就用 Path.home() 固化数据目录常量。
pytest 收集测试时会先导入 conftest.py 再导入测试模块，因此时机成立。

测试结束后临时目录由 atexit 钩子清理（不残留 test_xxx 会话 / 用户目录）。
"""
import atexit
import os
import shutil
import tempfile
from pathlib import Path

_TMP_HOME = Path(tempfile.mkdtemp(prefix="desktop_agent_pytest_home_"))
os.environ["HOME"] = str(_TMP_HOME)
os.environ["USERPROFILE"] = str(_TMP_HOME)  # Windows 兼容（Path.home 走 USERPROFILE）

# pytest 不会自动清理 mkdtemp 目录，注册进程退出钩子确保测试完删掉临时数据
atexit.register(shutil.rmtree, _TMP_HOME, ignore_errors=True)
