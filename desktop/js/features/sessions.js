/* sessions.js — 会话列表渲染、加载、切换、新建、删除、侧边栏
   依赖: state.js, util.js, i18n.js, streaming.js(handleStreamEvent, removeGeneratingBadge),
         messaging.js(addMessage, addUserMessage), stats.js(refreshStats) */

// ---------- 会话管理 ----------

let currentSessionSource = '';  // 当前会话来源: "web" / "wechat" / ""
let _sessionLoadToken = 0;      // 会话加载请求令牌: 单调递增, 仅最后一次 loadSessionMessages 可写回 DOM(防切/建会话时旧请求晚到回写)

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
    // 渲染：优先使用工作区/项目视图（workspace.js 提供），否则降级为旧列表
    if (typeof renderWorkspace === 'function') {
      await renderWorkspace();
    } else {
      renderSessionList(sessionsCache, currentSessionId);
    }
    // 重建列表后，依据各会话 runtime 状态恢复「正在执行」指示器
    updateRunIndicators();
  } catch {}
}

async function loadSessionMessages(sessionId, source, options = {}) {
  const container = document.getElementById('messages');
  // ponytail: 请求令牌 —— 仅最后一次 loadSessionMessages 可写回 DOM,
  // 防止切换/新建会话时仍“在途”的旧请求晚到回写, 把上一会话的消息塞进当前面板。
  const myToken = ++_sessionLoadToken;
  // 立即清空旧消息，避免切换会话时短暂残留上一会话内容
  container.innerHTML = '';
  // 占位：加载中提示（居中、轻量，不阻断滚动）
  const loadingHint = document.createElement('div');
  loadingHint.className = 'msg system';
  loadingHint.id = '__session_loading_hint__';
  loadingHint.innerHTML = '<span style="display:inline-block;width:12px;height:12px;border:2px solid #8e8e93;border-top-color:transparent;border-radius:50%;animation:spin .6s linear infinite;vertical-align:middle;margin-right:6px;"></span>' + escapeHtml(t('loadingSession') || '加载中...');
  container.appendChild(loadingHint);

  try {
    const limit = options.limit || 20;
    const offset = options.offset != null ? options.offset : -20;
    const qs = source ? `?source=${encodeURIComponent(source)}&include=lite&limit=${limit}&offset=${offset}` : `?include=lite&limit=${limit}&offset=${offset}`;
    const res = await fetch(`/sessions/${sessionId}/messages/lite${qs}`);
    // 若期间已发起更新的加载(切换/新建会话), 或当前会话已不是本次要加载的会话,
    // 本次结果作废, 避免旧会话内容回写进新会话面板。
    // 注意: newSession() 不调用本函数, 不会自增令牌, 故仅靠令牌守卫不够,
    // 必须用 sessionId 硬校验当前会话。
    if (myToken !== _sessionLoadToken) return;
    if (sessionId !== currentSessionId || (source || 'web') !== (currentSessionSource || 'web')) return;
    // 无论成功失败都先移除加载提示
    const hint = document.getElementById('__session_loading_hint__');
    if (hint) hint.remove();
    if (res.ok) {
      const data = await res.json();
      // 二次校验: json 解析期间可能又发生了更新的切换/新建
      if (myToken !== _sessionLoadToken) return;
      if (sessionId !== currentSessionId || (source || 'web') !== (currentSessionSource || 'web')) return;
      container.innerHTML = '';
      // 重建输入历史
      _msgHistory = [];
      _msgHistoryIndex = -1;
      // 用于估算每轮 bot 响应的耗时（前一条 user 消息时间 → 当前 bot 消息时间）
      var lastUserTs = 0;
      if (data.messages && data.messages.length > 0) {
      data.messages.forEach((msg, idx) => {
        const role = msg.role === 'user' ? 'user' : 'bot';
        const content = msg.content || '';
        const parsed = role === 'user' ? parseTextFilesFromContent(content) : null;
        // ponytail: 必须用后端返回的绝对 index（real_index），不能用 forEach 的 idx。
        // 因为 lite 接口按 offset=-20 取的是“最后 20 条”，idx 是窗口内相对序号，
        // 后端 delete_message 却按 ORDER BY id ASC 的绝对位置解释 —— 用 idx 会删错消息。
        const msgIndex = msg.index != null ? msg.index : idx;

        if (msg.role === 'user' && msg.content) {
          // 只存纯文本，排除含图片/文本文件的消息（避免把 base64 或文件正文塞进历史）
          const histText = (parsed && parsed.files.length) ? parsed.message : msg.content;
          if ((!msg.images || msg.images.length === 0) && histText) {
            _msgHistory.push(histText);
          }
          // 记录用户消息时间戳，用于估算下一轮 bot 耗时
          if (msg.timestamp) { try { lastUserTs = new Date(msg.timestamp).getTime(); } catch(e){} }
        }

        if (role === 'user' && parsed && parsed.files.length) {
          // ponytail: 文本附件刷新后也显示为图标，双击展开内容（与实时发送一致）
          addUserMessage(parsed.message, parsed.files.map(f => ({ name: f.name, mime_type: 'text/plain', content: f.content })), msgIndex);
        } else if (role === 'user' && msg.has_images) {
          addUserMessageLazyImages(content, msg.image_count, sessionId, msgIndex, source);
        } else if (role === 'bot' && msg.has_steps) {
          // 估算本轮 bot 响应耗时：bot 时间 - 前一条 user 消息时间
          var botElapsed = 0;
          if (msg.timestamp && lastUserTs > 0) {
            try { botElapsed = new Date(msg.timestamp).getTime() - lastUserTs; } catch(e){}
          }
          var placeholderEl = addBotMessagePlaceholder(content, msg.content_preview, botElapsed, sessionId, msgIndex);
          if (placeholderEl) container.appendChild(placeholderEl);
        } else if (role === 'bot') {
          addMessage(content || msg.content_preview || '', 'bot', msgIndex);
        } else {
          addMessage(content, role, msgIndex);
        }
      });
      } else {
        addMessage(t('emptySession'), 'system');
      }
      // 存储分页状态
      _sessionPageState = {
        sessionId,
        source: source || 'web',
        limit,
        offset,
        hasMore: data.has_more,
        totalCount: data.total_count,
        loadedCount: data.messages ? data.messages.length : 0,
      };
      // 绑定滚动加载更多
      _attachSessionScrollLoader();
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

// 历史消息占位卡片（带步骤但尚未展开详情）
function addBotMessagePlaceholder(content, contentPreview, elapsedMs, sessionId, messageIndex) {
  const container = document.getElementById('messages');
  _lastToolImageHtml = null;
  if (_currentTodoPanel && _currentTodoPanel.parentNode) {
    _currentTodoPanel.remove();
  }
  _currentTodoPanel = null;
  currentBotMsgEl = null;
  currentStepsEl = null;
  currentFinalContent = '';
  totalSteps = 0;
  hasToolCalls = false;
  generatingBadgeEl = null;

  var responseCard = document.createElement('div');
  responseCard.className = 'agent-response finished collapsed';
  if (typeof messageIndex === 'number') {
    responseCard.dataset.index = String(messageIndex);
  }
  var timeVal = (elapsedMs && elapsedMs > 0) ? formatElapsed(elapsedMs) : '\u2014';
  var headerEl = document.createElement('div');
  headerEl.className = 'agent-header';
  headerEl.innerHTML =
    '<div class="agent-avatar">\uD83E\uDD16</div>' +
    '<span class="agent-toggle-arrow">\u25B6</span>' +
    '<span class="agent-time"><span class="agent-time-label">' + (t('workElapsed') || '工作耗时') + ': </span> <span class="agent-time-val">' + timeVal + '</span></span>';
  headerEl.onclick = function() {
    if (responseCard.classList.contains('collapsed')) {
      expandBotMessagePlaceholder(responseCard, sessionId, messageIndex);
    } else {
      responseCard.classList.add('collapsed');
    }
  };
  responseCard.appendChild(headerEl);

  var bodyEl = document.createElement('div');
  bodyEl.className = 'agent-body';
  responseCard.appendChild(bodyEl);

  if (content || contentPreview) {
    const ans = document.createElement('div');
    ans.className = 'agent-final-output';
    ans.innerHTML = renderMarkdown(content || contentPreview || '');
    responseCard.appendChild(ans);
    currentBotMsgEl = ans;
  }

  // ponytail: 不在此处自动 container.appendChild，由调用方决定插入位置。
  // 否则 _loadOlderMessages 用 fragment 批量插顶部时，bot 卡片被此函数内部
  // append 到容器末尾，导致问题/回复位置全部错开。
  return responseCard;
}

// 用户图片消息：占位 + 懒加载。
// lite 接口不再返回图片 base64（单张截图可达数 MB），只给 has_images/image_count。
// 这里先渲染灰色占位方块，再走详情接口 /messages/{index} 异步拉取真实图片替换。
function addUserMessageLazyImages(text, imageCount, sessionId, messageIndex, source) {
  const div = addUserMessage(text, [], messageIndex); // 只渲染文本，不带附件
  const count = imageCount && imageCount > 0 ? imageCount : 1;
  const grid = document.createElement('div');
  grid.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;';
  for (let i = 0; i < count; i++) {
    const ph = document.createElement('div');
    ph.style.cssText = 'width:96px;height:96px;border-radius:8px;background:#e5e7eb;display:flex;align-items:center;justify-content:center;';
    ph.innerHTML = '<span style="display:inline-block;width:14px;height:14px;border:2px solid #9ca3af;border-top-color:transparent;border-radius:50%;animation:spin .6s linear infinite;"></span>';
    grid.appendChild(ph);
  }
  div.appendChild(grid);

  fetch(`/sessions/${sessionId}/messages/${messageIndex}?source=${encodeURIComponent(source || 'web')}`)
    .then(r => r.ok ? r.json() : null)
    .then(data => {
      const imgs = (data && data.message && data.message.images) || [];
      if (!imgs.length) { grid.remove(); return; }
      grid.innerHTML = '';
      imgs.forEach(u => {
        const img = document.createElement('img');
        img.src = u;
        img.alt = 'image';
        img.style.cssText = 'width:96px;height:96px;object-fit:cover;border-radius:8px;border:1px solid rgba(255,255,255,.5);cursor:zoom-in;';
        img.title = '点击放大查看';
        img.addEventListener('click', () => openImageZoom(u));
        grid.appendChild(img);
      });
    })
    .catch(() => { /* 拉取失败保留占位，不抛错 */ });

  return div;
}

// 点击图片放大查看：懒加载单例 overlay，点击遮罩/关闭按钮/Esc 关闭。
// ponytail: 复用 streaming.js 的 capsule overlay 模式（单例 + classList 切换 + 点击外部关闭）。
function openImageZoom(src) {
  let overlay = document.getElementById('img-zoom-overlay');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = 'img-zoom-overlay';
    overlay.className = 'img-zoom-overlay';
    overlay.innerHTML = '<button class="img-zoom-close" title="关闭">✕</button><img alt="放大查看">';
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay || e.target.classList.contains('img-zoom-close')) overlay.classList.remove('active');
    });
    overlay._escHandler = (e) => { if (e.key === 'Escape') overlay.classList.remove('active'); };
    document.addEventListener('keydown', overlay._escHandler);
    document.body.appendChild(overlay);
  }
  overlay.querySelector('img').src = src;
  overlay.classList.add('active');
}

// 展开历史消息占位卡片，按需加载完整 steps/todo
async function expandBotMessagePlaceholder(responseCard, sessionId, messageIndex) {
  if (responseCard.dataset.loading === 'true') return;
  responseCard.dataset.loading = 'true';
  const bodyEl = responseCard.querySelector('.agent-body');
  if (!bodyEl) return;
  bodyEl.innerHTML = '<div style="padding:8px 12px;color:#8e8e93;font-size:12px;">加载工作详情...</div>';

  try {
    const res = await fetch(`/sessions/${sessionId}/messages/${messageIndex}?source=${encodeURIComponent(currentSessionSource || 'web')}`);
    if (!res.ok) throw new Error('failed');
    const data = await res.json();
    const msg = data.message || {};
    const steps = msg.steps || [];
    const todoList = msg.todo_list;
    const content = msg.content || '';

    bodyEl.innerHTML = '';
    currentStepsEl = bodyEl;
    currentBotMsgEl = null;
    currentFinalContent = '';
    totalSteps = 0;
    hasToolCalls = false;
    _isReplaying = true;

    if (steps.length) {
      steps.forEach(evt => handleStreamEvent(evt));
    }

    _isReplaying = false;

    const ans = document.createElement('div');
    ans.className = 'agent-final-output';
    ans.innerHTML = renderMarkdown(content);
    currentBotMsgEl = ans;
    responseCard.appendChild(ans);

    if (todoList) {
      renderTodoPanel(todoList, false);
    }
    if (currentBotMsgEl && _currentTodoPanel) {
      responseCard.insertBefore(_currentTodoPanel, currentBotMsgEl);
    }

    document.querySelectorAll('.tool-status-dot.running').forEach(d => {
      d.className = 'tool-status-dot done';
    });
    document.querySelectorAll('.thinking-step').forEach(el => el.remove());
    document.querySelectorAll('.tool-card.open').forEach(card => {
      card.classList.remove('open');
    });
    removeGeneratingBadge();

    currentBotMsgEl = null;
    currentStepsEl = null;
    responseCard.classList.remove('collapsed');
  } catch (e) {
    bodyEl.innerHTML = '<div style="padding:8px 12px;color:#ff453a;font-size:12px;">加载失败，请重试</div>';
  } finally {
    responseCard.dataset.loading = 'false';
  }
}

// 恢复带步骤卡片的助手消息（从历史加载时使用）
function addBotMessageWithSteps(content, steps, todoList, elapsedMs) {
  const container = document.getElementById('messages');
  // 防止前一条消息的 _lastToolImageHtml 泄漏到当前消息
  _lastToolImageHtml = null;
  // 防止上一会话的 todo 面板泄漏到当前历史消息中（全局单例，跨会话不清理会串）
  if (_currentTodoPanel && _currentTodoPanel.parentNode) {
    _currentTodoPanel.remove();
  }
  _currentTodoPanel = null;
  // 重置状态，模拟新一轮流式输出的初始条件
  currentBotMsgEl = null;
  currentStepsEl = null;
  currentFinalContent = '';
  totalSteps = 0;
  hasToolCalls = false;
  generatingBadgeEl = null;
  _isReplaying = true;  // 不会发起 WebSocket 等实时连接

  // ── 创建 .agent-response 卡片外壳（与实时输出结构一致）──
  var hasSteps = steps && steps.length > 0;
  var responseCard = null;
  var bodyEl = null;

  if (hasSteps || content) {
    responseCard = document.createElement('div');
    responseCard.className = 'agent-response finished collapsed';
    // 历史消息：耗时使用传入的估算值（有值显示，无值显示 —）
    var timeVal = (elapsedMs && elapsedMs > 0) ? formatElapsed(elapsedMs) : '\u2014';
    var headerEl = document.createElement('div');
    headerEl.className = 'agent-header';
    headerEl.innerHTML =
      '<div class="agent-avatar">\uD83E\uDD16</div>' +
      '<span class="agent-toggle-arrow">\u25B6</span>' +
      '<span class="agent-time"><span class="agent-time-label">' + (t('workElapsed') || '工作耗时') + ': </span> <span class="agent-time-val">' + timeVal + '</span></span>';
    headerEl.onclick = function() {
      responseCard.classList.toggle('collapsed');
    };
    responseCard.appendChild(headerEl);

    if (hasSteps) {
      bodyEl = document.createElement('div');
      bodyEl.className = 'agent-body';
      responseCard.appendChild(bodyEl);
      // 关键：将 currentStepsEl 指向 bodyEl，使 ensureStepsContainer() 复用它而非创建旧 steps-container
      currentStepsEl = bodyEl;
    }

    container.appendChild(responseCard);
  }

  // 先重放 steps（生成 🤔 思考块 / 🔧 工具卡片），顺序与实时流式一致：
  // [用户消息] → [步骤容器：思考块/工具卡片] → [最终答案]
  if (hasSteps) {
    steps.forEach(data => {
      handleStreamEvent(data);
    });
  }

  _isReplaying = false;

  // 重放结束后，把最终答案 content 渲染为 agent-final-output（在卡片内）
  if (content) {
    const ans = document.createElement('div');
    ans.className = 'agent-final-output';
    ans.innerHTML = renderMarkdown(content);
    currentBotMsgEl = ans;
    if (responseCard) {
      responseCard.appendChild(ans);
    } else {
      // 无步骤也无卡片外壳时降级为旧格式
      ans.className = 'msg bot';
      container.appendChild(ans);
    }
  }

  // 渲染 todo 清单（置于答案之前）
  if (todoList) {
    renderTodoPanel(todoList, false);
  }
  if (currentBotMsgEl && _currentTodoPanel) {
    if (responseCard) {
      responseCard.insertBefore(_currentTodoPanel, currentBotMsgEl);
    } else {
      container.insertBefore(_currentTodoPanel, currentBotMsgEl);
    }
  }

  // 清理回放步骤后残留的"执行中/分析中"状态
  document.querySelectorAll('.tool-status-dot.running').forEach(d => {
    d.className = 'tool-status-dot done';
  });
  document.querySelectorAll('.thinking-step').forEach(el => el.remove());
  // 折叠所有工具卡片（默认收起，用户可展开查看详情）
  document.querySelectorAll('.tool-card.open').forEach(card => {
    card.classList.remove('open');
  });
  removeGeneratingBadge();

  // 清空引用
  currentBotMsgEl = null;
  currentStepsEl = null;
}

async function switchSession(sessionId, source, forceLoad = false) {
  // 切换会话时退出文件浏览器视图（若有）
  if (typeof exitFileBrowser === 'function') exitFileBrowser();
  source = source || 'web';
  const targetKey = sessionId + '_' + source;
  if (sessionId === currentSessionId && source === currentSessionSource && !forceLoad) return;

  // 先把「之前可见会话」的 live 关掉（它仍在后台跑的话继续累积事件，只是不再渲染到可见区）
  setVisibleSessionKey(targetKey);
  currentSessionId = sessionId;
  currentSessionSource = source;
  threadId = sessionId;
  // 切走时清理上一可见会话遗留的全局「思考中/生成中/空闲监测」指示器（它们不属于新会话）
  if (typeof hideTyping === 'function') hideTyping();
  if (typeof removeGeneratingBadge === 'function') removeGeneratingBadge();
  if (typeof stopStreamIdleWatch === 'function') stopStreamIdleWatch();

  // 更新激活样式（同时兼容旧 .session-item 与新 .psession-item）
  document.querySelectorAll('.session-item, .psession-item').forEach(el => {
    el.classList.toggle('active', el.dataset.key === targetKey);
  });

  // 在选中的会话项上展示 loading 动画
  const activeItem = document.querySelector('.session-item.active, .psession-item.active');
  if (activeItem) activeItem.classList.add('loading');

  try {
    const rt = sessionRuntimes.get(targetKey);
    if (rt && rt.status === 'streaming') {
      // ── 正在后台运行的会话：先加载历史消息，再重建实时画面并续接 ──
      await reconstructStreamingSession(rt);
    } else {
      // 加载该会话的历史消息（内部已立即清空旧消息）
      await loadSessionMessages(sessionId, source, { limit: 20, offset: -20 });
      refreshStats();
      // 加载该会话的工作目录
      loadWorkspaceDisplay();
    }
  } finally {
    // 无论成功失败都移除 loading 状态
    if (activeItem) activeItem.classList.remove('loading');
    updateRunIndicators();
    // 同步发送按钮 / 全局 streamingActive 到「当前可见会话」的真实状态
    if (typeof syncStreamingActive === 'function') syncStreamingActive();
  }
}

// 依据各会话 runtime 状态，给侧边栏会话项加上/去掉「正在执行」指示器（spinner）
function updateRunIndicators() {
  document.querySelectorAll('.session-item, .psession-item').forEach(el => {
    const rt = sessionRuntimes.get(el.dataset.key);
    el.classList.toggle('running', !!(rt && rt.status === 'streaming'));
  });
}

// 切回一个「正在后台运行」的会话：
// 1. 先加载该会话的历史消息（之前的对话轮次）
// 2. 再在历史消息之上重建本轮流式输出卡片骨架
// 3. 回放已缓冲的 SSE 事件重建实时画面
// 4. 继续接收该会话的实时事件（rt.live=true）
async function reconstructStreamingSession(rt) {
  const container = document.getElementById('messages');
  // ── 第一步：加载历史消息（含之前所有对话轮次）──
  await loadSessionMessages(rt.sessionId, rt.source, { limit: 20, offset: -20 });
  // loadSessionMessages 内部已清空容器并渲染历史消息，且做了令牌校验防串会话

  // ── 第二步：在历史消息之上，重建本轮流式输出卡片骨架 ──
  beginRoundRender(rt);
  rt.live = true;
  // ── 第三步：回放缓冲事件 ──
  // 但不设 _isReplaying，以便实时子连接（子代理 EventSource / Python 轮询）在需要时正常建立。
  _isReconstructing = true;
  try {
    rt.events.forEach(ev => {
      try { _restoreRoundState(rt); handleStreamEvent(ev); _saveRoundState(rt); } catch (e) { console.warn('[reconstruct] 跳过事件', ev.type, e.message); }
    });
  } finally {
    _isReconstructing = false;
  }
  // 切回后续接实时事件：重启空闲监测（切走时已停掉）
  if (typeof startStreamIdleWatch === 'function') startStreamIdleWatch();
  // 保证切回时自动跳到最新输出（用户明确要求的体验）
  container.scrollTop = container.scrollHeight;
  smartScroll(container);
  // 之后的实时事件会在 send() 循环中因 rt.live===true 直接渲染进可见区
}

async function newSession() {
  try {
    const res = await fetch('/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: '' }),
    });
    if (!res.ok) return;
    const data = await res.json();
    currentSessionId = data.id;
    currentSessionSource = 'web';
    threadId = data.id;
    // 标记新会话为可见渲染目标（同时把之前可见会话的 live 关掉，使其后台继续运行不渲染）
    setVisibleSessionKey(data.id + '_web');
    // 作废任何仍在途的旧会话加载请求(防止其晚到回写上一会话内容)
    ++_sessionLoadToken;
    // 重置分页状态, 防止滚动监听器用旧会话的 sessionId 继续往上翻页加载旧消息
    _sessionPageState = null;
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

// 会话消息滚动加载更多（往上滚动时加载更早的消息）
let _sessionPageState = null;
let _sessionLoadingMore = false;

function _attachSessionScrollLoader() {
  const container = document.getElementById('messages');
  if (!container) return;
  container.removeEventListener('scroll', _onSessionScroll);
  container.addEventListener('scroll', _onSessionScroll);
}

function _onSessionScroll() {
  const container = document.getElementById('messages');
  console.log('[scroll] fired', {
    hasContainer: !!container,
    hasState: !!_sessionPageState,
    loadingMore: _sessionLoadingMore,
    scrollTop: container ? container.scrollTop : null,
    hasMore: _sessionPageState ? _sessionPageState.hasMore : null,
  });
  if (!container || !_sessionPageState || _sessionLoadingMore) return;
  // 当用户往上滚动，且距离顶部小于 100px 时，加载更早的消息
  if (container.scrollTop < 100 && _sessionPageState.hasMore) {
    console.log('[scroll] trigger load older', {
      scrollTop: container.scrollTop,
      hasMore: _sessionPageState.hasMore,
      loadedCount: _sessionPageState.loadedCount,
      totalCount: _sessionPageState.totalCount,
      offset: _sessionPageState.offset,
    });
    _loadOlderMessages();
  }
}

async function _loadOlderMessages() {
  if (!_sessionPageState || _sessionLoadingMore) return;
  _sessionLoadingMore = true;
  const { sessionId, source, limit, loadedCount, totalCount } = _sessionPageState;
  // 从末尾往回取：用 totalCount 计算更早消息的起始 offset，避免重复。
  // 边界：当剩余未加载的老消息不足一页时，请求量收敛为实际剩余条数 requestedLimit，
  // 否则会越过开头、与已加载的窗口重叠——表现为“滚动到顶部又取一整页”。
  const remaining = totalCount - loadedCount;
  const requestedLimit = Math.max(0, Math.min(limit, remaining));
  const newOffset = Math.max(0, totalCount - loadedCount - requestedLimit);
  try {
    const qs = source ? `?source=${encodeURIComponent(source)}&include=lite&limit=${requestedLimit}&offset=${newOffset}` : `?include=lite&limit=${requestedLimit}&offset=${newOffset}`;
    const res = await fetch(`/sessions/${sessionId}/messages/lite${qs}`);
    if (!res.ok) return;
    const data = await res.json();
    if (!data.messages || data.messages.length === 0) {
      // 没有更多消息（含边界：剩余为负等异常情况），停止继续向上加载，避免反复触发
      _sessionPageState.hasMore = false;
      return;
    }

    // 在消息列表顶部插入更早的消息
    const container = document.getElementById('messages');
    const firstExisting = container.firstChild;
    // 临时保存当前 scrollHeight，以便插入后保持滚动位置
    const prevScrollHeight = container.scrollHeight;
    const prevScrollTop = container.scrollTop;

    // 用数组暂存新消息元素，再统一插入到顶部
    const newEls = [];
    data.messages.forEach((msg) => {
      const role = msg.role === 'user' ? 'user' : 'bot';
      const content = msg.content || '';
      const parsed = role === 'user' ? parseTextFilesFromContent(content) : null;
      const msgIndex = msg.index != null ? msg.index : idx;
      let newEl = null;
      if (role === 'user' && parsed && parsed.files.length) {
        newEl = addUserMessage(parsed.message, parsed.files.map(f => ({ name: f.name, mime_type: 'text/plain', content: f.content })), msgIndex);
      } else if (role === 'user' && msg.has_images) {
        newEl = addUserMessageLazyImages(content, msg.image_count, sessionId, msgIndex, source);
      } else if (role === 'bot' && msg.has_steps) {
        // 估算 bot 耗时
        var botElapsed = 0;
        if (msg.timestamp) {
          const prevUser = _msgHistory.length > 0 ? new Date(_msgHistory[_msgHistory.length - 1]).getTime() : 0;
          try { botElapsed = new Date(msg.timestamp).getTime() - prevUser; } catch(e){}
        }
        newEl = addBotMessagePlaceholder(content, msg.content_preview, botElapsed, sessionId, msgIndex);
      } else if (role === 'bot') {
        newEl = addMessage(content || msg.content_preview || '', 'bot', msgIndex);
      } else {
        newEl = addMessage(content, role, msgIndex);
      }
      if (newEl) newEls.push(newEl);
    });

    // 将新消息统一插入到现有消息顶部
    if (newEls.length > 0 && firstExisting) {
      const fragment = document.createDocumentFragment();
      newEls.forEach(el => fragment.appendChild(el));
      container.insertBefore(fragment, firstExisting);
    }

    // 更新分页状态
    _sessionPageState.offset = newOffset;
    _sessionPageState.hasMore = data.has_more;
    _sessionPageState.loadedCount += data.messages.length;

    // 保持滚动位置：用户往上滚时，新内容插在顶部，不应把视口往下推
    requestAnimationFrame(() => {
      const newScrollHeight = container.scrollHeight;
      container.scrollTop = prevScrollTop + (newScrollHeight - prevScrollHeight);
    });
  } catch (e) {
    console.error('[loadOlderMessages] failed:', e);
  } finally {
    _sessionLoadingMore = false;
  }
}

// ---------- 消息删除菜单（长按 / 右键） ----------
let _msgDeleteMenu = null;
let _msgDeleteTarget = null;
let _msgDeletePressTimer = null;
let _msgDeletePressStart = null;

function hideMessageDeleteMenu() {
  if (_msgDeleteMenu && _msgDeleteMenu.parentNode) {
    _msgDeleteMenu.remove();
  }
  _msgDeleteMenu = null;
  _msgDeleteTarget = null;
}

function showMessageDeleteMenuFor(el, x, y) {
  hideMessageDeleteMenu();
  const index = el.dataset.index;
  if (index === undefined || index === null || index === '') return;
  _msgDeleteTarget = el;

  const menu = document.createElement('div');
  menu.className = 'msg-delete-menu show';
  menu.innerHTML = '<div class="msg-delete-item">🗑 删除消息</div>';
  menu.querySelector('.msg-delete-item').addEventListener('click', () => {
    hideMessageDeleteMenu();
    deleteMessageByIndex(index);
  });
  document.body.appendChild(menu);
  _msgDeleteMenu = menu;

  const rect = menu.getBoundingClientRect();
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  let left = x;
  let top = y;
  if (left + rect.width > vw - 8) left = vw - rect.width - 8;
  if (top + rect.height > vh - 8) top = vh - rect.height - 8;
  if (left < 8) left = 8;
  if (top < 8) top = 8;
  menu.style.left = left + 'px';
  menu.style.top = top + 'px';
}

function deleteMessageByIndex(index) {
  if (!currentSessionId || !currentSessionSource) return;
  const confirmed = confirm(t('deleteMessageConfirm'));
  if (!confirmed) return;
  const url = `/sessions/${currentSessionId}/messages/${index}?source=${encodeURIComponent(currentSessionSource || 'web')}`;
  fetch(url, { method: 'DELETE' })
    .then(r => r.ok ? r.json() : Promise.reject(r))
    .then(data => {
      const target = _msgDeleteTarget;
      if (target && target.parentNode) target.remove();
      loadSessions();
      if (typeof refreshStats === 'function') refreshStats();
      if (currentSessionId && typeof loadSessionMessages === 'function') {
        loadSessionMessages(currentSessionId, currentSessionSource, { limit: 20, offset: -20 });
      }
      if (data && data.message) {
        if (typeof addMessage === 'function') addMessage(t('deleteMessageSuccess'), 'system');
      }
    })
    .catch(() => {
      if (typeof addMessage === 'function') addMessage(t('deleteMessageFailed'), 'system');
    });
}

messages.addEventListener('touchstart', (e) => {
  const msgEl = e.target.closest('.msg, .agent-response');
  if (!msgEl) return;
  if (msgEl.dataset.index === undefined || msgEl.dataset.index === null || msgEl.dataset.index === '') return;
  _msgDeletePressStart = { x: e.touches[0].clientX, y: e.touches[0].clientY, el: msgEl, time: Date.now() };
  _msgDeletePressTimer = setTimeout(() => {
    const rect = msgEl.getBoundingClientRect();
    showMessageDeleteMenuFor(msgEl, rect.left, rect.top);
    _msgDeletePressStart = null;
  }, 600);
}, { passive: true });

messages.addEventListener('touchmove', (e) => {
  if (!_msgDeletePressStart || !_msgDeletePressTimer) return;
  const dx = Math.abs((e.touches[0].clientX || 0) - _msgDeletePressStart.x);
  const dy = Math.abs((e.touches[0].clientY || 0) - _msgDeletePressStart.y);
  if (dx > 10 || dy > 10) {
    clearTimeout(_msgDeletePressTimer);
    _msgDeletePressTimer = null;
    _msgDeletePressStart = null;
  }
}, { passive: true });

messages.addEventListener('touchend', () => {
  if (_msgDeletePressTimer) {
    clearTimeout(_msgDeletePressTimer);
    _msgDeletePressTimer = null;
  }
  _msgDeletePressStart = null;
});

messages.addEventListener('contextmenu', (e) => {
  const msgEl = e.target.closest('.msg, .agent-response');
  if (!msgEl) return;
  if (msgEl.dataset.index === undefined || msgEl.dataset.index === null || msgEl.dataset.index === '') return;
  e.preventDefault();
  showMessageDeleteMenuFor(msgEl, e.clientX, e.clientY);
});

document.addEventListener('click', (e) => {
  if (_msgDeleteMenu && !_msgDeleteMenu.contains(e.target)) {
    hideMessageDeleteMenu();
  }
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') hideMessageDeleteMenu();
});
