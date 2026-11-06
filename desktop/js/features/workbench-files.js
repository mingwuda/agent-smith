/* workbench-files.js — 文件 tab：懒加载目录树 + 多文件标签 + 源码/预览 + @引用
   参考 agnes-harness files-panel/files-tree/files-preview 的「已展开路径集合」模型，
   但数据直接用本项目已有的 /files/browse、/files/read，预览容器复用 #file-preview-panel。
   ponytail: 筛选只过滤已加载的条目，不为搜索发起全仓库扫描。
*/
(function () {
  var MAX_TABS = 12;
  var state = {
    projectId: '', root: '', dirs: {}, expanded: {},
    tabs: [], active: '', filter: '', initialized: false,
  };
  var nodes = [];          // 每次渲染重建：data-i -> {path, isDir}，避免把路径拼进 HTML 属性
  var previewUrl = null;   // HTML/SVG 预览的 blob URL，切换时回收

  function el(id) { return document.getElementById(id); }
  // escapeHtml 不转义双引号，拼进 HTML 属性时补一层
  function attr(s) { return escapeHtml(String(s == null ? '' : s)).replace(/"/g, '&quot;'); }
  function extOf(name) { return (name || '').split('.').pop().toLowerCase(); }
  function join(base, name) { return (base || '').replace(/\/$/, '') + '/' + name; }
  function current() { return state.tabs.find(function (t) { return t.path === state.active; }); }
  function toast(msg) { if (typeof showToast === 'function') showToast(msg); }

  /* ---------- 项目下拉 ---------- */
  function globalProjects() {
    try { return (typeof projectsCache !== 'undefined' && Array.isArray(projectsCache)) ? projectsCache : []; }
    catch (e) { return []; }
  }
  function defaultProjectId() {
    try {
      if (typeof activeProjectId !== 'undefined' && activeProjectId) return String(activeProjectId);
      if (typeof currentProjectId !== 'undefined' && currentProjectId) return String(currentProjectId);
    } catch (e) {}
    return '';
  }

  async function fillProjectSelect() {
    var list = globalProjects();
    if (!list.length) {
      try {
        var r = await fetch('/projects');
        if (r.ok) list = (await r.json()).projects || [];
      } catch (e) {}
    }
    var sel = el('wb-project-select');
    if (!sel) return;
    var opts = '<option value="">' + escapeHtml(t('wbDefaultWorkspace')) + '</option>' + list.map(function (p) {
      return '<option value="' + attr(p.id) + '">' + escapeHtml(p.name || p.directory_path || p.id) + '</option>';
    }).join('');
    sel.innerHTML = opts;
    if (!state.projectId) state.projectId = defaultProjectId();
    sel.value = state.projectId;
    if (sel.value !== state.projectId) { state.projectId = ''; sel.value = ''; }
  }

  /* ---------- 目录树 ---------- */
  async function fetchDir(path) {
    var qs = new URLSearchParams();
    if (path) qs.set('path', path);
    if (state.projectId) qs.set('project_id', state.projectId);
    var r = await fetch('/files/browse?' + qs.toString());
    if (!r.ok) {
      var d = await r.json().catch(function () { return {}; });
      throw new Error(d.detail || ('HTTP ' + r.status));
    }
    var data = await r.json();
    state.dirs[path || data.path] = data.entries || [];
    return data;
  }

  async function refresh() {
    var box = el('wb-tree');
    if (!box) return;
    if (!state.root) {
      try {
        var rootData = await fetchDir('');
        state.root = rootData.path;
        state.expanded[state.root] = true;
      } catch (e) {
        box.innerHTML = '<div class="wb-empty">' + escapeHtml(e.message) + '</div>';
        return;
      }
    }
    var paths = Object.keys(state.expanded).filter(function (p) { return state.expanded[p]; });
    try {
      var values = await Promise.all(paths.map(function (p) { return fetchDir(p).catch(function () { return null; }); }));
      // 根目录的 path 可能与请求 key 不同，统一以返回的 data.path 为准
      values.forEach(function (d) { if (d) state.dirs[d.path] = d.entries || []; });
    } catch (e) { /* 单个目录失败由 .catch 吞掉 */ }
    renderTree();
  }

  function renderTree() {
    var box = el('wb-tree');
    if (!box) return;
    nodes = [];
    var needle = state.filter.trim().toLowerCase();
    var html = needle ? renderFlat(needle) : renderDir(state.root, 0);
    box.innerHTML = html || '<div class="wb-empty">' + escapeHtml(needle ? t('wbNoMatches') : t('wbFolderEmpty')) + '</div>';
  }

  function nodeHtml(item, depth, chevron, icon) {
    var i = nodes.push(item) - 1;
    var selected = item.path === state.active ? ' selected' : '';
    return '<div class="wb-node' + selected + '" data-i="' + i + '" style="padding-left:' + (8 + depth * 14) + 'px" title="' + attr(item.path) + '">' +
      '<span class="wb-chevron' + (chevron === 'open' ? ' open' : '') + '">' + (item.isDir ? '▶' : '') + '</span>' +
      '<span>' + icon + '</span>' +
      '<span class="wb-name">' + escapeHtml(item.name) + '</span>' +
      '<span class="wb-at" data-at="1" title="引用到对话">@</span></div>';
  }

  function renderDir(path, depth) {
    var entries = state.dirs[path] || [];
    var out = '';
    entries.forEach(function (e) {
      var child = join(path, e.name);
      var isDir = e.type === 'directory';
      var open = isDir && !!state.expanded[child];
      out += nodeHtml({ path: child, name: e.name, isDir: isDir }, depth,
        open ? 'open' : '', isDir ? (open ? '📂' : '📁') : (typeof fileIcon === 'function' ? fileIcon(e.name) : '📄'));
      if (isDir && open) out += renderDir(child, depth + 1);
    });
    return out;
  }

  function renderFlat(needle) {
    var out = '';
    Object.keys(state.dirs).forEach(function (dir) {
      (state.dirs[dir] || []).forEach(function (e) {
        if (e.type === 'directory') return;
        var p = join(dir, e.name);
        if (p.toLowerCase().indexOf(needle) < 0) return;
        out += nodeHtml({ path: p, name: e.name, isDir: false }, 0, '',
          typeof fileIcon === 'function' ? fileIcon(e.name) : '📄');
      });
    });
    return out;
  }

  function onTreeClick(e) {
    var row = e.target.closest('.wb-node');
    if (!row) return;
    var item = nodes[Number(row.dataset.i)];
    if (!item) return;
    if (e.target.closest('.wb-at')) { mention(item.path); return; }
    if (item.isDir) {
      if (state.expanded[item.path]) {
        delete state.expanded[item.path];
        renderTree();
      } else {
        state.expanded[item.path] = true;
        fetchDir(item.path).then(function (d) {
          state.dirs[d.path] = d.entries || [];
          renderTree();
        }).catch(function (err) {
          delete state.expanded[item.path];
          renderTree();
          toast(err.message || '读取目录失败');
        });
      }
    } else {
      openFile(item.path, item.name);
    }
  }

  /* ---------- 打开文件 / 标签 ---------- */
  async function openFile(path, name) {
    var existing = state.tabs.find(function (t) { return t.path === path; });
    if (existing) { activate(path); return; }
    if (state.tabs.length >= MAX_TABS) { toast(t('wbMaxTabs', { n: MAX_TABS })); return; }
    var qs = new URLSearchParams({ path: path });
    if (state.projectId) qs.set('project_id', state.projectId);
    try {
      var r = await fetch('/files/read?' + qs.toString());
      var d = await r.json();
      if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
      state.tabs.push({
        path: path,
        name: d.name || name,
        content: d.content || '',
        meta: (d.path || path) + '  ·  ' + (d.lines || 0) + ' 行  ·  ' +
              (typeof formatSize === 'function' ? formatSize(d.size || 0) : (d.size || 0) + ' B'),
        mode: 'source',
      });
      state.active = path;
      renderTabs();
      renderActive();
      renderTree();
    } catch (err) {
      toast(err.message || '读取文件失败');
    }
  }

  function activate(path) {
    state.active = path;
    renderTabs();
    renderActive();
    renderTree();
  }

  function closeTab(path) {
    var idx = state.tabs.findIndex(function (t) { return t.path === path; });
    if (idx < 0) return;
    state.tabs.splice(idx, 1);
    if (state.active === path) {
      var next = state.tabs[Math.min(idx, state.tabs.length - 1)];
      state.active = next ? next.path : '';
    }
    renderTabs();
    renderActive();
    renderTree();
  }

  function renderTabs() {
    var box = el('wb-file-tabs');
    if (!box) return;
    box.innerHTML = state.tabs.map(function (t, i) {
      return '<div class="wb-file-tab' + (t.path === state.active ? ' active' : '') + '" data-i="' + i + '">' +
        '<span class="wb-tab-name">' + escapeHtml(t.name) + '</span>' +
        '<span class="wb-tab-x" data-close="1" title="关闭">×</span></div>';
    }).join('');
    box.onclick = function (e) {
      var tab = e.target.closest('.wb-file-tab');
      if (!tab) return;
      var t = state.tabs[Number(tab.dataset.i)];
      if (!t) return;
      if (e.target.closest('[data-close]')) closeTab(t.path);
      else activate(t.path);
    };
  }

  /* ---------- 渲染：源码 / 预览 ---------- */
  function fillGutter(text) {
    var g = el('fpp-gutter');
    if (!g) return;
    var n = (text || '').split('\n').length;
    var s = '';
    for (var i = 1; i <= n; i++) s += i + (i < n ? '\n' : '');
    g.textContent = s;
  }

  function revokePreviewUrl() {
    if (previewUrl) { URL.revokeObjectURL(previewUrl); previewUrl = null; }
  }

  function setFppChrome(item) {
    var icon = el('fpp-icon'), title = el('fpp-title'), meta = el('fpp-meta');
    if (icon) icon.textContent = typeof fileIcon === 'function' ? fileIcon(item.name) : '📄';
    if (title) title.textContent = item.name;
    if (meta) meta.textContent = item.meta;
    var edit = document.querySelector('#file-preview-panel .fpp-edit-btn');
    if (edit) edit.remove();
    var toolbar = el('fpp-edit-toolbar');
    if (toolbar) toolbar.style.display = 'none';
    var actions = document.querySelector('#file-preview-panel .fpp-actions');
    if (actions) actions.style.display = '';
    var editorWrap = el('fpp-editor-wrap');
    if (editorWrap) editorWrap.style.display = 'none';
  }

  function hideViews() {
    ['fpp-code-wrap', 'fpp-md', 'fpp-diff-wrap', 'fpp-editor-wrap'].forEach(function (id) {
      var n = el(id);
      if (n) n.style.display = 'none';
    });
  }

  function renderSource(item) {
    revokePreviewUrl();
    hideViews();
    var wrap = el('fpp-code-wrap');
    if (wrap) wrap.style.display = '';
    var code = el('fpp-code');
    if (code) {
      code.textContent = item.content;
      var ext = extOf(item.name);
      code.className = typeof hljsLang === 'function' ? ('language-' + hljsLang(ext)) : '';
      code.removeAttribute('data-highlighted');
      code.classList.remove('hljs');
      if (typeof hljs !== 'undefined') {
        try { hljs.highlightElement(code); } catch (e) {}
      }
    }
    fillGutter(item.content);
    var body = el('fpp-body');
    if (body) body.scrollTop = 0;
  }

  function renderPreview(item) {
    hideViews();
    var md = el('fpp-md');
    if (!md) return;
    var ext = extOf(item.name);
    revokePreviewUrl();
    md.style.display = '';
    md.style.padding = '';
    md.style.height = '';
    md.style.position = '';

    if (['md', 'markdown', 'mdx'].indexOf(ext) >= 0) {
      md.innerHTML = typeof renderMarkdown === 'function' ? renderMarkdown(item.content) : '<pre>' + escapeHtml(item.content) + '</pre>';
      if (typeof hljs !== 'undefined') {
        md.querySelectorAll('pre code').forEach(function (b) { try { hljs.highlightElement(b); } catch (e) {} });
      }
      return;
    }
    if (ext === 'html' || ext === 'htm' || ext === 'svg') {
      var mime = ext === 'svg' ? 'image/svg+xml' : 'text/html';
      previewUrl = URL.createObjectURL(new Blob([item.content], { type: mime }));
      md.style.padding = '0';
      md.style.height = '100%';
      md.style.position = 'relative';
      // sandbox 不含 allow-same-origin：预览页是 opaque origin，拿不到父页权限
      var inner = ext === 'svg'
        ? '<img src="' + previewUrl + '" alt="SVG 预览">'
        : '<iframe src="' + previewUrl + '" sandbox="allow-scripts" referrerpolicy="no-referrer"></iframe>';
      md.innerHTML = '<div class="fpp-outer-preview">' + inner + '</div>';
      return;
    }
    md.innerHTML = '<div class="wb-preview-empty">' + escapeHtml(t('wbNoRenderPreview')) + '</div>';
  }

  function renderActive() {
    var meta = el('wb-file-meta');
    var item = current();
    if (meta) meta.textContent = item ? item.meta : '';
    document.querySelectorAll('#wb-mode-switch button').forEach(function (b) {
      b.classList.toggle('active', !!item && b.dataset.mode === item.mode);
    });
    if (!item) {
      hideViews();
      revokePreviewUrl();
      var host = el('wb-preview-host');
      var empty = host && host.querySelector('.wb-preview-empty');
      if (!empty && host) {
        empty = document.createElement('div');
        empty.className = 'wb-preview-empty';
        empty.textContent = t('wbSelectFile');
        host.appendChild(empty);
      }
      return;
    }
    var host2 = el('wb-preview-host');
    if (host2) {
      var old = host2.querySelector('.wb-preview-empty');
      if (old) old.remove();
    }
    setFppChrome(item);
    if (item.mode === 'preview') renderPreview(item);
    else renderSource(item);
  }

  function setMode(mode) {
    var item = current();
    if (!item) return;
    item.mode = mode;
    document.querySelectorAll('#wb-mode-switch button').forEach(function (b) {
      b.classList.toggle('active', b.dataset.mode === mode);
    });
    if (mode === 'preview') renderPreview(item);
    else renderSource(item);
  }

  /* ---------- @引用：只是把路径插入输入框，不发送、不改权限 ---------- */
  function mention(path) {
    var input = el('input');
    if (!input) return;
    var text = '@' + path + ' ';
    var s = input.selectionStart == null ? input.value.length : input.selectionStart;
    var e = input.selectionEnd == null ? s : input.selectionEnd;
    input.value = input.value.slice(0, s) + text + input.value.slice(e);
    input.focus();
    try { input.setSelectionRange(s + text.length, s + text.length); } catch (err) {}
    toast(t('wbMentioned'));
  }

  function setProject(id) {
    state.projectId = id || '';
    state.root = '';
    state.dirs = {};
    state.expanded = {};
    state.tabs = [];
    state.active = '';
    renderTabs();
    renderActive();
    refresh();
    if (window.WorkbenchChanges) WorkbenchChanges.invalidate();
    if (window.Workbench) Workbench.refreshCounts();
    // 终端已连接时按新项目目录重开，避免 cwd 与当前项目不一致
    if (window.WorkbenchTerminal) WorkbenchTerminal.restart();
  }

  function init() {
    if (state.initialized) return;
    state.initialized = true;
    var tree = el('wb-tree');
    if (tree) tree.addEventListener('click', onTreeClick);
    var filter = el('wb-tree-filter');
    if (filter) filter.addEventListener('input', function (e) {
      state.filter = e.target.value || '';
      renderTree();
    });
    fillProjectSelect().then(refresh);
    renderTabs();
    renderActive();
  }

  window.WorkbenchFiles = {
    onShow: init,
    refresh: function () { init(); refresh(); },
    filter: function (v) { state.filter = v || ''; renderTree(); },
    setProject: setProject,
    setMode: setMode,
    mentionActive: function () { var it = current(); if (it) mention(it.path); },
    projectId: function () { return state.projectId; },
    openPath: function (path, pid) {
      init();
      if (pid && pid !== state.projectId) setProject(pid);
      openFile(path, path.split('/').pop());
    },
  };
})();
