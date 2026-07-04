"""认证路由模块"""
import hashlib
import hmac
import json
import os
import secrets
import time

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from api.deps import (
    AUTH_COOKIE_NAME,
    AUTH_SESSION_SECONDS,
    AUTH_FILE,
    _auth_config,
    _sign_session,
    _verify_session,
    _get_current_user,
)

router = APIRouter(tags=["auth"])


# ---------- 请求模型 ----------


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


# ---------- 内联 HTML ----------

LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Desktop Agent 登录</title>
<style>
* { box-sizing:border-box; }
html, body { filter:none !important; opacity:1 !important; }
body { margin:0; min-height:100vh; display:grid; place-items:center; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:#f5f5f7 !important; color:#1d1d1f; }
.login { position:relative; z-index:2147483647; width:min(380px, calc(100vw - 32px)); background:#fff; border:1px solid #e5e5ea; border-radius:10px; padding:28px; box-shadow:0 18px 50px rgba(0,0,0,.08); }
.lang { position:fixed; top:16px; right:16px; z-index:2147483647; height:34px; border:1px solid #d2d2d7; border-radius:8px; padding:0 8px; background:#fff; color:#1d1d1f; }
h1 { margin:0 0 6px; font-size:24px; }
p { margin:0 0 22px; color:#6e6e73; font-size:14px; }
label { display:block; margin:14px 0 6px; font-size:13px; color:#515154; }
input { width:100%; height:40px; border:1px solid #d2d2d7; border-radius:8px; padding:0 12px; font-size:14px; outline:none; }
input:focus { border-color:#007aff; box-shadow:0 0 0 3px rgba(0,122,255,.12); }
button { width:100%; height:42px; margin-top:20px; border:0; border-radius:8px; background:#007aff; color:#fff; font-weight:600; font-size:15px; cursor:pointer; }
button:disabled { opacity:.65; cursor:not-allowed; }
.error { min-height:18px; margin-top:12px; color:#d70015; font-size:13px; }
</style>
</head>
<body>
<select class="lang" id="language-select" aria-label="Language">
  <option value="zh-CN">中文</option>
  <option value="en">English</option>
</select>
<form class="login" onsubmit="login(event)">
  <h1>Desktop Agent</h1>
  <p data-i18n="subtitle">请登录后继续操作</p>
  <label for="username" data-i18n="username">用户名</label>
  <input id="username" autocomplete="username" value="admin" autofocus>
  <label for="password" data-i18n="password">密码</label>
  <input id="password" type="password" autocomplete="current-password">
  <button id="submit" type="submit" data-i18n="login">登录</button>
  <div class="error" id="error"></div>
</form>
<script>
const I18N = {
  'zh-CN': {
    title: '智能体助手登录',
    subtitle: '请登录后继续操作',
    username: '用户名',
    password: '密码',
    login: '登录',
    invalid: '用户名或密码错误',
    network: '网络错误，请稍后重试',
  },
  en: {
    title: 'Desktop Agent Login',
    subtitle: 'Sign in to continue',
    username: 'Username',
    password: 'Password',
    login: 'Log in',
    invalid: 'Incorrect username or password',
    network: 'Network error. Please try again later.',
  },
};
let currentLanguage = localStorage.getItem('desktop-agent-language') || ((navigator.language || '').toLowerCase().startsWith('zh') ? 'zh-CN' : 'en');
if (!I18N[currentLanguage]) currentLanguage = 'zh-CN';
function t(key) {
  return (I18N[currentLanguage] && I18N[currentLanguage][key]) || I18N['zh-CN'][key] || key;
}
function applyI18n() {
  document.documentElement.lang = currentLanguage;
  document.title = t('title');
  document.querySelectorAll('[data-i18n]').forEach(el => {
    el.textContent = t(el.dataset.i18n);
  });
  document.getElementById('language-select').value = currentLanguage;
}
document.getElementById('language-select').addEventListener('change', (event) => {
  currentLanguage = event.target.value;
  localStorage.setItem('desktop-agent-language', currentLanguage);
  applyI18n();
});
applyI18n();

function clearOverlays() {
  document.documentElement.style.filter = 'none';
  document.documentElement.style.opacity = '1';
  document.body.style.filter = 'none';
  document.body.style.opacity = '1';
  document.querySelectorAll('.modal-overlay, #sidebar-overlay, .overlay, .backdrop').forEach(el => el.remove());
  Array.from(document.body.children).forEach(el => {
    if (el.classList.contains('login')) return;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    const coversViewport = rect.width >= window.innerWidth * 0.9 && rect.height >= window.innerHeight * 0.9;
    const overlaysPage = ['fixed', 'absolute'].includes(style.position) && coversViewport;
    if (overlaysPage) el.remove();
  });
}
clearOverlays();
window.addEventListener('pageshow', clearOverlays);

async function login(event) {
  event.preventDefault();
  const btn = document.getElementById('submit');
  const err = document.getElementById('error');
  btn.disabled = true;
  err.textContent = '';
  try {
    const res = await fetch('/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        username: document.getElementById('username').value,
        password: document.getElementById('password').value,
      }),
    });
    if (res.ok) {
      location.href = '/';
    } else {
      const data = await res.json().catch(() => ({}));
      err.textContent = currentLanguage === 'en' ? t('invalid') : (data.detail || t('invalid'));
    }
  } catch {
    err.textContent = t('network');
  } finally {
    btn.disabled = false;
  }
}
</script>
</body>
</html>"""

LOGIN_TOKEN_ERROR_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>登录失败 - Desktop Agent</title>
<style>
* { box-sizing:border-box; }
body { margin:0; min-height:100vh; display:grid; place-items:center; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:#f5f5f7; color:#1d1d1f; }
.card { width:min(380px, calc(100vw - 32px)); background:#fff; border:1px solid #e5e5ea; border-radius:10px; padding:28px; text-align:center; box-shadow:0 18px 50px rgba(0,0,0,.08); }
h1 { font-size:22px; margin:0 0 8px; }
p { color:#6e6e73; font-size:14px; margin:0 0 20px; }
a { display:inline-block; padding:10px 28px; border-radius:8px; background:#007aff; color:#fff; text-decoration:none; font-size:14px; }
</style>
</head>
<body>
<div class="card">
  <h1>登录链接无效或已过期</h1>
  <p>请重新获取登录链接，或使用密码登录</p>
  <a href="/login">前往密码登录</a>
</div>
</body>
</html>"""


# ---------- 路由 ----------


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page():
    return HTMLResponse(LOGIN_HTML)


@router.post("/auth/login")
def auth_login(req: LoginRequest, response: Response):
    auth = _auth_config()
    users = auth.get("users", {})
    expected_pwd = users.get(req.username)
    if not expected_pwd or not hmac.compare_digest(req.password, expected_pwd):
        raise HTTPException(401, "用户名或密码错误")
    expires_at = int(time.time()) + AUTH_SESSION_SECONDS
    response.set_cookie(
        AUTH_COOKIE_NAME,
        _sign_session(req.username, expires_at),
        max_age=AUTH_SESSION_SECONDS,
        httponly=True,
        samesite="lax",
        secure=os.getenv("DESKTOP_AGENT_AUTH_COOKIE_SECURE", "0") == "1",
    )
    return {"status": "ok"}


@router.post("/auth/logout")
def auth_logout(response: Response):
    response.delete_cookie(AUTH_COOKIE_NAME)
    return {"status": "ok"}


@router.post("/auth/change-password")
def auth_change_password(req: ChangePasswordRequest, request: Request):
    """修改当前登录用户的密码"""
    uid = _get_current_user(request)
    auth = _auth_config()
    users = auth.get("users", {})
    expected = users.get(uid)
    if not expected or not hmac.compare_digest(req.current_password, expected):
        raise HTTPException(403, "当前密码错误")
    if len(req.new_password) < 4:
        raise HTTPException(400, "新密码至少需要 4 个字符")
    users[uid] = req.new_password
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(json.dumps(auth, ensure_ascii=False, indent=2), encoding="utf-8")
    if hasattr(_auth_config, "_cache"):
        try:
            del _auth_config._cache
        except AttributeError:
            pass
    return {"status": "ok"}


@router.get("/auth/token-login", include_in_schema=False)
def auth_token_login(token: str = "", response: Response = None):
    """URL token 免密登录：验证 token，设置会话 cookie，跳转至主页"""
    if not token:
        return HTMLResponse(LOGIN_TOKEN_ERROR_HTML, status_code=400)

    if not _verify_session(token):
        return HTMLResponse(LOGIN_TOKEN_ERROR_HTML, status_code=401)

    # token 写入的部分就是 username:expires_at:signature，直接从中提取用户名
    username = token.split(":")[0]
    expires_at = int(time.time()) + AUTH_SESSION_SECONDS
    response.set_cookie(
        AUTH_COOKIE_NAME,
        _sign_session(username, expires_at),
        max_age=AUTH_SESSION_SECONDS,
        httponly=True,
        samesite="lax",
        secure=os.getenv("DESKTOP_AGENT_AUTH_COOKIE_SECURE", "0") == "1",
    )
    response.status_code = 302
    response.headers["Location"] = "/"
    return None
