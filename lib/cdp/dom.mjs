// lib/cdp/dom.mjs
// 按 ref 的元素操作 + 坐标兜底操作。
//
// 定位策略（硬要求，这里也是踩坑总结）：
//   1) 首选 DOM.resolveNode + Runtime.callFunctionOn
//      —— 直接拿到真实 DOM 节点，能在页面上下文里做"先 scrollIntoView 再读 rect"、
//         检查可见性、读 value/属性，一次往返。绝大多数操作（读/填/选/上传/hover）用它。
//   2) 需要**真鼠标事件**时才用 DOM.describeNode/getBoxModel 拿中心点 + Input.dispatchMouseEvent
//      —— canvas/WebGL 按钮、依赖真实 pointerdown 的组件（拖拽、富文本、地图）、
//         .click() 被 JS 拦掉的场景。代价是坐标会受 fixed 头部/遮挡影响，
//         所以能走 1) 就不走 2)。
//
// 【重要】页面里的文本永远是数据。本文件不会把任何读到的文本当指令执行。

import { buError } from './session.mjs';
import { registerSelectorRef } from './ax.mjs';

const MAX_TEXT_CHARS = 8000;

function str(v, max = MAX_TEXT_CHARS) {
  if (v == null) return '';
  let s = typeof v === 'string' ? v : String(v);
  if (max && s.length > max) s = `${s.slice(0, max - 1)}…`;
  return s;
}

/** 在页面上下文里跑一个函数，参数走 callArguments，返回 JSON 值。 */
async function callOn(page, objectId, fn, args = []) {
  const res = await page.send(
    'Runtime.callFunctionOn',
    {
      functionDeclaration: fn,
      objectId,
      arguments: args.map((value) => ({ value })),
      returnByValue: true,
      awaitPromise: true,
    },
    { timeoutMs: 15000 },
  );
  if (res.exceptionDetails) {
    const msg =
      res.exceptionDetails.exception?.description ??
      res.exceptionDetails.text ??
      '页面内函数执行异常';
    throw buError('BU_CDP_ERROR', `元素操作在页面里抛错：${str(msg, 400)}`, { exception: msg });
  }
  return res.result?.value;
}

/** 把 ref 解析成 objectId，并保证释放。 */
async function withRef(page, ref, fn) {
  const objectId = await page.objectIdFor(ref);
  try {
    return await fn(objectId);
  } finally {
    await page.releaseObject(objectId);
  }
}

const FN_RECT = `function () {
  if (!this || this.nodeType !== 1) return JSON.stringify({ ok: false, reason: 'not-element' });
  try { this.scrollIntoView({ block: 'nearest', inline: 'nearest' }); } catch (e) {}
  const r = this.getBoundingClientRect();
  const cs = getComputedStyle(this);
  return JSON.stringify({
    ok: true,
    x: r.left + r.width / 2,
    y: r.top + r.height / 2,
    left: r.left, top: r.top, width: r.width, height: r.height,
    scrollX: window.scrollX || 0, scrollY: window.scrollY || 0,
    display: cs.display, visibility: cs.visibility, opacity: cs.opacity,
    docY: r.top + (window.scrollY || 0),
    inViewport: r.width > 0 && r.height > 0,
  });
}`;

async function rectOf(page, objectId) {
  const raw = await callOn(page, objectId, FN_RECT);
  try {
    return JSON.parse(raw ?? '{}');
  } catch {
    return { ok: false, reason: 'rect-parse-failed' };
  }
}

/** 视口归一化（0-1000，和 computer-use 平面一致）→ 像素。 */
export async function toPixels(page, x, y) {
  const vp = await page.getViewport();
  const nx = Number(x);
  const ny = Number(y);
  if (!Number.isFinite(nx) || !Number.isFinite(ny)) {
    throw buError('BU_CDP_ERROR', `坐标必须是数字：(${x}, ${y})`);
  }
  if (nx < 0 || nx > 1000 || ny < 0 || ny > 1000) {
    throw buError('BU_CDP_ERROR', `坐标 ${nx},${ny} 超出 0-1000 归一化范围`);
  }
  return {
    x: Math.round((nx / 1000) * vp.width),
    y: Math.round((ny / 1000) * vp.height),
    viewport: vp,
  };
}

// ---------------- 鼠标 ----------------

async function mouseEvent(page, type, x, y, opts = {}) {
  const params = {
    type,
    x: Math.round(x),
    y: Math.round(y),
    button: opts.button ?? 'left',
    buttons: opts.buttons ?? (type === 'mouseReleased' ? 0 : 1),
    clickCount: opts.clickCount ?? (type === 'mouseMoved' ? 0 : 1),
  };
  if (opts.modifiers) params.modifiers = opts.modifiers;
  if (type === 'mouseWheel') {
    params.deltaX = opts.deltaX ?? 0;
    params.deltaY = opts.deltaY ?? 0;
  }
  return page.send('Input.dispatchMouseEvent', params);
}

async function clickAt(page, x, y, { button = 'left', clicks = 1 } = {}) {
  for (let i = 1; i <= clicks; i += 1) {
    await mouseEvent(page, 'mouseMoved', x, y, { button: 'none', buttons: 0, clickCount: 0 });
    await mouseEvent(page, 'mousePressed', x, y, { button, clickCount: i });
    await mouseEvent(page, 'mouseReleased', x, y, { button, clickCount: i, buttons: 0 });
  }
}

/** 元素中心的像素坐标（用于真鼠标事件）。 */
export async function centerOfRef(page, ref) {
  return withRef(page, ref, async (objectId) => {
    const rect = await rectOf(page, objectId);
    if (!rect.ok) {
      throw buError('BU_REF_STALE', `ref "${ref}" 不是可测量元素（${rect.reason}）`, { ref });
    }
    if (!(rect.width > 0 && rect.height > 0)) {
      // 零尺寸：可能是 display:none 或者还没布局。不猜坐标，直接报错。
      throw buError(
        'BU_REF_STALE',
        `ref "${ref}" 的尺寸是 ${rect.width}x${rect.height}，元素不可见（display=${rect.display} visibility=${rect.visibility}），无法点击`,
        { ref, rect },
      );
    }
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, rect, objectId };
  });
}

// ---------------- 元素操作 ----------------

/**
 * 点击元素。
 * 默认先用页面内 this.click()（快、稳、不受遮挡影响），
 * 如果元素不支持 .click 或者抛错，再退到真实鼠标事件。
 */
export async function click(page, ref, { clicks = 1, button = 'left' } = {}) {
  const info = page.refInfo(ref);
  const nativeResult = await withRef(page, ref, async (objectId) => {
    const raw = await callOn(
      page,
      objectId,
      `function (clicks) {
        if (!this || typeof this.click !== 'function') return JSON.stringify({ ok: false, reason: 'no-click-method' });
        try {
          if (clicks <= 1) { this.click(); } else {
            for (let i = 0; i < clicks; i++) this.click();
          }
          return JSON.stringify({ ok: true });
        } catch (e) { return JSON.stringify({ ok: false, reason: String(e && e.message || e) }); }
      }`,
      [clicks],
    );
    try {
      return JSON.parse(raw ?? '{}');
    } catch {
      return { ok: false, reason: 'parse' };
    }
  });

  if (nativeResult.ok && button === 'left') {
    return { ref, via: 'dom-click', role: info?.role ?? null, name: info?.name ?? null, clicks };
  }
  // 右键 / 双击 / .click() 不可用 → 真鼠标
  const center = await centerOfRef(page, ref);
  await clickAt(page, center.x, center.y, { button, clicks });
  return {
    ref,
    via: 'mouse-event',
    role: info?.role ?? null,
    name: info?.name ?? null,
    clicks,
    x: Math.round(center.x),
    y: Math.round(center.y),
  };
}

export async function hover(page, ref) {
  const center = await centerOfRef(page, ref);
  await mouseEvent(page, 'mouseMoved', center.x, center.y, { button: 'none', buttons: 0, clickCount: 0 });
  return { ref, via: 'mouse-event', x: Math.round(center.x), y: Math.round(center.y) };
}

const FN_PREPARE_INPUT = `function () {
  if (!this) return JSON.stringify({ ok: false, reason: 'no-node' });
  const tag = (this.tagName || '').toUpperCase();
  if (this.isContentEditable) {
    this.focus();
    return JSON.stringify({ ok: true, kind: 'contenteditable', submitOnEnter: false });
  }
  if (tag === 'TEXTAREA') {
    this.focus();
    return JSON.stringify({ ok: true, kind: 'textarea', submitOnEnter: false });
  }
  if (tag === 'INPUT') {
    this.focus();
    try { this.select(); } catch (e) {}
    const t = (this.type || 'text').toLowerCase();
    const submitOnEnter = !['checkbox','radio','file','range','color','button','submit','reset'].includes(t);
    return JSON.stringify({ ok: true, kind: 'input', inputType: t, submitOnEnter });
  }
  if (this.focus) this.focus();
  return JSON.stringify({ ok: true, kind: 'other', submitOnEnter: false });
}`;

/**
 * 往元素里输入文本。
 * 为什么先 select() 再 Input.insertText：insertText 是"输入法式插入"，
 * 不经过按键映射，中文/emoji/换行都能进去；配合 select() 就等于"覆盖原内容"。
 */
export async function type(page, ref, text, { submit = false, clear = true } = {}) {
  const value = String(text ?? '');
  const prep = await withRef(page, ref, async (objectId) => {
    const raw = await callOn(page, objectId, FN_PREPARE_INPUT);
    try {
      return JSON.parse(raw ?? '{}');
    } catch {
      return { ok: false };
    }
  });
  if (!prep.ok) {
    throw buError('BU_REF_STALE', `ref "${ref}" 无法聚焦输入（${prep.reason ?? 'unknown'}）`, { ref });
  }
  if (!clear && prep.kind !== 'contenteditable') {
    // 需要追加而不是覆盖：把光标放到末尾
    await withRef(page, ref, (objectId) =>
      callOn(
        page,
        objectId,
        `function () {
          if (typeof this.setSelectionRange === 'function' && typeof this.value === 'string') {
            const n = this.value.length;
            try { this.setSelectionRange(n, n); } catch (e) {}
          }
          return 1;
        }`,
      ),
    );
  }
  await page.send('Input.insertText', { text: value });
  let submitted = false;
  let submitMode = 'none';
  if (submit && prep.submitOnEnter !== false) {
    submitMode = await decideSubmit(page, ref, prep);
    if (submitMode !== 'none') {
      await pressKey(page, 'Enter');
      submitted = true;
    }
  }
  return { ref, typed: value.length, submitted, submitMode, kind: prep.kind, inputType: prep.inputType ?? null };
}

/**
 * 决定要不要发 Enter 触发提交。
 * 单行搜索框默认 Enter 提交；多行 textarea 里 Enter 是换行，不能乱发。
 */
async function decideSubmit(page, ref, prep) {
  if (prep.kind === 'contenteditable') return 'none';
  const raw = await withRef(page, ref, (objectId) =>
    callOn(
      page,
      objectId,
      `function () {
        const form = this.form || (this.closest ? this.closest('form') : null);
        const taCount = form ? form.querySelectorAll('textarea').length : 0;
        const txtCount = form ? form.querySelectorAll('input[type=text],input[type=search],input:not([type])').length : 0;
        const role = (this.getAttribute && this.getAttribute('role')) || '';
        const type = (this.type || '').toLowerCase();
        return JSON.stringify({ hasForm: !!form, taCount, txtCount, role, type });
      }`,
    ),
  );
  let d = {};
  try {
    d = JSON.parse(raw ?? '{}');
  } catch {
    /* ignore */
  }
  if (prep.kind === 'textarea') return 'none';
  if (d.role === 'searchbox' || d.type === 'search') return 'enter';
  if (d.hasForm && d.taCount === 0 && d.txtCount <= 1) return 'enter';
  if (!d.hasForm && (prep.inputType === 'text' || prep.inputType === 'search' || !prep.inputType)) {
    // 没有 form 的裸输入框：很多 SPA 的搜索框靠 Enter 触发，按 enter 更符合预期
    return 'enter';
  }
  return 'none';
}

/**
 * 下拉选择。
 * 目标可能是原生 <select>，也可能是 ARIA combobox / 自定义 listbox。
 * 原生 select 直接改值并派发 input/change；ARIA 的话要走"点开 + 点选项"，这里明确报错，
 * 让上层用 snapshot + click 手动完成（不假装成功）。
 */
export async function select(page, ref, value) {
  const info = page.refInfo(ref);
  const target = String(value ?? '');
  const result = await withRef(page, ref, (objectId) =>
    callOn(
      page,
      objectId,
      `function (want) {
        const tag = (this.tagName || '').toUpperCase();
        if (tag === 'SELECT') {
          const opts = [...this.options];
          const exact = opts.find(o => o.value === want);
          const textExact = opts.find(o => (o.textContent || '').trim() === want);
          if (exact) { this.value = exact.value; this.dispatchEvent(new Event('input', {bubbles:true})); this.dispatchEvent(new Event('change', {bubbles:true})); return JSON.stringify({ ok: true, selected: exact.value, label: (exact.textContent||'').trim() }); }
          if (textExact) { this.value = textExact.value; this.dispatchEvent(new Event('input', {bubbles:true})); this.dispatchEvent(new Event('change', {bubbles:true})); return JSON.stringify({ ok: true, selected: textExact.value, label: (textExact.textContent||'').trim() }); }
          const partial = opts.filter(o => (o.textContent || '').trim().includes(want) || o.value.includes(want));
          if (partial.length === 1) { const o = partial[0]; this.value = o.value; this.dispatchEvent(new Event('input', {bubbles:true})); this.dispatchEvent(new Event('change', {bubbles:true})); return JSON.stringify({ ok: true, selected: o.value, label: (o.textContent||'').trim(), fuzzy: true }); }
          return JSON.stringify({ ok: false, reason: 'no-match', options: opts.map(o => ({ value: o.value, label: (o.textContent||'').trim() })) });
        }
        return JSON.stringify({ ok: false, reason: 'not-native-select', tag, role: this.getAttribute ? this.getAttribute('role') : null });
      }`,
      [target],
    ),
  );
  let parsed;
  try {
    parsed = JSON.parse(result ?? '{}');
  } catch {
    parsed = { ok: false, reason: 'parse' };
  }
  if (parsed.ok) {
    return { ref, selected: parsed.selected, label: parsed.label ?? null, fuzzy: !!parsed.fuzzy, role: info?.role ?? null };
  }
  if (parsed.reason === 'no-match') {
    const opts = parsed.options ?? [];
    if (opts.length === 0) {
      throw buError('BU_SELECT_AMBIGUOUS', `ref "${ref}" 是 <select> 但没有任何 option，无法选择 "${target}"`, { ref, value: target });
    }
    throw buError(
      'BU_SELECT_AMBIGUOUS',
      `ref "${ref}" 里没有匹配 "${target}" 的选项。可选值：${opts.map((o) => `${o.value}(${o.label})`).join(', ')}`,
      { ref, value: target, options: opts },
    );
  }
  throw buError(
    'BU_SELECT_AMBIGUOUS',
    `ref "${ref}"（role=${info?.role ?? 'unknown'} tag=${parsed.tag ?? '?'}）不是原生 <select>，本库不能安全地"选择"它。请用 snapshot() 看展开后的选项再用 click(ref) 操作。`,
    { ref, value: target, role: info?.role, tag: parsed.tag },
  );
}

const FN_CHECK_STATE = `function () {
  const tag = (this.tagName || '').toUpperCase();
  const role = (this.getAttribute && this.getAttribute('role')) || '';
  const type = (this.type || '').toLowerCase();
  if (tag === 'INPUT' && (type === 'checkbox' || type === 'radio')) {
    return JSON.stringify({ ok: true, checkable: true, checked: !!this.checked, type, indeterminate: !!this.indeterminate });
  }
  if (role === 'checkbox' || role === 'radio' || role === 'switch' || role === 'menuitemcheckbox' || role === 'menuitemradio') {
    const checked = this.getAttribute('aria-checked');
    return JSON.stringify({ ok: true, checkable: true, checked: checked === 'true', aria: true, type: role, indeterminate: checked === 'mixed' });
  }
  return JSON.stringify({ ok: false, checkable: false, tag, type, role });
}`;

async function setChecked(page, ref, want) {
  const state = await withRef(page, ref, async (objectId) => {
    const raw = await callOn(page, objectId, FN_CHECK_STATE);
    try {
      return JSON.parse(raw ?? '{}');
    } catch {
      return { ok: false };
    }
  });
  if (!state.checkable) {
    throw buError(
      'BU_REF_SHAPE',
      `ref "${ref}" 不是可勾选的控件（tag=${state.tag} type=${state.type} role=${state.role ?? ''}）`,
      { ref },
    );
  }
  if (state.checked === want) {
    // 幂等：已经是目标状态就不点，避免把 toggle 类控件点反
    return { ref, changed: false, checked: state.checked, type: state.type };
  }
  const res = await click(page, ref, { clicks: 1, button: 'left' });
  const after = await withRef(page, ref, async (objectId) => {
    const raw = await callOn(page, objectId, FN_CHECK_STATE);
    try {
      return JSON.parse(raw ?? '{}');
    } catch {
      return {};
    }
  });
  return { ref, changed: true, checked: after.checked ?? null, type: state.type, via: res.via };
}

export function check(page, ref) {
  return setChecked(page, ref, true);
}

export function uncheck(page, ref) {
  return setChecked(page, ref, false);
}

/** 读属性/文本/值。attrName 支持一批常用别名。 */
export async function get(page, ref, attrName = 'text') {
  const attr = String(attrName ?? 'text').toLowerCase();
  return withRef(page, ref, (objectId) =>
    callOn(
      page,
      objectId,
      `function (attr) {
        const el = this;
        if (!el || el.nodeType !== 1) return null;
        switch (attr) {
          case 'text': case 'innertext': return el.innerText != null ? el.innerText : el.textContent;
          case 'textcontent': return el.textContent;
          case 'value': return (el.value != null) ? el.value : (el.getAttribute ? el.getAttribute('value') : null);
          case 'href': return el.href || (el.getAttribute ? el.getAttribute('href') : null);
          case 'src': return el.src || (el.getAttribute ? el.getAttribute('src') : null);
          case 'html': case 'innerhtml': return el.innerHTML;
          case 'name': return el.getAttribute ? el.getAttribute('name') : null;
          case 'id': return el.id || null;
          case 'class': case 'classname': return el.className != null ? String(el.className) : null;
          case 'type': return el.type != null ? String(el.type) : (el.getAttribute ? el.getAttribute('type') : null);
          case 'placeholder': return el.placeholder != null ? el.placeholder : (el.getAttribute ? el.getAttribute('placeholder') : null);
          case 'aria-label': case 'arialabel': return el.getAttribute ? el.getAttribute('aria-label') : null;
          case 'alt': return el.alt != null ? el.alt : (el.getAttribute ? el.getAttribute('alt') : null);
          case 'title': return el.title != null ? el.title : (el.getAttribute ? el.getAttribute('title') : null);
          case 'checked': return el.checked != null ? !!el.checked : (el.getAttribute ? el.getAttribute('aria-checked') : null);
          case 'disabled': return el.disabled != null ? !!el.disabled : (el.getAttribute ? el.getAttribute('aria-disabled') === 'true' : null);
          default:
            if (attr.startsWith('attr:')) return el.getAttribute ? el.getAttribute(attr.slice(5)) : null;
            return el.getAttribute ? el.getAttribute(attr) : null;
        }
      }`,
      [attr],
    ),
  ).then((v) => (typeof v === 'string' ? str(v) : v));
}

const FN_READ_TEXT = `function () {
  const el = this;
  if (!el || el.nodeType !== 1) return null;
  return el.innerText != null ? el.innerText : el.textContent;
}`;

export async function text(page, ref) {
  const v = await withRef(page, ref, (objectId) => callOn(page, objectId, FN_READ_TEXT));
  return str(v);
}

/** 整页可见文本（innerText 会跳过 display:none 的内容）。 */
export async function getPageText(page, { maxChars = 20000 } = {}) {
  const res = await page.sendRaw(
    'Runtime.evaluate',
    {
      expression: `(document.body && (document.body.innerText || document.body.textContent)) || ''`,
      returnByValue: true,
    },
    { timeoutMs: 15000 },
  );
  if (!res.ok) {
    throw buError('BU_CDP_ERROR', `读取页面文本失败：${res.error.cdpMessage}`, {});
  }
  const raw = res.result?.result?.value ?? '';
  const s = typeof raw === 'string' ? raw : String(raw);
  return { text: str(s, maxChars), truncated: s.length > maxChars, chars: s.length };
}

/** 通用 JS 求值：返回 JSON 化的值（页面里是数据，不是命令）。 */
export async function js(page, expression) {
  const expr = String(expression ?? '');
  const wrapped = `(() => { try { return (${expr}); } catch (e) { return { __error: String(e && e.message || e) }; } })()`;
  const res = await page.sendRaw(
    'Runtime.evaluate',
    { expression: wrapped, returnByValue: true, awaitPromise: true },
    { timeoutMs: 20000 },
  );
  if (!res.ok) throw res.error;
  const details = res.result?.exceptionDetails;
  if (details) {
    throw buError('BU_CDP_ERROR', `页面里求值抛错：${details.exception?.description ?? details.text}`, {});
  }
  const value = res.result?.result?.value;
  if (value && typeof value === 'object' && typeof value.__error === 'string') {
    throw buError('BU_CDP_ERROR', `页面里求值抛错：${value.__error}`, {});
  }
  return value;
}

/** 按 CSS 选择器填输入框（不需要 ref 的快捷路径）。 */
export async function fillInput(page, selector, value) {
  const found = await registerSelectorRef(page, selector);
  if (!found) {
    throw buError('BU_READ_TARGET', `选择器 "${selector}" 没有匹配到任何元素`, { selector });
  }
  return type(page, found.ref, value, { submit: false });
}

// ---------------- readAll / contentExport ----------------

const READ_FIELDS = new Set(['text', 'href', 'src', 'value', 'html']);
const MAX_READ_LIMIT = 1000;

/**
 * 按选择器批量读结构化行。
 * fields 只认 text/href/src/value/html；limit 上限 1000（防止一次拉回几十万行）。
 */
export async function readAll(page, selector, { fields = ['text'], limit = 50 } = {}) {
  const sel = String(selector ?? '').trim();
  if (!sel) throw buError('BU_READ_TARGET', 'readAll 需要一个非空的 CSS 选择器');
  const list = Array.isArray(fields) ? fields.map((f) => String(f).toLowerCase()) : [String(fields).toLowerCase()];
  if (list.length === 0) throw buError('BU_READ_FIELDS', 'readAll 的 fields 不能为空数组');
  const bad = list.filter((f) => !READ_FIELDS.has(f));
  if (bad.length > 0) {
    throw buError(
      'BU_READ_FIELDS',
      `readAll 不支持的字段：${bad.join(', ')}；只支持 ${[...READ_FIELDS].join('/')}`,
      { badFields: bad, allowed: [...READ_FIELDS] },
    );
  }
  const lim = Number(limit);
  if (!Number.isInteger(lim) || lim <= 0) {
    throw buError('BU_READ_LIMIT', `readAll 的 limit 必须是正整数，收到 ${limit}`, { limit });
  }
  if (lim > MAX_READ_LIMIT) {
    throw buError('BU_READ_LIMIT', `readAll 的 limit 上限是 ${MAX_READ_LIMIT}，收到 ${lim}`, { limit: lim });
  }
  const res = await page.sendRaw(
    'Runtime.evaluate',
    {
      expression: `(() => {
        const sel = ${JSON.stringify(sel)};
        const fields = ${JSON.stringify(list)};
        let els;
        try { els = document.querySelectorAll(sel); } catch (e) { return { __error: 'bad-selector: ' + String(e && e.message || e) }; }
        const total = els.length;
        const rows = [];
        for (let i = 0; i < els.length && rows.length < ${lim}; i++) {
          const el = els[i];
          const row = { index: i };
          for (const f of fields) {
            if (f === 'text') row.text = (el.innerText != null ? el.innerText : el.textContent || '').trim();
            else if (f === 'href') row.href = el.href || (el.getAttribute ? el.getAttribute('href') : null);
            else if (f === 'src') row.src = el.src || (el.getAttribute ? el.getAttribute('src') : null);
            else if (f === 'value') row.value = el.value != null ? el.value : null;
            else if (f === 'html') row.html = (el.innerHTML || '').slice(0, 2000);
          }
          rows.push(row);
        }
        return { __ok: true, total, rows };
      })()`,
      returnByValue: true,
    },
    { timeoutMs: 20000 },
  );
  if (!res.ok) throw res.error;
  const value = res.result?.result?.value;
  if (!value || value.__error) {
    throw buError('BU_READ_TARGET', `readAll 选择器无效：${value?.__error ?? '未知错误'}`, { selector: sel });
  }
  return {
    selector: sel,
    fields: list,
    total: value.total,
    returned: value.rows.length,
    truncated: value.total > value.rows.length,
    rows: value.rows,
  };
}

/** 页面正文导出：标题 + 标题层级 + 段落。给上层做"读文章"用。 */
export async function contentExport(page, { maxChars = 5000 } = {}) {
  const lim = Number(maxChars);
  if (!Number.isInteger(lim) || lim < 200) {
    throw buError('BU_EXPORT_LIMIT', `contentExport 的 maxChars 必须是不小于 200 的整数，收到 ${maxChars}`, {
      maxChars,
    });
  }
  const res = await page.sendRaw(
    'Runtime.evaluate',
    {
      expression: `(() => {
        const q = (s) => [...document.querySelectorAll(s)];
        const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
        const headings = q('h1,h2,h3,h4,h5,h6').map(h => ({ level: Number(h.tagName[1]), text: clean(h.innerText || h.textContent).slice(0, 300) })).filter(h => h.text);
        const paragraphs = q('p,li,blockquote,pre,td').map(p => clean(p.innerText || p.textContent)).filter(t => t.length > 0).map(t => t.slice(0, 1000));
        return { __ok: true, title: document.title || '', url: location.href, headings, paragraphs };
      })()`,
      returnByValue: true,
    },
    { timeoutMs: 20000 },
  );
  if (!res.ok) throw res.error;
  const v = res.result?.result?.value ?? {};
  let budget = lim;
  const headings = [];
  for (const h of v.headings ?? []) {
    if (budget - h.text.length < 0) break;
    budget -= h.text.length;
    headings.push(h);
  }
  const paragraphs = [];
  for (const p of v.paragraphs ?? []) {
    if (budget <= 0) break;
    const take = p.length > budget ? p.slice(0, budget) : p;
    budget -= take.length;
    paragraphs.push(take);
  }
  const truncated =
    headings.length < (v.headings?.length ?? 0) || paragraphs.length < (v.paragraphs?.length ?? 0);
  return {
    title: v.title ?? '',
    url: v.url ?? '',
    headings,
    paragraphs,
    truncated,
    chars: lim - Math.max(0, budget),
  };
}

/** 页面信息。 */
export async function pageInfo(page) {
  const res = await page.sendRaw(
    'Runtime.evaluate',
    {
      expression: `JSON.stringify({u: location.href, t: document.title || '', r: document.readyState, v: document.visibilityState, f: document.hasFocus()})`,
      returnByValue: true,
    },
    { timeoutMs: 8000 },
  );
  let d = {};
  if (res.ok) {
    try {
      d = JSON.parse(res.result?.result?.value ?? '{}');
    } catch {
      /* 页面把 JSON 弄坏了，用 CDP 侧的信息兜底 */
    }
  }
  const viewport = await page.getViewport({ refresh: true });
  const tab = await page.browser.targetInfo(page.targetId);
  return {
    url: d.u ?? tab?.url ?? page.url,
    title: d.t ?? tab?.title ?? page.title,
    viewport: { width: viewport.width, height: viewport.height },
    readyState: d.r ?? 'unknown',
    scope: page.refPrefix,
    documentScope: page.doc.scope,
    visibility: d.v ?? 'unknown',
    focused: !!d.f,
  };
}

/** 上传文件（绝对路径）。 */
export async function upload(page, ref, absolutePath) {
  const path = String(absolutePath ?? '');
  if (!path) throw buError('BU_CDP_ERROR', 'upload 需要绝对文件路径');
  const backendNodeId = page.backendNodeIdFor(ref);
  const described = await page.send('DOM.describeNode', { backendNodeId, depth: 0 }, { timeoutMs: 8000 });
  const node = described.node ?? {};
  const nodeName = String(node.nodeName ?? '').toUpperCase();
  // attributes 是 [name, value, name, value, ...] 的扁平数组，必须两两步进地读
  const attrs = {};
  const flat = node.attributes ?? [];
  for (let i = 0; i + 1 < flat.length; i += 2) attrs[flat[i]] = flat[i + 1];
  const type = String(attrs.type ?? '').toLowerCase();
  if (nodeName !== 'INPUT' || type !== 'file') {
    throw buError(
      'BU_REF_SHAPE',
      `ref "${ref}" 指向 <${nodeName.toLowerCase()}${type ? ` type=${type}` : ''}>，不是 <input type=file>，无法上传文件。`,
      { ref, nodeName, inputType: type || null },
    );
  }
  try {
    await page.send('DOM.setFileInputFiles', { files: [path], backendNodeId }, { timeoutMs: 15000 });
  } catch (err) {
    throw buError('BU_CDP_ERROR', `设置文件输入失败：${err.message}`, { ref, path });
  }
  return { ref, path, inputType: type };
}

// ---------------- 坐标兜底（canvas / WebGL） ----------------

export async function clickXy(page, x, y, { button = 'left', clicks = 1 } = {}) {
  const p = await toPixels(page, x, y);
  await clickAt(page, p.x, p.y, { button, clicks });
  return { x: p.x, y: p.y, normalized: { x: Number(x), y: Number(y) } };
}

export async function leftDouble(page, x, y) {
  const p = await toPixels(page, x, y);
  await mouseEvent(page, 'mouseMoved', p.x, p.y, { button: 'none', buttons: 0, clickCount: 0 });
  await mouseEvent(page, 'mousePressed', p.x, p.y, { button: 'left', clickCount: 1 });
  await mouseEvent(page, 'mouseReleased', p.x, p.y, { button: 'left', clickCount: 1, buttons: 0 });
  await mouseEvent(page, 'mousePressed', p.x, p.y, { button: 'left', clickCount: 2 });
  await mouseEvent(page, 'mouseReleased', p.x, p.y, { button: 'left', clickCount: 2, buttons: 0 });
  return { x: p.x, y: p.y, clicks: 2 };
}

export async function rightSingle(page, x, y) {
  const p = await toPixels(page, x, y);
  await mouseEvent(page, 'mouseMoved', p.x, p.y, { button: 'none', buttons: 0, clickCount: 0 });
  await mouseEvent(page, 'mousePressed', p.x, p.y, { button: 'right', clickCount: 1 });
  await mouseEvent(page, 'mouseReleased', p.x, p.y, { button: 'right', clickCount: 1, buttons: 0 });
  return { x: p.x, y: p.y, button: 'right' };
}

export async function drag(page, x1, y1, x2, y2, { steps = 12 } = {}) {
  const a = await toPixels(page, x1, y1);
  const b = await toPixels(page, x2, y2);
  await mouseEvent(page, 'mouseMoved', a.x, a.y, { button: 'none', buttons: 0, clickCount: 0 });
  await mouseEvent(page, 'mousePressed', a.x, a.y, { button: 'left', clickCount: 1 });
  const n = Math.max(2, Number(steps) || 12);
  for (let i = 1; i <= n; i += 1) {
    const t = i / n;
    await mouseEvent(page, 'mouseMoved', a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t, {
      button: 'left',
      buttons: 1,
      clickCount: 0,
    });
  }
  await mouseEvent(page, 'mouseReleased', b.x, b.y, { button: 'left', clickCount: 1, buttons: 0 });
  return { from: { x: a.x, y: a.y }, to: { x: b.x, y: b.y }, steps: n };
}

export async function scroll(page, x, y, direction, { amount = 3 } = {}) {
  const dir = String(direction ?? '').toLowerCase();
  const sign = dir === 'up' || dir === 'down' ? 1 : 1;
  const unit = 120;
  const n = Math.max(1, Number(amount) || 1);
  let dx = 0;
  let dy = 0;
  if (dir === 'up') dy = -unit * n;
  else if (dir === 'down') dy = unit * n;
  else if (dir === 'left') dx = -unit * n;
  else if (dir === 'right') dx = unit * n;
  else throw buError('BU_CDP_ERROR', `scroll 的 direction 只能是 up/down/left/right，收到 "${direction}"`);
  const p = await toPixels(page, x, y);
  await mouseEvent(page, 'mouseMoved', p.x, p.y, { button: 'none', buttons: 0, clickCount: 0 });
  await mouseEvent(page, 'mouseWheel', p.x, p.y, {
    button: 'none',
    buttons: 0,
    clickCount: 0,
    deltaX: dx * sign,
    deltaY: dy * sign,
  });
  return { x: p.x, y: p.y, direction: dir, amount: n, deltaX: dx, deltaY: dy };
}

// ---------------- 键盘 ----------------

/** 常用键名 → CDP key 参数。没收录的走"单字符"分支或直接当 DOM code 用。 */
const KEY_TABLE = {
  enter: { key: 'Enter', code: 'Enter', keyCode: 13, text: '\r' },
  return: { key: 'Enter', code: 'Enter', keyCode: 13, text: '\r' },
  tab: { key: 'Tab', code: 'Tab', keyCode: 9 },
  escape: { key: 'Escape', code: 'Escape', keyCode: 27 },
  esc: { key: 'Escape', code: 'Escape', keyCode: 27 },
  backspace: { key: 'Backspace', code: 'Backspace', keyCode: 8 },
  delete: { key: 'Delete', code: 'Delete', keyCode: 46 },
  arrowup: { key: 'ArrowUp', code: 'ArrowUp', keyCode: 38 },
  arrowdown: { key: 'ArrowDown', code: 'ArrowDown', keyCode: 40 },
  arrowleft: { key: 'ArrowLeft', code: 'ArrowLeft', keyCode: 37 },
  arrowright: { key: 'ArrowRight', code: 'ArrowRight', keyCode: 39 },
  up: { key: 'ArrowUp', code: 'ArrowUp', keyCode: 38 },
  down: { key: 'ArrowDown', code: 'ArrowDown', keyCode: 40 },
  left: { key: 'ArrowLeft', code: 'ArrowLeft', keyCode: 37 },
  right: { key: 'ArrowRight', code: 'ArrowRight', keyCode: 39 },
  home: { key: 'Home', code: 'Home', keyCode: 36 },
  end: { key: 'End', code: 'End', keyCode: 35 },
  pageup: { key: 'PageUp', code: 'PageUp', keyCode: 33 },
  pagedown: { key: 'PageDown', code: 'PageDown', keyCode: 34 },
  space: { key: ' ', code: 'Space', keyCode: 32, text: ' ' },
  f1: { key: 'F1', code: 'F1', keyCode: 112 },
  f2: { key: 'F2', code: 'F2', keyCode: 113 },
  f3: { key: 'F3', code: 'F3', keyCode: 114 },
  f4: { key: 'F4', code: 'F4', keyCode: 115 },
  f5: { key: 'F5', code: 'F5', keyCode: 116 },
  f6: { key: 'F6', code: 'F6', keyCode: 117 },
  f7: { key: 'F7', code: 'F7', keyCode: 118 },
  f8: { key: 'F8', code: 'F8', keyCode: 119 },
  f9: { key: 'F9', code: 'F9', keyCode: 120 },
  f10: { key: 'F10', code: 'F10', keyCode: 121 },
  f11: { key: 'F11', code: 'F11', keyCode: 122 },
  f12: { key: 'F12', code: 'F12', keyCode: 123 },
};

const MODIFIER_TABLE = {
  ctrl: { key: 'Control', code: 'ControlLeft', keyCode: 17, bit: 2 },
  control: { key: 'Control', code: 'ControlLeft', keyCode: 17, bit: 2 },
  alt: { key: 'Alt', code: 'AltLeft', keyCode: 18, bit: 1 },
  option: { key: 'Alt', code: 'AltLeft', keyCode: 18, bit: 1 },
  shift: { key: 'Shift', code: 'ShiftLeft', keyCode: 16, bit: 8 },
  meta: { key: 'Meta', code: 'MetaLeft', keyCode: 91, bit: 4 },
  cmd: { key: 'Meta', code: 'MetaLeft', keyCode: 91, bit: 4 },
  win: { key: 'Meta', code: 'MetaLeft', keyCode: 91, bit: 4 },
};

function resolveKey(token) {
  const t = String(token ?? '').trim();
  if (!t) return null;
  const lower = t.toLowerCase();
  if (KEY_TABLE[lower]) return { ...KEY_TABLE[lower] };
  if (t.length === 1) {
    const upper = t.toUpperCase();
    const code = /[a-z]/i.test(t) ? `Key${upper}` : /[0-9]/.test(t) ? `Digit${t}` : null;
    return { key: t, code: code ?? undefined, keyCode: t.charCodeAt(0), text: t };
  }
  // 认不出来的：当它是 DOM code 直接发（比如 'MediaPlayPause'）
  return { key: t, code: t, keyCode: 0 };
}

/** 按一次键。text 只在没有修饰键按下时给，否则浏览器会当成字符插入。 */
export async function pressKey(page, key) {
  const k = resolveKey(key);
  if (!k) throw buError('BU_CDP_ERROR', `无法识别的按键：${key}`);
  const base = { key: k.key, code: k.code, windowsVirtualKeyCode: k.keyCode, nativeVirtualKeyCode: k.keyCode };
  await page.send('Input.dispatchKeyEvent', { type: 'keyDown', ...base, text: k.text ?? undefined });
  await page.send('Input.dispatchKeyEvent', { type: 'keyUp', ...base });
  return { key: k.key, code: k.code ?? null };
}

/**
 * 组合键。支持 hotkey('ctrl','shift','a') 或 hotkey('ctrl+shift+a')。
 * 修饰键按位或进 modifiers，Chrome 靠它把 click 变成 ctrl+click 之类。
 */
export async function hotkey(page, ...keys) {
  const flat = keys
    .flatMap((k) => String(k ?? '').split('+'))
    .map((s) => s.trim())
    .filter(Boolean);
  if (flat.length === 0) throw buError('BU_CDP_ERROR', 'hotkey 至少需要一个按键');
  const mods = [];
  let main = null;
  for (const token of flat) {
    const m = MODIFIER_TABLE[token.toLowerCase()];
    if (m && !main) {
      mods.push(m);
    } else {
      main = token;
    }
  }
  let modifierBits = 0;
  for (const m of mods) modifierBits |= m.bit;
  for (const m of mods) {
    await page.send('Input.dispatchKeyEvent', {
      type: 'keyDown',
      key: m.key,
      code: m.code,
      windowsVirtualKeyCode: m.keyCode,
      nativeVirtualKeyCode: m.keyCode,
      modifiers: modifierBits,
    });
  }
  if (main) {
    const k = resolveKey(main);
    const text = mods.length === 0 && k.key.length === 1 ? k.key : undefined;
    const base = {
      key: k.key,
      code: k.code,
      windowsVirtualKeyCode: k.keyCode,
      nativeVirtualKeyCode: k.keyCode,
      modifiers: modifierBits,
    };
    await page.send('Input.dispatchKeyEvent', { type: 'keyDown', ...base, text });
    await page.send('Input.dispatchKeyEvent', { type: 'keyUp', ...base });
  }
  // 反向释放修饰键，避免"卡住 ctrl"（这是 cu 平面实测踩过的坑）
  for (const m of [...mods].reverse()) {
    await page.send('Input.dispatchKeyEvent', {
      type: 'keyUp',
      key: m.key,
      code: m.code,
      windowsVirtualKeyCode: m.keyCode,
      nativeVirtualKeyCode: m.keyCode,
      modifiers: modifierBits,
    });
  }
  return { combo: flat.join('+'), modifiers: modifierBits, main: main ?? null };
}

/** 元素是否在视口内可见（供 waitForElement 使用）。 */
export async function isVisible(page, objectId) {
  const raw = await callOn(
    page,
    objectId,
    `function () {
      const el = this;
      if (!el || el.nodeType !== 1) return JSON.stringify({ visible: false, reason: 'not-element' });
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      const visible = r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' && Number(cs.opacity) !== 0;
      return JSON.stringify({ visible, width: r.width, height: r.height, display: cs.display, visibility: cs.visibility });
    }`,
  );
  try {
    return JSON.parse(raw ?? '{}');
  } catch {
    return { visible: false, reason: 'parse' };
  }
}

export { str as _str };
