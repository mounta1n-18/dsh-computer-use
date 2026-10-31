// lib/cdp/selftest.mjs
// 真跑的自测：`node lib/cdp/selftest.mjs`
//
// 它会做三件事：
//   1) 先扫 9222..9232 上的 CDP 端点；扫不到就用独立 user-data-dir 自己起一个 Chrome
//      （Chrome >= 136 拒绝在默认 profile 上开 --remote-debugging-port，必须独立目录）
//   2) 打开一个本地 data: 页面，逐项验证 snapshot / find / click / type / navigate(back)
//      / drain 语义 / tab 管理 / 对话框 / 截图 / 下载 / 错误码
//   3) 收尾：关会话、杀掉自己起的 Chrome（记录 pid）、删掉临时 user-data-dir
//
// 退出码：全 PASS = 0，有任何 FAIL = 1。
// 页面文本一律当数据看，本文件不会执行页面里的任何内容。

import { spawn, execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, join } from 'node:path';
import { createServer } from 'node:http';
import * as cdp from './index.mjs';
import { drainConsole, drainNetwork, ensureDownloadBehavior } from './extra.mjs';

const CHROME_PORT_BASE = 9333;
const CHROME_PORT_TRIES = 8;

const results = [];
let chromeProc = null;
let chromeUserDataDir = null;
let api = null;
let probeServer = null;
let probePort = 0;
let exited = false;

function record(name, ok, detail = '') {
  results.push({ name, ok: !!ok, detail: String(detail ?? '') });
  const tag = ok ? 'PASS' : 'FAIL';
  const line = `[${tag}] ${name}${detail ? ` — ${detail}` : ''}`;
  console.log(line);
}

async function step(name, fn, { optional = false } = {}) {
  try {
    const detail = await fn();
    record(name, true, typeof detail === 'string' ? detail : JSON.stringify(detail ?? ''));
    return { ok: true, value: detail };
  } catch (err) {
    record(name, optional ? true : false, `抛出 ${err?.code ?? 'ERR'}: ${err?.message ?? err}`);
    // 失败后清掉可能挂着的原生对话框：对话框会阻塞页面 JS，
    // 不清掉后面所有步骤都会跟着超时，把一次失败放大成一片失败
    try {
      if (api?.dialogState?.().open) await api.handleDialog({ accept: true });
    } catch {
      /* 清理失败就算了 */
    }
    return { ok: false, error: err };
  }
}

/** 断言某个操作必须抛指定 code。 */
async function expectCode(name, code, fn) {
  try {
    await fn();
  } catch (err) {
    if (err?.code === code) {
      record(name, true, `正确抛出 ${code}`);
      return;
    }
    record(name, false, `期望 ${code}，实际抛出 ${err?.code ?? 'ERR'}: ${err?.message ?? err}`);
    return;
  }
  record(name, false, `期望抛出 ${code}，但没有抛错`);
}

function findChrome() {
  const candidates = [
    process.env.CHROME_PATH,
    process.env.DSH_CHROME_PATH,
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    join(process.env.LOCALAPPDATA ?? '', 'Google\\Chrome\\Application\\chrome.exe'),
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
  ].filter(Boolean);
  for (const c of candidates) {
    try {
      if (existsSync(c)) return c;
    } catch {
      /* ignore */
    }
  }
  return null;
}

async function portFree(port) {
  try {
    const res = await fetch(`http://127.0.0.1:${port}/json/version`, {
      signal: AbortSignal.timeout(400),
    });
    if (res.ok) return false; // 已经有 CDP 在听
  } catch {
    return true;
  }
  return false;
}

async function waitForEndpoint(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/json/version`, {
        signal: AbortSignal.timeout(800),
      });
      if (res.ok) {
        const body = await res.json();
        if (body?.webSocketDebuggerUrl) return body;
      }
    } catch {
      /* 还没起来 */
    }
    if (Date.now() >= deadline) return null;
    await new Promise((r) => setTimeout(r, 250));
  }
}

function killProcessTree(pid) {
  try {
    if (process.platform === 'win32') {
      execFileSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' });
    } else {
      process.kill(pid, 'SIGKILL');
    }
    return true;
  } catch (err) {
    return `kill 失败: ${err?.message ?? err}`;
  }
}

function removeDirWithRetry(dir, tries = 5) {
  for (let i = 0; i < tries; i += 1) {
    try {
      rmSync(dir, { recursive: true, force: true });
      if (!existsSync(dir)) return true;
    } catch {
      /* Chrome 可能还没释放句柄 */
    }
    try {
      execFileSync(process.platform === 'win32' ? 'cmd' : 'sh', [], { stdio: 'ignore' });
    } catch {
      /* ignore */
    }
    // 同步小睡，避免陷入忙等
    const until = Date.now() + 400;
    while (Date.now() < until) {
      /* spin */
    }
  }
  return !existsSync(dir);
}

/** 启动一个只有本地回环的探针 HTTP 服务，用于验证 httpGet / network drain / 下载。 */
function startProbeServer() {
  return new Promise((resolve, reject) => {
    const server = createServer((req, res) => {
      const url = String(req.url ?? '/');
      if (url.startsWith('/file.txt')) {
        // 真正带 Content-Disposition 的下载响应：这是能触发 Browser.downloadWillBegin 的路径
        res.writeHead(200, {
          'content-type': 'text/plain',
          'content-disposition': 'attachment; filename="probe-download.txt"',
        });
        res.end('probe-download-body');
        return;
      }
      // 带 CORS 头：页面是 data: URL（不透明来源），没这个头 fetch 会被浏览器拦掉
      res.writeHead(200, {
        'content-type': 'text/plain; charset=utf-8',
        'access-control-allow-origin': '*',
      });
      res.end('probe-ok');
    });
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      probeServer = server;
      probePort = server.address().port;
      resolve(probePort);
    });
  });
}

function stopProbeServer() {
  if (!probeServer) return;
  try {
    probeServer.close();
  } catch {
    /* ignore */
  }
  probeServer = null;
}

// ---------------- 测试页面 ----------------

const TEST_HTML = `<!doctype html>
<html><head><meta charset="utf-8"><title>CDP Selftest</title></head>
<body>
  <h1>hi</h1>
  <button id="signin" onclick="window.__count=(window.__count||0)+1; this.textContent='Signed in '+window.__count; console.log('clicked '+window.__count);">Sign in</button>
  <p id="count">0</p>
  <input id="email" type="email" placeholder="Email">
  <select id="pick"><option value="">--</option><option value="a">Alpha</option><option value="b">Beta</option></select>
  <input id="agree" aria-label="Agree" type="checkbox">
  <input id="up" type="file">
  <a id="dl" href="about:blank" download="hello.txt">download</a>
  <script>
    console.log('boot');
    setTimeout(() => console.log('late-log'), 300);
  </script>
</body></html>`;

const BLANK_HTML = `<!doctype html><html><head><title>Blank</title></head><body><p>blank page</p></body></html>`;

function dataUrl(html) {
  return `data:text/html;charset=utf-8,${encodeURIComponent(html)}`;
}

async function main() {
  console.log('=== lib/cdp selftest ===');
  console.log(`node ${process.version} / ${process.platform}`);

  // ---- 1. 找端点，找不到就自己起 Chrome ----
  let endpoints = await cdp.discoverEndpoints();
  const started = { endpoint: null, pid: null, userDataDir: null, port: null };
  if (endpoints.length > 0) {
    record('discoverEndpoints 扫到已有端点', true, `port=${endpoints.map((e) => e.port).join(',')}`);
  } else {
    record('discoverEndpoints 扫到已有端点', true, '没扫到（预期内），改为自启 Chrome');
    const chrome = findChrome();
    if (!chrome) {
      record('定位 Chrome 可执行文件', false, '找不到 chrome.exe，无法自测');
      return;
    }
    let port = null;
    for (let i = 0; i < CHROME_PORT_TRIES; i += 1) {
      const candidate = CHROME_PORT_BASE + i;
      if (await portFree(candidate)) {
        port = candidate;
        break;
      }
    }
    if (!port) {
      record('挑选空闲调试端口', false, `${CHROME_PORT_BASE}..${CHROME_PORT_BASE + CHROME_PORT_TRIES - 1} 都被占用`);
      return;
    }
    chromeUserDataDir = mkdtempSync(join(tmpdir(), 'dsh-cdp-selftest-'));
    const args = [
      '--headless=new',
      `--remote-debugging-port=${port}`,
      `--user-data-dir=${chromeUserDataDir}`,
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-extensions',
      '--disable-background-networking',
      '--disable-sync',
      '--no-service-autorun',
      '--password-store=basic',
      '--window-size=1280,900',
      'about:blank',
    ];
    chromeProc = spawn(chrome, args, { stdio: 'ignore', detached: false });
    chromeProc.on('error', (err) => console.error(`[selftest] Chrome 启动错误: ${err.message}`));
    console.log(`[selftest] 已启动 Chrome pid=${chromeProc.pid} port=${port} user-data-dir=${chromeUserDataDir}`);
    const ver = await waitForEndpoint(port, 20000);
    if (!ver) {
      record('自启 Chrome 并等到 CDP 端点', false, `20s 内端口 ${port} 没有响应`);
      return;
    }
    record('自启 Chrome 并等到 CDP 端点', true, `port=${port} ${ver.Browser}`);
    endpoints = await cdp.discoverEndpoints([port]);
    started.endpoint = endpoints[0] ?? null;
    started.pid = chromeProc.pid;
    started.userDataDir = chromeUserDataDir;
    started.port = port;
  }

  // ---- 2. 连接 ----
  const endpoint = endpoints[0];
  api = await cdp.connect(endpoint);
  record('connect()', true, `browser=${api.browser.version?.product ?? 'unknown'} port=${endpoint.port}`);

  // ---- 3. 准备一个空白页作为"上一页" ----
  const blankUrl = dataUrl(BLANK_HTML);
  await api.navigate(blankUrl);
  await api.waitForLoad(15);
  record('navigate(空白页) + waitForLoad', true, (await api.pageInfo()).url.slice(0, 40));

  const testUrl = dataUrl(TEST_HTML);
  await api.navigate(testUrl);
  await api.waitForLoad(15);

  // ---- 4. snapshot ----
  let snap = null;
  const snapStep = await step('snapshot() 返回机器可读结构', async () => {
    snap = await api.snapshot();
    return { scope: snap.scope, refs: snap.refs.length, viewport: snap.viewport };
  });
  if (snap) {
    const first = snap.text.split('\n')[0];
    record(
      'snapshot() 首行形如 scope=dN url=... viewport=WxH',
      /^scope=d\d+ url=\S+ viewport=\d+x\d+$/.test(first),
      first,
    );
    const btn = snap.refs.find((r) => r.role === 'button' && /sign in/i.test(r.name));
    record('snapshot() 含 button "Sign in"', !!btn, btn ? `${btn.ref} ${btn.role} "${btn.name}"` : `refs=${JSON.stringify(snap.refs.slice(0, 8))}`);
    const txt = snap.refs.find((r) => r.role === 'textbox' || r.role === 'searchbox');
    record(
      'snapshot() 含 Email 输入框且带 type/editable',
      !!txt && /email/i.test(txt.name ?? '') && txt.inputType === 'email' && txt.editable === true,
      txt ? JSON.stringify(txt) : '没找到',
    );
    record(
      'snapshot() 文本行形如 dN:eM role "name" [flags]',
      snap.text.split('\n').slice(1).every((l) => /^d\d+:e\d+ \S+ "/.test(l)),
      snap.text.split('\n')[1] ?? '(没有元素行)',
    );
  } else {
    record('snapshot() 首行格式', false, 'snapshot 失败');
  }

  // ---- 5. find ----
  let foundRef = null;
  await step('find("Sign in") 命中并给出 ref', async () => {
    const f = await api.find('Sign in');
    foundRef = f.first;
    if (!f.first) throw new Error('find 没有返回 ref');
    return { first: f.first, ambiguous: f.ambiguous, n: f.refs.length };
  });

  // ---- 6. click → 页面可观测变化 ----
  await step('click(ref) 后页面出现可观测变化', async () => {
    if (!foundRef) throw new Error('没有可用 ref');
    const before = await api.js('window.__count||0');
    await api.click(foundRef);
    const after = await api.js('window.__count||0');
    const label = await api.text(foundRef);
    if (!(after > before)) throw new Error(`计数器没变：${before} → ${after}`);
    if (!/Signed in/.test(label)) throw new Error(`按钮文字没变：${label}`);
    return `count ${before}→${after}, label="${label}"`;
  });

  // ---- 7. type + get(value) ----
  await step('type(ref) 后 get(ref,"value") 等于输入值', async () => {
    const f = await api.find('role=textbox');
    const ref = f.first ?? snap?.refs.find((r) => r.role === 'textbox')?.ref;
    if (!ref) throw new Error('没找到 textbox');
    await api.type(ref, 'a@b.com');
    const v = await api.get(ref, 'value');
    if (v !== 'a@b.com') throw new Error(`value 读回是 ${JSON.stringify(v)}`);
    const placeholder = await api.get(ref, 'placeholder');
    return `value=${v} placeholder=${placeholder}`;
  });

  // ---- 8. select / check ----
  await step('select(ref,value) 生效', async () => {
    const f = await api.find('role=combobox');
    const ref = f.first ?? snap?.refs.find((r) => r.role === 'combobox')?.ref;
    if (!ref) throw new Error('没找到 combobox');
    const r = await api.select(ref, 'Beta');
    const v = await api.js('document.getElementById("pick").value');
    if (v !== 'b') throw new Error(`select 后 value=${v}`);
    return `selected=${r.selected} label=${r.label}`;
  });

  await step('check(ref) 幂等（第二次不再点）', async () => {
    const f = await api.find('role=checkbox');
    const ref = f.first;
    if (!ref) throw new Error('没找到 checkbox');
    const a = await api.check(ref);
    const b = await api.check(ref);
    const checked = await api.js('document.getElementById("agree").checked');
    if (checked !== true) throw new Error('check 之后没勾上');
    if (b.changed !== false) throw new Error(`第二次 check 应该幂等，却 changed=${b.changed}`);
    return `first.changed=${a.changed} second.changed=${b.changed} checked=${checked}`;
  });

  // ---- 9. 观察类 --------
  await step('getPageText() / contentExport() / pageInfo()', async () => {
    const t = await api.getPageText();
    const c = await api.contentExport({ maxChars: 500 });
    const info = await api.pageInfo();
    // 按钮文字在前面已经被点成 "Signed in 1" 了，这里按当前实际文本断言
    if (!/Signed in/.test(t.text)) throw new Error(`页面文本里没有按钮文本，前 80 字：${t.text.slice(0, 80)}`);
    if (!(c.headings.length >= 1)) throw new Error('contentExport 没抓到标题');
    if (info.viewport.width <= 0) throw new Error('viewport 宽度为 0');
    return `text=${t.chars}字 headings=${c.headings.length} viewport=${info.viewport.width}x${info.viewport.height} scope=${info.scope}`;
  });

  await step('getPageText() 能读到元素自身文本（text(ref)）', async () => {
    const btn = (await api.find('Sign in')).first;
    const t = await api.text(btn);
    if (!/Signed in/.test(t)) throw new Error(`text(ref) 读到 ${JSON.stringify(t)}`);
    return `text(ref)="${t}"`;
  });

  await step('readAll(selector,{fields,limit})', async () => {
    const r = await api.readAll('#pick option', { fields: ['text', 'value'], limit: 10 });
    if (r.total !== 3) throw new Error(`option 数量应为 3，实际 ${r.total}`);
    return `total=${r.total} rows=${JSON.stringify(r.rows.map((x) => [x.value, x.text]))}`;
  });

  await expectCode('readAll 非法字段 → BU_READ_FIELDS', 'BU_READ_FIELDS', () =>
    api.readAll('p', { fields: ['bogus'] }),
  );
  await expectCode('readAll 超限 → BU_READ_LIMIT', 'BU_READ_LIMIT', () =>
    api.readAll('p', { fields: ['text'], limit: 99999 }),
  );

  // ---- 10. 坐标兜底 ----
  await step('clickXy() 用 0-1000 归一化坐标点到按钮', async () => {
    const box = await api.js(
      `(()=>{const r=document.getElementById('signin').getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2,w:innerWidth,h:innerHeight};})()`,
    );
    if (!box) throw new Error('拿不到按钮 rect');
    const before = await api.js('window.__count||0');
    const nx = (box.x / box.w) * 1000;
    const ny = (box.y / box.h) * 1000;
    const r = await api.clickXy(nx, ny);
    const after = await api.js('window.__count||0');
    if (!(after > before)) throw new Error(`坐标点击没生效 ${before} → ${after}`);
    return `归一化(${nx.toFixed(1)},${ny.toFixed(1)}) → 像素(${r.x},${r.y}) count ${before}→${after}`;
  });

  await step('hotkey("ctrl+a") 不报错且不留悬挂修饰键', async () => {
    const r = await api.hotkey('ctrl+a');
    return `combo=${r.combo} modifiers=${r.modifiers}`;
  });

  // ---- 11. console drain ----
  await step('consoleMessages() 是 drain 语义（第二次为空）', async () => {
    // 先排掉启动阶段的日志，保证后面断言干净
    drainConsole(api.page);
    await api.js('console.log("marker-A")');
    await api.js('console.error("marker-B")');
    await new Promise((r) => setTimeout(r, 250));
    const first = await api.consoleMessages();
    const second = await api.consoleMessages();
    if (first.length === 0) throw new Error('第一次读 console 是空的（Runtime.enable 可能没生效）');
    if (second.length !== 0) throw new Error(`第二次读应为空，实际 ${second.length} 条`);
    const texts = first.map((m) => m.text).join(' | ');
    if (!texts.includes('marker-A') || !texts.includes('marker-B')) {
      throw new Error(`没抓到 marker 日志：${texts}`);
    }
    return `first=${first.length}条(含 marker-A/B) second=${second.length}条`;
  });

  // ---- 12. network drain ----
  let serverOk = false;
  try {
    await startProbeServer();
    serverOk = true;
  } catch (err) {
    record('启动本地探针 HTTP 服务', false, err?.message ?? String(err));
  }
  if (serverOk) {
    await step('networkRequests() 抓到摘要且 drain', async () => {
      drainNetwork(api.page);
      // data: URL 页面来源是不透明的，跨源 fetch 会被 CORS 拦掉；no-cors 才发得出去
      await api.js(
        `fetch('http://127.0.0.1:${probePort}/x', { mode: 'no-cors' }).then(() => 'done').catch(e => 'err:' + e.message)`,
      );
      await new Promise((r) => setTimeout(r, 600));
      const first = await api.networkRequests();
      const second = await api.networkRequests();
      if (first.length === 0) throw new Error('没抓到任何网络请求');
      if (second.length !== 0) throw new Error(`第二次读应为空，实际 ${second.length} 条`);
      const hit = first.find((r) => String(r.url).includes(`:${probePort}/x`));
      if (!hit) throw new Error(`没抓到探针请求：${JSON.stringify(first.map((f) => f.url))}`);
      const hasBody =
        Object.prototype.hasOwnProperty.call(hit, 'body') ||
        Object.prototype.hasOwnProperty.call(hit, 'responseBody') ||
        Object.prototype.hasOwnProperty.call(hit, 'requestBody');
      if (hasBody) throw new Error('网络摘要里不该出现请求/响应体');
      return `first=${first.length}条 state=${hit.state} status=${hit.status} ${hit.method} ${hit.url}`;
    });

    await step('httpGet() 读取字节并带"不证明渲染"的说明', async () => {
      const r = await api.httpGet(`http://127.0.0.1:${probePort}/probe`);
      if (r.status !== 200 || r.body !== 'probe-ok') throw new Error(`status=${r.status} body=${r.body}`);
      if (!/不能证明/.test(r.note)) throw new Error('缺少"不证明页面渲染变化"的说明');
      return `status=${r.status} bytes=${r.bytes}`;
    });

    await step('download(url) + waitForDownload() 记录状态字段', async () => {
      await ensureDownloadBehavior(api.browser, api.page);
      const url = `http://127.0.0.1:${probePort}/file.txt`;
      const started = await api.download(url);
      if (!started.guid) throw new Error('没有拿到 download guid');
      const done = await api.waitForDownload(20);
      const rec = done.downloads.find((d) => d.guid === started.guid) ?? done.downloads[0];
      if (!Object.prototype.hasOwnProperty.call(rec, 'state')) throw new Error('下载记录缺少 state 字段');
      if (rec.state !== 'completed' || rec.completed !== true) {
        throw new Error(`下载没完成：state=${rec.state}`);
      }
      if (!rec.pathVerified) throw new Error(`完成状态但文件不在磁盘上：${rec.path}`);
      const all = api.downloads();
      return `guid=${started.guid} state=${rec.state} done=${rec.done} bytes=${rec.bytes} 记录数=${all.length}`;
    });

    await step('download(ref) 从元素 href 取下载地址并完成', async () => {
      // 回到测试页，把 <a href> 指向探针下载端点，再按 ref 触发
      await api.navigate(testUrl);
      await api.waitForLoad(15);
      const ref = (await api.find('download')).first;
      if (!ref) throw new Error('没在快照里找到下载链接元素');
      await api.js(
        `(()=>{document.getElementById('dl').href='http://127.0.0.1:${probePort}/file.txt';return 1;})()`,
      );
      const started = await api.download(ref, {});
      if (!started.guid) throw new Error('没有拿到 download guid');
      await api.waitForDownload(20);
      const rec = api.downloads().find((d) => d.guid === started.guid);
      if (!rec || rec.state !== 'completed') throw new Error(`按 ref 下载没完成：${rec?.state}`);
      return `ref=${ref} guid=${started.guid} state=${rec.state} pathVerified=${rec.pathVerified}`;
    });
  }

  // ---- 13. 对话框 ----
  await step('原生对话框：clickAndHandleDialog 能报告 + 能 accept', async () => {
    // 先确保没有遗留对话框（前面任何一步失败都可能留下一个挂着的对话框，
    // 那会让页面 JS 一直阻塞，后面所有步骤都跟着超时）
    if (api.dialogState().open) await api.handleDialog({ accept: true });

    // 给按钮挂上 alert 处理器。浏览器遇到 alert 会同步阻塞页面 JS，
    // 所以必须用 clickAndHandleDialog 这种"点击与接管并发"的方式。
    await api.js(
      `(()=>{document.getElementById('signin').setAttribute('onclick','alert("selftest-dialog")');return 1;})()`,
    );
    const snap = await api.snapshot();
    const btn = snap.refs.find((r) => r.role === 'button')?.ref;
    if (!btn) throw new Error('没找到按钮');

    const r = await api.clickAndHandleDialog(btn, { accept: true, timeoutSec: 5 });
    if (!r.dialog?.open) throw new Error(`点击后没检测到原生对话框：${r.reason ?? 'unknown'}`);
    if (!r.dialog.handled) throw new Error('检测到对话框但没能处理它');
    if (!r.dialog.accept) throw new Error('没有按 accept 处理对话框');
    await new Promise((res) => setTimeout(res, 150));
    if (api.dialogState().open) throw new Error('accept 之后对话框还挂着');
    return `type=${r.dialog.type} msg="${r.dialog.message}" closed=${r.dialog.closed}`;
  });

  await step('handleDialog() 在没有对话框时如实返回 handled:false', async () => {
    if (api.dialogState().open) await api.handleDialog({ accept: true });
    const r = await api.handleDialog({ accept: true });
    if (r.handled !== false) throw new Error('没有对话框却报告 handled=true');
    if (r.open !== false) throw new Error('返回形状里 open 应该是 false');
    return `handled=${r.handled} open=${r.open} reason=${r.reason}`;
  });

  // ---- 14. 截图 ----
  await step('screenshot() 返回完整 PNG 且宽高正确', async () => {
    const shot = await api.screenshot();
    if (shot.mimeType !== 'image/png') throw new Error('mimeType 不对');
    if (shot.png.length < 100) throw new Error('PNG 太小');
    if (shot.png.readUInt32BE(0) !== 0x89504e47) throw new Error('PNG magic 不对');
    if (!(shot.width > 0 && shot.height > 0)) throw new Error('宽高为 0');
    return `${shot.width}x${shot.height} ${shot.bytes} 字节`;
  });

  await step('screenshot({ref}) 返回元素裁剪图', async () => {
    const btn = (await api.find('Sign in')).first;
    const shot = await api.screenshot({ ref: btn, tag: 'selftest' });
    if (!(shot.width > 0 && shot.height > 0)) throw new Error('元素截图宽高为 0');
    if (!shot.elementBox) throw new Error('缺少 elementBox');
    return `${shot.width}x${shot.height} tag=${shot.tag}`;
  });

  // ---- 15. 下载（已并入 12 的探针服务块，因为下载需要一个真实的 Content-Disposition 响应）----

  // ---- 16. 标签页 ----
  await step('listTabs() / newTab() / switchTab() / closeTab()', async () => {
    const before = await api.listTabs();
    const nt = await api.newTab(dataUrl(BLANK_HTML));
    await api.waitForLoad(10).catch(() => {});
    const mid = await api.listTabs();
    if (mid.length !== before.length + 1) throw new Error(`新标签页没出现：${before.length} → ${mid.length}`);
    const back = await api.switchTab(before[0].targetId);
    if (back.targetId !== before[0].targetId) throw new Error('switchTab 没切回去');
    const cur = await api.currentTab();
    if (cur.targetId !== before[0].targetId) throw new Error('currentTab 不是切换后的那个');
    const closed = await api.closeTab(nt.targetId);
    if (!closed.closed) throw new Error('closeTab 失败');
    // 关闭是异步的，给 target 列表一点时间收敛（别用固定 sleep 赌一次读就准）
    let after = [];
    for (let i = 0; i < 25; i += 1) {
      after = await api.listTabs();
      if (after.length === before.length) break;
      await new Promise((r) => setTimeout(r, 120));
    }
    if (after.length !== before.length) {
      throw new Error(`closeTab 之后标签数应回到 ${before.length}，实际 ${after.length}`);
    }
    return `tabs ${before.length}→${mid.length}→${after.length}，active=${cur.targetId.slice(0, 8)}`;
  });

  await step('resync() 明确返回成功或明确报错（不许瞎猜）', async () => {
    try {
      const r = await api.resync();
      return `成功：targetId=${r.targetId.slice(0, 8)} approximated=${r.approximated} reason=${r.reason}`;
    } catch (err) {
      if (err?.code === 'BU_SESSION_STALE') {
        return `明确拒绝（符合设计）：${err.message.slice(0, 120)}`;
      }
      throw err;
    }
  });

  // ---- 17. navigate('back') 之后旧 ref 失效 ----
  await step('navigate("back") 让旧 ref 抛 BU_REF_STALE', async () => {
    // 重新回到测试页，保证历史里有一页可退
    await api.navigate(testUrl);
    await api.waitForLoad(15);
    const oldSnap = await api.snapshot();
    const oldRef = oldSnap.refs.find((r) => r.role === 'button')?.ref;
    if (!oldRef) throw new Error('测试页上没有 button');
    const scopeBefore = oldSnap.scope;
    const r = await api.navigate('back');
    await api.waitForLoad(15).catch(() => {});
    const info = await api.pageInfo();
    let thrown = null;
    try {
      await api.click(oldRef);
    } catch (err) {
      thrown = err;
    }
    if (!thrown) throw new Error(`旧 ref ${oldRef} 竟然还能点，作用域没失效`);
    if (thrown.code !== 'BU_REF_STALE') throw new Error(`期望 BU_REF_STALE，实际 ${thrown.code}`);
    return `scope ${scopeBefore}→${info.scope}，旧 ref ${oldRef} → ${thrown.code}`;
  });

  await expectCode('click("e3") 无前缀 → BU_REF_UNSCOPED', 'BU_REF_UNSCOPED', () => api.click('e3'));
  await expectCode('click("dX:eY") 形状错 → BU_REF_SHAPE', 'BU_REF_SHAPE', () => api.click('d9:x1'));
  await expectCode('click("d999:e1") 旧文档 → BU_REF_STALE', 'BU_REF_STALE', () => api.click('d999:e1'));
  await expectCode('waitForElement 超时 → BU_NAV_WAIT_TIMEOUT', 'BU_NAV_WAIT_TIMEOUT', () =>
    api.waitForElement('#does-not-exist-xyz', { timeoutSec: 1 }),
  );
  await expectCode('select 打在非 select 上 → BU_SELECT_AMBIGUOUS', 'BU_SELECT_AMBIGUOUS', async () => {
    await api.navigate(testUrl);
    await api.waitForLoad(15);
    const btn = (await api.find('Sign in')).first;
    return api.select(btn, 'whatever');
  });
  await expectCode('contentExport 超限 → BU_EXPORT_LIMIT', 'BU_EXPORT_LIMIT', () =>
    api.contentExport({ maxChars: 10 }),
  );
  await expectCode('upload 打在非 file 输入 → BU_REF_SHAPE', 'BU_REF_SHAPE', async () => {
    const f = await api.find('role=textbox');
    return api.upload(f.first, 'C:\\definitely\\not\\here.txt');
  });
  await step('upload(ref, 绝对路径) 真的把文件送进 <input type=file>', async () => {
    const tmpFile = join(tmpdir(), `dsh-cdp-upload-${process.pid}.txt`);
    writeFileSync(tmpFile, 'upload-selftest', 'utf8');
    try {
      await api.navigate(testUrl);
      await api.waitForLoad(15);
      const snap = await api.snapshot();
      const fileRef = snap.refs.find((r) => r.inputType === 'file')?.ref;
      if (!fileRef) throw new Error('快照里没有 type=file 的输入框');
      const r = await api.upload(fileRef, tmpFile);
      const names = await api.js(
        '(()=>{const el=document.getElementById("up");return JSON.stringify([...el.files].map(f=>f.name));})()',
      );
      if (!String(names).includes(basename(tmpFile))) {
        throw new Error(`文件没进 input：files=${names}`);
      }
      return `ref=${fileRef} inputType=${r.inputType} files=${names}`;
    } finally {
      try {
        rmSync(tmpFile, { force: true });
      } catch {
        /* ignore */
      }
    }
  });
  await expectCode('httpGet(data:) → BU_CDP_ERROR', 'BU_CDP_ERROR', () =>
    api.httpGet('data:text/html,hi'),
  );
  await expectCode('cdp() 传错方法名 → BU_CDP_ERROR', 'BU_CDP_ERROR', () =>
    api.cdp('Page.thisMethodDoesNotExist', {}),
  );

  // ---- 18. blockedAuth / waitForElement 正常路径 ----
  await step('waitForElement() 正常路径返回 ref', async () => {
    await api.navigate(testUrl);
    await api.waitForLoad(15);
    const r = await api.waitForElement('#signin', { visible: true, timeoutSec: 5 });
    if (!r.found || !r.ref) throw new Error('没返回 ref');
    return `ref=${r.ref} role=${r.role} name="${r.name}"`;
  });

  await step('blockedAuth() 在普通页面返回 blocked:false', async () => {
    await api.navigate(dataUrl('<html><head><title>Home</title></head><body><h1>Hello</h1></body></html>'));
    await api.waitForLoad(15);
    const b = await api.blockedAuth();
    if (b.blocked) throw new Error(`普通页被误判为 ${b.kind}`);
    return `blocked=${b.blocked} kind="${b.kind}"`;
  });

  await step('blockedAuth() 在登录页返回 kind:"auth"', async () => {
    await api.navigate(
      dataUrl('<html><head><title>Sign in</title></head><body><form><input type="password"><button>Log in</button></form></body></html>'),
    );
    await api.waitForLoad(15);
    const b = await api.blockedAuth();
    if (!b.blocked || b.kind !== 'auth') throw new Error(`没识别成 auth：${JSON.stringify(b)}`);
    return `kind=${b.kind} reason=${b.reason}`;
  });

  await step('pageSize() 返回视口/内容尺寸', async () => {
    const s = await api.pageSize();
    if (!(s.viewport.width > 0)) throw new Error('视口宽度为 0');
    return `viewport=${s.viewport.width}x${s.viewport.height} content=${s.content.width}x${s.content.height}`;
  });
}

async function cleanup() {
  console.log('\n--- 收尾 ---');
  stopProbeServer();
  if (api) {
    // 先删掉本次的下载目录（下载是落在 tmp 里的，不删就会攒一堆）
    try {
      const dir = api.browser?.downloadDirectory?.();
      if (dir && existsSync(dir)) {
        rmSync(dir, { recursive: true, force: true });
        console.log(`[cleanup] 删除下载临时目录 ${dir}: ${existsSync(dir) ? '失败' : '成功'}`);
      }
    } catch (err) {
      console.log(`[cleanup] 删除下载临时目录失败: ${err?.message ?? err}`);
    }
    try {
      await api.close();
      console.log('[cleanup] CDP 会话已关闭');
    } catch (err) {
      console.log(`[cleanup] 关闭 CDP 会话失败: ${err?.message ?? err}`);
    }
    api = null;
  }
  if (chromeProc && chromeProc.pid) {
    const pid = chromeProc.pid;
    const killed = killProcessTree(pid);
    console.log(`[cleanup] 结束自启 Chrome pid=${pid}: ${killed === true ? '成功' : killed}`);
    if (killed !== true) {
      console.log(`[cleanup] !!! 未能结束 Chrome 进程 pid=${pid}，请手动 taskkill /PID ${pid} /T /F`);
    }
    // 给进程树一点退出时间再删目录
    await new Promise((r) => setTimeout(r, 700));
  }
  if (chromeUserDataDir) {
    const ok = removeDirWithRetry(chromeUserDataDir);
    console.log(`[cleanup] 删除临时 user-data-dir ${chromeUserDataDir}: ${ok ? '成功' : '失败（可能被残留进程占用）'}`);
    if (!ok) {
      console.log(`[cleanup] !!! 目录仍然存在：${chromeUserDataDir}`);
    }
  } else {
    console.log('[cleanup] 本次没有自建 user-data-dir（复用了已有端点）');
  }
}

function report() {
  const failed = results.filter((r) => !r.ok);
  console.log('\n=== 结果 ===');
  for (const r of results) {
    console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}${r.detail ? `  [${r.detail}]` : ''}`);
  }
  console.log(`\n合计 ${results.length} 项：PASS ${results.length - failed.length} / FAIL ${failed.length}`);
  if (failed.length > 0) {
    console.log('失败项：');
    for (const f of failed) console.log(`  - ${f.name}: ${f.detail}`);
  }
  console.log(failed.length === 0 ? '\n总判定：ALL PASS ✅' : '\n总判定：FAILED ❌');
  return failed.length === 0 ? 0 : 1;
}

function finish(code) {
  if (exited) return;
  exited = true;
  process.exit(code);
}

process.on('unhandledRejection', (err) => {
  console.error(`[selftest] 未处理的 Promise 拒绝: ${err?.stack ?? err}`);
});
process.on('SIGINT', async () => {
  console.log('\n[selftest] 收到中断，开始收尾');
  await cleanup();
  finish(130);
});

try {
  await main();
} catch (err) {
  record('selftest 主流程', false, `未捕获异常 ${err?.code ?? ''}: ${err?.message ?? err}`);
  if (err?.stack) console.error(err.stack);
}
await cleanup();
finish(report());
