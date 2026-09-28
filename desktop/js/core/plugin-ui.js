/* plugin-ui.js — 前端插件注册框架（依赖: state.js, util.js, i18n.js）
   启动时拉取 /plugin-frontend，按插件的 FRONTEND 清单注册 4 类注入点：
     F  css/js    —— 裸 CSS/JS 全局注入
     A  sidebar    —— 侧边栏底部手风琴区块
     C  settings_tabs —— 设置弹窗自定义 Tab
     E  events     —— 自定义 SSE 事件 → dispatch 给插件注册的 onEvent 回调
*/

const PluginUI = (() => {
  // 已注入的 CSS 去重（按插件 id）
  const _injectedCss = new Set();
  // 已注入的 JS 去重（按插件 id）
  const _injectedJs = new Set();
  // 已注册的手风琴（按 plugin id）
  const _sidebarInserted = new Set();
  // 自定义 SSE 事件名 -> Set(回调)（same plugin 去重按函数引用）
  const _eventHandlers = {};

  function escapeHtml(text) {
    const d = document.createElement('div');
    d.textContent = text == null ? '' : String(text);
    return d.innerHTML;
  }

  // F：注入 CSS（放在 <head> 尾部一个 #plugin-css 容器）
  function injectCss(pluginId, css) {
    if (!css || _injectedCss.has(pluginId)) return;
    if (!Array.isArray(css)) css = [css];
    let container = document.getElementById('plugin-injected-css');
    if (!container) {
      container = document.createElement('style');
      container.id = 'plugin-injected-css';
      document.head.appendChild(container);
    }
    container.appendChild(document.createTextNode(css.join('\n')));
    _injectedCss.add(pluginId);
  }

  // F：注入 JS（创建 <script>，node 方式执行，普通文本）
  function injectJs(pluginId, js) {
    if (!js || _injectedJs.has(pluginId)) return;
    if (!Array.isArray(js)) js = [js];
    const script = document.createElement('script');
    script.textContent = js.join('\n');
    document.body.appendChild(script);
    script.remove?.(); // 执行后移除，避免重复节点
    _injectedJs.add(pluginId);
  }

  // A：侧边栏底部手风琴区块
  function insertSidebar(sidebar, plugin) {
    if (!sidebar || _sidebarInserted.has(plugin.id)) return;
    const sb = document.getElementById('sidebar');
    if (!sb) return;
    const title = escapeHtml(sidebar.title || plugin.name || plugin.id);
    const html = sidebar.html || '';
    const acc = document.createElement('div');
    acc.className = 'sidebar-accordion';
    acc.id = 'plugin-accordion-' + plugin.id;
    acc.innerHTML =
      '<button class="accordion-toggle" type="button" aria-expanded="false" ' +
        'onclick="toggleSidebarAccordion(\'plugin-accordion-' + plugin.id + '\')">' +
        '<span>' + title + '</span><span class="accordion-chevron">▶</span>' +
      '</button>' +
      '<div class="accordion-body">' + html + '</div>';
    sb.appendChild(acc);
    _sidebarInserted.add(plugin.id);
  }

  // C：设置弹窗自定义 Tab
  function insertSettingsTab(tabs, plugin) {
    if (!Array.isArray(tabs) || !tabs.length) return;
    const sidebarNav = document.querySelector('.settings-sidebar');
    const content = document.querySelector('.settings-content');
    if (!sidebarNav || !content) return;
    tabs.forEach(tab => {
      if (!tab || !tab.key) return;
      const tabId = 'panel-plugin-' + plugin.id + '-' + tab.key;
      if (document.getElementById(tabId)) return; // 已注册
      const btn = document.createElement('button');
      btn.className = 'settings-tab';
      btn.dataset.tab = 'plugin-' + plugin.id + '-' + tab.key;
      btn.textContent = tab.icon ? tab.icon + ' ' + (tab.title || tab.key) : (tab.title || tab.key);
      btn.onclick = () => {
        switchSettingsTab('plugin-' + plugin.id + '-' + tab.key);
        // 若有插件注入的 render 回调，切到这个 Tab 时填充内容
        const renderFn = window['__plugin_render_' + plugin.id];
        if (typeof renderFn === 'function') {
          const panel = document.getElementById('panel-plugin-' + plugin.id + '-' + tab.key);
          if (panel) { try { renderFn(panel, plugin); } catch (e) { console.error('[plugin-tab] render', plugin.id, e); } }
        }
      };
      sidebarNav.appendChild(btn);

      const panel = document.createElement('div');
      panel.className = 'settings-tab-panel';
      panel.id = 'panel-plugin-' + plugin.id + '-' + tab.key;
      // Tab 内容由插件的 JS 回调填充（plugin.onSettingsRender），否则放一个空容器
      panel.innerHTML = tab.html || '';
      content.appendChild(panel);
    });
  }

  // E：注册自定义 SSE 事件回调。事件在 handleStreamEvent 的 default 分支被派发过来。
  function onEvent(eventName, cb, pluginId) {
    if (!eventName || typeof cb !== 'function') return;
    (_eventHandlers[eventName] = _eventHandlers[eventName] || new Set()).add(cb);
  }

  // 供 streaming.js 调用：dispatch 一个自定义事件
  function dispatchEvent(eventName, payload) {
    const set = _eventHandlers[eventName];
    if (!set) return;
    set.forEach(cb => {
      try { cb(payload); } catch (e) { console.error('[plugin-event]', eventName, e); }
    });
  }

  // 供插件 JS 拉取其持久化设置
  async function loadState(pluginId) {
    try {
      const res = await fetch('/plugin-state/' + encodeURIComponent(pluginId), { credentials: 'include' });
      return await res.json();
    } catch (e) { return {}; }
  }

  // 供插件 JS 保存其持久化设置
  async function saveState(pluginId, value) {
    try {
      await fetch('/plugin-state/' + encodeURIComponent(pluginId), {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value }),
      });
    } catch (e) { console.error('[plugin-state] save failed', e); }
  }

  // 反注册：移除某插件注入的侧边栏区块与设置 Tab（保存设置后同步用）。
  // ponytail: CSS/JS 一旦注入无法真正"卸载"（JS 已执行、CSS 已进 <style>），
  // 这里只清理 DOM 落点；事件回调按插件 id 无法精确归属（onEvent 未记 pluginId），
  // 故保留。已知上限：取消勾选后插件的 JS 副作用与事件监听仍在，需刷新页面才彻底清除。
  function unregister(pluginId) {
    const acc = document.getElementById('plugin-accordion-' + pluginId);
    if (acc) acc.remove();
    document.querySelectorAll('[id^="panel-plugin-' + pluginId + '-"]').forEach(el => el.remove());
    document.querySelectorAll('.settings-tab[data-tab^="plugin-' + pluginId + '-"]').forEach(el => el.remove());
    _sidebarInserted.delete(pluginId);
  }

  // 按「当前启用的插件集合」同步 DOM 注入点：缺的补、多的删。
  // 保存设置后由 settings.js 调用，使用户取消勾选后侧边栏/设置 Tab 立即消失。
  function sync(enabledIds) {
    const keep = new Set(enabledIds || []);
    _sidebarInserted.forEach(id => { if (!keep.has(id)) unregister(id); });
  }

  // 入口：拉取所有启用插件的 FRONTEND 并注册
  async function init() {
    // 插件注入是 admin 能力（后端 /plugin-frontend 走 _require_admin）。
    // 非 admin 拉取只会拿到 403，且其 UI 注入点（设置 Tab）本就不可见，
    // 因此直接跳过，避免每次页面加载都产生一次无谓的 403 请求。
    if (typeof isAdmin !== 'undefined' && !isAdmin) return;
    let data;
    try {
      const res = await fetch('/plugin-frontend', { credentials: 'include' });
      if (!res.ok) {
        // 403（非 admin）/ 500 等：插件注入是增强能力，失败不影响主流程
        console.warn('[plugin-ui] 拉取前端清单失败 HTTP', res.status);
        return;
      }
      data = await res.json();
    } catch (e) {
      console.error('[plugin-ui] 拉取前端清单失败', e);
      return;
    }
    const plugins = data.plugins || [];
    plugins.forEach(p => {
      const fe = p.frontend || {};
      if (fe.css) injectCss(p.id, fe.css);          // F
      if (fe.js) {
        // JS 需要等注入函数就绪（injectJs 要求 document.body 存在，页面已加载则安全）
        try { injectJs(p.id, fe.js); } catch (e) { console.error('[plugin-ui] js inject', p.id, e); }
      }
      if (fe.sidebar) insertSidebar(fe.sidebar, p);   // A
      if (fe.settings_tabs) insertSettingsTab(fe.settings_tabs, p); // C
    });
    // 插件 JS 注入的代码会在 onload 时通过 PluginUI.onEvent / PluginUI.loadState 自注册
    // 设置 Tab 的渲染回调由插件 JS 在需要时绑定到 window（约定示例请参考 example_plugin 的 FRONTEND["js"]）
    // 保留 handleSettingsRender / handleSidebarRender 钩子：插件 JS 可覆盖以延迟渲染
    if (typeof window.pluginAppReady === 'function') {
      try { window.pluginAppReady(); } catch (e) { console.error('[plugin-ui] pluginAppReady', e); }
    }
  }

  // 暴露给插件 JS / streaming.js 的全局 API
  const api = { init, injectCss, injectJs, onEvent, dispatchEvent, loadState, saveState, unregister, sync };
  window.PluginUI = api;
  return api;
})();

// 自动初始化（页面加载后）
// ponytail: 不能直接在 load 时 init()——isAdmin 由 auth.js 的 loadCurrentUser() 异步赋值，
// 而 main.js 的启动 IIFE 里 await loadCurrentUser() 可能尚未完成，此时读 isAdmin 仍是
// state.js 的默认 false → admin 也会被误判跳过，插件永远不注入。
// 因此这里等 currentUser 就绪（loadCurrentUser 成功/失败都会给它赋值，null 表示未就绪），
// 最多等 ~3s，超时则按当前值判断，绝不无限阻塞页面。
function _initWhenUserReady() {
  if (currentUser !== null) { PluginUI.init(); return; }
  let waited = 0;
  const timer = setInterval(() => {
    waited += 100;
    if (currentUser !== null || waited >= 3000) {
      clearInterval(timer);
      PluginUI.init();
    }
  }, 100);
}

if (document.readyState === 'complete') {
  _initWhenUserReady();
} else {
  window.addEventListener('load', _initWhenUserReady);
}