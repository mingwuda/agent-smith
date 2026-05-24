"""桌面 AI 智能体核心"""
from typing import Optional

from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver

from config import AgentConfig
from monitoring.usage_tracker import get_tracker, UsageTracker
from skills.registry import get_registry, SkillRegistry


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
    
    async def run(self, message: str) -> str:
        """处理用户消息，返回结果"""
        config = {"configurable": {"thread_id": self._thread_id}}
        
        try:
            result = await self._graph.ainvoke(
                {"messages": [("human", message)]},
                config,
            )
            # 提取 AI 的最后一条消息
            for msg in reversed(result["messages"]):
                if hasattr(msg, "content") and msg.type == "ai" and msg.content:
                    # 尝试获取 token 用量
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
                    return msg.content
            
            return "（Agent 未产生输出）"
        except Exception as e:
            return f"❌ 执行出错: {type(e).__name__}: {e}"
    
    def switch_thread(self, thread_id: str):
        self._thread_id = thread_id
    
    def reload_skills(self):
        """热加载技能 -> 重建 system prompt"""
        count = self.registry.reload()
        self._rebuild_graph()
        return count
