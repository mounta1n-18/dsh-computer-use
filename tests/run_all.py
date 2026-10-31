# -*- coding: utf-8 -*-
"""跑完所有离线契约。任何一项 FAIL 就不许提交。

    python tests/run_all.py            # 默认：guard + 契约 + 常驻服务 + 虚拟桌面 + 插件端到端
    python tests/run_all.py --with-cdp # 额外跑浏览器 CDP 自测（会起一个 headless Chrome）

口径提醒：这里的每一项都是**契约检查**（结构/行为语义），不是"比数量"。
新增错误码、多一个动作，都不该让它们误报——只有行为退化才 FAIL。
"""
import os
import subprocess
import sys

# 中文 Windows 下 stdout 被管道接走时默认走 cp936，遇到 ▶ 这类字符会直接崩。
# 统一转 UTF-8，免得"测试脚本自己挂了"被误读成"被测代码挂了"。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

CASES = [
    ("guard（cu.py 旧契约，零丢失）", [PY, os.path.join(HERE, "guard.py")]),
    ("contracts（坐标/校验/错误码/截图/修饰键/凝视）", [PY, os.path.join(HERE, "test_contracts.py")]),
    ("server（常驻服务 + 传输层）", [PY, os.path.join(HERE, "test_server.py")]),
    ("desktop（虚拟桌面，打桩 + 真机报告）", [PY, os.path.join(HERE, "test_desktop.py")]),
    ("plugin（插件端到端，stub ctx）", ["node", os.path.join(HERE, "plugin.test.mjs")]),
]

OPTIONAL = [
    ("browser_use 包装层（会起 headless Chrome）",
     ["node", os.path.join(HERE, "browser-use.test.mjs")]),
    ("cdp 底层自测（会起 headless Chrome，47 项）",
     ["node", os.path.join(ROOT, "lib", "cdp", "selftest.mjs")]),
]


def run(name, argv):
    print("\n" + "=" * 70)
    print("▶ " + name)
    print("=" * 70)
    try:
        r = subprocess.run(argv, cwd=ROOT, timeout=900)
    except FileNotFoundError as e:
        print("  [SKIP] 找不到可执行文件：%s" % e)
        return None
    except subprocess.TimeoutExpired:
        print("  [FAIL] 超时（900s）")
        return False
    return r.returncode == 0


def main():
    with_cdp = "--with-cdp" in sys.argv
    results = []
    for name, argv in CASES:
        results.append((name, run(name, argv)))
    if with_cdp:
        for name, argv in OPTIONAL:
            results.append((name, run(name, argv)))

    print("\n" + "=" * 70)
    print("汇总")
    print("=" * 70)
    failed = 0
    for name, ok in results:
        tag = "PASS" if ok else ("SKIP" if ok is None else "FAIL")
        if ok is False:
            failed += 1
        print("  [%s] %s" % (tag, name))
    print()
    print("总判定：%s（%d 项，失败 %d）"
          % ("ALL PASS" if failed == 0 else "FAIL", len(results), failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
