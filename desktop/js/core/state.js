/* state.js — 全局 DOM 引用与跨功能域共享的可变状态
   最先加载（零依赖），其余 feature 文件直接读写这些全局变量。 */

// ── 顶层 DOM 引用 ──
const messages = document.getElementById('messages');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const attachmentPreview = document.getElementById('attachment-preview');
const statusDot = document.getElementById('status-dot');
const statusText = document.getElementById('status-text');
const quickProviderSelect = document.getElementById('quick-provider-select');
const settingsBtn = document.getElementById('settings-btn');
const providerCard = document.getElementById('provider-card');
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
