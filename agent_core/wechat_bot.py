"""微信 iLink Bot API 客户端

基于腾讯 iLink 协议（ilinkai.weixin.qq.com）实现微信个人号 Bot。
无需公网 IP，客户端主动长轮询收取消息。
"""
import asyncio
import base64
import fcntl
import hashlib
import json
import os
import random
import time
from collections import deque
from pathlib import Path
from typing import Optional

import httpx

from logger import get_logger
import session_store
from wechat_commands import WeChatCommandMixin
from wechat_send import WeChatSendMixin

# 暂存图片最久保留时间（秒），超时自动清理，防止内存泄漏
PENDING_IMAGE_TTL = 300  # 5 分钟

logger = get_logger(__name__)

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"

# ── WeChat Bot 类 ─────────────────────────────────────────────────

def _extract_text(msg: dict) -> str:
    """从微信消息 item_list 提取纯文本内容（type==1 的 text_item.text）。

    _dispatch_message 需要预先判断消息是否为命令（决定即时/排队），
    与 _handle_message 共用同一提取逻辑，避免两份实现漂移。
    """
    for item in msg.get("item_list", []):
        if item.get("type") == 1:
            return item.get("text_item", {}).get("text", "")
    return ""


# 全局串行锁：所有微信 Bot 共享，保证同一时刻只有一个用户的 agent 调用在飞。
# 原因：agent 的工具工作区（file_tools/shell_tools 等）是模块级全局状态，
# _apply_session_workspace 会改写它们；并发处理两个用户消息会互相覆盖工作区，
# 导致 A 用户的任务读取 B 用户的文件。独立 agent 实例隔离了 checkpoint/记忆，
# 但工具工作区仍共享，因此用锁串行化 agent 执行段。
# ponytail: 串行化在个人部署（1~4 个测试号）下无感知；若未来多用户高并发，
# 升级路径是把工具工作区改为 per-request ContextVar，去掉此锁。
_WECHAT_AGENT_LOCK = asyncio.Lock()


class WeChatBot(WeChatCommandMixin, WeChatSendMixin):
    """微信 iLink Bot API 客户端（按用户隔离）

    职责分布（2026-08-03 拆分）：
    - wechat_commands.py：斜杠指令 + 菜单构建（WeChatCommandMixin）
    - wechat_send.py：发送链路 + 补发队列 + 图片下载（WeChatSendMixin）
    - wechat_crypto.py：图片 AES-128-ECB 解密（纯函数）
    - 本文件：状态初始化、鉴权登录、轮询、消息处理、生命周期
    """

    def __init__(self, agent=None, user_id: str = "default", data_dir: Optional[str] = None, tools: Optional[list] = None):
        # agent 不传时懒创建该用户专属的独立实例（见 _ensure_agent）。
        # 必须与 Web 端全局 agent 分离：全局 agent 的 _user_id / workspace /
        # checkpoint 是共享可变状态，多微信用户 + Web 并发会互相覆盖，
        # 曾导致「zhangcaixin 用户的项目内容发给 admin 微信用户」的跨用户泄露。
        self.agent = agent
        # 独立 agent 的工具集：由 main._get_wechat_bot 显式传入（base_tools）。
        # 不在此处 `from main import app`：python main.py 模式下 sys.modules["main"]
        # 是第二次导入的双实例，其 state 依赖兜底 init 时序，不可靠。
        self._base_tools = list(tools) if tools else []
        self.user_id = user_id
        self.data_dir = data_dir or str(Path.home() / ".desktop_agent" / f"wechat_{user_id}")
        self.bot_token: Optional[str] = None
        self.bot_base_url: Optional[str] = None
        self._running = False
        self._lock_fd = None  # 跨进程文件锁（fcntl），用于防止同一账号被多进程同时轮询
        self._task: Optional[asyncio.Task] = None
        self._update_buf: str = ""
        self._seen_msg_ids: set[str] = set()
        self._wechat_sessions: dict[str, str] = {}  # wx_user_id → current session_id
        # /list 时缓存「序号 → sessionId」映射，供 /switch /delete 按序号解析（见 _resolve_session_arg）
        self._wechat_session_menu: dict[str, dict[str, str]] = {}
        # 当前选中的项目（key=from_user），影响 /new 和 /sessions 的归属
        self._wechat_current_project: dict[str, str] = {}
        # /projects 时缓存「序号 → project_id」映射，供 /project 按序号解析
        self._wechat_project_menu: dict[str, dict[str, str]] = {}
        # /modals 时缓存「序号 → (provider_id, model)」映射，供 /modal 按序号切换
        self._wechat_model_menu: dict[str, dict[str, tuple]] = {}
        # 暂存用户最近发送的图片（key=from_user），等待后续文本合并为图文消息
        # 值: {"data": dict, "time": float}，5 分钟后自动清理
        self._pending_images: dict[str, dict] = {}
        # 步骤级即时回复开关：思考/工具执行分段推送（WECHAT_STEP_REPLY=0 关闭，回到一次性回复）
        self.step_reply_enabled = os.environ.get("WECHAT_STEP_REPLY", "1") != "0"
        # 发送节流：上一条消息发送完成的时间戳（微信短时高频发送会触发 prepare failed 频控）
        self._last_send_at: float = 0.0
        # 每轮任务 step 消息防御性上限（默认 30 条）。官方 iLink 对 sendmessage 无公开条数配额，
        # 频控是频率型（ret=-2 rate limited，官方仓库 Tencent/openclaw-weixin#142 用 backoff 应对）。
        # 因此发送节奏由「批量合并省条数 + 滑动窗口限速 + 失败退避重试」保证，budget 仅在极端
        # 刷屏场景兜底（30 条 × 批量3 ≈ 覆盖 90 个工具过程，正常任务几乎触达不到；最终回复不受限）。
        # ponytail: 原设计在 budget 耗尽时静默丢弃 step 消息，现已改为暂存队列等冷却后补发。
        self.step_msg_budget = int(os.environ.get("WECHAT_STEP_MSG_BUDGET", "30"))
        # step 消息批量合并：每 N 个工具结果攒批合并为 1 条消息（默认 3）。
        # 批量后 30 条防御上限可覆盖 30×3=90 个工具执行过程，过程消息几乎全程可见，
        # 不再出现"预算用尽后用户只能干等最终消息"；单条 step 消息也更紧凑（多工具一屏看完）。
        self.step_msg_batch = int(os.environ.get("WECHAT_STEP_MSG_BATCH", "3"))
        # 攒批时间阈值（秒，默认 8）：批未攒满 batch 条但超过该时长也立即发送，
        # 避免工具执行慢时第一条反馈被"等满 3 条"延迟。
        self.step_msg_batch_timeout = float(os.environ.get("WECHAT_STEP_MSG_BATCH_TIMEOUT", "8"))
        # 滑动窗口发送限速：send_rate_window 秒内最多 send_rate_max 条 sendmessage。
        # 官方无公开配额，实测短时高频 burst 触发 ret=-2 rate limited；把发送摊平到窗口内
        # 从源头避免 burst（官方推荐 messages_per_second 限速器；默认 60s/10 条 ≈ 6s 一条）。
        self.send_rate_window = float(os.environ.get("WECHAT_SEND_RATE_WINDOW", "60"))
        self.send_rate_max = int(os.environ.get("WECHAT_SEND_RATE_MAX", "10"))
        self._send_timestamps: list[float] = []  # 滑动窗口内已发送成功的时间戳
        # 本轮已发送的 step 消息计数（每次处理用户消息前重置为 0）
        self._step_sent_count: int = 0
        # 最终回复补发队列：send_message 频控失败后入队，由 _retry_loop 后台指数退避重试。
        # 必须在 __init__ 初始化——stop() 与 _schedule_retry 都直接引用这两个属性，
        # 缺了会 AttributeError，造成「最终回复丢失」+「stop 异常→文件锁不释放→
        # 新 bot 拿不到轮询锁」两类连锁故障（2026-08-03 线上事故根因）。
        self._retry_queue: deque = deque()
        self._retry_task: Optional[asyncio.Task] = None
        # step 消息暂存队列：当 step_msg_budget 耗尽或频控冷却时，step 消息不入丢，
        # 而是暂存到此队列；冷却结束/下一轮任务开始时由 _drain_step_pending_queue 统一发出。
        # 避免工具调用多时用户看不到任何过程反馈（"以为卡死"的根本原因）。
        self._step_pending_queue: deque = deque()
        self._step_pending_draining: bool = False  # 防止 _drain 递归调用
        # 微信频控感知：sendmessage 返回 prepare failed 后进入冷却窗口，
        # 期间 step 消息暂停发送（不再持续踩频控，缩短频控持续时间），
        # 只保留最终回复的立即重试 + 后台补发。窗口内任一发送成功后清零。
        self._rate_limited_until: float = 0.0
        # 连续频控失败计数：冷却时长阶梯递增（60s 起步、每失败 +60s、封顶 300s）。
        # 不立即 5 分钟（频控可能是短时抖动，直接 300s 过度惩罚），
        # 也不固定 60s（连续失败说明频控未解除，固定 60s 会被反复撞穿）。
        # 成功一次即清零，回到 60s 基线。
        self._rate_limit_strikes: int = 0
        # 当前正在执行的 agent 任务（/stop 命令通过 cancel 它来中断请求）。
        # 同一 bot 同一时刻至多一个普通对话消息在跑 agent（_msg_lock 串行），
        # 因此单值即可；被 /stop cancel 后由 _handle_message 的 except 分支收尾。
        self._active_run_task: Optional[asyncio.Task] = None
        # push 队列：/push <内容> 在当前任务执行中入队（key=from_user），
        # 当前任务结束后由 _handle_message 尾部 _flush_push_queue 按序自动发送。
        self._push_queues: dict[str, list[str]] = {}
        self._push_seq: int = 0  # 合成消息 message_id 用自增序号，避免与真实消息 id 撞车
        # 普通对话消息串行锁：命令消息（含 /stop /push）不排队即时处理，
        # 普通消息排队执行，避免并发导致会话 history 乱序 / 回复错位。
        self._msg_lock = asyncio.Lock()
        # 消息处理任务集合：_poll_loop 并行派发 _dispatch_message 后跟踪，
        # 防止任务被 GC（fire-and-forget 的引用保持）。
        self._handle_tasks: set[asyncio.Task] = set()
        # 自适应轮询间隔
        self._last_activity_at: float = time.time()
        self._poll_delay: float = 0.0  # 当前轮询间隔（秒），0=无延迟

        os.makedirs(self.data_dir, exist_ok=True)

        # 补发队列持久化路径：服务重启后从磁盘恢复未完成的补发，
        # 避免「最终回复入队后服务重启 → 内存队列丢失 → 用户永远收不到」。
        self._retry_queue_path = Path(self.data_dir) / "retry_queue.json"
        self._load_retry_queue()

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
        self._load_current_project()

    # ── Agent 实例（用户隔离）──────────────────────

    def _ensure_agent(self):
        """懒创建该微信用户专属的独立 DesktopAgent 实例。

        - set_user(f"wechat_{user_id}")：checkpoint key 前缀为 wechat_<uid>，
          与 Web 端 admin 等用户彻底隔离（之前共用全局 agent 时 _user_id 漂移
          为 default，thread_key 与存储命名空间不匹配）。
        - 工具集用 main 传入的 base_tools（不含 MCP），MCP 为 Web 端会话级高级功能，
          微信渠道暂不加载（ponytail: 如需微信 MCP，在此按会话 workspace 重载）。
        """
        if self.agent is not None:
            return self.agent
        from agent import DesktopAgent
        from config import AgentConfig
        ag = DesktopAgent(AgentConfig.load())
        if self._base_tools:
            ag.set_tools(list(self._base_tools))
        ag.set_user(f"wechat_{self.user_id}")
        self.agent = ag
        logger.info("[微信Bot:%s] 已创建用户专属 agent 实例 (user=%s, tools=%d)",
                    self.user_id, f"wechat_{self.user_id}", len(self._base_tools))
        return ag

    # ── 鉴权 ──────────────────────────────────────

    def _load_current_project(self):
        """从磁盘恢复用户当前选中的项目（服务重启后不丢失）"""
        path = Path(self.data_dir) / "current_project.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._wechat_current_project = data.get("current_projects", {})
            except Exception:
                pass

    def _save_current_project(self):
        """持久化当前项目映射到磁盘"""
        path = Path(self.data_dir) / "current_project.json"
        try:
            path.write_text(
                json.dumps({"current_projects": self._wechat_current_project}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _load_token(self):
        path = Path(self.data_dir) / "token.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.bot_token = data.get("bot_token")
                self.bot_base_url = data.get("base_url") or ""
            except Exception:
                pass

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

    # ── 消息处理 ──────────────────────────────────

    def _cancel_active_run(self) -> bool:
        """请求中断当前正在执行的 agent 任务（/stop 命令）。

        返回是否有任务被取消。被中断的任务在 _handle_message 的
        except CancelledError 分支产出"⏹️ 任务已中断"最终回复。
        """
        task = self._active_run_task
        if task is not None and not task.done():
            task.cancel()
            return True
        return False

    async def _flush_push_queue(self, from_user: str, context_token: str) -> None:
        """按序处理该用户的 push 队列（/push 入队内容），当前任务结束后调用。

        每取出一条就完整走一遍 _handle_message（会话解析→历史加载→agent 执行→
        保存/发送最终回复），其尾部又会再调本方法，直到队列清空，天然串行。
        调用方必须已持有 _msg_lock（_handle_message 尾部 / /push 空闲分支），
        保证与并发普通消息不产生 history 乱序；合成 message_id 绕过 _seen_msg_ids 去重。
        """
        q = self._push_queues.get(from_user)
        while q:
            next_text = q.pop(0)
            self._push_seq += 1
            fake_msg = {
                "from_user_id": from_user,
                "context_token": context_token,
                "message_id": f"push_{self.user_id}_{time.time_ns()}_{self._push_seq}",
                "item_list": [{"type": 1, "text_item": {"text": next_text}}],
            }
            logger.info("[微信Bot:%s] flush push 队列消息: %s", self.user_id, next_text[:80])
            await self._handle_message(fake_msg)

    async def _dispatch_message(self, msg: dict) -> None:
        """消息分派：命令消息即时并行处理；普通对话消息串行排队。

        背景：agent 执行可能耗时数十秒，若 _poll_loop 串行 await _handle_message，
        执行期间用户发送的 /stop 根本不会被轮询到（轮询被 agent 执行阻塞），
        无法中断正在执行的任务。这里把命令消息（含 /stop）从串行队列拆出来
        即时处理；普通对话消息仍用 _msg_lock 串行化，保证会话写入与 agent
        执行的顺序一致（并发处理普通消息会导致 history 乱序、回复错位）。
        """
        if _extract_text(msg).strip().startswith("/"):
            # 命令类消息：即时处理（不排队）。除 /push 空闲直发外，
            # 命令分支都不跑 agent（各自直接 return），与正在执行的 agent 并发安全；
            # /stop 需要立即响应才可达中断目的，/push 忙时只入队不打断。
            await self._handle_message(msg)
            return
        async with self._msg_lock:
            await self._handle_message(msg)

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
        text = _extract_text(msg)

        # ── 纯图片消息（无文本）：暂存，等待后续文本合并 ──
        if not text:
            if image_data:
                img_msg_id = image_data.get("msg_id", "")
                # 如果这张图片已经处理过，不重复暂存（防御 iLink 重投）
                if img_msg_id and img_msg_id in self._seen_msg_ids:
                    logger.debug("[微信Bot] 跳过重复图片: %s", img_msg_id[:20])
                    return
                self._pending_images[from_user] = {"data": image_data, "time": time.time()}
                self._mark_activity()
                logger.info("[微信Bot:%s] 收到用户 %s 的图片，已暂存等待后续文字提问", self.user_id, from_user[:16])
            return

        # ── 消息去重（用微信 message_id，iLink 重投时该值不变）──
        message_id = msg.get("message_id", "")
        if message_id and message_id in self._seen_msg_ids:
            logger.debug("[微信Bot] 跳过重复消息: %s", text[:60])
            return
        if message_id:
            self._seen_msg_ids.add(message_id)
        self._mark_activity()

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

        # ── 命令处理（/new /list /switch /delete /projects /project /unproject
        #   /sessions /modals /modal /push /stop /help）── 实现见 wechat_commands.py。
        # 命令分支都不跑 agent、各自 send_message 后 return；返回 True 表示已处理。
        if text.strip().startswith("/") and await self._handle_command(text, from_user, context_token, wechat_uid):
            return
        # ── 会话管理 ──
        # 用户发起了真正的对话，会话列表可能已变化，序号映射失效
        self._wechat_session_menu.pop(from_user, None)
        current_pid = self._wechat_current_project.get(from_user, "")
        session_id = self._wechat_sessions.get(from_user)
        if current_pid:
            # 有当前项目：对话必须落在该项目下的会话。
            # 当前会话属于该项目 → 继续；否则自动切到项目下最近更新的会话，
            # 无则新建一个归属该项目的会话（保证「切项目后指令都对项目执行」）。
            if session_id:
                _cur = session_store.get_session(wechat_uid, session_id)
                if not (_cur and (_cur.get("project_id") or "") == current_pid):
                    session_id = None
            if session_id is None:
                proj_sessions = session_store.list_sessions_by_project(wechat_uid, current_pid)
                if proj_sessions:
                    session_id = proj_sessions[0]["id"]
                else:
                    session_id = hashlib.md5((from_user + ":" + current_pid).encode()).hexdigest()[:8]
                self._wechat_sessions[from_user] = session_id
                if not session_store.get_session(wechat_uid, session_id):
                    session_store.create_session(
                        wechat_uid,
                        title=text[:20],
                        session_id=session_id,
                        project_id=current_pid,
                    )
        elif session_id is None:
            # 无项目：首次消息用微信用户 ID 的 md5 作为稳定会话 ID（原行为）
            session_id = hashlib.md5(from_user.encode()).hexdigest()[:8]
            self._wechat_sessions[from_user] = session_id
            if not session_store.get_session(wechat_uid, session_id):
                session_store.create_session(
                    wechat_uid,
                    title=text[:20],
                    session_id=session_id,
                    project_id=None,
                )

        # ── 加载会话历史（必须在保存当前用户消息之前，否则当前消息会被算进历史导致重复）──
        # agent 的 checkpoint（MemorySaver）是内存态、重启即丢；从 session_store 拉取
        # 持久化历史传入 agent，保证重启后上下文不丢（Web 端同模式）。这是修复
        # 「重启后上下文为空 → agent 乱逛共享工作区 → 串出别的用户内容」的关键。
        session = session_store.get_session(wechat_uid, session_id)
        history = (session or {}).get("messages", []) if session else []
        if history:
            logger.debug("[微信Bot:%s] 会话 %s 加载历史 %d 条", self.user_id, session_id, len(history))

        # ── 检查是否有暂存的图片，合并为图文消息 ──
        attachments = None

        # 优先使用当前消息中自带的图片，其次使用之前暂存的图片
        pending_entry = self._pending_images.pop(from_user, None)
        img_to_use = image_data or (pending_entry["data"] if pending_entry else None)
        if img_to_use:
            try:
                data_url = await self._download_image_as_data_url(img_to_use)
                if data_url:
                    mime_type = data_url.split(";")[0].split(":")[1] if ";" in data_url else "image/png"
                    # 同时设置 attachments 供 agent 处理，并保存 JSON 格式供前端渲染
                    attachments = [{"mime_type": mime_type, "data_url": data_url}]
                    img_text = f"[图片: {img_to_use.get('msg_id', '')[-8:]}]"
                    payload = json.dumps(
                        {"text": text or img_text, "images": [data_url]},
                        ensure_ascii=False,
                    )
                    session_store.add_message(wechat_uid, session_id, "user", payload)
                    logger.info("[微信Bot:%s] 合并图片+文本消息", self.user_id)
            except Exception as e:
                logger.warning("[微信Bot:%s] 图片下载/转换失败: %s", self.user_id, e)
                # 降级为纯文本保存
                session_store.add_message(wechat_uid, session_id, "user", text)
        else:
            # 无图片时清理可能的残留（另一用户的 pending 不会被误pop）
            self._pending_images.pop(from_user, None)

        # 保存用户文本消息（无图片时的纯文本）
        if not img_to_use:
            add_ret = session_store.add_message(wechat_uid, session_id, "user", text)
            if add_ret is None:
                logger.warning("[微信Bot:%s] 用户消息保存失败: session=%s 不存在", self.user_id, session_id)

        # 发送"正在输入"状态
        await self.send_typing(from_user, context_token)

        # 流式调用 agent：思考与每步工具执行即时分段回复，最终回复收集后统一保存/发送。
        # 开启 step 分段回复时，用户无需等 agent 全部处理完才看到第一条反馈。
        # 官方 iLink 对 sendmessage 无公开条数配额，频控是频率型（ret=-2 rate limited）；
        # 这里用「批量合并省条数 + 滑动窗口限速 + 失败退避重试」保证过程消息持续可见，
        # step_msg_budget 仅作极端刷屏的防御性上限（默认 30 条），最终回复不受限。
        # agent 执行包成独立任务（self._active_run_task）：/stop 命令通过 cancel 它中断请求，
        # 被中断时下方捕获 CancelledError，产出"⏹️ 任务已中断"作为最终回复（复用统一保存/发送链路）。
        async def _run_agent_body() -> tuple[str, list[dict]]:
            reply = ""
            pending_thought = ""  # 缓存 thought，合并到下一个 step 批一起发，减少消息条数
            pending_step_lines: list[str] = []  # 批量 step 缓冲：攒满 batch 条或超时即合并发送
            pending_step_since: float = time.time()  # 攒批起始时间，配合 step_msg_batch_timeout 保证反馈及时
            collected_steps: list[dict] = []  # 收集步骤卡片，随最终回复保存（Web 端回放渲染工具卡片）
            self._step_sent_count = 0  # 每轮任务重置 step 消息预算计数
            self._step_pending_queue.clear()  # 每轮任务清空暂存队列
            agent = self._ensure_agent()
            try:
                # 工具调用间歇期持续发送 typing，让用户手机端看到"正在输入"
                typing_stop = asyncio.Event()

                async def _typing_loop():
                    while not typing_stop.is_set():
                        await self.send_typing(from_user, context_token)
                        try:
                            await asyncio.wait_for(typing_stop.wait(), timeout=4)
                        except asyncio.TimeoutError:
                            continue
                        else:
                            break

                typing_task = asyncio.create_task(_typing_loop())
                # 串行化 agent 执行段：工具工作区（file_tools/shell_tools/browser_tools）是
                # 模块级全局状态，_apply_session_workspace 会改写它。若两个用户的微信消息
                # 并发处理，A 的工具调用会读到 B 的工作区文件（跨用户内容串扰的根因之一）。
                # 用全局锁保证同一时刻只有一个用户在跑 agent。
                async with _WECHAT_AGENT_LOCK:
                        # 根据当前会话/项目设置工具工作目录（全局工具工作区，与 agent 调用同锁）
                        try:
                            from agent_core.services.agent_service import _apply_session_workspace
                            _apply_session_workspace(wechat_uid, session_id, self._wechat_current_project.get(from_user, ""))
                        except Exception:
                            pass
                        # 同步该用户专属 agent 的工作区（_apply_session_workspace 只设置全局 Web agent）
                        try:
                            from services.workspace import _workspace_for_user
                            eff_ws = session_store.get_session_workspace(wechat_uid, session_id) or str(_workspace_for_user(wechat_uid))
                            agent.set_workspace(str(Path(eff_ws).expanduser().resolve()))
                        except Exception:
                            pass
                        async for ev in agent.chat_stream_events(
                            text, attachments=attachments, thread_id=session_id, history=history,
                        ):
                            et = ev.get("type")
                            # 步骤卡片一律收集（无论 step_reply_enabled），随最终回复保存，
                            # 供 Web 端回放渲染工具卡片（与 Web 端 /run/stream 的 collected_steps 一致）
                            if et == "thought":
                                thought = str(ev.get("thought", "")).strip()
                                if thought:
                                    collected_steps.append({"type": "thought", "thought": thought[:200]})
                                    if self.step_reply_enabled:
                                        pending_thought = thought[:200]
                            elif et == "tool_start":
                                collected_steps.append(ev)
                            elif et == "tool_result":
                                collected_steps.append(ev)
                                if not self.step_reply_enabled:
                                    continue
                                tool = ev.get("tool", "")
                                result = str(ev.get("result", "") or "").strip()
                                ok = not ev.get("error")
                                dur = ev.get("duration_ms") or 0
                                dur_txt = f"（{dur / 1000:.1f}s）" if dur else ""
                                snippet = result[:150].replace("\n", " ")
                                # step 消息 markdown 化：工具名加粗、结果摘要用引用块，便于阅读。
                                # 每个工具仍是 pending_step_lines 的一个元素（内部多行），
                                # 攒批条件按工具数计（len(pending_step_lines) >= step_msg_batch）。
                                tool_line = f"{'✅' if ok else '❌'} **{tool}**{dur_txt}"
                                pending_step_lines.append(
                                    f"{tool_line}\n> {snippet}" if snippet else tool_line
                                )
                                # 攒批：满 batch 条或超过 batch_timeout 秒即合并发送（省配额 + 反馈及时）
                                if len(pending_step_lines) >= self.step_msg_batch or (
                                    pending_step_lines and time.time() - pending_step_since >= self.step_msg_batch_timeout
                                ):
                                    for batch_text in self._build_step_batches(pending_step_lines, self.step_msg_batch, pending_thought):
                                        await self._send_step_msg(from_user, context_token, batch_text)
                                    pending_step_lines.clear()
                                    pending_thought = ""
                                    pending_step_since = time.time()
                            elif et == "context_compacted":
                                # 压缩报告也收集，随最终回复保存供 Web 端回放
                                collected_steps.append(ev)
                            elif et == "done":
                                reply = ev.get("content", "")
                                # flush 残余批量 step（不足 batch 条的尾批也合并一条发出去，不留过程死角）
                                if self.step_reply_enabled and pending_step_lines:
                                    for batch_text in self._build_step_batches(pending_step_lines, self.step_msg_batch, pending_thought):
                                        await self._send_step_msg(from_user, context_token, batch_text)
                                    pending_step_lines.clear()
                                    pending_thought = ""
                            elif et == "error":
                                content = ev.get("content", "")
                                if not reply:
                                    reply = f"❌ {content}"
            except Exception as e:
                logger.exception("[微信Bot] agent 调用异常")
                reply = f"❌ 处理出错: {e}"
            finally:
                typing_stop.set()
                typing_task.cancel()
            return reply, collected_steps

        self._active_run_task = asyncio.create_task(_run_agent_body())
        try:
            reply, collected_steps = await self._active_run_task
        except asyncio.CancelledError:
            # /stop 中断：回复置为"任务已中断"，走下方统一保存/发送链路，
            # 用户收到明确确认，且会话中留档（Web 端回放可见）。
            logger.info("[微信Bot:%s] 用户 %s 的任务已被 /stop 中断", self.user_id, from_user[:16])
            reply = "⏹️ 任务已中断"
            collected_steps = []
        finally:
            self._active_run_task = None

        # 保存助手回复：与 Web 端同格式（{"text":..., "steps":[...]} JSON），
        # 使微信 bot 消息在 Web 端回放时也能渲染工具卡片。
        # 复用 _save_assistant_result：内部 _strip_screenshot_urls 同时剥离过期截图引用
        # （token 会清理，原样保存会在回放时渲染破图）。
        if reply:
            saved = False
            try:
                from services.agent_service import _save_assistant_result
                _save_assistant_result(wechat_uid, session_id, text, reply, collected_steps or None)
                saved = True
                logger.info("[微信Bot:%s] 会话 %s 已保存助手回复+steps (%d 字符, %d 条步骤)",
                            self.user_id, session_id, len(reply), len(collected_steps or []))
            except Exception as e:
                logger.warning("[微信Bot:%s] _save_assistant_result 保存失败，回退纯文本保存: %s", self.user_id, e)
            if not saved:
                reply_ret = session_store.add_message(wechat_uid, session_id, "assistant", reply)
                if reply_ret is None:
                    logger.warning("[微信Bot:%s] 助手回复保存失败: session=%s 不存在", self.user_id, session_id)
                else:
                    logger.info("[微信Bot:%s] 会话 %s 已保存助手回复 (%d 字符)", self.user_id, session_id, len(reply))
            sess = session_store.get_session(wechat_uid, session_id)
            if sess and sess.get("message_count", 0) <= 2:
                short = text[:30] + ("..." if len(text) > 30 else "")
                session_store.rename_session(wechat_uid, session_id, short)

        # 发送回复（含图片检测 + 图片/文本分开发送 + 频控冷却感知）
        await self._send_final_reply(from_user, context_token, reply)

        # push 队列：当前任务（及其最终回复发送）已结束，按序处理 /push 入队的内容。
        # 此处必然持有 _msg_lock（普通消息路径），递归 _handle_message 天然串行。
        await self._flush_push_queue(from_user, context_token)

    async def _send_final_reply(self, from_user: str, context_token: str, reply: str) -> None:
        """发送最终回复（图片 + 文本），失败转入后台补发队列。

        2026-08-03 事故链：最终回复发送失败 → 入补发队列 → 补发/step 每次都在
        微信频控窗口（实测 ≤12 分钟，prepare failed 每次失败都刷新窗口）内撞上，
        窗口被不断刷新 → 45 分钟不解除 → 7 轮补发耗尽后放弃、回复永久丢失。
        因此这里与 step 消息一致：**频控冷却中绝不发送**，冷却中直接入队、
        等冷却结束由 _retry_loop 再发，避免冷却中强发刷新微信侧窗口、把频控拉得更长。
        """
        if not reply:
            return
        import re
        screenshot_urls = re.findall(
            r'!\[([^\]]*)\]\(/api/screenshot\?token=([^)]+)\)',
            reply,
        )

        # 有截图引用：下载并发送图片，剩余文本作为文字发送
        sent_image = False
        text_reply = reply
        if screenshot_urls:
            # 从 agent workspace 查找截图文件
            ws = Path(self._ensure_agent().config.workspace)
            for alt_text, token in screenshot_urls:
                try:
                    if ws:
                        img_path = ws / ".browser_screenshots" / f"{token}.png"
                        if img_path.exists():
                            send_resp = await self.send_image(
                                from_user, context_token, str(img_path),
                            )
                            if send_resp.get("ret", -1) == 0:
                                sent_image = True
                                # 从文本中去掉已发送图片的 markdown
                                text_reply = text_reply.replace(
                                    f"![{alt_text}](/api/screenshot?token={token})", "",
                                )
                            # 图片发送失败：保留引用（文本里会带 URL，聊胜于无）
                except Exception as e:
                    logger.warning("[微信Bot:%s] 发送截图失败: %s", self.user_id, e)

        # 发送剩余的文本（去掉图片引用后的纯净文本）
        # 最终回复：不立即重试（max_retries=0），失败直接入补发队列。
        clean_text = re.sub(r'\n{3,}', '\n\n', text_reply).strip()
        if not clean_text:
            if sent_image:
                logger.info("[微信Bot:%s] 回复仅为图片，已发送", self.user_id)
            return

        # 频控冷却中（2026-08-03 事故：冷却中强发会刷新微信侧窗口，延长频控）：
        # 不撞、直接入补发队列，由 _retry_loop 在冷却结束后发送。
        if time.time() < self._rate_limited_until:
            logger.info("[微信Bot:%s] 最终回复在频控冷却中，直接入补发队列（剩余 %.0fs）",
                        self.user_id, self._rate_limited_until - time.time())
            self._schedule_retry(from_user, context_token, clean_text)
            return

        await self._throttle_send()
        send_resp = await self.send_message(from_user, context_token, clean_text, max_retries=0)
        if send_resp.get("ret", -1) != 0:
            logger.warning("[微信Bot:%s] 最终回复发送失败，转入后台补发: resp=%s",
                           self.user_id, json.dumps(send_resp, ensure_ascii=False)[:300])
            self._schedule_retry(from_user, context_token, clean_text)
        else:
            logger.info("[微信Bot:%s] 回复: message_id=%s text=%s",
                        self.user_id, send_resp.get("message_id", ""), clean_text[:120])

    # ── 生命周期 ──────────────────────────────────

    async def start(self):
        """启动后台轮询任务"""
        if self._running:
            return
        if not self.bot_token:
            logger.warning("[微信Bot] 未登录，请先扫码")
            return

        # 跨进程互斥：同一账号(同一 data_dir)全局只允许一个进程轮询。
        # 用 fcntl 文件锁保证——若已有另一进程在轮询同一账号，这里抢锁失败，
        # 直接放弃启动轮询并告警，从根上杜绝"两个进程都收消息→双回复"的问题。
        # 进程被 kill 时 OS 会自动释放锁，因此无需手动清理也能自愈。
        lock_path = Path(self.data_dir) / "wechat_bot.lock"
        try:
            self._lock_fd = open(lock_path, "w")
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            logger.warning(
                "[微信Bot:%s] 未能获取轮询锁(另一进程可能已在轮询同一账号)，"
                "本实例不启动轮询以避免重复回复: %s", self.user_id, e
            )
            if self._lock_fd:
                self._lock_fd.close()
            self._lock_fd = None
            return

        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("[微信Bot:%s] 已启动(已获取轮询锁)", self.user_id)

    async def stop(self):
        """停止后台轮询"""
        self._running = False
        if self._task:
            self._task.cancel()
        # 停止最终回复补发任务
        if self._retry_task:
            self._retry_task.cancel()
            try:
                await self._retry_task
            except asyncio.CancelledError:
                pass
            self._retry_task = None
        # 停止前把未完成补发写盘，服务重启后可恢复（避免内存队列随进程丢失）
        self._persist_retry_queue()
        # 释放跨进程轮询锁，允许其他实例接管
        if self._lock_fd:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                self._lock_fd.close()
            except OSError:
                pass
            self._lock_fd = None
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
        """主轮询循环（含自适应间隔）"""
        IDLE_TIMEOUT = 60        # 无消息持续 N 秒后进入慢速模式
        FAST_INTERVAL = 0.0      # 活跃时无延迟（长轮询本身 hold 35s）
        SLOW_INTERVAL = 60.0     # 空闲时每 N 秒轮询一次（35s + 60s ≈ 每 1.5 分钟一次）

        while self._running:
            idle_time = time.time() - self._last_activity_at
            self._poll_delay = FAST_INTERVAL if idle_time < IDLE_TIMEOUT else SLOW_INTERVAL

            # 定期清理过期的暂存图片（每轮最多检查一次）
            if self._pending_images:
                self._cleanup_expired_pending_images()

            if self._poll_delay > 0:
                await asyncio.sleep(self._poll_delay)

            try:
                msgs = await self.poll_messages()
                # 收到消息后立即批量发送「正在输入」，不等 _handle_message 解析，提升感知响应速度
                for msg in msgs:
                    from_user = msg.get("from_user_id", "")
                    context_token = msg.get("context_token", "")
                    if from_user and context_token:
                        asyncio.create_task(self.send_typing(from_user, context_token))
                for msg in msgs:
                    if not self._running:
                        break
                    # 并行派发消息处理：agent 执行可能耗时数十秒，若串行 await，
                    # 期间用户发送的 /stop 无法被轮询到。_dispatch_message 内
                    # 命令消息即时处理、普通消息 _msg_lock 串行（顺序不乱）。
                    task = asyncio.create_task(self._dispatch_message(msg))
                    self._handle_tasks.add(task)
                    task.add_done_callback(self._handle_tasks.discard)
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

    def _mark_activity(self):
        """标记有消息活动，重置轮询为快速模式"""
        self._last_activity_at = time.time()
        self._poll_delay = 0.0

    def _cleanup_expired_pending_images(self):
        """清理超时未消费的暂存图片，防止内存泄漏。"""
        now = time.time()
        expired = [uid for uid, entry in self._pending_images.items()
                   if now - entry.get("time", 0) > PENDING_IMAGE_TTL]
        if expired:
            for uid in expired:
                self._pending_images.pop(uid, None)
            logger.info("[微信Bot:%s] 已清理 %d 条超时暂存图片（TTL=%ds）",
                        self.user_id, len(expired), PENDING_IMAGE_TTL)