# -*- coding: utf-8 -*-
"""赛事档案与关门余量（教练模块 · P1/P2/P3）。

数据文件全部放在 data/events/<slug>/ 下：
  event.json   赛事档案（名称/日期/组别/目标/状态）
  course.json  赛道分段（CP 表 + 关门 + 特征注释），驱动余量表/教练卡/3D
  gear.md      装备清单（Markdown checklist，`- [x]` 表示已备）
  track.gpx    官方轨迹（可选，到手后 `event import-gpx` 解析）

数据来源分级（source 字段）：
  poster-2027  2027 官方赛道海报（用户提供的权威来源，CP/关门以此为准）
  ref-2026     2026 届官方赛道图（地形与分段特征参考）
  manual       手填
  gpx          由 GPX 轨迹解析得到

关门余量算法（C 类纯函数，不落库）：
  等效距离 = 段距离 + 段爬升/100（Naismith 近似），
  计划到达 = 起跑时刻 + 目标总用时 × 累计等效占比；余量 = 关门 - 计划到达。
"""
from __future__ import annotations

import datetime as dt
import io
import json
import math
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

# 数据目录：桌面用项目根；APK 里由 Kotlin 侧用 GARMIN_HOME 指到 filesDir
# （APK 内的 garmin_cn/ 在 assets 里只读，绝不能往那儿写）。
HOME = (Path(os.environ["GARMIN_HOME"]).expanduser()
        if os.environ.get("GARMIN_HOME") else Path(__file__).resolve().parent.parent)
EVENTS = HOME / "data" / "events"

# ---------------------------------------------------------------- 档案种子

SEED_EVENT = {
    "slug": "panda-105k-2027",
    "name": "2027 熊猫蜀道山超级越野赛 · 105K",
    "org": "柴古唐斯 TSAIGU",
    "status": "confirmed",          # announced / registered / confirmed / done
    "date": "2027-03-27",
    "start_time": "06:00",
    "cutoff_time": "2027-03-28 11:00",
    "duration_h": 29,
    "distance_km": 103.3,
    "ascent_m": 5547,
    "descent_m": 5547,
    "venue": "白鹿音乐广场（起点 = 终点，四川成都彭州）",
    "itra": 5,
    "goal_s": 22 * 3600,            # 目标完赛 22 h（可改 20–24 h）
    "source": "poster-2027",
}

# 2027 官方海报 CP 表（用户 2026-11-11 提供的赛道图海报，逐卡 OCR 校对）
SEED_SEGMENTS = [
    dict(idx=1,  name="仙山雅宿",       en="XianShan YaShu",              km=10.9,  dplus=515, dminus=418, cutoff="08:05", day=1),
    dict(idx=2,  name="冰川瀑布景区",   en="Glacier Fall",                km=22.3,  dplus=748, dminus=1125, cutoff="10:10", day=1),
    dict(idx=3,  name="准提庵",         en="Cundi Nunnery",               km=34.5,  dplus=759, dminus=570,  cutoff="12:45", day=1),
    dict(idx=4,  name="鲁班庙",         en="LuBan Temple",                km=42.5,  dplus=500, dminus=610,  cutoff="14:35", day=1),
    dict(idx=5,  name="海窝子古镇",     en="HaiWoZi Ancient Town",        km=50.6,  dplus=454, dminus=410,  cutoff="17:30", day=1, dropbag=True),
    dict(idx=6,  name="渔江楠游客中心", en="YuJiangNan Tourist Center",   km=62.5,  dplus=577, dminus=441,  cutoff="19:55", day=1),
    dict(idx=7,  name="庙子坪",         en="MiaoZiPing",                  km=69.2,  dplus=544, dminus=302,  cutoff="21:45", day=1),
    dict(idx=8,  name="朝天路梁子",     en="ChaoTianLu Ridge",            km=80.9,  dplus=514, dminus=511,  cutoff="00:45", day=2),
    dict(idx=9,  name="骆家山林场",     en="LuoJiaShan Forestry Farm",    km=91.6,  dplus=743, dminus=281,  cutoff="06:40", day=2),
    dict(idx=10, name="白鹿上书院",     en="Seminarium Annuntiationis",   km=99.5,  dplus=179, dminus=794,  cutoff="09:30", day=2),
    dict(idx=11, name="白鹿音乐广场",   en="BaiLu Music Plaza (FINISH)",  km=103.3, dplus=14,  dminus=85,   cutoff="11:00", day=2, finish=True),
]

# 分段特征注释：poster + 2026 届参考（永定桥/涌华寺段在 2027 改为准提庵/鲁班庙）
SEED_FEATURES = {
    0: ["起跑 06:00 天未亮", "头灯 + 保暖层", "夜爬 515 m"],
    1: ["大下降 1125 m", "护膝 / 下坡技术段"],
    2: ["连续爬升段", "2026 届此段走永定桥河谷"],
    3: ["爬升 500 m 后速降 610 m", "技术下坡"],
    4: ["换装点：存包 / 换鞋换衣", "半程过半，补给拉满 20 min 内"],
    5: ["河谷平缓段", "可以跑起来，调整节奏"],
    6: ["进入夜程前最后一个短站", "天黑前检查头灯电量"],
    7: ["夜程开始", "2026 届关门 21:45 → 2027 提前 2 h，节奏要紧"],
    8: ["全程最长夜爬之一", "0:45 关门后进入山脊夜风区，保暖"],
    9: ["后半夜长爬升 743 m", "黎明前最冷，防失温"],
    10: ["长下降 794 m 到书院", "最后 3.8 km 冲刺"],
}

SEED_GEAR = """# 强制装备清单（105K）

> 模板来源：柴古唐斯规程通用强制装备 + 2026 届海报补给图例。
> 勾选格式：`- [x]` 已备、`- [ ]` 未备。赛前 7 天（3-20）未勾满会进看板「需要注意」。

- [ ] 主头灯 + 备用头灯（含备用电池）
- [ ] 保温毯 / 应急保温层
- [ ] 急救包（含弹性绷带、消毒片、创可贴）
- [ ] 水袋 / 水壶 ≥ 1.5 L
- [ ] 手机 + 充电宝（≥10000 mAh）
- [ ] 防风防水外套（硬壳）
- [ ] 保暖中层（抓绒）
- [ ] 长裤 / 压缩腿套
- [ ] 帽子 + 手套
- [ ] 口哨 + 救生哨
- [ ] 越野跑鞋（已磨合的战靴 + 备用鞋进换装包）
- [ ] 换装包（CP5 海窝子：备用鞋 / 袜 / 上衣 / 电解质）
- [ ] 能量胶 / 固体补给（按 60–90 g 碳水 / 小时 配 12 h 夜程）
- [ ] 电解质丸 / 盐丸
- [ ] 现金 / 交通卡 + 号码布带
"""


# ---------------------------------------------------------------- 文件读写

def _dir(slug: str) -> Path:
    return EVENTS / slug


def _read(slug: str, name: str) -> dict:
    p = _dir(slug) / name
    if not p.exists():
        return {}
    return json.loads(io.open(p, encoding="utf-8").read())


def _write(slug: str, name: str, data: dict) -> Path:
    d = _dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    io.open(p, "w", encoding="utf-8").write(
        json.dumps(data, ensure_ascii=False, indent=1))
    return p


def load_event(slug: str | None = None) -> dict:
    slug = slug or SEED_EVENT["slug"]
    ev = _read(slug, "event.json")
    if not ev:
        ev = dict(SEED_EVENT)
        ev["slug"] = slug
        _write(slug, "event.json", ev)
        seed_gear(slug)
    return ev


def load_course(slug: str | None = None) -> dict:
    slug = slug or SEED_EVENT["slug"]
    c = _read(slug, "course.json")
    if not c:
        c = _build_seed_course(slug)
        _write(slug, "course.json", c)
    return c


def save_course(slug: str, course: dict) -> Path:
    return _write(slug, "course.json", course)


def seed_gear(slug: str | None = None) -> Path:
    slug = slug or SEED_EVENT["slug"]
    p = _dir(slug) / "gear.md"
    if not p.exists():
        _dir(slug).mkdir(parents=True, exist_ok=True)
        io.open(p, "w", encoding="utf-8").write(SEED_GEAR)
    return p


def _build_seed_course(slug: str) -> dict:
    segs = []
    for i, s in enumerate(SEED_SEGMENTS):
        seg = dict(
            idx=s["idx"],
            name=s["name"], en=s.get("en", ""),
            km=s["km"], dplus=s["dplus"], dminus=s["dminus"],
            cutoff=s["cutoff"], cutoff_day=s["day"],
            dropbag=bool(s.get("dropbag")), finish=bool(s.get("finish")),
            features=SEED_FEATURES.get(i, []),
            source="poster-2027",
        )
        segs.append(seg)
    return {
        "event": slug,
        "distance_km": SEED_EVENT["distance_km"],
        "ascent_m": SEED_EVENT["ascent_m"],
        "start": {"time": SEED_EVENT["start_time"], "date": SEED_EVENT["date"],
                  "venue": "白鹿音乐广场"},
        "reference": {
            "edition_2026": {
                "distance_km": 103.8, "ascent_m": 5611,
                "note": "2026 届同走廊：CP3 永定桥 / CP5 涌华寺 / CP6 熊猫运动公园，"
                        "2027 改为准提庵 / 鲁班庙直连；后半程庙子坪—白鹿上书院一致",
                "source": "ref-2026",
            }
        },
        "segments": segs,
    }


# ---------------------------------------------------------------- 余量计算

def _cutoff_dt(ev: dict, seg: dict) -> dt.datetime:
    d = dt.date.fromisoformat(ev["date"]) + dt.timedelta(days=seg["cutoff_day"] - 1)
    h, m = map(int, seg["cutoff"].split(":"))
    return dt.datetime(d.year, d.month, d.day, h, m)


def _equiv(seg_a: float, seg_b: float, dp_a: float, dp_b: float) -> float:
    """等效距离 = 距离 + 爬升/100（两 CP 之间）。"""
    return (seg_b - seg_a) + max(0.0, dp_b - dp_a) / 100.0


def plan_and_margin(slug: str | None = None, goal_s: int | None = None) -> list[dict]:
    """每个 CP：关门时刻 / 计划到达 / 余量。余量为负 = 该方案到不了。

    分段用时 = 段等效距离 × 策略系数，两遍近似：
      第 1 遍按基础权重估到达时刻 → 判断哪些段落在夜间窗口；
      第 2 遍对夜段加 night_factor、对前 45 km（体力新鲜的可跑段）打 early_factor，
      再整体归一到目标总用时。
    """
    ev = load_event(slug)
    course = load_course(slug)
    goal_s = goal_s or ev.get("goal_s") or 22 * 3600
    st = ev.get("strategy") or {}
    early_km = st.get("early_km", 45)
    early_f = st.get("early_factor", 0.85)
    night_f = st.get("night_factor", 1.15)
    n_win = st.get("night_window", ["21:30", "06:30"])
    segs = course["segments"]

    # 段等效距离 = 段里程 + 该段爬升/100（course 里的 dplus 就是本段爬升，非累计）
    ws = []
    for a, b in zip([dict(km=0)] + segs[:-1], segs):
        ws.append((b["km"] - a["km"]) + (b.get("dplus") or 0) / 100.0)
    total_eq = sum(ws) or 1e-6

    start = dt.datetime.combine(dt.date.fromisoformat(ev["date"]),
                                dt.time(*map(int, ev["start_time"].split(":"))))
    night = [dt.time(*map(int, t.split(":"))) for t in n_win]
    in_night = lambda t: (night[0] <= t or t <= night[1]) if night[0] > night[1] \
        else night[0] <= t <= night[1]

    def factor(km_end: float, arrive_hint: dt.datetime) -> float:
        f = 1.0
        if km_end <= early_km:
            f *= early_f
        if in_night(arrive_hint.time()):
            f *= night_f
        return f

    # 第 1 遍：均匀配速估到达
    hint, acc = [], 0.0
    for w in ws:
        acc += w
        hint.append(start + dt.timedelta(seconds=goal_s * acc / total_eq))
    # 第 2 遍：带策略系数并归一
    fs = [factor(s["km"], h) for s, h in zip(segs, hint)]
    ws2 = [w * f for w, f in zip(ws, fs)]
    total2 = sum(ws2) or 1e-6
    out, acc = [], 0.0
    for seg, w in zip(segs, ws2):
        acc += w
        arrive = start + dt.timedelta(seconds=goal_s * acc / total2)
        cutoff = _cutoff_dt(ev, seg)
        out.append(dict(
            idx=seg["idx"], name=seg["name"], km=seg["km"],
            dplus=seg["dplus"], dminus=seg.get("dminus") or 0,
            cutoff=cutoff, arrive=arrive,
            margin_s=(cutoff - arrive).total_seconds(),
            dropbag=seg.get("dropbag", False), finish=seg.get("finish", False),
            features=seg.get("features", []),
        ))
    return out


def margins_report(slug: str | None = None, goal_s: int | None = None) -> str:
    rows = plan_and_margin(slug, goal_s)
    ev = load_event(slug)
    lines = [
        "%s · 关门余量表（目标 %.0f h）" % (ev["name"], (goal_s or ev["goal_s"]) / 3600),
        "%-4s %-10s %7s %8s  %8s  %8s" % ("CP", "地点", "km", "关门", "计划到达", "余量"),
    ]
    warn = []
    for r in rows:
        m = r["margin_s"]
        sign = "+" if m >= 0 else "-"
        mm = "%s%dh%02dm" % (sign, abs(m) // 3600, abs(m) % 3600 // 60)
        if m < 0:
            warn.append(r["name"])
        extra = " ←换装" if r["dropbag"] else ""
        lines.append("%-4s %-10s %7.1f %8s  %8s  %8s%s" % (
            "CP%d" % r["idx"], r["name"], r["km"],
            r["cutoff"].strftime("%d日%H:%M"),
            r["arrive"].strftime("%d日%H:%M"), mm, extra))
    if warn:
        lines.append("!! 余量为负（会被关门）：" + "、".join(warn))
    return "\n".join(lines)


def set_goal(slug: str | None, goal_s: int) -> Path:
    slug = slug or SEED_EVENT["slug"]
    ev = load_event(slug)
    ev["goal_s"] = goal_s
    return _write(slug, "event.json", ev)


# ---------------------------------------------------------------- 编辑入口

STATUS_CN = {"announced": "未报名", "registered": "已报名待抽签",
             "confirmed": "已中签", "done": "已完赛", "cancelled": "已取消"}


def set_target(slug: str | None, *, goal_h: float | None = None,
               status: str | None = None, start_time: str | None = None) -> dict:
    """改目标完赛 / 赛事状态 / 起跑时间。只接受合法值，非法就抛 ValueError。

    页面表单直接调它——CLI 那条路（run.bat event --goal）现在也走同一个函数，
    避免两套校验各说各话。
    """
    slug = slug or SEED_EVENT["slug"]
    ev = load_event(slug)
    if goal_h is not None:
        if not 10 <= float(goal_h) <= 34:
            raise ValueError("目标完赛要在 10–34 小时之间")
        ev["goal_s"] = int(round(float(goal_h) * 3600))
    if status is not None:
        if status not in STATUS_CN:
            raise ValueError("赛事状态不认识：%s" % status)
        ev["status"] = status
    if start_time is not None:
        m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(start_time).strip())
        if not m:
            raise ValueError("起跑时间要写成 HH:MM")
        ev["start_time"] = "%02d:%02d" % (int(m.group(1)), int(m.group(2)))
    _write(slug, "event.json", ev)
    return ev


def set_cps(slug: str | None, changes: dict) -> dict:
    """批量改 CP 里程与关门时间：{idx: {"km": 10.9, "cutoff": "08:05"}}。

    校验很严：里程必须严格递增、不能超过终点里程，关门时间必须能解析。
    这是手工校对用的入口，宁可报错也不写进一份自相矛盾的赛道。
    """
    slug = slug or SEED_EVENT["slug"]
    course = load_course(slug)
    segs = course.get("segments") or []
    by_idx = {s["idx"]: s for s in segs}
    for raw_idx, ch in (changes or {}).items():
        idx = int(raw_idx)
        if idx not in by_idx:
            raise ValueError("没有 CP%d" % idx)
        seg = by_idx[idx]
        if "km" in ch and ch["km"] is not None:
            km = float(ch["km"])
            if not 0 < km < float(course.get("distance_km") or 200):
                raise ValueError("CP%d 的里程 %.1f 超出赛道范围" % (idx, km))
            seg["km"] = round(km, 1)
        if ch.get("cutoff"):
            m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(ch["cutoff"]).strip())
            if not m:
                raise ValueError("CP%d 的关门时间要写成 HH:MM" % idx)
            seg["cutoff"] = "%02d:%02d" % (int(m.group(1)), int(m.group(2)))
        seg["source"] = "manual"

    prev = 0.0
    for seg in sorted(segs, key=lambda x: x["idx"]):
        if seg["km"] <= prev:
            raise ValueError("CP%d 里程 %.1f 没有比前一个点大，赛道顺序会乱"
                             % (seg["idx"], seg["km"]))
        prev = seg["km"]
    course["segments"] = segs
    _write(slug, "course.json", course)
    return course


# ---------------------------------------------------------------- 赛中状态

# 赛中不写 event.json：比赛当天数据要能一键清空重来，单独一个文件更干净。
# {"start": "2027-03-27T06:00:00", "passed": {"1": "2027-03-27T08:12:00", ...}}
RACE_ACTIONS = ("start", "pass", "unpass", "reset", "finish")


def race_state(slug: str | None = None) -> dict:
    slug = slug or SEED_EVENT["slug"]
    st = _read(slug, "race.json") or {}
    st.setdefault("start", None)
    st.setdefault("passed", {})
    st.setdefault("finished", None)
    return st


def set_race(slug: str | None, action: str, idx: int | None = None,
             at: dt.datetime | None = None) -> dict:
    """赛中打点：开始 / 到达 / 撤销 / 重置 / 完赛。

    `at` 只在测试里用；正常从手机时钟取 now()。
    """
    slug = slug or SEED_EVENT["slug"]
    if action not in RACE_ACTIONS:
        raise ValueError("未知的赛中操作：%s" % action)
    st = race_state(slug)
    now = at or dt.datetime.now().replace(microsecond=0)

    if action == "reset":
        st = {"start": None, "passed": {}, "finished": None}
    elif action == "start":
        st["start"] = now.isoformat(timespec="seconds")
        st["passed"] = {}
        st["finished"] = None
    else:
        if not st.get("start"):
            raise ValueError("还没开始比赛，先点「开始」")
        if action in ("pass", "unpass") and idx is None:
            raise ValueError("要点名哪个 CP")
        if action == "pass":
            st["passed"][str(int(idx))] = now.isoformat(timespec="seconds")
            # 点完最后一个 CP 视为完赛
            last = max(int(k) for k in st["passed"])
            if last == len(load_course(slug)["segments"]):
                st["finished"] = now.isoformat(timespec="seconds")
        elif action == "unpass":
            st["passed"].pop(str(int(idx)), None)
            st["finished"] = None
        elif action == "finish":
            st["finished"] = now.isoformat(timespec="seconds")
    _write(slug, "race.json", st)
    return st


def race_projection(slug: str | None = None,
                    now: dt.datetime | None = None) -> dict:
    """赛中预测：用实际到达时间重算剩余 CP 的到达与余量。

    关键：所有比较都换算成**相对起点的用时**再相减，不直接减绝对时刻。
    否则手机是 2026 年、计划是 2027 年时，落后量会被算成几千小时（真踩过）。
    """
    slug = slug or SEED_EVENT["slug"]
    ev = load_event(slug)
    st = race_state(slug)
    rows = plan_and_margin(slug)
    plan_start = dt.datetime.combine(dt.date.fromisoformat(ev["date"]),
                                     dt.time(*map(int, ev["start_time"].split(":"))))
    start = dt.datetime.fromisoformat(st["start"]) if st.get("start") else plan_start
    now = (now or dt.datetime.now()).replace(microsecond=0)
    goal_s = int(ev.get("goal_s") or 22 * 3600)

    passed = {int(k): dt.datetime.fromisoformat(v) for k, v in (st["passed"] or {}).items()}
    last_idx = max(passed) if passed else 0
    last_actual = passed.get(last_idx)

    def plan_sec(idx: int) -> float:
        r = next((x for x in rows if x["idx"] == idx), None)
        return (r["arrive"] - plan_start).total_seconds() if r else 0.0

    # 落后量 = 实际用时 - 计划用时（同一基准，与年份无关）
    delay_s = 0.0
    warn = ""
    if last_idx and last_actual:
        delay_s = (last_actual - start).total_seconds() - plan_sec(last_idx)
        if abs(delay_s) > 24 * 3600:
            # 超过一天基本只剩一种可能：比赛日期/手机时钟对不上
            warn = "时间基准异常（落后量超过 24 h），请检查比赛日期与手机时间"
            delay_s = 0.0

    # 保目标所需倍率：剩余计划用时 / 剩余可用时间
    if last_idx:
        remaining_plan_s = goal_s - plan_sec(last_idx)
        remaining_budget_s = goal_s - (last_actual - start).total_seconds()
    else:
        remaining_plan_s = goal_s
        remaining_budget_s = goal_s - max(0.0, (now - start).total_seconds())
    if remaining_budget_s <= 0:
        req_factor = None
    elif remaining_plan_s <= 0:
        req_factor = 1.0
    else:
        req_factor = remaining_plan_s / max(1.0, remaining_budget_s)

    out = []
    for r in rows:
        done = r["idx"] in passed
        actual = passed.get(r["idx"])
        p_sec = (r["arrive"] - plan_start).total_seconds()
        proj = start + dt.timedelta(seconds=p_sec + delay_s)
        delta = ((actual - start).total_seconds() - p_sec) if done else delay_s
        at = actual if done else proj
        out.append({
            "idx": r["idx"], "name": r["name"], "km": r["km"],
            "cutoff": r["cutoff"], "planned": r["arrive"],
            "actual": actual, "projected": proj,
            "delta_s": delta, "margin_s": (r["cutoff"] - at).total_seconds(),
            "done": done, "dropbag": r["dropbag"],
        })

    return {
        "state": ("finished" if st.get("finished") else
                  ("racing" if st.get("start") else "pre")),
        "start": start, "now": now,
        "elapsed_s": max(0.0, (now - start).total_seconds()),
        "last_idx": last_idx, "last_actual": last_actual,
        "delay_s": delay_s, "warn": warn, "rows": out,
        "goal_at": start + dt.timedelta(seconds=goal_s),
        "req_factor": req_factor,
        "passed_count": len(passed), "total": len(rows),
        "finished": st.get("finished"),
    }


# ---------------------------------------------------------------- 补给计划

# 默认补给参数：碳水按 105K 的实战区间取 75 g/h（训练期 60，比赛日 75–90）；
# 水与钠按凉爽天气基准，热了在页面上直接调。
FUEL_DEFAULT = {
    "carb_g_h": 75,
    "fluid_ml_h": 500,
    "sodium_mg_h": 500,
    "gel_g": 25,            # 一支胶的碳水克数
    "stop_min": 3,          # 普通 CP 停留
    "dropbag_stop_min": 12,  # 换装点停留
    "temp_c": 12,
    "carry_ratio": 0.5,     # 碳水里自带的比例（其余在 CP 吃热食/粥面）
    "carry_ml": 1200,       # 水袋 + 软水壶的实际容量
}


def fuel_cfg(slug: str | None = None) -> dict:
    ev = load_event(slug)
    cfg = dict(FUEL_DEFAULT)
    cfg.update(ev.get("fuel") or {})
    return cfg


def set_fuel(slug: str | None, **kw) -> dict:
    """保存补给参数（页面表单）。范围校验，避免手滑写成 500 g 碳水/小时。"""
    slug = slug or SEED_EVENT["slug"]
    ev = load_event(slug)
    cfg = dict(FUEL_DEFAULT)
    cfg.update(ev.get("fuel") or {})
    limits = {"carb_g_h": (30, 120), "fluid_ml_h": (200, 1000),
              "sodium_mg_h": (200, 1500), "gel_g": (10, 40),
              "stop_min": (0, 30), "dropbag_stop_min": (0, 60),
              "temp_c": (-20, 40), "carry_ratio": (0.2, 1.0),
              "carry_ml": (500, 3000)}
    for k, (lo, hi) in limits.items():
        if kw.get(k) is None:
            continue
        v = float(kw[k])
        if not lo <= v <= hi:
            raise ValueError("%s 要在 %g–%g 之间" % (k, lo, hi))
        cfg[k] = int(v) if k != "temp_c" else round(v, 1)
    ev["fuel"] = cfg
    _write(slug, "event.json", ev)
    return cfg


def fuel_plan(slug: str | None = None, goal_s: int | None = None) -> dict:
    """按分段时长算每段要吃多少、带多少，以及换装点要做什么。

    假设：每个 CP 都有水和热食，所以「携带量」按段内需要算，到站补满；
    单段超过 2 h 或水需求 >1 L 会标出来——那种段必须提前确认补给点。
    """
    cfg = fuel_cfg(slug)
    rows = plan_and_margin(slug, goal_s)
    ev = load_event(slug)
    start = dt.datetime.combine(dt.date.fromisoformat(ev["date"]),
                                dt.time(*map(int, ev["start_time"].split(":"))))
    out, prev = [], start
    tot_carb = tot_fluid = tot_sodium = 0.0
    tot_gels = 0
    for r in rows:
        leg_h = max(0.0, (r["arrive"] - prev).total_seconds() / 3600.0)
        prev = r["arrive"]
        carb = cfg["carb_g_h"] * leg_h
        fluid = cfg["fluid_ml_h"] * leg_h
        sodium = cfg["sodium_mg_h"] * leg_h
        # 碳水一分为二：自带的（胶 + 能量棒）和 CP 上吃热食的部分。
        # 全按胶算会得出「背 40 支胶」这种没人这么干的结果。
        carry_g = carb * float(cfg["carry_ratio"])
        gels = int(-(-carry_g // cfg["gel_g"])) if carry_g > 0 else 0   # 向上取整
        stop = cfg["dropbag_stop_min"] if r["dropbag"] else cfg["stop_min"]
        night = r["arrive"].time() >= dt.time(21, 30) or r["arrive"].time() <= dt.time(6, 30)
        carry_ml = min(fluid, float(cfg["carry_ml"]))
        warn = []
        if fluid > float(cfg["carry_ml"]):
            warn.append("这段要 %d ml 水，超过携带量 %d ml：中途必须补水"
                        % (round(fluid), round(float(cfg["carry_ml"]))))
        elif leg_h >= 2.0:
            warn.append("%.1f h 长段，出发前把水加满并确认中途水源" % leg_h)
        tot_carb += carb
        tot_fluid += fluid
        tot_sodium += sodium
        tot_gels += gels
        out.append({
            "idx": r["idx"], "name": r["name"], "km": r["km"],
            "arrive": r["arrive"], "leg_h": round(leg_h, 2),
            "carb_g": int(round(carb)), "carry_g": int(round(carry_g)),
            "carb_station_g": int(round(carb - carry_g)),
            "fluid_ml": int(round(fluid)), "carry_ml": int(round(carry_ml)),
            "sodium_mg": int(round(sodium)), "gels": gels,
            "stop_min": stop, "dropbag": r["dropbag"], "night": night,
            "warn": warn,
        })
    total_h = max(0.01, (rows[-1]["arrive"] - start).total_seconds() / 3600.0) \
        if rows else 0.0
    return {
        "cfg": cfg, "rows": out,
        "totals": {
            "hours": round(total_h, 1),
            "carb_g": int(round(tot_carb)),
            "fluid_l": round(tot_fluid / 1000, 1),
            "sodium_mg": int(round(tot_sodium)),
            "gels": tot_gels,
        },
    }


# ---------------------------------------------------------------- 装备

def gear_items(slug: str | None = None) -> list[dict]:
    """装备清单逐条读出：idx 是「第几个勾选项」（不含说明行），用于页面回写。"""
    slug = slug or SEED_EVENT["slug"]
    p = _dir(slug) / "gear.md"
    if not p.exists():
        seed_gear(slug)
    items, n = [], 0
    for line in io.open(p, encoding="utf-8").read().splitlines():
        m = re.match(r"^- \[([ xX])\]\s*(.+)$", line.strip())
        if not m:
            continue
        items.append({"idx": n, "done": m.group(1).lower() == "x",
                      "text": m.group(2).strip()})
        n += 1
    return items


def set_gear(slug: str | None, idx: int, done: bool) -> bool:
    """勾选/取消第 idx 项。只改清单行，说明行原样保留。"""
    slug = slug or SEED_EVENT["slug"]
    p = _dir(slug) / "gear.md"
    if not p.exists():
        seed_gear(slug)
    lines = io.open(p, encoding="utf-8").read().splitlines()
    n = 0
    for i, line in enumerate(lines):
        m = re.match(r"^- \[([ xX])\]\s*(.+)$", line.strip())
        if not m:
            continue
        if n == idx:
            lines[i] = "- [%s] %s" % ("x" if done else " ", m.group(2).strip())
            io.open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")
            return True
        n += 1
    return False


def gear_progress(slug: str | None = None) -> dict:
    slug = slug or SEED_EVENT["slug"]
    p = _dir(slug) / "gear.md"
    if not p.exists():
        seed_gear(slug)
    txt = io.open(p, encoding="utf-8").read()
    total = len(re.findall(r"^- \[[ xX]\]", txt, re.M))
    done = len(re.findall(r"^- \[[xX]\]", txt, re.M))
    missing = [l.strip()[6:].strip() for l in txt.splitlines()
               if re.match(r"^- \[ \]", l.strip())]
    return {"done": done, "total": total, "missing": missing}


# ---------------------------------------------------------------- GPX

def parse_gpx(path: str | Path, slug: str | None = None,
              save: bool = True) -> dict:
    """解析 GPX：滤波累计爬升 + 500 m 剖面 + CP 就近匹配。

    坑（设计稿 §7.3）：GPS 高程噪声 ±3–5 m，直接累加会把 5547 m 算成 8000+。
    处理：5 点滑动平均后，只累计上升超过 3 m 的段落。
    """
    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    tree = ET.parse(path)
    pts = []
    for trkpt in tree.iter("{%s}trkpt" % ns["g"]):
        lat, lon = float(trkpt.get("lat")), float(trkpt.get("lon"))
        ele = trkpt.find("{%s}ele" % ns["g"])
        pts.append((lat, lon, float(ele.text) if ele is not None else None))
    if len(pts) < 10:
        raise ValueError("GPX 里少于 10 个轨迹点：%s" % path)

    # 距离
    def haversine(a, b):
        R = 6371000.0
        la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
        x = math.sin((la2 - la1) / 2) ** 2 + \
            math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
        return 2 * R * math.asin(math.sqrt(x))

    dists = [0.0]
    for a, b in zip(pts, pts[1:]):
        dists.append(dists[-1] + haversine(a, b))
    total_m = dists[-1]

    # 高程：无 ele 的点用前值；5 点滑动平均
    eles = [p[2] if p[2] is not None else (pts[0][2] or 0) for p in pts]
    sm = []
    for i in range(len(eles)):
        w = eles[max(0, i - 2):i + 3]
        sm.append(sum(w) / len(w))
    ascent = descent = 0.0
    base = sm[0]
    for v in sm[1:]:
        d = v - base
        if d > 3:
            ascent += d
            base = v
        elif d < -3:
            descent += -d
            base = v

    # 剖面：每 500 m 采样
    step = 500.0
    profile, nxt = [], 0.0
    for d, e in zip(dists, sm):
        if d >= nxt:
            profile.append([round(d / 1000, 2), round(e)])
            nxt += step

    # 航点投影：每个 wpt 找到轨迹上最近的点，换算成累计里程。
    # 直接按经纬度取最近点即可——这类赛事航点都落在轨迹上，误差 <20 m。
    raw_w = []
    for wpt in tree.iter("{%s}wpt" % ns["g"]):
        nm = wpt.find("{%s}name" % ns["g"])
        raw_w.append((float(wpt.get("lat")), float(wpt.get("lon")),
                      (nm.text or "").strip() if nm is not None else ""))
    wpts = []
    for lat, lon, nm in raw_w:
        best, bi = 1e18, 0
        for i, (la, lo, _e) in enumerate(pts):
            d = (la - lat) ** 2 + (lo - lon) ** 2
            if d < best:
                best, bi = d, i
        wpts.append({"name": nm, "km": round(dists[bi] / 1000, 2),
                     "ele": round(sm[bi]), "idx": bi})
    wpts.sort(key=lambda w: w["km"])

    # 航点之间的分段统计（含起点→首个航点、末个航点→终点）
    marks = [{"name": "起点", "km": 0.0, "ele": round(sm[0]), "idx": 0}] + wpts
    if marks[-1]["km"] < dists[-1] / 1000 - 0.05:
        marks.append({"name": "终点", "km": round(dists[-1] / 1000, 2),
                      "ele": round(sm[-1]), "idx": len(pts) - 1})
    for a, b in zip(marks, marks[1:]):
        seg_ele = sm[a["idx"]:b["idx"] + 1]
        up = down = 0.0
        base = seg_ele[0] if seg_ele else 0.0
        for v in seg_ele[1:]:
            d = v - base
            if d > 3:
                up += d
                base = v
            elif d < -3:
                down += -d
                base = v
        b["dplus"] = int(round(up))
        b["dminus"] = int(round(down))

    out = dict(
        points=len(pts), total_km=round(total_m / 1000, 2),
        ascent_m=int(ascent), descent_m=int(descent),
        max_ele=round(max(sm)), min_ele=round(min(sm)),
        profile=profile, waypoints=wpts,
    )
    if save:
        import shutil
        dst = _dir(slug or SEED_EVENT["slug"]) / "track.gpx"
        dst.parent.mkdir(parents=True, exist_ok=True)
        # 服务端上传时文件已经落在目标位置了，别再 copy 到自己身上
        if Path(path).resolve() != dst.resolve():
            shutil.copy(path, dst)
        _write(slug or SEED_EVENT["slug"], "track_stats.json", out)
    return out


# ---------------------------------------------------------------- 轨迹对照

# GPX 航点与海报 CP 的别名（不同届拼写有出入，比如「洛/骆家山林场」）
_ALIAS = {
    "仙山雅宿": "仙山雅宿", "冰川瀑布": "冰川瀑布景区", "准提庵": "准提庵",
    "鲁班庙": "鲁班庙", "海窝子古镇": "海窝子古镇", "渔江楠游客中心": "渔江楠游客中心",
    "庙子坪": "庙子坪", "朝天路梁子": "朝天路梁子", "洛家山林场": "骆家山林场",
    "骆家山林场": "骆家山林场", "金坪度假村": "金坪度假村",
    "白鹿上书院": "白鹿上书院", "白鹿上书院1": "白鹿上书院",
}


def _norm(name: str) -> str:
    return re.sub(r"\s+", "", name or "")


def track_compare(slug: str | None = None) -> dict:
    """把轨迹里的航点（实测里程）与 course.json 的官方 CP 对齐。

    两届路线可能改线：2026 的 105K 有「金坪度假村」，2027 海报没有，
    且中段里程整体差 2.5–2.7 km。所以**里程以官方海报为准**，
    轨迹只用来给地形形状和相对爬升做参考。差 >1 km 的 CP 会被标出来。
    """
    slug = slug or SEED_EVENT["slug"]
    stats = _read(slug, "track_stats.json")
    if not stats:
        return {"ok": False, "reason": "没有导入轨迹"}
    wpts = stats.get("waypoints") or []
    by = {}
    for w in wpts:
        key = _ALIAS.get(_norm(w["name"]), _norm(w["name"]))
        by.setdefault(key, w)

    rows, worst = [], 0.0
    for seg in load_course(slug)["segments"]:
        w = by.get(_ALIAS.get(_norm(seg["name"]), _norm(seg["name"])))
        if not w:
            rows.append({"idx": seg["idx"], "name": seg["name"],
                         "km_official": seg["km"], "km_gpx": None,
                         "d_km": None, "dplus_official": seg["dplus"],
                         "dplus_gpx": None})
            continue
        d = round(w["km"] - seg["km"], 2)
        worst = max(worst, abs(d))
        rows.append({"idx": seg["idx"], "name": seg["name"],
                     "km_official": seg["km"], "km_gpx": w["km"], "d_km": d,
                     "dplus_official": seg["dplus"], "dplus_gpx": w.get("dplus")})

    unmatched = [w["name"] for w in wpts
                 if _ALIAS.get(_norm(w["name"]), _norm(w["name"]))
                 not in {_ALIAS.get(_norm(x["name"]), _norm(x["name"]))
                         for x in load_course(slug)["segments"]}]
    return {
        "ok": True,
        "track_km": stats.get("total_km"),
        "track_ascent": stats.get("ascent_m"),
        "track_points": stats.get("points"),
        "rows": rows,
        "max_delta_km": worst,
        "unmatched": unmatched,
        "same_course": worst <= 1.0,
    }


# ---------------------------------------------------------------- 看板接口

STATUS_CN = {"announced": "已公布", "registered": "已报名",
             "confirmed": "已中签", "done": "已完赛"}


def countdown(ev: dict | None = None, today: dt.date | None = None) -> dict:
    ev = ev or load_event()
    today = today or dt.date.today()
    race = dt.date.fromisoformat(ev["date"])
    gear = gear_progress(ev["slug"])
    rows = plan_and_margin(ev["slug"])
    tightest = min(rows, key=lambda r: r["margin_s"])
    return dict(
        name=ev["name"], status=ev["status"],
        status_cn=STATUS_CN.get(ev.get("status"), ev.get("status", "")),
        days=(race - today).days,
        race=race,
        dist=ev["distance_km"], ascent=ev["ascent_m"],
        goal_h=ev.get("goal_s", 22 * 3600) / 3600,
        duration_h=ev.get("duration_h", 29),
        cutoff=ev["cutoff_time"],
        venue=ev["venue"],
        gear_done=gear["done"], gear_total=gear["total"],
        gear_missing=gear["missing"],
        tightest=tightest,
    )
