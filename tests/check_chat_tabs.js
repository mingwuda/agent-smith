#!/usr/bin/env node
/**
 * chat-tabs.js 自检（无 jsdom，使用最小 DOM 桩）。
 * 覆盖 chat-tabs.js 的页签管理核心：open(新开/激活)、close、activeKey、activeEl、多个页签切换。
 *
 * 运行: node tests/check_chat_tabs.js
 * 先读入 chat-tabs.js 源码，再用一个极简 element/container 桩满足其依赖的 DOM API。
 */

const fs = require('fs');
const path = require('path');

// ---------- 极简 DOM 桩 ----------
let createdSequential = 0;
function makeEl(tag) {
  const el = {
    tagName: tag, id: '', className: '', dataset: {},
    style: {}, children: [], parentNode: null,
    __scrollBtnBound: false,
    innerHTML: '',
    scrollTop: 0, scrollHeight: 100, clientHeight: 50,
    setAttribute(k, v) { this[k] = v; },
    removeAttribute(k) { this[k] = undefined; },
    appendChild(c) { c.parentNode = this; this.children.push(c); },
    insertBefore(child, refNode) {
      child.parentNode = this;
      const i = refNode ? this.children.indexOf(refNode) : this.children.length;
      if (i < 0) this.children.push(child); else this.children.splice(i, 0, child);
    },
    remove() { if (this.parentNode) { const p = this.parentNode; p.children = p.children.filter(c => c !== this); } },
    addEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    classList: {
      _set: new Set(),
      add() {}, remove() {}, toggle(klass, force) { const on = force !== undefined ? !!force : !this._set.has(klass); if (on) this._set.add(klass); else this._set.delete(klass); },
      contains(klass) { return this._set.has(klass); },
    },
  };
  // 注册到全局元素表，供 querySelectorAll('.msg-panel') 使用
  if (typeof allEls !== 'undefined' && allEls && allEls.indexOf(el) < 0) allEls.push(el);
  return el;
}

const byId = new Map(); // id -> element (登记初始容器)
function ensureEl(id) {
  if (!byId.has(id)) { const el = makeEl('div'); el.id = id; byId.set(id, el); }
  return byId.get(id);
}

// 全局元素注册表（模拟 DOM 树），供 querySelectorAll('.msg-panel') 按类/属性过滤
const allEls = [];
global.__makeEl = makeEl; // 由 makeEl 注册自己
makeEl.tagRegistry = allEls;

// 真实 DOM 的 getElementById：按「当前持有该 id 的元素」返回（模块会把 id="messages" 在各面板间移动）
function getEl(id) {
  if (typeof allEls !== 'undefined') {
    const cur = allEls.find(function (el) { return el.id === id; });
    if (cur) return cur;
  }
  return byId.get(id);
}

global.document = {
  getElementById: getEl,
  createElement: (tag) => makeEl(tag || 'div'),
  querySelectorAll: (sel) => {
    if (typeof sel === 'string' && sel.indexOf('.msg-panel') >= 0) {
      return allEls.filter(function (el) { return el.className === 'msg-panel'; });
    }
    return [];
  },
};
// 页签面板注册表（模拟 .msg-panel 集合，供 querySelectorAll('.msg-panel') 使用）
const trackers = { panels: [] };
global.window = { _activeTabKey: null, ChatTabs: null };
// chat-tabs.js 依赖的全局工具
global.escapeHtml = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
global.t = (k, params) => k;
// activate() 依赖的全局函数桩
global.setVisibleSessionKey = () => {};
global.updateRunIndicators = () => {};
global.syncStreamingActive = () => {};
global.initScrollToBottomBtn = () => {};
global.sessionRuntimes = new Map();

// 预建关键容器，并搭建简单层级：#main > (chat-tabs-container, messages)
const main = ensureEl('main');
ensureEl('messages');
ensureEl('chat-tabs-container');
ensureEl('chat-tabs');
main.appendChildrenPlus = true;
(() => {
  const msgs = getEl('messages');
  const tabsC = getEl('chat-tabs-container');
  main.children = [tabsC, msgs];
  tabsC.parentNode = main;
  msgs.parentNode = main;
  tabsC.nextSibling = msgs;
  getEl('chat-tabs').parentNode = tabsC;
})();

// ---------- 载入源码并执行 ----------
const src = fs.readFileSync(path.join(__dirname, '..', 'desktop', 'js', 'core', 'chat-tabs.js'), 'utf8');
global.window = {};
new Function('window', 'document', src + '\nreturn window.ChatTabs;')(global.window, global.document);

let failures = 0;
function assert(cond, name) {
  if (cond) console.log('  ✓ ' + name);
  else { failures++; console.error('  ✗ FAIL: ' + name); }
}

const gd = global.window;
const T = gd.ChatTabs;
function panelByKey(key) {
  // 找到携带 id=messages 或对应 data-key 的 .msg-panel
  const act = getEl('messages');
  if (act && act.dataset.key === key) return act;
  return allEls.find(function (el) { return (el.className === 'msg-panel') && el.dataset.key === key; }) || null;
}

// ---------- 用例 ----------
console.log('[1] 打开第一个页签（新开）');
ensureEl('messages');
const wasNew1 = T.open('sess1_web', '会话1');
assert(wasNew1 === true, 'open 第一个返回 true（新开）');
assert(T.activeKey() === 'sess1_web', 'activeKey 设为 sess1_web');
assert(getEl('messages').dataset.key === 'sess1_web', '新面板持有 id=messages');

console.log('[2] 打开第二个页签（新开），激活切换');
const wasNew2 = T.open('sess2_web', '会话2');
assert(wasNew2 === true, 'open 第二个返回 true');
assert(T.activeKey() === 'sess2_web', '当前激活为 sess2_web');
assert(getEl('messages').dataset.key === 'sess2_web', '#messages 现在指向 sess2 面板');

console.log('[3] 切回已打开页签（激活，不新建）');
// 给 sess1 面板塞一条内容，切回验证内容保留
const p1 = panelByKey('sess1_web');
if (p1) { p1.children = []; p1.children.push(makeEl('div')); }
const wasAct1 = T.open('sess1_web', '会话1');
assert(wasAct1 === false, '再次 open 返回 false（已存在，仅激活）');
assert(T.activeKey() === 'sess1_web', '切回 sess1_web 激活');
assert(getEl('messages') === p1, '#messages 指向 sess1 面板');
assert(p1.children.length === 1, 'sess1 面板内容保留（未清空）');

console.log('[4] 关闭当前激活页签，应切到相邻页签');
T.close('sess1_web');
assert(T.activeKey() === 'sess2_web', '关闭后自动切到 sess2_web');
assert(getEl('messages').dataset.key === 'sess2_web', '#messages 指向 sess2');

console.log('[5] 关闭最后一个页签');
T.close('sess2_web');
assert(T.all().length === 0, '页签全部清空');
assert(T.activeKey() === null, 'activeKey 为 null（无激活页签）');

console.log('[6] activeEl 解析');
ensureEl('messages');
T.open('sess3_web', '会话3');
assert(T.activeEl() === panelByKey('sess3_web'), 'activeEl 返回持有 #messages 的面板');

if (failures === 0) { console.log('\nALL PASS ✔ (' + '6 groups)'); process.exit(0); }
else { console.error('\nFAILURES: ' + failures); process.exit(1); }