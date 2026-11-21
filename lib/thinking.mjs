// thinking.mjs — 模型自己控制"下一步思考多深"的那一小块。
//
// 背景（2026-11 实测）：DSH 没有官方的"单请求覆盖 reasoningEffort"钩子。
//   · agent/request 已 deprecated，改 effort 直接抛错；
//   · llm/stream 拿到的 options 被 deep-freeze（只能读，不能改）；
//   · ConfigureCall 是官方钩子，但 agent-loop 根本不传 configure；
//   · DeepSeek 适配器也没有额外的 header 钩子。
// 官方可行的一条路是：改**这个会话**的模型选择事件 model/selection
// （AgentModel.select），下一个 step 生效，界面上的"推理等级"也会实时跟着变。
//
// 所以这里不做"偷偷改请求头"，而是把"调档"包装成一个轻量工具给模型：
//   thinking_control(action="set", effort="off"|"low"|"high"|"max",
//                    why="一句话为什么")
// 模型在一步动作的结尾自己决定下一步要不要思考、思考多深。
//
// 默认策略（可配）：cu 接管期间把思考打到 off；交还时恢复用户原来的档位。
// 每档对应一个很短的状态字，贴在右下角鲸鱼下面，用户随时看得见。

export const EFFORTS = ['off', 'low', 'high', 'max']

export const EFFORT_LABEL = {
  off: '思考关闭',
  low: '思考低',
  high: '思考高',
  max: '思考拉满',
}

/** 每个会话在被自动降档前，用户原来的档位（用于交还时恢复）。 */
const savedEffort = new Map()

/**
 * 这个会话有没有出现过 off（不思考）。
 * 为什么记它：DeepSeek 在思考模式下要求把上一轮的 thinking 原样回传；
 * 一旦中间有一步是 off 生成的，历史里就缺 thinking，再切回 low/high/max
 * 会直接 400：`content[].thinking ... must be passed back`。
 * 所以 off 是**单向门**：本会话关了思考，就只能新开会话再打开。
 */
const everOff = new Map()

export function sawOff(sessionId) {
  return everOff.get(String(sessionId)) === true
}

export function rememberEffort(sessionId, effort) {
  if (sessionId) savedEffort.set(String(sessionId), String(effort || 'high'))
}

export function recallEffort(sessionId) {
  return savedEffort.get(String(sessionId))
}

export function forgetEffort(sessionId) {
  savedEffort.delete(String(sessionId))
}

/** 取当前会话的模型选择；拿不到返回 null（宿主没给 agent 或没选过模型）。 */
export function currentSelection(exec) {
  try {
    const m = exec?.agent?.model
    if (!m || typeof m.current !== 'function') return null
    return m.current() || null
  } catch {
    return null
  }
}

/**
 * 把"下一步的思考档位"写进会话的 model/selection。
 * 成功后下一次模型请求就会按新档位发；当前这一步不受影响。
 */
export function applyEffort(exec, effort) {
  if (!EFFORTS.includes(effort)) {
    return { ok: false, reason: `effort 只能是 ${EFFORTS.join(' / ')}` }
  }
  const m = exec?.agent?.model
  if (!m || typeof m.select !== 'function') {
    return { ok: false, reason: '宿主没暴露 agent.model.select，这个版本不支持运行时调档' }
  }
  let cur = null
  try {
    cur = m.current() || null
  } catch {
    cur = null
  }
  if (!cur || !cur.provider || !cur.model) {
    return { ok: false, reason: '还没选过模型，拿不到 provider/model，调不了档' }
  }
  const before = cur.reasoningEffort || ''
  if (before === effort) return { ok: true, before, unchanged: true }
  if (effort === 'off' && String(exec?.agent?.id || '')) {
    everOff.set(String(exec.agent.id), true)
  }
  const sid = String(exec?.agent?.id || '')
  const hist = Array.isArray(exec?.agent?.messages) ? exec.agent.messages.length : 0
  if (effort !== 'off' && before === 'off' && (sawOff(sid) || hist > 0)) {
    return {
      ok: false,
      reason: '本会话已经用过 off。DeepSeek 的思考模式要求把上一轮的 thinking 原样回传，'
        + '中间缺了 thinking 再切回 low/high/max 会直接 400（INVALID_REQUEST）。'
        + '要恢复思考只能新开会话；这一步请继续用 off，或直接用 action="hold" 等用户。',
    }
  }
  try {
    m.select({ provider: cur.provider, model: cur.model, reasoningEffort: effort })
  } catch (e) {
    return { ok: false, reason: `写模型选择失败：${e?.message || e}` }
  }
  return { ok: true, before, effort }
}

/**
 * 注册 thinking_control 工具。
 * @param {object} opts
 * @param {boolean} opts.enabled 自适应开关（false 时整个工具不注册）
 * @param {(label:string)=>void} [opts.onLabel] 把当前档位写到鲸鱼状态字（可选）
 */
export function createThinkingTool({ enabled = true, onLabel } = {}) {
  if (!enabled) return null
  return {
    name: 'thinking_control',
    description:
      '控制"下一步"的思考档位（本会话，下一 step 生效）。'
      + 'high/max=想得久；low=少想但仍有思考；off=完全不思考——'
      + '**off 是单向的**：本会话一旦用过 off，再切回 low/high/max 会让 DeepSeek 报表'
      + 'thinking 未回传（400），所以 off 之后只能新开会话恢复思考。'
      + 'cu 接管期间自动压到 low，交还时恢复原档位；不要随便用 off。'
      + '不改变用户在其他会话里的设置。',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        action: { type: 'string', enum: ['set', 'status', 'restore'] , description: 'set=调档；status=看当前档位；restore=恢复调档前的档位' },
        effort: { type: 'string', enum: EFFORTS, description: 'set 时必填：下一步的思考档位' },
        why: { type: 'string', description: '一句话理由（例如「下一步要判断弹窗该点哪个」，不写也行）' },
      },
      required: ['action'],
    },
    render(args, value) {
      return [{ type: 'text', text: value?.text ?? '' }]
    },
    timeoutMs: 10000,
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const action = String(args?.action || '')
      const sid = String(exec?.agent?.id || '')
      const cur = currentSelection(exec)
      if (action === 'status') {
        const eff = cur?.reasoningEffort || '(未设置，跟全局默认)'
        const saved = recallEffort(sid)
        return {
          status: 'ok',
          text: `当前模型 ${cur ? `${cur.provider}/${cur.model}` : '未知'}，思考档位：${eff}`
            + (saved ? `\ncu 接管前的档位：${saved}（交还时恢复）` : ''),
        }
      }
      if (action === 'restore') {
        const saved = recallEffort(sid)
        if (!saved) return { status: 'ok', text: '没有记录到接管前的档位，不用恢复。' }
        const r = applyEffort(exec, saved)
        if (!r.ok) return { status: 'error', text: `恢复失败：${r.reason}` }
        forgetEffort(sid)
        onLabel?.('')
        return { status: 'ok', text: `已恢复到接管前的思考档位：${saved}（下一 step 生效）。` }
      }
      if (action !== 'set') {
        return { status: 'invalid_action', text: 'action 只能是 set / status / restore。' }
      }
      const effort = String(args?.effort || '')
      if (!EFFORTS.includes(effort)) {
        return { status: 'invalid_action', text: `effort 只能是 ${EFFORTS.join(' / ')}；收到「${effort}」。` }
      }
      const r = applyEffort(exec, effort)
      if (!r.ok) return { status: 'error', text: `调档失败：${r.reason}` }
      onLabel?.(EFFORT_LABEL[effort] || '')
      const why = String(args?.why || '').trim()
      return {
        status: 'ok',
        text: (r.unchanged ? `思考档位本来就是 ${effort}，没有改动。` : `下一步的思考档位 -> ${effort}（下一 step 生效）。`)
          + (why ? `\n理由：${why}` : ''),
      }
    },
  }
}
