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

import base64
import binascii
import datetime as dt
import json
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
PENDING_MAX = 20               # 一个组最多积压 20 条加入申请（不占人数上限）
# 营地的数值口径见 docs/用户组设计.md 11.4。休整点：每人每周最多 4 点，
# 团队池按 40 × 有效成员封顶，升级成本按人均 × 人数缩放（大组升级不该更快）。
VIGOR_WEEK_MAX = 4
CAMP_POOL_PER_MEMBER = 40
# 营地五级。文案必须和 expedition.py 的实际结算一致：
# Lv1 让首幕补给从 3 变 4；Lv2 让首幕支线多一条；Lv3 把「连续没动就剔除」
# 的门槛从 2 幕放宽到 3 幕；Lv4 每天贡献上限 +1 km；Lv5 每幕支线再多一条。
# 早期写的是「揭示前 2 格」「事件 3 选 1 变 4 选 1」，那是还没实现时的设想，
# 和代码对不上，用户升级后看不到对应变化，所以按实现改写。
CAMP_LEVELS = [
    ("宿营", "开局补给 4 点", 1),
    ("侦察", "首幕多一条支线", 3),
    ("医师", "缺勤三幕才剔除", 5),
    ("驮马", "每日贡献上限 +1 km", 7),
    ("传令", "每幕多一条支线", 10),
]
REPORT_MAX_DAYS = 31           # 一次最多报 31 天（首次加入补 30 天足够）
DAY_DIST_MAX_M = 200_000       # 单日 200 km，超过直接拒（不是判作弊，是防脏数据）
DAY_DUR_MAX_S = 24 * 3600
DAY_ACTS_MAX = 20
DAY_ELEV_MAX = 8000          # 单日爬升 8000 m，超过直接拒（防脏数据）
_ELEV_PER_KM_MAX = 200.0      # 爬升密度上限（m/km），日报表只能按这个截断
# 探索徽章：单日新增格数上限。和客户端 explore.DAY_CAP 必须一致
# （客户端封顶后再传，这里再截一次——两边都守一遍，客户端被改坏也不会脏库）。
DAY_EXPLORER_MAX = 50

CHALLENGE_MAX_DAYS = 90
CHALLENGE_MAX_RUNNING = 3
CHALLENGE_DIST_MIN, CHALLENGE_DIST_MAX = 1_000, 1_000_000    # 1–1000 km
CHALLENGE_ACTS_MIN, CHALLENGE_ACTS_MAX = 1, 200
CHALLENGE_ELEV_MIN, CHALLENGE_ELEV_MAX = 100, 300_000        # 累计爬升 100 m–300 km
CHALLENGE_CELL_MIN, CHALLENGE_CELL_MAX = 5, 5_000            # 新踩格子数
# 速度赛事比的是「全组平均配速」，target 存秒/公里（5'30" = 330）。
# 下限 2'30" 是防手滑（填 30 会变成「必须 30 秒一公里」，永远达不成）。
CHALLENGE_PACE_MIN, CHALLENGE_PACE_MAX = 150, 900
# kind → (下限字段, 上限字段, 中文单位)
CHALLENGE_KINDS = {
    "distance": (CHALLENGE_DIST_MIN, CHALLENGE_DIST_MAX, "km"),
    "acts": (CHALLENGE_ACTS_MIN, CHALLENGE_ACTS_MAX, "次"),
    "elevation": (CHALLENGE_ELEV_MIN, CHALLENGE_ELEV_MAX, "m"),
    "explore": (CHALLENGE_CELL_MIN, CHALLENGE_CELL_MAX, "格"),
    "speed": (CHALLENGE_PACE_MIN, CHALLENGE_PACE_MAX, "秒/km"),
}

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
          -- 当日新踩的 500 m 格数（探索徽章）。只有整数上来，格子 ID 留在手机。
          explorer     INTEGER NOT NULL DEFAULT 0,
          -- 当日骑行里程（米）。只服务月榜的「综合榜」（骑行 4 km 折算 1 km）。
          -- 跑步仍记在 distance_m，两者不重叠，所以综合 = distance_m + ride_m/4。
          -- 老客户端不传 → 0：结果是**只漏骑行**，不会把骑行误算成跑步，
          -- 所以历史行和旧客户端都不会污染榜单。
          ride_m       INTEGER NOT NULL DEFAULT 0,
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

        -- 本周目标（P2 让分赛与预言的基准）。周一报一次，本周内可改，
        -- 结算看最后一条。存 km 是因为这是给人看的数，不是里程账。
        CREATE TABLE IF NOT EXISTS group_goal(
          group_id   INTEGER NOT NULL,
          user_id    INTEGER NOT NULL,
          week_start TEXT NOT NULL,
          km         REAL NOT NULL,
          source     TEXT NOT NULL DEFAULT 'manual',
          created_at INTEGER NOT NULL,
          PRIMARY KEY(group_id, user_id, week_start)
        );

        -- 让分赛结对：一周一组关系。small 按自报跑，big 要让分。
        CREATE TABLE IF NOT EXISTS group_pair(
          group_id   INTEGER NOT NULL,
          week_start TEXT NOT NULL,
          slot       INTEGER NOT NULL,
          small_uid  INTEGER NOT NULL,
          big_uid    INTEGER NOT NULL,
          PRIMARY KEY(group_id, week_start, slot)
        );

        -- 预言投票：本周猜「谁完不成」。一人一周一票，可改。
        CREATE TABLE IF NOT EXISTS group_predict(
          group_id   INTEGER NOT NULL,
          week_start TEXT NOT NULL,
          user_id    INTEGER NOT NULL,
          target_uid INTEGER NOT NULL,
          created_at INTEGER NOT NULL,
          PRIMARY KEY(group_id, week_start, user_id)
        );

        -- 组内路线图：每人本周跑过的 500 m 格子。
        -- **只存格子，不存轨迹**——格号粗一个数量级，还原不出你走的是哪条人行道。
        -- 只在该成员主动打开 map_share 后才会有行；关掉就整周删干净。
        CREATE TABLE IF NOT EXISTS group_cell(
          group_id   INTEGER NOT NULL,
          week_start TEXT NOT NULL,
          user_id    INTEGER NOT NULL,
          cell       TEXT NOT NULL,
          -- 这格里跑了多少米。0 表示老客户端只发了「跑过」没发跑量
          meters     INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(group_id, week_start, user_id, cell)
        );

        -- 活动概要（组内动态瀑布流）。**只有概要**：日期、类型、距离、时长、
        -- 爬升。没有轨迹、没有配速、没有心率、没有活动名称——组页承诺的
        -- 「不上传轨迹和配速」在这张表上依然成立，也不出图。
        CREATE TABLE IF NOT EXISTS group_act(
          id          INTEGER PRIMARY KEY,
          group_id    INTEGER NOT NULL,
          user_id     INTEGER NOT NULL,
          sid         TEXT NOT NULL,     -- 客户端算的短指纹，只用于去重
          date        TEXT NOT NULL,
          kind        TEXT NOT NULL DEFAULT 'run',
          distance_m  INTEGER NOT NULL DEFAULT 0,
          duration_s  INTEGER NOT NULL DEFAULT 0,
          elevation_m INTEGER NOT NULL DEFAULT 0,
          -- 脱敏剪影："x,y;x,y;..."，0..100 的相对坐标，不含经纬度
          shape       TEXT NOT NULL DEFAULT '',
          created_at  INTEGER NOT NULL,
          UNIQUE(group_id, user_id, sid)
        );
        CREATE INDEX IF NOT EXISTS idx_gact_group ON group_act(group_id, date DESC);

        -- 评论：一句话 + 一个表情。表情只收白名单里的几个，
        -- 不然这里会变成贴图区，审核和存储都失控。
        CREATE TABLE IF NOT EXISTS group_run_camp(
          group_id   INTEGER PRIMARY KEY,
          level      INTEGER NOT NULL DEFAULT 1,
          spent      INTEGER NOT NULL DEFAULT 0,   -- 已花掉的休整点
          updated_at INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS group_comment(
          id         INTEGER PRIMARY KEY,
          group_id   INTEGER NOT NULL,
          act_id     INTEGER NOT NULL,
          user_id    INTEGER NOT NULL,
          text       TEXT NOT NULL DEFAULT '',
          emoji      TEXT NOT NULL DEFAULT '',
          created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_gcmt_act ON group_comment(act_id, id);
        -- 优质路线分享：用户**主动授权**才把某条活动的完整轨迹传上来。
        -- 和「只传 500m 格子」是两条并行的口径：格子永远归一化、不带位置；
        -- 这张表里的 points 是真经纬度，所以默认没有任何行，撤回即删。
        CREATE TABLE IF NOT EXISTS group_route(
          id         INTEGER PRIMARY KEY,
          group_id   INTEGER NOT NULL,
          user_id    INTEGER NOT NULL,
          act_id     TEXT NOT NULL DEFAULT '',
          title      TEXT NOT NULL DEFAULT '',
          distance_m INTEGER NOT NULL DEFAULT 0,
          date       TEXT NOT NULL DEFAULT '',
          points     TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          UNIQUE(group_id, user_id, act_id)
        );
        CREATE INDEX IF NOT EXISTS idx_groute ON group_route(group_id, id);

        -- 路线批注：作者在路线图上钉一个点，可带一张照片和一段文字。
        -- 位置用 0..1 的相对坐标，不用经纬度：批注钉在**渲染出来的那张地图**上，
        -- 而底图是按点集重新取景的，存经纬度会在换底图后飘到别处。
        CREATE TABLE IF NOT EXISTS group_route_note(
          id         INTEGER PRIMARY KEY,
          group_id   INTEGER NOT NULL,
          route_id   INTEGER NOT NULL,
          user_id    INTEGER NOT NULL,
          x          REAL NOT NULL DEFAULT 0.5,
          y          REAL NOT NULL DEFAULT 0.5,
          text       TEXT NOT NULL DEFAULT '',
          -- 照片存文件名（相对 ROUTE_IMG_DIR），不存 BLOB：
          -- 放进 SQLite 会让备份、VACUUM、每条查询都跟着变重。
          image      TEXT NOT NULL DEFAULT '',
          created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_grnote ON group_route_note(route_id, id);
        """)
        # 老库补列。SQLite 的 ADD COLUMN 支持带默认值，已有行会拿到 0；
        # 不做数据回填——0 就是「那几天还没有这个字段」，回填成别的值是编数据。
        cols = {r["name"] for r in c.execute("PRAGMA table_info(group_daily)")}
        if "elevation_m" not in cols:
            c.execute("ALTER TABLE group_daily ADD COLUMN elevation_m INTEGER"
                      " NOT NULL DEFAULT 0")
        if "explorer" not in cols:
            c.execute("ALTER TABLE group_daily ADD COLUMN explorer INTEGER"
                      " NOT NULL DEFAULT 0")
        # 骑行里程：只在「综合榜」里用。默认 0 表示「这一版客户端还没传」，
        # 综合距离会因此少算骑行，但绝不会把骑行当跑步多算。
        if "ride_m" not in cols:
            c.execute("ALTER TABLE group_daily ADD COLUMN ride_m INTEGER"
                      " NOT NULL DEFAULT 0")
        # 路线图开关默认 **开**（2026-11-23 产品决策，原先是关）。
        # 位置比里程敏感，所以这条默认值必须有配套的「随时可关、关掉即删」：
        # 关掉时 group_cell 整周数据会被 DELETE，不只是停止上传。
        mcols = {r["name"] for r in c.execute("PRAGMA table_info(group_members)")}
        if "map_share" not in mcols:
            c.execute("ALTER TABLE group_members ADD COLUMN map_share INTEGER"
                      " NOT NULL DEFAULT 1")
        # 「帮队友刷新佳明登录态」的志愿开关，**默认关**。
        # 开了以后，这个组里那些没有安卓设备的人（iPhone 用户）登录态过期时，
        # 服务器可以把「用他的账号登一次」的任务派给本机。
        # 默认关是因为这会临时拿到别人的佳明密码——必须是本人明确点开的东西。
        if "relay_share" not in mcols:
            c.execute("ALTER TABLE group_members ADD COLUMN relay_share INTEGER"
                      " NOT NULL DEFAULT 0")
        # 1.5.8 起格子带跑量：地图要用它决定「这块主要是谁跑的」和颜色深浅
        ccols = {r["name"] for r in c.execute("PRAGMA table_info(group_cell)")}
        if "meters" not in ccols:
            c.execute("ALTER TABLE group_cell ADD COLUMN meters INTEGER"
                      " NOT NULL DEFAULT 0")
        # 1.5.11 起动态带轨迹剪影。老行是空串 =「那几天还没有这个字段」，
        # 客户端按「没有剪影」处理（显示类型占位图），不回填、不编造。
        acols = {r["name"] for r in c.execute("PRAGMA table_info(group_act)")}
        if "shape" not in acols:
            c.execute("ALTER TABLE group_act ADD COLUMN shape TEXT"
                      " NOT NULL DEFAULT ''")
        # 1.6.0 起组长可选「加入需批准」。默认 open：老组不该因为加了个字段
        # 就突然把队友挡在门外。
        # 1.6.0 起日报带休整点（0/1），营地池按「累计获得 - 已花」算
        dcols = {r["name"] for r in c.execute("PRAGMA table_info(group_daily)")}
        if "vigor" not in dcols:
            c.execute("ALTER TABLE group_daily ADD COLUMN vigor INTEGER"
                      " NOT NULL DEFAULT 0")
        gcols = {r["name"] for r in c.execute("PRAGMA table_info(groups)")}
        if "join_mode" not in gcols:
            c.execute("ALTER TABLE groups ADD COLUMN join_mode TEXT"
                      " NOT NULL DEFAULT 'open'")


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


# 教练能做的事：建赛事、结束赛事、看审计、升级营地。
# 刻意**不含**人事（审批加入、踢人、改名、换邀请码）——教练在跑团里负责
# 训练安排，不该决定谁进谁出。这条边界写在这里，别在接口里各自判断。
STAFF_ROLES = ("owner", "admin", "coach")


def _require_staff(c, gid: int, uid: int):
    m = _require_member(c, gid, uid)
    if m["role"] not in STAFF_ROLES:
        raise GroupError(403, "需要教练及以上的角色")
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
    out = {
        "id": g["id"], "name": g["name"], "intro": g["intro"],
        "owner_id": g["owner_id"], "max_members": g["max_members"],
        "member_count": members, "created_at": g["created_at"],
        "role": role,
    }
    # 老库没有这一列时按 open 处理，不让客户端因为缺字段而崩
    try:
        out["join_mode"] = str(g["join_mode"] or "open")
    except (IndexError, KeyError):
        out["join_mode"] = "open"
    return out


def _member_view(m, me_id: int) -> dict:
    return {
        "user_id": m["user_id"],
        "alias": m["alias"],
        "role": m["role"],
        "share": m["share_level"],
        # 路线图开关。默认 0；只有 1 的人才会出现在组内地图上。
        "map_share": bool(m["map_share"]) if "map_share" in m.keys() else False,
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
        # 只数「还没解散」的组成员身份：解散过的组仍留着历史行，
        # 算进来的话「解散了却还是进不了新组」——这条曾经把组长逼进死结。
        n = c.execute(
            "SELECT COUNT(*) FROM group_members m JOIN groups g ON g.id=m.group_id"
            " WHERE m.user_id=? AND m.status='active' AND g.archived=0",
            (uid,)).fetchone()[0]
        if n >= MAX_GROUPS_PER_USER:
            raise GroupError(409, f"最多同时加入 {MAX_GROUPS_PER_USER} 个组")

        code = _new_code(c)
        cur = c.execute(
            "INSERT INTO groups(name,intro,owner_id,invite_code,max_members,created_at)"
            " VALUES(?,?,?,?,?,?)", (name, intro, uid, code, cap, _now()))
        gid = int(cur.lastrowid)
        # map_share 显式写 1：线上库的列定义早就固定成 DEFAULT 0 了，
        # 改 ALTER 里的默认值只影响「新建列」，不改已有库的行为。
        c.execute("INSERT INTO group_members(group_id,user_id,role,alias,joined_at,"
                  "map_share) VALUES(?,?,?,?,?,1)",
                  (gid, uid, "owner", alias, _now()))
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
        # 计数必须排除已解散的组：解散只是归档，成员行还在，
        # 不排除的话「最多 3 个组」会被一个界面上看不见的组永久占着。
        n = c.execute(
            "SELECT COUNT(*) FROM group_members m JOIN groups g ON g.id=m.group_id"
            " WHERE m.user_id=? AND m.status='active' AND g.archived=0",
            (uid,)).fetchone()[0]
        if n >= MAX_GROUPS_PER_USER:
            raise GroupError(409, f"最多同时加入 {MAX_GROUPS_PER_USER} 个组")

        alias = _clean_alias(body.get("alias"), me["phone"],
                             me["nickname"] if "nickname" in me.keys() else "")
        # 加入需批准：先挂 pending，组长批了才算进组。pending 不占人数上限
        # （否则一个人反复申请就能把组撑满），但也不能无限堆。
        pending = str(g["join_mode"] or "open") == "approve"
        if pending:
            waiting = c.execute(
                "SELECT COUNT(*) FROM group_members WHERE group_id=? AND status='pending'",
                (gid,)).fetchone()[0]
            if waiting >= PENDING_MAX:
                raise GroupError(409, "等待批准的申请太多了，让组长先处理一下")
            status = "pending"
        else:
            status = "active"
        if m:
            c.execute("UPDATE group_members SET status=?, alias=?, joined_at=?"
                      " WHERE group_id=? AND user_id=?", (status, alias, _now(), gid, uid))
        else:
            c.execute("INSERT INTO group_members(group_id,user_id,role,alias,"
                      "joined_at,status,map_share)"
                      " VALUES(?,?,?,?,?,?,1)",
                      (gid, uid, "member", alias, _now(), status))
        _audit(c, gid, uid, "apply" if pending else "join")
        members = active + (0 if pending else 1)
    return {"ok": True, "already": False, "pending": pending,
            "group": _group_brief(g, "member", members)}


def api_pending(me, gid: int) -> dict:
    """待批准的申请（只有 owner/admin 看得到）。"""
    uid = int(me["user_id"])
    with db() as c:
        _require_admin(c, gid, uid)
        rows = c.execute("SELECT user_id, alias, joined_at FROM group_members"
                         " WHERE group_id=? AND status='pending' ORDER BY joined_at",
                         (gid,)).fetchall()
    return {"ok": True,
            "pending": [{"user_id": int(r["user_id"]), "alias": r["alias"],
                         "at": int(r["joined_at"])} for r in rows]}


def api_decide(me, gid: int, target: int, ok: bool) -> dict:
    """组长批准 / 拒绝一条加入申请。"""
    uid = int(me["user_id"])
    target = int(target)
    with db() as c:
        _require_admin(c, gid, uid)
        m = _member(c, gid, target)
        if not m or m["status"] != "pending":
            raise GroupError(404, "没有这条申请")
        if ok:
            g = _group(c, gid)
            active = c.execute("SELECT COUNT(*) FROM group_members"
                               " WHERE group_id=? AND status='active'", (gid,)).fetchone()[0]
            if active >= g["max_members"]:
                raise GroupError(409, f"这个组满了（{g['max_members']} 人）")
            c.execute("UPDATE group_members SET status='active', joined_at=?"
                      " WHERE group_id=? AND user_id=?", (_now(), gid, target))
        else:
            c.execute("DELETE FROM group_members WHERE group_id=? AND user_id=?",
                      (gid, target))
        _audit(c, gid, uid, "approve" if ok else "reject", target)
    return {"ok": True}


def _camp_state(c, gid: int) -> dict:
    """在一个已有连接上读营地。别在这里再开 db()——升级走的是写事务，
    嵌套开连接会撞 database is locked。"""
    gid = int(gid)
    mem = c.execute("SELECT COUNT(*) FROM group_members"
                    " WHERE group_id=? AND status='active'", (gid,)).fetchone()[0]
    mem = max(1, int(mem))
    row = c.execute("SELECT level, spent FROM group_run_camp WHERE group_id=?",
                    (gid,)).fetchone()
    level = int(row["level"]) if row else 1
    spent = int(row["spent"]) if row else 0
    earned = int(c.execute("SELECT COALESCE(SUM(vigor),0) FROM group_daily"
                           " WHERE group_id=?", (gid,)).fetchone()[0])
    cap = CAMP_POOL_PER_MEMBER * mem
    gross = earned - spent
    if gross > cap:
        # 溢出即丢弃：写回 spent，下次读到就是满池
        c.execute("INSERT INTO group_run_camp(group_id,level,spent,updated_at)"
                  " VALUES(?,?,?,?) ON CONFLICT(group_id) DO UPDATE SET"
                  " spent=excluded.spent, updated_at=excluded.updated_at",
                  (gid, level, earned - cap, _now()))
        spent = earned - cap
        gross = cap
    pool = max(0, gross)
    level = max(1, min(len(CAMP_LEVELS), level))
    nxt = CAMP_LEVELS[level] if level < len(CAMP_LEVELS) else None
    cost = (nxt[2] * mem) if nxt else 0
    return {
        "level": level,
        "name": CAMP_LEVELS[level - 1][0],
        "effect": CAMP_LEVELS[level - 1][1],
        "pool": pool, "cap": cap, "earned": earned, "spent": spent,
        "members": mem,
        "next": ({"name": nxt[0], "effect": nxt[1], "cost": cost,
                  "afford": pool >= cost} if nxt else None),
        "levels": [{"lv": i + 1, "name": n[0], "effect": n[1],
                    "cost": n[2] * mem, "got": i + 1 <= level}
                   for i, n in enumerate(CAMP_LEVELS)],
    }


def camp_state(gid: int) -> dict:
    """营地当前状态（只读入口）。账本 = 累计获得 - 已花，池按 40 × 人数封顶。"""
    with db() as c:
        return _camp_state(c, gid)


def api_camp_upgrade(me, gid: int) -> dict:
    """花休整点升一级营地。只有 owner/admin 能点（钱的入口只给管理者）。"""
    uid = int(me["user_id"])
    with db() as c:
        _require_staff(c, gid, uid)
        st = _camp_state(c, gid)      # 同一个连接：先结算溢出，再扣点
        nxt = st.get("next")
        if not nxt:
            raise GroupError(409, "营地已经满级")
        if st["pool"] < nxt["cost"]:
            raise GroupError(409, f"休整点不够（还差 {nxt['cost'] - st['pool']} 点）")
        c.execute("INSERT INTO group_run_camp(group_id,level,spent,updated_at)"
                  " VALUES(?,?,?,?) ON CONFLICT(group_id) DO UPDATE SET"
                  " level=excluded.level, spent=excluded.spent,"
                  " updated_at=excluded.updated_at",
                  (gid, st["level"] + 1, st["spent"] + nxt["cost"], _now()))
        _audit(c, gid, uid, "camp_upgrade", st["level"] + 1)
    return {"ok": True, "camp": camp_state(gid)}


def api_audit(me, gid: int, query: dict) -> dict:
    """最近的操作记录（只有 owner/admin 能看）。

    审计表只存 id 和动作名，展示用的昵称在这里补一次——成员退出后
    JOIN 不到，就退回显示「已退出的人」。
    """
    uid = int(me["user_id"])
    try:
        limit = max(1, min(200, int((query.get("limit") or ["50"])[0])))
    except (TypeError, ValueError):
        limit = 50
    with db() as c:
        _require_staff(c, gid, uid)
        rows = c.execute(
            "SELECT a.actor_id, a.action, a.target_id, a.ts, m.alias AS actor_alias,"
            "       t.alias AS target_alias"
            "  FROM group_audit a"
            "  LEFT JOIN group_members m ON m.group_id=a.group_id AND m.user_id=a.actor_id"
            "  LEFT JOIN group_members t ON t.group_id=a.group_id AND t.user_id=a.target_id"
            " WHERE a.group_id=? ORDER BY a.id DESC LIMIT ?", (gid, limit)).fetchall()
        me_uid = uid
    return {"ok": True, "rows": [{
        "actor": r["actor_alias"] or "已退出的人",
        "actor_id": int(r["actor_id"]),
        "action": r["action"],
        "target": (r["target_alias"] or "") if r["target_id"] else "",
        "at": int(r["ts"]),
        "me": int(r["actor_id"]) == me_uid,
    } for r in rows]}


def api_set_join_mode(me, gid: int, mode: str) -> dict:
    """切换「加入需批准」。只有组长/管理员能改。"""
    uid = int(me["user_id"])
    mode = "approve" if str(mode) == "approve" else "open"
    with db() as c:
        _require_admin(c, gid, uid)
        c.execute("UPDATE groups SET join_mode=? WHERE id=?", (mode, gid))
        _audit(c, gid, uid, "join_mode", mode)
    return {"ok": True, "join_mode": mode}


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
            # 营地等级：组名后缀「· Lv3 营地」由客户端拼，不写进 groups.name
            g["camp_level"] = int(_camp_state(c, int(r["id"])).get("level") or 1)
            out.append(g)

    # 每组再补一份「我在这组的成就」：拿到几件衫、有没有探索徽章、目标完成到哪。
    # 直接复用 api_season（实测单组 ~5 ms，最多 3 个组就是 ~15 ms）。
    # 不另写一套轻量指标算法——两套算法迟早会对不上，列表说 2 件、点进去 3 件，
    # 那种 bug 比慢 15 毫秒难查得多。
    for g in out:
        try:
            se = api_season(me, int(g["id"]), {})
        except Exception:      # noqa: BLE001 - 成就算不出来不该让列表打不开
            continue
        mine = se.get("me") or {}
        # 下发**全部六项**、每项标 got：卡片要画出「六件里我有几件」，
        # 只给我拿到的那些，前端就没法画空位，用户数不出缺几件。
        got_ids = {str(x.get("id")) for x in (mine.get("shirts") or [])}
        g["honors"] = {
            "shirts": [{"id": x.get("id"), "name": x.get("name"),
                        "title": x.get("title"), "color": x.get("color"),
                        "pattern": str(x.get("pattern") or ""),
                        "got": str(x.get("id")) in got_ids}
                       for x in (se.get("shirts") or [])],
            "score": mine.get("score"),
            "explorer": int(mine.get("explorer") or 0),
            "explore_min": int((se.get("explore") or {}).get("min_cells") or 5),
            "goal_km": (se.get("goal") or {}).get("km"),
            "rank": mine.get("rank"),
            "total": len(se.get("people") or []),
            "total_km": round(sum(float(x.get("km") or 0)
                                  for x in (se.get("people") or [])), 1),
        }
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
        pend = (c.execute("SELECT user_id, alias, joined_at FROM group_members"
                          " WHERE group_id=? AND status='pending' ORDER BY joined_at",
                          (gid,)).fetchall()
                if my["role"] in ("owner", "admin") else [])
        # 连接还活着的时候读：db() 退出后连接就关了
        _lv = int(_camp_state(c, gid).get("level") or 1)
    out = _group_brief(g, my["role"], len(rows))
    out["camp_level"] = _lv
    # 邀请码只给管理角色：普通成员拿到就能无限拉人，等于绕过了组长审批
    if my["role"] in ("owner", "admin"):
        out["invite_code"] = g["invite_code"]
        # 待批准的申请只给管理角色：普通成员不该看到谁在申请
        out["pending"] = [{"user_id": int(r["user_id"]), "alias": r["alias"],
                           "at": int(r["joined_at"])} for r in pend]
    # map_share 必须带上：客户端进组页时会用它刷新本地的「要不要上报格子」
    # 缓存。以前这里只给了 alias/share，客户端读到 None 就当成 False 写回本地，
    # 于是每次进组页都把开关重置成「关」，用户看到的就是「路线共享又没了、
    # 还得手动重开」。
    out["me"] = {
        "alias": my["alias"], "share": my["share_level"],
        "map_share": bool(my["map_share"]) if "map_share" in my.keys() else False,
        "relay_share": (bool(my["relay_share"])
                        if "relay_share" in my.keys() else False),
    }
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
    clamped_elev = 0
    clamped_explorer = 0
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
            expl = int(d.get("explorer") or 0)
            # 骑行里程（米）。老客户端不传 → 0，综合榜少算骑行而已，不算错。
            ride = int(d.get("ride_m") or 0)
            # 休整点：0/1。每周上限 4 在后面按「本周已存 + 本次」截断，
            # 不让一次补传 30 天就把团队池灌满。
            vigor = 1 if int(d.get("vigor") or 0) > 0 else 0
        except (TypeError, ValueError):
            raise GroupError(400, f"{day} 的数值不合法")
        if not 0 <= dist <= DAY_DIST_MAX_M:
            raise GroupError(400, f"{day} 单日里程超出范围")
        if not 0 <= dur <= DAY_DUR_MAX_S:
            raise GroupError(400, f"{day} 单日时长超出范围")
        if not 0 <= acts <= DAY_ACTS_MAX:
            raise GroupError(400, f"{day} 单日次数超出范围")
        if not 0 <= ride <= DAY_DIST_MAX_M:
            raise GroupError(400, f"{day} 单日骑行里程超出范围")
        if not 0 <= elev <= DAY_ELEV_MAX:
            raise GroupError(400, f"{day} 单日爬升超出范围")
        # 爬升密度防刷：日报表只有日汇总，没有单次数据，所以按
        # 「爬升 / 里程 ≤ 200 m/km」在门口截断。0 公里的日子不截
        # （室内跑步机没有 GPS，爬升本来就是 0；真报了个数也没法比）。
        if dist > 0:
            cap = int(dist / 1000.0 * _ELEV_PER_KM_MAX)
            if elev > cap:
                elev = cap
                clamped_elev += 1
        # plan_done 三态：null = 当天没有计划 / 拿不到计划，不当成 0
        raw = d.get("plan_done", None)
        plan = None if raw is None else (1 if raw else 0)
        # 探索：负数当 0，超单日上限截断。客户端已经封顶过一遍（explore.DAY_CAP），
        # 这里再截一次是为了「客户端被改坏也不脏库」。截断不静默，响应里报数。
        if expl < 0:
            expl = 0
        if expl > DAY_EXPLORER_MAX:
            expl = DAY_EXPLORER_MAX
            clamped_explorer += 1
        rows.append((gid, uid, day, dist, dur, acts, elev, expl, plan, _now(),
                     vigor, ride))

    with db() as c:
        m = _require_member(c, gid, uid)
        if m["share_level"] == "off":
            # 关掉共享的意图就是「别再收我的数据」。写进去再藏起来是自欺欺人。
            return {"ok": True, "saved": 0, "skipped": len(rows), "share": "off"}
        # 休整点每周上限 4：按周汇总「库里已有 + 这次要加的」，超出部分清零。
        # 不清的话，一次补传 30 天就能把团队池灌满。
        if any(r[10] for r in rows):
            _byw: dict[tuple, list[int]] = {}
            for i, r in enumerate(rows):
                if not r[10]:
                    continue
                _d = dt.date.fromisoformat(r[2])
                _wf, _wt = _week(_d)
                _byw.setdefault((int(r[1]), _wf.isoformat(), _wt.isoformat()),
                                []).append(i)
            for (u, wf_s, wt_s), idxs in _byw.items():
                already = c.execute(
                    "SELECT COALESCE(SUM(vigor),0) FROM group_daily"
                    " WHERE group_id=? AND user_id=? AND date BETWEEN ? AND ?",
                    (gid, u, wf_s, wt_s)).fetchone()[0]
                room = max(0, VIGOR_WEEK_MAX - int(already))
                for i in idxs[room:]:
                    # 只清 vigor（第 11 项），ride_m 必须原样留着
                    rows[i] = rows[i][:10] + (0,) + rows[i][11:]

        c.executemany("""
            INSERT INTO group_daily(group_id,user_id,date,distance_m,duration_s,acts,elevation_m,explorer,plan_done,updated_at,vigor,ride_m)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(group_id,user_id,date) DO UPDATE SET
                distance_m=excluded.distance_m,
                duration_s=excluded.duration_s,
                acts=excluded.acts,
                elevation_m=excluded.elevation_m,
                explorer=excluded.explorer,
                plan_done=excluded.plan_done,
                updated_at=excluded.updated_at,
                vigor=excluded.vigor,
                ride_m=excluded.ride_m
        """, rows)
    out = {"ok": True, "saved": len(rows), "skipped": 0, "share": m["share_level"]}
    # 说出来，不静默改数：客户端传了不合理的爬升或格数，用户该知道被截了。
    # 两个字段分开报——老字段名 clamped_elev 被客户端读过，不能改语义。
    if clamped_elev:
        out["clamped_elev"] = clamped_elev
    if clamped_explorer:
        out["clamped_explorer"] = clamped_explorer
    return out


# ---------------------------------------------------------------- 周成绩
# 周目标（让分赛 / 预言的基准）。上限 1000 km 是防手滑：把「45」打成「4500」
# 会让整周对子全废，而且这人自己还不知道。
GOAL_MIN_KM, GOAL_MAX_KM = 1.0, 1000.0


def _goals(c, gid: int, wf: dt.date) -> dict[int, float]:
    rows = c.execute("SELECT user_id, km FROM group_goal"
                     " WHERE group_id=? AND week_start=?", (gid, wf.isoformat())).fetchall()
    return {int(r["user_id"]): float(r["km"]) for r in rows}


def _suggest_goal(c, gid: int, uid: int, wf: dt.date) -> float:
    """没报过目标时给个建议值：近 4 周周均里程 × 0.8。

    为什么是 0.8：目标是拿来达成的，不是拿来吓自己的。按上周平均报，
    状态一波动就完不成，让分赛和预言会变成「谁猜得准」的运气游戏。
    """
    since = (wf - dt.timedelta(days=28)).isoformat()
    end = (wf - dt.timedelta(days=1)).isoformat()
    r = c.execute("SELECT SUM(distance_m) m, COUNT(DISTINCT date) d FROM group_daily"
                  " WHERE group_id=? AND user_id=? AND date BETWEEN ? AND ?",
                  (gid, uid, since, end)).fetchone()
    km = float(r["m"] or 0) / 1000.0
    if km <= 0:
        return 0.0
    weeks = 4.0
    return round(km / weeks * 0.8, 1)


def _pairs(c, gid: int, wf: dt.date) -> list[dict]:
    rows = c.execute("SELECT slot, small_uid, big_uid FROM group_pair"
                     " WHERE group_id=? AND week_start=? ORDER BY slot",
                     (gid, wf.isoformat())).fetchall()
    return [{"slot": int(r["slot"]), "small_uid": int(r["small_uid"]),
             "big_uid": int(r["big_uid"])} for r in rows]


def _maybe_pair(c, gid: int, wf: dt.date) -> None:
    """把这次新报到的人跟同样还没配对的人配成对，已有对子不动。

    为什么不是「整组重配」：打到周三突然重排，已经领先的那对等于白打。
    为什么不是「只配一次」：报目标是陆续发生的，只配一次的话，第一对之后
    报进来的人永远是 solo（组里 6 个人只会有一对在比）。
    """
    goals = _goals(c, gid, wf)
    if len(goals) < 2:
        return
    old = _pairs(c, gid, wf)
    used: set[int] = set()
    slot = 0
    for p in old:
        used |= {p["small_uid"], p["big_uid"]}
        slot = max(slot, p["slot"])
    rest = {u: km for u, km in goals.items() if u not in used}
    if len(rest) < 2:
        return
    rows = []
    for p in ranking.pairing(rest):
        if "solo_uid" in p:
            continue
        slot += 1
        rows.append((gid, wf.isoformat(), slot,
                     int(p["small_uid"]), int(p["big_uid"])))
    if rows:
        c.executemany("INSERT OR IGNORE INTO group_pair(group_id,week_start,slot,small_uid,big_uid)"
                      " VALUES(?,?,?,?,?)", rows)


def api_goal(me, gid: int, body: dict) -> dict:
    """报本周目标，顺便可以投一票预言。

    一次请求干两件事，是因为它们本来就发生在同一个动作里：周一早上你
    想「这周跑 50」，顺手就想「谁会完不成」。分成两个接口，第二个永远
    没人点。

    **km 可以空着**：那是「只改预言」。原来的实现是 km 必填，于是
    「我只想猜别人、不想定目标」和「目标没变、只想换个人猜」这两件很
    自然的事都做不了——用户反馈的「好像不能猜别人的目标」就是这个。

    `predict` 这个键**在不在**决定改不改预言：不在 = 不动；在且为空 =
    撤掉这周的预言；在且是人 = 投/改。
    """
    uid = int(me["user_id"])
    today = _today(body.get("today"))
    wf, _wt = _week(today)

    km = None
    raw = body.get("km")
    if raw not in (None, ""):
        try:
            km = float(raw)
        except (TypeError, ValueError):
            raise GroupError(400, "目标要是数字，单位 km")
        if not GOAL_MIN_KM <= km <= GOAL_MAX_KM:
            raise GroupError(400, f"目标要在 {GOAL_MIN_KM:g}–{GOAL_MAX_KM:g} km 之间")

    pred_given = "predict" in body
    predict = body.get("predict")
    if km is None and not pred_given:
        raise GroupError(400, "先填本周目标，或者选一个要猜的人")

    with db() as c:
        _group(c, gid)
        m = _require_member(c, gid, uid)
        if m["share_level"] == "off":
            raise GroupError(409, "你关闭了共享，报目标也不会有人看到")
        if km is not None:
            c.execute("""
                INSERT INTO group_goal(group_id,user_id,week_start,km,source,created_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(group_id,user_id,week_start) DO UPDATE SET
                    km=excluded.km, created_at=excluded.created_at
            """, (gid, uid, wf.isoformat(), km,
                  str(body.get("source") or "manual"), _now()))

        if pred_given and predict in (None, "", 0, "0", "none"):
            # 撤掉预言：不写 NULL，直接删行，week 里就不会回显别人的名字
            c.execute("DELETE FROM group_predict WHERE group_id=? AND week_start=? AND user_id=?",
                      (gid, wf.isoformat(), uid))
        elif pred_given:
            try:
                target = int(predict)
            except (TypeError, ValueError):
                raise GroupError(400, "预言对象不合法")
            if target == uid:
                # 自投是无意义的——你既知道自己跑没跑，又能自己放弃
                raise GroupError(400, "不能预测自己")
            tm = c.execute("SELECT 1 FROM group_members WHERE group_id=? AND user_id=?"
                           " AND status='active'", (gid, target)).fetchone()
            if not tm:
                raise GroupError(400, "这个人不在组里")
            tg = _goals(c, gid, wf)
            if target not in tg:
                raise GroupError(400, "他还没报本周目标，没法判他完没完成")
            if float(tg[target]) < ranking.PAIR_MIN_KM:
                raise GroupError(400, f"他的目标不到 {ranking.PAIR_MIN_KM:g} km，"
                                      "没有「完不成」这回事")
            c.execute("""
                INSERT INTO group_predict(group_id,week_start,user_id,target_uid,created_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(group_id,week_start,user_id) DO UPDATE SET
                    target_uid=excluded.target_uid, created_at=excluded.created_at
            """, (gid, wf.isoformat(), uid, target, _now()))

        _maybe_pair(c, gid, wf)
        pairs = _pairs(c, gid, wf)
        goals = _goals(c, gid, wf)
        pred = c.execute("SELECT target_uid FROM group_predict"
                         " WHERE group_id=? AND week_start=? AND user_id=?",
                         (gid, wf.isoformat(), uid)).fetchone()
    return {"ok": True, "week_start": wf.isoformat(),
            "km": round(km, 1) if km is not None else goals.get(uid),
            "pairs": len(pairs), "goals": len(goals),
            "predict": int(pred["target_uid"]) if pred else None}


def api_week(me, gid: int, query: dict) -> dict:
    """本周的目标、对子、预言、建议目标——组页「本周玩法」那一块要的原料。

    单独一个接口而不是塞进 /home：这些数据只有组页展开「本周玩法」时才需要，
    塞进 home 会让每次进组都多算一遍配对结算。
    """
    uid = int(me["user_id"])
    today = _today((query.get("today") or [None])[0])
    wf, wt = _week(today)
    with db() as c:
        _group(c, gid)
        _require_member(c, gid, uid)
        mem = c.execute("SELECT user_id, alias, share_level FROM group_members"
                        " WHERE group_id=? AND status='active'", (gid,)).fetchall()
        goals = _goals(c, gid, wf)
        pairs_db = _pairs(c, gid, wf)
        preds = c.execute("SELECT user_id, target_uid FROM group_predict"
                          " WHERE group_id=? AND week_start=?",
                          (gid, wf.isoformat())).fetchall()
        agg = c.execute("""
            SELECT user_id, SUM(distance_m) m FROM group_daily
             WHERE group_id=? AND date BETWEEN ? AND ? GROUP BY user_id
        """, (gid, wf.isoformat(), min(wt, today).isoformat())).fetchall()
        # Bingo 要的是「我这一周的日明细」，不是合计——一半任务问的是「有一天」
        my_rows = c.execute(
            "SELECT date, distance_m, duration_s, acts, plan_done, explorer"
            " FROM group_daily WHERE group_id=? AND user_id=? AND date BETWEEN ? AND ?"
            " ORDER BY date",
            (gid, uid, wf.isoformat(), min(wt, today).isoformat())).fetchall()
        suggest = _suggest_goal(c, gid, uid, wf)
    alias = {int(m["user_id"]): m["alias"] for m in mem}
    actual = {int(r["user_id"]): round(float(r["m"] or 0) / 1000.0, 1) for r in agg}

    # 落库的只有 uid 对；目标与结果要按本周 goal 现算，不能落库——
    # 目标周内可改，落了就会和榜单对不上。
    built = []
    for p in pairs_db:
        s_u, b_u = p["small_uid"], p["big_uid"]
        s_km = float(goals.get(s_u) or 0)
        b_km = float(goals.get(b_u) or 0)
        gap = (b_km - s_km) / s_km if s_km > 0 else 0.0
        even = gap < ranking.PAIR_GAP_MIN
        b_target = b_km if even else min(b_km * ranking.BIG_CAP,
                                         b_km + s_km * ranking.BIG_HANDICAP)
        built.append({"slot": p["slot"],
                      "small": {"user_id": s_u, "alias": alias.get(s_u, ""),
                                "km": round(s_km, 1), "target": round(s_km, 1),
                                "actual": actual.get(s_u, 0.0)},
                      "big": {"user_id": b_u, "alias": alias.get(b_u, ""),
                              "km": round(b_km, 1), "target": round(b_target, 1),
                              "actual": actual.get(b_u, 0.0)},
                      "even": even,
                      # 让出去多少：页面要写「他多背 15 km」，不是让他自己减
                      "handicap_km": round(max(0.0, b_target - b_km), 1)})
    # 结果用纯函数算，和 api_season 里给分的口径必须是同一份
    flat = [{"slot": p["slot"], "small_uid": p["small"]["user_id"],
             "big_uid": p["big"]["user_id"],
             "small_target": p["small"]["target"], "big_target": p["big"]["target"],
             "even": p["even"]} for p in built]
    res = {r["slot"]: r for r in ranking.pair_results(flat, actual)}
    for p in built:
        p["result"] = res[p["slot"]]["result"]

    pred_map = {int(r["user_id"]): int(r["target_uid"]) for r in preds}
    pres = ranking.prediction_result(pred_map, actual, goals)
    my_pred = pred_map.get(uid)

    # Bingo 只报个进度（几点亮 / 几条线），整张卡另外用 /bingo 拉。
    # 组页要的就是一行字，没必要每次进组都传 25 个格子。
    import bingo as B

    _my_rows = [dict(r) for r in my_rows]
    _bm = ranking.metrics(_my_rows, [])
    _bcard = B.card_for(_my_rows, _bm, gid, uid, wf.isoformat())

    return {"ok": True, "from": wf.isoformat(), "to": wt.isoformat(),
            "bingo": {"done": _bcard["done"], "total": _bcard["total"],
                      "lines": _bcard["line_count"]},
            "goal": {"km": goals.get(uid), "suggest": suggest,
                     "reported": uid in goals},
            "pairs": built,
            "predict": {"mine": my_pred,
                        "rows": [dict(r, alias=alias.get(int(r["user_id"]), ""),
                                      target_alias=alias.get(int(r["target_uid"]), ""))
                                 for r in pres["rows"]]},
            "bonus": pres["bonus"],
            # 让分规则下发：页面要解释「他为什么多了 15 km」，写死的数字
            # 一旦和服务端算的不一样，用户会当成 bug。
            "pair_rule": {"min_km": ranking.PAIR_MIN_KM,
                          "gap": ranking.PAIR_GAP_MIN,
                          "handicap": ranking.BIG_HANDICAP,
                          "cap": ranking.BIG_CAP,
                          "predict_hit": ranking.PREDICT_HIT,
                          "predict_face": ranking.PREDICT_UNDERESTIMATED},
            "goal_rows": [{"user_id": u, "alias": alias.get(u, ""), "km": round(k, 1)}
                          for u, k in sorted(goals.items(), key=lambda x: -x[1])],
            # 可猜的人：报了 ≥5 km 目标、共享开着、不是自己。带 km 一起下发，
            # 下拉里直接显示「阿狗 40 km」——只有名字的话，猜谁全靠猜。
            "targets": [{"user_id": int(m["user_id"]), "alias": m["alias"],
                         "km": round(float(goals[int(m["user_id"])]), 1)}
                        for m in mem
                        if int(m["user_id"]) != uid
                        and (m["share_level"] or "summary") == "summary"
                        and float(goals.get(int(m["user_id"])) or 0) >= ranking.PAIR_MIN_KM]}


def api_bingo(me, gid: int, query: dict) -> dict:
    """我的本周 Bingo 卡（25 格 + 连线）。

    不落库：卡由 (组, 人, 周一) 的种子确定性生成，同一周每次请求算出来一样。
    25 行/人/周 的表不值得为这一点随机性去维护。
    """
    uid = int(me["user_id"])
    today = _today((query.get("today") or [None])[0])
    wf, wt = _week(today)
    pf, pt = wf - dt.timedelta(days=7), wf - dt.timedelta(days=1)
    with db() as c:
        _group(c, gid)
        m = _require_member(c, gid, uid)
        if m["share_level"] == "off":
            raise GroupError(409, "你关闭了共享，Bingo 也不参与了")
        rows = c.execute(
            # elevation_m 必须取：bingo 的「本周爬升 / 单日爬升 / 山地 12km」全靠它。
            # 漏了这一列时 rows 里没有这个键，_extras 和 ranking.metrics 都读到 0，
            # 于是所有爬升类卡片永远不亮（用户报：20km 爬升 1367m，爬升卡却没激活）。
            "SELECT user_id, date, distance_m, duration_s, elevation_m,"
            " acts, plan_done, explorer"
            " FROM group_daily WHERE group_id=? AND user_id=? AND date BETWEEN ? AND ?"
            " ORDER BY date", (gid, uid, wf.isoformat(), wt.isoformat())).fetchall()
        prev = c.execute(
            # elevation_m 必须取：bingo 的「本周爬升 / 单日爬升 / 山地 12km」全靠它。
            # 漏了这一列时 rows 里没有这个键，_extras 和 ranking.metrics 都读到 0，
            # 于是所有爬升类卡片永远不亮（用户报：20km 爬升 1367m，爬升卡却没激活）。
            "SELECT user_id, date, distance_m, duration_s, elevation_m,"
            " acts, plan_done, explorer"
            " FROM group_daily WHERE group_id=? AND user_id=? AND date BETWEEN ? AND ?"
            " ORDER BY date", (gid, uid, pf.isoformat(), pt.isoformat())).fetchall()

    import bingo as B

    this_rows = [dict(r) for r in rows]
    met = ranking.metrics(this_rows, [dict(r) for r in prev])
    out = B.card_for(this_rows, met, gid, uid, wf.isoformat())
    out["ok"] = True
    out["from"] = wf.isoformat()
    out["to"] = wt.isoformat()
    return out


# ---------------------------------------------------------------- 组内路线图
# 每人一种颜色。序号按「入组顺序」，某个人关掉路线共享时不重排——否则
# 他一关，别人的颜色全变，队里会以为换了人。
MAP_COLORS = ["#5EA0FF", "#3FA76A", "#E8C43A", "#F2555A", "#B98CF6",
              "#22D3EE", "#F5A524", "#FF7AB6", "#7DD3A0", "#C0C8D8"]
# 一周 100 km 大概 200–300 格，600 是给「跑了两个马拉松 + 通勤」留的余量。
MAX_CELLS = 600
# 单格跑量上限。500 m 见方的一格，跑 50 km 已经是操场刷 100 圈的量级了，
# 超过这个数只可能是脏数据——截断而不是拒收，免得一条坏数据废掉整批。
CELL_M_MAX = 50000
CELL_RE = re.compile(r"^-?\d{1,6}:-?\d{1,6}$")


ROUTE_MIN_M = 3000        # 短于 3 km 的路线不值得分享：小圈绕来绕去没有参考价值
ROUTE_MAX_POINTS = 800    # 800 点约 12 KB，画一条线够用；再多只是浪费流量
ROUTE_PER_USER = 5        # 每人每组最多 5 条，否则「我的路线」会变成相册
# 批注：一条路线最多几条、单图多大、存哪。
# 图从客户端上传前已经用 canvas 压到 ~900px / JPEG 0.72（约 100 KB），
# 这里再兜一道 400 KB：客户端被改坏也不能往服务器灌原图。
NOTE_PER_ROUTE = 9
NOTE_IMG_MAX = 400 * 1024
ROUTE_IMG_DIR = Path(GROUPS_DB).parent / "route_img"


def _clean_points(raw) -> list[list[float]]:
    """轨迹点校验。越界/非数字一律拒，不做「修一下」——修复过的坐标是假数据。"""
    if not isinstance(raw, list) or not 3 <= len(raw) <= ROUTE_MAX_POINTS:
        raise GroupError(400, f"轨迹要有 3–{ROUTE_MAX_POINTS} 个点")
    out: list[list[float]] = []
    for p in raw:
        try:
            lat, lon = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError):
            raise GroupError(400, "轨迹点格式不对") from None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise GroupError(400, "轨迹点超出经纬度范围")
        out.append([round(lat, 5), round(lon, 5)])   # 5 位 ≈ 1 米，再多没有意义
    return out


def api_route_share(me, gid: int, body: dict) -> dict:
    """分享一条路线。**主动授权**才调这里，默认一条都不存在。

    和 `api_cells` 的区别要说清楚：格子是归一化网格编号，看不出在哪条街；
    这条路线上传的是真经纬度，同组成员能把这条路铺在地图上。
    所以：
    - 只能一条一条传，没有「全部共享」；
    - 撤回是 DELETE，删了就没有；
    - 页面上必须让用户先勾「知道会包含位置」（客户端负责，服务端只认结果）。
    """
    uid = int(me["user_id"])
    act_id = str(body.get("act_id") or "").strip()[:40]
    title = str(body.get("title") or "").strip()[:24]
    date = str(body.get("date") or "").strip()[:10]
    try:
        dist = int(body.get("distance_m") or 0)
    except (TypeError, ValueError):
        raise GroupError(400, "distance_m 要是整数")
    if dist < ROUTE_MIN_M:
        raise GroupError(400, f"短于 {ROUTE_MIN_M // 1000} km 的路线不收录")
    pts = _clean_points(body.get("points"))

    with db() as c:
        _require_member(c, gid, uid)
        n = c.execute("SELECT COUNT(*) n FROM group_route WHERE group_id=? AND user_id=?",
                      (gid, uid)).fetchone()["n"]
        old = c.execute("SELECT id FROM group_route"
                        " WHERE group_id=? AND user_id=? AND act_id=?",
                        (gid, uid, act_id)).fetchone()
        if old is None and n >= ROUTE_PER_USER:
            raise GroupError(409, f"每人最多分享 {ROUTE_PER_USER} 条，先撤一条")
        c.execute(
            "INSERT INTO group_route(group_id,user_id,act_id,title,distance_m,date,"
            "points,created_at) VALUES(?,?,?,?,?,?,?,?)"
            " ON CONFLICT(group_id,user_id,act_id) DO UPDATE SET"
            " title=excluded.title, distance_m=excluded.distance_m,"
            " date=excluded.date, points=excluded.points, created_at=excluded.created_at",
            (gid, uid, act_id, title, dist, date, json.dumps(pts), _now()))
        rid = c.execute("SELECT id FROM group_route"
                        " WHERE group_id=? AND user_id=? AND act_id=?",
                        (gid, uid, act_id)).fetchone()["id"]
    return {"ok": True, "id": int(rid), "points": len(pts)}


def api_route_del(me, gid: int, rid: int) -> dict:
    """撤回一条路线。只能删自己的，别人的一律 404（不确认存在）。

    走 POST 而不是 DELETE：这个服务端只实现了 GET/POST/PATCH，
    DELETE 会被 base handler 直接判 501。
    """
    uid = int(me["user_id"])
    with db() as c:
        _require_member(c, gid, uid)
        r = c.execute("SELECT user_id FROM group_route WHERE id=? AND group_id=?",
                      (rid, gid)).fetchone()
        if not r or int(r["user_id"]) != uid:
            raise GroupError(404, "没有这条路线")
        # 批注和照片一起走：路线撤回了，钉在上面的照片还留着就说不通了。
        _imgs = [str(x["image"] or "") for x in c.execute(
            "SELECT image FROM group_route_note WHERE route_id=? AND group_id=?",
            (rid, gid)).fetchall()]
        c.execute("DELETE FROM group_route_note WHERE route_id=? AND group_id=?",
                  (rid, gid))
        c.execute("DELETE FROM group_route WHERE id=?", (rid,))
    for _f in _imgs:
        if _f:
            try:
                (ROUTE_IMG_DIR / _f).unlink(missing_ok=True)
            except OSError:
                pass
    return {"ok": True}


def api_routes(me, gid: int, query: dict) -> dict:
    """组内已分享的路线清单。**不带轨迹点**：先看有哪些，点开某条才取坐标。

    一次把所有轨迹都下发的话，5 人 × 5 条 × 12 KB 就是 300 KB，
    而用户通常只看其中一条。
    """
    uid = int(me["user_id"])
    with db() as c:
        _require_member(c, gid, uid)
        rows = c.execute(
            "SELECT r.id, r.user_id, r.title, r.distance_m, r.date, r.created_at,"
            "       COALESCE(NULLIF(m.alias, ''), '队友') AS alias"
            "  FROM group_route r"
            "  JOIN group_members m ON m.group_id = r.group_id"
            "       AND m.user_id = r.user_id AND m.status = 'active'"
            " WHERE r.group_id=? ORDER BY r.id DESC LIMIT 100", (gid,)).fetchall()
    return {"ok": True, "rows": [{
        "id": int(r["id"]), "user_id": int(r["user_id"]),
        "alias": str(r["alias"]), "title": str(r["title"]),
        "distance_m": int(r["distance_m"] or 0), "date": str(r["date"]),
        "me": int(r["user_id"]) == uid, "at": int(r["created_at"]),
    } for r in rows]}


def _note_view(r) -> dict:
    return {"id": int(r["id"]), "x": round(float(r["x"] or 0), 4),
            "y": round(float(r["y"] or 0), 4),
            "text": str(r["text"] or ""),
            "has_img": bool(str(r["image"] or ""))}


def api_route_note_add(me, gid: int, rid: int, body: dict) -> dict:
    """给路线钉一条批注。**只有这条路线的作者能钉。**

    让组内其他人也能加的话，这里会变成公共留言板，和「作者推荐的跑法」
    是两回事。评论已经有 feed / comment 那套了，不必在这里再开一个。
    """
    uid = int(me["user_id"])
    text = str(body.get("text") or "").strip()[:200]
    img_b64 = str(body.get("image") or "").strip()
    try:
        x = float(body.get("x"))
        y = float(body.get("y"))
    except (TypeError, ValueError):
        raise GroupError(400, "批注位置不合法") from None
    if not (0 <= x <= 1 and 0 <= y <= 1):
        raise GroupError(400, "批注位置不合法")
    if not text and not img_b64:
        raise GroupError(400, "批注要么写点字，要么放张照片")

    raw = b""
    if img_b64:
        # 前端 canvas.toDataURL 给的是 data:image/jpeg;base64,xxx，去掉前缀
        if img_b64.startswith("data:"):
            img_b64 = img_b64.split(",", 1)[-1]
        try:
            raw = base64.b64decode(img_b64, validate=True)
        except (binascii.Error, ValueError):
            raise GroupError(400, "照片格式不对") from None
        if len(raw) > NOTE_IMG_MAX:
            raise GroupError(413, "照片太大，换一张压缩过的")
        # 必须真是 JPEG：将来这张图要直接塞进 <img>，不能让它带上别的类型。
        # 校验魔数，不认扩展名：扩展名是客户端说了算的。
        if not raw.startswith(b"\xff\xd8\xff"):
            raise GroupError(400, "照片必须是 JPEG")

    with db() as c:
        _require_member(c, gid, uid)
        r = c.execute("SELECT user_id FROM group_route WHERE id=? AND group_id=?",
                      (rid, gid)).fetchone()
        if not r:
            raise GroupError(404, "没有这条路线")
        if int(r["user_id"]) != uid:
            raise GroupError(403, "只有这条路线的作者能加批注")
        n = int(c.execute("SELECT COUNT(*) n FROM group_route_note WHERE route_id=?",
                          (rid,)).fetchone()["n"])
        if n >= NOTE_PER_ROUTE:
            raise GroupError(409, f"一条路线最多 {NOTE_PER_ROUTE} 条批注")
        cur = c.execute(
            "INSERT INTO group_route_note(group_id,route_id,user_id,x,y,text,image,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (gid, rid, uid, x, y, text, "", _now()))
        nid = int(cur.lastrowid or 0)

    if raw and nid:
        try:
            ROUTE_IMG_DIR.mkdir(parents=True, exist_ok=True)
            fname = f"{nid}.jpg"
            (ROUTE_IMG_DIR / fname).write_bytes(raw)
            with db() as c:
                c.execute("UPDATE group_route_note SET image=? WHERE id=?",
                          (fname, nid))
        except OSError as e:
            # 照片没存住就把批注撤掉：否则界面上会留一个「有图但打不开」，
            # 用户以为传上去了。
            with db() as c:
                c.execute("DELETE FROM group_route_note WHERE id=?", (nid,))
            raise GroupError(500, f"照片保存失败：{e}") from None
    return {"ok": True, "id": nid}


def api_route_note_del(me, gid: int, rid: int, nid: int) -> dict:
    """删一条批注。只有作者能删（别人根本没有添加入口）。"""
    uid = int(me["user_id"])
    fname = ""
    with db() as c:
        _require_member(c, gid, uid)
        r = c.execute("SELECT user_id, image FROM group_route_note"
                      " WHERE id=? AND route_id=? AND group_id=?",
                      (nid, rid, gid)).fetchone()
        if not r or int(r["user_id"]) != uid:
            raise GroupError(404, "没有这条批注")
        fname = str(r["image"] or "")
        c.execute("DELETE FROM group_route_note WHERE id=?", (nid,))
    if fname:
        try:
            (ROUTE_IMG_DIR / fname).unlink(missing_ok=True)
        except OSError:
            pass                  # 文件删不掉不该让「删除」失败
    return {"ok": True}


def note_image(me, gid: int, rid: int, nid: int) -> tuple:
    """批注照片的二进制。**只有组内成员能取。**

    这是本项目第一份「照片 + 位置」的组合数据，比里程敏感得多，
    因此走成员校验，不做公开链接。
    """
    uid = int(me["user_id"])
    fname = ""
    with db() as c:
        _require_member(c, gid, uid)
        r = c.execute("SELECT image FROM group_route_note"
                      " WHERE id=? AND route_id=? AND group_id=?",
                      (nid, rid, gid)).fetchone()
        fname = str((r["image"] if r else "") or "")
    if not fname:
        raise GroupError(404, "这条批注没有照片")
    p = ROUTE_IMG_DIR / fname
    try:
        return p.read_bytes(), "image/jpeg"
    except OSError:
        raise GroupError(404, "照片不见了") from None


def api_route_get(me, gid: int, rid: int) -> dict:
    """单条路线 + 轨迹点。点开才取，取到就能直接铺到地图上。"""
    uid = int(me["user_id"])
    with db() as c:
        _require_member(c, gid, uid)
        r = c.execute(
            "SELECT r.*, COALESCE(NULLIF(m.alias, ''), '队友') AS alias"
            "  FROM group_route r"
            "  JOIN group_members m ON m.group_id = r.group_id"
            "       AND m.user_id = r.user_id AND m.status = 'active'"
            " WHERE r.id=? AND r.group_id=?", (rid, gid)).fetchone()
        if not r:
            raise GroupError(404, "没有这条路线")
        notes = c.execute(
            "SELECT id,x,y,text,image FROM group_route_note"
            " WHERE route_id=? AND group_id=? ORDER BY id", (rid, gid)).fetchall()
    try:
        pts = json.loads(r["points"])
    except (TypeError, ValueError):
        pts = []
    return {"ok": True, "route": {
        "id": int(r["id"]), "alias": str(r["alias"]), "title": str(r["title"]),
        "distance_m": int(r["distance_m"] or 0), "date": str(r["date"]),
        "me": int(r["user_id"]) == uid, "points": pts,
        # 批注随路线一起下发：钉子要跟着地图一次画出来，单独再请求一次
        # 会让地图先空一下再长点。
        "notes": [_note_view(n) for n in notes],
    }}


def api_relayshare(me, gid: int, body: dict) -> dict:
    """开关「帮队友刷新佳明登录态」（按组，默认关）。

    打开后，**这个组里**没有安卓设备的人（iPhone 用户）登录态过期时，服务器
    可以把「用他的账号登一次」的任务派给这台设备。代价是这台设备会临时拿到
    队友的佳明明文密码，所以它必须是自己点的、随时能关、并且只在本组生效。

    为什么不看被帮的人开没开：他填了佳明账密就是要人帮，没有第二个开关。
    真正需要同意的是**出设备的人**，也就是这里。
    """
    uid = int(me["user_id"])
    on = bool(body.get("on"))
    with db() as c:
        _require_member(c, gid, uid)
        c.execute("UPDATE group_members SET relay_share=?"
                  " WHERE group_id=? AND user_id=?",
                  (1 if on else 0, gid, uid))
        _audit(c, gid, uid, "relay_on" if on else "relay_off", uid)
    return {"ok": True, "relay_share": on}


def share_relay(worker: int, target: int) -> bool:
    """`worker` 这台设备能不能替 `target` 登录一次佳明。

    三个条件同时成立才行：同一个未归档的组、两人都在组里、worker 自己开了开关。
    只要有一条不满足就返回 False——**默认拒绝**，宁可任务多等一会儿，也不能
    让没同意的人拿到别人的密码。
    """
    if int(worker) == int(target):
        return False
    try:
        with db() as c:
            r = c.execute("""
                SELECT 1 FROM group_members a
                JOIN group_members b ON a.group_id = b.group_id
                JOIN groups g ON g.id = a.group_id
                WHERE a.user_id=? AND a.status='active' AND a.relay_share=1
                  AND b.user_id=? AND b.status='active' AND g.archived=0
                LIMIT 1""", (int(worker), int(target))).fetchone()
        return bool(r)
    except sqlite3.Error:
        return False


def relay_workers_for(target: int) -> list[int]:
    """能替这个人登录的成员（同组 + 开了「帮队友」）。

    新用户最容易踩的坑：他刚注册、还没进组，这里返回空——没有任何人能替他
    登录。所以调用方**必须**先问一句「有没有人能干」，没有就别排任务，否则
    任务会永远卡在 pending，用户等半天以为坏了。
    """
    try:
        with db() as c:
            rows = c.execute("""
                SELECT DISTINCT a.user_id FROM group_members a
                JOIN group_members b ON a.group_id = b.group_id
                JOIN groups g ON g.id = a.group_id
                WHERE b.user_id=? AND b.status='active'
                  AND a.status='active' AND a.relay_share=1
                  AND g.archived=0 AND a.user_id != ?
                """, (int(target), int(target))).fetchall()
        return [int(r["user_id"]) for r in rows]
    except sqlite3.Error:
        return []


def has_group(uid: int) -> bool:
    """这个人在不在任何未归档的组里。网页版用它决定要不要提示「先加入小组」。"""
    try:
        with db() as c:
            return bool(c.execute(
                "SELECT 1 FROM group_members m JOIN groups g ON g.id=m.group_id"
                " WHERE m.user_id=? AND m.status='active' AND g.archived=0"
                " LIMIT 1", (int(uid),)).fetchone())
    except sqlite3.Error:
        return False


def any_relay_worker() -> bool:
    """有没有任何一台设备开了「帮队友」。没有就不用排任务，排了也没人领。"""
    try:
        with db() as c:
            return bool(c.execute(
                "SELECT 1 FROM group_members WHERE relay_share=1"
                " AND status='active' LIMIT 1").fetchone())
    except sqlite3.Error:
        return False


def is_relay_worker(uid: int) -> bool:
    """这个人（任一组的成员身份）开了「帮队友」没有。

    设备来问「有没有活」时先用它挡一道：没开过的设备直接 403，客户端记下来
    就不再空问了，省电也省服务器。
    """
    try:
        with db() as c:
            return bool(c.execute(
                "SELECT 1 FROM group_members WHERE user_id=? AND relay_share=1"
                " AND status='active' LIMIT 1", (int(uid),)).fetchone())
    except sqlite3.Error:
        return False


def api_mapshare(me, gid: int, body: dict) -> dict:
    """打开/关闭「把本周跑过的 500 m 网格给同组看」。

    **默认是关的**，而且关掉会把已经存下的格子删干净——「不想共享」和
    「以后别共享」不能只差一个布尔值，那样历史数据还在库里躺着。
    """
    uid = int(me["user_id"])
    on = bool(body.get("on"))
    with db() as c:
        m = _require_member(c, gid, uid)
        if on and (m["share_level"] or "summary") != "summary":
            raise GroupError(409, "先把数据共享打开，再把路线共享打开")
        c.execute("UPDATE group_members SET map_share=? WHERE group_id=? AND user_id=?",
                  (1 if on else 0, gid, uid))
        if not on:
            c.execute("DELETE FROM group_cell WHERE group_id=? AND user_id=?", (gid, uid))
        _audit(c, gid, uid, "map_on" if on else "map_off", uid)
    return {"ok": True, "map_share": on}


def api_cells(me, gid: int, body: dict) -> dict:
    """上传本周跑过的格子。全量覆盖，不是增量——同一批重复发不会翻倍。"""
    uid = int(me["user_id"])
    today = _today(body.get("today"))
    wf, _wt = _week(today)
    raw = body.get("cells")
    if not isinstance(raw, list):
        raise GroupError(400, "cells 要是数组")
    if len(raw) > MAX_CELLS:
        raise GroupError(400, f"一次最多 {MAX_CELLS} 格")
    # 跑量是可选字段：老客户端（≤1.5.7）只发格子列表，那边 meters 就是 0，
    # 服务端照收，地图退化成「跑过/没跑过」的样子，不会报错。
    wraw = body.get("meters")
    weights: dict[str, int] = {}
    if isinstance(wraw, dict):
        for k, v in wraw.items():
            kk = str(k)
            if not CELL_RE.match(kk):
                continue
            try:
                weights[kk] = max(0, min(CELL_M_MAX, int(float(v))))
            except (TypeError, ValueError):
                continue

    cells: list[str] = []
    for x in raw:
        s = str(x)
        if CELL_RE.match(s) and s not in cells:
            cells.append(s)

    with db() as c:
        m = _require_member(c, gid, uid)
        if not m["map_share"]:
            raise GroupError(409, "还没打开路线共享")
        if (m["share_level"] or "summary") != "summary":
            raise GroupError(409, "数据共享已关闭，不接收路线")
        c.execute("DELETE FROM group_cell WHERE group_id=? AND week_start=? AND user_id=?",
                  (gid, wf.isoformat(), uid))
        c.executemany(
            "INSERT OR IGNORE INTO group_cell(group_id,week_start,user_id,cell,meters)"
            " VALUES(?,?,?,?,?)",
            [(gid, wf.isoformat(), uid, k, int(weights.get(k, 0))) for k in cells])
        # 只留最近 4 周：地图只看本周，旧格子留着没有用处，只是风险。
        cutoff = (wf - dt.timedelta(days=28)).isoformat()
        c.execute("DELETE FROM group_cell WHERE group_id=? AND week_start < ?",
                  (gid, cutoff))
    return {"ok": True, "saved": len(cells), "week_start": wf.isoformat()}


def api_map(me, gid: int, query: dict) -> dict:
    """本周组内路线：谁跑过哪些格子。只返回打开路线共享的人。"""
    uid = int(me["user_id"])
    today = _today((query.get("today") or [None])[0])
    wf, wt = _week(today)
    with db() as c:
        _group(c, gid)
        _require_member(c, gid, uid)
        mem = c.execute(
            "SELECT user_id, alias, share_level, map_share FROM group_members"
            " WHERE group_id=? AND status='active' ORDER BY joined_at, user_id",
            (gid,)).fetchall()
        rows = c.execute(
            "SELECT user_id, cell, meters FROM group_cell"
            " WHERE group_id=? AND week_start=? ORDER BY user_id",
            (gid, wf.isoformat())).fetchall()

    by: dict[int, list[str]] = {}
    bym: dict[int, dict[str, int]] = {}
    for r in rows:
        u = int(r["user_id"])
        by.setdefault(u, []).append(str(r["cell"]))
        bym.setdefault(u, {})[str(r["cell"])] = int(r["meters"] or 0)

    people = []
    for i, m in enumerate(mem):
        u = int(m["user_id"])
        if not m["map_share"]:
            continue
        # 关了数据共享的人即使 map_share 还留着 1 也不显示——两道门都过才出来
        if (m["share_level"] or "summary") != "summary":
            continue
        cells = by.get(u) or []
        if not cells:
            continue
        people.append({"user_id": u, "alias": m["alias"],
                       "color": MAP_COLORS[i % len(MAP_COLORS)],
                       "cells": cells,
                       # 每格跑量（米），和 cells 一一对应；老客户端不发就是全 0
                       "meters": bym.get(u) or {},
                       "is_me": u == uid})
    # 我自己的开关状态必须单独给：`people` 里只放「已经有格子的人」，
    # 默认开但本周还没上传格子时我不在里面，客户端就只能按「人不在列表里」
    # 推断成没开，于是永远显示「开启」按钮，点了也没变化（服务端本来就是 1）。
    _mine_m = next((m for m in mem if int(m["user_id"]) == uid), None)
    return {"ok": True, "from": wf.isoformat(), "to": wt.isoformat(),
            "people": people,
            "hidden": sum(1 for m in mem if not m["map_share"]),
            "cells": sum(len(p["cells"]) for p in people),
            "me": {
                "map_share": bool(_mine_m["map_share"]) if _mine_m else False,
                "share": str((_mine_m["share_level"] if _mine_m else "") or "summary"),
                "cells": len(by.get(uid) or []),
            }}


# ---------------------------------------------------------------- 动态
# 概要只能是这四类。骑行、走路也进动态（它们是"今天出门了"），
# 但里程榜仍然只算跑步——两件事的口径本来就不同。
ACT_KINDS = ("run", "ride", "walk", "other")
ACT_MAX = 200            # 一次最多传 200 条
FEED_LIMIT = 40          # 瀑布流一次最多 40 条
COMMENT_MAX = 60         # 一条评论最多 60 字
# 表情只收白名单：这一栏是"随手拍一下队友"，放开就变成贴图区，
# 存储和审核都会失控。前端的按钮也从这里取，两边不会分叉。
EMOJIS = ("👍", "💪", "🔥", "😂", "👏", "🎉")


def api_acts(me, gid: int, body: dict) -> dict:
    """上传活动概要。只有日期/类型/距离/时长/爬升——没有轨迹、配速、心率。"""
    uid = int(me["user_id"])
    today = _today(body.get("today"))
    raw = body.get("acts")
    if not isinstance(raw, list):
        raise GroupError(400, "acts 要是数组")
    if len(raw) > ACT_MAX:
        raise GroupError(400, f"一次最多 {ACT_MAX} 条")

    rows = []
    for a in raw:
        if not isinstance(a, dict):
            continue
        sid = str(a.get("sid") or "").strip()[:32]
        if not sid or not re.fullmatch(r"[0-9a-fA-F]{8,32}", sid):
            continue
        d = _date(a.get("date"))
        if d > today + dt.timedelta(days=FUTURE_DAYS):
            continue
        kind = str(a.get("kind") or "other")
        if kind not in ACT_KINDS:
            kind = "other"
        try:
            dist = max(0, min(DAY_DIST_MAX_M, int(a.get("distance_m") or 0)))
            dur = max(0, min(DAY_DUR_MAX_S, int(a.get("duration_s") or 0)))
            elev = max(0, min(DAY_ELEV_MAX, int(a.get("elevation_m") or 0)))
        except (TypeError, ValueError):
            continue
        # 爬升密度防刷，和日报表同一个上限
        if dist > 0:
            elev = min(elev, int(dist / 1000.0 * _ELEV_PER_KM_MAX))
        rows.append((gid, uid, sid, d.isoformat(), kind, dist, dur, elev,
                     _clean_shape(a.get("shape")), _now()))

    with db() as c:
        m = _require_member(c, gid, uid)
        if (m["share_level"] or "summary") != "summary":
            raise GroupError(409, "数据共享已关闭，不上传动态")
        if rows:
            c.executemany("""
                INSERT INTO group_act(group_id,user_id,sid,date,kind,distance_m,
                                      duration_s,elevation_m,shape,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(group_id,user_id,sid) DO UPDATE SET
                    date=excluded.date, kind=excluded.kind,
                    distance_m=excluded.distance_m, duration_s=excluded.duration_s,
                    elevation_m=excluded.elevation_m,
                    -- 空剪影不要覆盖已有的：客户端丢了明细、或某次只补了
                    -- 概要，都会让 shape 变空。已有的图比没有强。
                    shape=CASE WHEN excluded.shape='' THEN group_act.shape
                               ELSE excluded.shape END
            """, rows)
        # 只留 30 天：这是「最近怎么样」的信息流，不是档案库
        cutoff = (today - dt.timedelta(days=30)).isoformat()
        c.execute("DELETE FROM group_act WHERE group_id=? AND date < ?", (gid, cutoff))
    return {"ok": True, "saved": len(rows)}


SHAPE_MAX_LEN = 8000
SHAPE_SCALE = 1000
# 3–700 个点，每个 0..1000（0.1% 精度）。服务端不信客户端，形状不对就当没有。
# 上限定 700 而不是刚好 600：客户端是「不超 600 不抽稀」，留点余量给以后的
# 采样率调整，别因为差一个点就把整条轨迹丢掉。
_SHAPE_RE = re.compile(r"\d{1,4},\d{1,4}(?:;\d{1,4},\d{1,4}){2,699}")


def _clean_shape(v) -> str:
    """校验客户端上来的剪影。不合法就当没有——宁可缺图，不要画出乱七八糟的线。"""
    s = str(v or "").strip()[:SHAPE_MAX_LEN]
    if not s or not _SHAPE_RE.fullmatch(s):
        return ""
    for seg in s.split(";"):
        x, y = seg.split(",")
        if not (0 <= int(x) <= SHAPE_SCALE and 0 <= int(y) <= SHAPE_SCALE):
            return ""
    return s


def _kind_name(k: str) -> str:
    return {"run": "跑步", "ride": "骑行", "walk": "徒步", "other": "运动"}.get(k, "运动")


def api_feed(me, gid: int, query: dict) -> dict:
    """组内动态瀑布流：最近的活动 + 每条上最靠前的两条评论。

    带 `?act=N` 时只返回那一条和它的全部评论——详情页要用，列表里每条只给
    两条，全量塞进列表会让响应大一倍还没人看。
    """
    uid = int(me["user_id"])
    only = None
    raw_act = (query.get("act") or [""])[0]
    if raw_act:
        try:
            only = int(raw_act)
        except ValueError:
            raise GroupError(400, "act 不合法")
    with db() as c:
        _group(c, gid)
        _require_member(c, gid, uid)
        mem = c.execute(
            "SELECT user_id, alias, share_level FROM group_members"
            " WHERE group_id=? AND status='active' ORDER BY joined_at, user_id",
            (gid,)).fetchall()
        # 只取开着共享的人：关掉共享的人也不该在动态里出现
        keep = {int(m["user_id"]) for m in mem
                if (m["share_level"] or "summary") == "summary"}
        if only is not None:
            acts = c.execute(
                "SELECT id, user_id, date, kind, distance_m, duration_s, elevation_m,"
                " shape FROM group_act WHERE group_id=? AND id=?",
                (gid, only)).fetchall()
        else:
            acts = c.execute(
                "SELECT id, user_id, date, kind, distance_m, duration_s, elevation_m,"
                " shape FROM group_act WHERE group_id=?"
                " ORDER BY date DESC, id DESC LIMIT ?",
                (gid, FEED_LIMIT)).fetchall()
        ids = [int(r["id"]) for r in acts]
        cmts: dict[int, list] = {}
        counts: dict[int, int] = {}
        if ids:
            marks = ",".join("?" * len(ids))
            # 倒序取：列表里要的是**最新** 3 条，不是最早 2 条。
            # 原来 ORDER BY id 取前两条，最新的讨论全被挡在外面，用户看到的
            # 永远是三天前那两句「牛」——这就是「动态太简单」的一半原因。
            for r in c.execute(
                    f"SELECT id, act_id, user_id, text, emoji, created_at FROM group_comment"
                    f" WHERE act_id IN ({marks}) ORDER BY id DESC", ids).fetchall():
                a = int(r["act_id"])
                counts[a] = counts.get(a, 0) + 1
                # 评论**全给**：组页里每条活动都能原地展开看全部，不再有
                # 「点进去看更多」那一步。评论本来就少（限流 30/小时），
                # 40 条动态加起来通常不到 100 条、几十 KB。
                cmts.setdefault(a, []).append({
                    "id": int(r["id"]),
                    "user_id": int(r["user_id"]),
                    # 只有自己的评论才给编辑/删除入口——判断放服务端，
                    # 客户端别自己比 user_id（前端没有「我是谁」）
                    "mine": int(r["user_id"]) == uid,
                    "text": str(r["text"] or ""),
                    "emoji": str(r["emoji"] or "")})
            # 倒序取是为了「留哪几条」，展示仍按时间正序（像对话，最新的在最后）
            for a in cmts:
                cmts[a].reverse()

    color = {int(m["user_id"]): MAP_COLORS[i % len(MAP_COLORS)]
             for i, m in enumerate(mem)}
    alias = {int(m["user_id"]): m["alias"] for m in mem}
    out = []
    for r in acts:
        u = int(r["user_id"])
        if u not in keep:
            continue
        a = int(r["id"])
        out.append({
            "id": a, "user_id": u, "alias": alias.get(u, ""),
            "color": color.get(u, MAP_COLORS[0]),
            "is_me": u == uid,
            "date": str(r["date"]),
            "kind": str(r["kind"]), "kind_name": _kind_name(str(r["kind"])),
            "distance_m": int(r["distance_m"] or 0),
            "duration_s": int(r["duration_s"] or 0),
            "elevation_m": int(r["elevation_m"] or 0),
            # 空串 = 没有 GPS 或旧数据，客户端画类型占位图
            "shape": str(r["shape"] or ""),
            "comments": counts.get(a, 0),
            "recent": [dict(x, alias=alias.get(int(x["user_id"]), ""))
                       for x in cmts.get(a, [])],
        })
    return {"ok": True, "rows": out, "emojis": list(EMOJIS),
            "hidden": sum(1 for m in mem if (m["share_level"] or "summary") != "summary")}


NEWS_LIMIT = 5          # 通知里每类最多给几条：通知栏不需要 20 条


def api_news(me, gid: int, query: dict) -> dict:
    """自 `since` 以来**跟我有关**的新东西，给后台通知用。

    回四类：别人评论了我的动态、别人在我参与过的动态下发言、组里新建了赛事、
    队友今天跑了。前两类是「有人跟我说话」，后两类是「组里发生了什么」。

    队友跑步**只报今天的**：补传会把十几天的历史动态一起插进来，用 created_at
    判断的话，用户第一次打开会被十几条「队友跑了」淹没。按活动日期等于今天
    过滤，补历史就自然不会触发。

    刻意做得很轻：不带动态正文和轨迹剪影。后台每 6 小时替所有组拉一次，
    响应大一点就是长期白烧流量。所以只给「谁、干了什么、什么时候、说了几个字」。

    `since` 缺失或 ≤0 时只回当前时间、不返回条目：第一次装不该在通知栏里
    堆一屏三个月前的历史。
    """
    uid = int(me["user_id"])
    try:
        since = int((query.get("since") or ["0"])[0] or 0)
    except (TypeError, ValueError):
        raise GroupError(400, "since 不合法")
    now = _now()
    with db() as c:
        _group(c, gid)
        _require_member(c, gid, uid)
        if since <= 0:
            return {"ok": True, "now": now, "since": now,
                    "comments": [], "challenges": [], "acts": []}

        # 跟我有关 = 评论落在我发的动态上，或者落在我评论过的动态上。
        # 第二个条件用 EXISTS 而不是再查一遍评论表：同一条动态下我评过 5 次
        # 也只该算一条通知。
        cms = c.execute(
            "SELECT cm.id, cm.act_id, cm.user_id, cm.text, cm.created_at,"
            "       a.user_id AS act_owner,"
            "       COALESCE(NULLIF(m.alias, ''), '队友') AS alias"
            "  FROM group_comment cm"
            "  JOIN group_act a ON a.id = cm.act_id AND a.group_id = cm.group_id"
            "  LEFT JOIN group_members m"
            "         ON m.group_id = cm.group_id AND m.user_id = cm.user_id"
            " WHERE cm.group_id = ? AND cm.created_at > ? AND cm.user_id <> ?"
            "   AND (a.user_id = ?"
            "        OR EXISTS (SELECT 1 FROM group_comment mine"
            "                    WHERE mine.act_id = cm.act_id AND mine.user_id = ?))"
            " ORDER BY cm.created_at DESC LIMIT ?",
            (gid, since, uid, uid, uid, NEWS_LIMIT)).fetchall()

        chs = c.execute(
            "SELECT ch.id, ch.title, ch.kind, ch.created_at, ch.created_by,"
            "       COALESCE(NULLIF(m.alias, ''), '队友') AS alias"
            "  FROM group_challenges ch"
            "  LEFT JOIN group_members m"
            "         ON m.group_id = ch.group_id AND m.user_id = ch.created_by"
            " WHERE ch.group_id = ? AND ch.created_at > ? AND ch.archived = 0"
            "   AND ch.created_by <> ?"
            " ORDER BY ch.created_at DESC LIMIT ?",
            (gid, since, uid, NEWS_LIMIT)).fetchall()

        # 队友今天的跑步。按 (人, 日期) 归一条：同一个人一天跑两次只报一次。
        # 关了共享的人不报——他们本来就不在榜单和动态里。
        # 这条必须在 with 里：db() 退出就把连接关了。
        today = _today(None).isoformat()
        acts = c.execute(
            "SELECT a.user_id, a.date, MAX(a.distance_m) AS d, MAX(a.created_at) AS t,"
            "       COUNT(*) AS n,"
            "       COALESCE(NULLIF(m.alias, ''), '队友') AS alias"
            "  FROM group_act a"
            "  JOIN group_members m ON m.group_id = a.group_id AND m.user_id = a.user_id"
            " WHERE a.group_id=? AND a.created_at > ? AND a.user_id <> ?"
            "   AND a.date = ? AND m.status = 'active'"
            "   AND (m.share_level IS NULL OR m.share_level = 'summary')"
            " GROUP BY a.user_id, a.date ORDER BY t DESC LIMIT ?",
            (gid, since, uid, today, NEWS_LIMIT)).fetchall()

    comments = [{
        "id": int(r["id"]), "act_id": int(r["act_id"]),
        "by": str(r["alias"]), "at": int(r["created_at"]),
        # 评论我的动态 vs 在我参与过的动态下发言，通知文案不一样
        "on_my_act": int(r["act_owner"]) == uid,
        # 正文截 40 字：通知里放不下，完整内容点进去看
        "text": str(r["text"] or "")[:40],
    } for r in cms]
    challenges = [{
        "id": int(r["id"]), "title": str(r["title"]),
        "kind": str(r["kind"]), "by": str(r["alias"]),
        "at": int(r["created_at"]),
    } for r in chs]
    return {"ok": True, "now": now, "since": since, "comments": comments,
            "challenges": challenges,
            "acts": [{
                "user_id": int(r["user_id"]), "by": str(r["alias"]),
                "date": str(r["date"]), "distance_m": int(r["d"] or 0),
                "acts": int(r["n"] or 0), "at": int(r["t"] or 0),
            } for r in acts]}


def api_comment(me, gid: int, body: dict) -> dict:
    """给一条动态留一句话。

    表情**内嵌在 text 里**（一次可以好几个、能和文字连成一句），所以正常
    只要 text 非空就行。`emoji` 参数留着兼容旧客户端：老版本把表情单独放在
    这一列，仍按白名单校验；新版本不传它，表情都进了 text。
    """
    uid = int(me["user_id"])
    try:
        act_id = int(body.get("act"))
    except (TypeError, ValueError):
        raise GroupError(400, "act 不合法")
    text = str(body.get("text") or "").strip()[:COMMENT_MAX]
    emoji = str(body.get("emoji") or "").strip()
    if emoji and emoji not in EMOJIS:
        raise GroupError(400, "这个表情不在可选范围里")
    if not text and not emoji:
        raise GroupError(400, "说点什么，或者挑一个表情")
    _rate_limit(f"u{uid}", "comment", 30, window=3600)

    with db() as c:
        _group(c, gid)
        _require_member(c, gid, uid)
        row = c.execute("SELECT user_id FROM group_act WHERE id=? AND group_id=?",
                        (act_id, gid)).fetchone()
        if not row:
            raise GroupError(404, "这条动态不在了")
        # 评论的是别人的动态，所以被评论者必须还开着共享——否则等于绕过了
        # 他「别拿我数据做比较」的选择
        owner = c.execute("SELECT share_level FROM group_members"
                          " WHERE group_id=? AND user_id=?",
                          (gid, int(row["user_id"]))).fetchone()
        if not owner or (owner["share_level"] or "summary") != "summary":
            raise GroupError(409, "对方关闭了共享，不能评论")
        c.execute("INSERT INTO group_comment(group_id,act_id,user_id,text,emoji,created_at)"
                  " VALUES(?,?,?,?,?,?)", (gid, act_id, uid, text, emoji, _now()))
        cid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    return {"ok": True, "id": int(cid), "text": text, "emoji": emoji}


def _my_comment(c, gid: int, uid: int, cid: int):
    """取一条属于**本人**的评论。不是自己的就当没找到，不告诉调用方「存在但无权」。"""
    row = c.execute("SELECT id, user_id, act_id FROM group_comment"
                    " WHERE id=? AND group_id=?", (cid, gid)).fetchone()
    if not row or int(row["user_id"]) != uid:
        raise GroupError(404, "这条评论不在了")
    return row


def api_comment_edit(me, gid: int, body: dict) -> dict:
    """改自己的评论。只能改自己的：别人的评论连「存在」都不确认。

    表情同样在 text 里改（见 api_comment）：编辑框里本来就带着原文本，
    用户在里面增删表情即可，不再需要单独的 emoji 字段。
    """
    uid = int(me["user_id"])
    try:
        cid = int(body.get("id"))
    except (TypeError, ValueError):
        raise GroupError(400, "id 不合法")
    text = str(body.get("text") or "").strip()[:COMMENT_MAX]
    emoji = str(body.get("emoji") or "").strip()
    if emoji and emoji not in EMOJIS:
        raise GroupError(400, "这个表情不在可选范围里")
    if not text and not emoji:
        raise GroupError(400, "说点什么，或者挑一个表情")
    _rate_limit(f"u{uid}", "comment_edit", 60, window=3600)

    with db() as c:
        _group(c, gid)
        _require_member(c, gid, uid)
        _my_comment(c, gid, uid, cid)
        c.execute("UPDATE group_comment SET text=?, emoji=? WHERE id=?",
                  (text, emoji, cid))
    return {"ok": True, "id": cid, "text": text, "emoji": emoji}


def api_comment_del(me, gid: int, body: dict) -> dict:
    """删自己的评论。"""
    uid = int(me["user_id"])
    try:
        cid = int(body.get("id"))
    except (TypeError, ValueError):
        raise GroupError(400, "id 不合法")
    _rate_limit(f"u{uid}", "comment_del", 60, window=3600)

    with db() as c:
        _group(c, gid)
        _require_member(c, gid, uid)
        _my_comment(c, gid, uid, cid)
        c.execute("DELETE FROM group_comment WHERE id=?", (cid,))
    return {"ok": True, "id": cid}


def api_season(me, gid: int, query: dict) -> dict:
    """本周的六件衫 + 探索徽章 + 赛季分 + 名次 + 称号 + 让分/预言的加分。

    赛季分满分 100：基础 90（里程/出勤/爬升/连续/进步）+ 让分预言加分 ≤10
    （见 ranking.py 顶部说明）。页面别写成「x/100」以外的比例，也别拿
    90 当满分去算百分比。

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
            "SELECT user_id, date, distance_m, elevation_m, acts, plan_done, explorer"
            " FROM group_daily WHERE group_id=? AND date BETWEEN ? AND ?"
            " ORDER BY date", (gid, since, wt.isoformat())).fetchall()
        # 让分赛与预言：加分要并进本赛季分，所以必须在这一个请求里算完，
        # 不能等页面再发一次 /week——那样榜单和分数会对不上。
        goals = _goals(c, gid, wf)
        preds = c.execute("SELECT user_id, target_uid FROM group_predict"
                          " WHERE group_id=? AND week_start=?",
                          (gid, wf.isoformat())).fetchall()

    shared = [m for m in mem if (m["share_level"] or "summary") == "summary"]
    keep = {int(m["user_id"]) for m in shared}
    aliases = {int(m["user_id"]): m["alias"] for m in shared}

    this_w: dict[int, list[dict]] = {u: [] for u in keep}
    prev_w: dict[int, list[dict]] = {u: [] for u in keep}
    weeks: dict[int, set] = {u: set() for u in keep}
    # 进步（白衫 + 赛季分里的 15 分）用**滚动 7 天对 7 天**，不按日历周：
    # 窗口终点是今天，不是周日。本周四拿 4 天的里程去比上周整周 7 天，
    # 分母永远是 7，全组都显示 -60% 上下（线上实测 -62%），白衫就废了。
    r_end = min(today, wt)
    n = ranking.PROGRESS_WINDOW_DAYS
    r_a0 = r_end - dt.timedelta(days=n - 1)          # 最近 7 天
    r_b1 = r_a0 - dt.timedelta(days=1)
    r_b0 = r_b1 - dt.timedelta(days=n - 1)           # 再往前 7 天
    roll_km: dict[int, list[float]] = {u: [0.0, 0.0] for u in keep}
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
        try:
            d0 = dt.date.fromisoformat(d[:10])
        except ValueError:
            continue
        if r_a0 <= d0 <= r_end:
            roll_km[u][0] += float(r["distance_m"] or 0) / 1000.0
        elif r_b0 <= d0 <= r_b1:
            roll_km[u][1] += float(r["distance_m"] or 0) / 1000.0

    # 实际里程按本周已上报的算。预言里「没上报」就是 0 km，会被判没完成——
    # 这是服务端唯一的态度：只信上报。
    actual = {u: round(sum(float(x["distance_m"] or 0) for x in lst) / 1000.0, 1)
              for u, lst in this_w.items()}
    pres = ranking.prediction_result(
        {int(r["user_id"]): int(r["target_uid"]) for r in preds}, actual, goals)

    out = ranking.build(this_w, prev_w,
                        {u: len(weeks[u]) for u in keep}, aliases,
                        bonus=pres["bonus"],
                        roll_by_user={u: (v[0], v[1]) for u, v in roll_km.items()})
    out["ok"] = True
    out["from"] = wf.isoformat()
    out["to"] = wt.isoformat()
    out["me"] = next((p for p in out["people"]
                      if int(p["user_id"]) == uid), None)
    out["hidden"] = len(mem) - len(shared)
    out["goal"] = {"km": goals.get(uid),
                   "max": GOAL_MAX_KM,
                   "reported": uid in goals}
    out["predict"] = {"mine": next((int(r["target_uid"]) for r in preds
                                    if int(r["user_id"]) == uid), None),
                      "bonus": pres["bonus"]}
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
            "season": _safe(api_season, me, gid, query),
            # 本周玩法（目标 / 对子 / 预言）。合并进来而不是让页面再发一次：
            # 手机上多一个请求就是多一次 TLS 握手，不值。
            "week": _safe(api_week, me, gid, query)}


# ---------------------------------------------------------------- 榜单
# 综合榜的骑行折算：骑行 4 km 记 1 km 综合距离。
# 只影响「综合榜」。跑步榜、六件衫、探索徽章、赛季积分的口径全部不动——
# 那些都是「跑步」的语义，掺进骑行会让「里程王」变得说不清。
RIDE_DIVISOR = 4.0


def api_board(me, gid: int, query: dict) -> dict:
    uid = int(me["user_id"])
    rng = (query.get("range") or ["week"])[0]
    # by=dist 是原来的里程榜（只算跑步）；by=comp 是月榜的「综合榜」。
    # 只做月榜：周榜已经挂了衫和探索徽章，再加一种排序只会让「本周谁是第一」
    # 变得有歧义。
    by = (query.get("by") or ["dist"])[0]
    if by not in ("dist", "comp"):
        raise GroupError(400, "by 只能是 dist 或 comp")
    if by == "comp" and rng != "month":
        raise GroupError(400, "综合榜只提供月榜")
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
                   SUM(ride_m) AS ride,
                   -- 只数真正有运动的天：1.6.0 起「计划休息且确实没动」的
                   -- 日子也会上报一行（全 0，只为带 vigor），COUNT(*) 会把
                   -- 休息日算成出勤，出勤王的次要排序和「N 天」都会偏大。
                   SUM(CASE WHEN acts > 0 THEN 1 ELSE 0 END) AS days,
                   SUM(CASE WHEN plan_done=1 THEN 1 ELSE 0 END) AS plan_ok,
                   SUM(CASE WHEN plan_done IS NOT NULL THEN 1 ELSE 0 END) AS plan_total,
                   SUM(explorer) AS expl,
                   MAX(updated_at) AS seen
            FROM group_daily
            WHERE group_id=? AND date BETWEEN ? AND ?
            GROUP BY user_id
        """, (gid, s, e)).fetchall()
    # 注意别叫 `by`：上面那个 by 是「按什么排」，这里是人到汇总行的映射
    agg_by = {int(r["user_id"]): r for r in agg}

    rows, hidden = [], 0
    for m in members:
        if m["share_level"] != "summary":
            hidden += 1
            continue
        a = agg_by.get(int(m["user_id"]))
        plan_total = int(a["plan_total"] or 0) if a else 0
        _dist = int(a["dist"] or 0) if a else 0
        _ride = int(a["ride"] or 0) if a else 0
        rows.append({
            # user_id 必带：客户端要用它挂「穿了哪几件衫」「探索徽章」。
            # 原来只给 alias，组里两个人叫「小王」时徽章会挂到错的人头上。
            "user_id": int(m["user_id"]),
            "alias": m["alias"],
            "is_me": int(m["user_id"]) == uid,
            "distance_m": _dist,
            # 骑行原始里程，给界面显示分项用（「跑步 42 + 骑行 96」）
            "ride_m": _ride,
            # 综合距离：跑步 + 骑行/RIDE_DIVISOR。四舍五入到整数米，
            # 否则 4 舍 5 入的误差会在 30 天里攒出几百米。
            "comp_m": _dist + int(round(_ride / RIDE_DIVISOR)),
            "duration_s": int(a["dur"] or 0) if a else 0,
            "acts": int(a["acts"] or 0) if a else 0,
            "days": int(a["days"] or 0) if a else 0,
            # 计划完成率：没有计划的那几天不进分母，不伪造 0
            "plan_rate": (round(int(a["plan_ok"] or 0) / plan_total, 3) if plan_total else None),
            # 探索徽章的原料：本周新踩格数。榜单要能标一句「新 N 格」，
            # 否则徽章只在 season 里发，页面上根本看不见。
            "explorer": int(a["expl"] or 0) if a else 0,
            "updated_at": int(a["seen"] or 0) if a else 0,
        })
    # 综合榜按综合距离排，里程榜按跑步里程排。
    # 其次天数——同距离时「跑得更分散」的人排前面。
    _sort_key = "comp_m" if by == "comp" else "distance_m"
    rows.sort(key=lambda x: (-x[_sort_key], -x["days"], -x["duration_s"]))
    return {
        "ok": True, "range": rng, "by": by, "from": s, "to": e,
        "rows": rows, "hidden": hidden,
        "summary": {
            "members": len(members), "shown": len(rows), "hidden": hidden,
            "total_distance_m": sum(x["distance_m"] for x in rows),
            "total_ride_m": sum(x["ride_m"] for x in rows),
            "total_comp_m": sum(x["comp_m"] for x in rows),
        },
    }


# ---------------------------------------------------------------- 成员管理
def purge_user(uid: int) -> dict:
    """把一个人在本服务端的全部小组痕迹删干净（账号注销时调用）。

    注销是**不可逆**的，所以先过两道闸：
    1. 他还是某个组的组长、且组里还有别的活跃成员 → 拒绝，让他先转让
       （直接删会让那个组永远没有组长，谁也管不了）；
    2. 组里只剩他一个人 → 把组归档，而不是留一个空组占着邀请码。

    删的内容：成员行、日报、评论、目标、预言、对子、格子、活动概要。
    审计表保留（只存 id，不含昵称和里程），用于事后查「谁解散过组」。
    """
    uid = int(uid)
    out = {"groups_left": 0, "archived": 0}
    with db() as c:
        owned = c.execute("SELECT group_id FROM group_members WHERE user_id=?"
                          " AND status='active' AND role='owner'", (uid,)).fetchall()
        for r in owned:
            gid = int(r["group_id"])
            others = c.execute(
                "SELECT COUNT(*) FROM group_members WHERE group_id=? AND status='active'"
                " AND user_id<>?", (gid, uid)).fetchone()[0]
            if others:
                raise GroupError(
                    409, "你还是这个组的组长，先把组长转给别人（或解散这个组）再注销账号")
            c.execute("UPDATE groups SET archived=1 WHERE id=?", (gid,))
            out["archived"] += 1
        for r in c.execute("SELECT group_id FROM group_members WHERE user_id=?", (uid,)).fetchall():
            gid = int(r["group_id"])
            c.execute("DELETE FROM group_members WHERE group_id=? AND user_id=?", (gid, uid))
            c.execute("DELETE FROM group_daily WHERE group_id=? AND user_id=?", (gid, uid))
            c.execute("DELETE FROM group_goal WHERE group_id=? AND user_id=?", (gid, uid))
            c.execute("DELETE FROM group_predict WHERE group_id=? AND (user_id=? OR target_uid=?)",
                      (gid, uid, uid))
            c.execute("DELETE FROM group_pair WHERE group_id=? AND (small_uid=? OR big_uid=?)",
                      (gid, uid, uid))
            c.execute("DELETE FROM group_cell WHERE group_id=? AND user_id=?", (gid, uid))
            c.execute("DELETE FROM group_act WHERE group_id=? AND user_id=?", (gid, uid))
            c.execute("DELETE FROM group_comment WHERE group_id=? AND user_id=?", (gid, uid))
            out["groups_left"] += 1
        # 别处引用我的评论/活动（我是被评论的对象，评论本身不删——那是别人说的话）
    return out


def api_leave(me, gid: int) -> dict:
    uid = int(me["user_id"])
    with db() as c:
        _group(c, gid)
        m = _require_member(c, gid, uid)
        if m["role"] == "owner":
            raise GroupError(409, "组长不能直接退出，先转让给别人或解散这个组")
        c.execute("UPDATE group_members SET status='left' WHERE group_id=? AND user_id=?",
                  (gid, uid))
        # 退出即删本组内我的明细：留在库里等于「退出了但数据还在」。
        # 原来只删了日汇总，动态、格子、路线、评论都还挂着——地图过滤了
        # active 成员所以看不出来，但路线页不过滤，退组的人路线还能被点开。
        for _t in ("group_daily", "group_act", "group_cell",
                   "group_route", "group_comment"):
            c.execute(f"DELETE FROM {_t} WHERE group_id=? AND user_id=?", (gid, uid))
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


# 彻底删除时要清的表。表名是常量白名单，不接受任何外部输入拼接。
PURGE_TABLES = (
    "group_daily", "group_act", "group_comment", "group_route", "group_cell",
    "group_audit", "group_goal", "group_pair", "group_predict", "group_challenges",
    "group_members",
    # 批注在路线之上，必须先于 group_route 之外单独列出来；
    # 照片文件另外删（见 api_purge），只删行会留下孤儿文件。
    "group_route_note",
    # 远征与营地：这几张在 expedition.py 里建，同样带 group_id。
    # 漏了它们的话「彻底删除」只删掉一半——榜单没了、远征记录还躺在库里。
    "group_run_season", "group_run_member", "group_run_event", "group_run_trial",
    "group_run_buff", "group_run_camp",
)


def api_archived(me) -> dict:
    """我作为组长、已经解散的组。

    App 的组列表只查 `archived=0`，所以这些组在手机上完全看不到——
    它们的唯一出口就是在这里彻底删掉。
    """
    uid = int(me["user_id"])
    with db() as c:
        rows = c.execute(
            "SELECT g.id, g.name,"
            "       (SELECT COUNT(*) FROM group_members x WHERE x.group_id=g.id) AS n"
            "  FROM groups g WHERE g.owner_id=? AND g.archived=1"
            " ORDER BY g.id DESC", (uid,)).fetchall()
    return {"ok": True, "groups": [{
        "id": int(r["id"]), "name": str(r["name"]),
        "members": int(r["n"] or 0)} for r in rows]}


def api_purge(me, gid: int, body: dict) -> dict:
    """彻底删除一个已解散的组：所有 group_* 行 + groups 行，删了没有恢复。

    三道门槛，缺一不可：
    - 只有 owner 能删（解散后组长身份还留着）
    - 只有 `archived=1` 能删：活跃组必须先解散，避免手滑删掉正在用的组
    - 必须把组名原样传回来确认：这一步不是形式，删完什么都不会剩
    """
    uid = int(me["user_id"])
    confirm = str(body.get("confirm") or "").strip()
    with db() as c:
        g = c.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()
        if not g:
            raise GroupError(404, "没有这个组")
        if int(g["owner_id"]) != uid:
            raise GroupError(403, "只有组长能删除")
        if int(g["archived"] or 0) != 1:
            raise GroupError(409, "先解散再删除")
        if confirm != str(g["name"]):
            # 把组名写进提示里：只说「对不上」，用户不知道要输什么
            raise GroupError(400, "确认框里要一字不差地输入「" + str(g["name"]) + "」")
        # 老库/未初始化远征模块的库里可能缺表，先取一次实际存在的
        have = {r["name"] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        # 照片文件要在删行之前拿到名字，删完行就查不到了
        _imgs = []
        if "group_route_note" in have:
            _imgs = [str(x["image"] or "") for x in c.execute(
                "SELECT image FROM group_route_note WHERE group_id=?",
                (gid,)).fetchall()]
        for t in PURGE_TABLES:
            if t in have:
                c.execute(f"DELETE FROM {t} WHERE group_id=?", (gid,))
        c.execute("DELETE FROM groups WHERE id=?", (gid,))
    for _f in _imgs:
        if _f:
            try:
                (ROUTE_IMG_DIR / _f).unlink(missing_ok=True)
            except OSError:
                pass
    return {"ok": True, "purged": gid}


def api_set_role(me, gid: int, body: dict) -> dict:
    """任命或撤销教练 / 管理员。只有组长能改。

    coach 只多四件事（建赛事、结束赛事、看审计、升级营地），人事权限不给：
    教练在跑团里负责训练安排，不该决定谁进谁出。
    组长自己的角色不能在这里改，换组长走 transfer。
    """
    uid = int(me["user_id"])
    try:
        target = int(body.get("user_id") or 0)
    except (TypeError, ValueError):
        raise GroupError(400, "user_id 不合法")
    role = str(body.get("role") or "").strip()
    if role not in ("coach", "admin", "member"):
        raise GroupError(400, "只能设成教练、管理员或成员")
    with db() as c:
        _require_owner(c, gid, uid)
        m = _member(c, gid, target)
        if not m or m["status"] != "active":
            raise GroupError(404, "这个人不在组里")
        if target == uid or str(m["role"]) == "owner":
            raise GroupError(409, "不能改组长自己的角色")
        c.execute("UPDATE group_members SET role=? WHERE group_id=? AND user_id=?",
                  (role, gid, target))
        _audit(c, gid, uid, "role", target)
    return {"ok": True, "role": role}


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
        # 解散 = 这个组对所有成员都结束了。不改成员状态的话，他们会一直
        # 占着自己的「最多 3 个组」名额（见 api_join 的注释）。
        c.execute("UPDATE group_members SET status='left'"
                  " WHERE group_id=? AND status='active'", (gid,))
        _audit(c, gid, uid, "archive")
    return {"ok": True}


# ---------------------------------------------------------------- 赛事
# 五种赛事共用一条聚合 SQL：把区间内的距离 / 时长 / 次数 / 爬升 / 格子
# 一起 SUM 出来，再按 kind 取对应的那个。速度不能直接 SUM（它是比值），
# 要拿 dist + dur 现算，所以干脆五个量都取。
def _chal_cur(kind: str, dist: float, dur: float, acts: float,
              elev: float, expl: float) -> int:
    """把一次聚合结果换算成这个 kind 的「当前值」，单位与 target 一致。

    speed 的当前值是**全组平均配速**（秒/公里），不是某个人的配速：赛事
    是「全组一起完成」，按加权平均算才不会被一个人的慢跑拖垮整组，也不会
    被一个 3 公里的快跑刷出假成绩。
    """
    if kind == "distance":
        return int(dist or 0)
    if kind == "acts":
        return int(acts or 0)
    if kind == "elevation":
        return int(elev or 0)
    if kind == "explore":
        return int(expl or 0)
    if kind == "speed":
        d = float(dist or 0)
        return int(round(float(dur or 0) * 1000 / d)) if d > 0 else 0
    return 0


def _chal_pct(kind: str, target: float, cur: float) -> float:
    """完成度。速度是「越小越好」，所以分子分母调个个儿。"""
    target, cur = float(target or 0), float(cur or 0)
    if kind == "speed":
        return min(100.0, round(target * 100 / cur, 1)) if cur > 0 else 0.0
    return min(100.0, round(cur * 100 / target, 1)) if target > 0 else 0.0


def _challenge_view(ch, today: dt.date, current: int | None = None) -> dict:
    closed = ch["closed_at"] is not None
    ended = str(ch["end_date"]) < today.isoformat()
    status = "closed" if closed else ("ended" if ended else "running")
    out = {
        "id": ch["id"], "title": ch["title"], "kind": ch["kind"],
        "target": ch["target"], "from": ch["start_date"], "to": ch["end_date"],
        "status": status, "created_by": ch["created_by"],
        # 单位下发，页面不用为每一种类型各写一份换算表
        "unit": CHALLENGE_KINDS.get(str(ch["kind"]), (0, 0, ""))[2],
    }
    if current is not None:
        out["current"] = current
        # 分母为 0 时也要返回小数：`else 0` 是 int，而正常路径是 float，
        # 同一个字段两种类型会让客户端强类型模型直接崩。
        out["pct"] = _chal_pct(str(ch["kind"]), ch["target"], current)
    return out


def api_challenge_create(me, gid: int, body: dict) -> dict:
    uid = int(me["user_id"])
    title = str(body.get("title") or "").strip()
    if not 1 <= len(title) <= 30:
        raise GroupError(400, "标题 1–30 个字")
    kind = str(body.get("kind") or "").strip()
    if kind not in CHALLENGE_KINDS:
        raise GroupError(400, "kind 只能是 " + " / ".join(CHALLENGE_KINDS))
    try:
        target = int(body.get("target"))
    except (TypeError, ValueError):
        raise GroupError(400, "target 要是整数")
    lo, hi, unit = CHALLENGE_KINDS[kind]
    if not lo <= target <= hi:
        raise GroupError(400, f"目标要在 {lo}–{hi} 之间（单位 {unit}）")

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
        _require_staff(c, gid, uid)
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
        # 每个赛事的全组进度，一次查完再分组，避免 N+1。
        # 五种 kind 需要的五个量一起 SUM（速度要 dist+dur 现算），
        # 只统计还在组里、共享开着的人——关掉共享的人不该被算进「全组」。
        prog = c.execute("""
            SELECT ch.id AS challenge_id,
                   COALESCE(SUM(d.distance_m), 0) AS dist,
                   COALESCE(SUM(d.duration_s), 0) AS dur,
                   COALESCE(SUM(d.acts), 0) AS acts,
                   COALESCE(SUM(d.elevation_m), 0) AS elev,
                   COALESCE(SUM(d.explorer), 0) AS expl
              FROM group_challenges ch
              LEFT JOIN group_daily d
                     ON d.group_id = ch.group_id
                    AND d.date BETWEEN ch.start_date AND ch.end_date
                    AND d.user_id IN (SELECT user_id FROM group_members
                                       WHERE group_id = ch.group_id
                                         AND status = 'active'
                                         AND share_level = 'summary')
             WHERE ch.group_id=? AND ch.archived=0
             GROUP BY ch.id
        """, (gid,)).fetchall()
    by = {int(r["challenge_id"]): r for r in prog}
    out = []
    for r in rows:
        agg = by.get(int(r["id"]))
        cur = _chal_cur(str(r["kind"]), agg["dist"] if agg else 0,
                        agg["dur"] if agg else 0, agg["acts"] if agg else 0,
                        agg["elev"] if agg else 0, agg["expl"] if agg else 0)
        out.append(_challenge_view(r, today, cur))
    return {"ok": True, "challenges": out}


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
            SELECT user_id, date, distance_m, duration_s, acts, elevation_m, explorer
            FROM group_daily
            WHERE group_id=? AND date BETWEEN ? AND ?
            ORDER BY user_id, date
        """, (gid, ch["start_date"], ch["end_date"])).fetchall()

    kind = str(ch["kind"])
    target = int(ch["target"])
    acc: dict[int, float] = {}       # 累加类（里程/次数/爬升/格子）
    dist: dict[int, float] = {}      # 速度：累计距离
    dur: dict[int, float] = {}       # 速度：累计时长
    done_at: dict[int, str] = {}
    # 每种 kind 累加哪一列，只在这一处写一遍
    _col = {"distance": "distance_m", "acts": "acts",
            "elevation": "elevation_m", "explore": "explorer"}
    for r in raw:
        u = int(r["user_id"])
        if kind == "speed":
            # 逐日累计：速度的「当前配速」是累计配速，不是单日配速，
            # 所以达标日要按当天累计后重算，不能直接比单日值。
            dist[u] = dist.get(u, 0.0) + float(r["distance_m"] or 0)
            dur[u] = dur.get(u, 0.0) + float(r["duration_s"] or 0)
            cur = _chal_cur(kind, dist[u], dur[u], 0, 0, 0)
        else:
            acc[u] = acc.get(u, 0.0) + float(r[_col.get(kind, "distance_m")] or 0)
            cur = int(acc[u])
        # 首次达标日：按累计第一次「够」的那天
        if u not in done_at and _chal_pct(kind, target, cur) >= 100:
            done_at[u] = r["date"]

    rows, hidden = [], 0
    for m in members:
        if m["share_level"] != "summary":
            hidden += 1
            continue
        u = int(m["user_id"])
        cur = (_chal_cur(kind, dist.get(u, 0.0), dur.get(u, 0.0), 0, 0, 0)
               if kind == "speed" else int(acc.get(u, 0)))
        rows.append({
            "alias": m["alias"], "is_me": u == uid,
            "current": cur,
            "pct": _chal_pct(kind, target, cur),
            "done_at": done_at.get(u),
        })
    # 已达成按达成日期升序，未达成按完成率降序
    rows.sort(key=lambda x: (x["done_at"] is None, x["done_at"] or "", -x["pct"]))
    # 全组进度也一起给：详情页要做头部（总进度条 + 百分比），而速度类不能拿
    # 每人配速求平均——那是「平均的平均」，得用总时长 / 总距离现算。
    if kind == "speed":
        total = _chal_cur(kind, sum(dist.values()), sum(dur.values()), 0, 0, 0)
    else:
        total = int(sum(acc.values()))
    return {"ok": True, "challenge": _challenge_view(ch, today, total),
            "rows": rows, "hidden": hidden}


def api_challenge_close(me, gid: int, cid: int) -> dict:
    uid = int(me["user_id"])
    with db() as c:
        _require_staff(c, gid, uid)
        ch = c.execute("SELECT * FROM group_challenges WHERE id=? AND group_id=? AND archived=0",
                       (cid, gid)).fetchone()
        if not ch:
            raise GroupError(404, "赛事不存在")
        if ch["closed_at"] is None:
            c.execute("UPDATE group_challenges SET closed_at=? WHERE id=?", (_now(), cid))
            _audit(c, gid, uid, "challenge_close", cid)
        ch2 = c.execute("SELECT * FROM group_challenges WHERE id=?", (cid,)).fetchone()
    return {"ok": True, "challenge": _challenge_view(ch2, dt.date.today())}


# ---------------------------------------------------------------- 数据保留
# 保留期只写这一处，docs/用户组设计.md §11.12 跟着这份改。
# 最短的 90 天也远大于一个赛季（4 周），所以清理不会把正在进行的赛季删掉。
RETENTION_DAYS = {
    "daily": 365,      # 日汇总：一年，跨年对比还要用
    "act": 180,        # 动态：半年
    "comment": 180,    # 评论跟动态一起走
    "route": 180,      # 路线：半年（主动分享的，也不该永久存档）
    "audit": 90,       # 审计：三个月，够追责
}
KEEP_WEEKS = 4         # 足迹格子：只留最近 4 周，再往前没人看


def cleanup(dry_run: bool = True) -> dict:
    """按保留期清理组数据，返回每张表处理了多少行。

    为什么按「日」而不是「行数上限」：行数上限会让活跃组的近期数据被
    老组挤掉，而且「为什么我的记录少了」根本解释不清。按天说才站得住。

    `dry_run=True` 只统计，不动数据——线上第一次跑必须先看这个数。
    """
    today = dt.date.today()
    now = int(time.time())
    out: dict[str, int] = {}
    with db() as c:
        for name, days in RETENTION_DAYS.items():
            cut_day = (today - dt.timedelta(days=days)).isoformat()
            cut_ts = now - days * 86400
            if name == "daily":
                q, arg = "DELETE FROM group_daily WHERE date < ?", cut_day
                nq, narg = "SELECT COUNT(*) n FROM group_daily WHERE date < ?", cut_day
            elif name == "act":
                q, arg = "DELETE FROM group_act WHERE date < ?", cut_day
                nq, narg = "SELECT COUNT(*) n FROM group_act WHERE date < ?", cut_day
            elif name == "comment":
                q, arg = "DELETE FROM group_comment WHERE created_at < ?", cut_ts
                nq, narg = "SELECT COUNT(*) n FROM group_comment WHERE created_at < ?", cut_ts
            elif name == "route":
                q, arg = "DELETE FROM group_route WHERE created_at < ?", cut_ts
                nq, narg = "SELECT COUNT(*) n FROM group_route WHERE created_at < ?", cut_ts
            else:                                   # audit
                q, arg = "DELETE FROM group_audit WHERE ts < ?", cut_ts
                nq, narg = "SELECT COUNT(*) n FROM group_audit WHERE ts < ?", cut_ts
            n = int(c.execute(nq, (narg,)).fetchone()["n"])
            if n and not dry_run:
                c.execute(q, (arg,))
            out[name] = n

        # 格子按周清：保留最近 KEEP_WEEKS 个 week_start。用「第 4 新的周」
        # 当下界，而不是算 4×7 天——周起点是周一，算天数会切在一周中间。
        weeks = [r["week_start"] for r in c.execute(
            "SELECT DISTINCT week_start FROM group_cell"
            " ORDER BY week_start DESC LIMIT ?", (KEEP_WEEKS,)).fetchall()]
        if weeks:
            cut = weeks[-1]
            n = int(c.execute("SELECT COUNT(*) n FROM group_cell WHERE week_start < ?",
                              (cut,)).fetchone()["n"])
            if n and not dry_run:
                c.execute("DELETE FROM group_cell WHERE week_start < ?", (cut,))
            out["cell"] = n
        else:
            out["cell"] = 0

        # 孤儿评论：动态被清理后评论还挂着。没有外键，只能定期扫。
        n = int(c.execute(
            "SELECT COUNT(*) n FROM group_comment WHERE act_id NOT IN"
            " (SELECT id FROM group_act)").fetchone()["n"])
        if n and not dry_run:
            c.execute("DELETE FROM group_comment WHERE act_id NOT IN"
                      " (SELECT id FROM group_act)")
        out["orphan_comment"] = n
    return {"ok": True, "dry_run": bool(dry_run), "rows": out}


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
        if head == "archived" and method == "GET":
            return api_archived(me), 200
        if head in ("create", "join", "list", "archived"):
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
        if action == "week" and method == "GET":
            return api_week(me, gid, query), 200
        if action == "bingo" and method == "GET":
            return api_bingo(me, gid, query), 200
        if action == "map" and method == "GET":
            return api_map(me, gid, query), 200
        if action == "mapshare" and method == "POST":
            return api_mapshare(me, gid, body), 200
        if action == "relayshare" and method == "POST":
            return api_relayshare(me, gid, body), 200
        if action == "routes" and method == "GET":
            return api_routes(me, gid, query), 200
        if action == "route" and len(tail) == 1 and method == "POST":
            return api_route_share(me, gid, body), 200
        if action == "route" and len(tail) >= 2 and method == "GET":
            try:
                return api_route_get(me, gid, int(tail[1])), 200
            except ValueError:
                raise GroupError(400, "路线 id 不合法") from None
        if action == "route" and len(tail) >= 3 and tail[2] == "del" and method == "POST":
            try:
                return api_route_del(me, gid, int(tail[1])), 200
            except ValueError:
                raise GroupError(400, "路线 id 不合法") from None
        # 批注：/route/{rid}/note（新建）、/route/{rid}/note/{nid}/del（删除）。
        # 取图走二进制通道，不在这里（这里只吐 JSON）。
        if (action == "route" and len(tail) == 3 and tail[2] == "note"
                and method == "POST"):
            try:
                return api_route_note_add(me, gid, int(tail[1]), body), 200
            except ValueError:
                raise GroupError(400, "路线 id 不合法") from None
        if (action == "route" and len(tail) == 5 and tail[2] == "note"
                and tail[4] == "del" and method == "POST"):
            try:
                return api_route_note_del(me, gid, int(tail[1]),
                                          int(tail[3])), 200
            except ValueError:
                raise GroupError(400, "批注 id 不合法") from None
        if action == "cells" and method == "POST":
            return api_cells(me, gid, body), 200
        if action == "acts" and method == "POST":
            return api_acts(me, gid, body), 200
        if action == "feed" and method == "GET":
            return api_feed(me, gid, query), 200
        if action == "news" and method == "GET":
            return api_news(me, gid, query), 200
        if action == "comment" and method == "POST":
            return api_comment(me, gid, body), 200
        if action == "comment_edit" and method == "POST":
            return api_comment_edit(me, gid, body), 200
        if action == "comment_del" and method == "POST":
            return api_comment_del(me, gid, body), 200
        if action == "goal" and method == "POST":
            return api_goal(me, gid, body), 200
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
        if action == "role" and method == "POST":
            return api_set_role(me, gid, body), 200
        if action == "purge" and method == "POST":
            return api_purge(me, gid, body), 200
        if action == "archive" and method == "POST":
            return api_archive(me, gid, body), 200
        if action == "invite" and len(tail) >= 2 and tail[1] == "rotate" and method == "POST":
            return api_rotate(me, gid), 200
        if action == "camp" and len(tail) >= 2 and tail[1] == "upgrade" and method == "POST":
            return api_camp_upgrade(me, gid), 200
        if action == "camp" and method == "GET":
            with db() as c:
                _require_member(c, gid, int(me["user_id"]))
            return camp_state(gid), 200
        if action == "audit" and method == "GET":
            return api_audit(me, gid, query), 200
        if action == "joinmode" and method == "POST":
            return api_set_join_mode(me, gid, str(body.get("mode") or "open")), 200
        if action == "pending" and method == "GET":
            return api_pending(me, gid), 200
        if action == "decide" and method == "POST":
            try:
                target = int(body.get("user_id") or 0)
            except (TypeError, ValueError):
                raise GroupError(400, "成员 id 不合法")
            return api_decide(me, gid, target, bool(body.get("ok"))), 200
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
