/* main.js — 应用初始化（最后加载）
   依赖: 所有 core/ 和 features/ 模块均已就绪 */

// ---------- 辅助（被 streaming.js send() finally 块引用） ----------

function resetBlockingOverlays() {
  document.querySelectorAll('.modal-overlay.active').forEach(el => el.classList.remove('active'));
  document.getElementById('sidebar-overlay').classList.remove('show');
  document.getElementById('sidebar').classList.remove('open');
}

function setSendButtonRunning(running) {
  console.log('[sendBtn] setRunning:', running);
  sendBtn.disabled = false;
  sendBtn.classList.toggle('stop', running);
  sendBtn.title = running ? t('stopTitle') : '';
  sendBtn.innerHTML = running
    ? '<svg width="16" height="16" viewBox="0 0 16 16"><rect x="3" y="3" width="10" height="10" rx="2" fill="currentColor"/></svg>'
    : '<svg width="18" height="18" viewBox="0 0 18 18"><path d="M2 9l14-7-7 14-2-5-5-2z" fill="currentColor"/></svg>';
}

// ---------- 定时器 ----------

setInterval(() => { checkHealth(); refreshStats(); }, 30000);
setInterval(loadSessions, 60000);

// ---------- 启动入口 ----------

(async () => {
  applyI18n();
  resetBlockingOverlays();
  const ok = await checkHealth();
  await loadCurrentUser();
  await loadSettingsForSwitcher();
  if (ok) {
    // 加载会话列表
    await loadSessions();
    // 如果有历史会话，加载当前高亮会话的消息
    if (sessionsCache.length > 0) {
      const initialSessionId = currentSessionId && sessionsCache.find(s => s.id === currentSessionId)
        ? currentSessionId
        : sessionsCache[0].id;
      await switchSession(initialSessionId, true);
    } else {
      addMessage(t('welcome'), 'bot');
      addMessage(t('welcomeCapabilities'), 'bot');
    }
    refreshSkills();
    refreshStats();
  } else {
    addMessage(t('agentUnavailable'), 'system');
    addMessage('cd agent_core\npython main.py', 'system');
    addMessage(t('refreshAfterStart'), 'system');
  }
})();
