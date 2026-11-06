"""每日看板：生成单文件 HTML（无外部依赖，可直接双击打开）。"""
from __future__ import annotations

import datetime as dt
import html as _html
import math
from pathlib import Path

from .coach import brief
from .metrics import baseline, num

TYPE_CN = {
    "running": "跑步", "trail_running": "越野跑", "treadmill_running": "跑步机",
    "cycling": "骑行", "road_biking": "公路骑行", "mountain_biking": "山地骑行",
    "swimming": "游泳", "lap_swimming": "泳池", "open_water_swimming": "公开水域",
    "walking": "步行", "hiking": "徒步", "strength_training": "力量",
    "indoor_cardio": "室内有氧", "indoor_cycling": "动感单车", "yoga": "瑜伽",
    "elliptical": "椭圆机", "rowing": "划船", "other": "其他",
}
WEEK_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

CSS = """
:root{
  --bg:#F4F6F2; --card:#FFFFFF; --line:#E3E7DF; --line-2:#EDF0EA;
  --ink:#1B1F1A; --ink-2:#4A5147; --muted:#7A8276; --faint:#A5AC9F;
  --green:#1D9E75; --green-soft:#E4F3EC; --amber:#C08A16; --amber-soft:#FBF1DA;
  --red:#C2453F; --red-soft:#FAE7E5; --blue:#3A6EA5; --blue-soft:#E8F0F8;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased;line-height:1.6;font-size:14px}
.wrap{max-width:1120px;margin:0 auto;padding:28px 20px 56px}
.top{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;margin-bottom:18px;flex-wrap:wrap}
.top h1{margin:0;font-size:22px;font-weight:600;letter-spacing:.2px}
.top .sub{color:var(--muted);font-size:12.5px;margin-top:4px}
.badge{display:inline-flex;align-items:center;gap:6px;background:var(--card);border:1px solid var(--line);
  border-radius:999px;padding:5px 12px;font-size:12px;color:var(--ink-2)}
.dot{width:6px;height:6px;border-radius:50%;background:var(--green)}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px}
.hero{display:grid;grid-template-columns:1.05fr 1.15fr .95fr;gap:14px;margin-bottom:14px}
.metrics{display:grid;grid-template-columns:repeat(6,1fr);gap:14px;margin-bottom:14px}
.two{display:grid;grid-template-columns:1.45fr 1fr;gap:14px;margin-bottom:14px}
h2.sec{font-size:12px;font-weight:600;color:var(--muted);letter-spacing:1.2px;margin:26px 0 10px}
.card h3{margin:0 0 12px;font-size:12.5px;font-weight:600;color:var(--muted);letter-spacing:.8px}
.ring-wrap{display:flex;align-items:center;gap:16px}
.ring-num{font-size:32px;font-weight:600;line-height:1;font-variant-numeric:tabular-nums}
.ring-lv{font-size:12.5px;margin-top:5px}
.parts{margin-top:14px;display:flex;flex-direction:column;gap:7px}
.part{display:grid;grid-template-columns:64px 1fr 46px;align-items:center;gap:8px;font-size:12px;color:var(--ink-2)}
.bar{height:5px;border-radius:3px;background:var(--line-2);overflow:hidden}
.bar i{display:block;height:100%;border-radius:3px;background:var(--green)}
.part span:last-child{text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
.sess-type{font-size:21px;font-weight:600;letter-spacing:.2px;margin-bottom:2px}
.sess-meta{display:flex;gap:8px;margin:10px 0 10px;flex-wrap:wrap}
.chip{font-size:12px;padding:3px 9px;border-radius:999px;background:var(--green-soft);color:#0F6E56}
.chip.gray{background:var(--line-2);color:var(--ink-2)}
.sess-why{font-size:13px;color:var(--ink-2);line-height:1.65}
.flags{list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:9px}
.flags li{display:flex;gap:9px;font-size:13px;line-height:1.55;color:var(--ink-2)}
.fdot{width:7px;height:7px;border-radius:50%;margin-top:7px;flex:0 0 auto}
.f-alert{background:var(--red)} .f-warn{background:var(--amber)} .f-ok{background:var(--green)}
.m-val{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1.1;margin-top:2px}
.m-val small{font-size:12px;font-weight:400;color:var(--muted);margin-left:2px}
.m-delta{font-size:11.5px;color:var(--muted);margin-top:3px;font-variant-numeric:tabular-nums}
.m-delta b{font-weight:600}
.up{color:var(--green)} .down{color:var(--red)} .flat{color:var(--muted)}
.m-spark{margin-top:10px;height:34px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;font-weight:500;color:var(--muted);font-size:11.5px;padding:0 8px 8px;border-bottom:1px solid var(--line)}
td{padding:9px 8px;border-bottom:1px solid var(--line-2);color:var(--ink-2);font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
td.name{color:var(--ink);max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* 活动表 8 列，窄屏放不下：外层容器负责横向滚动，页面本身永不横向偏移。
   之前长活动名会把后面的列推出表格、整页跟着歪——根因是溢出发生在
   body 层；现在溢出被锁在 .tbl-scroll 里。 */
.tbl-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
.tbl-scroll table{min-width:620px}
.tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:5px;background:var(--blue-soft);color:var(--blue)}
.note{font-size:13.5px;line-height:1.8;color:var(--ink-2);white-space:pre-wrap}
.note-meta{font-size:11.5px;color:var(--faint);margin-top:12px}
.empty{color:var(--faint);font-size:13px}
.placeholder{border:1px dashed #D6DBD0;border-radius:12px;padding:22px;text-align:center;background:#FBFCF9}
.placeholder b{display:block;font-size:13px;font-weight:500;color:var(--ink-2);margin-bottom:4px}
.placeholder span{font-size:12.5px;color:var(--faint)}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:11.5px;color:var(--muted);margin-top:12px}
.legend i{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:5px;vertical-align:middle}
.body-grid{display:grid;grid-template-columns:1fr 1.45fr 1fr;gap:14px;margin-bottom:14px}
.bnum{font-size:36px;font-weight:600;line-height:1.05;font-variant-numeric:tabular-nums}
.bnum small{font-size:13px;font-weight:400;color:var(--muted);margin-left:4px}
.bdelta{font-size:11.5px;color:var(--muted);margin-top:8px;line-height:1.75;font-variant-numeric:tabular-nums}
.bdelta b{font-weight:600}
.bgrid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:9px}
/* n3=三格营养素。必须走类而不是内联样式：内联 style 的优先级高于媒体查询，
   窄屏换列的规则会被覆盖；再配 minmax(0,1fr)+bcell min-width:0，
   nowrap 文字才会省略号收尾而不是把格子（连带整页）撑出屏幕。 */
.bgrid.n3{grid-template-columns:repeat(3,minmax(0,1fr))}
.bcell{background:#FAFBF8;border:1px solid var(--line-2);border-radius:10px;padding:8px 10px;min-width:0}
.bcell b{display:block;font-size:16.5px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1.3}
.bcell b small{font-size:10.5px;font-weight:400;color:var(--muted);margin-left:2px}
.bcell span{font-size:10.5px;color:var(--muted);display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bnote{font-size:11.5px;color:var(--faint);line-height:1.75;margin-top:10px}
.nbar{display:flex;height:10px;border-radius:5px;overflow:hidden;margin:4px 0 12px;background:var(--line-2)}
.nbar i{display:block;height:100%}
.nlist{list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:8px}
.nlist li{font-size:12px;line-height:1.65;color:var(--ink-2);padding-left:15px;position:relative}
.nlist li:before{content:"";position:absolute;left:0;top:7px;width:5px;height:5px;
  border-radius:50%;background:var(--green);flex:0 0 auto}
.nlist li.warn:before{background:var(--amber)}
.nlist li.alert:before{background:var(--red)}
.kv{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:14px}
.kv div{background:#FAFBF8;border:1px solid var(--line-2);border-radius:10px;padding:10px 12px;min-width:0}
.kv b{display:block;font-size:16px;font-weight:600;font-variant-numeric:tabular-nums}
.kv span{font-size:11.5px;color:var(--muted)}
.foot{margin-top:26px;font-size:11.5px;color:var(--faint);line-height:1.9;border-top:1px solid var(--line);padding-top:14px}
@media(max-width:960px){
  .hero{grid-template-columns:1fr}
  .two{grid-template-columns:1fr}
  .body-grid{grid-template-columns:1fr}
  .metrics{grid-template-columns:repeat(3,1fr)}
}
@media(max-width:560px){
  .wrap{padding:18px 14px 40px}
  .card{padding:14px;border-radius:14px}
  .hero,.two{gap:10px;margin-bottom:10px}
  .metrics{grid-template-columns:repeat(2,1fr);gap:10px;margin-bottom:10px}
  .bgrid{grid-template-columns:repeat(2,minmax(0,1fr))}
  .bgrid.n3{grid-template-columns:repeat(2,minmax(0,1fr))}
  .kv{grid-template-columns:repeat(2,1fr);gap:10px}
  .top h1{font-size:19px}
  .m-val{font-size:21px}
  .ring-num{font-size:28px}
  /* 窄屏让活动名换成两行而不是硬撑宽度：多数日子不再需要横向滚动 */
  td.name{max-width:150px;white-space:normal;word-break:break-word;line-height:1.45}
}
"""


def esc(s) -> str:
    return _html.escape("" if s is None else str(s))


def fmt(v, unit="", d=0) -> str:
    if v is None:
        return "—"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"{v:,.{d}f}{unit}"


def spark(points: list[tuple[str, float]], color="#1D9E75", w=150, h=34) -> str:
    if len(points) < 2:
        return f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}"></svg>'
    vals = [p[1] for p in points]
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    pad = 3
    n = len(points)
    xy = []
    for i, v in enumerate(vals):
        x = pad + i * (w - pad * 2) / (n - 1)
        y = h - pad - (v - lo) / rng * (h - pad * 2)
        xy.append((x, y))
    line = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in xy)
    area = line + f" L {xy[-1][0]:.1f} {h} L {xy[0][0]:.1f} {h} Z"
    return (
        f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" preserveAspectRatio="none">'
        f'<path d="{area}" fill="{color}" opacity="0.10"/>'
        f'<path d="{line}" fill="none" stroke="{color}" stroke-width="1.6" stroke-linejoin="round"/>'
        f'<circle cx="{xy[-1][0]:.1f}" cy="{xy[-1][1]:.1f}" r="2.4" fill="{color}"/>'
        "</svg>"
    )


def donut(score, color, size=104, stroke=9) -> str:
    r = (size - stroke) / 2
    c = 2 * math.pi * r
    pct = 0 if score is None else max(0, min(100, score)) / 100
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}">'
        f'<circle cx="{size/2}" cy="{size/2}" r="{r}" fill="none" stroke="#EDF0EA" stroke-width="{stroke}"/>'
        f'<circle cx="{size/2}" cy="{size/2}" r="{r}" fill="none" stroke="{color}" stroke-width="{stroke}"'
        f' stroke-linecap="round" stroke-dasharray="{c*pct:.1f} {c:.1f}"'
        f' transform="rotate(-90 {size/2} {size/2})"/>'
        "</svg>"
    )


def load_chart(dates, loads, ctl, atl, w=620, h=150) -> str:
    if not loads:
        return '<p class="empty">暂无负荷数据</p>'
    n = len(loads)
    pad_l, pad_b = 8, 22
    hi = max(max(loads), max(ctl) if ctl else 0) or 1
    bw = (w - pad_l * 2) / n
    bars = []
    for i, v in enumerate(loads):
        bh = (v / hi) * (h - pad_b - 6) if hi else 0
        x = pad_l + i * bw
        color = "#1D9E75" if v > 0 else "#EDF0EA"
        bars.append(
            f'<rect x="{x+bw*0.18:.1f}" y="{h-pad_b-bh:.1f}" width="{bw*0.64:.1f}" height="{max(bh,0):.1f}"'
            f' rx="2" fill="{color}" opacity="{0.85 if v>0 else 1}"/>'
        )

    def poly(series, color):
        if not series or len(series) < 2:
            return ""
        pts = []
        for i, v in enumerate(series):
            x = pad_l + i * bw + bw / 2
            y = h - pad_b - (v / hi) * (h - pad_b - 6)
            pts.append(f"{x:.1f} {y:.1f}")
        return f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" stroke-width="1.5" opacity="0.9"/>'

    labels = []
    step = max(1, n // 6)
    for i in range(0, n, step):
        x = pad_l + i * bw + bw / 2
        labels.append(
            f'<text x="{x:.1f}" y="{h-6}" font-size="10" fill="#A5AC9F" text-anchor="middle">{dates[i][5:]}</text>'
        )
    return (
        f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}">'
        + "".join(bars) + poly(ctl, "#3A6EA5") + poly(atl, "#C08A16")
        + "".join(labels) + "</svg>"
    )


def sleep_bar(deep, light, rem, awake, w=360, h=26) -> str:
    segs = [(deep, "#2F4B7C"), (light, "#4E7FB8"), (rem, "#7FB2D9"), (awake, "#D9DED4")]
    total = sum(v for v, _ in segs if v)
    if not total:
        return '<p class="empty">昨晚没有睡眠记录</p>'
    x = 0
    parts = []
    for v, color in segs:
        if not v:
            continue
        wd = v / total * w
        parts.append(f'<rect x="{x:.1f}" y="0" width="{max(wd-1.5,1):.1f}" height="{h}" rx="4" fill="{color}"/>')
        x += wd
    return f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}">' + "".join(parts) + "</svg>"


def metric_card(label, value, unit, delta, delta_good, points, color, extra="", delta_unit=""):
    cls = "flat"
    arrow = ""
    if delta is not None:
        if abs(delta) < 0.05:
            cls = "flat"
        elif delta > 0:
            cls, arrow = ("up", "▲") if delta_good == "up" else ("down", "▲")
        else:
            cls, arrow = ("up", "▼") if delta_good == "down" else ("down", "▼")
    dhtml = ""
    if delta is not None:
        dhtml = f'<div class="m-delta">较 7 天 <b class="{cls}">{arrow} {abs(delta):g}{delta_unit}</b></div>'
    elif extra:
        dhtml = f'<div class="m-delta">{extra}</div>'
    return (
        f'<div class="card"><h3>{label}</h3>'
        f'<div class="m-val">{value}<small>{unit}</small></div>{dhtml}'
        f'<div class="m-spark">{spark(points, color)}</div></div>'
    )



def _chg(v, unit="", good_down=True, digits=2) -> str:
    """体成分变化：只有「好/坏」明确的才上色，体重绝对值本身不下判断。"""
    if v is None:
        return '<span style="color:var(--faint)">样本不足</span>'
    if abs(v) < 10 ** (-digits) / 2:
        return '<b style="color:var(--muted)">持平</b>'
    good = (v < 0) if good_down else (v > 0)
    color = "var(--green)" if good else "var(--red)"
    arrow = "▼" if v < 0 else "▲"
    return f'<b style="color:{color}">{arrow} {abs(v):.{digits}f}{unit}</b>'


def body_block(bd: dict) -> str:
    """身体成分：体重趋势 + 各项体成分 + 与负荷的联合结论。

    小米秤给的是 BIA 估算，绝对误差大但同一个人同一时段的**相对变化**可用，
    所以这里一律并列「最新值」和「变化量」，并且把跨度写出来，避免误读。
    """
    if not bd or not bd.get("latest"):
        return ""
    stale = bd.get("stale_days")
    when = esc(bd.get("latest_date") or "")
    when_txt = f"{when[5:]} 称重" + (f" · {stale} 天前" if stale else " · 今天")

    d7 = bd.get("delta_w")
    d4w = bd.get("delta_4w")
    pct = bd.get("delta_pct_w")
    pct_html = f'<span style="color:var(--faint)">（{pct:+.1f}%）</span>' if pct is not None else ""
    d7_html = (f'7 天均值 {_chg(d7, " kg")}{pct_html}' if d7 is not None
               else '<span style="color:var(--faint)">7 天对比样本不足</span>')
    d4w_html = ""
    if d4w is not None:
        days = bd.get("delta_4w_days") or 28
        label = "较 4 周前" if days <= 32 else f"较 {days} 天前"
        d4w_html = f'<br>{label} {_chg(d4w, " kg")}'

    def cell(label, value, unit="", sub="", digits=1):
        v = "—" if value is None else f"{value:.{digits}f}"
        return (f'<div class="bcell"><b>{v}<small>{unit}</small></b>'
                f'<span>{label}{(" · " + sub) if sub else ""}</span></div>')

    fat_d = bd.get("fat_delta")
    fat_sub = (f'{bd.get("fat_delta_span")} 天 {fat_d:+.1f} pt' if fat_d is not None else "变化样本不足")
    mus_d = bd.get("muscle_delta")
    mus_sub = (f'{bd.get("muscle_delta_span")} 天 {mus_d:+.1f} kg' if mus_d is not None else "变化样本不足")
    wat_d = bd.get("water_delta")
    wat_sub = (f'{bd.get("water_delta_span")} 天 {wat_d:+.1f}%' if wat_d is not None else "")

    cells = "".join([
        cell("体脂率", bd.get("body_fat"), "%", fat_sub),
        cell("脂肪量", bd.get("fat_mass"), "kg", "体重×体脂率"),
        cell("肌肉量", bd.get("muscle"), "kg", mus_sub),
        cell("瘦体重", bd.get("lean_mass"), "kg", "体重−脂肪"),
        cell("水分率", bd.get("water"), "%", wat_sub),
        cell("内脏脂肪", bd.get("visceral"), "级", "≤9 正常" if bd.get("visceral") else "", 0),
        cell("基础代谢", bd.get("bmr"), "kcal", "静息消耗", 0),
        cell("BMI", bd.get("bmi"), "", "小米秤估算", 1),
    ])

    lv = bd.get("level") or "none"
    vcolor = {"good": "var(--green)", "watch": "var(--amber)",
              "warn": "var(--red)"}.get(lv, "var(--ink-2)")
    score = bd.get("score")
    score_html = (f'<div class="bnote" style="margin-top:0">体成分评分 '
                  f'<b style="color:var(--ink)">{score:.0f}</b> / 100（秤自评，仅作参考）</div>'
                  if score else "")

    return f"""
<h2 class="sec">身体成分</h2>
<div class="body-grid">
  <div class="card">
    <h3>体重</h3>
    <div class="bnum">{bd['latest']:.1f}<small>kg</small></div>
    <div class="bdelta">{d7_html}{d4w_html}<br>
      <span style="color:var(--faint)">{when_txt} · 共 {bd.get('count', 0)} 次称重</span></div>
    <div class="m-spark" style="margin-top:12px">{spark(bd.get('series') or [], "#B0704A", h=46)}</div>
  </div>

  <div class="card">
    <h3>体成分明细</h3>
    <div class="bgrid">{cells}</div>
    <div class="bnote">体脂 / 肌肉 / 水分取同一天多次称重的均值；变化量跨度不足 10 天不显示。
      生物电阻抗（BIA）绝对误差 ±3~5 个百分点，看趋势比看单次数值可靠。</div>
  </div>

  <div class="card">
    <h3>体重 × 负荷</h3>
    <div class="sess-why" style="color:{vcolor};font-weight:500">{esc(bd.get('verdict') or '数据不足')}</div>
    {score_html}
    <div class="bnote">同一台秤、晨起空腹、排空后称最可比；训练后立刻称会因脱水低 0.5~1.5 kg。</div>
  </div>
</div>"""


def nutrition_block(nt: dict) -> str:
    """今日营养：目标热量 + 三大营养素 + 2~4 条可执行建议。

    数字全部来自 nutrition.targets()，这里只负责排版——口径的唯一来源是那个模块，
    免得「看板上 1900、教练上下文里 2100」这种自相矛盾。
    """
    if not nt or not nt.get("ok"):
        return ""
    from .nutrition import pct_split

    pp, cp, fp = pct_split(nt)
    bar = (f'<div class="nbar">'
           f'<i style="width:{pp}%;background:#3A6EA5"></i>'
           f'<i style="width:{cp}%;background:#C08A16"></i>'
           f'<i style="width:{fp}%;background:#B0704A"></i></div>')

    def cell(label, grams, kcal, pct, color):
        return (f'<div class="bcell"><b>{grams}<small>g</small></b>'
                f'<span style="color:{color}">{label} {pct}% · {kcal} kcal</span></div>')

    cells = (cell("蛋白", nt["protein_g"], nt["protein_kcal"], pp, "#3A6EA5")
             + cell("碳水", nt["carb_g"], nt["carb_kcal"], cp, "#C08A16")
             + cell("脂肪", nt["fat_g"], nt["fat_kcal"], fp, "#B0704A"))

    gap = nt["gap"]
    gap_txt = (f"缺口 {gap} kcal" if gap > 0 else
               (f"盈余 {abs(gap)} kcal" if gap < 0 else "热量平衡"))
    burn_note = "估算（日消耗记录不足）" if nt.get("estimated") else f"近 {nt['burn_days']} 天日均"
    today_txt = (f" · 今日已消耗 {nt['burn_today']:.0f}" if nt.get("burn_today") else "")

    rate = nt.get("rate")
    cls = {"fast": "warn", "slow": "warn", "noise": "", "ok": "", "unknown": ""}.get(rate, "")
    items = ""
    for i, a in enumerate(nt.get("advice") or []):
        k = cls if i == 0 else ""
        items += f'<li class="{k}">{esc(a)}</li>'

    eat = nt.get("intake") or {}
    eat_txt = ""
    if eat.get("ok"):
        left = nt.get("left_kcal") or 0
        eat_txt = (f'<br><span style="color:#4FD6A6">已吃 {eat["kcal"]} kcal（{len(eat.get("items") or [])} 餐）· '
                   + (f'还可吃 {left} kcal' if left > 0 else f'已超 {abs(left)} kcal') + '</span>')

    return f"""
<h2 class="sec">今日营养</h2>
<div class="body-grid">
  <div class="card">
    <h3>目标摄入 · {esc(nt['goal_label'])}</h3>
    <div class="bnum">{nt['target_kcal']}<small>kcal</small></div>
    <div class="bdelta">消耗基线 {nt['burn_base']} kcal（{esc(burn_note)}）<br>
      {gap_txt}{today_txt}{eat_txt}<br>
      <span style="color:var(--faint)">BMR {nt['bmr'] or '—'} kcal
      {'· 已按下限抬回' if nt.get('floor_hit') else '· 目标高于 BMR'}</span></div>
  </div>

  <div class="card">
    <h3>三大营养素</h3>
    {bar}
    <div class="bgrid n3">{cells}</div>
    <div class="bnote">碳水 {nt['carb_factor']} g/kg（{esc(nt['carb_reason'])}）；
      蛋白 {nt['protein_g'] / nt['weight']:.1f} g/kg；脂肪下限按 0.6 g/kg 与总热量 22% 取大。</div>
  </div>

  <div class="card">
    <h3>今天怎么吃</h3>
    <ul class="nlist">{items}</ul>
    <div class="bnote">只做静态估算，不记录每一餐：目标值按近几天消耗与体重趋势给，
      体重连掉两周再回来调。</div>
  </div>
</div>"""


def plan_card(plan: dict) -> str:
    """佳明教练计划卡片：本周七天课表 + 今日安排。"""
    from .plan import item_line

    if not plan or not plan.get("available"):
        return ""
    ph = plan.get("phase") or {}
    head = (f'<span class="chip">{esc(plan.get("name") or "训练计划")}</span>'
            f'<span class="chip gray" style="font-size:11px">{esc(plan.get("level") or "—")}</span>'
            f'<span class="chip gray" style="font-size:11px">{plan.get("weeks") or "—"} 周</span>'
            f'<span class="chip gray" style="font-size:11px">每周约 {plan.get("per_week") or "—"} 次</span>')
    if ph.get("label"):
        head += f'<span class="chip gray" style="font-size:11px">{esc(ph["label"])} · {esc(ph.get("end") or "")} 前</span>'

    rows = []
    today = (plan.get("today") or {}).get("date")
    for d in plan.get("week") or []:
        is_today = d["date"] == today
        mark = ' style="background:#F1F7EE"' if is_today else ""
        label = "今天" if is_today else d["dow"]
        if d["rest"]:
            body = '<span style="color:#8A8F87">休息</span>'
        else:
            body = "；".join(esc(item_line(x)) for x in d["items"])
        rows.append(
            f'<tr{mark}><td style="white-space:nowrap;color:#6B7268">{esc(d["date"][5:])}'
            f'<span style="margin-left:6px;color:#A8ADA4">{esc(label)}</span></td>'
            f'<td>{body}</td></tr>'
        )

    t = plan.get("today") or {}
    if t.get("rest"):
        today_txt = "教练今天没安排训练，是休息日。"
        if plan.get("next_date"):
            today_txt += f'下一次：{esc(plan["next_date"][5:])} ' + "；".join(
                esc(item_line(x)) for x in plan.get("next_items") or [])
    else:
        today_txt = "教练安排：" + "；".join(esc(item_line(x)) for x in t.get("items") or [])

    return (
        '<div class="card">'
        f'<div class="sess-meta" style="margin-bottom:14px">{head}</div>'
        '<table class="acts" style="margin-top:0">'
        + "".join(rows)
        + "</table>"
        '<p class="sess-why" style="margin-top:14px;font-size:12.5px">'
        f'<b style="font-weight:500">今日</b> · {today_txt}</p>'
        '<p style="font-size:11.5px;color:#A8ADA4;margin:8px 0 0">'
        "上方「今日处方」由本地规则按恢复度算出；这一栏是佳明教练的课表，两者冲突时取更保守的一边。</p>"
        "</div>"
    )


def race_card(d: dt.date) -> str:
    """105K 备赛课表卡片：与执行卡 / 逐日课表同源，优先级高于佳明教练计划。"""
    from . import raceplan

    if not raceplan.available():
        return ""
    info = raceplan.day_info(d)
    if not info:
        return ""
    rows = []
    for r in raceplan.week_rows(d):
        is_today = r["date"] == d.isoformat()
        mark = ' style="background:#F1F7EE"' if is_today else ""
        label = "今天" if is_today else r["dow"]
        if r["kind"] == "rest":
            body = '<span style="color:#8A8F87">休</span>'
        else:
            body = esc(r["main"]) + (f' · {esc(r["sub"])}' if r["sub"] else "")
        rows.append(
            f'<tr{mark}><td style="white-space:nowrap;color:#6B7268">{esc(r["date"][5:])}'
            f'<span style="margin-left:6px;color:#A8ADA4">{esc(label)}</span></td>'
            f'<td>{body}</td></tr>'
        )
    head = (f'<span class="chip">105K 备赛 · W{info["week"]} {esc(info["phase"])}期</span>'
            f'<span class="chip gray" style="font-size:11px">本周 {info["week_km"]} km / +{info["week_vert"]:,} m</span>')
    today_txt = (f'{info["abbr"]}' + (f' · {info["sub"]}' if info["sub"] else "")
                 + (f' · {info["tip"]}' if info["tip"] else ""))
    return (
        '<div class="card">'
        f'<div class="sess-meta" style="margin-bottom:14px">{head}</div>'
        '<table class="acts" style="margin-top:0">'
        + "".join(rows)
        + "</table>"
        f'<p class="sess-why" style="margin-top:14px;font-size:12.5px">'
        f'<b style="font-weight:500">今日</b> · {esc(today_txt)}</p>'
        '<p style="font-size:11.5px;color:#A8ADA4;margin:8px 0 0">'
        "与下方佳明教练课表冲突时，以这一栏为准（数据与执行卡 plan-105k-card 同源）。</p>"
        "</div>"
    )


def render(res: dict, store, out_path: Path, sync_at: str | None = None) -> Path:
    from .plan import attach

    attach(res)  # 挂上本地计划快照；没有就静默跳过这一栏
    d = dt.date.fromisoformat(res["date"])
    t = res["today"]
    r = res["readiness"]
    L = res["load"]
    sl = res["sleep"]
    b = brief(res)
    _pc = plan_card(res.get("plan"))
    _rc = race_card(d)
    race_sec = ('<h2 class="sec">105K 备赛 · 本周课表</h2>' + _rc) if _rc else ""
    if _rc:
        plan_sec = ('<h2 class="sec">RunnerCoach 教练 · 本周课表（参考）</h2>' + _pc) if _pc else ""
    else:
        plan_sec = ('<h2 class="sec">RunnerCoach 教练 · 本周课表</h2>' + _pc) if _pc else ""

    tot_sleep = sum(num(sl.get(k), 0) or 0 for k in ("deep_min", "light_min", "rem_min", "awake_min"))
    if tot_sleep:
        sleep_ratio = (
            f"深睡占比 {(sl['deep_min'] or 0) / tot_sleep * 100:.1f}% · "
            f"REM 占比 {(sl['rem_min'] or 0) / tot_sleep * 100:.1f}% · "
            f"睡眠效率 {(1 - (sl['awake_min'] or 0) / tot_sleep) * 100:.0f}%"
        )
    else:
        sleep_ratio = ""

    hist = res["history"]
    dates = [x["date"] for x in hist]
    loads = [L["loads"].get(x, 0) for x in dates]
    ctl = L["ctl_series"][-len(dates):] if L["ctl_series"] else []
    atl = L["atl_series"][-len(dates):] if L["atl_series"] else []

    tr = res["trends"]
    sleep_label = "睡眠" + (f" · 评分 {fmt(sl['score'])}" if sl["score"] else "")
    cards = [
        metric_card(sleep_label, fmt(sl["total_h"], d=1), "小时", tr["sleep"]["delta"], "up",
                    res["series"]["sleep_h"], "#3A6EA5", delta_unit=" 分"),
        metric_card("HRV", fmt(t.get("hrv_night")), "ms", tr["hrv"]["delta"], "up",
                    res["series"]["hrv"], "#1D9E75"),
        metric_card("静息心率", fmt(t.get("resting_hr")), "bpm", tr["rhr"]["delta"], "down",
                    res["series"]["rhr"], "#C2453F"),
        metric_card("身体电量", fmt(t.get("bb_high")), "", tr["bb"]["delta"], "up",
                    res["series"]["bb"], "#7A6AC4"),
        metric_card("压力", fmt(t.get("avg_stress")), "", tr["stress"]["delta"], "down",
                    [(x["date"], num(x.get("avg_stress")) or 0) for x in hist if num(x.get("avg_stress")) is not None],
                    "#C08A16"),
        metric_card("步数", fmt(t.get("steps")), "", None, "up",
                    res["series"]["steps"], "#6B7268",
                    extra=f"目标 {fmt(t.get('step_goal'))}"),
    ]

    # 体重 / 体成分单独成一块（见 body_block）：塞进 6 列指标格里放不下 8 个字段
    bd = res.get("body") or {}
    body_sec = body_block(bd)
    nutri_sec = nutrition_block(res.get("nutrition") or {})

    rhr_base = baseline(hist, "resting_hr")
    hrv_base = baseline(hist, "hrv_night")
    sl_base = baseline(hist, "sleep_total_min")

    def _cmp(cur, base, unit="", invert=False, digits=0):
        if cur is None or base is None:
            return "—", "基线样本不足"
        d = cur - base
        if abs(d) < 0.5:
            return '<b style="color:#7A8276">持平</b>', "较 28 天基线"
        sign = "+" if d >= 0 else "−"
        good = (d <= 0) if invert else (d >= 0)
        color = "#1D9E75" if good else "#C2453F"
        return f'<b style="color:{color}">{sign}{abs(d):.{digits}f}{unit}</b>', "较 28 天基线"

    hrv_v, hrv_l = _cmp(num(t.get("hrv_night")), hrv_base, "")
    rhr_v, rhr_l = _cmp(num(t.get("resting_hr")), rhr_base, "", invert=True)
    slp_v, slp_l = _cmp(num(t.get("sleep_total_min")), sl_base, "分", digits=0)
    base_rows = (
        f'<div class="kv" style="margin-top:16px">'
        f'<div><span>HRV</span>{hrv_v}<span style="font-size:10.5px">{hrv_l}</span></div>'
        f'<div><span>静息心率</span>{rhr_v}<span style="font-size:10.5px">{rhr_l}</span></div>'
        f'<div><span>睡眠</span>{slp_v}<span style="font-size:10.5px">{slp_l}</span></div>'
        f"</div>"
    )

    part_html = "".join(
        f'<div class="part"><span>{esc(p["name"])}</span>'
        f'<div class="bar"><i style="width:{p["score"]}%;background:{r["color"]}"></i></div>'
        f'<span>{p["score"]}</span></div>'
        for p in r["parts"]
    )

    flags_html = "".join(
        f'<li><i class="fdot f-{f["level"]}"></i><span>{esc(f["text"])}</span></li>'
        for f in res["flags"]
    )

    sess = b["session"]
    note = store.latest_note()
    if note:
        note_html = f'<div class="note">{esc(note["text"])}</div><div class="note-meta">由 {esc(note["source"])} 生成 · {esc(note["created_at"])}</div>'
    else:
        note_html = ('<div class="placeholder"><b>还没有教练点评</b>'
                     '<span>在 WorkBuddy 里说「写今天的教练点评」，我会读完上面的指标，把结论写回这里。</span></div>')

    rows = []
    for a in res["activities"][:10]:
        dur = (num(a.get("duration_s")) or 0) / 60
        dist = (num(a.get("distance_m")) or 0) / 1000
        pace = num(a.get("pace_s_km"))
        pace_s = f"{int(pace)//60}'{int(pace)%60:02d}\"" if pace else "—"
        # data-aid 给 App 的 WebView 用：点这一行就能出该活动的图。
        # 桌面/静态页面里它只是个无害的自定义属性，不绑事件。
        aid = a.get("activity_id")
        attr = f' data-aid="{esc(str(aid))}"' if aid else ""
        rows.append(
            f"<tr{attr}>"
            f'<td>{esc((a.get("date") or "")[5:])}</td>'
            f'<td><span class="tag">{esc(TYPE_CN.get(a.get("type"), a.get("type") or "—"))}</span></td>'
            f'<td class="name">{esc(a.get("name") or "—")}</td>'
            f'<td>{dur:.0f} 分</td>'
            f'<td>{dist:.2f} km</td>'
            f'<td>{pace_s}</td>'
            f'<td>{fmt(a.get("avg_hr"))}</td>'
            f'<td>{fmt(a.get("load"))}</td>'
            "</tr>"
        )
    table = (
        '<div class="tbl-scroll">'
        "<table><thead><tr><th>日期</th><th>类型</th><th>名称</th><th>时长</th>"
        "<th>距离</th><th>配速</th><th>均心率</th><th>负荷</th></tr></thead><tbody>"
        + ("".join(rows) or '<tr><td colspan="8" class="empty">近 5 周没有活动记录</td></tr>')
        + "</tbody></table></div>"
    )

    acwr_color = "#1D9E75"
    if L["acwr"] and L["acwr"] > 1.4:
        acwr_color = "#C2453F"
    elif L["acwr"] and L["acwr"] < 0.8:
        acwr_color = "#C08A16"

    # 睡眠债口径注脚：有小睡抵扣时提示一句，免得数字对不上手机上的缺口
    nap_note = ""
    if (sl.get("debt") or {}).get("nap_min"):
        nap_note = "（含小睡）"

    html_doc = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RunnerCoach 每日看板 · {esc(d)}</title>
<style>{CSS}</style></head><body><div class="wrap">

<div class="top">
  <div>
    <h1>每日看板</h1>
    <div class="sub">{esc(d)} {WEEK_CN[d.weekday()]} · 睡眠/恢复/负荷综合分析</div>
  </div>
  <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
    <a class="badge" href="ai" style="text-decoration:none">AI 教练 · 今日建议</a>
    <div class="badge"><i class="dot"></i>Garmin 国区 · 上次同步 {esc(sync_at or store.get_meta('last_sync') or '尚未同步')}</div>
  </div>
</div>

<div class="hero">
  <div class="card">
    <h3>恢复度</h3>
    <div class="ring-wrap">
      <div style="position:relative">{donut(r['score'], r['color'])}</div>
      <div>
        <div class="ring-num" style="color:{r['color']}">{r['score'] if r['score'] is not None else '—'}</div>
        <div class="ring-lv" style="color:{r['color']}">{esc(r['level'])}</div>
        <div class="m-delta" style="margin-top:6px">综合睡眠 / HRV / 静息心率 / 电量</div>
      </div>
    </div>
    <div class="parts">{part_html}</div>
  </div>

  <div class="card">
    <h3>今日处方</h3>
    <div class="sess-type">{esc(sess['type'])}</div>
    <div class="sess-meta">
      <span class="chip">{esc(sess['duration'])}</span>
      <span class="chip gray">{esc(sess['intensity'])}</span>
    </div>
    <div class="sess-why">{esc(sess['why'])}</div>
    <div class="sess-meta" style="margin-top:16px;margin-bottom:0">
      <span class="chip gray" style="font-size:11px">恢复度 {r['score'] if r['score'] is not None else '—'}</span>
      <span class="chip gray" style="font-size:11px">ACWR {L['acwr'] or '—'}</span>
      <span class="chip gray" style="font-size:11px">睡眠债 {fmt(sl['debt_h'], ' h', 1)}</span>
      <span class="chip gray" style="font-size:11px">TSB {L['tsb']}</span>
    </div>
  </div>

  <div class="card">
    <h3>关键提示</h3>
    <ul class="flags">{flags_html}</ul>
    {base_rows}
  </div>
</div>

<div class="metrics">{''.join(cards)}</div>

{body_sec}
{nutri_sec}
{race_sec}
{plan_sec}
<h2 class="sec">训练负荷</h2>
<div class="two">
  <div class="card">
    <h3>近 {len(dates)} 天负荷</h3>
    {load_chart(dates, loads, ctl, atl)}
    <div class="legend">
      <span><i style="background:#1D9E75"></i>每日负荷</span>
      <span><i style="background:#3A6EA5"></i>CTL 长期体能</span>
      <span><i style="background:#C08A16"></i>ATL 短期疲劳</span>
    </div>
    <div class="kv">
      <div><b style="color:{acwr_color}">{L['acwr'] if L['acwr'] else '—'}</b><span>ACWR · {esc(L['acwr_state'])}</span></div>
      <div><b>{L['ctl']}</b><span>CTL 体能</span></div>
      <div><b>{L['tsb']}</b><span>TSB 状态</span></div>
    </div>
  </div>

  <div class="card">
    <h3>昨晚睡眠结构</h3>
    {sleep_bar(sl['deep_min'], sl['light_min'], sl['rem_min'], sl['awake_min'])}
    <div class="legend">
      <span><i style="background:#2F4B7C"></i>深睡 {fmt(sl['deep_min'])} 分</span>
      <span><i style="background:#4E7FB8"></i>浅睡 {fmt(sl['light_min'])} 分</span>
      <span><i style="background:#7FB2D9"></i>REM {fmt(sl['rem_min'])} 分</span>
      <span><i style="background:#D9DED4"></i>清醒 {fmt(sl['awake_min'])} 分</span>
    </div>
    <div class="kv" style="grid-template-columns:1fr 1fr 1fr">
      <div><b>{fmt(sl['total_h'], ' h', 1)}</b><span>总睡眠</span></div>
      <div><b>{fmt(sl['debt_h'], ' h', 1)}</b><span>近 7 天睡眠债{nap_note}</span></div>
      <div><b>{fmt(sl['nap_min'])} 分</b><span>昨日小睡</span></div>
    </div>
    <p class="sess-why" style="margin-top:12px;font-size:12.5px">
      {esc((sl['start'] or '—')[11:])} 入睡 · {esc((sl['end'] or '—')[11:])} 起床
      {'· 睡眠评分 ' + fmt(sl['score']) if sl['score'] else ''}
      {f"· 佳明动态需求 {fmt(round(sl['debt']['need_avg_min'] / 60, 1), ' h', 1)}/晚" if (sl.get('debt') or {}).get('need_avg_min') else ''}
    </p>
    <p class="m-delta" style="margin-top:6px">{sleep_ratio}</p>
  </div>
</div>

<h2 class="sec">教练点评</h2>
<div class="card">{note_html}</div>

<h2 class="sec">最近活动</h2>
<div class="card">{table}</div>

<div class="foot">
恢复度 = 睡眠 35% + HRV 30% + 静息心率 20% + 身体电量 15%。睡眠与身体电量直接用 Garmin 原始 0–100 分；
HRV 与静息心率以 28 天中位数为基线，持平或更好记满分，HRV 每低 30% 扣满、静息心率每高 6 bpm 扣满。<br>
ACWR = 近 7 天负荷 ÷（近 28 天负荷 ÷ 4）；CTL/ATL 为 42 天与 7 天指数移动平均，TSB = CTL − ATL。<br>
数据来源：Garmin Connect 国区（connect.garmin.cn /gc-api）。本页为本地生成，不上传任何数据。
</div>
</div></body></html>"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_doc, encoding="utf-8")
    return out_path
