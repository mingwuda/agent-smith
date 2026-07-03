"""微信 iLink Bot API 客户端

基于腾讯 iLink 协议（ilinkai.weixin.qq.com）实现微信个人号 Bot。
无需公网 IP，客户端主动长轮询收取消息。
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import uuid
from pathlib import Path
from typing import Optional

import httpx

from logger import get_logger
import session_store

logger = get_logger(__name__)

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"


# ── 纯 Python AES-128-ECB 解密 ──────────────────────────────────
# AES S-box (前向)
_AES_SBOX = [
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
]

# AES 逆 S-box (用于 InvSubBytes)
_AES_INV_SBOX = [0] * 256
for _i, _v in enumerate(_AES_SBOX):
    _AES_INV_SBOX[_v] = _i

def _aes_decrypt_block(block: bytes, expanded_key: list[list[int]]) -> bytes:
    """解密一个 16 字节 AES 块（ECB 模式下逐块调用）。"""
    state = list(block)
    nr = 10

    def inv_sub_bytes(s):
        return [_AES_INV_SBOX[b] for b in s]

    def inv_shift_rows(s):
        return [
            s[0], s[5], s[10], s[15],
            s[4], s[9], s[14], s[3],
            s[8], s[13], s[2], s[7],
            s[12], s[1], s[6], s[11],
        ]

    def inv_mix_columns(s):
        def galois_mul(a, b):
            p = 0
            for _ in range(8):
                if b & 1:
                    p ^= a
                hi = a & 0x80
                a = (a << 1) & 0xFF
                if hi:
                    a ^= 0x1B
                b >>= 1
            return p
        result = [0] * 16
        for i in range(4):
            c = s[i*4:(i+1)*4]
            # InvMixColumns matrix on GF(2^8):
            # [14 11 13  9]   [c0]
            # [ 9 14 11 13] * [c1]
            # [13  9 14 11]   [c2]
            # [11 13  9 14]   [c3]
            result[i*4]   = galois_mul(14, c[0]) ^ galois_mul(11, c[1]) ^ galois_mul(13, c[2]) ^ galois_mul(9, c[3])
            result[i*4+1] = galois_mul(9, c[0])  ^ galois_mul(14, c[1]) ^ galois_mul(11, c[2]) ^ galois_mul(13, c[3])
            result[i*4+2] = galois_mul(13, c[0]) ^ galois_mul(9, c[1])  ^ galois_mul(14, c[2]) ^ galois_mul(11, c[3])
            result[i*4+3] = galois_mul(11, c[0]) ^ galois_mul(13, c[1]) ^ galois_mul(9, c[2])  ^ galois_mul(14, c[3])
        return result

    def add_round_key(s, rk):
        return [s[i] ^ rk[i] for i in range(16)]

    # 初始轮密钥加（使用最后一轮密钥）
    state = add_round_key(state, expanded_key[nr])

    # 解密主循环
    for r in range(nr - 1, 0, -1):
        state = inv_shift_rows(state)
        state = inv_sub_bytes(state)
        state = add_round_key(state, expanded_key[r])
        state = inv_mix_columns(state)

    # 最后一轮（无 InvMixColumns）
    state = inv_shift_rows(state)
    state = inv_sub_bytes(state)
    state = add_round_key(state, expanded_key[0])

    return bytes(state)


def _aes_key_expansion(key: bytes) -> list[list[int]]:
    """AES-128 密钥扩展: 16 字节 → 11 轮密钥 (每轮 16 字节)"""
    rcon = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]
    # 初始 4 个字 (每个字 4 字节)
    w = [list(key[0:4]), list(key[4:8]), list(key[8:12]), list(key[12:16])]
    for i in range(4, 44):
        temp = w[i-1][:]
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]  # RotWord
            temp = [_AES_SBOX[b] for b in temp]  # SubWord
            temp[0] ^= rcon[i//4 - 1]
        w.append([w[i-4][j] ^ temp[j] for j in range(4)])
    # 将 44 个字展平为 11 个轮密钥，每个轮密钥 16 字节
    round_keys = []
    for r in range(11):
        rk = []
        for word in w[r*4:(r+1)*4]:
            rk.extend(word)
        round_keys.append(rk)
    return round_keys


def aes_decrypt_ecb(ciphertext: bytes, key_hex: str) -> bytes:
    """AES-128-ECB 解密

    使用系统 OpenSSL 命令解密。
    如果 OpenSSL 不可用或失败，返回原始数据（不做解密）。

    Args:
        ciphertext: 密文（长度应为 16 的倍数）
        key_hex: 32 字符十六进制密钥

    Returns:
        解密后的明文，或解密失败时的原始数据
    """
    import subprocess
    try:
        result = subprocess.run(
            ['openssl', 'enc', '-d', '-aes-128-ecb', '-K', key_hex.lower(), '-nopad'],
            input=ciphertext,
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
    except Exception:
        pass

    # 解密失败：返回原始数据（调用方会得到无效图片，API 会报错）
    import logging
    logging.getLogger(__name__).warning(
        "[AES] OpenSSL 解密失败，返回原始数据 (%d bytes)", len(ciphertext),
    )
    return ciphertext


# ── WeChat Bot 类 ─────────────────────────────────────────────────


class WeChatBot:
    """微信 iLink Bot API 客户端（按用户隔离）"""

    def __init__(self, agent, user_id: str = "default", data_dir: Optional[str] = None):
        self.agent = agent
        self.user_id = user_id
        self.data_dir = data_dir or str(Path.home() / ".desktop_agent" / f"wechat_{user_id}")
        self.bot_token: Optional[str] = None
        self.bot_base_url: Optional[str] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._update_buf: str = ""
        self._seen_msg_ids: set[str] = set()
        self._wechat_sessions: dict[str, str] = {}  # wx_user_id → current session_id
        # 暂存用户最近发送的图片（key=from_user），等待后续文本合并为图文消息
        self._pending_images: dict[str, dict] = {}

        os.makedirs(self.data_dir, exist_ok=True)

        # ── 迁移旧版 token 到新版路径 ──
        if user_id == "admin":
            old_token_path = Path.home() / ".desktop_agent" / "wechat" / "token.json"
            new_token_path = Path(self.data_dir) / "token.json"
            if old_token_path.exists() and not new_token_path.exists():
                try:
                    new_token_path.parent.mkdir(parents=True, exist_ok=True)
                    new_token_path.write_bytes(old_token_path.read_bytes())
                    logger.info("[微信Bot] 已迁移旧版 token 到 %s", new_token_path)
                except Exception as e:
                    logger.warning("[微信Bot] token 迁移失败: %s", e)

        self._load_token()

    # ── 鉴权 ──────────────────────────────────────

    def _auth_headers(self) -> dict:
        uin = base64.b64encode(
            str(random.randint(0, 0xFFFFFFFF)).encode()
        ).decode()
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": uin,
        }
        if self.bot_token:
            headers["Authorization"] = f"Bearer {self.bot_token}"
        return headers

    def _save_token(self):
        path = Path(self.data_dir) / "token.json"
        path.write_text(
            json.dumps(
                {
                    "bot_token": self.bot_token,
                    "base_url": self.bot_base_url,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _load_token(self):
        path = Path(self.data_dir) / "token.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.bot_token = data.get("bot_token")
                self.bot_base_url = data.get("base_url") or ""
            except Exception:
                pass

    # ── 登录 ──────────────────────────────────────

    async def get_qrcode(self) -> dict:
        """获取登录二维码，返回 {qrcode, qrcode_img_content}"""
        async with httpx.AsyncClient(trust_env=False, verify=False) as client:
            resp = await client.get(
                f"{ILINK_BASE_URL}/ilink/bot/get_bot_qrcode",
                params={"bot_type": "3"},
            )
            return resp.json()

    async def poll_qrcode_status(self, qrcode: str):
        """轮询扫码状态，扫码确认后保存 token"""
        async with httpx.AsyncClient(trust_env=False, verify=False) as client:
            while True:
                resp = await client.get(
                    f"{ILINK_BASE_URL}/ilink/bot/get_qrcode_status",
                    params={"qrcode": qrcode},
                )
                data = resp.json()
                status = data.get("status")
                if status == "confirmed":
                    self.bot_token = data["bot_token"]
                    self.bot_base_url = data.get("baseurl") or ""
                    self._save_token()
                    logger.info("[微信Bot] 扫码登录成功")
                    # 自动启动轮询
                    await self.start()
                    return data
                elif status == "expired":
                    logger.warning("[微信Bot] 二维码已过期")
                    return data
                await asyncio.sleep(1)

    # ── 消息收发 ──────────────────────────────────

    async def poll_messages(self) -> list[dict]:
        """长轮询收取消息（最长 hold 35s）"""
        payload = {
            "get_updates_buf": self._update_buf,
            "base_info": {"channel_version": "1.0.2"},
        }
        base = self.bot_base_url or ILINK_BASE_URL
        async with httpx.AsyncClient(timeout=60, trust_env=False, verify=False) as client:
            resp = await client.post(
                f"{base}/ilink/bot/getupdates",
                headers=self._auth_headers(),
                json=payload,
            )
            data = resp.json()
            # 始终更新游标（即使返回空字符串），避免重复拉取已处理的消息
            if "get_updates_buf" in data:
                self._update_buf = data["get_updates_buf"] or ""
            return data.get("msgs", [])

    async def send_typing(self, to_user_id: str, context_token: str):
        """发送'正在输入'状态"""
        base = self.bot_base_url or ILINK_BASE_URL
        try:
            async with httpx.AsyncClient(trust_env=False, verify=False) as client:
                await client.post(
                    f"{base}/ilink/bot/sendtyping",
                    headers=self._auth_headers(),
                    json={
                        "to_user_id": to_user_id,
                        "context_token": context_token,
                        "base_info": {"channel_version": "1.0.2"},
                    },
                )
        except Exception:
            pass  # typing 失败不影响主流程

    async def send_message(
        self, to_user_id: str, context_token: str, text: str
    ) -> dict:
        """发送文本消息"""
        base = self.bot_base_url or ILINK_BASE_URL
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": f"bot-{uuid.uuid4().hex[:12]}",
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": [{"type": 1, "text_item": {"text": text}}],
            },
            "base_info": {"channel_version": "1.0.3"},
        }
        raw_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            **self._auth_headers(),
            "Content-Length": str(len(raw_bytes)),
        }
        async with httpx.AsyncClient(timeout=30, trust_env=False, verify=False) as client:
            resp = await client.post(
                f"{base}/ilink/bot/sendmessage",
                content=raw_bytes,
                headers=headers,
            )
            # {} 是 sendmessage 的成功响应（无返回值）
            resp_text = resp.text.strip()
            if resp_text == "{}" or resp_text == '{"ret":0}':
                return {"ret": 0}
            try:
                return json.loads(resp_text)
            except json.JSONDecodeError:
                logger.warning("[微信Bot] sendmessage 非 JSON: status=%s body=%s", resp.status_code, resp_text[:200])
                return {"ret": -1}

    async def _download_image_as_data_url(self, img_data: dict) -> Optional[str]:
        """下载微信 iLink 图片并转换为 data URL（base64 编码），供多模态 LLM 使用。

        iLink 图片通过 CDN 下载，数据使用 AES-128-ECB 加密，需用 aeskey 解密。
        """
        encrypt_query = img_data.get("encrypt_query", "")
        aeskey_hex = img_data.get("aeskey", "")
        if not encrypt_query:
            return None

        # 解析 AES key（兼容 hex 或 base64 格式）
        try:
            if aeskey_hex and len(aeskey_hex) == 32 and all(c in '0123456789abcdefABCDEF' for c in aeskey_hex):
                aes_key_hex = aeskey_hex.lower()
            elif aeskey_hex:
                # base64 编码的 16 字节 → hex
                aes_key_hex = base64.b64decode(aeskey_hex).hex()
            else:
                aes_key_hex = ""
        except Exception:
            aes_key_hex = ""

        # 构造 CDN 下载 URL
        import urllib.parse
        cdn_url = f"{ILINK_CDN_BASE}/download?encrypted_query_param={urllib.parse.quote(encrypt_query, safe='')}"

        try:
            async with httpx.AsyncClient(
                timeout=30, trust_env=False, verify=False,
                follow_redirects=True,
            ) as client:
                resp = await client.get(cdn_url)
                resp.raise_for_status()

                encrypted_data = resp.content
                if len(encrypted_data) < 16:
                    logger.warning("[微信Bot] CDN 图片数据过短: %d bytes", len(encrypted_data))
                    return None

                # AES-128-ECB 解密
                if aes_key_hex:
                    plaintext = aes_decrypt_ecb(encrypted_data, aes_key_hex)
                    logger.info("[微信Bot] CDN 图片下载+AES解密成功: %d bytes → %d bytes",
                                len(encrypted_data), len(plaintext))
                else:
                    # 无 AES key，直接使用原始数据
                    plaintext = encrypted_data
                    logger.info("[微信Bot] CDN 图片下载成功(无加密): %d bytes", len(plaintext))

                import base64 as b64_mod
                content_type = resp.headers.get("content-type", "image/png")
                if "image" not in content_type:
                    content_type = "image/png"
                b64_data = b64_mod.b64encode(plaintext).decode("ascii")
                return f"data:{content_type};base64,{b64_data}"

        except httpx.HTTPStatusError as e:
            logger.warning("[微信Bot] CDN 图片下载 HTTP %s", e.response.status_code)
            return None
        except Exception as e:
            logger.warning("[微信Bot] CDN 图片下载/解密失败: %s", e)
            return None

        return None

    # ── 消息处理 ──────────────────────────────────

    async def _handle_message(self, msg: dict):
        """处理单条微信消息：调用 agent 并回复，同时保存到会话存储"""
        from_user = msg.get("from_user_id", "")
        context_token = msg.get("context_token", "")

        # ── 调试：记录消息完整结构（关键：确认图片消息格式）──
        item_list = msg.get("item_list", [])
        item_types = [item.get("type") for item in item_list]
        msg_type = msg.get("message_type", "")
        logger.info(
            "[微信Bot:%s] 收到消息: from_user=%s msg_type=%s msg_keys=%s item_types=%s item_count=%d",
            self.user_id, from_user[:16], msg_type, list(msg.keys()), item_types, len(item_list),
        )
        if item_list:
            first_item = item_list[0]
            logger.info(
                "[微信Bot:%s] 首条 item: type=%s keys=%s preview=%s",
                self.user_id, first_item.get("type"), list(first_item.keys()),
                str(first_item)[:500],
            )

        # ── 提取图片 ──
        image_data = None
        for item in msg.get("item_list", []):
            if item.get("type") in (2, 3):
                img_item = item.get("image_item") or item.get("pic_item") or {}
                # iLink 图片：有 aeskey + media.encrypt_query_param，需通过下载接口获取
                aeskey = img_item.get("aeskey", "")
                media = img_item.get("media", {})
                encrypt_query = media.get("encrypt_query_param", "") if isinstance(media, dict) else ""
                if aeskey or encrypt_query:
                    image_data = {
                        "aeskey": aeskey,
                        "encrypt_query": encrypt_query,
                        "msg_id": item.get("msg_id") or msg.get("message_id", ""),
                    }
                    logger.info("[微信Bot:%s] 检测到 iLink 图片: msg_id=%s", self.user_id, image_data["msg_id"][:20])

        # ── 提取文本内容 ──
        text = ""
        for item in msg.get("item_list", []):
            if item.get("type") == 1:
                text = item.get("text_item", {}).get("text", "")
                break

        # ── 纯图片消息（无文本）：暂存，等待后续文本合并 ──
        if not text:
            if image_data:
                self._pending_images[from_user] = image_data
                logger.info("[微信Bot:%s] 收到用户 %s 的图片，已暂存等待后续文字提问", self.user_id, from_user[:16])
            return

        # ── 消息去重 ──
        msg_id = f"{from_user}|{context_token}|{text}"
        if msg_id in self._seen_msg_ids:
            logger.debug("[微信Bot] 跳过重复消息: %s", text[:60])
            return
        self._seen_msg_ids.add(msg_id)

        logger.info("[微信Bot:%s] 收到: %s  (context_token=%s...)", self.user_id, text[:120], (context_token or "")[:16])

        wechat_uid = f"wechat_{self.user_id}"

        # ── 首次启动时迁移旧版 wechat 命名空间下的会话 ──
        if getattr(self, '_sessions_migrated', False) is False:
            self._sessions_migrated = True
            try:
                old_sessions = session_store.list_sessions("wechat")
                if old_sessions and self.user_id == "admin":
                    for old_s in old_sessions:
                        sid = old_s["id"]
                        if not session_store.get_session(wechat_uid, sid):
                            # 复制会话到新命名空间（通过读取旧会话的所有消息重新写入）
                            old_detail = session_store.get_session("wechat", sid)
                            if old_detail and old_detail.get("messages"):
                                session_store.create_session(wechat_uid, title=old_detail.get("title", ""), session_id=sid)
                                for m in old_detail["messages"]:
                                    session_store.add_message(wechat_uid, sid, m["role"], m["content"])
                    logger.info("[微信Bot:%s] 已迁移 %d 个旧会话到新命名空间", self.user_id, len(old_sessions))
            except Exception as e:
                logger.warning("[微信Bot:%s] 会话迁移失败: %s", self.user_id, e)

        # ── /new 命令：创建新会话 ──
        if text.strip() == "/new":
            logger.debug("[微信Bot:%s] 触发 /new 命令", self.user_id)
            new_sid = uuid.uuid4().hex[:8]
            session_store.create_session(
                wechat_uid, title=f"[微信] 新会话", session_id=new_sid,
            )
            self._wechat_sessions[from_user] = new_sid
            await self.send_message(from_user, context_token, "✅ 已创建新会话，可以开始新的对话了")
            logger.info("[微信Bot:%s] 用户 %s 创建新会话 %s", self.user_id, from_user[:16], new_sid)
            return

        # ── /list 命令：列出所有会话 ──
        if text.strip() == "/list":
            all_sessions = session_store.list_sessions(wechat_uid)
            if not all_sessions:
                await self.send_message(from_user, context_token, "📭 暂无会话。发送 /new 创建新会话。")
                return
            lines = [f"📋 共有 {len(all_sessions)} 个会话："]
            for s in all_sessions:
                sid = s["id"]
                # 取最后一条用户消息作为摘要
                sess_detail = session_store.get_session(wechat_uid, sid)
                last_user_msg = ""
                if sess_detail and sess_detail.get("messages"):
                    for m in reversed(sess_detail["messages"]):
                        if m.get("role") == "user":
                            last_user_msg = m.get("content", "")[:50]
                            break
                marker = "→ " if sid == self._wechat_sessions.get(from_user) else "  "
                lines.append(f"{marker}{sid}: {last_user_msg or '(空)'}")
            await self.send_message(from_user, context_token, "\n".join(lines))
            return

        # ── /switch 命令：切换会话 ──
        if text.strip().startswith("/switch "):
            target_sid = text.strip()[len("/switch "):].strip()
            if not target_sid:
                await self.send_message(from_user, context_token, "❌ 请指定会话 ID，格式：/switch &lt;sessionId&gt;")
                return
            # 验证会话是否存在
            sess = session_store.get_session(wechat_uid, target_sid)
            if not sess:
                await self.send_message(from_user, context_token, f"❌ 会话 {target_sid} 不存在。发送 /list 查看可用会话。")
                return
            self._wechat_sessions[from_user] = target_sid
            await self.send_message(from_user, context_token, f"✅ 已切换到会话 {target_sid}，可以继续对话了")
            logger.info("[微信Bot:%s] 用户 %s 切换到会话 %s", self.user_id, from_user[:16], target_sid)
            return

        # ── 会话管理 ──
        session_id = self._wechat_sessions.get(from_user)
        if session_id is None:
            # 首次消息：用微信用户 ID 的 md5 作为稳定会话 ID
            session_id = hashlib.md5(from_user.encode()).hexdigest()[:8]
            self._wechat_sessions[from_user] = session_id
            session = session_store.get_session(wechat_uid, session_id)
            if session is None:
                session = session_store.create_session(
                    wechat_uid,
                    title=f"[微信] {text[:20]}",
                    session_id=session_id,
                )

        # ── 检查是否有暂存的图片，合并为图文消息 ──
        attachments = None

        # 优先使用当前消息中自带的图片，其次使用之前暂存的图片
        img_to_use = image_data or self._pending_images.pop(from_user, None)
        if img_to_use:
            try:
                data_url = await self._download_image_as_data_url(img_to_use)
                if data_url:
                    mime_type = data_url.split(";")[0].split(":")[1] if ";" in data_url else "image/png"
                    attachments = [{"mime_type": mime_type, "data_url": data_url}]
                    # 保存图片记录到消息
                    img_text = f"[图片: {img_to_use.get('msg_id', '')[-8:]}]"
                    session_store.add_message(wechat_uid, session_id, "user", img_text)
                    logger.info("[微信Bot:%s] 合并图片+文本消息", self.user_id)
            except Exception as e:
                logger.warning("[微信Bot:%s] 图片下载/转换失败: %s", self.user_id, e)
        else:
            # 无图片时清理可能的残留（另一用户的 pending 不会被误pop）
            self._pending_images.pop(from_user, None)

        # 保存用户文本消息
        add_ret = session_store.add_message(wechat_uid, session_id, "user", text)
        if add_ret is None:
            logger.warning("[微信Bot:%s] 用户消息保存失败: session=%s 不存在", self.user_id, session_id)

        # 发送"正在输入"状态
        await self.send_typing(from_user, context_token)

        # 为当前会话设置独立的 agent 线程
        self.agent._thread_id = session_id

        # 调用 agent 获取完整回复（支持图文消息）
        try:
            reply = await self.agent.chat_sync(text, attachments=attachments)
        except Exception as e:
            logger.exception("[微信Bot] agent 调用异常")
            reply = f"❌ 处理出错: {e}"

        # 保存助手回复
        if reply:
            reply_ret = session_store.add_message(wechat_uid, session_id, "assistant", reply)
            if reply_ret is None:
                logger.warning("[微信Bot:%s] 助手回复保存失败: session=%s 不存在", self.user_id, session_id)
            else:
                logger.info("[微信Bot:%s] 会话 %s 已保存助手回复 (%d 字符)", self.user_id, session_id, len(reply))
            sess = session_store.get_session(wechat_uid, session_id)
            if sess and sess.get("message_count", 0) <= 2:
                short = text[:30] + ("..." if len(text) > 30 else "")
                session_store.rename_session(wechat_uid, session_id, f"[微信] {short}")

        # 发送回复（带重试）
        if reply:
            send_resp = await self.send_message(from_user, context_token, reply)
            send_ret = send_resp.get("ret", -1)
            if send_ret != 0:
                logger.warning("[微信Bot] sendmessage 返回 ret=%s: %s", send_ret, json.dumps(send_resp, ensure_ascii=False)[:200])
                send_resp2 = await self.send_message(from_user, context_token, reply)
                send_ret2 = send_resp2.get("ret", -1)
                if send_ret2 == 0:
                    logger.info("[微信Bot:%s] 回复成功（重试）: %s", self.user_id, reply[:120])
                else:
                    logger.warning("[微信Bot:%s] 重试仍失败 ret=%s", self.user_id, send_ret2)
            else:
                logger.info("[微信Bot:%s] 回复: %s", self.user_id, reply[:120])

    # ── 生命周期 ──────────────────────────────────

    async def start(self):
        """启动后台轮询任务"""
        if self._running:
            return
        if not self.bot_token:
            logger.warning("[微信Bot] 未登录，请先扫码")
            return

        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("[微信Bot] 已启动")

    async def stop(self):
        """停止后台轮询"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("[微信Bot] 已停止")

    @property
    def is_running(self) -> bool:
        return self._running and self._task is not None

    @property
    def is_logged_in(self) -> bool:
        return bool(self.bot_token)

    async def _poll_loop(self):
        """主轮询循环"""
        while self._running:
            try:
                msgs = await self.poll_messages()
                for msg in msgs:
                    if not self._running:
                        break
                    await self._handle_message(msg)
            except asyncio.CancelledError:
                break
            except httpx.RemoteProtocolError:
                logger.warning("[微信Bot] 连接断开，5 秒后重试")
                await asyncio.sleep(5)
            except httpx.TimeoutException:
                logger.debug("[微信Bot] 轮询超时（正常）")
            except Exception as e:
                logger.error("[微信Bot] 轮询异常: %s", e)
                await asyncio.sleep(5)
