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
# 这套 token 是双向的唯一来源：改这里要同步 aiui.py 的 :root 与 MainActivity 的
# 颜色常量（docs/交互树.md 第五节有对照表）。
CSS = """
:root{
  /* 深空色：不用纯黑，带一点蓝，屏幕上有层次；辉光统一走 --glow */
  --bg:#070B12; --surface:#0F1622; --surface2:#131C2B;
  --line:rgba(96,165,250,.16); --line2:rgba(96,165,250,.09);
  --ink:#E8F0FB; --ink2:#B7C7DD; --muted:#7C8FA8; --faint:#4E6076;
  --brand:#3B82F6; --brand-soft:rgba(59,130,246,.14); --brand-ink:#8CC2FF;
  --cyan:#22D3EE; --cyan-soft:rgba(34,211,238,.13);
  --amber:#F5A524; --amber-soft:rgba(245,165,36,.14);
  --red:#F2555A; --red-soft:rgba(242,85,90,.14);
  --blue:#5EA0FF; --blue-soft:rgba(94,160,255,.14);
  --r:16px;
  --shadow:inset 0 1px 0 rgba(255,255,255,.035), 0 14px 34px -22px rgba(0,0,0,.9);
  --glow:0 0 0 1px rgba(96,165,250,.10), 0 10px 30px -16px rgba(59,130,246,.55);
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{-webkit-text-size-adjust:100%}
body{margin:0;color:var(--ink);line-height:1.6;font-size:14px;
  background:
    radial-gradient(900px 520px at 78% -8%, rgba(59,130,246,.17), transparent 62%),
    radial-gradient(680px 420px at 6% 2%, rgba(34,211,238,.10), transparent 60%),
    var(--bg);
  background-attachment:fixed;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased}
a{color:var(--brand-ink);text-decoration:none}
.app{max-width:820px;margin:0 auto;padding:0 14px calc(30px + env(safe-area-inset-bottom))}
.back{display:inline-flex;align-items:center;gap:5px;margin:14px 0 -6px;
  font-size:12.5px;color:var(--brand-ink);text-decoration:none}
.back:active{opacity:.6}
/* 页头只用一道渐隐的蓝线收尾，比整条实线更像仪表面板 */
.page-head{padding:20px 0 15px;margin-bottom:4px;
  background:linear-gradient(90deg,rgba(59,130,246,.60),rgba(34,211,238,.12) 46%,transparent)
    bottom / 100% 1px no-repeat}
.page-head.after-back{padding-top:14px}
.kicker{font-size:11px;font-weight:700;letter-spacing:2px;color:var(--cyan);
  text-shadow:0 0 12px rgba(34,211,238,.45)}
.page-head h1{margin:6px 0 4px;font-size:24px;font-weight:650;letter-spacing:-.5px;line-height:1.3}
.lede{margin:0;font-size:13px;color:var(--muted)}
.meta{display:flex;gap:6px;flex-wrap:wrap;margin-top:12px}
.chip{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;padding:4px 10px;
  border-radius:999px;background:rgba(15,22,34,.72);border:1px solid var(--line);color:var(--ink2)}
.chip.ok{background:var(--brand-soft);border-color:rgba(59,130,246,.34);color:var(--brand-ink)}
.chip.warn{background:var(--amber-soft);border-color:rgba(245,165,36,.32);color:#F5C26B}
.sec{margin-top:24px}
.sec-head{display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin:0 0 10px}
.sec-head h2{margin:0;font-size:11.5px;font-weight:700;letter-spacing:1.4px;
  color:var(--muted);position:relative;padding-left:12px}
.sec-head h2::before{content:"";position:absolute;left:0;top:50%;width:3px;height:12px;
  margin-top:-6px;border-radius:2px;
  background:linear-gradient(180deg,var(--cyan),var(--brand));
  box-shadow:0 0 10px rgba(34,211,238,.7)}
.sec-head .more{font-size:12px;color:var(--brand-ink)}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.grid.three{grid-template-columns:repeat(3,minmax(0,1fr))}
.grid.one{grid-template-columns:1fr}
.card{background:linear-gradient(180deg,rgba(19,28,43,.92),rgba(14,21,33,.95));
  border:1px solid var(--line);border-radius:var(--r);padding:16px;
  box-shadow:var(--shadow);position:relative;overflow:hidden}
/* 卡片顶沿一道高光：深色背景上不做这个，卡片会糊成一片 */
.card::after{content:"";position:absolute;left:0;right:0;top:0;height:1px;
  background:linear-gradient(90deg,transparent,rgba(96,165,250,.48),transparent);
  opacity:.75;pointer-events:none}
.card.flat{background:transparent;border-style:dashed;box-shadow:none}
.card.flat::after{display:none}
.card.brand{background:
  radial-gradient(340px 190px at 84% -10%, rgba(59,130,246,.32), transparent 70%),
  linear-gradient(180deg,rgba(20,32,52,.96),rgba(12,19,31,.96));
  border-color:rgba(96,165,250,.30);box-shadow:var(--glow)}
.card h3{margin:0 0 10px;font-size:11.5px;font-weight:700;letter-spacing:1.2px;color:var(--muted)}
.hero{display:flex;align-items:flex-end;justify-content:space-between;gap:14px;flex-wrap:wrap}
/* 大数字走「白→蓝」渐变 + 投影辉光，深色底上最抓眼 */
.big{font-size:46px;font-weight:700;line-height:1;letter-spacing:-2px;
  font-variant-numeric:tabular-nums;
  background:linear-gradient(180deg,#FFFFFF,#8CC2FF 78%);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  filter:drop-shadow(0 0 16px rgba(59,130,246,.45))}
.big small{font-size:14px;font-weight:500;letter-spacing:0;color:var(--muted);
  margin-left:6px;-webkit-text-fill-color:var(--muted);filter:none}
.hero-side{text-align:right;font-size:12px;color:var(--muted)}
.hero-side b{display:block;font-size:15px;color:var(--ink)}
.bar{height:6px;border-radius:999px;background:rgba(96,165,250,.12);overflow:hidden;margin-top:14px}
.bar i{display:block;height:100%;border-radius:999px;
  background:linear-gradient(90deg,var(--cyan),var(--brand));
  box-shadow:0 0 14px rgba(59,130,246,.75)}
.lab{font-size:11.5px;color:var(--muted);letter-spacing:.2px}
.val{font-size:23px;font-weight:650;line-height:1.2;margin-top:4px;
  font-variant-numeric:tabular-nums;color:var(--ink)}
.note{font-size:11.5px;color:var(--faint);margin-top:3px}
.row{display:flex;align-items:center;gap:12px;
  background:linear-gradient(180deg,rgba(19,28,43,.88),rgba(14,21,33,.92));
  border:1px solid var(--line);border-radius:14px;padding:12px 14px;margin-bottom:8px;
  color:inherit;cursor:pointer;box-shadow:var(--shadow)}
.row:active{background:rgba(59,130,246,.10);border-color:rgba(96,165,250,.34)}
/* 当前选中的周期行：左侧一道蓝条 + 蓝底。滚动列表里要一眼看出
   「现在这张柱状图讲的是哪个月」，否则点完还是一排一模一样的行。 */
.row.on{background:linear-gradient(180deg,rgba(59,130,246,.20),rgba(34,211,238,.07));
  border-color:rgba(96,165,250,.52);
  box-shadow:inset 3px 0 0 #3B82F6, 0 12px 30px -22px rgba(59,130,246,.9)}
.row.on .t{color:#DCEBFF}
.row.on .go{color:#8CC2FF}
/* 图标底座：蓝青渐变 + 内发光，是整页「科技感」的主要来源 */
.row .ic{flex:0 0 auto;width:36px;height:36px;border-radius:11px;display:flex;
  align-items:center;justify-content:center;font-size:15px;font-weight:600;
  background:linear-gradient(160deg,rgba(59,130,246,.26),rgba(34,211,238,.10));
  color:#9CCBFF;border:1px solid rgba(96,165,250,.24);
  box-shadow:inset 0 0 12px rgba(59,130,246,.20)}
.row .ic.b{background:linear-gradient(160deg,rgba(94,160,255,.28),rgba(59,130,246,.10));
  color:#A8CDFF;border-color:rgba(94,160,255,.30)}
.row .ic.a{background:linear-gradient(160deg,rgba(245,165,36,.24),rgba(245,165,36,.08));
  color:#F5C26B;border-color:rgba(245,165,36,.28)}
.row .tx{min-width:0;flex:1}
/* 标题最多两行：活动名是「日期 时间 · 名称」拼出来的，长一点就被截成
   省略号，看不出是哪次。参数行本来就会换行，不用 nowrap。 */
.row .t{font-size:14px;font-weight:600;line-height:1.35;
  display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;
  overflow:hidden;word-break:break-word}
.row .d{font-size:11.5px;color:var(--muted);margin-top:2px;font-variant-numeric:tabular-nums}
.row .go{margin-left:auto;color:var(--faint);font-size:17px;flex:0 0 auto}
.acts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.act{display:flex;flex-direction:column;gap:7px;
  background:linear-gradient(180deg,rgba(19,28,43,.88),rgba(14,21,33,.92));
  border:1px solid var(--line);border-radius:var(--r);padding:15px;color:inherit;
  cursor:pointer;box-shadow:var(--shadow)}
.act:active{background:rgba(59,130,246,.10);border-color:rgba(96,165,250,.34)}
.act .ai{width:34px;height:34px;border-radius:10px;display:flex;align-items:center;
  justify-content:center;font-size:16px;
  background:linear-gradient(160deg,rgba(34,211,238,.22),rgba(59,130,246,.12));
  color:#8FE7F5;border:1px solid rgba(34,211,238,.24);
  box-shadow:inset 0 0 12px rgba(34,211,238,.18)}
.act .at{font-size:13.5px;font-weight:600;color:var(--ink)}
.act .ad{font-size:11.5px;color:var(--muted);line-height:1.5}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;
  border:1px solid var(--line);background:rgba(15,22,34,.72);color:var(--ink2);
  border-radius:999px;padding:8px 14px;font-size:12.5px;font-family:inherit;cursor:pointer}
.btn.primary{background:linear-gradient(135deg,#3B82F6,#22D3EE);border-color:transparent;
  color:#04121F;font-weight:650;box-shadow:0 8px 22px -10px rgba(59,130,246,.85)}
.btn:active{opacity:.85}
.empty{padding:20px 4px;font-size:13px;color:var(--muted);line-height:1.8}
.foot{margin-top:30px;padding-top:14px;border-top:1px solid var(--line2);
  font-size:11px;color:var(--faint);line-height:1.8}
form.filters{display:flex;gap:9px;flex-wrap:wrap;align-items:center}
form.filters label{display:inline-flex;gap:5px;align-items:center;font-size:11.5px;color:var(--muted)}
input,select{font-family:inherit;font-size:12.5px;color:var(--ink);
  background:rgba(9,14,22,.9);border:1px solid var(--line);border-radius:9px;
  padding:7px 9px;max-width:100%}
input:focus,select:focus{outline:none;border-color:rgba(96,165,250,.55);
  box-shadow:0 0 0 3px rgba(59,130,246,.16)}
.pager{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:12px}
.tags{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
/* 训练计划：今日课表 + 本周 7 天 */
.plan-hero{background:linear-gradient(180deg,rgba(19,28,43,.92),rgba(14,21,33,.95));
  border:1px solid var(--line);border-radius:var(--r);
  padding:18px;box-shadow:var(--shadow);position:relative;overflow:hidden}
.plan-hero::after{content:"";position:absolute;left:0;right:0;top:0;height:1px;
  background:linear-gradient(90deg,transparent,rgba(96,165,250,.48),transparent);opacity:.75}
.plan-hero .t{font-size:20px;font-weight:650;line-height:1.35;letter-spacing:-.2px}
.plan-hero .d{font-size:13px;color:var(--ink2);margin-top:6px;line-height:1.7}
.plan-hero .n{font-size:11.5px;color:var(--muted);margin-top:10px}
.plan-hero .n + .n{margin-top:4px}
.wk{display:flex;flex-direction:column;gap:7px}
.wrow{display:flex;align-items:center;gap:11px;
  background:linear-gradient(180deg,rgba(19,28,43,.88),rgba(14,21,33,.92));
  border:1px solid var(--line);border-radius:14px;padding:11px 13px;box-shadow:var(--shadow)}
.wrow .wd{flex:0 0 40px;font-size:12px;color:var(--muted)}
.wrow .wb{flex:1;min-width:0;font-size:13.5px;font-weight:600;line-height:1.45}
.wrow .wb em{display:block;font-style:normal;font-size:11.5px;color:var(--muted);
  font-weight:400;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wrow.today{background:linear-gradient(90deg,rgba(59,130,246,.18),rgba(59,130,246,.04));
  border-color:rgba(96,165,250,.34);box-shadow:0 0 24px -12px rgba(59,130,246,.9)}
.wrow.today .wd{color:var(--brand-ink)}
.wrow .now{flex:0 0 auto;font-size:10.5px;color:#04121F;font-weight:700;
  background:linear-gradient(135deg,#3B82F6,#22D3EE);
  border-radius:999px;padding:2px 8px;box-shadow:0 0 12px -2px rgba(59,130,246,.8)}
.wrow .done{flex:0 0 auto;font-size:10.5px;color:var(--brand-ink);
  background:rgba(59,130,246,.12);border:1px solid rgba(96,165,250,.26);
  border-radius:999px;padding:2px 8px}
.wrow.rest .wb{color:var(--muted);font-weight:400}
.kvline{display:flex;justify-content:space-between;gap:12px;font-size:12.5px;
  color:var(--ink2);padding:7px 0;border-bottom:1px solid var(--line2)}
.kvline:last-child{border-bottom:0}
.kvline b{font-weight:600;color:var(--ink);font-variant-numeric:tabular-nums}
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
  background:rgba(7,11,18,.96);border-bottom:1px solid rgba(96,165,250,.18);
  display:flex;gap:7px;align-items:center;flex-wrap:wrap;
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
#gc-nav a,#gc-nav span,#gc-nav button{font-size:12px;text-decoration:none;padding:5px 10px;
  border-radius:999px;background:rgba(19,28,43,.9);border:1px solid rgba(96,165,250,.18);
  color:#E8F0FB;font-family:inherit;cursor:pointer;white-space:nowrap}
#gc-nav a.back{border:0;background:none;color:#8CC2FF;font-weight:600;padding-left:0}
#gc-nav button{background:rgba(59,130,246,.16);border-color:rgba(96,165,250,.32);
  color:#8CC2FF;font-weight:600}
#gc-nav span.off{opacity:.38}
#gc-nav input{font-size:12px;padding:5px 7px;border:1px solid rgba(96,165,250,.20);
  border-radius:8px;background:rgba(9,14,22,.9);color:#E8F0FB;font-family:inherit}
#gc-nav form{display:inline-flex;gap:5px;align-items:center;margin:0}
#gc-nav .tag{margin-left:auto;border:0;background:none;color:#7C8FA8}
"""

# AI 页自带完整 CSS，只统一外框、卡片层次与标题节奏，不动它内部的交互。
AI_FIX = """
.wrap{max-width:820px;padding:0 14px calc(30px + env(safe-area-inset-bottom))}
.top{padding-top:16px;margin-bottom:12px}
.top h1{font-size:21px}
.card{border-radius:16px;border-color:rgba(96,165,250,.16);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.035),
    0 14px 34px -22px rgba(0,0,0,.9)}
/* tabs 槽底比页面再深一点，选中项才浮得起来 */
.tabs{background:rgba(9,14,22,.9);border:1px solid rgba(96,165,250,.12)}
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
  /* 同页参数变化（日期 / 筛选 / 分页）一律 replace：不往历史栈堆条目，
     返回键才能保持「二级页 → 所属 tab → 今日 → 退出」这条单一链路。 */
  document.addEventListener('click', function(e){
    var t = e.target, a = null;
    if (t && t.closest) a = t.closest('a[data-nopush]');
    if (!a) return;
    var href = a.getAttribute('href');
    if (!href) return;
    e.preventDefault();
    location.replace(href);
  }, false);
  document.addEventListener('submit', function(e){
    var f = e.target;
    if (!f || !f.hasAttribute || !f.hasAttribute('data-nopush')) return;
    e.preventDefault();
    var qs = new URLSearchParams(new FormData(f)).toString();
    location.replace((f.getAttribute('action') || location.pathname) + (qs ? '?' + qs : ''));
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
         chips: str = "", foot: str = "数据只保存在本机，不上传任何第三方。",
         back: tuple[str, str] | None = None) -> str:
    """所有 tab 页与二级页的统一外壳。

    `back` 只给二级页（/view、/files）用：一级 tab 之间靠底部导航切换，
    页内不再放返回链接，避免和底部 tab 形成两条并行导航。
    """
    back_html, head_cls = "", ""
    if back:
        back_html = f'<a class="back" href="{esc(back[1])}">← {esc(back[0])}</a>'
        head_cls = " after-back"
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#070B12">
<title>{esc(title)} · RunnerCoach</title>
<style>{CSS}</style></head><body><div class="app">
{back_html}
<header class="page-head{head_cls}">
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


def _today_load_txt(load: dict, day: str) -> str:
    """当天训练负荷。

    `derive()` 里的 `load` 是 `metrics.training_status()` 的结果，字段是
    `loads`（按日期）/ `ctl` / `atl` / `tsb` / `acute_7d` …，**没有 `load`
    这个键**。首页以前读 `load["load"]`，取到的永远是 None，于是「今日负荷」
    一直渲染成「—」。历史里一天都没有才显示「—」；没练的那天就该显示 0。
    """
    loads = load.get("loads") or {}
    if not loads and load.get("acute_7d") is None:
        return "—"
    return f"{(loads.get(day) or 0):.0f}"


def _week_load_txt(load: dict) -> str:
    """近 7 天累计负荷（就是 training_status 的 acute_7d）。"""
    v = load.get("acute_7d")
    return "—" if v is None else f"{v:.0f}"


def stat(label: str, value, note: str = "", unit: str = "") -> str:
    u = f'<small style="font-size:12px;color:var(--muted)"> {esc(unit)}</small>' if unit else ""
    return (f'<div class="card"><div class="lab">{esc(label)}</div>'
            f'<div class="val">{esc(value)}{u}</div>'
            f'<div class="note">{esc(note)}</div></div>')


def row(icon: str, title: str, desc: str, *, act: str = "", href: str = "",
        aid: str = "", tone: str = "", go: bool = True,
        on: bool = False, nopush: bool = False) -> str:
    """列表行。

    `on=True` 表示「当前选中的那一条」，左侧加一道蓝条；
    `nopush=True` 走 `location.replace`，同页切参数不往历史栈里堆条目
    （否则连点五个月份，返回键要按五次才出得去）。
    """
    tag = "a" if href else "div"
    attr = f' href="{esc(href)}"' if href else ""
    if href and nopush:
        attr += " data-nopush"
    if act:
        attr += f' data-act="{esc(act)}"'
    if aid:
        attr += f' data-aid="{esc(aid)}"'
    arrow = '<span class="go">›</span>' if go else ""
    cls = "row on" if on else "row"
    return (f'<{tag} class="{cls}"{attr}><span class="ic {esc(tone)}">{esc(icon)}</span>'
            f'<span class="tx"><span class="t">{esc(title)}</span>'
            f'<span class="d">{esc(desc)}</span></span>{arrow}</{tag}>')


def act(icon: str, title: str, desc: str, action: str = "", href: str = "") -> str:
    tag = "a" if href else "div"
    attr = f' href="{esc(href)}"' if href else f' data-act="{esc(action)}"'
    return (f'<{tag} class="act"{attr}><span class="ai">{esc(icon)}</span>'
            f'<span class="at">{esc(title)}</span>'
            f'<span class="ad">{esc(desc)}</span></{tag}>')


def nav_html(day: str, cov: dict, from_: str = "history") -> str:
    """二级页（按日期看板）的上下文导航。

    只负责三件事：回到来源 tab、在这一天前后翻、跳到指定日期。
    不再出现「历史活动 / 首页」这类横向入口——那是底部 tab 的职责，
    两套导航并存正是以前「返回在 tab 之间循环」的根源。
    """
    first, last = cov.get("first") or day, cov.get("last") or day

    def shift(n: int) -> str:
        try:
            return (dt.date.fromisoformat(day) + dt.timedelta(days=n)).isoformat()
        except (TypeError, ValueError):
            return day

    src = "today" if from_ == "today" else "history"
    back_href, back_txt = ("/", "今日") if src == "today" else ("/history", "历史")
    prev, nxt = shift(-1), shift(1)
    p = (f'<a href="/view?date={prev}&from={src}" data-nopush>← 前一天</a>'
         if first <= prev <= last else '<span class="off">← 前一天</span>')
    n = (f'<a href="/view?date={nxt}&from={src}" data-nopush>后一天 →</a>'
         if first <= nxt <= last else '<span class="off">后一天 →</span>')
    return f"""<div id="gc-nav">
  <a class="back" href="{back_href}">← {back_txt}</a>
  {p}
  <form method="get" action="/view" data-nopush>
    <input type="hidden" name="from" value="{src}">
    <input type="date" name="date" min="{esc(first)}" max="{esc(last)}" value="{esc(day)}">
    <button type="submit">跳</button>
  </form>
  {n}
  <a href="/view?date={esc(last)}&from={src}" data-nopush>最新</a>
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
             stat("今日负荷", _today_load_txt(load, day),
                  "近 7 天累计 " + _week_load_txt(load)) +
             stat("CTL 体能", load.get("ctl") or "—", "42 天指数均线") +
             stat("TSB 状态", load.get("tsb") or "—", "体能 − 疲劳") +
             '</div>')

    quick = ('<div class="acts">' +
             act("▣", "导出今日一图流", "生成 1080×1920 竖图并分享", action="export") +
             act("◫", "完整看板", "睡眠 / 恢复 / 负荷 / 课表",
                 href=f"/view?date={day}&from=today") +
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

    # 今日课表：读同步落下的 plan.json，零请求；没有计划就不占版面。
    plan_html, plan_more = "", ""
    try:
        _pl = A.plan(False)
        if _pl.get("ok"):
            plan_html = _today_plan_card(_pl)
            plan_more = ("本周计划", "/plan?from=today")
    except Exception:
        plan_html = ""

    body = (sec("今日状态", hero + '<div style="height:10px"></div>' + stats) +
            (sec("今日课表", plan_html, plan_more) if plan_html else "") +
            sec("快捷操作", quick) +
            sec("最近活动", rows) +
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


def _plan_meta(item: dict) -> str:
    """一条课表的副标题：距离 · 时长 · 训练效果 · 运动类型。"""
    bits = []
    if item.get("km"):
        bits.append(f'{item["km"]} km')
    if item.get("minutes"):
        bits.append(f'{item["minutes"]} 分钟')
    if item.get("effect"):
        bits.append(str(item["effect"]))
    if item.get("sport") and item.get("sport") != "跑步":
        bits.append(str(item["sport"]))
    return " · ".join(bits) or "未标注强度"


def _plan_items_line(items: list[dict]) -> str:
    return "；".join(
        (x.get("name") or "训练") + (f' {x["minutes"]}′' if x.get("minutes") else "")
        for x in items) or "—"


def _today_plan_card(pl: dict) -> str:
    """今日课表卡片：有课就写课名 + 说明，没课就写休息和下一次。"""
    t = pl.get("today") or {}
    items = t.get("items") or []
    done = bool(t.get("done"))
    badge = '<div class="n" style="color:var(--brand-ink)">已完成</div>' if done else ""
    if items:
        it = items[0]
        more = "".join(
            f'<div class="n">另：{esc(x.get("name") or "训练")} · {esc(_plan_meta(x))}</div>'
            for x in items[1:])
        return (f'<div class="plan-hero"><div class="t">{esc(it.get("name") or "训练")}</div>'
                + (f'<div class="d">{esc(it.get("detail"))}</div>' if it.get("detail") else "")
                + f'<div class="n">{esc(_plan_meta(it))}</div>{badge}{more}</div>')
    nxt = pl.get("next_date")
    tip = (f'下一次：{esc(nxt)} · {esc(_plan_items_line(pl.get("next_items") or []))}'
           if nxt else "本周没有排课，按恢复度安排轻松活动")
    return (f'<div class="plan-hero"><div class="t">今天休息</div>'
            f'<div class="d">{tip}</div>{badge}</div>')


def plan_page(qs: dict | None = None) -> str:
    """训练计划二级页（属「今日」tab）：今日课表 + 本周 7 天 + 计划信息。

    数据来自佳明教练计划（同步时落的 `data/plan.json`）；高驰没有对外计划接口，
    这里不做兜底，没有计划就明确说「没读到」并给一个重新读取的按钮。
    """
    from . import appapi as A

    r = A.plan(False)
    if not r.get("ok"):
        body = ('<div class="card"><div class="empty"><b>还没读到训练计划</b>'
                f'{esc(r.get("error") or "先回今日页下拉同步一次")}</div>'
                '<div class="pager"><div class="btn primary" data-act="planRefresh">'
                '重新读取</div></div></div>')
        return page("PLAN · 训练计划", "今天练什么", "佳明教练计划",
                    sec("状态", body), chip("本机快照"), back=("返回今日", "/"))

    ph = r.get("phase") or {}
    if r.get("source") == "calendar":
        chips = (chip("佳明日历", "ok") +
                 chip(f'{r.get("start")} ~ {r.get("end")}') +
                 chip(f'本周 {7 - (r.get("rest_days_in_week") or 0)} 练'))
    else:
        chips = (chip("教练计划", "ok") +
                 (chip(str(r["level"])) if r.get("level") else "") +
                 (chip(f'{r["weeks"]} 周') if r.get("weeks") else "") +
                 (chip(f'当前 {ph["label"]}') if ph.get("label") else "") +
                 (chip(f'每周约 {r["per_week"]} 次') if r.get("per_week") else ""))

    today_iso = (r.get("today") or {}).get("date")
    rows = ""
    for d in r.get("week") or []:
        is_today = d["date"] == today_iso
        cls = "wrow today" if is_today else "wrow"
        if d.get("rest"):
            cls += " rest"
        if d.get("rest"):
            wb = '<span class="wb">休息</span>'
        else:
            first = (d["items"] or [{}])[0]
            wb = (f'<span class="wb">{esc(first.get("name") or "训练")}'
                  f'<em>{esc(_plan_meta(first))}</em></span>')
        if is_today:
            tag = '<span class="now">今天</span>'
        elif d.get("done"):
            tag = '<span class="done">已完成</span>'
        else:
            tag = ""
        rows += f'<div class="{cls}"><span class="wd">{esc(d["dow"])}</span>{wb}{tag}</div>'

    src_label = "佳明日历" if r.get("source") == "calendar" else (r.get("name") or "教练计划")
    info = ('<div class="card">'
            f'<div class="kvline"><span>来源</span><b>{esc(src_label)}</b></div>'
            f'<div class="kvline"><span>覆盖</span><b>{esc(r.get("start") or "—")}'
            f' ~ {esc(r.get("end") or "—")}</b></div>'
            + (f'<div class="kvline"><span>当前阶段</span><b>{esc(ph.get("label"))}</b></div>'
               if ph.get("label") else "")
            + f'<div class="kvline"><span>本周休息</span>'
              f'<b>{r.get("rest_days_in_week") or 0} 天</b></div>'
            '</div>')

    nxt = r.get("next_date")
    next_sec = ""
    if nxt:
        next_sec = sec("下一次训练",
                       f'<div class="card"><div class="kvline"><span>{esc(nxt)}</span>'
                       f'<b>{esc(_plan_items_line(r.get("next_items") or []))}</b></div></div>')

    body = (sec("今日", _today_plan_card(r)) +
            sec("本周", f'<div class="wk">{rows}</div>') +
            next_sec +
            sec("计划信息", info) +
            '<div class="pager"><div class="btn" data-act="planRefresh">刷新计划</div></div>')
    return page("PLAN · 训练计划", "今天练什么",
                (r.get("name") or "佳明教练计划"), body, chips,
                back=("返回今日", "/"))


def _map_on() -> bool:
    """活动图是否已配好底图 Key（只读配置，不联网）。"""
    try:
        from . import basemap as BM

        return BM.available()
    except Exception:
        return False


def _clip_count() -> int:
    """已登记的素材片段数（只读 JSON，不联网）。"""
    try:
        from . import appapi as A

        return len(A.clips_list().get("items") or [])
    except Exception:
        return 0


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

    # 每个数据源一行、点了直接进对应登录，不再先弹「选哪个账号」的二级选择器
    sources = (row("G", "佳明 Garmin", f"{g} · {esc(st.get('email') or '未设置账号')}",
                   act="login:garmin") +
               row("C", "高驰 COROS", f"{c_ok} · 可独立同步活动", act="login:coros", tone="b") +
               row("W", "小米体重", f"{m_ok} · 体脂秤数据", act="mi", tone="a"))

    # AI 设置只在教练页齿轮里改；这里只读展示，避免同一抽屉出现第二个入口
    try:
        from . import aiapi as AIAPI

        ai = AIAPI.dispatch("config.get", {}) or {}
    except Exception:
        ai = {}
    if ai.get("configured"):
        ai_txt = ((("平台额度" if ai.get("mode") == "platform" else "自配 API"))
                  + " · " + str(ai.get("model") or ""))
    else:
        ai_txt = "未配置"

    more = (row("◫", "产物文件", "看板 / 图片 / 视频清单", href="/files") +
            row("↻", "补拉更多历史",
                f"已覆盖 {cov['first']} ~ {cov['last']}，可再往前补",
                act="historySync", tone="a") +
            row("◍", "AI 模型服务", f"{ai_txt} · 在「教练」页右上角设置", tone="b", go=False) +
            row("⌖", "地图底图",
                ("已配置 · 活动图带高德底图" if _map_on()
                 else "未配置 · 活动图为矢量轨迹"), act="amap", tone="a") +
            row("✂", "素材",
                f"{_clip_count()} 个 · 图片 / 视频，回放时自动插入", act="clips", tone="b"))

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
            sec("更多", more) +
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
                sec("最近生成", rows), chip("只读"), back=("返回我的", "/me"))
