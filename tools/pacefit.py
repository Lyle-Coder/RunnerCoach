# -*- coding: utf-8 -*-
"""用历史数据反推五区配速（心率 ↔ 配速回归）。

数据源：本地 data/garmin.db
- activity_detail.series.points：逐点 (hr, spd, ele, gap)
- activity_detail.laps：每公里分段 (pace_s, hr_avg, ele_gain) —— 主用，噪声小

做法：只保留**平路样本**（每公里爬升 ≤8 m 或逐点坡度 |grade|<1.5%），
排除心率漂移/暂停/起跑等污染点，按心率分桶取配速中位数，再用最近 6 个月
与历史最佳分段（fastestSplit_*）交叉校验，最后给出 E/M/T/I/R 五区配速。
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import statistics as st
from collections import defaultdict

DB = "data/garmin.db"
MAX_HR = 207

# 期望分区（心率上限）：E/M/T/I/R
ZONES = [("E", None, 145), ("M", 146, 155), ("T", 156, 165),
         ("I", 166, 178), ("R", 179, 207)]


def pace_str(s: float) -> str:
    s = int(round(s))
    return "%d:%02d" % divmod(s, 60)


def load_laps():
    c = sqlite3.connect(DB)
    rows = c.execute("select activity_id, payload from activity_detail").fetchall()
    meta = {r[0]: r[1:] for r in c.execute("select activity_id, date, distance_m, avg_hr, "
                                           "ascent, raw from activities")}
    out = []
    for aid, payload in rows:
        try:
            p = json.loads(payload)
        except Exception:
            continue
        d = meta.get(aid)
        date = d[0] if d else "?"
        for lp in p.get("laps") or []:
            if not lp:
                continue
            dist = lp.get("dist_m") or 0
            pace = lp.get("pace_s")
            hr = lp.get("hr_avg")
            gain = lp.get("ele_gain") or 0
            if not pace or not hr:
                continue
            if lp.get("is_partial") and dist < 500:
                continue
            if dist < 800:                      # 只要整公里
                continue
            if gain > 8:                        # 平路：每公里爬升 ≤8 m
                continue
            if not (200 <= pace <= 900):        # 3:20–15:00/km
                continue
            if not (100 <= hr <= 200):
                continue
            out.append({"date": date, "hr": hr, "pace": pace, "gain": gain})
    return out, meta


def bucket(samples, lo, hi, width=5):
    b = defaultdict(list)
    for s in samples:
        k = int(s["hr"] // width) * width
        b[k].append(s["pace"])
    rows = []
    for k in sorted(b):
        v = sorted(b[k])
        if len(v) < 12 or not (lo <= k <= hi):
            continue
        rows.append((k, k + width - 1, len(v), st.median(v), v[len(v) // 4],
                     v[len(v) * 3 // 4]))
    return rows


def fit_linear(samples):
    """心率 → 配速 的最小二乘拟合（sec/km 对 bpm）。"""
    xs = [s["hr"] for s in samples]
    ys = [s["pace"] for s in samples]
    n = len(xs)
    if n < 30:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    slope = num / den
    return slope, my - slope * mx, n


def vdot(dist_m: float, sec: float) -> float:
    """Jack Daniels VDOT：由比赛距离(m)与用时(s)反解。"""
    t = sec / 60.0                                   # 分钟
    v = dist_m / t                                   # m/min
    vo2 = -4.60 + 0.182258 * v + 0.000104 * v * v    # ml/kg/min
    pct = (0.8 + 0.1894393 * pow(2.718281828, -0.012778 * t)
           + 0.2989558 * pow(2.718281828, -0.1932605 * t))
    return vo2 / pct


def main():
    samples, meta = load_laps()
    print("平路整公里分段样本：%d 段" % len(samples))
    if not samples:
        return
    dates = sorted(s["date"] for s in samples)
    print("时间跨度：%s → %s" % (dates[0], dates[-1]))

    recent = [s for s in samples if s["date"] >= "2026-05-01"]
    print("其中近 6 个月：%d 段\n" % len(recent))

    print("心率分桶 → 配速中位数（平路）")
    print("  HR 区间   样本   中位配速   P25     P75")
    for lo, hi, n, med, p25, p75 in bucket(samples, 110, 200):
        print("  %3d-%3d  %5d   %s   %s   %s"
              % (lo, hi, n, pace_str(med), pace_str(p25), pace_str(p75)))

    print("\n五区配速（按区间内样本的中位数 / 快端 P25）")
    res = {}
    for name, lo, hi in ZONES:
        sub = [s["pace"] for s in samples if (lo is None or s["hr"] >= lo) and s["hr"] <= hi]
        sub.sort()
        if not sub:
            continue
        med = st.median(sub)
        fast = sub[len(sub) // 5]        # P20（状态好时）
        slow = sub[len(sub) * 4 // 5]    # P80
        res[name] = (med, fast, slow, len(sub))
        print("  %s (%s–%s bpm)  n=%-5d 中位 %s   快端 %s   慢端 %s"
              % (name, lo or "—", hi, len(sub), pace_str(med), pace_str(fast),
                 pace_str(slow)))

    print("\n仅近 6 个月样本（更贴近当前状态）")
    print("  HR 区间   样本   中位配速   P25     P75")
    for lo, hi, n, med, p25, p75 in bucket(recent, 120, 200):
        print("  %3d-%3d  %5d   %s   %s   %s"
              % (lo, hi, n, pace_str(med), pace_str(p25), pace_str(p75)))
    print("\n  [近半年] 五区")
    for name, lo, hi in ZONES:
        sub = sorted(s["pace"] for s in recent
                     if (lo is None or s["hr"] >= lo) and s["hr"] <= hi)
        if len(sub) >= 8:
            print("   %s n=%-4d 中位 %s   快端(P20) %s   慢端(P80) %s"
                  % (name, len(sub), pace_str(st.median(sub)),
                     pace_str(sub[len(sub) // 5]), pace_str(sub[len(sub) * 4 // 5])))

    fit = fit_linear(samples)
    if fit:
        slope, icept, n = fit
        print("\n线性回归：配速(秒/km) = %.2f × 心率 + %.0f   （n=%d）" % (slope, icept, n))
        for name, lo, hi in ZONES:
            hr = (lo + hi) // 2 if lo else 138
            print("   心率 %d → 拟合配速 %s" % (hr, pace_str(slope * hr + icept)))
        print("   心率 145 → %s ｜ 160 → %s ｜ 175 → %s ｜ 185 → %s"
              % (pace_str(slope * 145 + icept), pace_str(slope * 160 + icept),
                 pace_str(slope * 175 + icept), pace_str(slope * 185 + icept)))

    # 比赛/最佳分段 → VDOT
    print("\n历史最佳分段（来自活动原始字段）")
    c = sqlite3.connect(DB)
    best = {}
    for aid, raw in c.execute("select activity_id, raw from activities"):
        try:
            a = json.loads(raw)
        except Exception:
            continue
        for k, lab in (("fastestSplit_5000", "5 km"), ("fastestSplit_10000", "10 km"),
                       ("fastestSplit_21098", "半马"), ("fastestSplit_42195", "全马")):
            v = a.get(k)
            if not v:
                continue
            dist = {"5 km": 5000, "10 km": 10000, "半马": 21097.5, "全马": 42195}[lab]
            if lab not in best or v < best[lab][1]:
                best[lab] = (aid, v, a.get("startTimeLocal"), v / (dist / 1000))
    for lab in ("5 km", "10 km", "半马", "全马"):
        if lab in best:
            aid, sec, when, pk = best[lab]
            print("  %-5s %s  %s   (活动 %s)   VDOT≈%.1f"
                  % (lab, pace_str(sec / ({"5 km": 5, "10 km": 10, "半马": 21.0975,
                                           "全马": 42.195}[lab])), str(when)[:10], aid,
                     vdot({"5 km": 5000, "10 km": 10000, "半马": 21097.5,
                           "全马": 42195}[lab], sec)))

    print("\n手表估算 VO2max（各活动 vO2MaxValue）")
    vals = []
    for aid, raw in c.execute("select activity_id, raw from activities"):
        try:
            a = json.loads(raw)
        except Exception:
            continue
        v = a.get("vO2MaxValue")
        if v:
            vals.append((str(a.get("startTimeLocal"))[:10], v))
    vals.sort()
    for d, v in vals[-8:]:
        print("   %s  %.0f" % (d, v))


if __name__ == "__main__":
    main()
