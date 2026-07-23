"""DesktopAgent 的模块级辅助函数与类（从 agent.py 抽出，便于维护与单测）。"""
import asyncio
import contextvars
import hashlib
import json
import os
import re
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator, Optional
from urllib.parse import urlparse

from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver

from config import AgentConfig
from context_manager import (
    checkpoint_replacement,
    compact_messages,
    compaction_threshold_tokens,
    estimate_message_tokens,
    estimate_messages_tokens,
    should_compact,
)
from logger import get_logger
from memory.local_memory import set_current_user
from monitoring.usage_tracker import get_tracker, UsageTracker
from network_resolver import configure_host_resolution
from skills.registry import get_registry, SkillRegistry
__all__ = ['logger', '_extract_tool_name', '_extract_tool_args', '_truncate', '_sse', '_tool_signature', '_tool_call_label', '_loop_guard_message', '_SCENE_PROMPTS', '_detect_scene', '_get_nested', '_extract_usage_tokens', '_message_text', '_normalize_messages', '_dump_context_profile', '_is_recursion_limit_error', '_recursion_limit_message', '_synthesize_guard_summary', '_connection_diagnostic', '_human_content', '_SCREENSHOT_URL_RE', '_strip_screenshot_urls_from_text', '_strip_image_content_from_message', '_strip_image_content_from_messages', '_tool_call_ids', '_tool_message_id', '_drop_dangling_tool_call_messages', '_recent_round_user_indexes', 'session_messages_to_langchain', 'compact_history_messages', '_extract_steps_from_messages', '_truncate_args', '_on_llm_idle_retry', '_astream_with_idle_timeout', 'RetryableLLM']


"""桌面 AI 智能体核心"""


logger = get_logger(__name__)


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


_SCENE_PROMPTS = {
    "coding": (
        "## 编程场景\n"
        "- 优先使用项目已有依赖和工具，不要引入新依赖。\n"
        "- 代码改动后先做语法检查再重启服务。\n"
        "- 遵循项目现有风格和架构，不做过度设计。"
    ),
    "browser": (
        "## 浏览器自动化场景\n"
        "- 优先使用 browser_ 系列工具操作页面。\n"
        "- 遇到验证码先识别再处理，不要盲目重试。\n"
        "- 操作前先确认页面状态和元素可见性。"
    ),
    "ppt": (
        "## PPT 制作场景\n"
        "- 使用 Node.js + PptxGenJS 生成 .pptx 文件。\n"
        "- 先确认主题和幻灯片内容，再调用脚本。\n"
        "- 输出文件保存在 reports/ 目录。"
    ),
    "article": (
        "## 文章写作场景\n"
        "- 先明确文章类型、受众和字数要求。\n"
        "- 结构化输出：标题 -> 大纲 -> 正文 -> 总结。\n"
        "- 避免空洞套话，给出具体可执行的内容。"
    ),
    "image": (
        "## 图片生成场景\n"
        "- 使用英文编写详细 prompt（主体+场景+风格+光照+构图+质量）。\n"
        "- 默认尺寸 1024x768，竖屏用 1024x1536。\n"
        "- 图片以 URL 形式返回，直接展示给用户。"
    ),
    "analysis": (
        "## 分析验证场景\n"
        "- 先看数据/日志/代码，不要凭印象下结论。\n"
        "- 区分事实、推测、待验证项，结论必须附带证据来源。\n"
        "- 涉及统计/趋势/对比时，先确认口径和样本范围。"
    ),
}


def _detect_scene(message: str, history: Optional[list[dict]] = None) -> str:
    """根据「最新一条用户消息」推断场景，返回命中的场景 key（空串表示未命中）。

    优先级 image / ppt 先于 coding：因为「用 Python 生成图片」这类请求会同时
    命中 coding(含 python) 与 image(含 生成图片)，按业务意图应判为 image 而非 coding。
    """
    text = message or ""
    # 当前消息为空时（如纯追问/继续），回退到历史里最后一条用户消息
    if not text and history:
        for m in reversed(history):
            if m.get("role") == "user":
                text = m.get("content") or ""
                if text:
                    break
    if not text:
        return ""
    msg = text.lower()
    # —— image / ppt 优先（避免被 coding 的关键词抢先命中）——
    if any(k in msg for k in ["图片", "画图", "image", "生成图片", "插画"]) or re.search(r'画.*图', msg) or re.search(r'生成.*图', msg):
        return "image"
    if any(k in msg for k in ["ppt", "演示文稿", "powerpoint", "pptx", "幻灯片"]):
        return "ppt"
    if any(k in msg for k in ["浏览器", "网页", "browser", "自动化", "打开网站", "截图"]):
        return "browser"
    if any(k in msg for k in ["文章", "写作", "blog", "写一篇", "文案"]):
        return "article"
    if any(k in msg for k in ["分析", "验证", "检查", "审查", "review", "debug", "测试", "调研", "数据", "统计", "为什么", "原因", "排查"]):
        return "analysis"
    if any(k in msg for k in ["代码", "编程", "coding", "实现", "开发", "修复bug", "重构", "python", "javascript"]):
        return "coding"
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


def _message_text(content) -> str:
    """把 LangChain 消息 content（str 或多模态 list）拼成纯文本，用于日志预览。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text", "")))
            else:
                parts.append(str(part))
        return "\n".join(parts)
    return str(content)


def _extract_reasoning(chunk) -> str:
    """从流式 chunk 中抽取推理/思考 token（推理模型专属）。

    不同厂商字段不同，这里做多源兼容：
      - DeepSeek / 兼容端点（agnes、qwen 推理版等）：chunk.reasoning_content
        或 chunk.additional_kwargs["reasoning_content"]
      - Claude extended thinking：chunk.additional_kwargs["thinking"]
      - 部分封装会把推理放在 additional_kwargs["reasoning"]

    非推理模型这些字段为空，返回 "" 不影响既有逻辑。
    """
    if chunk is None:
        return ""
    # 1) 直接属性（ChatOpenAI + DeepSeek 网关常直接暴露 reasoning_content）
    r = getattr(chunk, "reasoning_content", "")
    if isinstance(r, str) and r:
        return r
    # 2) additional_kwargs 兜底（langchain 把非标准字段放这里）
    ak = getattr(chunk, "additional_kwargs", None) or {}
    for key in ("reasoning_content", "reasoning", "thinking"):
        v = ak.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def _normalize_messages(inp):
    """把 LangChain on_chat_model_start 回调的 input 归一化为扁平的消息列表。

    LangChain 实际传入的结构可能是多层的，例如：
      - list[BaseMessage]
      - list[list[BaseMessage]]            (外层包了一层)
      - dict{'messages': list[BaseMessage]}
      - dict{'messages': list[list[BaseMessage]]}
    递归解开 list 包裹，直到首元素不再是 list，返回真正的消息对象列表。
    """
    if isinstance(inp, dict):
        inp = inp.get("messages", inp)
    while isinstance(inp, list) and len(inp) == 1 and isinstance(inp[0], list):
        inp = inp[0]
    return inp if isinstance(inp, list) else []


def _dump_context_profile(_input, run_id: str) -> int:
    """打印本次 LLM 调用的上下文画像，返回真实上下文 token 估算（用项目自带估算器）。

    真实 token 规模**总是计算并返回**（供 LLM_END 的 real= 字段直接对比网关虚高）；
    但详细的 [CTX] 画像日志与 [CTX_FULL] 全文 dump 仅由环境变量 AGENT_LOG_CONTEXT 开启：
      "1" -> INFO 级画像：消息数、真实 token、各消息类型/大小、Top8 大消息预览
      "2"/"full" -> 额外 DEBUG 级逐条 dump 每条消息全文（截断 4000 字防刷屏）
    """
    flag = os.getenv("AGENT_LOG_CONTEXT", "0").strip().lower()
    verbose = flag not in ("", "0", "false", "no", "off")
    full = flag in ("2", "full")
    _input = _normalize_messages(_input)
    if not isinstance(_input, list) or not _input:
        return 0
    try:
        from context_manager import estimate_messages_tokens, estimate_message_tokens

        rows = []
        for idx, m in enumerate(_input):
            mtype = getattr(m, "type", type(m).__name__)
            text = _message_text(getattr(m, "content", ""))
            tok = estimate_message_tokens(m) if hasattr(m, "type") else 0
            rows.append((idx, mtype, len(text), tok, text))
        total_tokens = estimate_messages_tokens([m for m in _input if hasattr(m, "type")])

        if not verbose:
            return total_tokens

        rows_sorted = sorted(rows, key=lambda r: r[3], reverse=True)
        type_counts: dict = {}
        for _, mtype, _, _, _ in rows:
            type_counts[mtype] = type_counts.get(mtype, 0) + 1

        logger.info(
            "[CTX] run_id=%s msgs=%d real_tokens≈%d "
            "(网关上报的 input_tokens 多为 session 累计虚高，真实规模以此为准)",
            run_id, len(_input), total_tokens,
        )
        logger.info("[CTX]   类型分布: %s", " ".join(f"{k}={v}" for k, v in sorted(type_counts.items())))
        for idx, mtype, clen, tok, text in rows_sorted[:8]:
            preview = text[:300].replace("\n", " ").replace("\r", " ")
            logger.info("[CTX]   #%-3d %-10s chars=%-7d tok=%-7d | %s", idx, mtype, clen, tok, preview)
        if full:
            for idx, mtype, clen, tok, text in rows:
                logger.debug("[CTX_FULL] #%d %s chars=%d tok=%d\n%s", idx, mtype, clen, tok, text[:4000])
        return total_tokens
    except Exception as e:
        logger.warning("[CTX] 上下文画像生成失败: %s", e)
        return 0


def _is_recursion_limit_error(exc: Exception) -> bool:
    return type(exc).__name__ == "GraphRecursionError"


def _recursion_limit_message(limit: int) -> str:
    return (
        f"执行步骤达到上限（recursion_limit={limit}），Agent 可能在反复调用工具或没有生成最终回答。"
        "可以在设置里调大“最大推理步数”，或把任务拆小后重试。"
    )


async def _synthesize_guard_summary(
    agent: "DesktopAgent",
    run_config: dict,
    subagent_results: list[dict],
    prior_final_buffer: str,
) -> str:
    """防循环触发后，基于已收集的工具/子代理结果生成真实汇总，而不是发送空壳占位。

    返回可直接作为最终回复的文本。任何异常都回退到已有的 final_buffer。
    """
    # 1) 优先使用子代理返回的真实结果
    context_parts: list[str] = []
    for r in subagent_results or []:
        task = r.get("task") or r.get("agent_type") or "子任务"
        result = (r.get("result") or "").strip()
        if result:
            context_parts.append(f"### 子任务：{task}\n{result}")

    # 2) 没有子代理结果时，尝试从图状态里取出工具返回内容
    if not context_parts:
        try:
            snapshot = await agent._graph.aget_state(run_config)
            msgs = list(getattr(snapshot, "values", {}).get("messages") or [])
            for m in msgs:
                if getattr(m, "type", "") == "tool" and getattr(m, "content", ""):
                    content = str(m.content).strip()
                    if len(content) > 20:
                        context_parts.append(content[:3000])
        except Exception:
            pass

    # 3) 没有可复用结果，但模型已流式输出过内容 → 直接保留
    if not context_parts:
        if prior_final_buffer.strip():
            return prior_final_buffer.strip()
        return "⚠️ 任务已被防循环机制提前结束，但没有可汇总的工具结果。"

    joined = "\n\n".join(context_parts)
    system_msg = SystemMessage(content=(
        "你是桌面 AI 智能体的汇总模块。下面是与用户任务相关的工具/子代理返回结果。"
        "请直接基于这些内容生成面向用户的最终回答，不要调用任何工具，"
        "也不要使用‘我将…’‘下面为你…’这类开场白，直接进入结论。"
    ))
    human_msg = HumanMessage(content=(
        f"以下是本轮已收集的结果：\n\n{joined}\n\n请直接给出最终回答（汇总结论）。"
    ))
    try:
        resp = await agent.llm.ainvoke([system_msg, human_msg])
        text = str(getattr(resp, "content", "")).strip()
        if text:
            return text
    except Exception as exc:  # noqa: BLE001
        logger.warning("[防循环] 生成汇总失败，回退到已有内容: %s", exc)
    return prior_final_buffer.strip() or context_parts[0][:2000]


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


def _recent_round_user_indexes(messages: list[dict], round_count: int = 5) -> set[int]:
    """计算最近 N 轮对话对应的 user 消息索引集合。

    一轮对话从一条 user 消息开始，到下一个 user 消息之前结束。
    只保留最近 `round_count` 条 user 消息的索引，用于决定是否保留历史图片。
    """
    if round_count <= 0:
        return set()

    user_indexes = [idx for idx, msg in enumerate(messages) if msg.get("role") == "user"]
    if len(user_indexes) <= round_count:
        return set(user_indexes)

    return set(user_indexes[-round_count:])


def session_messages_to_langchain(messages: list[dict]) -> list:
    """Convert persisted chat messages into LangChain messages.
    保留用户消息中的图片，让 LLM 能在后续轮次中看到历史图片。
    """
    converted = []
    keep_image_user_indexes = _recent_round_user_indexes(messages, round_count=5)
    for idx, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content") or ""
        images = msg.get("images") or []
        if role == "user":
            if images and idx in keep_image_user_indexes:
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
            # 只取 text 字段，丢弃 steps/steps_full 等前端展示用字段，避免 LLM 看到工具过程记录
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict) and parsed.get("text"):
                    content = parsed["text"]
            except (json.JSONDecodeError, TypeError):
                pass
            # 剥离历史截图引用，避免 LLM 在后续回复中重复输出
            content = _strip_screenshot_urls_from_text(content)[0]
            if content:
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


_retry_notifications_ctx: contextvars.ContextVar[list] = contextvars.ContextVar("retry_notifications", default=list)


def _on_llm_idle_retry(attempt: int, reason: str):
    """把 LLM 空闲超时重试事件记录到当前请求的队列，供 stream_run 转成 SSE 告知前端。"""
    try:
        _retry_notifications_ctx.get().append({"attempt": attempt, "reason": reason})
    except Exception:
        pass


async def _astream_with_idle_timeout(agen, idle_timeout: float):
    """包装异步生成器：若首块或任意相邻两块之间的间隔超过 idle_timeout，抛出 asyncio.TimeoutError。"""
    try:
        first = await asyncio.wait_for(agen.__anext__(), timeout=idle_timeout)
    except StopAsyncIteration:
        return
    yield first
    while True:
        try:
            chunk = await asyncio.wait_for(agen.__anext__(), timeout=idle_timeout)
        except StopAsyncIteration:
            return
        yield chunk


class RetryableLLM(Runnable):
    """包裹 chat model：当一次 LLM 调用在 idle_timeout 内未产出首个 token（或块间停顿过久）时，
    快速失败并**就地**重试该次模型调用——不重启整个 LangGraph 节点，已执行的工具结果会被保留。

    - 重试次数由 max_idle_retries 控制（默认 1）。
    - 每次重试通过 on_retry 回调上报，供上层转成 SSE 事件告知前端。
    - 非超时的其它异常直接上抛（连接级错误交给 LangChain 自带的 max_retries 处理）。
    """

    def __init__(self, llm, idle_timeout: float = 90.0, max_idle_retries: int = 1, on_retry=None):
        self.llm = llm
        self.idle_timeout = idle_timeout
        self.max_idle_retries = max_idle_retries
        self.on_retry = on_retry

    def bind_tools(self, tools, **kwargs):
        return RetryableLLM(
            llm=self.llm.bind_tools(tools, **kwargs),
            idle_timeout=self.idle_timeout,
            max_idle_retries=self.max_idle_retries,
            on_retry=self.on_retry,
        )

    async def astream(self, input, config=None, **kwargs):
        for attempt in range(self.max_idle_retries + 1):
            agen = None
            try:
                agen = self.llm.astream(input, config=config, **kwargs)
                async for chunk in _astream_with_idle_timeout(agen, self.idle_timeout):
                    yield chunk
                return
            except asyncio.TimeoutError:
                # 空闲超时：上报 + 清理挂起的连接 + 退避后重试（若还有次数）
                if self.on_retry and attempt < self.max_idle_retries:
                    try:
                        self.on_retry(attempt + 1, "idle_timeout")
                    except Exception:
                        pass
                if agen is not None:
                    try:
                        await agen.aclose()
                    except Exception:
                        pass
                if attempt < self.max_idle_retries:
                    await asyncio.sleep(min(2 ** attempt, 5))
                    continue
                raise
            except Exception:
                # 非超时异常：清理后上抛（连接错误由 LangChain 自带的 max_retries 兜底）
                if agen is not None:
                    try:
                        await agen.aclose()
                    except Exception:
                        pass
                raise

    async def ainvoke(self, input, config=None, **kwargs):
        # 统一走 astream，使「空闲超时 + 重试」逻辑只写在一处
        chunks = []
        async for chunk in self.astream(input, config=config, **kwargs):
            chunks.append(chunk)
        if not chunks:
            raise ValueError("LLM 未返回任何内容")
        merged = chunks[0]
        for c in chunks[1:]:
            merged = merged + c
        return merged

    def invoke(self, input, config=None, **kwargs):
        # 同步路径：直接透传到底层模型（流式/异步路径才走空闲超时 + 重试）。
        # LangGraph 的流式节点只调用 ainvoke，故此处无需重试逻辑。
        return self.llm.invoke(input, config=config, **kwargs)
