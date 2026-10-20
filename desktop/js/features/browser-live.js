/* browser-live.js — 项目 ⋯ 菜单「浏览器」入口：实时视图 + 接管
 * 方案 A：轮询 /api/browser/live 展示 agent 当前浏览器页面（截图 + URL + DOM 摘要）
 * 方案 B：给出「在真实浏览器打开」/ DevTools 接管 入口
 * 依赖: util.js(escapeHtml, escAttr), i18n.js(t), workspace.js(projectsCache, loadProjects)
 */
(function () {
  let browserTimer = null;   // 轮询定时器
  let browserPollMs = 3000;  // 轮询间隔
  let browserVisible = false;

  // ---------- 面板开合（与定时任务同款：替换项目列表） ----------
  window.openBrowserPanel = async function (projectId) {
    const panel = document.getElementById('browser-panel');
    if (!panel) return;
    if (typeof loadProjects === 'function') {
      try { await loadProjects(); } catch (e) { /* ignore */ }
    }
    const proj = (typeof projectsCache !== 'undefined' ? projectsCache : []) || []
      .find(p => String(p.id) === String(projectId));
    const title = document.getElementById('browser-panel-title');
    if (title) title.textContent = '🖥 ' + (proj && proj.name ? proj.name : '浏览器');

    const listEl = document.getElementById('project-list');
    if (listEl) listEl.style.display = 'none';
    panel.style.display = 'flex';
    document.querySelectorAll('.sidebar-accordion').forEach(a => a.style.display = 'none');
    browserVisible = true;
    browserLiveRefresh();
    startBrowserPolling();
  };

  window.exitBrowserPanel = function () {
    const panel = document.getElementById('browser-panel');
    const listEl = document.getElementById('project-list');
    if (panel) panel.style.display = 'none';
    if (listEl) listEl.style.display = '';
    document.querySelectorAll('.sidebar-accordion').forEach(a => a.style.display = '');
    browserVisible = false;
    stopBrowserPolling();
  };

  function startBrowserPolling() {
    stopBrowserPolling();
    browserTimer = setInterval(function () {
      if (browserVisible) browserLiveRefresh(true);
    }, browserPollMs);
  }
  function stopBrowserPolling() {
    if (browserTimer) { clearInterval(browserTimer); browserTimer = null; }
  }

  // ---------- 拉取实时视图 ----------
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
    // 截图
    if (data.screenshot) {
      html += '<div class="browser-live-img-wrap"><img class="browser-live-img" src="' +
        data.screenshot + '" alt="浏览器实时画面"></div>';
    }
    // 元信息
    html += '<div class="browser-live-meta">';
    if (data.title) html += '<div class="bl-title">' + escapeHtml(data.title) + '</div>';
    if (data.url) html += '<div class="bl-url" title="' + escapeHtml(data.url) + '">' + escapeHtml(data.url) + '</div>';
    if (data.backend) html += '<div class="bl-backend">后端：' + escapeHtml(data.backend) + '</div>';
    html += '</div>';
    // 接管按钮（方案 B）
    html += '<div class="browser-live-actions">';
    if (data.url) {
      html += '<button class="bl-btn" onclick="browserTakeoverOpen(this)">在真实浏览器打开</button>';
    }
    html += '<button class="bl-btn bl-btn-ghost" onclick="browserLiveRefresh()">刷新</button>';
    html += '</div>';
    // DOM 摘要
    if (data.dom) {
      html += '<div class="browser-live-dom"><div class="bl-dom-label">页面结构</div>' +
        '<pre class="bl-dom">' + escapeHtml(data.dom) + '</pre></div>';
    }
    box.innerHTML = html;
  }

  window.browserTakeoverOpen = function (btn) {
    const url = btn && btn.closest && btn.closest('.browser-live')
      ? (btn.closest('.browser-live').querySelector('.bl-url') || {}).textContent || ''
      : '';
    if (!url) return;
    const httpUrl = /^https?:\/\//i.test(url) ? url : url;
    window.open(httpUrl, '_blank');
  };

  // 面板隐藏时清理定时器（若通过其他路径隐藏侧边栏）
  document.addEventListener('click', function (e) {
    const willHide = e.target.closest('[data-toggle-sidebar]');
    // 简单兜底：折叠侧边栏时不强轮询（交给 exitBrowserPanel 主动停）
  });
})();