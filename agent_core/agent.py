"""桌面 AI 智能体核心"""
from typing import Optional

from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver

from config import AgentConfig
from monitoring.usage_tracker import get_tracker, UsageTracker
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
        self.tracker: UsageTracker = get_tracker()
        self.registry: SkillRegistry = get_registry()
        self.tools: list = []  # 由外部设置
        self._thread_id = "default"
        self._graph = None
    
    def set_tools(self, tools: list):
        self.tools = tools
        self._rebuild_graph()
    
    def _build_llm(self):
        kwargs = {
            "model": self.config.model,
            "api_key": self.config.api_key,
            "temperature": 0,
        }
        if self.config.base_url:
            kwargs["base_url"] = self.config.base_url
        return ChatOpenAI(**kwargs)
    
    def _build_system_prompt(self) -> str:
        prompt = self.config.system_prompt
        skill_block = self.registry.generate_prompt_block()
        if skill_block:
            prompt += skill_block
        return prompt
    
    def _rebuild_graph(self):
        self._graph = create_react_agent(
            self.llm,
            self.tools,
            prompt=self._build_system_prompt(),
            checkpointer=self.memory,
        )
    
    async def run(self, message: str) -> tuple[str, list[dict]]:
        """处理用户消息，返回 (最终回复, 中间步骤列表)"""
        config = {"configurable": {"thread_id": self._thread_id}}
        
        try:
            result = await self._graph.ainvoke(
                {"messages": [("human", message)]},
                config,
            )
            messages = result["messages"]
            
            # 提取中间步骤
            steps = _extract_steps_from_messages(messages)
            
            # 提取 AI 的最后一条消息作为最终回复
            final_content = "（Agent 未产生输出）"
            for msg in reversed(messages):
                if hasattr(msg, "content") and msg.type == "ai" and msg.content:
                    # 记录 token 用量
                    try:
                        usage = getattr(msg, "usage_metadata", None) or {}
                        if hasattr(usage, "input_tokens"):
                            input_tok = usage.input_tokens
                            output_tok = usage.output_tokens
                        else:
                            input_tok = usage.get("input_tokens", 0) if isinstance(usage, dict) else 0
                            output_tok = usage.get("output_tokens", 0) if isinstance(usage, dict) else 0
                    except Exception:
                        input_tok = 0
                        output_tok = len(msg.content)
                    
                    self.tracker.record(
                        model=self.config.model,
                        input_tokens=input_tok or len(message),
                        output_tokens=output_tok,
                        tool_name="agent_response",
                    )
                    final_content = msg.content
                    break
            
            return final_content, steps
        except Exception as e:
            return f"❌ 执行出错: {type(e).__name__}: {e}", []
    
    def switch_thread(self, thread_id: str):
        self._thread_id = thread_id
    
    def reload_skills(self):
        """热加载技能 -> 重建 system prompt"""
        count = self.registry.reload()
        self._rebuild_graph()
        return count
