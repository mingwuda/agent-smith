/* cron.js — 项目 ⋯ 菜单「定时任务」入口：侧边栏独立面板（类似文件浏览器）
   入口: workspace.js 项目下拉菜单 → openCronPanel(projectId)
   面板: #cron-panel（返回 ← / 刷新 ⟳ / 新增 ＋）
   依赖: util.js(escapeHtml, escAttr), i18n.js(t), workspace.js(loadProjects, projectsCache) */

let cronSelectedProject = '';   // 当前查看的项目 id（''=全部）
let cronEditing = null;         // 正在编辑的 task_id（null=新增）
let cronRunningIds = new Set(); // 正在手动执行的任务 id

// ---------- 面板开合（与文件浏览器同款：替换项目列表） ----------

async function openCronPanel(projectId) {
  const panel = document.getElementById('cron-panel');
  if (!panel) return;
  cronSelectedProject = projectId || '';
  if (typeof loadProjects === 'function') {
    try { await loadProjects(); } catch (e) { /* ignore */ }
  }
  const proj = (typeof projectsCache !== 'undefined' ? projectsCache : []) || []
    .find(p => String(p.id) === String(cronSelectedProject));
  const title = document.getElementById('cron-panel-title');
  if (title) title.textContent = '⏰ ' + (proj ? proj.name : t('cronPanelTitle'));

  const listEl = document.getElementById('project-list');
  if (listEl) listEl.style.display = 'none';
  panel.style.display = 'flex';
  document.querySelectorAll('.sidebar-accordion').forEach(a => a.style.display = 'none');
  loadCronTasks();
}

function exitCronPanel() {
  const panel = document.getElementById('cron-panel');
  const listEl = document.getElementById('project-list');
  if (panel) panel.style.display = 'none';
  if (listEl) listEl.style.display = '';
  document.querySelectorAll('.sidebar-accordion').forEach(a => a.style.display = '');
}

// ---------- 列表 ----------

async function loadCronTasks() {
  const list = document.getElementById('cron-list');
  if (!list) return;
  list.innerHTML = '<div class="cron-list-empty">' + escapeHtml(t('cronLoading')) + '</div>';
  try {
    const qs = cronSelectedProject ? '/projects/' + encodeURIComponent(cronSelectedProject) + '/cron' : '/cron';
    const res = await fetch(qs);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    renderCronTasks(list, data.tasks || []);
  } catch (e) {
    list.innerHTML = '<div class="cron-list-empty cron-list-err">' +
      escapeHtml(t('cronLoadFailed')) + '：' + escapeHtml(String(e && e.message || e)) + '</div>';
  }
}

function renderCronTasks(list, tasks) {
  if (!tasks.length) {
    list.innerHTML = '<div class="cron-list-empty">' + escapeHtml(t('cronEmpty')) + '</div>';
    return;
  }
  let html = '';
  tasks.forEach(tk => {
    const badge = tk.enabled
      ? '<span class="cron-badge on">' + escapeHtml(t('cronRunning')) + '</span>'
      : '<span class="cron-badge off">' + escapeHtml(t('cronDisabled')) + '</span>';
    const status = tk.last_status === 'error'
      ? '<span class="cron-status err">' + escapeHtml(t('cronLastError')) + '</span>'
      : (tk.last_status === 'ok' ? '<span class="cron-status ok">' + escapeHtml(t('cronLastOk')) + '</span>' : '');
    const lastRun = tk.last_run_at
      ? escapeHtml(tk.last_run_at.slice(0, 16).replace('T', ' '))
      : escapeHtml(t('cronNeverRun'));
    const running = cronRunningIds.has(tk.id);
    html += '<div class="cron-item" data-id="' + escAttr(tk.id) + '" data-project="' + escAttr(tk.project_id || '') + '" data-cron="' + escAttr(tk.cron || '') + '" data-content="' + escAttr((tk.content || '').slice(0, 200)) + '" data-enabled="' + tk.enabled + '">';
    html += '  <div class="cron-item-main">';
    html += '    <div class="cron-item-name">' + escapeHtml(tk.name) + ' ' + badge + '</div>';
    html += '    <div class="cron-item-cron">⏱ ' + escapeHtml(tk.cron_human || tk.cron || '') + '</div>';
    html += '    <div class="cron-item-content">' + escapeHtml(t('cronContentPrefix')) + escapeHtml((tk.content || '').slice(0, 80)) + '</div>';
    html += '    <div class="cron-item-meta">' + escapeHtml(t('cronLastRun')) + ' ' + lastRun + ' ' + status + '</div>';
    html += '  </div>';
    html += '  <div class="cron-item-actions">';
    html += '    <button class="cron-btn-run" onclick="runCronNow(\'' + escAttr(tk.id) + '\')"' + (running ? ' disabled' : '') + '>' +
      escapeHtml(running ? t('cronRunningNow') : t('cronRunNow')) + '</button>';
    html += '    <button class="cron-btn-soft" onclick="toggleCronEnabled(\'' + escAttr(tk.id) + '\')">' +
      escapeHtml(tk.enabled ? t('cronDisable') : t('cronEnable')) + '</button>';
    html += '    <button class="cron-btn-soft" onclick="openCronEditor(\'' + escAttr(tk.id) + '\')">' + escapeHtml(t('cronEdit')) + '</button>';
    html += '    <button class="cron-btn-danger" onclick="deleteCronTask(\'' + escAttr(tk.id) + '\')">' + escapeHtml(t('cronDelete')) + '</button>';
    html += '  </div>';
    html += '</div>';
  });
  list.innerHTML = html;
}

// ---------- 立即执行 ----------

async function runCronNow(taskId) {
  if (cronRunningIds.has(taskId)) return;
  cronRunningIds.add(taskId);
  _refreshCronRunButtons();
  let ok = false;
  try {
    const res = await fetch('/cron/' + encodeURIComponent(taskId) + '/run', { method: 'POST' });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || 'HTTP ' + res.status);
    }
    ok = true;
  } catch (e) {
    alert(t('cronRunFailed') + '：' + String(e && e.message || e));
  } finally {
    cronRunningIds.delete(taskId);
    if (ok && typeof switchSession === 'function') {
      switchSession('cron_sess_' + taskId, 'web', true);
    }
    loadCronTasks();
  }
}

function _refreshCronRunButtons() {
  document.querySelectorAll('#cron-list .cron-item').forEach(item => {
    const id = item.getAttribute('data-id');
    const btn = item.querySelector('.cron-btn-run');
    if (!btn) return;
    const running = cronRunningIds.has(id);
    btn.disabled = running;
    btn.textContent = running ? t('cronRunningNow') : t('cronRunNow');
  });
}

// ---------- 新增/编辑对话框 ----------

function openCronEditor(taskId) {
  cronEditing = taskId || null;
  const modal = document.getElementById('cron-editor-modal');
  if (!modal) return;
  ['cron-edit-name', 'cron-edit-cron', 'cron-edit-content'].forEach(id => {
    document.getElementById(id).value = '';
  });
  const hint = document.getElementById('cron-edit-hint');
  hint.textContent = t('cronTimeHint');

  const projSel = document.getElementById('cron-edit-project');
  const projects = (typeof projectsCache !== 'undefined' ? projectsCache : []) || [];
  let selHtml = '';

  if (taskId) {
    const item = document.querySelector('#cron-list .cron-item[data-id="' + CSS.escape(taskId) + '"]');
    const pid = item ? item.getAttribute('data-project') || cronSelectedProject : cronSelectedProject;
    selHtml = projects.map(p =>
      '<option value="' + escAttr(p.id) + '"' + (p.id === pid ? ' selected' : '') + '>' + escapeHtml(p.name) + '</option>'
    ).join('');
    projSel.disabled = true;
    if (item) {
      document.getElementById('cron-edit-name').value =
        (item.querySelector('.cron-item-name').childNodes[0].textContent || '').trim();
      document.getElementById('cron-edit-cron').value = item.getAttribute('data-cron') || '';
      document.getElementById('cron-edit-content').value = item.getAttribute('data-content') || '';
    }
    document.getElementById('cron-editor-title').textContent = t('cronEditTitle');
    document.getElementById('cron-edit-submit').textContent = t('cronSave');
  } else {
    const def = cronSelectedProject || (projects[0] && projects[0].id) || '';
    selHtml = projects.map(p =>
      '<option value="' + escAttr(p.id) + '"' + (p.id === def ? ' selected' : '') + '>' + escapeHtml(p.name) + '</option>'
    ).join('');
    projSel.disabled = false;
    document.getElementById('cron-editor-title').textContent = t('cronNewTitle');
    document.getElementById('cron-edit-submit').textContent = t('cronCreate');
  }
  projSel.innerHTML = projects.length ? selHtml : '<option value="">' + escapeHtml(t('cronNoProject')) + '</option>';
  modal.classList.add('active');
}

function closeCronEditor() {
  const modal = document.getElementById('cron-editor-modal');
  if (modal) modal.classList.remove('active');
}

async function submitCronEditor() {
  const name = document.getElementById('cron-edit-name').value.trim();
  const cronText = document.getElementById('cron-edit-cron').value.trim();
  const content = document.getElementById('cron-edit-content').value.trim();
  const hint = document.getElementById('cron-edit-hint');
  if (!name) { hint.textContent = t('cronNameRequired'); return; }
  if (!cronText) { hint.textContent = t('cronTimeRequired'); return; }
  if (!content) { hint.textContent = t('cronContentRequired'); return; }

  const projectId = document.getElementById('cron-edit-project').value;
  if (!projectId) { hint.textContent = t('cronProjectRequired'); return; }

  const submitBtn = document.getElementById('cron-edit-submit');
  submitBtn.disabled = true;
  try {
    let res;
    if (cronEditing) {
      res = await fetch('/cron/' + encodeURIComponent(cronEditing), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, cron: cronText, content }),
      });
    } else {
      res = await fetch('/cron', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_id: projectId, name, cron: cronText, content }),
      });
    }
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || 'HTTP ' + res.status);
    }
    closeCronEditor();
    loadCronTasks();
  } catch (e) {
    hint.textContent = '❌ ' + escapeHtml(String(e && e.message || e));
  } finally {
    submitBtn.disabled = false;
  }
}

// ---------- 启用/停用、删除 ----------

async function toggleCronEnabled(taskId) {
  const item = document.querySelector('#cron-list .cron-item[data-id="' + CSS.escape(taskId) + '"]');
  const curEnabled = item ? item.getAttribute('data-enabled') === 'true' : false;
  try {
    const res = await fetch('/cron/' + encodeURIComponent(taskId), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !curEnabled }),
    });
    if (res.ok) loadCronTasks();
  } catch (e) { /* ignore */ }
}

async function deleteCronTask(taskId) {
  if (!confirm(t('cronDeleteConfirm'))) return;
  try {
    const res = await fetch('/cron/' + encodeURIComponent(taskId), { method: 'DELETE' });
    if (res.ok) loadCronTasks();
  } catch (e) { /* ignore */ }
}
