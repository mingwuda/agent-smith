"""session_history_search 工具回归测试。"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from agent_core.main import app  # noqa: F401
from agent_core.tools.session_tools import session_history_search
from agent_core.session_store import (
    create_session,
    add_message,
    get_session,
    _connect as _session_connect,
)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def test_keyword_search_matches_message_content():
    uid = "test_user"
    sess = create_session(uid, title="测试会话", session_id="sess1")
    add_message(uid, "sess1", "user", "把 /data/file.py 重构为异步")
    add_message(uid, "sess1", "assistant", "收到，开始重构。")
    result = session_history_search.invoke({"query": "/data/file.py", "user_id": uid})
    assert "/data/file.py" in result
    assert "重构" in result


def test_time_range_today():
    uid = "test_user"
    create_session(uid, title="今天", session_id="today1")
    add_message(uid, "today1", "user", "今天的消息")
    old_ts = "2000-01-01T00:00:00"
    with _session_connect(uid) as conn:
        conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)", ("today1", "user", "旧消息", old_ts))
    result = session_history_search.invoke({"query": "消息", "user_id": uid, "time_range": "today"})
    assert "今天的消息" in result
    assert "旧消息" not in result


def test_session_id_filter():
    uid = "test_user"
    create_session(uid, title="A", session_id="a")
    create_session(uid, title="B", session_id="b")
    add_message(uid, "a", "user", "A 会话内容")
    add_message(uid, "b", "user", "B 会话内容")
    result = session_history_search.invoke({"query": "内容", "user_id": uid, "session_id": "a"})
    assert "A 会话内容" in result
    assert "B 会话内容" not in result


def test_empty_query_returns_recent_sessions():
    uid = "test_user"
    create_session(uid, title="最近", session_id="r1")
    add_message(uid, "r1", "user", "最近消息")
    result = session_history_search.invoke({"query": "", "user_id": uid, "limit": 10})
    assert "最近消息" in result


def test_no_match_returns_friendly_message():
    uid = "test_user"
    result = session_history_search.invoke({"query": "不存在的关键字xyz", "user_id": uid})
    assert "未找到匹配" in result
