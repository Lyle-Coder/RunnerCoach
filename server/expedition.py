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
3. **试炼只记不验**。配速分布只有手机算得出来（要 activity_detail 的分圈），
   服务端收到「完成了」就加格。防不住伪造，所以产品上接受（风险清单里
   已写明），不做反作弊的假动作——那只会让正常人被误伤。
4. **不做定时任务**。2C2G 不跑常驻进程，所以「周一开新幕」「周日结算」
   全是在有人访问时补算的（懒触发）。代价是没人打开就一直不结算，
   收益是少一个半夜挂掉没人知道的进程。
"""
from __future__ import annotations

import datetime as dt
import json
import random
import secrets
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
            if r["excluded"]:
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
    if s and _is_ready(s, today):
        act = _cur_act(s)
        if _has_draw(gid, act):
            return s, act, bool(s["ended"])
    return _settle_path(gid, today)


def _settle_path(gid: int, today: dt.date) -> tuple[dict, int, bool]:
    with G.db(immediate=True) as c:
        s = _season_of(c, gid)
        if not s:
            mon = today - dt.timedelta(days=today.weekday())
            end = mon + dt.timedelta(days=SEASON_DAYS - 1)
            c.execute("INSERT INTO group_run_season(group_id,season_start,season_end,act,morale,"
                      "seed,flags,settled_act,ended,updated_at) VALUES(?,?,?,1,5,?, '{}',0,0,?)",
                      (gid, mon.isoformat(), end.isoformat(), secrets.token_hex(8), G._now()))
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
        idle_prev = bool(prev and int(prev["excluded"]) == 0 and prev["week_target_km"] == 0
                         and not dist.get(uid, 0.0))
        # 用副表记连续次数会多一张表；直接用 excluded 的单调性：
        # 本幕没动且上一幕也没动 → 剔除。上一幕的判断看它有没有产格。
        if not moved and idle_prev:
            c.execute("UPDATE group_run_member SET excluded=1"
                      " WHERE group_id=? AND user_id=? AND act=?", (gid, uid, act_no))

    # 6) 记本幕总量，供 outcome 用
    flags = json.loads(season["flags"] or "{}")
    flags.setdefault("stages", {})[str(act_no)] = round(total, 1)
    flags["targets"] = flags.get("targets") or {}
    flags["targets"][str(act_no)] = round(_target_km(c, gid, act_no), 1)
    c.execute("UPDATE group_run_season SET flags=? WHERE group_id=?",
              (json.dumps(flags, ensure_ascii=False), gid))


# ---------------------------------------------------------------- 抽签落库
def draw(c, gid: int, season, act_no: int) -> None:
    """为本幕生成每个人的支线（幂等：已有就不重抽）。"""
    if c.execute("SELECT 1 FROM group_run_event WHERE group_id=? AND act=? LIMIT 1",
                 (gid, act_no)).fetchone():
        return
    uids = _members(c, gid, act_no)
    relic = _relic_of(act_no, season["seed"], uids)
    for uid in uids:
        sides = _sides_of(act_no, season["seed"], uid)
        if relic and relic[0] == uid:
            sides = [relic[1]] + sides[:-1]      # 遗迹占第一个槽位
        for slot, eid in enumerate(sides):
            c.execute("INSERT OR IGNORE INTO group_run_event(group_id,user_id,act,slot,"
                      "event_id,choice,tag,ts) VALUES(?,?,?,?,?,'','',?)",
                      (gid, uid, act_no, slot, eid, G._now()))
    # 本幕的初始状态：补给继承上一幕，没有就按 3 起
    for uid in uids:
        if _row(c, gid, uid, act_no):
            continue
        prev = _row(c, gid, uid, act_no - 1) if act_no > 1 else None
        c.execute("INSERT INTO group_run_member(group_id,user_id,act,supply,week_target_km,"
                  "status,updated_at) VALUES(?,?,?,?,?,'moving',?)",
                  (gid, uid, act_no, int(prev["supply"]) if prev else 3,
                   0, G._now()))


# ---------------------------------------------------------------- 视图
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
        fork = _fork_of(act_no, season["seed"])
        rows = c.execute("SELECT * FROM group_run_event WHERE group_id=? AND user_id=?"
                         " AND act=? ORDER BY slot", (gid, uid, act_no)).fetchall()
        mine = _row(c, gid, uid, act_no)
        uids = _members(c, gid, act_no)

        # 团队进度：本幕已结算的前几幕累加 + 本幕实时（训练格按日累计）
        team_now, target_now = team_progress(c, gid, season, act_no)
        pos = int(mine["pos"]) if mine else 0
        stranded = c.execute("SELECT user_id, stranded_at, rescue_by FROM group_run_member"
                             " WHERE group_id=? AND act=? AND status='stranded'",
                             (gid, act_no)).fetchall()
        fork_sel = _fork_row(c, gid, uid, act_no)

    evs = []
    for r in rows:
        d = _by_id(r["event_id"])
        if d:
            evs.append(_event_view(d, r))
    return {
        "ok": True,
        "season": {"start": season["season_start"], "end": season["season_end"],
                   "act": act_no, "acts": ACTS, "morale": int(season["morale"]),
                   "ended": ended, "week_from": s.isoformat(), "week_to": e.isoformat()},
        "progress": {"team": round(team_now, 1),
                     "target": round(target_now, 1),
                     "stage_hit": team_now >= target_now},
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
    """上报试炼结果。`done=false` 也要报——失败要扣补给，不报不等于没做。"""
    uid = int(me["user_id"])
    today = G._today(body.get("today"))
    trial_id = str(body.get("trial_id") or "").strip()
    done = bool(body.get("done"))
    activity_id = str(body.get("activity_id") or "")[:40]
    with G.db() as c:
        G._require_member(c, gid, uid)
    season, act_no, _ = ensure(gid, today)
    with G.db() as c:
        ev = _by_id(trial_id)
        if not ev or ev.get("type") != "trial":
            raise G.GroupError(400, "不是试炼")
        if int(ev.get("act") or 0) != act_no:
            raise G.GroupError(409, "这条试炼不属于当前这一幕")
        old = c.execute("SELECT * FROM group_run_trial WHERE group_id=? AND user_id=?"
                        " AND act=? AND trial_id=?", (gid, uid, act_no, trial_id)).fetchone()
        if old:
            if int(old["done"]) == int(done):
                return {"ok": True, "already": True, "done": done}
            raise G.GroupError(409, "这条试炼已经报过了")
        c.execute("INSERT INTO group_run_trial(group_id,user_id,act,trial_id,done,"
                  "activity_id,ts) VALUES(?,?,?,?,?,?,?)",
                  (gid, uid, act_no, trial_id, int(done), activity_id, G._now()))
        eff = (ev.get("success") if done else ev.get("fail")) or {}
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
    return {"ok": True, "done": done, "supply": supply,
            "advance": int(eff.get("advance") or 0),
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
    if not ended:
        with G.db() as c:
            team_now, target_now = team_progress(c, gid, season, act_no)
        return {"ok": True, "ended": False, "act": act_no,
                "team": round(team_now, 1), "target": round(target_now, 1),
                "stage_hit": team_now >= target_now}
    if len(relics) >= 3:
        ending = "discoverer"
    elif reach and morale >= 3:
        ending = "arrive"
    elif reach:
        ending = "detour"
    else:
        ending = "lost"
    return {"ok": True, "ended": True, "ending": ending, "team": round(total, 1),
            "morale": morale, "relics": relics}


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
