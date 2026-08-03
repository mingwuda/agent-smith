"""
工具诊断日志 —— 集中记录「错误调用」与「超长调用」，作为后续优化方向的数据源。

文件: ~/.desktop_agent/logs/tool_diagnostics.log
- 只写两类事件：工具返回错误(error) / 耗时超过阈值(slow)
- 每行一条 JSON（ts/tool/duration_ms/error/slow/args/result/session），便于 grep 或脚本解析
- 超过 MAX_BYTES 自动轮转一次（保留 .old 一份），避免无限增长

用法:
    from monitoring.tool_diagnostics import log_tool_event
    log_tool_event("run_shell", 45.2, False, {"command": "pytest"}, "ok", "tid123")
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from logger import DEFAULT_LOG_DIR

DIAG_LOG_PATH = DEFAULT_LOG_DIR / "tool_diagnostics.log"
# 超长阈值（秒）：可从环境变量覆盖；30s 对绝大多数工具偏慢
SLOW_THRESHOLD_S = float(os.getenv("TOOL_SLOW_THRESHOLD_S", "30"))
MAX_BYTES = 20 * 1024 * 1024  # 20MB，超限轮转一次

_lock = threading.Lock()


def _rollover_if_needed() -> None:
    """超过 MAX_BYTES 时把当前文件重命名为 .old（只保留一份历史）。"""
    try:
        if DIAG_LOG_PATH.exists() and DIAG_LOG_PATH.stat().st_size > MAX_BYTES:
            old = DIAG_LOG_PATH.with_suffix(DIAG_LOG_PATH.suffix + ".old")
            old.unlink(missing_ok=True)
            DIAG_LOG_PATH.rename(old)
    except OSError:
        pass


def _truncate_dict(d: Optional[dict]) -> dict:
    """工具入参摘要：跳过内部键、每个值截断到 200 字符。"""
    if not isinstance(d, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in d.items():
        if k.startswith("_"):
            continue
        s = str(v)
        out[k] = s[:200] + ("..." if len(s) > 200 else "")
    return out


def log_tool_event(
    tool: str,
    duration_s: float,
    is_error: bool,
    args: Optional[dict] = None,
    result_preview: str = "",
    session: str = "",
    slow_threshold_s: float = SLOW_THRESHOLD_S,
) -> bool:
    """记录一次工具调用事件；仅当 is_error 或耗时超阈值时写入。返回是否写入。"""
    if not is_error and duration_s < slow_threshold_s:
        return False
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "tool": tool,
        "duration_ms": int(duration_s * 1000),
        "error": is_error,
        "slow": duration_s >= slow_threshold_s,
        "args": _truncate_dict(args),
        "result": (result_preview or "")[:300],
        "session": (session or "")[:16],
    }
    line = json.dumps(record, ensure_ascii=False)
    with _lock:
        _rollover_if_needed()
        try:
            DIAG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with DIAG_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            return True
        except OSError:
            return False
