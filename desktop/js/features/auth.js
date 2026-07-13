/* auth.js — 用户认证、登出、用户菜单、修改密码
   依赖: state.js, util.js, i18n.js, messaging.js(addMessage),
         skills.js(refreshSkills), stats.js(checkHealth),
         sessions.js(renderSessionList) */

// ---------- 用户信息加载 ----------

async function loadCurrentUser() {
  try {
    const res = await fetch('/users/me');
    if (!res.ok) return null;
    currentUser = await res.json();
  } catch {
    currentUser = null;
  }
  isAdmin = currentUser && currentUser.id === 'admin';
  settingsBtn.style.display = isAdmin ? '' : 'none';
  if (!isAdmin) {
    document.getElementById('settings-modal').classList.remove('active');
  }
  // 更新用户头像
  const avatarText = document.getElementById('avatar-text');
  const userName = document.getElementById('user-menu-name');
  if (currentUser) {
    const display = currentUser.name || currentUser.id || '?';
    avatarText.textContent = display.charAt(0).toUpperCase();
    avatarText.title = display;
    userName.textContent = display;
  } else {
    avatarText.textContent = '?';
    userName.textContent = '?';
  }
  return currentUser;
}

// ---------- 登出 ----------

function logout() {
  closeUserMenu();
  fetch('/auth/logout', { method: 'POST' }).then(() => {
    window.location.href = '/login';
  });
}

// ── 用户头像下拉菜单 ──
function toggleUserMenu(event) {
  event.stopPropagation();
  document.getElementById('user-menu').classList.toggle('show');
}
// 点击其他地方关闭
document.addEventListener('click', function(e) {
  if (!e.target.closest('#user-avatar') && !e.target.closest('#user-menu')) {
    closeUserMenu();
  }
});
function closeUserMenu() {
  document.getElementById('user-menu').classList.remove('show');
}

// ── 微信 Bot 扫码登录 ──
function openWechatBot() {
  closeUserMenu();
  document.getElementById('wechat-bot-frame').src = '/wechat/qrcode';
  document.getElementById('wechat-bot-overlay').classList.add('active');
}
function closeWechatBot() {
  document.getElementById('wechat-bot-overlay').classList.remove('active');
  // 清空 iframe，避免关闭后仍在后台轮询扫码状态
  document.getElementById('wechat-bot-frame').src = 'about:blank';
}

// ── 修改密码 ──
function openChangePassword() {
  closeUserMenu();
  document.getElementById('cp-current').value = '';
  document.getElementById('cp-new').value = '';
  document.getElementById('cp-confirm').value = '';
  document.getElementById('cp-error').style.display = 'none';
  document.getElementById('change-password-overlay').classList.add('active');
}
function closeChangePassword() {
  document.getElementById('change-password-overlay').classList.remove('active');
}
function submitChangePassword() {
  const cur = document.getElementById('cp-current').value;
  const pwd = document.getElementById('cp-new').value;
  const confirm = document.getElementById('cp-confirm').value;
  const errEl = document.getElementById('cp-error');
  if (!cur || !pwd || !confirm) {
    errEl.textContent = currentLanguage === 'en' ? 'Please fill all fields' : '请填写所有字段';
    errEl.style.display = 'block';
    return;
  }
  if (pwd !== confirm) {
    errEl.textContent = currentLanguage === 'en' ? 'Passwords do not match' : '两次密码不一致';
    errEl.style.display = 'block';
    return;
  }
  if (pwd.length < 4) {
    errEl.textContent = currentLanguage === 'en' ? 'Password must be at least 4 characters' : '密码至少需要 4 个字符';
    errEl.style.display = 'block';
    return;
  }
  fetch('/auth/change-password', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ current_password: cur, new_password: pwd }),
  }).then(r => {
    if (r.ok) {
      closeChangePassword();
      const msg = currentLanguage === 'en' ? 'Password changed successfully' : '密码修改成功';
      addMessage(msg, 'system');
    } else {
      return r.json().then(d => { throw new Error(d.detail || 'Failed'); });
    }
  }).catch(e => {
    errEl.textContent = e.message;
    errEl.style.display = 'block';
  });
}
