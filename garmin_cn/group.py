# -*- coding: utf-8 -*-
"""训练小组：日汇总构造、上报队列、服务端接口封装。

三条设计约束（来自设计文档 5.3 / 11.3）
--------------------------------------
1. **只传日汇总**。不上传活动明细、不上传轨迹、不上传配速。服务端只看到
   「某天跑了多少米、多久、几次」，看不到你跑的是哪条路。
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
import os
import time
from pathlib import Path

from . import account

# 跑步类才算里程。骑行、力量、球类的里程不进组的「里程」口径——
# 不然一个骑 100 km 的人会把跑团榜刷成笑话。
RUN_TYPES = ("running", "trail_running", "treadmill_running")
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
                               "acts": 0, "ran": False})
        a["acts"] += 1
        a["duration_s"] += float(r["duration_s"] or 0)
        if _is_run(r["type"]):
            a["distance_m"] += float(r["distance_m"] or 0)
            # 爬升只算跑步类：骑车一趟 1000 m 爬升会把这件衫变成自行车榜，
            # 而里程只算跑步，两者口径必须一致。
            a["elevation_m"] += float(r["ascent"] or 0)
            a["ran"] = True

    plan = _plan_flags(s, e, agg)
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
            # 0 = 这天没踩到新路面（跑了老路也算 0），不是「没数据」。
            # 老版本客户端不传这个字段，服务端按 0 处理，两者在徽章上等价。
            "explorer": int(new_cells.get(d) or 0),
            "plan_done": plan.get(d),
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
def report_group(store, gid: int, today: str | None = None,
                 first: bool = False) -> dict:
    """构造一批并尝试立刻发送。失败就留在队列里，等下次前台补。"""
    days = 30 if first else 14
    payload = {"days": build_days(store, today, days)}
    box = Outbox(store)
    box.put(gid, payload)
    return flush_one(store, gid, payload)


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


def set_share(gid: int, share: str) -> dict:
    return _call("POST", f"/v1/group/{int(gid)}/share", {"share": share})


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


def set_goal(gid: int, km: float, predict: int | None = None,
             today: str | None = None) -> dict:
    """报本周目标；顺手可以把预言一起投了。

    两个动作合成一个请求，是因为它们发生在同一个念头里（「这周跑多少 /
    谁会完不成」）。分成两步，第二步永远没人做。
    """
    body: dict = {"km": float(km)}
    if predict:
        body["predict"] = int(predict)
    if today:
        body["today"] = today
    return _call("POST", f"/v1/group/{int(gid)}/goal", body)


def board(gid: int, rng: str = "week", today: str | None = None) -> dict:
    q = f"?range={rng}"
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
