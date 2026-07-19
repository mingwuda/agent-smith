/* util.js — 通用工具函数（转义、滚动、Markdown 渲染、YAML 序列化）
   依赖：state.js（无直接依赖，仅约定加载顺序在前）；全局 marked / katex（CDN 注入）。 */

function escapeHtml(text) {
  const d = document.createElement('div');
  d.textContent = text;
  return d.innerHTML;
}

// 将字符串中字面量的 \n \t \r \\ 还原为实际换行/制表符/反斜杠，用于展示后端 JSON 转义后的文本
function unescapeDisplay(text) {
  if (typeof text !== 'string') return text;
  const placeholder = '\u0000';
  return text
    .replace(/\\\\/g, placeholder)
    .replace(/\\n/g, '\n')
    .replace(/\\t/g, '\t')
    .replace(/\\r/g, '\r')
    .replace(new RegExp(placeholder, 'g'), '\\');
}

/* escHtml：与 escapeHtml 不同，不转义引号。
   用于权限可视化表单的 <input value="..."> 和 <option> 文本，
   转义引号会导致表单显示 &quot; 实体。保留两者语义差异。 */
function escHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function escAttr(s) { return s.replace(/'/g,"\\'").replace(/"/g,'&quot;'); }
function escapeId(s) { return s.replace(/[^a-zA-Z0-9_-]/g, '_'); }

function smartScroll(el) {
  const threshold = 2; // 距底部 ≤2px 才滚动，避免用户翻看时被打断
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight <= threshold;
  // 流式输出期间强制跟随到最新内容，避免用户手动滚屏后漏看新输出
  if (atBottom || streamingActive) {
    el.scrollTop = el.scrollHeight;
  }
}

marked.setOptions({
  breaks: true,
  gfm: true,
});

function renderMarkdown(text) {
  text = unescapeDisplay(String(text || ''));
  // ----- 数学公式渲染（KaTeX）-----
  // 直接嵌入 KaTeX HTML，不使用占位符方案。
  // marked 的 GFM 模式会保留 inline HTML，因此 KaTeX <span>/<div> 能原样通过。
  const katexAvailable = typeof katex !== 'undefined' && typeof katex.renderToString === 'function';
  var hasDollar = text.indexOf('$') >= 0;

  if (katexAvailable && hasDollar) {
    // 1) 处理显示公式 $$...$$（块级）→ 直接替换为 KaTeX HTML
    text = text.replace(/\$\$([\s\S]*?)\$\$/g, function(m, formula) {
      try {
        return katex.renderToString(formula.trim(), { displayMode: true, throwOnError: false });
      } catch (e) {
        return '<div class="katex-error">公式解析错误: ' + e.message + '</div>';
      }
    });

    // 2) 处理行内公式 $...$ → 直接替换为 KaTeX HTML
    text = text.replace(/(?<!\$)\$([^$\n]+?)\$(?!\$)/g, function(m, formula) {
      var f = formula.trim();
      if (!f) return m;
      // 仅跳过纯货币金额：$10 / $10.50 / $10 USD（数字+可选小数+可选空格+可选货币单位）
      // 但不跳过含数学运算符的公式（^ _ \ { } + - * / 等）
      if (/^\d+(\.\d+)?\s*(USD|CNY|EUR|美元|元|块)?$/.test(f) && !/[\\\^_{}]/.test(f)) return m;
      try {
        return katex.renderToString(f, { displayMode: false, throwOnError: false });
      } catch (e) {
        return m;
      }
    });
  }

  // 3) marked 渲染（开启 GFM + 换行）
  var raw = marked.parse(text);

  // 4) XSS 安全过滤
  var safe = raw
    .replace(/<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/gi, '')
    .replace(/on\w+="[^"]*"/gi, '')
    .replace(/on\w+='[^']*'/gi, '')
    .replace(/javascript:/gi, '');
  return safe;
}

function objToYaml(obj, indent=0) {
  const pad = '  '.repeat(indent);
  let lines = [];
  for (const [key, val] of Object.entries(obj)) {
    if (val === null || val === undefined) continue;
    if (Array.isArray(val)) {
      if (val.length === 0) {
        lines.push(`${pad}${key}: []`);
      } else if (typeof val[0] === 'object' && val[0] !== null) {
        lines.push(`${pad}${key}:`);
        for (const item of val) {
          lines.push(pad + '  - ' + objToYaml(item, indent + 2).trimStart());
        }
      } else {
        lines.push(`${pad}${key}: ${JSON.stringify(val).replace(/"/g, "'").slice(1,-1)}`);
      }
    } else if (typeof val === 'object') {
      const subKeys = Object.keys(val);
      if (subKeys.length === 0) {
        lines.push(`${pad}${key}: {}`);
      } else {
        lines.push(`${pad}${key}:`);
        lines.push(objToYaml(val, indent + 1));
      }
    } else if (typeof val === 'boolean') {
      lines.push(`${pad}${key}: ${val ? 'true' : 'false'}`);
    } else if (typeof val === 'number') {
      lines.push(`${pad}${key}: ${val}`);
    } else {
      lines.push(`${pad}${key}: "${String(val).replace(/\\/g,'\\\\').replace(/"/g,'\\"')}"`);
    }
  }
  return lines.join('\n');
}
