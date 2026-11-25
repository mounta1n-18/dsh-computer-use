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
import ctypes
import json
import os
import re
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
SERVER_VERSION = "1.1.0"
PIPE_NAME = r"\\.\pipe\dsh-computer-use"

KIND_JSON = 0
KIND_BINARY = 1

MAX_JSON = 8 * 1024 * 1024
MAX_BINARY = 32 * 1024 * 1024

DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_IO_TIMEOUT = 30.0
# 对齐豆包 client.py：主传输有兜底时只给 0.5s 的尝试窗口，免得 5s 全耗在死路上
IPC_CONNECT_TIMEOUT_WITH_FALLBACK = 0.5

# 本次使用 cu 的强度：让用户一眼看出这台电脑接下来会被怎么用。
#   script_main = 主要脚本实现，键鼠零星使用
#   unknown     = 我不知道，视情况而定
#   gui_main    = 主要键鼠实现，脚本零星使用
INTENSITY_LABELS = {
    "script_main": "主要脚本实现，键鼠零星使用",
    "unknown": "我不知道，视情况而定",
    "gui_main": "主要键鼠实现，脚本零星使用",
}

# 控制权是怎么没的：agent 拿到 closed 时必须同时拿到原因，否则它会拿激活文案里的
# "空闲 N 秒"当答案（2026-11-20 实测：用户按了急停，模型却告诉用户"闲置了 600 秒"）。
_STOP_DETAIL = {
    "emergency": "用户按了急停（Ctrl+Alt+Shift+Backspace）",
    "deactivate": "agent 主动交还了控制权",
    "idle-timeout": "控制权因长时间没有操作被系统收回",
    "max-ttl": "单次激活到达时长上限，控制权已收回",
    "turn-end": "本回合结束，cu 随之关闭",
    "manual": "用户手动关闭了 cu",
    "stop": "cu 已关闭",
}

# 客户端推来的阶段词 -> 上屏词。原则：只留三个稳定词，细节一律并进粗类，
# 免得模型两秒一步、屏幕上一秒变一次，用户读不过来。
_PHASE_WORD = {
    "操作中": "操作", "观察中": "操作", "后台执行中": "操作", "申请中": "操作",
    "思考中": "思考", "等待中": "等待", "提问中": "等待",
}


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


def recover_held_on_boot():
    """上一个进程可能是在拖拽途中被强杀的：按它留下的账本把**它自己按下的**键松回去。

    只在有账本时才动手；账本里没有的东西一律不碰（绝不猜"可能卡住了"）。"""
    try:
        released = cu.recover_held()
    except Exception:                             # noqa: BLE001
        released = []
    if released:
        log("补松上一个进程残留的按键：%s" % "、".join(released))
    return released


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


def _trace(msg):
    """DSH_CU_TRACE=1 时的连接级流水，用来定位"客户端只看到 EOF"这类没有异常的问题。"""
    if os.environ.get("DSH_CU_TRACE"):
        log("trace: " + msg)


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


# ---------------------------------------------------------------- 桌面提示层
class Overlay:
    """overlay.py 的子进程句柄：按需拉起、崩溃自动重启、stdout 事件回调。"""

    def __init__(self, on_event):
        self.on_event = on_event
        self.proc = None
        self._errlog = None
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.last_error = None

    def start(self):
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return True
            self.ready.clear()
            path = os.path.join(HERE, "overlay.py")
            flags = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW
            # 提示层的 stderr 落盘：它一崩，屏幕上就没有提示了，必须能查为什么。
            try:
                self._errlog = open(os.path.join(_home_dir(), "overlay.err"), "ab", buffering=0)
            except OSError:
                self._errlog = subprocess.DEVNULL
            try:
                self.proc = subprocess.Popen(
                    [sys.executable, path], cwd=HERE,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=self._errlog,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    creationflags=flags)
            except OSError as e:
                self.last_error = "起提示层失败：%s" % e
                log(self.last_error)
                return False
            threading.Thread(target=self._read, args=(self.proc,),
                             name="cu-overlay-read", daemon=True).start()
        ok = self.ready.wait(6.0)
        if not ok:
            self.last_error = "提示层 6 秒内没有就绪"
            log(self.last_error)
        return ok

    def _read(self, proc):
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("event") == "ready":
                    self.ready.set()
                elif msg.get("event") == "error":
                    self.last_error = msg.get("message")
                    log("提示层报错：%s" % msg.get("message"))
                else:
                    try:
                        self.on_event(msg)
                    except Exception:                        # noqa: BLE001
                        log("提示层事件处理失败：%s" % traceback.format_exc())
        except (OSError, ValueError):
            pass
        finally:
            self.ready.clear()

    def alive(self):
        """提示层进程是否还活着。

        提示层的边框/鲸鱼/横幅都是这个进程的窗口，进程一死窗口就没了，
        所以"进程活着"与"屏幕上还有提示"在这里是同一件事——不需要额外协议。
        """
        proc = self.proc
        return proc is not None and proc.poll() is None

    def send(self, **msg):
        for attempt in (1, 2):
            if attempt == 2 and not self.start():
                return False
            proc = self.proc
            if proc is None or proc.poll() is not None:
                if attempt == 1:
                    continue
                return False
            try:
                with self.lock:
                    proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
                    proc.stdin.flush()
                return True
            except (OSError, ValueError):
                self.stop()
        return False

    # ---- 对话框：独立进程 ----
    # 对话框必须跟渲染进程分开：渲染线程 40fps 一直在 UpdateLayeredWindow 上刷，
    # 同一个进程里再跑一个 Tk 窗口，Windows 那边只给它一块黑底（实测）。
    # 独立进程还顺带解决"用户点完就关"与"渲染层崩溃"互不影响。
    def dialog(self, title, hint, on_message, on_closed=None):
        self.close_dialog()
        path = os.path.join(HERE, "overlay.py")
        flags = 0x08000000 if os.name == "nt" else 0
        spec = json.dumps({"title": title, "hint": hint}, ensure_ascii=False)
        try:
            proc = subprocess.Popen(
                [sys.executable, path, "--dialog", spec], cwd=HERE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=flags)
        except OSError as e:
            self.last_error = "起对话框失败：%s" % e
            log(self.last_error)
            return False
        self.dialog_proc = proc
        log("对话框已启动 pid=%s" % proc.pid)

        def drain_err():
            try:
                err = proc.stderr.read()
            except (OSError, ValueError):
                return
            if err.strip():
                log("对话框退出码=%s stderr=%s" % (proc.returncode, err.strip()[-1200:]))

        threading.Thread(target=drain_err, name="cu-dialog-err", daemon=True).start()

        def read():
            try:
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("event") == "message":
                        on_message(str(msg.get("text") or ""))
                    elif msg.get("event") == "dialog_closed":
                        if on_closed:
                            on_closed()
            except (OSError, ValueError):
                pass

        threading.Thread(target=read, name="cu-dialog-read", daemon=True).start()
        return True

    def close_dialog(self):
        proc = getattr(self, "dialog_proc", None)
        self.dialog_proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def stop(self):
        self.close_dialog()
        proc = self.proc
        self.proc = None
        if self._errlog not in (None, subprocess.DEVNULL):
            try:
                self._errlog.close()
            except OSError:
                pass
        self._errlog = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                    proc.stdin.flush()
                except (OSError, ValueError):
                    pass
                proc.wait(timeout=1.5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:                pass


# ---------------------------------------------------------------- 请求处理
class NullOverlay:
    """不画任何东西的提示层替身（DSH_CU_OVERLAY=0）。

    自动化测试要的是"状态对不对"，不是"画得好不好"；用户也可能明确不想看到浮层。
    接口与 Overlay 保持一致，调用方不用改。
    """

    def start(self):
        return True

    def alive(self):
        # 测试替身：这里返回 True 表示"本轮测试不计较屏幕证据"。
        # 生产环境走真实 Overlay（进程活着 ⇔ 窗口在），不会走到这里。
        return True

    # `control` / 激活失败文案里会读这几个字段（真实 Overlay 用它们报告提示层健康度）
    proc = None
    ok = None
    last_error = None

    def send(self, **msg):
        return False

    def stop(self):
        return None

    def dialog(self, *a, **kw):
        return False

    def close_dialog(self):
        return None


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

        # ---- 控制权 ----
        self.control = cu._CONTROL
        self.control.enabled = True         # 服务模式下默认关闸：不激活什么都干不了
        self.control_lock = threading.RLock()
        self.pending = None
        # DSH_CU_CONSENT=auto 仅供自动化测试：不弹横幅直接同意。
        # 默认 user：必须用户按热键/点按钮，30 秒无响应按用户拍板"默认同意"。
        self.consent_mode = (os.environ.get("DSH_CU_CONSENT") or "user").strip().lower()
        # DSH_CU_OVERLAY=0：完全不起提示层（自动化测试、或用户明确不想看到任何浮层时用）。
        self.overlay = NullOverlay() if os.environ.get("DSH_CU_OVERLAY") == "0" else Overlay(self._overlay_event)
        self.hotkey_ok = None
        self.input_hook_ok = None
        self._hotkey_prev = {"emergency": False, "agree": False}
        self.notices = []                   # 待推给 agent 的提示：[{id, text}]
        self._notice_seq = 0                # 提示 id：客户端 ack 之后才删
        # 新实例不继承旧会话的留言/停机说明：上一轮死了就死了，不能让新会话一上来
        # 就收到"你被关了""用户手动关闭"这种过期的病话（用户 2026-11-20 要求）。
        self.inbox = []
        try:
            if self._inbox_load():
                log("丢弃上次遗留的留言（新实例不继承旧会话）")
            self._inbox_save()
        except Exception:                             # noqa: BLE001
            pass
        self.user_msgs = []                 # 给 agent.followup() 用的留言队列
        self.inflight = {}                  # 已认领、等 ack 的留言：id -> {item, at}
        self.msg_evt = threading.Event()    # 新留言 / cu 关闭时唤醒 `wait` 长轮询
        self._user_msg_seq = 0
        self.last_user_input = 0.0
        self._user_input_evt = threading.Event()
        self.session = ""                   # 当前持租约的会话（横幅上告诉用户"是哪个会话"）
        self.session_title = ""             # 用户给这个会话起的名字（可在面板里改）
        self.cwd = ""                       # 该会话的工作目录（只取末段显示）
        # 右下角鲸鱼的状态字 / 呼吸环颜色：由 agent 的调用阶段驱动。
        self._logo_state = "active"
        self._logo_label = None
        self._phase_at = 0.0
        self._logo_color = None
        # 低频状态机：屏幕上显示的词、上次换词时间、待确认的候选词。
        self._label = "待命"
        self._label_at = time.time()
        self._cand = None
        self._cand_at = 0.0
        # 留言投递反馈（绿/红环 + 短词），3 秒内优先于普通状态显示。
        self._relay_label = ""
        self._relay_color = ""
        self._relay_until = 0.0
        # 上一次"权威状态"推给提示层时的快照；每秒对账一次，防止漏发/旧进程画错。
        self._pushed = None
        # 心跳：最后一次"确实有动静"的时刻（动作/阶段字/留言/回复都会刷新它）。
        # 只记时间戳，零开销；用来识别"捏着租约但一步没走"的卡死。
        self._beat_at = time.time()
        self._logo_sweep = True
        # agent 亲手写的自我介绍 + 本次使用强度：activate 必填，用户点同意之前就要看见。
        self.intro = ""
        self.intensity = ""
        # 用户从递话框发来、agent 还没 reply 的那句话。非空时注入被 CU_UNANSWERED 拦住。
        self.pending_reply = None           # dict(id, text, at)
        # 控制权是怎么没的：agent 只能看到 closed:true，不告诉它原因它就会自己编
        # （"闲置 600 秒"就是这么来的）。reason 取值：emergency/deactivate/idle-timeout/
        # turn-end/expired/manual，另有 detail 给人看的一句话。
        self.stop_reason = ""
        self.stop_detail = ""
        self.revoke_reason = ""
        self.revoke_detail = ""
        self.dialog_open = False
        # 生产语义：交还/急停之后 cu 要彻底退出，不留后台。测试里设
        # DSH_CU_STAY_ALIVE=1 让它活着，好连续断言状态机。
        self.stay_alive = os.environ.get("DSH_CU_STAY_ALIVE") == "1"
        threading.Thread(target=self._control_ticker, name="cu-control-ticker",
                         daemon=True).start()
        threading.Thread(target=self._hotkey_loop, name="cu-hotkeys",
                         daemon=True).start()
        threading.Thread(target=self._input_hook_loop, name="cu-input-hook",
                         daemon=True).start()
        threading.Thread(target=self._notice_loop, name="cu-user-input",
                         daemon=True).start()
        threading.Thread(target=self._visual_watch, name="cu-visual-watch",
                         daemon=True).start()
        # 注入总闸也认这条：没有提示层就不许动键鼠（见 cu.Control.check）。
        cu._CONTROL.visual_check = self._visual_ok
        # 提示层在服务启动时就拉起来，而不是等第一次 activate 才 spawn：
        # ① 激活那一刻不必等它启动（窗口立刻出现）；② 避免在"正在处理请求"的
        # 连接线程里创建子进程（实测会让同一条长连接上的响应永远到不了客户端）。
        threading.Thread(target=self.overlay.start, name="cu-overlay-boot",
                         daemon=True).start()

    # ---- 可见性 ----
    def _visual_ok(self):
        """屏幕上此刻能不能看见"我们正在控制"的提示。

        两个条件都要成立：提示层进程活着（窗口在），且前台不是独占全屏
        （那种情况下窗口画了也被盖住，用户看不见）。
        """
        if not self.overlay.alive():
            return False
        try:
            return not cu.foreground_occludes_overlay()
        except Exception:                             # noqa: BLE001
            return True

    def _visual_watch(self):
        """提示层一旦不可见而我们还握着控制权，就立刻收工——绝不让"没提示"与
        "还在控制"同时成立。"""
        bad = 0
        while not self.stopping:
            time.sleep(0.5)
            try:
                if not self.control.lease:
                    bad = 0
                    continue
                if self._visual_ok():
                    bad = 0
                    continue
                bad += 1
                if bad >= 2:                          # 连续两次（约 1 秒）才算数，避开瞬时切换
                    log("提示层不可见，立即收工（提示消失就必须停止控制）")
                    self._push_notice("【已自动关闭】屏幕上看不到控制提示了，"
                                      "所以控制已立刻停止（这是硬规则）。")
                    self._full_stop("visual-lost")
                    return
            except Exception:                         # noqa: BLE001
                log("可见性巡检异常：%s" % traceback.format_exc())

    # ---- 提示层事件 ----
    def _overlay_event(self, msg):
        ev = msg.get("event")
        if ev in ("agree", "deny"):
            self._resolve_pending(ev)
        elif ev == "logo_click":
            self._on_logo_click()
        elif ev == "message":
            self._on_user_message(str(msg.get("text") or "")[:2000])
        elif ev == "dialog_closed":
            pass

    # ---- 存在感层（边框 / 光晕 / logo / 横幅）----
    def _session_line(self, owner=None):
        """横幅第二行：哪个会话在手、在哪个目录、这次用多大强度的键鼠。"""
        # 用用户在面板里给会话起的名字，而不是 session 号——用户看不懂 UUID。
        s = self.session_title or self.session or ""
        cwd = self.cwd or ""
        bits = []
        if s:
            bits.append("会话 %s" % s)
        if cwd:
            bits.append("目录 %s" % (os.path.basename(cwd.rstrip("\\/")) or cwd))
        if self.intensity in INTENSITY_LABELS:
            bits.append("强度 %s" % INTENSITY_LABELS[self.intensity])
        return " · ".join(bits) or "（未上报会话）"

    def _logo(self, state=None, label=None, color=None, sweep=None):
        """右下角鲸鱼：状态字 + 呼吸环颜色。

        color=None 表示"没改"；color="" 表示回到状态默认色（蓝/灰）。
        sweep=False 关掉状态词上的光泽——只有"确实在有序跑"时才让它扫。
        """
        if state is not None:
            self._logo_state = state
        if label is not None:
            self._logo_label = label or None
        if color is not None:
            self._logo_color = color or None
        if sweep is not None:
            self._logo_sweep = bool(sweep)
        self.overlay.send(cmd="logo", on=True, state=self._logo_state,
                          label=self._logo_label, color=self._logo_color,
                          sweep=self._logo_sweep)

    def _show_active(self, owner, banner_seconds=5):
        # 新一轮激活：上一次"为什么关"的账不能带到这一轮
        self.stop_reason = self.stop_detail = ""
        self.revoke_reason = self.revoke_detail = ""
        self._label, self._label_at, self._cand = "待命", time.time(), None
        self.overlay.send(cmd="border", on=True, color="#4D6BFE", alpha=0.42)
        self.overlay.send(cmd="halo", on=True)
        self._logo(state="active")
        sub = self._session_line()
        self.overlay.send(
            cmd="banner",
            text=("「%s」正在控制这台电脑" % self.intro) if self.intro else "正在控制这台电脑",
            sub=sub,
            note="手动关闭 Ctrl+Alt+Shift+Backspace · 暂停 Ctrl+Shift+Alt",
            seconds=int(banner_seconds), buttons=False, tone="active")
        if banner_seconds:
            self._later(banner_seconds + 0.2, lambda: self.overlay.send(cmd="hide_banner"))
        self._pushed = None
        self._beat_status()

    def _show_paused(self):
        """暂停态：灰边 + 鲸鱼状态字，不占顶部横幅。

        顶部只留给两类信息：① 启动申请（同意/拒绝）；② 关闭提示（手动关闭 /
        自动关闭）。暂停、递话、提问一律只在右下角鲸鱼和它的弹窗里表达，
        同一条信息不重复显示两遍。
        """
        self.overlay.send(cmd="border", on=True, color="#8A8F98", alpha=0.32)
        self.overlay.send(cmd="halo", on=False)
        self._logo(state="paused", label="", color="")
        self.overlay.send(cmd="hide_banner")
        self._pushed = None
        self._beat_status()

    def _show_idle(self):
        """交还/急停之后：所有特效立刻消失，用户可确定现在轮到自己了。"""
        self._pushed = None
        self.overlay.send(cmd="off")

    def _want_status(self):
        """提示层此刻**应该**是什么样：(state, label, 边框色, 要不要光晕)。

        这是唯一的权威口径：谁都不去猜"屏幕上现在画着什么"，只问租约和暂停位。
        None 表示此刻不该有任何提示。
        """
        if not self.control.lease:
            return None
        now = time.time()
        if self.control.paused:
            # 暂停有三种原因，状态词必须说清是哪一种，否则用户只知道"停了"。
            if self.dialog_open:
                label = "询问"
            elif self.pending_reply:
                label = "待答"
            else:
                label = "暂停"
            return ("paused", label, "#8A8F98", False, False)
        if now < self._relay_until and self._relay_label:
            return ("active", self._relay_label, self._relay_color or "#4D6BFE", True, True)
        # 心跳：最后一条动静（动作、阶段字、留言、回复）有多久了。超过 STALL_SECS
        # 还捏着租约却一步没走，多半是模型那头卡住/网络断了——把"卡住"显示出来，
        # 并且关掉光泽（静止的界面不该假装还在动）。
        gap = now - self._beat_at
        if gap > self.STALL_SECS:
            return ("active", "停滞 %.0fs" % gap, "#4D6BFE", True, False)
        return ("active", self._label, "#4D6BFE", True, True)

    def _beat_status(self):
        """每秒和提示层对一次账（幂等）。

        为什么需要：阶段字是事件驱动的，漏一条（工具异常、进程重启、旧提示层
        还活着）屏幕上就会停在错误状态——用户见过"蓝边框 + 已暂停"。
        这里以权威状态为准整套重发，不依赖任何一次事件成功送达。
        """
        want = self._want_status()
        if want == self._pushed:
            return
        self._pushed = want
        if want is None:
            self.overlay.send(cmd="off")
            return
        state, label, color, halo, sweep = want
        self.overlay.send(cmd="border", on=True, color=color,
                          alpha=0.42 if state == "active" else 0.32)
        self.overlay.send(cmd="halo", on=halo)
        self._logo(state=state, label=label, color="", sweep=sweep)

    # ---- 暂停 / logo 留言 ----
    def _pause_toggle(self, why="hotkey"):
        """暂停 = 保留租约但停止注入；再按一次恢复。急停态按这个不起作用。"""
        if self.control.emergency:
            return False
        if not self.control.lease:
            log("暂停键：当前没有控制权，忽略")
            return False
        on = not self.control.paused
        self.control.set_paused(on)
        self.control.touch()  # 用户在操作 = 有交互，续上空闲计时
        cu.audit("control", {"owner": (self.control.lease or {}).get("owner"),
                             "why": why}, "paused" if on else "resumed")
        if on:
            self._push_notice("用户已暂停 cu，请静默等待用户操作和指令。"
                              "不要重试刚失败的动作，也不要自己找活干。")
            self._show_paused()
        else:
            # 用户自己按回来 = 他不再等这句回答，未答的留言一并放行，免得永远卡住。
            with self.control_lock:
                self.control.pending_reply = False
                self.pending_reply = None
            self._push_notice("cu 已恢复，观察屏幕并继续操作。")
            owner = (self.control.lease or {}).get("owner") or "代理"
            self._show_active(owner, banner_seconds=3)
        log("控制权%s（%s）" % ("已暂停" if on else "已恢复", why))
        return True

    def _ask(self, req):
        """模型主动向用户要东西（密码、验证码、选哪个按钮）。

        与"点鲸鱼"同一个通道，只是由这一侧发起：先把 cu 挂起（不是关掉、也不是
        结束会话），问题只写在右下角的递话框里（顶部横幅不重复显示），
        用户答完自动恢复并继续。
        """
        if self.control.emergency:
            raise cu.CuError("CU_EMERGENCY_STOP", "")
        if not self.control.lease:
            raise cu.CuError("CU_NOT_ACTIVATED", "")
        text = str(req.get("text") or "").strip()[:400]
        if not text:
            raise cu.CuError("CU_BAD_ARG", "ask 需要 text：要说清需要用户做什么")
        self.control.set_paused(True, "ask")
        self.control.touch()
        cu.audit("control", {"text": text[:200]}, "asked")
        self._show_paused()
        self.dialog_open = True
        try:
            self.overlay.dialog("需要你配合一步", text, self._on_user_message,
                                lambda: setattr(self, "dialog_open", False))
        except Exception as e:  # 对话框起不来也不能把暂停撤销
            log("主动提问的对话框起不来：%r" % (e,))
        log("向用户提问：%s" % text[:120])
        return {"asked": True, "text": text, "paused": True}

    def _on_logo_click(self):
        """右下角鲸鱼：控制中 → 暂停并弹出留言框；已暂停 → 只弹留言框，不解除暂停。

        用户的意思很明确：暂停是他按的，恢复必须由他按回来；点鲸鱼只是"我想说句话"。
        """
        with self.control_lock:
            if not self.control.lease or self.control.emergency:
                return
            paused = self.control.paused
        if not paused:
            with self.control_lock:
                self.control.set_paused(True)
                self.control.touch()
                cu.audit("control", {}, "paused_for_chat")
            self._show_paused()
        self.dialog_open = True
        title = "对「%s」说点什么" % (self.intro or "这个会话")
        try:
            self.overlay.dialog(
                title,
                "已暂停鼠标键盘操作。你说完，agent 会先回你一句再继续；"
                "想自己恢复就按 Ctrl+Shift+Alt。",
                self._on_user_message,
                lambda: setattr(self, "dialog_open", False))
        except Exception as e:  # 对话框起不来不能把 overlay 读线程带走
            log("对话框起不来：%r" % (e,))

    def _on_user_message(self, text, from_dialog=False):
        """用户在递话框里说了话：保持暂停，等 agent 用 reply 回一句。

        规则（用户 2026-11-18 要求）：工具侧来的话必须从工具侧回答；在回答之前
        不许动键鼠，防止 agent 一边"没听见"一边继续干活。
        """
        self.dialog_open = False
        if not text.strip():
            return
        text = text.strip()[:2000]
        self._user_msg_seq += 1
        mid = self._user_msg_seq
        with self.control_lock:
            if self.control.lease and not self.control.emergency:
                self.control.set_paused(True, "waiting_reply")
                self.control.pending_reply = True
                self.pending_reply = {"id": mid, "text": text, "at": time.time()}
                self.control.touch()
        self.user_msgs.append({"id": mid, "text": text, "at": time.time(),
                               "owner": (self.control.lease or {}).get("owner") or ""})
        self._push_notice(
            "用户从右下角递话框说：%s\n"
            "—— 先用 action=\"reply\" 回一句（他会看到你的话），"
            "回完才能继续操作电脑；在那之前动键鼠会被拦。" % text,
            persist=True)
        self.msg_evt.set()
        with cu.control_bypass():
            cu.release_all()
        cu.audit("control", {"text": text[:200]}, "user_message")
        if self.control.lease and not self.control.emergency:
            self._show_paused()
            self._logo(state="paused", label="待回复")
        log("用户留言：%s" % text[:120])

    def _reply(self, req):
        """agent 回答用户在递话框里说的话：把原话显示在右下角，再解除暂停。

        text 允许为空：等于"我看过了，不需要回答"——同样解除暂停，但不弹回执卡，
        只在审计里留一条记录（用户 2026-11-18 拍板）。
        """
        text = str(req.get("text") or "").strip()[:800]
        with self.control_lock:
            pending = self.pending_reply
            if not pending:
                raise cu.CuError("CU_BAD_ARG", "当前没有等你回答的用户消息；不用 reply")
            self.pending_reply = None
            self.control.pending_reply = False
            owner = (self.control.lease or {}).get("owner") or ""
            self.control.set_paused(False)
            self.control.touch()
        # 回执卡放在右下角，贴着鲸鱼：用户刚在那儿说话，回答也在那儿出现。
        if text:
            self.overlay.send(cmd="note", text=text, seconds=8)
        cu.audit("control", {"text": text[:200]}, "replied" if text else "replied_silent")
        if text:
            self._push_notice("已回复用户：「%s」。继续工作。" % text)
        else:
            self._push_notice("已跳过回复（回复为空），暂停已解除，继续工作。")
        if owner:
            self._show_active(owner, banner_seconds=3)
            # 送达闭环：模型已经回了话 → 绿环亮 3 秒（回执卡同时出现），
            # 下一次操作时 _cmd 会把颜色复位。
            self._logo(state="active", label="已回复", color="#34C759")
        log("回复用户：%s" % (text[:120] if text else "（空，不弹窗）"))
        return {"replied": True, "to": pending["text"][:200], "text": text, "paused": False,
                "control": self.control.status()}

    def _resume(self, req):
        """显式恢复：只解暂停，不碰租约（agent 不该用 deactivate 来"恢复"）。"""
        if self.control.emergency:
            raise cu.CuError("CU_EMERGENCY_STOP", "")
        if not self.control.lease:
            raise cu.CuError("CU_NOT_ACTIVATED", "")
        if self.pending_reply:
            raise cu.CuError("CU_UNANSWERED", "")
        if not self.control.paused:
            return {"resumed": False, "note": "本来就没暂停"}
        self.control.set_paused(False)
        self.control.touch()
        owner = (self.control.lease or {}).get("owner") or ""
        self._show_active(owner, banner_seconds=3)
        self._push_notice("cu 已恢复，观察屏幕并继续操作。")
        cu.audit("control", {"why": "resume"}, "resumed")
        return {"resumed": True, "paused": False}

    def _phase(self, req):
        """鲸鱼下方的状态词：思考 / 操作 / 等待。

        这些阶段只有 agent 侧知道（它在想还是在点），所以由客户端按调用阶段推过来。
        只做一次粗粒度归并（7 个细分词 → 3 个），然后**立刻上屏**：
        用户要的是实时，不是"不闪"；防跳变交给状态词上的光束特效去提示"它还在动"。
        """
        if not self.control.lease or self.control.paused:
            return {"phase": None}
        raw = str(req.get("label") or "")[:10]
        label = _PHASE_WORD.get(raw, raw)
        self._beat()
        self._phase_at = time.time()
        if label and label != self._label:
            self._label = label
            self._label_at = self._phase_at
            # 立刻上屏：不靠下一次 ticker（1s 粒度）追，用户看到的是实时状态。
            self._beat_status()
        return {"phase": self._label}

    def _beat(self):
        """零开销心跳：任何"确实往前走了一步"的事件都调用它。"""
        self._beat_at = time.time()

    def _stable_label(self, now):
        """保留旧接口：直接返回当前词（迟滞已按用户要求移除）。"""
        return self._label

    def _relay(self, req):
        """用户留言的递送状态：sent=送达（绿），failed=失败（红），clear=复位。"""
        st = str(req.get("state") or "").lower()
        if st == "sent":
            self._relay_label, self._relay_color = "送达", "#34C759"
        elif st == "failed":
            self._relay_label, self._relay_color = "失败", "#FF3B30"
        else:
            self._relay_label, self._relay_color = "", ""
        self._relay_until = time.time() + 3.0 if self._relay_label else 0.0
        self._logo(color=self._relay_color, label=self._relay_label)
        self._pushed = None
        return {"relay": st or "clear"}

    # ---- 用户同意 ----
    def _resolve_pending(self, decision):
        with self.control_lock:
            p = self.pending
            if not p:
                return False
            if p["decision"] is None:
                p["decision"] = decision
            p["event"].set()
            return True

    def _wait_pending(self, timeout=31.0):
        """等用户决定；等不到按"默认同意"处理（用户 2026-11-09 拍板）。"""
        with self.control_lock:
            p = self.pending
            if p is None:
                return "deny"
        got = p["event"].wait(timeout)
        with self.control_lock:
            decision = p["decision"]
        if decision:
            return decision
        return "agree" if not got else "deny"

    @staticmethod
    def _later(sec, fn):
        """延时动作（横幅自动消失）。必须是 daemon，否则主进程退出会被它拖住。"""
        t = threading.Timer(float(sec), fn)
        t.daemon = True
        t.start()
        return t

    # ---- 控制权 op ----
    def _lease_view(self, lease):
        v = self.control.status()
        v["since"] = round(time.time() - lease["started"], 1) if lease else 0
        v["intro"] = self.intro
        v["intensity"] = self.intensity
        v["intensity_label"] = INTENSITY_LABELS.get(self.intensity, "")
        v["pending_reply"] = bool(self.pending_reply)
        return v

    def _activate(self, req):
        # 没有提示层就没有"正在控制"的证据：这一步过不了，后面一律不授权。
        if not self.overlay.start():
            raise cu.CuError(
                "CU_NO_OVERLAY",
                "提示层起不来（python/overlay.py），所以不能在屏幕上没有任何提示的情况下控制电脑")
        # 身份以会话 id 为准：label 只是调用方自报的名字，容易写成"主 agent"这种废话。
        # 只有拿不到 session 时才退回 label。
        self.session = str(req.get("session") or "")[:120]
        # 会话在客户端里显示的名字（用户可随时改），横幅优先用它。
        self.session_title = " ".join(str(req.get("title") or "").split())[:40]
        owner_id = str(req.get("agent") or req.get("session") or "")[:120]
        owner = (self.session or str(req.get("owner") or req.get("label") or "").strip()[:80]
                 or "未命名会话")
        self.cwd = str(req.get("cwd") or "")[:260]

        # 自我介绍：用户要能一眼分清"是哪个会话要动我的电脑"，所以不许拿 session id
        # 或"主 agent"这种称呼糊弄过去。必须 agent 自己写一句话，连同使用强度一起
        # 上报；这两样东西先上屏、用户同意之后才放开 cu。
        intro = str(req.get("intro") or "").strip()
        intro = " ".join(intro.split())[:60]
        intensity = str(req.get("intensity") or "").strip().lower()
        # 自动化测试里没人在看横幅，省掉这两样会逼着每个老用例改一遍；
        # 生产（不设 DSH_CU_TEST）保持强制，缺了照样 CU_NEED_INTRO。
        if os.environ.get("DSH_CU_TEST") == "1":
            intro = intro or "自动化测试会话"
            intensity = intensity or "unknown"
        if len(intro) < 4:
            raise cu.CuError("CU_NEED_INTRO",
                             'activate 必须带 intro：用一句话写清"你是哪个会话、正在做什么"，'
                             "例如 intro=\"课程表整理：把 Excel 里的课表抄进日历\"。"
                             "不要写 session id 或「主 agent」。")
        if re.fullmatch(r"[0-9a-fA-F\-:\s]{8,}", intro) or intro.lower().startswith("session"):
            raise cu.CuError("CU_NEED_INTRO",
                             "intro 不能是 session id 这类机器标识；请写人话，让用户知道你是谁、要做什么。")
        if self.session and intro.replace("-", "") in self.session.replace("-", ""):
            raise cu.CuError("CU_NEED_INTRO", "intro 不能直接抄 session id；请写人话。")
        if intensity not in INTENSITY_LABELS:
            raise cu.CuError("CU_NEED_INTRO",
                             "activate 必须带 intensity，三选一：script_main（主要脚本实现，键鼠零星使用）/ "
                             "unknown（我不知道，视情况而定）/ gui_main（主要键鼠实现，脚本零星使用）。")
        self.intro, self.intensity = intro, intensity
        for key, attr, lo, hi in (("idle_ttl", "idle_ttl", 2, 3600),
                                  ("max_ttl", "max_ttl", 0, 28800)):
            if req.get(key) is not None:
                try:
                    setattr(self.control, attr, max(lo, min(hi, float(req[key]))))
                except (TypeError, ValueError):
                    raise cu.CuError("CU_BAD_ARG", "%s 必须是数字" % key)
        with self.control_lock:
            if self.control.emergency:
                raise cu.CuError("CU_EMERGENCY_STOP",
                                 "用户手动关闭了电脑控制，这一轮的 cu 正在退出。"
                                 "不要重试、不要让用户再按任何键（再按只会立刻关掉，不是解锁）；"
                                 "要重新开始必须重新 activate 并等用户同意")
            L = self.control.lease
            if L:
                if L["owner"] == owner:
                    return dict(self._lease_view(L), activated=True, reused=True)
                raise cu.CuError("CU_BUSY", "正在使用：%s" % L["owner"])
            if self.pending:
                raise cu.CuError("CU_BUSY", "正在等待用户授权：%s" % self.pending["owner"])
            # 真正要授予新租约了：把上一轮留下的留言/提示全部作废，再放行。
            # 进程可能是复用的，不能指望"重启才干净"。
            self._purge_carryover()
            if self.consent_mode == "auto":
                lease = self.control.grant(owner, owner_id)
                self._show_active(owner)
                cu.audit("control", {"owner": owner}, "activated_auto")
                return dict(self._lease_view(lease), activated=True, auto=True)
            ev = threading.Event()
            self.pending = {"owner": owner, "event": ev, "decision": None,
                            "deadline": time.time() + 30}
        t0 = time.time()
        ok = self.overlay.send(
            cmd="banner",
            text=("「%s」想接管鼠标和键盘" % self.intro) if self.intro else "有 agent 想接管鼠标和键盘",
            sub=self._session_line(),
            note="同意后它会操作这台电脑；完成后自动交还，%s 没有任何操作也会收回。"
                 % ("%d 分钟" % max(1, int(self.control.idle_ttl // 60))
                    if self.control.idle_ttl >= 120 else "%d 秒" % int(self.control.idle_ttl)),
            seconds=30, buttons=True, tone="consent")
        if not ok:
            with self.control_lock:
                self.pending = None
            raise cu.CuError("CU_NO_OVERLAY",
                             self.overlay.last_error or "桌面提示层起不来")
        decision = self._wait_pending(31.0)
        waited = round(time.time() - t0, 1)
        with self.control_lock:
            self.pending = None
        self.overlay.send(cmd="hide_banner")
        if decision != "agree":
            cu.audit("control", {"owner": owner}, "activation_denied")
            raise cu.CuError("CU_ACTIVATION_DENIED",
                             "等了 %.1f 秒，用户没有同意；不要立刻重试，先问用户" % waited)
        lease = self.control.grant(owner, owner_id)
        self._show_active(owner)
        cu.audit("control", {"owner": owner, "waited": waited}, "activated")
        log("控制权已授予 %r（用户等待 %.1fs）" % (owner, waited))
        return dict(self._lease_view(lease), activated=True, waited=waited)

    def _deactivate(self, req):
        reason = str(req.get("reason") or "agent 完成")[:160]
        # 硬约束：DSH 不在最前面时，用户看不见后台窗口里的任何字。
        # 这时不许收工——先切回前台，或者改用工具侧的 reply/ask 说话。
        if self.control.lease and not self.control.paused:
            win, front = cu.cc_foreground()
            if win is not None and not front:
                raise cu.CuError(
                    "CU_NOT_FOREGROUND",
                    "DSH 现在不在最前面（前台是别的窗口），用户看不见你在这里说的话。"
                    "要收工先 action=\"focus_cc\" 把 DSH 切回前台再 deactivate；"
                    "想现在就跟用户说，用 action=\"reply\"（右下角卡片，不需要前台）。")
        if self.pending_reply:
            # 用户说了话还在等答复：不许用"收工"逃走，先把话回完（空 reply 也算回）。
            raise cu.CuError("CU_UNANSWERED", "")
        with self.control_lock:
            L = self.control.revoke(reason)
        # 防积压（第一道）：交还即清空这一轮的留言/提示，留到下一轮就是脏数据。
        self._purge_carryover()
        if L:
            self.revoke_reason = "turn-end" if reason == "turn-end" else "deactivate"
            self.revoke_detail = _STOP_DETAIL.get(self.revoke_reason, reason)
        if not L:
            return {"deactivated": False, "note": "本来就没有激活，无需关闭"}
        with cu.control_bypass():
            cu.release_all()
        # 交还：先把 DSH 的界面切回前台，用户回来就能直接说话；再撤提示层、
        # 最后把整个 cu 关掉（不是退后台）。
        if not self.stay_alive:
            with cu.control_bypass():
                try:
                    got = cu.focus_cc()
                    log("交还前台：%r" % (got,))
                except Exception as e:                     # noqa: BLE001
                    log("交还前台失败（不致命，继续关）：%s" % e)
        self.overlay.send(cmd="off")
        cu.audit("control", {"owner": L.get("owner"), "reason": reason}, "deactivated")
        log("控制权已交还（%r：%s）" % (L.get("owner"), reason))
        if not self.stay_alive:
            stop_reason = "turn-end" if reason == "turn-end" else "deactivate"
            self._later(1.0, lambda: self._full_stop(stop_reason))
        return {"deactivated": True, "owner": L.get("owner"), "reason": reason,
                "closed": not self.stay_alive,
                "note": "已交还用户，并把电脑控制完全关闭（提示层、热键都退出）"
                        if not self.stay_alive else
                        "已交还用户，用户现在可以正常使用鼠标键盘"}

    def _control(self, req):
        action = (req.get("action") or "status").strip().lower()
        if action == "status":
            running = bool(self.overlay.proc and self.overlay.proc.poll() is None)
            idle_gap = time.time() - self._beat_at
            return {"control": self.control.status(),
                    "alive": {"beat_age": round(idle_gap, 1),
                              "stalled": bool(self.control.lease) and not self.control.paused
                                         and idle_gap > self.STALL_SECS,
                              "threshold": self.STALL_SECS},
                    "hotkeys": {"emergency": "Ctrl+Alt+Shift+Backspace",
                                "pause": "Ctrl+Shift+Alt",
                                "agree": "Ctrl+Alt+Shift+Enter",
                                "polling_ok": self.hotkey_ok},
                    "session": self.session,
                    "overlay": {"running": running,
                                "error": self.overlay.last_error},
                    "consent_mode": self.consent_mode}
        # 急停只能由用户的热键触发；下面这几个动作只给自动化测试开一条缝。
        if action == "user_input":
            if os.environ.get("DSH_CU_TEST") != "1":
                raise cu.CuError("CU_BAD_ARG", "只有测试能伪造用户输入")
            self.last_user_input = 0.0
            self._flag_user_input()
            return {"action": action, "control": self.control.status()}
        if action in ("trip", "clear"):
            if os.environ.get("DSH_CU_TEST") != "1":
                raise cu.CuError("CU_BAD_ARG", "手动关闭只能由用户热键触发")
            if action == "trip":
                self._on_emergency_key()
            else:
                self.control.clear_emergency()
                self._show_idle()
            return {"action": action, "control": self.control.status()}
        # 暂停 / 点鲸鱼 / 留言：真机上分别是热键和鼠标事件，测试里由这里触发同一条路径。
        if action == "pause":
            if os.environ.get("DSH_CU_TEST") != "1":
                raise cu.CuError("CU_BAD_ARG", "暂停只能由用户的 Ctrl+Shift+Alt 触发")
            return {"action": action, "toggled": self._pause_toggle("test"),
                    "control": self.control.status()}
        if action == "logo_click":
            if os.environ.get("DSH_CU_TEST") != "1":
                raise cu.CuError("CU_BAD_ARG", "只有测试能伪造点击")
            self._on_logo_click()
            return {"action": action, "control": self.control.status()}
        if action == "message":
            if os.environ.get("DSH_CU_TEST") != "1":
                raise cu.CuError("CU_BAD_ARG", "只有测试能伪造留言")
            self._on_user_message(str(req.get("text") or ""))
            return {"action": action, "control": self.control.status()}
        if action == "emergency":
            if os.environ.get("DSH_CU_TEST") != "1":
                raise cu.CuError("CU_BAD_ARG", "手动关闭只能由用户的 Ctrl+Alt+Shift+Backspace 触发")
            # 走和真热键完全相同的一条路径（含 3 秒动画后彻底关闭）
            threading.Thread(target=self._on_emergency_key, daemon=True).start()
            return {"action": action, "control": self.control.status()}
        if action == "kill_overlay":
            if os.environ.get("DSH_CU_TEST") != "1":
                raise cu.CuError("CU_BAD_ARG", "只有测试能杀掉提示层")
            proc = getattr(self.overlay, "proc", None)
            if proc is not None:
                proc.kill()
            return {"action": action, "killed": proc is not None}
        raise cu.CuError("CU_BAD_ARG", "action 只支持 status / user_input / trip / clear / "
                                       "pause / logo_click / message / emergency / kill_overlay")

    # ---- 热键：Ctrl+Alt+Shift+Backspace 急停 / Ctrl+Alt+Shift+Enter 同意 / Ctrl+Shift+Alt 暂停 ----
    @staticmethod
    def _down(vk):
        return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)

    # 暂停键是"只有三个修饰键"，没有任何实体键；这份名单用来确认"真的没按别的"
    _PAUSE_GUARD = (tuple(range(0x20, 0x2F)) + tuple(range(0x30, 0x5B)) +
                    (0x5B, 0x5C, 0x5D) + tuple(range(0x60, 0x88)))

    def _hotkey_loop(self):
        # 不用 RegisterHotKey：它会和别的程序抢注册，而且被吞掉的键用户再也用不了。
        # 轮询 GetAsyncKeyState 不注册、不吞键，30ms 足够抓住人的按键。
        keys_em = (0x11, 0x12, 0x10, 0x08)      # Ctrl Alt Shift Backspace
        keys_ag = (0x11, 0x12, 0x10, 0x0D)      # Ctrl Alt Shift Enter
        self.hotkey_ok = True
        pause_since = 0.0
        pause_dirty = False
        pause_armed = False
        while not self.stopping:
            try:
                em = all(self._down(k) for k in keys_em)
                ag = all(self._down(k) for k in keys_ag)
                if em and not self._hotkey_prev["emergency"]:
                    self._on_emergency_key()
                if ag and not self._hotkey_prev["agree"]:
                    if self.pending:
                        self._resolve_pending("agree")
                self._hotkey_prev["emergency"] = em
                self._hotkey_prev["agree"] = ag

                # 暂停 = 只按住 Ctrl+Shift+Alt 三个修饰键再松开。触发点在"松开"，
                # 因为人按键通常只有 100ms 左右；以前要求按住 400ms，用户根本按不出来
                # （2026-11-14 实测：按了没反应）。同时记录按住期间有没有碰别的键，
                # 碰过就不算暂停——避免把 Ctrl+Alt+Shift+X 这类组合误判成暂停。
                mods = self._down(0x11) and self._down(0x12) and self._down(0x10)
                other = any(self._down(k) for k in self._PAUSE_GUARD)
                if mods and not em and not ag:
                    if not pause_since:
                        pause_since = time.time()
                        pause_dirty = other
                    elif other:
                        pause_dirty = True
                    pause_armed = True
                else:
                    if (pause_armed and not pause_dirty
                            and pause_since and time.time() - pause_since >= 0.03):
                        self._pause_toggle("hotkey")
                    pause_since = 0.0
                    pause_dirty = False
                    pause_armed = False
            except Exception:                                # noqa: BLE001
                self.hotkey_ok = False
            time.sleep(0.03)

    # ---- 真人输入检测：低级钩子能区分"人按的"和"我们注进去的"（INJECTED 标志）----
    # 用户随时可能误触鼠标键盘，也可能主动帮你/纠正你。这些都不该打断模型：
    # 不通报、不暂停注入，只做一件事——把我们自己按住的键松开（谁按的谁记账，
    # 见 cu.release_all），免得用户接手时我们的修饰键还卡着。
    def _flag_user_input(self):
        now = time.time()
        if now - self.last_user_input < 3.0:     # 节流：一次操作最多处理一次
            return
        self.last_user_input = now
        self._user_input_evt.set()

    def _notice_loop(self):
        while not self.stopping:
            if not self._user_input_evt.wait(0.5):
                continue
            self._user_input_evt.clear()
            try:
                if self.control.emergency or not self.control.lease:
                    continue                      # 没在控制电脑时，用户用键鼠与我无关
                with cu.control_bypass():
                    cu.release_all()              # 只松"我们自己按下"的键，不发任何提示
            except Exception:                     # noqa: BLE001
                pass

    def _input_hook_loop(self):
        from ctypes import wintypes
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                          ctypes.c_ssize_t, ctypes.c_void_p)

            class KBDLLHOOKSTRUCT(ctypes.Structure):
                _fields_ = [("vkCode", ctypes.c_uint32), ("scanCode", ctypes.c_uint32),
                            ("flags", ctypes.c_uint32), ("time", ctypes.c_uint32),
                            ("dwExtraInfo", ctypes.c_size_t)]

            class MSLLHOOKSTRUCT(ctypes.Structure):
                _fields_ = [("pt", wintypes.POINT), ("mouseData", ctypes.c_uint32),
                            ("flags", ctypes.c_uint32), ("time", ctypes.c_uint32),
                            ("dwExtraInfo", ctypes.c_size_t)]

            def kb_proc(nCode, wParam, lParam):
                if nCode == 0:
                    try:
                        info = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                        if not (info.flags & 0x10):        # LLKHF_INJECTED
                            self._flag_user_input()
                    except Exception:                      # noqa: BLE001
                        pass
                return user32.CallNextHookEx(None, nCode, wParam, lParam)

            def ms_proc(nCode, wParam, lParam):
                if nCode == 0:
                    try:
                        info = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                        if not (info.flags & 0x01):        # LLMHF_INJECTED
                            self._flag_user_input()
                    except Exception:                      # noqa: BLE001
                        pass
                return user32.CallNextHookEx(None, nCode, wParam, lParam)

            self._kb_proc = HOOKPROC(kb_proc)
            self._ms_proc = HOOKPROC(ms_proc)
            # ctypes 默认把 hMod 当 32 位 int 传，句柄被截断 → SetWindowsHookExW 报 126
            # (ERROR_MOD_NOT_FOUND)，钩子从来没装上过。这里显式声明签名。
            user32.SetWindowsHookExW.restype = ctypes.c_void_p
            user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                                 ctypes.c_void_p, ctypes.c_uint32]
            user32.CallNextHookEx.restype = ctypes.c_ssize_t
            user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                              ctypes.c_ssize_t, ctypes.c_void_p]
            kernel32.GetModuleHandleW.restype = ctypes.c_void_p
            kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
            hmod = kernel32.GetModuleHandleW(None)
            hk = user32.SetWindowsHookExW(13, self._kb_proc, hmod, 0)     # WH_KEYBOARD_LL
            hm = user32.SetWindowsHookExW(14, self._ms_proc, hmod, 0)     # WH_MOUSE_LL
            self.input_hook_ok = bool(hk and hm)
            if not self.input_hook_ok:
                log("真人输入钩子装不上（err=%d）：这项提示停用" % ctypes.get_last_error())
                return
            msg = wintypes.MSG()
            while not self.stopping:
                if user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) <= 0:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:                            # noqa: BLE001
            log("真人输入检测起不来：%s" % e)

    def _inbox_path(self):
        return os.path.join(_home_dir(), "inbox.json")

    def _inbox_load(self):
        try:
            with open(self._inbox_path(), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError, TypeError):
            return []
        out = []
        for x in data[-20:]:
            if isinstance(x, dict) and x.get("text"):
                out.append({"id": int(x.get("id") or 0), "text": str(x["text"])})
            elif isinstance(x, str) and x.strip():
                self._notice_seq += 1
                out.append({"id": self._notice_seq, "text": x})
        return out

    def _inbox_save(self):
        try:
            with open(self._inbox_path(), "w", encoding="utf-8") as f:
                json.dump(self.inbox[-20:], f, ensure_ascii=False)
        except OSError as e:
            log("写 inbox.json 失败：%s" % e)

    def _push_notice(self, text, persist=False):
        """挂一条提示。persist=True 的（用户留言）落盘，服务被关掉也不丢。"""
        self._notice_seq += 1
        item = {"id": self._notice_seq, "text": text}
        self.notices.append(item)
        del self.notices[:-20]
        if persist:
            self.inbox.append(item)
            self._inbox_save()
        return item

    def _peek_notices(self):
        """看一眼所有待推提示，**不删**。

        以前是取走就删：客户端只要没渲染（截图的二进制分支、Node 侧异常返回），
        这条留言就永久消失（2026-11-14 实测丢了用户两条留言）。现在改成
        "显示过、并且 agent 下一次请求回了 ack 才删"，至少一次投递。
        """
        return list(self.notices)

    # ---- 给"唤醒沉睡会话"用的留言队列 ----
    CLAIM_TTL = 45.0

    # 捏着租约超过这个秒数还没有任何动静，就认为"停滞"：状态字改成「停滞 Ns」并停掉
    # 光泽。阈值取 20s：正常的"思考 + 一次工具调用"都在 10s 内，超过 20s 基本是网络/卡死。
    STALL_SECS = 20.0

    def _claim_msgs(self, ids=None):
        """认领待投递留言：同一时刻只有一个消费者拿得到。

        工具体 hold 与宿主 relay 读的是同一个队列。原来两边各自读、各自 ack，
        同一条留言可能既进了工具结果、又被 followup 投一遍；现在先到先得，
        认领后 45 秒内没人 ack 才放回队列（at-least-once，但不重复）。
        """
        now = time.time()
        for mid, ent in list(self.inflight.items()):
            if now - ent["at"] > self.CLAIM_TTL:
                self.inflight.pop(mid, None)
                self.user_msgs.append(ent["item"])
        self.user_msgs.sort(key=lambda m: m.get("id") or 0)
        if ids is not None:
            want = set()
            for x in ids:
                try:
                    want.add(int(x))
                except (TypeError, ValueError):
                    pass
            take = [m for m in self.user_msgs if m["id"] in want]
        else:
            take = list(self.user_msgs)
        if take:
            taken = {m["id"] for m in take}
            self.user_msgs = [m for m in self.user_msgs if m["id"] not in taken]
            for m in take:
                self.inflight[m["id"]] = {"item": m, "at": now}
        return take

    def _messages(self, req):
        """留言队列。Node 侧轮询它，把留言塞进会话把模型叫醒。

        提示（notice）只在有工具调用时才随结果送到模型面前；会话睡着时
        一次工具调用都没有，留言就永远递不进去。所以另开一条通道：
        宿主侧拿到 items 后用 agent.followup() 直接唤醒会话。
        claim=true 表示"认领"：拿到的同时从队列挪进 inflight，别家就拿不到了。
        """
        ack = req.get("ack")
        if ack:
            ids = {int(x) for x in ack if str(x).isdigit()}
            self.user_msgs = [m for m in self.user_msgs if m["id"] not in ids]
            for i in ids:
                self.inflight.pop(i, None)
            return {"acked": len(ids), "items": []}
        if req.get("claim"):
            return {"items": self._claim_msgs(req.get("ids"))}
        return {"items": list(self.user_msgs)}

    def _wait(self, req):
        """长轮询：有新留言、控制权没了、或超时才返回。

        给"cu 开着就不许结束回合"用：模型调一次就挂在这里等，期间不烧 token，
        用户一说话立刻带着原话返回，回合自然续上。
        """
        try:
            seconds = float(req.get("seconds") or 45)
        except (TypeError, ValueError):
            seconds = 45.0
        seconds = max(1.0, min(seconds, 55.0))
        deadline = time.time() + seconds
        # 开始等之前先把事件清掉，免得刚进来就被上一次的残留唤醒
        self.msg_evt.clear()
        while time.time() < deadline:
            if self.stopping or self.user_msgs:
                break
            self.msg_evt.wait(0.2)
        items = self._claim_msgs()
        lease = self.control.lease or {}
        if self.stopping:
            why, detail = self.stop_reason, self.stop_detail
        elif not lease and self.revoke_reason:
            why, detail = self.revoke_reason, self.revoke_detail
        else:
            why, detail = "", ""
        return {
            "items": items,
            "active": bool(lease),
            "paused": bool(self.control.paused),
            "pending_reply": bool(self.pending_reply),
            "closed": bool(self.stopping),
            "reason": why,
            "detail": detail,
            "owner": lease.get("owner") or "",
            "seconds": round(seconds, 1),
        }

    def _ack_notice(self, nid):
        if isinstance(nid, (list, tuple)):
            ids = set()
            for x in nid:
                try:
                    ids.add(int(x))
                except (TypeError, ValueError):
                    pass
        else:
            try:
                ids = {int(nid)}
            except (TypeError, ValueError):
                return
        if not ids:
            return
        self.notices = [n for n in self.notices if n.get("id") not in ids]
        self.inbox = [n for n in self.inbox if n.get("id") not in ids]
        self._inbox_save()

    def _purge_carryover(self):
        """新一轮接管前清空上一轮的一切残留。

        用户 2026-11-20 要求：一轮新 cu 打开时，上一轮发生了什么都不能影响它。
        所以留言队列、已认领未 ack 的、待回复、待推提示、落盘 inbox 全清。
        """
        try:
            with self.control_lock:
                self.pending_reply = None
                self.control.pending_reply = False
                self.inflight.clear()
                self.user_msgs = []
                self.notices = []
            self.inbox = []
            self._inbox_save()
            self.stop_reason = self.stop_detail = ""
            self.revoke_reason = self.revoke_detail = ""
            log("新一轮接管：已清空上一轮遗留的留言/提示")
        except Exception as e:                     # noqa: BLE001
            log("清理上一轮遗留失败（不致命）：%s" % e)

    def _full_stop(self, reason="stop"):
        """彻底关掉 cu：撤提示层、带走凝视子进程、放掉监听管道，进程随之退出。"""
        if self.stopping:
            return
        self.stopping = True
        self.stop_reason = reason
        self.stop_detail = _STOP_DETAIL.get(reason, "cu 已关闭")
        # 防积压（第二道，与 activate 的清理互为冗余）：彻底关闭时全部清空并按盘清掉，
        # 下次启动绝不会把上一轮的东西捡回来。
        self._purge_carryover()
        # 唤醒所有在 `wait` 里长轮询的调用方：cu 已经关了，别再等
        try:
            self.msg_evt.set()
        except Exception:
            pass
        log("彻底关闭 cu（%s）" % reason)
        try:
            self._kill_watchers()
        finally:
            try:
                self.overlay.stop()
            finally:
                self._close_listener()

    def _on_emergency_key(self):
        with self.control_lock:
            # 正在征求同意时按它 = 拒绝；此时还没有控制权，不必进入急停流程。
            if self.pending:
                self._resolve_pending("deny")
                return
            if self.stopping:
                return
            if self.control.emergency:
                # 3 秒急停动画期间又按一次 = 别等了，立刻关
                self._full_stop("emergency")
                return
            self.control.set_paused(False)
            lease = self.control.trip_emergency()
            self.revoke_reason = "emergency"
            self.revoke_detail = _STOP_DETAIL["emergency"]
        with cu.control_bypass():
            cu.release_all()
        # 防积压（第一道）：只要控制权被收回，就把这一轮的留言/提示清干净。
        self._purge_carryover()
        # _purge 会清掉"为什么关"的记账；急停要把它写回去，wait 才能如实回报。
        self.revoke_reason = "emergency"
        self.revoke_detail = _STOP_DETAIL["emergency"]
        cu.audit("control", {"owner": (lease or {}).get("owner")}, "emergency")
        self.overlay.send(cmd="halo", on=False)
        self.overlay.send(cmd="logo", on=False)
        self.overlay.send(cmd="border", on=True, color="#FF3B30", alpha=0.55)
        self.overlay.send(cmd="flash", color="#FF3B30", times=3)
        self.overlay.send(cmd="banner",
                          text="已手动关闭：电脑控制已关闭",
                          sub="3 秒后撤掉所有提示，agent 会收到停手通知",
                          note="要恢复请让 agent 重新激活、由你点同意",
                          seconds=3, buttons=False, tone="emergency")
        if self.stay_alive:
            self._later(3.2, self._show_idle)
        else:
            self._later(3.2, lambda: self._full_stop("emergency"))
        log("急停触发：租约=%r，3 秒后彻底关闭" % ((lease or {}).get("owner"),))

    def _control_ticker(self):
        """空闲/超时自动收权。1 秒一次，成本可以忽略。"""
        while not self.stopping:
            try:
                with self.control_lock:
                    L = self.control.lease
                    expired = None
                    if L and not self.control.emergency:
                        now = time.time()
                        if self.control.max_ttl > 0 and now > L["max_deadline"]:
                            expired = "单次激活达到 %d 秒上限" % int(self.control.max_ttl)
                        elif not self.control.paused and now - L["last"] > self.control.idle_ttl:
                            # 暂停时用户正在和 agent 对话，不能因为"没调用工具"就把控制权收走
                            expired = "无任何操作超过 %d 秒" % int(self.control.idle_ttl)
                        if expired:
                            self.control.revoke("expired")
                            self.revoke_reason = ("max-ttl" if expired.startswith("单次激活")
                                                  else "idle-timeout")
                            self.revoke_detail = expired + "，控制权已收回"
                    # 状态词防卡死：动作结束后偶尔没来得及推新阶段，右下角会停在
                    # "等待"看着像死机。90 秒没有新阶段就退回"待命"。
                    if L and not self.control.paused and not expired \
                            and self._label in ("操作", "思考", "等待") \
                            and time.time() - self._phase_at > 90:
                        self._label = "待命"
                        self._label_at = time.time()
                        self._cand = None
                    if expired:
                        with cu.control_bypass():
                            cu.release_all()
                        # 防积压（第一道）：超时收权同样清空这一轮的残留。
                        # 先记住"有没有没答的话"，清空后还要用它决定鲸鱼环的颜色。
                        had_pending = bool(self.pending_reply)
                        self._purge_carryover()
                        self.revoke_reason = ("max-ttl" if expired.startswith("单次激活")
                                              else "idle-timeout")
                        self.revoke_detail = expired + "，控制权已收回"
                        cu.audit("control", {"reason": expired}, "expired")
                        # 有留言还没答就被收权：把鲸鱼环变红并停 2.5 秒，用户能看出没送达。
                        if had_pending:
                            self._logo(state="paused", label="未送达", color="#FF3B30")
                            self._later(2.5, self._show_idle)
                        else:
                            self._show_idle()
                        log("控制权自动收回：%s" % expired)
                        # 硬保证：agent 不主动交还也不能让 cu 一直挂着。
                        # 空闲/超时到期即整个退出（提示层一起撤），和手动关闭同一条路径。
                        if not self.stay_alive:
                            self._full_stop("idle-timeout")
                    # 每秒对账：状态字/边框/光晕以这里为准，漏发或旧进程画错都能自愈。
                    self._beat_status()
            except Exception:                                # noqa: BLE001
                log("收权线程异常：%s" % traceback.format_exc())
            time.sleep(1.0)

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
        # 客户端确认"这条提示已经显示给模型了"，这时才从队列和盘上删。
        # 不 ack 就一直留着，下一条结果里再带一次（至少一次投递）。
        if (req or {}).get("ack_notice") is not None:
            self._ack_notice(req.get("ack_notice"))
        try:
            if op == "ping":
                send_json(conn, self._wrap(rid, self._ping(), t0, with_notice=False))
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
                send_json(conn, self._wrap(rid, self._state(), t0, with_notice=False))
            elif op == "activate":
                send_json(conn, self._wrap(rid, self._activate(req), t0))
            elif op == "deactivate":
                send_json(conn, self._wrap(rid, self._deactivate(req), t0))
            elif op == "ask":
                send_json(conn, self._wrap(rid, self._ask(req), t0))
            elif op == "reply":
                send_json(conn, self._wrap(rid, self._reply(req), t0))
            elif op == "resume":
                send_json(conn, self._wrap(rid, self._resume(req), t0))
            elif op == "phase":
                send_json(conn, self._wrap(rid, self._phase(req), t0, with_notice=False))
            elif op == "relay":
                send_json(conn, self._wrap(rid, self._relay(req), t0, with_notice=False))
            elif op == "control":
                send_json(conn, self._wrap(rid, self._control(req), t0))
            elif op == "messages":
                send_json(conn, self._wrap(rid, self._messages(req), t0, with_notice=False))
            elif op == "wait":
                send_json(conn, self._wrap(rid, self._wait(req), t0, with_notice=False))
            elif op == "audit":
                n = int(req.get("n") or 20)
                send_json(conn, self._wrap(rid, {"records": cu.audit_tail(n)}, t0))
            elif op == "shutdown":
                send_json(conn, self._wrap(rid, {"bye": True}, t0))
                self.stopping = True
                self._kill_watchers()     # 自己起的凝视进程不能留成孤儿
                self.overlay.stop()       # 提示层跟着走，别在用户屏幕上留一圈蓝边
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

    def _wrap(self, rid, value, t0, error=None, with_notice=True):
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
        # 用户刚插过手/留过言：把提示挂在这次结果上，agent 下一次调用就能看到。
        # 失败结果也要带——"用户留言了"这种提示，恰恰是因为 agent 撞了 CU_PAUSED
        # 才会去看，只挂在成功结果上等于永远发不出去。
        # 探活/状态查询不领提示：管理器每次拉起都要 ping 一次，让它把提示
        # 领走的话，真正干活的 agent 就永远看不到了（2026-11-14 实测）。
        if with_notice:
            pend = self._peek_notices()
            if pend:
                # 一次把攒下的都带上：一条一送会被"队首没 ack"堵住后面的
                out["notice"] = "\n".join(n["text"] for n in pend)
                out["notice_ids"] = [n["id"] for n in pend]
            # 欠着用户一句答复时，每次结果都提醒（这是硬闸：不 reply 就一直动不了键鼠，
            # 也不许 deactivate 收工）。用户 2026-11-18 明确要求"先答再干"。
            if self.pending_reply:
                owe = ("【待回复】用户刚在右下角给你留了话，你还没回。用 action=\"reply\" "
                       "回一句（他会看到）；觉得不必回就 reply 空字符串照样解除暂停。"
                       "**reply 一返回，暂停就自动解除了**，不要再对用户说\"你还处于暂停\""
                       "或让他手动恢复。在 reply 之前：动键鼠会被 CU_UNANSWERED 拦，"
                       "deactivate 也走不了。")
                out["notice"] = (out.get("notice") + "\n\n" + owe) if out.get("notice") else owe
        return out

    def _ping(self):
        vx, vy, vw, vh = cu.screen()
        return {"protocol": PROTOCOL_VERSION, "version": SERVER_VERSION,
                "pid": os.getpid(), "uptime": round(time.time() - self.started, 1),
                "transport": self.config.kind, "endpoint": self.config.endpoint,
                "screen": {"x": vx, "y": vy, "width": vw, "height": vh},
                "queues": _queue_names(), "mute_until": cu._muted_until(),
                "modified_keys": sorted(cu._PRESSED_MODS | cu._HELD),
                "control": self.control.status(),
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
        # 用户下一次动手操作 = 上一条"递送失败"的红环该复位了。
        if self._logo_color:
            self._logo(color="", label="")
        self._beat()
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
        a.grid = req.get("grid") is not False
        a.at = req.get("at")
        a.span = req.get("span")
        a.window_only = bool(req.get("window_only"))
        png, meta = cu.capture_shot(a)
        if req.get("settle") and cu._settle_screen():
            # 首张正撞上动画/过渡：等落定后重截一张
            png, meta = cu.capture_shot(a)
            meta["settled"] = True
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
                "control": self.control.status(),
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
            try:
                # `wait` 是长轮询（最多 55s）：它若占着全局锁，用户的留言就永远
                # 递不进来（实测：留言要等 wait 超时才被处理）。所以它不参与串行。
                if (req or {}).get("op") == "wait":
                    _trace("req wait")
                    server.handle(conn, req)
                    _trace("done wait")
                else:
                    with server.lock:
                        _trace("req %s" % ((req or {}).get("op"),))
                        server.handle(conn, req)
                        _trace("done %s" % ((req or {}).get("op"),))
            except Exception as e:                      # noqa: BLE001
                # handle 内部已把业务异常包成响应；走到这里说明"连响应都发不出去"，
                # 记一笔再断，否则客户端只看到 EOF，查不出原因。
                log("会话异常，连接关闭：%r" % (e,))
                return
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
    recover_held_on_boot()
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
    recover_held_on_boot()
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
