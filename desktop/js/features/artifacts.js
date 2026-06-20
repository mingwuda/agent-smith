/* artifacts.js — Markdown 制品预览弹窗
   依赖: util.js(renderMarkdown), i18n.js(t, currentLanguage) */

async function openArtifactPreview(path) {
  const modal = document.getElementById('artifact-preview-modal');
  const title = document.getElementById('artifact-preview-title');
  const pathEl = document.getElementById('artifact-preview-path');
  const body = document.getElementById('artifact-preview-body');
  const download = document.getElementById('artifact-preview-download');
  title.textContent = t('artifactPreviewTitle');
  pathEl.textContent = path;
  body.textContent = t('artifactLoading');
  download.href = `/artifacts/download?path=${encodeURIComponent(path)}`;
  modal.classList.add('active');
  try {
    const res = await fetch(`/artifacts/preview?path=${encodeURIComponent(path)}`);
    const data = await res.json();
    if (!res.ok) throw new Error(currentLanguage === 'en' ? t('artifactPreviewFailed') : (data.detail || t('artifactPreviewFailed')));
    title.textContent = data.name || t('artifactPreviewTitle');
    pathEl.textContent = data.path || path;
    body.innerHTML = renderMarkdown(data.content || '');
    _enhancePreviewScroll(body);
  } catch (error) {
    body.textContent = error.message || t('artifactPreviewFailed');
  }
}

function closeArtifactPreview() {
  document.getElementById('artifact-preview-modal').classList.remove('active');
}

// 点击遮罩关闭
document.getElementById('artifact-preview-modal').addEventListener('click', function(e) {
  if (e.target === this) closeArtifactPreview();
});
