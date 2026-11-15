"""可迁移性验证：从一份干净副本启动服务，跑 ping/activate/shot/deactivate。

用法：python _port_check.py <包目录>
不依赖仓库里的任何测试设施，只用包自身的 python/ 模块。
"""
import os
import subprocess
import sys
import time

root = os.path.abspath(sys.argv[1])
os.chdir(root)
sys.path.insert(0, os.path.join(root, "python"))

import cu_server as S  # noqa: E402

endpoint = r"\\.\pipe\dsh-cu-port-" + str(os.getpid())
home = os.path.join(os.environ["TEMP"], "cu-port-home-" + str(os.getpid()))

env = {k: v for k, v in os.environ.items() if not k.startswith("DSH_CU_")}
env.update({
    "DSH_CU_ENDPOINT": endpoint,
    "DSH_CU_TRANSPORT": "named_pipe",
    "DSH_CU_HOME": home,
    "DSH_CU_CONSENT": "auto",
    "DSH_CU_OVERLAY": "0",
    "DSH_CU_STAY_ALIVE": "0",
    "PYTHONUTF8": "1",
})

t0 = time.time()
proc = subprocess.Popen([sys.executable, os.path.join(root, "python", "cu_server.py")],
                        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
client = S.Client(S.TransportConfig("named_pipe", endpoint, 0))
for _ in range(200):
    try:
        client.connect()
        break
    except Exception:
        time.sleep(0.05)
print("boot %.2fs" % (time.time() - t0))

print("ping       ", client.request("ping")[0].get("ok"))
r, _ = client.request("activate", owner="portable", idle_ttl=60)
print("activate   ", r.get("ok"))
print("shot       ", client.request("cmd", argv=["shot"])[0].get("ok"))
r, _ = client.request("deactivate", reason="portable")
print("deactivate ", r.get("ok"), "closed", (r.get("value") or {}).get("closed"))
proc.wait(timeout=20)
print("exit       ", proc.returncode)
