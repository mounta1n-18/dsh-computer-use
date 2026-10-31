// lib/cdp/index.mjs
// 对上层（lib/browser-use.mjs）暴露的统一门面。
//
// 这一层的唯一职责：把"会话 + 页面"两层对象收成一个对象，
// 并把 CDP 的细节（sessionId、targetId、scope 自增）挡在后面。
// 语义严格对齐豆包 seed_browser_use 的 browser_use 平面：
//   · ref 一律是 "dN:eM" 的文档作用域字符串，必须整串回传
//   · console/network 是 drain 语义（读一次就清空）
//   · 坐标统一 0-1000 归一化
//   · 所有失败都带稳定 .code，别让上层去猜

import { buError, CdpError, Session, DEFAULT_TIMEOUT_MS, newId, delay } from './session.mjs';
import {
  BrowserSession,
  Page,
  DocToken,
  discoverEndpoints,
  discoverEndpoint,
  DEFAULT_PORTS,
} from './targets.mjs';
import {
  snapshot,
  find,
  collectElements,
  fetchAX,
  formatElementLine,
  registerSelectorRef,
  INTERACTIVE_ROLES,
  DEFAULT_MAX_NODES,
  DEFAULT_MAX_CHARS,
} from './ax.mjs';
import * as domApi from './dom.mjs';
import * as extraApi from './extra.mjs';

export {
  // 底层（上层要自己拨 CDP 时用）
  Session,
  CdpError,
  BrowserSession,
  Page,
  DocToken,
  discoverEndpoints,
  discoverEndpoint,
  DEFAULT_PORTS,
  DEFAULT_TIMEOUT_MS,
  // AX / 快照
  snapshot,
  find,
  collectElements,
  fetchAX,
  formatElementLine,
  registerSelectorRef,
  INTERACTIVE_ROLES,
  DEFAULT_MAX_NODES,
  DEFAULT_MAX_CHARS,
  buError,
  newId,
  delay,
};

/** 让 cdp() 直通时也能拿到页面的 CDP 会话（不导出为公开 API，仅供内部/自测）。 */
export function rawPage(session) {
  const page = session?.browser?.page ?? session?.page;
  return page ?? null;
}

/**
 * 连接到 CDP 端点并返回门面对象。
 *
 * @param {number|string|{port?:number, webSocketDebuggerUrl?:string}} [endpointOrPort]
 *        不传就自己扫 9222..9232；扫不到抛 BU_NO_ENDPOINT（消息里带启动命令）。
 * @param {{targetId?:string, ports?:number[]}} [opts]
 */
export async function connect(endpointOrPort, opts = {}) {
  const browser = await BrowserSession.connect(endpointOrPort, opts);
  // 每个新附着的 Page 都要装额外域监听（console/network/dialog/download）
  browser.setPageExtras((page) => extraApi.installPageExtras(browser, page));
  // 已经附着过的 page 也要补装（setPageExtras 是在 _init 之后设置的）
  for (const page of browser.pages.values()) {
    await extraApi.installPageExtras(browser, page);
  }
  return new CdpSessionApi(browser);
}

/**
 * 门面会话对象。所有公开方法都在这里。
 */
export class CdpSessionApi {
  constructor(browser) {
    this.browser = browser;
    this.session = browser.session;
  }

  /** 当前附着的页面（内部用）。 */
  get page() {
    return this.browser.requirePage();
  }

  /** 当前文档作用域，形如 d3 */
  get scope() {
    return this.browser.page?.refPrefix ?? null;
  }

  /** 当前文档序号（数字） */
  get documentScope() {
    return this.browser.page?.doc.scope ?? null;
  }

  // ---------------- 生命周期 ----------------

  close() {
    return this.browser.close();
  }

  /** 兼容写法 */
  async dispose() {
    return this.close();
  }

  /** 底层 CDP 直通（不做任何包装，返回原始 result）。 */
  cdp(method, params = {}, { timeoutMs } = {}) {
    const page = this.browser.page;
    // 页面级域走 sessionId，浏览器级域（Browser./Target./大多数 Storage.）走裸连接。
    // 这里不能靠猜：调用方明确知道自己在干什么，所以两个都试一遍代价太大，
    // 采用"有附着页且不是 Browser./Target.* 就走页面"的规则。
    const browserLevel = /^(Browser|Target|SystemInfo)\./.test(String(method));
    if (!page || browserLevel) return this.browser.session.send(method, params, { timeoutMs });
    return page.send(method, params, { timeoutMs });
  }

  // ---------------- 导航 ----------------

  /**
   * 导航。url 可以是 http(s):// 地址、data: URL，或 'back'/'forward'/'reload'。
   */
  async navigate(url) {
    const page = this.browser.requirePage();
    const arg = String(url ?? '').trim();
    if (!arg) throw buError('BU_NAV_WAIT_TIMEOUT', 'navigate 需要一个 URL 或 back/forward/reload');

    if (arg === 'back' || arg === 'forward' || arg === 'reload') {
      return this._navigateHistory(page, arg);
    }
    const res = await page.send('Page.navigate', { url: arg }, { timeoutMs: 30000 });
    if (res.errorText) {
      return { ok: false, url: arg, errorText: String(res.errorText) };
    }
    return { ok: true, url: arg, frameId: res.frameId ?? null };
  }

  async _navigateHistory(page, kind) {
    const hist = await page.send('Page.getNavigationHistory', {}, { timeoutMs: 8000 });
    const entries = hist.entries ?? [];
    const index = hist.currentIndex ?? 0;
    if (kind === 'reload') {
      await page.send('Page.reload', { ignoreCache: false }, { timeoutMs: 30000 });
      return { ok: true, action: 'reload', url: entries[index]?.url ?? null };
    }
    const targetIndex = kind === 'back' ? index - 1 : index + 1;
    if (targetIndex < 0 || targetIndex >= entries.length) {
      throw buError(
        'BU_NAV_WAIT_TIMEOUT',
        `无法 ${kind}：当前历史位置 ${index}/${Math.max(0, entries.length - 1)}，没有${
          kind === 'back' ? '上一页' : '下一页'
        }`,
        { kind, index, total: entries.length },
      );
    }
    await page.send(
      'Page.navigateToHistoryEntry',
      { entryId: entries[targetIndex].id },
      { timeoutMs: 30000 },
    );
    return { ok: true, action: kind, url: entries[targetIndex].url };
  }

  /**
   * 等加载完成。
   * 判定顺序：readyState === 'complete' 就直接过；否则等 Page.loadEventFired（带超时）。
   * 超时抛 BU_NAV_WAIT_TIMEOUT，并把当时的状态放在错误对象上。
   */
  async waitForLoad(timeoutSec = 15) {
    const page = this.browser.requirePage();
    const seconds = Number(timeoutSec);
    if (!Number.isFinite(seconds) || seconds <= 0) {
      throw buError('BU_NAV_WAIT_TIMEOUT', `waitForLoad 的 timeoutSec 必须是正数，收到 ${timeoutSec}`);
    }
    const deadline = Date.now() + seconds * 1000;
    const state = async () => {
      const res = await page.sendRaw(
        'Runtime.evaluate',
        { expression: 'document.readyState', returnByValue: true },
        { timeoutMs: Math.min(8000, Math.max(500, deadline - Date.now())) },
      );
      return res.ok ? String(res.result?.result?.value ?? '') : 'unknown';
    };
    if ((await state()) === 'complete') {
      return { ok: true, readyState: 'complete', waitedMs: 0, alreadyLoaded: true };
    }
    const started = Date.now();
    const waiter = new Promise((resolve) => {
      page._loadWaiter = resolve;
    });
    const timer = new Promise((resolve) => {
      const t = setTimeout(() => resolve('timeout'), Math.max(1, deadline - Date.now()));
      if (typeof t.unref === 'function') t.unref();
    });
    const poll = (async () => {
      for (;;) {
        if (Date.now() >= deadline) return 'timeout';
        await delay(120);
        if ((await state()) === 'complete') return 'ready';
      }
    })();
    const outcome = await Promise.race([waiter.then(() => 'event'), timer, poll]);
    page._loadWaiter = null;
    if (outcome === 'timeout') {
      const rs = await state();
      throw buError(
        'BU_NAV_WAIT_TIMEOUT',
        `等页面加载超时（${seconds}s），当前 readyState=${rs}`,
        { timeoutSec: seconds, readyState: rs, url: page.url },
      );
    }
    return { ok: true, readyState: await state(), waitedMs: Date.now() - started, via: outcome };
  }

  /**
   * 等一个选择器出现（并可选要求可见）。
   * 返回 {found, ref, visible, reason}；超时抛 BU_NAV_WAIT_TIMEOUT。
   */
  async waitForElement(selector, { visible = true, timeoutSec = 15 } = {}) {
    const page = this.browser.requirePage();
    const sel = String(selector ?? '').trim();
    if (!sel) throw buError('BU_NAV_WAIT_TIMEOUT', 'waitForElement 需要一个非空 CSS 选择器');
    const seconds = Number(timeoutSec);
    if (!Number.isFinite(seconds) || seconds <= 0) {
      throw buError('BU_NAV_WAIT_TIMEOUT', `waitForElement 的 timeoutSec 必须是正数，收到 ${timeoutSec}`);
    }
    const deadline = Date.now() + seconds * 1000;
    let last = { found: false, visible: false, reason: 'not-found' };
    for (;;) {
      const res = await page.sendRaw(
        'Runtime.evaluate',
        {
          expression: `(() => {
            let el = null;
            try { el = document.querySelector(${JSON.stringify(sel)}); } catch (e) { return JSON.stringify({ bad: String(e && e.message || e) }); }
            if (!el) return JSON.stringify({ found: false });
            const r = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            const vis = r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' && Number(cs.opacity) !== 0;
            return JSON.stringify({ found: true, visible: vis, width: r.width, height: r.height, display: cs.display, visibility: cs.visibility });
          })()`,
          returnByValue: true,
        },
        { timeoutMs: 8000 },
      );
      if (res.ok) {
        let d = {};
        try {
          d = JSON.parse(res.result?.result?.value ?? '{}');
        } catch {
          /* ignore */
        }
        if (d.bad) throw buError('BU_READ_TARGET', `选择器 "${sel}" 不合法：${d.bad}`, { selector: sel });
        last = d.found
          ? { found: true, visible: !!d.visible, reason: d.visible ? 'visible' : `存在但不可见（display=${d.display} visibility=${d.visibility}）` }
          : { found: false, visible: false, reason: 'not-found' };
        if (last.found && (!visible || last.visible)) {
          const reg = await registerSelectorRef(page, sel);
          return {
            selector: sel,
            found: true,
            visible: last.visible,
            ref: reg?.ref ?? null,
            role: reg?.role ?? null,
            name: reg?.name ?? null,
            numbered: !!reg?.numbered,
          };
        }
      }
      if (Date.now() >= deadline) {
        throw buError(
          'BU_NAV_WAIT_TIMEOUT',
          `等元素 "${sel}" 超时（${seconds}s）：${last.reason}`,
          { selector: sel, timeoutSec: seconds, last },
        );
      }
      await delay(120);
    }
  }

  // ---------------- 观察 ----------------

  snapshot(opts) {
    return snapshot(this.browser.requirePage(), opts);
  }

  find(query, opts) {
    return find(this.browser.requirePage(), query, opts);
  }

  pageInfo() {
    return domApi.pageInfo(this.browser.requirePage());
  }

  getPageText(opts) {
    return domApi.getPageText(this.browser.requirePage(), opts);
  }

  text(ref) {
    return domApi.text(this.browser.requirePage(), ref);
  }

  get(ref, attrName) {
    return domApi.get(this.browser.requirePage(), ref, attrName);
  }

  readAll(selector, opts) {
    return domApi.readAll(this.browser.requirePage(), selector, opts);
  }

  contentExport(opts) {
    return domApi.contentExport(this.browser.requirePage(), opts);
  }

  js(expression) {
    return domApi.js(this.browser.requirePage(), expression);
  }

  // ---------------- 元素操作 ----------------

  click(ref, opts) {
    return domApi.click(this.browser.requirePage(), ref, opts);
  }

  hover(ref) {
    return domApi.hover(this.browser.requirePage(), ref);
  }

  type(ref, value, opts) {
    return domApi.type(this.browser.requirePage(), ref, value, opts);
  }

  select(ref, value) {
    return domApi.select(this.browser.requirePage(), ref, value);
  }

  check(ref) {
    return domApi.check(this.browser.requirePage(), ref);
  }

  uncheck(ref) {
    return domApi.uncheck(this.browser.requirePage(), ref);
  }

  upload(ref, absolutePath) {
    return domApi.upload(this.browser.requirePage(), ref, absolutePath);
  }

  fillInput(selector, value) {
    return domApi.fillInput(this.browser.requirePage(), selector, value);
  }

  // ---------------- 坐标兜底（0-1000 归一化） ----------------

  clickXy(x, y, opts) {
    return domApi.clickXy(this.browser.requirePage(), x, y, opts);
  }

  leftDouble(x, y) {
    return domApi.leftDouble(this.browser.requirePage(), x, y);
  }

  rightSingle(x, y) {
    return domApi.rightSingle(this.browser.requirePage(), x, y);
  }

  drag(x1, y1, x2, y2, opts) {
    return domApi.drag(this.browser.requirePage(), x1, y1, x2, y2, opts);
  }

  scroll(x, y, direction, opts) {
    return domApi.scroll(this.browser.requirePage(), x, y, direction, opts);
  }

  hotkey(...keys) {
    return domApi.hotkey(this.browser.requirePage(), ...keys);
  }

  pressKey(key) {
    return domApi.pressKey(this.browser.requirePage(), key);
  }

  // ---------------- 标签页 ----------------

  listTabs() {
    return this.browser.listTabs();
  }

  currentTab() {
    return this.browser.currentTab();
  }

  newTab(url) {
    return this.browser.newTab(url ?? 'about:blank');
  }

  switchTab(tab) {
    return this.browser.switchTab(tab);
  }

  closeTab(targetId) {
    return this.browser.closeTab(targetId ?? null);
  }

  resync() {
    return this.browser.resync();
  }

  // ---------------- 截图 / 尺寸 ----------------

  screenshot(opts) {
    return extraApi.screenshot(this.browser.requirePage(), opts);
  }

  pageSize() {
    return extraApi.pageSize(this.browser.requirePage());
  }

  // ---------------- 下载 ----------------

  download(refOrUrl, opts) {
    return extraApi.download(this.browser.requirePage(), refOrUrl, opts);
  }

  waitForDownload(timeoutSec = 30) {
    return extraApi.waitForDownload(this.browser, timeoutSec);
  }

  downloads() {
    return extraApi.downloads(this.browser);
  }

  // ---------------- 对话框 ----------------

  /** 当前是否有原生对话框挂着（同步，读事件缓冲）。 */
  dialogState() {
    return extraApi.dialogState(this.browser.requirePage());
  }

  handleDialog(opts) {
    return extraApi.handleDialog(this.browser.requirePage(), opts);
  }

  clickAndHandleDialog(ref, opts) {
    return extraApi.clickAndHandleDialog(this.browser.requirePage(), ref, opts);
  }

  // ---------------- 诊断 ----------------

  /** drain：读一次就清空。 */
  consoleMessages() {
    return extraApi.drainConsole(this.browser.requirePage());
  }

  /** drain：读一次就清空。 */
  networkRequests() {
    return extraApi.drainNetwork(this.browser.requirePage());
  }

  httpGet(url, opts) {
    return extraApi.httpGet(url, opts);
  }

  blockedAuth() {
    return extraApi.blockedAuth(this.browser.requirePage());
  }
}

export default { connect, discoverEndpoints, discoverEndpoint, CdpSessionApi, buError };
