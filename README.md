# dsh-computer-use

**DeepSeek Harness 的电脑操作插件**：把 `computer_use` 与 `browser_use` 两个工具注册进 DSH 工具表，
用 Python 常驻服务操作 Windows 桌面，用零依赖 CDP 操作浏览器。截图**直接作为图片块**返回，
不落盘、不用再 `read` 一次。

> A computer-use / browser-use plugin for DeepSeek Harness on Windows. Resident Python service,
> UIA element layer, virtual desktops, and a zero-dependency CDP browser plane.

- **零 Node 依赖、无构建**：只用 `node:*` 内置模块 + Node 22 的原生 `WebSocket` / `fetch`。
- **Python 侧只要两个包**：`pillow`（截图）与 `pywin32`（命名管道）。缺了会告诉你装什么。
- **软安装**：不需要安装程序。把仓库目录交给 DSH 的 `plugin_manager` 就装好了，
  详见 [INSTALL.md](INSTALL.md)（写给 agent 的分步指引）。

---

## 它解决什么问题

只给模型一个 shell，也能截图、也能点鼠标——但每一步都要"拼命令行 → 解析文本 → 再拼下一条"，
拼错时失败是**静默**的（点到了别的地方、截到了别的窗口）。做成插件之后：

| | 技能（SKILL.md + CLI） | 本插件 |
|---|---|---|
| 调用方式 | 每条命令一次 shell 往返 | DSH 工具表里的 `computer_use` / `browser_use` |
| 截图 | 写临时文件，再 `read_image` | **直接返回图片块**（走 `attachments`） |
| 生命周期 | 靠模型记得收尾 | 宿主加载即拉起、卸载即回收，退出跟子进程走 |
| 参数校验 | 靠模型自觉 | 内核校验 + 统一错误码 + `[next]` 提示 |
| 结果分类 | 文本自己猜 | `invalid_action` / `state_changed` / `blocked` 明确分流 |

---

## 能力

**`computer_use`（Windows 桌面）**

- 观察：`shot`（窗口/区域/整屏，`PrintWindow` 截被遮挡窗口、不抢前台）、`windows`、`where`、`rect`、
  `state_capture`（UIA 元素树，带 `eN` ref）、`find`（按名字/角色/正则找元素）
- 输入：`click` / `double_click` / `right_click` / `drag` / `scroll` / `type` / `key` / `paste`，
  坐标一律 **0-1000 归一化**（`region`、`at`、`element_at` 才吃物理像素）
- 元素：`element eN --invoke` 走 UI Automation 的 Invoke/SetValue/Toggle，比估坐标稳
- 应用：`launch` / `app_is_running` / `app_resize` / `app_terminate`（破坏性，必须 `confirm:true`）/
  `spawn`（不经 shell，参数原样传）
- 桌面：`desktop_list` / `desktop_switch` / `desktop_create` / `desktop_remove`；
  `shadow_start` 起隔离桌面（不抢鼠标，代价是真实注入被系统拒绝，只能走 `element` 的 Invoke/SetValue）
- 等待：`watch_start` → `watch_recv`（后台排队等变化，等待期间零 token），别连续截图
- 服务：`server_status` / `server_restart` / `doctor` / `codes`

**`browser_use`（浏览器平面，纯 CDP）**

- `launch`（独立 `--user-data-dir`，因为 Chrome 136+ 拒绝在默认 profile 上开调试端口）、`status`、
  `tabs` / `tabs_new` / `tabs_switch` / `tabs_close`
- `snapshot`（可访问性树，`dN:eM` 文档作用域）、`find`、`click` / `type` / `fill_input` / `select`
- `read_text` / `read_all` / `get_page_text` / `content_export` / `query`
- `console` / `network` / `http_get` / `download` / `wait_for_*` / `screenshot`

> **选平面**：网页内优先 `browser_use`（吃可访问性树，不靠截图估坐标）；
> 真实系统 UI、文件对话框、任务栏、非浏览器应用走 `computer_use`。一个工作流内平面固定。

---

## 安装

```jsonc
// 在 DSH 会话里，让 agent 执行：
plugin_manager { "action": "install_bundle", "target": "<本仓库的绝对路径>" }
```

完整步骤（含无 `plugin_manager` 时的兜底、发布模式 tarball、卸载回滚、自检清单）见
[INSTALL.md](INSTALL.md)。

装完自检：

```
computer_use { "action": "doctor" }        # 必需项应全 OK
computer_use { "action": "shot" }          # 会话里应直接渲染出图片
computer_use { "action": "server_status" } # running: true
```

---

## 已知边界（不假装做到）

- **内部虚拟桌面 COM 多数机器拿不到**（`REGDB_E_CLASSNOTREG`）：`desktop_list` 返回 `available:false`，
  切换用 `--direction left|right` 的热键路径。
- **Chrome 136+ 默认 profile 不能开调试端口**：`browser_use` 的 `launch` 用独立 user-data-dir，
  看不到你原本登录态里的标签页；需要登录的站点要重新登。
- **`eval` 的线程杀不掉**：超时只返回错误，真挂住用 `server_restart` 回收服务。
- **UIA 元素层在自绘界面上不可用**（无树可读）：退回 `shot` 看图路线。
- **截图没上 WGC**：`Windows.Graphics.Capture` 要 WinRT + D3D11 互操作，ctypes 下极易 access violation；
  `PrintWindow(PW_RENDERFULLCONTENT)` 已达成"截被遮挡窗口 + 不抢前台"。
- **`resync()` 判不出"屏幕上可见的标签页"**：CDP 没这个 API，能唯一确定才返回，否则抛 `BU_SESSION_STALE`。

---

## 目录

| 文件 | 作用 |
|---|---|
| `index.js` | Host 半：注册两个工具与自带技能、拉起/回收常驻服务 |
| `lib/computer-use.mjs` | `computer_use` 工具：action → argv、结果分类、`[next]` 提示、图片块渲染 |
| `lib/browser-use.mjs` + `lib/cdp/**` | CDP 浏览器平面（零依赖，`node lib/cdp/selftest.mjs` 自测） |
| `lib/pipe-client.mjs` | Node ↔ Python 分帧协议、请求串行、**不重发**、超时 |
| `lib/server-manager.mjs` | 解释器解析、拉起、探测复用、崩溃自愈、优雅停止 |
| `python/cu.py` | 内核 CLI：校验 / 审计 / PNG 三重校验 / 元素层 / 虚拟桌面 / doctor |
| `python/cu_server.py` | 常驻服务：传输层 / 分帧 / 单实例 / batch / eval / 信封校验 |
| `python/uia*.py` `python/uia_sidecar.ps1` | UIA 元素层（常驻 PowerShell 行协议） |
| `python/desktop.py` | 虚拟桌面（公共 COM / 内部 COM / 热键三档） |
| `skills/` | 随包下发的 `computer-use` / `browser-use` 技能文本（模型自动加载） |
| `scripts/` | `pack.ps1` 打包、`install-profile.ps1` 兜底安装、`chrome-debug.cmd` 起调试 Chrome、`rollback.ps1` |
| `tests/` | 契约测试（`python tests/run_all.py`） |
| `docs/` | 实战复盘：从一次 1000+ 题的浏览器自动化任务里提炼的通用经验 |

---

## 一条来自实战的经验

`docs/lessons-from-theol.md` 是一次真实任务（1000+ 题在线测试）的复盘：**先找接口再想操作**、
**单次执行预算 <20 秒**、**原生弹窗不是页面元素**。里面的 9 条规则换任何站点都成立，
也是本插件后续要补的能力清单。

## License

MIT
