"""压缩「长期/短期记忆分层」回归测试。

需求（优化项 #2）：old 段被压缩成「仅用户关键指令」后，把这些指令写入长期记忆
（SQLite FTS 可检索）；后续压缩若指令已在记忆中，摘要只写 [见记忆: key] 引用。

契约：
- compact_messages 传 memory（LocalMemory 实例）→ old 段每条用户指令归档进记忆，
  key 前缀 _ctx_old_，摘要行写 [见记忆: key] 引用；
- 同一指令反复压缩只归档一次（sha1 key 去重），不产生重复记忆；
- memory 为 None（纯函数/测试模式）→ 行为完全不变，不写任何记忆；
- 归档内容可被 memory.search（FTS trigram）检索，支持"继续/上次那个文件"式召回。
"""
from agent_core.main import app  # noqa: F401  触发 sys.path 注入
from agent_core.context_manager import compact_messages
from agent_core.memory.local_memory import LocalMemory
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage


def _build_long_history(rounds: int = 10) -> list:
    msgs = [SystemMessage(content="sys")]
    for i in range(rounds):
        msgs.append(HumanMessage(content=f"第{i}轮问题 " + "x" * 300))
        msgs.append(AIMessage(content=f"第{i}轮回答 " + "y" * 300))
    return msgs


def _old_archived_keys(memory: LocalMemory) -> list:
    return [k for k in memory.list_keys() if k.startswith("_ctx_old_")]


def test_archive_writes_user_instructions_into_memory(tmp_path):
    """传 memory 压缩：old 段用户指令写入长期记忆，key 带 _ctx_old_ 前缀。"""
    memory = LocalMemory(tmp_path / "mem")
    compacted = compact_messages(_build_long_history(), "deepseek-chat", configured_window=3000, memory=memory)

    keys = _old_archived_keys(memory)
    assert keys, "old 段用户指令应归档进长期记忆"
    # 摘要行必须写 [见记忆: key] 引用
    summary = next(
        getattr(m, "content", "") for m in compacted
        if getattr(m, "type", "") == "ai" and "摘要" in str(getattr(m, "content", ""))
    )
    assert "[见记忆: _ctx_old_" in summary


def test_same_instruction_archived_only_once(tmp_path):
    """同一指令反复压缩只归档一次：再次压缩同一批消息，记忆 key 数不增加。"""
    memory = LocalMemory(tmp_path / "mem")
    msgs = _build_long_history()
    compact_messages(msgs, "deepseek-chat", configured_window=3000, memory=memory)
    count_first = len(_old_archived_keys(memory))

    compact_messages(msgs, "deepseek-chat", configured_window=3000, memory=memory)
    count_second = len(_old_archived_keys(memory))
    assert count_second == count_first, "重复压缩不得产生重复记忆"


def test_no_memory_arg_keeps_pure_function_behavior(tmp_path):
    """memory 为 None：不写任何记忆，行为与旧版一致（摘要含早期摘要段）。"""
    memory = LocalMemory(tmp_path / "mem")
    compacted = compact_messages(_build_long_history(), "deepseek-chat", configured_window=3000)
    assert _old_archived_keys(memory) == []
    # 旧行为：摘要里直接含用户指令裁剪文本（不带 [见记忆:）
    summary = "\n".join(
        str(getattr(m, "content", "")) for m in compacted if getattr(m, "type", "") == "ai"
    )
    assert "【早期摘要" in summary
    assert "[见记忆:" not in summary


def test_archived_instruction_searchable_via_fts(tmp_path):
    """归档指令可被 FTS 检索（trigram 中文子串）——"继续/上次那个文件"式召回的基础。"""
    memory = LocalMemory(tmp_path / "mem")
    msgs = [SystemMessage(content="sys")]
    for i in range(10):
        msgs.append(HumanMessage(content=f"第{i}轮: 把 /data/file{i}.py 重构为异步 " + "z" * 100))
        msgs.append(AIMessage(content="收到 " + "y" * 300))
    compact_messages(msgs, "deepseek-chat", configured_window=3000, memory=memory)

    # 用真实召回场景的关键词搜索：应能命中归档的完整指令
    result = memory.search("/data/file3.py")
    assert "_ctx_old_" in result
