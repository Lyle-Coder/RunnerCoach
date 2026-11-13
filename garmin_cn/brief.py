"""把一次活动压成「手机上能一屏读完」的短消息，供微信 / 企业微信 / 短信推送。

为什么不复用 actcard 的 HTML
---------------------------
微信里发出去的是**纯文本 + 一张图**：`#`、`**`、`|` 这些 markdown 记号不会被渲染，
表格会退化成一行挤在一起的符号。所以这里不复用版式，只复用语料（配速、时长、
主要益处、分区）——版式重写成「一句话一行」，粘到任何聊天窗口都不变形。

判断比数字更重要
----------------
看板上一堆指标，微信里没人会去算。所以这里算出两类结论再落到文字：

1. **配速漂移**：前后半程配速差。超 2% 才算「掉速」，避免 GPS 抖动被夸大。
2. **心率漂移**：后半程心率是否反升。掉速 + 心率升 = 疲劳或起步过快；
   加速 + 心率稳 = 状态好。这是同一条数据里唯一能直接指导下一次训练的结论。

两项都没有（单圈、短距离、GPS 断点太多）时，退回配速稳定性（分圈 CV）。
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from .actcard import WEATHER_CN, benefit, dur_txt, kmh, pace_txt, theme_for
from .metrics import num

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "basemaps"

# 时段称呼：只分四档，多了反而要查表
_SLOT = ((5, "凌晨"), (9, "晨"), (11, "上午"), (13, "中午"),
         (17, "下午"), (19, "傍晚"), (23, "夜间"))

# 活动名里常带「跑步」「骑行」后缀，拼成「晨跑」「夜骑」时只留一个字；
# 两个字以上的（力量、游泳）改成「时段 · 项目」，否则「夜间力量」读着像地名。
_SHORT = {"跑步": "跑", "跑步机": "跑步机", "越野跑": "越野跑", "跑道跑": "跑道跑",
          "健走": "健走", "徒步": "徒步", "骑行": "骑", "公路骑行": "骑",
          "山地骑行": "骑", "室内骑行": "骑", "力量": "力量", "游泳": "游泳",
          "泳池游泳": "游泳"}


def _slot(ts: str) -> str:
    """从开始时间取时段前缀（`2026-11-02 18:42:00` → 「傍晚」）。"""
    try:
        h = int(ts[11:13])
    except Exception:
        return ""
    for cut, name in _SLOT:
        if h < cut:
            return name
    return "夜间"


def region(activity_id) -> str:
    """底图缓存里存过的「省·市」。读缓存，不联网（没底图就返回空）。"""
    try:
        import json
        for f in CACHE.glob(f"{activity_id}-*.json"):
            got = json.loads(f.read_text("utf-8")).get("region")
            if got:
                return str(got)
    except Exception:
        pass
    return ""


def _heading(row: dict) -> str:
    """第一行：日期 + 周几 + 时段 + 运动名（「晨跑」「傍晚骑」「夜间 · 力量」）。"""
    date = (row.get("date") or "")[5:]
    sport = theme_for(row.get("type"))["tag"]
    word = _SHORT.get(sport, sport)
    slot = _slot(row.get("start_ts") or "")
    try:
        wd = " 周" + "一二三四五六日"[dt.date.fromisoformat(row.get("date") or "").weekday()]
    except Exception:
        wd = ""
    if len(word) <= 1:
        tail = f"{slot}{word}"
    else:
        tail = f"{slot} · {word}" if slot else word
    return f"{date}{wd} {tail}".strip()


def _weather(w: dict) -> str:
    if not w:
        return ""
    bits = []
    if w.get("temp_c") is not None:
        bits.append(f"{w['temp_c']:.0f}℃")
    if w.get("humidity") is not None:
        bits.append(f"湿度 {w['humidity']}%")
    if w.get("text"):
        txt = str(w["text"])
        bits.append(WEATHER_CN.get(txt, txt))
    return " ".join(bits)


def verdict(row: dict, detail: dict) -> str:
    """一句话结论：这次跑得怎么样。没有足够数据就返回空串。"""
    s = detail.get("summary") or {}
    d = s.get("drift") or {}
    p1, p2 = num(d.get("pace_first")), num(d.get("pace_second"))
    h1, h2 = num(d.get("hr_first")), num(d.get("hr_second"))
    if p1 and p2 and p1 > 0:
        gap = p2 - p1                      # 正 = 后半程慢
        hpct = gap / p1
        dh = (h2 - h1) if (h1 and h2) else None
        if hpct > 0.02:
            tail = f"、心率还升了 {dh:.0f}" if dh and dh >= 4 else ""
            why = "起步偏快或耐力吃紧" if dh and dh >= 4 else "起步可以再稳一点"
            return f"后半程掉速 {abs(gap):.0f}″{tail}，{why}"
        if hpct < -0.02:
            tail = "、心率没漂" if dh is not None and dh <= 5 else ""
            return f"后半程快了 {abs(gap):.0f}″{tail}，状态不错"
        tail = f"，心率却升了 {dh:.0f}" if dh and dh >= 6 else ""
        return f"上下半程几乎同速{tail}" + ("，注意有氧底子还不够厚" if dh and dh >= 6 else "，节奏很稳")

    # 没有前后半程（单圈 / 短距离）：退回分圈稳定度
    paces = [num(l.get("pace_s")) for l in (detail.get("laps") or [])]
    paces = [p for p in paces if p and p > 0]
    if len(paces) >= 3:
        avg = sum(paces) / len(paces)
        sd = (sum((p - avg) ** 2 for p in paces) / len(paces)) ** 0.5
        cv = sd / avg if avg else 0
        if cv < 0.03:
            return "每公里配速几乎一样，控制得很好"
        if cv > 0.09:
            return f"分圈快慢差得多（最快 {pace_txt(min(paces))}、最慢 {pace_txt(max(paces))}）"
    return ""


def _main(row: dict) -> str:
    """第二行：主指标 + 距离 + 用时。按运动类型换口径，不硬套配速。"""
    th = theme_for(row.get("type"))
    dist = (num(row.get("distance_m")) or 0) / 1000
    dur = num(row.get("duration_s")) or 0
    from .sports import pace_seconds
    pace = pace_seconds(row)
    kind = th["kind"]

    if kind == "duration":
        return f"{dur_txt(dur)} ｜ {num(row.get('calories')) or 0:.0f} 千卡"
    bits = []
    if dist:
        bits.append(f"{dist:.2f} km")
    if dur:
        bits.append(dur_txt(dur))
    if kind == "speed":
        bits.append(f"{kmh(pace):.1f} km/h" if kmh(pace) else "—")
    elif kind == "pace100":
        bits.append(pace_txt(dur / (dist * 10)) if dist else "—")
    elif pace:
        bits.append(f"{pace_txt(pace)}/km")
    return " ｜ ".join(bits)


def _numbers(row: dict, detail: dict) -> str:
    """第三行：心率 / 步频 / 步幅；没有步频数据（骑行、力量）换成速度与负荷。"""
    import json

    s = detail.get("summary") or {}
    hr = s.get("hr_run_avg") or num(row.get("avg_hr"))
    cad = num(s.get("cad_avg"))
    st = num(s.get("stride_avg"))
    from .sports import family
    if family(row.get("type")) == "bike":
        from .cycling import for_detail
        cm = for_detail(detail)
        bits = [f"心率 {hr:.0f}" if hr else "", f"踏频 {cad:.0f} rpm" if cad is not None else "",
                f"均功率 {cm['avg_power_w']:.0f} W" if cm.get("avg_power_w") is not None else "",
                f"NP {cm['normalized_power_w']:.0f} W" if cm.get("normalized_power_w") is not None else ""]
    elif cad or st:
        bits = [f"心率 {hr:.0f}" if hr else "", f"步频 {cad:.0f}" if cad else "",
                f"步幅 {st:.0f}" if st else ""]
    else:
        raw = row.get("raw") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        mx = num((raw or {}).get("maxSpeed"))
        load = num(row.get("load"))
        bits = [f"心率 {hr:.0f}" if hr else "", f"最高 {mx * 3.6:.1f} km/h" if mx else "",
                f"负荷 {load:.0f}" if load else ""]
    return " ｜ ".join(b for b in bits if b)


def _tags(row: dict) -> str:
    """第四行：爬升 + 主要益处 + 有氧/无氧 TE。"""
    asc = num(row.get("ascent")) or 0
    raw = row.get("raw") or {}
    if isinstance(raw, str):
        import json
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    raw = raw or {}
    te = f"{num(row.get('aerobic_te')) or 0:.1f}"
    ana = f"{num(row.get('anaerobic_te')) or 0:.1f}"
    bits = []
    if asc:
        bits.append(f"爬升 {asc:.0f} m")
    bits.append(f"主要益处 {benefit(raw)}")
    bits.append(f"有氧 {te}" + (f" / 无氧 {ana}" if (num(row.get("anaerobic_te")) or 0) >= 0.1 else ""))
    return " ｜ ".join(bits)


def one(row: dict, detail: dict, *, place: str = "") -> str:
    """一次活动 → 一段可直接发进微信的纯文本。

    `place` 传空时自动读底图缓存里的省市。图片**不写进正文**：正文是给人看的，
    一个文件名对它没意义；图由调用方单独附上（`--json` 的 `image` 字段给机器人用）。
    """
    row = {**row, **(detail.get("row_metrics") or {})}
    lines = []
    head = _heading(row)
    loc = place if place else region(row.get("activity_id"))
    if loc:
        head += f" · {loc}"
    lines.append(head)
    lines.append(_main(row))
    nm = _numbers(row, detail)
    if nm:
        lines.append(nm)
    tg = _tags(row)
    if tg:
        lines.append(tg)
    v = verdict(row, detail)
    if v:
        lines.append(v)
    wx = _weather(detail.get("weather") or {})
    if wx:
        lines.append(wx)
    return "\n".join(lines)


def multi(pairs: list[tuple[dict, dict]], *, limit: int = 5) -> str:
    """多次活动 → 一段汇总。用于「最近跑了哪些」这种一次问一批的场景。"""
    out = []
    for row, detail in pairs[-limit:]:
        out.append(one(row, detail))
    return ("\n\n" + "-" * 12 + "\n\n").join(out)
