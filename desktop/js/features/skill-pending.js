/* skill-pending.js — Case→Skill 蒸馏候选的半自动审批面板
   依赖: util.js(escapeHtml), i18n.js(t), skills.js(refreshSkills)

   从长期记忆的 Case 累积已达晋升阈值、自动起草到待审批目录的候选技能，
   在这里让用户「批准 / 拒绝」。批准 → 移入生效目录并热加载；拒绝 → 丢弃候选。
 */

async function loadPendingSkills() {
  const section = document.getElementById('skill-pending-section');
  const list = document.getElementById('skill-pending-list');
  if (!section || !list) return;
  try {
    const res = await fetch('/skills/pending');
    if (!res.ok) {
      section.style.display = 'none';
      return;
    }
    const items = await res.json();
    if (!items || items.length === 0) {
      section.style.display = 'none';
      return;
    }
    section.style.display = '';
    list.innerHTML = items.map(item =>
      `<div class="skill-pending-item" data-name="${escapeHtml(item.skill_name)}">
        <span class="spi-name" title="${escapeHtml(item.pending_path || '')}">${escapeHtml(item.skill_name)}</span>
        <span class="spi-occ">×${Number(item.occurrences) || 0} 次</span>
        <button class="spi-btn approve" title="批准并生效">✓</button>
        <button class="spi-btn reject" title="拒绝并删除">✕</button>
      </div>`
    ).join('');
    list.querySelectorAll('.spi-btn').forEach(btn => {
      btn.onclick = () => _actPending(btn);
    });
  } catch {
    section.style.display = 'none';
  }
}

async function _actPending(btn) {
  const item = btn.closest('.skill-pending-item');
  if (!item) return;
  const name = item.dataset.name;
  const approve = btn.classList.contains('approve');
  btn.disabled = true;
  try {
    const res = await fetch(`/skills/pending/${approve ? 'approve' : 'reject'}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ skill_name: name }),
    });
    const data = await res.json().catch(() => ({}));
    const msg = (data && data.message) || (approve ? '已批准' : '已拒绝');
    // 用 toast 轻提示操作结果（临时反馈不污染消息区）
    if (typeof showToast === 'function') showToast(msg, approve ? 'success' : '');
    // 刷新待确认列表与左侧技能列表
    await loadPendingSkills();
    if (typeof refreshSkills === 'function') refreshSkills();
  } catch (e) {
    if (typeof showToast === 'function') showToast('操作失败: ' + String(e), 'error');
  } finally {
    btn.disabled = false;
  }
}