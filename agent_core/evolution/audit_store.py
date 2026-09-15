"""进化审计存储 —— 跨用户、进程级、时序可查的进化/自愈事件审计层。

设计依据 DESIGN.md §4.7：
- 巡检（patrol）、用户反馈（feedback）、守护回退（guardian）、技能生成（skill_gen）
  等所有"进化相关"动作在发生时经单一写入点 audit.log(...) 落一条记录。
- 与 per-user 的 LocalMemory 不同：巡检/自愈是进程级 admin 视角，故审计库放在
  ~/.desktop_agent/evolution_audit.sqlite3（与 sessions.sqlite3 同级），不按用户隔离。
- 纯 stdlib sqlite3，WAL + busy_timeout，范式复用 monitoring/usage_tracker.py。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

DATA_DIR = Path.home() / ".desktop_agent"

# 合法取值（与 DESIGN §4.7.2 对齐）
SOURCES = {"patrol", "feedback", "guardian", "skill_gen", "manual"}
CATEGORIES = {"pitfall", "fix", "config_change", "skill_change",
              "quarantine", "escalation", "feedback"}
SEVERITIES = {"info", "warn", "error", "fatal"}
OUTCOMES = {"auto_fixed", "escalated", "auto_reverted", "pending", "approved", "ignored"}

# POST /action 允许的状态流转目标
ACTION_OUTCOMES = {"approve": "approved", "ignore": "ignored", "revert": "auto_reverted"}


class EvolutionAuditStore:
    """进化审计事件的单一写入/查询点。"""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else DATA_DIR / "evolution_audit.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS evolution_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    source TEXT NOT NULL,
                    category TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'info',
                    summary TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '{}',
                    artifacts TEXT NOT NULL DEFAULT '[]',
                    outcome TEXT NOT NULL DEFAULT 'pending',
                    actor TEXT NOT NULL DEFAULT 'auto'
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON evolution_audit(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_source ON evolution_audit(source)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_category ON evolution_audit(category)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_outcome ON evolution_audit(outcome)")

    # ---------- 写入 ----------

    def log(
        self,
        *,
        source: str,
        category: str,
        summary: str,
        severity: str = "info",
        detail: Optional[dict] = None,
        artifacts: Optional[list] = None,
        outcome: str = "pending",
        actor: str = "auto",
    ) -> int:
        """写一条审计记录，返回自增 id。非法枚举直接拒绝（信任边界校验）。"""
        if source not in SOURCES:
            raise ValueError(f"非法 source: {source}")
        if category not in CATEGORIES:
            raise ValueError(f"非法 category: {category}")
        if severity not in SEVERITIES:
            raise ValueError(f"非法 severity: {severity}")
        if outcome not in OUTCOMES:
            raise ValueError(f"非法 outcome: {outcome}")
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO evolution_audit
                   (ts, source, category, severity, summary, detail, artifacts, outcome, actor)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now().isoformat(timespec="seconds"),
                    source, category, severity, summary,
                    json.dumps(detail or {}, ensure_ascii=False),
                    json.dumps(artifacts or [], ensure_ascii=False),
                    outcome, actor,
                ),
            )
            return int(cur.lastrowid)

    # ---------- 查询 ----------

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "ts": row["ts"],
            "source": row["source"],
            "category": row["category"],
            "severity": row["severity"],
            "summary": row["summary"],
            "detail": json.loads(row["detail"] or "{}"),
            "artifacts": json.loads(row["artifacts"] or "[]"),
            "outcome": row["outcome"],
            "actor": row["actor"],
        }

    def list_audit(
        self,
        *,
        source: Optional[str] = None,
        category: Optional[str] = None,
        severity: Optional[str] = None,
        outcome: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """时序列表（新→旧），支持来源/分类/严重度/结果过滤与分页。"""
        where, params = [], []
        for col, val in (("source", source), ("category", category),
                         ("severity", severity), ("outcome", outcome)):
            if val:
                where.append(f"{col} = ?")
                params.append(val)
        if since:
            where.append("ts >= ?")
            params.append(since)
        sql = "SELECT * FROM evolution_audit"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([max(1, min(int(limit), 200)), max(0, int(offset))])
        with self._connect() as conn:
            return [self._row_to_dict(r) for r in conn.execute(sql, params)]

    def get_one(self, audit_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM evolution_audit WHERE id = ?", (audit_id,)).fetchone()
            return self._row_to_dict(row) if row else None

    def set_outcome(self, audit_id: int, outcome: str, *, actor: str = "admin",
                    note: str = "") -> bool:
        """流转处理状态（approve/ignore/revert）。返回是否更新到行。"""
        if outcome not in OUTCOMES:
            raise ValueError(f"非法 outcome: {outcome}")
        with self._connect() as conn:
            row = conn.execute("SELECT detail FROM evolution_audit WHERE id = ?",
                               (audit_id,)).fetchone()
            if row is None:
                return False
            cur = conn.execute(
                "UPDATE evolution_audit SET outcome = ?, actor = ? WHERE id = ?",
                (outcome, actor, audit_id),
            )
            # ponytail: P1 只做状态流转+备注；revert 的实际文件恢复待 P3
            # （generated 技能/配置补丁产物出现后才有可回退对象），升级路径见 DESIGN §4.2。
            if note:
                try:
                    detail = json.loads(row["detail"] or "{}")
                    detail.setdefault("action_notes", []).append(
                        {"ts": datetime.now().isoformat(timespec="seconds"),
                         "actor": actor, "outcome": outcome, "note": note}
                    )
                    conn.execute("UPDATE evolution_audit SET detail = ? WHERE id = ?",
                                 (json.dumps(detail, ensure_ascii=False), audit_id))
                except (json.JSONDecodeError, OSError):
                    pass
            return cur.rowcount > 0

    def health_counts(self) -> dict:
        """管理端汇总计数（DESIGN §4.7.3 /health）。"""
        with self._connect() as conn:
            def cnt(where: str = "", *params) -> int:
                return conn.execute(
                    f"SELECT COUNT(*) FROM evolution_audit {where}", params
                ).fetchone()[0]
            day_ago = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
            return {
                "total": cnt(),
                "open_escalations": cnt("WHERE outcome = 'escalated'"),
                "pending": cnt("WHERE outcome = 'pending'"),
                "auto_fixed": cnt("WHERE outcome = 'auto_fixed'"),
                "auto_reverted": cnt("WHERE outcome = 'auto_reverted'"),
                "approved": cnt("WHERE outcome = 'approved'"),
                "pitfalls_24h": cnt(
                    "WHERE category = 'pitfall' AND ts >= ?", day_ago),
            }


_store: Optional[EvolutionAuditStore] = None


def get_audit_store() -> EvolutionAuditStore:
    """进程级单例（审计库不按用户隔离）。"""
    global _store
    if _store is None:
        _store = EvolutionAuditStore()
    return _store
