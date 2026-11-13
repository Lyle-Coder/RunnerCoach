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
from . import screen as SC

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
  min-height:44px;padding:0 2px;
  font-size:12.5px;color:var(--brand-ink);text-decoration:none}
.back:active{opacity:.6}
/* 页头只用一道渐隐的蓝线收尾，比整条实线更像仪表面板 */
.page-head{padding:20px 0 15px;margin-bottom:4px;position:relative;
  background:linear-gradient(90deg,rgba(59,130,246,.60),rgba(34,211,238,.12) 46%,transparent)
    bottom / 100% 1px no-repeat}
.page-head.after-back{padding-top:14px}
/* 页头不再挂右上角动作按钮：0.6.13 起一图流归到「我的」页的低频动作里。
   page() 的 action 参数仍留给二级页，首页不再传它。 */
/* 有右上角动作时给标题和副标题留出位置，长标题不会钻到按钮底下 */
.page-head.with-act h1,.page-head.with-act .lede{padding-right:92px}
.kicker{font-size:11px;font-weight:700;letter-spacing:2px;color:var(--cyan);
  text-shadow:0 0 12px rgba(34,211,238,.45)}
.page-head h1{margin:6px 0 4px;font-size:24px;font-weight:650;letter-spacing:-.5px;line-height:1.3}
.lede{margin:0;font-size:13px;color:var(--muted)}
.meta{display:flex;gap:6px;flex-wrap:wrap;margin-top:9px}
.chip{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;padding:3px 9px;
  border-radius:999px;background:rgba(15,22,34,.72);border:1px solid var(--line);color:var(--ink2)}
.chip.ok{background:var(--brand-soft);border-color:rgba(59,130,246,.34);color:var(--brand-ink)}
a.chip{min-height:44px;padding:0 14px;justify-content:center}
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
  align-items:center;min-height:44px;padding:0 2px}
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
/* 行尾开关：和数据源那种「当前用哪个」不同，它表示服务开不开。
   视觉上只有 38×22，但**命中区做到 44×44**——22px 高的可点元素手指按不准，
   audit 也会判「矮可点」。轨道用 ::before 画，滑块仍由 <i> 表示。
   点击命中开关自己，事件委托从 e.target 往上找，不会连带触发整行动作。 */
.row .sw{position:relative;flex:0 0 auto;margin-left:auto;width:44px;height:44px;
  cursor:pointer}
.row .sw::before{content:"";position:absolute;left:3px;top:11px;width:38px;height:22px;
  border-radius:11px;background:rgba(148,163,184,.26);transition:background .18s}
.row .sw i{position:absolute;left:5px;top:13px;width:18px;height:18px;border-radius:50%;
  background:#E8EEF7;transition:transform .18s,background .18s}
.row .sw.on::before{background:rgba(79,214,166,.85)}
.row .sw.on i{transform:translateX(16px);background:#06121C}
.acts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
/* 「我的」页 2 列网格：图标在左，标题 + 一行说明在右，整卡固定 68px，
   与账号行（67px）同一节奏。旧版是竖排「图标 / 标题 / 最多两行说明」，
   副标题有的 1 行有的 2 行，被 grid 拉伸后同排 136px、下一排只有 119px，
   两组网格之间也差一截；说明一律单行省略后所有卡片严格等高。 */
.act{display:grid;grid-template-columns:28px minmax(0,1fr);
  grid-template-rows:auto auto;column-gap:9px;row-gap:2px;
  align-content:center;box-sizing:border-box;height:68px;padding:8px 10px;
  background:linear-gradient(180deg,rgba(19,28,43,.88),rgba(14,21,33,.92));
  border:1px solid var(--line);border-radius:var(--r);color:inherit;
  cursor:pointer;box-shadow:var(--shadow);min-width:0}
.act:active{background:rgba(59,130,246,.10);border-color:rgba(96,165,250,.34)}
.act .ai{grid-row:1/3;align-self:center;width:28px;height:28px;border-radius:8px;
  display:flex;align-items:center;justify-content:center;font-size:14px;
  background:linear-gradient(160deg,rgba(34,211,238,.22),rgba(59,130,246,.12));
  color:#8FE7F5;border:1px solid rgba(34,211,238,.24);
  box-shadow:inset 0 0 12px rgba(34,211,238,.18)}
.act .at,.act .ad{min-width:0;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}
.act .at{font-size:13px;font-weight:600;line-height:1.25;color:var(--ink)}
.act .ad{display:block;font-size:11px;line-height:1.35;color:var(--muted)}
/* 只读卡片（如「AI 模型服务」）：不暗示可点，也就不该有按下态 */
.act.flat{cursor:default}
.act.flat:active{background:linear-gradient(180deg,rgba(19,28,43,.88),rgba(14,21,33,.92));
  border-color:var(--line)}
/* 只收紧「我的」页的节奏，别的 tab 留白一概不动 */
.me .sec{margin-top:16px}
.me .sec-head{margin-bottom:8px}
.me-diag{margin-top:10px}
.me .grid.three .card{box-sizing:border-box;height:88px;padding:7px 9px}
.me .grid.three .val{font-size:18px;line-height:1.15;margin-top:2px}
.me .grid.three .note{font-size:11px;line-height:1.3;margin-top:2px;
  display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;
  overflow:hidden}
.me .rc-about{padding:12px 14px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;
  border:1px solid var(--line);background:rgba(15,22,34,.72);color:var(--ink2);
  border-radius:999px;padding:0 16px;min-height:44px;font-size:13px;
  font-family:inherit;cursor:pointer}
.btn.primary{background:linear-gradient(135deg,#3B82F6,#22D3EE);border-color:transparent;
  color:#04121F;font-weight:650;box-shadow:0 8px 22px -10px rgba(59,130,246,.85)}
.btn:active{opacity:.85}
.empty{padding:20px 4px;font-size:13px;color:var(--muted);line-height:1.8}
.link-act{display:inline-flex;align-items:center;min-height:44px;
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
  /* 只有普通 .grid 退成单列；.acts 是 68px 的紧凑卡，360 宽下两列仍放得下，
     退成单列会让「我的」页凭空多出 4 行、又变回长列表。 */
  .grid{grid-template-columns:1fr}
  .big{font-size:40px}
}
@media(max-width:340px){.acts{grid-template-columns:1fr}}
""" + BR.BRAND_CSS + SC.SCREEN_CSS

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
  /* 宽屏 / 横屏由 CSS 媒体查询自动铺成五面板，不再放按钮：倾斜手机时
     系统已经给了旋转入口，App 再放一个横屏按钮只会重复，而且强制
     requestedOrientation 还会额外弹出一次系统旋转提示。 */
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
         back: tuple[str, str] | None = None, action: str = "",
         screen: bool = False) -> str:
    """所有 tab 页与二级页的统一外壳。

    `back` 只给二级页（/view、/files）用：一级 tab 之间靠底部导航切换，
    页内不再放返回链接，避免和底部 tab 形成两条并行导航。
    `action` 是页头右上角那个单动作按钮的 HTML（今日页放「分享/出图」）——
    低频但常用的动作放这儿，比在正文里占一整张卡片划算得多。
    `screen=True` 是 0.6.7 起「首屏即一页大屏」用的紧凑页头：去掉副标题、
    压低标题与留白，把视觉重心让给 main 里的 `.rc-hero`。
    """
    back_html, head_cls = "", ""
    if back:
        back_html = f'<a class="back" href="{esc(back[1])}">← {esc(back[0])}</a>'
        head_cls += " after-back"
    if action:
        head_cls += " with-act"
    if screen:
        head_cls += " rc-compact"
    # 首屏要脱离 .app 的 820px 上限：宽屏时铺满 12 栅格，其它 tab 不受影响。
    app_cls = "app rc-page" if screen else "app"
    # 页头不再放品牌角标：原生顶栏已经有一枚同样的图形（0.6.11），
    # 两枚叠在左上角显得重复。页脚左下角的字标保留，仍是同一个图形。
    foot_html = ('<div class="rc-foot">' + BR.badge_html(18) +
                 '<span>RunnerCoach</span></div>' + esc(foot))
    lede_html = "" if screen else f'<p class="lede">{esc(lede)}</p>'
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#070B12">
<title>{esc(title)} · RunnerCoach</title>
<style>{CSS}</style></head><body><div class="{app_cls}">
{back_html}
<header class="page-head{head_cls}">
  {action}
  <div class="brandline"><div class="kicker">{esc(kicker)}</div></div>
  <h1>{esc(title)}</h1>{lede_html}
  <div class="meta">{chips}</div>
</header>
<main>{body}</main>
<div class="foot">{foot_html}</div>
</div>{BRIDGE_JS}</body></html>"""


def sec(title: str, body: str, more: str = "", anchor: str = "") -> str:
    """一个区块。anchor 给区块加 id，表单 303 回来时可以用 #id 停在原处。"""
    aid = f' id="{esc(anchor)}"' if anchor else ""
    return (f'<section class="sec"{aid}><div class="sec-head"><h2>{esc(title)}</h2>'
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
        on: bool = False, nopush: bool = False,
        switch: str = "", sw_act: str = "") -> str:
    """列表行。

    `on=True` 表示「当前选中的那一条」，左侧加一道蓝条；
    `nopush=True` 走 `location.replace`，同页切参数不往历史栈里堆条目
    （否则连点五个月份，返回键要按五次才出得去）。
    `switch="on"/"off"` 在行尾放一个开关；它自己带 `data-act`，点击时
    事件委托从 e.target 往上找，先命中的是开关，不会误触整行的动作。
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
    sw = ""
    if switch in ("on", "off"):
        sw = (f'<span class="sw {switch}"'
              + (f' data-act="{esc(sw_act)}"' if sw_act else "")
              + f' role="switch" aria-checked="{"true" if switch == "on" else "false"}"'
              + ' aria-label="开关"><i></i></span>')
    cls = "row on" if on else "row"
    # 可点元素要么是 <a>（天然可访问），要么补上 role/tabindex/aria-label
    a11y = ("" if href else
            f' role="button" tabindex="0" aria-label="{esc(title)}"')
    return (f'<{tag} class="{cls}"{attr}{a11y}>'
            f'<span class="ic {esc(tone)}" aria-hidden="true">{esc(icon)}</span>'
            f'<span class="tx"><span class="t">{esc(title)}</span>'
            f'<span class="d">{esc(desc)}</span></span>{sw}{arrow}</{tag}>')


def act(icon: str, title: str, desc: str, action: str = "", href: str = "") -> str:
    """网格卡片（2 列）。`action`/`href` 都为空时是**只读卡片**：不加点击属性，
    TalkBack 也不会把它读成按钮——「AI 模型服务」这类纯展示项就靠这个。"""
    tag = "a" if href else "div"
    if href:
        attr = f' href="{esc(href)}"'
        a11y = ""
        cls = "act"
    elif action:
        attr = f' data-act="{esc(action)}"'
        a11y = f' role="button" tabindex="0" aria-label="{esc(title)}"'
        cls = "act"
    else:
        attr = ""
        a11y = ""
        cls = "act flat"
    return (f'<{tag} class="{cls}"{attr}{a11y}>'
            f'<span class="ai" aria-hidden="true">{esc(icon)}</span>'
            f'<span class="at">{esc(title)}</span>'
            f'<span class="ad">{esc(desc)}</span></{tag}>')


def acts_grid(items: list[str]) -> str:
    """把若干 act() 排成 2 列网格。

    为什么「我的」页要用网格：竖排行每行 64px 起，十几个低频入口就是三屏，
    用户要一直往下拉。网格一行放两个，同样的入口只占一半高度，扫视也更快。
    """
    return '<div class="acts">' + "".join(items) + "</div>"




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
    """「今日」tab：首屏即一页式大屏（0.6.7）。

    依据《主页面设计说明》：主页不是展示全部数据的地方，是回答
    「今天怎么样、今天怎么练」的地方。竖屏 6 段（结论条 / 恢复环 /
    负荷带 / 身体 2×2 / 计划一条 / 最近 7 天），宽屏 ≥900px 由同一份
    DOM 重排成 12 栅格 5 面板；宽屏/横屏由 CSS 自动切换，不再放
    「大屏模式」按钮（系统旋转入口已覆盖这件事）；report.py 只保留
    按日期回看与导出，首页不再长出第二个产品。

    冗余入口一并收掉：四个 sec-head 的「完整 XX →」、两张快捷卡、
    卡底那条总进度条、和结论条重复的「今天休息」。数据层仍只用
    metrics.derive() 的返回值，不动 appapi / 请求数。
    """
    from . import appapi as A
    from . import metrics as M

    today = dt.date.today().isoformat()
    st = A.status()
    day = st.get("last") or today
    res: dict = {}
    try:
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
    today_row = res.get("today") or {}
    hist = res.get("history") or []
    acts = res.get("activities") or []
    flags = res.get("flags") or []
    score = rd.get("score")
    try:
        d0 = dt.date.fromisoformat(day)
        week_cn = "一二三四五六日"[d0.weekday()]
    except (TypeError, ValueError):
        d0 = dt.date.today()
        week_cn = ""

    # ---- L1 结论条 44：一句话判断 + 窗口标签（28 天基线） ------------------
    delta = _baseline_delta(hist, score)
    if delta is None:
        meta = "28 天基线样本不足"
    else:
        meta = f"较 28 天基线 {'+' if delta >= 0 else '−'}{abs(delta)}"
    verdict = (f'<section class="rc-verdict"><span class="v">'
               f'{esc(_verdict(score))}</span><span class="m">{esc(meta)}</span>'
               f'</section>')

    # ---- 恢复 hero ≈176：环 108 + 3 条分解 + 1 句可执行建议 ---------------
    # 常态不发光；只有 <50 / >90 的异常日才点亮，辉光才有意义。
    alert = score is not None and (float(score) < 50 or float(score) > 90)
    flag_txt = str((flags[0].get("text") if flags else "") or "")
    # 只留逗号后面的动作短语：「近 7 天睡眠缺口 11.0 小时，优先补觉」
    # 整句塞进 hero 会折两行、还把焦点从环上抢走。
    if "，" in flag_txt:
        flag_txt = flag_txt.split("，", 1)[1]
    if len(flag_txt) > 12:
        flag_txt = flag_txt[:12] + "…"
    advice = (f'<div class="rc-advice"><b>今日建议</b> {esc(_session(res))}'
              + (f' · {esc(flag_txt)}' if flag_txt else "") + "</div>")
    hero = (f'<section class="rc-panel rc-hero-panel">'
            f'<div class="rc-panel-hd"><span class="rc-eyebrow">恢复度 · 今日</span>'
            f'<span class="rc-win">28 天基线分项</span></div>'
            f'<div class="rc-hero">{SC.ring_svg(score, alert=alert)}'
            f'<div class="rc-hero-side"><div class="rc-parts">'
            f'{SC.part_rows(rd.get("parts") or [], 3)}</div>{advice}</div>'
            f'</div></section>')

    # ---- 负荷带 60：三个数并列，不是三张卡；每个数都带窗口 ----------------
    acute, ctl, tsb = load.get("acute_7d"), load.get("ctl"), load.get("tsb")
    items = (
        ("7 天负荷", f"{acute:.0f}" if isinstance(acute, (int, float)) else "—",
         "统一心率 TRIMP · 近 7 天累计"),
        ("CTL 体能", f"{ctl:.1f}" if isinstance(ctl, (int, float)) else "—",
         "42 天指数均线"),
        ("TSB 状态", f"{tsb:+.1f}" if isinstance(tsb, (int, float)) else "—",
         "体能 − 疲劳 · 7 天"),
    )
    load_html = ('<section class="rc-load rc-load-panel">' + "".join(
        f'<div class="item"><div class="k">{esc(k)}</div>'
        f'<div class="v rc-num">{esc(v)}</div><div class="w">{esc(w)}</div></div>'
        for k, v, w in items)
        + '<div class="rc-load-chart">'
        + SC.load_chart(load.get("ctl_series"), load.get("atl_series"))
        + '</div></section>')

    # ---- 身体面板 ≈172：2×2，每格 数值 17 + 变化 + 22px sparkline --------
    wk = res.get("week") or {}

    def _wk(key: str) -> str:
        d = (wk.get(key) or {}).get("delta")
        return "—" if d is None else f"较上周 {d:+.1f}"

    hrv_base = M.baseline(hist, "hrv_night") if hist else None
    rhr_base = M.baseline(hist, "resting_hr") if hist else None
    _sleep_sub = []
    _sc = (sleep.get("score_est") if sleep.get("score_est") is not None
           else sleep.get("score"))
    if _sc is not None:
        _sleep_sub.append(
            ("估算 " if sleep.get("score_est") is not None else "评分 ")
            + f"{_sc:.0f}")
    _debt = (sleep.get("debt") or {}).get("deficit_h")
    if _debt:
        _sleep_sub.append(f"7 日欠债 {_debt:.1f}h")
    _hrv = today_row.get("hrv_night")
    _rhr = today_row.get("resting_hr")
    # 「今天没同步」时 derive 会用库里最后一天顶上（见 metrics.derive）。
    # 把真实数据日期写进卡片标签，别再笼统写「昨夜」让人拿旧值去对 App。
    _data_date = str(res.get("data_date") or day)[:10]
    _dd = _data_date[5:].replace("-", "/")
    # today_page 传给 derive 的是「库里最后一天」，它自己算不出滞后，
    # 这里直接拿真实的今天比，别让「今日」页把昨天的数据当今天而不吭声。
    try:
        _stale = max(0, (dt.date.today()
                         - dt.date.fromisoformat(_data_date)).days)
    except (TypeError, ValueError):
        _stale = 0
    _w = body.get("latest")
    _wsub = []
    if body.get("stale_days") and body["stale_days"] >= 3:
        _wsub.append(f"{body['stale_days']} 天未称重")
    if body.get("avg7"):
        _wsub.append(f"7 日均 {body['avg7']:.1f}")
    _dw = body.get("delta_w")
    if _dw is not None and abs(_dw) >= 0.05:
        _wsub.append(f"较前 7 天 {_dw:+.1f} kg")
    cells = (
        SC.body_cell(f"睡眠 · {_dd}", f"{sleep['total_h']:.1f}" if sleep.get("total_h") else None,
                     "h", " · ".join(_sleep_sub) or "7 日均 —",
                     _spark((series.get("sleep_h") or [])[-14:], h=22)),
        SC.body_cell(f"HRV · {_dd}", f"{_hrv:.0f}" if _hrv else None, "ms",
                     f"28 天基线 {hrv_base:.0f} · {_wk('hrv')}" if hrv_base else _wk("hrv"),
                     _spark((series.get("hrv") or [])[-14:], h=22)),
        SC.body_cell(f"静息心率 · {_dd}", f"{_rhr:.0f}" if _rhr else None, "bpm",
                     f"28 天基线 {rhr_base:.0f} · {_wk('rhr')}" if rhr_base else _wk("rhr"),
                     _spark((series.get("rhr") or [])[-14:], h=22)),
        # 体重刻意给完整一行：不再 nowrap + ellipsis 把「较…」切掉。
        SC.body_cell("体重 · 7 日变化", f"{_w:.2f}" if _w else None, "kg",
                     " · ".join(_wsub) or "还没有体重记录",
                     _spark((body.get("series") or [])[-14:], h=22)),
    )
    body_html = (f'<section class="rc-panel rc-body-panel">'
                 f'<div class="rc-panel-hd"><span class="rc-eyebrow">身体数据</span>'
                 f'<span class="rc-win">近 14 天趋势</span></div>'
                 f'<div class="rc-body">{"".join(cells)}</div></section>')

    # ---- 计划一条：以「真实今天」为准，不再拿最后同步日当今天 -------------
    # 旧版把 st["last"]（最后同步日）当 d0，而课表快照的 next_date 是相对
    # 真实今天算的：没同步的日子里两者错开一天，今天的课就被写成「明天」。
    # 恢复度/身体指标仍看最新有数据的那天，只有训练计划看日历今天。
    try:
        pl = A.plan(False) or {}
    except Exception:
        pl = {}
    tday = dt.date.today()
    real_today = tday.isoformat()
    week_rows = pl.get("week") or []
    today_plan = pl.get("today") or {}

    def _items_on(d: str) -> list[dict]:
        for w in week_rows:
            if str(w.get("date") or "") == d:
                return [x for x in (w.get("items") or []) if not x.get("rest")]
        return []

    today_items = _items_on(real_today)
    if not today_items and str(today_plan.get("date") or "") == real_today:
        today_items = [x for x in (today_plan.get("items") or [])
                       if not x.get("rest")]
    nxt = pl.get("next_date")
    nxt_items = pl.get("next_items") or []
    when, subwhen, title, desc = "—", "", "按恢复度安排", "本周暂无排课"
    eyebrow, pick = "训练计划 · 今天", None
    if today_items or (nxt and str(nxt) == real_today and nxt_items):
        # next_date 偶尔就是今天（日历/教练两种数据源口径不同），一并归今天。
        pick = (today_items or nxt_items)[0]
        when = "今天"
        subwhen = "周" + "一二三四五六日"[tday.weekday()]
    elif nxt_items:
        pick = nxt_items[0]
        eyebrow = "训练计划 · 下一次"
        try:
            nd = dt.date.fromisoformat(str(nxt))
            when = "明天" if nd == tday + dt.timedelta(days=1) else str(nxt)[5:]
            subwhen = "周" + "一二三四五六日"[nd.weekday()]
        except (TypeError, ValueError):
            when = str(nxt)[5:]
    elif nxt:
        eyebrow = "训练计划 · 下一次"
        when = str(nxt)[5:]
    if pick:
        title = _plan_name(pick.get("name"))
        desc = _plan_meta(pick)
        hr_cap = pick.get("hr_max") or pick.get("hr_cap") or pick.get("target_hr")
        if hr_cap:
            desc += f" · 心率 ≤ {hr_cap}"
    # 没有元信息就不渲染副标题，别留一行空白把计划条撑厚。
    desc_html = f'<div class="d">{esc(desc)}</div>' if desc else ""
    plan_html = (f'<section class="rc-panel rc-plan-panel">'
                 f'<div class="rc-panel-hd"><span class="rc-eyebrow">{esc(eyebrow)}</span>'
                 f'<span class="rc-win">按日课表</span></div>'
                 f'<div class="rc-plan"><div class="when"><b>{esc(when)}</b>'
                 f'{esc(subwhen)}</div><div class="what"><div class="t">'
                 f'{esc(title)}</div>{desc_html}</div>'
                 f'<a class="go" href="/plan?from=today">课表 →</a>'
                 f'</div></section>')

    # ---- 最近 7 天 ≈300：一个大数 + 7 根柱 + 3 条样本 --------------------
    dates = [(d0 - dt.timedelta(days=6 - i)).isoformat() for i in range(7)]
    week_acts = [a for a in acts
                 if dates[0] <= str(a.get("date") or "") <= day]
    by_day: dict[str, float] = {}
    for a in week_acts:
        k = str(a.get("date") or "")
        by_day[k] = by_day.get(k, 0.0) + float(a.get("distance_m") or 0) / 1000.0
    pairs = [(d, round(by_day.get(d, 0.0), 2)) for d in dates]
    km7 = sum(v for _d, v in pairs)
    _secs = sum(float(a.get("duration_s") or 0) for a in week_acts)
    _asc = sum(float(a.get("ascent") or 0) for a in week_acts)
    dur_txt = f"{int(_secs // 3600)}h{int((_secs % 3600) // 60):02d}" if _secs else "0h00"
    side = f"{len(week_acts)} 次 · {dur_txt}"
    if _asc:
        side += f" · 爬升 {_asc:.0f} m"
    samples = ""
    for a in acts[:3]:
        aid = str(a.get("activity_id") or "")
        name = str(a.get("name") or "活动")
        # 手表会把课名写进活动名（「西安市 - W2 周二 10km@E」）：这里也要洗一遍，
        # 否则首页最近活动会露出「W2」这种内部周标签。
        shown = _plan_name(name)
        samples += (f'<div class="rc-sample" data-aid="{esc(aid)}" role="button" '
                    f'tabindex="0" aria-label="{esc(shown)}">'
                    f'<span class="t">{esc(shown)}</span>'
                    f'<span class="d">{esc(_act_metric(a))}</span></div>')
    if not samples:
        samples = ('<div class="rc-sample"><span class="t">还没有活动数据，'
                   '同步一次就能看到近 7 天</span></div>')
    week_html = (f'<section class="rc-panel rc-week-panel">'
                 f'<div class="rc-panel-hd"><span class="rc-eyebrow">活动概述</span>'
                 f'<span class="rc-win">最近 7 天</span></div>'
                 f'<div class="rc-week"><div class="rc-week-hd">'
                 f'<div class="big-num rc-num">{km7:.1f}'
                 f'<small>km · 最近 7 天</small></div>'
                 f'<div class="side">{esc(side)}</div></div>'
                 f'<div class="rc-days">{SC.week_bars(pairs, day)}</div>'
                 f'<div class="rc-samples">{samples}</div></div></section>')

    body_html_all = ('<div class="rc-screen">' + verdict + hero + load_html
                     + body_html + plan_html + week_html + '</div>')
    # 只留两个数据胶囊：宽屏自动铺成大屏，不再需要第三个按钮。
    # 一图流也不再挂页头：那枚胶囊在横屏大屏里孤零零地压着结论条，
    # 0.6.13 起统一收进「我的」页的低频动作列表。
    # 数据停在旧日期时，把「上次同步」胶囊换成可执行的提示。胶囊数不变，
    # 不会碰到横屏 .rc-screen 的命名 grid 区域。
    _sync_chip = (f"数据停在 {_dd} · 下拉刷新" if _stale else
                  f"上次同步 {esc(str(st.get('last_sync') or '—')[5:16])}")
    chips = (chip(f"{st.get('days') or 0} 天数据") + chip(_sync_chip))
    return page("TODAY · 今日", "今天该怎么练", "", body_html_all, chips,
                screen=True,
                foot=("本机计算 · 不上传轨迹 · 离线可用。"
                      "静息心率取数据源每日值，与设备 App 里用于心率区间的设定值口径可能不同。"))


def _verdict(score) -> str:
    """结论条的一句话判断：先给答案，再给数字。"""
    if score is None:
        return "先同步数据，再谈今天怎么练"
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "先同步数据，再谈今天怎么练"
    if s >= 85:
        return "状态在线，按计划练"
    if s >= 70:
        return "可以练，别加量"
    if s >= 55:
        return "轻松活动，留点余量"
    return "休息，补恢复"


def _baseline_delta(hist: list, score) -> int | None:
    """近 28 天恢复度中位数作基线，返回今天相对它的差。

    每天都用「当天之前的历史」重算一次 readiness，口径和 derive() 一致；
    样本不足就直接不给这个数，绝不编一个「+9」出来。
    """
    if score is None or len(hist) < 8:
        return None
    try:
        from . import metrics as M

        vals = []
        for i in range(max(0, len(hist) - 28), len(hist)):
            got = M.readiness(hist[i], hist[:i + 1]).get("score")
            if got is not None:
                vals.append(got)
        if len(vals) < 5:
            return None
        base = sorted(vals)[len(vals) // 2]
        return int(round(float(score) - base))
    except Exception:
        return None


def _session(res: dict) -> str:
    """今日处方：一句能直接执行的动作。

    以前这里调 `brief.brief(res)`，但 brief 模块是「把一次活动压成推送
    短消息」，既没有 `brief()` 也没有 `session` 字段；异常被吞掉后首页
    永远显示「按恢复度安排」，结论条要的「今天怎么练」其实一直没接上。
    """
    s = (res.get("readiness") or {}).get("score")
    if s is None:
        return "先同步数据"
    from .coach import pick_session
    return pick_session(res)["type"]


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
    from .sports import metric_text
    kind = str(a.get("type") or "")
    bits: list[str] = []
    if kind in ("running", "trail_running", "walking") or dist >= 0.1:
        if dist >= 0.1:
            bits.append(f"{dist:.2f} km")
        if dist > 0:
            bits.append(metric_text(a, label=True))
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
    """课名去掉「W2 周二」这种内部周标签。

    它可能出现在开头（课表标题），也可能夹在活动名中间——手表会把课名写进
    活动名，于是变成「西安市 - W2 周二 10km@E」。左侧已经有星期，标题里
    再写一遍是重复，用户也不该看到「W2」这种开发口径。
    """
    t = str(name or "").strip()
    if not t:
        return "训练"
    t = re.sub(r"W\d+\s*周[一二三四五六日]\s*", "", t)
    t = t.strip(" -·—")
    return t or "训练"


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
                f'<div class="n">另练了：{esc(_plan_name(a.get("name")))} · {esc(_plan_meta(a))}</div>'
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

    steps_html = ""
    from .sports import profile
    ftp = profile("bike").get("ftp_w")
    for item in (r.get("today") or {}).get("items") or []:
        for step in item.get("steps") or []:
            power = (f"{ftp * step['ftp_low']:.0f}–{ftp * step['ftp_high']:.0f} W" if ftp
                     else f"FTP {step['ftp_low'] * 100:.0f}–{step['ftp_high'] * 100:.0f}%（未设置 FTP）")
            steps_html += f'<div class="kvline"><span>{esc(step["name"])} · {step["seconds"] / 60:g} 分钟</span><b>{esc(power)}</b></div>'
    steps_section = sec("骑行步骤", '<div class="card">' + steps_html + '</div>') if steps_html else ""
    body = (sec("今日", _today_plan_card(r)) + steps_section +
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


def sports_page(token: str, message: str = "") -> str:
    from . import sports as S
    cfg = S.config()
    select = '<label>主要运动 <select name="preferred_sport">' + ''.join(
        f'<option value="{key}"{" selected" if S.preferred() == key else ""}>{name}</option>'
        for key, name in (("run", "跑步"), ("bike", "骑行"))) + '</select></label>'
    fields = []
    for key, name in (("run", "跑步"), ("bike", "骑行")):
        p = S.profile(key, cfg)
        inputs = []
        for field, label, lo, hi in (("max_hr", "最大心率 bpm", 80, 240), ("lthr", "阈值心率 bpm", 60, 230), ("ftp_w", "FTP W", 30, 1000)):
            if key == "run" and field == "ftp_w":
                continue
            inputs.append(f'<label style="display:block;margin:12px 0">{label} '
                          f'<input type="number" name="{key}_{field}" min="{lo}" max="{hi}" step="1" value="{esc(p.get(field) or "")}" placeholder="未设置"></label>')
        names = ','.join(p.get("hr_zone_names") or [])
        if key == "bike":
            inputs.append('<label style="display:block;margin:12px 0">本地骑行模板 <select name="bike_workout">' + ''.join(
                f'<option value="{v}"{" selected" if p.get("workout", "endurance") == v else ""}>{label}</option>'
                for v, label in (("endurance", "有氧"), ("recovery", "恢复"), ("intervals", "功率间歇"))) + '</select></label>')
        inputs.append(f'<label style="display:block">Z1–Z5 名称 <input name="{key}_hr_zone_names" value="{esc(names)}" placeholder="热身,轻松,有氧,阈值,极限" style="width:100%;margin-top:8px"></label>')
        fields.append(sec(name, '<div class="card">' + ''.join(inputs) + '</div>'))
    body = (f'<p class="note">{esc(message)}</p>' if message else '')
    body += (f'<form action="/__sports" method="post"><input type="hidden" name="token" value="{esc(token)}">'
             + sec("训练偏好", '<div class="card">' + select + '</div>') + ''.join(fields)
             + '<div class="note">睡眠、HRV、体重仍共用。最大心率用于分项目负荷估算；阈值心率用于骑行课表目标说明，'
               '设备心率分区时长保持原始口径。FTP 留空时不计算 IF / TSS；历史骑行按当前 FTP 复算。'
               'VO₂max 的现有日汇总尚未按运动分项，请勿将它视为骑行专项指标。</div>'
               '<div class="pager"><button class="btn primary" type="submit">保存运动设置</button></div></form>')
    return page("SPORTS · 运动设置", "运动设置", "跑步与骑行分别配置", body, back=("返回我的", "/me"))


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
        h = A.hw_status()
    except Exception:
        h = {}
    try:
        from . import __version__ as VER
    except Exception:
        VER = "—"

    g = "已登录" if st.get("logged_in") else "未登录"
    c_ok = "已登录" if c.get("logged_in") else "未连接"
    try:
        _sleep_ok = bool(A.coros_mcp_status().get("authorized"))
    except Exception:
        _sleep_ok = False
    # 睡眠授权并进高驰这一行：登录成功时会直接问一次意向，没同意也能从
    # 账号卡片里补，不必再单独占一行「高驰睡眠」。
    c_sub = (f"{c_ok} · {'睡眠已授权' if _sleep_ok else '睡眠未授权'}"
             + (" · 查看 / 更换" if c_ok == "已登录" else " · 可独立同步活动"))
    m_ok = "已登录" if m.get("logged_in") else "未连接"
    h_ok = "已登录" if h.get("logged_in") else "未连接"
    try:
        k_ok = "已登录" if A.keep_status().get("logged_in") else "未连接"
    except Exception:
        k_ok = "未连接"
    # Keep 只有运动摘要（没有轨迹/分圈），文案里先说清楚，免得用户以为漏了图
    k_sub = (f"{k_ok} · 运动摘要，无轨迹"
             + (" · 查看 / 更换" if k_ok == "已登录" else ""))
    bs = A.body_sources()
    pref = bs.get("preferred", "mi")
    pref_label = bs.get("labels", {}).get(pref, pref)
    chips = (chip("本机数据", "ok") + chip(f"v{VER}") + chip("零第三方依赖"))

    # AI 模型服务：和数据源并列——这两类都在回答「这台机器现在连着什么服务」。
    # 副标题要能一眼看出「开没开 / 用哪个模型 / 打到哪个域名」，
    # 具体的 Key 与供应商仍然点进去改（抽屉在教练页，这里用 aiSettings 直接拉起来）。
    try:
        from . import aiapi as AIAPI

        ai = AIAPI.dispatch("config.get", {}) or {}
    except Exception:
        ai = {}
    ai_on = bool(ai.get("ai_enabled", True))
    if not ai_on:
        ai_sub = "已关闭 · 点这里重新打开"
    elif not ai.get("configured"):
        ai_sub = "未配置 · 点这里填 API Key"
    else:
        _base = str(ai.get("base_url") or "")
        _host = _base.split("//")[-1].split("/")[0] if _base else "本地"
        ai_sub = (("平台额度" if ai.get("mode") == "platform" else "自配 API")
                  + f" · {ai.get('model') or '未指定模型'} · {_host}")

    # 账号与数据源：只有这一组继续用竖排行——每行都要说清「登没登、设备是谁」，
    # 网格卡片塞不下这些信息。已登录点进去是账号卡片（查看/更换/退出），
    # 没登录才进登录框。
    sources = (row("G", "佳明 Garmin",
                   f"{g} · {esc(_mask_email(st.get('email')) or '未设置账号')}"
                   + (" · 查看 / 更换" if g == "已登录" else ""),
                   act="login:garmin") +
               row("C", "高驰 COROS", c_sub,
                   act="login:coros", tone="b") +
               row("K", "Keep", k_sub, act="login:keep", tone="b") +
               # 三个体重行并成一行：小米/华为的登录放进来源选择器的「登录账号」，
               # 少两行，语义反而更清楚（都在说「体重数据从哪来」）。
               row("W", "体重数据",
                   f"小米 {m_ok} · 华为 {h_ok} · 当前用 {esc(pref_label)}",
                   act="bodySource", tone="a") +
               # 开关自己带 data-act，点它不会顺带打开设置；点行的其它地方才进配置
               row("◍", "AI 模型服务", ai_sub, act="aiSettings", tone="b", go=False,
                   switch=("on" if ai_on else "off"), sw_act="aiToggle"))

    # 「补全历史」已经完全没有按钮了：它由 App 在每次刷新后**后台**由近到远
    # 自动往前挪，进度写在标题栏小字。这里只做只读展示，不提供点击 ——
    # 以前那个入口点开只有一个弹窗、跑完也只说「跳过 N 天」，用户只会觉得没反应。
    # 常用动作走 2 列网格：每张卡固定 68px，和账号行一样高。
    # 说明文字一律控制在一行内（≤14 字），长文案改去别处说。
    tool_tiles = acts_grid([
        act("⇪", "今日一图流", "1080×1920 看板", action="export"),
        act("✂", "素材", f"{_clip_count()} 个 · 回放插入", action="clips"),
        act("⌖", "地图底图",
            "已配置 · 带底图" if _map_on() else "未配置 · 矢量轨迹",
            action="amap"),
        act("⚙", "运动设置", "跑步 / 骑行 · FTP", href="/sports"),
    ])
    # 数据与备份：导出/导入是「换手机保命」的两个动作，放同一排最显眼
    data_tiles = acts_grid([
        act("⇩", "导出数据", "打包历史与设置", action="dbExport"),
        act("⇧", "导入数据", "先备份再覆盖", action="dbImport"),
        act("◫", "产物文件", "图片 / 视频清单", href="/files"),
        act("⭡", "检查更新", f"当前 v{VER}", action="update"),
    ])
    # 诊断日志单独一行、样式压低：它只在出问题时才需要，不该抢网格的位置，
    # 但也不能藏进二级菜单——崩溃时用户复述不清，能一键复制才有用。
    diag_row = row("!", "诊断日志", "出问题时把这段发出来，能定位到具体一步",
                   act="diag", tone="a")

    about = ('<div class="card rc-about"><div class="rc-about-h">' +
             BR.badge_html(30) +
             f'<div><div class="lab">RunnerCoach · v{esc(VER)}</div>' +
             '<div class="note" style="margin-top:2px">本地优先的跑步教练</div>'
             '</div></div>'
             '<div class="note" style="margin-top:8px;font-size:12px;line-height:1.65">'
             '数据只存本机 App 私有目录，看板 / 图片 / 回放全本地生成；'
             '无 CDN、远程字体与统计，卸载即清空。'
             '<a class="link-act" href="/privacy"> 隐私说明 →</a>'
             '</div></div>')

    stat_line = (f'<div class="grid three">' +
                 stat("日汇总", f"{cov['days']} 天",
                      f"{str(cov['first'])[5:]} ~ {str(cov['last'])[5:]}") +
                 stat("活动", f"{cov['activities']} 条", "全部本地") +
                 stat("上次同步", str(st.get("last_sync") or "—")[5:10],
                     (str(st.get("last_sync") or "")[11:16] + " 同步 · 可下拉刷新").lstrip(" ·")) +
                 '</div>'
                 '<div class="note" style="margin-top:8px;line-height:1.6">'
                 '历史在后台自动补齐，进度看标题栏；补完就不再请求，无需手动操作。</div>')

    body = ('<div class="me">' +
            sec("账号与数据", sources) +
            sec("常用", tool_tiles) +
            sec("数据与备份", data_tiles) +
            '<div class="me-diag">' + diag_row + '</div>' +
            sec("本地数据", stat_line) +
            sec("关于", about) +
            '</div>')
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
  var lastKey = '', lastCount = 0, timer = null, busy = false;
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
  function render(jobs){
    // 没变化就别碰 DOM：原来每 2 秒无条件重写 innerHTML，等于把进度条的
    // transition 反复打断，还白刷一帧。
    var key = jobs.map(function(j){ return j.name + '|' + j.pct + '|' + j.stage; }).join(';');
    if (key === lastKey) return;
    lastKey = key;
    if (!jobs.length) {
      box.innerHTML = '';
      if (lastCount > 0) { location.reload(); return; }
    } else {
      box.innerHTML = '<section class="sec"><div class="sec-head">' +
        '<h2>正在生成</h2></div>' + jobs.map(box_html).join('') + '</section>';
    }
    lastCount = jobs.length;
  }
  function tick(){
    if (busy) return;              // 上一次还没回来就别叠请求
    busy = true;
    fetch('/__progress', {cache:'no-store'})
      .then(function(r){ return r.json(); })
      .then(function(d){ render(d.jobs || []); })
      .catch(function(){})
      .then(function(){
        busy = false;
        if (timer) clearTimeout(timer);
        timer = setTimeout(tick, 1500);
      });
  }
  // 原生侧在生成期间会定时戳一下：WebView 空闲时可能把定时器节流，
  // 用户「不摸屏幕就不刷新」多半就是它。暴露成全局函数供 evaluateJavascript 调用。
  window.gcTick = tick;
  document.addEventListener('visibilitychange', function(){
    if (!document.hidden) tick();
  });
  tick();
})();</script>
"""
    manage_btn = ('<button class="btn manage-btn" id="pickmode" type="button">'
                  '管理</button>' if files else "")
    return page("FILES · 产物", "本机产物文件", "看板、图片、视频都只存在这台设备上",
                jobs + sec("最近生成", rows, manage_btn) + picker,
                chip(f"{len(files)} 个"), back=("返回我的", "/me"))
