"""手机端出图的「画幅 + 缩放契约」：Python 与安卓 WebView 之间唯一的尺寸口径。

为什么需要这一层
----------------
桌面端 `actcard.shoot()` 出完图会走 `pngutil.fit_to_frame()` 把成品收进
1080×1920（`card.shoot()` 本身就是这个尺寸）。手机相册、系统分享面板、
微信对「非 9:16 竖图」的缩略图一律按预览框做 centerCrop，非标画幅会被
四边切掉，典型症状就是分享预览里日期文字缺头少尾、轨迹只剩一角。

APK 走的是另一条路：App 只拿到 HTML，由离屏 WebView 自己 `draw(Canvas)`
出 PNG，不经过 pngutil。这条路还有一个更隐蔽的坑：HTML 写死了
`<meta name="viewport" content="width=1080,initial-scale=1">`，于是布局
视口恒为 1080 CSS px，但 WebView 控件只有 1080 **物理像素**（≈360dp）。
`initial-scale=1` 让 1 CSS px 摊到 density 个物理像素，整页按 2~4 倍画
到了画布之外，`draw(Canvas)` 只拿到左上角一块；再被 contain 收进 9:16，
分享预览就成了「只剩正常图的一个角、数字特别大」。

`window.innerWidth` 是布局视口宽度，不是「当前屏幕上能看见多少 CSS px」，
用它反推缩放必然误判。真正可信的是 `window.visualViewport.width`：

* 页面被缩到刚好铺满控件时，它是 1080，缩放系数 f = 1；
* 页面按 density=3 放大、只露出左 1/3 时，它是 360，f = 1/3，
  给画布套 `transform: scale(1/3)` 后正好铺满控件宽度。

于是约定：

1. HTML 注入只读探针，回报「画布 CSS 尺寸 / 布局视口 / 可视视口 / dpr」，
   并提供 `__gcFit(f)` 给 App 套 transform；桌面无头截图两者都不调用；
2. App 按 `f = visualViewport.width / cssWidth` 校正绘制，按内容真实高度
   加高 View，截完按背景色裁掉空白，再等比 contain 收进 9:16 画幅；
3. 原 `width` / `height` 字段保持不变（旧 APK 仍按老逻辑工作），
   新增的 `export_*` 字段只被新 APK 读取。

归一化之后内容在位图里占 `cssHeight * viewWidth / cssWidth` 个物理像素，
与 density、页面缩放都无关，所以桌面和手机的成品版式一致。
"""
from __future__ import annotations

import math

# 标准 9:16 竖屏画幅。W*16 == H*9，改动时 check() 会拦下来。
W = 1080
H = 1920

# 两种卡片各自的页面底色，App 用它填充画幅留白、并判断「内容底边」。
# 必须与 card.py / actcard.py 里 body 的 background 一致。
BG_CARD = "#12161B"
BG_ACT = "#0F1319"

# 最外层画布的标记，探针和 App 都按它找内容盒。
CANVAS_ATTR = "data-gc-canvas"

# 探针返回 6 段纯数字，不用 JSON：evaluateJavascript 对字符串返回值会加引号
# 和转义，管道分隔最不容易解析错。第 5 段（visualViewport.width）是关键。
PROBE = """<script>
/* 只读导出探针 + 缩放校正：桌面无头截图不会调用，对版式零影响。 */
(function () {
  var el = document.querySelector('[%s]') || document.body;
  var base = null;
  function baseSize() {
    if (!base) {
      var r = el.getBoundingClientRect();
      base = [Math.round(r.width), Math.round(r.height)];
    }
    return base;
  }
  window.__gcExportInfo = function () {
    var b = baseSize();
    var vv = window.visualViewport;
    return [
      b[0], b[1],
      window.innerWidth,
      document.documentElement.scrollHeight,
      vv ? Math.round(vv.width) : 0,
      window.devicePixelRatio || 1
    ].join('|');
  };
  /* transform 只改绘制、不改布局，底色/字体/图表仍按原版式栅格化。 */
  window.__gcFit = function (f) {
    var b = baseSize();
    el.style.transformOrigin = '0 0';
    el.style.transform = 'scale(' + f + ')';
    return b[0] + '|' + b[1] + '|' + f;
  };
})();
</script>""" % CANVAS_ATTR


def fit_scale(css_width: int, visual_width: int) -> float:
    """给固定版心套的 CSS `transform: scale(f)`。

    `visual_width` 必须是 `window.visualViewport.width`（可视视口 CSS 宽），
    不能是 `window.innerWidth`（布局视口 CSS 宽）。Kotlin 侧用同一公式。
    """
    if css_width <= 0 or visual_width <= 0:
        return 1.0
    return max(0.1, min(4.0, visual_width / css_width))


def content_height(view_px: int, css_width: int, css_height: int) -> int:
    """校正后内容在位图里应占的物理像素高度（与 density / 页面缩放无关）。"""
    if css_width <= 0 or view_px <= 0 or css_height <= 0:
        return 0
    return math.ceil(css_height * view_px / css_width)


def check() -> None:
    """画幅必须是 9:16，探针和自校正公式也必须配套。"""
    assert W > 0 and H > 0 and W * 16 == H * 9, (W, H)
    assert CANVAS_ATTR in PROBE and "__gcExportInfo" in PROBE and "__gcFit" in PROBE
    assert "visualViewport" in PROBE
    assert fit_scale(1080, 1080) == 1.0
    assert abs(fit_scale(1080, 360) - 1 / 3) < 1e-9
    assert fit_scale(1080, 0) == 1.0
    assert content_height(1080, 1080, 2250) == 2250
    assert content_height(1080, 1080, 1920) == 1920


def spec(bg: str, trim: bool = True) -> dict:
    """给 appapi 的返回值用的导出契约字段（不改动原有 width/height）。

    `trim` 表示出图前要不要把页面四周的底色留白裁掉：
    * 活动图（画幅 1080 宽、真实内容比 9:16 略高）**要裁**，否则页面
      padding 会变成成品里最粗的那圈边框；
    * 一图流（画幅本身就是 1080×1920，比例与成品完全一致）**不要裁**，
      它自己的 padding 就是设计留白，裁掉反而让上下被撑大。
    """
    check()
    assert bg in (BG_CARD, BG_ACT), bg
    return {
        "export_width": W,
        "export_height": H,
        "export_bg": bg,
        # contain = 内容超出画幅时整体等比缩小，绝不裁内容；短了就补同色底。
        "export_mode": "contain",
        "export_trim": trim,
    }


def probe() -> str:
    """注入到 HTML 末尾的探针脚本。"""
    check()
    return PROBE
