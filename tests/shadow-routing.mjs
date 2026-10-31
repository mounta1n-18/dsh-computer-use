// 验证 desktop:'shadow' 真的把调用切到了隔离桌面（而不是主桌面）。
import { ServerManager } from '../lib/server-manager.mjs'
import { createComputerUseTool } from '../lib/computer-use.mjs'
import { ensureShadow, stopShadow, shadowStatus, defaultDesktop, SHADOW_ENDPOINT } from '../lib/shadow-session.mjs'

let pass = 0
let fail = 0
function check(name, ok, detail = '') {
  if (ok) { pass += 1; console.log(`  [PASS] ${name}  ${detail}`) }
  else { fail += 1; console.log(`  [FAIL] ${name}  ${detail}`) }
}

const manager = new ServerManager({})
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
const attachments = {
  async saveImage({ data, mediaType, name }) {
    return { id: name || 'stub', mediaType, width: 1, height: 1, bytes: data?.length || 0 }
  },
}
const tool = createComputerUseTool({ manager, attachments, shadow })

const r1 = await tool.execute({ action: 'shadow_status' })
check('shadow_status 可调用', r1.status === 'ok', r1.text.slice(0, 60))

const r2 = await tool.execute({ action: 'shadow_start' })
check('shadow_start 起会话', r2.status === 'ok', r2.text.slice(0, 80))

const rMain = await tool.execute({ action: 'where' })
const rShadow = await tool.execute({ action: 'where', desktop: 'shadow' })
check('where 主桌面 = 真实光标', rMain.status === 'ok' && /像素\((?!0,0\))/.test(rMain.text), rMain.text)
check('where 隔离桌面 = 它自己的光标(0,0)', rShadow.status === 'ok' && /像素\(0,0\)/.test(rShadow.text), rShadow.text)
check('两条 where 结果不同（证明真的切换了目标）',
  rMain.text !== rShadow.text, `${rMain.text.slice(0, 24)} vs ${rShadow.text.slice(0, 24)}`)

const rClick = await tool.execute({ action: 'click', x: 500, y: 500, desktop: 'shadow' })
check('隔离桌面里 click 被系统拒绝（CU_UIPI）',
  rClick.status !== 'ok' && /CU_UIPI/.test(rClick.text), rClick.text.slice(0, 70))

const rDoctor = await tool.execute({ action: 'doctor', desktop: 'shadow' })
check('隔离桌面里 doctor 可用', rDoctor.status === 'ok', rDoctor.text.slice(0, 50))

await tool.execute({ action: 'shadow_stop' })
const r3 = await tool.execute({ action: 'shadow_status' })
check('shadow_stop 之后状态为未运行', /未运行/.test(r3.text), r3.text.slice(0, 50))

console.log(`\n路由测试：${fail ? 'FAIL' : 'PASS'}（${pass} 通过 / ${fail} 失败）`)
process.exit(fail ? 1 : 0)
