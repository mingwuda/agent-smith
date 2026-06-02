"""
统一日志模块 —— 全局日志配置，支持 7 天滚动轮换。

用法:
    from logger import get_logger, setup_logging

    # 在服务入口处初始化
    setup_logging()

    # 各模块获取 logger
    logger = get_logger(__name__)
    logger.info("服务启动成功")
    logger.error("连接失败", exc_info=True)
"""

import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

DEFAULT_LOG_DIR = Path.home() / ".desktop_agent" / "logs"
DEFAULT_LOG_FILE = "agent.log"
DEFAULT_LOG_LEVEL = "INFO"

# 日志格式
_FILE_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_CONSOLE_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 单例标记：防止重复初始化
_initialized = False


def setup_logging(
    log_dir: str | Path | None = None,
    log_file: str = DEFAULT_LOG_FILE,
    level: str | int | None = None,
    console: bool = True,
    backup_count: int = 4,
) -> logging.Logger:
    """初始化全局日志配置。

    参数:
        log_dir: 日志目录，默认 ~/.desktop_agent/logs
        log_file: 日志文件名，默认 agent.log
        level: 日志级别，默认从环境变量 AGENT_LOG_LEVEL 读取，回退到 INFO
        console: 是否同时输出到控制台，默认 True
        backup_count: 保留的旧日志文件数，默认 4（共保留约 28 天）

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

    # ── 文件 Handler：每 7 天滚动一次 ──
    file_handler = TimedRotatingFileHandler(
        log_path,
        when="D",
        interval=7,
        backupCount=backup_count,
        encoding="utf-8",
        delay=False,
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(file_handler)

    # ── 控制台 Handler ──
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(logging.Formatter(_CONSOLE_FORMAT, datefmt=_DATE_FORMAT))
        root.addHandler(console_handler)

    _initialized = True
    root.info("日志系统已初始化, 文件: %s, 级别: %s, 7 天自动滚动", log_path, logging.getLevelName(level))
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
