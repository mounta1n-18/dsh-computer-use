// 验证 CU 插件的 stop 是否已经"快且优雅"——DSH 更新失败的直接原因。
import { ServerManager } from '../lib/server-manager.mjs'

process.env.DSH_CU_ENDPOINT = '\\\\.\\pipe\\dsh-cu-stopverify'
process.env.DSH_CU_TRANSPORT = 'named_pipe'

const mgr = new ServerManager({})
const t0 = Date.now()
await mgr.ensure()
const tStart = Date.now() - t0
const st = await mgr.status()
const t1 = Date.now()
const stop = await mgr.stop()
const tStop = Date.now() - t1

console.log(JSON.stringify({
  ensure_ms: tStart,
  running: st && st.running,
  stop_ms: tStop,
  graceful: stop && stop.graceful,
  stopped: stop && stop.stopped,
  verdict: (tStop < 1500 && stop && stop.graceful) ? 'PASS' : 'FAIL',
}, null, 2))
process.exit(0)
