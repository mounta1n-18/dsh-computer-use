"""复现报告里的两个超时：shadow 下 shot；main 下 shot --region。"""
import json
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


def timed(c, label, argv, cap=70):
    t0 = time.time()
    try:
        r, _ = c.request("cmd", argv=[str(x) for x in argv])
        dt = time.time() - t0
        v = r.get("value") or {}
        text = "\n".join(v.get("lines") or [])[:80] if isinstance(v, dict) else ""
        print("  %-38s %6.1fs ok=%s %s %s" % (label, dt, r.get("ok"), r.get("code") or "", text))
    except Exception as e:
        dt = time.time() - t0
        print("  %-38s %6.1fs EXC %s" % (label, dt, type(e).__name__, str(e)[:80]))


def main():
    st = shadow._read_state()
    if not st:
        shadow.start_session()
        st = shadow._read_state()
    print("shadow 会话：%s" % st["endpoint"])

    print("\n[1] 隔离桌面（shadow）")
    cs = client(st["endpoint"])
    timed(cs, "shot（全屏，无 region）", ["shot", "--long", "400"])
    timed(cs, "shot --region 0,0,400,300", ["shot", "--region", "0,0,400,300", "--long", "400"])
    timed(cs, "shot --window <任意>", ["shot", "--window", "Calculator", "--long", "400"])

    print("\n[2] 用户桌面（main）")
    cm = client("\\\\.\\pipe\\dsh-computer-use")
    timed(cm, "shot（全屏）", ["shot", "--long", "400"])
    timed(cm, "shot --region 0,0,400,300", ["shot", "--region", "0,0,400,300", "--long", "400"])
    cm.close()
    cs.close()


if __name__ == "__main__":
    main()
