// ── MCP 连接角标与下拉框 ──
// 依赖：util.js (escapeHtml)、i18n.js (t)

function toggleMcpDropdown(event) {
  event.stopPropagation();
  const dd = document.getElementById('mcp-dropdown');
  if (!dd) return;
  const willShow = !dd.classList.contains('show');
  dd.classList.toggle('show');
  if (willShow) {
    fetchMcpStatus(false);
  }
}

function closeMcpDropdown() {
  const dd = document.getElementById('mcp-dropdown');
  if (dd) dd.classList.remove('show');
}

// 点击其他地方关闭下拉框
document.addEventListener('click', function (e) {
  if (!e.target.closest('#mcp-badge') && !e.target.closest('#mcp-dropdown')) {
    closeMcpDropdown();
  }
});

async function fetchMcpStatus(force) {
  try {
    const res = await fetch('/mcp/status');
    if (!res.ok) return;
    const data = await res.json();
    renderMcpStatus(data);
  } catch (err) {
    // 网络错误忽略，不影响主流程
  }
}

function renderMcpStatus(data) {
  const list = document.getElementById('mcp-list');
  const empty = document.getElementById('mcp-empty');
  const badge = document.getElementById('mcp-badge');
  const count = document.getElementById('mcp-badge-count');
  if (!list || !badge || !count) return;

  const servers = data.servers || [];
  const connected = data.connected != null ? data.connected : servers.filter(s => s.status === 'connected').length;

  // 更新角标
  count.textContent = String(connected);
  badge.classList.toggle('has-connected', connected > 0);
  badge.classList.toggle('has-error', servers.some(s => s.status === 'failed'));

  // 渲染列表
  list.innerHTML = '';
  if (!servers.length) {
    if (empty) empty.style.display = 'block';
    return;
  }
  if (empty) empty.style.display = 'none';

  const statusMap = {
    connected: t('mcpConnected'),
    connecting: t('mcpConnecting'),
    failed: t('mcpFailed'),
    skipped: t('mcpSkipped'),
  };

  for (const s of servers) {
    const item = document.createElement('div');
    item.className = 'mcp-item';
    const statusText = statusMap[s.status] || s.status;
    const srcText = s.source === 'project' ? t('mcpSourceProject') : t('mcpSourceGlobal');
    item.innerHTML =
      '<span class="mcp-dot ' + (s.status || 'skipped') + '"></span>' +
      '<span class="mcp-name">' + escapeHtml(s.name) +
        '<span class="mcp-src">' + escapeHtml(srcText) + '</span></span>' +
      '<span class="mcp-meta">' + escapeHtml(statusText) +
        (s.tool_count ? ' · ' + s.tool_count + ' ' + t('mcpTools') : '') + '</span>';
    list.appendChild(item);

    if (s.status === 'failed' && s.error) {
      const err = document.createElement('div');
      err.className = 'mcp-err';
      err.textContent = s.error;
      list.appendChild(err);
    }
  }
}
