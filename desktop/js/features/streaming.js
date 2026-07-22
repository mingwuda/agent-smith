/* streaming.js — SSE 流式发送、handleStreamEvent、typing/loading 指示器
   依赖: state.js, util.js, i18n.js, messaging.js(addMessage, addUserMessage, resizeComposer, renderAttachmentPreview) */

// ---------- 全局变量 ----------

var _currentPythonOutEl = null; // 当前 run_python 的实时日志容器
var _lastToolImageHtml = null; // 最近一次工具结果的图片 Markdown

// Agent 三段式输出卡片状态
var _agentStartTime = 0;       // 本轮开始时间戳（用于计算耗时）
var _timerInterval = null;     // 耗时更新定时器
var _currentActiveLine = null; // 当前执行中动作行 DOM
var _currentProgressLine = null; // 当前进度指示行 DOM

// 会话回放标记：加载历史消息时设为 true，避免重复触发工具副作用
var _isReplaying = false;
var _subagentToolStep = null;  // 当前子代理对应的工具调用 step，用于锚定胶囊行位置

// ---------- 耗时格式化 ----------

function formatElapsed(ms) {
  if (ms < 1000) return ms + 'ms';
  var s = Math.floor(ms / 1000);
  if (s < 60) return s + 's';
  var m = Math.floor(s / 60);
  var sec = s % 60;
  return m + 'm ' + (sec < 10 ? '0' : '') + sec + 's';
}

// ---------- Typing / Loading 指示器 ----------

function showTyping() {
  const bar = document.getElementById('loading-bar');
  const label = bar.querySelector('.label');
  if (label) label.textContent = t('thinking');
  bar.classList.add('show');
  smartScroll(messages);
}

function hideTyping() {
  const bar = document.getElementById('loading-bar');
  bar.classList.remove('show');
}

// ---------- 生成中徽章 ----------

function showGeneratingBadge(text = t('generating')) {
  if (!generatingBadgeEl) {
    generatingBadgeEl = document.createElement('div');
    generatingBadgeEl.className = 'generating-badge';
  }
  generatingBadgeEl.innerHTML = `<span class="spin"></span> ${escapeHtml(unescapeDisplay(text))}`;
  if (currentBotMsgEl) {
    currentBotMsgEl.after(generatingBadgeEl);
  } else if (currentStepsEl) {
    currentStepsEl.after(generatingBadgeEl);
  } else {
    messages.appendChild(generatingBadgeEl);
  }
  smartScroll(messages);
}

function removeGeneratingBadge() {
  if (generatingBadgeEl) {
    generatingBadgeEl.remove();
    generatingBadgeEl = null;
  }
}

// ---------- 流式空闲监测 ----------

function markStreamActivity() {
  lastStreamEventAt = Date.now();
}

function startStreamIdleWatch() {
  stopStreamIdleWatch();
  markStreamActivity();
  streamIdleTimer = setInterval(() => {
    if (!streamingActive) return;
    const idleMs = Date.now() - lastStreamEventAt;
    if (idleMs > 1800) {
      if (currentBotMsgEl || currentStepsEl) {
        showGeneratingBadge(t('stillProcessing'));
      } else {
        showTyping();
      }
    }
  }, 800);
}

function stopStreamIdleWatch() {
  if (streamIdleTimer) {
    clearInterval(streamIdleTimer);
    streamIdleTimer = null;
  }
}

// ---------- Python 进度 ----------

function closePythonProgress() {
  if (_pythonProgressSource) {
    _pythonProgressSource.close();
    _pythonProgressSource = null;
  }
  // 如果 outEl 仍在显示等待提示，将其更新为已结束
  if (_currentPythonOutEl) {
    var waitingText = _currentPythonOutEl.textContent;
    if (waitingText === '等待输出...' || waitingText === '等待输出中...（执行完成后会自动显示结果）') {
      _currentPythonOutEl.textContent = '（无实时输出）';
    }
    _currentPythonOutEl = null;
  }
}

// ---------- 停止当前运行 ----------

function stopCurrentRun() {
  // 只停止「当前可见会话」的运行（多会话并发时，其它后台会话不受影响）
  const rt = sessionRuntimes.get(visibleSessionKey);
  if (!rt || rt.status !== 'streaming' || !rt.controller) return;
  userStoppedCurrentRun = true;
  rt.controller.abort();
  addMessage(t('runStopRequested'), 'system');
  sendBtn.innerHTML = '<svg width="16" height="16" viewBox="0 0 16 16"><rect x="3" y="3" width="10" height="10" rx="2" fill="currentColor"/></svg>';
  sendBtn.disabled = true;
}

// ---------- 核心 SSE 发送 ----------

// 全局回合状态（handleStreamEvent 内部 80+ 处引用的 11 个隐式全局变量）。
// 多会话并发时这些变量不能共享——每个会话必须各自持有一份快照，
// 在渲染该会话事件前恢复、渲染后保存。
function _saveRoundState(rt) {
  rt._rs = rt._rs || {};
  rt._rs.currentStepsEl = currentStepsEl;
  rt._rs.currentBotMsgEl = currentBotMsgEl;
  rt._rs.currentFinalContent = currentFinalContent;
  rt._rs.totalSteps = totalSteps;
  rt._rs.hasToolCalls = hasToolCalls;
  rt._rs.generatingBadgeEl = generatingBadgeEl;
  rt._rs._agentStartTime = _agentStartTime;
  rt._rs._currentActiveLine = _currentActiveLine;
  rt._rs._currentProgressLine = _currentProgressLine;
  rt._rs._subagentToolStep = _subagentToolStep;
  rt._rs._lastToolImageHtml = _lastToolImageHtml;
}

function _restoreRoundState(rt) {
  if (!rt || !rt._rs) return;
  currentStepsEl = rt._rs.currentStepsEl;
  currentBotMsgEl = rt._rs.currentBotMsgEl;
  currentFinalContent = rt._rs.currentFinalContent;
  totalSteps = rt._rs.totalSteps;
  hasToolCalls = rt._rs.hasToolCalls;
  generatingBadgeEl = rt._rs.generatingBadgeEl;
  _agentStartTime = rt._rs._agentStartTime;
  _currentActiveLine = rt._rs._currentActiveLine;
  _currentProgressLine = rt._rs._currentProgressLine;
  _subagentToolStep = rt._rs._subagentToolStep;
  _lastToolImageHtml = rt._rs._lastToolImageHtml;
}

// 创建新一轮「三段式」输出卡片骨架，并初始化本轮所需的全局回合状态。
// 既被 send()（发起新请求）调用，也被 reconstructStreamingSession()（切回后台运行会话）调用，
// 两者共用同一套骨架，保证可见区与后台缓冲重建出的画面结构一致。
function beginRoundRender(rt) {
  currentStepsEl = null;
  currentBotMsgEl = null;
  currentFinalContent = '';
  totalSteps = 0;
  hasToolCalls = false;
  generatingBadgeEl = null;
  _agentStartTime = Date.now();
  _currentActiveLine = null;
  _currentProgressLine = null;
  _subagentToolStep = null;
  _lastToolImageHtml = null;  // 清空上一轮残留的工具截图，避免串入本轮最终输出

  const container = document.getElementById('messages');
  var responseCard = document.createElement('div');
  responseCard.className = 'agent-response';
  responseCard.dataset.roundId = 'r-' + Date.now() + '-' + (rt ? rt.key : 'x');

  // 第一行：头像 + 耗时
  var headerEl = document.createElement('div');
  headerEl.className = 'agent-header';
  headerEl.innerHTML =
    '<div class="agent-avatar">🤖</div>' +
    '<span class="agent-toggle-arrow">▶</span>' +
    '<span class="agent-time"><span class="agent-time-label">工作耗时:</span> <span class="agent-time-val">0s</span></span>';
  headerEl.onclick = function() {
    responseCard.classList.toggle('collapsed');
  };
  responseCard.appendChild(headerEl);

  // 展开区：思考+工具调用
  var bodyEl = document.createElement('div');
  bodyEl.className = 'agent-body';
  responseCard.appendChild(bodyEl);

  // 第二行：当前动作（初始隐藏）
  var activeLineEl = document.createElement('div');
  activeLineEl.className = 'agent-active-line';
  activeLineEl.style.display = 'none';
  responseCard.appendChild(activeLineEl);
  _currentActiveLine = activeLineEl;

  // 第三行：进度指示（初始隐藏）
  var progressLineEl = document.createElement('div');
  progressLineEl.className = 'agent-progress-line';
  progressLineEl.style.display = 'none';
  progressLineEl.innerHTML = '<span class="agent-progress-spinner"></span><span>' + escapeHtml(t('agentPlanning') || 'Agent 正在规划与执行...') + '</span>';
  responseCard.appendChild(progressLineEl);
  _currentProgressLine = progressLineEl;

  container.appendChild(responseCard);
  currentStepsEl = bodyEl;

  // 物理移除上一轮/上一会话的 todo 面板，避免跨会话泄漏
  if (_currentTodoPanel && _currentTodoPanel.parentNode) {
    _currentTodoPanel.remove();
  }
  _currentTodoPanel = null;

  // 启动耗时计时器（挂在 runtime 上，避免多会话并发时互相清掉对方的定时器）
  if (rt && rt._timerInterval) clearInterval(rt._timerInterval);
  rt._timerInterval = setInterval(function() {
    var elapsed = Date.now() - _agentStartTime;
    var valEl = responseCard.querySelector('.agent-time-val');
    if (valEl) {
      valEl.textContent = formatElapsed(elapsed);
    }
  }, 500);

  // 把刚初始化的全局回合状态快照到 runtime（多会话并发隔离关键）
  _saveRoundState(rt);

  return responseCard;
}

// 根据「当前可见会话 runtime」的实际状态，派生全局 streamingActive / isLoading，并同步发送按钮。
// 多会话并发时，这些全局量只代表「可见会话」的状态，而不是任意一个后台会话。
function syncStreamingActive() {
  const rt = sessionRuntimes.get(visibleSessionKey);
  streamingActive = !!(rt && rt.status === 'streaming');
  isLoading = streamingActive;
  setSendButtonRunning(streamingActive);
}

async function send() {
  const text = input.value.trim();
  const attachments = pendingAttachments.slice();
  if ((!text && attachments.length === 0)) return;

  // 目标会话 = 当前可见会话（用户始终对正在看的会话发请求）
  const targetSessionId = currentSessionId || threadId;
  const targetSource = currentSessionSource || 'web';
  const targetKey = visibleSessionKey || (targetSessionId + '_' + targetSource);
  const rt = getOrCreateRuntime(targetSessionId, targetSource);
  // 该会话仍在执行中：不重复发起（再次回车会走 stopCurrentRun 停止逻辑）
  if (rt.status === 'streaming') return;

  addUserMessage(text, attachments);
  // 推入输入历史
  if (text) {
    _msgHistory.push(text);
    _msgHistoryIndex = -1;
  }
  input.value = '';
  pendingAttachments = [];
  renderAttachmentPreview();
  resizeComposer();
  userStoppedCurrentRun = false;
  rt.status = 'streaming';
  rt.controller = new AbortController();
  rt.events = [];
  rt.live = true;
  setVisibleSessionKey(targetKey);
  // 立即在侧边栏标记该会话为「正在执行」，显示 loading 图标（否则要等到 finally / 60s 刷新才会出现）
  if (typeof updateRunIndicators === 'function') updateRunIndicators();
  currentAbortController = rt.controller;  // 兼容旧引用
  // 前端总超时：兜底保护，避免后端/网络异常导致 fetch 永久挂起（后端已有 90s 空闲看门狗 + 重试，这里给更长的上限）
  let fetchTimedOut = false;
  const fetchTimeoutMs = 600000;
  const fetchTimeout = setTimeout(() => {
    fetchTimedOut = true;
    if (rt.controller) rt.controller.abort();
  }, fetchTimeoutMs);
  setSendButtonRunning(true);
  streamingActive = true;
  isLoading = true;
  showTyping();

  // 初始化本轮卡片骨架（同时被后台会话切回重建复用）
  beginRoundRender(rt);
  startStreamIdleWatch();
  
  let streamDone = false;
  let gotTerminalEvent = false;
  let endedWithError = false;
  
  try {
    const res = await fetch(`/run/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: rt.controller.signal,
      body: JSON.stringify({
        message: text,
        thread_id: threadId,
        attachments,
        project_id: (typeof currentProjectId !== 'undefined' ? currentProjectId : '') || '',
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    streamDone = false;
    gotTerminalEvent = false;
    
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      
      for (const line of lines) {
        if (line === 'data: [DONE]') {
          streamDone = true;
          await reader.cancel().catch(() => {});
          break;
        }
        if (line.startsWith('data: ')) {
          let data;
          try {
            data = JSON.parse(line.slice(6));
          } catch (parseErr) {
            // ponytail: 单行坏数据不应中断整轮；跳过并继续后续事件
            console.warn('[SSE] 跳过无法解析的事件行:', line.slice(0, 120), parseErr.message);
            continue;
          }
          if (data.type === 'done' || data.type === 'error') gotTerminalEvent = true;
          if (data.type === 'error') endedWithError = true;
          rt.events.push(data);
          // 后台会话(rt.live=false)也追踪 token 累积内容，确保合成的 done 事件有正确的最终输出
          if (data.type === 'token' && !rt.live) {
            rt._accumulatedContent = (rt._accumulatedContent || '') + (data.content || '');
          }
          // 仅当该会话是当前可见渲染目标时才渲染进 #messages
          if (rt.live) { markStreamActivity(); _restoreRoundState(rt); handleStreamEvent(data); _saveRoundState(rt); }
        }
      }
      if (streamDone) break;
    }
    
    // 处理 buffer 中剩余内容
    if (buffer.trim() === 'data: [DONE]') {
      streamDone = true;
    } else if (buffer.startsWith('data: ')) {
      let data;
      try {
        data = JSON.parse(buffer.slice(6));
      } catch (parseErr) {
        console.warn('[SSE] 跳过无法解析的剩余事件:', buffer.slice(0, 120), parseErr.message);
        data = null;
      }
      if (data && (data.type === 'done' || data.type === 'error')) gotTerminalEvent = true;
      if (data && data.type === 'error') endedWithError = true;
      if (data) {
        rt.events.push(data);
        // 后台会话也追踪 token 累积内容（同主循环）
        if (data.type === 'token' && !rt.live) {
          rt._accumulatedContent = (rt._accumulatedContent || '') + (data.content || '');
        }
        if (rt.live) { markStreamActivity(); _restoreRoundState(rt); handleStreamEvent(data); _saveRoundState(rt); }
      }
    }

    if (streamDone && !gotTerminalEvent) {
      // 优先用该会话自己的累积内容(后台会话 rt._accumulatedContent), 其次才 fallback 到全局变量
      const sessionFinal = (rt._accumulatedContent || currentFinalContent || '').trim();
      const doneEv = { type: 'done', content: sessionFinal || t('taskEndedNoFinal') };
      rt.events.push(doneEv);
      if (rt.live) { _restoreRoundState(rt); handleStreamEvent(doneEv); _saveRoundState(rt); }
    }
    
  } catch (e) {
    console.error('[SSE] stream error:', e.name, e.message, 'live:', rt.live, 'streamingActive:', streamingActive, 'gotTerminalEvent:', gotTerminalEvent);
    // 后台会话的异常不应污染「正在看的会话」：仅结束自身（spinner 在 finally 移除），不往可见区注入提示
    if (!rt.live) {
      return;
    }
    if (fetchTimedOut && streamingActive && !gotTerminalEvent) {
      // 前端总超时触发的中止：明确告知用户，并保留已收到的内容
      document.querySelectorAll('.tool-status-dot.running').forEach(d => {
        d.className = 'tool-status-dot error';
      });
      addMessage(t('responseTimeout'), 'system');
    } else if (e.name === 'AbortError' || userStoppedCurrentRun) {
      if (currentBotMsgEl && currentFinalContent) {
        currentBotMsgEl.classList.remove('streaming-final');
      }
    } else if (streamingActive && !gotTerminalEvent) {
      // 连接中断：只在未收到终端事件时才提示，避免 done 已到达后的假报警
      document.querySelectorAll('.tool-status-dot.running').forEach(d => {
        d.className = 'tool-status-dot error';
      });
      addMessage(t('connectionInterrupted'), 'system');
    } else {
      // 已收到完整回复但连接异常关闭：只清理残留工具状态，不弹中断提示
      document.querySelectorAll('.tool-status-dot.running').forEach(d => {
        d.className = 'tool-status-dot error';
      });
    }
  } finally {
    clearTimeout(fetchTimeout);
    closePythonProgress();
    // 仅当该会话仍是可见渲染目标时，才清理可见区的「执行中」UI 状态，
    // 否则会误伤正在看的另一个会话的画面（后台会话结束不应扰动可见区）。
    if (rt.live) {
      stopStreamIdleWatch();
      hideTyping();
      removeGeneratingBadge();
      if (_timerInterval) { clearInterval(_timerInterval); _timerInterval = null; }
      document.querySelectorAll('.thinking-step').forEach(el => el.remove());
      document.querySelectorAll('.tool-status-dot.running').forEach(d => {
        d.className = 'tool-status-dot done';
      });
      if (_currentActiveLine) { _currentActiveLine.style.display = 'none'; }
      if (_currentProgressLine) { _currentProgressLine.style.display = 'none'; }
      input.focus();
    }
    if (rt._timerInterval) { clearInterval(rt._timerInterval); rt._timerInterval = null; }
    // 该会话本轮结束（成功/失败/中止都算结束，spinner 应消失）
    rt.status = endedWithError ? 'error' : 'done';
    rt.controller = null;
    rt.live = false;
    currentAbortController = null;
    // 派生态：可见/加载态、运行指示器、侧边栏列表
    syncStreamingActive();
    updateRunIndicators();
    refreshStats();
    loadSessions().finally(updateRunIndicators);
  }
}

// ---------- 流式事件处理（巨型 switch） ----------

function handleStreamEvent(data) {
  const container = document.getElementById('messages');

  function ensureStepsContainer() {
    // currentStepsEl 在 send() 中已初始化为 .agent-body，直接复用
    if (currentStepsEl) return currentStepsEl;
    // 降级：如果还没创建（如历史回放场景），用旧逻辑
    currentStepsEl = document.createElement('div');
    currentStepsEl.className = 'steps-container';
    if (currentBotMsgEl) {
      container.insertBefore(currentStepsEl, currentBotMsgEl);
    } else {
      container.appendChild(currentStepsEl);
    }
    return currentStepsEl;
  }

  // 获取当前 responseCard（向上查找）
  function getResponseCard() {
    var el = currentStepsEl;
    while (el && !el.classList.contains('agent-response')) el = el.parentElement;
    return el;
  }
  
  // 工具函数：创建进度条（如果有步骤容器且有步骤数）
  function updateProgress() {
    if (!currentStepsEl) return;
    let prog = currentStepsEl.querySelector('.step-progress');
    if (!prog) {
      prog = document.createElement('div');
      prog.className = 'step-progress';
      prog.innerHTML = `<div class="progress-bar"><div class="fill" style="width:0%"></div></div><span class="progress-text">${escapeHtml(t('preparing'))}</span>`;
      currentStepsEl.prepend(prog);
    }
    const fill = prog.querySelector('.fill');
    const text = prog.querySelector('.progress-text');
    if (fill && text) {
      const pct = totalSteps > 0 ? Math.min(90, Math.round((totalSteps / (totalSteps + 1)) * 100)) : 10;
      fill.style.width = pct + '%';
      text.textContent = t('stepCount', { count: totalSteps });
    }
  }

  // 工具函数：移除分析中提示
  function removeThinkingHint() {
    if (currentStepsEl) {
      const hints = currentStepsEl.querySelectorAll('.thinking-step');
      hints.forEach(el => el.remove());
    }
  }

  // 工具函数：更新第二行「当前动作」
  function updateActiveLine(icon, text, detailHtml, status) {
    var card = getResponseCard();
    if (!card || !_currentActiveLine) return;
    _currentActiveLine.style.display = 'flex';
    var statusClass = status || 'running';
    // 防御：text 为空时使用兜底文案，避免显示空白行
    var displayText = text && String(text).trim() ? text : (t('executingTask') || '正在执行');
    _currentActiveLine.innerHTML =
      '<span class="active-action-icon">' + (icon || '🔧') + '</span>' +
      '<span class="active-action-text">' + escapeHtml(displayText) + '</span>' +
      '<span class="active-action-toggle">▶</span>' +
      '<span class="active-status-dot ' + statusClass + '">' + (statusClass === 'running' ? '' : '') + '</span>' +
      '<div class="active-action-detail">' + (detailHtml || '') + '</div>';
    // 点击切换详情展开
    _currentActiveLine.onclick = function() { this.classList.toggle('expanded'); };
    smartScroll(container);
  }

  // 工具函数：显示第三行进度指示
  function showProgressLine(text) {
    if (!_currentProgressLine) return;
    _currentProgressLine.style.display = 'flex';
    var label = text || t('agentPlanning') || 'Agent 正在规划与执行...';
    _currentProgressLine.innerHTML = '<span class="agent-progress-spinner"></span><span>' + escapeHtml(label) + '</span>';
    smartScroll(container);
  }

  function hideProgressLine() {
    if (_currentProgressLine) _currentProgressLine.style.display = 'none';
  }

  // 子代理胶囊渲染（按 cap.id diff 复用/创建/删除）
  const _subagentStreams = new Map();  // capId -> EventSource

  function renderSubagentCapsules(capsules, forcedStatus, anchorStep) {
    if (!capsules) return;
    let row = anchorStep !== undefined && anchorStep !== null
      ? container.querySelector('.subagent-row[data-for-step="' + anchorStep + '"]')
      : container.querySelector('.subagent-row');
    if (!row) {
      row = document.createElement('div');
      row.className = 'subagent-row';
      if (anchorStep !== undefined && anchorStep !== null) {
        row.dataset.forStep = anchorStep;
      }
      let anchorCard = null;
      if (anchorStep !== undefined && anchorStep !== null) {
        anchorCard = currentStepsEl ? currentStepsEl.querySelector('.tool-card[data-step="' + anchorStep + '"]') : null;
      }
      if (anchorCard) {
        anchorCard.after(row);            // 锚定到对应工具卡片之后
      } else if (currentStepsEl) {
        currentStepsEl.appendChild(row);  // ponytail: 子代理输出放入工作耗时区域（agent-body），而非 messages 底部
      } else {
        container.appendChild(row);
      }
      // 折叠 toggle
      const toggle = document.createElement('div');
      toggle.className = 'subagent-row-toggle';
      toggle.innerHTML = '<span class="subagent-row-arrow">▶</span><span class="subagent-row-title">子代理执行</span><span class="subagent-row-count"></span>';
      toggle.onclick = function() {
        row.classList.toggle('collapsed');
        const arrow = toggle.querySelector('.subagent-row-arrow');
        if (arrow) arrow.style.transform = row.classList.contains('collapsed') ? '' : 'rotate(90deg)';
      };
      row.parentNode.insertBefore(toggle, row);
    }
    const iconMap = { searcher: '🔍', coder: '<>', reviewer: '👁', debugger: '🐛' };

    // 删除不再存在的子代理组
    const incomingIds = new Set(capsules.map(c => String(c.id)));
    Array.from(row.querySelectorAll('.subagent-group[data-cap-id]')).forEach(el => {
      if (!incomingIds.has(el.dataset.capId)) el.remove();
    });

    capsules.forEach(cap => {
      const capId = String(cap.id);
      const status = forcedStatus || cap.status || 'running';

      // 找或创建子代理组（每个组包含胶囊+日志，横向排列的一列）
      let group = row.querySelector(`.subagent-group[data-cap-id="${capId}"]`);
      if (!group) {
        group = document.createElement('div');
        group.className = 'subagent-group';
        group.dataset.capId = capId;
        row.appendChild(group);
      }

      // 找或创建组内的胶囊节点
      let capEl = group.querySelector('.subagent-capsule');
      if (!capEl) {
        capEl = document.createElement('div');
        capEl.className = 'subagent-capsule';
        capEl.dataset.capId = capId;
        capEl.onclick = (e) => {
          if (e.target.closest('.subagent-log-inline')) return;
          const logEl = group.querySelector('.subagent-log-inline');
          if (logEl) {
            logEl.classList.toggle('collapsed');
            smartScroll(container);
          }
        };
        group.appendChild(capEl);
      }
      const icon = iconMap[cap.agent_type] || '⚙';
      const initial = (cap.agent_type || '?')[0].toUpperCase();
      const statusHtml = status === 'running'
        ? '<span class="sa-status running"></span>'
        : status === 'done'
        ? '<span class="sa-status done">✓</span>'
        : '<span class="sa-status error">✗</span>';
      capEl.innerHTML = `
        <span class="sa-icon ${escapeHtml(cap.agent_type || '')}">${initial}</span>
        <span class="sa-task">${escapeHtml(cap.task || cap.agent_type)}</span>
        ${statusHtml}
        <span class="sa-badge">${icon} ${escapeHtml(cap.agent_type || '')} #${cap.id}</span>
      `;

      // 找或创建组内的日志节点
      let logEl = group.querySelector('.subagent-log-inline');
      if (!logEl) {
        logEl = document.createElement('div');
        logEl.className = 'subagent-log-inline';
        logEl.dataset.capId = capId;
        logEl.innerHTML = '<pre></pre>';
        group.appendChild(logEl);
      }
      // 运行中默认展开，完成后保持展开状态
      if (status === 'running' && !logEl.dataset.collapsedByUser) {
        logEl.classList.remove('collapsed');
      }
      // 写最终结果摘要
      const preEl = logEl.querySelector('pre');
      if (preEl) {
        if (status === 'done' && cap.result && !preEl.dataset.hasResult) {
          const tail = `\n─── 完成 ───\n${unescapeDisplay(String(cap.result)).slice(0, 2000)}`;
          preEl.textContent += tail;
          preEl.dataset.hasResult = '1';
        } else if (status === 'error' && cap.result && !preEl.dataset.hasResult) {
          preEl.textContent += `\n─── 失败 ───\n${unescapeDisplay(String(cap.result)).slice(0, 800)}`;
          preEl.dataset.hasResult = '1';
        }
      }

      // 启动或维持 EventSource
      _ensureCapsuleStream(capId, logEl);
    });

    // 更新折叠 toggle 计数
    const subRow = container.querySelector('.subagent-row');
    const toggle = subRow && subRow.parentNode.querySelector('.subagent-row-toggle');
    const countEl = toggle && toggle.querySelector('.subagent-row-count');
    if (countEl && capsules && capsules.length) {
      countEl.textContent = '(' + capsules.length + ')';
    }
    smartScroll(container);
  }

  function _ensureCapsuleStream(capId, logEl) {
    // 已存在则不重建
    if (_subagentStreams.has(capId)) return;
    // 历史回放模式下不建立 EventSource，避免为已结束的子代理创建无效连接
    if (_isReplaying) return;
    const preEl = logEl.querySelector('pre');
    if (!preEl) return;
    const es = new EventSource(`/subagent-progress/${capId}`);
    es.onmessage = (e) => {
      try {
        const d = JSON.parse(e.data || '{}');
        const prefix = d.cat === 'tool' ? '🔧 ' : d.cat === 'ai' ? '💭 ' : d.cat === 'error' ? '❌ ' : d.cat === 'done' ? '✅ ' : '';
        preEl.textContent += `[${d.cat}] ${prefix}${unescapeDisplay(d.text)}\n`;
        if (!logEl.classList.contains('collapsed')) smartScroll(container);
      } catch {}
    };
    es.onerror = () => {
      // 后端在 done=True 时会主动断开；这里保险起见也关闭
      es.close();
      _subagentStreams.delete(capId);
    };
    _subagentStreams.set(capId, es);
  }

  // 子代理胶囊详情弹窗
  function showCapsuleDetail(cap) {
    let overlay = document.getElementById('capsule-detail-overlay');
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.id = 'capsule-detail-overlay';
      overlay.className = 'capsule-detail-overlay';
      overlay.innerHTML = `
        <div class="capsule-detail-card">
          <div class="capsule-detail-header">
            <h3 id="capsule-detail-title"></h3>
            <button class="capsule-detail-close" onclick="document.getElementById('capsule-detail-overlay').classList.remove('show')">✕</button>
          </div>
          <div class="capsule-detail-body" id="capsule-detail-body"></div>
        </div>`;
      overlay.addEventListener('click', (e) => {
        if (e.target === overlay) overlay.classList.remove('show');
      });
      document.body.appendChild(overlay);
    }
    const iconMap = { searcher: '🔍', coder: '<>', reviewer: '👁', debugger: '🐛' };
    document.getElementById('capsule-detail-title').textContent =
      `${iconMap[cap.agent_type] || ''} ${cap.agent_type} #${cap.id}: ${cap.task || ''}`;
    const statusLabel = cap.status === 'running' ? '执行中...' : cap.status === 'done' ? '✅ 完成' : '❌ 失败';
    document.getElementById('capsule-detail-body').innerHTML =
      `<p>类型: ${escapeHtml(cap.agent_type)} #${cap.id}</p>
       <p>任务: ${escapeHtml(cap.task)}</p>
       <p>状态: ${statusLabel}</p>
       <div id="sa-log-${cap.id}" class="subagent-log"><pre></pre></div>
       ${cap.result ? '<hr><p><strong>结果:</strong></p><pre>' + escapeHtml(String(cap.result).slice(0, 2000)) + '</pre>' : ''}`;
    overlay.classList.add('show');

    // 打开子代理实时日志流
    if (cap.status === 'running') {
      const logPre = document.querySelector(`#sa-log-${cap.id} pre`);
      const es = new EventSource(`/subagent-progress/${cap.id}`);
      es.onmessage = (e) => {
        try {
          const d = JSON.parse(e.data || '{}');
          const prefix = d.cat === 'tool' ? '🔧 ' : d.cat === 'ai' ? '💭 ' : d.cat === 'error' ? '❌ ' : d.cat === 'done' ? '✅ ' : '';
          if (logPre) logPre.textContent += `[${d.cat}] ${prefix}${d.text}\n`;
        } catch {}
      };
      es.onerror = () => { es.close(); };
      // 关闭弹窗时断开 EventSource
      const origClose = () => { es.close(); overlay.classList.remove('show'); overlay.removeEventListener('_close_', origClose); };
      overlay.addEventListener('_close_', origClose);
    }
  }

  // 工具函数：添加分析中提示
  function showThinkingHint(text) {
    removeThinkingHint();
    ensureStepsContainer();
    const hint = document.createElement('div');
    hint.className = 'thinking-step';
    hint.innerHTML = `<span>${escapeHtml(unescapeDisplay(text))}</span><span class="dots"><span></span><span></span><span></span></span>`;
    currentStepsEl.appendChild(hint);
    smartScroll(container);
  }

  function findStepToggle(step) {
    if (!currentStepsEl) return null;
    if (step !== undefined && step !== null) {
      const stepCard = Array.from(currentStepsEl.children).find(el => el.dataset && el.dataset.step === String(step));
      if (stepCard) return stepCard.querySelector('.step-toggle');
    }
    const toggles = currentStepsEl.querySelectorAll('.step-toggle');
    return toggles[toggles.length - 1] || null;
  }

  switch (data.type) {
    case 'subagent_start':
      hideTyping();
      hasToolCalls = true;
      console.log('[胶囊] subagent_start 收到:', data);
      if (!data.capsules || !data.capsules.length) {
        console.warn('[胶囊] subagent_start 无胶囊数据:', data);
        addMessage('⚠️ 触发子代理但无胶囊数据', 'system');
        break;
      }
      renderSubagentCapsules(data.capsules, 'running', _subagentToolStep);
      break;

    case 'subagent_end':
      renderSubagentCapsules(data.capsules, 'done', _subagentToolStep);
      _subagentToolStep = null;
      showGeneratingBadge('🔄 正在汇总...');
      break;

    case 'thought': {
      hideTyping();
      removeThinkingHint();
      removeGeneratingBadge();
      hideProgressLine();
      hasToolCalls = true;
      ensureStepsContainer();
      const thoughtText = data.thought || '';
      // 优先"挪用"答案气泡里已经渲染好的那段
      let thoughtHtml;
      if (currentBotMsgEl && currentBotMsgEl.innerHTML.trim()) {
        thoughtHtml = currentBotMsgEl.innerHTML;
        currentBotMsgEl.remove();
        currentBotMsgEl = null;
        currentFinalContent = '';
      } else {
        thoughtHtml = renderMarkdown(thoughtText);
      }
      // 渲染为 .thought-block（在 agent-body 内）
      const thoughtDiv = document.createElement('div');
      thoughtDiv.className = 'thought-block';
      thoughtDiv.innerHTML = thoughtHtml;
      currentStepsEl.appendChild(thoughtDiv);
      showThinkingHint(t('keepAnalyzing'));
      smartScroll(container);
      break;
    }

    case 'tool_start':
      hideTyping();
      removeThinkingHint();
      removeGeneratingBadge();
      hideProgressLine();
      hasToolCalls = true;
      // 兜底：本轮乐观流出的临时答案实为推理
      if (currentBotMsgEl) { currentBotMsgEl.remove(); currentBotMsgEl = null; }
      currentFinalContent = '';
      totalSteps = data.step || totalSteps + 1;
      const curStep = data.step || (totalSteps - 1);
      _toolTimers[curStep] = Date.now();
      if (data.tool === 'delegate_tasks_parallel' || data.tool === 'delegate_task') {
        _subagentToolStep = curStep;
      }
      ensureStepsContainer();

      const toolIcon = getToolIcon(data.tool);
      const toolName = data.tool || 'unknown';

      // ── 更新第二行：当前动作 ──
      var activeLabel = getToolLabel(data.tool, data.args);
      updateActiveLine(toolIcon, activeLabel, '', 'running');

      // 渲染参数
      let argsHtml = '';
      if (data.tool === 'run_python' && data.args && typeof data.args.code === 'string') {
        const code = unescapeDisplay(data.args.code);
        const cwd = data.args.cwd ? escapeHtml(String(data.args.cwd)) : '';
        argsHtml = `
          <div class="tool-section-label">代码</div>
          <pre class="tool-code-block">${escapeHtml(code)}</pre>
          ${cwd ? `<div style="color:#888;font-size:11px;margin-top:4px;">cwd: ${cwd}</div>` : ''}
        `;
      } else {
        const argsStr = data.args ? JSON.stringify(data.args, null, 2) : '（无参数）';
        argsHtml = `
          <div class="tool-section-label">参数</div>
          <pre class="tool-code-block">${escapeHtml(unescapeDisplay(argsStr))}</pre>
        `;
      }

      const cardDiv = document.createElement('div');
      cardDiv.className = 'tool-card open';
      cardDiv.dataset.step = String(curStep);
      cardDiv.innerHTML = `
        <div class="tool-card-header" onclick="toggleToolCard(this)">
          <span class="arrow">▶</span>
          <span class="tool-icon">${toolIcon}</span>
          <span class="tool-label">调用工具:</span>
          <span class="tool-name-inline">${escapeHtml(toolName)}</span>
          <span class="search-source-badge" id="src-badge-${curStep}" style="display:none;margin-left:6px;font-size:11px;padding:1px 6px;border-radius:4px;background:#f0f0f0;color:#555;"></span>
          <span class="tool-duration" id="tool-dur-${curStep}"></span>
          <span class="tool-status-dot running" id="tool-status-${curStep}"></span>
        </div>
        <div class="tool-card-body">
          ${argsHtml}
          <div id="tool-output-${curStep}"></div>
        </div>`;
      currentStepsEl.appendChild(cardDiv);
      smartScroll(container);

      // ── Python 实时输出流（使用 HTTP 轮询，消除 WebSocket 部署兼容问题）──
      if (data.tool === 'run_python' && !_isReplaying) {
        closePythonProgress();
        var pyBox = document.createElement('div');
        pyBox.className = 'python-progress';
        pyBox.style.marginTop = '8px';
        pyBox.innerHTML = '<div style="color:#999;font-size:12px;margin-bottom:4px;">⏳ Python 实时日志</div>' +
          '<pre class="python-output" id="python-out-' + curStep + '">等待输出...</pre>';
        cardDiv.querySelector('.tool-card-body').appendChild(pyBox);
        var outEl = document.getElementById('python-out-' + curStep);
        _currentPythonOutEl = outEl;
        // HTTP 轮询获取实时输出
        var seenCount = 0;
        var pollTimer = setInterval(function() {
          fetch('/tool-progress-json').then(function(r) { return r.json(); }).then(function(d) {
            if (!d || !outEl) return;
            if (d.lines && d.lines.length > seenCount) {
              var newLines = d.lines.slice(seenCount);
              if (outEl.textContent === '等待输出...') outEl.textContent = '';
              for (var j = 0; j < newLines.length; j++) {
                outEl.textContent += unescapeDisplay(newLines[j]) + '\n';
              }
              outEl.scrollTop = outEl.scrollHeight;
              seenCount = d.lines.length;
            }
            if (d.running === false) {
              clearInterval(pollTimer);
              closePythonProgress();
            }
          }).catch(function() {});
        }, 500);
        _pythonProgressSource = { close: function() { clearInterval(pollTimer); } };
        // 8 秒后如果还没有实时日志，显示等待提示
        setTimeout(function() {
          if (outEl && outEl.textContent === '等待输出...') {
            outEl.textContent = '等待输出中...（执行完成后会自动显示结果）';
          }
        }, 8000);
      }
      break;

    case 'tool_result':
      closePythonProgress();
      const trStep = data.step !== undefined ? data.step : 0;
      const startedAt = _toolTimers[trStep];
      const elapsed = startedAt ? Date.now() - startedAt : 0;
      const durText = elapsed > 1000 ? `${(elapsed/1000).toFixed(1)}s` : `${elapsed}ms`;
      delete _toolTimers[trStep];

      // 更新第二行状态与文字
      var isError = data.error === true;
      if (_currentActiveLine && _currentActiveLine.style.display !== 'none') {
        var dot = _currentActiveLine.querySelector('.active-status-dot');
        if (dot) {
          dot.className = 'active-status-dot ' + (isError ? 'error' : 'done');
          dot.innerHTML = '';
        }
        // 刷新文字为完成状态，避免显示空白或过时内容
        var textEl = _currentActiveLine.querySelector('.active-action-text');
        if (textEl) {
          var toolNameForLabel = data.tool || 'tool';
          var doneLabel = isError
            ? (t('taskFailed') || '执行失败')
            : (t('taskCompleted') || '已完成');
          textEl.textContent = getToolLabel(toolNameForLabel, data.args) + ' — ' + doneLabel + ' (' + durText + ')';
        }
        // 展开详情显示结果摘要
        var detailEl = _currentActiveLine.querySelector('.active-action-detail');
        if (detailEl) {
          var resultPreview = String(data.result || '');
          if (resultPreview.length > 300) resultPreview = resultPreview.slice(0, 297) + '...';
          detailEl.innerHTML = '<pre style="background:#f5f5f7;padding:6px 8px;border-radius:4px;font-size:11.5px;white-space:pre-wrap;word-break:break-all;max-height:200px;overflow-y:auto;color:#444;">' + escapeHtml(resultPreview) + '</pre>';
        }
      }

      // 更新卡片状态
      const card = currentStepsEl.querySelector(`.tool-card[data-step="${trStep}"]`);
      if (card) {
        const isError = data.error === true;
        const dot = card.querySelector('.tool-status-dot');
        if (dot) { dot.className = 'tool-status-dot ' + (isError ? 'error' : 'done'); }
        const dur = card.querySelector('.tool-duration');
        if (dur) dur.textContent = durText;
        if (isError) card.classList.add('error');

        // 添加结果
        const outArea = card.querySelector(`#tool-output-${trStep}`);
        if (outArea && data.result) {
          // 如果有完整 Python 输出，显示完整版
          var fullResult = data.result;
          if (data.result_full && data.result_full.length > 400) {
            fullResult = data.result_full;
          }
          
          // 检测是否包含 Markdown 图片语法
          var hasMarkdownImage = /!\[.*?\]\(.*?\)/.test(fullResult);
          
          if (hasMarkdownImage) {
            // 提取图片 URL 并保存，供最终消息注入
            var imgMatch = String(fullResult).match(/!\[.*?\]\((.*?)\)/);
            if (imgMatch) {
              _lastToolImageHtml = '<a href="' + escapeHtml(imgMatch[1]) + '"><img src="' + escapeHtml(imgMatch[1]) + '" style="max-width:100%;border-radius:6px;margin:8px 0;"></a>';
            }
            
            // 包含图片，使用 Markdown 渲染
            outArea.innerHTML = '<div class="tool-section-label">结果</div>' +
              `<div class="tool-result-markdown">${renderMarkdown(unescapeDisplay(String(fullResult)))}</div>`;
          } else {
            // 纯文本，使用 <pre> 显示
            outArea.innerHTML = '<div class="tool-section-label">结果</div>' +
              `<pre class="tool-code-block" style="${isError ? 'color:#fca5a5;' : ''}max-height:400px;overflow-y:auto;">${escapeHtml(unescapeDisplay(String(fullResult)))}</pre>`;
          }

          // ── 工作区外文件写入授权 ──
          var resultStr = String(fullResult);
          var permMatch = resultStr.match(/__PERMISSION_NEEDED__:\s*(\S+)/);
          if (permMatch) {
            var permPath = permMatch[1];
            var permBtn = document.createElement('div');
            permBtn.style.cssText = 'margin-top:8px;display:flex;flex-direction:column;gap:8px;';
            permBtn.innerHTML = `
              <div style="font-size:12px;color:#ff9f0a;font-weight:600;">⏳ 等待您在界面授权：该文件位于工作区外，需点击「授权写入」后才能继续</div>
              <div style="display:flex;gap:10px;align-items:center;">
                <span style="font-size:12px;color:#8e8e93;">📝 目标路径 <code style="background:#2c2c2e;color:#f5f5f7;padding:2px 6px;border-radius:4px;font-size:11px;">${escapeHtml(permPath)}</code></span>
                <button class="perm-grant-btn" style="padding:5px 14px;background:#007aff;color:#fff;border:none;border-radius:6px;font-size:12px;cursor:pointer;" data-path="${escapeHtml(permPath)}">授权写入</button>
              </div>
            `;
            permBtn.querySelector('.perm-grant-btn').addEventListener('click', async function() {
              var path = this.dataset.path;
              try {
                var r = await fetch('/permissions/grant-path', {
                  method: 'POST',
                  headers: {'Content-Type': 'application/json'},
                  body: JSON.stringify({path: path}),
                });
                if (r.ok) {
                  var inner = this.parentElement;
                  // 提取目录路径（父目录）用于授权
                  var dirPath = path;
                  var lastSlash = dirPath.lastIndexOf('/');
                  if (lastSlash > 0) dirPath = dirPath.substring(0, lastSlash);
                  // 同时授权目录级
                  await fetch('/permissions/grant-path', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({path: dirPath}),
                  });
                  inner.innerHTML = '<span style="font-size:12px;color:#30d158;">✅ 已授权，请在输入框输入「继续」重试</span>';
                } else {
                  this.textContent = '授权失败';
                }
              } catch(e) {
                this.textContent = '网络错误';
              }
            });
            outArea.appendChild(permBtn);
          }

          // ── 高危命令执行确认 ──
          var confirmMatch = resultStr.match(/__CONFIRM_NEEDED__::([\s\S]*?)::__CMD__::([\s\S]+)/);
          if (confirmMatch) {
            var riskReason = confirmMatch[1];
            var dangerCmd = confirmMatch[2];
            var confirmWrap = document.createElement('div');
            confirmWrap.style.cssText = 'margin-top:8px;display:flex;flex-direction:column;gap:8px;';
            confirmWrap.innerHTML = `
              <div style="font-size:12px;color:#ff9f0a;font-weight:600;">⚠️ 高危操作待确认：${escapeHtml(riskReason)}</div>
              <div style="background:#2c2c2e;color:#f5f5f7;padding:8px 10px;border-radius:6px;font-size:12px;font-family:monospace;white-space:pre-wrap;word-break:break-all;">${escapeHtml(dangerCmd)}</div>
              <div style="display:flex;gap:10px;align-items:center;">
                <button class="danger-confirm-btn" style="padding:5px 14px;background:#ff453a;color:#fff;border:none;border-radius:6px;font-size:12px;cursor:pointer;" data-cmd="${escapeHtml(dangerCmd)}">确认执行</button>
                <button class="danger-cancel-btn" style="padding:5px 14px;background:#3a3a3c;color:#fff;border:none;border-radius:6px;font-size:12px;cursor:pointer;">取消</button>
              </div>
            `;
            confirmWrap.querySelector('.danger-confirm-btn').addEventListener('click', async function() {
              var cmd = this.dataset.cmd;
              try {
                var r = await fetch('/permissions/grant-command', {
                  method: 'POST',
                  headers: {'Content-Type': 'application/json'},
                  body: JSON.stringify({command: cmd}),
                });
                if (r.ok) {
                  this.parentElement.innerHTML = '<span style="font-size:12px;color:#30d158;">✅ 已确认，请在输入框输入「继续」重试</span>';
                } else {
                  this.textContent = '授权失败';
                }
              } catch(e) {
                this.textContent = '网络错误';
              }
            });
            confirmWrap.querySelector('.danger-cancel-btn').addEventListener('click', function() {
              this.parentElement.innerHTML = '<span style="font-size:12px;color:#8e8e93;">❌ 已取消执行</span>';
            });
            outArea.appendChild(confirmWrap);
          }

          // web_search 特殊处理：从结果中提取搜索来源并显示 badge
          if (data.tool === 'web_search') {
            var resultStr = String(fullResult);
            var sourceName = '';
            // 尝试正则匹配（来源: Xxx）
            var srcMatch = resultStr.match(/[（(]来源\s*[:：]\s*([^）)\]]+)/);
            if (srcMatch) {
              sourceName = srcMatch[1].trim();
            } else {
              // 降级：直接搜索 "来源:" 文本
              var idx = resultStr.indexOf('来源');
              if (idx >= 0) {
                var after = resultStr.slice(idx + 3);
                var colonIdx = after.search(/[:：]/);
                if (colonIdx >= 0) {
                  var endIdx = after.slice(colonIdx + 1).search(/[）)\]）]/);
                  sourceName = endIdx >= 0 ? after.slice(colonIdx + 1, colonIdx + 1 + endIdx).trim() : after.slice(colonIdx + 1).trim();
                }
              }
            }
            if (sourceName) {
              var badge = document.getElementById('src-badge-' + trStep);
              if (badge) {
                badge.textContent = sourceName;
                badge.style.display = 'inline';
                var colorMap = {
                  'AnySearch': { bg: '#e8f5e9', color: '#2e7d32' },
                  'Tavily': { bg: '#e3f2fd', color: '#1565c0' },
                  'Bing': { bg: '#fff3e0', color: '#e65100' },
                };
                var colors = colorMap[sourceName] || { bg: '#f3e5f5', color: '#7b1fa2' };
                badge.style.background = colors.bg;
                badge.style.color = colors.color;
              }
            }
          }
        }

        // Diff 视图（使用 CSS 类 + 行号 + 可折叠，匹配截图效果）
        if (data.diff && data.diff.diff) {
          const diffArea = card.querySelector('.tool-card-body');
          if (diffArea) {
            const filePath = data.diff_file_path || '';
            const diffWrap = document.createElement('div');
            diffWrap.className = 'diff-view';

            // ── 可折叠的标题栏 ──
            const toggle = document.createElement('div');
            toggle.className = 'diff-toggle open';
            toggle.addEventListener('click', function () {
              this.classList.toggle('open');
              var body = this.nextElementSibling;
              if (body) body.style.display = body.style.display === 'none' ? '' : 'none';
            });
            const arrow = document.createElement('span');
            arrow.className = 'diff-arrow';
            arrow.textContent = '▶';
            const summary = document.createElement('span');
            summary.className = 'diff-summary';
            summary.textContent = filePath ? escapeHtml(filePath) : '文件变更';
            const counts = document.createElement('span');
            counts.style.marginLeft = 'auto';
            counts.style.fontSize = '12px';
            counts.innerHTML = '<span class="diff-added-count">+' + (data.diff.added || 0) + '</span>'
              + ' <span class="diff-removed-count">-' + (data.diff.removed || 0) + '</span>';
            toggle.appendChild(arrow);
            toggle.appendChild(summary);
            toggle.appendChild(counts);
            diffWrap.appendChild(toggle);

            // ── Diff 内容区 ──
            const diffBody = document.createElement('div');
            diffBody.className = 'diff-body';
            var oldLn = 1, newLn = 1;
            for (var di = 0; di < data.diff.diff.length; di++) {
              var d = data.diff.diff[di];
              var line = document.createElement('div');
              line.className = 'diff-line';
              var lineNumHtml = '', prefixHtml = '';
              switch (d.t) {
                case '+':
                  line.classList.add('diff-add');
                  lineNumHtml = '<span style="color:#81c784;min-width:32px;display:inline-block;text-align:right;margin-right:8px;user-select:none;">' + newLn + '</span>';
                  prefixHtml = '<span style="color:#81c784;font-weight:700;margin-right:4px;user-select:none;">+</span>';
                  newLn++;
                  break;
                case '-':
                  line.classList.add('diff-remove');
                  lineNumHtml = '<span style="color:#e57373;min-width:32px;display:inline-block;text-align:right;margin-right:8px;user-select:none;">' + oldLn + '</span>';
                  prefixHtml = '<span style="color:#e57373;font-weight:700;margin-right:4px;user-select:none;">-</span>';
                  oldLn++;
                  break;
                case ' ':
                  line.classList.add('diff-keep');
                  lineNumHtml = '<span style="color:#aaa;min-width:32px;display:inline-block;text-align:right;margin-right:8px;user-select:none;">' + oldLn + '</span>';
                  prefixHtml = '<span style="color:#ccc;margin-right:4px;user-select:none;"> </span>';
                  oldLn++;
                  newLn++;
                  break;
                case '…':
                  line.classList.add('diff-more');
                  line.innerHTML = d.c;
                  diffBody.appendChild(line);
                  continue; // 跳过行号渲染
              }
              line.innerHTML = lineNumHtml + prefixHtml + escapeHtml(d.c);
              diffBody.appendChild(line);
            }
            diffWrap.appendChild(diffBody);
            diffArea.appendChild(diffWrap);
          }
        }
      }
      break;

    case 'progress':
      hideTyping();
      removeThinkingHint();
      hasToolCalls = true;
      ensureStepsContainer();
      if (data.step !== undefined && _toolTimers[data.step]) {
        const elapsed = Date.now() - _toolTimers[data.step];
        const durEl = document.getElementById('tool-dur-' + data.step);
        if (durEl) durEl.textContent = elapsed > 1000 ? `${(elapsed/1000).toFixed(0)}s` : `${elapsed}ms`;
      }
      // 使用第三行进度指示
      showProgressLine(data.message || t('stillProcessing'));
      smartScroll(container);
      break;

    case 'llm_thinking':
      hideTyping();
      removeGeneratingBadge();
      showProgressLine(t('callingAI'));
      break;

    case 'llm_response':
      if (!data.has_tool_calls) {
        hideProgressLine();
      }
      break;

    case 'llm_retry':
      // 模型响应超时，已自动重试（仅重发 LLM 调用，不重跑工具）——界面上明确提示
      markStreamActivity();
      showProgressLine(t('modelRetrying', { attempt: data.attempt, max: (data.max || 1) }));
      addMessage(t('modelRetryingNote', { attempt: data.attempt, max: (data.max || 1) }), 'system');
      break;

    case 'model_switch':
      addMessage(t('modelSwitched', { reason: data.reason || (currentLanguage === 'en' ? 'request' : '请求'), model: data.model }), 'system');
      break;
    
    case 'token':
      // 重放历史时跳过；但「切回后台会话」的重建回放需要渲染 token（_isReconstructing 放开）
      if (_isReplaying && !_isReconstructing) break;
      hideTyping();
      removeThinkingHint();
      removeGeneratingBadge();
      hideProgressLine();
      // 逐字流式渲染最终答案（复用 currentBotMsgEl 机制，done 时会迁移到 agent-final-output）
      if (!currentBotMsgEl) {
        currentBotMsgEl = document.createElement('div');
        currentBotMsgEl.className = 'msg bot streaming-final';
        if (currentStepsEl) {
          currentStepsEl.after(currentBotMsgEl);
        } else {
          container.appendChild(currentBotMsgEl);
        }
      }
      currentFinalContent += data.content;
      currentBotMsgEl.innerHTML = renderMarkdown(currentFinalContent);
      if (hasToolCalls) showProgressLine(t('continueProcessing'));
      smartScroll(container);
      break;

    case 'error':
      hideTyping();
      removeThinkingHint();
      removeGeneratingBadge();
      hideProgressLine();
      document.querySelectorAll('.tool-status-dot.running').forEach(d => {
        d.className = 'tool-status-dot error';
      });
      // 更新第二行为错误状态
      if (_currentActiveLine && _currentActiveLine.style.display !== 'none') {
        var dot = _currentActiveLine.querySelector('.active-status-dot');
        if (dot) { dot.className = 'active-status-dot error'; dot.innerHTML = ''; }
      }
      _lastToolImageHtml = null;
      addMessage('❌ ' + data.content, 'system');
      break;

    case 'todo':
      hideTyping();
      removeThinkingHint();
      removeGeneratingBadge();
      hideProgressLine();
      hasToolCalls = true;
      if (data.todo_list) {
        renderTodoPanel(data.todo_list, true);
      }
      break;

    case 'done':
      hideTyping();
      removeThinkingHint();
      removeGeneratingBadge();
      hideProgressLine();

      // 折叠所有工具卡片，标记 running 为 done
      document.querySelectorAll('.tool-card .tool-status-dot.running').forEach(dot => {
        dot.className = 'tool-status-dot done';
      });
      document.querySelectorAll('.tool-card.open').forEach(card => {
        card.classList.remove('open');
      });

      // 更新进度为 100%
      const prog = currentStepsEl ? currentStepsEl.querySelector('.step-progress') : null;
      if (prog) {
        const fill = prog.querySelector('.fill');
        const text = prog.querySelector('.progress-text');
        if (fill) fill.style.width = '100%';
        if (text) text.textContent = t('completeText');
      }

      // 冻结最终耗时到 header（防止定时器被清后值丢失）
      var finalElapsed = _agentStartTime ? (Date.now() - _agentStartTime) : 0;
      if (finalElapsed > 0 && responseCard) {
        var fvalEl = responseCard.querySelector('.agent-time-val');
        if (fvalEl) fvalEl.textContent = formatElapsed(finalElapsed);
      }

      // ── 渲染最终输出到 agent-final-output 区域 ──
      var responseCard = getResponseCard();
      // 标记卡片已完成（CSS 据此隐藏执行状态行；JS 也做 display:none 双保险）
      if (responseCard) responseCard.classList.add('finished');
      var finalContent = data.content || currentFinalContent || t('taskEndedNoFinal');

      // 在最终消息前注入截图（如果有工具图片且最终回复没含图片）
      var finalHtml = renderMarkdown(finalContent);
      if (_lastToolImageHtml && finalContent.indexOf('![') === -1) {
        finalHtml = '<div style="margin-bottom:12px;">' + _lastToolImageHtml + '</div>' + finalHtml;
      }

      // 创建或复用最终输出区域
      var finalOutputEl;
      if (currentBotMsgEl) {
        // 复用已有的临时答案气泡，改为正式样式
        currentBotMsgEl.innerHTML = finalHtml;
        currentBotMsgEl.className = 'agent-final-output';
        currentBotMsgEl.classList.remove('streaming-final');
        finalOutputEl = currentBotMsgEl;
      } else if (!_isReplaying) {
        finalOutputEl = document.createElement('div');
        finalOutputEl.className = 'agent-final-output';
        finalOutputEl.innerHTML = finalHtml;
        if (responseCard) {
          responseCard.appendChild(finalOutputEl);
        } else {
          container.appendChild(finalOutputEl);
        }
      }
      currentFinalContent = finalContent;

      // 隐藏第二行和第三行（执行中状态）
      if (_currentActiveLine) _currentActiveLine.style.display = 'none';
      if (_currentProgressLine) _currentProgressLine.style.display = 'none';

      // 重置工具图片缓存
      _lastToolImageHtml = null;

      // ── 兜底：确保最终答案在正确位置 ──
      if (finalOutputEl && responseCard && finalOutputEl.parentElement !== responseCard) {
        responseCard.appendChild(finalOutputEl);
      }

      // Done 事件携带最终 todo 清单时，渲染/更新面板（不触发闪烁）
      if (data.todo_list) {
        renderTodoPanel(data.todo_list, false);
        // 将 todo 面板移到 bot 消息之前
        if (currentBotMsgEl && _currentTodoPanel && _currentTodoPanel.nextSibling !== currentBotMsgEl) {
          container.insertBefore(_currentTodoPanel, currentBotMsgEl);
        }
      }
      smartScroll(container);
      break;
  }
}

// ---------- Todo 清单渲染 ----------

var _currentTodoPanel = null;  // 当前会话的 todo 面板 DOM

function renderTodoPanel(todoData, hasUpdate) {
  const container = document.getElementById('messages');
  if (!todoData || !todoData.items) return;

  // 找或创建 todo 面板
  if (!_currentTodoPanel || !document.body.contains(_currentTodoPanel)) {
    _currentTodoPanel = document.createElement('div');
    _currentTodoPanel.className = 'todo-panel';
    container.appendChild(_currentTodoPanel);
  }

  // 始终追加到消息容器末尾（与 steps/bot msg 平级）
  if (_currentTodoPanel.parentNode === container && container.lastChild !== _currentTodoPanel) {
    container.appendChild(_currentTodoPanel);
  }

  var items = todoData.items || [];
  var total = items.length;
  var doneCount = items.filter(function(i) { return i.status === 'done'; }).length;
  var summary = todoData.summary || ('共 ' + total + ' 项，已完成 ' + doneCount + ' 项');

  // 有更新时自动展开并闪烁
  if (hasUpdate) {
    _currentTodoPanel.classList.remove('collapsed');
    _currentTodoPanel.classList.add('has-update');
    setTimeout(function() {
      if (_currentTodoPanel) _currentTodoPanel.classList.remove('has-update');
    }, 1500);
  }

  var headerHtml = [
    '<div class="todo-header" onclick="toggleTodoPanel(event)">',
    '  <span class="todo-icon">📋</span>',
    '  <span class="todo-title">' + escapeHtml(t('todoList') || '任务清单') + '</span>',
    '  <span class="todo-summary">' + escapeHtml(summary) + '</span>',
    '  <span class="todo-arrow">▶</span>',
    '</div>'
  ].join('\n');

  var itemsHtml = items.map(function(item) {
    var statusClass = item.status || 'pending';
    var isDone = statusClass === 'done';
    var checkedAttr = isDone ? 'checked' : '';
    var contentClass = isDone ? 'todo-content done' : 'todo-content';
    var statusLabel = '';
    switch (statusClass) {
      case 'pending': statusLabel = '\u5F85\u5904\u7406'; break;
      case 'in_progress': statusLabel = '\u8FDB\u884C\u4E2D'; break;
      case 'done': statusLabel = '\u5DF2\u5B8C\u6210'; break;
      case 'blocked': statusLabel = '\u963B\u585E'; break;
    }
    return [
      '<div class="todo-item" data-todo-id="' + escapeHtml(item.id) + '">',
      '  <div class="todo-checkbox ' + checkedAttr + '" onclick="toggleTodoItem(event, \'' + escapeHtml(item.id) + '\')"></div>',
      '  <div class="' + contentClass + '">' + escapeHtml(item.content) + '</div>',
      '  <span class="todo-status-badge ' + statusClass + '">' + statusLabel + '</span>',
      '</div>'
    ].join('\n');
  }).join('\n');

  _currentTodoPanel.innerHTML = headerHtml + '<div class="todo-body">' + itemsHtml + '</div>';
  smartScroll(container);
}

function toggleTodoPanel(event) {
  var panel = event.currentTarget.closest('.todo-panel');
  if (panel) panel.classList.toggle('collapsed');
}

function toggleTodoItem(event, todoId) {
  event.stopPropagation();
  var checkbox = event.currentTarget;
  checkbox.classList.toggle('checked');
  var content = checkbox.nextElementSibling;
  if (content) content.classList.toggle('done');
  var badge = content ? content.nextElementSibling : null;
  if (badge) {
    if (checkbox.classList.contains('checked')) {
      badge.className = 'todo-status-badge done';
      badge.textContent = '\u5DF2\u5B8C\u6210';
    } else {
      badge.className = 'todo-status-badge pending';
      badge.textContent = '\u5F85\u5904\u7406';
    }
  }
}

// ---------- 工具图标映射 ----------

function getToolIcon(toolName) {
  const icons = {
    'read_file': '📄', 'write_file': '✏️', 'append_to_file': '📝',
    'list_files': '📂', 'delete_file': '🗑️', 'search_files': '🔍',
    'get_workspace_path': '📁', 'run_python': '🐍', 'get_system_info': '💻',
    'web_search': '🌐', 'web_fetch': '📄', 'bash': '💻', 'shell': '💻',
  };
  return icons[toolName] || '🔧';
}

// 生成工具调用摘要文本（用于第二行当前动作）
function getToolLabel(toolName, args) {
  const labels = {
    'read_file': t('readingFile') || '读取文件内容',
    'write_file': t('writingFile') || '写入文件',
    'append_to_file': t('appendingFile') || '追加写入',
    'list_files': t('listingFiles') || '列出目录',
    'delete_file': t('deletingFile') || '删除文件',
    'search_files': t('searchingFiles') || '搜索内容',
    'get_workspace_path': t('gettingPath') || '获取工作路径',
    'run_python': t('runningPython') || '运行 Python',
    'get_system_info': t('gettingSystemInfo') || '获取系统信息',
    'web_search': t('searchingWeb') || '网络搜索',
    'web_fetch': t('fetchingWeb') || '抓取网页',
    'bash': t('runningShell') || '运行 Shell 命令',
    'shell': t('runningShell') || '运行 Shell 命令',
  };
  var label = labels[toolName] || (t('callingTool') || '调用工具: ') + toolName;
  // 追加关键参数（截断避免过长）
  if (args) {
    if (typeof args.file_path === 'string' && args.file_path.length < 80) {
      label += ': ' + escapeHtml(args.file_path);
    } else if (typeof args.code === 'string') {
      var code = args.code.trim();
      if (code.length > 60) code = code.slice(0, 57) + '...';
      label += ': ' + escapeHtml(code.split('\n')[0]);
    } else if (typeof args.command === 'string') {
      var cmd = args.command;
      if (cmd.length > 70) cmd = cmd.slice(0, 67) + '...';
      label += ': ' + escapeHtml(cmd);
    } else if (typeof args.query === 'string') {
      var q = args.query;
      if (q.length > 50) q = q.slice(0, 47) + '...';
      label += ': ' + escapeHtml(q);
    }
  }
  return label;
}

function toggleStep(el) {
  el.classList.toggle('open');
  const details = el.nextElementSibling;
  if (details) details.classList.toggle('open');
}

function toggleToolCard(header) {
  const card = header.closest('.tool-card');
  if (card) card.classList.toggle('open');
}
