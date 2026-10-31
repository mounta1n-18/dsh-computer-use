"""复现：op='shot' 带 region 的三种形态（工具真实走这条），以及 shadow 的报错文本。"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "python"))
import cu_server  # noqa: E402
import shadow  # noqa: E402


def client(endpoint):
    old = {k: os.environ.get(k) for k in ("DSH_CU_ENDPOINT", "DSH_CU_TRANSPORT")}
    os.environ["DSH_CU_ENDPOINT"] = endpoint
    os.environ["DSH_CU_TRANSPORT"] = "named_pipe"
    cfg = cu_server.TransportConfig.from_env()
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return cu_server.Client(cfg)


def timed(c, label, payload, cap=70):
    t0 = time.time()
    try:
        r, binary = c.request("shot", binary=True, **payload)
        dt = time.time() - t0
        print("  %-42s %6.1fs ok=%s code=%s bytes=%s msg=%s"
              % (label, dt, r.get("ok"), r.get("code") or "-",
                 len(binary or b""), (r.get("message") or "")[:70]))
    except Exception as e:
        print("  %-42s %6.1fs EXC %s %s"
              % (label, time.time() - t0, type(e).__name__, str(e)[:70]))


def main():
    st = shadow._read_state()
    if not st:
        shadow.start_session()
        st = shadow._read_state()

    print("[shadow 桌面]")
    cs = client(st["endpoint"])
    timed(cs, "shot 无 region", {})
    timed(cs, "shot region=None", {"region": None})
    cs.close()

    print("\n[main 桌面]")
    cm = client("\\\\.\\pipe\\dsh-computer-use")
    timed(cm, "shot 无 region", {})
    timed(cm, "shot region=[0,0,400,300]（列表）", {"region": [0, 0, 400, 300]})
    cm.close(); cm = client("\\\\.\\pipe\\dsh-computer-use")
    timed(cm, "shot region='0,0,400,300'（字符串）", {"region": "0,0,400,300"})
    timed(cm, "shot region='0,0,0,0'", {"region": "0,0,0,0"})
    timed(cm, "shot region=[0,0,2560,1440]（全屏大小）", {"region": [0, 0, 2560, 1440]})
    cm.close()


if __name__ == "__main__":
    main()
