"""懒加载接口单测：get_session_lite / get_message_detail"""
import sqlite3
import tempfile
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent_core"))

from session_store import _connect, create_session, add_message, get_session_lite, get_message_detail


def test_lazy_load():
    with tempfile.TemporaryDirectory() as tmpdir:
        # 临时替换数据库目录
        import agent_core.session_store as ss
        orig_data_dir = ss.DATA_DIR
        ss.DATA_DIR = tmpdir

        try:
            user = "test_user"
            sid = create_session(user, title="测试会话")["id"]

            # 插入 3 条消息：2 条 user，2 条 bot（其中 1 条含 steps/todo）
            add_message(user, sid, "user", "你好")
            add_message(user, sid, "bot", '{"text":"简单回复"}')
            add_message(user, sid, "user", "帮我做点事")
            add_message(user, sid, "bot", '{"text":"复杂任务","steps":[{"type":"think","content":"思考中"}],"todo_list":{"items":[{"id":"1","content":"写代码","status":"done"}]}}')

            # 1. get_session_lite 应返回轻量结构
            lite = get_session_lite(user, sid)
            assert lite is not None
            assert lite["id"] == sid
            assert len(lite["messages"]) == 4

            # user 消息
            assert lite["messages"][0]["role"] == "user"
            assert "content_preview" in lite["messages"][0]
            assert lite["messages"][0]["has_steps"] is False

            # 简单 bot 消息
            assert lite["messages"][1]["role"] == "bot"
            assert lite["messages"][1]["has_steps"] is False

            # 复杂 bot 消息
            assert lite["messages"][3]["role"] == "bot"
            assert lite["messages"][3]["has_steps"] is True
            assert "steps" not in lite["messages"][3]  # lite 不应包含完整 steps
            assert "todo_list" not in lite["messages"][3]

            # 2. get_message_detail 应返回完整内容
            detail = get_message_detail(user, sid, 3)
            assert detail is not None
            assert detail["role"] == "bot"
            assert "steps" in detail
            assert len(detail["steps"]) == 1
            assert "todo_list" in detail
            assert detail["todo_list"]["items"][0]["content"] == "写代码"
            assert detail["content"] == "复杂任务"

            # 3. 越界索引应返回 None
            assert get_message_detail(user, sid, 99) is None

            print("PASS: lazy load backend tests")
        finally:
            ss.DATA_DIR = orig_data_dir


if __name__ == "__main__":
    test_lazy_load()
