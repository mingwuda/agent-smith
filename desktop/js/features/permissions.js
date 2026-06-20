/* permissions.js — 数据库权限可视化管理（实体/规则 CRUD）
   依赖: state.js, util.js(escHtml, escAttr, escapeId, escapeHtml) */

// ═════ 权限可视化管理 ═════
let _permData = { roles: {}, users: {}, global_defaults: {} };

async function loadVisualPermissions() {
  try {
    const resp = await fetch('/db/permissions');
    const data = await resp.json();
    _permData = data;
    // 全局默认值
    const gd = data.global_defaults || {};
    document.getElementById('g-max-rows').value = gd.max_query_rows || 500;
    document.getElementById('g-default-readonly').checked = gd.default_readonly !== false;
    document.getElementById('g-dangerous-sql').checked = !!gd.allow_dangerous_sql;
    renderPermEntities();
  } catch (e) {
    console.error('加载权限配置失败:', e);
  }
}

function renderPermEntities() {
  // 渲染角色列表
  const rolesList = document.getElementById('perm-roles-list');
  const roleNames = Object.keys(_permData.roles || {});
  if (roleNames.length === 0) {
    rolesList.innerHTML = '<p class="perm-placeholder">暂无角色权限，点击上方按钮添加</p>';
  } else {
    let html = '';
    for (const name of roleNames) {
      html += buildEntityCard(name, 'role', _permData.roles[name]);
    }
    rolesList.innerHTML = html;
  }

  // 渲染用户列表
  const usersList = document.getElementById('perm-users-list');
  const userNames = Object.keys(_permData.users || {});
  if (userNames.length === 0) {
    usersList.innerHTML = '<p class="perm-placeholder">暂无用户白名单，点击上方按钮添加</p>';
  } else {
    let html = '';
    for (const name of userNames) {
      html += buildEntityCard(name, 'user', _permData.users[name]);
    }
    usersList.innerHTML = html;
  }
}

function buildEntityCard(name, type, entityData) {
  const badgeClass = type === 'role' ? 'perm-type-role' : 'perm-type-user';
  const badgeText = type === 'role' ? '角色' : '用户';
  const icon = type === 'role' ? '👥' : '🧑';
  const rules = entityData.databases || {};
  const dbNames = Object.keys(rules);

  // 用户白名单：显示角色选择器和覆盖规则
  let roleSelectorHtml = '';
  if (type === 'user') {
    const currentRole = entityData.role || '';
    const roleNames = Object.keys(_permData.roles || {});
    let roleOptions = '<option value="">无角色（独立权限）</option>';
    for (const rn of roleNames) {
      const sel = rn === currentRole ? 'selected' : '';
      roleOptions += `<option value="${escAttr(rn)}" ${sel}>${escHtml(rn)}</option>`;
    }
    roleSelectorHtml = `
      <div class="perm-rule-field perm-rule-field-full" style="margin-bottom:10px;">
        <label>绑定角色（继承角色权限）</label>
        <select id="user-role-sel-${escapeId(name)}" onchange="onUserRoleChange('${escAttr(name)}', this.value)" style="padding:6px 10px;border:1px solid #ddd;border-radius:6px;font-size:12px;width:100%;background:#fff;">
          ${roleOptions}
        </select>
        <div style="font-size:10px;color:#888;margin-top:3px;">选择角色后，用户自动继承该角色的表权限；下方规则可覆盖或补充角色规则。</div>
      </div>`;
  }

  // 构建每个数据库的规则HTML
  let rulesHtml = '';
  if (dbNames.length > 0) {
    for (const dbName of dbNames) {
      const tableRules = rules[dbName] || [];
      for (let i = 0; i < tableRules.length; i++) {
        const r = tableRules[i];
        rulesHtml += buildRuleCard(dbName, r, i, type, name);
      }
    }
  } else {
    rulesHtml = '<p style="font-size:12px;color:#aaa;padding:8px 0;">暂无规则，点击下方添加</p>';
  }

  return `<div class="perm-entity-card" id="entity-${type}-${escapeId(name)}">
    <div class="perm-entity-card-header" onclick="togglePermEntity('${type}','${escAttr(name)}')">
      <span class="perm-entity-name">${icon} ${escHtml(name)}
        <span class="perm-entity-type-badge ${badgeClass}">${badgeText}</span>
      </span>
      <div class="perm-entity-actions" onclick="event.stopPropagation()">
        <button class="perm-act-btn" onclick="showAddRuleForm('${type}','${escAttr(name)}')">+ 规则</button>
        <button class="perm-act-btn danger" onclick="deletePermEntity('${type}','${escAttr(name)}')">删除</button>
      </div>
    </div>
    <div class="perm-entity-body" id="body-${type}-${escapeId(name)}">
      ${roleSelectorHtml}
      <div class="perm-rules-header">
        <span class="perm-rules-label">${dbNames.length} 个数据库的规则</span>
        <button class="perm-rule-add-btn" onclick="showAddRuleForm('${type}','${escAttr(name)}')">+ 添加规则</button>
      </div>
      ${rulesHtml}
      <!-- 内联添加规则表单（默认隐藏）-->
      <div class="perm-rule-card" id="addrule-form-${type}-${escapeId(name)}" style="display:none;">
        ${buildRuleForm(type, name, '', null)}
      </div>
    </div>
  </div>`;
}

function onUserRoleChange(userName, roleValue) {
  if (!_permData.users[userName]) return;
  _permData.users[userName].role = roleValue;
}

function buildRuleCard(dbName, rule, idx, type, name) {
  return `<div class="perm-rule-card" data-db="${escAttr(dbName)}" data-idx="${idx}">
    <div class="perm-rule-card-actions" style="position:absolute;top:6px;right:6px;display:flex;gap:4px;">
      <button class="perm-rule-edit-btn" onclick="editRule('${type}','${escAttr(name)}','${escAttr(dbName)}',${idx})" title="编辑规则">✏️</button>
      <button class="perm-rule-delete" onclick="deleteRule('${type}','${escAttr(name)}','${escAttr(dbName)}',${idx})" title="删除规则">×</button>
    </div>
    <div class="perm-rule-fields">
      <div class="perm-rule-field"><label>数据库连接</label><input value="${escHtml(dbName)}" disabled /></div>
      <div class="perm-rule-field"><label>表名</label><input value="${escHtml(rule.table || '')}" disabled /></div>
      <div class="perm-rule-field perm-rule-field-full"><label>允许的列</label><input value="${escHtml((rule.columns_allow||[]).join(', '))}" disabled /></div>
      <div class="perm-rule-field perm-rule-field-full"><label>行级过滤条件</label><input value="${escHtml(rule.row_filter || '')}" disabled /></div>
      <div class="perm-rule-field"><label>允许写操作</label><input value="${!!rule.allow_write ? '是' : '否'}" disabled /></div>
      <div class="perm-rule-field"><label>最大行数</label><input value="${rule.max_rows||1000}" disabled /></div>
    </div>
  </div>`;
}

function editRule(type, name, dbName, idx) {
  const key = type === 'user' ? 'users' : 'roles';
  const entity = _permData[key] && _permData[key][name];
  if (!entity) return;
  const rules = (entity.databases[dbName] || []);
  const rule = rules[idx];
  if (!rule) return;

  // 替换卡片内容为编辑表单
  const card = document.querySelector(
    `#entity-${type}-${escapeId(name)} .perm-rule-card[data-db="${escAttr(dbName)}"][data-idx="${idx}"]`
  );
  if (!card) return;

  const colsStr = (rule.columns_allow||[]).join(',');
  card.innerHTML = `
    <div class="perm-rule-fields" style="position:relative;">
      <button class="perm-rule-delete" onclick="cancelRuleEdit('${type}','${escAttr(name)}','${escAttr(dbName)}',${idx})" title="取消编辑" style="position:absolute;top:0;right:0;">×</button>
      <div class="perm-rule-field"><label>数据库连接</label>
        <input id="erf-db-${type}-${escapeId(name)}-${idx}" value="${escHtml(dbName)}" disabled style="background:#f5f5f5;" /></div>
      <div class="perm-rule-field"><label>表名 *（按住 Ctrl 多选）</label>
        <select id="erf-table-${type}-${escapeId(name)}-${idx}" multiple onchange="onEditRuleTableChange('${type}','${escAttr(name)}','${escAttr(dbName)}',${idx})" style="padding:6px 10px;border:1px solid #ddd;border-radius:6px;font-size:12px;width:100%;min-height:80px;background:#fff;">
        </select></div>
      <div class="perm-rule-field perm-rule-field-full"><label>允许列（勾选或逗号分隔，空=全部）</label>
        <div id="erf-cols-checkboxes-${type}-${escapeId(name)}-${idx}" style="max-height:120px;overflow-y:auto;border:1px solid #eee;border-radius:6px;padding:6px 8px;margin-bottom:4px;display:none;"></div>
        <input id="erf-cols-${type}-${escapeId(name)}-${idx}" value="${escHtml(colsStr)}" placeholder="id,amount,status" style="margin-top:4px;" oninput="onEditRuleColsInput('${type}','${escAttr(name)}','${escAttr(dbName)}',${idx})" /></div>
      <div class="perm-rule-field perm-rule-field-full"><label>行级过滤（Jinja2模板）</label>
        <input id="erf-filter-${type}-${escapeId(name)}-${idx}" value="${escHtml(rule.row_filter||'')}" placeholder='dept_id={{user.dept_id}}' /></div>
      <div class="perm-rule-field"><label>允许写入</label>
        <select id="erf-write-${type}-${escapeId(name)}-${idx}"><option value="false" ${!rule.allow_write?'selected':''}>否</option><option value="true" ${!!rule.allow_write?'selected':''}>是</option></select></div>
      <div class="perm-rule-field"><label>最大返回行数</label>
        <input type="number" id="erf-maxrows-${type}-${escapeId(name)}-${idx}" value="${rule.max_rows||500}" min="10" max="50000" /></div>
      <div class="perm-rule-field perm-rule-field-full" style="margin-top:6px;display:flex;gap:8px;">
        <button onclick="saveRuleEdit('${type}','${escAttr(name)}','${escAttr(dbName)}',${idx})" style="padding:5px 14px;border:1px solid #007aff;border-radius:6px;background:#007aff;color:#fff;cursor:pointer;font-size:12px;">💾 保存</button>
        <button onclick="cancelRuleEdit('${type}','${escAttr(name)}','${escAttr(dbName)}',${idx})" style="padding:5px 14px;border:1px solid #ddd;border-radius:6px;background:#fff;cursor:pointer;font-size:12px;">取消</button>
      </div>
    </div>`;
  card.classList.add('editing');

  // 加载该连接的表列表
  loadTablesForSelect(`erf-table-${type}-${escapeId(name)}-${idx}`, dbName, rule.table || '');
  // 加载列勾选框
  if (rule.table) {
    loadColumnCheckboxes(
      `erf-cols-checkboxes-${type}-${escapeId(name)}-${idx}`,
      `erf-cols-${type}-${escapeId(name)}-${idx}`,
      dbName, rule.table, rule.columns_allow || []
    );
  }
}

function saveRuleEdit(type, name, dbName, idx) {
  const key = type === 'user' ? 'users' : 'roles';
  const rules = (_permData[key][name] && _permData[key][name].databases[dbName]) || [];
  if (!rules[idx]) return;

  const sel = document.getElementById(`erf-table-${type}-${escapeId(name)}-${idx}`);
  const selected = Array.from(sel.selectedOptions).map(o => o.value).filter(v => v);
  const tableNames = selected.join(', ');
  const colsStr = document.getElementById(`erf-cols-${type}-${escapeId(name)}-${idx}`).value.trim();
  const rowFilter = document.getElementById(`erf-filter-${type}-${escapeId(name)}-${idx}`).value.trim();
  const allowWrite = document.getElementById(`erf-write-${type}-${escapeId(name)}-${idx}`).value === 'true';
  const maxRows = parseInt(document.getElementById(`erf-maxrows-${type}-${escapeId(name)}-${idx}`).value) || 500;

  if (!tableNames) { alert('请选择表名'); return; }
  const cols = colsStr ? colsStr.split(',').map(c=>c.trim()).filter(c=>c) : [];

  rules[idx] = {
    table: tableNames,
    columns_allow: cols,
    row_filter: rowFilter,
    allow_write: allowWrite,
    max_rows: maxRows
  };
  renderPermEntities();
}

function cancelRuleEdit(type, name, dbName, idx) {
  // 直接重新渲染恢复原样
  renderPermEntities();
}

function deleteRule(type, name, dbName, idx) {
  if (!confirm('确定要删除此规则吗？')) return;
  const key = type === 'user' ? 'users' : 'roles';
  const entity = _permData[key] && _permData[key][name];
  if (!entity) return;
  const rules = entity.databases[dbName] || [];
  rules.splice(idx, 1);
  if (rules.length === 0) {
    delete entity.databases[dbName];
  }
  renderPermEntities();
}

function buildRuleForm(type, name, dbName, existingRule) {
  const r = existingRule || {};
  const colsStr = (r.columns_allow||[]).join(',');
  return `<div class="perm-rule-fields">
    <div class="perm-rule-field"><label>数据库连接 *</label>
      <select id="rf-db-${type}-${escapeId(name)}" onchange="onAddRuleDbChange('${type}','${escAttr(name)}')">
        <option value="">选择数据库...</option></select>
    </div>
    <div class="perm-rule-field"><label>表名 *（按住 Ctrl 多选）</label>
      <select id="rf-table-${type}-${escapeId(name)}" multiple onchange="onAddRuleTableChange('${type}','${escAttr(name)}')" style="padding:6px 10px;border:1px solid #ddd;border-radius:6px;font-size:12px;width:100%;min-height:80px;background:#fff;">
      </select>
    </div>
    <div class="perm-rule-field perm-rule-field-full"><label>允许列（勾选或逗号分隔，空=全部允许）</label>
      <div id="rf-cols-checkboxes-${type}-${escapeId(name)}" style="max-height:120px;overflow-y:auto;border:1px solid #eee;border-radius:6px;padding:6px 8px;margin-bottom:4px;display:none;"></div>
      <input id="rf-cols-${type}-${escapeId(name)}" value="${escHtml(colsStr)}" placeholder="id,amount,status" oninput="onAddRuleColsInput('${type}','${escAttr(name)}')" />
    </div>
    <div class="perm-rule-field perm-rule-field-full"><label>行级过滤（Jinja2模板）</label>
      <input id="rf-filter-${type}-${escapeId(name)}" value="${escHtml(r.row_filter||'')}" placeholder='dept_id={{user.dept_id}}' />
    </div>
    <div class="perm-rule-field"><label>允许写入</label>
      <select id="rf-write-${type}-${escapeId(name)}"><option value="false" ${!r.allow_write?'selected':''}>否</option><option value="true" ${!!r.allow_write?'selected':''}>是</option></select>
    </div>
    <div class="perm-rule-field"><label>最大返回行数</label>
      <input type="number" id="rf-maxrows-${type}-${escapeId(name)}" value="${r.max_rows||500}" min="10" max="50000" /></div>
    <div class="perm-rule-field perm-rule-field-full" style="margin-top:6px;display:flex;gap:8px;">
      <button class="btn-secondary" onclick="confirmAddRule('${type}','${escAttr(name)}')" style="padding:5px 14px;border:1px solid #007aff;border-radius:6px;background:#007aff;color:#fff;cursor:pointer;font-size:12px;">✓ 确认添加</button>
      <button class="btn-secondary" onclick="hideAddRuleForm('${type}','${escAttr(name)}')" style="padding:5px 14px;border:1px solid #ddd;border-radius:6px;background:#fff;cursor:pointer;font-size:12px;">取消</button>
    </div>
  </div>`;
}

// ---- 数据库→表名→列联动 ----
function loadTablesForSelect(selectId, dbName, selectedTables) {
  const sel = document.getElementById(selectId);
  if (!sel) return;
  sel.innerHTML = '<option value="">加载中...</option>';
  sel.disabled = true;
  // selectedTables 可以是逗号分隔的多个表名，如 "orders, products"
  const selSet = new Set((selectedTables || '').split(',').map(s => s.trim().toLowerCase()).filter(s => s));
  fetch(`/db/schema/${encodeURIComponent(dbName)}`)
    .then(r => { if (!r.ok) throw new Error('连接失败'); return r.json(); })
    .then(tables => {
      sel.innerHTML = ''; // 移除占位 option（多选时不需要"选择表..."占位行）
      if (tables.length === 0) {
        sel.innerHTML = '<option value="">（该数据库无表）</option>';
        return;
      }
      for (const t of tables) {
        const opt = document.createElement('option');
        opt.value = t.name;
        opt.textContent = t.name;
        if (selSet.has(t.name.toLowerCase())) opt.selected = true;
        sel.appendChild(opt);
      }
      sel.disabled = false;
    })
    .catch(err => {
      sel.innerHTML = `<option value="">⚠️ ${err.message}</option>`;
      sel.disabled = false;
    });
}

function loadColumnCheckboxes(containerId, inputId, dbName, tableName, selectedCols) {
  const container = document.getElementById(containerId);
  const input = document.getElementById(inputId);
  if (!container || !input) return;

  if (!tableName) {
    container.style.display = 'none';
    return;
  }

  selectedCols = selectedCols || [];
  container.style.display = 'block';
  container.innerHTML = '<span style="font-size:11px;color:#999;">加载列...</span>';

  fetch(`/db/schema/${encodeURIComponent(dbName)}?table=${encodeURIComponent(tableName)}`)
    .then(r => { if (!r.ok) throw new Error('获取列失败'); return r.json(); })
    .then(tables => {
      const tableInfo = tables.find(t => t.name === tableName);
      if (!tableInfo || !tableInfo.columns || tableInfo.columns.length === 0) {
        container.innerHTML = '<span style="font-size:11px;color:#999;">无可用列</span>';
        return;
      }
      let html = '<div style="display:flex;flex-wrap:wrap;gap:4px;">';
      const selSet = new Set(selectedCols.map(c => c.toLowerCase()));
      for (const col of tableInfo.columns) {
        const checked = selSet.has(col.name.toLowerCase()) ? 'checked' : '';
        html += `<label style="font-size:11px;display:inline-flex;align-items:center;gap:3px;padding:2px 6px;border:1px solid #e0e0e6;border-radius:4px;background:#fff;cursor:pointer;">
          <input type="checkbox" value="${escAttr(col.name)}" ${checked} onchange="onColumnCheckboxChange('${inputId}')" style="width:auto;height:auto;" />
          ${escHtml(col.name)}</label>`;
      }
      html += '</div>';
      container.innerHTML = html;
      // 同步已有勾选状态到输入框
      syncColsCheckboxesToInput(inputId, container);
    })
    .catch(err => {
      container.innerHTML = `<span style="font-size:11px;color:#999;">⚠️ ${err.message}</span>`;
    });
}

function onColumnCheckboxChange(inputId) {
  const input = document.getElementById(inputId);
  if (!input) return;
  // 收集所有选中的列
  const container = input.parentElement.querySelector('div[id$="-checkboxes-"]') ||
    input.closest('.perm-rule-fields')?.querySelector('div[id$="-checkboxes-"]');
  syncColsCheckboxesToInput(inputId, container);
}

function syncColsCheckboxesToInput(inputId, container) {
  const input = document.getElementById(inputId);
  if (!input || !container) return;
  const checked = container.querySelectorAll('input[type="checkbox"]:checked');
  const vals = Array.from(checked).map(cb => cb.value).join(',');
  input.value = vals;
}

function onAddRuleColsInput(type, name) {
  const input = document.getElementById(`rf-cols-${type}-${escapeId(name)}`);
  const container = document.getElementById(`rf-cols-checkboxes-${type}-${escapeId(name)}`);
  if (input && container) {
    syncColsInputToCheckboxes(input, container);
  }
}

function onEditRuleColsInput(type, name, dbName, idx) {
  const input = document.getElementById(`erf-cols-${type}-${escapeId(name)}-${idx}`);
  const container = document.getElementById(`erf-cols-checkboxes-${type}-${escapeId(name)}-${idx}`);
  if (input && container) {
    syncColsInputToCheckboxes(input, container);
  }
}

function syncColsInputToCheckboxes(input, container) {
  const vals = input.value.split(',').map(v => v.trim().toLowerCase()).filter(v => v);
  const cbs = container.querySelectorAll('input[type="checkbox"]');
  for (const cb of cbs) {
    cb.checked = vals.includes(cb.value.toLowerCase());
  }
}

function onAddRuleDbChange(type, name) {
  const dbSel = document.getElementById(`rf-db-${type}-${escapeId(name)}`);
  const dbName = dbSel ? dbSel.value : '';
  // 重置并加载表列表
  const tableSel = document.getElementById(`rf-table-${type}-${escapeId(name)}`);
  if (tableSel) tableSel.innerHTML = '';
  const colsContainer = document.getElementById(`rf-cols-checkboxes-${type}-${escapeId(name)}`);
  if (colsContainer) { colsContainer.style.display = 'none'; colsContainer.innerHTML = ''; }
  if (dbName) {
    loadTablesForSelect(`rf-table-${type}-${escapeId(name)}`, dbName, '');
  }
}

function onAddRuleTableChange(type, name) {
  const tableSel = document.getElementById(`rf-table-${type}-${escapeId(name)}`);
  const dbSel = document.getElementById(`rf-db-${type}-${escapeId(name)}`);
  const firstSelected = tableSel ? Array.from(tableSel.selectedOptions).find(o => o.value) : null;
  const tableName = firstSelected ? firstSelected.value : '';
  const dbName = dbSel ? dbSel.value : '';
  const containerId = `rf-cols-checkboxes-${type}-${escapeId(name)}`;
  const inputId = `rf-cols-${type}-${escapeId(name)}`;
  if (dbName && tableName) {
    loadColumnCheckboxes(containerId, inputId, dbName, tableName, []);
  } else {
    const container = document.getElementById(containerId);
    if (container) { container.style.display = 'none'; container.innerHTML = ''; }
  }
}

function onEditRuleTableChange(type, name, dbName, idx) {
  const tableSel = document.getElementById(`erf-table-${type}-${escapeId(name)}-${idx}`);
  const firstSelected = tableSel ? Array.from(tableSel.selectedOptions).find(o => o.value) : null;
  const tableName = firstSelected ? firstSelected.value : '';
  const containerId = `erf-cols-checkboxes-${type}-${escapeId(name)}-${idx}`;
  const inputId = `erf-cols-${type}-${escapeId(name)}-${idx}`;
  if (dbName && tableName) {
    loadColumnCheckboxes(containerId, inputId, dbName, tableName, []);
  } else {
    const container = document.getElementById(containerId);
    if (container) { container.style.display = 'none'; container.innerHTML = ''; }
  }
}

// ---- 实体操作 ----

function togglePermEntity(type, name) {
  const body = document.getElementById(`body-${type}-${escapeId(name)}`);
  if (body) body.classList.toggle('open');
}

function showAddPermEntity(type) {
  const label = type === 'role' ? '角色名称' : '用户ID';
  const hint = type === 'role' ? '如 analyst, admin, viewer' : '如 alice, bob (支持逗号分隔多个用户)';
  const val = prompt(`请输入${label}:\n提示：${hint}`);
  // 严格校验空值、纯空格、过短值
  if (!val || !val.trim() || val.trim().length < 1) {
    return;
  }
  const names = val.split(',').map(s => s.trim()).filter(s => s && s.length > 0);
  if (names.length === 0) return;
  const addedNames = [];
  for (const n of names) {
    const key = type === 'user' ? 'users' : 'roles';
    if (!_permData[key]) _permData[key] = {};
    if (_permData[key][n]) {
      alert(`${n} 已存在，请勿重复添加`);
      continue;
    }
    // 如果是添加用户，如果有已定义的角色，让用户选择绑定角色
    let role = '';
    if (type === 'user') {
      const roleNames = Object.keys(_permData.roles || {}).filter(rn => rn && rn.trim());
      if (roleNames.length > 0) {
        role = prompt(`为用户 "${n}" 选择绑定的角色（留空则不绑定）:\n可用角色: ${roleNames.join(', ')}`) || '';
      }
    }
    _permData[key][n] = { databases: {}, role: role };
    addedNames.push(n);
  }
  renderPermEntities();
  // 自动展开新添加的实体卡片，让用户立即看到角色选择器和规则区
  for (const n of addedNames) {
    const body = document.getElementById(`body-${type}-${escapeId(n)}`);
    if (body) body.classList.add('open');
  }
}

function deletePermEntity(type, name) {
  if (!confirm(`确定要删除 ${type==='role'?'角色':'用户'} "${name}" 吗？`)) return;
  if (type === 'role') {
    delete _permData.roles[name];
  } else {
    delete _permData.users[name];
  }
  renderPermEntities();
}

// ---- 规则操作 ----

function showAddRuleForm(type, name) {
  const form = document.getElementById(`addrule-form-${type}-${escapeId(name)}`);
  if (form) {
    form.style.display = 'block';
    // 填充数据库下拉框
    populateDbSelectForRule(type, name);
  }
  // 确保实体展开
  const body = document.getElementById(`body-${type}-${escapeId(name)}`);
  if (body) body.classList.add('open');
}

function hideAddRuleForm(type, name) {
  const form = document.getElementById(`addrule-form-${type}-${escapeId(name)}`);
  if (form) form.style.display = 'none';
}

function populateDbSelectForRule(type, name) {
  const sel = document.getElementById(`rf-db-${type}-${escapeId(name)}`);
  if (!sel) return;
  sel.innerHTML = '<option value="">选择数据库...</option>';
  // 从已保存的连接列表获取数据库名
  fetch('/db/connections')
    .then(r=>r.json())
    .then(conns => {
      if (conns && Array.isArray(conns)) {
        conns.forEach(c => {
          const opt = document.createElement('option');
          opt.value = c.name; opt.textContent = `${c.name} (${c.db_type})`;
          sel.appendChild(opt);
        });
      }
    })
    .catch(()=>{});
}

function confirmAddRule(type, name) {
  const dbName = document.getElementById(`rf-db-${type}-${escapeId(name)}`).value;
  const sel = document.getElementById(`rf-table-${type}-${escapeId(name)}`);
  const selected = Array.from(sel.selectedOptions).map(o => o.value).filter(v => v);
  const tableNames = selected.join(', ');
  const colsStr = document.getElementById(`rf-cols-${type}-${escapeId(name)}`).value.trim();
  const rowFilter = document.getElementById(`rf-filter-${type}-${escapeId(name)}`).value.trim();
  const allowWrite = document.getElementById(`rf-write-${type}-${escapeId(name)}`).value === 'true';
  const maxRows = parseInt(document.getElementById(`rf-maxrows-${type}-${escapeId(name)}`).value) || 500;

  if (!dbName) { alert('请选择数据库连接'); return; }
  if (!tableNames) { alert('请选择至少一个表'); return; }

  const cols = colsStr ? colsStr.split(',').map(c=>c.trim()).filter(c=>c) : [];

  const ruleObj = {
    table: tableNames,
    columns_allow: cols,
    row_filter: rowFilter,
    allow_write: allowWrite,
    max_rows: maxRows
  };

  const key = type === 'user' ? 'users' : 'roles';
  if (!_permData[key]) _permData[key] = {};
  if (!_permData[key][name]) _permData[key][name] = { databases: {} };
  if (!_permData[key][name].databases[dbName]) {
    _permData[key][name].databases[dbName] = [];
  }
  _permData[key][name].databases[dbName].push(ruleObj);

  hideAddRuleForm(type, name);
  renderPermEntities();

  // 自动展开刚编辑的实体
  setTimeout(() => {
    const body = document.getElementById(`body-${type}-${escapeId(name)}`);
    if (body) body.classList.add('open');
  }, 50);
}

// ---- 保存到后端 ----

async function saveVisualPermissions() {
  try {
    console.log('[保存权限] 开始保存...');
    const msgEl = document.getElementById('perm-save-msg');
    if (!msgEl) { console.error('[保存权限] 找不到 perm-save-msg 元素'); return; }
    msgEl.style.display = 'inline';msgEl.style.color = '#666';msgEl.textContent = '正在保存...';

    // 收集全局默认值
    const permObj = {
      global_defaults: {
        max_query_rows: parseInt(document.getElementById('g-max-rows').value) || 500,
        default_readonly: document.getElementById('g-default-readonly').checked,
        allow_dangerous_sql: document.getElementById('g-dangerous-sql').checked,
      },
      roles: {},
      users: {}
    };

    console.log('[保存权限] _permData:', JSON.stringify(_permData).substring(0,300));

    // 收集角色（跳过空角色名）
    if (_permData && _permData.roles) {
      for (const [rn, rd] of Object.entries(_permData.roles)) {
        if (!rn || !rn.trim()) continue;
        if (!rd || !rd.databases) continue;
        permObj.roles[rn] = { databases: {} };
        for (const [dn, tables] of Object.entries(rd.databases)) {
          if (!dn || !tables) continue;
          permObj.roles[rn].databases[dn] = tables.map(t => ({
            table: t.table || '', columns_allow: t.columns_allow||[],
            row_filter: t.row_filter||'', allow_write: !!t.allow_write,
            max_rows: t.max_rows||500
          }));
        }
      }
    }
    // 收集用户（跳过空用户名）
    if (_permData && _permData.users) {
      for (const [un, ud] of Object.entries(_permData.users)) {
        if (!un || !un.trim()) continue;
        if (!ud) continue;
        const userObj = { databases: {} };
        if (ud.role) {
          userObj.role = ud.role;
        }
        if (ud.databases) {
          for (const [dn, tables] of Object.entries(ud.databases)) {
            if (!dn || !tables) continue;
            userObj.databases[dn] = tables.map(t => ({
              table: t.table || '', columns_allow: t.columns_allow||[],
              row_filter: t.row_filter||'', allow_write: !!t.allow_write,
              max_rows: t.max_rows||500
            }));
          }
        }
        permObj.users[un] = userObj;
      }
    }

    console.log('[保存权限] 发送数据:', JSON.stringify(permObj).substring(0,300));
    const saveResp = await fetch('/db/permissions', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(permObj)  // 直接发送 JSON，不再嵌套
    });
    console.log('[保存权限] 响应状态:', saveResp.status);
    const result = await saveResp.json();
    console.log('[保存权限] 响应内容:', result);
    if (result.status === 'ok') {
      msgEl.style.color = '#30d158';
      msgEl.textContent = '✓ 保存成功！';
      setTimeout(() => { msgEl.style.display = 'none'; }, 3000);
      loadVisualPermissions(); // 重新加载确认
    } else {
      const errMsg = (result.detail || result.message || '未知错误');
      msgEl.style.color = '#d70015';
      msgEl.textContent = '保存失败: ' + errMsg;
      alert('⚠️ 权限配置保存失败:\n\n' + errMsg);
    }
  } catch (e) {
    console.error('[保存权限] 出错:', e);
    try {
      const msgEl = document.getElementById('perm-save-msg');
      if (msgEl) {
        msgEl.style.color = '#d70015';
        msgEl.textContent = '保存出错: ' + e.message;
      }
    } catch (_) {}
    alert('⚠️ 保存权限时出错:\n\n' + e.message + '\n\n请打开浏览器控制台(F12)查看详细错误');
  }
}
