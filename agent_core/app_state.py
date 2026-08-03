"""进程级应用状态注册表（bootstrap 汇合点）。

背景：main.py 曾是 God Object——持有 agent / wechat_bots / UI 路径等全局单例，
路由与服务模块反向 ``from main import agent / _get_wechat_bot / UI_DIR ...``，
形成 main ↔ routes 的双向循环依赖：启动时序敏感（依赖 sys.modules["main"]
别名 hack 才不触发二次执行）、单测相互污染、多实例/多用户被全局变量锁死。

本模块是单向依赖的"汇合点"，只持有状态，不 import 任何业务模块：

    main.py         装配方：创建 app / 调 init_agent，把产物写入本模块
    路由/服务模块   消费方：只从这里读取状态，绝不 import main

依赖方向：main → app_state ← routes，无环。

并发说明：进程内以单线程事件循环为主，简单赋值由 GIL 保证原子；wechat_bots
为可变 dict，操作集中在请求/启动路径，与原先挂在 ``app.state`` 上时一致。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Callable, Optional

_app: Any = None
_agent: Any = None
_base_tools: list = []
_wechat_bots: dict[str, Any] = {}
_main_loop: Any = None
_agent_config: Any = None
_ui_dir: Optional[Path] = None
_html_content: Optional[str] = None
_init_agent_fn: Optional[Callable[[], None]] = None


def set_app(app_obj):
    global _app
    _app = app_obj


def get_app():
    return _app


def set_agent(a):
    global _agent
    _agent = a


def get_agent():
    return _agent


def set_base_tools(tools):
    global _base_tools
    _base_tools = list(tools)


def get_base_tools() -> list:
    return _base_tools


def set_wechat_bots(bots: dict):
    global _wechat_bots
    _wechat_bots = bots


def get_wechat_bots() -> dict:
    return _wechat_bots


def set_main_loop(loop):
    global _main_loop
    _main_loop = loop


def get_main_loop():
    return _main_loop


def set_agent_config(cfg):
    global _agent_config
    _agent_config = cfg


def get_agent_config():
    return _agent_config


def get_app_base_dir() -> Path:
    """项目根目录（源码模式）或 PyInstaller 资源根目录（打包模式）。"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def set_ui_dir(p: Path):
    global _ui_dir
    _ui_dir = Path(p)


def get_ui_dir() -> Optional[Path]:
    return _ui_dir


def set_html_content(content: Optional[str]):
    global _html_content
    _html_content = content


def get_html_content() -> Optional[str]:
    return _html_content


def set_init_agent(fn: Optional[Callable[[], None]]):
    global _init_agent_fn
    _init_agent_fn = fn


def get_init_agent() -> Optional[Callable[[], None]]:
    return _init_agent_fn


def get_or_create_wechat_bot(uid: str):
    """获取或创建指定用户的微信 Bot（懒加载，原 main._get_wechat_bot）。

    ponytail: WeChatBot 内部懒创建该用户专属的独立 agent 实例，避免多微信
    用户 / Web 端共用全局 agent 导致跨用户上下文串扰（数据泄露）。tools
    显式传基准工具集（app_state 持有，与 main 装配时写入的一致）。
    """
    bot = _wechat_bots.get(uid)
    if bot is None:
        from wechat_bot import WeChatBot
        _tools = get_base_tools() or (getattr(get_agent(), "tools", None) if get_agent() else [])
        bot = WeChatBot(user_id=uid, tools=_tools)
        _wechat_bots[uid] = bot
        if bot.is_logged_in:
            try:
                asyncio.create_task(bot.start())
            except Exception:
                pass
    return bot
