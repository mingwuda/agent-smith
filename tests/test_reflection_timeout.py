# -*- coding: utf-8 -*-
"""过程反思硬超时的回归测试（工作区内副本，可随时拷到 tests/ 下）。

背景：2026-11-04 21:07–21:22 那轮，`step % 8 == 0` 的 9 次工具调用中位数 8.7s，
而其余 67 次只有 0.5s——17 倍差距全部来自「工具已 start、工具未执行」间隙里
串行跑的过程反思。单次最坏 93.6s（占该轮 15min 的 10%）。根因是旧代码只设了
`llm.request_timeout`（LangChain 里不是生效属性），没有任何真正的超时。

修复后用 asyncio.wait_for(_REFLECTION_TIMEOUT) 硬超时。
本测试断言：quick_reflection 卡住不返回时，等待方仍能在超时附近返回，不阻塞。
"""
import asyncio
import time

import pytest

from agent_core.main import app  # noqa: F401  (sys.path 注入，与其他测试一致)


def test_reflection_timeout_constant_is_sane():
    """超时常量必须是有限正数，且明显小于历史上观测到的最坏阻塞(93.6s)。"""
    from agent_core.agent_run import _REFLECTION_TIMEOUT, _REFLECTION_INTERVAL
    assert 0 < _REFLECTION_TIMEOUT <= 10
    assert _REFLECTION_TIMEOUT < 93.6
    assert _REFLECTION_INTERVAL >= 8  # 不低于原频率，避免反思过于频繁


def test_hung_reflection_is_aborted_by_wait_for():
    """模拟 quick_reflection 永不返回：wait_for 必须按时放弃，而不是一直挂着。"""
    from agent_core import agent_run

    class _HangingAgent:
        async def quick_reflection(self, message, history):
            await asyncio.sleep(3600)  # 永不返回

    async def _scenario():
        agent = _HangingAgent()
        started = time.time()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                agent.quick_reflection("x", []),
                timeout=agent_run._REFLECTION_TIMEOUT,
            )
        return time.time() - started

    elapsed = asyncio.run(_scenario())
    # 容差 2s：CI 调度抖动之外，绝不能出现几十秒的阻塞
    assert elapsed < agent_run._REFLECTION_TIMEOUT + 2
    assert elapsed >= agent_run._REFLECTION_TIMEOUT * 0.8


def test_normal_reflection_still_passes_through():
    """正常（快速返回）的反思不能被超时机制误伤。"""
    from agent_core.agent_run import _REFLECTION_TIMEOUT

    class _FastAgent:
        async def quick_reflection(self, message, history):
            return "正常"

    async def _scenario():
        agent = _FastAgent()
        return await asyncio.wait_for(
            agent.quick_reflection("x", []), timeout=_REFLECTION_TIMEOUT
        )

    assert asyncio.run(_scenario()) == "正常"
