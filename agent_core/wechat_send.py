"""微信 Bot 发送链路（send channel）mixin。

从 wechat_bot.py 拆分（2026-08-03 大文件治理）：
- 发送文本/图片（send_message / send_image / _send_step_msg）
- 发送纪律（_throttle_send 节流 / _rate_limit_send 滑动窗口限速 / _build_step_batches 攒批）
- 最终回复后台补发（_schedule_retry / _retry_loop / 磁盘持久化）
- 图片下载（_download_image_as_data_url）

本模块方法全部通过 self 访问实例状态（__init__ 在主文件 wechat_bot.py 定义），
无独立状态；作为 WeChatBot 的 mixin 使用。
"""
import asyncio
import base64
import json
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Optional

import httpx

from logger import get_logger
from wechat_crypto import aes_decrypt_ecb

logger = get_logger(__name__)

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"


class WeChatSendMixin:
    # ── 发送 ──────────────────────────────────────

    @staticmethod
    def _parse_sendmessage_response(resp_text: str) -> dict:
        """解析 sendmessage 响应，统一返回 {"ret": ..., "message_id": ..., "detail": ...}。"""
        text = resp_text.strip()
        if text == "{}":
            return {"ret": 0}
        if text == '{"ret":0}':
            return {"ret": 0, "message_id": ""}
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {"ret": -1, "detail": {"raw": text[:200]}}
        if not isinstance(data, dict):
            return {"ret": -1, "detail": data}
        if data.get("ret") == 0:
            return {"ret": 0, "message_id": str(data.get("message_id", ""))}
        # iLink 成功响应可能只返回 message_id，不返回 ret=0
        # ponytail: 若未来出现 {"message_id": ..., "ret": -1} 这种矛盾包，需再收紧为 ret != -1 才视为成功
        if "message_id" in data:
            return {"ret": 0, "message_id": str(data["message_id"])}
        return {"ret": data.get("ret", -1), "message_id": str(data.get("message_id", "")), "detail": data}

    async def send_message(
        self, to_user_id: str, context_token: str, text: str,
        max_retries: int = 0, retry_delay: float = 1.0, max_backoff: float = 30.0,
    ) -> dict:
        """发送文本消息。

        max_retries: 失败后的重试次数（指数退避：delay, 2*delay, 4*delay...，单次退避上限 max_backoff）。
        微信对短时高频发送会返回 prepare failed（频控），指数退避重试可显著提高送达率。
        """
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
        last_send_resp: dict = {"ret": -1, "detail": {}}
        for attempt in range(max_retries + 1):
            async with httpx.AsyncClient(timeout=30, trust_env=False, verify=False) as client:
                resp = await client.post(
                    f"{base}/ilink/bot/sendmessage",
                    content=raw_bytes,
                    headers=headers,
                )
                resp_text = resp.text.strip()
                send_resp = self._parse_sendmessage_response(resp_text)
            if send_resp.get("ret", -1) == 0:
                logger.info("[微信Bot] 文本消息发送成功: message_id=%s", send_resp.get("message_id", ""))
                self._rate_limited_until = 0.0  # 发送成功说明频控解除，恢复 step 消息
                self._rate_limit_strikes = 0    # 连续失败计数清零，回到 60s 冷却基线
                return {"ret": 0, "message_id": send_resp.get("message_id", "")}
            last_send_resp = send_resp
            # 频控感知：prepare failed(ret=-2) 说明微信侧限频，进入冷却窗口，
            # 期间 step 消息暂停发送（见 _send_step_msg），避免持续踩频控拉长冷却。
            # 冷却时长阶梯递增：60s 起步、每失败 +60s、封顶 900s（15 分钟）。
            # 上限依据（2026-08-03 两次线上事故）：实测微信 prepare failed 频控窗口
            # ≤12 分钟，且**每次失败都会刷新窗口**——补发间隔必须 > 窗口才能等它
            # 自然过期；旧封顶 300s（5 分钟）仍在窗口内，45 分钟补发全失败。900s 保底
            # 是"撞不中窗口"的最小值；60s 起步保持对短时抖动的低惩罚（用户偏好）。
            if send_resp.get("ret") == -2:
                self._rate_limit_strikes += 1
                cooldown = min(60 * self._rate_limit_strikes, 900)
                self._rate_limited_until = max(self._rate_limited_until, time.time() + cooldown)
            logger.warning("[微信Bot] sendmessage 文本消息失败: status=%s resp=%s", resp.status_code, json.dumps(send_resp, ensure_ascii=False)[:300])
            if attempt < max_retries:
                delay = retry_delay * (2 ** attempt)
                logger.info("[微信Bot] sendmessage 失败，%.1fs 后重试 (%d/%d)", delay, attempt + 1, max_retries)
                await asyncio.sleep(delay)
        return {"ret": -1, "detail": last_send_resp}

    async def _throttle_send(self, min_interval: float = 1.5):
        """发送节流：与上一条消息保持最小间隔，避免短时高频发送触发微信 prepare failed 频控。

        由于 step 分段回复会在几十秒内连发十几条消息，微信侧短时 burst 会返回
        prepare failed；这里在每条消息前按需等待，把发送节奏摊平。
        """
        wait = min_interval - (time.time() - self._last_send_at)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_send_at = time.time()

    @staticmethod
    def _build_step_batches(lines: list[str], batch: int, head: str = "") -> list[str]:
        """把工具结果行按 batch 攒批合并，返回待发送的 step 消息文本列表。

        - 每满 batch 条合并为一条消息；残余不足 batch 的也合并为最后一条（不留过程死角）
        - head（💭思考）只拼到第一条，避免每条都重复思考内容
        """
        if not lines:
            return []
        batches = []
        for i in range(0, len(lines), batch):
            chunk = lines[i:i + batch]
            prefix = f"**💭 思考**\n{head}\n" if head and i == 0 else ""
            batches.append(prefix + "\n".join(chunk))
        return batches

    async def _rate_limit_send(self):
        """滑动窗口发送限速：send_rate_window 秒内最多 send_rate_max 条 sendmessage。

        官方 iLink 对 sendmessage 无公开条数配额，频控是频率型（ret=-2 rate limited，
        官方仓库 Tencent/openclaw-weixin#142 用 backoff 应对）。这里在发送前排队，
        把 burst 摊平到窗口内（默认 60s/10 条 ≈ 6s 一条），从源头避免触发频控。
        """
        now = time.time()
        self._send_timestamps = [t for t in self._send_timestamps if now - t < self.send_rate_window]
        while len(self._send_timestamps) >= self.send_rate_max:
            wait = self.send_rate_window - (now - self._send_timestamps[0])
            if wait <= 0:
                break
            logger.debug("[微信Bot:%s] 发送限速中，%.1fs 后重试", self.user_id, wait)
            await asyncio.sleep(min(wait, 5.0))
            now = time.time()
            self._send_timestamps = [t for t in self._send_timestamps if now - t < self.send_rate_window]
        self._send_timestamps.append(now)

    async def _send_step_msg(self, to_user_id: str, context_token: str, text: str):
        """发送步骤级进度消息（思考/工具执行分段），失败退避重试，不阻塞主流程。

        官方 iLink 无公开条数配额，频控是频率型（ret=-2 rate limited，官方 3s backoff 应对）。
        因此不做"失败即放弃后续 step"：滑动窗口限速 + 失败指数退避重试（max_retries=1,
        retry_delay=2s 即 2s→4s 退避），单条失败只记日志，后续 step 继续发，
        过程消息持续可见；最终回复有独立更长的重试兜底（send_message max_retries=3）。

        ponytail: 当 step_msg_budget 耗尽或频控冷却中时，消息不再丢弃，而是暂存到
        _step_pending_queue，由 _send_step_msg_immediate 在冷却结束后补发。
        """
        # 频控冷却中：微信侧限频未解除，step 消息暂停发送（避免持续踩频控拉长冷却），
        # 最终回复仍走立即重试 + 后台补发，不受影响。
        if time.time() < self._rate_limited_until:
            logger.debug("[微信Bot:%s] 频控冷却中，暂存 step 消息: %s", self.user_id, text[:40])
            self._step_pending_queue.append(text)
            return
        # 防御性上限：仅拦截极端刷屏场景，正常任务被限速器+批量约束不会触达
        if self._step_sent_count >= self.step_msg_budget:
            logger.info("[微信Bot:%s] step 消息达到防御上限(%d)，暂存等待冷却后补发: %s",
                        self.user_id, self.step_msg_budget, text[:40])
            self._step_pending_queue.append(text)
            return
        try:
            await self._throttle_send()
            await self._rate_limit_send()
            # max_retries=0：失败不立即重试（2026-08-03 事故后统一策略——频控窗口内
            # 重试只会刷新冷却窗口；step 是过程消息，失败跳过即可，最终回复有补发兜底）。
            resp = await self.send_message(to_user_id, context_token, text, max_retries=0)
            if resp.get("ret", -1) == 0:
                self._step_sent_count += 1
            else:
                logger.warning("[微信Bot:%s] step 消息发送失败（跳过）: %s", self.user_id, text[:60])
        except Exception as e:
            logger.warning("[微信Bot:%s] step 消息发送异常: %s", self.user_id, e)

    async def _send_step_msg_immediate(self, to_user_id: str, context_token: str, text: str):
        """直接发送 step 消息（跳过频控冷却检查），用于补发暂存队列中的消息。

        在任务结束/冷却结束后调用，此时微信侧频控已解除，直接发送即可。
        """
        if self._step_pending_draining:
            return  # 防止递归
        self._step_pending_draining = True
        try:
            await self._throttle_send()
            await self._rate_limit_send()
            resp = await self.send_message(to_user_id, context_token, text, max_retries=0)
            if resp.get("ret", -1) == 0:
                logger.info("[微信Bot:%s] 补发 step 消息成功: %s", self.user_id, text[:40])
            else:
                logger.warning("[微信Bot:%s] 补发 step 消息失败: %s", self.user_id, text[:60])
        except Exception as e:
            logger.warning("[微信Bot:%s] 补发 step 消息异常: %s", self.user_id, e)
        finally:
            self._step_pending_draining = False

    async def send_image(
        self, to_user_id: str, context_token: str, image_path: str
    ) -> dict:
        """发送图片消息（上传到 CDN 后再发送）

        流程:
          1. 读取图片文件 → AES-128-ECB 加密 → 计算 MD5 和大小
          2. POST /ilink/bot/getuploadurl 获取上传参数
          3. POST CDN /upload 上传加密后的图片数据
          4. POST /ilink/bot/sendmessage 发送消息（含 image_item）
        """
        import hashlib
        import subprocess

        base = self.bot_base_url or ILINK_BASE_URL
        cdn_base = ILINK_CDN_BASE

        try:
            # 1. 读取图片文件
            with open(image_path, "rb") as f:
                raw_data = f.read()
        except Exception as e:
            logger.warning("[微信Bot] 读取图片失败: %s", e)
            return {"ret": -1, "error": str(e)}

        raw_size = len(raw_data)
        raw_md5 = hashlib.md5(raw_data).hexdigest()
        filekey = uuid.uuid4().hex
        aes_key = uuid.uuid4().hex[:32]  # 32 hex chars = 16 bytes

        # 2. AES-128-ECB 加密（用 OpenSSL）
        try:
            result = subprocess.run(
                ['openssl', 'enc', '-e', '-aes-128-ecb', '-K', aes_key, '-nosalt'],
                input=raw_data,
                capture_output=True,
                timeout=30,
            )
            if result.returncode != 0 or not result.stdout:
                raise RuntimeError(f"OpenSSL encrypt failed: {result.stderr.decode()[:200]}")
            encrypted_data = result.stdout
        except Exception as e:
            logger.warning("[微信Bot] 图片加密失败: %s", e)
            return {"ret": -1, "error": str(e)}

        encrypted_size = len(encrypted_data)

        # 3. 获取上传 URL
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False, verify=False) as client:
                resp = await client.post(
                    f"{base}/ilink/bot/getuploadurl",
                    headers=self._auth_headers(),
                    json={
                        "filekey": filekey,
                        "media_type": 1,  # IMAGE
                        "to_user_id": to_user_id,
                        "rawsize": raw_size,
                        "rawfilemd5": raw_md5,
                        "filesize": encrypted_size,
                        "no_need_thumb": True,
                        "aeskey": aes_key,
                    },
                )
                data = resp.json()
                upload_param = data.get("upload_param") or ""
                if not upload_param:
                    upload_full_url = data.get("upload_full_url", "")
                    if upload_full_url:
                        import urllib.parse
                        parsed = urllib.parse.urlparse(upload_full_url)
                        qs = urllib.parse.parse_qs(parsed.query)
                        upload_param = qs.get("encrypted_query_param", [""])[0]
                if not upload_param:
                    logger.warning("[微信Bot] getuploadurl 返回无 upload_param: %s", str(data)[:200])
                    return {"ret": -1}
        except Exception as e:
            logger.warning("[微信Bot] getuploadurl 请求失败: %s", e)
            return {"ret": -1, "error": str(e)}

        # 4. 上传到 CDN
        import urllib.parse
        cdn_upload_url = (
            f"{cdn_base}/upload?encrypted_query_param={urllib.parse.quote(upload_param, safe='')}"
            f"&filekey={urllib.parse.quote(filekey, safe='')}"
        )
        try:
            async with httpx.AsyncClient(timeout=60, trust_env=False, verify=False) as client:
                resp = await client.post(
                    cdn_upload_url,
                    content=encrypted_data,
                    headers={"Content-Type": "application/octet-stream"},
                )
                if resp.status_code != 200:
                    logger.warning("[微信Bot] CDN 上传失败: HTTP %s", resp.status_code)
                    return {"ret": -1}
                download_param = resp.headers.get("x-encrypted-param")
                if not download_param:
                    logger.warning("[微信Bot] CDN 上传响应缺少 x-encrypted-param")
                    return {"ret": -1}
        except Exception as e:
            logger.warning("[微信Bot] CDN 上传请求失败: %s", e)
            return {"ret": -1, "error": str(e)}

        # 5. 发送图片消息
        # media.aes_key 在 iLink 协议中是 hex 字符串的 UTF-8 base64 编码
        import base64 as b64_mod
        aes_key_b64 = b64_mod.b64encode(aes_key.encode("utf-8")).decode("ascii")
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": f"bot-{uuid.uuid4().hex[:12]}",
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": [{
                    "type": 2,  # IMAGE
                    "image_item": {
                        "media": {
                            "encrypt_query_param": download_param,
                            "aes_key": aes_key_b64,
                            "encrypt_type": 1,
                        },
                        "mid_size": encrypted_size,
                    },
                }],
            },
            "base_info": {"channel_version": "1.0.3"},
        }
        raw_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            **self._auth_headers(),
            "Content-Length": str(len(raw_bytes)),
        }
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False, verify=False) as client:
                resp = await client.post(
                    f"{base}/ilink/bot/sendmessage",
                    content=raw_bytes,
                    headers=headers,
                )
                resp_text = resp.text.strip()
                send_resp = self._parse_sendmessage_response(resp_text)
                if send_resp.get("ret", -1) == 0:
                    logger.info("[微信Bot] 图片消息发送成功: message_id=%s %s -> %s", send_resp.get("message_id", ""), image_path[:60], to_user_id[:16])
                    return {"ret": 0, "message_id": send_resp.get("message_id", "")}
                logger.warning("[微信Bot] sendmessage 图片消息失败: status=%s resp=%s", resp.status_code, json.dumps(send_resp, ensure_ascii=False)[:300])
                return {"ret": -1, "detail": send_resp}
        except Exception as e:
            logger.warning("[微信Bot] 发送图片消息失败: %s", e)
            return {"ret": -1, "error": str(e)}

    # ── 最终回复后台补发 ─────────────────────────────

    def _load_retry_queue(self) -> None:
        """启动时从磁盘恢复未完成的补发队列（服务重启不丢最终回复）。

        磁盘上的 next 已过期（重启期间频控早已冷却）时立即重试。
        """
        try:
            if not self._retry_queue_path.exists():
                return
            data = json.loads(self._retry_queue_path.read_text(encoding="utf-8"))
            now = time.time()
            for item in data or []:
                self._retry_queue.append({
                    "to": item.get("to", ""),
                    "token": item.get("token", ""),
                    "text": item.get("text", ""),
                    "attempts": int(item.get("attempts", 0)),
                    "next": float(item.get("next", now)),
                })
            if self._retry_queue:
                logger.info("[微信Bot:%s] 已从磁盘恢复 %d 条待补发消息", self.user_id, len(self._retry_queue))
                # 无运行中事件循环时（如启动阶段）不建 task，由后续 _schedule_retry 重建
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    pass
                else:
                    if self._retry_task is None or self._retry_task.done():
                        self._retry_task = asyncio.create_task(self._retry_loop())
        except Exception as e:
            logger.warning("[微信Bot:%s] 补发队列加载失败: %s", self.user_id, e)

    def _persist_retry_queue(self) -> None:
        """把当前补发队列写盘（每次入队/重试后调用，重启后可恢复）。"""
        try:
            data = [
                {"to": i["to"], "token": i["token"], "text": i["text"],
                 "attempts": i["attempts"], "next": i["next"]}
                for i in self._retry_queue
            ]
            self._retry_queue_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            logger.warning("[微信Bot:%s] 补发队列持久化失败: %s", self.user_id, e)

    def _schedule_retry(self, to_user_id: str, context_token: str, text: str) -> None:
        """最终回复发送失败后入补发队列，由后台任务定时重试（不阻塞轮询循环）。

        立即重试（send_message max_retries=3）已覆盖短时频控；仍失败说明频控窗口更长
        （实测 prepare failed 可持续 1 分钟+），转入后台指数退避补发，避免回复静默丢失。
        """
        if not text:
            return
        # 同一用户同文本不重复入队（补发期间用户可能再次触发同一回复场景）
        for item in self._retry_queue:
            if item["to"] == to_user_id and item["text"] == text:
                return
        self._retry_queue.append({
            "to": to_user_id, "token": context_token, "text": text,
            "attempts": 0, "next": time.time() + 15,
        })
        self._persist_retry_queue()
        if self._retry_task is None or self._retry_task.done():
            self._retry_task = asyncio.create_task(self._retry_loop())
        logger.info("[微信Bot:%s] 最终回复已入补发队列（%d 条待补发）", self.user_id, len(self._retry_queue))

    async def _retry_loop(self) -> None:
        """后台补发循环：等频控冷却结束后再发，失败不立即重试。

        策略（ponytail，2026-08-03 线上事故教训）：
        - 频控冷却中（_rate_limited_until > now）整队列挂起、绝不发送：
          冷却中强行发送只会再吃 ret=-2，把冷却窗口又往后推（send_message 失败时
          按阶梯递增刷新 _rate_limited_until：60s→120s→…→300s），形成
          "越试越冷却"的恶性循环——之前 5 次补发全失败、频控持续 11 分钟+ 就是它造成的。
        - 每次补发只发一次（max_retries=0）：失败说明仍被频控/异常，等下一轮，
          不在补发路径里做立即重试。
        - 失败后 next 取 max(退避, 冷却结束)：确保下次尝试一定在冷却窗口之后。
        - BACKOFF 加长到 15 轮（30s→900s，总退避约 2 小时 + 每轮叠加冷却）：
          实测 prepare failed 频控窗口 ≤12 分钟且失败会刷新窗口，旧的 7 轮
          （30s→300s，总 21 分钟）间隔仍小于窗口、在窗口内反复撞上导致 45 分钟
          不解除；现在冷却封顶 900s > 窗口，补发间隔跟随冷却结束后再试。
        """
        BACKOFF = (30, 60, 120, 240, 300, 360, 420, 480, 540, 600, 660, 720, 780, 840, 900)
        while self._retry_queue:
            now = time.time()
            # 冷却中：整队列挂起，睡到冷却结束再检查（不发送，避免刷新冷却窗口）
            if now < self._rate_limited_until:
                await asyncio.sleep(min(self._rate_limited_until - now, 5.0))
                continue
            still: list[dict] = []
            for item in list(self._retry_queue):
                if item["next"] > now:
                    still.append(item)
                    continue
                if item["attempts"] >= len(BACKOFF):
                    logger.warning("[微信Bot:%s] 补发放弃（重试 %d 次仍失败）: %s",
                                   self.user_id, item["attempts"], item["text"][:60])
                    continue
                try:
                    await self._throttle_send()
                    resp = await self.send_message(
                        item["to"], item["token"], item["text"], max_retries=0,
                    )
                except Exception as e:
                    logger.warning("[微信Bot:%s] 补发异常: %s", self.user_id, e)
                    resp = {"ret": -1, "detail": str(e)}
                if resp.get("ret", -1) == 0:
                    logger.info("[微信Bot:%s] 补发成功: message_id=%s", self.user_id, resp.get("message_id", ""))
                else:
                    item["attempts"] += 1
                    # 至少等到冷却结束再试（send_message 失败已按阶梯刷新 _rate_limited_until）
                    item["next"] = max(now + BACKOFF[item["attempts"] - 1], self._rate_limited_until)
                    still.append(item)
            self._retry_queue = deque(still)
            self._persist_retry_queue()
            if self._retry_queue:
                await asyncio.sleep(5)
        self._retry_task = None

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
