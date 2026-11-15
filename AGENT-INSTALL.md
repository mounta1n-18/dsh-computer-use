# 软安装必读（给接手安装的 agent）

> 你先读这一页，再动手。`INSTALL.md` 是给人看的功能说明，这一页是给 **agent** 看的安装流程：
> 每一步都有"怎么算成功"，不满足就不许往下走。
> 目标：一台**没装过任何东西**的 Windows 机器，最终 `python python/cu.py doctor` 里
> `python / pillow / pywin32` 三项全绿，`computer_use` 与 `browser_use` 两个工具出现在 DSH 里。

---

## 0 先搞清楚要装什么（30 秒）

| 依赖 | 用途 | 形式 | 体量 |
|---|---|---|---|
| Python 3.10+ | 服务端与注入内核的运行环境 | 系统安装包 / `winget` | 安装 ~40 s |
| `pillow` | 画提示层（边框、光标圆、鲸鱼、横幅）、处理截图 PNG | 预编译 wheel | 下载 ~2.6 MB |
| `pywin32` | 调 Win32：命名管道、低级键鼠钩子、窗口消息 | 预编译 wheel | 下载 ~9 MB |
| DSH bundle | 让 DSH 认识这个插件 | `plugin_manager install_bundle` | 本体 ~1 MB |

两个 wheel 都**不需要编译器、不需要管理员**，`pip install --user` 即可。
DSH 自带 Node，不需要你装 Node。

**实测耗时**（干净的 venv，家用宽带）：`pip install pillow pywin32` **约 53 秒**；
装 CPython 大约 40–90 秒。全程 2–3 分钟。

---

## 1 找 Python：先探测，别急着重装

按顺序试，**命中即停**：

```powershell
# 1) PATH 里的 python / py
python -c "import sys;print(sys.version_info[:2], sys.executable)"
py -3 -c "import sys;print(sys.version_info[:2], sys.executable)"

# 2) DSH 已经指定过的解释器
$env:DSH_CU_PYTHON

# 3) 常见安装位置
Get-ChildItem "$env:LOCALAPPDATA\Programs\Python" -Directory -ErrorAction SilentlyContinue
Get-ChildItem "C:\Python3*" -Directory -ErrorAction SilentlyContinue
```

**判定标准**：版本 ≥ 3.10，且 `sys.executable` 指向真实的 `python.exe`（不是
`WindowsApps\python.exe` 那个 0 字节的商店占位符——它一跑就弹微软商店）。

> 记下这个绝对路径，后面每一步都用**同一个**解释器。装依赖装到 A、DSH 却用 B，
> 是最常见的"装完了还是缺包"。

---

## 2 装 Python（第 1 步没找到才做）

```powershell
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
```

没有 `winget` 就去 <https://www.python.org/downloads/windows/> 下 3.12 的
**Windows installer (64-bit)**，安装时勾选 **Add python.exe to PATH**。

为什么推荐 3.12 而不是最新版：3.10–3.13 的 pillow / pywin32 轮子最成熟；
3.14 也能用（本机就是 3.14.6 实测通过），但属于刚出的版本，遇到轮子缺失要多绕一步。

**验证**：开一个新终端，`python -c "import sys;print(sys.version)"` 能打印版本。
新装的 PATH 不会进你已经开着的进程，**这一步必须新开终端**再试。

---

## 3 装两个包

```powershell
& "<第 1 步记下的 python.exe>" -m pip install --user --upgrade pillow pywin32
```

- 优先 `--user`：写用户目录，不需要管理员，也不会污染别的项目。
- 报 `externally-managed-environment` 或权限错误时，改成 venv：
  `& "<python>" -m venv "$env:LOCALAPPDATA\dsh-cu-venv"`，然后用
  `"$env:LOCALAPPDATA\dsh-cu-venv\Scripts\python.exe" -m pip install pillow pywin32`，
  并把 `DSH_CU_PYTHON` 指向这个 venv 的 python.exe。
- 公司网络/代理导致下载失败：`-i https://pypi.tuna.tsinghua.edu.cn/simple`，
  或加 `--proxy http://<host>:<port>`。不要为此去改系统代理。

**验证**（用同一个解释器）：

```powershell
& "<python>" -c "import PIL, win32api; print('ok', PIL.__version__)"
```

打印 `ok <版本>` 才算过。报 `DLL load failed` 时跑一次
`& "<python>" -m pywin32_postinstall -install`，再重试。

---

## 4 自检：doctor 必须全绿

```powershell
& "<python>" "<插件目录>\python\cu.py" doctor
```

必须通过的三项：`python`、`pillow`、`pywin32`。

其余项（`uia_sidecar`、`browser_cdp`、虚拟桌面内部 COM 等）**失败是允许的**——
它们是可选能力，缺了只降级，不影响装机。

> 如果 `python` 项显示的解释器不是你装包的那个，说明 DSH 会去用别的解释器：
> 把 `DSH_CU_PYTHON` 设成你装包的那个绝对路径，再跑一次 doctor。

---

## 5 装进 DSH

**只能**通过 `plugin_manager`，不要手改 profile 的 `package.json` / `pnpm-lock.yaml`。

```jsonc
// 开发模式：文件夹链接，改代码后重启 DSH 即生效
plugin_manager { "action": "install_bundle", "target": "<插件目录>" }

// 发布模式：独立副本，本体可删可移
plugin_manager { "action": "install_bundle", "target": "<插件目录>\\local-dsh-computer-use-1.0.0.tgz" }
```

装完若工具没出现，重启一次 DSH（会结束当前会话，先确认没有跑一半的活）。

**验证**：`cordis_inspect_query(host / Tool)` 里出现 `computer_use` 与 `browser_use`；
或直接调 `computer_use{action:"doctor"}` 看必需项全 OK。

---

## 6 冒烟（做完这一步才算装好）

1. `computer_use{action:"shot"}` → 会话里直接渲染出图片（不是文件路径）。
2. `computer_use{action:"where"}` → 返回光标像素坐标与归一化坐标。
3. `computer_use{action:"activate", label:"<会话标识>"}` → 屏幕四边出现蓝色渐变提示、
   右下角出现鲸鱼；用户点"同意"后 `computer_use{action:"deactivate"}` → 提示消失、服务退出。

第 3 步会真的动屏幕。**做之前先问用户**，并告诉他按 `Ctrl+Alt+Shift+Backspace` 可以立刻关掉。

---

## 7 常见坑（按出现频率排序）

| 症状 | 原因 | 处理 |
|---|---|---|
| `python` 项 OK、`pillow` 项 FAIL | pip 装到了另一个解释器 | 设 `DSH_CU_PYTHON` 指向装包的那个 |
| `pip` 报 `No module named pip` | 解释器是精简版/嵌入式 | 换完整安装版，或 `-m ensurepip` |
| 跑 `python` 弹微软商店 | PATH 里是 `WindowsApps` 占位符 | 用绝对路径，或关掉"应用执行别名"里的 python |
| `import win32api` 失败 | pywin32 的 dll 没注册 | `python -m pywin32_postinstall -install` |
| 工具没出现 | bundle 没装上 / 没重启 | `plugin_manager list_plugins` 确认，再重启 DSH |
| `activate` 提示 `CU_NO_OVERLAY` | 提示层起不来（常见于缺 pillow） | 先修 pillow，再看 `<DSH_CU_HOME>\overlay.err` |

---

## 8 边界与礼貌

- 不要为了装依赖去改系统环境变量、注册表、全局代理；只动用户目录与 `--user` 包。
- 不要改 DSH 自己的代码或 profile 文件；安装只走 `plugin_manager`。
- 每一步失败就把**原文报错**贴给用户，不要自己编一个"应该好了"。
- 装完把"哪些必须项绿了、哪些可选项没绿"如实报给用户——可选项失败不算失败。
