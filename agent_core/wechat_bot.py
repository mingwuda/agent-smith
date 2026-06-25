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


class WeChatBot:
    """微信 iLink Bot API 客户端"""

    def __init__(self, agent, data_dir: Optional[str] = None):
        self.agent = agent
        self.data_dir = data_dir or str(Path.home() / ".desktop_agent" / "wechat")
        self.bot_token: Optional[str] = None
        self.bot_base_url: Optional[str] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._update_buf: str = ""
        self._seen_msg_ids: set[str] = set()
        self._wechat_sessions: dict[str, str] = {}  # wx_user_id → current session_id  # 已处理消息 ID 去重

        os.makedirs(self.data_dir, exist_ok=True)
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
        async with httpx.AsyncClient(trust_env=False) as client:
            resp = await client.get(
                f"{ILINK_BASE_URL}/ilink/bot/get_bot_qrcode",
                params={"bot_type": "3"},
            )
            return resp.json()

    async def poll_qrcode_status(self, qrcode: str):
        """轮询扫码状态，扫码确认后保存 token"""
        async with httpx.AsyncClient(trust_env=False) as client:
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
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
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
            async with httpx.AsyncClient(trust_env=False) as client:
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
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
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

    # ── 消息处理 ──────────────────────────────────

    async def _handle_message(self, msg: dict):
        """处理单条微信消息：调用 agent 并回复，同时保存到会话存储"""
        from_user = msg.get("from_user_id", "")
        context_token = msg.get("context_token", "")

        # 提取文本内容
        text = ""
        for item in msg.get("item_list", []):
            if item.get("type") == 1:
                text = item.get("text_item", {}).get("text", "")
                break
        if not text:
            return

        # ── 消息去重 ──
        msg_id = f"{from_user}|{context_token}|{text}"
        if msg_id in self._seen_msg_ids:
            logger.debug("[微信Bot] 跳过重复消息: %s", text[:60])
            return
        self._seen_msg_ids.add(msg_id)

        logger.info("[微信Bot] 收到: %s  (context_token=%s...)", text[:120], (context_token or "")[:16])

        WECHAT_USER = "wechat"

        # ── /new 命令：创建新会话 ──
        if text.strip() == "/new":
            new_sid = uuid.uuid4().hex[:8]
            session_store.create_session(
                WECHAT_USER, title=f"[微信] 新会话", session_id=new_sid,
            )
            self._wechat_sessions[from_user] = new_sid
            await self.send_message(from_user, context_token, "✅ 已创建新会话，可以开始新的对话了")
            logger.info("[微信Bot] 用户 %s 创建新会话 %s", from_user[:16], new_sid)
            return

        # ── 会话管理 ──
        session_id = self._wechat_sessions.get(from_user)
        if session_id is None:
            # 首次消息：用微信用户 ID 的 md5 作为稳定会话 ID
            session_id = hashlib.md5(from_user.encode()).hexdigest()[:8]
            self._wechat_sessions[from_user] = session_id
            session = session_store.get_session(WECHAT_USER, session_id)
            if session is None:
                session = session_store.create_session(
                    WECHAT_USER,
                    title=f"[微信] {text[:20]}",
                    session_id=session_id,
                )

        # 保存用户消息
        session_store.add_message(WECHAT_USER, session_id, "user", text)

        # 发送"正在输入"状态
        await self.send_typing(from_user, context_token)

        # 为当前会话设置独立的 agent 线程
        self.agent._thread_id = session_id

        # 调用 agent 获取完整回复
        try:
            reply = await self.agent.chat_sync(text)
        except Exception as e:
            logger.exception("[微信Bot] agent 调用异常")
            reply = f"❌ 处理出错: {e}"

        # 保存助手回复
        if reply:
            session_store.add_message(WECHAT_USER, session_id, "assistant", reply)
            sess = session_store.get_session(WECHAT_USER, session_id)
            if sess and sess.get("message_count", 0) <= 2:
                short = text[:30] + ("..." if len(text) > 30 else "")
                session_store.rename_session(WECHAT_USER, session_id, f"[微信] {short}")

        # 发送回复（带重试）
        if reply:
            send_resp = await self.send_message(from_user, context_token, reply)
            send_ret = send_resp.get("ret", -1)
            if send_ret != 0:
                logger.warning("[微信Bot] sendmessage 返回 ret=%s: %s", send_ret, json.dumps(send_resp, ensure_ascii=False)[:200])
                send_resp2 = await self.send_message(from_user, context_token, reply)
                send_ret2 = send_resp2.get("ret", -1)
                if send_ret2 == 0:
                    logger.info("[微信Bot] 回复成功（重试）: %s", reply[:120])
                else:
                    logger.warning("[微信Bot] 重试仍失败 ret=%s", send_ret2)
            else:
                logger.info("[微信Bot] 回复: %s", reply[:120])

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
