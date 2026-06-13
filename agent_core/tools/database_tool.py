"""数据库交互工具 —— Agent Tool

提供三个 LangChain Tool：
  db_schema  - 查看数据库表结构
  db_query   - 执行 SQL 查询（含权限检查）
  db_list    - 列出可用数据库和表

所有工具通过 dbcli 核心库调用，权限检查在 dbcli/auth.py 中统一处理。
"""

from pathlib import Path
from typing import Optional

from langchain_core.tools import tool

from dbcli.query import execute_query, execute_readonly
from dbcli.schema import get_schema, list_tables, format_schema_for_llm
from dbcli.connection import ConnectionPool
from dbcli.config import get_db_configs


# ── 全局上下文（由 Agent 运行时注入） ──
_current_role: str = ""
_current_user_context: dict = {}
_current_connection: str = "local_sqlite"


def set_db_context(role: str = "", user_context: Optional[dict] = None, connection: str = "local_sqlite"):
    """设置当前数据库交互上下文（由 main.py 在请求处理前调用）"""
    global _current_role, _current_user_context, _current_connection
    _current_role = role
    _current_user_context = user_context or {}
    _current_connection = connection


@tool
def db_schema(table_name: str = "") -> str:
    """查看数据库表结构。不传参数则列出所有表名；传表名则返回该表的列、类型、主键等详细信息。
    在编写 SQL 查询之前，建议先调用此工具了解表结构。
    """
    try:
        if not table_name:
            tables = list_tables(_current_connection)
            if not tables:
                return "数据库中没有找到任何表。"
            return "数据库中的表:\n" + "\n".join(f"  - {t}" for t in tables)

        return format_schema_for_llm(_current_connection, table_name)
    except Exception as e:
        return f"❌ 获取表结构失败: {e}"


@tool
def db_query(sql: str) -> str:
    """执行只读 SQL 查询（SELECT），自动经过权限检查。只允许执行 SELECT 语句，
    不支持 INSERT/UPDATE/DELETE 等写操作。返回 Markdown 格式的表格结果。
    建议先调用 db_schema 了解表结构后再编写 SQL。
    """
    try:
        result = execute_readonly(
            sql,
            connection_name=_current_connection,
            role=_current_role,
            user_context=_current_user_context,
        )
        return result.to_markdown()
    except Exception as e:
        return f"❌ 查询执行失败: {e}"


@tool
def db_connections() -> str:
    """列出当前可用的数据库连接。返回所有已配置的数据库连接及其状态。"""
    try:
        configs = get_db_configs()
        if not configs:
            return "没有配置任何数据库连接。"

        lines = ["可用的数据库连接:"]
        for c in configs:
            status = "启用" if c.enabled else "禁用"
            mode = "只读" if c.readonly else "读写"
            if c.db_type == "sqlite":
                detail = c.path
            else:
                detail = f"{c.host}:{c.port}/{c.database}"
            lines.append(f"  - {c.name} ({c.db_type}, {mode}, {status}) → {detail}")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 获取连接列表失败: {e}"


# ── 工具列表（供 main.py 注册） ──
TOOLS = [db_schema, db_query, db_connections]
