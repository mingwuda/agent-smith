"""回归测试：微信 Bot step 消息批量合并算法（_build_step_batches）。

防止攒批逻辑被改坏（例如：残余批不再 flush、head 重复拼接、空输入报错），
导致"预算用尽后用户只能干等最终消息"的问题复发。
"""
import sys
from pathlib import Path

# wechat_bot.py 使用扁平风格导入（from logger import ... / import session_store），
# 依赖 agent_core/ 在 sys.path；与 agent_core/main.py 相同的 sys.path 处理保持一致。
_HERE = Path(__file__).resolve().parent.parent       # 项目根
_AGENT_CORE = _HERE / "agent_core"
for _p in (str(_AGENT_CORE), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_core.wechat_bot import WeChatBot


def test_empty_lines_returns_no_batches():
    assert WeChatBot._build_step_batches([], batch=3) == []


def test_full_batch_single_message():
    lines = ["✅ a", "✅ b", "✅ c"]
    batches = WeChatBot._build_step_batches(lines, batch=3)
    assert len(batches) == 1
    assert batches[0] == "✅ a\n✅ b\n✅ c"


def test_remainder_flushed_as_tail_batch():
    """5 个工具结果、batch=3 → 2 条消息（3+2），残余批必须 flush，不留过程死角。"""
    lines = ["✅ a", "✅ b", "✅ c", "❌ d", "✅ e"]
    batches = WeChatBot._build_step_batches(lines, batch=3)
    assert len(batches) == 2
    assert batches[0] == "✅ a\n✅ b\n✅ c"
    assert batches[1] == "❌ d\n✅ e"


def test_head_only_prepended_to_first_batch():
    """💭思考只拼到第一条消息（markdown 加粗标题），避免每条都重复思考内容。"""
    lines = ["✅ a", "✅ b", "✅ c", "✅ d"]
    batches = WeChatBot._build_step_batches(lines, batch=3, head="正在查资料")
    assert len(batches) == 2
    assert batches[0] == "**💭 思考**\n正在查资料\n✅ a\n✅ b\n✅ c"
    assert batches[1] == "✅ d"


def test_no_head_prefix_when_head_empty():
    batches = WeChatBot._build_step_batches(["✅ a"], batch=3, head="")
    assert batches == ["✅ a"]



# ── 滑动窗口发送限速（_rate_limit_send）──
# 用 object.__new__ 绕过 __init__（避免拉起整个 Bot 的依赖），只测限速算法本身。
# 用 asyncio.run 直接跑 async 方法，不依赖 pytest-asyncio 插件。

import asyncio
import time


def _make_bot(window: float, max_per_window: int):
    bot = object.__new__(WeChatBot)
    bot.user_id = "test"
    bot.send_rate_window = window
    bot.send_rate_max = max_per_window
    bot._send_timestamps = []
    return bot


def test_rate_limit_clears_expired_window_and_accepts():
    """窗口内旧时间戳应被清理，未满时直接通过（不 sleep）。"""
    bot = _make_bot(window=60.0, max_per_window=2)
    bot._send_timestamps = [time.time() - 61.0, time.time() - 62.0]  # 均已出窗口

    async def run():
        t0 = time.time()
        await bot._rate_limit_send()
        return time.time() - t0

    elapsed = asyncio.run(run())
    assert elapsed < 0.5, f"不应等待，耗时 {elapsed:.2f}s"
    assert len(bot._send_timestamps) == 1  # 旧的全清掉，只留本次


def test_rate_limit_waits_when_window_full():
    """窗口已满时应该等待（让最早的发送滑出窗口）再放行。"""
    bot = _make_bot(window=0.3, max_per_window=1)
    bot._send_timestamps = [time.time()]  # 窗口内已满 1 条

    async def run():
        t0 = time.time()
        await bot._rate_limit_send()
        return time.time() - t0

    elapsed = asyncio.run(run())
    # 需等待最早时间戳滑出 0.3s 窗口；留容差避免 CI 抖动
    assert 0.25 <= elapsed <= 2.0, f"应等待约 0.3s，实际 {elapsed:.2f}s"
    assert len(bot._send_timestamps) == 1
