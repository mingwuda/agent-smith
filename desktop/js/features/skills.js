/* skills.js — 技能列表、详情弹窗、文件预览
   依赖: util.js(escapeHtml, renderMarkdown), i18n.js(t) */

// ── 技能详情弹窗状态 ──
let _skillDetailCurrent = null;   // { name, files: [{path, size, kind}], basePath }

async function openSkillDetail(name) {
  const modal = document.getElementById('skill-detail-modal');
  const titleEl = document.getElementById('skill-detail-title');
  const descEl = document.getElementById('skill-detail-desc');
  const triggersEl = document.getElementById('skill-detail-triggers');
  const filesEl = document.getElementById('skill-detail-files');
  const bodyEl = document.getElementById('skill-detail-body');
  titleEl.textContent = '🧩 ' + name;
  descEl.textContent = t('loading');
  triggersEl.innerHTML = '';
  filesEl.innerHTML = '';
  bodyEl.innerHTML = t('loading');
  modal.classList.add('active');

  try {
    const res = await fetch(`/skills/${encodeURIComponent(name)}`);
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      bodyEl.textContent = (detail.detail || res.statusText) + ' (' + res.status + ')';
      return;
    }
    const data = await res.json();
    descEl.textContent = data.description || t('noDescription');
    if (data.triggers && data.triggers.length) {
      const chips = data.triggers.map(x => `<span class="chip">${escapeHtml(x)}</span>`).join('');
      triggersEl.innerHTML = t('triggersLabel') + chips + `<span class="trigger-toggle">展开全部 ${data.triggers.length} 个</span>`;
      triggersEl.querySelector('.trigger-toggle').onclick = () => {
        const expanded = triggersEl.classList.toggle('expanded');
        triggersEl.querySelector('.trigger-toggle').textContent = expanded ? '收起' : `展开全部 ${data.triggers.length} 个`;
      };
    } else {
      triggersEl.textContent = t('triggersLabel') + t('noTriggers');
    }

    // 渲染文件清单
    const files = data.files || [];
    _skillDetailCurrent = { name, files };

    // 把 SKILL.md 放在最前
    filesEl.innerHTML = '';
    const sorted = files.slice().sort((a, b) => {
      if (a.path.toLowerCase().endsWith('skill.md')) return -1;
      if (b.path.toLowerCase().endsWith('skill.md')) return 1;
      if (a.kind !== b.kind) return a.kind === 'dir' ? -1 : 1;
      return a.path.localeCompare(b.path);
    });
    sorted.forEach(f => {
      const chip = document.createElement('div');
      chip.className = 'skill-file-chip' + (f.kind === 'dir' ? ' dir' : '');
      chip.textContent = f.path;
      chip.dataset.path = f.path;
      if (f.kind !== 'dir' && f.path.toLowerCase().endsWith('skill.md')) {
        chip.classList.add('active');
      }
      if (f.kind !== 'dir') {
        chip.onclick = () => {
          filesEl.querySelectorAll('.skill-file-chip').forEach(c => c.classList.remove('active'));
          chip.classList.add('active');
          _loadSkillFile(name, f.path);
        };
      }
      filesEl.appendChild(chip);
    });

    // 默认显示 SKILL.md
    const skillMd = sorted.find(f => f.path.toLowerCase().endsWith('skill.md') && f.kind === 'file');
    if (skillMd) {
      _loadSkillFile(name, skillMd.path);
    } else if (sorted.length === 0) {
      bodyEl.textContent = '（该技能没有文件）';
    } else {
      bodyEl.textContent = '（无 SKILL.md，请选择上方文件查看）';
    }
  } catch (e) {
    bodyEl.textContent = String(e);
  }
}

function _enhancePreviewScroll(container) {
  // 为超长代码块添加折叠展开按钮
  container.querySelectorAll('pre').forEach(pre => {
    // 只有内容足够高的 pre 才需要折叠按钮（粗略判断：代码行数 > 15）
    const lineCount = (pre.textContent || '').split('\n').length;
    if (lineCount < 15) return;
    const wrapper = document.createElement('div');
    wrapper.style.cssText = 'position:relative;';
    pre.parentNode.insertBefore(wrapper, pre);
    wrapper.appendChild(pre);
    const toggle = document.createElement('button');
    toggle.textContent = '📄 展开全部';
    toggle.style.cssText = 'display:block;width:100%;padding:6px;font-size:12px;background:#f0f0f3;border:1px solid #ddd;border-radius:0 0 8px 8px;cursor:pointer;color:#555;text-align:center;margin-top:-12px;position:relative;z-index:1;';
    wrapper.appendChild(toggle);
    let expanded = false;
    toggle.onclick = () => {
      expanded = !expanded;
      pre.style.maxHeight = expanded ? 'none' : '';
      toggle.textContent = expanded ? '📋 收起' : '📄 展开全部';
    };
  });
}

async function _loadSkillFile(name, path) {
  const bodyEl = document.getElementById('skill-detail-body');
  bodyEl.innerHTML = t('artifactLoading');
  try {
    const res = await fetch(`/skills/${encodeURIComponent(name)}/files?path=${encodeURIComponent(path)}`);
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      bodyEl.textContent = (detail.detail || res.statusText) + ' (' + res.status + ')';
      return;
    }
    const data = await res.json();
    if (data.truncated) {
      bodyEl.innerHTML = `<p style="color:#ff9500;font-size:12px;">⚠️ 文件超过 256KB，仅展示前 256KB</p>` + renderMarkdown(data.content || '');
    } else {
      bodyEl.innerHTML = renderMarkdown(data.content || '');
    }
    _enhancePreviewScroll(bodyEl);
  } catch (e) {
    bodyEl.textContent = String(e);
  }
}

function closeSkillDetail() {
  document.getElementById('skill-detail-modal').classList.remove('active');
}

// 技能项点击：打开详情
document.addEventListener('click', (e) => {
  const item = e.target.closest('.skill-item[data-name]');
  if (item) {
    openSkillDetail(item.dataset.name);
  }
});

// 技能详情弹窗点击遮罩关闭
document.getElementById('skill-detail-modal').addEventListener('click', function(e) {
  if (e.target === this) closeSkillDetail();
});

async function refreshSkills() {
  try {
    const res = await fetch(`/skills`);
    if (res.ok) {
      const data = await res.json();
      const container = document.getElementById('skills-list');
      if (data.length === 0) {
        container.innerHTML = `<div class="skill-item" style="color:#8e8e93;">${escapeHtml(t('noSkills'))}</div>`;
      } else {
        container.innerHTML = data.map(s =>
          `<div class="skill-item" title="${escapeHtml(s.description || t('noDescription'))}" data-name="${escapeHtml(s.name)}" data-description="${escapeHtml(s.description || t('noDescription'))}" data-triggers="${escapeHtml((s.triggers || []).join('、') || t('noTriggers'))}">📌 ${escapeHtml(s.name)}</div>`
        ).join('');
      }
    }
  } catch {}
}

document.addEventListener('mousemove', (event) => {
  const item = event.target.closest('.skill-item[data-name]');
  const tooltip = document.getElementById('skill-tooltip');
  if (!tooltip) return;
  if (!item) {
    tooltip.style.display = 'none';
    return;
  }
  tooltip.innerHTML = `
    <div class="tooltip-title">${item.dataset.name}</div>
    <div>${item.dataset.description || t('noDescription')}</div>
    <div class="tooltip-muted">${t('triggersLabel')}${item.dataset.triggers || t('noTriggers')}</div>
  `;
  tooltip.style.display = 'block';
  const margin = 12;
  const maxX = window.innerWidth - tooltip.offsetWidth - margin;
  const maxY = window.innerHeight - tooltip.offsetHeight - margin;
  tooltip.style.left = `${Math.max(margin, Math.min(event.clientX + margin, maxX))}px`;
  tooltip.style.top = `${Math.max(margin, Math.min(event.clientY + margin, maxY))}px`;
});
