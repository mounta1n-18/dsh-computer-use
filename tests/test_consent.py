# -*- coding: utf-8 -*-
"""真机验证：用户同意路径（横幅上屏 + 30 秒默认同意）。"""
import os
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python")
sys.path.insert(0, HERE)
import cu_server  # noqa: E402

HOME = tempfile.mkdtemp(prefix="cu-consent-")
ENDPOINT = r"\\.\pipe\dsh-cu-consent-%d" % os.getpid()
ENV = dict(os.environ)
ENV.update({"DSH_CU_ENDPOINT": ENDPOINT, "DSH_CU_HOME": HOME,
            "DSH_CU_CONSENT": "user", "DSH_CU_TEST": "1"})
os.environ.update(ENV)
cfg = cu_server.TransportConfig.from_env()

srv = subprocess.Popen([sys.executable, os.path.join(HERE, "cu_server.py")],
                       env=ENV, cwd=HERE, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True, encoding="utf-8")
try:
    for _ in range(50):
        if cu_server.ping(cfg):
            break
        time.sleep(0.2)
    c = cu_server.Client(cfg)
    box = {}

    def act():
        resp, _ = c.request("activate", owner="验收测试", timeout=50)
        box["resp"] = resp

    th = threading.Thread(target=act, daemon=True)
    th.start()
    time.sleep(3.0)
    from PIL import ImageGrab
    img = ImageGrab.grab(all_screens=True)
    out = os.path.join(tempfile.gettempdir(), "_consent_banner.png")
    img.save(out)
    print("screenshot:", out, img.size)
    # 点“同意”按钮（横幅居中：x=(2560-760)/2+412, y=12+126+17）
    import ctypes
    u = ctypes.WinDLL("user32")
    u.SetCursorPos(920 + 414 + 75, 12 + 116 + 17)
    time.sleep(0.3)
    u.mouse_event(0x0002, 0, 0, 0, 0)
    u.mouse_event(0x0004, 0, 0, 0, 0)
    print("clicked agree button")
    th.join(45)
    r = box.get("resp") or {}
    print("activate resp:", r.get("ok"), r.get("code"), (r.get("value") or {}).get("waited"))
    time.sleep(1.2)
    from PIL import ImageGrab as _IG
    _IG.grab(all_screens=True).save(os.path.join(tempfile.gettempdir(), "_consent_active.png"))
    resp, _ = c.request("control")
    print("control:", resp.get("value", {}).get("control"))
    c.request("deactivate", reason="test done")
    c.request("shutdown")
    c.close()
finally:
    try:
        srv.wait(timeout=10)
    except subprocess.TimeoutExpired:
        srv.kill()
    print("server log tail:", (srv.stdout.read() or "")[-800:])




