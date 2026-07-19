"""用量追踪器 —— 按用户隔离的 SQLite 持久化"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import user_manager

DATA_DIR = Path.home() / ".desktop_agent"
LEGACY_USAGE_DIR = DATA_DIR / "usage"
LEGACY_DB = DATA_DIR / "sessions.sqlite3"
MIGRATION_KEY = "usage_jsonl_migrated"


class UsageTracker:
    """追踪 LLM token 消耗、费用和工具调用次数。每个用户独立数据库。"""

    def __init__(self, user_id: str = "default"):
        udir = user_manager.usage_dir(user_id)
        self.db_path = udir / "usage.sqlite3"
        self.legacy_dir = LEGACY_USAGE_DIR
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._init_db()

    @contextmanager
    def _connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # WAL + 忙等待：避免流式循环里的同步写入因锁竞争阻塞事件循环或抛 "database is locked"
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            self._init_db(conn)
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self, conn: Optional[sqlite3.Connection] = None):
        owns_conn = conn is None
        if conn is None:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            """
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
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_date ON usage_records(date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_thread ON usage_records(thread_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_process_session ON usage_records(process_session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_kind ON usage_records(kind)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self._migrate_jsonl(conn)
        if owns_conn:
            conn.commit()
            conn.close()

    def _migrate_jsonl(self, conn: sqlite3.Connection):
        migrated = conn.execute(
            "SELECT value FROM metadata WHERE key = ?", (MIGRATION_KEY,)
        ).fetchone()
        if migrated or not self.legacy_dir.exists():
            return

        for path in sorted(self.legacy_dir.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._insert_record(conn, self._normalize_record(record), migrated=True)

        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            (MIGRATION_KEY, datetime.now().isoformat()),
        )

    def record_model_call(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
        cost: float = 0.0,
        thread_id: Optional[str] = None,
        source: str = "llm",
        estimated: bool = False,
    ):
        """记录一次模型调用。只有这里的 token 会进入模型 token 统计。"""
        input_count = max(0, int(input_tokens or 0))
        output_count = max(0, int(output_tokens or 0))
        cached_count = max(0, int(cached_input_tokens or 0))
        now = datetime.now()
        record = {
            "timestamp": now.isoformat(),
            "date": now.date().isoformat(),
            "kind": "model",
            "provider": provider or "unknown",
            "model": model or "unknown",
            "tool": "",
            "source": source or "llm",
            "thread_id": thread_id,
            "process_session_id": self._session_id,
            "input_tokens": input_count,
            "output_tokens": output_count,
            "cached_input_tokens": cached_count,
            "total_tokens": input_count + output_count,
            "cost": float(cost or 0.0),
            "tool_calls": 0,
            "estimated": bool(estimated),
        }
        self._write(record)

    def record_tool_call(
        self,
        tool_name: str,
        provider: str = "",
        model: str = "",
        thread_id: Optional[str] = None,
    ):
        """记录一次工具调用。工具调用不计入模型 token。"""
        now = datetime.now()
        record = {
            "timestamp": now.isoformat(),
            "date": now.date().isoformat(),
            "kind": "tool",
            "provider": provider or "unknown",
            "model": model or "",
            "tool": tool_name or "unknown",
            "source": "",
            "thread_id": thread_id,
            "process_session_id": self._session_id,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "total_tokens": 0,
            "cost": 0.0,
            "tool_calls": 1,
            "estimated": False,
        }
        self._write(record)

    def record(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
        cost: float = 0.0,
        tool_name: Optional[str] = None,
    ):
        """兼容旧调用；新代码优先使用 record_model_call / record_tool_call。"""
        self.record_model_call(
            provider="unknown",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            cost=cost,
            source=tool_name or "chat",
        )

    def _write(self, record: dict):
        with self._connect() as conn:
            self._insert_record(conn, record)

    def _insert_record(self, conn: sqlite3.Connection, record: dict, migrated: bool = False):
        timestamp = record.get("timestamp") or datetime.now().isoformat()
        day = record.get("date") or str(timestamp)[:10]
        conn.execute(
            """
            INSERT INTO usage_records (
                timestamp, date, kind, provider, model, tool, source, thread_id,
                process_session_id, input_tokens, output_tokens, cached_input_tokens,
                total_tokens, cost, tool_calls, estimated
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp,
                day,
                record.get("kind") or "model",
                record.get("provider") or "unknown",
                record.get("model") or "",
                record.get("tool") or "",
                record.get("source") or "",
                record.get("thread_id"),
                record.get("process_session_id") or record.get("session_id") or self._session_id,
                max(0, int(record.get("input_tokens") or 0)),
                max(0, int(record.get("output_tokens") or 0)),
                max(0, int(record.get("cached_input_tokens") or 0)),
                max(0, int(record.get("total_tokens") or 0)),
                float(record.get("cost") or 0.0),
                max(0, int(record.get("tool_calls") or (1 if record.get("kind") == "tool" else 0))),
                1 if record.get("estimated") else 0,
            ),
        )

    def _normalize_record(self, record: dict) -> dict:
        kind = self._kind(record)
        input_tokens = max(0, int(record.get("input_tokens") or 0)) if kind == "model" else 0
        output_tokens = max(0, int(record.get("output_tokens") or 0)) if kind == "model" else 0
        timestamp = record.get("timestamp") or datetime.now().isoformat()
        normalized = {
            "timestamp": timestamp,
            "date": str(timestamp)[:10],
            "kind": kind,
            "provider": record.get("provider") or "unknown",
            "model": record.get("model") or "",
            "tool": record.get("tool") or ("" if kind == "model" else "unknown"),
            "source": record.get("source") or record.get("tool") or "",
            "thread_id": record.get("thread_id"),
            "process_session_id": record.get("process_session_id") or record.get("session_id") or self._session_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": max(0, int(record.get("cached_input_tokens") or 0)) if kind == "model" else 0,
            "total_tokens": input_tokens + output_tokens,
            "cost": float(record.get("cost") or 0.0) if kind == "model" else 0.0,
            "tool_calls": int(record.get("tool_calls") or (1 if kind == "tool" else 0)),
            "estimated": bool(record.get("estimated")),
        }
        return normalized

    @staticmethod
    def _kind(record: dict) -> str:
        kind = record.get("kind")
        if kind in {"model", "tool"}:
            return kind
        tool_name = record.get("tool", "chat")
        if tool_name and tool_name not in {"chat", "agent_response", "user_input"}:
            return "tool"
        return "model"

    @staticmethod
    def _provider(record: dict) -> str:
        return record.get("provider") or "unknown"

    @staticmethod
    def _add_model_breakdown(bucket: dict, provider: str, model: str, record: dict):
        key = f"{provider}:{model or 'unknown'}"
        if key not in bucket:
            bucket[key] = {
                "provider": provider,
                "model": model or "unknown",
                "calls": 0,
                "tokens": 0,
                "cost": 0.0,
            }
        bucket[key]["calls"] += 1
        bucket[key]["tokens"] += record.get("total_tokens", 0)
        bucket[key]["cost"] += record.get("cost", 0)

    @staticmethod
    def _add_provider_breakdown(bucket: dict, provider: str, record: dict, kind: str):
        if provider not in bucket:
            bucket[provider] = {
                "model_calls": 0,
                "tool_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost": 0.0,
            }
        if kind == "model":
            bucket[provider]["model_calls"] += 1
            bucket[provider]["input_tokens"] += record.get("input_tokens", 0)
            bucket[provider]["output_tokens"] += record.get("output_tokens", 0)
            bucket[provider]["total_tokens"] += record.get("total_tokens", 0)
            bucket[provider]["cost"] += record.get("cost", 0)
        elif kind == "tool":
            bucket[provider]["tool_calls"] += record.get("tool_calls", 1)

    def _aggregate(self, records: list[dict]) -> dict:
        total_input = 0
        total_output = 0
        total_cached = 0
        total_cost = 0.0
        model_calls = 0
        tool_calls = 0
        provider_breakdown = {}
        model_breakdown = {}
        tool_breakdown = {}

        for r in records:
            kind = self._kind(r)
            provider = self._provider(r)
            self._add_provider_breakdown(provider_breakdown, provider, r, kind)

            if kind == "tool":
                tool_name = r.get("tool") or "unknown"
                calls = r.get("tool_calls", 1)
                tool_calls += calls
                tool_breakdown[tool_name] = tool_breakdown.get(tool_name, 0) + calls
                continue

            model_calls += 1
            total_input += r.get("input_tokens", 0)
            total_output += r.get("output_tokens", 0)
            total_cached += r.get("cached_input_tokens", 0)
            total_cost += r.get("cost", 0)
            self._add_model_breakdown(model_breakdown, provider, r.get("model", "unknown"), r)

        return {
            "total_calls": model_calls + tool_calls,
            "model_calls": model_calls,
            "tool_calls": tool_calls,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_cached_tokens": total_cached,
            "total_tokens": total_input + total_output,
            "total_cost": round(total_cost, 6),
            "provider_breakdown": provider_breakdown,
            "model_breakdown": model_breakdown,
            "tool_breakdown": tool_breakdown,
        }

    def _query_records(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        thread_id: Optional[str] = None,
        process_session_id: Optional[str] = None,
    ) -> list[dict]:
        where = []
        params = []
        if start_date:
            where.append("date >= ?")
            params.append(start_date)
        if end_date:
            where.append("date <= ?")
            params.append(end_date)
        if thread_id:
            where.append("thread_id = ?")
            params.append(thread_id)
        if process_session_id:
            where.append("process_session_id = ?")
            params.append(process_session_id)

        sql = "SELECT * FROM usage_records"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id ASC"

        with self._connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def get_today_stats(self) -> dict:
        """获取今日用量统计"""
        today = date.today().isoformat()
        records = self._query_records(start_date=today, end_date=today)
        stats = self._aggregate(records)
        stats.update({
            "date": today,
            "session_records": len(self._query_records(process_session_id=self._session_id)),
        })
        return stats

    def get_session_stats(self, thread_id: Optional[str] = None) -> dict:
        """获取当前进程或指定会话的持久化用量。"""
        records = self._query_records(thread_id=thread_id) if thread_id else self._query_records(process_session_id=self._session_id)
        stats = self._aggregate(records)
        return {
            "session_id": thread_id or self._session_id,
            "calls": stats["total_calls"],
            "input_tokens": stats["total_input_tokens"],
            "output_tokens": stats["total_output_tokens"],
            "total_tokens": stats["total_tokens"],
            "cost": stats["total_cost"],
            "model_calls": stats["model_calls"],
            "tool_calls": stats["tool_calls"],
            "provider_breakdown": stats["provider_breakdown"],
            "tool_breakdown": stats["tool_breakdown"],
        }

    def get_history(self, days: int = 7) -> list[dict]:
        """获取最近 N 天的使用历史"""
        start = date.today() - timedelta(days=max(1, days) - 1)
        records = self._query_records(start_date=start.isoformat(), end_date=date.today().isoformat())
        by_day: dict[str, list[dict]] = {}
        for record in records:
            by_day.setdefault(record.get("date") or record.get("timestamp", "")[:10], []).append(record)

        results = []
        for i in range(days):
            d = date.today() - timedelta(days=i)
            day = d.isoformat()
            if day not in by_day:
                continue
            stats = self._aggregate(by_day[day])
            results.append({
                "date": day,
                "total_tokens": stats["total_tokens"],
                "model_calls": stats["model_calls"],
                "tool_calls": stats["tool_calls"],
                "cost": stats["total_cost"],
            })
        return results

    @staticmethod
    def _empty_stats() -> dict:
        return {
            "date": date.today().isoformat(),
            "total_calls": 0,
            "model_calls": 0,
            "tool_calls": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cached_tokens": 0,
            "total_tokens": 0,
            "total_cost": 0.0,
            "provider_breakdown": {},
            "model_breakdown": {},
            "tool_breakdown": {},
            "session_records": 0,
        }


_trackers: dict[str, UsageTracker] = {}


def get_tracker(user_id: str = "default") -> UsageTracker:
    """获取指定用户的用量追踪器（每个用户独立）"""
    if user_id not in _trackers:
        _trackers[user_id] = UsageTracker(user_id)
    return _trackers[user_id]
