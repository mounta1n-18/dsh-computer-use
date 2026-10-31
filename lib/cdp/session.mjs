// lib/cdp/session.mjs
// CDP 底层会话层：纯 ESM，零第三方依赖。
//
// 为什么不用 ws / playwright：
//   Node.js >= 22 原生就有全局 WebSocket（undici 实现），Node 24 上已经稳定。
//   Playwright 体积大、要下浏览器、还会劫持用户 profile；我们只要拨一条 CDP WebSocket。
//
// 为什么每个命令都要超时：
//   CDP 是请求-响应协议，浏览器崩溃/页面卡死时响应永远不会回来。
//   没有超时的话上层 await 会永久挂住，工具调用就变成"卡死"，比报错更糟。
//
// 为什么 close() 必须 resolve 掉所有 pending：
//   否则进程退出前会有悬挂的 Promise 和未关闭的 socket，Node 不会自动退出。

import { randomUUID } from 'node:crypto';

/** 单条 CDP 命令默认超时（毫秒）。 */
export const DEFAULT_TIMEOUT_MS = 15000;

/** 等待 WebSocket 完成握手的超时。Chrome 在但端口被别的东西占了时靠这个兜住。 */
const CONNECT_TIMEOUT_MS = 10000;

/**
 * 带 .code 的错误工厂。上层（Cordis 工具层）靠 code 分派不同的提示语。
 * @param {string} code 稳定错误码字符串
 * @param {string} message 给人看的中文说明
 * @param {object} [extra] 附加字段（比如 cdpCode / url / ref）
 */
export function buError(code, message, extra) {
  const err = new Error(message);
  err.code = code;
  if (extra) Object.assign(err, extra);
  return err;
}

/** CDP 返回 error 时抛出。保留原始 code/message，别把浏览器的话吞掉。 */
export class CdpError extends Error {
  constructor(code, message, data, method) {
    super(`CDP ${method} 失败：${message}${code != null ? ` (code=${code})` : ''}`);
    this.name = 'CdpError';
    this.code = 'BU_CDP_ERROR';
    this.cdpCode = code == null ? null : code;
    this.cdpMessage = message || '';
    this.cdpData = data;
    this.method = method;
  }
}

/** 收集 WebSocket 的错误细节。浏览器 WebSocket 的 error 事件几乎不带信息。 */
function describeSocketError(ws) {
  return `readyState=${ws.readyState}`;
}

function delay(ms) {
  return new Promise((resolve) => {
    // unref：这个定时器不应该拖住进程退出
    const t = setTimeout(resolve, ms);
    if (typeof t.unref === 'function') t.unref();
  });
}

/**
 * 一条 CDP 连接（浏览器级 WebSocket，flatten 模式下所有 target 共用它）。
 *
 * 关于 flatten：Target.attachToTarget 传 flatten:true 之后，页面级命令和事件都在这条
 * WebSocket 上，用顶层 sessionId 区分。这样一条连接就能管所有 tab，不用每个 tab 开一条。
 */
export class Session {
  constructor(ws, label = 'cdp') {
    this.ws = ws;
    this.label = label;
    this.closed = false;
    /** @type {Map<string, {resolve:Function, reject:Function, timer:any, method:string}>} */
    this._pending = new Map();
    /** @type {Map<string, Set<Function>>} */
    this._listeners = new Map();
    /** @type {Set<Function>} */
    this._anyListeners = new Set();
    this._closeWaiters = [];
    /** CDP 事件计数，诊断用 */
    this.eventCount = 0;

    ws.addEventListener('message', (ev) => this._onMessage(ev));
    ws.addEventListener('close', (ev) => this._onClose(ev.code, ev.reason));
    ws.addEventListener('error', () => {
      // 说明：Chrome 侧断开、端口被回收都会走到这里。真正的错误原因在 close 事件里。
      this._onClose(-1, `websocket error (${describeSocketError(ws)})`);
    });
  }

  /** 打开一条 CDP WebSocket 连接。 */
  static async connect(wsUrl, label = 'cdp') {
    if (!/^wss?:\/\//.test(String(wsUrl ?? ''))) {
      throw buError('BU_CDP_ERROR', `不是合法的 CDP WebSocket 地址：${wsUrl}`);
    }
    const ws = new WebSocket(wsUrl);
    await new Promise((resolve, reject) => {
      let settled = false;
      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        try {
          ws.close();
        } catch {
          /* 连接还没建立，close 会抛，忽略 */
        }
        reject(
          new CdpError(-32000, `连接 ${wsUrl} 超时（${CONNECT_TIMEOUT_MS}ms）——确认 Chrome 还在跑`, null, 'connect'),
        );
      }, CONNECT_TIMEOUT_MS);
      ws.addEventListener('open', () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve();
      });
      ws.addEventListener('error', () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        reject(new CdpError(-32000, `无法连接到 ${wsUrl}（Chrome 可能已退出或端口被占用）`, null, 'connect'));
      });
      ws.addEventListener('close', (ev) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        reject(new CdpError(-32000, `连接 ${wsUrl} 在握手阶段被关闭：${ev.reason || ev.code}`, null, 'connect'));
      });
    });
    return new Session(ws, label);
  }

  _onMessage(ev) {
    let msg;
    const raw = ev.data;
    if (typeof raw !== 'string') {
      // CDP 只发文本帧（我们也没启用二进制）；二进制帧直接忽略，避免把 Buffer 当 JSON 解析
      return;
    }
    try {
      msg = JSON.parse(raw);
    } catch {
      return;
    }
    if (msg.id != null) {
      const pending = this._pending.get(msg.id);
      if (!pending) return;
      this._pending.delete(msg.id);
      clearTimeout(pending.timer);
      if (msg.error) {
        pending.reject(
          new CdpError(msg.error.code, msg.error.message, msg.error.data, pending.method),
        );
      } else {
        pending.resolve(msg.result ?? {});
      }
      return;
    }
    if (msg.method) {
      this.eventCount += 1;
      this._dispatch(msg.method, msg.params ?? {}, msg.sessionId);
    }
  }

  _dispatch(method, params, sessionId) {
    const ev = { method, params, sessionId };
    const set = this._listeners.get(method);
    if (set) {
      for (const fn of [...set]) {
        try {
          fn(params, ev);
        } catch (err) {
          // 监听器里的异常绝不能污染协议循环——最多往 stderr 丢一行
          console.error(`[cdp] 事件监听器异常 ${method}: ${err?.message ?? err}`);
        }
      }
    }
    for (const fn of [...this._anyListeners]) {
      try {
        fn(ev);
      } catch (err) {
        console.error(`[cdp] 通配监听器异常 ${method}: ${err?.message ?? err}`);
      }
    }
  }

  _onClose(code, reason) {
    if (this.closed) return;
    this.closed = true;
    const err = buError(
      'BU_SESSION_STALE',
      `CDP 连接已断开（code=${code}${reason ? `, ${reason}` : ''}），请调用 resync() 或 connect() 重新附着`,
      { wsCode: code, wsReason: reason },
    );
    this._rejectAllPending(err);
    for (const w of this._closeWaiters.splice(0)) w();
    this._listeners.clear();
    this._anyListeners.clear();
  }

  _rejectAllPending(err) {
    for (const [id, p] of this._pending) {
      clearTimeout(p.timer);
      this._pending.delete(id);
      p.reject(err);
    }
  }

  /**
   * 发一条 CDP 命令。
   * @param {string} method
   * @param {object} [params]
   * @param {{sessionId?:string, timeoutMs?:number}} [opts]
   */
  send(method, params, opts = {}) {
    if (this.closed) {
      return Promise.reject(
        buError('BU_SESSION_STALE', `CDP 连接已关闭，无法发送 ${method}；请 resync() 重新附着`, { method }),
      );
    }
    const id = ++Session._seq;
    const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    const payload = { id, method };
    if (params && Object.keys(params).length > 0) payload.params = params;
    if (opts.sessionId) payload.sessionId = opts.sessionId;

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this._pending.delete(id);
        reject(
          buError('BU_CDP_ERROR', `CDP 命令 ${method} 超时（${timeoutMs}ms）`, {
            method,
            timeoutMs,
            cdpTimeout: true,
          }),
        );
      }, timeoutMs);
      if (typeof timer.unref === 'function') timer.unref();
      this._pending.set(id, { resolve, reject, timer, method });
      try {
        this.ws.send(JSON.stringify(payload));
      } catch (err) {
        clearTimeout(timer);
        this._pending.delete(id);
        reject(buError('BU_SESSION_STALE', `发送 ${method} 失败：${err?.message ?? err}`, { method }));
      }
    });
  }

  /**
   * 发一条 CDP 命令，但**不因为 CDP error 而 reject**，而是把 {ok, result, error} 交回调用方。
   * 用于"先试一下，失败就走兜底"的场景（比如 Input.insertText 不被支持时的降级）。
   */
  async sendRaw(method, params, opts = {}) {
    try {
      const result = await this.send(method, params, opts);
      return { ok: true, result, error: null };
    } catch (err) {
      if (err instanceof CdpError) return { ok: false, result: null, error: err };
      throw err;
    }
  }

  /** 同时给多个 target 发同一条命令（比如给所有 tab 装下载监听）。 */
  broadcast(method, params, sessionIds, opts = {}) {
    return Promise.allSettled(
      [...sessionIds].map((sid) => this.send(method, params, { ...opts, sessionId: sid })),
    );
  }

  /** 订阅事件；返回退订函数。 */
  on(method, fn) {
    let set = this._listeners.get(method);
    if (!set) {
      set = new Set();
      this._listeners.set(method, set);
    }
    set.add(fn);
    return () => set.delete(fn);
  }

  /** 订阅所有事件（下载完成判定、会话失效兜底时需要）。 */
  onAny(fn) {
    this._anyListeners.add(fn);
    return () => this._anyListeners.delete(fn);
  }

  /** 等待某条事件出现一次。 */
  once(method, timeoutMs = DEFAULT_TIMEOUT_MS) {
    return new Promise((resolve, reject) => {
      let timer = null;
      const off = this.on(method, (params, ev) => {
        if (timer) clearTimeout(timer);
        off();
        resolve({ params, ev });
      });
      if (timeoutMs > 0) {
        timer = setTimeout(() => {
          off();
          reject(buError('BU_CDP_ERROR', `等待事件 ${method} 超时（${timeoutMs}ms）`, { method }));
        }, timeoutMs);
        if (typeof timer.unref === 'function') timer.unref();
      }
    });
  }

  /** 关闭连接：先尝试优雅关闭，超时就强关。保证不留下悬挂 socket。 */
  async close({ graceMs = 300 } = {}) {
    if (this.closed) return;
    this.closed = true;
    this._rejectAllPending(buError('BU_SESSION_STALE', 'CDP 连接正在关闭'));
    try {
      this.ws.close(1000, 'rea-cdp close');
    } catch {
      /* 已经断了就无所谓 */
    }
    // 原生 WebSocket 的 close 事件理论上是同步排队的；给一小段宽限，
    // 不完全依赖它（有些平台 close 事件会晚一拍），超时就直接放手。
    await Promise.race([
      new Promise((resolve) => this._closeWaiters.push(resolve)),
      delay(graceMs),
    ]);
    try {
      if (this.ws.readyState === 0 || this.ws.readyState === 1) this.ws.close();
    } catch {
      /* ignore */
    }
    this._listeners.clear();
    this._anyListeners.clear();
  }
}

Session._seq = 0;

/** 生成一个 CDP 会话内唯一的 id 字符串（给下载记录之类用）。 */
export function newId(prefix = 'id') {
  return `${prefix}_${randomUUID().slice(0, 8)}`;
}

export { delay };
