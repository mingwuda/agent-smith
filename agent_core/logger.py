"""
统一日志模块 —— 每日按日期轮换，自动清理超期日志。

提供 `set_log_context()` / `clear_log_context()`，在请求处理开始时设定
session_id 和 message_id，后续所有日志行自动携带上下文前缀。

用法:
    from logger import get_logger, setup_logging, set_log_context, clear_log_context

    # 在服务入口处初始化
    setup_logging()

    # 各模块获取 logger
    logger = get_logger(__name__)

    # 在每个请求开始时设定上下文
    set_log_context(session_id="abc123", message_id="uuid")
    logger.info("服务启动成功")   # → 日志行末尾自动包含 [s:abc123] [m:uuid]
    clear_log_context()
"""
from __future__ import annotations

import contextvars
import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional, Union

DEFAULT_LOG_DIR = Path.home() / ".desktop_agent" / "logs"
DEFAULT_LOG_FILE = "agent.log"
DEFAULT_LOG_LEVEL = "INFO"

# 日志格式
_FILE_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_CONSOLE_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 单例标记：防止重复初始化
_initialized = False

# ── 线程安全（实际是协程安全）的日志上下文 ──
_session_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "log_session_id", default=""
)
_message_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "log_message_id", default=""
)


def set_log_context(session_id: str = "", message_id: str = "") -> None:
    """设置当前请求的日志上下文（session_id / message_id）。

    之后所有日志行会自动带上 [s:xxx] [m:xxx] 前缀。
    请求结束时调用 clear_log_context() 清除。
    """
    _session_id_var.set(session_id or "")
    _message_id_var.set(message_id or "")


def clear_log_context() -> None:
    """清除当前请求的日志上下文。"""
    _session_id_var.set("")
    _message_id_var.set("")


class _LogContextFilter(logging.Filter):
    """日志过滤器：将当前请求的 session_id / message_id 注入日志记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        sid = _session_id_var.get("")
        mid = _message_id_var.get("")
        parts: list[str] = []
        if sid:
            parts.append(f"[s:{sid[:12]}]")
        if mid:
            parts.append(f"[m:{mid[:8]}]")
        if parts:
            record.msg = f"{' '.join(parts)} {record.msg}"
        return True


# 全局单例 Filter
_LOG_CONTEXT_FILTER = _LogContextFilter()


def setup_logging(
    log_dir: Optional[Union[str, Path]] = None,
    log_file: str = DEFAULT_LOG_FILE,
    level: Optional[Union[str, int]] = None,
    console: bool = True,
    retain_days: int = 7,
) -> logging.Logger:
    """初始化全局日志配置。

    参数:
        log_dir: 日志目录，默认 ~/.desktop_agent/logs
        log_file: 日志文件名，默认 agent.log
        level: 日志级别，默认从环境变量 AGENT_LOG_LEVEL 读取，回退到 INFO
        console: 是否同时输出到控制台，默认 True
        retain_days: 保留天数（含当天），默认 7

    返回:
        root logger
    """
    global _initialized

    # 确定日志级别
    if level is None:
        level = os.getenv("AGENT_LOG_LEVEL", DEFAULT_LOG_LEVEL)
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    # 确定日志目录
    if log_dir is None:
        log_dir = DEFAULT_LOG_DIR
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    log_path = log_dir / log_file

    # 获取 root logger
    root = logging.getLogger()
    root.setLevel(level)

    # 避免重复添加 handler（已初始化则清除旧 handler 重建）
    if _initialized:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()

    # 静默 httpx 的请求日志（轮询消息太频繁）
    # 静默 uvicorn access 的健康检查日志（前端定时轮询）
    class _NoiseFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            # 过滤 httpx/httpcore 的 INFO 日志
            if record.name in ("httpx", "httpcore") and record.levelno <= logging.INFO:
                return False
            # 过滤 /health 的访问日志
            if record.name == "uvicorn.access" and "/health" in str(record.msg):
                return False
            return True
    root.addFilter(_NoiseFilter())

    # ── 文件 Handler：每天午夜滚动，保留 retain_days 天 ──
    # ponytail: 自定义命名 agent.2026-07-23.log（而非默认的 agent.log.2026-07-23）
    _stem = log_path.stem  # "agent"
    _ext = log_path.suffix  # ".log"

    def _daily_namer(default_name: str) -> str:
        """把 agent.log.2026-07-23 → agent.2026-07-23.log"""
        # default_name = "/path/to/agent.log.2026-07-23"
        p = Path(default_name)
        date_part = p.suffixes[-1].lstrip(".")  # "2026-07-23"
        return str(p.with_name(f"{_stem}.{date_part}{_ext}"))

    file_handler = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        interval=1,
        backupCount=max(0, retain_days - 1),
        encoding="utf-8",
        delay=False,
    )
    file_handler.namer = _daily_namer
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT, datefmt=_DATE_FORMAT))
    file_handler.addFilter(_LOG_CONTEXT_FILTER)
    root.addHandler(file_handler)

    # ── 控制台 Handler ──
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(logging.Formatter(_CONSOLE_FORMAT, datefmt=_DATE_FORMAT))
        console_handler.addFilter(_LOG_CONTEXT_FILTER)
        root.addHandler(console_handler)

    _initialized = True
    root.info("日志系统已初始化, 文件: %s, 级别: %s, 每日滚动, 保留 %d 天", log_path, logging.getLevelName(level), retain_days)
    return root


def get_logger(name: str) -> logging.Logger:
    """获取指定名称的 logger 实例。"""
    return logging.getLogger(name)


def shutdown_logging():
    """关闭日志系统，刷新并释放所有 handler。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        handler.flush()
        handler.close()
        root.removeHandler(handler)
