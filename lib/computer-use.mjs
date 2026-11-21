// computer_use 工具：把模型的"动作 + 参数"翻译成 cu.py 的子命令，再把结果翻译成图文块。
//
// 三个刻意的设计：
//  1. **action 枚举 + 扁平参数**，而不是让模型自己拼命令行：拼错的代价是静默做错事
//     （比如把 `click 500 500` 写成 `click 500,500`）。
//  2. **argv 逃生舱**：实在表达不了的组合，模型可以原样传 CLI 参数数组，不会被卡死。
//  3. **状态分类**：每条结果都带 status（ok/error/invalid_action/state_changed/
//     blocked/timeout），对应豆包 envelope 的那套taxonomy——模型据此决定"改参数重试"
//     还是"重新观察画面"，而不是一律当失败重试。
import { CuIpcError } from './pipe-client.mjs'
import { applyEffort, currentSelection, rememberEffort, recallEffort, forgetEffort, EFFORT_LABEL } from './thinking.mjs'

const ACTIONS = [
  // 看
  'shot', 'windows', 'rect', 'where', 'probe', 'codes', 'doctor', 'setup',
  // 动
  'click', 'double_click', 'right_click', 'triple_click', 'move', 'move_smooth',
  'drag', 'scroll', 'type', 'key', 'key_down', 'key_up', 'release',
  // 剪贴板
  'paste', 'copy', 'clip_get', 'clip_set', 'clip_image', 'paste_image', 'save_image',
  // 窗口/应用
  'focus', 'focus_cc', 'apps', 'launch', 'post', 'wait',
  'app_is_running', 'app_resize', 'app_terminate', 'hybrid_programs', 'spawn',
  // 元素级（UIA）
  'state_capture', 'element', 'element_at', 'resync', 'find',
  // 凝视
  'watch_start', 'watch_recv', 'watch_status', 'mute',
  // 多步
  'batch', 'eval',
  // 控制权（激活 / 交还 / 查看 / 挂着等 / 提问 / 回答 / 恢复）
  'activate', 'deactivate', 'control', 'hold', 'ask', 'reply', 'resume',
  // 服务
  'server_status', 'server_restart',
]

// 鲸鱼下方的状态字：动作开始前先推到屏幕上，用户能看出它此刻在干什么。
// 没列的动作按"操作中"算（点、敲、拖都是前台键鼠）。
export const PHASE_BY_ACTION = {
  ask: '提问中',
  activate: '申请中',
  reply: '回复中',
  deactivate: '收工中',
  resume: '恢复中',
  hold: '等待中',
  wait: '等待中',
  watch_recv: '等待中',
  shot: '观察中',
  state_capture: '观察中',
  element: '观察中',
  element_at: '观察中',
  find: '观察中',
  windows: '观察中',
  apps: '观察中',
  eval: '后台执行中',
  batch: '后台执行中',
  spawn: '后台执行中',
  watch_start: '后台执行中',
  server_status: '后台执行中',
  server_restart: '后台执行中',
}

// 控制权没了的原因（服务端 reason → 给模型看的一句话）。这里只做兜底，
// 服务端会直接给 detail；两边都不给时绝不允许模型自己编。
export const WHY_CLOSED = {
  emergency: '用户按了急停（Ctrl+Alt+Shift+Backspace）',
  deactivate: 'agent 主动交还了控制权',
  'idle-timeout': '长时间没有操作，控制权被系统收回',
  'max-ttl': '到达单次激活的时长上限，控制权被收回',
  'turn-end': '本回合结束，cu 随之关闭',
  manual: '用户手动关闭了 cu',
}

export class CuToolError extends Error {
  constructor(code, message, extra = {}) {
    super(`[${code}] ${message}`)
    this.name = 'CuToolError'
    this.code = code
    this.extra = extra
  }
}

const S = (v) => (v === undefined || v === null ? undefined : String(v))
const N = (v) => (v === undefined || v === null ? undefined : Number(v))
const B = (v) => (v === undefined || v === null ? undefined : Boolean(v))

function regionStr(v) {
  if (v === undefined || v === null) return undefined
  if (Array.isArray(v)) {
    if (v.length !== 4) throw new CuToolError('CU_BAD_ARG', 'region 要给 [x,y,w,h] 四个数')
    return v.map((x) => Math.round(Number(x))).join(',')
  }
  return String(v)
}

function argvClean(list) {
  // 这个函数必须**递归拍平**：上面的映射里大量用 `cond && ['--flag', value]` 这种
  // 条件片段，如果只做一层 String(x)，数组会被转成 "--depth,4" 这种畸形参数——
  // 表现是 argparse 报"参数不合法"，而错误信息里看到的是逗号拼在旗标后面。
  // 同时：undefined/null/false 丢掉；0 和 '' 要保留（0 是合法坐标）。
  const out = []
  const push = (x) => {
    if (x === undefined || x === null || x === false) return
    if (Array.isArray(x)) {
      for (const y of x) push(y)
      return
    }
    out.push(String(x))
  }
  for (const x of list) push(x)
  return out
}

/**
 * 把工具的扁平参数翻译成服务端请求。
 * 返回 {op, payload}；op 是 'cmd' 时 payload.argv 是 CLI 参数数组。
 */
export function mapAction(a) {
  const action = a.action
  if (!action) throw new CuToolError('CU_BAD_ARG', '缺少 action')
  if (!ACTIONS.includes(action)) {
    throw new CuToolError('CU_BAD_ARG',
      `不认识的 action=${action}。可用：${ACTIONS.join(' / ')}`)
  }
  if (a.argv) {
    if (!Array.isArray(a.argv) || !a.argv.length) {
      throw new CuToolError('CU_BAD_ARG', 'argv 必须是非空字符串数组')
    }
    return { op: 'cmd', payload: { argv: a.argv.map(String) } }
  }
  const x = N(a.x); const y = N(a.y)
  const key = (combo) => {
    if (combo) return String(combo)
    if (Array.isArray(a.keys)) {
      // 对齐豆包 hotkey_keys：单元素里带 + 说明把整串当成了一个键，必须拒绝
      if (a.keys.length === 1 && a.keys[0] !== '+' && String(a.keys[0]).includes('+')) {
        throw new CuToolError('CU_BAD_HOTKEY',
          `把每个键分开传：收到 ${JSON.stringify(a.keys[0])}，应传 ["ctrl","s"]`)
      }
      return a.keys.map(String).join('+')
    }
    if (a.key) return String(a.key)
    throw new CuToolError('CU_BAD_ARG', `${action} 需要 key 或 keys`)
  }

  switch (action) {
    // ---------------- 看 ----------------
    case 'shot': {
      const out = S(a.out)
      if (out) {
        return {
          op: 'cmd',
          payload: {
            argv: argvClean(['shot', '--out', out,
              a.long !== undefined && a.long !== null && ['--long', N(a.long)],
              a.region !== undefined && a.region !== null && ['--region', regionStr(a.region)],
              a.window && ['--window', S(a.window)],
              a.full && '--full',
              a.settle && '--settle',
              a.grid === false && '--no-grid',
              a.keepAll && '--keep-all']),
          },
        }
      }
      return {
        op: 'shot',
        payload: {
          long: a.long === undefined ? undefined : N(a.long),
          region: regionStr(a.region),
          window: S(a.window),
          full: B(a.full) || false,
          settle: B(a.settle) || false,
          grid: a.grid === false ? false : true,
        },
      }
    }
    case 'windows':
      return { op: 'cmd', payload: { argv: argvClean(['windows', S(a.pattern), a.all && '--all']) } }
    case 'rect':
      return { op: 'cmd', payload: { argv: argvClean(['rect', S(a.pattern)]) } }
    case 'where':
      return { op: 'cmd', payload: { argv: ['where'] } }
    case 'probe':
      return { op: 'cmd', payload: { argv: ['probe'] } }
    case 'codes':
      return { op: 'cmd', payload: { argv: ['码'] } }
    case 'doctor':
      return { op: 'cmd', payload: { argv: argvClean(['doctor', a.asJson && '--json']) } }
    case 'setup':
      return { op: 'cmd', payload: { argv: argvClean(['setup', a.autoInstall && '--install']) } }

    // ---------------- 动 ----------------
    case 'click':
    case 'double_click':
    case 'right_click':
    case 'triple_click':
      return {
        op: 'cmd',
        payload: {
          argv: argvClean(['click', x, y,
            (a.right || action === 'right_click') && '--right',
            (a.double || action === 'double_click') && '--double',
            (a.triple || action === 'triple_click') && '--triple']),
        },
      }
    case 'move':
    case 'move_smooth':
      return {
        op: 'cmd',
        payload: {
          argv: argvClean([action === 'move' ? 'move' : 'move-smooth', x, y,
            action === 'move_smooth' && a.ms !== undefined && ['--ms', N(a.ms)]]),
        },
      }
    case 'drag':
      return {
        op: 'cmd',
        payload: {
          argv: argvClean(['drag', x, y, N(a.x2), N(a.y2),
            a.ms !== undefined && ['--ms', N(a.ms)]]),
        },
      }
    case 'scroll':
      if (a.n === undefined || a.n === null || a.n === '') {
        throw new CuToolError('CU_BAD_ARG', 'scroll 需要 n（-10..10 整数或 up/down/left/right）')
      }
      return { op: 'cmd', payload: { argv: ['scroll', x, y, String(a.n)] } }
    case 'type':
      if (a.text === undefined || a.text === null) {
        throw new CuToolError('CU_BAD_ARG', 'type 需要 text')
      }
      return { op: 'cmd', payload: { argv: ['type', String(a.text)] } }
    case 'key':
      return { op: 'cmd', payload: { argv: ['key', key()] } }
    case 'key_down':
    case 'key_up':
      return { op: 'cmd', payload: { argv: [action === 'key_down' ? 'key-down' : 'key-up', key(a.key || a.combo)] } }
    case 'release':
      return { op: 'cmd', payload: { argv: ['release'] } }

    // ---------------- 剪贴板 ----------------
    case 'paste':
      return { op: 'cmd', payload: { argv: argvClean(['paste', S(a.text), a.keep && '--keep']) } }
    case 'copy':
      return { op: 'cmd', payload: { argv: argvClean(['copy', a.keep && '--keep', a.out && ['--out', S(a.out)]]) } }
    case 'clip_get':
      return { op: 'cmd', payload: { argv: argvClean(['clip', a.out && ['--out', S(a.out)]]) } }
    case 'clip_set':
      return { op: 'cmd', payload: { argv: argvClean(['clip-set', S(a.text)]) } }
    case 'clip_image':
      return { op: 'cmd', payload: { argv: ['clip-image', String(a.path)] } }
    case 'paste_image':
      return { op: 'cmd', payload: { argv: ['paste-image', String(a.path)] } }
    case 'save_image':
      return { op: 'cmd', payload: { argv: ['save-image', String(a.path)] } }

    // ---------------- 窗口 / 应用 ----------------
    case 'focus':
      return { op: 'cmd', payload: { argv: ['focus', String(a.pattern)] } }
    case 'focus_cc':
      return { op: 'cmd', payload: { argv: argvClean(['focus-cc', S(a.pattern)]) } }
    case 'apps':
      return { op: 'cmd', payload: { argv: argvClean(['apps', S(a.pattern)]) } }
    case 'app_is_running':
      return {
        op: 'cmd',
        payload: {
          argv: argvClean(['app-is-running', String(a.name || a.pattern || a.query || ''),
            a.asJson && '--json']),
        },
      }
    case 'app_resize':
      return {
        op: 'cmd',
        payload: {
          argv: argvClean(['app-resize', String(a.name || a.pattern || a.window || ''),
            N(a.width), N(a.height)]),
        },
      }
    case 'app_terminate':
      return {
        op: 'cmd',
        payload: {
          argv: argvClean(['app-terminate', String(a.name || a.pattern || a.window || ''),
            a.confirm && '--confirm']),
        },
      }
    case 'hybrid_programs':
      return { op: 'cmd', payload: { argv: ['hybrid-programs'] } }
    case 'spawn': {
      // --cwd 必须排在 REMAINDER 的 args 前面，否则会被当成程序参数吞掉
      const target = S(a.path || a.exe || a.name)
      if (!target) throw new CuToolError('CU_BAD_ARG', 'spawn 需要 path（可执行文件）')
      const extra = Array.isArray(a.args) ? a.args.map(String) : []
      return {
        op: 'cmd',
        payload: { argv: argvClean(['spawn', target,
          a.cwd && ['--cwd', S(a.cwd)], ...extra]) },
      }
    }
    case 'launch':
      return {
        op: 'cmd',
        payload: {
          argv: argvClean(['launch', String(a.name || a.pattern),
            a.timeout !== undefined && ['--timeout', N(a.timeout)],
            a.noFocus && '--no-focus']),
        },
      }
    case 'post':
      return {
        op: 'cmd',
        payload: {
          argv: argvClean(['post', String(a.window), S(a.text),
            a.image && ['--image', S(a.image)],
            a.at && ['--at', String(a.at)],
            a.enter === false && '--no-enter',
            a.keep && '--keep',
            a.out && ['--out', S(a.out)]]),
        },
      }
    case 'wait':
      return { op: 'cmd', payload: { argv: ['wait', String(a.seconds ?? 1)] } }

    // ---------------- 元素级 ----------------
    case 'state_capture':
      return {
        op: 'cmd',
        payload: {
          argv: argvClean(['state-capture',
            a.window && ['--window', S(a.window)],
            a.depth !== undefined && ['--depth', N(a.depth)],
            a.limit !== undefined && ['--limit', N(a.limit)],
            a.asJson && '--json']),
        },
      }
    case 'element': {
      if (!a.ref) throw new CuToolError('CU_BAD_ARG', 'element 需要 ref（来自 state_capture）')
      return {
        op: 'cmd',
        payload: {
          argv: argvClean(['element', String(a.ref),
            a.click && '--click',
            a.invoke && '--invoke',
            a.setValue !== undefined && ['--set-value', String(a.setValue)],
            a.setFocus && '--set-focus',
            a.toggle && '--toggle',
            a.select && '--select',
            a.addToSelection && '--add-to-selection',
            a.expand && '--expand',
            a.collapse && '--collapse',
            a.scrollIntoView && '--scroll-into-view',
            a.setRangeValue !== undefined && ['--set-range-value', String(a.setRangeValue)],
            a.windowClose && '--window-close',
            a.get && ['--get', S(a.get)]]),
        },
      }
    }
    case 'element_at':
      return { op: 'cmd', payload: { argv: argvClean(['element-at', N(a.x), N(a.y), a.asJson && '--json']) } }
    case 'resync':
      return { op: 'cmd', payload: { argv: ['resync'] } }
    case 'find':
      return {
        op: 'cmd',
        payload: {
          argv: argvClean(['find', String(a.pattern),
            a.window && ['--window', S(a.window)],
            a.role && ['--role', S(a.role)],
            a.regex && '--regex',
            a.exact && '--exact',
            a.index !== undefined && a.index !== null && ['--index', N(a.index)],
            a.click && '--click',
            a.limit !== undefined && ['--limit', N(a.limit)],
            a.asJson && '--json']),
        },
      }

    // ---------------- 凝视 ----------------
    case 'watch_start':
      return {
        op: 'cmd',
        payload: {
          argv: argvClean(['盯',
            a.region && ['--region', regionStr(a.region)],
            a.window && ['--window', S(a.window)],
            a.timeout !== undefined && ['--timeout', N(a.timeout)],
            Array.isArray(a.ignore) && a.ignore.flatMap((v) => ['--ignore', String(v)]),
            a.box && '--box',
            a.once && '--once',
            a.queue && ['--queue', S(a.queue)],
            a.coalesce !== undefined && ['--coalesce', N(a.coalesce)],
            a.noRaise && '--no-raise']),
        },
      }
    case 'watch_recv':
      return {
        op: 'cmd',
        payload: {
          argv: argvClean(['收',
            a.wait !== undefined && ['--wait', N(a.wait)],
            a.all && '--all',
            a.archive && '--archive',
            a.peek && '--peek',
            a.queue && ['--queue', S(a.queue)],
            a.asJson && '--json']),
        },
      }
    case 'watch_status':
      return {
        op: 'cmd',
        payload: { argv: argvClean(['瞧', a.queue && ['--queue', S(a.queue)], a.asJson && '--json']) },
      }
    case 'mute':
      return {
        op: 'cmd',
        payload: { argv: argvClean(['静默', a.seconds !== undefined && String(a.seconds)]) },
      }

    // ---------------- 多步 ----------------
    case 'batch': {
      const cmds = a.commands || a.cmds
      if (!Array.isArray(cmds) || !cmds.length) {
        throw new CuToolError('CU_BAD_ARG', 'batch 需要 commands 字符串数组')
      }
      return {
        op: 'batch',
        payload: { cmds: cmds.map(String), continueOnError: Boolean(a.continueOnError) },
      }
    }
    case 'eval': {
      if (!a.code || typeof a.code !== 'string') {
        throw new CuToolError('CU_BAD_ARG', 'eval 需要 code 字符串')
      }
      return { op: 'eval', payload: { code: a.code, timeoutMs: N(a.timeout) || 20000 } }
    }

    default:
      throw new CuToolError('CU_BAD_ARG', `action=${action} 还没接上（内部错误）`)
  }
}

/** 把一条结果归类成稳定的 status，供模型决定"改参数 / 重新观察 / 放弃"。 */
export function classify(payload, errCode) {
  const code = errCode || ''
  if (code === 'CU_BAD_ARG' || code === 'CU_BAD_HOTKEY' || code === 'CU_SCROLL_DELTA'
    || code === 'CU_WAIT_SECONDS' || code === 'CU_APP_NOT_FOUND' || code === 'CU_APP_AMBIGUOUS') {
    return 'invalid_action'
  }
  // 控制权相关的错误：都不是"界面变了"，而是"这台机器现在不归你用"。
  if (code === 'CU_NOT_ACTIVATED' || code === 'CU_LEASE_EXPIRED' || code === 'CU_BUSY'
    || code === 'CU_EMERGENCY_STOP' || code === 'CU_ACTIVATION_DENIED' || code === 'CU_PAUSED'
    || code === 'CU_NO_OVERLAY' || code === 'CU_NEED_INTRO' || code === 'CU_UNANSWERED') {
    return 'control'
  }
  if (code === 'CU_COVERED' || code === 'CU_MINIMIZED' || code === 'CU_MOVED'
    || code === 'CU_NO_WINDOW' || code === 'CU_ELEMENT') {
    return 'state_changed'
  }
  if (code === 'CU_BLACK' || code === 'CU_UIPI' || code === 'CU_CONFIRM_REQUIRED') return 'blocked'
  if (code === 'CU_EVAL_TIMEOUT') return 'timeout'
  if (code) return 'error'
  return 'ok'
}

function textOf(value) {
  if (!value) return ''
  if (typeof value === 'string') return value
  if (Array.isArray(value.lines)) return value.lines.join('\n')
  if (typeof value.text === 'string') return value.text
  if (Array.isArray(value.results)) {
    // batch：逐条列出，最后给一行汇总
    const lines = value.results.map((r, i) =>
      `${i + 1}. ${r.ok ? 'OK  ' : 'FAIL'} ${r.cmd}`
      + (r.ok ? (r.lines?.length ? `\n     ${r.lines.join('\n     ')}` : '')
        : `\n     [${r.code}] ${r.message}`))
    lines.push(`—— ${value.ok_count}/${value.results.length} 成功`
      + (value.stopped_early ? '（遇错已停）' : '') + `  耗时 ${value.ms}ms`)
    return lines.join('\n')
  }
  if (value.timed_out) {
    return `[CU_EVAL_TIMEOUT] eval 超时\nstdout:\n${value.stdout || ''}\n${value.note || ''}`
  }
  if (value.error) {
    return `[${value.error.code}] ${value.error.message}\n${value.error.traceback || ''}`.trim()
  }
  if (value.stdout !== undefined || value.stderr !== undefined) {
    return `stdout:\n${value.stdout || ''}${value.stderr ? `\nstderr:\n${value.stderr}` : ''}`.trim()
  }
  if (value.checks) {
    return value.checks.map((c) =>
      `[${c.ok ? 'OK  ' : (c.required ? 'FAIL' : 'WARN')}] ${c.name.padEnd(16)} ${c.detail}`
      + (!c.ok && c.fix ? `\n       → ${c.fix}` : '')).join('\n')
  }
  return JSON.stringify(value)
}

/** 观察类的 [next] 提示：对齐豆包 envelope.py 的 anti-repeat 指引。 */
function nextHint(action, status, code = '') {
  if (code === 'CU_NO_OVERLAY') {
    return '[next] 屏幕上没有出现控制提示，所以现在没有控制权（硬规则）：'
      + '再试一次 activate 不会有害；还起不来就停下，告诉用户"屏幕上没有出现控制提示，暂时不能接管电脑"。'
  }
  if (code === 'CU_NOT_ACTIVATED') {
    return '[next] 现在没有控制权：先 action="activate" 并等用户同意，'
      + '再重新做这个动作。别用别的参数重试同一个动作。'
  }
  if (code === 'CU_LEASE_EXPIRED') {
    return '[next] 控制权已自动收回（空闲太久或单次上限到了）：重新 action="activate" 再继续。'
  }
  if (code === 'CU_BUSY') {
    return '[next] 另一个代理正持有控制权：不要抢。等它 deactivate，'
      + '或请用户按 Ctrl+Alt+Shift+Backspace 手动关闭。'
  }
  if (code === 'CU_EMERGENCY_STOP') {
    return '[next] 用户手动关闭了电脑控制：立刻停手。这是即停即止——红边闪 3 秒后整个 cu 已经关闭，'
      + '不要重试、不要换 action 绕过；需要再操作电脑时重新 activate，并等用户点同意。'
  }
  if (code === 'CU_ACTIVATION_DENIED') {
    return '[next] 用户拒绝了这次激活：不要马上再弹一次。先问清楚他希望你做什么。'
  }
  if (code === 'CU_NEED_INTRO') {
    return '[next] activate 少了自我介绍或使用强度：intro 用一句话写清"你是哪个会话、要做什么"'
      + '（不许写 session id / 「主 agent」），intensity 三选一 '
      + 'script_main / unknown / gui_main。补齐后重试 activate。'
  }
  if (code === 'CU_UNANSWERED') {
    return '[next] 用户在递话框里发了话，你还没回答：立刻 action="reply" 带 text 回一句'
      + '（他会看到你的话），回完再继续操作电脑。不要用 deactivate 或重试绕过。'
  }
  if (code === 'CU_PAUSED') {
    return '[next] 用户按了暂停（或点了鲸鱼给你留言）：现在不该动键鼠。'
      + '看看结果里有没有 [提示] 用户留言；有就先 action="reply" 回答'
      + '（reply 成功即自动解除暂停，别让用户手动恢复），没有就停下等用户按 Ctrl+Shift+Alt 恢复，别重试。'
  }  if (code === 'CU_CONFIRM_REQUIRED') {
    return '[next] 这是破坏性动作：先把"会丢掉什么"告诉用户，得到明确同意后再用 confirm:true 重试。'
  }
  if (status === 'state_changed') {
    return '[next] 界面状态变了：先重新观察（shot 或 state_capture），不要用旧坐标/旧 ref 重试。'
  }
  if (status === 'invalid_action') {
    return '[next] 是参数/名字的问题，不是界面的问题：按提示改参数，别重复同样的调用。'
  }
  if (status === 'blocked') {
    return '[next] 被系统级限制挡住（UIPI/安全桌面）：停止注入，向用户说明并请其接管。'
  }
  if (action === 'shot') {
    return '[next] 图在上一条消息里；不要再截同一张图。要像素坐标就用 0-1000 归一化值。'
  }
  if (action === 'state_capture') {
    return '[next] ref 只对本次快照有效；界面一变就重新 state_capture，别复用旧 ref。'
  }
  if (action === 'watch_start') {
    return '[next] 凝视是后台排队：用 watch_recv 取帧，别用连续截图代替等待。'
  }
  return ''
}

const DESCRIPTION = `操作这台 Windows 桌面：截图、鼠标键盘、窗口与应用、剪贴板、元素识别、多步执行。
坐标一律 0-1000 归一化（region / element_at 例外，吃物理像素）。

**怎么用**
1. 动手前 action="activate"，必须同时带两样东西：
   - intro：你亲手写的一句话，说明"你是哪个会话、要做什么"（例如"课程表整理：把 Excel 课表抄进日历"）。不许写 session id，也不许写「主 agent」——用户要靠这句话分清是哪个会话在动他的电脑。
   - intensity：这次用多少键鼠，三选一 —— script_main（主要脚本实现，键鼠零星使用）/ unknown（我不知道，视情况而定）/ gui_main（主要键鼠实现，脚本零星使用）。
   屏幕上出现的卡片会原样显示 intro + 完整 session id + 强度；30 秒没回应按同意处理。随后屏幕四边亮蓝、右下角出现鲸鱼标志。一次激活做完整个任务，不要每步重开。
2. 只看不动的动作（shot / windows / apps / find / wait / state_capture）不需要激活。
3. 要等用户下一步指令，用 action="hold" 挂着（一次最多 55 秒），别把回合结束掉——结束了他再说话就递不进来。
4. 需要用户亲自输入或决定（密码、验证码、选哪个选项），用 action="ask" 并带 text 说明。cu 会自动暂停，用户答完自动继续，你不用重问。
5. 干完必须 action="deactivate"：界面交还给用户，屏幕上的提示全部消失。之后要再用，重新 activate。

**用户从工具侧说话时，必须从工具侧回答**
- 用户在右下角递话框里发的每一句话，都要求你先用 action="reply" 带 text 回一句（他会看到你的话），然后才能继续操作电脑。没回答之前动键鼠会被 CU_UNANSWERED 拦住。
- **reply 一返回，暂停就自动解除了**。所以不要回完还说"你还处于暂停/请手动恢复"，也不要让用户去按热键；回完直接继续干活或 hold。
- 反过来：DSH 聊天窗口里的话，就在聊天里回答，不要用 reply 去答。两边不要串。
- 回答要短：一句结论 + 需要他知道的一步。

**用户会随时打断，这是他的权利，不是故障**
- Ctrl+Shift+Alt：暂停/恢复。暂停期间你的动作会被拒绝（CU_PAUSED），不要重试、不要绕，等恢复或等用户的话。
- Ctrl+Alt+Shift+Backspace：手动关闭。cu 直接结束，不会自动恢复；要再用必须重新 activate 并等用户同意。
- 点右下角鲸鱼：暂停并给用户一个留言框（暂停中再点鲸鱼**不会**恢复，只开留言框）。他写完你必须 reply。

**读结果**
- [提示] 开头的是用户的话（暂停/恢复/留言/对你提问的回答），照做，别当噪声。
- [next] 是下一步建议；status 不是 ok 时先看它，不要重复同一个失败调用。

纪律：除工具调用外不输出文字；单步任务思维链 ≤1 行。动作会改变布局（开菜单/切标签/滚动）后，先重新观察再点。
能用 API 或命令行完成的事不必走 GUI；网页内的操作用 browser_use。`

export function createComputerUseTool({ manager, attachments, sessions, sessionTitle, adaptive = false }) {
  // 用户留言的投递确认：服务端「显示过」不算数，只有我们把它拼进了工具结果、
  // 模型真能看到，下一次请求才回 ack，服务端这时才删。否则留言会因为某个
  // 分支没渲染而永久丢失（2026-11-14 实测丢过两条）。
  let ackNoticeId = null
  const withAck = (payload) => (ackNoticeId ? { ...payload, ack_notice: ackNoticeId } : payload)
  const trackNotice = (json) => {
    const ids = json && json.notice_ids
    if (Array.isArray(ids) && ids.length) ackNoticeId = ids
  }
  // 状态字是"顺带"推的：失败绝不能影响动作本身。
  const firePhase = (label) => {
    try {
      manager.call('phase', { label }, { timeoutMs: 2000 }).catch(() => {})
    } catch {}
  }

  const parameters = {
    type: 'object',
    properties: {
      action: { type: 'string', enum: ACTIONS, description: '要做的动作' },
      x: { type: 'number', description: '归一化 x（0-1000），或 element_at 的物理像素 x' },
      y: { type: 'number', description: '归一化 y（0-1000），或 element_at 的物理像素 y' },
      x2: { type: 'number', description: 'drag 终点归一化 x' },
      y2: { type: 'number', description: 'drag 终点归一化 y' },
      n: { type: ['integer', 'string'], description: 'scroll 步数 -10..10 或 up/down/left/right' },
      ms: { type: 'number', description: 'drag / move_smooth 时长毫秒' },
      text: { type: 'string', description: 'type/paste/clip_set/post 的文本' },
      key: { type: 'string', description: '单个键或组合键，如 "enter" / "ctrl+s"' },
      keys: { type: 'array', items: { type: 'string' }, description: '组合键按每个键分开传，如 ["ctrl","s"]' },
      window: { type: 'string', description: '窗口标题或进程名子串' },
      region: { type: ['string', 'array'], description: '物理像素 x,y,w,h（字符串或四元数组）' },
      long: { type: 'number', description: '截图长边像素上限' },
      full: { type: 'boolean', description: '截图不缩放' },
      keepAll: { type: 'boolean', description: '截图不清理旧文件' },
      settle: {
        type: 'boolean',
        description: '截图前先探一下屏幕是否还在动（动画/过渡/滚动惯性）；在动就等落定后补截一张。默认关，怀疑截到动画中途时开',
      },
      grid: {
        type: 'boolean',
        description: '截图是否叠 0-1000 归一化网格与刻度（默认开）。按刻度读坐标，比肉眼估百分比准；嫌挡视线可传 false',
      },
      out: { type: 'string', description: '截图/文本落盘路径（不给就不落盘）' },
      pattern: { type: 'string', description: 'windows/rect/focus/apps/find 的匹配串' },
      label: { type: 'string', description: '可选，不用填：身份以会话 id + 工作目录为准，冲突提示里显示的就是这个。' },
      intro: {
        type: 'string',
        description: 'activate 必填：你亲手写的一句自我介绍（哪个会话、要做什么），会显示在用户的同意卡片上。不许写 session id 或「主 agent」。',
      },
      intensity: {
        type: 'string',
        enum: ['script_main', 'unknown', 'gui_main'],
        description: 'activate 必填：本次使用强度。script_main=主要脚本实现，键鼠零星使用；unknown=我不知道，视情况而定；gui_main=主要键鼠实现，脚本零星使用。',
      },
      idle_ttl: { type: 'number', description: 'activate 可选：空闲多少秒后自动收回控制权（默认 120）' },
      max_ttl: { type: 'number', description: 'activate 可选：单次激活最长多少秒（默认 1800）' },
      reason: { type: 'string', description: 'deactivate 的原因（会写进审计）' },
      name: { type: 'string', description: 'launch 的应用名 / app_* 的目标应用名' },
      width: { type: 'number', description: 'app_resize 的目标宽度（像素）' },
      height: { type: 'number', description: 'app_resize 的目标高度（像素）' },
      confirm: { type: 'boolean', description: 'app_terminate 破坏性动作，必须显式 true 才执行' },
      role: { type: 'string', description: 'find 只要这个角色，如 Button/Edit/Text' },
      regex: { type: 'boolean', description: 'find 的 pattern 按正则匹配（默认子串）' },
      exact: { type: 'boolean', description: 'find 名字必须完全相等' },
      exe: { type: 'string', description: 'spawn 的可执行文件路径' },
      args: { type: 'array', items: { type: 'string' }, description: 'spawn 原样传给程序的参数' },
      cwd: { type: 'string', description: 'spawn 的工作目录' },
      all: { type: 'boolean', description: 'windows --all（连隐藏窗口）' },
      noFocus: { type: 'boolean', description: 'launch 后不置前' },
      right: { type: 'boolean', description: 'click 右键' },
      double: { type: 'boolean' },
      triple: { type: 'boolean' },
      image: { type: 'string', description: 'post --image 的 PNG 路径' },
      at: { type: 'string', description: 'post 输入框位置（窗口内 0-1000）x,y' },
      enter: { type: 'boolean', description: 'post 是否回车发送（默认 true）' },
      keep: { type: 'boolean', description: '不还原剪贴板' },
      path: { type: 'string', description: 'clip_image/paste_image/save_image 的 PNG 路径' },
      seconds: { type: 'number', description: 'wait / mute 的秒数' },
      timeout: { type: 'number', description: 'launch 等窗口超时秒 / eval 毫秒 / 凝视秒数' },
      ignore: { type: 'array', items: { type: 'string' }, description: '凝视排除区（整屏 0-1000 归一化 x0,y0,x1,y1）' },
      box: { type: 'boolean', description: '凝视结果带变化位置' },
      once: { type: 'boolean', description: '凝视只做一次' },
      queue: { type: 'string', description: '凝视队列名，默认 default' },
      coalesce: { type: 'number', description: '凝视合并窗口秒数' },
      noRaise: { type: 'boolean' },
      wait: { type: 'number', description: 'watch_recv 队列空时最多等多少秒' },
      archive: { type: 'boolean', description: 'watch_recv 只交结尾帧、中间帧留档' },
      peek: { type: 'boolean', description: 'watch_recv 只看不取' },
      ref: { type: 'string', description: 'element 的 ref（来自 state_capture）' },
      depth: { type: 'number', description: 'state_capture 最大深度' },
      limit: { type: 'number', description: 'state_capture/find 数量上限' },
      click: { type: 'boolean', description: 'element/find 直接点' },
      invoke: { type: 'boolean', description: 'element 走 InvokePattern' },
      setValue: { type: 'string', description: 'element 走 ValuePattern 写值' },
      setFocus: { type: 'boolean' },
      toggle: { type: 'boolean', description: 'element 走 TogglePattern（幂等）' },
      select: { type: 'boolean', description: 'element 走 SelectionItemPattern' },
      addToSelection: { type: 'boolean' },
      expand: { type: 'boolean' },
      collapse: { type: 'boolean' },
      scrollIntoView: { type: 'boolean' },
      setRangeValue: { type: 'number' },
      windowClose: { type: 'boolean' },
      get: { type: 'string', description: 'element 读属性：name/value/class/rect/enabled/focused/offscreen/password/accelerator/pattern' },
      commands: { type: 'array', items: { type: 'string' }, description: 'batch 的命令行数组' },
      continueOnError: { type: 'boolean', description: 'batch 遇错是否继续' },
      code: { type: 'string', description: 'eval 的 Python 代码（可直接调 cu.*）' },
      asJson: { type: 'boolean', description: '返回 JSON 而不是人话' },
      autoInstall: { type: 'boolean', description: 'setup 时真的执行安装命令' },
      argv: { type: 'array', items: { type: 'string' }, description: '逃生舱：直接给 CLI 参数数组，等价于跑 cu.py' },
    },
    required: ['action'],
    additionalProperties: false,
  }

  return {
    name: 'computer_use',
    description: DESCRIPTION,
    parameters,
    output: {
      schema: { type: 'object', additionalProperties: true },
      render(args, value) {
        const blocks = []
        const status = value?.status || 'ok'
        let text = value?.text ?? ''
        if (status !== 'ok') {
          blocks.push({ type: 'text', text: `[result status=${status}]\n${text}` })
        } else {
          blocks.push({ type: 'text', text })
        }
        if (value?.image) {
          blocks.push({ type: 'image', attachment: value.image })
        }
        // 成功路径不再追加 [next]：[next] 是给"出错后怎么改"的指路，
        // 成功时再发一遍等于每次多花约 40 token 复述技能里已有的内容。
        if (value?.next) blocks.push({ type: 'text', text: value.next })
        return blocks
      },
    },
    timeoutMs: 180000,
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      const action = args?.action

      // ---- 主动向用户要东西（密码 / 验证码 / 选哪个按钮）----
      // 与"点鲸鱼"同一个通道，只是由模型发起：先挂起 cu（不是关掉、也不是结束会话），
      // 再把问题同时写在顶部横幅和递话框里。用户答完自动恢复并继续。
      if (action === 'ask') {
        const text = String(args?.text || '').trim().slice(0, 400)
        if (!text) {
          return {
            status: 'invalid_action',
            text: '[CU_BAD_ARG] ask 必须带 text：一句话说清需要用户做什么。',
            next: '[next] 例如 text="需要你输入这页的登录密码"。',
          }
        }
        let resp
        try {
          resp = await manager.call('ask', withAck({ text }), { timeoutMs: 15000 })
        } catch (e) {
          if (e instanceof CuIpcError) {
            return {
              status: 'error',
              text: `[${e.code || 'CU_IPC'}] ${e.message}`,
              next: '[next] 服务不可用；控制权大概率已经没了，别再操作电脑。',
            }
          }
          throw e
        }
        const json = resp.json || {}
        if (!json.ok) {
          const code = json.code || 'CU_UNEXPECTED'
          return { status: classify(null, code), text: `[${code}] ${String(json.message || '').trim()}`, next: nextHint(action, 'control', code) }
        }
        trackNotice(json)
        return {
          status: 'ok',
          text: `已暂停 cu 并向用户提问：${text}\n`
            + '用户回答后会以 [提示] 回来（形如「cu 已恢复，用户有新指令：…」）；'
            + '他也可以按 Ctrl+Shift+Alt 直接恢复。在那之前不要动键鼠。',
          next: '[next] 想继续挂着等就用 action="hold"；用户答完再继续操作。',
        }
      }

      // ---- 回答用户在递话框里说的话（工具侧问 → 工具侧答）----
      if (action === 'reply') {
        // text 允许为空：等于"看过了，不用回"——照样解除暂停，只是不弹回执卡。
        const text = String(args?.text ?? '').trim().slice(0, 800)
        let resp
        try {
          resp = await manager.call('reply', withAck({ text }), { timeoutMs: 15000 })
        } catch (e) {
          if (e instanceof CuIpcError) {
            return {
              status: 'error',
              text: `[${e.code || 'CU_IPC'}] ${e.message}`,
              next: '[next] 服务不可用；控制权大概率已经没了，别再操作电脑。',
            }
          }
          throw e
        }
        const json = resp.json || {}
        if (!json.ok) {
          const code = json.code || 'CU_UNEXPECTED'
          return {
            status: classify(null, code),
            text: `[${code}] ${String(json.message || '').trim()}`,
            next: nextHint(action, 'control', code),
          }
        }
        trackNotice(json)
        const v = json.value || {}
        return {
          status: 'ok',
          text: (text
            ? `已回复用户：「${text}」——他会看到这句话（右下角回执卡）。`
            : '已跳过回复（空回复，不给用户弹窗，只在日志里留一条记录）。')
            + '\n暂停已解除，可以继续操作电脑。'
            + (v.to ? `\n（他刚才说的是：${v.to}）` : ''),
          next: '',
        }
      }

      // ---- 显式恢复：只解暂停，不关 cu ----
      if (action === 'resume') {
        let resp
        try {
          resp = await manager.call('resume', withAck({}), { timeoutMs: 10000 })
        } catch (e) {
          if (e instanceof CuIpcError) {
            return {
              status: 'error',
              text: `[${e.code || 'CU_IPC'}] ${e.message}`,
              next: '[next] 服务不可用；用 action="server_status" 看一眼。',
            }
          }
          throw e
        }
        const json = resp.json || {}
        if (!json.ok) {
          const code = json.code || 'CU_UNEXPECTED'
          return {
            status: classify(null, code),
            text: `[${code}] ${String(json.message || '').trim()}`,
            next: nextHint(action, 'control', code),
          }
        }
        trackNotice(json)
        return {
          status: 'ok',
          text: (json.value?.resumed ? '已恢复工作。' : '本来就没暂停。') + '控制权还在，别让用户替你按热键。',
          next: '',
        }
      }

      // ---- 挂着等用户说话（cu 开着时不许静默结束回合）----
      // 服务端长轮询：有新留言、控制权没了、或超时才返回。挂在这里不烧 token，
      // 用户一说话就带着原话回来，回合自然续上。
      if (action === 'hold') {
        const seconds = Math.min(Math.max(Number(args?.seconds) || 45, 5), 55)
        let resp
        firePhase('等待中')
        try {
          resp = await manager.call('wait', withAck({ seconds }), { timeoutMs: (seconds + 20) * 1000 })
        } catch (e) {
          firePhase('思考中')
          if (e instanceof CuIpcError) {
            return {
              status: 'error',
              text: `[${e.code || 'CU_IPC'}] ${e.message}`,
              next: '[next] 常驻服务不可用了。用 action="server_status" 看一眼；控制权大概率已经没了，别再操作电脑。',
            }
          }
          throw e
        }
        const json = resp.json || {}
        firePhase('思考中')
        if (!json.ok) {
          const code = json.code || 'CU_UNEXPECTED'
          return { status: classify(null, code), text: `[${code}] ${String(json.message || '').trim()}`, next: '' }
        }
        const v = json.value || {}
        const items = Array.isArray(v.items) ? v.items : []
        const lines = []
        if (items.length) {
          lines.push(`用户在你控制电脑期间发来 ${items.length} 条：`)
          for (const it of items) lines.push(`- ${String(it.text || '').trim()}`)
        } else {
          lines.push(`这 ${Math.round(v.seconds || seconds)} 秒内用户没有新指令。`)
        }
        if (v.closed || !v.active) {
          // 原因必须由服务端给：以前只说 closed:true，模型就按激活文案里的
          // "空闲 600 秒" 自己编，用户按了急停却被告诉"闲置了 600 秒"（2026-11-20 实测）。
          const why = String(v.detail || WHY_CLOSED[v.reason] || '').trim()
          lines.push(`控制权已经不在了${why ? `：${why}` : ''}。停止操作电脑；需要时重新 activate 并等用户同意。`
            + (why ? '' : '（服务端没给原因，不要替它猜，也不要对用户描述关闭经过。）'))
        } else {
          lines.push(`控制权仍在（租约持有者 ${v.owner}）${v.paused ? '，用户当前暂停中' : ''}。`)
        }
        if (v.pending_reply) {
          lines.push('你还没回答用户刚在递话框里说的话：先用 action="reply" 回一句，再继续操作。')
        }
        trackNotice(json)
        // 这些留言已经当面给模型了，从队列里摘掉，免得下一次又重复递一遍。
        if (items.length) {
          try {
            await manager.call('messages', { ack: items.map((i) => i.id) }, { timeoutMs: 3000 })
          } catch { /* ack 失败不影响这一次返回 */ }
        }
        return {
          status: 'ok',
          text: lines.join('\n') + (json.notice ? `\n\n[提示] ${json.notice}` : ''),
          next: v.active ? '[next] 还没收尾就再 action="hold" 挂一轮；干完了就 deactivate。' : '',
        }
      }

      // ---- 控制权：激活 / 交还 / 查看 ----
      // 状态存在 Python 常驻服务里（进程级单例），所以同一台机器上所有会话共享一把钥匙：
      // 谁先 activate 谁持有；别人再来会拿到 CU_BUSY，提示里带持有者（会话 id）。
      if (action === 'activate' || action === 'deactivate' || action === 'control') {
        const label = String(args?.label || '').trim().slice(0, 60)
        // 用户最想知道的是"哪个会话在用我的电脑"。Agent 只有 id，目录要从 session header 里取。
        const sessionId = String(exec?.agent?.id || '')
        const liveSession = sessions?.get?.(exec?.agent?.id)
        const cwd = String(liveSession?.header?.cwd || exec?.cwd || '')
        // 用户给会话起的名字（面板里可改）；拿不到就退回会话 id。
        let title = ''
        try {
          title = String(sessionTitle?.get?.(liveSession)?.title || '')
        } catch { title = '' }
        if (action === 'activate') firePhase('申请中')
        if (action === 'activate' && !sessionId && !label) {
          return {
            status: 'invalid_action',
            text: '[CU_BAD_ARG] activate 拿不到会话身份：宿主没给 session id，也没给 label。',
            next: '[next] 这不是你能补的：报告给用户，重启 DSH 让宿主把会话 id 带上。',
          }
        }
        let resp
        try {
          if (action === 'activate') {
            resp = await manager.call('activate', withAck({
              owner: sessionId || label,
              session: sessionId,
              title,
              cwd,
              intro: S(args?.intro),
              intensity: S(args?.intensity),
              idle_ttl: N(args?.idle_ttl),
              max_ttl: N(args?.max_ttl),
            }), { timeoutMs: 45000 })
          } else if (action === 'deactivate') {
            resp = await manager.call('deactivate', withAck({ reason: S(args?.reason) }), { timeoutMs: 20000 })
          } else {
            resp = await manager.call('control', withAck({}), { timeoutMs: 10000 })
          }
        } catch (e) {
          if (e instanceof CuIpcError) {
            return {
              status: 'error',
              text: `[${e.code || 'CU_IPC'}] ${e.message}`,
              next: '[next] 常驻服务不可用。先 action="server_status" 看状态。',
            }
          }
          throw e
        }
        const json = resp.json || {}
        if (!json.ok) {
          const code = json.code || 'CU_UNEXPECTED'
          const raw = String(json.message || '（无消息）')
          const msg = raw.startsWith(`[${code}]`) ? raw.slice(code.length + 2) : raw
          return { status: classify(null, code), text: `[${code}] ${msg.trim()}`, next: nextHint(action, 'control', code) }
        }
        const v = json.value || {}
        const notice = json.notice ? `\n\n[提示] ${json.notice}` : ''
        trackNotice(json)
        // 自适应思考：接管期间把档位压到 low，交还时恢复用户原档位。
        // 只动本会话的 model/selection（下一 step 生效），不写全局默认。
        // 为什么是 low 而不是 off：DeepSeek 的思考模式要求把上一轮的 thinking
        // 原样回传，中间只要有一 step 是 off 生成的，再切回 low/high/max 就会
        // 400 INVALID_REQUEST。low 仍带思考，来回切是安全的；off 是单向门。
        let thinkNote = ''
        if (adaptive && action === 'activate') {
          const cur = currentSelection(exec)
          if (cur?.reasoningEffort && (cur.reasoningEffort === 'high' || cur.reasoningEffort === 'max')) {
            rememberEffort(sessionId, cur.reasoningEffort)
            if (applyEffort(exec, 'low').ok) {
              firePhase(EFFORT_LABEL.low)
              thinkNote = `\n思考已降到 low（接管期间少想一点；下一步很难就 thinking_control 调回 ${cur.reasoningEffort}，交还时也会自动恢复）。`
            }
          }
        }
        if (adaptive && action === 'deactivate') {
          const saved = recallEffort(sessionId)
          if (saved) {
            const r = applyEffort(exec, saved)
            forgetEffort(sessionId)
            firePhase('思考中')
            if (!r.ok) {
              thinkNote = `\n（思考档位没能恢复到 ${saved}：${r.reason}）`
            }
          }
        }
        if (action === 'control') {
          const c = v.control || {}
          const hk = v.hotkeys || {}
          const ov = v.overlay || {}
          return {
            status: 'ok',
            text: [
              `控制权：${c.enabled ? '可用' : '已关闭'}${c.emergency ? '（已手动关闭）' : ''}${c.paused ? '（用户已暂停）' : ''}`,
              `租约：${c.active
                ? `代理「${c.owner}」持有；空闲剩 ${Math.round(c.idle_left || 0)}s，单次剩 ${Math.round(c.max_left || 0)}s`
                : '空闲（用户在用）'}`,
              `热键：${hk.polling_ok ? '已启用' : '未启用'}  手动关闭=${hk.emergency || 'Ctrl+Alt+Shift+Backspace'}`
                + `  暂停=${hk.pause || 'Ctrl+Shift+Alt'}  同意=${hk.agree || 'Ctrl+Alt+Shift+Enter'}`,
              `提示层：${ov.running ? '运行中' : `未运行${ov.error ? `（${ov.error}）` : ''}`}`,
            ].join('\n') + notice,
            next: '',
          }
        }
        if (action === 'activate') {
          return {
            status: 'ok',
            text: `已激活：持有者「${v.owner || label}」；空闲 ${Math.round(v.idle_ttl || 0)}s 后自动收回，`
              + `单次上限 ${Math.round(v.max_ttl || 0)}s。用户屏幕边缘已亮蓝色。`
              + (v.reused ? '\n（续用已有租约，没有重复打扰用户）' : '')
              + (v.auto ? '\n（30 秒没有回应，按默认同意放行）' : `\n（用户等了 ${v.waited ?? 0}s 才点同意）`)
              + '\n用户可能在这期间自己动鼠标键盘帮你或纠正你（也可能是误触）：看到屏幕变得不对就先看、再判断，别抢回去。'
              + '\n接下来必须调 computer_use action="hold" 等用户下指令：**不要在这里结束回合**。'
              + '用户没说下一步之前，回合结束等于把刚接过来的电脑又交回去。'
              + thinkNote
              + notice,
            next: '',
          }
        }
        return {
          status: 'ok',
          text: (v.deactivated
            ? `已交还控制权（持有者「${v.owner || ''}」，原因：${v.reason || '未填写'}）。`
              + (v.closed ? 'DSH 已切回前台，电脑控制完全关闭（提示层与热键都已退出）。'
                : '用户现在可以正常用鼠标键盘。')
            : '当前本来就没有控制权，无需交还。') + notice,
          next: '',
        }
      }

      const mgr = manager

      // ---- 纯 Node 侧动作（不经过 Python）----
      if (action === 'server_status') {
        const st = await mgr.status()
        return {
          status: st.running ? 'ok' : 'error',
          text: Object.entries(st)
            .filter(([, v]) => v !== null && v !== undefined && v !== '')
            .map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`)
            .join('\n'),
          next: st.running ? '' : '[next] 服务没在跑；下一次调用会自动拉起。',
        }
      }
      if (action === 'server_restart') {
        const r = await mgr.restart()
        return {
          status: 'ok',
          text: `服务已重启：pid=${r.status.pid} transport=${r.status.transport} `
            + `（owned=${r.status.owned}）队列=${JSON.stringify(r.status.queues)} `
            + `静默窗=${r.status.muteUntil}`,
          next: '',
        }
      }

      // ---- 需要经过 Python 的动作 ----
      let mapped
      try {
        mapped = mapAction(args || {})
      } catch (e) {
        if (e instanceof CuToolError) {
          return { status: classify(null, e.code), text: e.message, next: nextHint(action, 'invalid_action') }
        }
        throw e
      }

      let resp
      // 动作开始前把阶段推到鲸鱼下方；结束（成功或失败）一律回到"思考中"。
      firePhase(PHASE_BY_ACTION[action] || '操作中')
      try {
        if (mapped.op === 'eval') {
          // eval 可能跑很久：单独放宽 IO 超时到它的 timeout+10s
          const t = Number(mapped.payload.timeoutMs) || 20000
          resp = await mgr.call('eval', withAck(mapped.payload), { timeoutMs: t + 10000 })
        } else if (mapped.op === 'shot') {
          resp = await mgr.call('shot', withAck(mapped.payload), { binary: true, timeoutMs: 60000 })
        } else {
          const t = action === 'batch' ? 300000 : 120000
          resp = await mgr.call(mapped.op, withAck(mapped.payload), { timeoutMs: t })
        }
      } catch (e) {
        firePhase('思考中')
        if (e instanceof CuIpcError) {
          return {
            status: 'error',
            text: `[${e.code || 'CU_IPC'}] ${e.message}`,
            next: '[next] 常驻服务不可用。可先 action="server_status" 看状态，或 action="server_restart"。',
          }
        }
        throw e
      }
      firePhase('思考中')

      const json = resp.json || {}
      if (!json.ok) {
        const code = json.code || 'CU_UNEXPECTED'
        // Python 那边 CuError.__str__ 已经带 "[CU_XXX] " 前缀了，
        // 这里别再套一层，否则模型看到的是 "[CU_BAD_ARG] [CU_BAD_ARG] ..."。
        const raw = String(json.message || '（无消息）')
        const msg = raw.startsWith(`[${code}]`) ? raw.slice(code.length + 2) : raw
        const st = classify(null, code)
        return {
          status: st,
          text: `[${code}] ${msg.trim()}`,
          next: nextHint(action, st, code),
        }
      }
      const value = json.value ?? {}
      const notice = json.notice ? `\n\n[提示] ${json.notice}` : ''

      // 截图：二进制帧 + 附件
      if (mapped.op === 'shot' && resp.binary) {
        if (!attachments) {
          throw new CuToolError('CU_DEP_MISSING', '没有 attachments 服务，无法把截图渲染成图片')
        }
        const ref = await attachments.saveImage({
          data: new Uint8Array(resp.binary),
          mediaType: 'image/png',
          name: `cu-shot-${Date.now()}.png`,
        })
        const meta = value
        // 后端如实标出来：printwindow = 问窗口要的图（被遮挡也能截、不抢前台）
        const backend = meta.capture_path === 'printwindow' ? '打印窗口(遮挡也可截)' : '屏幕裁剪'
        const lines = [
          `${meta.width}x${meta.height}  源 ${meta.source?.width}x${meta.source?.height}`
            + `${meta.region ? `  裁剪@${meta.region.join(',')}` : ''}`
            + `  缩放 ${meta.scale}  ${Math.round((meta.bytes || 0) / 1024)}KB`
            + `  约 ${meta.estimatedImageTokens} 图像 token  [${backend}]`
            + (meta.window ? ` · 窗口「${meta.window}」` : ''),
        ]
        if (meta.lit_ratio !== undefined && meta.lit_ratio < 0.01) {
          lines.push('[CU_BLACK] 画面几乎全黑：可能是锁屏/UAC 安全桌面，或全屏独占程序。'
            + '别反复重截，先确认能不能输入。')
        }
        if (meta.region_normalized) {
          const [x0, y0, x1, y1] = meta.region_normalized
          lines.push(`裁剪覆盖屏幕归一化 x ${x0}~${x1}  y ${y0}~${y1}`
            + `；图内比例 (fx,fy) 换算：x=${x0}+fx*${(x1 - x0).toFixed(1)}`
            + ` y=${y0}+fy*${(y1 - y0).toFixed(1)}`)
        }
        return {
          status: 'ok',
          text: lines.join('\n') + notice,
          image: ref,
          imageScope: meta.region ? 'element' : 'screen',
          next: '',
        }
      }

      return {
        status: 'ok',
        text: (textOf(value) || '（完成，无输出）') + notice,
        next: '',
      }
    },
  }
}



