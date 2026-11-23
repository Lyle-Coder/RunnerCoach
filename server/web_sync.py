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

    # 顺手把最近几条活动的明细抓下来。为什么在这里做：活动图要用
    # `activity_detail`（轨迹 + 分段），而它只在打开图纸时才按需抓。登录态
    # 一旦被 App 的新登录顶掉（佳明一个账号同时只认最新会话），没缓存的图就
    # 永远打不开。趁这次登录态还有效，把最近几条先存进库，之后过期也能出图。
    if out["ok"]:
        try:
            out["details"] = _prefetch_details(A, limit=6)
        except Exception as e:      # noqa: BLE001 - 抓明细失败不能影响同步结果
            out["details"] = f"skip: {type(e).__name__}"

    # 只输出一行；父进程按最后一行解析，防止 import 时有别的输出混进来。
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["ok"] else 1


def _prefetch_details(A, limit: int = 6) -> int:
    """把最近没有明细的活动补齐，返回抓到几条。失败就停，别硬撑。"""
    from garmin_cn import activity as ACT
    from garmin_cn.store import Store

    store = Store(A.DB)
    rows = store.conn.execute(
        "SELECT a.activity_id FROM activities a "
        "LEFT JOIN activity_detail d ON d.activity_id = a.activity_id "
        "WHERE d.activity_id IS NULL ORDER BY a.date DESC LIMIT ?",
        (int(limit),)).fetchall()
    if not rows:
        return 0
    client = A._client(None, None)
    got = 0
    for row in rows:
        aid = str(row[0])
        try:
            if ACT.ensure(store, client, aid):
                got += 1
        except Exception:      # noqa: BLE001 - 认证失败/网络抖动就收手
            break
    return got


if __name__ == "__main__":
    raise SystemExit(main())
