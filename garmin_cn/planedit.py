# -*- coding: utf-8 -*-
"""训练计划的写入层：单日 / 整周保存、清除、列出本地改动。

为什么单独一个模块
------------------
`plan.py` 是**只读**的归一化层（日历 / 教练 / 本地兜底 → 同一结构），
谁都能调，也谁都不该在里面写文件。写入的需求完全不同：要校验、
要合并、要防止把来源快照改坏。混在一起，读的地方迟早把写逻辑带进去。

落盘只有一个文件：`data/plan_overrides.json`。

    data/plan.json            数据源快照（日历 / 教练 / 高驰），只由同步写
    data/plan_overrides.json  用户改动，按日期存，只有这里能改课表

这么分的原因：**快照可以被反复重新拉取覆盖，改动不能**。用户把周三从
10 km 改成 6 km，下次同步日历不能又变回 10 km。

一条改动长这样：

    {"2026-11-18": {"items": [{"name": "轻松跑", "km": 6, "kind": "easy",
                              "sport": "跑步", "sport_key": "running"}],
                    "rest": false, "note": "膝盖紧，减量", "source": "manual",
                    "updated_at": 1763452800}}

`items` 为空即「改成休息日」——这跟「没有改动」是两件不同的事，所以
清除改动必须显式调 `clear_day()`，而不是存一条空记录。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from pathlib import Path

# kind 枚举与中文名。`kind` 不填默认 `easy`：逼人分类只会让人放弃填计划，
# 大部分跑者心里只有「周三 7 公里」。
KIND = {
    "easy": "轻松跑",
    "tempo": "节奏跑",
    "interval": "间歇",
    "long": "长距离",
    "race": "比赛",
    "strength": "力量",
    "rest": "休息",
}
DEFAULT_KIND = "easy"

# 单日上限：超过这个数一定是填错了（100 km 也够超马了，60 km 是保守门槛）
MAX_KM_DAY = 60.0
MAX_KM_ITEM = 100.0
MAX_MIN_ITEM = 600
MAX_ITEMS_DAY = 5

_LOCK = threading.Lock()
_FILE_VERSION = 1
# 与 plan.py 的 OVERRIDES_NAME 必须一致：那边读、这边写，名字对不上就成了
# 「改了没生效」这种最难查的问题。
OVERRIDES_NAME = "plan_overrides.json"


def _home() -> Path:
    env = os.environ.get("GARMIN_HOME")
    return Path(env).expanduser() if env else Path(__file__).resolve().parent.parent


def overrides_path(path: Path | None = None) -> Path:
    p = Path(path) if path else (_home() / "data" / OVERRIDES_NAME)
    return p if p.suffix == ".json" else p / OVERRIDES_NAME


# ---------------------------------------------------------------- 读写
def _read(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    days = d.get("days")
    if not isinstance(days, dict):
        days = {}
    return {"version": int(d.get("version") or _FILE_VERSION), "days": days}


def _write(path: Path, data: dict) -> None:
    """先写临时文件再替换：写到一半被杀掉，不会留下半截 JSON 把计划读废。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _date(value) -> str:
    d = dt.date.fromisoformat(str(value or "")[:10])
    return d.isoformat()


# ---------------------------------------------------------------- 校验
def _num(value, name: str) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 不是数字：{value!r}")
    if v != v:                                   # NaN
        raise ValueError(f"{name} 不是数字")
    return v


def clean_item(item: dict) -> dict:
    """把一条课次规整成下游能直接用的形状。

    只保留白名单字段：`plan.json` 里的 items 会一路流到看板、教练上下文、
    训练课推送，多塞字段迟早有一处会把它当成本地活动的属性用。
    """
    if not isinstance(item, dict):
        raise ValueError("课次必须是对象")
    km = _num(item.get("km") or 0, "km")
    minutes = _num(item.get("minutes") or 0, "minutes")
    if km < 0 or minutes < 0:
        raise ValueError("km 和 minutes 不能是负数")
    if km > MAX_KM_ITEM:
        raise ValueError(f"单次里程 {km} km 超过上限 {MAX_KM_ITEM:g}")
    if minutes > MAX_MIN_ITEM:
        raise ValueError(f"单次时长 {minutes} 分钟超过上限 {MAX_MIN_ITEM}")
    if km <= 0 and minutes <= 0:
        raise ValueError("课次至少要填里程或时长")

    kind = str(item.get("kind") or DEFAULT_KIND).strip().lower()
    if kind not in KIND:
        raise ValueError(f"kind 只能是 {'/'.join(KIND)}，收到 {kind!r}")
    if kind == "rest":
        raise ValueError("休息日请用 rest:true 表示，不要写成一条休息课次")

    sport = str(item.get("sport") or "跑步")
    sport_key = str(item.get("sport_key") or "running")
    name = str(item.get("name") or KIND[kind]).strip()[:40] or KIND[kind]
    steps = item.get("steps")
    return {
        "name": name,
        "detail": str(item.get("detail") or "").strip()[:200],
        "sport": sport,
        "sport_key": sport_key,
        "kind": kind,
        "km": round(km, 2),
        "minutes": int(round(minutes)) or None,
        "steps": steps if isinstance(steps, list) else [],
        "rest": False,
        "note": str(item.get("note") or "").strip()[:200],
    }


def clean_day(items, *, rest: bool = False, note: str = "",
              origin: str = "manual") -> dict:
    # 只放过 None：空对象是「填错了」，静默丢掉会让用户少一条课还以为存进去了
    rows = [] if rest else [clean_item(x) for x in (items or []) if x is not None]
    if len(rows) > MAX_ITEMS_DAY:
        raise ValueError(f"一天最多 {MAX_ITEMS_DAY} 条课次")
    total = sum(r["km"] for r in rows)
    if total > MAX_KM_DAY:
        raise ValueError(f"当天合计 {total:g} km 超过上限 {MAX_KM_DAY:g}")
    return {"items": rows, "rest": not rows,
            "note": str(note or "").strip()[:200],
            # origin 区分「用户手改」和「生成器写进来的」：自动续下周时不能拿
            # 草稿盖掉用户亲手调过的那一天。
            "origin": "generated" if origin == "generated" else "manual",
            "source": "manual", "updated_at": int(time.time())}


# ---------------------------------------------------------------- 写入
def save_day(date: str, items=None, rest: bool = False, note: str = "",
             path: Path | None = None, origin: str = "manual") -> dict:
    """保存某一天的课。`rest=True` 或 items 为空 = 改成休息日。"""
    day = _date(date)
    row = clean_day(items, rest=rest, note=note, origin=origin)
    p = overrides_path(path)
    with _LOCK:
        data = _read(p)
        data["days"][day] = row
        _write(p, data)
        n = len(data["days"])
    return {"ok": True, "date": day, "rest": row["rest"],
            "items": len(row["items"]), "days": n}


def save_week(days, path: Path | None = None) -> dict:
    """整周一次写入。任何一天不合法就整体不落盘，不留半周。

    `days` 是 `[{"date": ..., "items": [...], "rest": bool, "note": ...}, ...]`。
    """
    if not isinstance(days, list) or not days:
        raise ValueError("days 必须是非空数组")
    rows, seen = {}, set()
    for d in days:
        if not isinstance(d, dict):
            raise ValueError("days 的每一项必须是对象")
        day = _date(d.get("date"))
        if day in seen:
            raise ValueError(f"日期重复：{day}")
        seen.add(day)
        rows[day] = clean_day(d.get("items"), rest=bool(d.get("rest")),
                              note=d.get("note") or "",
                              origin=str(d.get("origin") or "manual"))
    p = overrides_path(path)
    with _LOCK:
        data = _read(p)
        data["days"].update(rows)
        _write(p, data)
        n = len(data["days"])
    return {"ok": True, "saved": len(rows), "days": n,
            "dates": sorted(rows)}


def clear_day(date: str, path: Path | None = None) -> dict:
    """撤销某天的改动，回到数据源的说法。"""
    day = _date(date)
    p = overrides_path(path)
    with _LOCK:
        data = _read(p)
        had = data["days"].pop(day, None) is not None
        _write(p, data)
        n = len(data["days"])
    return {"ok": True, "date": day, "cleared": had, "days": n}


def clear_all(path: Path | None = None) -> dict:
    p = overrides_path(path)
    with _LOCK:
        data = _read(p)
        n = len(data["days"])
        _write(p, {"version": _FILE_VERSION, "days": {}})
    return {"ok": True, "cleared": n}


def list_overrides(path: Path | None = None) -> dict:
    """列出所有改动，按日期升序，附带每条的中文类型。"""
    p = overrides_path(path)
    data = _read(p)
    out = []
    for day in sorted(data["days"]):
        ov = data["days"][day] or {}
        items = [x for x in (ov.get("items") or []) if isinstance(x, dict)]
        out.append({
            "date": day,
            "dow": ("一二三四五六日")[dt.date.fromisoformat(day).weekday()],
            "rest": bool(ov.get("rest")) or not items,
            "note": ov.get("note") or "",
            "origin": ov.get("origin") or "manual",
            "updated_at": ov.get("updated_at") or 0,
            "km": round(sum(float(x.get("km") or 0) for x in items), 2),
            "items": [{"name": x.get("name"), "kind": x.get("kind"),
                       "label": KIND.get(str(x.get("kind") or ""), ""),
                       "km": x.get("km"), "minutes": x.get("minutes")}
                      for x in items],
        })
    return {"ok": True, "days": len(out), "items": out}


# ---------------------------------------------------------------- 编辑视图
def week_edit_view(plan: dict | None, monday: str | None = None,
                   today: str | None = None) -> dict:
    """把（已合并改动的）计划整理成 App 编辑页能直接渲染的结构。

    每天给三样东西：`items`（当前课）、`source`（来自哪个数据源）、
    `editable`（是不是本地改过）。过去的日子标 `past`，前端不该让人改历史。

    周一**永远按 `today` 推**，不按快照里的 `week[0]`：快照可能是上周同步的，
    照它推会让人在「本周编辑页」里改到上一周的课。快照过期时额外给 `stale`，
    让界面提示「先刷新」。
    """
    today = today or dt.date.today().isoformat()
    t0 = dt.date.fromisoformat(today)
    mon = dt.date.fromisoformat(str(monday)[:10]) if monday else t0 - dt.timedelta(days=t0.weekday())

    by_date = {w.get("date"): w for w in (plan or {}).get("week") or []
               if isinstance(w, dict)}
    snap = (plan or {}).get("week") or []
    stale = bool(snap) and str((snap[0] or {}).get("date") or "") != mon.isoformat()
    out = []
    for i in range(7):
        day = (mon + dt.timedelta(days=i)).isoformat()
        src = by_date.get(day) or {}
        items = [x for x in (src.get("items") or []) if isinstance(x, dict)]
        out.append({
            "date": day,
            "dow": ("周一", "周二", "周三", "周四", "周五", "周六", "周日")[i],
            "past": day < today,
            "rest": bool(src.get("rest")) or not items,
            "edited": bool(src.get("edited")),
            "source": src.get("source") or (plan or {}).get("source") or "",
            "note": src.get("note") or "",
            "km": round(sum(float(x.get("km") or 0) for x in items), 2),
            "items": [{"name": x.get("name"), "kind": x.get("kind") or DEFAULT_KIND,
                       "label": KIND.get(str(x.get("kind") or DEFAULT_KIND), KIND[DEFAULT_KIND]),
                       "km": x.get("km") or 0, "minutes": x.get("minutes") or 0,
                       "detail": x.get("detail") or ""} for x in items],
        })
    return {"ok": True, "monday": mon.isoformat(), "stale": stale,
            "week_km": round(sum(d["km"] for d in out), 2),
            "days": out}
