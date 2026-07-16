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
  if (!codeEl || !mdEl) return;

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
