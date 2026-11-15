"""横幅按钮能不能真的点到（真机，一次跑完就退）。

用法：python tests/test_click.py
做法：起 overlay → 发一个带按钮的横幅 → 截屏找琥珀色"同意"按钮 → 真鼠标点它 →
      看 overlay 有没有回 {"event":"agree"}。
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import subprocess
import sys
import threading
import time

from PIL import ImageGrab

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python")
PY = sys.executable
u = ctypes.windll.user32
u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))


def main():
    # 这个测试会用真鼠标点屏幕，必须显式允许才跑（和热键测试同一套规矩）
    if os.environ.get("DSH_CU_ALLOW_REAL_CLICK_TEST") != "1":
        print("SKIP 真机点击测试（要跑请设 DSH_CU_ALLOW_REAL_CLICK_TEST=1）")
        return 0
    p = subprocess.Popen([PY, os.path.join(HERE, "overlay.py")],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, encoding="utf-8")
    lines = queue.Queue()

    def reader():
        for line in p.stdout:
            lines.put(line.strip())

    threading.Thread(target=reader, daemon=True).start()

    def send(**msg):
        p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        p.stdin.flush()

    def take(timeout):
        try:
            return lines.get(timeout=timeout)
        except queue.Empty:
            return ""

    print("ready:", take(8)[:80])
    send(cmd="border", on=True)
    send(cmd="logo", on=True)
    send(cmd="banner", text="代理「验收」想接管鼠标和键盘",
         sub="会话 abcd1234 · 目录 dsh-computer-use", note="30 秒后自动同意",
         seconds=30, buttons=True, tone="consent")
    time.sleep(1.5)

    img = ImageGrab.grab(all_screens=True)
    img.save(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                          "_preview", "click_before.png"))
    px = img.convert("RGB").load()
    xs, ys = [], []
    for y in range(0, 400, 2):          # 只看顶部横幅区域，别把四边渐变光算进来
        for x in range(0, img.width, 2):
            r, g, b = px[x, y]
            if r > 200 and 140 < g < 200 and b < 90:
                xs.append(x)
                ys.append(y)
    if not xs:
        print("FAIL 没找到琥珀色按钮（横幅没出来？）")
        send(cmd="quit")
        return 1
    cx, cy = sum(xs) // len(xs), sum(ys) // len(ys)
    print("click at", cx, cy)
    u.SetCursorPos(cx, cy)
    time.sleep(0.2)
    u.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.06)
    u.mouse_event(0x0004, 0, 0, 0, 0)

    got = take(4)
    print("event:", got)
    send(cmd="quit")
    try:
        p.wait(timeout=6)
    except subprocess.TimeoutExpired:
        p.kill()
    err = p.stderr.read()
    if err.strip():
        print("stderr:", err[:600])
    ok = '"agree"' in got
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
