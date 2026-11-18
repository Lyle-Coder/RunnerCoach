# -*- coding: utf-8 -*-
"""用户组：组、成员、日汇总、榜单、组内赛事。

为什么单独一个库
----------------
上报是高频写——50 人组每人每小时最多 60 次，一次要写几十行 `group_daily`。
如果和 `app.db` 的余额流水共用，一次大批上报就能把登录、支付卡住几秒。
所以拆到 `data/groups.db`（可用 `GROUPS_DB` 覆盖）。

代价是没有跨库外键。成员昵称冗余存在 `group_members.alias` 里，不 JOIN `users`；
`user_id` 的有效性由调用方保证（令牌已经验过，用户一定在 app.db 里）。
这个冗余是刻意的——它就是为「独立库」付出的成本换来的解法。

为什么不靠定时任务
------------------
2C2G 不跑常驻 cron。赛事的「进行中」是**算出来的**：
`closed_at IS NULL AND end_date >= 今天`，不翻转状态字段；榜单也不缓存，
每次按区间聚合。少一个后台进程，就少一个半夜挂掉没人知道的东西。

日期口径
--------
`date` 一律是**用户本地日期字符串**，服务端不做时区换算。
服务器不知道用户在哪个时区，所以「今天」由客户端传 `today` 参数带来，
不传才退回服务器日期。硬用服务器日期会让 UTC+8 的用户在周一早上看到上周的榜。
"""
from __future__ import annotations

import datetime as dt
import os
import re
import secrets
import sqlite3
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import ranking

DEFAULT_DB = Path(__file__).resolve().parent / "data" / "groups.db"
GROUPS_DB = os.environ.get("GROUPS_DB", str(DEFAULT_DB))

# 邀请码去掉 0/O/1/I/L：手抄和口述时最容易错的那几个
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LEN = 8
# 允许上报的最远未来天数。**生产必须是 0**：设备时钟快一天就会把「明天」的
# 里程写进来，而那些里程会落进后面几幕的窗口，等于提前灌格。
# 只有测试要一次性模拟整季时会临时放宽（见 test_groups.py）。
FUTURE_DAYS = 0


MAX_MEMBERS_MIN, MAX_MEMBERS_MAX = 2, 200
MAX_GROUPS_PER_USER = 3        # 一个人最多同时加入 3 个组
REPORT_MAX_DAYS = 31           # 一次最多报 31 天（首次加入补 30 天足够）
DAY_DIST_MAX_M = 200_000       # 单日 200 km，超过直接拒（不是判作弊，是防脏数据）
DAY_DUR_MAX_S = 24 * 3600
DAY_ACTS_MAX = 20
DAY_ELEV_MAX = 8000          # 单日爬升 8000 m，超过直接拒（防脏数据）
_ELEV_PER_KM_MAX = 200.0      # 爬升密度上限（m/km），日报表只能按这个截断

CHALLENGE_MAX_DAYS = 90
CHALLENGE_MAX_RUNNING = 3
CHALLENGE_DIST_MIN, CHALLENGE_DIST_MAX = 1_000, 1_000_000    # 1–1000 km
CHALLENGE_ACTS_MIN, CHALLENGE_ACTS_MAX = 1, 200

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class GroupError(Exception):
    """带状态码的业务异常。handler 统一转成 {"ok":false,"error":...}。"""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message


# ---------------------------------------------------------------- 存储
@contextmanager
def db(immediate: bool = False):
    """数据库连接。

    `immediate=True` 时一开始就拿写锁（`BEGIN IMMEDIATE`），用于**跨进程**
    的「只做一次」场景（赛季结算）。区别很关键：

    * 默认（deferred）：先 SELECT 拿读锁，真要写时才升级成写锁。两个进程
      同时读、又同时想写，就会互相等对方的读锁，直接 `database is locked`。
    * `BEGIN IMMEDIATE`：进门就拿写锁，后到的排队等（timeout 10 秒），
      等到之后读到的是已经提交过的新值，判断自然成立。

    代价是排队的那段时间里，这个库的所有写都要等——所以只给短事务用，
    不要拿它包一次扫描全表的批处理。
    """
    Path(GROUPS_DB).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(GROUPS_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        # 与 app.py 同样的理由：WAL 拿不到就退回默认 journal，不让优化拖垮服务
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        if immediate:
            conn.isolation_level = None          # 关掉隐式事务，手动控制
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if immediate:
            conn.execute("COMMIT")
        else:
            conn.commit()
    except Exception:
        if immediate:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise
    finally:
        conn.close()


def init_db() -> None:
    Path(GROUPS_DB).parent.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS groups(
          id           INTEGER PRIMARY KEY,
          name         TEXT NOT NULL,
          intro        TEXT NOT NULL DEFAULT '',
          owner_id     INTEGER NOT NULL,
          invite_code  TEXT NOT NULL UNIQUE,
          max_members  INTEGER NOT NULL DEFAULT 50,
          created_at   INTEGER NOT NULL,
          archived     INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS group_members(
          group_id     INTEGER NOT NULL,
          user_id      INTEGER NOT NULL,
          role         TEXT NOT NULL DEFAULT 'member',
          alias        TEXT NOT NULL,
          share_level  TEXT NOT NULL DEFAULT 'summary',
          joined_at    INTEGER NOT NULL,
          status       TEXT NOT NULL DEFAULT 'active',
          PRIMARY KEY(group_id, user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_gmember_user ON group_members(user_id, status);

        CREATE TABLE IF NOT EXISTS group_daily(
          group_id     INTEGER NOT NULL,
          user_id      INTEGER NOT NULL,
          date         TEXT NOT NULL,
          distance_m   INTEGER NOT NULL DEFAULT 0,
          duration_s   INTEGER NOT NULL DEFAULT 0,
          acts         INTEGER NOT NULL DEFAULT 0,
          plan_done    INTEGER,
          -- 当日爬升（米）。圆点衫要用；老库靠下面的 _migrate 补列，
          -- 默认 0 表示「这一版客户端还没传」，不是「这天没爬升」。
          elevation_m  INTEGER NOT NULL DEFAULT 0,
          updated_at   INTEGER NOT NULL,
          PRIMARY KEY(group_id, user_id, date)
        );
        CREATE INDEX IF NOT EXISTS idx_gdaily_board ON group_daily(group_id, date);

        CREATE TABLE IF NOT EXISTS group_challenges(
          id           INTEGER PRIMARY KEY,
          group_id     INTEGER NOT NULL,
          title        TEXT NOT NULL,
          kind         TEXT NOT NULL,
          target       INTEGER NOT NULL,
          start_date   TEXT NOT NULL,
          end_date     TEXT NOT NULL,
          created_by   INTEGER NOT NULL,
          created_at   INTEGER NOT NULL,
          closed_at    INTEGER,
          archived     INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_gchal_group ON group_challenges(group_id, end_date);

        CREATE TABLE IF NOT EXISTS group_audit(
          id           INTEGER PRIMARY KEY,
          group_id     INTEGER NOT NULL,
          actor_id     INTEGER NOT NULL,
          action       TEXT NOT NULL,
          target_id    INTEGER,
          ts           INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_gaudit_group ON group_audit(group_id, id);
        """)
        # 老库补列。SQLite 的 ADD COLUMN 支持带默认值，已有行会拿到 0；
        # 不做数据回填——0 就是「那几天还没有这个字段」，回填成别的值是编数据。
        cols = {r["name"] for r in c.execute("PRAGMA table_info(group_daily)")}
        if "elevation_m" not in cols:
            c.execute("ALTER TABLE group_daily ADD COLUMN elevation_m INTEGER"
                      " NOT NULL DEFAULT 0")


# ---------------------------------------------------------------- 工具
def _now() -> int:
    return int(time.time())


def _date(v, field: str = "date") -> dt.date:
    s = str(v or "")[:10]
    if not DATE_RE.match(s):
        raise GroupError(400, f"{field} 格式应为 YYYY-MM-DD")
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        raise GroupError(400, f"{field} 不是有效日期")


def _today(v=None) -> dt.date:
    """客户端带来的「今天」优先。服务器不知道用户在哪个时区。"""
    return _date(v, "today") if v else dt.date.today()


def _week(today: dt.date) -> tuple[dt.date, dt.date]:
    mon = today - dt.timedelta(days=today.weekday())
    return mon, mon + dt.timedelta(days=6)


def _month(today: dt.date) -> tuple[dt.date, dt.date]:
    first = today.replace(day=1)
    nxt = (first + dt.timedelta(days=32)).replace(day=1)
    return first, nxt - dt.timedelta(days=1)


def _clean_alias(v, phone: str = "", fallback: str = "") -> str:
    """组内昵称。空值时按「账号昵称 → 打码账号 → 跑者」逐级兜底。

    账号昵称排在最前，是因为同一个人本来就叫那个名字；让他每建一个组都
    重新想一次，只会得到一堆「aa」「111」。组内仍然可以改（那是另一回事，
    同一个账号在不同组确实可能想叫不同名字）。
    """
    s = str(v or "").strip()[:16]
    if s:
        return s
    f = str(fallback or "").strip()[:16]
    if f:
        return f
    p = str(phone or "")
    if len(p) >= 7:
        return p[:3] + "****" + p[-4:]
    return p or "跑者"


def _new_code(c) -> str:
    for _ in range(8):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        if not c.execute("SELECT 1 FROM groups WHERE invite_code=?", (code,)).fetchone():
            return code
    raise GroupError(500, "邀请码生成失败，稍后再试")


def _audit(c, gid: int, actor: int, action: str, target: int | None = None) -> None:
    c.execute("INSERT INTO group_audit(group_id,actor_id,action,target_id,ts) VALUES(?,?,?,?,?)",
              (gid, actor, action, target, _now()))


def _member(c, gid: int, uid: int):
    return c.execute("SELECT * FROM group_members WHERE group_id=? AND user_id=?",
                     (gid, uid)).fetchone()


def _require_member(c, gid: int, uid: int):
    m = _member(c, gid, uid)
    if not m or m["status"] != "active":
        raise GroupError(403, "你不是这个组的成员")
    return m


def _require_admin(c, gid: int, uid: int):
    m = _require_member(c, gid, uid)
    if m["role"] not in ("owner", "admin"):
        raise GroupError(403, "需要组长或管理员")
    return m


def _require_owner(c, gid: int, uid: int):
    m = _require_member(c, gid, uid)
    if m["role"] != "owner":
        raise GroupError(403, "只有组长可以这么做")
    return m


def _group(c, gid: int):
    g = c.execute("SELECT * FROM groups WHERE id=? AND archived=0", (gid,)).fetchone()
    if not g:
        raise GroupError(404, "组不存在或已解散")
    return g


# ---------------------------------------------------------------- 限频
_RATE: dict[tuple[str, str], list[float]] = {}
_RATE_LOCK = threading.Lock()


def _rate_limit(scope: str, action: str, limit: int, window: int = 3600) -> None:
    """进程内滑动窗口。重启清零不影响安全，只影响精度——单实例够用。

    ThreadingHTTPServer 是多线程的，所以必须加锁；否则并发上报能绕过计数。
    """
    now = time.time()
    cut = now - window
    key = (scope, action)
    with _RATE_LOCK:
        bucket = _RATE.setdefault(key, [])
        bucket[:] = [t for t in bucket if t > cut]
        if len(bucket) >= limit:
            raise GroupError(429, "操作太频繁，稍后再试")
        bucket.append(now)
        if len(_RATE) > 4000:      # 顺手清理，避免长期堆积
            for k in [k for k, v in _RATE.items() if not v or v[-1] < cut]:
                _RATE.pop(k, None)


# ---------------------------------------------------------------- 序列化
def _group_brief(g, role: str = "", members: int = 0) -> dict:
    """列表/详情共用的组概要。

    `member_count` 才是人数。`members` 这个键只留给详情页的成员列表——
    两个接口同一个键一个存数字一个存数组，客户端一定会踩。
    """
    return {
        "id": g["id"], "name": g["name"], "intro": g["intro"],
        "owner_id": g["owner_id"], "max_members": g["max_members"],
        "member_count": members, "created_at": g["created_at"],
        "role": role,
    }


def _member_view(m, me_id: int) -> dict:
    return {
        "user_id": m["user_id"],
        "alias": m["alias"],
        "role": m["role"],
        "share": m["share_level"],
        "is_me": m["user_id"] == me_id,
        "joined_at": m["joined_at"],
    }


# ---------------------------------------------------------------- 组
def api_create(me, body: dict) -> dict:
    uid = int(me["user_id"])
    name = str(body.get("name") or "").strip()
    if not 1 <= len(name) <= 20:
        raise GroupError(400, "组名 1–20 个字")
    intro = str(body.get("intro") or "").strip()[:200]
    try:
        cap = int(body.get("max_members") or 50)
    except (TypeError, ValueError):
        raise GroupError(400, "人数上限要是整数")
    if not MAX_MEMBERS_MIN <= cap <= MAX_MEMBERS_MAX:
        raise GroupError(400, f"人数上限 {MAX_MEMBERS_MIN}–{MAX_MEMBERS_MAX}")
    alias = _clean_alias(body.get("alias"), me["phone"],
                         me["nickname"] if "nickname" in me.keys() else "")

    _rate_limit(f"u{uid}", "create", 3, 86400)
    with db() as c:
        # 文档原文是「已在组内时不能建组」。这里放宽成「不能同时当两个组的组长」：
        # 加入朋友的组不该妨碍自己建组，而一个人同时负责两个组确实不现实。
        if c.execute("SELECT 1 FROM groups WHERE owner_id=? AND archived=0", (uid,)).fetchone():
            raise GroupError(409, "你已经是一个组的组长，先解散或转让再建新组")
        n = c.execute("SELECT COUNT(*) FROM group_members WHERE user_id=? AND status='active'",
                      (uid,)).fetchone()[0]
        if n >= MAX_GROUPS_PER_USER:
            raise GroupError(409, f"最多同时加入 {MAX_GROUPS_PER_USER} 个组")

        code = _new_code(c)
        cur = c.execute(
            "INSERT INTO groups(name,intro,owner_id,invite_code,max_members,created_at)"
            " VALUES(?,?,?,?,?,?)", (name, intro, uid, code, cap, _now()))
        gid = int(cur.lastrowid)
        c.execute("INSERT INTO group_members(group_id,user_id,role,alias,joined_at)"
                  " VALUES(?,?,?,?,?)", (gid, uid, "owner", alias, _now()))
        _audit(c, gid, uid, "create")
        g = c.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()
    out = _group_brief(g, "owner", 1)
    out["invite_code"] = g["invite_code"]
    return {"ok": True, "group": out}


def api_join(me, body: dict, ip: str) -> dict:
    uid = int(me["user_id"])
    code = str(body.get("code") or "").strip().upper()
    if len(code) != CODE_LEN:
        raise GroupError(400, "邀请码是 8 位")
    _rate_limit(f"u{uid}", "join", 10)
    _rate_limit(f"ip{ip}", "join", 30)

    with db() as c:
        g = c.execute("SELECT * FROM groups WHERE invite_code=? AND archived=0",
                      (code,)).fetchone()
        if not g:
            raise GroupError(404, "邀请码无效或这个组已解散")
        gid = int(g["id"])
        m = _member(c, gid, uid)
        if m and m["status"] == "active":
            # 幂等：重复扫码不该报错，也不该重置角色
            members = c.execute("SELECT COUNT(*) FROM group_members WHERE group_id=? AND status='active'",
                                (gid,)).fetchone()[0]
            return {"ok": True, "already": True, "group": _group_brief(g, m["role"], members)}

        active = c.execute("SELECT COUNT(*) FROM group_members WHERE group_id=? AND status='active'",
                           (gid,)).fetchone()[0]
        if active >= g["max_members"]:
            raise GroupError(409, f"这个组满了（{g['max_members']} 人）")
        n = c.execute("SELECT COUNT(*) FROM group_members WHERE user_id=? AND status='active'",
                      (uid,)).fetchone()[0]
        if n >= MAX_GROUPS_PER_USER:
            raise GroupError(409, f"最多同时加入 {MAX_GROUPS_PER_USER} 个组")

        alias = _clean_alias(body.get("alias"), me["phone"],
                             me["nickname"] if "nickname" in me.keys() else "")
        if m:
            c.execute("UPDATE group_members SET status='active', alias=?, joined_at=? "
                      "WHERE group_id=? AND user_id=?", (alias, _now(), gid, uid))
        else:
            c.execute("INSERT INTO group_members(group_id,user_id,role,alias,joined_at)"
                      " VALUES(?,?,?,?,?)", (gid, uid, "member", alias, _now()))
        _audit(c, gid, uid, "join")
        members = active + 1
    return {"ok": True, "already": False, "group": _group_brief(g, "member", members)}


def api_me_update(me, gid: int, body: dict) -> dict:
    """改我在这个组里的昵称。任何在组的成员都能改自己，不需要管理员。

    昵称是冗余存在 group_members.alias 里的（不 JOIN users），所以「同一个
    账号在不同组叫不同名字」是天然支持的——这正是组内昵称的意义。
    """
    uid = int(me["user_id"])
    raw = str(body.get("alias") or "").strip()
    # 空值不走 _clean_alias 的兜底：那会回落到「138****0000」这种
    # 打码账号，比原来的昵称更糟，还会漏一半账号出去。
    if not raw:
        raise GroupError(400, "昵称不能为空")
    if len(raw) > 16:
        raise GroupError(400, "昵称最多 16 个字")
    alias = _clean_alias(raw, me["phone"])
    with db() as c:
        m = _require_member(c, gid, uid)
        if m["alias"] == alias:
            return {"ok": True, "alias": alias, "changed": False}
        c.execute("UPDATE group_members SET alias=? WHERE group_id=? AND user_id=?",
                  (alias, gid, uid))
        _audit(c, gid, uid, "alias", uid)
    return {"ok": True, "alias": alias, "changed": True}


def _week_rank(c, gid: int, uid: int) -> dict | None:
    """我在这个组本周排第几。列表页要「一眼看到排名」，不点进去也算得出来。

    只看本周有里程的人：一个人本周还没跑，不该被算成「最后一名」，
    而是「本周还没有记录」。
    """
    wf, wt = _week(_today())
    rows = c.execute(
        "SELECT user_id, SUM(distance_m) m FROM group_daily"
        " WHERE group_id=? AND date BETWEEN ? AND ? GROUP BY user_id",
        (gid, wf.isoformat(), wt.isoformat())).fetchall()
    km = {int(r["user_id"]): float(r["m"] or 0) / 1000.0 for r in rows}
    mine = round(km.get(uid, 0.0), 1)
    if mine <= 0:
        return None
    rank = 1 + sum(1 for v in km.values() if v > mine)
    return {"rank": rank, "total": len(km), "km": mine}


def api_list(me) -> dict:
    uid = int(me["user_id"])
    with db() as c:
        rows = c.execute("""
            SELECT g.*, m.role AS my_role,
                   (SELECT COUNT(*) FROM group_members x
                     WHERE x.group_id=g.id AND x.status='active') AS n
            FROM group_members m JOIN groups g ON g.id=m.group_id
            WHERE m.user_id=? AND m.status='active' AND g.archived=0
            ORDER BY m.joined_at DESC
        """, (uid,)).fetchall()
        out = []
        for r in rows:
            g = _group_brief(r, r["my_role"], r["n"])
            if r["my_role"] in ("owner", "admin"):
                g["invite_code"] = r["invite_code"]
            # 本周名次：列表页直接显示，不用点进去才知道自己第几
            g["week"] = _week_rank(c, int(r["id"]), uid)
            out.append(g)
    return {"ok": True, "groups": out}


def api_get(me, gid: int) -> dict:
    uid = int(me["user_id"])
    with db() as c:
        g = _group(c, gid)
        my = _require_member(c, gid, uid)
        rows = c.execute(
            "SELECT * FROM group_members WHERE group_id=? AND status='active'"
            " ORDER BY CASE role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END, joined_at",
            (gid,)).fetchall()
    out = _group_brief(g, my["role"], len(rows))
    # 邀请码只给管理角色：普通成员拿到就能无限拉人，等于绕过了组长审批
    if my["role"] in ("owner", "admin"):
        out["invite_code"] = g["invite_code"]
    out["me"] = {"alias": my["alias"], "share": my["share_level"]}
    out["members"] = [_member_view(r, uid) for r in rows]
    return {"ok": True, "group": out}


def api_patch(me, gid: int, body: dict) -> dict:
    uid = int(me["user_id"])
    with db() as c:
        g = _group(c, gid)
        my = _require_admin(c, gid, uid)
        sets, args = [], []
        if "name" in body:
            name = str(body.get("name") or "").strip()
            if not 1 <= len(name) <= 20:
                raise GroupError(400, "组名 1–20 个字")
            sets.append("name=?")
            args.append(name)
        if "intro" in body:
            sets.append("intro=?")
            args.append(str(body.get("intro") or "").strip()[:200])
        if "max_members" in body:
            try:
                cap = int(body.get("max_members"))
            except (TypeError, ValueError):
                raise GroupError(400, "人数上限要是整数")
            active = c.execute("SELECT COUNT(*) FROM group_members WHERE group_id=? AND status='active'",
                               (gid,)).fetchone()[0]
            # 不能把上限改到比现有人数还低：那会让已经在组里的人处于非法状态
            if cap < active:
                raise GroupError(400, f"人数上限不能低于当前成员数（{active}）")
            if not MAX_MEMBERS_MIN <= cap <= MAX_MEMBERS_MAX:
                raise GroupError(400, f"人数上限 {MAX_MEMBERS_MIN}–{MAX_MEMBERS_MAX}")
            sets.append("max_members=?")
            args.append(cap)
        if not sets:
            raise GroupError(400, "没有要改的字段")
        args.append(gid)
        c.execute(f"UPDATE groups SET {','.join(sets)} WHERE id=?", args)
        _audit(c, gid, uid, "patch")
        g2 = c.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()
        n = c.execute("SELECT COUNT(*) FROM group_members WHERE group_id=? AND status='active'",
                      (gid,)).fetchone()[0]
    out = _group_brief(g2, my["role"], n)
    if my["role"] in ("owner", "admin"):
        out["invite_code"] = g2["invite_code"]
    return {"ok": True, "group": out}


def api_share(me, gid: int, body: dict) -> dict:
    """共享开关。off 之后不再接收上报，也不出现在榜单里。"""
    uid = int(me["user_id"])
    level = str(body.get("share") or "").strip()
    if level not in ("summary", "off"):
        raise GroupError(400, "share 只能是 summary 或 off")
    with db() as c:
        _require_member(c, gid, uid)
        c.execute("UPDATE group_members SET share_level=? WHERE group_id=? AND user_id=?",
                  (level, gid, uid))
    return {"ok": True, "share": level}


# ---------------------------------------------------------------- 上报
def api_report(me, gid: int, body: dict) -> dict:
    uid = int(me["user_id"])
    days = body.get("days")
    if not isinstance(days, list) or not days:
        raise GroupError(400, "days 要是数组")
    if len(days) > REPORT_MAX_DAYS:
        raise GroupError(400, f"一次最多上报 {REPORT_MAX_DAYS} 天")

    _rate_limit(f"u{uid}", "report", 60)
    rows = []
    clamped = 0
    for d in days:
        if not isinstance(d, dict):
            raise GroupError(400, "days 里每一项要是对象")
        day_d = _date(d.get("date"))
        # 未来日期一律不收。设备时间快一天是最常见的输入错误，而后果不只是
        # 榜单多一行：那些里程会落进后面几幕的窗口，等于提前把格数灌进去，
        # 整季的判定就废了。宁可让他看到一句「检查手机时间」。
        if day_d > _today() + dt.timedelta(days=FUTURE_DAYS):
            raise GroupError(400, f"{day_d.isoformat()} 还没到，"
                                  "不能上报未来的里程（检查手机时间是否准确）")
        day = day_d.isoformat()
        try:
            dist = int(d.get("distance_m") or 0)
            dur = int(d.get("duration_s") or 0)
            acts = int(d.get("acts") or 0)
            elev = int(d.get("elevation_m") or 0)
        except (TypeError, ValueError):
            raise GroupError(400, f"{day} 的数值不合法")
        if not 0 <= dist <= DAY_DIST_MAX_M:
            raise GroupError(400, f"{day} 单日里程超出范围")
        if not 0 <= dur <= DAY_DUR_MAX_S:
            raise GroupError(400, f"{day} 单日时长超出范围")
        if not 0 <= acts <= DAY_ACTS_MAX:
            raise GroupError(400, f"{day} 单日次数超出范围")
        if not 0 <= elev <= DAY_ELEV_MAX:
            raise GroupError(400, f"{day} 单日爬升超出范围")
        # 爬升密度防刷：日报表只有日汇总，没有单次数据，所以按
        # 「爬升 / 里程 ≤ 200 m/km」在门口截断。0 公里的日子不截
        # （室内跑步机没有 GPS，爬升本来就是 0；真报了个数也没法比）。
        if dist > 0:
            cap = int(dist / 1000.0 * _ELEV_PER_KM_MAX)
            if elev > cap:
                elev = cap
                clamped += 1
        # plan_done 三态：null = 当天没有计划 / 拿不到计划，不当成 0
        raw = d.get("plan_done", None)
        plan = None if raw is None else (1 if raw else 0)
        rows.append((gid, uid, day, dist, dur, acts, elev, plan, _now()))

    with db() as c:
        m = _require_member(c, gid, uid)
        if m["share_level"] == "off":
            # 关掉共享的意图就是「别再收我的数据」。写进去再藏起来是自欺欺人。
            return {"ok": True, "saved": 0, "skipped": len(rows), "share": "off"}
        c.executemany("""
            INSERT INTO group_daily(group_id,user_id,date,distance_m,duration_s,acts,elevation_m,plan_done,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(group_id,user_id,date) DO UPDATE SET
                distance_m=excluded.distance_m,
                duration_s=excluded.duration_s,
                acts=excluded.acts,
                elevation_m=excluded.elevation_m,
                plan_done=excluded.plan_done,
                updated_at=excluded.updated_at
        """, rows)
    out = {"ok": True, "saved": len(rows), "skipped": 0, "share": m["share_level"]}
    if clamped:
        # 说出来，不静默改数：客户端传了不合理的爬升，用户该知道被截了
        out["clamped_elev"] = clamped
    return out


# ---------------------------------------------------------------- 周成绩
def api_season(me, gid: int, query: dict) -> dict:
    """本周的六件衫 + 赛季分 + 名次 + 称号。

    「赛季分」目前满分 90：让分赛与预言那 10 分还没做，它们以后是往上加的
    加分项，不是权重（见 ranking.py 顶部说明）。页面别写成 x/100。

    只算开着共享的人——和数据进不来的道理一样：关掉共享就是「别拿我的数据
    做比较」，把名字列进排名等于违背这句话。
    """
    uid = int(me["user_id"])
    today = _today((query.get("today") or [None])[0])
    wf, wt = _week(today)
    pf, pt = wf - dt.timedelta(days=7), wf - dt.timedelta(days=1)

    with db() as c:
        _group(c, gid)
        _require_member(c, gid, uid)
        mem = c.execute(
            "SELECT user_id, alias, share_level FROM group_members"
            " WHERE group_id=? AND status='active' ORDER BY joined_at, user_id",
            (gid,)).fetchall()
        # 一次取回够长的一段（本组最早的记录到现在），在内存里分桶。
        # 组小（<=200 人）、周数有限，比每人两次查询省得多。
        since = c.execute("SELECT MIN(date) d FROM group_daily WHERE group_id=?",
                          (gid,)).fetchone()["d"] or wf.isoformat()
        rows = c.execute(
            "SELECT user_id, date, distance_m, elevation_m, acts, plan_done"
            " FROM group_daily WHERE group_id=? AND date BETWEEN ? AND ?"
            " ORDER BY date", (gid, since, wt.isoformat())).fetchall()

    shared = [m for m in mem if (m["share_level"] or "summary") == "summary"]
    keep = {int(m["user_id"]) for m in shared}
    aliases = {int(m["user_id"]): m["alias"] for m in shared}

    this_w: dict[int, list[dict]] = {u: [] for u in keep}
    prev_w: dict[int, list[dict]] = {u: [] for u in keep}
    weeks: dict[int, set] = {u: set() for u in keep}
    for r in rows:
        u = int(r["user_id"])
        if u not in keep:
            continue
        d = str(r["date"])
        if int(r["acts"] or 0) > 0 or int(r["distance_m"] or 0) > 0:
            try:
                weeks[u].add(dt.date.fromisoformat(d).isocalendar()[:2])
            except ValueError:
                pass
        if wf.isoformat() <= d <= wt.isoformat():
            this_w[u].append(dict(r))
        elif pf.isoformat() <= d <= pt.isoformat():
            prev_w[u].append(dict(r))

    out = ranking.build(this_w, prev_w,
                        {u: len(weeks[u]) for u in keep}, aliases)
    out["ok"] = True
    out["from"] = wf.isoformat()
    out["to"] = wt.isoformat()
    out["me"] = next((p for p in out["people"]
                      if int(p["user_id"]) == uid), None)
    out["hidden"] = len(mem) - len(shared)
    return out


def api_home(me, gid: int, query: dict) -> dict:
    """组页首屏要的四块，一次给完。

    原来是 detail / board / challenges / season 四个请求。每次都是新建 TLS
    连接，实测一次 122 ms——组页光握手就快半秒，手机上更差。合成一个，
    往返从 4 次降到 1 次；数据库那边还是四次查询，但那是本地 SQLite，
    加起来几毫秒，和网络不是一个量级。

    任何一块出错都给 null，不让一个次要板块把整页拖垮。
    """
    def _safe(fn, *a):
        try:
            return fn(*a)
        except Exception as e:      # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    return {"ok": True,
            "detail": _safe(api_get, me, gid),
            "board": _safe(api_board, me, gid, query),
            "challenges": _safe(api_challenge_list, me, gid, query),
            "season": _safe(api_season, me, gid, query)}


# ---------------------------------------------------------------- 榜单
def api_board(me, gid: int, query: dict) -> dict:
    uid = int(me["user_id"])
    rng = (query.get("range") or ["week"])[0]
    today = _today((query.get("today") or [None])[0])
    if rng == "month":
        start, end = _month(today)
    elif rng == "week":
        start, end = _week(today)
    else:
        raise GroupError(400, "range 只能是 week 或 month")
    s, e = start.isoformat(), end.isoformat()

    with db() as c:
        _require_member(c, gid, uid)
        members = c.execute(
            "SELECT * FROM group_members WHERE group_id=? AND status='active'",
            (gid,)).fetchall()
        agg = c.execute("""
            SELECT user_id,
                   SUM(distance_m) AS dist, SUM(duration_s) AS dur, SUM(acts) AS acts,
                   COUNT(*) AS days,
                   SUM(CASE WHEN plan_done=1 THEN 1 ELSE 0 END) AS plan_ok,
                   SUM(CASE WHEN plan_done IS NOT NULL THEN 1 ELSE 0 END) AS plan_total,
                   MAX(updated_at) AS seen
            FROM group_daily
            WHERE group_id=? AND date BETWEEN ? AND ?
            GROUP BY user_id
        """, (gid, s, e)).fetchall()
    by = {int(r["user_id"]): r for r in agg}

    rows, hidden = [], 0
    for m in members:
        if m["share_level"] != "summary":
            hidden += 1
            continue
        a = by.get(int(m["user_id"]))
        plan_total = int(a["plan_total"] or 0) if a else 0
        rows.append({
            "alias": m["alias"],
            "is_me": int(m["user_id"]) == uid,
            "distance_m": int(a["dist"] or 0) if a else 0,
            "duration_s": int(a["dur"] or 0) if a else 0,
            "acts": int(a["acts"] or 0) if a else 0,
            "days": int(a["days"] or 0) if a else 0,
            # 计划完成率：没有计划的那几天不进分母，不伪造 0
            "plan_rate": (round(int(a["plan_ok"] or 0) / plan_total, 3) if plan_total else None),
            "updated_at": int(a["seen"] or 0) if a else 0,
        })
    # 里程优先，其次天数——同里程时「跑得更分散」的人排前面
    rows.sort(key=lambda x: (-x["distance_m"], -x["days"], -x["duration_s"]))
    return {
        "ok": True, "range": rng, "from": s, "to": e,
        "rows": rows, "hidden": hidden,
        "summary": {
            "members": len(members), "shown": len(rows), "hidden": hidden,
            "total_distance_m": sum(x["distance_m"] for x in rows),
        },
    }


# ---------------------------------------------------------------- 成员管理
def api_leave(me, gid: int) -> dict:
    uid = int(me["user_id"])
    with db() as c:
        _group(c, gid)
        m = _require_member(c, gid, uid)
        if m["role"] == "owner":
            raise GroupError(409, "组长不能直接退出，先转让给别人或解散这个组")
        c.execute("UPDATE group_members SET status='left' WHERE group_id=? AND user_id=?",
                  (gid, uid))
        # 退出即删本组内我的明细：留在库里等于「退出了但数据还在」
        c.execute("DELETE FROM group_daily WHERE group_id=? AND user_id=?", (gid, uid))
        _audit(c, gid, uid, "leave")
    return {"ok": True}


def api_remove(me, gid: int, target: int) -> dict:
    uid = int(me["user_id"])
    if target == uid:
        raise GroupError(400, "不能踢自己，用退出")
    with db() as c:
        _group(c, gid)
        _require_admin(c, gid, uid)
        t = _member(c, gid, target)
        if not t or t["status"] != "active":
            raise GroupError(404, "这个人不在组里")
        if t["role"] == "owner":
            raise GroupError(403, "不能踢组长")
        if t["role"] == "admin" and _member(c, gid, uid)["role"] != "owner":
            raise GroupError(403, "只有组长能移除管理员")
        c.execute("UPDATE group_members SET status='removed' WHERE group_id=? AND user_id=?",
                  (gid, target))
        c.execute("DELETE FROM group_daily WHERE group_id=? AND user_id=?", (gid, target))
        _audit(c, gid, uid, "remove", target)
    return {"ok": True}


def api_rotate(me, gid: int) -> dict:
    uid = int(me["user_id"])
    with db() as c:
        _group(c, gid)
        _require_admin(c, gid, uid)
        _rate_limit(f"g{gid}", "rotate", 10, 86400)
        code = _new_code(c)
        c.execute("UPDATE groups SET invite_code=? WHERE id=?", (code, gid))
        _audit(c, gid, uid, "rotate")
    return {"ok": True, "invite_code": code}


def api_transfer(me, gid: int, body: dict) -> dict:
    uid = int(me["user_id"])
    try:
        target = int(body.get("user_id"))
    except (TypeError, ValueError):
        raise GroupError(400, "缺少 user_id")
    with db() as c:
        _group(c, gid)
        _require_owner(c, gid, uid)
        t = _member(c, gid, target)
        if not t or t["status"] != "active":
            raise GroupError(404, "这个人不在组里")
        if target == uid:
            raise GroupError(400, "已经是组长了")
        c.execute("UPDATE group_members SET role='owner' WHERE group_id=? AND user_id=?", (gid, target))
        c.execute("UPDATE group_members SET role='admin' WHERE group_id=? AND user_id=?", (gid, uid))
        c.execute("UPDATE groups SET owner_id=? WHERE id=?", (target, gid))
        _audit(c, gid, uid, "transfer", target)
    return {"ok": True, "owner_id": target}


def api_archive(me, gid: int, body: dict) -> dict:
    uid = int(me["user_id"])
    with db() as c:
        _group(c, gid)
        _require_owner(c, gid, uid)
        # 解散是不可逆的，要求显式确认，防止误点
        if str(body.get("confirm") or "") != "1":
            raise GroupError(400, "解散需要二次确认（confirm=1）")
        c.execute("UPDATE groups SET archived=1 WHERE id=?", (gid,))
        _audit(c, gid, uid, "archive")
    return {"ok": True}


# ---------------------------------------------------------------- 赛事
def _challenge_view(ch, today: dt.date, current: int | None = None) -> dict:
    closed = ch["closed_at"] is not None
    ended = str(ch["end_date"]) < today.isoformat()
    status = "closed" if closed else ("ended" if ended else "running")
    out = {
        "id": ch["id"], "title": ch["title"], "kind": ch["kind"],
        "target": ch["target"], "from": ch["start_date"], "to": ch["end_date"],
        "status": status, "created_by": ch["created_by"],
    }
    if current is not None:
        out["current"] = current
        # 分母为 0 时也要返回小数：`else 0` 是 int，而正常路径是 float，
        # 同一个字段两种类型会让客户端强类型模型直接崩。
        out["pct"] = min(100.0, round(current * 100 / ch["target"], 1)) if ch["target"] else 0.0
    return out


def api_challenge_create(me, gid: int, body: dict) -> dict:
    uid = int(me["user_id"])
    title = str(body.get("title") or "").strip()
    if not 1 <= len(title) <= 30:
        raise GroupError(400, "标题 1–30 个字")
    kind = str(body.get("kind") or "").strip()
    if kind not in ("distance", "acts"):
        raise GroupError(400, "kind 只能是 distance 或 acts")
    try:
        target = int(body.get("target"))
    except (TypeError, ValueError):
        raise GroupError(400, "target 要是整数")
    lo, hi = ((CHALLENGE_DIST_MIN, CHALLENGE_DIST_MAX) if kind == "distance"
              else (CHALLENGE_ACTS_MIN, CHALLENGE_ACTS_MAX))
    if not lo <= target <= hi:
        raise GroupError(400, f"目标要在 {lo}–{hi} 之间")

    today = _today(body.get("today"))
    start = _date(body.get("start_date"), "start_date")
    end = _date(body.get("end_date"), "end_date")
    if end < start:
        raise GroupError(400, "结束日不能早于开始日")
    if (end - start).days + 1 > CHALLENGE_MAX_DAYS:
        raise GroupError(400, f"周期不能超过 {CHALLENGE_MAX_DAYS} 天")
    if start < today - dt.timedelta(days=7):
        raise GroupError(400, "开始日不能早于 7 天前")

    with db() as c:
        _group(c, gid)
        _require_admin(c, gid, uid)
        _rate_limit(f"g{gid}", "challenge", 5, 86400)
        running = c.execute("""
            SELECT COUNT(*) FROM group_challenges
            WHERE group_id=? AND archived=0 AND closed_at IS NULL AND end_date >= ?
        """, (gid, today.isoformat())).fetchone()[0]
        if running >= CHALLENGE_MAX_RUNNING:
            raise GroupError(409, f"同时进行的赛事已达 {CHALLENGE_MAX_RUNNING} 个")
        cur = c.execute("""
            INSERT INTO group_challenges(group_id,title,kind,target,start_date,end_date,
                                         created_by,created_at)
            VALUES(?,?,?,?,?,?,?,?)
        """, (gid, title, kind, target, start.isoformat(), end.isoformat(), uid, _now()))
        cid = int(cur.lastrowid)
        _audit(c, gid, uid, "challenge_create", cid)
        ch = c.execute("SELECT * FROM group_challenges WHERE id=?", (cid,)).fetchone()
    return {"ok": True, "challenge": _challenge_view(ch, today, 0)}


def api_challenge_list(me, gid: int, query: dict) -> dict:
    uid = int(me["user_id"])
    today = _today((query.get("today") or [None])[0])
    with db() as c:
        _require_member(c, gid, uid)
        rows = c.execute("""
            SELECT * FROM group_challenges WHERE group_id=? AND archived=0
            ORDER BY (closed_at IS NULL AND end_date >= ?) DESC, id DESC
        """, (gid, today.isoformat())).fetchall()
        # 每个赛事的全组进度，一次查完再分组，避免 N+1
        prog = c.execute("""
            SELECT challenge_id, SUM(cur) AS cur FROM (
              SELECT ch.id AS challenge_id,
                     COALESCE((SELECT SUM(CASE WHEN ch.kind='distance' THEN d.distance_m
                                               ELSE d.acts END)
                                 FROM group_daily d JOIN group_members m
                                   ON m.group_id=d.group_id AND m.user_id=d.user_id
                                  AND m.status='active' AND m.share_level='summary'
                                WHERE d.group_id=ch.group_id
                                  AND d.date BETWEEN ch.start_date AND ch.end_date), 0) AS cur
                FROM group_challenges ch WHERE ch.group_id=? AND ch.archived=0
            ) GROUP BY challenge_id
        """, (gid,)).fetchall()
    cur_by = {int(r["challenge_id"]): int(r["cur"] or 0) for r in prog}
    return {"ok": True,
            "challenges": [_challenge_view(r, today, cur_by.get(int(r["id"]), 0)) for r in rows]}


def api_challenge_get(me, gid: int, cid: int, query: dict) -> dict:
    uid = int(me["user_id"])
    today = _today((query.get("today") or [None])[0])
    with db() as c:
        _require_member(c, gid, uid)
        ch = c.execute("SELECT * FROM group_challenges WHERE id=? AND group_id=? AND archived=0",
                       (cid, gid)).fetchone()
        if not ch:
            raise GroupError(404, "赛事不存在")
        members = c.execute("SELECT * FROM group_members WHERE group_id=? AND status='active'",
                            (gid,)).fetchall()
        raw = c.execute("""
            SELECT user_id, date, distance_m, acts FROM group_daily
            WHERE group_id=? AND date BETWEEN ? AND ?
            ORDER BY user_id, date
        """, (gid, ch["start_date"], ch["end_date"])).fetchall()

    col = "distance_m" if ch["kind"] == "distance" else "acts"
    target = int(ch["target"])
    acc: dict[int, int] = {}
    done_at: dict[int, str] = {}
    for r in raw:
        u = int(r["user_id"])
        acc[u] = acc.get(u, 0) + int(r[col] or 0)
        # 首次达标日：按日累计第一次 >= 目标的那天
        if u not in done_at and acc[u] >= target:
            done_at[u] = r["date"]

    rows, hidden = [], 0
    for m in members:
        if m["share_level"] != "summary":
            hidden += 1
            continue
        u = int(m["user_id"])
        cur = acc.get(u, 0)
        rows.append({
            "alias": m["alias"], "is_me": u == uid,
            "current": cur,
            "pct": min(100.0, round(cur * 100 / target, 1)) if target else 0.0,
            "done_at": done_at.get(u),
        })
    # 已达成按达成日期升序，未达成按完成率降序
    rows.sort(key=lambda x: (x["done_at"] is None, x["done_at"] or "", -x["pct"]))
    return {"ok": True, "challenge": _challenge_view(ch, today), "rows": rows, "hidden": hidden}


def api_challenge_close(me, gid: int, cid: int) -> dict:
    uid = int(me["user_id"])
    with db() as c:
        _require_admin(c, gid, uid)
        ch = c.execute("SELECT * FROM group_challenges WHERE id=? AND group_id=? AND archived=0",
                       (cid, gid)).fetchone()
        if not ch:
            raise GroupError(404, "赛事不存在")
        if ch["closed_at"] is None:
            c.execute("UPDATE group_challenges SET closed_at=? WHERE id=?", (_now(), cid))
            _audit(c, gid, uid, "challenge_close", cid)
        ch2 = c.execute("SELECT * FROM group_challenges WHERE id=?", (cid,)).fetchone()
    return {"ok": True, "challenge": _challenge_view(ch2, dt.date.today())}


# ---------------------------------------------------------------- 分发
def handle(method: str, raw_path: str, headers, body, auth) -> tuple[dict, int]:
    """返回 (payload, status_code)。异常在这里统一转成 JSON，不往上抛。"""
    body = body if isinstance(body, dict) else {}
    try:
        u = urlsplit(raw_path)
        parts = [p for p in u.path.strip("/").split("/") if p]
        if len(parts) < 2 or parts[0] != "v1" or parts[1] != "group":
            raise GroupError(404, "没有这个接口")
        rest = parts[2:]
        query = parse_qs(u.query)

        me = auth(headers)
        uid = int(me["user_id"])
        ip = (headers.get("X-Forwarded-For") or "").split(",")[0].strip() or "-"

        # ---- 无组 id 的入口
        if not rest:
            raise GroupError(404, "没有这个接口")
        head = rest[0]
        if head == "create" and method == "POST":
            return api_create(me, body), 200
        if head == "join" and method == "POST":
            return api_join(me, body, ip), 200
        if head == "list" and method == "GET":
            return api_list(me), 200
        if head in ("create", "join", "list"):
            raise GroupError(405, "方法不对")

        try:
            gid = int(head)
        except ValueError:
            raise GroupError(404, "没有这个接口")

        # ---- /v1/group/{id}[...]
        tail = rest[1:]
        if not tail:
            if method == "GET":
                return api_get(me, gid), 200
            if method == "PATCH":
                return api_patch(me, gid, body), 200
            raise GroupError(405, "方法不对")

        action = tail[0]
        if action == "report" and method == "POST":
            return api_report(me, gid, body), 200
        if action == "board" and method == "GET":
            return api_board(me, gid, query), 200
        if action == "season" and method == "GET":
            return api_season(me, gid, query), 200
        if action == "home" and method == "GET":
            return api_home(me, gid, query), 200
        if action == "leave" and method == "POST":
            return api_leave(me, gid), 200
        if action == "share" and method == "POST":
            return api_share(me, gid, body), 200
        if action == "me" and method == "POST":
            return api_me_update(me, gid, body), 200
        if action == "transfer" and method == "POST":
            return api_transfer(me, gid, body), 200
        if action == "archive" and method == "POST":
            return api_archive(me, gid, body), 200
        if action == "invite" and len(tail) >= 2 and tail[1] == "rotate" and method == "POST":
            return api_rotate(me, gid), 200
        if action == "member" and len(tail) >= 3 and tail[2] == "remove" and method == "POST":
            try:
                target = int(tail[1])
            except ValueError:
                raise GroupError(400, "成员 id 不合法")
            return api_remove(me, gid, target), 200
        if action == "challenge":
            sub = tail[1] if len(tail) >= 2 else ""
            if sub == "create" and method == "POST":
                return api_challenge_create(me, gid, body), 200
            if sub == "list" and method == "GET":
                return api_challenge_list(me, gid, query), 200
            if sub and sub.isdigit():
                cid = int(sub)
                if len(tail) >= 3 and tail[2] == "close" and method == "POST":
                    return api_challenge_close(me, gid, cid), 200
                if method == "GET":
                    return api_challenge_get(me, gid, cid, query), 200
        raise GroupError(404, "没有这个接口")
    except GroupError as e:
        return {"ok": False, "error": e.message}, e.code
    except sqlite3.Error as e:
        # 库坏了要能被发现，不要吞成「操作失败」——部署后这是最容易掩盖的问题
        return {"ok": False, "error": f"数据库错误：{e}"}, 500
    except Exception as e:  # noqa: BLE001
        # app.py 的 Gate 也会走到这里（鉴权失败时 _auth 抛的就是它）。
        # 不能直接 import Gate——会形成循环依赖；按鸭子类型认它：
        # 有整数 code 和字符串 message 的异常，原样透出状态码。
        # 不然 401 会被吞成 500，客户端永远不知道是「该登录了」。
        code = getattr(e, "code", None)
        msg = getattr(e, "message", None)
        if isinstance(code, int) and isinstance(msg, str):
            return {"ok": False, "error": msg}, code
        # 其余带 traceback 打到 stderr：线上排查全靠这个，别只回一句「TypeError」
        traceback.print_exc()
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}, 500
