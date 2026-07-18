"""会话懒加载接口测试"""
import json
import time

from fastapi.testclient import TestClient

from agent_core.main import app
from agent_core import session_store
from agent_core.api.deps import _auth_config, _sign_session

client = TestClient(app)


def _auth_cookie():
    # 应用要求 HMAC 签名的会话 cookie，而非 Bearer token
    exp = int(time.time()) + 3600
    return {"desktop_agent_session": _sign_session("admin", exp)}


def test_lite_and_detail_roundtrip():
    # 创建会话并插入一条带 steps/todo 的 bot 消息（uid 需与登录 cookie 一致）
    uid = "admin"
    session = session_store.create_session(uid, title="懒加载测试")
    sid = session["id"]
    payload = json.dumps({
        "text": "最终答案",
        "steps": [{"type": "thought", "thought": "思考内容"}],
        "todo_list": {"items": [{"id": "t1", "content": "写测试", "status": "done"}]},
    })
    session_store.add_message(uid, sid, "assistant", payload)

    # lite 接口：应返回 has_steps/has_todo/content_preview，不含 steps/todo_list
    r = client.get(f"/sessions/{sid}/messages/lite?source=web&include=lite", cookies=_auth_cookie())
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == sid
    assert len(data["messages"]) == 1
    msg = data["messages"][0]
    assert msg["role"] == "assistant"
    assert msg["has_steps"] is True
    assert msg["has_todo"] is True
    assert "steps" not in msg
    assert "todo_list" not in msg
    assert msg["content_preview"] == "最终答案"

    # detail 接口：应返回完整 steps/todo/content
    r = client.get(f"/sessions/{sid}/messages/0?source=web", cookies=_auth_cookie())
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == sid
    msg = data["message"]
    assert msg["role"] == "assistant"
    assert msg["steps"] == [{"type": "thought", "thought": "思考内容"}]
    assert msg["todo_list"]["items"][0]["content"] == "写测试"
    assert msg["content"] == "最终答案"
