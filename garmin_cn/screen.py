# -*- coding: utf-8 -*-
"""首屏 = 一页大屏 的共用版式层（0.6.7）。

设计依据：飞书《主页面设计说明：从「卡片墙」到「身体数据大屏 + 活动概述」》。
文档把首页的毛病写成了一句结论：**主页不是展示全部数据的地方，是回答
「今天怎么样、今天怎么练」的地方**。所以这里不再有四个等高的 `sec()`，
而是同一份 DOM 的两种布局：

  * 竖屏 390：结论条 44 → 恢复环 ≈176 → 负荷带 60 → 身体 2×2 ≈172
               → 计划一条 72 → 最近 7 天 ≈300；
  * 宽屏 / 横屏：同一个 HTML 用 `@media (min-width:900px)` 重排成
               12 栅格 2 行 5 面板（恢复 4 / 负荷 4 / 身体 4；7 天 8 / 计划 4），
               Python 侧零改动、零新增请求。首屏与「一页大屏」就此合一，
               `report.py` 只保留按日期导出/回看，不再是首页的第二套视觉。

三条原则也写进了 CSS：

1. **一屏一个主角**：L1 结论 + 26px 环内数 / L2 19px 关键值 /
   L3 11.5–12.5px 解释 / L4 11px 元信息，没有 10px 以下承载信息的字；
2. **数字先行**：环、条、柱只服务当前那个结论；
3. **窗口写在旁边**：`近 7 天` / `42 天均线` / `7 日变化` / `最近 7 天`
   必须跟着数值出现，同屏不允许两个同数不同义。

克制：恢复环**常态不用辉光**，只有分数 <50 或 >90（今天状态异常）才亮；
全局背景的渐进/双辉光仍在 `ui.CSS` 的 `body::before`（0.6.6 品牌体系），
面板只留一层很轻的渐进与顶沿高光，不再「卡里套卡」。出图页 body 保持
纯色（`trim_bg` 靠四边纯色裁 padding），本文件只服务 App 内页面，不改出图。

颜色一律 `var(--x, fallback)`：App 里复用 `ui.py` 的 :root，单独渲染时
自动落到同色值，不另立色板。
"""
from __future__ import annotations

import math
from html import escape as _e

# CSS 花括号很多：这里用普通字符串 + 占位符替换，绝不用 f-string 拼整段。
_BASE = """
/* ---- 首屏专用作用域：只影响今日 tab ------------------------------ */
.rc-page .page-head.rc-compact{padding:10px 0 7px;margin-bottom:2px}
.rc-page .page-head.rc-compact h1{font-size:19px;margin:4px 0 0}
.rc-page .page-head.rc-compact .meta{margin-top:6px}
.rc-screen{display:flex;flex-direction:column;gap:10px}

/* 四级层级：L1 26 / L2 19 / L3 11.5–12.5 / L4 11，窗口标签必须贴着数字 */
.rc-eyebrow{font-size:11px;font-weight:700;letter-spacing:1.5px;
  color:var(--muted,#7C8FA8)}
.rc-win{font-size:11px;color:var(--faint,#7689A2);
  font-variant-numeric:tabular-nums;white-space:nowrap}
.rc-num{font-variant-numeric:tabular-nums;font-weight:650;
  color:var(--ink,#E8F0FB)}

/* 面板：一层渐进 + 一道顶沿高光，不再有三层背景 */
.rc-panel{position:relative;overflow:hidden;border-radius:16px;padding:11px 12px;
  background:linear-gradient(168deg,rgba(20,31,48,.92),rgba(11,18,29,.96));
  border:1px solid var(--line,rgba(96,165,250,.16))}
.rc-panel::before{content:"";position:absolute;left:0;right:0;top:0;height:1px;
  background:linear-gradient(90deg,transparent,rgba(96,165,250,.50),transparent);
  pointer-events:none}
.rc-panel>*{position:relative;z-index:1}
.rc-panel-hd{display:flex;align-items:baseline;justify-content:space-between;
  gap:8px;margin:0 0 8px}

/* L1 结论条 44：把原先藏在 hero 右侧的答案提到第一行 */
.rc-verdict{display:flex;align-items:center;justify-content:space-between;gap:10px;
  min-height:38px;padding:6px 12px;border-radius:14px;
  background:linear-gradient(100deg,rgba(59,130,246,.24),rgba(34,211,238,.07) 62%,transparent);
  border:1px solid rgba(96,165,250,.30);position:relative;overflow:hidden}
.rc-verdict::before{content:"";position:absolute;left:0;right:0;top:0;height:1px;
  background:linear-gradient(90deg,rgba(96,165,250,.7),transparent);pointer-events:none}
.rc-verdict .v{font-size:18px;font-weight:700;letter-spacing:-.3px;
  color:var(--brand-ink,#8CC2FF);min-width:0}
.rc-verdict .m{font-size:11.5px;color:var(--faint,#7689A2);white-space:nowrap;
  font-variant-numeric:tabular-nums}

/* 恢复 hero ≈176：环 108（r45 / 描边 9）+ 3 条分解 + 1 句可执行建议 */
.rc-hero{display:flex;gap:14px;align-items:stretch}
.rc-ring{flex:0 0 108px;width:108px;height:108px;display:block}
.rc-ring.alert{filter:drop-shadow(0 0 14px rgba(242,85,90,.55))}
.rc-hero-side{flex:1;min-width:0;display:flex;flex-direction:column;
  justify-content:space-between;gap:8px}
.rc-parts{display:flex;flex-direction:column;gap:5px}
.rc-part{display:grid;grid-template-columns:60px 1fr 26px;gap:6px;align-items:center}
.rc-part .n{font-size:11.5px;color:var(--muted,#7C8FA8);white-space:nowrap}
.rc-part .b{height:3px;border-radius:999px;background:rgba(255,255,255,.09);
  overflow:hidden;min-width:0}
.rc-part .b i{display:block;height:100%;border-radius:999px;
  background:linear-gradient(90deg,var(--brand,#3B82F6),var(--cyan,#22D3EE))}
.rc-part .v{font-size:11.5px;text-align:right;color:var(--ink2,#B7C7DD);
  font-variant-numeric:tabular-nums}
.rc-advice{font-size:12.5px;color:var(--ink2,#B7C7DD);line-height:1.5}
.rc-advice b{color:var(--ink,#E8F0FB);font-weight:650}

/* 负荷带 60：三个数并列，**不是三张卡** */
.rc-load{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));
  min-height:52px;padding:4px 0;
  border-top:1px solid var(--line2,rgba(96,165,250,.09));
  border-bottom:1px solid var(--line2,rgba(96,165,250,.09))}
.rc-load .item{padding:0 12px;border-right:1px solid var(--line2,rgba(96,165,250,.09));
  min-width:0}
.rc-load .item:first-child{padding-left:2px}
.rc-load .item:last-child{border-right:0}
.rc-load .k{font-size:11px;color:var(--muted,#7C8FA8)}
.rc-load .v{font-size:19px;margin-top:1px;line-height:1.25}
.rc-load .w{font-size:11px;color:var(--faint,#7689A2)}

/* 身体面板 172：2×2，每格 数值 17 + 变化 + 22 高 sparkline */
.rc-body{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.rc-cell{border-radius:12px;padding:7px 10px 6px;min-width:0;
  background:rgba(9,14,22,.42);border:1px solid var(--line2,rgba(96,165,250,.09))}
.rc-cell .k{font-size:11px;color:var(--muted,#7C8FA8)}
.rc-cell .v{font-size:17px;margin-top:2px;line-height:1.25}
.rc-cell .v small{font-size:11.5px;font-weight:500;margin-left:3px;
  color:var(--muted,#7C8FA8)}
/* 刻意不写 nowrap/ellipsis：体重「7 日 -0.3 kg」被截成「较…」是噪音 */
.rc-cell .sub{font-size:11px;color:var(--faint,#7689A2);line-height:1.45;
  margin-top:2px}
.rc-cell .spark{display:block;height:20px;margin-top:2px}

/* 计划一条 72：只放「下一次」 */
.rc-plan{display:flex;align-items:center;gap:12px;min-height:60px}
.rc-plan .when{flex:0 0 58px;font-size:11.5px;color:var(--muted,#7C8FA8);
  line-height:1.4}
.rc-plan .when b{display:block;font-size:17px;color:var(--brand-ink,#8CC2FF);
  font-variant-numeric:tabular-nums}
.rc-plan .what{min-width:0;flex:1}
.rc-plan .t{font-size:17px;font-weight:650;line-height:1.35}
.rc-plan .d{font-size:11.5px;color:var(--muted,#7C8FA8);margin-top:2px;
  line-height:1.5}
.rc-plan .go{flex:0 0 auto;font-size:11.5px;color:var(--brand-ink,#8CC2FF);
  display:inline-flex;align-items:center;min-height:44px;padding:0 2px}

/* 最近 7 天 ≈300：一个大数 + 7 根柱 + 3 条样本 */
.rc-week-hd{display:flex;align-items:flex-end;justify-content:space-between;gap:10px}
.rc-week-hd .big-num{font-size:26px;line-height:1.15}
.rc-week-hd .big-num small{font-size:12.5px;font-weight:500;margin-left:4px;
  color:var(--muted,#7C8FA8)}
.rc-week-hd .side{font-size:11.5px;color:var(--faint,#7689A2);text-align:right;
  line-height:1.6}
.rc-days{display:flex;gap:7px;align-items:flex-end;margin-top:10px;height:106px}
.rc-day{flex:1;min-width:32px;height:100%;min-height:44px;
  display:flex;flex-direction:column;justify-content:flex-end;align-items:center;
  gap:4px;text-decoration:none;color:var(--faint,#7689A2)}
.rc-day .bar{width:100%;max-width:44px;border-radius:6px 6px 3px 3px;
  background:linear-gradient(180deg,rgba(59,130,246,.58),rgba(34,211,238,.20));
  min-height:0}
/* 零值只留一个基线点，绝不画 6px 假柱（沿用 history._month_bars 的修法） */
.rc-day.zero .bar{height:0!important}
.rc-day .dot{width:4px;height:4px;border-radius:50%;
  background:var(--faint,#7689A2);opacity:.6;margin-bottom:2px}
.rc-day.on .bar{background:linear-gradient(180deg,#3B82F6,#22D3EE);
  box-shadow:0 0 18px -6px rgba(59,130,246,.9)}
.rc-day .lbl{font-size:11px;line-height:1.2}
.rc-day.on .lbl{color:var(--brand-ink,#8CC2FF)}
.rc-samples{margin-top:8px;border-top:1px solid var(--line2,rgba(96,165,250,.09))}
/* CTL/ATL 双线只在宽屏出现：竖屏首屏不占高度，大屏负荷面板才有图可读 */
.rc-load-chart{display:none}
.rc-sample{display:flex;align-items:center;gap:10px;min-height:44px;
  border-bottom:1px solid var(--line2,rgba(96,165,250,.09));
  text-decoration:none;color:inherit}
.rc-sample:last-child{border-bottom:0}
.rc-sample .t{font-size:12.5px;font-weight:600;min-width:0;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.rc-sample .d{font-size:11.5px;color:var(--muted,#7C8FA8);margin-left:auto;
  white-space:nowrap;font-variant-numeric:tabular-nums}

/* 宽屏铺开完全交给 @media，不再有横屏按钮：倾斜手机时系统已有
   旋转入口，再来一个 App 按钮既重复又会触发额外的系统旋转提示。 */
.rc-foot-line{margin-top:2px;font-size:11px;color:var(--faint,#7689A2);
  text-align:center;letter-spacing:.2px}
@media(max-width:380px){
  .rc-hero{gap:10px}
  .rc-ring{flex-basis:96px;width:96px;height:96px}
  .rc-part{grid-template-columns:52px 1fr 24px}
  .rc-load .item{padding:0 8px}
}
"""

# 大屏栅格：只由 @media 触发生成一份。{p} 前缀占位符保留是为了以后要加
# 别的限定时不用重写整段规则，但目前有且只有一处展开。
_BIG = """
{p}.rc-page{max-width:none;padding:0 16px 0}
{p}.rc-page .page-head{padding:8px 0 8px;margin-bottom:6px}
{p}.rc-page .page-head h1{font-size:19px;margin:3px 0 0}
{p}.rc-page .page-head .meta{margin-top:6px}
/* 1280×800 要把页脚也收进一屏：这里只压页脚留白，不动面板高度 */
{p}.rc-page .foot{margin-top:8px;padding-top:6px}
{p}.rc-screen{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));
  grid-template-areas:
    "v v v v v v v v v v v v"
    "h h h h l l l l b b b b"
    "w w w w w w w w p p p p";
  grid-template-rows:38px minmax(240px,1fr) minmax(312px,1fr);
  gap:12px;height:calc(100vh - 160px);min-height:560px}
{p}.rc-verdict{grid-area:v}
{p}.rc-hero-panel{grid-area:h}
{p}.rc-load{grid-area:l}
{p}.rc-body-panel{grid-area:b}
{p}.rc-week-panel{grid-area:w}
{p}.rc-plan-panel{grid-area:p}
/* 面板内部可滚，页面本身不滚；1280×800 无页面滚动、无面板截断 */
{p}.rc-panel{overflow:auto}
/* 负荷带在竖屏是一条裸带，大屏里补上面板外壳；三个数仍并列，只是
   CTL 是这块的唯一主数，另两个降为 L2。 */
{p}.rc-load{width:100%;align-items:start;align-content:center;overflow:auto;
  border:1px solid var(--line,rgba(96,165,250,.16));border-radius:16px;
  padding:10px 12px;
  background:linear-gradient(168deg,rgba(20,31,48,.92),rgba(11,18,29,.96))}
{p}.rc-load .item{padding:0 10px}
{p}.rc-load-chart{display:block;grid-column:1/-1;margin-top:12px}
{p}.rc-load-chart svg{display:block;width:100%;height:96px}
{p}.rc-load-chart .lg{display:flex;gap:14px;font-size:11px;
  color:var(--faint,#7689A2);margin-top:4px}
{p}.rc-load-chart .lg i{display:inline-block;width:10px;height:2px;
  margin-right:5px;vertical-align:middle;border-radius:2px}
{p}.rc-week .rc-days{height:80px}
"""

SCREEN_CSS = (
    _BASE
    + "@media(min-width:900px){" + _BIG.replace("{p}", "") + "}\n"
)

RING_R = 45
RING_C = 2 * math.pi * RING_R


def ring_svg(score, *, alert: bool = False) -> str:
    """恢复度环 108×108（r45 / 描边 9），渐变只在环上。

    `alert` 只在分数 <50 或 >90 时为 True：常态下辉光不亮，异常时才亮，
    辉光才有意义（文档第三节的刻意取舍）。SVG id 固定，保证同输入输出
    逐字节一致，出图/缓存/回归都不会因为随机 id 抖动。
    """
    try:
        pct = max(0.0, min(100.0, float(score)))
    except (TypeError, ValueError):
        pct = 0.0
    dash = RING_C * pct / 100.0
    txt = "—" if score is None else str(int(pct))
    cls = "rc-ring alert" if alert else "rc-ring"
    return (
        f'<svg class="{cls}" viewBox="0 0 108 108" width="108" height="108" '
        'xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-label="恢复度 {txt} 分">'
        '<defs><linearGradient id="rc-ring" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#22D3EE"/>'
        '<stop offset="1" stop-color="#3B82F6"/></linearGradient></defs>'
        '<circle cx="54" cy="54" r="45" fill="none" '
        'stroke="rgba(96,165,250,.14)" stroke-width="9"/>'
        f'<circle cx="54" cy="54" r="45" fill="none" stroke="url(#rc-ring)" '
        f'stroke-width="9" stroke-linecap="round" '
        f'stroke-dasharray="{dash:.2f} {RING_C:.2f}" '
        'transform="rotate(-90 54 54)"/>'
        f'<text x="54" y="59" text-anchor="middle" font-size="26" '
        f'font-weight="700" fill="#E8F0FB">{txt}</text>'
        '<text x="54" y="75" text-anchor="middle" font-size="11" '
        'fill="#7C8FA8">恢复度</text></svg>')


def part_rows(parts: list[dict], n: int = 3) -> str:
    """恢复度分解条：只要前 3 条，卡底那条「总进度」已删除。

    原来 2×2 四个分项 + 一条总进度，是同一个信息说两遍；现在环本身就是
    总进度，剩下三条只解释「这个分是怎么来的」。
    """
    out = []
    for p in (parts or [])[:n]:
        v = p.get("score")
        pct = max(0, min(100, int(v or 0)))
        out.append(
            '<div class="rc-part">'
            f'<span class="n">{_e(str(p.get("name") or ""))}</span>'
            f'<span class="b"><i style="width:{pct}%"></i></span>'
            f'<span class="v">{_e(str(v if v is not None else "—"))}</span>'
            '</div>')
    return "".join(out) or '<div class="rc-advice">还没有恢复度分解数据</div>'


def body_cell(label: str, value, unit: str, sub: str, spark: str) -> str:
    """身体面板一格：数值 17px + 变化 + 22px sparkline，说明不截断。"""
    if value in (None, "", "—"):
        v = '<div class="v">—</div>'
    else:
        u = f'<small>{_e(unit)}</small>' if unit else ""
        v = f'<div class="v">{_e(str(value))}{u}</div>'
    return (f'<div class="rc-cell"><div class="k">{_e(label)}</div>{v}'
            f'<div class="sub">{_e(sub)}</div>{spark}</div>')


def load_chart(ctl, atl, *, w: int = 360, h: int = 96) -> str:
    """大屏负荷面板的 CTL / ATL 双线，内联 SVG、无图表库、零请求。

    `path` 由 Python 拼字符串；两条线共用同一量程，才看得出「疲劳追上
    体能」的关系。窗口标签写进图例，不靠图本身猜。
    """
    a = [float(v) for v in (ctl or []) if isinstance(v, (int, float))]
    b = [float(v) for v in (atl or []) if isinstance(v, (int, float))]
    if len(a) < 3 and len(b) < 3:
        return '<div class="rc-win">负荷曲线样本不足</div>'
    vals = a + b
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0

    def poly(seq: list[float]) -> str:
        n = max(len(seq) - 1, 1)
        return " ".join(
            f"{4 + (w - 8) * i / n:.1f},"
            f"{h - 6 - (h - 14) * (v - lo) / span:.1f}"
            for i, v in enumerate(seq))

    return (
        f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" '
        'xmlns="http://www.w3.org/2000/svg" role="img" '
        'aria-label="CTL 与 ATL 负荷曲线">'
        f'<line x1="0" y1="{h - 6}" x2="{w}" y2="{h - 6}" '
        'stroke="rgba(96,165,250,.16)" stroke-width="1"/>'
        f'<polyline points="{poly(a)}" fill="none" stroke="#5EA0FF" '
        'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
        f'<polyline points="{poly(b)}" fill="none" stroke="#22D3EE" '
        'stroke-width="2" stroke-linejoin="round" stroke-linecap="round" '
        'opacity=".75"/>'
        '</svg><div class="lg">'
        '<span><i style="background:#5EA0FF"></i>CTL · 42 天指数均线</span>'
        '<span><i style="background:#22D3EE"></i>ATL · 近 7 天疲劳</span>'
        '</div>')


def week_bars(pairs: list[tuple[str, float]], hot: str,
              *, href: str = "/view?date={d}&from=today") -> str:
    """最近 7 天：每天一柱、当天高亮、点击进当天详情。

    柱宽 ≥32px、柱间 7px（CSS 里 `min-width:32px` / `gap:7px`），整根柱
    都是 ≥44px 的可点区域。零值只画一个基线点 —— 没跑的日子和跑了 0.4km
    的日子绝不能长得一样（沿用 `history._month_bars` 的修法）。
    """
    vals = [float(v or 0) for _d, v in (pairs or [])]
    top = max(vals) or 1.0
    out = []
    for d, v in (pairs or []):
        km = float(v or 0)
        zero = km <= 0
        h = 0 if zero else 6 + 88 * (km / top)
        cls = "rc-day zero" if zero else "rc-day"
        if str(d) == str(hot):
            cls += " on"
        link = _e(href.format(d=_e(str(d), quote=True)), quote=True)
        dot = '<span class="dot"></span>' if zero else ""
        lbl = str(d)[5:] or str(d)
        out.append(
            f'<a class="{cls}" href="{link}" data-nopush '
            f'aria-label="{_e(str(d))} {km:.1f} 公里">'
            f'{dot}<span class="bar" style="height:{h:.0f}px"></span>'
            f'<span class="lbl">{_e(lbl)}</span></a>')
    return "".join(out)
