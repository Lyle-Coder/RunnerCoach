# -*- coding: utf-8 -*-
"""远征界面：把服务端下发的 state 渲染成「一条在雪线上的路」。

设计取舍
--------
远征页和日常页是**两套气质，一套骨架**：

| | 日常页 | 远征页 |
|---|---|---|
| 信息 | 数字（配速、负荷、完成率） | 处境（走到哪、还剩多少、要不要冒险） |
| 正文字体 | 无衬线 | **衬线**（故事用衬线，数据用无衬线） |
| 进度 | 进度条 | **一条带折角的路 + 路名** |
| 暖色 | 无 | 赭石（当前所在、火、补给） |

骨架仍然是同一套：同样的深空底色、同样的卡片、同样的圆角与细线圈边。
换的只有「材质」和「隐喻」——这样它还是同一个 App，但一眼能看出这里在讲故事。

三处容易做错的地方：

1. **路名与幕名不写死**。它们属于内容（换一季就变了），由服务端随 state 下发。
2. **选项不显示数值**。服务端刻意不下发 `effect`，界面只能给 label + brief。
   前端一旦自己猜「哪个更划算」，前面四轮平衡做的「差 0.1 分只体现在情境里」就白做了。
3. **衬线字体要内嵌**。Android 的 WebView 里 `serif` 对中文没有可用字体，
   不内嵌就 fallback 成无衬线，故事感直接消失。见 `tools/build_font.py`。
"""

import json

from . import ui
from .ui import esc

# 衬线正文用的字体。名字带前缀避免和系统字体撞；font-display:swap 让正文
# 先用无衬线顶上，字体到了再换——内嵌 1.5 MB，不能让首屏空着等。
FONT_CSS = """
@font-face{font-family:"RC Serif";font-weight:400;font-display:swap;
  src:url("/fonts/expedition-serif.woff2") format("woff2")}
"""

CSS = """
/* ---------- 页头山脊 ----------
   绝对定位在页头里，不参与布局。三层低多边形，总不透明度 11%——
   它是「环境」不是「插画」，超过这个强度就会和标题抢注意力。 */
#runbg{position:absolute;left:-14px;right:-14px;top:0;bottom:0;z-index:0;
  pointer-events:none;overflow:hidden}
#runbg svg{position:absolute;left:0;bottom:0;width:100%;height:100%}
.page-head>*:not(#runbg){position:relative;z-index:1}

/* ---------- 状态条：士气 / 补给 / 行程 ---------- */
.rbar{display:flex;align-items:center;gap:10px;padding:10px 13px;margin-top:16px;
  border-radius:12px;background:rgba(15,22,34,.82);border:1px solid var(--line2)}
.rbar .it{display:flex;align-items:center;gap:5px;font-size:12px;color:var(--ink2)}
.rbar .it b{font-weight:600;font-variant-numeric:tabular-nums}
.rbar .sep{width:1px;height:14px;background:var(--line);flex:0 0 auto}
.rbar .sp{flex:1 1 auto}
.rbar .note{font-size:11px;color:var(--faint)}
/* 补给见底要显眼：受困就是从这里开始的 */
.rbar .it.low{color:var(--amber)}
.rbar .it.zero{color:var(--red)}

/* ---------- 四段路 ---------- */
.route{margin-top:18px}
.route .rh{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:3px}
.route svg{width:100%;height:auto;display:block;overflow:visible}
.route .gap{height:3px;border-radius:2px;background:var(--line2);overflow:hidden;margin-top:1px}
.route .gap i{display:block;height:100%;border-radius:2px;
  background:linear-gradient(90deg,#3B82F6,#22D3EE)}
.route .rm{font-size:10.5px;color:var(--faint);margin-top:7px}

/* ---------- 事件卡 ----------
   数据卡是「面板」，叙事卡是「纸条」：圆角更小、边更暗、左侧一道类型色。 */
.ecard{position:relative;background:linear-gradient(180deg,rgba(19,28,43,.92),
  rgba(14,21,33,.95));border:1px solid var(--line2);border-radius:10px;
  overflow:hidden;margin-top:12px}
.ecard::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;
  background:var(--ec,#5EA0FF)}
.ecard.fork{--ec:#C98B4B;border-color:rgba(201,139,75,.30)}
.ecard.trial{--ec:#22D3EE}
.ecard.supply{--ec:#5EA0FF}
.ecard.story{--ec:#8CC2FF}
/* 遗迹（旧日志）：暖边 + 稍亮的底，让它从一排事件里"跳"一下。
   只让人注意到"这个不一样"，不写有什么用——标成任务清单就不叫探索了。 */
.ecard.relic{--ec:#C98B4B;border-color:rgba(201,139,75,.34);
  background:linear-gradient(180deg,rgba(28,24,19,.92),rgba(18,21,30,.95))}
.ecard.relic .tag{background:rgba(201,139,75,.18);color:#DCA269}
.ecard.done{opacity:.9}
.ecard .in{padding:14px 15px 14px 18px}
.ecard .tag{display:inline-block;font-size:10.5px;line-height:1.5;padding:1px 7px;
  border-radius:4px;letter-spacing:.4px;background:rgba(94,160,255,.12);color:var(--brand-ink)}
.ecard.fork .tag{background:rgba(201,139,75,.15);color:#DCA269}
.ecard.trial .tag{background:rgba(34,211,238,.13);color:#5EE7F5}
.ecard .when{margin-left:6px;font-size:10.5px;color:var(--faint)}
.ecard h3{margin:7px 0 0;font-size:15.5px;font-weight:600;line-height:1.4}
.ecard.fork h3{font-size:16.5px}
/* 衬线正文：故事用宋，数据用黑。行高放到 1.85——宋体笔画细，挤在一起读不动 */
.ecard .lit{margin:7px 0 0;font-family:"RC Serif",Georgia,"Songti SC",serif;
  font-size:14px;line-height:1.85;color:#C6D4E6;
  text-align:justify;text-justify:inter-ideograph}
/* 选完之后的结论更暗、更短：是「回望」不是「继续」 */
.ecard .res{margin:8px 0 0;font-family:"RC Serif",Georgia,"Songti SC",serif;
  font-size:13px;line-height:1.72;color:var(--muted)}
.ecard .gain{margin-top:9px;display:flex;align-items:center;gap:6px;
  font-size:11.5px;color:#DCA269}

/* 选项：不是按钮，是「两条路」——整行可点，右侧一个指向 */
.opt{display:flex;align-items:center;gap:10px;margin-top:8px;padding:11px 12px;
  border-radius:10px;background:rgba(19,28,43,.55);border:1px solid var(--line2);
  color:inherit;text-decoration:none;min-height:52px}
.opt.on{background:rgba(59,130,246,.09);border-color:rgba(96,165,250,.28)}
.opt:active{opacity:.72}
.opt .tx{flex:1 1 auto;min-width:0}
.opt .tx b{display:block;font-size:13.5px;font-weight:600;color:var(--ink)}
.opt .tx span{display:block;font-size:11.5px;color:var(--muted);margin-top:2px}
.opt .ar{flex:0 0 auto;color:var(--faint);font-size:14px;line-height:1}
/* 带天数成本的选项把代价标出来——「等一晚」的重点就是那一天 */
.opt .cost{flex:0 0 auto;font-size:10.5px;color:var(--amber);
  padding:1px 6px;border-radius:4px;background:var(--amber-soft)}

/* 试炼：两个按钮等宽。跑完了 / 没跑成都得报——不报不等于没跑 */
.tbtns{display:flex;gap:8px;margin-top:11px}
.tbtns button{flex:1 1 0;min-height:46px;border-radius:10px;font-family:inherit;
  font-size:13px;cursor:pointer;border:1px solid var(--line2);
  background:rgba(19,28,43,.7);color:var(--ink2)}
.tbtns button.ok{background:linear-gradient(135deg,#3B82F6,#22D3EE);
  border-color:transparent;color:#04121F;font-weight:600}
.tbtns button:active{opacity:.75}
.treq{display:flex;align-items:center;gap:7px;margin-top:9px;padding:8px 11px;
  border-radius:8px;background:rgba(34,211,238,.07);
  border:1px solid rgba(34,211,238,.14);font-size:12px;color:#9FD9E4}
/* 核验说明：一句话而已，但要放在按钮上面——点完才解释就成了借口 */
.thint{margin-top:7px;font-size:11px;line-height:1.55;color:var(--faint)}
/* 结论的三种口径。未核验用灰，不是警告色——那不是惩罚，是「这次判断不了」 */
.lev{display:flex;align-items:center;gap:6px;margin-top:8px;font-size:11.5px}
.lev.verified{color:#6FD2A8}
.lev.claimed{color:var(--faint)}
.lev.failed{color:#F58C90}

/* ---------- 受困 ---------- */
.stuck{margin-top:14px;padding:13px 14px;border-radius:12px;
  background:rgba(242,85,90,.08);border:1px solid rgba(242,85,90,.26)}
.stuck .t{font-size:13.5px;font-weight:600;color:#F58C90}
.stuck .d{font-size:12px;color:var(--muted);margin-top:4px;line-height:1.6}
.stuck button{margin-top:10px;margin-right:8px;min-height:44px;padding:0 16px;
  border-radius:999px;border:1px solid rgba(242,85,90,.34);
  background:rgba(242,85,90,.12);color:#F58C90;font-family:inherit;
  font-size:13px;cursor:pointer}
.stuck button:active{opacity:.72}

.empty{text-align:center;padding:40px 16px;color:var(--muted);font-size:13px}
.empty a{display:inline-block;margin-top:14px}
"""

# 试炼类型的中文。这是「怎么演」不是「演什么」——它描述要跑什么，不涉及剧情。
METRIC_ZH = {
    "negative_split": "渐加速",
    "even_pace": "匀速",
    "cadence_stable": "步频稳定",
    "hr_drift": "心率漂移",
    "climb_steady": "爬升稳定",
}
TYPE_ZH = {"trial": "试炼", "supply": "补给", "story": "遭遇"}
KIND_ZH = {"fork": "岔路"}
CN_NUM = "一二三四五六"


# ---------------------------------------------------------------- 小工具
def _icon(name: str, size: int = 16) -> str:
    """内联 SVG 图标。用 path 不用字体：图标字体在 WebView 里要么缺字要么糊。"""
    paths = {
        "fire": ('<path d="M8 2 C8 2 4 6.2 4 9 A4 4 0 0 0 12 9 C12 6.2 8 2 8 2 Z"'
                 ' fill="currentColor" opacity=".9"/>'
                 '<path d="M8 7 C8 7 6.4 9 6.4 10.2 A1.6 1.6 0 0 0 9.6 10.2'
                 ' C9.6 9 8 7 8 7 Z" fill="#F5C26B"/>'),
        "flask": ('<rect x="5.5" y="2.5" width="5" height="1.6" rx=".8" fill="currentColor"/>'
                  '<path d="M5.5 4.1 h5 v8.4 a1.2 1.2 0 0 1 -1.2 1.2 h-2.6'
                  ' a1.2 1.2 0 0 1 -1.2 -1.2 Z" stroke="currentColor" stroke-width="1.1"'
                  ' fill="none"/><path d="M5.5 8.4 h5" stroke="currentColor"'
                  ' stroke-width=".9" opacity=".55"/>'),
        "foot": ('<path d="M6 2.6 C7.6 2.2 8.4 3.4 8.2 4.6 C8 5.8 7 6.2 6 6'
                 ' C5 5.8 5.2 3 6 2.6 Z" fill="currentColor"/>'
                 '<ellipse cx="6.6" cy="8.4" rx="2" ry="1.1" fill="currentColor"'
                 ' opacity=".65"/>'),
        "check": ('<path d="M1 6.5 L4.5 10 L11 2.5" stroke="currentColor"'
                  ' stroke-width="1.6" fill="none" stroke-linecap="round"/>'),
        # 试炼的「要求」用一个靶心，不用对勾——对勾已经给了「核验通过」，
        # 两个对勾并排会让「要跑什么」和「跑成没跑成」看起来是同一件事。
        "target": ('<circle cx="8" cy="8" r="5.1" stroke="currentColor"'
                   ' stroke-width="1.2" fill="none"/>'
                   '<circle cx="8" cy="8" r="1.5" fill="currentColor"/>'),
    }
    p = paths.get(name)
    if not p:
        return ""
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 16 16"'
            f' aria-hidden="true" style="display:block;flex:0 0 auto">{p}</svg>')


# ---------------------------------------------------------------- 山与路
def ridge_svg() -> str:
    """页头的山脊剪影。三层，越靠前越淡——雪线是远景，不该压过标题。"""
    return (
        '<svg viewBox="0 0 392 150" preserveAspectRatio="none" aria-hidden="true">'
        '<path d="M0 118 L34 88 L62 104 L96 62 L128 92 L158 74 L188 100 L214 82'
        ' L248 108 L282 78 L316 96 L352 70 L392 100 L392 150 L0 150 Z"'
        ' fill="#DCE7F5" opacity="0.062"/>'
        '<path d="M0 132 L40 116 L74 128 L112 100 L146 122 L182 108 L214 130'
        ' L252 112 L292 128 L330 110 L368 126 L392 118 L392 150 L0 150 Z"'
        ' fill="#DCE7F5" opacity="0.045"/>'
        '<path d="M96 62 L108 72 L120 66" stroke="#DCE7F5" stroke-width="1"'
        ' opacity="0.22" fill="none"/>'
        '<path d="M248 108 L258 116 L268 111" stroke="#DCE7F5" stroke-width="1"'
        ' opacity="0.18" fill="none"/>'
        '<circle cx="96" cy="55" r="1.6" fill="#DCE7F5" opacity="0.35"/>'
        '<circle cx="248" cy="101" r="1.2" fill="#DCE7F5" opacity="0.28"/>'
        '<circle cx="352" cy="62" r="1.4" fill="#DCE7F5" opacity="0.3"/>'
        "</svg>"
    )


# 五个途经点的纵向偏移：让路看起来在山间起伏，不是一条直线。
_Y = (34.0, 52.0, 40.0, 58.0, 46.0)


def _short(name: str) -> str:
    """路线上的地名缩写。

    五个站在 420 宽里每个只有 ~80px，10.5px 的字最多放三个字。
    「采药人小屋」五个字必然和邻居压在一起，取后两字「小屋」反而更像路标。
    """
    name = str(name or "")
    return name if len(name) <= 3 else name[-2:]


def route_svg(stops: list[str], act: int, stage_frac: float) -> str:
    """画一条四段的路，小旗放在**第 act 段的 `stage_frac` 处**。

    注意是单幕进度，不是整季累计。队伍超额时整季进度会超过 100%，拿它定位
    小旗会顶到最右端，看起来像这一季已经走完了——而其实才第二幕。
    """
    st = [_short(x) for x in (stops or [])]
    while len(st) < 5:
        st.append("")
    n = len(st)
    x0, x1 = 26.0, 346.0
    xs = [x0 + (x1 - x0) * i / (n - 1) for i in range(n)]
    ys = [_Y[i % len(_Y)] for i in range(n)]
    pts = list(zip(xs, ys))

    full = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts)
    # 第 act 幕是「点 act-1 → 点 act」这一段（幕从 1 数，点从 0 数）
    at = max(0, min(n - 2, int(act) - 1))
    t = max(0.0, min(1.0, stage_frac))
    cur = (xs[at] + (xs[at + 1] - xs[at]) * t,
           ys[at] + (ys[at + 1] - ys[at]) * t)
    done = [(xs[i], ys[i]) for i in range(at + 1)] + [cur]
    done_path = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in done) if len(done) > 1 else ""

    out = [
        '<svg viewBox="0 0 372 104" role="img" aria-label="这一季的路线与当前位置">',
        f'<path d="{full}" fill="none" stroke="rgba(124,143,168,.26)" stroke-width="1.6"'
        ' stroke-dasharray="3 5" stroke-linecap="round"/>',
    ]
    if done_path:
        out.append(f'<path d="{done_path}" fill="none" stroke="#8CC2FF" stroke-width="2.2"'
                   ' stroke-linecap="round" stroke-linejoin="round"/>')

    for i, (x, y) in enumerate(pts):
        passed = (i <= at)
        if i == 0 or passed:
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="#8CC2FF"/>')
        else:
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.4" fill="#070B12"'
                       ' stroke="rgba(124,143,168,.45)" stroke-width="1.4"/>')
        if st[i]:
            # 地名一律放在折线下方。上下交替本来是怕相邻压住，但缩写后
            # 最多三个字、站点间距 80px，本来就撞不上；而上方的位置会被
            # 「当前所在」的小旗占掉，交替反而更容易挤。
            color = "#B7C7DD" if passed else "#7689A2"
            out.append(f'<text x="{x:.1f}" y="{y + 18:.1f}" font-size="10.5"'
                       f' fill="{color}" text-anchor="middle">{esc(st[i])}</text>')

    # 当前所在：赭石小旗。全 App 唯一的暖色，只出现在这里
    cx, cy = cur
    out += [
        f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{cx:.1f}" y2="{cy - 18:.1f}"'
        ' stroke="#C98B4B" stroke-width="1.3"/>',
        f'<path d="M{cx:.1f} {cy - 18:.1f} l10 3.2 l-10 3.2 z" fill="#C98B4B"/>',
        f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="5.6" fill="#070B12"'
        ' stroke="#C98B4B" stroke-width="2"/>',
        f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="1.7" fill="#C98B4B"/>',
        "</svg>",
    ]
    return "".join(out)


# ---------------------------------------------------------------- 事件卡
def _options_html(ev: dict, slot: int) -> str:
    """选项行。选过之后只留选中那条——其余收起，不让人以为还能改。"""
    opts = ev.get("options") or []
    if not opts:
        return ""
    chosen = str(ev.get("chosen") or "")
    out = []
    for o in opts:
        oid = str(o.get("id") or "")
        picked = chosen == oid
        if chosen and not picked:
            continue
        cost = int(o.get("time") or 0)
        cost_html = f'<span class="cost">慢 {cost} 天</span>' if cost else ""
        brief = esc(o.get("brief") or "")
        inner = (f'<span class="tx"><b>{esc(o.get("label") or "")}</b>'
                 + (f"<span>{brief}</span>" if brief else "")
                 + f'</span>{cost_html}'
                 f'<span class="ar">{"·" if picked else "›"}</span>')
        if picked:
            out.append(f'<div class="opt on">{inner}</div>')
        else:
            out.append(f'<a class="opt" href="#" onclick="rcPick({slot},'
                       f'{json.dumps(oid)});return false">{inner}</a>')
    return "".join(out)


def event_card(ev: dict, slot: int) -> str:
    """一张事件卡。试炼与「有事要选」分两条渲染路径。

    试炼没有 options——要跑什么写在 `quality` 里，结果走 `/run/trial` 报。
    把两者混在一个分支里最容易出错：会渲染出一张没有任何按钮的空卡。
    """
    etype = str(ev.get("type") or "story")
    kind = str(ev.get("kind") or "")
    chosen = str(ev.get("chosen") or "")
    relic = bool(ev.get("relic"))
    cls = "ecard " + ("fork" if kind == "fork" else etype)
    if relic:
        cls += " relic"
    if chosen:
        cls += " done"

    label = "旧物" if relic else (KIND_ZH.get(kind) or TYPE_ZH.get(etype) or "事件")
    # 试炼不写「已选」：它的结论是「核验通过 / 未核验」，下面那行已经说了，
    # 头上再挂一个「已选」会让「试炼已选」连成一串，看着像个状态而不是标签。
    when = ("这一刻要定" if (kind == "fork" and not chosen)
            else ("已选" if (chosen and etype != "trial") else ""))
    head = (f'<span class="tag">{esc(label)}</span>'
            + (f'<span class="when">{esc(when)}</span>' if when else "")
            + f'<h3>{esc(ev.get("title") or "")}</h3>')

    body = f'<div class="lit">{esc(ev.get("text") or "")}</div>'
    tail = ""
    if etype == "trial":
        q = ev.get("quality") or {}
        metric = METRIC_ZH.get(str(q.get("metric") or ""), "")
        km = q.get("distance_km")
        req = " · ".join(x for x in ((f"{km:g} km" if km else ""), metric) if x)
        if req:
            tail += f'<div class="treq">{_icon("target", 13)}<span>{esc(req)}</span></div>'
        if chosen:
            tail += f'<div class="res">{esc(ev.get("result") or "")}</div>'
            lv = str(ev.get("level") or "")
            if lv == "verified":
                verdict, vcls = "核验通过 · 这段记录对得上", "verified"
            elif lv == "unverified":
                verdict, vcls = "未核验 · 没找到记录，格数只算一半", "claimed"
            elif lv == "failed" or chosen == "fail":
                verdict, vcls = "没跑成", "failed"
            else:
                verdict, vcls = "已记录", "claimed"
            tail += (f'<div class="lev {vcls}">{_icon("check", 12)}'
                     f"<span>{esc(verdict)}</span></div>")
        else:
            tid = json.dumps(str(ev.get("id") or ""))
            tail += ('<div class="tbtns">'
                     f'<button class="ok" onclick="rcTrial({tid},true)">跑完了</button>'
                     f'<button onclick="rcTrial({tid},false)">没跑成</button>'
                     "</div>"
                     '<div class="thint">会拿本周的活动记录核一遍：'
                     "没有记录就按未核验记，格数只算一半。</div>")
    elif chosen:
        res = str(ev.get("result") or "")
        if res:
            tail += f'<div class="res">{esc(res)}</div>'
        tail += ('<div class="gain">' + _icon("check", 12)
                 + "<span>已选 · 这一步算数</span></div>")
    else:
        tail += _options_html(ev, slot)

    return f'<div class="{cls}"><div class="in">{head}{body}{tail}</div></div>'


# ---------------------------------------------------------------- 页面
def run_page(token: str, message: str = "", data: dict | None = None) -> str:
    """远征主页。`data` 是 `/v1/group/{id}/run/state` 的返回。"""
    if not data or not data.get("ok"):
        err = (data or {}).get("error") or "还没有赛季数据"
        body = (f'<div class="empty">远征还没开始。<br>{esc(err)}'
                '<br><a class="btn" href="/groups">回到小组</a></div>')
        return ui.page("EXPEDITION", "远征", "雪线之前", body,
                       css=FONT_CSS + CSS, back=("返回", "/groups"))

    s = data.get("season") or {}
    prog = data.get("progress") or {}
    mine = data.get("me") or {}
    act = int(s.get("act") or 1)
    acts = int(s.get("acts") or 4)
    stage = s.get("stage") or {}
    stops = [str(x) for x in (s.get("stops") or [])]
    # 两套数：整季累计（stage_hit 用）与本幕（小旗与「这周还差多少」用）
    st_team = float(prog.get("stage_team") or 0)
    st_target = float(prog.get("stage_target") or 0)
    frac = (st_team / st_target) if st_target > 0 else 0.0

    morale = int(s.get("morale") or 0)
    supply = int(mine.get("supply") or 0)
    status = str(mine.get("status") or "moving")
    pos = int(mine.get("pos") or 0)
    wt = float(mine.get("week_target_effective") or 0)

    # ---- 页头（复用 page() 的标题结构，山脊作为装饰层塞进去）
    title = f"第{CN_NUM[act - 1] if 1 <= act <= 4 else act}幕 · {stage.get('theme') or ''}"
    road = ""
    if stage.get("from") and stage.get("to"):
        road = f"{stage['from']} → {stage['to']}"
    chips = ui.chip(f"幕 {act} / {acts}")
    chips += ui.chip(f"士气 {morale}", "warn" if morale <= 3 else "")
    chips += ui.chip(f"{int(data.get('members') or 0)} 人在走")

    # ---- 状态条
    low = "zero" if supply <= 0 else ("low" if supply < 2 else "")
    bar = (f'<div class="rbar">'
           f'<span class="it">{_icon("fire")}<span>士气</span><b>{morale}</b></span>'
           '<span class="sep"></span>'
           f'<span class="it {low}">{_icon("flask")}<span>补给</span><b>{supply}</b></span>'
           '<span class="sep"></span>'
           f'<span class="it">{_icon("foot")}<span>行程</span><b>{pos}</b>'
           '<span>格</span></span>'
           f'<span class="sp"></span><span class="note">目标 {wt:g} km</span></div>')

    # ---- 四段路
    pct = max(0.0, min(100.0, frac * 100))
    left = st_target - st_team
    route = (f'<div class="route"><div class="rh">'
             f'<span style="font-size:11.5px;font-weight:700;letter-spacing:1.4px;'
             f'color:var(--muted)">这一路</span>'
             f'<span style="font-size:11.5px;color:var(--muted)">这一段 '
             f'<b style="color:var(--ink);font-size:13px">{st_team:g}</b>'
             f' / {st_target:g} 格</span></div>'
             f'{route_svg(stops, act, frac)}'
             f'<div class="gap"><i style="width:{pct:.1f}%"></i></div>'
             f'<div class="rm">'
             + (f"还差 {left:.1f} 格" if left > 0 else "这一段已经够数")
             + ' · 周日结算</div></div>')

    # ---- 事件
    cards = ""
    fork = data.get("fork")
    if fork:
        cards += event_card(fork, -1)
    for i, ev in enumerate(data.get("events") or []):
        cards += event_card(ev, i)
    if not cards:
        cards = ('<div class="ecard story"><div class="in">'
                 '<div class="lit">这一段还没有事发生。跑起来，路会自己长出内容。'
                 "</div></div></div>")

    # ---- 受困与救援
    stuck = ""
    stranded = data.get("stranded") or []
    if status == "stranded":
        stuck = ('<div class="stuck"><div class="t">你被困住了</div>'
                 '<div class="d">补给见底又走了冒险的那条路。这一周不产格，'
                 "等队友来救——24 小时内有人到，这一趟就还能算。</div></div>")
    elif stranded:
        stuck = ('<div class="stuck"><div class="t">有人困在路上了</div>'
                 f'<div class="d">{len(stranded)} 人在等你。去接他要花掉自己'
                 " 1 格行程和 1 份补给，换来全队士气 +1。</div>")
        for x in stranded[:3]:
            uid = int(x.get("user_id") or 0)
            stuck += f'<button onclick="rcRescue({uid})">去接他</button>'
        stuck += "</div>"

    # ---- 本周目标
    tgt = (f'<div class="card" style="margin-top:18px">'
           '<div class="kvline"><span>本周目标</span>'
           f'<b>{wt:g} km</b></div>'
           '<p class="note">报一个低于计划的量，这周按它算格数——'
           "减量周不该因为跑得少就走不动。只能调小。</p>"
           f'<form action="/__run/target" method="post" style="margin-top:6px">'
           f'<input type="hidden" name="token" value="{esc(token)}">'
           '<div class="kvline"><span>改成</span><b>'
           '<input type="number" inputmode="decimal" name="km" min="0" max="200"'
           ' step="1" placeholder="km"></b></div>'
           '<div class="pager"><button class="btn" type="submit">保存</button></div>'
           "</form></div>")

    msg = f'<div class="note-box">{esc(message)}</div>' if message else ""
    body = msg + bar + route + cards + stuck + tgt

    js = ("<script>var TK=" + json.dumps(token) + ";"
          "function post(p,d){var f=new FormData();f.append('token',TK);"
          "for(var k in d){f.append(k,d[k]);}"
          "fetch(p,{method:'POST',body:f,credentials:'same-origin'})"
          ".then(function(){location.reload()})"
          ".catch(function(){location.reload()});}"
          "function rcPick(s,o){post('/__run/event',{slot:String(s),option:o});}"
          "function rcFork(o){post('/__run/fork',{option:o});}"
          "function rcTrial(i,d){post('/__run/trial',{trial_id:i,done:d?'1':'0'});}"
          "function rcRescue(u){post('/__run/rescue',{user_id:String(u)});}"
          "</script>")

    return ui.page("EXPEDITION · 雪线之前", title, road, body,
                   chips=chips, css=FONT_CSS + CSS,
                   head_bg=f'<div id="runbg">{ridge_svg()}</div>',
                   foot="进度来自你上报的里程，不上传轨迹与位置。",
                   back=("返回", "/groups")).replace("</body>", js + "</body>")


# ---------------------------------------------------------------- 结算
def _ending_svg(key: str) -> str:
    """结局面面：同一片雪地，用脚印的去向和屋里的灯光分四种结局。

    不用四张不同的插画，是因为「同一处地方、不同的到达方式」才是这个玩法
    的意思——玩家换一季再来，看见的还是这片雪地，变的是自己走成了什么样。
    """
    cabin = ('<rect x="262" y="74" width="66" height="38" rx="2" fill="#1B2739"'
             ' stroke="rgba(220,231,245,.28)" stroke-width="1"/>'
             '<path d="M256 74 l39 -22 l39 22 z" fill="#151F2E"'
             ' stroke="rgba(220,231,245,.3)" stroke-width="1" stroke-linejoin="round"/>')
    # 窗：唯一的暖光。四结局里只有「抵达」和「发现者」亮着
    lit = ('<rect x="290" y="88" width="11" height="9" fill="#C98B4B" opacity=".9"/>'
           if key in ("arrive", "discoverer") else
           '<rect x="290" y="88" width="11" height="9" fill="#0B111B"'
           ' stroke="rgba(220,231,245,.2)" stroke-width=".8"/>')

    def foot(x, y, op=1.0):
        # 脚印要看得见才有「有人走过」的意思。之前 rx=2.6、透明度从 0.3 起，
        # 在 420 宽的手机上几乎是一层噪点。
        return (f'<ellipse cx="{x:.1f}" cy="{y:.1f}" rx="3.4" ry="1.9"'
                f' fill="#DCE7F5" opacity="{min(0.95, op):.2f}"/>')

    prints = []
    ground = '<path d="M8 111 H364" stroke="rgba(220,231,245,.16)" stroke-width="1"/>'
    snow = ('<circle cx="60" cy="34" r="1.2" fill="#DCE7F5" opacity=".3"/>'
            '<circle cx="128" cy="52" r="1" fill="#DCE7F5" opacity=".22"/>'
            '<circle cx="196" cy="30" r="1.3" fill="#DCE7F5" opacity=".26"/>'
            '<circle cx="236" cy="58" r="1" fill="#DCE7F5" opacity=".2"/>'
            '<circle cx="330" cy="44" r="1.1" fill="#DCE7F5" opacity=".24"/>')

    if key == "arrive":
        for i in range(13):
            prints.append(foot(26 + i * 17, 106 - i * 1.3, 0.6 + i * 0.03))
    elif key == "detour":
        # 三条散开的脚印，只有最右那条走到了门口
        for i in range(9):
            prints.append(foot(26 + i * 15, 108 - i * 0.8, 0.62))
        for i in range(6):
            prints.append(foot(26 + i * 15, 100 + i * 2.6, 0.5))
        for i in range(5):
            prints.append(foot(26 + i * 15, 116 - i * 1.4, 0.44))
        for i in range(6):
            prints.append(foot(168 + i * 16, 96 - i * 1.1, 0.66))
    elif key == "lost":
        # 走到一半就断了，后面什么都没有。
        # 断的地方放一块石头——文案里写的是「退回背风的那块石头下面」，
        # 图里没有它，那句话就是空的。
        for i in range(8):
            prints.append(foot(26 + i * 15, 108 - i * 1.1, 0.66 - i * 0.045))
        prints.append('<path d="M158 111 L166 96 L180 104 L186 111 Z"'
                      ' fill="#1B2739" stroke="rgba(220,231,245,.24)"'
                      ' stroke-width="1" stroke-linejoin="round"/>')
    else:  # discoverer
        for i in range(12):
            prints.append(foot(24 + i * 17, 106 - i * 1.5, 0.5 + i * 0.035))
        # 三本叠着的日志，压在路边
        prints += [
            '<rect x="44" y="96" width="26" height="7" rx="1" fill="#1B2739"'
            ' stroke="rgba(201,139,75,.55)" stroke-width=".9"/>',
            '<rect x="47" y="89" width="26" height="7" rx="1" fill="#16202F"'
            ' stroke="rgba(201,139,75,.4)" stroke-width=".9"/>',
            '<rect x="50" y="82" width="26" height="7" rx="1" fill="#121A28"'
            ' stroke="rgba(201,139,75,.28)" stroke-width=".9"/>',
        ]

    return ('<svg viewBox="0 0 372 132" class="endsvg" role="img"'
            f' aria-label="结局示意图：{esc(key)}">'
            + snow + ground + cabin + lit + "".join(prints) + "</svg>")


def outcome_page(token: str, data: dict | None = None) -> str:
    """结算页。四个结局共用一套版式——变的是文案和脚印的去向。"""
    d = data or {}
    detail = d.get("ending_detail") or {}
    key = str(d.get("ending") or "lost")
    title = str(detail.get("title") or "结束了")
    line = str(detail.get("line") or "")
    text = str(detail.get("text") or "")

    team = float(d.get("team") or 0)
    morale = int(d.get("morale") or 0)
    members = int(d.get("members") or 0)
    relics = int(d.get("relic_count") or 0)
    season = str(d.get("season") or "雪线之前")

    # 逐幕：显示「走够了没有」，不打分。远征不是考试，这里只是回放
    acts = ""
    for a in d.get("acts") or []:
        got = float(a.get("got") or 0)
        need = float(a.get("target") or 0)
        hit = bool(a.get("hit"))
        if need <= 0:
            # 这一段根本没结算过（赛季提前终止、或 cron 没跑到）。
            # 显示「0/0 没够」是在骗人——它不是没走够，是没走到。
            mark, cls, val = "没走到", "arow miss", "—"
        else:
            mark = "走够" if hit else "没够"
            cls = "arow ok" if hit else "arow"
            val = f'{got:g}<i>/{need:g}</i>'
        acts += (f'<div class="{cls}"><span class="an">'
                 f'第{CN_NUM[int(a.get("act") or 1) - 1]}段</span>'
                 f'<span class="at">{esc(a.get("theme") or "")}</span>'
                 f'<span class="av">{val}</span>'
                 f'<span class="am">{mark}</span></div>')

    body = (f'<div class="endwrap">{_ending_svg(key)}'
            f'<div class="endk">SEASON OVER · {esc(season)}</div>'
            f'<h1 class="endh">{esc(title)}</h1>'
            f'<div class="endl">{esc(line)}</div>'
            f'<div class="lit endt">{esc(text)}</div>'
            '<div class="enddata">'
            f'<div><b>{team:g}</b><span>总行程</span></div>'
            f'<div><b>{morale}</b><span>士气</span></div>'
            f'<div><b>{members}</b><span>走过的人</span></div>'
            f'<div><b>{relics}<i>/3</i></b><span>旧日志</span></div>'
            "</div>"
            f'<div class="endacts">{acts}</div>'
            '<p class="note" style="margin-top:14px">下一季周一自动开始。'
            "队还在，路会换一段。</p>"
            '<div class="pager"><a class="btn" href="/groups">回到小组</a></div>'
            "</div>")

    # 页头不放 h1：结算页的大标题是结局名，页头再来一个「这一季」
    # 就是两个并列的主标题，谁也不是谁的上级。
    return ui.page("EXPEDITION · 雪线之前", "", "", body,
                   css=FONT_CSS + END_CSS,
                   foot="进度来自你上报的里程，不上传轨迹与位置。",
                   back=("返回", "/groups"))


END_CSS = """
.endwrap{padding:6px 0 4px;text-align:center}
.endsvg{width:100%;height:auto;display:block;margin:0 auto}
.endk{margin-top:14px;font-size:10.5px;font-weight:700;letter-spacing:2px;color:var(--cyan)}
.endh{margin:8px 0 0;font-size:26px;font-weight:650;letter-spacing:-.3px;line-height:1.3}
.endl{margin-top:7px;font-size:12.5px;color:var(--muted)}
/* 结局正文：整页最重的一段字，所以给它最宽的行距和最慢的颜色 */
.endt{margin:17px auto 0;max-width:330px;text-align:left;
  font-size:14.5px;line-height:1.95;color:#C6D4E6}
.enddata{display:grid;grid-template-columns:repeat(4,1fr);gap:9px;margin-top:22px;
  padding-top:18px;border-top:1px solid var(--line2)}
.enddata div{display:flex;flex-direction:column;gap:3px}
.enddata b{font-size:17px;font-weight:600;color:var(--ink);
  font-variant-numeric:tabular-nums}
.enddata b i{font-style:normal;font-size:11px;color:var(--faint);margin-left:1px}
.enddata span{font-size:10.5px;color:var(--faint);line-height:1.35}
.endacts{margin-top:20px;text-align:left}
.arow{display:grid;grid-template-columns:52px 1fr 62px 40px;gap:8px;
  align-items:center;padding:9px 0;border-bottom:1px solid var(--line2);
  font-size:12px;color:var(--muted)}
.arow:last-child{border-bottom:0}
.arow .an{font-size:10.5px;color:var(--faint)}
.arow .at{color:var(--ink2)}
.arow .av{text-align:right;font-variant-numeric:tabular-nums;color:var(--ink2)}
.arow .av i{font-style:normal;color:var(--faint);font-size:10.5px}
.arow .am{text-align:right;font-size:10.5px;color:var(--faint)}
.arow.ok .am{color:#5EE7F5}
.arow.miss .at{color:var(--faint)}
.arow.miss .av{color:var(--faint)}
.arow.ok .at{color:var(--ink)}
"""
