/* streaming.js — SSE 流式发送、handleStreamEvent、typing/loading 指示器
   依赖: state.js, util.js, i18n.js, messaging.js(addMessage, addUserMessage, resizeComposer, renderAttachmentPreview) */

// ---------- 全局变量 ----------

var _currentPythonOutEl = null; // 当前 run_python 的实时日志容器
var _lastToolImageHtml = null; // 最近一次工具结果的图片 Markdown

// Agent 三段式输出卡片状态
var _agentStartTime = 0;       // 本轮开始时间戳（用于计算耗时）
var _timerInterval = null;     // 耗时更新定时器
var _currentActiveLine = null; // 当前执行中动作行 DOM

// 会话回放标记：加载历史消息时设为 true，避免重复触发工具副作用
var _isReplaying = false;
var _subagentToolStep = null;  // 当前子代理对应的工具调用 step，用于锚定胶囊行位置
var _reasoningEl = null;       // 当前轮正在实时填充的「思考过程」面板（推理模型专属）
var _reasoningFadeTimer = null;// 思考完成后「延时 3s 淡出关闭」的定时器
var _thinkingEl = null;        // thought 事件在第二段顶部临时显示的「AI 正在思考...」面板
var _thinkingFadeTimer = null; // thought 面板「延时 3s 淡出关闭」的定时器
var _frontendFetchTimeoutMs = 300000; // 前端 fetch 总超时（ms），由「参数设置」的单轮硬超时联动放大
function setFrontendFetchTimeoutMs(ms) { if (Number(ms) > 0) _frontendFetchTimeoutMs = Number(ms); }

// 页面加载时自动从后端读取硬超时配置，避免前端比后端先掐断
async function initFrontendFetchTimeout() {
  try {
    const res = await fetch('/users/me/settings');
    if (res.ok) {
      const data = await res.json();
      if (data.llm_hard_timeout_seconds) {
        setFrontendFetchTimeoutMs((Number(data.llm_hard_timeout_seconds) + 30) * 1000);
      }
    }
  } catch (e) {
    // 读取失败时保持默认 5 分钟，不影响核心功能
  }
}
var _answerBodyEl = null;      // 第三段（最终回答）的 body 容器，token 最终答案挂载于此
var _historyBodyEl = null;     // 第一段（工作耗时）的「完整历史」容器，承载所有 step
var _indicatorsEl = null;      // 第二段（当前执行）的固定指示器区（当前动作），永不被提升进历史

// 悬浮「滚动到底部」按钮状态
var _scrollBtnVisible = false;
var _scrollBtnThreshold = 100; // 距底部超过 100px 时显示按钮

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
  // ponytail: DOM 去重 — 不管 generatingBadgeEl 变量状态如何, DOM 里永远最多一个 .generating-badge
  // (否则 removeGeneratingBadge 把变量置 null 后, 下次调用会创建第二个元素导致堆积)
  const existing = document.querySelector('.generating-badge');
  if (existing) {
    existing.innerHTML = `<span class="spin"></span> ${escapeHtml(unescapeDisplay(text))}`;
    generatingBadgeEl = existing;
    smartScroll(messages);
    return;
  }
  generatingBadgeEl = document.createElement('div');
  generatingBadgeEl.className = 'generating-badge';
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
  // 清理所有残留的 generating-badge（多会话并发时每个会话可能各留下一个）
  document.querySelectorAll('.generating-badge').forEach(el => el.remove());
  generatingBadgeEl = null;
}

// ---------- 流式空闲监测 ----------

function markStreamActivity() {
  lastStreamEventAt = Date.now();
}

function startStreamIdleWatch() {
  stopStreamIdleWatch();
  markStreamActivity();
  // ponytail: 「后端仍在处理」badge 已移除 —— 思考真空期由 ping 驱动的
  // loading 指示 + 第二段工具执行实时覆盖兜底，无需额外 badge。
  // 这里仅保留「卡片尚未建立」极短窗口的兜底（实际几乎不触发，因 beginRoundRender 同步建卡）。
  streamIdleTimer = setInterval(() => {
    if (!streamingActive) return;
    const idleMs = Date.now() - lastStreamEventAt;
    if (idleMs > 1800 && !currentBotMsgEl && !currentStepsEl) {
      showTyping();
    }
  }, 800);
}

function stopStreamIdleWatch() {
  if (streamIdleTimer) {
    clearInterval(streamIdleTimer);
    streamIdleTimer = null;
  }
  // 停止空闲监测时，顺手清理它可能已创建的 badge（避免多会话切换时残留堆积）
  removeGeneratingBadge();
}

// ---------- 悬浮「滚动到底部」按钮 ----------

function initScrollToBottomBtn() {
  const messages = document.getElementById('messages');
  const btn = document.getElementById('scroll-to-bottom-btn');
  if (!messages || !btn) return;

  messages.addEventListener('scroll', function() {
    const distFromBottom = messages.scrollHeight - messages.scrollTop - messages.clientHeight;
    const shouldShow = distFromBottom > _scrollBtnThreshold;
    if (shouldShow !== _scrollBtnVisible) {
      _scrollBtnVisible = shouldShow;
      btn.classList.toggle('visible', shouldShow);
    }
  });
}

function scrollToBottomManually() {
  const messages = document.getElementById('messages');
  if (messages) {
    messages.scrollTop = messages.scrollHeight;
  }
}

// 确保函数全局可用（HTML onclick 调用）
window.scrollToBottomManually = scrollToBottomManually;

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
  if (!rt || rt.status !== 'streaming') return;
  // resume 接管场景：agent 跑在后端，前端没有真实 fetch 可 abort，只有轮询。
  // 通过 _resumeStop 停轮询、还原按钮并把该前台 run 置为 done（后台任务仍会跑完）。
  if (typeof rt._resumeStop === 'function') {
    rt._resumeStop();
    return;
  }
  if (!rt.controller) return;
  userStoppedCurrentRun = true;
  rt.controller.abort();
  addMessage(t('runStopRequested'), 'system');
  sendBtn.innerHTML = '<svg width="16" height="16" viewBox="0 0 16 16"><rect x="3" y="3" width="10" height="10" rx="2" fill="currentColor"/></svg>';
  sendBtn.disabled = true;

  // 彻底终止：仅 abort 浏览器到后端的 SSE 连接只能断开订阅，后台 driver 仍会跑完。
  // 调用后端 cancel 端点取消 driver 的 asyncio task，让真正的 stream 停止并走清理。
  const sessionId = currentSessionId || threadId;
  if (sessionId) {
    fetch(`/sessions/${encodeURIComponent(sessionId)}/cancel`, { method: 'POST' })
      .then(res => res.json().catch(() => ({})))
      .then(data => {
        // 若本就在跑、取消成功，driver 的 finally 会补发 done；前端保持还原逻辑即可。
        if (data && !data.ok && data.detail) {
          addMessage(data.detail, 'system');
        }
      })
      .catch(() => {/* 取消请求失败不阻塞按钮还原，后台 driver 继续由超时兜底 */});
  }
}

// ---------- 实时干预（打断注入） ----------

// 立即把输入框内容作为「打断指令」写入后端 inbox(next_step)，
// 由运行中 agent 在下一个 LLM 调用边界注入。不打断正在执行的工具。
function steerCurrentRun() {
  const text = (input.value || '').trim();
  const sessionId = currentSessionId || threadId;
  if (!text) {
    addMessage('⚡ ' + (t('steerEmpty') || '输入不能为空'), 'system');
    return;
  }
  if (!streamingActive) {
    addMessage('⚡ ' + (t('steerNotActive') || '当前无执行任务'), 'system');
    // 无执行任务时回退为普通发送
    send();
    return;
  }
  const btn = document.getElementById('steer-btn');
  if (btn) btn.disabled = true;
  // 注意：不在本地立即 addUserMessage —— 后端在下个 LLM 边界注入后会回放
  // 'user_message_injected' 事件（见 handleStreamEvent），由它补渲染正式 user 消息，
  // 避免此处先渲染、事件又渲染导致重复。
  input.value = '';
  pendingAttachments = [];
  renderAttachmentPreview();
  resizeComposer();
  addMessage('⚡ ' + (t('steerOutgoing') || '正在打断…'), 'system');

  fetch(`/sessions/${encodeURIComponent(sessionId)}/inject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content: text, mode: 'step' }),
  })
    .then(res => res.json())
    .then(data => {
      if (btn) btn.disabled = false;
      if (data && data.ok) {
        // 后端会在下个 LLM 边界把该消息注入并回放 user_message_injected 事件（见 stream_run），
        // 前端届时把该消息作为正式 user 消息补渲染；这里仅提示已注入。
        addMessage('⚡ ' + (t('steerQueued') || '打断指令已注入（下一步生效）'), 'system');
      } else {
        addMessage('⚡ ' + ((data && data.detail) || '注入失败'), 'system');
        // 失败时还原输入
        input.value = text;
      }
    })
    .catch(() => {
      if (btn) btn.disabled = false;
      addMessage('⚡ ' + (t('connectionInterrupted') || '连接中断'), 'system');
      input.value = text;  // 还原输入，避免丢内容
    });
}

// ---------- ask_user 征询弹窗 ----------

let askUserModalEl = null;       // 当前弹窗 DOM（同一时间只显示一个）
let askUserOptions = [];         // 当前弹窗选项
let askUserFreeAllowed = false;  // 当前弹窗是否允许自定义输入

function showAskUserModal(data) {
  const askId = data.ask_id;
  const prompt = data.prompt || (t('askUserPrompt') || '请确认');
  const options = Array.isArray(data.options) ? data.options : [];
  const allowFree = !!data.allow_free_text;
  askUserOptions = options;
  askUserFreeAllowed = allowFree;

  // 已存在弹窗则更新内容（尽量避免叠加；正常情况下同一会话一次只弹一个）
  if (askUserModalEl) {
    closeAskUserModal();
  }

  askUserModalEl = document.createElement('div');
  askUserModalEl.className = 'modal-overlay active ask-user-modal-overlay';
  askUserModalEl.dataset.askId = askId;
  // 绑定弹窗所属会话：SSE 载荷带 session_id（裸 id）/ thread_key，提交时必须用它而非常用当前会话，
  // 否则弹窗期间切换会话会把 resolve 打到别的会话导致 ask 找不到 → 「征询提交失败」。
  askUserModalEl.dataset.sessionId = data.session_id || '';
  askUserModalEl.dataset.threadKey = data.thread_key || '';
  askUserModalEl.onclick = function (ev) {
    if (ev.target === askUserModalEl) submitAskUserInternal(askId); // 点背景视为提交当前选中的选项
  };

  const card = document.createElement('div');
  card.className = 'modal-card';
  card.style.width = '440px';
  card.style.maxWidth = '90vw';

  // 标题
  const header = document.createElement('div');
  header.className = 'modal-header';
  const hTitle = document.createElement('h3');
  hTitle.textContent = (t('askUserTitle') || '等待你的确认');
  const closeBtn = document.createElement('button');
  closeBtn.className = 'modal-close';
  closeBtn.textContent = '✕';
  closeBtn.onclick = function () { resolveAskUser(askId, ''); };
  header.appendChild(hTitle);
  header.appendChild(closeBtn);
  card.appendChild(header);

  // 问题正文
  const body = document.createElement('div');
  body.className = 'modal-body';

  const promptEl = document.createElement('div');
  promptEl.className = 'ask-user-prompt';
  promptEl.textContent = prompt;
  promptEl.style.cssText = 'font-size:14px;color:#333;line-height:1.6;margin:4px 0 16px;white-space:pre-wrap;word-break:break-word;';
  body.appendChild(promptEl);

  // 选项区
  const optionsWrap = document.createElement('div');
  optionsWrap.className = 'ask-user-options';
  optionsWrap.style.cssText = 'display:flex;flex-direction:column;gap:8px;margin-bottom:12px;';
  let selectedBtn = null;
  options.forEach(function (opt, idx) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'ask-user-option';
    btn.style.cssText =
      'padding:10px 14px;border:1px solid #ddd;border-radius:8px;background:#f9f9fb;' +
      'font-size:14px;color:#333;cursor:pointer;text-align:left;transition:all .12s;';
    btn.textContent = opt;
    btn.onclick = function () {
      if (selectedBtn) { selectedBtn.style.borderColor = '#ddd'; selectedBtn.style.background = '#f9f9fb'; }
      btn.style.borderColor = '#007aff';
      btn.style.background = '#e8f0fe';
      selectedBtn = btn;
      askUserSelected = opt;
    };
    optionsWrap.appendChild(btn);
  });
  body.appendChild(optionsWrap);

  // 自定义输入区（可选）
  let freeInput = null;
  if (allowFree) {
    const freeLabel = document.createElement('label');
    freeLabel.textContent = (t('askUserFreeText') || '或输入你的意见');
    freeLabel.style.cssText = 'display:block;font-size:13px;color:#555;margin-bottom:4px;';
    body.appendChild(freeLabel);

    freeInput = document.createElement('textarea');
    freeInput.className = 'modal-input';
    freeInput.rows = 3;
    freeInput.style.cssText = 'width:100%;padding:10px 12px;border:1px solid #ddd;border-radius:8px;' +
      'font-size:14px;box-sizing:border-box;outline:none;background:#fff;margin-top:4px;resize:vertical;' +
      'font-family:inherit;';
    freeInput.placeholder = (t('askUserFreePlaceholder') || '在此输入…');
    body.appendChild(freeInput);
  }

  card.appendChild(body);

  // 底部按钮
  const footer = document.createElement('div');
  footer.className = 'modal-footer';
  footer.style.cssText = 'display:flex;justify-content:flex-end;gap:10px;padding:14px 22px 18px;';

  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'modal-btn';
  cancelBtn.textContent = (t('askUserCancel') || '取消');
  cancelBtn.onclick = function () { resolveAskUser(askId, ''); };
  footer.appendChild(cancelBtn);

  const submitBtn = document.createElement('button');
  submitBtn.className = 'modal-btn modal-btn-primary';
  submitBtn.textContent = (t('askUserSubmit') || '提交');
  submitBtn.onclick = function () {
    let ans = askUserSelected || '';
    if (freeInput && freeInput.value.trim()) ans = freeInput.value.trim();
    const answer = ans || (options.length === 1 ? options[0] : '');
    resolveAskUser(askId, answer);
  };
  footer.appendChild(submitBtn);

  card.appendChild(footer);
  askUserModalEl.appendChild(card);
  document.body.appendChild(askUserModalEl);
}

// 当前已选中的选项（全局，供提交读取）
let askUserSelected = '';

// 点背景 / 关闭按钮时，如有选中的选项则提交它，否则取消；都不选则用唯一选项
function submitAskUserInternal(askId, freeInputEl) {
  let ans = askUserSelected || '';
  if (freeInputEl && freeInputEl.value && freeInputEl.value.trim()) {
    ans = freeInputEl.value.trim();
  }
  // 单选项且用户未做任何选择 → 默认选它（最符合「确认」语义）
  if (!ans && askUserOptions.length === 1) ans = askUserOptions[0];
  resolveAskUser(askId, ans);
}

function closeAskUserModal() {
  if (askUserModalEl && askUserModalEl.parentNode) {
    askUserModalEl.parentNode.removeChild(askUserModalEl);
  }
  askUserModalEl = null;
  askUserSelected = '';
  askUserOptions = [];
  askUserFreeAllowed = false;
}

function resolveAskUser(askId, answer) {
  // 优先用弹窗绑定所属会话（若该会话并未被用户切走则得相同值；若已切走则仍是原会话），
  // 兜底退回全局当前会话——避免弹窗期间切换会话导致 resolve 打到别的会话、ask 找不到。
  const boundSession = (askUserModalEl && askUserModalEl.dataset.sessionId) || '';
  const sessionId = boundSession || currentSessionId || threadId;
  closeAskUserModal();
  if (!sessionId || !askId) return;
  // 注意：后端路由挂在根路径（无 /agent 前缀），路径写错会 404 → 提交必然失败。
  fetch(`/sessions/${encodeURIComponent(sessionId)}/ask/${encodeURIComponent(askId)}/resolve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ answer: answer || '' }),
  })
    .then(function (resp) {
      // 显式检查 HTTP 状态：路由写错(404)/鉴权失败(401) 的响应体不含 resolved 字段，
      // 过去会被笼统报成"征询提交失败"，掩盖了真实的 404（排查时耗了很久）。带上状态码。
      if (!resp.ok) {
        addMessage('⚠️ ' + (t('askUserFailed') || '征询提交失败') + ' (HTTP ' + resp.status + ')', 'system');
        return null;
      }
      return resp.json();
    })
    .then(function (data) {
      if (data && !data.resolved) {
        addMessage('⚠️ ' + (t('askUserFailed') || '征询提交失败'), 'system');
      }
    })
    .catch(function () {
      addMessage('⚠️ ' + (t('connectionInterrupted') || '连接中断'), 'system');
    });
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
  rt._rs._subagentToolStep = _subagentToolStep;
  rt._rs._lastToolImageHtml = _lastToolImageHtml;
  rt._rs._reasoningEl = _reasoningEl;
  rt._rs._thinkingEl = _thinkingEl;
  rt._rs._answerBodyEl = _answerBodyEl;
  rt._rs._historyBodyEl = _historyBodyEl;
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
  _subagentToolStep = rt._rs._subagentToolStep;
  _lastToolImageHtml = rt._rs._lastToolImageHtml;
  _reasoningEl = rt._rs._reasoningEl;
  _thinkingEl = rt._rs._thinkingEl;
  _answerBodyEl = rt._rs._answerBodyEl;
  _historyBodyEl = rt._rs._historyBodyEl;
}

// 创建新一轮「三段式」输出卡片骨架，并初始化本轮所需的全局回合状态。
// 既被 send()（发起新请求）调用，也被 reconstructStreamingSession()（切回后台运行会话）调用，
// 两者共用同一套骨架，保证可见区与后台缓冲重建出的画面结构一致。
//
// 三段式结构（每段独立折叠）：
//   第一段 seg-time   工作耗时    —— 默认折叠
//   第二段 seg-tool   工具执行    —— 默认展开，顶部实时显示 LLM 思考过程，下方为当前动作+工具卡片
//   第三段 seg-answer 最终回答    —— 保持现状
function beginRoundRender(rt) {
  currentStepsEl = null;
  currentBotMsgEl = null;
  currentFinalContent = '';
  totalSteps = 0;
  hasToolCalls = false;
  generatingBadgeEl = null;
  _agentStartTime = Date.now();
  _currentActiveLine = null;
  _subagentToolStep = null;
  _lastToolImageHtml = null;  // 清空上一轮残留的工具截图，避免串入本轮最终输出
  _reasoningEl = null;
  _thinkingEl = null;
  _answerBodyEl = null;

  const container = document.getElementById('messages');

  var responseCard = document.createElement('div');
  responseCard.className = 'agent-response';
  responseCard.dataset.roundId = 'r-' + Date.now() + '-' + (rt ? rt.key : 'x');

  // ── 第一段：工作耗时（默认折叠）── 内部承载「完整的全部 step 历史」
  var segTime = document.createElement('div');
  segTime.className = 'seg seg-time collapsed';
  segTime.innerHTML =
    '<div class="seg-header">' +
      '<span class="seg-arrow">▶</span>' +
      '<div class="agent-avatar">🤖</div>' +
      '<span class="agent-time"><span class="agent-time-label">工作耗时:</span> <span class="agent-time-val">0s</span></span>' +
    '</div>' +
    '<div class="seg-body">' +
      '<div class="seg-time-summary"></div>' +
      '<div class="seg-history"></div>' +
      '<div class="seg-collapse-bar">' +
        '<button type="button" class="seg-collapse-btn" title="' + (t('collapseUpTip') || '折叠工作耗时区域') + '">' +
          '<span class="seg-collapse-arrow">▲</span>' +
          '<span class="seg-collapse-text">' + (t('collapseUp') || '向上收起') + '</span>' +
        '</button>' +
      '</div>' +
    '</div>';
  segTime.querySelector('.seg-header').onclick = function() { segTime.classList.toggle('collapsed'); };
  // 展开区底部「向上收起」快捷按钮：step 历史很长时，看完后无需翻回最顶部，底部一键收起
  var segTimeCollapseBtn = segTime.querySelector('.seg-collapse-btn');
  if (segTimeCollapseBtn) {
    segTimeCollapseBtn.onclick = function(e) {
      e.preventDefault();
      e.stopPropagation();
      if (segTime.classList.contains('collapsed')) return;  // 已折叠则忽略
      segTime.classList.add('collapsed');
      // 折叠后把「工作耗时」段头滚到滚动容器顶部，让用户立即看到该段已收起。
      // 用同步 scrollTop 赋值（smooth 异步滚动在长列表容器中不可靠），确保一次到位。
      var segTimeHeader = segTime.querySelector('.seg-header');
      var msgContainer = document.getElementById('messages');
      if (segTimeHeader && msgContainer) {
        var rect = segTimeHeader.getBoundingClientRect();
        var containerRect = msgContainer.getBoundingClientRect();
        msgContainer.scrollTop += (rect.top - containerRect.top);
      }
    };
  }
  responseCard.appendChild(segTime);
  _historyBodyEl = segTime.querySelector('.seg-history');  // 完整 step 历史进这里

  // ── 第二段：当前执行工具（默认展开）── 仅展示「最新的一个 step」，思考面板置顶
  // pending-header：首个工具/思考事件到达前隐藏段标题（简单问答流式期间不显示空的「工具执行」栏）
  var segTool = document.createElement('div');
  segTool.className = 'seg seg-tool pending-header';
  segTool.innerHTML =
    '<div class="seg-header">' +
      '<span class="seg-arrow">▶</span>' +
      '<span class="seg-title">' + escapeHtml(t('toolExecution') || '工具执行') + '</span>' +
    '</div>' +
    '<div class="seg-body">' +
      '<div class="seg-tool-indicators"></div>' +   // 固定：当前动作（永不被提升进历史）
      '<div class="seg-tool-current"></div>' +       // 仅承载「最新一个 step」，新 step 开始时旧块被提升进历史
    '</div>';
  segTool.querySelector('.seg-header').onclick = function() { segTool.classList.toggle('collapsed'); };
  responseCard.appendChild(segTool);
  _indicatorsEl = segTool.querySelector('.seg-tool-indicators');
  var bodyEl = segTool.querySelector('.seg-tool-current');

  // 第二行：当前动作（初始隐藏）—— 放入第二段「固定指示器区」（不被提升）
  var activeLineEl = document.createElement('div');
  activeLineEl.className = 'agent-active-line';
  activeLineEl.style.display = 'none';
  _indicatorsEl.appendChild(activeLineEl);
  _currentActiveLine = activeLineEl;

  // ── 第三段：最终回答（默认展开）──
  var segAnswer = document.createElement('div');
  segAnswer.className = 'seg seg-answer';
  segAnswer.innerHTML =
    '<div class="seg-header">' +
      '<span class="seg-arrow">▶</span>' +
      '<span class="seg-title">' + escapeHtml(t('finalAnswer') || '最终回答') + '</span>' +
    '</div>' +
    '<div class="seg-body"></div>';
  segAnswer.querySelector('.seg-header').onclick = function() { segAnswer.classList.toggle('collapsed'); };
  responseCard.appendChild(segAnswer);
  _answerBodyEl = segAnswer.querySelector('.seg-body');

  container.appendChild(responseCard);
  currentStepsEl = bodyEl;  // 工具卡片 / thought / 思考面板 都进第二段 body

  // 物理移除上一轮/上一会话的 todo 面板，避免跨会话泄漏
  if (_currentTodoPanel && _currentTodoPanel.parentNode) {
    _currentTodoPanel.remove();
  }
  _currentTodoPanel = null;

  // 启动耗时计时器（挂在 runtime 上，避免多会话并发时互相清掉对方的定时器）
  if (rt && rt._timerInterval) clearInterval(rt._timerInterval);
  rt._timerInterval = setInterval(function() {
    var elapsed = Date.now() - _agentStartTime;
    var valEl = segTime.querySelector('.agent-time-val');
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

async function send(queuedText) {
  const text = (queuedText || input.value).trim();
  const attachments = pendingAttachments.slice();
  if ((!text && attachments.length === 0)) return;

  // 目标会话 = 当前可见会话（用户始终对正在看的会话发请求）
  const targetSessionId = currentSessionId || threadId;
  const targetSource = currentSessionSource || 'web';
  const targetKey = visibleSessionKey || (targetSessionId + '_' + targetSource);
  const rt = getOrCreateRuntime(targetSessionId, targetSource);
  // 该会话仍在执行中：把消息推入 push 队列（不打断当前回复），
  // 本轮结束后由 send() 的 finally 按序自动发送；红色按钮才是「停止」。
  if (rt.status === 'streaming') {
    if (!queuedText && text) {
      rt.interventionQueue = rt.interventionQueue || [];
      rt.interventionQueue.push(text);
      input.value = '';
      pendingAttachments = [];
      renderAttachmentPreview();
      resizeComposer();
      addMessage('📥 ' + t('pushQueued'), 'system');
    }
    return;
  }

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
  // 前端总超时：兜底保护，避免后端/网络异常导致 fetch 永久挂起。
  // 基础 5 分钟（300000ms）确保后端重试序列有机会跑完；若「参数设置」里的单轮硬超时更大，
  // 则通过 setFrontendFetchTimeoutMs 联动放大，避免前端比后端先掐断（否则又会卡满超时）。
  let fetchTimedOut = false;
  const fetchTimeoutMs = Math.max(300000, _frontendFetchTimeoutMs); // 至少 5min，且跟随后端硬超时
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
        provider: (typeof composerProvider !== 'undefined' ? composerProvider : '') || '',
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
      // 连接中断：只在未收到终端事件时才处理，避免 done 已到达后的假报警。
      // 后端 agent 跑在与 HTTP 连接解耦的后台 driver 里（见 agent.py _drive_agent_stream），
      // 锁屏/切后台/网络抖动掐断的只是浏览器这条 SSE 订阅连接，任务仍在服务器继续并实时落盘。
      // 因此先尝试自动接管后台 run；只有后端也找不到本轮 run（真断网/服务重启）才提示中断。
      document.querySelectorAll('.tool-status-dot.running').forEach(d => {
        d.className = 'tool-status-dot error';
      });
      const _intKey = targetKey;
      recoverInterruptedStream(targetSessionId, targetSource).then(function(ok) {
        if (!ok && visibleSessionKey === _intKey) {
          addMessage(t('connectionInterrupted'), 'system');
        }
      }).catch(function() {
        if (visibleSessionKey === _intKey) addMessage(t('connectionInterrupted'), 'system');
      });
    } else {
      // 已收到完整回复但连接异常关闭：只清理残留工具状态，不弹中断提示
      document.querySelectorAll('.tool-status-dot.running').forEach(d => {
        d.className = 'tool-status-dot error';
      });
    }
  } finally {
    clearTimeout(fetchTimeout);
    closePythonProgress();
    // 空闲定时器是全局单例，无论该会话是否可见都必须停止（否则后台会话结束后定时器继续跑，
    // 切到别的会话时不断创建 orphaned "后端仍在处理..." badge）
    stopStreamIdleWatch();
    // 仅当该会话仍是可见渲染目标时，才清理可见区的「执行中」UI 状态，
    // 否则会误伤正在看的另一个会话的画面（后台会话结束不应扰动可见区）。
    // 用 try/catch 包裹：这里任何一步抛异常都不能阻断后续的 syncStreamingActive()，
    // 否则发送按钮无法从「停止(红色)」还原为「发送」（历史 bug 就是 finally 中途抛错导致）。
    if (rt.live) {
      try {
        _finalizeReasoning();  // 流结束：定稿推理模型「思考过程」面板并淡出
        _finalizeThinking();   // 流结束：确保 thought 顶部面板进入完成淡出
        hideTyping();
        removeGeneratingBadge();
        if (_timerInterval) { clearInterval(_timerInterval); _timerInterval = null; }
        document.querySelectorAll('.thinking-step').forEach(el => el.remove());
        document.querySelectorAll('.tool-status-dot.running').forEach(d => {
          d.className = 'tool-status-dot done';
        });
        if (_currentActiveLine) { _currentActiveLine.style.display = 'none'; }
        input.focus();
      } catch (uiErr) {
        console.error('[send finally] 清理可见区 UI 时出错（不影响按钮还原）:', uiErr);
      }
    }
    if (rt._timerInterval) { clearInterval(rt._timerInterval); rt._timerInterval = null; }
    // 该会话本轮结束（成功/失败/中止都算结束，spinner 应消失）
    rt.status = endedWithError ? 'error' : 'done';
    rt.controller = null;
    rt.live = false;
    currentAbortController = null;

    // push 队列：本轮结束后，按序自动发送流式执行中入队的消息（send 入队时已清空输入框）。
    // 放在「后台会话 runtime 清理」之前，保证用户切走后、后台会话结束时队列消息也不丢。
    // 用 setTimeout(0) 延后一拍：等 finally 剩余清理（按钮还原等）完成后再发起新一轮。
    const pushQueue = rt.interventionQueue;
    if (pushQueue && pushQueue.length) {
      const next = pushQueue.shift();
      setTimeout(function() {
        send(next).catch(function(err) {
          console.error('[push] 队列消息发送失败:', err);
        });
      }, 0);
    }

    // 内存回收: 后台完成的会话不再需要保留(历史消息从后端加载, reconstruct 不依赖它)
    // 可见会话暂留——等 switchSession 切走时再清
    if (visibleSessionKey !== rt.key && typeof sessionRuntimes !== 'undefined') {
      sessionRuntimes.delete(rt.key);
    }
    // 派生态：可见/加载态、运行指示器、侧边栏列表
    syncStreamingActive();
    updateRunIndicators();
    // 兜底：如果 data-key 与 runtime key 因 source 回退不一致，导致 updateRunIndicators 没清掉，
    // 直接按 rt.key 找到对应侧边栏会话项并移除 running 状态。
    if (rt.key) {
      document.querySelectorAll('.session-item, .psession-item').forEach(function(el) {
        if (el.dataset.key === rt.key) el.classList.remove('running');
      });
    }
    refreshStats();
    loadSessions().finally(updateRunIndicators);
  }
}

// ---------- 思考/推理面板定稿（模块级：send() finally 与 handleStreamEvent 共用）----------
// 注意：这两个函数必须在模块作用域，不能嵌套进 handleStreamEvent。
// 否则 send() 的 finally 块调用 _finalizeThinking() 会抛 ReferenceError，
// 导致 finally 在 syncStreamingActive() 之前中断，发送按钮无法从「停止(红色)」还原为「发送」。

// 推理模型思考过程面板：把本轮累积的推理文本定稿（markdown 渲染 + 标记已完成）
function _finalizeReasoning() {
  if (!_reasoningEl || !_reasoningEl.isConnected) { _reasoningEl = null; return; }
  const el = _reasoningEl;
  const stateEl = el.querySelector('.reasoning-state');
  if (stateEl) stateEl.textContent = '已完成';
  const contentEl = el.querySelector('.reasoning-content');
  if (contentEl) {
    const raw = contentEl.textContent || '';
    if (raw.trim()) {
      try { contentEl.innerHTML = renderMarkdown(raw); }
      catch (e) { /* 渲染失败则保留纯文本 */ }
    }
  }
  el.classList.add('done');
  _reasoningEl = null;
  // 思考完成：延时 5s 后淡出关闭（用户要求由 3s 延长）
  clearTimeout(_reasoningFadeTimer);
  _reasoningFadeTimer = setTimeout(function() {
    el.classList.add('fading');                 // 触发 CSS opacity 过渡
    setTimeout(function() { if (el.isConnected) el.remove(); }, 550);  // 真正从 DOM 移除（关闭）
  }, 5000);
}

// thought 事件在第二段顶部临时显示的思考面板：标记完成并延时 5s 淡出关闭。
// 与 reasoning 面板逻辑类似，但 thought 内容还会以 .thought-block 形式保留进历史。
// immediate=true 时立即开始淡出（用于新 thought 开始时替换旧面板，避免堆叠）。
function _finalizeThinking(immediate) {
  if (!_thinkingEl || !_thinkingEl.isConnected) { _thinkingEl = null; return; }
  const el = _thinkingEl;
  const stateEl = el.querySelector('.reasoning-state');
  if (stateEl) stateEl.textContent = '已完成';
  el.classList.add('done');
  _thinkingEl = null;
  clearTimeout(_thinkingFadeTimer);
  const fadeDelay = immediate ? 0 : 5000;
  _thinkingFadeTimer = setTimeout(function() {
    el.classList.add('fading');
    setTimeout(function() { if (el.isConnected) el.remove(); }, 550);
  }, fadeDelay);
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

  // 把「第二段·当前执行」容器里的步骤块（除思考面板外）全部移入「第一段·完整历史」。
  // 调用时机：每个新 step 开始（thought / tool_start / subagent_start）之前，确保第二段只留最新一个 step。
  function _promoteCurrentToHistory() {
    if (!_historyBodyEl || !currentStepsEl) return;
    const kids = Array.from(currentStepsEl.children);
    for (const kid of kids) {
      if (kid.classList && kid.classList.contains('reasoning-block')) continue;  // 思考面板单独管理，不进历史
      if (kid.classList && kid.classList.contains('thinking-step')) continue;    // 「继续分析」动画只留在当前执行区，不进历史
      if (kid.classList && kid.classList.contains('streaming-final')) continue;  // 正在流式的临时正文由 thought/tool_start/done 归位，不进历史
      if (kid === _reasoningEl) continue;
      _historyBodyEl.appendChild(kid);
    }
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

  // 子代理卡片渲染（tool-card 样式，与主 agent 工具调用一致）
  const _subagentStreams = new Map();  // capId -> EventSource

  function renderSubagentCards(capsules, forcedStatus, step) {
    if (!capsules || !currentStepsEl) return;
    const iconMap = { searcher: '🔍', coder: '<>', reviewer: '👁', debugger: '🐛' };
    const incomingIds = new Set(capsules.map(c => String(c.id)));

    // 清理本轮已不存在的子代理卡片
    Array.from(currentStepsEl.querySelectorAll('.tool-card[data-sa-card]')).forEach(el => {
      if (!incomingIds.has(el.dataset.capId)) el.remove();
    });

    capsules.forEach(cap => {
      const capId = String(cap.id);
      const status = forcedStatus || cap.status || 'running';
      const icon = iconMap[cap.agent_type] || '⚙';

      // 复用或创建卡片
      let card = currentStepsEl.querySelector(`.tool-card[data-cap-id="${capId}"]`);
      if (!card) {
        card = document.createElement('div');
        card.className = 'tool-card open';
        card.dataset.capId = capId;
        card.dataset.saCard = '1';
        if (step !== undefined && step !== null) card.dataset.step = String(step);

        card.innerHTML = `
          <div class="tool-card-header" onclick="toggleToolCard(this)">
            <span class="arrow">▶</span>
            <span class="tool-icon">${icon}</span>
            <span class="tool-label">子代理:</span>
            <span class="tool-name-inline">${escapeHtml(cap.agent_type)} #${cap.id}</span>
            <span class="tool-duration"></span>
            <span class="tool-status-dot running"></span>
          </div>
          <div class="tool-card-body">
            <div class="tool-section-label">任务</div>
            <pre class="tool-code-block">${escapeHtml(cap.task || '')}</pre>
            <div class="sa-tools"></div>
            <div class="subagent-live-log"><pre class="sa-log-pre"></pre></div>
          </div>`;
        currentStepsEl.appendChild(card);

        // 启动实时日志流
        _ensureCapsuleStream(capId, card);
      } else {
        // 更新状态
        const dot = card.querySelector('.tool-status-dot');
        if (dot) dot.className = 'tool-status-dot ' + (status === 'done' ? 'done' : status === 'error' ? 'error' : 'running');

        // 写最终结果
        if (status === 'done' && cap.result) {
          const logPre = card.querySelector('.sa-log-pre');
          if (logPre && !logPre.dataset.hasResult) {
            logPre.textContent += `\n─── 完成 ───\n${unescapeDisplay(String(cap.result)).slice(0, 2000)}`;
            logPre.dataset.hasResult = '1';
          }
        } else if (status === 'error' && cap.result) {
          const logPre = card.querySelector('.sa-log-pre');
          if (logPre && !logPre.dataset.hasResult) {
            logPre.textContent += `\n─── 失败 ───\n${unescapeDisplay(String(cap.result)).slice(0, 800)}`;
            logPre.dataset.hasResult = '1';
          }
        }
      }
      // 完成：折叠胶囊（与主 agent 一致，可点击展开查看）
      if (status === 'done' || status === 'error') {
        card.classList.remove('open');
        // 兜底归档：tool_end 未实时处理的残留工具卡片也折叠归档，执行区不留已完成卡片
        _archiveResidualTools(card);
      }
      // 历史回放/补全：用持久化的事件/日志重建工具卡片与思考过程
      if (Array.isArray(cap.tools) && cap.tools.length) {
        _renderSubagentToolsFromHistory(card, cap.tools);
      }
      if (Array.isArray(cap.logs) && cap.logs.length) {
        _renderSubagentLogsFromHistory(card, cap.logs);
      }
    });
    smartScroll(container);
  }

  function _ensureCapsuleStream(capId, cardEl) {
    if (_subagentStreams.has(capId)) return;
    if (_isReplaying) return;
    const logPre = cardEl.querySelector('.sa-log-pre');
    if (!logPre) return;
    const es = new EventSource(`/subagent-progress/${capId}`);
    es.onmessage = (e) => {
      try {
        const d = JSON.parse(e.data || '{}');
        if (d.event === 'tool_start') {
          _renderSubagentToolCard(cardEl, d);
        } else if (d.event === 'tool_end') {
          _updateSubagentToolCard(cardEl, d);
        } else {
          // 文本日志：ai/error/done 加图标前缀；tool 类结构化事件已走卡片分支，不进文本
          // 兼容子代理 thought 日志：若 text 已以对应前缀开头（后端直接存了 "💭 ..."），不再重复加。
          let prefix = d.cat === 'ai' ? '💭 ' : d.cat === 'error' ? '❌ ' : d.cat === 'done' ? '✅ ' : '';
          const rawText = String(d.text || '');
          if (prefix && rawText.startsWith(prefix)) prefix = '';
          logPre.textContent += `[${d.cat}] ${prefix}${unescapeDisplay(d.text)}\n`;
          smartScroll(container);
        }
      } catch {}
    };
    es.onerror = () => {
      es.close();
      _subagentStreams.delete(capId);
    };
    _subagentStreams.set(capId, es);
  }

  // 子代理内部工具调用：在胶囊卡片内渲染与主 agent 同款的工具卡片
  function _renderSubagentToolCard(cardEl, d) {
    const toolsBox = cardEl.querySelector('.sa-tools');
    if (!toolsBox) return;
    const tid = String(d.tool_id || '');
    if (!tid) return;
    let exists = false;
    toolsBox.querySelectorAll('.tool-card[data-tool-id]').forEach(function (el) {
      if (el.dataset.toolId === tid) exists = true;
    });
    if (exists) return;
    const icon = getToolIcon(d.tool_name);
    let argsText = d.tool_args;
    if (typeof argsText !== 'string') argsText = JSON.stringify(argsText || {}, null, 2);
    const div = document.createElement('div');
    div.className = 'tool-card open sa-inner-tool';
    div.dataset.toolId = tid;
    div.innerHTML = `
      <div class="tool-card-header" onclick="toggleToolCard(this)">
        <span class="arrow">▶</span>
        <span class="tool-icon">${icon}</span>
        <span class="tool-label">调用工具:</span>
        <span class="tool-name-inline">${escapeHtml(d.tool_name)}</span>
        <span class="tool-status-dot running"></span>
      </div>
      <div class="tool-card-body">
        <div class="tool-section-label">参数</div>
        <pre class="tool-code-block">${escapeHtml(unescapeDisplay(argsText))}</pre>
        <div class="sa-tool-output"></div>
      </div>`;
    toolsBox.appendChild(div);
    smartScroll(container);
  }

  function _updateSubagentToolCard(cardEl, d) {
    const toolsBox = cardEl.querySelector('.sa-tools');
    if (!toolsBox) return;
    const tid = String(d.tool_id || '');
    if (!tid) return;
    let div = null;
    toolsBox.querySelectorAll('.tool-card[data-tool-id]').forEach(function (el) {
      if (el.dataset.toolId === tid) div = el;
    });
    if (!div) return;
    const dot = div.querySelector('.tool-status-dot');
    const failed = d.tool_status === 'error';
    if (dot) dot.className = 'tool-status-dot ' + (failed ? 'error' : 'done');
    const outBox = div.querySelector('.sa-tool-output');
    if (outBox) {
      let outText = d.tool_output;
      if (typeof outText !== 'string') outText = JSON.stringify(outText);
      if (outText && outText !== '""' && outText !== 'undefined') {
        outBox.innerHTML = '<div class="tool-section-label">输出</div><pre class="tool-code-block">' +
          escapeHtml(unescapeDisplay(outText)).slice(0, 8000) + '</pre>';
      }
    }
    // 完成：折叠卡片并移入胶囊内「已完成工具」折叠区（执行区只留正在执行的卡片，避免占满屏幕）
    div.classList.remove('open');
    const doneBox = _ensureDoneToolsBox(cardEl);
    doneBox.querySelector('.sa-done-body').appendChild(div);
    _updateDoneToolsCount(doneBox);
    smartScroll(container);
  }

  // 子代理胶囊内「已完成工具调用」折叠容器：完成即归档，默认折叠只占一行
  function _ensureDoneToolsBox(cardEl) {
    let box = cardEl.querySelector('.sa-tools-done');
    if (box) return box;
    box = document.createElement('div');
    box.className = 'sa-tools-done';
    box.innerHTML =
      '<div class="sa-done-header" onclick="toggleDoneTools(this)">' +
        '<span class="arrow">▶</span>' +
        '<span class="sa-done-label">已完成工具调用</span>' +
        '<span class="sa-done-count">(0)</span>' +
      '</div>' +
      '<div class="sa-done-body"></div>';
    const toolsBox = cardEl.querySelector('.sa-tools');
    const body = cardEl.querySelector('.tool-card-body');
    if (toolsBox) toolsBox.parentNode.insertBefore(box, toolsBox);
    else if (body) body.appendChild(box);
    return box;
  }

  function _updateDoneToolsCount(box) {
    const n = box.querySelectorAll('.sa-done-body .tool-card').length;
    const c = box.querySelector('.sa-done-count');
    if (c) c.textContent = '(' + n + ')';
  }

  // subagent_end 兜底：把执行区残留的已完成工具卡片（tool_end 未实时归档的）折叠移入归档区。
  // ponytail: 折叠不再依赖 tool_end 事件时序——事件丢失/查找失败时，结束时刻统一收口。
  function _archiveResidualTools(cardEl) {
    const toolsBox = cardEl.querySelector('.sa-tools');
    if (!toolsBox) return;
    const leftovers = Array.from(toolsBox.querySelectorAll('.tool-card[data-tool-id]'));
    if (!leftovers.length) return;
    const doneBox = _ensureDoneToolsBox(cardEl);
    leftovers.forEach(function (div) {
      div.classList.remove('open');
      const dot = div.querySelector('.tool-status-dot');
      if (dot && dot.classList.contains('running')) dot.className = 'tool-status-dot done';
      doneBox.querySelector('.sa-done-body').appendChild(div);
    });
    _updateDoneToolsCount(doneBox);
  }

  // 历史回放/补全：用持久化的工具事件（subagent_end 携带的 cap.tools）重建工具卡片。
  // 实时流已渲染的卡片按 tool_id 去重；回放场景默认全部折叠进「已完成工具调用」归档区。
  function _renderSubagentToolsFromHistory(cardEl, tools) {
    if (!Array.isArray(tools) || !tools.length) return;
    const toolsBox = cardEl.querySelector('.sa-tools');
    if (!toolsBox) return;
    const doneBox = _ensureDoneToolsBox(cardEl);
    const doneBody = doneBox.querySelector('.sa-done-body');
    const toolExists = function (tid) {
      let found = false;
      [toolsBox, doneBody].forEach(function (box) {
        if (!box) return;
        box.querySelectorAll('.tool-card[data-tool-id]').forEach(function (el) {
          if (el.dataset.toolId === String(tid)) found = true;
        });
      });
      return found;
    };
    tools.forEach(function (ev) {
      const tid = String(ev.tool_id || '');
      if (!tid) return;
      if (ev.event === 'tool_start') {
        if (toolExists(tid)) return;
        const icon = getToolIcon(ev.tool_name);
        let argsText = ev.tool_args;
        if (typeof argsText !== 'string') argsText = JSON.stringify(argsText || {}, null, 2);
        const div = document.createElement('div');
        div.className = 'tool-card sa-inner-tool';  // 不带 open：归档区默认折叠
        div.dataset.toolId = tid;
        div.innerHTML =
          '<div class="tool-card-header" onclick="toggleToolCard(this)">' +
            '<span class="arrow">▶</span>' +
            '<span class="tool-icon">' + icon + '</span>' +
            '<span class="tool-label">调用工具:</span>' +
            '<span class="tool-name-inline">' + escapeHtml(ev.tool_name || '') + '</span>' +
            '<span class="tool-status-dot done"></span>' +
          '</div>' +
          '<div class="tool-card-body">' +
            '<div class="tool-section-label">参数</div>' +
            '<pre class="tool-code-block">' + escapeHtml(unescapeDisplay(argsText)) + '</pre>' +
            '<div class="sa-tool-output"></div>' +
          '</div>';
        doneBody.appendChild(div);
      } else if (ev.event === 'tool_end') {
        let div = null;
        doneBody.querySelectorAll('.tool-card[data-tool-id]').forEach(function (el) {
          if (el.dataset.toolId === tid) div = el;
        });
        if (!div) return;
        const failed = ev.tool_status === 'error';
        const dot = div.querySelector('.tool-status-dot');
        if (dot) dot.className = 'tool-status-dot ' + (failed ? 'error' : 'done');
        const outBox = div.querySelector('.sa-tool-output');
        if (outBox) {
          let outText = ev.tool_output;
          if (typeof outText !== 'string') outText = JSON.stringify(outText);
          if (outText && outText !== '""' && outText !== 'undefined') {
            outBox.innerHTML = '<div class="tool-section-label">输出</div><pre class="tool-code-block">' +
              escapeHtml(unescapeDisplay(outText)).slice(0, 8000) + '</pre>';
          }
        }
      }
    });
    _updateDoneToolsCount(doneBox);
  }

  // 历史回放/补全：用持久化日志（subagent_end 携带的 cap.logs）重建子代理思考过程。
  // 后端 text 已包含图标前缀（如 "💭 ..."），此处直接复用，不再重复加。
  function _renderSubagentLogsFromHistory(cardEl, logs) {
    if (!Array.isArray(logs) || !logs.length) return;
    const logPre = cardEl.querySelector('.sa-log-pre');
    if (!logPre) return;
    const seen = new Set();
    logs.forEach(function (ln) {
      const text = String(ln.text || '').trim();
      if (!text) return;
      const key = ln.cat + ':' + text;
      if (seen.has(key)) return;
      seen.add(key);
      logPre.textContent += '[' + ln.cat + '] ' + unescapeDisplay(text) + '\n';
    });
    smartScroll(container);
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
      _finalizeThinking();  // 子代理开始，结束 thought 顶部面板
      hideTyping();
      _promoteCurrentToHistory();  // 新 step 开始：把上一个 step 提升进完整历史
      hasToolCalls = true;
      if (!data.capsules || !data.capsules.length) {
        console.warn('[子代理] subagent_start 无胶囊数据:', data);
        break;
      }
      renderSubagentCards(data.capsules, 'running', _subagentToolStep);
      break;

    case 'subagent_end':
      renderSubagentCards(data.capsules, 'done', _subagentToolStep);
      _subagentToolStep = null;
      showGeneratingBadge('🔄 正在汇总...');
      break;

    case 'reasoning': {
      // 推理模型的思考过程（reasoning_content / thinking）：逐块实时填充到「思考过程」面板
      const delta = data.content || '';
      if (!delta) break;
      if (!_reasoningEl || !_reasoningEl.isConnected) {
        ensureStepsContainer();
        const block = document.createElement('div');
        block.className = 'reasoning-block';
        block.innerHTML =
          '<div class="reasoning-header">💭 ' + escapeHtml(t('modelThinking') || '模型思考过程') +
          ' <span class="reasoning-state">思考中…</span></div>' +
          '<div class="reasoning-content"></div>';
        // 置于第二段顶部：思考过程在最上，工具执行在其下（用户要求）
        currentStepsEl.insertBefore(block, currentStepsEl.firstChild);
        _reasoningEl = block;
      }
      const contentEl = _reasoningEl.querySelector('.reasoning-content');
      if (contentEl) contentEl.textContent += delta;  // 增量追加纯文本，成本低且防 XSS
      hideTyping();  // 顶部「思考中」让位给可见的思考面板
      removeGeneratingBadge();
      smartScroll(container);
      break;
    }

    case 'reflection': {
      // 过程反思：执行监督者发现偏航时给出的纠偏提示，实时展示在思考面板
      const advice = data.content || '';
      if (!advice) break;
      ensureStepsContainer();
      _promoteCurrentToHistory();
      const reflDiv = document.createElement('div');
      reflDiv.className = 'reasoning-block reflection-block';
      reflDiv.innerHTML =
        '<div class="reasoning-header">🧭 ' + escapeHtml(t('reflection') || '执行监督') +
        ' <span class="reasoning-state">发现偏航</span></div>' +
        '<div class="reasoning-content">' + renderMarkdown(advice) + '</div>';
      currentStepsEl.insertBefore(reflDiv, currentStepsEl.firstChild);
      smartScroll(container);

      // 反思卡片默认只展示在执行区几秒，延时淡出后搬入「第一段·工作耗时」历史，
      // 避免执行区被持续刷新堆叠。回放期间不动 DOM，保持视觉一致。
      // ponytail: 反思频率较高（每个 step 都可能触发），若卡在执行区会越积越多；
      // 4s 足够用户扫读，4s 后移入历史（第一段默认折叠），需要时展开即可查阅。
      if (!_isReplaying) {
        setTimeout(function() {
          if (!reflDiv.isConnected) return;
          reflDiv.classList.add('fading');
          setTimeout(function() {
            if (!reflDiv.isConnected) return;
            if (_historyBodyEl && _historyBodyEl.isConnected) {
              // 标记完成 + 搬入历史（第一段默认折叠），移除 fading 恢复不透明
              reflDiv.classList.add('done', 'archived');
              reflDiv.classList.remove('fading');
              _historyBodyEl.appendChild(reflDiv);
            } else {
              reflDiv.remove();
            }
          }, 550);  // 与 CSS .reasoning-block .5s 过渡对齐
        }, 4000);
      }
      break;
    }

    case 'thought': {
      _finalizeReasoning();       // 思考阶段结束（本轮为工具轮）
      _finalizeThinking(true);    // 结束上一个 thought 顶部面板，立即淡出避免堆叠
      _promoteCurrentToHistory();  // 新 step 开始：把上一个 step 提升进完整历史
      hideTyping();
      removeThinkingHint();
      removeGeneratingBadge();
      hasToolCalls = true;
      ensureStepsContainer();
      // 首个思考事件到达：露出第二段「工具执行」标题栏
      var _cardThought = getResponseCard();
      if (_cardThought) {
        var _segToolThought = _cardThought.querySelector('.seg-tool');
        if (_segToolThought) _segToolThought.classList.remove('pending-header');
      }
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

      // ── 在第二段顶部临时显示「AI 正在思考...」面板（仅实时流；历史回放只保留记录）
      if (!_isReplaying) {
        // 兜底：清除已脱离 _thinkingEl 引用的旧顶部面板，防止连续 thought 事件堆叠
        if (currentStepsEl) {
          currentStepsEl.querySelectorAll('.thinking-block').forEach(el => el.remove());
        }
        const thinkBlock = document.createElement('div');
        thinkBlock.className = 'reasoning-block thinking-block';
        thinkBlock.innerHTML =
          '<div class="reasoning-header"><span class="spin"></span> ' + escapeHtml(t('thinking') || 'AI 正在思考...') +
          ' <span class="reasoning-state">思考中…</span></div>' +
          '<div class="reasoning-content">' + thoughtHtml + '</div>';
        currentStepsEl.insertBefore(thinkBlock, currentStepsEl.firstChild);
        _thinkingEl = thinkBlock;
      }

      // 持久化的思考记录：直接挂到「第一段·工作耗时」历史（默认折叠），不再进执行区。
      // ponytail: 旧逻辑把 .thought-block append 到 currentStepsEl 底部，等待下一次
      // _promoteCurrentToHistory 提升进历史。这导致执行区同时存在：
      //   - 顶部 thinking-block（临时"AI 正在思考..."面板）
      //   - 底部 thought-block（持久记录，内容相同）
      // 两块内容一致，看着像两个思考区。改为直接挂到 _historyBodyEl（默认折叠），
      // 历史顺序仍正确：tool_start 会先 _promoteCurrentToHistory 把工具卡搬到历史，
      // 然后 thought-block appendChild 到历史末尾，自然排在工具卡之后。
      // 边界：_historyBodyEl 不可用时降级到 currentStepsEl，保持兼容。
      const thoughtDiv = document.createElement('div');
      thoughtDiv.className = 'thought-block';
      thoughtDiv.innerHTML = thoughtHtml;
      if (_historyBodyEl && _historyBodyEl.isConnected) {
        _historyBodyEl.appendChild(thoughtDiv);
      } else {
        currentStepsEl.appendChild(thoughtDiv);
      }
      if (!_isReplaying) {
        showThinkingHint(t('keepAnalyzing'));
      }
      smartScroll(container);
      break;
    }

    case 'tool_start':
      _finalizeReasoning();  // 工具开始 → 推理模型思考阶段结束
      _finalizeThinking();   // 工具开始 → thought 顶部面板结束
      _promoteCurrentToHistory();  // 新 step 开始：把上一个 step 提升进完整历史
      hideTyping();
      removeThinkingHint();
      removeGeneratingBadge();
      hasToolCalls = true;
      // 首个工具事件到达：露出第二段「工具执行」标题栏
      var _cardTool = getResponseCard();
      if (_cardTool) {
        var _segToolStart = _cardTool.querySelector('.seg-tool');
        if (_segToolStart) _segToolStart.classList.remove('pending-header');
      }
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

    case 'tool_output': {
      // run_shell 实时输出：追加到独立流式容器，不覆盖 tool_result 的结果区
      const outStep = data.step !== undefined ? data.step : 0;
      const card = currentStepsEl.querySelector(`.tool-card[data-step="${outStep}"]`);
      if (card) {
        const body = card.querySelector('.tool-card-body');
        let wrap = card.querySelector('.tool-stream-wrap');
        if (!wrap) {
          wrap = document.createElement('div');
          wrap.className = 'tool-stream-wrap';
          wrap.innerHTML = '<div class="tool-section-label">实时输出</div><pre class="tool-stream-output"></pre>';
          const outArea = card.querySelector(`#tool-output-${outStep}`);
          if (outArea) body.insertBefore(wrap, outArea);
          else body.appendChild(wrap);
        }
        const txt = unescapeDisplay(String(data.content || ''));
        if (txt) {
          const pre = wrap.querySelector('.tool-stream-output');
          pre.textContent += txt;
          pre.scrollTop = pre.scrollHeight;
          smartScroll(container);
        }
      }
      break;
    }

    case 'tool_result':
      closePythonProgress();
      const trStep = data.step !== undefined ? data.step : 0;
      // 优先用后端下发的真实耗时（实时流与历史回放都带），
      // 缺失时（旧历史数据）回退到前端 _toolTimers 估算
      const startedAt = _toolTimers[trStep];
      let elapsed = 0;
      if (data.duration_ms !== undefined && data.duration_ms !== null && data.duration_ms >= 0) {
        elapsed = data.duration_ms;
      } else {
        elapsed = startedAt ? Date.now() - startedAt : 0;
      }
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
                  inner.innerHTML = '<span style="font-size:12px;color:#30d158;">✅ 已授权，正在重试...</span>';
                  // 自动补一条「继续」并发送，无需用户手动输入
                  if (typeof input !== 'undefined') {
                    input.value = '继续';
                    if (typeof pendingAttachments !== 'undefined') pendingAttachments.length = 0;
                    if (typeof send === 'function') send();
                  }
                } else {
                  this.textContent = '授权失败';
                }
              } catch(e) {
                this.textContent = '网络错误';
              }
            });
            outArea.appendChild(permBtn);

            // ── 授权横幅：同步提到第二段「固定指示器区」──
            // 卡片内的按钮会随卡片被 _promoteCurrentToHistory 提升进第一段「工作耗时」（默认折叠），
            // 用户必须展开才能点。这里额外在 .seg-tool-indicators（永不被提升）顶部放一个醒目的横幅。
            if (_indicatorsEl) {
              // 去重：同一路径已有横幅则复用（遍历而非选择器，避免路径含特殊字符时抛错）
              var existingBanner = null;
              Array.prototype.forEach.call(_indicatorsEl.querySelectorAll('.perm-banner'), function(el) {
                if (el.dataset.path === permPath) existingBanner = el;
              });
              if (!existingBanner) {
                var banner = document.createElement('div');
                banner.className = 'perm-banner';
                banner.dataset.path = permPath;
                banner.innerHTML =
                  '<div class="perm-banner-title">⏳ 需要您的授权</div>' +
                  '<div class="perm-banner-desc">该文件位于工作区外，点击下方按钮授权后，在输入框输入「继续」即可重试：</div>' +
                  '<div class="perm-banner-path">📝 ' + escapeHtml(permPath) + '</div>' +
                  '<div class="perm-banner-actions">' +
                    '<button class="perm-banner-btn" data-path="' + escapeHtml(permPath) + '">授权写入</button>' +
                  '</div>';
                banner.querySelector('.perm-banner-btn').addEventListener('click', async function() {
                  var path = this.dataset.path;
                  try {
                    var r = await fetch('/permissions/grant-path', {
                      method: 'POST',
                      headers: {'Content-Type': 'application/json'},
                      body: JSON.stringify({path: path}),
                    });
                    // 同时授权目录级（与卡片内按钮行为一致）
                    var dirPath = path;
                    var lastSlash = dirPath.lastIndexOf('/');
                    if (lastSlash > 0) dirPath = dirPath.substring(0, lastSlash);
                    await fetch('/permissions/grant-path', {
                      method: 'POST',
                      headers: {'Content-Type': 'application/json'},
                      body: JSON.stringify({path: dirPath}),
                    });
                    var actions = banner.querySelector('.perm-banner-actions');
                    if (actions) {
                      actions.innerHTML = '<span class="perm-banner-ok">✅ 已授权，正在重试...</span>';
                    }
                    // 自动补一条「继续」并发送，无需用户手动输入
                    if (typeof input !== 'undefined') {
                      input.value = '继续';
                      if (typeof pendingAttachments !== 'undefined') pendingAttachments.length = 0;
                      if (typeof send === 'function') send();
                    }
                    setTimeout(function() { banner.remove(); }, 4000);
                  } catch(e) {
                    var bBtn = banner.querySelector('.perm-banner-btn');
                    if (bBtn) bBtn.textContent = '网络错误';
                  }
                });
                _indicatorsEl.insertBefore(banner, _indicatorsEl.firstChild);
              }
            }
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

    case 'context_compacted': {
      // 上下文压缩卡片（对齐 dsh-compaction 的 CompactionItem）：
      // 头部展示「压缩被调用 + 压缩后大小」，正文展开可见「压缩后的上下文」摘要。
      hideTyping();
      removeThinkingHint();
      removeGeneratingBadge();
      ensureStepsContainer();
      // 与其它 step 卡片一致：若上一 step 还在当前执行区，先提升进完整历史。
      // isConnected 守卫：历史回放时 _historyBodyEl 可能是已脱离 DOM 的旧引用，
      // 提升会把卡片移进不可见容器（只在有真实历史容器时提升）。
      if (_historyBodyEl && _historyBodyEl.isConnected && currentStepsEl && currentStepsEl.children.length) {
        _promoteCurrentToHistory();
      }
      const beforeT = data.before_tokens || 0;
      const afterT = data.after_tokens || 0;
      const savedT = Math.max(0, beforeT - afterT);
      const pct = data.reduction_pct != null ? data.reduction_pct
        : (beforeT > 0 ? Math.round(savedT / beforeT * 100) : 0);
      const triggerLabel = data.trigger === 'tool' ? (t('compactionManual') || '手动')
        : data.trigger === 'before_tool' ? (t('compactionBeforeTool') || '工具前自动')
        : (t('compactionAuto') || '自动');
      const stats = t('compactionStats', {
        a: data.before_count || 0,
        b: data.after_count || 0,
        c: beforeT.toLocaleString(),
        d: afterT.toLocaleString(),
        e: pct,
      });
      const cardDiv = document.createElement('div');
      cardDiv.className = 'tool-card compaction-card';
      if (data.compaction_id) cardDiv.dataset.compactionId = String(data.compaction_id);
      cardDiv.innerHTML =
        '<div class="tool-card-header" onclick="toggleToolCard(this)">' +
          '<span class="arrow">▶</span>' +
          '<span class="tool-icon">🧹</span>' +
          '<span class="tool-label">' + escapeHtml(t('contextCompaction') || '上下文压缩') + ':</span>' +
          '<span class="tool-name-inline">' + escapeHtml(triggerLabel) + '</span>' +
          '<span class="compaction-stats">' + escapeHtml(stats) + '</span>' +
          '<span class="tool-status-dot done"></span>' +
        '</div>' +
        '<div class="tool-card-body">' +
          '<div class="tool-section-label">' + escapeHtml(t('compactionResult') || '压缩后的上下文') + '</div>' +
          '<div class="tool-result-markdown">' + renderMarkdown(data.summary || '') + '</div>' +
        '</div>';
      currentStepsEl.appendChild(cardDiv);
      smartScroll(container);
      break;
    }

    case 'progress':
      _finalizeThinking();  // 进度更新，结束 thought 顶部面板
      hideTyping();
      removeThinkingHint();
      hasToolCalls = true;
      ensureStepsContainer();
      if (data.step !== undefined && _toolTimers[data.step]) {
        const elapsed = Date.now() - _toolTimers[data.step];
        const durEl = document.getElementById('tool-dur-' + data.step);
        if (durEl) durEl.textContent = elapsed > 1000 ? `${(elapsed/1000).toFixed(0)}s` : `${elapsed}ms`;
      }
      smartScroll(container);
      break;

    case 'llm_thinking':
      removeGeneratingBadge();
      break;

    case 'ping':
      // ponytail: 后端每 2s 心跳（无运行中工具时发），目的是「避免连接因空闲断开」。
      // 原前端无此分支，ping 被完全忽略 → 思考真空期零反馈。这里接住它：
      // 标记流活跃 + 确保可见指示器存在 + 刷新「已等待」计时，让用户明确知道连接还活着。
      markStreamActivity();
      if (!document.getElementById('loading-bar').classList.contains('show')) {
        showTyping();
      }
      break;

    case 'llm_response':
      _finalizeReasoning();  // 模型思考阶段结束，定稿「思考过程」面板
      _finalizeThinking();   // LLM 响应开始，结束 thought 顶部面板
      break;

    case 'llm_retry': {
      // 模型重试（空闲超时 / 限流 429），已自动重试（仅重发 LLM 调用，不重跑工具）
      markStreamActivity();
      // ponytail: 重试是临时状态，用 toast 提示，不污染消息区
      if (typeof showToast === 'function') {
        if (data.reason === 'rate_limit') {
          // 429 限流：明确告知「等待 N 秒后自动重试」，让用户知道是在排队等待而非卡死
          showToast(t('rateLimitRetryingNote', {
            attempt: data.attempt, max: (data.max || 1), wait: (data.wait || 30),
          }), 'warn', 8000);
        } else {
          showToast(t('modelRetryingNote', { attempt: data.attempt, max: (data.max || 1) }), '');
        }
      }
      break;
    }

    case 'model_switch':
      addMessage(t('modelSwitched', { reason: data.reason || (currentLanguage === 'en' ? 'request' : '请求'), model: data.model }), 'system');
      break;

    case 'user_message_injected': {
      // 实时干预：用户发的打断消息已在 LLM 边界注入（后端 stream_run 回放此事件）。
      // 补渲染为正式 user 消息，让对话流完整。
      markStreamActivity();
      if (data && data.content) {
        addUserMessage(data.content, []);
      }
      break;
    }

    case 'ask_user_modal': {
      // agent 调用 ask_user 向用户征询意见：弹出问卷弹窗等待用户选择/输入。
      // 用户提交后 POST 到 resolve 端点，后端唤醒阻塞中的 ask_user 工具继续执行。
      markStreamActivity();
      // 历史回放（刷新页面/切到已结束会话）不重弹：该 ask 早已 resolve 或早已超时，
      // 此时后端没有 pending ask，弹出来提交必然失败（resolved:false）。陈旧弹窗只此一处来源。
      // 但「切回仍在跑的会话」的重建回放（_isReconstructing）要正常弹——那是真的在等用户回答。
      if (_isReplaying && !_isReconstructing) break;
      if (data && data.ask_id) {
        showAskUserModal(data);
      }
      break;
    }

    case 'inbox_next_turn': {
      // 排队（turn 模式）的消息，在本轮结束后由 driver 归还给前端，按序发起新一轮。
      const msgs = data && Array.isArray(data.messages) ? data.messages : [];
      if (msgs.length) {
        const rt2 = sessionRuntimes.get(visibleSessionKey);
        rt2.interventionQueue = rt2.interventionQueue || [];
        for (const m of msgs) rt2.interventionQueue.push(m);
        // 该事件紧跟在 done 之后、[DONE] 之前；send() 的 finally 会 drain interventionQueue 发起新一轮
      }
      break;
    }
    
    case 'token':
    case 'token_delta':  // 回放端合并的批量 token（刷新恢复用，与逐字 token 同逻辑）
      // 重放历史时跳过；但「切回后台会话」的重建回放需要渲染 token（_isReconstructing 放开）
      if (_isReplaying && !_isReconstructing) break;
      _finalizeReasoning();  // 答案开始输出 → 推理模型思考阶段结束
      _finalizeThinking();   // 答案开始输出 → thought 顶部面板结束
      hideTyping();
      removeThinkingHint();
      removeGeneratingBadge();
      // 逐字流式渲染最终答案（复用 currentBotMsgEl 机制，done 时会迁移到 agent-final-output）
      if (!currentBotMsgEl) {
        currentBotMsgEl = document.createElement('div');
        if (hasToolCalls && currentStepsEl) {
          // 本次请求已出现过工具调用 → 中间轮次的正文大概率是推理，
          // 不再乐观流入最终输出区（避免「先进答案区→thought 归位→闪烁」），
          // 改为以思考样式流入步骤区；若本轮实为最终答案，done 事件会
          // 复用该节点、改为 agent-final-output 并迁回答案区（见 done 的兜底迁移）。
          currentBotMsgEl.className = 'thought-block streaming-final';
          currentStepsEl.appendChild(currentBotMsgEl);
        } else {
          // 简单问答（尚无工具调用）：流式期间临时气泡挂在第二段 body，
          // 不提前暴露第三段「最终回答」标题栏；done 时统一迁移进 _answerBodyEl。
          currentBotMsgEl.className = 'msg bot streaming-final';
          if (currentStepsEl) {
            currentStepsEl.appendChild(currentBotMsgEl);
          } else if (_answerBodyEl) {
            _answerBodyEl.appendChild(currentBotMsgEl);
          } else {
            container.appendChild(currentBotMsgEl);
          }
        }
      }
      currentFinalContent += data.content;
      currentBotMsgEl.innerHTML = renderMarkdown(currentFinalContent);
      smartScroll(container);
      break;

    case 'error':
      _finalizeThinking();  // 出错，结束 thought 顶部面板
      hideTyping();
      removeThinkingHint();
      removeGeneratingBadge();
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

      // SSE 输出完成后，隐藏整个「工具执行」段。
      // 但简单问答（无工具调用）出错时，已流出的临时答案气泡挂在第二段 body，
      // 隐藏会让用户已看到的部分内容消失，故仅在确有工具调用时隐藏。
      if (hasToolCalls) {
        var responseCardForError = getResponseCard();
        if (responseCardForError) {
          const segToolForError = responseCardForError.querySelector('.seg-tool');
          if (segToolForError) segToolForError.style.display = 'none';
        }
      }
      break;

    case 'todo':
      _finalizeThinking();  // todo 清单更新，结束 thought 顶部面板
      hideTyping();
      removeThinkingHint();
      removeGeneratingBadge();
      hasToolCalls = true;
      // todo 规划也算工作开始：露出第二段标题栏
      var _cardTodo = getResponseCard();
      if (_cardTodo) {
        var _segToolTodo = _cardTodo.querySelector('.seg-tool');
        if (_segToolTodo) _segToolTodo.classList.remove('pending-header');
      }
      if (data.todo_list) {
        renderTodoPanel(data.todo_list, true);
      }
      break;

    case 'done':
      _finalizeThinking();  // 任务完成，结束 thought 顶部面板
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

      // 冻结最终耗时到 header（防止定时器被清后值丢失）
      var responseCard = getResponseCard();
      var finalElapsed = _agentStartTime ? (Date.now() - _agentStartTime) : 0;
      if (finalElapsed > 0 && responseCard) {
        var fvalEl = responseCard.querySelector('.agent-time-val');
        if (fvalEl) fvalEl.textContent = formatElapsed(finalElapsed);
      }

      // ── 渲染最终输出到 agent-final-output 区域 ──
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
        // ponytail: 流式最终输出附上复制按钮（仅在还没有时挂一次）
        if (!currentBotMsgEl.querySelector('.msg-copy-btn')) attachCopyButton(currentBotMsgEl);
        attachFeedbackBar(currentBotMsgEl);
        finalOutputEl = currentBotMsgEl;
      } else if (!_isReplaying) {
        finalOutputEl = document.createElement('div');
        finalOutputEl.className = 'agent-final-output';
        finalOutputEl.innerHTML = finalHtml;
        attachCopyButton(finalOutputEl);
        attachFeedbackBar(finalOutputEl);
        if (_answerBodyEl) {
          _answerBodyEl.appendChild(finalOutputEl);
        } else if (responseCard) {
          responseCard.appendChild(finalOutputEl);
        } else {
          container.appendChild(finalOutputEl);
        }
      }
      currentFinalContent = finalContent;

      // 隐藏第二行（执行中状态）
      if (_currentActiveLine) _currentActiveLine.style.display = 'none';

      // 重置工具图片缓存
      _lastToolImageHtml = null;

      // ── 兜底：确保最终答案在正确位置（优先第三段 body）──
      if (finalOutputEl && _answerBodyEl && finalOutputEl.parentElement !== _answerBodyEl) {
        _answerBodyEl.appendChild(finalOutputEl);
      } else if (finalOutputEl && responseCard && finalOutputEl.parentElement !== responseCard) {
        responseCard.appendChild(finalOutputEl);
      }

      // 复制按钮已在上方（currentBotMsgEl 复用路径 1859 / 新建路径 1865）挂过，此处无需重复

      // 第一段「工作耗时」折叠区填充汇总（共 N 步 · 耗时 X）
      if (responseCard) {
        var sumEl = responseCard.querySelector('.seg-time-summary');
        if (sumEl) {
          var stepsTxt = totalSteps > 0 ? ('共 ' + totalSteps + ' 步') : '无工具调用';
          sumEl.textContent = stepsTxt + ' · 耗时 ' + formatElapsed(finalElapsed);
        }
      }

      // 收尾：把「第二段·当前执行」里残留的最后一个 step 提升进「第一段·完整历史」
      _promoteCurrentToHistory();

      // SSE 输出完成后，隐藏整个「工具执行」段
      if (responseCard) {
        const segTool = responseCard.querySelector('.seg-tool');
        if (segTool) segTool.style.display = 'none';
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

    default:
      // 插件自定义 SSE 事件（E 注入点）：后端在 on_message/on_tool_end 广播后
      // 把插件经 host.push_sse 排队的事件 yield 成 {type:'plugin_event', event, payload}，
      // 这里按 event 名 dispatch 给插件用 PluginUI.onEvent 注册的前端回调。
      if (data && data.type === 'plugin_event' && typeof window.PluginUI !== 'undefined') {
        try {
          window.PluginUI.dispatchEvent(data.event, data.payload);
        } catch (e) {
          console.error('[plugin-event] dispatch failed:', e);
        }
      }
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
    'compress_context': '🧹',
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
    'compress_context': t('compressingContext') || '压缩上下文',
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

// 子代理胶囊内「已完成工具调用」折叠区展开/收起
function toggleDoneTools(header) {
  const box = header.closest('.sa-tools-done');
  if (box) box.classList.toggle('open');
}
