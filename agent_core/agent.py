"""桌面 AI 智能体核心"""
import asyncio
import json
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Optional
from urllib.parse import urlparse

from langchain_openai import ChatOpenAI
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
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


import hashlib


def _tool_signature(tool_name: str, args: object) -> str:
    if isinstance(args, dict):
        normalized = {}
        for k, v in sorted(args.items()):
            if k.startswith("_"):
                continue
            raw = str(v)
            # 值超过 300 字符时用 md5 代替截断，避免不同内容因截断而碰撞
            normalized[k] = hashlib.md5(raw.encode()).hexdigest() if len(raw) > 300 else raw
    else:
        normalized = hashlib.md5(str(args).encode()).hexdigest() if len(str(args)) > 300 else str(args)[:300]
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
    if len(calls) < 4:
        return ""

    # ── 检测1：同一工具+同一参数严格重复 ≥20 次（单步循环）──
    latest = calls[-1]
    latest_sig = latest.get("signature", "")
    if latest_sig and latest.get("tool") not in {"web_search", "web_fetch"}:
        last30_sigs = [item.get("signature", "") for item in calls[-30:] if item.get("signature")]
        count = last30_sigs.count(latest_sig)
        if count >= 20:
            return f"最近 30 次工具调用中，同一工具和参数严格重复了 {count} 次"

    # ── 检测2：参数循环（A→B→A→B 模式）──
    # 要求连续重复至少 3 轮才中断，避免误杀 web_search ↔ web_fetch 正常配对
    if len(calls) >= 12:
        recent18 = calls[-18:]
        sigs = [c.get("signature", "") for c in recent18 if c.get("signature")]
        if len(sigs) >= 12:
            for window in (2, 3, 4):
                if (
                    len(sigs) >= window * 3
                    and sigs[-window:] == sigs[-window*2:-window]
                    and sigs[-window*2:-window] == sigs[-window*3:-window*2]
                ):
                    return (
                        f"工具调用出现循环模式：最近 {window*3} 次的形式为 "
                        + " → ".join(sigs[-window*3:])
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
            message or "ReadTimeout: 模型响应读超时",
            (
                f"模型响应读超时：当前 provider={config.active_provider}，model={config.model}，"
                f"timeout={config.api_timeout_seconds}s，base_url={config.base_url or '默认 OpenAI'}。"
            ),
            "这通常表示模型网关已连接，但在超时时间内没有返回下一段响应；长上下文、工具结果较多或模型端排队都会触发。",
            "可以新开会话减少上下文，或在设置里把 AGENT_API_TIMEOUT_SECONDS 临时调大到 180 再重试。",
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


import re

# 匹配浏览器截图 markdown 图片引用: ![...](/api/screenshot?token=xxx)
_SCREENSHOT_URL_RE = re.compile(r"!\[([^\]]*)\]\(/api/screenshot\?token=[^)]+\)")


def _strip_screenshot_urls_from_text(text: str) -> tuple[str, bool]:
    """从文本中移除浏览器截图的 markdown 图片引用，避免旧截图 URL 泄漏到新回复。"""
    if not text or "/api/screenshot" not in text:
        return text, False
    new_text, count = _SCREENSHOT_URL_RE.subn("", text)
    # 清理可能留下的空行
    new_text = re.sub(r"\n{3,}", "\n\n", new_text).strip()
    return new_text, count > 0


def _strip_image_content_from_message(message):
    content = getattr(message, "content", None)

    # 处理纯文本内容中的浏览器截图 URL（如 "![截图](/api/screenshot?token=xxx)"）
    if isinstance(content, str):
        stripped, changed = _strip_screenshot_urls_from_text(content)
        if not changed:
            return message, False
        note = "\n\n[已移除历史浏览器截图引用，避免跨请求串扰]"
        stripped += note
        if hasattr(message, "model_copy"):
            return message.model_copy(update={"content": stripped}), True
        return message.copy(update={"content": stripped}), True

    if not isinstance(content, list):
        return message, False

    text_parts = []
    removed_images = 0
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text":
            # 同时清理文本部分中的截图 URL
            text = str(item.get("text") or "")
            text, _ = _strip_screenshot_urls_from_text(text)
            text_parts.append(text)
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
    """Convert persisted chat messages into LangChain messages.
    保留用户消息中的图片，让 LLM 能在后续轮次中看到历史图片。
    """
    converted = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content") or ""
        images = msg.get("images") or []
        if role == "user":
            if images:
                # 多模态：图片 + 文本
                multimodal = []
                for url in images:
                    if isinstance(url, str) and url.startswith("data:image/"):
                        multimodal.append({"type": "image_url", "image_url": {"url": url}})
                if content:
                    multimodal.append({"type": "text", "text": content})
                if multimodal:
                    converted.append(HumanMessage(content=multimodal))
                    continue
            # 无图或图片格式无效：回退到纯文本
            if content:
                converted.append(HumanMessage(content=content))
        elif role == "assistant" and content:
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


def _truncate_args(args: object, max_len: int = 80) -> str:
    """截断工具参数，用于反思日志。"""
    text = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args)
    return text[:max_len] + "…" if len(text) > max_len else text


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
        # 同步文件工具的用户上下文（用于工作区外授权校验）
        try:
            from tools.file_tools import set_current_user as _set_ft_user
            _set_ft_user(user_id)
        except Exception:
            pass

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
            "api_key": self.config.api_key or "sk-no-key-required",
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
            + "- 遇到\u201c今天/昨日/今年/最新/current/latest/recent\u201d等相对时间时，必须以这里的日期为准。\n"
        )

        # ── 注入项目根目录的 AGENTS.md（如果存在）──
        try:
            agents_md_path = Path(__file__).resolve().parent.parent / "AGENTS.md"
            if agents_md_path.exists():
                agents_content = agents_md_path.read_text(encoding="utf-8").strip()
                if agents_content:
                    prompt += "\n\n" + agents_content
        except Exception:
            pass

        skill_block = self.registry.generate_prompt_block()
        if skill_block:
            prompt += skill_block

        # ── 注入长期记忆中积累的进化模式 ──
        try:
            patterns = self._load_learned_patterns()
            if patterns:
                prompt += "\n\n## 从过往任务中学到的经验\n" + patterns
        except Exception:
            pass

        return prompt

    def _load_learned_patterns(self) -> str:
        """从长期记忆中读取经验模式，用于注入系统提示。
        自动遗忘超过 3 天的旧经验，避免记忆膨胀。"""
        if not self._user_id:
            return ""
        from memory.local_memory import get_memory, user_manager
        mem = get_memory(self._user_id)
        items = mem.list_items()
        # 获取记忆文件目录以便检查文件年龄
        mem_dir = user_manager.memory_dir(self._user_id)

        now = time.time()
        ttl_seconds = 10 * 24 * 3600  # 10 天
        learned = []
        deleted_count = 0

        for entry in items:
            key = entry.get("key", "")
            val = entry.get("value", "")
            if not (key.startswith("_learned_") and isinstance(val, str) and val.strip()):
                continue

            # 检查文件修改时间
            mem_file = mem_dir / f"{key}.json"
            file_age = now
            try:
                if mem_file.exists():
                    file_age = now - mem_file.stat().st_mtime
            except OSError:
                file_age = 0  # 无法获取则保留

            if file_age > ttl_seconds:
                # 过期，从磁盘和缓存中删除
                try:
                    mem.delete(key)
                except Exception:
                    pass
                deleted_count += 1
                continue

            learned.append(f"- {val.strip()}")

        if deleted_count:
            logger.info("[记忆] 自动清理了 %d 条过期学习经验", deleted_count)

        return "\n".join(learned) if learned else ""

    async def chat_sync(self, message: str, attachments: Optional[list[dict]] = None) -> str:
        """同步聊天：运行 agent 并收集完整的流式回复文本。

        适用于非浏览器场景（如微信、API 调用）需要一次性获取完整回复。
        """
        full = ""
        async for sse_line in self._stream_done_wrapper(message, attachments=attachments):
            line = sse_line.strip()
            if line.startswith("data: ") and not line.startswith("data: [DONE]"):
                try:
                    data = json.loads(line[6:])
                    event_type = data.get("type")
                    if event_type == "done":
                        full = data.get("content", "")
                    elif event_type == "error":
                        content = data.get("content", "")
                        if not full:
                            full = f"❌ {content}"
                except json.JSONDecodeError:
                    pass
        return full

    async def reflect_on_task(
        self,
        user_message: str,
        steps: list[dict],
        final_result: str,
    ) -> Optional[str]:
        """任务完成后反思，总结可复用的模式经验。无工具调用或无有价值模式时返回 None。"""
        # 只对涉及工具调用的任务反思
        tool_steps = [s for s in steps if s.get("type") == "tool_start"]
        if not tool_steps:
            return None

        tool_summary = "\n".join(
            f"- {s.get('tool', '?')}({_truncate_args(s.get('args', {}))})"
            for s in tool_steps
        )

        prompt = (
            "你是一个 AI 助手，刚刚完成了一个多步骤任务。请回顾执行过程，总结可复用的经验。\n\n"
            f"## 用户需求\n{user_message[:300]}\n\n"
            f"## 工具调用过程\n{tool_summary}\n\n"
            f"## 最终结果\n{final_result[:500]}\n\n"
            "请用 20 字以内总结这个任务中是否有可复用的模式、工作流或经验教训。\n"
            "- 如果有用且可复用的模式，回复格式：关键词|一句话总结\n"
            "  例如：zip分析|用户上传zip后先解压再逐文件分析\n"
            "- 如果只是普通的问答或一次性工具调用，回复：无需记录"
        )

        llm = self._build_llm()
        # 用较短超时，避免阻塞后续请求
        llm.request_timeout = 15
        try:
            resp = await llm.ainvoke([HumanMessage(content=prompt)])
            text = str(resp.content).strip()
            if not text or "无需记录" in text:
                return None
            return text
        except Exception:
            return None

    def _record_model_usage(self, input_tokens: int, output_tokens: int, cached_tokens: int = 0, source: str = "llm", thread_id: str = ""):
        if input_tokens <= 0 and output_tokens <= 0:
            return
        self.tracker.record_model_call(
            provider=self.config.active_provider,
            model=self.config.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_tokens,
            thread_id=thread_id or self._thread_id,
            source=source,
        )

    def _record_tool_call(self, tool_name: str, thread_id: str = ""):
        self.tracker.record_tool_call(
            tool_name=tool_name,
            provider=self.config.active_provider,
            model=self.config.model,
            thread_id=thread_id or self._thread_id,
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

    def _thread_key(self, thread_id: str = "") -> str:
        tid = thread_id or self._thread_id
        return f"{self._user_id}:{tid}"
    
    def _run_config(self, thread_key: str = "") -> dict:
        if not thread_key:
            thread_key = self._thread_key()
        return {
            "configurable": {"thread_id": thread_key},
            "recursion_limit": max(1, int(self.config.recursion_limit or 60)),
        }

    def _rebuild_graph(self):
        self._graph = self._create_graph()
    
    async def run(
        self,
        message: str,
        history: Optional[list[dict]] = None,
        attachments: Optional[list[dict]] = None,
        model_override: str = "",
        thread_id: str = "",
    ) -> tuple[str, list[dict]]:
        """处理用户消息，返回 (最终回复, 中间步骤列表)

        参数:
          thread_id: 当前会话 ID，取代全局 self._thread_id（支持并发）
        """
        tid = thread_id or self._thread_id
        config = self._run_config(tid)
        graph = self._create_graph(model_override) if model_override else self._graph
        input_messages = []
        thread_key = self._thread_key(tid)
        await self._repair_checkpoint_tool_history(config, graph)
        await self._strip_checkpoint_images(config, graph)
        if thread_key not in self._hydrated_threads:
            input_messages = compact_history_messages(session_messages_to_langchain(history or []), self.config)
        current_content = _human_content(message, attachments)
        input_messages.append(HumanMessage(content=current_content))

        logger.info(
            "[run] 开始: tid=%s, thread_key=%s, model=%s, message_len=%d",
            tid, thread_key, model_override or self.config.model, len(message),
        )
        _run_started_at = time.time()

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
                    self._record_tool_call(step.get("tool") or "unknown", thread_id=tid)
            
            # 提取 AI 的最后一条消息作为最终回复
            final_content = "（Agent 未产生输出）"
            for msg in reversed(messages):
                if hasattr(msg, "content") and msg.type == "ai" and msg.content:
                    input_tok, output_tok, cached_tok = _extract_usage_tokens(msg)
                    if input_tok > 0 or output_tok > 0:
                        self._record_model_usage(input_tok, output_tok, cached_tok, source="agent_response", thread_id=tid)
                    else:
                        self.tracker.record_model_call(
                            provider=self.config.active_provider,
                            model=self.config.model,
                            input_tokens=0,
                            output_tokens=0,
                            thread_id=tid,
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
            elapsed = time.time() - _run_started_at
            logger.info(
                "[run] 结束: tid=%s, thread_key=%s, duration=%.1fs",
                tid, thread_key, elapsed,
            )
            self._hydrated_threads.add(thread_key)
            if attachments:
                await self._strip_checkpoint_images(config, graph)
            # 释放该会话的浏览器页面，避免跨会话页面状态串扰
            # 注意：必须使用 thread_key（"default:abc"）而非 tid（"abc"），
            # 因为工具函数从 RunnableConfig 中读取的 thread_id 是完整 key
            try:
                from tools.browser_tools import release_browser_page
                release_browser_page(thread_key)
            except Exception:
                pass

    async def _stream_events_with_heartbeat(
        self,
        graph,
        input_data: dict,
        run_config: dict,
        heartbeat_interval: float = 2.0,
    ) -> AsyncGenerator[dict, None]:
        """流式获取 LangGraph 事件，并定期产生心跳事件。

        心跳事件格式为 {"_heartbeat": True}。此实现用独立的心跳任务替代
        asyncio.shield，避免底层事件任务异常未被消费而触发 asyncio 的
        "exception in shielded future" 告警。
        """
        event_iter = graph.astream_events(input_data, run_config, version="v2").__aiter__()
        event_task = asyncio.create_task(event_iter.__anext__())
        heartbeat_task = asyncio.create_task(asyncio.sleep(heartbeat_interval))
        try:
            while True:
                done, _pending = await asyncio.wait(
                    {event_task, heartbeat_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if heartbeat_task in done:
                    try:
                        heartbeat_task.result()
                    except asyncio.CancelledError:
                        return
                    logger.debug("[stream_run] 心跳")
                    yield {"_heartbeat": True}
                    heartbeat_task = asyncio.create_task(asyncio.sleep(heartbeat_interval))
                    continue

                if event_task in done:
                    try:
                        event = event_task.result()
                    except StopAsyncIteration:
                        return
                    yield event
                    event_task = asyncio.create_task(event_iter.__anext__())
        finally:
            if not event_task.done():
                event_task.cancel()
            if not heartbeat_task.done():
                heartbeat_task.cancel()
            # 消费未处理的任务异常，避免 asyncio 产生 "exception in shielded future" 告警
            for task in (event_task, heartbeat_task):
                if task.done() and not task.cancelled():
                    try:
                        task.result()
                    except (StopAsyncIteration, asyncio.CancelledError):
                        pass
                    except Exception:
                        pass

    async def stream_run(
        self,
        message: str,
        history: Optional[list[dict]] = None,
        attachments: Optional[list[dict]] = None,
        model_override: str = "",
        thread_id: str = "",
    ) -> AsyncGenerator[str, None]:
        """流式处理用户消息，yield SSE 格式事件

        参数:
          thread_id: 当前会话 ID，取代全局 self._thread_id（支持并发）
        """
        tid = thread_id or self._thread_id
        run_config = self._run_config(tid)
        graph = self._create_graph(model_override) if model_override else self._graph
        input_messages = []
        thread_key = self._thread_key(tid)
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
        cancelled = False
        loop_guard_triggered = False
        _done_yielded = False
        tool_call_history: list[dict] = []
        subagent_capsules: list[dict] = []  # 并行子代理任务胶囊数据
        _subagent_dispatched = False         # 是否已派发过子代理
        _post_subagent_tool_calls = 0        # 子代理完成后父模型继续调用的工具次数
        _post_subagent_seen_run_ids: set[str] = set()  # 已统计过的非子代理工具 run_id（避免重试重复计数）
        subagent_end_sent_at = 0.0           # 子代理结束事件发送时间戳
        last_model_activity_at = 0.0         # 最后一次模型活动（token/thought/tool）时间戳
        
        # 当前 todo 清单数据（随 manage_todo 工具调用更新）
        current_todo_list = None
        
        try:
            logger.info(
                "[stream_run] 开始: tid=%s, thread_key=%s, model=%s, timeout=%s, message_len=%d",
                tid, thread_key,
                model_override or self.config.model,
                self.config.api_timeout_seconds,
                len(message),
            )
            await self._compact_checkpoint_if_needed(run_config)
            async for event in self._stream_events_with_heartbeat(graph, input_data, run_config):
                if event.get("_heartbeat"):
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
                    # 子代理结束但父模型长时间没有产生最终回复，强制终止
                    # 以 subagent_end 发送时间为基准，避免父模型内部的慢速/空轮询刷新 idle 时间
                    if subagent_end_sent_at and not running_tools and not loop_guard_triggered:
                        elapsed_after_subagent = now - subagent_end_sent_at
                        if elapsed_after_subagent >= 90:
                            logger.warning("[子代理] subagent_end 已发送 %d 秒，父模型仍未完成汇总，强制终止", int(elapsed_after_subagent))
                            loop_guard_triggered = True
                            final_buffer = "✅ 所有子代理搜索已完成。\n\n（模型在汇总阶段超时，未返回完整汇总，以下为已收集的子代理结果片段）\n"
                            done_data = {"type": "done", "content": final_buffer}
                            if current_todo_list:
                                done_data["todo_list"] = current_todo_list
                            _done_yielded = True
                            yield _sse(done_data)
                            break
                    continue

                kind = event["event"]
                node = event.get("metadata", {}).get("langgraph_node", "")
                logger.debug("[stream_run] 事件: kind=%s node=%s", kind, node)
                
                # ── LLM 调用开始（日志 + 前端状态提示）──
                if kind == "on_chat_model_start" and node == "agent":
                    _input = event.get("data", {}).get("input", {})
                    run_id = event.get("run_id", "")[:12]
                    if isinstance(_input, list):
                        msg_count = len(_input)
                        # 记录最后一条 user 消息预览
                        last_msg = _input[-1] if _input else {}
                        last_content = str(getattr(last_msg, "content", ""))[:200]
                        logger.info(
                            "[LLM_START] run_id=%s msgs=%d last_msg=%s",
                            run_id, msg_count, last_content,
                        )
                    else:
                        logger.info("[LLM_START] run_id=%s input=%s", run_id, str(_input)[:200])

                    # 前端显示"正在调用 AI..."
                    yield _sse({"type": "llm_thinking"})
                    last_model_activity_at = time.time()

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
                        last_model_activity_at = time.time()
                    elif has_content and in_tool_call:
                        # 工具调用后还在输出文本 → 这是下一轮思考或最终回复
                        # 这里不会走到，因为 tool start/end 会重置状态
                        pass
                
                # ── 工具开始 ──
                elif kind == "on_tool_start":
                    step_count += 1
                    tool_name = event.get("name", "")
                    run_id = event.get("run_id", "")
                    
                    # 子代理完成后如果父模型还在调工具，最多允许 3 次不同的非子代理工具，之后强制汇总
                    # 用 run_id 去重，避免同一工具因网络重试被重复计数
                    if _subagent_dispatched and tool_name not in {"delegate_tasks_parallel", "delegate_task"} and run_id not in _post_subagent_seen_run_ids:
                        _post_subagent_seen_run_ids.add(run_id)
                        _post_subagent_tool_calls += 1
                        if _post_subagent_tool_calls >= 3:
                            logger.warning("[防循环] 子代理完成后父模型已调用 %d 次不同工具，强制汇总", _post_subagent_tool_calls)
                            loop_guard_triggered = True
                            final_buffer = "✅ 所有子代理搜索已完成，开始汇总结果。\n\n（以下为归并总结）\n"
                            done_data = {"type": "done", "content": final_buffer}
                            if current_todo_list:
                                done_data["todo_list"] = current_todo_list
                            yield _sse(done_data)
                            break
                    
                    started_at = time.time()
                    last_model_activity_at = time.time()
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
                    # 将工具入参存入 running_tools，供后续 tool_end 提取文件路径等
                    if run_id in running_tools:
                        running_tools[run_id]["input"] = inp
                    if isinstance(inp, dict):
                        args_preview = {k: str(v)[:2000] for k, v in inp.items() if not k.startswith("_")}
                    else:
                        args_preview = {"input": str(inp)[:2000]}
                    tool_call_history.append({
                        "tool": tool_name,
                        "signature": _tool_signature(tool_name, inp),
                        "args": inp,
                    })

                    # ── 工具调用开始日志 ──
                    args_short = {k: (str(v)[:200] + "..." if len(str(v)) > 200 else str(v))
                                  for k, v in args_preview.items()}
                    logger.info(
                        "[TOOL_START] tool=%s step=%d run_id=%s args=%s",
                        tool_name, step_count, run_id[:12], args_short,
                    )

                    yield _sse({
                        "type": "tool_start",
                        "tool": tool_name,
                        "args": args_preview,
                        "step": step_count,
                    })

                    # 并行子代理：解析任务列表，发送子代理启动事件
                    if tool_name in {"delegate_tasks_parallel", "delegate_task"}:
                        try:
                            if not isinstance(inp, dict):
                                inp = {"tasks_json": str(inp) if tool_name == "delegate_tasks_parallel" else str(inp)}
                            if tool_name == "delegate_tasks_parallel":
                                raw_tasks = inp.get("tasks_json", "")
                                sub_tasks = json.loads(raw_tasks) if isinstance(raw_tasks, str) else raw_tasks
                            else:
                                sub_tasks = [{"task": str(inp.get("task", "")), "agent_type": str(inp.get("agent_type", "coder"))}]
                            if isinstance(sub_tasks, list):
                                capsules = []
                                for i, t in enumerate(sub_tasks):
                                    atype = t.get("agent_type", "coder") if isinstance(t, dict) else "coder"
                                    ttask = t.get("task", "") if isinstance(t, dict) else str(t)
                                    capsules.append({
                                        "id": i + 1,
                                        "agent_type": atype,
                                        "task": (ttask[:60] + "...") if len(ttask) > 60 else ttask,
                                        "status": "running",
                                    })
                                if capsules:
                                    _subagent_dispatched = True
                                    subagent_capsules = capsules
                                    logger.info("[子代理] 发送 subagent_start，capsules=%d", len(capsules))
                                    yield _sse({
                                        "type": "subagent_start",
                                        "capsules": capsules,
                                    })
                        except Exception as exc:
                            logger.warning("[子代理] 解析胶囊失败 (tool=%s inp=%s): %s", tool_name, type(inp).__name__, exc)

                # ── 工具结束 ──
                elif kind == "on_tool_end":
                    output = event.get("data", {}).get("output", "")
                    
                    # ── 提取工具返回的纯文本内容 ──
                    # LangGraph 的 on_tool_end 输出可能是：
                    #   1) ToolMessage 对象 → 有 .content 属性
                    #   2) 字符串 "content='...'" (str() repr)
                    #   3) 纯文本
                    full_output_raw = ""
                    if hasattr(output, "content") and isinstance(output.content, str):
                        full_output_raw = output.content
                    else:
                        _s = str(output).strip()
                        # 去掉可能的 content= / content='...' 包装
                        for _prefix in ("content=", "content='", 'content="'):
                            if _s.startswith(_prefix):
                                _s = _s[len(_prefix):]
                        # 去掉末尾可能残留的单引号
                        if len(_s) > 1 and _s.endswith("'") and not _s.endswith("\\'"):
                            _s = _s[:-1]
                        full_output_raw = _s

                    full_output = full_output_raw
                    output_str = full_output[:500]  # 先截断用于显示
                    tool_name = event.get("name", "")
                    run_id = event.get("run_id", "")
                    tinfo = running_tools.pop(run_id, None)
                    step_for_tool = tinfo["step"] if tinfo else 0
                    is_error = bool(output_str.strip().startswith("❌"))
                    
                    self._record_tool_call(tool_name, thread_id=tid)

                    # ── Todo 清单事件：manage_todo 工具调用结束后推送 ──
                    if tool_name == "manage_todo":
                        from tools.todo_tools import get_todo_list
                        todo_data = get_todo_list()
                        if todo_data:
                            current_todo_list = todo_data
                            yield _sse({
                                "type": "todo",
                                "todo_list": todo_data,
                            })

                    # 提取内嵌的 diff 数据（从完整输出中查找，不受截断影响）
                    diff_data = None
                    # 尝试从可能的 JSON 包装中提取纯文本（如 {"content": "..."} 格式）
                    raw_output = full_output
                    try:
                        _parsed = json.loads(full_output)
                        if isinstance(_parsed, dict):
                            for _key in ("content", "output", "result", "text"):
                                if isinstance(_parsed.get(_key), str) and "__DIFF__:" in _parsed[_key]:
                                    raw_output = _parsed[_key]
                                    break
                    except (json.JSONDecodeError, ValueError):
                        pass

                    for _marker in ("\n__DIFF__:", "__DIFF__:"):
                        if _marker in raw_output:
                            idx = raw_output.index(_marker)
                            output_str = raw_output[:idx].strip()[:500]  # 重新截断不含 diff 的部分
                            is_error = bool(output_str.strip().startswith("❌"))
                            try:
                                diff_data = json.loads(raw_output[idx + len(_marker):])
                                logger.debug("[DIFF] 成功提取 diff: added=%s removed=%s",
                                             diff_data.get("added"), diff_data.get("removed"))
                            except (json.JSONDecodeError, ValueError) as _de:
                                logger.warning("[DIFF] JSON 解析失败: %s", _de)
                            break
                    
                    # 提取文件路径（用于前端 diff 展示）
                    diff_file_path = ""
                    if diff_data and tinfo:
                        tool_input = tinfo.get("input", {})
                        if isinstance(tool_input, dict):
                            diff_file_path = tool_input.get("path", "")

                    yield _sse({
                        "type": "tool_result",
                        "tool": tool_name,
                        "step": step_for_tool,
                        "result": _truncate(output_str, 400),
                        "result_full": full_output if tool_name == "run_python" else "",
                        "error": is_error,
                        "diff": diff_data,
                        "diff_file_path": diff_file_path,
                    })

                    # ── 工具调用结束日志 ──
                    elapsed = time.time() - tinfo["started_at"] if tinfo else 0
                    result_preview = _truncate(output_str.strip(), 200)
                    logger.info(
                        "[TOOL_END] tool=%s step=%d run_id=%s duration=%.1fs error=%s result=%s",
                        tool_name, step_for_tool, run_id[:12], elapsed, is_error, result_preview,
                    )

                    # 并行子代理完成：发送每个子任务的状态更新
                    if tool_name in {"delegate_tasks_parallel", "delegate_task"} and subagent_capsules:
                        logger.info("[子代理] %s 执行完毕，准备合并...", tool_name)
                        try:
                            full_output = str(output)
                            updated = []
                            for cap in subagent_capsules:
                                upd = dict(cap)
                                upd["status"] = "done"
                                # 尝试从输出中提取该任务的执行结果
                                cap_id_str = f"#{cap['id']}"
                                idx_in_output = full_output.find(cap_id_str)
                                if idx_in_output >= 0:
                                    end_idx = min(idx_in_output + 600, len(full_output))
                                    upd["result"] = full_output[idx_in_output:end_idx]
                                updated.append(upd)
                            yield _sse({
                                "type": "subagent_end",
                                "capsules": updated,
                            })
                        except Exception:
                            # 简化降级：只标记状态
                            yield _sse({
                                "type": "subagent_end",
                                "capsules": [dict(cap, status="done") for cap in subagent_capsules],
                            })
                        subagent_capsules = []
                        logger.info("[子代理] subagent_end 已发送，等待父模型生成汇总回复...")
                        subagent_end_sent_at = time.time()
                    
                    # 重置推理上下文（但保留其他并行工具的进度状态）
                    in_tool_call = False
                    if not running_tools:
                        # 没有剩余工具时，也重置最后进度时间，避免 stale 进度事件
                        last_progress_at = 0.0

                    loop_reason = _detect_tool_loop(tool_call_history, run_config["recursion_limit"])
                    if loop_reason:
                        loop_guard_triggered = True
                        _done_yielded = True
                        final_buffer = _loop_guard_message(loop_reason, tool_call_history, run_config["recursion_limit"])
                        yield _sse({"type": "error", "content": final_buffer})
                        break
                elif kind == "on_chat_model_end" and node == "agent":
                    output = event.get("data", {}).get("output")
                    input_tok, output_tok, cached_tok = _extract_usage_tokens(output)
                    run_id = event.get("run_id", "")[:12]

                    # 记录 LLM 响应摘要
                    output_content = str(getattr(output, "content", ""))[:200] if output else ""
                    has_tool_calls = bool(getattr(output, "tool_calls", None)) if output else False
                    finish_reason = getattr(output, "response_metadata", {}).get("finish_reason", "") if output else ""
                    logger.info(
                        "[LLM_END] run_id=%s tokens=(in=%d out=%d cached=%d) finish=%s has_tool_calls=%s content=%s",
                        run_id, input_tok, output_tok, cached_tok,
                        finish_reason, has_tool_calls, output_content,
                    )

                    # 前端提示"AI 已响应"
                    yield _sse({"type": "llm_response", "has_tool_calls": has_tool_calls})

                    if input_tok > 0 or output_tok > 0:
                        self._record_model_usage(input_tok, output_tok, cached_tok, source="chat_model_end", thread_id=tid)
                        usage_recorded = True

            # 流结束，发送正常完成事件（含最终回复内容）
            if not _done_yielded:
                done_data = {"type": "done", "content": final_buffer}
                if current_todo_list:
                    done_data["todo_list"] = current_todo_list
                yield _sse(done_data)
                _done_yielded = True

        except asyncio.CancelledError:
            cancelled = True
            logger.info("[stream_run] 被取消，正常结束（不再重抛，确保 done 事件发出）")
        except GeneratorExit:
            cancelled = True
            logger.info("[stream_run] GeneratorExit，正常结束")
        except Exception as e:
            if _is_recursion_limit_error(e):
                logger.warning("[stream_run] 工具循环到达上限，结束任务")
                final_buffer = _recursion_limit_message(run_config["recursion_limit"])
                yield _sse({"type": "error", "content": final_buffer})
            else:
                logger.error("[stream_run] 异常: %s", e, exc_info=True)
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
            
            # 清理未完成的子代理胶囊
            if subagent_capsules:
                yield _sse({
                    "type": "subagent_end",
                    "capsules": [dict(cap, status="error") for cap in subagent_capsules],
                })
                subagent_capsules = []
        
        finally:
            logger.info(
                "[stream_run] 结束: tid=%s, thread_key=%s, tool_steps=%d, cancelled=%s, loop_guard=%s, final_len=%d, usage_recorded=%s",
                tid, thread_key, step_count, cancelled, loop_guard_triggered,
                len(final_buffer), usage_recorded,
            )
            self._hydrated_threads.add(thread_key)
            if attachments:
                await self._strip_checkpoint_images(run_config, graph)
            # 清理 todo 清单缓存（按 thread_id 清理，保留磁盘文件供恢复）
            try:
                from tools.todo_tools import pop_todo_list
                pop_todo_list(tid)
            except Exception:
                pass
            # 释放该会话的浏览器页面，避免跨会话页面状态串扰
            # 注意：必须使用 thread_key（完整 key），与工具函数中 RunnableConfig 读取的一致
            try:
                from tools.browser_tools import release_browser_page
                release_browser_page(thread_key)
            except Exception:
                pass
            if not loop_guard_triggered:
                if not usage_recorded:
                    self.tracker.record_model_call(
                        provider=self.config.active_provider,
                        model=self.config.model,
                        input_tokens=0,
                        output_tokens=0,
                        thread_id=tid,
                        source="chat_model_end",
                        estimated=True,
                    )
    
    async def _stream_done_wrapper(self, *args, **kwargs):
        """包装 stream_run，确保 \"done\" 事件在 finally 之外发送。
        
        stream_run 内部的 finally 块不能 yield（当 aclose() 调用时，
        Python 会抛出 RuntimeError），因此将 done 事件放在外层生成器发送。
        """
        done_yielded = False
        try:
            async for sse in self.stream_run(*args, **kwargs):
                yield sse
                if '"type": "done"' in sse or '"type": "error"' in sse:
                    done_yielded = True
        except GeneratorExit:
            # aclose() 被调用，stream_run 内部已处理 cleanup
            pass
        
        if not done_yielded:
            yield _sse({"type": "done", "content": ""})
            yield "data: [DONE]\n\n"
    
    def switch_thread(self, thread_id: str):
        self._thread_id = thread_id
    
    def reload_skills(self):
        """热加载技能 -> 重建 system prompt"""
        count = self.registry.reload()
        self._rebuild_graph()
        return count
