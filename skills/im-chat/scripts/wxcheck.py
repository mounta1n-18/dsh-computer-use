#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""wxcheck —— 这条微信到底发出去没有。

`cu.py post` 返回的「已发送」只保证"文本进了输入框并回了车"：对方账号停用、被拉黑、
风控拦截时，微信会在气泡旁挂一个**红色感叹号**并给一行系统提示，而 post 看不见这些。
本脚本就是去补这一眼 —— **用脚本判，不用 token 判**。

用法:
  python wxcheck.py --window weixin --skip-left 0.32    # 微信主窗口（左边有会话列表）
  python wxcheck.py --window weixin                     # 独立聊天窗口（没有列表）
  python wxcheck.py --region 1141,185,777,1123          # 直接给像素区域
  python wxcheck.py --image shot.png                    # 判一张已有的截图
  python wxcheck.py --window weixin --json

判据: 找**小而饱和、且自己独立成块**的红块。微信失败标记是 ~20px 的红底白感叹号。
红包偏橙（G 通道高）过不了通道阈值；聊天记录里的红色图片/表情会成"大块"——
按行分段后逐块量形状，**大块不算标记，也就不会每次都把你叫起来读图**。

只有真找到"形状像标记"的小块才报「存疑」；画面里只有别的红色内容时报「未见标记」
（附带说明有几处红、为什么不算），你就不必再看了。

注意: `--window` 收的是"标题或进程名子串"，和 cu.py 一个规矩。**用进程名 `weixin` 最稳**，
因为中文标题可能撞上别的窗口（实测 DSH 界面自己的标题里就有"微信"两个字）。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

try:
    from PIL import Image, ImageChops, ImageGrab
except ImportError:
    print("[IM_NO_PIL] 需要 Pillow：pip install pillow", file=sys.stderr)
    sys.exit(2)

# 同级技能 computer-use 里的 cu.py：先看 CU_PY，再按本脚本位置推导，最后退回默认安装位置。
def _find_cu():
    env = os.environ.get("CU_PY")
    if env:
        return env
    sibling = Path(__file__).resolve().parents[2] / "computer-use" / "scripts" / "cu.py"
    if sibling.is_file():
        return str(sibling)
    return os.path.expanduser("~/.dsh/skills/computer-use/scripts/cu.py")


CU = _find_cu()
SHOT_DIR = os.path.join(tempfile.gettempdir(), "im-chat")

# 失败标记那种红：通道亮、绿蓝都低、红绿差得开。红包偏橙（G 高）过不了。
LUT_R = [255 if v > 180 else 0 for v in range(256)]
LUT_G = [255 if v < 110 else 0 for v in range(256)]
LUT_B = [255 if v < 110 else 0 for v in range(256)]
LUT_D = [255 if v > 80 else 0 for v in range(256)]

MARK_MAX_SIDE = 40      # 失败标记是 ~20px，给到 40 已很宽松
MARK_MAX_PX = 1200      # 一个 ~20px 的实心圆约 300px，留 4 倍余量


def _cu_rect(name):
    """问 cu.py 要窗口矩形。复用它的窗口匹配，免得两处逻辑跑偏。

    最小化窗口是个陷阱：Windows 会给它一个 x/y = -32000 的假矩形，而**宽高仍然大于 0**，
    所以"宽高是否为正"这条拦不住它。漏过去就会去截一块不存在的区域，然后报"未见失败
    标记"——判消息有没有真发出去的脚本给出假阴性，比不判更糟。这里三道闸：
    cu.py rect 自己的 CU_MINIMIZED、返回里的 CU_MINIMIZED 字样、-32000 数值兜底。
    """
    r = subprocess.run([sys.executable, CU, "rect", name],
                       capture_output=True, text=True, encoding="utf-8", timeout=30)
    out = r.stdout or ""
    if "CU_MINIMIZED" in out:
        raise SystemExit("[IM_MINIMIZED] 窗口 %r 现在是最小化的，先让它露出来" % name)
    m = re.search(r"像素 x,y,w,h = (-?\d+),(-?\d+),(-?\d+),(-?\d+)", out)
    if not m:
        raise SystemExit("[IM_NO_WINDOW] 拿不到窗口矩形：%s"
                         % ((out or r.stderr or "").strip()[:120] or name))
    x, y, w, h = (int(v) for v in m.groups())
    if w <= 0 or h <= 0:
        raise SystemExit("[IM_MINIMIZED] 窗口 %r 现在是最小化的，先让它露出来" % name)
    if x <= -32000 or y <= -32000 or x + w <= 0 or y + h <= 0:
        raise SystemExit("[IM_MINIMIZED] 窗口 %r 的坐标是 %d,%d（最小化假坐标），不能用"
                         % (name, x, y))
    return x, y, w, h


def _grab(args):
    if args.image:
        im = Image.open(args.image)
        im.load()
        return im.convert("RGB"), os.path.abspath(args.image)
    if args.region:
        vals = [int(v) for v in args.region.split(",")]
        if len(vals) != 4:
            raise SystemExit("[IM_BAD_ARG] --region 要写 x,y,w,h 四个整数")
        box = tuple(vals)
    else:
        box = _cu_rect(args.window)
    x, y, w, h = box
    if args.skip_left:
        cut = int(w * args.skip_left)
        x, w = x + cut, w - cut
    if w <= 0 or h <= 0:
        raise SystemExit("[IM_BAD_ARG] 裁完没有可看的区域了，检查 --skip-left")
    im = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
    im.load()
    os.makedirs(SHOT_DIR, exist_ok=True)
    p = os.path.join(SHOT_DIR, "wxcheck_%d.png" % int(time.time() * 1000))
    im.convert("RGB").save(p)
    return im.convert("RGB"), p


def _red_mask(im):
    r, g, b = im.split()[:3]
    m = ImageChops.multiply(r.point(LUT_R), ImageChops.subtract(r, g).point(LUT_D))
    m = ImageChops.multiply(m, g.point(LUT_G))
    return ImageChops.multiply(m, b.point(LUT_B))


def _bands(mask):
    """按行切成"独立的红块"，返回 [(y0,y1,x0,x1,像素数), ...]。

    只按行切不给列做连通域：够用，而且 `bytes.count/find` 都是 C 速度，整屏也就几十毫秒。
    代价写在明处——**同一横带上并排的两个红块会被并成一块**，于是宽度变大、判不出标记形状；
    所以这块逻辑偏保守的方向是"漏判成未见"，不是"误报成标记"。
    """
    w, h = mask.size
    data = mask.tobytes()
    out, cur = [], None
    for y in range(h):
        row = data[y * w:(y + 1) * w]
        n = row.count(255)
        if n:
            x0, x1 = row.find(255), row.rfind(255)
            if cur is None:
                cur = [y, y, x0, x1, n]
            else:
                cur[1] = y
                cur[2] = min(cur[2], x0)
                cur[3] = max(cur[3], x1)
                cur[4] += n
        elif cur is not None:
            out.append(tuple(cur))
            cur = None
    if cur is not None:
        out.append(tuple(cur))
    return out


def _is_mark(b):
    """这块像不像微信那个失败感叹号：小、**接近方/圆**、**实心**。

    只卡"两边 ≤40"是不够的——实测聊天里一张橙色卡片图会被按行切成十几条 22x4 的细条，
    细条两边都小，照样混进来。所以必须再加长宽比与实心度：标记是 ~20px 的实心圆
    （实测 bbox 22x22、413 像素、实心度 0.85），长条和空心/散点都不是它。
    """
    y0, y1, x0, x1, px = b
    w, h = x1 - x0 + 1, y1 - y0 + 1
    if not (8 <= min(w, h) and max(w, h) <= MARK_MAX_SIDE):
        return False
    if not (60 <= px <= MARK_MAX_PX):
        return False
    if not (0.4 <= w / float(h) <= 2.5):
        return False
    return px / float(w * h) >= 0.35


def main():
    ap = argparse.ArgumentParser(description="微信发送结果核验：有没有那个红色感叹号")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--window", help="标题或进程名子串（建议直接用 weixin）")
    src.add_argument("--region", help="x,y,w,h 物理像素")
    src.add_argument("--image", help="判一张已有的截图")
    ap.add_argument("--skip-left", type=float, default=0.0,
                    help="丢掉左边这么多比例（微信主窗口左边是会话列表，未读红点会误判；"
                         "独立聊天窗口或已 --skip-left 掉侧栏就不用给）")
    ap.add_argument("--json", action="store_true", dest="as_json")
    a = ap.parse_args()

    im, path = _grab(a)
    mask = _red_mask(im)
    total = mask.histogram()[255]
    bands = _bands(mask)
    marks = [b for b in bands if _is_mark(b)]

    if marks:
        m = max(marks, key=lambda b: b[4])
        verdict = "存疑：找到 %d 处形状像失败标记的小红块" % len(marks)
        hint = "最大的一处 %dx%d、%d 像素，在 本图 %d,%d —— 去看图确认" \
               % (m[3] - m[2], m[1] - m[0], m[4], m[2], m[0])
    elif total:
        verdict = "未见发送失败标记"
        hint = "（画面里有 %d 处红色内容，共 %d 像素，但形状都不是那个小标记——" \
               "多半是红包/表情/图片，不用管）" % (len(bands), total)
    else:
        verdict = "未见发送失败标记"
        hint = ""

    if a.as_json:
        print(json.dumps({"verdict": verdict, "red_px": total, "bands": len(bands),
                          "marks": len(marks), "image": path, "size": list(im.size)},
                         ensure_ascii=False))
        return 0

    print("%s · 红色像素 %d · 判的是 %s" % (verdict, total, path))
    print("  " + hint if hint else "  画面里没有红色，可以放心继续。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
