"""本地记忆模块 —— 简单的键值存储"""
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


class LocalMemory:
    """基于文件的键值记忆存储"""
    
    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = data_dir or Path.home() / ".desktop_agent" / "memory"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, Any] = {}
        self._load_all()
    
    def _load_all(self):
        """从磁盘加载所有已保存的记忆文件"""
        for f in self.data_dir.glob("*.json"):
            key = f.stem
            try:
                self._cache[key] = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
    
    def get(self, key: str, default: Any = None) -> Any:
        """获取记忆"""
        if key not in self._cache:
            return default
        return self._cache[key]
    
    def set(self, key: str, value: Any) -> str:
        """设置记忆（持久化到磁盘）"""
        self._cache[key] = self._ensure_serializable(value)
        self._save(key)
        return f"✅ 已记忆 '{key}'"
    
    def delete(self, key: str) -> str:
        """删除记忆"""
        if key in self._cache:
            del self._cache[key]
        f = self.data_dir / f"{key}.json"
        if f.exists():
            f.unlink()
        return f"✅ 已删除记忆 '{key}'"
    
    def list_keys(self) -> list[str]:
        """列出所有记忆键名"""
        return sorted(self._cache.keys())
    
    def search(self, query: str) -> str:
        """按关键字搜索记忆内容"""
        results = []
        query_lower = query.lower()
        for key, value in self._cache.items():
            if query_lower in key.lower():
                results.append(f"  {key}: {self._summarize(value)}")
                continue
            str_val = json.dumps(value, ensure_ascii=False)
            if query_lower in str_val.lower():
                results.append(f"  {key}: {self._summarize(value)}")
        return "\n".join(results) if results else f"未找到包含 '{query}' 的记忆"
    
    def _save(self, key: str):
        f = self.data_dir / f"{key}.json"
        f.write_text(json.dumps(self._cache[key], ensure_ascii=False, default=str), encoding="utf-8")
    
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


# 全局单例
_memory: Optional[LocalMemory] = None

def get_memory() -> LocalMemory:
    global _memory
    if _memory is None:
        _memory = LocalMemory()
    return _memory
