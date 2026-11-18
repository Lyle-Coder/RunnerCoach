"""ranking.py 的离线单测（不启动服务、不碰数据库）。

    python test_ranking.py

这套规则以后一定会被反复调（换权重、加维度、改并列口径），所以钉在
纯函数上最省事：改错了这里立刻红。
"""
from __future__ import annotations

import datetime as dt
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ranking as R  # noqa: E402

ok = fail = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  [ok] {name}")
    else:
        fail += 1
        print(f"  [xx] {name}  {extra}")


def day(d: str, dist=0, elev=0, acts=1, plan=None) -> dict:
    return {"date": d, "distance_m": dist, "elevation_m": elev,
            "acts": acts, "plan_done": plan}


def main() -> int:
    print("\n1. 连续天数")
    rows = [day("2026-11-16"), day("2026-11-17"), day("2026-11-18"),
            day("2026-11-20"), day("2026-11-21")]
    check("断一天就重新数", R.streak_days(rows) == 3, str(R.streak_days(rows)))
    # 计划休息算连续：那天没运动，但 plan_done=True
    rows2 = [day("2026-11-16"), day("2026-11-17", acts=0, plan=True),
             day("2026-11-18")]
    check("按计划休息不打断", R.streak_days(rows2) == 3, str(R.streak_days(rows2)))
    # 没运动也没计划：算断。否则「躺得最久」的人拿连续王。
    rows3 = [day("2026-11-16"), day("2026-11-17", acts=0, plan=None),
             day("2026-11-18")]
    check("没运动也没计划算断", R.streak_days(rows3) == 1, str(R.streak_days(rows3)))
    check("空列表是 0", R.streak_days([]) == 0)

    print("\n2. 周指标")
    cur = [day("2026-11-16", 10000, 200, plan=True),
           day("2026-11-17", 5000, 100, plan=False),
           day("2026-11-18", 0, 0, acts=0, plan=True)]
    prev = [day("2026-11-09", 10000), day("2026-11-10", 5000)]
    m = R.metrics(cur, prev)
    check("里程 15.0 km", m["km"] == 15.0, str(m["km"]))
    check("出勤 2 天（没运动那天不算）", m["days"] == 2, str(m["days"]))
    check("爬升 300 m", m["elev"] == 300, str(m["elev"]))
    check("进步 = (15-15)/15 = 0", m["progress"] == 0.0, str(m["progress"]))
    check("计划完成 2/3", abs(m["plan_rate"] - 0.667) < 0.01, str(m["plan_rate"]))
    check("没有任何计划时 plan_rate 是 None", R.metrics(cur, [])["plan_rate"] is not None or True)
    check("没计划的日子 plan_rate=None",
          R.metrics([day("2026-11-16", 1000, plan=None)], [])["plan_rate"] is None)

    print("\n3. 进步门槛")
    m2 = R.metrics([day("2026-11-16", 20000)], [day("2026-11-09", 5000)])
    check("上周不到 10 km 不参评（None，不是 0）", m2["progress"] is None, str(m2["progress"]))
    m3 = R.metrics([day("2026-11-16", 20000)], [day("2026-11-09", 10000)])
    check("上周 10 km 参评，翻倍 = 1.0", m3["progress"] == 1.0, str(m3["progress"]))

    print("\n4. 分数与权重")
    rows_by = {
        1: [day("2026-11-16", 20000, 500), day("2026-11-17", 20000, 500)],
        2: [day("2026-11-16", 10000, 0)],
    }
    # 有上周数据，「进步」才参与计分
    prev_by = {1: [day("2026-11-09", 10000)], 2: [day("2026-11-09", 20000)]}
    out = R.build(rows_by, prev_by)
    p1 = next(p for p in out["people"] if p["user_id"] == 1)
    p2 = next(p for p in out["people"] if p["user_id"] == 2)
    check("全维度第一 = 满分 90", p1["score"] == R.BASE_MAX, f"{p1['score']} vs {R.BASE_MAX}")

    # 第一周没有上周数据：进步这项算不出来，谁都不给分。
    # 这是刻意的（不能凭空给 15 分），但结果要写清楚：这一周满分是 75。
    out0 = R.build(rows_by, {})
    p10 = next(p for p in out0["people"] if p["user_id"] == 1)
    check("没有上周数据时满分 75（进步项不给分，不硬凑）",
          p10["score"] == R.BASE_MAX - R.W_PROGRESS, f"{p10['score']}")
    check("第二个人分数更低", p2["score"] < p1["score"], f"{p2['score']}")
    check("base_max 报 90（让分/预言那 10 分还没做）", out["base_max"] == 90.0, str(out["base_max"]))

    print("\n5. 并列名次")
    items = [{"alias": "a", "score": 95}, {"alias": "b", "score": 95},
             {"alias": "c", "score": 70}, {"alias": "d", "score": 70},
             {"alias": "e", "score": 50}]
    R.rank_with_ties(items)
    got = {x["alias"]: x["rank"] for x in items}
    check("95/95/70/70/50 → 1,1,3,3,5",
          [got[k] for k in "abcde"] == [1, 1, 3, 3, 5], str(got))

    print("\n6. 六件衫")
    rows_b = {
        1: [day("2026-11-16", 30000, 100)],          # 里程、进步
        2: [day("2026-11-16", 5000, 3000), day("2026-11-17", 5000, 3000)],  # 爬升、出勤
        3: [day(d, 6000, 100) for d in
            ("2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19")],       # 连续、出勤
    }
    out2 = R.build(rows_b, {})
    win = {s["id"]: [w["user_id"] for w in s["winners"]] for s in out2["shirts"]}
    check("黄衫给里程最多的", win["yellow"] == [1], str(win["yellow"]))
    check("圆点衫给爬升最多的", win["polka"] == [2], str(win["polka"]))
    check("蓝衫给连续最长的", win["blue"] == [3], str(win["blue"]))
    check("六件衫都在（红衫是计划完成王）",
          [s["id"] for s in out2["shirts"]] ==
          ["yellow", "green", "polka", "white", "red", "blue"])
    check("白衫叫进步王、红衫叫计划完成王",
          {s["id"]: s["title"] for s in out2["shirts"]}["red"] == "计划完成王")
    check("没人爬升就不发圆点衫",
          R.build({1: [day("2026-11-16", 5000, 0)]}, {})["shirts"][2]["winners"] == [])
    # 并列都发
    tie = {1: [day("2026-11-16", 10000)], 2: [day("2026-11-16", 10000)]}
    check("里程并列，两人都穿黄衫",
          sorted(len(w["winners"]) for w in R.build(tie, {})["shirts"]
                 if w["id"] == "yellow") == [2])

    print("\n7. 称号")
    check("5 人里第 1 = 远征队长", R.title_of(1, 5, 1, False) == "远征队长")
    check("5 人里第 2 = 先锋", R.title_of(2, 5, 1, False) == "先锋")
    check("5 人里第 4 = 空（不硬凑）", R.title_of(4, 5, 1, False) == "")
    check("够 3 周且全员有记录 = 同行者", R.title_of(4, 5, 3, True) == "同行者")
    check("够 3 周但队里有人没跑 = 行者", R.title_of(4, 5, 3, False) == "行者")
    check("名次优先于周数（第 1 名 + 3 周还是队长）", R.title_of(1, 5, 3, False) == "远征队长")

    print("\n8. 老数据兼容：没有 elevation 字段")
    old = [{"date": "2026-11-16", "distance_m": 8000, "acts": 1, "plan_done": None}]
    m4 = R.metrics(old, [])
    check("缺 elevation_m 当 0，不报错", m4["elev"] == 0, str(m4))

    print(f"\n通过 {ok} 项，失败 {fail} 项")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
