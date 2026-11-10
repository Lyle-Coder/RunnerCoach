# -*- coding: utf-8 -*-
"""把 105K 备赛课次转成佳明 Connect 训练课程，并排进日历（手表自动同步）。

数据流：raceplan.day_info(日期) → build_all() 出 Connect 的 workout JSON（一天可能多课）→
POST /workout-service/workout 创建 → POST /workout-service/schedule/{id} 排到某天。

只依赖标准库与本项目 client。设计原则（v5，经真机验证修正）：
- 配速目标 pace.zone（targetTypeId 6）的单位是**米/秒**，快端在前：
  5:00/km = 3.3333 m/s。手表/网页会自动换算成 min/km 显示。
- 重复组（RepeatGroupDTO）的 endCondition 是 7 = iterations（6 是心率！）；
  力量动作次数用 9 = fixed.repetition，固定休息用 8 = fixed.rest。
- 力量课独立推送：sportTypeId 5 = strength_training（workout-service 专用映射，
  与 activity API 不同），周三/周五各推「力量课 + 跑步课」两节，同日排程。
- 路跑（周二质量课、周三/周五的跑、周内 easy）：配速区间监督，并按周相位放宽慢端
  ——恢复/回落/减量周整体后移；间歇步骤描述里保留心率红线。
- 山地课（周六长课、周日背靠背、比赛）与周四爬楼梯：GPS 配速不可靠，保留心率区间。

所有强度与执行卡上的缩略语同源（E/M/T/I/R 五区取自 make_plan.ZONES）。
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

SPORT_RUN = {"sportTypeId": 1, "sportTypeKey": "running"}
SPORT_STR = {"sportTypeId": 5, "sportTypeKey": "strength_training"}

# Garmin Connect step/end-condition/target 枚举（经真机验证 + 官方 DTO 对齐）
STEP_TYPE = {"warmup": 1, "cooldown": 2, "interval": 3, "recovery": 4,
             "rest": 5, "repeat": 6, "other": 7}
END_COND = {"lap": 1, "time": 2, "distance": 3, "heart_rate": 6,
            "iterations": 7, "fixed_rest": 8, "fixed_repetition": 9, "reps": 10}
TARGET = {"none": 1, "hr": 4, "pace": 6}   # pace.zone 单位 = 米/秒，快端在前

# 分区锚点重算（v8）：2026-11-08 安康半马 1:28:59 / 4:11/km / 均心率 190，
# 全程稳在 190–192、最后 3 km 加速到 3:25 → 阈值心率 LTHR ≈ 190、最大心率 ≈ 207。
# 旧版把 T 区写在 156–165（那实际是他的马拉松区），结果是「轻松跑不轻松、质量课不够狠」。
WARM_HR = (110, 150)      # 热身 / 冷身 / 组间恢复
EASY_HR = (140, 166)      # 轻松跑兜底上限（配速主导，心率只兜底）
LONG_HR = (135, 160)      # 山地长课 / 背靠背（105K 的比赛心率带）
RACE_HR = (130, 150)      # 越野赛（60 km 模拟赛 / 105K）
ZONE_HR = {"E": EASY_HR, "M": (167, 180), "T": (181, 190),
           "I": (191, 197), "R": (198, 207)}
# 五区平路配速（秒/公里，快端在前）。锚点全部来自实测：
# 145→5:38、152→5:23、164→4:56、182→4:35（杭州全马）、190→4:11（安康半马）。
PACE_SEC = {"E": (300, 340),   # 5:00–5:40  Z2 有氧，占 85% 的量
            "M": (275, 295),   # 4:35–4:55  Z3 马拉松
            "T": (250, 265),   # 4:10–4:25  Z4 阈值
            "I": (235, 250),   # 3:55–4:10  Z5 间歇
            "R": (225, 240)}   # 3:45–4:00  重复
# 恢复 / 回落 / 减量 / 赛前 / 比赛周：E 区窗口整体后移（5:20–6:00）
PACE_E_TIRED = (320, 360)
# 比赛周目标配速（秒/km）：key 是课表周序号，按自己报名的赛事调整
RACE_PACE_SEC = {1: (272, 288), 8: (270, 291)}
TIRED_PHASES = {"恢复", "回落", "减量", "赛前", "比赛"}

# 爬楼梯课的目标区间（上坡到 160 左右）
STAIR_HR = (160, 178)

# 力量课动作库：(名称, 组数, 次数 or None, 时长秒 or None, 要点)
# 周三 = 力量 A（臀腿 + 离心），周五 = 力量 B（核心 + 稳定），与 make_card 说明行同源
POWER = {
    2: [("单腿硬拉", 3, 10, None, "10 次/侧 · 慢下快起"),
        ("保加利亚分腿蹲", 3, 10, None, "10 次/侧 · 后脚垫高 前膝不内扣"),
        ("台阶上步", 3, 10, None, "10 次/侧 · 全脚踩实 不借反弹"),
        ("离心下蹲", 3, 8, None, "8 次 · 数 4 秒下 立刻起"),
        ("提踵", 3, 15, None, "15 次 · 顶端停 1 秒")],
    4: [("死虫", 3, 10, None, "10 次/侧 · 腰贴地 对侧手脚"),
        ("鸟狗", 3, 10, None, "10 次/侧 · 骨盆不转"),
        ("侧平板", 3, None, 30, "30 秒/侧"),
        ("弹力带侧向走", 3, 12, None, "12 步/侧 · 臀中发力"),
        ("单脚站", 3, None, 45, "45 秒/侧 · 闭眼更难")],
}

QUAL_RE = re.compile(r"(\d+)\s*[×x*]\s*(\d+)\s*min\s*@\s*([EMTIR])")
STAIR_RE = re.compile(r"St24\s*[×x*]\s*(\d+)")
# 本计划推送课程的课名特征（cli push --replace 按它识别并清理旧课）
OLD_NAME_RE = re.compile(r"^W\d+\s")


def pace_str(sec: int) -> str:
    return "%d:%02d" % divmod(sec, 60)


def pace_span(p: tuple[int, int]) -> str:
    return "%s–%s" % (pace_str(p[0]), pace_str(p[1]))


def mps(sec_per_km: int) -> float:
    """配速（秒/公里）→ 速度（米/秒），Connect pace.zone 存的就是这个。"""
    return round(1000.0 / sec_per_km, 4)


def pace_target(p: tuple[int, int]) -> tuple[float, float]:
    """(快端秒/km, 慢端秒/km) → (targetValueOne, targetValueTwo)，快端(m/s)在前。"""
    return mps(p[0]), mps(p[1])


def _step(order: int, step_type: str, end_cond: str, value: float,
          target: tuple[str, tuple[Any, Any]] | None = None, desc: str = "") -> dict:
    """一个可执行步（ExecutableStepDTO）。target = ("hr", (low, high)) 或 ("pace", (fast, slow))。"""
    s: dict[str, Any] = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": {"stepTypeId": STEP_TYPE[step_type], "stepTypeKey": step_type},
        "endCondition": {"conditionTypeId": END_COND[end_cond],
                         "conditionTypeKey": end_cond},
        "endConditionValue": value,
    }
    if target:
        tk, span = target
        key = "heart.rate.zone" if tk == "hr" else "pace.zone"
        s["targetType"] = {"workoutTargetTypeId": TARGET[tk],
                           "workoutTargetTypeKey": key}
        s["targetValueOne"] = span[0]
        s["targetValueTwo"] = span[1]
    else:
        s["targetType"] = {"workoutTargetTypeId": TARGET["none"],
                           "workoutTargetTypeKey": "no.target"}
    if desc:
        s["description"] = desc
    return s


def _repeat(order: int, times: int, steps: list[dict]) -> dict:
    """重复组（RepeatGroupDTO），组内步的 stepOrder 从 1 重新编号。

    注意两点（都是真机踩出来的）：
    - endCondition 是 7 = iterations（6 是心率，之前错用导致手表显示「0 次」）；
    - 光有 endConditionValue 不够，服务端还强制要独立的 numberOfIterations 字段，
      否则创建接口直接 400：RepeatGroup: numberOfIterations is required。
    """
    for i, s in enumerate(steps, 1):
        s["stepOrder"] = i
    return {
        "type": "RepeatGroupDTO",
        "stepOrder": order,
        "stepType": {"stepTypeId": STEP_TYPE["repeat"], "stepTypeKey": "repeat"},
        "endCondition": {"conditionTypeId": END_COND["iterations"],
                         "conditionTypeKey": "iterations"},
        "endConditionValue": times,
        "numberOfIterations": times,
        "workoutSteps": steps,
    }


def _e_pace(phase: str) -> tuple[int, int]:
    """本周状态的 E 区配速窗口：疲劳相位整体放慢。"""
    return PACE_E_TIRED if phase in TIRED_PHASES else PACE_SEC["E"]


def _power_steps(idx: int, order0: int) -> list[dict]:
    """把力量 A/B 动作表展开成重复组：3 组 ×（动作次数 + 45 秒组间休息）。"""
    steps: list[dict] = []
    order = order0
    for name, sets, reps, secs, tip in POWER[idx]:
        body = (_step(0, "interval", "fixed_repetition", reps, None,
                      "%s %s" % (name, tip))
                if reps else
                _step(0, "interval", "time", secs, None, "%s %s" % (name, tip)))
        steps.append(_repeat(order, sets,
                             [body,
                              _step(0, "recovery", "fixed_rest", 45, None,
                                    "组间休息 / 换边")]))
        order += 1
    return steps


def _workout(name: str, desc: str, sport: dict, segments: list[dict]) -> dict:
    return {
        "workoutName": name[:40],
        "description": desc[:200],
        "sportType": sport,
        "workoutSegments": segments,
    }


def _seg(sport: dict, steps: list[dict], order: int = 1) -> dict:
    return {"segmentOrder": order, "sportType": sport, "workoutSteps": steps}


def build(info: dict) -> dict | None:
    """单课模式：非力量日转成一节 Connect 课程。休息日返回 None。"""
    outs = build_all(info)
    return outs[0] if outs else None


def build_all(info: dict) -> list[dict]:
    """把某天的课次转成 Connect 课程列表。力量日 = 力量课 + 跑步课两节。"""
    if not info or info.get("is_rest"):
        return []

    kind = info["kind"]
    phase = info.get("phase") or ""
    km = float(info.get("km") or 0)
    minutes = int(info.get("minutes") or 0)
    vert = int(info.get("vert") or 0)
    name0 = f"W{info['week']} {info['dow']}"
    desc_bits = [info.get("tip") or "", info.get("note") or ""]
    tail = "｜本周 %d km / +%d m｜%s" % (info.get("week_km") or 0,
                                        info.get("week_vert") or 0,
                                        info.get("focus") or "")

    # ---------- 力量日：两节课 ----------
    if kind == "str":
        idx = 2 if "周三" in info.get("dow", "") else 4
        label = "力量A 臀腿" if idx == 2 else "力量B 核心"
        moves = "、".join(m[0] for m in POWER[idx])
        steps = [_step(1, "warmup", "time", 300, None, "关节活动度 + 弹力带唤醒")]
        steps.extend(_power_steps(idx, 2))
        steps.append(_step(2 + len(POWER[idx]), "cooldown", "time", 180, None,
                           "拉伸收尾：股四 / 腘绳 / 臀"))
        strength = _workout(
            f"{name0} {label}",
            ("动作：%s｜组间休 45 s，动作不熟就先做 1 组找感觉%s"
             % (moves, tail)),
            SPORT_STR, [_seg(SPORT_STR, steps)])

        run = None
        if km > 0:
            p = _e_pace(phase)
            run = _workout(
                f"{name0} {info['abbr']}",
                ("力量后轻松跑 · 配速 %s · 心率 ≤166，超了就再慢%s"
                 % (pace_span(p), "｜" + tail if tail else "")),
                SPORT_RUN,
                [_seg(SPORT_RUN, [_step(1, "other", "distance", km * 1000,
                                        ("pace", pace_target(p)),
                                        "配速 %s · 心率 ≤166 优先" % pace_span(p))])])
        return [x for x in (strength, run) if x]

    # ---------- 单课日 ----------
    steps: list[dict] = []
    if kind == "quality":
        m = QUAL_RE.search(info.get("abbr") or "")
        if m:
            n, dur, zk = int(m.group(1)), int(m.group(2)), m.group(3)
            z = ZONE_HR[zk]
            p = PACE_SEC[zk]
            rec = 3 if dur >= 8 else 2
            # 热身/冷身按余量平摊，但设上限：间歇课的热身不该比主组还长
            warm = min(15, max(5, (minutes - (n * (dur + rec))) // 2))
            cool = min(10, max(5, (minutes - (n * (dur + rec)) - warm) // 2))
            steps.append(_step(1, "warmup", "time", warm * 60, ("hr", WARM_HR),
                               "热身 · 心率不超 150"))
            steps.append(_repeat(
                2, n,
                [_step(0, "interval", "time", dur * 60, ("pace", pace_target(p)),
                       "%d 分钟 @ %s 区 · 配速 %s · 心率超 %d 立刻降"
                       % (dur, zk, pace_span(p), z[1])),
                 _step(0, "recovery", "time", rec * 60, ("hr", WARM_HR),
                       "恢复慢跑 · 心率回到 150 以下再开下一组")]))
            steps.append(_step(3, "cooldown", "time", cool * 60, ("hr", WARM_HR),
                               "冷身 · 心率不超 150"))
        else:
            steps.append(_step(1, "other", "distance", km * 1000,
                               ("pace", pace_target(_e_pace(phase))),
                               info.get("note") or ""))

    elif kind == "hill":
        m = STAIR_RE.search(info.get("abbr") or "")
        n = int(m.group(1)) if m else 2
        road = max(2.0, min(5.0, km if km > 0 else 3.0))
        warm = round(road / 2.0, 1)
        cool = round(road - warm, 1)
        steps.append(_step(1, "warmup", "distance", warm * 1000, ("hr", WARM_HR),
                           f"慢跑 {warm} km 到楼下 · 心率不超 150"))
        steps.append(_repeat(
            2, n,
            [_step(0, "interval", "time", 240, ("hr", STAIR_HR), "上楼跑 / 快走"),
             _step(0, "recovery", "time", 120, ("hr", WARM_HR),
                   "下楼走 + 楼下走动")]))
        steps.append(_step(3, "cooldown", "distance", cool * 1000, ("hr", WARM_HR),
                           f"冷身慢跑 {cool} km 回程"))

    elif kind == "long":
        steps.append(_step(1, "other", "time", minutes * 60, ("hr", LONG_HR),
                           (f"累计爬升 {vert} m · " if vert else "")
                           + "进山按时间跑 · 山里配速无效 · 心率 135–160 · 不追配速"))

    elif kind == "race":
        # 路跑比赛用配速目标监督；越野赛山里配速无意义，仍用心率。
        rp = RACE_PACE_SEC.get(info.get("week"))
        if rp:
            steps.append(_step(1, "other", "distance", km * 1000,
                               ("pace", pace_target(rp)),
                               "比赛配速 %s｜前段压住，过半再决定加不放｜%s"
                               % (pace_span(rp), info.get("tip") or "比赛日")))
        else:
            steps.append(_step(1, "other", "distance", km * 1000,
                               ("hr", RACE_HR),
                               info.get("tip") or "比赛日"))

    else:  # easy / b2b
        if km <= 0:
            return []
        if kind == "b2b" or vert > 0:
            # 带爬升的周日背靠背也在山里：心率监督
            steps.append(_step(1, "other", "distance", km * 1000, ("hr", LONG_HR),
                               info.get("note") or "心率 135–160 · 山里配速无效"))
        else:
            p = _e_pace(phase)
            steps.append(_step(1, "other", "distance", km * 1000, ("pace", pace_target(p)),
                               "配速 %s · 心率 ≤166 优先 · 超了就再慢"
                               % pace_span(p)))

    if not steps:
        return []
    desc = " ｜ ".join(b for b in desc_bits if b).strip(" ｜")
    return [_workout(f"{name0} {info['abbr']}", desc + tail, SPORT_RUN,
                     [_seg(SPORT_RUN, steps)])]


def create(client, payload: dict) -> dict | None:
    """创建课程，返回接口回包（含 workoutId）。"""
    return client.post("/workout-service/workout", payload)


def schedule(client, workout_id: int | str, date: str) -> Any:
    """把课程排到日历上的某一天。"""
    return client.post(f"/workout-service/schedule/{workout_id}", {"date": date})


def list_mine(client) -> list[dict]:
    """列出 Connect 账号里本计划推送的课程（课名以 W<周数> 开头，如「W15 周二 …」）。

    官方计划 / 手动创建的其他课程不受影响。
    """
    out = []
    start = 0
    while True:
        page = client.get("/workout-service/workouts",
                          {"start": start, "limit": 100}) or []
        if isinstance(page, dict):
            page = page.get("workoutList") or []
        if not page:
            break
        for w in page:
            if OLD_NAME_RE.match(w.get("workoutName") or ""):
                out.append({"workoutId": w["workoutId"],
                            "workoutName": w.get("workoutName")})
        if len(page) < 100:
            break
        start += 100
    return out


def delete_old(client, *, dry: bool = False) -> tuple[int, int]:
    """删除之前推送的旧课程，返回 (成功数, 失败数)。"""
    olds = list_mine(client)
    if dry:
        for w in olds:
            print("  将删除 %s  #%s" % (w["workoutName"], w["workoutId"]))
        return len(olds), 0
    ok = bad = 0
    for w in olds:
        if client.delete("/workout-service/workout/%s" % w["workoutId"]):
            ok += 1
        else:
            bad += 1
    return ok, bad


def push_day(client, info: dict, date: str, *, dry: bool = False) -> dict:
    """建课 + 排程（一天可能多节）。dry=True 时只返回待提交的 JSON，不联网。"""
    payloads = build_all(info)
    if not payloads:
        return {"date": date, "skipped": "休息日 / 无跑量", "name": None}
    out: dict = {"date": date, "name": " + ".join(p["workoutName"] for p in payloads),
                 "payload": payloads[0], "payloads": payloads}
    if dry:
        return out
    ids, errs, sched = [], [], True
    for p in payloads:
        created = create(client, p)
        if not created or not created.get("workoutId"):
            errs.append("创建失败[%s]：%s" % (p["workoutName"],
                        (getattr(client, "last_errors", [])[-1:] or ["未知错误"])[0]))
            sched = False
            continue
        ids.append(created["workoutId"])
        if not schedule(client, created["workoutId"], date):
            sched = False
    out["workoutId"] = ids
    out["scheduled"] = sched and not errs
    if errs:
        out["error"] = "；".join(errs)
    return out


def push_range(client, start: dt.date, end: dt.date, *, dry: bool = False,
               skip_rest: bool = True, replace: bool = False) -> list[dict]:
    """把区间内每天（不含休息日）推成课程并排程。replace=True 先清理旧课程。"""
    from . import raceplan

    if replace and not dry:
        ok, bad = delete_old(client)
        print("已清理旧课程 %d 节%s" % (ok, ("，失败 %d" % bad) if bad else ""))
    elif replace and dry:
        try:
            ok, _ = delete_old(client, dry=True)
        except Exception:
            ok = 0
        if ok:
            print("演练：将先清理 %d 节旧课程再推送" % ok)

    out = []
    d = start
    while d <= end:
        info = raceplan.day_info(d)
        if info and (not skip_rest or not info.get("is_rest")):
            out.append(push_day(client, info, d.isoformat(), dry=dry))
        d += dt.timedelta(days=1)
    return out
