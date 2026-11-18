# -*- coding: utf-8 -*-
"""探索徽章：500 m 网格，只在本机算，只上传「这周新踩了几格」。

为什么这么设计（docs/用户组设计.md 11.5）
----------------------------------------
轨迹是精确行踪，比里程敏感得多。所以格子账本完全留在手机上，上报的只有
一个整数；服务端拿这个整数跟别人的比，永远不知道你走的是哪条街。

口径
----
- 格边长 500 m。按纬度算格宽、按当前纬度算格长（经度一度只有 111 km×cos φ）。
- 单次 **≥ 3 km 的跑步类活动**才计入：1 km 的散步绕小区一圈，不该刷探索。
- 单日新增 **封顶 50 格**（25 km 的新路面）。这是防刷，也给「一天跑完半座城」
  留了富余。
- 首次启用时把历史活动按日期升序全部扫一遍：老路先占格，本周的才算「新增」。
  不这么做，第一天更新就会报出「本周新增 300 格」这种假数字。

增量
----
`explore_seen` 记已经扫过的活动 id。轨迹是同步时才拉下来的，所以「没有
detail」的活动**不标记已扫**，等它有轨迹了再算。
"""
from __future__ import annotations

import datetime as dt
import json
import math

# 格边长（米）。改这个数等于改整个徽章的口径，历史账本会全部作废——
# 真要大改就给 explore_grid 换张表，别原地改。
CELL_M = 500.0
M_PER_DEG = 111320.0

# 单次计入的最小距离（米）与单日新增上限（格）
MIN_ACT_M = 3000.0
DAY_CAP = 50

# 与 group.RUN_TYPES 必须一致（selftest 有断言）。这里单独写一份是为了
# 不让 explore 反向依赖 group——group 要调 explore，互相 import 会成环。
RUN_TYPES = ("running", "trail_running", "treadmill_running")


def is_run(t) -> bool:
    s = str(t or "").lower()
    return any(k in s for k in RUN_TYPES) or "跑" in s


def cell_of(lat: float, lon: float) -> str:
    """一个坐标落在哪一格。

    经纬方向用**同一个**格宽 `CELL_M / M_PER_DEG`（≈0.00449°），不做
    「经度格宽 = 500 m / cos φ」那种按纬度伸缩的修正。原因是格号必须全局
    稳定：按 cos φ 伸缩时，同一个经度在 39.900 与 39.905 会算进不同的列，
    跑同一条街会重复计格，徽章口径当场失效。代价是中纬度（北京 cos 40°）
    格子实际是 384 m × 500 m，偏窄——但稳定、可解释、跨城市也不会错位，
    对「去过哪片区域」这件事够用。
    """
    d = CELL_M / M_PER_DEG
    return f"{int(math.floor(lat / d))}:{int(math.floor(lon / d))}"


def cells_of_track(track) -> list[str]:
    """轨迹 → 去重后的格子列表（保持出现顺序，方便调试）。

    `track` 是 `[[lat, lon], ...]`；脏点（None / 长度不对 / 非数值）直接跳过，
    不抛异常——一条轨迹里有一两个坏点不该让整次统计失败。
    """
    out: list[str] = []
    seen: set[str] = set()
    for p in track or []:
        try:
            lat, lon = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError):
            continue
        # 0,0 是缺失坐标最常见的占位值，落进去会凭空多出一格
        if lat == 0 and lon == 0:
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        k = cell_of(lat, lon)
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def scan(store, limit: int = 500) -> dict:
    """把还没扫过的跑步轨迹落进网格账本。

    按日期升序处理：同一次调用里，先跑的路线先占格，后跑的才不会倒过来变成
    「最早访问」。`limit` 只是给首次全量扫描分页用的，一次最多处理这么多条。
    """
    with store.conn:
        rows = store.conn.execute("""
            SELECT a.activity_id, a.date, a.distance_m, d.payload
              FROM activities a
              JOIN activity_detail d ON d.activity_id = a.activity_id
             WHERE a.activity_id NOT IN (SELECT activity_id FROM explore_seen)
             ORDER BY a.date, a.activity_id
             LIMIT ?
        """, (int(limit),)).fetchall()

    added = scanned = 0
    for r in rows:
        try:
            payload = json.loads(r["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        cells = cells_of_track(payload.get("track") or [])
        day = str(r["date"] or "")[:10]
        if not day or not cells:
            # 没有轨迹（室内跑 / 老活动没拉明细）：标成已扫，否则每次同步都重试。
            # 室内跑本来就不该拿探索徽章——它没有「去了哪里」这件事。
            with store.conn:
                store.conn.execute("INSERT OR IGNORE INTO explore_seen(activity_id) VALUES(?)",
                                   (r["activity_id"],))
            scanned += 1
            continue
        if float(r["distance_m"] or 0) < MIN_ACT_M:
            with store.conn:
                store.conn.execute("INSERT OR IGNORE INTO explore_seen(activity_id) VALUES(?)",
                                   (r["activity_id"],))
            scanned += 1
            continue
        with store.conn:
            before = store.conn.total_changes
            store.conn.executemany(
                "INSERT OR IGNORE INTO explore_grid(cell, first_seen, first_act)"
                " VALUES(?,?,?)",
                [(k, day, r["activity_id"]) for k in cells])
            added += store.conn.total_changes - before
            store.conn.execute("INSERT OR IGNORE INTO explore_seen(activity_id) VALUES(?)",
                               (r["activity_id"],))
        scanned += 1
    return {"ok": True, "scanned": scanned, "added": added,
            "more": len(rows) >= int(limit)}


def daily_new(store, start: str, end: str) -> dict[str, int]:
    """[start, end] 每天新踩了几格。返回 {日期: 格数}，没有新增的日期不出现。

    单日封顶 `DAY_CAP`：一天扫 200 格只能是两种情况，要么坐了趟车，要么
    数据出了问题，两种都不该算成本事。
    """
    with store.conn:
        rows = store.conn.execute(
            "SELECT first_seen d, COUNT(*) n FROM explore_grid"
            " WHERE first_seen BETWEEN ? AND ? GROUP BY first_seen",
            (start, end)).fetchall()
    out: dict[str, int] = {}
    for r in rows:
        n = int(r["n"] or 0)
        if n > 0:
            out[str(r["d"])] = min(DAY_CAP, n)
    return out


def week_new(store, today: str | None = None) -> dict[str, int]:
    """本周（周一起）每天新踩了几格。"""
    day = dt.date.fromisoformat(today or dt.date.today().isoformat())
    start = day - dt.timedelta(days=day.weekday())
    return daily_new(store, start.isoformat(), day.isoformat())


def total_cells(store) -> int:
    """账本里一共有多少格。只用于诊断，不上报。"""
    with store.conn:
        r = store.conn.execute("SELECT COUNT(*) n FROM explore_grid").fetchone()
    return int(r["n"] or 0)
