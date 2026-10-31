"""演示：在**隔离桌面**里打开计算器 → 用元素级点 6 × 7 = → 截图放到用户的桌面。

全程走 shadow 端点（等价于工具里 desktop:"shadow"），并断言**用户光标一动没动**。
"""

import ctypes
import json
import os
import sys
import tempfile
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PLUGIN, "python"))

import cu_server  # noqa: E402
import shadow  # noqa: E402

DESKTOP_DIR = os.path.join(os.environ.get("USERPROFILE") or tempfile.gettempdir(), "Desktop")
CALC = os.path.join(PLUGIN, "scripts", "shadow-calc.ps1")


def cursor():
    p = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


def main():
    shadow.start_session()
    st = shadow._read_state()
    old = {k: os.environ.get(k) for k in ("DSH_CU_ENDPOINT", "DSH_CU_TRANSPORT")}
    os.environ["DSH_CU_ENDPOINT"] = st["endpoint"]
    os.environ["DSH_CU_TRANSPORT"] = "named_pipe"
    cfg = cu_server.TransportConfig.from_env()
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    c = cu_server.Client(cfg)

    def call(argv):
        r, _ = c.request("cmd", argv=[str(x) for x in argv])
        v = r.get("value") or {}
        text = "\n".join(v.get("lines") or []) if isinstance(v, dict) else json.dumps(v, ensure_ascii=False)
        return r.get("ok"), r.get("code") or "", text

    cur0 = cursor()
    print("[0] 用户光标起点：%s" % (cur0,))

    # 1) 在隔离桌面里起计算器（spawn 由 shadow 服务执行 → 子进程继承隔离桌面）
    ok, code, text = call(["spawn", "powershell.exe", "-NoProfile",
                           "-ExecutionPolicy", "Bypass", "-File", CALC])
    print("[1] 起计算器：%s %s" % (ok, text.strip()[:90]))
    time.sleep(4)

    ok, code, text = call(["windows"])
    print("[2] 隔离桌面窗口：\n" + text[:400])
    if "Shadow Calculator" not in text:
        print("!! 计算器没起来，停止")
        return 1

    # 2) 元素级：找到按钮并依次 Invoke（不走鼠标）
    ok, code, text = call(["state-capture", "--window", "Shadow Calculator",
                           "--depth", "6", "--limit", "60"])
    print("[3] 拿到元素：%s" % text.splitlines()[0][:90])
    refs = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].startswith("e") and parts[1] == "Button":
            refs.setdefault(parts[2], parts[0])
    print("    按钮映射：%s" % {k: refs[k] for k in ("6", "*", "7", "=") if k in refs})
    for name in ("6", "*", "7", "="):
        if name not in refs:
            print("!! 找不到按钮 %r" % name)
            return 1
        ok, code, text = call(["element", refs[name], "--invoke"])
        if not ok:
            print("!! 点击 %r 失败：%s %s" % (name, code, text[:80]))
            return 1
        time.sleep(0.4)
    print("[4] 已按顺序 Invoke：6 → * → 7 → =")

    # 3) 校验：窗口里显示的是 42
    shown = ""
    p = os.path.join(os.environ.get("TEMP", "."), "dsh-shadow-calc-display.txt")
    for _ in range(10):
        try:
            shown = open(p, encoding="utf-8").read().strip()
        except OSError:
            shown = ""
        if shown == "42":
            break
        time.sleep(0.3)
    print("[5] 计算器显示：%r  → %s" % (shown, "正确 ✅" if shown == "42" else "不对 ❌"))

    # 4) 截图并放到用户桌面
    shot = os.path.join(DESKTOP_DIR, "dsh-shadow-计算器-6x7.png")
    ok, code, text = call(["shot", "--window", "Shadow Calculator", "--long", "900",
                           "--out", shot])
    print("[6] 截图：%s %s" % (ok, text.strip()[:120]))

    # 5) 用户桌面有没有被打扰
    cur1 = cursor()
    print("[7] 用户光标终点：%s → %s" % (cur1, "没动 ✅" if cur0 == cur1 else "动了 ❌"))
    c.close()
    print("\n桌面文件：%s （%d 字节）"
          % (shot, os.path.getsize(shot) if os.path.exists(shot) else -1))
    return 0 if shown == "42" and cur0 == cur1 and os.path.exists(shot) else 1


if __name__ == "__main__":
    sys.exit(main())
