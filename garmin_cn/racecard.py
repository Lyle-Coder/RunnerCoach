"""赛后复盘图：把一场比赛渲染成一张竖版 PNG（1080×1920）。

和活动图（actcard）不同，这张图回答的是四个问题：
1. 成绩到底多少——以组委会官方成绩为准（净成绩 / 枪声），不是手表的起停时间；
2. 跟赛前定的目标差多少——PB 与「差多少秒」放在最显眼处；
3. 怎么跑的——分段配速、心率漂移、爬升，这些是复盘的依据；
4. 接下来怎么练——赛后 7 天的恢复课表。

为什么单独写一个模块而不是复用 actcard
--------------------------------------
活动图的语义是「这次运动发生了什么」，以手表数据为主体；复盘图的主体是
「官方成绩 + 目标差 + 分段证据 + 恢复安排」，两者信息结构完全不同，硬塞进
同一个模板只会两边都别扭。共用的是零件：分段曲线、心率分区条、走法提示。

口径说明
--------
- 官方成绩来自 `race_results` 表（组委会成绩），手表的 duration_s 只作参考；
  两者差几十秒是常态（起跑信号 vs 个人计时起点），不要混用。
- 目标时间存在 race_results.target_s，差值 = 实际 − 目标（负数=超额完成）。
- 分段用 3km 合并（每 3 圈一段），和赛前计划里的分段口径一致，便于对照。
"""
from __future__ import annotations

import datetime as dt
import html
import json
import math
import os
import subprocess
from pathlib import Path

from . import brand as BR
from .card import _find_browser
from .exportspec import probe as _export_probe
from .metrics import num
from . import hrzones as HRZ

W, H = 1080, 1920
_EXPORT_PROBE = _export_probe()

BG = "#12161B"
CARD = "#1A2027"
LINE = "#2B333C"
TEXT = "#EDF1F5"
MUTED = "#8B97A5"
ACC = "#4FD6A6"
ACC2 = "#5FA8E0"
WARM = "#E8843A"
GOOD = "#4FD6A6"
BAD = "#E06A5F"

ZONE_COLORS = ["#4E8FD6", "#2FBF8F", "#E8C547", "#E8843A", "#E05555"]
# 与活动图同源：佳明 Connect 的 Z1–Z5 名称由 hrzones 统一提供
ZONE_NAMES = list(HRZ.ZONE_LABELS)


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def hms(sec, dash: str = "—") -> str:
    """秒 → 时:分:秒。负数保留符号（「较目标差 −9:39」这类要用）。"""
    if sec is None:
        return dash
    s = int(round(sec))
    if s == 0:
        return dash
    sign = "−" if s < 0 else ""
    s = abs(s)
    h, m, ss = s // 3600, (s % 3600) // 60, s % 60
    body = f"{h}:{m:02d}:{ss:02d}" if h else f"{m}:{ss:02d}"
    return sign + body


def pace_txt(sec, dash: str = "—") -> str:
    if not sec:
        return dash
    s = int(round(sec))
    return f"{s // 60}'{s % 60:02d}\""


def _signed(sec) -> str:
    """+1:12 / −0:45，负号用真减号，视觉上和正文一致。"""
    if sec is None:
        return "—"
    sign = "−" if sec < 0 else "+"
    return sign + hms(abs(sec))


# ------------------------------------------------------------------ 分段
def split_rows(laps: list[dict], step_km: int = 3, target_pace: float | None = None,
               dist_scale: float = 1.0, time_offset: float = 0.0) -> list[dict]:
    """把逐公里分圈合并成每 step_km 公里一段。

    合并后的每段给三件事：段配速、段平均心率、累计用时。
    累计用时比「每段配速」更能说明问题——负分段跑的人看累计曲线一眼就懂。

    口径对齐（这两行是全表能自洽的关键）
    ------------------------------------
    手表量的距离通常比官方距离大 1~2%（GPS 误差 + 折返抖动），起停也和组委会
    的计时点差几十秒。直接把手表数据和官方目标放一起比，末行会差出好几秒，
    和顶部「较目标差」对不上。所以这里做两步归一：

    - `dist_scale`：把手表距离缩放到官方距离口径；
    - `time_offset`：给手表时间加一个常数偏移，使累计终点正好等于官方净成绩。

    归一后末行的「较目标差」与顶部的官方差值严格相等，整张图自洽。
    """
    real = [l for l in laps if (l.get("pace_s") or 0) > 0]
    if not real:
        return []
    # 先按 step_km 分组，再把「不足 1 km 的收尾零头段」并进上一段。
    # 直接在圈列表上合并会把上一段的时间算两遍——合并必须发生在分组之后。
    groups: list[list[dict]] = [real[i:i + step_km] for i in range(0, len(real), step_km)]
    if len(groups) > 1:
        last_d = sum(l["dist_m"] for l in groups[-1])
        if last_d < 1000:
            groups[-2] = groups[-2] + groups[-1]
            groups.pop()

    out, cum_t, cum_d = [], 0.0, 0.0
    for seg in groups:
        dist = sum(l["dist_m"] for l in seg) / 1000
        dur = sum(l["dur_s"] for l in seg)
        cum_t += dur
        cum_d += dist * dist_scale
        hrs = [l["hr_avg"] for l in seg if l.get("hr_avg")]
        out.append({
            "idx": f"{seg[0]['idx']}–{seg[-1]['idx']}" if len(seg) > 1 else str(seg[0]["idx"]),
            "dist_km": dist,
            "dur_s": dur,
            "pace_s": dur / dist if dist > 0.05 else None,
            "hr_avg": round(sum(hrs) / len(hrs)) if hrs else None,
            # 累计时间换算到官方计时口径
            "cum_t": cum_t + time_offset,
            "cum_d": cum_d,
            "gap_s": ((cum_t + time_offset) - target_pace * cum_d) if target_pace else None,
        })
    return out


def _svg_splits(rows: list[dict], w: int, h: int) -> str:
    """分段配速条：横向条长=用时（越短越好），附累计时间与落后/领先秒数。"""
    if not rows:
        return '<div class="empty">无分圈数据</div>'
    paces = [r["pace_s"] for r in rows if r["pace_s"]]
    if not paces:
        return '<div class="empty">无配速数据</div>'
    fast, slow = min(paces), max(paces)
    span = (slow - fast) or 1
    row_h = 40
    out = []
    for r in rows:
        k = (r["pace_s"] - fast) / span
        # 慢的条更长、颜色更暖；快的条短、偏冷。长度与颜色两重编码同一件事。
        color = _mix("#2FBF8F", "#E8843A", k)
        bar_w = max(8.0, 20 + k * 80)
        gap = r.get("gap_s")
        if gap is None:
            gap_txt = '<span class="sg">—</span>'
        elif gap < 0:
            gap_txt = f'<span class="sg lead">−{hms(abs(gap))}</span>'
        else:
            gap_txt = f'<span class="sg lag">+{hms(gap)}</span>'
        hr = f"{r['hr_avg']:.0f}" if r.get("hr_avg") else "—"
        out.append(
            f'<div class="sr">'
            f'<span class="sidx">{esc(r["idx"])}</span>'
            f'<span class="sbar"><i style="width:{bar_w:.1f}%;background:{color}"></i></span>'
            f'<span class="sp">{pace_txt(r["pace_s"])}</span>'
            f'<span class="shr">{hr}</span>'
            f'<span class="sc">{hms(r["cum_t"])}</span>'
            f'{gap_txt}'
            f'</div>')
    return "".join(out)


def _mix(c1: str, c2: str, k: float) -> str:
    a = [int(c1.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    b = [int(c2.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join(f"{round(a[i] + (b[i] - a[i]) * k):02x}" for i in range(3))


def _zones(raw: dict) -> list[tuple[str, str, float, float]]:
    descs = HRZ.zone_descriptions()
    out = []
    for i in range(5):
        t = num(raw.get(f"hrTimeInZone_{i + 1}")) or 0
        out.append((ZONE_NAMES[i], descs[i], t, 0))
    tot = sum(x[2] for x in out) or 1
    return [(n, d, t, t / tot * 100) for n, d, t, _ in out]


def _zone_block(raw: dict) -> str:
    zs = _zones(raw)
    tot = sum(z[2] for z in zs)
    if not tot:
        return '<div class="empty">本次未记录心率分区</div>'
    bar = "".join(
        f'<i style="width:{z[3]:.2f}%;background:{ZONE_COLORS[i]}"></i>'
        for i, z in enumerate(zs) if z[3] >= 0.4)
    rows = []
    for i, (n, d, t, pct) in enumerate(zs):
        rows.append(
            f'<div class="zr"><span class="zd" style="background:{ZONE_COLORS[i]}"></span>'
            f'<span class="zn">{n}<em>{esc(d)}</em></span>'
            f'<span class="zb"><i style="width:{min(100, pct):.1f}%;background:{ZONE_COLORS[i]}"></i></span>'
            f'<span class="zv">{hms(t, "0:00")}</span>'
            f'<span class="zp" style="color:{ZONE_COLORS[i]}">{pct:.0f}%</span></div>')
    return (f'<div class="zstack">{bar}</div>' + "".join(rows))


# ------------------------------------------------------------------ 组装
def build_html(race: dict, detail: dict, week_rows: list[dict],
               coach_text: str = "", row: dict | None = None) -> str:
    """race_results 一行 + activity_detail 明细 + 赛后一周课表 → 复盘图 HTML。

    `row` 是 activities 表里那一行（带 raw）。心率分区、爬升、负荷这些字段
    只存在于 raw，明细 payload 里没有，所以两处都得用。
    """
    row = row or {}
    net = num(race.get("net_s"))
    gun = num(race.get("gun_s"))
    target = num(race.get("target_s"))
    dist_km = num(race.get("distance_km")) or 21.0975
    best_t = net or gun
    gap = (best_t - target) if (target and best_t) else None
    gap_pct = (gap / target * 100) if (gap is not None and target) else None
    # 目标配速（秒/公里）：分段「较目标差」用它做基准
    target_pace = (target / dist_km) if target else None

    laps = (detail.get("laps") or [])
    raw = row.get("raw") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    summary = detail.get("summary") or {}

    # 官方距离才是比赛口径；手表量到的距离通常偏大 1~2%（GPS 误差 + 折返抖动）
    watch_dist = (num(row.get("distance_m")) or 0) / 1000
    watch_dur = num(row.get("duration_s"))
    pace_avg = (best_t / dist_km) if best_t else None

    # 归一参数：把手表距离缩放到官方口径、把手表时间平移到官方计时口径
    dist_scale = (dist_km / watch_dist) if watch_dist else 1.0
    time_offset = (best_t - watch_dur) if watch_dur else 0.0

    ascent = num(raw.get("elevationGain")) or num(row.get("ascent")) or 0
    hr_avg = num(raw.get("averageHR")) or num(row.get("avg_hr"))
    hr_max = num(raw.get("maxHR")) or num(row.get("max_hr"))
    te_a = num(row.get("aerobic_te"))
    te_n = num(row.get("anaerobic_te"))
    load = num(row.get("load"))
    kcal = num(row.get("calories"))
    vmax = num(raw.get("vO2MaxValue"))

    # 分段：手表累计时间作分段基线（官方成绩与手表起停口径不同，分段不能混）
    rows = split_rows(laps, 3, target_pace=target_pace,
                      dist_scale=dist_scale, time_offset=time_offset)

    # 前后半程：用活动模块算好的 drift（按里程对半，不是按段数对半）
    drift = summary.get("drift") or {}
    drift_txt = ""
    if drift:
        dh = drift["hr_second"] - drift["hr_first"]
        dp = drift["pace_second"] - drift["pace_first"]
        if dp < -3:
            trend, tc = "负分段", GOOD
        elif dp > 3:
            trend, tc = "正分段", WARM
        else:
            trend, tc = "前后持平", MUTED
        drift_txt = (
            f'<b style="color:{tc}">{trend}</b>　'
            f'前半程 {pace_txt(drift["pace_first"])}／{drift["hr_first"]} bpm　→　'
            f'后半程 {pace_txt(drift["pace_second"])}／{drift["hr_second"]} bpm　'
            f'心率{"上浮" if dh > 0 else "回落"} {abs(dh)} bpm')

    # 赛后 7 天课表
    wk_rows = []
    for r in week_rows:
        kind = r.get("kind") or "rest"
        cls = "wr" + (" rest" if kind == "rest" else "")
        wk_rows.append(
            f'<div class="{cls}"><span class="wd">{esc(r.get("dow") or "")}</span>'
            f'<span class="wb">{esc(r.get("main") or "")}</span>'
            f'<span class="ws">{esc(r.get("sub") or "")}</span></div>')

    # 跑法画像：填掉心率分区卡下方的空白，同时把「怎么跑的」补齐。
    # 最快/最慢公里直接从分段里取，一眼能看出这场的节奏分布。
    tech = []
    cad_avg = num(summary.get("cad_avg"))
    stride_avg = num(summary.get("stride_avg"))
    if cad_avg:
        tech.append((f"{cad_avg:.0f}", "平均步频 spm"))
    if stride_avg:
        tech.append((f"{stride_avg:.0f}", "平均步幅 cm"))
    if rows:
        fast = min(rows, key=lambda r: r["pace_s"] or 999)
        slow = max(rows, key=lambda r: r["pace_s"] or 0)
        tech.append((pace_txt(fast["pace_s"]), f"最快 3km（{fast['idx']}）"))
        tech.append((pace_txt(slow["pace_s"]), f"最慢 3km（{slow['idx']}）"))
    z5 = _zones(raw)[4][3] if raw else 0
    tech.append((f"{z5:.0f}%", f"{HRZ.zone_label(4)}占比"))
    tech.append((f"{load:.0f}" if load else "—", "训练负荷"))
    tech_html = "".join(
        f'<div class="tt"><b>{esc(v)}</b><span>{esc(u)}</span></div>' for v, u in tech)

    # 英雄区：官方成绩 + 目标差
    if gap is None:
        gap_html = '<div class="hg"><span class="gl">目标</span><span class="gv">—</span></div>'
    else:
        ahead = gap < 0
        gap_cls = "good" if ahead else ("warn" if gap > 0 else "")
        gap_html = (
            f'<div class="hg"><span class="gl">较目标</span>'
            f'<span class="gv {gap_cls}">{_signed(gap)}</span>'
            f'<span class="gp">{"" if gap_pct is None else f"{gap_pct:+.1f}%"}</span></div>')

    stats = [
        (f"{dist_km:.2f}", "官方距离 km"),
        (pace_txt(pace_avg), "平均配速 /km"),
        (f"{ascent:.0f}", "累计爬升 m"),
        (f"{hr_avg:.0f}" if hr_avg else "—", "平均心率 bpm"),
        (f"{hr_max:.0f}" if hr_max else "—", "最大心率 bpm"),
        (f"{load:.0f}" if load else "—", "训练负荷"),
    ]
    stat_html = "".join(
        f'<div class="st"><b>{esc(v)}</b><span>{esc(u)}</span></div>' for v, u in stats)

    extra = []
    if te_a:
        extra.append(f"有氧 TE {te_a:.1f}")
    if te_n:
        extra.append(f"无氧 TE {te_n:.1f}")
    if kcal:
        extra.append(f"{kcal:.0f} kcal")
    if vmax:
        extra.append(f"VO2max {vmax:.0f}")
    extra_html = "　·　".join(esc(x) for x in extra)

    # 手表与官方的时间差：赛后图里写出来，避免以后自己看表时困惑
    diff_html = ""
    if watch_dur and best_t:
        d = watch_dur - best_t
        diff_html = (f'手表 {hms(watch_dur)} / {watch_dist:.2f} km　'
                     f'与官方差 {d:+.0f} 秒（起停口径不同，以官方为准）')

    date_txt = race.get("date") or ""
    try:
        wd = "周" + "一二三四五六日"[dt.date.fromisoformat(date_txt).weekday()]
    except Exception:
        wd = ""

    coach_html = ""
    if coach_text:
        coach_html = f'<div class="coach">{esc(coach_text)}</div>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width={W},initial-scale=1,viewport-fit=cover">
<style>{_CSS}</style></head>
<body><div class="rc-glow"></div>{BR.watermark_html(400)}
<div class="page" data-gc-canvas>

  <div class="head">
    <div class="hbrand">{BR.badge_html(44)}</div>
    <div>
      <div class="hname">{esc(race.get("event") or "比赛")}</div>
      <div class="hsub">{esc(date_txt)} {esc(wd)}{(' · 号码 ' + esc(race['bib'])) if race.get('bib') else ''}</div>
    </div>
    <div class="htag">赛后复盘</div>
  </div>

  <div class="hero">
    <div class="hl">
      <div class="hlab">官方净成绩</div>
      <div class="hval">{esc(hms(net))}</div>
      <div class="hsub2">枪声 {esc(hms(gun))}</div>
    </div>
    <div class="hr">
      {gap_html}
    </div>
  </div>
  {f'<div class="diff">{esc(diff_html)}</div>' if diff_html else ''}
  <div class="stats">
    {stat_html}
  </div>
  <div class="meta">{extra_html}</div>

  <div class="card">
    <div class="ctitle">分段表现<span>每 3 km 合并 · 累计时间 / 较目标差</span></div>
    <div class="shead"><span>段</span><span></span><span>配速</span><span>心率</span><span>累计</span><span>差</span></div>
    {_svg_splits(rows, 0, 0)}
    {f'<div class="drift">{drift_txt}</div>' if drift_txt else ''}
  </div>

  <div class="dual">
    <div class="card">
      <div class="ctitle">心率分区<span>占比 / 时长</span></div>
      {_zone_block(raw)}
      <div class="ctitle tech-t">跑法画像<span>技术与节奏</span></div>
      <div class="tgrid">{tech_html}</div>
    </div>
    <div class="card wkcard">
      <div class="ctitle">赛后 7 天<span>恢复课表</span></div>
      {"".join(wk_rows)}
    </div>
  </div>

  {coach_html}
  <div class="foot"><span class="rc-foot">{BR.badge_html(18)}RunnerCoach</span> · 成绩以组委会公布为准 · 数据 Garmin</div>
</div>
{_EXPORT_PROBE}
</body></html>"""


_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
:root{--page-w:1080px}
body{width:var(--page-w);overflow:hidden;background:#12161B;
  font-family:"Microsoft YaHei","PingFang SC",sans-serif;color:#EDF1F5}
.page{width:var(--page-w);padding:28px 32px 34px;display:flex;flex-direction:column;gap:13px}
.head{display:flex;align-items:flex-end;justify-content:space-between}
.hname{font-size:42px;font-weight:700;letter-spacing:.5px}
.hsub{font-size:22px;color:#8B97A5;margin-top:8px}
.htag{font-size:19px;color:#4FD6A6;border:1px solid #2A4A42;background:#14261F;
  padding:7px 16px;border-radius:22px;flex:none}
.hero{display:flex;align-items:center;justify-content:space-between;gap:24px;
  background:#1A2027;border:1px solid #2B333C;border-radius:16px;padding:20px 26px}
.hl{flex:none}
.hlab{font-size:20px;color:#8B97A5;margin-bottom:6px}
.hval{font-size:82px;font-weight:800;line-height:1;letter-spacing:-2px;
  background:linear-gradient(180deg,#FFFFFF,#4FD6A6);-webkit-background-clip:text;
  -webkit-text-fill-color:transparent}
.hsub2{font-size:21px;color:#76838F;margin-top:8px}
.hr{flex:none;text-align:right}
.gl{font-size:19px;color:#8B97A5;display:block}
.gv{font-size:44px;font-weight:700;letter-spacing:-1px;display:block;line-height:1.15}
.gv.good{color:#4FD6A6}
.gv.warn{color:#E8843A}
.gp{font-size:17px;color:#76838F}
.diff{font-size:17px;color:#76838F;padding:0 4px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.st{background:#1A2027;border:1px solid #2B333C;border-radius:12px;padding:12px 16px;text-align:center}
.st b{display:block;font-size:34px;font-weight:700;line-height:1.15;letter-spacing:-.5px}
.st span{font-size:16px;color:#8B97A5}
.meta{font-size:18px;color:#8B97A5;text-align:center}
.card{background:#1A2027;border:1px solid #2B333C;border-radius:16px;padding:16px 20px}
.ctitle{font-size:23px;color:#D3DBE4;font-weight:600;margin-bottom:12px;
  display:flex;justify-content:space-between;align-items:baseline}
.ctitle span{font-size:16px;color:#76838F;font-weight:400}
.shead,.sr{display:flex;align-items:center;gap:9px}
.shead{font-size:15px;color:#76838F;padding-bottom:6px;border-bottom:1px solid #2B333C}
.shead span:nth-child(1){width:52px;text-align:right}
.shead span:nth-child(2){flex:1}
.shead span:nth-child(3){width:72px;text-align:right}
.shead span:nth-child(4){width:52px;text-align:right}
.shead span:nth-child(5){width:70px;text-align:right}
.shead span:nth-child(6){width:66px;text-align:right}
.sr{padding:4px 0}
.sidx{width:52px;text-align:right;font-size:18px;color:#8B97A5;flex:none}
.sbar{flex:1;height:11px;background:#141A20;border-radius:6px;overflow:hidden}
.sbar i{display:block;height:100%;border-radius:6px}
.sp{width:72px;text-align:right;font-size:21px;font-weight:600;flex:none}
.shr{width:52px;text-align:right;font-size:18px;color:#E8843A;flex:none;
  font-variant-numeric:tabular-nums}
.sc{width:70px;text-align:right;font-size:18px;color:#8B97A5;flex:none;
  font-variant-numeric:tabular-nums}
.sg{width:66px;text-align:right;font-size:18px;flex:none;font-variant-numeric:tabular-nums}
.sg.lead{color:#4FD6A6}
.sg.lag{color:#E8843A}
.drift{font-size:19px;color:#8B97A5;margin-top:11px;padding-top:11px;border-top:1px solid #2B333C}
.dual{display:flex;gap:13px;align-items:stretch}
.dual>.card{flex:1;min-width:0}
.zstack{display:flex;height:18px;border-radius:6px;overflow:hidden;background:#141A20;margin-bottom:9px}
.zstack i{display:block}
.zr{display:flex;align-items:center;gap:8px;margin-top:7px}
.zd{width:10px;height:10px;border-radius:3px;flex:none}
.zn{width:74px;font-size:18px;color:#D3DBE4;flex:none;white-space:nowrap}
.zn em{font-style:normal;font-size:14px;color:#76838F;margin-left:4px;
       display:inline-block;max-width:44px;overflow:hidden;
       text-overflow:ellipsis;white-space:nowrap}
.zb{flex:1;height:9px;background:#141A20;border-radius:5px;overflow:hidden}
.zb i{display:block;height:100%;border-radius:5px}
.zv{width:58px;text-align:right;font-size:17px;color:#8B97A5;flex:none;
  font-variant-numeric:tabular-nums}
.zp{width:42px;text-align:right;font-size:18px;font-weight:700;flex:none}
.wkcard{display:flex;flex-direction:column}
.tech-t{margin-top:16px;padding-top:14px;border-top:1px solid #2B333C}
.tgrid{display:grid;grid-template-columns:repeat(2,1fr);gap:11px 14px}
.tt b{display:block;font-size:24px;font-weight:700;line-height:1.2;color:#D3DBE4}
.tt span{font-size:15px;color:#76838F}
.wr{display:flex;align-items:center;gap:9px;padding:5px 0;font-size:18px}
.wd{width:34px;color:#76838F;flex:none}
.wb{width:104px;font-weight:600;color:#D3DBE4;flex:none}
.ws{flex:1;color:#8B97A5;font-size:16px;text-align:right}
.wr.rest .wb{color:#76838F;font-weight:400}
.coach{background:#16211C;border:1px solid #2A4438;border-radius:16px;padding:16px 20px;
  font-size:20px;line-height:1.75;color:#D3DBE4}
.foot{font-size:15px;color:#5A6675;text-align:center;letter-spacing:.3px}
.empty{font-size:18px;color:#64707E;padding:12px 0}
""" + BR.BRAND_CSS + BR.glow_overlay_css("28px 32px 34px") + BR.card_css(".card") + """
/* 赛后卡与看板 / 活动图共用同一套品牌角标、渐进与辉光 */
.page{position:relative;z-index:1}
.card{position:relative}
.card::before{content:"";position:absolute;left:0;right:0;top:0;height:1px;
  background:linear-gradient(90deg,transparent,rgba(96,165,250,.40),transparent)}
.rc-wm{right:36px;bottom:70px;opacity:.055}
.hbrand{flex:none;align-self:center;display:flex;align-items:center;margin-right:4px}
.foot .rc-foot{margin-right:6px;color:#8FA4BF}
.foot .rc-foot .rc-badge{width:18px;height:18px}
"""


# ------------------------------------------------------------------ 出图
def shoot(html_path: Path, png_path: Path) -> Path:
    browser = _find_browser()
    if not browser:
        raise RuntimeError("找不到 Chrome / Edge，无法生成图片")
    png_path.parent.mkdir(parents=True, exist_ok=True)
    if png_path.exists():
        png_path.unlink()
    cmd = [
        browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--force-device-scale-factor=1", "--default-background-color=00000000",
        f"--window-size={W},{H + 420}", f"--screenshot={png_path.resolve()}",
        html_path.resolve().as_uri(),
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=90)
    if not png_path.exists():
        raise RuntimeError("浏览器没有产出图片：" +
                           (proc.stderr or b"").decode("utf-8", "ignore")[:300])
    from .pngutil import fit_to_frame, trim_bg
    trim_bg(png_path, bg=BG)
    fit_to_frame(png_path, bg=BG)
    return png_path


def render(race: dict, detail: dict, week_rows: list[dict], out_png: Path,
           coach_text: str = "", row: dict | None = None) -> Path:
    out_png = Path(out_png)
    html_path = out_png.with_suffix(".html")
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(
        build_html(race, detail, week_rows, coach_text, row=row), encoding="utf-8")
    return shoot(html_path, out_png)
