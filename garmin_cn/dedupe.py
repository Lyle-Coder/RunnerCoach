# -*- coding: utf-8 -*-
"""跨数据源的同一活动去重：跑量、活动数、训练负荷都不能翻倍。

为什么需要
----------
同一次跑步可能同时被佳明和高驰记录（用户戴两块表，或其中一块只是转存），
接入 Keep 后还会再多一条（Keep 会从手表同步）。如果都算进统计，
跑量、活动数、训练负荷都会翻倍，恢复度和 ACWR 也会跟着失真。

判定「同一次」要同时满足
------------------------
- **不同来源**：同一来源的两条永远不算重复。同一天两练、同样的距离和配速
  是真实存在的（比如早晚各一次 10 km），不能因为长得像就合并。
- **运动类型同族**：跑步类互判、骑行类互判；跑步和骑行永不互判。
- **开始时间差 ≤ 180 s**：两块表起跑按键差几十秒很正常。
- **距离差 ≤ min(300 m, 2%)**：GPS 取样和轨迹过滤会带来 1% 量级的差。
- **时长差 ≤ min(120 s, 5%)**：暂停策略不同（自动暂停 vs 不停表）会差几分钟。

距离小于 500 m 的活动不参与判定：热身、拉伸、力量课没有可比的里程，
硬判只会制造误伤。

性能
----
切 tab 每次都要重算，不能写成 O(n²)。这里做三件事：
1. **单源直接返回**：绝大多数用户只有佳明，没有可比的第二条，O(n log n) 排完就走；
2. 每条只解析一次时间戳与距离，循环里不再重复 `fromisoformat`；
3. 多源时按天分桶，180 s 的时间差只可能落在当天或相邻一天，
   内层只比同桶的几条，整体接近 O(n)。

保留谁
------
`SOURCE_RANK` 数字越小越优先：佳明 > 高驰 > Keep。佳明有 1 Hz 明细、分圈、
轨迹，高驰次之，Keep 只有课程/运动摘要。被丢弃的那条不会删库，只在
`duplicate_of` 里记下保留者，需要时还能追回去。
"""
from __future__ import annotations

import datetime as dt

MAX_START_GAP_S = 180.0
MAX_DIST_RATIO = 0.02
MAX_DIST_ABS_M = 300.0
MAX_DUR_RATIO = 0.05
MAX_DUR_ABS_S = 120.0
MIN_DIST_M = 500.0

SOURCE_RANK = {"garmin": 0, "coros": 1, "keep": 2}

from .sports import RUN_TYPES as _RUN, BIKE_TYPES as _BIKE
_DAY = 86400.0


def source_of(row: dict) -> str:
    s = str(row.get("source") or "garmin").strip().lower()
    return s if s in SOURCE_RANK else "other"


def _ts(row: dict) -> float | None:
    raw = str(row.get("start_ts") or "").replace("T", " ").strip()
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw[:19]).timestamp()
    except ValueError:
        return None


def _family(kind) -> str:
    k = str(kind or "").strip().lower()
    if k in _RUN:
        return "run"
    if k in _BIKE:
        return "bike"
    return ""


def _day_key(ts: float) -> int:
    return int(ts // _DAY)


def _same_parsed(a: dict, b: dict) -> bool:
    """两条预解析记录是否同一次（不看来源，来源由调用方先筛）。"""
    if abs(a["ts"] - b["ts"]) > MAX_START_GAP_S:
        return False
    da, db = a["dist"], b["dist"]
    if da < MIN_DIST_M or db < MIN_DIST_M:
        return False
    longer = max(da, db)
    if abs(da - db) > min(MAX_DIST_ABS_M, longer * MAX_DIST_RATIO):
        return False
    sa, sb = a["dur"], b["dur"]
    if sa and sb:
        if abs(sa - sb) > min(MAX_DUR_ABS_S, max(sa, sb) * MAX_DUR_RATIO):
            return False
    return True


def same_run(a: dict, b: dict) -> bool:
    """两条活动是否可能是同一次。同源直接 False（同源不互判）。"""
    if source_of(a) == source_of(b):
        return False
    fam = _family(a.get("type"))
    if not fam or fam != _family(b.get("type")):
        return False
    ta, tb = _ts(a), _ts(b)
    if ta is None or tb is None:
        return False
    return _same_parsed(
        {"ts": ta, "dist": float(a.get("distance_m") or 0),
         "dur": float(a.get("duration_s") or 0)},
        {"ts": tb, "dist": float(b.get("distance_m") or 0),
         "dur": float(b.get("duration_s") or 0)},
    )


def _display_key(row: dict) -> tuple[str, str]:
    return (str(row.get("date") or ""), str(row.get("start_ts") or ""))


def pick_unique(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """按来源优先级去重，返回 `(保留, 丢弃)`；不修改入参。

    先让优先级高的来源占位（佳明在前），后来的低优先级条目只要与已保留的
    某条判为同一次就丢弃。返回值按 `date`、`start_ts` 倒序，和
    `Store.activities()` 原来的展示顺序一致。
    """
    if len(rows) < 2:
        return list(rows), []

    parsed = [{
        "row": r,
        "src": source_of(r),
        "fam": _family(r.get("type")),
        "ts": _ts(r),
        "dist": float(r.get("distance_m") or 0),
        "dur": float(r.get("duration_s") or 0),
    } for r in rows]

    # 单源（绝大多数用户）：没有可比的另一条，排完直接返回，不进 O(n²) 路径
    if len({p["src"] for p in parsed}) <= 1:
        parsed.sort(key=lambda p: _display_key(p["row"]), reverse=True)
        return [p["row"] for p in parsed], []

    kept_buckets: dict[int, list[dict]] = {}
    kept: list[dict] = []
    dropped: list[dict] = []
    # 先按来源优先级占位，再按时间：佳明先落，高驰/Keep 后到就被判重。
    # 桶里只放**已保留**的条目——否则佳明会先撞上还没被处理的高驰，反把自己丢掉。
    for p in sorted(parsed, key=lambda x: (SOURCE_RANK.get(x["src"], 9), x["ts"] or 0.0)):
        hit = None
        if p["fam"] and p["ts"] is not None and p["dist"] >= MIN_DIST_M:
            day = _day_key(p["ts"])
            for d in (day - 1, day, day + 1):
                for q in kept_buckets.get(d, ()):
                    if q["src"] == p["src"] or q["fam"] != p["fam"]:
                        continue
                    if _same_parsed(p, q):
                        hit = q
                        break
                if hit is not None:
                    break
        if hit is None:
            kept.append(p["row"])
            if p["fam"] and p["ts"] is not None and p["dist"] >= MIN_DIST_M:
                kept_buckets.setdefault(_day_key(p["ts"]), []).append(p)
            continue
        dropped.append({**p["row"], "duplicate_of": hit["row"].get("activity_id")})

    kept.sort(key=_display_key, reverse=True)
    return kept, dropped


def dedupe(rows: list[dict]) -> list[dict]:
    """只要保留集，给统计/列表直接用。"""
    return pick_unique(rows)[0]
