"""间歇训练识别：从「逐点速度序列」或「分圈」里找出快慢交替，并汇总成两组。

纯标准库、零 IO。输入是 `activity.py` 产出的 `series.points` / `laps`，
输出是给活动图底部双栏用的纯数据；识别不出来返回 `None`，版式完全不变。

为什么要两条路
--------------
佳明有两种记圈方式：手动按圈（热身 2 km、快 400 m、慢 400 m……放松 1 km）
时，`laps` 的配速直接呈快慢交替；但自动「每公里分圈」会把 400 m 快跑和
400 m 慢跑平均进同一圈，`laps` 看起来几乎是匀速。所以优先看逐点速度
（`spd`，m/s），不满足条件再回落到分圈。

防误报优先于多召回
------------------
* 快段至少 2 组，每组 >= `MIN_SEG_S` 秒且 >= `MIN_SEG_M` 米；
* 快组平均速度至少比慢组高 `MIN_RATIO` 倍；
* 快慢必须真正交替（段切换 >= 3 次），匀速跑的抖动、单调加速都不算；
* 阈值用一维 Otsu 自动二分，不写死配速，节奏跑 / 法特莱克 / 坡冲都能覆盖。
"""
from __future__ import annotations

# 一组快跑至少持续这么久、跑这么远，才不是 GPS 抖动或起跑冲刺
MIN_SEG_S = 20.0
MIN_SEG_M = 80.0
# 快组平均速度 / 慢组平均速度 的下限
MIN_RATIO = 1.15
MIN_SETS = 2
MIN_SWITCHES = 3
# 每栏最多列几行（常见的 400 m 间歇是 12~15 组，行距压紧后能全部列全），
# 再多才折叠成「其余 N 组」。
MAX_GROUPS = 16


def _split(values: list[float]) -> tuple[float, float, float]:
    """一维 Otsu：返回 (阈值, 低组均值, 高组均值)。样本不足时退化为中位数。"""
    s = sorted(v for v in values if v is not None and v > 0)
    n = len(s)
    if n < 4:
        m = s[n // 2] if s else 0.0
        return m, m, m
    prefix = [0.0]
    for v in s:
        prefix.append(prefix[-1] + v)
    best, k = -1.0, n // 2
    for i in range(2, n - 1):
        m0 = prefix[i] / i
        m1 = (prefix[n] - prefix[i]) / (n - i)
        var = i * (n - i) * (m0 - m1) ** 2
        if var > best:
            best, k = var, i
    thr = (s[k - 1] + s[k]) / 2
    return thr, prefix[k] / k, (prefix[n] - prefix[k]) / (n - k)


def _wmean(vals: list[float], weights: list[float]) -> float | None:
    pairs = [(v, w) for v, w in zip(vals, weights) if v and w and w > 0]
    if not pairs:
        return None
    tw = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / tw if tw else None


def _runs(flags: list[bool]) -> list[tuple[bool, int, int]]:
    """连续同值段 → [(值, 起, 止)]，止为开区间。"""
    out: list[tuple[bool, int, int]] = []
    i = 0
    while i < len(flags):
        j = i + 1
        while j < len(flags) and flags[j] == flags[i]:
            j += 1
        out.append((flags[i], i, j))
        i = j
    return out


def _seg(chunk: list[dict], fast: bool, smooth: list[float] | None = None,
         a: int = 0, b: int = 0) -> dict | None:
    """把一段连续采样点折成一行展示数据。距离用速度梯形积分，不依赖累积距离字段。"""
    if len(chunk) < 2:
        return None
    dur = (chunk[-1].get("t") or 0) - (chunk[0].get("t") or 0)
    if dur <= 0:
        return None
    dist = 0.0
    for i in range(1, len(chunk)):
        dt = (chunk[i].get("t") or 0) - (chunk[i - 1].get("t") or 0)
        if dt <= 0:
            continue
        s0 = chunk[i - 1].get("spd") or 0.0
        s1 = chunk[i].get("spd") or 0.0
        dist += (s0 + s1) / 2 * dt
    spd = dist / dur
    if smooth and b > a:
        spd = sum(smooth[a:b]) / (b - a) or spd
    hrs = [p.get("hr") for p in chunk if p.get("hr")]
    cads = [p.get("cad") for p in chunk if p.get("cad")]
    return {
        "fast": fast,
        "dist_m": round(dist, 1),
        "dur_s": round(dur, 1),
        "spd": spd,
        "pace_s": round(1000 / spd) if spd > 0.5 else None,
        "hr_avg": round(sum(hrs) / len(hrs)) if hrs else None,
        "cad": round(sum(cads) / len(cads), 1) if cads else None,
    }


def _agg(segs: list[dict], fast: bool) -> dict:
    picked = [s for s in segs if s["fast"] is fast]
    dist = sum(s["dist_m"] for s in picked)
    dur = sum(s["dur_s"] for s in picked)
    avg_spd = dist / dur if dur > 0 else 0.0
    pace = round(dur / (dist / 1000)) if dist > 50 else None
    return {
        "sets": len(picked),
        "dist_m": round(dist, 1),
        "dur_s": round(dur, 1),
        "pace_s": pace,
        "spd": round(avg_spd, 2),
        # 组间是原地站立/走动的恢复时，配速会变成几十分钟每公里，
        # 这个数字没有意义；展示层应改用「平均恢复时长」。
        "rest": bool(avg_spd < 1.2 or (pace or 0) > 600),
        "recover_s": round(dur / len(picked)) if picked else None,
        "hr_avg": _wmean([s["hr_avg"] or 0 for s in picked],
                         [s["dur_s"] for s in picked]),
        "cad": _wmean([s["cad"] or 0 for s in picked],
                      [s["dur_s"] for s in picked]),
        "segs": picked[:MAX_GROUPS],
        "omitted": max(0, len(picked) - MAX_GROUPS),
    }


def _finish(segs: list[dict], source: str) -> dict | None:
    """交替性 / 速度差 / 组数三重门禁，通过才认定是间歇。"""
    fasts = [s for s in segs if s["fast"]]
    slows = [s for s in segs if not s["fast"]]
    if len(fasts) < MIN_SETS or not slows:
        return None
    switches = sum(1 for i in range(1, len(segs))
                   if segs[i]["fast"] != segs[i - 1]["fast"])
    if switches < MIN_SWITCHES:
        return None
    vf = _wmean([s["spd"] for s in fasts], [s["dur_s"] for s in fasts]) or 0
    vs = _wmean([s["spd"] for s in slows], [s["dur_s"] for s in slows]) or 0
    if vs <= 0 or vf / vs < MIN_RATIO:
        return None
    return {
        "source": source,
        "sets": len(fasts),
        "ratio": round(vf / vs, 2),
        "fast": _agg(segs, True),
        "slow": _agg(segs, False),
    }


def from_series(points: list[dict] | None) -> dict | None:
    """逐点速度识别。点已由 `activity._thin()` 抽稀，量级几百个，O(n log n) 足够。

    **不能丢掉低速/静止点**：间歇的组间恢复常常是原地站立（spd≈0），
    过滤掉它们会让时间轴出现几十秒的空洞，`_seg()` 的梯形积分把这段按
    两端速度算成几百米，于是快组距离虚高、慢组凭空多出「1 km 慢跑」。
    真实样本里 600 个点有 242 个是静止点，正是这个坑。
    """
    pts = [p for p in (points or []) if isinstance(p.get("t"), (int, float))]
    if len(pts) < 8:
        return None
    pts.sort(key=lambda p: p["t"])
    # 5 点滑动平均，压掉 GPS 毛刺造成的假切换
    smooth: list[float] = []
    for i in range(len(pts)):
        a, b = max(0, i - 2), min(len(pts), i + 3)
        w = [pts[j].get("spd") or 0.0 for j in range(a, b)]
        smooth.append(sum(w) / len(w))
    thr, _, _ = _split(smooth)
    flags = [v >= thr for v in smooth]
    # 过短的快段不是一组间歇，并回慢段
    for is_fast, a, b in _runs(flags):
        if is_fast:
            dur = (pts[b - 1]["t"] - pts[a]["t"])
            dist = sum(smooth[a:b]) * dur / max(1, b - a)
            if dur < MIN_SEG_S or dist < MIN_SEG_M:
                for k in range(a, b):
                    flags[k] = False
    segs: list[dict] = []
    for is_fast, a, b in _runs(flags):
        s = _seg(pts[a:b], is_fast, smooth, a, b)
        if s and s["dur_s"] >= 5:
            segs.append(s)
    return _finish(segs, "series")


def from_laps(laps: list[dict] | None) -> dict | None:
    """分圈兜底：用「距离 / 时长」算速度，不依赖 `pace_s`。

    佳明对极短的休息圈（原地站立 90 秒只挪 30~60 m）经常不给 `pace_s`，
    按配速字段过滤会把所有慢圈丢掉、只剩一串快圈，自然认不出交替。
    """
    real = [l for l in (laps or [])
            if (l.get("dist_m") or 0) >= 20 and (l.get("dur_s") or 0) > 5]
    if len(real) < 4:
        return None
    spds = [l["dist_m"] / l["dur_s"] for l in real]
    thr, _, _ = _split(spds)
    kinds = [v >= thr for v in spds]           # True = 快
    switches = sum(1 for i in range(1, len(kinds)) if kinds[i] != kinds[i - 1])
    if switches < MIN_SWITCHES or sum(kinds) < MIN_SETS:
        return None
    # 相邻同档圈合成一组（热身/放松通常连续好几圈）
    segs: list[dict] = []
    for is_fast, a, b in _runs(kinds):
        chunk = real[a:b]
        dist = sum(l["dist_m"] for l in chunk)
        dur = sum(l.get("dur_s") or 0 for l in chunk)
        if dur <= 0:
            continue
        spd = dist / dur
        hrs = [l["hr_avg"] for l in chunk if l.get("hr_avg")]
        cads = [l["cad"] for l in chunk if l.get("cad")]
        segs.append({
            "fast": is_fast, "dist_m": round(dist, 1), "dur_s": round(dur, 1),
            "spd": spd, "pace_s": round(dur / (dist / 1000)) if dist > 50 else None,
            "hr_avg": round(sum(hrs) / len(hrs)) if hrs else None,
            "cad": round(sum(cads) / len(cads), 1) if cads else None,
        })
    return _finish(segs, "laps")


def detect(detail: dict | None) -> dict | None:
    """活动明细 → 间歇结构或 None。逐点优先，识别不出再用分圈。"""
    d = detail or {}
    points = ((d.get("series") or {}).get("points")) or []
    return from_series(points) or from_laps(d.get("laps") or [])


def check() -> None:
    """离线自检：典型间歇要认出，匀速 / 单调加速不能误报。"""
    assert abs(_split([1, 1, 1, 1, 4, 4, 4, 4])[0] - 2.5) < 1e-9
    # 6 组：快 400 m @ 4.2 m/s + 慢 200 m @ 2.6 m/s，采样间隔 5 s
    pts: list[dict] = []
    t, dist = 0.0, 0.0
    for _ in range(6):
        for spd, secs in ((4.2, 95.0), (2.6, 77.0)):
            n = int(secs / 5)
            for _ in range(n):
                pts.append({"t": t, "spd": spd, "hr": 165 if spd > 3 else 140,
                            "cad": 182 if spd > 3 else 168, "dist": dist})
                t += 5.0
                dist += spd * 5.0
    iv = from_series(pts)
    assert iv and iv["sets"] >= 4, iv
    assert iv["fast"]["dist_m"] > iv["slow"]["dist_m"] * 0.8, iv
    assert iv["fast"]["pace_s"] < iv["slow"]["pace_s"], iv
    assert iv["ratio"] >= MIN_RATIO
    assert all("segs" in iv[k] for k in ("fast", "slow"))

    # 匀速跑：围绕 3.0 m/s 的小抖动，不该判成间歇
    even = [{"t": i * 5.0, "spd": 3.0 + (0.06 if i % 3 == 0 else -0.04),
             "hr": 145, "cad": 176, "dist": i * 15.0} for i in range(120)]
    assert from_series(even) is None

    # 单调加速（越跑越快，没有快慢交替）
    ramp = [{"t": i * 5.0, "spd": 2.4 + i * 0.02, "hr": 140, "cad": 170,
             "dist": i * 15.0} for i in range(120)]
    assert from_series(ramp) is None

    # 分圈兜底：每公里一圈，快慢交替
    laps = []
    for i in range(8):
        fast = i % 2 == 1
        pace = 250 if fast else 400
        laps.append({"idx": i + 1, "dist_m": 1000.0, "dur_s": float(pace),
                     "pace_s": pace, "hr_avg": 168 if fast else 142,
                     "cad": 182 if fast else 168})
    iv2 = from_laps(laps)
    assert iv2 and iv2["sets"] >= 3 and iv2["source"] == "laps", iv2
    assert iv2["fast"]["pace_s"] < iv2["slow"]["pace_s"]
    assert from_laps([]) is None
    assert detect({}) is None

    # 真实样本形态（2026-10-21 21:06，15×400 m + 90 s 原地休息）：
    # 约四成采样点 spd=0，绝不能因过滤低速点打断时间轴、把距离算虚高。
    rest_pts: list[dict] = []
    t = 0.0
    for _ in range(15):
        for spd, secs in ((4.6, 86.0), (0.0, 90.0)):
            for _ in range(int(secs / 4)):
                rest_pts.append({"t": t, "spd": spd,
                                 "hr": 165 if spd else 150,
                                 "cad": 200 if spd else 0, "dist": 0.0})
                t += 4.0
    riv = from_series(rest_pts)
    assert riv and riv["sets"] == 15, riv
    assert riv["slow"]["rest"] and riv["slow"]["dist_m"] < 500, riv["slow"]
    # 两组距离相加不得超过全程（旧实现会算出 8 km > 实际 6.6 km）
    assert riv["fast"]["dist_m"] + riv["slow"]["dist_m"] < 6500, riv
    assert riv["slow"]["recover_s"] and riv["slow"]["recover_s"] > 60

    # 分圈兜底：极短休息圈常常没有 pace_s，也要认出交替
    rest_laps = []
    for i in range(30):
        fast = i % 2 == 0
        rest_laps.append({
            "idx": i + 1, "dist_m": 400.0 if fast else 40.0,
            "dur_s": 86.0 if fast else 90.0,
            "pace_s": 215 if fast else None,
            "hr_avg": 165 if fast else 190, "cad": 200 if fast else 0,
        })
    liv = from_laps(rest_laps)
    assert liv and liv["sets"] == 15 and liv["slow"]["rest"], liv
