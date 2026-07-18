#!/usr/bin/env python3
"""在远程机器上执行，更新 auth.json 添加 test 用户"""
import json
import os
import secrets
import string
from pathlib import Path

path = Path.home() / ".desktop_agent" / "auth.json"

data = path.read_text(encoding="utf-8")
config = json.loads(data)

# 兼容新旧格式
if "users" in config:
    users = config["users"]
else:
    users = {}
    old_user = config.get("username")
    old_pwd = config.get("password")
    if old_user and old_pwd:
        users[old_user] = old_pwd


def _resolve_password(env_var: str) -> str:
    """优先用环境变量传入的密码；缺失则生成 16 位强随机密码。

    注意：密码不得硬编码在脚本或版本库中，避免密钥泄露。
    """
    pw = os.environ.get(env_var)
    if pw:
        return pw
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(16))


# 保留 admin 原密码（环境变量或随机生成），新增 test
users["admin"] = _resolve_password("ADMIN_PASSWORD")
users["test"] = _resolve_password("TEST_PASSWORD")
config["users"] = users

# 清理旧单用户字段
config.pop("username", None)
config.pop("password", None)

path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
print("✅ auth.json 已更新")
print(f"   路径: {path}")
print(f"   用户: admin  (密码来自 $ADMIN_PASSWORD 或随机生成，见下方)")
print(f"   用户: test  (密码来自 $TEST_PASSWORD 或随机生成，见下方)")
print(f"   admin 密码: {users['admin']}   # 仅此显示一次，请妥善保存")
print(f"   test  密码: {users['test']}")
