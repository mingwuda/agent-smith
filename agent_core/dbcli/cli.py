"""dbcli 命令行工具

用法：
  python -m dbcli.cli query "SELECT * FROM orders" --conn prod_db --role analyst
  python -m dbcli.cli schema --conn prod_db
  python -m dbcli.cli connect --list
  python -m dbcli.cli config connections
  python -m dbcli.cli config permissions
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _ensure_path():
    """确保 agent_core 在 sys.path 中"""
    agent_core = Path(__file__).resolve().parent.parent
    if str(agent_core) not in sys.path:
        sys.path.insert(0, str(agent_core))


_ensure_path()

import click

from dbcli.query import execute_query, QueryResult
from dbcli.schema import get_schema, list_tables, format_schema_for_llm
from dbcli.connection import ConnectionPool
from dbcli.config import get_db_configs, get_permission_config, save_db_configs, save_permission_config, DatabaseConfig


@click.group()
@click.version_option(version="0.1.1", prog_name="dbcli")
def cli():
    """数据库交互 CLI 工具 —— 自然语言与数据库对话的中间层"""
    pass


@cli.command()
@click.argument("sql")
@click.option("--conn", "-c", default="local_sqlite", help="数据库连接名")
@click.option("--role", "-r", default="", help="用户角色")
@click.option("--json-output", "-j", is_flag=True, help="以 JSON 格式输出")
@click.option("--max-rows", "-n", default=50, help="最大显示行数")
def query(sql: str, conn: str, role: str, json_output: bool, max_rows: int):
    """执行 SQL 查询（含权限检查）"""
    result = execute_query(sql, connection_name=conn, role=role, max_display_rows=max_rows)

    if json_output:
        click.echo(result.to_json())
    else:
        click.echo(result.to_markdown(max_display_rows=max_rows))

    if not result.success:
        sys.exit(1)


@cli.command()
@click.option("--conn", "-c", default="local_sqlite", help="数据库连接名")
@click.option("--table", "-t", default=None, help="指定表名（不指定则全部）")
@click.option("--json-output", "-j", is_flag=True, help="以 JSON 格式输出")
@click.option("--llm", is_flag=True, help="输出 LLM 友好格式")
def schema(conn: str, table: str, json_output: bool, llm: bool):
    """查看表结构"""
    if llm:
        output = format_schema_for_llm(conn, table)
        click.echo(output)
        return

    tables = get_schema(conn, table)
    if json_output:
        data = [
            {
                "name": t.name,
                "columns": [
                    {
                        "name": c.name,
                        "type": c.type,
                        "nullable": c.nullable,
                        "primary_key": c.primary_key,
                    }
                    for c in t.columns
                ],
                "row_count_estimate": t.row_count_estimate,
                "comment": t.comment,
            }
            for t in tables
        ]
        click.echo(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        for t in tables:
            click.echo(f"\n📊 {t.name} (~{t.row_count_estimate} 行)")
            for c in t.columns:
                pk = " 🔑" if c.primary_key else ""
                click.echo(f"  {c.name}: {c.type}{pk}")


@cli.command()
@click.option("--conn", "-c", default="local_sqlite", help="数据库连接名")
def tables(conn: str):
    """列出所有表"""
    names = list_tables(conn)
    for n in names:
        click.echo(n)
    click.echo(f"\n共 {len(names)} 个表")


@cli.group()
def connect():
    """数据库连接管理"""
    pass


@connect.command("list")
def connect_list():
    """列出所有配置的数据库连接"""
    configs = get_db_configs()
    for c in configs:
        status = "✓" if c.enabled else "✗"
        click.echo(f"  [{status}] {c.name} ({c.db_type})")
        if c.db_type == "sqlite":
            click.echo(f"         path: {c.path}")
        else:
            click.echo(f"         {c.host}:{c.port}/{c.database}")


@connect.command("test")
@click.argument("name")
def connect_test(name: str):
    """测试数据库连接"""
    result = ConnectionPool.test_connection(name)
    if result["ok"]:
        click.echo(f"✓ 连接 {name} 正常")
    else:
        click.echo(f"✗ 连接 {name} 失败: {result['error']}")
        sys.exit(1)


@connect.command("add")
@click.option("--name", "-n", required=True, help="连接名")
@click.option("--type", "-t", "db_type", default="sqlite", help="数据库类型 (sqlite|postgresql|mysql)")
@click.option("--path", default="", help="SQLite 文件路径")
@click.option("--host", default="", help="主机地址")
@click.option("--port", default=0, type=int, help="端口")
@click.option("--database", "-d", default="", help="数据库名")
@click.option("--username", "-u", default="", help="用户名")
@click.option("--password", "-p", default="", help="密码")
def connect_add(name: str, db_type: str, path: str, host: str, port: int,
                database: str, username: str, password: str):
    """添加数据库连接配置"""
    configs = get_db_configs()
    # 检查是否已存在同名连接
    existing = [c for c in configs if c.name == name]
    if existing:
        configs.remove(existing[0])

    config = DatabaseConfig(
        name=name,
        db_type=db_type,
        path=path,
        host=host,
        port=port,
        database=database,
        username=username,
        password=password,
    )
    configs.append(config)
    save_db_configs(configs)
    click.echo(f"✓ 已添加连接: {name}")
    ConnectionPool.reload(name)


@connect.command("remove")
@click.argument("name")
def connect_remove(name: str):
    """移除数据库连接配置"""
    configs = get_db_configs()
    configs = [c for c in configs if c.name != name]
    save_db_configs(configs)
    ConnectionPool.remove(name)
    click.echo(f"✓ 已移除连接: {name}")


@cli.group()
def config():
    """查看和编辑配置"""
    pass


@config.command("connections")
def config_connections():
    """查看数据库连接配置（完整信息，密码已隐藏）"""
    configs = get_db_configs()
    for c in configs:
        info = c.to_dict()
        if info.get("password"):
            info["password"] = "***"
        click.echo(json.dumps(info, ensure_ascii=False, indent=2))


@config.command("permissions")
def config_permissions():
    """查看权限配置"""
    perm = get_permission_config()
    # 简化序列化
    output = {"global_defaults": perm.global_defaults, "roles": {}}
    for role_name, role in perm.roles.items():
        output["roles"][role_name] = {
            "databases": {
                db: [{"table": t.table, "columns_allow": t.columns_allow,
                      "row_filter": t.row_filter, "allow_write": t.allow_write,
                      "max_rows": t.max_rows}
                     for t in tables]
                for db, tables in role.databases.items()
            }
        }
    click.echo(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    cli()
