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
from . import design as D
from . import screen as SC

# 与 android values/colors.xml、aiui.CSS、report.CSS 保持同一组色值。
# 这套 token 是双向的唯一来源：改这里要同步 aiui.py 的 :root 与 MainActivity 的
# 颜色常量（docs/交互树.md 第五节有对照表）。
# token 统一来自 design.py（见 docs/设计语言.md §3）。
# 页面里不再写裸色值：颜色/字号/间距/圆角四类 token 只有这一个来源。
CSS = D.TOKENS + D.ALIASES + """
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
  font-size:12px;color:var(--brand-ink);text-decoration:none}
.back:active{opacity:.6}
/* 页头不再画收尾横线。
   原来用一道「蓝→青→透明」的 1px 渐变线给仪表盘收边，但页头下面紧跟着
   就是卡片：卡片自己有发丝线边框，再叠一条蓝渐变线，两条线相距 19px，
   看着像一条没对齐的分隔符。而且那条是蓝色的，和发丝线不是一套语言。 */
.page-head{padding:20px 0 15px;margin-bottom:4px;position:relative}
.page-head.after-back{padding-top:14px}
/* 页头不再挂右上角动作按钮：0.6.13 起一图流归到「我的」页的低频动作里。
   page() 的 action 参数仍留给二级页，首页不再传它。 */
/* 有右上角动作时给标题和副标题留出位置，长标题不会钻到按钮底下 */
.page-head.with-act h1,.page-head.with-act .lede{padding-right:92px}
/* 页头右上角的单动作按钮（组页那个齿轮）。44px 是触控目标的下限，
   不能只看图标 18px 就把它做小。 */
.hact{position:absolute;right:0;top:12px;width:44px;height:44px;border-radius:14px;
  display:flex;align-items:center;justify-content:center;font-size:16px;
  color:var(--ink2);text-decoration:none;border:1px solid var(--line);
  background:var(--surface-2)}
.hact:active{background:var(--brand-soft);border-color:var(--line-brand)}
.kicker{font-size:11px;font-weight:600;letter-spacing:2px;color:var(--cyan);
  text-shadow:0 0 12px rgba(34,211,238,.45)}
.page-head h1{margin:6px 0 4px;font-size:var(--fs-dsp,32px);font-weight:600;
  letter-spacing:-1px;line-height:1.2}
.lede{margin:0;font-size:14px;color:var(--muted)}
.meta{display:flex;gap:6px;flex-wrap:wrap;margin-top:9px}
.chip{display:inline-flex;align-items:center;gap:5px;font-size:12px;padding:3px 9px;
  border-radius:999px;background:rgba(15,22,34,.72);border:1px solid var(--line);color:var(--ink2)}
.chip.ok{background:var(--brand-soft);border-color:rgba(59,130,246,.34);color:var(--brand-ink)}
a.chip{min-height:44px;padding:0 14px;justify-content:center}
.chip b{font-weight:600;color:var(--ink)}
.chip.warn{background:var(--amber-soft);border-color:var(--warn);color:var(--warn)}
/* 状态胶囊：形状统一，只换色调。
   以前每处各写一套（绿的、蓝的、橙的、虚线的），同一屏里能出现三种圆角
   和三个字号，看着像不同人做的。现在只许用这一个组件 + 五种色调。 */
.pill{display:inline-flex;align-items:center;justify-content:center;height:26px;
  padding:0 10px;border-radius:var(--r-pill);font-size:var(--fs-micro,11px);
  font-weight:600;white-space:nowrap;border:1px solid transparent;
  font-variant-numeric:tabular-nums}
.pill.ok{color:var(--ok);background:var(--ok-soft);border-color:rgba(63,211,154,.28)}
.pill.run{color:var(--brand-ink);background:var(--brand-soft);border-color:var(--line-brand)}
.pill.warn{color:var(--warn);background:var(--warn-soft);border-color:rgba(245,165,36,.28)}
.pill.bad{color:var(--bad);background:var(--bad-soft);border-color:rgba(242,85,90,.28)}
/* 虚线灰：计划外、跳过这类「有这件事但不算计划内」的状态 */
.pill.mute{color:var(--ink-3);background:transparent;border:1px dashed rgba(148,163,184,.38)}
/* 中性胶囊：既不是状态也不是动作，只是「几个并列的选项」（今日建议、
   筛选条件）。用灰边灰字，别和表示状态的彩色胶囊抢注意力。 */
.pill.ghost{color:var(--ink-2);background:var(--surface-2);
  border-color:var(--line)}
/* 实心品牌色：只给「今天」这种唯一的当前项，一屏最多一个 */
.pill.brand{color:var(--ink-inv);background:var(--brand);border-color:transparent}
.sec{margin-top:24px}
.sec-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:0 0 10px}
/* 区块标题从 12px 字距 1.4px 的小标改成 16px 粗体：小标那套是「杂志目录」
   的做法，在手机上字太小、层级反而低于正文里的数字。 */
.sec-head h2{margin:0;font-size:var(--fs-title,16px);font-weight:600;letter-spacing:0;
  color:var(--ink);position:relative;padding-left:11px}
/* 竖条：纯品牌色、16px 高。原来用青→蓝渐变 + 青色外发光，一屏六七个
   区块就有六七个发光点，hero 的焦点被稀释掉了。 */
.sec-head h2::before{content:"";position:absolute;left:0;top:50%;width:3px;height:16px;
  margin-top:-8px;border-radius:2px;background:var(--brand)}
.sec-head .more{font-size:var(--fs-body,14px);color:var(--brand-ink);
  display:inline-flex;align-items:center;min-height:44px;padding:0 2px}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.grid.three{grid-template-columns:repeat(3,minmax(0,1fr))}
.grid.one{grid-template-columns:1fr}
/* 恢复度卡里的 4 个分项：一行两项、各带一条 3px 的分数条。
   原来这里是「睡眠 35% / HRV 30% …」一整行字，读得出权重却读不出得分。 */
.mm{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:14px}
.mm .i{background:rgba(9,14,22,.52);border:1px solid var(--line2);
  border-radius:10px;padding:8px 10px;min-width:0}
.mm .n{font-size:11px;color:var(--muted);letter-spacing:.3px}
.mm .v{font-size:14px;font-weight:600;margin-top:1px;
  font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mm .d{font-size:11px;color:var(--faint);margin-top:1px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mm .b{height:3px;border-radius:999px;background:var(--line);
  overflow:hidden;margin-top:6px}
.mm .b i{display:block;height:100%;border-radius:999px;
  background:linear-gradient(90deg,var(--brand),var(--cyan))}
/* 睡眠 / 体重这类「一项一个数」的卡：大数字 + 一行说明 + 迷你趋势线 */
/* 竖向数据格：--surface-2 底，不加边框和阴影（卡里套卡会显脏） */
.vital{background:var(--surface-2);border-radius:var(--r-2);padding:12px 13px 10px;
  min-width:0}
.vital .k{font-size:11px;letter-spacing:.6px;color:var(--muted)}
.vital .num{font-size:24px;font-weight:600;line-height:1.15;margin-top:3px;
  letter-spacing:-.6px;font-variant-numeric:tabular-nums;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vital .num small{font-size:12px;font-weight:500;letter-spacing:0;
  color:var(--muted);margin-left:3px}
.vital .sub{font-size:12px;color:var(--muted);margin-top:4px;line-height:1.5;
  display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;
  overflow:hidden}
.vital .sub2{font-size:12px;color:var(--faint);margin-top:2px;line-height:1.5;
  display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;
  overflow:hidden}
.vital .spark{margin-top:6px;display:block}
.vital .none{font-size:12px;color:var(--faint);margin-top:8px}
/* 普通卡片：--surface 平底 + 1px 发丝线（§4.3）。
   原来是 180° 深蓝渐变 + 顶沿一道蓝色高光：全站三十多张卡各有一道高光，
   叠在一起就是「满屏科技感」的来源，而参考稿的卡是平的。 */
.card{background:var(--surface);
  border:1px solid var(--line);border-radius:var(--r);padding:16px;
  box-shadow:var(--shadow);position:relative;overflow:hidden}
.card.flat{background:transparent;border-style:dashed;box-shadow:none}
.card.flat::after{display:none}
.card.brand{background:
  radial-gradient(340px 190px at 84% -10%, rgba(59,130,246,.32), transparent 70%),
  linear-gradient(180deg,rgba(20,32,52,.96),rgba(12,19,31,.96));
  border-color:var(--line-brand);box-shadow:var(--glow)}
.card h3{margin:0 0 10px;font-size:12px;font-weight:600;letter-spacing:1.2px;color:var(--muted)}
.hero{display:flex;align-items:flex-end;justify-content:space-between;gap:14px;flex-wrap:wrap}
/* 主视觉数字：实色 + 负字距。
   原来走「白→蓝」渐变 + 投影辉光，被设计语言 §3.1 规则 6 明确禁掉——
   渐变文字在高分屏上边缘发虚，外发光又和 hero 的唯一焦点打架。
   现在是 --ink 实色，靠 32px 的字号和字重抓眼，不需要底色渐变。 */
.big{font-size:32px;font-weight:600;line-height:1;letter-spacing:-1.2px;
  font-variant-numeric:tabular-nums;color:var(--ink)}
.big small{font-size:14px;font-weight:500;letter-spacing:0;color:var(--muted);
  margin-left:6px;-webkit-text-fill-color:var(--muted);filter:none}
.hero-side{text-align:right;font-size:12px;color:var(--muted)}
.hero-side b{display:block;font-size:15px;color:var(--ink)}
.bar{height:6px;border-radius:999px;background:var(--brand-soft);overflow:hidden;margin-top:14px}
.bar i{display:block;height:100%;border-radius:999px;
  background:linear-gradient(90deg,var(--cyan),var(--brand));
  box-shadow:0 0 14px rgba(59,130,246,.75)}
.lab{font-size:12px;color:var(--muted);letter-spacing:.2px}
.val{font-size:20px;font-weight:600;line-height:1.2;margin-top:4px;
  font-variant-numeric:tabular-nums;color:var(--ink);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.val.sm{font-size:20px}
.note{font-size:12px;color:var(--faint);margin-top:3px}
.row{display:flex;align-items:center;gap:12px;
  background:var(--surface);
  border:1px solid var(--line);border-radius:14px;padding:12px 14px;margin-bottom:8px;
  color:inherit;cursor:pointer;box-shadow:var(--shadow)}
.row:active{background:var(--brand-soft);border-color:var(--line-brand)}
/* 当前选中的周期行：左侧一道蓝条 + 蓝底。滚动列表里要一眼看出
   「现在这张柱状图讲的是哪个月」，否则点完还是一排一模一样的行。 */
.row.on{background:var(--surface-2);border-color:var(--line-brand);
  box-shadow:inset 2px 0 0 var(--brand)}
.row.on .t{color:var(--ink)}
.row.on .go{color:var(--brand-ink)}
/* 图标底座：--surface-2 + 品牌色字形（§4.7）。
   原来是蓝青渐变 + 内发光，一屏十几行就是十几块霓虹方块。 */
.row .ic{flex:0 0 auto;width:34px;height:34px;border-radius:var(--r-1);display:flex;
  align-items:center;justify-content:center;font-size:15px;font-weight:600;
  background:var(--surface-2);color:var(--brand-ink);border:1px solid var(--line)}
.row .ic.b{color:var(--brand-ink)}
.row .ic.a{color:var(--warn)}
.row .tx{min-width:0;flex:1}
/* 标题最多两行：活动名是「日期 时间 · 名称」拼出来的，长一点就被截成
   省略号，看不出是哪次。参数行本来就会换行，不用 nowrap。 */
.row .t{font-size:14px;font-weight:600;line-height:1.35;
  display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;
  overflow:hidden;word-break:break-word}
.row .d{font-size:12px;color:var(--muted);margin-top:2px;font-variant-numeric:tabular-nums}
.row .go{margin-left:auto;color:var(--faint);font-size:16px;flex:0 0 auto}
/* 行尾开关：和数据源那种「当前用哪个」不同，它表示服务开不开。
   视觉上只有 38×22，但**命中区做到 44×44**——22px 高的可点元素手指按不准，
   audit 也会判「矮可点」。轨道用 ::before 画，滑块仍由 <i> 表示。
   点击命中开关自己，事件委托从 e.target 往上找，不会连带触发整行动作。 */
.row .sw{position:relative;flex:0 0 auto;margin-left:auto;width:44px;height:44px;
  cursor:pointer}
.row .sw::before{content:"";position:absolute;left:3px;top:11px;width:38px;height:22px;
  border-radius:10px;background:var(--line-strong);transition:background .18s}
.row .sw i{position:absolute;left:5px;top:13px;width:18px;height:18px;border-radius:50%;
  background:var(--ink-inv);transition:transform .18s,background .18s}
.row .sw.on::before{background:var(--ok)}
.row .sw.on i{transform:translateX(16px);background:var(--bg)}
.acts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
/* 「我的」页 2 列网格：图标在左，标题 + 一行说明在右，整卡固定 68px，
   与账号行（67px）同一节奏。旧版是竖排「图标 / 标题 / 最多两行说明」，
   副标题有的 1 行有的 2 行，被 grid 拉伸后同排 136px、下一排只有 119px，
   两组网格之间也差一截；说明一律单行省略后所有卡片严格等高。 */
.act{display:grid;grid-template-columns:28px minmax(0,1fr);
  grid-template-rows:auto auto;column-gap:9px;row-gap:2px;
  align-content:center;box-sizing:border-box;height:68px;padding:8px 10px;
  background:var(--surface);
  border:1px solid var(--line);border-radius:var(--r);color:inherit;
  cursor:pointer;box-shadow:var(--shadow);min-width:0}
.act:active{background:var(--brand-soft);border-color:var(--line-brand)}
.act .ai{grid-row:1/3;align-self:center;width:28px;height:28px;border-radius:10px;
  display:flex;align-items:center;justify-content:center;font-size:14px;
  background:var(--surface-2);
  color:var(--brand-ink);border:1px solid var(--line)}
.act .at,.act .ad{min-width:0;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}
.act .at{font-size:14px;font-weight:600;line-height:1.25;color:var(--ink)}
.act .ad{display:block;font-size:11px;line-height:1.35;color:var(--muted)}
/* 只读卡片（如「AI 模型服务」）：不暗示可点，也就不该有按下态 */
.act.flat{cursor:default}
.act.flat:active{background:var(--surface);border-color:var(--line)}
/* 只收紧「我的」页的节奏，别的 tab 留白一概不动 */
.me .sec{margin-top:16px}
.me .sec-head{margin-bottom:8px}
.me-diag{margin-top:10px}
.me .grid.three .card{box-sizing:border-box;height:88px;padding:7px 9px}
.me .grid.three .val{font-size:20px;line-height:1.15;margin-top:2px}
.me .grid.three .note{font-size:11px;line-height:1.3;margin-top:2px;
  display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;
  overflow:hidden}
.me .rc-about{padding:12px 14px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;
  border:1px solid var(--line);background:rgba(15,22,34,.72);color:var(--ink2);
  border-radius:999px;padding:0 16px;min-height:44px;font-size:14px;
  font-family:inherit;cursor:pointer}
/* 主按钮：品牌实色 + 白字（§4.6）。
   原来是蓝→青渐变 + 深色字 + 蓝影，一屏几个就变成一片青光；
   现在只靠实色和字重区分层级，一屏最多一个。 */
.btn.primary{background:var(--brand);border-color:transparent;
  color:var(--ink-inv);font-weight:600}
.btn:active{opacity:.85}
.empty{padding:20px 4px;font-size:14px;color:var(--muted);line-height:1.8}
.link-act{display:inline-flex;align-items:center;min-height:44px;
  color:var(--brand-ink);text-decoration:none}
.foot{margin-top:30px;padding-top:14px;border-top:1px solid var(--line2);
  font-size:11px;color:var(--faint);line-height:1.8}
form.filters{display:flex;gap:9px;flex-wrap:wrap;align-items:center}
form.filters label{display:inline-flex;gap:5px;align-items:center;font-size:12px;color:var(--muted)}
input,select{font-family:inherit;font-size:12px;color:var(--ink);
  background:var(--surface-2);border:1px solid var(--line);border-radius:10px;
  padding:7px 9px;max-width:100%}
input:focus,select:focus{outline:none;border-color:var(--brand);
  box-shadow:0 0 0 3px var(--brand-soft)}
.pager{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:12px}
.tags{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
/* 训练计划：今日课表 + 本周 7 天 */
.plan-hero{background:var(--surface);
  border:1px solid var(--line);border-radius:var(--r-3);
  padding:16px 18px;position:relative;overflow:hidden}
.plan-hero .t{font-size:var(--fs-h1,24px);font-weight:600;line-height:1.3;
  letter-spacing:-.6px}
/* 本周完成的 6 段小条：比「2/6 次」更容易一眼看出还剩几次 */
.wseg{display:flex;gap:4px;align-items:center;margin-top:12px}
.wseg i{flex:0 0 auto;width:26px;height:6px;border-radius:999px;
  background:var(--line-strong)}
.wseg i.on{background:var(--brand)}
.plan-hero .d{font-size:14px;color:var(--ink2);margin-top:6px;line-height:1.7}
.plan-hero .n{font-size:12px;color:var(--muted);margin-top:10px}
.plan-hero .n + .n{margin-top:4px}
.wk{display:flex;flex-direction:column;gap:7px}
/* 周课表行：日期列 | 内容 | 状态贴右。
   日期列原来是 40px，「周二」两字加日期就装不下，中文被逐字折行、和卡片
   重叠——设计语言 §4.8 记的就是这个 bug。现在 56px + 一道竖发丝线，
   日期和课名分开，眼睛不用猜「周四」是日期还是课名的一部分。 */
.wrow{display:grid;grid-template-columns:56px minmax(0,1fr) auto;
  align-items:center;column-gap:12px;
  background:var(--surface);
  border-radius:var(--r-2);padding:11px 13px;border:1px solid var(--line)}
.wrow .wd{display:flex;flex-direction:column;gap:1px;white-space:nowrap;
  align-self:stretch;justify-content:center;padding-right:9px;
  box-shadow:inset -1px 0 0 var(--line);
  font-size:15px;font-weight:600;color:var(--ink);line-height:1.25}
.wrow .wd i{font-style:normal;font-size:11px;font-weight:400;color:var(--ink-3)}
.wrow .wb{min-width:0;font-size:16px;font-weight:600;line-height:1.4}
.wrow .wb em{display:block;font-style:normal;font-size:12px;color:var(--muted);
  font-weight:400;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* 今天这一行：整行描品牌色边 + 抬升面。
   原来是「左侧 3px 条」——一条竖线在整行里太弱，扫一眼分不出哪行是今天。 */
.wrow.today{background:var(--surface-2);border-color:var(--line-brand);
  box-shadow:0 0 0 1px var(--line-brand)}
.wrow.today .wd{color:var(--brand-ink);box-shadow:inset -1px 0 0 rgba(96,165,250,.22)}
.wrow.rest .wb{color:var(--muted);font-weight:400}
/* 草稿页要看清「为什么这么排」和每节课的说明，不能像周视图那样省略号截断 */
.whyrow{display:flex;gap:8px;font-size:12px;color:var(--ink2);
  padding:7px 0;border-bottom:1px solid var(--line2)}
.whyrow:last-child{border-bottom:0}
.whyrow .wno{flex:0 0 14px;color:var(--muted);font-variant-numeric:tabular-nums}
.whyrow .wtxt{flex:1 1 auto;min-width:0;line-height:1.55;word-break:break-word}
.wrow.dwk .wb em{white-space:normal;overflow:visible;text-overflow:clip;line-height:1.5}
.override{display:flex;align-items:center;gap:6px;font-size:12px;
  color:var(--muted);margin-top:10px;line-height:1.5}
.override input{width:16px;height:16px;accent-color:var(--brand)}
/* 表单里的开关：纯 CSS，状态由 input:checked 驱动，提交时跟着表单走。
   和「我的」页那个 .sw 不一样——那个走 data-act 调原生，这个必须能被 submit。 */
.tsw{position:relative;display:inline-block;width:46px;height:26px;
  flex:0 0 46px;cursor:pointer}
.tsw input{position:absolute;inset:0;width:100%;height:100%;margin:0;
  opacity:0;cursor:pointer;z-index:1}
.tsw i{position:absolute;inset:0;border-radius:999px;
  background:var(--line-strong);transition:background .16s;pointer-events:none}
.tsw i::after{content:"";position:absolute;left:3px;top:3px;width:20px;height:20px;
  border-radius:50%;background:var(--ink-3);
  transition:transform .16s,background .16s}
.tsw input:checked + i{background:var(--ok)}
.tsw input:checked + i::after{transform:translateX(20px);background:var(--bg)}
.tsw input:focus-visible + i{box-shadow:0 0 0 3px rgba(59,130,246,.3)}
.kvline{display:flex;justify-content:space-between;gap:12px;font-size:12px;
  color:var(--ink2);padding:7px 0;border-bottom:1px solid var(--line2)}
.kvline:last-child{border-bottom:0}
.kvline b{font-weight:600;color:var(--ink);font-variant-numeric:tabular-nums}
/* 训练小组 */
.grow{display:flex;align-items:center;gap:10px;padding:11px 0;
  border-bottom:1px solid var(--line2);color:inherit;text-decoration:none}
.grow:last-child{border-bottom:0}
/* 组名后面的角色小标签（组长/成员）：不给样式会直接粘成名的一部分 */
.grow .gname .sub{font-size:11px;font-weight:400;color:var(--faint);margin-left:6px}
.grow .gname{flex:1 1 auto;min-width:0;font-size:14px;font-weight:600;color:var(--ink);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.grow .gmeta{flex:0 0 auto;font-size:11px;color:var(--muted);text-align:right;line-height:1.4}
.grow .chev{flex:0 0 auto;color:var(--muted);font-size:15px}
.rank{display:flex;align-items:center;gap:9px;padding:9px 0;
  border-bottom:1px solid var(--line2)}
.rank:last-child{border-bottom:0}
.rank .no{flex:0 0 22px;font-size:12px;color:var(--muted);
  font-variant-numeric:tabular-nums}
.rank .nm{flex:1 1 auto;min-width:0;font-size:14px;color:var(--ink2);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rank .sub{font-size:11px;color:var(--muted);margin-left:5px}
.rank .km{flex:0 0 auto;font-size:14px;font-weight:600;color:var(--ink);
  font-variant-numeric:tabular-nums}
.rank.me{background:var(--brand-soft);border-radius:10px;margin:0 -8px;padding:9px 8px}
.chal{padding:10px 0;border-bottom:1px solid var(--line2)}
.chal:last-child{border-bottom:0}
.chal .t{font-size:14px;font-weight:600;color:var(--ink)}
.chal .m{font-size:11px;color:var(--muted);margin-top:3px}
.bar{height:5px;border-radius:3px;background:var(--line2);margin-top:7px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--brand);border-radius:3px}
.bar.done i{background:var(--ok)}
/* 无框区块：组页里除了「远征」那张入口，其余数据块都直接铺在页面上。
   每条卡都有 1px 边框 + 圆角 + 内阴影，六个区块叠起来就是一堵墙；
   数据本身有分隔线，不需要再给它们各发一个盒子。 */
.flat{padding:2px 0}
/* 折叠：一年用一次的（建赛事、换邀请码）默认收起，行本身当标题用。
   summary 去掉默认三角，换成右侧一个会转的 ›，展开与否一眼能看出来。 */
.fold{border-top:1px solid var(--line2);margin-top:6px}
.fold>summary{list-style:none;cursor:pointer;display:flex;align-items:center;
  gap:8px;min-height:44px;font-size:12px;color:var(--brand-ink)}
.fold>summary::-webkit-details-marker{display:none}
.fold>summary .fx{margin-left:auto;color:var(--muted);transition:transform .15s}
.fold[open]>summary .fx{transform:rotate(90deg)}
/* 展开态的「再加一个 / 建一个」是个入口标题，不是「更多」小尾巴：
   提到正文档、改用正文色，并给下面卡片留出呼吸。 */
.fold-lg>summary{font-size:var(--fs-body,14px);font-weight:600;color:var(--ink);
  padding-bottom:8px}
.fold-lg>summary .fx{color:var(--ink-3)}
/* 设置项一行一个：左边名字、右边控件，没有外框，只靠间距分组 */
/* 分段控件：一条胶囊槽 + 选中块。一屏里两组（粒度 / 运动类型）用同一形态，
   比两排散装 chip 更容易看出「这是单选」。 */
/* 分段控件：一条胶囊槽 + 选中块。
   宽度跟内容走（max-content），不铺满整行——铺满时四五个选项被拉得一样宽，
   看着像导航栏，反而看不出「这是一组单选」。 */
.seg{display:flex;gap:3px;background:var(--surface-2);border-radius:var(--r-pill);
  padding:3px;margin-bottom:8px;width:max-content;max-width:100%}
.seg a{flex:0 1 auto;min-height:44px;display:flex;align-items:center;
  justify-content:center;border-radius:var(--r-pill);text-decoration:none;
  font-size:var(--fs-cap,12px);color:var(--ink-2);white-space:nowrap;padding:0 14px;
  border:1px solid transparent}
/* 选中块用品牌色描边 + 淡底：和 `.pill.run` 同一套语义，用户不用重新学 */
.seg a.ok{background:var(--brand-soft);border-color:var(--line-brand);
  color:var(--ink);font-weight:600}
/* 历史概览：三个数在一张卡里，用竖线切成三格。
   三张独立小卡会把「本月 / 本年 / 累计」读成三个并列指标，其实是一句
   递进的话；一格一竖线才是同一件事的三个时间尺度。 */
.ovrow{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));
  background:var(--surface);border:1px solid var(--line);
  border-radius:var(--r-3);padding:14px 0;margin:2px 0 0}
.ovc{padding:0 12px;min-width:0}
.ovc+.ovc{border-left:1px solid var(--line)}
.ovc .v{font-size:var(--fs-h1,24px);font-weight:600;letter-spacing:-.8px;
  color:var(--ink);font-variant-numeric:tabular-nums;white-space:nowrap}
.ovc .v small{font-size:var(--fs-micro,11px);font-weight:500;letter-spacing:0;
  color:var(--ink-3);margin-left:3px}
.ovc .k{margin-top:2px;font-size:var(--fs-micro,11px);color:var(--ink-3);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* 周期行：左列「周期 + 状态」/ 中列「数据 + 横条」/ 右侧箭头。
   原来一行里塞「图标 + 月份·中文月 + 次数·里程·小时」，中文月和数字挤在
   一起，一列读下来完全没有对齐的边。 */
.prow{display:grid;grid-template-columns:auto minmax(0,1fr) 12px;
  align-items:center;column-gap:14px;margin-bottom:8px;padding:11px 13px;
  border-radius:var(--r-2);background:var(--surface);border:1px solid var(--line);
  text-decoration:none;color:inherit}
.prow.on{background:var(--surface-2);border-color:var(--line-brand);
  box-shadow:0 0 0 1px var(--line-brand)}
.pm{display:flex;flex-direction:column;gap:3px;white-space:nowrap;
  font-size:var(--fs-lead,15px);font-weight:600;color:var(--ink);
  font-variant-numeric:tabular-nums}
.pm i{font-style:normal;font-size:var(--fs-micro,11px);font-weight:400;color:var(--ink-3)}
.pm i.now{color:var(--brand-ink)}
.pd{min-width:0;font-size:var(--fs-cap,12px);color:var(--ink-2);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pb{display:block;height:4px;margin-top:7px;border-radius:999px;
  background:var(--line);overflow:hidden}
.pb i{display:block;height:100%;border-radius:999px;background:var(--brand)}
.prow .go{font-size:16px;color:var(--ink-3);line-height:1}
.prow.on .go{color:var(--brand-ink)}
/* 连续空周期合并行：虚线边一眼看出「这里没有数据但可以展开」，
   实线会和真实数据行混在一起。 */
.gap{margin:0 0 8px}
.gap>summary{display:flex;align-items:center;justify-content:space-between;
  gap:10px;min-height:44px;padding:0 13px;list-style:none;cursor:pointer;
  border:1px dashed var(--line);border-radius:var(--r-2);
  font-size:12px;color:var(--ink-3)}
.gap>summary::-webkit-details-marker{display:none}
.gap[open]>summary{border-style:solid;color:var(--ink-2)}
.gap .gx{font-size:11px;color:var(--ink-3)}
.gap[open] .gx{opacity:0}
.gap .gi{padding:6px 13px;font-size:11px;color:var(--ink-3)}
.setrow{display:flex;align-items:center;gap:10px;min-height:44px;
  border-bottom:1px solid var(--line2)}
.setrow:last-of-type{border-bottom:0}
/* 标签列固定 52px：「转让组长」「路线共享」都是四字，44px 会折成两行。
   只钉第一个 span，后面的说明文字要能自己撑开和换行。 */
.setrow>span{font-size:12px;color:var(--muted)}
.setrow>span:first-child{flex:0 0 52px;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.setrow input,.setrow select{flex:1 1 auto;min-width:0}
.setrow>.btn,.setrow>button{flex:0 0 auto}
.btn.sm{min-height:34px;padding:0 12px;font-size:12px;border-radius:10px}
/* 「我的」页三个数字一行说完：它们是一句话的三个分句，不需要各发一张卡 */
.minirow{display:flex;flex-wrap:wrap;gap:4px 16px;padding:2px 0;
  font-size:12px;color:var(--muted)}
.minirow b{font-size:14px;font-weight:600;color:var(--ink);
  font-variant-numeric:tabular-nums;margin-right:2px}
/* 一行小字：把「还有谁没共享 / 谁是组长」这种备注压在榜单底下，
   不再单开一个「成员」区块——成员就是榜上那些人。 */
.tinyline{font-size:11px;color:var(--faint);margin-top:8px;line-height:1.5}
/* 本周成绩条：我第几、多少分、穿着哪件衫。一行说完，不做榜单页。 */
.sline{display:flex;align-items:baseline;flex-wrap:wrap;gap:4px 9px;
  padding:9px 0 2px}
.sline .rk{font-size:12px;color:var(--ink2);font-weight:600}
.sline b{font-size:20px;font-weight:600;color:var(--ink);
  font-variant-numeric:tabular-nums;letter-spacing:-.4px;line-height:1}
.sline b i{font-style:normal;font-size:11px;font-weight:400;
  color:var(--faint);margin-left:2px}
.sline .tt{font-size:11px;color:var(--warn);background:var(--warn-soft);
  border:1px solid var(--warn-soft);border-radius:999px;padding:1px 7px}
/* 衫：一个小色块 + 名字。六件同时出现也不会挤，因为字号压到 10.5 */
.sbit{display:inline-flex;align-items:center;gap:5px;font-size:11px;
  color:var(--muted);white-space:nowrap}
/* 9px 而不是 8px：圆点衫要在这一颗小点上画波点，8px 只能塞下 2×2 个红点，
   远看和白衫没有区别；多给 1px 就能看出「白底红点」这件事。 */
.sbit i{width:9px;height:9px;border-radius:50%;flex:0 0 9px;
  box-shadow:0 0 0 1px rgba(255,255,255,.2) inset}
.sline2{display:flex;flex-wrap:wrap;gap:4px 10px;padding:2px 0 8px}
/* Bingo 5×5。grid item 默认 min-height:auto，会被文字撑高，一行里有长任务
   时整行就会变高、格子不再方正；所以显式 min-height:0 + overflow:hidden。 */
.bgrid{display:grid;grid-template-columns:repeat(5,1fr);gap:6px;margin:2px 0 10px}
.bcell{aspect-ratio:1/1;min-height:0;overflow:hidden;border-radius:10px;padding:3px;
  border:1px solid var(--line2);background:rgba(255,255,255,.015);
  display:flex;align-items:center;justify-content:center;text-align:center;
  font-size:11px;line-height:1.3;color:var(--faint);letter-spacing:-.2px}
.bcell.on{background:var(--brand-soft);border-color:rgba(59,130,246,.34);color:var(--ink)}
.bcell.free{background:var(--cyan-soft);border-color:rgba(34,211,238,.3);color:var(--cyan)}
/* 组内路线图。卡片贴边、图例压在下面一行——地图是主体，图例别抢高度。 */
.gmapcard{padding:0;overflow:hidden}
.gmap{display:block;width:100%;height:auto}
.gmap .mapsrc{font:10px/1 -apple-system,system-ui,sans-serif;fill:rgba(232,240,251,.5)}
.lgs{display:flex;flex-wrap:wrap;gap:6px 12px;padding:10px 12px 12px;
  border-top:1px solid var(--line2)}
.lgi{display:inline-flex;align-items:center;gap:5px;font-size:11px;color:var(--ink2)}
.lgi i{width:9px;height:9px;border-radius:2px;flex:0 0 9px}
.lgi b{font-weight:600;color:var(--muted)}
/* 共同跑过的格子：白色,和地图上那条白带对应 */
.lgi i.sh{background:var(--map-shared)}
/* 跨省分片：每片一个小标题 + 一张图。标题给地名和格数，
   不然三张图排下来分不清哪张是哪儿。 */
.shard-h{display:flex;align-items:baseline;justify-content:space-between;
  gap:10px;padding:14px 2px 6px;font-size:12px;font-weight:600;color:var(--ink)}
.shard-h:first-child{padding-top:2px}
.shard-h i{font-style:normal;font-size:11px;font-weight:400;color:var(--faint)}
/* setrow 的左侧标签默认会被 flex 压缩换行（「路线共／享」），有需要时钉住 */
.nowrap{white-space:nowrap;flex:0 0 auto}
/* 六件衫：一行一件。左色点 / 中榜名 / 右得主与成绩 / 下面一行小字写口径和前 2、3 名。
   不用卡片：这是张榜单，不是六个功能入口。 */
.shirtlist .shr{display:grid;grid-template-columns:10px 1fr auto;gap:2px 9px;
  padding:9px 0;border-bottom:1px solid var(--line2)}
.shirtlist .shr:last-child{border-bottom:0}
.shr .dot{width:10px;height:10px;border-radius:50%;margin-top:3px;
  box-shadow:0 0 0 1px rgba(255,255,255,.34), inset 0 0 0 1px rgba(0,0,0,.18)}
/* 圆点衫：白底红点。它和白衫的色号都是白，纯色画出来是两个一模一样的小点，
   所以按公路车赛的规矩画波点——这是唯一能在一颗 8–10px 的点上区分开的办法。 */
/* 圆点衫：白底红点 + 一圈红边。
   只画波点不够——9px 的点上 1px 的红点被抗锯齿冲淡成粉色，远看仍是一个白点。
   加红边不是为了好看，是为了在 9px 这个尺寸上还能一眼和白衫分开：
   白衫是「纯白 + 白边」，圆点衫是「白心 + 红边」。 */
i.pd{background:var(--map-shared);
  background-image:radial-gradient(#C6342E 1.15px, rgba(198,52,46,0) 1.3px);
  background-size:2.9px 2.9px;background-position:.55px .55px;
  box-shadow:0 0 0 1.4px #D9534F}
.shr .t{font-size:14px;font-weight:600;color:var(--ink)}
.shr .v{font-size:12px;color:var(--ink2);text-align:right;white-space:nowrap}
.shr .v b{font-weight:600;color:var(--ink)}
.shr .v i{font-style:normal;color:var(--muted);margin-left:5px}
.shr .v .none{color:var(--faint);font-weight:400}
.shr .h{grid-column:2/4;font-size:11px;color:var(--faint);line-height:1.5}
.shr .r3{margin-left:8px}
.shr .r3 .rest{margin-right:8px}
/* 小组页的「衫 / 目标 / 结对 / 预言」四行。
   类名特意用 .kvrows 而不是 .wk——.wk 已经是计划页周课表的容器，
   两处同名会让周课表被套进这里的 24px 列里，变成竖排叠字。 */
/* 两列：标签列固定 24px，内容列自适应。原来那版把三项塞进 flex 换行，
   每项语序还不一样（目标…已跑…达成 / 结对…我/他…逆袭），读起来像三句
   不相干的话。 */
.kvrows{display:grid;grid-template-columns:24px minmax(0,1fr);gap:4px 8px;
  padding:2px 0 8px;font-size:11px;color:var(--ink2);line-height:1.5}
.kvrows .k{color:var(--ink-3)}

.kvrows .v{min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.kvrows .v.wrap{white-space:normal;overflow:visible;display:flex;flex-wrap:wrap;
  gap:2px 9px}
/* 左右都要留白：只给左边距的话，「已跑 38.1」会和「达成」粘成「38.1达成」 */
.kvrows .v s{text-decoration:none;color:var(--muted);margin:0 7px}
.kvrows .v b{font-weight:600;color:var(--ink)}
.kvrows .v b.hit{color:var(--cyan)}
/* 功能入口：4 列并列小图标。数据用行、功能用图标，两类东西形态不同才好扫。
   不加卡片边框——加了就是四张大卡片，占掉一行多的高度；这里要的是「一排
   应用图标」，主体应该是图标本身。 */
.qgrid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;
  padding:4px 0 10px}
.qg{display:flex;flex-direction:column;align-items:center;gap:5px;
  padding:7px 2px 5px;border-radius:14px;text-decoration:none;min-width:0}
.qg:active{background:var(--brand-soft)}
.qg i{width:34px;height:34px;border-radius:10px;display:flex;align-items:center;
  justify-content:center;font-size:16px;font-style:normal;
  background:linear-gradient(160deg,var(--cyan-soft),var(--brand-soft));
  color:var(--cyan);border:1px solid var(--line);
  box-shadow:inset 0 0 14px rgba(34,211,238,.2)}
.qg .qn{font-size:11px;font-weight:600;color:var(--ink2);white-space:nowrap;
  max-width:100%;overflow:hidden;text-overflow:ellipsis}
/* 动态：一行一条。左色点认人，中间类型+距离，右边日期和评论数。
   没有缩略图——这一栏按设计就是概要，给不出活动图。 */
.frow{display:grid;grid-template-columns:8px minmax(0,auto) minmax(0,1fr) auto auto;
  align-items:center;gap:0 8px;padding:10px 0;border-bottom:1px solid var(--line2);
  color:inherit;text-decoration:none}
.frow:last-child{border-bottom:0}
.fd{width:8px;height:8px;border-radius:50%;box-shadow:0 0 0 1px rgba(255,255,255,.2) inset}
.fn{font-size:14px;font-weight:600;color:var(--ink);white-space:nowrap}
.fn .fk{font-style:normal;font-weight:400;font-size:11px;color:var(--muted);margin-left:6px}
.fv{font-size:11px;color:var(--ink2);white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}
.ft{font-size:11px;color:var(--faint);white-space:nowrap}
.fc{font-size:11px;color:var(--brand-ink);white-space:nowrap}
/* 评论：表情一排按钮 + 一句话输入 */
.erow{display:flex;gap:6px;flex-wrap:wrap;padding:2px 0 8px}
.eform{margin:0}
.ebtn{font-size:20px;line-height:1;padding:7px 10px;border-radius:10px;
  border:1px solid var(--line2);background:rgba(255,255,255,.02);cursor:pointer}
.ebtn:active{background:var(--brand-soft);border-color:var(--line-brand)}
.cm{display:flex;align-items:baseline;gap:7px;padding:7px 0;
  border-bottom:1px solid var(--line2);font-size:12px;color:var(--ink2)}
.cm:last-child{border-bottom:0}
.cm b{font-size:12px;font-weight:600;color:var(--ink);white-space:nowrap}
.cm .ce{font-size:16px;line-height:1}
/* 计划编辑：一行一天（左日期 / 中类型 / 右里程与时长）。
   手机上宽度紧，用 flex 固定前两列、只让数字输入弹性收缩。 */
/* 计划编辑：一行一天。用 grid 而不是 flex——select 的固有宽度由最长选项决定，
   flex 收缩时它会把后面的输入框挤出卡片；grid 的 minmax(0,1fr) 才是真的可缩。 */
/* 计划编辑：一天一块，两行堆叠（上：日期 + 类型；下：数值）。
   试过 flex/grid/table 三种横排，select 的固有宽度都会在 360 宽的手机上把
   输入框挤出卡片；堆成两行最笨但绝对不会溢出，手指也更好点。 */
.prow{padding:10px 0;border-bottom:1px solid var(--line2)}
.prow:last-child{border-bottom:0}
.prow .p1{display:flex;align-items:center;justify-content:space-between;gap:10px}
.prow .p1 b{font-size:14px;color:var(--ink);font-weight:600}
.prow .p1 b span{font-size:11px;color:var(--muted);margin-left:6px;font-weight:400}
.prow .p1 select{width:118px;flex:0 0 118px;padding:6px 6px;font-size:12px}
.prow .p2{display:flex;align-items:center;gap:7px;margin-top:7px}
.prow .p2 input[type=number]{flex:1 1 auto;min-width:0;padding:8px 10px;
  font-size:14px;text-align:center}
.prow .p2 .unit{flex:0 0 auto;font-size:12px;color:var(--muted)}
.prow .pinfo{font-size:12px;color:var(--muted);margin-top:6px}
.prow.past{opacity:.5}
.prow.cur{background:var(--brand-soft);border-radius:14px;
  margin:0 -10px;padding:10px}
/* 整块可点的计划条：点击面积从「一个小链接」扩到整张卡。
   里面的排版全交给 .rc-plan 的 flex，这里只负责别把链接样式漏进去。 */
a.rc-plan-link{color:inherit;text-decoration:none;cursor:pointer;
  -webkit-tap-highlight-color:transparent;transition:border-color .16s}
a.rc-plan-link:active{transform:scale(.997)}
a.rc-plan-link:focus-visible{outline:2px solid var(--line-brand);outline-offset:2px}
a.rc-plan-link .chev{flex:0 0 auto;font-size:20px;line-height:1;color:var(--muted);
  opacity:.65;margin-left:2px}
/* 表单里的数字输入统一收窄：默认宽度会把卡片撑破（手机上尤其明显） */
.kvline b{flex:0 0 auto;min-width:0;display:flex;justify-content:flex-end}
.kvline input[type=number]{width:88px;min-width:88px;padding:5px 6px;
  font-size:12px;text-align:center}
.kvline input[type=checkbox]{width:20px;height:20px;min-width:0;
  accent-color:var(--brand)}
@media(max-width:380px){
  /* 只有普通 .grid 退成单列；.acts 是 68px 的紧凑卡，360 宽下两列仍放得下，
     退成单列会让「我的」页凭空多出 4 行、又变回长列表。 */
  .grid{grid-template-columns:1fr}
  .big{font-size:32px}
}
@media(max-width:340px){.acts{grid-template-columns:1fr}}
""" + BR.BRAND_CSS + SC.SCREEN_CSS

# 看板是独立的宽版 HTML：在 App 里强制单列/收紧留白，别让三栏挤成一小坨。
DASH_FIX = """
.wrap{max-width:820px;padding:0 14px calc(30px + env(safe-area-inset-bottom))}
.top{margin:0 0 14px}
.top h1{font-size:20px}
.hero,.two{grid-template-columns:1fr}
.metrics{grid-template-columns:repeat(3,minmax(0,1fr))}
table{display:block;overflow-x:auto;white-space:nowrap}
/* nav 挂在 <body> 下（不是 .wrap 里），本身已经是整屏宽；
   以前用 -14px 抵消 .wrap 的内边距，结果在 body 上反而多出 14px，
   整页能横向拖动一小截。 */
#gc-nav{margin:0 0 16px;padding:9px 14px;position:sticky;top:0;z-index:99;
  background:var(--bg);border-bottom:1px solid var(--line-brand);
  display:flex;gap:7px;align-items:center;flex-wrap:wrap;
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
#gc-nav a,#gc-nav span,#gc-nav button{font-size:12px;text-decoration:none;padding:5px 10px;
  border-radius:999px;background:var(--surface);border:1px solid var(--line-brand);
  color:var(--ink);font-family:inherit;cursor:pointer;white-space:nowrap}
#gc-nav a.back{border:0;background:none;color:var(--brand-ink);font-weight:600;padding-left:0}
#gc-nav button{background:var(--brand-soft);border-color:var(--line-brand);
  color:var(--brand-ink);font-weight:600}
#gc-nav span.off{opacity:.38}
#gc-nav input{font-size:12px;padding:5px 7px;border:1px solid var(--line-brand);
  border-radius:10px;background:var(--surface-2);color:var(--ink);font-family:inherit}
#gc-nav form{display:inline-flex;gap:5px;align-items:center;margin:0}
#gc-nav .tag{margin-left:auto;border:0;background:none;color:var(--ink-3)}
"""

# AI 页自带完整 CSS，只统一外框、卡片层次与标题节奏，不动它内部的交互。
AI_FIX = """
.wrap{max-width:820px;padding:0 14px calc(30px + env(safe-area-inset-bottom))}
.top{padding-top:16px;margin-bottom:12px}
.top h1{font-size:20px}
.card{border-radius:18px;border-color:var(--line-brand);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.035),
    0 14px 34px -22px rgba(0,0,0,.9)}
/* tabs 槽底比页面再深一点，选中项才浮得起来 */
.tabs{background:var(--surface-2);border:1px solid var(--line-brand)}
""" + BR.BRAND_CSS + """
/* AI 页自带 CSS，用 h1 的角标伪元素补上同一枚品牌图标，不改它的 DOM。 */
.top>div:first-child h1{display:flex;align-items:center;gap:9px}
.top>div:first-child h1::before{content:"";flex:none;width:26px;height:26px;
  border-radius:10px;
  background:url("__RC_MARK_URI__") center/80% no-repeat,
    linear-gradient(155deg,#16273D,#091320);
  border:1px solid var(--line-brand);
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
        if (act) {
          /* 有原生桥就拦掉 <a href> 的默认跳转：导航按钮既要唤起高德 App，
             不能同时又开一次网页。桌面浏览器没有 GC，保留 href 兜底。
             只拦链接本身，别把表单里按钮的 submit 一并取消掉。 */
          if (window.GC && GC.action) {
            if (n.getAttribute('href')) e.preventDefault();
            GC.action(act);
          }
          return;
        }
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
         screen: bool = False, css: str = "", head_bg: str = "") -> str:
    """所有 tab 页与二级页的统一外壳。

    `back` 只给二级页（/view、/files）用：一级 tab 之间靠底部导航切换，
    页内不再放返回链接，避免和底部 tab 形成两条并行导航。
    `action` 是页头右上角那个单动作按钮的 HTML（今日页放「分享/出图」）——
    低频但常用的动作放这儿，比在正文里占一整张卡片划算得多。
    `screen=True` 是 0.6.7 起「首屏即一页大屏」用的紧凑页头：去掉副标题、
    压低标题与留白，把视觉重心让给 main 里的 `.rc-hero`。
    `css` 是这一页独有的样式（远征页的衬线字体），追加在公共样式之后。
    `head_bg` 是塞进页头最底层的一段装饰 HTML（远征页的山脊剪影），绝对定位、
    不参与布局——这样不用为了背景图去改页头的结构。
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
<style>{CSS}{css}</style></head><body><div class="{app_cls}">
{back_html}
<header class="page-head{head_cls}">
  {head_bg}
  {action}
  <div class="brandline"><div class="kicker">{esc(kicker)}</div></div>
  {(f"<h1>{esc(title)}</h1>" if title else "")}{lede_html}
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
    """页头的一个信息胶囊。

    文字里第一个数字/日期自动加粗：胶囊里真正要读的是「297」「11-19」，
    标签只是说明它是什么。原来整条一样灰，扫一眼分不出哪个是数。
    """
    cls = f"chip {tone}" if tone else "chip"
    body = re.sub(r"(\d[\d\-/:.]*)", r"<b>\1</b>", esc(text), count=1)
    return f'<span class="{cls}">{body}</span>'


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
    <button type="submit">查看</button>
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
            f'<stop offset="0" stop-color="var(--brand)" stop-opacity=".42"/>'
            f'<stop offset="1" stop-color="var(--brand)" stop-opacity="0"/>'
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


def _upcoming_html(days: int = 30, limit: int = 3) -> str:
    """首页「赛事提醒」：未来 N 天里的时间点 + 该订的待办（最多 limit 条）。

    一条都没有就整块不出现，不留空盒子。提醒开关走表单（/__event/remind），
    不依赖 JS。预订待办的文案带「该订」并强制标热色，避免和「开赛」混在一起。
    """
    try:
        from . import event as E

        rows = E.upcoming(days=days)[:limit]
    except Exception:
        return ""
    if not rows:
        return ""
    out = []
    for r in rows:
        slug = r.get("slug") or ""
        try:
            on = bool((E.reminders(slug) or {}).get("%s|%s" % (r["at"], r["label"])))
        except Exception:
            on = False
        d = r.get("days") or 0
        is_bk = bool(r.get("booking"))
        # 预订待办只给日期：截止日当天 09:00 是我们自己定的提醒时刻，
        # 显示成「01/25 09:00」会被误读成航班/入住的时刻
        hh = "" if (is_bk or r["at"][11:16] == "00:00") else r["at"][11:16]
        stamp = r["at"][5:10].replace("-", "/") + (f" {hh}" if hh else "")
        label = r.get("label") or "时间点"
        event = str(r.get("event") or "")

        # 标题：赛事 · 动作。原来把「预订过期 7 天」放最前面当主语，读的人要
        # 先看到状态才知道是哪场比赛，语序和实际关心顺序是反的。
        title = f"{event} · {label}" if event and label else (event or label)

        if is_bk:
            if d < 0:
                sub = f"{stamp} · 已过期 {-d} 天，尽快处理"
                pill, tone = f"逾期 {-d} 天", "warn"
            elif d == 0:
                sub = f"{stamp} · 今天该订"
                pill, tone = "今天该订", "warn"
            else:
                sub = f"{stamp} · 提前 {d} 天订"
                pill, tone = f"剩 {d} 天", "run"
        else:
            phrase = "就是今天" if d == 0 else ("明天" if d == 1 else f"{d} 天后")
            sub = f"{stamp} · {phrase}"
            if d == 0:
                pill, tone = "今天", "brand"
            else:
                pill, tone = f"{d} 天后", ("warn" if d <= 2 else "run")

        out.append(
            '<div class="rc-up">'
            f'<div class="rc-up-t"><div class="tt">{esc(title)}</div>'
            f'<div class="ss">{esc(sub)}</div></div>'
            f'<div class="rc-up-r"><span class="pill {tone}">{esc(pill)}</span>'
            '<form method="post" action="/__event/remind" style="margin:0">'
            f'<input type="hidden" name="slug" value="{esc(slug)}">'
            f'<input type="hidden" name="at" value="{esc(r["at"])}">'
            f'<input type="hidden" name="label" value="{esc(label)}">'
            f'<input type="hidden" name="on" value="{"0" if on else "1"}">'
            f'<button class="rc-bell{" on" if on else ""}" type="submit" '
            f'aria-label="{"取消提醒" if on else "设为提醒"}">'
            f'{"🔔" if on else "🔕"}</button></form></div></div>')
    return ('<section class="rc-panel rc-up-panel">'
            '<div class="rc-panel-hd"><span class="rc-eyebrow">赛事提醒</span></div>'
            + "".join(out) + "</section>")


def _reading_card(limit: int = 8) -> str:
    """首页最底部的「延伸阅读」：内容一行、换行、破折号接数据源。

    多条轮播（每 6 秒换一条），靠一段内联 JS 切 class，不发请求、不重排。
    内容取不到就整块不出现——首页不能因为一块可选内容变空。
    """
    try:
        from . import reading as RD

        items = RD.items(limit)
    except Exception:
        return ""
    if not items:
        return ""
    def _line(it: dict) -> str:
        # 术语的正文就是一句解释，直接用；赛事光有「报名剩 5 天 · 12月06日」
        # 不知道是哪场比赛，必须把名称带到前面。
        if it.get("kind") == "race":
            return f'{it.get("head") or ""} · {it.get("text") or ""}'.strip(" ·")
        return it.get("text") or ""

    slides = "".join(
        f'<div class="rdi{" on" if i == 0 else ""}">'
        f'<p class="rdt">{esc(_line(it))}</p>'
        f'<p class="rds">—— {esc(it["src"])}</p></div>'
        for i, it in enumerate(items))
    return ('<section class="rc-panel rc-rd-panel" id="rc-read">'
            '<div class="rc-panel-hd"><span class="rc-eyebrow">延伸阅读</span>'
            '</div>'
            f'<div class="rc-rd">{slides}</div></section>'
            '<script>(function(){var b=document.getElementById("rc-read");'
            'if(!b)return;var s=b.querySelectorAll(".rdi");'
            'if(s.length<2)return;var i=0;'
            'setInterval(function(){s[i].classList.remove("on");'
            'i=(i+1)%s.length;s[i].classList.add("on");},6000);})();</script>')


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

    # ---- L1 结论：并进 hero 当标题 -----------------------------------------
    # 原来页首还有一条独立的「结论条」，和 hero 说的是同一句话，等于把
    # 同一个结论写两遍；参考稿是「一句结论 + 一段解释 + 环」同处一卡。
    delta = _baseline_delta(hist, score)
    if delta is None:
        meta = "记录不足，还没有 28 天基准"
    else:
        meta = f"比近 28 天基准{'低' if delta < 0 else '高'} {abs(delta)}"

    # ---- 恢复 hero ≈176：一句结论 + 一句解释 + 环 + 3 条分解 + 建议 ---
    # 常态不发光；只有 <50 / >90 的异常日才点亮，辉光才有意义。
    alert = score is not None and (float(score) < 50 or float(score) > 90)
    try:
        _sc0 = float(score) if score is not None else None
    except (TypeError, ValueError):
        _sc0 = None
    _tone = ("bad" if (_sc0 is not None and _sc0 < 50)
             else "warn" if (_sc0 is not None and _sc0 < 70) else "ok")
    para = (f"恢复度 {_sc0:.0f}，{meta}。" if _sc0 is not None else meta + "。")

    # 建议从一整句改成几个胶囊：一句话里塞「St24×2 · 优先补觉」时，
    # 谁都会把它当成一段说明文读过去，而不是三个可执行的动作。
    _pills: list[str] = [_session(res)]
    for _f in flags:
        _t = str(_f.get("text") or "")
        if "，" in _t:
            _t = _t.split("，", 1)[1]
        _t = _t.strip("。 ")
        if _t and len(_t) <= 8 and _t not in _pills:
            _pills.append(_t)
    advice = ('<div class="rc-advice"><b>今日建议</b>' + "".join(
        f'<span class="pill ghost">{esc(x)}</span>' for x in _pills[:3]) + "</div>")
    hero = (f'<section class="rc-panel rc-hero-panel">'
            f'<div class="rc-hero"><div class="rc-hero-main">'
            f'<div class="rc-hero-t"><i class="rc-dot {_tone}"></i>'
            f'{esc(_verdict(score))}</div>'
            f'<p class="rc-hero-p">{esc(para)}</p></div>'
            f'{SC.ring_svg(score, alert=alert)}</div>'
            f'<div class="rc-parts">{SC.part_rows(rd.get("parts") or [], 3)}</div>'
            f'{advice}</section>')

    # ---- 负荷带 60：三个数并列，不是三张卡；每个数都带窗口 ----------------
    acute, ctl, tsb = load.get("acute_7d"), load.get("ctl"), load.get("tsb")
    items = (
        ("7 天负荷", f"{acute:.0f}" if isinstance(acute, (int, float)) else "—",
         "心率估算 TRIMP"),
        ("长期负荷", f"{ctl:.1f}" if isinstance(ctl, (int, float)) else "—",
         "42 天均值"),
        ("训练状态", f"{tsb:+.1f}" if isinstance(tsb, (int, float)) else "—",
         "长期减短期"),
    )
    load_html = ('<section class="rc-load rc-load-panel">'
                 '<div class="rc-load-hd"><span class="rc-eyebrow">训练负荷</span>'
                 '<span class="rc-win">近 7 天 / 42 天</span></div>' + "".join(
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
        _sleep_sub.append(f"近 7 天少睡 {_debt:.1f} 小时")
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
        _wsub.append(f"7 天平均 {body['avg7']:.1f}")
    _dw = body.get("delta_w")
    if _dw is not None and abs(_dw) >= 0.05:
        _wsub.append(f"较前 7 天 {_dw:+.1f} kg")
    cells = (
        SC.body_cell(f"睡眠 · {_dd}", f"{sleep['total_h']:.1f}" if sleep.get("total_h") else None,
                     "h", " · ".join(_sleep_sub) or "暂无 7 天平均值",
                     _spark((series.get("sleep_h") or [])[-14:], h=22)),
        SC.body_cell(f"HRV · {_dd}", f"{_hrv:.0f}" if _hrv else None, "ms",
                     f"28 天基线 {hrv_base:.0f} · {_wk('hrv')}" if hrv_base else _wk("hrv"),
                     _spark((series.get("hrv") or [])[-14:], h=22)),
        SC.body_cell(f"静息心率 · {_dd}", f"{_rhr:.0f}" if _rhr else None, "bpm",
                     f"28 天基线 {rhr_base:.0f} · {_wk('rhr')}" if rhr_base else _wk("rhr"),
                     _spark((series.get("rhr") or [])[-14:], h=22)),
        # 体重刻意给完整一行：不再 nowrap + ellipsis 把「较…」切掉。
        SC.body_cell("体重 · 近 7 天", f"{_w:.2f}" if _w else None, "kg",
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
    when, subwhen, title, desc = "—", "", "按恢复度安排", "本周暂无训练安排"
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
    # 右上角原来写死「按日课表」，重复说明了这一块是什么；换成「本周 3 练」
    # 这种能一眼判断的信息，体积不变、信息量更大。
    _wp, _wd = int(pl.get("week_planned") or 0), int(pl.get("week_done") or 0)
    win_txt = (f"本周已完成 {_wd}/{_wp} 次" if _wp else "本周暂无训练安排")
    # 整块可点：原来只有右上角「课表 →」那个小链接能点，手机上根本点不准。
    # 现在整张卡就是一个链接，右侧留个 › 作为「可以进去」的暗示。
    plan_html = (f'<a class="rc-panel rc-plan-panel rc-plan-link" href="/plan?from=today">'
                 f'<div class="rc-panel-hd"><span class="rc-eyebrow">{esc(eyebrow)}</span>'
                 f'<span class="rc-win">{esc(win_txt)}</span></div>'
                 f'<div class="rc-plan"><div class="when"><b>{esc(when)}</b>'
                 f'{esc(subwhen)}</div><div class="what"><div class="t">'
                 f'{esc(title)}</div>{desc_html}</div>'
                 f'<span class="chev" aria-hidden="true">›</span>'
                 f'</div></a>')

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
        _ld = a.get("training_load") or a.get("load")
        load_txt = f"负荷 {int(_ld)}" if _ld else ""
        samples += (f'<div class="rc-sample" data-aid="{esc(aid)}" role="button" '
                    f'tabindex="0" aria-label="{esc(shown)}">'
                    f'<i class="ico">{_act_icon(str(a.get("type") or ""))}</i>'
                    f'<div class="m"><div class="t">{esc(shown)}</div>'
                    f'<div class="d">{esc(_act_metric(a, want_load=False))}</div></div>'
                    f'<div class="r">{esc(load_txt)}</div></div>')
    if not samples:
        samples = ('<div class="rc-sample"><span class="t">最近 7 天还没有活动记录。'
                   '如果设备上有记录，可以下拉同步。</span></div>')
    week_html = (f'<section class="rc-panel rc-week-panel">'
                 f'<div class="rc-panel-hd"><span class="rc-eyebrow">最近活动</span>'
                 f'<span class="rc-win">最近 7 天</span></div>'
                 f'<div class="rc-week"><div class="rc-week-hd">'
                 f'<div class="big-num rc-num">{km7:.1f}'
                 f'<small>km · 最近 7 天</small></div>'
                 f'<div class="side">{esc(side)}</div></div>'
                 f'<div class="rc-days">{SC.week_bars(pairs, day)}</div>'
                 f'<div class="rc-samples">{samples}</div></div></section>')

    body_html_all = ('<div class="rc-screen">' + hero + load_html
                     + body_html + plan_html + _upcoming_html()
                     + week_html + _reading_card() + '</div>')
    # 只留两个数据胶囊：宽屏自动铺成大屏，不再需要第三个按钮。
    # 一图流也不再挂页头：那枚胶囊在横屏大屏里孤零零地压着结论条，
    # 0.6.13 起统一收进「我的」页的低频动作列表。
    # 数据停在旧日期时，把「上次同步」胶囊换成可执行的提示。胶囊数不变，
    # 不会碰到横屏 .rc-screen 的命名 grid 区域。
    _sync_chip = (f"数据截至 {_dd} · 下拉同步" if _stale else
                  f"上次同步 {esc(str(st.get('last_sync') or '—')[5:16])}")
    chips = (chip(f"{st.get('days') or 0} 天数据") + chip(_sync_chip))
    return page("TODAY · 今日", "今天该怎么练", "", body_html_all, chips,
                screen=True,
                foot=("数据在本机计算，离线也能查看，轨迹不会上传。"
                      "静息心率来自每天同步的记录，可能和设备里设置心率区间时用的数值不同。"))


def _verdict(score) -> str:
    """结论条的一句话判断：先给答案，再给数字。"""
    if score is None:
        return "同步设备后查看训练建议"
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "同步设备后查看训练建议"
    if s >= 85:
        return "今天可以按计划练"
    if s >= 70:
        return "可以练，别加量"
    if s >= 55:
        return "今天轻松活动就好"
    return "今天先休息"


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


def _act_metric(a: dict, want_load: bool = True) -> str:
    """活动行的一行摘要。按类型给不同指标 —— 力量课写「0.00 km · 配速 —」
    会被读成「跑了 0 公里」，明明是没跑。

    `want_load=False` 时不带「负荷 N」：负荷挪到行尾单独一列，
    和参考稿「左边数据、右边负荷」的分栏一致。
    """
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
        if te and want_load:
            bits.append(f"负荷 {int(te)}")
    return " · ".join(bits) or "已记录"


# 活动类型 → 行首的小图标。用内联 SVG 而不是 emoji：emoji 在 WebView 里
# 是彩色位图，一列下来五颜六色，和「只有数据和状态着色」的规矩打架。
_ACT_ICONS = {
    "run": ('<circle cx="9.6" cy="3.6" r="1.5"/>'
            '<path d="M9.2 5.6L7.4 8.8"/>'
            '<path d="M7.4 8.8L5.4 12.4"/><path d="M7.4 8.8L10 12.2"/>'
            '<path d="M5.8 6.6l3.6 1.2"/>'),
    "bike": ('<circle cx="3.9" cy="11.2" r="2.5"/><circle cx="12.1" cy="11.2" r="2.5"/>'
             '<path d="M3.9 11.2l2.4-5.4h3.1l2.7 5.4"/><path d="M6.3 5.8l1.8 5.4"/>'),
    "strength": ('<path d="M4.3 6v4"/><path d="M5.9 4.9v6.2"/><path d="M10.1 4.9v6.2"/>'
                 '<path d="M11.7 6v4"/><path d="M5.9 8h4.2"/>'),
    "swim": ('<path d="M2.2 10.4c1.4-1.5 2.8-1.5 4.2 0s2.8 1.5 4.2 0 2.8-1.5 4.2 0"/>'
             '<path d="M2.2 13.2c1.4-1.5 2.8-1.5 4.2 0s2.8 1.5 4.2 0 2.8-1.5 4.2 0"/>'
             '<circle cx="10.6" cy="4.4" r="1.3"/>'),
    "walk": ('<path d="M8.6 5.6l-1 3.4 1.8 1.6"/>'
             '<path d="M7.6 9l-1.2 3.4"/><path d="M9.4 10.6l1.2 2.4"/>'
             '<circle cx="8.8" cy="3.4" r="1.4"/>'),
}


def _act_icon(kind: str) -> str:
    """按活动类型取行首图标。未知类型给一个中性圆点，不留空白方块。"""
    k = str(kind or "").lower()
    if "bik" in k or "cycl" in k:
        key = "bike"
    elif "swim" in k:
        key = "swim"
    elif "strength" in k or "weight" in k or "gym" in k:
        key = "strength"
    elif "walk" in k or "hik" in k:
        key = "walk"
    elif "run" in k:
        key = "run"
    else:
        key = ""
    if not key:
        return ('<svg viewBox="0 0 16 16" width="16" height="16" fill="none" '
                'stroke="currentColor" stroke-width="1.5" aria-hidden="true">'
                '<circle cx="8" cy="8" r="4.2"/></svg>')
    return ('<svg viewBox="0 0 16 16" width="16" height="16" fill="none" '
            'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" '
            'stroke-linejoin="round" aria-hidden="true">'
            + _ACT_ICONS[key] + "</svg>")


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
    _why = str(pl.get("note") or "").strip() or "暂未同步到平台课表"
    local_tag = (f'<div class="n" style="color:var(--amber)">本地训练安排 · {esc(_why)}</div>'
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
        # 本周完成度：一排小段（参考稿的做法）。超过 12 次就不画，
        # 十几段挤在 380px 里每段只剩几像素，反而看不出完成了几次。
        _wt = int(pl.get("week_planned") or 0)
        _wd = int(pl.get("week_done") or 0)
        wseg = ""
        if 0 < _wt <= 12:
            wseg = ('<div class="wseg" aria-label="本周已完成 '
                    f'{_wd} / {_wt} 次">' + "".join(
                        f'<i class="{"on" if i < _wd else ""}"></i>'
                        for i in range(_wt)) + "</div>")
        return (f'<div class="plan-hero"><div class="t">{esc(_plan_name(it.get("name")))}</div>'
                + (f'<div class="d">{esc(it.get("detail"))}</div>' if it.get("detail") else "")
                + f'<div class="n">{esc(_plan_meta(it))}</div>{wseg}{badge}{more}{local_tag}</div>')

    nxt = pl.get("next_date")
    if nxt:
        tip = f'下一次：{esc(nxt)} · {esc(_plan_items_line(pl.get("next_items") or []))}'
    elif t.get("note"):
        # 本地兜底给的「为什么休息」比「本周没有排课」有用得多
        tip = esc(t["note"])
    else:
        tip = "本周还没排课。想活动的话，按今天的状态轻松练一练。"

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
        done_html = '<div class="n">今天的活动已同步，可在历史页查看。</div>'

    return (f'<div class="plan-hero"><div class="t">今天休息</div>'
            f'<div class="d">{tip}</div>{done_html}{local_tag}</div>')


def plan_page(qs: dict | None = None) -> str:
    """训练计划二级页（属「今日」tab）：今日课表 + 本周 7 天 + 计划信息。

    数据来自佳明计划快照或本地教练建议；本地生成内容可预览后导入高驰。
    """
    from . import appapi as A

    r = A.plan(False)
    if not r.get("ok"):
        body = ('<div class="card"><div class="empty"><b>还没读到训练计划</b>'
                f'{esc(r.get("error") or "先回今日页下拉同步一次")}</div>'
                '<div class="pager"><div class="btn primary" data-act="planRefresh">'
                '重新读取</div></div></div>')
        return page("PLAN · 训练计划", "今天练什么", "查看已同步的课表和本机训练建议",
                    sec("状态", body), chip("本机记录"), back=("返回今日", "/"))

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
        _dd = str(d.get("date") or "")[5:].replace("-", "/")
        _is_future = str(d.get("date") or "") > str(today_iso or "")
        if is_today:
            tag = '<span class="pill brand">今天</span>'
        elif d.get("done"):
            tag = '<span class="pill ok">已完成</span>'
        elif d.get("missed"):
            # 计划了但没做：以前这种日子被直接标成「已完成」藏起来了
            tag = '<span class="pill bad">未完成</span>'
        elif d.get("extra"):
            # 没排课但练过：不叫「完成」，只如实说这是计划外的
            tag = '<span class="pill mute">计划外</span>'
        elif its and _is_future:
            # 还没到的课：说清「等着你」，否则一片空白分不出是没排还是没做
            tag = '<span class="pill run">待完成</span>'
        else:
            tag = ""
        # 日期列「周四 / 11-20」两行 + 一道竖向发丝线：只写周几时一眼看不出
        # 是哪天，而日期和课名之间没有分隔时，两列会读成一句话。
        rows += (f'<div class="{cls}"><span class="wd">{esc(d["dow"])}'
                 + (f'<i>{esc(_dd)}</i>' if _dd else "")
                 + f'</span>{wb}{tag}</div>')

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
    steps_section = sec("骑行训练步骤", '<div class="card">' + steps_html + '</div>') if steps_html else ""
    # 只有连了高驰才显示这个入口：佳明用户点进去只会看到「请先授权高驰」
    import_link = ('<div class="pager"><a class="btn" href="/training">'
                   '导入本地训练到高驰</a></div>'
                   if A.data_source().get("coros") else "")
    # 编辑入口放在「本周」标题右侧，而不是只留在页脚：计划页一进来最想改的就是
    # 这一周，让人滚到底才能编辑，等于没有入口。
    body = (sec("今日", _today_plan_card(r)) + steps_section + import_link +
            sec("本周", f'<div class="wk">{rows}</div>', more=("编辑课表", "/plan/edit")) +
            next_sec +
            sec("计划信息", info) +
            '<div class="pager"><a class="btn primary" href="/plan/edit">编辑本周课表</a>'
            '<a class="btn" href="/plan/draft">查看草稿</a>'
            '<div class="btn" data-act="planRefresh">刷新计划</div></div>')
    return page("PLAN · 训练计划", "今天练什么",
                (r.get("name") or "平台课表"), body, chips,
                back=("返回今日", "/"))


def _num_input(name: str, value, *, step: str = "0.5", lo=0, hi=60,
               disabled: bool = False, ph: str = "") -> str:
    v = "" if value in (None, 0, 0.0, "") else f"{float(value):g}"
    dis = " disabled" if disabled else ""
    return (f'<input type="number" inputmode="decimal" name="{esc(name)}"'
            f' step="{step}" min="{lo}" max="{hi}"'
            f' value="{esc(v)}" placeholder="{esc(ph)}"{dis}>')


def _plan_push_block(token: str, src: dict, today: str) -> str:
    """按「连了哪个平台」渲染推送入口。

    不同用户的数据源不一样：只连高驰的人看到「推到佳明手表」会莫名其妙，
    只连佳明的人也根本用不上高驰那套导入。所以入口由 `data_source()` 决定，
    模板里不写死任何一家。

    两家都连的人会看到两个按钮——这不是多余，双修的人确实两边都要推。
    """
    blocks = []
    if src.get("garmin"):
        blocks.append(
            '<form action="/__plan/push" method="post">'
            f'<input type="hidden" name="token" value="{esc(token)}">'
            '<div class="pager"><button class="btn" type="submit">推到佳明手表</button></div>'
            '<p class="note">本周的训练会排进佳明 Connect 日历，佳明教练原有的计划会保留。'
            '再次推送时，会替换上次从这里发出的课程。</p></form>')
    if src.get("coros"):
        href = f'/training?source_date={today}&date={today}'
        tip = ("先预览课程，确认后再导入。高驰里已有的课程会保留。"
               if src.get("coros_import") else
               "第一次导入时，按页面提示登录高驰并授权。")
        blocks.append(
            f'<div class="pager"><a class="btn" href="{esc(href)}">导入到高驰</a></div>'
            f'<p class="note">{esc(tip)}</p>')
    if not blocks:
        blocks.append(
            '<div class="pager"><a class="btn" href="/me">去「我的」连接账号</a></div>'
            '<p class="note">连接佳明或高驰后，就能从这里发送训练。</p>')
    return "".join(blocks)


def plan_edit_page(token: str, message: str = "", data: dict | None = None,
                   rules: dict | None = None, src: dict | None = None) -> str:
    """计划编辑页：一周 7 天逐块改，改完保存。

    只写 `plan_overrides.json`，不动日历快照——下次同步不会把这里改的东西冲掉。
    过去的日子标灰且不提交：改历史会让完成率凭空变化，也没有任何用处。

    一天有多条课次时这一块只读并提示去日历改：这个页面只处理「一天一条」，
    硬压成一条会把另一条悄悄删掉。

    `data` / `rules` 可以外部传入（自检用），不传就现读。
    """
    from . import appapi as A, plangen as PG
    import datetime as _dt

    r = data if isinstance(data, dict) else A.plan_week()
    rules = rules if isinstance(rules, dict) else PG.rules()
    today = _dt.date.today().isoformat()
    if not r.get("ok"):
        body = ('<div class="card"><div class="empty"><b>暂时打不开本周课表</b>'
                f'{esc(r.get("error") or "先回今日页下拉同步一次")}</div>'
                '<div class="pager"><div class="btn primary" data-act="planRefresh">'
                '重新读取</div></div></div>')
        return page("PLAN · 编辑计划", "编辑本周课表", "暂未加载课表",
                    body, back=("返回计划", "/plan"))

    rows, multi_days, past_days = "", 0, 0
    for i, d in enumerate(r.get("days") or []):
        its = d.get("items") or []
        first = its[0] if its else None
        kind = (first or {}).get("kind") or ("rest" if not its else "easy")
        multi = len(its) > 1
        past = bool(d.get("past"))
        if multi:
            multi_days += 1
        if past:
            past_days += 1

        cls = ""
        if past:
            cls += " past"
        if d["date"] == today:
            cls += " cur"

        # 不改的日子（多条 / 已过去）**整行不提交**：多条怕覆盖掉另一条，
        # 过去的日子改了会让完成率凭空变化。只留一行说明。
        if multi or past:
            why = (f'当天有 {len(its)} 节训练，请在平台日历里修改' if multi else '日期已过')
            rows += (f'<div class="{cls}">'
                     f'<input type="hidden" name="d{i}_skip" value="1">'
                     f'<div class="p1"><b>{esc(d["dow"])}<span>{esc(d["date"][5:])}</span></b></div>'
                     f'<div class="pinfo">{esc(why)}</div></div>')
            continue

        opts = ""
        for key, label in (("rest", "休息"), ("easy", "轻松跑"), ("tempo", "节奏跑"),
                           ("long", "长距离跑"), ("interval", "间歇跑"),
                           ("strength", "力量训练"), ("race", "比赛")):
            sel = " selected" if key == kind else ""
            opts += f'<option value="{key}"{sel}>{label}</option>'

        # 数值框的语义跟着类型走：力量课填分钟，其余填里程。只留一个输入框，
        # 两个并排在手机上必然挤成一团。
        if kind == "strength":
            num = _num_input(f"d{i}_minutes", (first or {}).get("minutes"),
                             step="5", lo=0, hi=600, ph="0")
            unit = "分钟"
        else:
            num = _num_input(f"d{i}_km", (first or {}).get("km"), ph="0")
            unit = "km"

        rows += (f'<div class="{cls}">'
                 f'<input type="hidden" name="d{i}_date" value="{esc(d["date"])}">'
                 f'<div class="p1"><b>{esc(d["dow"])}<span>{esc(d["date"][5:])}</span></b>'
                 f'<select name="d{i}_kind">{opts}</select></div>'
                 f'<div class="p2">{num}<span class="unit">{unit}</span></div></div>')

    chips = (chip(f'本周 {r.get("week_km") or 0:g} km', "ok") +
             chip(f'{r.get("monday")} 起') +
             (chip("本机课表已修改") if r.get("edited") else "") +
             (chip("课表需要刷新", "") if r.get("stale") else ""))
    msg = f'<p class="note">{esc(message)}</p>' if message else ""
    stale_tip = ('<p class="note">现在显示的是之前的课表。先返回计划页点「刷新计划」，'
                 '或在今日页下拉同步，再回来编辑。</p>' if r.get("stale") else "")

    save_form = (f'<form action="/__plan/save" method="post">'
                 f'<input type="hidden" name="token" value="{esc(token)}">'
                 + sec("本周课表", '<div class="card">' + rows + '</div>')
                 + '<p class="note">按训练类型填写里程或时长。休息日选「休息」即可。'
                   '保存后会立即更新本机课表。</p>'
                 + '<div class="pager"><button class="btn primary" type="submit">保存本周课表</button>'
                   '<a class="btn" href="/plan">取消</a></div></form>')

    gen_form = (f'<form action="/__plan/generate" method="post">'
                f'<input type="hidden" name="token" value="{esc(token)}">'
                + '<div class="card">'
                '<div class="kvline"><span>每周跑几天</span><b>'
                '<input type="number" inputmode="numeric" name="run_days" min="2" max="6"'
                ' step="1" value="" placeholder="参考最近 4 周"></b></div>'
                '<div class="kvline"><span>每周跑量（km）</span><b>'
                '<input type="number" inputmode="decimal" name="week_km" min="0" max="200"'
                ' step="1" value="" placeholder="参考最近 4 周"></b></div>'
                '<div class="kvline"><span>赛前减量</span><b>'
                f'<label class="tsw"><input type="checkbox" name="race_deload" value="1"'
                f'{" checked" if rules["race_deload"] else ""}><i></i></label>'
                '</b></div></div>'
                '<p class="note">留空会参考最近 4 周的跑步记录。开关只控制赛前是否减量，'
                '每 4 周安排的减量照常。</p>'
                '<div class="pager"><button class="btn" type="submit">生成计划草稿</button></div></form>')

    src = src if isinstance(src, dict) else A.data_source()
    push_form = _plan_push_block(token, src, today)

    clear_form = (f'<form action="/__plan/clear" method="post">'
                  f'<input type="hidden" name="token" value="{esc(token)}">'
                  '<div class="pager"><button class="btn" type="submit">撤销所有本地改动</button></div>'
                  '<p class="note">撤销后回到日历/教练计划的原始课表。</p></form>')

    # 区块标题跟着数据源走：只连高驰的人看不到「佳明」两个字
    _push_title = ("推送到手表" if src.get("garmin") else
                   "导出到手表" if src.get("coros") else "推送到手表")
    body = msg + stale_tip + save_form + sec("根据跑步记录生成计划",
                                             '<div class="card">' + gen_form + '</div>') \
        + sec(_push_title, '<div class="card">' + push_form + '</div>') \
        + sec("恢复原课表", '<div class="card">' + clear_form + '</div>') \
        + (f'<p class="note">本周有 {multi_days} 天安排了多节训练，{past_days} 天已过去。 '
           '这些日期在此页仅供查看。</p>' if (multi_days or past_days) else "")
    return page("PLAN · 编辑计划", "编辑本周课表", f'{r.get("monday")} 起这一周',
                body, chips, back=("返回计划", "/plan"))


def plan_draft_page(token: str, message: str = "", draft: dict | None = None) -> str:
    """草稿预览：生成的计划先看再决定，确认之前一个字都不落盘。"""
    from . import plangen as PG

    d = draft if isinstance(draft, dict) else PG.load_draft()
    if not d or not d.get("week"):
        body = ('<div class="card"><div class="empty"><b>没有待确认的草稿</b>'
                '可以根据最近的跑步记录生成一份训练计划。</div>'
                '<div class="pager"><a class="btn primary" href="/plan/edit">前往生成计划</a></div></div>')
        return page("PLAN · 草稿", "训练计划草稿", "还没有草稿",
                    body, back=("返回计划", "/plan"))

    rows = ""
    for w in d.get("week") or []:
        its = w.get("items") or []
        if not its:
            # 日期列「周四 / 11-20」两行；只写周几看不出是哪天
            _rd = str(w.get("date") or "")[5:].replace("-", "/")
            rows += (f'<div class="wrow rest"><span class="wd">{esc(w.get("dow"))}'
                     + (f"<i>{esc(_rd)}</i>" if _rd else "")
                     + '</span><span class="wb">休息</span></div>')
            continue
        it = its[0]
        meta = " · ".join(x for x in (
            f'{it.get("km") or 0:g} km' if it.get("km") else "",
            f'{it.get("minutes")}′' if it.get("minutes") else "",
            it.get("detail") or "") if x)
        _wd = str(w.get("date") or "")[5:].replace("-", "/")
        rows += (f'<div class="wrow dwk"><span class="wd">{esc(w.get("dow"))}'
                 + (f"<i>{esc(_wd)}</i>" if _wd else "")
                 + f'</span><span class="wb">{esc(it.get("name") or "训练")}'
                 f'<em>{esc(meta)}</em></span></div>')

    reasons = "".join(
        f'<div class="whyrow"><span class="wno">{i + 1}</span>'
        f'<span class="wtxt">{esc(x)}</span></div>'
        for i, x in enumerate(d.get("reason") or []))
    chips = (chip(f'{d.get("week_km") or 0:g} km', "ok") +
             chip(f'{d.get("days") or 0} 天') +
             chip(f'{d.get("monday")} 起') +
             (chip(f'目标跑量 ×{d.get("factor"):g}') if d.get("factor") not in (None, 1) else ""))
    msg = f'<p class="note">{esc(message)}</p>' if message else ""

    # 三个动作放进同一个表单，用 formaction 区分：两个 form 并排会让按钮和
    # 勾选框挤成两行，手机上很难看清哪个按钮对应哪个动作。
    ok_form = (f'<form action="/__plan/confirm" method="post">'
               f'<input type="hidden" name="token" value="{esc(token)}">'
               '<div class="pager">'
               '<button class="btn primary" type="submit">用这份计划</button>'
               '<a class="btn" href="/plan/edit">调整目标</a>'
               '<button class="btn" type="submit" formaction="/__plan/discard">放弃草稿</button>'
               '</div>'
               '<label class="override"><input type="checkbox" name="override" value="1">'
               '替换手动安排的课程和休息日</label></form>')
    body = (msg
            + sec("为什么这么排", '<div class="card">' + reasons + '</div>')
            + sec("这一周", f'<div class="wk">{rows}</div>')
            + ok_form
            + '<p class="note">确认后，草稿会加入本机课表，之后会自动安排下一周的训练。'
              '手动安排的课程和休息日默认保留。</p>')
    return page("PLAN · 草稿", "训练计划草稿", "先看看这一周怎么练，再决定是否采用。",
                body, chips, back=("返回计划", "/plan"))


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
        for field, label, lo, hi in (("max_hr", "最大心率（次/分）", 80, 240), ("lthr", "阈值心率（次/分）", 60, 230), ("ftp_w", "功能阈值功率（FTP，瓦）", 30, 1000)):
            if key == "run" and field == "ftp_w":
                continue
            inputs.append(f'<label style="display:block;margin:12px 0">{label} '
                          f'<input type="number" name="{key}_{field}" min="{lo}" max="{hi}" step="1" value="{esc(p.get(field) or "")}" placeholder="未设置"></label>')
        names = ','.join(p.get("hr_zone_names") or [])
        if key == "bike":
            inputs.append('<label style="display:block;margin:12px 0">骑行课表类型 <select name="bike_workout">' + ''.join(
                f'<option value="{v}"{" selected" if p.get("workout", "endurance") == v else ""}>{label}</option>'
                for v, label in (("endurance", "有氧耐力骑行"), ("recovery", "轻松恢复骑行"), ("intervals", "功率间歇"))) + '</select></label>')
        inputs.append(f'<label style="display:block">心率区间名称（Z1–Z5） <input name="{key}_hr_zone_names" value="{esc(names)}" placeholder="热身,轻松,有氧,阈值,极限" style="width:100%;margin-top:8px"></label>')
        fields.append(sec(name, '<div class="card">' + ''.join(inputs) + '</div>'))
    body = (f'<p class="note">{esc(message)}</p>' if message else '')
    body += (f'<form action="/__sports" method="post"><input type="hidden" name="token" value="{esc(token)}">'
             + sec("训练偏好", '<div class="card">' + select + '</div>') + ''.join(fields)
             + '<div class="note">跑步与骑行共用睡眠、心率变异性（HRV）和体重数据。App 用各项目的最大心率估算训练负荷，骑行课表会参考阈值心率。'
               '各心率区间的时长以设备记录为准。设置 FTP 后，App 会按当前数值计算历史骑行的强度系数（IF）和训练压力分数（TSS）。'
               '每日汇总中的最大摄氧量（VO₂max）目前只有一个数值，供一般参考。</div>'
               '<div class="pager"><button class="btn primary" type="submit">保存运动设置</button></div></form>')
    return page("SPORTS · 运动设置", "运动设置", "可以分别设置跑步和骑行的参数", body, back=("返回我的", "/me"))


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
             + (" · 可查看或更换账号" if c_ok == "已登录" else " · 登录后可同步活动"))
    # 小米/华为只检查本地凭据文件在不在，刻意不联网验证。写「已登录」会被
    # 读成「凭据现在还有效」，于是同步报 401 时看着像 App 自相矛盾。
    m_ok = "已保存登录信息" if m.get("logged_in") else "未连接"
    h_ok = "已保存登录信息" if h.get("logged_in") else "未连接"
    try:
        k_ok = "已登录" if A.keep_status().get("logged_in") else "未连接"
    except Exception:
        k_ok = "未连接"
    # Keep 只有运动摘要（没有轨迹/分圈），文案里先说清楚，免得用户以为漏了图
    k_sub = (f"{k_ok} · 仅同步运动摘要（无轨迹）"
             + (" · 可查看或更换账号" if k_ok == "已登录" else ""))
    bs = A.body_sources()
    pref = bs.get("preferred", "mi")
    pref_label = bs.get("labels", {}).get(pref, pref)
    # 页头只留一个「本机数据」：版本号在「关于与诊断」里已经写着，
    # 三个数字在「本地数据」那一行也说了。同一份信息写两遍，读者会以为
    # 上面那个和下面那个不一样——这是把页面读长最省力的方式。
    chips = chip("数据只在本机", "ok")

    # AI 模型服务：和数据源并列——这两类都在回答「这台机器现在连着什么服务」。
    # 副标题要能一眼看出「开没开 / 用哪个模型 / 打到哪个域名」，
    # 具体的 Key 与供应商仍然点进去改（抽屉在教练页，这里用 aiSettings 直接拉起来）。
    try:
        from . import aiapi as AIAPI

        ai = AIAPI.dispatch("config.get", {}) or {}
    except Exception:
        ai = {}
    ai_on = bool(ai.get("ai_enabled", True))
    try:
        _simple = A.simple_mode()
    except Exception:
        _simple = False
    if not ai_on:
        ai_sub = "已关闭 · 点击设置"
    elif not ai.get("configured"):
        ai_sub = "未设置 · 点击填写 API 密钥"
    else:
        _base = str(ai.get("base_url") or "")
        _host = _base.split("//")[-1].split("/")[0] if _base else "本地"
        ai_sub = (("平台额度" if ai.get("mode") == "platform" else "自己的 API 密钥")
                  + f" · {ai.get('model') or '未指定模型'} · {_host}")

    # 账号与数据源：只有这一组继续用竖排行——每行都要说清「登没登、设备是谁」，
    # 网格卡片塞不下这些信息。已登录点进去是账号卡片（查看/更换/退出），
    # 没登录才进登录框。
    try:
        from . import account as ACC

        _acc = ACC.state()
    except Exception:  # noqa: BLE001 - 配置坏了不该让整页打不开
        _acc = {}
    _grp_desc = ("建组 / 加入 / 本周榜" if _acc.get("logged_in")
                 else "未登录 · 先登录")
    _acc_desc = ((_acc.get("label") or "已登录") if _acc.get("logged_in")
                 else "未登录 · 去登录")

    sources = (row("G", "佳明 Garmin",
                   f"{g} · {esc(_mask_email(st.get('email')) or '未设置账号')}"
                   + (" · 可查看或更换账号" if g == "已登录" else ""),
                   act="login:garmin") +
               row("C", "高驰 COROS", c_sub,
                   act="login:coros", tone="b") +
               row("K", "Keep", k_sub, act="login:keep", tone="b") +
               # 三个体重行并成一行：小米/华为的登录放进来源选择器的「登录账号」，
               # 少两行，语义反而更清楚（都在说「体重数据从哪来」）。
               row("W", "体重数据",
                   f"小米 {m_ok} · 华为 {h_ok} · 当前来源 {esc(pref_label)}",
                   act="bodySource", tone="a") +
               # 开关自己带 data-act，点它不会顺带打开设置；点行的其它地方才进配置
               row("◍", "AI 模型服务", ai_sub, act="aiSettings", tone="b", go=False,
                   switch=("on" if ai_on else "off"), sw_act="aiToggle") +
               # 简洁模式：把「先不用」的模块整个藏起来，而不是灰掉。
               # 灰按钮只会让人去点它试试，然后发现没什么用。
               row("◐", "简洁模式",
                   ("已开启 · 教练页仅显示今日建议和体重" if _simple
                    else "已关闭 · 教练页显示全部功能"),
                   act="simpleToggle", tone="a", go=False,
                   switch=("on" if _simple else "off"), sw_act="simpleToggle"))

    # 「补全历史」已经完全没有按钮了：它由 App 在每次刷新后**后台**由近到远
    # 自动往前挪，进度写在标题栏小字。这里只做只读展示，不提供点击 ——
    # 以前那个入口点开只有一个弹窗、跑完也只说「跳过 N 天」，用户只会觉得没反应。
    # 常用动作走 2 列网格：每张卡固定 68px，和账号行一样高。
    # 说明文字一律控制在一行内（≤14 字），长文案改去别处说。
    tool_tiles = acts_grid([
        act("⇪", "导出今日看板", "生成长图", action="export"),
        act("✂", "回放素材", f"{_clip_count()} 项照片和视频", action="clips"),
        act("⌖", "地图底图",
            "已设置 · 显示地图" if _map_on() else "未设置 · 仅显示轨迹",
            action="amap"),
        # 文案按 jack 那版的精简（一行内 ≤14 字），但保留他基线上不存在的
        # 「训练小组」「账号」两项——那是后来加的功能，不能因为合分支丢掉。
        act("⚙", "运动设置", "心率区间与骑行 FTP", href="/sports"),
        act("⇧", "导入到高驰", "预览训练课表", href="/training"),
        act("▦", "日历提醒", "同步到系统日历", action="calendarSync"),
        act("◈", "训练小组", _grp_desc, href="/groups"),
        act("◉", "账号", _acc_desc, href="/account"),
    ])
    # 数据与备份：导出/导入是「换手机保命」的两个动作，放同一排最显眼
    data_tiles = acts_grid([
        act("⇩", "导出数据", "打包历史与设置", action="dbExport"),
        act("⇧", "导入数据", "先备份再覆盖", action="dbImport"),
        act("◫", "导出文件", "查看图片和视频", href="/files"),
        act("⭡", "检查更新", f"当前 v{VER}", action="update"),
    ])
    # 诊断日志单独一行、样式压低：它只在出问题时才需要，不该抢网格的位置，
    # 但也不能藏进二级菜单——崩溃时用户复述不清，能一键复制才有用。
    diag_row = row("!", "诊断日志", "遇到问题时，可以复制日志帮助排查",
                   act="diag", tone="a")

    about_inner = ('<div class="rc-about"><div class="rc-about-h">' +
             BR.badge_html(30) +
             f'<div><div class="lab">RunnerCoach · v{esc(VER)}</div>' +
             '<div class="note" style="margin-top:2px">跑步与骑行训练助手</div>'
             '</div></div>'
             '<div class="note" style="margin-top:8px;font-size:12px;line-height:1.65">'
             '数据只存本机 App 私有目录，看板 / 图片 / 回放全本地生成；'
             '无 CDN、远程字体与统计，卸载即清空。'
             '<a class="link-act" href="/privacy"> 隐私说明 →</a>'
             '</div></div>')

    # 三个数字没必要各发一张卡：它们是一句话的三个分句。
    # 原来 3 张 stat 卡约 110px，换一行 30px，且一眼能连起来读。
    _ls = str(st.get("last_sync") or "")
    stat_line = (f'<div class="minirow">'
                 f'<span><b>{cov["days"]}</b> 天数据</span>'
                 f'<span><b>{cov["activities"]}</b> 条活动</span>'
                 f'<span>上次同步 <b>{_ls[5:16] or "—"}</b></span></div>'
                 f'<div class="tinyline">'
                 f'{esc(str(cov["first"])[5:])} ~ {esc(str(cov["last"])[5:])}'
                 ' · 后台自动补齐历史记录，进度在页面顶部</div>')

    # 关于与诊断合成一块折叠：这两样都是「装完基本不再看」的东西，
    # 却原来各占一整块（logo + 两段说明 + 一行诊断），加起来接近半屏。
    # 默认收起时只留一行版本号——需要时点开，信息一条不少。
    about = ('<details class="fold"><summary>关于与诊断 · v' + esc(VER) +
             '<span class="fx">›</span></summary>' + about_inner +
             '<div class="me-diag">' + diag_row + '</div></details>')

    body = ('<div class="me">' +
            sec("账号与数据", sources) +
            sec("常用", tool_tiles) +
            sec("数据与备份", data_tiles) +
            sec("本地数据", stat_line) +
            sec("关于", about) +
            '</div>')
    return page("ME · 我的", "数据与设置", "管理账号、数据备份和隐私", body, chips)


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
        "每日数据、活动记录、体成分和回放素材清单都保存在 App 的本地数据库中，"
        "卸载 App 后会清除这些数据。佳明、高驰的登录状态保存在 App 私有目录，"
        "Android 版使用系统的 Keystore 保护密码。")

    net = p(
        "以下三类服务需要联网，由你选择使用。<br>"
        "1. <b>官方接口</b>：佳明国区、高驰、小米健康云，用你自己的账号登录后同步；<br>"
        "2. <b>AI 服务</b>（可选）：你填自己的 base_url 与 key，提问内容直连你选的服务商；<br>"
        "3. <b>高德底图</b>（可选）：只请求地图瓦片，用于活动图与回放底图。<br>"
        "没有中间服务器，任何数据都不经过本项目作者。")

    share = p(
        "活动图与轨迹回放<b>包含 GPS 轨迹</b>，起终点可能暴露住址。"
        "生成前可以选择裁剪起点和终点，或隐藏整条轨迹。"
        "导出文件只包含你选择保留的轨迹。")

    never = p(
        "不做代登录服务（佳明条款不允许把凭据交给第三方）；"
        "不把任何数据托管到服务器；不转卖、不用于训练模型。")

    body = (sec("收集哪些数据", collect) + sec("存在哪里", where) +
            sec("什么时候会联网", net) + sec("分享前", share) +
            sec("不会做的事", never))
    return page("PRIVACY · 隐私", "隐私说明",
                "数据保存在这台设备上，以下是联网和分享时的处理方式",
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
        rows = '<div class="empty">还没有导出文件。同步后可查看每日看板，也可以导出活动图或回放视频。</div>'

    # 批量管理：全选 / 反选 / 二次确认删除。确认走页面内联，
    # 不用 window.confirm —— WebView 没挂 WebChromeClient，confirm 点了没反应。
    picker = f"""
<style>
.row.pick{{cursor:pointer;gap:10px}}
.row.pick .ck{{display:none;width:18px;height:18px;flex:0 0 auto;margin:0;
  accent-color:var(--brand)}}
/* 管理态：左侧让出勾选框，箭头变淡（仍可点开文件） */
body.manage .row.pick{{cursor:default;background:var(--brand-soft)}}
body.manage .row.pick .ck{{display:block}}
body.manage .row.pick .go{{opacity:.35}}
.row.pick .go{{text-decoration:none;font-size:20px;line-height:1;color:var(--muted);
  padding:0 4px;flex:0 0 auto}}
.row.pick .go:active{{color:var(--brand-ink)}}
.pickbar{{position:fixed;left:10px;right:10px;bottom:10px;z-index:40;display:none;
  gap:8px;align-items:center;padding:10px 12px;border-radius:18px;
  background:rgba(15,22,34,.97);border:1px solid var(--line);
  box-shadow:0 18px 40px -18px rgba(0,0,0,.92)}}
.pickbar.on{{display:flex}}
.manage-btn{{font-size:12px;padding:5px 12px;line-height:1.2}}
.pickbar .n{{flex:1;font-size:12px;color:var(--ink2);min-width:0}}
.btn.danger{{border-color:var(--bad-soft);color:var(--bad)}}
.btn.danger.armed{{background:var(--bad);color:var(--bg);border-color:transparent;
  font-weight:600}}
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
    info.textContent = n ? ('已选 ' + n + ' 项') : '勾选要删除的文件';
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
      '<div style="height:8px;border-radius:999px;background:var(--line-brand);' +
      'overflow:hidden;margin-top:10px">' +
      '<i style="display:block;height:100%;width:' + j.pct + '%;' +
      'background:linear-gradient(90deg,var(--cyan),var(--brand));transition:width .4s"></i></div>' +
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
    return page("FILES · 导出文件", "本机导出文件", "看板、图片、视频都只存在这台设备上",
                jobs + sec("最近生成", rows, manage_btn) + picker,
                chip(f"{len(files)} 个"), back=("返回我的", "/me"))
