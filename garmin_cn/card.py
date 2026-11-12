"""一图流看板：把当日快照渲染成一张竖版 PNG（1080 宽），适合在微信里直接看。

思路：先用纯字符串拼一张固定尺寸的 HTML，再调本机 Chrome/Edge 无头截图。
不依赖 Pillow / matplotlib，Windows 上开箱可用。
"""
from __future__ import annotations

import datetime as dt
import html
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import brand as BR
from .coach import brief
from .exportspec import probe as _export_probe
from .metrics import num
from .plan import item_line

W = 1080
H = 1920          # 图区压缩后总高跟着收，避免两列对齐后底部空出一大块
_EXPORT_PROBE = _export_probe()   # 手机端导出探针，桌面无头截图不调用
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

DOW = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
LEVEL_COLOR = {
    "优秀": "#4FD6A6", "良好": "#4FD6A6", "一般": "#D6A75A",
    "偏低": "#E06A5F", "较差": "#E06A5F", "数据不足": "#7C8794",
}


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def _f(v, unit="", d=0):
    if v is None:
        return "—"
    return f"{v:.{d}f}{unit}" if d else f"{v:.0f}{unit}"


def _find_browser() -> str | None:
    # 强制不出图：Termux 上想跳过截图、或者排查「是截图慢还是别的地方慢」时用
    if os.environ.get("GARMIN_NO_BROWSER") == "1":
        return None
    for p in CHROME_CANDIDATES:
        if Path(p).exists():
            return p
    for name in ("chrome", "msedge"):
        hit = shutil.which(name)
        if hit:
            return hit
    return None


def can_shot() -> bool:
    """这台机器能不能无头截图。

    Termux / 精简版 Windows 上通常是 False：PNG 出不了，但调用方可以退化成
    同目录的 HTML（手机浏览器打开整张缩放，长按存图一样能发微信）。
    """
    return _find_browser() is not None


def _level_color(score, level: str) -> str:
    if level in LEVEL_COLOR:
        return LEVEL_COLOR[level]
    if score is None:
        return "#7C8794"
    return "#4FD6A6" if score >= 75 else ("#D6A75A" if score >= 60 else "#E06A5F")


def _bars(parts: list[dict]) -> str:
    out = []
    for p in parts:
        score = p.get("score") or 0
        color = "#4FD6A6" if score >= 75 else ("#D6A75A" if score >= 60 else "#E06A5F")
        out.append(
            f'<div class="bar">'
            f'<div class="bar-top"><span class="bar-name">{esc(p["name"])}'
            + (f'<em>{esc(p["detail"])}</em>' if p.get("detail") else '')
            + f'</span>'
            f'<span class="bar-score" style="color:{color}">{score:.0f}</span></div>'
            f'<div class="bar-track"><i style="width:{max(2, min(100, score)):.0f}%;background:{color}"></i></div>'
            f'</div>'
        )
    return "".join(out)


def _cal_short(name: str) -> str:
    """课名压短：去掉「W10 」周次与「周四」这类星期前缀。

    一格只有 ~123px、18px 字号，只够两行；星期已经单独显示在格子上方，
    课名里再带一遍会把「6×800m 间歇」挤成第三行被裁掉。
    """
    s = re.sub(r"^W\d+\s+", "", str(name or "")).strip()
    s = re.sub(r"^周[一二三四五六日天]\s*", "", s).strip()
    return s or "训练"


def _cal_sub(item: dict, more: int = 1) -> str:
    """一格里的副标题：距离 · 时长（同一天多节课时补 +N）。"""
    bits = []
    km = item.get("km")
    if km:
        bits.append(f"{km:g} km")
    mins = item.get("minutes")
    if mins:
        bits.append(f"{mins}′")
    if more > 1:
        bits.append(f"+{more - 1}")
    return " · ".join(bits)


def _week_cells(plan: dict | None, today: str,
                done: dict | None = None) -> str:
    """本周 7 天横排日历条，与 105K 备赛卡（`_race_week`）同一套版式。

    以前这里是竖排 7 行（`wlist`），吃掉约 280px，直接把 1080×1920 的固定
    画幅挤变形；横排只占一格高度，还能横向比出「哪天练、哪天休」。
    数据来自佳明日历或教练计划，两者 `week` 结构相同。
    """
    week = (plan or {}).get("week") or []
    if not week:
        return '<p class="empty">还没有读到训练计划</p>'
    if all(d.get("rest") for d in week):
        return ('<p class="empty" style="padding:4px 0">'
                '本周计划均为休息日（教练未排训练）</p>')
    done = done or {}
    cells = []
    for d in week:
        date = str(d.get("date") or "")
        is_today = date == today
        items = [x for x in (d.get("items") or []) if isinstance(x, dict)]
        if items:
            name = _cal_short(items[0].get("name"))
            sub = _cal_sub(items[0], len(items))
        else:
            name, sub = "休", ""
        act = done.get(date) or {}
        if d.get("done") or (act.get("min") or 0) >= 5:
            badge = '<div class="act ok">已完成</div>'
        elif is_today:
            badge = '<div class="act void">今天</div>'
        elif date < today:
            badge = '<div class="act miss">未练</div>'
        else:
            badge = '<div class="act void">—</div>'
        cells.append(
            f'<div class="wkd{" today" if is_today else ""}">'
            f'<div class="d">{esc(d.get("dow") or "")}</div>'
            f'<div class="plan">{esc(name)}</div>'
            f'<div class="sub">{esc(sub)}</div>{badge}</div>')
    return '<div class="wk">' + "".join(cells) + "</div>"


def _race_week(day: dt.date, done: dict[str, dict] | None = None) -> str:
    """105K 备赛计划：横排 7 格，每格「计划 + 实际」。

    原来一周 7 行竖排、用 space-between 撑开，行距忽大忽小，看着松散；
    改成日历条之后同样是 7 天，但一眼能横向比出「哪天练了、哪天漏了」。
    """
    from . import raceplan

    rows = raceplan.week_rows(day)
    if not rows:
        return '<p class="empty">今天不在 105K 备赛计划区间内</p>'
    today = day.isoformat()
    done = done or {}
    cells = []
    for r in rows:
        is_today = r["date"] == today
        act = done.get(r["date"]) or {}
        km, mins = act.get("km") or 0, act.get("min") or 0
        if r["kind"] == "rest":
            plan, sub = "休", ""
        else:
            plan, sub = r["main"], r["sub"]
        # 实际完成徽标：达标绿、不足黄、已过日期未练灰；未来的日子不评价
        badge = ""
        if mins >= 5:
            body = f"{km:.1f} km" if km >= 0.1 else f"{mins:.0f} 分钟"
            tgt_km, tgt_min = r.get("km") or 0, r.get("minutes") or 0
            hit = ((tgt_km and km >= tgt_km * 0.9)
                   or (tgt_min and mins >= tgt_min * 0.9)
                   or (r["kind"] == "rest" and mins >= 25))
            badge = f'<div class="act {"ok" if hit else "part"}">{esc(body)}</div>'
        elif r["date"] < today and r["kind"] != "rest":
            badge = '<div class="act miss">未练</div>'
        else:
            badge = '<div class="act void">—</div>'
        cells.append(
            f'<div class="wkd{" today" if is_today else ""}">'
            f'<div class="d">{esc(r["dow"])}</div>'
            f'<div class="plan">{esc(plan)}</div>'
            f'<div class="sub">{esc(sub)}</div>'
            f'{badge}</div>'
        )
    return '<div class="wk">' + "".join(cells) + "</div>"


def _race_rows(day: dt.date) -> str:
    """105K 备赛计划：当天所在周 7 天，格式与佳明课表一致。"""
    from . import raceplan

    rows = raceplan.week_rows(day)
    if not rows:
        return '<p class="empty">今天不在 105K 备赛计划区间内</p>'
    today = day.isoformat()
    out = []
    for r in rows:
        is_today = r["date"] == today
        cls = "wrow today" if is_today else "wrow"
        tag = '<b class="now">今天</b>' if is_today else ""
        if r["kind"] == "rest":
            body = '<span class="rest">休</span>'
        else:
            body = f'<span class="wl">{esc(r["main"])}'
            if r["sub"]:
                body += f' · {esc(r["sub"])}'
            body += "</span>"
        out.append(
            f'<div class="{cls}"><span class="wd">{esc(r["dow"])}</span>'
            f'<span class="wbody">{body}</span>{tag}</div>'
        )
    return "".join(out)


def _trend_rows(hist: list[dict]) -> str:
    """近 7 天 vs 之前 7 天的三行对比。"""
    if not hist or len(hist) < 8:
        return '<p class="empty">样本还不足两周</p>'
    cur = hist[-7:]
    prev = hist[:-7][-7:]

    def avg(rows, key):
        vals = [num(r.get(key)) for r in rows]
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    specs = [
        ("睡眠", "sleep_total_min", " min", "up"),
        ("HRV", "hrv_night", " ms", "up"),
        ("静息心率", "resting_hr", " bpm", "down"),
    ]
    out = []
    for label, key, unit, better in specs:
        a, b = avg(cur, key), avg(prev, key)
        if a is None or b is None:
            out.append(f'<div class="tline"><span>{label}</span><span class="tv">—</span></div>')
            continue
        delta = a - b
        good = delta >= 0 if better == "up" else delta <= 0
        cls = "up" if good else "down"
        out.append(
            f'<div class="tline"><span>{label}</span>'
            f'<span class="tv">{a:.0f}{unit}'
            f'<i class="delta {cls}">{delta:+.0f}</i></span></div>'
        )
    return "".join(out)


def _acts_by_day(acts: list[dict], dates: set[str]) -> dict[str, dict]:
    """按日聚合实际完成的量与时长，供备赛区做「计划 vs 实际」对照。"""
    out: dict[str, dict] = {}
    for a in acts or []:
        day = (a.get("date") or "")[:10]
        if day not in dates:
            continue
        b = out.setdefault(day, {"km": 0.0, "min": 0.0, "n": 0})
        if (a.get("type") or "") == "running":
            b["km"] += (num(a.get("distance_m")) or 0) / 1000
        b["min"] += (num(a.get("duration_s")) or 0) / 60
        b["n"] += 1
    return out


def _bars7(rows: list[tuple[str, float | None]], *, height: int = 112,
           unit: str = "", target: float | None = None, accent: str = "#4FD6A6",
           extra: list[float | None] | None = None,
           extra_color: str = "#5FA8E0",
           legend: tuple[str, str] | None = None) -> str:
    """近 7 天柱状图（睡眠、跑量都能用）。最后一根是今天，用强调色。

    extra 与 rows 等长时做双色堆叠（睡眠：主睡眠 + 小睡），柱顶数字是合计。
    """
    rows = rows[-7:]
    if not rows or all(v is None for _, v in rows):
        return '<p class="empty">数据不足</p>'
    ext = list(extra)[-7:] if extra else []
    vals = []
    for i, (_, v) in enumerate(rows):
        if v is not None:
            vals.append(v + (ext[i] or 0 if i < len(ext) else 0))
    top = max(vals + ([target] if target else [])) * 1.12 or 1
    cells = []
    for i, (label, v) in enumerate(rows):
        is_last = i == len(rows) - 1
        col = accent if is_last else "#3B4C5E"
        ev = ext[i] if i < len(ext) else None
        if v is None:
            hgt, cap = 4, "—"
        else:
            hgt = max(4, v / top * 100)
            cap = (f"{v + (ev or 0):.1f}" if ev else f"{v:.0f}")
        # 列内 DOM 顺序 = 自上而下：小睡段放前面才叠在主睡眠上面
        segs = (f'<i style="height:{max(3, ev / top * 100):.0f}%;background:{extra_color}"></i>'
                if ev else "")
        segs += f'<i style="height:{hgt:.0f}%;background:{col}"></i>'
        cells.append(
            f'<div class="b7{" on" if is_last else ""}">'
            f'<b>{cap}</b><div class="b7t">{segs}</div>'
            f'<span>{esc(label)}</span></div>'
        )
    goal = (f'<div class="b7goal">目标 {round(target, 1):g}{unit}</div>'
            if target else "")
    leg = ""
    if legend:
        leg = ('<div class="b7legend"><span><i style="background:#3B4C5E"></i>'
               f'{esc(legend[0])}</span><span><i style="background:{extra_color}"></i>'
               f'{esc(legend[1])}</span></div>')
    return (f'<div class="bars-wrap">{goal}{leg}'
            f'<div class="bars7" style="height:{height}px">{"".join(cells)}</div></div>')


def _spark(points: list[tuple[float, float | None]], *, w: int = 470, h: int = 92,
           color: str = "#4FD6A6", label: str = "", unit: str = "") -> str:
    """SVG 折线，用来画当日身体电量 / HRV 这类连续值。点不够就老实说。"""
    pts = [(x, y) for x, y in points if y is not None]
    if len(pts) < 2:
        return '<p class="empty">数据不足</p>'
    x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
    y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
    if x1 == x0:
        x1 = x0 + 1
    if y1 == y0:
        y1 = y0 + 1
    pad = 8

    def X(x: float) -> float:
        return pad + (x - x0) / (x1 - x0) * (w - 2 * pad)

    def Y(y: float) -> float:
        return h - pad - (y - y0) / (y1 - y0) * (h - 2 * pad)

    line = " ".join(f"{'M' if i == 0 else 'L'}{X(x):.1f},{Y(y):.1f}"
                    for i, (x, y) in enumerate(pts))
    area = line + f" L{X(pts[-1][0]):.1f},{h - pad} L{X(pts[0][0]):.1f},{h - pad} Z"
    lx, ly = pts[-1]
    head = (f'<div class="sphead"><span>{esc(label)}</span>'
            f'<em>{ly:.0f}{unit}</em></div>') if label else ""
    return (
        head
        + f'<svg class="sp" style="height:{h}px" viewBox="0 0 {w} {h}" preserveAspectRatio="none">'
        f'<path d="{area}" fill="{color}" opacity=".14"/>'
        f'<path d="{line}" fill="none" stroke="{color}" stroke-width="2.4" '
        f'vector-effect="non-scaling-stroke" stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{X(lx):.1f}" cy="{Y(ly):.1f}" r="4" fill="{color}" '
        f'vector-effect="non-scaling-stroke"/></svg>'
    )


def bb_state(t: dict) -> dict:
    """身体电量的展示口径（纯函数，自检直接测它）。

    优先级：日内序列末点 → daily.bb_now → 全天最高 → 未测量。

    为什么要用序列末点：`bb_now` 是上次实时拉取写下的，可能比曲线旧；
    而序列最后一点才是「此刻还剩多少」。
    为什么最后要退到「全天最高」：`bb_high/low` 来自 daily summary，live 层
    上线之前同步的老数据只有这两个值 —— 直接写「未测量」等于否认那天有电量。
    """
    now = num(t.get("bb_now"))
    now_at = t.get("bb_now_at") or ""
    high = num(t.get("bb_high"))
    scrubbed = bool(t.get("night_scrubbed"))
    try:
        from . import live as livemod
        series = [] if scrubbed else livemod._load_series(t.get("bb_series"))
    except Exception:
        series = []
    pts = [((ts / 1000 + 8 * 3600) % 86400 / 3600.0, v) for ts, v in series]
    if series and not scrubbed:
        now = series[-1][1]
        if not now_at:
            hh = pts[-1][0]
            now_at = f"{int(hh):02d}:{int(round((hh % 1) * 60)):02d}"
    if now is not None:
        val = f"{now:.0f}"
        txt = f"{val}%"
        sub = "手表掉落" if scrubbed else (f"当前 {now_at}" if now_at else "当前值")
    elif high is not None:
        val = f"{high:.0f}"
        txt = f"最高 {val}%"
        sub = "今日最高" + ("（手表掉落）" if scrubbed else "")
    else:
        val, txt, sub = "—", "未测量", "未测量"
    if scrubbed and not series:
        head = "身体电量（今天 · 手表掉落，夜间未测量）"
    elif now is not None:
        # 有当前值但没有曲线（老数据 / 这次没拉到序列）时也要说清是当前值，
        # 别写成「今日最高」—— 值和副标题会对不上。
        _tail = f"{'，' + now_at if now_at else ''}"
        head = (f"身体电量（今天 · 当前 {txt}{_tail}）" if series
                else f"身体电量（今天 · 当前 {txt}{_tail} · 无日内曲线）")
    elif high is not None:
        head = f"身体电量（今日最高 {high:.0f}% · 日内曲线未同步）"
    else:
        head = "身体电量（未同步）"
    return {"now": now, "now_at": now_at, "high": high, "series": series,
            "pts": pts, "val": val, "txt": txt, "sub": sub, "head": head,
            "scrubbed": scrubbed}


def build_html(res: dict, sync_at: str | None = None,
               src: str = "Garmin 国区", ai_note: str = "") -> str:
    d = dt.date.fromisoformat(res["date"])
    t = res["today"]
    # 今天没同步时 derive 会回退到库里最后一天，出图必须写清是哪天的数据，
    # 否则分享出去的「今天的数据」其实是昨天的静息心率（真机 45 vs App 52）。
    _data_date = str(res.get("data_date") or res["date"])[:10]
    try:
        _stale = max(0, (dt.date.today()
                         - dt.date.fromisoformat(_data_date)).days)
    except (TypeError, ValueError):
        _stale = 0
    _today_view = str(res["date"])[:10] == dt.date.today().isoformat()
    _cur_day = _today_view and _data_date == dt.date.today().isoformat()
    _data_head = ("今天的数据" if _cur_day else
                  f"数据 · {_data_date[5:].replace('-', '/')}"
                  + ("（未同步今日）" if (_stale and _today_view) else ""))
    r = res["readiness"]
    L = res["load"]
    sl = res["sleep"]
    b = brief(res)
    s = b["session"]
    plan = res.get("plan") or {}
    color = _level_color(r.get("score"), r.get("level") or "")

    # 小睡单独在 nap_min 里，不算进主睡眠时长；睡眠债是「主睡眠 + 小睡」对动态需求算的，
    # 两个数不摆出来，看图的人会以为 7.5 h 就是全部，也看不出债务为什么被抵扣了。
    nap_today = num(sl.get("nap_eff")) or num(t.get("nap_min")) or 0
    nap_note = f" + 小睡 {nap_today:.0f} 分" if nap_today else ""
    debt_nap = "（含小睡）" if ((sl.get("debt") or {}).get("nap_min") or 0) else ""
    # 手动矫正过的夜晚要标出来：不显示佳明评分（没有），改显示矫正时段。
    # 文案控制在两行内——四格指标里只有这格会挤成三行，整行就被撑高了。
    if (t.get("sleep_src") == "manual") and sl.get("start") and sl.get("end"):
        sleep_sub = f"睡眠 · 矫正 {sl['start'][11:16]}–{sl['end'][11:16]}"
    else:
        _est = num(sl.get("score_est"))
        _sc = _est if _est is not None else sl.get("score")
        _tag = "估" if _est is not None else "评"
        sleep_sub = (f"睡眠 · {_tag} {_f(_sc)}"
                     f"{('+' + f'{nap_today:.0f}′小睡') if nap_today else ''}")

    flags = "".join(f'<li>{esc(f["text"])}</li>' for f in res["flags"][:3]) or "<li>没有需要提醒的异常</li>"

    acts = res.get("activities") or []
    last = ""
    if acts:
        a = acts[0]
        if (a.get("date") or "")[:10] != res["date"]:      # 今天练过的去「今日训练」卡看
            dist = (num(a.get("distance_m"), 0) or 0) / 1000
            pace = num(a.get("pace_s_km"))
            pace_s = f"{int(pace)//60}′{int(pace)%60:02d}″" if pace else "—"
            last = (f'{esc((a.get("date") or "")[5:])} · {dist:.2f}km · {pace_s}'
                    f' · HR{_f(a.get("avg_hr"))}')

    # 今日训练：每条活动一个摘要块，块数和内容都会变，所以不做固定格数，
    # 每天的活动上限按 4–5 条设计：方块网格 2 列，行高 1fr，
    # 块数变多时自动往上长（1 条横跨两列、奇数条最后一块横跨），
    # 全部撑满今日训练卡，不再有「文字挤在一行、底下留白」的问题。
    today_rows = []
    # 按时间正序排：一天里先跑后骑，读起来是时间线，不是倒序堆叠。
    t_acts = sorted([x for x in acts if (x.get("date") or "")[:10] == res["date"]],
                    key=lambda x: x.get("start_ts") or "")[:5]
    n_act = len(t_acts)
    for idx, a in enumerate(t_acts):
        atype = (a.get("type") or "").lower()
        dist = (num(a.get("distance_m"), 0) or 0) / 1000
        dur_s = num(a.get("duration_s")) or 0
        pace = num(a.get("pace_s_km"))
        # 骑行/游泳这类用配速没意义：5′06″/km 得自己换算才知道是 11.8 km/h，
        # 直接给均速。跑步仍然给配速，那是跑者真正在意的数字。
        if atype.startswith(("cycl", "bik")) and dist > 0 and dur_s > 0:
            metric = f"{dist / (dur_s / 3600):.1f} km/h"
        elif atype.startswith("swim") and pace:
            metric = f"{int(pace)//60 if pace >= 60 else 0}′{int(pace) % 60:02d}″/100m"
        else:
            metric = f"{int(pace)//60}′{int(pace)%60:02d}″" if pace else ""
        head = (a.get("start_ts") or "")[11:16] or "今天"
        name = (a.get("name") or "训练")
        if len(name) > 14:
            name = name[:13] + "…"
        _min = int(round(dur_s / 60))
        _dur = f"{_min // 60}:{_min % 60:02d}" if _min >= 60 else f"{_min}′"
        # 单个方块：名称一行、距离一行、指标一行。
        # 1 条时空间富余，指标行再加时长与卡路里；2 条以上只留指标和心率，
        # 否则半栏宽度会被 「5′06″ · HR87 · 13′ · 133 kcal」 挤成省略号。
        cls = "aact" + (" span2" if (n_act == 1 or (n_act % 2 == 1 and idx == n_act - 1)) else "")
        vcls = "aact-v" if n_act <= 2 else "aact-v sm"
        sub = " · ".join(x for x in (
            metric,
            f"HR{_f(a.get('avg_hr'))}",
            _dur if n_act == 1 else "",
            f"{num(a.get('calories')) or 0:.0f} kcal" if n_act == 1 else "",
        ) if x)
        today_rows.append(
            f'<div class="{cls}"><div class="aact-h">{esc(head)} · {esc(name)}</div>'
            f'<div class="{vcls}">{dist:.2f}<small> km</small></div>'
            f'<div class="aact-m">{esc(sub)}</div></div>'
        )

    plan_name = plan.get("name") or "教练计划"
    phase = (plan.get("phase") or {}).get("label") or ""

    # 今日营养：处方旁边给一行，训练和吃放一起看才有意义。
    # 数字来自 nutrition.targets()，与看板 / 教练上下文同源。
    # 每项一条进度条：达标绿、未达标黄——纯文字的 "1945 / 1879" 看不出差多少。
    nt = res.get("nutrition") or {}
    nutri_chips = ""
    if nt.get("ok"):
        eat = nt.get("intake") or {}

        def _nrow(label, v, tgt, unit, hard=False):
            v, tgt = v or 0, tgt or 0
            ratio = min(100, v / tgt * 100) if tgt else 0
            # 达标绿、未达标黄、超出红。蛋白/碳水/热量是「下限」，略超无妨，
            # 10% 以上才标红；脂肪是「上限」，一超就是超标——以前 59/54 还
            # 亮着绿条，吃完黄焖鸡看着像完全没问题，其实已经顶破额度。
            if not tgt:
                col = "#D6A75A"
            elif v > (tgt if hard else tgt * 1.1):
                col = "#E4574D"
            elif v >= tgt:
                col = "#4FD6A6"
            else:
                col = "#D6A75A"
            return (f'<div class="nrow"><span class="nl">{label}</span>'
                    f'<div class="ntrack"><i style="width:{ratio:.0f}%;background:{col}"></i></div>'
                    f'<span class="nv" style="color:{col}">{v:g} / {tgt:g}{unit}</span></div>')

        # 没有饮食记录也照样画条：全 0 的黄条比纯文字好——和有记录的日子
        # 排版一致，一眼知道「今天还没记 / 还差多少」，而不是换成另一套样子。
        nutri_chips = (
            '<div class="nutri">'
            + _nrow("热量", eat.get("kcal"), nt["target_kcal"], " kcal")
            + _nrow("蛋白", eat.get("protein_g"), nt["protein_g"], " g")
            + _nrow("碳水", eat.get("carb_g"), nt["carb_g"], " g")
            + _nrow("脂肪", eat.get("fat_g"), nt["fat_g"], " g", hard=True)
            + "</div>"
        )

    # 体重 / 体成分：一图流固定一行四格。数值来自 body 表的首选来源，缺项留「—」，
    # 不整块隐藏——每天都摆在那儿，才看得出「掉的是水还是脂肪」。
    bd = res.get("body") or {}
    body_row = ""
    if bd.get("latest"):
        def _delta(v, unit="kg"):
            if v is None:
                return ""
            cls = "up" if v < 0 else "down"
            return f'<i class="delta {cls}">{v:+.1f}{unit}</i>'

        stale = bd.get("stale_days")
        wsub = (f'7 天{_delta(bd.get("delta_w"))}' if bd.get("delta_w") is not None
                else (f'{stale} 天前' if stale else "今天"))
        if bd.get("source_label"):
            wsub += " · " + html.escape(str(bd["source_label"]))
        fat_sub = (f'脂肪 {bd["fat_mass"]:.1f} kg' if bd.get("fat_mass") else "体脂率")
        mus_sub = (f'瘦体重 {bd["lean_mass"]:.1f} kg' if bd.get("lean_mass") else "肌肉量")
        wat_sub = (f'内脏 {bd["visceral"]} 级' if bd.get("visceral") is not None else "水分率")

        def _cell(v, unit, label, sub, digits=1):
            val = "—" if v is None else f"{v:.{digits}f}"
            return (f'<div class="bkpi"><b>{val}<small>{unit}</small></b>'
                    f'<span>{label} {sub}</span></div>')

        body_row = (
            '<div class="brow">'
            + _cell(bd.get("latest"), "kg", "体重", wsub)
            + _cell(bd.get("body_fat"), "%", "体脂", fat_sub)
            + _cell(bd.get("muscle"), "kg", "肌肉", mus_sub)
            + _cell(bd.get("water"), "%", "水分", wat_sub)
            + "</div>"
        )

    # 105K 备赛课表（本地，与执行卡同源）；不在计划区间内则回退佳明教练计划
    rpi = None
    try:
        from . import raceplan
        if raceplan.available():
            rpi = raceplan.day_info(d)
    except Exception:
        rpi = None

    # 「计划 vs 实际」：把本周 7 天真正跑出来的量也摆上去，
    # 只看计划的话，练了和没练在看板上一模一样。
    week_dates: set[str] = set()
    if rpi:
        try:
            week_dates = {x["date"] for x in raceplan.week_rows(d)}
        except Exception:
            week_dates = set()
    done_by_day = _acts_by_day(acts, week_dates)
    if not week_dates:                     # 不在课表区间：退回近 7 天滚动窗口
        week_dates = {(d - dt.timedelta(days=i)).isoformat() for i in range(7)}
        done_by_day = _acts_by_day(acts, week_dates)
    week_run_km = sum(v["km"] for v in done_by_day.values())
    week_tgt_km = rpi["week_km"] if rpi else None

    # 今日训练卡：标题右侧带本周跑量（实际 / 计划），行内左右对齐
    wk_head = ""
    if week_tgt_km:
        wk_head = (f'<span class="chw">本周 <b>{week_run_km:.1f}</b>'
                   f' / {week_tgt_km:g} km</span>')
    # 没有训练的日子也放一张卡：右列三张卡的位置是固定的，
    # 缺一张整列就矮一截，左右不对称。占位文案垂直居中，
    # 空卡也吃满右列高度，不塌成一小条。
    if t_acts:
        # 方块只占「够用」的高度：行高按条数定，剩余高度留给需要提醒的卡。
        # 以前今日训练吃满 1fr，把提醒挤成两行省略号——提醒折叠了就没意义。
        _rows = 1 if n_act <= 1 else (n_act + 1) // 2
        _hh = 128 if _rows == 1 else 90
        _minh = _rows * _hh + (_rows - 1) * 9
        body_rows = (f'<div class="today-body" style="min-height:{_minh}px">'
                     f'{"".join(today_rows)}</div>')
    else:
        body_rows = ('<div class="empty today-empty">今天还没有训练记录'
                     '——练完同步后这里会亮起来</div>')
    today_card = (f'<div class="card today-card">'
                  f'<div class="chd"><h3>今日训练</h3>{wk_head}</div>'
                  f'{body_rows}</div>')

    # 身体电量：当前值 / 曲线来自 live.py 在出图前的实时拉取（bb_now / bb_series）
    # 夜间手表掉落时这些读数是缺测残渣，manual.apply_sleep_fix 已作废——
    # 这里老实显示「未测量」，不拿残渣冒充现状。
    _bb = bb_state(t)
    bb_val, bb_sub, bb_head = _bb["val"], _bb["sub"], _bb["head"]

    # 楼层：floorsAscended 是气压计按 3 m 阈值累加的全天值，下楼不倒扣，
    # 楼梯课/爬坡日会明显跳起来，而活动详情里根本没有这个字段——
    # 所以它是「爬楼课到底爬了多少」唯一能拿到手的数据。
    # 注意底噪：城市平路日也有 3–8 层（天桥、桥洞、气压漂移），绝对数别当净值看。
    _fl = num(t.get("floors"))
    floors_txt = ""
    if _fl and _fl > 0:
        # 包 nowrap：这行本来就贴着右边，断在「楼层」和数字中间很难看
        floors_txt = (' · <span style="white-space:nowrap">楼层 '
                      + str(round(_fl)) + '</span>')

    # 图表：睡眠 7 天柱状 + 身体电量当日曲线 + HRV / 静息心率 14 天折线
    sleep_need_h = (num(t.get("sleep_need_min")) or 0) / 60 or 7.5
    nap_rows = {d_: v for d_, v in (res["series"].get("nap_h") or [])}
    sleep_bars = _bars7([(d_[8:], v) for d_, v in (res["series"].get("sleep_h") or [])],
                        height=72, unit=" h", target=sleep_need_h,
                        extra=[nap_rows.get(d_) for d_, _ in (res["series"].get("sleep_h") or [])],
                        extra_color="#5FA8E0", legend=("主睡眠", "小睡"))
    bb_spark = _spark(_bb["pts"], h=48, color="#D6A75A", label="", unit="%")
    hrv_pts = [(i, num(v)) for i, (_, v) in enumerate(res["series"].get("hrv") or [])]
    rhr_pts = [(i, num(v)) for i, (_, v) in enumerate(res["series"].get("rhr") or [])]
    hrv_spark = _spark(hrv_pts[-14:], h=42, color="#5FA8E0", label="HRV", unit=" ms")
    rhr_spark = _spark(rhr_pts[-14:], h=42, color="#E06A5F", label="静息心率", unit=" bpm")

    # AI 点评：只有拿到现成文本才画。限两行——一图流高度写死 1080×1920，
    # 多三行就会把底部挤出画幅。
    ai_html = ""
    if (ai_note or "").strip():
        ai_html = (f'<div class="ai"><span>✦ AI 教练</span>'
                   f'{esc(ai_note.strip())}</div>')

    if rpi:
        # 备赛卡自带「本周主题」，不再放 rp_line，避免同一句话出现两次
        rp_line = ""
        plan_title = (f'<b>105K 备赛 · W{rpi["week"]} {esc(rpi["phase"])}期</b>'
                      f' · {rpi["week_km"]} km / +{rpi["week_vert"]:,} m'
                      f'<span class="pt-focus">　本周主题 {esc(rpi["focus"])}</span>')
        week_body = _race_week(d, done_by_day)
    else:
        rp_line = ""
        plan_title = f'<b>{esc(plan_name)}</b>' + (f' · {esc(phase)}' if phase else '')
        week_body = _week_cells(plan, res["date"], done_by_day)

    # 教练横带：赛事倒计时 + 关门预警 + 装备进度（缺数据/异常时整行不画）
    coach_html = ""
    try:
        from . import event as _event
        cd = _event.countdown(today=d)
        goal_h = cd["goal_h"]
        tight = cd["tightest"]
        # 没有 CP 表 / 没有官方关门的赛事（43K、全马）tight 为 None：只给倒计时
        tight_txt, warn = "", ""
        if tight:
            tm = int(tight["margin_s"])
            t_sign = "+" if tm >= 0 else "-"
            if abs(tm) >= 3600:
                t_txt = f"{t_sign}{abs(tm)//3600}h{abs(tm)%3600//60:02d}m"
            else:
                t_txt = f"{t_sign}{abs(tm)//60}m"
            tight_txt = (f'⚠CP{tight["idx"]} {esc(tight["name"])} {t_txt}'
                         if tm < 6 * 3600 else "")
            warn = ' class="warn"' if tm < 0 else ""
        gear_txt = (f'装备 {cd["gear_done"]}/{cd["gear_total"]}'
                    if cd["gear_total"] else "")
        dur_txt = f'{cd["duration_h"]}h' if cd["duration_h"] else "—"
        coach_html = (
            '<div class="coach">'
            f'<em>🏔 {esc(cd["short"])}</em>'
            f'<span class="cd">{esc(cd["status_cn"])} · <b>{cd["days"]}</b> 天</span>'
            f'<span>{cd["dist"]:.1f} km +{cd["ascent"]:,} · {goal_h:g}h / 关门 {dur_txt}</span>'
            + (f'<span{warn}>{tight_txt}</span>' if tight_txt else "")
            + (f'<span class="gear">{gear_txt}</span>' if gear_txt else "")
            + "</div>")
    except Exception:
        coach_html = ""

    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width={W},initial-scale=1,viewport-fit=cover">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
.wrap>*{{flex:none}}
.wrap>.dual{{flex:1 1 auto;min-height:0}}
body{{width:{W}px;height:{H}px;overflow:hidden;background:#12161B;
  font-family:"Microsoft YaHei","PingFang SC",sans-serif;color:#EDF1F5;
  -webkit-font-smoothing:antialiased}}
.wrap{{width:{W}px;height:{H}px;padding:34px 52px 26px;display:flex;flex-direction:column}}
.top{{position:relative;display:flex;justify-content:space-between;
  align-items:flex-end;gap:24px;padding-bottom:16px;
  border-bottom:1px solid #223349}}
.top::after{{content:"";position:absolute;left:0;right:0;bottom:-1px;height:1px;
  background:linear-gradient(90deg,#3B82F6,rgba(34,211,238,.35),transparent);
  pointer-events:none}}
.date{{font-size:38px;font-weight:700;letter-spacing:1px;white-space:nowrap}}
.date small{{font-size:21px;font-weight:400;color:#8FA4BF;margin-left:10px}}
/* 右侧限宽：来源/同步时间一长就会把右侧顶到画幅外。两行拆开、各自可
   换行，右侧永远留在 52px 安全区内。抬头只留日期，页脚左下角那枚品牌
   角标保留（0.6.10）。 */
.meta{{flex:0 1 46%;max-width:46%;text-align:right;font-size:19px;
  color:#8FA4BF;line-height:1.5;word-break:break-word}}
.meta .m1{{font-size:22px;font-weight:650;color:#8CC2FF;
  font-variant-numeric:tabular-nums}}
.meta .m2{{font-size:18px;color:#7689A2;margin-top:4px}}
.hero{{display:flex;align-items:center;gap:40px;padding:14px 0 12px;height:196px;overflow:hidden}}
.score-box{{text-align:center;min-width:250px}}
.score{{font-size:104px;font-weight:700;line-height:.95;color:{color};letter-spacing:-4px}}
.score small{{font-size:26px;font-weight:400;color:#6C7683}}
.level{{margin-top:4px;font-size:26px;color:{color};font-weight:600}}
.level span{{font-size:22px;color:#7F8A97;font-weight:400;margin-left:8px}}
.bars{{flex:1;display:flex;flex-direction:column;gap:10px}}
.bar-top{{display:flex;justify-content:space-between;font-size:21px;margin-bottom:4px}}
.bar-name{{color:#9AA5B1}}
.bar-name em{{font-style:normal;font-size:16px;color:#6C7683;margin-left:8px}}
.bar-score{{font-weight:700}}
.bar-track{{height:9px;background:#223349;border-radius:5px;overflow:hidden}}
.bar-track i{{display:block;height:100%;border-radius:5px}}
.card{{background:#141E2B;border:1px solid #24344A;border-radius:18px;padding:18px 26px}}
.card h3{{font-size:21px;color:#7F8A97;font-weight:400;margin-bottom:10px;letter-spacing:1px}}
.sess{{margin-top:16px;padding:22px 28px}}
.sess .st{{font-size:38px;font-weight:700;letter-spacing:1px}}
.sess .chips{{margin:10px 0 10px}}
.chip{{display:inline-block;font-size:20px;padding:4px 14px;border-radius:999px;
  background:#232C36;color:#B6C0CB;margin-right:10px}}
.chip.main{{background:rgba(79,214,166,.14);color:#4FD6A6}}
.chip.gray{{background:#202932;color:#A8B4C0}}
.sess .why{{font-size:21px;line-height:1.45;color:#B9C3CE;display:-webkit-box;
  -webkit-box-orient:vertical;-webkit-line-clamp:3;overflow:hidden}}
.sess .ai{{margin-top:12px;padding-top:10px;border-top:1px solid #24344A;
  font-size:18px;line-height:1.45;color:#93A3B5;display:-webkit-box;
  -webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden}}
.sess .ai span{{color:#5FA8E0;font-weight:700;margin-right:8px}}
.row4{{display:flex;gap:14px;margin-top:14px}}
.kpi{{flex:1;background:#141E2B;border:1px solid #24344A;border-radius:16px;
  padding:13px 8px;text-align:center}}
.kpi b{{display:block;font-size:32px;font-weight:700;letter-spacing:-1px}}
.kpi span{{display:block;font-size:18px;color:#7F8A97;margin-top:6px}}
.brow{{display:flex;gap:12px;margin-top:10px}}
.bkpi{{flex:1;background:#141E2B;border:1px solid #24344A;border-radius:14px;
  padding:10px 10px;text-align:center}}
.bkpi b{{display:block;font-size:27px;font-weight:700;letter-spacing:-.5px}}
.bkpi b small{{font-size:18px;font-weight:400;color:#8C97A4;margin-left:3px}}
.bkpi span{{display:block;font-size:16px;color:#7F8A97;margin-top:3px}}
/* 双列用 grid 而不是两个 flex 列：行轨道由两列共享，右列三卡的总高
   与左卡严格相等，底部永远齐平——flex 各自撑高做不到这一点。
   1fr 那行（今日训练）吃掉剩余高度，活动方块随之撑开。 */
/* 行权重：今日训练按内容定高（auto），多出来的高度给「需要注意」（1fr）。
   以前 1fr 给的是今日训练，卡片一撑高就把提醒挤到只剩两行折叠——提醒被
   折叠等于没有提醒。 */
.dual{{display:grid;grid-template-columns:1.15fr 1fr;grid-template-rows:auto 1fr auto;
  gap:10px 18px;margin-top:14px;align-items:stretch}}
.lcol{{grid-column:1;grid-row:1 / span 3;display:flex;flex-direction:column;min-height:0}}
.today-card{{grid-column:2;grid-row:1}}
.flags{{grid-column:2;grid-row:2}}
.tcard{{grid-column:2;grid-row:3}}
.plan-card{{margin-top:14px}}
.coach{{display:flex;align-items:baseline;gap:13px;margin-top:11px;
  padding:9px 14px;background:#1A2129;border:1px solid #2B3A4E;border-radius:12px;
  font-size:18px;color:#9AA5B1;white-space:nowrap;overflow:hidden}}
.coach em{{font-style:normal;color:#EDF1F5;font-weight:600;font-size:19px;flex:none}}
.coach .cd b{{color:#4FD6A6;font-weight:700;font-size:22px}}
.coach span.warn{{color:#E0A53A;font-weight:600}}
.coach .gear{{margin-left:auto;color:#7F8A97;flex:none}}
.coach span{{flex:none}}
.plan-title{{font-size:22px;color:#9AA5B1;margin-bottom:10px}}
.plan-title b{{color:#EDF1F5;font-weight:600}}
.pt-focus{{font-size:20px;color:#7F8A97;font-weight:400}}
/* 备赛区：横排 7 格日历条，计划在上、实际完成在下 */
.wk{{display:flex;gap:10px}}
.wkd{{flex:1;background:#20272F;border:1px solid #24344A;border-radius:14px;
  padding:9px 8px;text-align:center;display:flex;flex-direction:column}}
.wkd.today{{background:rgba(47,191,143,.09);border-color:rgba(79,214,166,.4)}}
.wkd .d{{font-size:17px;color:#7F8A97}}
.wkd.today .d{{color:#4FD6A6;font-weight:600}}
.wkd .plan{{font-size:18px;color:#DDE5EE;font-weight:600;margin-top:4px;line-height:1.28;
  min-height:38px;max-height:46px;overflow:hidden;
  display:flex;align-items:center;justify-content:center}}
.wkd .sub{{font-size:14px;color:#7F8A97;line-height:1.25;min-height:17px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.wkd .act{{margin-top:auto;font-size:15px;font-weight:600;border-radius:7px;
  padding:3px 0;margin-top:7px}}
.wkd .act.ok{{background:rgba(79,214,166,.16);color:#4FD6A6}}
.wkd .act.part{{background:rgba(224,163,58,.16);color:#D6A75A}}
.wkd .act.miss{{background:#1B222A;color:#6C7683}}
.wkd .act.void{{color:#3C4650}}
.wlist{{flex:1;display:flex;flex-direction:column;gap:4px}}
.wrow{{display:flex;align-items:center;gap:12px;padding:7px 10px;border-radius:10px;font-size:20px}}
.wrow.today{{background:rgba(47,191,143,.12);border:1px solid rgba(47,191,143,.3)}}
.wd{{width:52px;color:#7F8A97;flex:none}}
.wbody{{flex:1;color:#C9D2DC;line-height:1.4}}
.wl{{display:block;font-size:19px}}
.rest{{color:#6C7683}}
.now{{font-size:17px;color:#4FD6A6;border:1px solid rgba(79,214,166,.45);
  border-radius:6px;padding:1px 7px;flex:none}}
/* 图表 */
.bars-wrap{{margin-top:4px}}
.b7goal{{font-size:17px;color:#6C7683;text-align:right;margin-bottom:2px}}
.bars7{{display:flex;align-items:flex-end;gap:9px}}
.b7{{flex:1;height:100%;display:flex;flex-direction:column;align-items:center;gap:5px}}
.b7 b{{font-size:17px;color:#8C97A4;font-weight:600}}
.b7.on b{{color:#4FD6A6}}
.b7t{{flex:1;width:100%;display:flex;flex-direction:column;justify-content:flex-end;
  background:#1E262E;border-radius:6px;overflow:hidden}}
.b7t i{{display:block;width:100%;flex:none}}
.b7legend{{display:flex;gap:18px;margin:2px 0 4px;font-size:17px;color:#8C97A4}}
.b7legend i{{display:inline-block;width:14px;height:14px;border-radius:4px;
  vertical-align:-2px;margin-right:6px}}
.b7 span{{font-size:16px;color:#6C7683}}
.b7.on span{{color:#C9D2DC}}
.sphead{{display:flex;justify-content:space-between;align-items:baseline;
  font-size:18px;color:#8C97A4;margin:6px 0 1px}}
.sphead em{{font-style:normal;color:#EDF1F5;font-weight:700;font-size:21px}}
/* 折线是 lcol 里唯一的弹性块：天数内容少（没训练、提醒少、没体重行）时，
   弹性空间原本全堆在图表和 .last 之间，形成 10~15% 的整段空洞；
   flex:1 让三段折线按 grow 摊掉富余高度，行高变高而不是留白。
   内容多时 shrink 到 min-height 兜底，不会把底部挤出画幅。 */
.sp{{display:block;width:100%;height:88px;flex:1 1 auto;min-height:42px}}
.chart-h{{font-size:18px;color:#7F8A97;margin:7px 0 1px;letter-spacing:.5px}}
.mini4{{display:flex;gap:9px;margin-bottom:2px}}
.mini4 div{{flex:1;background:#1F262E;border:1px solid #24344A;border-radius:12px;
  padding:6px 6px;text-align:center}}
.mini4 b{{display:block;font-size:24px;font-weight:700;letter-spacing:-.5px}}
.mini4 b small{{font-size:15px;font-weight:400;color:#8C97A4;margin-left:2px}}
.mini4 span{{display:block;font-size:14px;color:#7F8A97;margin-top:2px}}
.rplan{{display:flex;justify-content:space-between;align-items:baseline;gap:14px;
  margin-top:16px;padding-top:14px;border-top:1px solid #2B343E;
  font-size:23px;color:#C9D2DC}}
.rplan em{{font-style:normal;color:#4FD6A6;font-weight:600}}
.rplan span{{font-size:19px;color:#7F8A97;flex:none}}
.flags{{display:flex;flex-direction:column}}
/* 提醒卡比内容高时，把条目均匀摊开而不是挤在顶部留一块空 —— 高度是
   从「今日训练」让过来的，留白应该在条目之间，不是在卡片底部。 */
.flags ul{{list-style:none;flex:1;display:flex;flex-direction:column;gap:12px;justify-content:center}}
.flags li{{font-size:20px;line-height:1.6;color:#C9D2DC;padding-left:20px;position:relative}}
.flags li:before{{content:"";position:absolute;left:0;top:11px;width:8px;height:8px;
  border-radius:50%;background:#D6A75A}}
.last{{font-size:19px;color:#8C97A4;margin-top:auto;line-height:1.45;padding-top:8px}}
.tline{{display:flex;justify-content:space-between;align-items:baseline;
  font-size:18px;color:#C9D2DC;padding:4px 0}}
.tv{{color:#EDF1F5;font-weight:600}}
.delta{{font-style:normal;font-size:18px;font-weight:600;margin-left:10px}}
.delta.up{{color:#4FD6A6}}
.delta.down{{color:#E06A5F}}
.foot{{display:flex;justify-content:space-between;font-size:17px;color:#5C6673;
  margin-top:auto;padding-top:12px;border-top:1px solid #223349}}
.aline{{display:flex;justify-content:space-between;align-items:baseline;gap:12px;
  padding:10px 0;border-bottom:1px solid #223349}}
.aline:last-child{{border-bottom:none}}
.aline span{{font-size:20px;color:#C9D2DC;min-width:0;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.aline b{{font-size:19px;font-weight:600;color:#9AA5B1;white-space:nowrap;flex:none}}
.chd{{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px}}
.chd h3{{margin-bottom:0}}
.chw{{font-size:20px;color:#7F8A97;flex:none}}
.chw b{{font-size:26px;color:#4FD6A6;font-weight:700;margin:0 2px}}
.nutri{{display:flex;flex-direction:column;gap:6px;margin-top:9px}}
.nrow{{display:flex;align-items:center;gap:12px;font-size:19px}}
.nl{{width:48px;color:#9AA5B1;flex:none}}
.ntrack{{flex:1;height:11px;background:#223349;border-radius:7px;overflow:hidden}}
.ntrack i{{display:block;height:100%;border-radius:7px}}
.nv{{width:172px;text-align:right;font-weight:600;font-size:19px;flex:none}}
.empty{{font-size:20px;color:#6C7683}}
.today-card{{display:flex;flex-direction:column;min-height:min-content}}
.today-body{{display:grid;grid-template-columns:1fr 1fr;grid-auto-rows:minmax(0,1fr);
  gap:9px;margin-top:2px}}
.today-empty{{flex:1;display:flex;align-items:center;justify-content:center;text-align:center;padding:0 12px}}
.aact{{min-height:0;display:flex;flex-direction:column;justify-content:center;gap:2px;
  background:#20272F;border:1px solid #24344A;border-radius:12px;padding:10px 14px;
  overflow:hidden}}
.aact.span2{{grid-column:span 2}}
.aact-h{{font-size:16px;color:#8C97A4;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.aact-v{{font-size:30px;font-weight:700;letter-spacing:-.8px;line-height:1.1}}
.aact-v.sm{{font-size:24px}}
.aact-v small{{font-size:15px;font-weight:400;color:#9AA5B1;letter-spacing:0}}
.aact-m{{font-size:15px;color:#7F8A97;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
/* ---- 品牌化：背景走「渐进 + 双辉光」，卡片顶沿一道高光，右下角大水印 ----
   与 ui.py / actcard.py 共用 garmin_cn/brand.py，不在这里另写一套色值。
   0.6.9 把中性灰底换成深空蓝灰、语义绿换成 icon 起点绿 #4FD6A6、
   琥珀降饱和，两张出图和启动器图标的冷调保持一致。body 仍保持纯色。 */
{BR.BRAND_CSS}
{BR.glow_overlay_css("34px 52px 26px")}
{BR.card_css(".card")}
.card{{position:relative}}
.card::before{{content:"";position:absolute;left:0;right:0;top:0;height:1px;
  background:linear-gradient(90deg,transparent,rgba(96,165,250,.42),transparent)}}
.kpi,.bkpi,.aact,.wkd{{background:linear-gradient(180deg,#1B2A3D,#111C2B)}}
.wrap{{position:relative;z-index:1}}
.rc-wm{{right:36px;bottom:70px;opacity:.06}}
.rc-date{{display:flex;align-items:center;flex:0 0 auto}}
.foot .rc-foot{{color:#8FA4BF;font-weight:600;letter-spacing:.5px}}
.foot .rc-foot .rc-badge{{width:20px;height:20px}}

</style></head><body><div class="rc-glow"></div>{BR.watermark_html(420)}<div class="wrap" data-gc-canvas>
<div class="top">
  <div class="rc-date"><div class="date">{d.strftime("%m月%d日")}<small>{DOW[d.weekday()]}</small></div></div>
  <div class="meta">
    <div class="m1">恢复度 <b>{_f(r.get("score"))}</b> / 100</div>
    <div class="m2">{esc(src)} · 已同步 {esc((sync_at or "")[:16]) or esc(res["date"])}</div>
  </div>
</div>

<div class="hero">
  <div class="score-box">
    <div class="score">{_f(r.get("score"))}<small>分</small></div>
    <div class="level">{esc(r.get("level"))}<span>恢复度</span></div>
  </div>
  <div class="bars">{_bars(r.get("parts") or [])}</div>
</div>

<div class="card sess">
  <h3>今日处方</h3>
  <div class="st">{esc(s["type"])}</div>
  <div class="chips"><span class="chip main">{esc(s["duration"])}</span><span class="chip">{esc(s["intensity"])}</span></div>
  <div class="why">{esc(s["why"])}</div>
  {ai_html}
  {nutri_chips}
  {rp_line}
</div>

<div class="row4">
  <div class="kpi"><b>{_f(L.get("acwr"), "", 2)}</b><span>ACWR</span></div>
  <div class="kpi"><b>{_f(L.get("ctl"), "", 1)}</b><span>CTL 体能</span></div>
  <div class="kpi"><b>{_f(L.get("atl"), "", 1)}</b><span>ATL 疲劳</span></div>
  <div class="kpi"><b>{_f(L.get("tsb"), "", 1)}</b><span>TSB 余量</span></div>
</div>
{body_row}

<div class="card plan-card">
  <div class="plan-title">{plan_title}</div>
  {week_body}
  {rp_line}
</div>
{coach_html}

<div class="dual">
  <div class="card lcol">
      <h3>{esc(_data_head)}</h3>
      <div class="mini4">
        <div><b>{_f(sl.get("total_h"), "", 1)}<small>h</small></b><span>{sleep_sub}</span></div>
        <div><b>{_f(t.get("hrv_night"), "", 0)}<small>ms</small></b><span>HRV 夜间</span></div>
        <div><b>{_f(t.get("resting_hr"), "", 0)}<small>bpm</small></b><span>静息心率</span></div>
        <div><b>{bb_val}<small>%</small></b><span>身体电量 · {esc(bb_sub)}</span></div>
      </div>
      <div class="chart-h">睡眠（近 7 天 · 柱高为小时）</div>
      {sleep_bars}
      <div class="chart-h">{esc(bb_head)}</div>
      {bb_spark}
      <div class="chart-h">HRV 与静息心率（近 14 天）</div>
      {hrv_spark}
      {rhr_spark}
      <div class="last" style="border-top:1px solid #223349">
        睡眠债 {_f(sl.get("debt_h"), " h", 1)}{debt_nap} · 压力 {_f(t.get("avg_stress"))} · 步数 {_f(t.get("steps"))}{floors_txt}
        {("　最近一次：" + last) if last else ""}
      </div>
  </div>
  {today_card}
  <div class="card flags">
    <h3>需要注意</h3>
    <ul>{flags}</ul>
  </div>
  <div class="card tcard">
    <h3>本周 vs 上周</h3>
    {_trend_rows(res.get("history") or [])}
  </div>
</div>

<div class="foot"><span class="rc-foot">{BR.badge_html(18)}RunnerCoach</span><span>数据来源：{esc(src)}</span><span>生成于 {dt.datetime.now().strftime("%m-%d %H:%M")}</span></div>
</div>
{_EXPORT_PROBE}</body></html>"""


def shoot(html_path: Path, png_path: Path, browser: str | None = None) -> Path:
    """调浏览器无头截一张固定尺寸的 PNG。"""
    browser = browser or _find_browser()
    if not browser:
        raise RuntimeError("找不到 Chrome / Edge，无法生成图片")
    png_path.parent.mkdir(parents=True, exist_ok=True)
    if png_path.exists():
        png_path.unlink()
    cmd = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--force-device-scale-factor=1",
        "--default-background-color=00000000",
        f"--window-size={W},{H}",
        f"--screenshot={png_path}",
        html_path.resolve().as_uri(),
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=90)
    if not png_path.exists():
        raise RuntimeError(
            "浏览器没有产出图片：" + (proc.stderr or b"").decode("utf-8", "ignore")[:300]
        )
    return png_path


def render(res: dict, out_png: Path, sync_at: str | None = None) -> Path:
    """生成 PNG；中间 HTML 存到 output/ 下同名的 .html，方便排查排版。"""
    out_png = Path(out_png)
    html_path = out_png.with_suffix(".html")
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(build_html(res, sync_at), encoding="utf-8")
    return shoot(html_path, out_png)
