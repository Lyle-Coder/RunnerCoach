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


def pick(seed: str | None = None) -> dict | None:
    """一图流用：随机取一条。

    用户要求「每次随机」——不按日期固定，导两次不一样也算正常。
    传 seed 时变成确定性（selftest 用）。
    """
    pool = _items()
    if not pool:
        return None
    if seed is not None:
        return pool[sum(ord(c) for c in str(seed)) % len(pool)]
    return random.choice(pool)


def items(limit: int = 8, seed: str | None = None) -> list[dict]:
    """首页轮播用：取若干条，尽量不重复（同一 id 只出现一次）。"""
    pool = _items()
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
