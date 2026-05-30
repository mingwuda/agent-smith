"""桌面 AI 智能体核心"""
import asyncio
import json
import socket
import time
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

    content = [{"type": "text", "text": message or "请分析这些图片。"}]
    for item in valid_images:
        content.append({"type": "image_url", "image_url": {"url": item["data_url"]}})
    return content


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
    
    def _build_llm(self):
        kwargs = {
            "model": self.config.model,
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
    
    def _build_system_prompt(self) -> str:
        prompt = self.config.system_prompt
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
        print(
            "🧹 上下文已压缩: "
            f"{len(messages)} -> {len(compacted)} messages, "
            f"~{before} -> ~{after} tokens, "
            f"threshold={compaction_threshold_tokens(self.config.model, self.config.context_window_tokens)}"
        )

    def _thread_key(self) -> str:
        return f"{self._user_id}:{self._thread_id}"
    
    def _rebuild_graph(self):
        self._graph = create_react_agent(
            self.llm,
            self.tools,
            prompt=self._build_system_prompt(),
            checkpointer=self.memory,
        )
    
    async def run(
        self,
        message: str,
        history: Optional[list[dict]] = None,
        attachments: Optional[list[dict]] = None,
    ) -> tuple[str, list[dict]]:
        """处理用户消息，返回 (最终回复, 中间步骤列表)"""
        config = self._run_config()
        input_messages = []
        thread_key = self._thread_key()
        if thread_key not in self._hydrated_threads:
            input_messages = compact_history_messages(session_messages_to_langchain(history or []), self.config)
        current_content = _human_content(message, attachments)
        input_messages.append(HumanMessage(content=current_content))
        
        try:
            await self._compact_checkpoint_if_needed(config)
            result = await self._graph.ainvoke(
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
    
    async def stream_run(
        self,
        message: str,
        history: Optional[list[dict]] = None,
        attachments: Optional[list[dict]] = None,
    ) -> AsyncGenerator[str, None]:
        """流式处理用户消息，yield SSE 格式事件"""
        run_config = self._run_config()
        input_messages = []
        thread_key = self._thread_key()
        if thread_key not in self._hydrated_threads:
            input_messages = compact_history_messages(session_messages_to_langchain(history or []), self.config)
        input_messages.append(HumanMessage(content=_human_content(message, attachments)))
        input_data = {"messages": input_messages}
        
        thinking_buffer = ""        # 累积推理文本（工具调用前的内容）
        final_buffer = ""           # 最终回复缓存
        step_count = 0
        in_tool_call = False        # 当前是否正在产生工具调用
        usage_recorded = False
        active_tool = ""
        active_step = 0
        active_tool_started_at = 0.0
        last_progress_at = 0.0
        pending_event = None
        cancelled = False
        
        try:
            await self._compact_checkpoint_if_needed(run_config)
            event_iter = self._graph.astream_events(input_data, run_config, version="v2").__aiter__()
            pending_event = asyncio.create_task(event_iter.__anext__())
            while True:
                try:
                    event = await asyncio.wait_for(asyncio.shield(pending_event), timeout=2.0)
                except asyncio.TimeoutError:
                    now = time.time()
                    if active_tool and now - last_progress_at >= 1.5:
                        elapsed = int(now - active_tool_started_at)
                        label = "子代理仍在执行" if active_tool == "delegate_task" else "工具仍在执行"
                        yield _sse({
                            "type": "progress",
                            "tool": active_tool,
                            "step": active_step,
                            "elapsed": elapsed,
                            "message": f"{label}，已耗时 {elapsed}s",
                        })
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
                    active_tool = tool_name
                    active_step = step_count
                    active_tool_started_at = time.time()
                    last_progress_at = active_tool_started_at
                    
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
                    
                    self._record_tool_call(tool_name)
                    
                    yield _sse({
                        "type": "tool_result",
                        "tool": tool_name,
                        "result": _truncate(output_str, 400),
                    })
                    
                    # 重置状态，准备接收下一轮推理
                    in_tool_call = False
                    active_tool = ""
                    active_step = 0
                    active_tool_started_at = 0.0

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
        
        finally:
            if pending_event and not pending_event.done():
                pending_event.cancel()
            self._hydrated_threads.add(thread_key)
            if not cancelled:
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
