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

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

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


def _monitors():
    """每台显示器的物理矩形（EnumDisplayMonitors）。

    为什么不能只用虚屏矩形：虚屏是包住所有显示器的大盒子，副屏和主屏一样大时它的
    四条边只贴着最外侧，中间那台显示器根本收不到边；两台大小不同时，小屏的四条边
    会落在离它边界有"固定距离"的地方——用户看到的就是"四条直线不在屏幕边上"。
    """
    out = []

    def _cb(hmon, hdc, lprc, lparam):
        r = lprc.contents
        out.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True

    try:
        MONITORENUMPROC = ctypes.WINFUNCTYPE(
            wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)
        user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_cb), 0)
    except Exception:
        pass
    if not out:
        out.append(_screen_rect())
    return out


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


_premult_cache = {}


def _premultiply(img: Image.Image) -> bytes:
    """RGBA -> 预乘 BGRA 字节串（UpdateLayeredWindow 要求）。

    用 PIL 的通道运算代替逐像素 Python 循环：同一个 119x158 的鲸鱼帧，
    逐像素要 3.3ms，240Hz 下等于一条核的 80% 全喂给它；通道乘完只要 0.3ms。
    结果按图片对象缓存（鲸鱼 24 帧、边框 4 张都是稳定对象，缓存后是纯命中）。
    """
    key = (id(img), img.size)
    hit = _premult_cache.get(key)
    if hit is not None:
        return hit
    rgba = img if img.mode == "RGBA" else img.convert("RGBA")
    r, g, b, a = rgba.split()
    alpha_rgb = Image.merge("RGB", (a, a, a))
    rgb = ImageChops.multiply(Image.merge("RGB", (r, g, b)), alpha_rgb)
    out = Image.merge("RGBA", (*rgb.split(), a)).tobytes("raw", "BGRA")
    if len(_premult_cache) > 64:      # 只是防呆：正常只有几十张
        _premult_cache.clear()
    _premult_cache[key] = out
    return out


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

    def present(self, img: Image.Image, x: int, y: int, alpha: int = 255):
        alpha = max(0, min(255, int(alpha)))
        # 同一张图 + 同一位置 + 同一透明度 + 已经显示着：不需要重新上传
        # （每帧重画 52x1600 的渐变太贵）
        if (img is getattr(self, "_last_img", None) and (x, y) == self._pos
                and alpha == getattr(self, "_last_alpha", 255) and self._visible):
            return
        # 图没变、只是挪位置（光标光晕每帧都在动）：分层窗口的内容不用重传，
        # SetWindowPos 就够。这样 240Hz 跟手也不会把 CPU 烧掉。
        if (img is getattr(self, "_last_img", None) and self._size == img.size
                and alpha == getattr(self, "_last_alpha", 255)):
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
        self._last_alpha = alpha
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
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, alpha, AC_SRC_ALPHA)
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
STATUS_OK = (52, 199, 89)       # 留言已送达、模型正在响应
STATUS_FAIL = (255, 59, 48)     # 留言递送失败
DANGER = (216, 74, 62)          # 状态条/错误：急停
PAUSED_INK = (146, 152, 164)
BREATH_SECONDS = 2.6             # 鲸鱼呼吸一圈的时间（按时间走，不按帧数）
HALO_BREATH_SECONDS = 1.0        # 光标光晕呼吸周期：用户要 1Hz
HALO_BREATH_AMP = 0.25           # 半径 ±25%
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


def halo_frames(d: int = 72, color=None, n: int = 30):
    """光标光晕的呼吸帧：半径按 1Hz 正弦在 ±25% 之间连续变化。

    逐像素画 30 张太慢，所以只画一张最大尺寸的底图，再按帧缩放出不同半径。
    帧率取 30（每秒吸/呼各一次，30 帧足够顺；再高也只是重复上传同一张图）。
    """
    dmax = max(8, int(round(d * (1.0 + HALO_BREATH_AMP))))
    base = halo_image(dmax, color)
    frames = []
    for i in range(n):
        k = 1.0 + HALO_BREATH_AMP * math.sin((i / n) * math.tau)
        size = max(8, int(round(d * k)))
        frames.append(base.resize((size, size), Image.LANCZOS))
    return frames


def _edge_ramp(span, peak, reverse=False):
    """一维向内渐隐的 alpha 斜坡（0..255）。"""
    out = []
    for i in range(span):
        d = (span - 1 - i) if reverse else i
        k = d / max(1, span - 1)
        out.append(max(0, min(255, int(255 * peak * (1.0 - k) ** 1.7))))
    return out


def _ramp_img(size, ramp, vertical):
    w, h = size
    if vertical:
        return Image.frombytes("L", (1, len(ramp)), bytes(ramp)).resize((w, h), Image.NEAREST)
    return Image.frombytes("L", (len(ramp), 1), bytes(ramp)).resize((w, h), Image.NEAREST)


def frame_images(w, h, th, color, peak):
    """一屏四条边：left/right 竖向整条，top/bottom 横向内缩。

    四角是重点：竖条的角格用 max(横向斜坡, 纵向斜坡) 合成——
    既不会两窗叠加变浓，也不是把一条边粗暴切掉（那样接缝处会突然断掉）。
    横向条从 x=th 开始，它在 x=th 处的 alpha 恰好等于竖条角格用 max
    合成后的值，所以接缝两边数值连续。
    """
    th = max(1, min(th, w // 2, h // 2))
    hx = _edge_ramp(th, peak, reverse=False)       # 从 x=0 向内
    hx_rev = _edge_ramp(th, peak, reverse=True)    # 从 x=th-1 向内
    vy = _edge_ramp(th, peak, reverse=False)       # 从 y=0 向内
    vy_rev = _edge_ramp(th, peak, reverse=True)    # 从 y=h-1 向内

    def vert_strip(reverse):
        row = _ramp_img((th, h), hx_rev if reverse else hx, vertical=False)
        v_top = _ramp_img((th, th), vy, vertical=True)
        v_bot = _ramp_img((th, th), vy_rev, vertical=True)
        a = Image.new("L", (th, h))
        a.paste(row.crop((0, th, th, h - th)), (0, th))
        a.paste(ImageChops.lighter(row.crop((0, 0, th, th)), v_top), (0, 0))
        a.paste(ImageChops.lighter(row.crop((0, h - th, th, h)), v_bot), (0, h - th))
        img = Image.new("RGBA", (th, h), (color[0], color[1], color[2], 0))
        img.putalpha(a)
        return img

    left = vert_strip(False)
    right = vert_strip(True)
    inner = max(1, w - 2 * th)
    top = Image.new("RGBA", (inner, th), (color[0], color[1], color[2], 0))
    top.putalpha(_ramp_img((inner, th), vy, vertical=True))
    bottom = Image.new("RGBA", (inner, th), (color[0], color[1], color[2], 0))
    bottom.putalpha(_ramp_img((inner, th), vy_rev, vertical=True))
    return left, right, top, bottom


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


def note_image(text, scale, max_w=340):
    """右下角的小回执卡：agent 用 reply 回答用户之后，把这句话显示在鲸鱼上方。

    之前 note 命令只存不画，用户根本看不到 agent 的回复（工具侧消息的闭环断了）。
    """
    s = scale
    f = _font(int(12.5 * s))
    pad = int(12 * s)
    w = int(max_w * s)
    lines = _wrap(f, text, w - pad * 2)[:4]
    line_h = int(18 * s)
    h = pad * 2 + line_h * len(lines)
    content = Image.new("RGBA", (w, h), SURFACE + (252,))
    d = ImageDraw.Draw(content)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=int(10 * s),
                        fill=SURFACE + (252,), outline=SURFACE_EDGE + (255,),
                        width=max(1, int(s)))
    y = pad
    for ln in lines:
        d.text((pad, y), ln, font=f, fill=SUB_INK + (255,))
        y += line_h
    return panel_image(content, int(10 * s))


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


def _logo_core(d: int, color, state: str = "active", label: str = None, sweep=None):
    """logo 静态部分：白底圆盘 + 官方鲸鱼（黑），下方一行状态字。

    状态字是用户要求的"一眼看出它在干活还是停了"：控制中=蓝，已暂停=灰蓝。
    label 非空时覆盖默认状态字（操作中/思考中/后台执行中…）。
    返回 (图, 画布尺寸, 总高)；图比圆盘高一块，多出来的是给状态字留的。
    """
    ss = 3
    size = d * ss
    # 状态字区：字形高度实测 + 上下留白。之前 0.30d 固定高，d=93 时字底贴着画布边缘，
    # 实拍里就是"文字被截断"。
    text = label or LOGO_LABEL.get(state, LOGO_LABEL["active"])
    try:
        font = ImageFont.truetype(os.path.join(FONT_DIR, "msyhbd.ttc"), max(11, int(d * 0.20)))
    except OSError:
        font = ImageFont.load_default()
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    tb = probe.textbbox((0, 0), text, font=font)
    glyph_h = tb[3] - tb[1]
    tw = tb[2] - tb[0]
    # 状态字可能比圆盘宽（「后台执行中」），画布跟着加宽，免得被切掉。
    cw = max(d, tw + int(d * 0.16))
    label_h = glyph_h + int(d * 0.22)
    total_h = d + label_h
    c = size / 2.0
    body_r = c - 5.0 * ss

    base = Image.new("RGBA", (cw, total_h), (0, 0, 0, 0))
    disc = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((c - body_r, c - body_r, c + body_r, c + body_r), fill=255)
    disc.paste(Image.new("RGBA", (size, size), SURFACE + (246,)), (0, 0), mask)
    ImageDraw.Draw(disc).ellipse(
        (c - body_r, c - body_r, c + body_r, c + body_r),
        outline=SURFACE_EDGE + (255,), width=max(1, int(1.2 * ss)))
    disc = disc.resize((d, d), Image.LANCZOS)
    base.alpha_composite(disc, (int(cw / 2 - d / 2), 0))

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
        base.alpha_composite(ink, (int(cw / 2 - nw / 2), int(d / 2 - nh / 2)))

    tx = int((cw - tw) / 2)
    ty = d + int(label_h * 0.34)
    td = ImageDraw.Draw(base)
    # 文字周围保持干净：不画胶囊底、不描边。之前那块深色胶囊在浅色壁纸上就是
    # 用户看到的"深色椭圆印记"。字用近白，光泽只沿笔划走。
    td.text((tx, ty - int(glyph_h * 0.12)), text, font=font, fill=(238, 242, 250, 252))
    if sweep is not None:
        _sweep_text(base, text, font, tx, ty - int(glyph_h * 0.12), phase=sweep)
    return base, (cw, total_h), total_h


def _sweep_text(base, text, font, tx, ty, phase, tilt_deg=20.0):
    """在**文字笔划内部**扫一道带 20° 倾角的浅色光束。

    用户要的是"字本身亮起来"，不是给胶囊底打光：所以光束先做成一条倾斜的
    渐变带，再乘文字 alpha 掩膜——胶囊底、空白处一点都不会亮，光带只沿着
    "操作中"这三个字的笔划流动，并且带倾角跨字。
    """
    w, h = base.size
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).text((tx, ty), text, font=font, fill=255)
    box = mask.getbbox()
    if not box:
        return
    x0, y0, x1, y1 = box
    span = x1 - x0
    bw = max(6, int(span * 0.55))
    mid_y = (y0 + y1) / 2.0
    slope = math.tan(math.radians(tilt_deg))
    left = x0 - bw + phase * (span + 2 * bw)
    band = Image.new("L", (w, h), 0)
    bd = ImageDraw.Draw(band)
    for y in range(y0, y1):
        cx = left + slope * (y - mid_y)
        for i in range(bw):
            k = 1.0 - abs(i - bw / 2.0) / (bw / 2.0)
            if k <= 0:
                continue
            x = int(cx) + i
            if 0 <= x < w:
                bd.point((x, y), fill=int(255 * (k ** 1.4)))
    glow = Image.new("RGBA", (w, h), (150, 200, 255, 255))
    alpha = ImageChops.multiply(band, mask)
    glow.putalpha(alpha)
    base.alpha_composite(glow)


def logo_frames(d: int, color, state: str = "active", n: int = 24, label: str = None,
                sweep_on: bool = True):
    """呼吸帧：静态核心 + 每帧两层柔光环。

    光环的外半径会超出圆盘，如果画在 d×d 画布上，环会被边缘切出直线（实拍里那道
    直线切口）。所以整张画布按 1.3d 开，圆盘居中，环在最外层也留了模糊余量——
    四周边缘必须完全透明，`tests/test_overlay_art.py` 会盯着这条。
    """
    core, (cw0, total_h), _ = _logo_core(d, color, state, label)
    pad = int(d * 0.15)                      # 四周留够模糊余量
    cw, ch = cw0 + pad * 2, total_h + pad * 2
    cx = cw / 2.0
    disc_c = d / 2.0                         # 圆盘半径（相对 d）
    out = []
    for i in range(n):
        # 状态词光束：2.5 秒一个周期，前 0.5 秒扫过文字（用户要求：单特效 0.5s）
        t = (i / float(n)) * BREATH_SECONDS
        ph = (t % 2.5) / 2.5
        sweep = (ph / 0.2) if (sweep_on and ph < 0.2) else None
        core_i = _logo_core(d, color, state, label, sweep=sweep)[0] if sweep is not None else core
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
        img.alpha_composite(core_i, (pad, pad))
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
        self.halo_color = None      # None = 跟随状态色
        self.logo_on = False
        self.logo_state = "active"
        self.logo_label = None      # 覆盖状态字（操作中/思考中/后台执行中…）
        self.logo_color = None      # 覆盖呼吸环颜色（留言送达=绿、失败=红）
        self.logo_sweep = True      # 只有"正在有序运行"时才让光泽扫过状态字
        self.banner = None          # dict(text/sub/note/seconds/buttons/tone/deadline)
        # 鲸鱼旁边的小回执卡：agent 用 reply 回答过用户之后，把自己的话说一遍给用户看。
        # 顶部横幅只留给"启动申请 / 关闭提示"，所以回执走右下角，跟鲸鱼同一个角落。
        self.note = None            # dict(text, deadline)
        self.flash_t0 = 0.0         # 闪烁起点（按时间算，不按帧）
        self.flash_n = 0            # 呼吸几下
        self.flash_color = RED
        self.border_peak = 0.5
        self.border_color = STATUS_ACTIVE
        self._border_cache = {}     # 每台显示器一份边条图缓存
        self._halo_img = None
        self._halo_pos = None
        self._halo_frames = None
        self._halo_d = 0
        self._halo_col = None
        self._halo_t0 = time.time()
        self._halo_idx = -1
        self._logo_frames = None
        self._logo_key = None
        self._logo_cache = {}
        self._logo_d = 0
        self._logo_phase = 0.0
        # 呼吸节奏按"时间"走，不按帧数：这台机是 240Hz，按帧推进会让鲸鱼每 0.1 秒
        # 呼吸一次（看起来在抖），而且每帧都要重传一次带 alpha 的整图。
        # 现在一圈固定 BREATH_SECONDS，且只在帧号变化时才上传。
        self._logo_t0 = time.time()
        self._logo_idx = -1
        self._logo_pos = None
        self._last_banner_key = None
        self._note_key = None
        self._note_img = None
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
        self._border_wins = []   # 每组 4 条边，对应一台显示器；按需增建
        self.w_halo = LayerWindow("cu-halo")
        # 光晕每帧都在动，但 z 序只在第一次显示时置顶（否则会跟鲸鱼互相顶，见 present）
        self.w_halo.sticky_zorder = True
        self.w_banner = LayerWindow("cu-banner", click_through=False,
                                    on_click=self._banner_click)
        self.w_note = LayerWindow("cu-note")
        self.w_logo = LayerWindow("cu-logo", click_through=False,
                                  on_click=lambda *a: self.emit({"event": "logo_click"}))
        for w in [self.w_halo, self.w_banner, self.w_note, self.w_logo]:
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
                self.note = None
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
                if cmd.get("color") is not None:
                    col = _rgb(cmd["color"]) if cmd.get("color") else None
                    if col != self.halo_color:
                        self.halo_color = col
                        self._halo_frames = None      # 换色要重画全部呼吸帧
            elif k == "logo":
                self.logo_on = bool(cmd.get("on", True))
                st = str(cmd.get("state") or "active")
                label = cmd.get("label")
                label = str(label)[:12] if label else None
                col = _rgb(cmd["color"]) if cmd.get("color") else None
                sw = bool(cmd.get("sweep", True))
                if (st, label, col, sw) != (self.logo_state, self.logo_label,
                                            self.logo_color, self.logo_sweep):
                    self.logo_state, self.logo_label, self.logo_color = st, label, col
                    self.logo_sweep = sw
                    self._logo_frames = None      # 换状态/字/环色都要重画
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
            elif k == "note":
                text = str(cmd.get("text") or "").strip()[:400]
                secs = int(cmd.get("seconds", 8) or 8)
                self.note = {"text": text, "seconds": secs,
                             "deadline": time.time() + secs} if text else None
            elif k == "flash":
                # 快速呼吸：只记"从此刻起呼吸几下"，透明度由渲染线程按时间曲线算，
                # 不在这里改 border_peak（那会让每帧都重画一张渐变图，正是闪烁的来源）。
                self.flash_t0 = time.time()
                self.flash_n = max(1, min(6, int(cmd.get("times", 3) or 3)))
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

    def _flash_env(self, now):
        """闪烁包络：(颜色, 透明系数)。系数 0..1，三下平滑呼吸后归零。

        形状：每下 0.34s，sin² 上升/回落（没有硬切，所以不会像频闪灯）；
        最后一下的尾段额外线性淡出，避免"啪"地消失。
        """
        if self.flash_n <= 0:
            return None, 0.0
        period = 0.42
        total = period * self.flash_n
        t = now - self.flash_t0
        if t < 0 or t >= total:
            return None, 0.0
        frac = (t % period) / period
        k = 0.16 + 0.84 * (math.sin(math.pi * frac) ** 2)
        tail = total - t
        if tail < 0.2:
            k *= max(0.0, tail / 0.2)
        return self.flash_color, k

    def _visible(self):
        return bool(self.banner or self._flash_env(time.time())[1] > 0.0
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
            self._border_cache = {}
            self._last_banner_key = None
        with self.lock:
            mode = self.mode
            border_on, halo_on, logo_on = self.border_on, self.halo_on, self.logo_on
            banner = dict(self.banner) if self.banner else None
            note = dict(self.note) if self.note else None
            if note and now > note["deadline"]:
                self.note = note = None
            fcol, fk = self._flash_env(now)
            peak = self.border_peak
            color = self.border_color
            alpha = 255
            flashing = fcol is not None and fk > 0.0
            if flashing:
                # 渐变图固定按"满强度"预画一次，闪烁只改窗口整体 alpha。
                # 这样渐变形状不会随强度变化被重画（旧实现每帧重画 → 频闪 + 叠加）。
                color = fcol
                peak = 0.95
                # 量化到 8 级：40fps 下肉眼看不出台阶，但整图上传少到 1/8，
                # 避免四条 52x1600 的位图每帧重传拖慢渲染线程。
                alpha = int(round(255 * fk / 8.0)) * 8

        mons = _monitors()
        # 每台显示器一组四条边：虚屏矩形只贴最外侧，多屏/不同 DPI 时会画到离边界
        # 固定距离的地方。这里按 EnumDisplayMonitors 逐屏各画一圈。
        while len(self._border_wins) < len(mons):
            i = len(self._border_wins)
            wins = [LayerWindow("cu-border-%d-%d" % (i, j)) for j in range(4)]
            for w in wins:
                w.hide()
            self._border_wins.append(wins)
        for extra in self._border_wins[len(mons):]:
            for w in extra:
                w.hide()

        for idx, (mx, my, mw, mh) in enumerate(mons):
            wins = self._border_wins[idx]
            # 非闪烁时用渐变峰值判可见；闪烁时渐变恒定、看整窗 alpha。
            if not (border_on and (alpha > 4 if flashing else peak > 0.01)):
                for w in wins:
                    w.hide()
                continue
            th = max(6, int(52 * _scale_at(mx + mw // 2, my + mh // 2)))
            key = (round(peak, 2), color, (mw, mh), th)
            cache = self._border_cache.setdefault(idx, {})
            if cache.get("key") != key:
                cache["key"] = key
                cache["imgs"] = list(frame_images(mw, mh, th, color, peak))
            imgs = cache["imgs"]
            wins[0].present(imgs[0], mx, my, alpha)
            wins[1].present(imgs[1], mx + mw - th, my, alpha)
            wins[2].present(imgs[2], mx + th, my, alpha)
            wins[3].present(imgs[3], mx + th, my + mh - th, alpha)

        if halo_on:
            cx, cy = _cursor()
            # 光标停在鲸鱼上时不画光晕：两个 topmost 窗口叠在一起会互相顶 z 序（闪烁），
            # 而且鲸鱼本身已经是"这里在控制"的提示，不需要再压一个圆点上去。
            r = getattr(self, "_logo_rect", None)
            if r and r[0] - 8 <= cx <= r[0] + r[2] + 8 and r[1] - 8 <= cy <= r[1] + r[3] + 8:
                self.w_halo.hide()
                self._halo_idx = -1
            else:
                hd = int(72 * _scale_at(cx, cy))
                # 暂停时灰蓝，正常时品牌蓝；服务端可用 halo{color} 覆盖。
                col = self.halo_color or (PAUSED_INK if mode == "paused" else STATUS_ACTIVE)
                if self._halo_frames is None or self._halo_d != hd or self._halo_col != col:
                    self._halo_frames = halo_frames(hd, col)
                    self._halo_d, self._halo_col = hd, col
                    self._halo_t0 = now
                    self._halo_idx = -1
                n = len(self._halo_frames)
                # 半径按时间正弦呼吸（1Hz、±25%），帧号只在变化时才上传。
                idx = int(((now - self._halo_t0) % HALO_BREATH_SECONDS)
                          / HALO_BREATH_SECONDS * n) % n
                img = self._halo_frames[idx]
                size = img.size[0]
                x, y = cx - size // 2, cy - size // 2
                if idx != self._halo_idx or (x, y) != self._halo_pos:
                    self._halo_idx = idx
                    self._halo_pos = (x, y)
                    # 和指针同心：用户要"对得上"，不是飘在下面。
                    self.w_halo.present(img, x, y)
        else:
            self.w_halo.hide()

        if logo_on:
            wx, wy, ww, wh = _work_area()
            ls = _scale_at(wx + ww - int(40 * self.scale), wy + wh - int(40 * self.scale))
            d = int(62 * ls)
            lcolor = self.logo_color or (STATUS_FAIL if self.logo_state == "failed" else
                                         PAUSED_INK if self.logo_state == "paused" else STATUS_ACTIVE)
            # 光泽只在"正在有序运行"时出现：暂停/待命/卡死都不扫，用户一眼能分出
            # "它在动"和"它停着"。
            sweep_on = bool(self.logo_sweep) and self.logo_state == "active"
            lkey = (d, lcolor, self.logo_state, self.logo_label, sweep_on)
            if self._logo_frames is None or self._logo_key != lkey:
                cached = self._logo_cache.get(lkey)
                if cached is None:
                    cached = logo_frames(d, lcolor, self.logo_state,
                                         label=self.logo_label, sweep_on=sweep_on)
                    if len(self._logo_cache) > 6:
                        self._logo_cache.clear()
                    self._logo_cache[lkey] = cached
                self._logo_frames = cached
                self._logo_key = lkey
                self._logo_d = d
                self._logo_idx = -1
                self._logo_pos = None
            n = len(self._logo_frames)
            phase = ((now - self._logo_t0) % BREATH_SECONDS) / BREATH_SECONDS
            idx = int(phase * n) % n
            img = self._logo_frames[idx]
            iw, ih = img.size
            lx = wx + ww - iw - int(16 * ls)
            ly = wy + wh - ih - int(16 * ls)
            self._logo_rect = (lx, ly, iw, ih)
            if idx != self._logo_idx or (lx, ly) != self._logo_pos:
                self._logo_idx = idx
                self._logo_pos = (lx, ly)
                self.w_logo.present(img, lx, ly)
        else:
            self._logo_rect = None
            self.w_logo.hide()

        if note:
            ls = _scale_at(*_cursor())
            key = (note["text"], ls)
            if key != self._note_key:
                self._note_key = key
                self._note_img = note_image(note["text"], ls)
            nimg = self._note_img
            nw_, nh_ = nimg.size
            lr = getattr(self, "_logo_rect", None)
            if lr:
                nx, ny = lr[0] + lr[2] - nw_, lr[1] - nh_ - int(4 * ls)
            else:
                wx, wy, ww, wh = _work_area()
                nx, ny = wx + ww - nw_ - int(16 * ls), wy + wh - nh_ - int(96 * ls)
            self.w_note.present(nimg, nx, ny)
        else:
            self.w_note.hide()

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
