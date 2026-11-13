"""Shared sport taxonomy, display units and athlete-specific settings."""
from __future__ import annotations

import json
import math
import os
import threading
import uuid
from pathlib import Path

RUN_TYPES = frozenset(("running", "trail_running", "treadmill_running", "track_running",
                       "virtual_run", "indoor_running", "obstacle_run", "ultra_run"))
BIKE_TYPES = frozenset(("cycling", "road_biking", "mountain_biking", "gravel_cycling",
                        "indoor_cycling", "virtual_ride", "e_bike_fitness"))
SWIM_TYPES = frozenset(("swimming", "lap_swimming", "open_water_swimming"))
HOME = Path(os.environ.get("GARMIN_HOME") or Path(__file__).resolve().parent.parent)
_CONFIG_LOCK = threading.Lock()


def number(value, default=None):
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def family(kind) -> str:
    kind = str(kind or "").lower().strip()
    if kind in RUN_TYPES or kind in ("run", "跑步"):
        return "run"
    if kind in BIKE_TYPES or kind in ("bike", "ride", "骑行"):
        return "bike"
    if kind in SWIM_TYPES:
        return "swim"
    return kind


def allowed_types(selector) -> set | None:
    key = str(selector or "").lower().strip()
    if key in ("", "all", "全部"):
        return None
    if family(key) == "run" and key in ("run", "跑步"):
        return set(RUN_TYPES)
    if key in ("bike", "ride", "骑行"):
        return set(BIKE_TYPES)
    return {x.strip() for x in key.split(",") if x.strip()}


def config() -> dict:
    try:
        value = json.loads((HOME / "config.json").read_text("utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def profile(kind, cfg=None) -> dict:
    cfg = config() if cfg is None else cfg
    profiles = cfg.get("sport_profiles") or {}
    value = (profiles.get(family(kind)) or {}) if isinstance(profiles, dict) else {}
    return value if isinstance(value, dict) else {}


def preferred(res=None) -> str:
    key = (res or {}).get("preferred_sport") or config().get("preferred_sport") or "run"
    return "bike" if family(key) == "bike" else "run"


def pace_seconds(row) -> float | None:
    if family(row.get("type")) == "bike":
        # A detail card may carry a more authoritative native average.
        speed = number(row.get("avg_speed_mps"))
        if speed is not None:
            return 1000 / speed if speed > 0 else None
        # Historical COROS rows retain the original list response. Re-read its
        # native units without requiring a full history re-sync or DB rewrite.
        raw = row.get("raw") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                raw = {}
        if isinstance(raw, dict) and (row.get("source") == "coros" or "labelId" in raw):
            from .coros import average_speed_mps
            speed = average_speed_mps(raw)
            if speed is not None and speed > 0:
                return 1000 / speed
    pace = number(row.get("pace_s_km"))
    dist, dur = number(row.get("distance_m"), 0), number(row.get("duration_s"), 0)
    return pace if pace and pace > 0 else (dur * 1000 / dist if dist > 0 and dur > 0 else None)


def metric(row) -> dict:
    kind = family(row.get("type"))
    pace = pace_seconds(row)
    if kind == "bike":
        return {"label": "均速", "value": f"{3600 / pace:.1f}" if pace else "—", "unit": "km/h"}
    if kind == "swim":
        pace = pace / 10 if pace else None
    if kind in ("run", "swim", "walking", "hiking"):
        return {"label": "配速", "value": f"{int(round(pace)) // 60}′{int(round(pace)) % 60:02d}″" if pace else "—",
                "unit": "/100m" if kind == "swim" else "/km"}
    minutes = (number(row.get("duration_s"), 0) or 0) / 60
    return {"label": "时长", "value": f"{minutes:.0f}", "unit": "分钟"}


def metric_text(row, label=False) -> str:
    m = metric(row)
    return (m["label"] + " " if label else "") + m["value"] + (" " if m["unit"] == "km/h" else "") + m["unit"]


def item_family(item) -> str:
    return family(item.get("sport_key") or item.get("type") or item.get("sport"))


def completion(items: list, activities: list) -> bool:
    """Every scheduled sport must have matching evidence; time cannot replace distance.

    Items without a sport cannot be matched safely. A record must reach 90% of the
    aggregate distance target, or the time target when no distance was specified.
    """
    targets = {}
    for item in items:
        if item.get("rest"):
            continue
        key = item_family(item)
        if not key:
            return False
        bucket = targets.setdefault(key, [0.0, 0.0])
        bucket[0] += number(item.get("km"), 0) or 0
        bucket[1] += number(item.get("minutes"), 0) or 0
    if not targets:
        return False
    for key, (km, minutes) in targets.items():
        rows = [a for a in activities if family(a.get("type") or a.get("sport_key")) == key]
        distance = sum(number(a.get("distance_m"), 0) or 0 for a in rows) / 1000
        duration = sum(number(a.get("duration_s"), 0) or 0 for a in rows) / 60
        if km > 0:
            if distance < km * .9:
                return False
        elif minutes > 0:
            if duration < minutes * .9:
                return False
        elif duration < 5:
            return False
    return True


def save_settings(values: dict) -> None:
    """Validate the complete form before atomically preserving unrelated settings."""
    preferred_key = values.get("preferred_sport", "run")
    if preferred_key not in ("run", "bike"):
        raise ValueError("请选择跑步或骑行")
    updates = {}
    for key in ("run", "bike"):
        fields = {}
        for field, low, high in (("max_hr", 80, 240), ("lthr", 60, 230), ("ftp_w", 30, 1000)):
            if key == "run" and field == "ftp_w":
                continue
            raw = values.get(f"{key}_{field}", "")
            v = number(raw)
            if str(raw or "").strip() and (v is None or not low <= v <= high):
                raise ValueError(f"{key} {field} 要在 {low}–{high} 之间")
            fields[field] = v
        if fields.get("lthr") and fields.get("max_hr") and fields["lthr"] >= fields["max_hr"]:
            raise ValueError("阈值心率必须小于最大心率")
        names = str(values.get(f"{key}_hr_zone_names") or "").strip()
        parts = [x.strip() for x in names.replace("，", ",").split(",")]
        if names and (len(parts) != 5 or any(not x or len(x) > 4 for x in parts)):
            raise ValueError("心率区间名称需填写 5 个，以逗号分隔，每个最多 4 字")
        fields["hr_zone_names"] = parts if names else None
        if key == "bike":
            workout = values.get("bike_workout", "endurance")
            if workout not in ("endurance", "recovery", "intervals"):
                raise ValueError("请选择有效的骑行训练模板")
            fields["workout"] = workout
        updates[key] = fields
    with _CONFIG_LOCK:
        cfg = config()
        profiles = cfg.get("sport_profiles")
        profiles = profiles if isinstance(profiles, dict) else {}
        for key, fields in updates.items():
            profiles[key] = {**profile(key, cfg), **fields}
        cfg.update(preferred_sport=preferred_key, sport_profiles=profiles)
        path = HOME / "config.json"
        temporary = path.with_name(f".config-{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
