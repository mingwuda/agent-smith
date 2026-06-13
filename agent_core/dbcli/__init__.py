"""dbcli —— 数据库交互核心库

三层架构：
  Layer 1  入口层：database_tool.py (Agent工具) / cli.py (终端命令)
  Layer 2  核心层：auth / connection / query / schema
  Layer 3  适配层：SQLite / PostgreSQL / MySQL via SQLAlchemy

公共 API：
  get_connection(name) → Engine
  execute_query(sql, connection_name, role, user_context) → QueryResult
  get_schema(connection_name, table) → list[TableInfo]
  check_permission(sql, table, role, user_context) → PermissionResult
"""

from dbcli.config import (
    DatabaseConfig,
    get_db_configs,
    save_db_configs,
    PermissionConfig,
    get_permission_config,
    save_permission_config,
)
from dbcli.connection import ConnectionPool, get_connection, reset_pool
from dbcli.auth import PermissionChecker, check_permission, apply_permissions
from dbcli.query import QueryResult, execute_query, execute_readonly
from dbcli.schema import TableInfo, ColumnInfo, get_schema, list_tables

__all__ = [
    "DatabaseConfig",
    "get_db_configs",
    "save_db_configs",
    "PermissionConfig",
    "get_permission_config",
    "save_permission_config",
    "ConnectionPool",
    "get_connection",
    "reset_pool",
    "PermissionChecker",
    "check_permission",
    "apply_permissions",
    "QueryResult",
    "execute_query",
    "execute_readonly",
    "TableInfo",
    "ColumnInfo",
    "get_schema",
    "list_tables",
]
