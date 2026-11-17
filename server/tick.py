# -*- coding: utf-8 -*-
"""定时预热：把该结算的组先结算掉，用户打开时就是最新状态。

    python tick.py              # 结算所有到期组
    python tick.py --dry-run    # 只列出会结算哪些组，不写库
    python tick.py --quiet      # 没有要结算的就一声不吭（给高频 cron 用）

crontab（服务器是 UTC，脚本内部按北京时间算「今天」）：

    # 每天凌晨跑一次，把跨周的组结掉
    5 0 * * * cd /srv/runnercoach/server && /usr/bin/python3 tick.py >> /var/log/rc-tick.log 2>&1
    # 每 10 分钟兜一次：懒触发已经在管，这条只是让「周一第一个打开的人」更快
    */10 * * * * cd /srv/runnercoach/server && /usr/bin/python3 tick.py --quiet

为什么两段都要
--------------
cron 是**预热**，懒触发是**兜底**，两者不冲突：

- cron 在低峰期把重活做掉，用户请求时走热路径（只读，3 ms）
- cron 万一没跑（机器重启、任务被删、脚本报错），用户打开时 ensure 仍然会
  把该结算的补上，只是那一刻由这个用户承担 17 ms

四个设计约束
------------
1. **幂等**。`ensure` 用条件 UPDATE 抢 `settled_act`，跑一百次也只有一次生效。
2. **跨进程安全**。抢锁在数据库层（`BEGIN IMMEDIATE`），不是进程内锁——
   否则 cron 进程和 web 进程各算一遍。
3. **逐组短事务**。不要一个事务扫全表：结算一组几十毫秒，扫一百组会让写锁
   被占几秒，期间所有上报都排队。
4. **时区**。服务器多半是 UTC，直接用 `date.today()` 会在北京时间早上 8 点才
   跨日。所以按 `TICK_TZ_OFFSET`（默认 +8）算「今天」。
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import expedition as EXP  # noqa: E402
import groups as G  # noqa: E402

TZ_OFFSET = int(os.environ.get("TICK_TZ_OFFSET", "8"))


def today_local() -> dt.date:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=TZ_OFFSET)).date()


def due(today: dt.date) -> list[int]:
    """还没结算到位的组。只看已有赛季的——没开季的组等用户第一次打开再开，
    否则 cron 会给一堆三个月没人说话的组也造出赛季。"""
    with G.db() as c:
        rows = c.execute("SELECT * FROM group_run_season WHERE ended=0").fetchall()
    return [int(r["group_id"]) for r in rows if not EXP._is_ready(dict(r), today)]


def run(*, dry: bool = False, quiet: bool = False,
        today: dt.date | None = None) -> dict:
    today = today or today_local()
    targets = due(today)
    if not targets:
        if not quiet:
            print(f"{today} 没有需要结算的组")
        return {"ok": True, "due": 0, "settled": 0, "failed": 0}

    if dry:
        print(f"{today} 需要结算 {len(targets)} 个组：{targets}")
        return {"ok": True, "due": len(targets), "settled": 0, "failed": 0,
                "groups": targets}

    t0 = time.perf_counter()
    ok = failed = 0
    for gid in targets:
        try:
            season, act, ended = EXP.ensure(gid, today)
            ok += 1
            if not quiet:
                flag = "已完结" if ended else f"第 {act} 幕"
                print(f"  [ok] 组 {gid} → {flag}")
        except Exception as e:  # noqa: BLE001 - 一个组坏了不能拖垮整轮
            failed += 1
            print(f"  [x]  组 {gid}: {type(e).__name__}: {e}", file=sys.stderr)
    cost = (time.perf_counter() - t0) * 1000
    if not quiet:
        print(f"{today} 扫描 {len(targets)} 组，成功 {ok}，失败 {failed}，"
              f"耗时 {cost:.0f} ms")
    return {"ok": failed == 0, "due": len(targets), "settled": ok,
            "failed": failed, "ms": round(cost)}


def main() -> int:
    dry = "--dry-run" in sys.argv
    quiet = "--quiet" in sys.argv
    # 建表是幂等的。第一次部署时 cron 可能先于 web 起来，不能让它因为
    # 「表还不存在」直接崩掉——那会在日志里留一条看不懂的 traceback。
    G.init_db()
    EXP.init_db()
    # 内容没配就别跑了，免得刷一屏 503 日志
    if not EXP.POOL_PATH.is_file():
        print(f"赛季内容未配置：{EXP.POOL_PATH}", file=sys.stderr)
        return 2
    r = run(dry=dry, quiet=quiet)
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
