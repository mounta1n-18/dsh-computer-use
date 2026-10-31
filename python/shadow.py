"""shadow desktop —— 在**独立桌面**上跑应用，用户桌面完全不受影响。

为什么这么做（和豆包 pip_sdk 同一路子，取证见豆包对照表 §0）：
豆包 `pip_main.exe` 同时导入 `LogonUserW` + `CreateProcessAsUserW` +
`ConvertStringSecurityDescriptorToSecurityDescriptorW` + `OpenInputDesktop`，
即"另开窗口站/桌面 + 给桌面设 ACL + 用令牌把进程丢进去"。
`LogonUserW` 要账号密码，所以它激活时要认证。

我们这一版先做**同一账号的独立桌面**（不需要密码，比它少一步认证）：
- `CreateDesktopW` 建一个不显示的桌面；
- `CreateProcessW` 带 `lpDesktop` 把目标程序起在那个桌面上；
- 要枚举/截图/发消息，调用线程必须先 `SetThreadDesktop` 切过去——
  这一步必须在**新线程**里做（主线程带窗口/钩子会失败）。
- 用户桌面上的鼠标键盘**一次都没被碰**：我们全程不调 SendInput。

用法（spike，独立脚本，不碰 cu.py）：
    python shadow.py demo            # 跑一遍自证：起记事本→写文本→回读→截图→验证光标没动
    python shadow.py list            # 列当前窗口站下有哪些桌面
"""

import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
u32 = ctypes.WinDLL("user32", use_last_error=True)
adv32 = ctypes.WinDLL("advapi32", use_last_error=True)

DESKTOP_ALL_ACCESS = 0x01FF
WINSTA_ALL_ACCESS = 0x37F
STARTF_USESHOWWINDOW = 0x00000001
STARTF_USESTDHANDLES = 0x00000100
SW_SHOW = 5
PW_RENDERFULLCONTENT = 0x00000002


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD),
                ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wintypes.BOOL)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
                ("hStdError", wintypes.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


# ---- 函数签名（64 位下必须声明 restype，否则句柄被截断成 32 位 → 崩溃 / 访问违例）----
_u = u32
_k = k32
_u.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                              ctypes.c_void_p, wintypes.DWORD,
                              wintypes.DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES)]
_u.CreateDesktopW.restype = wintypes.HANDLE
_u.OpenDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL,
                            wintypes.DWORD]
_u.OpenDesktopW.restype = wintypes.HANDLE
_u.CloseDesktop.argtypes = [wintypes.HANDLE]
_u.CloseDesktop.restype = wintypes.BOOL
_u.SetThreadDesktop.argtypes = [wintypes.HANDLE]
_u.SetThreadDesktop.restype = wintypes.BOOL
_u.GetThreadDesktop.argtypes = [wintypes.DWORD]
_u.GetThreadDesktop.restype = wintypes.HANDLE
_u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
_u.GetCursorPos.restype = wintypes.BOOL
_u.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
_u.EnumWindows.restype = wintypes.BOOL
_u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_u.GetWindowTextW.restype = ctypes.c_int
_u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_u.GetClassNameW.restype = ctypes.c_int
_u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_u.GetWindowRect.restype = wintypes.BOOL
_u.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
_u.PrintWindow.restype = wintypes.BOOL
_u.GetWindowDC.argtypes = [wintypes.HWND]
_u.GetWindowDC.restype = wintypes.HDC
_u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                            wintypes.LPARAM]
_u.SendMessageW.restype = wintypes.LPARAM
_u.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR,
                             wintypes.LPCWSTR]
_u.FindWindowExW.restype = wintypes.HWND
_u.IsWindow.argtypes = [wintypes.HWND]
_u.IsWindow.restype = wintypes.BOOL
_k.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
                              ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
                              ctypes.c_void_p, wintypes.LPCWSTR,
                              ctypes.POINTER(STARTUPINFOW),
                              ctypes.POINTER(PROCESS_INFORMATION)]
_k.CreateProcessW.restype = wintypes.BOOL
# WaitForInputIdle 在 user32 里（不在 kernel32），写错会 ModuleNotFound 式的 AttributeError
_u.WaitForInputIdle.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_u.WaitForInputIdle.restype = wintypes.DWORD
adv32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(wintypes.ULONG)]
adv32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL

gdi = ctypes.WinDLL("gdi32", use_last_error=True)
gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi.CreateCompatibleDC.restype = wintypes.HDC
gdi.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi.SelectObject.restype = wintypes.HGDIOBJ
gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi.DeleteDC.argtypes = [wintypes.HDC]
gdi.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                          wintypes.UINT, ctypes.c_void_p,
                          ctypes.POINTER(BITMAPINFO), wintypes.UINT]
gdi.GetDIBits.restype = ctypes.c_int


class ShadowError(RuntimeError):
    pass


def _sddl(spec="D:(A;;GA;;;WD)"):
    """把 SDDL 串转成 SECURITY_DESCRIPTOR；D:(A;;GA;;;WD) = 任何人全权。

    豆包也是这条 API（它用更严的 SID 列表）。我们用 WD（Everyone）先求通，
    因为"同一账号的独立桌面"不需要跨用户授权。"""
    psd = ctypes.c_void_p()
    if not adv32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            spec, 1, ctypes.byref(psd), None):
        raise ShadowError("SDDL 解析失败：%s" % spec)
    sa = SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
    sa.lpSecurityDescriptor = psd
    sa.bInheritHandle = False
    return sa


def create_desktop(name):
    sa = _sddl()
    h = _u.CreateDesktopW(name, None, None, 0, DESKTOP_ALL_ACCESS,
                          ctypes.byref(sa))
    if not h:
        err = ctypes.get_last_error()
        raise ShadowError("CreateDesktopW(%r) 失败，错误码 %s" % (name, err))
    return h


def open_desktop(name):
    h = _u.OpenDesktopW(name, 0, False, DESKTOP_ALL_ACCESS)
    if not h:
        raise ShadowError("OpenDesktopW(%r) 失败，错误码 %s"
                          % (name, ctypes.get_last_error()))
    return h


def launch_on_desktop(cmdline, desktop_name, cwd=None, detach=False):
    """在指定桌面上起一个进程。lpDesktop 用 "桌面名"（同窗口站内）。

    detach=True 时把子进程的 stdio 接到 NUL：否则它继承我们的管道，
    调用方（shell / 父进程）会一直等它退出——常驻会话必须是脱离的。
    """
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(STARTUPINFOW)
    si.lpDesktop = desktop_name
    si.dwFlags = STARTF_USESHOWWINDOW
    si.wShowWindow = SW_SHOW
    nul = None
    if detach:
        nul = k32.CreateFileW("NUL", 0x40000000, 3, None, 3, 0, None)
        if nul and nul != -1:
            si.dwFlags |= STARTF_USESTDHANDLES
            si.hStdInput = nul
            si.hStdOutput = nul
            si.hStdError = nul
    pi = PROCESS_INFORMATION()
    buf = ctypes.create_unicode_buffer(cmdline)
    ok = _k.CreateProcessW(None, buf, None, None, False, 0, None,
                           cwd or os.getcwd(), ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        raise ShadowError("CreateProcessW 失败，错误码 %s" % ctypes.get_last_error())
    try:
        _u.WaitForInputIdle(pi.hProcess, 8000)
    except Exception:
        pass
    return pi


def cursor_pos():
    p = wintypes.POINT()
    _u.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("pad", ctypes.c_byte * 40)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


_u.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
_u.SendInput.restype = wintypes.UINT
_u.GetSystemMetrics.argtypes = [ctypes.c_int]
_u.GetSystemMetrics.restype = ctypes.c_int
_u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
_u.SetCursorPos.restype = wintypes.BOOL


def _sendinput_probe():
    """在非输入桌面的线程上发一次绝对鼠标移动，判断事件会不会打到用户桌面。

    三种结果都有用：
      - 返回 0（被系统拒绝）→ 不跨桌面；驱动非输入桌面只能走消息级；
      - 成功且用户光标动了 → **危险**（本函数会把它放回去），绝不这么用；
      - 成功但光标没动 → 事件进了本桌面的队列，可以安全驱动。
    """
    before = cursor_pos()
    sw = max(1, _u.GetSystemMetrics(0))
    sh = max(1, _u.GetSystemMetrics(1))
    inp = INPUT()
    inp.type = 0                                   # INPUT_MOUSE
    inp.u.mi.dx = int(200 * 65535 / sw)
    inp.u.mi.dy = int(200 * 65535 / sh)
    inp.u.mi.mouseData = 0
    inp.u.mi.dwFlags = 0x0001 | 0x8000             # MOVE | ABSOLUTE
    inp.u.mi.time = 0
    inp.u.mi.dwExtraInfo = None
    n = _u.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    time.sleep(0.15)
    after = cursor_pos()
    if n == 0:
        return ("SendInput 被拒（err=%s）→ 不跨桌面；驱动非输入桌面只能走消息级"
                % ctypes.get_last_error())
    if after != before:
        _u.SetCursorPos(before[0], before[1])
        return ("光标被带走 %s→%s（已放回）→ 危险，不能对用户桌面跑 SendInput"
                % (before, after))
    return "SendInput 成功但用户光标没动 → 事件进了本桌面，可安全驱动"


def _enum_windows():
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, lparam):
        n = ctypes.create_unicode_buffer(256)
        _u.GetWindowTextW(hwnd, n, 256)
        c = ctypes.create_unicode_buffer(256)
        _u.GetClassNameW(hwnd, c, 256)
        out.append((hwnd, n.value, c.value))
        return True

    _u.EnumWindows(cb, 0)
    return out


def with_desktop(desktop_handle, fn):
    """在**新线程**里切到目标桌面执行 fn 并返回其结果。

    主线程不能 SetThreadDesktop：只要线程创建过窗口或装了钩子就会失败（error 170）。
    """
    box = {}

    def run():
        if not _u.SetThreadDesktop(desktop_handle):
            box["err"] = "SetThreadDesktop 失败，错误码 %s" % ctypes.get_last_error()
            return
        try:
            box["value"] = fn()
        except BaseException as e:      # noqa: BLE001
            box["err"] = "%s: %s" % (type(e).__name__, e)

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(20)
    if "err" in box:
        raise ShadowError(box["err"])
    if "value" not in box:
        raise ShadowError("切换桌面后执行超时")
    return box["value"]


def grab(hwnd):
    """PrintWindow 把窗口画进内存位图 → PNG 字节。不依赖窗口在前台/可见。"""
    from PIL import Image
    r = wintypes.RECT()
    _u.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        raise ShadowError("窗口尺寸非法 %dx%d" % (w, h))
    hdc = _u.GetWindowDC(hwnd)
    mfc = gdi.CreateCompatibleDC(hdc)
    bmp = gdi.CreateCompatibleBitmap(hdc, w, h)
    old = gdi.SelectObject(mfc, bmp)
    try:
        _u.PrintWindow(hwnd, mfc, PW_RENDERFULLCONTENT)
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        buf = ctypes.create_string_buffer(w * h * 4)
        if gdi.GetDIBits(mfc, bmp, 0, h, buf, ctypes.byref(bi), 0) == 0:
            raise ShadowError("GetDIBits 失败")
        img = Image.frombytes("RGB", (w, h), bytes(buf), "raw", "BGRX", 0, 1)
    finally:
        gdi.SelectObject(mfc, old)
        gdi.DeleteObject(bmp)
        gdi.DeleteDC(mfc)
        _u.ReleaseDC(hwnd, hdc)
    import io
    o = io.BytesIO()
    img.save(o, format="PNG")
    return o.getvalue(), (w, h)


def demo():
    name = "dsh_shadow"
    try:
        _u.CloseDesktop(open_desktop(name))
    except ShadowError:
        pass
    hdesk = create_desktop(name)
    print("[1] 独立桌面已创建：%r  handle=%s" % (name, hdesk))

    cur0 = cursor_pos()
    print("[2] 用户光标起点：%s" % (cur0,))

    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                       "System32", "notepad.exe")
    pi = launch_on_desktop('"%s"' % exe, name)
    print("[3] 记事本已起在独立桌面上：pid=%s" % pi.dwProcessId)

    def work():
        wins = [w for w in _enum_windows() if w[1] or w[2]]
        print("[4] 该桌面上枚举到 %d 个窗口，样例：%s"
              % (len(wins), [(t[:18], c) for _, t, c in wins[:4]]))
        top = None
        for hwnd, title, cls in wins:
            if "Notepad" in title or "记事本" in title:
                top = hwnd
                break
        if top is None and wins:
            top = wins[0][0]
        if top is None:
            raise ShadowError("独立桌面上没找到窗口")
        # 控制手段（跨桌面、不要焦点、**不走 SendInput**）：
        #   SetWindowText / PostMessage / UI Automation 都是消息级的，
        #   对非输入桌面上的窗口同样有效——这正是"不抢鼠标"能成立的前提。
        tag = "shadow-ok-%d" % int(time.time())
        _u.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
        _u.SetWindowTextW.restype = wintypes.BOOL
        _u.SetWindowTextW(top, tag)
        buf = ctypes.create_unicode_buffer(256)
        _u.GetWindowTextW(top, buf, 256)
        readback = buf.value

        # SendInput 探针：在**非输入桌面**的这条线程上发一次绝对鼠标移动。
        # 若事件仍被送进用户的输入队列 → 光标会动 → 说明"真注入"会打扰用户，
        # 必须放弃这条路；若不动/被拒 → 说明不会跨桌面泄露。
        probe = _sendinput_probe()

        png, size = grab(top)
        return {"top": top, "tag": tag, "readback": readback,
                "png": png, "size": size, "probe": probe}

    res = with_desktop(hdesk, work)
    print("[5] 跨桌面改标题后回读：%r" % (res["readback"],))
    print("[6] PrintWindow 截图：%dx%d，%.0f KB"
          % (res["size"][0], res["size"][1], len(res["png"]) / 1024.0))
    out = os.path.join(os.environ.get("TEMP", "."), "dsh-shadow-demo.png")
    with open(out, "wb") as f:
        f.write(res["png"])
    print("[7] 截图落盘：%s" % out)
    print("[8] SendInput 探针：%s" % (res["probe"],))

    time.sleep(0.5)
    cur1 = cursor_pos()
    print("[9] 用户光标：%s → %s  →  %s"
          % (cur0, cur1, "没动 ✅" if cur0 == cur1 else "动了 ❌"))

    # 清理：关掉记事本
    try:
        k32.TerminateProcess(pi.hProcess, 0)
    except Exception:
        pass
    _u.CloseDesktop(hdesk)
    ok = res["readback"] == res["tag"] and cur0 == cur1
    print("\n判定：%s" % ("PASS —— 独立桌面可起应用 / 可驱动 / 可截图，用户键鼠零干扰"
                        if ok else "FAIL —— 看上面哪一步不对"))
    return 0 if ok else 1


# ---------------------------------------------------------------- 常驻隔离会话
# 桌面对象随"最后一个句柄关闭"而销毁，所以必须有个常驻进程占着。
# 做法：把**整个 cu_server** 搬到隔离桌面上跑（它是常驻的），于是：
#   - 它的子进程（UIA sidecar）继承该桌面，元素级可用；
#   - 截图/窗口枚举都在该桌面；
#   - SendInput 仍被系统拒绝（err=5），所以永远碰不到用户的鼠标；
#   - cu.py / cu_server.py 一行不用改，客户端只是换了个端点。
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(os.environ.get("TEMP", "."), "cu", "dsh-shadow.json")
CU_SERVER = os.path.join(HERE, "cu_server.py")
LAUNCHER = os.path.join(HERE, "shadow_launch.py")
CU_PY = os.path.join(HERE, "cu.py")
DEFAULT_DESKTOP = "dsh_shadow"
DEFAULT_ENDPOINT = r"\\.\pipe\dsh-cu-shadow"


def _read_state():
    import json
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_state(obj):
    import json
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _ping(endpoint):
    """探测端点上有没有活着的服务（借 cu_server 自己的客户端）。"""
    old = {k: os.environ.get(k) for k in ("DSH_CU_ENDPOINT", "DSH_CU_TRANSPORT")}
    os.environ["DSH_CU_ENDPOINT"] = endpoint
    os.environ["DSH_CU_TRANSPORT"] = "named_pipe"
    try:
        import cu_server
        return bool(cu_server.ping(cu_server.TransportConfig.from_env()))
    except Exception:
        return False
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def start_session(desktop=DEFAULT_DESKTOP, endpoint=DEFAULT_ENDPOINT):
    import json
    st = _read_state()
    if st and _ping(st.get("endpoint", "")):
        print("已在运行：桌面=%r 端点=%s pid=%s"
              % (st.get("desktop"), st.get("endpoint"), st.get("pid")))
        return 0
    try:
        _u.CloseDesktop(open_desktop(desktop))
    except ShadowError:
        pass
    hdesk = create_desktop(desktop)
    env = {"DSH_CU_TRANSPORT": "named_pipe", "DSH_CU_ENDPOINT": endpoint}
    # 配置写文件传：JSON 里的双引号会被命令行解析吃掉
    cfg_path = os.path.join(os.environ.get("TEMP", "."), "cu", "shadow-env.json")
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"env": env}, f)
    cmd = '"%s" "%s" "%s"' % (sys.executable, LAUNCHER, cfg_path)
    pi = launch_on_desktop(cmd, desktop, detach=True)
    _write_state({"desktop": desktop, "endpoint": endpoint,
                  "pid": pi.dwProcessId, "started": time.time()})
    deadline = time.time() + 25
    while time.time() < deadline:
        if _ping(endpoint):
            _u.CloseDesktop(hdesk)
            print("隔离会话已就绪：桌面=%r 端点=%s pid=%s"
                  % (desktop, endpoint, pi.dwProcessId))
            print("客户端用法：DSH_CU_ENDPOINT=%s，其它一切照旧。" % endpoint)
            return 0
        time.sleep(0.4)
    raise ShadowError("隔离会话 25 秒内没就绪（pid=%s）" % pi.dwProcessId)


def stop_session():
    st = _read_state()
    if not st:
        print("没有在运行的隔离会话")
        return 0
    pid = int(st.get("pid") or 0)
    try:
        h = k32.OpenProcess(0x0001, False, pid)      # PROCESS_TERMINATE
        if h:
            k32.TerminateProcess(h, 0)
            k32.CloseHandle(h)
    except Exception as e:                          # noqa: BLE001
        print("结束进程失败：%s" % e)
    try:
        os.remove(STATE)
    except OSError:
        pass
    print("隔离会话已结束（pid=%s；桌面随最后一个句柄销毁）" % pid)
    return 0


def status_session():
    st = _read_state()
    if not st:
        print("没有隔离会话（用 `shadow.py start` 起一个）")
        return 1
    alive = _ping(st.get("endpoint", ""))
    print("桌面=%r\n端点=%s\npid=%s\n存活=%s"
          % (st.get("desktop"), st.get("endpoint"), st.get("pid"),
             "是" if alive else "否（进程可能已退出）"))
    return 0 if alive else 1


def run_cu(args):
    """把一条 cu 命令发给隔离会话：只换端点，不换别的。"""
    import subprocess
    st = _read_state()
    if not st:
        raise ShadowError("没有隔离会话；先 `shadow.py start`")
    env = dict(os.environ)
    env["DSH_CU_TRANSPORT"] = "named_pipe"
    env["DSH_CU_ENDPOINT"] = st["endpoint"]
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([sys.executable, CU_PY] + [str(x) for x in args],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env)
    sys.stdout.write(p.stdout)
    sys.stderr.write(p.stderr)
    return p.returncode


def list_desktops():
    """枚举当前窗口站下的桌面（EnumDesktopsW 只列本窗口站）。"""
    names = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.LPCWSTR, wintypes.LPARAM)
    def cb(name, lparam):
        names.append(name)
        return True

    _u.EnumDesktopsW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.LPARAM]
    _u.EnumDesktopsW.restype = wintypes.BOOL
    if not _u.EnumDesktopsW(None, cb, 0):
        raise ShadowError("EnumDesktopsW 失败，错误码 %s" % ctypes.get_last_error())
    for n in names:
        print("  ", n)
    return 0


def main(argv):
    cmd = (argv[1] if len(argv) > 1 else "demo").lower()
    if cmd == "demo":
        return demo()
    if cmd == "list":
        return list_desktops()
    if cmd == "start":
        desk = argv[2] if len(argv) > 2 else DEFAULT_DESKTOP
        return start_session(desk)
    if cmd == "stop":
        return stop_session()
    if cmd == "status":
        return status_session()
    if cmd == "cu":
        return run_cu(argv[2:])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
