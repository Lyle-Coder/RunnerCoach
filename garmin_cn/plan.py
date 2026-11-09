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
import os
import re
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
# 向前看多久：备赛周期常到 20 周（140 天），只拉一个月会看不到远期课
LOOKAHEAD_DAYS = 140


def _iso(value: str | None) -> str | None:
    return value[:10] if isinstance(value, str) and len(value) >= 10 else None


def _task_date(task: dict) -> str | None:
    return _iso((task.get("taskWorkout") or {}).get("scheduledDate"))


def normalize(detail: dict, today: str | None = None,
              activity_days: set[str] | None = None) -> dict:
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
            "minutes": round(_as_seconds(_num(w.get("estimatedDurationInSecs"))) / 60) or None,
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

    data = {
        "available": True,
        "classify": CLASSIFY_REV,
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
        "today": {"date": today, "items": today_items, "acts": [],
                  "rest": not today_items},
        "week": week,
        "next_date": upcoming[0] if upcoming else None,
        "next_items": [x for x in by_date.get(upcoming[0], []) if not x["rest"]] if upcoming else [],
        "rest_days_in_week": sum(1 for w in week if w["rest"]),
    }
    # 教练计划以前完全没有「完成」概念，本周永远是「已排」，也算不出来；
    # 用同一套证据（本地库 / 日历活动）统一标注。
    return annotate(data, activity_days, today)


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# 日历里这些不是「排课」，只是同一天的其它记录
_SKIP_TYPES = {"note", "goal", "badge", "event", "health",
               "weighin", "measurement"}


MAX_SESSION_S = 24 * 3600          # 单次训练超过一天就不正常了（超马另算）


def _as_seconds(raw: float, *, race: bool = False) -> float:
    """把「耗时」统一成秒，并挡住单位错误。

    Garmin 的字段单位并不统一：`estimatedDurationInSecs` 明确是秒，但日历项里的
    `duration` 在部分端（网页端 / 新版本的「计划活动」）给的是**毫秒**。
    旧代码一律按秒读，于是 1,072,560 毫秒 —— 一次 17 分 52 秒的骑行 ——
    被显示成 **17876 分钟**（12 天）。这不是数据脏，是单位没判。

    规则：≤ 上限就按秒；超了但除以 1000 落回合理区间，说明原来是毫秒；
    两种读法都离谱就当没给（宁可显示「—」，也不要摆一个 12 天）。
    比赛不受 24 小时限制（100 英里越野能跑 30 小时）。
    """
    if raw <= 0:
        return 0.0
    limit = 72 * 3600 if race else MAX_SESSION_S
    if raw <= limit:
        return raw
    alt = raw / 1000.0
    if 60 <= alt <= limit:
        return alt
    return 0.0


MAX_KM = 500.0            # 单次训练的距离上限（超马、长途骑行都在内）
MAX_KMH = 130.0           # 合理时速上限：再快就不是人，是单位错了


def _distance_km(raw, dur_s: float) -> float:
    """把日历里的距离换算成公里，顺带挡住单位错误。

    `distance` 和 `duration` 一样，不同端给的单位不一致：有米，也有**厘米**
    （安卓上传的活动常见）。旧代码一律 `dist / 1000` 当米读，于是今早 4.09 km
    的通勤被显示成 **409.3 公里**。

    用「隐含时速」判比拍一个阈值靠谱：4 公里骑 18 分钟是 13 km/h，正常；
    409 公里骑 18 分钟是 1364 km/h，物理上不可能 —— 那说明原值是厘米。
    依次试 米 / 厘米 / 毫米，取第一个「距离合理且时速合理」的读法。
    """
    try:
        v = float(raw or 0)
    except (TypeError, ValueError):
        return 0.0
    if v <= 0:
        return 0.0

    hours = (dur_s or 0) / 3600.0
    for div in (1.0, 100.0, 1000.0):        # 米 / 厘米 / 毫米
        km = v / div / 1000.0
        if km > MAX_KM:
            continue
        # 时间太短时算不准时速，只校验距离
        if hours > 0.01 and km / hours > MAX_KMH:
            continue
        return km
    return 0.0


def _dur_seconds(it: dict, *, race: bool = False) -> float:
    """按可信度取耗时字段：带单位语义的优先，其余依次兜底。"""
    for key in ("estimatedDurationInSecs", "duration", "elapsedDuration"):
        raw = _num(it.get(key))
        if raw > 0:
            return _as_seconds(raw, race=race)
    return 0.0


def _cal_item(it: dict) -> dict:
    """把一个日历项折成课表行。字段名各地区略有差异，这里都兜一遍。"""
    is_race = (bool(it.get("isRace"))
               or str(it.get("itemType") or "").lower() == "race")
    dur = _dur_seconds(it, race=is_race)
    # 带单位的优先；否则按「米→厘米→毫米」挑一个物理上说得通的读法
    dist = _num(it.get("estimatedDistanceInMeters"))
    km = dist / 1000.0 if dist > 0 else _distance_km(it.get("distance"), dur)
    sport_key = it.get("sportTypeKey") or (it.get("activityType") or {}).get("typeKey")
    return {
        "name": it.get("title") or it.get("workoutName") or "训练",
        "detail": it.get("description") or "",
        "sport": SPORT.get(sport_key),
        "sport_key": sport_key,
        "minutes": round(dur / 60) or None,
        "km": round(km, 1) or None,
        "effect": EFFECT.get(it.get("trainingEffectLabel") or ""),
        "rest": False,
        "race": is_race,
        "workout_id": it.get("workoutId"),
        "status": None,
    }


# 课表分类口径的版本号。变更记录：
#   1 — 按日期猜完成（错：过去即已完成）
#   2 — 看 activityId + 本地库活动日，但把「无 id 的 activity」当计划活动（错）
#   3 — `activity` 一律是已完成记录；计划只来自 workout / race
CLASSIFY_REV = 3


def annotate(data: dict, activity_days: set[str] | None = None,
             today: str | None = None) -> dict:
    """按「完成证据」重算 done / missed，不靠日期猜。

    证据只有两种：
      1. 日历里**带 activityId** 的记录（已经做完的活动）；
      2. 本地库里那天确实有活动 —— 这条很重要：高驰记录的活动压根不会出现在
         佳明日历里，只看日历会把「已经练了」误判成「没练」。

    `done_days` 只有在新口径（CLASSIFY_REV ≥ 2）落盘的快照里才可信：旧版本把
    「今天之前的所有项」都写成已完成，那是错的，读旧快照时直接丢掉重算。
    """
    today = today or dt.date.today().isoformat()
    done = set(data.get("done_days") or []) if int(data.get("classify") or 0) >= CLASSIFY_REV else set()
    done |= {d for d in (activity_days or set()) if d}

    for w in data.get("week") or []:
        w["done"] = w["date"] in done
        # 今天还没过完，谈不上「没完成」
        w["missed"] = bool(w.get("items")) and not w["done"] and w["date"] < today
    t = data.get("today") or {}
    if t:
        t["done"] = t.get("date") in done
        t["missed"] = False

    data["done_days"] = sorted(done)
    data["week_done"] = sum(1 for w in data.get("week") or [] if w.get("done"))
    data["week_planned"] = sum(1 for w in data.get("week") or [] if w.get("items"))
    return data


def from_calendar(items: list[dict] | None, today: str | None = None,
                  activity_days: set[str] | None = None) -> dict:
    """日历项 → 与 `normalize()` 相同结构的课表。

    日历里三类东西必须分开，分不清就会出现「计划没做被标成已完成」：

    | 形态 | 含义 | 处理 |
    |---|---|---|
    | `workout` / `race` | 排进日历的课 | **计划**，今天之后要练的 |
    | `activity` + activityId | 真做完的活动 | **完成证据**，当天没排课时单独显示 |
    | `activity` 无 id | 佳明把「排入计划的训练」也记成 activity | **计划**，不能因为日期在过去就算完成 |

    最后一条是旧实现的坑：它把「今天之前的所有项」都当已完成，于是上周计划了
    但没跑的课被悄悄抹掉，历史完成率永远是 100%。

    日期窗口是「本周一 ~ 今天 + 140 天」；`activity_days` 是本地库里有活动的日期，
    用来兜住「高驰记录、佳明日历里没有」的情况。
    """
    today = today or dt.date.today().isoformat()
    d = dt.date.fromisoformat(today)
    monday = d - dt.timedelta(days=d.weekday())
    lo = monday.isoformat()
    hi = (d + dt.timedelta(days=LOOKAHEAD_DAYS)).isoformat()

    workouts: dict[str, list[dict]] = {}
    acts: dict[str, list[dict]] = {}
    done_ids: set[str] = set()
    seen: set[tuple] = set()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        day = _iso(it.get("date"))
        if not day or not (lo <= day <= hi):
            continue
        kind = str(it.get("itemType") or "").lower()
        if kind in _SKIP_TYPES:
            continue

        # `activity` 一律是**已经做完的记录**（佳明日历会把当天上传的活动也列进来），
        # 只当完成证据，绝不当排课。
        #
        # v61 曾把它按「有没有 activityId」二分：有 id → 已完成，没 id → 计划活动。
        # 那个推断是错的 —— 今早 4 公里通勤没有 id，于是被当成「今日课表」，
        # 课表本该是休息却显示一条骑行。原始实现「activity 一律 continue」才是对的。
        if kind == "activity":
            done_ids.add(day)
            row = _cal_item(it)
            row["completed"] = True
            row["name"] = it.get("title") or row.get("name") or "已完成的训练"
            # 去重键：优先活动 id；没有就按「日期 + 名称 + 开始时间」
            seen_act = (day, row.get("name"),
                        it.get("activityId") or it.get("activityUUID")
                        or it.get("startTimeLocal"))
            if seen_act not in seen:
                seen.add(seen_act)
                acts.setdefault(day, []).append(row)
            continue

        # 其余才可能是「计划」：workout / race
        if not (it.get("title") or it.get("workoutId") or it.get("workoutUuid")):
            continue
        key = (day, str(it.get("title") or ""), it.get("workoutId"))
        if key in seen:                      # 多拉了相邻月份时可能重复
            continue
        seen.add(key)
        row = _cal_item(it)
        row["planned"] = True
        workouts.setdefault(day, []).append(row)

    week_dates = [(monday + dt.timedelta(days=i)).isoformat() for i in range(7)]
    week = []
    for date in week_dates:
        its = workouts.get(date, [])
        act = acts.get(date, [])
        week.append({"date": date, "dow": DOW[dt.date.fromisoformat(date).isoweekday()],
                     "items": its, "acts": act,
                     "rest": not its and not act})

    today_items = workouts.get(today, [])
    today_acts = acts.get(today, [])
    upcoming = sorted(k for k in workouts if k > today)
    days = sorted(set(workouts) | set(acts))

    data = {
        "available": True,
        "scheduled": bool(workouts),
        "source": "calendar",
        "classify": CLASSIFY_REV,
        "name": "佳明日历",
        "description": "来自佳明 Connect 日历的排课（自己排入的课程与比赛）",
        "level": None, "weeks": None, "per_week": None, "version": None,
        "start": days[0] if days else today,
        "end": days[-1] if days else today,
        "status": None, "phase": None,
        "today": {"date": today, "items": today_items, "acts": today_acts,
                  "rest": not today_items and not today_acts},
        "week": week,
        "next_date": upcoming[0] if upcoming else None,
        "next_items": workouts.get(upcoming[0], []) if upcoming else [],
        "rest_days_in_week": sum(1 for w in week if w["rest"]),
        "done_days": sorted(done_ids),
    }
    return annotate(data, activity_days, today)


def _months(today: str, span: int = LOOKAHEAD_DAYS) -> list[tuple[int, int]]:
    """从**上个月**开始的月份列表。

    多拉一个月是为了兼容服务端 `month` 的 0-based / 1-based 两种语义：
    如果传 11 拿到的其实是 12 月，那传 10 才能拿到 11 月。多出来的月份由
    `from_calendar()` 的日期窗口过滤掉，不会显示成课。
    """
    d = dt.date.fromisoformat(today)
    cur = (d.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
    out: list[tuple[int, int]] = []
    while (cur - d).days <= span and len(out) < 7:
        out.append((cur.year, cur.month))
        cur = (cur + dt.timedelta(days=32)).replace(day=1)
    return out


def fetch_calendar(client, today: str | None = None,
                   activity_days: set[str] | None = None) -> dict:
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
    return from_calendar(items, today, activity_days)


def diag(client, today: str | None = None) -> dict:
    """诊断用：真拉一遍日历，回报月份、条数、类型分布、日期范围与样例。

    当「页面显示不对」但看不出原因时跑它（`cli.py plan --diag`）：
    * `requests` 里某个月 0 条 → 该月没数据或接口又不认这个月份参数；
    * `dates` 最早日期比今天晚很多 → 月份语义错位；
    * `types` 里看不到 workout/race → 课程用的是别的 itemType。
    """
    today = today or dt.date.today().isoformat()
    months = _months(today)
    out: dict = {"today": today, "months": [f"{y}-{m:02d}" for y, m in months],
                 "requests": [], "types": {}, "dates": [], "samples": []}
    items: list[dict] = []
    for y, m in months:
        try:
            data = client.calendar_month(y, m)
        except Exception as e:
            out["requests"].append({"month": f"{y}-{m:02d}",
                                    "error": f"{type(e).__name__}: {e}"})
            continue
        got: list = []
        if isinstance(data, dict):
            got = data.get("calendarItems") or []
        elif isinstance(data, list):
            got = data
        out["requests"].append({"month": f"{y}-{m:02d}", "items": len(got)})
        items.extend(got)

    for it in items:
        if isinstance(it, dict):
            k = str(it.get("itemType"))
            out["types"][k] = out["types"].get(k, 0) + 1
    days = sorted(d for d in (_iso(it.get("date")) for it in items
                              if isinstance(it, dict)) if d)
    out["dates"] = [days[0], days[-1]] if days else []
    out["count"] = len(items)
    # 样例里同时给「原始 duration」和「解析出来的分钟」：单位错了一眼能看出来
    out["samples"] = []
    for it in items[:12]:
        if not isinstance(it, dict):
            continue
        item = _cal_item(it)
        out["samples"].append({
            "date": it.get("date"), "itemType": it.get("itemType"),
            "title": it.get("title"), "sportTypeKey": it.get("sportTypeKey"),
            "duration": it.get("duration"),
            "estimatedDurationInSecs": it.get("estimatedDurationInSecs"),
            "distance": it.get("distance"), "workoutId": it.get("workoutId"),
            "isRace": it.get("isRace"),
            "parsed_minutes": item.get("minutes"),
            "parsed_km": item.get("km"),
        })
    parsed = from_calendar(items, today)
    out["parsed"] = {
        "today": [i.get("name") for i in parsed["today"]["items"]],
        "today_acts": [i.get("name") for i in (parsed["today"].get("acts") or [])],
        "next_date": parsed["next_date"],
        "week": [{"date": w["date"], "dow": w["dow"],
                  "items": [i.get("name") for i in w["items"]],
                  "acts": [i.get("name") for i in (w.get("acts") or [])],
                  "done": w.get("done"), "missed": w.get("missed")}
                 for w in parsed["week"]],
        "week_done": parsed.get("week_done"),
        "week_planned": parsed.get("week_planned"),
        "start": parsed["start"], "end": parsed["end"],
    }
    return out


def fetch(client, today: str | None = None, out_path: Path | None = None,
          source: str = "auto", activity_days: set[str] | None = None) -> dict:
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
            cal = fetch_calendar(client, today, activity_days)
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
    data = normalize(detail or plan, today, activity_days)
    data["source"] = "coach"

    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def load(path: Path) -> dict | None:
    """读本地缓存的计划快照（看板/MCP 用，避免每次联网）。

    旧口径（classify < CLASSIFY_REV）的快照直接作废：那份里的 `items` / `acts`
    是按老规则分的（把已完成的活动当成计划），而原始日历项没有存下来，
    没法就地修正。宁可当作「没有计划」让上层重新拉一次，也不要拿错的课表
    继续骗人 —— 手机上换版本后一直显示错数据，就是这个原因。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if int(data.get("classify") or 0) < CLASSIFY_REV:
        return None
    return data


def _home() -> Path:
    """数据目录：桌面用项目根，APK 里由 Kotlin 侧用 GARMIN_HOME 指到 filesDir。

    不能直接用 `__file__` 推：APK 里代码在 Chaquopy 的只读 assets 里，
    `data/plan.json` 却在 GARMIN_HOME 下，两边不是同一个地方——一图流在手机上
    读不到计划快照就是踩了这个坑。
    """
    env = os.environ.get("GARMIN_HOME")
    return Path(env).expanduser() if env else Path(__file__).resolve().parent.parent


def attach(res: dict, path: Path | None = None) -> dict:
    """把本地计划快照挂到 derive() 的结果上，供教练上下文与看板使用。"""
    res["plan"] = load(path or (_home() / "data" / "plan.json"))
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
    _sp = str(item.get("sport") or "")
    if _sp and _sp != "跑步" and _sp not in str(item.get("name") or ""):
        bits.append(_sp)
    return " · ".join(bits)


# ---------------------------------------------------------------- 本地兜底
# 纯高驰用户永远拿不到佳明日历（高驰没有对外计划接口）。以前 plan() 直接
# 返回 ok:false，今日页整块课表就不渲染 —— 明明本地有备赛课表和恢复度，
# 能算出「今天练什么」，却让人以为这个功能根本不存在。

def _minutes_of(text) -> int | None:
    """从「45 分钟」「60-75 分钟」「已练 30 分钟」里取分钟数，取上限。"""
    m = re.search(r"(\d+)\s*(?:[-~到]\s*(\d+))?\s*分", str(text or ""))
    if not m:
        return None
    return int(m.group(2) or m.group(1))


def from_coach(res: dict, today: str | None = None,
               activity_days: set[str] | None = None) -> dict:
    """本地兜底课表：备赛计划优先，没有就退回今日处方。

    输出结构与 `normalize()` / `from_calendar()` 完全一致，展示层不用区分
    数据来自佳明日历还是本地推的 —— 否则每加一个兜底就要改一遍页面。
    """
    from . import raceplan
    from .coach import pick_session

    today = today or dt.date.today().isoformat()
    d0 = dt.date.fromisoformat(today)
    try:
        has_race = bool(raceplan.available())
    except Exception:
        has_race = False

    monday = d0 - dt.timedelta(days=d0.weekday())
    week: list[dict] = []
    for i in range(7):
        day = monday + dt.timedelta(days=i)
        date = day.isoformat()
        items: list[dict] = []
        if has_race:
            try:
                info = raceplan.day_info(day)
            except Exception:
                info = None
            if info and not info.get("is_rest"):
                items.append({
                    "name": info.get("title") or info.get("abbr") or "训练",
                    "detail": info.get("tip") or info.get("sub") or "",
                    "sport": "跑步",
                    "sport_key": "running",
                    "minutes": info.get("minutes") or None,
                    "effect": None,
                    "rest": False,
                    "status": None,
                    "priority": None,
                    "uuid": None,
                    "note": info.get("note") or "",
                })
        week.append({"date": date, "dow": DOW[day.isoweekday()],
                     "items": items, "rest": not items})

    today_items = next((w["items"] for w in week if w["date"] == today), [])
    from_coach_source = "raceplan"
    today_note = ""
    today_rest = False
    if not today_items:
        # 今天没排备赛课（或压根没挂计划）：用恢复度算一个处方，
        # 至少让人知道该练什么强度，而不是看到一片空白。
        try:
            s = pick_session(res)
        except Exception:
            s = {}
        _kind = str(s.get("type") or "")
        if _kind and "休息" in _kind and "加练" not in _kind:
            # 处方是「休息」时不能塞成一条 20 分钟 Z1 的课 —— 那正是
            # v64 修过的坑（把记录当课表，看着像要练）。保持休息，
            # 把「为什么休息」写进 note，页面在标题下面显示。
            today_rest = True
            today_note = str(s.get("why") or "")
            from_coach_source = "coach"
        elif _kind:
            today_items = [{
                "name": _kind,
                "detail": s.get("why") or "",
                "sport": "跑步",
                "sport_key": "running",
                "minutes": _minutes_of(s.get("duration")),
                "effect": s.get("intensity"),
                "rest": False,
                "status": None,
                "priority": None,
                "uuid": None,
                "note": s.get("duration") or "",
            }]
            from_coach_source = "coach"
            week = [{**w, "items": today_items, "rest": False}
                    if w["date"] == today else w for w in week]

    data = {
        "available": True,
        "classify": CLASSIFY_REV,
        "plan_id": None,
        "name": "本地教练计划" if from_coach_source == "coach" else "本地备赛计划",
        "description": "由备赛课表与恢复度在本地算出，不依赖任何账号",
        "level": None,
        "version": None,
        "weeks": None,
        "per_week": None,
        "start": None,
        "end": None,
        "status": "local",
        "phase": None,
        "source": from_coach_source,
        "today": {"date": today, "items": today_items, "acts": [],
                  "rest": today_rest, "note": today_note},
        "week": week,
        "next_date": None,
        "next_items": [],
        "rest_days_in_week": sum(1 for w in week if w["rest"]),
    }
    return annotate(data, activity_days, today)
