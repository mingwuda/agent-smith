"""FTS5 全文检索二级索引 —— 零依赖（标准库 sqlite3），trigram 分词支持中文子串匹配。

作为 LocalMemory 的搜索加速层：主存储仍是 per-key JSON 文件，
本索引只负责把 key+value 灌进 FTS5，供 search() 做 BM25 排序检索。

策略：set/delete 只置脏标记，search 时惰性全量重建（rebuild）。
当前量级（<5000 条）下 rebuild 仅几十~几百 ms，代码简单且永不失步。
（ponytail: 数据量到 5 万条+ 时再升级为增量同步 —— 届时需处理
 upsert/delete/expire 三路一致性与 commit 频率，当前不值得。）
"""
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable


def _value_to_text(value: Any) -> str:
    """把记忆值转成可索引文本（dict/list 序列化为 JSON，保证内容可检索）"""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


class FtsIndex:
    """SQLite FTS5 二级索引（trigram tokenizer，支持中文子串）"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts "
            "USING fts5(key, value, tokenize='trigram')"
        )

    def rebuild(self, items: Iterable[tuple[str, Any]]):
        """全量重建索引。items: (key, value) 可迭代对象"""
        self._conn.execute("DELETE FROM mem_fts")
        rows = [(k, _value_to_text(v)) for k, v in items]
        if rows:
            self._conn.executemany(
                "INSERT INTO mem_fts(key, value) VALUES(?,?)", rows
            )
        self._conn.commit()

    def search(self, query: str, limit: int = 50) -> list[tuple[str, str, float]]:
        """BM25 排序检索，返回 [(key, value_text, bm25_rank), ...]。

        调用方必须保证 query 已转义且长度 >= 3（trigram 的硬限制）。
        """
        cur = self._conn.execute(
            "SELECT key, value, bm25(mem_fts) AS rank "
            "FROM mem_fts WHERE mem_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (query, limit),
        )
        return cur.fetchall()

    @staticmethod
    def escape_match(text: str) -> str:
        """把查询转成安全的 FTS5 短语：双引号包裹整串，内部双引号翻倍转义。

        trigram tokenizer 下，引号包裹的短语按字符子串匹配，
        可规避 MATCH 语法中的括号/星号等特殊字符冲突。
        """
        return '"' + text.replace('"', '""') + '"'

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass
