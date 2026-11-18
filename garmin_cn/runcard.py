# -*- coding: utf-8 -*-
"""赛季结算分享图：1080 宽的竖图，给相册和微信。

为什么不用图像生成
------------------
这张图上要写真实的东西：结局名、总行程、士气、旧日志、四段走够了没有。
图像模型写中文十个字错三个，写数字几乎必错；错一个数，这张图就不好意思
发出去。所以走项目里现成的出图链路：Python 出 1080 宽的 HTML，App 的
离屏 WebView `draw(Canvas)` 成 PNG（契约见 `exportspec.py`）。和活动图、
一图流是同一套，画幅一致，分享面板不会对竖图二次裁切。

版式
----
上半「地方」，下半「这一季」：同一片雪地（复用结算页的插画），下面才是
数字与四段路。不做花边、不做大渐变光——分享图会被缩进聊天窗口，那时候
只剩轮廓能看清，装饰全部糊成脏色。

三个具体决定
------------
1. **插画横向铺满、不裁**。`_ending_svg` 的 viewBox 是 372×132，铺到
   1080 宽正好 383 高，比例不变形。
2. **四段用「走够 / 没够」不评分**。和结算页一致：远征不是考试。
3. **右下角不放二维码**。现在还没有人人可点的落地页，放一个扫不出东西
   的码比不放更差。
"""

from . import brand as BR
from .exportspec import BG_ACT, probe as _export_probe
from .runsui import CN_NUM, FONT_CSS, SCENE_CSS, _ending_scene
from .ui import esc

W = 1080
H = 1920
# 底色必须和 exportspec 的常量一致：App 出图前按这个颜色裁掉四周留白，
# 差一个色号就会在成品边上留一圈细边。
BG = BG_ACT

_PROBE = _export_probe()

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{width:1080px;background:#0F1319}
body{font-family:-apple-system,"PingFang SC","Noto Sans CJK SC",sans-serif;
  color:#EDF1F5;-webkit-font-smoothing:antialiased}
/* 不加整页渐变：出图前要按底色裁四周留白，渐变会让四边颜色不一致，
   裁不干净就会留一圈细边。要气氛就放进插画那一块。 */
/* 画布锁死 9:16（1080×1920）：出图链路按 contain 收进 9:16，内容比画布矮
   就会在上下留大块底色。所以这里把高度写死，把余量交给 `.end` 和 `.acts`
   两个 `flex:1` 的块分掉——留白是呼吸，不是凑数。 */
.wrap{position:relative;width:1080px;height:1920px;padding:76px 64px 64px;
  background:#0F1319;display:flex;flex-direction:column}

/* ---------- 抬头：左边是哪一段路，右边是哪一段时间 ---------- */
.top{display:flex;align-items:baseline;justify-content:space-between;
  padding-bottom:18px;border-bottom:1px solid rgba(120,150,190,.16)}
.kicker{font-size:22px;font-weight:700;letter-spacing:2.4px;color:#22D3EE}
.dates{font-size:22px;letter-spacing:.6px;color:#6B7A8D;
  font-variant-numeric:tabular-nums}

/* ---------- 雪地 ---------- */
.scene{margin-top:34px;border-radius:22px;overflow:hidden;
  border:1px solid rgba(120,150,190,.16);
  background:radial-gradient(90% 120% at 50% 0%,#152740,#0A1220 78%)}
.scene svg{display:block;width:100%;height:auto}

/* ---------- 结局 ---------- */
.end{flex:1 1 auto;display:flex;flex-direction:column;justify-content:center;
  text-align:center;min-height:0}
.end h1{font-size:88px;font-weight:700;letter-spacing:1px;line-height:1.2}
.end .line{margin-top:18px;font-size:30px;color:#8FA0B4;letter-spacing:1.2px}
.lit{font-family:"RC Serif",Georgia,"Songti SC",serif;text-align:justify;
  text-align-last:left;color:#C6D4E6}
.end .lit{margin:34px auto 0;max-width:900px;font-size:32px;line-height:1.95}

/* ---------- 数字 ---------- */
.stats{flex:0 0 auto;display:grid;grid-template-columns:repeat(4,1fr);
  padding-top:34px;padding-bottom:34px;border-top:1px solid rgba(120,150,190,.16)}
.stats div{display:flex;flex-direction:column;gap:8px;align-items:center}
.stats b{font-size:62px;font-weight:650;letter-spacing:-1px;
  font-variant-numeric:tabular-nums}
.stats b i{font-style:normal;font-size:22px;color:#6B7A8D;margin-left:7px}
.stats span{font-size:22px;color:#6B7A8D}

/* ---------- 四段路 ---------- */
.acts{flex:1 1 auto;display:flex;flex-direction:column;justify-content:center;
  padding-top:30px;border-top:1px solid rgba(120,150,190,.16);min-height:0}
.acts .h{display:flex;justify-content:space-between;align-items:baseline;
  margin-bottom:6px}
.acts .h span{font-size:20px;font-weight:700;letter-spacing:2px;color:#6B7A8D}
.ar{display:grid;grid-template-columns:110px 170px 1fr 110px 86px;gap:18px;
  align-items:center;padding:26px 0;border-bottom:1px solid rgba(120,150,190,.10)}
.ar:last-child{border-bottom:0}
.ar .an{font-size:20px;color:#6B7A8D}
.ar .at{font-size:27px;color:#C6D4E6}
.ab{height:8px;border-radius:5px;background:#1B2A3D;overflow:hidden}
.ab i{display:block;height:100%;border-radius:5px;
  background:linear-gradient(90deg,#3B82F6,#22D3EE)}
.ar.ok .ab i{background:linear-gradient(90deg,#2F7F6B,#4FD6A6)}
.ar .av{font-size:27px;text-align:right;color:#C6D4E6;
  font-variant-numeric:tabular-nums}
.ar .av i{font-style:normal;font-size:20px;color:#6B7A8D}
.ar .am{font-size:20px;text-align:right;color:#6B7A8D}
.ar.ok .am{color:#4FD6A6}
.ar.miss .at{color:#6B7A8D}

/* ---------- 页脚 ---------- */
.foot{flex:0 0 auto;display:flex;align-items:center;justify-content:space-between;
  margin-top:34px;padding-top:26px;border-top:1px solid rgba(120,150,190,.16)}
.foot .b{display:flex;align-items:center;gap:12px;font-size:22px;
  font-weight:600;letter-spacing:.6px;color:#8FA4BF}
.foot .n{font-size:20px;color:#4E5A69}
"""


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def build_html(d: dict) -> str:
    """`d` 是 `/run/outcome` 在赛季结束时的那份返回。"""
    detail = d.get("ending_detail") or {}
    key = str(d.get("ending") or "lost")
    title = esc(str(detail.get("title") or "结束了"))
    line = esc(str(detail.get("line") or ""))
    text = esc(str(detail.get("text") or ""))
    season = esc(str(d.get("season") or "雪线之前"))

    km = _num(d.get("km"))
    members = int(_num(d.get("members")))
    relics = int(_num(d.get("relic_count")))
    acts = d.get("acts") or []
    # 走够了三段还是两段，比「士气 7」更像这一季的结论。没有结算过的幕
    # 不计入分母——它不是没走够，是没走到。
    judged = [a for a in acts if _num(a.get("target")) > 0]
    hits = sum(1 for a in judged if a.get("hit"))
    # 旧日志只在真的拿到过时才占一格：它是藏起来的东西，一张没找到的
    # 分享图上写「旧日志 0/3」等于替玩家宣布他错过了什么。
    relic_stat = (f'<div><b>{relics}<i>/3</i></b><span>旧日志</span></div>'
                  if relics else "")
    stats_n = 4 if relics else 3

    # 日期只到「月.日」：分享图上写年份没意义，反而像报表
    start = str(d.get("start") or "")[5:].replace("-", ".")
    end = str(d.get("end") or "")[5:].replace("-", ".")

    rows = ""
    for a in acts:
        got, need = _num(a.get("got")), _num(a.get("target"))
        theme = esc(str(a.get("theme") or ""))
        idx = int(_num(a.get("act")) or 1)
        name = CN_NUM[idx - 1] if 1 <= idx <= len(CN_NUM) else str(idx)
        if need <= 0:
            # 没结算过：不是「没走够」，是「没走到」。两件事，画法不同。
            rows += (f'<div class="ar miss"><span class="an">第{name}段</span>'
                     f'<span class="at">{theme}</span>'
                     '<span class="ab"></span>'
                     '<span class="av">—</span>'
                     '<span class="am">没走到</span></div>')
            continue
        hit = bool(a.get("hit"))
        pct = max(0.0, min(100.0, got / need * 100))
        rows += (f'<div class="ar{" ok" if hit else ""}">'
                 f'<span class="an">第{name}段</span>'
                 f'<span class="at">{theme}</span>'
                 f'<span class="ab"><i style="width:{pct:.1f}%"></i></span>'
                 f'<span class="av">{got:g}<i>/{need:g}</i></span>'
                 f'<span class="am">{"走够" if hit else "没够"}</span></div>')

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width={W},initial-scale=1">
<title>{title} · {season}</title>
<style>{FONT_CSS}{CSS}{SCENE_CSS}</style></head><body>
<div class="wrap" data-gc-canvas>
  <div class="top">
    <div class="kicker">EXPEDITION · {season}</div>
    <div class="dates">{esc(start)} — {esc(end)}</div>
  </div>

  <div class="scene">{_ending_scene(key)}</div>

  <div class="end">
    <h1>{title}</h1>
    <div class="line">{line}</div>
    <div class="lit">{text}</div>
  </div>

  <div class="stats" style="grid-template-columns:repeat({stats_n},1fr)">
    <div><b>{km:g}<i>km</i></b><span>走到的里程</span></div>
    <div><b>{members}</b><span>走过的人</span></div>
    <div><b>{hits}<i>/{len(judged)}</i></b><span>四段路走够</span></div>
    {relic_stat}
  </div>

  <div class="acts">
    <div class="h"><span>这一段，走到哪</span><span>按全队周目标折算</span></div>
    {rows}
  </div>

  <div class="foot">
    <span class="b">{BR.badge_html(30)}RunnerCoach</span>
    <span class="n">数据只存本机，不上传轨迹与位置</span>
  </div>
</div>
{_PROBE}</body></html>"""
