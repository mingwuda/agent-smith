"""回归测试：删除单条消息的 index 语义必须与 lite 接口返回的 index 一致（绝对位置）。

锁定的契约：
- get_session_lite 返回的每条消息其 index 字段 = 全量 ORDER BY id ASC 的绝对位置。
- delete_message(uid, sid, message_index) 按同一绝对位置解析并删除。
前端必须用 msg.index（绝对）而非窗口内相对序号，否则会删错消息。
"""
import shutil
from pathlib import Path

# 必须先导入 main 以触发 agent_core/main.py 顶部的 sys.path 注入，
# 否则 session_store 里的顶层 `import user_manager` 会找不到模块。
from agent_core.main import app  # noqa: F401
from agent_core import session_store


UID = "test_delete_msg_uid"
SESSIONS_ROOT = Path.home() / ".desktop_agent" / "sessions"


def _cleanup():
    d = SESSIONS_ROOT / UID
    if d.exists():
        shutil.rmtree(d)


def test_delete_uses_absolute_index_matching_lite():
    _cleanup()
    try:
        sid = session_store.create_session(UID, title="t")["id"]
        n = 25
        for i in range(n):
            session_store.add_message(UID, sid, "user" if i % 2 == 0 else "bot", f"MSG_{i}")

        # lite 以绝对位置视角返回（offset=0）
        lite = session_store.get_session_lite(UID, sid, limit=100, offset=0)
        assert lite["total_count"] == n
        for i, m in enumerate(lite["messages"]):
            assert m["index"] == i, f"lite index 不对齐: {m['index']} != {i}"
            assert f"MSG_{i}" in (m.get("content") or ""), m

        # 删除绝对位置 5（≈会话顶部的旧消息），应删掉 MSG_5
        ok = session_store.delete_message(UID, sid, 5)
        assert ok is True

        lite2 = session_store.get_session_lite(UID, sid, limit=100, offset=0)
        assert lite2["total_count"] == n - 1
        # 原 MSG_5 已消失；原 MSG_6 现在落在绝对位置 5
        assert "MSG_5" not in (lite2["messages"][5].get("content") or ""), "MSG_5 应被删除"
        assert "MSG_6" in (lite2["messages"][5].get("content") or ""), "MSG_6 应落在 index=5"
        # 之前的 MSG_4 仍在 index=4
        assert "MSG_4" in (lite2["messages"][4].get("content") or "")

        # 越界 index 应安全返回 False（404）
        assert session_store.delete_message(UID, sid, 9999) is False
    finally:
        _cleanup()
