/* workbench-terminal.js — 终端 tab：vendored xterm UMD + /ws/terminal PTY
   与 agnes-harness 不同，这里不做「快照轮询」而是 WebSocket 直推字节流：
   后端已有 pty，前端只负责渲染/输入/尺寸，少一层状态同步。
*/
(function () {
  var term = null, addon = null, ws = null, ensured = false, observer = null;

  function el(id) { return document.getElementById(id); }
  function setStatus(text, kind) {
    var s = el('wb-term-status');
    if (!s) return;
    s.textContent = text;
    s.className = 'wb-term-status' + (kind ? ' ' + kind : '');
  }
  function send(msg) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(msg));
    }
  }
  function fit() {
    if (!addon || !term) return;
    var host = el('wb-term');
    if (!host || !host.clientWidth || !host.clientHeight) return;
    try { addon.fit(); } catch (e) {}
  }

  function url() {
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var pid = window.Workbench ? Workbench.projectId() : '';
    return proto + '//' + location.host + '/ws/terminal?project_id=' + encodeURIComponent(pid);
  }

  function connect() {
    if (ws) { try { ws.close(); } catch (e) {} ws = null; }
    setStatus(t('wbTermConnecting'));
    try {
      ws = new WebSocket(url());
    } catch (e) {
      setStatus(t('wbTermConnFailed'), 'error');
      return;
    }
    ws.onopen = function () {
      setStatus(t('wbTermConnected'), 'connected');
      if (window.Workbench) Workbench.setTerminalCount(1);
      if (term) {
        term.focus();
        setTimeout(function () {
          fit();
          send({ type: 'resize', cols: term.cols, rows: term.rows });
        }, 30);
      }
    };
    ws.onmessage = function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === 'ready') {
        var cwd = el('wb-term-cwd');
        if (cwd) cwd.textContent = msg.cwd || msg.shell || '';
      } else if (msg.type === 'output') {
        if (term) term.write(msg.data || '');
      } else if (msg.type === 'exit') {
        if (term) term.write('\r\n\x1b[90m[进程已退出，点 ＋ 新建终端]\x1b[0m\r\n');
        setStatus(t('wbTermExited'));
        if (window.Workbench) Workbench.setTerminalCount(0);
      } else if (msg.type === 'error') {
        if (term) term.write('\r\n\x1b[31m' + (msg.data || t('wbTermError')) + '\x1b[0m\r\n');
        setStatus(t('wbTermError'), 'error');
      }
    };
    ws.onclose = function () {
      if (window.Workbench) Workbench.setTerminalCount(0);
      if (term) term.write('\r\n\x1b[90m[' + t('wbTermDisconnected') + ']\x1b[0m\r\n');
      setStatus(t('wbTermDisconnected'), 'error');
    };
    ws.onerror = function () { setStatus(t('wbTermConnFailed'), 'error'); };
  }

  function ensure() {
    if (ensured) { setTimeout(fit, 0); return; }
    if (typeof Terminal === 'undefined') {
      setStatus(t('wbTermNoLib'), 'error');
      return;
    }
    ensured = true;
    var host = el('wb-term');
    term = new Terminal({
      cursorBlink: true,
      fontSize: 13,
      fontFamily: '"SF Mono", Menlo, Consolas, monospace',
      scrollback: 5000,
      allowProposedApi: true,
      theme: {
        background: '#121214', foreground: '#e8e8ed', cursor: '#0a84ff',
        selectionBackground: '#0a84ff55',
        black: '#1c1c1e', red: '#ff6b6b', green: '#4ade80', yellow: '#ffd60a',
        blue: '#64a0ff', magenta: '#bf5af2', cyan: '#5ac8fa', white: '#d1d1d6',
        brightBlack: '#6e6e73', brightWhite: '#ffffff',
      },
    });
    // addon-fit 的 UMD 包导出的是 { FitAddon: Class }，兼容直接导出的情况
    var FitCtor = (typeof FitAddon !== 'undefined') ? (FitAddon.FitAddon || FitAddon) : null;
    if (FitCtor) { addon = new FitCtor(); term.loadAddon(addon); }
    term.open(host);
    term.onData(function (data) { send({ type: 'input', data: data }); });
    term.onResize(function (size) { send({ type: 'resize', cols: size.cols, rows: size.rows }); });
    if (typeof ResizeObserver !== 'undefined') {
      observer = new ResizeObserver(function () { fit(); });
      observer.observe(host);
    }
    setTimeout(fit, 0);
    connect();
  }

  window.WorkbenchTerminal = {
    ensure: ensure,
    fit: fit,
    clear: function () { if (term) { term.clear(); term.focus(); } },
    restart: function () {
      if (!ensured) return;   // 用户还没打开过终端，不擅自建连
      if (term) term.reset();
      connect();
    },
  };
})();
