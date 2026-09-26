// client/src/markdown.js
// 极简 Markdown 解析：只支持聊天里真正会用到的子集，解析成结构化 token，
// 由 Vue 用真实元素渲染（不走 v-html），所以模型输出里即使带 HTML 也不会被执行。
//
// 支持：标题 / 无序有序列表 / 表格 / 引用 / 分隔线 / 围栏代码块
// 行内：**加粗**、`行内代码`、知识库问答的 [1] 引用标记、以及 tel: 拨号链接
//       （人工客服电话写成 [400-888-8888](tel:400-888-8888)，手机点一下就能拨号）

const INLINE_PATTERN = /(\*\*[^*\n]+\*\*|`[^`\n]+`|\[[^\]\n]+\]\(tel:[^)\s]+\)|\[\d{1,2}\])/g;

// 只放行 tel: 协议，号码里只允许数字 / + / - / 空格：模型即使吐出别的链接也点不开
const TEL_LINK_PATTERN = /^\[([^\]\n]+)\]\((tel:[+0-9\-\s]{3,24})\)$/;

/** 行内解析：返回 [{type:'text'|'bold'|'code'|'cite', ...}] */
export function parseInline(text, cites = []) {
  const source = String(text == null ? '' : text);
  const tokens = [];
  let last = 0;
  let match;
  INLINE_PATTERN.lastIndex = 0;

  while ((match = INLINE_PATTERN.exec(source)) !== null) {
    if (match.index > last) tokens.push({ type: 'text', text: source.slice(last, match.index) });
    const raw = match[0];
    if (raw.startsWith('**')) {
      const inner = raw.slice(2, -2);
      // 粗体里包着拨号链接时（**[400-888-8888](tel:...)**）要拆开，否则链接会被当成纯文字
      const nested = inner.match(TEL_LINK_PATTERN);
      if (nested) {
        if (nested.index > 0) tokens.push({ type: 'text', text: inner.slice(0, nested.index) });
        tokens.push({ type: 'link', href: nested[2].replace(/\s+/g, ''), text: nested[1] });
        const rest = inner.slice(nested.index + nested[0].length);
        if (rest) tokens.push({ type: 'text', text: rest });
      } else {
        tokens.push({ type: 'bold', text: inner });
      }
    } else if (raw.startsWith('`')) {
      tokens.push({ type: 'code', text: raw.slice(1, -1) });
    } else if (raw.includes('](tel:')) {
      const link = raw.match(TEL_LINK_PATTERN);
      if (link) tokens.push({ type: 'link', href: link[2].replace(/\s+/g, ''), text: link[1] });
      else tokens.push({ type: 'text', text: raw });
    } else {
      const index = Number(raw.slice(1, -1));
      // 只有来源里真实存在的编号才渲染成可点引用，其余当普通文字
      if (cites.includes(index)) tokens.push({ type: 'cite', index });
      else tokens.push({ type: 'text', text: raw });
    }
    last = match.index + raw.length;
  }
  if (last < source.length) tokens.push({ type: 'text', text: source.slice(last) });
  return tokens.length ? tokens : [{ type: 'text', text: source }];
}

const isTableSeparator = (line) => /^\s*\|?[\s:|-]*-[\s:|-]*\|[\s:|-]*$/.test(line || '');
const isListLine = (line) => /^\s*([-*+]|\d+[.)])\s+/.test(line || '');

function listItemText(line) {
  return line.replace(/^\s*([-*+]|\d+[.)])\s+/, '');
}

function splitRow(line) {
  return line.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map((cell) => cell.trim());
}

/** 块级解析：返回 [{type:'p'|'heading'|'list'|'table'|'quote'|'hr'|'code', ...}] */
export function parseMarkdown(text, cites = []) {
  const lines = String(text == null ? '' : text).replace(/\r\n/g, '\n').split('\n');
  const blocks = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) { i += 1; continue; }

    // 围栏代码块
    if (/^\s*`{3}/.test(line)) {
      const buffer = [];
      i += 1;
      while (i < lines.length && !/^\s*`{3}/.test(lines[i])) { buffer.push(lines[i]); i += 1; }
      i += 1;
      blocks.push({ type: 'code', text: buffer.join('\n') });
      continue;
    }

    // 分隔线
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { blocks.push({ type: 'hr' }); i += 1; continue; }

    // 标题
    const heading = line.match(/^\s*(#{1,4})\s+(.*)$/);
    if (heading) {
      blocks.push({ type: 'heading', level: heading[1].length, tokens: parseInline(heading[2], cites) });
      i += 1;
      continue;
    }

    // 表格
    if (line.includes('|') && isTableSeparator(lines[i + 1])) {
      const header = splitRow(line).map((cell) => parseInline(cell, cites));
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
        rows.push(splitRow(lines[i]).map((cell) => parseInline(cell, cites)));
        i += 1;
      }
      blocks.push({ type: 'table', header, rows });
      continue;
    }

    // 列表（有序 / 无序分开，不混在一起）
    if (isListLine(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const items = [];
      while (i < lines.length && isListLine(lines[i]) &&
             /^\s*\d+[.)]\s+/.test(lines[i]) === ordered) {
        items.push(parseInline(listItemText(lines[i]), cites));
        i += 1;
      }
      blocks.push({ type: 'list', ordered, items });
      continue;
    }

    // 引用
    if (/^\s*>\s?/.test(line)) {
      const buffer = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        buffer.push(lines[i].replace(/^\s*>\s?/, ''));
        i += 1;
      }
      blocks.push({ type: 'quote', tokens: parseInline(buffer.join('\n'), cites) });
      continue;
    }

    // 普通段落（连续非空行合并）
    const buffer = [line];
    i += 1;
    while (i < lines.length && lines[i].trim() &&
           !isListLine(lines[i]) && !/^\s*#{1,4}\s/.test(lines[i]) &&
           !/^\s*>\s?/.test(lines[i]) && !/^\s*`{3}/.test(lines[i])) {
      buffer.push(lines[i]);
      i += 1;
    }
    blocks.push({ type: 'p', tokens: parseInline(buffer.join('\n'), cites) });
  }

  return blocks;
}