"""Phase 2：自进化经验条目按类封顶淘汰测试。"""
import sys
from pathlib import Path

# local_memory 内部扁平 import user_manager，需把 agent_core 注入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_core"))

from agent_core.memory.local_memory import LocalMemory, MAX_EVOLUTION_ENTRIES_PER_KIND  # noqa: E402


def _make(tmp_path):
    return LocalMemory(data_dir=tmp_path / "memory")


def test_learned_entries_capped_oldest_evicted(tmp_path):
    mem = _make(tmp_path)
    cap = MAX_EVOLUTION_ENTRIES_PER_KIND
    for i in range(cap):
        mem.set(f"_learned_k{i:03d}", {"t": "technique", "v": f"经验{i}"})
    assert len([k for k in mem.list_keys() if k.startswith("_learned_")]) == cap

    # 再写 3 条 → 最旧的 3 条（k000-k002）被淘汰
    for i in range(cap, cap + 3):
        mem.set(f"_learned_k{i:03d}", {"t": "technique", "v": f"经验{i}"})
    keys = sorted(k for k in mem.list_keys() if k.startswith("_learned_"))
    assert len(keys) == cap
    assert "_learned_k000" not in keys and "_learned_k002" not in keys
    assert "_learned_k003" in keys and f"_learned_k{cap + 2:03d}" in keys


def test_avoid_and_learned_capped_independently(tmp_path):
    mem = _make(tmp_path)
    cap = MAX_EVOLUTION_ENTRIES_PER_KIND
    for i in range(cap + 2):
        mem.set(f"_learned_a{i:03d}", {"t": "technique", "v": f"a{i}"})
        mem.set(f"_avoid_b{i:03d}", {"t": "pitfall", "v": f"b{i}"})
    learned = [k for k in mem.list_keys() if k.startswith("_learned_")]
    avoid = [k for k in mem.list_keys() if k.startswith("_avoid_")]
    assert len(learned) == cap and len(avoid) == cap  # 各自独立封顶


def test_user_memory_not_capped(tmp_path):
    """普通用户记忆不受进化上限影响。"""
    mem = _make(tmp_path)
    for i in range(MAX_EVOLUTION_ENTRIES_PER_KIND + 10):
        mem.set(f"user_fact_{i}", f"事实{i}")
    assert len(mem.list_keys()) == MAX_EVOLUTION_ENTRIES_PER_KIND + 10


def test_cap_persists_across_reload(tmp_path):
    mem = _make(tmp_path)
    cap = MAX_EVOLUTION_ENTRIES_PER_KIND
    for i in range(cap + 5):
        mem.set(f"_learned_p{i:03d}", {"t": "technique", "v": f"p{i}"})
    # 重新从磁盘加载（模拟进程重启）
    reloaded = LocalMemory(data_dir=tmp_path / "memory")
    keys = [k for k in reloaded.list_keys() if k.startswith("_learned_")]
    assert len(keys) == cap
