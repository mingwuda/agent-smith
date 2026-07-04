"""数据库路由"""
import json
import os
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import session_store
from api.deps import _get_current_user, _require_admin
from logger import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["db"])


class DBConnectionRequest(BaseModel):
    name: str
    db_type: str = "sqlite"
    path: str = ""
    host: str = ""
    port: int = 0
    database: str = ""
    username: str = ""
    password: str = ""
    readonly: bool = True


class DBQueryRequest(BaseModel):
    sql: str
    connection: str = "local_sqlite"


class PermissionsSaveRequest(BaseModel):
    """权限配置保存请求体（直接接收 JSON，不再嵌套）"""
    global_defaults: dict = {}
    roles: dict = {}
    users: dict = {}


@router.get("/db/default-connection")
def db_get_default_connection(request: Request):
    """获取默认数据库连接名"""
    from dbcli.config import get_default_connection
    return {"default_connection": get_default_connection()}


@router.put("/db/default-connection")
def db_set_default_connection(req: DBConnectionRequest, request: Request):
    """设置默认数据库连接"""
    _require_admin(request)
    from dbcli.config import get_db_configs, save_db_configs
    configs = get_db_configs()
    name = req.name
    if not any(c.name == name for c in configs):
        raise HTTPException(400, f"连接 '{name}' 不存在")
    save_db_configs(configs, default_connection=name)
    return {"status": "ok", "default_connection": name}


@router.get("/db/connections")
def db_list_connections(request: Request):
    """列出所有数据库连接配置（含状态）"""
    _require_admin(request)
    from dbcli.config import get_db_configs
    configs = get_db_configs()
    return [{
        "name": c.name, "db_type": c.db_type, "readonly": c.readonly,
        "enabled": c.enabled,
        "path": c.path, "host": c.host, "port": c.port,
        "database": c.database, "username": c.username,
    } for c in configs]


@router.post("/db/connections")
def db_add_connection(req: DBConnectionRequest, request: Request):
    """添加或更新数据库连接"""
    _require_admin(request)
    from dbcli.config import get_db_configs, save_db_configs, DatabaseConfig
    from dbcli.connection import ConnectionPool
    configs = get_db_configs()
    configs = [c for c in configs if c.name != req.name]
    config = DatabaseConfig(
        name=req.name, db_type=req.db_type, path=req.path,
        host=req.host, port=req.port, database=req.database,
        username=req.username, password=req.password, readonly=req.readonly,
    )
    configs.append(config)
    save_db_configs(configs)
    ConnectionPool.reload(req.name)
    return {"status": "ok", "message": f"已添加/更新连接 {req.name}"}


@router.delete("/db/connections/{name}")
def db_remove_connection(name: str, request: Request):
    """删除数据库连接"""
    _require_admin(request)
    from dbcli.config import get_db_configs, save_db_configs
    from dbcli.connection import ConnectionPool
    configs = get_db_configs()
    configs = [c for c in configs if c.name != name]
    save_db_configs(configs)
    ConnectionPool.remove(name)
    return {"status": "ok", "message": f"已移除连接 {name}"}


@router.post("/db/connections/{name}/test")
def db_test_connection(name: str, request: Request):
    """测试已保存的数据库连接"""
    from dbcli.connection import ConnectionPool
    return ConnectionPool.test_connection(name)


@router.post("/db/test-connection")
def db_test_connection_inline(req: DBConnectionRequest, request: Request):
    """测试未保存的数据库连接（供前端表单预测试用）"""
    try:
        from sqlalchemy import create_engine, text

        if req.db_type == "sqlite":
            if not req.path:
                return {"ok": False, "error": "SQLite 需要指定文件路径"}
            url = f"sqlite:///{req.path}"
        elif req.db_type == "postgresql":
            if not req.host or not req.database:
                return {"ok": False, "error": "PostgreSQL 需要填写主机地址和数据库名"}
            pwd = f":{req.password}" if req.password else ""
            port = f":{req.port}" if req.port else ""
            url = f"postgresql://{req.username}{pwd}@{req.host}{port}/{req.database}"
        elif req.db_type == "mysql":
            if not req.host or not req.database:
                return {"ok": False, "error": "MySQL 需要填写主机地址和数据库名"}
            pwd = f":{req.password}" if req.password else ""
            port = f":{req.port}" if req.port else ""
            url = f"mysql+pymysql://{req.username}{pwd}@{req.host}{port}/{req.database}"
        else:
            return {"ok": False, "error": f"不支持的数据库类型: {req.db_type}"}

        engine = create_engine(url, pool_pre_ping=True)
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        finally:
            engine.dispose()

        return {"ok": True}

    except ImportError as e:
        missing = str(e).replace("No module named ", "").strip("'\" ")
        return {"ok": False, "error": f"缺少数据库驱动: {missing}，请安装对应的 Python 包（参考 requirements.txt）"}
    except Exception as e:
        return {"ok": False, "error": f"连接失败: {type(e).__name__}: {e}"}


@router.get("/db/permissions")
def db_get_permissions(request: Request):
    """获取权限配置"""
    _require_admin(request)
    from dbcli.config import get_permission_config, CONFIG_DIR
    from pathlib import Path
    import yaml
    perm = get_permission_config()
    # 简化返回（不暴露密码等敏感信息）
    output = {"global_defaults": perm.global_defaults, "roles": {}, "users": {}}
    for role_name, role in perm.roles.items():
        output["roles"][role_name] = {"databases": {
            db: [{"table": t.table, "columns_allow": t.columns_allow,
                   "row_filter": t.row_filter, "allow_write": t.allow_write,
                   "max_rows": t.max_rows} for t in tables]
            for db, tables in role.databases.items()
        }}
    for user_id, user in perm.users.items():
        output["users"][user_id] = {"role": user.role, "databases": {
            db: [{"table": t.table, "columns_allow": t.columns_allow,
                   "row_filter": t.row_filter, "allow_write": t.allow_write,
                   "max_rows": t.max_rows} for t in tables]
            for db, tables in user.databases.items()
        }}
    # 返回原始 YAML 文本（供前端编辑器使用）
    yaml_path = CONFIG_DIR / "permissions.yaml"
    if not yaml_path.exists():
        yaml_path = Path(__file__).parent / "dbcli" / "permissions.yaml"
    yaml_text = ""
    if yaml_path.exists():
        try:
            yaml_text = yaml_path.read_text(encoding="utf-8")
        except Exception:
            yaml_text = ""
    output["yaml"] = yaml_text
    return output


@router.put("/db/permissions")
def db_save_permissions(req: PermissionsSaveRequest, request: Request):
    """保存权限配置（后端生成 YAML）"""
    _require_admin(request)
    from dbcli.config import CONFIG_DIR, logger as config_logger
    import yaml
    import traceback
    try:
        logger.info(f"[保存权限] 收到保存请求: roles={len(req.roles or {})}, users={len(req.users or {})}")
        
        # 清理空 key
        roles = {k: v for k, v in (req.roles or {}).items() if k and k.strip()}
        users = {k: v for k, v in (req.users or {}).items() if k and k.strip()}
        data = {
            "global_defaults": req.global_defaults or {},
            "roles": roles,
            "users": users,
        }
        logger.info(f"[保存权限] 清理后数据: roles={list(roles.keys())}, users={list(users.keys())}")

        from dbcli.config import _parse_permission_config, _serialize_permission_config_for_yaml
        config = _parse_permission_config(data)
        logger.info(f"[保存权限] 解析配置成功")

        # 用 PyYAML 生成干净 YAML
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        yaml_path = CONFIG_DIR / "permissions.yaml"
        clean_data = _serialize_permission_config_for_yaml(config)
        logger.info(f"[保存权限] 序列化后的数据: {clean_data.keys()}")
        
        yaml_content = yaml.safe_dump(clean_data, allow_unicode=True, default_flow_style=False, sort_keys=False)
        logger.info(f"[保存权限] YAML内容预览:\n{yaml_content[:500]}")
        
        with open(yaml_path, "w", encoding="utf-8") as f:
            f.write(yaml_content)
        logger.info(f"[保存权限] YAML已写入: {yaml_path}")

        # 热更新权限检查器
        from dbcli.auth import reload_checker
        reload_checker()
        logger.info(f"[保存权限] 权限检查器已热更新")

        return {"status": "ok", "message": "权限配置已保存"}
    except Exception as e:
        error_detail = traceback.format_exc()
        logger.error(f"[保存权限] 保存失败: {e}\n{error_detail}")
        raise HTTPException(400, f"权限配置格式错误: {e}")


@router.post("/db/query")
def db_execute_query(req: DBQueryRequest, request: Request):
    """执行 SQL 查询（含权限检查，供 UI 管理界面使用）"""
    from dbcli.query import execute_query
    uid = _get_current_user(request)
    from user_manager import get_user
    user = get_user(uid) or {}
    role = user.get("role", "")
    user_context = {"user_id": uid, "role": role}
    result = execute_query(req.sql, connection_name=req.connection,
                       role=role, user_context=user_context)
    return result.to_dict()


@router.get("/db/schema/{connection_name}")
def db_get_schema(connection_name: str, table: str = "", request: Request = None):
    """获取数据库 schema（供 UI 管理界面自动补全）"""
    _require_admin(request)
    from dbcli.schema import get_schema
    tables = get_schema(connection_name, table or None)
    return [{"name": t.name, "columns": [
        {"name": c.name, "type": c.type, "primary_key": c.primary_key}
        for c in t.columns
    ]} for t in tables]
