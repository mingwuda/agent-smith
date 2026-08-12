"""会话历史检索工具（落盘 SQLite）。"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from langchain_core.tools import tool

import user_manager


def _db_path(user_id: str) -> Path:
    return user_manager.session_dir(user_id) / "sessions.sqlite3"


def _connect(user_id: str = "default") -> sqlite3.Connection:
    db_path = _db_path(user_id)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _parse_iso(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return datetime.min


@tool
def session_history_search(
    query: str,
    user_id: str = "default",
    session_id: str = "",
    time_range: str = "",
    limit: int = 20,
) -> str:
    """按关键字 + 时间范围检索历史会话（SQLite 落盘内容）。

    适用场景：压缩后上下文里找不到的早期会话信息，可用本工具从落盘库中召回。
    返回结果按时间倒序，包含会话标题、时间、匹配消息摘要。

    参数:
      query: 检索关键字（为空时返回最近会话列表）。
      user_id: 用户 ID，默认 default。
      session_id: 限定单会话（可选）。
      time_range: 时间范围，支持 "7d" / "today" / "2026-08-01~2026-08-12" 等。
      limit: 最多返回条数，默认 20。
    """
    if limit is None or limit <= 0:
        limit = 20
    if limit > 100:
        limit = 100

    try:
        conn = _connect(user_id)
    except Exception as e:
        return f"❌ 无法打开会话库：{e}"

    try:
        where: list[str] = []
        params: list[Any] = []

        if session_id:
            where.append("m.session_id = ?")
            params.append(session_id)

        # 时间范围解析
        start_ts: Optional[str] = None
        end_ts: Optional[str] = None
        if time_range:
            from datetime import timedelta
            tr = time_range.strip()
            now = datetime.now()
            try:
                if tr.lower() == "today":
                    start_ts = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
                    end_ts = now.isoformat()
                elif tr.endswith("d") and tr[:-1].isdigit():
                    days = int(tr[:-1])
                    start_ts = (now - timedelta(days=days)).isoformat()
                    end_ts = now.isoformat()
                elif "~" in tr:
                    parts = tr.split("~", 1)
                    start_ts = parts[0].strip()
                    end_ts = parts[1].strip()
            except Exception:
                start_ts = end_ts = None

        if start_ts:
            where.append("m.timestamp >= ?")
            params.append(start_ts)
        if end_ts:
            where.append("m.timestamp <= ?")
            params.append(end_ts)

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        if query:
            q = query.strip()
            like = f"%{q}%"
            sql = (
                "SELECT s.id, s.title, s.updated_at, m.role, m.content, m.timestamp "
                "FROM sessions s "
                "JOIN messages m ON m.session_id = s.id "
                + where_sql +
                " AND m.content LIKE ? "
                "ORDER BY m.timestamp DESC "
                "LIMIT ?"
            )
            params.extend([like, limit])
        else:
            sql = (
                "SELECT s.id, s.title, s.updated_at, m.role, m.content, m.timestamp "
                "FROM sessions s "
                "JOIN messages m ON m.session_id = s.id "
                + where_sql +
                "ORDER BY m.timestamp DESC "
                "LIMIT ?"
            )
            params.append(limit)

        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        if not rows:
            return "未找到匹配的历史会话消息。"

        lines: list[str] = []
        for r in rows:
            title = r["title"] or ""
            ts = r["timestamp"] or ""
            role = r["role"] or ""
            content = (r["content"] or "")[:300]
            try:
                parsed = __import__("json").loads(content)
                if isinstance(parsed, dict):
                    content = parsed.get("text") or parsed.get("content") or content
            except Exception:
                pass
            content = content.replace("\n", " ").strip()
            lines.append(f"[{ts}] {title} | {role}: {content}")

        return "\n".join(lines)
    except sqlite3.OperationalError as e:
        return f"❌ 会话库查询失败：{e}"
    finally:
        try:
            conn.close()
        except Exception:
            pass


TOOLS = [session_history_search]
