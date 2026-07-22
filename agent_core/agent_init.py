"""DesktopAgent 混入：构造 / 配置 / 模型与图构建。"""
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
from agent_helpers import _on_llm_idle_retry  # import * 不导入下划线名

logger = get_logger(__name__)


class AgentInitMixin:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.llm = self._build_llm()
        self.review_llm = self._build_review_llm()  # 审核模型（可选）
        self.memory = MemorySaver()
        self._user_id = "default"
        self._tracker: UsageTracker = get_tracker(self._user_id)
        self.registry: SkillRegistry = get_registry()
        self.tools: list = []  # 由外部设置
        self._thread_id = "default"
        self._graph = None
        self._current_workspace = ""  # 当前会话/项目实际工作目录，用于动态修正系统提示
        self._hydrated_threads: set[str] = set()
        self._ctx_token_sizes: dict[str, int] = {}  # run_id(12位) -> 真实上下文 token 估算，用于 LLM_END 对比网关虚高
        self._agents_md_cache = ""
        self._agents_md_mtime = 0.0


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
        # 同步 shell 工具的用户上下文（用于高危命令确认按用户隔离）
        try:
            from tools.shell_tools import set_current_user as _set_st_user
            _set_st_user(user_id)
        except Exception:
            pass


    def set_workspace(self, ws: str):
        """设置当前会话/项目的实际工作目录。

        当工作目录发生变化时，使缓存的 graph（含系统提示）失效，
        下次 run 会重建并注入正确的工作区路径，避免 LLM 始终按
        硬编码的 ~/agent_workspace 去找目录。
        """
        ws = str(ws or "").strip()
        if ws and ws != self._current_workspace:
            self._current_workspace = ws
            self._graph = None
        elif not ws and self._current_workspace:
            # 回落到默认配置工作区
            self._current_workspace = ""
            self._graph = None


    def user_id(self) -> str:
        return self._user_id


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
        if self.config.active_provider == "anthropic":
            return ChatAnthropic(
                model=kwargs["model"],
                api_key=self.config.api_key,
                base_url=self.config.base_url or None,
                temperature=kwargs["temperature"],
                max_retries=kwargs["max_retries"],
                timeout=kwargs["timeout"],
            )
        return ChatOpenAI(**kwargs)


    def _build_review_llm(self):
        """构建审核模型 LLM 实例（从 review_provider_id 配置）。"""
        pid = (self.config.review_provider_id or "").strip()
        if not pid or pid not in self.config.providers:
            return None
        prov = self.config.providers[pid]
        model = (self.config.review_model or "").strip() or prov.get("model", "")
        api_key = prov.get("api_key", "") or ""
        base_url = prov.get("base_url", "") or ""
        if not model:
            return None
        if pid == "anthropic":
            return ChatAnthropic(
                model=model,
                api_key=api_key,
                base_url=base_url or None,
                temperature=0,
                max_retries=self.config.api_max_retries,
                timeout=self.config.api_timeout_seconds,
            )
        return ChatOpenAI(
            model=model,
            api_key=api_key or "sk-no-key-required",
            base_url=base_url or None,
            temperature=0,
            max_retries=self.config.api_max_retries,
            timeout=self.config.api_timeout_seconds,
        )


    def _create_graph(self, model_override: str = ""):
        llm = self._build_llm(model_override)
        # 包裹「首 token 空闲看门狗 + 仅重发 LLM 调用」的健壮层：
        # 模型卡住时快速失败并重试，不动已执行的工具，也不重跑整轮。
        llm = RetryableLLM(
            llm,
            idle_timeout=self.config.llm_idle_timeout_seconds,
            max_idle_retries=self.config.llm_idle_max_retries,
            on_retry=_on_llm_idle_retry,
        )
        return create_react_agent(
            llm,
            self.tools,
            prompt=self._build_system_prompt(),
            checkpointer=self.memory,
        )


    def _build_system_prompt(self) -> str:
        now = datetime.now().astimezone()
        # 动态修正系统提示中的工作区路径：用当前实际工作目录替换硬编码的
        # ~/agent_workspace（否则 LLM 始终认为工作区在固定目录，可能跑错目录）
        ws = self._current_workspace or self.config.workspace
        prompt_base = self.config.system_prompt.replace(
            str(Path.home() / "agent_workspace"), ws
        )
        prompt = (
            prompt_base
            + "\n\n"
            + "## 当前日期与时间\n"
            + f"- 当前日期：{now.date().isoformat()}\n"
            + f"- 当前时间：{now.strftime('%Y-%m-%d %H:%M:%S %Z%z')}\n"
            + "- 遇到\u201c今天/昨日/今年/最新/current/latest/recent\u201d等相对时间时，必须以这里的日期为准。\n"
        )

        # ── 注入项目根目录的 AGENTS.md（如果存在，带缓存）──
        try:
            agents_md_path = Path(__file__).resolve().parent.parent / "AGENTS.md"
            if agents_md_path.exists():
                mtime = agents_md_path.stat().st_mtime
                if self._agents_md_cache and self._agents_md_mtime == mtime:
                    agents_content = self._agents_md_cache
                else:
                    agents_content = agents_md_path.read_text(encoding="utf-8").strip()
                    if agents_content:
                        self._agents_md_cache = agents_content
                        self._agents_md_mtime = mtime
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
                prompt += "\n\n" + patterns
        except Exception:
            pass

        return prompt


    def _load_learned_patterns(self) -> str:
        """从长期记忆中读取经验模式，用于注入系统提示。
        自动遗忘超过 10 天的旧经验，避免记忆膨胀。

        支持两种存储格式（向后兼容）：
        - 旧：_learned_<hash> = "关键词|一句话"（纯字符串）
        - 新：_learned_<hash> = {"t": "technique"|"preference", "v": "..."}
              _avoid_<hash>  = {"t": "pitfall", "v": "不要 X"}
        """
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
        avoid = []
        deleted_count = 0

        def extract_value(val) -> str:
            if isinstance(val, dict):
                return str(val.get("v", "")).strip()
            if isinstance(val, str):
                return val.strip()
            return ""

        for entry in items:
            key = entry.get("key", "")
            val = entry.get("value", "")
            if not (key.startswith("_learned_") or key.startswith("_avoid_")):
                continue
            if not isinstance(val, (str, dict)):
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

            text = extract_value(val)
            if not text:
                continue
            if key.startswith("_avoid_"):
                avoid.append(f"- 不要 {text}")
            else:
                learned.append(f"- {text}")

        if deleted_count:
            logger.info("[记忆] 自动清理了 %d 条过期学习经验", deleted_count)

        # ── 限制 learnings 数量与长度，避免 system prompt 膨胀 ──
        MAX_LEARNED = 3
        MAX_AVOID = 3
        MAX_ITEM_CHARS = 100

        def _shorten(items: list[str], limit: int) -> list[str]:
            out = []
            for text in items:
                if len(text) > MAX_ITEM_CHARS:
                    text = text[:MAX_ITEM_CHARS] + "..."
                out.append(text)
                if len(out) >= limit:
                    break
            return out

        learned = _shorten(learned, MAX_LEARNED)
        avoid = _shorten(avoid, MAX_AVOID)

        sections = []
        if learned:
            sections.append("## 从过往任务中学到的经验\n" + "\n".join(learned))
        if avoid:
            sections.append("## 历史踩坑与用户纠正（务必避免）\n" + "\n".join(avoid))
        return "\n\n".join(sections)
