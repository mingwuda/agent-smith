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
  // 打断注入按钮只在执行中显示（空闲隐藏）
  const steerBtn = document.getElementById('steer-btn');
  if (steerBtn) {
    steerBtn.style.display = running ? '' : 'none';
    if (!running) steerBtn.disabled = false;
  }
}

// ---------- 定时器 ----------

setInterval(() => { checkHealth(); refreshStats(); }, 30000);
setInterval(loadSessions, 60000);

// ---------- 启动入口 ----------

(async () => {
  applyI18n();
  resetBlockingOverlays();
  // 初始化悬浮「滚动到底部」按钮
  if (typeof initScrollToBottomBtn === 'function') initScrollToBottomBtn();
  // 初始化前端 fetch 超时（从后端读取硬超时配置）
  if (typeof initFrontendFetchTimeout === 'function') await initFrontendFetchTimeout();
  const ok = await checkHealth();
  await loadCurrentUser();
  await loadSettingsForSwitcher();
  if (ok) {
    // 加载会话列表
    await loadSessions();
    // 如果有历史会话，加载当前高亮会话的消息
    if (sessionsCache.length > 0) {
      // 先尝试还原上次刷新前的多页签打开状态
      const restored = (window.ChatTabs && window.ChatTabs.restore) ? window.ChatTabs.restore() : null;
      if (restored && restored.tabs && restored.tabs.length) {
        // 重新打开所有页签（逐个 switchSession 走完整加载），激活项放到最后处理保证其为最终可见页签
        const ordered = restored.tabs.slice().sort(function (a, b) {
          const aAct = a.key === restored.active ? 1 : 0;
          const bAct = b.key === restored.active ? 1 : 0;
          return aAct - bAct;
        });
        let first = true;
        for (const tab of ordered) {
          const s = window.ChatTabs.splitKey(tab.key);
          // 首个还原页签用 forceLoad；后续页签首次打开正常加载（保留该页签滚动/流式画面）
          await switchSession(s.id, s.source, first ? true : false);
          first = false;
        }
      } else {
        const initialSession = currentSessionId && sessionsCache.find(s => s.id === currentSessionId)
          ? { id: currentSessionId, source: (sessionsCache.find(s => s.id === currentSessionId) || {}).source || 'web' }
          : sessionsCache[0];
        await switchSession(initialSession.id, initialSession.source, true);
      }
    } else {
      addMessage(t('welcome'), 'bot');
      addMessage(t('welcomeCapabilities'), 'bot');
    }
    refreshSkills();
    refreshStats();
    // 加载 Case→Skill 蒸馏候选（待确认技能）
    if (typeof loadPendingSkills === 'function') loadPendingSkills();
  } else {
    addMessage(t('agentUnavailable'), 'system');
    addMessage('cd agent_core\npython main.py', 'system');
    addMessage(t('refreshAfterStart'), 'system');
  }
})();
