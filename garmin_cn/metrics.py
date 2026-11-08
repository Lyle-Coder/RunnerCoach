"""派生指标：恢复度、睡眠债、训练负荷（CTL/ATL/TSB/ACWR）、趋势与风险标记。

所有公式都写在函数里，便于你按自己的训练理念调整。
"""
from __future__ import annotations

import datetime as dt
import math
from statistics import median
from typing import Any

SLEEP_NEED_MIN = 480  # 8 小时
MAX_HR_DEFAULT = 190


def num(x, default=None):
    try:
        if x is None or x == "":
            return default
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def daterange(end: str, days: int) -> list[str]:
    end_d = dt.date.fromisoformat(end)
    return [(end_d - dt.timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]


def _series(rows: list[dict], key: str) -> list[tuple[str, float]]:
    out = []
    for r in rows:
        v = num(r.get(key))
        if v is not None:
            out.append((r["date"], v))
    return out


def baseline(rows: list[dict], key: str, days: int = 28) -> float | None:
    vals = [num(r.get(key)) for r in rows[-days:]]
    vals = [v for v in vals if v is not None]
    if len(vals) < 3:
        return None
    return median(vals)


# ---------------- 恢复度 ----------------
def readiness(today: dict, history: list[dict]) -> dict:
    """0-100 恢复度。权重：睡眠 35% / HRV 30% / 静息心率 20% / 身体电量 15%。

    每个分量都会和 28 天基线比较，缺席的分量按剩余权重归一化。
    """
    parts = []

    sleep_min = num(today.get("sleep_total_min"))
    sleep_score = num(today.get("sleep_score"))
    if sleep_score is not None:
        parts.append(("睡眠", 0.35, clamp(sleep_score), f"睡眠评分 {sleep_score:.0f}"))
    elif sleep_min:
        s = clamp(sleep_min / SLEEP_NEED_MIN * 100)
        parts.append(("睡眠", 0.35, s, f"睡眠 {sleep_min/60:.1f} 小时"))

    # 基线及以上记满分，低于基线按固定量程扣分：
    #   HRV    : 比基线低 30% 时归零
    #   静息心率: 比基线高 6 bpm 时归零（bpm 差比百分比更符合日常波动幅度）
    hrv = num(today.get("hrv_night"))
    hrv_base = baseline(history, "hrv_night")
    if hrv and hrv_base:
        dev = hrv / hrv_base - 1          # 相对基线的偏差，负数=低于基线
        s = clamp(100 + min(dev, 0) / 0.30 * 100)
        parts.append(("HRV", 0.30, s, f"{hrv:.0f} ms / 基线 {hrv_base:.0f}"))

    rhr = num(today.get("resting_hr"))
    rhr_base = baseline(history, "resting_hr")
    if rhr and rhr_base:
        delta = rhr - rhr_base            # 高出基线多少 bpm，正数=偏高
        s = clamp(100 - max(delta, 0) / 6 * 100)
        parts.append(("静息心率", 0.20, s, f"{rhr:.0f} bpm / 基线 {rhr_base:.0f}"))

    # 电量优先用 live.py 刚拉的当前值：恢复度是「现在能不能练」，
    # 深夜看板还拿早上的最高值打分，就是把过时的状态当现状。
    bb_now = num(today.get("bb_now"))
    bb_high = num(today.get("bb_high"))
    bb = bb_now if bb_now is not None else bb_high
    if bb:
        detail = (f"当前 {bb_now:.0f}%（日内最高 {bb_high:.0f}）" if bb_now is not None
                  else f"最高 {bb:.0f}")
        parts.append(("身体电量", 0.15, clamp(bb), detail))

    if not parts:
        return {"score": None, "level": "数据不足", "color": "#8A8F87", "parts": []}

    wsum = sum(p[1] for p in parts)
    score = sum(p[1] * p[2] for p in parts) / wsum
    # 基线日 ≈ 满分，所以档位整体上移，否则「几乎正常的一天」也会被叫优秀
    if score >= 90:
        level, color = "状态优秀", "#1D9E75"
    elif score >= 75:
        level, color = "良好", "#4E9F3D"
    elif score >= 60:
        level, color = "一般", "#C98A16"
    else:
        level, color = "需要恢复", "#C2453F"

    return {
        "score": round(score),
        "level": level,
        "color": color,
        "parts": [{"name": n, "score": round(s), "detail": d} for n, w, s, d in parts],
    }


# ---------------- 睡眠 ----------------
def sleep_debt(rows: list[dict], need_min: int = SLEEP_NEED_MIN, window: int = 7) -> dict:
    """近 window 天睡眠缺口，**小睡抵扣 + 佳明动态需求**。

    两层修正，都来自佳明自己的数据：
    1. sleep_total_min 只含主睡眠窗口，日间小睡单独在 nap_min 里（实测
       napTimeSeconds 不算进 sleepTimeSeconds），相加才是真实睡眠总量。
    2. 需求基线不用写死的 8 小时：dailySleepDTO.sleepNeed.actual 是佳明按
       训练负荷/睡眠史/HRV/小睡逐日调整的个性化需求（420~540 分钟都出现），
       有就用它，没有回退 need_min。

    已知的小幅重复计算：小睡既作为供给（+nap_min），佳明也会把下一晚需求
    调低（napAdjustment=DECREASING），两端各占一点便宜，量级约 20 分/晚，
    对 7 天合计影响有限，先不做对冲。
    """
    deficit = 0.0
    deficit_raw = 0.0
    short_nights = 0
    counted = 0
    nap_total = 0
    nap_manual = 0
    need_sum = 0
    need_n = 0
    from . import manual as manual_mod

    for r in rows[-window:]:
        m = num(r.get("sleep_total_min"))
        if m is None:
            continue
        counted += 1
        # 佳明有就用佳明的，没有才看手动补录（两者不叠加，避免同一段小睡算两遍）
        nap, src = manual_mod.merge(r.get("nap_min"), r.get("date") or "")
        if src == "manual":
            nap_manual += nap
        nap_total += nap
        need = num(r.get("sleep_need_min")) or need_min
        need_sum += need
        need_n += 1
        eff = m + nap
        if eff < need:
            deficit += need - eff
            short_nights += 1
        if m < need:                           # 不计小睡的口径，供对比
            deficit_raw += need - m
    return {
        "deficit_min": round(deficit),
        "deficit_h": round(deficit / 60, 1),
        "deficit_raw_h": round(deficit_raw / 60, 1),   # 不含小睡
        "nap_min": nap_total,
        "nap_manual_min": nap_manual,
        "need_avg_min": round(need_sum / need_n) if need_n else need_min,
        "short_nights": short_nights,
        "nights_counted": counted,
    }


# ---------------- 训练负荷 ----------------
def daily_loads(rows: list[dict], activities: list[dict], max_hr: int = MAX_HR_DEFAULT) -> dict[str, float]:
    loads: dict[str, float] = {r["date"]: 0.0 for r in rows}
    for a in activities:
        d = a.get("date")
        if not d:
            continue
        load = num(a.get("load"))
        if load is None:
            minutes = num(a.get("duration_s"), 0) / 60
            avg_hr = num(a.get("avg_hr"))
            rhr = num(next((r.get("resting_hr") for r in rows if r["date"] == d), None), 55)
            if avg_hr and minutes:
                hrr = clamp((avg_hr - rhr) / max(1, (max_hr - rhr)), 0, 1)
                load = minutes * hrr * 0.64 * math.exp(1.92 * hrr)
            else:
                load = minutes * 4
        loads[d] = loads.get(d, 0.0) + (load or 0)
    return loads


def ewma(series: list[float], span: int) -> list[float]:
    alpha = 2 / (span + 1)
    out = []
    prev = None
    for v in series:
        prev = v if prev is None else alpha * v + (1 - alpha) * prev
        out.append(prev)
    return out


def training_status(rows: list[dict], activities: list[dict], max_hr: int = MAX_HR_DEFAULT) -> dict:
    dates = [r["date"] for r in rows]
    loads = daily_loads(rows, activities, max_hr)
    seq = [loads.get(d, 0.0) for d in dates]
    ctl = ewma(seq, 42)
    atl = ewma(seq, 7)

    acute = sum(seq[-7:]) if len(seq) >= 7 else sum(seq)
    chronic28 = sum(seq[-28:]) if len(seq) >= 28 else sum(seq)
    acwr = (acute / (chronic28 / 4)) if chronic28 > 0 else None

    tsb = (ctl[-1] - atl[-1]) if ctl else 0

    # 最近 7 天训练总分钟：AI 教练的上下文要判断「这周练了多少」，
    # 以前它读 `training_status()["week_min"]`，这里根本没这个键 → 永远是 None。
    last7 = set(dates[-7:])
    week_min = round(sum((num(a.get("duration_s"), 0) or 0)
                         for a in activities if a.get("date") in last7) / 60, 1)

    if acwr is None:
        acwr_state = "样本不足"
    elif acwr > 1.5:
        acwr_state = "负荷激增，受伤风险高"
    elif acwr > 1.3:
        acwr_state = "负荷偏快"
    elif acwr < 0.8:
        acwr_state = "负荷偏低（减量期）"
    else:
        acwr_state = "负荷合理"

    return {
        "loads": {d: round(v, 1) for d, v in loads.items()},
        "ctl": round(ctl[-1], 1) if ctl else 0,
        "atl": round(atl[-1], 1) if atl else 0,
        "tsb": round(tsb, 1),
        "acwr": round(acwr, 2) if acwr else None,
        "acwr_state": acwr_state,
        "acute_7d": round(acute, 1),
        "chronic_28d": round(chronic28, 1),
        "week_min": week_min,
        "ctl_series": [round(x, 1) for x in ctl],
        "atl_series": [round(x, 1) for x in atl],
    }


# ---------------- 对比 ----------------
def week_compare(rows: list[dict], key: str) -> dict:
    vals = [num(r.get(key)) for r in rows]
    vals = [v for v in vals if v is not None]
    if len(vals) < 4:
        return {"now": None, "prev": None, "delta": None}
    now = sum(vals[-7:]) / len(vals[-7:])
    prev_slice = vals[-14:-7]
    prev = sum(prev_slice) / len(prev_slice) if prev_slice else None
    return {
        "now": round(now, 1),
        "prev": round(prev, 1) if prev is not None else None,
        "delta": round(now - prev, 1) if prev is not None else None,
    }


def trend(today: dict, rows: list[dict], key: str, window: int = 7) -> dict:
    v = num(today.get(key))
    prev = [num(r.get(key)) for r in rows[-window - 1:-1]]
    prev = [p for p in prev if p is not None]
    base = sum(prev) / len(prev) if prev else None
    return {
        "value": v,
        "base": round(base, 1) if base is not None else None,
        "delta": round(v - base, 1) if (v is not None and base is not None) else None,
    }


def flags(res: dict) -> list[dict]:
    out = []
    r = res["readiness"]
    if r["score"] is not None and r["score"] < 50:
        out.append({"level": "alert", "text": f"恢复度 {r['score']}，今天不建议安排质量课"})
    debt = res["sleep"]["debt_h"]
    if debt >= 3:
        out.append({"level": "warn", "text": f"近 7 天睡眠缺口 {debt} 小时，优先补觉"})
    bd = res.get("body") or {}
    if bd.get("level") == "warn":
        out.append({"level": "alert", "text": bd.get("verdict") or ""})
    elif bd.get("level") == "watch" and bd.get("delta_pct_w") is not None:
        out.append({"level": "warn", "text": bd.get("verdict") or ""})
    acwr = res["load"]["acwr"]
    if acwr and acwr > 1.4:
        out.append({"level": "alert", "text": f"ACWR {acwr}，周负荷涨幅过快"})
    elif acwr and acwr < 0.7:
        out.append({"level": "warn", "text": f"ACWR {acwr}，训练刺激不足"})
    tsb = res["load"]["tsb"]
    if tsb < -20:
        out.append({"level": "warn", "text": f"疲劳积累 TSB {tsb}，建议减量"})
    elif tsb > 15:
        out.append({"level": "ok", "text": f"状态新鲜 TSB {tsb}，可安排强度课"})
    if not out:
        out.append({"level": "ok", "text": "各项指标平稳，按计划训练即可"})
    return out


def derive(store, date: str, window: int = 30) -> dict:
    """把一次看板/教练需要的全部数据算好，供 report / coach / MCP 复用。"""
    from . import manual

    history = store.recent_days(window, end=date)
    today = store.day(date) or (history[-1] if history else {"date": date})
    acts = store.activities(daterange_end_back(date, 35)[0], date)

    # 手动睡眠矫正（手表整晚没戴这类）：给出即覆盖，历史行也原地改，
    # 这样 readiness / 睡眠债 / 7 天柱状图用的都是同一份矫正后的数。
    fix = manual.sleep_fix_of(date)
    if fix:
        manual.apply_sleep_fix(today, fix, date)
        for r in history:
            if r.get("date") == date:
                manual.apply_sleep_fix(r, fix, date)

    res = {
        "date": date,
        "today": today,
        "history": history,
        "readiness": readiness(today, history),
        "sleep": {
            "total_min": num(today.get("sleep_total_min")),
            "total_h": round((num(today.get("sleep_total_min")) or 0) / 60, 2),
            "nap_min": num(today.get("nap_min")),
            "naps": num(today.get("naps")),
            "score": num(today.get("sleep_score")),
            "deep_min": num(today.get("deep_min")),
            "light_min": num(today.get("light_min")),
            "rem_min": num(today.get("rem_min")),
            "awake_min": num(today.get("awake_min")),
            "start": today.get("sleep_start"),
            "end": today.get("sleep_end"),
            "debt_h": 0,
        },
        "load": training_status(history, acts),
        "activities": acts,
        "trends": {
            "hrv": trend(today, history, "hrv_night"),
            "rhr": trend(today, history, "resting_hr"),
            "steps": trend(today, history, "steps"),
            "sleep": trend(today, history, "sleep_total_min"),
            "bb": trend(today, history, "bb_high"),
            "stress": trend(today, history, "avg_stress"),
            "vo2max": trend(today, history, "vo2max"),
        },
        "week": {
            "sleep_h": week_compare(history, "sleep_total_min"),
            "hrv": week_compare(history, "hrv_night"),
            "rhr": week_compare(history, "resting_hr"),
        },
        "series": {
            "hrv": _series(history, "hrv_night"),
            "rhr": _series(history, "resting_hr"),
            "sleep_h": [(d, round(v / 60, 2)) for d, v in _series(history, "sleep_total_min")],
            "nap_h": [(d, round(v / 60, 2)) for d, v in _series(history, "nap_min")],
            "steps": _series(history, "steps"),
            "bb": _series(history, "bb_high"),
            "vo2": _series(history, "vo2max"),
            "hrv_base": _series(history, "hrv_weekly"),
            "dist_km": [(d, round((v or 0) / 1000, 2)) for d, v in _series(history, "distance_m")],
        },
    }

    debt = sleep_debt(history, SLEEP_NEED_MIN)
    res["sleep"]["debt_h"] = debt["deficit_h"]
    res["sleep"]["debt"] = debt

    # 体重 / 体成分：与 CTL 的变化做交叉判断（掉重 + 负荷涨 = 能量不足信号）
    from . import body as bodymod

    tb = bodymod.stats(store, date)
    ctls = res["load"].get("ctl_series") or []
    ctl_delta = (ctls[-1] - ctls[-29]) if len(ctls) >= 29 else None
    v, lv = bodymod.verdict(tb, ctl_delta)
    tb["verdict"], tb["level"] = v, lv
    tb["ctl_delta"] = ctl_delta
    res["body"] = tb

    res["flags"] = flags(res)

    # 营养目标：由体重 / 体成分 / 训练量推导，放在这里算一次，
    # report、card、coach、MCP 都读同一份，避免各处口径打架。
    from . import nutrition as nutr

    res["nutrition"] = nutr.targets(store, date, res)
    return res


def daterange_end_back(date: str, days: int) -> tuple[str, str]:
    a = daterange(date, days)
    return a[0], date
