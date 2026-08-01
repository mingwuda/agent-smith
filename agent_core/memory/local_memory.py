"""本地记忆模块 —— 键值存储（带元数据、可选 TTL、相关性搜索）

磁盘格式（新）：{"__value__": <原始值>, "__created_at__": ts, "__updated_at__": ts, "__expires_at__": ts|None}
旧格式（兼容）：文件内容即原始值（str / dict），无元数据，按文件 mtime 兜底。
搜索：>=3 字符查询走 FTS5（BM25 排序，抗高频词），短查询/异常回退线性扫描打分。
"""
from contextvars import ContextVar
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import user_manager
from memory.fts_index import FtsIndex


LEGACY_MEMORY_DIR = Path.home() / ".desktop_agent" / "memory"
_current_user: ContextVar[str] = ContextVar("memory_current_user", default="default")

# 新格式包装键（用双下划线避免与用户自定义 dict 值冲突）
_K_VALUE = "__value__"
_K_CREATED = "__created_at__"
_K_UPDATED = "__updated_at__"
_K_EXPIRES = "__expires_at__"


class LocalMemory:
    """基于文件的键值记忆存储"""

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = data_dir or user_manager.memory_dir("default")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, Any] = {}
        self._meta: dict[str, dict] = {}  # key -> {created_at, updated_at, expires_at}
        self._fts: Optional[FtsIndex] = None
        self._fts_dirty = True  # 初始索引未建，视为脏；set/delete/expire 后置脏
        self._load_all()
        self._purge_expired()

    # ---------- 加载 ----------

    def _load_all(self):
        """从磁盘加载所有已保存的记忆文件（兼容新/旧两种格式）"""
        for f in self.data_dir.glob("*.json"):
            key = f.stem
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(raw, dict) and _K_VALUE in raw:
                # 新格式：带元数据
                self._cache[key] = raw[_K_VALUE]
                self._meta[key] = {
                    "created_at": raw.get(_K_CREATED),
                    "updated_at": raw.get(_K_UPDATED),
                    "expires_at": raw.get(_K_EXPIRES),
                }
            else:
                # 旧格式：值即内容，元数据用文件 mtime 兜底
                self._cache[key] = raw
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    mtime = time.time()
                self._meta[key] = {
                    "created_at": mtime,
                    "updated_at": mtime,
                    "expires_at": None,
                }

    def _purge_expired(self):
        """清理已过期的记忆（惰性：加载时/读写前调用）"""
        now = time.time()
        removed = False
        for key, meta in list(self._meta.items()):
            exp = meta.get("expires_at")
            if exp is not None and exp <= now:
                self._cache.pop(key, None)
                self._meta.pop(key, None)
                f = self.data_dir / f"{key}.json"
                if f.exists():
                    try:
                        f.unlink()
                    except OSError:
                        pass
                removed = True
        if removed:
            self._fts_dirty = True

    # ---------- 读写 ----------

    def get(self, key: str, default: Any = None) -> Any:
        """获取记忆"""
        self._purge_expired()
        if key not in self._cache:
            return default
        return self._cache[key]

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> str:
        """设置记忆（持久化到磁盘）。

        ttl: 可选，有效期秒数；None/0 表示永不过期。
        返回文案区分：新建 / 覆盖更新 / 值未变化。
        """
        self._purge_expired()
        now = time.time()
        serialized = self._ensure_serializable(value)
        existed = key in self._cache
        same = existed and self._cache[key] == serialized
        self._cache[key] = serialized
        prev = self._meta.get(key, {})
        self._meta[key] = {
            "created_at": prev.get("created_at") or now,
            "updated_at": now,
            "expires_at": (now + ttl) if ttl else None,
        }
        if not same:
            self._fts_dirty = True
        self._save(key)
        if same:
            return f"ℹ️ '{key}' 已存在且内容未变化"
        if existed:
            return f"🔄 已覆盖更新记忆 '{key}'"
        return f"✅ 已记忆 '{key}'"

    def delete(self, key: str) -> str:
        """删除记忆"""
        self._purge_expired()
        if key in self._cache:
            del self._cache[key]
            self._fts_dirty = True
        self._meta.pop(key, None)
        f = self.data_dir / f"{key}.json"
        if f.exists():
            f.unlink()
        return f"✅ 已删除记忆 '{key}'"

    def list_keys(self) -> list[str]:
        """列出所有记忆键名"""
        self._purge_expired()
        return sorted(self._cache.keys())

    def list_items(self) -> list[dict]:
        """列出所有记忆条目（含元数据）"""
        self._purge_expired()
        return [
            {
                "key": key,
                "value": self._cache[key],
                "summary": self._summarize(self._cache[key]),
                "created_at": self._meta[key].get("created_at"),
                "updated_at": self._meta[key].get("updated_at"),
            }
            for key in self.list_keys()
        ]

    # ---------- 搜索 ----------

    def search(self, query: str) -> str:
        """搜索记忆，返回按相关性排序的文本结果。

        双轨制：
        - 查询 >= 3 字符 → FTS5 索引检索（BM25 排序，抗高频词），无结果回退线性扫描
        - 查询 < 3 字符 / FTS5 异常 → 线性扫描打分排序（key 命中优先）
        返回格式统一为文本行 "  key: summary"，与旧版一致，调用方零改动。
        """
        self._purge_expired()
        q = query.strip().lower()
        if not q:
            return "\n".join(f"  {k}: {self._summarize(v)}" for k, v in self.list_items()) or "暂无长期记忆"
        # trigram tokenizer 硬限制：查询至少 3 个字符，否则 MATCH 报错/空结果
        if len(q) >= 3:
            try:
                keys = self._fts_search(q)
                if keys:
                    return "\n".join(
                        f"  {k}: {self._summarize(self._cache[k])}" for k in keys
                    )
            except Exception:
                pass  # FTS 异常（特殊字符等）→ 回退线性扫描
        return self._linear_search(q)

    def _fts_search(self, q: str) -> list[str]:
        """FTS5 BM25 检索，返回按相关性排序的 key 列表。"""
        if self._fts is None:
            self._fts = FtsIndex(self.data_dir / ".fts_index.sqlite3")
        if self._fts_dirty:
            self._fts.rebuild(self._cache.items())
            self._fts_dirty = False
        # limit=50：BM25 排序后截断，避免海量命中刷屏（线性扫描路径仍返回全部，
        # 但 FTS5 路径对 LLM 调用更友好 —— top-50 已覆盖最相关结果）
        rows = self._fts.search(FtsIndex.escape_match(q))
        return [key for key, _, _ in rows]

    def _linear_search(self, q: str) -> str:
        """线性扫描 + 相关性打分（key 完全匹配 > 前缀 > 包含 > value 包含）。"""
        results = []
        terms = [t for t in q.split() if t]
        for key, value in self._cache.items():
            key_l = key.lower()
            str_val = json.dumps(value, ensure_ascii=False).lower()
            score = 0
            if q == key_l:
                score += 100
            elif key_l.startswith(q):
                score += 60
            elif q in key_l:
                score += 40
            if q in str_val:
                score += 30
            # 多词：每个词命中加分；value 中命中权重低于 key
            for t in terms:
                if t in key_l:
                    score += 20
                elif t in str_val:
                    score += 10
            if score > 0:
                results.append((score, key, self._summarize(value)))
        results.sort(key=lambda x: (-x[0], x[1]))
        if not results:
            return f"未找到包含 '{q}' 的记忆"
        return "\n".join(f"  {k}: {v}" for _, k, v in results)

    # ---------- 内部 ----------

    def _save(self, key: str):
        meta = self._meta.get(key, {})
        payload = {
            _K_VALUE: self._cache[key],
            _K_CREATED: meta.get("created_at"),
            _K_UPDATED: meta.get("updated_at"),
            _K_EXPIRES: meta.get("expires_at"),
        }
        f = self.data_dir / f"{key}.json"
        f.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")

    @staticmethod
    def _ensure_serializable(value: Any) -> Any:
        if isinstance(value, (str, int, float, bool, list, dict)):
            return value
        if isinstance(value, datetime):
            return value.isoformat()
        try:
            json.dumps(value)
            return value
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _summarize(value: Any, max_len: int = 80) -> str:
        s = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        return s[:max_len] + ("..." if len(s) > max_len else "")


def set_current_user(user_id: str):
    """设置当前上下文中的记忆用户。供 Agent 工具调用时使用。"""
    _current_user.set(user_id or "default")


def get_current_user() -> str:
    return _current_user.get() or "default"


# 每个用户一个记忆实例，避免跨用户共享缓存。
_memories: dict[str, LocalMemory] = {}


def get_memory(user_id: Optional[str] = None) -> LocalMemory:
    uid = user_id or get_current_user()
    if uid not in _memories:
        data_dir = user_manager.memory_dir(uid)
        _migrate_legacy_memory(data_dir)
        _memories[uid] = LocalMemory(data_dir)
    return _memories[uid]


def _migrate_legacy_memory(target_dir: Path):
    """Copy old single-user memory files into the current user's memory once."""
    marker = target_dir / ".legacy_migrated"
    if marker.exists() or not LEGACY_MEMORY_DIR.exists():
        return
    target_dir.mkdir(parents=True, exist_ok=True)
    for legacy_file in LEGACY_MEMORY_DIR.glob("*.json"):
        target = target_dir / legacy_file.name
        if target.exists():
            continue
        try:
            target.write_text(legacy_file.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass
    try:
        marker.write_text("ok", encoding="utf-8")
    except OSError:
        pass
