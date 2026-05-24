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
    
    def record(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
        cost: float = 0.0,
        tool_name: Optional[str] = None,
    ):
        """记录一次模型调用"""
        now = datetime.now()
        record = {
            "timestamp": now.isoformat(),
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": cached_input_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cost": cost,
            "tool": tool_name or "chat",
            "session_id": self._session_id,
        }
        self._session_records.append(record)
        
        # 追加写入今日文件
        today_file = self.data_dir / f"{date.today().isoformat()}.jsonl"
        with open(today_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    
    def get_today_stats(self) -> dict:
        """获取今日用量统计"""
        today_file = self.data_dir / f"{date.today().isoformat()}.jsonl"
        if not today_file.exists():
            return self._empty_stats()
        
        total_input = 0
        total_output = 0
        total_cached = 0
        total_cost = 0.0
        call_count = 0
        model_breakdown = {}
        
        with open(today_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                total_input += r.get("input_tokens", 0)
                total_output += r.get("output_tokens", 0)
                total_cached += r.get("cached_input_tokens", 0)
                total_cost += r.get("cost", 0)
                call_count += 1
                
                model_name = r.get("model", "unknown")
                if model_name not in model_breakdown:
                    model_breakdown[model_name] = {"calls": 0, "tokens": 0, "cost": 0.0}
                model_breakdown[model_name]["calls"] += 1
                model_breakdown[model_name]["tokens"] += r.get("total_tokens", 0)
                model_breakdown[model_name]["cost"] += r.get("cost", 0)
        
        return {
            "date": date.today().isoformat(),
            "total_calls": call_count,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_cached_tokens": total_cached,
            "total_tokens": total_input + total_output,
            "total_cost": round(total_cost, 6),
            "model_breakdown": model_breakdown,
            "session_records": len(self._session_records),
        }
    
    def get_session_stats(self) -> dict:
        """获取当前会话用量"""
        total_input = sum(r.get("input_tokens", 0) for r in self._session_records)
        total_output = sum(r.get("output_tokens", 0) for r in self._session_records)
        total_cost = sum(r.get("cost", 0) for r in self._session_records)
        return {
            "session_id": self._session_id,
            "calls": len(self._session_records),
            "input_tokens": total_input,
            "output_tokens": total_output,
            "total_tokens": total_input + total_output,
            "cost": round(total_cost, 6),
        }
    
    def get_history(self, days: int = 7) -> list[dict]:
        """获取最近 N 天的使用历史"""
        from datetime import timedelta
        
        results = []
        for i in range(days):
            d = date.today() - timedelta(days=i)
            f = self.data_dir / f"{d.isoformat()}.jsonl"
            if f.exists():
                daily_total = 0
                daily_cost = 0.0
                with open(f, encoding="utf-8") as fh:
                    for line in fh:
                        if line.strip():
                            try:
                                r = json.loads(line)
                                daily_total += r.get("total_tokens", 0)
                                daily_cost += r.get("cost", 0)
                            except json.JSONDecodeError:
                                pass
                results.append({
                    "date": d.isoformat(),
                    "total_tokens": daily_total,
                    "cost": round(daily_cost, 6),
                })
        return results
    
    @staticmethod
    def _empty_stats() -> dict:
        return {
            "date": date.today().isoformat(),
            "total_calls": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cached_tokens": 0,
            "total_tokens": 0,
            "total_cost": 0.0,
            "model_breakdown": {},
            "session_records": 0,
        }


# 全局单例
_tracker: Optional[UsageTracker] = None


def get_tracker() -> UsageTracker:
    global _tracker
    if _tracker is None:
        _tracker = UsageTracker()
    return _tracker
