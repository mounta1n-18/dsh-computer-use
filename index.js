// DSH Cordis 插件：computer-use / browser-use 两个工具 + 自带技能 + 常驻 Python 服务。
//
// 为什么是"插件"而不只是一个技能：
//  - 技能只能教模型怎么调外部命令；插件能把工具**注册进工具表**，模型直接调用，
//    不用每步都经过一次 shell、也不用自己拼命令行（拼错的代价是静默做错事）。
//  - 生命周期由宿主管：加载即拉起常驻服务，卸载即回收；宿主退出子进程跟着走。
//  - 截图能作为真正的图片块返回（走 attachments），不再"写临时文件再 read"。
//
// 依赖注入只有三个，都是 DSH 一等服务：
//   tools（注册工具）、attachments（把 PNG 变成图片块）、skills（注册自带技能）。
// computerUse / browserUse 两个"独占 provider 槽"是**可选**的：装上就算我们占了坑，
// 已经被别人占就只记一条日志、工具照常注册（绝不让注册失败拖垮整个插件）。
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { ServerManager } from './lib/server-manager.mjs'
import { createComputerUseTool } from './lib/computer-use.mjs'
import { createBrowserUseTool } from './lib/browser-use.mjs'
import { ensureShadow, stopShadow, shadowStatus, defaultDesktop, SHADOW_ENDPOINT } from './lib/shadow-session.mjs'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const SKILLS_DIR = path.join(HERE, 'skills')

export const name = 'computer-use'
export const inject = ['tools', 'attachments', 'skills']

/** 读一个技能目录里的 SKILL.md，拆出 frontmatter。 */
function readSkill(dirName) {
  const base = path.join(SKILLS_DIR, dirName)
  const file = path.join(base, 'SKILL.md')
  if (!fs.existsSync(file)) return null
  const raw = fs.readFileSync(file, 'utf8')
  const m = raw.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/)
  const meta = {}
  if (m) {
    for (const line of m[1].split(/\r?\n/)) {
      const kv = line.match(/^([A-Za-z_]+):\s*(.*)$/)
      if (kv) meta[kv[1]] = kv[2].replace(/^["']|["']$/g, '')
    }
  }
  return {
    name: meta.name || dirName,
    description: meta.description || '',
    content: raw,
    resourceBase: { kind: 'directory', path: base },
    source: 'runtime',
  }
}

function registerSkills(ctx, log) {
  if (!ctx.skills || typeof ctx.skills.register !== 'function') {
    log('[cu] 没有 skills 服务，跳过自带技能注册')
    return
  }
  for (const dir of ['computer-use', 'browser-use']) {
    const skill = readSkill(dir)
    if (!skill) {
      log(`[cu] 技能目录 ${dir} 没有 SKILL.md，跳过`)
      continue
    }
    try {
      ctx.skills.register(skill)
      log(`[cu] 已注册技能 ${skill.name}`)
    } catch (e) {
      log(`[cu] 注册技能 ${skill.name} 失败：${e.message}`)
    }
  }
}

/** 占住"独占 provider 槽"；被人抢先就只记日志，不影响工具。 */
function claimProvider(ctx, service, label, log) {
  let svc
  try {
    svc = ctx.get ? ctx.get(service) : undefined
  } catch {
    svc = undefined
  }
  if (!svc || typeof svc.register !== 'function') return
  try {
    const dispose = svc.register('dsh-computer-use')
    log(`[cu] 已占 ${service} provider 槽（${label}）`)
    if (typeof ctx.effect === 'function') {
      ctx.effect(() => () => Promise.resolve(dispose?.()).catch(() => {}))
    }
  } catch (e) {
    log(`[cu] ${service} 槽位已被占用，工具照常注册：${e.message}`)
  }
}

export function apply(ctx, config = {}) {
  const log = (msg) => {
    if (process.env.DSH_CU_DEBUG) console.error(msg)
  }

  const manager = new ServerManager({ log })
  const attachments = ctx.attachments

  // 隔离会话（shadow desktop）：桌面:'shadow' 时把调用改投到一个**独立桌面**上的
  // cu_server。它不抢用户的鼠标（真 SendInput 在非输入桌面上被系统拒绝），
  // 元素级/截图/剪贴板/凝视照常。懒创建：只有真用到才起进程。
  let shadowMgr = null
  const shadow = {
    defaultDesktop,
    async getManager() {
      if (!shadowMgr) {
        await ensureShadow()                       // 确保 python 侧会话在跑
        shadowMgr = new ServerManager({
          log,
          env: {
            ...process.env,
            DSH_CU_TRANSPORT: 'named_pipe',
            DSH_CU_ENDPOINT: SHADOW_ENDPOINT,
          },
        })
      }
      return shadowMgr
    },
    ensure: ensureShadow,
    stop: async () => {
      const r = await stopShadow()
      shadowMgr = null
      return r
    },
    status: shadowStatus,
  }

  // 工具注册：任一失败都只记日志，绝不抛——插件加载失败会拖垮整个 profile。
  try {
    ctx.tools.register(createComputerUseTool({ manager, attachments, shadow }))
    log('[cu] 已注册 computer_use')
  } catch (e) {
    log(`[cu] 注册 computer_use 失败：${e.stack || e.message}`)
  }
  try {
    ctx.tools.register(createBrowserUseTool({ attachments, log }))
    log('[cu] 已注册 browser_use')
  } catch (e) {
    log(`[cu] 注册 browser_use 失败：${e.stack || e.message}`)
  }

  registerSkills(ctx, log)
  claimProvider(ctx, 'computerUse', 'desktop', log)
  claimProvider(ctx, 'browserUse', 'browser', log)

  // 生命周期：默认加载即拉起（对齐"插件自动拉起"的要求）；
  // DSH_CU_LAZY=1 时改为首次调用再起，省一个常驻进程。
  const lazy = process.env.DSH_CU_LAZY === '1'
  if (!lazy) {
    manager.ensure().then(
      () => log('[cu] 常驻服务就绪'),
      (e) => log(`[cu] 常驻服务没能起来（不阻塞插件加载）：${e.message}`),
    )
  }

  // 收尾：只停自己拉起的那个；复用别人的绝不杀。
  const cleanup = async () => {
    try {
      await manager.stop()
    } catch (e) {
      log(`[cu] 停止常驻服务时出错：${e.message}`)
    }
  }
  if (typeof ctx.effect === 'function') {
    ctx.effect(() => cleanup)
  } else if (typeof ctx.on === 'function') {
    ctx.on('dispose', cleanup)
  }

  return { manager }
}

export default { name, inject, apply }
