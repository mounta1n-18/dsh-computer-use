// 策略回归：默认走隔离桌面；主桌面动键鼠必须先获批。
import { ServerManager } from '../lib/server-manager.mjs'
import { createComputerUseTool } from '../lib/computer-use.mjs'
import {
  ensureShadow, stopShadow, shadowStatus, defaultDesktop, SHADOW_ENDPOINT,
} from '../lib/shadow-session.mjs'

const shadow = {
  defaultDesktop,
  async getManager() {
    await ensureShadow()
    return new ServerManager({
      env: { ...process.env, DSH_CU_TRANSPORT: 'named_pipe', DSH_CU_ENDPOINT: SHADOW_ENDPOINT },
    })
  },
  ensure: ensureShadow,
  stop: stopShadow,
  status: shadowStatus,
}
const tool = createComputerUseTool({ manager: new ServerManager({}), attachments: {}, shadow })

let pass = 0
let fail = 0
const ck = (n, ok, d = '') => { ok ? pass += 1 : fail += 1; console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${n}  ${d}`) }

ck('默认已切成 shadow', defaultDesktop() === 'shadow', defaultDesktop())

const a = await tool.execute({ action: 'where' })
ck('不传 desktop 时走隔离桌面（光标 0,0）',
  a.status === 'ok' && /像素\(0,0\)/.test(a.text), a.text)

const b = await tool.execute({ action: 'click', x: 500, y: 500, desktop: 'main' })
ck('主桌面 click 未获批 → blocked',
  b.status === 'blocked' && /CU_NEED_APPROVAL/.test(b.text), b.text.slice(0, 60))
ck('提示要求先用 ask_user_question 问用户',
  /ask_user_question/.test(b.next || ''), (b.next || '').slice(0, 60))

const c = await tool.execute({ action: 'where', desktop: 'main' })
ck('主桌面 where 不拦（不是输入动作）',
  c.status === 'ok' && !/像素\(0,0\)/.test(c.text), c.text)

const d = await tool.execute({ action: 'type', text: 'x', desktop: 'main' })
ck('主桌面 type 未获批 → blocked', d.status === 'blocked', d.text.slice(0, 50))

const e = await tool.execute({ action: 'element', pattern: 'x', desktop: 'main' })
ck('主桌面 element（不点坐标）不拦', e.status !== 'blocked', e.text.slice(0, 50))

console.log(`\n策略测试：${fail ? 'FAIL' : 'PASS'}（${pass} 通过 / ${fail} 失败）`)
process.exit(fail ? 1 : 0)
