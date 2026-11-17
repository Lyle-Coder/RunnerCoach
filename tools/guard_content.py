# -*- coding: utf-8 -*-
"""构建前的剧情内容守卫（命令行入口）。

用法：

    python tools/guard_content.py          # 扫会进 APK 的目录
    python tools/guard_content.py --all    # 扫整仓（含 docs / preview）

退出码 0 = 干净，1 = 发现剧情内容混进了客户端。

判定规则在 `garmin_cn/contentguard.py` 里，这个脚本只负责报错和退出码，
好让 `build.bat` 能在打包前拦住。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from garmin_cn import contentguard as CG  # noqa: E402


def main(argv: list[str]) -> int:
    wide = "--all" in argv
    dirs = ("garmin_cn", "android", "server", "docs", "tools") if wide else CG.DEFAULT_DIRS
    issues = CG.scan(ROOT, dirs)
    if not issues:
        print("[ok] 客户端目录里没有剧情内容（事件池只在服务器）")
        return 0
    print("[x] 发现剧情内容混进了客户端：")
    for it in issues:
        print(f"    - {it['file']}：{it['why']}")
    print()
    print(CG.check(ROOT)["hint"])
    print("剧情内容请放 server/content/，客户端运行时拉取；"
          "确认误报请收窄 garmin_cn/contentguard.py 的规则。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
