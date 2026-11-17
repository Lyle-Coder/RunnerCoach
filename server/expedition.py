# -*- coding: utf-8 -*-
"""远征：赛季、岔路、支线、试炼、受困救援、结局。

为什么单独一个文件
------------------
`groups.py` 负责「组是存在的」——建组、成员、上报、榜单、赛事，900 行。
远征负责「组在做什么」——它们是两件事：改玩法不该碰群组逻辑，改群组
也不该担心把事件池搬错地方。两者共用一个库和一套鉴权。

四条不能松的口径
----------------
1. **抽签必须确定性**。同一组、同一幕、同一个人，刷新多少次都看到同一条
   事件。随机源用 `seed:幕:用户:槽位` 拼出来，不落库也能复现——落库只是
   为了让「选过了」有地方记。
2. **行程只在服务端算**。客户端只说「我这周跑了多少」，格数由服务端按
   `min(里程, 周目标) / 5` 折算。客户端能改自己的数字，改不了换算规则。
3. **试炼由数据判定，不自评**。配速分布只有手机算得出来（要
   `activity_detail` 的分圈），所以手机交**原始分圈**（每公里的距离与用时），
   由服务端逐条重算——交结论等于没交，改个包就全过了。判不出来时记
   `unverified`（只算一半），不直接判失败：一刀切会把「真跑了但手机没数据」
   的人罚掉，那比自评更糟。
4. **不做定时任务**。2C2G 不跑常驻进程，所以「周一开新幕」「周日结算」
   全是在有人访问时补算的（懒触发）。代价是没人打开就一直不结算，
   收益是少一个半夜挂掉没人知道的进程。
"""
from __future__ import annotations

import datetime as dt
import json
import random
import secrets
import statistics
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import groups as G

ACTS = 4
ACT_DAYS = 7
SEASON_DAYS = 28
KM_PER_GRID = 5.0
STAGE_RATIO = 0.8           # 与文档一致：Σ周目标 ÷ 5 × 0.8
DEFAULT_WEEK_KM = 25.0      # 没报目标的人按这个算，不让分母变 0
RELIC_RATE = 0.45           # 每幕出现遗迹的概率（与平衡模拟一致）
SIDES_PER_ACT = 2           # 每人每幕抽几条支线
TYPE_W = (("trial", 40), ("supply", 35), ("story", 25))
MORALE_MAX = 10
SUPPLY_MAX = 10
RESCUE_WINDOW = 24 * 3600   # 受困后 24 小时可被救

# 试炼核验的口径（配套 §11.3「试炼不增加距离，只提高精度」）
MIN_LAPS = 3                # 少于 3 个整圈没有「分布」可言，判不了
SHORT_OK = 0.95             # 距离允许差 5%（GPS 少算一点很正常）
MIN_LAP_M = 200             # 200 m 以下的残圈不参与判定
WALK_PACE_S = 390           # 6:30/km，爬坡试炼里低于这个速度算「走了一段」
CLIMB_NEED_M = 200          # 爬坡试炼的最低爬升
MAX_EVIDENCE = 3            # 最多收 3 条候选活动，挑最好的一条算
GRACE_DAYS = 1              # 赛季结束后留一天看结果，第二天开新的一季

POOL_PATH = Path(__file__).resolve().parent / "content" / "expedition_events.json"
_pool = {"mtime": 0.0, "data": None}


# ---------------------------------------------------------------- 事件池
def pool() -> dict:
    """按 mtime 缓存。换季只要替换 JSON，不用重启进程。"""
    try:
        mt = POOL_PATH.stat().st_mtime
    except OSError:
        return {"acts": [], "events": []}
    if _pool["data"] is None or mt != _pool["mtime"]:
        _pool["data"] = json.loads(POOL_PATH.read_text(encoding="utf-8"))
        _pool["mtime"] = mt
    return _pool["data"]


def _by_id(eid: str) -> dict | None:
    for e in pool().get("events") or []:
        if e["id"] == eid:
            return e
    return None


def _is_relic(e: dict) -> bool:
    return any(str(o.get("tag") or "").startswith("relic_") for o in (e.get("options") or []))


# ---------------------------------------------------------------- 建表
def init_db() -> None:
    with G.db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS group_run_season(
          group_id      INTEGER PRIMARY KEY,
          season_start  TEXT NOT NULL,
          season_end    TEXT NOT NULL,
          act           INTEGER NOT NULL DEFAULT 1,
          morale        INTEGER NOT NULL DEFAULT 5,
          seed          TEXT NOT NULL,
          flags         TEXT NOT NULL DEFAULT '{}',
          settled_act   INTEGER NOT NULL DEFAULT 0,
          ended         INTEGER NOT NULL DEFAULT 0,
          updated_at    INTEGER NOT NULL
        );

        -- 一人一幕一行。每幕单独存，历史才能回看「第二幕我在哪条支线上」。
        CREATE TABLE IF NOT EXISTS group_run_member(
          group_id      INTEGER NOT NULL,
          user_id       INTEGER NOT NULL,
          act           INTEGER NOT NULL,
          pos           INTEGER NOT NULL DEFAULT 0,   -- 事件带来的格（训练格在结算时加）
          supply        INTEGER NOT NULL DEFAULT 0,
          week_target_km INTEGER NOT NULL DEFAULT 0,
          branch        TEXT NOT NULL DEFAULT '',      -- 我选的岔路选项 id
          status        TEXT NOT NULL DEFAULT 'moving',
          rescue_by     INTEGER,
          stranded_at   INTEGER,
          excluded      INTEGER NOT NULL DEFAULT 0,    -- 连续两幕没动，从目标分母剔除
          updated_at    INTEGER NOT NULL,
          PRIMARY KEY(group_id, user_id, act)
        );

        CREATE TABLE IF NOT EXISTS group_run_event(
          group_id      INTEGER NOT NULL,
          user_id       INTEGER NOT NULL,
          act           INTEGER NOT NULL,
          slot          INTEGER NOT NULL,
          event_id      TEXT NOT NULL,
          choice        TEXT NOT NULL DEFAULT '',
          tag           TEXT NOT NULL DEFAULT '',
          ts            INTEGER NOT NULL,
          PRIMARY KEY(group_id, user_id, act, slot)
        );

        CREATE TABLE IF NOT EXISTS group_run_trial(
          group_id      INTEGER NOT NULL,
          user_id       INTEGER NOT NULL,
          act           INTEGER NOT NULL,
          trial_id      TEXT NOT NULL,
          done          INTEGER NOT NULL DEFAULT 0,
          activity_id   TEXT NOT NULL DEFAULT '',
          ts            INTEGER NOT NULL,
          PRIMARY KEY(group_id, user_id, act, trial_id)
        );
        """)
        # 老库补列。`level` 是「这次是怎么判的」：verified / failed /
        # unverified（见 api_trial）。解析不出还是 ''（历史数据）。
        cols = {r["name"] for r in c.execute("PRAGMA table_info(group_run_trial)")}
        if "level" not in cols:
            c.execute("ALTER TABLE group_run_trial ADD COLUMN level TEXT NOT NULL DEFAULT ''")


# ---------------------------------------------------------------- 试炼核验
def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _laps_of(cand: dict) -> list[dict]:
    """把客户端交来的分圈洗成「距离 / 用时 / 步频 / 心率 / 爬升」。

    `dist_m` 与 `dur_s` 都必须是正数——残圈（最后 30 米那种）的 `pace_s`
    是拿距离除出来的，一段 30 米能算出 0 秒配速，会把「匀速」判成铁定通过。
    """
    out = []
    for l in (cand.get("laps") or []):
        if not isinstance(l, dict):
            continue
        d, t = _num(l.get("dist_m")), _num(l.get("dur_s"))
        if not d or not t or d < MIN_LAP_M or t <= 0:
            continue
        out.append({"d": d, "t": t,
                    "cad": _num(l.get("cad")), "hr": _num(l.get("hr")),
                    "ele": _num(l.get("ele")) or 0.0})
    return out


def _window(laps: list[dict], need_m: float) -> list[dict]:
    """截出「正好这一段距离」的圈。

    跑 10 km 去应一个 5 km 的试炼，只取前 5 km：否则一个 20 km 的长距离
    跑，前后半程都很慢很匀，什么试炼都能顺手过掉。
    """
    if need_m <= 0:
        return laps if len(laps) >= MIN_LAPS else []
    out, cum = [], 0.0
    for l in laps:
        out.append(l)
        cum += l["d"]
        if cum >= need_m:
            break
    if cum < need_m * SHORT_OK or len(out) < MIN_LAPS:
        return []
    return out


def _halves(laps: list[dict]) -> tuple[list[dict], list[dict]]:
    """按累计距离对半分。跨在中间的那一圈按比例拆开——按「圈数」对半会
    让 6.4 km 的 7 圈变成 3 圈对 4 圈，前后半程差着一公里。"""
    cut = sum(l["d"] for l in laps) / 2.0
    first, second, cum = [], [], 0.0
    for l in laps:
        if cum + l["d"] <= cut:
            first.append(l)
        elif cum >= cut:
            second.append(l)
        else:
            k = cut - cum
            first.append({"d": k, "t": l["t"] * k / l["d"], "cad": l["cad"],
                          "hr": l["hr"], "ele": (l["ele"] or 0) * k / l["d"]})
            second.append({"d": l["d"] - k, "t": l["t"] * (l["d"] - k) / l["d"],
                           "cad": l["cad"], "hr": l["hr"],
                           "ele": (l["ele"] or 0) * (l["d"] - k) / l["d"]})
        cum += l["d"]
    return first, second


def _pace(ls: list[dict]) -> float:
    """整段平均配速（秒/公里）。用时加权，不是各圈配速的平均——短圈不该
    和长圈一样重。"""
    d = sum(x["d"] for x in ls)
    return (sum(x["t"] for x in ls) / d * 1000) if d > 0 else 0.0


def judge(quality: dict, cand: dict) -> str:
    """按「一段真实的分圈」判一条试炼。返回 ok / no / bad。

    bad 是「判不了」不是「没过」：数据缺项、圈不够、距离差太多都算 bad，
    由调用方降级成 unverified。把 bad 当 no 会把没戴心率带的人判成失败。
    """
    q = quality or {}
    metric = str(q.get("metric") or "")
    need = _num(q.get("distance_km")) or 0.0
    tol = _num(q.get("tolerance_s")) or 0.0

    laps = _laps_of(cand)
    win = _window(laps, need * 1000)
    if not win:
        return "bad"
    h1, h2 = _halves(win)

    if metric == "negative_split":
        return "ok" if _pace(h1) - _pace(h2) >= tol else "no"

    if metric == "even_pace":
        avg = _pace(win)
        whole = [l for l in win if l["d"] >= 800] or win
        return "ok" if all(abs(l["t"] / l["d"] * 1000 - avg) <= tol for l in whole) else "no"

    if metric == "cadence_stable":
        cads = [l["cad"] for l in win if l["cad"]]
        if len(cads) < MIN_LAPS:
            return "bad"
        return "ok" if statistics.pstdev(cads) <= tol else "no"

    if metric == "hr_drift":
        hr1 = [l["hr"] for l in h1 if l["hr"]]
        hr2 = [l["hr"] for l in h2 if l["hr"]]
        if not hr1 or not hr2:
            return "bad"
        return "ok" if statistics.fmean(hr2) - statistics.fmean(hr1) <= tol else "no"

    if metric == "climb_steady":
        if sum(l["ele"] for l in win) < CLIMB_NEED_M:
            return "no"
        return "ok" if all(l["t"] / l["d"] * 1000 <= WALK_PACE_S for l in win) else "no"

    return "bad"


def verify_trial(quality: dict, evidence: dict,
                 start: dt.date, end: dt.date) -> tuple[str, str]:
    """核验一条试炼。返回 (level, activity_id)。

    多条候选里**任一条达标**就算过；一条都判不了才算 unverified。这样
    「这周跑了好几次」的人不会因为交上来的第一条不达标就被判失败。
    """
    lo, hi = start.isoformat(), end.isoformat()
    verdicts: list[str] = []
    passed = ""
    for cand in ((evidence or {}).get("activities") or [])[:MAX_EVIDENCE]:
        if not isinstance(cand, dict):
            continue
        d = str(cand.get("date") or "")[:10]
        if not (lo <= d <= hi):
            continue                  # 不在本幕窗口里的活动不认
        v = judge(quality, cand)
        verdicts.append(v)
        if v == "ok" and not passed:
            passed = str(cand.get("activity_id") or "")
    if "ok" in verdicts:
        return "verified", passed
    if "no" in verdicts:
        return "failed", ""
    return "unverified", ""


# ---------------------------------------------------------------- 小工具
def _rng(seed: str, *parts) -> random.Random:
    return random.Random(":".join([seed] + [str(p) for p in parts]))


def _fork_of(act: int, seed: str) -> dict | None:
    forks = [e for e in pool().get("events") or []
             if int(e.get("act") or 0) == act and e.get("kind") == "fork"]
    if not forks:
        return None
    forks = sorted(forks, key=lambda x: x["id"])
    return forks[_rng(seed, act, "fork").randrange(len(forks))]


def _sides_of(act: int, seed: str, uid: int) -> list[str]:
    """每人每幕的支线。类型按 40/35/25 抽，同一个人不重复。"""
    cand = [e for e in pool().get("events") or []
            if int(e.get("act") or 0) == act and e.get("kind") != "fork"
            and not _is_relic(e)]
    out: list[str] = []
    for slot in range(SIDES_PER_ACT):
        rng = _rng(seed, act, uid, slot)
        kind = rng.choices([k for k, _ in TYPE_W], weights=[w for _, w in TYPE_W], k=1)[0]
        bucket = [e for e in cand if e["type"] == kind and e["id"] not in out]
        if not bucket:
            bucket = [e for e in cand if e["id"] not in out]
        if not bucket:
            break
        bucket.sort(key=lambda x: x["id"])
        out.append(bucket[rng.randrange(len(bucket))]["id"])
    return out


def _relic_of(act: int, seed: str, uids: list[int]) -> tuple[int, str] | None:
    """遗迹：每幕按概率出现一次，随机落到一个成员身上——谁先走到谁碰上。"""
    relics = [e for e in pool().get("events") or []
              if int(e.get("act") or 0) == act and _is_relic(e)]
    if not relics or not uids:
        return None
    rng = _rng(seed, act, "relic")
    if rng.random() >= RELIC_RATE:
        return None
    relics.sort(key=lambda x: x["id"])
    uid = sorted(uids)[rng.randrange(len(uids))]
    return uid, relics[rng.randrange(len(relics))]["id"]


def _season_of(c, gid: int):
    return c.execute("SELECT * FROM group_run_season WHERE group_id=?", (gid,)).fetchone()


def _members(c, gid: int, act: int) -> list[str]:
    return [r["user_id"] for r in c.execute(
        "SELECT user_id FROM group_members WHERE group_id=? AND status='active'"
        " ORDER BY joined_at, user_id", (gid,)).fetchall()]


def _row(c, gid: int, uid: int, act: int):
    return c.execute("SELECT * FROM group_run_member WHERE group_id=? AND user_id=? AND act=?",
                     (gid, uid, act)).fetchone()


def _act_span(season, act: int) -> tuple[dt.date, dt.date]:
    start = dt.date.fromisoformat(season["season_start"]) + dt.timedelta(days=(act - 1) * ACT_DAYS)
    return start, start + dt.timedelta(days=ACT_DAYS - 1)


def _target_km(c, gid: int, act: int) -> float:
    """累计段目标：前 N 幕各自「Σ本周目标 ÷ 5 × 0.8」的和。

    没报目标的人按 25 km 兜底——分母不能是 0，否则「谁都不填」就成了
    「怎么走都达标」，段目标直接失效。

    必须是累计：文档写的是「段目标 = 各周应完成量累加」，只算当幕会让
    第四幕的目标和第一幕一样大。
    """
    total = 0.0
    for a in range(1, max(1, act) + 1):
        rows = c.execute("SELECT week_target_km, excluded FROM group_run_member"
                         " WHERE group_id=? AND act=?", (gid, a)).fetchall()
        if not rows:
            continue
        for r in rows:
            if int(r["excluded"] or 0) >= 2:      # 连续两幕没动才剔除
                continue
            total += float(r["week_target_km"] or 0) or DEFAULT_WEEK_KM
    return total / KM_PER_GRID * STAGE_RATIO


# ---------------------------------------------------------------- 开季与推进
def _read(gid: int) -> dict | None:
    with G.db() as c:
        r = _season_of(c, gid)
        return dict(r) if r else None


def _has_draw(gid: int, act: int) -> bool:
    with G.db() as c:
        return bool(c.execute("SELECT 1 FROM group_run_event WHERE group_id=? AND act=? LIMIT 1",
                              (gid, act)).fetchone())


def _cur_act(s: dict) -> int:
    return max(1, min(ACTS, int(s["settled_act"]) + 1))


def _is_ready(s: dict, today: dt.date) -> bool:
    """是否已经结算到「今天该到的地方」。纯判断，不写库。"""
    start = dt.date.fromisoformat(s["season_start"])
    want = max(1, min(ACTS, (today - start).days // ACT_DAYS + 1))
    if int(s["settled_act"]) < want - 1:
        return False
    if not int(s["ended"]) and today > dt.date.fromisoformat(s["season_end"]):
        return False
    return True


def _is_over(s: dict, today: dt.date) -> bool:
    """这一季该翻篇了：已经结束，并且已经过了留给人看结果的那一天。

    留一天是必要的：结束是在「赛季最后一天之后」才判定的，判定和翻篇落在
    同一次请求里的话，结算页还没渲染就被下一季顶掉了——玩家永远看不到结果。
    """
    if not int(s["ended"]):
        return False
    end = dt.date.fromisoformat(s["season_end"]) + dt.timedelta(days=GRACE_DAYS)
    return today > end


def _summary_of(season) -> dict:
    """把一季算成一句结论。只用 `flags` 里记下的数，不读成员行——
    翻篇之后成员行已经清空了，靠行算会把上一季算成 0。"""
    flags = json.loads(season["flags"] or "{}")
    stages = flags.get("stages") or {}
    targets = flags.get("targets") or {}
    total = sum(float(stages.get(str(i), 0)) for i in range(1, ACTS + 1))
    need = float(targets.get(str(ACTS), 0) or 0)
    relics = flags.get("relics") or []
    morale = int(season["morale"])
    if len(relics) >= 3:
        key = "discoverer"
    elif total >= need > 0 and morale >= 3:
        key = "arrive"
    elif total >= need > 0:
        key = "detour"
    else:
        key = "lost"
    return {"start": season["season_start"], "end": season["season_end"],
            "ending": key, "title": _ending_view(key)["title"],
            "team": round(total, 1), "target": round(need, 1),
            "morale": morale, "relics": len(relics)}


def ensure(gid: int, today: dt.date) -> tuple[dict, int, bool]:
    """保证赛季存在且已结算到该到的一幕。返回 (season, 当前幕, 是否已结束)。

    两段式，这是给「cron + 懒触发」并存准备的关键：

    * **热路径**（绝大多数请求）：只读两次，不拿写锁。cron 已经结算过了，
      用户打开就是最新状态。
    * **冷路径**（跨周那一刻，cron 没跑到、或 cron 也没赶上）：进
      `BEGIN IMMEDIATE`，用**条件 UPDATE** 抢 `settled_act`。抢到才结算，
      没抢到说明别的进程/线程正在算，直接读最新值返回。

    「先抢锁再算」而不是「先算再抢锁」：算错的代价是白算 17 ms，
    而先算后抢在抢不到时白算一次，且两次结果若不同会让状态来回跳。
    """
    s = _read(gid)
    if s and _is_over(s, today):
        return _settle_path(gid, today)     # 冷路径里翻篇：归档结论 + 开新的一季
    if s and _is_ready(s, today):
        act = _cur_act(s)
        if _has_draw(gid, act):
            return s, act, bool(s["ended"])
    return _settle_path(gid, today)


def _settle_path(gid: int, today: dt.date) -> tuple[dict, int, bool]:
    with G.db(immediate=True) as c:
        s = _season_of(c, gid)
        carried = "{}"
        if s is not None and _is_over(dict(s), today):
            # 翻篇。上一季的结论压进 flags.prev —— 不另开一张历史表：
            # 每组只留「上一季」，而玩家真正要的也就是上一季的成绩。
            # 三张玩家表必须清掉：它们的主键是 (组, 人, 幕)，不清就会把上季的
            # 补给、行程和试炼当成这一季的，试炼还会因为「已报过」直接判死。
            carried = json.dumps({"prev": _summary_of(s)}, ensure_ascii=False)
            for t in ("group_run_member", "group_run_event", "group_run_trial"):
                c.execute(f"DELETE FROM {t} WHERE group_id=?", (gid,))
            c.execute("DELETE FROM group_run_season WHERE group_id=?", (gid,))
            s = None
        if not s:
            mon = today - dt.timedelta(days=today.weekday())
            end = mon + dt.timedelta(days=SEASON_DAYS - 1)
            c.execute("INSERT INTO group_run_season(group_id,season_start,season_end,act,morale,"
                      "seed,flags,settled_act,ended,updated_at) VALUES(?,?,?,1,5,?,?,0,0,?)",
                      (gid, mon.isoformat(), end.isoformat(), secrets.token_hex(8),
                       carried, G._now()))
            s = _season_of(c, gid)
            if s is None:
                raise G.GroupError(500, "赛季创建失败")

        start = dt.date.fromisoformat(s["season_start"])
        want = max(1, min(ACTS, (today - start).days // ACT_DAYS + 1))

        # 逐幕结算。每一幕先抢 settled_act，抢到才算——抢不到就是别的进程
        # 已经推进了，直接跳出读最新值。
        while int(s["settled_act"]) < want - 1:
            n = int(s["settled_act"]) + 1
            cur = c.execute("UPDATE group_run_season SET settled_act=?, updated_at=?"
                            " WHERE group_id=? AND settled_act=?",
                            (n, G._now(), gid, n - 1))
            if cur.rowcount != 1:
                break
            draw(c, gid, s, n)
            _settle(c, gid, n, s)
            s = _season_of(c, gid)
            if s is None:
                break

        # 赛季收尾：过了最后一个周日就把第 4 幕结算掉并定格
        if s is not None and not int(s["ended"]):
            end = dt.date.fromisoformat(s["season_end"])
            if today > end:
                if int(s["settled_act"]) < ACTS:
                    cur = c.execute("UPDATE group_run_season SET settled_act=?, updated_at=?"
                                    " WHERE group_id=? AND settled_act=?",
                                    (ACTS, G._now(), gid, int(s["settled_act"])))
                    if cur.rowcount == 1:
                        draw(c, gid, s, ACTS)
                        _settle(c, gid, ACTS, s)
                c.execute("UPDATE group_run_season SET ended=1, updated_at=? WHERE group_id=?",
                          (G._now(), gid))
                s = _season_of(c, gid)

        if s is None:
            raise G.GroupError(500, "赛季读取失败")

        # 当前幕如果还没抽签（第一次进入这一幕），补上
        act = _cur_act(dict(s))
        if not c.execute("SELECT 1 FROM group_run_event WHERE group_id=? AND act=? LIMIT 1",
                         (gid, act)).fetchone():
            draw(c, gid, s, act)
        return dict(s), act, bool(s["ended"])


def maybe_strand(c, gid: int, uid: int, act_no: int, season) -> bool:
    """判定并标记受困。返回是否**新**标记。

    三个条件同时成立才受困：补给见底（<2）、本幕有失败的试炼、走的是冒险分支。

    **必须在「报试炼失败」的那一刻就判，不能等到结算**。原来的写法只在
    `_settle` 里标，而 `_settle` 跑完 `settled_act` 就 +1、当前幕变成下一幕了；
    `api_rescue` 查的是当前幕的行，于是永远查不到那个 stranded，
    永远回「这个人没有受困」——24 小时救援窗口形同虚设。

    结算里保留这次调用做兜底：如果试炼是在岔路之前报的，那一刻还不知道
    分支是冒险还是保守，只能等结算时补判。
    """
    row = _row(c, gid, uid, act_no)
    if not row or row["status"] != "moving":
        return False
    if int(row["supply"]) >= 2:
        return False
    failed = c.execute("SELECT 1 FROM group_run_trial WHERE group_id=? AND user_id=?"
                       " AND act=? AND done=0 LIMIT 1", (gid, uid, act_no)).fetchone()
    if not failed:
        return False
    fork = _fork_of(act_no, season["seed"])
    picked = fork and next((o for o in (fork.get("options") or [])
                            if o["id"] == row["branch"]), None)
    # 必须是**正的** advance。`advance: -1` 是「慢一天」（比如抬人下山），
    # 那是稳妥选项，不是冒险——用 truthy 判会把救人算成莽撞。
    if not (picked and int((picked.get("effect") or {}).get("advance") or 0) > 0):
        return False
    c.execute("UPDATE group_run_member SET status='stranded', stranded_at=?"
              " WHERE group_id=? AND user_id=? AND act=?", (G._now(), gid, uid, act_no))
    return True


def _settle(c, gid: int, act_no: int, season) -> None:
    """结算一幕：把训练里程折算成格，加事件与试炼，判受困，算团队总量。"""
    s, e = _act_span(season, act_no)
    uids = _members(c, gid, act_no)

    # 1) 本周每人跑步里程（只有跑步类算里程，和上报口径一致）
    dist: dict[int, float] = {}
    for r in c.execute("SELECT user_id, SUM(distance_m) AS d FROM group_daily"
                       " WHERE group_id=? AND date BETWEEN ? AND ? GROUP BY user_id",
                       (gid, s.isoformat(), e.isoformat())):
        dist[int(r["user_id"])] = float(r["d"] or 0)

    # 2) 试炼：成功的直接加格；失败且补给见底 + 冒险分支 → 受困
    trial_ok: dict[int, int] = {}
    for r in c.execute("SELECT user_id, COUNT(*) AS n FROM group_run_trial"
                       " WHERE group_id=? AND act=? AND done=1 GROUP BY user_id",
                       (gid, act_no)).fetchall():
        trial_ok[int(r["user_id"])] = int(r["n"] or 0)

    total_fail = 0
    tested = 0
    stranded_now: list[int] = []
    for uid in uids:
        row = _row(c, gid, uid, act_no)
        if not row:
            continue
        trials = c.execute("SELECT trial_id, done FROM group_run_trial"
                           " WHERE group_id=? AND user_id=? AND act=?",
                           (gid, uid, act_no)).fetchall()
        for t in trials:
            tested += 1
            if not int(t["done"]):
                total_fail += 1
        # 兜底：正常情况下「报失败」那一刻就判过了（见 maybe_strand），
        # 这里补的是「先报试炼、后选岔路」的顺序。
        if maybe_strand(c, gid, uid, act_no, season):
            stranded_now.append(uid)

    # 3) 团队总量 = Σ(训练格 + 事件格 + 试炼格) × 士气加成
    morale = int(season["morale"])
    bonus = 1 + min(morale // 3, 3) * 0.05
    total = 0.0
    for uid in uids:
        row = _row(c, gid, uid, act_no)
        if not row or row["status"] == "stranded":
            continue            # 受困的人本幕不产格（救援成功另算）
        km = dist.get(uid, 0.0) / 1000.0
        target = float(row["week_target_km"] or 0) or DEFAULT_WEEK_KM
        base = min(km, target) / KM_PER_GRID
        total += (base + int(row["pos"]) + trial_ok.get(uid, 0) * 3)
    total *= bonus

    # 4) 士气：受困扣、群体失败扣
    if stranded_now:
        morale = max(0, morale - min(3, len(stranded_now)))
    if tested and total_fail / tested > 0.40:
        morale = max(0, morale - 1)
    c.execute("UPDATE group_run_season SET morale=?, act=? WHERE group_id=?",
              (morale, min(ACTS, act_no + 1), gid))

    # 5) 活跃度：本幕一点没动的人记一次，连续两幕就退出目标分母
    for uid in uids:
        row = _row(c, gid, uid, act_no)
        if not row:
            continue
        moved = dist.get(uid, 0.0) > 0 or int(row["pos"]) > 0 or uid in trial_ok
        prev = _row(c, gid, uid, act_no - 1) if act_no > 1 else None
        # `excluded` 记的是**连续没动的幕数**：0 正常、1 本幕没动、2 该剔除。
        #
        # 原来的判定是「本幕没动 + 上一幕 week_target_km == 0 + 本幕没里程」，
        # 而 week_target_km 在用户不报目标时**一直是 0**（兜底 25 km 是在
        # 算目标时补的，不回写表）。于是「连续两幕」退化成「只要有一幕没动」，
        # 踢得比设计严一倍，段目标也跟着变小。
        prev_idle = int(prev["excluded"]) if prev else 0
        nxt = 0 if moved else min(2, prev_idle + 1)
        if nxt != int(row["excluded"] or 0):
            c.execute("UPDATE group_run_member SET excluded=?"
                      " WHERE group_id=? AND user_id=? AND act=?", (nxt, gid, uid, act_no))

    # 6) 记本幕总量，供 outcome 用
    flags = json.loads(season["flags"] or "{}")
    flags.setdefault("stages", {})[str(act_no)] = round(total, 1)
    flags["targets"] = flags.get("targets") or {}
    flags["targets"][str(act_no)] = round(_target_km(c, gid, act_no), 1)
    c.execute("UPDATE group_run_season SET flags=? WHERE group_id=?",
              (json.dumps(flags, ensure_ascii=False), gid))


# ---------------------------------------------------------------- 抽签落库
def draw(c, gid: int, season, act_no: int) -> None:
    """为本幕生成每个人的支线与状态行（幂等）。

    两件事必须分开做：**建成员行**和**抽支线**。原来用「本幕已有事件」做
    提前返回，把建行也一起跳过了——只要这一幕先前被 draw 过一次（比如用户
    中途打开 App 看了一眼），后面结算时成员行就不存在，`_target_km` 会把
    这一幕的目标算成 0，段目标凭空少掉一整周。
    """
    uids = _members(c, gid, act_no)
    for uid in uids:
        if _row(c, gid, uid, act_no):
            continue
        prev = _row(c, gid, uid, act_no - 1) if act_no > 1 else None
        c.execute("INSERT OR IGNORE INTO group_run_member(group_id,user_id,act,supply,"
                  "week_target_km,status,updated_at) VALUES(?,?,?,?,?,'moving',?)",
                  (gid, uid, act_no, int(prev["supply"]) if prev else 3, 0, G._now()))
    if c.execute("SELECT 1 FROM group_run_event WHERE group_id=? AND act=? LIMIT 1",
                 (gid, act_no)).fetchone():
        return
    relic = _relic_of(act_no, season["seed"], uids)
    for uid in uids:
        sides = _sides_of(act_no, season["seed"], uid)
        if relic and relic[0] == uid:
            sides = [relic[1]] + sides[:-1]      # 遗迹占第一个槽位
        for slot, eid in enumerate(sides):
            c.execute("INSERT OR IGNORE INTO group_run_event(group_id,user_id,act,slot,"
                      "event_id,choice,tag,ts) VALUES(?,?,?,?,?,'','',?)",
                      (gid, uid, act_no, slot, eid, G._now()))



# ---------------------------------------------------------------- 视图
def stage_progress(c, gid: int, season, act_no: int) -> tuple[float, float]:
    """本幕的 (实时格数, 本幕目标)。画「这一段走到哪」用。

    和 `team_progress` 的区别：那个是整季累计（用于「本周 33.9/24 格」这种
    总量显示），这个是单幕（用于路线上的小旗）。整季进度超额时会 > 100%，
    小旗会顶到终点，看着像走完了——所以两件事必须分开算。
    """
    s, e = _act_span(season, act_no)
    flags = json.loads(season["flags"] or "{}")
    stayed: dict[int, float] = {}
    for r in c.execute("SELECT user_id, week_target_km, pos FROM group_run_member"
                       " WHERE group_id=? AND act=?", (gid, act_no)).fetchall():
        stayed[int(r["user_id"])] = float(r["week_target_km"] or 0) or DEFAULT_WEEK_KM
    dist = {int(r["user_id"]): float(r["d"] or 0) for r in c.execute(
        "SELECT user_id, SUM(distance_m) AS d FROM group_daily"
        " WHERE group_id=? AND date BETWEEN ? AND ? GROUP BY user_id",
        (gid, s.isoformat(), e.isoformat()))}
    live = 0.0
    for uid, target in stayed.items():
        row = c.execute("SELECT pos, status FROM group_run_member WHERE group_id=?"
                        " AND user_id=? AND act=?", (gid, uid, act_no)).fetchone()
        if row and row["status"] == "stranded":
            continue                  # 受困的人这一周不产格
        live += (min(dist.get(uid, 0.0) / 1000.0, target) / KM_PER_GRID
                 + int((row["pos"] if row else 0) or 0))
    bonus = 1 + min(int(season["morale"]) // 3, 3) * 0.05
    live *= bonus
    # 本幕目标 = 累计目标的增量
    total_target = _target_km(c, gid, act_no)
    prev_target = _target_km(c, gid, act_no - 1) if act_no > 1 else 0.0
    return live, max(0.1, total_target - prev_target)


def team_progress(c, gid: int, season, act_no: int) -> tuple[float, float]:
    """返回 (累计行程, 累计目标)。

    `state` 和 `outcome` 都用这一个函数算。原来各算各的：state 给
    「已结算 + 实时」，outcome 只给「已结算」，同一屏上两个数字能差一整格，
    而玩家看到的是「我这周走了 21.7」和「赛季总进度 20.7」，没法解释。
    """
    s, e = _act_span(season, act_no)
    flags = json.loads(season["flags"] or "{}")
    stages = flags.get("stages") or {}
    base_done = sum(float(v) for k, v in stages.items() if int(k) < act_no)

    # 一次 GROUP BY 拿全组的周里程。原来是人循环里各查一次——50 人组
    # 就是 51 条 SQL，而 state 是进组页就调的高频接口。
    dist = {int(r["user_id"]): float(r["d"] or 0) for r in c.execute(
        "SELECT user_id, SUM(distance_m) AS d FROM group_daily"
        " WHERE group_id=? AND date BETWEEN ? AND ? GROUP BY user_id",
        (gid, s.isoformat(), e.isoformat()))}
    live = 0.0
    for r in c.execute("SELECT user_id, week_target_km, pos FROM group_run_member"
                       " WHERE group_id=? AND act=?", (gid, act_no)).fetchall():
        target = float(r["week_target_km"] or 0) or DEFAULT_WEEK_KM
        live += (min(dist.get(int(r["user_id"]), 0.0) / 1000.0, target) / KM_PER_GRID
                 + int(r["pos"] or 0))
    return base_done + live, _target_km(c, gid, act_no)


def _route() -> dict:
    """把四幕的名字拆成「路段名 + 主题」+ 途经点。

    客户端要画「四段路」，但路名是内容（「老鹰嘴」「望雪站」），
    不该硬编码在界面里——换一季就得改客户端。所以由服务端随 state 下发。

    幕名格式是「第N幕 · A → B」，拆出两端；整季的途经点 = 各段起点 + 末段终点。
    """
    acts = sorted(pool().get("acts") or [], key=lambda a: int(a.get("act") or 0))
    out = []
    stops: list[str] = []
    for a in acts:
        name = str(a.get("name") or "")
        body = name.split("·", 1)[1].strip() if "·" in name else name
        parts = [x.strip() for x in body.split("→")]
        if parts and parts[0] and not stops:
            stops.append(parts[0])
        if len(parts) > 1 and parts[-1]:
            stops.append(parts[-1])
        out.append({"act": int(a.get("act") or 0),
                    "from": parts[0] if parts else "",
                    "to": parts[-1] if len(parts) > 1 else "",
                    "theme": str(a.get("theme") or ""),
                    "target": int(a.get("stage_target") or 0)})
    return {"acts": out, "stops": stops}


def _event_view(e: dict, row=None) -> dict:
    """下发给客户端的事件。正文只在服务端——这里确实要给，因为要渲染。

    `options` 只给 id / label / brief，**不给 effect**：效果由服务端在选中时
    结算，客户端看不到「哪个更划算」，免得被当成数值题来算。
    """
    out = {
        "id": e["id"], "title": e.get("title"), "type": e.get("type"),
        "kind": e.get("kind") or "", "act": e.get("act"),
        "summary": e.get("summary") or "", "text": e.get("text") or "",
        "options": [{"id": o["id"], "label": o.get("label"), "brief": o.get("brief") or "",
                     "time": o.get("time") or 0}
                    for o in (e.get("options") or [])],
    }
    if e.get("quality"):
        out["quality"] = e["quality"]
    # 遗迹（旧日志）：界面要把这类事件标出来，否则玩家会直接划过去，
    # 三处集齐的隐藏结局就变成了运气。只给「是否特殊」，不给任何用处说明——
    # 标得越明显越像任务清单，那就不叫探索了。
    if any(str(t).startswith("relic_") for t in
           [str(o.get("tag") or "") for o in (e.get("options") or [])]):
        out["relic"] = True
    if row is not None:
        out["chosen"] = row["choice"]
        out["result"] = (e.get("result_brief") or {}).get(row["choice"], "")
    return out


def api_state(me, gid: int, query: dict) -> dict:
    uid = int(me["user_id"])
    today = G._today((query.get("today") or [None])[0])
    with G.db() as c:
        G._require_member(c, gid, uid)
    season, act_no, ended = ensure(gid, today)
    with G.db() as c:

        s, e = _act_span(season, act_no)
        route = _route()
        fork = _fork_of(act_no, season["seed"])
        rows = c.execute("SELECT * FROM group_run_event WHERE group_id=? AND user_id=?"
                         " AND act=? ORDER BY slot", (gid, uid, act_no)).fetchall()
        mine = _row(c, gid, uid, act_no)
        uids = _members(c, gid, act_no)

        # 团队进度：本幕已结算的前几幕累加 + 本幕实时（训练格按日累计）
        team_now, target_now = team_progress(c, gid, season, act_no)
        stage_now, stage_tgt = stage_progress(c, gid, season, act_no)
        pos = int(mine["pos"]) if mine else 0
        stranded = c.execute("SELECT user_id, stranded_at, rescue_by FROM group_run_member"
                             " WHERE group_id=? AND act=? AND status='stranded'",
                             (gid, act_no)).fetchall()
        fork_sel = _fork_row(c, gid, uid, act_no)
        # 试炼的结果不在 group_run_event 里，在 group_run_trial 里（带 level）。
        # 两张表分开是对的：一个事件可能既被选过又被跑过，混一张就说不清了。
        trs = {r["trial_id"]: dict(r) for r in c.execute(
            "SELECT * FROM group_run_trial WHERE group_id=? AND user_id=? AND act=?",
            (gid, uid, act_no)).fetchall()}

    evs = []
    for r in rows:
        d = _by_id(r["event_id"])
        if not d:
            continue
        v = _event_view(d, r)
        if d.get("type") == "trial":
            t = trs.get(d["id"])
            if t:
                ok = bool(int(t["done"]))
                v["chosen"] = "ok" if ok else "fail"
                v["level"] = t["level"] or ""
                v["result"] = (d.get("result_brief") or {}).get(
                    "success" if ok else "fail", "")
        evs.append(v)
    return {
        "ok": True,
        "season": {"start": season["season_start"], "end": season["season_end"],
                   "act": act_no, "acts": ACTS, "morale": int(season["morale"]),
                   "ended": ended, "week_from": s.isoformat(), "week_to": e.isoformat(),
                   # 上一季的结论：新的一季开场时在页脚留一句，「上周太忙没打开」
                   # 不该等于「那一季没有过」。
                   "last": _last_of(season),
                   # 幕名、主题、途经点：画「四段路」要用，属于内容所以服务端给
                   "stage": route["acts"][act_no - 1] if len(route["acts"]) >= act_no else {},
                   "route": route["acts"], "stops": route["stops"]},
        "progress": {"team": round(team_now, 1),
                     "target": round(target_now, 1),
                     "stage_hit": team_now >= target_now,
                     # 单幕进度：路线上的小旗按这个放。整季累计在超额时会 >100%，
                     # 拿它定位小旗会顶到终点。
                     "stage_team": round(stage_now, 1),
                     "stage_target": round(stage_tgt, 1)},
        "me": {"pos": pos,
               "supply": int(mine["supply"]) if mine else 0,
               "status": mine["status"] if mine else "moving",
               # `week_target_km` 是**用户报的值**，0 表示没报过；
               # 段目标按 `week_target_effective` 算（没报就是 25 km 兜底）。
               # 只给前一个字段的话，前端会显示「本周目标 0 km」，
               # 而实际按 25 算，玩家看到的目标和进度条对不上。
               "week_target_km": float(mine["week_target_km"]) if mine else 0,
               "week_target_effective": (float(mine["week_target_km"]) if mine
                                         and float(mine["week_target_km"] or 0) > 0
                                         else DEFAULT_WEEK_KM),
               "branch": mine["branch"] if mine else ""},
        "fork": _event_view(fork, fork_sel) if fork else None,
        "events": evs,
        "stranded": [{"user_id": int(r["user_id"]), "since": int(r["stranded_at"] or 0),
                      "rescue_by": r["rescue_by"]} for r in stranded],
        "members": len(uids),
    }


def _fork_row(c, gid: int, uid: int, act: int):
    """岔路的选择记在 member.branch 上，不是 group_run_event——岔路是全组一条、
    各人各选，塞进支线表会让「几条支线」这件事变乱。"""
    m = _row(c, gid, uid, act)
    if not m or not m["branch"]:
        return None
    return {"choice": m["branch"]}


def api_target(me, gid: int, body: dict) -> dict:
    uid = int(me["user_id"])
    today = G._today(body.get("today"))
    try:
        km = float(body.get("week_target_km") or 0)
    except (TypeError, ValueError):
        raise G.GroupError(400, "week_target_km 要是数字")
    if not 0 <= km <= 300:
        raise G.GroupError(400, "周目标要在 0–300 km")
    with G.db() as c:
        G._require_member(c, gid, uid)
    season, act_no, _ = ensure(gid, today)
    with G.db() as c:
        # 只能下调，不能上调（文档 11.3：防止为团队多拿格而加练）
        cur = _row(c, gid, uid, act_no)
        if cur and float(cur["week_target_km"] or 0) > 0 and km > float(cur["week_target_km"]) + 0.01:
            raise G.GroupError(409, "周目标只能改小，不能改大")
        c.execute("UPDATE group_run_member SET week_target_km=?, updated_at=?"
                  " WHERE group_id=? AND user_id=? AND act=?", (km, G._now(), gid, uid, act_no))
    return {"ok": True, "week_target_km": km}


def api_fork(me, gid: int, body: dict) -> dict:
    uid = int(me["user_id"])
    today = G._today(body.get("today"))
    opt_id = str(body.get("option") or "").strip()
    with G.db() as c:
        G._require_member(c, gid, uid)
    season, act_no, _ = ensure(gid, today)
    with G.db() as c:
        fork = _fork_of(act_no, season["seed"])
        if not fork:
            raise G.GroupError(404, "本幕没有岔路")
        opt = next((o for o in (fork.get("options") or []) if o["id"] == opt_id), None)
        if not opt:
            raise G.GroupError(400, "没有这个选项")
        row = _row(c, gid, uid, act_no)
        if row and row["branch"]:
            # 选过就是选过。允许反复改会把「权衡」变成「试试哪个好」
            if row["branch"] == opt_id:
                return {"ok": True, "already": True, "result": (fork.get("result_brief") or {}).get(opt_id, "")}
            raise G.GroupError(409, "本幕已经选过了")
        eff = opt.get("effect") or {}
        supply = max(0, min(SUPPLY_MAX, int(row["supply"] if row else 3) + int(eff.get("supply") or 0)))
        c.execute("UPDATE group_run_member SET branch=?, supply=?, pos=pos+?, updated_at=?"
                  " WHERE group_id=? AND user_id=? AND act=?",
                  (opt_id, supply, int(eff.get("advance") or 0), G._now(), gid, uid, act_no))
        tag = str(opt.get("tag") or "")
        if tag:
            flags = json.loads(season["flags"] or "{}")
            picked = flags.setdefault("picks", {})
            picked[tag] = int(picked.get(tag, 0)) + 1
            c.execute("UPDATE group_run_season SET flags=? WHERE group_id=?",
                      (json.dumps(flags, ensure_ascii=False), gid))
    return {"ok": True, "supply": supply, "pos": int((row["pos"] if row else 0)) + int(eff.get("advance") or 0),
            "result": (fork.get("result_brief") or {}).get(opt_id, "")}


def api_event(me, gid: int, body: dict) -> dict:
    uid = int(me["user_id"])
    today = G._today(body.get("today"))
    slot = body.get("slot")
    opt_id = str(body.get("option") or "").strip()
    with G.db() as c:
        G._require_member(c, gid, uid)
    season, act_no, _ = ensure(gid, today)
    with G.db() as c:
        if slot is None:
            raise G.GroupError(400, "缺少 slot")
        try:
            slot = int(slot)
        except (TypeError, ValueError):
            raise G.GroupError(400, "slot 要是整数")
        erow = c.execute("SELECT * FROM group_run_event WHERE group_id=? AND user_id=?"
                         " AND act=? AND slot=?", (gid, uid, act_no, slot)).fetchone()
        if not erow:
            raise G.GroupError(404, "没有这条事件")
        ev = _by_id(erow["event_id"])
        if not ev:
            raise G.GroupError(404, "事件已失效")
        opt = next((o for o in (ev.get("options") or []) if o["id"] == opt_id), None)
        if not opt:
            raise G.GroupError(400, "没有这个选项")
        if erow["choice"]:
            if erow["choice"] == opt_id:
                return {"ok": True, "already": True,
                        "result": (ev.get("result_brief") or {}).get(opt_id, "")}
            raise G.GroupError(409, "这个事件已经选过了")

        eff = opt.get("effect") or {}
        m = _row(c, gid, uid, act_no)
        supply = max(0, min(SUPPLY_MAX, int(m["supply"] if m else 3) + int(eff.get("supply") or 0)))
        c.execute("UPDATE group_run_member SET supply=?, pos=pos+?, updated_at=?"
                  " WHERE group_id=? AND user_id=? AND act=?",
                  (supply, int(eff.get("advance") or 0), G._now(), gid, uid, act_no))
        tag = str(opt.get("tag") or "")
        c.execute("UPDATE group_run_event SET choice=?, tag=?, ts=? WHERE group_id=?"
                  " AND user_id=? AND act=? AND slot=?",
                  (opt_id, tag, G._now(), gid, uid, act_no, slot))

        # 遗迹：选什么都算记录（拿 / 抄），保证不会因为一个选项错过隐藏结局
        if tag.startswith("relic_"):
            flags = json.loads(season["flags"] or "{}")
            rel = flags.setdefault("relics", [])
            if tag not in rel:
                rel.append(tag)
            c.execute("UPDATE group_run_season SET flags=? WHERE group_id=?",
                      (json.dumps(flags, ensure_ascii=False), gid))
        elif tag:
            flags = json.loads(season["flags"] or "{}")
            picked = flags.setdefault("picks", {})
            picked[tag] = int(picked.get(tag, 0)) + 1
            c.execute("UPDATE group_run_season SET flags=? WHERE group_id=?",
                      (json.dumps(flags, ensure_ascii=False), gid))
    return {"ok": True, "supply": supply, "pos": int((m["pos"] if m else 0)) + int(eff.get("advance") or 0),
            "result": (ev.get("result_brief") or {}).get(opt_id, "")}


def api_trial(me, gid: int, body: dict) -> dict:
    """上报试炼结果。

    三档，由**数据**定，不由自评定：

    | level | 条件 | 奖励 |
    |---|---|---|
    | `verified` | 交了分圈、逐条算过、达标 | 全额 |
    | `failed` | 交了分圈、算出来没达标 | 按失败（扣补给，可能受困） |
    | `unverified` | 判不了（室内没圈 / 手表没同步） | 只算一半，不给士气与增益 |

    第三档是刻意留的：一刀切判失败会把「真跑了但手机没数据」的人罚掉，
    比自评更伤。但「随手点一下」和「跑完回来」拿的东西必须不一样，否则
    这个机制就只是装饰。
    """
    uid = int(me["user_id"])
    today = G._today(body.get("today"))
    trial_id = str(body.get("trial_id") or "").strip()
    claim = bool(body.get("done"))
    activity_id = str(body.get("activity_id") or "")[:40]
    evidence = body.get("evidence") if isinstance(body.get("evidence"), dict) else None
    with G.db() as c:
        G._require_member(c, gid, uid)
    season, act_no, _ = ensure(gid, today)
    with G.db() as c:
        ev = _by_id(trial_id)
        if not ev or ev.get("type") != "trial":
            raise G.GroupError(400, "不是试炼")
        if int(ev.get("act") or 0) != act_no:
            raise G.GroupError(409, "这条试炼不属于当前这一幕")
        # 必须是自己抽到的那条。池子不大（每幕三四条），不查这一下就等于
        # 「随便挑一条试炼来报」——奖励照样给，抽签也就白抽了。
        mine = c.execute("SELECT 1 FROM group_run_event WHERE group_id=? AND user_id=?"
                         " AND act=? AND event_id=?",
                         (gid, uid, act_no, trial_id)).fetchone()
        if not mine:
            raise G.GroupError(404, "这条试炼不在你这次的路上")
        old = c.execute("SELECT * FROM group_run_trial WHERE group_id=? AND user_id=?"
                        " AND act=? AND trial_id=?", (gid, uid, act_no, trial_id)).fetchone()
        if old:
            if int(old["done"]) == int(claim):
                return {"ok": True, "already": True, "done": claim,
                        "level": old["level"] or ""}
            raise G.GroupError(409, "这条试炼已经报过了")

        # 有证据就由证据说话。没证据才退回自评，并且只给一半。
        w_start, w_end = _act_span(season, act_no)
        level, hit = verify_trial(ev.get("quality") or {}, evidence, w_start, w_end)
        if level == "verified":
            done = True
        elif level == "failed":
            done = False
        else:
            done = claim          # 判不了：按用户说的记，但奖励打折

        if done and level == "verified":
            eff = dict(ev.get("success") or {})
        elif done:
            # 未核验：格数对折，士气与增益不给。补给是资源不是奖励，照给。
            eff = dict(ev.get("success") or {})
            adv = int(eff.get("advance") or 0)
            if adv:
                eff["advance"] = max(1, adv // 2)
            eff.pop("morale", None)
            eff.pop("buff", None)
        else:
            eff = dict(ev.get("fail") or {})

        c.execute("INSERT INTO group_run_trial(group_id,user_id,act,trial_id,done,"
                  "activity_id,ts,level) VALUES(?,?,?,?,?,?,?,?)",
                  (gid, uid, act_no, trial_id, int(done),
                   activity_id or hit, G._now(), level))
        # 事件行也标一下，否则卡片永远显示「跑完了 / 没跑成」两个按钮，
        # 用户会一遍遍点，每次都弹「已经报过了」。
        c.execute("UPDATE group_run_event SET choice=?, ts=? WHERE group_id=? AND user_id=?"
                  " AND act=? AND event_id=?",
                  ("ok" if done else "fail", G._now(), gid, uid, act_no, trial_id))
        m = _row(c, gid, uid, act_no)
        supply = max(0, min(SUPPLY_MAX, int(m["supply"] if m else 3) + int(eff.get("supply") or 0)))
        c.execute("UPDATE group_run_member SET supply=?, pos=pos+?, updated_at=?"
                  " WHERE group_id=? AND user_id=? AND act=?",
                  (supply, int(eff.get("advance") or 0), G._now(), gid, uid, act_no))
        morale_add = int(eff.get("morale") or 0)
        if morale_add:
            c.execute("UPDATE group_run_season SET morale=MIN(?, morale+?) WHERE group_id=?",
                      (MORALE_MAX, morale_add, gid))
        # 失败之后立刻判受困：补给刚被扣掉，这一刻三个条件全部已知。
        # 等结算再判就晚了——那一幕已经结束，救援窗口根本打不开。
        stranded = bool(not done and maybe_strand(c, gid, uid, act_no, season))
    note = ""
    if level == "unverified":
        note = ("这次没找到能核验的跑步记录，按未核验记：格数只算一半，"
                "士气与增益不算。" if done else "")
    elif level == "verified":
        note = "记录对得上，按达标算。"
    else:
        note = "记录里这一段没达到要求，按没跑成算。"
    return {"ok": True, "done": done, "level": level, "supply": supply,
            "advance": int(eff.get("advance") or 0), "note": note,
            # 叫 me_stranded 而不是 stranded：state 里的 `stranded` 是**数组**
            # （全组受困名单），同一个名字两种类型，客户端统一建模时会撞。
            "me_stranded": stranded,
            "result": (ev.get("result_brief") or {}).get("success" if done else "fail", "")}


def api_rescue(me, gid: int, body: dict) -> dict:
    uid = int(me["user_id"])
    today = G._today(body.get("today"))
    try:
        target = int(body.get("user_id"))
    except (TypeError, ValueError):
        raise G.GroupError(400, "缺少 user_id")
    with G.db() as c:
        G._require_member(c, gid, uid)
    season, act_no, _ = ensure(gid, today)
    with G.db() as c:
        if target == uid:
            raise G.GroupError(400, "不能救自己")
        t = _row(c, gid, target, act_no)
        if not t or t["status"] != "stranded":
            raise G.GroupError(404, "这个人没有受困")
        if t["rescue_by"]:
            raise G.GroupError(409, "已经有人去了")
        if G._now() - int(t["stranded_at"] or 0) > RESCUE_WINDOW:
            raise G.GroupError(409, "救援窗口已经关了")
        mine = _row(c, gid, uid, act_no)
        if mine["status"] == "stranded":
            raise G.GroupError(409, "自己也困住了，救不了人")
        # 救援成本：−1 补给、−1 格个人行程；回报：全组 +1 士气
        c.execute("UPDATE group_run_member SET supply=MAX(0, supply-1), pos=pos-1, updated_at=?"
                  " WHERE group_id=? AND user_id=? AND act=?", (G._now(), gid, uid, act_no))
        c.execute("UPDATE group_run_member SET status='rescued', rescue_by=? WHERE group_id=?"
                  " AND user_id=? AND act=?", (uid, gid, target, act_no))
        c.execute("UPDATE group_run_season SET morale=MIN(?, morale+1) WHERE group_id=?",
                  (MORALE_MAX, gid))
        flags = json.loads(season["flags"] or "{}")
        helpers = flags.setdefault("helpers", [])
        helpers.append(uid)
        c.execute("UPDATE group_run_season SET flags=? WHERE group_id=?",
                  (json.dumps(flags, ensure_ascii=False), gid))
    return {"ok": True, "rescued": target}


def _last_of(season) -> dict:
    """上一季的结论（翻篇时压进 `flags.prev`）。没有就是空 dict。"""
    try:
        return json.loads(season["flags"] or "{}").get("prev") or {}
    except (ValueError, KeyError, TypeError):
        return {}


def _ending_view(key: str) -> dict:
    """结局的标题与文案。

    文案是**内容**，和事件正文一样只在服务端：客户端平时只知道 `ending`
    这个键，到赛季结束才拿到具体文字。这样哪怕有人去抓包，「另一个结局
    是什么样」也得真的再走一季才知道。
    """
    ends = pool().get("endings") or {}
    one = ends.get(key) or {}
    return {"key": key,
            "title": str(one.get("title") or key),
            "line": str(one.get("line") or ""),
            "text": str(one.get("text") or "")}


def api_outcome(me, gid: int, query: dict) -> dict:
    """赛季结局。没到结束就返回当前进度，不提前剧透。"""
    uid = int(me["user_id"])
    today = G._today((query.get("today") or [None])[0])
    with G.db() as c:
        G._require_member(c, gid, uid)
    season, act_no, ended = ensure(gid, today)
    with G.db() as c:
        flags = json.loads(season["flags"] or "{}")
        stages = flags.get("stages") or {}
        targets = flags.get("targets") or {}
        total = sum(float(stages.get(str(i), 0)) for i in range(1, ACTS + 1))
        reach = total >= float(targets.get(str(ACTS), _target_km(c, gid, ACTS)))
        relics = flags.get("relics") or []
        morale = int(season["morale"])
        # 逐幕记录：结算页要显示「哪几段走够了」。前端不该自己从总分反推——
        # 各幕的目标是逐周算的，反推出来会和当时实际判定的不一样。
        route = _route()
        acts = []
        for i in range(1, len(route["acts"]) + 1):
            got = float(stages.get(str(i), 0))
            # 注意：`targets[i]` 是**到第 i 幕为止的累计目标**，而 `stages[i]`
            # 是**第 i 幕自己的产出**。直接比会把第 2 幕的 13 格拿去和
            # 「前两幕合计 24」比，于是每一段都判成没走够。
            # 要减出本段份额：targets[i] - targets[i-1]。
            prev = float(targets.get(str(i - 1), 0)) if i > 1 else 0.0
            need = max(0.0, float(targets.get(str(i), 0)) - prev)
            acts.append({"act": i,
                         "theme": (route["acts"][i - 1].get("theme") if i <= len(route["acts"]) else ""),
                         "got": round(got, 1), "target": round(need, 1),
                         "hit": need > 0 and got >= need})
        members = len(_members(c, gid, ACTS))
    if not ended:
        with G.db() as c:
            team_now, target_now = team_progress(c, gid, season, act_no)
        return {"ok": True, "ended": False, "act": act_no,
                "team": round(team_now, 1), "target": round(target_now, 1),
                "stage_hit": team_now >= target_now,
                # 上一季的成绩。翻篇之后没有人再能看到它，所以随这里带一份
                # ——「上周太忙没打开」不该等于「没有过这一季」。
                "last": _last_of(season)}
    if len(relics) >= 3:
        ending = "discoverer"
    elif reach and morale >= 3:
        ending = "arrive"
    elif reach:
        ending = "detour"
    else:
        ending = "lost"
    return {"ok": True, "ended": True, "ending": ending,
            "ending_detail": _ending_view(ending),
            "team": round(total, 1), "morale": morale, "relics": relics,
            "acts": acts, "members": members, "relic_count": len(relics),
            "start": season["season_start"], "end": season["season_end"],
            "season": pool().get("season") or "雪线之前"}


# ---------------------------------------------------------------- 分发
def handle(method: str, raw_path: str, headers, body, auth) -> tuple[dict, int]:
    body = body if isinstance(body, dict) else {}
    try:
        u = urlsplit(raw_path)
        parts = [p for p in u.path.strip("/").split("/") if p]
        # /v1/group/{id}/run/{action}
        if len(parts) < 5 or parts[0] != "v1" or parts[1] != "group" or parts[3] != "run":
            raise G.GroupError(404, "没有这个接口")
        try:
            gid = int(parts[2])
        except ValueError:
            raise G.GroupError(400, "组 id 不合法")
        action = parts[4]
        query = parse_qs(u.query)
        # 内容不随代码走（事件池进了 .gitignore，只放服务器）。忘了传就明确报错，
        # 否则表现是「岔路永远是空的」，排查会绕很远。
        if not POOL_PATH.is_file():
            raise G.GroupError(503, "赛季内容未配置：缺少 server/content/expedition_events.json")
        me = auth(headers)

        if action == "state" and method == "GET":
            return api_state(me, gid, query), 200
        if action == "outcome" and method == "GET":
            return api_outcome(me, gid, query), 200
        if action == "target" and method == "POST":
            return api_target(me, gid, body), 200
        if action == "fork" and method == "POST":
            return api_fork(me, gid, body), 200
        if action == "event" and method == "POST":
            return api_event(me, gid, body), 200
        if action == "trial" and method == "POST":
            return api_trial(me, gid, body), 200
        if action == "rescue" and method == "POST":
            return api_rescue(me, gid, body), 200
        raise G.GroupError(404, "没有这个接口")
    except G.GroupError as e:
        return {"ok": False, "error": e.message}, e.code
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        code = getattr(e, "code", None)
        msg = getattr(e, "message", None)
        if isinstance(code, int) and isinstance(msg, str):
            return {"ok": False, "error": msg}, code
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}, 500
