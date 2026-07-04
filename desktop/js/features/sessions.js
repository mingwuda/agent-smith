/* sessions.js — 会话列表渲染、加载、切换、新建、删除、侧边栏
   依赖: state.js, util.js, i18n.js, streaming.js(handleStreamEvent, removeGeneratingBadge),
         messaging.js(addMessage, addUserMessage), stats.js(refreshStats) */

// ---------- 会话管理 ----------

let currentSessionSource = '';  // 当前会话来源: "web" / "wechat" / ""

function _sessionKey(s) {
  return s.id + '_' + s.source;
}

function renderSessionList(sessions, currentId) {
  const container = document.getElementById('session-list');
  if (!sessions || sessions.length === 0) {
    container.innerHTML = `<div style="padding:16px;color:#8e8e93;font-size:13px;">${escapeHtml(t('noSessions'))}</div>`;
    return;
  }
  container.innerHTML = sessions.map(s => {
    const key = _sessionKey(s);
    const isActive = s.id === currentId && s.source === currentSessionSource;
    const time = s.updated_at ? s.updated_at.slice(5, 16).replace('T', ' ') : '';
    const isWechat = s.source === 'wechat';
    const title = isWechat ? '💬 ' + (s.title || escapeHtml(t('unnamed'))) : escapeHtml(s.title || t('unnamed'));
    return `<div class="session-item ${isActive ? 'active' : ''} ${isWechat ? 'session-wechat' : ''}" data-key="${key}" onclick="switchSession('${s.id}','${s.source}')">
      <div class="s-title">${title}</div>
      <div class="s-meta">
        <span>${isWechat ? '💬 ' : ''}${escapeHtml(t('messagesCount', { count: s.message_count || 0 }))}</span>
        <span>${time}</span>
        <button class="s-del" onclick="event.stopPropagation(); deleteSession('${s.id}')" title="${escapeHtml(t('deleteTitle'))}">✕</button>
      </div>
    </div>`;
  }).join('');
}

async function loadSessions() {
  try {
    const res = await fetch('/sessions');
    if (!res.ok) return;
    const data = await res.json();
    sessionsCache = data.sessions || [];
    if (!currentSessionId) {
      currentSessionId = data.current_id || (sessionsCache[0] && sessionsCache[0].id) || null;
      currentSessionSource = (sessionsCache[0] && sessionsCache[0].source) || '';
      threadId = currentSessionId || threadId;
    }
    // 如果服务端返回的当前会话不在列表中，优先回落到最新会话，避免加载不存在的 default。
    if (currentSessionId && !sessionsCache.find(s => s.id === currentSessionId && s.source === currentSessionSource)) {
      currentSessionId = (sessionsCache[0] && sessionsCache[0].id) || null;
      currentSessionSource = (sessionsCache[0] && sessionsCache[0].source) || '';
      threadId = currentSessionId || threadId;
    }
    renderSessionList(sessionsCache, currentSessionId);
  } catch {}
}

async function loadSessionMessages(sessionId, source) {
  const container = document.getElementById('messages');
  // 立即清空旧消息，避免切换会话时短暂残留上一会话内容
  container.innerHTML = '';
  // 占位：加载中提示（居中、轻量，不阻断滚动）
  const loadingHint = document.createElement('div');
  loadingHint.className = 'msg system';
  loadingHint.id = '__session_loading_hint__';
  loadingHint.innerHTML = '<span style="display:inline-block;width:12px;height:12px;border:2px solid #8e8e93;border-top-color:transparent;border-radius:50%;animation:spin .6s linear infinite;vertical-align:middle;margin-right:6px;"></span>' + escapeHtml(t('loadingSession') || '加载中...');
  container.appendChild(loadingHint);

  try {
    const qs = source ? `?source=${encodeURIComponent(source)}` : '';
    const res = await fetch(`/sessions/${sessionId}${qs}`);
    // 无论成功失败都先移除加载提示
    const hint = document.getElementById('__session_loading_hint__');
    if (hint) hint.remove();
    if (res.ok) {
      const data = await res.json();
      container.innerHTML = '';
      // 重建输入历史
      _msgHistory = [];
      _msgHistoryIndex = -1;
      if (data.messages && data.messages.length > 0) {
        data.messages.forEach(msg => {
          if (msg.role === 'user' && msg.content) {
            // 只存纯文本，排除含图片的消息
            if (!msg.images || msg.images.length === 0) {
              _msgHistory.push(msg.content);
            }
          }
          const role = msg.role === 'user' ? 'user' : 'bot';
          const content = msg.content || '';
          if (role === 'user' && msg.images && msg.images.length) {
            addUserMessage(content, msg.images.map(u => ({data_url: u})));
          } else if (role === 'bot' && msg.steps && msg.steps.length) {
            addBotMessageWithSteps(content, msg.steps);
          } else {
            addMessage(content, role);
          }
        });
      } else {
        addMessage(t('emptySession'), 'system');
      }
    }
    // 会话消息加载完成后强制滚动到底部
    if (container) container.scrollTop = container.scrollHeight;
  } catch (e) {
    // 异常时移除加载提示
    const hint = document.getElementById('__session_loading_hint__');
    if (hint) hint.remove();
    console.error('[loadSession] failed:', e);
  }
}

// 恢复带步骤卡片的助手消息（从历史加载时使用）
function addBotMessageWithSteps(content, steps) {
  // 重置状态，模拟新一轮流式输出的初始条件
  currentBotMsgEl = null;
  currentStepsEl = null;
  currentFinalContent = '';
  totalSteps = 0;
  hasToolCalls = false;
  generatingBadgeEl = null;
  _isReplaying = true;  // 不会发起 WebSocket 等实时连接

  steps.forEach(data => {
    handleStreamEvent(data);
  });

  _isReplaying = false;

  // 清理回放步骤后残留的"执行中/分析中"状态
  document.querySelectorAll('.tool-status-dot.running').forEach(d => {
    d.className = 'tool-status-dot done';
  });
  document.querySelectorAll('.thinking-step').forEach(el => el.remove());
  // 更新进度条为 100%
  const prog = currentStepsEl ? currentStepsEl.querySelector('.step-progress') : null;
  if (prog) {
    const fill = prog.querySelector('.fill');
    const text = prog.querySelector('.progress-text');
    if (fill) fill.style.width = '100%';
    if (text) text.textContent = t('completeText');
  }
  // 折叠所有工具卡片
  document.querySelectorAll('.tool-card.open').forEach(card => {
    card.classList.remove('open');
  });
  removeGeneratingBadge();

  if (content) {
    addMessage(content, 'bot');
  }
  currentBotMsgEl = null;
  currentStepsEl = null;
}

async function switchSession(sessionId, source, forceLoad = false) {
  if (sessionId === currentSessionId && source === currentSessionSource && !forceLoad) return;
  currentSessionId = sessionId;
  currentSessionSource = source || 'web';
  threadId = sessionId;

  // 更新激活样式
  document.querySelectorAll('.session-item').forEach(el => {
    el.classList.toggle('active', el.dataset.key === _sessionKey({id: sessionId, source: currentSessionSource}));
  });

  // 在选中的会话项上展示 loading 动画
  const activeItem = document.querySelector('.session-item.active');
  if (activeItem) activeItem.classList.add('loading');

  try {
    // 加载该会话的历史消息（内部已立即清空旧消息）
    await loadSessionMessages(sessionId, currentSessionSource);
    refreshStats();
    // 加载该会话的工作目录
    loadWorkspaceDisplay();
  } finally {
    // 无论成功失败都移除 loading 状态
    if (activeItem) activeItem.classList.remove('loading');
  }
}

async function newSession() {
  try {
    const res = await fetch('/sessions', { method: 'POST' });
    if (!res.ok) return;
    const data = await res.json();
    currentSessionId = data.id;
    currentSessionSource = 'web';
    threadId = data.id;
    // 清空消息区域
    document.getElementById('messages').innerHTML = '';
    addMessage(t('newSessionReady'), 'system');
    await loadSessions();
  } catch {}
}

async function deleteSession(sessionId) {
  if (!confirm(t('deleteSessionConfirm'))) return;
  try {
    await fetch(`/sessions/${sessionId}`, { method: 'DELETE' });
    if (sessionId === currentSessionId) {
      // 当前会话被删除，切到第一个或新建
      const remaining = sessionsCache.filter(s => s.id !== sessionId || s.source !== currentSessionSource);
      if (remaining.length > 0) {
        switchSession(remaining[0].id, remaining[0].source);
      } else {
        newSession();
      }
    }
    await loadSessions();
  } catch {}
}

// ---------- 工作目录管理 ----------

async function loadWorkspaceDisplay() {
  const el = document.getElementById('workspace-display');
  if (!el) return;
  if (!currentSessionId) {
    el.style.display = 'none';
    return;
  }
  try {
    const res = await fetch(`/sessions/${currentSessionId}/workspace`);
    if (!res.ok) { el.style.display = 'none'; return; }
    const data = await res.json();
    if (data.workspace) {
      el.style.display = 'inline';
      el.title = '🗂 工作目录: ' + data.workspace + '\n点击修改';
      var parts = data.workspace.split('/').filter(Boolean);
      var shortPath = parts.slice(-2).join('/');
      if (data.workspace.startsWith('/')) shortPath = '/' + shortPath;
      el.textContent = '📁 ' + shortPath;
    } else {
      el.style.display = 'inline';
      el.title = '点击设置工作目录';
      el.textContent = '📁 (未设置)';
    }
  } catch {
    el.style.display = 'none';
  }
}

function promptSetWorkspace() {
  var ws = prompt('请输入工作目录路径（支持绝对路径或相对路径）：\n\n留空取消设置');
  if (ws === null) return;
  ws = ws.trim();
  if (!ws) {
    // 清空工作目录
    fetch('/sessions/' + currentSessionId + '/workspace', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({workspace: ''}),
    }).then(function() { loadWorkspaceDisplay(); });
    return;
  }
  fetch('/sessions/' + currentSessionId + '/workspace', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({workspace: ws}),
  }).then(function(r) {
    if (r.ok) {
      loadWorkspaceDisplay();
    } else {
      r.json().then(function(d) { alert('设置失败: ' + (d.detail || '未知错误')); });
    }
  }).catch(function(e) {
    alert('网络错误: ' + e.message);
  });
}

// ---------- 侧边栏折叠逻辑 ----------

function toggleSidebarAccordion(id) {
  document.querySelectorAll('.sidebar-accordion').forEach(section => {
    const shouldOpen = section.id === id && !section.classList.contains('open');
    section.classList.toggle('open', shouldOpen);
    const button = section.querySelector('.accordion-toggle');
    if (button) button.setAttribute('aria-expanded', shouldOpen ? 'true' : 'false');
  });
}

function toggleSidebar() {
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('sidebar-overlay');
  const isMobile = window.innerWidth <= 768;
  
  if (isMobile) {
    sidebar.classList.toggle('open');
    overlay.classList.toggle('show');
  } else {
    sidebar.classList.toggle('collapsed');
  }
}

document.getElementById('sidebar-toggle').onclick = toggleSidebar;
document.getElementById('sidebar-overlay').onclick = function() {
  document.getElementById('sidebar').classList.remove('open');
  this.classList.remove('show');
};

// 窗口大小变化时自动适配
window.addEventListener('resize', function() {
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('sidebar-overlay');
  if (window.innerWidth > 768) {
    sidebar.classList.remove('open');
    overlay.classList.remove('show');
  }
});
