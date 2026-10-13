"""回归测试：/models 与 /modals 命令能列出模型且不崩溃。

背景：生产 config.json 里存了排序元数据键 __provider_order__（值为 list），
_build_model_list_text 遍历 providers 时未跳过它，对 list 调 .get("api_key")
抛 AttributeError，导致 /modals 命中命令分支却构建时崩溃、无任何回复。
本测试锁定该函数对『含 __provider_order__ / 非 dict provider 条目』配置
不抛异常、且能正常列出模型。

同时确认 /models 别名也被命令分发识别（纯本地、不依赖 LLM）。
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

_HERE = Path(__file__).resolve().parent.parent
for _p in (str(_HERE / "agent_core"), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_core.wechat_bot import WeChatBot
import agent_core.wechat_commands as wcmd


def _make_bot(providers: dict) -> WeChatBot:
    """object.__new__ 绕过 __init__，构造 _ensure_agent().config 所需最小对象。"""
    bot = object.__new__(WeChatBot)
    bot._wechat_model_menu = {}
    provs = dict(providers)
    # 保证存在一个当前 provider/model 用于 '当前' 标记
    active_provider = provs.get("active_key", next(k for k in provs if k != "__provider_order__"))
    agent = SimpleNamespace(
        config=SimpleNamespace(
            providers=provs,
            active_provider=active_provider,
            model=provs[active_provider]["models"][0],
        )
    )
    bot._ensure_agent = lambda: agent
    return bot


def _providers_with_order_meta():
    """构造含 __provider_order__（list，正是线上崩溃的元数据）的 providers。"""
    return {
        "__provider_order__": ["with-deepseek", "with-stepfun"],  # list，旧代码对它调 .get 崩溃
        "with-deepseek": {"name": "深度求索", "models": ["deepseek-v3"], "api_key": "sk-1"},
        "with-stepfun": {"name": "阶跃星辰", "models": ["stepfun-1", "stepfun-2"], "api_key": "sk-2"},
    }


def test_build_model_list_skips_order_meta_and_lists_models():
    """含 __provider_order__(list) + 非 dict 条目时：不抛异常，且正确列出模型。"""
    provs = _providers_with_order_meta()
    provs["_ctx_old_"] = "not-a-dict"  # 归档键也不是 dict
    bot = _make_bot(provs)
    text = bot._build_model_list_text("u1")
    assert "deepseek-v3" in text
    assert "stepfun-1" in text and "stepfun-2" in text
    # 当前模型被标记
    assert "← 当前" in text
    # 菜单序号映射按跨 provider 扁平编号
    assert bot._wechat_model_menu["u1"]["1"] == ("with-deepseek", "deepseek-v3")
    assert bot._wechat_model_menu["u1"]["3"] == ("with-stepfun", "stepfun-2")


def test_build_model_list_empty_when_no_keys():
    """所有 provider 都没配置 key → 返回提示，不崩溃。"""
    bot = _make_bot({"only": {"name": "x", "models": ["m"]}})  # 无 api_key
    text = bot._build_model_list_text("u1")
    assert "暂无可用模型" in text


async def test_dispatch_models_alias_matches_command():
    """/models 别名应被命令分发识别（命中纯本地分支，不进 LLM）。"""
    bot = _make_bot(_providers_with_order_meta())
    bot.send_message = AsyncMock(return_value={"ret": 0})
    for cmd in ("/models", "/modals"):
        ok = await bot._handle_command(cmd, "u1", "tok-1", "u1")
        assert ok is True, f"{cmd} 应被识别为命令并已处理"
        sent = bot.send_message.await_args.args[2]
        assert "deepseek-v3" in sent, f"{cmd} 应列出模型"