"""会话存储 —— 使用 SQLite 持久化对话历史"""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional


DATA_DIR = Path.home() / ".desktop_agent"
SESSION_DIR = DATA_DIR / "sessions"
DB_PATH = DATA_DIR / "sessions.sqlite3"
MIGRATION_KEY = "json_sessions_migrated"


def _timestamp() -> str:
    return datetime.now().isoformat()


def _ensure_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def _connect():
    _ensure_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        _init_db(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _init_db(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id, id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    _migrate_json_sessions(conn)


def _migrate_json_sessions(conn: sqlite3.Connection):
    migrated = conn.execute(
        "SELECT value FROM metadata WHERE key = ?", (MIGRATION_KEY,)
    ).fetchone()
    if migrated or not SESSION_DIR.exists():
        return

    for meta_path in sorted(SESSION_DIR.glob("*.json")):
        if meta_path.name.endswith("_messages.json"):
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        session_id = str(meta.get("id") or meta_path.stem)
        now = _timestamp()
        created_at = str(meta.get("created_at") or now)
        updated_at = str(meta.get("updated_at") or created_at)
        title = str(meta.get("title") or f"会话 {session_id[:8]}")

        conn.execute(
            """
            INSERT OR IGNORE INTO sessions (id, title, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, title, created_at, updated_at),
        )

        msgs_path = SESSION_DIR / f"{session_id}_messages.json"
        try:
            messages = json.loads(msgs_path.read_text(encoding="utf-8")) if msgs_path.exists() else []
        except (json.JSONDecodeError, OSError):
            messages = []
        if not isinstance(messages, list):
            messages = []

        existing_count = conn.execute(
            "SELECT COUNT(*) AS count FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()["count"]
        if existing_count:
            continue

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "")
            content = str(msg.get("content") or "")
            if not role or not content:
                continue
            conn.execute(
                """
                INSERT INTO messages (session_id, role, content, timestamp)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, role, content, str(msg.get("timestamp") or updated_at)),
            )

    conn.execute(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
        (MIGRATION_KEY, _timestamp()),
    )


def _row_to_session(row: sqlite3.Row, include_messages: bool = False, messages: Optional[list[dict]] = None) -> dict:
    session = {
        "id": row["id"],
        "title": row["title"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "message_count": row["message_count"],
    }
    if include_messages:
        session["messages"] = messages or []
    return session


def create_session(title: Optional[str] = None, session_id: Optional[str] = None) -> dict:
    """创建一个新会话"""
    session_id = session_id or str(uuid.uuid4())[:8]
    now = _timestamp()
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO sessions (id, title, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, title or f"会话 {now[:16].replace('T', ' ')}", now, now),
        )
    return get_session(session_id) or {
        "id": session_id,
        "title": title or f"会话 {now[:16].replace('T', ' ')}",
        "created_at": now,
        "updated_at": now,
        "message_count": 0,
        "messages": [],
    }


def add_message(session_id: str, role: str, content: str) -> Optional[dict]:
    """追加一条消息到会话"""
    now = _timestamp()
    with _connect() as conn:
        exists = conn.execute(
            "SELECT id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not exists:
            return None
        conn.execute(
            """
            INSERT INTO messages (session_id, role, content, timestamp)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, role, content, now),
        )
        conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (now, session_id),
        )
    return get_session(session_id)


def list_sessions() -> list[dict]:
    """列出所有会话（仅元数据，不含消息列表）"""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                s.id,
                s.title,
                s.created_at,
                s.updated_at,
                COUNT(m.id) AS message_count
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.id
            GROUP BY s.id
            ORDER BY s.updated_at DESC
            """
        ).fetchall()
        return [_row_to_session(row) for row in rows]


def get_session(session_id: str) -> Optional[dict]:
    """获取单个会话（含消息）"""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT
                s.id,
                s.title,
                s.created_at,
                s.updated_at,
                COUNT(m.id) AS message_count
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.id
            WHERE s.id = ?
            GROUP BY s.id
            """,
            (session_id,),
        ).fetchone()
        if not row:
            return None
        msg_rows = conn.execute(
            """
            SELECT role, content, timestamp
            FROM messages
            WHERE session_id = ?
            ORDER BY id ASC
            """,
            (session_id,),
        ).fetchall()
        messages = [
            {"role": r["role"], "content": r["content"], "timestamp": r["timestamp"]}
            for r in msg_rows
        ]
        return _row_to_session(row, include_messages=True, messages=messages)


def delete_session(session_id: str) -> bool:
    """删除会话"""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return cur.rowcount > 0


def rename_session(session_id: str, new_title: str) -> bool:
    """重命名会话"""
    now = _timestamp()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            (new_title, now, session_id),
        )
        return cur.rowcount > 0
