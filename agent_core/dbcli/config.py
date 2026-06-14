"""数据库连接与权限配置管理

配置存储于 ~/.desktop_agent/dbcli/ 目录：
  - dbcli/connections.yaml  数据库连接配置
  - dbcli/permissions.yaml  权限规则配置

预留 UI 管理接口：所有配置的读写通过本模块的 get/save 函数，
后续 API 端点直接调用这些函数即可。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import yaml

# ── 配置目录 ──
CONFIG_DIR = Path.home() / ".desktop_agent" / "dbcli"


def _ensure_dir():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


# ═══════════════════════════════════════════════════
# 数据库连接配置
# ═══════════════════════════════════════════════════

@dataclass
class DatabaseConfig:
    """单个数据库连接配置

    由 UI 或 YAML 配置生成，供 ConnectionPool 使用。
    """
    name: str                                      # 连接别名，如 "prod_db" / "analytics"
    db_type: str = "sqlite"                        # sqlite | postgresql | mysql
    # SQLite
    path: str = ""                                 # SQLite 文件路径
    # PostgreSQL / MySQL
    host: str = ""
    port: int = 0
    database: str = ""
    username: str = ""
    password: str = ""
    # 通用
    extra_params: dict = field(default_factory=dict)  # 额外连接参数
    readonly: bool = True                           # 默认只读模式
    enabled: bool = True                            # 是否启用

    @property
    def connection_string(self) -> str:
        """生成 SQLAlchemy 连接字符串"""
        if self.db_type == "sqlite":
            return f"sqlite:///{self.path}"
        elif self.db_type == "postgresql":
            pwd = f":{self.password}" if self.password else ""
            port = f":{self.port}" if self.port else ""
            return f"postgresql://{self.username}{pwd}@{self.host}{port}/{self.database}"
        elif self.db_type == "mysql":
            pwd = f":{self.password}" if self.password else ""
            port = f":{self.port}" if self.port else ""
            return f"mysql+pymysql://{self.username}{pwd}@{self.host}{port}/{self.database}"
        raise ValueError(f"不支持的数据库类型: {self.db_type}")

    def to_dict(self) -> dict:
        d = asdict(self)
        # 不暴露密码到前端（除非显式请求）
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "DatabaseConfig":
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


def get_db_configs() -> list[DatabaseConfig]:
    """读取所有数据库连接配置"""
    _ensure_dir()
    config_file = CONFIG_DIR / "connections.yaml"
    if not config_file.exists():
        return _default_connections()
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        connections = data.get("connections", [])
        return [DatabaseConfig.from_dict(c) for c in connections]
    except Exception:
        return _default_connections()


def save_db_configs(configs: list[DatabaseConfig], default_connection: str = "") -> None:
    """保存数据库连接配置（供 UI 管理接口调用）"""
    _ensure_dir()
    config_file = CONFIG_DIR / "connections.yaml"
    data = {"connections": [c.to_dict() for c in configs]}
    if default_connection:
        # 检查默认连接是否存在
        names = {c.name for c in configs}
        if default_connection in names:
            data["default_connection"] = default_connection
    with open(config_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _default_connections() -> list[DatabaseConfig]:
    """默认连接（项目内置 SQLite 示例）"""
    return [
        DatabaseConfig(
            name="local_sqlite",
            db_type="sqlite",
            path=str(Path.home() / ".desktop_agent" / "data.db"),
            readonly=False,
        )
    ]


def get_default_connection() -> str:
    """读取默认数据库连接名"""
    _ensure_dir()
    config_file = CONFIG_DIR / "connections.yaml"
    if not config_file.exists():
        return "local_sqlite"
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        default = data.get("default_connection", "")
        # 验证默认连接是否存在
        if default:
            configs = get_db_configs()
            if any(c.name == default for c in configs):
                return default
        return "local_sqlite"
    except Exception:
        return "local_sqlite"


# ═══════════════════════════════════════════════════
# 权限配置
# ═══════════════════════════════════════════════════

@dataclass
class TablePermission:
    """单表权限规则"""
    table: str                                     # 表名
    columns_allow: list[str] = field(default_factory=list)  # 允许的列（空=全部）
    columns_deny: list[str] = field(default_factory=list)   # 拒绝的列
    row_filter: str = ""                           # 行级过滤模板，如 "dept_id = {{user.dept_id}}"
    allow_write: bool = False                      # 是否允许写操作
    max_rows: int = 1000                           # 单次查询最大行数


@dataclass
class UserPermission:
    """用户权限定义（用户白名单，优先于角色权限）"""
    user_id: str                                  # 用户ID
    databases: dict[str, list[TablePermission]] = field(default_factory=dict)
    # databases: { "prod_db": [TablePermission, ...], "analytics": [...] }


@dataclass
class RolePermission:
    """角色权限定义"""
    role: str                                      # 角色名
    databases: dict[str, list[TablePermission]] = field(default_factory=dict)
    # databases: { "prod_db": [TablePermission, ...], "analytics": [...] }


@dataclass
class PermissionConfig:
    """权限配置总结构"""
    roles: dict[str, RolePermission] = field(default_factory=dict)
    users: dict[str, UserPermission] = field(default_factory=dict)
    global_defaults: dict = field(default_factory=lambda: {
        "max_query_rows": 500,
        "default_readonly": True,
        "allow_dangerous_sql": False,
    })


def get_permission_config() -> PermissionConfig:
    """读取权限配置（优先读用户配置，不存在时回退到包内模板）"""
    _ensure_dir()
    config_file = CONFIG_DIR / "permissions.yaml"

    # 优先：用户自定义配置
    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return _parse_permission_config(data)
        except Exception:
            pass

    # 回退：包内模板（首次运行或用户删除了配置）
    template = Path(__file__).parent / "permissions.yaml"
    if template.exists():
        try:
            with open(template, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            cfg = _parse_permission_config(data)
            # 将模板写入用户目录（供后续编辑）
            save_permission_config(cfg)
            return cfg
        except Exception:
            pass

    return PermissionConfig()


def save_permission_config(config: PermissionConfig) -> None:
    """保存权限配置（供 UI 管理接口调用）"""
    _ensure_dir()
    config_file = CONFIG_DIR / "permissions.yaml"
    data = _serialize_permission_config(config)
    with open(config_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _parse_permission_config(data: dict) -> PermissionConfig:
    """从 YAML 字典解析权限配置"""
    roles = {}
    for role_name, role_data in data.get("roles", {}).items():
        dbs = {}
        for db_name, tables in role_data.get("databases", {}).items():
            dbs[db_name] = [TablePermission(**t) if isinstance(t, dict) else t for t in tables]
        roles[role_name] = RolePermission(role=role_name, databases=dbs)
    users = {}
    for user_key, user_data in data.get("users", {}).items():
        # 支持逗号分隔的多用户 key: "alice,bob" → ["alice", "bob"]
        user_ids = [u.strip() for u in user_key.split(",") if u.strip()]
        dbs = {}
        for db_name, tables in user_data.get("databases", {}).items():
            dbs[db_name] = [TablePermission(**t) if isinstance(t, dict) else t for t in tables]
        for uid in user_ids:
            users[uid] = UserPermission(user_id=uid, databases=dbs)
    return PermissionConfig(
        roles=roles,
        users=users,
        global_defaults=data.get("global_defaults", {}),
    )


def _serialize_permission_config(config: PermissionConfig) -> dict:
    """序列化权限配置到 YAML 字典"""
    roles_out = {}
    for role_name, role in config.roles.items():
        dbs_out = {}
        for db_name, tables in role.databases.items():
            dbs_out[db_name] = [asdict(t) for t in tables]
        roles_out[role_name] = {"databases": dbs_out}
    users_out = {}
    for user_id, user in config.users.items():
        dbs_out = {}
        for db_name, tables in user.databases.items():
            dbs_out[db_name] = [asdict(t) for t in tables]
        users_out[user_id] = {"databases": dbs_out}
    result = {"roles": roles_out}
    if users_out:
        result["users"] = users_out
    result["global_defaults"] = config.global_defaults
    return result
