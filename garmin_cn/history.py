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
    """「历史」页顶部的覆盖情况卡片（也是首页数据覆盖区块的素材）。"""
    if not cov.get("days"):
        return ('<div class="card"><div class="empty">本地库还是空的。'
                '下拉同步一次，或用「补拉更多历史」拉取更早的区间。</div></div>')
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
    links = ('<div class="pager">'
             '<div class="btn primary" data-act="historySync">补拉更早的数据</div>')
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
            "要更早的看板，请先用「补拉更多历史」同步；已经落库的历史活动"
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


def _month_bars(months: list[dict]) -> str:
    """近 12 个月跑量柱状图（纯 SVG，不引第三方库，离线可用）。

    当前月用实色、历史月半透明：一眼能看出「这个月还没跑完」。
    没跑的月份也画一根 8px 的底座，否则中间断档读不出来。
    """
    if not months:
        return '<div class="empty">还没有活动，先同步或补拉历史</div>'
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
        cur = i == n - 1
        show_lab = (not dense) or i == 0 or (i + 1) % 5 == 0 or cur
        bars.append(
            f'<rect x="{x:.0f}" y="{y:.0f}" width="{bw:.0f}" height="{h:.0f}" rx="4" '
            f'fill="url(#mb)" opacity="{1 if cur else 0.52}"/>'
            + (f'<text x="{x + bw / 2:.0f}" y="{y - 7:.0f}" text-anchor="middle" '
               f'font-size="11" fill="{"#8CC2FF" if cur else "#7C8FA8"}">{km:.0f}</text>'
               if km > 0 and not dense else "")
            + (f'<text x="{x + bw / 2:.0f}" y="{H - 12}" text-anchor="middle" '
               f'font-size="11" fill="#7C8FA8">{_esc(m.get("label") or "")}</text>'
               if show_lab else ""))
    return (f'<svg viewBox="0 0 {W} {H}" width="100%" '
            f'xmlns="http://www.w3.org/2000/svg" '
            f'style="display:block;max-width:520px;margin:0 auto">'
            f'<defs><linearGradient id="mb" x1="0" y1="1" x2="0" y2="0">'
            f'<stop offset="0" stop-color="#1D5FC4"/><stop offset="1" stop-color="#22D3EE"/>'
            f'</linearGradient></defs>{"".join(bars)}</svg>')


def _bucket_rows(items: list[dict], mode: str, kind: str) -> str:
    """周/月/年分组列表：每行是一个周期汇总，点进去看该周期的活动明细。"""
    if not items:
        return '<div class="empty">这个范围里还没有活动。</div>'
    rows = []
    for b in items:
        if mode == "week":
            title = f"{b['key']} · {b.get('label') or ''}"
            icon = "▤"
        elif mode == "month":
            title = f"{b['key']} · {int(str(b['key'])[5:7])} 月"
            icon = "▦"
        else:
            title = f"{b['key']} 年"
            icon = "▣"
        desc = (f"{b.get('count') or 0} 次 · {float(b.get('km') or 0):.1f} km"
                f" · {float(b.get('dur_h') or 0):.1f} 小时")
        href = f"/history?{_q(start=b.get('start') or '', end=b.get('end') or '',
                              type=kind, mode='list')}"
        rows.append(ui.row(icon, title, desc, href=href, tone="b"))
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
    def chart_card(title: str, series: list) -> str:
        """柱状图卡片。series 为空（例如这段确实没数据）就不占位置。"""
        if not series:
            return ""
        return (f'<div class="card" style="margin-bottom:10px">'
                f'<h3>{_esc(title)}</h3>{_month_bars(series)}</div>')

    def range_stats(rs: str, re_: str) -> dict:
        """按周期范围再取一次统计；取不到就用空序列。"""
        if not (rs and re_):
            return {}
        got = A.history_stats(rs, re_, "run" if kind in ("", "run") else "")
        return got if got.get("ok") else {}

    _types = "run" if kind in ("", "run") else ""

    # ---- 视图切换
    def mchip(label: str, m: str) -> str:
        on = " ok" if mode == m else ""
        return (f'<a class="chip{on}" href="/history?{_q(mode=m, type=kind)}" '
                f'data-nopush>{label}</a>')

    switch = ('<div class="tags">' + mchip("活动列表", "list") +
              mchip("按周", "week") + mchip("按月", "month") +
              mchip("按年", "year") + '</div>')

    # ---- 分组视图
    if mode == "week":
        base = (stats.get("weeks") or [{}])[-1]
        rs = start or str(base.get("start") or "")
        re_ = end or str(base.get("end") or "")
        bs = range_stats(rs, re_)
        chart = chart_card(f"{rs[5:]}~{re_[5:]} 每日跑量（km）" if rs and re_
                           else "本周每日跑量（km）", bs.get("days") or [])
        groups = _bucket_rows(stats.get("weeks") or [], "week", kind)
        grouping = ui.sec("最近 12 周", switch + chart
                          + '<div style="margin-top:10px">' + groups + '</div>')
    elif mode == "month":
        key = str((months or [{}])[-1].get("key") or "")
        rs = start or (f"{key}-01" if key else "")
        re_ = end or (_month_end(key) if key else "")
        bs = range_stats(rs, re_)
        chart = chart_card(f"{key} 每日跑量（km）" if key else "本月每日跑量（km）",
                           bs.get("days") or [])
        groups = _bucket_rows(
            [{"key": m["key"], "count": m["count"], "km": m["km"], "dur_h": 0,
              "start": m["key"] + "-01", "end": _month_end(m["key"])}
             for m in reversed(months)], "month", kind)
        grouping = ui.sec("最近 12 个月", switch + chart
                          + '<div style="margin-top:10px">' + groups + '</div>')
    elif mode == "year":
        key = str((stats.get("years") or [{}])[0].get("key") or "")
        rs = start or (f"{key}-01-01" if key else "")
        re_ = end or (f"{key}-12-31" if key else "")
        bs = range_stats(rs, re_)
        chart = chart_card(f"{key} 年各月跑量（km）" if key else "本年各月跑量（km）",
                           bs.get("months") or [])
        groups = _bucket_rows(
            [{"key": y["key"], "count": y["count"], "km": y["km"],
              "dur_h": y.get("dur_h") or 0, "start": y["key"] + "-01-01",
              "end": y["key"] + "-12-31"}
             for y in (stats.get("years") or [])], "year", kind)
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
                    '或用「补拉更早的数据」同步更早的区间。</div>')

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

        more_data = ('<div class="pager">'
                     '<div class="btn primary" data-act="historySync">补拉更早的数据</div>'
                     '<div class="btn" data-act="historySyncAll">一键拉全部历史</div>'
                     '</div>')
        chart = chart_card("近 12 个月跑量（km）", months)
        detail = (ui.sec("活动", chart + rows + pager +
                         '<div class="note" style="margin-top:8px">'
                         f'当前 {offset + 1}~{offset + len(items)} 条 · 共 {r.get("total", 0)} 条 · {_esc(scope)}'
                         '</div>') +
                  ui.sec("筛选", form + more_data))

    body = ui.sec("概览", hero) + grouping + detail
    return ui.page("HISTORY · 历史", "历史数据",
                   "按周 / 月 / 年管理，页首看月度跑量", body, chips)
