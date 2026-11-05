"""手机端出图的「画幅契约」：Python 与安卓 WebView 之间唯一的尺寸口径。

为什么需要这一层
----------------
桌面端 `actcard.shoot()` 出完图会走 `pngutil.fit_to_frame()` 把成品收进
1080×1920（`card.shoot()` 本身就是这个尺寸）。手机相册、系统分享面板、
微信对「非 9:16 竖图」的缩略图一律按预览框做 centerCrop，非标画幅会被
四边切掉，典型症状就是分享预览里日期文字缺头少尾、轨迹只剩一角。

APK 走的是另一条路：App 只拿到 HTML，由离屏 WebView 自己 `draw(Canvas)`
出 PNG，不经过 pngutil，而且 WebView 的 density / overview 缩放、父容器
重测量都会让 View 的实际像素尺寸与 HTML 的 CSS 尺寸对不上。所以：

1. HTML 里注入一个只读探针，把「layout 视口宽度 + 画布实际 CSS 高度」
   回报给 Kotlin，让 App 自己算缩放系数，不再猜 density；
2. App 截完图按背景色找内容底边，裁掉多余空白，再等比缩放（contain）
   到本模块定义的 9:16 画幅——只缩不裁，内容永远完整；
3. 原 `width` / `height` 字段保持不变（旧 APK 仍按老逻辑工作），
   新增的 `export_*` 字段只被新 APK 读取。

桌面无头截图不调用探针，也不读这些字段，行为完全不变。
"""
from __future__ import annotations

# 标准 9:16 竖屏画幅。W*16 == H*9，改动时 check() 会拦下来。
W = 1080
H = 1920

# 两种卡片各自的页面底色，App 用它填充画幅留白、并判断「内容底边」。
# 必须与 card.py / actcard.py 里 body 的 background 一致。
BG_CARD = "#12161B"
BG_ACT = "#0F1319"

# 最外层画布的标记，探针和 App 都按它找内容盒。
CANVAS_ATTR = "data-gc-canvas"

# 探针返回 "cssW|cssH|innerW|scrollH" 四段纯数字，不用 JSON：
# evaluateJavascript 对字符串返回值会加引号和转义，管道分隔最不容易解析错。
PROBE = """<script>
/* 只读导出探针：桌面无头截图不会调用，对版式零影响。 */
(function () {
  window.__gcExportInfo = function () {
    var el = document.querySelector('[%s]') || document.body;
    var r = el.getBoundingClientRect();
    return [
      Math.round(r.width),
      Math.round(r.height),
      window.innerWidth,
      document.documentElement.scrollHeight
    ].join('|');
  };
})();
</script>""" % CANVAS_ATTR


def check() -> None:
    """画幅必须是 9:16，否则分享面板的 centerCrop 又会切掉内容。"""
    assert W > 0 and H > 0 and W * 16 == H * 9, (W, H)


def spec(bg: str) -> dict:
    """给 appapi 的返回值用的导出契约字段（不改动原有 width/height）。"""
    check()
    assert bg in (BG_CARD, BG_ACT), bg
    return {
        "export_width": W,
        "export_height": H,
        "export_bg": bg,
        # contain = 内容超出画幅时整体等比缩小，绝不裁内容；短了就在底部补底色。
        "export_mode": "contain",
    }


def probe() -> str:
    """注入到 HTML 末尾的探针脚本。"""
    check()
    return PROBE
