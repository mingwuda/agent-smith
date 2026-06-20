/* database.js — 数据库连接管理（CRUD + 子 Tab 切换）
   依赖: state.js, util.js(escapeHtml) */

// ---------- 数据库消息提示 ----------

function showDBMsg(msg, type) {
  const el = document.getElementById('db-msg');
  el.textContent = msg;
  el.style.display = msg ? '' : 'none';
  el.style.color = type === 'success' ? '#30d158' : type === 'error' ? '#d70015' : '#999';
}

// ---------- 表单工具 ----------

function toggleDbFields() {
  const type = document.getElementById('db-type').value;
  document.getElementById('db-sqlite-fields').style.display = type === 'sqlite' ? '' : 'none';
  document.getElementById('db-server-fields').style.display = type !== 'sqlite' ? '' : 'none';
}

function getDBFormData() {
  const type = document.getElementById('db-type').value;
  const data = {
    name: document.getElementById('db-name').value.trim(),
    db_type: type,
    readonly: document.getElementById('db-readonly').checked,
  };
  if (type === 'sqlite') {
    data.path = document.getElementById('db-path').value.trim();
  } else {
    data.host = document.getElementById('db-host').value.trim();
    data.port = parseInt(document.getElementById('db-port').value) || 0;
    data.database = document.getElementById('db-database').value.trim();
    data.username = document.getElementById('db-username').value.trim();
    data.password = document.getElementById('db-password').value;
  }
  return data;
}

function clearDBForm() {
  document.getElementById('db-name').value = '';
  document.getElementById('db-path').value = '';
  document.getElementById('db-host').value = '';
  document.getElementById('db-port').value = '';
  document.getElementById('db-database').value = '';
  document.getElementById('db-username').value = '';
  document.getElementById('db-password').value = '';
  document.getElementById('db-readonly').checked = true;
  showDBMsg('', '');
}

function fillDBForm(data) {
  document.getElementById('db-name').value = data.name || '';
  document.getElementById('db-type').value = data.db_type || 'sqlite';
  toggleDbFields();
  if (data.db_type === 'sqlite') {
    document.getElementById('db-path').value = data.path || '';
  } else {
    document.getElementById('db-host').value = data.host || '';
    document.getElementById('db-port').value = data.port || '';
    document.getElementById('db-database').value = data.database || '';
    document.getElementById('db-username').value = data.username || '';
    document.getElementById('db-password').value = data.password || '';
  }
  document.getElementById('db-readonly').checked = data.readonly !== false;
}

// ---------- 连接列表 ----------

async function loadDBConnections() {
  try {
    const res = await fetch('/db/connections');
    if (!res.ok) return;
    const data = await res.json();
    // 获取默认连接
    var defaultConn = '';
    try {
      var defRes = await fetch('/db/default-connection');
      var defData = await defRes.json();
      defaultConn = defData.default_connection || '';
    } catch(e) {}
    const list = document.getElementById('db-list');
    if (!data || data.length === 0) {
      list.innerHTML = '<div style="color:#999;font-size:13px;padding:4px 0 8px;">暂无数据库连接，点击下方添加</div>';
      return;
    }
    list.innerHTML = data.map(function(c, idx) {
      var statusColor = c.enabled ? '#30d158' : '#ff453a';
      var dataAttr = encodeURIComponent(JSON.stringify(c));
      var isDefault = c.name === defaultConn;
      var starIcon = isDefault ? '⭐' : '☆';
      var starTitle = isDefault ? '当前默认连接' : '设为默认';
      var starClick = isDefault ? '' : 'event.stopPropagation();setDefaultConn(\'' + encodeURIComponent(c.name) + '\')';
      return '<div onclick="fillDBFromList(\'' + dataAttr + '\')" style="cursor:pointer;padding:6px 8px;border:1px solid #eee;border-radius:6px;margin-bottom:4px;font-size:13px;display:flex;align-items:center;gap:6px;">' +
        '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:' + statusColor + ';"></span>' +
        '<span style="flex:1;">' + escapeHtml(c.name) + '</span>' +
        '<span style="color:#999;font-size:11px;">' + escapeHtml(c.db_type) + '</span>' +
        '<span onclick="' + starClick + '" title="' + starTitle + '" style="cursor:pointer;font-size:16px;line-height:1;">' + starIcon + '</span>' +
        '</div>';
    }).join('');
  } catch {}
}

function fillDBFromList(encoded) {
  try {
    var data = JSON.parse(decodeURIComponent(encoded));
    fillDBForm(data);
  } catch(e) {}
}

async function setDefaultConn(encodedName) {
  try {
    var name = decodeURIComponent(encodedName);
    var res = await fetch('/db/default-connection', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name }),
    });
    if (res.ok) {
      showDBMsg('已设 "' + name + '" 为默认连接', 'success');
      loadDBConnections();
    } else {
      var err = await res.text();
      showDBMsg('设置失败: ' + err, 'error');
    }
  } catch(e) {
    showDBMsg('设置失败: ' + e.message, 'error');
  }
}

async function testDBConnection() {
  var data = getDBFormData();
  if (!data.name) { showDBMsg('请输入连接名', 'error'); return; }
  showDBMsg('测试中...', '');
  try {
    var res = await fetch('/db/test-connection', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    var result = await res.json();
    if (result.ok) {
      showDBMsg('连接成功！', 'success');
    } else {
      showDBMsg('连接失败: ' + (result.error || '未知错误'), 'error');
    }
  } catch (e) {
    showDBMsg('测试失败: ' + e.message, 'error');
  }
}

async function saveDBConnection() {
  var data = getDBFormData();
  if (!data.name) { showDBMsg('请输入连接名', 'error'); return; }
  showDBMsg('保存中...', '');
  try {
    var res = await fetch('/db/connections', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    if (res.ok) {
      showDBMsg('保存成功！', 'success');
      clearDBForm();
      loadDBConnections();
    } else {
      var err = await res.text();
      showDBMsg('保存失败: ' + err, 'error');
    }
  } catch (e) {
    showDBMsg('保存失败: ' + e.message, 'error');
  }
}

// ═════ 数据库内部二级页签切换 ═════
function switchDBSubTab(tab) {
  document.querySelectorAll('.db-inner-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.db-sub-panel').forEach(p => p.classList.remove('active'));
  if (tab === 'connections') {
    document.getElementById('dbtab-conn').classList.add('active');
    document.getElementById('db-sub-connections').classList.add('active');
  } else {
    document.getElementById('dbtab-perm').classList.add('active');
    document.getElementById('db-sub-permissions').classList.add('active');
    loadVisualPermissions(); // 切换到权限页签时加载
  }
}
