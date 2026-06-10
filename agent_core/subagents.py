"""Subagent runtime with a synchronous MVP and task-state model for future parallel execution."""
from __future__ import annotations
import asyncio
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Literal, Optional

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
    # 实时日志（线程安全，前端 SSE 轮询用）
    _log_lines: list[dict] = field(default_factory=list)
    _log_lock: threading.Lock = field(default_factory=threading.Lock)

    def append_log(self, text: str, cat: str = "info") -> None:
        with self._log_lock:
            self._log_lines.append({"ts": time.time(), "cat": cat, "text": text})

    def get_logs_since(self, index: int) -> tuple[list[dict], int]:
        with self._log_lock:
            return list(self._log_lines[index:]), len(self._log_lines)


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
    "searcher": (
        "你是 searcher 子代理，专精互联网搜索。你的唯一任务是：\n"
        "1. 调用 web_search 搜索指定关键词，获取结果摘要\n"
        "2. 从搜索结果中选择 2-4 个最相关的链接，调用 web_fetch 抓取正文\n"
        "3. 基于抓取内容整理出结构化的事实、数据、观点，标注来源\n"
        "4. 如果搜索无果或结果不相关，换关键词或换语言重试\n\n"
        "不要写文件、不要执行代码、不要调用其他工具。只做搜索和整理。"
        "输出格式：每条信息标注来源标题和链接。涉及时间信息时明确标注日期。"
    ),
}


# 各子代理类型可用的工具（None 表示全部可用，除了委托工具）
SUBAGENT_TOOL_WHITELIST: dict[str, Optional[list[str]]] = {
    "searcher": ["web_search", "web_fetch"],
}


class SubagentManager:
    """Stores subagent tasks now; can run them concurrently in a later task API."""

    def __init__(self):
        self._tasks: dict[str, SubagentTask] = {}
        self._config: Optional[AgentConfig] = None
        self._tools: list = []
        # 当前批次任务列表（按 capsule_id 索引），供前端 SSE 轮询用
        self._current_batch: list[SubagentTask] = []

    def configure(self, config: AgentConfig, tools: list):
        self._config = config
        all_tools = [
            item for item in tools
            if getattr(item, "name", "") not in {"delegate_task", "delegate_tasks_parallel"}
        ]
        self._tools = all_tools
        # 为各子代理类型预过滤工具
        self._tools_by_type: dict[str, list] = {}
        for agent_type in SUBAGENT_PROMPTS:
            whitelist = SUBAGENT_TOOL_WHITELIST.get(agent_type)
            if whitelist is None:
                self._tools_by_type[agent_type] = all_tools
            else:
                self._tools_by_type[agent_type] = [
                    t for t in all_tools if getattr(t, "name", "") in whitelist
                ]

    def list_agent_types(self) -> list[str]:
        return sorted(SUBAGENT_PROMPTS)

    def get_task(self, task_id: str) -> Optional[SubagentTask]:
        return self._tasks.get(task_id)

    def get_progress_logs(self, capsule_id: int) -> tuple[list[dict], int, bool]:
        """获取指定 capsule 的增量日志。返回 (新日志行, 总行数, 是否已完成)。"""
        idx = capsule_id - 1  # capsule_id 从 1 开始
        if 0 <= idx < len(self._current_batch):
            task = self._current_batch[idx]
            lines, total = task.get_logs_since(0)
            done = task.status in ("done", "error")
            return lines, total, done
        return [], 0, True

    def start_batch(self, tasks: list[SubagentTask]) -> None:
        self._current_batch = tasks

    def clear_batch(self) -> None:
        self._current_batch = []

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
        agent_tools = self._tools_by_type.get(item.agent_type, self._tools)
        graph = create_react_agent(llm, agent_tools, prompt=prompt)
        message = item.task
        if item.context:
            message = f"上下文：\n{item.context}\n\n任务：\n{item.task}"
        item.append_log(f"开始执行 {item.agent_type} 子代理任务...")
        item.append_log(f"任务: {message[:200]}")

        final_text = ""
        try:
            async for chunk in graph.astream(
                {"messages": [HumanMessage(content=message)]},
                {"recursion_limit": max(1, int(self._config.recursion_limit or 60))},
                stream_mode="values",
            ):
                msgs = chunk.get("messages", [])
                if not msgs:
                    continue
                last = msgs[-1]
                msg_type = getattr(last, "type", "")
                if msg_type == "tool":
                    tool_name = getattr(last, "name", "unknown")
                    item.append_log(f"🔧 调用工具: {tool_name}", "tool")
                elif msg_type == "ai":
                    content = getattr(last, "content", "")
                    if content:
                        item.append_log(f"💭 {content[:300]}", "ai")
                        final_text = content  # 实时记录最后一条 AI 回复
            # 流结束后从最后一条 AI 消息取完整输出（可能比 stream 逐条更长）
            msgs_final = chunk.get("messages", [])
            for msg in reversed(msgs_final):
                if getattr(msg, "type", "") == "ai" and getattr(msg, "content", ""):
                    final_text = msg.content
                    break
        except Exception as exc:
            item.append_log(f"❌ 执行出错: {exc}", "error")
            raise
        finally:
            if not final_text:
                final_text = "（子代理未产生输出）"
            item.append_log(f"✅ {item.agent_type} 完成", "done")
        return final_text


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
    """把一个子任务委派给子代理执行并同步等待结果。agent_type 可选 coder、reviewer、debugger、searcher。"""
    item = _run_coro_in_thread(manager.run_sync(task=task, agent_type=agent_type, context=context))
    return (
        f"子代理任务 {item.id} [{item.agent_type}] 状态：{item.status}\n\n"
        f"{item.result or item.error}"
    )


def _parallel_task_wrapper(task_def: dict) -> str:
    """Run a single subagent task in a thread pool worker."""
    item = _run_coro_in_thread(manager.run_sync(
        task=task_def["task"],
        agent_type=task_def.get("agent_type", "coder"),
        context=task_def.get("context", ""),
    ))
    return (
        f"任务 {item.id} [{item.agent_type}] 状态：{item.status}\n\n"
        f"{item.result or item.error}"
    )


@tool
def delegate_tasks_parallel(tasks_json: str) -> str:
    """并行派发多个独立子任务。tasks_json 是一个 JSON 数组，每个元素包含 task/agent_type/context 字段。
    适用于多个任务之间没有文件或数据依赖的场景。同一时间最多并行 4 个子代理。"""
    try:
        tasks = json.loads(tasks_json)
    except (json.JSONDecodeError, TypeError) as e:
        return f"❌ 参数解析失败: {e}，需要传入 JSON 数组字符串"

    if not isinstance(tasks, list) or len(tasks) == 0:
        return "❌ 需要至少一个任务"

    # 预创建所有任务，注册到 manager 供前端 SSE 轮询
    items = []
    for i, t in enumerate(tasks[:4]):
        agent_type = t.get("agent_type", "coder") if isinstance(t, dict) else "coder"
        if agent_type not in SUBAGENT_PROMPTS:
            agent_type = "coder"
        item = SubagentTask(
            id=f"subagent-{uuid.uuid4().hex[:12]}",
            agent_type=agent_type,
            task=t.get("task", "") if isinstance(t, dict) else str(t),
            context=t.get("context", "") if isinstance(t, dict) else "",
        )
        item.append_log(f"队列中，等待执行...")
        items.append(item)
        manager._tasks[item.id] = item

    manager.start_batch(items)

    def run_one(item: SubagentTask) -> str:
        try:
            item.status = "running"
            item.started_at = time.time()
            _run_coro_in_thread(manager._run_agent(item))
            item.status = "done"
        except Exception as exc:
            item.status = "error"
            item.error = f"{type(exc).__name__}: {exc}"
            item.result = f"❌ 子代理执行失败：{item.error}"
        finally:
            item.finished_at = time.time()
        return (
            f"任务 {item.id} [{item.agent_type}] 状态：{item.status}\n\n"
            f"{item.result or item.error}"
        )

    max_workers = min(len(items), 4)
    results = []
    errors = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(run_one, item): i for i, item in enumerate(items)}
        for future in as_completed(futures):
            idx = futures[future]
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                errors.append(f"任务 #{idx + 1} 失败: {type(e).__name__}: {e}")

    manager.clear_batch()
    parts = [f"✅ 并行子代理执行完成（{len(results)}/{len(tasks)} 成功）\n"]
    if results:
        parts.append("\n---\n".join(results))
    if errors:
        parts.append(f"\n\n❌ 失败任务：\n" + "\n".join(errors))
    return "\n".join(parts)


TOOLS = [delegate_task, delegate_tasks_parallel]
