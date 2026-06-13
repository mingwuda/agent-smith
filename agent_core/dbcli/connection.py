"""数据库连接管理

基于 SQLAlchemy 的统一数据库抽象：
  - SQLite / PostgreSQL / MySQL 三种数据库适配
  - 连接池自动管理
  - 支持运行时重载配置（供 UI 管理接口调用）
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Engine, create_engine, text, inspect
from sqlalchemy.pool import QueuePool, StaticPool

from dbcli.config import DatabaseConfig, get_db_configs


# ── 全局连接池 ──
_engines: dict[str, Engine] = {}


class ConnectionPool:
    """数据库连接池管理

    用法：
        pool = ConnectionPool()
        engine = pool.get("prod_db")
        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1"))
    """

    @staticmethod
    def get(name: str) -> Engine:
        """获取指定名称的数据库引擎（不存在则创建）"""
        if name in _engines:
            return _engines[name]

        configs = get_db_configs()
        config = next((c for c in configs if c.name == name), None)
        if config is None:
            raise ValueError(f"未找到数据库连接配置: {name}")

        return ConnectionPool._create_engine(config)

    @staticmethod
    def reload(name: str) -> Engine:
        """重新加载指定连接的配置（热更新）"""
        if name in _engines:
            old = _engines.pop(name)
            old.dispose()
        return ConnectionPool.get(name)

    @staticmethod
    def reload_all() -> None:
        """重新加载所有连接"""
        for name in list(_engines.keys()):
            ConnectionPool.reload(name)

    @staticmethod
    def remove(name: str) -> None:
        """移除并关闭指定连接"""
        if name in _engines:
            _engines.pop(name).dispose()

    @staticmethod
    def list_connections() -> list[str]:
        """列出所有活跃连接名"""
        return list(_engines.keys())

    @staticmethod
    def _create_engine(config: DatabaseConfig) -> Engine:
        """根据 DatabaseConfig 创建 SQLAlchemy Engine"""
        connect_args = {}

        if config.db_type == "sqlite":
            connect_args["check_same_thread"] = False
            poolclass = StaticPool
        else:
            poolclass = QueuePool
            connect_args.update(config.extra_params)

        engine = create_engine(
            config.connection_string,
            poolclass=poolclass,
            pool_size=3,
            max_overflow=5,
            pool_recycle=3600,
            pool_pre_ping=True,          # 自动检测断连
            connect_args=connect_args,
            echo=False,
        )

        _engines[config.name] = engine
        return engine

    @staticmethod
    def test_connection(name: str) -> dict:
        """测试数据库连接是否正常

        Returns:
            {"ok": True} 或 {"ok": False, "error": "..."}
        """
        try:
            engine = ConnectionPool.get(name)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @staticmethod
    def get_table_names(name: str) -> list[str]:
        """获取数据库中所有表名"""
        engine = ConnectionPool.get(name)
        insp = inspect(engine)
        return insp.get_table_names()


# ── 便捷函数 ──

def get_connection(name: str) -> Engine:
    """获取数据库引擎（快捷调用）"""
    return ConnectionPool.get(name)


def reset_pool() -> None:
    """重置所有连接（用于测试或重新配置）"""
    for name in list(_engines.keys()):
        _engines.pop(name).dispose()
