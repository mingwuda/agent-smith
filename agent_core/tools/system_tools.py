"""系统信息工具"""
import platform
import shutil
from pathlib import Path
from langchain_core.tools import tool

from skills.registry import get_registry


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


@tool
def list_loaded_skills() -> str:
    """列出当前已加载的 Skills，包括名称、描述、触发词、来源和是否声明 MCP。用户询问有哪些技能时优先使用。"""
    skills = sorted(get_registry().list_all(), key=lambda item: item.name)
    if not skills:
        return "当前没有加载任何 Skills。"
    lines = [f"当前已加载 {len(skills)} 个 Skills："]
    for skill in skills:
        triggers = "、".join(skill.triggers[:8]) if skill.triggers else "未声明"
        mcp_note = "，声明了 MCP（当前仅识别不执行）" if "mcp" in skill.metadata else ""
        lines.append(
            f"- {skill.name}: {skill.description or '无描述'}"
            f"；触发词：{triggers}；来源：{skill.root}{mcp_note}"
        )
    return "\n".join(lines)


TOOLS = [get_system_info, list_loaded_skills]
