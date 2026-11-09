"""实时取值层：身体电量这类「一天里一直在变」的指标，出图前重新拉一次。

为什么单独一层
--------------
`daily` 表里的 `bb_high / bb_low` 来自 daily summary，是一天里第一次同步时的
快照，之后无论同步多少次都不会变（sync 里那句 `not row.get("bb_high")` 就是
为了不覆盖）。结果就是看板上「身体电量 95」从早上摆到深夜，跟此刻还剩多少
完全无关。

bodyBattery 的日内序列里，最后一点才是「现在还剩多少」。这里在每次生成看板 /
报告前拉一次，把 `bb_now / bb_now_at / bb_series` 写回 daily（这三列不在
DAILY_COLS 里，不会被 sync 的 upsert 抹掉），失败就退回库里上次的值。
"""
from __future__ import annotations

import datetime as dt
import json

TTL = 90          # 秒：连着出图不重复打接口，但过半分钟就会重新取一次
SCALE_TTL = 300   # 秒：秤的数据一天变一两次，5 分钟一次足够


def _load_series(raw) -> list[list[int]]:
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    out = []
    for item in data or []:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            try:
                out.append([int(item[0]), int(item[1])])
            except (TypeError, ValueError):
                continue
    return out


def save_bb(store, date: str, bb: dict) -> None:
    """把解析好的身体电量写回 daily（只写有值的列）。"""
    cols, vals = [], []
    for k in ("bb_high", "bb_low", "bb_charged", "bb_drained",
              "bb_now", "bb_now_at", "bb_series"):
        v = bb.get(k)
        if v is not None:
            cols.append(k)
            vals.append(v)
    if not cols:
        return
    sets = ",".join(f"{c}=?" for c in cols)
    store.conn.execute(f"UPDATE daily SET {sets} WHERE date=?", vals + [date])
    store.conn.commit()


def body_battery(store, date: str, client=None, *, ttl: int = TTL,
                 timeout: int = 8, retries: int = 1) -> dict:
    """当天身体电量：当前值 + 当日曲线。client 为空时只读库。

    返回 {now, now_at, high, low, charged, drained, series, live}
    `live=True` 表示这次真的打到了接口。
    """
    row = store.day(date) or {}
    state = {
        "now": row.get("bb_now"),
        "now_at": row.get("bb_now_at") or "",
        "high": row.get("bb_high"),
        "low": row.get("bb_low"),
        "charged": row.get("bb_charged"),
        "drained": row.get("bb_drained"),
        "series": _load_series(row.get("bb_series")),
        "live": False,
    }
    if client is None:
        return state

    key = f"bb_fetch:{date}"
    last = store.get_meta(key)
    if last:
        try:
            age = (dt.datetime.now()
                   - dt.datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).total_seconds()
            if 0 <= age < ttl:
                return state
        except ValueError:
            pass

    try:
        # 出图是「顺手刷新」，不是同步任务：8 秒拿不到就退回库里的值。
        # 用默认的 30 秒 + 3 次重试会让离线时打开完整看板卡住近两分钟。
        raw = client.body_battery_day(date, timeout=timeout, retries=retries)
    except Exception:
        return state          # 会话过期 / 网络抖动都不该让看板挂掉

    from .sync import parse_bb        # 延迟导入：sync 在跑批时也会用到本模块
    bb = parse_bb(raw)
    if not bb:
        return state
    save_bb(store, date, bb)
    store.set_meta(key, dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    fresh = store.day(date) or {}
    state.update({
        "now": fresh.get("bb_now", state["now"]),
        "now_at": fresh.get("bb_now_at") or state["now_at"],
        "high": fresh.get("bb_high", state["high"]),
        "low": fresh.get("bb_low", state["low"]),
        "charged": fresh.get("bb_charged", state["charged"]),
        "drained": fresh.get("bb_drained", state["drained"]),
        "series": _load_series(fresh.get("bb_series")) or state["series"],
        "live": True,
    })
    return state


def scale(store, home, *, ttl: int = SCALE_TTL) -> dict:
    """体重 / 体成分：出图前也顺手拉一次秤。

    和身体电量同一个道理——秤的数据不在佳明云里，只有 `sync` 会去拉，
    早上称完没同步的话，看板会一直拿昨天的数当「最新」。这里按天 5 分钟
    一次的频率补拉，没登录 / 网络异常都静默跳过，用库里上一次的值。

    拉哪家按「首选来源」走：华为就找华为，小米就找小米，其余不动。

    返回 {n, latest, weight, live, source}
    """
    from . import body as B

    source = B.preferred_source(store)
    if source not in ("mi", "huawei"):
        return {"n": 0, "latest": None, "weight": None, "live": False, "source": source}

    key = f"{source}_fetch:{dt.date.today().isoformat()}"
    last = store.get_meta(key)
    if last:
        try:
            age = (dt.datetime.now()
                   - dt.datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).total_seconds()
            if 0 <= age < ttl:
                return {"n": 0, "latest": None, "weight": None, "live": False,
                        "source": source}
        except ValueError:
            pass

    try:
        n, recs = B.sync_preferred(store, home, days=7)
    except Exception:
        return {"n": 0, "latest": None, "weight": None, "live": False, "source": source}

    store.set_meta(key, dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    recs = sorted([r for r in (recs or []) if r.get("date")], key=lambda r: r["date"])
    latest = recs[-1] if recs else None
    return {
        "n": n,
        "latest": latest["date"] if latest else None,
        "weight": latest.get("weight_kg") if latest else None,
        "live": True,
        "source": source,
    }
