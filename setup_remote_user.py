#!/usr/bin/env python3
"""在远程机器上执行，更新 auth.json 添加 test 用户"""
import json
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

# 保留 admin 原密码，新增 test
users["admin"] = "oUEP-WDzX-OE3a80Wy8yPKI"
users["test"] = "test123"
config["users"] = users

# 清理旧单用户字段
config.pop("username", None)
config.pop("password", None)

path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
print("✅ auth.json 已更新")
print(f"   路径: {path}")
print(f"   用户: admin / oUEP-WDzX-OE3a80Wy8yPKI")
print(f"   用户: test  / test123")
