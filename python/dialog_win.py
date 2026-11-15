"""留言对话框：和横幅同一套前端做法（自绘分层窗口 + PIL 图），不用 Tk。

为什么重写：Tk 那版有两个毛病——卡片做不出和横幅一致的描边/投影，而且 Tk 进程
会在任务栏多出一个 python 图标。现在卡片是 WS_EX_LAYERED|WS_EX_TOOLWINDOW，
和横幅、鲸鱼、边框完全同一条渲染路径；输入区放一个原生 EDIT 子窗口（无边框、
白底），既不进任务栏，输入法也能拿到正确光标位置。

协议：stdout 每行一个 JSON 事件——
    {"event":"message","text":"..."}
    {"event":"dialog_closed"}
"""

import ctypes
import json
import os
import sys
import time

from ctypes import wintypes

import overlay as ov

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SendMessageW.restype = ctypes.c_ssize_t
# 第三个/第四个参数要能收指针（WM_GETTEXT 的缓冲区），所以用 c_void_p 而不是窄整数
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.CallWindowProcW.restype = ctypes.c_ssize_t
user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                   wintypes.WPARAM, wintypes.LPARAM]
user32.SetWindowLongPtrW.restype = ctypes.c_void_p
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
gdi32.CreateFontW.restype = wintypes.HFONT

WM_SETFONT = 0x0030
WM_GETTEXT = 0x000D
WM_KEYDOWN = 0x0100
WM_CHAR = 0x0102
WM_APP = 0x8000
MSG_SEND = WM_APP + 1
MSG_CANCEL = WM_APP + 2

WS_POPUP = 0x80000000
WS_CHILD = 0x40000000
ES_LEFT = 0x0000
ES_AUTOHSCROLL = 0x0080
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = wintypes.HWND(-1)

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)

_keep = []          # 字体/窗口过程必须活着，否则回调被回收就崩


def _emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _font(px, bold=False):
    h = gdi32.CreateFontW(
        -int(px), 0, 0, 0, 700 if bold else 400, 0, 0, 0,
        1, 0, 0, 5, 0, "Microsoft YaHei UI")
    _keep.append(h)
    return h


class DialogWin(object):
    def __init__(self, spec):
        self.spec = spec
        s = self.scale = ov._dpi_scale()
        self.img, self.hits, self.pad = self._render(s)
        self.done = False
        self.edit = None
        self._old_edit_proc = None
        self.card = ov.LayerWindow("cu-dialog", click_through=False, on_click=self._click)
        sx, sy, sw, sh = ov._screen_rect()
        self.x = sx + sw - self.img.width - int(24 * s)
        # 让开右下角那只鲸鱼：它也是 topmost，压在卡片上就会把按钮的点击吃掉。
        self.y = sy + sh - self.img.height - int(200 * s)
        self.card.present(self.img, self.x, self.y)
        self._make_edit(s)
        self._pump()

    # ---- 画面 ----
    def _render(self, s):
        from PIL import Image, ImageDraw
        spec = self.spec
        title = spec.get("title") or "对 agent 说点什么"
        hint = spec.get("hint") or "它会照你说的调整；发送后自动继续。"
        f_title = ov._font(int(20 * s), True)
        f_hint = ov._font(int(13 * s))
        f_btn = ov._font(int(14 * s))
        pad_x = int(20 * s)
        w = int(460 * s)
        text_w = w - pad_x * 2
        title_lines = ov._wrap(f_title, title, text_w)
        hint_lines = ov._wrap(f_hint, hint, text_w)
        lh_title = int(29 * s)
        lh = int(20 * s)
        in_h = int(40 * s)
        btn_h = int(36 * s)
        btn_w = int(96 * s)

        y = int(18 * s)
        y_title = y
        y += lh_title * len(title_lines)
        y_hint = y + int(4 * s)
        y = y_hint + lh * len(hint_lines)
        y_in = y + int(14 * s)
        y_btn = y_in + in_h + int(16 * s)
        content_h = y_btn + btn_h + int(18 * s)

        content = Image.new("RGBA", (w, content_h), ov.SURFACE + (255,))
        d = ImageDraw.Draw(content)
        d.rounded_rectangle((0, 0, w - 1, content_h - 1), radius=int(12 * s),
                            fill=ov.SURFACE + (255,), outline=ov.SURFACE_EDGE + (255,),
                            width=max(1, int(s)))

        yy = y_title
        for ln in title_lines:
            d.text((pad_x, yy), ln, font=f_title, fill=ov.INK + (255,))
            yy += lh_title
        yy = y_hint
        for ln in hint_lines:
            d.text((pad_x, yy), ln, font=f_hint, fill=ov.SUB_INK + (255,))
            yy += lh

        # 输入区：白底 + 细描边 + 圆角，EDIT 就摆在这块里面
        d.rounded_rectangle((pad_x, y_in, pad_x + text_w - 1, y_in + in_h - 1),
                            radius=int(8 * s), fill=(255, 255, 255, 255),
                            outline=ov.SURFACE_EDGE + (255,), width=max(1, int(s)))

        hits = {}
        gap = int(10 * s)
        x_send = pad_x + text_w - btn_w
        x_cancel = x_send - gap - btn_w
        d.rounded_rectangle((x_cancel, y_btn, x_cancel + btn_w, y_btn + btn_h),
                            radius=int(8 * s), fill=(255, 255, 255, 255),
                            outline=ov.SURFACE_EDGE + (255,), width=max(1, int(s)))
        d.text((x_cancel + btn_w / 2, y_btn + btn_h / 2), "取消", font=f_btn,
               fill=ov.SUB_INK + (255,), anchor="mm")
        d.rounded_rectangle((x_send, y_btn, x_send + btn_w, y_btn + btn_h),
                            radius=int(8 * s), fill=ov.ACCENT + (255,))
        d.text((x_send + btn_w / 2, y_btn + btn_h / 2), "发送", font=f_btn,
               fill=ov.ACCENT_INK + (255,), anchor="mm")
        hits["cancel"] = (x_cancel, y_btn, btn_w, btn_h)
        hits["send"] = (x_send, y_btn, btn_w, btn_h)
        hits["input"] = (pad_x, y_in, text_w, in_h)

        img = ov.panel_image(content, int(12 * s), int(18 * s))
        return img, hits, self._pad_for(int(18 * s))

    @staticmethod
    def _pad_for(shadow):
        blur = max(2.0, shadow * 0.7)
        return int(blur * 2.2) + 8

    # ---- 原生输入框 ----
    def _make_edit(self, s):
        px, py, pw, ph = self.hits["input"]
        x = self.x + self.pad + px + int(10 * s)
        y = self.y + self.pad + py + int(9 * s)
        w = pw - int(20 * s)
        h = ph - int(16 * s)
        self.edit = user32.CreateWindowExW(
            WS_EX_TOOLWINDOW | WS_EX_TOPMOST, "EDIT", "",
            WS_POPUP | ES_LEFT | ES_AUTOHSCROLL,
            x, y, w, h, self.card.hwnd, None, None, None)
        user32.SendMessageW(self.edit, WM_SETFONT, _font(int(14 * s)), 1)
        user32.ShowWindow(self.edit, 5)
        self._focus_tries = 0
        self._grab_focus()

    def _grab_focus(self):
        """把焦点抢到输入框。前台锁经常让第一次 SetForegroundWindow 白给，
        这里带 AttachThreadInput 重试几次——否则用户点进来打不了字。"""
        try:
            user32.SetForegroundWindow(self.card.hwnd)
            fg = user32.GetForegroundWindow()
            tid_fg = user32.GetWindowThreadProcessId(fg, None)
            tid_me = kernel32.GetCurrentThreadId()
            attached = False
            if tid_fg and tid_fg != tid_me:
                attached = bool(user32.AttachThreadInput(tid_me, tid_fg, True))
            user32.SetForegroundWindow(self.edit)
            user32.SetFocus(self.edit)
            if attached:
                user32.AttachThreadInput(tid_me, tid_fg, False)
        except Exception:
            pass

    # ---- 交互 ----
    def _click(self, win, x, y):
        if os.environ.get("DSH_CU_DEBUG_MSG") == "1":
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "..", "_preview", "msg.log"), "a", encoding="utf-8") as fh:
                fh.write("click %s %s pad=%s edit=%r\n" % (x, y, self.pad, self.edit))
        for name, (hx, hy, hw, hh) in self.hits.items():
            if hx <= x - self.pad <= hx + hw and hy <= y - self.pad <= hy + hh:
                if name == "send":
                    self._send()
                elif name == "cancel":
                    self._cancel()
                elif name == "input":
                    self._grab_focus()
                return

    def _text(self):
        buf = ctypes.create_unicode_buffer(2048)
        n = user32.GetWindowTextW(self.edit, buf, 2048)
        if n:
            return buf.value.strip()
        # 有的机器上 GetWindowTextW 跨消息泵读回来是空串，直接发 WM_GETTEXT 更稳
        n2 = user32.SendMessageW(self.edit, WM_GETTEXT, 2048, buf)
        return buf.value.strip() if n2 else ""

    def _send(self):
        text = self._text()
        if not text:
            return
        self._finish({"event": "message", "text": text})

    def _cancel(self):
        self._finish({"event": "dialog_closed"})

    def _finish(self, obj):
        if self.done:
            return
        self.done = True
        self._emit_obj = obj
        user32.PostMessageW(self.card.hwnd, WM_APP + 9, 0, 0)

    # ---- 消息循环 ----
    def _pump(self):
        msg = wintypes.MSG()
        kept = 0
        dbg = os.environ.get("DSH_CU_DEBUG_MSG") == "1"
        while not self.done:
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                if dbg:
                    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           "..", "_preview", "msg.log"), "a",
                              encoding="utf-8") as fh:
                        # 线程消息的 hWnd 可能是 None，别让调试日志把对话框搞崩
                        fh.write("%s %s %s\n" % (hex(msg.message), msg.hWnd, msg.wParam))
                # 回车/Esc 在这里统一收口：子类化 EDIT 的窗口过程在这台机器上不生效
                # （SetWindowLongPtrW 换不上去），改成在消息循环里拦，见 2026-11-14 实测。
                if msg.message == WM_KEYDOWN and msg.hWnd == self.edit:
                    if msg.wParam == 0x0D:
                        self._send()
                        continue
                    if msg.wParam == 0x1B:
                        self._cancel()
                        continue
                if msg.message == WM_CHAR and msg.hWnd == self.edit and msg.wParam == 0x0D:
                    self._send()
                    continue
                if msg.message in (MSG_SEND, MSG_CANCEL):
                    if msg.message == MSG_SEND:
                        self._send()
                    else:
                        self._cancel()
                    continue
                if msg.message == WM_APP + 9:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.01)
                if dbg and kept % 120 == 0:
                    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           "..", "_preview", "msg.log"), "a",
                              encoding="utf-8") as fh:
                        fh.write("tick len=%s text=%r fg_edit=%s\n"
                                 % (user32.GetWindowTextLengthW(self.edit),
                                    self._text(), user32.GetForegroundWindow() == self.edit))
                # 输入框刚显示时经常抢不到焦点，隔 0.1s 再试几次（最多 1.5s）。
                if self._focus_tries < 15 and user32.GetForegroundWindow() != self.edit:
                    time.sleep(0.08)
                    self._focus_tries += 1
                    self._grab_focus()
            kept += 1
        obj = getattr(self, "_emit_obj", {"event": "dialog_closed"})
        try:
            self.card.hide()
            if self.edit:
                user32.DestroyWindow(self.edit)
        except Exception:
            pass
        _emit(obj)
        time.sleep(0.05)
        os._exit(0)


def dialog_main(spec_json):
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    DialogWin(json.loads(spec_json))


if __name__ == "__main__":
    dialog_main(sys.argv[1] if len(sys.argv) > 1 else "{}")

