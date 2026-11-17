# -*- coding: utf-8 -*-
"""把本地计划推成佳明训练课，排进 Connect 日历，手表上就能直接照着跑。

为什么不用现成的 `delete_old()`
------------------------------
`workout.delete_old()` 删的是**所有**课名以 `W<数字>` 开头的课程。它本来是给
「作者自己的备赛课表」用的，那时候账号里只有他自己的课。

现在计划是**用户自己的**，同一个账号里还可能有官方教练计划的课。按名字前缀
批量删，迟早把不该删的删掉。所以这里换一套：

    data/plan_push.json  记住「哪些 workoutId 是我们建的」
    重推时只删记录里的 id，别人的课一个都不碰

代价是要自己维护一份记录，好处是永远不会误伤——这个取舍在用户数据上不值得赌。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from pathlib import Path

PUSH_NAME = "plan_push.json"


def _home() -> Path:
    env = os.environ.get("GARMIN_HOME")
    return Path(env).expanduser() if env else Path(__file__).resolve().parent.parent


def push_path(path: Path | None = None) -> Path:
    p = Path(path) if path else (_home() / "data" / PUSH_NAME)
    return p if p.suffix == ".json" else p / PUSH_NAME


def _load(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    days = d.get("days")
    if not isinstance(days, dict):
        days = {}
    return {"version": 1, "days": days}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def record(path: Path | None = None) -> dict:
    """已经推到佳明的课：按日期列出 workoutId。"""
    d = _load(push_path(path))
    days = {k: v for k, v in d["days"].items() if isinstance(v, list) and v}
    return {"ok": True, "days": len(days), "items": [
        {"date": k, "workout_ids": v} for k, v in sorted(days.items())]}


def info_of(item: dict, date: str, week_km: float = 0.0,
            phase: str = "BASE") -> dict:
    """本地课次 → `workout.build_all()` 认识的 info。

    字段名刻意对齐备赛课表那套（`kind` / `abbr` / `dow` / `week`），这样推送
    逻辑只有一份，不用为「自建计划」再写一套建课代码。
    """
    d = dt.date.fromisoformat(str(date)[:10])
    kind = str(item.get("kind") or "easy")
    # 本地 kind 与 workout 的 kind 只有一处不同：力量课它叫 str
    if kind == "strength":
        kind = "str"
    return {
        "is_rest": False,
        "kind": kind,
        "km": float(item.get("km") or 0),
        "minutes": int(item.get("minutes") or 0),
        "vert": 0,
        "week": d.isocalendar()[1],
        "dow": ("周一", "周二", "周三", "周四", "周五", "周六", "周日")[d.weekday()],
        "abbr": str(item.get("name") or "训练")[:12],
        "phase": phase,
        "tip": str(item.get("detail") or "")[:80],
        "note": str(item.get("note") or "")[:80],
        "week_km": week_km,
        "week_vert": 0,
        "focus": "",
    }


def push(client, days, week_km: float = 0.0, phase: str = "BASE",
         path: Path | None = None, dry: bool = False) -> dict:
    """把一周的课推到 Connect 并排进日历。

    `days` 是 `[{"date":..., "items":[...], "rest": bool}, ...]`，直接吃
    `plan.json` / 草稿的 `week[]`，不需要转换。

    `dry=True` 只返回将要提交的 payload，不联网——自检和「先看一眼」用。
    """
    from . import workout as W

    if not isinstance(days, list) or not days:
        return {"ok": False, "error": "没有要推送的日子"}

    p = push_path(path)
    state = _load(p)
    created = scheduled = 0
    errors: list[dict] = []
    preview: list[dict] = []
    dates: list[str] = []

    for w in days:
        if not isinstance(w, dict):
            continue
        date = str(w.get("date") or "")[:10]
        items = [x for x in (w.get("items") or []) if isinstance(x, dict)]
        if not date or not items:
            continue

        payloads = []
        for it in items:
            payloads.extend(W.build_all(info_of(it, date, week_km, phase)) or [])
        if not payloads:
            continue

        if dry:
            preview.append({"date": date,
                            "names": [x.get("workoutName") for x in payloads],
                            "payload": payloads[0]})
            continue

        # 同一天重推：先把自己上次建的删掉，避免日历里堆一串重复课
        old = state["days"].get(date) or []
        for wid in old:
            try:
                client.delete("/workout-service/workout/%s" % wid)
            except Exception as e:                        # noqa: BLE001
                errors.append({"date": date, "step": "delete", "id": wid,
                               "error": f"{type(e).__name__}: {e}"})

        ids: list[int] = []
        for payload in payloads:
            try:
                got = W.create(client, payload)
                wid = (got or {}).get("workoutId")
                if not wid:
                    errors.append({"date": date, "step": "create",
                                   "name": payload.get("workoutName"),
                                   "error": "接口没有返回 workoutId"})
                    continue
                W.schedule(client, wid, date)
                ids.append(wid)
                created += 1
                scheduled += 1
            except Exception as e:                        # noqa: BLE001
                errors.append({"date": date, "step": "create",
                               "name": payload.get("workoutName"),
                               "error": f"{type(e).__name__}: {e}"})
        if ids:
            state["days"][date] = ids
            dates.append(date)

    if dry:
        return {"ok": True, "dry": True, "days": len(preview), "preview": preview}

    state["updated_at"] = int(time.time())
    _save(p, state)
    return {"ok": not errors, "created": created, "scheduled": scheduled,
            "days": len(dates), "dates": dates, "errors": errors}


def clear(client, path: Path | None = None) -> dict:
    """把自己推过的课从 Connect 删掉（只删记录里的，不动别人的）。"""
    p = push_path(path)
    state = _load(p)
    ok = bad = 0
    for date, ids in list(state["days"].items()):
        for wid in ids if isinstance(ids, list) else []:
            try:
                if client.delete("/workout-service/workout/%s" % wid):
                    ok += 1
                else:
                    bad += 1
            except Exception:                             # noqa: BLE001
                bad += 1
        state["days"].pop(date, None)
    _save(p, state)
    return {"ok": bad == 0, "deleted": ok, "failed": bad}
