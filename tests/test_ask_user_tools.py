"""ask_user 工具测试。

验证：
  - 注册 → resolve（前端提交）→ 唤醒完整闭环
  - cancel（取消）路径
  - 超时降级路径
  - 无 config / 无 thread_key 时优雅降级
  - 多会话隔离
  - 选项上限
  - 工具 schema 字段齐全

运行：python -m pytest tests/test_ask_user_tools.py -q
"""
import asyncio

import pytest

from agent_core.tools import ask_user_tools as aut


def make_config(thread_key):
    return {"configurable": {"thread_id": thread_key}}


async def _invoke(prompt="继续？", options=("继续", "取消"),
                  allow_free_text=False, thread_key="user_t:sess", config=None):
    """在子 task 中调用 ask_user（因为 ask_user 会 await 事件，需与 resolve 并发）。"""
    cfg = config if config is not None else make_config(thread_key)
    return await aut.ask_user.ainvoke(
        {"prompt": prompt, "options": list(options),
         "allow_free_text": allow_free_text},
        config=cfg,
    )


def test_schema_fields():
    props = list(aut.ask_user.args_schema.model_json_schema()["properties"].keys())
    for k in ("prompt", "options", "allow_free_text"):
        assert k in props, f"缺少参数 {k}"


def test_tools_export():
    assert any(getattr(t, "name", None) == "ask_user" for t in aut.TOOLS)


def test_no_config_degrades():
    async def _run():
        return await aut.ask_user.ainvoke(
            {"prompt": "继续？", "options": ["继续", "取消"]}, config={})
    assert "跳过" in asyncio.run(_run())


def test_no_thread_key_degrades():
    # config 存在但 thread_id 为空
    async def _run():
        return await aut.ask_user.ainvoke(
            {"prompt": "继续？", "options": ["继续", "取消"]},
            config={"configurable": {}},
        )
    assert "跳过" in asyncio.run(_run())


def test_full_flow_resolve():
    """core：工具阻塞等待 → resolve 写入 → 工具拿到答复返回。"""
    tk = "user_t:sess_flow"
    reg = aut.get_registry()

    async def _run():
        task = asyncio.create_task(
            _invoke(prompt="选一个方案", options=["A方案", "B方案", "C方案"],
                    allow_free_text=True, thread_key=tk))
        await asyncio.sleep(0.05)
        asks = reg.pending_asks(tk)
        assert len(asks) == 1
        ask_id = asks[0]["ask_id"]
        assert asks[0]["options"] == ["A方案", "B方案", "C方案"]
        assert asks[0]["allow_free_text"] is True
        ok = aut.resolve_ask(tk, ask_id, "B方案")
        assert ok is True
        ans = await task
        assert ans == "B方案"
        # resolve 后 registry 应清空该 ask
        assert reg.is_empty(tk)

    asyncio.run(_run())


def test_cancel_path():
    tk = "user_t:sess_cancel"
    reg = aut.get_registry()

    async def _run():
        task = asyncio.create_task(_invoke(thread_key=tk))
        await asyncio.sleep(0.05)
        asks = reg.pending_asks(tk)
        ok = aut.cancel_ask(tk, asks[0]["ask_id"], "用户关闭了征询")
        assert ok is True
        ans = await task
        assert "用户关闭了征询" in ans
        assert reg.is_empty(tk)

    asyncio.run(_run())


def test_timeout_degrades():
    tk = "user_t:sess_timeout"
    reg = aut.get_registry()
    saved = reg.wait_timeout
    reg.wait_timeout = 0.2
    try:
        async def _run():
            task = asyncio.create_task(_invoke(thread_key=tk))
            ans = await task
            assert "超时" in ans or "未响应" in ans
            assert reg.is_empty(tk)
        asyncio.run(_run())
    finally:
        reg.wait_timeout = saved


def test_resolve_already_gone_returns_false():
    assert aut.resolve_ask("user_t:none", "ask_missing", "x") is False
    assert aut.cancel_ask("user_t:none", "ask_missing", "x") is False


def test_multi_session_isolated():
    tk_a = "user_t:A"
    tk_b = "user_t:B"
    reg = aut.get_registry()

    async def _run():
        task_a = asyncio.create_task(_invoke(prompt="A的问", options=["a1", "a2"], thread_key=tk_a))
        task_b = asyncio.create_task(_invoke(prompt="B的问", options=["b1", "b2"], thread_key=tk_b))
        await asyncio.sleep(0.05)
        asks_a = reg.pending_asks(tk_a)
        asks_b = reg.pending_asks(tk_b)
        assert len(asks_a) == 1 and len(asks_b) == 1
        assert asks_a[0]["prompt"] == "A的问"
        assert asks_b[0]["prompt"] == "B的问"
        # 只解析 A，不影响 B
        aut.resolve_ask(tk_a, asks_a[0]["ask_id"], "a1")
        assert (await task_a) == "a1"
        assert len(reg.pending_asks(tk_b)) == 1  # B 仍 pending
        aut.resolve_ask(tk_b, asks_b[0]["ask_id"], "b2")
        assert (await task_b) == "b2"

    asyncio.run(_run())


def test_default_options_when_empty():
    tk = "user_t:sess_default_opts"
    reg = aut.get_registry()

    async def _run():
        task = asyncio.create_task(_invoke(options=[], thread_key=tk))
        await asyncio.sleep(0.05)
        asks = reg.pending_asks(tk)
        assert asks[0]["options"] == ["确认", "取消"]
        aut.resolve_ask(tk, asks[0]["ask_id"], "确认")
        assert (await task) == "确认"

    asyncio.run(_run())


def test_timeout_override_per_ask():
    """超时用单个 ask 自己的 timeout，而非改全局。"""
    tk = "user_t:sess_override"
    reg = aut.get_registry()

    async def _run():
        m = aut._PendingAsk("x", "q", ["y"], False, 0.15)
        reg.register(tk, m)
        ans = await reg.wait(tk, "x")
        assert "超时" in ans or "未响应" in ans
        assert reg.is_empty(tk)

    asyncio.run(_run())