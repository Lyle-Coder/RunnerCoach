# -*- coding: utf-8 -*-
"""延伸阅读：一图流右下角一行、首页底部一张卡。

内容不放数据库，放 JSON：这些是「人工校对的说明文字」，不是用户数据。
两个位置：
  · garmin_cn/content/   随包发布，装好就有
  · data/content/        可选覆盖，改文案不用发版

设计约束（见 docs/延伸阅读设计.md）：
- 纯静态、不联网、不调模型：报告/卡片生成不能被它拖慢；
- 任何异常都返回空，绝不把出图搞挂；
- 一图流空间紧，只给一句 brief；首页给整条 + 数据源。
"""
from __future__ import annotations

import datetime as dt
import json
import random
from pathlib import Path

PKG = Path(__file__).resolve().parent
BUNDLED = PKG / "content"
OVERRIDE = PKG.parent / "data" / "content"

FILES = ("terms.json", "quotes.json")
RACES = "races.json"


def _read(name: str) -> dict:
    """读一个内容文件：data/content 优先，其次包内。坏文件直接跳过。"""
    for d in (OVERRIDE, BUNDLED):
        p = d / name
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data
    return {}


def _items() -> list[dict]:
    """所有可展示条目，统一成 {id, text, src}。

    terms 用 brief（一图流放不下 body）；quotes 用 text。两者都强制要 source，
    没有出处的一律丢掉——这类内容最容易「看着像真的」。
    """
    out = []
    for name in FILES:
        data = _read(name)
        for it in data.get("items") or []:
            if not isinstance(it, dict):
                continue
            if name == "terms.json":
                text, src = it.get("brief"), it.get("source")
                head, rel = it.get("term") or "", it.get("rel") or []
            else:
                text, src = it.get("text"), it.get("source")
                head, rel = it.get("who") or "", []
            text, src = str(text or "").strip(), str(src or "").strip()
            if not text or not src or not head:
                continue
            out.append({"id": str(it.get("id") or text[:8]), "head": str(head),
                        "text": text, "src": src, "rel": rel})
    return out


def _race_of(it: dict, today: dt.date) -> dict | None:
    """一条赛事 → 展示条目；**只认报名窗口内的**，其余一律返回 None。

    刻意不做「报名未开始 / 已截止」的灰条：这类卡片出现一次就会让人误以为
    还能报，宁可少显示一场。窗口字段缺失或解析失败同样不显示。
    """
    try:
        reg_open = dt.date.fromisoformat(str(it.get("reg_open") or "")[:10])
        reg_close = dt.date.fromisoformat(str(it.get("reg_close") or "")[:10])
        race_day = dt.date.fromisoformat(str(it.get("date") or "")[:10])
    except (TypeError, ValueError):
        return None
    if not (reg_open <= today <= reg_close):
        return None
    name = str(it.get("name") or "").strip()
    if not name:
        return None
    left = (reg_close - today).days
    bits = [race_day.strftime("%m月%d日")]
    try:
        if it.get("dist_km"):
            bits.append(f"{float(it['dist_km']):g} km")
    except (TypeError, ValueError):
        pass
    try:
        if it.get("ascent_m"):
            bits.append(f"爬升 {int(float(it['ascent_m']))} m")
    except (TypeError, ValueError):
        pass
    if it.get("place"):
        bits.append(str(it["place"]).strip())
    text = " · ".join(b for b in bits if b)
    if str(it.get("note") or "").strip():
        text += "；" + str(it["note"]).strip()
    return {"id": "race-" + str(it.get("id") or name[:8]), "head": name,
            "text": f"报名剩 {left} 天 · {text}",
            "src": str(it.get("source") or "赛事官方").strip(),
            "rel": ["race"], "kind": "race", "_close": reg_close.isoformat()}


def races(today: dt.date | None = None) -> list[dict]:
    """报名窗口内的赛事（按截止日升序）。窗口外的、字段不全的都不返回。"""
    today = today or dt.date.today()
    out = []
    for it in (_read(RACES).get("items") or []):
        if not isinstance(it, dict):
            continue
        r = _race_of(it, today)
        if r:
            out.append(r)
    out.sort(key=lambda x: x.get("_close") or "")
    return out


def pick(seed: str | None = None) -> dict | None:
    """一图流用：随机取一条。

    用户要求「每次随机」——不按日期固定，导两次不一样也算正常。
    传 seed 时变成确定性（selftest 用）。
    """
    pool = _items() + races()
    if not pool:
        return None
    if seed is not None:
        return pool[sum(ord(c) for c in str(seed)) % len(pool)]
    return random.choice(pool)


def items(limit: int = 8, seed: str | None = None) -> list[dict]:
    """首页轮播用：取若干条，尽量不重复（同一 id 只出现一次）。"""
    pool = _items() + races()
    if not pool:
        return []
    if seed is not None:
        pool = list(pool)
        rnd = random.Random(seed)
        rnd.shuffle(pool)
    else:
        random.shuffle(pool)
    out, seen = [], set()
    for it in pool:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        out.append(it)
        if len(out) >= limit:
            break
    return out


def validate() -> list[str]:
    """给 selftest 用的结构检查，返回问题列表（空表示合格）。"""
    bad = []
    # 赛事：报名窗口必须能解析、且顺序合理；截止日不能晚于比赛日
    try:
        _rp = json.loads((BUNDLED / RACES).read_text(encoding="utf-8")) if (BUNDLED / RACES).is_file() else {}
    except (OSError, ValueError) as e:
        _rp = {}
        bad.append(f"{RACES}: 读不出来 ({e})")
    _ids = set()
    for i, it in enumerate(_rp.get("items") or []):
        if not isinstance(it, dict):
            bad.append(f"{RACES}[{i}]: 不是对象")
            continue
        iid = str(it.get("id") or "")
        if not iid:
            bad.append(f"{RACES}[{i}]: 缺 id")
        elif iid in _ids:
            bad.append(f"{RACES}[{i}]: id 重复 {iid}")
        _ids.add(iid)
        try:
            _o = dt.date.fromisoformat(str(it.get("reg_open") or "")[:10])
            _c = dt.date.fromisoformat(str(it.get("reg_close") or "")[:10])
            _d = dt.date.fromisoformat(str(it.get("date") or "")[:10])
        except ValueError:
            bad.append(f"{RACES}[{i}]: 日期不可解析（date/reg_open/reg_close 都要 YYYY-MM-DD）")
            continue
        if _c < _o:
            bad.append(f"{RACES}[{i}]: 报名截止早于报名开始")
        if _d < _c:
            bad.append(f"{RACES}[{i}]: 比赛日早于报名截止（多半填反了）")
    for name in FILES:
        p = BUNDLED / name
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            bad.append(f"{name}: 读不出来 ({e})")
            continue
        if not isinstance(data.get("items"), list):
            bad.append(f"{name}: 缺 items 数组")
            continue
        try:
            dt.date.fromisoformat(str(data.get("updated") or ""))
        except ValueError:
            bad.append(f"{name}: updated 不是 YYYY-MM-DD")
        seen = set()
        for i, it in enumerate(data["items"]):
            if not isinstance(it, dict):
                bad.append(f"{name}[{i}]: 不是对象")
                continue
            iid = str(it.get("id") or "")
            if not iid:
                bad.append(f"{name}[{i}]: 缺 id")
            elif iid in seen:
                bad.append(f"{name}[{i}]: id 重复 {iid}")
            seen.add(iid)
            if not str(it.get("source") or "").strip():
                bad.append(f"{name}[{i}]: 缺 source（没法追溯出处）")
            if name == "terms.json":
                brief = str(it.get("brief") or "").strip()
                if not (10 <= len(brief) <= 60):
                    bad.append(f"{name}[{i}]: brief 长度 {len(brief)} 不在 10–60")
                body = str(it.get("body") or "").strip()
                if len(body) < 60:
                    bad.append(f"{name}[{i}]: body 太短")
            else:
                if not str(it.get("who") or "").strip():
                    bad.append(f"{name}[{i}]: 名言缺作者")
                if len(str(it.get("text") or "")) > 120:
                    bad.append(f"{name}[{i}]: 名言太长")
    return bad
