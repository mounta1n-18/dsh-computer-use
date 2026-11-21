"""美术层不截断：呼吸环、状态字、横幅卡片都不能贴边裁切。

2026-11-14 的教训：只看裁剪过的截图，环被画布切掉都没发现。这里直接量像素：
图片四条边的 alpha 必须全为 0，说明所有内容都在画布内、四周留了余量。
"""

import os
import sys

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python")
sys.path.insert(0, HERE)

import overlay  # noqa: E402


def edge_alpha(img):
    w, h = img.size
    a = img.split()[3]
    top = max(a.getpixel((x, 0)) for x in range(w))
    bottom = max(a.getpixel((x, h - 1)) for x in range(w))
    left = max(a.getpixel((0, y)) for y in range(h))
    right = max(a.getpixel((w - 1, y)) for y in range(h))
    return {"top": top, "bottom": bottom, "left": left, "right": right}


def main():
    ok = True

    for state in ("active", "paused"):
        frames = overlay.logo_frames(93, overlay.STATUS_ACTIVE, state, n=4)
        for i, img in enumerate(frames):
            e = edge_alpha(img)
            bad = {k: v for k, v in e.items() if v != 0}
            if bad:
                ok = False
                print("FAIL logo %s 帧%d 边缘有像素：%s（说明被画布裁了）" % (state, i, bad))
            else:
                print("PASS logo %s 帧%d 四边透明（%dx%d）" % (state, i, img.size[0], img.size[1]))

    # 状态字必须在图内：底部留白 > 0
    core, (w, total_h), d = overlay._logo_core(93, overlay.STATUS_ACTIVE, "active")
    a = core.split()[3]
    bottom_row = [a.getpixel((x, total_h - 1)) for x in range(w)]
    if max(bottom_row) != 0:
        ok = False
        print("FAIL 状态字贴到底边，底部没有留白")
    else:
        print("PASS 状态字底部留白正常")

    # 横幅：卡片四边也应全透明（阴影不触边）
    img, hits, pad = overlay.banner_image(
        "代理「验收」想接管鼠标和键盘", "会话 abcd1234 · 目录 dsh-computer-use",
        "同意后它会操作这台电脑；完成或空闲自动收回", 30, True, "consent", 1.0)
    e = edge_alpha(img)
    if any(v != 0 for v in e.values()):
        ok = False
        print("FAIL 横幅四边有像素：%s" % e)
    else:
        print("PASS 横幅四边透明（%dx%d）" % img.size)

    # 急停闪烁：包络必须是"平滑呼吸三下"，不是频闪。
    r = overlay.Renderer.__new__(overlay.Renderer)
    r.flash_t0 = 100.0
    r.flash_n = 3
    r.flash_color = overlay.RED
    ks = [r._flash_env(100.0 + i * 0.05)[1] for i in range(0, 27)]
    flash_ok = True
    if ks[0] > 0.3:
        flash_ok = False
        print("FAIL 闪烁起始不是从暗处升起：k=%.2f" % ks[0])
    if max(ks) < 0.95:
        flash_ok = False
        print("FAIL 闪烁没到峰值：max=%.2f" % max(ks))
    jumps = [abs(ks[i + 1] - ks[i]) for i in range(len(ks) - 1)]
    if jumps and max(jumps) > 0.55:
        flash_ok = False
        print("FAIL 闪烁跳变过大（疑似频闪）：max jump=%.2f" % max(jumps))
    if r._flash_env(100.0 + 0.42 * 3)[1] != 0.0:
        flash_ok = False
        print("FAIL 三下之后没有归零")
    if flash_ok:
        print("PASS 闪烁包络：平滑呼吸 3 下（起始 %.2f，峰值 %.2f，%.2fs 收尾）"
              % (ks[0], max(ks), 0.42 * 3))
    ok = ok and flash_ok

    # 四角：竖条的角格 = max(横, 纵)，既不多一层也不切断；接缝两侧数值连续。
    th = 60
    imgs = overlay.frame_images(800, 600, th, overlay.STATUS_ACTIVE, 0.95)
    left, right, top, bottom = imgs
    la = left.split()[3]
    ta = top.split()[3]
    corner_ok = True
    peak_val = la.getpixel((0, 0))
    if peak_val < 240:
        corner_ok = False
        print("FAIL 角上没到峰值（被两窗叠暗？）：%d" % peak_val)
    if la.getpixel((th - 1, 0)) != ta.getpixel((0, 0)):
        corner_ok = False
        print("FAIL 接缝不连续：竖条 %d vs 横条 %d"
              % (la.getpixel((th - 1, 0)), ta.getpixel((0, 0))))
    worst = max(la.getpixel((x, y)) for x in range(th) for y in (0, 1, 599 - 1))
    if worst > 245:
        corner_ok = False
        print("FAIL 角上有叠加发白：max=%d" % worst)
    # 沿上边缘走到角里，不该出现"先变暗再突然亮"的断点
    row = [la.getpixel((x, 0)) for x in range(th)]
    if min(row) < peak_val - 2:
        corner_ok = False
        print("FAIL 上边缘在角内出现下跌：min=%d peak=%d" % (min(row), peak_val))
    if corner_ok:
        print("PASS 四角 max 合成：无叠加、无断点（角格 %dx%d，接缝 %d→%d）"
              % (th, th, la.getpixel((th - 1, 0)), ta.getpixel((0, 0))))
    ok = ok and corner_ok

    print("RESULT:", "ALL PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
