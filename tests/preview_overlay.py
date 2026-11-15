#!/usr/bin/env python
"""preview_overlay.py — 人工看效果用：起 overlay，逐个状态截图，然后退出。

    python tests/preview_overlay.py [输出目录]

不属于自动化测试（要真实桌面与光标），所以不在 run_all.py 里。
"""
import ctypes
import json
import os
import subprocess
import sys
import time

from PIL import ImageGrab

HERE = os.path.dirname(os.path.abspath(__file__))
PYDIR = os.path.join(HERE, "..", "python")
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "_preview")


def dpi_aware():
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass


def main():
    dpi_aware()
    os.makedirs(OUT, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(PYDIR, "overlay.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
        creationflags=0x08000000)
    ready = json.loads(proc.stdout.readline())
    print("ready:", ready)

    def send(**msg):
        proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def shot(name):
        time.sleep(0.6)
        img = ImageGrab.grab(all_screens=True)
        path = os.path.join(OUT, name + ".png")
        img.save(path)
        print("shot:", path, img.size)

    try:
        send(cmd="border", on=True, color="#FFB000", alpha=0.5)
        send(cmd="halo", on=True)
        send(cmd="logo", on=True, state="active")
        send(cmd="banner", text="代理「实验·cu 验收」想接管鼠标和键盘",
             sub="来自会话 4f2a9c · 工作目录 dsh-computer-use",
             note="同意后它会操作这台电脑；完成或空闲会自动交还。",
             seconds=30, buttons=True, tone="consent")
        shot("01_consent")

        send(cmd="banner", text="代理「实验·cu 验收」正在控制这台电脑",
             sub="来自会话 4f2a9c · 工作目录 dsh-computer-use",
             note="急停 Ctrl+Alt+Shift+Backspace · 暂停 Ctrl+Shift+Alt",
             seconds=4, buttons=False, tone="active")
        shot("02_active")

        send(cmd="banner", text="已暂停：agent 不再动鼠标键盘",
             sub="会话 4f2a9c41 · 目录 dsh-computer-use",
             note="按 Ctrl+Shift+Alt 继续 · 或点右下角鲸鱼给 agent 留言 · 急停 Ctrl+Alt+Shift+Backspace",
             seconds=0, buttons=False, tone="paused")
        send(cmd="halo", on=False)
        send(cmd="logo", on=True, state="paused")
        shot("03_paused")

        send(cmd="banner", text="已急停：所有电脑控制被阻断",
             sub="再按一次 Ctrl+Alt+Shift+Backspace 解除",
             note="解除后 agent 仍需重新征求你的同意",
             seconds=4, buttons=False, tone="emergency")
        send(cmd="border", on=True, color="#FF3B30", alpha=0.55)
        shot("04_emergency")

        # 对话框是独立进程（渲染进程里开 Tk 窗口会黑屏），所以这里单独起一份。
        dlg = subprocess.Popen(
            [sys.executable, os.path.join(PYDIR, "overlay.py"), "--dialog",
             json.dumps({"title": "对「实验·cu 验收」说点什么",
                         "hint": "已暂停鼠标键盘操作。发出后 agent 会继续，并看到你的这句话。"},
                        ensure_ascii=False)],
            cwd=PYDIR, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8")
        time.sleep(1.2)
        shot("05_dialog")
        dlg.kill()

        send(cmd="off")
        shot("06_off")
    finally:
        send(cmd="quit")
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    main()
