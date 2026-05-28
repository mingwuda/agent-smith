#!/usr/bin/env python3
"""迁移旧版全局数据到指定用户隔离目录

用法:
  python3 migrate_legacy_data.py <user_id>

示例:
  python3 migrate_legacy_data.py admin   # 迁移到 admin 用户
  python3 migrate_legacy_data.py test    # 迁移到 test 用户
"""
import json
import sqlite3
import sys
from pathlib import Path
from contextlib import contextmanager

DATA_DIR = Path.home() / ".desktop_agent"
OLD_DB = DATA_DIR / "sessions.sqlite3"
OLD_USAGE_DIR = DATA_DIR / "usage"
OLD_MEMORY_DIR = DATA_DIR / "memory"


def user_session_db(user_id: str) -> Path:
    return DATA_DIR / "users" / user_id / "sessions" / "sessions.sqlite3"


def user_usage_db(user_id: str) -> Path:
    return DATA_DIR / "users" / user_id / "usage" / "usage.sqlite3"


def user_memory_dir(user_id: str) -> Path:
    return DATA_DIR / "users" / user_id / "memory"


@contextmanager
def connect(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def migrate_sessions(user_id: str) -> int:
    """迁移会话数据，返回迁移的消息条数"""
    if not OLD_DB.exists():
        print(f"  ⚠️  未找到旧会话数据库: {OLD_DB}")
        return 0

    with connect(OLD_DB) as old, connect(user_session_db(user_id)) as new:
        # 确保新表存在
        new.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, title TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
        """)
        new.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL, role TEXT NOT NULL,
                content TEXT NOT NULL, timestamp TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            )
        """)
        new.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id, id)"
        )

        rows = old.execute(
            "SELECT id, title, created_at, updated_at FROM sessions"
        ).fetchall()
        migrated_msgs = 0

        for row in rows:
            sid = row["id"]
            exists = new.execute(
                "SELECT id FROM sessions WHERE id = ?", (sid,)
            ).fetchone()
            if exists:
                print(f"  ⏭️  跳过已存在的会话: {row['title']} ({sid})")
                continue

            new.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (sid, row["title"], row["created_at"], row["updated_at"]),
            )
            msgs = old.execute(
                "SELECT role, content, timestamp FROM messages WHERE session_id = ? ORDER BY id ASC",
                (sid,),
            ).fetchall()
            for m in msgs:
                new.execute(
                    "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                    (sid, m["role"], m["content"], m["timestamp"]),
                )
            migrated_msgs += len(msgs)
            print(f"  ✅ 迁移会话: {row['title']} ({sid}) - {len(msgs)} 条消息")

    return migrated_msgs


def migrate_usage(user_id: str) -> int:
    """迁移用量数据，返回迁移的记录条数"""
    migrated = 0
    dest = user_usage_db(user_id)
    dest.parent.mkdir(parents=True, exist_ok=True)

    with connect(dest) as new:
        # 确保新表存在（匹配 usage_tracker.py 的 schema）
        new.execute("""
            CREATE TABLE IF NOT EXISTS usage_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                date TEXT NOT NULL,
                kind TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                tool TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                thread_id TEXT,
                process_session_id TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                cost REAL NOT NULL DEFAULT 0,
                tool_calls INTEGER NOT NULL DEFAULT 0,
                estimated INTEGER NOT NULL DEFAULT 0
            )
        """)
        new.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_date ON usage_records(date)"
        )

        # 1) 从旧 SQLite 的 usage_records 表迁移
        if OLD_DB.exists():
            try:
                with connect(OLD_DB) as old:
                    has_usage = old.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='usage_records'"
                    ).fetchone()
                    if has_usage:
                        rows = old.execute(
                            "SELECT * FROM usage_records"
                        ).fetchall()
                        for r in rows:
                            try:
                                new.execute(
                                    """INSERT OR REPLACE INTO usage_records
                                    (id, timestamp, date, kind, provider, model, tool, source,
                                     thread_id, process_session_id, input_tokens, output_tokens,
                                     cached_input_tokens, total_tokens, cost, tool_calls, estimated)
                                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                    (
                                        r["id"], r["timestamp"], r["date"],
                                        r["kind"], r["provider"], r["model"],
                                        r["tool"], r["source"],
                                        r["thread_id"], r["process_session_id"],
                                        r["input_tokens"], r["output_tokens"],
                                        r["cached_input_tokens"], r["total_tokens"],
                                        r["cost"], r["tool_calls"], r["estimated"],
                                    ),
                                )
                                migrated += 1
                            except Exception as e:
                                print(f"     ⚠️  跳过记录 #{r['id']}: {e}")
                        print(f"  ✅ 迁移用量记录: {len(rows)} 条 (来自旧 SQLite)")
            except Exception as e:
                print(f"  ⚠️  从旧 SQLite 迁移用量时出错: {e}")

        # 2) 从 legacy JSONL 文件迁移
        if OLD_USAGE_DIR.exists():
            for fpath in sorted(OLD_USAGE_DIR.glob("*.jsonl")):
                try:
                    date_str = fpath.stem
                    lines = fpath.read_text(encoding="utf-8").strip().splitlines()
                    count = 0
                    for line in lines:
                        if not line.strip():
                            continue
                        d = json.loads(line)
                        new.execute(
                            """INSERT INTO usage_records
                            (timestamp, date, kind, provider, model, tool, source,
                             process_session_id,
                             input_tokens, output_tokens, cached_input_tokens,
                             total_tokens, cost, tool_calls, estimated)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                d.get("timestamp", ""),
                                date_str,
                                "model",
                                "unknown",
                                d.get("model", "unknown"),
                                d.get("tool", ""),
                                "",
                                d.get("session_id", ""),
                                d.get("input_tokens", 0),
                                d.get("output_tokens", 0),
                                d.get("cached_input_tokens", 0),
                                d.get("total_tokens", 0),
                                d.get("cost", 0.0),
                                0,
                                1,  # estimated
                            ),
                        )
                        count += 1
                        migrated += 1
                    print(f"  ✅ 迁移 JSONL 用量: {fpath.name} - {count} 条")
                except Exception as e:
                    print(f"  ⚠️  迁移 {fpath.name} 失败: {e}")

    return migrated


def migrate_memory(user_id: str) -> int:
    """迁移旧版全局长期记忆文件，返回迁移条数"""
    if not OLD_MEMORY_DIR.exists():
        print(f"  ⚠️  未找到旧记忆目录: {OLD_MEMORY_DIR}")
        return 0

    dest = user_memory_dir(user_id)
    dest.mkdir(parents=True, exist_ok=True)
    migrated = 0
    for src in sorted(OLD_MEMORY_DIR.glob("*.json")):
        target = dest / src.name
        if target.exists():
            print(f"  ⏭️  跳过已存在的记忆: {src.name}")
            continue
        target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        migrated += 1
        print(f"  ✅ 迁移记忆: {src.name}")
    return migrated


def main():
    if len(sys.argv) < 2:
        print("用法: python3 migrate_legacy_data.py <user_id>")
        print("示例: python3 migrate_legacy_data.py admin")
        sys.exit(1)

    user_id = sys.argv[1]
    print(f"🔍 正在迁移旧版数据到用户 «{user_id}» ...\n")

    msgs = migrate_sessions(user_id)
    print(f"📝 会话迁移完成: {msgs} 条消息\n")

    usage = migrate_usage(user_id)
    print(f"📊 用量迁移完成: {usage} 条记录\n")

    memory = migrate_memory(user_id)
    print(f"🧠 记忆迁移完成: {memory} 条\n")

    print("✅ 迁移完成！重启服务后即可查看历史数据。")


if __name__ == "__main__":
    main()
