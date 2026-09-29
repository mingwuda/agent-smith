/* chat-tabs.js — 聊天区多页签（DOM 常驻）
 * 依赖: state.js（先加载）, util.js(escapeHtml), i18n.js(t)
 *
 * 设计（DOM 常驻）：
 *   每个打开的页签对应一个独立的 .msg-panel 消息容器，常驻在 #main 内、按激活态显示/隐藏。
 *   关键技巧：当前激活页签的容器始终携带 id="messages"。
 *   这样整个 streaming.js / sessions.js / messaging.js 里所有 document.getElementById('messages')
 *   的写入（包括流式渲染、历史加载、滚动）都自动落在「当前激活页签」上，无需改动那些热路径。
 *   页签切换 = 切换哪个容器携带 id="messages" + display 显隐，内容与滚动位置天然保留。
 */
(function () {
  const TABS_CONTAINER_ID = 'chat-tabs';
  const PANEL_CLASS = 'msg-panel';
  // 当前打开的页签（保序）。{ key, title }
  let tabs = [];
  // 每个页签的滚动位置缓存：key -> scrollTop
  const scrollCache = {};
  // 正在重建实时画面的页签（防重入）：key -> true
  const _resuming = {};

  function safeId(key) {
    return (key || '').replace(/[^a-zA-Z0-9_-]/g, '_') || 'tab';
  }

  // 按 data-key 查找页签面板（激活面板持有 id="messages"，不能仅按 id 定位非激活面板）
  function panelEl(key) {
    return Array.prototype.slice.call(document.querySelectorAll('.msg-panel')).find(function (p) {
      return p.dataset.key === key;
    }) || null;
  }

  function tabsEl() {
    return document.getElementById(TABS_CONTAINER_ID);
  }

  function containerEl() {
    return document.getElementById('chat-tabs-container');
  }

  // —— 页签栏渲染 ——
  function renderTabBar() {
    const el = tabsEl();
    if (!el) return;
    el.innerHTML = tabs.map(function (tb) {
      const active = tb.key === activeKey();
      return (
        '<div class="chat-tab ' + (active ? 'active' : '') + '" data-key="' + escapeHtml(tb.key) + '"' +
        ' onclick="window.ChatTabs && ChatTabs.activate(\'' + tb.key.replace(/'/g, "\\'") + '\')">' +
        '<span class="chat-tab-title" title="' + escapeHtml(tb.title) + '">' + escapeHtml(tb.title) + '</span>' +
        '<button class="chat-tab-close" onclick="event.stopPropagation();window.ChatTabs && ChatTabs.close(\'' + tb.key.replace(/'/g, "\\'") + '\')" title="关闭">✕</button>' +
        '</div>'
      );
    }).join('');
    const c = containerEl();
    if (c) c.style.display = tabs.length ? '' : 'none';
  }

  // —— 激活指定页签（只切显隐与 id，不重新加载）——
  // opts.bootstrap=true 表示调用方（open()）刚为本 key 建好新面板、紧接着还会走
  // switchSession 的完整加载路径；此时不要在这里触发实时画面重建，否则会重复加载/重复回放。
  function activate(key, opts) {
    const panel = panelEl(key);
    if (!panel) return;
    document.querySelectorAll('#' + TABS_CONTAINER_ID + ' .chat-tab').forEach(function (t) {
      t.classList.toggle('active', t.dataset.key === key);
    });
    document.querySelectorAll('.' + PANEL_CLASS).forEach(function (p) {
      const isActive = p.dataset.key === key;
      if (isActive) {
        p.id = 'messages';
        p.style.display = '';
      } else {
        // 缓存非激活页签的滚动位置
        if (p.id === 'messages') scrollCache[p.dataset.key] = p.scrollTop;
        p.removeAttribute('id');
        p.style.display = 'none';
      }
    });
    panel.style.display = '';
    panel.id = 'messages';
    // 恢复该页签滚动位置
    if (scrollCache[key] != null) panel.scrollTop = scrollCache[key];
    _setActiveKey(key);
    // ponytail: 同步侧边栏会话选中高亮（.session-item / .psession-item）。
    // 点击页签栏或点击「已在页签中」的侧边栏会话，都只走 activate() 而不经过
    // switchSession() 末尾的高亮代码（后者在 tabAlreadyOpen 早退分支会被跳过），
    // 因此必须在这里一并同步，否则侧边栏选中态不会跟随切换。
    document.querySelectorAll('.session-item, .psession-item').forEach(function (el) {
      el.classList.toggle('active', el.dataset.key === key);
    });
    // 同步全局「当前可见会话」，保证 streaming 的 live 只对激活页签生效
    if (window.setVisibleSessionKey) setVisibleSessionKey(key);
    // 同步 currentSessionId / source，保证本轮后续 fetch 走对会话。
    // 注意：state.js 里 currentSessionId 是顶层 let（不挂 window），
    // 这里必须按共享全局作用域名赋值，不能写 window.currentSessionId（那样永远 undefined、同步无效）。
    // 用 typeof 守卫规避加载顺序（currentSessionSource 定义于 sessions.js，晚于本文件加载，但 call 时均已就绪）。
    const s = splitKey(key);
    if (s && typeof currentSessionId !== 'undefined') {
      currentSessionId = s.id;
      if (typeof currentSessionSource !== 'undefined') currentSessionSource = s.source;
      if (typeof threadId !== 'undefined') threadId = s.id;
    }
    // 重建悬浮「滚动到底部」按钮绑定（只绑到当前 #messages）
    if (window.initScrollToBottomBtn) initScrollToBottomBtn();
    if (window.updateRunIndicators) updateRunIndicators();
    if (window.syncStreamingActive) syncStreamingActive();

    // ── 切回「仍在后台流式运行」的会话：必须恢复 live 并重建实时画面 ──
    // 此前只调了 setVisibleSessionKey，而它只把**旧**会话置 live=false、不会点亮新会话
    // （live=true 全仓仅 3 处：streaming.js 的 send、sessions.js 的 reconstruct/resume）。
    // 于是切回时 rt.live 仍为 false → streaming.js 的 `if (rt.live)` 渲染分支不成立，
    // 事件只推进 rt.events 不渲染 → 画面永久冻结在切走那一帧；本轮结束后再点回
    // 仍走同一路径，导致永远看不到完整回答（刷新页面才能恢复）。
    // bootstrap 场景由 switchSession 的完整路径负责（见函数头注释）。
    if (!(opts && opts.bootstrap) && window.sessionRuntimes && sessionRuntimes.has(key)) {
      const rt = sessionRuntimes.get(key);
      if (rt && rt.status === 'streaming' && !rt.live && !_resuming[key]
          && typeof reconstructStreamingSession === 'function') {
        // 防重入：同一会话的重建会先清空容器再回放，连点页签并发跑两次会互相踩踏
        _resuming[key] = true;
        reconstructStreamingSession(rt)
          .catch(function (e) { console.warn('[chat-tabs] 重建实时画面失败:', e); })
          .then(function () { delete _resuming[key]; });
      }
    }
  }

  // 打开（或激活）一个页签。title 用于页签名。
  // 仅负责「页签/容器管理」，不做消息加载 —— 由调用方（switchSession）负责把内容加载进 #messages。
  // 返回: true = 本次新打开；false = 已在页签中（仅激活，内容/滚动保留）。
  function open(key, title) {
    // 已打开 -> 直接激活（不重载，保留内容/滚动/流式画面）
    if (panelEl(key)) {
      activate(key);
      return false;
    }
    // 新开：把当前激活容器切到一个新 panel，并把原 #messages 转成普通 panel
    const holder = document.getElementById('messages');
    const newPanel = document.createElement('div');
    newPanel.className = PANEL_CLASS;
    newPanel.dataset.key = key;
    newPanel.id = 'messages';
    // 仅在「首个页签」bootstrap 时迁移原 #messages 的内容（首次从静态容器切到页签体系）。
    // 已有页签时新开 tab 是全新空面板，由调用方 loadSessionMessages 填充 —— 否则会把
    // 上一个激活 tab 的内容「劫持」进新 tab，导致上一 tab 丢失内容。
    if (tabs.length === 0) {
      while (holder && holder.firstChild) {
        if (holder.firstChild && holder.firstChild.id === '__session_loading_hint__') {
          holder.removeChild(holder.firstChild);
          continue;
        }
        newPanel.appendChild(holder.firstChild);
      }
    }
    // 原 #messages 变成普通 panel（保留，避免旧代码引用失效），隐藏
    if (holder) {
      holder.removeAttribute('id');
      holder.style.display = 'none';
      if (tabs.length === 0) holder.innerHTML = '';
    }
    // 插入到 tab 容器之后
    const ref = containerEl();
    ref.parentNode.insertBefore(newPanel, ref.nextSibling);
    // 记录页签
    tabs.push({ key: key, title: title || key });
    _setActiveKey(key);
    renderTabBar();
    // 激活新页签（让新 panel 持有 id=messages）。
    // bootstrap=true：紧接着由调用方（switchSession）走完整加载路径，
    // 这里不要触发实时画面重建，否则会重复 loadSessionMessages + 重复回放。
    activate(key, { bootstrap: true });
    return true;
  }

  // 关闭页签
  function close(key) {
    const idx = tabs.findIndex(function (t) { return t.key === key; });
    if (idx < 0) return;
    const panel = panelEl(key);
    // 关闭的是流式运行中的会话：保留后台继续跑，仅从界面移除
    if (window.sessionRuntimes && sessionRuntimes.has(key)) {
      const rt = sessionRuntimes.get(key);
      if (rt) rt.live = false;
    }
    tabs.splice(idx, 1);
    delete scrollCache[key];
    if (panel) panel.remove();
    // 如果关闭的是激活页签，切到左侧相邻页签（或清空）
    if (key === activeKey()) {
      if (tabs.length > 0) {
        const next = tabs[Math.max(0, idx - 1)] || tabs[0];
        activate(next.key);
      } else {
        _setActiveKey(null);
        // 关掉最后一个页签后必须补一个可写的消息容器：
        // 页签体系建立时原 #messages 的 id 已被摘掉（见 open()），若不补，
        // document.getElementById('messages') 会返回 null，之后 addMessage /
        // beginRoundRender 取到 null → appendChild 抛 TypeError（且调用点在 try 之外），
        // 表现为「发送按钮卡红、消息发不出去」。
        let holder = document.getElementById('messages');
        if (!holder) {
          holder = document.createElement('div');
          const ref = containerEl();
          if (ref && ref.parentNode) ref.parentNode.insertBefore(holder, ref.nextSibling);
          else document.body.appendChild(holder);
        }
        holder.id = 'messages';
        holder.style.display = '';
        holder.innerHTML = '';
        // 同时清掉可见会话：该会话已无可见容器，留着会让 streaming.js 收尾时
        // 误判「它还是可见会话」而不回收 runtime（visibleSessionKey !== rt.key 才 delete）。
        if (window.setVisibleSessionKey) setVisibleSessionKey(null);
      }
    }
    renderTabBar();
  }

  // 供 session 切换复用：确认页签打开。
  function ensureTab(key, title) {
    return open(key, title);
  }

  // —— 工具函数 ——
  function activeKey() {
    return (window._activeTabKey) || null;
  }
  function _setActiveKey(k) { window._activeTabKey = k; }
  // 返回当前激活页签的消息容器（即当前持有 id=messages 的元素）
  function activeEl() {
    return document.getElementById('messages');
  }
  function splitKey(key) {
    if (!key) return null;
    const i = key.lastIndexOf('_');
    if (i < 0) return { id: key, source: 'web' };
    return {
      id: key.slice(0, i),
      source: key.slice(i + 1) || 'web'
    };
  }

  // 内部再用（真实时序里 streaming 通过 #messages 拿到激活容器；这里仅同步滚动 cache 便于切换恢复）
  function reloadSession(key) {
    const s = splitKey(key);
    if (s && typeof loadSessionMessages === 'function') {
      return loadSessionMessages(s.id, s.source, { limit: 20, offset: -20 });
    }
    return Promise.resolve();
  }

  window.ChatTabs = {
    open: open,
    activate: activate,
    close: close,
    ensureTab: ensureTab,
    isOpen: function (key) { return !!panelEl(key); },
    activeKey: activeKey,
    activeEl: activeEl,
    // 页签 key = sessionId + '_' + source。切分必须用 lastIndexOf（sessionId 自身可能含下划线，
    // 如 cron 生成的 `cron_sess_xxx`），否则会切错 id。对外暴露供 sessions.js 等处复用同一约定。
    splitKey: splitKey,
    all: function () { return tabs.slice(); }
  };
})();