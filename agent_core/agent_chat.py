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


    async def chat_stream_events(self, message: str, attachments: Optional[list[dict]] = None, thread_id: str = "", history: Optional[list[dict]] = None) -> AsyncGenerator[dict, None]:
        """流式运行 agent，逐条产出精简事件（thought/tool_start/tool_result/done/error）。

        供非流式渠道（如微信）边执行边分段回复：思考与每步工具执行即时可见，
        无需等 agent 全部处理完才拿到第一条反馈。done/error 事件携带最终回复。

        history: 会话历史（session_store messages 格式），透传给 stream_run。
        非流式渠道（微信）的 checkpoint 是内存态、重启即丢，必须显式传历史恢复上下文。
        """
        async for sse_line in self._stream_done_wrapper(message, attachments=attachments, thread_id=thread_id, history=history):
            line = sse_line.strip()
            if line.startswith("data: ") and not line.startswith("data: [DONE]"):
                try:
                    data = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if data.get("type") in ("thought", "tool_start", "tool_result", "reflection", "done", "error"):
                    yield data


    async def quick_reflection(
        self,
        user_message: str,
        tool_call_history: list[dict],
    ) -> Optional[str]:
        """过程反思：任务执行中途快速自检是否偏航，返回纠偏提示（或 None=无需干预）。

        - 与 reflect_on_task（任务结束后总结经验）互补，这里只关注「现在是否走偏了」。
        - 用审核模型（可选回退主模型）做轻量判断，15s 超时，失败静默返回 None，
          绝不阻塞主流程。
        - 返回文本会作为 SystemMessage 注入 checkpoint，引导模型在下一轮 LLM 调用
          时重新评估策略（ReAct 图会自然读取到该消息）。
        """
        # 无工具调用历史（首轮或纯问答）不反思
        if not tool_call_history:
            return None
        tool_summary = "\n".join(
            f"- {c.get('tool', '?')}({_truncate_args(c.get('args', {}))})"
            for c in tool_call_history[-10:]  # 只看最近 10 步，控制 prompt 长度
        )
        context = (
            "你是一个 AI 助手的执行监督者。该助手正在执行多步任务，请检查它是否偏航。\n\n"
            f"## 用户需求\n{user_message[:300]}\n\n"
            f"## 最近工具调用\n{tool_summary}\n\n"
            "## 判断准则（严格遵守）\n"
            "- 仅在以下情况纠偏：明显重复同一工具相同参数（绕圈）、多次失败重试同一路径、"
            "工具调用明显与用户需求无关、已偏离用户原始目标。\n"
            "- 仅给出「下一步建议」但当前流程仍正常推进 → 不算偏航。\n"
            "- 仅给出「可优化项」但当前任务未受影响 → 不算偏航。\n\n"
            "## 回复格式\n"
            "- 若一切正常，请**只**回复一个字：「正常」。不要补充说明、不要给下一步建议、不要列举可优化点。\n"
            "- 若确实偏航/停滞，用一句话（30 字以内）指出问题。\n"
        )
        # 优先用审核模型（与主模型解耦、控成本）；未配置则回退主模型
        llm = self._build_review_llm() or self._build_llm()
        llm.request_timeout = 15  # 短超时，绝不阻塞主流程
        try:
            resp = await llm.ainvoke([HumanMessage(content=context)])
            text = str(resp.content).strip()
            if not text:
                return None
            # 抑制条件（ponytail：原只看前 10 字符，误判多）：
            # 1) 标准"正常"回复（去标点后等于"正常"）
            # 2) 开头即"进展正常/整体正常/基本正常/暂时正常/暂无偏航/无需干预"等正向短语
            # 注：不开头"正常"是因为"正常情况下..."/"正常来说..."等句首修饰词会误伤（改为靠 rstrip 兜底纯"正常"回复）
            if text.rstrip("。.,， ") == "正常":
                return None
            if any(text.startswith(kw) for kw in (
                "进展正常", "整体正常", "基本正常", "暂时正常",
                "暂无偏航", "暂无问题", "无需干预", "无需纠偏",
            )):
                return None
            return text
        except Exception:
            return None


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


    def maybe_generate_skill(self, uid: str, pattern: dict,
                             user_message: str = "",
                             tool_steps: Optional[list] = None) -> Optional[str]:
        """Case → Skill 蒸馏入口（半自动）：把多次成功的同类 technique 累积为 Case，
        达到阈值后起草候选 SKILL.md 到待审批目录，待用户确认后生效。

        仅在 enable_self_evolution 开启时激活；否则只做无副作用的行为记录。

        pattern: reflect_on_task 产出的反思 dict {t, v}（仅 technique 参与蒸馏）。
        uid / user_message / tool_steps 一律由调用方**显式传入**：
        反思跑在 asyncio.create_task 的后台任务里，而 self._user_id / self._last_* 是
        全局单例 agent 上的可变字段，会被并发请求的 set_user() 覆盖 →
        曾导致 Case 与 _skill_ 指针写进**别的用户**的记忆（2026-09-29 修复）。
        """
        # 仅 technique 类型的经验才值得蒸馏成技能；preference/pitfall 走原负向/偏好路径
        if not pattern or pattern.get("t") != "technique":
            return None
        # 自进化开关关闭时不自动起草技能（保持默认保守行为）
        if not getattr(self.config, "enable_self_evolution", False):
            self._case_buffer = getattr(self, "_case_buffer", [])
            return None
        if not uid:
            return None
        try:
            from case_forge import accumulate_case, find_promotable_cases, draft_skill
            # 采集当前会话工具轨迹（由调用方随请求一起传入，不读单例可变字段）
            actions = tool_steps or []
            accumulate_case(uid, user_message or "", pattern, actions=actions)
            # 达到阈值 → 起草候选技能（半自动：写 pending，不直接生效）
            # 用 config 的唯一解析入口：空 skills_dir 会正确回退到内置 samples，
            # 不会像 `Path("")` 那样变成进程 CWD（见 AgentConfig.skills_root）。
            skills_dir = self.config.skills_root()
            promotable = find_promotable_cases(uid)
            for case in promotable:
                draft_skill(uid, case["key"], skills_dir)
            return "case-accumulated"
        except Exception as exc:
            logger.warning("[蒸馏] Case 累积/起草失败（已忽略）: %s", exc)
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
