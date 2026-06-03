"""桌面 AI 智能体核心"""
import asyncio
import json
import socket
import time
from datetime import datetime
from typing import AsyncGenerator, Optional
from urllib.parse import urlparse

from langchain_openai import ChatOpenAI
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver

from config import AgentConfig
from context_manager import (
    checkpoint_replacement,
    compact_messages,
    compaction_threshold_tokens,
    estimate_messages_tokens,
    should_compact,
)
from logger import get_logger

logger = get_logger(__name__)
from memory.local_memory import set_current_user
from monitoring.usage_tracker import get_tracker, UsageTracker
from network_resolver import configure_host_resolution
from skills.registry import get_registry, SkillRegistry


def _extract_tool_name(msg) -> str:
    """从消息中提取工具名"""
    if hasattr(msg, "tool_calls") and msg.tool_calls:
        return msg.tool_calls[0].get("name", "") if isinstance(msg.tool_calls[0], dict) else msg.tool_calls[0].name
    return ""


def _extract_tool_args(msg) -> dict:
    """从消息中提取工具参数"""
    if hasattr(msg, "tool_calls") and msg.tool_calls:
        tc = msg.tool_calls[0]
        if isinstance(tc, dict):
            return tc.get("args", {}) or tc.get("parameters", {})
        return getattr(tc, "args", {})
    return {}


def _truncate(text: str, max_len: int = 300) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _tool_signature(tool_name: str, args: object) -> str:
    if isinstance(args, dict):
        normalized = {k: str(v)[:300] for k, v in sorted(args.items()) if not k.startswith("_")}
    else:
        normalized = str(args)[:300]
    return f"{tool_name}:{json.dumps(normalized, ensure_ascii=False, sort_keys=True)}"


def _tool_call_label(item: dict) -> str:
    tool = item.get("tool", "") or "unknown"
    args = item.get("args")
    if isinstance(args, dict):
        if tool == "web_search" and args.get("query"):
            return f"{tool}({str(args.get('query'))[:48]})"
        if tool == "web_fetch" and args.get("url"):
            return f"{tool}({str(args.get('url'))[:48]})"
        if args.get("path"):
            return f"{tool}({str(args.get('path'))[:48]})"
    return tool


def _loop_guard_message(reason: str, calls: list[dict], recursion_limit: int) -> str:
    recent = " -> ".join(_tool_call_label(item) for item in calls[-8:] if item.get("tool"))
    return (
        "检测到工具调用可能陷入重复循环，已提前停止本轮任务，避免继续消耗步骤和上下文。\n\n"
        f"- 判断原因：{reason}\n"
        f"- 最近工具链路：{recent or '无'}\n"
        f"- 当前最大推理步数：{recursion_limit}\n\n"
        "建议把任务拆得更具体一些，或直接指定要检查的文件/关键词后重试。"
    )


def _detect_tool_loop(calls: list[dict], recursion_limit: int) -> str:
    latest = calls[-1]
    latest_sig = latest.get("signature", "")
    exact_repeat_count = sum(1 for item in calls[-8:] if item.get("signature") == latest_sig)
    if latest_sig and exact_repeat_count >= 3:
        return f"最近 8 次工具调用中，同一工具和参数重复了 {exact_repeat_count} 次"

    if len(calls) < 6:
        return ""

    recent12 = calls[-12:]
    tool_names = [item.get("tool", "") for item in recent12]
    signatures = [item.get("signature", "") for item in recent12 if item.get("signature")]
    unique_signatures = set(signatures)
    unique_tools = set(tool_names)
    exploratory_tools = {"search_files", "read_file", "list_files", "web_search", "web_fetch"}
    low_argument_diversity = len(unique_signatures) <= max(3, len(signatures) // 4)
    if (
        len(recent12) >= 10
        and unique_tools
        and unique_tools.issubset(exploratory_tools)
        and len(unique_tools) <= 2
        and low_argument_diversity
    ):
        dominant = max(unique_tools, key=tool_names.count)
        return (
            f"最近 {len(recent12)} 次调用集中在 {', '.join(sorted(unique_tools))}，"
            f"但不同参数签名只有 {len(unique_signatures)} 个，{dominant} 出现 {tool_names.count(dominant)} 次"
        )

    estimated_graph_steps = len(calls) * 2 + 1
    if estimated_graph_steps >= max(6, recursion_limit - 3):
        tail = calls[-8:]
        tail_unique = {item.get("tool", "") for item in tail}
        tail_signatures = {item.get("signature", "") for item in tail if item.get("signature")}
        if len(tail_unique) <= 3 and len(tail_signatures) <= 3:
            return f"已接近最大推理步数，且最近工具类型仍高度重复：{', '.join(sorted(tail_unique))}"

    return ""


def _get_nested(mapping: dict, *keys: str):
    value = mapping
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _extract_usage_tokens(payload) -> tuple[int, int, int]:
    """从 LangChain/OpenAI 响应对象中提取 input/output/cached token。"""
    if payload is None:
        return 0, 0, 0

    usage = getattr(payload, "usage_metadata", None)
    if not usage:
        usage = getattr(payload, "response_metadata", None)
        if isinstance(usage, dict):
            usage = usage.get("token_usage") or usage.get("usage") or usage

    if isinstance(usage, dict):
        input_tokens = (
            usage.get("input_tokens")
            or usage.get("prompt_tokens")
            or _get_nested(usage, "token_usage", "prompt_tokens")
            or 0
        )
        output_tokens = (
            usage.get("output_tokens")
            or usage.get("completion_tokens")
            or _get_nested(usage, "token_usage", "completion_tokens")
            or 0
        )
        cached_tokens = (
            usage.get("cached_input_tokens")
            or _get_nested(usage, "input_token_details", "cache_read")
            or _get_nested(usage, "prompt_tokens_details", "cached_tokens")
            or 0
        )
        return int(input_tokens or 0), int(output_tokens or 0), int(cached_tokens or 0)

    input_tokens = getattr(usage, "input_tokens", 0) or getattr(usage, "prompt_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or getattr(usage, "completion_tokens", 0) or 0
    cached_tokens = getattr(usage, "cached_input_tokens", 0) or 0
    return int(input_tokens or 0), int(output_tokens or 0), int(cached_tokens or 0)


def _is_recursion_limit_error(exc: Exception) -> bool:
    return type(exc).__name__ == "GraphRecursionError"


def _recursion_limit_message(limit: int) -> str:
    return (
        f"执行步骤达到上限（recursion_limit={limit}），Agent 可能在反复调用工具或没有生成最终回答。"
        "可以在设置里调大“最大推理步数”，或把任务拆小后重试。"
    )


def _connection_diagnostic(exc: Exception, config: AgentConfig) -> str:
    message = f"{type(exc).__name__}: {exc}"
    exc_name = type(exc).__name__
    if "support image input" in message or "image input" in message:
        return "\n".join([
            message,
            (
                f"当前模型端点不支持图片输入：provider={config.active_provider}，"
                f"model={config.model}，base_url={config.base_url or '默认 OpenAI'}。"
            ),
            "图片粘贴和前端传输已生效，但需要切换到支持 vision/image input 的模型或网关后才能分析图片。",
        ])
    if exc_name == "ReadTimeout" or "ReadTimeout" in message:
        return "\n".join([
            message,
            (
                f"模型响应读超时：当前 provider={config.active_provider}，model={config.model}，"
                f"timeout={config.api_timeout_seconds}s，base_url={config.base_url or '默认 OpenAI'}。"
            ),
            "这通常表示模型网关已连接，但在超时时间内没有返回下一段响应；长上下文、工具结果较多或模型端排队都会触发。",
            "可以新开会话减少上下文，或把 AGENT_API_TIMEOUT_SECONDS 临时调大到 60 再重试。",
        ])
    if exc_name != "APIConnectionError" and "Connection error" not in str(exc):
        return message

    details = [
        message,
        (
            f"模型连接失败，已重试 {config.api_max_retries} 次仍未成功。"
            f"当前 provider={config.active_provider}，model={config.model}，base_url={config.base_url or '默认 OpenAI'}。"
        ),
    ]
    if config.base_url:
        host = urlparse(config.base_url).hostname
        if host:
            try:
                ip = socket.gethostbyname(host)
                details.append(f"Python DNS 检查：{host} -> {ip}。请检查模型服务是否可访问、API Key 是否有效，或稍后重试。")
            except OSError as dns_exc:
                details.append(
                    f"Python DNS 检查失败：无法解析 {host}（{dns_exc}）。"
                    "这通常是运行服务的 Python 进程 DNS/网络环境问题，不是任务工具执行失败。"
                )
    return "\n".join(details)


def _human_content(message: str, attachments: Optional[list[dict]] = None):
    attachments = attachments or []
    valid_images = [
        item for item in attachments
        if isinstance(item, dict)
        and str(item.get("mime_type") or "").startswith("image/")
        and str(item.get("data_url") or "").startswith("data:image/")
    ]
    if not valid_images:
        return message

    content = []
    for item in valid_images:
        content.append({"type": "image_url", "image_url": {"url": item["data_url"]}})
    content.append({"type": "text", "text": message or "请分析这些图片。"})
    return content


def _strip_image_content_from_message(message):
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return message, False

    text_parts = []
    removed_images = 0
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text":
            text_parts.append(str(item.get("text") or ""))
        elif item_type in {"image_url", "input_image", "image"}:
            removed_images += 1

    if removed_images == 0:
        return message, False

    text = "\n".join(part for part in text_parts if part).strip() or "请分析这些图片。"
    text += f"\n\n[本轮曾包含 {removed_images} 张图片；图片内容已从会话内存中移除，避免后续纯文本请求重复携带图片。]"
    if hasattr(message, "model_copy"):
        return message.model_copy(update={"content": text}), True
    return message.copy(update={"content": text}), True


def _strip_image_content_from_messages(messages: list) -> tuple[list, bool]:
    changed = False
    stripped = []
    for message in messages:
        new_message, message_changed = _strip_image_content_from_message(message)
        stripped.append(new_message)
        changed = changed or message_changed
    return stripped, changed


def _tool_call_ids(message) -> set[str]:
    ids: set[str] = set()
    for tool_call in getattr(message, "tool_calls", None) or []:
        if isinstance(tool_call, dict):
            call_id = tool_call.get("id")
        else:
            call_id = getattr(tool_call, "id", "")
        if call_id:
            ids.add(str(call_id))
    return ids


def _tool_message_id(message) -> str:
    return str(getattr(message, "tool_call_id", "") or "")


def _drop_dangling_tool_call_messages(messages: list) -> tuple[list, bool]:
    result = []
    changed = False
    idx = 0
    while idx < len(messages):
        message = messages[idx]
        expected_ids = _tool_call_ids(message) if getattr(message, "type", "") == "ai" else set()
        if expected_ids:
            next_idx = idx + 1
            seen_ids: set[str] = set()
            while next_idx < len(messages) and getattr(messages[next_idx], "type", "") == "tool":
                tool_call_id = _tool_message_id(messages[next_idx])
                if tool_call_id:
                    seen_ids.add(tool_call_id)
                next_idx += 1
            if not expected_ids.issubset(seen_ids):
                changed = True
                idx = next_idx
                continue
        result.append(message)
        idx += 1
    return result, changed


def session_messages_to_langchain(messages: list[dict]) -> list:
    """Convert persisted chat messages into LangChain messages."""
    converted = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content") or ""
        if not content:
            continue
        if role == "user":
            converted.append(HumanMessage(content=content))
        elif role == "assistant":
            converted.append(AIMessage(content=content))
    return converted


def compact_history_messages(messages: list, config: AgentConfig) -> list:
    if should_compact(messages, config.model, config.context_window_tokens):
        return compact_messages(messages, config.model, config.context_window_tokens)
    return messages


def _extract_steps_from_messages(messages: list) -> list[dict]:
    """从消息历史中提取中间步骤（工具调用 + 思考）"""
    steps = []
    for msg in messages:
        if msg.type == "ai" and hasattr(msg, "tool_calls") and msg.tool_calls:
            # AI 调用工具 —— 这是"思考"步骤
            name = _extract_tool_name(msg)
            args = _extract_tool_args(msg)
            thought = msg.content or ""
            steps.append({
                "type": "tool_call",
                "tool": name,
                "args": args,
                "thought": thought,
            })
        elif msg.type == "tool":
            # 工具返回结果
            tool_name = getattr(msg, "name", "") or ""
            raw = msg.content
            try:
                import json
                result_text = json.dumps(json.loads(raw), ensure_ascii=False) if raw.startswith("{") else raw
            except (json.JSONDecodeError, ValueError):
                result_text = raw
            steps.append({
                "type": "tool_result",
                "tool": tool_name,
                "result": _truncate(result_text),
                "result_full": result_text,
            })
        elif msg.type == "ai" and msg.content and not getattr(msg, "tool_calls", None):
            # AI 的纯文本思考（非工具调用）
            pass  # 不做特殊处理，因为最终回复会包含
    
    return steps


class DesktopAgent:
    """桌面 AI 智能体"""
    
    def __init__(self, config: AgentConfig):
        self.config = config
        self.llm = self._build_llm()
        self.memory = MemorySaver()
        self._user_id = "default"
        self._tracker: UsageTracker = get_tracker(self._user_id)
        self.registry: SkillRegistry = get_registry()
        self.tools: list = []  # 由外部设置
        self._thread_id = "default"
        self._graph = None
        self._hydrated_threads: set[str] = set()

    def set_user(self, user_id: str):
        """切换当前用户"""
        self._user_id = user_id
        set_current_user(user_id)
        self._tracker = get_tracker(user_id)

    @property
    def user_id(self) -> str:
        return self._user_id

    @property
    def tracker(self) -> UsageTracker:
        return self._tracker
    
    def set_tools(self, tools: list):
        self.tools = tools
        self._rebuild_graph()
    
    def _build_llm(self, model_override: str = ""):
        kwargs = {
            "model": model_override or self.config.model,
            "api_key": self.config.api_key,
            "temperature": 0,
            "max_retries": self.config.api_max_retries,
            "timeout": self.config.api_timeout_seconds,
        }
        if self.config.base_url:
            kwargs["base_url"] = self.config.base_url
            host = urlparse(self.config.base_url).hostname
            if host:
                configure_host_resolution(host, self.config.api_host_ips)
        return ChatOpenAI(**kwargs)

    def _create_graph(self, model_override: str = ""):
        return create_react_agent(
            self._build_llm(model_override),
            self.tools,
            prompt=self._build_system_prompt(),
            checkpointer=self.memory,
        )
    
    def _build_system_prompt(self) -> str:
        now = datetime.now().astimezone()
        prompt = (
            self.config.system_prompt
            + "\n\n"
            + "## 当前日期与时间\n"
            + f"- 当前日期：{now.date().isoformat()}\n"
            + f"- 当前时间：{now.strftime('%Y-%m-%d %H:%M:%S %Z%z')}\n"
            + "- 遇到“今天/昨日/今年/最新/current/latest/recent”等相对时间时，必须以这里的日期为准。\n"
        )
        skill_block = self.registry.generate_prompt_block()
        if skill_block:
            prompt += skill_block
        return prompt

    def _record_model_usage(self, input_tokens: int, output_tokens: int, cached_tokens: int = 0, source: str = "llm"):
        if input_tokens <= 0 and output_tokens <= 0:
            return
        self.tracker.record_model_call(
            provider=self.config.active_provider,
            model=self.config.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_tokens,
            thread_id=self._thread_id,
            source=source,
        )

    def _record_tool_call(self, tool_name: str):
        self.tracker.record_tool_call(
            tool_name=tool_name,
            provider=self.config.active_provider,
            model=self.config.model,
            thread_id=self._thread_id,
        )

    def _run_config(self) -> dict:
        return {
            "configurable": {"thread_id": self._thread_key()},
            "recursion_limit": max(1, int(self.config.recursion_limit or 60)),
        }

    async def _compact_checkpoint_if_needed(self, run_config: dict):
        if not self._graph:
            return
        try:
            snapshot = await self._graph.aget_state(run_config)
        except Exception:
            return
        values = getattr(snapshot, "values", {}) or {}
        messages = list(values.get("messages") or [])
        if not messages:
            return
        if not should_compact(messages, self.config.model, self.config.context_window_tokens):
            return
        before = estimate_messages_tokens(messages)
        compacted = compact_messages(messages, self.config.model, self.config.context_window_tokens)
        await self._graph.aupdate_state(run_config, {"messages": checkpoint_replacement(compacted)})
        after = estimate_messages_tokens(compacted)
        logger.info(
            "🧹 上下文已压缩: %d -> %d messages, ~%d -> ~%d tokens, threshold=%d",
            len(messages), len(compacted), before, after,
            compaction_threshold_tokens(self.config.model, self.config.context_window_tokens),
        )

    async def _repair_checkpoint_tool_history(self, run_config: dict, graph=None):
        graph = graph or self._graph
        if not graph:
            return
        try:
            snapshot = await graph.aget_state(run_config)
        except Exception:
            return
        values = getattr(snapshot, "values", {}) or {}
        messages = list(values.get("messages") or [])
        if not messages:
            return
        repaired, changed = _drop_dangling_tool_call_messages(messages)
        if changed:
            await graph.aupdate_state(run_config, {"messages": checkpoint_replacement(repaired)})

    async def _strip_checkpoint_images(self, run_config: dict, graph=None):
        graph = graph or self._graph
        if not graph:
            return
        try:
            snapshot = await graph.aget_state(run_config)
        except Exception:
            return
        values = getattr(snapshot, "values", {}) or {}
        messages = list(values.get("messages") or [])
        if not messages:
            return
        stripped, changed = _strip_image_content_from_messages(messages)
        if changed:
            await graph.aupdate_state(run_config, {"messages": checkpoint_replacement(stripped)})

    def _thread_key(self) -> str:
        return f"{self._user_id}:{self._thread_id}"
    
    def _rebuild_graph(self):
        self._graph = self._create_graph()
    
    async def run(
        self,
        message: str,
        history: Optional[list[dict]] = None,
        attachments: Optional[list[dict]] = None,
        model_override: str = "",
    ) -> tuple[str, list[dict]]:
        """处理用户消息，返回 (最终回复, 中间步骤列表)"""
        config = self._run_config()
        graph = self._create_graph(model_override) if model_override else self._graph
        input_messages = []
        thread_key = self._thread_key()
        await self._repair_checkpoint_tool_history(config, graph)
        await self._strip_checkpoint_images(config, graph)
        if thread_key not in self._hydrated_threads:
            input_messages = compact_history_messages(session_messages_to_langchain(history or []), self.config)
        current_content = _human_content(message, attachments)
        input_messages.append(HumanMessage(content=current_content))
        
        try:
            await self._compact_checkpoint_if_needed(config)
            result = await graph.ainvoke(
                {"messages": input_messages},
                config,
            )
            messages = result["messages"]
            
            # 提取中间步骤
            current_start = 0
            for idx in range(len(messages) - 1, -1, -1):
                msg = messages[idx]
                if getattr(msg, "type", "") == "human" and getattr(msg, "content", "") == current_content:
                    current_start = idx + 1
                    break
            steps = _extract_steps_from_messages(messages[current_start:])
            for step in steps:
                if step.get("type") == "tool_result":
                    self._record_tool_call(step.get("tool") or "unknown")
            
            # 提取 AI 的最后一条消息作为最终回复
            final_content = "（Agent 未产生输出）"
            for msg in reversed(messages):
                if hasattr(msg, "content") and msg.type == "ai" and msg.content:
                    input_tok, output_tok, cached_tok = _extract_usage_tokens(msg)
                    if input_tok > 0 or output_tok > 0:
                        self._record_model_usage(input_tok, output_tok, cached_tok, source="agent_response")
                    else:
                        self.tracker.record_model_call(
                            provider=self.config.active_provider,
                            model=self.config.model,
                            input_tokens=0,
                            output_tokens=0,
                            thread_id=self._thread_id,
                            source="agent_response",
                            estimated=True,
                        )
                    final_content = msg.content
                    break
            
            return final_content, steps
        except Exception as e:
            if _is_recursion_limit_error(e):
                return f"❌ {_recursion_limit_message(config['recursion_limit'])}", []
            return f"❌ 执行出错: {_connection_diagnostic(e, self.config)}", []
        finally:
            self._hydrated_threads.add(thread_key)
            if attachments:
                await self._strip_checkpoint_images(config, graph)
    
    async def stream_run(
        self,
        message: str,
        history: Optional[list[dict]] = None,
        attachments: Optional[list[dict]] = None,
        model_override: str = "",
    ) -> AsyncGenerator[str, None]:
        """流式处理用户消息，yield SSE 格式事件"""
        run_config = self._run_config()
        graph = self._create_graph(model_override) if model_override else self._graph
        input_messages = []
        thread_key = self._thread_key()
        await self._repair_checkpoint_tool_history(run_config, graph)
        await self._strip_checkpoint_images(run_config, graph)
        if thread_key not in self._hydrated_threads:
            input_messages = compact_history_messages(session_messages_to_langchain(history or []), self.config)
        input_messages.append(HumanMessage(content=_human_content(message, attachments)))
        input_data = {"messages": input_messages}
        
        thinking_buffer = ""        # 累积推理文本（工具调用前的内容）
        final_buffer = ""           # 最终回复缓存
        step_count = 0
        in_tool_call = False        # 当前是否正在产生工具调用
        usage_recorded = False
        running_tools: dict[str, dict] = {}   # run_id -> {name, step, started_at}
        last_progress_at = 0.0
        pending_event = None
        cancelled = False
        loop_guard_triggered = False
        tool_call_history: list[dict] = []
        
        try:
            await self._compact_checkpoint_if_needed(run_config)
            event_iter = graph.astream_events(input_data, run_config, version="v2").__aiter__()
            pending_event = asyncio.create_task(event_iter.__anext__())
            while True:
                try:
                    event = await asyncio.wait_for(asyncio.shield(pending_event), timeout=2.0)
                except asyncio.TimeoutError:
                    now = time.time()
                    # 发送所有正在运行的工具进度
                    for rid, tinfo in list(running_tools.items()):
                        elapsed = int(now - tinfo["started_at"])
                        label = "子代理仍在执行" if tinfo["name"] == "delegate_task" else "工具仍在执行"
                        yield _sse({
                            "type": "progress",
                            "tool": tinfo["name"],
                            "step": tinfo["step"],
                            "elapsed": elapsed,
                            "message": f"{label}，已耗时 {elapsed}s",
                        })
                    if running_tools:
                        last_progress_at = now
                    continue
                except StopAsyncIteration:
                    break
                pending_event = asyncio.create_task(event_iter.__anext__())

                kind = event["event"]
                node = event.get("metadata", {}).get("langgraph_node", "")
                
                # ── LLM 流式 token ──
                if kind == "on_chat_model_stream" and node == "agent":
                    chunk = event["data"]["chunk"]
                    has_content = bool(chunk.content)
                    has_tool_chunks = bool(getattr(chunk, "tool_call_chunks", None))
                    
                    if has_tool_chunks:
                        # AI 正在发出工具调用 → 此前的内容就是推理过程
                        in_tool_call = True
                        if has_content:
                            thinking_buffer += chunk.content
                    elif has_content and not in_tool_call:
                        # 没有工具调用 → 可能是推理（后续可能有工具调用）或最终回复
                        thinking_buffer += chunk.content
                        # 暂时作为 token 流式输出，但最终如果发现是推理会转为 thought
                        final_buffer += chunk.content
                        yield _sse({"type": "token", "content": chunk.content})
                    elif has_content and in_tool_call:
                        # 工具调用后还在输出文本 → 这是下一轮思考或最终回复
                        # 这里不会走到，因为 tool start/end 会重置状态
                        pass
                
                # ── 工具开始 ──
                elif kind == "on_tool_start":
                    step_count += 1
                    tool_name = event.get("name", "")
                    run_id = event.get("run_id", "")
                    started_at = time.time()
                    running_tools[run_id] = {
                        "name": tool_name,
                        "step": step_count,
                        "started_at": started_at,
                    }
                    last_progress_at = started_at
                    
                    # 取出推理文本
                    thought = thinking_buffer.strip()
                    thinking_buffer = ""  # 重置
                    
                    # 从 previous tokens 中移除推理文本（它们不是最终回复）
                    if thought:
                        # 从 final_buffer 中去掉这部分
                        if final_buffer.endswith(thought):
                            final_buffer = final_buffer[:-len(thought)]
                        # 发送 thought 事件
                        yield _sse({
                            "type": "thought",
                            "thought": thought,
                            "step": step_count,
                        })
                    
                    # 工具参数
                    inp = event.get("data", {}).get("input", {})
                    if isinstance(inp, dict):
                        args_preview = {k: str(v)[:80] for k, v in inp.items() if not k.startswith("_")}
                    else:
                        args_preview = {"input": str(inp)[:80]}
                    tool_call_history.append({
                        "tool": tool_name,
                        "signature": _tool_signature(tool_name, inp),
                        "args": inp,
                    })
                    
                    yield _sse({
                        "type": "tool_start",
                        "tool": tool_name,
                        "args": args_preview,
                        "step": step_count,
                    })

                # ── 工具结束 ──
                elif kind == "on_tool_end":
                    output = event.get("data", {}).get("output", "")
                    output_str = str(output)[:500]
                    tool_name = event.get("name", "")
                    run_id = event.get("run_id", "")
                    tinfo = running_tools.pop(run_id, None)
                    step_for_tool = tinfo["step"] if tinfo else 0
                    is_error = bool(output_str.strip().startswith("❌"))
                    
                    self._record_tool_call(tool_name)
                    
                    yield _sse({
                        "type": "tool_result",
                        "tool": tool_name,
                        "step": step_for_tool,
                        "result": _truncate(output_str, 400),
                        "error": is_error,
                    })
                    
                    # 重置推理上下文（但保留其他并行工具的进度状态）
                    in_tool_call = False
                    if not running_tools:
                        # 没有剩余工具时，也重置最后进度时间，避免 stale 进度事件
                        last_progress_at = 0.0

                    loop_reason = _detect_tool_loop(tool_call_history, run_config["recursion_limit"])
                    if loop_reason:
                        loop_guard_triggered = True
                        final_buffer = _loop_guard_message(loop_reason, tool_call_history, run_config["recursion_limit"])
                        yield _sse({"type": "error", "content": final_buffer})
                        if pending_event and not pending_event.done():
                            pending_event.cancel()
                        break
                elif kind == "on_chat_model_end" and node == "agent":
                    output = event.get("data", {}).get("output")
                    input_tok, output_tok, cached_tok = _extract_usage_tokens(output)
                    if input_tok > 0 or output_tok > 0:
                        self._record_model_usage(input_tok, output_tok, cached_tok, source="chat_model_end")
                        usage_recorded = True
        
        except asyncio.CancelledError:
            cancelled = True
            if pending_event and not pending_event.done():
                pending_event.cancel()
            raise
        except GeneratorExit:
            cancelled = True
            if pending_event and not pending_event.done():
                pending_event.cancel()
            raise
        except Exception as e:
            if _is_recursion_limit_error(e):
                final_buffer = _recursion_limit_message(run_config["recursion_limit"])
                yield _sse({"type": "error", "content": final_buffer})
            else:
                final_buffer = _connection_diagnostic(e, self.config)
                yield _sse({"type": "error", "content": final_buffer})
            
            # 清理因异常中断而残留的运行中工具——发送合成的失败事件
            if running_tools:
                logger.warning(
                    "流异常中断，清理 %d 个未完成工具: %s",
                    len(running_tools),
                    ", ".join(t["name"] for t in running_tools.values()),
                )
            for rid, tinfo in list(running_tools.items()):
                yield _sse({
                    "type": "tool_result",
                    "tool": tinfo["name"],
                    "step": tinfo["step"],
                    "result": "❌ 连接中断，工具未完成",
                    "error": True,
                })
                running_tools.pop(rid, None)
        
        finally:
            if pending_event and not pending_event.done():
                pending_event.cancel()
            self._hydrated_threads.add(thread_key)
            if attachments:
                await self._strip_checkpoint_images(run_config, graph)
            if not cancelled and not loop_guard_triggered:
                if not usage_recorded:
                    self.tracker.record_model_call(
                        provider=self.config.active_provider,
                        model=self.config.model,
                        input_tokens=0,
                        output_tokens=0,
                        thread_id=self._thread_id,
                        source="chat_model_end",
                        estimated=True,
                    )
                # 发送完成事件（thinking_buffer 中剩余的是最终回复）
                remaining = thinking_buffer.strip()
                if remaining:
                    final_buffer = remaining
                yield _sse({"type": "done", "content": final_buffer})
                yield "data: [DONE]\n\n"
    
    def switch_thread(self, thread_id: str):
        self._thread_id = thread_id
    
    def reload_skills(self):
        """热加载技能 -> 重建 system prompt"""
        count = self.registry.reload()
        self._rebuild_graph()
        return count
