# -*- coding: utf-8 -*-
"""网页版渲染子进程：一个进程只服务一个用户。

为什么用子进程
--------------
`garmin_cn.appapi` 把 `HOME` / `DB` 做成模块级常量，一个进程只能指一个
`GARMIN_HOME`。网页版是多用户的，直接 import 会串数据。这里让父进程
（app.py）带 `GARMIN_HOME=/opt/runnercoach/users/<uid>` 起一个短命子进程，
渲染完就退出：简单、天然隔离，几十个用户够用。

用法（只由 webapp.py 调用）：
    GARMIN_HOME=... python3 web_render.py today '{}'
    GARMIN_HOME=... python3 web_render.py history '{"mode":["month"]}'
    GARMIN_HOME=... python3 web_render.py view '{"date":["2026-11-23"]}'
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    kind = sys.argv[1] if len(sys.argv) > 1 else "today"
    try:
        qs = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    except ValueError:
        qs = {}

    from garmin_cn import history, ui

    if kind == "today":
        html = ui.home_page()
    elif kind == "history":
        html = history.history_page(qs)
    elif kind == "view":
        html = history.view_page(qs)
    elif kind == "plan":
        html = ui.plan_page(qs)
    elif kind == "event":
        from garmin_cn import eventui

        html = eventui.render_page(qs)
    else:
        raise SystemExit(f"unknown page: {kind}")

    # 网页版不要首页的自动同步：它打的是本机 `/__sync/today`，服务端没这条路由。
    # 同步由「我的 → 立即同步」按钮触发，走服务端队列。
    try:
        from garmin_cn.ui import _AUTO_SYNC_JS

        html = html.replace(_AUTO_SYNC_JS, "")
    except Exception:      # noqa: BLE001 - 私有常量改名了也不该让页面挂掉
        pass

    sys.stdout.write(html)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
