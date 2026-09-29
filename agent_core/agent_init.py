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
from agent_helpers import _make_inbox_pre_hook  # import * 不导入下划线名

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
        self._agents_md_path = ""  # 当前缓存的 AGENTS.md 路径（工作区切换时失效）


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


    def _resolve_provider_config(self, model_override: str = "", provider_override: str = ""):
        """根据可选的 provider 覆盖解析本次 LLM 调用的有效参数。

        不修改 self.config（active_provider 保持不变），仅用于本次请求临时选用指定 provider。
        返回 (provider_id, model, api_key, base_url, is_anthropic)。
        """
        pid = provider_override or self.config.active_provider
        prov = (self.config.providers or {}).get(pid, {})
        is_anthropic = (pid == "anthropic")
        model = model_override or prov.get("model") or self.config.model
        api_key = prov.get("api_key") or self.config.api_key or "sk-no-key-required"
        base_url = prov.get("base_url") or ""
        return pid, model, api_key, base_url, is_anthropic

    def _build_llm(self, model_override: str = "", provider_override: str = ""):
        pid, model, api_key, base_url, is_anthropic = self._resolve_provider_config(
            model_override, provider_override
        )
        kwargs = {
            "model": model,
            "api_key": api_key,
            "temperature": 0,
            "max_retries": self.config.api_max_retries,
            "timeout": self.config.api_timeout_seconds,
        }
        if base_url:
            kwargs["base_url"] = base_url
            host = urlparse(base_url).hostname
            if host:
                configure_host_resolution(host, self.config.api_host_ips)
        if is_anthropic:
            return ChatAnthropic(
                model=model,
                api_key=api_key,
                base_url=base_url or None,
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


    def _create_graph(self, model_override: str = "", provider_override: str = ""):
        llm = self._build_llm(model_override, provider_override)
        # 包裹「首 token 空闲看门狗 + 仅重发 LLM 调用」的健壮层：
        # 模型卡住时快速失败并重试，不动已执行的工具，也不重跑整轮。
        llm = RetryableLLM(
            llm,
            idle_timeout=self.config.llm_idle_timeout_seconds,
            max_idle_retries=self.config.llm_idle_max_retries,
            on_retry=_on_llm_idle_retry,
            # 限流(429)：不立即抛，固定等待后重试本次 LLM 调用（通过 on_retry → SSE 告知前端）
            rate_limit_wait=getattr(self.config, "llm_rate_limit_wait_seconds", 30.0),
            max_rate_limit_retries=getattr(self.config, "llm_rate_limit_max_retries", 3),
            # 上下文超长(context-overflow)：运行层注入压缩 handler 后，压缩 checkpoint 再重试
            max_overflow_retries=getattr(self.config, "llm_context_overflow_retries", 1),
        )
        # —— 实时干预（next_step 桶）注入钩子：每次进 LLM 节点前取走待办消息 ——
        # create_react_agent 原生 pre_model_hook 在每个「LLM 调用边界」前触发，正好满足
        # 「只在 LLM 调用边界注入、不打断正在执行的工具」的语义。返回 messages 追加进 agent 输入，
        # 仅当本轮有待注入消息时才活动（claim 后为空即原样返回，无额外内容、零行为变化）。
        pre_hook = _make_inbox_pre_hook(self._user_id)
        return create_react_agent(
            llm,
            self.tools,
            prompt=self._build_system_prompt(),
            checkpointer=self.memory,
            pre_model_hook=pre_hook,
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
            + "\n"
            + "## 推理策略\n"
            + "- 复杂任务先分解为可验证的子任务，再列出执行计划（用 manage_todo 工具维护）。\n"
            + "- 每完成一个子任务，检查结果是否符合预期，不符合就先修正再继续。\n"
            + "- 如果连续 2 次工具调用未取得实质进展，停下来重新评估策略，不要盲目重试。\n"
            + "- 优先利用「从过往任务中学到的经验」避免已知陷阱，不要重复踩坑。\n"
            + "\n## 长任务异步处理（重要：不要干等 run_shell）\n"
            + "- run_shell 阈值：命令预计耗时 < 30s 走同步立即返回；>= 30s 启动后台任务立即返回 task_id。\n"
            + "- 拿到 task_id 后用以下工具主动跟进，**绝对不要傻等**同步返回：\n"
            + "  - get_async_task(task_id)：查状态（status / elapsed / output_chars）\n"
            + "  - wait_async_task(task_id, timeout=10)：等待完成（短轮询，最多 60s）\n"
            + "  - cancel_async_task(task_id)：终止（卡死时立刻调用）\n"
            + "  - list_async_tasks()：盘点本会话所有后台任务（检查遗漏）\n"
            + "- 命令卡死超过预期：先 cancel，再决定重跑或换方案。\n"
            + "- 可以在 wait 期间做其他事（继续分析已得输出、准备下一步计划等），不要阻塞主流程。\n"
            + "\n## 用户决策征询（ask_user）——仅在必须由用户决定时才使用\n"
            + "- 当任务推进被一个**必须由用户拍板**的决策阻塞，且不征求用户就无法可靠继续时，调用 ask_user 向前端弹出问卷征询用户，拿到答复后再继续。\n"
            + "- **不要滥用**：只在用户拥有你缺失的判断依据/偏好/授权，或选择会显著影响用户利益时使用。能用合理默认值自行推进的，**不要**打扰用户。\n"
            + "- 典型适用：在多个互斥方案里选一个、确认涉及用户资源/账户/对外影响的操作、需要用户补充关键参数或授权。\n"
            + "- 用好参数：prompt 写清楚问题与取舍；options 给 2~6 个简短互斥选项（必要时 1 个默认项）；拿不准用户会怎么答时开启 allow_free_text 让其自由输入。\n"
            + "- ask_user 是阻塞式（会暂停当前任务等待用户），确认必须征询再调用，避免无关紧要的打断。\n"
        )

        # ── 注入当前工作区目录下的 AGENTS.md（仅当工作区恰为项目仓库/目录时注入，带缓存）──
        # 原实现固定注入本仓库根的 AGENTS.md，导致普通会话（如微信日常问答）也背上
        # ponytail 开发规范（~2.8KB）。改为按当前工作区查找：只有工作区目录下存在
        # AGENTS.md（即用户正在操作该仓库）才注入，其余会话不再携带，节省每轮 token。
        try:
            agents_md_path = Path(ws) / "AGENTS.md"
            if agents_md_path.exists() and agents_md_path.is_file():
                key = str(agents_md_path)
                mtime = agents_md_path.stat().st_mtime
                if (self._agents_md_path == key
                        and self._agents_md_cache
                        and self._agents_md_mtime == mtime):
                    agents_content = self._agents_md_cache
                else:
                    agents_content = agents_md_path.read_text(encoding="utf-8").strip()
                    if agents_content:
                        self._agents_md_cache = agents_content
                        self._agents_md_mtime = mtime
                        self._agents_md_path = key
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

        # ── Case → Skill 蒸馏的可复用工作流注入（需求4）──
        # 仅注入达到晋升阈值（多次成功）的 Case，作为「已验证可行路径」提示，
        # 让 agent 遇到同类任务时优先采用，而非从头摸索。同样有数量/长度上限。
        case_lines = []
        try:
            from case_forge import find_promotable_cases
            for c in find_promotable_cases(self._user_id):
                val = c.get("value") or {}
                v = str(val.get("v", "")).strip()
                actions = [str(a) for a in (val.get("actions") or []) if a]
                if not v:
                    continue
                if actions:
                    case_lines.append(f"- {v}（已验证 {val.get('occurrences', 0)} 次，路径: {' → '.join(actions)}）")
                else:
                    case_lines.append(f"- {v}（已验证 {val.get('occurrences', 0)} 次）")
        except Exception:
            pass  # Case 注入失败不影响基础经验注入
        case_lines = _shorten(case_lines, MAX_LEARNED)

        sections = []
        if learned:
            sections.append("## 从过往任务中学到的经验\n" + "\n".join(learned))
        if avoid:
            sections.append("## 历史踩坑与用户纠正（务必避免）\n" + "\n".join(avoid))
        if case_lines:
            sections.append("## 已验证的可复用工作流（Case 蒸馏）\n"
                            "遇到相同类型任务时，可直接复用以下已验证成功的处理路径：\n"
                            + "\n".join(case_lines))
        return "\n\n".join(sections)
