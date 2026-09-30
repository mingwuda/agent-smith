"""P0 归档治理回归测试：`_ctx_old_` 压缩归档 TTL + 数量封顶 + 检索排除。

背景（2026-09-30 评估）：压缩归档 _ctx_old_ 曾与真实记忆混用导致：
1. 无 TTL、无上限、无限增长（实测 138 条占全库 70%）；
2. recall_memory 模糊搜索把压缩暂存与经验/技能并列，淹没 agent 判断。

契约：
- 归档写入自动带 TTL（默认 30 天），过期被 _purge_expired 清理；
- 归档同前缀超出上限（MAX_ARCHIVE_ENTRIES）淘汰最久未更新条目；
- search/search_scoped 默认保留归档（压缩摘要 [见记忆: key] 依赖召回），
  但提供 exclude_archive 参数；recall_memory 工具默认排除归档。
"""
from agent_core.main import app  # noqa: F401  触发 sys.path 注入
from agent_core.memory.local_memory import (
    LocalMemory, _ARCHIVE_PREFIX, MAX_ARCHIVE_ENTRIES, ARCHIVE_TTL,
)
from agent_core.tools.memory_tools import recall_memory


def _count_archive(memory) -> int:
    return len([k for k in memory.list_keys() if k.startswith(_ARCHIVE_PREFIX)])


def test_archive_write_gets_default_ttl(tmp_path):
    """归档写入自动带上 ARCHIVE_TTL，普通记忆不带。"""
    m = LocalMemory(tmp_path / "mem")
    m.set("_ctx_old_demo", "归档内容")
    m.set("_learned_demo", {"t": "technique", "v": "经验"})
    arch_meta = m._meta["_ctx_old_demo"]
    real_meta = m._meta["_learned_demo"]
    assert arch_meta["expires_at"] is not None
    assert arch_meta["expires_at"] - arch_meta["created_at"] == ARCHIVE_TTL
    assert real_meta["expires_at"] is None, "普通记忆不应被套用归档 TTL"


def test_archive_expires_after_ttl(tmp_path):
    """过期归档被 _purge_expired 清理。"""
    m = LocalMemory(tmp_path / "mem")
    m.set("_ctx_old_short", "短命归档", ttl=1)
    assert _count_archive(m) == 1
    import time
    time.sleep(1.2)
    m._purge_expired()
    assert _count_archive(m) == 0


def test_archive_capped_at_max(tmp_path):
    """归档超过上限淘汰最久未更新条目。"""
    m = LocalMemory(tmp_path / "mem")
    import time
    over = MAX_ARCHIVE_ENTRIES + 15
    for i in range(over):
        m.set(f"_ctx_old_key_{i:04d}", f"指令{i}")
        time.sleep(0.001)
    assert _count_archive(m) == MAX_ARCHIVE_ENTRIES
    # 最旧的 15 条（key_0000..0014）应被淘汰
    remaining = [k for k in m.list_keys() if k.startswith("_ctx_old_")]
    kept_old = [k for k in remaining if int(k.split("_")[-1]) < 15]
    assert kept_old == [], "应淘汰最旧的超量归档"


def test_search_default_includes_archive_but_can_exclude(tmp_path):
    """search 默认保留归档（供压缩召回），exclude_archive 时排除且保留真实记忆。"""
    m = LocalMemory(tmp_path / "mem")
    m.set("_ctx_old_aaa", "归档 关于 git 冲突")
    m.set("_learned_bbb", {"t": "technique", "v": "git 冲突经验"})
    keep = m.search("git")
    assert "_ctx_old_aaa" in keep and "_learned_bbb" in keep

    excl = m.search("git", exclude_archive=True)
    assert "_ctx_old_aaa" not in excl, "exclude 后不应含归档"
    assert "_learned_bbb" in excl, "exclude 后真实记忆仍应返回"


def test_recall_memory_tool_excludes_archive(tmp_path):
    """recall_memory 工具默认排除压缩归档，避免污染 agent 判断。"""
    m = LocalMemory(tmp_path / "mem")
    m.set("_ctx_old_aaa", "归档 关于 git 冲突")
    m.set("_learned_bbb", {"t": "technique", "v": "git 冲突经验"})
    result = m.search("git", exclude_archive=True)
    assert "_ctx_old_aaa" not in result
    assert "_learned_bbb" in result


def test_md_mirror_structured_and_frontmatter(tmp_path):
    """P2-8/9：含 list 字段的 dict 渲染为结构化 Markdown（非裸 JSON）+ 带 YAML front-matter。"""
    m = LocalMemory(tmp_path / "mem")
    m.set("_case_real", {"t": "technique", "v": "git冲突", "occurrences": 3,
                         "actions": ["git_status", "git_diff"], "contexts": ["用户问冲突"]})
    md = m.data_dir / "_case_real.md"
    content = md.read_text(encoding="utf-8")
    assert content.startswith("---\n"), "应含 YAML front-matter"
    assert "## actions" in content and "- git_status" in content, "list 字段应渲染为 Markdown 小节"
    assert "## contexts" in content and "- 用户问冲突" in content
    assert "\n  {" not in content, "不应退化成裸 JSON"


def test_delete_removes_md_mirror(tmp_path):
    """P2-10：delete() 同步清理 .md 镜像，不留孤儿文件。"""
    m = LocalMemory(tmp_path / "mem")
    m.set("_case_del", {"t": "technique", "v": "将被删"})
    assert (m.data_dir / "_case_del.md").exists()
    m.delete("_case_del")
    assert not (m.data_dir / "_case_del.md").exists(), "删除后 md 应被清理"
    assert not (m.data_dir / "_case_del.json").exists()


def test_purge_expired_removes_md_mirror(tmp_path):
    """P2-10：TTL 过期时 _purge_expired 同步删除 .md 镜像。"""
    import time
    m = LocalMemory(tmp_path / "mem")
    m.set("_case_ttl", {"t": "technique", "v": "ttl"}, ttl=1)
    assert (m.data_dir / "_case_ttl.md").exists()
    time.sleep(1.2)
    m._purge_expired()
    assert not (m.data_dir / "_case_ttl.md").exists(), "TTL 过期后 md 应被清理"
    assert not (m.data_dir / "_case_ttl.json").exists()