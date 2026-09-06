/* file-preview.js — 右侧文件预览面板（点击项目文件后展示内容） */

// 支持 Markdown 渲染的扩展名
const MD_EXTS = ['md', 'markdown', 'mdx'];
// 支持语法高亮的代码扩展名
const CODE_EXTS = [
  'js', 'jsx', 'ts', 'tsx', 'py', 'java', 'go', 'rs', 'c', 'cpp', 'h', 'hpp',
  'cs', 'php', 'rb', 'sh', 'bash', 'zsh', 'json', 'yaml', 'yml', 'toml',
  'css', 'scss', 'html', 'xml', 'sql', 'swift', 'kt', 'kts', 'scala', 'r',
  'lua', 'vim', 'dockerfile', 'makefile', 'ini', 'conf', 'gitignore', 'txt'
];

// 部分扩展名映射到 highlight.js 的语言名
function hljsLang(ext) {
  const map = { sh: 'bash', zsh: 'bash', yml: 'yaml', scss: 'css',
    dockerfile: 'dockerfile', makefile: 'makefile', gitignore: 'bash', conf: 'ini' };
  return map[ext] || ext;
}

function fileIcon(name) {
  const ext = (name || '').split('.').pop().toLowerCase();
  if (name && !name.includes('.')) return '📄';
  const map = {
    md: '📝', markdown: '📝', mdx: '📝',
    js: '📜', jsx: '⚛️', ts: '📘', tsx: '⚛️',
    py: '🐍', java: '☕', go: '🐹', rs: '⚙️',
    c: '🔧', cpp: '🔧', h: '🔧', hpp: '🔧',
    cs: '🔷', php: '🐘', rb: '💎', swift: '🦉',
    kt: '🅺', kts: '🅺', scala: '🔴', r: '📊',
    lua: '🌙', vim: '📄',
    sh: '⌨️', bash: '⌨️', zsh: '⌨️',
    json: '📋', yaml: '⚙️', yml: '⚙️', toml: '⚙️',
    css: '🎨', scss: '🎨', html: '🌐', xml: '🌐',
    sql: '🗃️', dockerfile: '🐳', makefile: '🔨',
    ini: '⚙️', conf: '⚙️', gitignore: '🔒', txt: '📄',
  };
  return map[ext] || '📄';
}

function openFilePreview(name, content, meta) {
  const panel = document.getElementById('file-preview-panel');
  if (!panel) return;
  const icon = document.getElementById('fpp-icon');
  const title = document.getElementById('fpp-title');
  const metaEl = document.getElementById('fpp-meta');
  const codeEl = document.getElementById('fpp-code');
  const codeWrap = document.getElementById('fpp-code-wrap');
  const gutter = document.getElementById('fpp-gutter');
  const mdEl = document.getElementById('fpp-md');
  const diffWrap = document.getElementById('fpp-diff-wrap');
  if (!codeEl || !mdEl) return;

  // 普通预览时隐藏 diff 容器（避免与 diff 视图互相串味）
  if (diffWrap) diffWrap.style.display = 'none';

  // 按内容行数生成连续行号(零依赖行号列), 行号与代码逐行对齐
  function fillGutter(text) {
    if (!gutter) return;
    const n = (text || '').split('\n').length;
    let s = '';
    for (let i = 1; i <= n; i++) s += i + (i < n ? '\n' : '');
    gutter.textContent = s;
  }

  if (icon) icon.textContent = fileIcon(name);
  if (title) title.textContent = name || (t('filePreview') || '文件预览');
  if (metaEl) metaEl.textContent = meta || '';

  const ext = (name || '').split('.').pop().toLowerCase();
  const isMd = MD_EXTS.includes(ext);
  const isCode = CODE_EXTS.includes(ext);
  const hljsReady = typeof hljs !== 'undefined';

  if (isMd && typeof renderMarkdown === 'function') {
    // ----- Markdown：渲染为富文本 HTML -----
    if (codeWrap) codeWrap.style.display = 'none';
    mdEl.style.display = '';
    mdEl.innerHTML = renderMarkdown(content || '');
    if (hljsReady) {
      mdEl.querySelectorAll('pre code').forEach(function (b) { hljs.highlightElement(b); });
    }
  } else if (isCode && hljsReady) {
    // ----- 代码文件：语法高亮 + 行号 -----
    mdEl.style.display = 'none';
    if (codeWrap) codeWrap.style.display = '';
    codeEl.textContent = content || '';
    codeEl.className = 'language-' + hljsLang(ext);
    // 复用同一 <code> 元素时, 清除上一次高亮留下的 data-highlighted 标记,
    // 否则 highlight.js 检测到"已高亮"会直接 return, 导致第二次起不再高亮
    codeEl.removeAttribute('data-highlighted');
    codeEl.classList.remove('hljs');
    try { hljs.highlightElement(codeEl); } catch (e) { /* 忽略 */ }
    fillGutter(content);
  } else {
    // ----- 其他：纯文本 + 行号 -----
    mdEl.style.display = 'none';
    if (codeWrap) codeWrap.style.display = '';
    codeEl.textContent = content || '';
    codeEl.className = '';
    fillGutter(content);
  }

  panel.classList.add('open');
  document.body.classList.add('file-preview-open');
  const body = panel.querySelector('.fpp-body');
  if (body) body.scrollTop = 0;
}

function closeFilePreview() {
  const panel = document.getElementById('file-preview-panel');
  if (panel) panel.classList.remove('open');
  document.body.classList.remove('file-preview-open');
}

function copyFileContent() {
  // diff 视图：复制原始 diff 文本（不含行号，保持可用）
  const diffWrap = document.getElementById('fpp-diff-wrap');
  if (diffWrap && diffWrap.style.display !== 'none' && typeof _currentDiffText !== 'undefined' && _currentDiffText) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(_currentDiffText).then(function () {
        if (typeof showToast === 'function') showToast(t('copied') || '已复制');
      }).catch(function () {});
    }
    return;
  }

  const mdEl = document.getElementById('fpp-md');
  const codeEl = document.getElementById('fpp-code');
  let text = '';
  if (mdEl && mdEl.style.display !== 'none') {
    text = mdEl.textContent || '';
  } else if (codeEl) {
    text = codeEl.textContent || '';
  }
  if (!text) return;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(function () {
      if (typeof showToast === 'function') showToast(t('copied') || '已复制');
    }).catch(function () {});
  }
}


/* === ponytail: 文件内联编辑支持 ===
   - 注入"编辑"按钮到预览面板 actions 区
   - enterEditMode() 切换到 textarea 编辑器
   - saveEditFile() 调 /files/write 保存
   - cancelEditFile() 还原到只读视图
   - loadMoreFileLines() 大文件分段加载（滚到底时调用）
*/

// 当前打开的文件上下文（每次 openFilePreview 时刷新）
let _currentFileCtx = null;

function injectEditButton(ctx) {
  // 找 .fpp-actions 容器，幂等：先移除已存在的 edit-btn
  const actions = document.querySelector('#file-preview-panel .fpp-actions');
  if (!actions) return;
  const oldBtn = actions.querySelector('.fpp-edit-btn');
  if (oldBtn) oldBtn.remove();

  // 仅在可编辑 + 非 Markdown（Markdown 编辑体验差，先不支持）时显示
  const ext = (ctx.name || '').split('.').pop().toLowerCase();
  if (!ctx.editable || MD_EXTS.includes(ext)) return;

  const btn = document.createElement('button');
  btn.className = 'fpp-btn fpp-edit-btn';
  btn.setAttribute('data-i18n-title', 'editFile');
  btn.title = t('editFile') || '编辑';
  btn.setAttribute('aria-label', btn.title);
  btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg>';
  btn.onclick = enterEditMode;
  // 插在第一个位置（复制按钮之前）
  actions.insertBefore(btn, actions.firstChild);
}

function enterEditMode() {
  if (!_currentFileCtx || !_currentFileCtx.editable) return;
  const codeWrap = document.getElementById('fpp-code-wrap');
  const editorWrap = document.getElementById('fpp-editor-wrap');
  const editor = document.getElementById('fpp-editor');
  const editorGutter = document.getElementById('fpp-editor-gutter');
  const toolbar = document.getElementById('fpp-edit-toolbar');
  const loadMoreBtn = document.getElementById('fpp-load-more-btn');
  if (!codeWrap || !editorWrap || !editor) return;

  // ponytail: 加载更多按钮——还有更多行就显示
  if (loadMoreBtn) {
    loadMoreBtn.style.display = _currentFileCtx.hasMore ? '' : 'none';
  }

  // ponytail: 切到编辑态前，先把"复制/编辑"按钮隐藏，工具栏显示
  const actions = document.querySelector('#file-preview-panel .fpp-actions');
  if (actions) actions.style.display = 'none';
  if (toolbar) toolbar.style.display = '';

  // ponytail: 行号 + textarea 内容（保留 trailing_newline，渲染时多一个空行）
  const lines = (_currentFileCtx.content || '').split('\n');
  editor.value = _currentFileCtx.content || '';
  syncEditorGutter();

  // 显示编辑区，隐藏只读 code 区
  codeWrap.style.display = 'none';
  editorWrap.style.display = '';
  editor.focus();

  // ponytail: 滚动同步 + 接近底部自动加载更多
  editor.onscroll = function () {
    // gutter 与 textarea 同步滚动
    if (editorGutter) editorGutter.scrollTop = editor.scrollTop;
    // ponytail: 距离底部 < 5 行时自动加载下一批（只在 has_more=true 且未在加载中时）
    if (!_currentFileCtx.hasMore || _currentFileCtx._loadingMore) return;
    const remaining = editor.scrollHeight - editor.scrollTop - editor.clientHeight;
    const lineHeight = parseFloat(getComputedStyle(editor).lineHeight) || 18;
    if (remaining < lineHeight * 5) {
      loadMoreFileLines();
    }
  };
  editor.oninput = syncEditorGutter;
}

function syncEditorGutter() {
  const editor = document.getElementById('fpp-editor');
  const gutter = document.getElementById('fpp-editor-gutter');
  if (!editor || !gutter) return;
  const n = editor.value.split('\n').length;
  let s = '';
  for (let i = 1; i <= n; i++) s += i + (i < n ? '\n' : '');
  gutter.textContent = s;
}

function exitEditMode() {
  const codeWrap = document.getElementById('fpp-code-wrap');
  const editorWrap = document.getElementById('fpp-editor-wrap');
  const toolbar = document.getElementById('fpp-edit-toolbar');
  const actions = document.querySelector('#file-preview-panel .fpp-actions');
  if (codeWrap) codeWrap.style.display = '';
  if (editorWrap) editorWrap.style.display = 'none';
  if (toolbar) toolbar.style.display = 'none';
  if (actions) actions.style.display = '';
}

function cancelEditFile() {
  if (!_currentFileCtx) return;
  // ponytail: 取消 = 还原 content（重新打开预览）——简单粗暴，避免 dirty 状态跟踪
  exitEditMode();
  // 重新渲染只读视图
  if (typeof openFilePreview === 'function') {
    openFilePreview(
      _currentFileCtx.name,
      _currentFileCtx.content,
      document.getElementById('fpp-meta').textContent,
      _currentFileCtx
    );
  }
}

async function saveEditFile() {
  if (!_currentFileCtx || !_currentFileCtx.path) return;
  const editor = document.getElementById('fpp-editor');
  const saveBtn = document.getElementById('fpp-save-btn');
  if (!editor) return;

  // ponytail: 禁用保存按钮防重复提交
  if (saveBtn) {
    saveBtn.disabled = true;
    const oldText = saveBtn.innerHTML;
    saveBtn.innerHTML = (t('saving') || '保存中...');
  }

  try {
    const res = await fetch('/files/write', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        path: _currentFileCtx.path,
        content: editor.value,
        project_id: _currentFileCtx.projectId || '',
      }),
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      const msg = d.detail || (t('saveFileFailed') || '保存失败');
      if (typeof showToast === 'function') showToast(msg, 'error');
      else alert(msg);
      return;
    }
    // ponytail: 保存成功——更新 _currentFileCtx.content 为新值（供后续 cancel 还原）
    _currentFileCtx.content = editor.value;
    if (typeof showToast === 'function') showToast(t('saved') || '已保存');
    // 退出编辑态，重新渲染只读视图（带语法高亮）
    exitEditMode();
    if (typeof openFilePreview === 'function') {
      openFilePreview(
        _currentFileCtx.name,
        _currentFileCtx.content,
        document.getElementById('fpp-meta').textContent,
        _currentFileCtx
      );
    }
  } catch (e) {
    if (typeof showToast === 'function') showToast((t('saveFileFailed') || '保存失败') + ': ' + e, 'error');
    else alert(t('saveFileFailed') || '保存失败');
  } finally {
    if (saveBtn) {
      saveBtn.disabled = false;
      saveBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"></path><polyline points="17 21 17 13 7 13 7 21"></polyline><polyline points="7 3 7 8 15 8"></polyline></svg><span>' + (t('save') || '保存') + '</span>';
    }
  }
}

async function loadMoreFileLines() {
  if (!_currentFileCtx || !_currentFileCtx.hasMore || !_currentFileCtx.path) return;
  const loadBtn = document.getElementById('fpp-load-more-btn');
  const editor = document.getElementById('fpp-editor');
  if (!editor) return;

  // ponytail: 防止并发触发
  if (_currentFileCtx._loadingMore) return;
  _currentFileCtx._loadingMore = true;
  if (loadBtn) loadBtn.disabled = true;

  try {
    const qs = new URLSearchParams({
      path: _currentFileCtx.path,
      offset: String(_currentFileCtx.nextOffset || 0),
      limit: '100',
    });
    if (_currentFileCtx.projectId) qs.set('project_id', _currentFileCtx.projectId);
    const res = await fetch('/files/read?' + qs.toString());
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      if (typeof showToast === 'function') showToast(d.detail || (t('loadMoreFailed') || '加载更多失败'), 'error');
      return;
    }
    const data = await res.json();
    // ponytail: 追加到 textarea——首行如果是上一批末尾的延续则需处理换行
    // 后端返回的 content 是 [start, end) 行段的纯文本，无 \n 结尾
    const sep = _currentFileCtx.content.endsWith('\n') || _currentFileCtx.content === '' ? '' : '\n';
    editor.value = _currentFileCtx.content + sep + (data.content || '');
    _currentFileCtx.content = editor.value;
    _currentFileCtx.hasMore = !!data.has_more;
    _currentFileCtx.nextOffset = data.next_offset;
    syncEditorGutter();
    // 隐藏按钮当无更多
    if (loadBtn) loadBtn.style.display = data.has_more ? '' : 'none';
  } catch (e) {
    if (typeof showToast === 'function') showToast((t('loadMoreFailed') || '加载更多失败') + ': ' + e, 'error');
  } finally {
    _currentFileCtx._loadingMore = false;
    if (loadBtn) loadBtn.disabled = false;
  }
}
