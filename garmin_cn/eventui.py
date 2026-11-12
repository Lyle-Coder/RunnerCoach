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
/* 补给与换装 */
.ftot{font-size:12.5px;color:var(--muted,#7F8A97);line-height:1.8;
  padding-bottom:10px;border-bottom:1px solid rgba(255,255,255,.06)}
.ftot b{color:var(--fg,#E8F0FB);font-weight:600;font-variant-numeric:tabular-nums}
.frm{margin:10px 0 0}
.frow{display:flex;flex-wrap:wrap;gap:8px 10px;margin-bottom:8px}
/* 标签在上、输入在下：横排时 input 的 width:100% 会把标签挤成一个字一行 */
.frow label{display:flex;flex-direction:column;align-items:stretch;gap:3px;
  font-size:11px;color:var(--muted,#7F8A97);flex:1 1 86px;min-width:0}
.fi{flex:1 1 auto;min-width:0;width:100%;background:rgba(255,255,255,.05);
  border:1px solid rgba(127,138,151,.28);border-radius:7px;color:var(--fg,#E8F0FB);
  font-size:12px;padding:5px 7px;font-variant-numeric:tabular-nums}
.fi:focus{outline:none;border-color:#4FD6A6}
select.fi{appearance:none}
.fbtns{display:flex;align-items:center;gap:10px;margin-top:4px}
.fhint{font-size:11px;color:var(--muted,#7F8A97);line-height:1.4}
.fl{padding:9px 0;border-bottom:1px solid rgba(255,255,255,.05)}
.fl:last-of-type{border-bottom:0}
.flh{display:flex;align-items:baseline;gap:6px}
.fla{margin-left:auto;font-size:11px;color:var(--muted,#7F8A97);
  font-variant-numeric:tabular-nums}
.flb{font-size:11.5px;color:var(--muted,#7F8A97);line-height:1.7;margin-top:2px}
.flw{font-size:11.5px;color:#D6A75A;line-height:1.6;margin-top:2px}
.db{font-size:10px;color:#062018;background:#4FD6A6;border-radius:4px;
  padding:0 4px;font-weight:600}
.dbox{margin-top:12px;padding:10px 12px;border-radius:10px;
  background:rgba(79,214,166,.06);border:1px solid rgba(79,214,166,.22)}
.dbt{font-size:12px;color:#9FE7C8;font-weight:600;margin-bottom:6px}
.dbl{list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:4px}
.dbl li{font-size:11.5px;color:var(--fg,#E8F0FB);padding-left:12px;position:relative}
.dbl li:before{content:"";position:absolute;left:0;top:6px;width:5px;height:5px;
  border-radius:50%;background:#4FD6A6}
/* 调整 */
.fold{border-top:1px solid rgba(255,255,255,.06);padding:10px 0}
.fold:first-child{border-top:0;padding-top:0}
.fold summary{cursor:pointer;font-size:12.5px;color:var(--fg,#E8F0FB);
  list-style:none;display:flex;align-items:center;gap:6px}
.fold summary::-webkit-details-marker{display:none}
.fold summary:before{content:"▸";color:var(--muted,#7F8A97);font-size:11px}
.fold[open] summary:before{content:"▾"}
.cpe{display:grid;grid-template-columns:30px minmax(0,1fr) 62px 62px;gap:5px;
  align-items:center;padding:4px 0;border-bottom:1px solid rgba(255,255,255,.04)}
.cpe.head{color:var(--muted,#7F8A97);font-size:10.5px;border-bottom-color:rgba(255,255,255,.1)}
.cpe.head span:nth-child(3),.cpe.head span:nth-child(4){text-align:center}
.fi-km,.fi-cut{text-align:right;font-size:11.5px;padding:4px 5px}
/* 赛中 */
.rbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
  padding:10px 12px;border-radius:10px;margin-bottom:10px}
.rbar.pre{background:rgba(96,165,250,.08);border:1px solid rgba(96,165,250,.24)}
.rbar.live{background:rgba(79,214,166,.07);border:1px solid rgba(79,214,166,.24)}
.rbtxt{font-size:12px;color:var(--muted,#7F8A97);line-height:1.6;flex:1 1 100%}
.rbline{flex:1 1 100%;display:flex;flex-direction:column;gap:2px}
.rt{font-size:17px;font-weight:600;color:var(--fg,#E8F0FB);letter-spacing:-.3px;
  font-variant-numeric:tabular-nums}
.rd{font-size:11.5px;color:var(--muted,#7F8A97);font-variant-numeric:tabular-nums}
.rd b{font-weight:600}
.rd b.bad{color:#E06A5F}.rd b.ok{color:#4FD6A6}
.rinline{display:inline;margin:0}
.rbar .rinline{margin-left:auto}
.rbtn{padding:7px 16px;border-radius:9px;border:1px solid rgba(79,214,166,.55);
  background:rgba(79,214,166,.14);color:#9FE7C8;font-size:13px;font-weight:600;
  min-height:38px;cursor:pointer}
.rbtn:active{opacity:.8}
.rbtn2{padding:5px 11px;border-radius:8px;border:1px solid rgba(127,138,151,.35);
  background:transparent;color:var(--muted,#7F8A97);font-size:11.5px;cursor:pointer}
.rlist{margin-top:2px}
/* 左滑完成：动作层压在下面，前面板整体位移露出提示 */
.rrow{position:relative;overflow:hidden;border-bottom:1px solid rgba(255,255,255,.05)}
.rrow:last-child{border-bottom:0}
.rrow.done{opacity:.5}
.rbg{position:absolute;inset:0;display:flex;align-items:center;justify-content:flex-end;
  padding-right:22px;font-size:12.5px;font-weight:600;letter-spacing:.08em}
.rbg.pass{background:rgba(79,214,166,.22);color:#9FE7C8}
.rbg.unpass{background:rgba(224,106,95,.2);color:#F0A9A1}
.rface{position:relative;padding:8px 0;background:#111a26;
  transition:transform .18s ease;will-change:transform;touch-action:pan-y;
  -webkit-user-select:none;user-select:none;-webkit-tap-highlight-color:transparent}
.rline1{display:flex;align-items:center;gap:6px}
.rline1 .rbtn,.rline1 .rbtn2{margin-left:auto;flex:none}
.rline2{font-size:11.5px;color:var(--muted,#7F8A97);line-height:1.6;margin-top:1px}
.rpass{font-size:11.5px;color:#4FD6A6;font-variant-numeric:tabular-nums}
.rrow.done .rpass{color:var(--muted,#7F8A97)}
.errbar{margin:0 0 12px;padding:9px 12px;border-radius:9px;font-size:12px;
  background:rgba(224,106,95,.12);border:1px solid rgba(224,106,95,.38);color:#F0A9A1}
</style>"""


def _hm(sec: float) -> str:
    """秒 → 4h12m / 52m。赛中看用时，不需要秒。"""
    sec = int(abs(sec))
    h, m = divmod(sec // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


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


SWIPE_JS = """<script>
(function(){
  var TH = 54, MAX = 96;
  function send(act, idx){
    var f = document.createElement('form');
    f.method = 'post'; f.action = '/__event/race';
    [['act', act], ['idx', idx]].forEach(function(kv){
      var i = document.createElement('input');
      i.type = 'hidden'; i.name = kv[0]; i.value = kv[1];
      f.appendChild(i);
    });
    document.body.appendChild(f); f.submit();
  }
  var rows = document.querySelectorAll('.rrow[data-act]');
  Array.prototype.forEach.call(rows, function(row){
    var face = row.querySelector('.rface');
    if (!face) return;
    var sx = 0, sy = 0, dx = 0, axis = null, active = false;
    function start(x, y){ sx = x; sy = y; dx = 0; axis = null; active = true; }
    function move(x, y, e){
      if (!active) return;
      var ddx = x - sx, ddy = y - sy;
      if (!axis){
        if (Math.abs(ddx) < 8 && Math.abs(ddy) < 8) return;
        axis = Math.abs(ddx) > Math.abs(ddy) ? 'x' : 'y';
        if (axis === 'y'){ active = false; return; }   /* 竖滑交给页面滚动 */
      }
      if (e && e.cancelable) e.preventDefault();
      dx = Math.max(-MAX, Math.min(0, ddx));
      face.style.transition = 'none';
      face.style.transform = 'translateX(' + dx + 'px)';
    }
    function end(){
      if (!active) return;
      active = false;
      if (dx <= -TH){
        face.style.transition = 'transform .14s ease';
        face.style.transform = 'translateX(' + (-MAX) + 'px)';
        setTimeout(function(){ send(row.dataset.act, row.dataset.idx); }, 120);
      } else {
        face.style.transition = 'transform .16s ease';
        face.style.transform = 'translateX(0)';
      }
    }
    row.addEventListener('touchstart', function(e){
      var t = e.touches[0]; start(t.clientX, t.clientY);
    }, {passive: true});
    row.addEventListener('touchmove', function(e){
      var t = e.touches[0]; move(t.clientX, t.clientY, e);
    }, {passive: false});
    row.addEventListener('touchend', end);
    row.addEventListener('touchcancel', end);
    row.addEventListener('mousedown', function(e){
      if (e.button !== 0) return;
      start(e.clientX, e.clientY);
      function mm(ev){ move(ev.clientX, ev.clientY, ev); }
      function mu(){
        document.removeEventListener('mousemove', mm);
        document.removeEventListener('mouseup', mu);
        end();
      }
      document.addEventListener('mousemove', mm);
      document.addEventListener('mouseup', mu);
    });
  });
})();
</script>"""


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

    # ---- 赛中：点一下已过 CP，后面全部按实际时间重算
    # ?now=2027-03-27T15:00 只在调试/预览时用，不写任何状态
    _now = None
    try:
        _raw = (qs or {}).get("now", [""])[0]
        if _raw:
            _now = dt.datetime.fromisoformat(_raw)
    except Exception:
        _now = None
    race = E.race_projection(ev["slug"], now=_now)
    st = race["state"]
    if st == "pre":
        bar = ('<div class="rbar pre"><div class="rbtxt">还没开始。起跑时点「开始」，'
               '之后每到一个 CP，<b>把那一行向左滑</b>，时间自动记下、后面全部重算。</div>'
               '<form method="post" action="/__event/race" class="rinline">'
               '<input type="hidden" name="act" value="start">'
               '<button class="btn primary rbtn" type="submit">开始比赛</button></form></div>')
    else:
        el = _hm(race["elapsed_s"])
        d = int(race["delay_s"])
        d_txt = ("落后 %s" % _hm(abs(d))) if d > 0 else ("领先 %s" % _hm(abs(d)) if d < 0 else "与计划持平")
        d_cls = "bad" if d > 0 else ("ok" if d < 0 else "")
        fin = race.get("finished")
        if fin:
            total = (dt.datetime.fromisoformat(fin) - race["start"]).total_seconds()
            head = (f'<span class="rt">已完赛 {_hm(total)}</span>'
                    f'<span class="rd">起点 {race["start"].strftime("%d日%H:%M")} · 终点 '
                    f'{dt.datetime.fromisoformat(fin).strftime("%d日%H:%M")}</span>')
        else:
            rf = race["req_factor"]
            if rf is None:
                need = '<span class="cpm bad">目标已保不住，按关门跑</span>'
            elif rf > 1.05:
                need = (f'<span class="cpm warn">保 {ev["goal_s"]//3600}h 需提速 {int((rf-1)*100)}%</span>')
            else:
                need = f'<span class="cpm ok">按现节奏可保目标（需 {rf:.2f}× 配速）</span>'
            head = (f'<span class="rt">已用时 {el}</span>'
                    f'<span class="rd">已过 {race["passed_count"]}/{race["total"]} · '
                    f'<b class="{d_cls}">{d_txt}</b></span>')
        warn_html = (f'<div class="flw">⚠ {ui.esc(race["warn"])}</div>'
                     if race.get("warn") else "")
        bar = ('<div class="rbar live">'
               f'<div class="rbline">{head}{need if not fin else ""}{warn_html}</div>'
               '<form method="post" action="/__event/race" class="rinline" '
               'onsubmit="return confirm(&quot;确定重置赛况？已记录的到达会清空&quot;)">'
               '<input type="hidden" name="act" value="reset">'
               '<button class="rbtn2" type="submit">重置</button></form></div>')

    cps_rows = []
    for r in race["rows"]:
        done = r["done"]
        m = int(r["margin_s"])
        mtxt, mcls = _fmt_margin(m)
        if done:
            delta = int(r["delta_s"])
            dtag = ("慢 %s" % _hm(abs(delta))) if delta > 0 else (
                "快 %s" % _hm(abs(delta)) if delta < 0 else "准时")
            first = (f'<span class="cpn">CP{r["idx"]}</span>'
                     f'<span class="cpl">{ui.esc(r["name"])}</span>'
                     f'<span class="rpass">已过 {r["actual"].strftime("%H:%M")}</span>')
            second = (f'计划 {r["planned"].strftime("%H:%M")} · {dtag} · '
                      f'关门 {r["cutoff"].strftime("%d日%H:%M")} · '
                      f'余量 <b class="cpm {mcls}">{mtxt}</b>')
            act, hint = "unpass", "撤销"
        else:
            first = (f'<span class="cpn">CP{r["idx"]}</span>'
                     f'<span class="cpl">{ui.esc(r["name"])}'
                     + (' <b class="db">换装</b>' if r["dropbag"] else "") + "</span>")
            proj = r["projected"].strftime("%H:%M")
            second = (f'计划 {r["planned"].strftime("%H:%M")} · 预测 {proj} · '
                      f'关门 {r["cutoff"].strftime("%d日%H:%M")} · '
                      f'余量 <b class="cpm {mcls}">{mtxt}</b>')
            act, hint = "pass", "完成"
        # 左滑完成：行本身就是按钮，不再占一条 44px 高的按钮
        cps_rows.append(
            f'<div class="rrow{" done" if done else ""}" data-act="{act}" data-idx="{r["idx"]}">'
            f'<div class="rbg {act}">{hint}</div>'
            f'<div class="rface"><div class="rline1">{first}</div>'
            f'<div class="rline2">{second}</div></div></div>')

    cps = ('<div class="card">' + bar
           + '<div class="rlist">' + "".join(cps_rows) + "</div>"
           + '<div class="sub2">到 CP 时<b>向左滑动</b>该行即记为已到达（记当前时间），'
             "已过的行变灰；再左滑一次可撤销。预测列按最后一个已过 CP 的落后量平移。</div></div>")

    # ---- 装备：每条是一个表单，点了直接回写 gear.md（PRG 回跳，不依赖 JS）
    pct = int(gear["done"] / max(gear["total"], 1) * 100)
    gear_rows = []
    for it in E.gear_items(ev["slug"]):
        nxt = "0" if it["done"] else "1"
        gear_rows.append(
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
        + ('<div class="glist">' + "".join(gear_rows) + "</div>"
           if gear_rows else '<div class="sub2">清单是空的。</div>')
        + '<div class="sub2">点一下勾选（写回 gear.md）；赛前 7 天没勾满会进看板'
          "「需要注意」。</div></div>")

    # ---- 补给与换装（按分段时间算，参数可改）
    fuel = E.fuel_plan(ev["slug"])
    cfg = fuel["cfg"]
    tot = fuel["totals"]
    fuel_rows = []
    for r in fuel["rows"]:
        tag = ' <b class="db">换装</b>' if r["dropbag"] else ""
        moon = " 🌙" if r["night"] else ""
        warn = ("".join('<div class="flw">⚠ %s</div>' % ui.esc(w) for w in r["warn"]))
        fuel_rows.append(
            '<div class="fl">'
            f'<div class="flh"><span class="cpn">CP{r["idx"]}</span>'
            f'<span class="cpl">{ui.esc(r["name"])}{tag}{moon}</span>'
            f'<span class="fla">{r["arrive"].strftime("%d日%H:%M")}</span></div>'
            f'<div class="flb">段 {r["leg_h"]:.1f} h｜碳水 {r["carb_g"]} g'
            f'（胶 {r["gels"]} 支 / 站内 {r["carb_station_g"]} g）｜'
            f'水 {r["carry_ml"]} ml｜钠 {r["sodium_mg"]} mg｜停 {r["stop_min"]} min</div>'
            + warn + "</div>")

    dbg = next((r for r in fuel["rows"] if r["dropbag"]), None)
    drop = ""
    if dbg:
        drop = (
            '<div class="dbox"><div class="dbt">换装点 · CP%d %s（%s 到，停 %d min）</div>'
            '<ul class="dbl">'
            "<li>换鞋 + 换袜（干袜一定带）</li>"
            "<li>换上衣、加保暖层（夜段开始前）</li>"
            "<li>补电解质 / 盐丸，水袋加满</li>"
            "<li>头灯换新电池，备用电池塞包里</li>"
            "<li>丢掉的湿衣物装袋交给志愿者</li>"
            "</ul></div>" % (dbg["idx"], ui.esc(dbg["name"]),
                             dbg["arrive"].strftime("%d日%H:%M"), dbg["stop_min"]))

    fuel_form = (
        '<form class="frm" method="post" action="/__event/fuel">'
        '<div class="frow"><label>碳水 g/h<input class="fi" name="carb_g_h" '
        f'type="number" step="5" min="30" max="120" value="{cfg["carb_g_h"]}"></label>'
        '<label>水 ml/h<input class="fi" name="fluid_ml_h" type="number" step="50" '
        f'min="200" max="1000" value="{cfg["fluid_ml_h"]}"></label>'
        '<label>钠 mg/h<input class="fi" name="sodium_mg_h" type="number" step="50" '
        f'min="200" max="1500" value="{cfg["sodium_mg_h"]}"></label></div>'
        '<div class="frow"><label>自带比例<input class="fi" name="carry_ratio" '
        f'type="number" step="0.05" min="0.2" max="1" value="{cfg["carry_ratio"]}"></label>'
        '<label>携水 ml<input class="fi" name="carry_ml" type="number" step="100" '
        f'min="500" max="3000" value="{cfg["carry_ml"]}"></label>'
        '<label>停留 min<input class="fi" name="stop_min" type="number" step="1" '
        f'min="0" max="30" value="{cfg["stop_min"]}"></label>'
        '<label>换装停留<input class="fi" name="dropbag_stop_min" type="number" '
        f'step="1" min="0" max="60" value="{cfg["dropbag_stop_min"]}"></label></div>'
        '<div class="fbtns"><button class="btn primary" type="submit">保存并重算</button>'
        '<span class="fhint">碳水含胶与能量棒；总时长按目标配速推</span></div>'
        "</form>")

    fuel_card = (
        '<div class="card">'
        f'<div class="ftot">{tot["hours"]:.1f} h · 碳水 <b>{tot["carb_g"]:,} g</b> · '
        f'水 <b>{tot["fluid_l"]:.1f} L</b> · 钠 <b>{tot["sodium_mg"]/1000:.1f} g</b> · '
        f'自带胶 <b>{tot["gels"]} 支</b></div>'
        + fuel_form + "".join(fuel_rows) + drop + "</div>")

    # ---- 调整（目标 / 状态 / CP 表）：折叠，平时不占屏
    status_opts = "".join(
        f'<option value="{k}"{" selected" if ev.get("status") == k else ""}>{ui.esc(v)}</option>'
        for k, v in E.STATUS_CN.items())
    cp_rows = []
    for r in rows:
        seg = next(x for x in course["segments"] if x["idx"] == r["idx"])
        cp_rows.append(
            f'<div class="cpe"><span class="cpn">CP{r["idx"]}</span>'
            f'<span class="cpl">{ui.esc(r["name"])}</span>'
            f'<input class="fi fi-km" name="km_{r["idx"]}" value="{seg["km"]:.1f}" '
            f'inputmode="decimal" aria-label="CP{r["idx"]} 里程">'
            f'<input class="fi fi-cut" name="cut_{r["idx"]}" value="{seg["cutoff"]}" '
            f'inputmode="numeric" aria-label="CP{r["idx"]} 关门"></div>')

    edit_card = (
        '<div class="card">'
        '<details class="fold"><summary>调整目标、状态与起跑时间</summary>'
        '<form class="frm" method="post" action="/__event/target">'
        '<div class="frow"><label>目标完赛<input class="fi" name="goal_h" type="number" '
        f'step="0.5" min="10" max="34" value="{ev["goal_s"]/3600:g}"></label>'
        f'<label>状态<select class="fi" name="status">{status_opts}</select></label>'
        '<label>起跑<input class="fi" name="start_time" value="'
        f'{ui.esc(ev["start_time"])}"></label></div>'
        '<div class="fbtns"><button class="btn primary" type="submit">保存</button>'
        '<span class="fhint">改目标会立刻重算余量与补给</span></div></form></details>'
        '<details class="fold"><summary>CP 表（里程 / 关门）</summary>'
        '<form class="frm" method="post" action="/__event/course">'
        '<div class="cpe head"><span>CP</span><span>地点</span><span>km</span><span>关门</span></div>'
        + "".join(cp_rows) +
        '<div class="fbtns"><button class="btn primary" type="submit">保存 CP 表</button>'
        '<span class="fhint">里程必须递增，否则整表不保存</span></div></form></details>'
        "</div>")

    err = (qs or {}).get("err", [""])[0] if qs else ""
    err_bar = (f'<div class="errbar">保存失败：{ui.esc(err)}</div>' if err else "")

    body = (CSS + err_bar + ui.sec("赛事", overview)
            + ui.sec("赛道轨迹", track)
            + ui.sec("赛况与余量", cps)
            + ui.sec("补给与换装", fuel_card)
            + ui.sec("强制装备", gears)
            + ui.sec("调整", edit_card))

    return ui.page("EVENT · 赛事", "熊猫蜀道山 105K",
                   "赛前理解赛道：关门余量、轨迹剖面、装备进度",
                   body + SWIPE_JS, chips=chips, back=("返回今日", "/"))


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
