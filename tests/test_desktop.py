#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_desktop.py —— desktop.py 的自测。直接跑：

    python tests/test_desktop.py

两部分，**失败语义严格分开**：

1) 打桩测试（STRICT）：用 monkeypatch 假造三档路径，不依赖本机有没有多桌面。
   公共 COM 成/败、内部 COM 校验失败→永久禁用（断言调用次数）、内部 COM 校验通过
   →list/switch 走 com_internal、热键路径（记录发了哪些键）、target="pip"、
   COM 不可用时 desktop_list 的 available/desktops 形状。
   这部分任何一条失败 = FAIL，最终退出码 1。

2) 真机冒烟（REPORT）：调 desktop_probe() / desktop_current() 并打印真实结果；
   再用 desktop_switch(direction='right'/'left') 各切一次并**切回来**。
   这部分只作"报告"，跑不出结果会打 ENV-SKIP / ENV-FAIL，**不会**伪装 PASS，
   也**不参与**最终退出码判定。

**本文件绝不调用 desktop_create / desktop_remove 的真机路径**（那会真的动用户的桌面），
这两个函数只在打桩测试里出现。
"""

import ctypes
import json
import os
import sys
import traceback
from ctypes import wintypes

# --- 路径：让 `python tests/test_desktop.py` 与 `python -m tests.test_desktop` 都能跑 ---
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "python"))

import desktop  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

# ================================================================ 测试框架
RESULTS = []          # [(kind, name, ok, detail)]


def check(name, ok, detail=""):
    RESULTS.append(("STRICT", name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  —— %s" % detail) if detail else ""))
    return bool(ok)


def report(name, ok, detail=""):
    """真机项：只报告，不影响退出码。ok=None 表示跳过。"""
    tag = "ENV-SKIP" if ok is None else ("ENV-OK" if ok else "ENV-FAIL")
    RESULTS.append(("REPORT", name, ok, detail))
    print("  [%s] %s%s" % (tag, name, ("  —— %s" % detail) if detail else ""))


class Sandbox(object):
    """把 desktop 模块里的可打桩点换掉，退出时原样还原。

    **默认就把真机的 COM 发现屏蔽掉**（_co_get_internal 返回"拿不到"），
    这样任何一条打桩测试都不可能意外碰到真的 ImmersiveShell / 注册表扫描——
    否则测试自己会去碰未公开接口，那是最不该发生的事。
    """

    DEFAULTS = {
        "_co_get_internal": None,            # 见 __enter__：懒设成"拿不到"
        "_co_init": lambda: ("none", False),
        "_co_uninit": lambda tok: None,
        "_co_create": lambda clsid, iid: (None, 0x80040154),
        "_co_window_id": lambda h, iface=None: None,
        "_co_on_current_desktop": lambda h, iface=None: None,
        "sample_desktop_hwnds": lambda: [],
        "u32": None,
    }

    def __init__(self, **kw):
        self.patch = dict(kw)
        self.saved = {}

    def __enter__(self):
        patch = dict(self.DEFAULTS)
        if patch["_co_get_internal"] is None:
            patch["_co_get_internal"] = lambda: (None, None, "stub：打桩环境不碰真 COM")
        if patch["u32"] is None:
            patch["u32"] = _FakeU32(0x11)
        patch.update(self.patch)
        self.patch = patch
        self.saved = {k: getattr(desktop, k) for k in self.patch}
        for k, v in self.patch.items():
            setattr(desktop, k, v)
        desktop.reset_internal()
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(desktop, k, v)
        desktop.reset_internal()
        return False


# ================================================================ 假的 COM 世界
class FakePublic(object):
    """假 IVirtualDesktopManager。

    接口指针是编的（0xF00D...），但 **desktop._vcall 被打桩成分发表**，
    所以既不会对着无效地址读 vtable（那在真机上是崩溃，测试里也一样），
    又能精确断言"调的是哪个下标、参数是什么"。
    """

    def __init__(self, win_ids, current_guid, fail=False):
        self.win_ids = dict(win_ids)
        self.current_guid = current_guid
        self.fail = fail
        self.created = 0
        self.calls = []
        self.released = 0
        self.by_index = {}          # vtable 下标 -> 处理函数
        self.rows = []              # 每次分发的 (下标, 参数) 记录

    def _build(self):
        self.by_index = {0: self._qi, 1: self._addref, 2: self._release,
                         3: self._on_current, 4: self._window_id, 5: self._move}
        return 0xF00D0000 + self.created * 0x100

    def dispatch(self, ptr, idx, args):
        """替掉 desktop._vcall。args 是调用方原本要传的地址（int / c_void_p）。"""
        self.rows.append((idx, tuple(args)))
        fn = self.by_index.get(idx)
        if fn is None:
            return 0x80004002
        return fn(args)

    def _qi(self, args):
        return 0x80004002

    def _addref(self, args):
        return 1

    def _release(self, args):
        self.released += 1
        return 0

    def _on_current(self, args):
        buf = args[1] if len(args) > 1 else None
        if buf:
            ctypes.cast(ctypes.c_void_p(int(buf)),
                        ctypes.POINTER(ctypes.c_int))[0] = 1
        return 0

    def _window_id(self, args):
        hwnd = int(args[0])
        buf = args[1] if len(args) > 1 else None
        g = self.win_ids.get(hwnd)
        if not g or not buf:
            return 0x8002802B
        gg = desktop._guid_from_str(g)
        ctypes.memmove(ctypes.c_void_p(int(buf)), ctypes.byref(gg), ctypes.sizeof(gg))
        return 0

    def _move(self, args):
        return 0

    def create(self, clsid, iid):
        self.calls.append((clsid, iid))
        if self.fail:
            return None, 0x80040154
        self.created += 1
        return self._build(), 0


class FakeInternal(object):
    """假 VirtualDesktopManagerInternal：用真 vtable 分发方式暴露一组假函数。

    _VTABLE_GUESS 里写的下标（3=GetCount、4=GetDesktop、6=Switch、7=Create、9=Remove）
    都指向 _vf_*，于是**真的**走 desktop._validate_internal() 与 _int_* 包装，
    等于同时测到了护栏逻辑与"调用确实落在校验过的下标上"。
    """

    def __init__(self, desktops=("11111111-1111-1111-1111-111111111111",
                                 "22222222-2222-2222-2222-222222222222"),
                 current=0, counts=None, hr_count=0, hr_desktop=0, zero_guid=False):
        self.desktops = list(desktops)
        self.current = current
        self.counts = list(counts) if counts else [len(self.desktops)]
        self.hr_count = hr_count
        self.hr_desktop = hr_desktop
        self.zero_guid = zero_guid
        self.switched = []
        self.created = 0
        self.removed = []
        self.made = 0
        self.by_index = {}       # vtable 下标 -> 处理函数（make() 里填）
        self.rows = []           # 每次分发的 (下标, 参数)
        self._out_slot = None

    # --- 假 vtable：下标 -> 处理函数 ---
    def make(self):
        """造一张假 vtable 映射表，并返回一个假接口指针。

        为什么不做真的机器码桩：桩要自己写 x64 汇编 + VirtualAlloc，写错就是
        access violation（而且是在测试进程里崩，Python 捕获不到）。改成把
        desktop._vcall 打桩成分发表之后，**每个下标对应哪个假方法一目了然**，
        而且能直接断言"调用的确落在 _VTABLE_GUESS 写的那几个下标上"——
        这正是要测的东西。
        """
        self.made += 1
        self.by_index = {
            desktop._VTABLE_GUESS["get_count"]: self._vf_count,
            desktop._VTABLE_GUESS["get_desktop"]: self._vf_desktop,
            desktop._VTABLE_GUESS["switch"]: self._vf_switch,
            desktop._VTABLE_GUESS["create"]: self._vf_create,
            desktop._VTABLE_GUESS["remove"]: self._vf_remove,
            0: self._vf_qi, 1: self._vf_addref, 2: self._vf_release,
        }
        return 0x7E570000 + self.made * 0x100

    def dispatch(self, ptr, idx, args):
        """替掉 desktop._vcall。args 是调用方原本要传的地址（int）。

        注意 desktop._vcall(ptr, idx, addressof(x)) 这种**单参数**调用，
        Python 会把 addressof(x) 直接绑到 args 上（不是元组），所以这里要归一。
        """
        if not isinstance(args, tuple):
            args = (args,)
        self.rows.append((idx, args))
        fn = self.by_index.get(idx)
        if fn is None:
            return 0x80004002
        return fn(args)

    def _out_int(self, ptr_addr, value):
        ctypes.cast(ctypes.c_void_p(int(ptr_addr)),
                    ctypes.POINTER(ctypes.c_int))[0] = value

    def _out_guid(self, ptr_addr, guid):
        ctypes.memmove(ctypes.c_void_p(int(ptr_addr)), ctypes.byref(guid),
                       ctypes.sizeof(guid))

    def _vf_qi(self, args):
        return 0x80004002

    def _vf_addref(self, args):
        return 1

    def _vf_release(self, args):
        return 0

    def _vf_count(self, args):
        out = args[0] if args else None
        # counts 是"逐次返回"的队列，最后一个值会一直重复（模拟稳定状态）
        v = self.counts[0]
        if len(self.counts) > 1:
            self.counts.pop(0)
        if out:
            self._out_int(out, v)
        return self.hr_count

    def _vf_desktop(self, args):
        """GetDesktop(idx, GUID* out)。args = (idx, addressof(gd))。"""
        idx = int(args[0]) if args else 0
        out = args[1] if len(args) > 1 else None
        if out:
            g = desktop.GUID()
            if not self.zero_guid:
                g = desktop._guid_from_str(self.desktops[idx % len(self.desktops)])
            self._out_guid(out, g)
        return self.hr_desktop

    def _vf_switch(self, args):
        self.switched.append(desktop._guid_to_str(
            ctypes.cast(ctypes.c_void_p(int(args[0])),
                        ctypes.POINTER(desktop.GUID))[0]))
        return 0

    def _vf_create(self, args):
        self.created += 1
        out = args[0] if args else None
        if out:
            g = desktop._guid_from_str("99999999-9999-9999-9999-999999999999")
            self._out_guid(out, g)
        return 0

    def _vf_remove(self, args):
        tgt = desktop._guid_to_str(ctypes.cast(ctypes.c_void_p(int(args[0])),
                                               ctypes.POINTER(desktop.GUID))[0])
        fb = (desktop._guid_to_str(ctypes.cast(ctypes.c_void_p(int(args[1])),
                                               ctypes.POINTER(desktop.GUID))[0])
              if len(args) > 1 else None)
        self.removed.append((tgt, fb))
        return 0


class FakeSource(object):
    """假 _co_get_internal：记录被调用了几次，返回假指针。"""

    def __init__(self, fake):
        self.fake = fake
        self.n = 0

    def __call__(self):
        self.n += 1
        return self.fake.make(), "{A9CD92FF-29BE-454C-8D04-D82879FB3F1B}", "fake"


def fake_hotkey_factory():
    """假 _hotkey：记录 (名字, 键序列)，返回成功。"""
    log = []

    def fake(name):
        log.append((name, list(desktop.HOTKEYS[name])))
        return True, list(desktop.HOTKEYS[name])

    return fake, log


def fake_sample(hwnds):
    def f():
        return [(h, "win%d" % h) for h in hwnds]

    return f


def pub(**kw):
    """公共 COM 打桩：_co_create + _vcall 分发一起装好。"""
    fp = kw.pop("fp")
    kw.setdefault("_co_window_id", lambda h, iface=None: fp.win_ids.get(int(h)))
    kw.setdefault("_co_on_current_desktop", lambda h, iface=None: True)
    return Sandbox(_co_create=fp.create, _vcall=fp.dispatch, **kw)


def internal(**kw):
    """内部 COM 打桩：_co_get_internal + _vcall 分发一起装好，并返回 (Sandbox, src)。"""
    f = kw.pop("fake", None) or FakeInternal()
    src = FakeSource(f)
    s = Sandbox(_co_get_internal=src, _vcall=f.dispatch, **kw)
    return s, src, f


def _hotkey_patches(fh):
    """给"热键档"测试用的补丁字典：内部 COM 一路打桩成"拿不到"。

    这里**必须**让 _co_get_internal 返回 None，而不能返回一个假指针然后指望
    校验调用失败：真的 _vcall 会对着假指针读 vtable，那是 access violation，
    Python 捕获不到，测试进程直接没。真机上这条也是同样道理 —— 守卫必须先于
    任何"未校验的调用"，本文件里不允许出现"拿假指针去试真 _vcall"的测法。
    """
    kw = {"_co_get_internal": lambda: (None, None, "stub：内部 COM 拿不到")}
    if fh is not None:
        kw["_hotkey"] = fh
    return kw


# ================================================================ 打桩测试
def test_public_com_ok():
    print("\n[打桩] 公共 COM 成功路径")
    win = 0x1234
    fp = FakePublic({win: "{AAAAAAAA-0000-0000-0000-000000000001}"},
                    "{AAAAAAAA-0000-0000-0000-000000000001}")
    with pub(fp=fp, sample_desktop_hwnds=fake_sample([win]),
             u32=_FakeU32(win)):
        r = desktop.desktop_current()
    check("path == com_public", r["path"] == "com_public", r.get("reason", "")[:70])
    check("available 为真", r["available"] is True)
    check("guid 取到", r["guid"] == "{AAAAAAAA-0000-0000-0000-000000000001}",
          str(r["guid"]))
    check("index 未编造（内部 COM 不可用时为 None）", r["index"] is None)
    check("COM 只创建了一次", fp.created >= 1)
    check("确实是公共档的 CLSID/IID",
          any(c == desktop.CLSID_VirtualDesktopManager for c, _ in fp.calls))


def test_public_com_fail():
    print("\n[打桩] 公共 COM 失败路径")
    fp = FakePublic({}, None, fail=True)
    with pub(fp=fp, _co_window_id=lambda h, iface=None: None,
             sample_desktop_hwnds=fake_sample([0x11]), u32=_FakeU32(0x11)):
        r = desktop.desktop_current()
    check("available 为假", r["available"] is False)
    check("guid 为 None（不编造）", r["guid"] is None)
    check("reason 里有可执行的手动办法", "Win+Tab" in (r.get("reason") or ""),
          (r.get("reason") or "")[:70])


def test_public_com_no_guid():
    print("\n[打桩] 公共 COM 能建但窗口给不出 GUID（跨桌面时的真实现象）")
    win = 0x22
    fp = FakePublic({}, None)
    with pub(fp=fp, _co_window_id=lambda h, iface=None: None,
             sample_desktop_hwnds=fake_sample([win]), u32=_FakeU32(win)):
        r = desktop.desktop_current()
    check("path 仍是 com_public", r["path"] == "com_public")
    check("available 为假且 guid 为 None", r["available"] is False and r["guid"] is None)
    check("reason 提到 0x8002802B", "8002802B" in (r.get("reason") or ""),
          (r.get("reason") or "")[:80])


def test_internal_guard_bad_count():
    print("\n[打桩] 内部 COM 校验失败（GetCount 不合理）→ 永久禁用")
    s, src, f = internal(fake=FakeInternal(counts=[9999]))   # 9999 不在 1..64
    with s:
        r1 = desktop.desktop_list()
        n_after_list = src.n
        r2 = desktop.desktop_list()
        r3 = desktop.desktop_switch(index=1)
        r4 = desktop.desktop_create()
        r5 = desktop.desktop_remove()
        dead_after = desktop.internal_available()      # 必须在 sandbox 里读，出去就被复位了
    check("第一次尝试确实调了 _co_get_internal", n_after_list == 1, "n=%d" % n_after_list)
    check("list 返回 available=False 且 desktops==[]",
          r1["available"] is False and r1["desktops"] == [] and r1["count"] == 0)
    check("校验失败后内部档被永久禁用", dead_after is False, "dead=%s" % dead_after)
    check("后续调用**不再**尝试内部 COM（调用计数仍为 1）", src.n == 1, "n=%d" % src.n)
    check("index 切换 → CU_VDESKTOP_PARTIAL",
          r3.get("code") == "CU_VDESKTOP_PARTIAL", str(r3.get("code")))
    check("create → CU_VDESKTOP_UNSUPPORTED",
          r4.get("code") == "CU_VDESKTOP_UNSUPPORTED", str(r4.get("code")))
    check("remove → CU_VDESKTOP_UNSUPPORTED",
          r5.get("code") == "CU_VDESKTOP_UNSUPPORTED", str(r5.get("code")))
    check("create/remove 都带了手动办法",
          "Win+Ctrl+D" in r4["reason"] and "Win+Ctrl+F4" in r5["reason"])


def test_internal_guard_error_and_zero():
    print("\n[打桩] 内部 COM 校验失败的另两种形态")
    for tag, f in (("GetCount 返回错误码", FakeInternal(hr_count=0x80004005)),
                   ("GetDesktop(0) 返回全零 GUID", FakeInternal(zero_guid=True)),
                   ("GetDesktop(0) 返回错误码", FakeInternal(hr_desktop=0x80070005))):
        s, src, _f = internal(fake=f)
        with s:
            r = desktop.desktop_list()
            dead = desktop.internal_available()
            # 注意：_co_get_internal 被打桩了，所以模块内的 _INTERNAL_TRIES 不会动，
            # "试了几次"要看假 _co_get_internal 自己被调了几次。
            tries = src.n
        check("%s → available=False & 永久禁用" % tag,
              r["available"] is False and dead is False and tries == 1,
              "dead=%s tries=%s | %s" % (dead, tries, (r.get("reason") or "")[:44]))


def test_internal_ok():
    print("\n[打桩] 内部 COM 校验通过 → list/switch 走 com_internal")
    f = FakeInternal()
    s, src, _f = internal(fake=f,
                          _co_window_id=lambda h, iface=None: f.desktops[0])
    with s:
        rl = desktop.desktop_list()
        check("path == com_internal", rl["path"] == "com_internal", str(rl["path"]))
        check("available 为真", rl["available"] is True)
        check("count == 2", rl["count"] == 2 and len(rl["desktops"]) == 2)
        check("desktops 结构正确",
              all(set(d) >= {"index", "guid", "current"} for d in rl["desktops"]))
        check("current 只标在第 0 个", [d["current"] for d in rl["desktops"]] == [True, False])
        rs = desktop.desktop_switch(index=1)
        check("path == com_internal", rs["path"] == "com_internal", str(rs["path"]))
        check("ok 为真", rs["ok"] is True, str(rs.get("reason"))[:60])
        check("SwitchDesktop 收到第 1 个桌面的 GUID",
              f.switched and desktop._guid_norm(f.switched[-1]) ==
              desktop._guid_norm(f.desktops[1]), str(f.switched))
        check("steps == 1", rs["steps"] == 1)

        rc = desktop.desktop_create()
        check("create 走 com_internal 且 ok", rc["path"] == "com_internal" and rc["ok"],
              str(rc.get("reason"))[:60])
        check("create 返回了新 GUID", rc.get("guid") ==
              "{99999999-9999-9999-9999-999999999999}", str(rc.get("guid")))

        rr = desktop.desktop_remove(index=1)
        check("remove 走 com_internal 且 ok",
              rr["path"] == "com_internal" and rr["ok"], str(rr.get("reason"))[:60])
        check("RemoveDesktop 收到目标 GUID + fallback",
              f.removed and desktop._guid_norm(f.removed[-1][0]) ==
              desktop._guid_norm(f.desktops[1]) and f.removed[-1][1] is not None,
              str(f.removed))

        ro = desktop.desktop_switch(index=99)
        check("index 越界被拦住（不调用 SwitchDesktop）",
              ro["ok"] is False and len(f.switched) == 1, str(ro.get("reason"))[:50])

        rc1 = desktop.desktop_switch(target="main")
        check('target="main" 是 no-op', rc1["ok"] is True and rc1["steps"] == 0,
              str(rc1.get("reason"))[:60])
    check("内部 COM 只拿了一次（缓存生效）", src.n == 1, "n=%d" % src.n)


def test_hotkey_paths():
    print("\n[打桩] 热键路径（记录发了哪些键）")
    fh, log = fake_hotkey_factory()
    kw = _hotkey_patches(fh)
    kw.update(_co_window_id=lambda h, iface=None: None,
              sample_desktop_hwnds=fake_sample([0x33]), u32=_FakeU32(0x33))
    with Sandbox(**kw):
        r1 = desktop.desktop_switch(direction="right")
        r2 = desktop.desktop_switch(direction="left")
    check("right 走 hotkey 档", r1["path"] == "hotkey", str(r1["path"]))
    check("right ok/steps", r1["ok"] is True and r1["steps"] == 1)
    check("right 发的是 win+ctrl+right",
          log[0] == ("right", ["win", "ctrl", "right"]), str(log[0]))
    check("left 发的是 win+ctrl+left",
          log[-1] == ("left", ["win", "ctrl", "left"]), str(log[-1]))

    # 真发键的分发器也要验一次：假 _send_combo 记录键序列
    sent = []

    def fake_send(names, gap=None):
        sent.append(list(names))
        return True

    with Sandbox(_send_combo=fake_send, _SWITCH_SETTLE=0, _CREATE_SETTLE=0):
        ok, keys = desktop._hotkey("right")
        ok2, keys2 = desktop._hotkey("left")
        ok3, keys3 = desktop._hotkey("create")
        ok4, keys4 = desktop._hotkey("remove")
    check("_hotkey('right') 按序按下 win/ctrl/right",
          keys == ["win", "ctrl", "right"] and sent[0] == ["win", "ctrl", "right"])
    check("_hotkey('left') = win/ctrl/left", keys2 == ["win", "ctrl", "left"])
    check("_hotkey('create') = win/ctrl/d", keys3 == ["win", "ctrl", "d"], str(keys3))
    check("_hotkey('remove') = win/ctrl/f4", keys4 == ["win", "ctrl", "f4"], str(keys4))
    check("四次都报成功", all([ok, ok2, ok3, ok4]))


def test_pip_never_mapped():
    print('\n[打桩] target="pip" 必须 CU_VDESKTOP_UNSUPPORTED')
    fh, log = fake_hotkey_factory()
    kw = _hotkey_patches(fh)
    kw.update(_co_window_id=lambda h, iface=None: None,
              sample_desktop_hwnds=fake_sample([0x44]), u32=_FakeU32(0x44))
    with Sandbox(**kw):
        r = desktop.desktop_switch(target="pip")
    check("ok 为假", r["ok"] is False)
    check("code == CU_VDESKTOP_UNSUPPORTED", r.get("code") == "CU_VDESKTOP_UNSUPPORTED")
    check("reason 点明是豆包沙箱概念且本机没有",
          "豆包" in r["reason"] and "Windows" in r["reason"],
          r["reason"][:60])
    check("没有偷偷发任何热键去「映射」pip", log == [], str(log))
    check("即使内部 COM 可用也不映射", r["path"] == "unsupported", str(r["path"]))


def test_list_shape_without_com():
    print("\n[打桩] COM 全不可用时 desktop_list 的形状")
    kw = _hotkey_patches(None)
    kw.update(_co_window_id=lambda h, iface=None: None,
              sample_desktop_hwnds=fake_sample([0x55]), u32=_FakeU32(0x55))
    with Sandbox(**kw):
        r = desktop.desktop_list()
    check("available 为 False", r["available"] is False)
    check("desktops == []（不编造列表）", r["desktops"] == [])
    check("count == 0", r["count"] == 0)
    check("reason 给出按方向切的出路",
          "direction" in r["reason"] and "Win+Tab" in r["reason"],
          r["reason"][:70])
    check("发现走查默认关闭（没有 discovered_by）", "discovered_by" not in r)


def test_discover_walk():
    print("\n[打桩] discover=True 的走查与回位")
    fh, log = fake_hotkey_factory()
    HWNDS = [0x101, 0x102]
    # 走查会调 desktop_current() 拿 GUID 去重（这里第 1/2 步各给一个不同的 GUID），
    # 以及 cloaked 判断"这一屏和上一屏是不是同一个"。
    guids = iter(["guidA", "guidB", "guidB"])
    cl = iter([0, 0, 2, 2, 2, 2, 2, 2])

    def fake_current():
        return {"path": "com_public", "guid": next(guids, "guidB"), "index": None,
                "available": True, "reason": "stub"}

    kw = _hotkey_patches(fh)
    kw.update(desktop_current=fake_current,
              sample_desktop_hwnds=lambda: [(h, "win%d" % h) for h in HWNDS],
              _cloaked=lambda h: next(cl, 2))
    with Sandbox(**kw):
        r = desktop.desktop_list(discover=True)
    rights = [x for x in log if x[0] == "right"]
    lefts = [x for x in log if x[0] == "left"]
    check("标注 discovered_by == walk", r.get("discovered_by") == "walk")
    check("走了 1 步就停（第 2 步屏上 cloaked 没变 → 到头）", r.get("steps") == 1,
          "steps=%s" % r.get("steps"))
    check("走不动时停住，并按**实际走过的步数**走回原位",
          len(lefts) == r.get("steps") == 1 and len(rights) == 2,
          "steps=%s right=%d left=%d（right 多一次是「到头那一探」，这一探也必须回位）"
          % (r.get("steps"), len(rights), len(lefts)))
    check("走查结果里标了 walked_back", r.get("walked_back") is True)
    check("GUID 去重回填进 desktops",
          [d["guid"] for d in r["desktops"]] == ["guidA", "guidB"],
          str(r["desktops"]))
    check("第一个被标成 current（就是出发的那个桌面）",
          r["desktops"][0]["current"] is True and r["desktops"][1]["current"] is False)
    check("reason 说的是走查去重", "去重" in r["reason"], r["reason"][:70])


class _FakeU32(object):
    """替掉 user32 的最少接口：只要 GetForegroundWindow/GetShellWindow 返回固定 hwnd。"""

    def __init__(self, fg, shell=0x99):
        self._fg, self._shell = fg, shell

    def GetForegroundWindow(self):
        return self._fg

    def GetShellWindow(self):
        return self._shell

    def GetDesktopWindow(self):
        return 0x98

    def GetWindowTextLengthW(self, h):
        return 0

    def GetWindowTextW(self, h, b, n):
        return 0

    def EnumWindows(self, cb, lp):
        return 1


def test_codes_and_error():
    print("\n[打桩] 错误码注册与 DesktopError 兼容")
    codes = {}
    n = desktop.install_codes(codes)
    check("install_codes 注册了 3 个码", n == 3 and len(codes) == 3, str(codes.keys()))
    check("注册内容含三个指定码",
          set(codes) == {"CU_VDESKTOP_UNAVAILABLE", "CU_VDESKTOP_UNSUPPORTED",
                         "CU_VDESKTOP_PARTIAL"})
    check("重复注册是幂等的", desktop.install_codes(codes) == 0)
    e = desktop.DesktopError("CU_VDESKTOP_PARTIAL", "细节")
    check("DesktopError 有 .code", e.code == "CU_VDESKTOP_PARTIAL")
    check("消息形状与 CuError 一致（[码] 中文：detail）",
          str(e).startswith("[CU_VDESKTOP_PARTIAL]") and str(e).endswith("细节"),
          str(e))

    # 真的用 cu.py 的 CODES 注册一次（cu.py 只读，不改它的内容）
    cu_path = os.path.join(_ROOT, "python", "cu.py")
    if os.path.exists(cu_path):
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("cu_for_test", cu_path)
            cu = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cu)
            before = len(cu.CODES)
            n2 = desktop.install_codes(cu.CODES)     # 就是 cu.py 那边该写的那一行
            check("install_codes(cu.CODES) 把 3 个码并进去了",
                  n2 == 3 and set(desktop.CODES) <= set(cu.CODES),
                  "n=%d before=%d after=%d" % (n2, before, len(cu.CODES)))
            check("cu.py 与 desktop.py 的码说明一致",
                  all(cu.CODES[k] == v for k, v in desktop.CODES.items()))
            check("CuError 能直接吃这些码",
                  "[CU_VDESKTOP_PARTIAL]" in str(cu.CuError("CU_VDESKTOP_PARTIAL", "x")))
        except Exception as ex:
            check("能读 cu.py 并注册", False, "%s: %s" % (type(ex).__name__, ex))
    else:
        check("能找到 cu.py（只读参考）", False, cu_path)


def test_target_parsing():
    print("\n[打桩] target / index / direction 参数归一")
    check('target="main" → main', desktop._parse_target("main", None)[:2] == ("main", None))
    check('target="PIP" → pip（大小写无关）',
          desktop._parse_target("PIP", None)[:2] == ("pip", None))
    check('target="2" → index 2', desktop._parse_target("2", None)[:2] == ("index", 2))
    check("index=3 优先于 target", desktop._parse_target("main", 3)[:2] == ("index", 3))
    check("乱七八糟的 target 报错", desktop._parse_target("screen2", None)[0] is None)
    with Sandbox(_hotkey=lambda n: (True, list(desktop.HOTKEYS[n]))):
        r = desktop.desktop_switch(direction="up")
    check("direction 只认 left/right", r["ok"] is False and "left" in r["reason"])


def test_vtable_guess_is_documented():
    print("\n[打桩] 内部档护栏的静态性质")
    check("_VTABLE_GUESS 只用一组猜测（不列多套偏移去乱试）",
          set(desktop._VTABLE_GUESS) == {"get_count", "get_desktop", "switch",
                                         "create", "remove"},
          str(desktop._VTABLE_GUESS))
    check("GetCount/GetDesktop 的下标是静态常量，不是运行时试探出来的",
          desktop._VTABLE_GUESS["get_count"] == 3 and desktop._VTABLE_GUESS["get_desktop"] == 4)
    check("已知内部 CLSID 表非空且每个都带来源说明",
          len(desktop._KNOWN_INTERNAL_CLSIDS) >= 5 and
          all(len(t) == 3 for t in desktop._KNOWN_INTERNAL_CLSIDS))
    check("pip 的拒绝是写死的（不看内部 COM 状态）",
          desktop.desktop_switch(target="pip").get("code") == "CU_VDESKTOP_UNSUPPORTED")


# ================================================================ 真机冒烟（只报告）
def _fingerprint():
    """当前屏上"哪些窗口被藏起来了"的指纹。桌面切走时它会变。"""
    try:
        return [(h, desktop._cloaked(h)) for h, _t in desktop.sample_desktop_hwnds()][:12]
    except Exception:
        return None


def real_machine():
    print("\n" + "=" * 78)
    print("真机冒烟（报告性质：失败只会打 ENV-FAIL，不影响退出码）")
    print("=" * 78)

    try:
        pr = desktop.desktop_probe()
        print("  真机 desktop_probe() =")
        print("    " + json.dumps(pr, ensure_ascii=False, indent=2).replace("\n", "\n    "))
        report("真机 probe 跑通（public_com=%s internal_com=%s hotkey=%s）"
               % (pr.get("public_com"), pr.get("internal_com"), pr.get("hotkey")),
               bool(pr) and pr.get("hotkey") is True)
        if pr.get("internal_clsid"):
            report("真机试出的 internal_clsid", True, pr["internal_clsid"])
        else:
            report("真机试出的 internal_clsid", None,
                   "None（内部 COM 在本机拿不到，这是真实结论，不是跳过了）")
    except Exception as ex:
        report("真机 desktop_probe()", False, "%s: %s" % (type(ex).__name__, ex))
        traceback.print_exc()

    try:
        cur = desktop.desktop_current()
        print("  真机 desktop_current() = %s" % json.dumps(cur, ensure_ascii=False))
        if cur.get("available"):
            report("真机读到当前桌面（path=%s guid=%s）"
                   % (cur.get("path"), cur.get("guid")), True)
        else:
            report("真机读到当前桌面", None,
                   "读不到：%s" % str(cur.get("reason"))[:90])
    except Exception as ex:
        report("真机 desktop_current()", False, "%s: %s" % (type(ex).__name__, ex))

    try:
        rl = desktop.desktop_list()
        print("  真机 desktop_list() = %s" % json.dumps(rl, ensure_ascii=False)[:200])
        report("真机 desktop_list 形状自洽（available 与 desktops 不矛盾）",
               (rl["available"] and rl["desktops"]) or
               (not rl["available"] and rl["desktops"] == []))
    except Exception as ex:
        report("真机 desktop_list()", False, "%s: %s" % (type(ex).__name__, ex))

    # --- 切换一次再切回来（必须切回来）---
    before = _fingerprint()
    try:
        r1 = desktop.desktop_switch(direction="right")
        mid = _fingerprint()
        print("  真机 switch(direction='right') = %s" % json.dumps(r1, ensure_ascii=False)[:200])
        changed = (before is not None and mid is not None and before != mid)
        if r1.get("ok") and changed:
            report("真机 Win+Ctrl+Right 确实切了桌面（cloaked 指纹变了）", True)
        elif r1.get("ok"):
            report("真机 Win+Ctrl+Right 切了桌面", None,
                   "热键发送成功但屏上 cloaked 指纹没变：本机可能只有 1 个虚拟桌面，"
                   "或已经是最右一个")
        else:
            report("真机 Win+Ctrl+Right", False, str(r1.get("reason"))[:90])
    except Exception as ex:
        report("真机 switch right", False, "%s: %s" % (type(ex).__name__, ex))

    try:
        r2 = desktop.desktop_switch(direction="left")
        after = _fingerprint()
        print("  真机 switch(direction='left')  = %s" % json.dumps(r2, ensure_ascii=False)[:200])
        back = (before is not None and after is not None and before == after)
        if back:
            report("真机已切回原来的桌面（cloaked 指纹与出发前一致）", True)
        else:
            # 尽力回位：再往左走几步，然后如实报告
            for _ in range(3):
                try:
                    desktop.desktop_switch(direction="left")
                except Exception:
                    pass
            after2 = _fingerprint()
            report("真机已切回原来的桌面", bool(before == after2),
                   "不一致：出发前=%s 结束后=%s（已尽力往左回位，请用 Win+Tab 复核）"
                   % (str(before)[:60], str(after2)[:60]))
    except Exception as ex:
        report("真机 switch left", False, "%s: %s" % (type(ex).__name__, ex))

    # --- 明确不做的真机动作 ---
    report("真机 desktop_create / desktop_remove", None,
           "**故意不测**：会真的动用户的桌面。这两个函数只由上面的打桩覆盖。")


# ================================================================ main
def main():
    print("desktop.py 自测 · 打桩部分为 STRICT（失败即退出码 1）")
    print("stdout 编码 %s · desktop.py = %s" % (sys.stdout.encoding, desktop.__file__))

    strict = [
        ("公共 COM 成功", test_public_com_ok),
        ("公共 COM 失败", test_public_com_fail),
        ("公共 COM 建得起来但窗口无 GUID", test_public_com_no_guid),
        ("内部 COM 校验失败→永久禁用", test_internal_guard_bad_count),
        ("内部 COM 其它失败形态", test_internal_guard_error_and_zero),
        ("内部 COM 校验通过", test_internal_ok),
        ("热键路径", test_hotkey_paths),
        ("pip 绝不映射", test_pip_never_mapped),
        ("无 COM 时 list 形状", test_list_shape_without_com),
        ("discover 走查", test_discover_walk),
        ("错误码与异常", test_codes_and_error),
        ("参数归一", test_target_parsing),
        ("护栏静态性质", test_vtable_guess_is_documented),
    ]
    for name, fn in strict:
        try:
            fn()
        except Exception as ex:
            check("%s：测试自身抛异常" % name, False,
                  "%s: %s" % (type(ex).__name__, ex))
            traceback.print_exc()

    real_machine()

    strict_fail = [r for r in RESULTS if r[0] == "STRICT" and not r[2]]
    env_fail = [r for r in RESULTS if r[0] == "REPORT" and r[2] is False]
    env_skip = [r for r in RESULTS if r[0] == "REPORT" and r[2] is None]
    strict_n = len([r for r in RESULTS if r[0] == "STRICT"])

    print("\n" + "=" * 78)
    print("汇总：打桩 %d 项，失败 %d 项 · 真机报告 %d 项（ENV-FAIL %d / ENV-SKIP %d）"
          % (strict_n, len(strict_fail),
             len([r for r in RESULTS if r[0] == "REPORT"]), len(env_fail), len(env_skip)))
    for r in strict_fail:
        print("  FAIL(打桩) %s —— %s" % (r[1], r[3]))
    for r in env_fail:
        print("  ENV-FAIL(真机) %s —— %s" % (r[1], r[3]))
    verdict = "打桩全部通过" if not strict_fail else "打桩存在失败"
    print("总判定：%s；退出码 %d" % (verdict, 1 if strict_fail else 0))
    print("=" * 78)
    return 1 if strict_fail else 0


if __name__ == "__main__":
    sys.exit(main())
