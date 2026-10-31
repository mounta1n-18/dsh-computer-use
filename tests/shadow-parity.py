"""隔离桌面能力矩阵：把每个能力在隔离会话里**真跑一遍并断言**。

不是"我觉得能用"，而是逐项 PASS/FAIL/SKIP + 证据。走的是插件的真实路径
（`cu_server.Client.request('cmd', argv=[...])`），不是本地 CLI，
否则测的是"用户桌面上的 cu.py"，跟隔离无关。

用法：
    python tests/shadow-parity.py            # 跑矩阵（需要先 shadow.py start）
    python tests/shadow-parity.py --keep     # 跑完不关会话
"""

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
PY = os.path.join(PLUGIN, "python")
sys.path.insert(0, PY)

import cu  # noqa: E402
import cu_server  # noqa: E402
import shadow  # noqa: E402

PASS, FAIL, SKIP = [], [], []


def check(name, ok, detail="", mode="PASS"):
    tag = mode if not ok else "PASS"
    if tag == "PASS":
        PASS.append(name)
    elif tag == "SKIP":
        SKIP.append(name)
    else:
        FAIL.append(name)
    print("  [%-4s] %-34s %s" % (tag, name, str(detail)[:110]))


def endpoint_of_session():
    st = shadow._read_state()
    if not st:
        print("没有隔离会话；先 `python python/shadow.py start`")
        sys.exit(2)
    return st["endpoint"], st.get("desktop")


def client_for(endpoint):
    old = {k: os.environ.get(k) for k in ("DSH_CU_ENDPOINT", "DSH_CU_TRANSPORT")}
    os.environ["DSH_CU_ENDPOINT"] = endpoint
    os.environ["DSH_CU_TRANSPORT"] = "named_pipe"
    cfg = cu_server.TransportConfig.from_env()
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return cu_server.Client(cfg)


def call(c, argv, **kw):
    """发一条 cmd，返回 (ok, code, text)。"""
    req = {"argv": [str(x) for x in argv]}
    req.update(kw)
    resp, _binary = c.request("cmd", **req)
    val = resp.get("value") or {}
    lines = val.get("lines") if isinstance(val, dict) else None
    text = "\n".join(lines) if isinstance(lines, list) else json.dumps(val, ensure_ascii=False)
    return bool(resp.get("ok")), resp.get("code") or "", text


def user_state():
    """用户桌面侧的可观测状态：光标 + 前台窗口标题。"""
    u = ctypes.windll.user32
    buf = ctypes.create_unicode_buffer(256)
    u.GetForegroundWindow.restype = wintypes.HWND
    h = u.GetForegroundWindow()
    u.GetWindowTextW(wintypes.HWND(h), buf, 256)
    p = wintypes.POINT()
    u.GetCursorPos(ctypes.byref(p))
    return {"cursor": (p.x, p.y), "foreground": buf.value}


def main():
    endpoint, desktop = endpoint_of_session()
    print("隔离会话：桌面=%r 端点=%s" % (desktop, endpoint))
    c = client_for(endpoint)

    before = user_state()
    print("\n用户桌面基线：光标=%s 前台=%r\n" % (before["cursor"], before["foreground"]))

    print("== A 基础 ==")
    ok, code, text = call(c, ["码"])
    check("A1 错误码表可读", ok and "CU_" in text, text[:40])

    ok, code, text = call(c, ["doctor"])
    check("A2 环境自检（doctor）", ok and "FAIL" not in text, text[:60])

    ok, code, text = call(c, ["where"])
    check("A3 光标位置 where", ok, text[:50])

    print("\n== B 应用与窗口 ==")
    ok, code, text = call(c, ["launch", "记事本", "--timeout", "15"])
    check("B1 launch 应用", ok and "ready" in text, text[:70])
    time.sleep(1.5)

    ok, code, text = call(c, ["windows"])
    has_win = ("Notepad" in text) or ("记事本" in text)
    check("B2 枚举窗口（隔离桌面内）", ok and has_win, text[:70])

    title = "Notepad" if "Notepad" in text else "记事本"
    ok, code, text = call(c, ["shot", "--window", title, "--long", "700"])
    shot_path = text.split("  ")[0].strip() if ok else ""
    size = os.path.getsize(shot_path) if ok and os.path.exists(shot_path) else 0
    check("B3 窗口截图（PrintWindow）", ok and size > 2000,
          "%s %d bytes" % (shot_path, size))
    check("B3b 截图走打印窗口后端", "打印窗口" in text, text[:90])

    print("\n== C 元素级（UIA 必须也落在隔离桌面）==")
    ok, code, text = call(c, ["state-capture", "--window", title, "--limit", "20"])
    check("C1 state_capture 出元素", ok and ("e1" in text), text[:80])
    ref = None
    for tok in text.split():
        if tok.startswith("e") and tok[1:].isdigit():
            ref = tok
            break
    if ref:
        ok2, code2, text2 = call(c, ["element", ref, "--get", "name"])
        check("C2 element --get name", ok2, text2[:70])
        ok3, code3, text3 = call(c, ["element", ref, "--get", "patterns"])
        check("C3 element --get patterns", ok3, text3[:70])
    else:
        check("C2 element --get name", False, "没拿到 ref（自绘界面？）")
        check("C3 element --get patterns", False, "没有 ref")

    ok, code, text = call(c, ["find", "记事本", "--limit", "5"])
    check("C4 find 文字检索", ok or code == "CU_NO_ELEMENT", text[:60])

    print("\n== D 输入（预期：真实注入被系统拒绝）==")
    ok, code, text = call(c, ["click", "500", "500"])
    check("D1 SendInput 点击", ok, "%s %s" % (code, text[:60]),
          mode=("PASS" if ok else "SKIP"))
    ok2, code2, text2 = call(c, ["type", "parity"])
    check("D2 SendInput 输入", ok2, "%s %s" % (code2, text2[:60]),
          mode=("PASS" if ok2 else "SKIP"))

    print("\n== E 剪贴板（同一窗口站内，应互通）==")
    ok, code, text = call(c, ["clip-set", "PARITY-%d" % int(time.time())])
    check("E1 clip-set", ok, text[:50])
    ok, code, text = call(c, ["clip"])
    check("E2 clip 读回", ok and "PARITY-" in text, text[:60])

    print("\n== F 凝视（PrintWindow 路线）==")
    ok, code, text = call(c, ["盯", "--window", title, "--timeout", "5",
                              "--queue", "parity"])
    proc_ok = ok and ("pid=" in text or "已" in text)
    check("F1 凝视能起（不阻塞服务）", proc_ok, text[:70])
    ok2, code2, text2 = call(c, ["瞧", "--queue", "parity"])
    check("F2 瞧 读队列", ok2 or code2 == "CU_NO_QUEUE", text2[:70])

    print("\n== G 多步 / 服务 ==")
    ok, code, text = call(c, ["码"])
    ok, code, text = call(c, ["where"])
    st, _ = c.request("state")
    check("G1 state 报告服务存活", bool(st.get("ok")),
          json.dumps(st.get("value") or {}, ensure_ascii=False)[:70])

    print("\n== H 用户桌面是否被打扰 ==")
    after = user_state()
    check("H1 用户光标未移动", before["cursor"] == after["cursor"],
          "%s → %s" % (before["cursor"], after["cursor"]))
    check("H2 用户前台窗口未改变", before["foreground"] == after["foreground"],
          "%r → %r" % (before["foreground"], after["foreground"]))

    # 收尾：关掉隔离会话里起的记事本（只动隔离桌面）
    try:
        call(c, ["app-terminate", title, "--confirm"])
    except Exception:
        pass
    c.close()

    print("\n" + "=" * 70)
    print("矩阵结果：PASS %d / FAIL %d / SKIP %d"
          % (len(PASS), len(FAIL), len(SKIP)))
    if SKIP:
        print("SKIP（系统物理限制，见下）：%s" % ", ".join(SKIP))
    if FAIL:
        print("FAIL：%s" % ", ".join(FAIL))
    print("=" * 70)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
