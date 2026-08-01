"""Subagent runtime with a synchronous MVP and task-state model for future parallel execution."""
from __future__ import annotations
import asyncio
import json
import threading
import time
import uuid
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

    def append_log(self, text: str, cat: str = "info", **extra) -> None:
        with self._log_lock:
            line = {"ts": time.time(), "cat": cat, "text": text}
            line.update(extra)
            self._log_lines.append(line)

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
    "analysis": (
        "你是 analysis 子代理，负责分析与探索：代码结构、模块依赖、性能瓶颈、数据分布、可行性评估等。\n"
        "只做只读探查（读文件、搜索、git 查看、运行只读命令/脚本），绝不修改文件、绝不执行写操作。\n"
        "输出结构化分析报告：结论先行，附证据（文件:行号 / 命令输出），最后给建议。"
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


# 各子代理类型可用的工具（whitelist 优先；None 表示默认全量但排除联网类工具）
SUBAGENT_TOOL_WHITELIST: dict[str, Optional[list[str]]] = {
    "searcher": ["web_search", "web_fetch"],
    # analysis 只做只读探查：文件/搜索/git 查看/系统信息/数据库只读/记忆只读 + run_python/run_shell（prompt 约束只读）。
    # 不包含任何写文件/写 git/写记忆/委派/联网工具，天然杜绝分析过程中误改代码。
    "analysis": [
        "read_file", "read_bytes", "list_files", "search_files", "get_workspace_path",
        "run_python", "run_shell",
        "git_status", "git_diff", "git_log", "git_show", "git_command",
        "get_system_info", "list_loaded_skills",
        "db_schema", "db_query", "db_connections",
        "recall_memory", "list_memories",
    ],
}

# 非 searcher 子代理默认排除的联网类工具前缀：
# 本地文件/代码/系统任务不需要联网，coder/reviewer/debugger 不应拿到 web_search/web_fetch/浏览器工具，
# 否则 LLM 会把本地任务误判成需要联网搜索（如"清理目录"任务跑去 web_search）。
SUBAGENT_TOOL_EXCLUDE_PREFIXES: tuple[str, ...] = ("web_", "browser_")


class SubagentManager:
    """Stores subagent tasks now; can run them concurrently in a later task API."""

    def __init__(self):
        self._tasks: dict[str, SubagentTask] = {}
        self._config: Optional[AgentConfig] = None
        self._tools: list = []
        self._review_llm: Optional[ChatOpenAI] = None  # 审核子代理用独立模型
        self._graph_cache: dict[str, object] = {}  # agent_type -> 预构建的 React 图（LLM/tools 复用）
        # 当前批次任务列表（按 capsule_id 索引），供前端 SSE 轮询用
        self._current_batch: list[SubagentTask] = []

    def configure(self, config: AgentConfig, tools: list, review_llm=None):
        self._config = config
        self._review_llm = review_llm
        all_tools = [
            item for item in tools
            if getattr(item, "name", "") not in {"delegate_task", "delegate_tasks_parallel"}
        ]
        self._tools = all_tools
        # 为各子代理类型预过滤工具
        self._tools_by_type: dict[str, list] = {}
        for agent_type in SUBAGENT_PROMPTS:
            whitelist = SUBAGENT_TOOL_WHITELIST.get(agent_type)
            if whitelist is not None:
                self._tools_by_type[agent_type] = [
                    t for t in all_tools if getattr(t, "name", "") in whitelist
                ]
            else:
                # 默认全量，但排除联网类工具（web_/browser_ 前缀）：
                # 本地任务不需要联网，避免 coder/reviewer/debugger 误用搜索/浏览器
                self._tools_by_type[agent_type] = [
                    t for t in all_tools
                    if not getattr(t, "name", "").startswith(SUBAGENT_TOOL_EXCLUDE_PREFIXES)
                ]
        # 预构建各类型子代理的 React 图（LLM 实例 + prompt + 工具集一次成型），
        # 运行期所有任务共享同一 graph 并发 astream —— langgraph 每次调用独立 state，并发安全。
        # 这比之前每个任务新建 ChatOpenAI + create_react_agent 省掉重复建图开销，
        # 也是并行改造的前提：并发任务共享同一连接池，I/O 真正并行。
        # 预构建失败时静默降级（留 None，_run_agent 按需现建）——configure 的核心职责是工具过滤，
        # 不能被建图阻塞（如测试/异常配置下传占位 config）。
        self._graph_cache = {}
        for agent_type in SUBAGENT_PROMPTS:
            try:
                self._graph_cache[agent_type] = self._build_subagent_graph(agent_type)
            except Exception:
                self._graph_cache[agent_type] = None

    def _build_subagent_llm(self, agent_type: str):
        """构建单个子代理类型的 LLM 实例（reviewer 用审核模型，其余用主模型配置）。"""
        if agent_type == "reviewer" and self._review_llm is not None:
            return self._review_llm
        assert self._config is not None
        if self._config.base_url:
            host = urlparse(self._config.base_url).hostname
            if host:
                configure_host_resolution(host, self._config.api_host_ips)
        return ChatOpenAI(
            model=self._config.model,
            api_key=self._config.api_key or "sk-no-key-required",
            base_url=self._config.base_url or None,
            temperature=0,
            max_retries=self._config.api_max_retries,
            timeout=self._config.api_timeout_seconds,
        )

    def _build_subagent_prompt(self, agent_type: str) -> str:
        prompt = (
            f"{SUBAGENT_PROMPTS[agent_type]}\n\n"
            "你是主代理派发出的子代理。你的输出会返回给主代理整合。"
            "保持聚焦，不要假装可以调用不存在的并行/团队工具。"
            "如果需要修改文件，说明建议和风险；如果已调用工具完成修改，列出验证结果。\n"
        )
        if agent_type != "searcher":
            prompt += (
                "\n工具使用约束：你没有联网工具（web_search / web_fetch / 浏览器）可用，"
                "本地文件/代码/系统任务不需要联网。需要最新外部信息时，"
                "在最终输出中列出所需关键词，由主代理另行处理。\n"
            )
        return prompt

    def _build_subagent_graph(self, agent_type: str):
        llm = self._build_subagent_llm(agent_type)
        agent_tools = self._tools_by_type.get(agent_type, self._tools)
        return create_react_agent(
            llm, agent_tools, prompt=self._build_subagent_prompt(agent_type)
        )

    def list_agent_types(self) -> list[str]:
        return sorted(SUBAGENT_PROMPTS)

    def get_task(self, task_id: str) -> Optional[SubagentTask]:
        return self._tasks.get(task_id)

    def get_capsule_tool_events(self, capsule_id: int) -> list[dict]:
        """返回指定 capsule 已产生的结构化工具事件（tool_start/tool_end）。

        subagent_end 时由 agent_run 打包进 capsules 一起持久化到历史：
        子代理工具事件走 /subagent-progress 独立 SSE，不进主流事件流，
        若不在此打包，历史回放时子代理的工具调用与结果将全部丢失。
        """
        idx = capsule_id - 1
        if not (0 <= idx < len(self._current_batch)):
            return []
        task = self._current_batch[idx]
        lines, _ = task.get_logs_since(0)
        keys = ("event", "tool_id", "tool_name", "tool_args", "tool_output", "tool_status")
        return [
            {k: ln[k] for k in keys if k in ln}
            for ln in lines
            if ln.get("event") in ("tool_start", "tool_end")
        ]

    def get_progress_logs(self, capsule_id: int) -> tuple[list[dict], int, bool]:
        """获取指定 capsule 的增量日志。返回 (新日志行, 总行数, 是否已完成)。

        注意：当 _current_batch 尚未设置时，返回 done=False 让 EventSource 继续轮询。
        一旦 delegate_tasks_parallel 创建了 items 并调用 start_batch，日志就能正常推送。
        若 60 秒内 batch 仍未设置，自动断开避免泄漏。
        """
        idx = capsule_id - 1  # capsule_id 从 1 开始
        if 0 <= idx < len(self._current_batch):
            task = self._current_batch[idx]
            lines, total = task.get_logs_since(0)
            done = task.status in ("done", "error")
            return lines, total, done
        # batch 尚未设置（时序：前端 EventSource 比 tool 执行更快）
        # 返回 done=False 让 SSE generator 继续轮询，不自闭
        return [], 0, False

    def start_batch(self, tasks: list[SubagentTask]) -> None:
        self._current_batch = tasks

    def clear_batch(self) -> None:
        self._current_batch = []

    async def run_sync(self, task: str, agent_type: str = "coder", context: str = "", wall_timeout: float = 180.0, idle_timeout: float = 60.0, item: Optional[SubagentTask] = None) -> SubagentTask:
        if not self._config:
            raise RuntimeError("SubagentManager 尚未初始化")
        agent_type = agent_type if agent_type in SUBAGENT_PROMPTS else "coder"
        # ponytail: 支持复用外部传入的 item（前端 SSE 轮询依赖 _current_batch 里的同一对象）——
        # 之前 run_sync 总是内部 new 一个新 SubagentTask，导致 batch 里预创建的 item 永远拿不到
        # 执行期间的 append_log（只有初始"队列中，等待执行..."），前端子代理卡片一直显示等待中。
        if item is None:
            item = SubagentTask(
                id=f"subagent-{uuid.uuid4().hex[:12]}",
                agent_type=agent_type,
                task=task,
                context=context,
            )
            self._tasks[item.id] = item
        else:
            item.agent_type = agent_type
            item.task = task
            item.context = context
            if item.id not in self._tasks:
                self._tasks[item.id] = item
        item.status = "running"
        item.started_at = time.time()
        # 最后一个日志时间戳，用于检测 idle timeout
        item._last_log_ts = time.time()
        # 包装 append_log 记录活跃时间（透传结构化 extra 字段）
        _orig_append = item.append_log
        def _tracked_append_log(text: str, cat: str = "info", **extra) -> None:
            item._last_log_ts = time.time()
            _orig_append(text, cat, **extra)
        item.append_log = _tracked_append_log  # type: ignore[method-assign]

        # 启动 idle 监视后台任务
        async def _idle_watcher():
            while item.status == "running":
                await asyncio.sleep(5)
                idle = time.time() - item._last_log_ts
                if idle > idle_timeout:
                    item.append_log(
                        f"❌ 长时间无新日志（{idle:.0f}s），判定为卡死，强制结束",
                        "error",
                    )
                    raise asyncio.CancelledError(f"idle timeout {idle:.0f}s")
        watcher = asyncio.create_task(_idle_watcher())
        try:
            item.result = await asyncio.wait_for(
                self._run_agent(item),
                timeout=wall_timeout,
            )
            item.status = "done"
        except asyncio.TimeoutError:
            elapsed = time.time() - item.started_at
            item.error = f"超时（{elapsed:.1f}s）"
            item.status = "error"
            item.result = (
                f"❌ 子代理执行超时（{elapsed:.1f}s）。\n"
                f"可能是 search 源响应慢或模型端排队。可以缩小任务范围或换个网络环境重试。"
            )
            item.append_log(f"❌ 子代理整体超时（{elapsed:.1f}s，预算 {wall_timeout}s）", "error")
        except (asyncio.CancelledError, Exception) as exc:
            if item.status == "running":  # 来自 idle watcher
                idle = time.time() - item._last_log_ts
                item.error = f"长时间无活动（{idle:.0f}s）"
                item.status = "error"
                item.result = (
                    f"❌ 子代理长时间无新日志（{idle:.0f}s），判定为卡死。\n"
                    f"通常是 search 源不可达或模型端无响应。可以降低搜索并发或换个网络重试。"
                )
            else:
                item.error = f"{type(exc).__name__}: {exc}"
                item.status = "error"
                item.result = f"❌ 子代理执行失败：{item.error}"
        finally:
            watcher.cancel()
            try:
                await watcher
            except (asyncio.CancelledError, Exception):
                pass
            item.finished_at = time.time()
        return item

    async def _run_agent(self, item: SubagentTask) -> str:
        graph = self._graph_cache.get(item.agent_type)
        if graph is None:
            # ponytail: 兜底 —— 若 configure 未构建（如测试直接改 _config），按需现建
            graph = self._build_subagent_graph(item.agent_type)
        message = item.task
        if item.context:
            message = f"上下文：\n{item.context}\n\n任务：\n{item.task}"
        item.append_log(f"开始执行 {item.agent_type} 子代理任务...")
        item.append_log(f"任务: {message[:200]}")

        final_text = ""
        started_ids: set = set()   # 已发出 tool_start 的工具调用 id
        ended_ids: set = set()     # 已发出 tool_end 的工具调用 id
        try:
            async for chunk in graph.astream(
                {"messages": [HumanMessage(content=message)]},
                {"recursion_limit": max(1, int(self._config.recursion_limit or 60))},
                stream_mode="values",
            ):
                msgs = chunk.get("messages", [])
                if not msgs:
                    continue
                # 结构化工具事件（前端据此渲染工具卡片）
                for ev in _collect_tool_events(msgs, started_ids, ended_ids):
                    if ev["event"] == "tool_start":
                        item.append_log(
                            f"调用工具: {ev['name']}",
                            "tool",
                            event="tool_start",
                            tool_id=ev["id"],
                            tool_name=ev["name"],
                            tool_args=ev["args"],
                        )
                    else:
                        item.append_log(
                            f"工具完成: {ev['name']}",
                            "tool",
                            event="tool_end",
                            tool_id=ev["id"],
                            tool_name=ev["name"],
                            tool_output=ev["output"],
                            tool_status=ev["status"],
                        )
                last = msgs[-1]
                msg_type = getattr(last, "type", "")
                if msg_type == "ai":
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


def _collect_tool_events(msgs, started: set, ended: set) -> list[dict]:
    """从一条 values 状态（完整 messages 列表）中提取新增的工具事件。

    stream_mode="values" 每轮返回完整消息列表，重复遍历需去重：
    - AI 消息里的 tool_calls（含 name/args/id）→ tool_start 事件
    - Tool 消息（含 tool_call_id/content/name）→ tool_end 事件
    返回事件列表（纯函数，便于单测）。
    """
    events: list[dict] = []
    for msg in msgs:
        msg_type = getattr(msg, "type", "")
        if msg_type == "ai":
            for tc in getattr(msg, "tool_calls", None) or []:
                tid = tc.get("id")
                if not tid or tid in started:
                    continue
                started.add(tid)
                events.append({
                    "event": "tool_start",
                    "id": tid,
                    "name": tc.get("name", "unknown"),
                    "args": tc.get("args") or {},
                })
        elif msg_type == "tool":
            tid = getattr(msg, "tool_call_id", "")
            if not tid or tid in ended or tid not in started:
                continue
            ended.add(tid)
            events.append({
                "event": "tool_end",
                "id": tid,
                "name": getattr(msg, "name", "unknown"),
                "output": getattr(msg, "content", ""),
                "status": getattr(msg, "status", "success"),
            })
    return events


def _run_coro_in_thread(coro, timeout: float = 60.0):
    result = {}

    def runner():
        try:
            result["value"] = asyncio.run(coro)
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise TimeoutError(f"子代理执行超过 {timeout}s")
    if "error" in result:
        raise result["error"]
    return result.get("value")


@tool
def delegate_task(task: str, agent_type: str = "coder", context: str = "") -> str:
    """把一个子任务委派给子代理执行并同步等待结果。agent_type 可选 coder、reviewer、debugger、analysis、searcher。

    选型提示：本地文件/代码/系统任务用 coder（无联网工具）；分析探索/可行性研究用 analysis（只读探查，不改代码）；
    需要联网检索最新信息用 searcher（仅 web_search/web_fetch）。
    """
    if agent_type not in SUBAGENT_PROMPTS:
        agent_type = "coder"
    # ponytail: 单发子代理也走 start_batch，让 /subagent-progress SSE 能读到日志。
    # 之前 delegate_task 从不设置 _current_batch，前端轮询永远返回空 → 胶囊无任何实时日志。
    item = SubagentTask(
        id=f"subagent-{uuid.uuid4().hex[:12]}",
        agent_type=agent_type,
        task=task,
        context=context,
    )
    item.append_log("队列中，等待执行...")
    manager._tasks[item.id] = item
    manager.start_batch([item])
    # timeout 与并行路径一致（200s）：单发子代理常见跑 2~3 分钟，默认 60s 会误杀
    item = _run_coro_in_thread(
        manager.run_sync(task=task, agent_type=agent_type, context=context, item=item),
        timeout=200.0,
    )
    return (
        f"子代理任务 {item.id} [{item.agent_type}] 状态：{item.status}\n\n"
        f"{item.result or item.error}"
    )


async def _run_all_parallel(items: list[SubagentTask]) -> list[SubagentTask]:
    """在一个事件循环里并发执行全部子代理任务。

    与旧的 ThreadPoolExecutor + 每任务独立线程/asyncio.run 不同：
    所有任务共享同一批预构建 graph（LLM 连接池），在单个事件循环里 gather，
    HTTP 请求真正同时发出（I/O 并发），不再有每任务一个线程 + 一个事件循环的重复开销。
    每个任务仍保留各自 run_sync 内的 wall_timeout(180s)/idle_timeout(60s) 保护。
    """
    return await asyncio.gather(
        *(
            manager.run_sync(
                task=item.task,
                agent_type=item.agent_type,
                context=item.context,
                wall_timeout=180.0,
                idle_timeout=60.0,
                item=item,  # 复用 batch item：日志/状态写入同一对象，前端 SSE 才能实时读到
            )
            for item in items
        )
    )


@tool
def delegate_tasks_parallel(tasks_json: str) -> str:
    """并行派发多个独立子任务。tasks_json 是一个 JSON 数组，每个元素包含 task/agent_type/context 字段。
    适用于多个任务之间没有文件或数据依赖的场景。同一时间最多并行 4 个子代理。

    选型提示：本地文件/代码/系统任务用 coder（无联网工具）；分析探索/可行性研究用 analysis（只读探查，不改代码）；
    需要联网检索最新信息用 searcher（仅 web_search/web_fetch）。"""
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

    # 真正并发：单线程单事件循环里 gather 全部任务（见 _run_all_parallel）。
    # 相比旧的 ThreadPoolExecutor(max_workers=2) + 任务间 sleep(1) 错峰：
    #  - 最多 4 个任务同时发出 HTTP 请求（入口 tasks[:4] 已截断），I/O 并行
    #  - 不再有每任务一个线程 + 一个事件循环的重复开销
    #  - 若整体超时（200s），结果以各自 item.status 为准（线程 daemon 后台收尾）
    results = []
    errors = []
    try:
        completed = _run_coro_in_thread(_run_all_parallel(items), timeout=200.0)
        for item in completed:
            results.append(
                f"任务 {item.id} [{item.agent_type}] 状态：{item.status}\n\n"
                f"{item.result or item.error}"
            )
    except TimeoutError as exc:
        errors.append(f"并行批次整体超时: {exc}")
    except Exception as exc:
        errors.append(f"并行批次失败: {type(exc).__name__}: {exc}")

    manager.clear_batch()
    parts = [f"✅ 并行子代理执行完成（{len(results)}/{len(tasks)} 成功）\n"]
    if results:
        parts.append("\n---\n".join(results))
    if errors:
        parts.append(f"\n\n❌ 失败任务：\n" + "\n".join(errors))
    return "\n".join(parts)


TOOLS = [delegate_task, delegate_tasks_parallel]
