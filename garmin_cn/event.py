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

import collections
import datetime as dt
import io
import json
import math
import os
import re
import shutil
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
    "short": "熊猫 105K",
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
    "priority": "A",                # A = 主赛，B = 有成绩要求，C = 训练赛/模拟
    "role": "主赛",
    "gear_due_days": 7,
    "source": "poster-2027",
}

# 其余三场：11/29 秦岭 43K（训练赛）、12/27 福州全马（B）、1/16 60K 模拟（自测）。
# 这三场都没有官方 CP 表与关门时间，所以 course.json 用「单段合成」（见 _synthetic_course），
# 页面只给总距离/总爬升与补给，不编造中途关门——编出来的余量比没有更危险。
SEED_OTHERS = [
    {
        "slug": "qinling-43k-2026",
        "short": "秦岭 43K",
        "name": "2026 秦岭山地马拉松 · 43K",
        "org": "秦岭",
        "status": "confirmed",
        "date": "2026-11-29",
        "start_time": "08:00",
        "cutoff_time": None,
        "duration_h": None,          # 官方关门未公布，不猜
        "distance_km": 43.0,
        "ascent_m": 1500,
        "descent_m": 1500,
        "venue": "秦岭（起终点见赛事公告）",
        "priority": "C",
        "role": "训练赛",
        "goal_s": int(5.5 * 3600),
        "gear_due_days": 3,
        "source": "manual",
        "notes": "作为 105K 的训练赛：全程 Z2、练补给与下坡，不追成绩。"
                 "起跑时间与关门时间待官方公布后在「调整」里改。",
    },
    {
        "slug": "fuzhou-marathon-2026",
        "short": "福州全马",
        "name": "2026 福州马拉松 · 全马",
        "org": "福州马拉松",
        "status": "confirmed",
        "date": "2026-12-27",
        "start_time": "07:30",
        "cutoff_time": None,
        "duration_h": 6,
        "distance_km": 42.2,
        "ascent_m": 120,
        "descent_m": 120,
        "venue": "福州",
        "priority": "B",
        "role": "B 级目标赛",
        "goal_s": int(3.75 * 3600),
        "gear_due_days": 3,
        "source": "manual",
        "notes": "路跑全马，爬升可忽略。关门按 6 h 粗算（多数城市马拉松的兜底），"
                 "以官方为准。",
    },
    {
        "slug": "sim-60k-2027",
        "short": "60K 模拟",
        "name": "2027 60K 模拟赛 · 自测",
        "org": "自组织",
        "status": "confirmed",
        "date": "2027-01-16",
        "start_time": "06:00",
        "cutoff_time": None,
        "duration_h": None,
        "distance_km": 60.0,
        "ascent_m": 2800,
        "descent_m": 2800,
        "venue": "自选环线（与 4 月青泥岭对照）",
        "priority": "C",
        "role": "专项自测",
        "goal_s": int(12 * 3600),
        "gear_due_days": 3,
        "source": "manual",
        "notes": "检验 60 km / +2800 m 的完成度，为 105K 定配速与补给节奏。"
                 "没有关门，跑完即达标。",
    },
]

# slug -> 种子；ensure_seeds() 只按这里的 slug 建目录，不会给陌生 slug 编数据
EVENT_SEEDS = {e["slug"]: e for e in [SEED_EVENT, *SEED_OTHERS]}

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


SEED_GEAR_OTHERS = {
    "qinling-43k-2026": """# 强制装备清单（秦岭 43K · 训练赛）

> 5.5 h 的山地赛，11 月底秦岭白天 5–10 ℃、山口有风。按「短赛也要能自救」配。

- [ ] 头灯 + 备用电池（可能天黑前回不来）
- [ ] 保温毯 / 应急保温层
- [ ] 急救包（绷带、创可贴、止痛）
- [ ] 水袋 / 软水壶 ≥ 1 L
- [ ] 手机 + 充电宝（≥10000 mAh）
- [ ] 防风外套（硬壳）
- [ ] 保暖中层（薄抓绒）
- [ ] 帽子 + 手套
- [ ] 能量胶 6 支 + 盐丸
- [ ] 号码布 + 计时芯片
- [ ] 越野鞋（磨合过的，别穿新鞋）
""",
    "fuzhou-marathon-2026": """# 装备清单（福州全马）

- [ ] 比赛鞋（已磨合 ≥ 50 km）
- [ ] 号码布 + 计时芯片
- [ ] 能量胶 4 支（10/20/30 km 各一）
- [ ] 盐丸 / 电解质片
- [ ] 一次性雨衣（起跑保暖，跑开即丢）
- [ ] 乳贴 / 防磨膏
""",
    "sim-60k-2027": """# 强制装备清单（60K 模拟 · 自测）

> 自组织长距离，没有关门与救援，装备按 105K 的标准打折：能自救、能过夜。

- [ ] 头灯 + 备用电池
- [ ] 保温毯
- [ ] 急救包
- [ ] 水袋 ≥ 1.5 L
- [ ] 手机 + 充电宝
- [ ] 硬壳 + 保暖层
- [ ] 手杖（一对）
- [ ] 能量胶 / 固体补给（按 12 h 配）
- [ ] 盐丸
- [ ] 备用鞋袜（放车上）
- [ ] 现金 / 交通卡
""",
}


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


def ensure_seeds() -> None:
    """把种子赛事落成目录（只补缺，不覆盖已有文件）。

    只处理 EVENT_SEEDS 里的 slug：陌生 slug 不会凭空生成一份假档案，
    否则 `/event?slug=xxx` 一次手误就会写出一堆只有 slug 的空赛事。
    """
    for slug, seed in EVENT_SEEDS.items():
        cur = _read(slug, "event.json")
        if not cur:
            ev = dict(seed)
            ev["slug"] = slug
            _write(slug, "event.json", ev)
        else:
            # 补齐后来新增的字段（short / priority / role …），但绝不覆盖用户改过的值：
            # event.json 里有他自己的目标、状态、CP 调整，迁移只能「填空」。
            miss = {k: v for k, v in seed.items() if k not in cur}
            if miss:
                cur.update(miss)
                _write(slug, "event.json", cur)
        seed_gear(slug)
        # 老档案（这一版之前建的）没有 provenance，补一份：来源取第一段的 source，
        # 时间取文件写入日期。只补一次，之后以实际导入记录为准。
        cp = _dir(slug) / "course.json"
        if cp.exists():
            try:
                cc = json.loads(io.open(cp, encoding="utf-8").read())
                if cc.get("segments") and not cc.get("provenance"):
                    # 取「多数段」的来源：手改过一两个 CP 不代表整份赛道是手填的
                    _src = collections.Counter(
                        (x.get("source") or "manual") for x in cc["segments"])
                    cc["provenance"] = {
                        "source": _src.most_common(1)[0][0],
                        "by": "user",
                        "at": dt.date.fromtimestamp(cp.stat().st_mtime).isoformat(),
                        "checked": [],
                    }
                    _write(slug, "course.json", cc)
            except (ValueError, OSError):
                pass


def list_events(today: dt.date | None = None) -> list[dict]:
    """所有赛事：按日期升序，带上「还有几天 / 是否已过 / 有没有 CP 表」。

    today 可传：教练上下文要按「那一天」算倒计时，不能用跑批当天的日期，
    否则回看历史某天时所有赛事的剩余天数都是错的。
    """
    # 不再自动播种。EVENT_SEEDS 里那 4 场是作者自己的比赛（含真实 CP 表、
    # 关门时间和装备清单），跟着安装包走会让**每个用户**打开就看到别人的赛事
    # （2026-11-12 用户反馈）。种子只在显式调用 ensure_seeds() 时生成，
    # 例如开发环境或 `event --seed`。老设备上已经建好的目录不动，删不删由用户决定。
    today = today or dt.date.today()
    out = []
    if EVENTS.exists():
        for d in sorted(EVENTS.iterdir()):
            if not d.is_dir() or d.name.startswith("_"):
                continue
            ev = _read(d.name, "event.json")
            if not ev or not ev.get("date"):
                continue
            try:
                race = dt.date.fromisoformat(ev["date"])
            except ValueError:
                continue
            course = _read(d.name, "course.json")
            segs = (course or {}).get("segments") or []
            has_course = bool(course) and len(segs) > 1
            out.append({
                "slug": d.name,
                "short": ev.get("short") or ev.get("name", d.name)[:10],
                "name": ev.get("name") or d.name,
                "date": ev["date"], "race": race,
                "days": (race - today).days,
                "status": ev.get("status") or "announced",
                "priority": ev.get("priority") or "C",
                "role": ev.get("role") or "",
                "distance_km": ev.get("distance_km") or 0,
                "ascent_m": ev.get("ascent_m") or 0,
                "goal_s": ev.get("goal_s") or 0,
                "duration_h": ev.get("duration_h"),
                "has_course": has_course,
                "done": ev.get("status") == "done" or (race - today).days < -1,
            })
    out.sort(key=lambda e: e["date"])
    return out


def nearest_upcoming(today: dt.date | None = None) -> dict | None:
    """下一场：日期未过、状态不是完赛/取消里最近的一场。

    每天用的卡片、教练上下文都按这个走，不做「手动指定当前赛事」——
    指定了就会在赛后继续显示上一场，反而要人记着去改。
    """
    today = today or dt.date.today()
    cands = [e for e in list_events(today=today)
             if e["status"] not in ("done", "cancelled") and e["days"] >= 0]
    return cands[0] if cands else None


def active_slug(slug: str | None = None) -> str:
    """解析「当前赛事」：显式传 slug 就用它，否则取下一场比赛，再否则最近的已完赛。"""
    if slug:
        if slug not in EVENT_SEEDS and not (_dir(slug) / "event.json").exists():
            raise ValueError("没有这个赛事：%s" % slug)
        return slug
    ev = nearest_upcoming()
    if ev:
        return ev["slug"]
    allv = list_events()
    if allv:
        return allv[-1]["slug"]
    # 一场都没有就抛出去：以前这里回落到 SEED_EVENT，配上 load_event 的
    # 「读不到就按种子生成」，等于谁打开一次就凭空多出作者的那场比赛
    raise ValueError("还没有任何赛事")


def load_event(slug: str | None = None) -> dict:
    slug = active_slug(slug)
    ev = _read(slug, "event.json")
    if not ev:
        seed = EVENT_SEEDS.get(slug)
        if not seed:
            raise ValueError("没有这个赛事：%s" % slug)
        ev = dict(seed)
        ev["slug"] = slug
        _write(slug, "event.json", ev)
        seed_gear(slug)
    return ev


def new_event(name: str, date: str, distance_km: float,
              ascent_m: int | None = None, start_time: str = "08:00",
              duration_h: float | None = None, priority: str = "C",
              goal_s: int | None = None, kind: str = "trail") -> dict:
    """从零建一场赛事。只要求名字 + 日期 + 距离，其余能推就推。

    slug 用日期生成（race-2027-05-01），同一天再建就加 -2：
    中文名字转拼音得不偿失，日期 slug 既稳定又一眼能看出是哪场。
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("赛事名不能为空")
    # 手机上打日期很随意：2027-5-1 / 2027/7/10 / 2027.7.10 / 2027年7月10日 都认
    m = re.search(r"(20\d{2})\D{0,2}(\d{1,2})\D{0,2}(\d{1,2})", str(date or ""))
    try:
        d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
    except (TypeError, ValueError):
        d = None
    if not d:
        raise ValueError("日期要写成 2027-05-01")
    if not 1 <= float(distance_km) <= 400:
        raise ValueError("距离要在 1–400 km 之间")
    base = "race-%s" % d.isoformat()
    slug, n = base, 2
    while (_dir(slug) / "event.json").exists():
        slug, n = "%s-%d" % (base, n), n + 1
    ev = {
        "slug": slug, "name": name, "short": name[:10],
        "status": "confirmed", "date": d.isoformat(),
        "start_time": start_time or "08:00",
        "cutoff_time": None, "duration_h": duration_h,
        "distance_km": round(float(distance_km), 1),
        "ascent_m": int(ascent_m or 0),
        "descent_m": int(ascent_m or 0),
        "venue": "", "priority": priority or "C",
        "role": {"A": "主赛", "B": "B 级目标赛"}.get(priority, "训练赛"),
        # 目标时间的初值按类型给：路跑约 6:00/km，越野约 9:36/km（含爬升）
        "goal_s": int(goal_s or (float(distance_km)
                                 * (0.10 if kind == "road" else 0.16) * 3600)),
        "kind": "road" if kind == "road" else "trail",
        "gear_due_days": 3, "source": "manual",
    }
    _write(slug, "event.json", ev)
    seed_gear(slug)
    return ev


def delete_event(slug: str) -> dict:
    """删除整场赛事：目录连同 CP 表、装备、资料、附件一起。

    不可逆，所以只接受「EVENTS 下某一层、且确实有 event.json」的目录：
    空目录不删（多半是误建），上层目录不删（防 `../` 穿越），
    陌生 slug 不删（`load_event` 那句「读不到就按种子生成」的教训）。
    """
    s = (slug or "").strip()
    if not s or s in (".", "..") or "/" in s or "\\" in s:
        raise ValueError("赛事标识不合法")
    d = _dir(s)
    try:
        if d.parent.resolve() != EVENTS.resolve():
            raise ValueError("只能删赛事目录下的子目录")
    except OSError:
        raise ValueError("赛事目录不可用") from None
    if not (d / "event.json").is_file():
        raise ValueError("没有这个赛事：%s" % s)
    ev = _read(s, "event.json") or {}
    shutil.rmtree(d)
    return {"slug": s, "short": ev.get("short") or ev.get("name") or s}


def _synthetic_course(ev: dict) -> dict:
    """没有 CP 表时的单段赛道：整场算一段，只用于补给与总用时。

    为什么允许：43K / 全马 / 自测这三场拿不到中途关门时间。硬编一套 CP 与关门
    会让人按假数据做决策——宁可只给「总距离 + 总爬升 + 目标用时」这一层。
    """
    dur = ev.get("duration_h")
    cutoff, day = None, 1
    if dur:
        st = dt.datetime.combine(dt.date.fromisoformat(ev["date"]),
                                 dt.time(*map(int, ev["start_time"].split(":"))))
        c = st + dt.timedelta(hours=float(dur))
        cutoff = c.strftime("%H:%M")
        day = 1 + (c.date() - dt.date.fromisoformat(ev["date"])).days
    return {
        "event": ev["slug"],
        "distance_km": ev.get("distance_km") or 0,
        "ascent_m": ev.get("ascent_m") or 0,
        "start": {"time": ev.get("start_time") or "08:00",
                  "date": ev.get("date"), "venue": ev.get("venue") or ""},
        "segments": [dict(
            idx=1, name="终点",
            en="", km=ev.get("distance_km") or 0,
            dplus=ev.get("ascent_m") or 0, dminus=ev.get("descent_m") or 0,
            cutoff=cutoff, cutoff_day=day, dropbag=False, finish=True,
            features=[], source="synthetic",
        )],
        "note": "没有官方 CP 表：整场按一段计算，只有总距离/爬升与补给。",
    }


def load_course(slug: str | None = None) -> dict:
    slug = active_slug(slug)
    c = _read(slug, "course.json")
    if not c:
        c = _build_seed_course(slug) if slug == SEED_EVENT["slug"] \
            else _synthetic_course(load_event(slug))
        _write(slug, "course.json", c)
    return c


def save_course(slug: str, course: dict) -> Path:
    return _write(slug, "course.json", course)


GEAR_ROAD = """# 装备清单（路跑）

> 路跑补给站密，**不用自带水壶**；先看官方是否有强制装备，没有就按这份来。

- [ ] 号码布 + 计时芯片
- [ ] 路跑鞋（磨合过的，别穿新鞋）
- [ ] 能量胶（按分段补给计划，约每 40 分钟 1 支）
- [ ] 盐丸（热天每 10 km 一组）
- [ ] 雨衣 / 一次性雨披（预报有雨才带，比防风衣实用）
- [ ] 手机
"""

GEAR_TRAIL = """# 装备清单（越野）

> 越野先对照官方强制装备清单，这里按「短赛也要能自救」配；官方要求更高的以官方为准。

- [ ] 号码布 + 计时芯片
- [ ] 越野鞋（磨合过的战靴，别穿新鞋）
- [ ] 水袋 / 软水壶 ≥ 1 L
- [ ] 能量胶 / 固体补给（按分段计划）
- [ ] 盐丸 / 电解质
- [ ] 保温毯（多数赛事强制）
- [ ] 急救包（绷带、创可贴）
- [ ] 防风防水外套
- [ ] 头灯 + 备用电池（天黑前回不来就要）
- [ ] 手机 + 充电宝
- [ ] 换装包（有换装点的赛事）
"""


def seed_gear(slug: str | None = None) -> Path:
    slug = active_slug(slug)
    p = _dir(slug) / "gear.md"
    if not p.exists():
        _dir(slug).mkdir(parents=True, exist_ok=True)
        # 新建的赛事按类型给模板：路跑和越野的清单根本不是一回事，
        # 给路跑塞「头灯 + 保温毯」只会让人以为要背一堆没用的东西。
        if slug == SEED_EVENT["slug"]:
            text = SEED_GEAR
        elif slug in SEED_GEAR_OTHERS:
            text = SEED_GEAR_OTHERS[slug]
        else:
            ev = _read(slug, "event.json") or {}
            text = GEAR_ROAD if (ev.get("kind") or "trail") == "road" else GEAR_TRAIL
        io.open(p, "w", encoding="utf-8").write(text)
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

def _cutoff_dt(ev: dict, seg: dict) -> dt.datetime | None:
    """该 CP 的关门时刻；没有官方关门就返回 None（不猜）。"""
    if not seg.get("cutoff"):
        return None
    d = dt.date.fromisoformat(ev["date"]) + dt.timedelta(days=(seg.get("cutoff_day") or 1) - 1)
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
            dplus=seg.get("dplus") or 0, dminus=seg.get("dminus") or 0,
            cutoff=cutoff, arrive=arrive,
            # 没有关门就没有余量：None 比 0 诚实，排版层按「—」显示
            margin_s=((cutoff - arrive).total_seconds() if cutoff else None),
            dropbag=seg.get("dropbag", False), finish=seg.get("finish", False),
            features=seg.get("features", []),
        ))
    return out


def margins_report(slug: str | None = None, goal_s: int | None = None) -> str:
    rows = plan_and_margin(slug, goal_s)
    ev = load_event(slug)
    lines = [
        "%s · 关门余量表（目标 %g h）" % (ev["name"], (goal_s or ev["goal_s"]) / 3600),
        "%-4s %-10s %7s %8s  %8s  %8s" % ("CP", "地点", "km", "关门", "计划到达", "余量"),
    ]
    warn = []
    for r in rows:
        m = r["margin_s"]
        if m is None:
            mm = "—（无关门）"
        else:
            sign = "-" if m < 0 else "+"
            mm = "%s%dh%02dm" % (sign, abs(m) // 3600, abs(m) % 3600 // 60)
            if m < 0:
                warn.append(r["name"])
        extra = " ←换装" if r["dropbag"] else ""
        lines.append("%-4s %-10s %7.1f %8s  %8s  %8s%s" % (
            "CP%d" % r["idx"] if len(rows) > 1 else "全程", r["name"], r["km"],
            r["cutoff"].strftime("%d日%H:%M") if r["cutoff"] else "—",
            r["arrive"].strftime("%d日%H:%M"), mm, extra))
    if warn:
        lines.append("!! 余量为负（会被关门）：" + "、".join(warn))
    return "\n".join(lines)


def set_goal(slug: str | None, goal_s: int) -> Path:
    slug = active_slug(slug)
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
    slug = active_slug(slug)
    ev = load_event(slug)
    if goal_h is not None:
        # 范围随赛事规模走：写死 10–34 h 会把 43K / 全马的目标全都拒掉（真踩过）
        dist = float(ev.get("distance_km") or 0)
        lo = max(0.5, dist * 0.045)                  # 约 2.7 min/km，人类下限
        hi = float(ev.get("duration_h") or max(2.0, dist * 0.5))
        hi = max(hi, lo * 2)
        if not lo <= float(goal_h) <= hi:
            raise ValueError("目标完赛要在 %.1f–%.1f 小时之间（%s %.0f km）"
                             % (lo, hi, ev.get("short") or ev.get("name", ""), dist))
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
    slug = active_slug(slug)
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
            # 终点 CP 的里程本来就等于赛道总长，不能要求「严格小于」
            if not 0 < km <= float(course.get("distance_km") or 200) + 0.05:
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


# ---------------------------------------------------------------- 资料条目

# 设计稿 §3.2 的六类，对应六种真实需求；gear 不在这里——装备有专门的 gear.md
# （要逐项勾选状态，用 Markdown checklist 更合适）。
DOC_KINDS = {
    "rule": "规则 / 关门 / 罚则",
    "logistics": "交通 / 住宿 / 领物",
    "aid": "补给站与换装",
    "entry": "报名 / 参赛号",
    "result": "成绩 / 证书",
    "misc": "其它",
}

# 附件白名单：只认这些扩展名，别的（比如 .exe）连列都不列
ATTACH_EXT = (".pdf", ".png", ".jpg", ".jpeg", ".txt", ".md", ".csv",
              ".gpx", ".ics", ".xlsx", ".docx")
# 赛事目录里的系统文件，不算「用户资料」
RESERVED_FILES = {"event.json", "course.json", "gear.md", "race.json",
                  "track.gpx", "track_stats.json", "docs.json",
                  "trip.json", "trip_rules.json", "remind.json"}


def human_size(n: float) -> str:
    """字节 → 8 B / 12 KB / 1.2 MB。小文件不要显示成 0 KB。"""
    n = float(n or 0)
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.0f KB" % (n / 1024)
    return "%.1f MB" % (n / 1048576)


def docs(slug: str | None = None) -> dict:
    """资料条目 + 目录里的附件。

    附件不登记在 docs.json 里：把 PDF 拖进 `data/events/<slug>/` 就算数，
    少一步「先登记再能看」的操作——这是设计稿 §6 里定的方式，照做。
    """
    slug = active_slug(slug)
    data = _read(slug, "docs.json") or {}
    items = []
    for it in data.get("items") or []:
        if not isinstance(it, dict) or not it.get("title"):
            continue
        items.append({
            "kind": it.get("kind") or "misc",
            "kind_cn": DOC_KINDS.get(it.get("kind") or "misc", "其它"),
            "title": it.get("title"),
            "body": it.get("body") or "",
            "url": it.get("url") or "",
            "at": it.get("at") or "",
            "locked": False,
        })
    # notes.md 当成一条只读资料：桌面端直接编辑它比在手机上敲长文舒服
    np = _dir(slug) / "notes.md"
    if np.exists():
        try:
            items.append({"kind": "misc", "kind_cn": DOC_KINDS["misc"],
                          "title": "备注（notes.md）", "locked": True, "url": "",
                          "body": io.open(np, encoding="utf-8").read().strip(),
                          "at": dt.date.fromtimestamp(np.stat().st_mtime).isoformat()})
        except OSError:
            pass

    files = []
    d = _dir(slug)
    if d.exists():
        for p in sorted(d.iterdir()):
            if not p.is_file() or p.name in RESERVED_FILES:
                continue
            if p.suffix.lower() not in ATTACH_EXT:
                continue
            files.append({
                "name": p.name,
                "ext": p.suffix.lower().lstrip("."),
                "size": p.stat().st_size,
                "at": dt.date.fromtimestamp(p.stat().st_mtime).isoformat(),
            })
    return {"items": items, "files": files}


def add_doc(slug: str | None, kind: str, title: str, body: str | None = None,
            url: str | None = None) -> dict:
    """加一条资料。标题必填；链接只认 http(s)——`javascript:` 这类一律拒绝。"""
    slug = active_slug(slug)
    if kind not in DOC_KINDS:
        raise ValueError("资料分类不认识：%s" % kind)
    title = (title or "").strip()
    if not title:
        raise ValueError("标题不能为空")
    if len(title) > 80:
        raise ValueError("标题太长了（上限 80 字）")
    body = (body or "").strip()
    if len(body) > 4000:
        raise ValueError("正文太长了（上限 4000 字）")
    url = (url or "").strip()
    if url and not re.match(r"^https?://", url):
        raise ValueError("链接要以 http:// 或 https:// 开头")
    if not body and not url:
        raise ValueError("正文和链接至少填一个")
    data = _read(slug, "docs.json") or {}
    items = data.get("items") or []
    items.append({"kind": kind, "title": title, "body": body, "url": url,
                  "at": dt.date.today().isoformat()})
    _write(slug, "docs.json", {"items": items})
    return {"items": len(items)}


def del_doc(slug: str | None, idx: int) -> bool:
    """删一条。idx 是 docs.json 里的下标（页面按同一顺序渲染，能对上号）。"""
    slug = active_slug(slug)
    data = _read(slug, "docs.json") or {}
    items = data.get("items") or []
    if idx < 0 or idx >= len(items):
        return False
    items.pop(idx)
    _write(slug, "docs.json", {"items": items})
    return True


def update_doc(slug: str | None, idx: int, *, title: str | None = None,
               kind: str | None = None, body: str | None = None,
               url: str | None = None) -> dict:
    """改一条资料。只改传进来的字段，没传的保持原样。

    为什么单独一个函数而不是「删了重加」：删了重加会换掉 `at`（添加日期），
    顺序也会跑到末尾，用户改个错别字就会发现条目位置变了。
    """
    slug = active_slug(slug)
    data = _read(slug, "docs.json") or {}
    items = data.get("items") or []
    if idx < 0 or idx >= len(items):
        raise ValueError("这条资料已经不存在了")
    it = items[idx]
    if title is not None:
        t = title.strip()
        if not t:
            raise ValueError("标题不能为空")
        if len(t) > 80:
            raise ValueError("标题太长了（上限 80 字）")
        it["title"] = t
    if kind is not None:
        if kind not in DOC_KINDS:
            raise ValueError("资料分类不认识：%s" % kind)
        it["kind"] = kind
    if body is not None:
        b = body.strip()
        if len(b) > 4000:
            raise ValueError("正文太长了（上限 4000 字）")
        it["body"] = b
    if url is not None:
        u = url.strip()
        if u and not re.match(r"^https?://", u):
            raise ValueError("链接要以 http:// 或 https:// 开头")
        it["url"] = u
    if not (it.get("body") or it.get("url")):
        raise ValueError("正文和链接至少留一个")
    _write(slug, "docs.json", {"items": items})
    return {"title": it["title"], "kind": it["kind"]}


def rename_file(slug: str | None, name: str, new_name: str) -> str:
    """附件改名。扩展名必须保留、不能覆盖已有文件。

    附件是按文件名引用的，所以改名就等于移动文件——这里拒绝任何路径成分，
    免得「../」把文件挪出赛事目录。
    """
    slug = active_slug(slug)
    d = _dir(slug)
    src = d / name
    if not src.is_file():
        raise ValueError("附件不存在：%s" % name)
    if src.suffix.lower() not in ATTACH_EXT:
        raise ValueError("不支持的类型：%s" % src.suffix)
    raw = (new_name or "").strip().replace("\\", "/")
    raw = raw.split("/")[-1].strip()
    if not raw:
        raise ValueError("新名字不能为空")
    stem = Path(raw).stem.strip()
    if not stem:
        raise ValueError("新名字不能为空")
    if len(stem) > 60:
        raise ValueError("新名字太长了（上限 60 字）")
    if stem.startswith("."):
        raise ValueError("新名字不能以点开头")
    dst = d / (stem + src.suffix.lower())
    if dst.name == src.name:
        return dst.name
    if dst.exists():
        raise ValueError("已经有同名附件了：%s" % dst.name)
    src.rename(dst)
    return dst.name


def remove_file(slug: str | None, name: str) -> str:
    """删掉一个附件，返回被删的文件名。

    这里是真删（不是移到回收站）：附件目录本身就在赛事档案里，删错了
    只能重传。所以前端必须带确认，服务端这边把三道关都卡住：

    1. 只认 basename，任何路径成分一律拒绝（不剥掉，直接报错——删文件
       这件事上，「猜用户想删哪个」比报错危险得多）；
    2. 扩展名必须在白名单里（跟上传、改名、读取共用一套）；
    3. resolve 之后必须还在赛事目录内，挡住符号链接之类的绕路。
    """
    slug = active_slug(slug)
    raw = (name or "").strip()
    if not raw:
        raise ValueError("没指定要删哪个附件")
    base = Path(raw.replace("\\", "/")).name
    if base != raw or base in (".", "..") or base.startswith("."):
        raise ValueError("文件名不合法：%s" % raw)
    d = _dir(slug).resolve()
    fp = (d / base).resolve()
    try:
        fp.relative_to(d)
    except ValueError:
        raise ValueError("文件名不合法：%s" % base)
    if not fp.is_file():
        raise ValueError("附件不存在：%s" % base)
    if fp.suffix.lower() not in ATTACH_EXT:
        raise ValueError("不支持的类型：%s" % fp.suffix)
    fp.unlink()
    return base


# ---------------------------------------------------------------- 时间点识别

# 三种常见写法，都允许后面跟 HH:MM / HH：MM：
#   2026-11-20 18:00 / 2026年11月20日 / 11月20日 18:00 / 11-20
# 单独的 HH:MM 不认——没有日期的时间点在时间线上没法排序，认了只会更乱。
_TIME_RES = (
    re.compile(r"(20\d{2})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})\s*日?"
               r"(?:\s*[T ]?\s*(\d{1,2})\s*[:：]\s*(\d{2}))?"),
    re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日"
               r"(?:\s*[T ]?\s*(\d{1,2})\s*[:：]\s*(\d{2}))?"),
    # 最后一条最容易误伤：「配速 4-5 分」「心率 150-160」不是日期，
    # 所以后面紧跟单位的一律不认。
    re.compile(r"(?<![\d.])(\d{1,2})[-.](\d{1,2})(?![\d])"
               r"(?:\s*[T ]?\s*(\d{1,2})\s*[:：]\s*(\d{2}))?"
               r"(?!\s*(?:分|小时|km|公里|kg|%|次|个|天|周|倍))"),
)

_LABEL_TRIM = " \t　，。；：、（）()【】[]<>《》-—·…•>"


def parse_times(text: str, year: int | None = None) -> list[dict]:
    """从一段文字里认出具体时间点，返回 [{at, label, date_only}]，按时间升序。

    `label` 取日期前面那一小截（「报名截止」「领物」），没有就用「时间点」。
    认不出来就返回空列表，不猜——宁缺勿错。
    """
    y0 = int(year or dt.date.today().year)
    out, seen = [], set()
    for line in str(text or "").splitlines():
        for rx in _TIME_RES:
            for m in rx.finditer(line):
                g = m.groups()
                if len(g) == 5:
                    y, mo, da, hh, mm = g
                else:
                    y, (mo, da, hh, mm) = y0, g
                try:
                    yi, mo_i, da_i = int(y), int(mo), int(da)
                    hi = int(hh) if hh else 0
                    mi = int(mm) if mm else 0
                    when = dt.datetime(yi, mo_i, da_i, hi, mi)
                except (TypeError, ValueError):
                    continue
                at = when.strftime("%Y-%m-%dT%H:%M")
                if at in seen:
                    continue
                # 标签取「同一句里、日期前面」那一小截：先按标点切成句，
                # 再取最后一段，避免把上一句的「11-18 18:00，」也带进来。
                _cut = re.split(r"[，。；;、\n]", line[:m.start()])[-1]
                label = _cut.strip(_LABEL_TRIM)[-14:].strip() or "时间点"
                seen.add(at)
                out.append({"at": at, "label": label,
                            "date_only": not (hh and mm), "raw": m.group(0)})
    out.sort(key=lambda x: x["at"])
    return out


def timeline(slug: str | None = None, *, today: str | None = None,
             horizon_days: int = 180) -> list[dict]:
    """一场赛事的时间线：开赛 + 资料正文里认出来的时间点。

    只用来「提醒」，不写回文件：资料随时会改，缓存只会让时间对不上。
    """
    slug = active_slug(slug)
    ev = _read(slug, "event.json") or {}
    d0 = str(ev.get("date") or "")[:10]
    year = int(d0[:4]) if d0[:4].isdigit() else dt.date.today().year
    rows = []
    if d0:
        t0 = str(ev.get("start_time") or "00:00")[:5]
        rows.append({"at": f"{d0}T{t0}", "label": "开赛",
                     "src": "赛事", "slug": slug, "title": ev.get("name") or slug})
    for it in docs(slug).get("items") or []:
        text = (it.get("title") or "") + "\n" + (it.get("body") or "")
        for tp in parse_times(text, year):
            rows.append({"at": tp["at"], "label": tp["label"],
                         "src": it.get("title") or "资料",
                         "slug": slug, "title": ev.get("name") or slug})
    # 手填的行程也要进时间线：住宿几号入住、机票几点起飞，这些是用户自己
    # 确定的计划，比从资料里猜出来的更该推提醒（首页「临近」按它排）
    _seen_at = {r["at"] for r in rows}
    for it in trip_manual(slug):
        _at = str(it.get("at") or "")[:16]
        if not _at or _at in _seen_at:
            continue
        _seen_at.add(_at)
        rows.append({"at": _at, "label": str(it.get("label") or "行程")[:40],
                     "src": "行程", "slug": slug,
                     "title": ev.get("name") or slug})
    rows.sort(key=lambda x: x["at"])
    if today:
        try:
            end = (dt.date.fromisoformat(today) + dt.timedelta(days=horizon_days)).isoformat()
            rows = [r for r in rows if r["at"][:10] <= end]
        except ValueError:
            pass
    return rows


# 行程类关键词。只用来给识别出的时间点分类，不参与「有没有时间」的判断——
# 分类错了顶多图标不对，判错了才会漏掉提醒，两者的代价不一样。
_TRIP_KINDS = (
    ("flight", ("航班", "机票", "飞机", "机场", "登机", "值机", "起飞", "落地")),
    ("train", ("高铁", "动车", "火车", "列车", "车次", "站台", "出发站")),
    ("stay", ("酒店", "民宿", "客栈", "住宿", "入住", "退房", "宾馆", "青旅")),
    ("race", ("开赛", "起跑", "领物", "报到", "检录")),
)


def _trip_kind(label: str, text: str) -> str:
    blob = (label or "") + (text or "")
    for kind, words in _TRIP_KINDS:
        if any(w in blob for w in words):
            return kind
    return "other"


# ---------------------------------------------------------------- 行程与住宿（手填）

# 资料里认出来的时间点是「可能」，用户自己填的才是「确定」。住宿尤其如此：
# 订好了哪家酒店，就要能在赛道图上看到它离起点多远——光靠正文里一行字不够。
TRIP_KINDS = {
    "stay": "住宿",
    "flight": "航班",
    "train": "高铁",
    "race": "领物 / 开赛",
    "other": "其他",
}

# 预订提前量（天）：**从比赛日期**倒推「该订了」的日子。
# 锚点必须是赛事日，不能是行程日——要提醒人去订，就不能反过来先要求
# 他填好「订的是哪趟航班 / 住哪家酒店」，那等于订完了才催。
# 机票越早越便宜、高铁放票晚、酒店居中；0 = 这类不提醒。
# 默认值，每场赛事可在「行程与预订 → 改提前量」里覆盖。
BOOKING_LEAD = {"flight": 60, "train": 15, "stay": 45,
                "race": 0, "other": 0}
BOOKING_KINDS = ("flight", "train", "stay")
BOOKING_LABELS = {"flight": "订机票", "train": "订高铁票",
                  "stay": "订酒店"}
BOOKING_FILE = "trip_rules.json"
BOOKING_HOUR = "09:00"       # 到期当天早上提示，别拿 00:00 当提醒时刻

_TRIP_AT = "%Y-%m-%dT%H:%M"


def _norm_at(raw: str) -> str:
    """把填进来的时间归一成 `YYYY-MM-DDTHH:MM`。

    手机原生 datetime-local 给的就是这个格式，但复制粘贴来的常是
    `2027-03-25`、`2027/3/25 15:00`，这里都认；认不出就报错让人改，
    绝不替人猜一个日期——行程猜错比空着更糟。
    """
    s = str(raw or "").strip().replace("/", "-")
    if not s:
        raise ValueError("要填日期")
    s = re.sub(r"\s+", "T", s)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        s += "T00:00"
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})T(\d{1,2}):(\d{2})(?::\d{2})?", s)
    if not m:
        raise ValueError("时间写成 2027-03-25 15:00 这样的")
    y, mo, da, hh, mi = (int(x) for x in m.groups())
    try:
        return dt.datetime(y, mo, da, hh, mi).strftime(_TRIP_AT)
    except ValueError:
        raise ValueError("这个日期/时刻不存在：%s" % str(raw).strip())


def trip_manual(slug: str | None = None) -> list[dict]:
    """手填的行程条目（trip.json）。原样返回，不排序也不推断。"""
    slug = active_slug(slug)
    items = (_read(slug, "trip.json") or {}).get("items")
    if not isinstance(items, list):
        return []
    return [dict(x) for x in items if isinstance(x, dict)]


def _trip_save(slug: str, items: list[dict]) -> list[dict]:
    items = sorted(items, key=lambda x: str(x.get("at") or ""))
    _write(slug, "trip.json", {"items": items})
    return items


_TRIP_CITY_CACHE: dict[str, str] = {}


def _trip_city(slug: str) -> str:
    """用赛道起点反查城市，给地址解析加限定。

    「白鹿音乐广场」不加城市会被高德匹配到云南鹤庆县（差 2000 km），
    加上「成都」才落到四川彭州。手写地址的人多半只写店名，不限定城市
    很容易出一枚把整张地图取景拉坏的坐标。同一场赛事只查一次。
    """
    if slug in _TRIP_CITY_CACHE:
        return _TRIP_CITY_CACHE[slug]
    city = ""
    try:
        line = (_read(slug, "track_stats.json") or {}).get("polyline") or []
        if line:
            from . import basemap as BM

            region = BM.regeo(line[0][0], line[0][1])
            city = (region or "").split("·")[-1]
    except Exception:
        city = ""
    if city:
        _TRIP_CITY_CACHE[slug] = city
    return city


def _trip_plausible(slug: str, lat: float, lon: float) -> bool:
    """解析出的坐标离赛道太远就当没解析到，别让它把底图取景拉到外省。

    阈值按赛道跨度放宽：越野赛的住宿可能真在几十公里外的城里；
    但一个跑到 1000 km 外的结果一定是同名地点匹配错了，宁可不标。
    没有轨迹时无从判断，交给用户自己看地址文字。
    """
    try:
        line = (_read(slug, "track_stats.json") or {}).get("polyline") or []
        if len(line) < 2:
            return True

        def km(a, b):
            return math.hypot((a[0] - b[0]) * 111.0,
                              (a[1] - b[1]) * 111.0
                              * math.cos(math.radians((a[0] + b[0]) / 2)))

        step = max(1, len(line) // 60)
        near = min(km((lat, lon), p) for p in line[::step])
        span = max(km(line[0], p) for p in line[::step])
        return near <= max(80.0, span * 2.0)
    except Exception:
        return True


def _trip_locate(item: dict, slug: str | None = None) -> bool:
    """把 item["place"] 落到坐标上（WGS，跟轨迹同一套）。成功与否都返回。"""
    place = str(item.get("place") or "").strip()
    if not place:
        return False
    try:
        from . import basemap as BM

        hit = BM.geocode(place, city=_trip_city(active_slug(slug)))
    except Exception:
        hit = None
    if not hit:
        return False
    if not _trip_plausible(active_slug(slug), hit["lat"], hit["lon"]):
        return False
    item["lat"] = hit["lat"]
    item["lon"] = hit["lon"]
    item["geo"] = hit.get("name") or place
    return True


def _lead_days(raw, what: str = "提前量") -> int:
    """提前量只认 0~365 的整数；不替人猜「两周」这种写法。"""
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{what}要填整数天数")
    if not 0 <= v <= 365:
        raise ValueError(f"{what}填 0~365 天")
    return v


def trip_add(slug: str | None, kind: str, at: str, label: str,
             place: str = "", lead: str | None = None) -> dict:
    """加一条行程/住宿。`place` 填了才解析坐标。

    `lead` 是历史字段（1.3.1 前单条行程能覆盖预订提前量）。改按比赛日
    倒推后，提前量只跟赛事/类型走，单条行程不再有独立的待办；参数留着
    只为兼容旧调用和老 trip.json，值照存但**不参与任何提醒计算**。
    填进来的这条本身会被 bookings() 视为「这一类已经订好了」。
    """
    slug = active_slug(slug)
    kind = kind if kind in TRIP_KINDS else _trip_kind(label, label) or "other"
    label = str(label or "").strip()
    if not label:
        raise ValueError("写一句这是什么（例：入住 都江堰锦江之星）")
    item = {"kind": kind, "at": _norm_at(at), "label": label[:80],
            "place": str(place or "").strip()[:120]}
    if lead is not None and str(lead).strip():
        item["lead"] = _lead_days(lead, "这条的提前量")
    _trip_locate(item, slug)
    _trip_save(slug, trip_manual(slug) + [item])
    return item


def trip_geo(slug: str | None, idx: int, place: str = "") -> dict | None:
    """补/改地点并重新定位。地址第一次没认出来时用得上，不用删了重加。"""
    slug = active_slug(slug)
    items = trip_manual(slug)
    if not (0 <= idx < len(items)):
        raise ValueError("这条行程已经不存在了")
    if str(place or "").strip():
        items[idx]["place"] = str(place).strip()[:120]
    for k in ("lat", "lon", "geo"):
        items[idx].pop(k, None)
    _trip_locate(items[idx], slug)
    _trip_save(slug, items)
    return items[idx]


def trip_del(slug: str | None, idx: int) -> bool:
    slug = active_slug(slug)
    items = trip_manual(slug)
    if not (0 <= idx < len(items)):
        return False
    items.pop(idx)
    _trip_save(slug, items)
    return True


def trip_pins(slug: str | None = None) -> list[dict]:
    """有坐标的手填行程，用来在赛道俯视图上打点（按时间排序）。"""
    out = []
    for it in trip_manual(slug):
        lat, lon = it.get("lat"), it.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        out.append({"kind": it.get("kind") or "other",
                    "label": str(it.get("label") or ""),
                    "place": it.get("place") or "",
                    "at": it.get("at") or "",
                    "lat": float(lat), "lon": float(lon)})
    return out


def trip_book(slug: str | None, idx: int, on: bool = True) -> dict | None:
    """兼容旧页面缓存的 `book/unbook:idx`：转成按类型的覆盖。

    1.3.1 前「已订」写在某条行程的 booked 字段上；现在待办不绑行程，
    所以这里不再改 trip.json，只把那一条的类型转发给 set_booking_done()。
    """
    slug = active_slug(slug)
    items = trip_manual(slug)
    if not (0 <= idx < len(items)):
        return None
    kind = items[idx].get("kind")
    if kind in BOOKING_KINDS:
        set_booking_done(slug, kind, done=bool(on))
    out = dict(items[idx])
    out["booked"] = bool(on)
    return out


def _booking_prefs(slug: str) -> dict:
    """赛事级的手动覆盖：done = 不用再催，todo = 恢复提醒。

    done 给「本地比赛不用坐飞机」这种情形，没填行程也能关掉提醒；
    todo 给「填了行程但只是计划、票还没买」的情形，能盖掉自动已订。
    两个都显式写下来，优先级高于自动判定，免得用户改完又被系统覆盖。
    """
    raw = _read(slug, BOOKING_FILE) or {}

    def _set(key: str) -> set:
        v = raw.get(key)
        return ({k for k in v if k in BOOKING_KINDS}
                if isinstance(v, list) else set())

    return {"done": _set("done"), "todo": _set("todo")}


def set_booking_done(slug: str | None, kind: str, done: bool = True) -> dict:
    """标「已订 / 不用提醒」或「仍需提醒」，按**类型**而非行程下标。

    按 idx 会漂：同一类常有去程+返程两条，删一条下标就全错；何况现在
    待办压根不绑具体行程。恢复提醒要同时清 done、加 todo，否则
    「填了行程自动视为已订」会立刻把状态盖回去。
    """
    slug = active_slug(slug)
    if kind not in BOOKING_KINDS:
        raise ValueError("只能改航班 / 高铁 / 住宿的预订状态")
    data = _read(slug, BOOKING_FILE) or {}
    prefs = _booking_prefs(slug)
    if done:
        prefs["done"].add(kind)
        prefs["todo"].discard(kind)
    else:
        prefs["done"].discard(kind)
        prefs["todo"].add(kind)
    for key in ("done", "todo"):
        if prefs[key]:
            data[key] = sorted(prefs[key])
        else:
            data.pop(key, None)
    _write(slug, BOOKING_FILE, data)
    return {k: sorted(v) for k, v in prefs.items()}


def booking_rules(slug: str | None = None) -> dict:
    """这场赛事的预订提前量（天）。没改过就用 BOOKING_LEAD。"""
    slug = active_slug(slug)
    saved = (_read(slug, BOOKING_FILE) or {}).get("lead") or {}
    out = dict(BOOKING_LEAD)
    for k, v in saved.items():
        if k in out and isinstance(v, int) and 0 <= v <= 365:
            out[k] = v
    return out


def set_booking_rules(slug: str | None, lead: dict) -> dict:
    """改提前量。空值 = 这一项保持不变；填了就必须合法，不静默吞掉。"""
    slug = active_slug(slug)
    cur = booking_rules(slug)
    for k, raw in (lead or {}).items():
        if k not in cur or raw is None or not str(raw).strip():
            continue
        cur[k] = _lead_days(raw, TRIP_KINDS.get(k, k) + "提前量")
    # 同一个文件还存着「不用提醒 / 仍需提醒」的覆盖，整包覆盖会把它们抹掉
    data = _read(slug, BOOKING_FILE) or {}
    data["lead"] = cur
    _write(slug, BOOKING_FILE, data)
    return cur


def bookings(slug: str | None = None, *, today: str | None = None,
             include_booked: bool = True) -> list[dict]:
    """按**比赛日期**倒推的三类预订提醒：机票 / 高铁票 / 酒店。

    1.3.1 前是「每条手填行程的发生日 - 提前量」，逻辑是反的：要提醒人
    去订，却先要求他填好订的哪趟航班、住哪家酒店；填完明明已经订了，
    还得再手动点一次「标为已订」才不催。现在：

    * 锚点 = event.json 的比赛日，跟有没有填行程无关，建好赛事就有提醒；
    * 填了**同类型**的手填行程 → 自动视为已订，提醒和日历事件一起消失；
    * 手动覆盖：done（不用提醒）/ todo（仍需提醒，盖掉自动已订）；
    * due 过了仍留在赛事页（最该催的就是这条），但不往过去插日历事件。

    只认手填行程：资料正文里认出来的「推荐酒店」「建议航班」不是用户
    已经订好的东西，拿它当已订会漏掉真提醒。
    """
    slug = active_slug(slug)
    t0 = dt.date.fromisoformat(today) if today else dt.date.today()
    ev = _read(slug, "event.json") or {}
    try:
        race_day = dt.date.fromisoformat(str(ev.get("date") or "")[:10])
    except ValueError:
        return []
    if race_day < t0:            # 赛后不再催预订，历史赛事页保持干净
        return []
    rules = booking_rules(slug)
    prefs = _booking_prefs(slug)
    manual_kinds = {it.get("kind") for it in trip_manual(slug)}
    out = []
    for kind in BOOKING_KINDS:
        lead = rules.get(kind, 0)
        if not isinstance(lead, int) or lead <= 0:
            continue
        due = race_day - dt.timedelta(days=lead)
        auto = kind in manual_kinds
        done = kind in prefs["done"]
        todo = kind in prefs["todo"]
        booked = done or (auto and not todo)
        reason = ("已标记不用提醒" if done else
                  ("已填行程，自动视为已订" if auto else ""))
        days = (due - t0).days
        out.append({
            "at": due.isoformat() + "T" + BOOKING_HOUR,
            "due": due.isoformat(), "days": days, "lead": lead,
            "kind": kind, "label": BOOKING_LABELS[kind],
            "race": race_day.isoformat(),
            "booked": booked, "auto": auto and not done,
            "reason": reason, "overdue": days < 0, "booking": True,
            "src": "预订", "slug": slug, "title": ev.get("name") or slug,
            "event": ev.get("short") or ev.get("name") or slug,
        })
    # 未订的按截止日排前面；已订的沉到底但保留可见，方便反悔/看清状态
    out.sort(key=lambda x: (x["booked"], x["due"]))
    return out if include_booked else [x for x in out if not x["booked"]]


def trips(slug: str | None = None, *, today: str | None = None,
          horizon_days: int = 120) -> list[dict]:
    """赛事资料里的「行程与预订」：航班 / 高铁 / 住宿 / 开赛 / 其他。

    和 timeline() 的区别是**带分类和倒计时**：赛前最容易忘的不是比赛本身，
    是机票什么时候订、酒店几号入住、领物几点关，这些东西写进资料以后
    散在正文里，临到头谁也想不起来翻。

    分类只认关键词，认不出就归 other（照样显示，只是没图标）——
    宁可分类不准，也不要因为分错类而漏掉一条提醒。
    """
    slug = active_slug(slug)
    t0 = dt.date.fromisoformat(today) if today else dt.date.today()
    # 年份必须按**赛事年份**推：赛前踩点写的「3月25日」在去年 12 月看，
    # 按当年算就成了「今年 3 月 25 日」——早就过去了，整条提醒直接消失。
    ev = _read(slug, "event.json") or {}
    _d0 = str(ev.get("date") or "")[:10]
    year = int(_d0[:4]) if _d0[:4].isdigit() else t0.year
    # 视界至少覆盖到赛事前后：一场明年 3 月的比赛，今天看还有 130 多天，
    # 按固定 120 天会把「提前 3 天到」的那条机票漏掉。
    try:
        race_day = dt.date.fromisoformat(_d0)
        need = (race_day - t0).days + 7
    except ValueError:
        need = 0
    end = t0 + dt.timedelta(days=max(1, int(horizon_days), need))
    out, seen = [], set()
    # 手填的排在最前，而且**不过时间窗**：用户明确写进来的，哪怕日期是
    # 上个月（补记行程）也要看得见；资料里猜出来的才按窗口裁。
    manual = trip_manual(slug)
    for i, it in enumerate(manual):
        try:
            when = dt.datetime.strptime(str(it.get("at") or "")[:16], _TRIP_AT)
        except ValueError:
            continue
        kind = it.get("kind") if it.get("kind") in TRIP_KINDS else "other"
        out.append({
            "at": when.strftime(_TRIP_AT),
            "label": str(it.get("label") or "行程"),
            "source": "手填",
            "kind": kind,
            "days": (when.date() - t0).days,
            "date_only": when.strftime("%H:%M") == "00:00",
            "text": str(it.get("label") or "")[:80],
            "manual": True, "idx": i,
            "place": str(it.get("place") or ""),
            "lat": it.get("lat"), "lon": it.get("lon"),
            # 填进来的机票/高铁/住宿本身就是预订信息，bookings() 会据此
            # 自动视为已订，列表里的标签要跟着一致，别让人以为漏标了。
            "booked": bool(it.get("booked")) or kind in BOOKING_KINDS,
            "lead": it.get("lead") if isinstance(it.get("lead"), int) else None,
        })
    # 手填过的「同一类、同一天」就不再从资料里重复提醒一遍
    _manual_day = {(x["kind"], x["at"][:10]) for x in out}
    for it in docs(slug).get("items") or []:
        title = str(it.get("title") or "")
        body = str(it.get("body") or "")
        # **逐行**识别，不是整段识别后再回头找行：整段识别时「3月25日」
        # 会先命中「航班 ... 3月25日 08:30」那一行，于是酒店那条被当成航班
        # （分类和说明文字全跟着错）。按行来，一行就是一件事。
        for raw_line in (title + "\n" + body).splitlines():
            line = raw_line.strip()
            if not line:
                continue
            for tp in parse_times(line, year):
                try:
                    when = dt.datetime.strptime(tp["at"], "%Y-%m-%dT%H:%M")
                except ValueError:
                    continue
                if when.date() < t0 or when.date() > end:
                    continue
                key = tp["at"]
                if key in seen:
                    continue
                _kind = _trip_kind(tp["label"], line)
                if (_kind, tp["at"][:10]) in _manual_day:
                    continue
                seen.add(key)
                out.append({
                    "at": tp["at"], "label": tp["label"], "source": title,
                    "kind": _kind,
                    "days": (when.date() - t0).days,
                    "date_only": tp.get("date_only"),
                    "text": line[:80],
                })
    # 开赛本身排最后：它不会「忘记」，真正要提醒的是前面那些预订
    out.sort(key=lambda x: (x["kind"] == "race", x["at"]))
    return out


def upcoming(days: int = 45, today: str | None = None) -> list[dict]:
    """所有赛事里未来 N 天的时间点 + 该订的待办，首页「临近」用。"""
    t0 = dt.date.fromisoformat(today) if today else dt.date.today()
    win = max(1, int(days))
    end = t0 + dt.timedelta(days=win)
    out = []
    for e in list_events(today=t0):
        slug = e.get("slug")
        if not slug:
            continue
        try:
            rows = timeline(slug)
        except Exception:
            continue
        for r in rows:
            try:
                when = dt.datetime.strptime(r["at"], "%Y-%m-%dT%H:%M")
            except ValueError:
                continue
            if when.date() < t0 or when.date() > end:
                continue
            r = dict(r)
            r["days"] = (when.date() - t0).days
            r["event"] = e.get("short") or e.get("name") or slug
            out.append(r)
        # 「该订了」比「要起飞了」更容易忘：过期的还留 7 天，再久就别占首页，
        # 但赛事页的待办区始终保留，不会凭空消失。
        try:
            bks = bookings(slug, today=t0.isoformat(), include_booked=False)
        except Exception:
            bks = []
        for b in bks:
            if b["days"] < -7 or b["days"] > win:
                continue
            out.append({"at": b["at"], "label": b["label"],
                        "days": b["days"], "booking": True,
                        "kind": b["kind"], "due": b["due"],
                        "src": "预订", "slug": slug,
                        "title": b["title"], "event": b["event"]})
    out.sort(key=lambda x: x["at"])
    return out


# ---------------------------------------------------------------- 系统日历
# 交给 Kotlin 写进系统日历的清单。Python 只算「哪天、提醒什么、提前多久」，
# 原生只管按 uid 增删改事件——倒推规则绝不在两端各写一遍。
# 系统日历负责真正弹出通知：不需要后台常驻、不需要精确闹钟权限，
# 也不会被国产 ROM 的电池优化掐掉。
CAL_MARK = "#RunnerCoach"
CAL_UID_KEY = "rcuid="
CAL_DEFAULT_HH = "09:00"      # 只填日期没填时刻，别把提醒定在半夜
CAL_TRIP_MINUTES = {"flight": [1440, 180], "train": [180],
                    "stay": [180], "race": [1440, 120],
                    "other": [120]}


def _cal_uid(prefix: str, *parts: str) -> str:
    """稳定 id：内容变了就是另一件事（旧事件会被删、新事件会建）。

    不用数组下标：删掉前面一条行程，后面所有下标都会漂，同步时就会把
    整个日历删一遍再建一遍。用内容 hash，改期/改说明才换 id。
    """
    import hashlib

    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return prefix + hashlib.sha1(raw).hexdigest()[:16]


def calendar_items(today: str | None = None) -> list[dict]:
    """所有赛事里要写进系统日历的事件清单。

    两类：① 未订的预订提醒（按比赛日倒推，截止日当天 09:00 弹）；
    ② 已确定下来的手填行程本身（航班/高铁/住宿/开赛，按类型提前提醒）。
    过期的待办不写——往过去插事件最多弹一次骚扰通知，交给赛事页去催；
    已订的待办不写：填了行程或标「不用提醒」后，下次同步原生会把
    清单里消失的 uid 从日历删掉，通知自然就停了。
    """
    t0 = dt.date.fromisoformat(today) if today else dt.date.today()
    out, seen = [], set()

    def add(item: dict) -> None:
        if item["uid"] not in seen:
            seen.add(item["uid"])
            out.append(item)

    for e in list_events(today=t0):
        slug = e.get("slug")
        if not slug:
            continue
        name = str(e.get("short") or e.get("name") or slug)[:16]
        try:
            bks = bookings(slug, today=t0.isoformat(), include_booked=False)
        except Exception:
            bks = []
        for b in bks:
            if b["days"] < 0:
                continue
            uid = _cal_uid("bk:", slug, b["kind"], b["race"],
                           b["due"], b["lead"])
            desc = (f"{name} · 比赛日 {b['race']}\n"
                    f"该{b['label']}了（提前 {b['lead']} 天，最晚 {b['due']}）\n"
                    "在 RunnerCoach 赛事页填一条对应类型的行程，或点「不用提醒」，"
                    "这条会自动从日历移除。\n"
                    f"{CAL_MARK} {CAL_UID_KEY}{uid}")
            add({"uid": uid, "type": "booking", "kind": b["kind"],
                 "title": f"该订了 · {name} · {TRIP_KINDS.get(b['kind'], '行程')}"[:70],
                 "desc": desc, "at": b["at"], "minutes": [0],
                 "duration": 30, "slug": slug, "event": name})
        try:
            manual = trip_manual(slug)
        except Exception:
            manual = []
        for it in manual:
            raw_at = str(it.get("at") or "")[:16]
            try:
                when = dt.datetime.strptime(raw_at, _TRIP_AT)
            except ValueError:
                continue
            if when.date() < t0:
                continue
            kind = it.get("kind") if it.get("kind") in TRIP_KINDS else "other"
            label = str(it.get("label") or "行程")[:40]
            at = when.strftime(_TRIP_AT)
            hint = ""
            if at[11:16] == "00:00":
                at = at[:11] + CAL_DEFAULT_HH
                hint = "（只填了日期，按 09:00 记）"
            uid = _cal_uid("tp:", slug, kind, raw_at, label)
            place = str(it.get("place") or "").strip()[:60]
            desc = (f"{name} · {TRIP_KINDS.get(kind, '行程')}\n"
                    f"{label}\n行程时间 {raw_at.replace('T', ' ')}{hint}"
                    + (f"\n地点 {place}" if place else "")
                    + f"\n{CAL_MARK} {CAL_UID_KEY}{uid}")
            add({"uid": uid, "type": "trip", "kind": kind,
                 "title": f"{TRIP_KINDS.get(kind, '行程')} · {name} · {label}"[:70],
                 "desc": desc, "at": at,
                 "minutes": list(CAL_TRIP_MINUTES.get(kind, [120])),
                 "duration": 60, "slug": slug, "event": name})
    out.sort(key=lambda x: (x["at"], x["uid"]))
    return out


def reminders(slug: str | None = None) -> dict:
    """已设提醒：{"<at>|<label>": true}。关掉就删键，不留空壳。"""
    slug = active_slug(slug)
    return (_read(slug, "remind.json") or {}).get("on") or {}


def set_remind(slug: str | None, at: str, label: str, on: bool = True) -> dict:
    slug = active_slug(slug)
    data = _read(slug, "remind.json") or {}
    cur = dict(data.get("on") or {})
    key = "%s|%s" % (at, label)
    if on:
        cur[key] = True
    else:
        cur.pop(key, None)
    _write(slug, "remind.json", {"on": cur})
    return cur


# ---------------------------------------------------------------- 赛中状态

# 赛中不写 event.json：比赛当天数据要能一键清空重来，单独一个文件更干净。
# {"start": "2027-03-27T06:00:00", "passed": {"1": "2027-03-27T08:12:00", ...}}
RACE_ACTIONS = ("start", "pass", "unpass", "reset", "finish")


def race_state(slug: str | None = None) -> dict:
    slug = active_slug(slug)
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
    slug = active_slug(slug)
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
    slug = active_slug(slug)
    ev = load_event(slug)
    st = race_state(slug)
    rows = plan_and_margin(slug)
    plan_start = dt.datetime.combine(dt.date.fromisoformat(ev["date"]),
                                     dt.time(*map(int, ev["start_time"].split(":"))))
    start = dt.datetime.fromisoformat(st["start"]) if st.get("start") else plan_start
    now = (now or dt.datetime.now()).replace(microsecond=0)
    goal_s = int(ev.get("goal_s") or 22 * 3600)

    passed_all = {int(k): dt.datetime.fromisoformat(v)
                  for k, v in (st["passed"] or {}).items()}
    # 记录时间晚于「现在」只有两种可能：手机时间被改过，或换机后时钟错乱。
    # 这种点会让落后量变成垃圾数，直接忽略并提示，不参与任何计算。
    future = {k: v for k, v in passed_all.items()
              if v > now + dt.timedelta(minutes=2)}
    passed = {k: v for k, v in passed_all.items() if k not in future}
    warn = ""
    if future:
        warn = ("有 %d 个 CP 的到达时间晚于当前时间，已忽略：请检查手机时间"
                % len(future))
    last_idx = max(passed) if passed else 0
    last_actual = passed.get(last_idx)

    def plan_sec(idx: int) -> float:
        r = next((x for x in rows if x["idx"] == idx), None)
        return (r["arrive"] - plan_start).total_seconds() if r else 0.0

    # 落后量 = 实际用时 - 计划用时（同一基准，与年份无关）
    delay_s = 0.0
    if last_idx and last_actual:
        delay_s = (last_actual - start).total_seconds() - plan_sec(last_idx)
        if abs(delay_s) > 24 * 3600:
            # 超过一天基本只剩一种可能：比赛日期/手机时钟对不上
            warn = warn or "时间基准异常（落后量超过 24 h），请检查比赛日期与手机时间"
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
            "delta_s": delta,
            # 没有官方关门就没有余量，不能拿 0 或 now 硬凑
            "margin_s": ((r["cutoff"] - at).total_seconds() if r["cutoff"] else None),
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
    slug = active_slug(slug)
    ev = load_event(slug)
    cfg = dict(FUEL_DEFAULT)
    cfg.update(ev.get("fuel") or {})
    limits = {"carb_g_h": (30, 120), "fluid_ml_h": (200, 1000),
              "sodium_mg_h": (200, 1500), "gel_g": (10, 40),
              "stop_min": (0, 30), "dropbag_stop_min": (0, 60),
              "temp_c": (-20, 40), "carry_ratio": (0.2, 1.0),
              # 下限给 0：路跑补给站密，本来就不用带水；以前卡 500，
              # 想填 0 的人会被原生校验直接顶回来（用户反馈）。
              "carry_ml": (0, 3000)}
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
    slug = active_slug(slug)
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
    slug = active_slug(slug)
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
    slug = active_slug(slug)
    p = _dir(slug) / "gear.md"
    if not p.exists():
        seed_gear(slug)
    txt = io.open(p, encoding="utf-8").read()
    total = len(re.findall(r"^- \[[ xX]\]", txt, re.M))
    done = len(re.findall(r"^- \[[xX]\]", txt, re.M))
    missing = [l.strip()[6:].strip() for l in txt.splitlines()
               if re.match(r"^- \[ \]", l.strip())]
    return {"done": done, "total": total, "missing": missing}


# 赛道俯视图的画幅（2x 屏）。定成常量是因为底图缓存的文件名带着它，
# 渲染页和导入时的预取必须用同一组数字，否则永远命中不了缓存。
MAP_W, MAP_H = 720, 360


def map_ident(slug: str | None = None) -> str:
    """底图缓存的 ident：`ev-<slug>`，只留文件名安全的字符。

    这里**不校验赛事是否真的存在**：它只负责拼一个文件名，校验是 _dir 的事。
    顺手也不依赖调用上下文（不传 slug 时才去问 active_slug）。
    """
    raw = str(slug or "") or str(active_slug(None))
    return "ev-" + re.sub(r"[^A-Za-z0-9._-]+", "-", raw)[:40]


def thin_track(rows: list, n: int = 240) -> list:
    """把 GPS 点抽稀成平面轨迹（[[lat, lon], ...]）。

    240 点足够画出弯道和折返——马拉松全程也就 42 km。不抽稀的话，一个
    5 万点的 GPX 塞进 track_stats.json 就是几 MB，而页面每次渲染都要读它。
    """
    if len(rows) < 2:
        return []
    if len(rows) <= n:
        pick = rows
    else:
        step = (len(rows) - 1) / (n - 1)
        pick = [rows[int(round(i * step))] for i in range(n)]
    return [[round(float(a), 5), round(float(b), 5)] for a, b, _ in pick]


# ---------------------------------------------------------------- GPX

def parse_gpx(path: str | Path, slug: str | None = None,
              save: bool = True) -> dict:
    """解析 GPX：滤波累计爬升 + 500 m 剖面 + CP 就近匹配。

    坑（设计稿 §7.3）：GPS 高程噪声 ±3–5 m，直接累加会把 5547 m 算成 8000+。
    处理：5 点滑动平均后，只累计上升超过 3 m 的段落。

    三种写法都要认（用户报过「GPX 里少于 10 个轨迹点」，就是只认了第一种）：
      · <trk><trkseg><trkpt>  运动手表导出，最常见
      · <rte><rtept>          官方「路线」文件，赛事方给的多半是这个
      · <wpt>                 只有航点没有线的兜底
    命名空间也不写死：GPX 1.0 / 1.1 / 无命名空间都用 `{*}tag` 通配，
    1.0 的文件按 1.1 取同样会得到 0 个点。
    """
    tree = ET.parse(path)

    def _local(el) -> str:
        # ElementTree 的 iter() **不认** `{*}` 通配（那是 find/findall 的语法），
        # 所以按 local name 自己比。命名空间可能是 1.1 / 1.0 / 没有。
        return str(el.tag).rsplit("}", 1)[-1]

    def _child_text(el, name: str):
        for ch in el:
            if _local(ch) == name:
                return ch.text
        return None

    buckets: dict[str, list] = {"trkpt": [], "rtept": [], "wpt": []}
    for node in tree.iter():
        name = _local(node)
        if name not in buckets:
            continue
        try:
            lat, lon = float(node.get("lat")), float(node.get("lon"))
        except (TypeError, ValueError):
            continue
        try:
            raw = _child_text(node, "ele")
            z = float(raw) if raw else None
        except (TypeError, ValueError):
            z = None
        buckets[name].append((lat, lon, z))

    pts, kind = [], ""
    for tag in ("trkpt", "rtept", "wpt"):
        got = buckets[tag]
        if len(got) > len(pts):
            pts, kind = got, tag
        if len(pts) >= 10:
            break
    if len(pts) < 10:
        # 报错要说清「文件里到底有什么」，否则用户只会看到一句无从下手的话
        raise ValueError(
            "这个 GPX 里只找到 %d 个坐标点（trkpt/rtept/wpt 都试过），"
            "少于 10 个没法算距离。可能不是轨迹文件，或者被截断了。" % len(pts))

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
    for wpt in tree.iter():
        if _local(wpt) != "wpt":
            continue
        nm = _child_text(wpt, "name")
        try:
            raw_w.append((float(wpt.get("lat")), float(wpt.get("lon")),
                          (nm or "").strip()))
        except (TypeError, ValueError):
            continue
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
        # 俯视图用：抽稀后的平面轨迹。老数据没有这个字段，渲染时补一次
        polyline=thin_track(pts),
    )
    if save:
        import shutil
        dst = _dir(slug) / "track.gpx"
        dst.parent.mkdir(parents=True, exist_ok=True)
        # 服务端上传时文件已经落在目标位置了，别再 copy 到自己身上
        if Path(path).resolve() != dst.resolve():
            shutil.copy(path, dst)
        _write(slug, "track_stats.json", out)
        # 趁用户还在等导入结果，顺手把底图取回来：渲染时才去联网的话，
        # 打开赛事页会白等几秒（而且失败还不知道为什么）。
        try:
            from . import basemap as _BM

            if _BM.available() and out["polyline"]:
                _BM.ensure(out["polyline"], MAP_W, MAP_H,
                           ident=map_ident(slug), timeout=8)
        except Exception:
            pass
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
    slug = active_slug(slug)
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
    known = [r for r in rows if r.get("margin_s") is not None]
    tightest = min(known, key=lambda r: r["margin_s"]) if known else None
    course = load_course(ev["slug"])
    return dict(
        slug=ev["slug"],
        name=ev["name"], short=ev.get("short") or ev["name"],
        priority=ev.get("priority") or "C", role=ev.get("role") or "",
        status=ev["status"],
        status_cn=STATUS_CN.get(ev.get("status"), ev.get("status", "")),
        days=(race - today).days,
        race=race,
        dist=ev["distance_km"], ascent=ev["ascent_m"],
        goal_h=(ev.get("goal_s") or 0) / 3600,
        duration_h=ev.get("duration_h"),
        has_course=len(course.get("segments") or []) > 1,
        cps=len(course.get("segments") or []),
        # 手工建的赛事可能只填了名字和日期，不能因为缺字段就炸整页
        cutoff=ev.get("cutoff_time"),
        venue=ev.get("venue") or "",
        gear_done=gear["done"], gear_total=gear["total"],
        gear_missing=gear["missing"],
        tightest=tightest,
    )
