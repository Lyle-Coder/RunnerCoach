# -*- coding: utf-8 -*-
"""赛事页（/event）：赛事档案 + 关门余量表 + 装备 + 轨迹导入。

一页回答三件事：
  1) 这场比赛长什么样（距离 / 爬升 / 关门 / 倒计时）
  2) 每个 CP 我还有多少关门余量，哪个最紧
  3) 装备备了几件、路线文件导进来了没有（GPX 可从 App 里直接选文件）
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import re
import urllib.parse as _up

from . import event as E
from . import ui

# 只在赛事页生效的样式，跟着页面走，不去动全局 ui.CSS
CSS = """<style>
.kv{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;text-align:center}
.kv b{display:block;font-size:16px;color:var(--ink);letter-spacing:-.4px}
.kv b small{font-size:11px;font-weight:400;color:var(--ink-2);margin-left:1px}
.kv span{display:block;font-size:11px;color:var(--ink-2);margin-top:2px}
.sub2{font-size:12px;color:var(--ink-2);line-height:1.7;margin-top:10px}
.trk{display:flex;flex-wrap:wrap;gap:4px 12px;align-items:baseline;margin-bottom:8px}
.trk b{font-size:16px;color:var(--ink)}
.trk span{font-size:12px;color:var(--ink-2)}
.prof{width:100%;max-width:100%;height:96px;display:block;margin:6px 0 2px;
  background:var(--ok-soft);border-radius:10px}
.tag{font-size:11px;color:var(--ink-3);border:1px solid var(--line);
  border-radius:5px;padding:1px 5px}
.warnbox{color:var(--warn)}
.hms{display:inline-flex;align-items:center;gap:3px}
.hms input{width:54px;text-align:center;flex:0 0 auto}
.cmptitle{font-size:12px;color:var(--ink-2);margin:10px 0 2px}
.cmphead,.cmp{display:grid;grid-template-columns:30px minmax(0,1fr) 42px 42px 42px;
  gap:4px;align-items:center;font-size:12px;padding:5px 0;
  border-bottom:1px solid var(--surface-2)}
.cmphead{color:var(--ink-2);font-size:11px;padding-top:0}
.cph,.cp{display:grid;grid-template-columns:32px minmax(0,1fr) 36px 64px 56px;
  gap:4px;align-items:center;font-size:12px;padding:7px 0;
  border-bottom:1px solid var(--line)}
.cph{color:var(--ink-2);font-size:11px;border-bottom-color:var(--line)}
.cp:last-of-type{border-bottom:0}
.cpn{color:var(--ink-2);font-size:11px}
.cpl{color:var(--ink);min-width:0;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.cpk,.cpc{color:var(--ink-2);text-align:right;font-variant-numeric:tabular-nums;
  white-space:nowrap}
.cpm{text-align:right;font-weight:600;font-variant-numeric:tabular-nums}
.cpm.ok{color:var(--ok)}.cpm.warn{color:var(--warn)}.cpm.bad{color:var(--bad)}
.gw{display:flex;align-items:center;gap:10px}
.gbar{flex:1;height:8px;border-radius:5px;background:var(--line);overflow:hidden}
.gbar i{display:block;height:100%;background:linear-gradient(90deg,var(--brand),var(--cyan))}
.gw span{font-size:12px;color:var(--ink-2);flex:none}
.glist{margin:12px 0 0;display:flex;flex-direction:column}
.grow{display:flex;align-items:flex-start;gap:9px;padding:7px 0;
  border-bottom:1px solid var(--line);margin:0}
.grow:last-child{border-bottom:0}
.gchk{flex:none;width:20px;height:20px;margin:1px 0 0;padding:0;border-radius:6px;
  border:1.5px solid rgba(127,138,151,.55);background:transparent;color:#070B12;
  font-size:14px;line-height:1;font-weight:600;cursor:pointer;
  display:flex;align-items:center;justify-content:center}
.gchk.on{background:var(--ok);border-color:var(--ok);color:var(--bg)}
.gtx{font-size:12px;line-height:1.55;color:var(--ink)}
.gtx.on{color:var(--ink-2);text-decoration:line-through}
/* 补给与换装 */
.ftot{font-size:12px;color:var(--ink-2);line-height:1.8;
  padding-bottom:10px;border-bottom:1px solid var(--line)}
.ftot b{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}
.frm{margin:10px 0 0}
.frow{display:flex;flex-wrap:wrap;gap:8px 10px;margin-bottom:8px}
/* 标签在上、输入在下：横排时 input 的 width:100% 会把标签挤成一个字一行 */
.frow label{display:flex;flex-direction:column;align-items:stretch;gap:3px;
  font-size:11px;color:var(--ink-2);flex:1 1 0;min-width:0}
/* 一个 label 里塞了多个输入（时:分:秒）时必须独占一行：
   三个 54px 的框 + 冒号 ≈ 180px，按 1/3 平分只有 ~110px，会溢出盖住
   右边的「状态」下拉。 */
.frow label.fwide{flex:1 1 100%}
.frow label.fwide .hms{width:100%}
.frow label.fwide .hms input{flex:1 1 0;width:auto;max-width:96px}
.fi{flex:1 1 auto;min-width:0;width:100%;background:var(--line);
  border:1px solid var(--line);border-radius:7px;color:var(--ink);
  font-size:12px;padding:5px 7px;font-variant-numeric:tabular-nums}
.fi:focus{outline:none;border-color:var(--ok)}
select.fi{cursor:pointer}
.fbtns{display:flex;align-items:center;gap:10px;margin-top:4px}
.fhint{font-size:11px;color:var(--ink-2);line-height:1.4}
.fl{padding:9px 0;border-bottom:1px solid var(--line)}
.fl:last-of-type{border-bottom:0}
.flh{display:flex;align-items:baseline;gap:6px}
.fla{margin-left:auto;font-size:11px;color:var(--ink-2);
  font-variant-numeric:tabular-nums}
.flb{font-size:12px;color:var(--ink-2);line-height:1.7;margin-top:2px}
.flw{font-size:12px;color:var(--warn);line-height:1.6;margin-top:2px}
.db{font-size:11px;color:var(--bg);background:var(--ok);border-radius:4px;
  padding:0 4px;font-weight:600}
.dbox{margin-top:12px;padding:10px 12px;border-radius:10px;
  background:var(--ok-soft);border:1px solid var(--ok-soft)}
.dbt{font-size:12px;color:var(--ok);font-weight:600;margin-bottom:6px}
.dbl{list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:4px}
.dbl li{font-size:12px;color:var(--ink);padding-left:12px;position:relative}
.dbl li:before{content:"";position:absolute;left:0;top:6px;width:5px;height:5px;
  border-radius:50%;background:var(--ok)}
.prov{font-size:12px;color:var(--ink-2);line-height:1.7;
  padding-bottom:9px;margin-bottom:2px;border-bottom:1px solid var(--line)}
.prov b{color:var(--ink);font-weight:600}
.prov b.warn{color:var(--warn)}

/* 资料 */
.dg{font-size:11px;color:var(--ink-2);letter-spacing:.04em;
  margin:12px 0 2px;padding-bottom:4px;border-bottom:1px solid var(--line)}
.dg:first-of-type{margin-top:6px}
.dr{padding:8px 0;border-bottom:1px solid var(--surface-2)}
.dr:last-child{border-bottom:0}
.drt{display:flex;align-items:center;gap:8px}
.drt b{font-size:12px;color:var(--ink);font-weight:600}
.dat{margin-left:auto;font-size:11px;color:var(--ink-2)}
/* 删除按钮：视觉上是小字「删」，命中区给到 44×44（手指按得准），
   多出来的高度用负 margin 吃掉，行高不受影响。 */
.dbtn{flex:none;min-width:44px;min-height:44px;padding:0;border-radius:10px;
  cursor:pointer;border:1px solid rgba(224,106,95,.35);background:transparent;
  color:var(--bad);font-size:12px;line-height:1;margin:-10px -4px -10px 0}
.dinline{margin:0}
.drb{font-size:12px;color:var(--ink-2);line-height:1.75;margin-top:3px;
  word-break:break-word}
.dlink{color:var(--ok);font-size:12px;word-break:break-all}
.dfl{display:flex;align-items:center;gap:8px;padding:7px 0;
  border-bottom:1px solid var(--surface-2)}
.dfl:last-child{border-bottom:0}
.dfn{font-size:12px;color:var(--ink);min-width:0;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.dfs{font-size:11px;color:var(--ink-2);flex:none}
.dfb{margin-left:auto;flex:none;padding:4px 12px;border-radius:10px;cursor:pointer;
  border:1px solid var(--ok);background:var(--ok-soft);
  color:var(--ok);font-size:12px}
/* 附件行：名字 + 预览/系统打开 + 改名折叠 */
.df{padding:7px 0;border-bottom:1px solid var(--surface-2)}
.df:last-child{border-bottom:0}
.df .dfl{padding:0;border:0}
.dfa{display:flex;gap:6px;flex:none;margin-left:8px}
.dfa .dfb{margin-left:0}
/* 附件里的删除：和「预览 / 系统打开」同排但红一档，误点代价高 */
.dfa .dfb.danger{color:var(--bad);border-color:rgba(224,106,95,.42);
  background:transparent}
.dfa .dfb.danger:active{background:var(--bad-soft)}
/* 行程与预订：一行一件事，左边标签、中间倒计时、右边日期与出处 */
.trip{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 8px;padding:7px 0;
  border-top:1px solid var(--line)}
.trip:first-of-type{border-top:0}
.tk{flex:none;font-size:11px;padding:1px 7px;border-radius:999px;
  border:1px solid rgba(127,138,151,.3);color:var(--ink-2);line-height:1.6}
.tk.flight{color:#8CC2FF;border-color:rgba(140,194,255,.4)}
.tk.train{color:var(--ok);border-color:var(--ok-soft)}
.tk.stay{color:var(--warn);border-color:rgba(214,167,90,.4)}
.tk.race{color:var(--bad);border-color:rgba(224,106,95,.4)}
.td{flex:none;font-size:11px;color:var(--ink-2);
  font-variant-numeric:tabular-nums}
.td.warn{color:var(--warn)}
.td.hot{color:var(--bad);font-weight:600}
.tt{flex:1 1 55%;min-width:0;font-size:12px;color:var(--ink)}
.tt b{font-variant-numeric:tabular-nums;margin-right:4px}
.ts{margin-left:6px;font-size:11px;color:var(--faint,var(--ink-3))}
.tplace{display:block;margin-top:3px;font-size:11px;color:var(--faint,var(--ink-3))}
.tnav,.tdel{flex:none;font-size:11px;padding:3px 9px;border-radius:6px;
  border:1px solid var(--ok-soft);color:var(--ok);text-decoration:none;
  background:transparent;line-height:1.5;cursor:pointer}
.tdel{border-color:var(--line);color:var(--ink-2)}
.tinline{display:inline}
.tgeo{margin-top:6px}
.tgeo .fi{min-width:0}
/* 预订待办：过了截止日也留着，最该催的恰恰是这一条 */
.bk{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 8px;padding:7px 0;
  border-top:1px solid var(--line)}
.bk:first-of-type{border-top:0}
.bk.done{opacity:.52}
.bk .bt{flex:1 1 55%;min-width:0;font-size:12px;color:var(--ink)}
.bk .bd{display:block;margin:2px 0 0;font-size:11px;color:var(--faint,var(--ink-3))}
.bkrule{display:flex;flex-wrap:wrap;gap:8px}
.bkrule label{flex:1 1 90px;font-size:11px;color:var(--ink-2)}
.bkrule .fi{width:100%}
/* 俯视图：SVG 是 720×360 的定尺，宽度拉满、高度按比例 */
.cmap{margin-top:10px}
.cmap svg{display:block;width:100%;height:auto;border-radius:10px}
.maplg{display:flex;flex-wrap:wrap;gap:12px;margin:5px 0 2px;
  font-size:11px;color:var(--ink-2)}
.maplg i{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px}
.maplg .s i{background:var(--ok)}.maplg .e i{background:#E05555}
.maplg .h i{background:var(--warn)}
.cmapnav{margin-top:8px}
.cmapnav a.btn{text-decoration:none;padding:0 13px;font-size:12px;min-height:40px}
.dtimes{display:flex;flex-wrap:wrap;gap:6px;margin-top:5px}
.dtime{font-size:11px;padding:2px 8px;border-radius:999px;color:var(--ok);
  background:var(--ok-soft);border:1px solid var(--ok)}
.dmore{margin-top:6px}
.dmore>summary{font-size:12px;color:var(--ink-2);cursor:pointer;
  list-style:none;display:inline-block}
.dmore>summary::-webkit-details-marker{display:none}
.dmore>summary::before{content:"✎ "}
.dpre{white-space:pre-wrap;word-break:break-word;font-size:12px;
  line-height:1.7;color:var(--ink);margin:0}

.dbig{padding:24px 12px;text-align:center;color:var(--ink-2);font-size:12px}
.hint code{background:var(--line);border-radius:4px;padding:0 4px;
  font-size:11px}

/* 赛后复盘 */
.rvs{font-size:14px;color:var(--ink);font-weight:600;line-height:1.6;
  padding-bottom:8px;margin-bottom:8px;border-bottom:1px solid var(--line);
  font-variant-numeric:tabular-nums}
.rvl{font-size:12px;color:var(--ink-2);line-height:1.7;margin-bottom:2px}
.rvlist{margin-top:10px}
.rvr{padding:7px 0;border-bottom:1px solid var(--line)}
.rvr:last-child{border-bottom:0}
.rv1{display:flex;align-items:baseline;gap:6px}
.rv1 .cpm{margin-left:auto;font-weight:600}
.rv2{font-size:12px;color:var(--ink-2);line-height:1.6;margin-top:1px}

/* 调整 */
.fold{border-top:1px solid var(--line);padding:10px 0}
.fold:first-child{border-top:0;padding-top:0}
.fold summary{cursor:pointer;font-size:12px;color:var(--ink);
  list-style:none;display:flex;align-items:center;gap:6px}
.fold summary::-webkit-details-marker{display:none}
.fold summary:before{content:"▸";color:var(--ink-2);font-size:11px}
.fold[open] summary:before{content:"▾"}
.cpe{display:grid;grid-template-columns:30px minmax(0,1fr) 62px 62px;gap:5px;
  align-items:center;padding:4px 0;border-bottom:1px solid var(--surface-2)}
.cpe.head{color:var(--ink-2);font-size:11px;border-bottom-color:rgba(255,255,255,.1)}
.cpe.head span:nth-child(3),.cpe.head span:nth-child(4){text-align:center}
.fi-km,.fi-cut{text-align:right;font-size:12px;padding:4px 5px}
/* 赛事切换条：横滑一行，当前高亮 */
.evbar{display:flex;gap:8px;overflow-x:auto;padding:2px 0 12px;
  -webkit-overflow-scrolling:touch;scrollbar-width:none}
.evbar::-webkit-scrollbar{display:none}
.ev{flex:none;display:flex;flex-direction:column;gap:1px;text-decoration:none;
  padding:7px 11px;border-radius:10px;background:var(--surface-2);
  border:1px solid rgba(127,138,151,.22)}
.ev b{font-size:12px;font-weight:600;color:var(--ink-2)}
.ev span{font-size:11px;color:var(--ink-2);opacity:.8}
.ev.on{background:var(--ok-soft);border-color:var(--ok)}
.ev.on b{color:var(--ok)}
.ev.on span{color:var(--ok);opacity:1}

/* 赛中 */
.rbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
  padding:10px 12px;border-radius:10px;margin-bottom:10px}
.rbar.pre{background:var(--brand-soft);border:1px solid var(--line-brand)}
.rbar.live{background:var(--ok-soft);border:1px solid var(--ok-soft)}
.rbtxt{font-size:12px;color:var(--ink-2);line-height:1.6;flex:1 1 100%}
.rbline{flex:1 1 100%;display:flex;flex-direction:column;gap:2px}
.rt{font-size:16px;font-weight:600;color:var(--ink);letter-spacing:-.3px;
  font-variant-numeric:tabular-nums}
.rd{font-size:12px;color:var(--ink-2);font-variant-numeric:tabular-nums}
.rd b{font-weight:600}
.rd b.bad{color:var(--bad)}.rd b.ok{color:var(--ok)}
.rinline{display:inline;margin:0}
.rbar .rinline{margin-left:auto}
.rbtn{padding:7px 16px;border-radius:10px;border:1px solid var(--ok);
  background:var(--ok-soft);color:var(--ok);font-size:14px;font-weight:600;
  min-height:38px;cursor:pointer}
.rbtn:active{opacity:.8}
.rbtn2{padding:5px 11px;border-radius:10px;border:1px solid var(--line);
  background:transparent;color:var(--ink-2);font-size:12px;cursor:pointer}
.rlist{margin-top:2px}
/* 左滑完成：动作层压在下面，前面板整体位移露出提示 */
.rrow{position:relative;overflow:hidden;border-bottom:1px solid var(--line)}
.rrow:last-child{border-bottom:0}
.rrow.done{opacity:.5}
.rbg{position:absolute;inset:0;display:flex;align-items:center;justify-content:flex-end;
  padding-right:22px;font-size:12px;font-weight:600;letter-spacing:.08em}
.rbg.pass{background:var(--ok-soft);color:var(--ok)}
.rbg.unpass{background:rgba(224,106,95,.2);color:var(--bad)}
.rface{position:relative;padding:8px 0;background:#111a26;
  transition:transform .18s ease;will-change:transform;touch-action:pan-y;
  -webkit-user-select:none;user-select:none;-webkit-tap-highlight-color:transparent}
.rline1{display:flex;align-items:center;gap:6px}
.rline1 .rbtn,.rline1 .rbtn2{margin-left:auto;flex:none}
.rline2{font-size:12px;color:var(--ink-2);line-height:1.6;margin-top:1px}
.rpass{font-size:12px;color:var(--ok);font-variant-numeric:tabular-nums}
.rrow.done .rpass{color:var(--ink-2)}
.okbar{margin:0 0 12px;padding:9px 12px;border-radius:10px;font-size:12px;
  background:var(--ok-soft);border:1px solid var(--ok-soft);color:var(--ok)}
.hint{font-size:12px;color:var(--ink-2);line-height:1.75;margin:0 0 10px}
.ta{width:100%;min-height:118px;background:var(--line);border-radius:10px;
  border:1px solid var(--line);color:var(--ink);font-size:12px;
  padding:8px 10px;line-height:1.7;resize:vertical;font-family:ui-monospace,Menlo,monospace}
.ta:focus{outline:none;border-color:var(--ok)}
.ev.add{justify-content:center;border-style:dashed;opacity:.85}
.ev.add b{font-size:14px}
.errbar{margin:0 0 12px;padding:9px 12px;border-radius:10px;font-size:12px;
  background:var(--bad-soft);border:1px solid rgba(224,106,95,.38);color:var(--bad)}
</style>"""


def _day_hm(t, ref) -> str:
    """实际时刻：和计划同一天只写 HH:MM，跨日才写「27日07:38」。

    105K 这种跨零点的比赛里，一半的行会跨日，混着写看起来像两套格式。
    """
    if not t:
        return "—"
    return t.strftime("%H:%M") if t.date() == ref.date() else t.strftime("%d日%H:%M")


def _hm(sec: float) -> str:
    """秒 → 4h12m / 52m。赛中看用时，不需要秒。"""
    sec = int(abs(sec))
    h, m = divmod(sec // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _fmt_margin(s: float | None) -> tuple[str, str]:
    """余量 → (文案, 颜色类)。负值红色，1 h 内黄色，其余绿色；无官方关门返回「—」。"""
    if s is None:
        return "—", ""
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


def _spark(profile: list, tick_km: float = 0) -> str:
    """剖面 sparkline（纯 SVG，不依赖外部图表库）。

    `tick_km` 给了就每 N 公里画一条竖虚线和里程数字。马拉松的 GPX 通常
    只有路线、没有途经点，剖面就成了一条没刻度的曲线——加上 5 km 刻度
    才看得出「30 km 之后那道长坡在哪」。
    """
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

    grid = ""
    if tick_km and tick_km > 0 and x1 > tick_km:
        k = tick_km
        marks = []
        while k < x1 - 0.05:
            px = (k - x0) / ((x1 - x0) or 1) * W
            marks.append(
                f'<line x1="{px:.1f}" y1="0" x2="{px:.1f}" y2="{H - 2:.0f}" '
                f'stroke="#223349" stroke-width="1" stroke-dasharray="3 4"/>'
                f'<text x="{px + 2:.1f}" y="{H - 3:.0f}" fill="#5B6775" '
                f'font-size="9.5">{k:g}</text>')
            k += tick_km
        grid = "".join(marks)

    return (f'<svg class="prof" viewBox="0 0 {W:.0f} {H:.0f}" '
            f'preserveAspectRatio="none">'
            + grid +
            f'<polyline points="{" ".join(pts)}" fill="none" stroke="var(--ok)" '
            f'stroke-width="2.2" stroke-linejoin="round"/>'
            f'<text x="2" y="12" fill="var(--ink-3)" font-size="11">{y1:.0f} m</text>'
            f'<text x="2" y="{H - 4:.0f}" fill="var(--ink-3)" font-size="11">{y0:.0f} m</text>'
            f"</svg>")


def _amap_gcj(lat, lon, name: str = ""):
    """返回 (GCJ 纬, GCJ 经, 清洗过的名称)，转不了就返回 None。

    轨迹和 trip.json 存的都是 WGS-84，高德 App 的 `androidamap://` 和网页
    URI 都要 GCJ-02；直接塞 WGS 会偏 400~500 m，导航导到隔壁街。
    两种入口共用这一处转换，别各写一遍。名称里的逗号换成空格：动作串用
    逗号分隔，Kotlin 侧 split(",", 3) 才不会把地名切开。
    """
    try:
        from . import basemap as BM

        glat, glon = BM.to_gcj(float(lat), float(lon))
    except Exception:
        return None
    return glat, glon, str(name or "位置").replace(",", " ")[:30]


def _amap_url(lat, lon, name: str, nav: bool = True) -> str:
    """高德网页 URI：桌面浏览器直接用，App 里给「没装高德」兜底。

    `nav=True` **不传 from**：高德在移动端会自动把当前位置填成起点；
    `nav=False` 是单点标注，只定位不规划路线。
    """
    g = _amap_gcj(lat, lon, name)
    if not g:
        return ""
    glat, glon, label = g
    if nav:
        q = _up.urlencode({"to": f"{glon:.6f},{glat:.6f},{label}",
                           "mode": "car", "coordinate": "gaode",
                           "callnative": "1", "src": "RunnerCoach"}, safe=",")
        return "https://uri.amap.com/navigation?" + q
    q = _up.urlencode({"position": f"{glon:.6f},{glat:.6f}",
                       "name": label, "coordinate": "gaode",
                       "callnative": "1", "src": "RunnerCoach"}, safe=",")
    return "https://uri.amap.com/marker?" + q


def _amap_act(lat, lon, name: str, nav: bool = True) -> str:
    """原生动作载荷：让 Kotlin 用 `androidamap://` 直接拉起高德 App。

    格式 `amapNav|amapView:经度,纬度,URL编码的名称`。以前走网页 URI 的
    `callnative=1`，要浏览器先打开高德网页再由网页唤起 App，多一跳且常被
    国产 ROM / 浏览器拦掉，用户看到的就是「打开网页」。现在 WebView 里点
    一下直接发 Intent；桌面浏览器没有 GC 桥，仍走 href 的网页兜底。
    """
    g = _amap_gcj(lat, lon, name)
    if not g:
        return ""
    glat, glon, label = g
    kind = "amapNav" if nav else "amapView"
    return f"{kind}:{glon:.6f},{glat:.6f},{_up.quote(label, safe='')}"


def _pin_tag(coords: list) -> str:
    """POI 变了就换一张底图：取景里要装下酒店，缓存名必须跟着坐标变。

    不带这层的话，加完住宿仍命中导入 GPX 时那张「只按轨迹取景」的缓存，
    酒店会落在画幅外，看一眼像是没标上。
    """
    if not coords:
        return ""
    raw = "|".join("%.5f,%.5f" % (float(a), float(b)) for a, b in coords)
    return "-p" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:6]


def _course_pins(slug: str, line: list) -> list[dict]:
    """赛道图上要标的三个点：起点、终点、住宿。

    只认 `stay`：机票 / 高铁 / 领物虽然也有坐标，但它们在几十公里外的
    机场和车站，一起塞进取景会把 105K 赛道压成画幅一角的一小团。
    那些点在「行程与预订」列表里各自带导航按钮，不往赛道图上画。
    """
    pins = []
    try:
        ev = E._read(slug, "event.json") or {}
    except Exception:
        ev = {}
    short = str(ev.get("short") or ev.get("name") or "赛事")[:12]
    if line:
        for kind, label, p in (("start", "起点", line[0]),
                               ("end", "终点", line[-1])):
            # 点地图上的点只「看位置」：导航留给图下那排快捷按钮，
            # 不然同一个动作有两个入口，点错了就起不了步
            _nm = short + " · " + label
            pins.append({"kind": kind, "label": label,
                         "lat": p[0], "lon": p[1],
                         "url": _amap_url(p[0], p[1], _nm, nav=False),
                         "act": _amap_act(p[0], p[1], _nm, nav=False),
                         # 图下那排按钮要的是真导航，单独留一份，别复用它
                         "nav_url": _amap_url(p[0], p[1], _nm),
                         "nav_act": _amap_act(p[0], p[1], _nm)})
    try:
        pois = E.trip_pins(slug)
    except Exception:
        pois = []
    for p in pois:
        if p["kind"] != "stay":
            continue
        # 标签只留几个字：酒店全名铺在 720×360 的图上会把轨迹盖住
        label = "住宿"
        short_name = str(p.get("label") or p.get("place") or label)[:6]
        _place = p.get("place") or p.get("label") or label
        pins.append({"kind": p["kind"], "label": short_name,
                     "lat": p["lat"], "lon": p["lon"],
                     "url": _amap_url(p["lat"], p["lon"], _place, nav=False),
                     "act": _amap_act(p["lat"], p["lon"], _place, nav=False),
                     "nav_url": _amap_url(p["lat"], p["lon"], _place),
                     "nav_act": _amap_act(p["lat"], p["lon"], _place)})
    return pins


def _course_map(slug: str, stats: dict) -> str:
    """赛道俯视图：底图 + 轨迹 + 可点开的起点/终点/住宿标注。

    剖面回答「哪一段在爬」，俯视图回答「它在哪儿」；起点、终点、住哪儿
    是赛前真正要找的三个点：点一下只为「看位置」，导航走图下那排按钮。
    """
    line = stats.get("polyline") or []
    if len(line) < 2:
        # 加 polyline 之前导的老数据：现读一次 GPX，顺便补写回 stats，
        # 免得每次打开赛事页都解析一遍几 MB 的 XML
        try:
            g = E._dir(slug) / "track.gpx"
            if g.is_file():
                r = E.parse_gpx(g, slug=slug, save=False)
                line = r.get("polyline") or []
                if line:
                    _s2 = dict(stats)
                    _s2["polyline"] = line
                    E._write(slug, "track_stats.json", _s2)
        except Exception:
            line = []
    if len(line) < 2:
        return ""

    pins = _course_pins(slug, line)
    # 起终点本来就在轨迹外框内，不用扩大取景；POI 才可能把画幅撑开。
    extra = [[p["lat"], p["lon"]] for p in pins
             if p["kind"] not in ("start", "end")]
    ident = E.map_ident(slug) + _pin_tag(extra)
    bm, href = None, None
    try:
        from . import basemap as BM

        if BM.available():
            # 缓存命中是秒回；没命中最多等 6 秒（导入时已经预取过一次）
            bm = BM.ensure(line, E.MAP_W, E.MAP_H, ident=ident,
                           timeout=6, extra=extra)
            if bm:
                href = "/basemap/%s-%dx%d.png" % (
                    ident, E.MAP_W, E.MAP_H)
    except Exception:
        bm, href = None, None

    try:
        from . import actcard as AC

        svg = AC.track_svg(line, E.MAP_W, E.MAP_H,
                           basemap=bm, href=href, pins=pins)
    except Exception:
        return ""
    cap = ("含高德底图 · 只作地形参考；<b>点标注可看位置，导航用下面的按钮</b>" if bm
           else "矢量轨迹 · <b>点标注可看位置，导航用下面的按钮</b>；"
                "在「我的 → 地图底图」填写高德密钥后，即可查看地图底图")
    legend = ('<div class="maplg"><span class="s"><i></i>起点</span>'
              '<span class="e"><i></i>终点</span>'
              + ('<span class="h"><i></i>住宿</span>'
                 if any(p["kind"] == "stay" for p in pins) else "")
              + "</div>")
    # SVG 里的圆点在手机上只有几像素，旁边再给一排 40px 高的按钮兜底：
    # 手指点不准标注时，照样能一键导航到起点 / 终点 / 住处。
    # 住处多填了几家时按钮只留第一家（按钮写死「住宿」，重复三份没意义），
    # 其余的仍能在图上点开。
    quick = ([p for p in pins if p["kind"] in ("start", "end")]
             + [p for p in pins if p["kind"] == "stay"][:1])
    navs = ('<div class="pager cmapnav">'
            + "".join(
                f'<a class="btn" href="{ui.esc(p.get("nav_url") or p["url"])}" '
                f'data-act="{ui.esc(p.get("nav_act") or p.get("act", ""))}">导航到'
                + ui.esc(p["label"] if p["kind"] in ("start", "end")
                         else "住宿") + "</a>"
                for p in quick if p.get("url"))
            + "</div>")
    return ('<div class="cmap">' + svg + "</div>" + legend + navs
            + f'<div class="sub2" style="margin:6px 0 2px">{cap}</div>')


_TRIP_CN = {"flight": "航班", "train": "高铁", "stay": "住宿",
            "race": "开赛", "other": "其他"}


def _booking_html(slug: str, hid: str) -> str:
    """「该订了」：按**比赛日**倒推的三类提醒，填了同类型行程自动已订。

    按钮按类型而不是行程下标提交——待办现在根本不绑具体行程，锚点是
    赛事日期。已订的沉底变灰但留着，既能反悔，也让人看清「为什么没催了」。
    """
    try:
        items = E.bookings(slug)
        rules = E.booking_rules(slug)
    except Exception:
        return ""
    if not items:
        return ""
    race = items[0].get("race") or ""
    race_txt = race[5:].replace("-", "/") if race else "比赛日"
    rows = []
    for b in items:
        d = b["days"]
        when = ("已订" if b["booked"] else
                (f"已过 {-d} 天" if d < 0 else
                 ("今天" if d == 0 else f"还有 {d} 天")))
        cls = "" if b["booked"] else (
            " hot" if d <= 7 else (" warn" if d <= 14 else ""))
        stamp = b["due"][5:].replace("-", "/")
        act, txt = (("unbook", "仍需提醒") if b["booked"]
                    else ("book", "已订 / 不用提醒"))
        btn = ('<form class="dinline" method="post" action="/__event/trip">' + hid
               + f'<input type="hidden" name="act" value="{act}">'
               + f'<input type="hidden" name="kind" value="{b["kind"]}">'
               + f'<button class="tnav" type="submit">{txt}</button></form>')
        note = (f'<span class="bd">{ui.esc(b["reason"])}</span>'
                if b.get("reason") else "")
        rows.append(
            f'<div class="bk{" done" if b["booked"] else ""}">'
            f'<span class="tk {b["kind"]}">订</span>'
            f'<span class="td{cls}">{when}</span>'
            f'<span class="bt"><b>{stamp}</b> 前 · {ui.esc(b["label"])}'
            f'<span class="bd">提前 {b["lead"]} 天</span>' + note + '</span>'
            + btn + "</div>")
    head = ('<div class="sub2"><b>该订了</b> · 按比赛日 '
            f'<b>{race_txt}</b> 倒推：航班 {rules["flight"]} 天 / '
            f'高铁 {rules["train"]} 天 / 住宿 {rules["stay"]} 天。'
            '到点由<b>系统日历</b>推送通知，不用打开 App。'
            '<b>填好对应类型的行程就自动视为已订</b>，提醒和日历事件一起停；'
            '本地比赛不坐飞机这类情况，直接点「不用提醒」也行。'
            '错过了也留在这里，不会悄悄消失。</div>')
    return ('<div class="card">' + head + "".join(rows)
            + '<div class="pager"><div class="btn" data-act="calendarSync">'
              '同步到系统日历</div></div>' + "</div>")


def _trips_html(slug: str, hid: str = "") -> str:
    """「行程与预订」：手填的 + 资料里认出来的，带倒计时和入口。

    之前这一块只在**已经从资料里认出时间点**时才出现，等于「先有鸡才有蛋」：
    没写资料的人根本找不到在哪加住宿。现在常驻，且空的时候把添加表单
    默认展开——入口就在眼前，不用去资料里翻。

    住宿填了地址会解析成坐标，赛道俯视图上跟着标点；没配高德 Key 也能
    存文字、跳导航（导航 URI 不依赖 Key），只是地图上标不出位置。
    """
    try:
        items = E.trips(slug)
    except Exception:
        items = []
    rows = []
    for x in items:
        d = x["days"]
        when = ("今天" if d == 0 else "明天" if d == 1 else
                (f"已过 {-d} 天" if d < 0 else f"还有 {d} 天"))
        hh = x["at"][11:16]
        stamp = x["at"][5:10].replace("-", "/") + ("" if hh == "00:00" else " " + hh)
        cls = " hot" if 0 <= d <= 3 else (" warn" if 0 <= d <= 7 else "")
        lat, lon = x.get("lat"), x.get("lon")
        nav = ""
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            label = x.get("place") or x["label"]
            url = _amap_url(lat, lon, label)
            if url:
                nav = (f'<a class="tnav" href="{ui.esc(url)}" '
                   f'data-act="{ui.esc(_amap_act(lat, lon, label))}">导航</a>'
                   f'<a class="tdel" href="{ui.esc(_amap_url(lat, lon, label, nav=False))}" '
                   f'data-act="{ui.esc(_amap_act(lat, lon, label, nav=False))}">'
                   "看位置</a>")
        place = str(x.get("place") or "")
        # 手填但没定位上的：给一个「补地址」入口，不用删了重加
        geo_form = ""
        if x.get("manual") and lat is None:
            geo_form = (
                '<details class="dmore tgeo"><summary>补 / 改地址，定位到地图</summary>'
                '<form class="frm" method="post" action="/__event/trip">' + hid
                + '<input type="hidden" name="act" value="geo">'
                f'<input type="hidden" name="idx" value="{x.get("idx", 0)}">'
                '<div class="frow"><label>酒店 / 车站全名或地址'
                f'<input class="fi" name="place" value="{ui.esc(place)}" '
                'placeholder="例：都江堰锦江之星"></label></div>'
                '<div class="fbtns"><button class="btn" type="submit">重新定位</button>'
                '<span class="fhint">要配高德 Web 服务 Key</span></div>'
                "</form></details>")
        del_btn = (
            '<form class="dinline" method="post" action="/__event/trip">' + hid
            + '<input type="hidden" name="act" value="del">'
            f'<input type="hidden" name="idx" value="{x.get("idx", 0)}">'
            '<button class="tdel" type="submit">删除</button></form>'
            if x.get("manual") else "")
        rows.append(
            '<div class="trip">'
            f'<span class="tk {x["kind"]}">{_TRIP_CN.get(x["kind"], "其他")}</span>'
            f'<span class="td{cls}">{when}</span>'
            f'<span class="tt"><b>{stamp}</b> {ui.esc(x["label"])}'
            f'<span class="ts">{ui.esc(x["source"])}</span>'
            + (f'<span class="tplace">📍 {ui.esc(place)}'
               + (" · 已在赛道图标注" if nav and x["kind"] == "stay"
                  else " · 已定位，可导航" if nav else " · 未定位")
               + "</span>" if place else "")
            + geo_form
            + ('<span class="ts">已订</span>' if x.get("booked") else "")
            + "</span>" + nav + del_btn + "</div>")

    kinds = "".join(f'<option value="{k}"'
                    + (' selected' if k == "stay" else "")
                    + f'>{ui.esc(v)}</option>' for k, v in E.TRIP_KINDS.items())
    form = (
        '<details class="fold"' + (" open" if not rows else "") + '>'
        '<summary>＋ 加一条行程 / 住宿（入口就在这里）</summary>'
        '<p class="hint">机票、高铁、酒店、领物都能加。'
        '<b>住宿填酒店全名或地址</b>，会自动定位并标到赛道俯视图上，'
        '点标注可跳高德导航；航班 / 高铁在下面列表里各自带导航按钮，'
        '不标到赛道图上（离赛道几十公里，画上去会把赛道挤成一小团）。'
        '所有条目同时进首页「临近」，并写进系统日历按类型提前提醒。'
        '<b>填了机票 / 高铁 / 住宿，上面「该订了」里对应的一类就自动视为已订，'
        '不再催你。</b></p>'
        '<form class="frm" method="post" action="/__event/trip">' + hid
        + '<input type="hidden" name="act" value="add">'
        '<div class="frow"><label>类型<select class="fi" name="kind">'
        + kinds + '</select></label>'
        '<label>日期和时间<input class="fi" name="at" type="datetime-local"></label></div>'
        '<div class="frow"><label class="fwide">说明<input class="fi" name="label" '
        'placeholder="例：入住 都江堰锦江之星 / CA1883 起飞" required></label></div>'
        '<div class="frow"><label class="fwide">地点（可留空，住宿建议填）'
        '<input class="fi" name="place" placeholder="酒店全名 / 车站 / 地址，用来定位">'
        '</label></div>'
        '<div class="fbtns"><button class="btn primary" type="submit">添加行程</button>'
        '<span class="fhint">没配 Key 也能存，只是地图上不标点</span></div>'
        "</form></details>")

    head = ('<div class="sub2">航班 / 高铁 / 住宿写在「资料」里也会自动出现在这里；'
            '确定好的信息用上面的表单直接填更准，也算「已订」。'
            '<b>3 天内标红</b>，别等出发前才想起来看。</div>'
            if rows else
            '<div class="sub2">这里还没有行程，但上面的「该订了」已经按比赛日'
            '在倒计时了。订好机票 / 高铁 / 酒店后<b>在下面直接加一条</b>：'
            '会按时间提醒，住宿填了地址还会在赛道图上标出位置，'
            '对应那一类的预订提醒也会自动停掉。</div>')
    try:
        rules = E.booking_rules(slug)
    except Exception:
        rules = dict(E.BOOKING_LEAD)
    rule_html = (
        '<details class="fold" id="triprule"><summary>改预订提前量'
        '（航班 / 高铁 / 住宿）</summary>'
        '<p class="hint">按<b>比赛日期</b>倒推「该订了」的日子，'
        '跟填没填行程无关。填 0 = 这一类不提醒；改完立刻重算，'
        '同步日历后旧事件会自动替换。</p>'
        '<form class="frm" method="post" action="/__event/trip">' + hid
        + '<input type="hidden" name="act" value="rules">'
        '<div class="frow bkrule">'
        + "".join(
            f'<label>{ui.esc(E.TRIP_KINDS[k])}（天）'
            f'<input class="fi" type="number" min="0" max="365" '
            f'name="{k}" value="{rules[k]}"></label>'
            for k in ("flight", "train", "stay"))
        + '</div><div class="fbtns"><button class="btn" type="submit">'
          '保存提醒设置</button><span class="fhint">保存后会更新预订提醒</span>'
          '</div></form></details>')
    return (_booking_html(slug, hid) + '<div class="card">' + head
            + "".join(rows) + "</div>" + form + rule_html)


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
    # 只有真存在「无关门」的点才挂灰点图例，否则全绿黄的 105K 也会多一句废话
    none_leg = ("，<b>灰点＝关门未公布</b>"
                if any(r.get("margin_s") is None for r in rows) else "")
    for r, y in zip(rows, ys[1:]):
        # 没有官方关门的 CP 用灰点：直接比较 None 会 TypeError，
        # 而在「有分段但关门没公布」的赛事上（路跑补给站常见）这条一定会炸。
        m = r.get("margin_s")
        cls = ("none" if m is None else
               "bad" if m < 0 else ("warn" if m < 3600 else "ok"))
        color = {"bad": "var(--bad)", "warn": "var(--warn)", "ok": "var(--ok)",
                 "none": "#6B7A8C"}[cls]
        marks.append(
            f'<circle cx="{X(r["km"]):.1f}" cy="{Y(y):.1f}" r="3.2" fill="{color}"/>'
            f'<text x="{X(r["km"]):.1f}" y="{Y(y) - 6:.1f}" fill="var(--ink-3)" '
            f'font-size="9" text-anchor="middle">{r["idx"]}</text>')
    return (
        f'<svg class="prof" viewBox="0 0 {W:.0f} {H:.0f}" preserveAspectRatio="none">'
        f'<polygon points="{area}" fill="rgba(79,214,166,.10)"/>'
        f'<polyline points="{line}" fill="none" stroke="var(--ok)" stroke-width="2" '
        f'stroke-linejoin="round"/>' + "".join(marks) + "</svg>"
        f'<div class="sub2">相对高度（起点 = 0，纵轴 ±{int(span)} m）· '
        "形状来自官方分段 D+/D-，<b>不是实测地形</b>；圆点是各 CP，"
        f"绿/黄/红对应该方案的关门余量{none_leg}。</div>")


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


def _event_bar(cur: str) -> str:
    """赛事切换条：横向滚动，一天内可能同时备三场，不让人猜「现在是哪一场」。"""
    items = []
    for e in E.list_events():
        on = " on" if e["slug"] == cur else ""
        days = e["days"]
        dtxt = ("%d 天" % days) if days > 0 else ("今天" if days == 0 else "已过")
        tag = {"A": "主赛", "B": "B 赛"}.get(e.get("priority") or "", "")
        items.append(
            f'<a class="ev{on}" href="/event?slug={e["slug"]}">'
            f'<b>{ui.esc(e["short"])}</b>'
            f'<span>{dtxt}{(" · " + tag) if tag else ""}</span></a>')
    # 新建/导入是独立流程，不塞在赛事详情页底部：这里跳出去，详情页只讲「这一场」
    items.append('<a class="ev add" href="/event?new=1"><b>＋</b><span>新建 / 导入</span></a>')
    return '<div class="evbar">' + "".join(items) + "</div>"


def _first_run() -> str:
    """一条赛事都没有时的首屏。

    种子赛事不再随安装包生成（2026-11-12 用户反馈：所有用户都能看到作者的
    4 场比赛），所以新用户第一次进来就是空库。这里不摆 7 个只有「—」的区块，
    只回答一个问题：先建一场。
    """
    body = (
        CSS
        + '<p class="hint">还没有赛事。建一场之后，这里会算出每个 CP 的关门余量、'
          '分段补给计划和装备清单。</p>'
        + '<div class="card"><form class="frm" method="post" action="/__event/new">'
        '<div class="frow"><label>赛事名<input class="fi" name="name" '
        'placeholder="例：2027 崇礼 168 · 70K"></label></div>'
        '<div class="frow"><label>日期<input class="fi" name="date" '
        'placeholder="2027-07-10" inputmode="numeric"></label>'
        '<label>距离 km<input class="fi" name="distance_km" type="number" '
        'step="0.1" min="1" max="400" placeholder="70"></label></div>'
        '<div class="frow"><label>爬升 m<input class="fi" name="ascent_m" type="number" '
        'step="10" min="0" placeholder="3200"></label>'
        '<label>起跑时间<input class="fi" name="start_time" value="08:00"></label></div>'
        '<div class="frow"><label>类型<select class="fi" name="kind">'
        '<option value="trail" selected>越野</option>'
        '<option value="road">路跑</option></select></label>'
        '<label>赛事优先级<select class="fi" name="priority">'
        '<option value="A">A · 重点赛事</option><option value="B">B · 有成绩目标</option>'
        '<option value="C" selected>C · 训练赛</option></select></label></div>'
        '<div class="fbtns"><button class="btn primary" type="submit">创建赛事</button>'
        '<span class="fhint">目标时间按距离与类型先给个初值，之后能改</span></div>'
        '</form></div>'
        + '<p class="sub2">添加赛事后，可以在「新建 / 导入」粘贴规程中的检查点表，查看各站预计用时和关门时间。'
          '路线有官方 GPX 的话，在赛事页点「导入轨迹」即可。</p>')
    return ui.page("EVENT · 赛事", "添加第一场赛事",
                   "只要名字、日期、距离，其它能推就推", body,
                   back=("AI 教练", "/ai"))


def _hms(sec) -> str:
    """秒 → H:MM:SS。目标完赛以前显示成 12.5h 这种小数，不好读也不好核对。"""
    t = int(round(float(sec or 0)))
    return "%d:%02d:%02d" % (t // 3600, t % 3600 // 60, t % 60)


def render_page(qs: dict | None = None) -> str:
    qs = qs or {}

    def _q(k: str) -> str | None:
        v = (qs.get(k) or [""])[0]
        return v.strip() or None

    try:
        ev = E.load_event(_q("slug"))
    except ValueError:
        # slug 无效就回落到「下一场」；真的一个赛事都没有才走首建页
        if not E.list_events():
            return _first_run()
        ev = E.load_event()
    slug = ev["slug"]
    course = E.load_course(slug)
    cd = E.countdown(ev)
    rows = E.plan_and_margin(slug)
    gear = E.gear_progress(slug)
    # 所有表单都要带上 slug：页面能切赛事后，任何一个表单少这一行都会改错档案
    HID = f'<input type="hidden" name="slug" value="{ui.esc(slug)}">'

    dur_txt = ("%gh" % ev["duration_h"]) if ev.get("duration_h") else "—"
    chips = (ui.chip(cd["status_cn"], "ok")
             + (ui.chip(f'倒计时 {cd["days"]} 天') if cd["days"] >= 0
                else ui.chip(f'已过 {abs(cd["days"])} 天', "gray"))
             + ui.chip(f'目标 {_hms(ev.get("goal_s"))} / 关门 {dur_txt}'
                       if ev.get("duration_h") else f'目标 {_hms(ev.get("goal_s"))}'))

    # ---- 概览
    overview = (
        '<div class="card"><div class="kv">'
        f'<div><b>{ev["distance_km"]:.1f}<small>km</small></b><span>距离</span></div>'
        f'<div><b>+{ev["ascent_m"]:,}<small>m</small></b><span>累计爬升</span></div>'
        f'<div><b>{dur_txt}</b><span>总关门</span></div>'
        f'<div><b>{len(rows) if cd["has_course"] else "整段"}</b><span>CP 数</span></div>'
        "</div>"
        f'<div class="sub2">{ui.esc(ev.get("date") or "")} '
        f'{ui.esc(ev.get("start_time") or "")} 起跑'
        + (f' · {ui.esc(ev["venue"])}' if ev.get("venue") else "") + "</div>"
        + (f'<div class="sub2">{ui.esc(ev["notes"])}</div>' if ev.get("notes") else "")
        # 提醒挂在「有没有余量」上而不是「有没有 CP 表」：在 CP 表里补了关门
        # 之后，这条提示应该自动消失
        + ("" if cd["tightest"] else
           '<div class="sub2 warnbox">这场还没有关门时间：只按整段算总用时与补给，'
           "获取官方关门时间后，可在「赛事设置 → 检查点表」补填，再查看预计能提前多久到达。</div>")
        + "</div>")

    # ---- 轨迹
    stats = E._read(slug, "track_stats.json")
    if stats:
        cmp = E.track_compare(slug) if cd["has_course"] else {"rows": []}
        ref = (course.get("reference") or {}).get("track") or {}
        edition = ref.get("edition") or ""
        # 差异一律拿「轨迹实测」跟「赛事标注」比。原先取 cmp["track_km"]，
        # 而没有 course.json 时（马拉松基本都没有 CP 表）它是 None，于是
        # 一律算成「相差 -42.2 km」——一句凭空冒出来的假话。
        _total = stats.get("total_km") or 0
        _dist0 = ev.get("distance_km") or 0
        _asc0 = ev.get("ascent_m") or 0
        d_km = _total - _dist0
        d_up = (stats.get("ascent_m") or 0) - _asc0
        # 刻度疏密跟着赛程走：全马 5 km、百公里 10 km、168 用 20 km
        _tick = 5 if _total <= 45 else (10 if _total <= 120 else 20)
        _ed = f"{edition} 官方" if edition else "赛事标注"
        _deviated = cd["has_course"] and not cmp.get("same_course", True)
        warn = ""
        if abs(d_km) >= 0.3 or abs(d_up) > max(50, _asc0 * 0.3):
            warn = (f'<div class="sub2 warnbox">轨迹与{_ed}相差 '
                    f'{d_km:+.1f} km / {d_up:+d} m'
                    + ("；中间几段是改线区，<b>CP 里程与关门以海报为准</b>，"
                       "轨迹只作地形参考" if _deviated else "") + "</div>")
        # 没有途经点不是错：马拉松的官方 GPX 通常只给路线，不给 CP 标注
        note = "" if (stats.get("waypoints") or []) else (
            '<div class="sub2">只有路线、没有途经点：距离与海拔照常计算，'
            f'剖面按每 {_tick:g} km 打了刻度；CP 里程对照做不了'
            "（马拉松一般就是这样，不影响其他计算）。</div>")
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
            + "</div>" + warn + note + _spark(stats.get("profile") or [], _tick)
            + _course_map(slug, stats)
            + (('<div class="cmptitle">CP 里程对照（官方 vs 轨迹）</div>' + cmp_tbl)
               if cmp_tbl else "")
            + '<div class="pager"><div class="btn" data-act="gpx">换一条 GPX</div></div></div>')
    else:
        track = (
            '<div class="card"><div class="empty"><b>还没导入赛道轨迹</b><br>'
            + ("官方 GPX 从「柴古唐斯」小程序下载后，用下面的按钮选中即可，"
               "会自动算出滤波爬升与海拔剖面（GPX 是 3D 赛道视频的前提）。"
               if cd["has_course"] else
               "官方 GPX 如果是「只有路线、没有途经点」的也没关系："
               "会算出距离、滤波爬升和带里程刻度的海拔剖面，只是做不了 CP 对照。")
            + "</div>"
            + (_relief(rows) if cd["has_course"] else "")
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
               '到达检查点后，<b>向左滑动对应的一行</b>，即可记录到达时间并更新后续的预计用时。</div>'
               '<form method="post" action="/__event/race" class="rinline">'
               + HID +
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
                need = (f'<span class="cpm warn">保 {_hms(ev["goal_s"])} 需提速 {int((rf-1)*100)}%</span>')
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
               + HID + '<input type="hidden" name="act" value="reset">'
               '<button class="rbtn2" type="submit">重置</button></form></div>')

    cps_rows = []
    _is_cp = cd["has_course"]
    for r in race["rows"]:
        done = r["done"]
        cpn = (f'<span class="cpn">CP{r["idx"]}</span>' if _is_cp else "")
        mtxt, mcls = _fmt_margin(r["margin_s"])
        cut_txt = (r["cutoff"].strftime("%d日%H:%M") if r["cutoff"]
                   else "未公布")
        margin_txt = (f' · 余量 <b class="cpm {mcls}">{mtxt}</b>' if r["cutoff"] else "")
        if done:
            delta = int(r["delta_s"])
            dtag = ("慢 %s" % _hm(abs(delta))) if delta > 0 else (
                "快 %s" % _hm(abs(delta)) if delta < 0 else "准时")
            first = (cpn + f'<span class="cpl">{ui.esc(r["name"])}</span>'
                     f'<span class="rpass">已过 {r["actual"].strftime("%H:%M")}</span>')
            second = (f'计划 {r["planned"].strftime("%H:%M")} · {dtag} · '
                      f'关门 {cut_txt}' + margin_txt)
            act, hint = "unpass", "撤销"
        else:
            first = (cpn + f'<span class="cpl">{ui.esc(r["name"])}'
                     + (' <b class="db">换装</b>' if r["dropbag"] else "") + "</span>")
            proj = r["projected"].strftime("%H:%M")
            second = (f'计划 {r["planned"].strftime("%H:%M")} · 预测 {proj} · '
                      f'关门 {cut_txt}' + margin_txt)
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
            + HID +
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
            f'<div class="flh">' + (f'<span class="cpn">CP{r["idx"]}</span>'
                                    if cd["has_course"] else "") +
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
        + HID +
        '<div class="frow"><label>碳水 g/h<input class="fi" name="carb_g_h" '
        f'type="number" step="5" min="30" max="120" value="{cfg["carb_g_h"]}"></label>'
        '<label>水 ml/h<input class="fi" name="fluid_ml_h" type="number" step="50" '
        f'min="200" max="1000" value="{cfg["fluid_ml_h"]}"></label>'
        '<label>钠 mg/h<input class="fi" name="sodium_mg_h" type="number" step="50" '
        f'min="200" max="1500" value="{cfg["sodium_mg_h"]}"></label></div>'
        '<div class="frow"><label>自带比例<input class="fi" name="carry_ratio" '
        f'type="number" step="0.05" min="0.2" max="1" value="{cfg["carry_ratio"]}"></label>'
        '<label>携水 ml<input class="fi" name="carry_ml" type="number" step="100" '
        f'min="0" max="3000" value="{cfg["carry_ml"]}"></label>'
        '<label>停留 min<input class="fi" name="stop_min" type="number" step="1" '
        f'min="0" max="30" value="{cfg["stop_min"]}"></label>'
        '<label>换装停留<input class="fi" name="dropbag_stop_min" type="number" '
        f'step="1" min="0" max="60" value="{cfg["dropbag_stop_min"]}"></label></div>'
        '<div class="fbtns"><button class="btn primary" type="submit">保存补给设置</button>'
        '<span class="fhint">碳水含胶与能量棒；总时长按目标配速推</span></div>'
        "</form>"
        '<p class="hint">这几个数只影响下面每段的「该吃多少、该背多少水」，'
        "不会改动余量表。天气热就调高水与钠，长距离前先按实战试一次。</p>")

    fuel_card = (
        '<div class="card">'
        f'<div class="ftot">{tot["hours"]:.1f} h · 碳水 <b>{tot["carb_g"]:,} g</b> · '
        f'水 <b>{tot["fluid_l"]:.1f} L</b> · 钠 <b>{tot["sodium_mg"]/1000:.1f} g</b> · '
        f'自带胶 <b>{tot["gels"]} 支</b></div>'
        + fuel_form + "".join(fuel_rows) + drop + "</div>")

    # ---- 资料：规则/交通/补给/报名/成绩 + 目录里的附件
    import urllib.parse as _up

    dcs = E.docs(slug)
    try:
        _yr = int(str(ev.get("date") or "")[:4])
    except (TypeError, ValueError):
        _yr = 0
    _yr = _yr or dt.date.today().year
    docs_rows, _idx = [], 0
    for _k in E.DOC_KINDS:
        part = [x for x in dcs["items"] if x["kind"] == _k]
        if not part:
            continue
        docs_rows.append(f'<div class="dg">{ui.esc(E.DOC_KINDS[_k])}</div>')
        for it in part:
            i_now = None if it.get("locked") else _idx
            if not it.get("locked"):
                _idx += 1
            body = ui.esc(it["body"]).replace("\n", "<br>")
            link = (f'<a class="dlink" href="{ui.esc(it["url"])}">{ui.esc(it["url"])}</a>'
                    if it.get("url") else "")
            # 删除按钮写成「删」而不是一个 ×：× 太像「关闭」，
            # 24px 的小图标在手机上也不容易点中（会误触成打开链接）。
            del_btn = ("" if it.get("locked") else
                       '<form class="dinline" method="post" action="/__event/doc" '
                       'onsubmit="return confirm(&quot;删掉这条资料？删了要重新加&quot;)">'
                       + HID + f'<input type="hidden" name="act" value="del">'
                       f'<input type="hidden" name="idx" value="{i_now}">'
                       '<button class="dbtn" type="submit" aria-label="删除这条资料">'
                       "删</button></form>")
            # 时间点：正文里认出来的日期/时刻，带提醒标记，一眼知道什么时候截止
            _times = ""
            try:
                _tps = E.parse_times(f'{it["title"]}\n{it["body"]}', _yr)
            except Exception:
                _tps = []
            if _tps:
                _times = ('<div class="dtimes">' + "".join(
                    f'<span class="dtime">⏱ {ui.esc(t["at"][5:10].replace("-", "/"))}'
                    + (f' {ui.esc(t["at"][11:16])}' if t["at"][11:16] != "00:00" else "")
                    + f' · {ui.esc(t["label"])}</span>' for t in _tps[:4])
                    + "</div>")
            # 改标题/正文：改错别字不该「删了重加」（会换掉添加日期、跑到末尾）
            edit_form = ("" if it.get("locked") else
                         '<details class="dmore"><summary>改</summary>'
                         '<form class="frm" method="post" action="/__event/doc">' + HID
                         + '<input type="hidden" name="act" value="edit">'
                         f'<input type="hidden" name="idx" value="{i_now}">'
                         '<div class="frow"><label>标题<input class="fi" name="title" '
                         f'value="{ui.esc(it["title"])}"></label></div>'
                         '<div class="frow"><label>正文<textarea class="ta" name="body" '
                         f'rows="3">{ui.esc(it["body"])}</textarea></label></div>'
                         '<div class="frow"><label>链接<input class="fi" name="url" '
                         f'value="{ui.esc(it["url"])}"></label></div>'
                         '<div class="fbtns"><button class="btn primary" type="submit">'
                         "保存</button></div></form></details>")
            docs_rows.append(
                f'<div class="dr"><div class="drt"><b>{ui.esc(it["title"])}</b>'
                + (f'<span class="dat">{ui.esc(it["at"])}</span>' if it.get("at") else "")
                + del_btn + "</div>"
                + _times
                + (f'<div class="drb">{body}</div>' if body else "")
                + (f'<div class="drb">{link}</div>' if link else "")
                + edit_form
                + "</div>")

    files_html = ""
    if dcs["files"]:
        rows_f = []
        for f in dcs["files"]:
            size = E.human_size(f["size"])
            url = "/event-file/%s/%s" % (slug, _up.quote(f["name"]))
            prev = "/event-doc?slug=%s&name=%s" % (slug, _up.quote(f["name"]))
            stem = f["name"].rsplit(".", 1)[0]
            rename_form = (
                '<details class="dmore"><summary>改名</summary>'
                '<form class="frm" method="post" action="/__event/file">' + HID
                + '<input type="hidden" name="act" value="rename">'
                f'<input type="hidden" name="name" value="{ui.esc(f["name"])}">'
                '<div class="frow"><label>新名字<input class="fi" name="new_name" '
                f'value="{ui.esc(stem)}"></label></div>'
                '<div class="fbtns"><button class="btn" type="submit">保存</button>'
                '<span class="fhint">扩展名自动保留</span></div></form></details>')
            del_form = (
                '<form class="dinline" method="post" action="/__event/file" '
                'onsubmit="return confirm(&quot;删掉这个附件？删了要重新上传&quot;)">'
                + HID + '<input type="hidden" name="act" value="del">'
                f'<input type="hidden" name="name" value="{ui.esc(f["name"])}">'
                '<button class="dfb danger" type="submit" '
                f'aria-label="删除附件 {ui.esc(f["name"])}">删除</button></form>')
            rows_f.append(
                f'<div class="df"><div class="dfl">'
                f'<a class="dfn" href="{prev}">{ui.esc(f["name"])}</a>'
                f'<span class="dfs">{size}</span></div>'
                f'<div class="dfa"><a class="dfb" href="{prev}">预览</a>'
                f'<button class="dfb" data-act="openUrl:{url}" type="button">用其他应用打开</button>'
                + del_form + "</div>" + rename_form + "</div>")
        files_html = ('<div class="dg">附件 · 点名字在软件内预览；PDF / Office 走系统应用</div>'
                      + "".join(rows_f))

    doc_form = (
        '<details class="fold"><summary>添一条资料（规则、领物、官网链接…）</summary>'
        '<form class="frm" method="post" action="/__event/doc">'
        + HID + '<input type="hidden" name="act" value="add">'
        '<div class="frow"><label>分类<select class="fi" name="kind">'
        + "".join(f'<option value="{k}">{ui.esc(v)}</option>'
                  for k, v in E.DOC_KINDS.items())
        + '</select></label><label>标题<input class="fi" name="title" '
        'placeholder="例：关门与罚则"></label></div>'
        '<div class="frow"><label>正文（可留空）<textarea class="ta" name="body" '
        'rows="3" placeholder="例：各 CP 关门见余量表；超时 1 分钟即退赛"></textarea>'
        '</label></div>'
        '<div class="frow"><label>链接（可留空）<input class="fi" name="url" '
        'placeholder="https://"></label></div>'
        '<div class="fbtns"><button class="btn primary" type="submit">添加赛事资料</button>'
        '<span class="fhint">正文与链接至少填一个；链接只认 http/https</span></div>'
        "</form></details>")

    docs_card = (
        '<p class="hint">赛前要翻的东西都放这里：规则、交通住宿、领物、报名凭证、成绩。'
        + "规程 PDF、参赛号截图可以点右上「传附件」直接选文件。"
        + '<b>订好的酒店 / 航班 / 高铁请到 <a href="#trip">行程与预订</a> 单条添加</b>：'
        + "那里能定位并在赛道图上标点，也会单独提醒。</p>"
        + '<div class="pager" style="margin:10px 0 2px">'
          f'<div class="btn primary" data-act="attach:{slug}">传附件</div>'
          '<span class="fhint" style="margin-left:10px">PDF / 图片 / 文本，单个 ≤ 20 MB</span>'
          "</div>"
        + ("".join(docs_rows) if docs_rows else
           '<div class="sub2">还没有文字资料，下面加一条。</div>')
        + files_html + doc_form)

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
            f'<input class="fi fi-cut" name="cut_{r["idx"]}" value="{seg.get("cutoff") or ""}" '
            f'inputmode="numeric" aria-label="CP{r["idx"]} 关门"></div>')

    prov = course.get("provenance") or {}
    src_cn = {"poster-2027": "2027 官方海报", "manual": "手工填写",
              "text": "从规程文字导入", "gpx": "GPX 解析"}.get(
                  prov.get("source") or "", prov.get("source") or "")
    if (prov.get("source") or "").startswith("copy:"):
        src_cn = "沿用 %s 的赛道" % prov["source"].split(":", 1)[1]
    prov_html = ""
    if prov:
        pend = len(prov.get("checked") or [])
        prov_html = (f'<div class="prov">赛道数据来源：<b>{ui.esc(src_cn)}</b>'
                     f' · {ui.esc(prov.get("at") or "")}'
                     f' · {ui.esc({"user": "本人确认", "ocr": "截图识别",
                                     "official": "官方页面"}.get(prov.get("by") or "",
                                                                prov.get("by") or ""))}'
                     + (f' · <b class="warn">{pend} 项待核对</b>' if pend else " · 无待核对项")
                     + "</div>")

    edit_card = (
        '<div class="card">' + prov_html +
        '<details class="fold"><summary>调整目标、状态与起跑时间</summary>'
        '<form class="frm" method="post" action="/__event/target">'
        + HID +
        # 目标完赛：时/分/秒三个框。以前是一个小数（12.5），跑者脑子里是
        # 「12 小时 30 分」，换算成小数既不好填也不好核对。
        f'<div class="frow"><label class="fwide">目标完赛用时（时:分:秒）<span class="hms">'
        f'<input class="fi" name="goal_hh" type="number" min="0" max="48" '
        f'value="{_hms(ev["goal_s"]).split(":")[0]}">:'
        f'<input class="fi" name="goal_mm" type="number" min="0" max="59" '
        f'value="{_hms(ev["goal_s"]).split(":")[1]}">:'
        f'<input class="fi" name="goal_ss" type="number" min="0" max="59" '
        f'value="{_hms(ev["goal_s"]).split(":")[2]}">'
        '</span></label>'
        f'<label>状态<select class="fi" name="status">{status_opts}</select></label>'
        '<label>起跑时间<input class="fi" name="start_time" value="'
        f'{ui.esc(ev["start_time"])}"></label></div>'
        '<div class="fbtns"><button class="btn primary" type="submit">保存</button>'
        '<span class="fhint">保存后会更新预计到达时间和补给安排</span></div></form></details>'
        '<details class="fold"><summary>检查点表（CP）</summary>'
        # 建完赛事之后也得能补 CP 表：原来只有「新建」视图里有粘贴入口，
        # 建完就再也找不到地方贴了（用户反馈）。这里给两条路——
        # 截图识别（大模型）和粘贴文字（规则解析），写盘走同一条链路。
        '<div class="pager" style="margin:0 0 10px">'
        f'<button class="btn primary" data-act="cpocr:{slug}" type="button">'
        "从截图识别检查点</button>"
        '<span class="fhint" style="margin-left:8px">'
        "拍摄规程中的表格或选择截图，App 会识别并填写检查点信息。</span></div>"
        '<form class="frm" method="post" action="/__event/course">'
        + HID +
        '<div class="cpe head"><span>CP</span><span>地点</span><span>km</span><span>关门</span></div>'
        + "".join(cp_rows) +
        '<div class="fbtns"><button class="btn primary" type="submit">保存检查点表</button>'
        '<span class="fhint">请按赛道顺序填写，后一站的里程应大于前一站。</span></div></form>'
        '<details class="fold"><summary>或者粘贴规程里的表格文字</summary>'
        '<p class="hint">每行填写一个检查点，包括编号、地名、累计里程和关门时间。关门时间也可以填写为出发后的时长。</p>'
        '<form class="frm" method="post" action="/__event/import">'
        + HID +
        '<textarea class="ta" name="text" rows="5" placeholder='
        '"CP1 仙山雅宿 10.9km 关门 08:05&#10;CP2 冰川瀑布景区 22.3km 关门 10:10">'
        "</textarea>"
        '<div class="fbtns"><button class="btn" type="submit">导入检查点表</button>'
        '<span class="fhint">格式正确后才会更新现有的检查点。</span></div></form></details>'
        "</details>"
        "</div>")

    err = (qs or {}).get("err", [""])[0] if qs else ""
    ok_msg = (qs or {}).get("ok", [""])[0] if qs else ""
    at = (qs.get("at") or [""])[0] if qs else ""
    err_bar = (f'<div class="errbar">没成功：{ui.esc(err)}</div>' if err else "")
    ok_bar = f'<div class="okbar">✓ {ui.esc(ok_msg)}</div>' if ok_msg else ""

    def _bar(name: str) -> str:
        """提示条只出现在「用户刚操作的那一块」，而不是页面顶端。

        提交发生在页面底部，303 回到 #fuel 之后顶端那条绿条已经滚出屏幕，
        用户看不到就等于没有反馈。
        """
        if at != name:
            return ""
        return err_bar or ok_bar

    # 没有指明区块（例如从别处带参数进来）时，退回顶部显示
    _ = None

    # ---- 赛后复盘：把计划余量和实际分段对上（只在比赛日过后出现）
    review_sec = ""
    if cd["days"] < 0:
        from . import review as R

        rep = R.auto(ev)
        if not rep.get("ok"):
            rv_body = ('<div class="card"><div class="empty"><b>还没有可复盘的数据</b><br>'
                       + ui.esc(rep.get("reason") or "未知原因") + "</div></div>")
        else:
            lines = [x for x in (rep.get("text") or "").splitlines() if x]
            rows_rv = []
            _is_cp = cd["has_course"]
            for x in rep["rows"]:
                dlt = x.get("leg_delta_s")
                if dlt is None:
                    cls, dtxt = "", "未到达"
                else:
                    cls = "bad" if dlt > 300 else ("ok" if dlt < -300 else "")
                    dtxt = ("慢 " + R._hm(dlt)) if dlt > 0 else (
                        "快 " + R._hm(-dlt) if dlt < 0 else "按计划")
                rows_rv.append(
                    '<div class="rvr">'
                    f'<div class="rv1">'
                    + (f'<span class="cpn">CP{x["idx"]}</span>' if _is_cp else "")
                    + f'<span class="cpl">{ui.esc(x["name"])}</span>'
                    f'<b class="cpm {cls}">{dtxt}</b></div>'
                    f'<div class="rv2">计划 {x["planned"].strftime("%H:%M")} · '
                    f'实际 {_day_hm(x["actual"], x["planned"])}'
                    + (f' · 段 {R._hm(x["leg_s"])}（计划 {R._hm(x["plan_leg_s"])}）'
                       if x.get("leg_s") else "")
                    + (f' · 余量 {"+" if x["margin_s"] >= 0 else "-"}{R._hm(x["margin_s"])}'
                       if x.get("margin_s") is not None else "")
                    + "</div></div>")
            rv_body = ('<div class="card">'
                       f'<div class="rvs">{ui.esc(lines[0] if lines else "")}</div>'
                       + ("".join(f'<div class="rvl">· {ui.esc(x)}</div>' for x in lines[1:]))
                       + '<div class="rvlist">' + "".join(rows_rv) + "</div>"
                       + '<div class="sub2">实际到达按活动记录插值到各 CP 里程；'
                         "「段」指的是相邻两个 CP 之间，比累计差值更能看出哪一段出了问题。</div>"
                       "</div>")
        review_sec = ui.sec("赛后复盘", _bar("review") + rv_body, anchor="review")

    # ---- 导入 / 新建：手机上从零建一场、或把规程里的 CP 表贴进来
    others = [e for e in E.list_events() if e["slug"] != slug]
    prev_opts = "".join(
        f'<option value="{e["slug"]}">{ui.esc(e["short"])}（{e["date"]}）</option>'
        for e in others)
    prev_block = (
        '<p class="hint">多数比赛每年改线：沿用后请核对 CP 里程，差 1 km 以上会在'
        "余量表里标红。</p>"
        '<form class="frm" method="post" action="/__event/prev">'
        + HID +
        (f'<div class="frow"><label>沿用哪一场<select class="fi" name="from">'
         f'{prev_opts}</select></label></div>'
         '<div class="fbtns"><button class="btn" type="submit">复制该赛事的检查点表</button>'
         '<span class="fhint">会覆盖当前赛道的 CP 与关门</span></div></form>'
         if others else '<div class="sub2">还没有别的赛事可以沿用。</div>'))

    import_card = (
        '<p class="hint">先添加赛事，再粘贴规程中的检查点表，'
        "最后核对里程和关门时间。</p>"
        '<details class="fold"><summary>① 新建一场赛事（只要名字、日期、距离）</summary>'
        '<form class="frm" method="post" action="/__event/new">'
        '<div class="frow"><label>赛事名<input class="fi" name="name" '
        'placeholder="例：2027 崇礼 168 · 70K"></label></div>'
        '<div class="frow"><label>日期<input class="fi" name="date" '
        'placeholder="2027-07-10" inputmode="numeric"></label>'
        '<label>距离 km<input class="fi" name="distance_km" type="number" '
        'step="0.1" min="1" max="400" placeholder="70"></label>'
        '<label>爬升 m<input class="fi" name="ascent_m" type="number" step="10" '
        'min="0" placeholder="3200"></label></div>'
        '<div class="frow"><label>起跑时间<input class="fi" name="start_time" '
        'value="08:00"></label><label>赛事优先级<select class="fi" name="priority">'
        '<option value="A">A · 重点赛事</option><option value="B">B · 有成绩目标</option>'
        '<option value="C" selected>C · 训练赛</option></select></label></div>'
        # 类型决定默认装备清单和目标配速：路跑给「号码布+鞋+胶+壶」，
        # 越野给「头灯+保温毯+水袋+换装包」，塞错了等于让人背一堆没用的东西
        '<div class="frow"><label>类型<select class="fi" name="kind">'
        '<option value="trail" selected>越野跑</option>'
        '<option value="road">路跑</option></select></label></div>'
        '<div class="fbtns"><button class="btn primary" type="submit">创建赛事</button>'
        '<span class="fhint">App 会根据距离给出目标用时，之后可以修改。</span></div></form></details>'

        '<details class="fold"><summary>② 粘贴规程里的检查点表</summary>'
        '<p class="hint">请粘贴完整表格，每行包含编号、地名、累计里程和关门时间。'
        "关门时间可以填写具体时刻或从起跑开始计算的时长。</p>"
        '<form class="frm" method="post" action="/__event/import">'
        + HID +
        '<textarea class="ta" name="text" rows="7" placeholder='
        '"CP1 仙山雅宿 10.9km 关门 08:05&#10;CP2 冰川瀑布景区 22.3km 关门 10:10">'
        "</textarea>"
        '<div class="fbtns"><button class="btn primary" type="submit">导入检查点表</button>'
        '<span class="fhint">格式正确后才会更新现有的检查点。</span></div></form>'
        "</details>"

        '<details class="fold"><summary>③ 或者直接沿用上一届的赛道</summary>'
        + prev_block + "</details>")

    top_bar = "" if at else (err_bar or ok_bar)
    # 删除整场：压在「调整」区块最底部，和保存类动作隔开一段。
    # 两步确认（4 秒内再点一次），不用 confirm()——WebView 有的机型不弹窗，
    # 点了像没反应，这种不可逆操作不能赌弹窗存在。
    _slug_js = '"' + slug.replace("\\", "\\\\").replace('"', '\\"') + '"'
    danger_card = (
        '<div style="margin-top:20px;padding-top:16px;border-top:1px solid var(--line)">'
        '<button class="btn" type="button" id="delEvBtn">删除这一场赛事</button>'
        '<p class="hint" style="margin-top:8px">会连同 CP 表、补给配置、装备勾选、'
        "资料附件和轨迹一起删掉，不可恢复。导出数据包里的那份还在。</p></div>"
        '<script>(function(){var b=document.getElementById("delEvBtn");if(!b)return;'
        'var arm=0;b.onclick=function(){'
        'if(Date.now()>arm){arm=Date.now()+4000;b.textContent="再点一次确认删除";'
        'b.style.color="var(--bad)";setTimeout(function(){if(Date.now()>arm){'
        'b.textContent="删除这一场赛事";b.style.color="";}},4200);return;}'
        'var f=document.createElement("form");f.method="post";'
        'f.action="/__event/delete";var i=document.createElement("input");'
        'i.type="hidden";i.name="slug";i.value=' + _slug_js + ';'
        'f.appendChild(i);document.body.appendChild(f);f.submit();};})();</script>')

    # 新建 / 导入是独立视图：赛事详情页只回答「这一场怎么样」，
    # 建新赛事不该垫在已有赛事的一堆区块下面（2026-11-12 用户反馈）。
    if _q("new"):
        return ui.page("EVENT · 新建", "新建赛事 / 导入赛道",
                       "添加赛事后，可从规程导入检查点。",
                       import_card,
                       back=("返回赛事", "/event?slug=" + slug))

    body = (CSS + _event_bar(slug) + top_bar + ui.sec("赛事", overview)
            + ui.sec("赛道轨迹", _bar("track") + track, anchor="track")
            + ui.sec("赛况与关门时间" if cd["has_course"] else "赛况",
                     _bar("race") + cps, anchor="race")
            + review_sec
            + ui.sec("补给与换装", _bar("fuel") + fuel_card, anchor="fuel")
            + ui.sec("强制装备", _bar("gear") + gears, anchor="gear")
            + ui.sec("行程与预订", _bar("trip") + _trips_html(slug, HID),
                     anchor="trip")
            + ui.sec("赛事资料", _bar("docs") + docs_card, anchor="docs")
            + ui.sec("赛事设置", _bar("adjust") + edit_card + danger_card, anchor="adjust"))

    subtitle = ev.get("short") or ev.get("name") or "赛事"
    sub3 = ("看余量 · 赛中打点 · 勾装备 · 每一块都能改"
            if cd["has_course"] else
            "还没有检查点表。可以先查看总用时、补给和装备，也可以从赛事规程导入检查点。")
    return ui.page("EVENT · 赛事", subtitle,
                   sub3,
                   body + SWIPE_JS, chips=chips, back=("AI 教练", "/ai"))


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

    if getattr(args, "review", False):
        from garmin_cn import review as R

        ev = E.load_event(slug)
        rep = R.auto(ev)
        if not rep.get("ok"):
            print("× 还不能复盘：%s" % rep.get("reason"))
            return 2
        print("%s · 赛后复盘" % ev["name"])
        print(rep["text"])
        print()
        print("%-4s %-10s %8s %10s %8s %8s" % ("CP", "地点", "计划", "实际", "段", "余量"))
        for x in rep["rows"]:
            print("%-4s %-10s %8s %10s %8s %8s" % (
                "CP%d" % x["idx"] if len(rep["rows"]) > 1 else "全程",
                x["name"], x["planned"].strftime("%H:%M"),
                x["actual"].strftime("%d日%H:%M") if x["actual"] else "—",
                R._hm(x["leg_s"]) if x.get("leg_s") else "—",
                R._signed(x["margin_s"]) if x.get("margin_s") is not None else "—"))
        return 0

    if getattr(args, "check", False) or getattr(args, "import_file", None) \
            or getattr(args, "from_prev", None):
        from garmin_cn import ingest as I

        ev = E.load_event(slug)
        if getattr(args, "check", False):
            issues = I.validate(ev, E.load_course(ev["slug"]))
            print("%s · %s" % (ev["name"], I.summary(issues)))
            print(I.explain(issues))
            return 1 if I.errors(issues) else 0

        if getattr(args, "from_prev", None):
            draft = I.from_prev(args.from_prev, ev)
            print("沿用 %s 的赛道（%d 段）——记得核对改线" % (args.from_prev, len(draft["segments"])))
        else:
            try:
                txt = io.open(args.import_file, encoding="utf-8").read()
            except OSError as e:
                print(f"× 读不到文件：{e}")
                return 2
            draft = I.from_text(txt, ev)
            if not draft["segments"]:
                print("× 没从这段文字里认出 CP 表：需要「CPn 地名 里程 关门」这样的行。"
                      "宁可手填也不要猜——请把规程里的 CP 表原样贴进来。")
                return 2
            print("解析到 %d 个 CP · 总距离 %s km · 爬升 %s"
                  % (len(draft["segments"]), draft["distance_km"], draft["ascent_m"]))
        r = I.apply(ev, draft, by="user", force=bool(getattr(args, "force", False)))
        print(I.explain(r["issues"]))
        if not r["ok"]:
            print("× %s。确认无误可加 --force 强制写入" % r["error"])
            return 1
        print("√ 已写入 %s（%d 段）" % (ev["slug"], r["n_segments"]))
        return 0

    if getattr(args, "list", False):
        rows = E.list_events()
        nxt = E.nearest_upcoming()
        print("赛事（按日期）")
        for e in rows:
            mark = "*" if nxt and e["slug"] == nxt["slug"] else " "
            d = e["days"]
            when = ("还有 %d 天" % d) if d > 0 else ("今天" if d == 0 else "已过 %d 天" % -d)
            print(" %s %-10s %s  %-9s %-4s %6.1f km / +%-5d %s%s"
                  % (mark, e["short"], e["date"], when, e["priority"],
                     e["distance_km"], e["ascent_m"],
                     "有CP表" if e["has_course"] else "整段",
                     "  [" + E.STATUS_CN.get(e["status"], e["status"]) + "]"
                     if e["status"] != "confirmed" else ""))
        print("\n  * = 下一场（页面与看板默认用它）")
        return 0

    # 默认：档案概览 + 关门余量
    ev = E.load_event(slug)
    cd = E.countdown(ev)
    today = dt.date.today()
    race = dt.date.fromisoformat(ev["date"])
    print(f"{ev['name']}　[{E.STATUS_CN.get(ev['status'], ev['status'])}]")
    print(f"日期 {ev['date']} {ev['start_time']} 起跑 · {ev['venue']}")
    dur = ("%g h" % ev["duration_h"]) if ev.get("duration_h") else "未公布"
    print(f"{ev['distance_km']} km / +{ev['ascent_m']:,} m · 总关门 {dur}"
          f" · 倒计时 {(race - today).days} 天")
    print(f"目标 {_hms(ev.get('goal_s'))} · 装备 {cd['gear_done']}/{cd['gear_total']}")
    print()
    print(E.margins_report(slug))
    return 0


def add_parser(sub):
    """把 `run.bat event` 的参数注册也收进来，cli.py 只留两行。"""
    q = sub.add_parser("event", help="赛事档案：关门余量表 / 目标 / 装备 / GPX")
    q.add_argument("slug", nargs="?", help="赛事目录名（默认取「下一场」）")
    q.add_argument("--list", action="store_true", help="列出所有赛事与倒计时")
    q.add_argument("--review", action="store_true",
                   help="赛后复盘：计划余量 vs 实际分段（需要活动明细）")
    q.add_argument("--check", action="store_true",
                   help="校验当前赛道的内部一致性与可行性（不改数据）")
    q.add_argument("--import", dest="import_file", metavar="文件",
                   help="从一段规程文字导入 CP 表（txt，粘贴即可）")
    q.add_argument("--from-prev", metavar="SLUG",
                   help="沿用另一场（通常是上一届）的赛道，标为「需核对」")
    q.add_argument("--force", action="store_true", help="校验有 error 时仍强制写入")
    q.add_argument("--goal", type=float, metavar="H",
                   help="设置目标完赛小时数并查看余量")
    q.add_argument("--gear", nargs="+", metavar="关键词",
                   help="按关键词勾选装备清单（如 --gear 头灯 保温毯）")
    q.add_argument("--gpx", metavar="文件", help="导入官方 GPX 轨迹（滤波爬升 + 剖面）")
    q.set_defaults(func=cmd)
    return q

# 预览页用 ui.page 的外壳，拿不到上面那份 CSS，所以这几条随身带。
# 原来靠 ui.page 的基础样式撑着，图片连 max-height 都没有——长截图会把
# 整页拉得老长，看着像「图没加载出来」。
_PV_CSS = (
    "<style>"
    ".dimg{display:block;width:100%;height:auto;max-height:72vh;object-fit:contain;"
    "background:rgba(255,255,255,.03);border-radius:14px;cursor:zoom-in;"
    "border:1px solid var(--line)}"
    ".dimg.full{max-height:none;object-fit:fill;cursor:zoom-out}"
    ".dpre{white-space:pre-wrap;word-break:break-word;font-size:12px;"
    "line-height:1.7;color:var(--ink,#E8F0FB);margin:0}"
    ".dbig{padding:24px 12px;text-align:center;color:var(--ink-2);"
    "font-size:12px}"
    "</style>")

_PREVIEW_TEXT = (".txt", ".md", ".csv", ".ics", ".json")
_PREVIEW_IMG = (".png", ".jpg", ".jpeg")


def render_doc_page(qs: dict | None = None) -> str:
    """附件在软件内预览：图片直接显示，文本原样排版，PDF / Office 明说去哪看。

    WebView 没有内置 PDF 渲染器，硬塞 iframe 只会得到一块空白；
    这类文件老实给「用系统应用打开」，比做一个假预览诚实。
    """
    qs = qs or {}

    def _q(k: str) -> str:
        return (qs.get(k) or [""])[0].strip()

    slug, name = _q("slug"), _q("name")
    if not slug or not name or "/" in name or "\\" in name or name.startswith("."):
        return ui.page("EVENT · 资料", "打不开这个附件", "", 
                       '<div class="dbig">文件名不合法。</div>'
                       '<div class="pager"><a class="btn" href="/event">返回赛事</a></div>',
                       back=("返回赛事", "/event"))
    try:
        slug = E.active_slug(slug)
    except ValueError:
        return ui.page("EVENT · 资料", "找不到这场赛事", "",
                       '<div class="dbig">赛事可能已经被删除。</div>',
                       back=("返回赛事", "/event"))
    fp = E._dir(slug) / name
    if not fp.is_file():
        return ui.page("EVENT · 资料", "附件不见了", "",
                       f'<div class="dbig">赛事目录里没有 {ui.esc(name)}。</div>',
                       back=("返回赛事", "/event"))
    suf = fp.suffix.lower()
    raw_url = "/event-file/%s/%s" % (slug, _up.quote(name))
    if suf in _PREVIEW_IMG:
        body = (f'<img class="dimg" id="pvImg" src="{raw_url}" '
                f'alt="{ui.esc(name)}">'
                '<div class="sub2" id="pvHint" style="text-align:center;margin-top:8px">'
                "已缩放到一屏 · 点图片看原尺寸</div>"
                '<script>(function(){var i=document.getElementById("pvImg"),'
                'h=document.getElementById("pvHint");if(!i)return;'
                'i.addEventListener("click",function(){'
                'var on=i.classList.toggle("full");'
                'if(h)h.textContent=on?"原尺寸 · 点图片缩回一屏":"已缩放到一屏 · 点图片看原尺寸";'
                '});})();</script>')
    elif suf in _PREVIEW_TEXT:
        try:
            text = io.open(fp, encoding="utf-8", errors="replace").read(200000)
        except OSError as e:
            text = "读不出来：%s" % e
        body = f'<pre class="dpre">{ui.esc(text)}</pre>'
    else:
        body = ('<div class="dbig">手机里没有内置的 PDF / Office 渲染器，'
                '点下面用系统应用打开。</div>'
                f'<div class="pager" style="justify-content:center">'
                f'<button class="btn primary" data-act="openUrl:{raw_url}" '
                'type="button">用系统应用打开</button></div>')
    size = E.human_size(fp.stat().st_size)
    return ui.page("EVENT · 资料", name,
                   f"{slug} · {size}",
                   '<div class="card">' + _PV_CSS + body + "</div>",
                   chips=ui.chip(suf.lstrip(".").upper()),
                   back=("返回赛事", "/event#docs"))
