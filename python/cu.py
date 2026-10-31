#!/usr/bin/env python3
"""
cu.py — 看屏幕、动鼠标、敲键盘、开应用、管窗口。

原理与豆包「操作电脑」同源：截图 -> 模型看图 -> 坐标操作 -> 再截图。
不同处：这里是单用户单进程，所以没有服务端、没有二进制协议、没有宿主注入，
只有这一个文件。第三方依赖仅 Pillow（用于截图）。

坐标一律 0-1000 归一化
    (0,0) 左上，(1000,1000) 右下，(500,500) 正中。
    脚本内部换算成物理像素，所以坐标与分辨率、DPI 缩放、截图缩放全部解耦。
    支持小数；本机 2560 宽屏下 0.01 单位 = 0.026px，精度绰绰有余。

常用：
    python cu.py shot                       # 截图（默认缩到长边 1280）
    python cu.py shot --region 0,0,640,360  # 只截左上 640x360（长边≤1568 时 1:1），看小字用
    python cu.py click 500 500              # 点屏幕正中
    python cu.py apps 计算器                 # 找应用（Get-StartApps 目录）
    python cu.py launch 计算器               # 启动
    python cu.py windows                    # 列出可见窗口 + 能否注入
    python cu.py focus 计算器                # 精确把窗口切到前台
    python cu.py paste "一大段中文"           # 剪贴板粘贴，长文本走这条路
    python cu.py batch                      # 多步一次执行，省进程启动与工具往返。
                                            # 命令从 stdin 一行一条喂进来。
                                            # DSH 的 shell 是 pwsh，bash 的 <<'EOF'
                                            # heredoc 在这里是语法错误；用 here-string：
                                            #   @'
                                            #   click 500 500
                                            #   key enter
                                            #   '@ | python cu.py batch
                                            # DSH 里更顺手的是在 run_code 里顺序
                                            # await 多个 tools.pwsh，不必用 batch。

退出码 0 成功 / 2 失败；失败时 stderr 首行形如 [CU_XXX] 说明，按码决定恢复策略。
"""
import argparse, atexit, ctypes, io, json, os, re, shlex, shutil, struct, subprocess, sys, tempfile, time

# 依赖缺失时给**可执行**的命令，而不是一句"缺少依赖"。对齐豆包 agent_infra 的做法：
# 环境不合法要报出"缺什么、怎么补"，不能静默降级。
PY = sys.executable or "python"
PIP_HINT = '"%s" -m pip install --user pillow' % PY

# 让 `import cu` 拿到的是**同一个模块对象**。
# 为什么必须有：`cu.py` 以脚本方式跑时模块名是 `__main__`，而 `uia.py` 里有
# `from cu import CuError`，会再导入一份 `cu` → CuError 变成两个不同的类，
# main() 的 `except CuError` 就抓不住 uia 抛的错，最终退化成 CU_UNEXPECTED。
# 注意：不能用 `sys.modules[__name__]`——用 importlib 按文件路径加载（模块名不在
# sys.modules 里）时会直接 KeyError，测试就是这么逮到第一版的。
_self_mod = sys.modules.get(__name__)
if _self_mod is not None:
    sys.modules.setdefault("cu", _self_mod)
from ctypes import wintypes

try:
    # 三个流都要显式 UTF-8：中文 Windows 上 Python 默认走 cp936，
    # 中文参数经 pwsh 或 stdin 进来会变"娴嬭瘯"这种乱码。
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
    # errors="replace" 是必需的：batch 的 stdin 里混进一个非 UTF-8 字节时，
    # 默认会抛 UnicodeDecodeError，而那是 read() 阶段——整批一行都跑不了。
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------- 错误码
# 让模型能按失败类型直接决定恢复策略，而不是靠猜。
CODES = {
    "CU_BAD_ARG": "参数非法",
    "CU_UIPI": "目标窗口完整性级别高于本进程，注入会被系统静默拦截",
    "CU_NO_WINDOW": "找不到匹配的窗口 / 没有前台窗口",
    "CU_NO_ELEMENT": "UIA 里没有匹配的控件（该应用可能没暴露可访问性树），改用 shot 看图",
    "CU_COVERED": "目标窗口被别的窗口盖住，截出来会是盖在它上面的内容",
    "CU_APP_NOT_FOUND": "Get-StartApps 目录里没有这个名字",
    "CU_APP_AMBIGUOUS": "匹配到多个应用，改用更完整的名字或直接用 AppID",
    "CU_NO_CLIPBOARD": "剪贴板取不到文本（可能是空的或非文本内容）",
    "CU_NO_SELECTION": "Ctrl+C 之后剪贴板没变化：可能没选中内容，或焦点不在目标窗口",
    "CU_NO_CHANGE": "监看到时间上限画面都没变化",
    "CU_MINIMIZED": "目标窗口最小化，脚本不替你还原（切显示状态有卡死风险）",
    "CU_HUNG": "目标窗口不响应消息，可能已经卡死",
    "CU_MOVED": "监看期间窗口位置或大小变了，基准作废",
    "CU_NO_PIL": "缺少 Pillow，截图功能不可用（pip install pillow）",
    "CU_SHOT_FAIL": "截图失败",
    "CU_BLACK": "抓到的画面是黑的：多半不在可输入桌面（锁屏 / UAC 安全桌面），"
                "重试注入没有用，切回终端叫用户",
    "CU_NO_QUEUE": "没有这个盯队列，先用 `盯` 起一个",
    "CU_OS": "操作系统层失败（包着 OSError），多半是路径 / 权限 / 文件被占用",
    "CU_UNEXPECTED": "兜底：脚本没预料到的异常，按类型 + 消息给，别当它是正常失败",
    # ↓↓↓ 对齐豆包 _validation.py / envelope.py 的校验与错误码语义后新增的码
    "CU_SCROLL_DELTA": "scroll 步数非法：必须是非零、有限、在 -10..10 之间",
    "CU_BAD_HOTKEY": "组合键写成了一个带 + 的字符串，要每个键分开传",
    "CU_WAIT_SECONDS": "wait 的单位是秒（本实现 0..600），毫秒值要先除以 1000",
    "CU_NO_PYTHON": "找不到可用的 Python 解释器，常驻服务起不来",
    "CU_IPC": "与常驻服务的通道失败（管道/套接字建不起来或对端断开）",
    "CU_PROTOCOL": "常驻服务回的信封不合规（缺字段、类型未知、失败却没带 error）",
    "CU_EVAL": "eval 里那段代码抛异常了，看 traceback",
    "CU_EVAL_TIMEOUT": "eval 超时仍未返回；线程杀不掉，可 server_restart 回收",
    "CU_AUDIT": "审计日志写不进去（只影响留痕，动作本身可能已经执行）",
    "CU_ELEMENT": "UIA 元素层失败（找不到该元素 / 动作不被该元素支持）",
    "CU_NO_SIDECAR": "UIA 常驻子进程不可用，元素级能力降级；可用 shot 看图路线",
    "CU_DEP_MISSING": "缺少 Python 依赖，按提示的 pip 命令装",
    "CU_CONFIRM_REQUIRED": "这个动作有破坏性，必须显式确认（--confirm / confirm:true）才执行",


}


class CuError(Exception):
    def __init__(self, code, detail=""):
        msg = "[%s] %s" % (code, CODES.get(code, ""))
        if detail:
            msg += "：" + detail
        super().__init__(msg)
        self.code = code


# ---------------------------------------------------------------- 运行目录 / 审计
# 所有本插件自己产生的东西都收在一个目录下：审计日志、隔离临时目录、绘图/截图缓存。
# 删掉这一个目录 = 彻底清干净，不给用户的机器留散件。
def _home_dir():
    base = os.environ.get("DSH_CU_HOME")
    if not base:
        la = os.environ.get("LOCALAPPDATA")
        base = os.path.join(la, "dsh-computer-use") if la else \
            os.path.join(tempfile.gettempdir(), "dsh-computer-use")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        base = os.path.join(tempfile.gettempdir(), "dsh-computer-use")
        os.makedirs(base, exist_ok=True)
    return base


AUDIT_FILE = os.path.join(_home_dir(), "audit.jsonl")
AUDIT_MAX_BYTES = 2 * 1024 * 1024


def _audit_value(v):
    """审计里只留**摘要**，绝不落正文。
    剪贴板内容、输入的文本、URL 查询值都可能含隐私，所以：
    字符串截到 80 字符，`type` 这类正文型参数只记长度。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    s = str(v)
    return s if len(s) <= 80 else s[:80] + "..."


def audit(action, args=None, state="completed", extra=None):
    """动作三态留痕（对齐豆包 _instrumentation.py 的 attempted/completed/rejected）。

    写不进去**绝不影响动作本身**——留痕是附加值，不是主功能。
    但也不能静默：返回错误字符串，由调用方决定要不要提示。
    """
    rec = {"t": round(time.time(), 3), "pid": os.getpid(), "action": str(action),
           "state": str(state)}
    if args:
        try:
            rec["args"] = {str(k): _audit_value(v) for k, v in dict(args).items()}
        except Exception:
            rec["args"] = {}
    if extra:
        try:
            rec["extra"] = {str(k): _audit_value(v) for k, v in dict(extra).items()}
        except Exception:
            rec["extra"] = {}
    try:
        try:
            if os.path.getsize(AUDIT_FILE) > AUDIT_MAX_BYTES:
                os.replace(AUDIT_FILE, AUDIT_FILE + ".1")
        except OSError:
            pass
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return None
    except OSError as e:
        return "CU_AUDIT: %s" % e


def audit_tail(n=20):
    """读审计尾巴（给 doctor / 排障用）。读不到就回空表，不抛。"""
    out = []
    try:
        with open(AUDIT_FILE, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return out[-max(1, int(n)):]


# ---------------------------------------------------------------- 结果信封
# 对齐豆包 agent_infra 的那套严格校验：缺 ok / output.type 未知 / 失败却没带
# error 对象，一律当协议错误。宁可报错，也不要把半成品当结果喂给模型。
def envelope_ok(text=None, image=None, error=None):
    """构造一份结果信封并做严格自检；不合规直接抛 CU_PROTOCOL。"""
    env = {"ok": error is None}
    if error is not None:
        env["error"] = {"code": getattr(error, "code", "CU_UNEXPECTED"),
                        "message": str(error)}
    if image is not None:
        env["output"] = {"type": "image", "image": image}
    elif text is not None:
        env["output"] = {"type": "text", "text": text}
    else:
        env["output"] = {"type": "none"}
    _check_envelope(env)
    return env


def _check_envelope(env):
    if not isinstance(env, dict):
        raise CuError("CU_PROTOCOL", "结果信封不是对象")
    if "ok" not in env or not isinstance(env["ok"], bool):
        raise CuError("CU_PROTOCOL", "结果信封缺少布尔型 ok 字段")
    out = env.get("output")
    if out is not None:
        if not isinstance(out, dict) or "type" not in out:
            raise CuError("CU_PROTOCOL", "output 缺少 type 字段")
        if out["type"] not in ("text", "image", "none"):
            raise CuError("CU_PROTOCOL", "output.type 未知：%r" % (out["type"],))
    if env["ok"] is False:
        err = env.get("error")
        if not isinstance(err, dict) or not err.get("code") or not err.get("message"):
            raise CuError("CU_PROTOCOL", "失败信封缺少合规的 error 对象")
    return env


# ---------------------------------------------------------------- Win32 基础
u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
a32 = ctypes.WinDLL("advapi32", use_last_error=True)

# 必须最先调用：不声明 DPI 感知的话，屏幕坐标是 1707x960 逻辑值，
# 而真实物理分辨率是 2560x1440，点击会系统性偏移 1.5 倍。
u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2

PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("data", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("vk", wintypes.WORD), ("scan", wintypes.WORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("extra", PTR)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _send(inp):
    # UIPI 拦截时 SendInput 返回 0，GetLastError=5。这是唯一可靠的失败信号。
    if u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) != 1:
        err = ctypes.get_last_error()
        if err == 5:
            raise CuError("CU_UIPI", "SendInput 返回 ERROR_ACCESS_DENIED")
        raise OSError("SendInput 失败，GetLastError=%d" % err)


def _mouse(flags, dx=0, dy=0, data=0):
    i = INPUT(type=0)
    i.u.mi = MOUSEINPUT(dx, dy, data, flags, 0, 0)
    _send(i)


def _key(vk, up=False):
    # 扫描码必须一并给出：只给虚拟键码时，部分控件认不出修饰键（Ctrl/Alt/Shift）状态
    scan = u32.MapVirtualKeyW(vk, 0)   # MAPVK_VK_TO_VSC
    i = INPUT(type=1)
    i.u.ki = KEYBDINPUT(vk, scan, 0x0002 if up else 0, 0, 0)
    _send(i)


def _unicode(ch, up=False):
    i = INPUT(type=1)
    i.u.ki = KEYBDINPUT(0, ord(ch), 0x0004 | (0x0002 if up else 0), 0, 0)
    _send(i)


MOVE, LDOWN, LUP, RDOWN, RUP, WHEEL = 0x0001, 0x0002, 0x0004, 0x0008, 0x0010, 0x0800
ABS, VDESK = 0x8000, 0x4000

VK = {"enter": 0x0D, "tab": 0x09, "esc": 0x1B, "space": 0x20, "backspace": 0x08,
      "delete": 0x2E, "insert": 0x2D, "up": 0x26, "down": 0x28, "left": 0x25,
      "right": 0x27, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
      "ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B, "capslock": 0x14,
      "printscreen": 0x2C, "apps": 0x5D, "pause": 0x13,
      "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
      "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B}


# ---------------------------------------------------------------- 坐标换算
def screen():
    """返回虚拟桌面 (x, y, w, h)。多屏时是整个拼接桌面的外接矩形。"""
    g = u32.GetSystemMetrics
    return g(76), g(77), g(78), g(79)


def _unit(v, name):
    """归一化坐标校验：0-1000 的有限数。bool 是 int 的子类，必须显式拒绝。"""
    if isinstance(v, bool):
        raise CuError("CU_BAD_ARG", "%s 不能是布尔值" % name)
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise CuError("CU_BAD_ARG", "%s 必须是 0-1000 的数字，收到 %r" % (name, v))
    if f != f or f in (float("inf"), float("-inf")):
        raise CuError("CU_BAD_ARG", "%s 必须是有限数" % name)
    if not 0 <= f <= 1000:
        raise CuError("CU_BAD_ARG",
                      "%s=%s 越界。坐标是 0-1000 归一化值，不是像素——"
                      "屏幕宽度对应 1000" % (name, f))
    return f


def to_px(nx, ny):
    """归一化 (0-1000) -> 物理像素，并夹在屏幕内。"""
    vx, vy, vw, vh = screen()
    x = int(round(vx + _unit(nx, "x") * vw / 1000.0))
    y = int(round(vy + _unit(ny, "y") * vh / 1000.0))
    return (max(vx, min(vx + vw - 1, x)), max(vy, min(vy + vh - 1, y)))


def to_norm(x, y):
    vx, vy, vw, vh = screen()
    return (round((x - vx) * 1000.0 / vw, 1), round((y - vy) * 1000.0 / vh, 1))


class _P(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def where():
    p = _P()
    u32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


# ---------------------------------------------------------------- 鼠标键盘
def move(x, y):
    vx, vy, vw, vh = screen()
    nx = max(0, min(65535, int(round((x - vx) * 65535 / (vw - 1)))))
    ny = max(0, min(65535, int(round((y - vy) * 65535 / (vh - 1)))))
    _mouse(MOVE | ABS | VDESK, nx, ny)


def _move_smooth(nx, ny, ms=300):
    """平滑移动光标（对齐豆包 mouse.move_smooth）。

    为什么需要它：目标应用常按 MOUSEMOVE 的**连续性**判断"这是人在移动鼠标"——
    hover 菜单、拖拽手柄、自动隐藏工具栏都吃这一套。瞬移会让它们收不到中间事件，
    表现成"悬停没反应"。和 drag 里那段"不是瞬移"的注释是同一个道理。
    """
    if not 0 <= ms <= 5000:
        raise CuError("CU_BAD_ARG", "平滑移动时长需在 0..5000 毫秒，收到 %s" % ms)
    x0, y0 = where()
    x1, y1 = to_px(nx, ny)
    dist = max(abs(x1 - x0), abs(y1 - y0))
    steps = 1 if ms <= 0 else max(2, min(60, dist // 12 or 2))
    pause = (ms / 1000.0) / steps if ms > 0 else 0
    for i in range(1, steps + 1):
        move(int(x0 + (x1 - x0) * i / steps), int(y0 + (y1 - y0) * i / steps))
        if pause:
            time.sleep(pause)
    return x1, y1


def wait_seconds(v, lo=0.0, hi=600.0):
    """wait 的秒数校验（对齐豆包 _validation.py 的 duration_milliseconds）。

    三个必须拦的输入：布尔（Python 里 True 是 1，静默通过最危险）、
    非数字、以及**毫秒误传**——`wait(1000)` 意图是 1 秒，直接照做会挂 16 分钟。
    """
    if isinstance(v, bool):
        raise CuError("CU_WAIT_SECONDS", "wait 的秒数不能是布尔值，收到 %r" % (v,))
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise CuError("CU_WAIT_SECONDS", "wait 的秒数必须是数字，收到 %r" % (v,))
    if f != f or f in (float("inf"), float("-inf")) or not lo <= f <= hi:
        raise CuError("CU_WAIT_SECONDS",
                      "wait 秒数要在 %g..%g，收到 %s。单位是秒不是毫秒——"
                      "要等 1 秒写 wait(1)，不是 wait(1000)" % (lo, hi, v))
    return f


def click(nx, ny, right=False, double=False, triple=False):
    x, y = to_px(nx, ny)
    move(x, y)
    time.sleep(0.02)
    down, up = (RDOWN, RUP) if right else (LDOWN, LUP)
    n = 3 if triple else (2 if double else 1)
    for i in range(n):
        _mouse(down)
        _mouse(up)
        if i < n - 1:
            time.sleep(0.05)
    return x, y


def drag(nx1, ny1, nx2, ny2, ms=800):
    """ms 是整个拖拽的时长。默认 800ms，**不是瞬移**——资源管理器、Outlook 这类
    拖放目标常常收不到太快的中间 MOUSEMOVE，表现成"拖了没反应"这个静默失败。"""
    if not 50 <= ms <= 10000:
        raise CuError("CU_BAD_ARG", "drag 时长需在 50..10000 毫秒，收到 %s" % ms)
    x1, y1 = to_px(nx1, ny1)
    x2, y2 = to_px(nx2, ny2)
    steps = max(10, min(60, int(ms / 20)))
    pause = (ms / 1000.0) / steps
    move(x1, y1)
    time.sleep(0.06)
    _mouse(LDOWN)
    try:
        time.sleep(0.06)
        for i in range(1, steps + 1):
            move(int(x1 + (x2 - x1) * i / steps), int(y1 + (y2 - y1) * i / steps))
            time.sleep(pause)
        time.sleep(0.08)      # 落点停顿：让目标应用确认悬停到位再松手
    finally:
        # 必须兜底松左键：中途被 Ctrl+C / 超时杀掉时，左键会永久按住在系统层，
        # 之后所有鼠标动作都变成拖选。
        _mouse(LUP)
    return (x1, y1, x2, y2)


def scroll(nx, ny, n):
    """n: 非零整数 [-10,10]，正数向上；或 up/down/left/right。"""
    horiz = False
    if isinstance(n, bool):
        raise CuError("CU_SCROLL_DELTA", "scroll 步数不能是布尔值，收到 %r" % (n,))
    if isinstance(n, str):
        s = n.strip().lower()
        alias = {"up": 1, "down": -1, "right": 1, "left": -1}
        if s in alias:
            horiz = s in ("left", "right")
            n = alias[s]
        else:
            try:                      # 命令行传进来的永远是字符串，先当数字试
                n = float(s)
            except ValueError:
                raise CuError("CU_SCROLL_DELTA",
                              "scroll 只接受 -10..10 的整数或 up/down/left/right，收到 %r" % n)
    else:
        try:
            n = float(n)
        except (TypeError, ValueError):
            raise CuError("CU_SCROLL_DELTA", "scroll 步数必须是数字，收到 %r" % (n,))
    if n != n or n in (float("inf"), float("-inf")):
        raise CuError("CU_SCROLL_DELTA", "scroll 步数必须是有限数，收到 %r" % (n,))
    if n == 0:
        raise CuError("CU_SCROLL_DELTA", "scroll 步数不能为 0")
    if not -10 <= n <= 10:
        raise CuError("CU_SCROLL_DELTA", "scroll 步数需在 -10..10，收到 %s" % n)
    # 对齐豆包：取整后若变成 0（比如 0.4），按**取整前**的符号取 ±1，而不是当成没滚
    sign = 1 if n > 0 else -1
    n = int(round(n))
    if n == 0:
        n = sign
    x, y = to_px(nx, ny)
    move(x, y)
    time.sleep(0.02)
    # MOUSEEVENTF_HWHEEL = 0x1000，data 正数向右
    _mouse(0x1000 if horiz else WHEEL, 0, 0, n * 120)
    return n


# 键名别名：对齐豆包 _validation.py 的 _KEY_ALIASES。
# 模型经常写成 "ArrowLeft" 这种 W3C 风格的名字，直接拒绝太苛刻，统一归一化。
_KEY_ALIASES = {
    "arrowleft": "left", "arrowright": "right",
    "arrowup": "up", "arrowdown": "down",
    "escape": "esc", "return": "enter", "spacebar": "space",
    "control": "ctrl", "del": "delete", "pgup": "pageup", "pgdn": "pagedown",
}


def _norm_key(name):
    n = str(name).strip().lower()
    return _KEY_ALIASES.get(n, n)


def key(combo):
    """'ctrl+s' / 'enter' / 'alt+tab'。也接受已分开的列表。"""
    parts = combo if isinstance(combo, (list, tuple)) else str(combo).split("+")
    names, ks = [], []
    for p in [_norm_key(s) for s in parts]:
        if not p:
            continue
        names.append(p)
        if p in VK:
            ks.append(VK[p])
        elif len(p) == 1:
            ks.append(ord(p.upper()))
        else:
            raise CuError("CU_BAD_ARG",
                          "不认识的键 %r。可用：%s，或单个字母/数字"
                          % (p, "/".join(sorted(VK))))
    if not ks:
        raise CuError("CU_BAD_ARG", "没有解析出任何键")
    # 按下与松开必须成对：中途任何一步失败都要把已按下的补松开，
    # 否则修饰键 latch 在系统层，且不报任何错。进程被杀时由 atexit 兜底。
    try:
        for n, v in zip(names, ks):
            if n in HOLDABLE:
                _PRESSED_MODS.add(n)
            _key(v)
            time.sleep(0.02)
    finally:
        for n, v in zip(reversed(names), reversed(ks)):
            _PRESSED_MODS.discard(n)
            try:
                _key(v, up=True)
            except OSError:
                pass
            time.sleep(0.02)
    return "+".join(names)


def key_multi(keys):
    """给工具层用的「分开传键」入口，对齐豆包 hotkey_keys() 的校验。

    豆包在这里拒绝 `hotkey("ctrl+s")`：单个元素里带 `+` 说明调用者把整串当成了一个键，
    真发出去就是一串字符而不是组合键——而且是静默的错误行为，必须报错。
    """
    if isinstance(keys, str):
        keys = [keys]
    try:
        parts = [str(k) for k in keys]
    except TypeError:
        raise CuError("CU_BAD_HOTKEY", "组合键要传字符串数组，收到 %r" % (keys,))
    if not parts:
        raise CuError("CU_BAD_HOTKEY", "组合键不能为空")
    if len(parts) == 1 and parts[0] != "+" and "+" in parts[0]:
        raise CuError("CU_BAD_HOTKEY",
                      "把每个键分开传：收到 %r，应传 ['ctrl', 's']" % (parts[0],))
    return key(parts)


# 长按修饰键：Ctrl+拖拽、Shift+点选多行这类操作用。
# 必须成对，而且**必须在同一次 batch 调用里**——分成两次 `bash` 调用时，中间任何
# 一步失败（超时、被拒、用户中断）都会让修饰键永久 latch 在系统层：
# 之后每一次点击都自带 Ctrl，屏幕就此失控，且没有任何报错。
HOLDABLE = ("ctrl", "alt", "shift", "win")
_HELD = set()          # 本进程按下过、还没松开的修饰键

# 同上，但记录 key() 组合键里按下的修饰键。
# 中断（异常 / 超时 / 进程被杀）会在系统层留下"按住不放"的修饰键，
# 表现是用户以为键盘坏了：按空格在屏幕左上角弹出窗口菜单 = Alt 卡住（Alt+Space）。
# 所以按下即登记、松开即注销，并由 atexit 在进程退出时无条件补一次 up。
_PRESSED_MODS = set()


def _mods_cleanup():
    for n in tuple(_PRESSED_MODS):
        try:
            _key(VK[n], up=True)
        except Exception:
            pass
    _PRESSED_MODS.clear()


atexit.register(_mods_cleanup)


def key_hold(name, down=True):
    n = str(name).strip().lower()
    if n not in HOLDABLE:
        raise CuError("CU_BAD_ARG",
                      "只能长按修饰键 %s，收到 %r" % ("/".join(HOLDABLE), name))
    _key(VK[n], up=not down)
    if down:
        _HELD.add(n)
    else:
        _HELD.discard(n)
    return n


def release_all(force=False):
    """松开修饰键。force=True 无条件全松（独立的 release 命令用这个）；
    默认只松本进程按下过的，免得把用户正按着的键也松开。"""
    names = HOLDABLE if force else tuple(_HELD)
    done = []
    for n in names:
        try:
            _key(VK[n], up=True)
            done.append(n)
        except OSError:
            pass
    _HELD.clear()
    if force:
        # drag 被中断会在系统层留下按住的左键，release 一并覆盖鼠标键
        for f, tag in ((LUP, "鼠标左键"), (RUP, "鼠标右键")):
            try:
                _mouse(f)
                done.append(tag)
            except OSError:
                pass
    return done


def type_text(s):
    """逐字符注入。短文本用；长文本用 paste（快几百倍）。

    **只打字，一个键都不按。** 文本里出现换行或制表符直接报错：Enter 在聊天软件里
    是"发送"、在表单里是"提交"，都是撤不回的；Tab 会把焦点跳到下一个控件。
    要按回车就单独发一条 `key enter`，让这一步在工具调用里显式可见、可拦。
    """
    if not s:
        return 0
    for bad, name, hint in (("\n", "换行符", "key enter"),
                            ("\r", "回车符", "key enter"),
                            ("\t", "制表符", "key tab")):
        if bad in s:
            raise CuError("CU_BAD_ARG",
                          "type 的文本里含%s。type 只打字不按键——要按就单独发一条 "
                          "`%s`。这样设计是为了防止把消息或表单误发出去。"
                          % (name, hint))
    n = skipped = 0
    for ch in s:
        if ord(ch) > 0xFFFF:
            # 星平面外字符（emoji 等）ord() 会溢出 WORD，注入不了。
            # 静默跳过是危险的——模型会以为发出去了。所以打到 stderr。
            skipped += 1
            continue
        _unicode(ch)
        _unicode(ch, up=True)
        n += 1
        # 输入节流（不是消抖）：1ms 太快，WinUI3 目标会丢 WM_CHAR——实测
        # "AB CD EF" 丢成 "AB EF"、"TEST" 退化成 "TTTT"。词边界多等一点，
        # 因为丢字集中在空格/标点之后；普通字符 8ms 已被验证足够。
        time.sleep(0.03 if ch in " \t,.;:!?，。；：！？、-—" else 0.008)
    if skipped:
        print("（有 %d 个字（emoji 这类）逐字注入不了，已跳过——要发它们请改用 paste）"
              % skipped, file=sys.stderr)
    return n



# ---------------------------------------------------------------- 剪贴板
# 三个 Win32 标准常量。CF_DIB 是"设备无关位图"，微信 / QQ / Word 粘贴图片
# 只认这个格式，所以 clip-image 走的是它而不是 CF_BITMAP。
CF_UNICODETEXT = 13
CF_DIB = 8
GMEM_MOVEABLE = 0x0002


# ctypes 默认把返回值当 32 位 int。64 位下句柄和指针会被截断，GlobalLock 拿到
# 半截句柄就是 access violation——而且不报错、直接崩。凡是返回句柄或指针的，
# 必须显式声明 restype，这是这段代码里最容易漏、后果最狠的一处。
u32.OpenClipboard.argtypes = [wintypes.HWND]
u32.GetClipboardData.argtypes = [wintypes.UINT]
u32.GetClipboardData.restype = ctypes.c_void_p
u32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
u32.SetClipboardData.restype = ctypes.c_void_p
k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
k32.GlobalAlloc.restype = ctypes.c_void_p
k32.GlobalLock.argtypes = [ctypes.c_void_p]
k32.GlobalLock.restype = ctypes.c_void_p
k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
k32.GlobalSize.argtypes = [ctypes.c_void_p]
k32.GlobalSize.restype = ctypes.c_size_t


def clip_get(attempts=6):
    for _ in range(attempts):
        if u32.OpenClipboard(None):
            try:
                h = u32.GetClipboardData(CF_UNICODETEXT)
                if not h:
                    return None
                p = k32.GlobalLock(h)
                if not p:
                    return None
                try:
                    return ctypes.wstring_at(p)
                finally:
                    k32.GlobalUnlock(h)
            finally:
                u32.CloseClipboard()
        time.sleep(0.05)   # 剪贴板常被别的进程短暂占用，重试几次
    raise CuError("CU_NO_CLIPBOARD", "OpenClipboard 连续失败")


def clip_set(text):
    data = (text + "\0").encode("utf-16-le")
    for _ in range(6):
        if u32.OpenClipboard(None):
            try:
                u32.EmptyClipboard()
                h = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
                p = k32.GlobalLock(h)
                ctypes.memmove(p, data, len(data))
                k32.GlobalUnlock(h)
                if not u32.SetClipboardData(CF_UNICODETEXT, h):
                    raise CuError("CU_NO_CLIPBOARD", "SetClipboardData 失败")
                return len(text)
            finally:
                u32.CloseClipboard()
        time.sleep(0.05)
    raise CuError("CU_NO_CLIPBOARD", "OpenClipboard 连续失败")


def clip_set_image(path):
    """把一张 PNG 放到剪贴板，格式 CF_DIB——微信/QQ/Word 粘贴图片认这个格式。

    CF_DIB 就是 BMP 去掉开头 14 字节的 BITMAPFILEHEADER，所以直接让 PIL 存 BMP
    再切掉文件头即可，不需要额外的库。
    """
    try:
        from PIL import Image
    except ImportError:
        raise CuError("CU_NO_PIL", "pip install pillow")
    import io as _io
    try:
        img = Image.open(path)
        img.load()
    except Exception as e:
        raise CuError("CU_BAD_ARG", "读不了图片 %s：%s" % (path, e))
    # 带 alpha 的图 CF_DIB 认不了。**合成到白底**，而不是 convert("RGB")——
    # 后者把透明像素摊成黑块，浅色底的截图发出去就是一张脏图。
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    buf = _io.BytesIO()
    img.save(buf, "BMP")
    data = buf.getvalue()[14:]        # 掐掉 BITMAPFILEHEADER
    for _ in range(6):
        if u32.OpenClipboard(None):
            try:
                u32.EmptyClipboard()
                h = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
                if not h:
                    raise CuError("CU_NO_CLIPBOARD", "GlobalAlloc 失败")
                p = k32.GlobalLock(h)
                ctypes.memmove(p, data, len(data))
                k32.GlobalUnlock(h)
                if not u32.SetClipboardData(CF_DIB, h):
                    raise CuError("CU_NO_CLIPBOARD", "SetClipboardData(CF_DIB) 失败")
                mb = len(data) / 1048576.0
                # 体积必须报出来：CF_DIB 是未压缩位图，一张 1568px 的图就是 4.7MB，
                # 贴进聊天框会一直挂在输入框里。不报数字的话模型没有任何信号可依。
                big = ("  ⚠ 偏大，聊天窗口里贴大图会把输入框撑爆，"
                       "先把长边缩到 1000 上下再发") if mb > 1.5 else ""
                return "%dx%d  %.1fMB%s" % (img.size[0], img.size[1], mb, big)
            finally:
                u32.CloseClipboard()
        time.sleep(0.05)
    raise CuError("CU_NO_CLIPBOARD", "OpenClipboard 连续失败")


def clip_get_image(out):
    """把剪贴板里的图片存成 PNG。返回尺寸字符串；剪贴板没图就报错。"""
    p = _clip_get_dib()
    if p is None:
        raise CuError("CU_NO_CLIPBOARD", "剪贴板里没有图片")
    try:
        from PIL import Image
    except ImportError:
        raise CuError("CU_NO_PIL", "pip install pillow")
    import io as _io
    hdr = p[:4]
    if len(hdr) >= 4 and hdr[:2] == b"BM":
        img = Image.open(_io.BytesIO(p))
    else:
        # 补回 BMP 文件头，PIL 才肯读 CF_DIB
        size = 14 + len(p)
        fh = b"BM" + size.to_bytes(4, "little") + b"\0\0\0\0" + (14 + 40).to_bytes(4, "little")
        img = Image.open(_io.BytesIO(fh + p))
    img.load()
    img.save(out)
    return "%dx%d" % img.size


def _clip_get_dib():
    for _ in range(6):
        if u32.OpenClipboard(None):
            try:
                h = u32.GetClipboardData(CF_DIB)
                if not h:
                    return None
                p = k32.GlobalLock(h)
                if not p:
                    return None
                try:
                    sz = k32.GlobalSize(h)
                    return ctypes.string_at(p, sz)
                finally:
                    k32.GlobalUnlock(h)
            finally:
                u32.CloseClipboard()
        time.sleep(0.05)
    raise CuError("CU_NO_CLIPBOARD", "OpenClipboard 连续失败")


def paste_image(path):
    """把图片放上剪贴板再 Ctrl+V。**不自动回车**——发送是不可逆的，
    让调用方自己决定什么时候按 Enter。"""
    size = clip_set_image(path)
    time.sleep(0.15)
    key("ctrl+v")
    return size


def paste(text):
    """写剪贴板 -> Ctrl+V。长文本走这条路，比逐字符注入快几百倍且不会丢字。
    注意：会覆盖用户剪贴板内容。"""
    clip_set(text)
    time.sleep(0.05)
    key("ctrl+v")
    return len(text)


def copy_selection():
    """Ctrl+C 后取剪贴板——验证"选中了什么"最便宜的方式。

    关键：轮询结束仍没变化时**必须报错，不能返回旧剪贴板**。
    没选中内容 / 焦点不在目标窗口 / Ctrl+C 被 UIPI 丢掉，全都表现为"剪贴板没变"，
    返回旧值会让模型把用户几小时前复制的东西当成"选中内容"继续推理。"""
    before = clip_get()
    key("ctrl+c")
    for _ in range(10):          # 目标程序写剪贴板有延迟
        time.sleep(0.08)
        now = clip_get()
        if now is not None and now != before:
            return now
    now = clip_get()
    if now is not None and now != before:
        return now
    raise CuError("CU_NO_SELECTION",
                  "轮询 0.8 秒后剪贴板仍是原内容。先 click 一下目标窗口确认焦点，"
                  "或确认真的选中了东西，再重试")


# ---------------------------------------------------------------- 窗口 / 应用
class WND:
    __slots__ = ("hwnd", "pid", "title", "exe")

    def __init__(self, hwnd, pid, title):
        self.hwnd, self.pid, self.title = hwnd, pid, title
        self.exe = ""

    def match(self, pattern):
        """按标题或进程名匹配。窗口标题往往是文档名（'SKILL.md - Notepad'），
        照标题找不到东西，所以进程名也要能匹配。"""
        p = str(pattern).casefold()
        return p in self.title.casefold() or p in self.exe.replace(".exe", "")


def enum_windows(visible_only=True):
    out = []
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _):
        if visible_only and not u32.IsWindowVisible(h):
            return True
        n = u32.GetWindowTextLengthW(h)
        if n:
            b = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(h, b, n + 1)
            pid = wintypes.DWORD()
            u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
            out.append(WND(h, pid.value, b.value))
        return True

    u32.EnumWindows(CB(cb), 0)
    for w in out:
        w.exe = proc_exe(w.pid)
    return out


def integrity(pid):
    """目标进程完整性级别。取不到返回 None（受保护进程，必然注入不了）。
    判定注入可行性用：目标 IL > 本进程 IL 时，UIPI 会静默丢弃我们的输入。"""
    h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        tok = wintypes.HANDLE()
        if not a32.OpenProcessToken(h, 0x0008, ctypes.byref(tok)):
            return None
        try:
            size = wintypes.DWORD()
            a32.GetTokenInformation(tok, 25, None, 0, ctypes.byref(size))  # TokenIntegrityLevel
            buf = ctypes.create_string_buffer(size.value)
            if not a32.GetTokenInformation(tok, 25, buf, size.value, ctypes.byref(size)):
                return None
            sid = int.from_bytes(bytes(buf[:8]), "little")
            cnt = ctypes.cast(sid, ctypes.POINTER(ctypes.c_ubyte))[1]
            return ctypes.cast(sid + 8 + 4 * (cnt - 1), ctypes.POINTER(ctypes.c_uint32))[0]
        finally:
            k32.CloseHandle(tok)
    finally:
        k32.CloseHandle(h)


IL_NAMES = {0x0000: "Untrusted", 0x1000: "Low", 0x2000: "Medium",
            0x3000: "High", 0x4000: "System"}


def injectable(pid):
    """(能否注入, 级别名)。None 级别 = 打不开 = 不可注入。"""
    il = integrity(pid)
    return (il is not None and il <= _SELF_IL), IL_NAMES.get(il, str(il))



def hang_check(hwnd, ms=300):
    """窗口还在不在响应消息。True = 活着。

    主动探活，不读系统标记：SendMessageTimeout(WM_NULL) 要求目标把消息从队列里
    取出来才会返回，真卡住就超时。IsHungAppWindow 只反映系统自己的判定，
    短时卡顿看不出来，而短时卡顿恰恰是最需要知道的。
    """
    res = ctypes.c_void_p()
    return bool(u32.SendMessageTimeoutW(wintypes.HWND(hwnd), 0x0000, 0, 0,
                                        0x0002 | 0x0001, ms, ctypes.byref(res)))

_SELF_IL = integrity(k32.GetCurrentProcessId()) or 0x2000


def _force_foreground(hwnd, tries=3):
    """把窗口真正置前。Windows 默认禁止后台进程抢前台（SetForegroundWindow 会
    静默失败），标准解法是先把自己的线程接到当前前台线程上，再置前。

    **绝不改动窗口的显示状态。** 最小化的窗口不替你还原：ShowWindow / 最大化会让
    Chromium 内核程序带着重草稿做窗口重排，实测把微信卡死过（AppHangB1，WER 级
    Critical）。遇到最小化就报错交回上层，让人来决定。
    """
    if u32.IsIconic(hwnd):
        raise CuError("CU_MINIMIZED",
                      "目标窗口处于最小化。本脚本不替你还原窗口——切显示状态对 "
                      "Chromium 内核程序有卡死风险（实测过）。但**用鼠标点任务栏/托盘"
                      "图标把它拉起来是安全的**：先 shot 定位图标，click 一下，再重试。"
                      "不行再让用户自己点开，或先 focus-cc 把界面切出来问用户。")
    for _ in range(tries):
        if u32.GetForegroundWindow() == hwnd:
            return True
        fg = u32.GetForegroundWindow()
        tid_fg = u32.GetWindowThreadProcessId(fg, None)
        tid_me = k32.GetCurrentThreadId()
        attached = bool(u32.AttachThreadInput(tid_me, tid_fg, True))
        try:
            u32.BringWindowToTop(hwnd)
            u32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                u32.AttachThreadInput(tid_me, tid_fg, False)
        time.sleep(0.25)
        if u32.GetForegroundWindow() == hwnd:
            return True
        # 退路：自己发一次 ALT，绕开"最后输入事件不属于本进程"的限制。
        # 同样必须成对兜底——留下按住的 Alt 会让用户以为键盘坏了。
        _PRESSED_MODS.add("alt")
        try:
            _key(VK["alt"]); _key(VK["alt"], up=True)
        finally:
            _PRESSED_MODS.discard("alt")
        u32.SetForegroundWindow(hwnd)
        time.sleep(0.25)
    return u32.GetForegroundWindow() == hwnd


def window_rect(hwnd):
    """窗口在屏幕上的外接矩形（物理像素）。用来算 --region 该裁哪儿。"""
    r = wintypes.RECT()
    if not u32.GetWindowRect(hwnd, ctypes.byref(r)):
        raise CuError("CU_NO_WINDOW", "GetWindowRect 失败")
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def find_windows(pattern):
    return [w for w in enum_windows() if w.match(pattern)]


def focus(pattern):
    """精确聚焦：别名 -> hwnd 数字 -> 标题子串。返回落实到的窗口。
    比 alt+tab 可靠：alt+tab 只能在最近两个窗口间切，且顺序会漂。"""
    wins = enum_windows()
    target = None
    if str(pattern).isdigit():
        h = int(pattern)
        target = next((w for w in wins if w.hwnd == h), None)
        if target is None:
            raise CuError("CU_NO_WINDOW", "hwnd=%s 不是当前可见窗口" % h)
    else:
        hits = [w for w in wins if w.match(pattern)]
        if not hits:
            raise CuError("CU_NO_WINDOW",
                          "没有标题或进程名含 %r 的可见窗口。用 cu.py windows 看有哪些" % pattern)
        target = hits[0]
    if not hang_check(target.hwnd):
        print("⚠ 窗口 %r 不响应消息（可能已卡死）。切过去了，但它多半不会响应点击——"
              "先 focus-cc 把终端切出来告诉用户，别继续注入。" % target.title[:30],
              file=sys.stderr)
    ok, level = injectable(target.pid)
    if not ok:
        raise CuError("CU_UIPI",
                      "窗口 %r (pid=%d, 完整性级别 %s) 高于本进程，切过去也点不动"
                      % (target.title[:30], target.pid, level))
    active = _force_foreground(target.hwnd)
    return target, active


def proc_exe(pid):
    """进程可执行文件名（小写）。拿不到返回空串。"""
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(600)
        size = wintypes.DWORD(600)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        k32.CloseHandle(h)


# DSH 的界面可能是桌面应用（Electron），也可能是承载 dsh 的终端。
# CU_HOST_EXES 用 ";" 分隔，可整体覆盖；桌面应用永远排在终端前面。
HOST_EXES = [h.strip().lower() for h in (
    os.environ.get("CU_HOST_EXES")
    or "deepseek harness.exe;windowsterminal.exe;code.exe;code - insiders.exe;"
       "conhost.exe;powershell.exe;pwsh.exe;cmd.exe;mintty.exe;"
       "wezterm-gui.exe;alacritty.exe;tabby.exe").split(";") if h.strip()]


def focus_cc(pattern=None):
    """把 DSH 的界面切到前台。挂机场景用：屏幕上全是操作画面时，
    用户看不到我这边说了什么。"""
    wins = enum_windows()
    if pattern:
        hits = [w for w in wins if w.match(pattern)]
    else:
        hits = [w for w in wins if w.exe in HOST_EXES]
        hits.sort(key=lambda w: HOST_EXES.index(w.exe))
    if not hits:
        raise CuError("CU_NO_WINDOW",
                      "找不到 DSH 界面窗口，用 cu.py windows 看看，或 cu.py focus-cc <标题子串>")
    t = hits[0]
    return t, _force_foreground(t.hwnd)


def start_apps():
    """Get-StartApps：用户视角的应用目录（开始菜单里能搜到的名字）。
    返回 [(name, appid)]。中文必须靠 UTF8 控制台编码，否则经 CP936 乱码。"""
    ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
          "Get-StartApps | ForEach-Object { $_.Name + [char]9 + $_.AppID }")
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=20)
    except subprocess.TimeoutExpired:
        # 没有超时的话 PowerShell 一卡就是无限等待，而且不吐错误码，
        # 上层没法按码决定恢复策略。
        raise CuError("CU_APP_NOT_FOUND", "Get-StartApps 超过 20 秒没返回（PowerShell 卡住）")
    apps = []
    for line in r.stdout.splitlines():
        if "\t" in line:
            n, a = line.split("\t", 1)
            apps.append((n.strip(), a.strip()))
    if not apps:
        raise CuError("CU_APP_NOT_FOUND", "Get-StartApps 返回空（%s）" % r.stderr.strip()[:80])
    return apps


# UIA 查询脚本：参数走环境变量，避免引号与中文在 PowerShell 命令行里被吃掉。
_UIA_PS = r"""
[Console]::OutputEncoding=[Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
$q = $env:CU_UIA_Q
$w = $env:CU_UIA_WIN
$lim = [int]$env:CU_UIA_LIMIT
$root = [System.Windows.Automation.AutomationElement]::RootElement
$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
$target = $null
if ($w -ne '') {
  $c = $walker.GetFirstChild($root)
  while ($c) {
    if ($c.Current.Name -like "*$w*") { $target = $c; break }
    $c = $walker.GetNextSibling($c)
  }
} else {
  $target = [System.Windows.Automation.AutomationElement]::FocusedElement
  if ($target) {
    while ($true) {
      $p = $walker.GetParent($target)
      if (-not $p -or $p -eq $root) { break }
      $target = $p
    }
  }
}
if (-not $target) { Write-Output '[CU_NO_WINDOW]'; exit 2 }
$out = New-Object System.Collections.Generic.List[string]
$seen = 0
function Walk($el, $d) {
  if ($script:out.Count -ge $lim -or $script:seen -gt 4000 -or $d -gt 14) { return }
  $script:seen = $script:seen + 1
  $ct = $el.Current.ControlType.ProgrammaticName -replace 'ControlType.',''
  $n = $el.Current.Name
  if ($n -and $n -like "*$q*") {
    $r = $el.Current.BoundingRectangle
    if (-not [double]::IsNaN($r.X) -and $r.Width -gt 0) {
      $cx = [int]($r.X + $r.Width / 2)
      $cy = [int]($r.Y + $r.Height / 2)
      $script:out.Add($ct + [char]9 + $n + [char]9 + $cx + [char]9 + $cy)
    }
  }
  $ch = $walker.GetFirstChild($el)
  while ($ch) { Walk $ch ($d + 1); $ch = $walker.GetNextSibling($ch) }
}
Walk $target 0
$out
"""


def uia_find(query, window=None, limit=20, timeout=25):
    """用 Windows UI Automation 找控件：直接问系统「这个控件叫什么、在哪」，**不截图**。

    返回 [(控件类型, 名字, 点击x, 点击y)]，坐标是物理像素。
    自绘界面（很多 Qt / 游戏 / 部分聊天软件）不暴露可访问性树，那时返回空列表 ——
    调用方应退回 shot 看图定位。这是快路，不是唯一的路。
    """
    env = dict(os.environ)
    env["CU_UIA_Q"] = query
    env["CU_UIA_WIN"] = window or ""
    env["CU_UIA_LIMIT"] = str(max(1, min(200, limit)))
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                            "-Command", _UIA_PS],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise CuError("CU_OS", "UI Automation 查询超过 %ds 没返回" % timeout)
    if "[CU_NO_WINDOW]" in (r.stdout or ""):
        raise CuError("CU_NO_WINDOW", "UIA 里没有标题含 %r 的顶层窗口" % (window or ""))
    rows = []
    for line in (r.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        try:
            rows.append((parts[0], parts[1], int(parts[2]), int(parts[3])))
        except ValueError:
            continue
    return rows


def spawn_process(exe, args=(), cwd=None):
    """对齐豆包 win_global.create_process：直接起一个可执行文件。

    为什么**不经过 shell**：不经过 cmd/powershell 就没有通配符展开、没有引号地狱，
    参数原样交给进程——这也正是"安全地起一个程序"该有的形状。
    要跑 shell 命令请用宿主自己的 shell 工具，不要把 shell 语义塞进这里。
    """
    exe = str(exe or "").strip()
    if not exe:
        raise CuError("CU_BAD_ARG", "spawn 需要一个可执行文件路径")
    resolved = exe if os.path.isabs(exe) else shutil.which(exe)
    if not resolved or not os.path.exists(resolved):
        raise CuError("CU_APP_NOT_FOUND",
                      "找不到可执行文件 %r。给绝对路径，或确认它在 PATH 里。" % exe)
    argv = [resolved] + [str(a) for a in (args or [])]
    if cwd:
        cwd = os.path.abspath(str(cwd))
        if not os.path.isdir(cwd):
            raise CuError("CU_BAD_ARG", "工作目录不存在：%s" % cwd)
    try:
        p = subprocess.Popen(argv, cwd=cwd or None, close_fds=True)
    except OSError as e:
        raise CuError("CU_OS", "启动失败：%s" % e)
    return {"pid": p.pid, "exe": resolved, "argv": argv[1:], "cwd": cwd}


def app_is_running(query):
    """对齐豆包 app.is_running：这个应用现在有没有在跑。

    用两条独立证据：① 可见窗口的标题/进程名匹配；② 应用目录里解析出的规范名。
    为什么不用 tasklist 全文：`launch` 走的是 AppsFolder，很多应用的进程名和
    显示名对不上（"计算器" 的进程是 CalculatorApp.exe），只按进程名会漏。
    """
    q = str(query or "").strip()
    if not q:
        raise CuError("CU_BAD_ARG", "app-is-running 需要一个应用名或 AppID")
    ql = q.casefold()
    matches, seen = [], set()
    for w in enum_windows(visible_only=True):
        if w.pid in seen:
            continue
        try:
            exe = proc_exe(w.pid) or ""
        except Exception:
            exe = ""
        base = os.path.basename(exe).casefold()
        if ql in w.title.casefold() or ql in base:
            seen.add(w.pid)
            matches.append({"hwnd": w.hwnd, "pid": w.pid, "exe": exe,
                            "title": w.title[:60]})
    catalog_name = None
    try:
        catalog_name = resolve_app(q)[0]
    except CuError:
        pass
    if catalog_name:
        cl = catalog_name.casefold()
        for w in enum_windows(visible_only=True):
            if w.pid in seen:
                continue
            if cl in w.title.casefold():
                seen.add(w.pid)
                try:
                    exe = proc_exe(w.pid) or ""
                except Exception:
                    exe = ""
                matches.append({"hwnd": w.hwnd, "pid": w.pid, "exe": exe,
                                "title": w.title[:60]})
    return {"query": q, "catalog_name": catalog_name,
            "running": bool(matches), "matches": matches}


def _window_for(query, what="app"):
    hits = find_windows(str(query))
    if not hits:
        raise CuError("CU_NO_WINDOW",
                      "%s：没有标题或进程名含 %r 的可见窗口" % (what, query))
    return hits[0]


def app_resize(query, width, height):
    """对齐豆包 app.resize：把窗口改成指定大小（不动位置、不动 Z 序）。"""
    try:
        w = int(width)
        h = int(height)
    except (TypeError, ValueError):
        raise CuError("CU_BAD_ARG", "宽高要是整数，收到 %r x %r" % (width, height))
    if not 100 <= w <= 10000 or not 80 <= h <= 10000:
        raise CuError("CU_BAD_ARG", "宽高需在 100..10000 之间，收到 %dx%d" % (w, h))
    win = _window_for(query, "app-resize")
    # SWP_NOMOVE(0x0002) | SWP_NOZORDER(0x0004)：只改尺寸
    ok = u32.SetWindowPos(win.hwnd, None, 0, 0, w, h, 0x0002 | 0x0004)
    if not ok:
        raise CuError("CU_OS", "SetWindowPos 失败（错误码 %s）"
                      % ctypes.get_last_error())
    rx, ry, rw, rh = window_rect(win.hwnd)
    return {"hwnd": win.hwnd, "title": win.title[:60],
            "requested": [w, h], "actual": [rw, rh]}


def app_terminate(query, confirm=False):
    """对齐豆包 app.terminate。**破坏性**：没显式确认一律拒绝。

    为什么必须确认：它会丢掉应用里所有未保存内容，而且不可撤销。
    自动化里最危险的不是"点错坐标"，而是"把用户没存的东西关掉"。
    """
    win = _window_for(query, "app-terminate")
    if not confirm:
        raise CuError("CU_CONFIRM_REQUIRED",
                      "要关掉 %r（pid=%d）需要显式确认：加 --confirm（工具里传 confirm:true）。"
                      "这会丢掉它所有未保存内容，且不可撤销。"
                      % (win.title[:40], win.pid))
    r = subprocess.run(["taskkill", "/PID", str(win.pid), "/T", "/F"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=20)
    ok = r.returncode == 0
    return {"hwnd": win.hwnd, "pid": win.pid, "title": win.title[:60],
            "ok": ok, "output": (r.stdout or r.stderr or "").strip()[:200]}


# 对齐豆包 hybird 判定用的浏览器清单（它写在 aha_cua.dll 里的 chrome/msedge/...）
BROWSER_EXES = ("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe",
                "opera.exe", "vivaldi.exe", "iexplore.exe")


def hybrid_programs():
    """对齐豆包 win_global.dump_hybrid_programs：哪些可见窗口"其实是浏览器"。

    用途只有一个——**选平面**：这些窗口里的网页操作应该走 browser_use（CDP），
    而不是截图估坐标。所以这里只报"像浏览器的窗口 + 有没有可用 CDP 端点"。
    """
    rows = []
    for w in enum_windows(visible_only=True):
        try:
            exe = proc_exe(w.pid) or ""
        except Exception:
            exe = ""
        if os.path.basename(exe).casefold() in BROWSER_EXES:
            rows.append({"hwnd": w.hwnd, "pid": w.pid, "exe": exe,
                         "title": w.title[:60]})
    cdp = _scan_cdp()
    return {"browsers": rows, "cdp_endpoints": cdp,
            "advice": ("网页内的操作走 browser_use；这些窗口对应的浏览器实例若没有"
                       "CDP 端点，用 scripts/chrome-debug.cmd 起一个独立 profile 的实例。"
                       if rows else
                       "没检测到浏览器窗口。若目标其实是网页，先确认应用名（可能有别的浏览器进程名）。")}


def resolve_app(query):
    """解析应用名。AppID 天然唯一，所以先用 AppID 精确匹配；
    名字重名时**不能静默取第一个**——本机 221 项里就有 8 组重名
    （Tina Help / Read Me / Quick Start Manual …），静默取错等于开错应用。"""
    apps = start_apps()
    q = query.strip()
    by_id = [a for a in apps if a[1] == q]
    if len(by_id) == 1:
        return by_id[0]
    ql = q.casefold()
    exact = [a for a in apps if a[0].casefold() == ql]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise CuError("CU_APP_AMBIGUOUS",
                      "有 %d 个应用都叫 %r，请改用 AppID 指定：\n%s"
                      % (len(exact), query,
                         "\n".join("  %s" % aid for _, aid in exact[:6])))
    part = [a for a in apps if ql in a[0].casefold()]
    if len(part) == 1:
        return part[0]
    if not part:
        raise CuError("CU_APP_NOT_FOUND",
                      "目录里没有匹配 %r 的应用。用 cu.py apps <关键词> 先看看" % query)
    raise CuError("CU_APP_AMBIGUOUS",
                  "%r 匹配到 %d 个：%s\n（用更完整的名字，或直接用 AppID）"
                  % (query, len(part), "；".join(n for n, _ in part[:8])))


def launch(query, timeout=10.0, do_focus=True):
    """启动应用并验证窗口出现。返回 (status, wnd or None, name, appid)。
    status: ready = 稳定窗口已出现；accepted_unverified = 系统收下了但没等到窗口。

    两个必须踩过的坑：
    - 打包应用（UWP）启动时会先冒一个瞬态窗口然后消失，真正稳定的窗口往往属于
      ApplicationFrameHost.exe 而不是应用自己的进程。所以新窗口要活过一轮复查才采信。
    - 新窗口不一定会自己到前台（终端最大化时会被压在后面），所以默认置前。
    """
    name, appid = resolve_app(query)
    before = {w.hwnd for w in enum_windows()}
    subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + appid])
    deadline = time.time() + timeout
    seen_new, stable = set(), []
    while time.time() < deadline:
        time.sleep(0.45)
        cur = {w.hwnd: w for w in enum_windows()}
        stable = [cur[h] for h in seen_new if h in cur]     # 上一轮见过、这一轮还在
        for h in cur:
            if h not in before:
                seen_new.add(h)
        if stable:
            break
        if not seen_new and time.time() > deadline - timeout + 4 and \
                any(name.casefold() in w.title.casefold() for w in cur.values()):
            return "accepted_unverified", None, name, appid, False   # 本来就开着，别干等
    if not stable:
        return "accepted_unverified", None, name, appid, False
    key = name.casefold()
    named = [w for w in stable if key in w.title.casefold()]
    pick = (named or stable)[0]
    if do_focus:
        _force_foreground(pick.hwnd)
    # owned=False 时这个窗口未必真属于该应用（可能只是同时冒出来的别的窗口），
    # 调用方要把这点告诉模型，别默默当真。
    return "ready", pick, name, appid, bool(named)


# ---------------------------------------------------------------- 截图
SHOT_DIR = os.path.join(tempfile.gettempdir(), "cu")
SHOT_KEEP = 12          # 只留最近 N 张，避免临时目录越堆越多


def _prune_shots():
    try:
        files = sorted((os.path.join(SHOT_DIR, f) for f in os.listdir(SHOT_DIR)
                        if f.startswith("cu_")), key=os.path.getmtime)
        for p in files[:-SHOT_KEEP]:
            try:
                os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


def _lit_ratio(img):
    """画面里"非黑"像素的占比。用来区分两种黑帧：
    全屏独占程序（游戏/硬件加速播放器）——正常现象，换个方式抓；
    安全桌面（UAC 提示、锁屏）——我们根本不在可输入桌面上，再注入也没用。"""
    try:
        data = img.convert("L").resize((64, 36)).tobytes()
        return sum(1 for v in data if v > 16) / float(len(data))
    except Exception:
        return 1.0


def input_desktop_ok():
    """当前线程是否在"可输入桌面"上。锁屏 / UAC 安全桌面会返回 False。"""
    u32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    u32.OpenInputDesktop.restype = wintypes.HANDLE
    h = u32.OpenInputDesktop(0, False, 0x0100)   # DESKTOP_SWITCHDESKTOP
    if not h:
        return False
    u32.CloseDesktop(h)
    return True


def _validate_png(png, width=None, height=None):
    """严格校验一张抓到的 PNG（对齐豆包 ScreenshotResponse._validate_png）。

    必须校验，因为下游是"模型看图"：一张截断的 PNG 在多数解码器里会变成
    半张图或纯色图，而模型不会说"这图是坏的"，它会照着半张图推理。
    宁可在这里报 CU_SHOT_FAIL。
    """
    if not isinstance(png, (bytes, bytearray)):
        raise CuError("CU_SHOT_FAIL", "截图不是字节串")
    if (len(png) < 33 or bytes(png[:8]) != b"\x89PNG\r\n\x1a\n"
            or bytes(png[8:12]) != b"\x00\x00\x00\r"
            or bytes(png[12:16]) != b"IHDR"
            or bytes(png[-12:-8]) != b"\x00\x00\x00\x00"
            or bytes(png[-8:-4]) != b"IEND"):
        raise CuError("CU_SHOT_FAIL", "这不是一张完整 PNG（签名/IHDR/IEND 不齐）")
    pw, ph = struct.unpack(">II", bytes(png[16:24]))
    if pw <= 0 or ph <= 0:
        raise CuError("CU_SHOT_FAIL", "PNG 尺寸非法 %dx%d" % (pw, ph))
    if width is not None and height is not None and (pw, ph) != (int(width), int(height)):
        raise CuError("CU_SHOT_FAIL",
                      "PNG 自身宽高 %dx%d 与声明的 %dx%d 不一致"
                      % (pw, ph, width, height))
    return pw, ph


def capture_png(long_edge=1280, region=None):
    """只抓图、不落盘。返回 (png_bytes, meta)。

    抽出来是为了让常驻服务能把 PNG 直接塞进图片帧（不落临时文件），
    同时 shot() 保持原有"落盘 + 返回同一组字段"的行为，CLI 契约一个都不变。
    """
    try:
        from PIL import ImageGrab
    except ImportError:
        raise CuError("CU_NO_PIL", "缺少 Pillow，截图不可用。装它：%s" % PIP_HINT)
    try:
        img = ImageGrab.grab(bbox=region, all_screens=True) if region \
            else ImageGrab.grab(all_screens=True)
    except Exception as e:
        if not on_input_desktop():
            raise CuError("CU_SHADOW_NO_SCREEN",
                          "当前在**非输入桌面**（隔离桌面）上，GDI 抓不到屏幕。"
                          "改用 shot --window <窗口名>（走 PrintWindow），"
                          "或传 desktop:'main' 到用户桌面截屏。原始错误：%s" % (e or type(e).__name__))
        raise CuError("CU_SHOT_FAIL", str(e) or type(e).__name__)
    return _encode_png_img(img, long_edge, region, "gdi")


# ---------------------------------------------------------------- PrintWindow 后端
# 豆包的截图是 WGC（Windows.Graphics.Capture）主路 + GDI 兜底，好处是**被遮挡的窗口也能截**、
# 不必先抢前台。WGC 要 WinRT + D3D11 互操作（ctypes 里极易 access violation），风险不划算；
# 这里用主流替代：PrintWindow(PW_RENDERFULLCONTENT)——同样能截遮挡窗口，且不动 Z 序、不抢焦点。
PW_RENDERFULLCONTENT = 0x00000002


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


g32 = ctypes.WinDLL("gdi32", use_last_error=True)

# 64 位下句柄/指针必须显式声明 restype，否则会被当 32 位 int 截断 → access violation。
_GWR = u32.GetWindowRect
_GWR.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_GWR.restype = wintypes.BOOL
_PW = u32.PrintWindow
_PW.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
_PW.restype = wintypes.BOOL
_GWDC = u32.GetWindowDC
_GWDC.argtypes = [wintypes.HWND]
_GWDC.restype = wintypes.HDC
_RDC = u32.ReleaseDC
_RDC.argtypes = [wintypes.HWND, wintypes.HDC]
_RDC.restype = ctypes.c_int
_CDC = g32.CreateCompatibleDC
_CDC.argtypes = [wintypes.HDC]
_CDC.restype = wintypes.HDC
_CBM = g32.CreateCompatibleBitmap
_CBM.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
_CBM.restype = wintypes.HBITMAP
_SO = g32.SelectObject
_SO.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_SO.restype = wintypes.HGDIOBJ
_DO = g32.DeleteObject
_DO.argtypes = [wintypes.HGDIOBJ]
_DO.restype = wintypes.BOOL
_DDC = g32.DeleteDC
_DDC.argtypes = [wintypes.HDC]
_DDC.restype = wintypes.BOOL
_GDB = g32.GetDIBits
_GDB.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                 ctypes.c_void_p, ctypes.POINTER(_BITMAPINFO), wintypes.UINT]
_GDB.restype = ctypes.c_int

# Win11 的窗口在 GetWindowRect 里带了一圈**不可见的调整边框**（实测 2582x1390 对 2560x1440），
# 直接按 GetWindowRect 截会多出一圈黑边/桌面残影。DWM 的 EXTENDED_FRAME_BOUNDS 才是
# 用户真正看得见的那块，用它裁一下。
DWMWA_EXTENDED_FRAME_BOUNDS = 9
try:
    _dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
    _DWA = _dwm.DwmGetWindowAttribute
    _DWA.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    _DWA.restype = ctypes.c_long
except OSError:                                   # pragma: no cover
    _dwm = None


def _visible_frame_bounds(hwnd):
    if _dwm is None:
        return None
    r = wintypes.RECT()
    hr = _DWA(wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS,
              ctypes.byref(r), ctypes.sizeof(r))
    if hr != 0:
        return None
    return (r.left, r.top, r.right, r.bottom)


def capture_window_png(hwnd):
    """问窗口要一张它自己的图。返回 (PIL.Image, printwindow_ok)。

    `printwindow_ok=False` 只说明 PrintWindow 的返回值是假，图可能仍然是好的
    （部分应用对 flag 不友好但画得出来），所以由调用方看亮度再决定用不用。
    """
    from PIL import Image
    r = wintypes.RECT()
    if not _GWR(hwnd, ctypes.byref(r)):
        raise CuError("CU_SHOT_FAIL", "GetWindowRect 失败")
    w, h = int(r.right - r.left), int(r.bottom - r.top)
    if w <= 0 or h <= 0:
        raise CuError("CU_SHOT_FAIL", "窗口尺寸非法 %dx%d" % (w, h))
    hwnd_dc = _GWDC(hwnd)
    if not hwnd_dc:
        raise CuError("CU_SHOT_FAIL", "GetWindowDC 失败")
    mfc = bmp = old = None
    try:
        mfc = _CDC(hwnd_dc)
        if not mfc:
            raise CuError("CU_SHOT_FAIL", "CreateCompatibleDC 失败")
        bmp = _CBM(hwnd_dc, w, h)
        if not bmp:
            raise CuError("CU_SHOT_FAIL", "CreateCompatibleBitmap 失败")
        old = _SO(mfc, bmp)
        ok = bool(_PW(hwnd, mfc, PW_RENDERFULLCONTENT))
        if not ok:
            # 有些老应用只认老调用；两个都不行才让上层退回屏幕裁剪
            ok = bool(_PW(hwnd, mfc, 0))
        bi = _BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h        # 负高 = 自上而下，省掉翻行
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0    # BI_RGB
        buf = ctypes.create_string_buffer(w * h * 4)
        n = _GDB(mfc, bmp, 0, h, buf, ctypes.byref(bi), 0)
        if n == 0:
            raise CuError("CU_SHOT_FAIL", "GetDIBits 失败")
        # frombuffer 会与 ctypes 缓冲区共享内存，这里用 frombytes 拷一份更稳
        img = Image.frombytes("RGB", (w, h), bytes(buf), "raw", "BGRX", 0, 1)
        # 裁掉 Win11 那圈不可见边框；裁失败（或本来就没边框）就保持原样
        vb = _visible_frame_bounds(hwnd)
        if vb:
            l = max(0, vb[0] - r.left)
            t = max(0, vb[1] - r.top)
            rt = min(w, vb[2] - r.left)
            b = min(h, vb[3] - r.top)
            if rt - l >= 32 and b - t >= 32 and (rt - l, b - t) != (w, h):
                img = img.crop((l, t, rt, b))
        return img, ok
    finally:
        try:
            if old:
                _SO(mfc, old)
            if bmp:
                _DO(bmp)
            if mfc:
                _DDC(mfc)
        finally:
            _RDC(hwnd, hwnd_dc)


def _encode_png_img(img, long_edge, region=None, capture_path="gdi"):
    """把 PIL 图编码成 PNG 并产出 meta。缩放/token 估算口径只有这一处。"""
    src_w, src_h = img.size
    scale = 1.0
    if long_edge and max(src_w, src_h) > long_edge:
        k = long_edge / float(max(src_w, src_h))
        img = img.resize((max(1, int(src_w * k)), max(1, int(src_h * k))))
        scale = k
    if img.size[0] <= 0 or img.size[1] <= 0:
        raise CuError("CU_SHOT_FAIL", "截出来的图是空的（%dx%d）" % img.size)
    lit = _lit_ratio(img)
    buf = io.BytesIO()
    try:
        img.save(buf, format="PNG")
    except Exception as e:
        raise CuError("CU_SHOT_FAIL", "PNG 编码失败：%s" % e)
    png = buf.getvalue()
    w, h = _validate_png(png, img.size[0], img.size[1])
    vx, vy, vw, vh = screen()
    meta = {
        "width": w, "height": h,
        "source": {"width": src_w, "height": src_h},
        "physical": {"width": vw, "height": vh},
        "region": list(region) if region else None,
        "scale": round(scale, 4),
        "bytes": len(png),
        "lit_ratio": round(lit, 4),
        # 图像 token 估算口径：沿用本技能既有实测（长边 1280 ≈ 1200 token），
        # 即每约 750 px² 计 1 token。只当预算用，不是计费口径。
        "estimatedImageTokens": int(w * h / 750),
        "capture_path": capture_path,
    }
    return png, meta


def capture_shot(a):
    """按 shot 的参数抓一帧，返回 (png, meta)。**CLI / emit-binary / 常驻服务共用这一条**。

    后端阶梯（对齐豆包"先 WGC 再 GDI"的思路）：
      1. 指定了 --window → 先 PrintWindow（被遮挡也能截、不抢前台）
      2. PrintWindow 拿不到 / 截成黑 → 退回屏幕裁剪（会做遮挡检查，遮了就 CU_COVERED）
    """
    win = None
    if getattr(a, "window", None):
        hits = find_windows(a.window)
        if not hits:
            raise CuError("CU_NO_WINDOW",
                          "没有标题或进程名含 %r 的可见窗口" % a.window)
        win = hits[0]
        if u32.IsIconic(win.hwnd):
            raise CuError("CU_MINIMIZED",
                          "窗口 %r 是最小化的，截不到内容。可以让用户点开，也可以用 "
                          "cu.py click 点任务栏图标把它拉起来（先 shot 看图标位置）。"
                          % win.title[:24])
    le = _shot_le(a)      # 统一决策点，顺带完成 --long 的校验

    # 非输入桌面（隔离桌面）上 GDI 抓不到屏幕，而且 `ImageGrab.grab()` 在那里
    # 可能**直接卡住**（实测把整条服务挂死到超时）。所以先判桌面，再决定要不要抓：
    # 有窗口就走 PrintWindow（不依赖屏幕），没窗口就明确报错，不硬试。
    if win is None and not on_input_desktop():
        raise CuError("CU_SHADOW_NO_SCREEN",
                      "当前在隔离桌面（非输入桌面）上，抓不到“屏幕”。"
                      "改用 shot window=<窗口名>（PrintWindow，不依赖屏幕），"
                      "或传 desktop:'main'（需用户批准）到用户桌面截屏。")

    if win is not None:
        try:
            img, _ok = capture_window_png(win.hwnd)
            if _lit_ratio(img) > 0.01:
                region = None
                r2 = getattr(a, "region", None)
                if r2 is not None:
                    # --region 与 --window 同时给：在**窗口图**上按屏幕坐标裁交集
                    wx, wy, ww, wh = window_rect(win.hwnd)
                    ix0, iy0, ix1, iy1 = _intersect(_region_arg(r2, "--region"),
                                                    (wx, wy, wx + ww, wy + wh))
                    box = (ix0 - wx, iy0 - wy, ix1 - wx, iy1 - wy)
                    img = img.crop(box)
                    region = [ix0, iy0, ix1, iy1]
                png, meta = _encode_png_img(img, le, region, "printwindow")
                meta["window"] = win.title[:38]
                return png, meta
        except CuError:
            pass          # 落到下面的屏幕裁剪
    region, win_note, le2 = resolve_shot_region(a)
    png, meta = capture_png(le2, region)
    if win is not None:
        meta["window"] = win.title[:38]
    return png, meta


def shot(out=None, long_edge=1280, region=None, keep_all=False):
    """region 给 x,y,w,h（左上角 + 宽高，不是 l,t,r,b；单位是物理像素）。
    给 long_edge 就等比缩放；长边不超过 long_edge 时保持原样（看小字要的就是这个）。"""
    png, meta = capture_png(long_edge, region)
    os.makedirs(SHOT_DIR, exist_ok=True)
    if out is None:
        out = os.path.join(SHOT_DIR, "cu_%d.png" % int(time.time() * 1000))
    else:
        os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    try:
        with open(out, "wb") as f:
            f.write(png)
    except Exception as e:
        raise CuError("CU_SHOT_FAIL", "写盘失败 %s：%s" % (out, e))
    # 落盘后再校验一次文件：磁盘满/被杀这类情况会写出截断文件，
    # 而调用方随后要用 read_image 打开它——坏文件必须在这里就暴露。
    try:
        with open(out, "rb") as f:
            _validate_png(f.read(), meta["width"], meta["height"])
    except CuError:
        try:
            os.remove(out)
        except OSError:
            pass
        raise
    if not keep_all:
        _prune_shots()
    return (out, (meta["width"], meta["height"]), meta["bytes"],
            meta["scale"], meta["lit_ratio"])


# ---------------------------------------------------------------- 持续监看
# 等待期间全部在脚本里跑（截图、比对、丢弃），你这边一个 token 都不产生，
# 所以理论上可以等无限久。正确用法：`bash` 带 run_in_background 启动它——
# DSH 里前台命令超时会自动转成后台 job，但等待本身仍要显式后台起。
FP_WIDTH = 480          # 指纹宽度。缩到这个宽度再比，既抗锯齿噪声又跑得快
DIFF_LEVEL = 16         # 灰度差超过它才算"这个像素变了"

# 下面三个是监看的内部参数，**故意不做成命令行参数**：用的人只该关心"盯哪儿、盯多久"，
# 不该关心脚本怎么判。调参归调参，用的时候看不见。
WATCH_INTERVAL = 3.0    # 比对间隔（秒）。恰好是 Chromium 系光标闪烁周期(1000ms)的 3 倍，
                        # 顺带避开光标；而光标本身也远低于下面的阈值（实测约 30px）。
MIN_CHANGED_PX = 3000   # 判定"画面真的变了"的绝对像素预算。不用百分比，是因为百分比的
                        # 分母是监看区面积——框得越小越灵敏，同一个阈值在窗口模式下比全屏
                        # 灵敏好几倍，噪声（时钟、红点、高亮残影）会被放进来。3000px 实测
                        # 高于所有噪声（时钟约 300、光标约 30、26x26 红点 676），又远低于
                        # 一条新消息气泡（约 18000），两头都留了几倍余量。
STABLE_FRAMES = 3       # 取基线前要求连续 N 帧完全一致。高亮消退、动画收尾这类"落定期"
                        # 变化会让基线取早，之后永远对不上，靠事后去抖救不回来。

# 常驻盯的队列（默认就是常驻）。一次性盯 = "变了就交出一帧然后退出"；常驻盯 = "每次变化压进
# 队列、立刻换基准、自己不退出"。你按自己的节奏用 `收` 顺序取：正在想的那张不会
# 被新变化顶掉，想的期间来的每一张都还排在队里，一张不漏。
WATCHQ_ROOT = os.path.join(SHOT_DIR, "watchq")
# 帧文件什么时候能删。**不能"取走就删"**：通用场景要读整个队列，`收 --all` 一次交出一批
# 路径，还要一张张 `read`；早删一张就等于漏一段过程。
FRAME_KEEP_SECONDS = 600.0   # 取走的帧至少留这么久，给读图留出时间
FRAME_KEEP_MAX = 400         # 帧目录硬上限；超了只从"已取走"里删最旧的
ARCHIVE_KEEP = 300           # 留档目录（--archive）里最多留几张

# 静默窗：注入类命令（click/type/paste/post/key/...）自己把"静默到什么时候"往后推，
# 静默期内常驻盯只换基准、不压队列。这样你自己操作引起的重绘不会变成假唤醒——
# 关键是这件事由脚本自己做，你不需要记得调什么，也就不会忘。
# 代价说清楚：静默期里真来的消息会被一起吞掉，所以操作收尾要自己核验一次（见 im-chat）。
MUTE_SECONDS = 5.0      # 一次注入后静默多久：够覆盖界面重绘 + 一个比对间隔
INJECT_CMDS = {"click", "move", "hover", "move-smooth", "scroll", "drag", "type", "key",
               "key-down", "key-up", "release", "paste", "copy", "paste-image", "post",
               "launch", "focus", "focus-cc", "element",
               # app-resize 会改窗口几何、app-terminate 会关应用，都算"动界面"
               "app-resize", "app-terminate"}

# 审计范围（比注入范围大）：eval/spawn 不是注入，但能起进程/跑任意代码，
# 是绕过 confirm 闸的主要路径，必须留痕。
AUDIT_CMDS = INJECT_CMDS | {"eval", "spawn"}


def _mute_file():
    return os.path.join(SHOT_DIR, "mute")


def _mute(sec):
    """把静默窗推到 max(现在, 此刻+sec)。只往后推不往前提，多次调用自然续租。"""
    try:
        os.makedirs(SHOT_DIR, exist_ok=True)
        cur = 0.0
        try:
            with open(_mute_file(), "r") as f:
                cur = float(f.read().strip() or 0)
        except (OSError, ValueError):
            pass
        with open(_mute_file(), "w") as f:
            f.write("%.3f" % max(cur, time.time() + sec))
    except OSError:
        pass


def _unmute():
    try:
        with open(_mute_file(), "w") as f:
            f.write("0")
    except OSError:
        pass


def _muted_until():
    try:
        with open(_mute_file(), "r") as f:
            return float(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0.0


def _top_root_at(x, y):
    """屏幕上这个点最顶层的根窗口 hwnd。

    "谁真的盖在谁上面"只能这么问——光比矩形重叠，会把"终端最大化但在后台"误判成遮挡，
    结果那条告警在终端最大化时恒亮。恒亮的告警等于教人忽略告警。
    """
    try:
        user32 = ctypes.windll.user32
        pt = wintypes.POINT(int(x), int(y))
        h = user32.WindowFromPoint(pt)
        if not h:
            return None
        r = user32.GetAncestor(h, 2)        # GA_ROOT
        return int(r or h)
    except Exception:
        return None


def _fingerprint(img, ignore=()):
    """把画面缩成固定宽度的灰度指纹。

    缩放本身就是第一道"防无意义时变"：任务栏时钟在 2560 宽的屏上约 70px，
    缩到 480 宽只剩 ~13px，四位数一变也就动几十个指纹像素，占比低于阈值。
    第二道是 ignore 区（用 0-1000 归一化给，免得再算像素）。
    """
    g = img.convert("L")
    if g.width > FP_WIDTH:
        k = FP_WIDTH / float(g.width)
        g = g.resize((FP_WIDTH, max(1, int(round(g.height * k)))))
    w, h = g.size
    buf = bytearray(g.tobytes())
    for (nx0, ny0, nx1, ny1) in ignore:
        ix0 = max(0, min(w, int(nx0 * w / 1000.0)))
        ix1 = max(0, min(w, int(nx1 * w / 1000.0)))
        iy0 = max(0, min(h, int(ny0 * h / 1000.0)))
        iy1 = max(0, min(h, int(ny1 * h / 1000.0)))
        for yy in range(iy0, iy1):
            base = yy * w
            for xx in range(ix0, ix1):
                buf[base + xx] = 0
    return w, h, bytes(buf)


def _diff(a, b, w, h, level=DIFF_LEVEL):
    """比两份指纹，返回 (变化像素占比, 变化区包围盒的归一化四元组或 None)。"""
    if len(a) != len(b):
        return 1.0, None
    changed = 0
    minx, miny, maxx, maxy = w, h, -1, -1
    for i in range(len(a)):
        if abs(a[i] - b[i]) > level:
            changed += 1
            yy, xx = divmod(i, w)
            if xx < minx:
                minx = xx
            if xx > maxx:
                maxx = xx
            if yy < miny:
                miny = yy
            if yy > maxy:
                maxy = yy
    if maxx < 0:
        return 0.0, None
    box = (minx / float(w), miny / float(h),
           (maxx + 1) / float(w), (maxy + 1) / float(h))
    return changed / float(len(a)), box


def on_input_desktop():
    """当前线程所在的桌面是不是"输入桌面"。

    非输入桌面（隔离桌面）上：GDI 抓屏拿不到内容、SendInput 被系统拒绝。
    很多"莫名其妙的失败"其实是没区分这两种桌面。
    """
    try:
        h_input = u32.OpenInputDesktop(0, False, 0x0001)   # DESKTOP_READOBJECTS
        h_here = u32.GetThreadDesktop(k32.GetCurrentThreadId())
        if not h_input:
            return False
        if h_input == h_here:
            u32.CloseDesktop(h_input)
            return True

        def _name(h):
            buf = ctypes.create_unicode_buffer(256)
            need = wintypes.DWORD()
            u32.GetUserObjectInformationW(h, 2, buf, ctypes.sizeof(buf),
                                          ctypes.byref(need))
            return buf.value

        same = _name(h_input) == _name(h_here)
        u32.CloseDesktop(h_input)
        return same
    except Exception:
        return True          # 判不出来就按"能抓"处理，别误报


def _region_arg(spec, what="--region"):
    """解析 x,y,w,h（物理像素），校验并夹进屏幕。

    同时接受 `"x,y,w,h"` 字符串和 `[x,y,w,h]` 列表：服务端把 JSON 里的 region
    原样传下来时是列表，只认字符串会把它 str 成 `"[0, 0, 400, 300]"` 再解析失败
    （或解析出离谱的数字），这是通道层最容易踩的一个坑。
    """
    if isinstance(spec, (list, tuple)):
        parts = list(spec)
    elif isinstance(spec, dict):
        parts = [spec.get(k) for k in ("x", "y", "w", "h")]
    else:
        parts = str(spec).split(",")
    try:
        x, y, w, h = (int(v) for v in parts)
    except ValueError:
        raise CuError("CU_BAD_ARG", "%s 要写 x,y,w,h 四个整数，收到 %r" % (what, spec))
    if w <= 0 or h <= 0:
        raise CuError("CU_BAD_ARG", "%s 的宽高必须为正，收到 w=%d h=%d" % (what, w, h))
    vx, vy, vw, vh = screen()
    x0, y0 = max(x, vx), max(y, vy)
    x1, y1 = min(x + w, vx + vw), min(y + h, vy + vh)
    if x1 <= x0 or y1 <= y0:
        raise CuError("CU_BAD_ARG", "%s %s 与屏幕 %dx%d 没有交集" % (what, spec, vw, vh))
    return (x0, y0, x1, y1)


def _occlusion(region, pid):
    """在监看区里采样，看最上层窗口是不是目标进程的。返回 (被挡点数, 总点数)。

    屏幕监看抓的是"这块区域当前显示什么"，**不是"那个窗口的内容"**。
    窗口一旦被盖住，你监看到的就是盖在它上面的东西，而上面那个东西（终端、
    播放器、任何会动的界面）一变就会把你唤醒——那多半不是你要等的事件。
    所以 arm 之前先确认监看窗口在最前面、而且期间没人会去盖它。
    """
    try:
        u32.WindowFromPoint.argtypes = [wintypes.POINT]
        u32.WindowFromPoint.restype = wintypes.HWND
        u32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        u32.GetAncestor.restype = wintypes.HWND
    except Exception:
        return 0, 1
    x0, y0, x1, y1 = region
    cover = total = 0
    for fx in (0.25, 0.5, 0.75):
        for fy in (0.25, 0.5, 0.75):
            h = u32.WindowFromPoint(wintypes.POINT(int(x0 + (x1 - x0) * fx),
                                                   int(y0 + (y1 - y0) * fy)))
            if not h:
                total += 1
                continue
            # 必须上溯到顶级窗口再取 pid：UWP 窗口挂在 ApplicationFrameHost 下，
            # WindowFromPoint 给的是应用自己进程的子窗口，直接比 pid 会永远不匹配。
            root = u32.GetAncestor(h, 2)          # GA_ROOT
            if root:
                h = root
            total += 1
            p = wintypes.DWORD()
            u32.GetWindowThreadProcessId(h, ctypes.byref(p))
            if p.value != pid:
                cover += 1
    return cover, total


def _host_window_rect():
    """DSH 自己那个窗口的矩形。它是最容易被忽略的"无意义时变源"——
    它一边监看一边输出，界面就在不断重绘，把监看唤醒成假警报。"""
    for w in enum_windows():
        if w.exe in HOST_EXES:
            return window_rect(w.hwnd), w.title, w.hwnd
    return None, None, None


def _intersect(a, b):
    """两个 bbox 的交集。没有交集就报错，不静默取其中一个——
    "以为在看 A、其实在看 B"是这类工具最难查的错。"""
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 - x0 < 16 or y1 - y0 < 16:
        raise CuError("CU_BAD_ARG",
                      "裁出来的区域太小或没有交集：窗口 (%d,%d)-(%d,%d) 与 "
                      "--region (%d,%d)-(%d,%d)" % (b + a))
    return (x0, y0, x1, y1)


def _parse_ts(v, default):
    """起算时刻：认 unix 秒，也认 HH:MM:SS（按今天算）。"""
    s = str(v).strip()
    if ":" in s:
        try:
            hh, mm, ss = (int(x) for x in s.split(":"))
        except ValueError:
            raise CuError("CU_BAD_ARG", "--start-at 时刻要写 HH:MM:SS，收到 %r" % v)
        n = time.localtime()
        return time.mktime((n.tm_year, n.tm_mon, n.tm_mday, hh, mm, ss, 0, 0, -1))
    try:
        return float(s)
    except ValueError:
        raise CuError("CU_BAD_ARG", "--start-at 要写 unix 秒或 HH:MM:SS，收到 %r" % v)


def _ignore_local(rects, reg):
    """把**整屏**归一化的排除区换算成监看区内的归一化坐标。

    在边界上换这一次，_fingerprint 就不必知道整屏。否则同样是 0-1000，在 click、
    --region、--ignore 三处含义各不相同——这是个真踩过的坑，统一在这里收口。
    """
    if not rects:
        return ()
    vx, vy, vw, vh = screen()
    rx0, ry0, rx1, ry1 = reg if reg else (vx, vy, vx + vw, vy + vh)
    rw, rh = float(rx1 - rx0), float(ry1 - ry0)
    out = []
    for a, b, c, d in rects:
        a, c = min(a, c), max(a, c)
        b, d = min(b, d), max(b, d)
        X0 = max(vx + a * vw / 1000.0, rx0)
        Y0 = max(vy + b * vh / 1000.0, ry0)
        X1 = min(vx + c * vw / 1000.0, rx1)
        Y1 = min(vy + d * vh / 1000.0, ry1)
        if X1 <= X0 or Y1 <= Y0:
            continue        # 圈的地方不在监看区里 = 这次不用屏蔽谁，不是错误
        out.append(((X0 - rx0) * 1000.0 / rw, (Y0 - ry0) * 1000.0 / rh,
                    (X1 - rx0) * 1000.0 / rw, (Y1 - ry0) * 1000.0 / rh))
    return tuple(out)



def _盯_qdir(queue):
    """队列目录。默认 default；要同时盯好几处就用 --queue 分开，互不打架。"""
    q = (queue or "default").strip() or "default"
    if q in (".", "..") or any(c in '\\/:*?"<>|' for c in q):
        raise CuError("CU_BAD_ARG", "队列名不能带路径符号：%r" % queue)
    return os.path.join(WATCHQ_ROOT, q)


def _盯_read_jsonl(path):
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass          # 半行/坏行跳过：队列宁可少一条也不能整条读不出来
    except OSError:
        pass
    return out


def _盯_write_jsonl(path, events):
    """整份重写 + 原子替换。常驻那边要合并尾条，消费者随时可能在读，不能写一半被读到。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _盯_cursor(qdir):
    """消费者读到第几条了。单独一个文件，跟 state.json 分开写，免得两边互相覆盖。"""
    try:
        with open(os.path.join(qdir, "cursor"), "r", encoding="utf-8") as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def _盯_set_cursor(qdir, n):
    with open(os.path.join(qdir, "cursor"), "w", encoding="utf-8") as f:
        f.write(str(n))


def _盯_state(qdir):
    try:
        with open(os.path.join(qdir, "state.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _盯_set_state(qdir, **kw):
    st = _盯_state(qdir)
    st.update(kw)
    tmp = os.path.join(qdir, "state.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(qdir, "state.json"))


def _盯_prune(qdir, events, cursor):
    """清理帧文件。**没取走的一张都不动**；取走的也要放够时间才删。

    早先是"取走即删到只剩 N 张"，那对"全队列读图"是致命的：你刚拿到一批路径，
    文件已经被删掉了。现在按"取走 + 放够 FRAME_KEEP_SECONDS"双条件，外加目录硬上限。
    """
    taken = [ev.get("path") for ev in events[:cursor] if ev.get("path")]
    now = time.time()
    for i, ev in enumerate(events[:cursor]):
        p = ev.get("path")
        if p and now - (ev.get("ts") or 0) >= FRAME_KEEP_SECONDS:
            try:
                os.remove(p)
            except OSError:
                pass
    alive = [p for p in taken if os.path.exists(p)]
    for p in alive[:max(0, len(alive) - FRAME_KEEP_MAX)]:
        try:
            os.remove(p)
        except OSError:
            pass


def _盯_archive(qdir, evs):
    """把一批的中间帧挪进 archive/ 留档。

    "只读结尾那张"是有前提的——**还能回查**。不然一旦"那条消息到底出现过没有"这种问题
    冒出来，就再也答不了了。留档按张数封顶，不让它无限长。
    """
    adir = os.path.join(qdir, "archive")
    try:
        os.makedirs(adir, exist_ok=True)
    except OSError:
        return 0
    n = 0
    for ev in evs:
        p = ev.get("path")
        if p and os.path.exists(p):
            try:
                os.replace(p, os.path.join(adir, os.path.basename(p)))
                n += 1
            except OSError:
                pass
    try:
        files = sorted((os.path.join(adir, f) for f in os.listdir(adir)),
                       key=os.path.getmtime)
        for p in files[:max(0, len(files) - ARCHIVE_KEEP)]:
            try:
                os.remove(p)
            except OSError:
                pass
    except OSError:
        pass
    return n


def _盯_alive(st):
    """常驻进程还活着吗。看心跳而不是 pid：Windows 上探 pid 要么不准要么要额外权限，
    而心跳停摆本身就说明它没了（被外部打断时来不及写 closed）。"""
    b = st.get("beat")
    if not b or st.get("closed"):
        return False
    return (time.time() - b) < max(3.0, WATCH_INTERVAL * 3)


def _box_norm(box_n, reg):
    """指纹里的归一化变化框 → 整屏 0-1000 坐标（可直接拿去 click）。"""
    vx, vy, vw, vh = screen()
    rx0, ry0 = (reg[0], reg[1]) if reg else (vx, vy)
    rw, rh = ((reg[2] - reg[0]), (reg[3] - reg[1])) if reg else (vw, vh)
    return (to_norm(rx0 + box_n[0] * rw, ry0 + box_n[1] * rh)
            + to_norm(rx0 + box_n[2] * rw, ry0 + box_n[3] * rh))


def _盯_render(ev, pending, as_json=False):
    if as_json:
        return [json.dumps(ev, ensure_ascii=False)]
    t = time.strftime("%H:%M:%S", time.localtime(ev.get("ts") or 0))
    kind = ev.get("type", "change")
    if kind == "end":
        head = "盯收尾（%s）· %s · 队里还剩 %d 条" % (ev.get("reason"), t, pending)
    elif kind == "note":
        head = "盯提示（%s）· %s · 队里还剩 %d 条" % (ev.get("reason"), t, pending)
    else:
        head = "画面变了（%s）· 变化量 %.0fpx · 队里还剩 %d 条" % (t, ev.get("phys") or 0, pending)
    out = [head]
    if ev.get("warn"):
        out.append(ev["warn"])
    if ev.get("box"):
        out.append("变化位置 归一化 x %.0f~%.0f  y %.0f~%.0f（可直接拿去 click）"
                   % (ev["box"][0], ev["box"][2], ev["box"][1], ev["box"][3]))
    if ev.get("path"):
        out.append(ev["path"])
    return out


def 盯取(queue="default", wait=0.0, peek=False, as_json=False, take_all=False,
        archive=False):
    """从常驻盯的队列里按顺序取下一帧（FIFO）。

    取不到就最多等 wait 秒（wait=0 立刻返回）。等的这段时间不是空转：常驻那边照盯不误，
    这期间来的每一张都在队里排着，回头一张一张取——画面产出对齐你的思考速度，既不打断
    正在进行的思考，也不漏掉空窗期里的消息。
    """
    qdir = _盯_qdir(queue)
    qfile = os.path.join(qdir, "q.jsonl")
    if not os.path.isdir(qdir):
        raise CuError("CU_NO_QUEUE",
                      "没有名为 %r 的盯队列。先起一个：cu.py 盯 --window 微信"
                      % (queue or "default"))
    end = time.time() + max(0.0, wait)
    while True:
        events = _盯_read_jsonl(qfile)
        cur = _盯_cursor(qdir)
        if cur < len(events):
            if take_all and not peek:
                # 一次收干净。醒来时往往积了好几条（对方连发、界面滚动各算一次），
                # 一条一条取就是十几次工具往返——流水线的意义是"不丢"，不是"必须逐条"。
                bunch = events[cur:]
                kept = 0
                if archive:
                    kept = _盯_archive(qdir, bunch[:-1])   # 中间帧留档，只留结尾那张
                _盯_set_cursor(qdir, len(events))
                _盯_prune(qdir, events, len(events))
                if as_json:
                    return [json.dumps({"events": bunch, "archived": kept},
                                       ensure_ascii=False)]
                t = lambda x: time.strftime("%H:%M:%S", time.localtime(x or 0))
                ends = [b.get("reason") for b in bunch if b.get("type") == "end"]
                span = "%s → %s" % (t(bunch[0].get("ts")), t(bunch[-1].get("ts")))
                if archive:
                    # 社媒场景：老内容本来就在画面上，结尾这张就是"结论性截图"。
                    out = ["收起 %d 条变化（%s）· 中间 %d 帧已留档到 archive/"
                           % (len(bunch), span, kept)]
                    if bunch[-1].get("path"):
                        out.append("结论性一帧（读它就行）：")
                        out.append(bunch[-1]["path"])
                else:
                    # 通用场景：画面不保证累积，**得按顺序把过程看全**。
                    paths = [b["path"] for b in bunch if b.get("path")]
                    out = ["收起 %d 条变化（%s）· 下面 %d 帧按时间顺序，**逐张读**"
                           % (len(bunch), span, len(paths))]
                    out.extend(paths)
                if ends:
                    out.append("期间常驻已收尾（%s）——要接着盯就重新起一个" % ends[-1])
                return out
            ev = events[cur]
            if not peek:
                _盯_set_cursor(qdir, cur + 1)
                _盯_prune(qdir, events, cur + 1)
            return _盯_render(ev, max(0, len(events) - (cur if peek else cur + 1)), as_json)
        st = _盯_state(qdir)
        if time.time() >= end:
            if as_json:
                return [json.dumps({"event": None, "pending": 0,
                                    "daemon_alive": _盯_alive(st),
                                    "closed": bool(st.get("closed"))},
                                   ensure_ascii=False)]
            if st.get("closed"):
                return ["队列空 · 常驻盯已收尾（%s）· 没有待处理画面" % (st.get("reason") or "?")]
            if _盯_alive(st):
                return ["队列空 · 常驻盯还在跑 · 这段窗口里没有新画面"]
            return ["队列空 · 常驻盯似乎已经不在了（心跳停在 %s）"
                    % (time.strftime("%H:%M:%S", time.localtime(st["beat"]))
                       if st.get("beat") else "?")]
        time.sleep(0.25)


def 盯状态(queue="default", as_json=False):
    """看这个队列的积压与常驻进程状态。"""
    qdir = _盯_qdir(queue)
    if not os.path.isdir(qdir):
        raise CuError("CU_NO_QUEUE", "没有名为 %r 的盯队列" % (queue or "default"))
    events = _盯_read_jsonl(os.path.join(qdir, "q.jsonl"))
    cur = _盯_cursor(qdir)
    st = _盯_state(qdir)
    info = {"queue": queue or "default", "produced": len(events), "consumed": cur,
            "pending": max(0, len(events) - cur), "daemon_alive": _盯_alive(st),
            "closed": bool(st.get("closed")), "reason": st.get("reason"),
            "window": st.get("window"), "region": st.get("region"),
            "started": st.get("started"), "deadline": st.get("deadline"),
            "beat": st.get("beat"),
            # 现算而不是读常驻进程的快照：它 3 秒才轮询一次，读快照会显示成"无静默"
            "muted_left": max(0.0, round(_muted_until() - time.time(), 1))}
    if as_json:
        return [json.dumps(info, ensure_ascii=False)]
    fmt = lambda t: time.strftime("%H:%M:%S", time.localtime(t)) if t else "—"
    return ["队列 %s · 已产 %d 条 / 已取 %d 条 / 待取 %d 条"
            % (info["queue"], info["produced"], cur, info["pending"]),
            "常驻：%s%s · 盯 %s · %s → %s（心跳 %s）"
            % ("运行中" if info["daemon_alive"]
               else ("已收尾" if info["closed"] else "似乎已不在"),
               ("（%s）" % info["reason"]) if info["reason"] else "",
               info["window"] or (str(info["region"]) if info["region"] else "?"),
               fmt(info["started"]), fmt(info["deadline"]), fmt(info["beat"])),
            ("静默中 · 还剩 %.1f 秒（这期间的变化只换基准、不排队）" % info["muted_left"])
            if info["muted_left"] else "静默窗：无"]


def 盯(baseline=None, region=None, window=None, timeout=300.0,
      ignore=(), out=None, raise_win=True, start_at=None, box=False,
      follow=False, queue="default", coalesce=0.0):
    """盯住一块画面，等它变；变了就把那一帧交出来，等到时限也把当前这帧交出来。

    baseline: 参考图路径；不给就以启动时的画面为基准。
    start_at: 起算时刻（unix 秒或 HH:MM:SS）；不给就沿用参考图的拍摄时间，
              再没有就是现在。
    region:   盯一块 x,y,w,h（物理像素）。window: 盯一个窗口。
              两个都给时取交集——"盯住这个窗口但把导航栏裁掉"就是这么写。
    timeout:  最多盯多少秒，默认 300（5 分钟）；0 = 不限。
    ignore:   排除区 x0,y0,x1,y1（**整屏** 0-1000 归一化），可重复。
              画面里有会自己闪的东西（时钟、动图头像、进度条）就把它圈掉。
    box:      打开后结果里会给出变化位置，关掉就没有。
    follow:   常驻模式。每次变化把「帧+时间戳」压进队列、立刻把基准换成新帧，自己不退出；
              只有 --timeout 到点或外部打断才结束。配 `收` 按顺序取。
    queue:    队列名，默认 default；同时盯好几处时用它分开。
    coalesce: 常驻下，距上一张还没被取走的帧不足这么多秒，就只把它换成最新的、不新增一条
              （默认 0 = 每张都留）。抖动/动画期想少传几张时打开。
    """
    try:
        from PIL import Image, ImageGrab
    except ImportError:
        raise CuError("CU_NO_PIL", "pip install pillow")

    warn, tgt, win_r = "", None, None
    # 2026-10-27：无 --window 时不会走到下面的赋值分支，后面 2394/2414 行
    # 引用 pw_ok 会抛 UnboundLocalError（凝视一启动即崩）。这里先给默认值。
    pw_ok = False
    if window:
        hits = find_windows(window)
        if not hits:
            raise CuError("CU_NO_WINDOW", "没有标题或进程名含 %r 的可见窗口" % window)
        tgt = hits[0]
        if not hang_check(tgt.hwnd):
            raise CuError("CU_HUNG",
                          "窗口 %r 不响应消息，盯着它没有意义。先 focus-cc 告诉用户。"
                          % tgt.title[:30])
        # 先探一次 PrintWindow：能问窗口要到真画面，就**不必抢前台**。
        # 凝视本该是"安静地看"——为盯一个窗口把用户正在用的窗口顶掉（旧行为）
        # 是过度施力，而且被遮挡就盯不到。拿不到才退回"置前 + 屏幕裁剪"。
        pw_img = None
        try:
            _im, _pw_ok = capture_window_png(tgt.hwnd)
            if _lit_ratio(_im) > 0.01:
                pw_img = _im
        except CuError:
            pw_img = None
        pw_ok = pw_img is not None
        if not pw_ok and raise_win:
            # 盯一个被盖住的窗口毫无意义——看到的是盖在它上面的东西。置前必须和
            # arm 在同一次调用里做：focus 上一次成功、下一次失效是常事。
            _force_foreground(tgt.hwnd)
        if pw_ok:
            # 用 DWM 可见边界，和 capture_window_png 的裁剪口径一致，
            # 这样后面的 ignore/region 坐标原点不会错位。
            vb = _visible_frame_bounds(tgt.hwnd)
            win_r = _region_arg("%d,%d,%d,%d" % (vb or window_rect(tgt.hwnd)),
                                "--window 的窗口矩形")
        else:
            win_r = _region_arg("%d,%d,%d,%d" % window_rect(tgt.hwnd),
                                "--window 的窗口矩形")
        cover, total = _occlusion(win_r, tgt.pid)
        if total and cover / float(total) > 0.5:
            warn = "⚠ 这个窗口现在被别的窗口盖着，盯到的不是它本身。先让它露出来。"

    reg = win_r
    if region is not None:
        r2 = _region_arg(region)
        reg = _intersect(r2, win_r) if win_r else r2

    ign = []
    for spec in ignore:
        try:
            vals = tuple(float(v) for v in str(spec).split(","))
        except ValueError:
            raise CuError("CU_BAD_ARG", "--ignore 要写 x0,y0,x1,y1 四个数，收到 %r" % spec)
        if len(vals) != 4:
            raise CuError("CU_BAD_ARG", "--ignore 要写四个数 x0,y0,x1,y1，收到 %r" % spec)
        for i, v in enumerate(vals):
            _unit(v, "--ignore 第 %d 个数" % (i + 1))   # 越界硬报错，不再静默夹成空区间
        ign.append(vals)
    local = _ignore_local(ign, reg)

    # 盯的区域压到 DSH 界面自己的窗口 = 必然假触发：它一边等一边输出，界面一直在重绘。
    # 不给 --region 时 reg 是 None（等于全屏），这里必须一并算上——全屏监看恰恰是最
    # 容易踩这个坑的用法，而它偏偏是默认值。
    ccr, cct, cch = _host_window_rect()
    vx, vy, vw, vh = screen()
    rs = reg if reg else (vx, vy, vx + vw, vy + vh)
    if ccr and not pw_ok:
        ox = min(rs[2], ccr[0] + ccr[2]) - max(rs[0], ccr[0])
        oy = min(rs[3], ccr[1] + ccr[3]) - max(rs[1], ccr[1])
        if ox > 0 and oy > 0:
            frac = (ox * oy) / float((rs[2] - rs[0]) * (rs[3] - rs[1]))
            # 矩形重叠只说明"面积上压着"，不说明"视觉上盖着"：终端最大化但在后台是常态。
            # 真去问一下那一点上最顶层的是谁，是它才报。
            cx = (max(rs[0], ccr[0]) + min(rs[2], ccr[0] + ccr[2])) // 2
            cy = (max(rs[1], ccr[1]) + min(rs[3], ccr[1] + ccr[3])) // 2
            really = (cch is not None and _top_root_at(cx, cy) == cch)
            if frac > 0.05 and really:
                w2 = ("⚠ 盯的画面里有 %.0f%% 是 DSH 界面自己的窗口——它一边等一边输出，界面"
                      "一直在重绘，会把你唤醒成假警报。改用 --window 盯目标窗口，或用 "
                      "--region 把终端排除掉。" % (frac * 100))
                warn = (warn + " " + w2) if warn else w2

    def grab():
        # 给了 --window 且 PrintWindow 可用 → 直接问窗口要图：**被遮挡也盯得到**，
        # 而且不依赖前台。--region 叠加时按屏幕坐标把它从窗口图里裁出来，
        # 保证返回图的坐标系仍然等于 reg（后面的 ignore 与指纹都按 reg 算）。
        if pw_ok and tgt is not None:
            try:
                im2, _ok2 = capture_window_png(tgt.hwnd)
                if _lit_ratio(im2) > 0.01:
                    if reg and win_r and tuple(reg) != tuple(win_r):
                        box = (reg[0] - win_r[0], reg[1] - win_r[1],
                               reg[2] - win_r[0], reg[3] - win_r[1])
                        im2 = im2.crop(box)
                    return im2
            except CuError:
                pass
        return ImageGrab.grab(bbox=reg, all_screens=True) if reg \
            else ImageGrab.grab(all_screens=True)

    def fp_of(im):
        return _fingerprint(im, local)

    if baseline:
        try:
            bimg = Image.open(baseline)
            bimg.load()
        except Exception as e:
            raise CuError("CU_BAD_ARG", "读不了参考图 %s：%s" % (baseline, e))
        mt = os.path.getmtime(baseline)
        base_ts = _parse_ts(start_at, mt) if start_at is not None else mt
        bw, bh, bfp = fp_of(bimg)
    else:
        # 稳定基线：连 STABLE_FRAMES 帧完全一致才认。刚 arm 时画面常常还在"落定"
        # ——选中高亮消退、动画收尾——那类一次性变化会让基线永远对不上，
        # 之后每次比对都"有变化"，变成必然的假警报（实测踩过）。
        prev_fp, prev_dim, bimg, same = None, None, None, 0
        t_s = time.time()
        while True:
            cur = grab()
            w_, h_, f_ = fp_of(cur)
            if prev_fp is not None and (w_, h_) == prev_dim \
                    and _diff(prev_fp, f_, w_, h_)[0] == 0.0:
                same += 1
            else:
                same = 0
            prev_fp, prev_dim, bimg = f_, (w_, h_), cur
            if same >= STABLE_FRAMES - 1 or time.time() - t_s > 4.0:
                break
            time.sleep(0.3)
        base_ts = _parse_ts(start_at, time.time()) if start_at is not None else time.time()
        bw, bh, bfp = prev_dim[0], prev_dim[1], prev_fp

    t0 = time.time()
    qdir = _盯_qdir(queue) if follow else None
    qfile = os.path.join(qdir, "q.jsonl") if follow else None
    events, seq = [], 0
    if follow:
        os.makedirs(os.path.join(qdir, "frames"), exist_ok=True)
        _盯_write_jsonl(qfile, events)
        _盯_set_cursor(qdir, 0)
        _盯_set_state(qdir, pid=os.getpid(), queue=queue or "default", follow=True,
                      started=t0, deadline=(t0 + timeout) if timeout else None,
                      window=(tgt.title if tgt else None),
                      region=(list(reg) if reg else None),
                      produced=0, closed=False, reason=None, beat=time.time())
        # 后台跑的时候 stdout 要等它退出才有人看，所以这句直接 print + flush：
        # 想确认"到底盯上没有"时，读后台任务的输出文件就能看到。
        print("盯已常驻 · 队列 %s · 盯 %s · 时限 %s（到点自动收尾；用 `收` 取帧）"
              % (queue or "default",
                 ("「%s」" % tgt.title[:20]) if tgt else ("区域 %s" % (reg,)),
                 _fmt_dur(timeout) if timeout else "不限"), flush=True)
    hit, phys, box_n, img = False, 0.0, None, bimg
    while True:
        time.sleep(WATCH_INTERVAL)
        img = grab()
        moved = False
        if win_r is not None:
            # 窗口挪了或改了大小，盯的就不是原来那块了，基准作废。不查的话整体位移
            # 会被当成"画面变了"，白白唤醒一次。
            now_r = _region_arg("%d,%d,%d,%d" % window_rect(tgt.hwnd), "窗口")
            if now_r != win_r:
                if not follow:
                    raise CuError("CU_MOVED",
                                  "盯的过程中窗口 %r 的位置或大小变了，之前的基准作废。"
                                  "重新盯一次。" % tgt.title[:24])
                # 常驻：不复位。记一条提示继续盯——终止权只在调用方和 --timeout 手上。
                win_r, moved = now_r, True
        fw, fh, fp = fp_of(img)
        if (fw, fh) != (bw, bh):
            hit, phys, box_n = True, 0.0, None      # 尺寸变了，基准作废
        else:
            frac, box_n = _diff(bfp, fp, fw, fh)
            k = max(1.0, img.size[0] / float(FP_WIDTH))
            phys = frac * (fw * fh) * k * k
            hit = phys > MIN_CHANGED_PX

        if not follow:
            if hit or (timeout and time.time() - t0 >= timeout):
                break
            continue

        # ---- 常驻：压队列、立刻换基准、继续盯 ----
        now = time.time()
        muted = now < _muted_until()   # 静默期：你自己操作引起的重绘不算"画面变了"
        if muted:
            pass
        elif moved:
            seq += 1
            events.append({"seq": seq, "type": "note", "reason": "window_moved",
                           "ts": now, "warn": warn.strip()})
            _盯_write_jsonl(qfile, events)
        elif hit:
            fpath = os.path.join(qdir, "frames", "e%05d_%d.png" % (seq + 1, int(now * 1000)))
            try:
                img.save(fpath)
            except Exception:
                fpath = None
            cur = _盯_cursor(qdir)
            near = (coalesce > 0 and events and len(events) > cur
                    and events[-1].get("type") == "change"
                    and now - (events[-1].get("ts") or 0) < coalesce)
            if near:            # 抖动期：只把还没被取走的上一张换成最新的，不新增一条
                old = events[-1].get("path")
                if old and old != fpath:
                    try:
                        os.remove(old)
                    except OSError:
                        pass
                events[-1] = {"seq": events[-1]["seq"], "type": "change", "ts": now,
                              "path": fpath, "phys": phys, "warn": warn.strip(),
                              "box": _box_norm(box_n, reg) if (box and box_n) else None}
            else:
                seq += 1
                events.append({"seq": seq, "type": "change", "ts": now, "path": fpath,
                               "phys": phys, "warn": warn.strip(),
                               "box": _box_norm(box_n, reg) if (box and box_n) else None})
            _盯_write_jsonl(qfile, events)
            _盯_prune(qdir, events, _盯_cursor(qdir))
        bfp, bw, bh = fp, fw, fh        # 基准立刻换成刚看到的这一帧
        _盯_set_state(qdir, produced=len(events), beat=now, muted=muted,
                      muted_left=(round(_muted_until() - now, 1) if muted else 0.0))
        if timeout and time.time() - t0 >= timeout:
            seq += 1
            events.append({"seq": seq, "type": "end", "reason": "timeout", "ts": time.time()})
            _盯_write_jsonl(qfile, events)
            left = max(0, len(events) - _盯_cursor(qdir))
            _盯_set_state(qdir, produced=len(events), closed=True, reason="timeout",
                          beat=time.time())
            return {"follow": True,
                    "head": "盯已收尾（等满 %s）· 队列 %s 还剩 %d 条没取"
                            % (_fmt_dur(time.time() - t0), queue or "default", left)}

    end_ts = time.time()
    if tgt is not None:
        c2, t2 = _occlusion(win_r, tgt.pid)
        if t2 and c2 / float(t2) > 0.5:
            late = "⚠ 这个窗口现在被别的窗口盖着，这次的画面未必来自它本身，先看清楚再动手。"
            warn = (warn + " " + late).strip()

    os.makedirs(SHOT_DIR, exist_ok=True)
    path = out or os.path.join(SHOT_DIR, "cu_watch_%d.png" % int(end_ts * 1000))
    try:
        img.save(path)
    except Exception as e:
        raise CuError("CU_SHOT_FAIL", "写盘失败 %s：%s" % (path, e))
    _prune_shots()      # 和 shot 一样要自清理，否则盯久了会把临时目录堆满

    where = _box_norm(box_n, reg) if (box and box_n) else None
    return {"path": path, "reason": "changed" if hit else "timeout",
            "start_ts": base_ts, "end_ts": end_ts, "phys": phys,
            "target": tgt.title if tgt else None,
            "box": where, "warn": warn.strip()}


watch = 盯      # 旧名保留：已有脚本/文档里的 watch 调用不至于断


def 码(as_json=False):
    """错误码表。失败时 stderr 首行形如 [CU_XXX]，对着这张表决定下一步，不用去读源码。"""
    if as_json:
        return [json.dumps(CODES, ensure_ascii=False, indent=2)]
    out = ["退出码 0 成功 / 2 失败；失败时 stderr 首行形如 [CU_XXX] 说明。"]
    for k in sorted(CODES):
        out.append("  %-17s %s" % (k, CODES[k]))
    return out


def 口径():
    """把"用的时候该知道、但不必读源码"的口径一次说清。

    这是 ins/outs，不是实现细节：黑盒不靠藏，靠让输出自证。
    """
    lines = [
        "【坐标三套，别混】",
        "  click / move / hover / scroll / drag 的坐标：整屏 0-1000（0,0 左上，1000,1000 右下）",
        "  --region：物理像素 x,y,w,h —— 全脚本唯一吃像素的参数",
        "  post --at：那个窗口内 0-1000",
        "",
        "【盯的判据：不是百分比，是绝对像素预算】",
        "  单像素按灰度差判，超过 %d 才算「变了」" % DIFF_LEVEL,
        "  先把盯的区域等比缩到宽 %d 再比（这一步本身就吃掉锯齿噪声）" % FP_WIDTH,
        "  触发线：变化像素换算回物理像素后 > %d" % MIN_CHANGED_PX,
        "  换算：phys = 指纹里变化占比 x 指纹像素数 x (区域宽/%d)^2" % FP_WIDTH,
        "  于是同一个预算下，框越宽越难触发（百分比阈值反而会让窄框过敏，故不用）",
        "  实测参考：777x1123 的窗口约 0.34%；777x620 约 0.62%",
        "  已知噪声量级：任务栏时钟 ~300px、光标 ~30px、26x26 红点 676px、"
        "一条消息气泡 ~18000px",
        "  比对间隔 %.1fs；取基线前要求连续 %d 帧完全一致（躲开高亮消退那类落定期）"
        % (WATCH_INTERVAL, STABLE_FRAMES),
        "",
        "【静默窗】注入类命令（click/type/paste/post/key/...）自动把静默窗推到 %g 秒后；"
        % MUTE_SECONDS,
        "  静默期内常驻盯只换基准、不压队列，所以你自己操作引起的重绘不会变成假唤醒。",
        "  代价：这段里真来的消息也会被一起吞掉 —— 所以操作收尾要自己核验一次。",
        "  手动改：cu.py 静默 [秒]（0 = 立即清除）",
        "",
        "【队列落盘】%s/<队列名>/ ：q.jsonl 事件、cursor 读到第几条、"
        "state.json 心跳、frames/ 帧图" % WATCHQ_ROOT,
        "",
        "错误码表：cu.py 码",
    ]
    return lines


def 静默(seconds=None):
    if seconds is None:
        mu = _muted_until()
        left = mu - time.time()
        if left > 0:
            return ["静默中 · 还剩 %.1f 秒（%s 解除）"
                    % (left, time.strftime("%H:%M:%S", time.localtime(mu)))]
        return ["当前没有静默窗（注入类命令会自动设 %g 秒）" % MUTE_SECONDS]
    if seconds <= 0:
        _unmute()
        return ["静默已清除"]
    _mute(seconds)
    return ["静默窗设为 %.1f 秒：这期间常驻盯只换基准、不压队列" % seconds]


def _ink(img):
    """一块区域里"有多少墨"：以该区域出现最多的灰度为底，数明显偏离它的像素占比。
    判断"东西落进输入框没有""发完清空没有"用它，跟具体是哪个软件无关。"""
    g = img.convert("L")
    hist = g.histogram()
    bg = max(range(256), key=lambda v: hist[v])
    n = 0
    for v in range(256):
        if abs(v - bg) > 40:
            n += hist[v]
    return n / float(g.size[0] * g.size[1])


def post(window, text=None, image=None, at=None, enter=True, out=None, keep=False):
    """把一段文本或一张图送进某个窗口的输入框，可选回车——**一次调用干完一整轮**。

    平时发一条消息要 4 次工具往返（截图 → 点输入框 → 输入 → 再截图验证）。这里把
    "定位窗口、点输入框、粘贴、回车、本地验证"全塞进一个进程，只回一行结论。
    at: 输入框位置，**窗口内** 0-1000 归一化；不给就按窗口底部中间算。
    """
    if not text and not image:
        raise CuError("CU_BAD_ARG", "要么给文本，要么给 --image")
    if text and image:
        raise CuError("CU_BAD_ARG", "text 和 --image 只能给一个")
    try:
        from PIL import ImageGrab
    except ImportError:
        raise CuError("CU_NO_PIL", "pip install pillow")

    hits = find_windows(window)
    if not hits:
        raise CuError("CU_NO_WINDOW", "没有标题或进程名含 %r 的可见窗口" % window)
    t = hits[0]
    ok, level = injectable(t.pid)
    if not ok:
        raise CuError("CU_UIPI", "窗口 %r 完整性级别 %s 高于本进程，注入会被静默丢弃"
                      % (t.title[:30], level))
    if not hang_check(t.hwnd):
        raise CuError("CU_HUNG", "窗口 %r 不响应消息，别再往里发东西。" % t.title[:30])
    _force_foreground(t.hwnd)
    wx, wy, ww, wh = window_rect(t.hwnd)
    if ww < 120 or wh < 120:
        raise CuError("CU_BAD_ARG", "窗口只有 %dx%d，输入框定位不可靠" % (ww, wh))

    if at is None:
        ax, ay = 500.0, 935.0
    else:
        try:
            ax, ay = (float(v) for v in str(at).split(","))
        except ValueError:
            raise CuError("CU_BAD_ARG", "--at 要写 x,y（窗口内 0-1000），收到 %r" % at)
        _unit(ax, "--at 的 x")
        _unit(ay, "--at 的 y")

    # 输入框那一条：窗口底部 12%。用它判断"东西落进去了没""发完清空了没"。
    band = (wx, wy + int(wh * 0.88), wx + ww, wy + wh)

    def ink():
        return _ink(ImageGrab.grab(bbox=band, all_screens=True))

    click(*to_norm(int(round(wx + ax * ww / 1000.0)),
                   int(round(wy + ay * wh / 1000.0))))
    time.sleep(0.25)
    ink0 = ink()                    # 打字之前（输入框里可能本来就有草稿）

    old_clip = clip_get_soft()
    if image:
        clip_set_image(image)
    else:
        clip_set(text)
    key("ctrl+v")
    time.sleep(0.45)
    ink1 = ink()                    # 粘贴之后应当明显"更黑"

    if ink1 - ink0 < 0.004:
        # 粘进去的东西在输入框里看不见 = 多半根本没点中。这一步拦住最划算：
        # 一旦回车，发出去就撤不回了。
        verdict = "存疑：没看到内容进输入框（可能没点中，或这个窗口不接受粘贴）"
    elif not enter:
        verdict = "已输入（用了 --no-enter，没有回车）"
    else:
        key("enter")
        time.sleep(0.6)
        if ink() <= ink0 + 0.003:
            verdict = "已发送"
        else:
            verdict = "存疑：回车之后输入框里还有内容，可能没发出去"

    note = ("（按 --keep 未还原剪贴板）" if keep
            else _restore_clip(old_clip, False).strip())
    # 只在存疑（或明确要文件）时才把截图交出去：确认发出去了就不必再花一次
    # 读图的钱和一次往返——这正是这个动词存在的意义。
    path = None
    if verdict.startswith("存疑") or out:
        path = shot(out, 1280, None, False)[0]
    return verdict, path, note, t.title


def _fmt_dur(sec):
    sec = int(sec)
    if sec < 60:
        return "%d 秒" % sec
    if sec < 3600:
        return "%d 分 %d 秒" % (sec // 60, sec % 60)
    return "%d 时 %d 分" % (sec // 3600, (sec % 3600) // 60)


# ---------------------------------------------------------------- CLI
def _p_shot(sub):
    p = sub.add_parser("shot", help="截图，然后用 read 打开它")
    p.add_argument("--out")
    p.add_argument("--long", type=int, default=None,
                   help="长边缩到 N。全屏默认 1280；只要布局可给 800（省一半图像 token）")
    p.add_argument("--region", help="x,y,w,h 只截一块（物理像素；长边≤1568 时 1:1）")
    p.add_argument("--window", help="只截某个窗口（每次调用现读它的位置，窗口挪了也对）")
    p.add_argument("--full", action="store_true", help="不缩放，出原分辨率")
    p.add_argument("--keep-all", action="store_true", help="不清理旧截图")
    p.add_argument("--emit-binary", action="store_true", dest="emit_binary",
                   help="（内部用）不落盘：先输出一行 JSON meta，再输出原始 PNG 字节。"
                        "给常驻服务与 --rpc 降级路径用；人不要手敲")


def _p_point(sub, name, help_, extra=()):
    p = sub.add_parser(name, help=help_)
    p.add_argument("x", type=float, help="归一化 0-1000")
    p.add_argument("y", type=float, help="归一化 0-1000")
    for a, kw in extra:
        p.add_argument(a, **kw)
    return p


def build_parser():
    ap = argparse.ArgumentParser(prog="cu.py", description="看屏幕、动鼠标、敲键盘、开应用")
    sub = ap.add_subparsers(dest="cmd", required=True)

    _p_shot(sub)

    _p_point(sub, "click", "点击（坐标 0-1000）",
             extra=(("--right", dict(action="store_true")),
                    ("--double", dict(action="store_true")),
                    ("--triple", dict(action="store_true"))))
    _p_point(sub, "move", "移动光标")
    _p_point(sub, "hover", "同 move", extra=())
    _p_point(sub, "move-smooth", "平滑移动光标（对齐豆包 mouse.move_smooth）",
             extra=(("--ms", dict(type=int, default=300,
                                  help="总时长毫秒 0..5000，默认 300")),))
    _p_point(sub, "scroll", "滚轮（正数向上）",
             extra=(("n", dict(type=str, help="整数 -10..10 或 up/down/left/right")),))

    p = sub.add_parser("drag", help="拖拽（默认 800ms，不是瞬移）")
    for a in ("x1", "y1", "x2", "y2"):
        p.add_argument(a, type=float)
    p.add_argument("--ms", type=int, default=800, help="拖拽总时长，50..10000 毫秒")

    p = sub.add_parser("type", help="输入文字（短文本；长文本用 paste）")
    p.add_argument("text")

    p = sub.add_parser("key", help="按键，如 ctrl+s / enter / alt+tab")
    p.add_argument("combo")

    # 长按修饰键：Ctrl+拖拽、Shift+点选多行这类操作用。
    p = sub.add_parser("key-down", help="按住修饰键（必须与 key-up 在同一次 batch 里）")
    p.add_argument("name", help="ctrl / alt / shift / win")
    p = sub.add_parser("key-up", help="松开修饰键")
    p.add_argument("name")
    sub.add_parser("release", help="把所有修饰键都松开（怀疑 latch 住了就调它）")

    p = sub.add_parser("paste", help="剪贴板粘贴（长文本走这条路）")
    p.add_argument("text")
    p.add_argument("--keep", action="store_true", help="粘完不还原原剪贴板内容")

    p = sub.add_parser("copy", help="Ctrl+C 并返回选中内容（默认还原剪贴板）")
    p.add_argument("--keep", action="store_true", help="不还原剪贴板")
    p.add_argument("--out", help="内容写到文件，避免长文本被截断")
    p = sub.add_parser("clip", help="读剪贴板")
    p.add_argument("--out", help="内容写到文件，避免长文本被截断")
    p = sub.add_parser("clip-set", help="写剪贴板（文本）")
    p.add_argument("text")
    p = sub.add_parser("clip-image", help="把 PNG 放到剪贴板（CF_DIB，微信/QQ/Word 认）")
    p.add_argument("path")
    p = sub.add_parser("paste-image", help="贴图到当前窗口（不自动回车，发送由你决定）")
    p.add_argument("path")
    p = sub.add_parser("save-image", help="把剪贴板里的图存成 PNG")
    p.add_argument("path")

    sub.add_parser("where", help="光标在哪（同时给归一化值）")

    p = sub.add_parser("wait", help="等一会儿（batch 里唯一能暂停的办法）")
    p.add_argument("seconds", type=float, nargs="?", default=1.0)

    p = sub.add_parser("windows", help="列出可见窗口 + 能否注入")
    p.add_argument("pattern", nargs="?", help="只看标题或进程名含此串的")
    p.add_argument("--all", action="store_true", dest="show_hidden",
                   help="连**隐藏**窗口一起列（被收进托盘的会标出来）——"
                        "找不到某个应用时先用它确认是不是还活着，别急着重启")

    p = sub.add_parser("rect", help="窗口在屏幕上的位置（算 --region 用）")
    p.add_argument("pattern")

    p = sub.add_parser("focus", help="精确把窗口切到前台")
    p.add_argument("pattern", help="标题子串 或 hwnd")

    p = sub.add_parser("focus-cc", help="把 DSH 界面切到前台（挂机时给用户看）")
    p.add_argument("pattern", nargs="?", help="窗口标题子串，默认自动找终端")

    p = sub.add_parser("apps", help="列应用目录（Get-StartApps）")
    p.add_argument("pattern", nargs="?", help="关键词")

    p = sub.add_parser("find", help="在 UIA 树里按名字找控件（不截图，快路）")
    p.add_argument("pattern", help="控件名字子串；--regex 时按正则")
    p.add_argument("--window", help="限定在标题含此串的窗口内；默认当前前台窗口")
    p.add_argument("--role", help="只要这个角色，如 Button / Edit / Text")
    p.add_argument("--regex", action="store_true", help="pattern 按正则匹配（默认子串）")
    p.add_argument("--exact", action="store_true", help="名字必须完全相等")
    p.add_argument("--index", type=int, help="只返回第 N 个候选（0 起）")
    p.add_argument("--click", action="store_true", help="直接点第一个匹配")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true", dest="as_json")

    # ---- app.*：对齐豆包 app.is_running / app.resize / app.terminate ----
    p = sub.add_parser("app-is-running", help="这个应用现在有没有在跑（窗口 + 进程两条证据）")
    p.add_argument("query", help="应用名或 AppID")
    p.add_argument("--json", action="store_true", dest="as_json")

    p = sub.add_parser("app-resize", help="把窗口改成指定大小（不动位置、不抢焦点）")
    p.add_argument("query", help="窗口标题或进程名子串")
    p.add_argument("width", type=int)
    p.add_argument("height", type=int)

    p = sub.add_parser("app-terminate", help="关掉应用（破坏性：必须 --confirm）")
    p.add_argument("query", help="窗口标题或进程名子串")
    p.add_argument("--confirm", action="store_true",
                   help="确认执行；不加只会返回 CU_CONFIRM_REQUIRED")

    sub.add_parser("hybrid-programs",
                   help="哪些可见窗口其实是浏览器 + 有没有可用 CDP 端点（用来选平面）")

    p = sub.add_parser("spawn", help="直接起一个可执行文件（不经 shell，参数原样传）")
    p.add_argument("exe", help="可执行文件路径，或 PATH 里的名字")
    p.add_argument("args", nargs=argparse.REMAINDER, help="原样传给该程序的参数")
    p.add_argument("--cwd", help="工作目录")

    # ---- 元素级（UIA）：对齐豆包 state.capture / visual_tree.* / app.* 那一族 ----
    p = sub.add_parser("state-capture",
                       help="UIA 树快照：给每个可交互元素一个 ref，之后用 element 按 ref 操作")
    p.add_argument("--window", help="限定在标题含此串的窗口内；默认当前前台窗口")
    p.add_argument("--depth", type=int, default=12, help="最大递归深度，默认 12")
    p.add_argument("--limit", type=int, default=400, help="最多返回多少个元素，默认 400")
    p.add_argument("--json", action="store_true", dest="as_json")

    p = sub.add_parser("element",
                       help="按 state-capture 给出的 ref 操作元素（优先走 UIA pattern，失败回退点击）")
    p.add_argument("ref", help="形如 e12，或上一次 state-capture 里的完整 ref")
    p.add_argument("--click", action="store_true", help="点它（不看 pattern，只点中心）")
    p.add_argument("--invoke", action="store_true", help="走 InvokePattern")
    p.add_argument("--set-value", dest="set_value", help="走 ValuePattern 写值")
    p.add_argument("--set-focus", action="store_true", dest="set_focus")
    p.add_argument("--toggle", action="store_true", help="走 TogglePattern（幂等：先读状态）")
    p.add_argument("--select", action="store_true", help="走 SelectionItemPattern")
    p.add_argument("--add-to-selection", action="store_true", dest="add_to_selection")
    p.add_argument("--expand", action="store_true")
    p.add_argument("--collapse", action="store_true")
    p.add_argument("--scroll-into-view", action="store_true", dest="scroll_into_view")
    p.add_argument("--set-range-value", dest="set_range_value", help="走 RangeValuePattern")
    p.add_argument("--window-close", action="store_true", dest="window_close")
    p.add_argument("--get", help="只读某个属性：name/value/class/rect/enabled/focused/offscreen/"
                                 "password/accelerator/pattern")

    p = sub.add_parser("element-at", help="这个屏幕点上是什么元素（物理像素）")
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p.add_argument("--json", action="store_true", dest="as_json")

    sub.add_parser("resync", help="丢掉元素缓存（元素层行为异常时调它重来）")

    # 2026-10-27：Windows 虚拟桌面相关子命令整体移除（功能重做中）。
    # 原 desktop-list / desktop-current / desktop-switch / desktop-create /
    # desktop-remove 不再提供。

    p = sub.add_parser("launch", help="按应用名启动，等稳定窗口出现并置前")
    p.add_argument("name")
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--no-focus", action="store_true", help="启动后不置前")

    p = sub.add_parser("batch", help="从 stdin 读多行命令，一次执行")
    p.add_argument("--continue-on-error", action="store_true")

    def _盯_args(p):
        p.add_argument("--baseline", help="（只在 --once 下用）参考图路径")
        p.add_argument("--start-at", help="（只在 --once 下用）起算时刻，unix 秒或 HH:MM:SS")
        p.add_argument("--region", help="盯 x,y,w,h 一块（物理像素）；和 --window 同时给就取交集")
        p.add_argument("--window", help="盯标题或进程名含此串的窗口（arm 时自动置前）")
        p.add_argument("--no-raise", action="store_true",
                       help="用 --window 时不要把那个窗口置前")
        p.add_argument("--timeout", type=float, default=1800.0,
                       help="最多盯多少秒（默认 1800 = 30 分钟）；0 = 不限")
        p.add_argument("--ignore", action="append", default=[],
                       help="排除区 x0,y0,x1,y1（整屏 0-1000 归一化），可重复；"
                            "画面里有会自己闪的东西就圈掉它")
        p.add_argument("--box", action="store_true",
                       help="结果里带上变化位置（可直接拿去 click）")
        p.add_argument("--out", help="（只在 --once 下用）这一帧的落盘路径")
        # 默认就是常驻——**让直觉最短的那条路 = 安全的那条路**。曾经反过来（默认一次性、
        # 常驻要加 --follow），结果凭直觉敲 `盯 --window X` 正好踩中"退出后留空窗"那个坑。
        # 一次性降级成 --once，它是"等一个已知的、马上会来的信号"用的。
        p.add_argument("--once", action="store_true",
                       help="一次性：变了就交出那一帧并退出（默认不这样，默认是常驻排队）")
        p.add_argument("--queue", default="default",
                       help="队列名（默认 default）；同时盯好几处时用它分开")
        p.add_argument("--coalesce", type=float, default=0.0,
                       help="常驻下距上一张还没被取走的帧不足这么多秒，就只更新它不新增"
                            "（默认 0 = 每张都留）；动画抖动期想少传几张时打开")

    p = sub.add_parser("盯", help="盯住一块画面（默认常驻排队）；取帧用 `收`，看积压用 `瞧`")
    _盯_args(p)
    p = sub.add_parser("watch", help="盯（别名，参数与 盯 完全相同）")
    _盯_args(p)

    p = sub.add_parser("post", help="把文本/图片送进某个窗口的输入框，一次调用干完一整轮（含本地验证）")
    p.add_argument("window", help="窗口标题或进程名子串")
    p.add_argument("text", nargs="?", help="要发的文本")
    p.add_argument("--image", help="改发这张图（png/jpg），此时不要给 text")
    p.add_argument("--at", help="输入框位置 x,y（窗口内 0-1000）；默认底部中间")
    p.add_argument("--no-enter", action="store_true", help="只输入不回车，留给你先看一眼")
    p.add_argument("--keep", action="store_true", help="不还原剪贴板")
    p.add_argument("--out", help="存疑时那张截图的落盘位置")

    p = sub.add_parser("收", help="取一帧（配 盯 用）；队列空就等 --wait 秒")
    p.add_argument("--wait", type=float, default=0.0,
                   help="队列空时最多等多少秒（默认 0）。**后台放一个 `收 --wait 1800` 就是"
                        "叫醒机制**：它一退出就说明有事，而 盯 那边不会退出、也不丢事件")
    p.add_argument("--all", action="store_true", dest="all",
                   help="一次把积压的变化全收掉，并把这一批的帧路径**按顺序全列出来**"
                        "（通用场景要逐张读，看清变化全过程）")
    p.add_argument("--archive", action="store_true",
                   help="配合 --all：只交出结尾那张结论性截图，中间帧挪进 archive/ 留档。"
                        "**只在「老内容会留在画面上」的累积型场景用**（聊天记录就是），"
                        "通用场景别用——那会漏掉过程")
    p.add_argument("--peek", action="store_true", help="只看，不取走")
    p.add_argument("--queue", default="default", help="队列名（默认 default）")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.set_defaults(next=True)

    p = sub.add_parser("瞧", help="看队列积压与常驻状态")
    p.add_argument("--queue", default="default", help="队列名（默认 default）")
    p.add_argument("--json", action="store_true", dest="as_json")
    p.set_defaults(status=True)

    p = sub.add_parser("静默", help="手动设/清静默窗（注入类命令会自动设，平时不用管）")
    p.add_argument("seconds", nargs="?", type=float, default=None,
                   help="静默多少秒；0 = 立即清除；不给 = 看当前状态")

    sub.add_parser("码", help="错误码表：失败时 stderr 的 [CU_XXX] 都是什么意思")
    sub.add_parser("口径", help="一次说清：坐标三套、盯的判据、噪声量级、静默窗、队列位置")

    sub.add_parser("probe", help="自检：屏幕、DPI、Pillow、剪贴板、注入权限")
    p = sub.add_parser("doctor", help="全项环境自检：解释器/依赖/DPI/注入/剪贴板/审计/"
                                      "元素层/虚拟桌面/浏览器 CDP，每项都给修复命令")
    p.add_argument("--json", action="store_true", dest="as_json")
    p = sub.add_parser("setup", help="依赖安装引导：默认只打印命令，--install 才真的装")
    p.add_argument("--install", action="store_true")
    p.add_argument("--json", action="store_true", dest="as_json")
    return ap


def clip_get_soft():
    """读剪贴板，读不到就返回 None。
    paste/copy 的"还原"只是附加价值，不该因为剪贴板被别的进程短暂占用
    就把"粘贴/复制"这个主功能也拖死。"""
    try:
        return clip_get()
    except CuError:
        return None


def _restore_clip(old, keep):
    """把剪贴板还原成 old，返回给模型看的尾注。
    old 为 None = 原来没有文本内容（空剪贴板或非文本），此时**不能谎报"已还原"**。"""
    if keep:
        return "\n（按 --keep 未还原剪贴板）"
    if old is None:
        return "\n（原来剪贴板里没有文本，未还原）"
    try:
        time.sleep(0.08)
        clip_set(old)
        return "\n（剪贴板已还原成原来的内容）"
    except CuError:
        return "\n（剪贴板被占用，未能还原）"


def _dump_text(v, out, label):
    """文本回显：默认只给前 2000 字（护上下文），要全量就 --out 落盘。
    没有出口的截断是危险的——模型会拿半张表当全表推理，而且信号是"字数"却看不到内容。"""
    if v is None:
        raise CuError("CU_NO_CLIPBOARD", "剪贴板里没有文本")
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(v)
        return "%s共 %d 字，已写入 %s（要看全文就 read 它）" % (label, len(v), out)
    head = v[:2000]
    more = "" if len(v) <= 2000 else \
        "\n…（还有 %d 字没显示；要全量就加 --out <路径>）" % (len(v) - 2000)
    return "%s（%d 字）：\n%s%s" % (label, len(v), head, more)


def _shot_le(a):
    """`--long` / `--full` / 有没有 `--region` 合起来的缩放决策**只有这一处**。

    早先 `capture_shot` 和 `resolve_shot_region` 各算一份，结果已经分叉：
    窗口截图走 PrintWindow 时用的是本地那份，**绕过了 `--long <= 0` 的校验**，
    `--long -1` 会静默产出 1x1 的图（`_encode_png_img` 里 `max(1, int(w*k))`）。
    同一命令因为后端不同给不同大小的图，而且产出的还是"合法 PNG"——最难查的那类。
    """
    if getattr(a, "full", False):
        return None
    if getattr(a, "long", None) is not None:
        if a.long <= 0:
            # --long 0 会放大成"全屏原图"（最贵那条路），--long -1 出 1x1 废图，
            # 两者都静默返回"成功"，模型拿到的是坏数据还以为没事。
            raise CuError("CU_BAD_ARG",
                          "--long 必须是正数，收到 %d（要原分辨率用 --full）" % a.long)
        return a.long
    # 区域截图默认保持 1:1（看小字），只在超过下游模型的 1568px 长边上限时才缩；
    # 再大也会被下游降采样，白占文件体积。
    return 1568 if getattr(a, "region", None) else 1280


def resolve_shot_region(a):
    """把 shot 的参数（--window / --region / --long / --full）解析成
    (region_物理像素或None, win_note, long_edge)。run() 与 --emit-binary 共用同一份逻辑，
    免得两条路各算一遍、慢慢长歪。"""
    region = None
    win_note = ""
    if getattr(a, "window", None):
        hits = find_windows(a.window)
        if not hits:
            raise CuError("CU_NO_WINDOW",
                          "没有标题或进程名含 %r 的可见窗口" % a.window)
        w0 = hits[0]
        if u32.IsIconic(w0.hwnd):
            raise CuError("CU_MINIMIZED",
                          "窗口 %r 是最小化的，截不到内容。可以让用户点开，也可以用 "
                          "cu.py click 点任务栏图标把它拉起来（先 shot 看图标位置）。"
                          % w0.title[:24])
        # 屏幕截图抓的是"那块区域当前显示什么"，**不是"窗口的内容"**。
        # 被盖住时会静默截到别的东西，上层拿到一张不相干的图还以为成功——
        # 和 CU_MINIMIZED 那类假阴性一样，必须报出来而不是猜。
        _rx, _ry, _rw, _rh = window_rect(w0.hwnd)
        if _top_root_at(_rx + _rw // 2, _ry + _rh // 2) != w0.hwnd:
            _force_foreground(w0.hwnd)
            time.sleep(0.15)
            _rx, _ry, _rw, _rh = window_rect(w0.hwnd)
            if _top_root_at(_rx + _rw // 2, _ry + _rh // 2) != w0.hwnd:
                raise CuError("CU_COVERED",
                              "窗口 %r 被别的窗口盖住了，截出来会是盖在它上面的内容。"
                              "先 focus 它、或把挡着的窗口挪开。" % w0.title[:24])
        region = _region_arg("%d,%d,%d,%d" % (_rx, _ry, _rw, _rh), "--window 的窗口矩形")
        win_note = " · 窗口「%s」" % w0.title[:24]
    if getattr(a, "region", None) is not None:
        # 两个都给就取交集，这样"截这个窗口，但把导航栏裁掉"才写得出来
        r2 = _region_arg(a.region, "--region")
        region = _intersect(r2, region) if region else r2
    return region, win_note, _shot_le(a)   # 决策与校验都在 _shot_le 里


def emit_binary(a):
    """--emit-binary：把一帧 PNG **直接**写到 stdout，不落盘。

    协议（给常驻服务与 --rpc 降级路径用，人不要手敲）：
        第一行：一行 JSON meta
        其后：原始 PNG 字节
    用二进制流而不是 base64，是为了省掉 33% 的膨胀——本地管道的带宽不是瓶颈，
    但每次多拷 1/3 字节是白花的；而且不落地就没有"临时文件泄漏"这条尾巴。
    """
    png, meta = capture_shot(a)
    vx, vy, vw, vh = screen()
    meta["screen"] = {"x": vx, "y": vy, "width": vw, "height": vh}
    if meta.get("region"):
        x0, y0 = to_norm(meta["region"][0], meta["region"][1])
        x1, y1 = to_norm(meta["region"][2], meta["region"][3])
        meta["region_normalized"] = [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)]
    out = sys.stdout.buffer
    out.write((json.dumps(meta, ensure_ascii=False) + "\n").encode("utf-8"))
    out.write(png)
    out.flush()
    return 0


# ---------------------------------------------------------------- 环境自检 / 安装引导
def _chk(name, ok, detail, fix="", required=True):
    return {"name": name, "ok": bool(ok), "detail": str(detail),
            "fix": fix, "required": bool(required)}


def _scan_cdp(ports=range(9222, 9233), timeout=0.25):
    """扫本机 CDP 端点。只在 doctor 里报告"能不能接浏览器"，
    不在这里启动任何浏览器。"""
    import urllib.request
    found = []
    for p in ports:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % p,
                                        timeout=timeout) as r:
                info = json.loads(r.read().decode("utf-8", "replace"))
            found.append({"port": p, "browser": info.get("Browser") or "?"})
        except Exception:
            continue
    return found


def _chrome_paths():
    cands = []
    for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
                 os.environ.get("LOCALAPPDATA")):
        if not base:
            continue
        for sub in ("Google\\Chrome\\Application\\chrome.exe",
                    "Microsoft\\Edge\\Application\\msedge.exe"):
            p = os.path.join(base, sub)
            if os.path.exists(p):
                cands.append(p)
    return cands


def doctor(as_json=False):
    """一次说清"这台机器上我们还缺什么、坏在哪、怎么修"。

    对齐豆包 agent_infra 的环境校验思路：**缺依赖不许静默降级**，
    必须报出"缺什么 + 那条能直接粘出去跑的命令"。
    """
    checks = []
    checks.append(_chk("python", sys.version_info >= (3, 10),
                       "%d.%d.%d  %s" % (sys.version_info[0], sys.version_info[1],
                                         sys.version_info[2], sys.executable),
                       "装 Python 3.10+ 并用 DSH_CU_PYTHON 指定绝对路径"))
    try:
        import PIL
        checks.append(_chk("pillow", True, PIL.__version__))
    except ImportError:
        checks.append(_chk("pillow", False, "缺失（截图不可用）", PIP_HINT))
    try:
        import win32file  # noqa: F401
        import win32pipe  # noqa: F401
        checks.append(_chk("pywin32", True, "已装（常驻服务命名管道需要）"))
    except ImportError:
        checks.append(_chk("pywin32", os.name != "nt",
                           "缺失（命名管道不可用，会退回 --rpc）",
                           '"%s" -m pip install --user pywin32' % PY))
    try:
        vx, vy, vw, vh = screen()
        ok = vw > 0 and vh > 0
        checks.append(_chk("screen", ok, "虚拟桌面 %dx%d @(%d,%d)" % (vw, vh, vx, vy),
                           "检查显示设置/远程会话"))
    except Exception as e:
        checks.append(_chk("screen", False, "读取失败：%s" % e))
    try:
        ok = input_desktop_ok()
        checks.append(_chk("input_desktop", ok,
                           "是" if ok else "否（锁屏或 UAC 安全桌面：注入与截图都不可用）",
                           "让用户解锁后再试", required=False))
    except Exception as e:
        checks.append(_chk("input_desktop", False, str(e), "", required=False))
    try:
        hwnd = u32.GetForegroundWindow()
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        ok, lvl = injectable(pid.value) if pid.value else (False, "?")
        checks.append(_chk("inject", ok,
                           "前台 hwnd=%s pid=%s 完整性=%s" % (hwnd, pid.value, lvl),
                           "目标权限高于本进程时注入会被静默拦截（UIPI）", required=False))
    except Exception as e:
        checks.append(_chk("inject", False, str(e), "", required=False))
    try:
        v = clip_get()
        checks.append(_chk("clipboard", True, "文本通道 OK（%d 字）" % (len(v) if v else 0),
                           required=False))
    except CuError as e:
        checks.append(_chk("clipboard", False, str(e),
                           "别的进程常短暂占用剪贴板，重试一次即可", required=False))
    try:
        os.makedirs(os.path.dirname(AUDIT_FILE), exist_ok=True)
        with open(AUDIT_FILE, "a", encoding="utf-8"):
            pass
        sz = os.path.getsize(AUDIT_FILE) if os.path.exists(AUDIT_FILE) else 0
        checks.append(_chk("audit", True, "%s（%d 字节）" % (AUDIT_FILE, sz)))
    except OSError as e:
        checks.append(_chk("audit", False, "写不进去：%s" % e,
                           "检查 DSH_CU_HOME / LOCALAPPDATA 的写权限"))
    try:
        u = _uia()
        snap = u._call("ping", timeout=8)
        checks.append(_chk("uia_sidecar", True,
                           "pid=%s gen=%s" % (snap.get("pid"), snap.get("gen")),
                           "", required=False))
    except Exception as e:
        checks.append(_chk("uia_sidecar", False, "不可用：%s" % e,
                           "元素级能力降级；仍可用 shot 看图路线", required=False))
    found = _scan_cdp()
    chromes = _chrome_paths()
    if found:
        checks.append(_chk("browser_cdp", True,
                           "已有端点：%s" % ", ".join(":%d %s" % (f["port"], f["browser"])
                                                      for f in found), "", required=False))
    elif chromes:
        checks.append(_chk("browser_cdp", False,
                           "没扫到调试端口；已装：%s" % chromes[0],
                           "用 scripts/chrome-debug.cmd 以独立 user-data-dir 启动 Chrome"
                           "（Chrome 136+ 默认 profile 拒绝开调试端口）", required=False))
    else:
        checks.append(_chk("browser_cdp", False, "没找到 Chrome/Edge",
                           "装 Chrome 后用 scripts/chrome-debug.cmd 启动", required=False))

    bad = [c for c in checks if c["required"] and not c["ok"]]
    if as_json:
        return [json.dumps({"checks": checks, "ok": not bad,
                            "required_failed": [c["name"] for c in bad]},
                           ensure_ascii=False)]
    lines = ["环境自检：必需项%s" % ("全部通过" if not bad else
                                     "有 %d 项未通过（%s）"
                                     % (len(bad), "、".join(c["name"] for c in bad)))]
    for c in checks:
        tag = "OK  " if c["ok"] else ("FAIL" if c["required"] else "WARN")
        lines.append("  [%s] %-16s %s" % (tag, c["name"], c["detail"]))
        if not c["ok"] and c["fix"]:
            lines.append("         → %s" % c["fix"])
    return lines


def setup(install=False, as_json=False):
    """依赖安装引导。**默认只打印命令**，加 --install 才真的执行。

    自动改用户的 Python 环境属于偏重的不可逆操作：打印出来让人看清楚再决定，
    比"我帮你装好了"稳妥。
    """
    cmds = [('"%s" -m pip install --user pillow' % PY, "截图（必需）"),
            ('"%s" -m pip install --user pywin32' % PY,
             "命名管道与虚拟桌面 COM（Windows 建议装）")]
    if as_json:
        return [json.dumps({"install": install,
                            "commands": [{"cmd": c, "why": w} for c, w in cmds]},
                           ensure_ascii=False)]
    if not install:
        lines = ["需要的安装命令（本命令只打印，不执行）："]
        lines += ["  %s    # %s" % (c, w) for c, w in cmds]
        lines.append("确认要装就加 --install（会真的执行上面两条 pip 命令）。")
        return lines
    import subprocess as _sp
    lines = ["开始安装依赖……"]
    for c, w in cmds:
        try:
            r = _sp.run(c, shell=True, capture_output=True, text=True, timeout=300)
            lines.append("  %s -> 退出码 %d  %s"
                         % (w, r.returncode,
                            (r.stdout or r.stderr or "").strip()[-200:]))
        except Exception as e:
            lines.append("  %s -> 失败：%s" % (w, e))
    lines.append("装完重新跑 doctor 复核；如果 Python 环境是别的解释器，"
                 "先用 DSH_CU_PYTHON 指过去。")
    return lines


def _uia():
    """惰性 import 元素层客户端。

    为什么惰性：uia.py 需要 `from cu import CuError`，模块级互相 import 会成环。
    顺带的好处是——不用元素级能力的人，永远不必付那次 PowerShell 子进程启动。
    """
    try:
        import uia
    except ImportError as e:
        raise CuError("CU_NO_SIDECAR", "元素层模块 uia.py 不可用：%s" % e)
    return uia


# 2026-10-27：虚拟桌面（desktop.py / _desktop / _fmt_desktop）整体移除，功能重做中。


def execute_namespace(a):
    """执行一条已解析的命令，返回输出行。

    CLI 与常驻服务**共用这一条路径**：静默窗的开启/续租、审计的 attempted/completed/
    rejected 三态，只在这一处实现。两边各写一份的话，迟早会有一边忘了续静默窗，
    而那个 bug 的表现是"自己操作触发了自己的凝视队列"——很难查。
    """
    inj = a.cmd in INJECT_CMDS
    # 审计集合比"注入集合"大：eval/spawn 不是注入，但它们能起进程、跑任意代码，
    # 是绕过 confirm 闸的主要路径——**必须留痕**，否则破坏性约束形同虚设。
    audited = a.cmd in AUDIT_CMDS
    if inj:
        _mute(MUTE_SECONDS)
    try:
        lines = list(run(a))
        if audited:
            audit(a.cmd, {}, "completed")
        return lines
    except (CuError, OSError) as e:
        if audited:
            audit(a.cmd, {}, "rejected", {"error": getattr(e, "code", "CU_OS")})
        raise
    finally:
        if inj:
            _mute(MUTE_SECONDS)


def run(a):
    """执行一条已解析的命名空间，返回若干输出行。batch 复用这个函数。"""
    if a.cmd in INJECT_CMDS:
        # 对齐豆包 _instrumentation.py 的三态审计，但只记摘要、绝不记正文。
        args = {}
        if a.cmd == "type":
            args = {"chars": len(getattr(a, "text", "") or "")}
        elif a.cmd == "key":
            args = {"combo": getattr(a, "combo", "")}
        else:
            args = {k: getattr(a, k) for k in ("x", "y") if hasattr(a, k)}
        audit(a.cmd, args, "attempted")
    if a.cmd == "shot":
        png, meta = capture_shot(a)
        os.makedirs(SHOT_DIR, exist_ok=True)
        out = a.out or os.path.join(SHOT_DIR, "cu_%d.png" % int(time.time() * 1000))
        if a.out:
            os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
        try:
            with open(out, "wb") as f:
                f.write(png)
        except Exception as e:
            raise CuError("CU_SHOT_FAIL", "写盘失败 %s：%s" % (out, e))
        # 落盘后再校验一次：磁盘满/被杀会写出截断文件，而调用方随后要 read_image 打开它
        try:
            with open(out, "rb") as f:
                _validate_png(f.read(), meta["width"], meta["height"])
        except CuError:
            try:
                os.remove(out)
            except OSError:
                pass
            raise
        if not a.keep_all:
            _prune_shots()
        size = (meta["width"], meta["height"])
        nb, scale, lit = meta["bytes"], meta["scale"], meta["lit_ratio"]
        region = meta.get("region")
        vx, vy, vw, vh = screen()
        if region:
            src = "%dx%d@%d,%d" % (region[2] - region[0], region[3] - region[1],
                                   region[0], region[1])
        elif meta.get("capture_path") == "printwindow":
            # 窗口截图必须报**窗口自身**的尺寸；以前这里一律报屏幕尺寸，是误导。
            src = "%dx%d(窗口)" % (meta["source"]["width"], meta["source"]["height"])
        else:
            src = "%dx%d" % (vw, vh)
        # 后端如实标出来：printwindow = 问窗口要的图（遮挡也能截），gdi = 屏幕裁剪
        backend = "打印窗口(PrintWindow)" if meta.get("capture_path") == "printwindow" \
            else "屏幕裁剪(GDI)"
        win_note = (" · 窗口「%s」" % meta["window"]) if meta.get("window") else ""
        lines = ["%s  %dx%d  源 %s  缩放 %.3f  %.0fKB  约 %d 图像 token%s  [%s]"
                 % (out, size[0], size[1], src, scale, nb / 1024.0,
                    meta["estimatedImageTokens"], win_note, backend)]
        if lit < 0.01:
            if region:
                lines.append("[CU_BLACK] 这块区域几乎全黑（非黑像素 %.1f%%）。"
                             "裁剪区本来就黑是常事，别据此判断异常；"
                             "要看整屏状态就截全屏。" % (lit * 100))
            elif not input_desktop_ok():
                lines.append("[CU_BLACK] 画面几乎全黑，而且当前**不在可输入桌面上**——"
                             "多半是锁屏或 UAC 安全桌面。此时注入完全无效，"
                             "请停下并告诉用户手动解锁/确认，不要重试点击。")
            else:
                lines.append("[CU_BLACK] 画面几乎全黑（非黑像素 %.1f%%）。可能是全屏独占程序"
                             "（游戏/硬件加速播放器），也可能窗口没画出来。"
                             "换个方式验证，别反复重截。" % (lit * 100))
        if region:
            # --region 用的是物理像素，而 click 收 0-1000。裁剪之后坐标原点变了，
            # 所以直接把换算公式连数字一起给出来，省得心算错。
            x0, y0 = to_norm(region[0], region[1])
            x1, y1 = to_norm(region[2], region[3])
            lines.append("裁剪覆盖屏幕归一化 x %.1f~%.1f  y %.1f~%.1f"
                         % (x0, x1, y0, y1))
            lines.append("图内量到的比例 (fx,fy)（0~1，从左/上起）换算成点击坐标："
                         "x=%.1f+fx*%.1f  y=%.1f+fy*%.1f"
                         % (x0, x1 - x0, y0, y1 - y0))
        return lines

    if a.cmd in ("click", "move", "hover"):
        if a.cmd == "move" or a.cmd == "hover":
            x, y = to_px(a.x, a.y)
            move(x, y)
            return ["光标 -> 像素(%d,%d) 归一化%s" % (x, y, (round(a.x, 2), round(a.y, 2)))]
        x, y = click(a.x, a.y, a.right, a.double, a.triple)
        what = "三击" if a.triple else ("双击" if a.double else ("右键" if a.right else "点击"))
        return ["%s 归一化(%.2f,%.2f) -> 像素(%d,%d)" % (what, a.x, a.y, x, y)]

    if a.cmd == "drag":
        r = drag(a.x1, a.y1, a.x2, a.y2, a.ms)
        return ["拖拽 归一化(%.1f,%.1f)->(%.1f,%.1f) 像素%s 用时 %dms"
                % (a.x1, a.y1, a.x2, a.y2, r, a.ms)]

    if a.cmd == "scroll":
        n = scroll(a.x, a.y, a.n)
        return ["滚轮 %+d @ 归一化(%.1f,%.1f)" % (n, a.x, a.y)]

    if a.cmd == "type":
        return ["已输入 %d 字" % type_text(a.text)]

    if a.cmd == "key":
        return ["已按 %s" % key(a.combo)]

    if a.cmd == "key-down":
        return ["已按住 %s（记住：必须与 key-up 在同一次 batch 里成对出现，"
                "否则它会 latch 住，之后所有点击都带修饰键）" % key_hold(a.name, True)]

    if a.cmd == "key-up":
        return ["已松开 %s" % key_hold(a.name, False)]

    if a.cmd == "release":
        return ["已松开：%s" % "、".join(release_all(force=True))]

    if a.cmd == "paste":
        old = clip_get_soft()
        n = paste(a.text)
        return ["已粘贴 %d 字%s" % (n, _restore_clip(old, a.keep))]

    if a.cmd == "copy":
        # Ctrl+C 会冲掉用户剪贴板，而内容已经取回来了，所以默认还原
        old = clip_get_soft()
        v = copy_selection()
        return [_dump_text(v, a.out, "选中内容") + _restore_clip(old, a.keep)]

    if a.cmd == "clip":
        return [_dump_text(clip_get(), a.out, "剪贴板")]

    if a.cmd == "clip-set":
        return ["已写入剪贴板 %d 字" % clip_set(a.text)]

    if a.cmd == "clip-image":
        return ["已把 %s 放到剪贴板（%s）" % (a.path, clip_set_image(a.path))]

    if a.cmd == "paste-image":
        return ["已贴图 %s（%s）。确认没问题再 key enter 发送" % (a.path, paste_image(a.path))]

    if a.cmd == "save-image":
        return ["剪贴板图片 %s 已存到 %s" % (clip_get_image(a.path), a.path)]

    if a.cmd == "wait":
        # 单位是**秒**。对齐豆包 _validation.py：显式拒布尔、拒毫秒误传，并给可执行提示。
        # 上界保留我方更宽的 600s（豆包是 180s）——更久的等待本来就该交给后台 job，
        # 但那是用法问题，没必要在这里硬拦；这条差异在能力对照表里标明。
        time.sleep(wait_seconds(a.seconds))
        return ["等了 %gs" % a.seconds]

    if a.cmd == "move-smooth":
        _move_smooth(a.x, a.y, getattr(a, "ms", 300))
        return ["光标平滑移动到 归一化(%.2f,%.2f) 用时 %dms"
                % (a.x, a.y, getattr(a, "ms", 300))]

    if a.cmd == "where":
        x, y = where()
        return ["光标 像素(%d,%d) 归一化%s" % (x, y, to_norm(x, y))]

    if a.cmd == "rect":
        hits = find_windows(a.pattern)
        if not hits:
            raise CuError("CU_NO_WINDOW", "没有匹配的可见窗口。用 cu.py windows 看有哪些")
        # 最小化的窗口拿到的是 Windows 给的假矩形：x/y = -32000，而宽高仍大于 0。
        # 下游（如 wxcheck）只看宽高是否为正，会把这块假矩形当有效区域去截图判读，
        # 得出"未见失败标记"的假阴性——比不判更糟。所以必须和 shot --window 一样拦掉。
        if u32.IsIconic(hits[0].hwnd):
            raise CuError("CU_MINIMIZED",
                          "窗口 %r 处于最小化，位置是假坐标（-32000），不能用。"
                          "三个出路：① 让用户点开；② 你自己点任务栏/托盘图标把它拉起"
                          "（先 shot 看图标位置，或 windows --all 确认在不在托盘）；"
                          "③ 先 focus-cc 把界面切出来问用户。"
                          % hits[0].title[:24])
        lines = []
        for w in hits[:4]:
            if u32.IsIconic(w.hwnd):
                lines.append("hwnd=%d %s %r\n  [CU_MINIMIZED] 已最小化，位置是假坐标，略过"
                             % (w.hwnd, w.exe or "?", w.title[:40]))
                continue
            x, y, cw, ch = window_rect(w.hwnd)
            n1, n2 = to_norm(x, y)
            n3, n4 = to_norm(x + cw, y + ch)
            lines.append("hwnd=%d %s %r\n  像素 x,y,w,h = %d,%d,%d,%d\n"
                         "  归一化 左上%s 右下%s\n  截图用 --region %d,%d,%d,%d"
                         % (w.hwnd, w.exe or "?", w.title[:40], x, y, cw, ch,
                            (n1, n2), (n3, n4), x, y, cw, ch))
        return lines

    if a.cmd == "windows":
        pat = a.pattern
        if getattr(a, "show_hidden", False):
            wins = [w for w in enum_windows(visible_only=False) if not pat or w.match(pat)]
        else:
            wins = find_windows(pat) if pat else enum_windows()
        if not wins:
            raise CuError("CU_NO_WINDOW",
                          "没有匹配的窗口" + ("（连隐藏的也没有）"
                                          if getattr(a, "show_hidden", False)
                                          else "（被收进托盘的看不见，加 --all 试）"))
        lines = ["%-10s %-22s %-13s %s" % ("hwnd", "进程", "注入", "标题")]
        for w in wins:
            ok, lvl = injectable(w.pid)
            note = "" if ctypes.windll.user32.IsWindowVisible(w.hwnd)                 else "  ← 隐藏：多半在托盘里，别急着重启它"
            lines.append("%-10d %-22s %-13s %s%s" % (w.hwnd, (w.exe or "?")[:20],
                                                     "OK" if ok else "★UIPI " + lvl,
                                                     w.title[:52], note))
        # 不带关键词的 --all 能吐出两百多行（大量是无名的辅助窗口）。别让它整坨进上下文。
        if not pat and len(lines) > 41:
            more = len(lines) - 41
            lines = lines[:41] + ["……还有 %d 个窗口。**给个关键词**再看（如 "
                                  "`windows --all 微信`），别把整张表拉进上下文。" % more]
        return lines

    if a.cmd == "focus":
        t, active = focus(a.pattern)
        return ["已切到 hwnd=%d pid=%d %r  前台=%s" % (t.hwnd, t.pid, t.title[:40],
                                                       "是" if active else "否（可能被别的窗口抢回）")]

    if a.cmd == "focus-cc":
        t, active = focus_cc(a.pattern)
        return ["已切到 DSH 界面 hwnd=%d pid=%d %r  前台=%s"
                % (t.hwnd, t.pid, t.title[:40], "是" if active else "否（被别的窗口抢回）")]

    if a.cmd == "find":
        # 优先走元素层：它给的是带 ref 的结构化元素（还能直接拿去 element 动作），
        # 比老的"类型+名字+坐标"三列信息量大。**只有元素层本身不可用**时才退回老路；
        # "过滤后没结果"必须原样报 CU_NO_ELEMENT，不能假装元素层坏了又去跑老路
        # （那会把"正则没匹配到"变成一条看不懂的旧消息）。
        try:
            uia = _uia()
        except CuError:
            uia = None
        if uia is not None:
            try:
                need_all = bool(getattr(a, "regex", False) or getattr(a, "exact", False)
                                or getattr(a, "index", None) is not None)
                query = "" if getattr(a, "regex", False) else a.pattern
                got = uia.find(query, window=a.window,
                                limit=max(a.limit or 20, 200 if need_all else 50))
                rows = got["refs"]
                if getattr(a, "role", None):
                    want = a.role.casefold()
                    rows = [x for x in rows if str(x.get("role", "")).casefold() == want]
                if getattr(a, "exact", False):
                    rows = [x for x in rows if str(x.get("name", "")).casefold()
                            == a.pattern.casefold()]
                if getattr(a, "regex", False):
                    try:
                        rx = re.compile(a.pattern, re.IGNORECASE)
                    except re.error as e:
                        raise CuError("CU_BAD_ARG", "正则写错了：%s" % e)
                    rows = [x for x in rows if rx.search(str(x.get("name", "")))]
                if getattr(a, "index", None) is not None:
                    i = int(a.index)
                    rows = rows[i:i + 1] if 0 <= i < len(rows) else []
            except CuError as e:
                if e.code != "CU_NO_SIDECAR":
                    raise
                uia = None
            else:
                if not rows:
                    raise CuError("CU_NO_ELEMENT",
                                  "UIA 里没有匹配 %r 的控件（可能没暴露可访问性树，"
                                  "或者被 --role/--exact/--regex 过滤没了）。"
                                  "改用 shot 看图定位。" % a.pattern)
                if a.click:
                    ref = rows[0]["ref"]
                    cx, cy, el = uia.center_of(ref)
                    nx, ny = to_norm(cx, cy)
                    x, y = click(nx, ny)
                    return ["已点 %s %s %r（像素 %d,%d）"
                            % (ref, el.get("role"), str(el.get("name"))[:30], x, y)]
                if getattr(a, "as_json", False):
                    return [json.dumps({"refs": rows}, ensure_ascii=False)]
                lines = ["UIA 找到 %d 个（未截图；ref 只对本次快照有效）：" % len(rows)]
                for it in rows[:max(1, a.limit or 20)]:
                    lines.append("  %-6s %-13s %-32s element %s --invoke"
                                 % (it.get("ref"), it.get("role"),
                                    str(it.get("name"))[:32], it.get("ref")))
                return lines
        # 元素层不可用 → 老路（纯坐标，不支持 ref）
        rows = uia_find(a.pattern, a.window, a.limit)
        if not rows:
            raise CuError("CU_NO_ELEMENT",
                          "UIA 里没有名称含 %r 的控件（该应用多半没暴露可访问性树）。"
                          "改用 shot 看图定位。" % a.pattern)
        if a.click:
            ct, nm, cx, cy = rows[0]
            nx, ny = to_norm(cx, cy)
            x, y = click(nx, ny)
            return ["已点 UIA 控件 %s %r（像素 %d,%d）" % (ct, nm[:30], x, y)]
        lines = ["UIA 找到 %d 个（未截图；元素层不可用，只能给坐标）：" % len(rows)]
        for ct, nm, cx, cy in rows:
            nx, ny = to_norm(cx, cy)
            lines.append("  %-12s %-34s 点这里: click %.0f %.0f"
                         % (ct, nm[:34], nx, ny))
        return lines

    if a.cmd == "spawn":
        r = spawn_process(a.exe, getattr(a, "args", []) or [], getattr(a, "cwd", None))
        return ["已启动 pid=%d  %s %s" % (r["pid"], r["exe"],
                                         " ".join(r["argv"]) if r["argv"] else "")]

    if a.cmd == "app-is-running":
        r = app_is_running(a.query)
        if getattr(a, "as_json", False):
            return [json.dumps(r, ensure_ascii=False)]
        if not r["running"]:
            return ["%r 没在跑（规范名：%s）" % (r["query"], r["catalog_name"] or "目录里也没找到")]
        lines = ["%r 在跑，%d 个窗口：" % (r["query"], len(r["matches"]))]
        for m in r["matches"]:
            lines.append("  hwnd=%s pid=%s %s  %r"
                         % (m["hwnd"], m["pid"], os.path.basename(m["exe"]),
                            m["title"][:40]))
        return lines

    if a.cmd == "app-resize":
        r = app_resize(a.query, a.width, a.height)
        return ["窗口 %r 改成 %dx%d（实际 %dx%d，hwnd=%s）"
                % (r["title"], r["requested"][0], r["requested"][1],
                   r["actual"][0], r["actual"][1], r["hwnd"])]

    if a.cmd == "app-terminate":
        r = app_terminate(a.query, bool(getattr(a, "confirm", False)))
        return ["已结束 %r pid=%s\n%s" % (r["title"], r["pid"], r["output"] or "（无输出）")]

    if a.cmd == "hybrid-programs":
        r = hybrid_programs()
        if not r["browsers"]:
            return [r["advice"]]
        lines = ["浏览器窗口 %d 个（网页内的活应走 browser_use）：" % len(r["browsers"])]
        for b in r["browsers"]:
            lines.append("  hwnd=%s pid=%s %s  %r"
                         % (b["hwnd"], b["pid"], os.path.basename(b["exe"]),
                            b["title"][:40]))
        if r["cdp_endpoints"]:
            lines.append("可用 CDP 端点：" + ", ".join(
                ":%d %s" % (f["port"], f["browser"]) for f in r["cdp_endpoints"]))
        else:
            lines.append("**没有 CDP 端点** —— 这些窗口目前只能走 screenshot+坐标；"
                         "要 ref-first 操作就用 scripts/chrome-debug.cmd 起独立 profile 实例。")
        return lines

    if a.cmd == "apps":
        apps = start_apps()
        if a.pattern:
            q = a.pattern.casefold()
            apps = [x for x in apps if q in x[0].casefold() or q in x[1].casefold()]
            if not apps:
                raise CuError("CU_APP_NOT_FOUND", "目录里没有含 %r 的应用" % a.pattern)
        cap = 40 if not a.pattern else 80
        head = apps[:cap]
        # 名字和 AppID 都**不截断**：本机 96/221 个 AppID 超过 60 字符（最长 126），
        # 截了就没法用来启动。用制表符分隔，省掉对齐填充。
        lines = ["匹配 %d 个，显示前 %d 个（名字<制表符>AppID；launch 两者都收）"
                 % (len(apps), len(head))]
        lines += ["%s\t%s" % (n, aid) for n, aid in head]
        if len(apps) > len(head):
            lines.append("（还有 %d 个未显示，用 cu.py apps <更具体的关键词> 缩小范围）"
                         % (len(apps) - len(head)))
        return lines

    if a.cmd == "launch":
        status, w, name, appid, owned = launch(a.name, a.timeout, not a.no_focus)
        if status == "ready":
            ok, lvl = injectable(w.pid)
            note = "" if owned else "\n⚠ 这个窗口标题跟应用名对不上，未必真是它——先截图确认再动手。"
            return ["status=ready  应用=%r\n稳定窗口 hwnd=%d 进程=%s pid=%d 标题=%r 注入=%s%s"
                    % (name, w.hwnd, w.exe or "?", w.pid, w.title[:40],
                       "OK" if ok else "★UIPI " + lvl, note)]
        return ["status=accepted_unverified  应用=%r\n没等到稳定的新窗口（可能本来就开着，"
                "也可能还在加载）。不要重复 launch——改 shot 看屏幕，或再等一会儿。" % name]

    if a.cmd == "码":
        return 码(getattr(a, "as_json", False))
    if a.cmd == "口径":
        return 口径()
    if a.cmd == "静默":
        return 静默(a.seconds)

    if a.cmd in ("盯", "watch", "收", "瞧"):
        if getattr(a, "status", False):
            return 盯状态(a.queue, getattr(a, "as_json", False))
        if getattr(a, "next", False):
            return 盯取(a.queue, a.wait, a.peek, getattr(a, "as_json", False),
                        getattr(a, "all", False), getattr(a, "archive", False))
        r = 盯(a.baseline, a.region, a.window, a.timeout, a.ignore,
              a.out, not a.no_raise, a.start_at, a.box,
              not a.once, a.queue, getattr(a, "coalesce", 0.0))
        if r.get("follow"):
            return [r["head"]]
        fmt = lambda t: time.strftime("%H:%M:%S", time.localtime(t))
        dur = _fmt_dur(r["end_ts"] - r["start_ts"])
        if r["reason"] == "changed":
            head = "画面变了（%s → %s，等了 %s）" % (fmt(r["start_ts"]),
                                                fmt(r["end_ts"]), dur)
        else:
            head = "等满 %s 画面一直没变（%s → %s）" % (dur, fmt(r["start_ts"]),
                                                  fmt(r["end_ts"]))
        if r.get("target"):
            head += " · 盯的是「%s」" % r["target"][:24]
        lines = [head]
        if r.get("warn"):
            lines.append(r["warn"])
        if r.get("box"):
            lines.append("变化位置 归一化 x %.0f~%.0f  y %.0f~%.0f（可直接拿去 click）"
                         % (r["box"][0], r["box"][2], r["box"][1], r["box"][3]))
        lines.append(r["path"])
        return lines

    if a.cmd == "post":
        verdict, path, note, title = post(a.window, a.text, a.image, a.at,
                                          not a.no_enter, a.out, a.keep)
        lines = ["%s · 窗口「%s」" % (verdict, title[:24])]
        if path:
            lines.append(path)
        if note:
            lines.append(note)
        return lines

    if a.cmd == "probe":
        vx, vy, vw, vh = screen()
        lines = ["屏幕 虚拟桌面(%d,%d,%d,%d) 物理 %dx%d" % (vx, vy, vw, vh, vw, vh),
                 "本进程完整性级别 %s（注入只对同级或更低级窗口有效）"
                 % IL_NAMES.get(_SELF_IL, str(_SELF_IL))]
        try:
            import PIL
            lines.append("Pillow %s OK" % PIL.__version__)
        except ImportError:
            lines.append("Pillow 缺失 —— shot 不可用")
        try:
            v = clip_get()
            lines.append("剪贴板 OK（%s 字）" % (len(v) if v else 0))
        except CuError as e:
            lines.append("剪贴板异常：%s" % e)
        x, y = where()
        lines.append("光标 像素(%d,%d) 归一化%s" % (x, y, to_norm(x, y)))
        lines.append("可见窗口 %d 个" % len(enum_windows()))
        lines.append("可输入桌面：%s" % ("是" if input_desktop_ok()
                    else "否（锁屏或 UAC 安全桌面上，注入与截图都不可用）"))
        return lines

    if a.cmd == "state-capture":
        uia = _uia()
        snap = uia.snapshot(a.window, a.depth, a.limit)
        refs = snap.get("refs") or []
        if a.as_json:
            return [json.dumps(snap, ensure_ascii=False)]
        win = (snap.get("window") or {}).get("name") or "?"
        lines = ["UIA 快照 gen=%s 窗口「%s」 %d 个元素%s"
                 % (snap.get("gen"), win[:28], len(refs),
                    "（已达上限被截断，缩 --depth/--limit 或指定 --window）"
                    if snap.get("truncated") else "")]
        for it in refs:
            r = it.get("rect") or {}
            extra = ""
            if it.get("value"):
                extra = " value=%r" % str(it["value"])[:30]
            if it.get("toggle"):
                extra += " toggle=%s" % it["toggle"]
            if it.get("patterns"):
                extra += " [%s]" % ",".join(p.replace("Pattern", "") for p in it["patterns"])
            lines.append("  %-6s %-13s %-30s 点这里: element %s --click%s"
                         % (it.get("ref"), it.get("role"),
                            str(it.get("name"))[:30], it.get("ref"), extra))
        lines.append("（用 `element <ref> <动作>` 操作；UI 变了就重新 state-capture，"
                     "旧 ref 会失效）")
        return lines

    if a.cmd == "element":
        uia = _uia()
        acts = [k for k in ("click", "invoke", "set_focus", "toggle", "select",
                            "add_to_selection", "expand", "collapse",
                            "scroll_into_view", "window_close")
                if getattr(a, k, False)]
        if a.set_value is not None:
            acts.append("set_value")
        if a.set_range_value is not None:
            acts.append("set_range_value")
        if a.get:
            r = uia.get(a.ref, a.get)
            return ["ref=%s %s=%s" % (a.ref, a.get,
                                      json.dumps(r.get("value"), ensure_ascii=False))]
        if not acts:
            r = uia.get(a.ref)
            el = r.get("element") or {}
            return ["ref=%s role=%s name=%r 支持模式：%s"
                    % (a.ref, el.get("role"), el.get("name"),
                       ",".join(el.get("patterns") or []) or "（无）"),
                    "（要操作就加 --invoke/--set-value/--toggle/--click 之一）"]
        if "click" in acts:
            # 强制走真鼠标：有些自绘控件不认 UIA pattern，点了才算
            cx, cy, el = uia.center_of(a.ref)
            nx, ny = to_norm(cx, cy)
            x, y = click(nx, ny)
            return ["element %s 点击 -> 像素(%d,%d) 归一化(%.1f,%.1f) role=%s name=%r"
                    % (a.ref, x, y, nx, ny, el.get("role"), el.get("name"))]
        action = acts[0]
        value = a.set_value if action == "set_value" else a.set_range_value
        r = uia.act(a.ref, action, value)
        if r.get("noop"):
            return ["element %s %s 已经是 %s，未重复切换" % (a.ref, action, r.get("state"))]
        el = r.get("element") or {}
        return ["element %s 执行 %s 成功。现在 role=%s name=%r value=%r toggle=%s"
                % (a.ref, action, el.get("role"), el.get("name"),
                   el.get("value"), el.get("toggle"))]

    if a.cmd == "element-at":
        uia = _uia()
        r = uia.at(a.x, a.y)
        el = r.get("element") or {}
        if a.as_json:
            return [json.dumps(el, ensure_ascii=False)]
        return ["像素(%d,%d) 上是 ref=%s role=%s name=%r cls=%s 支持模式：%s"
                % (a.x, a.y, el.get("ref"), el.get("role"), el.get("name"),
                   el.get("cls"), ",".join(el.get("patterns") or []) or "（无）")]

    if a.cmd == "resync":
        _uia().resync()
        return ["元素缓存已清空（旧 ref 全部失效，重新 state-capture）"]

    if a.cmd == "doctor":
        return doctor(getattr(a, "as_json", False))

    if a.cmd == "setup":
        return setup(bool(getattr(a, "install", False)), getattr(a, "as_json", False))

    raise CuError("CU_BAD_ARG", "未知命令 %r" % a.cmd)


_PARSER = None


def _parser():
    """整份 subparser 树只建一次。

    argparse 的树是静态的（60+ 子命令），但 `build_parser()` 单次约 3ms；
    以前的写法是**每条命令、batch 的每一行**都重建一次——整个插件的卖点就是
    "省掉每条命令的冷启动"，结果协议层又按行白付一遍，很讽刺。
    """
    global _PARSER
    if _PARSER is None:
        _PARSER = build_parser()
    return _PARSER


def make_parser_for(line):
    """batch 用：把一行文本解析成同样的命名空间。
    用 posix=False 保住 Windows 路径里的反斜杠，再手工剥掉成对的引号——
    posix=True 会把 C:\\Users 吃成 C:Users。
    引号不成对时 shlex 会抛 ValueError，必须在这里转成 CuError：
    否则它会一路逸出到顶层把整批打死，后面的 key-up 永不执行，修饰键就 latch 了。"""
    try:
        parts = shlex.split(line, posix=False)
    except ValueError as e:
        raise CuError("CU_BAD_ARG", "这一行引号不成对（%s）：%s" % (e, line[:60]))
    parts = [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p
             for p in parts]
    return _parser().parse_args(parts)


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "batch":
        ns = _parser().parse_args(argv)
        rc = 0
        try:
            # 逐行读而不是 read() 全量：输入里混进坏字节时最多毁掉一行，
            # 前面已经执行过的行不受连累。
            for raw in sys.stdin:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                print(">>> " + line)
                try:
                    # 必须走 execute_namespace：静默窗续租与三态审计都只在那里实现。
                    # 以前这里手写 _mute + run()，于是 batch 的行只记 attempted、
                    # 永远不记 completed——审计形同虚设。
                    for out in execute_namespace(make_parser_for(line)):
                        print(out)
                except SystemExit:
                    print("[CU_BAD_ARG] 这一行解析失败，已跳过", file=sys.stderr)
                    rc = 2
                    if not ns.continue_on_error:
                        return rc
                except (CuError, OSError) as e:
                    print(str(e) if isinstance(e, CuError) else "[CU_OS] %s" % e,
                          file=sys.stderr)
                    rc = 2
                    if not ns.continue_on_error:
                        return rc
                except Exception as e:
                    print("[CU_UNEXPECTED] %s: %s" % (type(e).__name__, e),
                          file=sys.stderr)
                    rc = 2
                    if not ns.continue_on_error:
                        return rc
        finally:
            # 无论怎么退出，都别把按下的修饰键留在系统层
            held = release_all()
            if held:
                print("（已自动松开 %s）" % "、".join(held), file=sys.stderr)
        return rc

    ap = build_parser()
    a = ap.parse_args(argv)
    if a.cmd == "shot" and getattr(a, "emit_binary", False):
        try:
            return emit_binary(a)
        except (CuError, OSError) as e:
            print(str(e) if isinstance(e, CuError) else "[CU_OS] %s" % e,
                  file=sys.stderr)
            return 2
        except Exception as e:
            print("[CU_UNEXPECTED] %s: %s" % (type(e).__name__, e), file=sys.stderr)
            return 2
    inj = a.cmd in INJECT_CMDS
    try:
        for line in execute_namespace(a):
            print(line)
    except (CuError, OSError) as e:
        print(str(e) if isinstance(e, CuError) else "[CU_OS] %s" % e,
              file=sys.stderr)
        return 2
    except Exception as e:
        print("[CU_UNEXPECTED] %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
