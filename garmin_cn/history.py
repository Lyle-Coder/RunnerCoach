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


def history_page(qs: dict) -> str:
    """历史活动列表：本地库分页查询，点一行交给 App 的 JS 桥出活动图。"""
    from . import appapi as A

    cov = coverage()
    start = _date(qs, "start")
    end = _date(qs, "end")
    kind = _one(qs, "type")
    if kind not in ("", "run"):
        kind = ""
    try:
        offset = max(0, int(_one(qs, "offset", "0") or 0))
    except ValueError:
        offset = 0

    r = A.activities(start or None, end or None, kind or None, PAGE, offset)
    if not r.get("ok"):
        return _message("历史活动查询失败", str(r.get("error") or "未知错误"), cov)
    items = r.get("items") or []
    s = r.get("start") or cov.get("act_first") or ""
    e = r.get("end") or dt.date.today().isoformat()
    scope = f"{s} ~ {e}" if s else "全部"
    chips = (ui.chip(f"{r.get('total', 0)} 条活动", "ok") +
             ui.chip(scope) +
             ui.chip("只看跑步" if kind == "run" else "全部类型") +
             ui.chip(f"第 {offset + 1}~{offset + len(items)} 条"))

    selected_all = " selected" if not kind else ""
    selected_run = " selected" if kind == "run" else ""
    form = f"""<form class="card filters" method="get" action="/history" data-nopush>
      <label>起 <input type="date" name="start" value="{_esc(start)}"></label>
      <label>止 <input type="date" name="end" value="{_esc(end)}"></label>
      <label>类型 <select name="type">
        <option value=""{selected_all}>全部</option>
        <option value="run"{selected_run}>只看跑步</option>
      </select></label>
      <button class="btn primary" type="submit">筛选</button>
      <div class="tags" style="width:100%">
        <a class="chip" href="/history?{_q(type=kind, start=_shift(e, -29), end=e)}" data-nopush>近 30 天</a>
        <a class="chip" href="/history?{_q(type=kind, start=_shift(e, -89), end=e)}" data-nopush>近 90 天</a>
        <a class="chip" href="/history?{_q(type=kind, start=_shift(e, -364), end=e)}" data-nopush>近 365 天</a>
        <a class="chip" href="/history?{_q(type=kind)}" data-nopush>全部</a>
      </div></form>"""

    rows = "".join(
        ui.row("▶", f"{i.get('date')} {i.get('start')} · {i.get('name')}",
               f"{i.get('dist', 0):.2f} km · 配速 {i.get('pace')} · "
               f"时长 {i.get('dur')} · 心率 {i.get('hr') or '—'} · 点这行出图",
               aid=str(i.get("id") or ""), tone="b")
        for i in items)
    if not rows:
        rows = ('<div class="empty">这个区间没有活动。可以放宽日期，'
                '或用「补拉更多历史」同步更早的数据。</div>')

    pager = '<div class="pager">'
    if offset:
        pager += (f'<a class="btn" href="/history?'
                  f'{_q(start=start, end=end, type=kind, offset=max(0, offset - PAGE))}" '
                  f'data-nopush>↑ 较新的活动</a>')
    if r.get("has_more"):
        pager += (f'<a class="btn primary" href="/history?'
                  f'{_q(start=start, end=end, type=kind, offset=offset + PAGE)}" '
                  f'data-nopush>↓ 加载更早的活动</a>')
    pager += '</div>'

    # 「补拉历史」只在这里出现一次：今日页和我的页都不再重复放这个动作
    more_data = ('<div class="pager">'
                 '<div class="btn primary" data-act="historySync">补拉更早的数据</div>'
                 '</div>')

    body = (ui.sec("覆盖范围与日期", index_section(cov)) +
            ui.sec("筛选", form + more_data) +
            ui.sec("活动", rows + pager))
    return ui.page("HISTORY · 历史", "历史活动", "按区间、类型分页查本地库，点一条出活动图",
                   body, chips)
