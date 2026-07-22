"""DesktopAgent 混入：chat_sync / 反思 / 技能 / 用量统计。"""
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
from agent_helpers import _truncate_args  # import * 不导入下划线名

logger = get_logger(__name__)


class AgentChatMixin:
    async def chat_sync(self, message: str, attachments: Optional[list[dict]] = None, thread_id: str = "") -> str:
        """同步聊天：运行 agent 并收集完整的流式回复文本。

        适用于非浏览器场景（如微信、API 调用）需要一次性获取完整回复。

        thread_id: 显式指定会话线程，避免并发时多个调用方互相覆盖共享的
        self._thread_id（曾导致微信多用户并发串会话）。不传则回退到默认线程。
        """
        full = ""
        async for sse_line in self._stream_done_wrapper(message, attachments=attachments, thread_id=thread_id):
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
        outcome: str = "success",
        feedback: Optional[str] = None,
    ) -> Optional[dict]:
        """任务完成后反思，总结可复用模式 / 用户偏好 / 踩坑。返回 {t, v} 或 None。

        t ∈ {technique, preference, pitfall}：
        - technique：可复用的工作流/模式（成功且值得记）
        - preference：用户明确表达的个人偏好
        - pitfall：踩过的坑 / 不要再做的事
        """
        # 成功路径保持原有行为：只对涉及工具调用的任务反思
        tool_steps = [s for s in steps if s.get("type") == "tool_start"]
        if not tool_steps and outcome == "success":
            return None

        tool_summary = "\n".join(
            f"- {s.get('tool', '?')}({_truncate_args(s.get('args', {}))})"
            for s in tool_steps
        )

        # 失败 / 用户反馈：走根因 / 纠正分支
        if outcome == "error" or feedback:
            if outcome == "error":
                instruction = (
                    "这个任务执行失败了。请分析根因，总结一条「不要再这样做」的踩坑经验。\n"
                    "回复格式：一句话（20 字以内），说明「不要 X」或「应改 Y」。\n"
                    "若无法总结出有用教训，回复：无需记录"
                )
            else:
                instruction = (
                    "用户对刚才的结果给出了反馈/纠正。请归纳其中反映的用户偏好或可复用纠正。\n"
                    "若属于个人偏好，回复格式：偏好|一句话\n"
                    "若属于「不要再这样做」的纠正，回复格式：不要|一句话\n"
                    "若只是随意评价无明确偏好，回复：无需记录"
                )
            context = (
                f"## 用户需求\n{user_message[:300]}\n\n"
                f"## 工具调用过程\n{tool_summary or '（无工具调用）'}\n\n"
                f"## 最终结果\n{final_result[:500]}\n\n"
                f"## 用户反馈\n{(feedback or '')[:500]}\n\n"
                f"{instruction}"
            )
        else:
            context = (
                "你是一个 AI 助手，刚刚完成了一个多步骤任务。请回顾执行过程，总结可复用的经验。\n\n"
                f"## 用户需求\n{user_message[:300]}\n\n"
                f"## 工具调用过程\n{tool_summary}\n\n"
                f"## 最终结果\n{final_result[:500]}\n\n"
                "请用 20 字以内总结这个任务中是否有可复用的模式、工作流或经验教训。\n"
                "- 如果有用且可复用的模式，回复格式：关键词|一句话总结\n"
                "  例如：zip分析|用户上传zip后先解压再逐文件分析\n"
                "- 如果只是普通的问答或一次性工具调用，回复：无需记录"
            )

        # 优先用审核模型（与主模型解耦、控成本）；未配置则回退主模型
        llm = self._build_review_llm() or self._build_llm()
        llm.request_timeout = 15  # 短超时，绝不阻塞主流程
        try:
            resp = await llm.ainvoke([HumanMessage(content=context)])
            text = str(resp.content).strip()
            if not text or "无需记录" in text:
                return None
            return self._classify_reflection(text, outcome=outcome, feedback=feedback)
        except Exception:
            return None


    def _classify_reflection(self, text: str, outcome: str, feedback: Optional[str]) -> dict:
        """把反思文本归类成结构化 {t, v}（纯函数，便于单测）。"""
        # ponytail: 反馈分支用显式前缀（偏好| / 不要|）区分类型；其余按 outcome 兜底。
        if feedback:
            if text.startswith("不要"):
                v = text.split("|", 1)[-1].strip() or text
                return {"t": "pitfall", "v": v}
            if text.startswith("偏好"):
                v = text.split("|", 1)[-1].strip() or text
                return {"t": "preference", "v": v}
            return {"t": "preference", "v": text}
        if outcome == "error":
            return {"t": "pitfall", "v": text}
        return {"t": "technique", "v": text}


    def maybe_generate_skill(self, pattern: dict) -> Optional[str]:
        # ponytail: P3 占位——高价值 technique 起草 SKILL.md 并 reload_skills()。
        # 当前不激活：需 enable_self_evolution + 审批闸 + 沙箱验证才允许写技能。
        return None


    def _approval_gate(self, candidate: Any) -> bool:
        # ponytail: P3/P4 占位——人工审批/沙箱校验，当前恒 False（不激活）。
        return False


    def _record_model_usage(self, input_tokens: int, output_tokens: int, cached_tokens: int = 0, source: str = "llm", thread_id: str = "", real_output_hint: int = 0):
        if input_tokens <= 0 and output_tokens <= 0:
            return
        # ponytail: 上游网关偶发把 session 累计 token 当作单次 input_tokens 上报（虚高），
        # 真实单次调用不可能在数秒内处理数百万 token。这类读数直接把 input 归零，
        # 不计入用量统计，避免面板被假数字撑高。
        if input_tokens > 500_000:
            logger.warning(
                "[usage] 单次 input_tokens=%d 异常偏高（疑似网关累计值虚高，已将该次 input 归零、不计入用量统计）",
                input_tokens,
            )
            input_tokens = 0
        # output_tokens 同样会被网关虚高（如 435 字回复报 out=30525）。这里用本地基于
        # 实际输出内容估算的 real_output_hint 做基准：当上报值远高于本地估算（3 倍且超 2000）
        # 时，判定为网关虚高，改用本地估算值，保证用量统计中的输出 token 贴近真实。
        if real_output_hint > 0 and output_tokens > max(real_output_hint * 3, 2000):
            logger.warning(
                "[usage] 单次 output_tokens=%d 异常偏高（远超本地估算 %d，疑似网关虚高，已按本地估算修正）",
                output_tokens, real_output_hint,
            )
            output_tokens = real_output_hint
        self._tracker.record_model_call(
            provider=self.config.active_provider,
            model=self.config.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_tokens,
            thread_id=thread_id or self._thread_id,
            source=source,
        )


    def _record_tool_call(self, tool_name: str, thread_id: str = ""):
        self._tracker.record_tool_call(
            tool_name=tool_name,
            provider=self.config.active_provider,
            model=self.config.model,
            thread_id=thread_id or self._thread_id,
        )


    def reload_skills(self):
        """热加载技能 -> 重建 system prompt"""
        count = self.registry.reload()
        self._rebuild_graph()
        return count
