"""SSE 流事件持久化 —— 刷新浏览器后可从文件系统恢复实时画面。

背景：/run/stream 的 SSE 事件此前只推给当前 fetch 连接，不落盘。客户端刷新/断开后，
本轮实时输出（三段式卡片、工具步骤、最终回答）全部丢失，且后端断开即 aclose 杀掉
仍在跑的 agent。

本模块提供三块能力，供路由层与前端恢复 API 复用：
  1. 事件实时 append 到  ~/.desktop_agent/users/{uid}/streams/{session_id}/{message_id}.jsonl
     （一行一个 JSON 事件对象，终态事件带 finished 标记）
  2. 运行中登记  running.json  （该会话当前在跑的 message_id 列表，agent 后台续跑期间存在）
  3. 读取/回放  （列目录、读事件、判断 finished）

设计取舍（ponytail）：
  - 用 jsonl 而非 SQLite：事件是追加型、单写者（一个 run 一个消费者）、读取时整体回放，
    文件最直观、无锁、调试友好；单会话并发 run 数极少，目录列举足够。
  - 写入在路由层（已解析出 dict 的位置）调用，不重复解析原始 sse 文本。
  - 清理：finished 且超过保留期的文件由 cleanup() 定期删除，避免无限膨胀。
"""
import json
import re
import time
from pathlib import Path
from typing import Optional

import user_manager

_STREAMS_SUBDIR = "streams"
# 保留 finished 流日志的天数（之后清理，防磁盘无限膨胀）
KEEP_DAYS = 7
_RUNNING_FILE = "running.json"


def _streams_dir(user_id: str) -> Path:
    """用户级流日志根目录（每个 user 一个，session 一级子目录）。"""
    base = user_manager.session_dir(user_id) / _STREAMS_SUBDIR
    base.mkdir(parents=True, exist_ok=True)
    return base


def _session_dir(user_id: str, session_id: str) -> Path:
    d = _streams_dir(user_id) / re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "default")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _event_path(user_id: str, session_id: str, message_id: str) -> Path:
    return _session_dir(user_id, session_id) / f"{message_id}.jsonl"


# ---------- 写：实时追加事件 ----------

def append_event(user_id: str, session_id: str, message_id: str, event: dict) -> None:
    """把一条已解析的 SSE 事件追加落盘。

    event: 解析后的 dict（即 `data: {...}` 的 JSON 对象）。
    终态事件（type in {done, error}）会额外打上 finished 标记，供回放端判断本轮是否结束。
    任何 I/O 异常都自吞（流日志丢失不应影响主流程）。
    """
    try:
        rec = dict(event)
        if rec.get("type") in ("done", "error"):
            rec["finished"] = True
        with _event_path(user_id, session_id, message_id).open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # ponytail: 流日志是增强能力，写失败不能阻断 agent 主流程


def mark_running(user_id: str, session_id: str, message_id: str) -> None:
    """登记一个 run 正在后台执行（客户端断开后 agent 续跑期间存在）。"""
    try:
        p = _session_dir(user_id, session_id) / _RUNNING_FILE
        data = {}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        if message_id not in data:
            data[message_id] = {"started_at": time.time(), "finished": False}
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def clear_running(user_id: str, session_id: str, message_id: str, finished: bool = True) -> None:
    """run 结束（正常完成 / 出错 / 被取消）后从登记移除。"""
    try:
        p = _session_dir(user_id, session_id) / _RUNNING_FILE
        data = {}
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
        if message_id in data:
            data[message_id]["finished"] = finished
            del data[message_id]
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def get_active_runs(user_id: str, session_id: str) -> dict:
    """读取该会话当前在跑的 message_id 登记表（可能为空 dict）。"""
    try:
        p = _session_dir(user_id, session_id) / _RUNNING_FILE
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ---------- 读：回放 ----------

def read_events(user_id: str, session_id: str, message_id: str) -> list[dict]:
    """读取一个 run 已落盘的全部事件（按时间顺序，已解析为 dict 列表）。"""
    p = _event_path(user_id, session_id, message_id)
    if not p.exists():
        return []
    events = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                # 半行（写入瞬间被读）丢弃，下次轮询/重载自然补全
                continue
    except Exception:
        pass
    return events


def last_active_run(user_id: str, session_id: str) -> Optional[dict]:
    """取该会话最近一个 run（优先在跑的，其次按文件 mtime 最新的 finished）。

    返回: {"message_id":..., "events":[...], "finished": bool, "running": bool} 或 None。
    前端刷新后调用：有 running 就实时续看，只有 finished 就回放定稿画面。
    """
    active = get_active_runs(user_id, session_id)
    cand = []
    for mid in active:
        cand.append((time.time(), mid, True))
    sd = _session_dir(user_id, session_id)
    for f in sd.glob("*.jsonl"):
        try:
            cand.append((f.stat().st_mtime, f.stem, False))
        except OSError:
            continue
    if not cand:
        return None
    running_c = [c for c in cand if c[2]]
    if running_c:
        mid = running_c[0][1]
    else:
        mid = max(cand, key=lambda x: x[0])[1]
    evs = read_events(user_id, session_id, mid)
    finished = bool(evs) and evs[-1].get("finished") is True
    return {"message_id": mid, "events": evs, "finished": finished, "running": mid in active}


# ---------- 清理 ----------

def cleanup(user_id: str, session_id: str, keep_days: int = KEEP_DAYS) -> int:
    """删除 finished 且超过保留期的 jsonl，返回删除个数。"""
    removed = 0
    sd = _session_dir(user_id, session_id)
    cutoff = time.time() - keep_days * 86400
    active = set(get_active_runs(user_id, session_id).keys())
    for f in sd.glob("*.jsonl"):
        if f.stem in active:
            continue
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            continue
    return removed
