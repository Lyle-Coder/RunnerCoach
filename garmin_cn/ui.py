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
import re
import html
from pathlib import Path

from . import brand as BR

# 与 android values/colors.xml、aiui.CSS、report.CSS 保持同一组色值。
# 这套 token 是双向的唯一来源：改这里要同步 aiui.py 的 :root 与 MainActivity 的
# 颜色常量（docs/交互树.md 第五节有对照表）。
CSS = """
:root{
  /* 深空色：不用纯黑，带一点蓝，屏幕上有层次；辉光统一走 --glow */
  --bg:#070B12; --surface:#0F1622; --surface2:#131C2B;
  --line:rgba(96,165,250,.16); --line2:rgba(96,165,250,.09);
  --ink:#E8F0FB; --ink2:#B7C7DD; --muted:#7C8FA8; --faint:#7689A2;
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
  background:var(--bg);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased}
/* 两层辉光挪到固定伪元素：`background-attachment:fixed` 会让 WebView 在每次
   滚动时重绘整面背景（而不是缓存成层），中低端机就是「滚动发涩、下拉闪一下」。
   伪元素只合成一次，视觉效果完全一样。 */
body::before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
  background:
    radial-gradient(900px 520px at 78% -8%, rgba(59,130,246,.17), transparent 62%),
    radial-gradient(680px 420px at 6% 2%, rgba(34,211,238,.10), transparent 60%)}
a{color:var(--brand-ink);text-decoration:none}
.app{max-width:820px;margin:0 auto;padding:0 14px calc(30px + env(safe-area-inset-bottom))}
.back{display:inline-flex;align-items:center;gap:5px;margin:8px 0 -4px;
  min-height:40px;padding:0 2px;
  font-size:12.5px;color:var(--brand-ink);text-decoration:none}
.back:active{opacity:.6}
/* 页头只用一道渐隐的蓝线收尾，比整条实线更像仪表面板 */
.page-head{padding:20px 0 15px;margin-bottom:4px;position:relative;
  background:linear-gradient(90deg,rgba(59,130,246,.60),rgba(34,211,238,.12) 46%,transparent)
    bottom / 100% 1px no-repeat}
.page-head.after-back{padding-top:14px}
/* 页头右上角的单动作按钮（今日页就是「分享」出图）。
   以前它是页面里的一整张快捷卡，占掉半屏却只做一件事。 */
.head-act{position:absolute;top:12px;right:0;display:inline-flex;align-items:center;
  justify-content:center;gap:5px;font-size:12.5px;font-weight:600;padding:0 14px;
  min-height:40px;border-radius:999px;
  color:var(--brand-ink);background:var(--brand-soft);cursor:pointer;
  border:1px solid rgba(59,130,246,.36);box-shadow:0 0 18px -6px rgba(59,130,246,.9)}
.head-act:active{background:rgba(59,130,246,.28)}
/* 有右上角动作时给标题和副标题留出位置，长标题不会钻到按钮底下 */
.page-head.with-act h1,.page-head.with-act .lede{padding-right:92px}
.kicker{font-size:11px;font-weight:700;letter-spacing:2px;color:var(--cyan);
  text-shadow:0 0 12px rgba(34,211,238,.45)}
.page-head h1{margin:6px 0 4px;font-size:24px;font-weight:650;letter-spacing:-.5px;line-height:1.3}
.lede{margin:0;font-size:13px;color:var(--muted)}
.meta{display:flex;gap:6px;flex-wrap:wrap;margin-top:12px}
.chip{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;padding:4px 10px;
  border-radius:999px;background:rgba(15,22,34,.72);border:1px solid var(--line);color:var(--ink2)}
.chip.ok{background:var(--brand-soft);border-color:rgba(59,130,246,.34);color:var(--brand-ink)}
a.chip{min-height:40px;padding:0 14px;justify-content:center}
.chip.warn{background:var(--amber-soft);border-color:rgba(245,165,36,.32);color:#F5C26B}
.sec{margin-top:24px}
.sec-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:0 0 10px}
.sec-head h2{margin:0;font-size:11.5px;font-weight:700;letter-spacing:1.4px;
  color:var(--muted);position:relative;padding-left:12px}
.sec-head h2::before{content:"";position:absolute;left:0;top:50%;width:3px;height:12px;
  margin-top:-6px;border-radius:2px;
  background:linear-gradient(180deg,var(--cyan),var(--brand));
  box-shadow:0 0 10px rgba(34,211,238,.7)}
.sec-head .more{font-size:12.5px;color:var(--brand-ink);display:inline-flex;
  align-items:center;min-height:40px;padding:0 2px}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.grid.three{grid-template-columns:repeat(3,minmax(0,1fr))}
.grid.one{grid-template-columns:1fr}
/* 恢复度卡里的 4 个分项：一行两项、各带一条 3px 的分数条。
   原来这里是「睡眠 35% / HRV 30% …」一整行字，读得出权重却读不出得分。 */
.mm{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:14px}
.mm .i{background:rgba(9,14,22,.52);border:1px solid var(--line2);
  border-radius:10px;padding:8px 10px;min-width:0}
.mm .n{font-size:11px;color:var(--muted);letter-spacing:.3px}
.mm .v{font-size:14px;font-weight:650;margin-top:1px;
  font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mm .d{font-size:11px;color:var(--faint);margin-top:1px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mm .b{height:3px;border-radius:999px;background:rgba(255,255,255,.07);
  overflow:hidden;margin-top:6px}
.mm .b i{display:block;height:100%;border-radius:999px;
  background:linear-gradient(90deg,var(--brand),var(--cyan))}
/* 睡眠 / 体重这类「一项一个数」的卡：大数字 + 一行说明 + 迷你趋势线 */
.vital{background:linear-gradient(180deg,rgba(19,28,43,.92),rgba(14,21,33,.95));
  border:1px solid var(--line);border-radius:14px;padding:12px 13px 10px;
  box-shadow:var(--shadow);min-width:0}
.vital .k{font-size:11px;letter-spacing:.6px;color:var(--muted)}
.vital .num{font-size:24px;font-weight:700;line-height:1.15;margin-top:3px;
  letter-spacing:-.6px;font-variant-numeric:tabular-nums;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vital .num small{font-size:11.5px;font-weight:500;letter-spacing:0;
  color:var(--muted);margin-left:3px}
.vital .sub{font-size:11.5px;color:var(--muted);margin-top:4px;line-height:1.5;
  display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;
  overflow:hidden}
.vital .sub2{font-size:11.5px;color:var(--faint);margin-top:2px;line-height:1.5;
  display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;
  overflow:hidden}
.vital .spark{margin-top:6px;display:block}
.vital .none{font-size:12px;color:var(--faint);margin-top:8px}
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
.val{font-size:22px;font-weight:650;line-height:1.2;margin-top:4px;
  font-variant-numeric:tabular-nums;color:var(--ink);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.val.sm{font-size:18px}
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
  border-radius:999px;padding:0 16px;min-height:44px;font-size:13px;
  font-family:inherit;cursor:pointer}
.btn.primary{background:linear-gradient(135deg,#3B82F6,#22D3EE);border-color:transparent;
  color:#04121F;font-weight:650;box-shadow:0 8px 22px -10px rgba(59,130,246,.85)}
.btn:active{opacity:.85}
.empty{padding:20px 4px;font-size:13px;color:var(--muted);line-height:1.8}
.link-act{display:inline-flex;align-items:center;min-height:40px;
  color:var(--brand-ink);text-decoration:none}
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
.wrow .now{flex:0 0 auto;font-size:11px;color:#04121F;font-weight:700;
  background:linear-gradient(135deg,#3B82F6,#22D3EE);
  border-radius:999px;padding:2px 8px;box-shadow:0 0 12px -2px rgba(59,130,246,.8)}
.wrow .done{flex:0 0 auto;font-size:11px;color:var(--brand-ink);
  background:rgba(59,130,246,.12);border:1px solid rgba(96,165,250,.26);
  border-radius:999px;padding:2px 8px}
/* 计划了但没做：虚线灰标签，比「已完成」弱一档，但不至于被藏起来 */
.wrow .miss{flex:0 0 auto;font-size:11px;color:var(--muted);background:transparent;
  border:1px dashed var(--line);border-radius:999px;padding:2px 8px}
/* 没排课但练过了：琥珀色，说清「这不是计划内的完成」 */
.wrow .extra{flex:0 0 auto;font-size:11px;color:var(--amber);
  background:rgba(245,165,36,.10);border:1px solid rgba(245,165,36,.28);
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
""" + BR.BRAND_CSS

# 看板是独立的宽版 HTML：在 App 里强制单列/收紧留白，别让三栏挤成一小坨。
DASH_FIX = """
.wrap{max-width:820px;padding:0 14px calc(30px + env(safe-area-inset-bottom))}
.top{margin:0 0 14px}
.top h1{font-size:19px}
.hero,.two{grid-template-columns:1fr}
.metrics{grid-template-columns:repeat(3,minmax(0,1fr))}
table{display:block;overflow-x:auto;white-space:nowrap}
/* nav 挂在 <body> 下（不是 .wrap 里），本身已经是整屏宽；
   以前用 -14px 抵消 .wrap 的内边距，结果在 body 上反而多出 14px，
   整页能横向拖动一小截。 */
#gc-nav{margin:0 0 16px;padding:9px 14px;position:sticky;top:0;z-index:99;
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
""" + BR.BRAND_CSS + """
/* AI 页自带 CSS，用 h1 的角标伪元素补上同一枚品牌图标，不改它的 DOM。 */
.top>div:first-child h1{display:flex;align-items:center;gap:9px}
.top>div:first-child h1::before{content:"";flex:none;width:26px;height:26px;
  border-radius:8px;
  background:url("__RC_MARK_URI__") center/80% no-repeat,
    linear-gradient(155deg,#16273D,#091320);
  border:1px solid rgba(96,165,250,.36);
  box-shadow:0 0 16px -5px rgba(59,130,246,.9)}
""".replace("__RC_MARK_URI__", BR.mark_uri(26))

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
         back: tuple[str, str] | None = None, action: str = "") -> str:
    """所有 tab 页与二级页的统一外壳。

    `back` 只给二级页（/view、/files）用：一级 tab 之间靠底部导航切换，
    页内不再放返回链接，避免和底部 tab 形成两条并行导航。
    `action` 是页头右上角那个单动作按钮的 HTML（今日页放「分享/出图」）——
    低频但常用的动作放这儿，比在正文里占一整张卡片划算得多。
    """
    back_html, head_cls = "", ""
    if back:
        back_html = f'<a class="back" href="{esc(back[1])}">← {esc(back[0])}</a>'
        head_cls += " after-back"
    if action:
        head_cls += " with-act"
    # 页头左上角放一枚品牌角标：和启动器、开屏、一图流是同一个图形。
    # 只做装饰，读屏器跳过；真正的标题仍是 h1。
    badge = BR.badge_html(26, "rc-head-badge")
    foot_html = ('<div class="rc-foot">' + BR.badge_html(18) +
                 '<span>RunnerCoach</span></div>' + esc(foot))
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#070B12">
<title>{esc(title)} · RunnerCoach</title>
<style>{CSS}</style></head><body><div class="app">
{back_html}
<header class="page-head{head_cls}">
  {action}
  <div class="brandline">{badge}<div class="kicker">{esc(kicker)}</div></div>
  <h1>{esc(title)}</h1><p class="lede">{esc(lede)}</p>
  <div class="meta">{chips}</div>
</header>
<main>{body}</main>
<div class="foot">{foot_html}</div>
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
    """统计卡。值太长时自动降一档字号。

    `.val` 是 nowrap + ellipsis：360 宽的屏上三列卡内容只有 ~82px，
    「1163 km」在 22px 下会超宽，被省略号吃掉尾数 —— 数字被截比字小更糟。
    """
    u = f'<small style="font-size:12px;color:var(--muted)"> {esc(unit)}</small>' if unit else ""
    long = len(str(value)) >= 4
    return (f'<div class="card"><div class="lab">{esc(label)}</div>'
            f'<div class="val{" sm" if long else ""}">{esc(value)}{u}</div>'
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
    arrow = '<span class="go" aria-hidden="true">›</span>' if go else ""
    cls = "row on" if on else "row"
    # 可点元素要么是 <a>（天然可访问），要么补上 role/tabindex/aria-label
    a11y = ("" if href else
            f' role="button" tabindex="0" aria-label="{esc(title)}"')
    return (f'<{tag} class="{cls}"{attr}{a11y}>'
            f'<span class="ic {esc(tone)}" aria-hidden="true">{esc(icon)}</span>'
            f'<span class="tx"><span class="t">{esc(title)}</span>'
            f'<span class="d">{esc(desc)}</span></span>{arrow}</{tag}>')


def act(icon: str, title: str, desc: str, action: str = "", href: str = "") -> str:
    tag = "a" if href else "div"
    attr = f' href="{esc(href)}"' if href else f' data-act="{esc(action)}"'
    a11y = "" if href else f' role="button" tabindex="0" aria-label="{esc(title)}"'
    return (f'<{tag} class="act"{attr}{a11y}>'
            f'<span class="ai" aria-hidden="true">{esc(icon)}</span>'
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


def _hm(minutes) -> str:
    """81 → `1h21`；不足一小时 → `45′`。睡眠分项用，比「81 分钟」短一截。"""
    m = int(minutes or 0)
    return f"{m // 60}h{m % 60:02d}" if m >= 60 else f"{m}′"


def _spark(pairs, *, w: int = 120, h: int = 30, pad: int = 2) -> str:
    """迷你趋势线（内联 SVG）：`pairs` 是 [(日期, 数值), ...]。

    值可能是 None（那天没测），跳过但**保留 x 位置**——否则缺测的日子会被
    挤掉，趋势看起来像连续记录，反而骗人。点数不足 2 个就返回空串，
    让卡片落到「未同步」文案上，而不是画一条假直线。
    """
    pts = [(i, v) for i, (_d, v) in enumerate(pairs or [])
           if isinstance(v, (int, float))]
    if len(pts) < 2:
        return ""
    vals = [v for _, v in pts]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = max(len(pairs) - 1, 1)

    def x(i: int) -> float:
        return pad + (w - 2 * pad) * i / n

    def y(v: float) -> float:
        return h - pad - (h - 2 * pad) * (v - lo) / span

    line = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in pts)
    area = (f"{x(pts[0][0]):.1f},{h - pad} " + line
            + f" {x(pts[-1][0]):.1f},{h - pad}")
    gid = f"sp{abs(hash(tuple(vals))) % 9999}"
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" width="100%" height="{h}" '
            f'preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">'
            f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0" stop-color="#3B82F6" stop-opacity=".42"/>'
            f'<stop offset="1" stop-color="#3B82F6" stop-opacity="0"/>'
            f'</linearGradient></defs>'
            f'<polygon points="{area}" fill="url(#{gid})"/>'
            f'<polyline points="{line}" fill="none" stroke="#5EA0FF" '
            f'stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>'
            f'</svg>')


def _vital(label: str, value, unit: str, sub: str, spark: str,
           empty: str, sub2: str = "") -> str:
    """一项一个数的卡：大数字 + 一到两行说明 + 趋势线。

    两行是刻意的：半屏宽的卡片放不下「评分 80 · 深睡 1h25 · REM 1h51 · 欠债 11.3h」
    这么长一行，单行省略会把最后那个最该看的数字切掉。拆成两行短句，
    每行都能完整读完。
    """
    if value in (None, "", "—"):
        return (f'<div class="vital"><div class="k">{esc(label)}</div>'
                f'<div class="none">{esc(empty)}</div></div>')
    _s2 = f'<div class="sub2">{esc(sub2)}</div>' if sub2 else ""
    return (f'<div class="vital"><div class="k">{esc(label)}</div>'
            f'<div class="num">{esc(value)}<small>{esc(unit)}</small></div>'
            f'<div class="sub">{esc(sub)}</div>{_s2}{spark}</div>')


def home_page() -> str:
    """「今日」tab：一屏看完恢复、课表、身体和最近训练。

    密度优先。这一版挪走了三类「低频又占地方」的块：
    - **数据覆盖**：「我的」页的「本地数据」已经有同样三个数，首页重复一遍只是噪音；
    - **两张快捷卡**：完整看板退成页头链接，一图流退成右上角小按钮；
    - **纯权重文字**：恢复度的四个分项换成带分数条的格子，权重和得分一起读。
    空出来的一屏放了睡眠与体重的实际数值和近两周趋势 —— 这两个是每天都会变、
    但以前只藏在「完整看板」里的东西。
    """
    from . import appapi as A

    today = dt.date.today().isoformat()
    st = A.status()
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
    sleep = res.get("sleep") or {}
    body = res.get("body") or {}
    series = res.get("series") or {}
    score = rd.get("score")
    level = rd.get("level") or "暂无数据"
    acts = res.get("activities") or []
    week = "一二三四五六日"[dt.date.fromisoformat(day).weekday()] if day else ""

    chips = (chip(f"{day} 周{week}", "ok") +
             chip(f"{st.get('days') or 0} 天数据") +
             chip(f"上次同步 {esc(str(st.get('last_sync') or '—')[5:16])}"))

    # 恢复度的四个分项：名字、得分、一行依据，外加一条分数条
    mm = ""
    for p in (rd.get("parts") or [])[:4]:
        _v = p.get("score")
        _pct = max(0, min(100, int(_v or 0)))
        mm += (f'<div class="i"><div class="n">{esc(p.get("name") or "")}</div>'
               f'<div class="v">{esc(_v if _v is not None else "—")}</div>'
               f'<div class="d">{esc(p.get("detail") or "")}</div>'
               f'<div class="b"><i style="width:{_pct}%"></i></div></div>')

    hero = f"""<div class="card brand"><div class="hero">
      <div><div class="lab">恢复度</div>
        <div class="big">{esc(score if score is not None else '—')}<small>/ 100</small></div>
        <div class="note" style="margin-top:6px;font-size:13px;color:var(--brand-ink)">
          {esc(level)}</div>
      </div>
      <div class="hero-side">今日建议<b>{esc(_session(res))}</b>
        <span>{esc(day)} 的本地分析</span></div>
    </div><div class="mm">{mm}</div>
    <div class="bar"><i style="width:{max(0, min(100, int(score or 0)))}%"></i></div></div>"""

    # 睡眠：总分 + 深睡/REM，趋势取近两周；欠债单独提一句（它才是「今天困不困」）
    sh = sleep.get("total_h")
    _sub, _sub2 = [], []
    if sleep.get("score") is not None:
        _sub.append(f"评分 {sleep['score']:.0f}")
    if sleep.get("deep_min"):
        _sub.append(f"深睡 {_hm(sleep['deep_min'])}")
    if sleep.get("rem_min"):
        _sub2.append(f"REM {_hm(sleep['rem_min'])}")
    _debt = (sleep.get("debt") or {}).get("deficit_h")
    if _debt:
        _sub2.append(f"欠债 {_debt:.1f}h")
    sleep_card = _vital("睡眠", f"{sh:.1f}" if sh else None, "h",
                        " · ".join(_sub), _spark((series.get("sleep_h") or [])[-14:]),
                        "昨夜没同步到睡眠", " · ".join(_sub2))

    # 体重：最新值 + 7 日均 + 体脂；太久没称就把「多久」写在最前面
    w = body.get("latest")
    _wsub = []
    if body.get("stale_days") and body["stale_days"] >= 3:
        _wsub.append(f"{body['stale_days']} 天未称重")
    if body.get("avg7"):
        _wsub.append(f"7 日均 {body['avg7']:.1f}")
    if body.get("body_fat"):
        _wsub.append(f"体脂 {body['body_fat']:.1f}%")
    # 变化量四舍五入到 0.1 kg 后是 0.0 就不要写了 ——「较前 7 天 -0.0 kg」是噪音
    _dw = body.get("delta_w")
    if _dw is not None and abs(_dw) >= 0.05:
        _wsub.append(f"较前 7 天 {_dw:+.1f} kg")
    weight_card = _vital("体重", f"{w:.2f}" if w else None, "kg",
                         " · ".join(_wsub), _spark((body.get("series") or [])[-14:]),
                         "还没有体重记录（小米秤或体成分 CSV）")

    stats = ('<div class="grid three">' +
             stat("今日负荷", _today_load_txt(load, day),
                  "近 7 天累计 " + _week_load_txt(load)) +
             stat("CTL 体能", load.get("ctl") or "—", "42 天指数均线") +
             stat("TSB 状态", load.get("tsb") or "—", "体能 − 疲劳") +
             '</div>')

    rows = ""
    for a in acts[:4]:
        aid = str(a.get("activity_id") or "")
        rows += row("▶", a.get("name") or "活动",
                    f"{a.get('date') or ''} · {_act_metric(a)}",
                    aid=aid, tone="b")
    if not rows:
        rows = ('<div class="empty">还没有活动数据。下拉同步一次，'
                '或到「我的 → 历史数据」补全更早的区间。</div>')

    # 今日课表：读同步落下的 plan.json，零请求；没有计划就不占版面。
    plan_html = ""
    try:
        _pl = A.plan(False)
        if _pl.get("ok"):
            plan_html = _today_plan_card(_pl)
    except Exception:
        plan_html = ""

    body_html = (sec("今日状态", hero + '<div style="height:10px"></div>' + stats,
                     ("完整看板", f"/view?date={day}&from=today")) +
                 # 课表卡本身不可点，这里必须留一个入口进二级页看整周与计划信息。
                 # v63 重排首页时把快捷卡删了，连带把这个链接一起弄丢了。
                 (sec("今日课表", plan_html, ("完整课表", "/plan?from=today"))
                  if plan_html else "") +
                 sec("身体", f'<div class="grid">'
                             f'{sleep_card}{weight_card}</div>') +
                 sec("最近活动", rows))
    action = ('<div class="head-act" data-act="export" role="button" tabindex="0" '
              'aria-label="导出一图流"><span aria-hidden="true">⇪</span>一图流</div>')
    return page("TODAY · 今日", "今天该怎么练",
                "恢复、负荷、睡眠与体重，一屏看完", body_html, chips,
                action=action)


def _session(res: dict) -> str:
    try:
        from . import brief as B

        s = (B.brief(res) or {}).get("session") or {}
        return str(s.get("type") or s.get("title") or "按恢复度安排")
    except Exception:
        return "按恢复度安排"


def _mask_email(v) -> str:
    """邮箱打码：`a***@qq.com`。

    页面里直接显示完整登录邮箱，和项目自己的隐私立场不一致
    （`appapi._mask_uid` 早就在打码了）。打码规则对短账号也要安全。
    """
    s = str(v or "").strip()
    if not s or "@" not in s:
        return s
    name, _, domain = s.partition("@")
    if not name:
        return s
    keep = name[0]
    return f"{keep}***@{domain}"


def _act_metric(a: dict) -> str:
    """活动行的一行摘要。按类型给不同指标 —— 力量课写「0.00 km · 配速 —」
    会被读成「跑了 0 公里」，明明是没跑。"""
    dist = float(a.get("distance_m") or 0) / 1000.0
    dur = float(a.get("duration_s") or 0) / 60.0
    pace = a.get("pace_s_km") or 0
    kind = str(a.get("type") or "")
    bits: list[str] = []
    if kind in ("running", "trail_running", "walking") or dist >= 0.1:
        if dist >= 0.1:
            bits.append(f"{dist:.2f} km")
        if pace:
            bits.append(f"配速 {int(pace) // 60}'{int(pace) % 60:02d}\"")
        elif dur >= 1:
            bits.append(f"{dur:.0f} 分钟")
    else:
        # 骑行/力量/游泳这类：距离要么是 0 要么不是一个量级，只报时长
        if dist >= 0.1:
            bits.append(f"{dist:.2f} km")
        if dur >= 1:
            bits.append(f"{dur:.0f} 分钟")
    te = a.get("training_load") or a.get("load")
    if te:
        bits.append(f"负荷 {int(te)}")
    return " · ".join(bits) or "已记录"


def _plan_name(name) -> str:
    """课名去掉「W2 周二」前缀。左侧已经有星期，标题里再写一遍是重复。"""
    t = str(name or "").strip()
    if not t:
        return "训练"
    return re.sub(r"^W\d+\s*周[一二三四五六日]\s*", "", t) or t


def _plan_meta(item: dict) -> str:
    """一条课表的副标题：距离 · 时长 · 训练效果 · 运动类型。"""
    bits = []
    if item.get("km"):
        bits.append(f'{item["km"]} km')
    if item.get("minutes"):
        bits.append(f'{item["minutes"]} 分钟')
    if item.get("effect"):
        bits.append(str(item["effect"]))
    # 名字里已经带了这个词（「骑行 恢复」+ 骑行）就别再补一遍，副标题会重复
    _sp = str(item.get("sport") or "")
    if _sp and _sp != "跑步" and _sp not in str(item.get("name") or ""):
        bits.append(_sp)
    # 没有元信息就返回空串，由调用方决定不渲染副标题。
    # 以前兜底成「未标注强度」：七行课表七个「未标注」，像解析失败。
    return " · ".join(bits)


def _plan_items_line(items: list[dict]) -> str:
    return "；".join(
        _plan_name(x.get("name")) + (f' {x["minutes"]}′' if x.get("minutes") else "")
        for x in items) or "—"


def _today_plan_card(pl: dict) -> str:
    """今日课表卡片：**课表**只说计划，实际练了什么单列一行。

    这里有一条容易搞错的分界：日历里的 `activity` 是「已经做完的记录」，
    不是排课。所以通勤骑车、临时去跑的五公里，都不该把「今天休息」顶掉。
    以前把它当计划显示，结果课表上写着「骑行 409.3 公里 · 18 分钟」——
    那是早上骑车上班，跟今天的训练安排没关系。

    现在：没排课 → 标题就是「休息」；确实练过 → 底下加一行「今天练过：…」，
    数据仍可读，但不会被误当成课表。
    """
    t = pl.get("today") or {}
    items = t.get("items") or []
    acts = t.get("acts") or []
    # done 现在是「有计划且已完成」；trained 只表示「今天练过了」。
    # 没排课但去骑了车，是 trained 而不是 done —— 不能算课表完成。
    done = bool(t.get("done"))
    trained = bool(t.get("trained")) or bool(acts)

    # 本地兜底（备赛课表 / 今日处方）不是佳明课表，必须写出来 —— 而且要写原因：
    # 只说「本地推算」，用户不知道该去重新登录还是该怪数据。
    _src = str(pl.get("source") or "")
    _why = str(pl.get("note") or "").strip() or "佳明课表未读到"
    local_tag = (f'<div class="n" style="color:var(--amber)">本地推算 · {esc(_why)}</div>'
                 if _src and _src != "calendar" else "")

    if items:
        it = items[0]
        more = "".join(
            f'<div class="n">另：{esc(x.get("name") or "训练")} · {esc(_plan_meta(x))}</div>'
            for x in items[1:])
        badge = ('<div class="n" style="color:var(--brand-ink)">今天已完成</div>'
                 if done else "")
        # 计划里的课 + 实际做完了：把完成的记录也列出来，别只放一个小角标
        if acts:
            more += "".join(
                f'<div class="n">另练了：{esc(a.get("name"))} · {esc(_plan_meta(a))}</div>'
                for a in acts[:2])
        return (f'<div class="plan-hero"><div class="t">{esc(_plan_name(it.get("name")))}</div>'
                + (f'<div class="d">{esc(it.get("detail"))}</div>' if it.get("detail") else "")
                + f'<div class="n">{esc(_plan_meta(it))}</div>{badge}{more}{local_tag}</div>')

    nxt = pl.get("next_date")
    if nxt:
        tip = f'下一次：{esc(nxt)} · {esc(_plan_items_line(pl.get("next_items") or []))}'
    elif t.get("note"):
        # 本地兜底给的「为什么休息」比「本周没有排课」有用得多
        tip = esc(t["note"])
    else:
        tip = "本周没有排课，按恢复度安排轻松活动"

    # 今天确实练过：单独列一行，语气是「记录」而不是「课表」
    done_html = ""
    if acts:
        a = acts[0]
        done_html = (f'<div class="n">今天练过：{esc(a.get("name") or "训练")}'
                     f' · {esc(_plan_meta(a))}</div>')
        done_html += "".join(
            f'<div class="n">另：{esc(x.get("name"))} · {esc(_plan_meta(x))}</div>'
            for x in acts[1:3])
    elif trained:
        # 本地库有活动但日历没有记录（高驰的活动不进佳明日历）。
        # 这句说的是「练过了」，不是「完成了课表」。
        done_html = '<div class="n">今天练过：训练记录已同步（含高驰）</div>'

    return (f'<div class="plan-hero"><div class="t">今天休息</div>'
            f'<div class="d">{tip}</div>{done_html}{local_tag}</div>')


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
        _wk = (f'{r.get("week_done") or 0}/{r.get("week_planned") or 0} 完成'
               if r.get("week_planned") else f'本周 {7 - (r.get("rest_days_in_week") or 0)} 练')
        chips = (chip("佳明日历", "ok") +
                 chip(f'{r.get("start")} ~ {r.get("end")}') +
                 chip(_wk))
    elif r.get("source") in ("raceplan", "coach"):
        # 没有佳明账号 / 日历没排课时的本地兜底：说清来源，别让人以为
        # 这是从手表里读出来的课表
        chips = (chip("本地生成", "ok") +
                 chip(str(r.get("name") or "本地课表")))
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
        its = d.get("items") or []
        act = d.get("acts") or []
        if its:
            first = its[0]
            _meta = _plan_meta(first)
            wb = (f'<span class="wb">{esc(_plan_name(first.get("name")))}'
                  + (f'<em>{esc(_meta)}</em>' if _meta else "") + '</span>')
        elif act:
            # 当天没排课、却有实际训练：**标题仍然是「休息」**（这是课表），
            # 练了什么写在副行。以前直接把活动名顶上来，通勤骑车会看着像当天的课。
            a = act[0]
            _more = f"（另 {len(act) - 1} 项）" if len(act) > 1 else ""
            wb = (f'<span class="wb">休息'
                  f'<em>已练：{esc(a.get("name") or "训练")} · '
                  f'{esc(_plan_meta(a))}{esc(_more)}</em></span>')
        else:
            wb = '<span class="wb">休息</span>'
        if is_today:
            tag = '<span class="now">今天</span>'
        elif d.get("done"):
            tag = '<span class="done">已完成</span>'
        elif d.get("missed"):
            # 计划了但没做：以前这种日子被直接标成「已完成」藏起来了
            tag = '<span class="miss">未完成</span>'
        elif d.get("extra"):
            # 没排课但练过：不叫「完成」，只如实说这是计划外的
            tag = '<span class="extra">计划外</span>'
        else:
            tag = ""
        rows += f'<div class="{cls}"><span class="wd">{esc(d["dow"])}</span>{wb}{tag}</div>'

    src_label = "佳明日历" if r.get("source") == "calendar" else (r.get("name") or "教练计划")
    info = ('<div class="card">'
            f'<div class="kvline"><span>来源</span><b>{esc(src_label)}</b></div>'
            + (f'<div class="kvline"><span>覆盖</span><b>{esc(r.get("start") or "—")}'
               f' ~ {esc(r.get("end") or "—")}</b></div>'
               if (r.get("start") or r.get("end")) else "")
            + (f'<div class="kvline"><span>说明</span><b>{esc(r["note"])}</b></div>'
               if r.get("note") else "")
            + (f'<div class="kvline"><span>当前阶段</span><b>{esc(ph.get("label"))}</b></div>'
               if ph.get("label") else "")
            + f'<div class="kvline"><span>本周完成</span>'
              f'<b>{r.get("week_done") or 0} / {r.get("week_planned") or 0} 次</b></div>'
            + f'<div class="kvline"><span>本周休息</span>'
              f'<b>{r.get("rest_days_in_week") or 0} 天</b></div>'
            + (f'<div class="kvline"><span>计划外训练</span>'
               f'<b>{r["week_extra"]} 天</b></div>' if r.get("week_extra") else "")
            + '</div>')

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
    sources = (row("G", "佳明 Garmin", f"{g} · {esc(_mask_email(st.get('email')) or '未设置账号')}",
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

    # 「补全历史」已经完全没有按钮了：它由 App 在每次刷新后**后台**由近到远
    # 自动往前挪，进度写在标题栏小字。这里只做只读展示，不提供点击 ——
    # 以前那个入口点开只有一个弹窗、跑完也只说「跳过 N 天」，用户只会觉得没反应。
    more = (row("◫", "产物文件", "看板 / 图片 / 视频清单", href="/files") +
            row("◍", "AI 模型服务", f"{ai_txt} · 在「教练」页右上角设置", tone="b", go=False) +
            row("⌖", "地图底图",
                ("已配置 · 活动图带高德底图" if _map_on()
                 else "未配置 · 活动图为矢量轨迹"), act="amap", tone="a") +
            row("✂", "素材",
                f"{_clip_count()} 个 · 图片 / 视频，回放时自动插入", act="clips", tone="b") +
            # 换手机就靠这两行：App 是 allowBackup=false，不导出等于历史清零。
            # 导入会覆盖当前库，所以弹窗里要再说一次「覆盖前自动备份」。
            row("⇩", "导出数据", "打包历史与设置，换手机 / 重装时用",
                act="dbExport", tone="a") +
            row("⇧", "导入数据", "选数据包覆盖当前数据（会先自动备份）",
                act="dbImport", tone="b") +
            # 崩溃时只有一句 toast，用户复述不清；这里能直接把最近几条异常复制出来
            row("!", "诊断日志", "出问题时把这段发出来，能定位到具体一步",
                act="diag", tone="a"))

    about = ('<div class="card rc-about"><div class="rc-about-h">' +
             BR.badge_html(38) +
             f'<div><div class="lab">RunnerCoach · v{esc(VER)}</div>' +
             '<div class="note" style="margin-top:2px">本地优先的跑步教练</div>'
             '</div></div>'
             '<div class="note" style="margin-top:10px;font-size:12.5px;line-height:1.8">'
             '凭据与健康数据只存在这台设备的 App 私有目录；看板、图片、回放全部本地生成，'
             '页面不加载任何 CDN、远程字体或统计脚本。卸载 App 即清空本地数据。'
             '<br><a class="link-act" href="/privacy">完整隐私说明 →</a>'
             '</div></div>')

    stat_line = (f'<div class="grid three">' +
                 stat("日汇总", f"{cov['days']} 天", f"{cov['first']} ~ {cov['last']}") +
                 stat("活动", f"{cov['activities']} 条", "全部本地") +
                 stat("上次同步", str(st.get("last_sync") or "—")[5:10],
                     (str(st.get("last_sync") or "")[11:16] + " 同步 · 下拉可刷新").lstrip(" ·")) +
                 '</div>'
                 '<div class="note" style="margin-top:10px;line-height:1.7">'
                 '历史由 App 在后台自动补齐：每次刷新从最近往前挪一段（一年一段，'
                 '最多回溯 8 年），进度显示在标题栏；补完就不再发请求，无需手动操作。</div>')

    body = (sec("数据源", sources) +
            sec("更多", more) +
            sec("本地数据", stat_line) +
            sec("关于", about))
    return page("ME · 我的", "数据与设置", "账号、同步、产物和隐私都在这里", body, chips)


def privacy_page() -> str:
    """隐私说明：App 内可独立引用的一页。

    内容与 docs/隐私说明.md 同口径。改这里记得同步那份，反之亦然 ——
    公开分发时用户/审核看到的必须是同一套说法。
    """
    def p(txt: str) -> str:
        return f'<div class="card"><div class="note" style="line-height:1.85">{txt}</div></div>'

    collect = p(
        "<b>不收集。</b>没有账号体系、没有遥测、没有崩溃上报。<br>"
        "「诊断日志」只写在这台设备上，发不发由你决定。")

    where = p(
        "日汇总、活动、明细、体成分、素材清单都存 App 私有目录里的 SQLite，"
        "卸载即清空。<b>账号密码不进数据库</b>：佳明/高驰的会话保存在私有目录，"
        "安卓版密码放在系统的 Keystore 里。")

    net = p(
        "只有三类请求会出网，且都由你主动触发：<br>"
        "1. <b>官方接口</b>：佳明国区、高驰、小米健康云，用你自己的账号登录后同步；<br>"
        "2. <b>AI 服务</b>（可选）：你填自己的 base_url 与 key，提问内容直连你选的服务商；<br>"
        "3. <b>高德底图</b>（可选）：只请求地图瓦片，用于活动图与回放底图。<br>"
        "没有中间服务器，任何数据都不经过本项目作者。")

    share = p(
        "活动图与轨迹回放<b>包含 GPS 轨迹</b>，起终点可能暴露住址。"
        "出图前会先问脱敏档位（裁剪 / 隐藏），脱敏在生成产物之前完成，"
        "产物里不留原始折线。是否分享由你决定。")

    never = p(
        "不做代登录服务（佳明条款不允许把凭据交给第三方）；"
        "不把任何数据托管到服务器；不转卖、不用于训练模型。")

    body = (sec("收集", collect) + sec("存在哪里", where) +
            sec("什么时候出网", net) + sec("分享前", share) +
            sec("不会做的事", never))
    return page("PRIVACY · 隐私", "隐私说明",
                "数据留在本机；出网的只有你自己触发的请求",
                body, chip("本地优先"), back=("返回我的", "/me"))


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
        # 默认是「普通列表」：点整行打开文件；点右上角「管理」才进选择态，
        # 那时左侧才出现勾选框、点行变成勾选。一上来就给一堆方框，
        # 想打开文件还得躲着它们点，是这套交互最别扭的地方。
        rows += (f'<div class="row pick" data-href="{esc(href)}">'
                 f'<input type="checkbox" class="ck" value="{esc(p.name)}">'
                 f'<span class="ic b">▤</span>'
                 f'<span class="tx"><span class="t">{esc(title)}</span>'
                 f'<span class="d">{esc(group)} · {esc(kind)} · {esc(when)} · '
                 f'{esc(_size(p.stat().st_size))}</span></span>'
                 f'<a class="go" href="{esc(href)}" data-nopush>›</a></div>')
    if not rows:
        rows = '<div class="empty">还没有生成任何产物。同步或出一次图，这里就会出现文件。</div>'

    # 批量管理：全选 / 反选 / 二次确认删除。确认走页面内联，
    # 不用 window.confirm —— WebView 没挂 WebChromeClient，confirm 点了没反应。
    picker = f"""
<style>
.row.pick{{cursor:pointer;gap:10px}}
.row.pick .ck{{display:none;width:18px;height:18px;flex:0 0 auto;margin:0;
  accent-color:#3B82F6}}
/* 管理态：左侧让出勾选框，箭头变淡（仍可点开文件） */
body.manage .row.pick{{cursor:default;background:rgba(59,130,246,.05)}}
body.manage .row.pick .ck{{display:block}}
body.manage .row.pick .go{{opacity:.35}}
.row.pick .go{{text-decoration:none;font-size:20px;line-height:1;color:var(--muted);
  padding:0 4px;flex:0 0 auto}}
.row.pick .go:active{{color:var(--brand-ink)}}
.pickbar{{position:fixed;left:10px;right:10px;bottom:10px;z-index:40;display:none;
  gap:8px;align-items:center;padding:10px 12px;border-radius:16px;
  background:rgba(15,22,34,.97);border:1px solid var(--line);
  box-shadow:0 18px 40px -18px rgba(0,0,0,.92)}}
.pickbar.on{{display:flex}}
.manage-btn{{font-size:11.5px;padding:5px 12px;line-height:1.2}}
.pickbar .n{{flex:1;font-size:12.5px;color:var(--ink2);min-width:0}}
.btn.danger{{border-color:rgba(242,85,90,.45);color:#FF9AA0}}
.btn.danger.armed{{background:rgba(242,85,90,.92);color:#1A0708;border-color:transparent;
  font-weight:650}}
body.has-pick{{padding-bottom:74px}}
</style>
<div class="pickbar" id="pickbar">
  <span class="n" id="pickinfo"></span>
  <button class="btn" id="pickall" type="button">全选</button>
  <button class="btn danger" id="pickdel" type="button">删除</button>
</div>
<script>(function(){{
  var boxes = Array.prototype.slice.call(document.querySelectorAll('.row.pick .ck'));
  var bar = document.getElementById('pickbar');
  if (!boxes.length || !bar) return;
  var info = document.getElementById('pickinfo');
  var all = document.getElementById('pickall');
  var del = document.getElementById('pickdel');
  var armed = false, timer = null;

  function chosen() {{
    return boxes.filter(function(b) {{ return b.checked; }});
  }}
  function reset() {{
    armed = false;
    if (timer) {{ clearTimeout(timer); timer = null; }}
    del.classList.remove('armed');
    del.textContent = '删除';
    del.disabled = false;
  }}
  function sync() {{
    var n = chosen().length;
    info.textContent = n ? ('已选 ' + n + ' 项') : '点左侧方框选择要删除的产物';
    bar.classList.toggle('on', n > 0);
    document.body.classList.toggle('has-pick', n > 0);
    if (!n) reset();
  }}
  // 默认不是选择态：点整行打开文件。点右上角「管理」才进选择态。
  var modeBtn = document.getElementById('pickmode');
  var managing = false;
  function setManage(on) {{
    managing = on;
    document.body.classList.toggle('manage', on);
    if (modeBtn) modeBtn.textContent = on ? '完成' : '管理';
    if (!on) {{
      boxes.forEach(function(b) {{ b.checked = false; }});
      sync();
    }}
  }}
  if (modeBtn) modeBtn.addEventListener('click', function() {{ setManage(!managing); }});

  boxes.forEach(function(b) {{
    b.addEventListener('change', function() {{ if (managing) sync(); }});
    var r = b.closest('.row');
    if (!r) return;
    r.addEventListener('click', function(e) {{
      // 箭头任何时候都是「打开文件」；管理态下也不劫持它
      if (e.target.closest('a')) return;
      if (!managing) {{
        var href = r.getAttribute('data-href');
        if (href) location.href = href;
        return;
      }}
      b.checked = !b.checked;
      sync();
    }});
  }});
  all.addEventListener('click', function() {{
    var on = chosen().length === boxes.length;
    boxes.forEach(function(b) {{ b.checked = !on; }});
    sync();
  }});
  del.addEventListener('click', function() {{
    var list = chosen().map(function(b) {{ return b.value; }});
    if (!list.length) return;
    // 第一次点只是「备战」：按钮变红显示要删几项，5 秒内不确认就自动撤销
    if (!armed) {{
      armed = true;
      del.classList.add('armed');
      del.textContent = '确认删除 ' + list.length + ' 项？';
      timer = setTimeout(reset, 5000);
      return;
    }}
    reset();
    del.disabled = true;
    info.textContent = '删除中…';
    fetch('/__files/delete', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{names: list}})
    }}).then(function(r) {{ return r.json(); }}).then(function(d) {{
      if (d && d.ok) {{ location.reload(); return; }}
      del.disabled = false;
      info.textContent = '删除失败：' + ((d && d.error) || '未知错误');
    }}).catch(function() {{
      del.disabled = false;
      info.textContent = '删除失败：无法连接本机服务';
    }});
  }});
  sync();
}})();</script>
"""

    # 「正在生成」由 JS 轮询 /__progress 渲染：回放要跑几分钟，用户不必守在
    # 出图那一页看转圈，切到产物这里能看到真实百分比；生成完页面自动刷新。
    jobs = """
<div id="gc-jobs"></div>
<script>(function(){
  var box = document.getElementById('gc-jobs');
  if (!box) return;
  var last = 0;
  function box_html(j){
    return '<div class="card" style="margin-bottom:10px">' +
      '<div class="lab">正在生成 · ' + j.name + '</div>' +
      '<div style="height:8px;border-radius:99px;background:rgba(96,165,250,.16);' +
      'overflow:hidden;margin-top:10px">' +
      '<i style="display:block;height:100%;width:' + j.pct + '%;' +
      'background:linear-gradient(90deg,#22D3EE,#3B82F6);transition:width .4s"></i></div>' +
      '<div class="note" style="margin-top:8px">' + j.stage + ' ' + j.pct +
      '% · 可以先切走，生成完这里会自动出现</div></div>';
  }
  function tick(){
    fetch('/__progress', {cache:'no-store'})
      .then(function(r){ return r.json(); })
      .then(function(d){
        var jobs = d.jobs || [];
        if (!jobs.length) {
          box.innerHTML = '';
          // 上一轮还有任务、这一轮没了：刚生成完，刷一次让新文件进列表
          if (last > 0) { location.reload(); return; }
        } else {
          box.innerHTML = '<section class="sec"><div class="sec-head">' +
            '<h2>正在生成</h2></div>' + jobs.map(box_html).join('') + '</section>';
        }
        last = jobs.length;
      })
      .catch(function(){});
  }
  tick();
  setInterval(tick, 2000);
})();</script>
"""
    manage_btn = ('<button class="btn manage-btn" id="pickmode" type="button">'
                  '管理</button>' if files else "")
    return page("FILES · 产物", "本机产物文件", "看板、图片、视频都只存在这台设备上",
                jobs + sec("最近生成", rows, manage_btn) + picker,
                chip(f"{len(files)} 个"), back=("返回我的", "/me"))
