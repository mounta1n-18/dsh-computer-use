// 隔离会话（shadow desktop）的前端粘合层。
//
// 设计：把 **整个 cu_server** 搬到一个独立桌面上跑（`python/shadow.py start`），
// 客户端只换端点。于是同一个 `computer_use` 工具可以二选一：
//   desktop:'main'   → 用户的真实桌面（默认，行为与以前完全一致）
//   desktop:'shadow' → 隔离桌面（不抢鼠标、看不见，但真实 SendInput 被系统拒绝）
//
// 为什么不默认 shadow：`shot` 要看用户的屏幕、`focus 微信` 要抓用户已开的窗口，
// 这些在隔离桌面里天然不存在。默认值由 DSH_CU_DEFAULT_DESKTOP 控制。

import { spawn } from 'node:child_process'
import { readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'

export const SHADOW_DESKTOP = 'dsh_shadow'
export const SHADOW_ENDPOINT = '\\\\.\\pipe\\dsh-cu-shadow'

function pluginRoot() {
  // 本文件在 <plugin>/lib/ 下
  return path.resolve(path.dirname(new URL(import.meta.url).pathname.slice(1)), '..')
}

export function shadowStatePath() {
  return path.join(process.env.TEMP || tmpdir(), 'cu', 'dsh-shadow.json')
}

export async function readShadowState() {
  try {
    return JSON.parse(await readFile(shadowStatePath(), 'utf8'))
  } catch {
    return null
  }
}

function runShadowPy(args, timeoutMs = 60000) {
  return new Promise((resolve) => {
    const py = process.env.DSH_CU_PYTHON || 'python'
    const script = path.join(pluginRoot(), 'python', 'shadow.py')
    let out = ''
    let err = ''
    let child
    try {
      child = spawn(py, [script, ...args], { windowsHide: true })
    } catch (e) {
      resolve({ ok: false, code: 1, out: '', err: String(e) })
      return
    }
    child.stdout.on('data', (d) => { out += d.toString('utf8') })
    child.stderr.on('data', (d) => { err += d.toString('utf8') })
    const timer = setTimeout(() => {
      try { child.kill() } catch { /* 已经退出 */ }
    }, timeoutMs)
    child.on('error', (e) => {
      clearTimeout(timer)
      resolve({ ok: false, code: 1, out, err: `${err}${e.message}` })
    })
    child.on('close', (code) => {
      clearTimeout(timer)
      resolve({ ok: code === 0, code: code ?? 1, out, err })
    })
  })
}

/** 确保隔离会话在跑；返回 { endpoint, desktop, started }。 */
export async function ensureShadow() {
  const st = await readShadowState()
  if (st && st.endpoint) {
    // 状态文件在，但进程可能已经死了：让 shadow.py status 判一次
    const r = await runShadowPy(['status'], 20000)
    if (r.ok) return { endpoint: st.endpoint, desktop: st.desktop, started: false }
  }
  const r = await runShadowPy(['start'], 90000)
  const st2 = await readShadowState()
  if (!r.ok || !st2) {
    throw new Error(`隔离会话起不来：${(r.err || r.out || '').trim().slice(0, 300)}`)
  }
  return { endpoint: st2.endpoint, desktop: st2.desktop, started: true,
           detail: (r.out || '').trim() }
}

export async function stopShadow() {
  const r = await runShadowPy(['stop'], 30000)
  return { ok: r.ok, detail: (r.out || r.err || '').trim() }
}

export async function shadowStatus() {
  const st = await readShadowState()
  if (!st) return { running: false }
  const r = await runShadowPy(['status'], 20000)
  return { running: r.ok, desktop: st.desktop, endpoint: st.endpoint,
           pid: st.pid, detail: (r.out || '').trim() }
}

export function defaultDesktop() {
  // 默认回**用户桌面**。2026-10-18 的实测证明 shadow 还不能当默认：
  // 整屏 shot 抓不到、UWP 应用起不来、真实注入被系统拒绝；而且一旦某条调用卡住，
  // 其余调用会跟着白等 60s（一次真实任务因此白烧 3 分钟）。
  // 现在靠"主桌面动键鼠必须先获批"这条硬闸来保证不抢鼠标；shadow 作为可选项。
  const v = String(process.env.DSH_CU_DEFAULT_DESKTOP || 'main').toLowerCase()
  return v === 'shadow' ? 'shadow' : 'main'
}
