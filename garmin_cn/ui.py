# -*- coding: utf-8 -*-
"""App 内所有网页共用的一套外壳与视觉语言。

以前每个页面各写一份 CSS：文件列表首页 max-width 560、历史 680、AI 720、
看板 1120，字号/圆角/间距都不一样；功能又全挤在右上角 ⋮ 里。现在统一成：

  原生底部 4 个 tab（今日 / 历史 / 教练 / 我的）
    └ 每个 tab 对应一个同源网页，都用本模块的 page() 外壳
       ├ 统一的 page-head（分区名 + 标题 + 状态 chip）
       ├ 分区 sec + 卡片/网格，信息分块而不是一长条堆在中间
       └ 统一的 data-act / data-aid 点击转发到原生桥

看板是独立产物（桌面 file:// 也要好看），不改它的 CSS，只在 /view 里注入
DASH_FIX 做移动端适配；AI 页同理注入 AI_FIX。这样一套改动两端都生效，
出图用的 card / actcard 完全不碰。
"""
from __future__ import annotations

import datetime as dt
import html
from pathlib import Path

# 与 android values/colors.xml、aiui.CSS、report.CSS 保持同一组色值。
CSS = """
:root{
  --bg:#F4F6F2; --surface:#FFFFFF; --line:#E3E7DF; --line2:#EDF0EA;
  --ink:#1B1F1A; --ink2:#4A5147; --muted:#7A8276; --faint:#A5AC9F;
  --brand:#1D9E75; --brand-soft:#E4F3EC; --brand-ink:#0F6E56;
  --amber:#C08A16; --amber-soft:#FBF1DA;
  --red:#C2453F; --red-soft:#FAE7E5;
  --blue:#3A6EA5; --blue-soft:#E8F0F8;
  --r:16px;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);line-height:1.6;font-size:14px;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased}
a{color:var(--brand);text-decoration:none}
.app{max-width:820px;margin:0 auto;padding:0 14px calc(30px + env(safe-area-inset-bottom))}
.page-head{padding:20px 0 15px;border-bottom:1px solid var(--line);margin-bottom:4px}
.kicker{font-size:11px;font-weight:700;letter-spacing:1.4px;color:var(--brand-ink)}
.page-head h1{margin:6px 0 4px;font-size:24px;font-weight:650;letter-spacing:-.3px;line-height:1.3}
.lede{margin:0;font-size:13px;color:var(--muted)}
.meta{display:flex;gap:6px;flex-wrap:wrap;margin-top:12px}
.chip{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;padding:4px 10px;
  border-radius:999px;background:var(--surface);border:1px solid var(--line);color:var(--ink2)}
.chip.ok{background:var(--brand-soft);border-color:transparent;color:var(--brand-ink)}
.chip.warn{background:var(--amber-soft);border-color:transparent;color:#8A6410}
.sec{margin-top:24px}
.sec-head{display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin:0 0 10px}
.sec-head h2{margin:0;font-size:11.5px;font-weight:700;letter-spacing:1.2px;color:var(--muted)}
.sec-head .more{font-size:12px;color:var(--brand-ink)}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.grid.three{grid-template-columns:repeat(3,minmax(0,1fr))}
.grid.one{grid-template-columns:1fr}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:16px}
.card.flat{background:transparent;border-style:dashed}
.card.brand{background:var(--brand-soft);border-color:transparent}
.card h3{margin:0 0 10px;font-size:11.5px;font-weight:700;letter-spacing:1px;color:var(--muted)}
.hero{display:flex;align-items:flex-end;justify-content:space-between;gap:14px;flex-wrap:wrap}
.big{font-size:46px;font-weight:700;line-height:1;letter-spacing:-2px;font-variant-numeric:tabular-nums}
.big small{font-size:14px;font-weight:500;letter-spacing:0;color:var(--muted);margin-left:6px}
.hero-side{text-align:right;font-size:12px;color:var(--muted)}
.hero-side b{display:block;font-size:15px;color:var(--ink)}
.bar{height:6px;border-radius:999px;background:var(--line2);overflow:hidden;margin-top:14px}
.bar i{display:block;height:100%;border-radius:999px;background:var(--brand)}
.lab{font-size:11.5px;color:var(--muted)}
.val{font-size:23px;font-weight:650;line-height:1.2;margin-top:4px;font-variant-numeric:tabular-nums}
.note{font-size:11.5px;color:var(--faint);margin-top:3px}
.row{display:flex;align-items:center;gap:12px;background:var(--surface);
  border:1px solid var(--line);border-radius:14px;padding:12px 14px;margin-bottom:8px;
  color:inherit;cursor:pointer}
.row:active{background:#EDF2EA}
.row .ic{flex:0 0 auto;width:36px;height:36px;border-radius:11px;display:flex;
  align-items:center;justify-content:center;font-size:16px;
  background:var(--brand-soft);color:var(--brand-ink)}
.row .ic.b{background:var(--blue-soft);color:var(--blue)}
.row .ic.a{background:var(--amber-soft);color:var(--amber)}
.row .tx{min-width:0;flex:1}
.row .t{font-size:14px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row .d{font-size:11.5px;color:var(--muted);margin-top:2px;font-variant-numeric:tabular-nums}
.row .go{margin-left:auto;color:var(--faint);font-size:17px;flex:0 0 auto}
.acts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.act{display:flex;flex-direction:column;gap:7px;background:var(--surface);
  border:1px solid var(--line);border-radius:var(--r);padding:15px;color:inherit;cursor:pointer}
.act:active{background:#EDF2EA}
.act .ai{width:34px;height:34px;border-radius:10px;display:flex;align-items:center;
  justify-content:center;font-size:17px;background:var(--brand-soft);color:var(--brand-ink)}
.act .at{font-size:13.5px;font-weight:600}
.act .ad{font-size:11.5px;color:var(--muted);line-height:1.5}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;
  border:1px solid var(--line);background:var(--surface);color:var(--ink2);
  border-radius:999px;padding:8px 14px;font-size:12.5px;font-family:inherit;cursor:pointer}
.btn.primary{background:var(--brand);border-color:var(--brand);color:#fff}
.btn:active{opacity:.85}
.empty{padding:20px 4px;font-size:13px;color:var(--muted);line-height:1.8}
.foot{margin-top:30px;padding-top:14px;border-top:1px solid var(--line);
  font-size:11px;color:var(--faint);line-height:1.8}
form.filters{display:flex;gap:9px;flex-wrap:wrap;align-items:center}
form.filters label{display:inline-flex;gap:5px;align-items:center;font-size:11.5px;color:var(--muted)}
input,select{font-family:inherit;font-size:12.5px;color:var(--ink);background:var(--surface);
  border:1px solid var(--line);border-radius:9px;padding:7px 9px;max-width:100%}
.pager{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:12px}
.tags{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
@media(max-width:380px){
  .acts,.grid{grid-template-columns:1fr}
  .big{font-size:40px}
}
"""

# 看板是独立的宽版 HTML：在 App 里强制单列/收紧留白，别让三栏挤成一小坨。
DASH_FIX = """
.wrap{max-width:820px;padding:0 14px calc(30px + env(safe-area-inset-bottom))}
.top{margin:0 0 14px}
.top h1{font-size:19px}
.hero,.two{grid-template-columns:1fr}
.metrics{grid-template-columns:repeat(3,minmax(0,1fr))}
table{display:block;overflow-x:auto;white-space:nowrap}
#gc-nav{margin:0 -14px 16px;padding:9px 14px;position:sticky;top:0;z-index:99;
  background:rgba(244,246,242,.97);border-bottom:1px solid #E3E7DF;
  display:flex;gap:7px;align-items:center;flex-wrap:wrap;
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
#gc-nav a,#gc-nav span,#gc-nav button{font-size:12px;text-decoration:none;padding:5px 10px;
  border-radius:999px;background:#fff;border:1px solid #E3E7DF;color:#1B1F1A;
  font-family:inherit;cursor:pointer;white-space:nowrap}
#gc-nav a.primary,#gc-nav button{background:#1D9E75;border-color:#1D9E75;color:#fff}
#gc-nav span.off{opacity:.38}
#gc-nav input{font-size:12px;padding:5px 7px;border:1px solid #E3E7DF;
  border-radius:8px;background:#fff;color:#1B1F1A;font-family:inherit}
#gc-nav form{display:inline-flex;gap:5px;align-items:center;margin:0}
#gc-nav .tag{margin-left:auto;border:0;background:none;color:#6E7669}
"""

# AI 页自带完整 CSS，只统一外框与标题节奏，不动它内部的交互。
AI_FIX = """
.wrap{max-width:820px;padding:0 14px calc(30px + env(safe-area-inset-bottom))}
.top{padding-top:18px;margin-bottom:12px}
.top h1{font-size:21px}
.card{border-radius:16px}
"""

BRIDGE_JS = """<script>
(function(){
  if (window.__gcUI) return;
  window.__gcUI = true;
  document.addEventListener('click', function(e){
    var n = e.target;
    while (n && n !== document) {
      if (n.getAttribute) {
        var aid = n.getAttribute('data-aid');
        if (aid) { if (window.GC && GC.activity) GC.activity(aid); return; }
        var act = n.getAttribute('data-act');
        if (act) { if (window.GC && GC.action) GC.action(act); return; }
      }
      n = n.parentNode;
    }
  }, false);
})();
</script>"""


def esc(v) -> str:
    return html.escape(str(v if v is not None else ""), quote=True)


def coverage() -> dict:
    """只读本地库：日汇总 / 活动各自的覆盖范围。不联网。"""
    from . import appapi as A

    cov = {"first": "", "last": "", "days": 0, "activities": 0,
           "act_first": "", "act_total": 0, "recent": []}
    try:
        d = A.days_index(14)
        if d.get("ok"):
            cov.update(first=d.get("first") or "", last=d.get("last") or "",
                       days=d.get("count") or 0, activities=d.get("activities") or 0,
                       recent=d.get("items") or [])
        a = A.activities(None, None, None, 1, 0)
        if a.get("ok"):
            cov["act_first"] = a.get("start") or ""
            cov["act_total"] = a.get("total") or 0
    except Exception:
        pass                       # 空库也照常出页面
    return cov


def page(kicker: str, title: str, lede: str, body: str,
         chips: str = "", foot: str = "数据只保存在本机，不上传任何第三方。") -> str:
    """所有 tab 页的统一外壳。"""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#F4F6F2">
<title>{esc(title)} · RunnerCoach</title>
<style>{CSS}</style></head><body><div class="app">
<header class="page-head">
  <div class="kicker">{esc(kicker)}</div>
  <h1>{esc(title)}</h1><p class="lede">{esc(lede)}</p>
  <div class="meta">{chips}</div>
</header>
<main>{body}</main>
<div class="foot">{esc(foot)}</div>
</div>{BRIDGE_JS}</body></html>"""


def sec(title: str, body: str, more: str = "") -> str:
    return (f'<section class="sec"><div class="sec-head"><h2>{esc(title)}</h2>'
            f'{_more(more)}</div>{body}</section>')


def _more(more) -> str:
    """more 可以是 (文字, 链接) 或完整的 HTML 片段。"""
    if not more:
        return ""
    if isinstance(more, (tuple, list)) and len(more) == 2:
        return f'<a class="more" href="{esc(more[1])}">{esc(more[0])} →</a>'
    return str(more)


def chip(text: str, tone: str = "") -> str:
    cls = f"chip {tone}" if tone else "chip"
    return f'<span class="{cls}">{esc(text)}</span>'


def stat(label: str, value, note: str = "", unit: str = "") -> str:
    u = f'<small style="font-size:12px;color:var(--muted)"> {esc(unit)}</small>' if unit else ""
    return (f'<div class="card"><div class="lab">{esc(label)}</div>'
            f'<div class="val">{esc(value)}{u}</div>'
            f'<div class="note">{esc(note)}</div></div>')


def row(icon: str, title: str, desc: str, *, act: str = "", href: str = "",
        aid: str = "", tone: str = "", go: bool = True) -> str:
    tag = "a" if href else "div"
    attr = f' href="{esc(href)}"' if href else ""
    if act:
        attr += f' data-act="{esc(act)}"'
    if aid:
        attr += f' data-aid="{esc(aid)}"'
    arrow = '<span class="go">›</span>' if go else ""
    return (f'<{tag} class="row"{attr}><span class="ic {esc(tone)}">{esc(icon)}</span>'
            f'<span class="tx"><span class="t">{esc(title)}</span>'
            f'<span class="d">{esc(desc)}</span></span>{arrow}</{tag}>')


def act(icon: str, title: str, desc: str, action: str = "", href: str = "") -> str:
    tag = "a" if href else "div"
    attr = f' href="{esc(href)}"' if href else f' data-act="{esc(action)}"'
    return (f'<{tag} class="act"{attr}><span class="ai">{esc(icon)}</span>'
            f'<span class="at">{esc(title)}</span>'
            f'<span class="ad">{esc(desc)}</span></{tag}>')


def nav_html(day: str, cov: dict) -> str:
    """看板顶部的日期导航（样式来自 DASH_FIX，样式表由 view 注入）。"""
    first, last = cov.get("first") or day, cov.get("last") or day

    def shift(n: int) -> str:
        try:
            return (dt.date.fromisoformat(day) + dt.timedelta(days=n)).isoformat()
        except (TypeError, ValueError):
            return day

    prev, nxt = shift(-1), shift(1)
    p = (f'<a href="/view?date={prev}">← 前一天</a>' if first <= prev <= last
         else '<span class="off">← 前一天</span>')
    n = (f'<a href="/view?date={nxt}">后一天 →</a>' if first <= nxt <= last
         else '<span class="off">后一天 →</span>')
    return f"""<div id="gc-nav">
  {p}
  <form method="get" action="/view">
    <input type="date" name="date" min="{esc(first)}" max="{esc(last)}" value="{esc(day)}">
    <button class="primary" type="submit">跳</button>
  </form>
  {n}
  <a href="/view?date={esc(last)}">最新</a>
  <a href="/history">历史活动</a>
  <a href="/">首页</a>
  <span class="tag">{esc(first)} ~ {esc(last)}</span>
</div>"""


def home_page() -> str:
    """「今日」tab：概览 + 分区卡片，不再是产物文件列表。"""
    from . import appapi as A

    today = dt.date.today().isoformat()
    st = A.status()
    cov = coverage()
    day = st.get("last") or today
    res: dict = {}
    try:
        from . import metrics as M
        from .appapi import DB
        from .store import Store

        s = Store(DB)
        try:
            res = M.derive(s, day)
        finally:
            s.conn.close()
    except Exception:
        res = {}

    rd = res.get("readiness") or {}
    load = res.get("load") or {}
    score = rd.get("score")
    level = rd.get("level") or "暂无数据"
    acts = res.get("activities") or []

    week = "一二三四五六日"[dt.date.fromisoformat(day).weekday()] if day else ""
    chips = (chip(f"{day} 周{week}", "ok") +
             chip(f"已同步 {st.get('days') or 0} 天") +
             chip(f"上次同步 {esc(str(st.get('last_sync') or '—')[5:16])}") +
             chip("本机 · 离线可用"))

    hero = f"""<div class="card brand"><div class="hero">
      <div><div class="lab">恢复度</div>
        <div class="big">{esc(score if score is not None else '—')}<small>/ 100</small></div>
        <div class="note" style="margin-top:6px;font-size:13px;color:var(--brand-ink)">
          {esc(level)} · 睡眠 35% / HRV 30% / 静息心率 20% / 身体电量 15%</div>
      </div>
      <div class="hero-side">今日建议<b>{esc(_session(res))}</b>
        <span>{esc(day)} 的本地分析结果</span></div>
    </div><div class="bar"><i style="width:{max(0, min(100, int(score or 0)))}%"></i></div></div>"""

    stats = ('<div class="grid three">' +
             stat("今日负荷", load.get("load") or "—", "近 7 天累计") +
             stat("CTL 体能", load.get("ctl") or "—", "42 天指数均线") +
             stat("TSB 状态", load.get("tsb") or "—", "体能 − 疲劳") +
             '</div>')

    quick = ('<div class="acts">' +
             act("▣", "导出今日一图流", "生成 1080×1920 竖图并分享", action="export") +
             act("↧", "补拉更多历史", "90 / 180 / 365 天或自定义区间", action="historySync") +
             act("◫", "打开完整看板", "睡眠 / 恢复 / 负荷 / 课表", href=f"/view?date={day}") +
             act("≡", "历史活动列表", "按日期查、点一条出活动图", href="/history") +
             '</div>')

    rows = ""
    for a in acts[:5]:
        aid = str(a.get("activity_id") or "")
        dist = (a.get("distance_m") or 0) / 1000
        pace = a.get("pace_s_km") or 0
        p = f"{int(pace) // 60}'{int(pace) % 60:02d}\"" if pace else "—"
        rows += row("▶", a.get("name") or "活动",
                    f"{a.get('date') or ''} · {dist:.2f} km · 配速 {p}",
                    aid=aid, tone="b")
    if not rows:
        rows = ('<div class="empty">还没有活动数据。下拉同步一次，'
                '或到「历史」里补拉更早的区间。</div>')

    cover = (f'<div class="grid three">' +
             stat("日汇总覆盖", f"{cov['days']} 天", f"{cov['first']} ~ {cov['last']}") +
             stat("活动", f"{cov['activities']} 条", f"最早 {cov['act_first'] or '—'}") +
             stat("数据源", "佳明", "可切换高驰 / 小米体重") +
             '</div>')

    body = (sec("今日状态", hero + '<div style="height:10px"></div>' + stats) +
            sec("快捷操作", quick) +
            sec("最近活动", rows, ("查看全部", "/history")) +
            sec("数据覆盖", cover))
    return page("TODAY · 今日", "今天该怎么练", "一屏看完恢复、负荷和最近几次训练",
                body, chips)


def _session(res: dict) -> str:
    try:
        from . import brief as B

        s = (B.brief(res) or {}).get("session") or {}
        return str(s.get("type") or s.get("title") or "按恢复度安排")
    except Exception:
        return "按恢复度安排"


def me_page() -> str:
    """「我的」tab：数据源、低频动作、产物、关于。原本都在右上角 ⋮ 里。"""
    from . import appapi as A

    st = A.status()
    cov = coverage()
    try:
        c = A.coros_status()
    except Exception:
        c = {}
    try:
        m = A.mi_status()
    except Exception:
        m = {}
    try:
        from . import __version__ as VER
    except Exception:
        VER = "—"

    g = "已登录" if st.get("logged_in") else "未登录"
    c_ok = "已登录" if c.get("logged_in") else "未连接"
    m_ok = "已登录" if m.get("logged_in") else "未连接"
    chips = (chip("本机数据", "ok") + chip(f"v{VER}") + chip("零第三方依赖"))

    sources = (row("G", "佳明 Garmin", f"{g} · {esc(st.get('email') or '未设置账号')}",
                   act="login") +
               row("C", "高驰 COROS", f"{c_ok} · 可独立同步活动", act="login", tone="b") +
               row("W", "小米体重", f"{m_ok} · 体脂秤数据", act="mi", tone="a"))

    actions = ('<div class="acts">' +
               act("▣", "导出一图流", "今日看板竖图", action="export") +
               act("↧", "补拉历史", "增量补齐更早的日汇总", action="historySync") +
               act("⚙", "模型服务设置", "AI 教练的密钥与模型", action="settings") +
               act("W", "小米体重", "登录与同步体脂秤", action="mi") +
               act("↻", "刷新当前页", "重新读取本机数据", action="refresh") +
               act("◫", "产物文件", "看板 / 图片 / 视频清单", href="/files") +
               '</div>')

    about = (f'<div class="card"><div class="lab">RunnerCoach · v{esc(VER)}</div>'
             '<div class="note" style="margin-top:8px;font-size:12.5px;line-height:1.8">'
             '凭据与健康数据只存在这台设备的 App 私有目录；看板、图片、回放全部本地生成，'
             '页面不加载任何 CDN、远程字体或统计脚本。卸载 App 即清空本地数据。</div></div>')

    stat_line = (f'<div class="grid three">' +
                 stat("日汇总", f"{cov['days']} 天", f"{cov['first']} ~ {cov['last']}") +
                 stat("活动", f"{cov['activities']} 条", "全部本地") +
                 stat("上次同步", str(st.get("last_sync") or "—")[5:16], "下拉可随时刷新") +
                 '</div>')

    body = (sec("数据源", sources) +
            sec("操作", actions) +
            sec("本地数据", stat_line) +
            sec("关于", about))
    return page("ME · 我的", "数据与设置", "账号、同步、产物和隐私都在这里", body, chips)


def files_page(root: Path) -> str:
    """原「首页」降级为产物清单：给需要找 HTML/PNG/MP4 文件的人。"""
    from .serve import LIST_EXT, _size, _title

    files = []
    if root.is_dir():
        for p in root.iterdir():
            if (p.is_file() and p.suffix.lower() in LIST_EXT
                    and not p.name.startswith("_") and p.stat().st_size > 0):
                files.append(p)
    files.sort(key=lambda x: -x.stat().st_mtime)
    cov = coverage()
    rows = ""
    for p in files[:60]:
        title, group, kind = _title(p.name)
        when = dt.datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M")
        href = p.name
        if p.name == "latest.html" and cov.get("last"):
            href = "/view?date=" + cov["last"]
        elif p.name.startswith("dashboard-") and p.name.endswith(".html"):
            d = p.name[len("dashboard-"):-5]
            try:
                dt.date.fromisoformat(d)
                href = "/view?date=" + d
            except ValueError:
                pass
        rows += row("▤", title, f"{group} · {kind} · {when} · {_size(p.stat().st_size)}",
                    href=href, tone="b")
    if not rows:
        rows = '<div class="empty">还没有生成任何产物。同步或出一次图，这里就会出现文件。</div>'
    return page("FILES · 产物", "本机产物文件", "看板、图片、视频都只存在这台设备上",
                sec("最近生成", rows, ("返回首页", "/")), chip("只读"))
