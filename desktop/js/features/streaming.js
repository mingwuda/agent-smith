/* streaming.js — SSE 流式发送、handleStreamEvent、typing/loading 指示器
   依赖: state.js, util.js, i18n.js, messaging.js(addMessage, addUserMessage, resizeComposer, renderAttachmentPreview) */

// ---------- 全局变量 ----------

var _currentPythonOutEl = null; // 当前 run_python 的实时日志容器
var _lastToolImageHtml = null; // 最近一次工具结果的图片 Markdown

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
  if (!isLoading || !currentAbortController) return;
  userStoppedCurrentRun = true;
  currentAbortController.abort();
  addMessage(t('runStopRequested'), 'system');
  sendBtn.innerHTML = '<svg width="16" height="16" viewBox="0 0 16 16"><rect x="3" y="3" width="10" height="10" rx="2" fill="currentColor"/></svg>';
  sendBtn.disabled = true;
}

// ---------- 核心 SSE 发送 ----------

async function send() {
  const text = input.value.trim();
  const attachments = pendingAttachments.slice();
  if ((!text && attachments.length === 0) || isLoading) return;
  
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
  isLoading = true;
  userStoppedCurrentRun = false;
  currentAbortController = new AbortController();
  setSendButtonRunning(true);
  streamingActive = true;
  showTyping();
  
  // 初始化步骤容器和 bot 消息
  currentStepsEl = null;
  currentBotMsgEl = null;
  currentFinalContent = '';
  totalSteps = 0;
  hasToolCalls = false;
  generatingBadgeEl = null;
  startStreamIdleWatch();
  
  let streamDone = false;
  let gotTerminalEvent = false;
  
  try {
    const res = await fetch(`/run/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: currentAbortController.signal,
      body: JSON.stringify({ message: text, thread_id: threadId, attachments }),
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
          const data = JSON.parse(line.slice(6));
          if (data.type === 'done' || data.type === 'error') gotTerminalEvent = true;
          markStreamActivity();
          handleStreamEvent(data);
        }
      }
      if (streamDone) break;
    }
    
    // 处理 buffer 中剩余内容
    if (buffer.trim() === 'data: [DONE]') {
      streamDone = true;
    } else if (buffer.startsWith('data: ')) {
      const data = JSON.parse(buffer.slice(6));
      if (data.type === 'done' || data.type === 'error') gotTerminalEvent = true;
      markStreamActivity();
      handleStreamEvent(data);
    }

    if (streamDone && !gotTerminalEvent) {
      handleStreamEvent({
        type: 'done',
        content: currentFinalContent || t('taskEndedNoFinal'),
      });
    }
    
  } catch (e) {
    console.error('[SSE] stream error:', e.name, e.message, 'streamingActive:', streamingActive, 'gotTerminalEvent:', gotTerminalEvent);
    if (e.name === 'AbortError' || userStoppedCurrentRun) {
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
    stopStreamIdleWatch();
    closePythonProgress();
    hideTyping();
    document.querySelectorAll('.thinking-step').forEach(el => el.remove());
    removeGeneratingBadge();
    // 兜底：清理页面上所有残留的"执行中"步骤状态
    document.querySelectorAll('.tool-status-dot.running').forEach(d => {
      d.className = 'tool-status-dot done';
    });
    streamingActive = false;
    isLoading = false;
    currentAbortController = null;
    setSendButtonRunning(false);
    input.focus();
    refreshStats();
    loadSessions();
  }
}

// ---------- 流式事件处理（巨型 switch） ----------

function handleStreamEvent(data) {
  const container = document.getElementById('messages');

  function ensureStepsContainer() {
    if (currentStepsEl) return currentStepsEl;
    currentStepsEl = document.createElement('div');
    currentStepsEl.className = 'steps-container';
    if (currentBotMsgEl) {
      container.insertBefore(currentStepsEl, currentBotMsgEl);
    } else {
      container.appendChild(currentStepsEl);
    }
    return currentStepsEl;
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

  // 子代理胶囊渲染（按 cap.id diff 复用/创建/删除）
  const _subagentStreams = new Map();  // capId -> EventSource

  function renderSubagentCapsules(capsules, forcedStatus) {
    if (!capsules) return;
    let row = container.querySelector('.subagent-row');
    if (!row) {
      row = document.createElement('div');
      row.className = 'subagent-row';
      if (currentStepsEl) {
        currentStepsEl.insertAdjacentElement('afterend', row);
      } else {
        container.appendChild(row);
      }
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

    smartScroll(container);
  }

  function _ensureCapsuleStream(capId, logEl) {
    // 已存在则不重建
    if (_subagentStreams.has(capId)) return;
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
      addMessage('🔍 子代理搜索已启动：' + data.capsules.length + ' 个任务', 'system');
      renderSubagentCapsules(data.capsules, 'running');
      break;

    case 'subagent_end':
      renderSubagentCapsules(data.capsules, 'done');
      addMessage('✅ 子代理搜索已完成，正在汇总...', 'system');
      showGeneratingBadge('🔄 正在汇总...');
      break;

    case 'thought':
      hideTyping();
      removeThinkingHint();
      removeGeneratingBadge();
      hasToolCalls = true;
      ensureStepsContainer();
      updateProgress();
      const thoughtText = data.thought || '';
      const thoughtDiv = document.createElement('div');
      thoughtDiv.style.marginBottom = '4px';
      thoughtDiv.dataset.step = data.step || '0';
      thoughtDiv.innerHTML = `
        <div class="step-toggle open" onclick="toggleStep(this)">
          <span class="arrow">▶</span>
          <span class="tool-icon">🤔</span>
          <span class="tool-name">${escapeHtml(t('thought'))}</span>
          <span class="step-status">第 ${data.step || '?'} 步</span>
        </div>
        <div class="step-details open">
          <div class="thought">${renderMarkdown(thoughtText)}</div>
        </div>`;
      currentStepsEl.appendChild(thoughtDiv);
      showThinkingHint(t('keepAnalyzing'));
      smartScroll(container);
      break;

    case 'tool_start':
      hideTyping();
      removeThinkingHint();
      removeGeneratingBadge();
      hasToolCalls = true;
      totalSteps = data.step || totalSteps + 1;
      const curStep = data.step || (totalSteps - 1);
      _toolTimers[curStep] = Date.now();
      ensureStepsContainer();
      updateProgress();

      const toolIcon = getToolIcon(data.tool);
      const toolName = data.tool || 'unknown';

      // 渲染参数：run_python 的代码单独显示，其他工具显示 JSON 但还原转义换行
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
            permBtn.style.cssText = 'margin-top:8px;display:flex;gap:10px;align-items:center;';
            permBtn.innerHTML = `
              <span style="font-size:12px;color:#8e8e93;">📝 需要授权后才能写入 <code style="background:#2c2c2e;color:#f5f5f7;padding:2px 6px;border-radius:4px;font-size:11px;">${escapeHtml(permPath)}</code></span>
              <button class="perm-grant-btn" style="padding:5px 14px;background:#007aff;color:#fff;border:none;border-radius:6px;font-size:12px;cursor:pointer;" data-path="${escapeHtml(permPath)}">授权写入</button>
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
      updateProgress();
      if (data.step !== undefined && _toolTimers[data.step]) {
        const elapsed = Date.now() - _toolTimers[data.step];
        const durEl = document.getElementById('tool-dur-' + data.step);
        if (durEl) durEl.textContent = elapsed > 1000 ? `${(elapsed/1000).toFixed(0)}s` : `${elapsed}ms`;
      }
      showGeneratingBadge(data.message || t('stillProcessing'));
      smartScroll(container);
      break;

    case 'llm_thinking':
      hideTyping();
      removeGeneratingBadge();
      showGeneratingBadge(t('callingAI'));
      break;

    case 'llm_response':
      if (!data.has_tool_calls) {
        removeGeneratingBadge();
      }
      break;

    case 'model_switch':
      addMessage(t('modelSwitched', { reason: data.reason || (currentLanguage === 'en' ? 'request' : '请求'), model: data.model }), 'system');
      break;
    
    case 'token':
      hideTyping();
      removeThinkingHint();
      removeGeneratingBadge();
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
      if (hasToolCalls) showGeneratingBadge(t('continueProcessing'));
      smartScroll(container);
      break;

    case 'error':
      hideTyping();
      removeThinkingHint();
      removeGeneratingBadge();
      document.querySelectorAll('.tool-status-dot.running').forEach(d => {
        d.className = 'tool-status-dot error';
      });
      addMessage('❌ ' + data.content, 'system');
      break;
    
    case 'done':
      hideTyping();
      removeThinkingHint();
      removeGeneratingBadge();
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
      if (data.content && !currentBotMsgEl) {
        currentBotMsgEl = document.createElement('div');
        currentBotMsgEl.className = 'msg bot';
        if (currentStepsEl) {
          currentStepsEl.after(currentBotMsgEl);
        } else {
          container.appendChild(currentBotMsgEl);
        }
      }
      if (currentBotMsgEl && data.content) {
        // 在最终消息前注入截图（如果有工具图片且最终回复没含图片）
        var finalHtml = renderMarkdown(data.content);
        if (_lastToolImageHtml && data.content.indexOf('![') === -1) {
          finalHtml = '<div style="margin-bottom:12px;">' + _lastToolImageHtml + '</div>' + finalHtml;
        }
        currentBotMsgEl.innerHTML = finalHtml;
        currentFinalContent = data.content;
      }
      // 重置工具图片缓存
      _lastToolImageHtml = null;
      smartScroll(container);
      break;
  }
}

// ---------- 工具图标映射 ----------

function getToolIcon(toolName) {
  const icons = {
    'read_file': '📖',
    'write_file': '✏️',
    'append_to_file': '📝',
    'list_files': '📂',
    'delete_file': '🗑️',
    'search_files': '🔍',
    'get_workspace_path': '📁',
    'run_python': '🐍',
    'get_system_info': '💻',
    'web_search': '🌐',
    'web_fetch': '📄',
  };
  return icons[toolName] || '🔧';
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
