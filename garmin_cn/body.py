"""体重 / 体成分：导入、趋势、与训练负荷的联合判断。

为什么单独一层
--------------
小米秤（Mi Body Composition Scale 2 / S400 / 8 电极）的数据到不了佳明：
佳明里只有 4 条手动录入的体重，`bodyFat/bodyWater/muscleMass` 全是 null。
所以体成分不进 daily 表，单独存 `body` 表，允许一天多次称重（晨起、训练后
各一次是常态，混成日均值会把水分波动抹平）。

导入来源统一成一张「扁平行」再入库，来源只影响 source 字段：
  csv   —— 小米运动健康「数据副本」导出、Zepp Life 导出、手工表
  garmin——佳明 connect 的 weigh-in（含历史手动录入）
  mi    —— 小米健康云（见 mi.py，ms116 体脂秤的主力来源）
  ble   —— 本机蓝牙直读秤的广播（后续接）
表头中英文都认，见 ALIASES；重名列取第一个能解析出数值的。

判断口径
--------
体重绝对值没有意义，**趋势**才有；体脂率是生物电阻抗估算（BIA），
绝对误差 ±3~5 个百分点，所以只在跨 ≥14 天、变化 ≥1 个百分点时才说话。
真正要抓的是「体重在掉 + 负荷在涨」这个组合——它比任何单项都更早提示
能量摄入不足（RED-S 早期），也是业余跑者最容易忽略的一条。
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import re
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS body (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    ts TEXT,
    weight_kg REAL,
    body_fat_pct REAL,
    muscle_kg REAL,
    water_pct REAL,
    visceral INTEGER,
    bmr INTEGER,
    source TEXT,
    raw TEXT,
    imported_at TEXT,
    UNIQUE(date, ts, source)
);
CREATE INDEX IF NOT EXISTS idx_body_date ON body(date);
"""

# 表头别名：小米运动健康 / Zepp Life / 佳明导出的叫法都收进来
ALIASES = {
    "date": ("date", "time", "datetime", "day", "日期", "时间", "测量时间",
             "日期时间", "记录时间", "测量日期"),
    "weight_kg": ("weight", "weightkg", "weight(kg)", "weight（kg）", "bodyweight",
                  "体重", "体重(kg)", "体重（kg）", "体重kg"),
    "body_fat_pct": ("bodyfat", "body_fat", "bodyfatpct", "fat", "fatrate", "fatpercent",
                     "体脂", "体脂率", "体脂率(%)", "体脂率（%）", "脂肪率", "体脂肪率"),
    "muscle_kg": ("muscle", "musclemass", "musclemasskg", "muscle(kg)",
                  "肌肉量", "肌肉量(kg)", "肌肉量（kg）", "肌肉kg"),
    "water_pct": ("water", "bodywater", "waterpct", "waterpercent", "moisture",
                  "水分", "水分率", "体水分", "体水分率", "水分率(%)"),
    "visceral": ("visceralfat", "visceral", "visceralfatgrade", "fatgrade",
                 "内脏脂肪", "内脏脂肪等级", "内脏脂肪指数"),
    "bmr": ("bmr", "basalmetabolism", "basalmetabolicrate", "bmrkcal",
            "基础代谢", "基础代谢率", "基础代谢(kcal)"),
}


def _key(s: str) -> str:
    """表头归一化：去空格/单位括号/下划线，转小写。"""
    return re.sub(r"[\s_\-()（）%％]", "", str(s)).lower()


_FIELD_BY_KEY = {_key(a): f for f, alts in ALIASES.items() for a in alts}


def _num(v) -> float | None:
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not s or s in ("-", "—", "N/A", "null", "None"):
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group())
    except ValueError:
        return None


def ensure(store) -> None:
    store.conn.executescript(SCHEMA)
    store.conn.commit()


def _norm_date(v) -> str | None:
    """接受 2026-11-03 / 2026/11/3 / 2026年11月3日 10:20 / ISO 时间戳。"""
    if v is None or str(v).strip() == "":
        return None
    s = str(v).strip()
    m = re.search(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        try:
            return dt.date(y, mo, d).isoformat()
        except ValueError:
            return None
    m = re.search(r"(\d{4})(\d{2})(\d{2})", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        try:
            return dt.date(y, mo, d).isoformat()
        except ValueError:
            return None
    return None


def _norm_ts(v, date: str) -> str:
    s = str(v or "").strip()
    t = re.search(r"(\d{1,2}):(\d{2})", s)
    return f"{date} {int(t.group(1)):02d}:{t.group(2)}" if t else date


def _weight(v) -> float | None:
    w = _num(v)
    if w is None:
        return None
    if w > 300:          # 佳明用克；有些导出也带克
        w /= 1000
    if not 20 <= w <= 300:
        return None
    return round(w, 2)


def _fat(v) -> float | None:
    f = _num(v)
    if f is None:
        return None
    if f > 1.5:          # 有的导出是 0.183 这种小数
        return round(f, 1)
    return round(f * 100, 1)


def _visceral(v) -> int | None:
    x = _num(v)
    return int(round(x)) if x is not None and 0 < x < 60 else None


def parse_table(rows: list[dict]) -> list[dict]:
    """把「表头 → 值」的字典行，映射成统一字段。表头不认识就跳过该列。"""
    out = []
    for r in rows:
        rec: dict = {}
        for k, v in r.items():
            field = _FIELD_BY_KEY.get(_key(k))
            if field and field not in rec:
                rec[field] = v
        date = _norm_date(rec.get("date"))
        if not date:
            continue
        w = _weight(rec.get("weight_kg"))
        if w is None:
            continue
        out.append({
            "date": date,
            "ts": _norm_ts(rec.get("date"), date),
            "weight_kg": w,
            "body_fat_pct": _fat(rec.get("body_fat_pct")),
            "muscle_kg": _weight(rec.get("muscle_kg")),
            "water_pct": _num(rec.get("water_pct")),
            "visceral": _visceral(rec.get("visceral")),
            "bmr": int(_num(rec.get("bmr")) or 0) or None,
            "raw": json.dumps(r, ensure_ascii=False)[:600],
        })
    return out


def read_file(path: Path) -> list[dict]:
    """CSV / JSON 都吃。JSON 可以是列表或 {"data": [...]}。"""
    p = Path(path)
    text = p.read_text(encoding="utf-8-sig", errors="replace")
    if p.suffix.lower() in (".json", ".jsonl"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = [json.loads(x) for x in text.splitlines() if x.strip()]
        if isinstance(data, dict):
            for k in ("data", "items", "records", "body_measurements", "measurements"):
                if isinstance(data.get(k), list):
                    data = data[k]
                    break
        return parse_table([x for x in data if isinstance(x, dict)])
    # CSV：小米导出的表头可能带单位与空格，按首行是否有表头猜
    sample = text[:4000]
    delim = ";" if sample.count(";") > sample.count(",") else ","
    rows = list(csv.DictReader(text.splitlines(), delimiter=delim))
    return parse_table(rows)


def save(store, records: list[dict], source: str) -> int:
    ensure(store)
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    n = 0
    for r in records:
        store.conn.execute(
            "INSERT INTO body (date, ts, weight_kg, body_fat_pct, muscle_kg, water_pct, "
            "visceral, bmr, source, raw, imported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(date, ts, source) DO UPDATE SET "
            "weight_kg=excluded.weight_kg, body_fat_pct=excluded.body_fat_pct, "
            "muscle_kg=excluded.muscle_kg, water_pct=excluded.water_pct, "
            "visceral=excluded.visceral, bmr=excluded.bmr, raw=excluded.raw, "
            "imported_at=excluded.imported_at",
            (r["date"], r["ts"], r["weight_kg"], r.get("body_fat_pct"),
             r.get("muscle_kg"), r.get("water_pct"), r.get("visceral"),
             r.get("bmr"), source, r.get("raw"), now),
        )
        n += 1
    store.conn.commit()
    fill_daily(store)
    return n


def fill_daily(store, days: int | None = 365) -> int:
    """把体成分按日合并进 daily 表，让「每日」记录自己带上体重。

    为什么回写 daily 而不是让看板现算：教练上下文、MCP、活动的联合分析都只读
    daily，一处写、处处有；也避免每次渲染都重新扫 body 表。
    只覆盖这两张表都有的日期，不新增只称重、没有任何日汇总的空行。
    """
    ensure(store)
    preferred = preferred_source(store)
    query = "SELECT date FROM daily"
    cur = (store.conn.execute(query) if days is None else store.conn.execute(
        query + " WHERE date >= date('now', ?)", (f"-{int(days)} days",)))
    dates = [r["date"] for r in cur.fetchall()]
    if not dates:
        return 0
    n = 0
    for d in dates:
        rs = store.conn.execute(
            "SELECT * FROM body WHERE date = ?", (d,)).fetchall()
        if not rs:
            continue
        rs = _prefer([dict(r) for r in rs], preferred)
        w = [r["weight_kg"] for r in rs if r.get("weight_kg")]
        fat = [r["body_fat_pct"] for r in rs if r.get("body_fat_pct")]
        mus = [r["muscle_kg"] for r in rs if r.get("muscle_kg")]
        wat = [r["water_pct"] for r in rs if r.get("water_pct")]
        store.conn.execute(
            "UPDATE daily SET weight_kg=?, body_fat_pct=?, muscle_kg=?, water_pct=? "
            "WHERE date=?",
            (round(sum(w) / len(w), 2) if w else None,
             round(sum(fat) / len(fat), 1) if fat else None,
             round(sum(mus) / len(mus), 2) if mus else None,
             round(sum(wat) / len(wat), 1) if wat else None, d),
        )
        n += 1
    store.conn.commit()
    return n


def import_file(store, path: Path, source: str = "csv") -> tuple[int, list[dict]]:
    recs = read_file(path)
    return save(store, recs, source), recs


def import_garmin(store, client, days: int = 400) -> int:
    """把佳明 connect 里的 weigh-in 拉进来（sourceType: MANUAL / INDEX_SCALE …）。"""
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    data = client.get(f"/weight-service/weight/range/{start}/{end}") or {}
    recs = []
    for s in data.get("dailyWeightSummaries") or []:
        for w in s.get("allWeightMetrics") or [s.get("latestWeight") or {}]:
            mm = w or {}
            date = _norm_date(mm.get("calendarDate") or s.get("summaryDate"))
            kg = _weight(mm.get("weight"))
            if not date or kg is None:
                continue
            recs.append({
                "date": date,
                "ts": date,
                "weight_kg": kg,
                "body_fat_pct": _fat(mm.get("bodyFat")),
                "muscle_kg": _weight(mm.get("muscleMass")),
                "water_pct": _num(mm.get("bodyWater")),
                "visceral": _visceral(mm.get("visceralFat")),
                "bmr": None,
                "raw": json.dumps(mm, ensure_ascii=False)[:600],
            })
    return save(store, recs, "garmin")


def rows(store, end: str | None = None, days: int = 60) -> list[dict]:
    ensure(store)
    end = end or dt.date.today().isoformat()
    start = (dt.date.fromisoformat(end) - dt.timedelta(days=days)).isoformat()
    cur = store.conn.execute(
        "SELECT * FROM body WHERE date BETWEEN ? AND ? ORDER BY date, ts", (start, end))
    return [dict(r) for r in cur.fetchall()]


SRC_RANK = {"mi": 0, "ble": 1, "huawei": 2, "csv": 3, "garmin": 4}
SOURCE_LABELS = {"mi": "小米", "huawei": "华为", "ble": "蓝牙秤",
                 "csv": "文件导入", "garmin": "佳明"}
_PREFERRED_SOURCE_KEY = "body.preferred_source"


def preferred_source(store) -> str:
    source = store.get_meta(_PREFERRED_SOURCE_KEY)
    return source if source in ("mi", "huawei") else "mi"


def sync_preferred(store, home, days: int = 30):
    """所有自动同步入口共用用户选择的体重云服务。"""
    if preferred_source(store) == "huawei":
        from . import huawei as cloud
    else:
        from . import mi as cloud
    return cloud.sync(store, home, days=days)


def source_settings(store) -> dict:
    """只读取本地记录，不表示某个云服务已连接或已支持。

    `available` 是库里实际有数据的来源；`choices` 是偏好弹窗该展示的
    小米和华为候选——没数据的来源也要能预设，否则用户永远等不到「能切换」的那天
    （典型：先选华为，再在电脑上导入会话，数据到账自动生效）。
    """
    ensure(store)
    available = [r[0] for r in store.conn.execute(
        "SELECT DISTINCT source FROM body WHERE source IS NOT NULL ORDER BY source")]
    return {"preferred": preferred_source(store), "available": available,
            "choices": ["mi", "huawei"],
            "labels": SOURCE_LABELS.copy()}


def set_preferred_source(store, source: str) -> None:
    if source not in ("mi", "huawei"):
        raise ValueError("首选体重来源仅支持小米或华为")
    store.set_meta(_PREFERRED_SOURCE_KEY, source)
    # 切换口径时重算全部已有日汇总，不留下旧年份的混合口径。
    fill_daily(store, days=None)


def selected_rows(store, end: str | None = None, days: int = 60) -> list[dict]:
    """趋势、最新指标和 AI 共用：逐日只保留一个来源的所有称重。"""
    grouped: dict[str, list[dict]] = {}
    for r in rows(store, end, days):
        grouped.setdefault(r["date"], []).append(r)
    preferred = preferred_source(store)
    return [r for group in grouped.values() for r in _prefer(group, preferred)]


def _prefer(rs: list[dict], preferred: str = "auto") -> list[dict]:
    """同一天多来源时只认最可信的那一个。

    典型冲突：秤记了 65.15，同时佳明里有一笔手动录的 66.3（往往是隔天补记）。
    两者混着取均值会把趋势抹平，权重也说不清，所以按来源先淘汰再取日均。
    """
    if not rs:
        return rs
    sources = {r.get("source") for r in rs}
    best = preferred if preferred in sources else min(
        sources, key=lambda s: (SRC_RANK.get(s, 99), str(s or "")))
    return [r for r in rs if r.get("source") == best]


def _daily(rows: list[dict]) -> list[tuple[str, float]]:
    """同一天多次称重取均值——晨起与训练后混在一起，均值比「最新一条」稳。"""
    by_date: dict[str, list[dict]] = {}
    for r in rows:
        by_date.setdefault(r["date"], []).append(r)
    out = []
    for d, grp in by_date.items():
        ws = [r["weight_kg"] for r in _prefer(grp) if r.get("weight_kg")]
        if ws:
            out.append((d, sum(ws) / len(ws)))
    return sorted(out)


def _daily_key(rows: list[dict], key: str, digits: int = 1) -> list[tuple[str, float]]:
    """把任一列按日聚成均值序列（体脂/肌肉/水分都要同口径，否则曲线互相打架）。"""
    by_date: dict[str, list[dict]] = {}
    for r in rows:
        by_date.setdefault(r["date"], []).append(r)
    out = []
    for d, grp in by_date.items():
        vals = []
        for r in _prefer(grp):
            v = r.get(key)
            if v not in (None, ""):
                try:
                    vals.append(float(v))
                except (TypeError, ValueError):
                    continue
        if vals:
            out.append((d, round(sum(vals) / len(vals), digits)))
    return sorted(out)


def _change(points: list[tuple[str, float]], min_span: int = 10) -> tuple[float | None, int]:
    """首尾变化 + 跨度。跨度不足就不给数：BIA 的日常噪声比一周真实变化还大。"""
    if len(points) < 2:
        return None, 0
    span = (dt.date.fromisoformat(points[-1][0])
            - dt.date.fromisoformat(points[0][0])).days
    if span < min_span:
        return None, span
    return round(points[-1][1] - points[0][1], 2), span


def _raw_num(rs: list[dict], key: str, lo: float, hi: float) -> float | None:
    """从小米原始 payload 里捞一个数（bmi / body_score 这类没有独立列的字段）。

    raw 入库时截到 600 字符，但这两个字段都在最前面，正则足够。
    """
    for r in reversed(rs):
        m = re.search(rf'"{key}"\s*:\s*(-?\d+(?:\.\d+)?)', r.get("raw") or "")
        if m:
            v = float(m.group(1))
            if lo < v < hi:
                return v
    return None


def _last(rs: list[dict], key: str):
    """最近一条**有值**的记录——CSV 里补录的行常常只有体重，不能让它把体脂抹掉。"""
    for r in reversed(rs):
        if r.get(key) not in (None, ""):
            return r[key]
    return None


def _src_of_last(rs: list[dict], key: str) -> str | None:
    """最近一条有该字段的记录的来源中文名，给教练上下文标注口径用。"""
    for r in reversed(rs):
        if r.get(key) not in (None, ""):
            s = r.get("source")
            return SOURCE_LABELS.get(s, s)
    return None


def stats(store, end: str | None = None, days: int = 28) -> dict:
    """体重趋势 + 体成分最新值 + 各指标变化。数据太少时判断留空，不硬编结论。"""
    rs = selected_rows(store, end, days + 14)
    # 体成分的「主口径」仍跟体重同一来源（同一台秤的读数才可比）；
    # 另取全量表给教练/简报兜底：首选来源没体脂时，别让模型以为完全没数据。
    all_rs = rows(store, end, days + 14)
    series = _daily(rs)
    fat_series = _daily_key(rs, "body_fat_pct")
    muscle_series = _daily_key(rs, "muscle_kg", 2)
    water_series = _daily_key(rs, "water_pct")
    out = {
        "preferred_source": preferred_source(store),
        "source": rs[-1].get("source") if rs else None,
        "source_label": SOURCE_LABELS.get(rs[-1].get("source"), rs[-1].get("source")) if rs else None,
        "count": len(series),
        "span_days": (len(series) and (dt.date.fromisoformat(series[-1][0])
                                       - dt.date.fromisoformat(series[0][0])).days) or 0,
        "series": [(d, round(w, 2)) for d, w in series],
        "latest": round(series[-1][1], 2) if series else None,
        "latest_date": series[-1][0] if series else None,
        "body_fat": _last(rs, "body_fat_pct"),
        "muscle": _last(rs, "muscle_kg"),
        "water": _last(rs, "water_pct"),
        "visceral": _last(rs, "visceral"),
        "bmr": _last(rs, "bmr"),
        "bmi": _raw_num(rs, "bmi", 8, 60),
        "score": _raw_num(rs, "body_score", 1, 100),
        # 教练/简报兜底：首选来源没体脂时，给出其他来源的最近值与来源名。
        # 主口径（上面的 body_fat）仍只认同一台秤，避免体重和体脂混源。
        "body_fat_any": _last(all_rs, "body_fat_pct"),
        "body_fat_any_src": _src_of_last(all_rs, "body_fat_pct"),
        "muscle_any": _last(all_rs, "muscle_kg"),
        "water_any": _last(all_rs, "water_pct"),
        "fat_series": fat_series[-30:],
        "muscle_series": muscle_series[-30:],
        "water_series": water_series[-30:],
        "avg7": None, "avg_prev7": None, "delta_w": None, "delta_pct_w": None,
        "fat_delta": None, "stale_days": None, "verdict": "", "level": "none",
        "fat_mass": None, "lean_mass": None,
        "fat_delta_span": None, "muscle_delta": None, "muscle_delta_span": None,
        "water_delta": None, "water_delta_span": None,
        "delta_4w": None,
    }
    if series:
        ref = dt.date.fromisoformat(end or dt.date.today().isoformat())
        out["stale_days"] = max(0, (ref - dt.date.fromisoformat(series[-1][0])).days)
    if len(series) < 2:
        out["verdict"] = "体重只有 1 次记录，先连续称一周再谈趋势" if series else "还没有体重数据"
        return out
    end_d = dt.date.fromisoformat(series[-1][0])
    last7 = [w for d, w in series if (end_d - dt.date.fromisoformat(d)).days < 7]
    prev7 = [w for d, w in series
             if 7 <= (end_d - dt.date.fromisoformat(d)).days < 14]
    if last7:
        out["avg7"] = round(sum(last7) / len(last7), 2)
    if prev7:
        out["avg_prev7"] = round(sum(prev7) / len(prev7), 2)
    if out["avg7"] and out["avg_prev7"]:
        out["delta_w"] = round(out["avg7"] - out["avg_prev7"], 2)
        out["delta_pct_w"] = round(out["delta_w"] / out["avg_prev7"] * 100, 2)

    # 脂肪量 / 瘦体重：由体重 × 体脂率推导，比单看体脂率直观
    if out["latest"] and out["body_fat"]:
        fat_kg = out["latest"] * out["body_fat"] / 100
        out["fat_mass"] = round(fat_kg, 1)
        out["lean_mass"] = round(out["latest"] - fat_kg, 1)

    # 体脂 / 肌肉 / 水分：14 天以上才给变化值（BIA 的绝对误差就有 ±3~5 pt）
    for key, src in (("fat_delta", fat_series), ("muscle_delta", muscle_series),
                     ("water_delta", water_series)):
        d, span = _change(src)
        out[key] = d
        out[key.replace("_delta", "_delta_span")] = span
    # 兼容旧字段：看板里一直读的是 fat_span
    out["fat_span"] = out["fat_delta_span"]
    out["fat_pts"] = [(d, round(v, 1)) for d, v in fat_series]

    # 4 周变化：取距今 ≥28 天的最后一个点，用来区分「短期水分」与「真的在变」。
    # 同时记下实际天数——数据不够时基准会落到 40 天前，标「4 周」就是骗人。
    if len(series) >= 3:
        target = end_d - dt.timedelta(days=28)
        base = [s for s in series if dt.date.fromisoformat(s[0]) <= target]
        if base:
            out["delta_4w"] = round(series[-1][1] - base[-1][1], 2)
            out["delta_4w_days"] = (end_d - dt.date.fromisoformat(base[-1][0])).days
    return out


def verdict(st: dict, ctl_delta: float | None = None) -> tuple[str, str]:
    """体重趋势 × 训练负荷 → 一句结论 + 分级（good / watch / warn / none）。

    只在国内业余跑者的常见情境上做判断：掉重、增重、稳定三类，再乘上
    「负荷在涨/平/降」。脱水会伪装成掉重，所以先用水分率和单日跌幅排除。
    """
    pct = st.get("delta_pct_w")
    if pct is None:
        return (st.get("verdict") or "体重数据不足，无法判断", "none")

    stale = st.get("stale_days")
    if stale and stale >= 14:
        return (f"上次称重已是 {stale} 天前，趋势判断参考价值有限；"
                f"先恢复晨起空腹称重（同一时间、同一状态）", "watch")

    # 一周掉得多、一个月却几乎没动 → 水分/进食波动，不是真的在掉脂肪。
    # 这条要排在 RED-S 之前：否则每周的水分波动都会被写成「能量摄入不足」的警报。
    d4w = st.get("delta_4w")
    if d4w is not None and abs(d4w) < 0.5 and abs(pct) >= 0.8:
        return (f"一周 {pct:+.1f}%（{st.get('delta_w'):+.1f} kg），"
                f"但 {st.get('delta_4w_days') or 28} 天累计只有 {d4w:+.1f} kg："
                f"更像水分与进食波动，先不用调饮食", "watch")

    # 单日暴跌但 7 天均值变化不大 → 多半是脱水，不是掉脂肪
    series = st.get("series") or []
    if len(series) >= 2 and st.get("latest") and st.get("avg7"):
        drop = st["avg7"] - st["latest"]
        if drop > 0.8 and abs(pct) < 0.4:
            return (f"比 7 天均值低 {drop:.1f} kg，水分波动为主（训练失水/未进食），"
                    f"别当减重成果", "watch")

    up = (ctl_delta or 0) > 1.5
    down = (ctl_delta or 0) < -1.5
    if pct <= -0.8:
        if up:
            return (f"一周 {pct:+.1f}%，同时负荷在涨：能量摄入可能跟不上，"
                    f"留意疲劳、晨脉升高、配速掉——这是 RED-S 的早期组合", "warn")
        return (f"一周 {pct:+.1f}%，主动减重节奏里算偏快（建议 ≤0.5~0.7%/周），"
                f"注意蛋白质与力量训练，别掉肌肉", "watch")
    if pct >= 0.8:
        if down:
            return (f"一周 {pct:+.1f}% 而负荷在降：更像恢复期补回来了，不是长胖", "good")
        return (f"一周 {pct:+.1f}%，增重偏快（>0.8%/周），若非刻意增肌可看看饮食", "watch")
    if up:
        return (f"体重稳定（{pct:+.1f}%），负荷在涨：训练适应正常的信号", "good")
    return (f"体重稳定（{pct:+.1f}%），能量平衡", "good")


def text(st: dict, ctl_delta: float | None = None) -> str:
    """给微信简报 / 教练上下文的一段纯文本。"""
    if not st.get("latest"):
        return "体重：暂无数据（`run.bat body --import <文件>` 或 `--garmin`）"
    bits = [f"体重 {st['latest']:.1f} kg"]
    if st.get("stale_days"):
        bits[0] += f"（{st['stale_days']} 天前）"
    if st.get("delta_w") is not None:
        bits.append(f"7 天 {st['delta_w']:+.2f} kg（{st['delta_pct_w']:+.1f}%）")
    if st.get("body_fat"):
        fat = f"体脂 {st['body_fat']:.1f}%"
        if st.get("fat_mass"):
            fat += f"（脂肪 {st['fat_mass']:.1f} kg）"
        # BIA 的绝对误差就有 ±3~5 pt，变化不到 1 pt 不值得提
        if st.get("fat_delta") is not None and abs(st["fat_delta"]) >= 1.0:
            fat += f"（{st['fat_span']} 天 {st['fat_delta']:+.1f} pt）"
        bits.append(fat)
    elif st.get("body_fat_any"):
        # 首选秤只称体重、体脂在另一台秤上：仍给出来，但标明不是同一口径
        bits.append(f"体脂 {st['body_fat_any']:.1f}%（来自"
                    f"{st.get('body_fat_any_src') or '另一来源'}，与体重不同秤）")
    if st.get("muscle"):
        mus = f"肌肉 {st['muscle']:.1f} kg"
        if st.get("muscle_delta") is not None:
            mus += f"（{st['muscle_delta_span']} 天 {st['muscle_delta']:+.1f}）"
        bits.append(mus)
    elif st.get("muscle_any"):
        bits.append(f"肌肉 {st['muscle_any']:.1f} kg（另一来源）")
    if st.get("water"):
        bits.append(f"水分 {st['water']:.0f}%")
    if st.get("visceral"):
        bits.append(f"内脏脂肪 {st['visceral']}")
    if st.get("bmi"):
        bits.append(f"BMI {st['bmi']:.1f}")
    if st.get("bmr"):
        bits.append(f"BMR {st['bmr']} kcal")
    v, _ = verdict(st, ctl_delta)
    return " ｜ ".join(bits) + "\n" + v
