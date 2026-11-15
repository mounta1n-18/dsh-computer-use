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

    print("RESULT:", "ALL PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
