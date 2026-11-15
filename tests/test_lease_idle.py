"""待命语义：空闲会收权，但"用户有交互"要续期。

场景（实测踩过）：agent 激活后停下等指令，用户在聊天里慢慢打字/发留言，
如果只按"没有 cu 调用"计时，控制权会在用户打字期间被收走，连命令都来不及下。

这里验三件事：
1. 激活后一直没动静 → 空闲到期收权；
2. 到期前用户发一条留言 → 空闲计时被续上，租约还在；
3. 暂停期间（用户正在对话）不按空闲收权。

无窗口：DSH_CU_OVERLAY=0。默认 SKIP，需要 DSH_CU_ALLOW_LEASE_TEST=1。
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
FAILED = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (("  " + extra) if extra and not ok else ""))
    if not ok:
        FAILED.append(name)


def main():
    if os.environ.get("DSH_CU_ALLOW_LEASE_TEST") != "1":
        print("SKIP 设置 DSH_CU_ALLOW_LEASE_TEST=1 才跑（会起一个独立服务）")
        return 0

    home = os.path.join(os.environ.get("TEMP", "."), "cu-lease-%d" % os.getpid())
    os.makedirs(home, exist_ok=True)
    ep = r"\\.\pipe\cu-lease-%d" % os.getpid()
    env = dict(os.environ, DSH_CU_ENDPOINT=ep, DSH_CU_HOME=home,
               DSH_CU_CONSENT="auto", DSH_CU_OVERLAY="0",
               DSH_CU_STAY_ALIVE="1", DSH_CU_TEST="1")
    srv = subprocess.Popen([PY, os.path.join(HERE, "cu_server.py")], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=0x08000000)
    cfg = cu_server.TransportConfig.from_env()
    cfg.endpoint = ep
    try:
        time.sleep(1.5)

        def call(op, **kw):
            c = cu_server.Client(cfg)
            try:
                return c.request(op, **kw)[0]
            finally:
                c.close()

        r = call("activate", owner="lease-test", idle_ttl=3, max_ttl=600)
        check("激活成功", bool(r.get("ok")), json.dumps(r)[:200])

        # 1) 用户留言应当续期：2 秒时发一条，再等 2 秒（已超过 3 秒空闲窗口的一半）
        time.sleep(2.0)
        call("control", action="message", text="继续，等我下一步")
        time.sleep(2.0)
        st = call("control").get("value", {}).get("control", {})
        check("留言后续期，租约还在", bool(st.get("active")), json.dumps(st)[:200])

        # 2) 暂停期间不收权（用户正在对话）
        call("control", action="pause")
        time.sleep(4.0)
        st = call("control").get("value", {}).get("control", {})
        check("暂停期间不因空闲收权", bool(st.get("active")) and bool(st.get("paused")),
              json.dumps(st)[:200])
        call("control", action="pause")
        time.sleep(0.5)

        # 3) 恢复后一直没交互 → 空闲到期收权
        time.sleep(4.5)
        st = call("control").get("value", {}).get("control", {})
        check("停止交互后空闲收权", not st.get("active"), json.dumps(st)[:200])
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()

    print("RESULT: " + ("ALL PASS" if not FAILED else "FAILED " + ", ".join(FAILED)))
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
