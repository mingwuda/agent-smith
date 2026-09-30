"""记忆语义检索测试（P0-2）：同义词扩展召回 + min_score 相关性阈值过滤。

覆盖两项语义增强能力，均不依赖网络/真实 LLM：
- 中文近义表述可召回字面不同的英文记忆（git冲突 → merge conflict）
- min_score 过滤掉"仅子串偶然重合"的弱相关项
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent_core"))

from agent_core.memory.local_memory import LocalMemory  # noqa: E402


def _make():
    tmp = tempfile.mkdtemp(prefix="mem_semantic_")
    return LocalMemory(Path(tmp))


def test_synonym_expansion_recalls_semantically_related():
    """中文「git冲突」能召回字面不同的英文 merge conflict 记忆（语义兜底）。"""
    mem = _make()
    mem.set("exp.merge", "merge conflict 时先看两个分支的最近共同祖先")
    mem.set("exp.other", "docker 容器日志查看")
    out = mem.search("git冲突")
    assert "exp.merge" in out, "同义词扩展应召回 merge conflict 记忆"


def test_min_score_filters_weak_match():
    """min_score 过滤弱相关项，只保留强相关。"""
    mem = _make()
    mem.set("exp.weak", "文档里顺带提过一次 冲突 这个词")
    mem.set("exp.strong", "git merge 冲突解决办法：push 前先 rebase")
    out = mem.search("git冲突", min_score=40)
    assert "exp.strong" in out
    assert "exp.weak" not in out, "弱相关项应被阈值剔除"


def test_synonym_group_query():
    """命中同义词组中的任一词都会扩展出整组相关词（不影响精确匹配）。"""
    from agent_core.memory.local_memory import _expand_query
    terms = _expand_query("部署上线")
    assert "deploy" in terms and "上线" in terms