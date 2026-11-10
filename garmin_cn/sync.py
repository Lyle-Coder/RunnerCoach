"""同步：把 /gc-api/ 原始返回解析成扁平日记录并落库。"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from .client import GarminCNClient, SessionExpired
from .metrics import daterange, num
from .store import Store


def _g(d: Any, *keys, default=None):
    """安全取值，支持 a.b.c 形式的多层 key。"""
    cur = d
    for k in keys:
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            return default
        if cur is None:
            return default
    return cur


def _ts_local(ms) -> str | None:
    v = num(ms)
    if not v:
        return None
    try:
        return dt.datetime.fromtimestamp(v / 1000).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return None


def parse_sleep(payload: dict | None) -> dict:
    dto = _g(payload, "dailySleepDTO") or payload or {}
    scores = _g(dto, "sleepScores", "overall", "value")
    total = num(_g(dto, "sleepTimeSeconds"))
    # 小睡：napTimeSeconds 是当天全部小睡之和；dailyNapDTOS 是每一段的明细
    # （起止、napFeedback 时机评价、napSource 自动/手动）。注意它**不包含**在
    # sleepTimeSeconds 里，日间小睡要单独累加才反映真实睡眠总量。
    nap_sec = num(_g(dto, "napTimeSeconds"))
    naps = _g(dto, "dailyNapDTOS") or []
    # 佳明的动态睡眠需求（分钟）：基线 480，按训练负荷/睡眠史/HRV/小睡逐日调整
    # （actual 420~540 都出现过）。比固定 8 小时更能反映「今晚该睡多少」。
    need = num(_g(dto, "sleepNeed", "actual"))
    return {
        "sleep_total_min": round(total / 60) if total else None,
        "sleep_score": round(num(scores)) if scores is not None else None,
        "deep_min": round(num(_g(dto, "deepSleepSeconds"), 0) / 60) or None,
        "light_min": round(num(_g(dto, "lightSleepSeconds"), 0) / 60) or None,
        "rem_min": round(num(_g(dto, "remSleepSeconds"), 0) / 60) or None,
        "awake_min": round(num(_g(dto, "awakeSleepSeconds"), 0) / 60) or None,
        "nap_min": round(nap_sec / 60) if nap_sec else None,
        "naps": len(naps) or None,
        "sleep_need_min": round(need) if need else None,
        "sleep_start": _ts_local(_g(dto, "sleepStartTimestampLocal")),
        "sleep_end": _ts_local(_g(dto, "sleepEndTimestampLocal")),
    }


def parse_hrv(payload: dict | None) -> dict:
    s = _g(payload, "hrvSummary") or payload or {}
    return {
        "hrv_night": num(_g(s, "lastNightAvg")),
        "hrv_weekly": num(_g(s, "weeklyAvg")),
        "hrv_status": _g(s, "status"),
    }


def parse_bb(payload: Any, tz_hours: int = 8) -> dict:
    """身体电量：从数值序列里取最高/最低/当前值，另外读 charged/drained。

    `bodyBatteryValuesArray` 是 [[毫秒时间戳, 电量], ...]，按时间升序。
    序列最后一点就是「此刻还剩多少」——这是看板最该显示的那个数，
    daily summary 里只有全天最高/最低，看不出现在还剩多少。
    """
    # 日接口有时把一天的记录包成 [ {...} ]（reports/daily 那种口径），先拆开
    if isinstance(payload, list):
        payload = next((x for x in payload if isinstance(x, dict)), None)
    if not isinstance(payload, dict):
        return {}
    vals, pts = [], []
    arr = payload.get("bodyBatteryValuesArray") or []
    for item in arr:
        if isinstance(item, list) and len(item) >= 2:
            v = num(item[1])
            ts = num(item[0])
            if v is not None:
                vals.append(v)
                if ts:
                    pts.append([int(ts), int(v)])
        elif isinstance(item, dict):
            v = num(item.get("value") or item.get("bodyBatteryLevel"))
            ts = num(item.get("timestamp") or item.get("startTimestampGMT"))
            if v is not None:
                vals.append(v)
                if ts:
                    pts.append([int(ts), int(v)])
    if not pts and vals:
        # 没有时间戳就只留一条，别让下游以为有曲线
        pts = []
    now = vals[-1] if vals else num(payload.get("bodyBatteryMostRecentValue"))
    now_at = ""
    if pts:
        now_at = dt.datetime.fromtimestamp(pts[-1][0] / 1000,
                                           dt.timezone(dt.timedelta(hours=tz_hours))
                                           ).strftime("%H:%M")
    out = {
        "bb_high": round(max(vals)) if vals else num(payload.get("bodyBatteryHighestValue")),
        "bb_low": round(min(vals)) if vals else num(payload.get("bodyBatteryLowestValue")),
        "bb_charged": num(payload.get("charged") or payload.get("bodyBatteryChargedValue")),
        "bb_drained": num(payload.get("drained") or payload.get("bodyBatteryDrainedValue")),
        "bb_now": round(now) if now is not None else None,
        "bb_now_at": now_at,
        "bb_series": json.dumps(pts, separators=(",", ":")) if pts else None,
    }
    return out


def parse_daily(payload: dict | None, date: str) -> dict:
    p = payload or {}
    return {
        "date": date,
        "steps": num(_g(p, "totalSteps")),
        "step_goal": num(_g(p, "dailyStepGoal")),
        "distance_m": num(_g(p, "totalDistanceMeters")),
        "calories_total": num(_g(p, "totalKilocalories")),
        "calories_active": num(_g(p, "activeKilocalories")),
        "floors": num(_g(p, "floorsAscended")),
        "resting_hr": num(_g(p, "restingHeartRate")),
        "min_hr": num(_g(p, "minHeartRate")),
        "max_hr": num(_g(p, "maxHeartRate")),
        "avg_stress": num(_g(p, "averageStressLevel")),
        "max_stress": num(_g(p, "maxStressLevel")),
        "bb_high": num(_g(p, "bodyBatteryHighestValue")),
        "bb_low": num(_g(p, "bodyBatteryLowestValue")),
        "bb_charged": num(_g(p, "bodyBatteryChargedValue")),
        "bb_drained": num(_g(p, "bodyBatteryDrainedValue")),
        "spo2_avg": num(_g(p, "averageSpo2") or _g(p, "latestSpo2")),
        "resp_avg": num(_g(p, "avgWakingRespirationValue")),
        "intensity_mod": num(_g(p, "moderateIntensityMinutes")),
        "intensity_vig": num(_g(p, "vigorousIntensityMinutes")),
        "raw": json.dumps(p, ensure_ascii=False)[:200000] if p else None,
    }


def parse_activity(a: dict) -> dict:
    dist = num(a.get("distance"))
    dur = num(a.get("duration"))
    pace = None
    if dist and dur and dist > 0:
        pace = round(dur / (dist / 1000)) if dist > 1000 else None
    return {
        "activity_id": a.get("activityId"),
        "date": (a.get("startTimeLocal") or "")[:10] or None,
        "start_ts": a.get("startTimeLocal"),
        "type": _g(a, "activityType", "typeKey") or a.get("activityType"),
        "name": a.get("activityName"),
        "duration_s": dur,
        "distance_m": dist,
        "avg_hr": num(a.get("averageHR")),
        "max_hr": num(a.get("maxHR")),
        "calories": num(a.get("calories")),
        "load": num(a.get("activityTrainingLoad")),
        "aerobic_te": num(a.get("aerobicTrainingEffect")),
        "anaerobic_te": num(a.get("anaerobicTrainingEffect")),
        "pace_s_km": pace,
        "ascent": num(a.get("elevationGain")),
        "source": "garmin",
        "raw": json.dumps(a, ensure_ascii=False)[:100000],
    }


class SyncService:
    def __init__(self, client: GarminCNClient, store: Store):
        self.client = client
        self.store = store
        self.errors: list[str] = []
        # 活动列表这一趟顺手记下的两件事：见过的最早活动、以及列表是否
        # 已经翻到底。补历史靠它判断「账号从哪天开始有记录」，不用额外请求。
        self.earliest_activity: str | None = None
        self.activities_exhausted = False

    def sync_day(self, date: str, extras: bool = False,
                 hrv: dict | None = None) -> dict:
        """拉一天。`hrv` 可传入区间接口预取的当日 HRV，省掉每段一次请求。"""
        row = {"date": date, "synced_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}
        bb_row: dict | None = None

        try:
            daily = self.client.daily_summary(date)
        except SessionExpired:
            raise
        if daily:
            row.update(parse_daily(daily, date))

        try:
            sleep = self.client.sleep(date)
            if sleep:
                row.update(parse_sleep(sleep))
            if hrv is None:
                hrv = self.client.hrv(date)
            if hrv:
                row.update(parse_hrv(hrv))
            if extras:
                stress = self.client.stress(date)
                if isinstance(stress, dict):
                    row["avg_stress"] = row.get("avg_stress") or num(stress.get("avgStressLevel"))
                    row["max_stress"] = row.get("max_stress") or num(stress.get("maxStressLevel"))
                bb = parse_bb(self.client.body_battery_day(date))
                bb_row = bb
                if bb.get("bb_high") and not row.get("bb_high"):
                    row.update({k: v for k, v in bb.items() if v is not None})
        except SessionExpired:
            raise
        except Exception as e:
            self.errors.append(f"{date}: {type(e).__name__} {e}")

        self.store.upsert_daily(row)
        # bb_now / bb_series 不在 DAILY_COLS 里（upsert 会忽略），单独回写一次：
        # 否则「此刻电量」永远只能看 daily summary 里那个早上的数。
        if bb_row:
            from . import live
            live.save_bb(self.store, date, bb_row)
        return row

    def sync_activities(self, limit: int = 60, max_pages: int = 1,
                        oldest: str | None = None) -> int:
        """分页拉活动列表。默认只取第一页（最近 60 条），与旧行为一致。

        补历史时必须翻页：佳明活动列表按时间倒序，`start` 是偏移量，
        一次 `sync(days=365)` 若还只取 60 条，日汇总补齐了、活动却仍是
        最近两个月。翻到「本页最早活动早于区间起点」或不足一页就停，
        免得为了补一年把账号里十年的活动全拉一遍。
        """
        total = 0
        pages = max(1, min(20, int(max_pages or 1)))
        for page in range(pages):
            try:
                items = self.client.activities(limit=limit, start=page * limit)
            except SessionExpired:
                raise
            except Exception as e:
                self.errors.append(f"activities: {type(e).__name__} {e}")
                return total
            if not items:
                self.activities_exhausted = True
                return total
            reached = False
            n = 0
            for a in items:
                if not isinstance(a, dict) or not a.get("activityId"):
                    continue
                day = (a.get("startTimeLocal") or "")[:10]
                if day and (self.earliest_activity is None
                            or day < self.earliest_activity):
                    self.earliest_activity = day
                if oldest and day and day < oldest:
                    reached = True
                self.store.upsert_activity(parse_activity(a))
                n += 1
                total += 1
            # 不满一页 = 列表到底了，这时 earliest_activity 才是真的第一条
            if n == 0 or len(items) < limit:
                self.activities_exhausted = True
                return total
            if reached:
                return total
        return total

    def sync(self, days: int = 30, end: str | None = None, extras: bool = False,
             progress=None, force: bool = False, activity_pages: int = 1) -> dict:
        """同步最近 N 天（或截止到 `end` 往前 N 天）。

        增量是默认行为：**已经稳定的一天不会再拉一遍**。判定标准是
        「库里已有这天的数据，且上次同步发生在这天之后」——过去的日汇总、
        睡眠、HRV 不会变，重复拉只浪费时间。只有今天和数据不全的日子会重拉。
        首装/补历史用 `force=True` 全量重拉。
        """
        end = end or dt.date.today().isoformat()
        dates = daterange(end, days)
        today = dt.date.today().isoformat()

        if force:
            todo = list(dates)
        else:
            todo = [d for d in dates if d >= today or not self._settled(d)]
        skipped = len(dates) - len(todo)

        # HRV 有区间接口：30 段逐日拉 = 30 次请求，一次区间 = 1 次。
        # 先把整段拿下来，逐日回填时直接用；接口口径不符就回落到逐日。
        hrv_map: dict[str, dict] = {}
        if len(todo) >= 4:
            try:
                hrv_map = self._hrv_range(todo[0], todo[-1])
            except Exception as e:
                self.errors.append(f"hrv range: {type(e).__name__} {e}")

        ok = 0
        for i, d in enumerate(todo, 1):
            # 区间接口拿到过数据就信它：区间里没有的日期 = 那天真的没测到，
            # 不必再逐日补一次（只会拿到空结果，白花一次请求）。
            # 区间整体失败（空 dict）才回落到逐日。
            self.sync_day(d, extras=extras,
                          hrv=hrv_map.get(d, {}) if hrv_map else None)
            ok += 1
            if progress:
                progress(i, len(todo), d)
        acts = self.sync_activities(max_pages=activity_pages, oldest=dates[0])
        plan = self.sync_plan(end)
        self.store.set_meta("last_sync", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self.store.set_meta("last_sync_end", end)
        return {
            "range": f"{dates[0]} ~ {dates[-1]}",
            "days": len(dates),          # 区间天数（界面口径不变）
            "fetched": ok,               # 本次真正拉取的天数
            "skipped": skipped,          # 命中的已有缓存
            "activities": acts,
            "earliest_activity": self.earliest_activity,
            "list_exhausted": self.activities_exhausted,
            "plan": plan,
            "requests": getattr(self.client.net, "count", 0),
            "errors": self.errors[:10],
        }

    def _settled(self, date: str) -> bool:
        """这一天还会不会变：库里要有数据，且上次同步是在这天结束之后做的。"""
        try:
            row = self.store.day(date)
        except Exception:
            return False
        if not row:
            return False
        synced = (row.get("synced_at") or "")[:10]
        if not synced or synced <= date:      # 当天同步的不算数：数据还没长齐
            return False
        return any(row.get(k) is not None
                   for k in ("steps", "sleep_total_min", "resting_hr"))

    def _hrv_range(self, start: str, end: str) -> dict[str, dict]:
        """一次请求取整段 HRV，返回 {日期: 当日 HRV 对象}。

        国区返回口径有两种（`{"hrvSummaries": [...]}` 或直接列表），
        认不出结构就返回空 dict，调用方自动回落逐日拉，不影响正确性。
        """
        data = self.client.hrv_range(start, end)
        items = None
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    items = v
                    break
        elif isinstance(data, list):
            items = data
        out: dict[str, dict] = {}
        for it in items or []:
            if not isinstance(it, dict):
                continue
            day = str(it.get("calendarDate") or it.get("date") or "")[:10]
            if day:
                out[day] = it
        return out

    def sync_plan(self, today: str | None = None) -> dict:
        """刷新佳明教练计划快照（2 次请求，失败不影响主流程）。"""
        from .plan import fetch

        try:
            return fetch(self.client, today, out_path=self.store.path.parent / "plan.json")
        except Exception as e:
            self.errors.append(f"plan: {type(e).__name__} {e}")
            return {"available": False, "reason": str(e)}
