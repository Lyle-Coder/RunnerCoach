# -*- coding: utf-8 -*-
"""网页版同步子进程：一个进程只同步一个用户。

父进程（webapp.py）通过 stdin 传 JSON（含账密），避免出现在命令行参数 /
`ps` / 日志里。`GARMIN_HOME` 指向该用户的目录，同步完把数据写进
`<home>/data/garmin.db`，父进程再起渲染子进程出页面。

只输出一行 JSON；账密绝不回显。
"""
from __future__ import annotations

import json
import sys
import time


def main() -> int:
    try:
        req = json.load(sys.stdin)
    except Exception as e:      # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"bad request: {type(e).__name__}"},
                         ensure_ascii=False))
        return 2
    email = str(req.get("email") or "")
    password = str(req.get("password") or "")
    source = str(req.get("source") or "garmin")
    try:
        days = max(1, min(365, int(req.get("days") or 3)))
    except (TypeError, ValueError):
        days = 3

    t0 = time.time()
    try:
        from garmin_cn import appapi as A

        if source == "coros":
            r = A.coros_sync(days=days, email=email, password=password)
        else:
            r = A.sync(days=days, email=email or None,
                       password=password or None, with_body=True,
                       activity_pages=1)
    except Exception as e:      # noqa: BLE001 - 任何异常都要变成 JSON，不然父进程只看到崩
        r = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    out = {
        "ok": bool(r.get("ok")),
        "error": str(r.get("error") or "")[:300],
        "need_login": bool(r.get("need_login") or r.get("need_mfa")),
        "need_mfa": bool(r.get("need_mfa")),
        "new": int(r.get("new") or r.get("activities") or 0),
        "days": days,
        "seconds": round(time.time() - t0, 1),
    }
    # 只输出一行；父进程按最后一行解析，防止 import 时有别的输出混进来。
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
