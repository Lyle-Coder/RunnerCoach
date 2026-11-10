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

# 7 = 垂直振幅命中 avgVertVibration、分圈空解析留诊断；废弃 6 的旧缓存
ADAPTER_VERSION = 7


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
            "contactTime", "contactTimeMs", "avgGct", "gct", "groundTime",
            "avgGroundTime", "avgStanceTime", "stanceTime", "stanceTimeMs")
# 高驰真机 summary 里垂直振幅叫 avgVertVibration（垂直振动，cm）；
# avgVertRatio 是「垂直步幅比 %」，值域 6~12 会撞进 2~25cm 的合理区间，
# 绝不能混进来，否则卡片会印一个假振幅。
VO_KEYS = ("avgVertVibration", "vertVibration", "verticalVibration",
           "avgVerticalOscillation", "verticalOscillation", "avgVertOscillation",
           "vertOscillation", "avgVosc", "vosc", "verticalOscillationCm",
           "avgVerticalOscillationCm", "vertOsc")


def dynamics(data: dict) -> tuple[float | None, float | None]:
    """在整个明细里取（触地时间 ms，垂直振幅 cm）。

    高驰官方没公开跑步动态在明细里的位置，summary 只是猜测之一——
    真实字段可能在任意子对象里，所以做限深递归：值命中候选键后仍要过
    量纲校验（触地 60~600 ms，垂直振幅 2~25 cm），宁可显示「—」也不印
    错数量级的数字。
    """
    if not isinstance(data, dict):
        return None, None
    pools: list[dict] = []
    budget = [300]                      # 明细里的 points 是海量秒级采样，别全扫

    def collect(obj, depth: int = 0) -> None:
        if depth > 4 or budget[0] <= 0:
            return
        if isinstance(obj, dict):
            budget[0] -= 1
            pools.append(obj)
            for v in obj.values():
                collect(v, depth + 1)
        elif isinstance(obj, list) and obj:
            for v in obj[:3]:           # list 只探前几个元素，防 points 拖垮
                collect(v, depth + 1)

    collect(data)

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


def key_tree(data: dict, limit: int = 700) -> str:
    """明细的键树摘要（顶层键与二级键），诊断日志用——字段名谜底的钥匙。"""
    parts = []
    for k, v in (data or {}).items():
        if isinstance(v, dict):
            parts.append(k + "{" + ",".join(sorted(str(x) for x in v))[:120] + "}")
        elif isinstance(v, list):
            inner = v[0] if v and isinstance(v[0], dict) else None
            more = "{" + ",".join(sorted(str(x) for x in inner))[:80] + "}" if inner else ""
            parts.append(f"{k}[{len(v)}]{more}")
        else:
            parts.append(str(k))
    return " | ".join(parts)[:limit]


def _diag_laps(groups: list, selected: dict, parsed: int) -> None:
    """分圈解析为空时记录 lapList 与首条 item 的真实字段，用户发诊断日志即可定位。

    只在解析出 0 圈时写：既覆盖「组缺失」，也覆盖「组在但 item 字段名不
    匹配被全跳过」——后者正是真机反馈里没有 coros.laps 条目的原因。
    """
    try:
        from . import diag as _dg
        parts = [f"type={g.get('type')} lapDistance={g.get('lapDistance')} "
                 f"items={len(g.get('lapItemList') or [])}" for g in groups or []]
        sample = ""
        items = (selected or {}).get("lapItemList") or []
        if items and isinstance(items[0], dict):
            first = items[0]
            sample = (" | 首条 keys=" + ",".join(sorted(str(k) for k in first))[:220]
                      + " 值=" + ",".join(f"{k}:{str(v)[:10]}"
                                         for k, v in list(first.items())[:14]))
        _dg.log_exc("coros.laps", f"laps={parsed} 组：" + (" | ".join(parts) or "缺失") + sample)
    except Exception:
        pass


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
    _gct, _vo = dynamics(data)
    _sport = number(data.get("sportType") or native.get("sportType"))
    _is_run = _sport is not None and int(_sport) in (100, 101, 102, 103)
    if _gct is None and _vo is None and _is_run:
        # 整个明细都没找到触地/垂直振幅：把键树记下来，用户发「诊断日志」
        # 就能看到真实字段在哪一层、叫什么——这是唯一的定位手段。
        try:
            from . import diag as _dg
            _dg.log_exc("coros.dynamics", "未找到跑步动态，键树：" + key_tree(data))
        except Exception:
            pass
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
    if not candidates:
        # 有的活动（如运动场跑）lapList 里可能只有 type=-1 的组或干脆没有分圈，
        # 宁可用最后一组也不静默丢掉。
        fallback = [g for g in groups if g.get("lapItemList")]
        if fallback:
            # 选分圈条数最多的一组（type=-1 组可能只是全程汇总，只有 1 条）
            selected = max(fallback, key=lambda g: len(g["lapItemList"]))
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
    if not laps:
        # 组在但一条都解析不出（字段名不匹配）时同样要留证据，
        # 否则用户只能看到「无分圈数据」而我们无从下手。
        _diag_laps(groups, selected, 0)
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
