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
  // 虚拟桌面
  'desktop_list', 'desktop_current', 'desktop_switch', 'desktop_create', 'desktop_remove',
  'shadow_start', 'shadow_stop', 'shadow_status',
  // 服务
  'server_status', 'server_restart',
]

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

    // ---------------- 虚拟桌面 ----------------
    case 'desktop_list':
      return { op: 'cmd', payload: { argv: argvClean(['desktop-list', a.discover && '--discover', a.asJson && '--json']) } }
    case 'desktop_current':
      return { op: 'cmd', payload: { argv: argvClean(['desktop-current', a.asJson && '--json']) } }
    case 'desktop_switch':
      return {
        op: 'cmd',
        payload: {
          argv: argvClean(['desktop-switch',
            a.index !== undefined && a.index !== null && String(a.index),
            a.direction && ['--direction', S(a.direction)],
            a.target && ['--target', S(a.target)],
            a.asJson && '--json']),
        },
      }
    case 'desktop_create':
      return { op: 'cmd', payload: { argv: argvClean(['desktop-create', a.asJson && '--json']) } }
    case 'desktop_remove':
      return {
        op: 'cmd',
        payload: {
          argv: argvClean(['desktop-remove',
            a.index !== undefined && a.index !== null && String(a.index),
            a.confirm && '--confirm']),
        },
      }

    default:
      throw new CuToolError('CU_BAD_ARG', `action=${action} 还没接上（内部错误）`)
  }
}

/** 把一条结果归类成稳定的 status，供模型决定"改参数 / 重新观察 / 放弃"。 */
// 哪些动作会**动键鼠或夺焦点**——这些在主桌面上必须先经用户明确批准。
// 注意别把 launch/spawn/app_resize 算进来：它们改界面但不抢键鼠，算进来会误拦正常任务；
// app_terminate 自己就有 confirm 闸。desktop_switch 会切走用户屏幕，必须拦。
const MAIN_INPUT_ACTIONS = new Set([
  'click', 'double_click', 'right_click', 'triple_click', 'move', 'move_smooth',
  'drag', 'scroll', 'type', 'key', 'key_down', 'key_up', 'release',
  'paste', 'paste_image', 'post', 'focus', 'focus_cc',
  'desktop_switch', 'desktop_create', 'desktop_remove',
])

function needsMainApproval(action, args = {}) {
  if (MAIN_INPUT_ACTIONS.has(action)) return true
  if (action === 'element' && args && args.click === true) return true
  return false
}

export function classify(payload, errCode) {
  const code = errCode || ''
  if (code === 'CU_BAD_ARG' || code === 'CU_BAD_HOTKEY' || code === 'CU_SCROLL_DELTA'
    || code === 'CU_WAIT_SECONDS' || code === 'CU_APP_NOT_FOUND' || code === 'CU_APP_AMBIGUOUS') {
    return 'invalid_action'
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
  if (code === 'CU_CONFIRM_REQUIRED') {
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

const DESCRIPTION = `操作这台 Windows 桌面：截图、鼠标键盘、窗口/应用、剪贴板、UIA 元素、后台凝视、虚拟桌面、多步执行。
坐标一律 0-1000 归一化（region / element_at 例外，吃物理像素）。动作名单见 action 枚举，这里不重复。
纪律：除工具调用外不输出文字；单步任务思维链 ≤1 行，需要先规划时才展开（详见 computer-use 技能）。
看屏幕用 action="shot"（直接回图片、不落盘）；只看布局不读小字时给 long:800，图像 token 省一半。
会改变布局的动作（开菜单/对话框/切标签/滚动）之后，下一步坐标必须重新观察。
用户正在用电脑、不想被打扰时传 desktop:"shadow" 在隔离桌面里干活：不抢鼠标，但真实点击/输入会被系统拒绝（CU_UIPI），改用 element 的 Invoke/SetValue。
能用 API / 命令行完成的事不要走 GUI；网页内操作用 browser_use，别用本工具。
拿不准参数就传 argv:["<CLI 参数数组>"]（等价于直接跑 cu.py）。
需要登录 / 验证码 / MFA 时：不要硬试，叫用户接管。`

export function createComputerUseTool({ manager, attachments, shadow }) {
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
      out: { type: 'string', description: '截图/文本落盘路径（不给就不落盘）' },
      pattern: { type: 'string', description: 'windows/rect/focus/apps/find 的匹配串' },
      approveMain: { type: 'boolean', description: '要在用户真实桌面上控制键鼠时必须先经用户明确同意，同意后传 true；不传会被 CU_NEED_APPROVAL 拦下' },
      desktop: { type: 'string', enum: ['main', 'shadow'], description: '在哪个桌面上做：main=你的真实桌面（默认）；shadow=隔离桌面（不抢鼠标、看不见，但真实注入被系统拒绝）' },
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
      index: { type: 'number', description: '虚拟桌面序号' },
      direction: { type: 'string', enum: ['left', 'right'], description: '虚拟桌面按方向切' },
      target: { type: 'string', description: '虚拟桌面目标：main（当前桌面）/pip（豆包沙箱概念，本机不支持）' },
      discover: { type: 'boolean', description: '虚拟桌面：COM 不可用时按方向走一圈反推（会短暂切屏）' },
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
    async execute(args) {
      const action = args?.action

      // ---- 隔离会话（shadow desktop）----
      // desktop:'shadow' 时把整条调用改投到独立桌面上的那个常驻服务：
      // 它不抢鼠标、你看不见，但真实 SendInput 会被系统拒绝（CU_UIPI），
      // 元素级/截图/剪贴板/凝视照常。默认值由 DSH_CU_DEFAULT_DESKTOP 决定。
      if (action === 'shadow_start' || action === 'shadow_stop' || action === 'shadow_status') {
        if (!shadow) {
          return { status: 'error', text: '[CU_SHADOW] 本实例没有启用隔离会话支持',
                   next: '[next] 升级插件后重试。' }
        }
        try {
          if (action === 'shadow_start') {
            const r = await shadow.ensure()
            return { status: 'ok', next: '',
                     text: `隔离会话已就绪：桌面=${r.desktop} 端点=${r.endpoint}`
                       + `${r.started ? '（本次新起）' : '（复用）'}\n`
                       + '之后给 computer_use 传 desktop:"shadow" 即可在隔离桌面里干活；'
                       + '注意真实鼠标键盘注入在那里会被系统拒绝（CU_UIPI）。' }
          }
          if (action === 'shadow_stop') {
            const r = await shadow.stop()
            return { status: 'ok', next: '', text: r.detail || '隔离会话已结束' }
          }
          const s = await shadow.status()
          return { status: 'ok', next: '',
                   text: s.running
                     ? `隔离会话运行中：桌面=${s.desktop} 端点=${s.endpoint} pid=${s.pid}`
                     : '隔离会话未运行（action="shadow_start" 起一个）' }
        } catch (e) {
          return { status: 'error', text: `[CU_SHADOW] ${e.message || e}`,
                   next: '[next] 先 action="shadow_status" 看状态；起不来多半是 Python/Pillow 缺失。' }
        }
      }

      // 选目标平面：显式 desktop 参数优先，否则用默认（现在默认就是 shadow）
      const want = (args?.desktop || (shadow ? shadow.defaultDesktop() : 'main'))
        .toString().toLowerCase()

      // ---- 主桌面上"动键鼠"必须先获批（用户 2026-10-18 定的规矩）----
      // 做成硬闸而不是靠模型自觉：没带批准直接拦下，让它先问用户。
      // 光 `approveMain:true` 不够——模型会悄悄自批，所以还要求 `approvalNote`
      // 写下"谁在什么时候批的"，它会进结果文本，事后可查。
      const approvalNote = String(args?.approvalNote || '').trim()
      if (want !== 'shadow' && needsMainApproval(action, args)
          && (args?.approveMain !== true || approvalNote.length < 4)) {
        return {
          status: 'blocked',
          text: `[CU_NEED_APPROVAL] 这次要在**用户的真实桌面**上控制键鼠（${action}），`
            + '按规矩必须先征得同意；我没有擅自行动。',
          next: '[next] 先 ask_user_question 明确问用户"是否允许在你的桌面上操作键鼠"，'
            + '得到同意后带 approveMain:true + approvalNote:"<用户的原话或批准时间>" 重试；'
            + '或改用 desktop:"shadow"。**任务结束前记得 focus_cc 把 DSH 切回前台**。',
        }
      }

      let mgr = manager
      if (shadow && want === 'shadow') {
        try {
          mgr = await shadow.getManager()
        } catch (e) {
          return { status: 'error',
                   text: `[CU_SHADOW] 连不上隔离会话：${e.message || e}`,
                   next: '[next] 先 action="shadow_start"；或传 desktop:"main" 走主桌面（需用户批准）。' }
        }
      }

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
      try {
        if (mapped.op === 'eval') {
          // eval 可能跑很久：单独放宽 IO 超时到它的 timeout+10s
          const t = Number(mapped.payload.timeoutMs) || 20000
          resp = await mgr.call('eval', mapped.payload, { timeoutMs: t + 10000 })
        } else if (mapped.op === 'shot') {
          resp = await mgr.call('shot', mapped.payload, { binary: true, timeoutMs: 60000 })
        } else {
          const t = action === 'batch' ? 300000 : 120000
          resp = await mgr.call(mapped.op, mapped.payload, { timeoutMs: t })
        }
      } catch (e) {
        if (e instanceof CuIpcError) {
          return {
            status: 'error',
            text: `[${e.code || 'CU_IPC'}] ${e.message}`,
            next: '[next] 常驻服务不可用。可先 action="server_status" 看状态，或 action="server_restart"。',
          }
        }
        throw e
      }

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
          text: lines.join('\n'),
          image: ref,
          imageScope: meta.region ? 'element' : 'screen',
          next: '',
        }
      }

      return {
        status: 'ok',
        text: textOf(value) || '（完成，无输出）',
        next: '',
      }
    },
  }
}
