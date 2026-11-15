#!/usr/bin/env python
"""overlay.py — cu 的「存在感层」独立进程。

协议：stdin 每行一个 JSON 命令，stdout 每行一个 JSON 事件。

命令
  {"cmd":"state","mode":"active|paused|consent|emergency|idle",
   "owner":"lab","session":"4f2a9c","cwd":"dsh-computer-use",
   "text":"...","sub":"...","seconds":30,"buttons":true}
  {"cmd":"banner","text":...,"sub":...,"seconds":N,"buttons":B,"tone":"consent"}
  {"cmd":"hide_banner"}
  {"cmd":"halo","on":true}
  {"cmd":"border","on":true,"color":"#4D6BFE","alpha":0.5}
  {"cmd":"logo","on":true}
  {"cmd":"dialog","on":true}
  {"cmd":"flash","color":"#FF3B30","times":2}
  {"cmd":"off"}                  全部淡出隐藏（交还/急停后调用）
  {"cmd":"quit"}

事件
  {"event":"ready","screen":[x,y,w,h]}
  {"event":"agree"} / {"event":"deny"}
  {"event":"message","text":"..."}
  {"event":"logo_click"}

实现要点（踩过的坑都在这里）：
  * 渐变的半透明只有 UpdateLayeredWindow 能画（Tk 的 -alpha 只能整窗一个值、边缘是硬的），
    所以边框/光晕/横幅/logo 都是 WS_EX_LAYERED 窗口 + PIL 逐像素 alpha。
  * Tk 的 -transparentcolor / geometry 会被 Tk 自己覆盖，故装饰层不用 Tk；
    只有需要文字输入的对话框用 Tk。
  * UpdateLayeredWindow 要求预乘 alpha，颜色要先乘 alpha/255。
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import re
import sys
import threading
import time
import traceback
from ctypes import wintypes as wt

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ---------------------------------------------------------------- win32 基元

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1
WM_DESTROY = 0x0002
WM_LBUTTONUP = 0x0202
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONDBLCLK = 0x0203
IDC_HAND = 32649
IDC_ARROW = 32512
WM_SETCURSOR = 0x0020
WM_NCHITTEST = 0x0084
HTTRANSPARENT = -1
SPI_GETWORKAREA = 0x0030
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wt.HWND), ("message", wt.UINT), ("wParam", wt.WPARAM),
                ("lParam", wt.LPARAM), ("time", wt.DWORD), ("pt", wt.POINT)]


user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU,
                                   wt.HINSTANCE, wt.LPVOID]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.UpdateLayeredWindow.restype = wt.BOOL
user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT),
                                       ctypes.POINTER(wt.SIZE), wt.HDC, ctypes.POINTER(wt.POINT),
                                       wt.DWORD, ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
user32.GetDC.restype = wt.HDC
user32.GetDC.argtypes = [wt.HWND]
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.PeekMessageW.argtypes = [ctypes.POINTER(MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
user32.LoadCursorW.restype = wt.HANDLE
user32.LoadCursorW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
user32.SetCursor.restype = wt.HANDLE
user32.SetCursor.argtypes = [wt.HANDLE]
user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wt.UINT]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.POINTER(BITMAPINFO), wt.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]
gdi32.CreateRoundRectRgn.restype = wt.HRGN
gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
user32.SetWindowRgn.argtypes = [wt.HWND, wt.HRGN, wt.BOOL]
user32.GetAncestor.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetParent.restype = wt.HWND
user32.GetParent.argtypes = [wt.HWND]


def _screen_rect():
    u = user32
    return (u.GetSystemMetrics(SM_XVIRTUALSCREEN), u.GetSystemMetrics(SM_YVIRTUALSCREEN),
            u.GetSystemMetrics(SM_CXVIRTUALSCREEN), u.GetSystemMetrics(SM_CYVIRTUALSCREEN))


def _work_area():
    r = wt.RECT()
    if user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0):
        return r.left, r.top, r.right - r.left, r.bottom - r.top
    return _screen_rect()


def _cursor():
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def _dpi_scale():
    try:
        return user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


def _scale_at(x: int, y: int) -> float:
    """某一点所在显示器的缩放。多屏不同 DPI 时，光标在 4K 上、鲸鱼在 1080p 上，
    两边要各按各的比例画，不能全局共用一个 scale。"""
    try:
        mon = user32.MonitorFromPoint(wt.POINT(int(x), int(y)), 2)  # NEAREST
        dx, dy = ctypes.c_uint(96), ctypes.c_uint(96)
        ctypes.windll.shcore.GetDpiForMonitor(mon, 0, ctypes.byref(dx), ctypes.byref(dy))
        return max(1.0, dx.value / 96.0)
    except Exception:
        return _dpi_scale()


def _premultiply(img: Image.Image) -> bytes:
    """RGBA -> 预乘 BGRA 字节串（UpdateLayeredWindow 要求）。"""
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    px = img.load()
    w, h = img.size
    out = bytearray(w * h * 4)
    i = 0
    for y in range(h):
        for x in range(w):
            pr, pg, pb, pa = px[x, y]
            out[i] = (pb * pa) // 255
            out[i + 1] = (pg * pa) // 255
            out[i + 2] = (pr * pa) // 255
            out[i + 3] = pa
            i += 4
    return bytes(out)


class LayerWindow:
    """一个 WS_EX_LAYERED 窗口，内容是一张 RGBA 图。"""

    _class_atom = None
    _class_name = "DshCuOverlay"
    _lock = threading.Lock()
    _keepalive = []      # 注册时用到的 WNDCLASSW / WNDPROC 必须一直活着，否则窗口创建即崩溃

    def __init__(self, name, click_through=True, on_click=None):
        self.name = name
        self.on_click = on_click
        self.hwnd = None
        self._memdc = None
        self._bmp = None
        self._bits = None
        self._old = None
        self._size = (0, 0)
        self._pos = (0, 0)
        self._visible = False
        self._proc = WNDPROC(self._wndproc)
        self._ensure_class()
        ex = WS_EX_LAYERED | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        if click_through:
            ex |= WS_EX_TRANSPARENT
        self.hwnd = user32.CreateWindowExW(
            ex, self._class_name, name, WS_POPUP, 0, 0, 1, 1, None, None, None, None)
        if not self.hwnd:
            raise OSError("CreateWindowExW failed: %s" % ctypes.get_last_error())
        _WINDOWS[self.hwnd] = self
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 1, 1,
                            SWP_NOACTIVATE | SWP_SHOWWINDOW)

    @classmethod
    def _ensure_class(cls):
        with cls._lock:
            if cls._class_atom:
                return
            wc = WNDCLASSW()
            wc.style = 0x0002 | 0x0001
            proc = WNDPROC(_global_wndproc)
            wc.lpfnWndProc = proc
            wc.hInstance = None
            wc.lpszClassName = "DshCuOverlay"
            # 类不给 hCursor 的话，鼠标停在窗口上会显示"后台启动"光标（箭头+转圈），像卡死
            wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
            atom = user32.RegisterClassW(ctypes.byref(wc))
            cls._class_atom = atom or True
            cls._class_name = "DshCuOverlay"
            cls._keepalive.append((wc, proc))

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_NCHITTEST and self.on_click:
            return 1                     # HTCLIENT：让鼠标落在这个窗口上，不要穿透
        if msg in (WM_LBUTTONDOWN, WM_LBUTTONDBLCLK) and self.on_click:
            return 0
        if msg == WM_LBUTTONUP and self.on_click:
            try:
                x = ctypes.c_short(lparam & 0xFFFF).value
                y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                self.on_click(self, x, y)
            except Exception:
                traceback.print_exc()
            return 0
        if msg == WM_SETCURSOR and self.on_click:
            user32.SetCursor(user32.LoadCursorW(None, ctypes.c_void_p(IDC_HAND)))
            return 1
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def present(self, img: Image.Image, x: int, y: int):
        # 同一张图 + 同一位置 + 已经显示着：不需要重新上传（每帧重画 52x1600 的渐变太贵）
        if (img is getattr(self, "_last_img", None) and (x, y) == self._pos
                and self._visible):
            return
        # 图没变、只是挪位置（光标光晕每帧都在动）：分层窗口的内容不用重传，
        # SetWindowPos 就够。这样 240Hz 跟手也不会把 CPU 烧掉。
        if img is getattr(self, "_last_img", None) and self._size == img.size:
            self._pos = (x, y)
            self._visible = True
            # sticky_zorder 的窗口（光标光晕）只在首次显示时置顶；之后挪位置不动 z 序。
            # 否则它每帧 SetWindowPos(HWND_TOPMOST) 会把自己抬到鲸鱼上面，下一帧鲸鱼
            # 动画又把自己抬起去 —— 两个 topmost 窗口互相顶，表现就是"停在鲸鱼上时闪烁"。
            if getattr(self, "sticky_zorder", False):
                user32.SetWindowPos(self.hwnd, None, x, y, 0, 0,
                                    SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOZORDER | SWP_SHOWWINDOW)
            else:
                user32.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, 0, 0,
                                    SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
            return
        self._last_img = img
        w, h = img.size
        if (w, h) != self._size:
            self._size = (w, h)
            self._release_surface()
            screen_dc = user32.GetDC(None)
            self._memdc = gdi32.CreateCompatibleDC(screen_dc)
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = w
            bmi.bmiHeader.biHeight = -h
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0  # BI_RGB
            bits = ctypes.c_void_p()
            self._bmp = gdi32.CreateDIBSection(
                screen_dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
            self._bits = bits
            self._old = gdi32.SelectObject(self._memdc, self._bmp)
            user32.ReleaseDC(None, screen_dc)
        data = _premultiply(img)
        ctypes.memmove(self._bits, data, len(data))
        screen_dc = user32.GetDC(None)
        pt_dst = wt.POINT(x, y)
        size = wt.SIZE(w, h)
        pt_src = wt.POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(self.hwnd, screen_dc, ctypes.byref(pt_dst),
                                   ctypes.byref(size), self._memdc, ctypes.byref(pt_src),
                                   0, ctypes.byref(blend), ULW_ALPHA)
        user32.ReleaseDC(None, screen_dc)
        # 这里必须无条件 SetWindowPos(SWP_SHOWWINDOW)：窗口初建是 1x1，hide() 会把它藏起来，
        # 而左/上两条边的坐标正好是 (0,0)（与初始 _pos 相同），只按"位置变了才摆"判断
        # 会让它们永远藏在那儿 —— 表现就是"只有右边和下边有边框"。
        self._pos = (x, y)
        self._visible = True
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, 0, 0,
                            SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)

    def move(self, x: int, y: int):
        if (x, y) == self._pos and self._visible:
            return
        self._pos = (x, y)
        self._visible = True
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, 0, 0,
                            SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)

    def hide(self):
        if not self._visible:
            return
        self._visible = False
        user32.ShowWindow(self.hwnd, 0)

    def _release_surface(self):
        try:
            if self._old:
                gdi32.SelectObject(self._memdc, self._old)
                self._old = None
            if self._bmp:
                gdi32.DeleteObject(self._bmp)
                self._bmp = None
            if self._memdc:
                gdi32.DeleteDC(self._memdc)
                self._memdc = None
            self._bits = None
        except Exception:
            pass


def _global_wndproc(hwnd, msg, wparam, lparam):
    """所有窗口共用一个窗口过程，按 hwnd 找到实例再分发。

    2026-11-14 的坑：类注册时把 `_global_wndproc` 填进 lpfnWndProc 之后，
    每个实例的 `self._wndproc` 就再也没被调用过 —— 于是 WM_LBUTTONUP 全丢，
    横幅上的"同意"点不动；WM_SETCURSOR 也没人处理，类又没设 hCursor，
    Windows 就用"后台启动"光标（箭头+转圈）顶上，看起来像卡死。
    """
    win = _WINDOWS.get(hwnd)
    if win is not None:
        try:
            return win._wndproc(hwnd, msg, wparam, lparam)
        except Exception:
            traceback.print_exc()
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


_WINDOWS = {}


# ---------------------------------------------------------------- 视觉素材

BRAND_BLUE = (77, 107, 254)     # DSH 品牌蓝：界面里唯一的强调色
RED = (255, 59, 48)
GREY = (150, 155, 165)
LOGO_BLUE = (77, 107, 254)      # DeepSeek 品牌蓝
LOGO_INK = (26, 29, 36)         # 官方鲸鱼是黑的，不是蓝的
LOGO_LABEL = {"active": "控制中", "paused": "已暂停"}

# ---- 视觉规范：对齐 Windows 11 / DSH 现有界面
SURFACE = (255, 255, 255)       # 卡片/浮层：白
PAGE = (245, 246, 248)          # 页面底色
SURFACE_EDGE = (229, 231, 235)
DIALOG_EDGE = (203, 208, 217)   # 对话框边框比卡片描边深一档，否则贴着同色背景看不出边界
INK = (31, 35, 41)              # 主文字
SUB_INK = (100, 106, 115)       # 次文字
MUTED_INK = (152, 158, 168)     # 弱文字/占位
ACCENT = (77, 107, 254)         # 主按钮：DSH 品牌蓝
ACCENT_INK = (255, 255, 255)    # 蓝底白字
ACCENT_HOVER = (58, 90, 240)
STATUS_ACTIVE = (77, 107, 254)  # 控制中 = 品牌蓝
STATUS_PAUSED = (146, 152, 164) # 状态条：已暂停
DANGER = (216, 74, 62)          # 状态条/错误：急停
PAUSED_INK = (146, 152, 164)
HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
_cache_font = {}


def _font(px: int, bold: bool = False):
    key = (px, bold)
    if key in _cache_font:
        return _cache_font[key]
    for name in (("msyhbd.ttc" if bold else "msyh.ttc"), "msyh.ttf", "simhei.ttf"):
        path = os.path.join(FONT_DIR, name)
        if os.path.exists(path):
            try:
                idx = 0
                f = ImageFont.truetype(path, px, index=idx)
                _cache_font[key] = f
                return f
            except Exception:
                continue
    f = ImageFont.load_default()
    _cache_font[key] = f
    return f


def _text_w(font, text):
    try:
        return int(font.getlength(text))
    except Exception:
        return font.getbbox(text)[2]


def _wrap(font, text, max_w):
    """按宽度折行；优先在 " · "、空格、连字符处断，实在放不下才逐字断。

    会话 id 这种长串如果逐字断，会从 UUID 中间劈开（dsh-com / puter-use），
    所以先按分隔符切段，再逐段放。
    """
    lines = []
    for para in text.split("\n"):
        cur = ""
        for chunk in re.split(r"(\s+|·|(?<=-))", para):
            if not chunk:
                continue
            if _text_w(font, cur + chunk) <= max_w:
                cur += chunk
                continue
            if cur.strip():
                lines.append(cur.rstrip())
            cur = ""
            rest = chunk
            while rest and _text_w(font, rest) > max_w:
                cut = len(rest)
                while cut > 1 and _text_w(font, rest[:cut]) > max_w:
                    cut -= 1
                lines.append(rest[:cut])
                rest = rest[cut:]
            cur = rest
        if cur.strip():
            lines.append(cur.rstrip())
    return lines or [""]


def halo_image(d: int = 44, color=None, strength: float = 0.55):
    """指针下方那颗半透明实心圆：中心浓、边缘柔和渐隐。"""
    if color is None:
        color = STATUS_ACTIVE
    ss = 3
    big = Image.new("RGBA", (d * ss, d * ss), (0, 0, 0, 0))
    px = big.load()
    cx = cy = (d * ss - 1) / 2.0
    r = (d * ss) / 2.0
    core = r * 0.52
    for y in range(d * ss):
        for x in range(d * ss):
            dist = math.hypot(x - cx, y - cy)
            if dist >= r:
                continue
            if dist <= core:
                t = strength
            else:
                k = (dist - core) / max(1e-6, (r - core))
                t = strength * (1.0 - k) ** 2.2
            if t <= 0.004:
                continue
            px[x, y] = (color[0], color[1], color[2], int(255 * t))
    return big.resize((d, d), Image.LANCZOS)


def edge_image(size, color, peak: float, horizontal: bool, from_start: bool):
    """一条向内柔和渐隐的边。

    horizontal=True 表示渐变沿 x 走（左右边），否则沿 y 走（上下边）。
    用一维 alpha 斜坡拉伸实现，避免逐像素 Python 循环（2560x78 也只要毫秒级）。
    """
    w, h = size
    span = w if horizontal else h
    alpha = bytearray(span)
    for i in range(span):
        d = i if from_start else span - 1 - i
        k = d / max(1, span - 1)
        a = peak * (1.0 - k) ** 1.7
        alpha[i] = max(0, min(255, int(255 * a)))
    ramp = Image.frombytes("L", (span, 1) if horizontal else (1, span), bytes(alpha))
    if horizontal:
        alpha_img = ramp.resize((span, h), Image.NEAREST)
    else:
        alpha_img = ramp.resize((w, span), Image.NEAREST)
    img = Image.new("RGBA", (w, h), (color[0], color[1], color[2], 0))
    img.putalpha(alpha_img)
    return img


def _round_mask(size, radius):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return m


def _rgb(s):
    """"#RRGGBB" → (r, g, b)。命令里传的颜色都是这个格式。"""
    s = str(s).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except (ValueError, IndexError):
        return STATUS_ACTIVE


def _darken(color, k: float):
    """按比例调暗，给浅色底上的同色文字用（ImageDraw 不做 alpha 混合）。"""
    return (max(0, int(color[0] * k)), max(0, int(color[1] * k)), max(0, int(color[2] * k)))


def panel_image(content: Image.Image, radius: int, shadow: int = 18):
    """给内容图加圆角、1px 描边和柔和投影，返回更大的 RGBA 图。

    画布要按投影的模糊半径留够余量（约 2σ + 位移），否则投影在边缘被硬切，
    浅色桌面上会显出一条淡淡的直边。
    """
    w, h = content.size
    blur = max(2.0, shadow * 0.7)
    pad = int(blur * 2.2) + 8
    body = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    body.paste(content, (0, 0))
    body.putalpha(_round_mask((w, h), radius))

    canvas = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    sh = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle(
        (pad, pad + 3, pad + w - 1, pad + h + 3), radius=radius, fill=(0, 0, 0, 120))
    sh = sh.filter(ImageFilter.GaussianBlur(blur))
    canvas.alpha_composite(sh)
    canvas.alpha_composite(body, (pad, pad))
    return canvas


def banner_image(title, sub, note, seconds, buttons, tone, scale):
    """横幅：标题 / 会话来源 / 说明 / 倒计时 / 按钮。返回 (图, 命中框, 边距)。"""
    s = scale
    f_title = _font(int(20 * s), True)
    f_sub = _font(int(13 * s))
    f_note = _font(int(12 * s))
    f_pill = _font(int(11 * s), True)
    pad_x = int(24 * s)
    pad_y = int(18 * s)
    # 宽度跟着内容走：固定 430pt 时 "Ctrl+Alt+Shift+Backspace" 这种长串会在词中间
    # 被硬折成两行，看着像截断。上限 600pt，再长才换行。
    natural = _text_w(f_title, title)
    for t, f in ((sub, f_sub), (note, f_note)):
        for seg in (t or "").split(" · "):
            natural = max(natural, _text_w(f, seg))
    text_w = max(int(380 * s), min(int(680 * s), natural + int(4 * s)))

    color, pill_text = {
        "consent": (STATUS_ACTIVE, "征询同意"),
        "active": (STATUS_ACTIVE, "控制中"),
        "paused": (STATUS_PAUSED, "已暂停"),
        "emergency": (DANGER, "已手动关闭"),
    }.get(tone, (STATUS_ACTIVE, "控制中"))

    # 右上角状态胶囊；标题第一行要给它让位，否则长 holder 名会顶到边。
    pill_h = int(24 * s)
    pill_w = _text_w(f_pill, pill_text) + int(22 * s)
    gap = int(12 * s)
    title_w = text_w
    title_lines = _wrap(f_title, title, title_w)
    line_h_title = int(30 * s)

    sub_lines = _wrap(f_sub, sub, text_w) if sub else []
    note_lines = _wrap(f_note, note, text_w) if note else []
    line_h = int(20 * s)

    btn_h = int(38 * s) if buttons else 0
    content_h = pad_y + line_h_title * len(title_lines) + (line_h * len(sub_lines)) + int(8 * s) + \
        (line_h * len(note_lines)) + (int(14 * s) + btn_h + pad_y if buttons else pad_y)
    w = text_w + pad_x * 2
    content_h = max(content_h, int(96 * s))

    content = Image.new("RGBA", (w, content_h), SURFACE + (250,))
    d = ImageDraw.Draw(content)
    d.rounded_rectangle((0, 0, w - 1, content_h - 1), radius=int(12 * s),
                        fill=SURFACE + (255,),
                        outline=SURFACE_EDGE + (255,), width=max(1, int(s)))

    # 状态不另做胶囊：卡片保持干净，状态由按钮颜色和右下角鲸鱼的状态字表达。

    y = pad_y
    for ln in title_lines:
        d.text((pad_x, y), ln, font=f_title, fill=INK + (255,))
        y += line_h_title
    for ln in sub_lines:
        d.text((pad_x, y), ln, font=f_sub, fill=SUB_INK + (255,))
        y += line_h
    y += int(8 * s)
    for ln in note_lines:
        d.text((pad_x, y), ln, font=f_note, fill=MUTED_INK + (255,))
        y += line_h

    hits = {}
    if buttons:
        by = content_h - pad_y - btn_h
        bw_ok, bw_no = int(118 * s), int(84 * s)
        gap_btn = int(12 * s)
        x_no = w - pad_x - bw_no
        x_ok = x_no - gap_btn - bw_ok
        d.rounded_rectangle((x_ok, by, x_ok + bw_ok, by + btn_h), radius=int(8 * s),
                            fill=ACCENT + (255,))
        d.text((x_ok + bw_ok / 2, by + btn_h / 2), "同意", font=_font(int(13.5 * s), True),
               fill=ACCENT_INK + (255,), anchor="mm")
        d.rounded_rectangle((x_no, by, x_no + bw_no, by + btn_h), radius=int(8 * s),
                            fill=SURFACE + (255,), outline=SURFACE_EDGE + (255,),
                            width=max(1, int(1.2 * s)))
        d.text((x_no + bw_no / 2, by + btn_h / 2), "拒绝", font=_font(int(13.5 * s)),
               fill=SUB_INK + (255,), anchor="mm")
        hits["agree"] = (x_ok, by, bw_ok, btn_h)
        hits["deny"] = (x_no, by, bw_no, btn_h)

    img = panel_image(content, int(12 * s))
    return img, hits, int(18 * s)


def _whale_source():
    """真正的 DeepSeek 鲸鱼：直接用程序自带的 icon.png（深色鲸鱼、白底圆角方）。

    之前手画的"椭圆 + 两片鳍"被用户一眼认出是条鱼，别再手画了。
    """
    global _WHALE
    if _WHALE is not None:
        return _WHALE
    cand = [os.path.join(HERE, "..", "assets", "whale.png"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs",
                         "DeepSeek Harness", "resources", "icon.png")]
    for p in cand:
        try:
            src = Image.open(p).convert("RGBA")
        except (OSError, ValueError):
            continue
        # 白底黑鲸 → 抠出鲸鱼。先摊平到白底（图标四角是透明的，直接取亮度会变成黑），
        # 再用硬阈值；线性抠图会在鲸鱼外面留一圈方形边。
        flat = Image.alpha_composite(Image.new("RGBA", src.size, (255, 255, 255, 255)), src)
        g = flat.convert("L")
        mask = g.point(lambda v: 255 if v < 120 else 0)
        bbox = mask.getbbox()
        if bbox:
            src = src.crop(bbox)
            mask = mask.crop(bbox)
        _WHALE = (src, mask)
        return _WHALE
    return None


_WHALE = None


def _logo_core(d: int, color, state: str = "active"):
    """logo 静态部分：白底圆盘 + 官方鲸鱼（黑），下方一行状态字。

    状态字是用户要求的"一眼看出它在干活还是停了"：控制中=琥珀，已暂停=灰蓝。
    返回 (图, 盘心, 总高)；图比圆盘高一块，多出来的是给状态字留的。
    """
    ss = 3
    size = d * ss
    # 状态字区：字形高度实测 + 上下留白。之前 0.30d 固定高，d=93 时字底贴着画布边缘，
    # 实拍里就是"文字被截断"。
    text = LOGO_LABEL.get(state, LOGO_LABEL["active"])
    try:
        font = ImageFont.truetype(os.path.join(FONT_DIR, "msyhbd.ttc"), max(11, int(d * 0.20)))
    except OSError:
        font = ImageFont.load_default()
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    tb = probe.textbbox((0, 0), text, font=font)
    glyph_h = tb[3] - tb[1]
    label_h = glyph_h + int(d * 0.22)
    total_h = d + label_h
    c = size / 2.0
    body_r = c - 5.0 * ss

    base = Image.new("RGBA", (d, total_h), (0, 0, 0, 0))
    disc = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((c - body_r, c - body_r, c + body_r, c + body_r), fill=255)
    disc.paste(Image.new("RGBA", (size, size), SURFACE + (246,)), (0, 0), mask)
    ImageDraw.Draw(disc).ellipse(
        (c - body_r, c - body_r, c + body_r, c + body_r),
        outline=SURFACE_EDGE + (255,), width=max(1, int(1.2 * ss)))
    disc = disc.resize((d, d), Image.LANCZOS)
    base.alpha_composite(disc, (0, 0))

    whale = _whale_source()
    if whale is not None:
        src, wmask = whale
        box = int(d * 0.72)
        sw, sh = src.size
        k = min(box / sw, box / sh)
        nw, nh = max(1, int(sw * k)), max(1, int(sh * k))
        wm = wmask.resize((nw, nh), Image.LANCZOS)
        ink = Image.new("RGBA", (nw, nh), LOGO_INK + (255,))
        ink.putalpha(wm)
        base.alpha_composite(ink, (int(d / 2 - nw / 2), int(d / 2 - nh / 2)))

    tw = tb[2] - tb[0]
    tx = int((d - tw) / 2)
    ty = d + int(label_h * 0.34)
    td = ImageDraw.Draw(base)
    # 浅色底上的字：先描一圈极淡的白色，压住深色壁纸；再上主色
    for dx, dy in ((0, 1), (1, 0), (0, -1), (-1, 0)):
        td.text((tx + dx, ty + dy), text, font=font, fill=(255, 255, 255, 90))
    td.text((tx, ty), text, font=font, fill=color + (240,))
    return base, (d, total_h), total_h


def logo_frames(d: int, color, state: str = "active", n: int = 24):
    """呼吸帧：静态核心 + 每帧两层柔光环。

    光环的外半径会超出圆盘，如果画在 d×d 画布上，环会被边缘切出直线（实拍里那道
    直线切口）。所以整张画布按 1.3d 开，圆盘居中，环在最外层也留了模糊余量——
    四周边缘必须完全透明，`tests/test_overlay_art.py` 会盯着这条。
    """
    core, (w, total_h), _ = _logo_core(d, color, state)
    pad = int(d * 0.15)                      # 四周留够模糊余量
    cw, ch = d + pad * 2, total_h + pad * 2
    cx = cw / 2.0
    disc_c = d / 2.0                         # 圆盘半径（相对 d）
    out = []
    for i in range(n):
        pulse = 0.5 + 0.5 * math.sin((i / n) * math.tau)
        ring = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        rd = ImageDraw.Draw(ring)
        for rr, aa, blur in ((1.0, 0.85, 1.6), (1.12, 0.40, 2.4)):
            rad = disc_c * rr
            rd.ellipse((cx - rad, pad + disc_c - rad, cx + rad, pad + disc_c + rad),
                       outline=color + (int(255 * aa * (0.55 + 0.45 * pulse)),),
                       width=max(2, int(d * 0.02)))
        ring = ring.filter(ImageFilter.GaussianBlur(d * 0.012))
        img = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        img.alpha_composite(ring)
        img.alpha_composite(core, (pad, pad))
        out.append(img)
    return out


# ---------------------------------------------------------------- 渲染器

class DEVMODEW(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", ctypes.c_wchar * 32),
        ("dmSpecVersion", ctypes.c_ushort), ("dmDriverVersion", ctypes.c_ushort),
        ("dmSize", ctypes.c_ushort), ("dmDriverExtra", ctypes.c_ushort),
        ("dmFields", ctypes.c_ulong),
        ("dmPositionX", ctypes.c_long), ("dmPositionY", ctypes.c_long),
        ("dmDisplayOrientation", ctypes.c_ulong), ("dmDisplayFixedOutput", ctypes.c_ulong),
        ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short), ("dmFormName", ctypes.c_wchar * 32),
        ("dmLogPixels", ctypes.c_ushort), ("dmBitsPerPel", ctypes.c_ulong),
        ("dmPelsWidth", ctypes.c_ulong), ("dmPelsHeight", ctypes.c_ulong),
        ("dmDisplayFlags", ctypes.c_ulong), ("dmDisplayFrequency", ctypes.c_ulong),
        ("dmICMMethod", ctypes.c_ulong), ("dmICMIntent", ctypes.c_ulong),
        ("dmMediaType", ctypes.c_ulong), ("dmDitherType", ctypes.c_ulong),
        ("dmReserved1", ctypes.c_ulong), ("dmReserved2", ctypes.c_ulong),
        ("dmPanningWidth", ctypes.c_ulong), ("dmPanningHeight", ctypes.c_ulong),
    ]


def _display_fps(default=60):
    """当前显示模式的刷新率。光晕跟手要跟到屏幕的帧，而不是写死 40fps。"""
    try:
        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(DEVMODEW)
        if ctypes.windll.user32.EnumDisplaySettingsW(None, -1, ctypes.byref(dm)):
            hz = int(dm.dmDisplayFrequency or 0)
            if 30 <= hz <= 360:
                return hz
    except Exception:
        pass
    return default


class Renderer(threading.Thread):
    """所有 WS_EX_LAYERED 窗口都在这个线程里画，也在这个线程里收鼠标消息。"""

    def __init__(self, emit):
        super().__init__(name="cu-overlay-render", daemon=True)
        self.emit = emit
        self.scale = _dpi_scale()
        self.fps = _display_fps()
        self.lock = threading.Lock()
        self.running = True
        self.mode = "idle"
        self.border_on = False
        self.halo_on = False
        self.logo_on = False
        self.logo_state = "active"
        self.banner = None          # dict(text/sub/note/seconds/buttons/tone/deadline)
        self.flash_until = 0.0
        self.flash_color = RED
        self.border_peak = 0.5
        self.border_color = STATUS_ACTIVE
        self._border_imgs = None
        self._halo_img = None
        self._halo_pos = None
        self._logo_frames = None
        self._logo_d = 0
        self._logo_phase = 0.0
        self._last_banner_key = None
        self._last_border_key = None
        self._banner_hits = {}
        self._banner_pad = 0
        self._wins_ready = False

    # ---- 窗口必须建在渲染线程里
    def _ensure_windows(self):
        """在**渲染线程**里创建全部窗口。

        窗口只能被拥有它的线程 SetWindowPos：放到主线程建、渲染线程摆，Windows 会把
        WM_WINDOWPOSCHANGING 发回主线程，而主线程正卡在 stdin 读命令上，于是第一次
        SetWindowPos 就永久阻塞——表现是"四条边只出来一条，之后什么都不再动"。
        """
        if self._wins_ready:
            return
        self.w_border = [LayerWindow("cu-border-%d" % i) for i in range(4)]
        self.w_halo = LayerWindow("cu-halo")
        # 光晕每帧都在动，但 z 序只在第一次显示时置顶（否则会跟鲸鱼互相顶，见 present）
        self.w_halo.sticky_zorder = True
        self.w_banner = LayerWindow("cu-banner", click_through=False,
                                    on_click=self._banner_click)
        self.w_logo = LayerWindow("cu-logo", click_through=False,
                                  on_click=lambda *a: self.emit({"event": "logo_click"}))
        for w in self.w_border + [self.w_halo, self.w_banner, self.w_logo]:
            w.hide()
        self._wins_ready = True

    # ---- 外部入口（线程安全，只改状态，不直接画）
    def apply(self, cmd: dict):
        with self.lock:
            k = cmd.get("cmd")
            if k == "off":
                self.mode = "idle"
                self.border_on = self.halo_on = self.logo_on = False
                self.banner = None
                self.flash_until = 0.0
                return
            if k == "border":
                self.border_on = bool(cmd.get("on", True))
                if cmd.get("color"):
                    self.border_color = _rgb(cmd["color"])
                if cmd.get("alpha") is not None:
                    self.border_peak = float(cmd["alpha"])
            elif k == "halo":
                self.halo_on = bool(cmd.get("on", True))
            elif k == "logo":
                self.logo_on = bool(cmd.get("on", True))
                st = str(cmd.get("state") or "active")
                if st != self.logo_state:
                    self.logo_state = st
                    self._logo_frames = None      # 换状态要重画（字和环色都变）
            elif k == "banner":
                self.banner = {
                    "text": cmd.get("text", ""),
                    "sub": cmd.get("sub", ""),
                    "note": cmd.get("note", ""),
                    "seconds": int(cmd.get("seconds", 0) or 0),
                    "buttons": bool(cmd.get("buttons", False)),
                    "tone": cmd.get("tone", "consent"),
                    "deadline": time.time() + int(cmd.get("seconds", 0) or 0),
                }
                self.mode = cmd.get("tone", "consent")
            elif k == "hide_banner":
                self.banner = None
            elif k == "flash":
                self.flash_until = time.time() + 0.45 * int(cmd.get("times", 2) or 2)
                self.flash_color = _rgb(cmd.get("color", "#FF3B30"))
            elif k == "quit":
                self.running = False

    # ---- 渲染循环
    def run(self):
        self._ensure_windows()
        # 画不出来就必须死：服务端把"提示层进程活着"当作"屏幕上有提示"，
        # 一个活着但画不出画面的窗口等于偷偷控制电脑，所以这里宁可退出。
        fails = 0
        # 光标光晕要跟手，帧率就跟显示器刷新率走（60Hz 屏 60fps、144Hz 屏 144fps）。
        # Windows 默认 15.6ms 的定时粒度会把 144fps 压成 60fps，这里临时要到 1ms。
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)
            fine_timer = True
        except Exception:
            fine_timer = False
        fps = self.fps
        interval = 1.0 / fps
        next_at = time.perf_counter()
        try:
            while self.running:
                try:
                    self._pump()
                    self._draw()
                    fails = 0
                except Exception:
                    traceback.print_exc()
                    fails += 1
                    if fails >= 5:
                        print("overlay: 连续 %d 帧画不出来，退出" % fails, flush=True)
                        os._exit(1)
                # 什么都没有时没必要满帧空转：降到 4fps，画面要么本来就空，
                # 要么下一帧就会因为命令把状态改回来。
                step = interval if self._visible() else 0.25
                next_at += step
                delay = next_at - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_at = time.perf_counter()   # 落后太多就重新对齐，不补帧
        finally:
            if fine_timer:
                try:
                    ctypes.windll.winmm.timeEndPeriod(1)
                except Exception:
                    pass

    def _visible(self):
        return bool(self.banner or self.flash_until > time.time()
                    or self.border_on or self.halo_on or self.logo_on)

    def _pump(self):
        msg = MSG()
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            if msg.message == WM_DESTROY:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _draw(self):
        now = time.time()
        # 分辨率/缩放可能在运行中被改（拖动窗口到另一块屏、改显示设置）：
        # 每帧顺手比一次，变了就把按 scale 生成的图全部作废重画。
        sc = _dpi_scale()
        if abs(sc - self.scale) > 0.01:
            self.scale = sc
            self._halo_img = None
            self._logo_frames = None
            self._border_imgs = None
            self._last_border_key = None
            self._last_banner_key = None
        with self.lock:
            mode = self.mode
            border_on, halo_on, logo_on = self.border_on, self.halo_on, self.logo_on
            banner = dict(self.banner) if self.banner else None
            flashing = now < self.flash_until
            peak = self.border_peak
            color = self.flash_color if flashing else self.border_color
            if flashing:
                peak = 0.55 + 0.25 * math.sin(now * 26.0)

        sx, sy, sw, sh = _screen_rect()
        thick = int(52 * self.scale)

        if border_on and peak > 0.01:
            key = (round(peak, 2), color, (sw, sh), thick)
            if key != self._last_border_key:
                self._last_border_key = key
                self._border_imgs = [
                    edge_image((thick, sh), color, peak, True, True),
                    edge_image((thick, sh), color, peak, True, False),
                    edge_image((sw, thick), color, peak, False, True),
                    edge_image((sw, thick), color, peak, False, False),
                ]
            imgs = self._border_imgs
            self.w_border[0].present(imgs[0], sx, sy)
            self.w_border[1].present(imgs[1], sx + sw - thick, sy)
            self.w_border[2].present(imgs[2], sx, sy)
            self.w_border[3].present(imgs[3], sx, sy + sh - thick)
        else:
            for w in self.w_border:
                w.hide()

        if halo_on:
            cx, cy = _cursor()
            # 光标停在鲸鱼上时不画光晕：两个 topmost 窗口叠在一起会互相顶 z 序（闪烁），
            # 而且鲸鱼本身已经是"这里在控制"的提示，不需要再压一个圆点上去。
            r = getattr(self, "_logo_rect", None)
            if r and r[0] - 8 <= cx <= r[0] + r[2] + 8 and r[1] - 8 <= cy <= r[1] + r[3] + 8:
                self.w_halo.hide()
            else:
                hd = int(72 * _scale_at(cx, cy))
                if self._halo_img is None or self._halo_img.size[0] != hd:
                    self._halo_img = halo_image(hd)
                # 和指针同心：用户要"对得上"，不是飘在下面。
                self.w_halo.present(self._halo_img, cx - hd // 2, cy - hd // 2)
        else:
            self.w_halo.hide()

        if logo_on:
            wx, wy, ww, wh = _work_area()
            ls = _scale_at(wx + ww - int(40 * self.scale), wy + wh - int(40 * self.scale))
            d = int(62 * ls)
            self._logo_phase += 0.02
            if self._logo_frames is None or self._logo_d != d:
                color = PAUSED_INK if self.logo_state == "paused" else STATUS_ACTIVE
                self._logo_frames = logo_frames(d, color, self.logo_state)
                self._logo_d = d
            img = self._logo_frames[int(self._logo_phase * len(self._logo_frames)) %
                                    len(self._logo_frames)]
            iw, ih = img.size
            lx = wx + ww - iw - int(16 * ls)
            ly = wy + wh - ih - int(16 * ls)
            self._logo_rect = (lx, ly, iw, ih)
            self.w_logo.present(img, lx, ly)
        else:
            self._logo_rect = None
            self.w_logo.hide()

        if banner:
            wx, wy, ww, wh = _work_area()
            bs = _scale_at(wx + ww // 2, wy + int(20 * self.scale))
            key = (banner["text"], banner["sub"], banner["note"], banner["buttons"],
                   banner["tone"], max(0, int(banner["deadline"] - now)), bs)
            if key != self._last_banner_key:
                self._last_banner_key = key
                left = max(0, int(banner["deadline"] - now))
                img, hits, pad = banner_image(
                    banner["text"], banner["sub"], banner["note"], left,
                    banner["buttons"], banner["tone"], bs)
                self._banner_hits = hits
                self._banner_pad = pad
                self.w_banner.present(img, wx + (ww - img.size[0]) // 2, wy + int(6 * bs))
        else:
            self.w_banner.hide()

    def _banner_click(self, win, x, y):
        with self.lock:
            hits = getattr(self, "_banner_hits", {})
            pad = getattr(self, "_banner_pad", 0)
        for name, (hx, hy, hw, hh) in hits.items():
            if hx <= x - pad <= hx + hw and hy <= y - pad <= hy + hh:
                self.emit({"event": name})
                return


# ---------------------------------------------------------------- 对话框

def dialog_main(spec_json):
    """--dialog 模式：单独进程开一个留言框，发完退出并往 stdout 回一行 JSON。

    和渲染进程分居两个进程是必须的：渲染线程 40fps 在 UpdateLayeredWindow 上刷，
    同进程里再开第二个自绘窗口，Windows 只给它一块黑底（2026-11-13 实测）。
    画法和横幅完全一致（见 python/dialog_win.py）。
    """
    import dialog_win
    dialog_win.dialog_main(spec_json)


def main():
    if "--dialog" in sys.argv:
        idx = sys.argv.index("--dialog")
        spec = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else "{}"
        dialog_main(spec)
        return
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass

    out_lock = threading.Lock()

    def emit(obj):
        line = json.dumps(obj, ensure_ascii=False)
        with out_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    renderer = Renderer(emit)
    renderer.start()

    import queue
    cmds: "queue.Queue[dict]" = queue.Queue()

    def reader():
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                cmds.put(json.loads(line))
            except Exception:
                pass
        cmds.put({"cmd": "quit"})

    threading.Thread(target=reader, name="cu-overlay-stdin", daemon=True).start()

    emit({"event": "ready", "screen": list(_screen_rect()), "scale": renderer.scale})
    try:
        while True:
            try:
                cmd = cmds.get(timeout=0.04)
            except queue.Empty:
                continue
            if cmd.get("cmd") == "quit":
                break
            renderer.apply(cmd)
    finally:
        renderer.apply({"cmd": "off"})
        renderer.running = False
        time.sleep(0.15)


if __name__ == "__main__":
    main()
