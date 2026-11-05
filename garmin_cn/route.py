"""把一次活动的「几何」和「时序」合成一条可直接播放的轨迹。

为什么要单独合一次
------------------
Garmin `/details` 里其实有两套东西：

* ``geoPolylineDTO.polyline`` —— 每点带 lat / lon / time(ms) / speed，是**几何**
* ``activityDetailMetrics``    —— 1Hz 的心率 / 步频 / 累计里程，是**时序**

但 `activity.build_track()` 只留下 lat/lon（丢 time/speed），
`build_series()` 只留时序（丢坐标）。于是库里 60 多条活动的 payload 里，
**没有任何一处同时有「时间 + 坐标 + 心率」**——而播放要的正是三者同点。

实测（2026-11-03 那次 2.73 km）：polyline 891 点、1Hz 891 行、首点时间差 0.0 s，
逐点对齐。但仍按**时间戳**建索引，不依赖「点数相等」这个巧合——开了「暂停计
时」或服务端抽稀的活动会点数不等。

两个踩点的记录
--------------
* 里程不取 ``distanceInMeters``：实测 891 个点全为 ``None``，只能自己用
  haversine 累加几何距离。
* 不能只用 1Hz 序列的 ``sumDistance``：信号丢失时它会跳变，而几何距离是连续
  的。两者差异超过 5% 时以时序为准并在返回值里标记 ``drift``。
"""
from __future__ import annotations

import bisect
import math

from .activity import _cols
from .metrics import num

# 采样中断超过这个秒数就断线：GPS 丢失期间不能画一条直线穿过建筑
GAP_S = 12.0
# haversine 用的地球半径（IUGG 平均半径，米）
EARTH_R = 6371008.8


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点球面距离（米）。短距离下与椭球差异 <0.5%，对画图足够。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def _index(cols: dict, key: str) -> tuple[list[int], list[float]]:
    """时间戳（毫秒，升序）→ 某个 1Hz 指标 的索引表，供 bisect 取最近点。"""
    ts = cols.get("directTimestamp") or []
    raw = cols.get(key) or []
    pairs: list[tuple[int, float]] = []
    for t, v in zip(ts, raw):
        x = num(v)
        if t is None or x is None:
            continue
        pairs.append((int(t), x))
    pairs.sort(key=lambda p: p[0])
    return [p[0] for p in pairs], [p[1] for p in pairs]


def _nearest(keys: list[int], vals: list[float], ms: int,
             tol: int = 1500) -> float | None:
    """取最接近 ms 的值；超过 tol 毫秒视为没有采样，返回 None。"""
    if not keys:
        return None
    i = bisect.bisect_left(keys, ms)
    cand = [j for j in (i - 1, i) if 0 <= j < len(keys)]
    if not cand:
        return None
    j = min(cand, key=lambda k: abs(keys[k] - ms))
    return vals[j] if abs(keys[j] - ms) <= tol else None


def _smooth(vals: list[float], win: int = 5) -> list[float]:
    """居中滑动平均：留着原始波峰给心率很吵，去掉又看不出区间，5 点折中。"""
    n = len(vals)
    if n < win:
        return list(vals)
    half = win // 2
    out = []
    for i in range(n):
        a, b = max(0, i - half), min(n, i + half + 1)
        out.append(sum(vals[a:b]) / (b - a))
    return out


def build(details: dict, *, gap_s: float = GAP_S) -> dict:
    """合成轨迹。返回紧凑数组（体积约为对象数组的 1/4），字段见文件头。"""
    poly = ((details.get("geoPolylineDTO") or {}).get("polyline")) or []
    cols = _cols(details)
    k_hr, v_hr = _index(cols, "directHeartRate")
    # polyline 自带的 speed 实测全为 0（Garmin 只对部分运动填充），
    # 真速度在 1Hz 序列的 directSpeed 里
    k_sp, v_sp = _index(cols, "directSpeed")

    t_rel: list[int] = []
    lats: list[float] = []
    lons: list[float] = []
    dists: list[float] = []
    hrs: list[int | None] = []
    spds: list[float | None] = []
    segs: list[int] = []

    t0 = None
    prev = None            # 上一个有效点 (ms, lat, lon)
    acc = 0.0
    seg = 0
    for p in poly:
        ms = num(p.get("time"))
        lat, lon = num(p.get("lat")), num(p.get("lon"))
        # valid=False 是手表判定的漂移点，留着会画出锯齿
        if ms is None or lat is None or lon is None or p.get("valid") is False:
            continue
        ms = int(ms)
        if t0 is None:
            t0 = ms
        if prev is not None:
            gap = (ms - prev[0]) / 1000.0
            if gap > gap_s:
                # 断线：段号 +1，距离不再累加（中间那段本来就没记录）
                seg += 1
            elif gap > 0:
                acc += haversine(prev[1], prev[2], lat, lon)
        t_rel.append(int(round((ms - t0) / 1000.0)))
        lats.append(round(lat, 6))
        lons.append(round(lon, 6))
        dists.append(round(acc, 1))
        h = _nearest(k_hr, v_hr, ms)
        hrs.append(int(round(h)) if h else None)
        s = _nearest(k_sp, v_sp, ms)
        spds.append(round(s, 2) if s is not None else None)
        segs.append(seg)
        prev = (ms, lat, lon)

    # 几何距离 vs 手表累计里程：偏差大说明 polyline 被抽稀得厉害，改用 sumDistance
    k_sd, v_sd = _index(cols, "sumDistance")
    drift = 0.0
    if k_sd and acc > 0:
        official = v_sd[-1]
        drift = abs(acc - official) / max(official, 1.0)
        if drift > 0.05:
            for i, tr in enumerate(t_rel):
                j = bisect.bisect_left(k_sd, t0 + tr * 1000)
                j = min(max(j, 0), len(k_sd) - 1)
                dists[i] = round(v_sd[j], 1)
            # 插值后可能轻微回退，累一遍保证单调（播放器按下标推进）
            for i in range(1, len(dists)):
                if dists[i] < dists[i - 1]:
                    dists[i] = dists[i - 1]

    # 速度缺失的点用几何差分补：播放器要靠它判断「在跑还是在走」
    good = [v for v in spds if v is not None]
    if len(good) < len(spds) * 0.5:
        for i in range(len(spds)):
            a, b = max(0, i - 1), min(len(spds) - 1, i + 1)
            dt = t_rel[b] - t_rel[a]
            spds[i] = round((dists[b] - dists[a]) / dt, 2) if dt > 0 else 0.0
    spds = [round(x, 2) if x is not None else None for x in
            _smooth([s if s is not None else 0.0 for s in spds], 5)]

    return {
        "t0": t0,
        "n": len(t_rel),
        "t": t_rel,
        "lat": lats,
        "lon": lons,
        "d": dists,
        "hr": hrs,
        "v": spds,
        "seg": segs,
        "drift": round(drift, 4),
    }


def points(rt: dict) -> list[dict]:
    """展开成对象数组，给照片匹配之类的消费者用。"""
    return [{"t": rt["t"][i], "lat": rt["lat"][i], "lon": rt["lon"][i],
             "d": rt["d"][i], "hr": rt["hr"][i], "v": rt["v"][i],
             "seg": rt["seg"][i]} for i in range(rt.get("n") or 0)]


def _idx_at(rt: dict, sec: float) -> int:
    """时间（相对秒）→ 最近的数组下标。"""
    t = rt.get("t") or []
    if not t:
        return -1
    return min(max(bisect.bisect_left(t, sec), 0), len(t) - 1)


def at(rt: dict, sec: float) -> dict:
    """取某一时刻的插值状态：位置按时间在两采样点间线性插值，心率取整。"""
    t = rt.get("t") or []
    if not t:
        return {}
    i = _idx_at(rt, sec)
    if i <= 0 or sec <= t[0]:
        j, k, w = 0, 0, 0.0
    else:
        j = i - 1 if t[i] > sec else i
        k = min(j + 1, len(t) - 1)
        span = (t[k] - t[j]) or 1
        w = min(max((sec - t[j]) / span, 0.0), 1.0)
    lat = rt["lat"][j] + (rt["lat"][k] - rt["lat"][j]) * w
    lon = rt["lon"][j] + (rt["lon"][k] - rt["lon"][j]) * w
    dist = rt["d"][j] + (rt["d"][k] - rt["d"][j]) * w
    hr = rt["hr"][k] if w >= 0.5 else rt["hr"][j]
    if hr is None:
        hr = rt["hr"][j] or rt["hr"][k]
    return {"t": sec, "lat": lat, "lon": lon, "d": dist, "hr": hr,
            "seg": rt["seg"][j], "i": j}


def pace(rt: dict, sec: float, win: float = 15.0) -> float | None:
    """滚动窗口配速（秒/公里）。

    1Hz 的瞬时配速在 ±40s/km 之间乱摆，直接用会让角标跳成噪音；
    取 15 s 窗口内「距离增量 / 时间增量」，并且窗口内必须有位移。
    """
    a = at(rt, max(sec - win, 0.0))
    b = at(rt, sec)
    if not a or not b:
        return None
    dd = b["d"] - a["d"]
    dt = sec - max(sec - win, 0.0)
    if dd < 5 or dt <= 0:
        return None
    return dt / dd * 1000.0


def summary(rt: dict) -> dict:
    """播放器要用的几个总数，省得前端再算一遍。"""
    n = rt.get("n") or 0
    if not n:
        return {"n": 0}
    t = rt["t"]
    d = rt["d"]
    hr = [h for h in rt["hr"] if h]
    move = sum(1 for v in rt["v"] if v and v > 0.5)
    return {
        "n": n,
        "duration_s": t[-1] if t else 0,
        "distance_m": d[-1] if d else 0,
        "elapsed_s": move,                     # 有位移的秒数，用来算平均配速
        "hr_avg": round(sum(hr) / len(hr), 1) if hr else None,
        "hr_max": max(hr) if hr else None,
        "pace_s_km": round(t[-1] / max(d[-1], 1) * 1000, 1) if d and d[-1] else None,
        "segments": (rt["seg"][-1] + 1) if rt.get("seg") else 1,
        "drift": rt.get("drift"),
    }
