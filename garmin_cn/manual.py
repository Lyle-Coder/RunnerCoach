# -*- coding: utf-8 -*-
"""手动补录：佳明没记到的那些数（目前只有日间小睡）。

为什么要这一层
--------------
小睡只有在手表进入睡眠模式、或机型支持自动侦测且手机同步过之后，才会出现在
dailySleepDTO.napTimeSeconds 里。漏记的时候，睡眠债会被高估——而睡眠债又直接
影响今日处方的量。所以给一个手动的口子：佳明有数据就以佳明为准，没有才用补录，
两边都不做加法，避免同一段小睡被算两遍。

数据落在 data/manual.json：
    {"naps": {"2026-11-03": [{"time": "13:05", "minutes": 30}]}}
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "data" / "manual.json"


def _num(v, default=0.0) -> float:
    """独立的小工具：不从 metrics 导，免得和 metrics 互相 import。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f


def _load() -> dict:
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def add_nap(date: str, minutes: float, clock: str | None = None) -> dict:
    data = _load()
    naps = data.setdefault("naps", {})
    naps.setdefault(date, []).append({
        "time": clock or dt.datetime.now().strftime("%H:%M"),
        "minutes": round(float(minutes)),
    })
    _save(data)
    return nap_of(date)


def nap_of(date: str) -> dict:
    """某天的手动小睡合计（分钟）与条目。"""
    items = ((_load().get("naps") or {}).get(date) or [])
    return {
        "minutes": round(sum(_num(x.get("minutes")) for x in items)),
        "items": items,
    }


def merge(garmin_min, date: str) -> tuple[int, str]:
    """佳明与手动二选一，返回 (分钟, 来源)。

    同一段小睡两边都记时取佳明那笔：权威来源是手表，手动只是补漏。
    """
    g = int(garmin_min or 0)
    m = nap_of(date)["minutes"]
    if g:
        return g, "garmin"
    if m:
        return m, "manual"
    return 0, ""


# ---------------- 主睡眠矫正 ----------------
# 手表整晚没戴（没电 / 掉落 / 忘戴）时，佳明侧整段缺失或明显偏短。
# 这类矫正和小睡补录不同：是「用户亲口说的实际时段」，比佳明的缺失可信，
# 所以一旦给出就**覆盖**同日的佳明记录，而不是二选一。

def add_sleep(date: str, start: str, end: str, note: str = "") -> dict:
    """记录某晚的实际睡眠时段（start/end 都是 HH:MM，跨零点自动处理）。"""
    data = _load()
    fixes = data.setdefault("sleep_fixes", {})
    fixes[date] = {
        "start": start.strip(),
        "end": end.strip(),
        "minutes": _span_min(start, end),
        "note": note,
        "set_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    _save(data)
    return fixes[date]


def _span_min(start: str, end: str) -> int:
    """22:30 → 06:13 这类跨零点时段的分钟数。"""
    def _m(s: str) -> int:
        h, m = s.split(":")[:2]
        return int(h) * 60 + int(m)

    span = _m(end) - _m(start)
    return span + 1440 if span <= 0 else span


def sleep_fix_of(date: str) -> dict | None:
    return (_load().get("sleep_fixes") or {}).get(date) or None


def apply_sleep_fix(row: dict, fix: dict, date: str) -> None:
    """把矫正写进一行 daily 记录（原地改）。

    总时长以时段推算；深睡/浅睡/REM 手表没记到就不编造，留空让看板
    显示「—」；评分同理——佳明没打分的夜里不该冒出一个假分数。

    夜间缺测的连锁清理
    ------------------
    手表掉落后，佳明会用捡回表之后的碎片时间（甚至把静止的表判成浅睡）
    拼出一份「看起来完整」的记录：睡眠评分 48、HRV 59、身体电量 44%。
    这些不是测量值，是缺测的残渣——参与恢复度加权只会把分数拖向错误的
    「需要恢复」。用户没戴表 ≠ 没睡好，所以这里一并作废：评分、分段、
    夜间 HRV、身体电量（含当前值和日内曲线）。只清本次计算的视图，
    库里原始数据不动，戴表整夜后自然恢复。
    """
    row["sleep_total_min"] = fix.get("minutes") or _span_min(fix["start"], fix["end"])
    prev = (dt.date.fromisoformat(date) - dt.timedelta(days=1)).isoformat()
    row["sleep_start"] = f"{prev} {fix['start']}"
    row["sleep_end"] = f"{date} {fix['end']}"
    row["sleep_src"] = "manual"
    row["night_scrubbed"] = True
    for k in ("sleep_score", "deep_min", "light_min", "rem_min", "awake_min",
              "hrv_night", "hrv_status",
              "bb_now", "bb_now_at", "bb_series",
              "bb_high", "bb_low", "bb_charged", "bb_drained"):
        row[k] = None
