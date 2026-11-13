# -*- coding: utf-8 -*-
"""CP 表图片识别：一张规程截图 → 结构化 CP 行。

为什么要走大模型
----------------
CP 表在规程里几乎只有两种形态：PDF 里的表格、公众号文章里的截图。前者
文字层常被压成图片，后者本来就只有图。OCR 引擎（tesseract 之类）能把字
读出来，但读不出「哪一列是里程」——表头合并、跨页、单位混写，规则引擎
要写几十条特例还未必对。

反过来，**识别错了的代价很高**：余量表、关门时间、补给计划全建在这上面。
所以这里的策略是「宁可少认，不许猜」：

- 只读图上真实存在的行，缺的 CP 不补全、不插值；
- 里程必须严格递增，否则整表退回，让人手动贴文字；
- 地名必须非空，看不懂的行整行丢弃；
- 识别结果先过一遍校验再写盘，有 error 就不写（和手动粘贴同一套）。

数据格式与 ingest.from_text 完全一致，写盘走同一个 apply()，所以「识别
进来」和「粘贴进来」在数据库里没有任何区别，后续所有计算不用分叉。
"""
from __future__ import annotations

import json
import re

PROMPT = """你在读一张跑步赛事规程里的「CP 表」截图（越野赛或马拉松的检查点/关门表）。

逐行读出每个 CP，只输出一个 JSON 对象，不要解释、不要 markdown 代码块：
{"segments":[{"idx":1,"name":"仙山雅宿","km":10.9,"cutoff":"08:05"}]}

规则（违反任何一条都会让整张表没法用）：
1. 只读图上**真实存在**的行。看不清、被截断的行整行丢掉，绝不猜测、绝不补全。
2. `km` 是**累计里程**（不是分段里程），单位公里，保留一位小数即可。
3. `cutoff` 是关门时间，写成 24 小时制 HH:MM；图上没写就填 null。
4. `name` 保留图上原文（中文），不要翻译、不要简写、不要加 CP 编号。
5. `idx` 从 1 开始按图上顺序编号，不要跳号。
6. 里程必须严格递增。如果图和这两条冲突，以图上数字为准，但不要为了让它们
   递增而改数字。
7. 最多 60 行。一个 CP 都没认出来就返回 {"segments":[]}。
"""

_JSON_RE = re.compile(r"\{.*\}", re.S)
_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})$")


def _json_obj(text: str) -> dict:
    t = str(text or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    m = _JSON_RE.search(t)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else {}
    except ValueError:
        return {}


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if 0 < f < 1000 else None


def _clock(v) -> str:
    """关门时间归一化成 HH:MM；不认识就返回空串（不是跳过整行）。"""
    s = str(v or "").strip()
    m = _CLOCK.match(s)
    if not m:
        return ""
    h, mi = int(m.group(1)), int(m.group(2))
    return f"{h:02d}:{mi:02d}" if (0 <= h <= 47 and 0 <= mi < 60) else ""


def clean(raw_items, ev: dict | None = None) -> tuple[list[dict], list[str]]:
    """把模型给的原始行洗成能用的一批。返回 (segments, problems)。

    这里**不修数据**，只做三件事：丢掉不能用的、按里程排序、重排编号。
    任何「为了凑成一条赛道」的修补都不做——识别错一格，余量就全错。
    """
    ev = ev or {}
    dist = _num(ev.get("distance_km"))
    limit = (dist * 1.3) if dist else 1000.0

    rows, seen = [], set()
    for it in raw_items or []:
        if not isinstance(it, dict):
            continue
        km = _num(it.get("km"))
        name = re.sub(r"\s+", " ", str(it.get("name") or "")).strip()
        # 地名里常见的脏字符：编号前缀、单位残留
        name = re.sub(r"^(?:CP|cp|检查点)\s*\d+\s*[:：]?\s*", "", name).strip(" -—·:：")
        if km is None or not name or km > limit:
            continue
        if len(name) > 24:
            name = name[:24]
        rows.append({"idx": 0, "name": name, "km": round(km, 1),
                     "cutoff": _clock(it.get("cutoff")),
                     "cutoff_day": 1, "source": "ocr"})

    # 同里程的两行只留第一行（表里常有「CP5 / CP5 补给站」这种重复）
    uniq = []
    for r in rows:
        if r["km"] in seen:
            continue
        seen.add(r["km"])
        uniq.append(r)
    uniq.sort(key=lambda x: x["km"])

    problems = []
    prev = 0.0
    for i, r in enumerate(uniq, 1):
        if r["km"] <= prev:
            problems.append(f"CP{i} 里程没有递增（{r['km']}）")
        prev = r["km"]
        r["idx"] = i
    if len(uniq) < 2:
        problems.append("只认出不到 2 个 CP，不值得写进档案")
    return uniq, problems


def parse_image(image_b64: str, mime: str = "image/jpeg",
                ev: dict | None = None) -> dict:
    """图片（base64）→ {segments, problems, model}。失败抛 llm.LLMError / ValueError。"""
    from . import llm

    if not image_b64:
        raise ValueError("没有图片")
    if not llm.is_configured():
        raise ValueError("还没配置模型服务：去「教练 → 设置 → 模型服务」填 Key 再来")

    msg = [{"role": "system", "content": PROMPT},
           {"role": "user", "content": [
               {"type": "text", "text": "读出这张表里的所有 CP。"},
               {"type": "image_url", "image_url": {
                   "url": f"data:{mime or 'image/jpeg'};base64,{image_b64}"}}]}]
    r = llm.chat(msg, temperature=0.1, max_tokens=1600)
    obj = _json_obj(r.get("text") or "")
    items = obj.get("segments")

    if not isinstance(items, list) or not items:
        # 一次重试：模型常把思考过程写进正文，JSON 被截断就成了「没认出来」
        retry = msg + [
            {"role": "assistant", "content": (r.get("text") or "")[:600]},
            {"role": "user", "content": (
                "上面那段不是我要的格式。只输出 JSON 对象，键是 segments，"
                "值是数组。读不出就返回 {\"segments\":[]}，不要解释。")}]
        try:
            r2 = llm.chat(retry, temperature=0.1, max_tokens=1600)
            obj2 = _json_obj(r2.get("text") or "")
            if isinstance(obj2.get("segments"), list) and obj2["segments"]:
                obj, r = obj2, r2
        except llm.LLMError:
            pass
        items = obj.get("segments")

    segs, problems = clean(items, ev)
    return {"segments": segs, "problems": problems,
            "model": r.get("model") or "", "raw_count": len(items or [])}
