"""在**隔离桌面**上启动 cu_server 的启动器（由 shadow.py 用 lpDesktop 拉起）。

为什么需要它：`CreateProcessW` 的 `lpDesktop` 只有我们自己调 ctypes 才可靠——
实测 `subprocess.STARTUPINFO.lpDesktop` 被忽略，子进程仍落在 `Default` 桌面。
这个启动器本身就在隔离桌面上，于是它 spawn 的一切（包括 UIA sidecar）都继承该桌面，
现有 cu.py / cu_server.py **一行都不用改**。
"""
import json
import os
import runpy
import sys

# 配置走**文件**而不是命令行：JSON 里的双引号会被 CreateProcessW 的命令行
# 解析吃掉（实测第一版就是这么挂的）。
cfg_path = sys.argv[1]
with open(cfg_path, encoding="utf-8") as f:
    cfg = json.load(f)
os.environ.update(cfg.get("env") or {})
here = os.path.dirname(os.path.abspath(__file__))
if here not in sys.path:
    sys.path.insert(0, here)
runpy.run_path(os.path.join(here, "cu_server.py"), run_name="__main__")
