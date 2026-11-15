"""回归：松手只松"自己按的"，绝不凭空发鼠标抬起。

2026-11-15 事故：用户重启 DSH 后鼠标每隔几秒"自己点右键"。
链路：低级钩子发现"用户在动鼠标" → 提示线程调 release_all() 想松手 →
当时实现里 **无条件** 发了一个从未按下过的 BUTTONUP；Windows 把孤立的
RBUTTONUP 当成一次完整右键点击，于是鼠标一动就弹右键菜单。

现在的契约：
1. 只松本进程登记过（_HELD / _HELD_BTNS）的键；
2. 没按过就一个事件都不发，release_all 是幂等的；
3. 进程被强杀留下的残局，靠 held.json 账本在下次启动时精确收尾（只松账本里的）。
"""
import json
import os
import sys
import tempfile

HOME = tempfile.mkdtemp(prefix="cu-held-")
os.environ["DSH_CU_HOME"] = HOME
os.environ["DSH_CU_ALLOW_REAL_HOTKEY_TEST"] = "0"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "python"))

import cu  # noqa: E402

sent = []
real_mouse = cu._mouse
real_move = cu.move

fails = []


def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg)
    if not ok:
        fails.append(msg)


def record(flags, *a, **kw):
    sent.append(flags)


def begin():
    sent.clear()
    cu._mouse = record


def end():
    cu._mouse = real_mouse
    cu._HELD.clear()
    cu._HELD_BTNS.clear()
    cu._persist_held()


# ---- 1. 什么都没按：一个事件都不发，而且幂等
begin()
out1 = cu.release_all()
out2 = cu.release_all()
end()
check(sent == [] and out1 == [] and out2 == [], "没按过时 release_all 不发任何鼠标事件（发了 %r）" % (sent,))

# ---- 2. 一次点击：下+上，结束后不再登记
begin()
cu.move = lambda *a, **kw: None
try:
    cu.click(0.5, 0.5)
finally:
    cu.move = real_move
check(sent == [cu.LDOWN, cu.LUP], "click 发 DOWN+UP 且顺序正确（实际 %r）" % (sent,))
check(not cu._HELD_BTNS, "click 结束后不残留按住状态")
after = cu.release_all()
end()
check(after == [], "click 之后 release_all 不再补发抬起")

# ---- 3. 拖拽被异常打断：finally 里仍然松手，且只松这一次
begin()
cu.move = lambda *a, **kw: None
try:
    cu.drag(0.1, 0.1, 0.2, 0.2, ms=50)
finally:
    cu.move = real_move
check(sent[0] == cu.LDOWN and sent[-1] == cu.LUP, "drag 以 DOWN 开始、以 UP 结束（实际 %r）" % (sent,))
check(not cu._HELD_BTNS, "drag 结束后不残留按住状态")
end()

# ---- 4. 上一个进程被强杀：账本里有左键+shift，启动时精确补松
held = os.path.join(HOME, "held.json")
with open(held, "w", encoding="utf-8") as f:
    json.dump({"keys": ["shift"], "buttons": [cu.LDOWN]}, f)
begin()
released = cu.recover_held()
end()
check(released == ["鼠标左键", "shift"],
      "recover_held 只松账本里的键（实际 %r）" % (released,))
check(not os.path.exists(held), "收尾后账本被清掉")

# ---- 5. 没有账本时 recover_held 什么都不做（绝不猜"可能卡住了"）
begin()
released = cu.recover_held()
end()
check(released == [] and sent == [], "没有账本时 recover_held 不发任何事件")

print("RESULT:", "ALL PASS" if not fails else "%d FAILED" % len(fails))
sys.exit(0 if not fails else 1)
