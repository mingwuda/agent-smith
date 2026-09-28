#!/usr/bin/env node
/**
 * check_stop_cancel.js — 验证 stopCurrentRun 的 /cancel 目标使用 rt.sessionId（而非 currentSessionId）。
 *
 * 回归场景：多页签下用户发送会话 B 后切到会话 A，再停 B。若 currentSessionId 漂移为 A，
 * 旧的实现会 cancel 到 /sessions/A/cancel（错），导致 B 的后台 driver 停不掉 → 「停止无效」。
 * 修复后用 rt.sessionId(B) 作为 cancel 目标。
 *
 * 运行: node tests/check_stop_cancel.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'desktop', 'js', 'features', 'streaming.js'), 'utf8');

// 提取 stopCurrentRun 函数体（支持跳过字符串/模板字符串，避免 ${} 里的花括号干扰闭合计数）。
function extractFunc(source, name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(([^)]*)\\)\\s*\\{');
  const m = source.match(re);
  if (!m) throw new Error('function ' + name + ' not found');
  const start = m.index + m[0].length - 1; // 指向 '{'
  let depth = 1, i = start + 1;
  while (i < source.length && depth > 0) {
    const ch = source[i];
    if (ch === '{' || ch === '}') {
      depth += (ch === '{' ? 1 : -1);
      i++;
      continue;
    }
    if (ch === "'" || ch === '"') {
      // 跳过普通字符串
      const q = ch;
      i++;
      while (i < source.length && source[i] !== q) { if (source[i] === '\\') i++; i++; }
      i++;
      continue;
    }
    if (ch === '`') {
      // 跳过模板字符串（整段，含 ${...}）
      i++;
      while (i < source.length) {
        if (source[i] === '\\') { i += 2; continue; }
        if (source[i] === '`') { i++; break; }
        i++;
      }
      continue;
    }
    i++;
  }
  return { params: m[1], body: source.slice(start, i) };
}

let failures = 0;
function assert(cond, name) {
  if (cond) console.log('  ✓ ' + name);
  else { failures++; console.error('  ✗ FAIL: ' + name); }
}

// capture cancel/fetch 调用目标
let cancelTargets = [];
let abortCount = 0;
let toastMsgs = [];   // showToast 调用记录
let msgAdds = [];      // addMessage 调用记录（验证取消失败不再污染消息区）

function makeRuntime(key, sessionId) {
  return {
    sessionId: sessionId,
    source: 'web',
    key,
    status: 'streaming',
    controller: { abort: () => { abortCount++; } },
    events: [],
    live: true,
    _resumeStop: undefined,
  };
}

// fetch 响应：默认 cancel 成功；用例4 改为返回「无运行中任务」detail
let fetchResult = { ok: true };

// 桩全局环境
const sandbox = {
  console,
  URLSearchParams: Object,
  fetch: (url, opts) => { cancelTargets.push({ url, method: opts.method }); const snap = fetchResult; return Promise.resolve({ json: () => Promise.resolve(snap) }); },
  sessionRuntimes: new Map(),
  visibleSessionKey: 'B_web',
  currentSessionId: 'A',   // 故意漂移（用户切到 A 后未留意 currentSessionId 已是 A）—— 旧实现在此会 cancel 到 A
  threadId: 'A',
  userStoppedCurrentRun: false,
  sendBtn: { innerHTML: '', disabled: false },
  showToast: (m, type) => { toastMsgs.push({ m, type }); },
  addMessage: (m, type) => { msgAdds.push({ m, type }); },
  t: (k) => k,
};

const { params, body } = extractFunc(src, 'stopCurrentRun');
const fn = vm.runInNewContext('(function(' + params + ') ' + body + ')', sandbox);

// 用例1：currentSessionId 漂移，rt.sessionId 是正确目标 B
function runCase1() {
  cancelTargets = [];
  abortCount = 0;
  sandbox.sessionRuntimes.set('B_web', makeRuntime('B_web', 'B'));
  sandbox.visibleSessionKey = 'B_web';
  sandbox.currentSessionId = 'A';   // 漂移到 A
  sandbox.threadId = 'A';
  fn();
}
runCase1();
assert(abortCount === 1, 'abort 被调用（前端连接断开）');
assert(cancelTargets.length === 1, '向后端发 cancel 请求');
assert(cancelTargets[0] && cancelTargets[0].url === '/sessions/B/cancel' && cancelTargets[0].method === 'POST',
  'cancel 目标是 rt.sessionId=B，而非漂移的 currentSessionId=A  (url=' + (cancelTargets[0] && cancelTargets[0].url) + ')');

// 用例2：rt 无 sessionId 时回退到 currentSessionId（兼容旧 rt）
function runCase2() {
  cancelTargets = [];
  sandbox.sessionRuntimes.set('B_web', makeRuntime('B_web', undefined));
  sandbox.visibleSessionKey = 'B_web';
  sandbox.currentSessionId = 'C';
  fn();
}
runCase2();
assert(cancelTargets.length === 1 && cancelTargets[0].url === '/sessions/C/cancel', '无 rt.sessionId 时回退到 currentSessionId (url=' + (cancelTargets[0] && cancelTargets[0].url) + ')');

// 用例3：resume 接管路径走 _resumeStop，不发 cancel（前置早退）
function runCase3() {
  cancelTargets = [];
  abortCount = 0;
  let resumeStopped = false;
  const rt = makeRuntime('B_web', 'B');
  rt._resumeStop = () => { resumeStopped = true; };
  sandbox.sessionRuntimes.set('B_web', rt);
  sandbox.visibleSessionKey = 'B_web';
  fn();
  assert(resumeStopped === true, 'resume 接管会话走 _resumeStop');
  assert(cancelTargets.length === 0 && abortCount === 0, 'resume 路径不 abort、不发 cancel');
}
runCase3();

// 用例4（异步）：取消失败（后端报「无运行中任务」）→ 用 toast 轻提示，不再往消息区插入第二条系统消息
(async () => {
  toastMsgs = [];
  msgAdds = [];
  cancelTargets = [];
  abortCount = 0;
  fetchResult = { ok: false, running: false, detail: '没有正在运行的后台任务（可能已完成或本会话无运行中请求）' };
  sandbox.sessionRuntimes.set('B_web', makeRuntime('B_web', 'B'));
  sandbox.visibleSessionKey = 'B_web';
  sandbox.currentSessionId = 'B';
  fn();
  // 等 fetch .then 链落地（含 json 解析层）
  await new Promise((r) => setTimeout(r, 20));
  assert(toastMsgs.length === 1, '取消失败用 showToast 提示 (type=' + (toastMsgs[0] && toastMsgs[0].type) + ')');
  assert(toastMsgs[0] && toastMsgs[0].type === 'warn', 'toast 类型为 warn');
  assert(toastMsgs[0] && toastMsgs[0].m === fetchResult.detail, 'toast 内容为后端 detail');
  assert(msgAdds.length === 1, '只保留第一条「已请求终止」系统消息，不再插入第二条 (count=' + msgAdds.length + ')');
  assert(!(msgAdds[0] && msgAdds[0].m === fetchResult.detail), '"没有正在运行的后台任务"不再以系统消息形式插入消息区');

  if (failures === 0) { console.log('\nALL PASS ✔ (stopCurrentRun cancel 目标正确 + 取消失败走 toast)'); process.exit(0); }
  else { console.error('\nFAILURES: ' + failures); process.exit(1); }
})();