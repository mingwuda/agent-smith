"""心跳空闲超时测试：工具执行期间（is_busy=True）不触发 _timeout。

背景（ponytail）：run_shell(600s) 等长跑工具执行期间，LangGraph 不产生任何
astream_events 事件（on_tool_start 与 on_tool_end 之间静默），原实现会在
llm_timeout（默认 90s）后误杀工具。修复：_stream_events_with_heartbeat 增加
is_busy 回调，工具执行中跳过空闲超时（时长由工具自身 timeout 控制）。

只覆盖不依赖真实 LLM/LangGraph 的心跳超时逻辑。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_core"))

from agent_run import AgentRunMixin  # noqa: E402


class FakeGraph:
    """astream_events：产出 2 个事件（LLM 开始 + 工具开始）后静默，模拟工具执行中。"""

    async def astream_events(self, input_data, run_config, version="v2"):
        yield {"event": "on_chat_model_start", "metadata": {}, "data": {}}
        yield {"event": "on_tool_start", "metadata": {}, "name": "run_shell", "run_id": "r1", "data": {}}
        # 工具执行中：不产出任何事件
        while True:
            await asyncio.sleep(10)


async def _collect(mixin, graph, timeout, is_busy=None, stop_at_heartbeats=None):
    """收集事件，直到结束或心跳数达到 stop_at_heartbeats（用于验证持续心跳）"""
    events = []
    hb_count = 0
    async for ev in mixin._stream_events_with_heartbeat(
        graph, {}, {}, heartbeat_interval=0.05, timeout=timeout, is_busy=is_busy
    ):
        events.append(ev)
        if "_heartbeat" in ev:
            hb_count += 1
            if stop_at_heartbeats is not None and hb_count >= stop_at_heartbeats:
                break
    return events


def test_tool_running_skips_timeout():
    """工具执行中（is_busy=True）：超过 timeout 仍持续心跳，不触发 _timeout。"""
    busy = {"v": True}
    events = asyncio.run(_collect(
        AgentRunMixin(), FakeGraph(), timeout=0.15,
        is_busy=lambda: busy["v"], stop_at_heartbeats=4,
    ))
    # 0.2s 的 4 次心跳 > 0.15s 超时阈值，若未跳过超时早已产出 _timeout
    assert not any("_timeout" in e for e in events), f"工具执行中不应超时: {events}"
    assert sum("_heartbeat" in e for e in events) >= 4


def test_no_tool_triggers_timeout():
    """无工具（is_busy=None）：超时后产出 _timeout 并结束。"""
    events = asyncio.run(_collect(AgentRunMixin(), FakeGraph(), timeout=0.15))
    assert events and events[-1].get("_timeout"), f"无工具时应超时: {events}"


def test_tool_end_resumes_timeout():
    """工具结束后 is_busy 变 False：超时检查恢复，产出 _timeout。"""
    busy = {"v": True}
    hb_count = {"n": 0}

    async def run():
        mixin = AgentRunMixin()
        events = []
        async for ev in mixin._stream_events_with_heartbeat(
            FakeGraph(), {}, {}, heartbeat_interval=0.05, timeout=0.15,
            is_busy=lambda: busy["v"],
        ):
            events.append(ev)
            if "_heartbeat" in ev:
                hb_count["n"] += 1
                if hb_count["n"] >= 2:
                    busy["v"] = False  # 模拟工具结束
        return events

    events = asyncio.run(run())
    assert events and events[-1].get("_timeout"), f"工具结束后应恢复超时: {events}"
