/* browser-live.js — 聊天区右下角「可拖拽浮动窗」：浏览器实时视图 + 接管
 * 方案 A：轮询 /api/browser/live 展示 agent 当前浏览器页面（截图 + URL + DOM 摘要）
 * 方案 B：给出「在真实浏览器打开」入口
 *
 * 交互模型（重构自旧的侧边栏面板方案）：
 *   - 浮动窗默认贴在聊天区右下角，拖拽 header 可移动（位置随会话保持）
 *   - agent 有浏览器操作时（SSE tool_start 命中 browser_*）→ 自动弹出/置顶（showBrowserFloat）
 *   - 可点 × 关闭；关闭后下一次浏览器操作仍会再自动弹出
 *   - 轮询仅在浮动窗可见时进行，隐藏即停
 * 依赖: util.js(escapeHtml), i18n.js(t)
 */
(function () {
  let browserTimer = null;   // 轮询定时器
  let browserPollMs = 3000;  // 轮询间隔
  let floatVisible = false;  // 浮动窗是否可见（显隐 + 轮询共用）

  var _floatEl = null;   // 缓存浮动窗根元素（延迟绑定）
  var _contentEl = null; // 内容区 .bl-float-body
  var _dragging = false; // 拖拽中（暂停轮询渲染干扰）

  // ---------- 浮动窗 DOM 定位（懒绑定，避免脚本先于 DOM 解析） ----------
  function _els() {
    if (!_floatEl) _floatEl = document.getElementById('browser-float');
    if (!_contentEl) _contentEl = document.getElementById('browser-live');
    return _floatEl;
  }

  // ---------- 自动弹出（由 streaming.js 在 browser_* tool_start 时调用） ----------
  window.showBrowserFloat = function () {
    // 浮动窗已搬进右侧工作台：agent 有 browser_* 操作时自动展开工作台，
    // 已展开则不打断用户当前所在的 tab。
    if (typeof Workbench !== 'undefined' && Workbench.onBrowserActivity) Workbench.onBrowserActivity();
    const el = _els();
    if (!el) return;
    el.classList.remove('bl-hidden');
    floatVisible = true;
    // 自动弹出时立即刷新一次 + 启动轮询；已可见则只确保轮询在跑
    browserLiveRefresh(true);
    startBrowserPolling();
  };

  window.hideBrowserFloat = function () {
    const el = _els();
    if (el) el.classList.add('bl-hidden');
    floatVisible = false;
    stopBrowserPolling();
  };

  // ---------- 轮询（仅可见时） ----------
  function startBrowserPolling() {
    stopBrowserPolling();
    if (!floatVisible) return;
    browserTimer = setInterval(function () {
      if (floatVisible && !_dragging) browserLiveRefresh(true);
    }, browserPollMs);
  }
  function stopBrowserPolling() {
    if (browserTimer) { clearInterval(browserTimer); browserTimer = null; }
  }

  // ---------- 拉取实时视图（渲染进浮动窗内容区） ----------
  window.browserLiveRefresh = async function (silent) {
    const box = document.getElementById('browser-live');
    if (!box) return;
    if (!silent) {
      box.innerHTML = '<div class="browser-live-empty">加载中…</div>';
    } else if (box.dataset.loading === '1') {
      return; // 防止轮询重入
    }
    box.dataset.loading = '1';
    try {
      const res = await fetch('/api/browser/live');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      renderBrowserLive(box, data);
    } catch (e) {
      box.innerHTML = '<div class="browser-live-empty browser-live-err">' +
        escapeHtml('加载失败：' + String(e && e.message || e)) + '</div>';
    } finally {
      delete box.dataset.loading;
    }
  };

  function renderBrowserLive(box, data) {
    const avail = !!data.available;
    if (!avail) {
      box.innerHTML = '<div class="browser-live-empty">' +
        escapeHtml(data.note || '当前无浏览器页面') + '</div>';
      return;
    }
    let html = '';
    if (data.screenshot) {
      html += '<div class="browser-live-img-wrap"><img class="browser-live-img" src="' +
        data.screenshot + '" alt="浏览器实时画面"></div>';
    }
    html += '<div class="browser-live-meta">';
    if (data.title) html += '<div class="bl-title">' + escapeHtml(data.title) + '</div>';
    if (data.url) html += '<div class="bl-url" title="' + escapeHtml(data.url) + '">' + escapeHtml(data.url) + '</div>';
    if (data.backend) html += '<div class="bl-backend">后端：' + escapeHtml(data.backend) + '</div>';
    html += '</div>';
    html += '<div class="browser-live-actions">';
    if (data.url) {
      html += '<button class="bl-btn" onclick="browserTakeoverOpen(this)">在真实浏览器打开</button>';
    }
    html += '<button class="bl-btn bl-btn-ghost" onclick="browserLiveRefresh()">刷新</button>';
    html += '</div>';
    if (data.dom) {
      html += '<div class="browser-live-dom"><div class="bl-dom-label">页面结构</div>' +
        '<pre class="bl-dom">' + escapeHtml(data.dom) + '</pre></div>';
    }
    box.innerHTML = html;
  }

  window.browserTakeoverOpen = function (btn) {
    const url = btn && btn.closest && btn.closest('.bl-float-body')
      ? (btn.closest('.bl-float-body').querySelector('.bl-url') || {}).textContent || ''
      : '';
    if (!url) return;
    window.open(url, '_blank');
  };

  // ---------- 拖拽：header 拖动移动浮动窗 ----------
  function initDrag() {
    const el = _els();
    if (!el) return;
    const header = el.querySelector('.bl-float-header');
    if (!header || header.dataset.dragBound) return;
    header.dataset.dragBound = '1';
    header.addEventListener('pointerdown', function (e) {
      // 按钮（× 关闭）上的按下不触发拖拽
      if (e.target.closest && e.target.closest('.bl-float-close')) return;
      e.preventDefault();
      const parent = el.parentElement || document.body;
      const pRect = parent.getBoundingClientRect();
      const eRect = el.getBoundingClientRect();
      const offX = e.clientX - eRect.left;
      const offY = e.clientY - eRect.top;
      _dragging = true;
      el.classList.add('bl-dragging');

      function onMove(ev) {
        let nx = ev.clientX - offX;
        let ny = ev.clientY - offY;
        // 约束在父容器内
        const maxX = pRect.width - eRect.width;
        const maxY = pRect.height - eRect.height;
        nx = Math.max(0, Math.min(nx, Math.max(0, maxX)));
        ny = Math.max(0, Math.min(ny, Math.max(0, maxY)));
        el.style.right = 'auto';
        el.style.bottom = 'auto';
        el.style.left = (nx - pRect.left) + 'px';
        el.style.top = (ny - pRect.top) + 'px';
      }
      function onUp() {
        _dragging = false;
        el.classList.remove('bl-dragging');
        document.removeEventListener('pointermove', onMove);
        document.removeEventListener('pointerup', onUp);
      }
      document.addEventListener('pointermove', onMove);
      document.addEventListener('pointerup', onUp);
    });
  }

  // DOM 就绪后初始化（兼容脚本先于/后于 HTML 解析的两种加载时序）
  function _boot() {
    initDrag();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _boot);
  } else {
    _boot();
  }
})();
