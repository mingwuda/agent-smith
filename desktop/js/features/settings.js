/* settings.js — 设置弹窗、Provider 管理、快速切换
   依赖: state.js, util.js, i18n.js, messaging.js(addMessage), stats.js(checkHealth, refreshStats) */

// ---------- 设置按钮 ----------
document.getElementById('settings-btn').onclick = openSettings;
// 新建会话按钮
document.getElementById('new-session-btn').onclick = newSession;

// ---------- Provider 工具函数 ----------

function providerLabel(provider, id) {
  const name = provider.name || id;
  const model = provider.model || t('modelNotConfigured');
  return `${name} · ${model}`;
}

function populateProviderOptions(select, data, includeMissing = false) {
  select.innerHTML = '';
  Object.entries(data.providers || {}).forEach(([id, provider]) => {
    if (!includeMissing && (!provider.model || !provider.api_key_configured)) return;
    const option = document.createElement('option');
    option.value = id;
    option.textContent = providerLabel(provider, id);
    select.appendChild(option);
  });
}

function populateProviderSelect(data) {
  const select = document.getElementById('s-provider');
  populateProviderOptions(select, data, true);
  select.value = data.active_provider || 'openai';
}

// ── 顶部状态栏 Provider 切换下拉菜单 ──

function refreshHeaderProviderDropdown(data) {
  settingsData = data;
  const dropdown = document.getElementById('header-provider-dropdown');
  if (!dropdown) return;
  dropdown.innerHTML = '';
  const active = data.active_provider || 'openai';
  const entries = Object.entries(data.providers || {});
  
  // 只显示已配置 API Key 的 provider（当前选中项始终显示，避免空列表）
  const filtered = entries.filter(([id, p]) => id === active || p.api_key_configured);
  
  // 有可切换项时才让状态栏可点击
  const statusText = document.getElementById('status-text');
  if (statusText) {
    statusText.classList.toggle('clickable', isAdmin && filtered.length > 1);
  }
  
  filtered.forEach(([id, provider]) => {
    const item = document.createElement('div');
    item.className = 'header-dropdown-item' + (id === active ? ' active' : '');
    const label = providerLabel(provider, id);
    item.textContent = id === active && !provider.api_key_configured
      ? label + ' ' + (currentLanguage === 'en' ? '(no key)' : '(未配置 Key)')
      : label;
    item.onclick = function(e) {
      e.stopPropagation();
      dropdown.style.display = 'none';
      quickSwitchProvider(id);
    };
    dropdown.appendChild(item);
  });
}

function toggleProviderDropdown(event) {
  if (!isAdmin) return;
  event.stopPropagation();
  const dropdown = document.getElementById('header-provider-dropdown');
  if (!dropdown || !dropdown.children.length) return;
  const isVisible = dropdown.style.display === 'block';
  // 关闭其他可能的弹出层
  document.getElementById('user-menu').classList.remove('show');
  dropdown.style.display = isVisible ? 'none' : 'block';
}

// 点击页面其他地方关闭 provider 下拉菜单
document.addEventListener('click', function() {
  var dd = document.getElementById('header-provider-dropdown');
  if (dd) dd.style.display = 'none';
});

async function loadSettingsForSwitcher() {
  if (!isAdmin) return null;
  try {
    const res = await fetch('/settings');
    if (!res.ok) return null;
    const data = await res.json();
    refreshHeaderProviderDropdown(data);
    return data;
  } catch {
    return null;
  }
}

// ---------- 设置弹窗操作 ----------

function renderProviderFields(providerId) {
  if (!settingsData || !settingsData.providers) return;
  const provider = settingsData.providers[providerId] || {};
  const isCustom = !!provider.is_custom;
  const modelOptions = document.getElementById('s-model-options');
  modelOptions.innerHTML = '';
  (provider.models || []).forEach(modelName => {
    const option = document.createElement('option');
    option.value = modelName;
    modelOptions.appendChild(option);
  });
  
  document.getElementById('s-model').value = provider.model || '';
  document.getElementById('s-base-url').value = provider.base_url || '';
  document.getElementById('s-recursion-limit').value = settingsData.recursion_limit || 60;
  document.getElementById('s-api-timeout').value = settingsData.api_timeout_seconds || 120;
  document.getElementById('s-tavily-enabled').checked = !!settingsData.tavily_search_enabled;
  document.getElementById('s-tavily-api-key').value = '';
  document.getElementById('s-tavily-search-url').value = settingsData.tavily_search_url || 'https://api.tavily.com/search';
  document.getElementById('s-api-key').value = '';
  document.getElementById('s-provider-name').value = provider.name || '';
  document.getElementById('s-provider-name-group').classList.toggle('hidden', !isCustom);
  document.getElementById('s-provider-hint').textContent = t('currentProvider', { name: provider.name || providerId });
  document.getElementById('s-api-key-hint').textContent = provider.api_key_configured
    ? t('apiKeySaved', { preview: provider.api_key_preview })
    : t('apiKeyNotSaved');
  document.getElementById('s-tavily-api-key-hint').textContent = settingsData.tavily_api_key_configured
    ? t('tavilyApiKeySaved', { preview: settingsData.tavily_api_key_preview })
    : t('tavilyApiKeyNotSaved');
  document.getElementById('s-anysearch-api-key').value = '';
  document.getElementById('s-anysearch-api-key-hint').textContent = settingsData.anysearch_api_key_configured
    ? t('anysearchApiKeySaved', { preview: settingsData.anysearch_api_key_preview })
    : t('anysearchApiKeyNotSaved');
}

function onProviderChange() {
  renderProviderFields(document.getElementById('s-provider').value);
}

function addCustomProvider() {
  if (!settingsData) settingsData = { providers: {} };
  const id = `custom_${Date.now()}`;
  settingsData.providers[id] = {
    name: t('customProviderName'),
    is_custom: true,
    api_key_configured: false,
    api_key_preview: currentLanguage === 'en' ? 'Not set' : '未设置',
    model: '',
    base_url: '',
    models: [],
  };
  settingsData.active_provider = id;
  populateProviderSelect(settingsData);
  renderProviderFields(id);
  document.getElementById('s-provider-name').focus();
  document.getElementById('s-provider-name').select();
}

function openSettings() {
  if (!isAdmin) return;
  const modal = document.getElementById('settings-modal');
  modal.classList.add('active');
  document.getElementById('save-feedback').textContent = '';
  document.getElementById('save-feedback').className = 'save-feedback';
  document.getElementById('save-settings-btn').disabled = false;
  
  // 加载当前配置
  fetch('/settings').then(r => r.json()).then(data => {
    settingsData = data;
    populateProviderSelect(data);
    refreshHeaderProviderDropdown(data);
    renderProviderFields(data.active_provider || 'openai');
  }).catch(() => {});
}

function closeSettings() {
  document.getElementById('settings-modal').classList.remove('active');
}

async function saveSettings() {
  if (!isAdmin) return;
  const btn = document.getElementById('save-settings-btn');
  const feedback = document.getElementById('save-feedback');
  btn.disabled = true;
  feedback.textContent = t('saving');
  feedback.className = 'save-feedback';
  
  try {
    const res = await fetch('/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        active_provider: document.getElementById('s-provider').value,
        provider_name: document.getElementById('s-provider-name').value,
        api_key: document.getElementById('s-api-key').value,
        model: document.getElementById('s-model').value,
        base_url: document.getElementById('s-base-url').value,
        recursion_limit: Number(document.getElementById('s-recursion-limit').value || 60),
        api_max_retries: settingsData?.api_max_retries ?? 3,
        api_timeout_seconds: Number(document.getElementById('s-api-timeout').value || 120),
        api_host_ips: settingsData?.api_host_ips || '',
        context_window_tokens: settingsData?.context_window_tokens || 0,
        tavily_search_enabled: document.getElementById('s-tavily-enabled').checked,
        tavily_api_key: document.getElementById('s-tavily-api-key').value,
        tavily_search_url: document.getElementById('s-tavily-search-url').value,
        anysearch_api_key: document.getElementById('s-anysearch-api-key').value,
      }),
    });
    const data = await res.json();
    if (data.status === 'ok') {
      feedback.textContent = '✅ ' + (currentLanguage === 'en' ? t('settingsSaved') : (data.message || t('settingsSaved')));
      feedback.className = 'save-feedback ok';
      // 清空密码框
      document.getElementById('s-api-key').value = '';
      document.getElementById('s-tavily-api-key').value = '';
      document.getElementById('s-anysearch-api-key').value = '';
      // 刷新状态
      setTimeout(async () => {
        closeSettings();
        await loadSettingsForSwitcher();
        checkHealth();
        refreshStats();
      }, 1000);
    } else {
      feedback.textContent = '⚠️ ' + (currentLanguage === 'en' ? t('saveFailed') : (data.message || t('saveFailed')));
      feedback.className = 'save-feedback err';
      btn.disabled = false;
    }
  } catch (e) {
    feedback.textContent = t('networkSettingsError');
    feedback.className = 'save-feedback err';
    btn.disabled = false;
  }
}

async function quickSwitchProvider(providerId) {
  if (!isAdmin || !providerId || !settingsData || !settingsData.providers) return;
  const provider = settingsData.providers[providerId];
  if (!provider) return;
  
  showToast(currentLanguage === 'en' ? 'Switching…' : '切换中…');
  try {
    const res = await fetch('/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        active_provider: providerId,
        provider_name: provider.name || providerId,
        api_key: '',
        model: provider.model || '',
        base_url: provider.base_url || '',
        recursion_limit: settingsData.recursion_limit || 60,
        api_max_retries: settingsData.api_max_retries ?? 3,
        api_timeout_seconds: settingsData.api_timeout_seconds ?? 120,
        api_host_ips: settingsData.api_host_ips || '',
        context_window_tokens: settingsData.context_window_tokens || 0,
        tavily_search_enabled: !!settingsData.tavily_search_enabled,
        tavily_api_key: '',
        tavily_search_url: settingsData.tavily_search_url || 'https://api.tavily.com/search',
        anysearch_api_key: '',
      }),
    });
    const data = await res.json();
    if (data.status !== 'ok') {
      showToast('⚠️ ' + (currentLanguage === 'en' ? t('switchProviderFailed') : (data.message || t('switchProviderFailed'))), 'error');
    } else {
      showToast('✅ ' + providerLabel(provider, providerId), 'success');
      // 立即更新顶部状态文本，不用等 health check（可能滞后）
      var statusText = document.getElementById('status-text');
      if (statusText) {
        statusText.textContent = t('connectedWithModel', {
          provider: provider.name || providerId,
          model: provider.model || t('statusConfiguredMissing'),
        });
      }
    }
    await loadSettingsForSwitcher();
    await checkHealth();  // 后台确认，不一致时会被修正
  } catch {
    showToast('⚠️ ' + t('switchProviderNetworkFailed'), 'error');
  }
}

// ── Toast 通知 ──

function showToast(message, type) {
  // 复用或创建 toast 容器
  var container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    document.body.appendChild(container);
  }
  var el = document.createElement('div');
  el.className = 'toast ' + (type || '');
  el.textContent = message;
  container.appendChild(el);
  // 触发入场动画
  requestAnimationFrame(function() { el.classList.add('show'); });
  // 自动消失
  setTimeout(function() {
    el.classList.remove('show');
    setTimeout(function() { if (el.parentNode) el.parentNode.removeChild(el); }, 300);
  }, 2500);
}

// ---------- 设置弹窗 Tab 切换 ----------

function switchSettingsTab(tabId) {
  document.querySelectorAll('.settings-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tabId));
  document.querySelectorAll('.settings-tab-panel').forEach(p => p.classList.toggle('active', p.id === 'panel-' + tabId));
}
