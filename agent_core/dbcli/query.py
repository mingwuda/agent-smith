"""SQL 查询执行与结果格式化

所有查询必须经过 auth.apply_permissions() 权限检查。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import CursorResult

from dbcli.auth import apply_permissions
from dbcli.connection import ConnectionPool


@dataclass
class QueryResult:
    """查询结果"""
    success: bool = True
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    row_count: int = 0
    sql_executed: str = ""      # 实际执行的 SQL（改写后）
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "columns": self.columns,
            "rows": self.rows,
            "row_count": self.row_count,
            "sql_executed": self.sql_executed,
            "error": self.error,
            "warnings": self.warnings,
        }

    def to_markdown(self, max_display_rows: int = 50) -> str:
        """将结果格式化为 Markdown 表格"""
        if not self.success:
            return f"❌ 查询失败: {self.error}"

        if not self.columns:
            return "查询成功，但没有返回数据。"

        display_rows = self.rows[:max_display_rows]
        lines = []

        # 表头
        lines.append("| " + " | ".join(self.columns) + " |")
        lines.append("| " + " | ".join(["---"] * len(self.columns)) + " |")

        # 数据行
        for row in display_rows:
            values = [str(v) if v is not None else "NULL" for v in row]
            lines.append("| " + " | ".join(values) + " |")

        if self.row_count > max_display_rows:
            lines.append(f"\n*仅显示前 {max_display_rows} 行，共 {self.row_count} 行*")

        if self.sql_executed:
            lines.append(f"\n```sql\n{self.sql_executed}\n```")

        return "\n".join(lines)

    def to_json(self) -> str:
        """将结果格式化为 JSON 字符串"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def _execute_raw_sql(
    connection_name: str,
    sql: str,
    params: Optional[dict] = None,
) -> tuple[CursorResult, list[str]]:
    """执行原始 SQL（内部使用，不做权限检查）"""
    engine = ConnectionPool.get(connection_name)
    with engine.connect() as conn:
        if sql.strip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
            # 写操作使用事务
            with conn.begin():
                result = conn.execute(text(sql), params or {})
        else:
            result = conn.execute(text(sql), params or {})
        columns = list(result.keys()) if result.returns_rows else []
        return result, columns


def execute_query(
    sql: str,
    connection_name: str = "local_sqlite",
    role: str = "",
    user_context: Optional[dict] = None,
    max_display_rows: int = 50,
) -> QueryResult:
    """执行 SQL 查询（含权限检查）

    这是所有数据库查询的统一入口。

    Args:
        sql: 原始 SQL
        connection_name: 数据库连接名
        role: 用户角色
        user_context: 运行时上下文（用于行级过滤模板）
        max_display_rows: 返回的最大行数（显示用）

    Returns:
        QueryResult 包含结果数据和元信息
    """
    # ── 权限检查 ──
    perm_result = apply_permissions(sql, connection_name, role, user_context)
    if not perm_result.allowed:
        return QueryResult(
            success=False,
            error=perm_result.reason,
            sql_executed=sql,
        )

    rewritten_sql = perm_result.rewritten_sql
    warnings = perm_result.warnings

    # ── 执行 ──
    try:
        result, columns = _execute_raw_sql(connection_name, rewritten_sql)

        if result.returns_rows:
            rows = [list(row) for row in result.fetchall()]
        else:
            rows = []
            warnings.append(f"影响行数: {result.rowcount}" if result.rowcount >= 0 else "操作已执行")

        return QueryResult(
            success=True,
            columns=columns,
            rows=rows[:max_display_rows],
            row_count=result.rowcount if result.returns_rows else 0,
            sql_executed=rewritten_sql,
            warnings=warnings,
        )
    except Exception as e:
        return QueryResult(
            success=False,
            error=str(e),
            sql_executed=rewritten_sql,
            warnings=warnings,
        )


def execute_readonly(
    sql: str,
    connection_name: str = "local_sqlite",
    role: str = "",
    user_context: Optional[dict] = None,
    max_display_rows: int = 50,
) -> QueryResult:
    """执行只读查询（便捷函数，拒绝写操作）"""
    sql_upper = sql.strip().upper()
    if any(sql_upper.startswith(kw) for kw in ("INSERT", "UPDATE", "DELETE", "REPLACE", "DROP", "ALTER", "CREATE")):
        return QueryResult(
            success=False,
            error="只读模式下不允许写操作",
            sql_executed=sql,
        )
    return execute_query(sql, connection_name, role, user_context, max_display_rows)
