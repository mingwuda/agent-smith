"""ask_user 工具：让 agent 在执行过程中向用户征询意见。

语义：agent 需要用户决策/输入时调用本工具，向用户展示一组选项 +
可选的自定义意见输入。工具会**暂停当前流程**（阻塞等待用户响应），
前端弹出问卷弹窗，用户提交后 agent 拿到结果继续后续阶段。

## 数据流
1. LLM 调用 ask_user(prompt, options, allow_free_text)
2. 工具注册一个 pending ask 到本模块的注册表（key=thread_key），
   生成 ask_id，设置一个 `asyncio.Event`，然后 **await** 该事件。
3. stream_run 的心跳循环每 ~2s drain 该线程的新 pending ask，
   以 `ask_user_modal` SSE 事件推给前端渲染弹窗。
4. 用户在前端弹窗选择/输入并提交 → POST /sessions/{sid}/ask/{ask_id}
   → 调用方 resolve_ask(thread_key, ask_id, answer) 写入答案并 set 事件。
5. 工具的 await 返回，拿到答案作为工具返回值 → on_tool_end 继续图执行。

## 事件循环说明
driver（agent_run）与 FastAPI HTTP 端点运行在**同一个 asyncio 事件循环**，
因此 `asyncio.Event` 在工具 await 端与 resolve 端之间可直接共享：
resolve 端（HTTP handler）在同一 loop 上 `ev.set()` 即唤醒等待端，无跨线程。
若未来 driver 与 HTTP 分离到不同 loop，需用 `run_coroutine_threadsafe` 迁移。

## 设计原则
- **阻塞但可超时**：工具默认等待 DEFAULT_WAIT_TIMEOUT（3600s），超时返回
  "用户未响应"降级文本，避免永久挂死 agent。
- **会话隔离**：注册表按 thread_key（"{uid}:{session_id}"）隔离。
- **失败静默**：绑定缺失/无会话时返回提示文本，绝不抛异常。
- **watchdog 免疫**：工具阻塞期间位于 running_tools → is_busy() 返回 True，
  空闲看门狗不会误杀长等待。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from typing import Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# 默认等待用户响应的最长时间（秒）。
DEFAULT_WAIT_TIMEOUT = 3600.0


class _PendingAsk:
    """一个待响应的征询请求。"""

    __slots__ = ("ask_id", "prompt", "options", "allow_free_text",
                 "created_at", "event", "answer", "answered", "timeout")

    def __init__(self, ask_id: str, prompt: str, options: list[str],
                 allow_free_text: bool, timeout: float):
        self.ask_id = ask_id
        self.prompt = prompt
        self.options = list(options)
        self.allow_free_text = allow_free_text
        self.created_at = time.time()
        self.timeout = timeout
        self.event = asyncio.Event()  # 与 driver/HTTP 同一 loop
        self.answer = ""
        self.answered = False


class AskRegistry:
    """按 thread_key 管理的征询注册表（线程安全）。

    - _asks: thread_key -> list[_PendingAsk]
    - _emitted: thread_key -> set[ask_id]，记录已通过 SSE 推给前端的 ask
    - 均在同一个 asyncio event loop 上操作（driver + FastAPI 共享 loop）
    """
    def __init__(self):
        self._asks: dict[str, list[_PendingAsk]] = {}
        self._emitted: dict[str, set[str]] = {}
        self._lock = threading.Lock()
        self.wait_timeout = DEFAULT_WAIT_TIMEOUT

    def register(self, thread_key: str, ask: _PendingAsk) -> None:
        with self._lock:
            self._asks.setdefault(thread_key, []).append(ask)
        logger.info("[ask_user] 已登记 ask_id=%s thread=%s prompt=%.60s options=%d",
                    ask.ask_id, thread_key, ask.prompt, len(ask.options))

    def pending_asks(self, thread_key: str) -> list[dict]:
        """返回该会话所有待响应 ask 的载荷（不弹出）。"""
        with self._lock:
            asks = self._asks.get(thread_key, [])
            return [
                {
                    "ask_id": a.ask_id,
                    "prompt": a.prompt,
                    "options": a.options,
                    "allow_free_text": a.allow_free_text,
                    "created_at": a.created_at,
                }
                for a in asks
            ]

    def mark_emitted(self, thread_key: str, ask_id: str) -> None:
        with self._lock:
            self._emitted.setdefault(thread_key, set()).add(ask_id)

    def not_emitted(self, thread_key: str, ask_id: str) -> bool:
        with self._lock:
            s = self._emitted.get(thread_key, set())
            return ask_id not in s

    def is_empty(self, thread_key: str) -> bool:
        with self._lock:
            return not self._asks.get(thread_key, [])

    def _find(self, thread_key: str, ask_id: str) -> Optional[_PendingAsk]:
        with self._lock:
            for a in self._asks.get(thread_key, []):
                if a.ask_id == ask_id:
                    return a
        return None

    def _remove(self, thread_key: str, ask_id: str) -> None:
        """从待办中删除一个 ask，若清空则连带移除 emitted 记录。"""
        with self._lock:
            lst = self._asks.get(thread_key)
            if lst:
                self._asks[thread_key] = [a for a in lst if a.ask_id != ask_id]
                if not self._asks[thread_key]:
                    self._asks.pop(thread_key, None)
                    self._emitted.pop(thread_key, None)

    def resolve(self, thread_key: str, ask_id: str, answer: str) -> bool:
        """用户提交答案：写入并唤醒等待方。找不到返回 False。"""
        ask = self._find(thread_key, ask_id)
        if ask is None:
            return False
        ask.answer = answer or ""
        ask.answered = True
        ask.event.set()
        self._remove(thread_key, ask_id)
        logger.info("[ask_user] 已解析 ask_id=%s thread=%s answer=%.80s",
                    ask_id, thread_key, answer)
        return True

    def cancel(self, thread_key: str, ask_id: str, reason: str = "") -> bool:
        """取消（如超时）：唤醒等待方并标记降级。返回 True=取消了。"""
        ask = self._find(thread_key, ask_id)
        if ask is None:
            return False
        ask.answer = reason or "用户未响应"
        ask.answered = True
        ask.event.set()
        self._remove(thread_key, ask_id)
        logger.info("[ask_user] 已取消 ask_id=%s thread=%s", ask_id, thread_key)
        return True

    def cleanup(self, thread_key: str) -> None:
        """会话结束时清理残留（防止内存泄漏）。"""
        with self._lock:
            for a in self._asks.get(thread_key, []):
                a.event.set()  # 唤醒等待方返回降级文本
            self._asks.pop(thread_key, None)
            self._emitted.pop(thread_key, None)

    async def wait(self, thread_key: str, ask_id: str) -> str:
        """阻塞等待该 ask 被 resolve/cancel/超时。返回答案字符串。"""
        ask = self._find(thread_key, ask_id)
        if ask is None:
            return "用户未响应"
        try:
            await asyncio.wait_for(ask.event.wait(), timeout=ask.timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self.cancel(thread_key, ask_id, "用户未响应（等待超时）")
            return "用户未响应（等待超时）"
        # 事件已 set：答案已由 resolve/cancel 写入 _find
        resolved = self._find(thread_key, ask_id)
        return resolved.answer if resolved else ask.answer or "用户未响应"


_registry = AskRegistry()


def get_registry() -> AskRegistry:
    return _registry


def _thread_key_from(config: RunnableConfig) -> str:
    return str((config or {}).get("configurable", {}).get("thread_id", ""))


@tool
async def ask_user(
    prompt: str,
    options: list[str] = None,
    allow_free_text: bool = False,
    config: RunnableConfig = None,
) -> str:
    """向用户征询意见：展示一组选项（+可选自定义输入），等待用户选择后返回。

    适用场景：agent 在执行中需要用户决策、确认、补充信息或做出选择，
    且这一步必须先得到用户答复才能继续。调用后当前流程会暂停，前端弹出
    问卷弹窗，用户提交后本工具返回其选择/输入的内容。

    参数:
      prompt: 要问用户的问题（明确、具体）。
      options: 可选的选项列表（简短、互斥、清晰）。一般 1~6 个。
      allow_free_text: 是否允许用户直接输入自定义意见（默认关闭）。
        建议在 options 不足以覆盖用户可能的答复时开启。
      config: 运行配置（由 LangGraph 注入，用于定位会话）。

    返回: 用户的答复文本（选中的选项，或用户自定义输入），
    或「用户未响应」降级文本。
    """
    if not config:
        return "【ask_user 未能在运行上下文中定位会话，已跳过征询】请据提示自行继续。"
    thread_key = _thread_key_from(config)
    if not thread_key:
        return "【ask_user 无法确定会话，已跳过征询】请据提示自行继续。"
    ask_id = f"ask_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}"
    if not options:
        options = ["确认", "取消"]
    opts = [str(o) for o in options][:12]  # 上限 12 个选项，防溢出
    ask = _PendingAsk(ask_id, str(prompt), opts, bool(allow_free_text),
                      _registry.wait_timeout)
    _registry.register(thread_key, ask)
    # 等待用户响应（心跳会把 pending ask 推给前端渲染弹窗）
    return await _registry.wait(thread_key, ask_id)


def resolve_ask(thread_key: str, ask_id: str, answer: str) -> bool:
    """供 API 层调用：解析一个 pending ask。"""
    return _registry.resolve(thread_key, ask_id, answer)


def cancel_ask(thread_key: str, ask_id: str, reason: str = "") -> bool:
    """供 API 层调用：取消一个 pending ask。"""
    return _registry.cancel(thread_key, ask_id, reason)


TOOLS = [ask_user]