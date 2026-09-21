"""会话级 Inbox：用户执行中实时干预消息的暂存与注入（参考 dsh 双桶模型）。

两个桶：
- next_step：注入「当前正在跑的回合」下一步（steering），在每次 LLM 调用边界被 claim。
- next_turn：排到「当前回合结束后」处理（入列下轮）。

后端 agent 跑在独立 driver（与 HTTP 解耦，见 agent.py _drive_agent_stream），
因此注入不打断正在执行的工具，只在模型下一次被调用前生效。

线程/协程安全：Web(HTTP 任务) 与 driver(asyncio 任务) 并发安全（同步锁即可，asyncio 单线程不阻塞）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class SessionInbox:
    """单个会话的在途干预消息桶。"""

    def __init__(self, key: str) -> None:
        self.key = key
        self._next_step: list[dict] = []   # wait: [{"id","content","ts"}]
        self._next_turn: list[dict] = []
        self._lock = threading.Lock()      # Web(HTTP) 与 driver(asyncio) 并发写；同步锁即可（同线程不阻塞）
        self._active = False   # 会话是否有一个 run 正在执行（driver 挂上才置 True）

    @property
    def active(self) -> bool:
        return self._active

    def set_active(self, active: bool) -> None:
        self._active = active

    def append(self, target: str, content: str) -> dict:
        """写入一个干预消息。target: "step" 或 "turn"。返回写入的消息 dict。"""
        msg = {"id": _new_id(), "content": content, "ts": time.time()}
        with self._lock:
            if target == "step":
                self._next_step.append(msg)
            else:
                self._next_turn.append(msg)
        return msg

    def claim_next_step(self, max_items: int = 8) -> list[dict]:
        """取出当前回合下一步要注入的所有消息（FIFO）。只取 step 桶。"""
        with self._lock:
            batch = self._next_step[:max_items]
            del self._next_step[:max_items]
        return batch

    def claim_next_turn(self, max_items: int = 8) -> list[dict]:
        """当前回合结束后取出待下轮处理的消息。"""
        with self._lock:
            batch = self._next_turn[:max_items]
            del self._next_turn[:max_items]
        return batch

    def peek_step(self) -> list[dict]:
        with self._lock:
            return list(self._next_step)

    def turn_counts(self) -> dict:
        with self._lock:
            return {"step": len(self._next_step), "turn": len(self._next_turn)}


class InboxManager:
    """全局 Inbox 注册表：key=(uid, session_id) -> SessionInbox。"""

    def __init__(self) -> None:
        self._inboxes: dict[str, SessionInbox] = {}
        self._g_lock = threading.Lock()
        self._data_dir: Optional[Path] = None
        self._inject_events: dict[str, list[dict]] = {}   # key -> 已注入消息事件（供 stream 层转发 SSE）
        self._ev_lock = threading.Lock()

    def record_injected(self, uid: str, session_id: str, content: str) -> None:
        """pre_hook 注入成功后记录事件，供 stream_run 转成 SSE 告知前端。幂等记录。"""
        key = f"{uid}:{session_id}"
        with self._ev_lock:
            self._inject_events.setdefault(key, []).append(
                {"type": "user_message_injected", "content": content}
            )

    def drain_injected(self, uid: str, session_id: str) -> list[dict]:
        """取出该会话待转发的注入事件（stream_run 每轮心跳/事件后 drain）。"""
        key = f"{uid}:{session_id}"
        with self._ev_lock:
            return self._inject_events.pop(key, [])

    def configure(self, data_dir: Path | str) -> None:
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, key: str) -> Path:
        if self._data_dir is None:
            return Path(f"/tmp/agent_inbox_{key}.json")
        return self._data_dir / f"inbox_{_safe(key)}.json"

    def get(self, uid: str, session_id: str) -> SessionInbox:
        key = f"{uid}:{session_id}"
        with self._g_lock:
            inbox = self._inboxes.get(key)
            if inbox is None:
                inbox = SessionInbox(key)
                self._inboxes[key] = inbox
            return inbox

    def mark_run_active(self, uid: str, session_id: str, active: bool) -> None:
        self.get(uid, session_id).set_active(active)

    def persist(self, uid: str, session_id: str) -> None:
        """把当前未消费的干预消息落盘（刷新/重连不丢）。"""
        inbox = self._inboxes.get(f"{uid}:{session_id}")
        if inbox is None:
            return
        path = self._path_for(f"{uid}:{session_id}")
        state = {
            "step": getattr(inbox, "_next_step", []),
            "turn": getattr(inbox, "_next_turn", []),
        }
        Path(path).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def restore(self, uid: str, session_id: str) -> None:
        path = self._path_for(f"{uid}:{session_id}")
        if not path.exists():
            return
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        inbox = self.get(uid, session_id)
        inbox._next_step = state.get("step", [])
        inbox._next_turn = state.get("turn", [])
        path.unlink(missing_ok=True)


_inbox_manager: Optional[InboxManager] = None


def get_inbox_manager() -> InboxManager:
    global _inbox_manager
    if _inbox_manager is None:
        _inbox_manager = InboxManager()
    return _inbox_manager


def _new_id() -> str:
    return f"inj_{int(time.time()*1000)}_{id(object())}"


def _safe(key: str) -> str:
    return key.replace(":", "_").replace("/", "_").replace("\\", "_")