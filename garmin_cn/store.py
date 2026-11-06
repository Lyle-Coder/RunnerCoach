"""SQLite 存储：日汇总、活动、教练点评。"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

DAILY_COLS = [
    "date", "steps", "step_goal", "distance_m", "calories_total", "calories_active",
    "floors", "resting_hr", "min_hr", "max_hr", "avg_stress", "max_stress",
    "bb_high", "bb_low", "bb_charged", "bb_drained",
    "spo2_avg", "resp_avg", "intensity_mod", "intensity_vig",
    "sleep_score", "sleep_total_min", "deep_min", "light_min", "rem_min", "awake_min",
    "nap_min", "naps", "sleep_need_min",
    "sleep_start", "sleep_end", "hrv_night", "hrv_weekly", "hrv_status",
    "fitness_age", "vo2max", "synced_at", "raw",
]

ACT_COLS = [
    "activity_id", "date", "start_ts", "type", "name", "duration_s", "distance_m",
    "avg_hr", "max_hr", "calories", "load", "aerobic_te", "anaerobic_te",
    "pace_s_km", "ascent", "raw",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily (
    date TEXT PRIMARY KEY,
    steps INTEGER, step_goal INTEGER, distance_m REAL,
    calories_total INTEGER, calories_active INTEGER, floors INTEGER,
    resting_hr INTEGER, min_hr INTEGER, max_hr INTEGER,
    avg_stress INTEGER, max_stress INTEGER,
    bb_high INTEGER, bb_low INTEGER, bb_charged INTEGER, bb_drained INTEGER,
    spo2_avg REAL, resp_avg REAL,
    intensity_mod INTEGER, intensity_vig INTEGER,
    sleep_score INTEGER, sleep_total_min INTEGER, deep_min INTEGER,
    light_min INTEGER, rem_min INTEGER, awake_min INTEGER,
    nap_min INTEGER, naps INTEGER, sleep_need_min INTEGER,
    sleep_start TEXT, sleep_end TEXT,
    hrv_night REAL, hrv_weekly REAL, hrv_status TEXT,
    fitness_age INTEGER, vo2max REAL,
    weight_kg REAL, body_fat_pct REAL, muscle_kg REAL, water_pct REAL,
    synced_at TEXT, raw TEXT
);
CREATE TABLE IF NOT EXISTS activities (
    activity_id TEXT PRIMARY KEY,
    date TEXT, start_ts TEXT, type TEXT, name TEXT,
    duration_s REAL, distance_m REAL, avg_hr REAL, max_hr REAL,
    calories REAL, load REAL, aerobic_te REAL, anaerobic_te REAL,
    pace_s_km REAL, ascent REAL, raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_act_date ON activities(date);
CREATE TABLE IF NOT EXISTS coach_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT, created_at TEXT, source TEXT, text TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS activity_detail (
    activity_id TEXT PRIMARY KEY,
    fetched_at TEXT,
    cad_avg REAL, cad_max REAL, stride_cm REAL,
    payload TEXT
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # 旧库没有新列：CREATE TABLE IF NOT EXISTS 不会补，逐列 ALTER
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(daily)")}
        for c in ("nap_min", "naps", "sleep_need_min"):
            if c not in have:
                self.conn.execute(f"ALTER TABLE daily ADD COLUMN {c} INTEGER")
        # 体重 / 体成分由 body 表按日回填（见 body.fill_daily），不参与 sync 的 upsert：
        # DAILY_COLS 里刻意没有这四列，否则每天同步会用 None 把体重抹掉。
        for c in ("weight_kg", "muscle_kg"):
            if c not in have:
                self.conn.execute(f"ALTER TABLE daily ADD COLUMN {c} REAL")
        for c in ("body_fat_pct", "water_pct"):
            if c not in have:
                self.conn.execute(f"ALTER TABLE daily ADD COLUMN {c} REAL")
        # 身体电量「当前值」与当日曲线：daily summary 里的 bb_high 是全天最高，
        # 一天里只有早上那一次值，看板上它永远不动。这三列由 live.py 每次出图前
        # 实时回填（不放进 DAILY_COLS，避免 sync 的 upsert 用 None 抹掉）。
        if "bb_now" not in have:
            self.conn.execute("ALTER TABLE daily ADD COLUMN bb_now INTEGER")
        if "bb_now_at" not in have:
            self.conn.execute("ALTER TABLE daily ADD COLUMN bb_now_at TEXT")
        if "bb_series" not in have:
            self.conn.execute("ALTER TABLE daily ADD COLUMN bb_series TEXT")
        self.conn.commit()

    # ---------- 写入 ----------
    def upsert_daily(self, row: dict, merge: bool = False) -> None:
        """`merge=True` 只更新非 None 的列（高驰/佳明双源共用 daily 表时，
        一边缺的字段不能把另一边写过的值抹成 NULL）。Garmin 全量同步仍走
        默认的整行覆盖，保证同源重拉时删掉的字段也会被清掉。"""
        data = {k: row.get(k) for k in DAILY_COLS}
        if isinstance(data.get("raw"), (dict, list)):
            data["raw"] = json.dumps(data["raw"], ensure_ascii=False)
        if merge:
            cols = [c for c in DAILY_COLS if data.get(c) is not None]
            if not cols:
                return
            cols = list(dict.fromkeys(["date"] + cols))
            sets = [f"{c}=COALESCE(excluded.{c}, {c})" for c in cols if c != "date"]
            ph = ",".join("?" * len(cols))
            self.conn.execute(
                f"INSERT INTO daily ({','.join(cols)}) VALUES ({ph}) "
                f"ON CONFLICT(date) DO UPDATE SET {','.join(sets)}",
                [data[c] for c in cols],
            )
            self.conn.commit()
            return
        cols = ",".join(DAILY_COLS)
        ph = ",".join("?" * len(DAILY_COLS))
        updates = ",".join(f"{c}=excluded.{c}" for c in DAILY_COLS if c != "date")
        self.conn.execute(
            f"INSERT INTO daily ({cols}) VALUES ({ph}) "
            f"ON CONFLICT(date) DO UPDATE SET {updates}",
            [data[c] for c in DAILY_COLS],
        )
        self.conn.commit()

    def upsert_activity(self, row: dict) -> None:
        data = {k: row.get(k) for k in ACT_COLS}
        data["activity_id"] = str(data["activity_id"])
        if isinstance(data.get("raw"), (dict, list)):
            data["raw"] = json.dumps(data["raw"], ensure_ascii=False)
        cols = ",".join(ACT_COLS)
        ph = ",".join("?" * len(ACT_COLS))
        updates = ",".join(f"{c}=excluded.{c}" for c in ACT_COLS if c != "activity_id")
        self.conn.execute(
            f"INSERT INTO activities ({cols}) VALUES ({ph}) "
            f"ON CONFLICT(activity_id) DO UPDATE SET {updates}",
            [data[c] for c in ACT_COLS],
        )
        self.conn.commit()

    def add_note(self, text: str, date: str | None = None, source: str = "agent") -> None:
        self.conn.execute(
            "INSERT INTO coach_notes (date, created_at, source, text) VALUES (?,?,?,?)",
            (date or time.strftime("%Y-%m-%d"), time.strftime("%Y-%m-%d %H:%M"), source, text),
        )
        self.conn.commit()

    def save_detail(self, activity_id: str, payload: dict, fetched_at: str | None = None) -> None:
        sm = payload.get("summary") or {}
        self.conn.execute(
            "INSERT INTO activity_detail (activity_id, fetched_at, cad_avg, cad_max, stride_cm, payload)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(activity_id) DO UPDATE SET"
            " fetched_at=excluded.fetched_at, cad_avg=excluded.cad_avg, cad_max=excluded.cad_max,"
            " stride_cm=excluded.stride_cm, payload=excluded.payload",
            (
                str(activity_id),
                fetched_at or time.strftime("%Y-%m-%d %H:%M:%S"),
                sm.get("cad_avg"), sm.get("cad_max"), sm.get("stride_avg"),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        self.conn.commit()

    def get_detail(self, activity_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT payload FROM activity_detail WHERE activity_id=?", (str(activity_id),)
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    def get_activity(self, activity_id: str | None = None, date: str | None = None) -> dict | None:
        if activity_id:
            row = self.conn.execute(
                "SELECT * FROM activities WHERE activity_id=?", (str(activity_id),)
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM activities WHERE date=? ORDER BY duration_s DESC LIMIT 1", (date,)
            ).fetchone()
        return dict(row) if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    # ---------- 读取 ----------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def day(self, date: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM daily WHERE date=?", (date,)).fetchone()
        return dict(row) if row else None

    def days(self, start: str, end: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM daily WHERE date BETWEEN ? AND ? ORDER BY date", (start, end)
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_days(self, n: int = 30, end: str | None = None) -> list[dict]:
        end = end or time.strftime("%Y-%m-%d")
        rows = self.conn.execute(
            "SELECT * FROM daily WHERE date<=? ORDER BY date DESC LIMIT ?", (end, n)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def activities(self, start: str, end: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM activities WHERE date BETWEEN ? AND ? ORDER BY date DESC, start_ts DESC",
            (start, end),
        ).fetchall()
        return [dict(r) for r in rows]

    def latest_note(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM coach_notes ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def notes(self, limit: int = 5) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM coach_notes ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        d = self.conn.execute(
            "SELECT COUNT(*) c, MIN(date) a, MAX(date) b FROM daily"
        ).fetchone()
        a = self.conn.execute("SELECT COUNT(*) c FROM activities").fetchone()
        return {
            "days": d["c"], "first": d["a"], "last": d["b"],
            "activities": a["c"],
            "path": str(self.path),
        }
