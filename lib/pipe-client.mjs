// 与 Python 常驻服务通信的 Node 客户端。
//
// 协议（与 python/cu_server.py 严格一致）：
//   帧 = [u32 BE 长度][u8 类型][载荷]；类型 0=JSON、1=PNG 二进制、2=日志
//   请求 = JSON 帧；响应 = JSON 帧（需要时紧跟一个二进制帧）
//
// 三条不变量（都是对齐豆包 GacTransportClient 的做法，理由见注释）：
//   1. **不重发**：写/读任何一步失败，本次请求直接失败，绝不在新连接上重试。
//      对端可能已经执行过了——重试等于把一次点击变成两次点击。
//   2. **请求串行**：桌面操作天然互斥，服务端也持锁；这里用一条 promise 链排队，
//      避免"两个请求的帧交错"这种最难查的错。
//   3. **失败后作废连接**：脏流绝不复用，下一个请求前重连。
import net from 'node:net'

export const KIND_JSON = 0
export const KIND_BINARY = 1
export const KIND_LOG = 2

export const PROTOCOL_VERSION = 1

const MAX_JSON = 8 * 1024 * 1024
const MAX_BINARY = 32 * 1024 * 1024

/** 把 DSH_CU_* 环境变量解析成传输配置；非法值直接抛可执行的错。 */
export function transportConfigFromEnv(env = process.env) {
  const isWin = process.platform === 'win32'
  const kind = (env.DSH_CU_TRANSPORT || (isWin ? 'named_pipe' : 'uds')).trim().toLowerCase()
  if (!['named_pipe', 'uds', 'tcp'].includes(kind)) {
    throw new Error(`GUI 操作不可用：DSH_CU_TRANSPORT 只支持 named_pipe / uds / tcp（收到 ${kind}）`)
  }
  let endpoint = (env.DSH_CU_ENDPOINT || '').trim()
  if (kind === 'named_pipe' && !endpoint) endpoint = '\\\\.\\pipe\\dsh-computer-use'
  if (kind === 'uds' && !endpoint) {
    const base = env.LOCALAPPDATA || env.TMPDIR || '/tmp'
    endpoint = `${base}/dsh-computer-use/cu.sock`
  }
  const port = Number(env.DSH_CU_PORT || 0)
  if (kind === 'tcp' && !port) {
    throw new Error('GUI 操作不可用：DSH_CU_TRANSPORT=tcp 时必须给 DSH_CU_PORT')
  }
  const num = (name, dflt) => {
    const v = env[name]
    if (v === undefined || v === '') return dflt
    const n = Number(v)
    if (!Number.isFinite(n)) throw new Error(`GUI 操作不可用：${name} 不是数字（收到 ${v}）`)
    return n
  }
  return {
    kind,
    endpoint,
    port,
    fallbackKind: (env.DSH_CU_FALLBACK_TRANSPORT || '').trim().toLowerCase(),
    fallbackPort: Number(env.DSH_CU_FALLBACK_PORT || 0),
    connectTimeout: num('DSH_CU_CONNECT_TIMEOUT', 5.0),
    ioTimeout: num('DSH_CU_IO_TIMEOUT', 30.0),
  }
}

export class CuIpcError extends Error {
  constructor(message, code = 'CU_IPC') {
    super(message)
    this.name = 'CuIpcError'
    this.code = code
  }
}

/** 一条已建立的连接；只负责分帧收发，不理解业务。 */
class Connection {
  constructor(socket) {
    this.socket = socket
    this.buf = Buffer.alloc(0)
    this.waiters = []
    this.closed = false
    socket.on('data', (chunk) => {
      this.buf = this.buf.length ? Buffer.concat([this.buf, chunk]) : chunk
      this._drain()
    })
    socket.on('error', (e) => this._fail(new CuIpcError(`管道错误：${e.message}`)))
    socket.on('close', () => this._fail(new CuIpcError('管道被对端关闭')))
  }

  _fail(err) {
    if (this.closed) return
    this.closed = true
    const waiters = this.waiters
    this.waiters = []
    for (const w of waiters) w.reject(err)
  }

  _drain() {
    while (this.waiters.length) {
      const frame = this._nextFrame()
      if (!frame) return
      this.waiters.shift().resolve(frame)
    }
  }

  _nextFrame() {
    if (this.buf.length < 5) return null
    const size = this.buf.readUInt32BE(0)
    const kind = this.buf.readUInt8(4)
    const limit = kind === KIND_BINARY ? MAX_BINARY : MAX_JSON
    if (size > limit) {
      this._fail(new CuIpcError(`对端声明的帧过大：${size} > ${limit}`))
      return null
    }
    if (this.buf.length < 5 + size) return null
    const payload = this.buf.subarray(5, 5 + size)
    this.buf = this.buf.subarray(5 + size)
    return { kind, payload: Buffer.from(payload) }
  }

  /** 等下一个帧，带超时。超时即视为失败（脏流不可复用）。 */
  readFrame(timeoutMs) {
    const frame = this._nextFrame()
    if (frame) return Promise.resolve(frame)
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        const i = this.waiters.indexOf(waiter)
        if (i >= 0) this.waiters.splice(i, 1)
        reject(new CuIpcError(`等待服务响应超时（${timeoutMs}ms）`))
      }, timeoutMs)
      const waiter = {
        resolve: (f) => {
          clearTimeout(timer)
          resolve(f)
        },
        reject: (e) => {
          clearTimeout(timer)
          reject(e)
        },
      }
      this.waiters.push(waiter)
    })
  }

  writeFrame(kind, payload) {
    if (this.closed) throw new CuIpcError('连接已关闭')
    const limit = kind === KIND_BINARY ? MAX_BINARY : MAX_JSON
    if (payload.length > limit) throw new CuIpcError(`帧过大：${payload.length} > ${limit}`)
    const head = Buffer.alloc(5)
    head.writeUInt32BE(payload.length, 0)
    head.writeUInt8(kind, 4)
    this.socket.write(Buffer.concat([head, payload]))
  }

  close() {
    this.closed = true
    try {
      this.socket.destroy()
    } catch {
      /* 关闭失败不算错 */
    }
  }
}

function connectOnce(config, timeoutMs) {
  return new Promise((resolve, reject) => {
    let socket
    if (config.kind === 'tcp') {
      socket = net.connect({ host: '127.0.0.1', port: config.port })
    } else if (config.kind === 'uds') {
      socket = net.connect({ path: config.endpoint })
    } else {
      socket = net.connect({ path: config.endpoint })
    }
    const timer = setTimeout(() => {
      socket.destroy()
      reject(new CuIpcError(`连接 ${config.kind}:${config.endpoint || config.port} 超时（${timeoutMs}ms）`))
    }, timeoutMs)
    socket.once('connect', () => {
      clearTimeout(timer)
      socket.removeAllListeners('error')
      resolve(new Connection(socket))
    })
    socket.once('error', (e) => {
      clearTimeout(timer)
      reject(new CuIpcError(`连接失败：${e.message}`))
    })
  })
}

export async function connect(config = transportConfigFromEnv()) {
  const primary = config.fallbackKind
    ? Math.min(config.connectTimeout, 0.5)
    : config.connectTimeout
  try {
    return await connectOnce(config, primary * 1000)
  } catch (e) {
    if (config.fallbackKind !== 'tcp') throw e
    return await connectOnce({ ...config, kind: 'tcp', port: config.fallbackPort },
      config.connectTimeout * 1000)
  }
}

/**
 * 面向业务的客户端：一个连接 + 一条请求队列。
 * 断开后**不自动重连当前请求**，但下一个请求会重连（这是"不重发"与"可恢复"的平衡点）。
 */
export class CuClient {
  constructor(config) {
    this.config = config || transportConfigFromEnv()
    this.conn = null
    this.seq = 0
    this.chain = Promise.resolve()
    this.unrecoverable = false
  }

  async _ensure() {
    if (this.unrecoverable) {
      throw new CuIpcError('上一次通信失败后本客户端已作废，请新建一个')
    }
    if (this.conn) return this.conn
    this.conn = await connect(this.config)
    return this.conn
  }

  /** 单次请求；返回 {json, binary}。失败会把连接作废。 */
  async request(op, payload = {}, { binary = false, timeoutMs } = {}) {
    const run = async () => {
      const conn = await this._ensure()
      this.seq += 1
      const body = Buffer.from(JSON.stringify({ id: this.seq, op, ...payload }), 'utf8')
      const ioTimeout = (timeoutMs ?? this.config.ioTimeout * 1000)
      try {
        conn.writeFrame(KIND_JSON, body)
        const head = await conn.readFrame(ioTimeout)
        if (head.kind !== KIND_JSON) {
          throw new CuIpcError(`期待 JSON 帧，收到类型 ${head.kind}`)
        }
        let json
        try {
          json = JSON.parse(head.payload.toString('utf8'))
        } catch (e) {
          throw new CuIpcError(`响应不是合法 JSON：${e.message}`, 'CU_PROTOCOL')
        }
        // 真正该做校验的是**消费方**：服务端自检自己刚拼的字典永远通过（假防御）。
        // id 对不上说明帧序错位、协议层已不可信；ok 不是布尔说明对端实现不对。
        if (typeof json.ok !== 'boolean' || json.id !== this.seq) {
          throw new CuIpcError(
            `响应信封不合规（id=${json.id} 期望 ${this.seq}，ok=${typeof json.ok}）`,
            'CU_PROTOCOL',
          )
        }
        let bin = null
        // 只有**成功**的图片响应才会跟随一个二进制帧。失败时服务端只发 JSON 错误帧，
        // 这里若无条件 readFrame 就会一直等到 IO 超时（实测 60s），
        // 而且会把连接标成不可恢复、连累之后每一次调用——这曾是"整条工具链卡死"的根因。
        const wantBin = (binary || json.hasImage) && json.ok === true
        if (wantBin) {
          const frame = await conn.readFrame(ioTimeout)
          if (frame.kind === KIND_BINARY) bin = frame.payload
        }
        return { json, binary: bin }
      } catch (e) {
        // 关键：不重发。脏流作废，连接丢弃；下一个请求会重连。
        this.unrecoverable = true
        this.close()
        throw e instanceof CuIpcError ? e : new CuIpcError(String(e?.message || e))
      }
    }
    const result = this.chain.then(run, run)
    this.chain = result.then(
      () => undefined,
      () => undefined,
    )
    return result
  }

  ping() {
    return this.request('ping', {}, { timeoutMs: 5000 })
  }

  close() {
    if (this.conn) {
      this.conn.close()
      this.conn = null
    }
  }

  /** 允许在服务重启后复用同一个客户端对象。 */
  reset() {
    this.close()
    this.unrecoverable = false
  }
}
