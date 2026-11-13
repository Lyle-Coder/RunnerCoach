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

# 12 = 接入高驰天气，旧缓存重新抓取以补齐天气
ADAPTER_VERSION = 13


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
           "avgVertVibrationCm", "avgVertVib", "vertVib",
           "avgVerticalOscillation", "verticalOscillation", "avgVertOscillation",
           "vertOscillation", "avgVosc", "vosc", "voCm", "avgVoCm",
           "verticalOscillationCm", "avgVerticalOscillationCenti",
           "avgVerticalOscillationCm", "vertOsc")


def laps_from_points(points: list[dict], step_m: float = 1000.0) -> list[dict]:
    """分圈兜底：lapList 缺失/解析不出时，用逐点累计距离按 1 km 切。

    真机上见过「活动有完整时序、但 lapList 只给了 type=-1 的全程汇总」，
    于是底部一整块是「无分圈数据」——图看着像坏了。这时用 sumDistance
    自己切，虽然不如手表计圈精确，但至少每公里配速/心率/步频都在。
    """
    pts = [p for p in (points or [])
           if p.get("t") is not None and p.get("dist") is not None]
    if len(pts) < 30:
        return []
    pts.sort(key=lambda p: p["t"])
    laps: list[dict] = []
    t0 = pts[0]["t"]
    d0 = pts[0]["dist"] or 0.0
    nxt = d0 + step_m
    acc: dict[str, list[float]] = {"hr": [], "cad": [], "stride": []}

    def close(p_end: dict, idx: int) -> None:
        d = (p_end["dist"] or 0.0) - d0
        dur = (p_end["t"] or 0) - t0
        if d <= 20 or dur <= 1:
            return
        laps.append({
            "idx": idx, "dist_m": round(d, 1), "dur_s": round(dur, 1),
            "pace_s": round(dur / (d / 1000)) if d > 50 else None,
            "hr_avg": round(sum(acc["hr"]) / len(acc["hr"])) if acc["hr"] else None,
            "hr_max": None,
            "cad": round(sum(acc["cad"]) / len(acc["cad"]), 1) if acc["cad"] else None,
            "stride": (round(sum(acc["stride"]) / len(acc["stride"]), 1)
                       if acc["stride"] else None),
            "ele_gain": None, "cad_server": None, "stride_server": None,
            "is_partial": d < 900,
        })

    for p in pts[1:]:
        for k in ("hr", "cad", "stride"):
            v = p.get(k)
            if v:
                acc[k].append(v)
        if (p["dist"] or 0.0) >= nxt:
            close(p, len(laps) + 1)
            t0, d0 = p["t"], p["dist"] or 0.0
            nxt = d0 + step_m
            acc = {"hr": [], "cad": [], "stride": []}
    if pts[-1]["dist"] is not None and (pts[-1]["dist"] or 0) - d0 > 100:
        close(pts[-1], len(laps) + 1)
    return laps


def dynamics(data: dict) -> tuple[float | None, float | None]:
    """全程触地时间 ms / 垂直振幅 cm；只读汇总，不能取第一圈或第一点。

    Training Hub 的 summary 可能全为 0，但 graphList 仍有有效均值：
    groundTime.graphItem.avg 是 ms，verticalVibration.graphItem.avg 是 mm。
    按 key 匹配，不依赖图表顺序，也不把 verticalStrideRatio 当成振幅。
    """
    if not isinstance(data, dict):
        return None, None

    def valid(value, low, high, divisor=1):
        value = number(value, divisor)
        return value if value is not None and low <= value <= high else None

    def pick(pool, keys, low, high, scale):
        if not isinstance(pool, dict):
            return None
        for key in keys:
            value = number(pool.get(key))
            if value is None:
                continue
            # 兼容已有 summary 别名的秒/米口径；显式单位键不猜测。
            if not key.endswith(("Ms", "Cm", "Centi")) and value < low:
                value *= scale
            value = valid(value, low, high)
            if value is not None:
                return value
        return None

    summary = data.get("summary")
    gct = pick(summary, GCT_KEYS, 60, 600, 1000)
    vo = pick(summary, VO_KEYS, 2, 25, 100)
    graphs = data.get("graphList")
    for graph in graphs if isinstance(graphs, list) else []:
        if not isinstance(graph, dict):
            continue
        item = graph.get("graphItem")
        if not isinstance(item, dict):
            continue
        if graph.get("key") == "groundTime" and gct is None:
            gct = valid(item.get("avg"), 60, 600)
        elif graph.get("key") == "verticalVibration" and vo is None:
            vo = valid(item.get("avg"), 2, 25, divisor=10)
    # 某些旧响应直接把汇总放在顶层；不递归进入分圈与逐点采样。
    if gct is None:
        gct = pick(data, GCT_KEYS, 60, 600, 1000)
    if vo is None:
        vo = pick(data, VO_KEYS, 2, 25, 100)
    return gct, vo


# Training Hub 的 provider=1 为 AccuWeather，provider=2 为心知天气；
# 天气代码是两个不同枚举。映射核对自高驰官方中文 locale（2026-11-11）。
WEATHER_TEXT = {1: {1: '晴',
     2: '晴时多云',
     3: '局部晴朗',
     4: '晴间多云',
     5: '阳光朦胧',
     6: '大部多云',
     7: '多云',
     8: '阴天',
     11: '雾',
     12: '阵雨',
     13: '大部多云转阵雨',
     14: '局部晴朗转阵雨',
     15: '暴风雨',
     16: '大部多云转暴风雨',
     17: '局部晴朗转暴风雨',
     18: '雨',
     19: '小雪',
     20: '大部多云转小雪',
     21: '局部晴朗转小雪',
     22: '中雪',
     23: '大部多云转中雪',
     24: '冰',
     25: '雨夹雪',
     26: '冰雨',
     29: '雨雪',
     30: '高温',
     31: '低温',
     32: '风',
     33: '晴',
     34: '大部晴朗',
     35: '局部多云',
     36: '晴间多云',
     37: '月色朦胧',
     38: '大部多云',
     39: '局部多云转阵雨',
     40: '大部多云转阵雨',
     41: '局部多云转暴风雨',
     42: '大部多云转暴风雨',
     43: '大部多云转小雪',
     44: '大部多云转中雪'},
 2: {0: '晴',
     1: '晴',
     2: '晴',
     3: '晴',
     4: '多云',
     5: '晴间多云',
     6: '大部多云',
     7: '晴间多云',
     8: '大部多云',
     9: '阴',
     10: '阵雨',
     11: '雷阵雨',
     12: '雷阵雨伴有冰雹',
     13: '小雨',
     14: '中雨',
     15: '大雨',
     16: '暴雨',
     17: '大暴雨',
     18: '特大暴雨',
     19: '冻雨',
     20: '雨夹雪',
     21: '阵雪',
     22: '小雪',
     23: '中雪',
     24: '大雪',
     25: '暴雪',
     26: '浮尘',
     27: '扬沙',
     28: '沙尘暴',
     29: '强沙尘暴',
     30: '雾',
     31: '霾',
     32: '风',
     33: '大风',
     34: '飓风',
     35: '热带风暴',
     36: '龙卷风',
     37: '低温',
     38: '高温'}}
WIND_DIRECTIONS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def build_weather(weather: dict | None) -> dict | None:
    """高驰天气 → activity.build_weather 的共用契约。

    与 Training Hub 活动页一致：temperature / humidity / windSpeed 除以 10，
    分别得到 ℃ / % / km/h；风向使用 windDirectionType 的八方位枚举。
    不使用佳明的华氏温度换算，也不请求活动地点的当前天气。
    """
    if not isinstance(weather, dict) or not weather:
        return None

    def scaled(key, low, high):
        value = number(weather.get(key), 10)
        return value if value is not None and low <= value <= high else None

    direction = number(weather.get("windDirectionType"))
    wind_dir = (WIND_DIRECTIONS[int(direction)]
                if direction is not None and direction.is_integer() and 0 <= direction < 8
                else None)
    provider = number(weather.get("provider"))
    code = number(weather.get("weatherType"))
    result = {
        "temp_c": scaled("temperature", -100, 100),
        "feels_c": scaled("bodyFeelTemp", -100, 100),
        "humidity": scaled("humidity", 0, 100),
        "wind_dir": wind_dir,
        "wind_speed": scaled("windSpeed", 0, 500),
        "text": WEATHER_TEXT.get(provider, {}).get(code),
        "station": None,
    }
    return result if any(value is not None for value in result.values()) else None


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


def _lite_laps(group: dict, cap: int = 40) -> list[dict]:
    """分圈组 → 判定用的最小圈信息（距离 / 时长 / 心率），不带展示字段。

    高驰的 lapList 往往同时有「自动公里圈」「手动计圈」「全程汇总」几组，
    哪一组才是间歇课的分段无法预设，所以每组都留一份轻量副本给判定层。
    """
    out: list[dict] = []
    for lap in (group.get("lapItemList") or [])[:cap]:
        if not isinstance(lap, dict):
            continue
        d = number(lap.get("distance"), 100) or 0     # 厘米 → 米
        t = number(lap.get("time", lap.get("totalTime")), 100) or 0   # 厘秒 → 秒
        if d > 0 and t > 0:
            out.append({"dist_m": d, "dur_s": t,
                        "hr_avg": number(lap.get("avgHr"))})
    return out


def lap_groups_of(groups: list) -> list[dict]:
    """lapList 各组 → [{type, lap_m, laps}]（最多 6 组）。出图不需要，判定需要。"""
    out: list[dict] = []
    for g in groups or []:
        if not isinstance(g, dict):
            continue
        laps = _lite_laps(g)
        if not laps:
            continue
        out.append({"type": g.get("type"),
                    "lap_m": number(g.get("lapDistance"), 100),
                    "laps": laps})
        if len(out) >= 6:
            break
    return out


def _diag_training(data: dict, lap_groups: list) -> None:
    """记录高驰是否暴露了「训练/间歇」类字段，以及各组圈距。

    高驰没有佳明 `hasIntensityIntervals` 那样的官方标记（官方文档也未公开），
    现阶段靠「手动计圈结构」判定。这条日志是为了在朋友下次同步后确认：
    接口里到底有没有可直接用的 interval/workout 字段——有就换成权威判定。
    """
    try:
        from . import diag as _dg

        native = data.get("summary") or {}
        keys = []
        for src, tag in ((native, "summary"), (data, "top")):
            if isinstance(src, dict):
                keys += [f"{tag}.{k}" for k in src
                         if any(s in str(k).lower() for s in
                                ("interval", "lap", "workout", "train", "plan", "session"))]
        groups = " | ".join(f"type={g['type']} lap_m={g['lap_m']} n={len(g['laps'])}"
                            for g in lap_groups) or "无分圈组"
        _dg.log_exc("coros.training",
                    "训练相关字段：" + (",".join(keys) or "无") + " ｜ " + groups)
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
    from .coros import SPORT_TYPE
    from .sports import family, profile
    sport_key = SPORT_TYPE.get(int(_sport), "other") if _sport is not None else "other"
    bike = family(sport_key) == "bike"
    if bike:
        for point in points:
            point["stride"] = None
    if (_gct is None or _vo is None) and _is_run:
        # 有任一跑步动态缺失：把键树记下来，用户发「诊断日志」
        # 就能看到真实字段在哪一层、叫什么。
        try:
            from . import diag as _dg

            # 除了键树，再单独列出候选键和它们的**值**——真机上 summary
            # 有 avgGroundTime / avgVertVibration 却仍然报「未找到」，多半是
            # 值为 0 或量纲不同。只列键名（旧版做法）永远查不出是哪一种。
            # 触地类键名不含 vert/osc/vib，以前被漏掉，一并收进来。
            hits = []

            def _scan(obj, path="", depth=0):
                if depth > 4 or len(hits) > 16:
                    return
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        kk = str(k)
                        low = kk.lower()
                        if any(t in low for t in ("vert", "osc", "vib",
                                                  "ground", "contact", "gct",
                                                  "stance")):
                            hits.append(f"{path}{kk}={str(v)[:12]}")
                        _scan(v, f"{path}{kk}.", depth + 1)
                elif isinstance(obj, list) and obj:
                    _scan(obj[0], path + "[0].", depth + 1)

            _scan(data)
            _dg.log_exc("coros.dynamics",
                        "未找到跑步动态｜疑似键：" + ("；".join(hits) or "无")
                        + "｜键树：" + key_tree(data))
        except Exception:
            pass
    summary = {"cad_avg": number(native.get("avgCadence")),
               "cad_max": number(native.get("maxCadence")),
               "stride_avg": number(native.get("avgStepLen")),
               "hr_run_avg": number(native.get("avgHr")),
               "gct_ms": _gct, "vo_cm": _vo,
               "all_samples": len(points), "repaired": 0, "rescaled": 0,
               "drift": None}
    from .cycling import analyze
    cycling = analyze(points, profile("bike").get("ftp_w")) if bike else None
    if cycling:
        summary["cad_avg"] = summary["cad_avg"] if summary["cad_avg"] is not None else cycling["cad_avg"]
        summary["stride_avg"] = None
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
                     "pwr_avg": number(lap.get("avgPower")),
                     # 不足一公里的尾圈用暗色标记；间歇的短圈另有完整配速，不算 partial
                     "is_partial": False})
    if len(laps) > 1 and laps[-1]["dist_m"] < 900 and \
            laps[-1]["dist_m"] < max(l["dist_m"] for l in laps[:-1]) * 0.9:
        laps[-1]["is_partial"] = True
    if not laps:
        # 组在但一条都解析不出（字段名不匹配）时同样要留证据，
        # 否则用户只能看到「无分圈数据」而我们无从下手。
        _diag_laps(groups, selected, 0)
    # 兜底：lapList 给不出分圈时，用逐点累计距离自己按 1 km 切，
    # 别让底部一整块变成「无分圈数据」。
    # 注意「只有 1 圈且那圈就是全程汇总」也是同一种残缺——上一版只在
    # laps 完全为空时才兜底，于是这种情况仍然显示「1 圈 · 全程 5.80 km」。
    _only_summary = len(laps) <= 1 and str(selected.get("type")) == "-1"
    if (not laps or _only_summary) and len(points) >= 30:
        laps = laps_from_points(points)
        if laps:
            try:
                from . import diag as _dg

                _dg.log_exc("coros.laps_auto",
                            f"lapList 无有效分圈，已按逐点切出 {len(laps)} 圈")
            except Exception:
                pass
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
                 "seconds": number(z.get("second")) or 0,
                 "low_bpm": number(z.get("leftScope")),
                 "high_bpm": number(z.get("rightScope"))}
                for z in sorted(zones, key=lambda z: number(z.get("zoneIndex")) or 0)]
    track_points = track if track else act.build_track(canonical)
    lap_groups = lap_groups_of(groups)
    _diag_training(data, lap_groups)
    return {"activity_id": str(activity_id), "source": "coros", "sport": sport_key,
            **({"cycling": cycling} if bike else {}),
            "adapter_version": ADAPTER_VERSION,
            "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
            "summary": summary, "laps": laps, "hr_zones": hr_zones,
            "lap_distance_m": number(selected.get("lapDistance"), 100),
            # 抓取时已确认「这条活动本来就没有分圈」（短距离、无轨迹采样），
            # 缓存健康检查据此放行，避免每次看图都重新联网
            "laps_absent_ok": not laps,
            # 间歇判定用：各组圈距/圈数（含手动计圈组），展示层不用
            "lap_groups": lap_groups,
            "series": {"points": act._thin(points, act.SERIES_POINTS)},
            "track": act._thin(track_points, act.TRACK_POINTS),
            "route": route.build(canonical),
            "weather": build_weather(data.get("weather")), "row_metrics": {k: v for k, v in row_metrics.items() if v is not None}}


def is_current(detail: dict) -> bool:
    """缓存是否**既新口径、又没残缺**。

    只比版本号不够：真机上见过一次残缺响应（时序有、lapList 只有全程汇总），
    按版本号会一直认它，于是「分圈空 + 没有跑步动态」永远好不了。
    所以再加一条健康检查：有足够轨迹点却没有分圈 → 视为残缺，下次重抓。
    确实本来就没有分圈的（短距离、无采样）由 `laps_absent_ok` 放行，
    不会每次看图都联网。
    """
    if not detail or detail.get("source") != "coros" or \
            detail.get("adapter_version") != ADAPTER_VERSION:
        return False
    if detail.get("laps"):
        return True
    return bool(detail.get("laps_absent_ok"))


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
