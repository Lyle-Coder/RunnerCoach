# -*- coding: utf-8 -*-
"""RunnerCoach 品牌视觉的唯一来源：发光跑道 + 心电脉冲 + 起终点标记。

同一组路径同时供下面这些位置使用，改图形只改这里，不要各处再手画一遍：

  * ``tools/gen_icon.py``                        -> 启动器 mipmap / docs/icon 主图
  * ``android/.../res/drawable/ic_brand_mark.xml`` -> 开屏动画 / 顶栏角标
  * ``garmin_cn/ui.py``                          -> 页头角标、卡片水印、页脚
  * ``garmin_cn/card.py``                        -> 一图流看板
  * ``garmin_cn/actcard.py``                     -> 单次活动图

配色与 ``ui.py`` 的 ``:root`` / ``android values/colors.xml`` 同源：深空蓝底、
科技蓝主色、青色辅光、绿色起点、红色终点。背景一律「渐进 + 辉光」，
禁止再退回平涂；辉光走 CSS radial-gradient 或半透明宽描边，不用
``background-attachment:fixed``（WebView 滚动会重绘整面）。
"""
from __future__ import annotations

from urllib.parse import quote

# ------------------------------------------------------------------ 色板 ----
NAVY = "#0F1B2A"
BRAND = "#3B82F6"
CYAN = "#22D3EE"
BLUE = "#5FA8E0"
BLUE_DEEP = "#2E5A80"
PULSE = "#CFE6FF"
GREEN = "#4FD6A6"
RED = "#E06A5F"

# ------------------------------------------------------------------ 图形 ----
# 130 x 130 单位画布。外/内两圈都是「体育场形」，用三次贝塞尔写死：
# Android vector drawable 既没有 <rect> 也没有可靠的 A 指令，SVG 与 vector
# 用同一串 pathData 才能保证启动器、开屏、网页三个地方长得一模一样。
# 外圈：半径 31，左右圆心 (55,65) / (75,65)；内圈：半径 17。
TRACK_OUT = (
    "M55 34 H75 C92.12 34 106 47.88 106 65 C106 82.12 92.12 96 75 96 "
    "H55 C37.88 96 24 82.12 24 65 C24 47.88 37.88 34 55 34 Z"
)
TRACK_IN = (
    "M55 48 H75 C84.39 48 92 55.61 92 65 C92 74.39 84.39 82 75 82 "
    "H55 C45.61 82 38 74.39 38 65 C38 55.61 45.61 48 55 48 Z"
)
PULSE_PATH = "M22 65 H48 L57 47 L68 85 L78 56 L85 65 H108"

# 全文档共用同一组 defs id。一页里会有好几枚角标，重复 id 在浏览器里
# 解析到第一份 filter/gradient —— 它们内容完全相同，视觉无差别，但能让
# 「同样输入 → 逐字节同样的 HTML」这条性质成立（自检和出图缓存都靠它）。
FILTER_ID = "rc-glow"
TILE_ID = "rc-tile"


def filter_def(fid: str) -> str:
    return ('<filter id="' + fid + '" x="-45%" y="-45%" width="190%" height="190%">'
            '<feGaussianBlur stdDeviation="4"/></filter>')


def glow_layer(fid: str) -> str:
    """模糊宽描边。Android vector 没有滤镜，那边用半透明粗描边近似同一层。"""
    return (
        '<g filter="url(#' + fid + ')" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="' + TRACK_OUT + '" fill="none" stroke="#2E7BBF" stroke-width="11" stroke-opacity=".95"/>'
        '<path d="' + TRACK_IN + '" fill="none" stroke="#2E5A80" stroke-width="4" stroke-opacity=".8"/>'
        '<path d="' + PULSE_PATH + '" fill="none" stroke="' + BLUE + '" stroke-width="10"/>'
        '<circle cx="22" cy="65" r="10" fill="' + GREEN + '"/>'
        '<rect x="98" y="55" width="20" height="20" rx="4" fill="' + RED + '"/>'
        '</g>')


def core_layer() -> str:
    """清晰层：跑道、内圈、脉冲、绿起点、红终点。"""
    return (
        '<g stroke-linecap="round" stroke-linejoin="round">'
        '<path d="' + TRACK_OUT + '" fill="none" stroke="' + BLUE + '" stroke-width="5"/>'
        '<path d="' + TRACK_IN + '" fill="none" stroke="' + BLUE_DEEP + '" stroke-width="3"/>'
        '<path d="' + PULSE_PATH + '" fill="none" stroke="' + PULSE + '" stroke-width="3.5"/>'
        '<circle cx="22" cy="65" r="8" fill="' + GREEN + '"/>'
        '<rect x="100" y="57" width="16" height="16" rx="3.5" fill="' + RED + '"/>'
        '</g>')


def mark_svg(size: int = 32, cls: str = "", *, tile: bool = False,
             halo: bool = True, glow_color: str = BRAND,
             style: str = "") -> str:
    """内联 SVG。``size`` 只写 width/height 属性，外层 CSS 仍可覆盖。

    ``halo`` 是图形后面那团柔光；活动图里把它换成该运动的主色，
    品牌图形本身不变，就能和青/橙/蓝的运动主题协调。
    """
    fid, tid = FILTER_ID, TILE_ID
    defs = "<defs>" + filter_def(fid)
    if tile:
        defs += ('<linearGradient id="' + tid + '" x1="0" y1="0" x2="1" y2="1">'
                 '<stop offset="0" stop-color="#16273D"/>'
                 '<stop offset="1" stop-color="#091320"/>'
                 '</linearGradient>')
    defs += "</defs>"
    bg = ""
    if tile:
        bg = ('<rect width="130" height="130" rx="29" fill="url(#' + tid + ')"/>'
              '<rect width="130" height="130" rx="29" fill="none" '
              'stroke="#60A5FA" stroke-opacity=".34" stroke-width="1.5"/>')
    glow = ""
    if halo:
        glow = ('<circle cx="65" cy="65" r="48" fill="' + glow_color +
                '" opacity=".30" filter="url(#' + fid + ')"/>')
    extra = ' style="' + style + '"' if style else ""
    return ('<svg class="' + cls + '" viewBox="0 0 130 130" width="' + str(size) +
            '" height="' + str(size) + '" xmlns="http://www.w3.org/2000/svg"' + extra +
            '>' + defs + bg + glow + glow_layer(fid) + core_layer() + '</svg>')


def mark_uri(size: int = 140) -> str:
    """给 CSS ``background-image`` 用的 data URI（# 必须编码成 %23）。"""
    return "data:image/svg+xml," + quote(mark_svg(size, halo=False), safe="")


def badge_html(size: int = 32, cls: str = "", glow_color: str = BRAND) -> str:
    """圆角渐变底座 + 品牌图形，用在页头 / 看板抬头 / 活动图抬头。"""
    return ('<span class="rc-badge ' + cls + '" aria-hidden="true" style="width:' +
            str(size) + 'px;height:' + str(size) + 'px">' +
            mark_svg(size, halo=True, glow_color=glow_color) + '</span>')


def watermark_html(size: int = 420, cls: str = "rc-wm") -> str:
    """绝对定位的大尺寸低透明度水印；不参与布局、不吃点击。"""
    return ('<div class="' + cls + '" aria-hidden="true" style="width:' + str(size) +
            'px;height:' + str(size) + 'px">' + mark_svg(size, halo=False) + '</div>')


def rgba(hex_color: str, alpha: float) -> str:
    """把 #RRGGBB 转成 rgba()，活动图按运动主色生成辉光时用。"""
    s = str(hex_color).lstrip("#")
    return "rgba(%d,%d,%d,%.2f)" % (
        int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), alpha)


def glow_overlay_css(inset: str = "34px 52px 26px", *,
                     g1: str = "rgba(59,130,246,.20)",
                     g2: str = "rgba(34,211,238,.10)") -> str:
    """出图专用的辉光层：只在内容盒内铺光，外圈留纯色边框。

    这一步不能偷懒直接改 ``body`` 的背景：``pngutil.trim_bg`` 靠「四边整行
    都是 exportspec.BG_CARD / BG_ACT」去裁页面 padding，App 端也用同一个
    纯色判断内容底边、填充画幅留白。把 body 换成渐变会让裁边整圈失效，
    成片里的字突然变小一圈 —— 正是之前反复踩过的「预览和成片不一致」。
    所以渐变放在卡片上（card_css），辉光放在这层 inset 的装饰层上。
    """
    # 辉光层是 inset 的矩形，渐变在自己边界处还没落到 0，就会在
    # 「纯色外圈 / 内容区」之间切出一条硬边（实测差 7 个色阶，看着像框）。
    # 用 mask 在四边各留一段渐隐，边界处彻底透明，外圈仍是纯色（裁边不受影响）。
    return ("body{position:relative}"
            ".rc-glow{position:absolute;inset:" + inset + ";z-index:0;"
            "pointer-events:none;background:"
            "radial-gradient(760px 540px at 82% -6%, " + g1 + ", transparent 62%),"
            "radial-gradient(600px 460px at 4% 8%, " + g2 + ", transparent 62%);"
            "-webkit-mask-image:"
            "linear-gradient(180deg,transparent,#000 72px,#000 calc(100% - 72px),transparent),"
            "linear-gradient(90deg,transparent,#000 88px,#000 calc(100% - 88px),transparent);"
            "-webkit-mask-composite:source-in;"
            "mask-image:"
            "linear-gradient(180deg,transparent,#000 72px,#000 calc(100% - 72px),transparent),"
            "linear-gradient(90deg,transparent,#000 88px,#000 calc(100% - 88px),transparent);"
            "mask-composite:intersect}")


def card_css(selector: str = ".card", *, top: str = "#1A2027",
             bottom: str = "#141A22", edge: str = "rgba(96,165,250,.14)") -> str:
    """卡片也走渐进：顶沿一道高光 + 极轻的投影，避免深色底糊成一片。"""
    return (selector + "{background:linear-gradient(180deg," + top + "," + bottom + ");"
            "box-shadow:inset 0 1px 0 " + edge +
            ",0 18px 40px -30px rgba(0,0,0,.9)}")


# ------------------------------------------------------- 共用的一段 CSS ----
# 用占位符而不是 f-string：CSS 里全是花括号，拼字符串容易把 {} 吃掉。
_CSS_TMPL = """
/* ---- RunnerCoach 品牌角标 / 水印：ui.py、card.py、actcard.py 共用 ---- */
.rc-badge{position:relative;display:inline-flex;align-items:center;justify-content:center;
  flex:none;line-height:0;overflow:hidden;border-radius:30%;
  background:radial-gradient(120% 120% at 18% 0%,rgba(96,165,250,.30),transparent 62%),
    linear-gradient(155deg,#16273D,#0B1524 72%);
  border:1px solid rgba(96,165,250,.36);
  box-shadow:0 0 20px -6px rgba(59,130,246,.85),inset 0 0 14px rgba(59,130,246,.22)}
.rc-badge::after{content:"";position:absolute;inset:0;pointer-events:none;
  background:linear-gradient(135deg,rgba(255,255,255,.13),transparent 46%)}
.rc-badge>svg{position:relative;z-index:1;width:80%;height:80%;display:block}
.rc-wm{position:absolute;pointer-events:none;z-index:0;opacity:.055;
  filter:drop-shadow(0 0 44px rgba(59,130,246,.7))}
.rc-wm>svg{display:block;width:100%;height:100%}
.rc-foot{display:inline-flex;align-items:center;gap:7px;vertical-align:middle}
.rc-foot .rc-badge{width:22px;height:22px;border-radius:8px;
  box-shadow:0 0 10px -3px rgba(59,130,246,.8)}
.rc-foot .rc-badge>svg{width:82%;height:82%}
.foot .rc-foot{margin-right:8px;color:#8CC2FF;font-weight:600}
.rc-about-h{display:flex;align-items:center;gap:12px}
.rc-about-h .rc-badge{flex:none}
.rc-surface{position:relative;z-index:1}
.brandline{display:flex;align-items:center;gap:9px}
.brandline .kicker{letter-spacing:1.6px}
.card.brand::before,.plan-hero::before{content:"";position:absolute;
  right:-24px;top:-40px;width:116px;height:116px;pointer-events:none;
  background:url("__RC_MARK_URI__") center/contain no-repeat;
  opacity:.05;filter:drop-shadow(0 0 20px rgba(59,130,246,.45))}
"""
BRAND_CSS = _CSS_TMPL.replace("__RC_MARK_URI__", mark_uri(116))


# ------------------------------------------------------------- 开屏预览 ----
def splash_html() -> str:
    """开屏动画的 HTML 版（设计预览 / docs 用）。

    App 里真正的开屏是原生层：windowBackground 先出静态品牌底，
    MainActivity 再盖一层同款渐进+呼吸辉光的动画层，首屏加载完淡出。
    这里保留一份可在浏览器里直接看的版本，方便调色时不装 APK。
    """
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>RunnerCoach · 开屏</title><style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%}
body{display:flex;align-items:center;justify-content:center;overflow:hidden;
  color:#E8F0FB;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
  background:radial-gradient(760px 540px at 78% 12%,rgba(59,130,246,.24),transparent 62%),
    radial-gradient(620px 480px at 12% 88%,rgba(34,211,238,.13),transparent 62%),
    linear-gradient(165deg,#070B12 0%,#0C1624 52%,#0A101B 100%)}
.halo{position:absolute;width:min(78vw,520px);height:min(78vw,520px);border-radius:50%;
  background:radial-gradient(circle,rgba(59,130,246,.42),rgba(34,211,238,.08) 52%,transparent 70%);
  filter:blur(18px);animation:breathe 2.6s ease-in-out infinite alternate}
@keyframes breathe{from{opacity:.35;transform:scale(.92)}to{opacity:.85;transform:scale(1.06)}}
.stack{position:relative;display:flex;flex-direction:column;align-items:center;gap:22px;
  animation:rise .72s cubic-bezier(.2,.9,.3,1.25) both}
@keyframes rise{from{opacity:0;transform:translateY(18px) scale(.9)}to{opacity:1;transform:none}}
.tile{width:132px;height:132px;border-radius:34px;display:flex;align-items:center;
  justify-content:center;position:relative;overflow:hidden;
  background:radial-gradient(120% 120% at 18% 0%,rgba(96,165,250,.32),transparent 62%),
    linear-gradient(155deg,#16273D,#091320 72%);
  border:1px solid rgba(96,165,250,.36);
  box-shadow:0 0 60px -12px rgba(59,130,246,.9),inset 0 0 26px rgba(59,130,246,.22)}
.tile::after{content:"";position:absolute;inset:0;
  background:linear-gradient(135deg,rgba(255,255,255,.14),transparent 46%)}
.tile svg{width:112px;height:112px;position:relative;z-index:1}
.sweep{position:absolute;inset:0;z-index:2;pointer-events:none;
  background:linear-gradient(105deg,transparent 40%,rgba(207,230,255,.20) 50%,transparent 60%);
  animation:sweep 2.4s linear infinite}
@keyframes sweep{from{transform:translateX(-100%)}to{transform:translateX(100%)}}
.word{font-size:27px;font-weight:700;letter-spacing:2.5px;
  text-shadow:0 0 26px rgba(59,130,246,.55)}
.sub{font-size:13px;color:#8FA4BF;letter-spacing:1px;margin-top:-12px}
.track{width:186px;height:3px;border-radius:99px;overflow:hidden;
  background:rgba(96,165,250,.16);margin-top:6px;position:relative}
.track i{position:absolute;top:0;bottom:0;width:42%;border-radius:99px;
  background:linear-gradient(90deg,transparent,#3B82F6,#22D3EE,transparent);
  box-shadow:0 0 16px rgba(59,130,246,.9);animation:run 1.5s ease-in-out infinite}
@keyframes run{from{left:-42%}to{left:100%}}
</style></head><body>
<div class="halo"></div>
<div class="stack">
  <div class="tile">__RC_MARK__<div class="sweep"></div></div>
  <div class="word">RunnerCoach</div>
  <div class="sub">正在准备今日数据…</div>
  <div class="track"><i></i></div>
</div>
</body></html>""".replace("__RC_MARK__", mark_svg(112, halo=True))
