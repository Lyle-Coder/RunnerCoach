# -*- coding: utf-8 -*-
"""赛事页（/event）：赛事档案 + 关门余量表 + 装备 + 轨迹导入。

一页回答三件事：
  1) 这场比赛长什么样（距离 / 爬升 / 关门 / 倒计时）
  2) 每个 CP 我还有多少关门余量，哪个最紧
  3) 装备备了几件、路线文件导进来了没有（GPX 可从 App 里直接选文件）
"""
from __future__ import annotations

import datetime as dt
import re

from . import event as E
from . import ui

# 只在赛事页生效的样式，跟着页面走，不去动全局 ui.CSS
CSS = """<style>
.kv{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;text-align:center}
.kv b{display:block;font-size:17px;color:var(--fg,#E8F0FB);letter-spacing:-.4px}
.kv b small{font-size:11px;font-weight:400;color:var(--muted,#7F8A97);margin-left:1px}
.kv span{display:block;font-size:11px;color:var(--muted,#7F8A97);margin-top:2px}
.sub2{font-size:12px;color:var(--muted,#7F8A97);line-height:1.7;margin-top:10px}
.trk{display:flex;flex-wrap:wrap;gap:4px 12px;align-items:baseline;margin-bottom:8px}
.trk b{font-size:17px;color:var(--fg,#E8F0FB)}
.trk span{font-size:12px;color:var(--muted,#7F8A97)}
.prof{width:100%;max-width:100%;height:96px;display:block;margin:6px 0 2px;
  background:rgba(79,214,166,.05);border-radius:8px}
.tag{font-size:10.5px;color:#7F8A97;border:1px solid rgba(127,138,151,.4);
  border-radius:5px;padding:1px 5px}
.warnbox{color:#D6A75A}
.cmptitle{font-size:11.5px;color:var(--muted,#7F8A97);margin:10px 0 2px}
.cmphead,.cmp{display:grid;grid-template-columns:30px minmax(0,1fr) 42px 42px 42px;
  gap:4px;align-items:center;font-size:11.5px;padding:5px 0;
  border-bottom:1px solid rgba(255,255,255,.04)}
.cmphead{color:var(--muted,#7F8A97);font-size:10.5px;padding-top:0}
.cph,.cp{display:grid;grid-template-columns:32px minmax(0,1fr) 36px 64px 56px;
  gap:4px;align-items:center;font-size:12px;padding:7px 0;
  border-bottom:1px solid rgba(255,255,255,.05)}
.cph{color:var(--muted,#7F8A97);font-size:11px;border-bottom-color:rgba(255,255,255,.12)}
.cp:last-of-type{border-bottom:0}
.cpn{color:var(--muted,#7F8A97);font-size:11px}
.cpl{color:var(--fg,#E8F0FB);min-width:0;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.cpk,.cpc{color:var(--muted,#7F8A97);text-align:right;font-variant-numeric:tabular-nums;
  white-space:nowrap}
.cpm{text-align:right;font-weight:600;font-variant-numeric:tabular-nums}
.cpm.ok{color:#4FD6A6}.cpm.warn{color:#D6A75A}.cpm.bad{color:#E06A5F}
.gw{display:flex;align-items:center;gap:10px}
.gbar{flex:1;height:8px;border-radius:5px;background:rgba(255,255,255,.08);overflow:hidden}
.gbar i{display:block;height:100%;background:linear-gradient(90deg,#3B82F6,#22D3EE)}
.gw span{font-size:12px;color:var(--muted,#7F8A97);flex:none}
.glist{margin:12px 0 0;display:flex;flex-direction:column}
.grow{display:flex;align-items:flex-start;gap:9px;padding:7px 0;
  border-bottom:1px solid rgba(255,255,255,.05);margin:0}
.grow:last-child{border-bottom:0}
.gchk{flex:none;width:20px;height:20px;margin:1px 0 0;padding:0;border-radius:6px;
  border:1.5px solid rgba(127,138,151,.55);background:transparent;color:#070B12;
  font-size:13px;line-height:1;font-weight:700;cursor:pointer;
  display:flex;align-items:center;justify-content:center}
.gchk.on{background:#4FD6A6;border-color:#4FD6A6;color:#062018}
.gtx{font-size:12.5px;line-height:1.55;color:var(--fg,#E8F0FB)}
.gtx.on{color:var(--muted,#7F8A97);text-decoration:line-through}
</style>"""


def _fmt_margin(s: float) -> tuple[str, str]:
    """余量 → (文案, 颜色类)。负值红色，1 h 内黄色，其余绿色。"""
    s = int(s)
    sign = "+" if s >= 0 else "-"
    a = abs(s)
    txt = (f"{sign}{a // 3600}h{a % 3600 // 60:02d}m" if a >= 3600
           else f"{sign}{a // 60}m")
    if s < 0:
        return txt, "bad"
    if s < 3600:
        return txt, "warn"
    return txt, "ok"


def _spark(profile: list) -> str:
    """剖面 sparkline（纯 SVG，不依赖外部图表库）。"""
    if len(profile) < 3:
        return ""
    xs = [p[0] for p in profile]
    ys = [p[1] for p in profile]
    x0, x1 = min(xs), max(xs) or 1
    y0, y1 = min(ys), max(ys)
    span = (y1 - y0) or 1
    W, H = 640.0, 120.0
    pts = []
    for x, y in zip(xs, ys):
        px = (x - x0) / ((x1 - x0) or 1) * W
        py = H - (y - y0) / span * (H - 10) - 5
        pts.append(f"{px:.1f},{py:.1f}")
    return (f'<svg class="prof" viewBox="0 0 {W:.0f} {H:.0f}" '
            f'preserveAspectRatio="none">'
            f'<polyline points="{" ".join(pts)}" fill="none" stroke="#4FD6A6" '
            f'stroke-width="2.2" stroke-linejoin="round"/>'
            f'<text x="2" y="12" fill="#7F8A97" font-size="11">{y1:.0f} m</text>'
            f'<text x="2" y="{H - 4:.0f}" fill="#7F8A97" font-size="11">{y0:.0f} m</text>'
            f"</svg>")


def _relief(rows: list) -> str:
    """相对高度剖面 + CP 余量标记（没有 GPX 时的降级方案）。

    只有分段 D+/D-，没有绝对海拔，所以纵轴画「相对起点的高度」，
    曲线形状可信、绝对值不可信——标签里写清楚，别让人当成地形图。
    """
    if len(rows) < 2:
        return ""
    xs, ys = [0.0], [0.0]
    for r in rows:
        xs.append(r["km"])
        ys.append(ys[-1] + r["dplus"] - r["dminus"])
    W, H, pad = 640.0, 118.0, 14.0
    x0, x1 = 0.0, xs[-1] or 1
    y0, y1 = min(ys), max(ys)
    span = (y1 - y0) or 1

    def X(v):
        return pad + (v - x0) / (x1 - x0) * (W - pad * 2)

    def Y(v):
        return H - pad - (v - y0) / span * (H - pad * 2)

    line = " ".join(f"{X(x):.1f},{Y(y):.1f}" for x, y in zip(xs, ys))
    area = f"{X(xs[0]):.1f},{H - pad:.1f} " + line + f" {X(xs[-1]):.1f},{H - pad:.1f}"
    marks = []
    for r, y in zip(rows, ys[1:]):
        cls = "bad" if r["margin_s"] < 0 else ("warn" if r["margin_s"] < 3600 else "ok")
        color = {"bad": "#E06A5F", "warn": "#D6A75A", "ok": "#4FD6A6"}[cls]
        marks.append(
            f'<circle cx="{X(r["km"]):.1f}" cy="{Y(y):.1f}" r="3.2" fill="{color}"/>'
            f'<text x="{X(r["km"]):.1f}" y="{Y(y) - 6:.1f}" fill="#7F8A97" '
            f'font-size="9" text-anchor="middle">{r["idx"]}</text>')
    return (
        f'<svg class="prof" viewBox="0 0 {W:.0f} {H:.0f}" preserveAspectRatio="none">'
        f'<polygon points="{area}" fill="rgba(79,214,166,.10)"/>'
        f'<polyline points="{line}" fill="none" stroke="#4FD6A6" stroke-width="2" '
        f'stroke-linejoin="round"/>' + "".join(marks) + "</svg>"
        f'<div class="sub2">相对高度（起点 = 0，纵轴 ±{int(span)} m）· '
        "形状来自官方分段 D+/D-，<b>不是实测地形</b>；圆点是各 CP，"
        "绿/黄/红对应该方案的关门余量。</div>")


def render_page(qs: dict | None = None) -> str:
    ev = E.load_event()
    course = E.load_course(ev["slug"])
    cd = E.countdown(ev)
    rows = E.plan_and_margin(ev["slug"])
    gear = E.gear_progress(ev["slug"])

    chips = (ui.chip(cd["status_cn"], "ok")
             + ui.chip(f'倒计时 {cd["days"]} 天')
             + ui.chip(f'目标 {cd["goal_h"]:.0f}h / 关门 {ev["duration_h"]}h'))

    # ---- 概览
    overview = (
        '<div class="card"><div class="kv">'
        f'<div><b>{ev["distance_km"]:.1f}<small>km</small></b><span>距离</span></div>'
        f'<div><b>+{ev["ascent_m"]:,}<small>m</small></b><span>累计爬升</span></div>'
        f'<div><b>{ev["duration_h"]}<small>h</small></b><span>总关门</span></div>'
        f'<div><b>{len(rows)}</b><span>CP 数</span></div>'
        "</div>"
        f'<div class="sub2">{ui.esc(ev["date"])} {ui.esc(ev["start_time"])} 起跑 · '
        f'{ui.esc(ev["venue"])}</div></div>')

    # ---- 轨迹
    stats = E._read(ev["slug"], "track_stats.json")
    if stats:
        cmp = E.track_compare(ev["slug"])
        ref = (course.get("reference") or {}).get("track") or {}
        edition = ref.get("edition") or ""
        d_km = (cmp.get("track_km") or 0) - ev["distance_km"]
        d_up = (cmp.get("track_ascent") or 0) - ev["ascent_m"]
        warn = (f'<div class="sub2 warnbox">轨迹与 2027 官方相差 '
                f'{d_km:+.1f} km / {d_up:+d} m'
                + ("；中间几段是改线区，<b>CP 里程与关门以海报为准</b>，轨迹只作地形参考"
                   if not cmp.get("same_course", True) else "") + "</div>")
        rows_cmp = []
        for x in (cmp.get("rows") or []):
            if x.get("km_gpx") is None:
                continue
            d = x["d_km"]
            cls = "bad" if abs(d) > 1.5 else ("warn" if abs(d) > 0.8 else "ok")
            rows_cmp.append(
                f'<div class="cmp"><span class="cpn">CP{x["idx"]}</span>'
                f'<span class="cpl">{ui.esc(x["name"])}</span>'
                f'<span class="cpk">{x["km_official"]:.1f}</span>'
                f'<span class="cpk">{x["km_gpx"]:.1f}</span>'
                f'<span class="cpm {cls}">{d:+.1f}</span></div>')
        cmp_tbl = ""
        if rows_cmp:
            cmp_tbl = ('<div class="cmphead"><span>CP</span><span>地点</span>'
                       "<span>官方</span><span>轨迹</span><span>Δ</span></div>"
                       + "".join(rows_cmp))
        track = (
            '<div class="card">'
            f'<div class="trk"><b>{stats["total_km"]} km</b>'
            f'<span>滤波爬升 +{stats["ascent_m"]:,} m / -{stats["descent_m"]:,} m</span>'
            f'<span>{stats["points"]} 点 · 海拔 {stats["min_ele"]}–{stats["max_ele"]} m</span>'
            + (f'<span class="tag">{edition} 届参考</span>' if edition else "")
            + "</div>" + warn + _spark(stats.get("profile") or [])
            + (('<div class="cmptitle">CP 里程对照（官方 vs 轨迹）</div>' + cmp_tbl)
               if cmp_tbl else "")
            + '<div class="pager"><div class="btn" data-act="gpx">换一条 GPX</div></div></div>')
    else:
        track = (
            '<div class="card"><div class="empty"><b>还没导入赛道轨迹</b>'
            "官方 GPX 从「柴古唐斯」小程序下载后，用下面的按钮选中即可，"
            "会自动算出滤波爬升与海拔剖面（GPX 是 3D 赛道视频的前提）。</div>"
            + _relief(rows)
            + '<div class="pager"><div class="btn primary" data-act="gpx">选择 GPX 文件</div>'
            "</div></div>")

    # ---- 余量表
    body_rows = []
    for r in rows:
        txt, cls = _fmt_margin(r["margin_s"])
        tag = " · 换装" if r["dropbag"] else ""
        body_rows.append(
            f'<div class="cp"><span class="cpn">CP{r["idx"]}</span>'
            f'<span class="cpl">{ui.esc(r["name"])}{tag}</span>'
            f'<span class="cpk">{r["km"]:.1f}</span>'
            f'<span class="cpc">{r["cutoff"].strftime("%d日%H:%M")}</span>'
            f'<span class="cpm {cls}">{txt}</span></div>')
    cps = ('<div class="card"><div class="cph"><span>CP</span><span>地点</span>'
           "<span>km</span><span>关门</span><span>余量</span></div>"
           + "".join(body_rows)
           + '<div class="sub2">余量按目标完赛时间分摊（前段抢速、夜段放慢），'
             "红色 = 这个方案会被关门。</div></div>")

    # ---- 装备：每条是一个表单，点了直接回写 gear.md（PRG 回跳，不依赖 JS）
    pct = int(gear["done"] / max(gear["total"], 1) * 100)
    rows = []
    for it in E.gear_items(ev["slug"]):
        nxt = "0" if it["done"] else "1"
        rows.append(
            '<form class="grow" method="post" action="/__event/gear">'
            f'<input type="hidden" name="idx" value="{it["idx"]}">'
            f'<input type="hidden" name="done" value="{nxt}">'
            f'<button class="gchk{" on" if it["done"] else ""}" type="submit"'
            f' aria-label="{"取消" if it["done"] else "勾选"}">{("✓" if it["done"] else "")}'
            "</button>"
            f'<span class="gtx{" on" if it["done"] else ""}">{ui.esc(it["text"])}</span>'
            "</form>")
    gears = (
        '<div class="card">'
        f'<div class="gw"><div class="gbar"><i style="width:{pct}%"></i></div>'
        f'<span>{gear["done"]}/{gear["total"]} 已备</span></div>'
        + ('<div class="glist">' + "".join(rows) + "</div>"
           if rows else '<div class="sub2">清单是空的。</div>')
        + '<div class="sub2">点一下勾选（写回 gear.md）；赛前 7 天没勾满会进看板'
          "「需要注意」。</div></div>")

    body = (CSS + ui.sec("赛事", overview)
            + ui.sec("赛道轨迹", track)
            + ui.sec("关门余量", cps)
            + ui.sec("强制装备", gears))

    return ui.page("EVENT · 赛事", "熊猫蜀道山 105K",
                   "赛前理解赛道：关门余量、轨迹剖面、装备进度",
                   body, chips=chips, back=("返回今日", "/"))


# ---------------------------------------------------------------- CLI

def cmd(args) -> int:
    """CLI 入口（`run.bat event`）：赛事档案 / 关门余量 / 装备 / GPX。

    逻辑放这里、cli.py 只留一层薄包装：入口文件有行数上限，
    新增功能不该继续往那里堆。
    """
    """赛事档案：CP 关门余量 / 目标完赛 / 装备清单 / GPX 导入。"""
    from garmin_cn import event as E

    slug = getattr(args, "slug", None)
    if getattr(args, "gpx", None):
        try:
            r = E.parse_gpx(args.gpx, slug=slug)
        except Exception as e:
            print(f"× GPX 解析失败：{e}")
            return 2
        ev = E.load_event(slug)
        print(f"√ 已导入 {ev['slug']}/track.gpx")
        print(f"  距离 {r['total_km']} km · 滤波爬升 +{r['ascent_m']} m / -{r['descent_m']} m"
              f" · 海拔 {r['min_ele']}–{r['max_ele']} m · {r['points']} 点")
        print(f"  剖面 {len(r['profile'])} 点（每 500 m）· 航点 {len(r['waypoints'])} 个")
        if abs(r["ascent_m"] - ev["ascent_m"]) > ev["ascent_m"] * 0.1:
            print("  ! 与官方累计爬升差 >10%，剖面走势可用、绝对值以官方为准")
        return 0

    if getattr(args, "gear", None):
        ev = E.load_event(slug)
        p = E._dir(ev["slug"]) / "gear.md"
        txt = p.read_text(encoding="utf-8")
        hit = 0
        for kw in args.gear:
            pat = re.compile(r"^(- \[)(x|X| )(\] *[^\n]*" + re.escape(kw) + r")", re.M)
            new, n = pat.subn(r"\1x\3", txt)
            if n:
                txt, hit = new, hit + n
                print(f"√ 已勾选：{kw}")
            else:
                print(f"× 没找到未勾选项：{kw}")
        p.write_text(txt, encoding="utf-8")
        g = E.gear_progress(ev["slug"])
        print(f"装备进度 {g['done']}/{g['total']}")
        return 0

    if getattr(args, "goal", None):
        goal_s = int(args.goal * 3600)
        ev = E.load_event(slug)
        E.set_goal(slug, goal_s)
        print(f"√ 目标完赛 {args.goal} h（{ev['name']}）")
        print()
        print(E.margins_report(slug, goal_s))
        return 0

    # 默认：档案概览 + 关门余量
    ev = E.load_event(slug)
    cd = E.countdown(ev)
    today = dt.date.today()
    race = dt.date.fromisoformat(ev["date"])
    print(f"{ev['name']}　[{E.STATUS_CN.get(ev['status'], ev['status'])}]")
    print(f"日期 {ev['date']} {ev['start_time']} 起跑 · {ev['venue']}")
    print(f"{ev['distance_km']} km / +{ev['ascent_m']:,} m · 总关门 {ev['duration_h']} h"
          f" · ITRA {ev.get('itra', '—')} · 倒计时 {(race - today).days} 天")
    print(f"目标 {ev.get('goal_s', 22*3600)/3600:.0f} h · 装备 {cd['gear_done']}/{cd['gear_total']}")
    print()
    print(E.margins_report(slug))
    return 0


def add_parser(sub):
    """把 `run.bat event` 的参数注册也收进来，cli.py 只留两行。"""
    q = sub.add_parser("event", help="赛事档案：关门余量表 / 目标 / 装备 / GPX")
    q.add_argument("slug", nargs="?", help="赛事目录名（默认 panda-105k-2027）")
    q.add_argument("--goal", type=float, metavar="H",
                   help="设置目标完赛小时数并查看余量")
    q.add_argument("--gear", nargs="+", metavar="关键词",
                   help="按关键词勾选装备清单（如 --gear 头灯 保温毯）")
    q.add_argument("--gpx", metavar="文件", help="导入官方 GPX 轨迹（滤波爬升 + 剖面）")
    q.set_defaults(func=cmd)
    return q
