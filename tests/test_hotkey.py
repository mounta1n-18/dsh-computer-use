# -*- coding: utf-8 -*-
"""真机验证：急停热键（Ctrl+Alt+Shift+Backspace）与暂停热键（Ctrl+Shift+Alt）。

⚠️ 这个测试会往真实桌面注入全局按键：如果此时用户自己的 DSH 会话正开着 cu，
按键会直接打到那个会话上（会把人家急停掉）。所以默认不跑，必须显式开：

    set DSH_CU_ALLOW_REAL_HOTKEY_TEST=1
    python tests/test_hotkey.py

平时不要放进 run_all，也不要在用户正在用电脑时跑。
"""
import ctypes
import os
import subprocess
import sys
import tempfile
import time

if os.environ.get("DSH_CU_ALLOW_REAL_HOTKEY_TEST") != "1":
    print("SKIP：真机热键测试会注入全局按键，需 DSH_CU_ALLOW_REAL_HOTKEY_TEST=1 才跑")
    raise SystemExit(0)

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python")
sys.path.insert(0, HERE)
import cu_server  # noqa: E402

# 热键是"整台机器的"：只要线上还有一个 cu_server 在跑，它也会收到这些按键。
# 曾经因为这条把用户正在用的会话急停了好几次，所以线上有服务时直接拒绝跑。
if cu_server.ping(cu_server.TransportConfig.from_env()):
    print("SKIP：线上 cu 正在运行，注入热键会打断用户；先停掉它再跑这个测试")
    raise SystemExit(0)

HOME = tempfile.mkdtemp(prefix="cu-hot-")
ENDPOINT = r"\\.\pipe\dsh-cu-hot-%d" % os.getpid()
ENV = dict(os.environ)
ENV.update({"DSH_CU_ENDPOINT": ENDPOINT, "DSH_CU_HOME": HOME,
            "DSH_CU_CONSENT": "auto", "DSH_CU_TEST": "1"})
os.environ.update(ENV)
cfg = cu_server.TransportConfig.from_env()

u = ctypes.WinDLL("user32")
KEYEVENTF_KEYUP = 0x0002
VK = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "back": 0x08}


def combo(down=True, hold=0.25):
    order = ["ctrl", "alt", "shift", "back"] if down else ["back", "shift", "alt", "ctrl"]
    for name in order:
        vk = VK[name]
        u.keybd_event(vk, 0, 0 if down else KEYEVENTF_KEYUP, 0)
        time.sleep(0.03)
    if down:
        # 四个键同时按住的窗口太短（30ms）会被 30ms 的轮询错过；真人不会这么快。
        time.sleep(hold)


def pause_chord(down=True, hold=0.55):
    """只按 Ctrl+Alt+Shift（不带第四个键），按住 hold 秒触发暂停。"""
    order = ["ctrl", "alt", "shift"] if down else ["shift", "alt", "ctrl"]
    for name in order:
        u.keybd_event(VK[name], 0, 0 if down else KEYEVENTF_KEYUP, 0)
        time.sleep(0.03)
    if down:
        time.sleep(hold)


srv = subprocess.Popen([sys.executable, os.path.join(HERE, "cu_server.py")],
                       env=ENV, cwd=HERE, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True, encoding="utf-8")
ok = True


def expect(label, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + label)
    ok = ok and cond


try:
    for _ in range(50):
        if cu_server.ping(cfg):
            break
        time.sleep(0.2)
    c = cu_server.Client(cfg)
    r, _ = c.request("activate", owner="热键测试")
    expect("activate ok", r.get("ok"))
    r, _ = c.request("control")
    expect("active", (r.get("value") or {}).get("control", {}).get("active") is True)

    combo(True)
    time.sleep(0.2)
    combo(False)
    time.sleep(0.5)
    r, _ = c.request("control")
    st = (r.get("value") or {}).get("control", {})
    expect("emergency tripped", st.get("emergency") is True)
    expect("lease revoked", st.get("active") is False)

    r, _ = c.request("cmd", argv=["focus", "__no_such_window__"])
    expect("input blocked by emergency", r.get("code") == "CU_EMERGENCY_STOP")

    combo(True)
    time.sleep(0.2)
    combo(False)
    time.sleep(0.5)
    r, _ = c.request("control")
    st = (r.get("value") or {}).get("control", {})
    expect("emergency cleared", st.get("emergency") is False)
    expect("still inactive after clear", st.get("active") is False)

    r, _ = c.request("activate", owner="热键测试")
    expect("re-activate ok", r.get("ok"))

    # 暂停热键：只按 Ctrl+Alt+Shift，按住 0.55 秒
    pause_chord(True)
    pause_chord(False)
    time.sleep(0.4)
    r, _ = c.request("control")
    st = (r.get("value") or {}).get("control", {})
    expect("pause hotkey paused", st.get("paused") is True)
    r, _ = c.request("cmd", argv=["focus", "__no_such_window__"])
    expect("action blocked on pause", r.get("code") == "CU_PAUSED")

    pause_chord(True)
    pause_chord(False)
    time.sleep(0.4)
    r, _ = c.request("control")
    st = (r.get("value") or {}).get("control", {})
    expect("pause hotkey resumes", st.get("paused") is False)
    expect("lease kept across pause", st.get("active") is True)

    c.request("deactivate", reason="done")
    c.request("shutdown")
    c.close()
finally:
    try:
        srv.wait(timeout=10)
    except subprocess.TimeoutExpired:
        srv.kill()
    print("log tail:", (srv.stdout.read() or "")[-600:])
print("RESULT:", "ALL PASS" if ok else "FAILURES")

