"""训练计划的读取与归一化。

两条数据源，输出**同一个结构**，展示层不用区分：

1. **佳明 Connect 日历**（首选）：`/calendar-service/year/{y}/month/{m}`
   自己排进日历的课程（`workout`）、比赛（`race`）都在这里，已完成的
   当天会有 `activity` 项。Garmin Coach 暂停时也能照常读到排课。
2. **佳明教练计划**（兜底）：`/trainingplan-service/trainingplan/...`
   只有启用 Garmin Coach / FBT 自适应计划时才有内容。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

DOW = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}
SPORT = {
    "running": "跑步",
    "trail_running": "越野跑",
    "cycling": "骑行",
    "swimming": "游泳",
    "strength_training": "力量",
    "cardio_training": "有氧",
    "walking": "步行",
}
PHASE = {
    "TRANSITION": "过渡期",
    "BASE": "基础期",
    "BUILD": "建设期",
    "PEAK": "巅峰期",
    "TAPER": "减量期",
    "RACE": "比赛期",
}
EFFECT = {
    "AEROBIC_BASE": "有氧基础",
    "AEROBIC_CAPACITY": "有氧能力",
    "LACTATE_THRESHOLD": "乳酸阈值",
    "VO2MAX": "最大摄氧量",
    "ANAEROBIC_CAPACITY": "无氧能力",
    "SPRINT": "冲刺",
    "RECOVERY": "恢复",
    "MAINTAIN": "维持",
}
LEVEL = {"Beginner": "初级", "Intermediate": "中级", "Advanced": "高级"}


def _iso(value: str | None) -> str | None:
    return value[:10] if isinstance(value, str) and len(value) >= 10 else None


def _task_date(task: dict) -> str | None:
    return _iso((task.get("taskWorkout") or {}).get("scheduledDate"))


def normalize(detail: dict, today: str | None = None) -> dict:
    """把 taskList 展开成「按日期分组的本周课表 + 今天的建议 + 下一次训练」。"""
    today = today or dt.date.today().isoformat()
    tasks = detail.get("taskList") or []

    by_date: dict[str, list[dict]] = {}
    for t in tasks:
        date = _task_date(t)
        if not date:
            continue
        w = t.get("taskWorkout") or {}
        item = {
            "name": w.get("workoutName"),
            "detail": w.get("workoutDescription"),
            "sport": SPORT.get(((w.get("sportType") or {}).get("sportTypeKey")), None),
            "sport_key": (w.get("sportType") or {}).get("sportTypeKey"),
            "minutes": round((w.get("estimatedDurationInSecs") or 0) / 60) or None,
            "effect": EFFECT.get(w.get("trainingEffectLabel") or "", None),
            "rest": bool(w.get("restDay")),
            "status": w.get("adaptiveCoachingWorkoutStatus"),
            "priority": w.get("priorityType"),
            "uuid": w.get("workoutUuid"),
            "note": t.get("taskNote"),
        }
        by_date.setdefault(date, []).append(item)

    # 本周（周一起）范围
    d = dt.date.fromisoformat(today)
    monday = d - dt.timedelta(days=d.weekday())
    week_dates = [(monday + dt.timedelta(days=i)).isoformat() for i in range(7)]

    week = []
    for date in week_dates:
        items = [x for x in by_date.get(date, []) if not x["rest"]]
        week.append({
            "date": date,
            "dow": DOW[dt.date.fromisoformat(date).isoweekday()],
            "items": items,
            "rest": not items,
        })

    today_items = [x for x in by_date.get(today, []) if not x["rest"]]
    upcoming = sorted(k for k in by_date if k > today and any(not x["rest"] for x in by_date[k]))

    phases = detail.get("planPhases") or []
    cur_phase = next((p for p in phases if p.get("currentPhase")), None)
    if not cur_phase:  # 没标 current 就按日期落点找
        for p in phases:
            if (_iso(p.get("startDate")) or "") <= today <= (_iso(p.get("endDate")) or "9999"):
                cur_phase = p
                break

    return {
        "available": True,
        "plan_id": detail.get("trainingPlanId"),
        "name": detail.get("name"),
        "description": detail.get("description"),
        "level": LEVEL.get((detail.get("trainingLevel") or {}).get("levelKey"), None),
        "version": (detail.get("trainingVersion") or {}).get("versionName"),
        "weeks": detail.get("durationInWeeks"),
        "per_week": detail.get("avgWeeklyWorkouts"),
        "start": _iso(detail.get("startDate")),
        "end": _iso(detail.get("endDate")),
        "status": (detail.get("trainingStatus") or {}).get("statusKey"),
        "phase": {
            "key": (cur_phase or {}).get("trainingPhase"),
            "label": PHASE.get((cur_phase or {}).get("trainingPhase") or "", None),
            "start": _iso((cur_phase or {}).get("startDate")),
            "end": _iso((cur_phase or {}).get("endDate")),
        } if cur_phase else None,
        "today": {"date": today, "items": today_items, "rest": not today_items},
        "week": week,
        "next_date": upcoming[0] if upcoming else None,
        "next_items": [x for x in by_date.get(upcoming[0], []) if not x["rest"]] if upcoming else [],
        "rest_days_in_week": sum(1 for w in week if w["rest"]),
    }


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _cal_item(it: dict) -> dict:
    """把一个日历项折成课表行。字段名各地区略有差异，这里都兜一遍。"""
    dur = _num(it.get("duration") or it.get("estimatedDurationInSecs")
               or it.get("elapsedDuration"))
    dist = _num(it.get("distance") or it.get("estimatedDistanceInMeters"))
    sport_key = it.get("sportTypeKey") or (it.get("activityType") or {}).get("typeKey")
    return {
        "name": it.get("title") or it.get("workoutName") or "训练",
        "detail": it.get("description") or "",
        "sport": SPORT.get(sport_key),
        "sport_key": sport_key,
        "minutes": round(dur / 60) or None,
        "km": round(dist / 1000, 1) or None,
        "effect": EFFECT.get(it.get("trainingEffectLabel") or ""),
        "rest": False,
        "race": bool(it.get("isRace")) or str(it.get("itemType") or "").lower() == "race",
        "workout_id": it.get("workoutId"),
        "status": None,
    }


def from_calendar(items: list[dict] | None, today: str | None = None) -> dict:
    """日历项 → 与 `normalize()` 相同结构的课表。

    只取 `workout` / `race`（自己排进日历的课与比赛）；`activity` 是同一天已
    完成的记录，只用来给当天打「已完成」标记，不再重复成一条课。
    """
    today = today or dt.date.today().isoformat()
    workouts: dict[str, list[dict]] = {}
    done: set[str] = set()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        day = _iso(it.get("date"))
        if not day:
            continue
        kind = str(it.get("itemType") or "").lower()
        if kind == "activity":
            done.add(day)
            continue
        if kind not in ("workout", "race") and not it.get("workoutId"):
            continue
        workouts.setdefault(day, []).append(_cal_item(it))

    d = dt.date.fromisoformat(today)
    monday = d - dt.timedelta(days=d.weekday())
    week_dates = [(monday + dt.timedelta(days=i)).isoformat() for i in range(7)]

    week = []
    for date in week_dates:
        its = workouts.get(date, [])
        week.append({"date": date, "dow": DOW[dt.date.fromisoformat(date).isoweekday()],
                     "items": its, "rest": not its, "done": date in done})

    today_items = workouts.get(today, [])
    upcoming = sorted(k for k in workouts if k > today)
    days = sorted(workouts)

    return {
        "available": True,
        "scheduled": bool(workouts),
        "source": "calendar",
        "name": "佳明日历",
        "description": "来自佳明 Connect 日历的排课（自己排入的课程与比赛）",
        "level": None, "weeks": None, "per_week": None, "version": None,
        "start": days[0] if days else today,
        "end": days[-1] if days else today,
        "status": None, "phase": None,
        "today": {"date": today, "items": today_items,
                  "rest": not today_items, "done": today in done},
        "week": week,
        "next_date": upcoming[0] if upcoming else None,
        "next_items": workouts.get(upcoming[0], []) if upcoming else [],
        "rest_days_in_week": sum(1 for w in week if w["rest"]),
        "done_days": sorted(done),
    }


def _months(today: str, span: int = 62) -> list[tuple[int, int]]:
    """从当月开始的月份列表：够覆盖未来 span 天即可（日历按月取）。"""
    d = dt.date.fromisoformat(today)
    cur = d.replace(day=1)
    out: list[tuple[int, int]] = []
    while (cur - d).days <= span and len(out) < 4:
        out.append((cur.year, cur.month))
        cur = (cur + dt.timedelta(days=32)).replace(day=1)
    return out


def fetch_calendar(client, today: str | None = None) -> dict:
    """按月拉日历并归一化。一个排课都没有时返回 available=False。"""
    today = today or dt.date.today().isoformat()
    items: list[dict] = []
    for year, month in _months(today):
        try:
            data = client.calendar_month(year, month)
        except Exception as e:
            if not items:
                return {"available": False,
                        "reason": f"日历读取失败：{type(e).__name__}: {e}"}
            break
        if isinstance(data, dict):
            items.extend(data.get("calendarItems") or [])
        elif isinstance(data, list):
            items.extend(data)
    if not items:
        return {"available": False, "reason": "佳明日历里还没有排课"}
    return from_calendar(items, today)


def fetch(client, today: str | None = None, out_path: Path | None = None,
          source: str = "auto") -> dict:
    """拉一次训练安排。取不到就返回 available=False，不抛异常。

    `source`：
    * `auto`（默认）—— 先看日历，日历里没有排课再回落到 Garmin Coach 计划；
    * `calendar` —— 只看日历；
    * `coach` —— 只看教练计划。

    之所以日历优先：Garmin Coach 暂停后 active-plans 是空的，但自己排进日历的
    课程/比赛仍在，那才是实际执行的计划。
    """
    today = today or dt.date.today().isoformat()

    if source in ("auto", "calendar"):
        try:
            cal = fetch_calendar(client, today)
        except Exception as e:
            cal = {"available": False, "reason": f"{type(e).__name__}: {e}"}
        use = cal.get("scheduled") if source == "auto" else cal.get("available")
        if use:
            if out_path:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(json.dumps(cal, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
            return cal
        if source == "calendar":
            return cal

    try:
        plans = client.active_plans(today)
    except Exception as e:
        return {"available": False, "reason": f"{type(e).__name__}: {e}"}
    if not plans:
        return {"available": False, "reason": "当前没有激活的训练计划"}

    plan = plans[0]
    detail = None
    try:
        detail = client.adaptive_plan(plan.get("trainingPlanId"))
    except Exception:
        detail = None
    data = normalize(detail or plan, today)
    data["source"] = "coach"

    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def load(path: Path) -> dict | None:
    """读本地缓存的计划快照（看板/MCP 用，避免每次联网）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def attach(res: dict, path: Path | None = None) -> dict:
    """把本地计划快照挂到 derive() 的结果上，供教练上下文与看板使用。"""
    res["plan"] = load(path or (Path(__file__).resolve().parent.parent / "data" / "plan.json"))
    return res


def item_line(item: dict) -> str:
    """单条课表转一行中文。"""
    bits = [item.get("name") or "训练"]
    if item.get("detail"):
        bits.append(item["detail"])
    if item.get("minutes"):
        bits.append(f"{item['minutes']} 分钟")
    if item.get("effect"):
        bits.append(item["effect"])
    if item.get("sport") and item.get("sport") != "跑步":
        bits.append(item["sport"])
    return " · ".join(bits)
