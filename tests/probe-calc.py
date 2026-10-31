"""探路：计算器能不能在隔离桌面里起来（UWP 在无 DWM 的桌面上可能不渲染）。"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "python"))

import cu_server  # noqa: E402
import shadow  # noqa: E402

info = shadow.ensure_session() if hasattr(shadow, "ensure_session") else None
if info is None:
    st = shadow._read_state()
    if not st:
        shadow.start_session()
        st = shadow._read_state()
else:
    st = info

old = {k: os.environ.get(k) for k in ("DSH_CU_ENDPOINT", "DSH_CU_TRANSPORT")}
os.environ["DSH_CU_ENDPOINT"] = st["endpoint"]
os.environ["DSH_CU_TRANSPORT"] = "named_pipe"
cfg = cu_server.TransportConfig.from_env()
for k, v in old.items():
    if v is None:
        os.environ.pop(k, None)
    else:
        os.environ[k] = v

c = cu_server.Client(cfg)
print("ping:", bool(cu_server.ping(cfg)))


def call(argv, **kw):
    r, _ = c.request("cmd", argv=[str(x) for x in argv], **kw)
    v = r.get("value") or {}
    text = "\n".join(v.get("lines") or []) if isinstance(v, dict) else json.dumps(v)
    return r.get("ok"), r.get("code") or "", text


ok, code, text = call(["launch", "计算器", "--timeout", "20"])
print("launch:", ok, code, text[:200])
time.sleep(2)
ok, code, text = call(["windows"])
print("windows:\n" + text[:800])
c.close()
