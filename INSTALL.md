# INSTALL — 写给 agent 的安装指引

> 这份文件是给**模型/agent** 看的，不是给人看的安装向导。人类只需要把仓库路径交给你。
> 每一步都有"怎么确认这一步成了"，不要跳过验证。
> 目标：让 `computer_use` 与 `browser_use` 出现在工具表里，并且 `shot` 能直接回图片。

---

## 0. 先看环境（30 秒）

```powershell
node -v            # 需要 >= 22（用到原生 WebSocket / fetch）
python -V          # 需要 >= 3.10
python -c "import PIL, win32pipe; print('deps ok')"
```

缺 `pillow` / `pywin32` 时，插件自己能告诉你装什么：

```powershell
python <仓库>\python\cu.py setup            # 只打印命令
python <仓库>\python\cu.py setup --install  # 真的装（默认不代劳）
```

**不要**在没有确认的情况下 `--install`——改用户 Python 环境属于偏重操作，先打印、问过再装。

---

## 1. 安装（三条路，按优先级选）

### A. 官方路径：`plugin_manager`（推荐）

在 DSH 会话里调用（这是官方唯一支持的安装路径）：

```jsonc
plugin_manager { "action": "install_bundle", "target": "<仓库绝对路径>" }
```

- 依赖会被 pnpm 记成 `link:`，**本体就是仓库目录**：改 `index.js` / `python/*.py` 后重启 DSH 即生效；
- 代价：**移动或删除仓库目录前必须先卸载**，否则该 profile 启动会报缺包。

确认装上了：

```jsonc
plugin_manager { "action": "list_bundles" }        // 应看到 @local/dsh-computer-use
```

### B. 发布模式：tarball 独立副本（本体可删可移）

```powershell
powershell -ExecutionPolicy Bypass -File <仓库>\scripts\pack.ps1
# 产出 local-dsh-computer-use-<version>.tgz
```

```jsonc
plugin_manager { "action": "install_bundle", "target": "<绝对路径>\\local-dsh-computer-use-1.0.0.tgz" }
```

升级：改 `package.json` 的 `version` → 重新 pack → 再 `install_bundle`。

### C. 兜底：没有 `plugin_manager` 时

只有当本机 DSH 没有暴露 `plugin_manager` 工具时才走这条。脚本会**先备份再改**，
默认 dry-run，必须显式 `-Apply`：

```powershell
powershell -ExecutionPolicy Bypass -File <仓库>\scripts\install-profile.ps1            # 只打印将要做的改动
powershell -ExecutionPolicy Bypass -File <仓库>\scripts\install-profile.ps1 -Apply     # 真的改
```

它做两件事（只改 `<DSH_HOME>\profiles\<profile>\package.json`，带 `.bak-<时间戳>`）：

1. `dependencies` 增加 `"@local/dsh-computer-use": "link:<仓库绝对路径>"`；
2. `dsh.profile.bundles` 追加 `"@local/dsh-computer-use"`。

改完需要**重启 DSH**（`plugin_manager` 没装成时，pnpm 的软链要等宿主下次解析依赖才建）。
如果重启后工具仍不出现，说明这台 DSH 的依赖解析不认手改——回退到 A 或 B，并删除 `.bak` 之外的改动。

---

## 2. 装完必做的四项验证

```jsonc
computer_use { "action": "doctor" }          // ① 必需项全 OK；缺什么它会直接给命令
computer_use { "action": "shot" }            // ② 会话里应**直接渲染出图片**（不是文件路径）
computer_use { "action": "server_status" }   // ③ running: true，能看到 transport
computer_use { "action": "where" }           // ④ 光标像素 + 归一化坐标（输入链路通）
```

浏览器平面另测：

```powershell
powershell -ExecutionPolicy Bypass -File <仓库>\scripts\chrome-debug.cmd   # 起带调试端口的 Chrome
```

```jsonc
browser_use { "action": "status" }    // 应扫到端点
browser_use { "action": "snapshot" }  // 应出 scope=dN … 与 dN:eM
```

四项里任何一项失败，先看 `computer_use { "action": "codes" }` 的错误码表，不要瞎试。

---

## 3. 卸载 / 回滚

```jsonc
plugin_manager { "action": "remove_bundle", "target": "@local/dsh-computer-use" }
```

```powershell
powershell -ExecutionPolicy Bypass -File <仓库>\scripts\rollback.ps1
# 回收残留 python 进程、松开 latch 的修饰键、删运行目录与截图缓存
```

- 走 C 路线装的，还要把 `package.json` 从 `.bak-<时间戳>` 还原（脚本会打印要还原的路径）。
- 检查 `<DSH_HOME>\profiles\<profile>\cordis.patch.yml` 是否残留 `- id: computer-use`，有就删。
- 确认没有残留 `python.exe`：`Get-Process python -ErrorAction SilentlyContinue`。

---

## 4. 环境变量（都在 `python/` 侧生效）

| 变量 | 作用 | 默认 |
|---|---|---|
| `DSH_CU_TRANSPORT` | `named_pipe` / `uds` / `tcp` | Windows 命名管道，其它 UDS |
| `DSH_CU_ENDPOINT` | 管道名或 socket 路径 | `\\.\pipe\dsh-computer-use` |
| `DSH_CU_PORT` | tcp 端口 | 无 |
| `DSH_CU_FALLBACK_TRANSPORT` / `_PORT` | 兜底传输（tcp） | 空 |
| `DSH_CU_CONNECT_TIMEOUT` | 连接超时秒 | 5.0 |
| `DSH_CU_IO_TIMEOUT` | 单请求 IO 超时秒 | 30 |
| `DSH_CU_HOME` | 运行目录（审计 / 日志 / socket） | `%LOCALAPPDATA%\dsh-computer-use` |
| `DSH_CU_PYTHON` | 指定 Python 绝对路径 | 自动探测 |
| `DSH_CU_LAZY` | `1` = 首次调用才拉起服务 | 空（加载即拉起） |
| `DSH_CU_DEBUG` | `1` = 插件往 stderr 打调试日志 | 空 |
| `DSH_CU_SERVER_STDERR` | `1` = 服务日志同时写 stderr | 空 |

---

## 5. 换机器时最容易踩的三件事

1. **Python 不止一个。** `python` 指向的可能不是装了 pillow/pywin32 的那个；
   用 `DSH_CU_PYTHON` 钉死绝对路径，或先在 `doctor` 里看清它探测到了谁。
2. **Chrome 136+ 的默认 profile 开不了调试端口。** `browser_use` 看的是独立 user-data-dir 的实例，
   你日常那个已登录的 Chrome **不会**被接管——需要登录态的站，要么在调试实例里重登，
   要么换 `huashu-chrome` 那类"驱动用户真实 Chrome"的方案。
3. **命名管道被安全软件拦。** 症状是 `server_status` 连不上但进程活着；
   设 `DSH_CU_TRANSPORT=tcp` + `DSH_CU_PORT=9xxx` 可绕过。

---

## 6. 文件清单

| 文件/目录 | 作用 |
|---|---|
| `package.json` | 包清单：`dsh.bundle.patch` + `files` 白名单 |
| `cordis.patch.yml` | 一条 `insert`，把插件挂进 profile 条目树 |
| `index.js` | Host 半：注册工具与技能、拉起/回收常驻服务 |
| `lib/pipe-client.mjs` | Node ↔ Python 分帧协议、请求串行、不重发、超时 |
| `lib/server-manager.mjs` | 解释器解析、拉起、探测复用、崩溃自愈 |
| `lib/computer-use.mjs` / `lib/browser-use.mjs` | 两个工具的参数整形与结果渲染 |
| `lib/cdp/**` | 纯 CDP 浏览器平面（零依赖，自带 selftest） |
| `python/cu.py` | 内核 CLI（校验/审计/PNG 三重校验/元素层/虚拟桌面/doctor） |
| `python/cu_server.py` | 常驻服务（传输/分帧/单实例/batch/eval/信封校验） |
| `python/uia*` | UIA 元素层（常驻 PowerShell 行协议） |
| `python/desktop.py` / `shadow.py` | 虚拟桌面与隔离桌面 |
| `skills/**` | 随包下发的技能文本 |
| `scripts/**` | `pack.ps1` / `install-profile.ps1` / `chrome-debug.cmd` / `rollback.ps1` |
| `tests/**` | 契约测试（`python tests/run_all.py`） |
