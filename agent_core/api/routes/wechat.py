"""微信 Bot 路由"""
import base64
import json
from io import BytesIO

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

import session_store
from wechat_bot import WeChatBot
from api.deps import _get_current_user
from logger import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["wechat"])


@router.get("/wechat/status")
async def wechat_status(request: Request):
    """获取当前用户的微信 Bot 状态"""
    uid = _get_current_user(request)
    from main import _get_wechat_bot
    from main import _get_wechat_bot
    bot = _get_wechat_bot(uid)
    return {
        "user_id": uid,
        "logged_in": bot.is_logged_in,
        "running": bot.is_running,
    }


@router.get("/wechat/qrcode", response_class=HTMLResponse)
async def wechat_qrcode(request: Request):
    """获取当前用户的微信登录二维码"""
    uid = _get_current_user(request)
    from main import _get_wechat_bot
    bot = _get_wechat_bot(uid)
    data = await bot.get_qrcode()
    qrcode_str = data.get("qrcode", "")
    img_url = data.pop("qrcode_img_content", None)
    qr_data = img_url or f"https://liteapp.weixin.qq.com/q/{qrcode_str}"

    # 生成二维码 PNG
    img_b64 = ""
    try:
        import qrcode
        img = qrcode.make(qr_data)
        buf = BytesIO()
        img.save(buf, format="PNG")
        img_b64 = base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        logger.warning("生成二维码失败: %s", e)

    status_url = f"/wechat/qrcode-status?qrcode={qrcode_str}"
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>微信 Bot 扫码登录 - {uid}</title>
<style>
  body {{ font-family: -apple-system, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; background: #f5f5f5; }}
  .card {{ background: #fff; border-radius: 16px; padding: 40px; text-align: center; box-shadow: 0 2px 12px rgba(0,0,0,.08); }}
  img {{ width: 280px; height: 280px; }}
  .hint {{ color: #666; font-size: 14px; margin-top: 16px; }}
  .status {{ color: #999; font-size: 13px; margin-top: 8px; }}
  .success {{ color: #07c160; font-weight: bold; }}
  .uid {{ color: #999; font-size: 12px; margin-bottom: 8px; }}
</style>
</head>
<body>
<div class="card">
  <div class="uid">👤 {uid}</div>
  <h2>📱 微信 Bot 登录</h2>
  <img src="data:image/png;base64,{img_b64}" alt="微信登录二维码" />
  <div class="hint">请使用微信扫描二维码登录</div>
  <div class="status" id="status">等待扫码...</div>
</div>
<script>
(function() {{
  var statusEl = document.getElementById('status');
  function poll() {{
    fetch('{status_url}')
      .then(function(r) {{ return r.json(); }})
      .then(function(d) {{
        if (d.status === 'confirmed') {{
          statusEl.innerHTML = '✅ <span class="success">扫码成功！Bot 已登录</span>';
        }} else if (d.status === 'expired') {{
          statusEl.innerHTML = '❌ 二维码已过期，请刷新页面重新获取';
        }} else {{
          statusEl.textContent = '等待扫码...（' + d.status + '）';
          setTimeout(poll, 2000);
        }}
      }})
      .catch(function() {{ setTimeout(poll, 2000); }});
  }}
  setTimeout(poll, 2000);
}})();
</script>
</body>
</html>""")


@router.get("/wechat/qrcode-status")
async def wechat_qrcode_status(qrcode: str, request: Request):
    """轮询扫码状态"""
    uid = _get_current_user(request)
    from main import _get_wechat_bot
    bot = _get_wechat_bot(uid)
    return await bot.poll_qrcode_status(qrcode)


@router.post("/wechat/start")
async def wechat_start(request: Request):
    """启动当前用户的微信 Bot 轮询"""
    uid = _get_current_user(request)
    from main import _get_wechat_bot
    bot = _get_wechat_bot(uid)
    if not bot.is_logged_in:
        raise HTTPException(400, "尚未登录，请先扫码")
    await bot.start()
    return {"user_id": uid, "status": "started"}


@router.post("/wechat/stop")
async def wechat_stop(request: Request):
    """停止当前用户的微信 Bot 轮询"""
    uid = _get_current_user(request)
    from main import _get_wechat_bot
    bot = _get_wechat_bot(uid)
    await bot.stop()
    return {"user_id": uid, "status": "stopped"}


@router.get("/wechat/sessions")
async def wechat_sessions_list(request: Request):
    """列出当前用户的微信 Bot 会话"""
    uid = _get_current_user(request)
    wechat_uid = f"wechat_{uid}"
    raw = session_store.list_sessions(wechat_uid)
    return [
        {
            "id": s["id"],
            "title": s.get("title", "未命名"),
            "created_at": s.get("created_at", ""),
            "updated_at": s.get("updated_at", ""),
            "message_count": s.get("message_count", 0),
        }
        for s in raw
    ]


@router.get("/wechat/sessions/{session_id}")
async def wechat_session_messages(session_id: str, request: Request):
    """获取当前用户的微信 Bot 会话消息"""
    uid = _get_current_user(request)
    wechat_uid = f"wechat_{uid}"
    session = session_store.get_session(wechat_uid, session_id)
    if not session:
        raise HTTPException(404, "会话不存在")
    return {
        "id": session["id"],
        "title": session.get("title", "未命名"),
        "messages": session.get("messages", []),
    }
