"""回归：失败请求不再产生/回灌脏 assistant 消息（2026-10-02 事故）。

事故根因：agent.py 失败路径把 "❌ " + error 当 assistant 落盘 →
下轮 session_messages_to_langchain 转成 AIMessage 回灌 LLM →
OpenAI 校验 tool_calls 失败 → 级联 400。

修复（双边界）：
  P1 写边界  services.agent_service._save_assistant_result：
             纯错误文本（❌ 前缀 + 无 steps/todo）不落 assistant 消息。
  P2 读边界  agent_helpers.session_messages_to_langchain：
             存量脏 assistant（❌ 前缀、非 JSON）不生成 AIMessage。
"""
import json

# 触发 agent_core 的 sys.path 注入，使 agent_service 里的裸 import session_store 可用
from agent_core.main import app  # noqa: F401

from agent_core.services.agent_service import _save_assistant_result
from agent_core import session_store
from agent_core.agent_helpers import session_messages_to_langchain


# ── 测试 DB 隔离由 conftest 全局重定向 HOME 完成（.desktop_agent 落临时目录），
#    本文件不再手动 patch，只需每条用独立 session_id 避免计数互相干扰。
def _assistant_count(uid, sid):
    with session_store._connect(uid) as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE session_id=? AND role='assistant'",
            (sid,),
        ).fetchone()["c"]


class TestSaveAssistantResultWriteGuard:
    """P1 写边界。"""

    def test_pure_error_text_not_persisted(self):
        sid = "guard_s1"
        session_store.create_session("test", title="t", session_id=sid)
        _save_assistant_result("test", sid, "帮我看看", "❌ 模型事件流异常，本次回复已中断：400")
        assert _assistant_count("test", sid) == 0, "纯错误文本不应落 assistant 消息"

    def test_error_with_steps_still_persisted(self):
        sid = "guard_s2"
        session_store.create_session("test", title="t", session_id=sid)
        _save_assistant_result(
            "test", sid, "帮我看看", "❌ 失败但已执行部分步骤",
            steps=[{"type": "tool_result", "tool": "run_shell"}],
        )
        assert _assistant_count("test", sid) == 1, "带 steps 的错误结果应保留"

    def test_normal_result_persisted(self):
        sid = "guard_s3"
        session_store.create_session("test", title="t", session_id=sid)
        _save_assistant_result("test", sid, "你好", "你好，我是助手。")
        assert _assistant_count("test", sid) == 1


class TestLangchainReadGuard:
    """P2 读边界：跳过存量脏 assistant。"""

    def test_dirty_assistant_skipped(self):
        msgs = [
            {"role": "user", "content": "继续"},
            {"role": "assistant", "content": "❌ 模型事件流异常，本次回复已中断：400"},
        ]
        out = session_messages_to_langchain(msgs)
        roles = [type(m).__name__ for m in out]
        assert "AIMessage" not in roles, "❌ 前缀纯错误 assistant 不应生成 AIMessage"
        assert "HumanMessage" in roles

    def test_normal_assistant_kept(self):
        msgs = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好，我已处理。"},
        ]
        out = session_messages_to_langchain(msgs)
        assert any(type(m).__name__ == "AIMessage" for m in out)

    def test_assistant_json_with_steps_kept(self):
        # 带 steps 的结构化正常结果（text 正常），不应被误伤
        payload = json.dumps({"text": "完成", "steps": [{"tool": "x"}]}, ensure_ascii=False)
        msgs = [
            {"role": "user", "content": "做"},
            {"role": "assistant", "content": payload},
        ]
        out = session_messages_to_langchain(msgs)
        assert any(type(m).__name__ == "AIMessage" for m in out)
