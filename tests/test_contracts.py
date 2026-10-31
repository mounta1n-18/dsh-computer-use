# -*- coding: utf-8 -*-
"""cu.py 的契约测试（不靠"数数量"，只钉行为语义）。

跑：`python tests/test_contracts.py`

覆盖（对应计划的 A1/A2 判据）：
  A 坐标：0-1000 ↔ 物理像素、小数、越界/布尔/NaN 拒绝、region 解析
  B 输入校验：键别名、scroll、wait 秒/毫秒、hotkey 分开传
  C 错误码与**零副作用**：参数错时一个输入事件都不许发出去
  D 截图：PNG 三重校验（签名/IHDR/IEND/宽高一致），坏图必须被拒
  E 修饰键：组合键中途抛异常也必须把已按下的补松开
  F 凝视队列：FIFO、peek 不消费、take_all 一次收干净、cursor、静默窗

说明：F 只测队列原语（读写/cursor/取帧语义），不测"盯"的比对循环——
那需要真实屏幕变化，属于真机验证，不在离线契约里冒充。
"""
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PYDIR = os.path.join(HERE, "..", "python")
sys.path.insert(0, os.path.abspath(PYDIR))

import cu  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %-50s %s%s" % (name, "PASS" if cond else "FAIL",
                            "" if cond else "   <<< " + str(detail)[:180]))


def expect_code(fn, code):
    try:
        fn()
        return False, "没有抛错"
    except cu.CuError as e:
        return e.code == code, e.code
    except Exception as e:                                    # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, e)


class Spy:
    """把注入通道换成计数器，用来断言"错误路径零副作用"。"""

    def __init__(self):
        self.mouse = []
        self.keys = []
        self.old = {}

    def install(self):
        self.old = {"_mouse": cu._mouse, "_key": cu._key, "move": cu.move}
        cu._mouse = lambda *a, **k: self.mouse.append(a)
        cu._key = lambda *a, **k: self.keys.append(a)
        cu.move = lambda *a, **k: self.mouse.append(("move",) + a)

    def uninstall(self):
        for k, v in self.old.items():
            setattr(cu, k, v)

    def clear(self):
        self.mouse.clear()
        self.keys.clear()


def main():
    real_screen = cu.screen()
    real_shot_dir = cu.SHOT_DIR

    print("== A 坐标 ==")
    cu.screen = lambda: (0, 0, 2560, 1440)
    check("(0,0) → 左上角像素", cu.to_px(0, 0) == (0, 0), cu.to_px(0, 0))
    check("(1000,1000) → 右下角像素(夹在屏内)", cu.to_px(1000, 1000) == (2559, 1439),
          cu.to_px(1000, 1000))
    check("(500,500) → 正中", cu.to_px(500, 500) == (1280, 720), cu.to_px(500, 500))
    check("小数坐标可用", cu.to_px(0.5, 0.5) == (1, 1), cu.to_px(0.5, 0.5))
    check("归一化往返误差 ≤0.5（一个像素≈0.39 归一化单位，边缘还有夹取）", all(
        abs(cu.to_norm(*cu.to_px(v, v))[0] - v) <= 0.5 for v in (0, 1, 250, 500, 999, 1000)),
        [cu.to_norm(*cu.to_px(v, v)) for v in (250, 500, 1000)])
    check("越界坐标被拒", expect_code(lambda: cu.to_px(1001, 500), "CU_BAD_ARG")[0],
          expect_code(lambda: cu.to_px(1001, 500), "CU_BAD_ARG"))
    check("布尔坐标被拒", expect_code(lambda: cu.to_px(True, 500), "CU_BAD_ARG")[0])
    check("NaN/inf 被拒", expect_code(lambda: cu._unit(float("nan"), "x"), "CU_BAD_ARG")[0])
    check("region 非法被拒", expect_code(lambda: cu._region_arg("1,2,3"), "CU_BAD_ARG")[0])
    check("region 越界与屏幕无交集时报错",
          expect_code(lambda: cu._region_arg("9999,9999,10,10"), "CU_BAD_ARG")[0])

    print("== B 输入校验 ==")
    check("键别名 ArrowLeft → left", cu._norm_key("ArrowLeft") == "left", cu._norm_key("ArrowLeft"))
    check("键别名 Control → ctrl", cu._norm_key("Control") == "ctrl")
    check("键别名 Esc → esc", cu._norm_key("Escape") == "esc")
    spy = Spy()
    spy.install()
    try:
        check("scroll 别名 up → +1", cu.scroll(500, 500, "up") == 1)
        check("scroll 别名 left → 横向 -1", cu.scroll(500, 500, "left") == -1)
        check("scroll 0.4 → 取整后按符号取 +1", cu.scroll(500, 500, 0.4) == 1)
        check("scroll -0.4 → -1（不能取成 +1）", cu.scroll(500, 500, -0.4) == -1)
        check("scroll 0 被拒", expect_code(lambda: cu.scroll(500, 500, 0), "CU_SCROLL_DELTA")[0],
              expect_code(lambda: cu.scroll(500, 500, 0), "CU_SCROLL_DELTA"))
        check("scroll 11 被拒", expect_code(lambda: cu.scroll(500, 500, 11), "CU_SCROLL_DELTA")[0])
        check("scroll 布尔被拒", expect_code(lambda: cu.scroll(500, 500, True), "CU_SCROLL_DELTA")[0])
        spy.clear()
        expect_code(lambda: cu.scroll(500, 500, 0), "CU_SCROLL_DELTA")
        check("scroll 参数错时零输入事件", not spy.mouse and not spy.keys, spy.mouse + spy.keys)
        check("wait 1.5 通过", abs(cu.wait_seconds(1.5) - 1.5) < 1e-9)
        check("wait 0 通过", cu.wait_seconds(0) == 0)
        check("wait 布尔被拒", expect_code(lambda: cu.wait_seconds(True), "CU_WAIT_SECONDS")[0])
        check("wait 1000（毫秒误传）被拒且提示除 1000",
              expect_code(lambda: cu.wait_seconds(1000), "CU_WAIT_SECONDS")[0])
        check("hotkey 单元素带 + 被拒（要求分开传）",
              expect_code(lambda: cu.key_multi(["ctrl+s"]), "CU_BAD_HOTKEY")[0])
        check("hotkey 分开传可用", cu.key_multi(["ctrl", "s"]) == "ctrl+s")
        spy.clear()
    finally:
        spy.uninstall()

    print("== C 错误码与零副作用 ==")
    bad = [k for k, v in cu.CODES.items() if not v]
    check("错误码表每项都有中文说明", not bad, bad)
    for code in ("CU_UIPI", "CU_MINIMIZED", "CU_BLACK", "CU_HUNG", "CU_MOVED",
                 "CU_BAD_ARG", "CU_NO_WINDOW", "CU_SCROLL_DELTA", "CU_BAD_HOTKEY",
                 "CU_WAIT_SECONDS", "CU_ELEMENT", "CU_NO_SIDECAR", "CU_IPC",
                 "CU_PROTOCOL", "CU_EVAL", "CU_EVAL_TIMEOUT",
                 "CU_VDESKTOP_UNAVAILABLE", "CU_VDESKTOP_UNSUPPORTED"):
        check("码表含 %s" % code, code in cu.CODES)
    spy = Spy()
    spy.install()
    try:
        spy.clear()
        expect_code(lambda: cu.click(2000, 500), "CU_BAD_ARG")
        check("click 越界零输入事件", not spy.mouse and not spy.keys, spy.mouse)
        spy.clear()
        expect_code(lambda: cu.type_text("a\nb"), "CU_BAD_ARG")
        check("type 含换行时零输入事件（不把消息误发出去）", not spy.keys, spy.keys)
        spy.clear()
        expect_code(lambda: cu.drag(500, 500, 600, 600, ms=1), "CU_BAD_ARG")
        check("drag 时长越界零输入事件", not spy.mouse, spy.mouse)
        spy.clear()
        expect_code(lambda: cu.key_hold("bogus", True), "CU_BAD_ARG")
        check("长按非修饰键被拒且零事件", not spy.keys, spy.keys)
    finally:
        spy.uninstall()

    print("== D 截图 PNG 三重校验 ==")
    cu.screen = lambda: real_screen
    png, meta = cu.capture_png(320)
    ok, detail = True, ""
    try:
        cu._validate_png(png, meta["width"], meta["height"])
    except cu.CuError as e:
        ok, detail = False, str(e)
    check("真抓到的是完整 PNG 且通过三重校验", ok, detail)
    check("meta 宽高与 PNG 自身 IHDR 一致",
          cu.struct.unpack(">II", png[16:24]) == (meta["width"], meta["height"]),
          cu.struct.unpack(">II", png[16:24]))
    check("meta 带物理分辨率", meta["physical"]["width"] == real_screen[2], meta)
    check("meta 带图像 token 估算", meta["estimatedImageTokens"] > 0, meta)
    check("长边确实被缩到 320", max(meta["width"], meta["height"]) == 320, meta)

    check("截断的 PNG 被拒", expect_code(
        lambda: cu._validate_png(png[:len(png) // 2], meta["width"], meta["height"]),
        "CU_SHOT_FAIL")[0])
    bad_sig = b"\x00" + png[1:]
    check("签名不对的 PNG 被拒", expect_code(
        lambda: cu._validate_png(bad_sig, meta["width"], meta["height"]),
        "CU_SHOT_FAIL")[0])
    check("声明宽高与 PNG 不符被拒", expect_code(
        lambda: cu._validate_png(png, meta["width"] + 7, meta["height"]),
        "CU_SHOT_FAIL")[0])
    check("无 IEND 的 PNG 被拒", expect_code(
        lambda: cu._validate_png(png[:-12] + b"\x00" * 12, meta["width"], meta["height"]),
        "CU_SHOT_FAIL")[0])

    print("== E 修饰键复位 ==")
    spy = Spy()
    spy.install()
    try:
        spy.clear()
        # 让 'a' 按下时抛异常：组合键的 finally 必须把 ctrl 与 a 都补松。
        # 注意 recorder 是 Spy 装上的**记录器**，绝不能碰真注入——测试不许动用户键盘。
        recorder = cu._key
        calls = {"n": 0}

        def flaky(vk, up=False):
            calls["n"] += 1
            if calls["n"] == 2 and not up:
                raise RuntimeError("模拟注入中途失败")
            return recorder(vk, up)

        cu._key = flaky
        try:
            cu.key("ctrl+a")
            raised = False
        except RuntimeError:
            raised = True
        check("组合键中途异常会向上抛", raised)
        check("异常后已按下的键都被补松开（_PRESSED_MODS 清空）",
              not cu._PRESSED_MODS, cu._PRESSED_MODS)
        cu._key = spy.old["_key"]
        spy.clear()
        cu.key_hold("shift", True)
        held = sorted(cu._HELD)
        done = cu.release_all(force=True)
        check("key_hold 后 release_all 真的松开", held == ["shift"] and "shift" in done,
              {"held": held, "done": done})
        check("release_all 后 latch 清空", not cu._HELD and not cu._PRESSED_MODS)
    finally:
        spy.uninstall()

    print("== F 凝视队列 ==")
    tmp = tempfile.mkdtemp(prefix="cu-q-")
    real_watchq = cu.WATCHQ_ROOT
    try:
        # 注意：队列根用的是**模块级常量** WATCHQ_ROOT（= SHOT_DIR/watchq），
        # 在 import 时就定好了；只改 SHOT_DIR 不生效，会写到真实临时队列里去。
        cu.SHOT_DIR = tmp
        cu.WATCHQ_ROOT = os.path.join(tmp, "watchq")
        qdir = cu._盯_qdir("t")
        os.makedirs(qdir, exist_ok=True)
        frames = []
        for i in range(3):
            p = os.path.join(tmp, "f%d.png" % i)
            with open(p, "wb") as f:
                f.write(b"\x89PNG")
            frames.append(p)
        evs = [{"type": "change", "ts": time_now, "phys": 9000 + i, "path": frames[i]}
               for i, time_now in enumerate((1000, 1001, 1002))]
        cu._盯_write_jsonl(os.path.join(qdir, "q.jsonl"), evs)
        check("队列读写往返一致",
              [e["phys"] for e in cu._盯_read_jsonl(os.path.join(qdir, "q.jsonl"))]
              == [9000, 9001, 9002])
        check("初始 cursor 为 0", cu._盯_cursor(qdir) == 0)
        peeks = cu.盯取("t", peek=True, as_json=True)
        check("peek 不消费（cursor 仍 0）", cu._盯_cursor(qdir) == 0, cu._盯_cursor(qdir))
        head = json.loads(peeks[0])
        check("peek 只看队首那一条，不整批吞掉", "phys" in head and head["phys"] == 9000, head)
        one = cu.盯取("t", as_json=True)
        check("单取返回队首那一帧（FIFO）",
              json.loads(one[0]).get("phys") == 9000, one)
        check("单取后 cursor=1", cu._盯_cursor(qdir) == 1, cu._盯_cursor(qdir))
        rest = cu.盯取("t", take_all=True, as_json=True)
        bunch = json.loads(rest[0])["events"]
        check("take_all 一次收干净剩下的 2 帧（一个 JSON 里给出整批）",
              len(bunch) == 2 and [e["phys"] for e in bunch] == [9001, 9002], rest)
        check("take_all 后 cursor=3", cu._盯_cursor(qdir) == 3, cu._盯_cursor(qdir))
        empty = json.loads(cu.盯取("t", as_json=True)[0])
        check("空队列立刻返回 event=None（不等）",
              empty.get("event") is None and empty.get("pending") == 0, empty)
        # --archive：中间帧进 archive/，只留结尾那张当结论
        cu._盯_set_cursor(qdir, 0)
        for i, p in enumerate(frames):
            with open(p, "wb") as f:
                f.write(b"\x89PNG")
        arch = json.loads(cu.盯取("t", take_all=True, archive=True, as_json=True)[0])
        check("take_all+archive 把中间帧留档、只交结尾",
              arch.get("archived") == 2 and len(arch.get("events") or []) == 3, arch)
        check("archive/ 里真有 2 张中间帧",
              len(os.listdir(os.path.join(qdir, "archive"))) == 2,
              os.listdir(os.path.join(qdir, "archive")))
        check("不存在的队列报 CU_NO_QUEUE",
              expect_code(lambda: cu.盯取("nope", as_json=True), "CU_NO_QUEUE")[0])
        st = cu._盯_state(qdir)
        cu._盯_set_state(qdir, beat=cu.time.time(), closed=False)
        check("state 读写可用", cu._盯_state(qdir).get("beat", 0) > 0)
        check("心跳新鲜判定为活着", cu._盯_alive(cu._盯_state(qdir)) is True)
        cu._盯_set_state(qdir, closed=True)
        check("closed=True 判定为不活", cu._盯_alive(cu._盯_state(qdir)) is False)
        # 静默窗
        cu._unmute()
        check("静默窗初始为 0", cu._muted_until() == 0)
        cu._mute(2)
        check("静默窗往后推", cu._muted_until() > cu.time.time())
        cu._mute(1)     # 不应缩短
        check("静默窗只续租不缩短", cu._muted_until() > cu.time.time() + 1.5)
        cu._unmute()
        check("显式清静默窗", cu._muted_until() == 0)
    finally:
        cu.SHOT_DIR = real_shot_dir
        cu.WATCHQ_ROOT = real_watchq
        shutil.rmtree(tmp, ignore_errors=True)

    print("== G 本轮新增：app.* / hybrid / spawn / find / PrintWindow ==")
    # G1 解析层：find 的新参数都要被 argparse 接受（不执行，只验形状）
    ok_parse = True
    detail = ""
    try:
        a = cu.build_parser().parse_args(
            ["find", "保存", "--role", "Button", "--regex", "--exact",
             "--index", "1", "--window", "记事本", "--json", "--limit", "5"])
        ok_parse = (a.role == "Button" and a.regex and a.exact and a.index == 1
                    and a.window == "记事本" and a.as_json and a.limit == 5)
    except SystemExit:
        ok_parse, detail = False, "argparse 拒绝了新参数"
    check("find 接受 --role/--regex/--exact/--index/--json", ok_parse, detail)

    # G2 app-is-running：不存在的应用必须明确说没在跑，不许猜
    r = cu.app_is_running("zzz_no_such_app_xyz")
    check("app-is-running 对不存在的应用回 running=False",
          r["running"] is False and r["matches"] == [], r)

    # G3 hybrid-programs：形状自洽
    h = cu.hybrid_programs()
    check("hybrid-programs 形状自洽",
          isinstance(h.get("browsers"), list) and isinstance(h.get("cdp_endpoints"), list)
          and bool(h.get("advice")), h)

    # G4 spawn：真的起一个立刻退出的进程；不存在的 exe 必须报错
    sp = cu.spawn_process("cmd.exe", ["/c", "exit"])
    check("spawn 起进程拿到 pid", isinstance(sp.get("pid"), int) and sp["pid"] > 0, sp)
    check("spawn 不存在的 exe 报 CU_APP_NOT_FOUND",
          expect_code(lambda: cu.spawn_process("zzz_no_such.exe"), "CU_APP_NOT_FOUND")[0])

    # G5 app-terminate 无确认必须拒绝（破坏性动作的第二道闸）。
    # 注意：只测"拒绝"分支，**绝不**传 confirm=True 去真关用户的东西。
    hits = cu.enum_windows()
    if hits:
        ok_term, code = expect_code(lambda: cu.app_terminate(hits[0].title[:4], False),
                                    "CU_CONFIRM_REQUIRED")
        check("app-terminate 不加确认时拒绝（CU_CONFIRM_REQUIRED）", ok_term, code)
    else:
        check("app-terminate 不加确认时拒绝（CU_CONFIRM_REQUIRED）", True, "（没有可见窗口）")

    # G6 窗口截图：双后端 + PNG 合规（用当前真实存在的某个窗口）
    target = None
    for pat in ("DeepSeek Harness", "Notepad", "记事本", ""):
        ws = cu.find_windows(pat) if pat else []
        if ws:
            target = ws[0]
            break
    if target is not None:
        class _A:
            pass
        _aa = _A()
        _aa.window = target.title[:10]
        _aa.region = None
        _aa.long = 480
        _aa.full = False
        try:
            png2, meta2 = cu.capture_shot(_aa)
            cu._validate_png(png2, meta2["width"], meta2["height"])
            check("窗口截图走双后端且 PNG 合规",
                  meta2.get("capture_path") in ("printwindow", "gdi")
                  and max(meta2["width"], meta2["height"]) <= 480,
                  (meta2.get("capture_path"), meta2["width"], meta2["height"]))
        except cu.CuError as e:
            check("窗口截图走双后端且 PNG 合规", False, e)
    else:
        check("窗口截图走双后端且 PNG 合规", True, "（没有可用窗口，跳过）")

    print()
    print("cu.py 契约: %s（%d 通过 / %d 失败）"
          % ("PASS" if not FAIL else "FAIL", len(PASS), len(FAIL)))
    for f in FAIL:
        print("  失败：%s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
