"""高驰明细适配为 activity/actcard 共用的标准 payload。

Training Hub frequencyList: timestamp 为百分之一秒，GPS 为度 × 1e7，
distance/altitude 为厘米，speed 为秒/公里（不是 m/s）。分圈时间为百分之一秒。
协议参考 https://github.com/benniblau/coros-mcp/blob/main/coros_downloader.py
只转换入口，统计、步频修正、轨迹合并、抽稀和出图仍复用佳明实现。
"""
from __future__ import annotations

import datetime as dt
import json
import math

from . import activity as act
from . import route
from .coros import CorosAuthError, CorosCloud, CorosError


def raw_activity(row: dict) -> dict:
    raw = row.get("raw") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return raw if isinstance(raw, dict) else {}


def source(row: dict) -> str:
    # 兼容已经同步到手机里的旧记录，不依赖新增数据库列或重新同步。
    raw = raw_activity(row)
    return "coros" if raw.get("labelId") is not None and "sportType" in raw else "garmin"


def number(value, divisor: float = 1) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n / divisor if math.isfinite(n) and n != 65535 else None


def normalize(data: dict, activity_id: str) -> dict:
    if not isinstance(data, dict) or not data or not any(
            k in data for k in ("frequencyList", "lapList", "summary")):
        raise CorosError("高驰未返回有效活动明细，请重新同步后重试")
    samples = data.get("frequencyList") or []
    if not isinstance(samples, list) or any(not isinstance(p, dict) for p in samples):
        raise CorosError("高驰时序数据格式已变化，无法解析轨迹")
    fields = ("directTimestamp", "directSpeed", "directHeartRate", "directDoubleCadence",
              "directElevation", "sumDistance", "directPower")
    metrics, poly = [], []
    samples = sorted(samples, key=lambda p: number(p.get("timestamp")) or 0)
    for p in samples:
        t = number(p.get("timestamp"))
        if t is None:
            continue
        ms = t * 10  # centiseconds -> milliseconds
        pace = number(p.get("speed"))
        speed = 1000 / pace if pace and pace > 0 else None
        metrics.append({"metrics": [ms, speed, number(p.get("heart")),
                        number(p.get("cadence")), number(p.get("altitude"), 100),
                        number(p.get("distance"), 100), number(p.get("power"))]})
        lat, lon = number(p.get("gpsLat"), 1e7), number(p.get("gpsLon"), 1e7)
        if (lat is not None and lon is not None and -90 <= lat <= 90
                and -180 <= lon <= 180 and (lat != 0 or lon != 0)):
            poly.append({"lat": lat, "lon": lon, "time": ms, "valid": True})
    canonical = {
        "metricDescriptors": [{"key": k, "metricsIndex": i} for i, k in enumerate(fields)],
        "activityDetailMetrics": metrics,
        "geoPolylineDTO": {"polyline": poly},
    }
    series = act.build_series(canonical)
    points = series["points"]
    summary = act.aggregate(points)
    summary.update(repaired=series.get("repaired", 0), rescaled=series.get("rescaled", 0))
    # lapList 包含不同分组（自动分圈/手动圈等），取第一组有数据的圈，避免重复累加。
    groups = data.get("lapList") or []
    if not isinstance(groups, list) or any(not isinstance(g, dict) for g in groups):
        raise CorosError("高驰分圈数据格式已变化")
    laps = []
    items = next((g["lapItemList"] for g in groups if g.get("lapItemList")), [])
    for i, lap in enumerate(items):
        if not isinstance(lap, dict):
            raise CorosError("高驰分圈数据格式已变化")
        dist = number(lap.get("distance"), 100) or 0
        duration = number(lap.get("time", lap.get("totalTime")), 100) or 0
        stride = number(lap.get("avgStrideLength", lap.get("avgStepLen")))
        laps.append({"idx": i + 1, "dist_m": dist, "dur_s": duration,
                     "pace_s": round(duration * 1000 / dist) if dist > 50 else None,
                     "hr_avg": number(lap.get("avgHr")), "hr_max": number(lap.get("maxHr")),
                     "cad": number(lap.get("avgCadence")), "stride": stride,
                     "ele_gain": number(lap.get("elevGain", lap.get("ascent")), 100),
                     "cad_server": number(lap.get("avgCadence")),
                     "stride_server": stride, "is_partial": dist < 100})
    native_summary = data.get("summary") or {}
    row_metrics = {"aerobic_te": number(native_summary.get("aerobicEffect")),
                   "anaerobic_te": number(native_summary.get("anaerobicEffect"))}
    return {"activity_id": str(activity_id), "source": "coros", "adapter_version": 1,
            "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
            "summary": summary, "laps": laps,
            "series": {"points": act._thin(points, act.SERIES_POINTS)},
            "track": act.build_track(canonical), "route": route.build(canonical),
            "weather": None, "row_metrics": {k: v for k, v in row_metrics.items() if v is not None}}


def ensure(store, home, row: dict, email=None, password=None) -> dict:
    aid = str(row["activity_id"])
    cached = store.get_detail(aid)
    if cached and cached.get("source") == "coros":
        return cached
    cloud = CorosCloud(home)
    loaded = cloud.load()
    if not loaded:
        if not (email and password):
            raise CorosAuthError("还没有登录高驰，请先登录高驰账号")
        cloud.login(email, password, cloud.region)
    sport = raw_activity(row).get("sportType")
    if sport is None:
        raise CorosError("缺少高驰运动类型，请重新同步活动")
    try:
        data = cloud.activity_detail(aid, sport)
    except CorosAuthError:
        if not (email and password):
            raise
        cloud.login(email, password, cloud.region)
        data = cloud.activity_detail(aid, sport)
    payload = normalize(data, aid)
    store.save_detail(aid, payload)
    return payload
