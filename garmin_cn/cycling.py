"""Cycling metrics computed before downsampling. No FTP is guessed.

NP uses the 30-second rolling fourth-power mean; IF=NP/FTP;
TSS=hours*IF**2*100. References:
https://www.trainingpeaks.com/coach-blog/normalized-power-how-coaches-use/
https://www.trainingpeaks.com/learn/articles/estimating-training-stress-score-tss/
"""
from __future__ import annotations

from collections import deque
from .sports import number, profile

VERSION = 1
ZONE_LIMITS = (.55, .75, .90, 1.05, 1.20, 1.50)


def analyze(points: list, ftp=None) -> dict:
    """Use actual sample durations, preserving zeros and breaking sensor gaps.

    Hold samples for at most 5s; longer gaps and missing power break the NP
    window. TSS is withheld unless at least 90% of elapsed time has power.
    """
    ftp = number(ftp)
    ftp = ftp if ftp and 30 <= ftp <= 1000 else None
    rows = sorted((p for p in points if number(p.get("t")) is not None), key=lambda p: p["t"])
    work = known = cad_sum = cad_seconds = 0.0
    powers, rolling, fourth = [], deque(), []
    histogram = {}
    zone_seconds = [0.0] * 7
    total = max(0, rows[-1]["t"] - rows[0]["t"]) if len(rows) > 1 else 0
    window_sum = 0.0
    # One-second bins make the 30s window independent of sampling frequency.
    for a, b in zip(rows, rows[1:]):
        dt = b["t"] - a["t"]
        pwr = number(a.get("pwr"))
        cad = number(a.get("cad"))
        if dt <= 0 or dt > 5:
            rolling.clear()
            window_sum = 0.0
            continue
        if cad is not None and 0 <= cad <= 250:
            cad_sum += cad * dt
            cad_seconds += dt
        if pwr is None or not 0 <= pwr <= 3000:
            rolling.clear()
            window_sum = 0.0
            continue
        known += dt
        work += pwr * dt
        powers.append(pwr)
        key = str(round(pwr, 1))
        histogram[key] = histogram.get(key, 0) + dt
        if ftp:
            zone = next((i for i, limit in enumerate(ZONE_LIMITS) if pwr <= ftp * limit), 6)
            zone_seconds[zone] += dt
        for second in range(int(a["t"]), int(b["t"])):
            rolling.append(pwr)
            window_sum += pwr
            if len(rolling) > 30:
                window_sum -= rolling.popleft()
            if len(rolling) == 30:
                fourth.append((window_sum / 30) ** 4)
    np = (sum(fourth) / len(fourth)) ** .25 if fourth else None
    coverage = known / total if total else 0
    intensity = np / ftp if np is not None and ftp else None
    return {"version": VERSION, "ftp_w": ftp,
            "avg_power_w": round(work / known, 1) if known else None,
            "max_power_w": max(powers) if powers else None,
            "cad_avg": round(cad_sum / cad_seconds, 1) if cad_seconds else None,
            "normalized_power_w": round(np, 1) if np is not None else None,
            "intensity_factor": round(intensity, 3) if intensity is not None else None,
            "tss": round(total / 3600 * intensity ** 2 * 100, 1)
                   if intensity is not None and coverage >= .9 else None,
            "work_kj": round(work / 1000, 1) if known else None,
            "power_seconds": round(known, 1), "power_coverage": round(coverage, 3),
            "power_histogram": histogram,
            "power_zones": [{"zone": i + 1, "seconds": round(v, 1)} for i, v in enumerate(zone_seconds)] if ftp else []}


def for_detail(detail: dict, ftp=None) -> dict:
    """Retain full-resolution aggregates and recalculate FTP-dependent values."""
    ftp = number(profile("bike").get("ftp_w")) if ftp is None else number(ftp)
    ftp = ftp if ftp and 30 <= ftp <= 1000 else None
    cached = detail.get("cycling") or {}
    if cached.get("version") != VERSION:
        return analyze((detail.get("series") or {}).get("points") or [], ftp)
    result = dict(cached)
    result["ftp_w"] = ftp
    np = number(result.get("normalized_power_w"))
    result["intensity_factor"] = round(np / ftp, 3) if np is not None and ftp else None
    seconds = result.get("power_seconds") or 0
    coverage = result.get("power_coverage") or 0
    result["tss"] = round(seconds / coverage / 3600 * (np / ftp) ** 2 * 100, 1) if np is not None and ftp and coverage >= .9 else None
    # Re-bin the full-resolution histogram when the current FTP changes.
    if ftp != cached.get("ftp_w"):
        zones = [0.0] * 7
        for power, duration in (cached.get("power_histogram") or {}).items():
            if ftp:
                zone = next((i for i, limit in enumerate(ZONE_LIMITS) if float(power) <= ftp * limit), 6)
                zones[zone] += duration
        result["power_zones"] = [{"zone": i + 1, "seconds": round(v, 1)} for i, v in enumerate(zones)] if ftp else []
    return result


def session(score, fatigue=0, ftp=None, workout="endurance") -> dict:
    """A cycling-specific local session; a recovery score never invents an FTP."""
    low = score is None or score < 75 or fatigue < -20 or workout == "recovery"
    minutes = 30 if low else 60
    lo, hi = (.40, .55) if low else (.56, .75)
    ftp = number(ftp)
    intensity = f"{ftp * lo:.0f}–{ftp * hi:.0f} W" if ftp else ("低强度，能轻松交谈" if low else "有氧强度，保持能交谈")
    result = {"type": "恢复骑行" if low else "有氧骑行", "sport_key": "cycling", "sport": "骑行",
            "duration": f"{minutes} 分钟", "minutes": minutes, "intensity": intensity,
            "why": "按恢复状态安排骑行；路况和风阻会影响均速，用功率或体感控制强度。",
            "steps": [{"name": "热身", "seconds": 300, "ftp_low": .4, "ftp_high": .55},
                      {"name": "主体", "seconds": (minutes - 10) * 60, "ftp_low": lo, "ftp_high": hi},
                      {"name": "放松", "seconds": 300, "ftp_low": .35, "ftp_high": .5}]}
    if workout == "intervals" and not low and score >= 88 and fatigue >= 0 and ftp:
        steps = [{"name": "热身", "seconds": 600, "ftp_low": .4, "ftp_high": .6}]
        for i in range(4):
            steps += [{"name": f"间歇 {i + 1}", "seconds": 180, "ftp_low": .9, "ftp_high": 1.0},
                      {"name": f"恢复 {i + 1}", "seconds": 180, "ftp_low": .4, "ftp_high": .55}]
        steps.append({"name": "放松", "seconds": 360, "ftp_low": .35, "ftp_high": .5})
        result.update(type="骑行功率间歇", minutes=40, duration="40 分钟", steps=steps,
                      intensity=f"4 × 3 分钟 {ftp * .9:.0f}–{ftp:.0f} W，组间恢复 3 分钟")
    elif workout == "intervals":
        result["why"] += "所选间歇模板暂未启用：需要设置 FTP，且恢复度与疲劳状态满足条件。"
    return result


def intervals(detail: dict) -> dict | None:
    """Detect repeated power efforts, including stationary indoor rides.

    Speed alone is not evidence: descents, traffic and wind also alternate.
    """
    points = (detail.get("series") or {}).get("points") or []
    points = sorted((p for p in points if number(p.get("t")) is not None), key=lambda p: p["t"])
    powers = sorted(number(p.get("pwr")) for p in points if number(p.get("pwr")) is not None and 0 <= number(p.get("pwr")) <= 3000)
    if len(powers) < 30:
        return None
    lo, hi = powers[len(powers) // 5], powers[len(powers) * 4 // 5]
    if hi < 50 or hi - lo < max(40, hi * .25):
        return None
    threshold = (lo + hi) / 2
    groups, current = [], []
    state = None
    for a, b in zip(points, points[1:]):
        value = number(a.get("pwr"))
        dt = b["t"] - a["t"]
        if value is None or not 0 <= value <= 3000 or not 0 < dt <= 30:
            if current:
                groups.append((state, current))
            current, state = [], None
            continue
        flag = value >= threshold
        if state is not None and state != flag:
            groups.append((state, current))
            current = []
        state = flag
        current.append((a, dt))
    if current:
        groups.append((state, current))
    segments = []
    for work, group in groups:
        duration = sum(dt for _, dt in group)
        if duration < 20:
            continue
        segments.append({"work": work, "dur_s": duration,
                         "power_w": round(sum(p["pwr"] * dt for p, dt in group) / duration),
                         "t0": group[0][0]["t"]})
    works = [s for s in segments if s["work"]]
    rests = [s for s in segments if not s["work"]]
    switches = sum(a["work"] != b["work"] for a, b in zip(segments, segments[1:]))
    if len(works) < 3 or len(rests) < 3 or switches < 5:
        return None
    return {"sets": len(works), "segments": segments[:32], "source": "power", "threshold_w": round(threshold)}


def interval_html(value: dict) -> str:
    bars = []
    peak = max(s["power_w"] for s in value["segments"]) or 1
    for s in value["segments"]:
        color = "var(--acc)" if s["work"] else "#66758B"
        bars.append(f'<div style="flex:1;min-width:32px;text-align:center;font-size:17px">'
                    f'<div style="height:100px;display:flex;align-items:end">'
                    f'<i style="display:block;width:100%;height:{max(8, s["power_w"] / peak * 100):.0f}%;background:{color}"></i></div>'
                    f'<b>{s["power_w"]} W</b><br>{s["dur_s"] / 60:.1f}′</div>')
    return '<div style="display:flex;gap:8px;overflow:hidden">' + ''.join(bars) + '</div>'


def power_chart(points: list, width: int, height: int) -> str:
    """Power/heart-rate chart for indoor rides without distance or GPS."""
    rows = sorted((p for p in points if number(p.get("t")) is not None), key=lambda p: p["t"])
    if len(rows) < 2:
        return '<div class="empty">无足够功率时序</div>'
    t0, span = rows[0]["t"], max(1, rows[-1]["t"] - rows[0]["t"])
    left, right, top, bottom = 68, width - 74, 30, height - 26
    paths = []
    for field, color, unit, side in (("pwr", "#FFC46B", "W", left - 5), ("hr", "#FF7A6B", "bpm", right + 5)):
        values = [number(p.get(field)) for p in rows if number(p.get(field)) is not None]
        if not values:
            continue
        lo = 0 if field == "pwr" else max(0, min(values) - 10)
        hi = max(lo + 1, max(values) * 1.05)
        segments, current, prev = [], [], None
        for p in rows:
            value = number(p.get(field))
            if value is None or (prev is not None and p["t"] - prev > 30):
                if current:
                    segments.append(current)
                current = []
            if value is not None:
                x = left + (p["t"] - t0) / span * (right - left)
                y = bottom - (value - lo) / (hi - lo) * (bottom - top)
                current.append(f'{x:.1f},{y:.1f}')
            prev = p["t"]
        if current:
            segments.append(current)
        for segment in segments:
            paths.append(f'<polyline points="{" ".join(segment)}" fill="none" stroke="{color}" stroke-width="2"/>')
        anchor = "end" if field == "pwr" else "start"
        paths.append(f'<text x="{side}" y="{top}" fill="{color}" font-size="15" text-anchor="{anchor}">{hi:.0f} {unit}</text>')
        paths.append(f'<text x="{side}" y="{bottom}" fill="{color}" font-size="15" text-anchor="{anchor}">{lo:.0f} {unit}</text>')
    paths.append(f'<text x="{left}" y="{height - 3}" fill="#939FAD" font-size="15">0 min</text>')
    paths.append(f'<text x="{right}" y="{height - 3}" fill="#939FAD" font-size="15" text-anchor="end">{span / 60:.1f} min</text>')
    return f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">' + ''.join(paths) + '</svg>'


def push_cli(args, db, client_factory) -> int:
    """Single-day cycling upload; dry runs never request a client or write remotely."""
    import datetime as dt
    import json
    from . import workout
    from .auth import AuthError
    from .metrics import derive
    from .store import Store
    if args.all or args.week or args.start or args.end or args.replace:
        print("× 骑行按单日推送，请使用 --date；不支持批量替换跑步课程")
        return 2
    day = args.date or dt.date.today().isoformat()
    try:
        dt.date.fromisoformat(day)
        store = Store(db)
        try:
            res = derive(store, day)
        finally:
            store.conn.close()
        score = res["readiness"]["score"]
        if score is None and not args.dry_run:
            print("× 缺少当日恢复数据，请先同步；可用 --dry-run 预览恢复骑行模板")
            return 2
        p = profile("bike")
        info = session(score, res["load"]["tsb"], p.get("ftp_w"), p.get("workout", "endurance"))
        result = workout.push_day(None if args.dry_run else client_factory(require=True), info, day, dry=args.dry_run)
    except (ValueError, AuthError) as e:
        print(f"× {e}")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("error") else 0
