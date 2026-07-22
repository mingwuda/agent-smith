/* state.js — 全局 DOM 引用与跨功能域共享的可变状态
   最先加载（零依赖），其余 feature 文件直接读写这些全局变量。 */

// ── 顶层 DOM 引用 ──
const messages = document.getElementById('messages');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const attachmentPreview = document.getElementById('attachment-preview');
const statusDot = document.getElementById('status-dot');
const statusText = document.getElementById('status-text');
const settingsBtn = document.getElementById('settings-btn');
const languageSelect = document.getElementById('language-select');

// ── 通用状态变量 ──
let threadId = 'default';
let isLoading = false;
let _pythonProgressSource = null;
let _toolTimers = {};  // { step: timestamp } 用于计算工具耗时
let sessionsCache = [];
let currentSessionId = null;
let settingsData = null;
let currentUser = null;
let isAdmin = false;
let pendingAttachments = [];
let currentAbortController = null;
let userStoppedCurrentRun = false;

// ── 流式状态变量（send 与 handleStreamEvent 之间的隐式通道）──
let currentBotMsgEl = null;       // 当前 bot 消息 DOM
let currentStepsEl = null;        // 当前步骤容器 DOM
let currentFinalContent = '';     // 累积的最终回复
let streamingActive = false;
let totalSteps = 0;               // 已完成的步骤数
let hasToolCalls = false;         // 本轮是否有工具调用
let generatingBadgeEl = null;     // 生成中徽章 DOM
let streamIdleTimer = null;        // 流式响应空闲提示定时器
let lastStreamEventAt = 0;         // 最近一次收到后端事件的时间

// ── 输入区消息历史（上下方向键导航）──
let _msgHistory = [];              // 当前会话的用户消息列表
let _msgHistoryIndex = -1;         // -1 = 当前输入（空/新消息）

// ── 多会话并发运行时（A、B 等会话可同时执行任务，互不阻塞）──
// 每个会话一份 runtime：status(idle|streaming|done|error)、各自的 AbortController、累积的 SSE 事件、是否当前可见渲染目标 live。
const sessionRuntimes = new Map(); // key(sessionId_source) -> runtime 对象
let visibleSessionKey = null;      // 当前在 #messages 中显示的会话 key
let _isReconstructing = false;     // 切回后台运行中的会话时回放缓冲事件（放开 token 渲染、但禁止开启实时子连接）

function getOrCreateRuntime(sessionId, source) {
  const key = (sessionId || '') + '_' + (source || 'web');
  let rt = sessionRuntimes.get(key);
  if (!rt) {
    rt = {
      sessionId: sessionId,
      source: source || 'web',
      key,
      status: 'idle',       // idle | streaming | done | error
      controller: null,     // 该会话自己的 AbortController
      events: [],           // 本轮累积的 SSE 事件（用于切回时重建实时画面）
      live: false,          // 是否当前可见渲染目标（true 才把事件渲染进 #messages）
      _timerInterval: null, // 该会话自己的耗时定时器
      _agentStartTime: 0,
    };
    sessionRuntimes.set(key, rt);
  }
  return rt;
}

// 维护「当前可见会话」标记：切走时把旧会话的 live 关掉（后台继续累积事件，只是不再渲染到可见区）
function setVisibleSessionKey(key) {
  if (visibleSessionKey && visibleSessionKey !== key && sessionRuntimes.has(visibleSessionKey)) {
    sessionRuntimes.get(visibleSessionKey).live = false;
  }
  visibleSessionKey = key;
}
