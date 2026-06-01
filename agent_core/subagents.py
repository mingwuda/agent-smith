"""Subagent runtime with a synchronous MVP and task-state model for future parallel execution."""
from __future__ import annotations
import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from urllib.parse import urlparse

from config import AgentConfig
from network_resolver import configure_host_resolution


TaskStatus = Literal["pending", "running", "done", "error"]


@dataclass
class SubagentTask:
    id: str
    agent_type: str
    task: str
    context: str = ""
    status: TaskStatus = "pending"
    result: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0
    finished_at: float = 0


SUBAGENT_PROMPTS = {
    "coder": (
        "你是 coder 子代理，负责实现清晰、可验证、符合现有代码风格的代码修改。"
        "优先给出可执行方案、关键文件、风险和验证方式。"
    ),
    "reviewer": (
        "你是 reviewer 子代理，负责代码审查。重点找 bug、回归风险、边界条件、缺失测试。"
        "不要做无依据的风格建议；按严重程度输出。"
    ),
    "debugger": (
        "你是 debugger 子代理，负责系统化排查问题。先列假设，再给验证步骤和最可能根因。"
    ),
}


class SubagentManager:
    """Stores subagent tasks now; can run them concurrently in a later task API."""

    def __init__(self):
        self._tasks: dict[str, SubagentTask] = {}
        self._config: AgentConfig | None = None
        self._tools: list = []

    def configure(self, config: AgentConfig, tools: list):
        self._config = config
        self._tools = [item for item in tools if getattr(item, "name", "") != "delegate_task"]

    def list_agent_types(self) -> list[str]:
        return sorted(SUBAGENT_PROMPTS)

    def get_task(self, task_id: str) -> SubagentTask | None:
        return self._tasks.get(task_id)

    async def run_sync(self, task: str, agent_type: str = "coder", context: str = "") -> SubagentTask:
        if not self._config:
            raise RuntimeError("SubagentManager 尚未初始化")
        agent_type = agent_type if agent_type in SUBAGENT_PROMPTS else "coder"
        item = SubagentTask(
            id=f"subagent-{uuid.uuid4().hex[:12]}",
            agent_type=agent_type,
            task=task,
            context=context,
        )
        self._tasks[item.id] = item
        item.status = "running"
        item.started_at = time.time()
        try:
            item.result = await self._run_agent(item)
            item.status = "done"
        except Exception as exc:
            item.error = f"{type(exc).__name__}: {exc}"
            item.status = "error"
            item.result = f"❌ 子代理执行失败：{item.error}"
        finally:
            item.finished_at = time.time()
        return item

    async def _run_agent(self, item: SubagentTask) -> str:
        assert self._config is not None
        if self._config.base_url:
            host = urlparse(self._config.base_url).hostname
            if host:
                configure_host_resolution(host, self._config.api_host_ips)
        llm = ChatOpenAI(
            model=self._config.model,
            api_key=self._config.api_key,
            base_url=self._config.base_url or None,
            temperature=0,
            max_retries=self._config.api_max_retries,
            timeout=self._config.api_timeout_seconds,
        )
        prompt = (
            f"{SUBAGENT_PROMPTS[item.agent_type]}\n\n"
            "你是主代理派发出的子代理。你的输出会返回给主代理整合。"
            "保持聚焦，不要假装可以调用不存在的并行/团队工具。"
            "如果需要修改文件，说明建议和风险；如果已调用工具完成修改，列出验证结果。\n"
        )
        graph = create_react_agent(llm, self._tools, prompt=prompt)
        message = item.task
        if item.context:
            message = f"上下文：\n{item.context}\n\n任务：\n{item.task}"
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content=message)]},
            {"recursion_limit": max(1, int(self._config.recursion_limit or 60))},
        )
        for msg in reversed(result.get("messages", [])):
            if getattr(msg, "type", "") == "ai" and getattr(msg, "content", ""):
                return msg.content
        return "（子代理未产生输出）"


manager = SubagentManager()


def _run_coro_in_thread(coro):
    result = {}

    def runner():
        try:
            result["value"] = asyncio.run(coro)
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


@tool
def delegate_task(task: str, agent_type: str = "coder", context: str = "") -> str:
    """把一个子任务委派给子代理执行并同步等待结果。agent_type 可选 coder、reviewer、debugger。"""
    item = _run_coro_in_thread(manager.run_sync(task=task, agent_type=agent_type, context=context))
    return (
        f"子代理任务 {item.id} [{item.agent_type}] 状态：{item.status}\n\n"
        f"{item.result or item.error}"
    )


TOOLS = [delegate_task]
