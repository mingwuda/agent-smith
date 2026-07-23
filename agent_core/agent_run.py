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
from agent_helpers import *  # noqa: F401,F403
# import * 不导入下划线名;以下显式注入本文件实际引用的私有符号(共19个)
from agent_helpers import (
    _SCENE_PROMPTS, _connection_diagnostic, _detect_scene,
    _drop_dangling_tool_call_messages, _dump_context_profile,
    _extract_steps_from_messages, _extract_usage_tokens,
    _human_content, _is_recursion_limit_error, _extract_reasoning, _message_text,
    _normalize_messages, _recursion_limit_message, _retry_notifications_ctx,
    _sse, _strip_image_content_from_messages, _synthesize_guard_summary,
    _tool_signature, _truncate,
)
from loop_guard import _detect_tool_loop  # 原版 agent.py:169 的文件中间导入,拆分时需显式补回

logger = get_logger(__name__)


class AgentRunMixin:
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
        # 硬上限：消息数超过 50 时强制压缩，避免长会话无限制膨胀
        if not should_compact(messages, self.config.model, self.config.context_window_tokens):
            if len(messages) > 50:
                logger.info("[压缩] 消息数=%d 超过硬上限 50，强制压缩", len(messages))
            else:
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


    def _run_config(self, thread_key: str = "") -> dict:
        if not thread_key:
            thread_key = self._thread_key()
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


    def _get_graph(self, model_override: str = ""):
        """获取当前可用的编译图，统一处理两种取图场景。

        - model_override 给定时总是重新编译（用于按请求切换模型），不缓存。
        - 否则若缓存的 self._graph 为 None（例如 set_workspace 使其失效），
          则惰性重建并缓存，避免重复编译。
        """
        if model_override:
            return self._create_graph(model_override)
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
    ) -> tuple[str, list[dict]]:
        """处理用户消息，返回 (最终回复, 中间步骤列表)

        参数:
          thread_id: 当前会话 ID，取代全局 self._thread_id（支持并发）
        """
        tid = thread_id or self._thread_id
        config = self._run_config(tid)
        # 取可用 graph：model_override 时重建，缺失时惰性重建（见 _get_graph）
        graph = self._get_graph(model_override)
        # 为本请求建立独立的 LLM 重试通知队列（非流式调用也会走 RetryableLLM）
        _retry_notif_token = _retry_notifications_ctx.set([])
        input_messages = []
        thread_key = self._thread_key(tid)
        await self._repair_checkpoint_tool_history(config, graph)
        await self._strip_checkpoint_images(config, graph)
        if thread_key not in self._hydrated_threads:
            input_messages = compact_history_messages(session_messages_to_langchain(history or []), self.config)
        current_content = _human_content(message, attachments)
        input_messages.append(HumanMessage(content=current_content))

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
                        self._tracker.record_model_call(
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
    ) -> AsyncGenerator[dict, None]:
        """流式获取 LangGraph 事件，并定期产生心跳事件。

        心跳事件格式为 {"_heartbeat": True}。此实现用独立的心跳任务替代
        asyncio.shield，避免底层事件任务异常未被消费而触发 asyncio 的
        "exception in shielded future" 告警。

        如果 timeout 秒内无任何事件（LLM/工具卡死），产生 {"_timeout": True} 事件后结束，
        消费端应据此返回超时错误，避免无限挂起。
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
                    last_event_at = time.time()
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
        # 取可用 graph：model_override 时重建，缺失时惰性重建（见 _get_graph）
        graph = self._get_graph(model_override)
        # 为本请求建立独立的 LLM 重试通知队列（并发安全：每个会话各自隔离）
        _retry_notif_list: list = []
        _retry_notif_token = _retry_notifications_ctx.set(_retry_notif_list)
        input_messages = []
        thread_key = self._thread_key(tid)
        await self._repair_checkpoint_tool_history(run_config, graph)
        await self._strip_checkpoint_images(run_config, graph)
        if thread_key not in self._hydrated_threads:
            input_messages = compact_history_messages(session_messages_to_langchain(history or []), self.config)
        input_messages.append(HumanMessage(content=_human_content(message, attachments)))

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
        step_count = 0
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
                "[stream_run] 开始: tid=%s, thread_key=%s, model=%s, timeout=%s, message_len=%d",
                tid, thread_key,
                model_override or self.config.model,
                self.config.api_timeout_seconds,
                len(message),
            )
            await self._compact_checkpoint_if_needed(run_config)
            # 从配置读取超时，默认 90 秒
            llm_timeout = getattr(self.config, "llm_timeout_seconds", 90)
            # fix #1: 单次 LLM 调用的硬墙钟上限（秒）。上游挂起（连接开着但无首 token/无结束）时，
            # 即便心跳与 RetryableLLM 重试不断刷新现有计时器，此墙钟也会强制终止该轮。
            llm_hard_timeout = getattr(self.config, "llm_hard_timeout_seconds", 180.0)
            async for event in self._stream_events_with_heartbeat(
                graph, input_data, run_config, timeout=llm_timeout
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
                # 把 RetryableLLM 上报的「空闲超时重试」事件转成 SSE，提示前端正在重试
                if _retry_notif_list:
                    for _note in _retry_notif_list:
                        yield _sse({
                            "type": "llm_retry",
                            "attempt": _note["attempt"],
                            "reason": _note["reason"],
                            "max": self.config.llm_idle_max_retries,
                        })
                    _retry_notif_list.clear()
                if event.get("_stream_event_error"):
                    yield _sse({
                        "type": "tool_result",
                        "tool": "_stream_events",
                        "step": step_count,
                        "result": f"❌ 工具事件流异常: {event['_stream_event_error']}",
                        "result_full": "",
                        "error": True,
                        "diff": None,
                        "diff_file_path": "",
                    })
                    continue
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
                    if running_tools:
                        last_progress_at = now
                    else:
                        # 无运行中工具时仍发送 ping 事件，避免连接因空闲断开
                        yield _sse({"type": "ping"})
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

                kind = event["event"]
                node = event.get("metadata", {}).get("langgraph_node", "")
                logger.debug("[stream_run] 事件: kind=%s node=%s", kind, node)
                
                # ── LLM 调用开始（日志 + 前端状态提示）──
                if kind == "on_chat_model_start" and node == "agent":
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
                        try:
                            await self._compact_checkpoint_if_needed(run_config)
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
                            _subagent_results = updated
                            yield _sse({
                                "type": "subagent_end",
                                "capsules": updated,
                            })
                        except Exception:
                            # 简化降级：只标记状态
                            _subagent_results = [dict(cap, status="done") for cap in subagent_capsules]
                            yield _sse({
                                "type": "subagent_end",
                                "capsules": _subagent_results,
                            })
                        subagent_capsules = []
                        logger.info("[子代理] subagent_end 已发送，等待父模型生成汇总回复...")
                        subagent_end_sent_at = time.time()
                    
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
                    if finish_reason == "length" and not tool_calls:
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
                        final_text = thinking_buffer
                        thinking_buffer = ""
                        if final_text:
                            # 已逐字流出，仅补进 final_buffer（done 事件据此校正为权威内容）
                            final_buffer += final_text
                        elif output is not None:
                            fallback_text = _message_text(getattr(output, "content", "")) or ""
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
