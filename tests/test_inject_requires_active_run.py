"""step 注入必须要求「有活跃 run」——否则消息会滞留 next_step 桶。

背景（线上问题）：点「授权写入」后前端自动补一条「继续」并调用 send()，
命中 send() 的 streaming 入队分支 → 提示「已加入队列」而不是立即继续。
修复后授权走 inject(mode=step) 立即注入；为了避免「无活跃 run 时注入」
把消息滞留在桶里、等下一轮开始时被意外重复注入，inject 接口需直接拒绝。

本测试守护两点：
1. 无活跃 run 时 step 注入返回 ok=False，桶保持为空；
2. 有活跃 run 时 step 注入正常写入。
"""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent_core"))

from inbox import get_inbox_manager  # noqa: E402


def _fake_request():
    return SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))


@pytest.fixture()
def inject_endpoint(monkeypatch):
    """导入 inject_session_message，并把会话/用户解析替换成固定值。"""
    from api.routes import agent as agent_routes

    monkeypatch.setattr(agent_routes, "_resolve_user", lambda request: "u1")
    monkeypatch.setattr(
        agent_routes.session_store,
        "get_session",
        lambda uid, sid: {"id": sid, "uid": uid},
    )
    return agent_routes.inject_session_message


def _drain(uid, sid):
    inbox = get_inbox_manager().get(uid, sid)
    return inbox.claim_next_step(), inbox.claim_next_turn()


def test_step_injection_rejected_without_active_run(inject_endpoint):
    uid, sid = "u1", "sess-no-run"
    mgr = get_inbox_manager()
    mgr.mark_run_active(uid, sid, False)

    req = SimpleNamespace(content="继续", mode="step")
    res = asyncio.run(inject_endpoint(sid, req, _fake_request()))

    assert res["ok"] is False
    assert res["active"] is False
    assert res["reason"] == "no_active_run"
    step, turn = _drain(uid, sid)
    assert step == [], "无活跃 run 时不得写入 next_step 桶（否则会滞留并在下一轮被重复注入）"
    assert turn == []


def test_step_injection_accepted_with_active_run(inject_endpoint):
    uid, sid = "u1", "sess-active"
    mgr = get_inbox_manager()
    mgr.mark_run_active(uid, sid, True)

    req = SimpleNamespace(content="继续", mode="step")
    res = asyncio.run(inject_endpoint(sid, req, _fake_request()))

    assert res["ok"] is True
    assert res["active"] is True
    assert res["queued"] == "step"
    step, _ = _drain(uid, sid)
    assert [m["content"] for m in step] == ["继续"]


def test_turn_injection_still_allowed_without_active_run(inject_endpoint):
    """mode=turn 是「排到下一轮」，无活跃 run 时也应受理（不受本次收紧影响）。"""
    uid, sid = "u1", "sess-turn"
    get_inbox_manager().mark_run_active(uid, sid, False)

    req = SimpleNamespace(content="稍后处理", mode="turn")
    res = asyncio.run(inject_endpoint(sid, req, _fake_request()))

    assert res["ok"] is True
    assert res["queued"] == "turn"
    _, turn = _drain(uid, sid)
    assert [m["content"] for m in turn] == ["稍后处理"]
