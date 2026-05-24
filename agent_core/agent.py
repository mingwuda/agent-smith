"""桌面 AI 智能体核心"""
import json
from typing import AsyncGenerator, Optional

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
    
    async def stream_run(self, message: str) -> AsyncGenerator[str, None]:
        """流式处理用户消息，yield SSE 格式事件"""
        run_config = {"configurable": {"thread_id": self._thread_id}}
        input_data = {"messages": [("human", message)]}
        
        # 记录输入 token
        self.tracker.record(
            model=self.config.model, input_tokens=len(message),
            output_tokens=0, tool_name="user_input",
        )
        
        thinking_buffer = ""        # 累积推理文本（工具调用前的内容）
        final_buffer = ""           # 最终回复缓存
        step_count = 0
        in_tool_call = False        # 当前是否正在产生工具调用
        
        try:
            async for event in self._graph.astream_events(
                input_data, run_config, version="v2",
            ):
                kind = event["event"]
                node = event.get("metadata", {}).get("langgraph_node", "")
                
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
                        yield f"data: {json.dumps({'type': 'token', 'content': chunk.content}, ensure_ascii=False)}\n\n"
                    elif has_content and in_tool_call:
                        # 工具调用后还在输出文本 → 这是下一轮思考或最终回复
                        # 这里不会走到，因为 tool start/end 会重置状态
                        pass
                
                # ── 工具开始 ──
                elif kind == "on_tool_start":
                    step_count += 1
                    tool_name = event.get("name", "")
                    
                    # 取出推理文本
                    thought = thinking_buffer.strip()
                    thinking_buffer = ""  # 重置
                    
                    # 从 previous tokens 中移除推理文本（它们不是最终回复）
                    if thought:
                        # 从 final_buffer 中去掉这部分
                        if final_buffer.endswith(thought):
                            final_buffer = final_buffer[:-len(thought)]
                        # 发送 thought 事件
                        yield f"data: {json.dumps({
                            'type': 'thought',
                            'thought': thought,
                            'step': step_count,
                        }, ensure_ascii=False)}\n\n"
                    
                    # 工具参数
                    inp = event.get("data", {}).get("input", {})
                    if isinstance(inp, dict):
                        args_preview = {k: str(v)[:80] for k, v in inp.items() if not k.startswith("_")}
                    else:
                        args_preview = {"input": str(inp)[:80]}
                    
                    yield f"data: {json.dumps({
                        'type': 'tool_start',
                        'tool': tool_name,
                        'args': args_preview,
                        'step': step_count,
                    }, ensure_ascii=False)}\n\n"
                
                # ── 工具结束 ──
                elif kind == "on_tool_end":
                    output = event.get("data", {}).get("output", "")
                    output_str = str(output)[:500]
                    tool_name = event.get("name", "")
                    
                    self.tracker.record(
                        model=self.config.model, input_tokens=0,
                        output_tokens=len(output_str), tool_name=tool_name,
                    )
                    
                    yield f"data: {json.dumps({
                        'type': 'tool_result',
                        'tool': tool_name,
                        'result': _truncate(output_str, 400),
                    }, ensure_ascii=False)}\n\n"
                    
                    # 重置状态，准备接收下一轮推理
                    in_tool_call = False
        
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': f'{type(e).__name__}: {e}'}, ensure_ascii=False)}\n\n"
        
        finally:
            # 发送完成事件（thinking_buffer 中剩余的是最终回复）
            remaining = thinking_buffer.strip()
            if remaining:
                final_buffer = remaining
            yield f"data: {json.dumps({'type': 'done', 'content': final_buffer}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
    
    def switch_thread(self, thread_id: str):
        self._thread_id = thread_id
    
    def reload_skills(self):
        """热加载技能 -> 重建 system prompt"""
        count = self.registry.reload()
        self._rebuild_graph()
        return count
