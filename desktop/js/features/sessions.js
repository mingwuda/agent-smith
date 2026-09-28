/* sessions.js — 会话列表渲染、加载、切换、新建、删除、侧边栏
   依赖: state.js, util.js, i18n.js, streaming.js(handleStreamEvent, removeGeneratingBadge),
         messaging.js(addMessage, addUserMessage), stats.js(refreshStats) */

// ---------- 会话管理 ----------

let currentSessionSource = '';  // 当前会话来源: "web" / "wechat" / ""
let _sessionLoadToken = 0;      // 会话加载请求令牌: 单调递增, 仅最后一次 loadSessionMessages 可写回 DOM(防切/建会话时旧请求晚到回写)

function _sessionKey(s) {
  // source 为空时与 getOrCreateRuntime 保持一致，避免侧边栏 data-key 和 runtime key 不匹配
  return (s.id || '') + '_' + (s.source || 'web');
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
          var placeholderEl = addBotMessagePlaceholder(content, msg.content_preview, botElapsed, sessionId, msgIndex, msg.model);
          if (placeholderEl) container.appendChild(placeholderEl);
        } else if (role === 'bot') {
          addMessage(content || msg.content_preview || '', 'bot', msgIndex, { model: msg.model, timestamp: msg.timestamp });
        } else {
          addMessage(content, role, msgIndex, { timestamp: msg.timestamp });
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
function addBotMessagePlaceholder(content, contentPreview, elapsedMs, sessionId, messageIndex, model) {
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
  // ponytail: header 除「工作耗时」外，回放时还展示该条回复使用的模型名（旧消息无此字段则不显示）。
  var modelHtml = model ? '<span class="agent-model" title="' + escapeHtml(String(model)) + '">' + escapeHtml(String(model)) + '</span>' : '';
  headerEl.innerHTML =
    '<div class="agent-avatar">\uD83E\uDD16</div>' +
    '<span class="agent-toggle-arrow">\u25B6</span>' +
    '<span class="agent-time"><span class="agent-time-label">' + (t('workElapsed') || '工作耗时') + ': </span> <span class="agent-time-val">' + timeVal + '</span></span>' +
    modelHtml;
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
    attachCopyButton(ans);
    attachFeedbackBar(ans);
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

    // 先删掉卡片上已有的最终输出（占位创建时或上一次展开追加的），
    // 否则每次「展开→折叠→再展开」都会累积复制一份最终输出。
    responseCard.querySelectorAll(':scope > .agent-final-output').forEach(el => el.remove());

    const ans = document.createElement('div');
    ans.className = 'agent-final-output';
    ans.innerHTML = renderMarkdown(content);
    attachCopyButton(ans);
    attachFeedbackBar(ans);
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
    attachCopyButton(ans);
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

  // ── 多页签：把目标会话放入/激活页签容器（DOM 常驻切换，内容与滚动不丢）──
  // open() 返回 true=新打开（需走下方加载/重建），false=已在页签中（仅切激活，保留内容）。
  // open 内部会确保当前激活的 .msg-panel 持有 id="messages"，并同步 currentSessionId/threadId。
  let tabAlreadyOpen = false;
  if (window.ChatTabs) {
    const title = (sessionsCache.find(s => (s.id + '_' + (s.source || 'web')) === targetKey) || {})['title']
      || ((sessionsCache.find(s => s.id === sessionId) || {})['title']) || sessionId;
    if (forceLoad && window.ChatTabs.isOpen(targetKey)) {
      // 强制刷新：仅激活（容器固定），但标记为需刷新，走下方全新加载分支
      window.ChatTabs.open(targetKey, title);
      tabAlreadyOpen = false;
    } else {
      tabAlreadyOpen = !window.ChatTabs.open(targetKey, title);
    }
  }

  // 记录旧可见会话 key（setVisibleSessionKey 会覆盖它）
  const prevVisibleKey = visibleSessionKey;

  // 先把「之前可见会话」的 live 关掉（它仍在后台跑的话继续累积事件，只是不再渲染到可见区）
  setVisibleSessionKey(targetKey);

  // ── 多页签：若目标会话已在页签中（非强制刷新），仅切激活、同步状态后直接返回，不重载 ──
  if (window.ChatTabs && tabAlreadyOpen) {
    if (typeof syncStreamingActive === 'function') syncStreamingActive();
    if (typeof updateRunIndicators === 'function') updateRunIndicators();
    if (typeof refreshStats === 'function') refreshStats();
    return;
  }

  currentSessionId = sessionId;
  currentSessionSource = source;
  threadId = sessionId;
  // 切走时清理上一可见会话遗留的全局「思考中/生成中/空闲监测」指示器（它们不属于新会话）
  if (typeof hideTyping === 'function') hideTyping();
  if (typeof removeGeneratingBadge === 'function') removeGeneratingBadge();
  if (typeof stopStreamIdleWatch === 'function') stopStreamIdleWatch();

  // 内存回收: 旧可见会话若已完成(done/error), 不再需要保留其 runtime
  if (prevVisibleKey && prevVisibleKey !== targetKey && typeof sessionRuntimes !== 'undefined') {
    const prevRt = sessionRuntimes.get(prevVisibleKey);
    if (prevRt && (prevRt.status === 'done' || prevRt.status === 'error')) {
      sessionRuntimes.delete(prevVisibleKey);
    }
  }

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
      // 刷新恢复：若该会话在后台仍有正在跑的 run（agent 流不随连接断开而终止），
      // 回放已落盘的 SSE 事件并轮询增量续看实时画面。
      if (typeof resumeActiveStream === 'function') {
        resumeActiveStream(sessionId, source).catch(function(e) {
          console.warn('[resume] 恢复后台运行流失败:', e);
        });
      }
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

  // ── 第一步后处理：移除最后一条 bot 消息的历史占位卡片 ──
  // 原因: 历史加载对本轮 bot 响应创建了折叠的 agent-response.finished 卡片(内容可能被
  // lite 接口截断至 4000 字符), 但接下来 beginRoundRender + 回放会用完整的 SSE 缓冲事件
  // 重建一个全新的、内容完整的卡片。不删会导致: ①重复渲染(两个卡片) ②上面那个显示截断内容。
  const allFinished = container.querySelectorAll('.agent-response.finished');
  if (allFinished.length > 0) {
    allFinished[allFinished.length - 1].remove();
  }

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

// ---------- 刷新后恢复后台仍在跑的 run（SSE 流已实时落盘，从文件恢复） ----------
// 轮询间隔（ms）：agent 后台跑时前端没有 live 连接，用短轮询增量续看。
var _resumePollTimers = {};  // key(sessionId_source) -> 轮询 timer

// 回放一批事件（带 _isReconstructing 标记，使 token 得以渲染），跳过已回放前 n 条
function _replayEvents(rt, events, fromIndex) {
  _isReconstructing = true;
  try {
    for (var i = fromIndex; i < events.length; i++) {
      try {
        _restoreRoundState(rt);
        handleStreamEvent(events[i]);
        _saveRoundState(rt);
      } catch (e) {
        console.warn('[resume] 跳过事件', events[i] && events[i].type, e.message);
      }
    }
  } finally {
    _isReconstructing = false;
  }
  var c = document.getElementById('messages');
  if (c) { c.scrollTop = c.scrollHeight; }
}

// 刷新浏览器后：查该会话是否有在后台跑的 run，有则回放已落盘事件并轮询增量续看
async function resumeActiveStream(sessionId, source) {
  source = source || 'web';
  var key = sessionId + '_' + source;
  // 已有该会话的 live 运行（用户切回时 send 仍在跑）则不重复接管
  var rt0 = sessionRuntimes.get(key);
  if (rt0 && rt0.status === 'streaming') return;
  // 防止同会话并发接管（例如 30s 轮询 loadSessions 再次触发）
  if (_resumePollTimers[key]) return;

  var res = await fetch('/sessions/' + encodeURIComponent(sessionId) + '/stream/active');
  if (!res.ok) return;
  var data = await res.json();
  if (!data.active || !data.message_id) return;

  // running=false：本轮已结束（历史可能刚由后台落库），刷新历史列表即可，无需重建画面
  if (!data.running) {
    if (typeof loadSessions === 'function') loadSessions();
    return;
  }

  // running=true：后台 agent 仍在跑，接管实时画面
  rt0 = getOrCreateRuntime(sessionId, source);
  rt0.status = 'streaming';
  rt0.controller = new AbortController();  // resume 无真实 fetch，但保留 controller 供语义一致；真实收尾在 _resumeStop
  rt0.events = [];
  rt0.live = true;
  setVisibleSessionKey(key);
  _resumePollTimers[key] = true;  // 占位，防并发
  startStreamIdleWatch && startStreamIdleWatch();
  // 建三段式骨架（同时初始化本轮全局状态）
  beginRoundRender(rt0);
  updateRunIndicators && updateRunIndicators();
  syncStreamingActive && syncStreamingActive();

  var replayed = 0;
  // 回放已落盘事件
  _replayEvents(rt0, data.events || [], 0);
  replayed = (data.events || []).length;

  var stopped = false;
  function stop() {
    if (stopped) return;
    stopped = true;
    delete _resumePollTimers[key];
    // 收尾：本轮 done/error 已由回放触发，清理可见区 UI 状态并还原发送按钮
    if (typeof _finalizeReasoning === 'function') _finalizeReasoning();
    if (typeof _finalizeThinking === 'function') _finalizeThinking();
    hideTyping && hideTyping();
    removeGeneratingBadge && removeGeneratingBadge();
    document.querySelectorAll('.tool-status-dot.running').forEach(function(d) { d.className = 'tool-status-dot done'; });
    if (_currentActiveLine) _currentActiveLine.style.display = 'none';
    rt0.status = 'done';
    rt0.live = false;
    syncStreamingActive && syncStreamingActive();
    updateRunIndicators && updateRunIndicators();
    if (typeof loadSessions === 'function') loadSessions();
  }

  // 供 stopCurrentRun（发送按钮→停止）调用：resume 接管场景没有真实 fetch 可 abort，
  // 只能通过这里停轮询 + 还原按钮 + 结束该前台 run。后台 agent 任务仍会跑完（与 HTTP 解耦）。
  rt0._resumeStop = stop;

  var poll = function() {
    if (stopped) return;
    fetch('/sessions/' + encodeURIComponent(sessionId) + '/stream/active')
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (stopped) return;
        // 会话已切走（不再是可见渲染目标）：不再回放，但保持后台运行，切回时重新接管
        if (visibleSessionKey !== key) {
          setTimeout(poll, 2000);
          return;
        }
        var evs = (d && d.events) || [];
        if (evs.length > replayed) {
          _replayEvents(rt0, evs, replayed);
          replayed = evs.length;
        }
        if (d && d.finished) {
          stop();
        } else {
          setTimeout(poll, 2000);
        }
      })
      .catch(function() {
        // 网络抖动：短暂重试
        if (!stopped) setTimeout(poll, 3000);
      });
  };
  // 首拍延迟 1.5s，给后台 agent 一点时间产生新事件
  setTimeout(poll, 1500);
}

// ---------- 同页 SSE 断连自动接管（锁屏 / 切后台 / 网络抖动）----------
// 后端 agent 跑在与 HTTP 连接解耦的后台 driver 里，断的只是浏览器这条订阅连接，
// 任务仍在服务器继续并实时落盘。这里复用 switchSession 非流式分支同款两步：
//   1) loadSessionMessages 清空重建历史（顺带移除断连的半成品卡片）
//   2) resumeActiveStream 接管后台 run（running=true 重建实时卡片并轮询；
//      running=false 时历史已含完整结果，只需刷新列表，不重复建卡）
// 返回 true=后端确认本轮 run 存在（画面已重建/刷新）；false=无 run（真断网/服务重启），调用方可提示中断。
var _recoverInflight = {};  // key -> Promise，同步去重，避免 reader 报错与 visibilitychange 并发双接管
function recoverInterruptedStream(sessionId, source) {
  source = source || 'web';
  var key = sessionId + '_' + source;
  if (_recoverInflight[key]) return _recoverInflight[key];

  var p = (async function() {
    // 先探测后端是否还有本轮 run（落盘）。探测失败（真断网）→ false，让调用方提示中断。
    var data;
    try {
      var res = await fetch('/sessions/' + encodeURIComponent(sessionId) + '/stream/active');
      if (!res.ok) return false;
      data = await res.json();
    } catch (e) {
      return false;
    }
    if (!data || !data.active || !data.message_id) return false;
    // 断连期间用户已切走会话：不抢画面（切回时 switchSession 会自行接管）
    if (visibleSessionKey !== key) return true;

    // 第一步：重载历史（内部清空容器，移除断连的半成品卡片）
    await loadSessionMessages(sessionId, source, { limit: 20, offset: -20 });
    if (visibleSessionKey !== key) return true;
    // 第二步：仍在跑则重建实时卡片并轮询续看；已跑完则历史已含完整结果，仅刷新列表
    if (data.running && typeof resumeActiveStream === 'function') {
      await resumeActiveStream(sessionId, source);
    } else if (typeof loadSessions === 'function') {
      loadSessions();
    }
    if (typeof refreshStats === 'function') refreshStats();
    return true;
  })();

  _recoverInflight[key] = p;
  p.then(function() { delete _recoverInflight[key]; }, function() { delete _recoverInflight[key]; });
  return p;
}

// 锁屏/切后台后回到页面、或网络从离线恢复：若当前可见会话没有健康的 live 连接，
// 自动探测并接管后台仍在跑的 run，无需用户手动刷新。
// 后端约每 2s 一个 ping/progress 心跳；回到页面后若超过该阈值仍无任何事件，
// 视为连接被系统/运营商静默掐断（reader 可能还没来得及 reject），主动 abort 后接管。
var _RESUME_STALE_MS = 15000;
function _autoResumeVisibleSession() {
  if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
  var sid = (typeof currentSessionId !== 'undefined' && currentSessionId) ||
            (typeof threadId !== 'undefined' ? threadId : null);
  if (!sid) return;
  var src = (typeof currentSessionSource !== 'undefined' && currentSessionSource) || 'web';
  var key = sid + '_' + src;
  if (visibleSessionKey !== key) return;
  var rt = sessionRuntimes.get(key);
  if (!rt || rt.status !== 'streaming') return;

  var controllerDead = !rt.controller || (rt.controller.signal && rt.controller.signal.aborted);
  // ponytail: 僵死判定天花板——依赖后端 2s 心跳刷新 lastStreamEventAt；
  // 若未来心跳间隔调整，这里的 15s 阈值需同步放宽（升级路径：改由后端在 SSE 里带时间戳）。
  var stale = (typeof lastStreamEventAt === 'number') &&
              Date.now() - lastStreamEventAt > _RESUME_STALE_MS;
  if (!controllerDead && !stale) return;  // 连接健康，无需恢复

  // 先掐掉旧订阅连接：使其 reader 走 AbortError 静默分支（不弹"连接中断"、不重复接管），
  // 再由本函数主动接管后台 run。
  if (rt.controller && !(rt.controller.signal && rt.controller.signal.aborted)) {
    try { rt.controller.abort(); } catch (e) {}
  }
  recoverInterruptedStream(sid, src).catch(function() {});
}
document.addEventListener('visibilitychange', function() {
  if (document.visibilityState === 'visible') _autoResumeVisibleSession();
});
window.addEventListener('online', _autoResumeVisibleSession);

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
    // 多页签：新会话打开一个页签
    if (window.ChatTabs) {
      window.ChatTabs.open(data.id + '_web', t('newChat') || '新对话');
    }
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
    // 清理该会话所有 source 的 runtime（web/wechat 等）
    if (typeof sessionRuntimes !== 'undefined') {
      for (const key of [...sessionRuntimes.keys()]) {
        if (key.startsWith(sessionId + '_')) sessionRuntimes.delete(key);
      }
    }
    // 多页签：关闭该会话的所有页签
    if (window.ChatTabs) {
      window.ChatTabs.all().forEach(function (t) {
        if (t.key.split('_')[0] === sessionId) window.ChatTabs.close(t.key);
      });
    }
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
let _msgDeleteFromTouch = false;  // 当前菜单是否由触摸长按弹出（用于抬手时屏蔽合成 click）
let _msgDeleteSuppressUntil = 0;  // 该时间戳前的 click 为长按抬手合成事件，需忽略（不删、不关菜单）

function hideMessageDeleteMenu() {
  if (_msgDeleteMenu && _msgDeleteMenu.parentNode) {
    _msgDeleteMenu.remove();
  }
  _msgDeleteMenu = null;
  _msgDeleteTarget = null;
  _msgDeleteFromTouch = false;
}

function showMessageDeleteMenuFor(el, x, y, fromTouch = false) {
  hideMessageDeleteMenu();
  const index = el.dataset.index;
  if (index === undefined || index === null || index === '') return;
  _msgDeleteTarget = el;

  const menu = document.createElement('div');
  menu.className = 'msg-delete-menu show';
  menu.innerHTML = '<div class="msg-delete-item">🗑 删除消息</div>';
  menu.querySelector('.msg-delete-item').addEventListener('click', () => {
    // 长按抬手瞬间菜单处于 pointer-events:none，合成 click 点不到这里；
    // 只有用户在菜单出现后「再点一次」才会真正进入删除确认，杜绝抬手误删。
    hideMessageDeleteMenu();
    deleteMessageByIndex(index);
  });
  document.body.appendChild(menu);
  _msgDeleteMenu = menu;

  // 标记来源为触摸；真正的「抬手屏蔽合成 click」在 touchend 里精确执行
  // （无论手指按住多久，抬手那一下都要盖住）。右键菜单无此合成 click。
  _msgDeleteFromTouch = !!fromTouch;

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

// 多页签：消息删除菜单的长按/右键触发绑定到稳定祖先 #main（激活面板动态切换，直接绑面板会失效）
const _msgHost = document.getElementById('main') || messages;
_msgHost.addEventListener('touchstart', (e) => {
  const msgEl = e.target.closest('.msg, .agent-response');
  if (!msgEl) return;
  if (msgEl.dataset.index === undefined || msgEl.dataset.index === null || msgEl.dataset.index === '') return;
  _msgDeletePressStart = { x: e.touches[0].clientX, y: e.touches[0].clientY, el: msgEl, time: Date.now() };
  _msgDeletePressTimer = setTimeout(() => {
    // 触觉反馈：明确告知「长按已触发」，避免菜单突然弹出造成误操作
    if (navigator.vibrate) { try { navigator.vibrate(15); } catch (_) {} }
    // 菜单锚定在「手指按住的点位」而非消息元素左上角：长消息按在中段时，
    // 用 rect.left/top 会让菜单弹到消息顶部，与用户操作点隔很远（易误点）。
    const press = _msgDeletePressStart;
    const x = press ? press.x : msgEl.getBoundingClientRect().left;
    const y = press ? press.y : msgEl.getBoundingClientRect().top;
    showMessageDeleteMenuFor(msgEl, x, y, true);
    _msgDeletePressStart = null;
  }, 1000);  // 3000→1000ms：长按 1 秒即触发删除菜单（原 3s 偏久，1s 更顺手且仍区别于普通点击/滚动）
}, { passive: true });

_msgHost.addEventListener('touchmove', (e) => {
  if (!_msgDeletePressStart || !_msgDeletePressTimer) return;
  const dx = Math.abs((e.touches[0].clientX || 0) - _msgDeletePressStart.x);
  const dy = Math.abs((e.touches[0].clientY || 0) - _msgDeletePressStart.y);
  if (dx > 10 || dy > 10) {
    clearTimeout(_msgDeletePressTimer);
    _msgDeletePressTimer = null;
    _msgDeletePressStart = null;
  }
}, { passive: true });

_msgHost.addEventListener('touchend', () => {
  if (_msgDeletePressTimer) {
    clearTimeout(_msgDeletePressTimer);
    _msgDeletePressTimer = null;
  }
  _msgDeletePressStart = null;
  // 长按菜单已弹出后的抬手：屏蔽这一下抬手产生的合成 click——
  // 既避免「抬手即点中删除项」，也避免它穿透到下层触发「点外部关闭菜单」。
  // 菜单短暂 pointer-events:none，用户需在菜单出现后「再点一次」才会删除。
  if (_msgDeleteMenu && _msgDeleteFromTouch) {
    const menu = _msgDeleteMenu;
    menu.style.pointerEvents = 'none';
    _msgDeleteSuppressUntil = Date.now() + 380;
    setTimeout(() => {
      if (menu === _msgDeleteMenu) menu.style.pointerEvents = '';
    }, 380);
  }
});

_msgHost.addEventListener('contextmenu', (e) => {
  const msgEl = e.target.closest('.msg, .agent-response');
  if (!msgEl) return;
  if (msgEl.dataset.index === undefined || msgEl.dataset.index === null || msgEl.dataset.index === '') return;
  e.preventDefault();
  showMessageDeleteMenuFor(msgEl, e.clientX, e.clientY);
});

document.addEventListener('click', (e) => {
  // 点击菜单外部：始终立即关闭，不受长按抬手抑制窗口影响。
  // 抑制窗口（_msgDeleteSuppressUntil + pointerEvents:none）只负责防「抬手合成 click
  // 误触发删除」，作用域在菜单项本身；若它也屏蔽「点外部关闭」，会让用户点空白处
  // 菜单卡滞 380ms 才消失（a2dcec8 引入，实测复现），体验极差。
  if (_msgDeleteMenu && !_msgDeleteMenu.contains(e.target)) {
    hideMessageDeleteMenu();
  }
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') hideMessageDeleteMenu();
});
