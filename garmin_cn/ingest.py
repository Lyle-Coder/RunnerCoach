# -*- coding: utf-8 -*-
"""赛事导入：把「用户手上已有的东西」变成可用的档案，并且不放过错数据。

这个模块只做三件事，但每件事都必须成立，否则宁可让人继续手填：

1. **少问**（from_*）——用户只需要给一个入口：一段规程文字、一张海报 OCR 出来的
   文本、上一届的档案、或一条 GPX。其余全部由这里推。少问的边界是「不问就编」，
   所以推不出来的字段一律留空，不填默认值。
2. **挑错**（validate）——真实性靠来源，有效性靠物理约束。四个硬约束：
   CP 里程必须递增、末点必须等于总距离、关门必须递增且落在总关门内、
   每段的「最低要求速度」必须在人类区间。最后一条是抓 OCR 错位的主力：
   把 12:45 认成 02:45、或漏掉一个数字，都会立刻表现为「这段要跑 18 km/h」。
3. **留痕**（apply）——落盘时写 provenance（来源 / 时间 / 谁确认的），
   页面上把「官方发布的」和「你自己填的」分开显示。没有来源的关门时间，
   不给它配红色「会被关门」的告警。

设计取舍：不做「自动联网搜索赛事」。赛事规程大多在报名平台的小程序里、
需要登录、还常改版，抓取成功率低且错了没法追责。让人贴一段文字，
比让程序猜一个 404 页面可靠得多。
"""
from __future__ import annotations

import datetime as dt
import io
import re

# 关门可行性的物理边界（等效速度 = (距离 + 爬升/100) / 小时）
# 下限：再宽松的关门也不会低于 1.5 km/h 等效——低于这个数多半是把「12:45」认成「12:00」
# 上限：越野的世界级水平约 12–13 km/h 等效，超过这个数说明数据错了，不是人强
MIN_EQ_SPEED = 1.5
MAX_EQ_SPEED = 13.0

# 里程与累计爬升的对账容差
KM_TOL = 0.2
ASCENT_TOL = 0.15


def _clock(s: str) -> dt.time | None:
    """'8:05' / '08：05' / '8.05' → time；不认识返回 None。"""
    m = re.fullmatch(r"\s*(\d{1,2})\s*[:：.．]\s*(\d{2})\s*", str(s or ""))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return dt.time(h, mi)


def _dur(s: str) -> int | None:
    """'5h30' / '5h' / '5小时30分' / '330min' → 秒；不认识返回 None。"""
    t = str(s or "").strip()
    m = re.fullmatch(r"(\d{1,2})\s*[hH小时]\s*(?:(\d{1,2})\s*[mM分]?)?", t)
    if m:
        return (int(m.group(1)) * 3600 + int(m.group(2) or 0) * 60)
    m = re.fullmatch(r"(\d{2,4})\s*(?:min|分钟)", t)
    if m:
        return int(m.group(1)) * 60
    return None


def _norm_digits(s: str) -> str:
    """全角转半角（数字、字母、标点、全角空格）。

    规程里「ＣＰ１　１０.９ｋｍ」这种混排很常见，转一次省掉后面所有兼容代码。
    注意要连字母一起转：只转数字时「ｋｍ」不是「km」，里程单位就认不出来。
    """
    out = []
    for ch in s:
        o = ord(ch)
        if 0xFF01 <= o <= 0xFF5E:
            out.append(chr(o - 0xFF01 + 0x21))
        elif ch == "\u3000":
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------- 从文本导入
#
# 不用大模型，但也不能只会认一种排版。规程里的 CP 表至少有四种写法：
#   CP1 仙山雅宿 10.9km 关门 08:05      （最理想）
#   CP1  仙山雅宿  10.9  515  08:05  有水（制表符 + 多列，单位全省）
#   CP1 仙山雅宿 07:30-08:05 关门        （开放 + 关门两个时间）
#   CP1 山顶 12.5 公里 2小时30分         （时长制）
# 做法是「先摘数、再摘名」：把时间/时长/里程从行里挖出来，剩下的文字就是地名。
# 这样比一条大正则可读，也能容纳多出来的列（爬升、补给、备注）。

_STOP_NAMES = ("总距离", "累计", "爬升", "下降", "关门", "起跑", "发枪", "日期",
               "海拔", "补给", "换装", "号码", "报名", "规程", "组别", "时限",
               "距离", "里程", "费用", "名额", "人数", "项目", "检查点")

# 地名后面容易粘上的列值/表头词，遇到就截断
_NAME_CUT = ("关门", "开放", "截止", "时间", "补给", "有水", "无水", "热食",
             "饮水", "厕所", "换装", "爬升", "下降", "备注", "累计")

_CLOCK = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")
_DUR = re.compile(r"(?<!\d)(\d{1,2})\s*(?:小时|h|H)\s*(?:(\d{1,2})\s*分?)?"
                  r"|(?<!\d)(\d{2,4})\s*(?:min|分钟)")
_KM_UNIT = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,2})?)\s*(?:km|KM|Km|公里|千米)")
# 只删「两边都是分隔符」的数字，避免把「2026年新设站」里的年份删掉
_LOOSE_NUM = re.compile(r"(?<=[\s\t|,;])\d{1,4}(?:\.\d{1,2})?(?=[\s\t|,;]|$)")
_IDX = re.compile(r"^\s*(?:CP|cp|Cp|检查点|打卡点|点位)\s*0*(\d{1,3})\b")
_IDX_TAIL = re.compile(r"(?:CP|cp|检查点|打卡点)\s*0*(\d{1,3})\s*$")
# 「约 10.5-11.2km」这种区间：统一取上界，且把整段区间从行里抹掉，
# 否则「约10.5」会被当成地名的一部分（实测出现过「仙山雅宿约105」）
_RANGE = re.compile(r"(?:约|大约)?\s*\d{1,3}(?:\.\d{1,2})?\s*[-~—至到]\s*"
                    r"(\d{1,3}(?:\.\d{1,2})?)\s*(?=(?:km|KM|公里|千米))")
# PDF 双列抽出后常见的「关门单独一行」：只有时间、没有里程
_BARE_CLOCK = re.compile(r"^\s*(?:关门|关门时间|截止|close)\s*[:：]?\s*"
                         r"(\d{1,2}):(\d{2})")


def _parse_row(line: str) -> dict | None:
    """把一行解析成 {idx, name, km, cutoff}；不是 CP 行就返回 None。

    宁可返回 None，也不要返回一行猜出来的数据——人行里带个「总距离」这种
    表头行，如果被当成 CP1，整张余量表就全错。
    """
    s0 = _norm_digits(line).replace("\u3000", " ").strip()
    if not s0 or len(s0) < 4:
        return None

    # 区间先归一：取上界，并把区间抹成单个数字
    s0 = _RANGE.sub(lambda mm: mm.group(1), s0)

    idx = None
    m = _IDX.match(s0)
    if m:
        idx = int(m.group(1))
    else:
        m = re.match(r"^\s*0*(\d{1,2})\s*[.、)）]\s+", s0)
        if m:
            idx = int(m.group(1))
        else:
            m = _IDX_TAIL.search(s0)          # 编号写在行尾的表格
            if m:
                idx = int(m.group(1))

    # 时间：范围写法取**最后一个**（规程里几乎都是「开放-关门」的顺序；
    # 取第一个会把开放时间当关门，余量凭空多出半小时以上）
    clocks = [(int(a), int(b)) for a, b in _CLOCK.findall(s0)]
    durs = []
    for mm in _DUR.finditer(s0):
        if mm.group(1):
            h, mi = int(mm.group(1)), int(mm.group(2) or 0)
            if h <= 47 and mi <= 59:
                durs.append(h * 3600 + mi * 60)
        elif mm.group(3):
            durs.append(int(mm.group(3)) * 60)

    if not _KM_UNIT.search(s0) and not re.search(r"(?<![\d.])\d{1,3}(?:\.\d{1,2})?(?![\d.])", _CLOCK.sub(" ", s0)):
        bc = _BARE_CLOCK.match(s0)
        if bc:
            h, mi = int(bc.group(1)), int(bc.group(2))
            if 0 <= h <= 23 and 0 <= mi <= 59:
                return {"bare_clock": "%02d:%02d" % (h, mi)}

    rest = _CLOCK.sub(" ", s0)
    rest = _DUR.sub(" ", rest)
    # 编号必须先摘掉：否则「CP1」里的 1 会被当成 1 km（实测踩过）
    rest = _IDX.sub(" ", rest)
    rest = _IDX_TAIL.sub(" ", rest)

    kms = [float(x) for x in _KM_UNIT.findall(s0)]
    rest = _KM_UNIT.sub(" ", rest)
    if not kms:
        # 没有单位的表格（制表符分隔）：取第一个 0–200 的独立数字当里程
        for mm in re.finditer(r"(?<![\d.])(\d{1,3}(?:\.\d{1,2})?)(?![\d.])", rest):
            v = float(mm.group(1))
            if 0 < v <= 200:
                kms = [v]
                rest = rest[:mm.start()] + " " + rest[mm.end():]
                break
    rest = _LOOSE_NUM.sub(" ", rest)

    name = re.sub(r"[^A-Za-z0-9\u4e00-\u9fa5·（）()+\-\s]", "", rest).strip()
    if re.search(r"[\u4e00-\u9fa5]", name):
        name = re.sub(r"\s+", "", name)      # 中文地名里的空格是无意义的
    else:
        name = re.sub(r"\s{2,}", " ", name).strip(" -+()")
    name = name.strip("·-+()（）")
    # 多出来的列（「有水」「热食」）和表头词会粘在地名后面，在关键字处截断
    for w in _NAME_CUT:
        if w in name:
            name = name.split(w)[0].strip("·-+()（）")
            break
    if not name or len(name) > 24:
        return None
    if idx is None and any(w in name for w in _STOP_NAMES):
        return None                      # 表头行，别当 CP
    if not kms:
        return None
    if idx is None and not (clocks or durs):
        return None                      # 既没编号也没时间，不像 CP 行

    if durs:
        d = durs[-1]
        cutoff = "%dh%02d" % (d // 3600, d % 3600 // 60) if d >= 3600 else "%dm" % (d // 60)
    elif clocks:
        h, mi = clocks[-1]
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            return None
        cutoff = "%02d:%02d" % (h, mi)
    else:
        cutoff = None
    return {"idx": idx, "name": name, "km": round(kms[0], 2), "cutoff": cutoff}


def from_text(text: str, ev: dict | None = None) -> dict:
    """从一段规程文字里抽赛事与 CP 表。

    逐行解析，认不出来的行直接跳过；整段认不到 2 个 CP 就返回空表——
    宁可让人手填，也不要猜出一份看起来合理、跑起来要命的赛道。
    """
    ev = ev or {}
    raw = _norm_digits(text or "")
    segs, seen = [], set()
    for line in raw.splitlines():
        row = _parse_row(line)
        if not row:
            continue
        if row.get("bare_clock"):
            # PDF 抽出后「关门 08:05」常常单独一行：挂到上一个还没关门的 CP 上
            for prev in reversed(segs):
                if not prev["cutoff"]:
                    prev["cutoff"] = row["bare_clock"]
                    break
            continue
        idx = row["idx"] or (len(segs) + 1)
        if idx in seen or idx < 1 or idx > 60:
            continue
        seen.add(idx)
        segs.append({"idx": idx, "name": row["name"], "km": row["km"],
                     "cutoff": row["cutoff"], "cutoff_day": 1, "source": "text"})
    segs.sort(key=lambda x: x["idx"])

    # 日期：2027-03-27 / 2027年3月27日
    date = None
    m = re.search(r"(20\d{2})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})", raw)
    if m:
        try:
            date = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            date = None

    # 总距离：只在有「距离/总长」字样时取，避免把最后一个 CP 的里程当总长
    dist = None
    m = re.search(r"(?:总)?(?:里程|距离|赛道全长)[^\d]{0,8}([\d]+(?:\.\d+)?)\s*(?:km|公里)", raw)
    if m:
        dist = float(m.group(1))
    elif segs:
        dist = max(s["km"] for s in segs)

    # 起跑时间：认「06:00 起跑」「起跑时间 06:00」两种写法
    start_time = None
    m = (re.search(r"(\d{1,2}[:：]\d{2})\s*(?:起跑|出发|发枪)", raw)
         or re.search(r"(?:起跑|出发|发枪)(?:时间)?[^\d\n]{0,4}(\d{1,2}[:：]\d{2})", raw))
    if not m:
        # 兜底：「2027年6月5日 20:00」——日期行后面直接跟钟点，就是起跑时间
        m = re.search(r"20\d{2}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?\s*(\d{1,2}[:：]\d{2})", raw)
    if m:
        tt = _clock(m.group(1))
        if tt:
            start_time = tt.strftime("%H:%M")

    # 总关门：条例里的写法五花八门，认「总关门 / 关门时间 / 时限」后跟的时长
    dur_h = None
    m = re.search(r"(?:总关门|关门时间|时限|关门)[^\d\n]{0,6}(\d{1,2}(?:\.\d)?)\s*(?:小时|h|H)", raw)
    if m:
        dur_h = float(m.group(1))

    # 累计爬升
    ascent = None
    m = re.search(r"(?:累计)?(?:爬升|D\+|上升)[^\d]{0,8}([\d,]+)\s*(?:m|米)?", raw)
    if m:
        ascent = int(m.group(1).replace(",", ""))
    return {"date": date, "distance_km": dist, "ascent_m": ascent,
            "duration_h": dur_h, "start_time": start_time,
            "segments": segs, "source": "text"}


# ---------------------------------------------------------------- 沿用上一届

def from_prev(src_slug: str, ev: dict | None = None) -> dict:
    """把另一场的赛道（通常是上一届）复制过来，但**不假装它是今年的**。

    这是最省力也最容易出错的一档：多数比赛每年改线，CP 里程会整体平移 2–3 km。
    所以这里只做复制，并在每段标上 source=copy:<slug>，交给 validate 与页面
    把「沿用」两个字显示出来；用户核对完再确认。
    """
    from . import event as E

    src = E.load_course(src_slug)
    segs = []
    for s in src.get("segments") or []:
        c = dict(s)
        c["source"] = "copy:%s" % src_slug
        segs.append(c)
    return {
        "date": None,
        "distance_km": src.get("distance_km"),
        "ascent_m": src.get("ascent_m"),
        "segments": segs,
        "source": "copy:%s" % src_slug,
    }


# ---------------------------------------------------------------- 校验

def _issue(level: str, code: str, where: str, msg: str) -> dict:
    return {"level": level, "code": code, "where": where, "msg": msg}


def validate(ev: dict, course: dict) -> list[dict]:
    """校验一份赛道是否「可信且可用」。返回问题列表（error 必须修，warn 需确认）。

    这里判的是**内部一致性与物理可行性**，不是「对不对」——
    来源对不对只能靠 provenance 与人工确认，物理上跑不通的数据一定是错的。
    """
    issues: list[dict] = []
    segs = sorted(course.get("segments") or [], key=lambda s: s.get("idx") or 0)
    dist = float(course.get("distance_km") or ev.get("distance_km") or 0)
    dur_total = float(ev.get("duration_h") or 0)      # 总关门（未知就是 0，相关判据自跳过）

    if not segs:
        return [_issue("error", "empty", "segments", "赛道是空的，至少需要一个终点")]
    if not (0 < dist <= 400):
        issues.append(_issue("error", "dist", "distance_km",
                             "总距离 %s km 不像一场越野赛" % dist))

    # 1) 里程递增 + 末点等于总距离
    prev = 0.0
    for s in segs:
        km = float(s.get("km") or 0)
        if km <= prev:
            issues.append(_issue("error", "km_order", "CP%s" % s.get("idx"),
                                 "CP%s 的 %.1f km 没有比前一个点（%.1f）大" % (s.get("idx"), km, prev)))
        prev = km
    last = float(segs[-1].get("km") or 0)
    if dist and abs(last - dist) > KM_TOL:
        issues.append(_issue(
            "warn", "km_last", "CP%s" % segs[-1].get("idx"),
            "最后一个 CP 是 %.1f km，总距离写的是 %.1f km（差 %.1f）" % (last, dist, last - dist)))

    # 2) 关门：能解析、递增、落在总关门内
    st = _clock(ev.get("start_time") or "00:00") or dt.time(0, 0)
    start = dt.datetime.combine(dt.date(2027, 1, 1), st)
    day = 1
    prev_cut = start
    with_cut = 0
    for s in segs:
        raw = s.get("cutoff")
        if not raw:
            continue
        with_cut += 1
        t = _clock(raw)
        sec = _dur(raw) if t is None else None
        if t is not None:
            c = dt.datetime.combine(dt.date(2027, 1, 1) + dt.timedelta(days=day - 1), t)
            if c < prev_cut:
                c += dt.timedelta(days=1)
                day += 1
        elif sec is not None:
            c = start + dt.timedelta(seconds=sec)
        else:
            issues.append(_issue("error", "cut_fmt", "CP%s" % s.get("idx"),
                                 "CP%s 的关门「%s」不是时间也不是时长" % (s.get("idx"), raw)))
            continue
        if c <= prev_cut and with_cut > 1:
            issues.append(_issue("error", "cut_order", "CP%s" % s.get("idx"),
                                 "CP%s 的关门 %s 不晚于上一个点 %s"
                                 % (s.get("idx"), c.strftime("%m-%d %H:%M"),
                                    prev_cut.strftime("%m-%d %H:%M"))))
        # 3) 物理可行性：这段要求的最低等效速度
        leg_h = (c - prev_cut).total_seconds() / 3600.0
        eq = (float(s.get("km") or 0) - float(
            next((x["km"] for x in reversed(segs[:segs.index(s)]) if x.get("km")), 0))
            ) + (float(s.get("dplus") or 0) / 100.0)
        if eq < 0:
            eq = 0.0
        s["_leg_h"], s["_eq"] = leg_h, eq
        if leg_h > 0.01:
            spd = eq / leg_h
            s["_spd"] = spd
            if spd > MAX_EQ_SPEED:
                issues.append(_issue(
                    "error", "speed_fast", "CP%s" % s.get("idx"),
                    "CP%s 这段要求 %.1f km/h 等效（%.1f km / %.1f h），超过人类极限 %.0f——"
                    "关门时间大概率录错（漏了一小时/12:45 认成 02:45）"
                    % (s.get("idx"), spd, eq, leg_h, MAX_EQ_SPEED)))
            elif spd < MIN_EQ_SPEED:
                issues.append(_issue(
                    "warn", "speed_slow", "CP%s" % s.get("idx"),
                    "CP%s 这段只要 %.1f km/h 等效，宽松得反常，建议核对关门"
                    % (s.get("idx"), spd)))
        prev_cut = c

    # 3b) 段落之间的一致性：某一段的「要求速度」和其余段差 3.5 倍以上，通常是录错了
    #     （钟点写错会既可能让这段变得不可能快，也可能让这段变得几乎不用动）
    spds = [(s0.get("idx"), s0.get("_spd")) for s0 in segs if s0.get("_spd")]
    spds = [(i, v) for i, v in spds if v]
    if len(spds) >= 4:
        vals = sorted(v for _i, v in spds)
        med = vals[len(vals) // 2]
        for idx0, v in spds:
            if med and (v > med * 3.5 or v < med / 3.5):
                issues.append(_issue(
                    "warn", "speed_outlier", "CP%s" % idx0,
                    "CP%s 这段要求 %.1f km/h，其余段中位数 %.1f km/h（差 %.1f 倍），"
                    "建议核对这一行的里程与关门" % (idx0, v, med, max(v / med, med / v))))

    # 3c) 占比异常：某一段吃掉一半以上的总关门，但等效距离只占一小部分
    if dur_total and with_cut:
        # 用「时间占比 ÷ 等效距离占比」而不是固定阈值：赛道后段本来就可能给得宽松，
        # 固定阈值会冤枉它；但一个段花掉 3 倍于自己份额的时间，只能是录错。
        tot_eq = sum(float(x.get("_eq") or 0) for x in segs) or 1.0
        for x in segs:
            lh, leq = x.get("_leg_h"), float(x.get("_eq") or 0)
            if not lh or not leq:
                continue
            t_share, e_share = lh / dur_total, leq / tot_eq
            if t_share > 3 * e_share and lh > dur_total * 0.2:
                issues.append(_issue(
                    "error", "share", "CP%s" % x.get("idx"),
                    "CP%s 这一段花了总关门的 %.0f%%（%.1f h），等效距离只占 %.0f%%"
                    "（%.0f 倍）——钟点大概率录错（小时位漏了一位，或跨零点被算错）"
                    % (x.get("idx"), t_share * 100, lh, e_share * 100,
                       t_share / max(e_share, 1e-6))))

    if with_cut and with_cut < len(segs):
        issues.append(_issue(
            "warn", "cut_partial", "segments",
            "%d 个 CP 里只有 %d 个有关门，其余按「未公布」处理（不参与余量计算）"
            % (len(segs), with_cut)))
    if not with_cut and len(segs) > 1:
        issues.append(_issue("warn", "cut_none", "segments",
                             "没有任何关门时间：只算总用时，不出余量表"))

    # 4) 总关门（若已填）与最后关门的一致性
    dur = ev.get("duration_h")
    if dur and with_cut:
        total = start + dt.timedelta(hours=float(dur))
        if abs((prev_cut - total).total_seconds()) > 3600:
            issues.append(_issue(
                "warn", "dur_vs_last", "duration_h",
                "总关门 %.1f h（至 %s）与最后一个 CP 的关门（%s）差超过 1 小时"
                % (float(dur), total.strftime("%m-%d %H:%M"),
                   prev_cut.strftime("%m-%d %H:%M"))))

    # 5) 累计爬升对账
    dsum = sum(float(s.get("dplus") or 0) for s in segs)
    asc = ev.get("ascent_m")
    if asc and dsum:
        if abs(dsum - asc) > max(80, asc * ASCENT_TOL):
            issues.append(_issue(
                "warn", "ascent", "ascent_m",
                "各段爬升相加 %.0f m，档案写的是 %d m（差 %+.0f）" % (dsum, asc, dsum - asc)))

    # 6) 日期合理性
    try:
        d = dt.date.fromisoformat(str(ev.get("date"))[:10])
        if abs((d - dt.date.today()).days) > 365 * 3:
            issues.append(_issue("warn", "date", "date",
                                 "比赛日期 %s 距今天超过 3 年，确认没写错年份" % d))
    except (ValueError, TypeError):
        issues.append(_issue("error", "date", "date", "比赛日期缺失或不合法"))

    for x in segs:                      # 临时字段不留在返回值里（course 会被写盘）
        for k in ("_spd", "_eq", "_leg_h"):
            x.pop(k, None)
    return issues


def errors(issues: list[dict]) -> list[dict]:
    return [i for i in issues if i["level"] == "error"]


def summary(issues: list[dict]) -> str:
    e, w = len(errors(issues)), sum(1 for i in issues if i["level"] == "warn")
    if not issues:
        return "校验通过：0 个问题"
    return "校验结果：%d 个必须修，%d 个待确认" % (e, w)


# ---------------------------------------------------------------- 落盘

def _fill_dplus(segs: list[dict], ascent: float | None) -> bool:
    """分段爬升缺失时按里程比例摊总爬升。返回是否做了估算。

    为什么猜这一项：余量模型用「等效距离 = 距离 + 爬升/100」，全填 0 会让
    爬坡段被当成平路，算出来的余量偏乐观——这比「按比例摊」更危险。
    摊完会在 issues 里标出来，让人知道这一层是估的。
    """
    if not ascent or not segs:
        return False
    if all(s.get("dplus") for s in segs):
        return False
    total_km = float(segs[-1].get("km") or 0) or 1.0
    prev = 0.0
    for s in segs:
        km = float(s.get("km") or 0)
        if not s.get("dplus"):
            s["dplus"] = int(round(float(ascent) * max(0.0, km - prev) / total_km))
        prev = km
    return True


def _walk_days(segs: list[dict], start_time: str | None) -> None:
    """按钟点顺序推断每个关门的「第几天」，写回 cutoff_day。

    赛道上跨零点是很常见的（105K 的 CP8 在次日 00:45），而 CP 表里只写 00:45。
    规则：钟点比上一个点小就算进了一天。不做更聪明的推断——跨两次零点的比赛
    在实测里几乎不存在，真有的话页面上的余量表会立刻显形。
    """
    prev = _clock(start_time or "00:00") or dt.time(0, 0)
    day = 1
    for s in segs:
        t = _clock(s.get("cutoff")) if s.get("cutoff") else None
        if t is None:
            continue
        if t < prev:
            day += 1
        s["cutoff_day"] = day
        prev = t


def apply(ev: dict, draft: dict, by: str = "user", force: bool = False) -> dict:
    """把草稿写进档案。有 error 且未 force 时拒绝写入（不动现有数据）。

    写入时三件事必须一起做：改 course.json、改 event.json 的日期/距离/爬升、
    记 provenance。少做一件，下次打开就会看到「距离 103.3 但最后一个 CP 在 99.5」。
    """
    from . import event as E

    slug = ev["slug"]
    course = E.load_course(slug)
    segs = draft.get("segments") or []
    merged = dict(course)
    if draft.get("date"):
        merged_ev = dict(ev)
        merged_ev["date"] = draft["date"].isoformat() if hasattr(draft["date"], "isoformat") \
            else str(draft["date"])
    else:
        merged_ev = dict(ev)
    if draft.get("distance_km"):
        merged_ev["distance_km"] = round(float(draft["distance_km"]), 1)
        merged["distance_km"] = merged_ev["distance_km"]
    if draft.get("ascent_m"):
        merged_ev["ascent_m"] = int(draft["ascent_m"])
        merged["ascent_m"] = merged_ev["ascent_m"]
    if draft.get("start_time"):
        merged_ev["start_time"] = draft["start_time"]
    if draft.get("duration_h"):
        # 总关门以规程为准：CP 表最后一点的关门不该和它差一个小时以上
        merged_ev["duration_h"] = float(draft["duration_h"])
    if segs:
        merged["segments"] = segs
        if not merged_ev.get("distance_km"):
            merged_ev["distance_km"] = max(float(s["km"]) for s in segs)

    _walk_days(merged["segments"], merged_ev.get("start_time"))
    estimated = _fill_dplus(merged["segments"], merged_ev.get("ascent_m"))
    issues = validate(merged_ev, merged)
    if estimated:
        issues.append(_issue(
            "warn", "dplus_est", "segments",
            "没有官方的分段爬升，已按里程比例摊总爬升——等效距离与余量是估算值"))
    if errors(issues) and not force:
        return {"ok": False, "issues": issues,
                "error": "有 %d 处必须修正，未写入" % len(errors(issues))}

    merged["provenance"] = {
        "source": draft.get("source") or "manual",
        "by": by,
        "at": dt.date.today().isoformat(),
        "checked": [i for i in issues if i["level"] == "warn"],
    }
    E.save_course(slug, merged)
    E._write(slug, "event.json", merged_ev)
    return {"ok": True, "issues": issues, "event": merged_ev,
            "n_segments": len(segs)}


def explain(issues: list[dict]) -> str:
    """给 CLI / 页面用的一段人话，按 level 分组。"""
    if not issues:
        return "✓ 没有发现问题"
    out = []
    for i in issues:
        mark = "×" if i["level"] == "error" else "!"
        out.append("%s %s：%s" % (mark, i["where"], i["msg"]))
    return "\n".join(out)
