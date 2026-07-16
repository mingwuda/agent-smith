/* file-preview.js — 右侧文件预览面板（点击项目文件后展示内容） */

function openFilePreview(name, content, meta) {
  const panel = document.getElementById('file-preview-panel');
  if (!panel) return;
  const title = document.getElementById('fpp-title');
  const metaEl = document.getElementById('fpp-meta');
  const code = document.getElementById('fpp-code');
  if (title) title.textContent = name || (t('filePreview') || '文件预览');
  if (metaEl) metaEl.textContent = meta || '';
  if (code) code.textContent = content || '';
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
  const code = document.getElementById('fpp-code');
  if (!code) return;
  const text = code.textContent || '';
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(function () {
      if (typeof showToast === 'function') showToast(t('copied') || '已复制');
    }).catch(function () {});
  }
}
