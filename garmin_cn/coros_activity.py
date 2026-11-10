"""高驰明细适配为 activity/actcard 共用的标准 payload。

Training Hub frequencyList: timestamp 为百分之一秒，GPS 为度 × 1e7，
**distance 为厘米（→ ÷100 得米），altitude 已经是米（不要再除）**，
speed 是「秒/公里」（→ 1000/pace 得 m/s）。summary 里以毫秒为单位的
totalTime 实际是厘秒，distance 同样是厘米，maxSpeed 是秒/公里。
分圈 lapItemList 的 distance/time 也是厘米/厘秒。
协议参考 https://github.com/benniblau/coros-mcp/blob/main/coros_downloader.py
高驰汇总和步态保留原始口径；轨迹、抽稀和出图复用公共实现。

历史坑（2026-11-09 修）：coros.py 里另有一份 `detail_to_payload` 把 lap
distance 直接当米用，导致 1 km 圈显示成 100.00 km、配速 0'06"，同时时序
distance 未换算让 speed 全被当成漂移丢掉。现在两者共用本模块，只此一份。
版本号 ADAPTER_VERSION 用于让旧缓存自动失效（缓存里没有版本号或版本
更低的明细会被重新抓取）。
"""
from __future__ import annotations

import datetime as dt
import json
import math

from . import activity as act
from . import route
from .coros import CorosAuthError, CorosCloud, CorosError

# 3 = 原生汇总/步态/六区心率，优先公里分圈；废弃佳明修正后的旧缓存
ADAPTER_VERSION = 4


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


# 跑步动态（触地时间 / 垂直振幅）：高驰不同固件与接口版本给的字段名不统一，
# 官方文档也没公开，所以按候选名找。找到后必须按量纲归一，否则卡上会印出
# 「触地 0 ms」这类看着像 bug 的数字。
GCT_KEYS = ("avgGroundContactTime", "groundContactTime", "avgContactTime",
            "contactTime", "avgGct", "gct", "avgStanceTime", "stanceTime")
VO_KEYS = ("avgVerticalOscillation", "verticalOscillation", "avgVertOscillation",
           "vertOscillation", "avgVosc", "vosc")


def dynamics(native: dict) -> tuple[float | None, float | None]:
    """从高驰 summary 里取（触地时间 ms，垂直振幅 cm）。

    找不到或量纲不在合理区间就返回 None —— 出图侧会显示「—」，这比印一个
    错数量级的数字安全。合理区间：触地 60~600 ms，垂直振幅 2~25 cm。
    """
    if not isinstance(native, dict):
        return None, None
    pools = [native]
    for key in ("runningDynamics", "runDynamics", "dynamics", "metrics"):
        sub = native.get(key)
        if isinstance(sub, dict):
            pools.append(sub)

    def pick(keys: tuple[str, ...]) -> float | None:
        for pool in pools:
            for key in keys:
                v = number(pool.get(key))
                if v is not None and v != 0:
                    return v
        return None

    gct = pick(GCT_KEYS)
    if gct is not None:
        if gct < 1:                 # 秒 → 毫秒
            gct *= 1000
        gct = gct if 60 <= gct <= 600 else None
    vo = pick(VO_KEYS)
    if vo is not None:
        if vo < 2:                  # 米 → 厘米
            vo *= 100
        vo = vo if 2 <= vo <= 25 else None
    return gct, vo


def normalize(data: dict, activity_id: str, track: list | None = None) -> dict:
    """高驰明细 → 出图契约。`track` 可传入 GPX 解析出的 [[lat, lon], ...]。"""
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
                        number(p.get("cadence")),
                        number(p.get("altitude")),        # 米，原值
                        number(p.get("distance"), 100),   # 厘米 → 米
                        number(p.get("power"))]})
        lat, lon = number(p.get("gpsLat"), 1e7), number(p.get("gpsLon"), 1e7)
        if (lat is not None and lon is not None and -90 <= lat <= 90
                and -180 <= lon <= 180 and (lat != 0 or lon != 0)):
            poly.append({"lat": lat, "lon": lon, "time": ms, "valid": True})
    canonical = {
        "metricDescriptors": [{"key": k, "metricsIndex": i} for i, k in enumerate(fields)],
        "activityDetailMetrics": metrics,
        "geoPolylineDTO": {"polyline": poly},
    }
    # COROS already reports total cadence and measured stride. Never apply
    # Garmin's unilateral-cadence heuristics or exclude walking on trail runs.
    points = []
    for p, m in zip([p for p in samples if number(p.get("timestamp")) is not None], metrics):
        t, speed, hr, cad, ele, dist, power = m["metrics"]
        points.append({"t": t / 1000, "spd": speed, "hr": hr,
                       "cad": cad, "stride": number(p.get("cadenceLength")),
                       "ele": ele, "dist": dist, "pwr": power,
                       "bb": None, "gap": None, "source": "coros"})
    native = data.get("summary") or {}
    _gct, _vo = dynamics(native)
    _sport = number(data.get("sportType") or native.get("sportType"))
    _is_run = _sport is not None and int(_sport) in (100, 101, 102, 103)
    if _gct is None and _vo is None and _is_run:
        # 高驰官方没公开跑步动态的字段名，只能按候选名试。真没有就留一条
        # 带字段名的诊断记录 —— 用户把「诊断日志」发过来就能确认到底叫什么。
        try:
            from . import diag as _dg
            _dg.log_exc("coros.dynamics",
                        "未找到触地/垂直振幅，summary 字段："
                        + ", ".join(sorted(str(k) for k in native)[:40]))
        except Exception:
            pass
    _gct, _vo = dynamics(native)
    summary = {"cad_avg": number(native.get("avgCadence")),
               "cad_max": number(native.get("maxCadence")),
               "stride_avg": number(native.get("avgStepLen")),
               "hr_run_avg": number(native.get("avgHr")),
               "gct_ms": _gct, "vo_cm": _vo,
               "all_samples": len(points), "repaired": 0, "rescaled": 0,
               "drift": None}
    # lapList 包含公里、5 公里、10 公里和全程汇总，只选一组。
    groups = data.get("lapList") or []
    if not isinstance(groups, list) or any(not isinstance(g, dict) for g in groups):
        raise CorosError("高驰分圈数据格式已变化")
    native_summary = data.get("summary") or {}
    laps = []
    # Prefer the native kilometre splits, not whichever 5/10 km group comes first.
    candidates = [g for g in groups if g.get("lapItemList") and g.get("type") != -1]
    selected = next((g for g in candidates if number(g.get("lapDistance")) == 100000),
                    candidates[0] if candidates else {})
    items = selected.get("lapItemList") or []
    for i, lap in enumerate(items):
        if not isinstance(lap, dict):
            raise CorosError("高驰分圈数据格式已变化")
        dist = number(lap.get("distance"), 100) or 0      # 厘米 → 米
        duration = number(lap.get("time", lap.get("totalTime")), 100) or 0  # 厘秒 → 秒
        if not duration:
            t0 = number(lap.get("startTimestamp"))
            t1 = number(lap.get("endTimestamp"))
            if t0 and t1 and t1 > t0:
                duration = (t1 - t0) / 100
        if dist <= 0 and duration <= 0:
            continue
        stride = number(lap.get("avgStrideLength", lap.get("avgStepLen")))
        # 高驰 avgCadence 是总步频（summary 同口径），不做佳明那边的单侧值翻倍
        cad = number(lap.get("avgCadence"))
        laps.append({"idx": int(number(lap.get("lapIndex")) or (i + 1)), "dist_m": dist,
                     "dur_s": duration,
                     "pace_s": round(duration * 1000 / dist) if dist > 50 and duration else None,
                     "hr_avg": number(lap.get("avgHr")), "hr_max": number(lap.get("maxHr")),
                     "cad": cad, "stride": stride,
                     "ele_gain": number(lap.get("elevGain") or lap.get("ascent")),
                     "cad_server": cad, "stride_server": stride,
                     # 不足一公里的尾圈用暗色标记；间歇的短圈另有完整配速，不算 partial
                     "is_partial": False})
    if len(laps) > 1 and laps[-1]["dist_m"] < 900 and \
            laps[-1]["dist_m"] < max(l["dist_m"] for l in laps[:-1]) * 0.9:
        laps[-1]["is_partial"] = True
    # summary 的最大速度是「秒/公里」，换算成 m/s 给卡片用（列表接口的 maxSpeed 常为 0）
    max_pace = number(native_summary.get("maxSpeed"))
    row_metrics = {"aerobic_te": number(native_summary.get("aerobicEffect")),
                   "anaerobic_te": number(native_summary.get("anaerobicEffect")),
                   "max_speed_mps": round(1000 / max_pace, 3) if max_pace and max_pace > 0 else None}
    for target, key, divisor in (
            ("distance_m", "distance", 100), ("duration_s", "totalTime", 100),
            ("pace_s_km", "avgSpeed", 1), ("avg_hr", "avgHr", 1),
            ("max_hr", "maxHr", 1), ("ascent", "elevGain", 1),
            ("load", "trainingLoad", 1), ("calories", "calories", 1000)):
        value = number(native_summary.get(key), divisor)
        if value is not None:
            row_metrics[target] = value
    zones = next((g.get("zoneItemList") or [] for g in data.get("zoneList") or []
                  if g.get("type") == 126), [])
    hr_zones = [{"index": int(number(z.get("zoneIndex")) or 0),
                 "seconds": number(z.get("second")) or 0}
                for z in sorted(zones, key=lambda z: number(z.get("zoneIndex")) or 0)]
    track_points = track if track else act.build_track(canonical)
    return {"activity_id": str(activity_id), "source": "coros",
            "adapter_version": ADAPTER_VERSION,
            "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
            "summary": summary, "laps": laps, "hr_zones": hr_zones,
            "lap_distance_m": number(selected.get("lapDistance"), 100),
            "series": {"points": act._thin(points, act.SERIES_POINTS)},
            "track": act._thin(track_points, act.TRACK_POINTS),
            "route": route.build(canonical),
            "weather": None, "row_metrics": {k: v for k, v in row_metrics.items() if v is not None}}


def is_current(detail: dict) -> bool:
    """缓存是否是新口径（旧缓存没有版本号，必须重抓）。"""
    return bool(detail) and detail.get("source") == "coros" and \
        detail.get("adapter_version") == ADAPTER_VERSION


def ensure(store, home, row: dict, email=None, password=None, force: bool = False) -> dict:
    aid = str(row["activity_id"])
    cached = store.get_detail(aid)
    if not force and is_current(cached):
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
