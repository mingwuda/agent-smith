"""MCP 工具状态查询接口"""
from fastapi import APIRouter, Request

from logger import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["mcp"])


@router.get("/mcp/status")
async def mcp_status(request: Request):
    """返回当前 MCP 连接状态列表（供前端右上角角标下拉框展示）。

    每条记录包含：name / status(connecting|connected|failed|skipped) /
    tool_count / error / source(global|project)。
    """
    try:
        from tools.mcp_tools import get_mcp_status
        servers = get_mcp_status()
    except Exception:
        logger.exception("获取 MCP 状态失败")
        servers = []
    connected = sum(1 for s in servers if s.get("status") == "connected")
    return {
        "servers": servers,
        "total": len(servers),
        "connected": connected,
    }
