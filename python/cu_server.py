# -*- coding: utf-8 -*-
"""cu 常驻服务：把"每条命令起一个 Python 进程"换成"一次拉起、长连接收发"。

为什么值得做：
- 冷启动约 150~300ms，每条命令都付一次；常驻后单命令只剩一次管道往返。
- 更要紧的是**状态**：凝视队列、修饰键 latch、静默窗到期时间，以前靠临时文件在
  进程之间传；现在它们就是内存里的对象，语义清楚得多（磁盘那份仍然保留，
  作为 CLI 路径与崩溃恢复的兜底）。

设计要点（对齐豆包 GAC 的传输层，但用我们自己的命名，避免与 CUA_GAC_* 撞车）：
- **三种传输 + 兜底链**：Windows 优先命名管道，其它平台优先 Unix socket，TCP 兜底。
  配置全部来自 DSH_CU_* 环境变量，非法就报**可执行**的错（缺什么、怎么补）。
- **分帧**：`[u32 BE 长度][u8 类型][载荷]`；类型 0=JSON、1=PNG、2=日志。
  图片走独立的二进制帧，不 base64——省 33% 的无谓拷贝。
- **不重发**：一次请求写/读失败后**绝不自动重试**（对端可能已经执行过了，
  重试等于把点击点两次）。只标记为不可恢复，并在下一个请求前重连。
- **单实例**：靠锁文件；第二个进程拿不到锁就退出，由插件复用已在跑的那个。
- **结果信封严格校验**：缺 ok / output.type 未知 / 失败却没带 error 对象，
  一律 CU_PROTOCOL。宁可报错，也不把半成品当结果。
"""
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

CU_PY = os.path.join(HERE, "cu.py")
# 凝视是**长任务**：它要在后台跑几十分钟盯着画面。这种东西绝不能占用服务的请求锁，
# 否则一条 `watch_start` 会把整个工具链（shot/click/eval…）堵到天荒地老。
# 所以服务端对它特殊处理：起一个独立进程，立刻返回。
WATCH_CMDS = ("盯", "watch")

import cu  # noqa: E402  （cu 在 import 时会设置 DPI 感知与三个流的 UTF-8）

PROTOCOL_VERSION = 1
SERVER_VERSION = "1.0.0"
PIPE_NAME = r"\\.\pipe\dsh-computer-use"

KIND_JSON = 0
KIND_BINARY = 1

MAX_JSON = 8 * 1024 * 1024
MAX_BINARY = 32 * 1024 * 1024

DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_IO_TIMEOUT = 30.0
# 对齐豆包 client.py：主传输有兜底时只给 0.5s 的尝试窗口，免得 5s 全耗在死路上
IPC_CONNECT_TIMEOUT_WITH_FALLBACK = 0.5


# ---------------------------------------------------------------- 传输配置
class TransportConfig:
    """从环境变量解析出的传输配置。字段与豆包 GacTransportConfig 一一对应。"""

    def __init__(self, kind, endpoint, port, fallback_kind="", fallback_port=0,
                 connect_timeout=DEFAULT_CONNECT_TIMEOUT, io_timeout=DEFAULT_IO_TIMEOUT):
        self.kind = kind
        self.endpoint = endpoint
        self.port = port
        self.fallback_kind = fallback_kind
        self.fallback_port = fallback_port
        self.connect_timeout = connect_timeout
        self.io_timeout = io_timeout

    @classmethod
    def from_env(cls):
        def _port(name):
            v = os.environ.get(name)
            if not v:
                return 0
            try:
                p = int(v)
            except (TypeError, ValueError):
                raise SystemExit("GUI 操作不可用：%s 不是合法端口号（收到 %r）" % (name, v))
            if not 0 <= p <= 65535:
                raise SystemExit("GUI 操作不可用：%s 越界（收到 %r）" % (name, v))
            return p

        default_kind = "named_pipe" if os.name == "nt" else "uds"
        kind = (os.environ.get("DSH_CU_TRANSPORT") or default_kind).strip().lower()
        if kind not in ("named_pipe", "uds", "tcp"):
            raise SystemExit(
                "GUI 操作不可用：DSH_CU_TRANSPORT 只支持 named_pipe / uds / tcp（收到 %r）"
                % kind)
        endpoint = (os.environ.get("DSH_CU_ENDPOINT") or "").strip()
        if kind == "named_pipe" and not endpoint:
            endpoint = PIPE_NAME
        if kind == "uds" and not endpoint:
            endpoint = os.path.join(_home_dir(), "cu.sock")
        port = _port("DSH_CU_PORT")
        if kind == "tcp" and not port:
            raise SystemExit("GUI 操作不可用：DSH_CU_TRANSPORT=tcp 时必须给 DSH_CU_PORT")
        fb = (os.environ.get("DSH_CU_FALLBACK_TRANSPORT") or "").strip().lower()
        if fb and fb not in ("", "tcp"):
            raise SystemExit("GUI 操作不可用：DSH_CU_FALLBACK_TRANSPORT 只支持留空或 tcp")
        fb_port = _port("DSH_CU_FALLBACK_PORT")
        if fb == "tcp" and not fb_port:
            raise SystemExit("GUI 操作不可用：有 TCP 兜底时必须有 DSH_CU_FALLBACK_PORT")

        def _f(name, default):
            v = os.environ.get(name)
            if not v:
                return default
            try:
                return float(v)
            except (TypeError, ValueError):
                raise SystemExit("GUI 操作不可用：%s 不是数字（收到 %r）" % (name, v))

        return cls(kind, endpoint, port, fb, fb_port,
                   _f("DSH_CU_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT),
                   _f("DSH_CU_IO_TIMEOUT", DEFAULT_IO_TIMEOUT))


def _home_dir():
    base = os.environ.get("DSH_CU_HOME")
    if not base:
        la = os.environ.get("LOCALAPPDATA")
        base = os.path.join(la, "dsh-computer-use") if la else \
            os.path.join(cu.tempfile.gettempdir(), "dsh-computer-use")
    os.makedirs(base, exist_ok=True)
    return base


LOG_FILE = os.path.join(_home_dir(), "server.log")


def log(msg):
    line = "%s [%d] %s" % (time.strftime("%H:%M:%S"), os.getpid(), msg)
    try:
        try:
            if os.path.getsize(LOG_FILE) > 1024 * 1024:
                os.replace(LOG_FILE, LOG_FILE + ".1")
        except OSError:
            pass
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if os.environ.get("DSH_CU_SERVER_STDERR"):
        print(line, file=sys.stderr)


# ---------------------------------------------------------------- 分帧
def write_frame(conn, kind, payload, binary=False):
    if binary:
        if len(payload) > MAX_BINARY:
            raise ValueError("二进制帧超过 %d 字节上限" % MAX_BINARY)
    elif len(payload) > MAX_JSON:
        raise ValueError("JSON 帧超过 %d 字节上限" % MAX_JSON)
    conn.write_all(struct.pack(">IB", len(payload), kind) + payload)


def read_frame(conn):
    head = conn.read_exact(5)
    size, kind = struct.unpack(">IB", head)
    if kind == KIND_BINARY:
        if size > MAX_BINARY:
            raise ValueError("对端声明的二进制帧过大：%d" % size)
    elif size > MAX_JSON:
        raise ValueError("对端声明的 JSON 帧过大：%d" % size)
    return kind, conn.read_exact(size) if size else b""


def send_json(conn, obj, binary_payload=None):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    write_frame(conn, KIND_JSON, data)
    if binary_payload is not None:
        write_frame(conn, KIND_BINARY, binary_payload, binary=True)


def recv_json(conn):
    kind, data = read_frame(conn)
    if kind != KIND_JSON:
        raise ValueError("期待 JSON 帧，收到类型 %d" % kind)
    return json.loads(data.decode("utf-8"))


# ---------------------------------------------------------------- 连接实现
class SockConn:
    def __init__(self, sock):
        self.sock = sock

    def read_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise EOFError("对端关闭")
            buf += chunk
        return buf

    def write_all(self, data):
        self.sock.sendall(data)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class PipeConn:
    """Windows 命名管道。用 pywin32；没装的话服务端会明确报缺依赖。"""

    def __init__(self, handle):
        self.handle = handle

    def read_exact(self, n):
        buf = b""
        while len(buf) < n:
            try:
                _, data = win32file.ReadFile(self.handle, n - len(buf))
            except pywintypes.error as e:      # 109 = ERROR_BROKEN_PIPE
                raise EOFError("管道断开（%s）" % (e.args[2] if len(e.args) > 2 else e,))
            if not data:
                raise EOFError("管道断开")
            buf += data
        return buf

    def write_all(self, data):
        try:
            win32file.WriteFile(self.handle, data)
        except pywintypes.error as e:
            raise EOFError("管道写失败（%s）" % (e.args[2] if len(e.args) > 2 else e,))

    def close(self):
        try:
            win32file.CloseHandle(self.handle)
        except Exception:
            pass


try:
    import win32file
    import win32pipe
    import pywintypes
    _HAVE_PYWIN32 = True
except ImportError:                                   # pragma: no cover
    _HAVE_PYWIN32 = False


def connect(config, timeout=None):
    """按配置连接服务端；主传输失败且有 TCP 兜底时落兜底。"""
    t = config.connect_timeout if timeout is None else timeout
    try:
        return _connect_kind(config.kind, config.endpoint, config.port,
                             min(t, IPC_CONNECT_TIMEOUT_WITH_FALLBACK)
                             if config.fallback_kind else t)
    except (OSError, EOFError, RuntimeError) as primary:
        if config.fallback_kind != "tcp":
            raise
        try:
            return _connect_kind("tcp", "", config.fallback_port, config.connect_timeout)
        except (OSError, EOFError, RuntimeError) as fb:
            raise RuntimeError("主传输 %s 与 TCP 兜底都连不上：%s；%s"
                               % (config.kind, primary, fb))


def _connect_kind(kind, endpoint, port, timeout):
    if kind == "named_pipe":
        if not _HAVE_PYWIN32:
            raise RuntimeError("缺少 pywin32，命名管道不可用。装它：%s -m pip install --user pywin32"
                               % sys.executable)
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                h = win32file.CreateFile(
                    endpoint, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0, None, win32file.OPEN_EXISTING, 0, None)
                return PipeConn(h)
            except pywintypes.error as e:
                last = e
                time.sleep(0.05)
        raise RuntimeError("连不上命名管道 %s（%s）" % (endpoint, last))
    if kind == "uds":
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(endpoint)
        except OSError:
            s.close()
            raise
        s.settimeout(None)
        return SockConn(s)
    if kind == "tcp":
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        s.settimeout(None)
        return SockConn(s)
    raise RuntimeError("未知传输类型 %r" % kind)


# ---------------------------------------------------------------- 请求处理
class Server:
    """服务端状态机。所有请求串行处理（桌面操作本来就是串行的）。"""

    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.started = time.time()
        self.requests = 0
        self.last_error = None
        self.stopping = False
        self.watchers = []          # 自己拉起的凝视子进程，shutdown 时要带走
        self.listener = None        # 正在 accept 的监听句柄：shutdown 时叫醒主循环用

    # ---- 凝视：起独立进程，立刻返回 ----
    def _prune_watchers(self):
        self.watchers = [p for p in self.watchers if p.poll() is None]

    def _kill_watchers(self):
        for p in self.watchers:
            try:
                if p.poll() is None:
                    p.terminate()
            except OSError:
                pass
        self.watchers = []

    def _close_listener(self):
        """把阻塞在 accept 上的主循环叫醒。stopping=True 本身唤不醒系统调用。"""
        listener, self.listener = self.listener, None
        if self.config.kind == "named_pipe":
            # Windows 上关闭一个正阻塞在 ConnectNamedPipe 的句柄不会让它返回；
            # 主动连一下这个端点，ConnectNamedPipe 就会成功返回，主循环随即
            # 看到 stopping 并退出（关闭句柄那条路实测无效）。
            def _poke():
                try:
                    import win32file
                    h = win32file.CreateFile(
                        self.config.endpoint,
                        win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                        0, None, win32file.OPEN_EXISTING, 0, None)
                    win32file.CloseHandle(h)
                except Exception:                            # noqa: BLE001
                    pass
            threading.Thread(target=_poke, daemon=True).start()
            return
        if listener is None:
            return
        try:
            listener.close()
        except Exception:                                     # noqa: BLE001
            log("关闭监听句柄失败：%s" % traceback.format_exc())

    def _watch_spawn(self, req):
        argv = req.get("argv")
        line = req.get("line")
        if isinstance(argv, list) and argv:
            cmd_argv = [str(x) for x in argv]
        elif isinstance(line, str) and line.strip():
            import shlex
            cmd_argv = shlex.split(line)
        else:
            raise cu.CuError("CU_BAD_ARG", "凝视需要 argv 数组或 line 字符串")
        # 先本地解析一遍：参数不合法就当场报，别起一个立刻报错的进程让调用方去找日志
        cu.build_parser().parse_args(cmd_argv)
        flags = 0
        if os.name == "nt":
            flags = 0x08000000        # CREATE_NO_WINDOW：不弹黑框
        try:
            proc = subprocess.Popen(
                [sys.executable, CU_PY] + cmd_argv,
                cwd=HERE, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
        except OSError as e:
            raise cu.CuError("CU_OS", "起凝视进程失败：%s" % e)
        self._prune_watchers()
        self.watchers.append(proc)
        q = "default"
        if "--queue" in cmd_argv:
            i = cmd_argv.index("--queue")
            if i + 1 < len(cmd_argv):
                q = cmd_argv[i + 1]
        return {"spawned": True, "pid": proc.pid, "queue": q, "argv": cmd_argv,
                "lines": [
                    "凝视已在后台启动：pid=%d 队列=%r（它自己按 --timeout 到点退出）"
                    % (proc.pid, q),
                    "取帧用 watch_recv（队列空就等 wait 秒）；看积压用 watch_status。",
                    "**不要**再连续截图代替等待——凝视期间你这边零 token。",
                ]}

    @staticmethod
    def _is_watch(req):
        argv = req.get("argv")
        if isinstance(argv, list) and argv:
            return str(argv[0]) in WATCH_CMDS
        line = req.get("line")
        if isinstance(line, str):
            import shlex
            try:
                parts = shlex.split(line)
            except ValueError:
                return False
            return bool(parts) and parts[0] in WATCH_CMDS
        return False

    # ---- 各 op ----
    def handle(self, conn, req):
        op = (req or {}).get("op")
        rid = (req or {}).get("id")
        t0 = time.time()
        try:
            if op == "ping":
                send_json(conn, self._wrap(rid, self._ping(), t0))
            elif op == "cmd":
                if self._is_watch(req):
                    send_json(conn, self._wrap(rid, self._watch_spawn(req), t0))
                else:
                    send_json(conn, self._wrap(rid, self._cmd(req), t0))
            elif op == "batch":
                send_json(conn, self._wrap(rid, self._batch(req), t0))
            elif op == "eval":
                send_json(conn, self._wrap(rid, self._eval(req), t0))
            elif op == "shot":
                out = self._shot(req)
                send_json(conn, self._wrap(rid, out["meta"], t0),
                          binary_payload=out["png"])
            elif op == "state":
                send_json(conn, self._wrap(rid, self._state(), t0))
            elif op == "audit":
                n = int(req.get("n") or 20)
                send_json(conn, self._wrap(rid, {"records": cu.audit_tail(n)}, t0))
            elif op == "shutdown":
                send_json(conn, self._wrap(rid, {"bye": True}, t0))
                self.stopping = True
                self._kill_watchers()     # 自己起的凝视进程不能留成孤儿
                # 主 accept 循环正阻塞在 ConnectNamedPipe/accept 上，不会因为
                # stopping 被唤醒；叫醒它，进程才能走正常 atexit（释放按住的
                # 修饰键、结束 UIA 子进程），也才能赶在宿主 5s 关机预算内退出。
                self._close_listener()
            else:
                send_json(conn, self._wrap(rid, None, t0, error=(
                    "CU_PROTOCOL", "不认识的 op=%r" % op)))
        except cu.CuError as e:
            self.last_error = str(e)
            send_json(conn, self._wrap(rid, None, t0, error=(e.code, str(e))))
        except SystemExit as e:
            # 万一还有没被 _parse_* 拦住的 SystemExit（比如某个子命令自己 sys.exit），
            # 也必须变成一条响应，而不是把会话掐断。
            self.last_error = "SystemExit(%s)" % getattr(e, "code", None)
            send_json(conn, self._wrap(rid, None, t0, error=(
                "CU_BAD_ARG", "参数不合法（退出码 %s）" % getattr(e, "code", None))))
        except Exception as e:                                   # noqa: BLE001
            self.last_error = "%s: %s" % (type(e).__name__, e)
            log("handler 异常：%s" % traceback.format_exc())
            send_json(conn, self._wrap(rid, None, t0, error=(
                "CU_UNEXPECTED", "%s: %s" % (type(e).__name__, e))))

    def _wrap(self, rid, value, t0, error=None):
        """组装响应并**严格自检信封**：缺 ok / 类型未知 / 失败却没带 error 对象，
        一律 CU_PROTOCOL。宁可报协议错，也不把半成品当结果发给模型。"""
        self.requests += 1
        out = {"id": rid, "ok": error is None, "ms": int((time.time() - t0) * 1000)}
        if error:
            out["code"], out["message"] = error
            out["error"] = {"code": error[0], "message": error[1]}
        else:
            out["value"] = value
        # 这里**不做**信封自检：这个字典是我们自己刚拼的，检查它永远通过，
        # 只是"看起来校验过"的假防御。真正该校验的是**消费方**——Node 客户端
        # 会校验响应里的 id 与 ok 字段（见 lib/pipe-client.mjs 的 request()）。
        # 二进制（图片）不进 JSON：只留一个标记，真实字节走紧随其后的二进制帧
        if isinstance(value, dict) and "image" in value:
            out["value"] = {k: v for k, v in value.items() if k != "image"}
            out["hasImage"] = True
        return out

    def _ping(self):
        vx, vy, vw, vh = cu.screen()
        return {"protocol": PROTOCOL_VERSION, "version": SERVER_VERSION,
                "pid": os.getpid(), "uptime": round(time.time() - self.started, 1),
                "transport": self.config.kind, "endpoint": self.config.endpoint,
                "screen": {"x": vx, "y": vy, "width": vw, "height": vh},
                "queues": _queue_names(), "mute_until": cu._muted_until(),
                "modified_keys": sorted(cu._PRESSED_MODS | cu._HELD),
                "requests": self.requests, "last_error": self.last_error}

    # ---- 解析：把 argparse 的 SystemExit+stderr 统一变成 CuError ----
    # argparse 在参数不合法时会 `sys.exit(2)` 并把用法打到 stderr。SystemExit 是
    # BaseException，`except Exception` 抓不到——它会一路穿出会话循环、把连接掐断，
    # 客户端只看到"管道已结束"，根本不知道是自己参数写错了。必须在边界上拦住。
    @staticmethod
    def _parse_argv(argv):
        import contextlib
        import io as _io
        buf = _io.StringIO()
        try:
            with contextlib.redirect_stderr(buf):
                return cu._parser().parse_args([str(x) for x in argv])
        except SystemExit:
            raise cu.CuError("CU_BAD_ARG", Server._usage_error(argv, buf))

    @staticmethod
    def _parse_line(line):
        import contextlib
        import io as _io
        buf = _io.StringIO()
        try:
            with contextlib.redirect_stderr(buf):
                return cu.make_parser_for(line)
        except SystemExit:
            raise cu.CuError("CU_BAD_ARG", Server._usage_error(line, buf))

    @staticmethod
    def _usage_error(src, buf):
        text = " ".join((buf.getvalue() or "").split())
        if len(text) > 300:
            text = text[:300] + "…"
        return "参数不合法：%s（输入：%s）" % (text or "见各子命令 --help", src)

    def _cmd(self, req):
        argv = req.get("argv")
        line = req.get("line")
        if argv is None and line is not None:
            a = self._parse_line(line)
        elif isinstance(argv, list) and argv:
            a = self._parse_argv(argv)
        else:
            raise cu.CuError("CU_BAD_ARG", "cmd 需要 argv 数组或 line 字符串")
        lines = cu.execute_namespace(a)
        return {"argv": argv or line, "cmd": a.cmd, "lines": lines}

    def _batch(self, req):
        cmds = req.get("cmds") or req.get("commands") or []
        if not isinstance(cmds, list):
            raise cu.CuError("CU_BAD_ARG", "batch 需要 cmds 数组")
        cont = bool(req.get("continueOnError") or req.get("continue_on_error"))
        results = []
        stopped = False
        t0 = time.time()
        for c in cmds:
            line = str(c).strip()
            if not line or line.startswith("#"):
                # 跳过行单独计数：以前它算进 ok_count，于是"3/3 成功"里有一条
                # 根本没执行——模型会据此判断"三步都过了"。
                results.append({"cmd": line, "ok": True, "skipped": True, "lines": []})
                continue
            try:
                a = self._parse_line(line)
                lines = cu.execute_namespace(a)
                results.append({"cmd": line, "ok": True, "lines": lines})
            except cu.CuError as e:
                results.append({"cmd": line, "ok": False, "code": e.code,
                                "message": str(e)})
                if not cont:
                    stopped = True
                    break
            except Exception as e:                               # noqa: BLE001
                results.append({"cmd": line, "ok": False, "code": "CU_UNEXPECTED",
                                "message": "%s: %s" % (type(e).__name__, e)})
                if not cont:
                    stopped = True
                    break
        return {"results": results, "stopped_early": stopped,
                "ok_count": sum(1 for r in results if r.get("ok") and not r.get("skipped")),
                "skipped_count": sum(1 for r in results if r.get("skipped")),
                "ms": int((time.time() - t0) * 1000)}

    def _eval(self, req):
        code = req.get("code")
        if not isinstance(code, str) or not code.strip():
            raise cu.CuError("CU_BAD_ARG", "eval 需要 code 字符串")
        timeout = float(req.get("timeoutMs") or 20000) / 1000.0
        timeout = max(1.0, min(300.0, timeout))
        import ast
        import contextlib
        import io as _io

        ns = {"cu": cu, "os": os, "sys": sys, "time": time, "json": json,
              "re": __import__("re"), "subprocess": __import__("subprocess"),
              "pathlib": __import__("pathlib"), "math": __import__("math")}
        out, err = _io.StringIO(), _io.StringIO()
        box = {}

        def _run():
            try:
                tree = ast.parse(code, "<eval>", "exec")
                last = None
                if tree.body and isinstance(tree.body[-1], ast.Expr):
                    last = ast.Expression(tree.body.pop().value)
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    exec(compile(tree, "<eval>", "exec"), ns)     # noqa: S102
                    if last is not None:
                        r = eval(compile(last, "<eval>", "eval"), ns)   # noqa: S307
                        if r is not None:
                            print(repr(r))
                box["ok"] = True
            except BaseException as e:                           # noqa: BLE001
                box["ok"] = False
                box["code"] = getattr(e, "code", None) or "CU_EVAL"
                box["message"] = str(e)
                box["traceback"] = traceback.format_exc()[-2000:]

        th = threading.Thread(target=_run, name="cu-eval", daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():
            # 线程杀不掉：如实说明，并给出口（server_restart）。
            return {"timed_out": True, "stdout": out.getvalue(), "stderr": err.getvalue(),
                    "note": "eval 超过 %gs 未返回；线程无法强杀，可用 server_restart 回收"
                            % timeout}
        res = {"stdout": out.getvalue(), "stderr": err.getvalue()}
        if box.get("ok"):
            res["ok"] = True
        else:
            res["ok"] = False
            res["error"] = {"code": box.get("code"), "message": box.get("message"),
                            "traceback": box.get("traceback")}
        return res

    def _shot(self, req):
        """截图：直接回原始 PNG 帧，**不落盘**。
        后端阶梯（PrintWindow → GDI 屏幕裁剪）统一在 cu.capture_shot 里。"""
        class _A:
            pass
        a = _A()
        a.window = req.get("window")
        a.region = req.get("region")
        a.long = req.get("long")
        a.full = bool(req.get("full"))
        png, meta = cu.capture_shot(a)
        vx, vy, vw, vh = cu.screen()
        meta["screen"] = {"x": vx, "y": vy, "width": vw, "height": vh}
        if meta.get("region"):
            x0, y0 = cu.to_norm(meta["region"][0], meta["region"][1])
            x1, y1 = cu.to_norm(meta["region"][2], meta["region"][3])
            meta["region_normalized"] = [round(x0, 1), round(y0, 1),
                                         round(x1, 1), round(y1, 1)]
        return {"meta": meta, "png": png}

    def _state(self):
        self._prune_watchers()
        return {"queues": _queue_names(), "mute_until": cu._muted_until(),
                "now": time.time(),
                "modified_keys": sorted(cu._PRESSED_MODS | cu._HELD),
                "uptime": round(time.time() - self.started, 1),
                "requests": self.requests, "last_error": self.last_error,
                "watchers": [p.pid for p in self.watchers]}


def _queue_names():
    root = os.path.join(cu.SHOT_DIR, "watchq")
    try:
        return sorted(d for d in os.listdir(root)
                      if os.path.isdir(os.path.join(root, d)))
    except OSError:
        return []


# ---------------------------------------------------------------- 服务端主循环
def _lock_path(config=None):
    """单实例锁的路径**按端点派生**。

    早期版本用固定文件名 `dsh-computer-use.lock`，结果一个进程在测试用的
    \\\\.\\pipe\\dsh-cu-test-xxx 上占着锁，另一个本该独立的实例就被判成"已有实例"。
    锁的语义是"同一个端点只能有一个服务"，所以键就该是端点本身。
    """
    import hashlib
    key = (config.endpoint if config is not None else
           (os.environ.get("DSH_CU_ENDPOINT") or PIPE_NAME))
    if config is not None and config.kind == "tcp":
        key = "tcp:127.0.0.1:%d" % config.port
    tag = hashlib.sha1(str(key).encode("utf-8")).hexdigest()[:12]
    return os.path.join(os.environ.get("TEMP", cu.tempfile.gettempdir()),
                        "dsh-computer-use-%s.lock" % tag)


def acquire_single_instance(config=None):
    """锁文件（第二道防线，**不是主判据**）。

    踩过的坑：Windows 上 `msvcrt.locking` 在 "a+b" 模式的文件上并不可靠——
    实测两个进程都能"锁成功"，然后第二个进程在 `truncate/flush` 时才炸
    PermissionError，最后以一个谁也看不懂的 traceback 退出。
    所以主判据改成"端点探测"：连得上、ping 得通就说明已经有人在服务这个端点。
    这里的锁只用来兜住"探测与 bind 之间的竞态"，而且**任何 OSError 都当成
    '别人占着'**，绝不让它把进程炸掉。
    """
    try:
        path = _lock_path(config)
        f = open(path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            f.seek(0)
            f.truncate()
            f.write(str(os.getpid()).encode())
            f.flush()
            return f
        except OSError:
            try:
                f.close()
            except OSError:
                pass
            return None
    except OSError:
        # 锁文件本身建不出来（权限/磁盘）不该挡住服务——探测已经在前面把关了。
        return True


def probe_running(config, timeout=0.6):
    """端点是否已经有活着的服务。用短超时，别让启动等 5 秒。"""
    try:
        old = config.connect_timeout
        config.connect_timeout = timeout
        try:
            return ping(config)
        finally:
            config.connect_timeout = old
    except Exception:                                         # noqa: BLE001
        return None


def serve(config, single=True):
    if single:
        alive = probe_running(config)
        if alive:
            log("端点 %s 已有服务在跑（pid=%s），本进程退出"
                % (config.endpoint, alive.get("pid")))
            print(json.dumps({"ok": False, "code": "CU_IPC",
                              "message": "该端点已有服务在跑（pid=%s）" % alive.get("pid")},
                             ensure_ascii=False))
            return 3
    if config.kind == "named_pipe":
        if not _HAVE_PYWIN32:
            raise SystemExit("GUI 操作不可用：缺少 pywin32，命名管道起不来。装它："
                             "%s -m pip install --user pywin32" % sys.executable)
        return _serve_pipe(config, single)
    if config.kind == "uds":
        try:
            os.unlink(config.endpoint)
        except OSError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(config.endpoint)
        srv.listen(4)
        return _serve_sock(config, srv, single)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", config.port))
    except OSError as e:
        log("绑定端口 %d 失败：%s" % (config.port, e))
        print(json.dumps({"ok": False, "code": "CU_IPC",
                          "message": "端口 %d 已被占用：%s" % (config.port, e)},
                         ensure_ascii=False))
        return 3
    srv.listen(4)
    return _serve_sock(config, srv, single)


def _session(server, conn):
    """一条连接上的请求循环。

    **不重发**：读/写任何一步失败，本次请求直接结束，不在同一条连接上重试；
    连接就此作废，由客户端在**下一个**请求前重连。
    """
    try:
        while not server.stopping:
            try:
                req = recv_json(conn)
            except (EOFError, OSError, ValueError):
                return
            # 锁只包住"处理"，不包住整条连接——否则一个长连接会把
            # 第二个 DSH 实例的 ping 永远挡在门外。
            with server.lock:
                server.handle(conn, req)
    finally:
        conn.close()


def _serve_sock(config, srv, single):
    lock = acquire_single_instance(config) if single else True
    if lock is None:
        log("已有实例在跑，退出")
        print(json.dumps({"ok": False, "code": "CU_IPC",
                          "message": "已有实例在跑（单实例锁被占）"}, ensure_ascii=False))
        return 3
    # 重启 = 重置：静默窗是"刚刚我自己的注入引起的重绘先别当变化"这种**短期**状态，
    # 它持久化在文件里，是为了让 CLI 路径与服务共享同一份。但服务重启之后还留着它，
    # 就变成"没人知道为什么，反正变化被吞了"——误吞真变化的代价远大于多看一帧。
    # CLI 每次注入会自己续租，该有的保护不会丢。
    cu._unmute()
    log("服务启动 transport=%s endpoint=%s" % (config.kind, config.endpoint))
    server = Server(config)
    server.listener = srv
    try:
        while not server.stopping:
            try:
                conn, _ = srv.accept()
            except OSError:
                break
            conn.settimeout(None)
            threading.Thread(target=_session, args=(server, SockConn(conn)),
                             daemon=True).start()
    finally:
        server.listener = None
        srv.close()
        if config.kind == "uds":
            try:
                os.unlink(config.endpoint)
            except OSError:
                pass
    log("服务退出")
    return 0


def _serve_pipe(config, single):
    lock = acquire_single_instance(config) if single else True
    if lock is None:
        log("已有实例在跑，退出")
        print(json.dumps({"ok": False, "code": "CU_IPC",
                          "message": "已有实例在跑（单实例锁被占）"}, ensure_ascii=False))
        return 3
    # 重启 = 重置（理由同 _serve_sock 里那段：旧的静默窗会无声吞掉真变化）
    cu._unmute()
    log("服务启动 transport=named_pipe endpoint=%s" % config.endpoint)
    server = Server(config)
    # 每个连接一个线程、并**立刻**再建一个管道实例：
    # 只留一个实例的话，第一条长连接会把第二个客户端的 ConnectNamedPipe 堵死。
    while not server.stopping:
        h = win32pipe.CreateNamedPipe(
            config.endpoint,
            win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
            win32pipe.PIPE_UNLIMITED_INSTANCES, 65536, 65536, 0, None)
        server.listener = h
        try:
            win32pipe.ConnectNamedPipe(h, None)
        except Exception as e:                                # noqa: BLE001
            log("ConnectNamedPipe 失败：%s" % e)
            server.listener = None
            try:
                win32file.CloseHandle(h)
            except Exception:
                pass
            continue
        threading.Thread(target=_session, args=(server, PipeConn(h)),
                         daemon=True).start()
    server.listener = None
    log("服务退出")
    return 0


# ---------------------------------------------------------------- 客户端（测试 / --rpc / 降级用）
class Client:
    def __init__(self, config=None):
        self.config = config or TransportConfig.from_env()
        self.conn = None
        self.seq = 0
        self._unrecoverable = False
        self.lock = threading.Lock()

    def connect(self):
        with self.lock:
            if self.conn is not None:
                return
            self.conn = connect(self.config)

    def close(self):
        with self.lock:
            if self.conn is not None:
                self.conn.close()
                self.conn = None

    def request(self, op, binary=False, timeout=None, **payload):
        with self.lock:
            if self._unrecoverable:
                raise cu.CuError("CU_IPC", "上一次不可恢复的失败之后，本客户端已作废；请新建一个")
            if self.conn is None:
                self.conn = connect(self.config)
            self.seq += 1
            payload["id"] = self.seq
            payload["op"] = op
            try:
                send_json(self.conn, payload)
                resp = recv_json(self.conn)
                png = None
                if binary or payload.get("wantBinary"):
                    kind, data = read_frame(self.conn)
                    if kind == KIND_BINARY:
                        png = data
                return resp, png
            except (EOFError, OSError, ValueError) as e:
                # 写/读失败后**不重发**：对端可能已经执行了。只标记不可恢复并断开。
                self._unrecoverable = True
                try:
                    self.conn.close()
                except Exception:
                    pass
                self.conn = None
                raise cu.CuError("CU_IPC", "与常驻服务通信失败（%s）；本次请求不会重发" % e)


def ping(config=None):
    """探测是否已有服务在跑。返回 ping 字典或 None。"""
    try:
        c = Client(config)
        resp, _ = c.request("ping")
        c.close()
        if resp.get("ok"):
            return resp.get("value") or {}
        return None
    except Exception:                                         # noqa: BLE001
        return None


# ---------------------------------------------------------------- --rpc 降级模式
def rpc_loop():
    """行协议降级模式：一行一个 JSON 请求（stdin），一行一个 JSON 响应（stdout）。
    图片走 base64。给"管道建不起来"的极端情况留一条活路——慢，但能用。"""
    import base64
    server = Server(TransportConfig.from_env())
    out = sys.stdout.buffer

    class _MemoryConn:
        def __init__(self):
            self.frames = []

        def write_all(self, data):
            self.frames.append(data)

    while True:
        raw = sys.stdin.buffer.readline()
        if not raw:
            break
        try:
            req = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            out.write(json.dumps({"ok": False, "code": "CU_PROTOCOL",
                                  "message": "请求不是合法 JSON"},
                                 ensure_ascii=False).encode("utf-8") + b"\n")
            out.flush()
            continue
        conn = _MemoryConn()
        try:
            server.handle(conn, req)
        except Exception as e:                                # noqa: BLE001
            log("rpc 异常：%s" % e)
        for frame in conn.frames:
            out.write(base64.b64encode(frame) + b"\n")
        out.flush()
        if server.stopping:
            break
    return 0


# ---------------------------------------------------------------- 入口
def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if "--rpc" in args:
        return rpc_loop()
    config = TransportConfig.from_env()
    if "--print-config" in args:
        print(json.dumps({"kind": config.kind, "endpoint": config.endpoint,
                          "port": config.port, "fallback": config.fallback_kind,
                          "fallback_port": config.fallback_port}, ensure_ascii=False))
        return 0
    if "--ping" in args:
        p = ping(config)
        print(json.dumps(p or {"ok": False, "message": "没有在跑的服务"}, ensure_ascii=False))
        return 0 if p else 1
    single = "--allow-multi" not in args
    try:
        return serve(config, single=single)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
