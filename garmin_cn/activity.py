"""单次活动明细：抓分圈 / 1Hz 时序 / 轨迹 / 天气，修正步频与步幅后落库。

为什么要自己重算步频和步幅
------------------------
佳明 /gc-api 的字段定义与直觉不同：

* ``directRunCadence``      —— **单侧**步频（一步/单腿），一路是总步频的 1/2
* ``directDoubleCadence``   —— 总步频（steps/min），这个才是我们看的「步频」

实测 3 次活动，``doubleCadence / runCadence`` 的中位数都是 2.00。用户在
Connect 上看到的 93–98，就是单侧值被当成总步频显示了。

更麻烦的是活动级汇总 ``averageRunningCadenceInStepsPerMinute``：它是全样本
（含停顿、走、单侧值）的混合平均，同样不可信。例：2026-09-03 那次 13.51km
@4'48"，汇总步频 116.9、步幅 157cm；按配速反推真实值应约 180 spm / 116cm。

因此本模块的原则是：**汇总字段一律不用，全部从 1Hz 时序重算**。
步幅也不再采信 ``directStrideLength``，直接用 speed / cadence 推：
    stride_cm = speed(m/s) * 60 / cadence(spm) * 100
"""
from __future__ import annotations

import datetime as dt
import statistics
from typing import Any

from .metrics import num

# 跑步判定：低于这个速度视为走或停（m/s）
SPEED_RUN = 2.2
# 合法跑步步频区间，超出视为脏数据
CAD_LO, CAD_HI = 120.0, 235.0
# 合法步幅区间（cm）
STRIDE_LO, STRIDE_HI = 55.0, 240.0
# 落库时序点数上限（1Hz 全量对 42km 是 3000+ 点，压到 600 足够画图）
SERIES_POINTS = 600
# 轨迹点数上限
TRACK_POINTS = 420


# ---------------------------------------------------------------- 时序解析
def _cols(details: dict) -> dict[str, list]:
    desc = {m["key"]: m["metricsIndex"] for m in details.get("metricDescriptors") or []}
    rows = details.get("activityDetailMetrics") or []
    out: dict[str, list] = {}
    for key, idx in desc.items():
        out[key] = [r["metrics"][idx] if idx < len(r["metrics"]) else None for r in rows]
    return out


def _fix_cadence(cad: float | None, dcad: float | None,
                 speed: float | None) -> tuple[float | None, bool]:
    """取总步频，并标记这个采样点是否做过单位换算。

    ``directRunCadence`` 是单侧步频（比值实测恒为 2.00），``directDoubleCadence``
    才是总步频。返回 (步频, 是否换算过)，后者用于在图上报出「校准了多少点」。
    """
    fixed = False
    if num(dcad):
        v = num(dcad)
    elif num(cad):
        v, fixed = num(cad) * 2, True
    else:
        return None, False
    # 双保险：速度明显是跑动，但步频落在单侧区间 → 判定为未翻倍
    if v < CAD_LO and (num(speed) or 0) >= 2.5:
        v, fixed = v * 2, True
    return (v, fixed) if CAD_LO <= v <= CAD_HI else (None, fixed)


def build_series(details: dict, *, keep: int | None = None) -> dict:
    """把 details 展开并清洗成绘图用的序列。

    ``directTimestamp`` 是**毫秒**，这里统一换算成秒（与分圈 startTimeGMT 对齐）。
    ``keep=None`` 保留全量，供统计与分圈计算；给定值时均匀抽稀，供落库。
    """
    c = _cols(details)
    n = len(c.get("directTimestamp") or [])
    ts = c.get("directTimestamp") or [None] * n
    spd = c.get("directSpeed") or [None] * n
    hr = c.get("directHeartRate") or [None] * n
    ele = c.get("directElevation") or [None] * n
    dist = c.get("sumDistance") or [None] * n
    cad_raw = c.get("directRunCadence") or [None] * n
    dcad = c.get("directDoubleCadence") or [None] * n
    pwr = c.get("directPower") or [None] * n
    bb = c.get("directBodyBattery") or [None] * n
    gap = c.get("directGradeAdjustedSpeed") or [None] * n

    rows: list[dict] = []
    rescaled = 0
    for i in range(n):
        s = num(spd[i])
        cad, scaled = _fix_cadence(cad_raw[i], dcad[i], s)
        rescaled += 1 if scaled else 0
        stride = None
        if s and cad and s > 0:
            stride = s * 60.0 / cad * 100.0
            if not (STRIDE_LO <= stride <= STRIDE_HI):
                stride = None
        t = num(ts[i])
        if t and t > 1e11:          # 毫秒 → 秒
            t = t / 1000.0
        rows.append({
            "t": t, "spd": s, "hr": num(hr[i]), "cad": cad, "stride": stride,
            "ele": num(ele[i]), "dist": num(dist[i]), "pwr": num(pwr[i]),
            "bb": num(bb[i]), "gap": num(gap[i]),
        })

    if keep and len(rows) > keep:
        step = len(rows) / keep
        rows = [rows[min(int(i * step), len(rows) - 1)] for i in range(keep)]
    # 修正过的点保留 fixed 标记，出图时可以标出来（用户要求「修正而非置空」要可见）
    rows_fixed = _repair_outliers(rows)
    return {"points": rows, "repaired": rows_fixed, "rescaled": rescaled}


def _repair_outliers(rows: list[dict], tol: float = 0.30) -> int:
    """修正步幅离群点（不丢弃）。

    速度与步频来自不同通道，起停切换时会错位，算出 186cm/129spm 这种自洽但
    物理上说不通的组合。以全活动步幅中位数为基准：

    * 步幅 ≈ 2 倍中位数 → 该段步频被记成单侧值，步频 ×2、步幅 ÷2
    * 步幅 ≈ 0.5 倍     → 步频被重复计数，步频 ÷2、步幅 ×2
    * 其余离群          → 步幅夹回 [0.7, 1.3] × 中位数，步频按 speed 守恒反推

    返回修正的采样点数。
    """
    vals = [p["stride"] for p in rows if p["stride"]]
    if len(vals) < 20:
        return 0
    med = statistics.median(vals)
    lo, hi = med * (1 - tol), med * (1 + tol)
    fixed = 0
    for p in rows:
        st, cad = p["stride"], p["cad"]
        if not st or not cad or lo <= st <= hi:
            continue
        if st > hi:
            if cad < 120:                  # 步频是单侧值 → 翻倍，步幅减半
                p["cad"] = min(CAD_HI, cad * 2)
                p["stride"] = st / 2
            else:                          # 其他原因（GPS 漂移等）→ 步幅夹回上界
                p["stride"] = hi
        else:
            if cad > 180:                  # 步频被重复计数 → 减半，步幅翻倍
                p["cad"] = max(CAD_LO, cad / 2)
                p["stride"] = st * 2
            else:
                p["stride"] = lo
        p["fixed"] = True
        fixed += 1
    return fixed


def aggregate(points: list[dict]) -> dict:
    """基于清洗后的时序算统计量，只统计「确实在跑」的样本。"""
    run = [p for p in points if (p["spd"] or 0) >= SPEED_RUN and p["cad"]]
    cad = [p["cad"] for p in run]
    stride = [p["stride"] for p in run if p["stride"]]
    hr = [p["hr"] for p in run if p["hr"]]
    pwr = [p["pwr"] for p in run if p["pwr"]]

    def _avg(xs):
        return round(statistics.fmean(xs), 1) if xs else None

    def _p(xs, q):
        if not xs:
            return None
        xs = sorted(xs)
        return round(xs[min(int(len(xs) * q), len(xs) - 1)], 1)

    bands = {"<170": 0, "170-180": 0, "180-190": 0, "≥190": 0}
    for v in cad:
        if v < 170:
            bands["<170"] += 1
        elif v < 180:
            bands["170-180"] += 1
        elif v < 190:
            bands["180-190"] += 1
        else:
            bands["≥190"] += 1
    total = sum(bands.values()) or 1

    # 心率漂移：跑动样本前半 vs 后半（配速同步给出，避免误判）
    drift = None
    if len(run) >= 20:
        h1, h2 = run[: len(run) // 2], run[len(run) // 2:]
        hr1 = [p["hr"] for p in h1 if p["hr"]]
        hr2 = [p["hr"] for p in h2 if p["hr"]]
        sp1 = [p["spd"] for p in h1 if p["spd"]]
        sp2 = [p["spd"] for p in h2 if p["spd"]]
        if hr1 and hr2 and sp1 and sp2:
            drift = {
                "hr_first": round(statistics.fmean(hr1)), "hr_second": round(statistics.fmean(hr2)),
                "pace_first": round(1000 / statistics.fmean(sp1)),
                "pace_second": round(1000 / statistics.fmean(sp2)),
            }

    return {
        "cad_avg": _avg(cad), "cad_p95": _p(cad, 0.95), "cad_max": (max(cad) if cad else None),
        "stride_avg": _avg(stride), "hr_run_avg": _avg(hr), "pwr_run_avg": _avg(pwr),
        "run_samples": len(run), "all_samples": len(points),
        "cad_bands": {k: round(v / total * 100) for k, v in bands.items()},
        "drift": drift,
    }


# ---------------------------------------------------------------- 分圈
def build_laps(splits: dict, points: list[dict]) -> list[dict]:
    """分圈距离/用时取服务端，步频与步幅按时间窗从时序重算。"""
    laps: list[dict] = []
    for l in (splits.get("lapDTOs") or []):
        t0 = _parse_gmt(l.get("startTimeGMT"))
        dur = num(l.get("duration")) or 0
        cad, stride, hr = [], [], []
        if t0:
            t_end = t0 + dur
            for p in points:
                t = p["t"]
                if not t or not (t0 <= t <= t_end):
                    continue
                if (p["spd"] or 0) >= SPEED_RUN and p["cad"]:
                    cad.append(p["cad"])
                    if p["stride"]:
                        stride.append(p["stride"])
                    if p["hr"]:
                        hr.append(p["hr"])
        d = num(l.get("distance")) or 0
        laps.append({
            "idx": l.get("lapIndex"),
            "dist_m": round(d, 1),
            "dur_s": round(dur, 1),
            "pace_s": round(dur / (d / 1000)) if d > 50 else None,
            "hr_avg": round(statistics.fmean(hr)) if hr else num(l.get("averageHR")),
            "hr_max": l.get("maxHR"),
            "cad": round(statistics.fmean(cad), 1) if cad else None,
            "stride": round(statistics.fmean(stride), 1) if stride else None,
            "ele_gain": num(l.get("elevationGain")),
            "cad_server": num(l.get("averageRunCadence")),
            "stride_server": num(l.get("strideLength")),
            "is_partial": d < 100,
        })
    return laps


def _parse_gmt(s: str | None) -> float | None:
    """'2026-11-01T03:12:53.0' -> epoch 秒。"""
    if not s:
        return None
    try:
        s = s.replace("Z", "").split(".")[0]
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------- 轨迹
def build_track(details: dict, *, keep: int = TRACK_POINTS) -> list[list[float]]:
    poly = ((details.get("geoPolylineDTO") or {}).get("polyline")) or []
    pts = [[p["lat"], p["lon"]] for p in poly
           if p.get("valid", True) and p.get("lat") is not None and p.get("lon") is not None]
    if len(pts) > keep:
        step = len(pts) / keep
        pts = [pts[min(int(i * step), len(pts) - 1)] for i in range(keep)]
    return pts


def project(track: list[list[float]], w: float, h: float, pad: float = 6.0) -> list[tuple[float, float]]:
    """等距圆柱投影 + 等比缩放，把经纬度压到 w×h 画布内（无底图）。"""
    if not track:
        return []
    lats = [p[0] for p in track]
    lons = [p[1] for p in track]
    lat0 = sum(lats) / len(lats)
    kx = 111320.0 * abs(__import__("math").cos(__import__("math").radians(lat0)))
    ky = 110540.0
    xs = [(lo) * kx for lo in lons]
    ys = [(la) * ky for la in lats]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    spanx = (maxx - minx) or 1.0
    spany = (maxy - miny) or 1.0
    scale = min((w - 2 * pad) / spanx, (h - 2 * pad) / spany)
    ox = (w - spanx * scale) / 2
    oy = (h - spany * scale) / 2
    return [(ox + (x - minx) * scale, oy + (maxy - y) * scale) for x, y in zip(xs, ys)]


# ---------------------------------------------------------------- 天气
def build_weather(w: dict | None) -> dict | None:
    if not w:
        return None
    f2c = lambda f: round((f - 32) * 5 / 9, 1) if isinstance(f, (int, float)) else None
    return {
        "temp_c": f2c(w.get("temp")),
        "feels_c": f2c(w.get("apparentTemp")),
        "humidity": w.get("relativeHumidity"),
        "wind_dir": w.get("windDirectionCompassPoint"),
        "wind_speed": w.get("windSpeed"),
        "text": (w.get("weatherTypeDTO") or {}).get("desc"),
        "station": (w.get("weatherStationDTO") or {}).get("name"),
    }


# ---------------------------------------------------------------- 对外入口
def fetch(client, activity_id: int | str, *, with_track: bool = True) -> dict:
    """抓取并清洗一次活动的完整明细（不落库）。"""
    aid = str(activity_id)
    details = client.get(
        f"/activity-service/activity/{aid}/details?maxChartSize=4000&maxPolylineSize=4000")
    splits = client.get(f"/activity-service/activity/{aid}/splits")
    try:
        weather = build_weather(client.get(f"/activity-service/activity/{aid}/weather"))
    except Exception:
        weather = None

    series_full = build_series(details)
    points = series_full["points"]
    summary = aggregate(points)
    summary["repaired"] = series_full.get("repaired", 0)
    summary["rescaled"] = series_full.get("rescaled", 0)
    # route 是「几何 + 时序」合体，播放器专用；旧 payload 没有这个键，
    # 需要 route 的调用方自己带 force 重抓一次（见 motion.ensure_route）
    from . import route as route_mod

    return {
        "activity_id": aid,
        "fetched_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": summary,
        "laps": build_laps(splits, points),
        "series": {"points": _thin(points, SERIES_POINTS)},
        "track": build_track(details) if with_track else [],
        "route": route_mod.build(details) if with_track else {},
        "weather": weather,
    }


def _thin(points: list[dict], keep: int) -> list[dict]:
    if len(points) <= keep:
        return points
    step = len(points) / keep
    return [points[min(int(i * step), len(points) - 1)] for i in range(keep)]


def ensure(store, client, activity_id: int | str, *, force: bool = False) -> dict:
    """取明细：库里有就用库里的，没有就抓一次存进去。"""
    aid = str(activity_id)
    if not force:
        got = store.get_detail(aid)
        if got:
            return got
    payload = fetch(client, aid)
    store.save_detail(aid, payload)
    return payload
