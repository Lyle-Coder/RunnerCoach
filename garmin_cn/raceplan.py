# -*- coding: utf-8 -*-
"""105K 备赛计划（本地课表）读取层。

数据直接复用 tools/make_plan.py 的 WEEKS、缩略语复用 tools/make_card.py，
保证「逐日课表 / 执行卡 / 每日看板」三处永远同源，不会各说一套。

只依赖标准库。这两份课表脚本属于个人数据，仓库不含；脚本缺失时
available() 返回 False，调用方自动回退到佳明教练计划。想用自己的课表，
把同名脚本放进 tools/ 并暴露 WEEKS / KIND_CN 即可。

命令行：run.bat today [--date YYYY-MM-DD] [--week] [--days N] [--json]
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
OVERRIDES = ROOT / "data" / "week_overrides.json"

_CACHE: dict = {}


def overrides() -> dict:
    """每周校准写下的周级覆盖：{"5": {"scale": 0.85, "quality_off": true, ...}}。

    只影响推送与「今日课次」，不改 WEEKS 源数据——计划本身保持原样，
    手表上按当周状态打折。重新生成前想清空就把文件删掉。
    """
    try:
        import json

        return json.loads(OVERRIDES.read_text(encoding="utf-8"))
    except Exception:
        return {}

# kind -> 中文课次名
KIND_CN = {
    "rest": "休息", "easy": "轻松跑", "str": "力量", "hill": "爬楼梯",
    "quality": "质量课", "long": "进山长课", "b2b": "背靠背", "race": "比赛",
}


def _load(mod_name: str, path: Path):
    if mod_name in _CACHE:
        return _CACHE[mod_name]
    if not path.is_file():
        raise FileNotFoundError(path)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"无法加载 {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(mod_name, None)
        raise
    _CACHE[mod_name] = mod
    return mod


def _mp():
    return _load("make_plan", TOOLS / "make_plan.py")


def _mc():
    _mp()  # make_card 顶层会 from make_plan import ...，先占位避免重复加载
    return _load("make_card", TOOLS / "make_card.py")


def available() -> bool:
    return (TOOLS / "make_plan.py").is_file() and (TOOLS / "make_card.py").is_file()


def span():
    """计划起止日期 (start, end)。"""
    mp = _mp()
    return mp.WEEK1, mp.date_of(mp.WEEKS[-1]["no"], 6)


def week_of(d: dt.date):
    """返回 (week dict, day index)；day index 0 = 周一。不在区间内返回 (None, None)。"""
    s, e = span()
    if not (s <= d <= e):
        return None, None
    no, i = divmod((d - s).days, 7)
    w = _mp().WEEKS[no] if 0 <= no < len(_mp().WEEKS) else None
    return w, i


def _hhmm(minutes: int) -> str:
    if not minutes:
        return "—"
    h, m = divmod(int(round(minutes)), 60)
    return f"{h}h{m:02d}" if h else f"{m} min"


def day_info(d: dt.date) -> dict | None:
    """某一天的完整课次信息。不在计划区间内返回 None。"""
    w, i = week_of(d)
    if w is None:
        return None
    mp, mc = _mp(), _mc()
    hit = None
    for row in mp.week_days(w):
        if row[7] == i:
            hit = row
            break
    if hit is None:  # pragma: no cover
        return None
    kind, title, minutes, km, vert, hr, note, _ = hit

    # 周级覆盖：每周日校准觉得该周该降量时，只改推送出去的量与强度，不动 WEEKS
    ov = overrides().get(str(w["no"]))
    if ov and kind != "race":
        scale = float(ov.get("scale") or 1.0)
        if ov.get("quality_off") and kind == "quality":
            kind, title = "easy", "轻松跑"
        km = round(km * scale, 1)
        vert = int(round(vert * scale))
        minutes = int(round(minutes * scale))

    main, sub = mc.abbr(w, i, kind, km, vert, minutes)
    tip = (mc.note_of(w, i, kind, minutes, vert) or "").replace("\\n", " ")
    tot_km, tot_vert = mp.totals(w)
    return {
        "date": d.isoformat(),
        "dow": mp.DOW[i],
        "week": w["no"],
        "phase": w["phase"],
        "focus": w["focus"],
        "kind": kind,
        "title": title,
        "abbr": main,
        "sub": sub,
        "tip": tip,
        "minutes": minutes,
        "dur": _hhmm(minutes),
        "km": km,
        "vert": int(vert),
        "hr": hr,
        "note": note or "",
        "week_km": round(tot_km),
        "week_vert": int(tot_vert),
        "is_race": kind == "race",
        "is_rest": kind == "rest",
    }


def week_rows(d: dt.date) -> list[dict]:
    """当天所在周的 7 天精简信息，供看板渲染。"""
    w, _ = week_of(d)
    if w is None:
        return []
    s = span()[0]
    out = []
    for i in range(7):
        day = s + dt.timedelta(days=(w["no"] - 1) * 7 + i)
        info = day_info(day) or {}
        out.append({
            "date": day.isoformat(),
            "dow": _mp().DOW[i],
            "main": info.get("abbr", "休"),
            "sub": info.get("sub", ""),
            "kind": info.get("kind", "rest"),
            # km / minutes 给看板做「计划 vs 实际」对照，缺了就只能显示文字
            "km": info.get("km") or 0,
            "minutes": info.get("minutes") or 0,
        })
    return out


def format_day(info: dict) -> str:
    """给 CLI / 定时任务看的纯文本。"""
    if not info:
        return "今天不在 105K 备赛计划区间内。"
    head = (f"{info['date']} {info['dow']} · W{info['week']} {info['phase']}期"
            f"　（本周 {info['week_km']} km / +{info['week_vert']:,} m）")
    body = f"今日课次：{info['abbr']}"
    if info["sub"]:
        body += f"  {info['sub']}"
    if not info["is_rest"]:
        body += f"　约 {info['dur']}"
    if info["hr"] and info["hr"] != "—":
        body += f"　心率 {info['hr']}"
    lines = [head, body]
    if info["tip"]:
        lines.append(f"要点：{info['tip']}")
    if info["note"]:
        lines.append(f"说明：{info['note']}")
    lines.append(f"本周主题：{info['focus']}")
    return "\n".join(lines)


def format_week(d: dt.date) -> str:
    rows = week_rows(d)
    if not rows:
        return "该日期不在 105K 备赛计划区间内。"
    info0 = day_info(d) or {}
    head = f"W{info0.get('week')} {info0.get('phase')}期 · 本周 {info0.get('week_km')} km / +{info0.get('week_vert'):,} m"
    out = [head, f"主题：{info0.get('focus')}", ""]
    for r in rows:
        mark = " ←" if r["date"] == d.isoformat() else ""
        seg = f"{r['dow']} {r['date'][5:]}  {r['main']}"
        if r["sub"]:
            seg += f"  {r['sub']}"
        out.append(seg + mark)
    return "\n".join(out)


def upcoming(d: dt.date, days: int = 3) -> list[dict]:
    """含当天在内的未来 N 天课次。"""
    out = []
    for k in range(max(1, days)):
        info = day_info(d + dt.timedelta(days=k))
        if info:
            out.append(info)
    return out
