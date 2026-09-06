/* settings.js — 设置弹窗、Provider 管理、快速切换
   依赖: state.js, util.js, i18n.js, messaging.js(addMessage), stats.js(checkHealth, refreshStats) */

// ---------- 设置按钮 ----------
// 注意：settingsBtn 已在 state.js 的顶层 DOM 引用中声明，这里直接复用全局变量
if (settingsBtn) settingsBtn.onclick = openSettings;
if (statusDot) {
  statusDot.onclick = function(e) {
    toggleProviderDropdown(e || window.event);
  };
}
// 新建会话按钮（全局新建入口已从侧边栏移除，仅当元素存在时绑定）
const newSessionBtn = document.getElementById('new-session-btn');
if (newSessionBtn) newSessionBtn.onclick = newSession;

// ---------- Provider 工具函数 ----------

// 底部发送区当前选中的 provider（仅本地状态，随消息作为参数传给后端，不切换全局 active_provider）
var composerProvider = '';

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
  // 按 provider_order 排序（若存在）
  const order = data.provider_order || [];
  const entries = Array.from(select.options);
  entries.sort((a, b) => {
    const ia = order.indexOf(a.value);
    const ib = order.indexOf(b.value);
    if (ia === -1 && ib === -1) return 0;
    if (ia === -1) return 1;
    if (ib === -1) return -1;
    return ia - ib;
  });
  select.innerHTML = '';
  entries.forEach(opt => select.appendChild(opt));
  select.value = data.active_provider || '';
  // 同时填充审核模型下拉框
  const reviewSelect = document.getElementById('s-review-provider');
  var curVal = reviewSelect.value;
  reviewSelect.innerHTML = '<option value="">— 不启用 —</option>';
  const reviewEntries = Object.entries(data.providers || {});
  reviewEntries.sort((a, b) => {
    const ia = order.indexOf(a[0]);
    const ib = order.indexOf(b[0]);
    if (ia === -1 && ib === -1) return 0;
    if (ia === -1) return 1;
    if (ib === -1) return -1;
    return ia - ib;
  });
  reviewEntries.forEach(function(entry) {
    var id = entry[0], provider = entry[1];
    var opt = document.createElement('option');
    opt.value = id;
    opt.textContent = providerLabel(provider, id);
    reviewSelect.appendChild(opt);
  });
  reviewSelect.value = data.review_provider_id || curVal || '';
}

// ── 底部发送区 Provider 选择器 ──
// 模型选择已从头部的"已连接"下拉迁移到输入框底部工具栏。

function refreshProviderSelects(data) {
  settingsData = data;
  // 底部发送区默认选中当前激活 provider（除非用户已另行选择）
  composerProvider = data.active_provider || '';
  const select = document.getElementById('composer-provider-select');
  if (!select) return;
  select.innerHTML = '';
  const active = data.active_provider || '';
  const entries = Object.entries(data.providers || {});

  // 只显示已配置 API Key 的 provider（当前选中项始终显示，避免空列表）
  const filtered = entries.filter(([id, p]) => id === active || (p.model && p.api_key_configured));

  // 按 provider_order 排序（与设置页一致）
  const order = data.provider_order || [];
  filtered.sort((a, b) => {
    const ia = order.indexOf(a[0]);  // a[0] = provider id
    const ib = order.indexOf(b[0]);
    if (ia === -1 && ib === -1) return 0;
    if (ia === -1) return 1;
    if (ib === -1) return -1;
    return ia - ib;
  });

  filtered.forEach(([id, provider]) => {
    const option = document.createElement('option');
    option.value = id;
    option.textContent = providerLabel(provider, id) +
      (id === active && !provider.api_key_configured
        ? ' ' + (currentLanguage === 'en' ? '(no key)' : '(未配置 Key)')
        : '');
    if (id === active) option.selected = true;
    select.appendChild(option);
  });

  // 没有可选项时给出占位提示
  if (!filtered.length) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = t('statusConfiguredMissing');
    select.appendChild(option);
  }

  // 旧版顶部下拉已废弃：如果还存在则清空隐藏
  const dropdown = document.getElementById('header-provider-dropdown');
  if (dropdown) {
    dropdown.innerHTML = '';
    dropdown.style.display = 'none';
  }
  const statusText = document.getElementById('status-text');
  if (statusText) statusText.classList.remove('clickable');
}

function toggleProviderDropdown(event) {
  event.stopPropagation();  const dropdown = document.getElementById('header-provider-dropdown');
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
  try {
    const res = await fetch('/users/me/settings');
    if (!res.ok) return null;
    const data = await res.json();
    refreshProviderSelects(data);
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
  
  // 显示/隐藏删除按钮（只有自定义 provider 才显示）
  const deleteBtn = document.getElementById('delete-provider-btn');
  if (deleteBtn) {
    deleteBtn.style.display = isCustom ? '' : 'none';
  }
  
  // 按 model_order 排序模型列表
  const modelOrder = provider.model_order || [];
  const models = Array.from(provider.models || []);
  models.sort((a, b) => {
    const ia = modelOrder.indexOf(a);
    const ib = modelOrder.indexOf(b);
    if (ia === -1 && ib === -1) return 0;
    if (ia === -1) return 1;
    if (ib === -1) return -1;
    return ia - ib;
  });
  
  const modelSelect = document.getElementById('s-model');
  if (modelSelect) {
    modelSelect.innerHTML = '';
    models.forEach(modelName => {
      const option = document.createElement('option');
      option.value = modelName;
      modelSelect.appendChild(option);
    });
    modelSelect.value = provider.model || '';
  }
  document.getElementById('s-base-url').value = provider.base_url || '';
  document.getElementById('s-api-key').value = '';
  document.getElementById('s-provider-name').value = provider.name || '';
  document.getElementById('s-provider-name-group').classList.toggle('hidden', !isCustom);
  document.getElementById('s-provider-hint').textContent = t('currentProvider', { name: provider.name || providerId });
  document.getElementById('s-api-key-hint').textContent = provider.api_key_configured
    ? t('apiKeySaved', { preview: provider.api_key_preview })
    : t('apiKeyNotSaved');

  // ── 审核模型 ──
  var reviewSelect = document.getElementById('s-review-provider');
  var currentReview = settingsData.review_provider_id || '';
  for (var i = 0; i < reviewSelect.options.length; i++) {
    reviewSelect.options[i].selected = reviewSelect.options[i].value === currentReview;
  }
  document.getElementById('s-review-model').value = settingsData.review_model || '';
  var reviewModelGroup = document.getElementById('s-review-model-group');
  if (reviewModelGroup) {
    reviewModelGroup.style.display = currentReview ? '' : 'none';
  }
  // 填充审核模型候选列表
  var reviewModelOpts = document.getElementById('s-review-model-options');
  reviewModelOpts.innerHTML = '';
  var selProv = settingsData.providers[currentReview];
  if (selProv && selProv.models) {
    const rOrder = selProv.model_order || [];
    const rModels = Array.from(selProv.models);
    rModels.sort((a, b) => {
      const ia = rOrder.indexOf(a);
      const ib = rOrder.indexOf(b);
      if (ia === -1 && ib === -1) return 0;
      if (ia === -1) return 1;
      if (ib === -1) return -1;
      return ia - ib;
    });
    rModels.forEach(function(m) {
      var opt = document.createElement('option');
      opt.value = m;
      reviewModelOpts.appendChild(opt);
    });
  }
}

function onReviewProviderChange() {
  var sel = document.getElementById('s-review-provider');
  var g = document.getElementById('s-review-model-group');
  if (g) g.style.display = sel.value ? '' : 'none';
  // 更新候选列表
  var opts = document.getElementById('s-review-model-options');
  opts.innerHTML = '';
  if (sel.value && settingsData && settingsData.providers) {
    var prov = settingsData.providers[sel.value];
    if (prov && prov.models) {
      prov.models.forEach(function(m) {
        var opt = document.createElement('option');
        opt.value = m;
        opts.appendChild(opt);
      });
    }
    // 默认填入该提供商的 model
    if (prov && prov.model) {
      document.getElementById('s-review-model').value = prov.model;
    }
  }
}

function onProviderChange() {
  renderProviderFields(document.getElementById('s-provider').value);
}

function moveSelectedProvider(delta) {
  if (!settingsData || !settingsData.providers) return;
  const select = document.getElementById('s-provider');
  const providerId = select.value;
  if (!providerId) return;
  const order = settingsData.provider_order ? Array.from(settingsData.provider_order) : Object.keys(settingsData.providers || {});
  const idx = order.indexOf(providerId);
  const newIdx = idx + delta;
  if (newIdx < 0 || newIdx >= order.length) return;
  [order[idx], order[newIdx]] = [order[newIdx], order[idx]];
  settingsData.provider_order = order;
  populateProviderSelect(settingsData);
  select.value = providerId;
  persistOrder();
}

function moveSelectedModel(delta) {
  if (!settingsData || !settingsData.providers) return;
  const providerId = document.getElementById('s-provider').value;
  if (!providerId) return;
  const provider = settingsData.providers[providerId];
  if (!provider) return;
  const modelSelect = document.getElementById('s-model');
  const model = modelSelect ? modelSelect.value : provider.model;
  if (!model) return;
  const models = Array.from(provider.models || []);
  const idx = models.indexOf(model);
  const newIdx = idx + delta;
  if (newIdx < 0 || newIdx >= models.length) return;
  [models[idx], models[newIdx]] = [models[newIdx], models[idx]];
  provider.models = models;
  if (modelSelect) {
    modelSelect.innerHTML = '';
    models.forEach(m => {
      const opt = document.createElement('option');
      opt.value = m;
      modelSelect.appendChild(opt);
    });
    modelSelect.value = model;
  }
  persistOrder();
}

// 将当前 provider_order / model_order 增量写回后端（不依赖"保存设置"按钮）
let _persistOrderPending = false;
async function persistOrder() {
  if (!isAdmin || _persistOrderPending) return;
  if (!settingsData || !settingsData.providers) return;
  _persistOrderPending = true;
  try {
    const body = {
      provider_order: settingsData.provider_order || Object.keys(settingsData.providers || {}),
      model_order: (() => {
        const out = {};
        Object.entries(settingsData.providers).forEach(([pid, p]) => {
          if (p.models && p.models.length) out[pid] = p.models;
        });
        return out;
      })(),
    };
    const res = await fetch('/settings/order', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => '');
      console.error('[persistOrder] 保存排序失败', res.status, detail);
      if (res.status === 403) {
        // 非 admin 无法保存排序，给出明确提示，避免"静默无效"
        if (typeof addMessage === 'function') addMessage(t('orderSaveNeedAdmin') || '排序保存失败：需要以 admin 身份登录', 'system');
      }
    }
  } catch (e) {
    console.error('[persistOrder] 异常', e);
  } finally {
    _persistOrderPending = false;
  }
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

async function deleteProvider() {
  if (!isAdmin || !settingsData) return;
  const select = document.getElementById('s-provider');
  const providerId = select.value;
  if (!providerId) return;
  const provider = settingsData.providers[providerId];
  if (!provider || !provider.is_custom) return;
  
  const confirmMsg = currentLanguage === 'en'
    ? `Delete provider "${provider.name || providerId}"? This cannot be undone.`
    : `确定删除 Provider "${provider.name || providerId}"？此操作不可恢复。`;
  if (!confirm(confirmMsg)) return;
  
  try {
    const res = await fetch(`/settings/provider/${encodeURIComponent(providerId)}`, {
      method: 'DELETE',
    });
    const data = await res.json();
    if (data.status === 'ok') {
      showToast('✅ ' + (currentLanguage === 'en' ? 'Deleted' : '已删除'), 'success');
      // 刷新设置弹窗的下拉列表和状态栏
      const fresh = await loadSettingsForSwitcher();
      if (fresh) {
        populateProviderSelect(fresh);
        renderProviderFields(fresh.active_provider || 'openai');
        checkHealth();
      }
    } else {
      showToast('⚠️ ' + (data.message || (currentLanguage === 'en' ? 'Delete failed' : '删除失败')), 'error');
    }
  } catch {
    showToast('⚠️ ' + (currentLanguage === 'en' ? 'Network error' : '网络错误'), 'error');
  }
}

function openSettings() {
  if (!isAdmin) return;
  const modal = document.getElementById('settings-modal');
  modal.classList.add('active');
  document.getElementById('save-feedback').textContent = '';
  document.getElementById('save-feedback').className = 'save-feedback';
  document.getElementById('save-settings-btn').disabled = false;
  const restartBtn = document.getElementById('restart-backend-btn');
  if (restartBtn) restartBtn.disabled = false;
  
  // 加载当前配置
  fetch('/settings').then(r => r.json()).then(data => {
    settingsData = data;
    populateProviderSelect(data);
    refreshProviderSelects(data);
    renderProviderFields(data.active_provider || 'openai');
    renderParamsFields(data);
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
        enable_loop_guard: document.getElementById('s-enable-loop-guard').checked,
        api_max_retries: settingsData?.api_max_retries ?? 3,
        api_host_ips: settingsData?.api_host_ips || '',
        context_window_tokens: settingsData?.context_window_tokens || 0,
        tavily_search_enabled: document.getElementById('s-tavily-enabled').checked,
        tavily_api_key: document.getElementById('s-tavily-api-key').value,
        tavily_search_url: document.getElementById('s-tavily-search-url').value,
        anysearch_api_key: document.getElementById('s-anysearch-api-key').value,
        review_provider_id: document.getElementById('s-review-provider').value,
        review_model: document.getElementById('s-review-model').value,
        update_server: document.getElementById('s-update-server') ? document.getElementById('s-update-server').value : '',
        llm_idle_timeout_seconds: Number(document.getElementById('s-llm-idle-timeout').value || 60),
        llm_idle_max_retries: Number(document.getElementById('s-llm-idle-retries').value || 2),
        llm_hard_timeout_seconds: Number(document.getElementById('s-llm-hard-timeout').value || 600),
        provider_order: settingsData?.provider_order || Object.keys(settingsData?.providers || {}),
        model_order: (() => {
          const out = {};
          if (!settingsData?.providers) return out;
          Object.entries(settingsData.providers).forEach(([pid, p]) => {
            if (p.models && p.models.length) out[pid] = p.models;
          });
          return out;
        })(),
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

async function restartBackend() {
  if (!isAdmin) return;
  const btn = document.getElementById('restart-backend-btn');
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  showToast(t('restartingBackend'), '');
  try {
    const res = await fetch('/system/restart', { method: 'POST' });
    const data = await res.json();
    if (data.status !== 'ok') {
      throw new Error(data.message || t('restartBackendFailed'));
    }
  } catch (e) {
    showToast('⚠️ ' + (e.message || t('restartBackendFailed')), 'error');
    if (btn) btn.disabled = false;
    return;
  }
  // 后端正在退出，等待其恢复
  let recovered = false;
  for (let i = 0; i < 30; i++) {
    await new Promise(r => setTimeout(r, 1000));
    try {
      const health = await fetch('/health');
      if (health.ok) {
        recovered = true;
        break;
      }
    } catch {
      // 仍在重启中
    }
  }
  if (recovered) {
    showToast('✅ ' + t('restartBackendSuccess'), 'success');
    await checkHealth();
    await loadSettingsForSwitcher();
    closeSettings();
  } else {
    showToast('⚠️ ' + (currentLanguage === 'en' ? 'Backend did not recover in time' : '后端未在预期时间内恢复'), 'error');
  }
  if (btn) btn.disabled = false;
}

async function quickSwitchProvider(providerId) {
  if (!providerId || !settingsData || !settingsData.providers) return;
  const provider = settingsData.providers[providerId];
  if (!provider) return;  if (!provider) return;
  
  showToast(currentLanguage === 'en' ? 'Switching…' : '切换中…');
  try {
    const res = await fetch('/users/me/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        active_provider: providerId,
        provider_name: provider.name || providerId,
        api_key: '',
        model: provider.model || '',
        base_url: provider.base_url || '',
        recursion_limit: settingsData.recursion_limit || 60,
        enable_loop_guard: settingsData.enable_loop_guard !== false,
        api_max_retries: settingsData.api_max_retries ?? 3,
        api_timeout_seconds: settingsData.api_timeout_seconds ?? 120,
        api_host_ips: settingsData.api_host_ips || '',
        context_window_tokens: settingsData.context_window_tokens || 0,
        tavily_search_enabled: !!settingsData.tavily_search_enabled,
        tavily_api_key: '',
        tavily_search_url: settingsData.tavily_search_url || 'https://api.tavily.com/search',
        anysearch_api_key: '',
        review_provider_id: settingsData.review_provider_id || '',
        review_model: settingsData.review_model || '',
      }),
    });
    const data = await res.json();
    if (data.status !== 'ok') {
      showToast('⚠️ ' + (currentLanguage === 'en' ? t('switchProviderFailed') : (data.message || t('switchProviderFailed'))), 'error');
    } else {
      showToast('✅ ' + providerLabel(provider, providerId), 'success');
    }
    await loadSettingsForSwitcher();
    await checkHealth();  // 后台确认（可能返回滞后数据）
    // 状态栏只保留"已连接"，模型名显示已迁移到底部发送区选择器
    var statusText = document.getElementById('status-text');
    if (statusText) statusText.textContent = t('connected');
  } catch {
    showToast('⚠️ ' + t('switchProviderNetworkFailed'), 'error');
  }
}

// ── 更新功能 ──

let lastUpdateCheck = null;

async function renderUpdatePanel() {
  if (!settingsData) return;
  const serverInput = document.getElementById('s-update-server');
  if (serverInput) serverInput.value = settingsData.update_server || '';
  // 打开即检查一次，顺便显示当前版本与可用更新
  await checkForUpdate();
}

async function checkForUpdate() {
  const btn = document.getElementById('check-update-btn');
  const installBtn = document.getElementById('install-update-btn');
  const feedback = document.getElementById('update-feedback');
  const curEl = document.getElementById('s-current-version');
  if (btn) btn.disabled = true;
  if (feedback) { feedback.textContent = currentLanguage === 'en' ? 'Checking…' : '检查中…'; feedback.className = 'save-feedback'; }
  try {
    const res = await fetch('/update/check');
    const data = await res.json();
    lastUpdateCheck = data;
    if (curEl) curEl.textContent = data.current_version || '—';
    if (data.error) {
      if (feedback) { feedback.textContent = '⚠️ ' + data.error; feedback.className = 'save-feedback err'; }
      if (installBtn) installBtn.disabled = true;
      return;
    }
    if (!data.has_update) {
      if (feedback) {
        feedback.textContent = '✅ ' + (currentLanguage === 'en' ? `Up to date (${data.current_version})` : `已是最新版本（${data.current_version}）`);
        feedback.className = 'save-feedback ok';
      }
      if (installBtn) installBtn.disabled = true;
      return;
    }
    const typeText = data.update_type === 'incremental'
      ? (currentLanguage === 'en' ? 'incremental' : '增量')
      : (currentLanguage === 'en' ? 'full' : '全量');
    let msg = `🔄 ${data.current_version} → ${data.latest_version}（${typeText}）`;
    if (data.changelog) msg += '\n' + data.changelog;
    if (feedback) { feedback.textContent = msg; feedback.className = 'save-feedback ok'; }
    if (installBtn) installBtn.disabled = false;
  } catch {
    if (feedback) { feedback.textContent = currentLanguage === 'en' ? 'Network error' : '网络错误'; feedback.className = 'save-feedback err'; }
    if (installBtn) installBtn.disabled = true;
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function installUpdate() {
  if (!lastUpdateCheck || !lastUpdateCheck.has_update) return;
  const installBtn = document.getElementById('install-update-btn');
  const feedback = document.getElementById('update-feedback');
  if (installBtn) installBtn.disabled = true;
  if (feedback) { feedback.textContent = currentLanguage === 'en' ? 'Installing…' : '安装中…'; feedback.className = 'save-feedback'; }
  try {
    const res = await fetch('/update/install', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        patches: lastUpdateCheck.patches || [],
        full_url: lastUpdateCheck.full_url || '',
        target_version: lastUpdateCheck.latest_version || '',
        full_sha256: lastUpdateCheck.full_sha256 || '',
      }),
    });
    const data = await res.json();
    if (data.ok) {
      const needRestart = currentLanguage === 'en'
        ? 'Installed. Please restart the backend to apply.'
        : '安装完成，请重启后端以生效。';
      showToast('✅ ' + needRestart, 'success');
      if (feedback) { feedback.textContent = '✅ ' + needRestart; feedback.className = 'save-feedback ok'; }
      if (installBtn) installBtn.disabled = true;
      lastUpdateCheck = null;
    } else {
      if (feedback) {
        feedback.textContent = '⚠️ ' + (data.error || (currentLanguage === 'en' ? 'Install failed' : '安装失败'));
        feedback.className = 'save-feedback err';
      }
      if (installBtn) installBtn.disabled = false;
    }
  } catch {
    if (feedback) { feedback.textContent = currentLanguage === 'en' ? 'Network error' : '网络错误'; feedback.className = 'save-feedback err'; }
    if (installBtn) installBtn.disabled = false;
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

// 把与模型无关的运行/搜索参数回填到「参数设置」tab 的输入框
function renderParamsFields(data) {
  if (!data) return;

  // ── LLM 超时 ──
  const idle = document.getElementById('s-llm-idle-timeout');
  const retries = document.getElementById('s-llm-idle-retries');
  const hard = document.getElementById('s-llm-hard-timeout');
  if (idle) idle.value = data.llm_idle_timeout_seconds ?? 60;
  if (retries) retries.value = data.llm_idle_max_retries ?? 2;
  if (hard) hard.value = data.llm_hard_timeout_seconds ?? 600;

  // ── 任务控制 ──
  const recursion = document.getElementById('s-recursion-limit');
  const loopGuard = document.getElementById('s-enable-loop-guard');
  if (recursion) recursion.value = data.recursion_limit || 60;
  if (loopGuard) loopGuard.checked = data.enable_loop_guard !== false;

  // ── 搜索 ──
  const tavilyEnabled = document.getElementById('s-tavily-enabled');
  const tavilyKey = document.getElementById('s-tavily-api-key');
  const tavilyUrl = document.getElementById('s-tavily-search-url');
  const tavilyHint = document.getElementById('s-tavily-api-key-hint');
  const anysearchKey = document.getElementById('s-anysearch-api-key');
  const anysearchHint = document.getElementById('s-anysearch-api-key-hint');
  if (tavilyEnabled) tavilyEnabled.checked = !!data.tavily_search_enabled;
  if (tavilyKey) tavilyKey.value = '';
  if (tavilyUrl) tavilyUrl.value = data.tavily_search_url || 'https://api.tavily.com/search';
  if (tavilyHint) {
    tavilyHint.textContent = data.tavily_api_key_configured
      ? t('tavilyApiKeySaved', { preview: data.tavily_api_key_preview })
      : t('tavilyApiKeyNotSaved');
  }
  if (anysearchKey) anysearchKey.value = '';
  if (anysearchHint) {
    anysearchHint.textContent = data.anysearch_api_key_configured
      ? t('anysearchApiKeySaved', { preview: data.anysearch_api_key_preview })
      : t('anysearchApiKeyNotSaved');
  }

  // 联动前端 fetch 总超时：留 30s 余量，且不小于 5 分钟，避免前端比后端硬超时先掐断
  if (typeof setFrontendFetchTimeoutMs === 'function' && data.llm_hard_timeout_seconds) {
    setFrontendFetchTimeoutMs((Number(data.llm_hard_timeout_seconds) + 30) * 1000);
  }
}

// ---------- 设置弹窗 Tab 切换 ----------

function switchSettingsTab(tabId) {
  document.querySelectorAll('.settings-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tabId));
  document.querySelectorAll('.settings-tab-panel').forEach(p => p.classList.toggle('active', p.id === 'panel-' + tabId));
}

// ── 底部发送区模型选择器事件绑定 ──
(function initComposerProviderSelect() {
  const select = document.getElementById('composer-provider-select');
  if (!select) return;
  // 仅本地记录选择，随消息以 provider 参数传给后端；不切换全局 active_provider，也不弹提示
  select.addEventListener('change', function() {
    composerProvider = this.value || '';
  });
})();
