/* workbench.js — 右侧工作台 dock：收缩开关、tab 切换、宽度拖拽、更改 tab、浏览器桥接
   参考 agnes-harness workbench/dock.tsx 的布局状态机，但保持本项目的全局脚本风格。
   依赖: util.js(escapeHtml)、workspace.js(renderDiffHtml / projectsCache)、
         browser-live.js(showBrowserFloat / browserLiveRefresh)、file-preview.js
*/
(function () {
  var KEY = 'moss.workbench.v1';
  var DEFAULTS = { open: false, width: 720, tab: 'files', treeWidth: 240, maximized: false };
  var state = Object.assign({}, DEFAULTS);
  var dock, toggleBtn;

  function load() {
    try {
      var raw = localStorage.getItem(KEY);
      if (raw) state = Object.assign({}, DEFAULTS, JSON.parse(raw));
    } catch (e) { /* 存储不可用时用默认值 */ }
  }
  function save() {
    try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {}
  }
  function clamp(n, lo, hi) { return Math.max(lo, Math.min(hi, n)); }
  // escapeHtml 不转义双引号，拼进 HTML 属性时补一层
  function attr(s) { return escapeHtml(String(s == null ? '' : s)).replace(/"/g, '&quot;'); }

  function apply() {
    dock.hidden = !state.open;
    document.body.classList.toggle('wb-open', state.open);
    toggleBtn.classList.toggle('active', state.open);
    toggleBtn.setAttribute('aria-expanded', state.open ? 'true' : 'false');
    document.documentElement.style.setProperty('--wb-width', state.maximized ? '60vw' : state.width + 'px');
    dock.classList.toggle('wb-maximized', state.maximized);
    document.getElementById('wb-files-left').style.width = state.treeWidth + 'px';
    if (state.open && state.tab === 'terminal' && window.WorkbenchTerminal) WorkbenchTerminal.fit();
  }

  function setBadge(name, count) {
    var el = document.getElementById('wb-count-' + name);
    if (!el) return;
    el.textContent = count > 99 ? '99+' : String(count);
    el.hidden = !count;
  }

  var Workbench = {
    get activeTab() { return state.tab; },
    isOpen: function () { return state.open; },

    open: function (tab) {
      state.open = true;
      if (tab) state.tab = tab;
      apply();                 // 先让面板可见，懒初始化的 xterm 才能量到尺寸
      setTab(state.tab);       // 每次展开都触发当前 tab 的 onShow（首次要建文件树）
      save();
      Workbench.refreshCounts();
    },
    close: function () {
      state.open = false;
      apply();
      save();
      // 浏览器轮询只在面板可见时有意义，收起时停掉
      if (typeof hideBrowserFloat === 'function') hideBrowserFloat();
    },
    toggle: function () { state.open ? Workbench.close() : Workbench.open(); },
    toggleMaximize: function () {
      state.maximized = !state.maximized;
      apply();
      save();
    },
    setTab: function (id) { setTab(id); },

    /* 左侧文件浏览器 / diff 仍调用 openFilePreview()，把结果统一落到 dock 的文件 tab */
    onFilePreview: function () {
      if (!state.open) Workbench.open('files');
      else if (state.tab !== 'files') setTab('files');
    },
    /* agent 执行 browser_* 工具时自动展示工作台（不打断用户当前已打开的其它 tab） */
    onBrowserActivity: function () {
      if (!state.open) Workbench.open('browser');
    },

    refreshCounts: function () {
      var pid = window.WorkbenchFiles ? WorkbenchFiles.projectId() : '';
      var qs = pid ? '?project_id=' + encodeURIComponent(pid) : '';
      fetch('/files/changes' + qs)
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { if (d) setBadge('changes', d.total_changes || 0); })
        .catch(function () {});
    },
    setTerminalCount: function (n) { setBadge('terminal', n); },
    projectId: function () { return window.WorkbenchFiles ? WorkbenchFiles.projectId() : ''; },
  };
  window.Workbench = Workbench;

  function setTab(id) {
    state.tab = id;
    dock.querySelectorAll('.wb-tab').forEach(function (b) {
      b.classList.toggle('active', b.dataset.tab === id);
    });
    dock.querySelectorAll('.wb-panel').forEach(function (p) {
      p.classList.toggle('active', p.id === 'wb-panel-' + id);
    });
    save();
    if (id === 'files' && window.WorkbenchFiles) WorkbenchFiles.onShow();
    if (id === 'changes' && window.WorkbenchChanges) WorkbenchChanges.load();
    if (id === 'terminal' && window.WorkbenchTerminal) WorkbenchTerminal.ensure();
    if (id === 'browser' && typeof showBrowserFloat === 'function') showBrowserFloat();
  }

  /* ---------- 更改 tab：复用 /files/changes + /files/diff + workspace.js 的 renderDiffHtml ---------- */
  var changesLoadedPid = null;
  var WorkbenchChanges = {
    load: function (force) {
      var pid = Workbench.projectId();
      if (!force && changesLoadedPid === pid) return;
      changesLoadedPid = pid;
      var list = document.getElementById('wb-changes-list');
      list.innerHTML = '<div class="wb-empty">' + escapeHtml(t('wbLoading')) + '</div>';
      var qs = pid ? '?project_id=' + encodeURIComponent(pid) : '';
      fetch('/files/changes' + qs).then(function (r) {
        return r.json().then(function (d) {
          if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
          return d;
        });
      }).then(function (data) {
        var items = data.changes || [];
        setBadge('changes', data.total_changes || 0);
        if (!items.length) { list.innerHTML = '<div class="wb-empty">' + escapeHtml(t('wbNoChanges')) + '</div>'; return; }
        list.innerHTML = items.map(function (c) {
          return '<div class="wb-change-item" data-path="' + attr(c.path) + '" onclick="WorkbenchChanges.showDiff(this.dataset.path, this)">' +
            '<span class="wb-change-status">' + escapeHtml(c.status || '').slice(0, 1).toUpperCase() + '</span>' +
            '<span class="wb-change-path" title="' + attr(c.path) + '">' + escapeHtml(c.path) + '</span></div>';
        }).join('');
      }).catch(function (e) {
        list.innerHTML = '<div class="wb-empty">' + escapeHtml(e.message || '加载失败') + '</div>';
      });
    },
    showDiff: function (path, el) {
      document.querySelectorAll('.wb-change-item').forEach(function (n) { n.classList.remove('active'); });
      if (el) el.classList.add('active');
      var box = document.getElementById('wb-changes-diff');
      box.innerHTML = '<div class="wb-empty">' + escapeHtml(t('wbLoading')) + '</div>';
      var qs = new URLSearchParams({ file_path: path });
      var pid = Workbench.projectId();
      if (pid) qs.set('project_id', pid);
      fetch('/files/diff?' + qs.toString()).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      }).then(function (data) {
        box.classList.add('fpp-diff-wrap');
        box.innerHTML = '';
        if (typeof renderDiffHtml === 'function') renderDiffHtml(box, data.diff_text || '(无差异)');
        else box.textContent = data.diff_text || '(无差异)';
        box.scrollTop = 0;
      }).catch(function (e) {
        box.innerHTML = '<div class="wb-empty">' + escapeHtml(e.message || '获取 diff 失败') + '</div>';
      });
    },
    invalidate: function () { changesLoadedPid = null; },
  };
  window.WorkbenchChanges = WorkbenchChanges;

  /* ---------- 拖拽：dock 宽度 + 文件树宽度 ---------- */
  function bindResize(handle, getStart, onMove) {
    var dragging = false, startX = 0, startW = 0;
    handle.addEventListener('pointerdown', function (e) {
      dragging = true;
      startX = e.clientX;
      startW = getStart();
      handle.classList.add('dragging');
      try { handle.setPointerCapture(e.pointerId); } catch (err) {}
      e.preventDefault();
    });
    handle.addEventListener('pointermove', function (e) {
      if (!dragging) return;
      onMove(startW, e.clientX - startX);
    });
    function end() { dragging = false; handle.classList.remove('dragging'); save(); }
    handle.addEventListener('pointerup', end);
    handle.addEventListener('pointercancel', end);
  }

  function init() {
    dock = document.getElementById('workbench-dock');
    toggleBtn = document.getElementById('workbench-toggle');
    if (!dock || !toggleBtn) return;

    // 把两个已有面板搬进 dock：id 保持不变，预览/编辑/diff/浏览器轮询代码零改动
    var previewHost = document.getElementById('wb-preview-host');
    var fpp = document.getElementById('file-preview-panel');
    if (previewHost && fpp) previewHost.appendChild(fpp);
    var browserHost = document.getElementById('wb-panel-browser');
    var bf = document.getElementById('browser-float');
    if (browserHost && bf) {
      browserHost.appendChild(bf);
      var closeBtn = bf.querySelector('.bl-float-close');
      if (closeBtn) closeBtn.onclick = function () { Workbench.close(); };
    }

    load();
    apply();
    // tab 状态恢复：不触发懒加载，等用户真正切过去时再初始化
    dock.querySelectorAll('.wb-tab').forEach(function (b) {
      b.classList.toggle('active', b.dataset.tab === state.tab);
    });
    dock.querySelectorAll('.wb-panel').forEach(function (p) {
      p.classList.toggle('active', p.id === 'wb-panel-' + state.tab);
    });
    if (state.open) {
      setTab(state.tab);
      Workbench.refreshCounts();
    }

    bindResize(document.getElementById('wb-resize'),
      function () { return dock.getBoundingClientRect().width; },
      function (startW, dx) {
        state.width = clamp(startW - dx, 300, Math.max(320, window.innerWidth - 420));
        state.maximized = false;
        apply();
      });
    bindResize(document.getElementById('wb-files-split'),
      function () { return document.getElementById('wb-files-left').getBoundingClientRect().width; },
      function (startW, dx) {
        state.treeWidth = clamp(startW + dx, 150, dock.getBoundingClientRect().width - 260);
        apply();
      });

    window.addEventListener('resize', function () { if (state.open) apply(); });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
