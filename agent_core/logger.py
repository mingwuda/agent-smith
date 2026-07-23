"""
统一日志模块 —— 每日独立日期文件，自动清理超期日志。

每天一个文件: agent.2026-07-23.log, agent.2026-07-22.log, ...
超过 retain_days 的自动删除。
午夜自动切换到新一天文件（后台守护线程，无需每天重启）。

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
import threading
import time
from datetime import date, timedelta
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


class DailyLogFileHandler(logging.FileHandler):
    """每天一个独立日志文件: agent.2026-07-23.log。

    特性:
      - 当前文件名自带日期（而非 TimedRotatingFileHandler 那样只有滚动后才有日期后缀）
      - 后台守护线程每分钟检查日期变更，午夜自动切换到新文件（无需重启）
      - 启动时自动创建当天文件
      - 每次 rollover 自动清理超过 retain_days 的旧文件
      - 兼容 logging.Handler 接口，可直接替换 TimedRotatingFileHandler
    """

    def __init__(
        self,
        log_dir: Union[str, Path],
        stem: str = "agent",
        suffix: str = ".log",
        retain_days: int = 7,
        encoding: str = "utf-8",
        delay: bool = False,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.stem = stem
        self.suffix = suffix
        self.retain_days = retain_days
        self._current_date = date.today()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

        # 确保目录存在
        self.log_dir.mkdir(parents=True, exist_ok=True)

        filepath = self._filepath(self._current_date)
        super().__init__(str(filepath), encoding=encoding, delay=delay)

        # 启动后台守护线程（每 60s 检查是否跨天）
        self._watcher = threading.Thread(target=self._watch_midnight, daemon=True, name="log-daily-rotator")
        self._watcher.start()

    def _filepath(self, d: date) -> Path:
        return self.log_dir / f"{self.stem}.{d.strftime('%Y-%m-%d')}{self.suffix}"

    def _watch_midnight(self) -> None:
        """后台线程：每分钟检查日期是否变更，跨天则执行 rollover。"""
        while not self._stop_event.is_set():
            self._stop_event.wait(60)  # 每 60s 检查一次
            if self._stop_event.is_set():
                break
            today = date.today()
            if today != self._current_date:
                with self._lock:
                    # double-check after acquiring lock
                    if today == self._current_date:
                        continue
                    self._perform_rollover(today)

    def _perform_rollover(self, new_date: date) -> None:
        """关闭当前文件、清理超期、打开新日期文件。"""
        # 先 flush + close 当前流
        self.flush()
        if self.stream and not self.stream.closed:
            try:
                self.stream.close()
            except OSError:
                pass

        self._current_date = new_date
        new_path = self._filepath(new_date)

        # 清理超期日志
        self._cleanup_old_logs()

        # 切换到新文件
        self.baseFilename = str(new_path)
        self.mode = "a"
        self.stream = open(self.baseFilename, mode=self.mode, encoding=self.encoding)

    def _cleanup_old_logs(self) -> None:
        """删除超过 retain_days 的旧日志文件。"""
        cutoff = date.today() - timedelta(days=self.retain_days)
        pattern = f"{self.stem}.*{self.suffix}"
        for f in self.log_dir.glob(pattern):
            try:
                # 从文件名提取日期: agent.2026-07-22.log → 2026-07-22
                name = f.name
                # 去掉 stem 前缀和 suffix 后缀
                date_str = name[len(self.stem) + 1:-len(self.suffix)]
                file_date = date.fromisoformat(date_str)
                if file_date < cutoff:
                    f.unlink()
            except (ValueError, IndexError, OSError):
                # 文件名不符合预期格式，跳过
                pass

    def close(self) -> None:
        self._stop_event.set()
        with self._lock:
            super().close()


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
        log_file: 日志文件名（仅取 stem 部分），默认 agent.log
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

    # 从 log_file 提取 stem (如 "agent.log" → stem="agent", suffix=".log")
    _p = Path(log_file)
    _stem = _p.stem
    _ext = _p.suffix or ".log"

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

    # ── 文件 Handler：每天一个日期文件，自动清理超期 ──
    today = date.today()
    log_path = log_dir / f"{_stem}.{today.strftime('%Y-%m-%d')}{_ext}"

    file_handler = DailyLogFileHandler(
        log_dir,
        stem=_stem,
        suffix=_ext,
        retain_days=retain_days,
        encoding="utf-8",
        delay=False,
    )
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
    root.info("日志系统已初始化, 文件: %s, 级别: %s, 每日独立文件, 保留 %d 天", log_path, logging.getLevelName(level), retain_days)
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
