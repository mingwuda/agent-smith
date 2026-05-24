"""系统信息工具"""
import platform
import shutil
from pathlib import Path
from langchain_core.tools import tool


@tool
def get_system_info() -> str:
    """获取当前系统的基本信息（操作系统、Python 版本、CPU、内存等）"""
    info = [
        f"🖥 系统: {platform.system()} {platform.release()}",
        f"💻 主机名: {platform.node()}",
        f"🐍 Python: {platform.python_version()}",
        f"🏗 架构: {platform.machine()}",
        f"⏰ 当前时间: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    # 磁盘空间
    try:
        usage = shutil.disk_usage(Path.home())
        free_gb = usage.free / (1024**3)
        total_gb = usage.total / (1024**3)
        info.append(f"💾 磁盘: 剩余 {free_gb:.1f}GB / 共 {total_gb:.1f}GB")
    except Exception:
        pass
    return "\n".join(info)


TOOLS = [get_system_info]
