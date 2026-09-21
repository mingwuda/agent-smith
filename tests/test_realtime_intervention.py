"""实时干预（Inbox 双桶 + pre_model_hook 注入）的守护测试。

参考 dsh 的 Inbox next-step/next-turn 双桶模型：
  - next_step：注入「当前正在跑的回合」下一步（steering），在每次 LLM 调用边界被 claim。
  - next_turn：排到「当前回合结束后」处理（入列下轮）。

覆盖：
  1. Inbox 双桶语义：append/claim FIFO、step 与 turn 桶隔离、max_items 限制。
  2. pre_hook 无待注入 → 返回 None（图行为零变化）。
  3. pre_hook 有待注入 → 追加 HumanMessage 到 messages，并 record（供 stream 层转发 SSE）。
  4. pre_hook 不匹配 user → no-op 且不消费待办。
  5. 真实 create_react_agent(pre_model_hook) 中，注入消息进入 LLM 输入。
  6. mark_run_active + hint restore。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent_core"))

from langchain_core.messages import HumanMessage  # noqa: E402

import inbox as Inbox  # noqa: E402
from agent_helpers import _make_inbox_pre_hook  # noqa: E402


def test_inbox_double_bucket_semantics():
    m = Inbox.get_inbox_manager()
    m.get("u", "s1").claim_next_step(999)
    m.get("u", "s1").claim_next_turn(999)

    m.get("u", "s1").append("step", "steer-1")
    m.get("u", "s1").append("step", "steer-2")
    m.get("u", "s1").append("turn", "queued-1")

    # step 桶 FIFO 取出，与 turn 桶隔离
    claims = m.get("u", "s1").claim_next_step(8)
    assert [c["content"] for c in claims] == ["steer-1", "steer-2"]
    assert m.get("u", "s1").turn_counts() == {"step": 0, "turn": 1}

    # turn 桶保留（本轮结束才消费）
    turn = m.get("u", "s1").claim_next_turn(8)
    assert [c["content"] for c in turn] == ["queued-1"]
    assert m.get("u", "s1").turn_counts() == {"step": 0, "turn": 0}


def test_inbox_fifo_and_limit():
    m = Inbox.get_inbox_manager()
    m.get("u", "s2").claim_next_step(999)
    for i in range(10):
        m.get("u", "s2").append("step", f"m{i}")
    batch = m.get("u", "s2").claim_next_step(3)
    assert [c["content"] for c in batch] == ["m0", "m1", "m2"]
    rest = m.get("u", "s2").claim_next_step(999)
    assert len(rest) == 7


def test_pre_hook_no_pending_returns_none():
    hook = _make_inbox_pre_hook("u")
    m = Inbox.get_inbox_manager()
    state = {"messages": [HumanMessage(content="hi")]}
    result = hook(state, config={"configurable": {"thread_id": "u:empty"}})
    assert result is None


def test_pre_hook_injects_human_messages():
    hook = _make_inbox_pre_hook("u")
    m = Inbox.get_inbox_manager()
    m.get("u", "s4").claim_next_step(999)
    m.get("u", "s4").append("step", "请改用 JSON 输出")

    state = {"messages": [HumanMessage(content="原始任务")]}
    result = hook(state, config={"configurable": {"thread_id": "u:s4"}})
    assert result is not None
    msgs = result["messages"]
    contents = [getattr(x, "content", "") for x in msgs]
    assert contents == ["原始任务", "请改用 JSON 输出"]
    # claim 后被清空
    assert m.get("u", "s4").claim_next_step(8) == []
    # 注入被记录（供 stream 层转发 SSE）
    evs = m.drain_injected("u", "s4")
    assert [e["content"] for e in evs] == ["请改用 JSON 输出"]


def test_pre_hook_wrong_user_noop():
    hook = _make_inbox_pre_hook("alice")
    m = Inbox.get_inbox_manager()
    m.get("alice", "s5").append("step", "给 alice 的指令")
    # 用 bob 身份调 hook（thread_id 前缀不匹配）→ 不注入，且不消费
    state = {"messages": [HumanMessage(content="x")]}
    result = hook(state, config={"configurable": {"thread_id": "bob:s5"}})
    assert result is None
    assert len(m.get("alice", "s5").peek_step()) == 1


def test_mark_active_and_persist(tmp_path):
    m = Inbox.get_inbox_manager()
    m.configure(tmp_path)
    m.get("u", "s6").claim_next_step(999)
    assert m.get("u", "s6").turn_counts() == {"step": 0, "turn": 0}
    m.get("u", "s6").append("step", "中断内容")
    m.mark_run_active("u", "s6", True)
    assert m.get("u", "s6").active is True
    m.persist("u", "s6")
    m.mark_run_active("u", "s6", False)

    # 模拟重启恢复：新 manager 从磁盘 restore
    m2 = Inbox.InboxManager()
    m2.configure(tmp_path)
    m2.restore("u", "s6")
    assert m2.get("u", "s6").peek_step()[0]["content"] == "中断内容"


async def test_pre_model_hook_in_real_graph():
    """端到端：pre_model_hook 挂进真实 create_react_agent，注入消息进入 LLM 输入。"""
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import Runnable
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.prebuilt import create_react_agent

    hook = _make_inbox_pre_hook("u")
    m = Inbox.get_inbox_manager()

    seen_inject = []

    class FakeLLM(Runnable):
        # 静态 Runnable 模型：ainvoke 捕获输入并返回最终 AI 消息（无工具 → 一轮结束）。
        model_keys: tuple = ("name",)
        def bind_tools(self, tools):
            return self
        def invoke(self, input, config=None):
            return self._respond(input)
        async def ainvoke(self, input, config=None):
            return self._respond(input)
        def _respond(self, input):
            msgs = input["messages"] if isinstance(input, dict) and "messages" in input else input
            joined = "\n".join(str(getattr(x, "content", "")) for x in msgs)
            if "请改用 JSON" in joined:
                seen_inject.append(True)
            return AIMessage(content="最终回答")

    graph = create_react_agent(
        FakeLLM(),
        [],
        prompt="sys",
        checkpointer=MemorySaver(),
        pre_model_hook=hook,
    )

    # 预置一条待注入消息（thread_key 与 run 的 config 对齐：u:graph-run）
    m.get("u", "graph-run").claim_next_step(999)
    m.get("u", "graph-run").append("step", "途中请改用 JSON")
    await graph.ainvoke(
        {"messages": [HumanMessage(content="开始")]},
        {"configurable": {"thread_id": "u:graph-run"}, "recursion_limit": 10},
    )

    # 注入内容确实进入过 LLM 输入
    assert seen_inject, "注入消息未进入 LLM 输入"
    # pre_hook 注入后被 claim 清空，且 record 出事件（供 stream 层转发 SSE）
    assert m.get("u", "graph-run").claim_next_step(8) == [], "注入后 step 桶应被清空"
    evs = m.drain_injected("u", "graph-run")
    assert [e["content"] for e in evs] == ["途中请改用 JSON"]