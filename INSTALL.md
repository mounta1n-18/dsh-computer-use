# dsh-computer-use — 安装与使用

> **软安装（换一台机器、由 agent 代装）先读 [`AGENT-INSTALL.md`](AGENT-INSTALL.md)**：
> 那一页给的是逐步流程、判定标准与常见坑；这一页讲功能和参数。

把 `~/.dsh/skills/computer-use` 那套"CLI + 提示词"升级成的 **DSH Cordis 插件**：
注册 `computer_use` / `browser_use` 两个工具，Python 侧是常驻服务，截图直接作为图片块返回。

**零 Node 依赖、无构建步骤**：只用 `node:*` 内置模块 + Node 22+ 原生 `WebSocket`/`fetch`；
Python 侧只需要 `pillow`（已有）+ `pywin32`（已有，命名管道用）。

---

## 1 前置条件（先跑一次自检）

```powershell
python <插件目录>\python\cu.py doctor
```

必需项：Python 3.10+、pillow、pywin32（Windows）。可选项（缺了只是降级，不影响装机）：
UIA 元素层、虚拟桌面内部 COM、浏览器 CDP 端点。

缺依赖时它会打印**可直接粘出去跑**的命令；也可以：

```powershell
python <插件目录>\python\cu.py setup            # 只打印命令
python <插件目录>\python\cu.py setup --install  # 真的装（默认不代劳，改用户环境属于偏重操作）
```

---

## 2 安装（两种模式，二选一）

安装动作**只能**通过 DSH 的 `plugin_manager` 工具完成——这是官方唯一支持的路径
（手改 profile 的 `package.json` / `pnpm-lock.yaml` 会留下不一致）。

### A. 开发模式：文件夹链接安装（改代码即时生效）

```jsonc
plugin_manager { "action": "install_bundle", "target": "C:\\DSHprojects\\cu-improve\\dsh-plugin-computer-use" }
```

- pnpm 把依赖记成 `link:`，**本体始终是这个文件夹**：改 `index.js` / `python/*.py`
  后重启 DSH（或刷新页面）即生效
- 代价：**删除或移动这个文件夹前必须先卸载**，否则该 profile 启动时会报缺包

### B. 发布模式：tarball 独立副本安装（本体可删可移）

```powershell
powershell -ExecutionPolicy Bypass -File <插件目录>\scripts\pack.ps1
```

```jsonc
plugin_manager { "action": "install_bundle", "target": "<绝对路径>\\local-dsh-computer-use-1.0.0.tgz" }
```

升级：改 `package.json` 的 `version` → 重新 pack → 再 `install_bundle`。

### 装完要不要重启？

插件加载属于**条目树变更**。多数情况下 DSH 会热重载；如果工具没出现，重启 DSH
（或刷新页面）再看。**重启会结束当前会话，请先确认手头没有跑一半的活。**

---

## 3 验证装好了

1. **结构**：`cordis_inspect_query(host/Tool)` 里应能看到 `computer_use` 与 `browser_use`。
2. **行为**（在本会话里直接调工具）：
   - `computer_use{action:"doctor"}` → 必需项全 OK
   - `computer_use{action:"shot"}` → **会话里直接渲染出图片**（不是文件路径）
   - `computer_use{action:"where"}` → 光标像素 + 归一化坐标
   - `computer_use{action:"state_capture"}` → 一串带 `e1..eN` 的元素
   - `computer_use{action:"server_status"}` → `running: true`、`owned`、`transport`
3. **浏览器平面**：先 `scripts\chrome-debug.cmd` 起一个带调试端口的 Chrome，
   再 `browser_use{action:"status"}` 应扫到端点；`snapshot` 出 `scope=dN …` 与 `dN:eM`。
4. **卸载后干净**：`plugin_manager remove_bundle` → 再跑
   `scripts\rollback.ps1` 清运行目录，最后确认没有残留 `python.exe`。

---

## 4 用起来是什么样

技能文本随插件下发（`computer_use` / `browser_use` 各自一份 SKILL.md），模型会自动加载。
要点：

- 坐标一律 **0-1000 归一化**；`region` / `at` 这类参数才吃物理像素。
- `shot` 直接回图片、不落盘；要留证据才给 `out`。
- 网页内优先 `browser_use`（吃可访问性树），本地应用/系统 UI 走 `computer_use`；
  **一个工作流内平面固定**。
- 等外部事件用 `watch_start` → `watch_recv`（后台排队、等待零 token），别连续截图。
- 元素级：`state_capture` 拿 `eN`，`element eN --invoke` 走 UI Automation，比估坐标稳。
- 结果带 `status`：`invalid_action` 改参数、`state_changed` 重新观察、`blocked` 请用户接管。
- 应用/窗口：`app_is_running` 看有没有在跑；`app_resize` 只改尺寸；`app_terminate` 是破坏性
  动作，**必须传 `confirm:true`** 否则返回 `CU_CONFIRM_REQUIRED`；`spawn` 直接起任意可执行
  文件（不经 shell，参数原样传）；`hybrid_programs` 报告"哪些可见窗口其实是浏览器 + 有没有
  CDP 端点"，用来决定走 cu 还是 bu。
- 元素查找支持 `find{pattern, role, regex, exact, index}`，返回带 `eN` ref 的结构化结果。


CLI 仍然完整可用（降级/调试）：

```powershell
python <插件目录>\python\cu.py shot --long 800
python <插件目录>\python\cu.py click 500 500
python <插件目录>\python\cu.py state-capture --window 记事本
python <插件目录>\python\cu.py 码          # 错误码表
```

---

## 5 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `DSH_CU_TRANSPORT` | `named_pipe` / `uds` / `tcp` | Windows 命名管道 / 其它 UDS |
| `DSH_CU_ENDPOINT` | 管道名或 socket 路径 | `\\.\pipe\dsh-computer-use` |
| `DSH_CU_PORT` | tcp 端口 | 无 |
| `DSH_CU_FALLBACK_TRANSPORT` / `DSH_CU_FALLBACK_PORT` | 兜底传输（tcp） | 空 |
| `DSH_CU_CONNECT_TIMEOUT` | 连接超时秒 | 5.0 |
| `DSH_CU_IO_TIMEOUT` | 单请求 IO 超时秒 | 30 |
| `DSH_CU_HOME` | 运行目录（审计/日志/socket） | `%LOCALAPPDATA%\dsh-computer-use` |
| `DSH_CU_PYTHON` | 指定 Python 绝对路径 | 自动探测 |
| `DSH_CU_LAZY` | `1` = 首次调用才拉起服务 | 空（加载即拉起） |
| `DSH_CU_DEBUG` | `1` = 插件往 stderr 打调试日志 | 空 |
| `DSH_CU_SERVER_STDERR` | `1` = 服务日志同时写 stderr | 空 |

---

## 6 卸载 / 回滚

1. `plugin_manager { "action": "remove_bundle", "target": "@local/dsh-computer-use" }`
2. `powershell -ExecutionPolicy Bypass -File <插件目录>\scripts\rollback.ps1 -OldSkillsBackup <备份目录>`
   —— 回收残留进程、松开 latch 的修饰键、删运行目录与截图缓存，并打印恢复旧技能的命令
3. 旧技能目录：升级前的备份在 `~/.dsh/backups/iter6-computer-use-<时间戳>/`，
   需要的话拷回 `~/.dsh/skills/computer-use/`
4. 检查 `~/.dsh/profiles/<profile>/cordis.patch.yml` 是否残留
   `- id: computer-use` 的覆盖项，有就删掉（切换插件开关时 plugin_manager 会追加）

---

## 7 已知边界（不假装做到）

- **内部虚拟桌面 COM 多数机器拿不到**（本机实测 `REGDB_E_CLASSNOTREG`）：
  `desktop_list` 会如实返回 `available:false`；切换用 `--direction left|right`（热键路径）。
  `desktop_create/remove` 不会代替你按热键。
- **`target:"pip"` 永远不支持**：那是豆包沙箱的 PiP 会话桌面概念，本机没有。
- **Chrome 136+ 拒绝在默认 profile 上开调试端口**：`browser_use{action:"launch"}` 与
  `scripts/chrome-debug.cmd` 都用独立 `--user-data-dir`；同一个 chrome 实例**看不到**
  你原来登录态里的标签页（需要登录的站点要重新登）。
- **`resync()` 判不出"屏幕上可见的标签页"**：CDP 没这个 API；能唯一确定才返回，
  否则抛 `BU_SESSION_STALE` 让你用 `listTabs()+switchTab()`。
- **`eval` 的线程杀不掉**：超时只返回错误，真挂住用 `server_restart` 回收服务。
- **UIA 元素层在自绘界面上不可用**（无树可读）：退回 `shot` 看图路线。
- **截图是双后端**：窗口截图走 `PrintWindow(PW_RENDERFULLCONTENT)`——**被别的窗口盖住也能截**、
  不抢前台，并用 DWM 可见边界裁掉 Win11 的隐形边框；全屏走 GDI（Pillow/ImageGrab）。
  PrintWindow 拿不到的重绘型窗口会自动退回屏幕裁剪并做遮挡检测（`CU_COVERED`）。
  **没上 WGC**：`Windows.Graphics.Capture` 要 WinRT+D3D11 互操作，ctypes 下极易
  access violation；PrintWindow 已达成同样的"截遮挡窗口 + 不抢前台"效果。

---

## 8 文件清单

| 文件/目录 | 作用 |
|---|---|
| `package.json` | 包清单：`dsh.bundle.patch` + `files` 白名单 |
| `cordis.patch.yml` | 一条 `insert`，把插件挂进 profile 条目树 |
| `index.js` | Host 半：注册两个工具 + 自带技能、拉起/回收常驻服务、占 provider 槽 |
| `lib/pipe-client.mjs` | Node ↔ Python 分帧协议、请求串行、**不重发**、超时 |
| `lib/server-manager.mjs` | 解释器解析、拉起、探测复用、崩溃自愈、优雅停止 |
| `lib/computer-use.mjs` | `computer_use` 工具：action→argv、status 分类、`[next]` 提示、图片块渲染 |
| `lib/browser-use.mjs` | `browser_use` 工具：包 `lib/cdp` |
| `lib/cdp/**` | 纯 CDP 浏览器平面（零依赖，`node lib/cdp/selftest.mjs` 自测 47 项） |
| `python/cu.py` | 内核 CLI（v2：校验/审计/PNG 三重校验/元素层/虚拟桌面/doctor） |
| `python/cu_server.py` | 常驻服务（传输层/分帧/单实例/batch/eval/信封校验） |
| `python/uia_sidecar.ps1` + `python/uia.py` | UIA 元素层（常驻 PowerShell 行协议） |
| `python/desktop.py` | Windows 虚拟桌面（公共 COM / 内部 COM / 热键三档） |
| `skills/computer-use/` | 自带技能（SKILL.md ≤5KB + `references/full-manual.md`） |
| `skills/browser-use/` | 浏览器平面技能 |
| `skills/im-chat/` | 社媒场景技能（随包分发的副本，不注册） |
| `tests/` | 契约测试（`python tests/run_all.py` 一键全跑） |
| `scripts/` | `pack.ps1` 打包、`chrome-debug.cmd` 起调试 Chrome、`rollback.ps1` 回滚 |
