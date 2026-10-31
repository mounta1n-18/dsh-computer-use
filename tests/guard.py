# -*- coding: utf-8 -*-
"""护栏：钉住 cu.py 的功能契约。任何改动后必须 PASS，否则回退。
签名只记「结构」，不记会自然变化的坐标/尺寸数字。"""
import json, os, re, subprocess, sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# 默认指向本插件自带的那份 cu.py；可用环境变量 CU_PY 覆盖（对照旧版时用）。
CU = os.environ.get("CU_PY") or os.path.join(_HERE, "..", "python", "cu.py")
CU = os.path.abspath(CU)
GOLD = os.path.join(_HERE, "golden.json")

def run(argv):
    p = subprocess.run([sys.executable, "-X", "utf8", CU] + argv, capture_output=True, timeout=90)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")

def sig_where(rc, out, err):   return "%d|%s" % (rc, "光标" if "光标" in out else "?")
def sig_probe(rc, out, err):
    keys = [k for k in ("屏幕", "完整性", "Pillow", "剪贴板", "光标", "可见窗口", "可输入桌面") if k in out]
    return "%d|%s" % (rc, ",".join(keys))
def sig_windows(rc, out, err):
    head = out.splitlines()[0] if out.strip() else ""
    return "%d|cols=%d" % (rc, head.count(" ") > 0 and len(re.findall(r"\s+", head)) or 0)
def sig_rect(rc, out, err):
    # 只看契约，不看环境：找不到窗口必须是 CU_NO_WINDOW；找到了必须给坐标。
    both = out + err
    if "CU_NO_WINDOW" in both: return "%d|no-window" % rc
    if "CU_MINIMIZED" in both: return "%d|minimized" % rc
    return "%d|%s" % (rc, "coords" if "像素 x,y,w,h" in out else "other")
def sig_shot(rc, out, err):    return "%d|%s" % (rc, "shot" if re.search(r"\d+x\d+\s+源", out) else "?")
def sig_key(rc, out, err):     return "%d|%s" % (rc, "已按" if "已按" in out else "?")
def sig_wait(rc, out, err):    return "%d|%s" % (rc, "等了" if "等了" in out else "?")
def sig_codes(rc, out, err):
    # 契约检查，不数个数：新增错误码是正常演进，不该让护栏误报。
    need = ("CU_UIPI", "CU_MINIMIZED", "CU_NO_WINDOW", "CU_BLACK", "CU_HUNG",
            "CU_MOVED", "CU_BAD_ARG", "CU_NO_PIL")
    missing = [c for c in need if c not in out]
    return "%d|%s" % (rc, ("missing:" + ",".join(missing)) if missing else "ok")
def sig_help(rc, out, err):
    for c in ("shot", "click", "post", "盯", "batch", "probe"):
        if c not in out: return "%d|MISSING:%s" % (rc, c)
    return "%d|ok" % rc
def sig_launch_unknown(rc, out, err):
    return "%d|%s" % (rc, "CU_APP_NOT_FOUND" if "CU_APP_NOT_FOUND" in out + err else "?")

CASES = [
    ("where",            ["where"],                       sig_where),
    ("probe",            ["probe"],                       sig_probe),
    ("windows",          ["windows"],                     sig_windows),
    ("rect-no-window",   ["rect", "__no_such_window_xyz__"], sig_rect),
    ("shot-region",      ["shot", "--region", "0,0,400,300", "--out", os.path.join(os.environ.get("TEMP","."), "g.png")], sig_shot),
    ("key-shift",        ["key", "shift"],                sig_key),
    ("wait",             ["wait", "0.1"],                 sig_wait),
    ("codes",            ["码"],                          sig_codes),
    ("help",             ["-h"],                          sig_help),
    ("bad-app",          ["launch", "zzz_no_such_app_xyz"], sig_launch_unknown),
]

def collect():
    got = {}
    for name, argv, fn in CASES:
        rc, out, err = run(argv)
        got[name] = fn(rc, out, err)
    return got

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    got = collect()
    if mode == "init":
        json.dump(got, open(GOLD, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("已记录黄金基线：")
        for k, v in got.items(): print("  %-16s %s" % (k, v))
        return 0
    want = json.load(open(GOLD, encoding="utf-8"))
    bad = 0
    for k in want:
        ok = got.get(k) == want[k]
        if not ok: bad += 1
        print("  %-16s %s   %s" % (k, got.get(k), "" if ok else "<<< 基线是 " + want[k]))
    print()
    print("护栏: %s（%d/%d 通过）" % ("PASS" if bad == 0 else "FAIL", len(want) - bad, len(want)))
    return 0 if bad == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
