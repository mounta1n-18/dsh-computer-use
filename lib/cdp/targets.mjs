// lib/cdp/targets.mjs
// 端点发现 + 浏览器会话 + tab（target）管理与文档作用域（dN）分配。
//
// 关键设计：文档作用域 token
//   ref 形如 "d7:e3"。d7 是"文档序号"，e3 是"该文档内的元素序号"。
//   每次主框架真的换文档（Page.frameNavigated，非 same-document）→ 序号自增，
//   于是旧 ref 的 dN 对不上当前 token，立刻 BU_REF_STALE。
//   这是刻意做成"绝不复用旧编号"的：宁可让上层重新 snapshot()，
//   也不允许出现"点到了另一个长得差不多的元素"这种更难查的错。

import { Session, buError, delay } from './session.mjs';
import { existsSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

export const DEFAULT_PORTS = Object.freeze(
  Array.from({ length: 11 }, (_, i) => 9222 + i), // 9222..9232
);

/** 扫端点时每个端口的探测上限。本机回环，超过这个时间基本就是没开。 */
const PROBE_TIMEOUT_MS = 600;

/** listTabs 的缓存时长：同一轮工具调用里连着问几次不必反复打 CDP。 */
const TARGET_CACHE_MS = 500;

/**
 * 给出"扫不到 CDP 端点"时该说什么。
 * 这条消息是硬要求：必须写清 Chrome >= 136 拒绝在默认 profile 上开调试端口，
 * 必须用独立 user-data-dir，并且不能假装能连用户已经开着的那个浏览器。
 */
function noEndpointError(ports, details) {
  const list = ports.join(',');
  return buError(
    'BU_NO_ENDPOINT',
    [
      `在 127.0.0.1:${list} 上没扫到 Chrome DevTools Protocol 端点。`,
      '',
      '注意（这是 Chrome 的行为，不是本库的缺陷）：Chrome 136+ 出于安全考虑，',
      '拒绝在**默认用户配置**上开启 --remote-debugging-port，所以"直接连用户已经开着的浏览器"',
      '在当前 Chrome 上不可靠/不可行。必须用独立 --user-data-dir 起一个可调试实例：',
      '',
      '  & "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" `',
      '      --remote-debugging-port=9222 `',
      '      --user-data-dir="$env:TEMP\\dsh-cdp-profile" `',
      '      --no-first-run --no-default-browser-check',
      '',
      '（想共用登录态，可以在起实例前把默认 profile 拷进 --user-data-dir。）',
      '端点起来后用 discoverEndpoints() 复查，再 connect(9222)。',
      details ? `\n探测细节：${details}` : '',
    ].join('\n'),
    { ports },
  );
}

/**
 * 扫 127.0.0.1:9222..9232 的 /json/version，命中即返回。
 * @param {number[]} [ports]
 * @returns {Promise<Array<{port:number, browser:string, webSocketDebuggerUrl:string, userAgent?:string, protocolVersion?:string}>>}
 */
export async function discoverEndpoints(ports = DEFAULT_PORTS) {
  const list = (ports ?? DEFAULT_PORTS).filter((p) => Number.isInteger(p) && p > 0);
  const found = [];
  const queue = [...list];
  const worker = async () => {
    for (;;) {
      const port = queue.shift();
      if (port == null) return;
      try {
        const res = await fetch(`http://127.0.0.1:${port}/json/version`, {
          signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
          headers: { accept: 'application/json' },
        });
        if (!res.ok) continue;
        const body = await res.json();
        if (body && typeof body.webSocketDebuggerUrl === 'string') {
          found.push({
            port,
            browser: body.Browser ?? '',
            webSocketDebuggerUrl: body.webSocketDebuggerUrl,
            userAgent: body['User-Agent'],
            protocolVersion: body['Protocol-Version'],
          });
        }
      } catch {
        // 端口没人听 / 不是 CDP：这里静默是合理的，扫描本身就是"试探"
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(6, queue.length || 1) }, worker));
  found.sort((a, b) => a.port - b.port);
  return found;
}

/**
 * 扫描并返回**第一个**端点；扫不到直接抛 BU_NO_ENDPOINT（带启动命令）。
 */
export async function discoverEndpoint(ports = DEFAULT_PORTS) {
  const all = await discoverEndpoints(ports);
  if (all.length === 0) throw noEndpointError(ports ?? DEFAULT_PORTS);
  return all[0];
}

/**
 * 文档作用域 token。每次"换文档"就换一个新对象，旧对象的 dead=true。
 * ref 校验看的是 scope 数字，dead 只是给诊断用的补充。
 */
export class DocToken {
  constructor(scope) {
    this.scope = scope;
    this.dead = false;
  }

  get refPrefix() {
    return `d${this.scope}`;
  }
}

/**
 * 一个已附着的页面（tab）。
 * 这里是"当前文档"的持有者：ref 映射、AX 缓存、viewport、事件订阅都挂在它身上。
 */
export class Page {
  /**
   * @param {BrowserSession} browser
   * @param {string} targetId
   * @param {string} sessionId
   */
  constructor(browser, targetId, sessionId) {
    this.browser = browser;
    this.targetId = targetId;
    this.sessionId = sessionId;
    this.closed = false;
    /** 当前文档 token */
    this.doc = new DocToken(browser._nextScope());
    /** backendNodeId -> 元素信息（snapshot 时记录；具体动作会重新校验存活） */
    this.refMap = new Map();
    this._snapshotCache = null;
    this._axCache = null;
    /** @type {{width:number,height:number,dpr:number,scrollX:number,scrollY:number}|null} */
    this._viewport = null;
    this.url = '';
    this.title = '';
    /** 本页最近一次导航时间戳（waitForLoad 用） */
    this._lastLoadAt = 0;
    this._loadWaiter = null;
    // 下载/对话框/控制台/网络缓冲区由 extra.mjs 挂到这个对象上
    this.consoleLogs = [];
    this.networkRecords = new Map();
    this.dialog = null;
    this._off = [];
  }

  /** 页面级 CDP 命令（自动带上本 session 的 sessionId）。 */
  send(method, params, opts = {}) {
    return this.browser.session.send(method, params, { ...opts, sessionId: this.sessionId });
  }

  sendRaw(method, params, opts = {}) {
    return this.browser.session.sendRaw(method, params, { ...opts, sessionId: this.sessionId });
  }

  /** 换文档：自增 scope，旧 ref 全部作废。 */
  _newDocument(why) {
    const old = this.doc;
    old.dead = true;
    this.doc = new DocToken(this.browser._nextScope());
    this.refMap.clear();
    this._snapshotCache = null;
    this._axCache = null;
    this._viewport = null;
    this._lastLoadAt = Date.now();
    this.browser._emit('document', { page: this, why, scope: this.doc.scope });
    return this.doc;
  }

  /** 当前文档序号 */
  get scope() {
    return this.doc.scope;
  }

  /** 当前 ref 前缀，形如 d7 */
  get refPrefix() {
    return this.doc.refPrefix;
  }

  /**
   * 把一个 dN:eM 字符串拆开并校验作用域。
   * 注意：这里**不做**"宽容匹配"（不会把 d1:e3 当成 d7:e3 用）。
   * @param {string} ref
   * @returns {{scope:number, index:number, backendNodeId?:number, ref:string}}
   */
  parseRef(ref) {
    if (typeof ref !== 'string' || ref.length === 0) {
      throw buError('BU_REF_SHAPE', `ref 必须是非空字符串，收到 ${JSON.stringify(ref)}`);
    }
    const m = /^d(\d+):e(\d+)$/.exec(ref.trim());
    if (!m) {
      if (/^e\d+$/.test(ref.trim())) {
        throw buError(
          'BU_REF_UNSCOPED',
          `ref "${ref}" 缺少文档作用域前缀，正确写法是 "dN:eM"（例如 ${this.refPrefix}:e1）`,
        );
      }
      throw buError(
        'BU_REF_SHAPE',
        `ref "${ref}" 形状不合法，必须是 "dN:eM"（N/M 为非负整数），例如 ${this.refPrefix}:e1`,
      );
    }
    const scope = Number(m[1]);
    const index = Number(m[2]);
    if (scope !== this.doc.scope) {
      throw buError(
        'BU_REF_STALE',
        `ref "${ref}" 属于文档 d${scope}，当前文档已是 ${this.refPrefix}（页面导航/重载/切换标签页都会换文档）`,
        { ref, refScope: scope, currentScope: this.doc.scope },
      );
    }
    return { scope, index, ref: ref.trim() };
  }

  /** 拿出 ref 对应的 backendNodeId；元素已不在 → BU_REF_STALE。 */
  backendNodeIdFor(ref) {
    const parsed = this.parseRef(ref);
    const info = this.refMap.get(parsed.index);
    if (!info || info.backendNodeId == null) {
      throw buError(
        'BU_REF_STALE',
        `ref "${ref}" 在本文档（${this.refPrefix}）里没有对应元素：元素可能已被移除，或该 ref 来自上一次 snapshot 之前的文档。请重新 snapshot()。`,
        { ref },
      );
    }
    return info.backendNodeId;
  }

  /** 该 ref 的快照期信息（role/name 等），给诊断和返回值用。 */
  refInfo(ref) {
    const parsed = this.parseRef(ref);
    return this.refMap.get(parsed.index) ?? null;
  }

  /** 解析 ref → Runtime objectId（真实 DOM 节点）。 */
  async objectIdFor(ref) {
    const backendNodeId = this.backendNodeIdFor(ref);
    return this.objectIdForBackendNodeId(backendNodeId, ref);
  }

  async objectIdForBackendNodeId(backendNodeId, refForError) {
    const res = await this.send('DOM.resolveNode', { backendNodeId });
    const objectId = res?.object?.objectId;
    if (!objectId) {
      throw buError(
        'BU_REF_STALE',
        `ref "${refForError ?? backendNodeId}" 指向的 DOM 节点已经不存在（可能是 SPA 重渲染把节点换掉了），请重新 snapshot()。`,
        { ref: refForError, backendNodeId },
      );
    }
    return objectId;
  }

  /** 释放一个 RemoteObject，避免长时间会话里 objectId 泄漏。 */
  async releaseObject(objectId) {
    if (!objectId) return;
    try {
      await this.send('Runtime.releaseObject', { objectId }, { timeoutMs: 3000 });
    } catch {
      // 释放失败无关紧要（连接可能已经关了）
    }
  }

  /** 读取/刷新视口尺寸（含 devicePixelRatio 和滚动位置）。 */
  async getViewport({ refresh = false } = {}) {
    if (this._viewport && !refresh) return this._viewport;
    const [metrics, dprRes, scrollRes] = await Promise.all([
      this.send('Page.getLayoutMetrics', {}, { timeoutMs: 8000 }),
      this.sendRaw('Runtime.evaluate', {
        expression: 'devicePixelRatio || 1',
        returnByValue: true,
      }),
      this.sendRaw('Runtime.evaluate', {
        expression: 'JSON.stringify({x:window.scrollX||0,y:window.scrollY||0})',
        returnByValue: true,
      }),
    ]);
    const css = metrics.cssLayoutViewport || metrics.layoutViewport || { clientWidth: 0, clientHeight: 0 };
    let scroll = { x: 0, y: 0 };
    if (scrollRes.ok) {
      try {
        scroll = JSON.parse(scrollRes.result?.result?.value ?? '{"x":0,"y":0}');
      } catch {
        /* 页面可能禁用了 window，保持 0 */
      }
    }
    this._viewport = {
      width: Math.round(css.clientWidth ?? 0),
      height: Math.round(css.clientHeight ?? 0),
      dpr: dprRes.ok ? Number(dprRes.result?.result?.value) || 1 : 1,
      scrollX: Number(scroll.x) || 0,
      scrollY: Number(scroll.y) || 0,
    };
    return this._viewport;
  }

  /** 页面级事件订阅（Page/Runtime/Network/Log/Target 都走这条）。 */
  on(method, fn) {
    const off = this.browser.session.on(method, (params, ev) => {
      // flatten 模式下事件带 sessionId；只处理属于本页的。
      if (ev.sessionId && ev.sessionId !== this.sessionId) return;
      fn(params, ev);
    });
    this._off.push(off);
    return off;
  }

  /** 退订本页所有订阅（detach 时调用，避免监听器越挂越多）。 */
  _dispose() {
    for (const off of this._off.splice(0)) {
      try {
        off();
      } catch {
        /* ignore */
      }
    }
  }
}

/**
 * 浏览器级会话：一条 CDP WebSocket + 若干个已附着的 Page。
 * 上层（browser-use.mjs）拿到的基本就是这个对象。
 */
export class BrowserSession {
  /**
   * @param {Session} session
   * @param {{endpoint?:object}} [opts]
   */
  constructor(session, opts = {}) {
    this.session = session;
    this.endpoint = opts.endpoint ?? null;
    this.closed = false;
    /** @type {Page|null} */
    this.page = null;
    /** @type {Map<string, Page>} */
    this.pages = new Map();
    /** @type {Map<string, {targetId:string,type:string,url:string,title:string,attached:boolean}>} */
    this.targets = new Map();
    this._scopeSeq = 0;
    this._targetsAt = 0;
    this._downloadDir = null;
    this._listeners = new Map();
    this._off = [];
    this.version = null;
  }

  _nextScope() {
    this._scopeSeq += 1;
    return this._scopeSeq;
  }

  // ---- 极简事件发射器（给上层订阅 document/targetChanged/dialog） ----
  on(event, fn) {
    let set = this._listeners.get(event);
    if (!set) {
      set = new Set();
      this._listeners.set(event, set);
    }
    set.add(fn);
    return () => set.delete(fn);
  }

  _emit(event, payload) {
    const set = this._listeners.get(event);
    if (!set) return;
    for (const fn of [...set]) {
      try {
        fn(payload);
      } catch (err) {
        console.error(`[cdp] ${event} 监听器异常: ${err?.message ?? err}`);
      }
    }
  }

  /**
   * 连接并初始化：拿版本、打开 target 发现、附着到活动页。
   * @param {string|number|{port?:number, webSocketDebuggerUrl?:string}} endpointOrPort
   * @param {{targetId?:string, ports?:number[]}} [opts]
   */
  static async connect(endpointOrPort, opts = {}) {
    let endpoint = null;
    if (typeof endpointOrPort === 'object' && endpointOrPort) endpoint = endpointOrPort;
    else if (typeof endpointOrPort === 'number') {
      const found = await discoverEndpoints([endpointOrPort]);
      if (found.length === 0) throw noEndpointError([endpointOrPort]);
      endpoint = found[0];
    } else if (typeof endpointOrPort === 'string') {
      // 允许直接丢一个 ws:// 或者 http://127.0.0.1:9222
      const m = /^ws/.test(endpointOrPort)
        ? null
        : /:(\d+)/.exec(endpointOrPort);
      if (m) {
        const port = Number(m[1]);
        const found = await discoverEndpoints([port]);
        if (found.length === 0) throw noEndpointError([port]);
        endpoint = found[0];
      } else {
        endpoint = { webSocketDebuggerUrl: endpointOrPort, port: null, browser: '' };
      }
    } else {
      endpoint = await discoverEndpoint(opts.ports);
    }
    if (!endpoint?.webSocketDebuggerUrl) {
      // 没给端点就自己扫；扫不到抛 BU_NO_ENDPOINT
      endpoint = await discoverEndpoint(opts.ports);
    }

    const session = await Session.connect(endpoint.webSocketDebuggerUrl, 'browser');
    const bs = new BrowserSession(session, { endpoint });
    await bs._init(opts);
    return bs;
  }

  async _init(opts = {}) {
    const ver = await this.session.sendRaw('Browser.getVersion', {}, { timeoutMs: 5000 });
    this.version = ver.ok ? ver.result : null;

    // 目标发现：拿到 targetCreated/targetInfoChanged/targetDestroyed，保持 tab 列表新鲜
    await this.session.sendRaw('Target.setDiscoverTargets', { discover: true });
    this._off.push(
      this.session.on('Target.targetCreated', (p) => this._onTargetInfo(p.targetInfo, true)),
      this.session.on('Target.targetInfoChanged', (p) => this._onTargetInfo(p.targetInfo, false)),
      this.session.on('Target.targetDestroyed', (p) => {
        this.targets.delete(p.targetId);
        const page = this.pages.get(p.targetId);
        if (page) {
          page.closed = true;
          page._dispose();
          this.pages.delete(p.targetId);
          if (this.page === page) this.page = null;
          this._emit('targetChanged', { type: 'destroyed', targetId: p.targetId });
        }
      }),
      this.session.on('Target.detachedFromTarget', (p) => {
        const page = this.pages.get(p.targetId);
        if (page) {
          page.closed = true;
          page._dispose();
          this.pages.delete(p.targetId);
          if (this.page === page) this.page = null;
        }
      }),
    );

    await this.refreshTargets({ force: true });

    let targetId = opts.targetId ?? null;
    if (!targetId) {
      const pages = [...this.targets.values()].filter((t) => t.type === 'page');
      // 优先挑"已经在屏幕上"的那个；挑不出就用第一个
      targetId = pages.find((t) => t.attached)?.targetId ?? pages[0]?.targetId ?? null;
    }
    if (targetId) {
      await this.attach(targetId, { makeCurrent: true });
    } else {
      await this.newTab('about:blank');
    }
  }

  _onTargetInfo(info, isNew) {
    if (!info?.targetId) return;
    if (this.targets.has(info.targetId) && isNew) return;
    this.targets.set(info.targetId, {
      targetId: info.targetId,
      type: info.type,
      url: info.url,
      title: info.title,
      attached: !!info.attached,
    });
    this._emit('targetChanged', { type: isNew ? 'created' : 'changed', targetId: info.targetId });
  }

  /**
   * 拉取 target 列表（带 TTL 缓存）。
   * @param {{force?:boolean}} [opts]
   */
  async refreshTargets({ force = false } = {}) {
    const now = Date.now();
    if (!force && now - this._targetsAt < TARGET_CACHE_MS) return [...this.targets.values()];
    const res = await this.session.send('Target.getTargets', {}, { timeoutMs: 8000 });
    const infos = res.targetInfos ?? [];
    const seen = new Set();
    for (const info of infos) {
      if (!info?.targetId) continue;
      seen.add(info.targetId);
      this._onTargetInfo(info, false);
    }
    for (const id of [...this.targets.keys()]) {
      if (!seen.has(id) && !this.pages.has(id)) this.targets.delete(id);
    }
    this._targetsAt = now;
    return [...this.targets.values()];
  }

  /** 附着到一个 target，建立 flatten 会话。 */
  async attach(targetId, { makeCurrent = false } = {}) {
    const existing = this.pages.get(targetId);
    if (existing && !existing.closed) {
      if (makeCurrent) this.page = existing;
      return existing;
    }
    let res;
    try {
      res = await this.session.send(
        'Target.attachToTarget',
        { targetId, flatten: true },
        { timeoutMs: 10000 },
      );
    } catch (err) {
      // 附着失败最常见的原因是 target 刚被关掉
      throw buError('BU_SESSION_STALE', `附着到 target ${targetId} 失败：${err.message}`, {
        targetId,
        cause: err,
      });
    }
    const sessionId = res.sessionId;
    const page = new Page(this, targetId, sessionId);
    this.pages.set(targetId, page);
    await this.browser_initPage(page);
    if (makeCurrent || !this.page) this.page = page;
    this._emit('targetChanged', { type: 'attached', targetId });
    return page;
  }

  /** 给一个 Page 装好基础域（Page/Runtime/Network/Log）和导航监听。 */
  async browser_initPage(page) {
    // Page.enable：要 frameNavigated / loadEventFired / javascriptDialogOpening
    await page.send('Page.enable', {});
    // 注意：Runtime.enable 会让 Chrome 缓存 console 参数的 RemoteObject（有内存开销），
    // 但 drain 语义要求我们拿到 console 文本，这个代价必须付。
    await page.send('Runtime.enable', {});
    const extras = this._pageExtras;
    if (extras) await extras(page);

    page.on('Page.frameNavigated', (p) => {
      // 只有主框架换文档才换 scope；iframe 导航不影响 ref 作用域
      if (p.frame && !p.frame.parentId) {
        page.url = p.frame.url ?? page.url;
        const why = p.frame.url === page.url ? 'frameNavigated' : 'frameNavigated';
        page._newDocument(why);
        this._emit('document', { page, why, scope: page.doc.scope });
      }
    });
    page.on('Page.frameStartedLoading', () => {
      page._lastLoadAt = Date.now();
    });
    page.on('Page.loadEventFired', () => {
      page._lastLoadAt = Date.now();
      if (page._loadWaiter) {
        const w = page._loadWaiter;
        page._loadWaiter = null;
        w();
      }
    });
    page.on('Runtime.executionContextsCleared', () => {
      // 有些文档替换（比如某些 SPA 的 location 重写）不一定发 frameNavigated，
      // 但一定会清执行上下文。这里也换 scope，宁可多换一次也不能让旧 ref 混进来。
      page._newDocument('executionContextsCleared');
    });
  }

  /**
   * 注册"每个新 page 都要装的额外域监听"（extra.mjs 注入 console/network/dialog/download）。
   * 采用注入而不是 import，避免模块循环依赖。
   */
  setPageExtras(fn) {
    this._pageExtras = fn;
  }

  // ---------------- tab 管理 ----------------

  /** @returns {Promise<Array<{targetId:string,url:string,title:string,active:boolean}>>} */
  async listTabs() {
    const infos = await this.refreshTargets();
    return infos
      .filter((t) => t.type === 'page')
      .map((t) => ({
        targetId: t.targetId,
        url: t.url,
        title: t.title,
        active: this.page?.targetId === t.targetId,
      }));
  }

  /** 当前附着页的 tab 记录；没有附着页返回 null。 */
  async currentTab() {
    if (!this.page || this.page.closed) return null;
    const tab = await this.targetInfo(this.page.targetId);
    return {
      targetId: this.page.targetId,
      url: tab?.url ?? this.page.url,
      title: tab?.title ?? this.page.title,
      active: true,
    };
  }

  async targetInfo(targetId) {
    await this.refreshTargets();
    return this.targets.get(targetId) ?? null;
  }

  /** 新开一个标签页并附着（置前）。 */
  async newTab(url = 'about:blank') {
    const res = await this.session.send('Target.createTarget', { url }, { timeoutMs: 10000 });
    const targetId = res.targetId;
    await this.refreshTargets({ force: true });
    const page = await this.attach(targetId, { makeCurrent: true });
    return { targetId, url, title: '', active: true, page };
  }

  /**
   * 切换到指定 tab 并置前。
   * @param {{targetId:string}|string} tab
   */
  async switchTab(tab) {
    const targetId = typeof tab === 'string' ? tab : tab?.targetId;
    if (!targetId) throw buError('BU_CDP_ERROR', 'switchTab 需要 targetId');
    const page = await this.attach(targetId, { makeCurrent: true });
    // 置前：让用户也能看到我们操作的是哪个 tab
    try {
      await this.session.send('Target.activateTarget', { targetId });
    } catch {
      // headless 下 activateTarget 可能不起作用，不影响后面的操作
    }
    this.page = page;
    // 换 tab 意味着换了文档上下文 → 旧 ref 一律作废
    page._newDocument('switchTab');
    return {
      targetId,
      url: this.targets.get(targetId)?.url ?? page.url,
      title: this.targets.get(targetId)?.title ?? page.title,
      active: true,
    };
  }

  /** 关闭一个 tab（默认当前）。 */
  async closeTab(targetId = null) {
    const id = targetId ?? this.page?.targetId;
    if (!id) return { closed: false, targetId: null, reason: '没有可关闭的标签页' };
    const page = this.pages.get(id);
    if (page) {
      page.closed = true;
      page._dispose();
      this.pages.delete(id);
      if (this.page === page) this.page = null;
    }
    try {
      await this.session.send('Target.closeTarget', { targetId: id }, { timeoutMs: 8000 });
    } catch (err) {
      return { closed: false, targetId: id, reason: err.message };
    }
    await this.refreshTargets({ force: true });
    if (!this.page) {
      const next = [...this.targets.values()].find((t) => t.type === 'page');
      if (next) await this.attach(next.targetId, { makeCurrent: true });
    }
    return { closed: true, targetId: id };
  }

  /**
   * 重新附着到"屏幕上可见的那个 tab"。
   *
   * 诚实声明：CDP **没有**任何 API 能直接告诉你"哪个 tab 对用户可见"。
   * 我们能拿到的只有 document.visibilityState / document.hasFocus() 这类近似信号，
   * 而且它们在 headless 或被遮挡的窗口里会给出不准确的结果。
   * 因此这里只在**能唯一确定**时返回；一旦有歧义就抛 BU_SESSION_STALE 让上层显式指定，
   * 不做"猜一个"。
   */
  async resync() {
    const infos = (await this.refreshTargets({ force: true })).filter((t) => t.type === 'page');
    if (infos.length === 0) {
      throw buError('BU_SESSION_STALE', '没有可附着的标签页（浏览器里一个页面都没有）');
    }
    const probes = [];
    for (const info of infos) {
      let page;
      try {
        page = await this.attach(info.targetId);
      } catch {
        continue;
      }
      const probe = await page.sendRaw('Runtime.evaluate', {
        expression:
          'JSON.stringify({v:document.visibilityState,f:document.hasFocus(),h:document.hidden,url:location.href,t:document.title})',
        returnByValue: true,
      });
      let state = { v: 'unknown', f: false, h: true, url: info.url, t: info.title };
      if (probe.ok) {
        try {
          state = { ...state, ...JSON.parse(probe.result?.result?.value ?? '{}') };
        } catch {
          /* 页面可能把 JSON 弄坏了，按 unknown 处理 */
        }
      }
      probes.push({ info, page, state, focusedVisible: state.v === 'visible' && state.f === true });
    }

    const candidates = probes.filter((p) => p.focusedVisible);
    let picked = null;
    let reason = '';
    if (candidates.length === 1) {
      picked = candidates[0];
      reason = '唯一同时满足 visibilityState=visible 且 hasFocus=true 的标签页';
    } else if (candidates.length > 1) {
      throw buError(
        'BU_SESSION_STALE',
        `无法可靠判断"屏幕上可见的标签页"：有 ${candidates.length} 个标签页都报告 visible+focused（CDP 没有真正的"可见 tab"API）。请用 listTabs() + switchTab(targetId) 显式指定。`,
        { candidates: candidates.map((c) => c.info.targetId) },
      );
    } else {
      const visible = probes.filter((p) => p.state.v === 'visible');
      if (visible.length === 1) {
        picked = visible[0];
        reason = '唯一 visibilityState=visible 的标签页（没有得到焦点信号，属于近似判断）';
      } else {
        throw buError(
          'BU_SESSION_STALE',
          `无法可靠判断"屏幕上可见的标签页"：${visible.length} 个标签页报告 visible，0 个报告 focused。CDP 不提供真正的可见性 API，本库不做猜测。请用 listTabs() + switchTab(targetId) 显式指定。`,
          { visible: visible.map((p) => p.info.targetId), total: infos.length },
        );
      }
    }

    this.page = picked.page;
    picked.page._newDocument('resync');
    try {
      await this.session.send('Target.activateTarget', { targetId: picked.info.targetId });
    } catch {
      /* 非致命 */
    }
    return {
      targetId: picked.info.targetId,
      url: picked.state.url ?? picked.info.url,
      title: picked.state.t ?? picked.info.title,
      active: true,
      reason,
      approximated: !picked.focusedVisible,
    };
  }

  /** 确保有一个可用的当前页；没有就报错（不偷偷开新页）。 */
  requirePage() {
    if (this.page && !this.page.closed) return this.page;
    throw buError(
      'BU_SESSION_STALE',
      '当前没有附着的标签页（可能已被关闭）。请调用 resync() 或 newTab()。',
    );
  }

  /** 浏览器级命令直通（不经过 page session）。 */
  browser(method, params, opts) {
    return this.session.send(method, params, opts);
  }

  /** 下载目录（extra.mjs 会用到；懒创建）。 */
  downloadDirectory() {
    if (!this._downloadDir) {
      const dir = join(tmpdir(), `dsh-cdp-downloads-${process.pid}`);
      if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
      this._downloadDir = dir;
    }
    return this._downloadDir;
  }

  /** 关闭：先收尾页面订阅，再关 WebSocket。保证不留悬挂连接。 */
  async close() {
    if (this.closed) return;
    this.closed = true;
    for (const page of this.pages.values()) {
      page.closed = true;
      page._dispose();
    }
    this.pages.clear();
    this.page = null;
    for (const off of this._off.splice(0)) {
      try {
        off();
      } catch {
        /* ignore */
      }
    }
    this._listeners.clear();
    await this.session.close();
    this.targets.clear();
  }
}

/** 给 makeError 风格调用留个出口（extra.mjs 用）。 */
export { delay };
