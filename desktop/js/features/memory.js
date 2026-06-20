/* memory.js — 记忆管理弹窗
   依赖: state.js, util.js, i18n.js */

function openMemory() {
  document.getElementById('memory-modal').classList.add('active');
  document.getElementById('memory-feedback').textContent = '';
  document.getElementById('memory-feedback').className = 'save-feedback';
  loadMemories();
}

function closeMemory() {
  document.getElementById('memory-modal').classList.remove('active');
}

function renderMemories(items) {
  const list = document.getElementById('memory-list');
  if (!items || items.length === 0) {
    list.innerHTML = `<div style="padding:12px;color:#8e8e93;font-size:13px;">${escapeHtml(t('memoryEmpty'))}</div>`;
    return;
  }
  list.innerHTML = items.map(item => {
    const key = escapeHtml(item.key || '');
    const encodedKey = encodeURIComponent(item.key || '');
    const value = escapeHtml(typeof item.value === 'string' ? item.value : JSON.stringify(item.value, null, 2));
    return `<div class="memory-item">
      <div class="memory-key">
        <span>${key}</span>
        <button class="memory-delete" onclick="deleteMemory(decodeURIComponent('${encodedKey}'))">${escapeHtml(t('deleteMemory'))}</button>
      </div>
      <div class="memory-value">${value}</div>
    </div>`;
  }).join('');
}

async function loadMemories() {
  const q = document.getElementById('m-search').value.trim();
  try {
    const res = await fetch(`/memories${q ? '?q=' + encodeURIComponent(q) : ''}`);
    if (!res.ok) return;
    const data = await res.json();
    let items = data.items || [];
    if (q) {
      const lower = q.toLowerCase();
      items = items.filter(item => {
        const value = typeof item.value === 'string' ? item.value : JSON.stringify(item.value);
        return (item.key || '').toLowerCase().includes(lower) || value.toLowerCase().includes(lower);
      });
    }
    renderMemories(items);
  } catch {}
}

async function saveMemory() {
  const keyEl = document.getElementById('m-key');
  const valueEl = document.getElementById('m-value');
  const feedback = document.getElementById('memory-feedback');
  const key = keyEl.value.trim();
  const value = valueEl.value.trim();
  if (!key || !value) {
    feedback.textContent = t('fillMemory');
    feedback.className = 'save-feedback err';
    return;
  }
  try {
    const res = await fetch('/memories', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key, value }),
    });
    const data = await res.json();
    if (res.ok) {
      feedback.textContent = currentLanguage === 'en' ? t('memorySaved') : (data.message || t('memorySaved'));
      feedback.className = 'save-feedback ok';
      keyEl.value = '';
      valueEl.value = '';
      await loadMemories();
    } else {
      feedback.textContent = currentLanguage === 'en' ? t('memorySaveFailed') : (data.detail || t('memorySaveFailed'));
      feedback.className = 'save-feedback err';
    }
  } catch {
    feedback.textContent = t('memoryNetworkError');
    feedback.className = 'save-feedback err';
  }
}

async function deleteMemory(key) {
  if (!confirm(t('deleteMemoryConfirm', { key }))) return;
  try {
    await fetch(`/memories/${encodeURIComponent(key)}`, { method: 'DELETE' });
    await loadMemories();
  } catch {}
}

// 点击遮罩关闭的记忆弹窗
document.getElementById('memory-modal').addEventListener('click', function(e) {
  if (e.target === this) closeMemory();
});
