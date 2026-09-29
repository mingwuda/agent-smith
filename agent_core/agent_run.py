"""DesktopAgent 混入：run / 流式输出 / 检查点修复。"""
import asyncio
import contextvars
import json
import os
import re
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Optional
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
    compact_messages_report,
    compaction_threshold_tokens,
    estimate_message_tokens,
    estimate_messages_tokens,
    should_compact,
)
from logger import get_logger
from memory.local_memory import get_memory, set_current_user
from monitoring.usage_tracker import get_tracker, UsageTracker
from network_resolver import configure_host_resolution
from skills.registry import get_registry, SkillRegistry
from agent_helpers import *  # noqa: F401,F403
# import * 不导入下划线名;以下显式注入本文件实际引用的私有符号(共19个)
from agent_helpers import (
    _SCENE_PROMPTS, _connection_diagnostic, _detect_scene,
    _drop_dangling_tool_call_messages, _dump_context_profile,
    _extract_steps_from_messages, _extract_usage_tokens,
    _human_content, _is_recursion_limit_error, _extract_reasoning, _message_text,
    _model_supports_vision, _normalize_messages, _recursion_limit_message, _retry_notifications_ctx,
    _sse, _strip_image_content_from_messages, _strip_think_tags, _synthesize_guard_summary,
    _synthetic_ocr_sse_steps, _split_inflight_tail,
    set_overflow_compact_handler,
    _tool_signature, _truncate, _ensure_no_image_for_non_vision,
    is_image_input_error, record_model_image_unsupported,
    llm_waiting, _llm_wait_ctx, op_busy, OpBusy, _op_busy_ctx,
    resolve_outer_idle_timeout,
)
from loop_guard import _detect_tool_loop  # 原版 agent.py:169 的文件中间导入,拆分时需显式补回
from tools.shell_tools import drain_shell_output  # run_shell 实时输出（心跳循环 drain 队列）

logger = get_logger(__name__)


# ── LLM 摘要增强：把「规则摘要」改写为结构化 checkpoint（参考 dsh-compaction 的
#    COMPACTION_INSTRUCTION）。复用用户本次请求所选模型；写入压缩出的 checkpoint，
#    供后续轮次作为已建立背景继续推理。输出须为 Markdown、保留关键事实/路径/标识符。
_COMPACTION_LLM_TEMPLATE = (
    "现在你正在为这个 AI 编程助手扮演上下文压缩引擎。请把下面「规则摘要」中压缩的对话历史，"
    "改写为一份结构化 checkpoint，让另一个模型无需丢失关键信息即可继续这份工作。\n\n"
    "请严格按下面的 Markdown 结构输出（保留每一节、按顺序，用简洁要点而非大段描述，"
    "空节写 (none)，不要省略任何一节）：\n\n"
    "## 主要请求与意图\n"
    "- [用户的原始与演化目标；关键措辞尽量原文引用]\n\n"
    "## 关键技术概念\n"
    "- [涉及的框架、模式、约定]\n\n"
    "## 文件与代码\n"
    "- [确切路径：为什么重要、关键改动或片段]\n\n"
    "## 错误与修复\n"
    "- [错误：如何解决，以及相关用户反馈]\n\n"
    "## 当前工作\n"
    "- [本次 checkpoint 时正在进行的确切内容]\n\n"
    "## 下一步\n"
    "- [紧接最近一次请求的下一步动作，或写 (none)]\n\n"
    "## 关键上下文\n"
    "- [决策与理由、约束、用户偏好、待解决的问题、继续所需的数据]\n\n"
    "规则：\n"
    "- 用简洁的中文工程表述。保留确切的文件路径、命令、错误字符串、标识符、数字、函数签名与语法片段。\n"
    "- 忠实捕获用户反馈与明确指令，尤其是指正。\n"
    "- 不要提及本次压缩请求或上下文被压缩这件事。\n"
    "- 只输出 checkpoint 文本，不要调用工具或做其它动作。\n\n"
    "待改写的规则摘要如下：\n"
    "----------\n{summary}\n----------"
)


class AgentRunMixin:
    async def _llm_enhance_summary(self, rules_summary: str, model_override: str = "",
                                   provider_override: str = "") -> str:
        """用 LLM 把规则摘要改写为结构化 checkpoint（复用用户本次所选模型）。

        成功返回增强后的文本；任何失败（异常 / 空输出 / 构建失败）返回空串，
        由调用方回退「规则摘要」，绝不阻断压缩主流程。
        """
        text = (rules_summary or "").strip()
        if not text:
            return ""
        try:
            llm = self._build_llm(model_override, provider_override)
            resp = await llm.ainvoke([HumanMessage(content=_COMPACTION_LLM_TEMPLATE.format(summary=text))])
            content = getattr(resp, "content", "")
            if isinstance(content, list):
                content = "".join(
                    (part.get("text") or "") for part in content if isinstance(part, dict)
                )
            content = (content or "").strip()
            return content
        except Exception:
            logger.warning("[压缩] LLM 摘要增强调用失败，回退规则摘要", exc_info=True)
            return ""

    async def _maybe_llm_enhance(self, compacted, report, model_override, provider_override):
        """若压缩产出了规则摘要且可用，则用 LLM 改写成结构化 checkpoint。

        增强成功：把 compacted 中那条摘要 AIMessage 换成 LLM 输出，并同步 report.summary。
        失败 / 无摘要：原样返回，绝不阻断压缩。
        返回 (compacted, report)。
        """
        if report is None or not getattr(report, "summary", ""):
            return compacted, report
        enhanced = await self._llm_enhance_summary(
            report.summary, model_override, provider_override,
        )
        if not enhanced:
            return compacted, report
        # 替换 compacted 中唯一的那条「摘要 AIMessage」（压缩结构: system... + 摘要AIMessage + recent...）
        replaced = False
        out = []
        for m in compacted:
            if (not replaced and isinstance(m, AIMessage)
                    and not getattr(m, "tool_calls", None) and report.summary
                    and report.summary == (m.content if isinstance(m.content, str) else "")):
                out.append(AIMessage(content=enhanced))
                replaced = True
            else:
                out.append(m)
        if not replaced:
            # 未精确匹配到摘要消息（如内容被截断）：不强行插入，避免结构错乱
            return compacted, report
        report.summary = enhanced
        return out, report

    async def _force_compact_checkpoint(self, run_config: dict, model_override: str = "",
                                        provider_override: str = "") -> bool:
        """上下文溢出时的强制压缩：无论是否达到常规阈值都压缩一次 checkpoint。

        供 _inject_overflow_handler 用作 RetryableLLM 的 overflow handler。
        返回 True 表示确有压缩发生（重试 LLM 调用有希望）；False 表示放弃。
        """
        if not self._graph:
            return False
        try:
            snapshot = await self._graph.aget_state(run_config)
        except Exception:
            return False
        messages = list((getattr(snapshot, "values", {}) or {}).get("messages") or [])
        if not messages:
            return False
        compacted, report = compact_messages_report(
            messages, self.config.model, self.config.context_window_tokens,
            memory=get_memory(self._user_id), trigger="context-overflow",
            reason="模型报上下文超长，强制压缩后重试",
            use_jev_compaction=getattr(self.config, "jev_compaction_enabled", False),
        )
        # 未触发压缩（低于阈值）→ 无实际缩减，重试无益
        if report is None:
            return False
        # 与调用前压缩一致：自净 + 无对话兜底
        compacted, _ = _drop_dangling_tool_call_messages(compacted)
        if not compacted or not any(not isinstance(m, SystemMessage) for m in compacted):
            logger.warning("[压缩] 溢出强制压缩结果为空，放弃")
            return False
        # LLM 摘要增强（可选，复用本次模型；失败回退规则摘要）
        compacted, report = await self._maybe_llm_enhance(
            compacted, report, model_override, provider_override,
        )
        await self._graph.aupdate_state(run_config, {"messages": checkpoint_replacement(compacted)})
        logger.info(
            "🧹 上下文溢出强制压缩: %d -> %d messages, ~%d -> ~%d tokens",
            len(messages), len(compacted),
            report.before_tokens, estimate_messages_tokens(compacted),
        )
        return True

    def _bind_overflow_handler(self, run_config: dict, model_override: str,
                               provider_override: str) -> None:
        """把「overflow → 强制压缩 checkpoint」的 async 回调注入当前请求上下文。"""
        async def _handler() -> bool:
            return await self._force_compact_checkpoint(
                run_config, model_override, provider_override,
            )
        set_overflow_compact_handler(_handler)

    async def _compact_checkpoint_if_needed(self, run_config: dict, trigger: str = "auto", reason: str = "",
                                            model_override: str = "", provider_override: str = "") -> Optional[dict]:
        """LLM 调用前按需压缩 checkpoint。

        返回本次压缩的报告 dict（`context_compacted` SSE 事件载荷）；
        未触发压缩或压缩被放弃时返回 None。
        """
        if not self._graph:
            return None
        try:
            snapshot = await self._graph.aget_state(run_config)
        except Exception:
            return None
        values = getattr(snapshot, "values", {}) or {}
        messages = list(values.get("messages") or [])
        if not messages:
            return None
        # 硬上限：消息数超过 50 时强制压缩，避免长会话无限制膨胀
        if not should_compact(messages, self.config.model, self.config.context_window_tokens):
            if len(messages) > 50:
                logger.info("[压缩] 消息数=%d 超过硬上限 50，强制压缩", len(messages))
            else:
                return None
        compacted, report = compact_messages_report(
            messages, self.config.model, self.config.context_window_tokens,
            memory=get_memory(self._user_id), trigger=trigger, reason=reason,
            use_jev_compaction=getattr(self.config, "jev_compaction_enabled", False),
        )
        # 兜底：压缩切片仍可能在边界残留悬空/孤儿 tool 消息，写回前统一自净，
        # 保证喂给 graph 的历史永远满足 tool_call ↔ ToolMessage 配对（避免 INVALID_CHAT_HISTORY）。
        compacted, _ = _drop_dangling_tool_call_messages(compacted)
        # 兜底：压缩结果若不含任何对话消息（理论极端：摘要为空 + 最近轮残缺工具块被
        # dropper 清空），放弃本次压缩、保留原历史——宁可上下文大一点，也不让用户
        # 说"继续"时 agent 失去指代。
        if not any(not isinstance(m, SystemMessage) for m in compacted):
            logger.warning(
                "[压缩] 压缩后无任何对话历史（%d 条全被摘要/清理），放弃压缩，保留原 %d 条消息",
                len(compacted), len(messages),
            )
            return None
        # LLM 摘要增强：把规则摘要改写成结构化 checkpoint（复用用户本次所选模型；失败回退）
        compacted, report = await self._maybe_llm_enhance(
            compacted, report, model_override, provider_override,
        )
        await self._graph.aupdate_state(run_config, {"messages": checkpoint_replacement(compacted)})
        after = estimate_messages_tokens(compacted)
        _before_tok = report.before_tokens if report else estimate_messages_tokens(messages)
        logger.info(
            "🧹 上下文已压缩: %d -> %d messages, ~%d -> ~%d tokens, threshold=%d",
            len(messages), len(compacted), _before_tok, after,
            compaction_threshold_tokens(self.config.model, self.config.context_window_tokens),
        )
        if report is None:
            return None
        # 报告数字对齐实际写回结果（dropper 可能进一步减少消息）
        report.after_count = len(compacted)
        report.after_tokens = after
        return report.to_dict()

    async def _compact_checkpoint_before_tool(self, run_config: dict, trigger: str = "before_tool", reason: str = "",
                                              model_override: str = "", provider_override: str = "") -> Optional[dict]:
        """工具执行前按需压缩（用户需求：工具链中达到阈值即压缩，不等下次 LLM 调用）。

        与 `_compact_checkpoint_if_needed`（LLM 调用前压缩）的区别：
        - 触发点更早：工具刚启动就检查，长工具链（连续多轮 read_file/run_shell 等）
          期间上下文滚雪球时，压缩提前介入，而不是等工具链结束、下次 LLM 调用前才压。
        - 保护在飞工具：压缩前先 `_split_inflight_tail` 切出「当前正在执行的
          AI(tool_calls) 尾块」，只压缩 head。否则该块因缺少响应 ToolMessage 会被
          判残缺整块丢弃，正在执行的工具调用凭空消失（当前工具结果仍会写入，
          反而变成孤儿 ToolMessage）。tail 原样拼回，不动。
        - 未超阈值只做轻量读取（aget_state + 估算），不写 checkpoint。

        返回本次压缩的报告 dict（`context_compacted` SSE 事件载荷）；未触发返回 None。
        """
        if not self._graph:
            return None
        try:
            snapshot = await self._graph.aget_state(run_config)
        except Exception:
            return None
        values = getattr(snapshot, "values", {}) or {}
        messages = list(values.get("messages") or [])
        if not messages:
            return None
        head, tail = _split_inflight_tail(messages)
        if not head:
            return None
        # 硬上限同 LLM 前压缩：token 超阈值 或 消息数 > 50 才真正压缩
        if not should_compact(head, self.config.model, self.config.context_window_tokens):
            if len(head) <= 50:
                return None
        compacted, report = compact_messages_report(
            head, self.config.model, self.config.context_window_tokens,
            memory=get_memory(self._user_id), trigger=trigger, reason=reason,
            use_jev_compaction=getattr(self.config, "jev_compaction_enabled", False),
        )
        compacted, _ = _drop_dangling_tool_call_messages(compacted)
        if not compacted:
            return None
        # 兜底同 LLM 前压缩：结果无任何对话消息则放弃写回（保留原历史 + 在飞尾块）
        if not any(not isinstance(m, SystemMessage) for m in compacted):
            logger.warning(
                "[压缩] 工具前压缩结果无任何对话历史，放弃压缩，保留原历史",
            )
            return None
        # LLM 摘要增强：把规则摘要改写成结构化 checkpoint（复用用户本次所选模型；失败回退）
        compacted, report = await self._maybe_llm_enhance(
            compacted, report, model_override, provider_override,
        )
        await self._graph.aupdate_state(run_config, {"messages": checkpoint_replacement([*compacted, *tail])})
        after = estimate_messages_tokens(compacted)
        _before_tok = report.before_tokens if report else estimate_messages_tokens(head)
        logger.info(
            "🧹 工具前压缩: %d -> %d messages, ~%d -> ~%d tokens (阈值 %d), 在飞工具尾块 %d 条已保留",
            len(head), len(compacted), _before_tok, after,
            compaction_threshold_tokens(self.config.model, self.config.context_window_tokens),
            len(tail),
        )
        if report is None:
            return None
        report.after_count = len(compacted)
        report.after_tokens = after
        return report.to_dict()


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
        
        # 只扫描最近 20 条消息，更老的直接跳过（不清理，也不扫描）
        # 避免长会话中每次请求都全量遍历，导致越来越慢
        recent = messages[-20:]
        stripped, changed = _strip_image_content_from_messages(recent)
        if changed:
            # 只回写最近的消息部分，保留完整历史
            new_messages = messages[:-20] + stripped if len(messages) > 20 else stripped
            logger.info("[_strip_checkpoint_images] 已从最近 %d 条消息中移除图片/截图引用（共 %d 条）", len(recent), len(messages))
            await graph.aupdate_state(run_config, {"messages": checkpoint_replacement(new_messages)})


    def _thread_key(self, thread_id: str = "") -> str:
        tid = thread_id or self._thread_id
        return f"{self._user_id}:{tid}"


    def _run_config(self, thread_id: str = "") -> dict:
        # 入参是「裸会话 ID」（或空）；统一转成完整 thread_key（"{uid}:{sid}"）后再写入
        # configurable.thread_id。必须与 _thread_key()/工具/peek/注入钩子同一命名空间：
        # 曾因直接把裸 tid 当 thread_key（漏掉 _thread_key 的 uid 前缀），导致工具按裸 key
        # 写 _TODO_CACHE、链路按完整 key peek 读到 None（todo 事件不再发出、面板消失），
        # 且 inbox 注入钩子因 key 不含 ":" 而永久放弃注入（实时干预静默失效）。
        thread_key = self._thread_key(thread_id)
        # 命名空间不变式：thread_key 必须是 "{uid}:{sid}"。工具写缓存 / peek 读缓存 /
        # inbox 注入钩子全部按这个 key 对齐，漏掉 uid 前缀会「静默失效」（todo 面板消失、
        # 实时干预不注入，见 d1d9b8b 回归）。这里把静默错误变成显式崩溃，便于早发现。
        assert ":" in thread_key, f"thread_key 缺少 uid 前缀（应为 'uid:sid'）: {thread_key!r}"
        limit = max(1, int(self.config.recursion_limit or 60))
        # 关闭防循环时放宽递归上限，交由用户手动终止任务
        if not getattr(self.config, "enable_loop_guard", True):
            limit = max(limit, 1000)
        return {
            "configurable": {"thread_id": thread_key},
            "recursion_limit": limit,
        }


    def _rebuild_graph(self):
        self._graph = self._create_graph()


    def _get_graph(self, model_override: str = "", provider_override: str = ""):
        """获取当前可用的编译图，统一处理两种取图场景。

        - model_override / provider_override 给定时总是重新编译（用于按请求切换模型/provider），不缓存。
        - 否则若缓存的 self._graph 为 None（例如 set_workspace 使其失效），
          则惰性重建并缓存，避免重复编译。
        """
        if model_override or provider_override:
            return self._create_graph(model_override, provider_override)
        if self._graph is None:
            self._graph = self._create_graph()
        return self._graph


    async def run(
        self,
        message: str,
        history: Optional[list[dict]] = None,
        attachments: Optional[list[dict]] = None,
        model_override: str = "",
        thread_id: str = "",
        provider_override: str = "",
    ) -> tuple[str, list[dict]]:
        """处理用户消息，返回 (最终回复, 中间步骤列表)

        参数:
          thread_id: 当前会话 ID，取代全局 self._thread_id（支持并发）
          provider_override: 可选的 provider id，覆盖本次请求使用的模型后端（不改动全局 active_provider）
        """
        tid = thread_id or self._thread_id
        config = self._run_config(tid)
        # 取可用 graph：model_override / provider_override 时重建，缺失时惰性重建（见 _get_graph）
        graph = self._get_graph(model_override, provider_override)
        # 为本请求建立独立的 LLM 重试通知队列（非流式调用也会走 RetryableLLM）
        _retry_notif_token = _retry_notifications_ctx.set([])
        # 限流等待窗口同样按请求隔离：可变容器跨 context 共享，RetryableLLM 写、is_busy 读
        _llm_wait_ctx.set([0.0])
        # 「无事件长任务」（如上下文压缩）的忙计数容器，同样按请求隔离
        _op_busy_ctx.set([0])
        input_messages = []
        thread_key = self._thread_key(tid)
        await self._repair_checkpoint_tool_history(config, graph)
        await self._strip_checkpoint_images(config, graph)
        # ponytail: 用"本次解析出的 provider/model"判断视觉能力，而非全局 config.model——
        # 切换厂商发送时，实际模型是该厂商自己的 model（如 step-router-v1），否则会误判为
        # 支持视觉、图片原样直发非视觉模型 → 400 image input not supported。
        _run_pid, _run_mdl, *_ = self._resolve_provider_config(model_override, provider_override)
        _ocr_fallback = not _model_supports_vision(self.config, provider_id=_run_pid, model=_run_mdl)
        if thread_key not in self._hydrated_threads:
            input_messages = compact_history_messages(session_messages_to_langchain(history or [], ocr_fallback=_ocr_fallback), self.config, memory=get_memory(self._user_id))
        ocr_sink: list = []
        current_content = _human_content(message, attachments, ocr_fallback=_ocr_fallback, ocr_sink=ocr_sink)
        input_messages.append(HumanMessage(content=current_content))
        # 兜底：模型不支持视觉时，清除任何残留 image_url（防御未来新路径漏图）
        input_messages = _ensure_no_image_for_non_vision(input_messages, self.config, provider_id=_run_pid, model=_run_mdl)

        # ── 按场景注入专项指导（仅在命中时插入 SystemMessage，不污染基础 prompt）──
        scene = _detect_scene(message, history)
        if scene:
            scene_prompt = _SCENE_PROMPTS.get(scene)
            if scene_prompt:
                input_messages.insert(0, SystemMessage(content=scene_prompt))
                logger.info(
                    "[场景注入] tid=%s 命中场景: %s",
                    tid, scene,
                )

        logger.info(
            "[run] 开始: tid=%s, thread_key=%s, provider=%s, model=%s, message_len=%d",
            tid, thread_key,
            provider_override or self.config.active_provider,
            model_override or self.config.model, len(message),
        )
        _run_started_at = time.time()

        try:
            # 上下文溢出时自动压缩 checkpoint 并重试（RetryableLLM 内通过 ContextVar 取到该 handler）
            self._bind_overflow_handler(config, model_override, provider_override)
            # 在 LLM 调用前按需压缩 checkpoint，记录压缩报告以便历史回放时展示
            compaction_step = None
            _compact_report = await self._compact_checkpoint_if_needed(config, model_override=model_override, provider_override=provider_override)
            if _compact_report:
                compaction_step = {"type": "context_compacted", **_compact_report}
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
            # 方案A：本轮图片被 OCR 降级（模型不支持视觉）时，把降级动作补成 synthetic
            # 工具步骤，让非流式 /run 的返回 steps 也能体现"识别图片"这一工作过程。
            if ocr_sink:
                steps = _synthetic_ocr_sse_steps(ocr_sink) + steps
            # 将压缩报告插入步骤列表开头，让历史回放时也能看到压缩卡片
            if compaction_step:
                steps = [compaction_step] + steps
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
                        self._tracker.record_model_call(
                            provider=provider_override or self.config.active_provider,
                            model=model_override or self.config.model,
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
            # ponytail: API 拒绝图片输入 → 自动把当前模型标记为"实际不支持视觉"，
            # 下次调用即走视觉路由（图片交给用户标记的真实视觉模型 → 描述 → 文本）。
            # 仅 in-memory，进程重启后清空，给厂商升级/换模型留机会。
            if attachments and is_image_input_error(e):
                # ponytail: 必须用 _resolve_provider_config 解析"本次实际使用的模型"，
                # 不能直接取 self.config.model——发送区切换 provider 后，实际模型是
                # 该 provider 自己的 model（如 step-router-v1），而非全局 config.model
                # （如 agnes-2.5-flash）。取错会把视觉模型冤枉成"不支持图片"。
                pid, mdl, _key, _url, _is_anth = self._resolve_provider_config(
                    model_override, provider_override
                )
                record_model_image_unsupported(pid, mdl)
                return (
                    f"❌ 当前模型 {mdl}（厂商 {pid}）不支持图片输入，已自动标记为非视觉模型。"
                    f"\n👉 请重试本条消息：图片会先发给你标记过的视觉模型生成描述，"
                    f"再由 {mdl} 基于描述继续工作。"
                    f"\n💡 永久修复：在「设置 → 模型」取消 {mdl} 的 👁 视觉标记，"
                    f"或在发送区切换到已标记视觉模型的厂商。",
                    [],
                )
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
            # 复位本请求的 LLM 重试通知队列（避免 ContextVar 泄漏到其它请求）
            try:
                _retry_notifications_ctx.reset(_retry_notif_token)
            except Exception:
                pass


    async def _stream_events_with_heartbeat(
        self,
        graph,
        input_data: dict,
        run_config: dict,
        heartbeat_interval: float = 2.0,
        timeout: float = 90.0,
        is_busy: Optional[Callable[[], bool]] = None,
    ) -> AsyncGenerator[dict, None]:
        """流式获取 LangGraph 事件，并定期产生心跳事件。

        心跳事件格式为 {"_heartbeat": True}。此实现用独立的心跳任务替代
        asyncio.shield，避免底层事件任务异常未被消费而触发 asyncio 的
        "exception in shielded future" 告警。

        如果 timeout 秒内无任何事件（LLM/工具卡死），产生 {"_timeout": True} 事件后结束，
        消费端应据此返回超时错误，避免无限挂起。

        is_busy: 可选回调，每次超时检查时询问「是否正忙」。返回 True（如有工具正在执行）
                则跳过空闲超时——工具执行期间 LangGraph 不产生任何事件（on_tool_start
                与 on_tool_end 之间），时长由工具自身 timeout 控制，不应被 LLM 空闲超时误杀。
        """
        event_iter = graph.astream_events(input_data, run_config, version="v2").__aiter__()
        event_task = asyncio.create_task(event_iter.__anext__())
        heartbeat_task = asyncio.create_task(asyncio.sleep(heartbeat_interval))
        last_event_at = time.time()
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
                    now = time.time()
                    # 超时检查：距上次任何事件已超过 timeout 秒 → 强制结束
                    if now - last_event_at > timeout:
                        if is_busy and is_busy():
                            # 有工具/上下文压缩/限流等待在进行时，LangGraph 不产生事件，空闲超时不适用；
                            # 顺带把计时基准推到当前 —— 否则「超长耗时操作刚结束」会被这段时间的旧账
                            # 立刻判超时而误杀（操作本身合法，只是没有事件）。
                            last_event_at = now
                            logger.debug("[stream_events] 忙（工具/压缩/等待）中，跳过空闲超时并重置计时")
                        else:
                            logger.warning(
                                "[stream_events] 超时: 距上次事件 %.1fs（阈值 %.1fs），强制结束",
                                now - last_event_at, timeout,
                            )
                            yield {"_timeout": True, "reason": f"no event for {now - last_event_at:.1f}s"}
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
                    except Exception as exc:
                        logger.warning("[stream_events] 事件消费异常: %s", exc, exc_info=True)
                        yield {"_stream_event_error": str(exc)}
                        # 事件流已处于错误态, 重建 task 会立即再次抛同一异常,
                        # 若用 continue 会陷入无限循环(每轮对同一个已失败 task 调 .result() 反复抛异常)。
                        # 改为 return: 仅产出一次错误事件, 让消费端 async for 自然结束。
                        return
                    # 只在成功拿到事件时更新 last_event_at — ponytail: 若 event_task 被外部取消,
                    # CancelledError 在此处抛出, last_event_at 保持原值, 下次心跳时超时检查会触发,
                    # 产出 _timeout 事件而非被静默取消。
                    last_event_at = time.time()
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
        provider_override: str = "",
    ) -> AsyncGenerator[str, None]:
        """流式处理用户消息，yield SSE 格式事件

        参数:
          thread_id: 当前会话 ID，取代全局 self._thread_id（支持并发）
          provider_override: 可选的 provider id，覆盖本次请求使用的模型后端（不改动全局 active_provider）
        """
        tid = thread_id or self._thread_id
        run_config = self._run_config(tid)
        # 取可用 graph：model_override / provider_override 时重建，缺失时惰性重建（见 _get_graph）
        graph = self._get_graph(model_override, provider_override)
        # 为本请求建立独立的 LLM 重试通知队列（并发安全：每个会话各自隔离）
        _retry_notif_list: list = []
        _retry_notif_token = _retry_notifications_ctx.set(_retry_notif_list)
        # 限流等待窗口同理按请求隔离：RetryableLLM 写、is_busy 读（可变容器跨 context 共享）
        _llm_wait_ctx.set([0.0])
        # 「无事件长任务」（如 in-loop 上下文压缩）的忙计数容器，同样按请求隔离
        _op_busy_ctx.set([0])
        input_messages = []
        thread_key = self._thread_key(tid)
        await self._repair_checkpoint_tool_history(run_config, graph)
        await self._strip_checkpoint_images(run_config, graph)
        ocr_sink: list = []
        # ponytail: 用"本次解析出的 provider/model"判断视觉能力，而非全局 config.model——
        # 切换厂商发送时，实际模型是该厂商自己的 model（如 step-router-v1），否则会误判为
        # 支持视觉、图片原样直发非视觉模型 → 400 image input not supported。
        _run_pid, _run_mdl, *_ = self._resolve_provider_config(model_override, provider_override)
        _ocr_fallback = not _model_supports_vision(self.config, provider_id=_run_pid, model=_run_mdl)
        if thread_key not in self._hydrated_threads:
            input_messages = compact_history_messages(session_messages_to_langchain(history or [], ocr_fallback=_ocr_fallback), self.config, memory=get_memory(self._user_id))
        input_messages.append(HumanMessage(content=_human_content(message, attachments, ocr_fallback=_ocr_fallback, ocr_sink=ocr_sink)))
        # 插件事件钩子：on_message（用户消息进入 agent 处理前广播）。
        # 单个插件监听器异常已被 PluginHost.emit 隔离，绝不影响主流程。
        # 广播后把插件经 host.push_sse 排队的前端事件随之 yield 成 SSE 帧（E 注入点）。
        try:
            from plugin_loader import get_registry as _get_plugin_reg
            _plug = _get_plugin_reg()
            _plug.emit("on_message", {
                "text": message, "user_id": self._user_id, "session_id": tid,
            })
            for _ev_name, _ev_payload in _plug.drain_sse(tid):
                yield _sse({"type": "plugin_event", "event": _ev_name, "payload": _ev_payload})
        except Exception:
            pass
        # 兜底：模型不支持视觉时，清除任何残留 image_url（防御未来新路径漏图）
        input_messages = _ensure_no_image_for_non_vision(input_messages, self.config, provider_id=_run_pid, model=_run_mdl)

        # ── 按场景注入专项指导（仅在命中时插入 SystemMessage，不污染基础 prompt）──
        scene = _detect_scene(message, history)
        if scene:
            scene_prompt = _SCENE_PROMPTS.get(scene)
            if scene_prompt:
                input_messages.insert(0, SystemMessage(content=scene_prompt))
                logger.info(
                    "[场景注入] tid=%s 命中场景: %s",
                    tid, scene,
                )

        # ── 按需注入命中的技能完整指令（命中触发，避免把全部技能塞进 system prompt）──
        # system prompt 里只放精简目录（name+描述+触发词）；当用户输入命中某技能触发词/名称时，
        # 才把该技能的完整工作流注入到当前用户消息末尾，仅本轮生效。
        injected_skills = self.registry.find_by_prompt(message) if message else []
        if injected_skills:
            inject_block = self.registry.render_injection_block(injected_skills)
            last_human = input_messages[-1]
            if isinstance(last_human.content, list):
                last_human.content.append({"type": "text", "text": inject_block})
            else:
                last_human.content = str(last_human.content) + "\n\n" + inject_block
            logger.info(
                "[技能注入] tid=%s 命中 %d 个技能: %s",
                tid, len(injected_skills), ", ".join(s.name for s in injected_skills),
            )
        input_data = {"messages": input_messages}
        
        thinking_buffer = ""        # 累积推理文本（工具调用前的内容）
        reasoning_buffer = ""       # 累积推理模型的思考 token（reasoning_content），不进入最终答案
        final_buffer = ""           # 最终回复缓存
        # 方案A：OCR 降级 synthetic 卡片占用 step 1..N，真实工具的 step 从 N+1 开始，
        # 保证 step 编号不重复（前端 _toolTimers 以 step 为 key）。
        step_count = len(ocr_sink)
        in_tool_call = False        # 当前是否正在产生工具调用
        usage_recorded = False
        running_tools: dict[str, dict] = {}   # run_id -> {name, step, started_at}
        last_progress_at = 0.0
        cancelled = False
        loop_guard_triggered = False
        truncated_final = False          # 主模型输出被 max_tokens 截断（finish_reason=length）
        graph_steps = 0                  # 真实图步数累计（模型/工具各计一步），供防循环步数估算
        _done_yielded = False
        tool_call_history: list[dict] = []
        subagent_capsules: list[dict] = []  # 并行子代理任务胶囊数据
        _subagent_dispatched = False         # 是否已派发过子代理
        _post_subagent_tool_calls = 0        # 子代理完成后父模型继续调用的工具次数
        _post_subagent_seen_run_ids: set[str] = set()  # 已统计过的非子代理工具 run_id（避免重试重复计数）
        subagent_end_sent_at = 0.0           # 子代理结束事件发送时间戳
        _subagent_results: list[dict] = []   # 子代理完成后收集的结果（防循环触发时用于生成真实汇总）
        last_model_activity_at = 0.0         # 最后一次模型活动（token/thought/tool）时间戳
        # fix #1: 单次 LLM 调用硬墙钟超时状态
        llm_call_in_flight = False           # 当前是否有 LLM 调用在飞（node=agent）
        llm_silent_since = 0.0               # 硬超时计时钟：上次真实输出/调用开始的时刻；空 keepalive 不刷新
        _stream_chunk_idx = 0                # 当前 LLM 调用的流式 chunk 计数，用于调试首块结构
        
        # 当前 todo 清单数据（随 manage_todo 工具调用更新）
        current_todo_list = None
        
        try:
            logger.info(
                "[stream_run] 开始: tid=%s, thread_key=%s, provider=%s, model=%s, timeout=%s, message_len=%d",
                tid, thread_key,
                provider_override or self.config.active_provider,
                model_override or self.config.model,
                self.config.api_timeout_seconds,
                len(message),
            )
            self._bind_overflow_handler(run_config, model_override, provider_override)
            _compact_report = await self._compact_checkpoint_if_needed(run_config, model_override=model_override, provider_override=provider_override)
            if _compact_report:
                yield _sse({"type": "context_compacted", **_compact_report})
            # 方案A：OCR 降级动作以 synthetic 工具卡片先行发出。纯文本模型收到图片时，
            # 图片在进入 LLM 前已被转成 OCR 文本，模型不会真的调用 ocr_image 工具，
            # 历史里就看不到"识别图片"步骤；这里补发 tool_start/tool_result 事件对，
            # 前端实时流与历史回放（collected_steps）都能渲染「调用工具: ocr_image」卡片。
            for _syn_ev in _synthetic_ocr_sse_steps(ocr_sink):
                yield _sse(_syn_ev)
            # 外层空闲看门狗阈值：自动保证 ≥ 内层 idle 重试总预算，避免「外层抢跑使内层重试失效」
            # （历史故障：外层硬编码 90s < 内层 120s → 上游完全静默时空闲重试永远跑不到）
            llm_timeout = resolve_outer_idle_timeout(self.config)
            logger.info(
                "[stream_run] 外层空闲看门狗阈值=%.0fs（配置 llm_timeout_seconds=%s；内层预算=idle %.0fs×%d 次）",
                llm_timeout,
                getattr(self.config, "llm_timeout_seconds", 0.0) or "自动",
                getattr(self.config, "llm_idle_timeout_seconds", 0.0),
                int(getattr(self.config, "llm_idle_max_retries", 0) or 0) + 1,
            )
            # fix #1: 单次 LLM 调用的硬墙钟上限（秒）。上游挂起（连接开着但无首 token/无结束）时，
            # 即便心跳与 RetryableLLM 重试不断刷新现有计时器，此墙钟也会强制终止该轮。
            llm_hard_timeout = getattr(self.config, "llm_hard_timeout_seconds", 600.0)
            async for event in self._stream_events_with_heartbeat(
                graph, input_data, run_config, timeout=llm_timeout,
                # ponytail: 工具执行期间（on_tool_start→on_tool_end）LangGraph 不产生事件，
                # 空闲超时会误杀长跑工具（如 600s 的 run_shell）。有工具在跑时跳过空闲超时，
                # 工具时长由其自身 timeout 控制；无工具时仍按 llm_timeout 兜底防 LLM 挂起。
                # 限流等待期间 LLM 在休眠、上下文压缩期间同样零图事件，一并豁免，避免被误判卡死强杀。
                is_busy=lambda: bool(running_tools) or llm_waiting() or op_busy(),
            ):
                # ── 超时事件：LLM/工具长时间无响应 ──
                if event.get("_timeout"):
                    logger.error(
                        "[stream_run] 超时: %s，tid=%s",
                        event.get("reason", "unknown"), tid,
                    )
                    yield _sse({
                        "type": "error",
                        "content": f"模型响应超时（{llm_timeout} 秒内无响应），请重试或切换模型。",
                    })
                    _done_yielded = True
                    return
                # 把 RetryableLLM 上报的重试事件（空闲超时 / 限流 429）转成 SSE，提示前端正在重试
                if _retry_notif_list:
                    for _note in _retry_notif_list:
                        _is_rl = _note.get("reason") == "rate_limit"
                        yield _sse({
                            "type": "llm_retry",
                            "attempt": _note["attempt"],
                            "reason": _note["reason"],
                            "wait": _note.get("wait", 0),
                            "max": (getattr(self.config, "llm_rate_limit_max_retries", 3)
                                    if _is_rl else self.config.llm_idle_max_retries),
                        })
                    _retry_notif_list.clear()
                if event.get("_stream_event_error"):
                    err_msg = str(event["_stream_event_error"])
                    # 区分「模型超时」与一般性事件流异常，给出可读提示（不再伪装成工具错误卡片）
                    is_timeout = ("TimeoutError" in err_msg) or ("timed out" in err_msg.lower())
                    if is_timeout:
                        logger.error("[stream_run] 模型事件流超时中断 tid=%s: %s", tid, err_msg[:300])
                        yield _sse({
                            "type": "error",
                            "content": "模型响应超时，本次回复已中断，请重试或切换模型。",
                        })
                    else:
                        logger.error("[stream_run] 模型事件流异常 tid=%s: %s", tid, err_msg[:300])
                        # ponytail: API 拒绝图片输入 → 自动把当前模型标记为"实际不支持视觉"，
                        # 下次调用即走视觉路由（图片交给真视觉模型描述 → 文本喂给当前模型）。
                        # 仅 in-memory，进程重启后清空，给厂商升级/换模型留机会。
                        # 注意：本分支是流式路径最内层捕获点，错误不会传播到外层 handler，
                        # 所以自适应标记必须在这里做，否则永远触发不到。
                        if attachments and is_image_input_error(err_msg):
                            # ponytail: 必须用 _resolve_provider_config 解析"本次实际使用的模型"，
                            # 不能直接取 self.config.model——发送区切换 provider 后，实际模型是
                            # 该 provider 自己的 model（如 step-router-v1），而非全局 config.model
                            # （如 agnes-2.5-flash）。取错会把视觉模型冤枉成"不支持图片"。
                            pid, mdl, _k, _u, _a = self._resolve_provider_config(
                                model_override, provider_override
                            )
                            record_model_image_unsupported(pid, mdl)
                            yield _sse({
                                "type": "error",
                                "content": (
                                    f"❌ 当前模型 {mdl}（厂商 {pid}）不支持图片输入，已自动标记为非视觉模型。"
                                    f"\n👉 请重试本条消息：图片会先发给你标记过的视觉模型生成描述，"
                                    f"再由 {mdl} 基于描述继续工作。"
                                    f"\n💡 永久修复：在「设置 → 模型」取消 {mdl} 的 👁 视觉标记，"
                                    f"或在发送区切换到已标记视觉模型的厂商。"
                                ),
                            })
                        else:
                            yield _sse({
                                "type": "error",
                                "content": f"模型事件流异常，本次回复已中断：{err_msg[:200]}",
                            })
                    _done_yielded = True
                    return
                if event.get("_heartbeat"):
                    now = time.time()
                    # fix #1: 单次 LLM 调用硬墙钟超时。上游挂起（连接开着但无首 token / 无结束）时，
                    # 心跳仍规律发出，故在此检测。计时钟只在「真实 token/推理」或「调用开始（且仅当此前无调用在飞）」
                    # 时刷新；空 keepalive chunk 与 RetryableLLM 的重试（新 run_id 的 on_chat_model_start）都不会重置它。
                    if llm_call_in_flight and (now - llm_silent_since) >= llm_hard_timeout:
                        logger.error(
                            "[stream_run] 单次 LLM 调用硬超时 %.0fs（>=%.0fs），强制终止 tid=%s",
                            now - llm_silent_since, llm_hard_timeout, tid,
                        )
                        yield _sse({
                            "type": "error",
                            "content": f"模型单轮响应超时（{int(llm_hard_timeout)} 秒内未返回有效内容），请重试或切换模型。",
                        })
                        _done_yielded = True
                        return
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
                    # run_shell 实时输出转发：drain 工具执行期间入队的输出块，SSE 推给前端
                    # （方案B：心跳注入，粒度 ≈ heartbeat_interval，2s 一次）
                    try:
                        _shell_chunk = drain_shell_output()
                    except Exception:
                        _shell_chunk = ""
                    if _shell_chunk:
                        for _rid, _tinfo in list(running_tools.items()):
                            if _tinfo["name"] == "run_shell":
                                yield _sse({
                                    "type": "tool_output",
                                    "step": _tinfo["step"],
                                    "content": _shell_chunk,
                                })
                                break
                    if running_tools:
                        last_progress_at = now
                    else:
                        # 无运行中工具时仍发送 ping 事件，避免连接因空闲断开
                        yield _sse({"type": "ping"})

                    # ── ask_user 征询：drain 该会话新鲜待响应的 ask，推 SSE 给前端渲染弹窗 ──
                    # ask_user 工具阻塞等待用户响应期间，图不产生事件（on_tool_start→on_tool_end 之间），
                    # 靠心跳循环把待响应的问卷推给前端；用户提交经 API resolve 后工具才返回、图才继续。
                    # 用 not_emitted 去重，避免同一 ask 在每次心跳重复弹窗。
                    try:
                        from tools.ask_user_tools import get_registry as _ask_reg
                        _ask_asks = _ask_reg().pending_asks(thread_key)
                        _ask_thread = thread_key  # "{uid}:{sid}"
                        for _a in _ask_asks:
                            if _ask_reg().not_emitted(thread_key, _a["ask_id"]):
                                _ask_reg().mark_emitted(thread_key, _a["ask_id"])
                                # 把 session_id（裸 id）与 thread_key 一并带上：
                                # 前端弹窗绑定其所属会话，提交时用它而非常用全局 currentSessionId，
                                # 避免弹窗期间切换会话导致 resolve 打到别的会话、ask 找不到 → 「征询提交失败」。
                                yield _sse({"type": "ask_user_modal", "session_id": tid,
                                            "thread_key": _ask_thread, **_a})
                    except Exception:
                        pass
                    # 子代理结束但父模型长时间没有产生最终回复，强制终止
                    # 以 subagent_end 发送时间为基准，避免父模型内部的慢速/空轮询刷新 idle 时间
                    if self.config.enable_loop_guard and subagent_end_sent_at and not running_tools and not loop_guard_triggered:
                        # ponytail: 以"最后一次模型活动"为基准计时，任何 token/thought/工具开始都会刷新，
                        # 避免父模型慢速生成长总结（持续有输出）时被误杀；仅在 subagent_end 之后且无运行中工具的真空闲才计时。
                        idle_since = max(subagent_end_sent_at, last_model_activity_at)
                        idle_after_subagent = now - idle_since
                        if idle_after_subagent >= 90:
                            logger.warning("[子代理] 父模型已 %d 秒无活动（自 subagent_end），强制终止", int(idle_after_subagent))
                            loop_guard_triggered = True
                            # 基于已收集的子代理结果生成真实汇总（而非空壳占位）
                            final_buffer = await _synthesize_guard_summary(
                                self, run_config, _subagent_results, final_buffer
                            )
                            done_data = {"type": "done", "content": final_buffer}
                            if current_todo_list:
                                done_data["todo_list"] = current_todo_list
                            _done_yielded = True
                            yield _sse(done_data)
                            break
                    continue

                # 防御：任何未被上述特殊分支（_timeout/_stream_event_error/_heartbeat/重试通知）命中的
                # 异常或未知事件，跳过以防 KeyError 崩溃（例如运行旧版代码时冒泡进来的异常字典）。
                if "event" not in event:
                    logger.warning("[stream_run] 收到无 'event' 键的未知事件，跳过: keys=%s", list(event.keys()))
                    continue
                kind = event["event"]
                node = event.get("metadata", {}).get("langgraph_node", "")
                logger.debug("[stream_run] 事件: kind=%s node=%s", kind, node)
                
                # ── LLM 调用开始（日志 + 前端状态提示）──
                if kind == "on_chat_model_start" and node == "agent":
                    # ── 实时干预：pre_model_hook 刚在此 LLM 边界注入了 next_step 消息，转发给前端 ──
                    # pre_model_hook 节点在 agent 节点之前运行，注入完成后记录事件；此处正是该消息
                    # 进入 LLM 上下文的时刻，drain 并作为 SSE 告知用户「打断已生效」。
                    try:
                        from inbox import get_inbox_manager
                        for _inj in get_inbox_manager().drain_injected(self._user_id, tid):
                            yield _sse(_inj)
                    except Exception:
                        pass
                    graph_steps += 1
                    # fix #1: 标记 LLM 调用在飞；仅在「此前无调用在飞」时启动硬超时计时钟，
                    # 这样 RetryableLLM 的重试（新 run_id 的 start）不会把计时钟清零，避免无限挂起。
                    if not llm_call_in_flight:
                        llm_silent_since = time.time()
                    llm_call_in_flight = True
                    # fix #2: 单轮内周期性压缩。工具步累积使上下文滚雪球时，在每次（首轮之后）LLM 调用
                    # 开始前触发一次压缩——函数内部按 token 阈值 / 50 条硬上限自判，未超阈值只做轻量读取、不写。
                    # 此处是改写 checkpoint 最安全的时机：图刚结束上一步 checkpoint 写入、尚未开始本轮 LLM 写入，
                    # 不与图循环竞争；压缩只影响「下一轮」LLM 上下文，当前在飞调用已加载完消息、不受影响。
                    if graph_steps >= 2:
                        # 单轮内（工具执行后、下次 LLM 调用前）先自净残缺 tool 配对，再按需压缩。
                        # 畸形 tool_calls（id 重复/为空/数量不齐）若只依赖压缩路径，消息数未超阈值
                        # 时根本不会被修，API 会按 tool_calls 数量校验报 insufficient tool messages。
                        try:
                            await self._repair_checkpoint_tool_history(run_config, graph)
                        except Exception as exc:
                            logger.warning("[修复] 单轮内 tool 历史修复失败（已忽略，不影响主流程）: %s", exc)
                        try:
                            # 压缩耗时不可预知且期间零图事件 → 标「忙」，避免被外层空闲看门狗误杀
                            with OpBusy():
                                _report = await self._compact_checkpoint_if_needed(run_config, model_override=model_override, provider_override=provider_override)
                            if _report:
                                yield _sse({"type": "context_compacted", **_report})
                        except Exception as exc:
                            logger.warning("[压缩] 单轮内压缩失败（已忽略，不影响主流程）: %s", exc)
                    _input = event.get("data", {}).get("input", {})
                    run_id = event.get("run_id", "")[:12]
                    # LangChain 回调的 input 结构可能是多层的（list / dict / 嵌套 list），
                    # 统一交给 _normalize_messages 展平为真正的消息列表。
                    _msgs = _normalize_messages(_input)
                    if _msgs:
                        msg_count = len(_msgs)
                        # 记录最后一条 user 消息预览
                        last_msg = _msgs[-1] if _msgs else {}
                        last_content = str(getattr(last_msg, "content", ""))[:200]
                        logger.info(
                            "[LLM_START] run_id=%s msgs=%d last_msg=%s",
                            run_id, msg_count, last_content,
                        )
                        # 上下文画像：真实 token 规模默认就算（供 LLM_END 的 real= 对照网关虚高）；
                        # 详细 [CTX] 消息拆解/全文 dump 才由 AGENT_LOG_CONTEXT 控制。
                        real_tokens = _dump_context_profile(_msgs, run_id)
                        if real_tokens:
                            self._ctx_token_sizes[run_id] = real_tokens
                            if len(self._ctx_token_sizes) > 64:
                                self._ctx_token_sizes.pop(next(iter(self._ctx_token_sizes)), None)
                    else:
                        logger.info("[LLM_START] run_id=%s input=%s", run_id, str(_input)[:200])

                    # 前端显示"正在调用 AI..."
                    yield _sse({"type": "llm_thinking"})
                    last_model_activity_at = time.time()

                # ── LLM 流式正文 ──
                # 边流边发 + 轮末归位：
                #   工具块尚未出现时，正文乐观地作为 token 逐字流式发出（恢复"打字机"体验），同时累积到 thinking_buffer；
                #   若本轮随后出现工具调用（on_tool_start），说明这段正文其实是推理 → 作为 thought 块整块补发，
                #     前端据此清除误进"答案气泡"的临时内容；
                #   若本轮无工具调用（on_chat_model_end），这段正文即最终答案，已逐字流出，无需重复。
                # 一旦已确定进入工具轮（has_tool_chunks），正文不再逐字流出（避免无谓的清除闪烁），仅累积后由 thought 块展示。
                if kind == "on_chat_model_stream" and node == "agent":
                    chunk = event["data"]["chunk"]
                    has_content = bool(chunk.content)
                    has_tool_chunks = bool(getattr(chunk, "tool_call_chunks", None))

                    # ── 诊断：确认 chunk 中推理字段结构（新增特性上线确认）──
                    # 只在「有 content / 有推理 token」时打（空 chunk 已靠 end 汇总计数），
                    # 且每条流最多打 12 条，避免刷屏。
                    _stream_chunk_idx += 1
                    if (_stream_chunk_idx <= 2 or has_content or reasoning_delta) and _stream_chunk_idx <= 12:
                        ak_keys = list(getattr(chunk, "additional_kwargs", {}).keys())
                        logger.info(
                            "[推理诊断] chunk#%d content_has=%s reasoning_content_attr=%s ak_keys=%s",
                            _stream_chunk_idx, has_content,
                            bool(getattr(chunk, "reasoning_content", None)),
                            ak_keys,
                        )

                    # ── 推理模型的思考 token（reasoning_content / thinking）──
                    # 推理模型先把思考过程逐块流出，最后才输出正文。若不单独捕获，
                    # 思考阶段正文为空 → 前端长时间空白，且思考过程被静默丢弃。
                    # 这里实时转发给前端做「思考过程」实时展示，并独立累积（不污染最终答案）。
                    reasoning_delta = _extract_reasoning(chunk)
                    if reasoning_delta:
                        reasoning_buffer += reasoning_delta
                        yield _sse({"type": "reasoning", "content": reasoning_delta})
                    # fix #1: 真实 token / 推理会刷新硬超时计时钟；空 keepalive chunk 不刷新
                    if has_content or reasoning_delta:
                        llm_silent_since = time.time()
                        last_model_activity_at = time.time()

                    if has_tool_chunks:
                        # 已确定本轮为工具轮 → 正文归为推理，不逐字流出，仅累积（轮末作 thought 展示）
                        in_tool_call = True
                        if has_content:
                            thinking_buffer += chunk.content
                            last_model_activity_at = time.time()
                    elif has_content:
                        # 尚不知是否会有工具调用 → 乐观逐字流式发出，同时累积；
                        # 若本轮实为工具轮，前端会在 thought/tool_start 时清除这段临时答案。
                        thinking_buffer += chunk.content
                        yield _sse({"type": "token", "content": chunk.content})
                        last_model_activity_at = time.time()
                
                # ── 工具开始 ──
                elif kind == "on_tool_start":
                    step_count += 1
                    graph_steps += 1
                    tool_name = event.get("name", "")
                    run_id = event.get("run_id", "")
                    
                    # 子代理完成后如果父模型还在调工具，最多允许若干次不同的非子代理工具，之后强制汇总
                    # 用 run_id 去重，避免同一工具因网络重试被重复计数
                    # ponytail: 阈值 6 为经验值；更稳的做法是改为"空闲超时"（参考心跳里的 90s 计时），
                    # 但那样要跨事件维护父模型活动时钟，先保留计数上限以控制复杂度。
                    if self.config.enable_loop_guard and _subagent_dispatched and tool_name not in {"delegate_tasks_parallel", "delegate_task"} and run_id not in _post_subagent_seen_run_ids:
                        _post_subagent_seen_run_ids.add(run_id)
                        _post_subagent_tool_calls += 1
                        if _post_subagent_tool_calls >= 6:
                            logger.warning("[防循环] 子代理完成后父模型已调用 %d 次不同工具，强制汇总", _post_subagent_tool_calls)
                            loop_guard_triggered = True
                            # 基于已收集的子代理结果生成真实汇总（而非空壳占位）
                            final_buffer = await _synthesize_guard_summary(
                                self, run_config, _subagent_results, final_buffer
                            )
                            done_data = {"type": "done", "content": final_buffer}
                            if current_todo_list:
                                done_data["todo_list"] = current_todo_list
                            _done_yielded = True
                            yield _sse(done_data)
                            break

                    # 工具执行前按需压缩（在飞工具尾块受保护）。与 LLM 前压缩互补：
                    # 长工具链（连续多轮 read_file/run_shell 等）期间提前介入，
                    # 避免上下文滚雪球到下次 LLM 调用才压。失败不中断工具执行。
                    # compress_context 手动工具例外：压缩由工具自身在其执行中完成
                    # （结果文本即压缩报告），此处跳过避免同一轮压缩两次。
                    try:
                        if tool_name != "compress_context":
                            # 压缩耗时不可预知且期间零图事件 → 标「忙」，避免被外层空闲看门狗误杀
                            with OpBusy():
                                _report = await self._compact_checkpoint_before_tool(run_config, model_override=model_override, provider_override=provider_override)
                            if _report:
                                yield _sse({"type": "context_compacted", **_report})
                    except Exception as exc:
                        logger.warning("[压缩] 工具前压缩失败（已忽略，不影响工具执行）: %s", exc)

                    started_at = time.time()
                    last_model_activity_at = time.time()
                    running_tools[run_id] = {
                        "name": tool_name,
                        "step": step_count,
                        "started_at": started_at,
                    }
                    last_progress_at = started_at
                    
                    # 取出本轮缓冲的推理文本，作为 thought 块整块发出。
                    # 推理内容已不再进入 final_buffer，无需再做回退删除（claw-back）。
                    thought = thinking_buffer.strip()
                    thinking_buffer = ""  # 重置
                    if thought:
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

                    # ── 过程反思：每 8 次工具调用后快速自检是否偏航 ──
                    # 用审核模型（回退主模型）轻量判断，15s 超时失败静默，绝不阻塞主流程。
                    # 若判定偏航，把纠偏提示写入 checkpoint + 发 reflection 事件给前端。
                    # ponytail: 触发间隔从 5 提到 8，平衡纠偏价值与噪音（反思卡片过频）。
                    if len(tool_call_history) % 8 == 0 and len(tool_call_history) > 0:
                        try:
                            _advice = await self.quick_reflection(message, tool_call_history)
                            if _advice:
                                logger.info("[过程反思] 第 %d 次工具调用后触发纠偏: %s",
                                            len(tool_call_history), _advice)
                                await graph.aupdate_state(
                                    run_config,
                                    {"messages": [SystemMessage(content=f"[执行监督] {_advice}")]},
                                )
                                yield _sse({"type": "reflection", "content": _advice})
                        except Exception as _refl_exc:
                            logger.debug("[过程反思] 失败（已忽略）: %s", _refl_exc)

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
                        # 工具开始时间戳（毫秒）：随 SSE 事件一并收集进历史，
                        # 历史回放时前端据此可展示真实耗时而非瞬时差值 0
                        "ts": int(time.time() * 1000),
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
                    # 兜底补发 run_shell 最后一块实时输出：心跳 drain 粒度 2s，工具结束前
                    # 的残余（shell_tools 的 finally 已不再清空）在此 drain 发出，避免丢失；
                    # 顺序在 tool_result 之前，前端先追加实时输出、再展示完整结果。
                    if tool_name == "run_shell":
                        try:
                            _tail_chunk = drain_shell_output()
                        except Exception:
                            _tail_chunk = ""
                        if _tail_chunk:
                            yield _sse({
                                "type": "tool_output",
                                "step": step_for_tool,
                                "content": _tail_chunk,
                            })
                    is_error = bool(output_str.strip().startswith("❌"))
                    self._record_tool_call(tool_name, thread_id=tid)

                    # 插件事件钩子：on_tool_end（工具调用结束后广播）。
                    # 单个插件监听器异常已被 PluginHost.emit 隔离，绝不影响主流程。
                    # 广播后把插件经 host.push_sse 排队的前端事件随之 yield 成 SSE 帧（E 注入点）。
                    try:
                        from plugin_loader import get_registry as _get_plugin_reg
                        _plug = _get_plugin_reg()
                        _plug.emit("on_tool_end", {
                            "name": tool_name,
                            "args": (tinfo or {}).get("input", {}),
                            "result": output_str,
                            "is_error": is_error,
                            "user_id": self._user_id,
                            "session_id": tid,
                        })
                        for _ev_name, _ev_payload in _plug.drain_sse(tid):
                            yield _sse({"type": "plugin_event", "event": _ev_name, "payload": _ev_payload})
                    except Exception:
                        pass

                    # ── Todo 清单事件：manage_todo 工具调用结束后推送 ──
                    if tool_name == "manage_todo":
                        from tools.todo_tools import peek_todo_list
                        # 用 peek（只查内存不读盘）+ 完整 thread_key：本轮刚 create/update
                        # 必然在缓存里；无参/读盘会取到其它会话或上一轮残留的清单
                        # （见浏览器页面释放处的同款教训）。
                        todo_data = peek_todo_list(thread_key)
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
                        # 工具实际耗时（毫秒）：随 SSE 事件收集进历史，
                        # 历史回放直接展示真实耗时；实时流前端也优先用它
                        "duration_ms": int((time.time() - tinfo["started_at"]) * 1000) if tinfo else 0,
                    })

                    # ── 工具调用结束日志 ──
                    elapsed = time.time() - tinfo["started_at"] if tinfo else 0
                    result_preview = _truncate(output_str.strip(), 200)
                    logger.info(
                        "[TOOL_END] tool=%s step=%d run_id=%s duration=%.1fs error=%s result=%s",
                        tool_name, step_for_tool, run_id[:12], elapsed, is_error, result_preview,
                    )

                    # ── 工具诊断日志：集中记录错误/超长调用，供后续优化参考 ──
                    # 只埋流式路径（实际使用路径）；非流式 graph.ainvoke 拿不到耗时/错误信息。
                    try:
                        from monitoring.tool_diagnostics import log_tool_event
                        log_tool_event(
                            tool_name, elapsed, is_error,
                            args=tinfo.get("input") if tinfo else None,
                            result_preview=result_preview,
                            session=tid,
                        )
                    except Exception:
                        pass  # 诊断日志失败不应影响主流程


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
                                # 打包子代理内部工具事件 + 文本日志：随 subagent_end 进主流事件流，
                                # 历史保存（collected_steps）时才有数据，回放才能重建工具卡片与 💭 思考。
                                try:
                                    from subagents import manager as _subagent_manager
                                    upd["tools"] = _subagent_manager.get_capsule_tool_events(cap["id"])
                                    upd["logs"] = _subagent_manager.get_capsule_logs(cap["id"])
                                except Exception:
                                    pass
                                updated.append(upd)
                            _subagent_results = updated
                            yield _sse({
                                "type": "subagent_end",
                                "capsules": updated,
                            })
                        except Exception:
                            # 简化降级：只标记状态（同样带上工具事件和日志，保证历史回放不丢）
                            _subagent_results = [dict(cap, status="done") for cap in subagent_capsules]
                            try:
                                from subagents import manager as _subagent_manager
                                for _cap in _subagent_results:
                                    _cap["tools"] = _subagent_manager.get_capsule_tool_events(_cap["id"])
                                    _cap["logs"] = _subagent_manager.get_capsule_logs(_cap["id"])
                            except Exception:
                                pass
                            yield _sse({
                                "type": "subagent_end",
                                "capsules": _subagent_results,
                            })
                        subagent_capsules = []
                        logger.info("[子代理] subagent_end 已发送，等待父模型生成汇总回复...")
                        subagent_end_sent_at = time.time()
                        # 子代理日志/工具事件已随 subagent_end 打包完毕，此刻才安全清理 batch。
                        # （工具函数内不能清——on_tool_end 发生在工具返回之后，提前清会让历史回放丢日志）
                        try:
                            from subagents import manager as _subagent_manager
                            _subagent_manager.clear_batch()
                        except Exception:
                            pass
                    
                    # 重置推理上下文（但保留其他并行工具的进度状态）
                    in_tool_call = False
                    if not running_tools:
                        # 没有剩余工具时，也重置最后进度时间，避免 stale 进度事件
                        last_progress_at = 0.0

                    loop_reason = _detect_tool_loop(tool_call_history, run_config["recursion_limit"], current_steps=graph_steps)
                    if self.config.enable_loop_guard and loop_reason:
                        loop_guard_triggered = True
                        _done_yielded = True
                        # 基于已收集结果生成真实汇总（而非空壳占位提示）
                        final_buffer = await _synthesize_guard_summary(
                            self, run_config, _subagent_results, final_buffer
                        )
                        yield _sse({"type": "done", "content": final_buffer})
                        break
                elif kind == "on_chat_model_end" and node == "agent":
                    # fix #1: 调用正常结束 → 清除在飞标记，硬超时计时钟失效
                    llm_call_in_flight = False
                    output = event.get("data", {}).get("output")
                    input_tok, output_tok, cached_tok = _extract_usage_tokens(output)
                    run_id = event.get("run_id", "")[:12]

                    # 记录 LLM 响应摘要
                    output_content = str(getattr(output, "content", ""))[:200] if output else ""
                    has_tool_calls = bool(getattr(output, "tool_calls", None)) if output else False
                    finish_reason = getattr(output, "response_metadata", {}).get("finish_reason", "") if output else ""
                    # ponytail: 主模型输出被 max_tokens 截断（finish_reason=length）且无工具调用时，
                    # 会被当成最终回答发出半截内容；此处仅标记，流结束处追加提示（自动续写需图层面支持）。
                    if finish_reason == "length" and not has_tool_calls:
                        logger.warning("[LLM_END] 模型输出被截断 (finish_reason=length)，最终回答可能不完整")
                        truncated_final = True

                    # ── 诊断汇总：本次 LLM 调用是否下发推理 token（定性此模型是否为推理模型）──
                    _total_chunks = _stream_chunk_idx
                    _has_reasoning = bool(reasoning_buffer)
                    logger.info(
                        "[推理汇总] 本次调用 chunk 总数=%d 推理 token 长度=%d 是否为推理模型=%s",
                        _total_chunks, len(reasoning_buffer), _has_reasoning,
                    )
                    _stream_chunk_idx = 0  # 重置，供下一轮 LLM 调用重新计数
                    real_ctx = self._ctx_token_sizes.pop(run_id, 0)
                    logger.info(
                        "[LLM_END] run_id=%s tokens=(in=%d out=%d cached=%d real=%d) finish=%s has_tool_calls=%s content=%s",
                        run_id, input_tok, output_tok, cached_tok, real_ctx,
                        finish_reason, has_tool_calls, output_content,
                    )

                    # 前端提示"AI 已响应"
                    yield _sse({"type": "llm_response", "has_tool_calls": has_tool_calls})

                    # ── 本轮无工具调用 → 缓冲区里的正文即最终答案 ──
                    # 有工具调用时不在这里处理：那段正文是推理，交由随后的 on_tool_start 作为 thought 块发出。
                    # 正文已在 on_chat_model_stream 逐字流式发出，这里只补进 final_buffer 供 done 校正，不重复 yield；
                    # 若网关未逐块下发正文（thinking_buffer 为空），回退到 output.content 整块补发，避免最终答案丢失。
                    if not has_tool_calls:
                        # 剥离内联 <think>...</think> 思考块：部分网关把推理混进正文，
                        # 若不清理会漏进最终答案。done 携带的是权威内容，前端据此校正，
                        # 因此即便流式阶段短暂闪过 think 标签，最终答案也会是干净的。
                        final_text = _strip_think_tags(thinking_buffer)
                        thinking_buffer = ""
                        if final_text:
                            # 已逐字流出，仅补进 final_buffer（done 事件据此校正为权威内容）
                            final_buffer += final_text
                        elif output is not None:
                            fallback_text = _strip_think_tags(_message_text(getattr(output, "content", "")) or "")
                            if fallback_text:
                                final_buffer += fallback_text
                                yield _sse({"type": "token", "content": fallback_text})
                                last_model_activity_at = time.time()

                    if input_tok > 0 or output_tok > 0:
                        real_out = estimate_message_tokens(output) if output else 0
                        self._record_model_usage(
                            input_tok, output_tok, cached_tok,
                            source="chat_model_end", thread_id=tid,
                            real_output_hint=real_out,
                        )
                        usage_recorded = True

            # 流结束，发送正常完成事件（含最终回复内容）
            if not _done_yielded:
                if truncated_final:
                    final_buffer = final_buffer + "\n\n⚠️ 以上回答可能因模型输出长度限制被截断，你可以说「继续」让我把剩余部分补完。"
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
                logger.warning("[stream_run] 工具循环到达上限，尽力汇总后结束任务（不再抛错误中断）")
                notice = _recursion_limit_message(run_config["recursion_limit"])
                try:
                    summary = await _synthesize_guard_summary(
                        self, run_config, _subagent_results, final_buffer
                    )
                except Exception:
                    summary = final_buffer
                final_buffer = f"{summary}\n\n---\n{notice}"
                _done_yielded = True
                yield _sse({"type": "done", "content": final_buffer})
            else:
                logger.error("[stream_run] 异常: %s", e, exc_info=True)
                # ponytail: API 拒绝图片输入 → 自动把当前模型标记为"实际不支持视觉"，
                # 下次调用即走视觉路由。仅 in-memory，重启后清空。
                if attachments and is_image_input_error(e):
                    # ponytail: 必须用 _resolve_provider_config 解析"本次实际使用的模型"，
                    # 不能直接取 self.config.model——切换厂商后实际模型是该厂商自己的 model
                    # （如 step-router-v1），取错会把视觉模型冤枉成"不支持图片"。
                    pid, mdl, _key, _url, _is_anth = self._resolve_provider_config(
                        model_override, provider_override
                    )
                    record_model_image_unsupported(pid, mdl)
                    final_buffer = (
                        f"❌ 当前模型 {mdl} 不支持图片输入，已自动标记为非视觉模型。"
                        f"\n👉 请重试本条消息：图片会先发给你标记过的视觉模型生成描述，"
                        f"再由 {mdl} 基于描述继续工作。"
                        f"\n💡 永久修复：在「设置 → 模型」取消 {mdl} 的 👁 视觉标记，避免误判。"
                    )
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
            
            # 清理未完成的子代理胶囊
            if subagent_capsules:
                yield _sse({
                    "type": "subagent_end",
                    "capsules": [dict(cap, status="error") for cap in subagent_capsules],
                })
                subagent_capsules = []
        
        finally:
            # 复位本请求的 LLM 重试通知队列（避免 ContextVar 泄漏到其它请求）
            try:
                _retry_notifications_ctx.reset(_retry_notif_token)
            except Exception:
                pass
            logger.info(
                "[stream_run] 结束: tid=%s, thread_key=%s, tool_steps=%d, cancelled=%s, loop_guard=%s, final_len=%d, usage_recorded=%s",
                tid, thread_key, step_count, cancelled, loop_guard_triggered,
                len(final_buffer), usage_recorded,
            )
            self._hydrated_threads.add(thread_key)
            # 始终清理 checkpoint 中的图片/截图引用，避免跨请求残留
            try:
                await self._strip_checkpoint_images(run_config, graph)
            except Exception:
                pass
            # 清理 todo 清单缓存（按完整 thread_key 清理，保留磁盘文件供恢复）。
            # 必须用 thread_key 而非裸 tid：工具按 LangGraph config 的完整 key
            # （"uid:sessionId"）写入，用 tid 会 key 不匹配导致旧清单永久残留。
            try:
                from tools.todo_tools import pop_todo_list
                pop_todo_list(thread_key)
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
                    self._tracker.record_model_call(
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
        cancelled = False
        try:
            async for sse in self.stream_run(*args, **kwargs):
                yield sse
                if '"type": "done"' in sse or '"type": "error"' in sse:
                    done_yielded = True
        except GeneratorExit:
            # aclose() 被调用，stream_run 内部已清理。GeneratorExit 后不能 yield
            cancelled = True
            return
        
        if not done_yielded and not cancelled:
            yield _sse({"type": "done", "content": ""})
            yield "data: [DONE]\n\n"


    def switch_thread(self, thread_id: str):
        self._thread_id = thread_id
