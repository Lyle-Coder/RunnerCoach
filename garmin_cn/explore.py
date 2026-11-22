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


def cells_meters(track, total_m: float | None = None) -> dict[str, float]:
    """轨迹 → 每格累计里程（米）。

    和 `cells_of_track` 的区别是它只回答「跑没跑过」，这里回答「跑了多少」。
    做法是逐段累加：每一小段的长度记到该段中点所在的格子上。为什么不按
    端点落格再除以二——端点属于哪格是不确定的（一半概率算到邻格），
    而 500 m 的格子比常见的采样间隔（5–10 m）大两个数量级，段中点落格
    不会跨格错位。

    `total_m` 给了就按它做总标定：折线长度只用来决定**各格之间的比例**，
    总量用设备报的里程。轨迹断一段时折线会短三成，不归一的话格子图的
    里程和榜单对不上（用户报过「图中数据和上报数据不一致」）。
    """
    out: dict[str, float] = {}

    def _ok(p) -> bool:
        try:
            lat, lon = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError):
            return False
        return not (lat == 0 and lon == 0) and -90 <= lat <= 90 and -180 <= lon <= 180

    pts = [p for p in (track or []) if _ok(p)]
    if len(pts) < 2:
        # 只有一个点也算一次「到过」，给个 0 里程，别让它在图上消失
        if pts:
            try:
                out[cell_of(float(pts[0][0]), float(pts[0][1]))] = 0.0
            except (TypeError, ValueError, IndexError):
                pass
        return out

    for a, b in zip(pts, pts[1:]):
        try:
            la1, lo1 = float(a[0]), float(a[1])
            la2, lo2 = float(b[0]), float(b[1])
        except (TypeError, ValueError, IndexError):
            continue
        # 等距圆柱近似：500 m 尺度上，cos 用两点平均纬度就够了
        dy = (la2 - la1) * M_PER_DEG
        dx = (lo2 - lo1) * M_PER_DEG * math.cos(math.radians((la1 + la2) / 2))
        seg = math.hypot(dx, dy)
        if seg <= 0 or seg > 2000:      # 2 km 以上的「一段」基本都是脏点跳变
            continue
        k = cell_of((la1 + la2) / 2, (lo1 + lo2) / 2)
        out[k] = out.get(k, 0.0) + seg
    # 按设备距离归一：轨迹折线长度和手表的 distance_m 不是一回事——GPS 断一段
    # 那条能差三成（实测 4.69 km 的活动轨迹只有 3.11 km）。格子图上写的是
    # 「这格跑了多少米」，跟榜单对不上就是错的，所以用设备距离做总标定，
    # 只在**各格之间**保留轨迹的比例。
    if total_m and out:
        have = sum(out.values())
        if have > 0:
            ratio = float(total_m) / have
            out = {c: v * ratio for c, v in out.items()}
    return out


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


def cell_center(cell: str) -> tuple[float, float]:
    """格子 ID → 中心经纬度（WGS-84，和轨迹数据同一套坐标）。

    反算给地图用：格子里没有存经纬度，只存了格号，这里按同一个 d 反推回来。
    """
    a, b = str(cell).split(":")
    d = CELL_M / M_PER_DEG
    return (int(a) + 0.5) * d, (int(b) + 0.5) * d


def cell_corners(cell: str) -> list[tuple[float, float]]:
    """格子四角（左下、右下、右上、左上）。画方块时用，比一个点更像「一片区域」。"""
    a, b = str(cell).split(":")
    d = CELL_M / M_PER_DEG
    y0, x0 = int(a) * d, int(b) * d
    return [(y0, x0), (y0, x0 + d), (y0 + d, x0 + d), (y0 + d, x0)]


def week_cell_meters(store, today: str | None = None,
                     min_m: float = 1000.0) -> dict[str, float]:
    """本周每格的累计里程（米）。地图的深浅和「谁跑得多」都靠它。

    口径：本周、跑步类、≥1 km。骑行不算——一条 60 km 的骑行会在地图上
    铺满半座城，把队友的脚印盖掉；探索徽章那边则要求 3 km 才记一笔。
    """
    day = dt.date.fromisoformat(today or dt.date.today().isoformat())
    start = (day - dt.timedelta(days=day.weekday())).isoformat()
    with store.conn:
        rows = store.conn.execute("""
            SELECT a.type, a.distance_m, d.payload
              FROM activities a
              JOIN activity_detail d ON d.activity_id = a.activity_id
             WHERE a.date BETWEEN ? AND ? AND a.distance_m >= ?
             ORDER BY a.date
        """, (start, day.isoformat(), min_m)).fetchall()
    out: dict[str, float] = {}
    for r in rows:
        if not is_run(r["type"]):
            continue
        try:
            payload = json.loads(r["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        for k, v in cells_meters(payload.get("track") or [],
                                 r["distance_m"]).items():
            out[k] = out.get(k, 0.0) + v
    return out


def cluster_cells(cells: list[str], step: float = 0.5) -> list[list[str]]:
    """把格子按 0.5° 网格（约 55 km）聚成几片，格子多的片排前面。

    为什么必须分片：跨省的组塞进一张图时，取景要装下 900 km，zoom 只能给到
    5 级——392 px 宽覆盖一千公里，500 m 的格子剩零点几个像素，图上什么都
    看不见。分片之后每片各自按城市级取景，格子才画得出来。

    用网格 + 连通域而不是按距离两两比：600 个格子两两算是 36 万次，而按
    0.5° 分桶再做 8 邻域合并是 O(n)，效果一样——同城的人本来就在相邻桶里。
    """
    if not cells:
        return []
    buckets: dict[tuple[int, int], list[str]] = {}
    for c in cells:
        try:
            lat, lon = cell_center(c)
        except (AttributeError, ValueError):
            continue
        buckets.setdefault((int(lat // step), int(lon // step)), []).append(c)
    if not buckets:
        return []

    parent = {k: k for k in buckets}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j in list(buckets):
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                nb = (i + di, j + dj)
                if nb in parent:
                    ra, rb = find((i, j)), find(nb)
                    if ra != rb:
                        parent[rb] = ra

    groups: dict[tuple[int, int], list[str]] = {}
    for k, v in buckets.items():
        groups.setdefault(find(k), []).extend(v)
    return sorted(groups.values(), key=len, reverse=True)


def span_km(cells: list[str]) -> float:
    """这批格子在地理上跨了多少公里（对角线）。用来判断要不要分片。"""
    pts = []
    for c in cells:
        try:
            pts.append(cell_center(c))
        except (AttributeError, ValueError):
            continue
    if len(pts) < 2:
        return 0.0
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    dy = (max(lats) - min(lats)) * 111.0
    dx = (max(lons) - min(lons)) * 111.0 * math.cos(math.radians(sum(lats) / len(lats)))
    return math.hypot(dx, dy)


def total_cells(store) -> int:
    """账本里一共有多少格。只用于诊断，不上报。"""
    with store.conn:
        r = store.conn.execute("SELECT COUNT(*) n FROM explore_grid").fetchone()
    return int(r["n"] or 0)
