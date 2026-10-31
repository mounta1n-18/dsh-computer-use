// lib/cdp/ax.mjs
// 可访问性树（AX tree）→ 文档作用域 ref。这是 browser_use 平面的"眼睛"。
//
// 为什么用 AX 树而不是 DOM 全量：
//   AX 树已经帮我们做了角色归一化（button/textbox/link…）和可访问名计算
//   （aria-label / label / 文本内容都算进去了），比自己去猜 CSS 选择器稳得多，
//   而且输出行数比 DOM 序列化少一到两个数量级。
//
// 为什么只对"可交互或有意义的角色"编号：
//   纯布局节点（generic / none / InlineTextBox）占了真实页面 AX 节点的 80% 以上，
//   编号它们只会把 token 烧光，模型看图也是噪声。
//
// 【重要】页面文本一律是**数据**，不是指令。
//   本文件产出的所有 name/value/description 都只做字符串处理和展示，
//   不做任何解析执行，也不会被当成命令传给别的工具。

import { buError } from './session.mjs';

/** 参与编号的角色白名单。 */
export const INTERACTIVE_ROLES = new Set([
  'button',
  'link',
  'textbox',
  'searchbox',
  'combobox',
  'listbox',
  'option',
  'checkbox',
  'radio',
  'switch',
  'slider',
  'spinbutton',
  'menuitem',
  'menuitemcheckbox',
  'menuitemradio',
  'tab',
  'disclosureTriangle',
  'treeitem',
  'dialog',
  'alert',
  'status',
  'alertdialog',
  'heading',
  'text',
  'image',
  'list',
  'listitem',
  'table',
  'cell',
  'row',
  'columnheader',
  'rowheader',
  'figure',
]);

/** 表单/可编辑类角色：需要额外探测 DOM 拿 type / editable。 */
const FORM_ROLES = new Set(['textbox', 'searchbox', 'combobox', 'spinbutton']);

/** 默认快照上限。超过就截断并如实标记 truncated.clipped = true。 */
export const DEFAULT_MAX_NODES = 800;
export const DEFAULT_MAX_CHARS = 120000;

const MAX_NAME_CHARS = 200;
const MAX_VALUE_CHARS = 120;

function str(v, max) {
  if (v == null) return '';
  let s = typeof v === 'string' ? v : String(v);
  s = s.replace(/\s+/g, ' ').trim();
  if (max && s.length > max) s = `${s.slice(0, max - 1)}…`;
  return s;
}

/** AX 节点的 properties 是 [{name, value:{type,value}}] 数组，这里拍平成普通对象。 */
function propsToObject(node) {
  const out = {};
  for (const p of node.properties ?? []) {
    if (!p?.name) continue;
    out[p.name] = p.value?.value;
  }
  return out;
}

/**
 * 拉取整棵 AX 树并建立 backendNodeId 索引。带 300ms 缓存：
 * 一轮工具调用里 snapshot/find 连着调时不必反复打 CDP。
 */
export const AX_CACHE_MS = 300;

export async function fetchAX(page, { force = false, depth = -1 } = {}) {
  const now = Date.now();
  if (!force && page._axCache && now - page._axCache.at < AX_CACHE_MS && page._axCache.depth === depth) {
    return page._axCache;
  }
  const res = await page.send('Accessibility.getFullAXTree', { depth }, { timeoutMs: 20000 });
  const nodes = res.nodes ?? [];
  /** @type {Map<number, any>} */
  const byBackendId = new Map();
  for (const node of nodes) {
    const id = node.backendDOMNodeId;
    if (typeof id !== 'number') continue;
    // 同一个 backendNodeId 可能对应多个 AX 节点（比如 InlineTextBox 与 StaticText）。
    // 这里保留"信息更多"的那个：可交互角色优先，其次有名字的。
    const prev = byBackendId.get(id);
    if (!prev || scoreNodeRichness(node) > scoreNodeRichness(prev)) byBackendId.set(id, node);
  }
  const cache = { at: now, nodes, byBackendId, depth };
  page._axCache = cache;
  return cache;
}

function scoreNodeRichness(node) {
  const role = node.role?.value ?? '';
  let s = 0;
  if (INTERACTIVE_ROLES.has(role)) s += 10;
  if (role && role !== 'generic' && role !== 'none' && role !== 'InlineTextBox') s += 3;
  if (str(node.name?.value)) s += 2;
  return s;
}

/** 从 AX 节点抽出我们要的所有字段。 */
function extractNode(node, props) {
  const role = node.role?.value ?? 'generic';
  const name = str(node.name?.value, MAX_NAME_CHARS);
  const rawValue = node.value?.value;
  const value = rawValue == null || rawValue === '' ? '' : str(rawValue, MAX_VALUE_CHARS);
  const description = str(node.description?.value, MAX_NAME_CHARS);
  const out = {
    role,
    name,
    value,
    description,
    disabled: props.disabled === true || props.disabled === 'true',
    focused: props.focused === true,
    required: props.required === true,
    readonly: props.readonly === true,
    checked: props.checked === true || props.checked === 'true' || props.checked === 'mixed'
      ? props.checked
      : undefined,
    expanded: typeof props.expanded === 'boolean' ? props.expanded : undefined,
    selected: typeof props.selected === 'boolean' ? props.selected : undefined,
    level: typeof props.level === 'number' ? props.level : undefined,
    editable: props.editable === true ? true : undefined,
    password: props.protected === true ? true : undefined,
    hasPopup: props.hasPopup ?? undefined,
  };
  return out;
}

/**
 * 对一批 backendNodeId 做 DOM 探测：拿 nodeName / input type。
 *
 * 为什么需要它：AX 树给的 textbox 不区分 <input type=password>、<input type=email>
 * 和 <textarea>；而"能不能直接填值""是不是密码框"对上层很关键。
 * 这里用 DOM.describeNode（只读元数据，不取节点对象），比 DOM.resolveNode 便宜。
 */
async function probeDomNodes(page, backendNodeIds) {
  const out = new Map();
  const ids = [...backendNodeIds].slice(0, 400); // 防御性上限，极端表单页不至于打爆 CDP
  const CONCURRENCY = 8;
  let cursor = 0;
  const worker = async () => {
    for (;;) {
      const i = cursor++;
      if (i >= ids.length) return;
      const id = ids[i];
      try {
        const res = await page.send('DOM.describeNode', { backendNodeId: id, depth: 0 }, { timeoutMs: 6000 });
        const node = res.node;
        if (!node) continue;
        const attrs = {};
        const flat = node.attributes ?? [];
        for (let k = 0; k + 1 < flat.length; k += 2) attrs[flat[k]] = flat[k + 1];
        out.set(id, {
          nodeName: String(node.nodeName ?? '').toUpperCase(),
          localName: String(node.localName ?? '').toLowerCase(),
          attributes: attrs,
        });
      } catch {
        // 节点可能在探测过程中被移除：当作未知，交给上层按不可编辑处理
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(CONCURRENCY, ids.length || 1) }, worker));
  return out;
}

/**
 * 把 AX 树拍平成"可编号元素"列表。
 * @returns {Promise<{items:Array, total:number, omitted:number, url:string, viewport:{width:number,height:number}}>}
 */
export async function collectElements(
  page,
  { maxNodes = DEFAULT_MAX_NODES, includeRoles = INTERACTIVE_ROLES, force = false } = {},
) {
  const [ax, urlRes, viewport] = await Promise.all([
    fetchAX(page, { force }),
    page.sendRaw('Runtime.evaluate', { expression: 'location.href', returnByValue: true }),
    page.getViewport(),
  ]);
  const url = urlRes.ok ? String(urlRes.result?.result?.value ?? '') : (page.url ?? '');

  // 第一遍：挑出候选（角色白名单 + 有名字的节点 + 去重）
  const seenBackend = new Set();
  const candidates = [];
  let totalEligible = 0;
  const needsProbe = new Set();
  for (const node of ax.nodes) {
    if (node.ignored === true) continue;
    const backendNodeId = node.backendDOMNodeId;
    if (typeof backendNodeId !== 'number') continue;
    if (seenBackend.has(backendNodeId)) continue; // 同一个元素只编号一次
    const props = propsToObject(node);
    const info = extractNode(node, props);
    if (!includeRoles.has(info.role)) continue;
    // 过滤规则：
    //   · 有名字/有值的节点算"有意义"，保留
    //   · 表单控件即使没名字（比如没挂 label 的 checkbox）也必须保留 ——
    //     上层本来就是靠 ref 去操作它，"没名字"不该让它从快照里消失
    //   · 没名字的 button 也先收下：<input type=file> 在 Chrome 里 AX 角色就是
    //     button 且没有可访问名，得等 DOM 探测确认它是文件输入框之后再决定留不留
    const isControl =
      FORM_ROLES.has(info.role) ||
      info.role === 'checkbox' ||
      info.role === 'radio' ||
      info.role === 'switch' ||
      info.role === 'slider' ||
      info.role === 'option';
    const maybeFileInput = info.role === 'button';
    if (!info.name && !info.value && !isControl && !maybeFileInput) continue;
    seenBackend.add(backendNodeId);
    if (isControl || maybeFileInput) needsProbe.add(backendNodeId);
    if (candidates.length >= maxNodes) {
      totalEligible += 1; // 只统计不收集，用于 truncated.omitted
      continue;
    }
    totalEligible += 1;
    candidates.push({ backendNodeId, ...info, nodeId: node.nodeId, isControl, maybeFileInput });
  }

  const probed = needsProbe.size > 0 ? await probeDomNodes(page, needsProbe) : new Map();

  // 第二遍：把"没名字又不是文件输入框也不是表单控件"的候选剔掉
  // （否则每个图标按钮/装饰性 button 都会占一个 ref，白烧 token）
  const kept = candidates.filter((c) => {
    if (c.name || c.value || c.isControl) return true;
    const dom = probed.get(c.backendNodeId);
    const isFile = dom?.nodeName === 'INPUT' && String(dom?.attributes?.type ?? '').toLowerCase() === 'file';
    if (!isFile) totalEligible -= 1;
    return isFile;
  });

  const items = kept.map((c, i) => {
    const dom = probed.get(c.backendNodeId) ?? null;
    const item = { ...c, index: i + 1, dom };
    const type = dom?.attributes?.type;
    if (type) item.inputType = String(type).toLowerCase();
    if (dom?.nodeName === 'INPUT') {
      const t = item.inputType ?? 'text';
      item.password = item.password || t === 'password';
      item.editable = !['checkbox', 'radio', 'button', 'submit', 'reset', 'file', 'image', 'range', 'color', 'hidden'].includes(t);
    } else if (dom?.nodeName === 'TEXTAREA') {
      item.editable = true;
    } else if (dom?.attributes?.contenteditable === '' || dom?.attributes?.contenteditable === 'true') {
      item.editable = true;
    } else if (item.editable == null && FORM_ROLES.has(item.role)) {
      // AX 说它是可编辑角色，但 DOM 探测没拿到（节点可能刚被换掉）：保守按"可编辑"
      item.editable = true;
    }
    if (item.editable === undefined) item.editable = FORM_ROLES.has(item.role);
    return item;
  });

  return {
    items,
    total: totalEligible,
    omitted: Math.max(0, totalEligible - items.length),
    url,
    viewport,
  };
}

/** 把一条元素渲染成一行 snapshot 文本。 */
export function formatElementLine(prefix, item) {
  const parts = [`${prefix}:e${item.index}`, item.role, JSON.stringify(item.name || '')];
  const flags = [];
  if (item.inputType || (item.editable && FORM_ROLES.has(item.role))) {
    flags.push(`type=${item.inputType ?? 'text'}`);
  }
  // 空名的控件给个显式占位，免得输出行里的 "" 让人以为是解析失败
  if (!item.name && item.role !== 'text') flags.push('unnamed');
  if (item.editable) flags.push('edit');
  if (item.password) flags.push('password');
  if (item.disabled) flags.push('disabled');
  if (item.focused) flags.push('focused');
  if (item.checked !== undefined) flags.push(`checked=${item.checked}`);
  if (item.required) flags.push('required');
  if (item.readonly) flags.push('readonly');
  if (item.expanded !== undefined) flags.push(`expanded=${item.expanded}`);
  if (item.hasPopup) flags.push(`popup=${item.hasPopup}`);
  if (item.level) flags.push(`level=${item.level}`);
  if (item.value) flags.push(`value=${JSON.stringify(item.value)}`);
  let line = parts.join(' ');
  if (flags.length) line += ` [${flags.join(' ')}]`;
  return line;
}

/**
 * 快照：文本 + 机器可读结构。
 * 文本首行固定是 `scope=dN url=<url> viewport=WxH`（上层有断言依赖这个格式）。
 */
export async function snapshot(page, { maxNodes = DEFAULT_MAX_NODES, maxChars = DEFAULT_MAX_CHARS } = {}) {
  const collected = await collectElements(page, { maxNodes });
  const prefix = page.refPrefix;
  const viewport = collected.viewport ?? { width: 0, height: 0 };

  // 重建 refMap：编号 = 数组下标 + 1
  page.refMap.clear();
  const refs = [];
  // 首行是"空格分隔的头字段"，所以 URL 里不能留空格（data: URL 的 HTML 里常见），
  // 否则 scope/url/viewport 的边界就糊了。这里只做最小替换，不改动 URL 语义。
  const safeUrl = String(collected.url ?? '').replace(/\s+/g, '%20');
  const lines = [`scope=${prefix} url=${safeUrl} viewport=${viewport.width}x${viewport.height}`];
  let chars = lines[0].length;
  let clipped = false;
  let omitted = collected.omitted;

  for (const item of collected.items) {
    const line = formatElementLine(prefix, item);
    if (chars + line.length + 1 > maxChars) {
      clipped = true;
      omitted += 1;
      continue;
    }
    chars += line.length + 1;
    lines.push(line);
    page.refMap.set(item.index, {
      backendNodeId: item.backendNodeId,
      role: item.role,
      name: item.name,
      value: item.value,
      inputType: item.inputType,
      editable: item.editable,
      password: item.password,
      disabled: item.disabled,
      ref: `${prefix}:e${item.index}`,
    });
    const ref = {
      ref: `${prefix}:e${item.index}`,
      role: item.role,
      name: item.name,
    };
    if (item.value) ref.value = item.value;
    if (item.disabled) ref.disabled = true;
    if (item.focused) ref.focused = true;
    if (item.password) ref.password = true;
    if (item.editable) ref.editable = true;
    if (item.checked !== undefined) ref.checked = item.checked;
    if (item.inputType) ref.inputType = item.inputType;
    if (item.description) ref.description = item.description;
    refs.push(ref);
  }
  if (collected.omitted > 0) clipped = true;

  const result = {
    scope: prefix,
    documentScope: page.doc.scope,
    url: collected.url,
    viewport: { width: viewport.width, height: viewport.height },
    refs,
    truncated: { clipped, omitted },
    text: lines.join('\n'),
    lines: lines.length - 1,
  };
  page._snapshotCache = result;
  return result;
}

// ---------------- find：按 name/role 模糊匹配 + 相关度排序 ----------------

function normalizeQuery(query) {
  // 支持 `role=button 登录` 和 `button "Sign in"` 这类写法
  const raw = String(query ?? '').trim();
  const roleMatch = /\brole\s*[:=]\s*([A-Za-z]+)/.exec(raw);
  let role = roleMatch ? roleMatch[1].toLowerCase() : null;
  let rest = roleMatch ? raw.replace(roleMatch[0], ' ') : raw;
  // 去掉引号但保留引号里的短语
  rest = rest.replace(/["'“”‘’]/g, ' ').replace(/\s+/g, ' ').trim();
  const terms = rest.toLowerCase().split(' ').filter(Boolean);
  return { role, terms, raw };
}

function scoreTerm(haystack, term) {
  if (!haystack) return 0;
  if (haystack === term) return 100;
  if (haystack.startsWith(term)) return 80;
  if (new RegExp(`(^|[^a-z0-9])${escapeRe(term)}`).test(haystack)) return 70;
  if (haystack.includes(term)) return 55;
  // 中文没有词边界，包含就算命中
  return 0;
}

function escapeRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * 模糊查找元素。返回按相关度排序的候选（**多个同样合理就都返回**，让上层判断）。
 * @returns {Promise<{query:string, refs:Array, first:string|null, ambiguous:boolean, scope:string}>}
 */
export async function find(page, query, { maxNodes = DEFAULT_MAX_NODES, limit = 25, minScore = 30 } = {}) {
  const { role, terms } = normalizeQuery(query);
  if (!role && terms.length === 0) {
    return { query: String(query ?? ''), refs: [], first: null, ambiguous: false, scope: page.refPrefix };
  }
  const collected = await collectElements(page, { maxNodes, force: true });
  const prefix = page.refPrefix;
  const scored = [];
  for (const item of collected.items) {
    const name = item.name.toLowerCase();
    const itemRole = item.role.toLowerCase();
    let score = 0;
    if (role) {
      if (itemRole === role) score += 45;
      else if (itemRole.includes(role)) score += 20;
      else continue;
    }
    if (terms.length > 0) {
      let worst = 0;
      let allMatched = true;
      for (const term of terms) {
        const s = Math.max(scoreTerm(name, term), scoreTerm(itemRole, term) * 0.6, scoreTerm(item.value.toLowerCase(), term) * 0.5);
        if (s === 0) {
          allMatched = false;
          break;
        }
        worst = worst === 0 ? s : Math.min(worst, s);
      }
      if (!allMatched) continue;
      score += worst;
    }
    // 没名字又不是表单控件的，降权（避免纯装饰节点挤进结果）
    if (!item.name && !item.editable) score -= 20;
    if (item.disabled) score -= 5;
    if (item.focused) score += 3;
    if (score >= minScore) scored.push({ item, score });
  }
  scored.sort((a, b) => b.score - a.score || a.item.index - b.item.index);
  const top = scored.slice(0, limit);
  const refs = top.map(({ item, score }) => {
    const ref = {
      ref: `${prefix}:e${item.index}`,
      role: item.role,
      name: item.name,
      score: Math.round(score),
    };
    if (item.value) ref.value = item.value;
    if (item.editable) ref.editable = true;
    if (item.disabled) ref.disabled = true;
    if (item.password) ref.password = true;
    if (item.checked !== undefined) ref.checked = item.checked;
    if (item.inputType) ref.inputType = item.inputType;
    return ref;
  });
  // 把命中的 ref 补进 refMap，这样 find() 之后可以直接 click(ref)
  for (const { item } of top) {
    if (!page.refMap.has(item.index)) {
      page.refMap.set(item.index, {
        backendNodeId: item.backendNodeId,
        role: item.role,
        name: item.name,
        value: item.value,
        editable: item.editable,
        ref: `${prefix}:e${item.index}`,
      });
    }
  }
  const ambiguous = refs.length > 1 && refs[0].score - refs[1].score <= 10;
  const result = {
    query: String(query ?? ''),
    scope: prefix,
    refs,
    first: refs.length > 0 ? refs[0].ref : null,
    ambiguous,
  };
  if (scored.length > limit) result.omitted = scored.length - limit;
  return result;
}

/**
 * 用 CSS 选择器定位元素，并把它登记成一个 ref（无 selector 映射机制，
 * 所以这里用"直接 resolve 成 backendNodeId 再塞进 refMap"的办法）。
 * 这是给 waitForElement / fillInput(selector) 用的桥。
 */
export async function registerSelectorRef(page, selector) {
  const res = await page.sendRaw('Runtime.evaluate', {
    expression: `document.querySelector(${JSON.stringify(selector)})`,
    returnByValue: false,
  });
  if (!res.ok) {
    throw buError('BU_CDP_ERROR', `CSS 选择器 ${selector} 求值失败：${res.error.cdpMessage}`, { selector });
  }
  const objectId = res.result?.result?.objectId;
  if (!objectId) return null; // 没找到
  try {
    const described = await page.send('DOM.describeNode', { objectId }, { timeoutMs: 6000 });
    const backendNodeId = described.node?.backendNodeId;
    if (typeof backendNodeId !== 'number') return null;
    // 在最新 AX 树里找它的角色/名字，顺便确认它是不是"可编号元素"
    const ax = await fetchAX(page, { force: true });
    const node = ax.byBackendId.get(backendNodeId) ?? null;
    const props = node ? propsToObject(node) : {};
    const info = node ? extractNode(node, props) : { role: described.node?.nodeName?.toLowerCase() ?? 'generic', name: '' };
    // 分配一个 ref 编号（不回填 snapshot 文本，只保证可用）
    let index = null;
    for (const [k, v] of page.refMap) {
      if (v.backendNodeId === backendNodeId) {
        index = k;
        break;
      }
    }
    if (index == null) {
      index = page.refMap.size + 1;
      while (page.refMap.has(index)) index += 1;
    }
    page.refMap.set(index, {
      backendNodeId,
      role: info.role,
      name: info.name,
      value: info.value,
      editable: info.editable,
      ref: `${page.refPrefix}:e${index}`,
    });
    return {
      ref: `${page.refPrefix}:e${index}`,
      role: info.role,
      name: info.name,
      backendNodeId,
      numbered: INTERACTIVE_ROLES.has(info.role),
    };
  } finally {
    await page.releaseObject(objectId);
  }
}

/** 快照里包含某个 backendNodeId 吗（判断元素是否还在最近的快照内）。 */
export function snapshotHasBackend(page, backendNodeId) {
  if (!page._snapshotCache) return false;
  return page.refMap.size >= 0 && [...page.refMap.values()].some((v) => v.backendNodeId === backendNodeId);
}
