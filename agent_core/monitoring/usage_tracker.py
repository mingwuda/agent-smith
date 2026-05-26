"""用量追踪器 —— 统计数据送给监控 API"""
import json
import os
import time
from datetime import datetime, date
from pathlib import Path
from typing import Optional


class UsageTracker:
    """追踪 LLM token 消耗和费用"""
    
    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = data_dir or Path.home() / ".desktop_agent" / "usage"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._session_records: list[dict] = []
    
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
        now = datetime.now()
        input_count = max(0, int(input_tokens or 0))
        output_count = max(0, int(output_tokens or 0))
        cached_count = max(0, int(cached_input_tokens or 0))
        record = {
            "timestamp": now.isoformat(),
            "kind": "model",
            "provider": provider or "unknown",
            "model": model,
            "input_tokens": input_count,
            "output_tokens": output_count,
            "cached_input_tokens": cached_count,
            "total_tokens": input_count + output_count,
            "cost": float(cost or 0.0),
            "source": source,
            "estimated": estimated,
            "thread_id": thread_id,
            "session_id": self._session_id,
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
            "kind": "tool",
            "provider": provider or "unknown",
            "model": model,
            "tool": tool_name or "unknown",
            "tool_calls": 1,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "total_tokens": 0,
            "cost": 0.0,
            "thread_id": thread_id,
            "session_id": self._session_id,
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
        self._session_records.append(record)

        today_file = self.data_dir / f"{date.today().isoformat()}.jsonl"
        with open(today_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    @staticmethod
    def _kind(record: dict) -> str:
        kind = record.get("kind")
        if kind in {"model", "tool"}:
            return kind

        # 兼容旧日志：旧版本把工具结果长度写进 output_tokens。
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
    
    def get_today_stats(self) -> dict:
        """获取今日用量统计"""
        today_file = self.data_dir / f"{date.today().isoformat()}.jsonl"
        if not today_file.exists():
            return self._empty_stats()
        
        records = []
        
        with open(today_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                records.append(r)

        stats = self._aggregate(records)
        stats.update({
            "date": date.today().isoformat(),
            "session_records": len(self._session_records),
        })
        return stats
    
    def get_session_stats(self, thread_id: Optional[str] = None) -> dict:
        """获取当前会话用量"""
        records = self._session_records
        if thread_id:
            records = [r for r in records if r.get("thread_id") == thread_id]

        stats = self._aggregate(records)
        return {
            "session_id": self._session_id,
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
        from datetime import timedelta
        
        results = []
        for i in range(days):
            d = date.today() - timedelta(days=i)
            f = self.data_dir / f"{d.isoformat()}.jsonl"
            if f.exists():
                records = []
                daily_cost = 0.0
                with open(f, encoding="utf-8") as fh:
                    for line in fh:
                        if line.strip():
                            try:
                                r = json.loads(line)
                                records.append(r)
                            except json.JSONDecodeError:
                                pass
                stats = self._aggregate(records)
                daily_cost = stats["total_cost"]
                results.append({
                    "date": d.isoformat(),
                    "total_tokens": stats["total_tokens"],
                    "model_calls": stats["model_calls"],
                    "tool_calls": stats["tool_calls"],
                    "cost": round(daily_cost, 6),
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


# 全局单例
_tracker: Optional[UsageTracker] = None


def get_tracker() -> UsageTracker:
    global _tracker
    if _tracker is None:
        _tracker = UsageTracker()
    return _tracker
