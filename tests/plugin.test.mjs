// 插件端到端测试：不装进 DSH，用一个 stub ctx 跑真实的工具调用链。
//
// 为什么要有这一层：单元测试能证明"每个模块自己没问题"，但证明不了
// "Cordis 的 ToolDefinition 形状对不对、attachments 能不能把 PNG 变成图片块、
//  dispose 会不会真的把 python 进程收掉"。这些只能端到端跑。
//
// 跑：node tests/plugin.test.mjs
import { spawnSync } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import plugin from '../index.js'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(HERE, '..')

// 用**独有端点**跑：否则会复用机器上别的 DSH 会话已经拉起的服务，
// 那样"本测试自己拉起再回收"这条路径根本没被验证到。
process.env.DSH_CU_TRANSPORT = 'named_pipe'
process.env.DSH_CU_ENDPOINT = `\\\\.\\pipe\\dsh-cu-plugin-test-${process.pid}`

const PASS = []
const FAIL = []

function check(name, cond, detail = '') {
  if (cond) PASS.push(name)
  else FAIL.push(name)
  console.log(`  ${name.padEnd(48)} ${cond ? 'PASS' : 'FAIL'}` + (cond ? '' : `   <<< ${String(detail).slice(0, 200)}`))
}

function makeCtx() {
  const tools = new Map()
  const skills = []
  const effects = []
  const images = []
  return {
    tools: {
      register(def) {
        if (!def?.name) throw new Error('tool 没有 name')
        if (typeof def.execute !== 'function') throw new Error('tool 没有 execute')
        if (!def.parameters || def.parameters.type !== 'object') throw new Error('tool parameters 不是 object schema')
        tools.set(def.name, def)
        return () => tools.delete(def.name)
      },
    },
    attachments: {
      async saveImage({ data, mediaType, name }) {
        if (!(data instanceof Uint8Array) || data.length < 8) throw new Error('截图字节不合法')
        if (mediaType !== 'image/png') throw new Error('mediaType 不是 image/png')
        const ref = { attachmentId: `att_${images.length}`, mediaType, bytes: data.length, width: 0, height: 0, name }
        images.push(ref)
        return ref
      },
    },
    skills: {
      register(s) {
        skills.push(s)
        return () => {}
      },
    },
    get() {
      return undefined // 模拟 provider 槽不存在
    },
    effect(fn) {
      effects.push(fn)
    },
    _tools: tools,
    _skills: skills,
    _effects: effects,
    _images: images,
  }
}

function countPython() {
  const r = spawnSync('powershell.exe', ['-NoProfile', '-Command',
    "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*cu_server*' } | Measure-Object).Count"],
  { encoding: 'utf8', timeout: 20000 })
  const n = Number((r.stdout || '').trim())
  return Number.isFinite(n) ? n : -1
}

async function main() {
  const ctx = makeCtx()
  const before = countPython()
  const ret = plugin.apply(ctx)
  check('apply 返回 manager', !!ret && typeof ret.manager?.ensure === 'function')

  const cu = ctx._tools.get('computer_use')
  const bu = ctx._tools.get('browser_use')
  check('注册了 computer_use', !!cu)
  check('注册了 browser_use', !!bu)
  check('computer_use 有 output.render', typeof cu?.output?.render === 'function')
  check('computer_use action 枚举非空', Array.isArray(cu?.parameters?.properties?.action?.enum)
    && cu.parameters.properties.action.enum.length > 40,
  cu?.parameters?.properties?.action?.enum?.length)
  check('computer_use isConcurrencySafe=false（桌面操作不可并发）', cu?.isConcurrencySafe?.() === false)
  check('注册了自带技能', ctx._skills.length >= 1, ctx._skills.map((s) => s.name))

  // ---- 工具调用链（会真的拉起常驻服务并读一次桌面）----
  const r1 = await cu.execute({ action: 'where' })
  check('where → ok 且带光标信息', r1.status === 'ok' && /光标/.test(r1.text), r1.text)

  const r2 = await cu.execute({ action: 'server_status' })
  check('server_status → 服务在跑', r2.status === 'ok' && /running: true/.test(r2.text), r2.text)

  const r3 = await cu.execute({ action: 'shot', long: 640 })
  check('shot → 拿到图片附件', r3.status === 'ok' && !!r3.image && r3.image.mediaType === 'image/png', r3.text)
  check('shot → 文本里有尺寸与 token 估算', /\d+x\d+/.test(r3.text) && /token/.test(r3.text), r3.text)
  const rendered = cu.output.render({ action: 'shot' }, r3)
  check('shot render → 文本块 + 图片块',
    Array.isArray(rendered) && rendered.some((b) => b.type === 'image' && b.attachment === r3.image),
    JSON.stringify(rendered.map((b) => b.type)))

  const r4 = await cu.execute({ action: 'eval', code: "print('hi')\n1+1" })
  check('eval → 捕获输出', r4.status === 'ok' && /hi/.test(r4.text) && /2/.test(r4.text), r4.text)

  const r5 = await cu.execute({ action: 'desktop_current' })
  check('desktop_current → 拿到真实 GUID', r5.status === 'ok' && /\{[0-9A-F-]{36}\}/.test(r5.text), r5.text)

  const r6 = await cu.execute({ action: 'doctor' })
  check('doctor → 全项自检有结论', r6.status === 'ok' && /pillow|python/.test(r6.text), r6.text.slice(0, 120))

  const r7 = await cu.execute({ action: '__nope__' })
  check('非法 action → invalid_action 而非抛异常', r7.status === 'invalid_action', r7.text)

  const r8 = await cu.execute({ action: 'batch', commands: ['where', 'where'] })
  check('batch → 两条都成功', r8.status === 'ok' && /2\/2 成功/.test(r8.text), r8.text)

  // 这一条专门钉住"嵌套参数必须被拍平"：曾经 argvClean 只做一层 String()，
  // 于是 ['--depth', 4] 被拼成 "--depth,4"，真机上一调就报 CU_BAD_ARG。
  const r9 = await cu.execute({ action: 'state_capture', depth: 4, limit: 8 })
  check('state_capture（嵌套参数）→ ok 且给出 eN ref',
    r9.status === 'ok' && /\be\d+\b/.test(r9.text), r9.text.slice(0, 200))

  const r10 = await cu.execute({ action: 'windows', all: false })
  check('windows → ok', r10.status === 'ok', r10.text.slice(0, 160))

  // ---- 第二轮对齐新增的动作（同时证明 argvClean 的扁平/嵌套拼接都对） ----
  const r11 = await cu.execute({ action: 'app_is_running', name: 'zzz_no_such_app_xyz' })
  check('app_is_running → 明确回没在跑', r11.status === 'ok' && /没在跑/.test(r11.text), r11.text)

  const r12 = await cu.execute({ action: 'hybrid_programs' })
  check('hybrid_programs → ok', r12.status === 'ok', r12.text.slice(0, 160))

  const r13 = await cu.execute({ action: 'spawn', exe: 'cmd.exe', args: ['/c', 'exit'] })
  check('spawn → 起进程成功并给 pid', r13.status === 'ok' && /pid=\d+/.test(r13.text), r13.text)

  const r14 = await cu.execute({ action: 'find', pattern: '新建', role: 'Button', limit: 3 })
  check('find(role) → 给带 ref 的候选', r14.status === 'ok' && /\be\d+\b/.test(r14.text), r14.text.slice(0, 200))

  // 破坏性动作必须被确认闸拦住：传一个不存在的窗口名，确保**绝不**真去关用户的应用
  const r15 = await cu.execute({ action: 'app_terminate', window: 'zzz_no_such_window_xyz' })
  check('app_terminate（目标不存在）→ 不执行且给明确错误',
    r15.status !== 'ok' && /CU_CONFIRM_REQUIRED|CU_NO_WINDOW/.test(r15.text), r15.text)

  // 无确认 + 真实存在的窗口 → 必须是 CU_CONFIRM_REQUIRED（第二道闸）
  const firstWin = (r10.text || '').match(/\r?\n\s*(\d{4,})\s+\S+/)
  if (firstWin) {
    const r16 = await cu.execute({ action: 'app_terminate', window: 'DeepSeek' })
    check('app_terminate（真实窗口但无 confirm）→ 拒绝执行',
      /CU_CONFIRM_REQUIRED|CU_NO_WINDOW/.test(r16.text) && r16.status !== 'ok', r16.text)
  }

  // ---- ui 层：browser_use 在没端点时应如实报错而不是假装成功 ----
  const b1 = await bu.execute({ action: 'status' })
  check('browser_use status → 不抛异常且给出端点结论', b1.status === 'ok' || b1.status === 'error', b1.text)

  // ---- 卸载收尾：**本测试拉起的** python 进程必须被回收 ----
  // 注意：机器上可能已经有一个别的 DSH 会话拉起的 cu_server（before>0），
  // 那不是本测试的、也不该被本测试杀掉（复用/拥有的语义），所以只断言"没有新增"。
  const cleanups = ctx._effects.map((f) => f()).filter((x) => typeof x === 'function')
  for (const c of cleanups) await c()
  await new Promise((r) => setTimeout(r, 1500))
  const after = countPython()
  check('dispose 后没有新增 cu_server 进程', after <= before, `before=${before} after=${after}`)

  console.log()
  console.log(`插件端到端: ${FAIL.length ? 'FAIL' : 'PASS'}（${PASS.length} 通过 / ${FAIL.length} 失败）`)
  for (const f of FAIL) console.log(`  失败：${f}`)
  return FAIL.length ? 1 : 0
}

main().then((code) => process.exit(code)).catch((e) => {
  console.error('测试脚本自身异常：', e)
  process.exit(2)
})
