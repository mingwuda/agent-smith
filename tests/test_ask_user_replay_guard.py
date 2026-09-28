"""ask_user（征询）前端交互的两条守卫：回放不重弹陈旧弹窗 + 提交失败要暴露 HTTP 状态。

背景（线上现象）：「征询提交失败」的真正原因是前端把 resolve 打到了不存在的
`/agent/sessions/...`（404），但 `resolveAskUser` 只判 `data.resolved`、不看 `resp.ok`，
于是 404 被笼统报成「征询提交失败」，掩盖了真因（排查成本很高）。

另外 `ask_user_modal` 事件会被持久化进 stream_log，历史回放时会重弹一个
早已 resolve/超时的陈旧弹窗，提交必然 `resolved:false`。

这里用源码级断言把两条结论钉住（前端无测试框架，源码扫描是当前最轻的可运行守卫）。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STREAMING = ROOT / "desktop" / "js" / "features" / "streaming.js"


def _src() -> str:
    return STREAMING.read_text(encoding="utf-8")


def _case_block(name: str) -> str:
    """截取 `case '<name>': { ... }` 到下一个 case（或文件尾）之间的源码。"""
    src = _src()
    m = re.search(r"case '" + re.escape(name) + r"':", src)
    assert m, f"未找到 case '{name}'"
    rest = src[m.end():]
    nxt = re.search(r"\n    case '", rest)
    return rest[: nxt.start()] if nxt else rest


def test_ask_user_modal_skipped_on_history_replay():
    """历史回放（_isReplaying 且非重建）必须跳过，否则重弹陈旧弹窗。"""
    block = _case_block("ask_user_modal")
    assert "_isReplaying && !_isReconstructing" in block, (
        "ask_user_modal 缺少回放守卫：刷新/切到已结束会话会重弹陈旧弹窗"
    )
    # 守卫必须在真正弹窗之前 return
    guard_at = block.index("_isReplaying && !_isReconstructing")
    show_at = block.index("showAskUserModal(")
    assert guard_at < show_at, "守卫必须在 showAskUserModal 之前生效"


def test_ask_user_modal_still_shown_for_live_reconstruction():
    """守卫不能误伤：切回仍在跑的会话（_isReconstructing）时仍要弹窗。"""
    block = _case_block("ask_user_modal")
    # 守卫条件里必须有 !_isReconstructing，保证重建路径能穿透
    assert "!_isReconstructing" in block


def test_resolve_ask_user_checks_http_status():
    """resolve 必须检查 resp.ok，失败时带出状态码，避免 404 被掩盖。"""
    src = _src()
    m = re.search(r"function resolveAskUser\([\s\S]*?\n}\n", src)
    assert m, "未找到 resolveAskUser"
    body = m.group(0)
    assert "resp.ok" in body, "resolveAskUser 未检查 HTTP 状态：404/401 会被报成含糊的‘征询提交失败’"
    assert "resp.status" in body, "提交失败时应把 HTTP 状态码带给用户，便于定位"
    # 仍必须复用弹窗绑定的会话（切走后再提交也要打到原会话）
    assert "askUserModalEl" in body and "dataset.sessionId" in body
