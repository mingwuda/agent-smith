"""FTS5 记忆搜索测试：中文子串 / BM25 排序 / 短词回退 / 一致性 / 特殊字符。

只覆盖不依赖网络/真实 LLM 的核心逻辑（ponytail：非平凡逻辑留一个可跑的 check）。
"""
import json
import sys
import tempfile
import time
from pathlib import Path

# 与 agent_core/main.py 一致的 sys.path 注入，保证能 import 到内部模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_core"))

from agent_core.memory.local_memory import LocalMemory  # noqa: E402
from agent_core.memory.fts_index import FtsIndex  # noqa: E402


def _make_memory():
    tmp = tempfile.mkdtemp(prefix="mem_fts_")
    return LocalMemory(Path(tmp)), Path(tmp)


def test_chinese_substring_match():
    """中文查询：trigram 子串匹配 value 内容"""
    mem, _ = _make_memory()
    mem.set("deploy.host", "服务器部署地址 192.168.1.10")
    out = mem.search("服务器部署")
    assert "deploy.host" in out


def test_key_match_via_fts():
    """>=3 字符查询走 FTS5，key 命中可检索"""
    mem, _ = _make_memory()
    mem.set("deploy.host", "192.168.1.10")
    out = mem.search("deploy.host")
    assert "deploy.host" in out


def test_bm25_tf_ordering():
    """BM25：文档内词频高的排前面（deploy.script 的 value 也含 deploy）"""
    mem, _ = _make_memory()
    mem.set("deploy.host", "192.168.1.10")
    mem.set("deploy.script", "sh deploy.sh")
    lines = [ln.strip() for ln in mem.search("deploy").splitlines()]
    assert lines[0].startswith("deploy.script:")
    assert lines[1].startswith("deploy.host:")


def test_short_query_fallback():
    """<3 字符查询回退线性扫描，不报错且命中"""
    mem, _ = _make_memory()
    mem.set("server.ip", "192.168.1.10")
    out = mem.search("ip")
    assert "server.ip" in out


def test_fts_result_set_equals_linear():
    """对 >=3 字符查询，FTS5 命中集合 == 线性扫描命中集合（不丢结果）"""
    mem, _ = _make_memory()
    mem.set("deploy.host", "192.168.1.10")
    mem.set("deploy.script", "sh deploy.sh")
    mem.set("other.note", "某服务器说明")
    fts_keys = set(mem._fts_search("deploy"))          # FTS5 路径
    linear = mem._linear_search("deploy")              # 线性路径
    linear_keys = {ln.strip().split(":", 1)[0] for ln in linear.splitlines()}
    assert fts_keys == linear_keys


def test_consistency_after_set_delete():
    """set/delete 置脏后重建，结果始终一致"""
    mem, _ = _make_memory()
    mem.set("k1", "alpha beta")
    assert "k1" in mem.search("alpha")
    mem.set("k1", "gamma delta")      # 覆盖 → 旧内容应消失、新内容可命中
    assert "k1" in mem.search("gamma")
    out = mem.search("alpha")
    assert "k1" not in out             # 旧内容 alpha 已不在索引中
    mem.delete("k1")                  # 删除 → 不应再命中
    assert "k1" not in mem.search("gamma")
    mem.set("k2", "alpha beta")       # 新增 → 可命中
    assert "k2" in mem.search("alpha")


def test_special_char_escaping():
    """含引号/括号的查询不崩溃（转义生效）"""
    mem, _ = _make_memory()
    mem.set("note.quote", '他说 "你好" 然后 (离开)')
    out = mem.search('"你好" (离开)')
    assert "note.quote" in out or "未找到" in out  # 不崩溃即可，命中与否都接受


def test_dict_value_indexed():
    """dict 值（如 _learned_* 经验）序列化后也可被检索"""
    mem, _ = _make_memory()
    mem.set("_learned_abc123", {"t": "technique", "v": "问题定位|先搜索定位根因"})
    out = mem.search("问题定位")
    assert "_learned_abc123" in out


def test_fts_index_file_isolated():
    """索引库文件不干扰主存储（glob 只认 *.json）"""
    mem, data_dir = _make_memory()
    mem.set("k1", "hello world")
    mem.search("hello")  # 触发建索引
    json_files = list(data_dir.glob("*.json"))
    assert len(json_files) == 1 and json_files[0].name == "k1.json"


def test_escape_match_unit():
    assert FtsIndex.escape_match("a\"b(c)") == '"a""b(c)"'
