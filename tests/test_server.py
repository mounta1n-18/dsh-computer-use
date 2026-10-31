# -*- coding: utf-8 -*-
"""常驻服务契约测试。

跑：`python tests/test_server.py`
不碰桌面（除了 where/shot 这种只读的），也不依赖外部环境。

覆盖（对应计划的 A2/A3 判据）：
  - 分帧编解码与超长拒绝
  - 单实例锁：第二个进程必须**退出**而不是抢桌面
  - cmd / batch（遇错停）/ eval（成功、异常、超时）
  - 截图：PNG 二进制帧 + 三重校验 + meta 自洽
  - 信封严格校验：未知 op 必须报协议错，不许当成功
  - **不重发**：写/读失败后客户端标记不可恢复，第二次请求连线上都不碰
  - 重启后状态干净（队列为空、静默窗归零、修饰键为空）
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PYDIR = os.path.join(ROOT, "python")
sys.path.insert(0, PYDIR)

PIPE = r"\\.\pipe\dsh-cu-test-%d" % os.getpid()
# 必须**同时**设进本进程：ping/Client 都走 TransportConfig.from_env()，
# 只给子进程设环境变量的话，本进程还在连默认管道名，永远 ping 不通。
os.environ["DSH_CU_ENDPOINT"] = PIPE
os.environ["DSH_CU_TRANSPORT"] = "named_pipe"
os.environ.pop("DSH_CU_HOME", None)
ENV = dict(os.environ)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %-46s %s%s" % (name, "PASS" if cond else "FAIL",
                            "" if cond else "   <<< " + str(detail)[:160]))


def start_server():
    return subprocess.Popen([sys.executable, os.path.join(PYDIR, "cu_server.py")],
                            env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def wait_ping(timeout=15.0):
    import cu_server
    cfg = cu_server.TransportConfig.from_env()
    deadline = time.time() + timeout
    while time.time() < deadline:
        p = cu_server.ping(cfg)
        if p:
            return p
        time.sleep(0.2)
    return None


def main():
    import cu_server
    import cu

    print("== 1. 分帧 ==")
    class Mem:
        def __init__(self):
            self.buf = b""

        def write_all(self, d):
            self.buf += d

        def read_exact(self, n):
            d, self.buf = self.buf[:n], self.buf[n:]
            if len(d) != n:
                raise EOFError
            return d

    m = Mem()
    cu_server.write_frame(m, cu_server.KIND_JSON, b'{"a":1}')
    cu_server.write_frame(m, cu_server.KIND_BINARY, b"\x89PNG", binary=True)
    k1, d1 = cu_server.read_frame(m)
    k2, d2 = cu_server.read_frame(m)
    check("帧往返：类型与载荷一致", (k1, d1, k2, d2) ==
          (cu_server.KIND_JSON, b'{"a":1}', cu_server.KIND_BINARY, b"\x89PNG"),
          (k1, d1, k2, d2))
    try:
        cu_server.write_frame(m, cu_server.KIND_JSON, b"x" * (cu_server.MAX_JSON + 1))
        check("超长 JSON 帧被拒", False, "没有抛错")
    except ValueError:
        check("超长 JSON 帧被拒", True)
    try:
        cu_server.write_frame(m, cu_server.KIND_BINARY, b"x" * (cu_server.MAX_BINARY + 1),
                              binary=True)
        check("超长二进制帧被拒", False, "没有抛错")
    except ValueError:
        check("超长二进制帧被拒", True)

    print("== 2. 启动与单实例 ==")
    srv = start_server()
    pong = wait_ping()
    check("服务起来了并能 ping 通", bool(pong), pong)
    if not pong:
        srv.kill()
        return 1
    check("ping 带协议版本", pong.get("protocol") == cu_server.PROTOCOL_VERSION, pong)
    check("ping 带屏幕与 pid", bool(pong.get("screen")) and pong.get("pid"), pong)

    srv2 = start_server()
    try:
        out2, err2 = srv2.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        srv2.kill()
        out2, err2 = b"", b""
    check("第二个实例自己退出（返回码 3）", srv2.returncode == 3,
          "rc=%s out=%s" % (srv2.returncode, out2[:200]))

    cfg = cu_server.TransportConfig.from_env()
    c = cu_server.Client(cfg)

    print("== 3. cmd / 信封 ==")
    resp, _ = c.request("cmd", argv=["where"])
    ok = resp.get("ok") and any("光标" in ln for ln in (resp.get("value") or {}).get("lines", []))
    check("cmd where 正常返回", ok, resp)
    resp, _ = c.request("nonsense")
    check("未知 op 报 CU_PROTOCOL", (not resp.get("ok")) and resp.get("code") == "CU_PROTOCOL",
          resp)
    resp, _ = c.request("cmd", argv=["__no_such_cmd__"])
    check("未知子命令报错且不崩", not resp.get("ok"), resp)

    print("== 4. shot：二进制帧 + 三重校验 ==")
    resp, png = c.request("shot", binary=True, **{"long": 640})
    meta = resp.get("value") or {}
    ok_sig = bool(png) and png[:8] == b"\x89PNG\r\n\x1a\n" and png[-8:-4] == b"IEND"
    check("shot 回的是完整 PNG", ok_sig, (resp, len(png or b"")))
    if ok_sig:
        w, h = cu.struct.unpack(">II", png[16:24])
        check("PNG 自身宽高 == meta 宽高",
              (w, h) == (meta.get("width"), meta.get("height")), (w, h, meta))
        check("meta 带物理分辨率与 token 估算",
              bool(meta.get("physical")) and meta.get("estimatedImageTokens", 0) > 0, meta)
        check("长边确实被缩到 640", max(w, h) == 640, (w, h))

    print("== 5. batch ==")
    resp, _ = c.request("batch", cmds=["where", "# 注释行", "where"])
    v = resp.get("value") or {}
    check("batch 顺序执行并跳过注释（跳过的不算成功）",
          resp.get("ok") and len(v.get("results", [])) == 3
          and v.get("ok_count") == 2 and v.get("skipped_count") == 1, v)
    resp, _ = c.request("batch", cmds=["where", "__no_such_cmd__", "where"])
    v = resp.get("value") or {}
    check("batch 遇错即停（不 continueOnError）",
          len(v.get("results", [])) == 2 and v.get("stopped_early") is True, v)
    resp, _ = c.request("batch", cmds=["__no_such_cmd__", "where"], continueOnError=True)
    v = resp.get("value") or {}
    check("batch continueOnError 继续跑完",
          len(v.get("results", [])) == 2 and v.get("stopped_early") is False, v)

    print("== 6. eval ==")
    resp, _ = c.request("eval", code="print(1+1)\n'结果'")
    v = resp.get("value") or {}
    check("eval 捕获 stdout 与末表达式",
          resp.get("ok") and "2" in v.get("stdout", "") and "结果" in v.get("stdout", ""), v)
    resp, _ = c.request("eval", code="raise ValueError('boom')")
    v = resp.get("value") or {}
    check("eval 异常被结构化返回",
          resp.get("ok") and v.get("ok") is False
          and (v.get("error") or {}).get("code") == "CU_EVAL", v)
    resp, _ = c.request("eval", code="import time; time.sleep(30)", timeoutMs=1500)
    v = resp.get("value") or {}
    check("eval 超时如实报告且不挂死服务",
          resp.get("ok") and v.get("timed_out") is True and "server_restart" in v.get("note", ""),
          v)
    resp, _ = c.request("ping")
    check("超时之后服务仍然可用", resp.get("ok"), resp)

    print("== 7. 不重发 ==")
    c2 = cu_server.Client(cfg)
    r, _ = c2.request("ping")
    check("新客户端首次 ping 成功", r.get("ok"), r)
    c2.conn.close()          # 人为把底层连接弄断
    try:
        c2.request("cmd", argv=["where"])
        check("断链后请求报 CU_IPC", False, "竟然成功了")
    except cu.CuError as e:
        check("断链后请求报 CU_IPC", e.code == "CU_IPC", e)
    check("断链后客户端被标记为不可恢复（不重发）", c2._unrecoverable is True, c2._unrecoverable)
    try:
        c2.request("cmd", argv=["where"])
        check("不可恢复后拒绝再发", False, "竟然又发了")
    except cu.CuError as e:
        check("不可恢复后拒绝再发（线上一次都没碰）", e.code == "CU_IPC", e)

    print("== 9. 凝视不阻塞服务（回归：曾经把整条服务锁死 30 分钟）==")
    t0 = time.time()
    resp, _ = c.request("cmd", argv=["盯", "--timeout", "4", "--queue", "wq"])
    dt = time.time() - t0
    check("watch_start 立刻返回（不阻塞）", bool(resp.get("ok")) and dt < 3.0, (round(dt, 2), resp))
    t1 = time.time()
    resp2, _ = c.request("cmd", argv=["where"])
    check("凝视期间 where 仍可用", bool(resp2.get("ok")) and time.time() - t1 < 3.0,
          (round(time.time() - t1, 2), resp2))
    resp3, _ = c.request("cmd", argv=["瞧", "--queue", "wq"])
    # 队列目录要等凝视进程取到基线才建；这期间如实报 CU_NO_QUEUE 是**正确**行为，
    # 不能算失败（要断言的是"服务还活着"，上面两条已经覆盖）。
    check("凝视期间 瞧 可用（队列未建时如实报 NO_QUEUE 也算通过）",
          bool(resp3.get("ok")) or resp3.get("code") == "CU_NO_QUEUE", resp3)
    st, _ = c.request("state")
    check("state 里能看到自拉起的 watcher",
          bool((st.get("value") or {}).get("watchers")), st)

    print("== 8. 状态与重启 ==")
    resp, _ = c.request("cmd", argv=["静默", "3"])
    resp, _ = c.request("state")
    st = resp.get("value") or {}
    check("state 能看到静默窗", st.get("mute_until", 0) > time.time(), st)
    check("state 里修饰键为空", st.get("modified_keys") == [], st)
    c.request("shutdown")
    c.close()
    try:
        srv.wait(timeout=20)
    except subprocess.TimeoutExpired:
        srv.kill()
    time.sleep(1.0)
    check("shutdown 后服务真的退出", srv.poll() is not None, srv.poll())

    srv3 = start_server()
    pong3 = wait_ping()
    check("重启后能再次 ping 通", bool(pong3), pong3)
    if pong3:
        check("重启后队列为空", pong3.get("queues") == [], pong3.get("queues"))
        check("重启后静默窗归零（状态干净）", pong3.get("mute_until", 1) == 0, pong3)
        check("重启后修饰键为空", pong3.get("modified_keys") == [], pong3)
        c3 = cu_server.Client(cfg)
        c3.request("shutdown")
        c3.close()
        try:
            srv3.wait(timeout=20)
        except subprocess.TimeoutExpired:
            srv3.kill()

    print()
    print("常驻服务契约: %s（%d 通过 / %d 失败）"
          % ("PASS" if not FAIL else "FAIL", len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败：%s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
