# -*- coding: utf-8 -*-
"""控制权状态机测试：跑在独立端点 + 独立 HOME 上，不碰正在使用的服务。"""
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python")
sys.path.insert(0, HERE)
import cu_server  # noqa: E402

HOME = tempfile.mkdtemp(prefix="cu-test-home-")
ENDPOINT = r"\\.\pipe\dsh-cu-test-%d" % os.getpid()
ENV = dict(os.environ)
ENV.update({
    "DSH_CU_ENDPOINT": ENDPOINT,
    "DSH_CU_HOME": HOME,
    "DSH_CU_CONSENT": "auto",
    "DSH_CU_TEST": "1",
    "DSH_CU_STAY_ALIVE": "1",   # 状态机测试要连续断言，别让交还/急停把服务关了
    "DSH_CU_OVERLAY": "0",      # 不弹窗：这些用例只验状态机，别打扰正在用电脑的人
    "DSH_CU_IDLE_TTL": "120",
})
for k, v in ENV.items():
    os.environ[k] = v

cfg = cu_server.TransportConfig.from_env()
assert cfg.endpoint == ENDPOINT, cfg.endpoint

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
    p = wait_ping()
    check("server up", bool(p))
    c = cu_server.Client(cfg)

    def req(op, **kw):
        resp, _ = c.request(op, **kw)
        return resp

    r = req("ping")["value"]
    check("control enabled", r["control"]["enabled"] is True, r["control"])
    check("not active", r["control"]["active"] is False)
    check("hotkey polling", r["hotkeys_ok"] if "hotkeys_ok" in r else True)

    r = req("cmd", argv=["focus", "__no_such_window__"])
    check("action blocked before activate", r.get("code") == "CU_NOT_ACTIVATED", r)

    r = req("cmd", argv=["shot"])
    check("shot allowed before activate", r.get("ok") is True, r.get("code"))

    r = req("activate", owner="agent-A")
    check("activate auto", r.get("ok") and r["value"]["activated"], r.get("code"))
    lease = r["value"]["lease_id"]

    r = req("eval", code="cu._CONTROL.check(); print('ok')")
    check("gate passes after activate", r.get("ok") and "ok" in r["value"]["stdout"], r)

    r = req("activate", owner="agent-A")
    check("same owner reuses", r.get("ok") and r["value"].get("reused"), r.get("code"))

    r = req("activate", owner="agent-B")
    check("other owner busy", r.get("code") == "CU_BUSY" and "agent-A" in r.get("message", ""), r)

    r = req("control")
    v = r["value"]
    check("status owner", v["control"]["owner"] == "agent-A", v["control"])

    r = req("deactivate", reason="done")
    check("deactivate", r.get("ok") and r["value"]["deactivated"], r)
    # 交还之后 hold 必须报出"为什么没了"，不能让模型拿激活文案里的空闲秒数当答案。
    w = req("wait", seconds=1)["value"]
    check("wait reports deactivate reason",
          w.get("active") is False and w.get("reason") == "deactivate" and "交还" in w.get("detail", ""),
          w.get("reason") or w)
    r = req("cmd", argv=["focus", "__no_such_window__"])
    check("action blocked after deactivate", r.get("code") == "CU_NOT_ACTIVATED", r)

    # 急停
    r = req("activate", owner="agent-A")
    check("reactivate", r.get("ok"), r.get("code"))
    r = req("control", action="trip")
    check("trip", r.get("ok") and r["value"]["control"]["emergency"], r)
    # 用户按急停不是"闲置"：wait 必须报 emergency，detail 里也不能出现空闲字样。
    w = req("wait", seconds=1)["value"]
    check("wait reports emergency reason",
          w.get("reason") == "emergency" and "急停" in w.get("detail", "")
          and "空闲" not in w.get("detail", ""),
          w.get("reason") or w)
    r = req("cmd", argv=["focus", "__no_such_window__"])
    check("action blocked on emergency", r.get("code") == "CU_EMERGENCY_STOP", r)
    r = req("activate", owner="agent-A")
    check("activate blocked on emergency", r.get("code") == "CU_EMERGENCY_STOP", r)
    r = req("cmd", argv=["release"])
    check("release allowed on emergency", r.get("ok") is True, r.get("code"))
    r = req("control", action="clear")
    check("clear emergency", r.get("ok") and not r["value"]["control"]["emergency"], r)
    r = req("activate", owner="agent-A")
    check("activate after clear", r.get("ok"), r.get("code"))

    # 用户插手：不通报、不暂停注入（2026-11-15 用户要求），但投递机制仍要能工作。
    # 这里用"用户留言"当那条通知，验证 notice + ids + ack 之后清空。
    r = req("activate", owner="agent-A")
    req("control", action="message", text="这句话只用来验证投递")
    r = req("cmd", argv=["shot"])
    check("notice delivered", "这句话只用来验证投递" in (r.get("notice") or ""), r.get("notice"))
    # 确认投递（ack 之后才删；不 ack 会一直重发）
    check("notice has ids", bool(r.get("notice_ids")), r.get("notice_ids"))
    req("ping", ack_notice=r.get("notice_ids"))
    r = req("control")
    check("notice cleared after ack", "这句话只用来验证投递" not in (r.get("notice") or ""), r.get("notice"))
    # 回了话才能收工（2026-11-18 规则：待回复时 deactivate 会被 CU_UNANSWERED 挡）
    r = req("deactivate", reason="before-reply")
    check("deactivate blocked while pending", r.get("code") == "CU_UNANSWERED", r.get("code"))
    req("reply", text="")
    # 空闲过期
    req("deactivate", reason="test-expiry")
    r = req("activate", owner="agent-A", idle_ttl=2)
    check("activate with idle_ttl=2", r.get("ok"), r.get("code"))
    time.sleep(3.5)
    r = req("cmd", argv=["focus", "__no_such_window__"])
    check("action after idle expiry", r.get("code") in ("CU_LEASE_EXPIRED", "CU_NOT_ACTIVATED"), r)
    r = req("control")
    check("lease cleared by ticker", r["value"]["control"]["active"] is False, r["value"]["control"])

    # ---- 暂停：保留租约但不注入；恢复后继续 ----
    req("activate", owner="agent-P", idle_ttl=120)
    r = req("control", action="pause")
    check("pause toggles on", r.get("ok") and r["value"]["control"]["paused"] is True, r)
    r = req("cmd", argv=["focus", "__no_such_window__"])
    check("action blocked on pause", r.get("code") == "CU_PAUSED", r.get("code"))
    r = req("control")
    check("lease kept while paused", r["value"]["control"]["active"] is True, r["value"]["control"])
    # 暂停期间空闲再久也不收权
    time.sleep(3.0)
    r = req("control")
    check("paused lease not idle-expired", r["value"]["control"]["active"] is True, r["value"]["control"])
    r = req("control", action="pause")
    check("pause toggles off", r.get("ok") and r["value"]["control"]["paused"] is False, r)
    r = req("cmd", argv=["focus", "__no_such_window__"])
    # 闸门放行 = 不再是 CU_PAUSED；这个窗口本来就不存在，CU_NO_WINDOW 是对的
    check("action works after resume", r.get("ok") is True or r.get("code") == "CU_NO_WINDOW", r.get("code"))

    # ---- 点鲸鱼：暂停 + 留言，留言挂在下一条结果上 ----
    r = req("control", action="logo_click")
    check("logo click pauses", r.get("ok") and r["value"]["control"]["paused"] is True, r)
    r = req("cmd", argv=["focus", "__no_such_window__"])
    check("blocked while chatting", r.get("code") == "CU_PAUSED", r.get("code"))
    r = req("control", action="message", text="别点右键，用键盘")
    check("message keeps paused", r.get("ok") and r["value"]["control"]["paused"] is True, r)
    r = req("control")
    check("message reaches agent", "别点右键" in (r.get("notice") or ""), r.get("notice"))
    # 不 ack 就重发（至少一次投递）
    r2 = req("control")
    check("notice redelivered until ack", "别点右键" in (r2.get("notice") or ""), r2.get("notice"))
    # 工具侧的话必须工具侧答：reply 之后才恢复
    r = req("reply", text="好，只用键盘")
    check("reply resumes", r.get("ok") and r["value"]["control"]["paused"] is False, r)
    req("ping", ack_notice=r2.get("notice_ids"))
    r3 = req("control")
    check("message cleared after ack", "别点右键" not in (r3.get("notice") or ""), r3.get("notice"))

    # ---- 防积压：新一轮接管不许继承上一轮的留言/提示 ----
    # （退回合闸门已改到 JS 侧 steer，Python 不再有 guard；这里只验服务端的清场）
    req("deactivate", reason="prep-carryover")
    r = req("activate", owner="agent-B")
    check("carryover: activate", r.get("ok"), r.get("code"))
    req("control", action="message", text="这句不能留到下一轮")
    r = req("deactivate", reason="blocked-by-unanswered")
    check("未回复不许收工", r.get("code") == "CU_UNANSWERED", r.get("code"))
    req("reply", text="")
    r = req("deactivate", reason="cleanup-carryover")
    check("deactivate after reply", r.get("ok"), r.get("code"))
    r = req("activate", owner="agent-C")
    check("activate after carryover", r.get("ok"), r.get("code"))
    c3 = req("control")["value"]
    check("no carryover into next cu", not (c3.get("notice") or "").strip(), c3.get("notice"))

    # ---- 急停：解除后必须彻底静默（不激活、不弹提示） ----
    req("control", action="trip")
    r = req("control", action="clear")
    check("clear after trip", r.get("ok") and not r["value"]["control"]["emergency"], r)
    r = req("control")
    check("silent after clear", r["value"]["control"]["active"] is False
          and r["value"]["control"]["paused"] is False, r["value"]["control"])

    req("shutdown")
    c.close()
finally:
    try:
        srv.wait(timeout=8)
    except subprocess.TimeoutExpired:
        srv.kill()
    out = srv.stdout.read() if srv.stdout else ""
    if out.strip():
        print("--- server log ---")
        print(out[-4000:])

print("RESULT:", "ALL PASS" if not fails else "FAILED: %s" % fails)
sys.exit(0 if not fails else 1)





