#!/usr/bin/env python3
"""调试：验证 generate_login_url.py 生成的 token 能否被服务端正确验签"""
import hashlib, hmac, time, json, urllib.parse
from pathlib import Path

auth_file = Path.home() / ".desktop_agent" / "auth.json"
data = json.loads(auth_file.read_text(encoding="utf-8"))
secret = data["secret"]
username = data.get("username", "admin")
print(f"✅ auth.json 读取成功")
print(f"   username: {username}")
print(f"   secret: {secret[:8]}...{secret[-8:]}")

# 模拟 generate_login_url.py 生成 token
expires_in = 3600
expires_at = int(time.time()) + expires_in
payload = f"{username}:{expires_at}"
token_sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
token = f"{payload}:{token_sig}"

print(f"\n=== 生成 token ===")
print(f"expires_at: {expires_at} (当前: {int(time.time())})")
print(f"payload: {payload}")
print(f"signature: {token_sig}")
print(f"Token: {token}")

# 模拟服务端 _verify_session
print(f"\n=== 服务端验证 ===")
parts = token.split(":")
print(f"split(':') 得到 {len(parts)} 个部分 → {'OK' if len(parts) == 3 else '❌ 失败!'}")

u, e, s = parts
print(f"用户名: {u}")
try:
    e_int = int(e)
    print(f"过期时间: {e_int} {'✅ 未过期' if e_int > int(time.time()) else '❌ 已过期!'}")
except ValueError:
    print(f"过期时间解析失败! ❌")

# 重新计算期望签名
expected = _sign_session = hmac.new(
    secret.encode("utf-8"),
    f"{u}:{e}".encode("utf-8"),
    hashlib.sha256,
).hexdigest()
print(f"期望签名: {expected}")
print(f"实际签名: {s}")
sig_ok = hmac.compare_digest(s, expected)
print(f"签名匹配: {'✅' if sig_ok else '❌'}")

user_ok = hmac.compare_digest(u, username)
print(f"用户名匹配: {'✅' if user_ok else '❌'}")

final = sig_ok and user_ok
print(f"\n{'✅ 验证通过! token 有效!' if final else '❌ 验证失败!'}")

# URL 编码情况
print(f"\n=== 最终 URL ===")
encoded = urllib.parse.quote(token, safe="")
url = f"http://127.0.0.1:8899/auth/token-login?token={encoded}"
print(f"URL: {url}")
print(f"\nURL 中 token 参数:")
print(f"  {encoded[:50]}...{encoded[-20:]}")
print(f"  URL decode 后: {urllib.parse.unquote(encoded)}")
