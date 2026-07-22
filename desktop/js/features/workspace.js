/* workspace.js — 工作区 / 项目 / 会话 组织 + 侧边栏文件浏览器
   依赖: sessions.js(currentSessionId, currentSessionSource, threadId, sessionsCache,
         switchSession, deleteSession, loadSessions, addMessage),
         util.js(escapeHtml), i18n.js(t), streaming.js 无关 */

let projectsCache = [];
let activeProjectId = null;       // 当前展开的项目
let currentProjectDir = '';       // 当前文件浏览器根目录
let currentProjectId = '';        // 当前文件浏览器所属项目
let currentBrowsePath = '';       // 当前浏览路径

// ---------- 项目数据 ----------

async function loadProjects() {
  try {
    const res = await fetch('/projects');
    if (!res.ok) return;
    const data = await res.json();
    projectsCache = data.projects || [];
  } catch (e) { /* 忽略 */ }
}

// ---------- 渲染工作区（被 sessions.js 的 loadSessions 调用） ----------

async function renderWorkspace() {
  const listEl = document.getElementById('project-list');
  if (!listEl) return;

  await loadProjects();

  let html = '';

  if (!projectsCache || projectsCache.length === 0) {
    const unassigned = (sessionsCache || []).filter(s => !s.project_id);
    if (unassigned.length === 0) {
      listEl.innerHTML = '<div style="padding:16px;color:#8e8e93;font-size:13px;font-style:italic;">' +
        escapeHtml(t('noProjects') || '暂无项目，点击右上角 ＋ 新建') + '</div>';
      return;
    }
  }

  for (const p of projectsCache) {
    const expanded = (p.id === activeProjectId);
    const projSessions = (sessionsCache || []).filter(s => s.project_id === p.id);

    html += '<div class="project-item ' + (expanded ? 'expanded active' : '') + '" data-pid="' + p.id + '">';
    html += '  <div class="project-row" onclick="toggleProject(\'' + p.id + '\')">';
    html += '    <span class="proj-toggle">▶</span>';
    html += '    <span class="proj-icon">📁</span>';
    html += '    <div class="proj-main">';
    html += '      <div class="proj-name">' + escapeHtml(p.name) + '</div>';
    html += '      <div class="proj-path">' + escapeHtml(p.directory_path || (t('noDirSet') || '未设置目录')) + '</div>';
    html += '    </div>';
    html += '    <div class="proj-actions">';
    html += '      <button class="pa-btn" title="' + escapeHtml(t('viewFiles') || '查看文件') + '" onclick="event.stopPropagation(); openFileBrowser(\'' + p.id + '\')">📂</button>';
    html += '      <button class="pa-btn" title="' + escapeHtml(t('editProject') || '编辑') + '" onclick="event.stopPropagation(); editProject(\'' + p.id + '\')">✎</button>';
    html += '      <button class="pa-btn" title="' + escapeHtml(t('deleteProject') || '删除') + '" onclick="event.stopPropagation(); deleteProject(\'' + p.id + '\')">🗑</button>';
    html += '    </div>';
    html += '  </div>';

    if (expanded) {
      html += '  <div class="project-sessions">';
      if (projSessions.length === 0) {
        html += '    <div style="padding:4px 10px;font-size:11px;color:#8e8e93;">' +
          escapeHtml(t('noSessionsInProject') || '该项目下暂无会话') + '</div>';
      } else {
        projSessions.forEach(s => {
          const isActive = s.id === currentSessionId && s.source === currentSessionSource;
          html += '<div class="psession-item ' + (isActive ? 'active' : '') + '" data-key="' + _sessionKey(s) + '" ' +
            'onclick="switchSession(\'' + s.id + '\',\'' + (s.source || 'web') + '\')">';
          html += '  <span class="psi-icon">' + ((s.source === 'wechat') ? '📱' : '💬') + '</span>';
          html += '  <span class="psi-title">' + escapeHtml(s.title || t('unnamed') || '未命名') + '</span>';
          html += '  <span class="psi-meta">' + escapeHtml(t('messagesCount', { count: s.message_count || 0 })) + '</span>';
          html += '  <span class="psi-del" onclick="event.stopPropagation(); deleteSession(\'' + s.id + '\')">✕</span>';
          html += '</div>';
        });
      }
      html += '    <div class="project-new-session" onclick="newSessionInProject(\'' + p.id + '\')">＋ ' +
        escapeHtml(t('newSessionInProject') || '新建会话') + '</div>';
      html += '  </div>';
    }
    html += '</div>';
  }

  // 未归属任何项目的会话
  const unassigned = (sessionsCache || []).filter(s => !s.project_id);
  if (unassigned.length > 0) {
    html += '<div class="project-item">';
    html += '  <div class="project-row" style="cursor:default;">';
    html += '    <span class="proj-icon">💬</span>';
    html += '    <div class="proj-main"><div class="proj-name">' +
      escapeHtml((t('unassignedSessions') || '其他会话') + ' (' + unassigned.length + ')') + '</div></div>';
    html += '    <span class="project-new-session-inline" onclick="newSession()" title="' +
      escapeHtml(t('newSessionInProject') || '新建会话') + '">＋</span>';
    html += '  </div>';
    html += '  <div class="project-sessions">';
    unassigned.forEach(s => {
      const isActive = s.id === currentSessionId && s.source === currentSessionSource;
      html += '<div class="psession-item ' + (isActive ? 'active' : '') + '" data-key="' + _sessionKey(s) + '" ' +
        'onclick="switchSession(\'' + s.id + '\',\'' + (s.source || 'web') + '\')">';
      html += '  <span class="psi-icon">' + ((s.source === 'wechat') ? '📱' : '💬') + '</span>';
      html += '  <span class="psi-title">' + escapeHtml(s.title || t('unnamed') || '未命名') + '</span>';
      html += '  <span class="psi-meta">' + escapeHtml(t('messagesCount', { count: s.message_count || 0 })) + '</span>';
      html += '  <span class="psi-del" onclick="event.stopPropagation(); deleteSession(\'' + s.id + '\')">✕</span>';
      html += '</div>';
    });
    html += '  </div>';
    html += '</div>';
  }

  listEl.innerHTML = html;
}

// ---------- 项目交互 ----------

function toggleProject(id) {
  activeProjectId = (activeProjectId === id) ? null : id;
  if (typeof renderWorkspace === 'function') renderWorkspace();
}

async function newSessionInProject(projectId) {
  try {
    const res = await fetch('/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: projectId }),
    });
    if (!res.ok) return;
    const data = await res.json();
    currentSessionId = data.id;
    currentSessionSource = 'web';
    threadId = data.id;
    // 标记新会话为可见渲染目标（同时把之前可见会话的 live 关掉，使其后台继续运行不渲染）
    if (typeof setVisibleSessionKey === 'function') setVisibleSessionKey(data.id + '_web');
    // 作废任何仍在途的旧会话加载请求(防止其晚到回写上一会话内容)
    if (typeof _sessionLoadToken !== 'undefined') ++_sessionLoadToken;
    // 重置分页状态, 防止滚动监听器用旧会话的 sessionId 继续往上翻页加载旧消息
    if (typeof _sessionPageState !== 'undefined') _sessionPageState = null;
    const box = document.getElementById('messages');
    if (box) box.innerHTML = '';
    addMessage(t('newSessionReady') || '开始新对话', 'system');
    await loadSessions();
  } catch (e) { /* 忽略 */ }
}

function showNewProjectModal() {
  const name = prompt(t('projectNamePrompt') || '项目名称：');
  if (!name || !name.trim()) return;
  const dir = prompt(t('projectDirPrompt') || '项目目录路径（留空则使用默认工作区）：', '');
  if (dir === null) return; // 取消
  createProject(name.trim(), (dir || '').trim());
}

async function createProject(name, dir) {
  try {
    const res = await fetch('/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name, directory_path: dir || '' }),
    });
    if (!res.ok) { alert(t('createProjectFailed') || '创建项目失败'); return; }
    const p = await res.json();
    activeProjectId = p.id;
    await loadProjects();
    if (typeof renderWorkspace === 'function') renderWorkspace();
  } catch (e) { /* 忽略 */ }
}

async function deleteProject(id) {
  if (!confirm(t('deleteProjectConfirm') || '确定删除该项目？其下的会话不会被删除，仅解除归属。')) return;
  try {
    const res = await fetch('/projects/' + id, { method: 'DELETE' });
    if (!res.ok) return;
    if (activeProjectId === id) activeProjectId = null;
    await loadProjects();
    if (typeof renderWorkspace === 'function') renderWorkspace();
  } catch (e) { /* 忽略 */ }
}

async function editProject(id) {
  const p = projectsCache.find(x => x.id === id);
  if (!p) return;
  const name = prompt(t('renameProjectPrompt') || '项目名称：', p.name);
  if (!name || !name.trim()) return;
  const dir = prompt(t('setProjectDirPrompt') || '项目目录：', p.directory_path || '');
  if (dir === null) return;
  try {
    const res = await fetch('/projects/' + id, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name.trim(), directory_path: (dir || '').trim() }),
    });
    if (res.ok) {
      await loadProjects();
      if (typeof renderWorkspace === 'function') renderWorkspace();
    }
  } catch (e) { /* 忽略 */ }
}

function toggleWorkspaceView() {
  const sidebar = document.getElementById('sidebar');
  if (!sidebar) return;
  if (window.innerWidth <= 768) {
    sidebar.classList.remove('open');
    const ov = document.getElementById('sidebar-overlay');
    if (ov) ov.classList.remove('show');
  } else {
    sidebar.classList.toggle('collapsed');
  }
}

// ---------- 文件浏览器 ----------

async function openFileBrowser(projectId) {
  const p = projectsCache.find(x => x.id === projectId);
  currentProjectDir = (p && p.directory_path) ? p.directory_path : '';
  currentProjectId = projectId;
  _isChangesView = false;
  _resetChangesBtn();

  const listEl = document.getElementById('project-list');
  const fbEl = document.getElementById('file-browser-panel');
  if (listEl) listEl.style.display = 'none';
  // 直接通过 inline style 控制显隐，避免被 HTML 中内联的 display:none 覆盖
  if (fbEl) fbEl.style.display = 'flex';
  document.querySelectorAll('.sidebar-accordion').forEach(a => a.style.display = 'none');

  const path = currentProjectDir || '';
  await browseDirectory(path, projectId);
  // 预加载变更数量，显示角标
  prefetchChangesCount();
  // 检测未推送提交，显示推送图标
  checkUnpushedCommits();
}

function exitFileBrowser() {
  const listEl = document.getElementById('project-list');
  const fbEl = document.getElementById('file-browser-panel');
  if (listEl) listEl.style.display = '';
  // 与 openFileBrowser 对应：通过 inline style 隐藏，避免被 class 覆盖
  if (fbEl) fbEl.style.display = 'none';
  document.querySelectorAll('.sidebar-accordion').forEach(a => a.style.display = '');
  closeFilePreview();
  // 重置变更视图状态和角标
  _isChangesView = false;
  const btn = document.getElementById('fb-changes-btn');
  if (btn) btn.classList.remove('active');
  _updateChangesBadge(0);
  _resetChangesBtn();
}

function refreshFileBrowser() {
  browseDirectory(currentBrowsePath, currentProjectId);
  checkUnpushedCommits();
}

async function checkUnpushedCommits() {
  const pushBtn = document.getElementById('fb-push-btn');
  const pullBtn = document.getElementById('fb-pull-btn');
  if (!pushBtn || !pullBtn) return;
  try {
    const qs = new URLSearchParams();
    if (currentProjectId) qs.set('project_id', currentProjectId);
    const res = await fetch('/files/unpushed-count?' + qs.toString());
    if (!res.ok) {
      pushBtn.style.display = 'none';
      pullBtn.style.display = 'none';
      return;
    }
    const data = await res.json();
    const count = data.unpushed_count || 0;
    if (count > 0) {
      pushBtn.style.display = '';
      pushBtn.title = '推送 ' + count + ' 个未推送提交';
    } else if (count === -1) {
      pushBtn.style.display = '';
      pushBtn.title = '推送（未设置 upstream）';
    } else {
      pushBtn.style.display = 'none';
    }
    // 只要接口成功，说明是 git 仓库，显示 pull 按钮
    pullBtn.style.display = '';
    pullBtn.title = '拉取远程代码';
  } catch (_) {
    if (pushBtn) pushBtn.style.display = 'none';
    if (pullBtn) pullBtn.style.display = 'none';
  }
}

async function pushUnpushedCommits() {
  const btn = document.getElementById('fb-push-btn');
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  btn.textContent = '⏳';
  try {
    const res = await fetch('/files/push', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: currentProjectId || '' }),
    });
    const d = await res.json();
    if (d.success) {
      if (typeof showToast === 'function') showToast('✅ ' + (d.output || '推送成功'));
      checkUnpushedCommits();
    } else {
      alert('推送失败：\n' + (d.output || '未知错误'));
    }
  } catch (e) {
    alert('推送失败：' + (e && e.message ? e.message : e));
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '⬆️';
    }
  }
}

async function pullCommits() {
  const btn = document.getElementById('fb-pull-btn');
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  btn.textContent = '⏳';
  try {
    const res = await fetch('/files/pull', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: currentProjectId || '' }),
    });
    const d = await res.json();
    if (d.success) {
      if (typeof showToast === 'function') showToast('✅ ' + (d.output || '拉取成功'));
      // 拉取后刷新文件树和推送状态
      refreshFileBrowser();
      checkUnpushedCommits();
    } else {
      alert('拉取失败：\n' + (d.output || '未知错误'));
    }
  } catch (e) {
    alert('拉取失败：' + (e && e.message ? e.message : e));
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '⬇️';
    }
  }
}

async function browseDirectory(path, projectId) {
  currentBrowsePath = path || '';
  const fbPathEl = document.getElementById('fb-current-path');
  if (fbPathEl) {
    fbPathEl.innerHTML = '';
    if (!path) {
      fbPathEl.textContent = t('defaultWorkspace') || '默认工作区';
    } else {
      const parts = path.split('/').filter(Boolean);
      const crumbs = [];
      let accum = '';
      parts.forEach((p, i) => {
        accum += (accum ? '/' : '') + p;
        crumbs.push({ name: p, path: accum, isLast: i === parts.length - 1 });
      });
      const root = document.createElement('span');
      root.className = 'crumb';
      root.textContent = (t('defaultWorkspace') || '默认工作区');
      root.onclick = () => browseDirectory('', projectId);
      fbPathEl.appendChild(root);
      crumbs.forEach(c => {
        const sep = document.createElement('span');
        sep.className = 'crumb-sep';
        sep.textContent = '/';
        fbPathEl.appendChild(sep);
        const span = document.createElement('span');
        span.className = 'crumb' + (c.isLast ? ' crumb-active' : '');
        span.textContent = c.name;
        if (!c.isLast) {
          span.onclick = () => browseDirectory(c.path, projectId);
          span.style.cursor = 'pointer';
        }
        fbPathEl.appendChild(span);
      });
    }
  }

  const treeEl = document.getElementById('file-tree');
  if (treeEl) {
    treeEl.innerHTML = '<div class="fb-empty">…</div>';
    if (!treeEl._bound) {
      treeEl.addEventListener('click', onTreeClick);
      treeEl._bound = true;
    }
  }

  try {
    const qs = new URLSearchParams();
    if (path) {
      const apiPath = path.startsWith('/') ? path : '/' + path;
      qs.set('path', apiPath);
    }
    if (projectId) qs.set('project_id', projectId);
    const res = await fetch('/files/browse?' + qs.toString());
    if (!res.ok) {
      if (res.status === 403) {
        if (typeof showToast === 'function') showToast('🔒 无权限访问该目录');
        const rootPath = currentProjectDir || '';
        if (path !== rootPath) {
          browseDirectory(rootPath, projectId);
        }
      } else {
        if (treeEl) treeEl.innerHTML = '<div class="fb-empty">' + escapeHtml(t('loadFailed') || '加载失败') + '</div>';
      }
      return;
    }
    const data = await res.json();
    renderFileTree(data, projectId);
  } catch (e) {
    if (treeEl) treeEl.innerHTML = '<div class="fb-empty">' + escapeHtml(t('loadFailed') || '加载失败') + '</div>';
  }
}

function renderFileTree(data, projectId) {
  const treeEl = document.getElementById('file-tree');
  if (!treeEl) return;
  const entries = data.entries || [];
  if (entries.length === 0) {
    treeEl.innerHTML = '<div class="fb-empty">' + escapeHtml(t('folderEmpty') || '空文件夹') + '</div>';
    treeEl._basePath = data.path;
    treeEl._projectId = projectId;
    return;
  }
  let html = '';
  entries.forEach(e => {
    const isDir = e.type === 'directory';
    const icon = isDir ? '📁' : (e.previewable ? '📄' : '📦');
    html += '<div class="tree-item ' + (isDir ? 'folder' : 'file') + '" ' +
      'data-name="' + escapeHtml(e.name) + '" data-type="' + (isDir ? 'folder' : 'file') + '" ' +
      'data-previewable="' + (e.previewable ? '1' : '0') + '">';
    html += '<span class="ti-icon">' + icon + '</span>';
    html += '<span class="ti-name">' + escapeHtml(e.name) + '</span></div>';
  });
  treeEl.innerHTML = html;
  treeEl._basePath = data.path;
  treeEl._projectId = projectId;
}

function onTreeClick(e) {
  const item = e.target.closest('.tree-item');
  if (!item) return;
  const name = item.getAttribute('data-name');
  const type = item.getAttribute('data-type');
  const previewable = item.getAttribute('data-previewable') === '1';
  const basePath = e.currentTarget._basePath || '';
  const projectId = e.currentTarget._projectId || '';
  const fullPath = basePath ? (basePath.replace(/\/$/, '') + '/' + name) : name;
  if (type === 'folder') {
    browseDirectory(fullPath, projectId);
  } else if (previewable) {
    readFile(fullPath, projectId);
  } else if (typeof showToast === 'function') {
    showToast(t('fileNotPreviewable') || '该文件类型不可预览');
  }
}

async function readFile(path, projectId) {
  try {
    const qs = new URLSearchParams({ path: path });
    if (projectId) qs.set('project_id', projectId);
    const res = await fetch('/files/read?' + qs.toString());
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      alert(d.detail || (t('readFileFailed') || '读取文件失败'));
      return;
    }
    const data = await res.json();
    openFilePreview(data.name, data.content, (data.path || '') + '  ·  ' + (data.lines || 0) + ' 行  ·  ' + formatSize(data.size || 0));
  } catch (e) {
    alert(t('readFileFailed') || '读取文件失败');
  }
}

function formatSize(bytes) {
  bytes = bytes || 0;
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1024 / 1024).toFixed(1) + ' MB';
}

// ---------- 变更文件视图 ----------

let _isChangesView = false;   // 当前是否在变更文件视图

function toggleChangesView() {
  _isChangesView = !_isChangesView;
  const btn = document.getElementById('fb-changes-btn');
  if (btn) {
    btn.classList.toggle('active', _isChangesView);
    if (_isChangesView) {
      // 进入变更视图：先加载数据，再根据是否有变更决定按钮行为
      btn.title = '变更文件';
      const iconNode = btn.childNodes[0];
      if (iconNode && iconNode.nodeType === Node.TEXT_NODE) iconNode.textContent = '📝';
      btn.onclick = toggleChangesView;
    } else {
      btn.title = '变更文件';
      const iconNode = btn.childNodes[0];
      if (iconNode && iconNode.nodeType === Node.TEXT_NODE) iconNode.textContent = '📝';
      btn.onclick = toggleChangesView;
    }
  }
  if (_isChangesView) {
    loadChangedFiles();
  } else {
    refreshFileBrowser();
  }
}

async function loadChangedFiles() {
  const treeEl = document.getElementById('file-tree');
  const pathEl = document.getElementById('fb-current-path');
  if (!treeEl || !pathEl) return;

  // 更新路径显示
  const repoName = currentProjectDir ? currentProjectDir.split('/').pop() : '';
  pathEl.textContent = '📝 变更文件' + (repoName ? (' · ' + repoName) : '');

  treeEl.innerHTML = '<div class="fb-empty">加载中…</div>';

  try {
    const qs = new URLSearchParams();
    if (currentProjectId) qs.set('project_id', currentProjectId);
    const res = await fetch('/files/changes?' + qs.toString());
    if (!res.ok) {
      const errData = await res.json().catch(() => ({ detail: res.statusText }));
      treeEl.innerHTML = '<div class="fb-empty">' + escapeHtml(errData.detail || '加载失败') + '</div>';
      _resetChangesBtn();
      return;
    }
    const data = await res.json();
    _updateChangesBadge(data.total_changes || 0);
    renderChangedFiles(data);
    // 根据是否有变更，切换按钮行为
    const hasChanges = (data.total_changes || 0) > 0;
    const btn = document.getElementById('fb-changes-btn');
    if (btn) {
      if (hasChanges) {
        btn.title = '提交变更';
        const iconNode = btn.childNodes[0];
        if (iconNode && iconNode.nodeType === Node.TEXT_NODE) iconNode.textContent = '💾';
        btn.onclick = openCommitDialog;
      } else {
        btn.title = '变更文件';
        const iconNode = btn.childNodes[0];
        if (iconNode && iconNode.nodeType === Node.TEXT_NODE) iconNode.textContent = '📝';
        btn.onclick = toggleChangesView;
      }
    }
  } catch (e) {
    treeEl.innerHTML = '<div class="fb-empty">' + escapeHtml(t('loadFailed') || '加载失败') + '</div>';
    _resetChangesBtn();
  }
}

function _resetChangesBtn() {
  const btn = document.getElementById('fb-changes-btn');
  if (!btn) return;
  btn.title = '变更文件';
  const iconNode = btn.childNodes[0];
  if (iconNode && iconNode.nodeType === Node.TEXT_NODE) iconNode.textContent = '📝';
  btn.onclick = toggleChangesView;
}

/** 更新变更按钮角标数字 */
function _updateChangesBadge(count) {
  const badge = document.getElementById('fb-changes-badge');
  if (!badge) return;
  if (count > 0) {
    badge.textContent = count > 99 ? '99+' : String(count);
    badge.style.display = '';
  } else {
    badge.style.display = 'none';
  }
}

/** 预加载变更数量（打开文件浏览器时自动调用，不切换视图） */
function prefetchChangesCount() {
  try {
    const qs = new URLSearchParams();
    if (currentProjectId) qs.set('project_id', currentProjectId);
    fetch('/files/changes?' + qs.toString())
      .then(res => res.ok ? res.json() : null)
      .then(data => { if (data) _updateChangesBadge(data.total_changes || 0); })
      .catch(() => {});
  } catch (_) {}
}

const _STATUS_LABELS = {
  modified: '已修改', added: '新增', deleted: '已删除',
  renamed: '重命名', copied: '复制', unmerged: '冲突',
  untracked: '新增', ignored: '忽略',
};

const _STATUS_ICONS = {
  modified: '📝', added: '✚', deleted: '✖', renamed: '↔',
  copied: '⧉', unmerged: '⚠', untracked: '✚', ignored: '⊘',
};

function renderChangedFiles(data) {
  const treeEl = document.getElementById('file-tree');
  if (!treeEl) return;

  const changes = data.changes || [];
  if (changes.length === 0) {
    treeEl.innerHTML = '<div class="fb-empty" style="padding:20px;text-align:center;color:#8e8e93;">✅ 没有变更</div>';
    return;
  }

  let html = '<div class="changes-summary">' +
    '<span>共 <strong>' + changes.length + '</strong> 个文件有变更</span>' +
    '</div>';

  changes.forEach(c => {
    const label = _STATUS_LABELS[c.status] || c.status;
    const icon = _STATUS_ICONS[c.status] || '•';

    // 文件名（取 basename）
    const displayName = c.path.split('/').pop();
    const dirPart = c.path.includes('/') ? ('<span class="cf-dir">' + escapeHtml(c.path.replace(/\/[^/]+$/, '')) + '/</span>') : '';

    const isUntracked = c.status === 'untracked';
    const actionBtn = isUntracked
      ? '<button class="cf-action-btn track-btn" title="加入跟踪" onclick="event.stopPropagation(); trackFile(\'' + escapeJsStr(c.path) + '\')">➕</button>'
      : '<button class="cf-action-btn untrack-btn" title="取消跟踪" onclick="event.stopPropagation(); untrackFile(\'' + escapeJsStr(c.path) + '\')">📤</button>';

    html += '<div class="change-item" data-path="' + escapeHtml(c.path) + '" onclick="showFileDiff(\'' + escapeJsStr(c.path) + '\')">';
    html += '  <span class="cf-icon">' + icon + '</span>';
    html += '  <span class="cf-info">';
    html += '    <span class="cf-name">' + dirPart + escapeHtml(displayName) + '</span>';
    html += '    <span class="cf-status ' + c.status + '">' + label + '</span>';
    html += '  </span>';
    html += '  <span class="cf-actions">' + actionBtn + '</span>';
    html += '</div>';
  });

  treeEl.innerHTML = html;
}

// 转义 JS 字符串字面量中的特殊字符
function escapeJsStr(s) {
  return s.replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n').replace(/\r/g, '\\r').replace(/"/g, '\\"');
}

/* ───────────────────────── 提交变更对话框 ───────────────────────── */

function openCommitDialog() {
  const existing = document.getElementById('commit-modal');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = 'commit-modal';
  overlay.className = 'commit-modal-overlay';
  overlay.innerHTML =
    '<div class="commit-modal">' +
      '<div class="commit-modal-head">' +
        '<span>💾 提交变更</span>' +
        '<button class="commit-modal-close" onclick="closeCommitDialog()" title="关闭">✕</button>' +
      '</div>' +
      '<textarea id="commit-msg" class="commit-msg" placeholder="提交信息（可手写，或点击「生成提交信息」自动生成后调整）"></textarea>' +
      '<div class="commit-modal-actions">' +
        '<button id="commit-gen-btn" class="btn-soft" onclick="commitGenerate()">✨ 生成提交信息</button>' +
        '<button id="commit-btn" class="btn-primary" onclick="commitDo(false)">📦 提交</button>' +
        '<button id="commit-push-btn" class="btn-accent" onclick="commitDo(true)">🚀 提交并推送</button>' +
      '</div>' +
      '<div id="commit-status" class="commit-status"></div>' +
    '</div>';
  document.body.appendChild(overlay);
  overlay.addEventListener('click', function (e) {
    if (e.target === overlay) closeCommitDialog();
  });
}

function closeCommitDialog() {
  const m = document.getElementById('commit-modal');
  if (m) m.remove();
  _resetChangesBtn();
}

async function commitGenerate() {
  const btn = document.getElementById('commit-gen-btn');
  const status = document.getElementById('commit-status');
  const ta = document.getElementById('commit-msg');
  if (!btn || !status || !ta) return;
  btn.disabled = true;
  btn.textContent = '⏳ 生成中…';
  status.className = 'commit-status';
  status.textContent = '正在根据改动生成提交信息…';
  try {
    const res = await fetch('/files/generate-commit-message', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: currentProjectId || '' }),
    });
    const d = await res.json();
    ta.value = d.message || '';
    status.className = 'commit-status ok';
    status.textContent = '✅ 已生成，可修改后提交';
  } catch (e) {
    status.className = 'commit-status err';
    status.textContent = '❌ 生成失败：' + (e && e.message ? e.message : e);
  } finally {
    btn.disabled = false;
    btn.textContent = '✨ 生成提交信息';
  }
}

async function commitDo(push) {
  const ta = document.getElementById('commit-msg');
  const btn = push ? document.getElementById('commit-push-btn') : document.getElementById('commit-btn');
  const status = document.getElementById('commit-status');
  if (!ta || !btn || !status) return;
  const msg = (ta.value || '').trim();
  if (!msg) {
    status.className = 'commit-status err';
    status.textContent = '⚠️ 请先填写或生成提交信息';
    return;
  }
  btn.disabled = true;
  btn.textContent = push ? '⏳ 提交并推送…' : '⏳ 提交中…';
  status.className = 'commit-status';
  status.textContent = push ? '正在提交并推送到远程…' : '正在提交…';
  try {
    const res = await fetch(push ? '/files/commit-and-push' : '/files/commit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: currentProjectId || '', message: msg }),
    });
    const d = await res.json();
    if (d.success) {
      status.className = 'commit-status ok';
      status.textContent = '✅ ' + (push ? '已提交并推送' : '已提交') + '\n' + (d.output || '');
      setTimeout(function () { loadChangedFiles(); checkUnpushedCommits(); closeCommitDialog(); }, 900);
    } else {
      status.className = 'commit-status err';
      status.textContent = '❌ 失败:\n' + (d.output || '未知错误');
    }
  } catch (e) {
    status.className = 'commit-status err';
    status.textContent = '❌ 请求失败：' + (e && e.message ? e.message : e);
  } finally {
    btn.disabled = false;
    btn.textContent = push ? '🚀 提交并推送' : '📦 提交';
  }
}

async function showFileDiff(filePath) {
  try {
    const qs = new URLSearchParams({ file_path: filePath });
    if (currentProjectId) qs.set('project_id', currentProjectId);
    const res = await fetch('/files/diff?' + qs.toString());
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      alert(d.detail || '获取 diff 失败');
      return;
    }
    const data = await res.json();

    // 用 diff 预览模式打开
    openFileDiffPreview(data);
  } catch (e) {
    alert(t('readFileFailed') || '读取失败');
  }
}

async function trackFile(filePath) {
  if (!confirm('确定要跟踪该文件吗？\n' + filePath)) return;
  try {
    const res = await fetch('/files/track', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: currentProjectId || '', file_path: filePath }),
    });
    const d = await res.json();
    if (d.success) {
      if (typeof showToast === 'function') showToast('✅ ' + (d.output || '已跟踪'));
      loadChangedFiles();
    } else {
      alert('跟踪失败：\n' + (d.output || '未知错误'));
    }
  } catch (e) {
    alert('跟踪失败：' + (e && e.message ? e.message : e));
  }
}

async function untrackFile(filePath) {
  if (!confirm('确定要取消跟踪该文件吗？文件将保留在工作区，但不再被 git 管理。\n' + filePath)) return;
  try {
    const res = await fetch('/files/untrack', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: currentProjectId || '', file_path: filePath }),
    });
    const d = await res.json();
    if (d.success) {
      if (typeof showToast === 'function') showToast('✅ ' + (d.output || '已取消跟踪'));
      loadChangedFiles();
    } else {
      alert('取消跟踪失败：\n' + (d.output || '未知错误'));
    }
  } catch (e) {
    alert('取消跟踪失败：' + (e && e.message ? e.message : e));
  }
}

/** 当前正在预览的 diff 原始文本（供复制按钮使用） */
let _currentDiffText = '';

/** 打开 diff 预览面板 */
function openFileDiffPreview(diffData) {
  const panel = document.querySelector('.file-preview-panel');
  if (!panel) return;

  // 标题：显示文件名、状态标签和统计
  const titleEl = panel.querySelector('.fpp-title');
  if (titleEl) {
    const fileName = (diffData.file_path || '').split('/').pop();
    const statusLabel = _STATUS_LABELS[diffData.effective_status] || '';
    const stats = diffData.stats || {};
    let extra = [];
    if (stats.additions) extra.push('+' + stats.additions);
    if (stats.deletions) extra.push('-' + stats.deletions);
    titleEl.textContent = '📝 ' + fileName +
      (statusLabel ? ' · ' + statusLabel : '') +
      (extra.length ? ' (' + extra.join(' ') + ')' : '');
  }

  // 元信息
  const metaEl = panel.querySelector('.fpp-meta');
  if (metaEl) {
    let meta = (diffData.file_path || '') + '  ·  ' + (diffData.line_count || 0) + ' 行 diff';
    if (diffData.is_new) meta += '  ·  新文件（整份内容均为新增）';
    metaEl.textContent = meta;
  }

  // 渲染 diff 内容
  const bodyEl = panel.querySelector('#fpp-body');
  if (!bodyEl) return;

  bodyEl.style.display = ''; // 显示 fpp-body
  const mdContainer = bodyEl.querySelector('#fpp-md');
  if (mdContainer) mdContainer.style.display = 'none'; // 隐藏 MD 容器
  const codeWrap = bodyEl.querySelector('#fpp-code-wrap');
  if (codeWrap) codeWrap.style.display = 'none';       // 隐藏普通代码容器

  const diffWrap = bodyEl.querySelector('#fpp-diff-wrap');
  if (!diffWrap) return;
  diffWrap.style.display = '';
  diffWrap.scrollTop = 0;

  _currentDiffText = diffData.diff_text || '';
  renderDiffHtml(diffWrap, _currentDiffText || '(无差异)');

  // 显示面板
  panel.classList.add('open');
  document.body.classList.add('file-preview-open');
}

/**
 * 将原始 diff 文本渲染为带双列行号（旧行号 | 新行号）的网格。
 * 从 @@ -old,count +new,count @@ hunk 头解析起始行号，逐行计算精确行号。
 */
function renderDiffHtml(wrapEl, diffText) {
  const lines = diffText.split('\n');
  let oldLine = 0, newLine = 0;
  let html = '';

  // 元信息行（无行号）：diff --git / index / --- / +++ / \ No newline 等
  const isMeta = (s) =>
    s.startsWith('diff --git') || s.startsWith('index ') ||
    s.startsWith('--- ') || s.startsWith('+++ ') ||
    s.startsWith('\\ No newline');

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    if (line.startsWith('@@')) {
      // 解析 hunk 头：@@ -a,b +c,d @@
      const m = line.match(/^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/);
      if (m) { oldLine = parseInt(m[1], 10); newLine = parseInt(m[3], 10); }
      html += diffRow('', '', line, 'hunk');
    } else if (isMeta(line)) {
      html += diffRow('', '', line, 'meta');
    } else if (line.startsWith('+')) {
      html += diffRow('', String(newLine), line, 'add');
      newLine++;
    } else if (line.startsWith('-')) {
      html += diffRow(String(oldLine), '', line, 'del');
      oldLine++;
    } else {
      // 上下文行（含前导空格），新旧行号都推进
      html += diffRow(String(oldLine), String(newLine), line, 'context');
      oldLine++;
      newLine++;
    }
  }

  wrapEl.innerHTML = html;
}

/** 构建一行 diff（三列网格：旧行号 | 新行号 | 内容） */
function diffRow(oldLn, newLn, text, kind) {
  return '<div class="diff-row ' + kind + '">' +
    '<span class="dg-old">' + escapeHtml(oldLn) + '</span>' +
    '<span class="dg-new">' + escapeHtml(newLn) + '</span>' +
    '<span class="dg-code">' + escapeHtml(text) + '</span>' +
  '</div>';
}
