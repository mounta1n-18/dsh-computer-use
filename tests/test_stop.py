"""交还/急停之后 cu 必须**彻底退出**（不是退后台）。

用户 2026-11-14 定的语义：
  急停 = 即停即止 → 红边 3 秒 + 告诉 agent → 整个服务关掉；
  agent 交还 = 先把 DSH 切回前台 → 再关掉整个服务。

这个测试开独立端点、**不开** DSH_CU_STAY_ALIVE，所以能真的看到进程退出，
以及下一次调用由管理器重新拉起的新实例。
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python")
sys.path.insert(0, HERE)
import cu_server  # noqa: E402

PY = sys.executable
HOME = os.path.join(os.environ.get("TEMP", "."), "cu-stop-test-%d" % os.getpid())
os.makedirs(HOME, exist_ok=True)
ENDPOINT = r"\\.\pipe\dsh-cu-stop-test-%d" % os.getpid()

ENV = dict(os.environ)
ENV.update({
    "DSH_CU_ENDPOINT": ENDPOINT,
    "DSH_CU_HOME": HOME,
    "DSH_CU_CONSENT": "auto",
    "DSH_CU_TEST": "1",
})
ENV.pop("DSH_CU_STAY_ALIVE", None)

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  " + str(extra)))
    if not cond:
        fails.append(name)


def cfg():
    cfg = cu_server.TransportConfig.from_env()
    cfg.endpoint = ENDPOINT
    return cfg


def start():
    p = subprocess.Popen([PY, os.path.join(HERE, "cu_server.py")], env=ENV,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         creationflags=0x08000000)
    deadline = time.time() + 15
    while time.time() < deadline:
        if p.poll() is not None:
            raise RuntimeError("服务起不来：%s" % (p.stderr.read() or b"")[:400])
        try:
            cu_server.ping(cfg())
            return p
        except Exception:                                  # noqa: BLE001
            time.sleep(0.2)
    raise RuntimeError("服务 15s 没就绪")


def req(op, **kw):
    resp, _png = cu_server.Client(cfg()).request(op, timeout=10, **kw)
    return resp


def wait_exit(p, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if p.poll() is not None:
            return True
        time.sleep(0.1)
    return False


# ---- 1. 交还 → 服务退出 ----
p = start()
r = req("activate", owner="agent-stop")
check("activate", r.get("ok") and r["value"]["activated"], r.get("code"))
r = req("deactivate", reason="test-done")
check("deactivate ok", r.get("ok") and r["value"]["deactivated"], r)
check("deactivate 声明已关闭", r["value"].get("closed") is True, r["value"])
check("交还后进程退出", wait_exit(p, 6), "进程还活着 pid=%s" % p.pid)
marker = os.path.join(HOME, "last_stop.json")
check("不再写 last_stop.json（新实例不继承旧账）", not os.path.exists(marker), marker)

# ---- 2. 新实例：没有控制权，也不许拿上一轮的"为什么关掉"来吓模型 ----
p = start()
r = req("control")
st = r["value"]["control"]
check("新实例未激活", st["active"] is False and st["emergency"] is False, st)
check("新实例不继承停机提示", "电脑控制" not in (r.get("notice") or ""), r.get("notice"))

# ---- 3. 急停 → 红边 3 秒 → 服务退出 ----
r = req("activate", owner="agent-emg")
check("activate again", r.get("ok"), r.get("code"))
r = req("control", action="emergency")
check("emergency 受理", r.get("ok"), r)
time.sleep(0.4)                      # 热键线程刚起步，给它一点时间落状态
check("急停当场阻断", req("cmd", argv=["focus", "__no_such_window__"]).get("code")
      == "CU_EMERGENCY_STOP")
check("急停后 3 秒内还活着（让动画播完）", p.poll() is None)
check("急停动画后进程退出", wait_exit(p, 8), "进程还活着")
check("急停后也不留 last_stop.json", not os.path.exists(marker), marker)

# ---- 4. 再起一个新实例：不许把上一次的急停当成这一次的"关闭" ----
p = start()
r = req("control")
check("新实例不继承急停提示", "手动关闭" not in (r.get("notice") or ""), r.get("notice"))
req("shutdown")
time.sleep(0.5)

print("\nRESULT: " + ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
