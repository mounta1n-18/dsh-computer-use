# -*- coding: utf-8 -*-
"""工具侧留言 → 必须工具侧答复（reply）才能动键鼠 / 才能收工。

用户 2026-11-18 的规则：
- 用户在递话框里说的话，只能由 agent 用 reply 回；回之前不许动键鼠，
  也不许用 deactivate 一走了之。
- reply 允许空字符串：等于"看过了，不用回"，照样解除暂停，但不给用户弹窗，
  只在审计/日志里留一条记录。
"""
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python")
sys.path.insert(0, HERE)
import cu_server  # noqa: E402

HOME = tempfile.mkdtemp(prefix="cu-reply-home-")
ENDPOINT = r"\\.\pipe\dsh-cu-reply-%d" % os.getpid()
ENV = dict(os.environ)
ENV.update({
    "DSH_CU_ENDPOINT": ENDPOINT,
    "DSH_CU_HOME": HOME,
    "DSH_CU_CONSENT": "auto",
    "DSH_CU_TEST": "1",
    "DSH_CU_STAY_ALIVE": "1",
    "DSH_CU_OVERLAY": "0",      # 不弹任何窗口
})
for k, v in ENV.items():
    os.environ[k] = v

cfg = cu_server.TransportConfig.from_env()
srv = subprocess.Popen([sys.executable, os.path.join(HERE, "cu_server.py")],
                       env=ENV, cwd=HERE, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True, encoding="utf-8")

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (("  " + str(extra)) if extra else ""))
    if not cond:
        fails.append(name)


def wait_ping(timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        p = cu_server.ping(cfg)
        if p:
            return p
        time.sleep(0.2)
    return None


try:
    check("server up", bool(wait_ping()))
    c = cu_server.Client(cfg)

    def req(op, **kw):
        resp, _ = c.request(op, **kw)
        return resp

    r = req("activate", owner="sess-reply", intro="回复闸门测试", intensity="script_main")
    check("activate", r.get("ok") and r["value"]["activated"], r.get("code"))
    check("intro shown", r["value"].get("intro") == "回复闸门测试", r["value"].get("intro"))

    # 用户留言 → 必须回复
    r = req("control", action="message", text="你卡住了吗")
    check("message accepted", r.get("ok"), r.get("code"))
    r = req("control")
    v = r["value"]["control"]
    check("pending_reply set", v.get("pending_reply") is True, v)
    check("still paused", v.get("paused") is True, v)

    r = req("cmd", argv=["focus", "__no_such_window__"])
    check("action blocked until reply", r.get("code") == "CU_UNANSWERED", r.get("code"))

    r = req("deactivate", reason="想跑")
    check("deactivate blocked until reply", r.get("code") == "CU_UNANSWERED", r.get("code"))

    r = req("resume")
    check("resume blocked until reply", r.get("code") == "CU_UNANSWERED", r.get("code"))

    # 空回复：解除暂停、不弹窗
    r = req("reply", text="")
    check("empty reply accepted", r.get("ok") and r["value"].get("replied"), r.get("code"))
    check("empty reply text", r["value"].get("text") == "", r["value"])
    r = req("control")
    v = r["value"]["control"]
    check("unpaused after empty reply", v.get("paused") is False, v)
    check("pending cleared", v.get("pending_reply") is False, v)
    r = req("cmd", argv=["focus", "__no_such_window__"])
    check("action allowed after reply", r.get("code") in ("CU_NO_WINDOW", "CU_BAD_ARG"), r.get("code"))

    # 正常回复
    req("control", action="message", text="还在吗")
    r = req("reply", text="在，刚在存文件，这就继续")
    check("reply ok", r.get("ok") and r["value"].get("replied"), r.get("code"))
    check("reply echoes user", "还在吗" in (r["value"].get("to") or ""), r["value"].get("to"))
    r = req("control")
    check("unpaused after reply", r["value"]["control"].get("paused") is False, r["value"]["control"])

    # 待回复时每个结果都带硬提醒
    req("control", action="message", text="提醒测试")
    r = req("cmd", argv=["shot"])
    check("notice carries reminder", "待回复" in (r.get("notice") or ""), r.get("notice"))

finally:
    try:
        srv.terminate()
        srv.wait(timeout=5)
    except Exception:
        srv.kill()

print("RESULT:", "ALL PASS" if not fails else ("FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
