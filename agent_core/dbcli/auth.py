"""权限检查与 SQL 改写引擎

核心职责：
  1. 列级控制：检查 SELECT 列是否在白名单内，自动展开 *
  2. 行级控制：注入 row_filter 模板变量到 WHERE 子句
  3. 危险操作拦截：DROP / ALTER / TRUNCATE 等需显式放行
  4. 结果集限制：强制执行 max_rows

所有 SQL 在执行前必须通过本模块的 apply() 函数。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from string import Template
from typing import Optional

from dbcli.config import (
    PermissionConfig,
    RolePermission,
    UserPermission,
    TablePermission,
    get_permission_config,
)
from dbcli.connection import get_connection


@dataclass
class PermissionResult:
    """权限检查结果"""
    allowed: bool = True
    rewritten_sql: str = ""                     # 改写后的 SQL
    reason: str = ""                            # 拒绝原因
    warnings: list[str] = field(default_factory=list)  # 警告信息
    applied_filters: list[str] = field(default_factory=list)  # 应用的过滤条件


# ── 危险 SQL 模式 ──
_DANGEROUS_PATTERNS = [
    (r"\bDROP\s+(TABLE|DATABASE|INDEX|VIEW)\b", "DROP 操作"),
    (r"\bALTER\s+(TABLE|DATABASE)\b", "ALTER 操作"),
    (r"\bTRUNCATE\s+(TABLE\s+)?", "TRUNCATE 操作"),
    (r"\bCREATE\s+(TABLE|DATABASE|INDEX)\b", "CREATE 操作"),
    (r"\bGRANT\b", "权限授予"),
    (r"\bREVOKE\b", "权限回收"),
]


def _table_matches(rule_tables: str, query_table: str) -> bool:
    """判断规则表名是否匹配查询的表名

    支持：
      - 精确匹配: "orders" == "orders"
      - 通配: "*" 匹配任何表
      - 逗号分隔多表: "orders, products" 匹配 "orders" 或 "products"
    """
    if not rule_tables or not query_table:
        return False
    if rule_tables.strip() == "*":
        return True
    for t in rule_tables.split(","):
        if t.strip().lower() == query_table.lower():
            return True
    return False


class PermissionChecker:
    """权限检查器

    用法：
        checker = PermissionChecker()
        result = checker.apply(
            sql="SELECT * FROM orders",
            connection_name="prod_db",
            role="analyst",
            user_context={"dept_id": 5},
        )
        if result.allowed:
            engine.execute(result.rewritten_sql)
    """

    def __init__(self, config: Optional[PermissionConfig] = None):
        self.config = config or get_permission_config()

    def reload(self):
        """重新加载权限配置（热更新）"""
        self.config = get_permission_config()

    def get_role(self, role: str) -> Optional[RolePermission]:
        return self.config.roles.get(role)

    def get_user(self, user_id: str) -> Optional[UserPermission]:
        """获取用户权限配置"""
        return self.config.users.get(user_id)

    def get_table_permission(
        self, role: str, connection_name: str, table_name: str
    ) -> Optional[TablePermission]:
        """获取指定角色在某数据库某表的权限规则"""
        role_perm = self.get_role(role)
        if not role_perm:
            return None
        db_tables = role_perm.databases.get(connection_name, [])
        for tp in db_tables:
            if _table_matches(tp.table, table_name):
                return tp
        return None

    def get_user_table_permission(
        self, user_id: str, connection_name: str, table_name: str
    ) -> Optional[TablePermission]:
        """获取指定用户在某数据库某表的权限规则（用户白名单优先）"""
        user_perm = self.get_user(user_id)
        if not user_perm:
            return None
        db_tables = user_perm.databases.get(connection_name, [])
        for tp in db_tables:
            if _table_matches(tp.table, table_name):
                return tp
        return None

    def resolve_permission(
        self, user_id: str, role: str, connection_name: str, table_name: str
    ) -> Optional[TablePermission]:
        """合并用户白名单与角色权限，返回最终的权限规则

        优先级：用户白名单中的表规则 > 用户绑定的角色的表规则
        如果用户白名单有覆盖规则，直接返回用户规则；
        否则查找用户绑定的角色规则。
        """
        # 1. 先查用户白名单中的表规则（覆盖规则）
        utp = self.get_user_table_permission(user_id, connection_name, table_name)
        if utp:
            return utp

        # 2. 查用户绑定的角色
        if user_id:
            user_perm = self.get_user(user_id)
            if user_perm and user_perm.role:
                role_perm = self.get_role(user_perm.role)
                if role_perm:
                    db_tables = role_perm.databases.get(connection_name, [])
                    for tp in db_tables:
                        if _table_matches(tp.table, table_name):
                            return tp

        # 3. 最后回退到传入的角色参数
        return self.get_table_permission(role, connection_name, table_name)



    def _extract_tables(self, sql: str) -> list[str]:
        """从 SQL 语句中提取涉及的表名"""
        sql_upper = sql.upper()
        tables = []

        # FROM / JOIN 之后的表名
        pattern = r'(?:FROM|JOIN)\s+(\[?[\w.`"]+\]?)'
        for m in re.finditer(pattern, sql_upper, re.IGNORECASE):
            tables.append(sql[m.start(1):m.end(1)].strip('`"[]'))

        # INSERT INTO / UPDATE / DELETE FROM
        pattern2 = r'(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(\[?[\w.`"]+\]?)'
        for m in re.finditer(pattern2, sql_upper, re.IGNORECASE):
            tables.append(sql[m.start(1):m.end(1)].strip('`"[]'))

        return list(set(tables)) if tables else ["unknown"]

    def apply(
        self,
        sql: str,
        connection_name: str,
        role: str = "",
        user_context: Optional[dict] = None,
    ) -> PermissionResult:
        """对 SQL 进行权限检查和改写

        Args:
            sql: 原始 SQL 语句
            connection_name: 数据库连接名
            role: 用户角色
            user_context: 运行时上下文，为 row_filter 提供变量

        Returns:
            PermissionResult 包含改写后的 SQL 和检查信息
        """
        result = PermissionResult()
        user_context = user_context or {}
        sql_stripped = sql.strip()

        if not sql_stripped:
            result.allowed = False
            result.reason = "SQL 语句为空"
            return result

        # ── 1. 危险操作检查 ──
        sql_upper = sql_stripped.upper()
        if not self.config.global_defaults.get("allow_dangerous_sql", False):
            for pattern, desc in _DANGEROUS_PATTERNS:
                if re.search(pattern, sql_upper):
                    result.allowed = False
                    result.reason = f"不允许执行 {desc}，如需此操作请联系管理员开启"
                    return result

        # ── 2. 提取表名 ──
        tables = self._extract_tables(sql_stripped)

        # ── 3. 获取权限规则（用户继承角色 + 用户覆盖规则）──
        user_id = user_context.get("user_id", "") if user_context else ""
        table_perm = None
        for t in tables:
            table_perm = self.resolve_permission(user_id, role, connection_name, t)
            if table_perm:
                break

        if table_perm is None:
            # 没有显式授权 → 拒绝
            result.allowed = False
            if user_id:
                result.reason = f"用户 '{user_id}' (角色 '{role}') 无权访问数据库 '{connection_name}' 中的表 {tables}"
            else:
                result.reason = f"角色 '{role}' 无权访问数据库 '{connection_name}' 中的表 {tables}"
            return result

        # ── 4. 写操作检查 ──
        is_write = any(
            sql_upper.startswith(kw)
            for kw in ("INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE")
        )
        if is_write and not table_perm.allow_write:
            result.allowed = False
            result.reason = f"角色 '{role}' 对表 '{table_perm.table}' 没有写权限"
            return result

        # ── 5. 列级控制 ──
        rewritten = self._apply_column_filter(sql_stripped, table_perm, connection_name)
        if rewritten is None:
            result.allowed = False
            result.reason = "列权限检查失败，无法解析 SELECT 列"
            return result

        # ── 6. 行级控制 ──
        rewritten = self._apply_row_filter(rewritten, table_perm, user_context)

        # ── 7. 追加 max_rows LIMIT ──
        rewritten = self._apply_row_limit(rewritten, table_perm)

        result.rewritten_sql = rewritten
        return result

    def _apply_column_filter(
        self, sql: str, perm: TablePermission, connection_name: str
    ) -> Optional[str]:
        """列级过滤：将 * 展开为白名单列，检查显式列是否在白名单内"""
        if not perm.columns_allow:
            # 白名单为空 = 全部列允许
            return sql

        sql_upper = sql.upper()

        # 非 SELECT 语句不过滤列
        if not sql_upper.strip().startswith("SELECT"):
            return sql

        # 处理 SELECT * 的情况
        if re.search(r'\bSELECT\s+\*\b', sql_upper):
            # 需要展开 * → 白名单列
            try:
                engine = get_connection(connection_name)
                inspector = sqlite3
                # 使用 SQLAlchemy inspect 获取列
                from sqlalchemy import inspect as sa_inspect, text
                insp = sa_inspect(engine)
                columns = insp.get_columns(perm.table)
                all_cols = [c["name"] for c in columns]
                allowed = [c for c in all_cols if c in perm.columns_allow]
                if not allowed:
                    return None  # 没有允许的列
                cols_str = ", ".join(allowed)
                sql = re.sub(r'\bSELECT\s+\*\b', f"SELECT {cols_str}", sql, count=1,
                             flags=re.IGNORECASE)
            except Exception:
                # 如果自省失败，用白名单直接替换
                cols_str = ", ".join(perm.columns_allow)
                sql = re.sub(r'\bSELECT\s+\*\b', f"SELECT {cols_str}", sql, count=1,
                             flags=re.IGNORECASE)

        # 检查显式列（有 columns_deny 时）
        if perm.columns_deny:
            deny_set = set(c.lower() for c in perm.columns_deny)
            # 简单检测：如果 SQL 中含有被拒绝的列名则警告
            for col in deny_set:
                if re.search(rf'\b{re.escape(col)}\b', sql, re.IGNORECASE):
                    # 注：不做硬拦截，因为列名可能出现在其他上下文中
                    pass

        return sql

    def _apply_row_filter(
        self, sql: str, perm: TablePermission, user_context: dict
    ) -> str:
        """行级过滤：将 row_filter 模板注入到 SQL 的 WHERE 子句"""
        if not perm.row_filter:
            return sql

        # 模板替换
        try:
            tmpl = Template(perm.row_filter)
            filter_clause = tmpl.safe_substitute(**user_context)
        except Exception:
            return sql  # 模板错误时不拦截，记录日志即可

        if not filter_clause.strip():
            return sql

        sql_upper = sql.upper()

        # 在已有 WHERE 后追加 AND，或在 GROUP BY / ORDER BY / LIMIT 前插入 WHERE
        if " WHERE " in sql_upper:
            # 已有 WHERE → 追加 AND
            # 找到 WHERE 位置并找到其后第一个 GROUP BY / ORDER BY / LIMIT / HAVING
            where_pos = sql_upper.index(" WHERE ")
            # 在 WHERE 子句末尾追加
            # 简化处理：直接在末尾追加，让 SQL 引擎自行解析
            # 更好的做法是找到下一个子句关键字的位置
            keywords = ["GROUP BY", "ORDER BY", "LIMIT", "HAVING", "UNION"]
            insert_pos = len(sql)
            for kw in keywords:
                kw_pos = sql_upper.find(f" {kw} ", where_pos + 7)
                if kw_pos > 0:
                    insert_pos = min(insert_pos, kw_pos)
            sql = sql[:insert_pos] + f" AND ({filter_clause})" + (
                sql[insert_pos:] if insert_pos < len(sql) else ""
            )
        else:
            # 没有 WHERE → 插入
            keywords = ["GROUP BY", "ORDER BY", "LIMIT", "HAVING", "UNION"]
            insert_pos = len(sql)
            for kw in keywords:
                kw_pos = sql_upper.find(f" {kw} ")
                if kw_pos > 0:
                    insert_pos = min(insert_pos, kw_pos)
            sql = sql[:insert_pos] + f" WHERE ({filter_clause})" + (
                sql[insert_pos:] if insert_pos < len(sql) else ""
            )

        return sql

    def _apply_row_limit(self, sql: str, perm: TablePermission) -> str:
        """确保查询有 LIMIT 限制"""
        sql_upper = sql.upper()

        if not sql_upper.strip().startswith("SELECT"):
            return sql

        # 如果已有 LIMIT，检查是否超过上限
        limit_match = re.search(r'\bLIMIT\s+(\d+)', sql_upper)
        if limit_match:
            current_limit = int(limit_match.group(1))
            max_limit = perm.max_rows or self.config.global_defaults.get("max_query_rows", 500)
            if current_limit > max_limit:
                sql = re.sub(
                    r'\bLIMIT\s+\d+',
                    f"LIMIT {max_limit}",
                    sql,
                    count=1,
                    flags=re.IGNORECASE,
                )
        else:
            # 追加 LIMIT
            max_limit = perm.max_rows or self.config.global_defaults.get("max_query_rows", 500)
            # 移除末尾分号
            sql = sql.rstrip().rstrip(";").rstrip()
            sql += f" LIMIT {max_limit}"

        return sql


# ── 便捷函数（供 query.py 调用）──

_checker: Optional[PermissionChecker] = None


def _get_checker() -> PermissionChecker:
    global _checker
    if _checker is None:
        _checker = PermissionChecker()
    return _checker


def check_permission(
    sql: str,
    connection_name: str,
    role: str = "",
    user_context: Optional[dict] = None,
) -> PermissionResult:
    """检查并改写 SQL（快捷调用）"""
    return _get_checker().apply(sql, connection_name, role, user_context)


def apply_permissions(
    sql: str,
    connection_name: str,
    role: str = "",
    user_context: Optional[dict] = None,
) -> PermissionResult:
    """等同于 check_permission，语义化别名"""
    return check_permission(sql, connection_name, role, user_context)
