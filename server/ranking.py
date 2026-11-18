"""周成绩：六件衫 + 赛季分 + 并列名次。

这里的函数全是不碰数据库的纯函数，输入是「每人这一周 / 上一周的数字」，
输出衫、分、名次。理由很简单：这套规则以后会被反复调（换权重、加维度、
改并列口径），能被单测钉住的只有纯函数。

口径来源是 docs/用户组设计.md 的 11.5 / 11.6，两处差异在这里说清楚：

1. **红衫从「恢复王」换成「计划完成王」**。恢复要睡眠 / HRV，服务端没有，
   也不该有——组页页脚承诺的是「只上传每日里程合计」。`plan_done` 我们本来
   就在收，而且它是三态（完成 / 有课没跑 / 没计划），休息日不会被算成失败，
   比恢复分更能说明「这个队是不是在一起练」。
2. **让分赛与预言那 10 分先空着**。它们的分数是往总分上加的（+2 / +3），
   得等分的基数定死再加。所以现在的满分是 90，不是 100；页面显示时不要
   写成「78/100」，会让人以为自己被扣了 22 分。
"""
from __future__ import annotations

# ---------------------------------------------------------------- 权重
# 里程 30 + 出勤 20 + 爬升 15 + 进步 15 + 连续 10 = 90。
# 剩下的 10 留给让分赛与预言（还没做），它们按「加分」而不是「权重」进总分。
W_KM = 30.0
W_DAYS = 20.0
W_ELEV = 15.0
W_PROGRESS = 15.0
W_STREAK = 10.0
BASE_MAX = W_KM + W_DAYS + W_ELEV + W_PROGRESS + W_STREAK      # 90

# 白衫的参与门槛：上周不到 10 km 不参评。上周跑了 1 km、这周跑 5 km
# 是 400% 的进步，但那是刚开始跑，不该把练了一整年的人挤掉。
PROGRESS_MIN_PREV_KM = 10.0

# 单次爬升与爬升密度上限（防刷）。日报表是日汇总，没有单次数据，
# 所以按「爬升 / 里程 ≤ 200 m/km」在入口处截断，比事后抓作弊省事。
ELEV_PER_KM_MAX = 200.0

# 六件衫。color 是页面上的小球色，用十六进制字符串，前端直接当 CSS 用。
SHIRTS = [
    {"id": "yellow", "name": "黄衫", "title": "里程王", "field": "km",
     "color": "#E8C43A"},
    {"id": "green", "name": "绿衫", "title": "出勤王", "field": "days",
     "color": "#3FA76A"},
    {"id": "polka", "name": "圆点衫", "title": "爬升王", "field": "elev",
     "color": "#D8DEE9"},
    {"id": "white", "name": "白衫", "title": "进步王", "field": "progress",
     "color": "#F2F5FA"},
    {"id": "red", "name": "红衫", "title": "计划完成王", "field": "plan_rate",
     "color": "#D9534F"},
    {"id": "blue", "name": "蓝衫", "title": "连续王", "field": "streak",
     "color": "#4C8DF6"},
]


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def streak_days(rows: list[dict]) -> int:
    """一段日期里的最长连续天数。

    `rows` 是按日期升序的日报表（date / acts / plan_done）。
    「算连续」的日：当天有运动，或者当天按计划完成（`plan_done` 为真）——
    后者覆盖了「今天课表就是休息，我照做了」这种情况。
    既没运动、又没有计划的空档**算断**：服务端分不出「计划休息」和
    「躺着」，把空档一律当休息，那这件的冠军永远是懒得最久的人。
    """
    best = cur = 0
    prev = None
    import datetime as dt

    for r in rows:
        try:
            d = dt.date.fromisoformat(str(r.get("date") or "")[:10])
        except ValueError:
            continue
        ok = int(_f(r.get("acts"))) > 0 or bool(r.get("plan_done"))
        if prev is not None and (d - prev).days == 1 and ok:
            cur += 1
        else:
            cur = 1 if ok else 0
        best = max(best, cur)
        prev = d
    return best


def metrics(rows: list[dict], prev_rows: list[dict]) -> dict:
    """一周的原始指标。rows / prev_rows 都是按日期升序的日报表。"""
    km = sum(_f(r.get("distance_m")) for r in rows) / 1000.0
    elev = sum(_f(r.get("elevation_m")) for r in rows)
    days = sum(1 for r in rows if int(_f(r.get("acts"))) > 0)
    planned = [r for r in rows if r.get("plan_done") is not None]
    done = sum(1 for r in planned if r.get("plan_done"))
    prev_km = sum(_f(r.get("distance_m")) for r in prev_rows) / 1000.0
    # 进步：上周不到门槛就 None（不参评），而不是 0（等于垫底）。
    # 这两种在页面上差很多：前者没份，后者是「你很烂」。
    progress = None
    if prev_km >= PROGRESS_MIN_PREV_KM:
        progress = round((km - prev_km) / prev_km, 4)
    return {
        "km": round(km, 1),
        "elev": int(round(elev)),
        "days": days,
        "streak": streak_days(rows),
        "plan_rate": (round(done / len(planned), 3) if planned else None),
        "plan_days": len(planned),
        "prev_km": round(prev_km, 1),
        "progress": progress,
    }


def _maxes(all_m: list[dict]) -> dict[str, float]:
    def top(field):
        vals = [_f(m.get(field)) for m in all_m if m.get(field) is not None]
        return max(vals) if vals else 0.0

    return {"km": top("km"), "days": top("days"), "elev": top("elev"),
            "streak": top("streak"), "progress": top("progress")}


def score(m: dict, maxes: dict) -> float:
    """0–90 的百分制。每个维度按「组内最高」归一，再乘权重。

    为什么不按绝对量给分（比如 30 km 满分）：一个 5 人的小队和一个 30 人的
    大组，绝对阈值会让小队的分数永远难看；而赛季分本来只用来看组内排序。
    归一化的副作用是「全组都没跑步时没人得分」——这正是对的，不该有分。
    """
    def part(field: str, weight: float, val) -> float:
        top = _f(maxes.get(field))
        if top <= 0 or val is None:
            return 0.0
        return max(0.0, min(1.0, _f(val) / top)) * weight

    s = (part("km", W_KM, m.get("km"))
         + part("days", W_DAYS, m.get("days"))
         + part("elev", W_ELEV, m.get("elev"))
         + part("streak", W_STREAK, m.get("streak"))
         + part("progress", W_PROGRESS, m.get("progress")))
    return round(s, 1)


def shirts(all_m: dict[int, dict]) -> dict[str, list[int]]:
    """每件衫的得主（并列都发，可能多个）。

    值为 0 / None 时不发：全组都没爬升，就不该有人穿圆点衫。
    进步那件额外要求「本周确实涨了」——只参评而不涨（progress ≤ 0）
    不该拿进步王，那是说反话。
    """
    out: dict[str, list[int]] = {}
    for s in SHIRTS:
        field = s["field"]
        cand = {u: m.get(field) for u, m in all_m.items() if m.get(field) is not None}
        if field == "progress":
            cand = {u: v for u, v in cand.items() if _f(v) > 0}
        if not cand:
            out[s["id"]] = []
            continue
        top = max(_f(v) for v in cand.values())
        if top <= 0:
            out[s["id"]] = []
            continue
        # 比较用四舍五入后的值：0.3333 与 0.33333 不该因为浮点尾数算成不同名次
        out[s["id"]] = sorted(u for u, v in cand.items()
                              if round(_f(v), 3) == round(top, 3))
    return out


def rank_with_ties(items: list[dict], key: str = "score") -> None:
    """就地写 `rank`：同分同名次，后续名次顺延（95、95、70 → 1、1、3）。

    这是文档 11.6 的原文。用「比较上一个不同分值的名次」实现，
    而不是 enumerate，否则并列第二会变成第三。
    """
    order = sorted(items, key=lambda x: (-_f(x.get(key)), str(x.get("alias") or "")))
    last_val = None
    last_rank = 0
    for i, it in enumerate(order, 1):
        v = round(_f(it.get(key)), 1)
        if last_val is not None and v == last_val:
            it["rank"] = last_rank
        else:
            it["rank"] = i
            last_rank = i
            last_val = v


def title_of(rank: int, total: int, weeks_done: int, all_active: bool) -> str:
    """称号。取最高的那个，不叠加。

    文档：前 10% 远征队长、前 30% 先锋、完成 3 周 行者、全员 同行者。
    「全员同行者」放最后判断——它描述的是整队，不是个人名次；
    但如果每个人都够格，那每个人都配得上这句。
    """
    if total <= 0:
        return ""
    # 「前 10%」要按人头上取整：5 个人时 10% 是半个人，四舍五入会把第 1 名
    # 也挡在门外（rank/total 落在 0.2）。所以用 ceil，至少留 1 个名额。
    import math

    top10 = max(1, math.ceil(total * 0.10))
    top30 = max(1, math.ceil(total * 0.30))
    if all_active and weeks_done >= 3:
        return "同行者"
    if rank <= top10:
        return "远征队长"
    if rank <= top30:
        return "先锋"
    if weeks_done >= 3:
        return "行者"
    return ""


def build(rows_by_user: dict[int, list[dict]],
          prev_by_user: dict[int, list[dict]],
          weeks_by_user: dict[int, int] | None = None,
          aliases: dict[int, str] | None = None) -> dict:
    """把一周的原始日报表算成「每人一行 + 衫 + 名次」。

    `rows_by_user` 的 key 是 user_id，值是这一周的日报表；`prev_by_user`
    是上一周（算进步用）。缺谁就是没数据，不补 0 行——0 km 和「没上报」
    在页面上必须区分开。
    """
    aliases = aliases or {}
    weeks_by_user = weeks_by_user or {}
    all_m: dict[int, dict] = {}
    for uid, rows in rows_by_user.items():
        all_m[uid] = metrics(rows, prev_by_user.get(uid) or [])

    maxes = _maxes(list(all_m.values()))
    shirt_win = shirts(all_m)

    people: list[dict] = []
    for uid, m in all_m.items():
        mine = [{"id": s["id"], "name": s["name"], "title": s["title"],
                 "color": s["color"]}
                for s in SHIRTS if uid in shirt_win.get(s["id"], [])]
        d = dict(m)
        d.update({"user_id": uid, "alias": aliases.get(uid, ""),
                  "score": score(m, maxes), "shirts": mine})
        people.append(d)

    rank_with_ties(people)

    active = sum(1 for p in people if (p.get("km") or 0) > 0 or (p.get("days") or 0) > 0)
    all_active = bool(people) and active == len(people)
    for p in people:
        p["title"] = title_of(int(p["rank"]), len(people),
                              int(weeks_by_user.get(int(p["user_id"]), 1)), all_active)

    # 衫的得主也按名字列出来：组页那一条「黄衫 阿狗」要用
    by_shirt = []
    for s in SHIRTS:
        uids = shirt_win.get(s["id"]) or []
        by_shirt.append({"id": s["id"], "name": s["name"], "title": s["title"],
                         "color": s["color"],
                         "winners": [{"user_id": u, "alias": aliases.get(u, "")}
                                     for u in uids]})

    return {"people": people, "shirts": by_shirt,
            "max": {"km": round(maxes["km"], 1), "days": int(maxes["days"]),
                    "elev": int(maxes["elev"]), "streak": int(maxes["streak"]),
                    "progress": maxes["progress"]},
            "base_max": BASE_MAX}
