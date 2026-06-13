"""数据库 Schema 自省

获取表结构、列信息、索引等元数据，供 Agent 生成 SQL 时参考。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import inspect, text

from dbcli.connection import ConnectionPool


@dataclass
class ColumnInfo:
    """列信息"""
    name: str
    type: str
    nullable: bool = True
    default: Optional[str] = None
    primary_key: bool = False
    comment: str = ""


@dataclass
class TableInfo:
    """表信息"""
    name: str
    columns: list[ColumnInfo] = field(default_factory=list)
    row_count_estimate: int = 0
    comment: str = ""


def get_schema(connection_name: str, table_name: Optional[str] = None) -> list[TableInfo]:
    """获取数据库表结构

    Args:
        connection_name: 数据库连接名
        table_name: 指定表名（None 则返回所有表）

    Returns:
        表信息列表
    """
    engine = ConnectionPool.get(connection_name)
    insp = inspect(engine)

    tables = [table_name] if table_name else insp.get_table_names()
    result = []

    for t in tables:
        try:
            cols = insp.get_columns(t)
        except Exception:
            continue

        column_infos = []
        for c in cols:
            col_type = str(c.get("type", "unknown"))
            column_infos.append(ColumnInfo(
                name=c["name"],
                type=col_type,
                nullable=c.get("nullable", True),
                default=str(c.get("default")) if c.get("default") is not None else None,
                primary_key=c.get("primary_key", False),
                comment=c.get("comment", ""),
            ))

        # 估算行数
        row_count = 0
        try:
            with engine.connect() as conn:
                r = conn.execute(text(f'SELECT COUNT(*) FROM "{t}"'))
                row_count = r.scalar()
        except Exception:
            pass

        table_comment = ""
        try:
            table_comment = insp.get_table_comment(t).get("text", "")
        except Exception:
            pass

        result.append(TableInfo(
            name=t,
            columns=column_infos,
            row_count_estimate=row_count,
            comment=table_comment or "",
        ))

    return result


def list_tables(connection_name: str) -> list[str]:
    """列出数据库中所有表名"""
    engine = ConnectionPool.get(connection_name)
    insp = inspect(engine)
    return insp.get_table_names()


def format_schema_for_llm(connection_name: str, table_name: Optional[str] = None) -> str:
    """将表结构格式化为 LLM 友好的文本，方便生成 SQL"""
    tables = get_schema(connection_name, table_name)
    if not tables:
        return "数据库中没有找到表。"

    lines = []
    for t in tables:
        pk_cols = [c.name for c in t.columns if c.primary_key]
        lines.append(f"\n## 表: {t.name}")
        if t.comment:
            lines.append(f"  描述: {t.comment}")
        lines.append(f"  行数: ~{t.row_count_estimate}")
        lines.append(f"  主键: {', '.join(pk_cols) if pk_cols else '无'}")
        lines.append("  列:")
        for c in t.columns:
            flags = []
            if c.primary_key:
                flags.append("PK")
            if not c.nullable:
                flags.append("NOT NULL")
            flag_str = f" ({', '.join(flags)})" if flags else ""
            default_str = f" [默认: {c.default}]" if c.default else ""
            comment_str = f" -- {c.comment}" if c.comment else ""
            lines.append(f"    - {c.name}: {c.type}{flag_str}{default_str}{comment_str}")

    return "\n".join(lines)
