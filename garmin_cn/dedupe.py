# -*- coding: utf-8 -*-
"""跨数据源的同一活动去重：跑量、活动数、训练负荷都不能翻倍。

为什么需要
----------
同一次跑步可能同时被佳明和高驰记录（用户戴两块表，或其中一块只是转存），
将来接入 Keep 后还会再多一条（Keep 会从手表同步）。如果都算进统计，
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

_RUN = {"running", "trail_running", "treadmill_running", "track_running", "virtual_run"}
_BIKE = {"cycling", "road_biking", "mountain_biking", "gravel_cycling", "indoor_cycling"}


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


def same_run(a: dict, b: dict) -> bool:
    """两条活动是否可能是同一次。同源直接 False（同源不互判）。"""
    if source_of(a) == source_of(b):
        return False
    fam = _family(a.get("type"))
    if not fam or fam != _family(b.get("type")):
        return False
    da = float(a.get("distance_m") or 0)
    db = float(b.get("distance_m") or 0)
    if da < MIN_DIST_M or db < MIN_DIST_M:
        return False
    ta, tb = _ts(a), _ts(b)
    if ta is None or tb is None or abs(ta - tb) > MAX_START_GAP_S:
        return False
    longer = max(da, db)
    if abs(da - db) > min(MAX_DIST_ABS_M, longer * MAX_DIST_RATIO):
        return False
    sa = float(a.get("duration_s") or 0)
    sb = float(b.get("duration_s") or 0)
    if sa and sb:
        longer_s = max(sa, sb)
        if abs(sa - sb) > min(MAX_DUR_ABS_S, longer_s * MAX_DUR_RATIO):
            return False
    return True


def pick_unique(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """按来源优先级去重，返回 `(保留, 丢弃)`；不修改入参。

    先让优先级高的来源占位（佳明在前），后来的低优先级条目只要与已保留的
    某条判为同一次就丢弃。返回值按 `date`、`start_ts` 倒序，和
    `Store.activities()` 原来的展示顺序一致。
    """
    ordered = sorted(rows, key=lambda r: (SOURCE_RANK.get(source_of(r), 9),
                                          _ts(r) or 0.0))
    kept: list[dict] = []
    dropped: list[dict] = []
    for row in ordered:
        fam = _family(row.get("type"))
        t = _ts(row)
        hit = None
        if fam and t is not None and float(row.get("distance_m") or 0) >= MIN_DIST_M:
            for k in kept:
                if _family(k.get("type")) != fam:
                    continue
                kt = _ts(k)
                if kt is not None and abs(kt - t) > MAX_START_GAP_S:
                    continue
                if same_run(row, k):
                    hit = k
                    break
        if hit is None:
            kept.append(row)
        else:
            dropped.append({**row, "duplicate_of": hit.get("activity_id")})
    kept.sort(key=lambda r: (str(r.get("date") or ""), str(r.get("start_ts") or "")),
              reverse=True)
    return kept, dropped


def dedupe(rows: list[dict]) -> list[dict]:
    """只要保留集，给统计/列表直接用。"""
    return pick_unique(rows)[0]
