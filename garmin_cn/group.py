# -*- coding: utf-8 -*-
"""训练小组：日汇总构造、上报队列、服务端接口封装。

三条设计约束（来自设计文档 5.3 / 11.3）
--------------------------------------
1. **只传日汇总 + 脱敏剪影**。不上传活动明细、原始轨迹点、原始配速和位置。
   除了「某天跑了多少米、多久、几次」，动态卡片另带一份**路线形状**：
   轨迹抽稀成 120 个点、等比缩放到长边 1000 的相对坐标（`_shape()`）。
   它不含经纬度、不含起终点、不含朝向之外的任何元信息，也无法反查是哪条路。
   页面不配底图、不标起终点——看得到怎么跑的，看不出跑在哪条街。
2. **上报是窗口全量，不是增量**。每次发「最近 14 天」（首次加入 30 天），
   服务端按 (组, 人, 日期) 幂等覆盖。这样断网重连、重复提交都不会算两遍，
   也就不需要在客户端维护"发到哪儿了"的游标。
3. **断网先入队**。队列里同组只留最新一批——全量窗口的新批次天然包含旧的，
   留旧批次只会把同一批数字发两遍。退避 1m → 5m → 30m → 2h，8 次之后
   不再退避，等下次进前台就试。
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import time
from pathlib import Path

from . import account

# 跑步类才算里程。骑行、力量、球类的里程不进组的「里程」口径——
# 不然一个骑 100 km 的人会把跑团榜刷成笑话。
RUN_TYPES = ("running", "trail_running", "treadmill_running")

# 轨迹规格。**不抽稀，把所有点都带上**——上游 track 本身已经是降采样过的
# （本地 75 条活动全部 ≤ 420 点），我们再按 120 点切一刀等于二次压缩：
# 同一条 4.7 km 的轨迹，420 点还原出来的路径长度 3.1 km，抽到 120 点后
# 直线化得更厉害，用户一眼就看出「不像我跑的那条路」。
#
# 上限 600 只是防呆（马拉松 3 小时按 10 秒采样约 1000 点），超了才抽稀。
# 归一化边长 1000 给 0.1% 的精度。
SHAPE_MAX_POINTS = 600
SHAPE_SCALE = 1000
SHAPE_MAX_LEN = 6000         # 服务端也按这个上限校验（约 600 点 × 9 字节）
BACKOFF = (60, 300, 1800, 7200)
MAX_TRIES = 8


def _home() -> Path:
    env = os.environ.get("GARMIN_HOME")
    return Path(env).expanduser() if env else Path(__file__).resolve().parent.parent


def _is_run(t) -> bool:
    s = str(t or "").lower()
    return any(k in s for k in RUN_TYPES) or "跑" in s


# ---------------------------------------------------------------- 日汇总
def build_days(store, today: str | None = None, days: int = 14) -> list[dict]:
    """把本地数据聚合成服务端要的日汇总。

    `plan_done` 三态：True 完成 / False 有课没跑 / None 那天没计划。
    休息日返回 None 而不是 False——把休息日算成"未完成"会让完成率凭空变低，
    而用户明明是按计划休息的。
    """
    day = dt.date.fromisoformat(today or dt.date.today().isoformat())
    start = day - dt.timedelta(days=max(1, days) - 1)
    s, e = start.isoformat(), day.isoformat()
    with store.conn:
        rows = store.conn.execute(
            "SELECT date, type, distance_m, duration_s, ascent FROM activities"
            " WHERE date BETWEEN ? AND ?", (s, e)).fetchall()

    agg: dict[str, dict] = {}
    for r in rows:
        d = str(r["date"] or "")[:10]
        if not d:
            continue
        a = agg.setdefault(d, {"distance_m": 0.0, "duration_s": 0.0, "elevation_m": 0.0,
                               "ride_m": 0.0, "acts": 0, "ran": False})
        a["acts"] += 1
        a["duration_s"] += float(r["duration_s"] or 0)
        # 骑行单独记一列：`distance_m` 只装跑步，两者不重叠。
        # 综合榜才会去折算（骑行 4 km = 1 km），跑步榜和衫的口径完全不受影响。
        if _kind(r["type"]) == "ride":
            a["ride_m"] += float(r["distance_m"] or 0)
        if _is_run(r["type"]):
            a["distance_m"] += float(r["distance_m"] or 0)
            # 爬升只算跑步类：骑车一趟 1000 m 爬升会把这件衫变成自行车榜，
            # 而里程只算跑步，两者口径必须一致。
            a["elevation_m"] += float(r["ascent"] or 0)
            a["ran"] = True

    plan = _plan_flags(s, e, agg)
    # 休整点：课表说「今天休息」且当天确实没动。服务端按每周 ≤4 截断，
    # 客户端只负责如实上报这一天算不算。
    rest = _plan_rest(s, e, agg)
    # 探索徽章：先扫轨迹落格（增量，只扫没扫过的），再取窗口内每日新增格数。
    # 只在本机算，上报的只有这个整数——格子 ID 不出手机。
    from . import explore as EX

    try:
        EX.scan(store)
        new_cells = EX.daily_new(store, s, e)
    except Exception:      # noqa: BLE001 - 网格算不出来不能拖垮上报
        new_cells = {}
    dates = sorted(set(agg) | {k for k, v in plan.items() if v is not None})
    out = []
    for d in dates:
        a = agg.get(d) or {}
        out.append({
            "date": d,
            "distance_m": int(round(a.get("distance_m") or 0)),
            "duration_s": int(round(a.get("duration_s") or 0)),
            "acts": int(a.get("acts") or 0),
            "elevation_m": int(round(a.get("elevation_m") or 0)),
            # 骑行里程：只有综合榜用。老服务端会忽略这个字段，不影响其他功能。
            "ride_m": int(round(a.get("ride_m") or 0)),
            # 0 = 这天没踩到新路面（跑了老路也算 0），不是「没数据」。
            # 老版本客户端不传这个字段，服务端按 0 处理，两者在徽章上等价。
            "explorer": int(new_cells.get(d) or 0),
            "plan_done": plan.get(d),
            "vigor": 1 if d in rest else 0,
        })
    return out


def _plan_flags(start: str, end: str, agg: dict) -> dict[str, bool | None]:
    """从 plan.json 读「哪几天有课」，再看那天跑没跑。

    只覆盖当前快照里那一周。历史周没有快照，就返回空——那些天的
    `plan_done` 是 None（不进完成率分母），不伪造成「未完成」。
    """
    p = _home() / "data" / "plan.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, bool | None] = {}
    for w in (data.get("week") or []):
        if not isinstance(w, dict):
            continue
        d = str(w.get("date") or "")[:10]
        if not d or not (start <= d <= end):
            continue
        items = w.get("items") or []
        if not items:
            continue                      # 休息日：不进分母
        out[d] = bool((agg.get(d) or {}).get("ran"))
    return out


def _plan_rest(start: str, end: str, agg: dict) -> set[str]:
    """课表里明确写了休息、当天又确实没动的那些天。

    只认**课表里有这一天但 items 为空**的情况。没有快照的日子不算：
    「不知道那天该不该休息」和「安排了休息」是两件事，后者才配拿休整点。
    """
    p = _home() / "data" / "plan.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:      # noqa: BLE001
        return set()
    out: set[str] = set()
    for w in (data.get("week") or []):
        if not isinstance(w, dict):
            continue
        d = str(w.get("date") or "")[:10]
        if not d or not (start <= d <= end):
            continue
        if w.get("items"):
            continue                      # 有课的日不算休息
        if int((agg.get(d) or {}).get("acts") or 0) > 0:
            continue                      # 安排了休息但去跑了，不给点
        out.add(d)
    return out


# ---------------------------------------------------------------- 队列
class Outbox:
    """上报队列。写本地库，App 重启后还在。"""

    def __init__(self, store):
        self.store = store

    def put(self, gid: int, payload: dict) -> None:
        with self.store.conn:
            self.store.conn.execute(
                "INSERT INTO group_outbox(group_id,payload,tries,next_at) VALUES(?,?,0,0)"
                " ON CONFLICT(group_id) DO UPDATE SET"
                " payload=excluded.payload, tries=0, next_at=0",
                (int(gid), json.dumps(payload, ensure_ascii=False)))

    def pending(self, now: int | None = None) -> list[dict]:
        now = int(now or time.time())
        with self.store.conn:
            rows = self.store.conn.execute(
                "SELECT * FROM group_outbox WHERE next_at<=? ORDER BY group_id", (now,)).fetchall()
        return [dict(r) for r in rows]

    def drop(self, gid: int) -> None:
        with self.store.conn:
            self.store.conn.execute("DELETE FROM group_outbox WHERE group_id=?", (int(gid),))

    def bump(self, gid: int, tries: int) -> int:
        """失败退避。8 次之后不再往后推，让下次进前台立刻再试。"""
        if tries >= MAX_TRIES:
            next_at = 0
        else:
            next_at = int(time.time()) + BACKOFF[min(tries - 1, len(BACKOFF) - 1)]
        with self.store.conn:
            self.store.conn.execute("UPDATE group_outbox SET tries=?, next_at=? WHERE group_id=?",
                                    (tries, next_at, int(gid)))
        return next_at


# ---------------------------------------------------------------- 上报
def _map_on(store, gid: int) -> bool:
    """本机缓存的「路线共享开着吗」。

    缓存的是**这个组**的开关（每个组可以不一样）。组页拉 detail 时会写一次，
    上报时读它决定要不要多发一次格子——不缓存就得每次上报前先拉一遍 detail，
    为了一件可有可无的事多一次往返。
    """
    try:
        return store.get_meta(f"group_map_{int(gid)}") == "1"
    except Exception:      # noqa: BLE001
        return False


def note_map(store, gid: int, on: bool) -> None:
    """组页/设置页读到开关状态后，把它记到本地，供上报时用。"""
    try:
        store.set_meta(f"group_map_{int(gid)}", "1" if on else "0")
    except Exception:      # noqa: BLE001
        pass


def report_group(store, gid: int, today: str | None = None,
                 first: bool = False, client=None) -> dict:
    """构造一批并尝试立刻发送。失败就留在队列里，等下次前台补。"""
    # 一直报 30 天，不再区分首次/增量。
    # 原因：月榜要覆盖整个自然月，而 14 天在月中之后够不到月头；一旦某个
    # 字段是后来才加的（比如 ride_m），只有 30 天窗口才能把当月补齐。
    # 30 行的日汇总只有几 KB，比一次请求本身的开销还小。
    days = 30
    payload = {"days": build_days(store, today, days)}
    box = Outbox(store)
    box.put(gid, payload)
    out = flush_one(store, gid, payload)
    # 路线图与动态都是独立的可选项：失败了不影响里程上报，也不重试——
    # 下次上报会全量重发，天然自愈。
    if out.get("ok"):
        try:
            from . import explore as EX

            if _map_on(store, gid):
                mm = EX.week_cell_meters(store, today)
                if mm:
                    push_cells(gid, list(mm), mm, today)
            acts = build_acts(store, today, client=client)
            if acts:
                push_acts(gid, acts, today)
        except Exception:      # noqa: BLE001
            pass
    return out


def flush_one(store, gid: int, payload: dict) -> dict:
    ok, d = account.request("POST", f"/v1/group/{int(gid)}/report", payload, timeout=20)
    box = Outbox(store)
    if ok and d.get("ok"):
        box.drop(gid)
        with store.conn:
            store.conn.execute("INSERT INTO meta(key,value) VALUES(?,?)"
                               " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               (f"group_seen_{int(gid)}", "1"))
        return {"ok": True, "saved": d.get("saved", 0), "skipped": d.get("skipped", 0)}
    return {"ok": False, "error": d.get("error") or "上报失败", "queued": True}


def flush(store, today: str | None = None) -> dict:
    """把所有到期的队列行发一遍。进前台 / 下拉刷新时调。"""
    box = Outbox(store)
    sent = failed = 0
    for row in box.pending():
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            box.drop(row["group_id"])       # 坏数据不留在队列里反复失败
            continue
        ok, d = account.request("POST", f"/v1/group/{row['group_id']}/report",
                                payload, timeout=20)
        if ok and d.get("ok"):
            box.drop(row["group_id"])
            sent += 1
        else:
            box.bump(row["group_id"], int(row["tries"]) + 1)
            failed += 1
    return {"ok": True, "sent": sent, "failed": failed}


def seen(gid: int, store) -> bool:
    with store.conn:
        r = store.conn.execute("SELECT 1 FROM meta WHERE key=?",
                               (f"group_seen_{int(gid)}",)).fetchone()
    return bool(r)


# ---------------------------------------------------------------- 接口
def _call(method: str, path: str, body=None) -> dict:
    ok, d = account.request(method, path, body)
    if not isinstance(d, dict):
        return {"ok": False, "error": "服务器返回格式不对"}
    d.setdefault("ok", ok)
    return d


def create(name: str, intro: str = "", max_members: int = 50, alias: str = "") -> dict:
    return _call("POST", "/v1/group/create",
                 {"name": name, "intro": intro, "max_members": max_members, "alias": alias})


def join(code: str, alias: str = "") -> dict:
    return _call("POST", "/v1/group/join", {"code": str(code or "").strip().upper(),
                                            "alias": alias})


def groups() -> dict:
    return _call("GET", "/v1/group/list")


def detail(gid: int) -> dict:
    return _call("GET", f"/v1/group/{int(gid)}")


def rename(gid: int, name: str = "", intro: str = "", max_members: int | None = None) -> dict:
    body: dict = {}
    if name:
        body["name"] = name
    if intro:
        body["intro"] = intro
    if max_members is not None:
        body["max_members"] = int(max_members)
    return _call("PATCH", f"/v1/group/{int(gid)}", body)


def leave(gid: int) -> dict:
    return _call("POST", f"/v1/group/{int(gid)}/leave", {})


def kick(gid: int, uid: int) -> dict:
    return _call("POST", f"/v1/group/{int(gid)}/member/{int(uid)}/remove", {})


def rotate(gid: int) -> dict:
    return _call("POST", f"/v1/group/{int(gid)}/invite/rotate", {})


def transfer(gid: int, uid: int) -> dict:
    return _call("POST", f"/v1/group/{int(gid)}/transfer", {"user_id": int(uid)})


def archive(gid: int) -> dict:
    return _call("POST", f"/v1/group/{int(gid)}/archive", {"confirm": "1"})


def camp_upgrade(gid: int, today: str | None = None) -> dict:
    return _call("POST", f"/v1/group/{int(gid)}/camp/upgrade", {})


def audit(gid: int, limit: int = 50) -> dict:
    return _call("GET", f"/v1/group/{int(gid)}/audit?limit={int(limit)}")


def set_join_mode(gid: int, mode: str) -> dict:
    """加入需批准：mode = "approve" / "open"。"""
    return _call("POST", f"/v1/group/{int(gid)}/joinmode", {"mode": mode})


def pending(gid: int) -> dict:
    return _call("GET", f"/v1/group/{int(gid)}/pending")


def decide(gid: int, uid: int, ok: bool) -> dict:
    return _call("POST", f"/v1/group/{int(gid)}/decide",
                 {"user_id": int(uid), "ok": bool(ok)})


def set_share(gid: int, share: str) -> dict:
    return _call("POST", f"/v1/group/{int(gid)}/share", {"share": share})


def set_role(gid: int, uid: int, role: str) -> dict:
    """任命或撤销教练 / 管理员。只有组长能调，服务端会再判一次。"""
    return _call("POST", f"/v1/group/{int(gid)}/role",
                 {"user_id": int(uid), "role": str(role)})


def set_alias(gid: int, alias: str) -> dict:
    """改我在这个组里的昵称（只影响这个组）。"""
    return _call("POST", f"/v1/group/{int(gid)}/me", {"alias": alias})


def home(gid: int, rng: str = "week", today: str | None = None) -> dict:
    """组页首屏四块（detail / board / challenges / season）一次取回。

    一次往返代替四次：实测一次新建 TLS 连接 122 ms，四次就是近半秒。
    老服务端没有这个接口，调用方要能退回逐个请求。
    """
    q = f"?range={rng}"
    if today:
        q += f"&today={today}"
    return _call("GET", f"/v1/group/{int(gid)}/home{q}")


def season(gid: int, today: str | None = None) -> dict:
    """本周的衫、分、名次、称号。"""
    q = f"?today={today}" if today else ""
    return _call("GET", f"/v1/group/{int(gid)}/season{q}")


def week(gid: int, today: str | None = None) -> dict:
    """本周玩法：目标、对子、预言、建议目标。"""
    q = f"?today={today}" if today else ""
    return _call("GET", f"/v1/group/{int(gid)}/week{q}")


def bingo(gid: int, today: str | None = None) -> dict:
    """我的本周 Bingo 卡（25 格 + 连线）。"""
    q = f"?today={today}" if today else ""
    return _call("GET", f"/v1/group/{int(gid)}/bingo{q}")


def set_mapshare(gid: int, on: bool, store=None, today: str | None = None) -> dict:
    """开关「路线共享」。开着才会上传本周格子，关掉服务端会删干净。"""
    r = _call("POST", f"/v1/group/{int(gid)}/mapshare", {"on": bool(on)})
    if r.get("ok") and store is not None:
        note_map(store, gid, bool(r.get("map_share")))
    return r


def push_cells(gid: int, cells: list[str], meters: dict | None = None,
               today: str | None = None) -> dict:
    """把本周跑过的格子发上去（全量覆盖）。

    `meters` 是每格的累计里程：地图用它决定「这块主要是谁跑的」和颜色深浅。
    不带也能发（服务端存 0），只是地图会退化成「跑过/没跑过」。
    """
    body: dict = {"cells": list(cells)}
    if meters:
        body["meters"] = {str(k): int(round(float(v))) for k, v in meters.items()}
    if today:
        body["today"] = today
    return _call("POST", f"/v1/group/{int(gid)}/cells", body)


def map_data(gid: int, today: str | None = None) -> dict:
    """组内路线：每人的颜色 + 本周格子。"""
    q = f"?today={today}" if today else ""
    return _call("GET", f"/v1/group/{int(gid)}/map{q}")


def note_add(gid: int, rid: int, x: float, y: float,
             text: str = "", image_b64: str = "") -> dict:
    """给路线加一条批注。位置是 0..1 的相对坐标（钉在渲染出的那张图上）。

    只有路线作者能加，服务端会再判一次。
    """
    return _call("POST", f"/v1/group/{int(gid)}/route/{int(rid)}/note",
                 {"x": float(x), "y": float(y),
                  "text": str(text or ""), "image": str(image_b64 or "")})


def note_del(gid: int, rid: int, nid: int) -> dict:
    return _call("POST",
                 f"/v1/group/{int(gid)}/route/{int(rid)}/note/{int(nid)}/del", {})


def note_image(gid: int, rid: int, nid: int) -> tuple:
    """批注照片的字节。返回 `(ok, data, ctype)`；拿不到就是 `(False, b"", "")`。"""
    from . import account as AC

    return AC.request_raw(
        f"/v1/group/{int(gid)}/route/{int(rid)}/note/{int(nid)}/img")


def archived() -> dict:
    """我作为组长、已经解散的组。App 的组列表看不到它们。"""
    return _call("GET", "/v1/group/archived")


def purge(gid: int, confirm: str) -> dict:
    """彻底删掉一个已解散的组。`confirm` 必须是组名，服务端逐字比对。"""
    return _call("POST", f"/v1/group/{int(gid)}/purge",
                 {"confirm": str(confirm or "")})


def share_route(gid: int, act_id: str, title: str, distance_m: int,
                date: str, points: list) -> dict:
    """把一条活动的完整轨迹发给同组。

    这是唯一一条上传真实经纬度的通路（`push_cells` 只传网格编号）。
    由用户在页面上勾了「知道会包含位置」才会调到这里。
    """
    return _call("POST", f"/v1/group/{int(gid)}/route",
                 {"act_id": act_id, "title": title, "distance_m": int(distance_m),
                  "date": date, "points": points})


def unshare_route(gid: int, rid: int) -> dict:
    """撤回一条路线。删了就没了。"""
    return _call("POST", f"/v1/group/{int(gid)}/route/{int(rid)}/del", {})


def routes(gid: int) -> dict:
    """组内已分享的路线清单（不含轨迹点）。"""
    return _call("GET", f"/v1/group/{int(gid)}/routes")


def route(gid: int, rid: int) -> dict:
    """单条路线 + 轨迹点。"""
    return _call("GET", f"/v1/group/{int(gid)}/route/{int(rid)}")


def news(gid: int, since: int = 0) -> dict:
    """自 `since` 以来跟我有关的新东西（别人评论我的动态、新赛事）。

    只给后台通知用，不带正文和剪影。`since=0` 时服务端只回当前时间戳，
    用来给新设备取一个起点，不会补一屏历史。
    """
    q = f"?since={int(since)}" if since else ""
    return _call("GET", f"/v1/group/{int(gid)}/news{q}")


# ---------------------------------------------------------------- 动态
# 只传概要。活动名称、轨迹、配速、心率一概不上——这一栏是「今天谁出门了」，
# 不是活动详情页，组页也就不能出图。
RIDE_TYPES = ("cycling", "road_biking", "mountain_biking", "骑")
WALK_TYPES = ("hiking", "walking", "徒步", "步")


def _kind(t) -> str:
    s = str(t or "").lower()
    if _is_run(s):
        return "run"
    if any(k in s for k in RIDE_TYPES) or "骑" in s:
        return "ride"
    if any(k in s for k in WALK_TYPES):
        return "walk"
    return "other"


def _shape(track) -> str:
    """把轨迹压成**脱敏剪影**：`"x,y;x,y;..."`，x/y 是 0..100 的相对坐标。

    三步：
    1. 丢掉非法点（0,0 是佳明没定位时的占位值，留着会把形状压成一条线）；
    2. **原样保留所有点**（上游 track 已经降采样过，不再二次压缩）；
       只有超过 SHAPE_MAX_POINTS 的超长轨迹才按累计长度均匀抽稀；
    3. 等比缩放到长边 = SHAPE_SCALE（1000），短边按比例（**不能各自拉伸**，
       那会把圆形跑道画成正方形）。

    出去的东西里没有任何经纬度、起终点坐标或时间——只有形状。
    """
    pts: list[tuple[float, float]] = []
    for p in (track or []):
        try:
            lat, lon = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        if abs(lat) < 1e-6 and abs(lon) < 1e-6:
            continue
        pts.append((lat, lon))
    if len(pts) < 4:
        return ""

    # 累计长度（米）。经度按纬度压缩，否则高纬度会把东西方向拉长。
    coslat = math.cos(math.radians(pts[0][0])) or 1.0
    acc = [0.0]
    for i in range(1, len(pts)):
        dy = (pts[i][0] - pts[i - 1][0]) * 111320.0
        dx = (pts[i][1] - pts[i - 1][1]) * 111320.0 * coslat
        acc.append(acc[-1] + math.hypot(dx, dy))
    total = acc[-1]
    if total <= 1.0:          # 全程不到 1 米，等于没动
        return ""

    if len(pts) <= SHAPE_MAX_POINTS:
        # 不抽稀：上游给多少点就用多少点，形状原样保留
        picked = pts
    else:
        # 只有超长轨迹才按累计长度均匀抽稀（不是按索引——慢速段点密，
        # 按索引抽会把圈画歪）
        n = SHAPE_MAX_POINTS
        picked = []
        j = 0
        for k in range(n):
            target = total * k / (n - 1)
            while j < len(acc) - 1 and acc[j] < target:
                j += 1
            picked.append(pts[j])

    ys = [(p[0] - pts[0][0]) * 111320.0 for p in picked]
    xs = [(p[1] - pts[0][1]) * 111320.0 * coslat for p in picked]
    # 原点挪回最小角，避免负号
    xs = [v - min(xs) for v in xs]
    ys = [v - min(ys) for v in ys]
    # **y 轴要翻过来**：纬度越大越靠北，而 SVG 的 y 是向下增大的。
    # 少了这一步，整条轨迹是南北颠倒的——往北跑会画成往南跑。
    # （本地活动图的 projector 一直是对的，它用的是 maxy - lat；这里漏了。）
    ymax = max(ys)
    ys = [ymax - v for v in ys]
    span = max(max(xs), max(ys))
    if span <= 0:
        return ""
    return ";".join(f"{round(x / span * SHAPE_SCALE):d},{round(y / span * SHAPE_SCALE):d}"
                   for x, y in zip(xs, ys))


# 一次上报最多补抓几条活动明细。明细是**按需懒抓**的（打开详情才抓），
# 所以骑行、走路这类不常点开的活动经常没有轨迹。补抓要发网络请求，一条
# 0.5–1 秒，太多了「立即上报」会等很久；8 条约 6 秒，剩下的下次再补。
ACT_FETCH_MAX = 8


def build_acts(store, today: str | None = None, days: int = 14,
               client=None) -> list[dict]:
    """把最近的活动压成概要。

    `sid` 是活动 id 的短哈希：服务端靠它去重，但它反查不出是哪条活动，
    也不含任何位置信息。不上传原始 activity_id，是因为那个 id 在佳明那边
    能直接打开活动页。
    """
    import hashlib

    day = dt.date.fromisoformat(today or dt.date.today().isoformat())
    start = day - dt.timedelta(days=max(1, days) - 1)
    with store.conn:
        rows = store.conn.execute(
            "SELECT activity_id, date, type, distance_m, duration_s, ascent"
            " FROM activities WHERE date BETWEEN ? AND ? ORDER BY date",
            (start.isoformat(), day.isoformat())).fetchall()
        # 剪影要读活动明细里的 track。一次 IN 查询取回，别在循环里一条条查。
        shapes: dict[str, str] = {}
        ids = [str(r["activity_id"]) for r in rows if r["activity_id"]]
        if ids:
            marks = ",".join("?" * len(ids))
            for d in store.conn.execute(
                    f"SELECT activity_id, payload FROM activity_detail"
                    f" WHERE activity_id IN ({marks})", ids):
                try:
                    t = json.loads(d["payload"]).get("track") or []
                except (ValueError, TypeError, AttributeError):
                    t = []
                shapes[str(d["activity_id"])] = _shape(t)

    # 没有明细的活动（骑行、走路这些不常点开详情的）补抓一次，否则动态里
    # 它们永远是「无轨迹」。只在调用方给了 client 时做——离线也应该能上报里程。
    if client is not None:
        need = [str(r["activity_id"]) for r in rows
                if r["activity_id"] and str(r["activity_id"]) not in shapes
                and float(r["distance_m"] or 0) > 0]
        if need:
            from . import activity as act

            for aid in need[:ACT_FETCH_MAX]:
                try:
                    got = act.ensure(store, client, aid) or {}
                except Exception:      # noqa: BLE001 - 抓不到就留空，别拖垮上报
                    shapes[aid] = ""
                    continue
                shapes[aid] = _shape(got.get("track") or [])
    out = []
    for r in rows:
        aid = str(r["activity_id"] or "")
        d = str(r["date"] or "")[:10]
        if not aid or not d:
            continue
        out.append({
            "sid": hashlib.sha1(aid.encode("utf-8")).hexdigest()[:16],
            "date": d,
            "kind": _kind(r["type"]),
            "distance_m": int(round(float(r["distance_m"] or 0))),
            "duration_s": int(round(float(r["duration_s"] or 0))),
            "elevation_m": int(round(float(r["ascent"] or 0))),
            # 空串 = 没有 GPS（室内/器械）或还没同步到明细，服务端照样收
            "shape": shapes.get(aid, "")[:SHAPE_MAX_LEN],
        })
    return out


def push_acts(gid: int, acts: list[dict], today: str | None = None) -> dict:
    body: dict = {"acts": list(acts)}
    if today:
        body["today"] = today
    return _call("POST", f"/v1/group/{int(gid)}/acts", body)


def feed(gid: int, today: str | None = None, act: int | None = None) -> dict:
    q = "?"
    if today:
        q += f"today={today}&"
    if act:
        q += f"act={int(act)}"
    return _call("GET", f"/v1/group/{int(gid)}/feed{q.rstrip('&')}")


def comment(gid: int, act: int, text: str = "", emoji: str = "") -> dict:
    return _call("POST", f"/v1/group/{int(gid)}/comment",
                 {"act": int(act), "text": text, "emoji": emoji})


def comment_edit(gid: int, cid: int, text: str = "", emoji: str = "") -> dict:
    """改自己的评论。服务端只认本人的，别人的会回 404。"""
    return _call("POST", f"/v1/group/{int(gid)}/comment_edit",
                 {"id": int(cid), "text": text, "emoji": emoji})


def comment_del(gid: int, cid: int) -> dict:
    return _call("POST", f"/v1/group/{int(gid)}/comment_del", {"id": int(cid)})


def set_goal(gid: int, km: float | None = None, predict: int | None = None,
             today: str | None = None, touch_predict: bool = False) -> dict:
    """报本周目标；顺手可以把预言一起投了。

    两个动作合成一个请求，是因为它们发生在同一个念头里（「这周跑多少 /
    谁会完不成」）。分成两步，第二步永远没人做。

    `km=None` = 只改预言（服务端允许 km 空着）。`touch_predict=True` 时
    即使 `predict` 是 None 也会把键发过去——服务端把「空值」读成「撤掉
    这周的预言」，这是唯一能取消投票的方式。
    """
    body: dict = {}
    if km is not None:
        body["km"] = float(km)
    if predict or touch_predict:
        body["predict"] = int(predict) if predict else ""
    if today:
        body["today"] = today
    return _call("POST", f"/v1/group/{int(gid)}/goal", body)


def board(gid: int, rng: str = "week", today: str | None = None,
          by: str = "dist") -> dict:
    """榜单。`by=comp` 是月榜的综合榜（骑行按 4:1 折算）。"""
    q = f"?range={rng}"
    if by and by != "dist":
        q += f"&by={by}"
    if today:
        q += f"&today={today}"
    return _call("GET", f"/v1/group/{int(gid)}/board{q}")


def challenge_create(gid: int, title: str, kind: str, target: int,
                     start_date: str, end_date: str, today: str | None = None) -> dict:
    return _call("POST", f"/v1/group/{int(gid)}/challenge/create",
                 {"title": title, "kind": kind, "target": int(target),
                  "start_date": start_date, "end_date": end_date,
                  "today": today or dt.date.today().isoformat()})


def challenge_list(gid: int, today: str | None = None) -> dict:
    q = f"?today={today}" if today else ""
    return _call("GET", f"/v1/group/{int(gid)}/challenge/list{q}")


def challenge_get(gid: int, cid: int, today: str | None = None) -> dict:
    q = f"?today={today}" if today else ""
    return _call("GET", f"/v1/group/{int(gid)}/challenge/{int(cid)}{q}")


def challenge_close(gid: int, cid: int) -> dict:
    return _call("POST", f"/v1/group/{int(gid)}/challenge/{int(cid)}/close", {})


# ---------------------------------------------------------------- 远征
# 接口清单见 docs/用户组设计.md 11.9。`today` 必须带：服务器不知道用户在哪个
# 时区，「第几幕」按用户本地日期算，不带就会差一天。
def run_state(gid: int, today: str | None = None) -> dict:
    d = today or dt.date.today().isoformat()
    return _call("GET", f"/v1/group/{int(gid)}/run/state?today={d}")


def run_fork(gid: int, option: str, today: str | None = None) -> dict:
    d = today or dt.date.today().isoformat()
    return _call("POST", f"/v1/group/{int(gid)}/run/fork",
                 {"option": option, "today": d})


def run_event(gid: int, slot: int, option: str, today: str | None = None) -> dict:
    d = today or dt.date.today().isoformat()
    return _call("POST", f"/v1/group/{int(gid)}/run/event",
                 {"slot": int(slot), "option": option, "today": d})


def run_trial(gid: int, trial_id: str, done: bool,
              activity_id: str = "", today: str | None = None,
              evidence: dict | None = None) -> dict:
    """报一条试炼。

    `evidence` 是**原始分圈**（`trial_evidence()` 抽出来的），不是「我达标了」。
    交结论等于没交——服务端必须能自己重算，否则这个机制只是装饰。
    """
    d = today or dt.date.today().isoformat()
    body = {"trial_id": trial_id, "done": bool(done),
            "activity_id": activity_id, "today": d}
    if evidence:
        body["evidence"] = evidence
    return _call("POST", f"/v1/group/{int(gid)}/run/trial", body)


# ---------------------------------------------------------------- 试炼证据
EVIDENCE_MAX = 3          # 最多带 3 条候选：一条不达标不代表这周没跑成
EVIDENCE_MIN_LAP = 200    # 与 server/expedition.py 的 MIN_LAP_M 对齐


def trial_evidence(store, quality: dict, start: str, end: str) -> dict:
    """从本周的活动里抽出「每公里一圈」的原始数据，供服务端复算。

    三条取舍：

    * **只抽原始数字**（距离、用时、步频、心率、爬升），不抽判定。判定一定
      在服务端做，否则客户端改一行就能过。
    * **窗口内取距离最长的几条**。跑 10 km 去应 5 km 的试炼时，服务端只截
      前 5 km，所以长距离跑不会因为「后面那段正好匀」白捡。
    * **判不了就返回空**，让服务端落到 unverified（只算一半）。宁可少给，
      也不假装核验过。
    """
    need = float((quality or {}).get("distance_km") or 0)
    floor = need * 0.85 * 1000          # 差太多的连候选都不当
    try:
        with store.conn:
            rows = store.conn.execute(
                "SELECT activity_id, date, type, distance_m FROM activities"
                " WHERE date BETWEEN ? AND ? AND distance_m >= ?"
                " ORDER BY distance_m DESC", (start, end, floor)).fetchall()
    except Exception:                    # noqa: BLE001 - 老库没有这张表也不能崩
        return {}
    out = []
    for r in rows:
        if not _is_run(r["type"]):
            continue
        try:
            detail = store.get_detail(r["activity_id"])
        except Exception:                # noqa: BLE001
            detail = None
        laps = []
        for l in ((detail or {}).get("laps") or []):
            d, t = l.get("dist_m") or 0, l.get("dur_s") or 0
            if d < EVIDENCE_MIN_LAP or t <= 0:
                continue
            laps.append({"dist_m": round(float(d)),
                         "dur_s": round(float(t), 1),
                         "cad": l.get("cad"), "hr": l.get("hr_avg"),
                         "ele": l.get("ele_gain")})
        if len(laps) < 3:
            continue
        out.append({"activity_id": str(r["activity_id"]),
                    "date": str(r["date"])[:10],
                    "distance_km": round(float(r["distance_m"] or 0) / 1000, 2),
                    "laps": laps})
        if len(out) >= EVIDENCE_MAX:
            break
    return {"activities": out} if out else {}


def run_rescue(gid: int, user_id: int, today: str | None = None) -> dict:
    d = today or dt.date.today().isoformat()
    return _call("POST", f"/v1/group/{int(gid)}/run/rescue",
                 {"user_id": int(user_id), "today": d})


def run_target(gid: int, week_target_km: float, today: str | None = None) -> dict:
    d = today or dt.date.today().isoformat()
    return _call("POST", f"/v1/group/{int(gid)}/run/target",
                 {"week_target_km": float(week_target_km), "today": d})


def run_outcome(gid: int, today: str | None = None) -> dict:
    d = today or dt.date.today().isoformat()
    return _call("GET", f"/v1/group/{int(gid)}/run/outcome?today={d}")
