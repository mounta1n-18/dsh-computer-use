"""`ask`：模型主动向用户要东西时，cu 必须"暂停但活着"。

约定（用户 2026-11-15）：
- ask 之后 cu 处于暂停：动的动作被 CU_PAUSED 拒，但控制权还在、服务还在；
- 用户答完（留言）自动恢复，agent 下一次动作放行，并收到"cu 已恢复，用户有新指令：…"；
- 暂停期间再点鲸鱼（测试缝 logo_click）等于恢复。

不需要提示层：DSH_CU_OVERLAY=0。
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "python"))
import cu_server as S  # noqa: E402

PY = sys.executable
HOME = os.path.join(os.environ.get("TEMP", "."), "cu-ask-test")
results = []


def check(name, ok, extra=""):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (("  " + extra) if extra else ""))


def start(ep, home):
    env = dict(os.environ)
    env.update({
        "DSH_CU_ENDPOINT": ep,
        "DSH_CU_TRANSPORT": "named_pipe",
        "DSH_CU_HOME": home,
        "DSH_CU_CONSENT": "auto",
        "DSH_CU_OVERLAY": "0",
        "DSH_CU_STAY_ALIVE": "1",
        "DSH_CU_TEST": "1",
        "PYTHONUTF8": "1",
    })
    p = subprocess.Popen([PY, os.path.join(HERE, "..", "python", "cu_server.py")],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cfg = S.TransportConfig("named_pipe", ep, 0)
    c = S.Client(cfg)
    for _ in range(100):
        try:
            c.connect()
            return p, c, cfg
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("服务没起来")


def main():
    os.makedirs(HOME, exist_ok=True)
    for f in os.listdir(HOME):
        try:
            os.remove(os.path.join(HOME, f))
        except OSError:
            pass
    ep = r"\\.\pipe\dsh-cu-ask-" + str(os.getpid())
    p, c, cfg = start(ep, HOME)
    try:
        r, _ = c.request("activate", owner="sess-ask", idle_ttl=600)
        check("激活成功", r.get("ok"), str(r.get("code") or ""))

        r, _ = c.request("ask", text="需要你输入这页的登录密码")
        v = r.get("value") or {}
        check("ask 成功", r.get("ok") and v.get("paused") is True, str(r.get("code") or ""))

        r, _ = c.request("control")
        ctl = r["value"]["control"]
        check("ask 后处于暂停但仍有控制权", ctl["paused"] is True and ctl["active"] is True)

        r, _ = c.request("cmd", argv=["focus", "__no_such_window__"])
        check("暂停期间动作被 CU_PAUSED 拦", (r.get("code") == "CU_PAUSED"), str(r.get("code")))

        r, _ = c.request("control", action="message", text="密码是 1234")
        ctl = (r.get("value") or {}).get("control", {})
        # 2026-11-18 规则：用户说完话仍然暂停，agent 必须 reply 才能继续
        check("用户回答后仍暂停（等 agent 回话）",
              ctl.get("paused") is True and ctl.get("pending_reply") is True)

        r, _ = c.request("cmd", argv=["focus", "__no_such_window__"])
        check("回话前动作被 CU_UNANSWERED 拦",
              r.get("code") == "CU_UNANSWERED", str(r.get("code")))

        r, _ = c.request("reply", text="收到密码，继续")
        check("reply 之后解除暂停",
              (r.get("value") or {}).get("control", {}).get("paused") is False)

        r, _ = c.request("cmd", argv=["focus", "__no_such_window__"])
        check("恢复后动作放行（CU_NO_WINDOW 而非 CU_PAUSED）",
              r.get("code") == "CU_NO_WINDOW", str(r.get("code")))
        note = r.get("notice") or ""
        check("agent 收到用户回答", "1234" in note, note[:60])

        r, _ = c.request("ask", text="再确认一次")
        c.request("control", action="logo_click")
        r2, _ = c.request("control")
        check("再点鲸鱼只开留言框、不解除暂停",
              r2["value"]["control"]["paused"] is True)

        r, _ = c.request("ask", text="")
        check("缺 text 报 CU_BAD_ARG", r.get("code") == "CU_BAD_ARG", str(r.get("code")))

        # 先确保处于"恢复"状态，再测暂停/恢复两条提示（前面点过鲸鱼，可能还暂停着）
        c.request("resume")
        c.request("control", action="pause")
        r, _ = c.request("control")
        check("暂停时 agent 收到提示", "用户已暂停 cu" in (r.get("notice") or ""),
              (r.get("notice") or "")[:40])
        c.request("control", action="pause")
        r, _ = c.request("control")
        check("恢复时 agent 收到提示", "cu 已恢复，观察屏幕" in (r.get("notice") or ""),
              (r.get("notice") or "")[:40])

        c.request("deactivate", reason="test-done")
    finally:
        try:
            c.request("shutdown")
        except Exception:
            pass
        try:
            p.wait(timeout=8)
        except Exception:
            p.kill()

    ok = all(v for _, v in results)
    print("\nRESULT:", "ALL PASS" if ok else "FAILED")
    print("  %d/%d" % (sum(1 for _, v in results if v), len(results)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
