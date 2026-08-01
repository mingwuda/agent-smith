"""长期记忆高优先级优化测试：元数据 / TTL / 相关性搜索 / 覆盖提示。

只覆盖不依赖网络/真实 LLM 的核心逻辑（ponytail：非平凡逻辑留一个可跑的 check）。
"""
import json
import sys
import tempfile
import time
from pathlib import Path

# 与 agent_core/main.py 一致的 sys.path 注入，保证能 import 到 user_manager 等内部模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_core"))

from agent_core.memory.local_memory import LocalMemory  # noqa: E402


def _make_memory():
    tmp = tempfile.mkdtemp(prefix="mem_test_")
    return LocalMemory(Path(tmp)), Path(tmp)


def test_meta_created_updated():
    mem, _ = _make_memory()
    t0 = time.time()
    mem.set("project.name", "desktop-agent")
    items = mem.list_items()
    assert items[0]["key"] == "project.name"
    assert items[0]["value"] == "desktop-agent"
    assert items[0]["created_at"] is not None
    assert items[0]["updated_at"] is not None
    assert abs(items[0]["created_at"] - t0) < 5
    # 覆盖更新后 updated_at 变化、created_at 保持
    old_created = items[0]["created_at"]
    time.sleep(0.02)
    mem.set("project.name", "desktop-agent-v2")
    items = mem.list_items()
    assert items[0]["value"] == "desktop-agent-v2"
    assert items[0]["created_at"] == old_created
    assert items[0]["updated_at"] >= old_created


def test_ttl_expires():
    mem, data_dir = _make_memory()
    mem.set("temp.key", "临时", ttl=1)
    assert mem.get("temp.key") == "临时"
    time.sleep(1.2)
    assert mem.get("temp.key") is None          # 过期后读不到
    assert "temp.key" not in mem.list_keys()    # 列表也排除
    assert not (data_dir / "temp.key.json").exists()  # 磁盘文件被清理


def test_ttl_zero_means_forever():
    mem, _ = _make_memory()
    mem.set("perm.key", "永久", ttl=0)
    time.sleep(0.1)
    assert mem.get("perm.key") == "永久"


def test_set_returns_distinct_messages():
    mem, _ = _make_memory()
    assert "已记忆" in mem.set("k1", "v1")               # 新建
    assert "已覆盖更新" in mem.set("k1", "v2")            # 覆盖
    assert "已存在且内容未变化" in mem.set("k1", "v2")    # 相同


def test_search_relevance_ranking():
    mem, _ = _make_memory()
    mem.set("deploy.host", "192.168.1.10")
    mem.set("deploy.script", "sh deploy.sh")
    mem.set("other.note", "服务器在 192.168.1.10")
    # key 精确匹配 "deploy.host" 应排在第一位（100 分 vs 前缀 60 分 vs value 命中 30 分）
    out = mem.search("deploy.host")
    lines = [ln.strip() for ln in out.splitlines()]
    assert lines[0].startswith("deploy.host:")
    # 多词搜索：key 与 value 都命中的排前面（deploy.script 的 value 含 deploy.sh）
    out2 = mem.search("deploy")
    lines2 = [ln.strip() for ln in out2.splitlines()]
    assert lines2[0].startswith("deploy.script:")
    assert lines2[1].startswith("deploy.host:")


def test_search_no_result():
    mem, _ = _make_memory()
    mem.set("only.key", "value")
    assert "未找到" in mem.search("不存在的内容")


def test_legacy_format_compat():
    """旧格式（裸值，无包装）应能正常读取"""
    mem, data_dir = _make_memory()
    # 手动写入旧格式文件
    (data_dir / "legacy.key.json").write_text(json.dumps("旧值"), encoding="utf-8")
    mem2 = LocalMemory(data_dir)
    assert mem2.get("legacy.key") == "旧值"
    items = mem2.list_items()
    assert items[0]["key"] == "legacy.key"
    assert items[0]["updated_at"] is not None  # mtime 兜底


def test_roundtrip_persistence():
    """新格式写入后，重新加载实例应读到相同值 + 元数据"""
    mem, data_dir = _make_memory()
    mem.set("round.key", {"t": "pitfall", "v": "不要重复解压"})
    mem2 = LocalMemory(data_dir)
    assert mem2.get("round.key") == {"t": "pitfall", "v": "不要重复解压"}
    assert mem2.list_items()[0]["created_at"] is not None
