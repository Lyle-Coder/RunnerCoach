# -*- coding: utf-8 -*-
"""Bingo 卡：5×5 任务格，连成一条线算一次 Bingo。

设计上只有一句话的余地（docs/用户组设计.md 11.5 只写了「5×5 任务格，连线得分」），
所以这里把能落地的那部分口径写死，免得以后每改一次都要重新吵：

1. **数据只来自日汇总**。任务只用「某天跑了多少、多久、几次、爬升多少、有没按计划、
   新踩几格」。配速、心率、睡眠一概不碰——组页页脚承诺的就是这一条。
2. **一人一张卡，不共享**。同组不同人卡不同（种子含 user_id），但难度构成一样
   （易/中/难各 8 格）。共享一张卡看着更"团队"，实际是大组一天就全点亮、小组
   永远差一半，反而更不公平。
3. **只发荣誉，不进赛季分**。赛季分那 100 是「跑量 + 出勤 + 爬升 + 连续 + 进步
   + 让分预言」算出来的，把 Bingo 塞进去会让「9 条线」比「多跑 30 km」还值钱，
   那不是这个小组的主题。连线数单独展示。
4. **中心格免费**。开局就有一格亮着，才有往下填的动力。
"""
from __future__ import annotations

import random

SIZE = 5
FREE_TEXT = "开卡即得"
LEVEL_EASY, LEVEL_MID, LEVEL_HARD = 1, 2, 3
# 每个难度抽几格。3 × 8 = 24，加中心免费格正好 25。
PER_LEVEL = 8

# 任务池。`fn(m, x)` 里 m 是 ranking.metrics 的结果，x 是本模块算的日级极值
# （见 `_extras`）。level 只是抽卡时的分层标签，不参与判定。
#
# 门槛是照着「一个正常训练周应该完成 10–14 格」定的：第一版写的
# 「出门跑一次 / 单日 3 km / 出勤 2 天」太送分，两天 28 km 就点亮 17 格，
# 连带线都成了必然——那样就没有「我今天特意去补某一格」这件事了。
TASKS = [
    # ---- 易：每周都能撞上
    ("d5", "单日 5km", LEVEL_EASY, lambda m, x: x["day_km"] >= 5),
    ("d6", "单日 6km", LEVEL_EASY, lambda m, x: x["day_km"] >= 6),
    ("days3", "出勤 3 天", LEVEL_EASY, lambda m, x: m["days"] >= 3),
    ("streak2", "连续 2 天", LEVEL_EASY, lambda m, x: m["streak"] >= 2),
    ("w15", "本周 15km", LEVEL_EASY, lambda m, x: m["km"] >= 15),
    ("plan1", "按计划 1 次", LEVEL_EASY, lambda m, x: x["plan_ok"] >= 1),
    ("time15", "累计 90 分", LEVEL_EASY, lambda m, x: x["dur_h"] >= 1.5),
    ("elev150", "本周爬升 150m", LEVEL_EASY, lambda m, x: m["elev"] >= 150),
    ("weekend", "周末跑一次", LEVEL_EASY, lambda m, x: x["weekend"]),

    # ---- 中
    ("d8", "单日 8km", LEVEL_MID, lambda m, x: x["day_km"] >= 8),
    ("d10", "单日 10km", LEVEL_MID, lambda m, x: x["day_km"] >= 10),
    ("days4", "出勤 4 天", LEVEL_MID, lambda m, x: m["days"] >= 4),
    ("streak3", "连续 3 天", LEVEL_MID, lambda m, x: m["streak"] >= 3),
    ("w25", "本周 25km", LEVEL_MID, lambda m, x: m["km"] >= 25),
    ("w35", "本周 35km", LEVEL_MID, lambda m, x: m["km"] >= 35),
    ("plan3", "按计划 3 次", LEVEL_MID, lambda m, x: x["plan_ok"] >= 3),
    ("dayelev300", "单日爬升 300m", LEVEL_MID, lambda m, x: x["day_elev"] >= 300),
    ("explore8", "新踩 8 格", LEVEL_MID, lambda m, x: m["explorer"] >= 8),
    ("twice", "一天跑两次", LEVEL_MID, lambda m, x: x["day_acts"] >= 2),

    # ---- 难：一周里做得到，但要专门安排
    ("d15", "单日 15km", LEVEL_HARD, lambda m, x: x["day_km"] >= 15),
    ("d18", "单日 18km", LEVEL_HARD, lambda m, x: x["day_km"] >= 18),
    ("days5", "出勤 5 天", LEVEL_HARD, lambda m, x: m["days"] >= 5),
    ("streak5", "连续 5 天", LEVEL_HARD, lambda m, x: m["streak"] >= 5),
    ("w50", "本周 50km", LEVEL_HARD, lambda m, x: m["km"] >= 50),
    ("welelev800", "本周爬升 800m", LEVEL_HARD, lambda m, x: m["elev"] >= 800),
    ("dayelev600", "单日爬升 600m", LEVEL_HARD, lambda m, x: x["day_elev"] >= 600),
    ("explore18", "新踩 18 格", LEVEL_HARD, lambda m, x: m["explorer"] >= 18),
    ("time6h", "累计 6 小时", LEVEL_HARD, lambda m, x: x["dur_h"] >= 6),
    ("trail", "山地 12km", LEVEL_HARD,
     lambda m, x: x["day_km"] >= 12 and x["day_elev"] >= 400),
]

BY_ID = {t[0]: t for t in TASKS}
TOTAL_CELLS = SIZE * SIZE
CENTER = TOTAL_CELLS // 2


def _extras(rows: list[dict]) -> dict:
    """日级极值。metrics 只给"一周合计"，而 Bingo 里一半任务问的是"有一天"。"""
    import datetime as dt

    day_km = day_elev = day_acts = 0
    dur = 0.0
    plan_ok = 0
    weekend = False
    for r in rows:
        try:
            km = float(r.get("distance_m") or 0) / 1000.0
            elev = float(r.get("elevation_m") or 0)
            acts = int(r.get("acts") or 0)
            dur += float(r.get("duration_s") or 0)
        except (TypeError, ValueError):
            continue
        day_km = max(day_km, km)
        day_elev = max(day_elev, elev)
        day_acts = max(day_acts, acts)
        if r.get("plan_done"):
            plan_ok += 1
        try:
            wd = dt.date.fromisoformat(str(r.get("date") or "")[:10]).weekday()
        except ValueError:
            wd = -1
        if wd >= 5 and (km > 0 or acts > 0):
            weekend = True
    return {"day_km": day_km, "day_elev": day_elev, "day_acts": day_acts,
            "dur_h": dur / 3600.0, "plan_ok": plan_ok, "weekend": weekend}


def make_card(gid: int, uid: int, week_start: str) -> list[dict]:
    """生成一张卡：易/中/难各抽 8 格 + 中心免费格。

    种子含 gid / uid / 周，所以「同一周同一人」每次算出来一样——不落库也能
    稳定复现；换周自动换卡。不落库的好处是免掉一张 25 行/人/周 的表。
    """
    rnd = random.Random(f"bingo:{gid}:{uid}:{week_start}")
    picked: list[dict] = []
    for lv in (LEVEL_EASY, LEVEL_MID, LEVEL_HARD):
        pool = [t for t in TASKS if t[2] == lv]
        rnd.shuffle(pool)
        for tid, text, level, _fn in pool[:PER_LEVEL]:
            picked.append({"task": tid, "text": text, "level": level})
    rnd.shuffle(picked)
    # 24 格 + 中心。中心插在中间而不是随机位，页面排版才固定
    picked.insert(CENTER, {"task": "free", "text": FREE_TEXT, "level": 0})
    return picked[:TOTAL_CELLS]


def _lines(done: list[bool]) -> list[str]:
    """连成线的格子索引集合名。5 行 + 5 列 + 2 对角。"""
    out = []
    for r in range(SIZE):
        if all(done[r * SIZE + c] for c in range(SIZE)):
            out.append(f"r{r}")
    for c in range(SIZE):
        if all(done[r * SIZE + c] for r in range(SIZE)):
            out.append(f"c{c}")
    if all(done[i * SIZE + i] for i in range(SIZE)):
        out.append("d0")
    if all(done[i * SIZE + (SIZE - 1 - i)] for i in range(SIZE)):
        out.append("d1")
    return out


def evaluate(card: list[dict], rows: list[dict], metrics: dict) -> dict:
    """判每格完成没、连了几条线。

    `metrics` 传 ranking.metrics 的结果（周合计 + 连续天数）；日级极值在这里
    自己算，别让调用方记两套口径。
    """
    x = _extras(rows)
    done: list[bool] = []
    cells = []
    for i, c in enumerate(card):
        tid = c["task"]
        if tid == "free":
            ok = True
        else:
            t = BY_ID.get(tid)
            try:
                ok = bool(t[3](metrics, x)) if t else False
            except Exception:      # noqa: BLE001 - 单个任务判错不该炸掉整张卡
                ok = False
        done.append(ok)
        cells.append({"i": i, "task": tid, "text": c["text"],
                      "level": c["level"], "done": ok})
    # 格数不对就别算线（调用方传了半张卡的时候，按 0 条线处理比抛异常合适）
    solved = _lines(done) if len(done) == TOTAL_CELLS else []
    return {"cells": cells, "lines": solved, "line_count": len(solved),
            "done": sum(1 for d in done if d), "total": len(card)}


def card_for(rows: list[dict], metrics: dict,
             gid: int, uid: int, week_start: str) -> dict:
    """一次拿齐：生成卡 + 判定 + 连线。调用方只要传本周日报表和 metrics。"""
    card = make_card(gid, uid, week_start)
    return evaluate(card, rows, metrics)
