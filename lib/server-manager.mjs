// 常驻服务的生命周期管理：解析解释器、拉起、探测复用、优雅停止、崩溃自愈。
//
// 三条设计取舍，都是为了避免"多一个进程就多一堆烂摊子"：
//  1. **先探测再拉起**：如果这个端点上已经有活着的服务（比如另一个 DSH 窗口起的），
//     直接复用（owned=false），dispose 时**不杀**它。否则两个窗口会互相杀掉对方的服务。
//  2. **子进程跟随宿主**：不是 detached，宿主退出时子进程一并结束；
//     Windows 上加 windowsHide + CREATE_NO_WINDOW，避免每次弹一个黑框。
//  3. **崩溃自愈**：请求失败 → 下次调用自动重新 ensure 并如实回 restarted=true。
//     绝不静默失败（否则模型会以为动作做过了）。
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { CuClient, CuIpcError, connect, transportConfigFromEnv } from './pipe-client.mjs'

const HERE = path.dirname(fileURLToPath(import.meta.url))
export const PLUGIN_ROOT = path.resolve(HERE, '..')
export const PY_DIR = path.join(PLUGIN_ROOT, 'python')
export const SERVER_SCRIPT = path.join(PY_DIR, 'cu_server.py')

const STDERR_TAIL = 8000

/** 找一个能用的 Python：DSH_CU_PYTHON 优先，其次 py -3，最后 PATH 里的 python。 */
export function pythonCandidates(env = process.env) {
  const list = []
  if (env.DSH_CU_PYTHON) list.push({ cmd: env.DSH_CU_PYTHON, args: [] })
  if (process.platform === 'win32') list.push({ cmd: 'py', args: ['-3'] })
  list.push({ cmd: 'python', args: [] })
  list.push({ cmd: 'python3', args: [] })
  return list
}

async function probeCandidate(cand) {
  return new Promise((resolve) => {
    let out = ''
    let child
    try {
      child = spawn(cand.cmd, [...cand.args, '-c', 'import sys;print(sys.version_info[0], sys.executable)'], {
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
      })
    } catch {
      resolve(null)
      return
    }
    const timer = setTimeout(() => {
      try {
        child.kill()
      } catch {
        /* 已经退了 */
      }
      resolve(null)
    }, 8000)
    child.stdout.on('data', (d) => {
      out += d.toString('utf8')
    })
    child.on('error', () => {
      clearTimeout(timer)
      resolve(null)
    })
    child.on('exit', (code) => {
      clearTimeout(timer)
      if (code !== 0) {
        resolve(null)
        return
      }
      const m = out.trim().match(/^(\d+)\s+(.+)$/)
      if (!m) {
        resolve(null)
        return
      }
      const major = Number(m[1])
      resolve(major >= 3 ? { cmd: cand.cmd, args: cand.args, version: m[1], exe: m[2] } : null)
    })
  })
}

export async function resolvePython(env = process.env) {
  for (const cand of pythonCandidates(env)) {
    const ok = await probeCandidate(cand)
    if (ok) return ok
  }
  throw new CuIpcError(
    '找不到可用的 Python（需要 3.10+）。用 DSH_CU_PYTHON 指定绝对路径，或确认 python 在 PATH 里。',
    'CU_NO_PYTHON',
  )
}

export class ServerManager {
  constructor({ env = process.env, log = () => {} } = {}) {
    this.env = env
    this.log = log
    this.config = transportConfigFromEnv(env)
    this.client = null
    this.child = null
    this.owned = false
    this.python = null
    this.stderrTail = ''
    this.restarts = 0
    this.lastError = null
    this.starting = null
  }

  /** 探测端点上是否已有活服务。 */
  async probe(timeoutMs = 1200) {
    const cfg = { ...this.config, connectTimeout: Math.min(this.config.connectTimeout, 0.6) }
    let conn = null
    try {
      conn = await connect(cfg)
      const body = Buffer.from(JSON.stringify({ id: 1, op: 'ping' }), 'utf8')
      const head = Buffer.alloc(5)
      head.writeUInt32BE(body.length, 0)
      head.writeUInt8(0, 4)
      conn.socket.write(Buffer.concat([head, body]))
      const frame = await conn.readFrame(timeoutMs)
      const json = JSON.parse(frame.payload.toString('utf8'))
      return json?.ok ? json.value : null
    } catch {
      return null
    } finally {
      if (conn) conn.close()
    }
  }

  _spawn(python) {
    const env = {
      ...this.env,
      PYTHONUTF8: '1',
      PYTHONIOENCODING: 'utf-8',
      // 服务端默认把日志写文件；这一条只在排查时打开，避免污染宿主输出
      DSH_CU_SERVER_STDERR: this.env.DSH_CU_SERVER_STDERR || '',
    }
    const child = spawn(python.cmd, [...python.args, SERVER_SCRIPT], {
      cwd: PY_DIR,
      env,
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
      // 关键：不 detached。宿主退出时它必须一起走，否则会留下没人管的 python.exe。
      detached: false,
    })
    const collect = (chunk) => {
      this.stderrTail = (this.stderrTail + chunk.toString('utf8')).slice(-STDERR_TAIL)
    }
    child.stdout.on('data', collect)
    child.stderr.on('data', collect)
    child.on('exit', (code, signal) => {
      if (this.child === child) {
        this.log(`[cu] 服务进程退出 code=${code} signal=${signal}`)
        this.child = null
        this.owned = false
        if (this.client) {
          this.client.close()
          this.client = null
        }
      }
    })
    return child
  }

  _client() {
    if (!this.client) this.client = new CuClient(this.config)
    return this.client
  }

  /** 保证有一个可用服务：能复用就复用，否则拉起并等它就绪。 */
  async ensure({ spawnIfMissing = true } = {}) {
    if (this.starting) return this.starting
    this.starting = (async () => {
      const alive = await this.probe()
      if (alive) {
        if (!this.child) {
          this.owned = false
          this.log(`[cu] 复用已有服务 pid=${alive.pid}`)
        }
        return { adopted: true, ping: alive }
      }
      if (!spawnIfMissing) throw new CuIpcError('常驻服务没有在运行', 'CU_IPC')
      if (!fs.existsSync(SERVER_SCRIPT)) {
        throw new CuIpcError(`找不到服务脚本：${SERVER_SCRIPT}`, 'CU_IPC')
      }
      this.python = this.python || (await resolvePython(this.env))
      this.log(`[cu] 拉起服务 ${this.python.exe}`)
      this.child = this._spawn(this.python)
      this.owned = true
      const deadline = Date.now() + 15000
      let lastErr = null
      while (Date.now() < deadline) {
        if (this.child.exitCode !== null && this.child.exitCode !== undefined) {
          const tail = this.stderrTail.trim().split('\n').slice(-6).join(' | ')
          throw new CuIpcError(
            `服务进程启动即退出（code=${this.child.exitCode}）。stderr：${tail || '（空）'}`,
            this.stderrTail.includes('CU_IPC') ? 'CU_IPC' : 'CU_IPC',
          )
        }
        const ping = await this.probe(800)
        if (ping) return { adopted: false, ping }
        await new Promise((r) => setTimeout(r, 250))
        lastErr = this.stderrTail.trim().split('\n').slice(-3).join(' | ')
      }
      throw new CuIpcError(`服务在 15s 内没有就绪。stderr：${lastErr || '（空）'}`, 'CU_IPC')
    })()
    try {
      return await this.starting
    } finally {
      this.starting = null
    }
  }

  /** 发一个业务请求；连接失败自动重启一次，但**请求本身不重发**。 */
  async call(op, payload = {}, opts = {}) {
    try {
      await this.ensure()
      return await this._client().request(op, payload, opts)
    } catch (e) {
      if (!(e instanceof CuIpcError)) throw e
      // 这里**只重连、不重发**：先把服务拉起来，然后把错误原样抛回去。
      // 调用方（模型）自己决定要不要再做一次——因为上一次可能已经执行了。
      this.lastError = e.message
      try {
        await this.ensure()
        this.restarts += 1
        this._client().reset()
      } catch (e2) {
        throw new CuIpcError(`${e.message}；重启服务也失败：${e2.message}`, e2.code || 'CU_IPC')
      }
      throw e
    }
  }

  async status() {
    const ping = await this.probe()
    return {
      running: !!ping,
      owned: this.owned,
      pid: ping?.pid ?? this.child?.pid ?? null,
      python: this.python ? { version: this.python.version, exe: this.python.exe } : null,
      transport: this.config.kind,
      endpoint: this.config.endpoint || `127.0.0.1:${this.config.port}`,
      uptime: ping?.uptime ?? null,
      requests: ping?.requests ?? null,
      queues: ping?.queues ?? [],
      muteUntil: ping?.mute_until ?? 0,
      modifiedKeys: ping?.modified_keys ?? [],
      lastError: this.lastError,
      restarts: this.restarts,
      stderrTail: this.stderrTail.trim().split('\n').slice(-8).join('\n'),
    }
  }

  async restart() {
    await this.stop()
    const r = await this.ensure()
    return { ...r, status: await this.status() }
  }

  /** 只停自己拉起的那个；复用别人的绝不杀。 */
  async stop({ timeoutMs = 1500 } = {}) {
    const child = this.child
    const owned = this.owned
    if (child && owned) {
      // 即使本次会话没发过业务请求（client 尚未建立），也要主动发 shutdown：
      // 服务端的 accept 循环不响应 stopping，必须由宿主催它退出，否则只能等
      // 超时强杀；旧实现等 6s，已经超过桌面端 5s 的关机预算，会让更新永远装不上。
      if (!this.client) this.client = this._client()
      try {
        await this.client.request('shutdown', {}, { timeoutMs: 1000 })
      } catch {
        /* 服务可能已经不在了，继续走强杀 */
      }
    }
    if (this.client) {
      this.client.close()
      this.client = null
    }
    if (!child) {
      this.owned = false
      return { stopped: !owned, owned }
    }
    const exited = await new Promise((resolve) => {
      const timer = setTimeout(() => resolve(false), timeoutMs)
      child.once('exit', () => {
        clearTimeout(timer)
        resolve(true)
      })
    })
    if (!exited) {
      try {
        child.kill()
      } catch {
        /* 已经退出 */
      }
    }
    this.child = null
    this.owned = false
    return { stopped: true, owned, graceful: exited }
  }
}
