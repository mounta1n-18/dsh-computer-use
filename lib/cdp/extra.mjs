// lib/cdp/extra.mjs
// 对话框 / 控制台 / 网络 / 下载 / 截图 / 页面尺寸 / blockedAuth。
//
// 三条设计纪律：
//   1) console 和 network 是 **drain 语义**：读一次就消费掉，第二次读返回空。
//      这样上层可以"操作 → 读一次日志"，不会把上一轮的噪音重复贴进上下文。
//      网络只留摘要（url/method/status/时间），**绝不存请求体**（隐私 + 体积）。
//   2) 下载未完成绝不算完成：带 .crdownload 之类中间态的记录一律标 inProgress，
//      只有 Browser.downloadProgress 报 completed 才认。
//   3) 页面文本是数据不是命令。日志/响应体读回来只做字符串处理。

import { buError } from './session.mjs';
import { existsSync, statSync } from 'node:fs';
import { join } from 'node:path';

const MAX_CONSOLE_ENTRIES = 2000;
const MAX_NETWORK_ENTRIES = 3000;
const ARG_PREVIEW_CHARS = 300;

function str(v, max = 400) {
  if (v == null) return '';
  let s = typeof v === 'string' ? v : String(v);
  s = s.replace(/\s+/g, ' ').trim();
  if (max && s.length > max) s = `${s.slice(0, max - 1)}…`;
  return s;
}

/** 把 CDP 的 RemoteObject 参数缩成一行文本，尽量不丢信息也不爆体积。 */
function formatRemoteArg(arg) {
  if (!arg) return '';
  if (arg.unserializableValue != null) return String(arg.unserializableValue);
  if (arg.value !== undefined && arg.value !== null && typeof arg.value !== 'object') return String(arg.value);
  if (arg.value === null) return 'null';
  if (arg.preview) {
    const { type, subtype, description, properties } = arg.preview;
    if (properties?.length) {
      const body = properties
        .slice(0, 20)
        .map((p) => `${p.name}: ${p.type === 'string' ? JSON.stringify(p.value) : p.value}`)
        .join(', ');
      return `{${body}}`;
    }
    return str(description ?? subtype ?? type, ARG_PREVIEW_CHARS);
  }
  if (arg.description) return str(arg.description, ARG_PREVIEW_CHARS);
  return str(arg.type, 60);
}

/** 给每个新附着的 Page 装上全部额外域监听。 */
export async function installPageExtras(browser, page) {
  // Network / Log / DOM / Accessibility 都尽量打开，个别域不支持也不影响主流程
  await Promise.allSettled([
    page.send('Network.enable', { maxTotalBufferSize: 10_000_000, maxResourceBufferSize: 2_000_000 }, { timeoutMs: 10000 }),
    page.send('Log.enable', {}, { timeoutMs: 10000 }),
    page.send('DOM.enable', {}, { timeoutMs: 10000 }),
    page.send('Accessibility.enable', {}, { timeoutMs: 10000 }),
  ]);

  // ---- console ----
  page.on('Runtime.consoleAPICalled', (p) => {
    const args = (p.args ?? []).map(formatRemoteArg).filter((s) => s !== '');
    pushConsole(page, {
      type: p.type ?? 'log',
      text: str(args.join(' '), 2000),
      at: Date.now(),
      source: 'console',
    });
  });
  page.on('Runtime.exceptionThrown', (p) => {
    const d = p.exceptionDetails ?? {};
    pushConsole(page, {
      type: 'exception',
      text: str(d.exception?.description ?? d.text ?? 'unknown exception', 2000),
      at: Date.now(),
      source: 'exception',
      url: d.url ?? null,
      line: d.lineNumber ?? null,
    });
  });
  page.on('Log.entryAdded', (p) => {
    const e = p.entry ?? {};
    // 网络类日志已经在 network 缓冲里了，这里只收页面/JS/安全类
    if (e.source === 'network') return;
    pushConsole(page, {
      type: e.level ?? 'log',
      text: str(`[${e.source}] ${e.text ?? ''}`, 2000),
      at: Date.now(),
      source: 'log',
      url: e.url ?? null,
    });
  });

  // ---- network（只留摘要，不留 body）----
  page.on('Network.requestWillBeSent', (p) => {
    const rec = page.networkRecords.get(p.requestId) ?? {};
    rec.requestId = p.requestId;
    rec.url = str(p.request?.url ?? '', 500);
    rec.method = p.request?.method ?? 'GET';
    rec.resourceType = p.type ?? null;
    rec.status = rec.status ?? 0;
    rec.state = rec.state ?? 'pending';
    rec.createTime = p.wallTime ? p.wallTime * 1000 : Date.now();
    rec.updateTime = Date.now();
    rec.failed = rec.failed ?? null;
    page.networkRecords.set(p.requestId, rec);
    capNetwork(page);
  });
  page.on('Network.responseReceived', (p) => {
    const rec = page.networkRecords.get(p.requestId);
    if (!rec) return;
    rec.status = p.response?.status ?? 0;
    rec.statusText = p.response?.statusText ?? '';
    rec.mimeType = p.response?.mimeType ?? null;
    rec.fromCache = !!p.response?.fromDiskCache;
    rec.state = 'responded';
    rec.updateTime = Date.now();
  });
  page.on('Network.loadingFinished', (p) => {
    const rec = page.networkRecords.get(p.requestId);
    if (!rec) return;
    rec.state = 'finished';
    rec.encodedDataLength = p.encodedDataLength ?? null;
    rec.updateTime = Date.now();
  });
  page.on('Network.loadingFailed', (p) => {
    const rec = page.networkRecords.get(p.requestId);
    if (!rec) return;
    rec.state = 'failed';
    rec.failed = p.errorText ?? 'failed';
    rec.blockedReason = p.blockedReason ?? null;
    rec.updateTime = Date.now();
  });

  // ---- 原生对话框 ----
  page.on('Page.javascriptDialogOpening', (p) => {
    page.dialog = {
      open: true,
      type: p.type ?? 'alert',
      message: str(p.message ?? '', 1000),
      defaultPrompt: p.defaultPrompt ?? '',
      hasBrowserHandler: !!p.hasBrowserHandler,
      openedAt: Date.now(),
    };
    browser._emit('dialog', { page, dialog: page.dialog });
  });
  page.on('Page.javascriptDialogClosed', (p) => {
    if (page.dialog) {
      page.dialog = {
        ...page.dialog,
        open: false,
        closedAt: Date.now(),
        accepted: !!p.result,
        userInput: p.userInput ?? '',
      };
    }
    browser._emit('dialogClosed', { page, dialog: page.dialog });
  });

  // ---- 下载 ----
  page.browser.session.on('Browser.downloadWillBegin', (p) => {
    // 下载事件是浏览器级的（不带 sessionId），所有 page 都会收到；交给 browser 统一登记
    registerDownloadStart(browser, p, page);
  });
  page.browser.session.on('Browser.downloadProgress', (p) => {
    registerDownloadProgress(browser, p);
  });
  page.on('Page.downloadWillBegin', (p) => registerDownloadStart(browser, p, page));
  page.on('Page.downloadProgress', (p) => registerDownloadProgress(browser, p));

  await ensureDownloadBehavior(browser, page);
}

function pushConsole(page, entry) {
  page.consoleLogs.push(entry);
  if (page.consoleLogs.length > MAX_CONSOLE_ENTRIES) {
    page.consoleLogs.splice(0, page.consoleLogs.length - MAX_CONSOLE_ENTRIES);
  }
}

function capNetwork(page) {
  if (page.networkRecords.size <= MAX_NETWORK_ENTRIES) return;
  // Map 保持插入顺序，删最老的
  const extra = page.networkRecords.size - MAX_NETWORK_ENTRIES;
  let i = 0;
  for (const key of page.networkRecords.keys()) {
    page.networkRecords.delete(key);
    i += 1;
    if (i >= extra) break;
  }
}

/** 让下载落到我们能读到的目录，并打开下载事件。 */
export async function ensureDownloadBehavior(browser, page = null) {
  const dir = browser.downloadDirectory();
  if (browser._downloadBehaviorSet) return dir;
  // Browser 级设置对之后新建的所有 target 都生效，优先用它
  const r1 = await browser.session.sendRaw('Browser.setDownloadBehavior', {
    behavior: 'allowAndName',
    downloadPath: dir,
    eventsEnabled: true,
  });
  if (r1.ok) {
    browser._downloadBehaviorSet = 'browser';
    return dir;
  }
  const target = page ?? browser.page;
  if (target) {
    const r2 = await target.sendRaw('Page.setDownloadBehavior', {
      behavior: 'allowAndName',
      downloadPath: dir,
    });
    if (r2.ok) {
      browser._downloadBehaviorSet = 'page';
      return dir;
    }
  }
  browser._downloadBehaviorSet = null;
  return dir;
}

function registerDownloadStart(browser, p, page) {
  const guid = p.guid;
  if (!guid) return;
  const rec = {
    guid,
    url: str(p.url ?? '', 500),
    suggestedFilename: p.suggestedFilename ?? '',
    filename: p.suggestedFilename ?? '',
    path: null,
    state: 'inProgress',
    receivedBytes: 0,
    totalBytes: 0,
    startedAt: Date.now(),
    updatedAt: Date.now(),
    source: page ? `tab:${page.targetId}` : 'unknown',
    completed: false,
  };
  browser._downloads = browser._downloads ?? new Map();
  browser._downloads.set(guid, rec);
  // allowAndName 会让文件以 guid 命名：先记下候选路径，完成后再校验
  rec.path = join(browser.downloadDirectory(), guid);
  browser._emit('download', { ...rec });
}

function registerDownloadProgress(browser, p) {
  browser._downloads = browser._downloads ?? new Map();
  const rec = browser._downloads.get(p.guid);
  if (!rec) return;
  rec.receivedBytes = p.receivedBytes ?? rec.receivedBytes;
  rec.totalBytes = p.totalBytes ?? rec.totalBytes;
  rec.updatedAt = Date.now();
  rec.state = p.state ?? rec.state;
  if (p.state === 'completed') {
    rec.completed = true;
    rec.state = 'completed';
    // 完成时确认文件真的在磁盘上；不在就如实标成 unknown 而不是假成功
    if (rec.path && existsSync(rec.path)) {
      const st = statSync(rec.path);
      rec.bytes = st.size;
      rec.pathVerified = true;
    } else {
      rec.pathVerified = false;
    }
    browser._emit('download', { ...rec });
  } else if (p.state === 'canceled') {
    rec.completed = false;
    rec.pathVerified = false;
    browser._emit('download', { ...rec });
  }
}

/** 把下载记录整理成上层好用的形状。 */
function presentDownload(rec) {
  if (!rec) return null;
  const out = { ...rec };
  // .crdownload / .tmp 这类中间态文件绝不是"完成"
  if (out.filename && /\.(crdownload|tmp|part|download)$/i.test(out.filename)) {
    out.state = out.completed ? out.state : 'inProgress';
  }
  out.done = out.state === 'completed' && out.completed === true;
  return out;
}

export function downloads(browser) {
  browser._downloads = browser._downloads ?? new Map();
  return [...browser._downloads.values()].map(presentDownload);
}

export function downloadByGuid(browser, guid) {
  browser._downloads = browser._downloads ?? new Map();
  return presentDownload(browser._downloads.get(guid) ?? null);
}

/**
 * 触发下载。
 * refOrUrl 传 ref 时，先读该元素的 href/src；传 http(s) 链接则直接用。
 * @returns {Promise<{guid:string, url:string, filename:string, state:string}>}
 */
export async function download(page, refOrUrl, { filename = '' } = {}) {
  const target = String(refOrUrl ?? '').trim();
  if (!target) throw buError('BU_DOWNLOAD_BLOCKED', 'download 需要一个 ref 或一个 http(s) URL');
  let url = target;
  if (!/^https?:|^data:|^blob:/i.test(target)) {
    // 当 ref 处理
    const objectId = await page.objectIdFor(target);
    try {
      const res = await page.send(
        'Runtime.callFunctionOn',
        {
          functionDeclaration: `function () {
            const v = this.href || this.src || (this.getAttribute && (this.getAttribute('href') || this.getAttribute('src'))) || null;
            return v ? String(v) : null;
          }`,
          objectId,
          returnByValue: true,
        },
        { timeoutMs: 8000 },
      );
      const v = res.result?.value;
      if (!v) {
        throw buError('BU_DOWNLOAD_BLOCKED', `ref "${target}" 上没有 href/src，无法作为下载目标`, { ref: target });
      }
      url = String(v);
    } finally {
      await page.releaseObject(objectId);
    }
  }
  await ensureDownloadBehavior(page.browser, page);
  const dir = page.browser.downloadDirectory();

  // 方式一：真正的导航级下载（服务端 Content-Disposition 会触发下载事件）
  const before = new Set(downloads(page.browser).map((d) => d.guid));
  const guid = await page.sendRaw('Page.setDownloadBehavior', { behavior: 'allowAndName', downloadPath: dir });
  // 上面这条只是确保 page 级也放行；真正发起用 Runtime 里的 anchor click 更容易被拦截，
  // 所以直接走浏览器导航（会离开当前页）——但下载类响应不会替换文档。
  await page.send('Page.navigate', { url }, { timeoutMs: 20000 });
  // 等一小会儿看有没有新下载记录出现
  const newRec = await waitForNewDownload(page.browser, before, 2500);
  if (newRec) return presentDownload(newRec);

  // 方式二：Page.navigate 没能触发下载（比如 URL 直接内联显示）→ 如实报错
  throw buError(
    'BU_DOWNLOAD_BLOCKED',
    `导航到 ${url} 之后 ${2.5}s 内没有出现下载事件。可能原因：该 URL 不是下载响应（浏览器会直接显示内容）、响应需要登录、或浏览器策略禁止自动下载。${guid.ok ? '' : '（Page.setDownloadBehavior 也不可用）'}`,
    { url, downloadDirectory: dir },
  );
}

async function waitForNewDownload(browser, beforeGuids, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    browser._downloads = browser._downloads ?? new Map();
    for (const [guid, rec] of browser._downloads) {
      if (!beforeGuids.has(guid)) return rec;
    }
    if (Date.now() >= deadline) return null;
    await new Promise((r) => setTimeout(r, 100));
  }
}

/**
 * 等下载完成。
 * 超时抛 BU_DOWNLOAD_TIMEOUT，并把"还没完成"的实况放在错误上。
 */
export async function waitForDownload(browser, timeoutSec = 30) {
  const t = Number(timeoutSec);
  if (!Number.isFinite(t) || t <= 0) {
    throw buError('BU_DOWNLOAD_TIMEOUT', `timeoutSec 必须是正数，收到 ${timeoutSec}`);
  }
  const deadline = Date.now() + t * 1000;
  const started = Date.now();
  for (;;) {
    const all = downloads(browser);
    const done = all.filter((d) => d.done);
    const pending = all.filter((d) => !d.done && d.state !== 'canceled');
    if (all.length > 0 && pending.length === 0) {
      return { downloads: all, waitedMs: Date.now() - started };
    }
    if (Date.now() >= deadline) {
      const latest = pending[pending.length - 1] ?? all[all.length - 1] ?? null;
      throw buError(
        'BU_DOWNLOAD_TIMEOUT',
        `等下载完成超时（${t}s）。当前状态：${latest ? `${latest.filename || latest.guid} state=${latest.state} ${latest.receivedBytes ?? 0}/${latest.totalBytes ?? '?'} 字节` : '一个下载记录都没有'}`,
        { downloads: all, timeoutSec: t },
      );
    }
    await new Promise((r) => setTimeout(r, 150));
  }
}

// ---------------- 对话框 ----------------

export function dialogState(page) {
  if (!page.dialog) return { open: false, type: '', message: '' };
  return { ...page.dialog };
}

/** 处理当前挂着的原生对话框。没有对话框时返回 handled:false（不假装成功）。 */
export async function handleDialog(page, { accept = true, promptText = undefined } = {}) {
  const state = dialogState(page);
  if (!state.open) {
    return {
      handled: false,
      open: false,
      reason: '当前没有挂起的原生对话框',
      type: state.type ?? '',
      message: state.message ?? '',
    };
  }
  const params = { accept: !!accept };
  if (promptText !== undefined) params.promptText = String(promptText);
  await page.send('Page.handleJavaScriptDialog', params, { timeoutMs: 8000 });
  // 事件是异步来的，给一小段时间让 javascriptDialogClosed 落地
  await new Promise((r) => setTimeout(r, 60));
  const after = dialogState(page);
  // 统一返回形状：两个分支都带 open 字段，调用方不用猜这次是哪种形状。
  // （实测踩坑：分支形状不一致会让上层 `if (!res.dialog.open)` 把成功误判成失败）
  return {
    handled: true,
    open: false,
    accept: !!accept,
    type: state.type,
    message: state.message,
    closed: after.open === false,
  };
}

/**
 * 点一个元素并准备接它可能弹出来的原生对话框。
 *
 * 【踩坑】不能先 await 点击请求再等对话框：
 *   浏览器遇到 alert/confirm/prompt 会同步阻塞页面 JS，`this.click()` 这个
 *   Runtime.callFunctionOn 在对话框被处理之前不会返回 → 直接死锁
 *   （实测表现：CDP 命令 15s 超时，整个会话被拖死）。
 *   正确做法：先把点击请求丢出去，同时等“对话框打开事件”或“点击响应”哪个先到；
 *   事件一到立刻 handleJavaScriptDialog，页面解锁后点击响应自己会回来。
 *   两种浏览器行为都要兼容：
 *     · 对话框保持打开等我们处理（阻塞式）
 *     · 对话框被浏览器立刻自动关闭（headless 实测见过，事件顺序仍是
 *       opening -> 我们发 handleJavaScriptDialog -> closed）
 */
export async function clickAndHandleDialog(page, ref, { accept = true, promptText = undefined, timeoutSec = 3 } = {}) {
  const { click } = await import('./dom.mjs');
  const timeout = Math.max(200, Number(timeoutSec) * 1000);

  let settle;
  const appeared = new Promise((resolve) => {
    settle = resolve;
  });
  let done = false;
  const finish = (v) => {
    if (done) return;
    done = true;
    settle(v);
  };
  const off = page.browser.session.on('Page.javascriptDialogOpening', (p, ev) => {
    if (ev.sessionId && ev.sessionId !== page.sessionId) return;
    finish(true);
  });
  const timer = setTimeout(() => finish(false), timeout);
  if (typeof timer.unref === 'function') timer.unref();
  // 单独造一个 timeout Promise：Node 的 setTimeout 返回的是 Timeout 对象（不是 Promise），
  // 直接对它 .then 会报 "timer.then is not a function" —— 这里踩过。
  const timedOut = new Promise((resolve) => {
    const t = setTimeout(() => resolve('timeout'), timeout);
    if (typeof t.unref === 'function') t.unref();
  });

  // 点击结果无论成败都收着，别吞错
  const clickPromise = click(page, ref, {}).then(
    (r) => ({ ok: true, result: r }),
    (err) => ({ ok: false, error: err }),
  );

  const outcome = await Promise.race([appeared, clickPromise.then(() => 'clicked'), timedOut]);
  clearTimeout(timer);
  off();

  if (outcome !== true) {
    const clicked = await clickPromise;
    if (!clicked.ok) {
      return { ref, dialog: { open: false, handled: false }, reason: `点击失败：${clicked.error.message}` };
    }
    return {
      ...clicked.result,
      dialog: { open: false, handled: false },
      reason: outcome === 'timeout' ? `等待 ${timeout}ms 内没有出现原生对话框` : '点击已返回，没有出现原生对话框',
    };
  }

  const handled = await handleDialog(page, { accept, promptText });
  // 对话框关掉之后被阻塞的点击才会返回；给一小段宽限，但绝不让整体挂住
  const clicked = await Promise.race([
    clickPromise,
    new Promise((r) => setTimeout(() => r(null), 2000)),
  ]);
  const base = clicked && clicked.ok ? clicked.result : { ref };
  return { ...base, dialog: { ...handled, open: true } };
}

// ---------------- console / network drain ----------------

/** drain：读一次就清空。第二次调用返回空数组是有意的语义。 */
export function drainConsole(page) {
  const out = page.consoleLogs.slice();
  page.consoleLogs.length = 0;
  return out;
}

export function drainNetwork(page, { includePending = false } = {}) {
  const out = [];
  for (const rec of page.networkRecords.values()) {
    if (!includePending && rec.state === 'pending') continue;
    out.push({
      requestId: rec.requestId,
      url: rec.url,
      method: rec.method,
      status: rec.status ?? 0,
      statusText: rec.statusText ?? '',
      resourceType: rec.resourceType ?? null,
      mimeType: rec.mimeType ?? null,
      state: rec.state,
      failed: rec.failed ?? null,
      fromCache: !!rec.fromCache,
      createTime: rec.createTime ?? null,
      updateTime: rec.updateTime ?? null,
    });
  }
  page.networkRecords.clear();
  return out;
}

// ---------------- 截图 ----------------

/** 从 PNG 头里读宽高（IHDR 固定在偏移 16）。 */
function pngSize(buf) {
  if (buf.length < 24) return null;
  if (buf.readUInt32BE(0) !== 0x89504e47) return null;
  return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
}

/**
 * 截图。
 * - 传 ref → 该元素的裁剪图（用 DOM.getContentQuads/getBoundingClientRect 拿页面坐标）
 * - 传 maxDimension → 尝试用 Emulation.setDeviceMetricsOverride 的 deviceScaleFactor 缩小；
 *   如果浏览器不认（返回的图没变小），就如实返回原始尺寸，由上层决定要不要再处理。
 */
export async function screenshot(page, { ref = null, tag = null, maxDimension = null } = {}) {
  const vp = await page.getViewport({ refresh: true });
  let clip = null;
  let elementMeta = null;
  if (ref) {
    const objectId = await page.objectIdFor(ref);
    try {
      const res = await page.send(
        'Runtime.callFunctionOn',
        {
          functionDeclaration: `function () {
            try { this.scrollIntoView({ block: 'center', inline: 'center' }); } catch (e) {}
            const r = this.getBoundingClientRect();
            return JSON.stringify({ x: r.left + (window.scrollX||0), y: r.top + (window.scrollY||0), width: r.width, height: r.height });
          }`,
          objectId,
          returnByValue: true,
        },
        { timeoutMs: 10000 },
      );
      const r = JSON.parse(res.result?.value ?? 'null');
      if (!r || !(r.width > 0) || !(r.height > 0)) {
        throw buError('BU_REF_STALE', `ref "${ref}" 尺寸为 0，无法截图`, { ref });
      }
      clip = { x: r.x, y: r.y, width: r.width, height: r.height, scale: 1 };
      elementMeta = r;
      // scrollIntoView 会改变滚动位置，重新读一次 viewport（影响坐标兜底换算）
      await page.getViewport({ refresh: true });
    } finally {
      await page.releaseObject(objectId);
    }
  }

  const max = Number(maxDimension);
  let overrideApplied = false;
  let scale = 1;
  if (Number.isFinite(max) && max > 0) {
    const baseW = clip ? clip.width : vp.width;
    const baseH = clip ? clip.height : vp.height;
    const largest = Math.max(baseW, baseH);
    if (largest > max) {
      scale = Math.max(0.05, max / largest);
      const r = await page.sendRaw('Emulation.setDeviceMetricsOverride', {
        width: Math.round(vp.width),
        height: Math.round(vp.height),
        deviceScaleFactor: scale,
        mobile: false,
      });
      overrideApplied = r.ok;
    }
  }

  let shot;
  try {
    shot = await page.send(
      'Page.captureScreenshot',
      {
        format: 'png',
        fromSurface: true,
        captureBeyondViewport: false,
        ...(clip ? { clip } : {}),
      },
      { timeoutMs: 30000 },
    );
  } finally {
    if (overrideApplied) {
      try {
        await page.send('Emulation.clearDeviceMetricsOverride', {}, { timeoutMs: 8000 });
      } catch {
        /* 清不掉也不致命，下次 refresh 会重算 */
      }
      await page.getViewport({ refresh: true });
    }
  }

  const png = Buffer.from(shot.data ?? '', 'base64');
  const size = pngSize(png);
  const requestedMax = Number.isFinite(max) && max > 0 ? max : null;
  const result = {
    png,
    width: size?.width ?? 0,
    height: size?.height ?? 0,
    mimeType: 'image/png',
    bytes: png.length,
    ref: ref ?? null,
    tag: tag ?? null,
  };
  if (requestedMax && size && Math.max(size.width, size.height) > requestedMax) {
    result.maxDimensionHonored = false;
    result.note = `请求最大边长 ${requestedMax}，但浏览器返回 ${size.width}x${size.height}：本次会话的 deviceScaleFactor 缩放未被接受，返回的是原始分辨率。`;
  } else if (requestedMax) {
    result.maxDimensionHonored = true;
  }
  if (elementMeta) result.elementBox = elementMeta;
  return result;
}

/** 视口/内容尺寸。 */
export async function pageSize(page) {
  const metrics = await page.send('Page.getLayoutMetrics', {}, { timeoutMs: 8000 });
  const css = metrics.cssLayoutViewport ?? metrics.layoutViewport ?? {};
  const content = metrics.cssContentSize ?? metrics.contentSize ?? {};
  return {
    viewport: { width: Math.round(css.clientWidth ?? 0), height: Math.round(css.clientHeight ?? 0) },
    content: { width: Math.round(content.width ?? 0), height: Math.round(content.height ?? 0) },
    dpr: (await page.getViewport())?.dpr ?? 1,
  };
}

// ---------------- httpGet ----------------

/**
 * 直接读一个 URL 的字节。
 * 说明：这是**从 Node 侧发起的独立 HTTP 请求**，走的是本机网络栈，
 * 不经过当前页面，因此"HTTP 能取到内容"**不证明页面渲染变了**，
 * 也不代表浏览器里看到了同样的东西（可能差在登录态、Cookie、JS 渲染、反爬）。
 */
export async function httpGet(url, { timeoutMs = 15000, maxBytes = 2_000_000, headers = {}, redirect = 'follow' } = {}) {
  const target = String(url ?? '');
  if (!/^https?:\/\//i.test(target)) {
    throw buError('BU_CDP_ERROR', `httpGet 只接受 http(s) URL，收到 ${target}`, { url: target });
  }
  const res = await fetch(target, { signal: AbortSignal.timeout(timeoutMs), headers, redirect });
  const buf = Buffer.from(await res.arrayBuffer());
  const truncated = buf.length > maxBytes;
  const body = buf.subarray(0, maxBytes).toString('utf8');
  return {
    url: target,
    status: res.status,
    ok: res.ok,
    headers: Object.fromEntries(res.headers.entries()),
    mimeType: res.headers.get('content-type') ?? '',
    bytes: buf.length,
    truncated,
    body,
    note: '这是 Node 侧独立 HTTP 取回的字节，只能证明服务端返回了什么；不能证明浏览器页面渲染/执行结果发生了变化。',
  };
}

// ---------------- blockedAuth ----------------

const AUTH_URL_HINTS = [
  'login',
  'signin',
  'sign-in',
  'logon',
  'auth',
  'sso',
  'oauth',
  'passport',
  'account',
  'session/new',
  'verify',
  'captcha',
  'challenge',
  '2fa',
  'mfa',
  'otp',
];

const CAPTCHA_HINTS = ['captcha', 'recaptcha', 'hcaptcha', 'verify', 'challenge', 'slide', 'geetest', 'turnstile'];

/**
 * 判断当前是不是"卡在登录/验证码/打不开"的状态。
 * 只看 URL / title / 密码框 / 验证码容器这些弱信号，不做任何网络探测。
 */
export async function blockedAuth(page) {
  const info = await (await import('./dom.mjs')).pageInfo(page).catch(() => null);
  const url = info?.url ?? page.url ?? '';
  const title = info?.title ?? '';

  const probe = await page.sendRaw(
    'Runtime.evaluate',
    {
      expression: `(() => {
        const t = (document.title || '');
        const bodyText = ((document.body && (document.body.innerText || '')) || '').slice(0, 4000);
        const pw = document.querySelectorAll('input[type=password]').length;
        const txt = document.querySelectorAll('input[type=text],input[type=email],input:not([type])').length;
        const codes = document.querySelectorAll('input[autocomplete=one-time-code],input[name*=code i],input[id*=code i],input[name*=otp i],input[id*=otp i]').length;
        const captchaEls = document.querySelectorAll('iframe[src*=captcha i],iframe[src*=recaptcha i],div[class*=captcha i],div[id*=captcha i],[class*=geetest i],[class*=turnstile i]').length;
        const errPage = !!document.querySelector('#main-frame-error, .neterr, #error-information-popup-content');
        const errCode = (document.querySelector('.error-code') || {}).textContent || '';
        return JSON.stringify({ t, bodyLen: bodyText.length, head: bodyText.slice(0, 400), pw, txt, codes, captchaEls, errPage, errCode: String(errCode).trim() });
      })()`,
      returnByValue: true,
    },
    { timeoutMs: 10000 },
  );

  let d = {};
  if (probe.ok) {
    try {
      d = JSON.parse(probe.result?.result?.value ?? '{}');
    } catch {
      /* 页面坏了就按未知处理 */
    }
  }

  if (d.errPage || /^chrome-error:/.test(url) || /^edge-error:/.test(url)) {
    return {
      blocked: true,
      kind: 'unreachable',
      url,
      reason: `浏览器错误页（${d.errCode || 'net error'}）：页面根本没加载成功`,
      signals: { errPage: true, errorCode: d.errCode || null },
    };
  }

  const hay = `${url} ${title} ${d.head ?? ''}`.toLowerCase();
  const captchaHit = CAPTCHA_HINTS.find((h) => hay.includes(h)) || (d.captchaEls > 0 ? 'captcha-element' : null);
  const passwordField = (d.pw ?? 0) > 0;
  const codeField = (d.codes ?? 0) > 0;
  const urlHint = AUTH_URL_HINTS.find((h) => url.toLowerCase().includes(h)) || null;
  const strong = passwordField || codeField || !!captchaHit;

  if (strong || (urlHint && (d.txt ?? 0) > 0)) {
    return {
      blocked: true,
      kind: 'auth',
      url,
      reason: passwordField
        ? '页面存在 password 输入框，判定为登录页'
        : codeField
          ? '页面存在验证码/一次性口令输入框'
          : captchaHit
            ? `命中验证码特征：${captchaHit}`
            : `URL 命中登录特征 "${urlHint}" 且存在输入框`,
      signals: { passwordField, codeField, captchaHit, urlHint, title: str(title, 120) },
    };
  }

  return { blocked: false, kind: '', url, signals: { passwordField, codeField, captchaHit: null, urlHint } };
}
