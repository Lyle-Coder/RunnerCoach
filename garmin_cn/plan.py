"""佳明教练计划（Garmin Run Coach / FBT 自适应）的读取与归一化。

接口来源（国区 /gc-api）：
  GET /trainingplan-service/trainingplan/active-plans?startDate=YYYY-MM-DD
  GET /trainingplan-service/trainingplan/fbt-adaptive/{planId}   ← taskList 是逐日课表
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


def fetch(client, today: str | None = None, out_path: Path | None = None) -> dict:
    """拉一次当前计划。取不到就返回 available=False，不抛异常。"""
    today = today or dt.date.today().isoformat()
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
