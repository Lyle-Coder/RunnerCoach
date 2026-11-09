# -*- coding: utf-8 -*-
"""「历史」tab 的两个页面：按日期看板 + 历史活动列表。

  /view?date=YYYY-MM-DD   按需生成该日看板，注入统一移动端适配与日期导航
  /history                 历史活动列表（区间 / 类型 / 分页 / 点一行出活动图）

页面外壳、色板、卡片和点击转发全部走 `ui.py`，和其它 tab 保持一致。
全程只读本地库、不联网；`data-aid` 由 ui.BRIDGE_JS 转给原生出图。
"""
from __future__ import annotations

import datetime as dt
import html
from pathlib import Path
from urllib.parse import urlencode

from . import ui

PAGE = 60          # 一页活动条数


def _esc(v) -> str:
    return html.escape(str(v if v is not None else ""), quote=True)


def _one(qs: dict, key: str, default: str = "") -> str:
    vals = qs.get(key) or [default]
    return str(vals[0] if vals and vals[0] is not None else default).strip()


def _date(qs: dict, key: str, default: str = "") -> str:
    s = _one(qs, key, default)
    try:
        dt.date.fromisoformat(s)
        return s
    except (TypeError, ValueError):
        return default


def _shift(day: str, n: int) -> str:
    try:
        return (dt.date.fromisoformat(day) + dt.timedelta(days=n)).isoformat()
    except (TypeError, ValueError):
        return day


def _q(**kw) -> str:
    return urlencode({k: v for k, v in kw.items() if v not in (None, "")})


def coverage() -> dict:
    """兼容入口：真正的实现在 ui.coverage，所有 tab 共用一份。"""
    return ui.coverage()


def nav_html(day: str, cov: dict, from_: str = "history") -> str:
    """兼容入口：导航条由 ui 统一渲染。"""
    return ui.nav_html(day, cov, from_)


def index_section(cov: dict) -> str:
    """「历史」页顶部的覆盖情况与按日期查看看板的入口。"""
    if not cov.get("days"):
        return ('<div class="card"><div class="empty">本地库还是空的。'
                '下拉同步一次，或到「我的 → 历史数据」补全更早的区间。</div></div>')
    first, last = cov["first"], cov["last"]
    quick = "".join(
        f'<a class="chip" href="/view?date={_esc(d)}&from=history" data-nopush>{_esc(d[5:])}</a>'
        for d in (cov.get("recent") or [])[:12])
    dates = (f'<form class="filters" method="get" action="/view" data-nopush style="margin-top:12px">'
             f'<input type="hidden" name="from" value="history">'
             f'<label>按日期查看板 <input type="date" name="date" '
             f'min="{_esc(first)}" max="{_esc(last)}" value="{_esc(last)}"></label>'
             f'<button class="btn primary" type="submit">打开</button></form>')
    return (f'<div class="grid three">' +
            ui.stat("日汇总覆盖", f"{cov['days']} 天", f"{first} ~ {last}") +
            ui.stat("活动", f"{cov['activities']} 条", f"最早 {cov['act_first'] or '—'}") +
            ui.stat("按日期看板", "已就绪", "选中日期即可切换") +
            '</div>' + f'<div class="card" style="margin-top:10px">{dates}'
            f'<div class="tags">{quick}</div></div>')


def _message(title: str, text: str, cov: dict) -> str:
    links = '<div class="pager">'
    if cov.get("last"):
        links += (f'<a class="btn" href="/view?date={cov["last"]}&from=history">'
                  '看最新一天的看板</a>')
    links += '</div>'
    return ui.page("HISTORY · 历史", title, "本地 · 离线可用",
                   ui.sec("提示", f'<div class="card"><div class="empty">{_esc(text)}'
                          f'</div>{links}</div>'), ui.chip("本机数据", "ok"),
                   back=("返回历史", "/history"))


def view_page(qs: dict) -> str:
    """按日期出看板：调 dashboard(..., live=False) 生成，再注入统一导航与适配样式。"""
    from . import appapi as A

    cov = coverage()
    today = dt.date.today().isoformat()
    day = _date(qs, "date", cov.get("last") or today)
    from_ = "today" if _one(qs, "from") == "today" else "history"
    if cov.get("first") and not (cov["first"] <= day <= cov["last"]):
        return _message(
            "这天还没有日汇总",
            f"{day} 不在已覆盖的日汇总区间 {cov['first']} ~ {cov['last']} 内。"
            "要更早的看板，请先到「我的 → 历史数据」补全；已经落库的历史活动"
            "可以直接在「历史活动」里查到。", cov)
    r = A.dashboard(day, False)
    if not r.get("ok"):
        return _message("看板生成失败", str(r.get("error") or "未知错误"), cov)
    try:
        doc = Path(str(r["html"])).read_text(encoding="utf-8")
    except OSError as e:
        return _message("看板打不开", str(e), cov)
    # 看板是独立产物，桌面 file:// 打开时不带这些；只有走 /view 才注入。
    fix = f"<style>{ui.DASH_FIX}</style>"
    return doc.replace("</head>", fix + "</head>", 1).replace(
        "<body>", "<body>" + ui.nav_html(day, cov, from_), 1)


def _month_end(key: str) -> str:
    """`2026-11` → `2026-11-30`。月长度不一致，不能一律拼 -31。"""
    try:
        y, m = int(key[:4]), int(key[5:7])
    except (ValueError, IndexError):
        return key
    d = dt.date(y, m, 28)
    while (d + dt.timedelta(days=1)).month == m:
        d += dt.timedelta(days=1)
    return d.isoformat()


def _month_bars(months: list[dict], hot: str = "", link: str = "") -> str:
    """周期柱状图（纯 SVG，不引第三方库，离线可用）。

    `hot` 是「当前这一根」的 key（日期 `2026-11-08` 或月份 `2026-11`）。
    旧版固定把最后一根画成实色 —— 那只对「近 12 个月」成立；一旦柱状图
    跟着选中的历史月份走，最后一根不再是「本月」，实色就成了误导。
    """
    today_hint = hot
    if not months:
        return '<div class="empty">还没有活动，先同步，或到「我的 → 历史数据」补全</div>'
    n = len(months)
    # 周/月视图会给到 28~31 根：密排时隐藏数值、轴标签每 5 格标一次，
    # 否则 9px 宽的柱子上挤一堆字比柱子本身还乱。
    dense = n > 14
    vapor = max((float(m.get("km") or 0) for m in months), default=0.0) or 1.0
    # viewBox 按手机实际宽度设计（不是 792）：按 792 画再缩到 420 屏，
    # 11px 的刻度字会被缩成 6px，等于没标。桌面端用 max-width 兜住。
    W, H = 400, 210
    pad_b, pad_t, gap = 38, 24, (3 if dense else 6)
    bw = (W - gap * (n - 1)) / n
    bars = []
    for i, m in enumerate(months):
        km = float(m.get("km") or 0)
        h = 6 + (H - pad_b - pad_t) * (km / vapor)
        x = i * (bw + gap)
        y = H - pad_b - h
        cur = bool(today_hint) and str(m.get("key") or "") == today_hint
        show_lab = (not dense) or i == 0 or (i + 1) % 5 == 0 or i == n - 1
        bars.append(
            f'<rect x="{x:.0f}" y="{y:.0f}" width="{bw:.0f}" height="{h:.0f}" rx="4" '
            f'fill="url(#mb)" opacity="{1 if cur else 0.52}"/>'
            + (f'<text x="{x + bw / 2:.0f}" y="{y - 7:.0f}" text-anchor="middle" '
               f'font-size="11" fill="{"#8CC2FF" if cur else "#7C8FA8"}">{km:.0f}</text>'
               if km > 0 and not dense else "")
            + (f'<text x="{x + bw / 2:.0f}" y="{H - 12}" text-anchor="middle" '
               f'font-size="11" fill="#7C8FA8">{_esc(m.get("label") or "")}</text>'
               if show_lab else ""))
        if link and not dense:
            # 柱子够粗（≤14 根）时才做成可点：30 根柱只有 9px 宽，
            # 手指根本点不准，宁可让下面的周期行去承担这个交互。
            # 再铺一层整列透明热区：没跑的月份柱子只有 6px 高，
            # 光靠它当点击目标，等于让人去点一根头发丝。
            hit = (f'<rect x="{x:.0f}" y="{pad_t - 12:.0f}" width="{bw:.0f}" '
                   f'height="{H - pad_b - pad_t + 12:.0f}" fill="transparent" '
                   f'pointer-events="all"/>')
            href = link.format(key=str(m.get("key") or ""))
            bars[-1] = (f'<a href="{_esc(href)}" data-nopush>{hit}'
                        f'{bars[-1]}</a>')
    return (f'<svg viewBox="0 0 {W} {H}" width="100%" '
            f'xmlns="http://www.w3.org/2000/svg" '
            f'style="display:block;max-width:520px;margin:0 auto">'
            f'<defs><linearGradient id="mb" x1="0" y1="1" x2="0" y2="0">'
            f'<stop offset="0" stop-color="#1D5FC4"/><stop offset="1" stop-color="#22D3EE"/>'
            f'</linearGradient></defs>{"".join(bars)}</svg>')


def _week_span(key: str) -> tuple[str, str]:
    """`2026-W45` → 该 ISO 周的周一 / 周日。

    不能直接用活动里的 min/max 日期：那只是「这周有跑的那几天」，
    周视图的柱状图要的是完整一周（没跑的日子也该占一根空柱）。
    """
    try:
        y, w = int(str(key)[:4]), int(str(key)[6:])
        mon = dt.date.fromisocalendar(y, w, 1)
    except (TypeError, ValueError):
        return "", ""
    return mon.isoformat(), (mon + dt.timedelta(days=6)).isoformat()


_WEEKDAY = "一二三四五六日"


def _weekday_series(days: list[dict]) -> list[dict]:
    """逐日序列的刻度从「8」换成「日」：周视图里光看数字读不出星期几。"""
    out = []
    for d in days:
        try:
            i = dt.date.fromisoformat(str(d.get("key"))).weekday()
        except (TypeError, ValueError):
            out.append(d)
            continue
        out.append({**d, "label": _WEEKDAY[i]})
    return out


def _bucket_rows(items: list[dict], mode: str, kind: str, sel: str = "") -> str:
    """周/月/年分组列表：每行是一个周期汇总。

    点一行**留在当前粒度**、把柱状图和汇总切到那个周期（`sel`），
    不再直接跳去活动明细 —— 以前一律 `mode=list`，于是「点哪个月都只看到
    最新那个月的图」。明细改成周期卡片上的「看这 N 次活动」按钮。
    """
    if not items:
        return '<div class="empty">这个范围里还没有活动。</div>'
    rows = []
    for b in items:
        key = str(b.get("key") or "")
        if mode == "week":
            # 行的日期用**完整 ISO 周**（周一到周日），不用活动里的 min/max：
            # 某一周只跑了周二、周四时，min/max 会显示成「11-03~11-05」，
            # 看着像这周只有三天，和柱状图的 7 根对不上。
            ws, we = _week_span(key)
            span = f"{ws[5:]}~{we[5:]}" if ws and we else (b.get("label") or "")
            title = f"{key} · {span}"
            icon = "▤"
        elif mode == "month":
            title = f"{key} · {int(key[5:7])} 月"
            icon = "▦"
        else:
            title = f"{key} 年"
            icon = "▣"
        desc = (f"{b.get('count') or 0} 次 · {float(b.get('km') or 0):.1f} km"
                f" · {float(b.get('dur_h') or 0):.1f} 小时")
        href = f"/history?{_q(mode=mode, sel=key, type=kind)}"
        rows.append(ui.row(icon, title, desc, href=href, tone="b",
                           on=bool(sel) and key == sel, nopush=True))
    return "".join(rows)


def history_page(qs: dict) -> str:
    """历史页：页首月度跑量统计 + 按周/月/年管理 + 活动明细列表。

    三种视图共用同一个 `mode` 参数：`list` 是原来的活动分页列表；
    `week` / `month` / `year` 显示周期汇总，点一行再回到该周期的明细。
    """
    from . import appapi as A

    cov = coverage()
    start = _date(qs, "start")
    end = _date(qs, "end")
    kind = _one(qs, "type")
    if kind not in ("", "run"):
        kind = ""
    mode = _one(qs, "mode", "list")
    if mode not in ("list", "week", "month", "year"):
        mode = "list"
    try:
        offset = max(0, int(_one(qs, "offset", "0") or 0))
    except ValueError:
        offset = 0

    stats = A.history_stats(None, None, "run" if kind in ("", "run") else "")
    if not stats.get("ok"):
        stats = {}
    total = stats.get("total") or {}
    cur_m = stats.get("month") or {}
    cur_y = stats.get("year") or {}
    months = stats.get("months") or []

    chips = (ui.chip(f"{total.get('count', 0)} 次跑步", "ok") +
             ui.chip(f"累计 {float(total.get('km') or 0):.0f} km") +
             ui.chip(f"{(stats.get('range') or {}).get('first') or '—'} 起") +
             ui.chip("本机 · 离线可用"))

    # ---- 页首：三个数和一张柱状图
    hero = ('<div class="grid three">' +
            ui.stat("本月跑量", f"{float(cur_m.get('km') or 0):.1f} km",
                    f"{cur_m.get('count') or 0} 次 · {cur_m.get('key') or ''}") +
            ui.stat("本年跑量", f"{float(cur_y.get('km') or 0):.0f} km",
                    f"{cur_y.get('count') or 0} 次 · {cur_y.get('key') or ''}") +
            ui.stat("累计跑量", f"{float(total.get('km') or 0):.0f} km",
                    f"{total.get('count') or 0} 次 · {float(total.get('dur_h') or 0):.0f} 小时") +
            '</div>')
    today = dt.date.today().isoformat()

    def chart_card(title: str, series: list, hot: str = "", foot: str = "",
                   link: str = "") -> str:
        """柱状图卡片。series 为空（例如这段确实没数据）就不占位置。"""
        if not series:
            return ""
        return (f'<div class="card" style="margin-bottom:10px">'
                f'<h3>{_esc(title)}</h3>{_month_bars(series, hot, link)}{foot}</div>')

    def range_stats(rs: str, re_: str) -> dict:
        """按周期范围再取一次统计；取不到就用空序列。"""
        if not (rs and re_):
            return {}
        got = A.history_stats(rs, re_, "run" if kind in ("", "run") else "")
        return got if got.get("ok") else {}

    def period_foot(rs: str, re_: str, count, km, dur_h) -> str:
        """柱状图下面那行「这个周期跑了多少」+ 进明细的按钮。

        以前「点月份」就是进明细；现在点行是**选中**这个月，
        进明细挪到这个按钮上，两条路都还在。
        """
        if not (rs and re_):
            return ""
        if not count:
            return (f'<div class="note" style="margin-top:8px">'
                    f'这个周期没有活动 · {rs} ~ {re_}</div>')
        href = f"/history?{_q(mode='list', start=rs, end=re_, type=kind)}"
        return (f'<div class="note" style="margin-top:8px">'
                f'{float(km or 0):.1f} km · {count} 次 · '
                f'{float(dur_h or 0):.1f} 小时 · {rs} ~ {re_}</div>'
                f'<div class="pager" style="margin-top:8px">'
                f'<a class="btn primary" href="{href}" data-nopush>'
                f'看这 {count} 次活动的明细</a></div>')

    # ---- 视图切换
    def mchip(label: str, m: str) -> str:
        on = " ok" if mode == m else ""
        return (f'<a class="chip{on}" href="/history?{_q(mode=m, type=kind)}" '
                f'data-nopush>{label}</a>')

    switch = ('<div class="tags">' + mchip("活动列表", "list") +
              mchip("按周", "week") + mchip("按月", "month") +
              mchip("按年", "year") + '</div>')

    # ---- 分组视图
    # `sel` 是「当前选中的周期 key」。点下面任意一行都会带着它回到本粒度，
    # 柱状图/汇总跟着它走；不传就默认最新一期。旧版没有这个参数，
    # 于是无论点哪个月，图上永远是最近那个月。
    if mode == "week":
        weeks = list(stats.get("weeks") or [])        # 旧 → 新
        keys = [str(w.get("key") or "") for w in weeks]
        sel = _one(qs, "sel")
        if sel not in keys:
            sel = keys[-1] if keys else ""
        cur = next((w for w in weeks if str(w.get("key")) == sel), {})
        rs, re_ = _week_span(sel) if sel else ("", "")
        # 同上：整周 7 根都画，周中打开也是 7 根（后几天是 0），
        # 否则周三只画 3 根，和月视图的疏密完全对不上。
        bs = range_stats(rs, re_)
        chart = chart_card(f"{sel} 每日跑量（km）" if sel else "本周每日跑量（km）",
                           _weekday_series(bs.get("days") or []),
                           hot=today,
                           foot=period_foot(rs, re_, cur.get("count"),
                                            cur.get("km"), cur.get("dur_h")))
        groups = _bucket_rows(list(reversed(weeks)), "week", kind, sel)
        grouping = ui.sec("最近 12 周", switch + chart
                          + '<div style="margin-top:10px">' + groups + '</div>')
    elif mode == "month":
        keys = [str(m.get("key") or "") for m in months]
        sel = _one(qs, "sel")
        if sel not in keys:
            sel = keys[-1] if keys else ""
        cur = next((m for m in months if str(m.get("key")) == sel), {})
        rs = f"{sel}-01" if sel else ""
        re_ = _month_end(sel) if sel else ""
        # 整月都要铺出来：11 月才到 9 号也画满 30 根，后面是 0。
        # 以前这里把 re_ 截到今天，月初打开只有 9 根柱子，图被压成一条窄带，
        # 也看不出「这个月还剩多少天」。柱状图的刻度本来就是按月数量身定的
        # （dense 分支就是为 28~31 根写的）。
        bs = range_stats(rs, re_)
        chart = chart_card(f"{sel} 每日跑量（km）" if sel else "本月每日跑量（km）",
                           bs.get("days") or [], hot=today,
                           foot=period_foot(rs, re_, cur.get("count"),
                                            cur.get("km"), cur.get("dur_h")))
        groups = _bucket_rows(
            [{"key": m["key"], "count": m["count"], "km": m["km"],
              "dur_h": m.get("dur_h") or 0,
              "start": m["key"] + "-01", "end": _month_end(m["key"])}
             for m in reversed(months)], "month", kind, sel)
        grouping = ui.sec("最近 12 个月", switch + chart
                          + '<div style="margin-top:10px">' + groups + '</div>')
    elif mode == "year":
        years = list(stats.get("years") or [])        # 新 → 旧
        keys = [str(y.get("key") or "") for y in years]
        sel = _one(qs, "sel")
        if sel not in keys:
            sel = keys[0] if keys else ""
        cur = next((y for y in years if str(y.get("key")) == sel), {})
        rs = f"{sel}-01-01" if sel else ""
        re_ = f"{sel}-12-31" if sel else ""
        bs = range_stats(rs, re_)
        # 年视图固定 12 根：当年没到的月份是空柱，不裁剪，
        # 否则「1~11 月」和「1~12 月」的图长得不一样，反而不易横向比较。
        chart = chart_card(f"{sel} 年各月跑量（km）" if sel else "本年各月跑量（km）",
                           bs.get("months") or [], hot=today[:7],
                           link="/history?mode=month&sel={key}",
                           foot=period_foot(rs, re_, cur.get("count"),
                                            cur.get("km"), cur.get("dur_h")))
        groups = _bucket_rows(
            [{"key": y["key"], "count": y["count"], "km": y["km"],
              "dur_h": y.get("dur_h") or 0, "start": y["key"] + "-01-01",
              "end": y["key"] + "-12-31"}
             for y in years], "year", kind, sel)
        grouping = ui.sec("按年份", switch + chart
                          + '<div style="margin-top:10px">' + groups + '</div>')
    else:
        grouping = ui.sec("按周期查看", switch)

    # ---- 活动明细（mode == list）
    detail = ""
    if mode == "list":
        r = A.activities(start or None, end or None, kind or None, PAGE, offset)
        if not r.get("ok"):
            return _message("历史活动查询失败", str(r.get("error") or "未知错误"), cov)
        items = r.get("items") or []
        s = r.get("start") or cov.get("act_first") or ""
        e = r.get("end") or dt.date.today().isoformat()
        scope = f"{s} ~ {e}" if s else "全部"

        selected_all = " selected" if not kind else ""
        selected_run = " selected" if kind == "run" else ""
        form = f"""<form class="card filters" method="get" action="/history" data-nopush>
      <input type="hidden" name="mode" value="list">
      <label>起 <input type="date" name="start" value="{_esc(start)}"></label>
      <label>止 <input type="date" name="end" value="{_esc(end)}"></label>
      <label>类型 <select name="type">
        <option value=""{selected_all}>全部</option>
        <option value="run"{selected_run}>只看跑步</option>
      </select></label>
      <button class="btn primary" type="submit">筛选</button>
      <div class="tags" style="width:100%">
        <a class="chip" href="/history?{_q(mode='list', type=kind, start=_shift(e, -29), end=e)}" data-nopush>近 30 天</a>
        <a class="chip" href="/history?{_q(mode='list', type=kind, start=_shift(e, -89), end=e)}" data-nopush>近 90 天</a>
        <a class="chip" href="/history?{_q(mode='list', type=kind, start=_shift(e, -364), end=e)}" data-nopush>近 365 天</a>
        <a class="chip" href="/history?{_q(mode='list', type=kind)}" data-nopush>全部</a>
      </div></form>"""

        rows = "".join(
            ui.row("▶", f"{i.get('date')} {i.get('start')} · {i.get('name')}",
                   f"{i.get('dist', 0):.2f} km · 配速 {i.get('pace')} · "
                   f"时长 {i.get('dur')} · 心率 {i.get('hr') or '—'} · 点这行出图",
                   aid=str(i.get("id") or ""), tone="b")
            for i in items)
        if not rows:
            rows = ('<div class="empty">这个区间没有活动。可以放宽日期，'
                    '或到「我的 → 历史数据」补全。</div>')

        pager = '<div class="pager">'
        if offset:
            pager += (f'<a class="btn" href="/history?'
                      f'{_q(mode="list", start=start, end=end, type=kind, offset=max(0, offset - PAGE))}" '
                      f'data-nopush>↑ 较新的活动</a>')
        if r.get("has_more"):
            pager += (f'<a class="btn primary" href="/history?'
                      f'{_q(mode="list", start=start, end=end, type=kind, offset=offset + PAGE)}" '
                      f'data-nopush>↓ 加载更早的活动</a>')
        pager += '</div>'

        # 柱子可点：点哪个月就切到「按月」并选中它，省得先去按月再翻。
        chart = chart_card("近 12 个月跑量（km） · 点柱子看当月", months,
                           hot=str((stats.get("month") or {}).get("key") or ""),
                           link="/history?mode=month&sel={key}")
        detail = (ui.sec("活动", chart + rows + pager +
                         '<div class="note" style="margin-top:8px">'
                         f'当前 {offset + 1}~{offset + len(items)} 条 · 共 {r.get("total", 0)} 条 · {_esc(scope)}'
                         '</div>') +
                  ui.sec("筛选", form))

    body = ui.sec("概览", hero) + grouping + detail
    return ui.page("HISTORY · 历史", "历史数据",
                   "点周期看当期柱状图，按周 / 月 / 年管理", body, chips)
