/* messaging.js — 消息渲染、附件处理、输入事件绑定
   依赖: state.js, util.js, i18n.js, artifacts.js(openArtifactPreview) */

// ---------- 消息区域点击：制品预览链接委托 ----------
messages.addEventListener('click', (event) => {
  const link = event.target.closest('a');
  if (!link) return;
  const href = link.getAttribute('href') || '';
  if (!href.startsWith('#artifact-preview:')) return;
  event.preventDefault();
  const path = decodeURIComponent(href.slice('#artifact-preview:'.length));
  openArtifactPreview(path);
});

// ---------- 消息渲染 ----------

function addMessage(text, role) {
  text = unescapeDisplay(String(text || ''));
  const div = document.createElement('div');
  div.className = `msg ${role}`;
  if (role === 'bot') {
    // 渲染 Markdown
    div.innerHTML = renderMarkdown(text);
  } else {
    div.textContent = text;
  }
  messages.appendChild(div);
  smartScroll(messages);
  return div;
}

function addUserMessage(text, attachments = []) {
  const hasZip = attachments.some(item => item.mime_type === 'application/zip' || (item.name || '').endsWith('.zip'));
  const hasText = !hasZip && attachments.some(function(item) {
    return /\.(md|txt|json|yaml|yml|xml|html|css|js|ts|jsx|tsx|py|java|c|cpp|h|hpp|go|rs|rb|php|sh|bash|zsh|sql|csv|log|env|toml|ini|cfg|conf|vue|svelte|kt|swift|scala)$/i.test(item.name || '');
  });
  var msgFallback = '分析附件中...';
  if (hasZip) msgFallback = '分析项目中...';
  else if (hasText) msgFallback = '分析文件中...';
  const div = addMessage(text || (attachments.length ? msgFallback : ''), 'user');
  if (attachments.length) {
    const grid = document.createElement('div');
    grid.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;';
    attachments.forEach(item => {
      const name = item.name || '';
      const isZip = item.mime_type === 'application/zip' || name.endsWith('.zip');
      const isText = !isZip && /\.(md|txt|json|yaml|yml|xml|html|css|js|ts|jsx|tsx|py|java|c|cpp|h|hpp|go|rs|rb|php|sh|bash|zsh|sql|csv|log|env|toml|ini|cfg|conf|vue|svelte|kt|swift|scala)$/i.test(name);
      if (isZip) {
        const badge = document.createElement('div');
        badge.style.cssText = 'width:96px;height:96px;display:flex;flex-direction:column;align-items:center;justify-content:center;border-radius:8px;background:#e0f2fe;color:#0369a1;border:1px solid rgba(255,255,255,.5);font-size:12px;';
        badge.innerHTML = '<span style="font-size:28px">📦</span><span style="margin-top:4px">' + escapeHtml(name) + '</span>';
        grid.appendChild(badge);
      } else if (isText) {
        const badge = document.createElement('div');
        const ext = name.lastIndexOf('.') >= 0 ? name.slice(name.lastIndexOf('.') + 1).toUpperCase() : 'FILE';
        badge.style.cssText = 'width:96px;height:96px;display:flex;flex-direction:column;align-items:center;justify-content:center;border-radius:8px;background:#f3f4f6;color:#374151;border:1px solid rgba(255,255,255,.5);font-size:11px;font-weight:bold;cursor:pointer;';
        badge.innerHTML = '<span style="font-size:28px;line-height:1">📄</span><span style="margin-top:4px">' + escapeHtml(name) + '</span>';
        // ponytail: 文本附件默认只显示图标，用户双击才展开内容（刷新恢复的消息 item.content 已有内容；实时消息从 data_url 解码）
        badge.title = currentLanguage === 'en' ? 'Double-click to view file content' : '双击查看文件内容';
        badge.addEventListener('dblclick', () => toggleTextExpand(div, item, badge));
        grid.appendChild(badge);
      } else {
        const img = document.createElement('img');
        img.src = item.data_url;
        img.alt = name || 'pasted image';
        img.style.cssText = 'width:96px;height:96px;object-fit:cover;border-radius:8px;border:1px solid rgba(255,255,255,.5);';
        grid.appendChild(img);
      }
    });
    div.appendChild(grid);
  }
  return div;
}

// ---------- 文本附件：解析 / 双击展开 ----------
// 后端把文本附件内联成 "消息\n── 文件: 名称 ──\n内容" 存进用户消息 text 字段；
// 刷新恢复时据此把内联文本还原成「图标 + 双击展开」，与实时发送表现一致。
const TEXT_FILE_RE = /\n── 文件: ([^\n]+?) ──\n([\s\S]*?)(?=\n── 文件: |$)/g;

function parseTextFilesFromContent(content) {
  if (!content || content.indexOf('\n── 文件:') === -1) {
    return { message: content || '', files: [] };
  }
  const files = [];
  let m;
  TEXT_FILE_RE.lastIndex = 0;
  while ((m = TEXT_FILE_RE.exec(content)) !== null) {
    files.push({ name: m[1], content: m[2] });
  }
  const firstIdx = content.indexOf('\n── 文件:');
  const message = content.slice(0, firstIdx).trim();
  return { message: message, files: files };
}

// 取文本附件内容：优先用 item.content（刷新恢复），否则从 data_url(base64) 解码（实时发送）
function attachmentTextContent(item) {
  if (item && item.content) return item.content;
  if (item && typeof item.data_url === 'string' && item.data_url.indexOf('data:') === 0) {
    const comma = item.data_url.indexOf(',');
    if (comma >= 0) {
      try {
        const bin = atob(item.data_url.slice(comma + 1));
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        // ponytail: 文本附件 ≤1MB，utf-8 优先；gbk 仅作极端回退
        try {
          return new TextDecoder('utf-8').decode(bytes);
        } catch (e) {
          try { return new TextDecoder('gbk').decode(bytes); } catch (e2) { return ''; }
        }
      } catch (e) {
        return '';
      }
    }
  }
  return '';
}

// 双击文本徽章：在消息气泡内展开/收起内容面板
function toggleTextExpand(msgDiv, item, chip) {
  const existing = msgDiv.querySelector(':scope > .text-expand-panel');
  if (existing) {
    existing.remove();
    chip.classList.remove('expanded');
    return;
  }
  const panel = document.createElement('pre');
  panel.className = 'text-expand-panel';
  panel.textContent = attachmentTextContent(item);
  msgDiv.appendChild(panel);
  chip.classList.add('expanded');
}

function resizeComposer() {
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
}

function renderAttachmentPreview() {
  if (!attachmentPreview) return;
  attachmentPreview.innerHTML = '';
  attachmentPreview.classList.toggle('show', pendingAttachments.length > 0);
  pendingAttachments.forEach((item, index) => {
    const chip = document.createElement('div');
    chip.className = 'attachment-chip';

    var name = item.name || '';
    var isZip = item.mime_type === 'application/zip' || name.endsWith('.zip');
    var isText = !isZip && /\.(md|txt|json|yaml|yml|xml|html|css|js|ts|jsx|tsx|py|java|c|cpp|h|hpp|go|rs|rb|php|sh|bash|zsh|sql|csv|log|env|toml|ini|cfg|conf|vue|svelte|kt|swift|scala)$/i.test(name);
    if (isZip) {
      const icon = document.createElement('div');
      icon.style.cssText = 'width:100%;height:100%;display:flex;align-items:center;justify-content:center;font-size:28px;background:#e0f2fe;color:#0369a1;font-weight:bold;';
      icon.textContent = '📦';
      chip.appendChild(icon);
    } else if (isText) {
      const icon = document.createElement('div');
      var ext = name.lastIndexOf('.') >= 0 ? name.slice(name.lastIndexOf('.') + 1).toUpperCase() : 'FILE';
      icon.style.cssText = 'width:100%;height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;font-size:11px;background:#f3f4f6;color:#374151;font-weight:bold;';
      icon.textContent = ext;
      chip.appendChild(icon);
    } else {
      const img = document.createElement('img');
      img.src = item.data_url;
      img.alt = name || 'pasted image';
      chip.appendChild(img);
    }

    const removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.setAttribute('aria-label', currentLanguage === 'en' ? 'Remove image' : '移除图片');
    removeBtn.textContent = '×';
    removeBtn.addEventListener('click', () => {
      pendingAttachments.splice(index, 1);
      renderAttachmentPreview();
    });
    chip.appendChild(removeBtn);
    attachmentPreview.appendChild(chip);
  });
}

function addFile(file) {
  var name = file.name || '';
  var isZip = name.endsWith('.zip') || file.type === 'application/zip' || file.type === 'application/x-zip-compressed';
  var TEXT_EXTS = ['.md','.txt','.json','.yaml','.yml','.xml','.html','.css','.js','.ts','.jsx','.tsx',
    '.py','.java','.c','.cpp','.h','.hpp','.go','.rs','.rb','.php','.sh','.bash','.zsh','.sql',
    '.csv','.log','.env','.toml','.ini','.cfg','.conf','.vue','.svelte','.kt','.swift','.scala'];
  var isText = !isZip && TEXT_EXTS.some(function(ext) { return name.toLowerCase().endsWith(ext); });

  if (!isZip && !isText && !file.type.startsWith('image/')) return;

  var maxSize = isZip ? 50 * 1024 * 1024 : (isText ? 1 * 1024 * 1024 : 6 * 1024 * 1024);
  if (file.size > maxSize) {
    addMessage(t('imageTooLarge', { name: name }), 'system');
    return;
  }

  if (pendingAttachments.length >= 4) {
    addMessage(t('imageLimit'), 'system');
    return;
  }

  var reader = new FileReader();
  reader.onload = function() {
    pendingAttachments.push({
      name: name,
      mime_type: isZip ? 'application/zip' : (isText ? 'text/plain' : (file.type || 'image/png')),
      data_url: String(reader.result || ''),
    });
    renderAttachmentPreview();
  };
  reader.readAsDataURL(file);
}

function handleFiles(files) {
  files.forEach(addFile);
}

// ---------- 文件上传按钮 ----------
document.getElementById('attach-btn').onclick = () => {
  document.getElementById('file-input').click();
};
document.getElementById('file-input').onchange = (e) => {
  handleFiles(Array.from(e.target.files));
  e.target.value = '';
};

// ---------- 输入框事件绑定 ----------
let inputComposing = false;
let lastCompositionEndAt = 0;

input.addEventListener('compositionstart', () => {
  inputComposing = true;
});

input.addEventListener('compositionend', () => {
  inputComposing = false;
  lastCompositionEndAt = Date.now();
});

input.addEventListener('input', resizeComposer);

input.addEventListener('paste', (e) => {
  // ponytail: 系统截图（Win+Shift+S / Snip 等）粘贴时 clipboardData.files 常为空，
  // 图片只存在于 items 中，必须通过 item.getAsFile() 取出；旧实现只读 files 且误调用
  // 未定义的 addImageFile → 截图粘贴彻底失效。这里优先遍历 items，回退到 files。
  const dt = e.clipboardData;
  if (!dt) return;

  // 优先从 items 取（截图必经之路），再回退到 files（资源管理器复制图片文件时）
  let images = [];
  if (dt.items && dt.items.length) {
    images = Array.from(dt.items)
      .filter(it => it.kind === 'file' && it.type.startsWith('image/'))
      .map(it => it.getAsFile())
      .filter(Boolean);
  }
  if (!images.length && dt.files && dt.files.length) {
    images = Array.from(dt.files).filter(file => file.type.startsWith('image/'));
  }
  if (!images.length) return;

  e.preventDefault();
  images.forEach(addFile);
});

input.addEventListener('keydown', (e) => {
  // ── 上下方向键：消息历史导航 ──
  if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
    e.preventDefault();
    if (_msgHistory.length === 0) return;
    if (e.key === 'ArrowUp') {
      // 上移：后退
      if (_msgHistoryIndex === -1) {
        // 首次按↑：从最后一条开始
        _msgHistoryIndex = _msgHistory.length - 1;
      } else if (_msgHistoryIndex === 0) {
        // 已到最旧消息，不再滚动
        return;
      } else {
        _msgHistoryIndex--;
      }
    } else {
      // 下移：前进
      if (_msgHistoryIndex === -1) return;
      _msgHistoryIndex++;
      if (_msgHistoryIndex >= _msgHistory.length) {
        _msgHistoryIndex = -1;
        input.value = '';
        resizeComposer();
        return;
      }
    }
    input.value = _msgHistoryIndex >= 0 ? _msgHistory[_msgHistoryIndex] : '';
    resizeComposer();
    // 光标移到末尾
    input.selectionStart = input.selectionEnd = input.value.length;
    return;
  }

  if (e.key !== 'Enter') return;
  const justEndedComposition = Date.now() - lastCompositionEndAt < 120;
  if (inputComposing || e.isComposing || e.keyCode === 229 || justEndedComposition) {
    return;
  }
  // Enter 单独按 → 发送；⌘/Ctrl+Enter → 换行；屏蔽 Shift+Enter
  if (e.metaKey || e.ctrlKey) {
    // 插入换行
    const start = input.selectionStart, end = input.selectionEnd;
    input.value = input.value.slice(0, start) + '\n' + input.value.slice(end);
    input.selectionStart = input.selectionEnd = start + 1;
    resizeComposer();
    e.preventDefault();
    return;
  }
  if (e.shiftKey) return;
  e.preventDefault();
  send();
});
sendBtn.onclick = () => {
  if (isLoading) {
    stopCurrentRun();
    return;
  }
  send();
};
