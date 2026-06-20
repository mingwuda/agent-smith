/* stats.js — 连接状态、健康检查、用量统计
   依赖: state.js, i18n.js */

// ---------- 更新状态 ----------

function setStatus(state) {
  statusDot.className = state === 'ok' ? 'online' : state === 'error' ? 'error' : '';
  statusText.textContent = state === 'ok' ? t('connected') : state === 'error' ? t('connectionFailed') : t('connecting');
}

async function checkHealth() {
  try {
    const res = await fetch(`/health`);
    if (res.ok) {
      const data = await res.json();
      const modelName = data.model || t('statusConfiguredMissing');
      const providerName = data.provider_name || data.provider || t('statusModel');
      if (data.status === 'ok') {
        setStatus('ok');
        statusText.textContent = t('connectedWithModel', { provider: providerName, model: modelName });
      } else if (data.error && data.error.includes('credentials')) {
        setStatus('ok');
        statusText.textContent = t('needsApiKey', { provider: providerName, model: modelName });
      } else {
        setStatus('ok');
        statusText.textContent = t('connectedWithModel', { provider: providerName, model: modelName });
      }
      return true;
    }
    setStatus('error');
    return false;
  } catch {
    setStatus('error');
    return false;
  }
}

async function refreshStats() {
  try {
    const res = await fetch(`/usage/session?thread_id=${encodeURIComponent(threadId)}`);
    if (res.ok) {
      const data = await res.json();
      const modelCalls = data.model_calls ?? data.calls ?? 0;
      const toolCalls = data.tool_calls ?? 0;
      document.getElementById('stat-calls').textContent = `${modelCalls} / ${toolCalls}`;
      document.getElementById('stat-total').textContent = data.total_tokens.toLocaleString() + ' tokens';
      document.getElementById('stat-cost').textContent = `$${data.cost.toFixed(6)}`;
    }
  } catch {}
}
