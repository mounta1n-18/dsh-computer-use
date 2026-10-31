# -*- coding: utf-8 -*-
"""元素级（UIA）能力的 Python 侧客户端。

设计要点（都是踩出来的取舍）：
- **常驻**：ref 要能跨调用存活，.NET 的 AutomationElement 就得一直活在同一个进程里。
  每次起一个新 PowerShell 的话上一轮的元素对象已经没了，ref 变成空号。
- **一行一 JSON 的行协议**：比二进制分帧简单得多，而 UIA 查询本身就有几十毫秒开销，
  协议层的开销可以忽略。
- **读线程 + 队列**：Windows 上 `select` 对管道无效，所以只能用后台线程读、
  主线程带超时地从队列取。超时能报错，而不是永久挂住。
- **不在这里做真鼠标**：需要真点击时只回中心点，交给 cu.py 那套 click（带静默窗、
  带修饰键复位）。注入只有一条实现，不重复造。
"""
import atexit
import json
import os
import queue
import subprocess
import sys
import threading
import time

from cu import CuError

HERE = os.path.dirname(os.path.abspath(__file__))
SIDECAR = os.path.join(HERE, "uia_sidecar.ps1")
DEFAULT_TIMEOUT = 25.0

_lock = threading.Lock()
_proc = None
_q = None
_reader = None
_seq = 0
_err_path = None


def _stderr_tail(limit=600):
    """子进程的 stderr 落文件、出错时回读尾巴。
    为什么不直接读管道：没人读的管道写满就会把子进程堵死（经典死锁），
    而崩溃现场的 stderr 恰恰是最有用的一条信息。"""
    try:
        with open(_err_path, "rb") as f:
            data = f.read()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", "replace").strip()


def _spawn():
    global _proc, _q, _reader, _err_path
    if _proc is not None and _proc.poll() is None:
        return _proc
    if not os.path.exists(SIDECAR):
        raise CuError("CU_NO_SIDECAR", "找不到 %s" % SIDECAR)
    _err_path = os.path.join(os.environ.get("TEMP", "."), "dsh-cu-uia-stderr.log")
    try:
        err = open(_err_path, "wb")
    except OSError:
        err = subprocess.DEVNULL
    # `CreateProcess` 不指定 lpDesktop 时，子进程继承**父进程**的桌面——
    # 所以在"独立桌面"里跑 UIA 必须显式把 sidecar 送进去
    # （在切了 SetThreadDesktop 的线程里 spawn 也没用，继承看父进程）。
    startupinfo = None
    desk = os.environ.get("DSH_CU_UIA_DESKTOP")
    if desk:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.lpDesktop = desk
    try:
        _proc = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", SIDECAR],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
            cwd=HERE, startupinfo=startupinfo,
        )
    except OSError as e:
        raise CuError("CU_NO_SIDECAR", "起不来 PowerShell 子进程：%s" % e)
    _q = queue.Queue()

    def _read():
        stream = _proc.stdout
        while True:
            try:
                raw = stream.readline()
            except Exception:
                break
            if not raw:
                break
            try:
                _q.put(json.loads(raw.decode("utf-8", "replace")))
            except ValueError:
                _q.put({"ok": False, "code": "CU_PROTOCOL",
                        "message": "子进程回了一行非 JSON：%r" % raw[:120]})
        _q.put(None)      # 结束哨兵

    _reader = threading.Thread(target=_read, name="uia-reader", daemon=True)
    _reader.start()
    return _proc


def _kill():
    global _proc
    p, _proc = _proc, None
    if p is None:
        return
    try:
        if p.poll() is None:
            p.kill()
    except OSError:
        pass
    try:
        p.wait(timeout=3)
    except Exception:
        pass


atexit.register(_kill)


def _call(op, timeout=DEFAULT_TIMEOUT, **payload):
    """发一个请求、等一个响应。任何异常都转成 CuError，且**不重发**——
    和常驻服务那边同一条不变量：写失败后对端可能已经执行过了。"""
    global _seq
    with _lock:
        p = _spawn()
        _seq += 1
        req = dict(payload)
        req["id"] = _seq
        req["op"] = op
        line = (json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            p.stdin.write(line)
            p.stdin.flush()
        except (OSError, ValueError) as e:
            _kill()
            raise CuError("CU_NO_SIDECAR", "写 UIA 子进程失败：%s" % e)
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                _kill()
                raise CuError("CU_NO_SIDECAR",
                              "UIA 子进程 %gs 没响应，已回收。可用 resync 重来" % timeout)
            try:
                resp = _q.get(timeout=min(left, 1.0))
            except queue.Empty:
                continue
            if resp is None:
                tail = _stderr_tail()
                _kill()
                raise CuError("CU_NO_SIDECAR",
                              "UIA 子进程退出了" + ("；stderr 尾部：%s" % tail if tail else ""))
            break
    if not isinstance(resp, dict):
        raise CuError("CU_PROTOCOL", "UIA 响应不是对象")
    if not resp.get("ok"):
        code = resp.get("code") or "CU_ELEMENT"
        raise CuError(code, resp.get("message") or "UIA 操作失败")
    return resp


# ---------------------------------------------------------------- 对外接口
def snapshot(window=None, depth=12, limit=400):
    """整棵树快照。返回 {gen, window, refs:[...], truncated}。"""
    return _call("snapshot", window=window, depth=int(depth), limit=int(limit))


def at(x, y):
    """物理像素点上是什么元素。"""
    return _call("at", x=int(x), y=int(y))


def get(ref, prop=None):
    """读某个 ref 的属性；prop 省略就回整份元素信息。"""
    return _call("get", ref=str(ref), prop=prop)


def act(ref, action, value=None):
    """对元素执行模式动作。action ∈ invoke/set_value/set_focus/toggle/select/
    add_to_selection/remove_from_selection/expand/collapse/scroll_into_view/
    set_range_value/window_close。"""
    return _call("act", ref=str(ref), action=str(action),
                 value=None if value is None else str(value))


def resync():
    """丢掉元素缓存（元素层行为异常时调它重来）。"""
    return _call("resync")


def find(query, window=None, limit=400, depth=12):
    """在快照里按名字/角色找。返回排序后的候选列表。

    相关度：名字完全相等 > 名字以它开头 > 名字包含 > 角色匹配。
    多个同样合理时**全部返回**，由调用方决定——猜一个"最像的"是错答案的开始。
    """
    snap = snapshot(window=window, depth=depth, limit=limit)
    q = str(query or "").strip().casefold()
    hits = []
    for it in snap.get("refs") or []:
        name = str(it.get("name") or "")
        role = str(it.get("role") or "")
        n = name.casefold()
        if not q:
            score = 0
        elif n == q:
            score = 100
        elif n.startswith(q):
            score = 80
        elif q in n:
            score = 60
        elif q in role.casefold():
            score = 30
        else:
            continue
        if not it.get("enabled", True):
            score -= 10
        hits.append((score, it))
    hits.sort(key=lambda t: (-t[0], t[1].get("ref", "")))
    return {"gen": snap.get("gen"), "window": snap.get("window"),
            "refs": [it for _, it in hits],
            "truncated": snap.get("truncated", False)}


def center_of(ref, window=None):
    """拿某个 ref 的可点中心（物理像素）。找不到就报错，不猜。"""
    info = get(ref)
    el = info.get("element") or {}
    r = el.get("rect") or {}
    if not r or r.get("w", 0) <= 0 or r.get("h", 0) <= 0:
        raise CuError("CU_ELEMENT",
                      "元素 %s 没有可点区域（可能已滚出可视区或已失效）" % ref)
    return int(el.get("cx", 0)), int(el.get("cy", 0)), el


def alive():
    """子进程是否还活着（doctor 用）。"""
    with _lock:
        if _proc is None:
            return False
        return _proc.poll() is None


if __name__ == "__main__":
    # 手动冒烟：python uia.py [窗口名子串]
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    win = sys.argv[1] if len(sys.argv) > 1 else None
    snap = snapshot(window=win, depth=6, limit=60)
    print("gen=%s window=%s refs=%d truncated=%s"
          % (snap.get("gen"), (snap.get("window") or {}).get("name"),
             len(snap.get("refs") or []), snap.get("truncated")))
    for it in (snap.get("refs") or [])[:30]:
        print("  %-6s %-14s %-30s rect=%s" % (
            it.get("ref"), it.get("role"), str(it.get("name"))[:30],
            it.get("rect")))
