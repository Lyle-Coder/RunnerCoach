# -*- coding: utf-8 -*-
"""计划生成器：没有训练计划的人，也要有一个能撑起周目标的框架。

为什么必须做这件事
------------------
用户组的赛季靠「本周目标」算格数，而周目标来自训练计划。没有计划的人，
远征就没有目标可算，整个玩法是空的。所以计划层必须先保证**每名跑者都有计划**。

但「有」不等于「随便给一个」。这个生成器守三条：

1. **只从历史推**，不凭空定目标。近 4 周跑多少，就在那附近排，最多上浮 10%。
2. **先出草稿，要人确认**。系统不替人决定训练——确认之前一个字都不落盘。
3. **不排间歇**。没有人给强度目标时，自己加间歇课是越界；只出轻松 / 节奏 /
   长距离三种，强度靠配速而不是靠秒表。

输出结构与 `plan.py` 完全一致（`week[]` + 每天 `items[]` + `source`），
所以生成的计划能被看板、教练上下文、课表推送直接吃，不需要任何适配。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import statistics
import time
from pathlib import Path

from . import plan as PL

# 每周跑几天 → (长距离占比, 强度课占比)。天数太少时不排强度课：
# 一周只跑两天的人，把一天练成节奏跑，训练效果还不如两次轻松有氧。
SHARE = {
    1: (1.00, 0.0),
    2: (0.55, 0.0),
    3: (0.40, 0.25),
    4: (0.34, 0.24),
    5: (0.30, 0.22),
    6: (0.28, 0.20),
}

# 排课优先级：周三先占（周中最适合强度），周六次之（长距离），
# 轻松日按「离强度日越远越好」补：周一 → 周五 → 周四 → 周二 → 周日。
# 之前把周二排在第三位，3 天框架就变成「周二轻松 12 km + 周三节奏」连着两天，
# 那是把恢复日当成了训练日。
SLOT_ORDER = [3, 6, 1, 5, 4, 2, 7]

NAME = {"easy": "轻松跑", "tempo": "节奏跑", "long": "长距离"}
DETAIL = {
    "easy": "全程能说话，心率 135–150",
    "tempo": "热身 10 分钟，中段稳住 20 分钟，冷身 5 分钟",
    "long": "慢，全程能说话；每 20 分钟补水一次",
}
PACE_FACTOR = {"easy": 1.08, "tempo": 0.97, "long": 1.10}

DEFAULT_PACE = 360          # 没有任何配速数据时的兜底：6'00"/km
MAX_LONG_KM = 32.0          # 长距离单次上限
MIN_WEEK_KM = 6.0           # 低于这个量就不排结构，只给两天轻松跑
GROWTH_CAP = 1.15           # 周量相对近 4 周均值的上浮上限

DRAFT_NAME = "plan_draft.json"
RULES_NAME = "plan_rules.json"


def _home() -> Path:
    env = os.environ.get("GARMIN_HOME")
    return Path(env).expanduser() if env else Path(__file__).resolve().parent.parent


def _draft_path(path: Path | None = None) -> Path:
    p = Path(path) if path else (_home() / "data" / DRAFT_NAME)
    return p if p.suffix == ".json" else p / DRAFT_NAME


def _rules_path(path: Path | None = None) -> Path:
    p = Path(path) if path else (_home() / "data" / RULES_NAME)
    return p if p.suffix == ".json" else p / RULES_NAME


def _dump(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _load_json(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


# ---------------------------------------------------------------- 画像
def profile(store, today: str | None = None, weeks: int = 4) -> dict:
    """近 N 周的跑量画像：周均里程、平均每周跑几天、中位配速。

    只读**跑步类**活动（骑行 100 km 不该变成跑步的周目标）。配速取中位数，
    不取平均：一次跑崩的 8'30" 能把均值拉歪，中位数不会。
    """
    from .sports import RUN_TYPES

    today = today or dt.date.today().isoformat()
    end = dt.date.fromisoformat(today) - dt.timedelta(days=1)
    start = end - dt.timedelta(days=weeks * 7 - 1)
    rows = []
    try:
        for r in store.activities(start.isoformat(), end.isoformat()):
            if (r.get("type") or "") not in RUN_TYPES:
                continue
            km = float(r.get("distance_m") or 0) / 1000
            if km < 0.5:                      # 走路误记的跑步不算样本
                continue
            rows.append(r)
    except Exception:
        rows = []

    total = sum(float(r.get("distance_m") or 0) / 1000 for r in rows)
    days = {r.get("date") for r in rows if r.get("date")}
    pace = [float(r.get("pace_s_km")) for r in rows
            if r.get("pace_s_km") and 150 <= float(r.get("pace_s_km")) <= 900]
    per_week = round(total / weeks, 1)
    run_days = int(round(len(days) / weeks))
    # 数据太少（新装、刚补历史）时不要给出「一周跑 0 天」这种没法用的结论
    if not rows:
        run_days, per_week = 3, 0.0
    run_days = max(1, min(6, run_days))
    return {
        "weeks": weeks,
        "samples": len(rows),
        "days_trained": len(days),
        "week_km": per_week,
        "run_days": run_days,
        "pace_s_km": int(statistics.median(pace)) if pace else DEFAULT_PACE,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }


def race_factor(today: str | None = None) -> tuple[float, dict | None]:
    """按「离下一场比赛还有几天」给周量系数。没有赛事就是 1.0。"""
    today = today or dt.date.today().isoformat()
    try:
        from . import event as EV

        ev = EV.nearest_upcoming(dt.date.fromisoformat(today))
    except Exception:
        ev = None
    if not ev:
        return 1.0, None
    days = int(ev.get("days") or 0)
    if days <= 7:
        f = 0.5
    elif days <= 21:
        f = 0.7
    elif days <= 84:
        f = 1.1
    else:
        f = 1.05
    return f, {"slug": ev.get("slug"), "name": ev.get("name"),
               "date": ev.get("date"), "days": days,
               "distance_km": ev.get("distance_km") or 0}


def _week_index(rules_path: Path | None = None) -> int:
    r = _load_json(_rules_path(rules_path))
    try:
        return max(1, int(r.get("week_index") or 1))
    except (TypeError, ValueError):
        return 1


# ---------------------------------------------------------------- 排课
def _spread(week_km: float, run_days: int) -> dict[int, tuple[str, float]]:
    """把周量摊到具体星期几，返回 {weekday: (kind, km)}。"""
    run_days = max(1, min(6, run_days))
    if week_km < MIN_WEEK_KM:
        # 量太小：只给两次轻松跑，不排长距离也不排强度
        run_days = min(run_days, 2)
    long_share, quality_share = SHARE[run_days]
    pick = sorted(SLOT_ORDER[:run_days])

    has_long = 6 in pick
    has_quality = bool(quality_share) and 3 in pick

    long_km = 0.0
    if has_long:
        long_km = min(round(week_km * long_share * 2) / 2, MAX_LONG_KM,
                      round(week_km * 0.45 * 2) / 2)
        long_km = max(3.0, long_km)
    qual_km = max(3.0, round(week_km * quality_share * 2) / 2) if has_quality else 0.0

    easy_days = [d for d in pick if d != 6 and d != 3]
    if not has_quality:
        easy_days = [d for d in pick if d != 6]

    rest = max(0.0, week_km - long_km - qual_km)
    each = 0.0
    if easy_days:
        each = round(rest / len(easy_days) * 2) / 2
        # 轻松跑不该比长距离还长——那等于把「长距离」的名头按在了短课上。
        # 超出的部分整块挪给长距离，周总量不变。
        if has_long and each > long_km:
            each = round(long_km * 2) / 2
            long_km = min(MAX_LONG_KM,
                          round(week_km - each * len(easy_days) - qual_km, 1))
        each = max(2.0, each)

    out: dict[int, tuple[str, float]] = {}
    if has_long:
        out[6] = ("long", long_km)
    if has_quality:
        out[3] = ("tempo", qual_km)
    for d in easy_days:
        out[d] = ("easy", each)
    return out


def _item(kind: str, km: float, pace_s: int) -> dict:
    pace = pace_s * PACE_FACTOR.get(kind, 1.0)
    minutes = max(10, int(round(km * pace / 60 / 5)) * 5)
    return {
        "name": NAME.get(kind, "轻松跑"),
        "detail": DETAIL.get(kind, ""),
        "sport": "跑步",
        "sport_key": "running",
        "kind": kind,
        "km": round(km, 1),
        "minutes": minutes,
        "steps": [],
        "rest": False,
        "note": "",
    }


def build_draft(store=None, today: str | None = None, monday: str | None = None,
                run_days: int | None = None, week_km: float | None = None,
                from_date: str | None = None,
                path: Path | None = None, rules_path: Path | None = None,
                save: bool = True) -> dict:
    """生成一份整周草稿。**不写 plan.json，也不写 overrides**，只出草稿。

    `run_days` / `week_km` 可以显式传，让人手动调整框架；不传就按历史画像。

    `from_date` 之后才有课。自动续用它避开今天——下午三点给人排一节 13 km
    的轻松跑，只会让人直接关掉这个功能。
    """
    today = today or dt.date.today().isoformat()
    d0 = dt.date.fromisoformat(today)
    mon = dt.date.fromisoformat(str(monday)[:10]) if monday else d0 - dt.timedelta(days=d0.weekday())

    prof = profile(store, today) if store is not None else {
        "weeks": 4, "samples": 0, "days_trained": 0, "week_km": 0.0,
        "run_days": 3, "pace_s_km": DEFAULT_PACE,
    }
    base_km = float(week_km if week_km is not None else prof["week_km"])
    days = int(run_days if run_days is not None else prof["run_days"])

    f_race, race = race_factor(today)
    idx = _week_index(rules_path)
    f_deload = 0.7 if idx % 4 == 0 else 1.0
    factor = min(f_race, f_deload)

    target = base_km * factor if base_km > 0 else 0.0
    if base_km > 0:
        target = min(target, base_km * GROWTH_CAP)
    target = round(target * 2) / 2

    reasons = []
    if week_km is not None or run_days is not None:
        reasons.append(f"按你指定的框架：每周 {days} 天、{base_km:g} km")
    elif prof["samples"]:
        reasons.append(f"近 {prof['weeks']} 周周均 {prof['week_km']:g} km，"
                       f"平均每周跑 {prof['run_days']} 天")
    else:
        reasons.append("本地还没有跑步记录，先按 3 天框架给个起步量")
    if race:
        reasons.append(f"下一场是 {race['date']} 的 {race['name']}，还有 {race['days']} 天"
                       f"（周量 ×{f_race:g}）")
    if f_deload < 1:
        reasons.append(f"第 {idx} 周按计划减量（周量 ×{f_deload:g}）")
    if not target:
        reasons.append("没有可参考的跑量，先不排里程；跑两周后会按实际调整")

    pace = int(prof.get("pace_s_km") or DEFAULT_PACE)
    spread = _spread(target, days) if target > 0 else {}
    if from_date:
        fd = dt.date.fromisoformat(str(from_date)[:10])
        spread = {wd: v for wd, v in spread.items()
                  if mon + dt.timedelta(days=wd - 1) >= fd}

    week = []
    for i in range(7):
        day = mon + dt.timedelta(days=i)
        date = day.isoformat()
        hit = spread.get(day.isoweekday())
        items = [_item(hit[0], hit[1], pace)] if hit else []
        week.append({
            "date": date,
            "dow": PL.DOW[day.isoweekday()],
            "items": items,
            "rest": not items,
            "source": "generated",
            "past": date < today,
        })

    draft = {
        "ok": True,
        "kind": "generated",
        "source": "generated",
        "created_at": int(time.time()),
        "today": today,
        "monday": mon.isoformat(),
        "week_index": idx,
        "factor": round(factor, 3),
        "profile": prof,
        "race": race,
        "reason": reasons,
        "week_km": round(sum(float(x["km"]) for w in week for x in w["items"]), 1),
        "days": sum(1 for w in week if w["items"]),
        "week": week,
        "confirmed": False,
    }
    if save:
        _dump(_draft_path(path), draft)
    return draft


def load_draft(path: Path | None = None) -> dict | None:
    d = _load_json(_draft_path(path))
    return d or None


# ---------------------------------------------------------------- 确认
def confirm(path: Path | None = None, rules_path: Path | None = None,
            today: str | None = None) -> dict:
    """把草稿写进 overrides，并记住「以后可以自动续」。

    只写今天及以后的天：把过去的日子改成新计划，会让已完成的那几天凭空
    变成「未完成」，完成率立刻失真。
    """
    from . import planedit as PE

    draft = load_draft(path)
    if not draft or not draft.get("week"):
        return {"ok": False, "error": "没有待确认的草稿"}
    today = today or draft.get("today") or dt.date.today().isoformat()

    days = []
    for w in draft["week"]:
        if not isinstance(w, dict):
            continue
        if str(w.get("date") or "") < today:
            continue
        days.append({"date": w.get("date"), "items": w.get("items") or [],
                     "rest": bool(w.get("rest")),
                     "origin": "generated",
                     "note": "自动生成" if w.get("items") else ""})

    # 用户亲手改过的那天，草稿不许盖。自动续是给「没管这周」的人用的，
    # 不是给「安排好了又被系统改掉」的人用的。
    kept = {x.get("date") for x in
            PE.list_overrides(path=(Path(path).parent if path else None)).get("items") or []
            if x.get("origin") == "manual"}
    skipped = [d["date"] for d in days if d["date"] in kept]
    days = [d for d in days if d["date"] not in kept]

    if not days:
        return {"ok": False, "error": "这一周每天都已被你手动安排过，草稿没有可写的位置",
                "skipped_manual": skipped}

    r = PE.save_week(days, path=(Path(path).parent if path else None))

    rules = _load_json(_rules_path(rules_path))
    rules.update({
        "auto": True,
        "week_index": int(draft.get("week_index") or 1) + 1,
        "last_week": draft.get("monday"),
        "confirmed_at": int(time.time()),
    })
    _dump(_rules_path(rules_path), rules)

    draft["confirmed"] = True
    _dump(_draft_path(path), draft)
    return {"ok": True, "saved": r.get("saved"), "dates": r.get("dates"),
            "skipped_manual": skipped,
            "week_km": draft.get("week_km"), "auto_renew": True}


def rules(path: Path | None = None) -> dict:
    r = _load_json(_rules_path(path))
    return {"auto": bool(r.get("auto")), "week_index": int(r.get("week_index") or 1),
            "last_week": r.get("last_week"), "confirmed_at": r.get("confirmed_at")}


def auto_renew(store=None, today: str | None = None, path: Path | None = None,
               rules_path: Path | None = None) -> dict:
    """每周一自动续下周：**只对确认过一次的生成型计划生效**。

    由 App 启动时调一次即可，不需要后台定时任务（国产 ROM 上那玩意儿活不过
    一晚）。判断放在这里而不是客户端：客户端只负责「醒来了就问我一句」。
    """
    from . import planedit as PE
    from . import plan as _PL

    today = today or dt.date.today().isoformat()
    r = rules(rules_path)
    if not r["auto"]:
        return {"ok": True, "action": "off", "reason": "没有开启自动续"}

    d0 = dt.date.fromisoformat(today)
    mon = (d0 - dt.timedelta(days=d0.weekday())).isoformat()
    if r.get("last_week") == mon:
        return {"ok": True, "action": "skip", "reason": "这周已经续过了"}

    # 本周已经有计划（用户自己排的、或从日历同步来的）就不动它
    cur = _PL.load(_home() / "data" / "plan.json") if (_home() / "data" / "plan.json").exists() else None
    tgt = _PL.week_target(cur, mon)
    if tgt.get("available"):
        return {"ok": True, "action": "skip", "reason": "本周已有计划",
                "km": tgt["km"], "source": tgt.get("source")}

    draft = build_draft(store, today=today, monday=mon,
                        from_date=(d0 + dt.timedelta(days=1)).isoformat(),
                        path=path, rules_path=rules_path)
    if not draft.get("week_km"):
        return {"ok": True, "action": "skip", "reason": "没有可参考的跑量",
                "draft": draft}
    res = confirm(path, rules_path, today)
    return {"ok": True, "action": "renewed", "monday": mon,
            "week_km": draft.get("week_km"), "saved": res.get("saved"),
            "reason": draft.get("reason")}
