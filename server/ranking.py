"""周成绩：六件衫 + 赛季分 + 并列名次。

这里的函数全是不碰数据库的纯函数，输入是「每人这一周 / 上一周的数字」，
输出衫、分、名次。理由很简单：这套规则以后会被反复调（换权重、加维度、
改并列口径），能被单测钉住的只有纯函数。

口径来源是 docs/用户组设计.md 的 11.5 / 11.6，两处差异在这里说清楚：

1. **红衫从「恢复王」换成「计划完成王」**。恢复要睡眠 / HRV，服务端没有，
   也不该有——组页页脚承诺的是「只上传每日里程合计」。`plan_done` 我们本来
   就在收，而且它是三态（完成 / 有课没跑 / 没计划），休息日不会被算成失败，
   比恢复分更能说明「这个队是不是在一起练」。
2. **让分赛与预言是加分项，不是权重**。基础分满分 90，加分最多 +10，合计 100。
   加分按事件给（预言命中 +2 / 被预测却完成 +3），不由权重归一——因为
   「这周猜对了几个人」本来就跟里程不是一个量纲。页面显示「62 分」，
   不要写成 `62/100`，否则看起来像被扣了 38 分。
"""
from __future__ import annotations

# ---------------------------------------------------------------- 权重
# 里程 30 + 出勤 20 + 爬升 15 + 进步 15 + 连续 10 = 90。
# 剩下的 10 是让分赛与预言的加分（已实现，见 BONUS_MAX / pair_results /
# prediction_result）：按「加分」而不是「权重」进总分。
W_KM = 30.0
W_DAYS = 20.0
W_ELEV = 15.0
W_PROGRESS = 15.0
W_STREAK = 10.0
BASE_MAX = W_KM + W_DAYS + W_ELEV + W_PROGRESS + W_STREAK      # 90
# 让分赛与预言的加分上限。超出就不再加——否则「猜得准」会盖过「跑得多」，
# 而这是个跑步小组，不是竞猜群。
BONUS_MAX = 10.0

# 探索徽章的达标线：一周新踩 5 格算「真的换了地方跑」。
# 不排名、不占衫——跑步的人不该因为住在路网密的老城就吃亏。
EXPLORE_MIN_CELLS = 5

# 白衫的参与门槛：基数（前 7 天 / 上周）不到 10 km 不参评。基数只有 1 km、
# 现在跑 5 km 是 400% 的进步，那是刚开始跑，不该把练了一整年的人挤掉。
PROGRESS_MIN_PREV_KM = 10.0

# 进步的对照窗口：滚动 7 天对 7 天，**不按日历周**。
# 为什么改：拿「本周至今」比「上周整周」，周一只过了 1 天、周四过了 4 天，
# 分母永远是 7 天，全组都显示 -40% ~ -85%（实测线上 -62%），白衫基本发不出去，
# 15 分的进步项也恒为 0。改成两个等长窗口之后，任何一天算出来都可比。
PROGRESS_WINDOW_DAYS = 7

# 单次爬升与爬升密度上限（防刷）。日报表是日汇总，没有单次数据，
# 所以按「爬升 / 里程 ≤ 200 m/km」在入口处截断，比事后抓作弊省事。
ELEV_PER_KM_MAX = 200.0

# 六件衫。color 是页面上的小球色，用十六进制字符串，前端直接当 CSS 用。
# `hint` 是「这件衫在比什么」的一句话——颜色和名字（黄衫/绿衫）本身不表意，
# 页面上只写「黄衫 阿狗」的话，新来的人根本不知道那是里程榜。
# `pattern` 是给「光靠颜色分不出来」的那一两件用的：圆点衫按公路车赛的规矩
# 是白底红点，纯色画出来和白衫一模一样（#D8DEE9 vs #F2F5FA，都是白），
# 所以让前端画波点，而不是继续调色号——调浅调深都还是两个白点。
SHIRTS = [
    {"id": "yellow", "name": "黄衫", "title": "里程王", "field": "km",
     "color": "#E8C43A", "unit": "km", "hint": "本周跑步的总里程"},
    {"id": "green", "name": "绿衫", "title": "出勤王", "field": "days",
     "color": "#3FA76A", "unit": "天", "hint": "本周有跑步的天数（一天练两次算一天）"},
    {"id": "polka", "name": "圆点衫", "title": "爬升王", "field": "elev",
     "color": "#D8DEE9", "unit": "m", "hint": "本周累计爬升的米数",
     "pattern": "dots"},
    {"id": "white", "name": "白衫", "title": "进步王", "field": "progress",
     "color": "#F2F5FA", "unit": "%", "hint": "最近 7 天比之前 7 天多跑的比例"},
    {"id": "red", "name": "红衫", "title": "计划完成王", "field": "plan_rate",
     "color": "#D9534F", "unit": "%", "hint": "有课的日子里完成的比例"},
    {"id": "blue", "name": "蓝衫", "title": "连续王", "field": "streak",
     "color": "#4C8DF6", "unit": "天", "hint": "本周最长连续出勤的天数"},
]


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _fmt_val(field: str, v) -> str:
    """衫榜上的成绩写法。单位跟着口径走，别让客户端自己猜。"""
    if v is None:
        return "—"
    f = _f(v)
    if field == "km":
        return f"{f:.1f} km"
    if field == "elev":
        return f"{int(round(f))} m"
    if field in ("days", "streak"):
        return f"{int(round(f))} 天"
    if field == "progress":
        # 进步可以是负的：榜上要看得出谁在退步，不藏
        return f"{f * 100:+.0f}%"
    if field == "plan_rate":
        return f"{f * 100:.0f}%"
    return f"{f:g}"


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


def metrics(rows: list[dict], prev_rows: list[dict],
            roll: tuple[float, float] | None = None) -> dict:
    """一周的原始指标。rows / prev_rows 都是按日期升序的日报表。

    `roll = (最近 7 天 km, 前 7 天 km)` 专门给「进步」用：不传时退回
    「本周 vs 上周」，但那个口径在周内是失真的（见 PROGRESS_WINDOW_DAYS）。
    """
    km = sum(_f(r.get("distance_m")) for r in rows) / 1000.0
    elev = sum(_f(r.get("elevation_m")) for r in rows)
    days = sum(1 for r in rows if int(_f(r.get("acts"))) > 0)
    # 探索：客户端算好的「每天新踩几格」，这里只求和。
    # 服务端拿不到轨迹，也不需要——格子 ID 从来不上传。
    explore = int(round(sum(_f(r.get("explorer")) for r in rows)))
    planned = [r for r in rows if r.get("plan_done") is not None]
    done = sum(1 for r in planned if r.get("plan_done"))
    prev_km = sum(_f(r.get("distance_m")) for r in prev_rows) / 1000.0
    # 进步：上周不到门槛就 None（不参评），而不是 0（等于垫底）。
    # 这两种在页面上差很多：前者没份，后者是「你很烂」。
    if roll is None:
        cur_km, base_km = km, prev_km
    else:
        cur_km, base_km = float(roll[0]), float(roll[1])
    progress = None
    if base_km >= PROGRESS_MIN_PREV_KM:
        progress = round((cur_km - base_km) / base_km, 4)
    return {
        "km": round(km, 1),
        "elev": int(round(elev)),
        "days": days,
        "streak": streak_days(rows),
        "explorer": explore,
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
            "streak": top("streak"), "progress": top("progress"),
            "explorer": top("explorer")}


def score_parts(m: dict, maxes: dict, bonus: float = 0.0) -> dict:
    """把总分拆成五项 + 加分，页面要拿它解释「这 50.6 分是怎么来的」。

    用户看到的是一个「50.6」，没有任何办法知道里面跑量占多少、进步占多少；
    只写「跑量 · 出勤 · 爬升 · 进步 · 连跑」五个词，等于让他自己猜权重。
    这里把每一项的得分按同一套口径算出来，页面直接列。
    """
    def part(field: str, weight: float, val) -> float:
        top = _f(maxes.get(field))
        if top <= 0 or val is None:
            return 0.0
        return max(0.0, min(1.0, _f(val) / top)) * weight

    out = {}
    for field, weight in (("km", W_KM), ("days", W_DAYS), ("elev", W_ELEV),
                          ("progress", W_PROGRESS), ("streak", W_STREAK)):
        out[field] = round(part(field, weight, m.get(field)), 1)
    out["bonus"] = round(max(0.0, min(BONUS_MAX, _f(bonus))), 1)
    return out


def score(m: dict, maxes: dict, bonus: float = 0.0) -> float:
    """0–100 的百分制：基础 90 + 让分/预言加分（最多 10）。

    为什么不按绝对量给分（比如 30 km 满分）：一个 5 人的小队和一个 30 人的
    大组，绝对阈值会让小队的分数永远难看；而赛季分本来只用来看组内排序。
    归一化的副作用是「全组都没跑步时没人得分」——这正是对的，不该有分。

    加分项单独加在这里，不并进权重：命中预言和跑量不是一个量纲，
    混进权重会让人算不明白自己为什么掉分。
    """
    s = sum(score_parts(m, maxes, bonus).values())
    return round(min(BASE_MAX + BONUS_MAX, s), 1)


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


# 称号的说明。客户端不写死文案：门槛是服务端算的（ceil 取整、3 周、全员
# 活跃），页面上一句「前 10%」如果和实际判定不一致，用户只会觉得是 bug。
TITLES = [
    {"name": "远征队长", "hint": "本周综合分在组里前 10%"},
    {"name": "先锋", "hint": "本周综合分在组里前 30%"},
    {"name": "行者", "hint": "在这个组已经参与满 3 周"},
    {"name": "同行者", "hint": "全组这周都动了，且你已参与满 3 周"},
]


# ---------------------------------------------------------------- 让分赛
# 目标低于 5 km 不参加让分：一周 3 km 的「目标」本来就不是训练计划，
# 拿它当让分基准只会让配对变成抽奖。
PAIR_MIN_KM = 5.0
# 两人自报目标差不到 20% 就不让分。设计原文是「大神目标 = 自报 + 小白
# 目标 × 50%」，但那只在强弱分明时成立：两个水平相当的人（100 km vs 95 km）
# 配成一对会被算出 147.5 km 的目标，等于把强的那个人直接判死。所以差距小
# 的一对标成 even——只结伴、不让分。
PAIR_GAP_MIN = 0.20
BIG_HANDICAP = 0.50      # 大神让分 = 小白自报目标 × 50%
BIG_CAP = 1.60           # 让分后不超过自报 × 1.6（和文档里大组版的上限一致）


def pairing(goals: dict[int, float]) -> list[dict]:
    """按自报目标高低配对：第 1 名配最后 1 名，中间两两。

    奇数人时**中间那位轮空**（solo），不硬拉一个人来凑对——把中游的人
    塞进一对强弱悬殊的组合里，对谁都不公平。
    """
    cand = sorted(((u, _f(v)) for u, v in goals.items() if _f(v) >= PAIR_MIN_KM),
                  key=lambda x: (-x[1], x[0]))
    out: list[dict] = []
    i, j = 0, len(cand) - 1
    while i < j:
        big_uid, big_km = cand[i]
        small_uid, small_km = cand[j]
        gap = (big_km - small_km) / small_km if small_km > 0 else 0.0
        even = gap < PAIR_GAP_MIN
        target_big = big_km if even else min(
            big_km * BIG_CAP, big_km + small_km * BIG_HANDICAP)
        out.append({
            "slot": len(out) + 1,
            "small_uid": small_uid, "small_km": round(small_km, 1),
            "big_uid": big_uid, "big_km": round(big_km, 1),
            "small_target": round(small_km, 1),
            "big_target": round(target_big, 1),
            "gap": round(gap, 3),
            "even": even,
        })
        i += 1
        j -= 1
    if i == j:
        uid, km = cand[i]
        out.append({"slot": len(out) + 1, "solo_uid": uid, "solo_km": round(km, 1)})
    return out


def pair_results(pairs: list[dict], actual_km: dict[int, float]) -> list[dict]:
    """每对本周打成什么样。

    `double` 两人都完成（双人徽章）、`upset` 小白完成而大神没完成（逆袭）、
    `big_only` 大神完成小白没完成（让分没让住，但也没翻车）、
    `none` 都没完成。`even` 对（本来就没让分）只判 double / split / none。
    """
    out = []
    for p in pairs:
        if "solo_uid" in p:
            out.append(dict(p, result="solo"))
            continue
        s = _f(actual_km.get(p["small_uid"]))
        b = _f(actual_km.get(p["big_uid"]))
        s_done = s >= _f(p.get("small_target"))
        b_done = b >= _f(p.get("big_target"))
        if s_done and b_done:
            res = "double"
        elif s_done and not b_done:
            res = "upset"
        elif b_done and not s_done:
            res = "big_only"
        else:
            res = "none"
        if p.get("even") and res in ("upset", "big_only"):
            res = "split"
        out.append(dict(p, result=res,
                        small_actual=round(s, 1), big_actual=round(b, 1)))
    return out


# ---------------------------------------------------------------- 预言
PREDICT_HIT = 2.0              # 猜对一个人没完成
PREDICT_UNDERESTIMATED = 3.0   # 被猜会失败、结果完成了（给被猜的人）


def prediction_result(predicts: dict[int, int],
                      actual_km: dict[int, float],
                      goals: dict[int, float]) -> dict:
    """结算本周预言：谁猜中了、谁被打脸、加分多少。

    只认「目标存在且 ≥ PAIR_MIN_KM」的被猜对象——没报目标的人没有
    「完不成」这个说法，把他当靶子等于白送分。

    `actual_km` 里没有这个人按 0 算：服务端只信上报，没上报就是没跑。
    """
    rows: list[dict] = []
    bonus: dict[int, float] = {}
    for voter, target in (predicts or {}).items():
        voter, target = int(voter), int(target)
        tgt = _f(goals.get(target))
        if tgt < PAIR_MIN_KM:
            rows.append({"user_id": voter, "target_uid": target,
                         "result": "unknown"})
            continue
        act = _f(actual_km.get(target))
        if act < tgt:
            rows.append({"user_id": voter, "target_uid": target,
                         "result": "hit", "bonus": PREDICT_HIT})
            bonus[voter] = bonus.get(voter, 0.0) + PREDICT_HIT
        else:
            rows.append({"user_id": voter, "target_uid": target,
                         "result": "wrong", "target_bonus": PREDICT_UNDERESTIMATED})
            bonus[target] = bonus.get(target, 0.0) + PREDICT_UNDERESTIMATED
    # 上限在 score() 里还会再夹一次，这里先夹是为了让返回的明细和实际得分一致
    bonus = {u: round(min(BONUS_MAX, v), 1) for u, v in bonus.items()}
    return {"rows": rows, "bonus": bonus}


def build(rows_by_user: dict[int, list[dict]],
          prev_by_user: dict[int, list[dict]],
          weeks_by_user: dict[int, int] | None = None,
          aliases: dict[int, str] | None = None,
          bonus: dict[int, float] | None = None,
          roll_by_user: dict[int, tuple[float, float]] | None = None) -> dict:
    """把一周的原始日报表算成「每人一行 + 衫 + 名次 + 探索徽章」。

    `rows_by_user` 的 key 是 user_id，值是这一周的日报表；`prev_by_user`
    是上一周（算进步用）。缺谁就是没数据，不补 0 行——0 km 和「没上报」
    在页面上必须区分开。

    `bonus` 是让分赛与预言的加分（user_id → 分），不传就是没这回事。
    """
    aliases = aliases or {}
    weeks_by_user = weeks_by_user or {}
    bonus = bonus or {}
    all_m: dict[int, dict] = {}
    roll_by_user = roll_by_user or {}
    for uid, rows in rows_by_user.items():
        all_m[uid] = metrics(rows, prev_by_user.get(uid) or [],
                             roll=roll_by_user.get(uid))

    maxes = _maxes(list(all_m.values()))
    shirt_win = shirts(all_m)

    people: list[dict] = []
    for uid, m in all_m.items():
        mine = [{"id": s["id"], "name": s["name"], "title": s["title"],
                 "color": s["color"]}
                for s in SHIRTS if uid in shirt_win.get(s["id"], [])]
        d = dict(m)
        b = max(0.0, min(BONUS_MAX, _f(bonus.get(uid))))
        d.update({"user_id": uid, "alias": aliases.get(uid, ""),
                  "score": score(m, maxes, b), "bonus": round(b, 1),
                  # 分项分数：页面上要能点开看「这 50.6 分是怎么来的」，
                  # 只给一个总数等于让用户自己猜权重。
                  "parts": score_parts(m, maxes, b),
                  "weeks": int(weeks_by_user.get(uid, 1)),
                  "shirts": mine,
                  # 探索徽章不占衫：达标就发，不算名次
                  "explorer_badge": int(m.get("explorer") or 0) >= EXPLORE_MIN_CELLS})
        people.append(d)

    rank_with_ties(people)

    active = sum(1 for p in people if (p.get("km") or 0) > 0 or (p.get("days") or 0) > 0)
    all_active = bool(people) and active == len(people)
    for p in people:
        p["title"] = title_of(int(p["rank"]), len(people),
                              int(weeks_by_user.get(int(p["user_id"]), 1)), all_active)

    # 衫的得主也按名字列出来：组页那一条「黄衫 阿狗」要用。
    # 顺带把「这件衫在比什么」和「前 3 名 + 成绩」一起给出去：客户端只拿
    # id 和 winners 的话，页面上就只剩一排彩色小点，没人知道黄衫是里程榜。
    by_shirt = []
    for s in SHIRTS:
        uids = shirt_win.get(s["id"]) or []
        field = s["field"]
        ranked = sorted(
            ((u, m.get(field)) for u, m in all_m.items() if m.get(field) is not None),
            key=lambda x: -_f(x[1]))
        full = [{"user_id": u, "alias": aliases.get(u, ""),
                 "text": _fmt_val(field, v)}
                for u, v in ranked]
        top = full[:3]
        by_shirt.append({"id": s["id"], "name": s["name"], "title": s["title"],
                         "color": s["color"], "field": field,
                         "unit": s.get("unit", ""), "hint": s.get("hint", ""),
                         # 完整榜：组最多 20 人，客户端直接全列出来，
                         # 不用再点「看全部」——荣誉页的价值就是一次看完
                         "rank": full,
                         # 圆点衫靠波点区分，纯色和白衫撞车
                         "pattern": s.get("pattern", ""),
                         "winners": [{"user_id": u, "alias": aliases.get(u, "")}
                                     for u in uids],
                         "top": top})

    return {"people": people, "shirts": by_shirt,
            "max": {"km": round(maxes["km"], 1), "days": int(maxes["days"]),
                    "elev": int(maxes["elev"]), "streak": int(maxes["streak"]),
                    "progress": maxes["progress"],
                    "explorer": int(maxes.get("explorer") or 0)},
            # 权重和称号说明一起下发：页面要解释分数和称号，写死一份文案
            # 迟早和服务端算的对不上（改权重、改门槛的那天）。
            "weights": {"km": W_KM, "days": W_DAYS, "elev": W_ELEV,
                        "progress": W_PROGRESS, "streak": W_STREAK,
                        "bonus": BONUS_MAX},
            "titles": TITLES,
            "all_active": all_active,
            "explore": {"min_cells": EXPLORE_MIN_CELLS,
                        "badges": [{"user_id": int(p["user_id"]),
                                    "alias": p.get("alias") or "",
                                    "cells": int(p.get("explorer") or 0)}
                                   for p in people if p.get("explorer_badge")]},
            "base_max": BASE_MAX, "bonus_max": BONUS_MAX}
