"""`wait` 长轮询与"cu 关了就清队列"的自测。

两件事必须成立（用户 2026-11-15 的硬要求）：
1. agent 用 action="hold" 挂着时，用户一留言就立刻带着原话返回；
2. cu 关闭（含手动关闭 / 收尾 / 空闲超时）后，未送达的留言不能再留到下一次，
   否则新会话一激活就被上一轮的旧指令干扰。

不需要提示层：DSH_CU_OVERLAY=0。
"""
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "python"))
import cu_server as S  # noqa: E402

PY = sys.executable
HOME = os.path.join(os.environ.get("TEMP", "."), "cu-hold-test")
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
        "DSH_CU_STAY_ALIVE": "0",
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
    ep = r"\\.\pipe\dsh-cu-hold-" + str(os.getpid())
    p, c, cfg = start(ep, HOME)
    side = S.Client(cfg)
    side.connect()

    c.request("activate", owner="sess-hold", idle_ttl=600)
    threading.Timer(1.2, lambda: side.request("control", action="message", text="先写下周三的会")).start()
    t0 = time.time()
    v = c.request("wait", seconds=10)[0]["value"]
    dt = time.time() - t0
    check("留言一到，wait 立刻返回", dt < 5 and [i["text"] for i in v["items"]] == ["先写下周三的会"], "%.1fs" % dt)
    ids = [i["id"] for i in v["items"]]
    c.request("messages", ack=ids)
    check("取走后队列清空", c.request("messages")[0]["value"]["items"] == [])

    # 没留言时按秒数超时返回，不提前也不空转
    t0 = time.time()
    v2 = c.request("wait", seconds=2)[0]["value"]
    check("无留言时按超时返回", 1.5 < time.time() - t0 < 6 and v2["items"] == [])

    # cu 关掉 → 队列清空、盘上也不留
    side.request("control", action="message", text="这条不许留到下一次")
    inbox = os.path.join(HOME, "inbox.json")
    check("留言已落盘", os.path.exists(inbox))
    c.request("deactivate", reason="test")
    for _ in range(80):
        if p.poll() is not None:
            break
        time.sleep(0.1)
    check("交还后服务退出", p.poll() is not None)
    left = json.load(open(inbox, encoding="utf-8")) if os.path.exists(inbox) else []
    check("盘上不残留用户指令", all("【用户" not in str(n.get("text", "")) for n in left), str(left))

    p2, c2, _ = start(ep, HOME)
    try:
        check("新实例读不到旧指令", c2.request("messages")[0]["value"]["items"] == [])
    finally:
        p2.terminate()

    print("\nRESULT:", "ALL PASS" if all(ok for _, ok in results) else "FAILED")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
