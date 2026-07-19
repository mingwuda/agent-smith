"""守护进程（控制面）—— 运行时巡检与自愈编排。

设计（ponytail，混合架构）：
- 本进程独立于主 app，由 systemd 监管（Restart=always）。主 app 负责启动 + /health 探针
  + 进程内 boot 快路径；本进程负责"聪明"的事：周期探测健康、扫描日志、发现异常后
  编排恢复（LIFO 回退进化产物 + 触发重启）、写审计、升级人工。
- **观察永远执行，动作受 `enable_self_healing` 门控**：关时只记录、不改动；开时才回退/重启。
- 纯标准库 + 复用 `guardian` 的回退原语（`roll_back_latest`/`record_evolution`），不重复造轮子。
- 自身绝不因探测/分析失败而崩：所有外部调用（HTTP、文件、subprocess）都自吞异常。

运行：
    python -m agent_core.guardian_daemon            # 常驻循环
    python -m agent_core.guardian_daemon --once     # 只跑一轮（测试/调试）
环境变量：
    SELF_HEAL_HEALTH_URL   主 app 健康探针完整 URL（默认 http://127.0.0.1:8899/health）
    SELF_HEAL_RESTART_CMD  恢复时执行的重启命令（默认空=只记录不重启，如 systemctl restart desktop-agent）
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# 让本文件既能被 `python -m agent_core.guardian_daemon` 也能被直接 `python agent_core/guardian_daemon.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parent))

import guardian  # noqa: E402  (path bootstrap 之后)
from config import AgentConfig  # noqa: E402

logger = logging.getLogger("guardian_daemon")


# ---------- 路径解析（复用 main._app_base_dir 的同一逻辑，避免 import 重业务模块） ----------

def _app_base_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent  # agent_core/.. = 仓库根


def resolve_paths() -> dict:
    base = _app_base_dir()
    return {
        "generated_dir": base / "skills" / ".generated",
        "manifest_path": base / "skills" / ".generated" / "manifest.json",
        "quarantine_dir": base / "skills" / ".quarantine",
        "boot_ok_marker": base / ".boot_ok",
        "log_file": Path.home() / ".desktop_agent" / "logs" / "agent.log",
    }


# ---------- 健康探测（stdlib urllib，无新依赖） ----------

def _http_get_json(url: str, timeout: float = 5) -> Optional[dict]:
    import urllib.request
    import urllib.error
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # 连接失败 / 超时 / 非 JSON —— 一律视为不可达
        logger.debug("[守护] 健康探针失败: %s (%s)", url, exc)
        return None


def check_health(health_url: str, timeout: float = 5) -> Optional[dict]:
    """返回主 app 健康状态 dict 或 None（不可达）。"""
    return _http_get_json(health_url, timeout)


# ---------- 日志扫描（轻量启发式，完整分析见 DESIGN §4.6.3 后续） ----------

_ERROR_MARKERS = ("Traceback", "CRITICAL", "FATAL", "Unhandled")


def scan_log_for_errors(log_file: Path, tail_lines: int = 300) -> list[str]:
    """读日志末尾，收集错误类行。返回命中的行（最多 tail_lines）。"""
    if not log_file or not log_file.exists():
        return []
    try:
        lines = log_file.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []
    recent = lines[-tail_lines:]
    return [ln for ln in recent if any(m in ln for m in _ERROR_MARKERS)]


# ---------- 恢复编排（复用 guardian 回退原语） ----------

def trigger_restart(restart_cmd: Optional[str]) -> None:
    """触发主 app 重启。restart_cmd 为空时只记录不执行（安全默认）。"""
    if not restart_cmd:
        logger.warning("[守护] 需要重启主 app，但未配置 SELF_HEAL_RESTART_CMD，仅记录不执行")
        return
    try:
        logger.warning("[守护] 执行重启命令: %s", restart_cmd)
        subprocess.run(restart_cmd, shell=True, timeout=60, check=False)
    except Exception as exc:
        logger.exception("[守护] 重启命令执行失败: %s", exc)


def orchestrate_boot_recovery(paths: dict, restart_cmd: Optional[str]) -> bool:
    """编排一次 boot 恢复：LIFO 回退最近进化产物，必要时触发重启。

    返回 True 表示执行了回退动作。
    """
    manifest_path = Path(paths["manifest_path"])
    quarantine_dir = Path(paths["quarantine_dir"])
    moved = guardian.roll_back_latest(manifest_path, quarantine_dir)
    if moved is None:
        logger.info("[守护] boot 恢复：manifest 无登记产物，转而直接触发重启")
    trigger_restart(restart_cmd)
    return moved is not None or restart_cmd is not None


# ---------- 单轮巡检 ----------

def run_patrol(
    cfg: AgentConfig,
    paths: dict,
    health_url: str,
    restart_cmd: Optional[str],
    *,
    log_tail: int = 300,
) -> dict:
    """执行一轮巡检。观察永远执行；动作仅在 `enable_self_healing` 开启时执行。

    返回本轮摘要（便于测试与观测）。
    """
    summary = {"ts": int(time.time()), "acted": False, "findings": []}

    # 1) 健康探测
    health = check_health(health_url)
    unhealthy = (health is None) or (not health.get("boot_ok") and not health.get("agent_ready"))
    if unhealthy:
        summary["findings"].append("app_unhealthy")
        if getattr(cfg, "enable_self_healing", False):
            logger.warning("[守护] 探测到主 app 不健康: %s", health)
            orchestrate_boot_recovery(paths, restart_cmd)
            summary["acted"] = True
        else:
            logger.info("[守护] 主 app 不健康（观察模式，未开启自愈，仅记录）: %s", health)
    else:
        logger.info("[守护] 健康巡检通过: %s", health)

    # 2) 日志扫描（轻量启发式；完整分析/自愈器在 §4.6 后续阶段）
    errors = scan_log_for_errors(Path(paths["log_file"]), tail_lines=log_tail)
    if errors:
        summary["findings"].append(f"log_errors:{len(errors)}")
        if getattr(cfg, "enable_self_healing", False):
            # ponytail: 当前仅记录；未来接 §4.6.4 healers（如 quarantine_bad_skill / write_pitfall_memory）
            logger.warning("[守护] 日志发现 %d 处错误标记（观察记录，自愈逻辑待 §4.6 扩展）", len(errors))
            summary["acted"] = True
        else:
            logger.info("[守护] 日志发现 %d 处错误标记（观察模式，仅记录）", len(errors))

    return summary


# ---------- 主循环 ----------

def patrol_loop(
    cfg: AgentConfig,
    *,
    health_url: str,
    restart_cmd: Optional[str],
    stop=None,
    max_iterations: Optional[int] = None,
    interval: Optional[int] = None,
) -> None:
    paths = resolve_paths()
    interval = interval or getattr(cfg, "self_healing_interval_seconds", 600)
    iteration = 0
    logger.info("[守护] 巡检循环启动: 周期 %ss, 自愈=%s, 探针=%s",
                interval, getattr(cfg, "enable_self_healing", False), health_url)
    while True:
        try:
            run_patrol(cfg, paths, health_url, restart_cmd)
        except Exception:  # 自检任务自吞异常，绝不能成为新的不稳定源
            logger.exception("[守护] 单轮巡检异常（已忽略，继续循环）")
        iteration += 1
        if max_iterations is not None and iteration >= max_iterations:
            logger.info("[守护] 已达 max_iterations=%d，退出", max_iterations)
            return
        if stop is not None and stop.is_set():
            logger.info("[守护] 收到停止信号，退出")
            return
        time.sleep(interval)


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="桌面 Agent 守护进程（控制面：巡检与自愈编排）")
    p.add_argument("--once", action="store_true", help="只跑一轮巡检后退出（调试/测试）")
    p.add_argument("--health-url", default=os.getenv("SELF_HEAL_HEALTH_URL", "http://127.0.0.1:8899/health"))
    p.add_argument("--restart-cmd", default=os.getenv("SELF_HEAL_RESTART_CMD"))
    p.add_argument("--interval", type=int, default=None, help="覆盖巡检周期（秒）")
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    cfg = AgentConfig.load()

    if args.once:
        run_patrol(cfg, resolve_paths(), args.health_url, args.restart_cmd)
        return 0

    patrol_loop(cfg, health_url=args.health_url, restart_cmd=args.restart_cmd,
                interval=args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
