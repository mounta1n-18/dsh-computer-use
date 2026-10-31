// browser_use 工具包装层测试：只验"工具 → CDP"这条胶水对不对。
//
// CDP 本身的行为由 lib/cdp/selftest.mjs（47 项）覆盖；这里只验包装层最容易错的三件事：
//   1. launch 起的端口能不能被后续调用找到（曾经默认 9333、而 discover 只扫 9222-9232）
//   2. 嵌套参数有没有被正确拼成 CDP 调用
//   3. 未连上时是否**如实报错**而不是假装成功
//
// 跑：node tests/browser-use.test.mjs
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

import { createBrowserUseTool } from '../lib/browser-use.mjs'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const PASS = []
const FAIL = []

function check(name, cond, detail = '') {
  ;(cond ? PASS : FAIL).push(name)
  console.log(`  ${name.padEnd(46)} ${cond ? 'PASS' : 'FAIL'}` + (cond ? '' : `   <<< ${String(detail).slice(0, 200)}`))
}

const images = []

async function main() {
  const tool = createBrowserUseTool({
    attachments: {
      async saveImage({ data, mediaType, name }) {
        if (!(data instanceof Uint8Array) || data.length < 8) throw new Error('截图字节不合法')
        const ref = { attachmentId: `att_${images.length}`, mediaType, bytes: data.length, width: 0, height: 0, name }
        images.push(ref)
        return ref
      },
    },
  })

  // 0) 没起浏览器时：必须如实报"没有端点 + 怎么起"，不是假装成功
  const s0 = await tool.execute({ action: 'status' })
  check('未起浏览器时 status 如实报告', s0.status === 'ok' && /没扫到|没扫到|CDP 端点/.test(s0.text), s0.text)

  // 1) launch（无头，端口 9222，落在默认扫描区间内）
  const l = await tool.execute({ action: 'launch', headless: true, port: 9222 })
  check('launch 起了一个可调试实例', l.status === 'ok' && /已启动浏览器/.test(l.text), l.text)

  // 2) status：必须能连上自己刚起的实例（这条就是那个端口 bug 的回归测试）
  const s1 = await tool.execute({ action: 'status' })
  check('launch 之后 status 能连上（端口衔接正确）', s1.status === 'ok' && /当前页/.test(s1.text), s1.text)
  check('status 里没有 BU_NO_ENDPOINT', !/BU_NO_ENDPOINT/.test(s1.text), s1.text)

  // 3) navigate 到本地 data: 页面（不联网）
  const html = 'data:text/html,<h1>bu-test</h1>'
    + '<button id=b onclick="document.getElementById(\'n\').textContent=String(Number(document.getElementById(\'n\').textContent)+1)">Go</button>'
    + '<span id=n>0</span><input id=i placeholder="Email">'
  const nav = await tool.execute({ action: 'navigate', url: html })
  check('navigate data: 页面', nav.status === 'ok', nav.text)

  // 4) snapshot：首行 scope/url/viewport，且 ref 形如 dN:eM
  const snap = await tool.execute({ action: 'snapshot' })
  const first = (snap.text || '').split('\n')[0]
  check('snapshot 首行是 scope=dN url=… viewport=WxH',
    /^scope=d\d+ url=\S+ viewport=\d+x\d+/.test(first), first)
  check('snapshot 给出了 dN:eM 形式的 ref', /\bd\d+:e\d+\b/.test(snap.text), snap.text.slice(0, 200))

  // 5) find + click：点按钮后计数器应变成 1
  const found = await tool.execute({ action: 'find', query: 'Go' })
  const ref = (found.text.match(/d\d+:e\d+/) || [])[0]
  check('find 找到了 Go 按钮的 ref', !!ref, found.text)
  if (ref) {
    const c = await tool.execute({ action: 'click', ref })
    check('click 无异常', c.status === 'ok', c.text)
    const after = await tool.execute({ action: 'js', expression: "document.getElementById('n').textContent" })
    check('点击后页面状态真的变了（0→1）', /"1"|1/.test(after.text), after.text)
  }

  // 6) type 后回读 value
  const ir = (snap.text.match(/d\d+:e\d+ textbox .*Email.*/) || [])[0]
  const inputRef = ir && (ir.match(/d\d+:e\d+/) || [])[0]
  if (inputRef) {
    await tool.execute({ action: 'type', ref: inputRef, text: 'a@b.com' })
    const v = await tool.execute({ action: 'get', ref: inputRef, value: 'value' })
    check('type 后 get(value) 回读一致', /a@b\.com/.test(v.text), v.text)
  } else {
    check('type 后 get(value) 回读一致', false, '没在快照里找到 Email 输入框')
  }

  // 7) 截图 → 附件
  const shot = await tool.execute({ action: 'screenshot' })
  check('screenshot → 拿到图片附件', shot.status === 'ok' && !!shot.image && shot.image.mediaType === 'image/png', shot.text)

  // 8) 错误码：旧 ref 必须 BU_REF_STALE，而不是去点别的元素
  const stale = await tool.execute({ action: 'click', ref: 'd999:e1' })
  check('旧文档的 ref → BU_REF_STALE', /BU_REF_STALE/.test(stale.text), stale.text)

  // 9) drain 语义：console 连读两次第二次为空
  const c1 = await tool.execute({ action: 'console' })
  const c2 = await tool.execute({ action: 'console' })
  check('console 是 drain 语义（第二次为空）', /\[\]/.test(c2.text), `first=${(c1.text || '').slice(0, 60)} second=${c2.text}`)

  // 收尾：杀掉自己起的 Chrome（按 profile 精确匹配，不碰用户的 Chrome）
  // 注意：-like 里反斜杠不是转义字符，别把路径里的 \ 加倍，否则必然匹配不上。
  const profile = path.join(process.env.LOCALAPPDATA || process.env.TEMP, 'dsh-computer-use', 'chrome-profile')
  const r = spawnSync('powershell.exe', ['-NoProfile', '-Command',
    `Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" | Where-Object { $_.CommandLine -like '*${profile}*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $_.ProcessId }`],
  { encoding: 'utf8', timeout: 30000 })
  const killed = (r.stdout || '').trim().split(/\s+/).filter(Boolean)
  check('收尾：结束了自己起的 Chrome 实例', killed.length > 0, r.stdout || r.stderr)

  console.log()
  console.log(`browser_use 包装层: ${FAIL.length ? 'FAIL' : 'PASS'}（${PASS.length} 通过 / ${FAIL.length} 失败）`)
  for (const f of FAIL) console.log(`  失败：${f}`)
  return FAIL.length ? 1 : 0
}

main().then((c) => process.exit(c)).catch((e) => {
  console.error('测试脚本自身异常：', e)
  process.exit(2)
})
