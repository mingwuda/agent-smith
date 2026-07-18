"""回归测试：流式回复的保存逻辑不能丢失 bot 轮次。

复现 bug：当 Agent 转发了一个「正文为空」的 terminal 事件（如到达最大推理步数、
最终缓冲区为空）且已收集到 steps 时，原逻辑会跳过保存，导致历史里只剩用户提问，
step 信息与最终输出全部丢失。

本测试把一个桩 _stream_done_wrapper 打到真实 Agent 实例上（路由就使用该实例），
产出 `tool_start` + 空 `done`，断言这一轮 bot 消息仍被落库（带 steps 与占位正文），
而不是被静默丢弃。

运行：python -m pytest tests/test_stream_save_no_drop.py -q
"""
import sys
import time
import types

import pytest

from fastapi.testclient import TestClient

from agent_core.main import app
from agent_core import session_store
from agent_core.api.deps import _auth_config, _sign_session


def _fake_stream():
    async def gen():
        yield 'data: {"type": "tool_start", "tool": "read_file", "args": {"file_path": "/tmp/x"}, "step": 1}\n\n'
        yield 'data: {"type": "done", "content": ""}\n\n'
        yield "data: [DONE]\n\n"
    return gen()


class _FakeAgent:
    def __init__(self):
        self.config = types.SimpleNamespace(recursion_limit=60)
        self.calls = 0

    def set_user(self, uid):
        pass

    def set_workspace(self, path):
        pass

    def _stream_done_wrapper(self, *args, **kwargs):
        self.calls += 1
        return _fake_stream()


# 路由模块在运行期以 `api.routes.agent` 名字加载（见 agent_core/main.py:442），
# 且其 init_agent 会从顶层 `main` 模块读取 agent（agent.py:68-70）。
# 这两个都可能以独立模块对象存在，逐一打桩；只要其中任一个被路由真正使用即可。
_ROUTE_MODULES = ("api.routes.agent", "agent_core.api.routes.agent")


def _patch_module(monkeypatch, mod, fake):
    monkeypatch.setattr(mod, "init_agent", lambda: None, raising=False)
    monkeypatch.setattr(mod, "agent", fake, raising=False)


@pytest.fixture
def client_and_fake(monkeypatch):
    with TestClient(app) as c:
        fake = _FakeAgent()
        patched = []
        for name in _ROUTE_MODULES:
            mod = sys.modules.get(name)
            if mod is not None:
                _patch_module(monkeypatch, mod, fake)
                patched.append(name)
        # 顶层 main 模块（agent.py 的 init_agent 实际读取它）。可能尚未加载，主动导入。
        try:
            import main as _main_mod
        except Exception:
            _main_mod = None
        if _main_mod is not None:
            _patch_module(monkeypatch, _main_mod, fake)
            patched.append("main")
        # agent_core.main 别名，避免任何残留别名
        _patch_module(monkeypatch, sys.modules["agent_core.main"], fake)
        patched.append("agent_core.main")
        # 让 fake 可被断言是否真的被调用（防止打错模块却假绿）
        yield c, fake


def _auth_cookie() -> str:
    cfg = _auth_config()
    exp = int(time.time()) + 3600
    return _sign_session("admin", exp)


def test_empty_terminal_event_still_saves_bot_message(client_and_fake):
    client, fake = client_and_fake
    uid = "admin"
    sid = session_store.create_session(uid, title="保存回归测试")["id"]
    cookie = {"desktop_agent_session": _auth_cookie()}

    # 消费完整 SSE 流，确保落库逻辑执行
    with client.stream(
        "POST",
        "/run/stream",
        json={"message": "帮我读一下 /tmp/x", "thread_id": sid},
        cookies=cookie,
    ) as r:
        assert r.status_code == 200
        for _ in r.iter_lines():
            pass

    # 桩必须真的被路由调用，否则说明打桩模块不对、测试在假绿
    assert fake.calls >= 1, f"桩 Agent 未被调用（打桩模块可能不对）: patched={getattr(client, 'patched', None)}"

    sess = session_store.get_session(uid, sid)
    msgs = sess["messages"]
    roles = [m["role"] for m in msgs]
    assert "user" in roles, f"用户消息应存在: {roles}"
    # 关键断言：bot 消息必须被保存，不能因为正文为空而丢失
    assert "assistant" in roles, f"空正文 terminal 事件不应导致 bot 消息丢失: {roles}"

    bot = next(m for m in msgs if m["role"] == "assistant")
    # 已收集的步骤必须保留（否则展开历史看不到工作过程）
    assert "steps" in bot and bot["steps"], f"bot 消息应保留 steps: {bot}"
    # 占位正文应非空，至少让用户知道这一轮发生了什么
    assert bot.get("content", "").strip(), f"bot 消息应有占位正文: {bot}"
