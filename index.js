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
import { randomUUID } from 'node:crypto'

import { ServerManager } from './lib/server-manager.mjs'
import { createComputerUseTool } from './lib/computer-use.mjs'
import { createBrowserUseTool } from './lib/browser-use.mjs'

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
  // sessions 只用来把"是哪个会话在操作"写上横幅（Agent 本身只有 id，没有目录）。
  const sessions = ctx.get ? ctx.get('sessions') : undefined

  // 工具注册：任一失败都只记日志，绝不抛——插件加载失败会拖垮整个 profile。
  try {
    ctx.tools.register(createComputerUseTool({ manager, attachments, sessions }))
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

  // 控制权跨回合：允许，但**不许在还握着控制权的时候把回合放下不管**。
  //
  // 用户的硬要求（2026-11-15）：cu 还在运行、DSH 还没被交还到前台之前，
  // 这一轮不许就这么结束——否则控制权空挂、用户下一条指令模型也收不到。
  // `agent/turn-stopping` 是 serial 事件、否决不了回合，所以这里做的是"回合刚停就把它
  // 拽回来"：立刻给同一个会话 followup 一条提示，让它继续挂着（hold）或收尾。
  // 它不替模型关 cu，也不抢执行权，只保证"cu 开着 ⟹ 会话不会静默睡下去"。
  const TURN_NUDGE = [
    '【控制权还在你手里】cu 仍开着，所以这一轮不能就这么结束。',
    '现在做两件事之一：',
    '1) 还要继续操作：调 computer_use action="hold" 等用户的话（每轮最多 55 秒，超时就再调一次），',
    '   用户一说话就会带着原话返回；',
    '2) 已经干完：调 action="deactivate" 交还并把 cu 关掉，再结束回合。',
    '不要沉默地把回合结束掉——用户下一条指令会丢。',
  ].join('\n')
  if (typeof ctx.on === 'function') {
    let lastNudge = 0
    ctx.on('agent/turn-stopping', async ({ agent } = {}) => {
      try {
        if (!agent?.id) return
        // 拽回来也要有节奏：模型硬是不听话时，20 秒内只拽一次，别把会话刷屏。
        if (Date.now() - lastNudge < 20000) return
        const alive = await manager.probe(300)
        if (!alive) return
        const res = await manager.call('control', {}, { timeoutMs: 3000 })
        const c = res?.json?.value?.control
        if (!c?.active || c.emergency) return
        if (String(c.owner || '') !== String(agent.id)) return
        // 有留言没送出去时别插话：轮询线程会用用户原话把它叫醒，比这条模板更准。
        const q = await manager.call('messages', {}, { timeoutMs: 3000 })
        if ((q?.json?.value?.items || []).length) return
        if (typeof agent.followup === 'function') {
          lastNudge = Date.now()
          agent.followup({
            id: randomUUID(),
            role: 'user',
            content: [{ type: 'text', text: TURN_NUDGE }],
            source: { kind: 'user' },
          })
          log('[cu] 回合结束时控制权还在手里，已把会话拽回来继续挂着')
        }
      } catch (e) {
        log(`[cu] 回合守卫失败（不影响收尾）：${e.message}`)
      }
    })
  }

  // ---- 留言唤醒 ----
  //
  // 用户点鲸鱼留的话，如果只挂在"下一次工具结果"里，那么会话一旦答完话睡着、
  // 再也不调用工具，留言就永远到不了模型面前（2026-11-15 实测：用户连发三条，
  // 模型一条都没看见）。所以这里另开一条路：轮询服务端的留言队列，拿到后用
  // `agent.followup()` 直接叫醒那个会话——followup 会开一个新回合，留言就是
  // 用户消息本身。
  const agents = ctx.get ? ctx.get('agents') : undefined
  if (agents && typeof agents.get === 'function') {
    let relaying = false
    const relay = async () => {
      if (relaying) return
      relaying = true
      try {
        const ping = await manager.probe(200)
        if (!ping) return
        const res = await manager.call('messages', {}, { timeoutMs: 3000 })
        const items = res?.json?.value?.items || []
        if (!items.length) return
        const done = []
        for (const item of items) {
          const owner = String(item.owner || '')
          const agent = owner ? agents.get(owner) : undefined
          // 会话还活着才叫得醒；叫不醒就先留着（用户可能只是把标签页关了，
          // 等他重新打开这个会话，下一次轮询还能送进去）。超过 10 分钟就不再等。
          if (!agent || typeof agent.followup !== 'function') {
            if (Date.now() / 1000 - Number(item.at || 0) > 600) done.push(item.id)
            continue
          }
          try {
            agent.followup({
              id: randomUUID(),
              role: 'user',
              content: [{ type: 'text', text: `【用户在电脑控制期间的留言】${String(item.text || '')}` }],
              source: { kind: 'user' },
            })
            done.push(item.id)
            log(`[cu] 已把留言送进会话 ${owner}`)
          } catch (e) {
            log(`[cu] 唤醒会话 ${owner} 失败：${e.message}`)
          }
        }
        if (done.length) await manager.call('messages', { ack: done }, { timeoutMs: 3000 })
      } catch (e) {
        log(`[cu] 留言轮询失败：${e.message}`)
      } finally {
        relaying = false
      }
    }
    const timer = setInterval(() => { relay().catch(() => {}) }, 1200)
    if (typeof timer.unref === 'function') timer.unref()
    if (typeof ctx.effect === 'function') {
      ctx.effect(() => () => clearInterval(timer))
    }
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
