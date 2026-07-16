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
          html += '  <span class="psi-icon">💬</span>';
          html += '  <span class="psi-title">' + escapeHtml(s.title || t('unnamed') || '未命名') + '</span>';
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
    html += '  </div>';
    html += '  <div class="project-sessions">';
    unassigned.forEach(s => {
      const isActive = s.id === currentSessionId && s.source === currentSessionSource;
      html += '<div class="psession-item ' + (isActive ? 'active' : '') + '" data-key="' + _sessionKey(s) + '" ' +
        'onclick="switchSession(\'' + s.id + '\',\'' + (s.source || 'web') + '\')">';
      html += '  <span class="psi-icon">💬</span>';
      html += '  <span class="psi-title">' + escapeHtml(s.title || t('unnamed') || '未命名') + '</span>';
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

  const listEl = document.getElementById('project-list');
  const fbEl = document.getElementById('file-browser-panel');
  if (listEl) listEl.style.display = 'none';
  // 直接通过 inline style 控制显隐，避免被 HTML 中内联的 display:none 覆盖
  if (fbEl) fbEl.style.display = 'flex';
  document.querySelectorAll('.sidebar-accordion').forEach(a => a.style.display = 'none');

  const path = currentProjectDir || '';
  await browseDirectory(path, projectId);
}

function exitFileBrowser() {
  const listEl = document.getElementById('project-list');
  const fbEl = document.getElementById('file-browser-panel');
  if (listEl) listEl.style.display = '';
  // 与 openFileBrowser 对应：通过 inline style 隐藏，避免被 class 覆盖
  if (fbEl) fbEl.style.display = 'none';
  document.querySelectorAll('.sidebar-accordion').forEach(a => a.style.display = '');
  closeFilePreview();
}

function refreshFileBrowser() {
  browseDirectory(currentBrowsePath, currentProjectId);
}

async function browseDirectory(path, projectId) {
  currentBrowsePath = path || '';
  const fbPathEl = document.getElementById('fb-current-path');
  if (fbPathEl) fbPathEl.textContent = path || (t('defaultWorkspace') || '默认工作区');

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
    if (path) qs.set('path', path);
    if (projectId) qs.set('project_id', projectId);
    const res = await fetch('/files/browse?' + qs.toString());
    if (!res.ok) {
      if (treeEl) treeEl.innerHTML = '<div class="fb-empty">' + escapeHtml(t('loadFailed') || '加载失败') + '</div>';
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
