"""硬等价验收：屏幕上没有"正在控制"的提示 ⇔ cu 没有在控制这台电脑。

两个方向：
  A. 提示消失（提示层进程没了） → 服务必须在一秒左右自己收工，不能继续握着控制权；
  B. 提示层起不来 → 激活直接被拒（CU_NO_OVERLAY），不允许"无提示地开工"。

A 会真的起提示层（屏幕上会出现边框/鲸鱼 1~2 秒），所以默认跳过，
要跑就显式 DSH_CU_ALLOW_VISUAL_TEST=1。B 不开窗口，永远跑。
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
fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  " + str(extra)))
    if not cond:
        fails.append(name)


# ---------------------------------------------------------------- B：激活前先确认提示层可用
os.environ["DSH_CU_OVERLAY"] = "0"        # 不开窗口：用替身，再把 start() 弄坏
os.environ["DSH_CU_TEST"] = "1"
os.environ.setdefault("DSH_CU_HOME", os.path.join(os.environ.get("TEMP", "."),
                                                  "cu-visual-unit-%d" % os.getpid()))
cfg = cu_server.TransportConfig.from_env()
cfg.endpoint = r"\\.\pipe\cu-visual-unit-%d" % os.getpid()
server = cu_server.Server(cfg)
server.overlay.start = lambda: False      # 模拟"overlay.py 起不来"
try:
    server._activate({"owner": "unit"})
    check("提示层起不来时拒绝激活", False, "居然授权了")
except Exception as e:                    # noqa: BLE001
    code = getattr(e, "code", "")
    check("提示层起不来时拒绝激活", code == "CU_NO_OVERLAY", "实际 %r" % (e,))
server.stopping = True

# ---------------------------------------------------------------- A：提示消失 → 立刻收工
if os.environ.get("DSH_CU_ALLOW_VISUAL_TEST") != "1":
    print("SKIP 提示层消失用例（要设 DSH_CU_ALLOW_VISUAL_TEST=1，会在屏幕上画 1~2 秒）")
else:
    home = os.path.join(os.environ.get("TEMP", "."), "cu-visual-%d" % os.getpid())
    os.makedirs(home, exist_ok=True)
    endpoint = r"\\.\pipe\cu-visual-%d" % os.getpid()
    env = dict(os.environ)
    env.update({
        "DSH_CU_ENDPOINT": endpoint,
        "DSH_CU_HOME": home,
        "DSH_CU_CONSENT": "auto",
        "DSH_CU_TEST": "1",
    })
    env.pop("DSH_CU_OVERLAY", None)       # 真提示层
    env.pop("DSH_CU_STAY_ALIVE", None)    # 生产语义：该退就退
    proc = subprocess.Popen([PY, os.path.join(HERE, "cu_server.py")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=0x08000000)

    conf = cu_server.TransportConfig.from_env()
    conf.endpoint = endpoint

    def call(op, payload=None, wait=30):
        """一次请求一条新连接：长连接在这台机器上会被第二个 accept 循环抢掉，
        实测同一 Client 连续发 ping→activate 时,activate 的响应到不了客户端。"""
        conn = cu_server.connect(conf)
        try:
            req = dict(payload or {})
            req["op"] = op
            req["id"] = 1
            cu_server.send_json(conn, req)
            resp = cu_server.recv_json(conn)
            return resp
        finally:
            try:
                conn.close()
            except Exception:                 # noqa: BLE001
                pass

    for _ in range(60):
        time.sleep(0.25)
        try:
            if call("ping", wait=5).get("ok"):
                break
        except Exception:                 # noqa: BLE001
            pass

    resp = call("activate", {"owner": "visual-gate", "idle_ttl": 60, "max_ttl": 300}, wait=60)
    check("真提示层下激活成功", resp.get("ok") is True, resp)

    # 杀掉提示层：屏幕上提示消失
    call("control", {"action": "kill_overlay"}, wait=10)
    exited = False
    for _ in range(40):                   # 最多 8 秒
        time.sleep(0.2)
        if proc.poll() is not None:
            exited = True
            break
    check("提示消失后服务自己退出（没有继续握着控制权）", exited)

    marker = os.path.join(home, "last_stop.json")
    reason = None
    if os.path.exists(marker):
        try:
            reason = json.load(open(marker, encoding="utf-8")).get("reason")
        except ValueError:
            reason = None
    check("关闭原因记为 visual-lost", reason == "visual-lost", "实际 %r" % (reason,))

    if proc.poll() is None:
        proc.kill()

print("RESULT:", "ALL PASS" if not fails else "%d FAILED" % len(fails))
sys.exit(0 if not fails else 1)
