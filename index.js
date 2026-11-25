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
import { createThinkingTool } from './lib/thinking.mjs'

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
  // 会话在界面里显示的名字（用户可改名）；同意弹窗与横幅优先显示它。
  const sessionTitle = ctx.get ? ctx.get('sessionTitle') : undefined

  // 自适应思考：cu 接管期间默认关思维链，交还恢复；模型可用 thinking_control 自行调档。
  const adaptive = config?.adaptiveThinking !== false

  // 工具注册：任一失败都只记日志，绝不抛——插件加载失败会拖垮整个 profile。
  try {
    ctx.tools.register(createComputerUseTool({ manager, attachments, sessions, sessionTitle, adaptive }))
    log('[cu] 已注册 computer_use')
  } catch (e) {
    log(`[cu] 注册 computer_use 失败：${e.stack || e.message}`)
  }
  try {
    const thinking = createThinkingTool({
      enabled: adaptive,
      onLabel: (label) => {
        try { manager.call('phase', { label }, { timeoutMs: 1500 }).catch(() => {}) } catch {}
      },
    })
    if (thinking) {
      ctx.tools.register(thinking)
      log('[cu] 已注册 thinking_control（自适应思考）')
    }
  } catch (e) {
    log(`[cu] 注册 thinking_control 失败：${e.stack || e.message}`)
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

  // 退回合闸门（用户 2026-11-20 的硬要求，第三次返工后定稿）：
  //
  //   只在 cu 还开着的时候拦，平时一根手指都不碰。
  //   cu 没关就不许结束回合——不是替模型关（那是作弊），而是**把它拽回来**：
  //   往它的 next-step 队列里塞一条指令，让主循环再跑一步，逼它自己 deactivate。
  //
  // DSH 的官方口径：turn-stopping 的返回值/异常都不算"反对"，唯一的开关是
  // 监听器里调 agent.steer(...) 往 inbox 塞数据；塞完 `inbox.nextStep.length > 0`，
  // 循环就不会 break（`dsh-agent-loop/lib/index.js:803-811`）。
  //
  // 消息不冒充用户：source.kind 用自定义的 'cu-gate'，客户端把它渲染成折叠的
  // "上下文注入"行（`dsh-client-ui-chat/.../message.ts:96` 判 kind !== 'user'），
  // 不进聊天气泡，也不会像以前那样让模型在用户面前自说自话。
  const gateAttempts = new Map()
  const GATE_MAX_TRIES = 4

  // 状态字：模型开始流式输出 = 真的在"思考中"。以前只有工具边界才推阶段，
  // 模型在两个工具之间长时间推理时，右下角会一直停在"操作中"，看着像没反应。
  let lastThinkPush = 0
  if (typeof ctx.on === 'function') {
    ctx.on('agent/assistant-stream', (frame) => {
      try {
        const agent = frame?.agent
        if (!agent?.id) return
        const now = Date.now()
        if (now - lastThinkPush < 1200) return
        lastThinkPush = now
        manager.call('phase', { label: '思考中' }, { timeoutMs: 1500 }).catch(() => {})
      } catch {
        /* 状态字失败绝不影响主流程 */
      }
    })
  }

  if (typeof ctx.on === 'function') {
    ctx.on('agent/turn-stopping', async ({ agent, turn } = {}) => {
      try {
        if (!agent?.id) return
        const alive = await manager.probe(300)
        if (!alive) return
        const res = await manager.call('control', {}, { timeoutMs: 3000 })
        const c = res?.json?.value?.control
        // 没租约 = 没在用 cu：正常收尾，什么都不做。
        if (!c?.active) return
        if (String(c.owner || '') !== String(agent.id)) return

        const key = `${agent.id}:${turn}`
        const n = gateAttempts.get(key) || 0
        if (n >= GATE_MAX_TRIES) {
          log(`[cu] 退回合闸门：同一回合已拦 ${n} 次，放弃（cu 仍开着，用户看得见）`)
          return
        }
        gateAttempts.set(key, n + 1)

        const text = '[cu 闸门] 你还握着这台电脑的控制权（cu 没关）。现在不能结束回合：'
          + '先调 computer_use action="deactivate" 妥善收工（它会关提示层、把 DSH 切回前台），'
          + '确认之后才能结束本轮。如果你只是想等用户说话，改调 action="hold"。'
          + '不要用"结束回合"来甩掉控制权——用户会看不见模型是否还在用电脑。'
        const message = {
          id: globalThis.crypto?.randomUUID?.() || `cu-gate-${Date.now()}`,
          role: 'user',
          content: [{ type: 'text', text }],
          source: { kind: 'cu-gate', form: 'notice', summary: 'cu 未关闭，回合被拦回' },
        }
        try {
          agent.steer(message)
        } catch (e) {
          // 兜底：老版本 Agent 可能不接受外来的 source 种类，退回普通文本，
          // 至少把模型拽回来（宁可多一行上下文，也不能让它把 cu 晾着）。
          log(`[cu] 退回合闸门 steer 失败，改 inject：${e.message}`)
          if (typeof agent.inject === 'function') agent.inject(text)
          else if (typeof agent.steer === 'function') agent.steer(text)
        }
        log(`[cu] 退回合闸门：cu 仍开着，已把模型拽回下一步（第 ${n + 1} 次）`)
      } catch (e) {
        log(`[cu] 退回合闸门失败（不影响收尾）：${e.message}`)
      }
    })
  }

  // ---- 留言送达：只走 hold 的工具结果 ----
  //
  // 以前这里用 agent.followup 把留言直接塞进会话，好处是睡着了也能叫醒；
  // 但那会在对话界面留下一条用户消息（用户 2026-11-20 明确禁止：提示词只能在
  // 后台轨迹里）。现在留言只挂在服务端队列里，等模型下一次 hold 时随工具结果带回，
  // 轨迹里可见、对话里不可见。relay（鲸鱼环颜色）全在 Python 侧维护。

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
