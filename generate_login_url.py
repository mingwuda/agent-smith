#!/usr/bin/env python3
"""生成免密码登录链接（URL Token 登录）

用法:
    python generate_login_url.py                          # 使用默认配置
    python generate_login_url.py --host 192.168.1.100     # 指定主机地址
    python generate_login_url.py --port 8899               # 指定端口
    python generate_login_url.py --expires 300             # token 有效期（秒）
    python generate_login_url.py --user test               # 指定登录用户
    python generate_login_url.py --qr                       # 同时输出二维码

生成的链接可直接在浏览器打开，自动登录并跳转到主页。
"""
import argparse
import hashlib
import hmac
import json
import time
import urllib.parse
from pathlib import Path


def load_auth_config() -> dict:
    """从 auth.json 读取多用户认证配置"""
    auth_file = Path.home() / ".desktop_agent" / "auth.json"
    if not auth_file.exists():
        print(f"❌ 未找到认证配置文件: {auth_file}")
        print("   请先启动一次服务以自动生成认证配置")
        raise SystemExit(1)
    data = json.loads(auth_file.read_text(encoding="utf-8"))
    secret = data.get("secret", "")
    if not secret:
        print("❌ auth.json 中缺少 secret 字段")
        raise SystemExit(1)

    users = data.get("users")
    if isinstance(users, dict) and users:
        usernames = sorted(users.keys())
    elif data.get("username"):
        usernames = [data["username"]]
    else:
        print("❌ auth.json 中没有可用用户")
        raise SystemExit(1)

    return {"secret": secret, "users": usernames}


def generate_login_token(secret: str, username: str, expires_in: int) -> str:
    """生成一次性的登录 token（与 session cookie 格式兼容）"""
    expires_at = int(time.time()) + expires_in
    payload = f"{username}:{expires_at}"
    signature = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def try_qr(text: str):
    """尝试在终端输出二维码（需要 qrcode 或 pillow 库）"""
    try:
        import qrcode
        from io import StringIO
        f = StringIO()
        qr = qrcode.QRCode(border=1)
        qr.add_data(text)
        qr.print_ascii(out=f)
        f.seek(0)
        return f.read()
    except ImportError:
        return None


def main():
    parser = argparse.ArgumentParser(description="生成免密码登录链接")
    parser.add_argument("--host", default="127.0.0.1", help="服务主机地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8899, help="服务端口（默认 8899）")
    parser.add_argument("--expires", type=int, default=300, help="token 有效期秒（默认 300 = 5 分钟）")
    parser.add_argument("--user", default="", help="登录用户名；默认使用 auth.json 中的第一个用户")
    parser.add_argument("--qr", action="store_true", help="同时输出二维码")
    parser.add_argument("--copy", action="store_true", help="自动复制到剪贴板")
    args = parser.parse_args()

    auth = load_auth_config()
    secret = auth["secret"]
    username = args.user or auth["users"][0]
    if username not in auth["users"]:
        print(f"❌ 用户不存在: {username}")
        print(f"   可用用户: {', '.join(auth['users'])}")
        raise SystemExit(1)

    token = generate_login_token(secret, username, args.expires)
    encoded_token = urllib.parse.quote(token, safe="")

    login_url = f"http://{args.host}:{args.port}/auth/token-login?token={encoded_token}"

    expiry_min = args.expires / 60
    print(f"🔗 免密登录链接（{expiry_min:.0f} 分钟内有效）:")
    print()
    print(f"   {login_url}")
    print()

    if args.qr:
        qr_text = try_qr(login_url)
        if qr_text:
            print("📱 扫码登录:")
            print()
            print(qr_text)
        else:
            print("ℹ️  提示: 安装 qrcode 库可输出二维码 (pip install qrcode[pil])")

    if args.copy:
        try:
            import pyperclip
            pyperclip.copy(login_url)
            print("✅ 已复制到剪贴板")
        except ImportError:
            print("ℹ️  提示: 安装 pyperclip 库可自动复制 (pip install pyperclip)")


if __name__ == "__main__":
    main()
