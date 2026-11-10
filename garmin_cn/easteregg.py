"""「完美渐加速」彩蛋：判定 + 彩虹配色 + 炫彩五角星徽章。

两条触发路径
------------
* 普通活动：有效分圈 ≥ `MIN_SETS` 圈，且每一圈的配速都不慢于上一圈；
* 间歇活动：底部走「间歇结构」版式，改用**强度组**（`interval.detect()` 产出的
  `order` 里非恢复段）做同样的判定。间歇卡没有分圈条，拿包含组间休息的
  原始计圈去比快慢没有意义。

「快于等于」的口径
------------------
`pace_s` 是秒/公里，数值越小越快。允许 `PACE_TOL_S` 的 GPS 抖动（1 s/km，
佳明/高驰的圈配速本身就取整到秒），但首尾必须真的拉开 `MIN_GAIN_*` 的差距：
否则一圈圈匀速跑也算「渐加速」，彩蛋就不稀奇了。

判定只看数据，不看运动类型：骑行/游泳的 `pace_s` 同样是「每公里秒数」，
语义一致。纯标准库、零 IO；渲染部分给 `actcard.py` 用。
"""
from __future__ import annotations

import colorsys
import hashlib
import math

# 至少要 5 段才叫「一路加速」，两三圈太容易凑巧
MIN_SETS = 5
# 允许比上一圈慢不超过 1 s/km（GPS 抖动 / 服务端取整）
PACE_TOL_S = 1.0
# 首尾至少快 4 s/km，且至少快 1.5%，避免把匀速跑当成渐加速
MIN_GAIN_S = 4.0
MIN_GAIN_RATIO = 0.015
# 太短的圈（起停、GPS 漂移、手动误触计圈）不参与判定
MIN_LAP_M = 100.0

BADGE_LINE1 = "完美"
BADGE_LINE2 = "渐加速"


# ------------------------------------------------------------------ 判定
def lap_paces(laps: list[dict] | None) -> list[float]:
    """按时间顺序取出可比的分圈配速（秒/公里）。"""
    out: list[float] = []
    for l in laps or []:
        if not isinstance(l, dict):
            continue
        p = l.get("pace_s")
        if not p or p <= 0:
            continue
        d = l.get("dist_m") or 0
        # 残圈（不满 100 m / 被标成 is_partial）配速不可靠，直接跳过
        if l.get("is_partial") or (d and d < MIN_LAP_M):
            continue
        out.append(float(p))
    return out


def interval_paces(iv: dict | None) -> list[float]:
    """间歇的「强度组」配速：order 里非恢复段，按开始时刻排序。"""
    segs = [o for o in ((iv or {}).get("order") or [])
            if isinstance(o, dict) and not o.get("rest") and (o.get("pace_s") or 0) > 0]
    segs.sort(key=lambda o: o.get("t0") or 0)
    return [float(o["pace_s"]) for o in segs]


def is_perfect(paces: list[float], *, min_sets: int = MIN_SETS,
               tol: float = PACE_TOL_S) -> bool:
    """每圈配速快于等于上一圈（数值非增），且首尾确实拉开了差距。"""
    if len(paces) < min_sets or any(p <= 0 for p in paces):
        return False
    for prev, cur in zip(paces, paces[1:]):
        if cur > prev + tol:        # 慢了超过抖动容差 → 断了
            return False
    gain = paces[0] - paces[-1]
    return gain >= max(MIN_GAIN_S, paces[0] * MIN_GAIN_RATIO)


def evaluate(laps: list[dict] | None, iv: dict | None = None) -> dict | None:
    """命中返回 {'kind','sets','gain_s','first','last'}，否则 None。

    有间歇结构时以强度组为准，不再回退到分圈 —— 两条路径对应两种版式，
    混着判会让一张图里出现「按休息圈判出来的彩虹」。
    """
    if iv:
        paces, kind = interval_paces(iv), "interval"
    else:
        paces, kind = lap_paces(laps), "laps"
    if not is_perfect(paces):
        return None
    return {"kind": kind, "sets": len(paces),
            "gain_s": round(paces[0] - paces[-1], 1),
            "first": paces[0], "last": paces[-1]}


# ------------------------------------------------------------------ 配色
RAINBOW = ("#FF5D73", "#FF9F45", "#FFD84A", "#63E6A0",
           "#4FD8E8", "#5C9CFF", "#B981FF")


def _hsl(t: float, light: int = 62) -> str:
    """色带位置 0..1 → 红橙黄绿青蓝紫；逗号写法兼容旧 WebView。"""
    h = (4 + 292 * max(0.0, min(1.0, t))) % 360
    return f"hsl({h:.0f},92%,{light}%)"


def rainbow_bg(i: int, n: int) -> str:
    """第 i / n 条数据的彩虹渐变：相邻条首尾接得上，整组看是一条连续色带。"""
    span = max(1, n - 1)
    t0 = i / span
    t1 = min(1.0, t0 + 0.75 / span + 0.04)
    return f"linear-gradient(90deg,{_hsl(t0, 57)},{_hsl(t1, 66)})"


def rainbow_hex(i: int, n: int) -> str:
    """给不需要渐变的场景（图例小色块）用，与 `rainbow_bg` 同一条色带。"""
    t = i / max(1, n - 1)
    h = (4 + 292 * max(0.0, min(1.0, t))) % 360
    r, g, b = colorsys.hls_to_rgb(h / 360, 0.62, 0.92)
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


# ------------------------------------------------------------------ 徽章
def _star_path(cx: float, cy: float, r_out: float, r_in: float,
               n: int = 5, start: float = -90.0) -> str:
    pts = []
    for k in range(n * 2):
        r = r_out if k % 2 == 0 else r_in
        a = math.radians(start + k * 180.0 / n)
        pts.append(f"{cx + r * math.cos(a):.1f},{cy + r * math.sin(a):.1f}")
    return "M" + " L".join(pts) + " Z"


def _sparkle(cx: float, cy: float, r: float, opacity: float = 0.8) -> str:
    """四角小星星，给徽章加一点「闪」的感觉。"""
    pts = []
    for k in range(8):
        rr = r if k % 2 == 0 else r * 0.16
        a = math.radians(-90 + k * 45)
        pts.append(f"{cx + rr * math.cos(a):.1f},{cy + rr * math.sin(a):.1f}")
    return (f'<path d="M{" L".join(pts)} Z" fill="#FFFFFF" '
            f'opacity="{opacity}"/>')


def badge_html(uid: str = "gcEgg") -> str:
    """右上角炫彩五角星徽章（纯 SVG，不依赖 CSS clip-path / conic-gradient）。

    彩虹渐变星 + 深色圆形奖章芯，芯上两行「完美 / 渐加速」；外圈再叠一层
    模糊彩虹光晕。渐变 id 带 uid，页面里嵌多张活动图也不会串色。
    """
    star = _star_path(80, 80, 72, 31)
    g = f"url(#{uid}Rainbow)"
    stops = "".join(
        f'<stop offset="{i / (len(RAINBOW) - 1):.3f}" stop-color="{c}"/>'
        for i, c in enumerate(RAINBOW))
    return f'''<div class="egg-badge" aria-hidden="true">
<svg viewBox="0 0 160 160" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="{uid}Rainbow" x1="0" y1="0" x2="1" y2="1">
      {stops}
    </linearGradient>
    <linearGradient id="{uid}Sheen" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0.32" stop-color="#FFFFFF" stop-opacity="0"/>
      <stop offset="0.46" stop-color="#FFFFFF" stop-opacity="0.55"/>
      <stop offset="0.58" stop-color="#FFFFFF" stop-opacity="0"/>
    </linearGradient>
    <filter id="{uid}Blur" x="-60%" y="-60%" width="220%" height="220%">
      <feGaussianBlur stdDeviation="7"/>
    </filter>
  </defs>
  <circle cx="80" cy="82" r="64" fill="{g}" opacity="0.55"
          filter="url(#{uid}Blur)"/>
  <path d="{star}" transform="translate(0,4)" fill="#050A12" opacity="0.75"/>
  <path d="{star}" fill="{g}" stroke="#0A1220" stroke-width="3.5"
        stroke-linejoin="round"/>
  <path d="{star}" fill="url(#{uid}Sheen)" opacity="0.5"/>
  <path d="{star}" fill="none" stroke="#FFFFFF" stroke-opacity="0.42"
        stroke-width="1.2" stroke-linejoin="round"/>
  <circle cx="80" cy="84" r="38" fill="#0B1220" fill-opacity="0.90"
          stroke="{g}" stroke-width="3"/>
  <circle cx="80" cy="84" r="38" fill="none" stroke="#FFFFFF"
          stroke-opacity="0.18" stroke-width="1"/>
  <text x="80" y="80" text-anchor="middle" font-size="28" font-weight="800"
        font-family="Microsoft YaHei,PingFang SC,sans-serif" fill="#FFFFFF"
        stroke="#0A1220" stroke-width="2.5" paint-order="stroke">{BADGE_LINE1}</text>
  <text x="80" y="104" text-anchor="middle" font-size="19" font-weight="700"
        font-family="Microsoft YaHei,PingFang SC,sans-serif" fill="#EAF4FF"
        stroke="#0A1220" stroke-width="2.5" paint-order="stroke"
        letter-spacing="0.5">{BADGE_LINE2}</text>
  {_sparkle(20, 28, 11, 0.85)}
  {_sparkle(139, 34, 8, 0.7)}
  {_sparkle(137, 131, 7, 0.6)}
  {_sparkle(24, 137, 6, 0.55)}
</svg></div>'''


def badge_uid(row: dict, title: str | None = None) -> str:
    """稳定且唯一的渐变 id 后缀：同一活动每次出图得到同样的 HTML。"""
    key = str(row.get("activity_id") or title or row.get("name") or "egg")
    return "e" + hashlib.md5(key.encode("utf-8")).hexdigest()[:6]


# ------------------------------------------------------------------ 样式
EGG_CSS = """
/* 「完美渐加速」彩蛋：右上角徽章要给抬头让出位置，避免压住标题/运动标签。
   .page 是相对定位的 flex 容器，绝对定位子元素不占 flex 间隙。 */
.egg-badge{position:absolute;right:18px;top:10px;width:146px;height:146px;
  transform:rotate(9deg);z-index:6;pointer-events:none;
  filter:drop-shadow(0 10px 16px rgba(0,0,0,.55))}
.egg-badge svg{display:block;width:100%;height:100%}
.head.egg{padding-right:160px}
/* 分圈卡顶沿换成一条彩虹高光；不新增行高，截图裁边不受影响 */
.lp.egg::before{height:3px;opacity:1;
  background:linear-gradient(90deg,#FF5D73,#FFD84A,#63E6A0,#4FD8E8,#5C9CFF,#B981FF)}
.lp.egg{border-color:#3B4A60;box-shadow:0 0 38px -20px rgba(140,190,255,.85)}
.lp.egg .lrow .lpace{color:#F4F8FF;text-shadow:0 0 14px rgba(150,200,255,.45)}
.lp.egg .lbar i,.ivband[data-egg] .ivbar,.ivband[data-egg] .ivsb:not(.rest){
  box-shadow:0 0 12px rgba(255,255,255,.20)}
.ivband[data-egg]{border-color:#46536B;
  box-shadow:inset 0 0 40px -24px rgba(150,190,255,.8)}
.egg-sub{font-style:normal;font-weight:700;
  background:linear-gradient(90deg,#FF7A8A,#FFD84A,#5CE1A0,#5C9CFF,#B981FF);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent}
"""


def check() -> None:
    """正反例自检（`cli.py selftest` 会调）。"""
    ramp = [360.0, 352.0, 344.0, 336.0, 328.0, 320.0]
    assert is_perfect(ramp), ramp
    assert is_perfect([300.0, 300.0, 299.0, 298.0, 295.0]), "持平也算快于等于"
    assert is_perfect([360.0, 352.0, 352.6, 344.0, 336.0, 328.0]), \
        "1 s/km 内的抖动要容忍"
    assert not is_perfect(ramp[:4]), "少于 5 圈不触发"
    assert not is_perfect([360, 352, 356, 340, 330, 320]), "中间掉速即断"
    assert not is_perfect([300] * 6), "匀速跑不算渐加速"
    assert not is_perfect([300, 299, 298, 297, 296]), "首尾只快 4 s/km 以下不算"
    assert not is_perfect([360, 352, 344, 336, 328, 400]), "最后冲崩不算"

    laps = [{"idx": i + 1, "dist_m": 1000.0, "pace_s": p,
             "dur_s": p, "is_partial": False} for i, p in enumerate(ramp)]
    assert evaluate(laps) == {"kind": "laps", "sets": 6, "gain_s": 40.0,
                              "first": 360.0, "last": 320.0}
    assert evaluate(laps + [{"idx": 7, "dist_m": 80.0,
                             "pace_s": 999.0, "dur_s": 80.0,
                             "is_partial": True}])["sets"] == 6, "残圈不参与"
    assert evaluate(laps[:4]) is None

    iv = {"order": [
        {"t0": i * 200.0, "spd": 1000.0 / p, "pace_s": p,
         "rest": False} if i % 2 == 0 else
        {"t0": i * 200.0 + 100.0, "spd": 1.0, "pace_s": None,
         "rest": True}
        for i, p in enumerate(sum(([p, p] for p in ramp), []))]}
    got = evaluate(laps[:2], iv)
    assert got and got["kind"] == "interval" and got["sets"] == 6, got
    assert "完美" in badge_html() and "渐加速" in badge_html()
    assert "linear-gradient(90deg,hsl(" in rainbow_bg(0, 6)
