# -*- coding: utf-8 -*-
"""赛后复盘：把「计划余量」和「实际分段」对到一起。

计划侧的数据在 `event.plan_and_margin()`（每段的计划到达与关门余量）；
实际侧只有两处能拿到：

1. `activity_detail.payload["route"]`：`d` 累计米、`t` 累计秒，最准；
2. `payload["laps"]`：1 km 自动圈（`dist_m` + `dur_s`），累加后插值。

两条都没有就只能提示去补明细——**不拿总时长平摊去猜分段**，
那会得出一张每段都「刚好等于平均配速」的假表，看不出任何东西。

口径说明：
- 实际到达时刻 = 活动开始时刻 + 到该 CP 的累计用时（不是计划起跑 + 用时，
  因为很多人起跑就晚了几分钟，混进来会让每段都偏）；
- 关门的比较仍用计划里的绝对时刻（关门是官方发布的钟点）；
- 没跑到的 CP 记 `actual=None`，不参与统计，并在结尾说明停在哪。
"""
from __future__ import annotations

import datetime as dt

# 一处 CP 的到达时刻允许误差：路书里程与轨迹里程本来就有 0.2–0.5 km 的出入
KM_TOL = 0.35


def _series(detail: dict) -> tuple[list[float], list[float]]:
    """取 (累计米, 累计秒) 两条曲线。优先 route，退化到 laps。"""
    rt = (detail or {}).get("route") or {}
    d, t = rt.get("d") or [], rt.get("t") or []
    if len(d) > 2 and len(d) == len(t):
        return [float(x) for x in d], [float(x) for x in t]

    laps = (detail or {}).get("laps") or []
    if not laps:
        return [], []
    cd, ct = [0.0], [0.0]
    for lap in laps:
        cd.append(cd[-1] + float(lap.get("dist_m") or 0))
        ct.append(ct[-1] + float(lap.get("dur_s") or 0))
    return cd, ct


def at_km(cum_d: list[float], cum_t: list[float], km: float) -> float | None:
    """累计距离达到 km 时的累计秒数；超出活动总距离返回 None（没跑到）。

    线性插值：1 km 自动圈在 30–40 min/km 的陡坡上会粗一点，
    但 CP 判读只需要分钟级，够用。
    """
    if not cum_d or km <= 0:
        return 0.0
    target = km * 1000.0
    if target > cum_d[-1] + 50:
        return None
    lo, hi = 0, len(cum_d) - 1
    while lo < hi:                       # 二分，轨迹点数可能上万
        mid = (lo + hi) // 2
        if cum_d[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    i = lo
    if i == 0:
        return cum_t[0]
    d0, d1 = cum_d[i - 1], cum_d[i]
    t0, t1 = cum_t[i - 1], cum_t[i]
    if d1 - d0 < 1e-6:
        return t1
    return t0 + (t1 - t0) * (target - d0) / (d1 - d0)


def _pace(s: float, km: float) -> str:
    if not km or not s:
        return "—"
    p = s / km
    return "%d:%02d" % (p // 60, p % 60)


def _hm(s: float) -> str:
    s = int(abs(s))
    h, m = divmod(s // 60, 60)
    return ("%dh%02dm" % (h, m)) if h else ("%dm" % m)


def _signed(s: float) -> str:
    """耗时差：+12m / -3m。负数先取绝对值再补符号（地板除会把 -83s 算成 -2m）。"""
    sign = "-" if s < 0 else "+"
    return sign + _hm(s)


def find_activity(acts: list[dict], ev: dict) -> dict | None:
    """在赛事日前后一天的活动中挑「最像这场比赛」的一次。

    优先同日；其次比赛日次日（凌晨起跑、跨零点结束的活动日期可能落在次日）。
    距离上要求至少达到赛事距离的 60%，否则多半是赛前热身或出门慢跑。
    """
    try:
        race = dt.date.fromisoformat(ev["date"])
    except (KeyError, ValueError):
        return None
    want = float(ev.get("distance_km") or 0) * 1000
    cands = []
    for a in acts:
        try:
            d = dt.date.fromisoformat(str(a.get("date"))[:10])
        except (ValueError, TypeError):
            continue
        gap = (d - race).days
        if gap not in (0, 1, -1):
            continue
        km = float(a.get("distance_m") or 0)
        if want and km < want * 0.6:
            continue
        cands.append((abs(gap), -km, a))
    if not cands:
        return None
    cands.sort(key=lambda x: (x[0], x[1]))
    return cands[0][2]


def review(ev: dict, rows: list[dict], activity: dict,
           detail: dict | None) -> dict:
    """把计划表与实际分段合成一张复盘表。

    rows 来自 `event.plan_and_margin(slug)`（含计划到达与关门），
    activity 是 activities 表一行，detail 是 activity_detail 的 payload。
    """
    start = activity.get("start_ts") or ""
    try:
        t0 = dt.datetime.fromisoformat(str(start)[:19])
    except ValueError:
        t0 = dt.datetime.combine(dt.date.fromisoformat(ev["date"]),
                                 dt.time(*map(int, (ev.get("start_time") or "06:00").split(":"))))

    cum_d, cum_t = _series(detail or {})
    have_split = bool(cum_t)
    total_s = float(activity.get("duration_s") or (cum_t[-1] if cum_t else 0))
    total_km = float(activity.get("distance_m") or (cum_d[-1] if cum_d else 0)) / 1000.0
    goal_s = int(ev.get("goal_s") or 0)

    # 计划侧也换成「相对起跑的用时」，才能和实际用时直接相减
    try:
        plan_start = dt.datetime.combine(dt.date.fromisoformat(ev["date"]),
                                         dt.time(*map(int, (ev.get("start_time") or "06:00").split(":"))))
    except (KeyError, ValueError):
        plan_start = rows[0]["arrive"] if rows else t0

    out, prev_t, prev_plan_t = [], 0.0, 0.0
    for r in rows:
        km = float(r["km"])
        plan_leg = (r["arrive"] - plan_start).total_seconds() - prev_plan_t
        prev_plan_t = (r["arrive"] - plan_start).total_seconds()
        t_leg = at_km(cum_d, cum_t, km) if have_split else None
        done = t_leg is not None
        # 里程容差内也算到达（活动距离略短于路书是常态）
        if not done and total_km and abs(total_km - km) <= KM_TOL:
            t_leg, done = total_s, True
        actual = (t0 + dt.timedelta(seconds=t_leg)) if done else None
        out.append({
            "idx": r["idx"], "name": r["name"], "km": km,
            "planned": r["arrive"], "cutoff": r["cutoff"],
            "actual": actual, "done": done,
            "delta_s": (t_leg - (r["arrive"] - plan_start).total_seconds()
                        if done else None),
            "leg_s": (t_leg - prev_t) if done else None,
            "plan_leg_s": plan_leg,
            # 段级偏差才是「这段跑快了还是跑慢了」；累计偏差只说明整场进度
            "leg_delta_s": ((t_leg - prev_t) - plan_leg) if done else None,
            "margin_s": ((r["cutoff"] - actual).total_seconds()
                         if (done and r["cutoff"]) else None),
            "pace": _pace(t_leg - prev_t, km - (out[-1]["km"] if out else 0.0)) if done else "—",
        })
        if done:
            prev_t = t_leg

    reached = [x for x in out if x["done"]]
    stats = {
        "total_s": total_s, "total_km": total_km,
        "goal_s": goal_s, "goal_delta_s": (total_s - goal_s) if goal_s else None,
        "avg_pace": _pace(total_s, total_km),
        "half_ratio": None, "reached": len(reached), "cps": len(out),
        "dnf_km": (total_km if len(reached) < len(out) else None),
        "worst": None, "best": None,
    }
    # 前后半程配速比：正分段（后慢）是长距离最典型的崩法，比逐段差值更好读
    if have_split and total_km > 1:
        half = total_km / 2
        th = at_km(cum_d, cum_t, half)
        if th:
            first, second = th, total_s - th
            stats["half_ratio"] = second / first if first else None
            stats["first_half_s"] = first
            stats["second_half_s"] = second
    # 与计划偏离最大的「段」（只看跑到的）。用段级偏差而不是累计偏差：
    # 累计偏差会一路放大，最后一段永远是「最大」，看不出是哪一段出的问题。
    legs = [x for x in reached if x.get("leg_delta_s") is not None]
    if legs:
        stats["worst"] = max(legs, key=lambda x: x["leg_delta_s"])
        stats["best"] = min(legs, key=lambda x: x["leg_delta_s"])

    return {
        "ok": True, "have_split": have_split,
        "activity": {"id": str(activity.get("activity_id") or ""),
                     "name": activity.get("name") or "",
                     "date": str(activity.get("date") or "")[:10]},
        "start": t0, "rows": out, "stats": stats,
    }


def summary_text(rep: dict, ev: dict) -> str:
    """给 CLI / 页面顶部的一段人话结论（3–4 行，不堆数字）。"""
    s = rep["stats"]
    lines = []
    total = _hm(s["total_s"])
    if s["goal_s"]:
        lines.append("%s / 目标 %s（%s）· 平均 %s/km"
                     % (total, _hm(s["goal_s"]), _signed(s["goal_delta_s"]), s["avg_pace"]))
    else:
        lines.append("%s · 平均 %s/km" % (total, s["avg_pace"]))
    r = s.get("half_ratio")
    if r:
        pct = (r - 1) * 100
        if pct > 8:
            lines.append("后半程比前半慢 %.0f%%（%s → %s）：典型的后程掉速，"
                         "下次把前 1/3 再压 15–20 s/km"
                         % (pct, _hm(s["first_half_s"]), _hm(s["second_half_s"])))
        elif pct < -5:
            lines.append("后半程比前半快 %.0f%%：负分段，配速控制得很好" % -pct)
        else:
            lines.append("前后半程基本持平（±%.0f%%），配速分配可用" % abs(pct))
    w = s.get("worst")
    if w and w["leg_delta_s"] > 300:
        lines.append("最慢的一段是到 CP%d %s：这段比计划多用了 %s"
                     % (w["idx"], w["name"], _hm(w["leg_delta_s"])))
    b = s.get("best")
    if b and b["leg_delta_s"] < -300:
        lines.append("最快的一段是到 CP%d %s：这段比计划省了 %s"
                     % (b["idx"], b["name"], _hm(-b["leg_delta_s"])))
    if s.get("dnf_km"):
        lines.append("没跑完：在 %.1f km 停下（最后一个到达的是 CP%d）"
                     % (s["dnf_km"], s["reached"]))
    return "\n".join(lines)


def auto(ev: dict) -> dict:
    """从本地库找这场比赛的活动并复盘；找不到就返回原因（页面直接显示原因）。

    不在这里抓明细：抓明细需要账号凭证，属于 App 侧（appapi）的职责；
    页面只做「库里有什么就用什么」，并明确告诉人缺什么、去哪补。
    """
    from . import event as E

    db = E.HOME / "data" / "garmin.db"
    if not db.exists():
        return {"ok": False, "reason": "本地还没有数据库"}
    try:
        from .store import Store

        store = Store(db)
        race = dt.date.fromisoformat(ev["date"])
        acts = store.activities((race - dt.timedelta(days=1)).isoformat(),
                                (race + dt.timedelta(days=1)).isoformat())
        act = find_activity(acts, ev)
        if not act:
            return {"ok": False, "reason": "比赛日前后没有活动记录：同步一次再回来"}
        detail = store.get_detail(act["activity_id"])
        if not detail:
            return {"ok": False, "activity": {"id": str(act["activity_id"]),
                                             "name": act.get("name") or ""},
                    "reason": "这次活动还没有分段明细：在「历史」里打开一次这次活动即可补上"}
        rep = review(ev, E.plan_and_margin(ev["slug"]), act, detail)
        rep["text"] = summary_text(rep, ev)
        return rep
    except Exception as e:                      # 库坏了也不能让整页打不开
        return {"ok": False, "reason": "读取出错：%s" % e}
