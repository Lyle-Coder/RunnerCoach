"""单次活动图：把一次跑步渲染成一张竖版 PNG（画幅 1160 宽，成品 1080×1920）。

版式（自上而下）
----------------
1. 抬头：活动名 / 日期时间 / 地点天气
2. 主数字：配速 + 距离/用时/爬升/步频/步幅/心率
3. 左：无底图轨迹线；右：心率五区（彩色）+ 功率五区
4. 配速 + 心率双轴曲线
5. 跑步技术（步频/步幅/触地/垂直振幅）+ 负荷
6. 分圈横条（速度用长度与颜色两重编码）

宽度一律从版心反推
------------------
页面 1160 - 左右各 36 = 1088 的版心（原 1080/1008，加宽是为让内容比例
贴近 9:16 导出画幅），卡片再扣掉边框与内边距才是内容宽度。
早前把曲线按 1000px 硬写，比实际内容宽 42px，右侧刻度就被裁到卡片外面了；
现在统一用下面的常量算，SVG 不会溢出。

关于地图合规
------------
默认只画自己的 GPS 折线，不叠任何在线底图（腾讯/高德都不接），因此不涉及
地图数据资质问题。若在 `data/amap.json` 里配了高德 Web 服务 Key，轨迹卡会
改铺一张高德静态地图当底图——那是走持证服务商的在线接口，合规；但不能裁掉
它的水印，也不能把底图扒下来当素材分发。底图只在本地生成，不随日看板上线，
因为「轨迹 + 底图」等于更精确的个人位置信息。

底图由我们自己取景，折线也自己画
--------------------------------
`basemap.fit()` 用 `location`+`zoom` 定住画幅（不让高德自动取景——它留的内边距
是黑盒，叠自己的线必然错位），`to_px()` 因此能把经纬度算成画幅内像素。底图只当
背景（叠一层 SVG 滤镜压暗降饱和），发光轨迹仍由本文件绘制，两者共用同一条
`p' = p*s + t` 变换。踩过的三个坑写在 `basemap.py` 和 `_track_on_map()` 里：
坐标系要纠偏、高德一个 zoom 是 512×2^z、变换不能写成三段串联。

将来若要叠实拍照片做底图，放在 data/photos/<activity_id>.jpg 即可，代码会
自动取用；照片是用户自己的素材，同样不涉地图底图。

注意：轨迹是个人信息（会暴露常跑路线与居住区域），默认只出本地图，不自动
发布到线上站点。
"""
from __future__ import annotations

import datetime as dt
import html
import json
import math
import os
import subprocess
from pathlib import Path

from . import brand as BR
from .activity import project
from .activity import resolve_training as _resolve_training
from .card import _find_browser
from .exportspec import probe as _export_probe
from .metrics import num
from . import privacy as PV
from . import interval as IV
from . import easteregg as EG
from . import hrzones as HRZ

# 画幅宽 1080：内容高度约 1830（桌面/手机实测一致），裁掉页面留白后
# 比例 ≈ 1.81，与 9:16 的 1.778 只差 2%，contain 后四边几乎只剩安全带。
# 曾经试过加宽到 1160 想让比例更贴近，结果内容反而变「矮胖」（1.69），
# 上下留白被撑到 4.5%——画幅不是越宽越好，要跟内容高度配。
# 版心宽度必须由这里的常量驱动（见 CSS 里的 --page-w/--trk-w/--tw-w），
# 写死旧值会让卡片撑出 body 被裁掉，看起来就像「数据溢出边框」。
W, H = 1080, 2250
_EXPORT_PROBE = _export_probe()   # 手机端导出探针，桌面无头截图不调用

# ---- 几何常量：卡片内容宽度 = 版心 - 边框(2) - 左右内边距 ------------------
PAD = 36                       # 页面左右留白
INNER = W - PAD * 2            # 1008
GAP = 16                       # 卡片间距
TRK_W = 468                    # 轨迹卡外宽（占版心 46.4%）
ZC_W = INNER - TRK_W - GAP     # 分区卡外宽
TRK_CW = TRK_W - 2 - 22 * 2    # 轨迹卡内容宽
ZC_CW = ZC_W - 2 - 24 * 2      # 分区卡内容宽
CH_CW = INNER - 2 - 24 * 2     # 曲线卡内容宽
TRK_CH = 225                   # 轨迹画面高度（由分区卡内容反推，见 CSS .mid）
MID_H = 310                    # 轨迹 / 分区 两卡同高（去掉功率条后可以矮一截）
# 跑步技术 / 负荷 两卡：技术卡固定 1.18 份，负荷卡吃掉剩下的 1 份。
# 两卡总宽必须等于版心，否则右缘会比上面的卡片短一截（曾多减一个 GAP）。
TECH_W = INNER
TW_W = round(TECH_W * 1.18 / 2.18)
LD_W = TECH_W - TW_W
TW_CW = TW_W - 2 - 48          # 技术卡内容宽
LD_CW = LD_W - 2 - 48          # 负荷卡内容宽
TECH_H = 322                   # 两卡同高
SPK_H = 140                    # 步频 / 步幅走势高度（原 96，去掉区间条后加高）

BG = "#0F1319"
CARD = "#121E2C"
LINE = "#23364A"
TEXT = "#EDF1F5"
MUTED = "#8FA4BF"

# 五区配色：由冷到热，与表盘/常见运动 App 的语义一致
ZONE_COLORS = ["#5FA8E0", "#4FD6A6", "#E8C547", "#E8843A", "#E06A5F"]
# 曲线与坐标轴同色：主指标线跟随运动主题色（跑步青/骑行橙…），心率恒为橙，
# 不用读图例也知道哪条对应哪根轴
AXIS_HR = "#EE9A55"
# Z1–Z5 的中文副名统一走 hrzones：时长是佳明按账号里的心率区间算的，
# 名称也必须用佳明 Connect 那一套（config.json 的 hr_zone_names 可覆盖）。
# 旧版这里写死 E/M/I/T/R 训练强度名「恢复/有氧/节奏/阈值/无氧」，与 Z1–Z5
# 边界不重合，会和佳明 App 里的名称对不上。
ZONE_NAMES = list(HRZ.ZONE_LABELS)

# ---- 运动类型主题 -----------------------------------------------------------
# 一张活动图不该只有跑步一套皮。这里按 activityType.typeKey 给出：
#   tag  右上角标记的文案
#   acc  主色（轨迹折线、主数字渐变、配速/速度曲线、走势线）
#   acc2 辅色（渐变末端，让轨迹带一点方向感）
#   kind 主指标口径 pace=配速/km、speed=时速、pace100=配速/100m、duration=时长
# 配色按项目直觉分家：跑步青、骑行星橙、游泳蔚蓝、徒步草绿、力量紫。
# 0.6.9 把兜底色、分区首尾色、卡片/边框的灰黑换成品牌蓝青系，运动主色
# 仍按项目保留语义，只让「底」和 icon 的深空蓝 + 科技蓝协调。
SPORTS: dict[str, tuple[str, str, str, str]] = {
    "running": ("跑步", "#5CE1D0", "#3FA9F5", "pace"),
    "trail_running": ("越野跑", "#A8E063", "#4E9F3D", "pace"),
    "treadmill_running": ("跑步机", "#5CE1D0", "#3FA9F5", "pace"),
    "track_running": ("跑道跑", "#5CE1D0", "#3FA9F5", "pace"),
    "virtual_run": ("虚拟跑", "#5CE1D0", "#3FA9F5", "pace"),
    "cycling": ("骑行", "#FFC46B", "#F2794E", "speed"),
    "road_biking": ("公路骑行", "#FFC46B", "#F2794E", "speed"),
    "mountain_biking": ("山地骑行", "#C9E36B", "#4E9F3D", "speed"),
    "gravel_cycling": ("碎石骑行", "#D9CB7A", "#8E7A3A", "speed"),
    "indoor_cycling": ("室内骑行", "#FFB35C", "#E86A3C", "speed"),
    "swimming": ("游泳", "#7CD3FF", "#3B7BE0", "pace100"),
    "lap_swimming": ("泳池游泳", "#7CD3FF", "#3B7BE0", "pace100"),
    "open_water_swimming": ("公开水域", "#6FC3F0", "#2E6FD6", "pace100"),
    "hiking": ("徒步", "#9FE08A", "#3E9E63", "pace"),
    "walking": ("健走", "#9BE7C4", "#39A98A", "pace"),
    "strength_training": ("力量", "#C2A8FF", "#7C5CF6", "duration"),
    "indoor_cardio": ("室内有氧", "#C2A8FF", "#7C5CF6", "duration"),
    "yoga": ("瑜伽", "#F2A8D8", "#B85CF6", "duration"),
    "rowing": ("划船", "#7CD3FF", "#3B7BE0", "speed"),
    "indoor_rowing": ("划船机", "#7CD3FF", "#3B7BE0", "speed"),
    "skiing": ("滑雪", "#AFD7FF", "#4E7FBF", "speed"),
    "other": ("运动", "#9FB3C8", "#5B7A99", "pace"),
}
SPORT_FALLBACK = ("运动", "#5FA8E0", "#3B82F6", "pace")


def theme_for(activity_type: str | None) -> dict:
    """按运动类型取主题，顺带算好标签底/边的三档色阶（同一个主色压暗得来）。"""
    key = (activity_type or "").strip().lower()
    tag, acc, acc2, kind = SPORTS.get(key, SPORT_FALLBACK)
    return {
        "key": key or "other",
        "tag": tag, "acc": acc, "acc2": acc2, "kind": kind,
        "tag_bg": _mix(acc, BG, 0.86),      # 深底带一点主色
        "tag_bd": _mix(acc, BG, 0.52),      # 描边更亮一档
        "grad2": _mix(acc2, "#FFFFFF", 0.55),  # 主数字渐变的末端（跑步≈#9FD8FF，与旧版一致）
        "is_run": kind == "pace" and key in (
            "running", "trail_running", "treadmill_running", "track_running", "virtual_run"),
    }


def kmh(sec_per_km) -> float | None:
    """配速（秒/公里）→ 时速（km/h），骑行、划船这类不适合说「配速」。"""
    if not sec_per_km or sec_per_km <= 0:
        return None
    return 3600.0 / sec_per_km


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def pace_txt(sec, dash: str = "—") -> str:
    if not sec or sec <= 0:
        return dash
    sec = round(sec)
    return f"{sec // 60}'{sec % 60:02d}\""


def dur_txt(sec, zero: str = "—") -> str:
    if sec is None:
        return zero
    if not sec and zero != "0:00":
        return zero
    sec = round(sec)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _hcell(v: str, u: str, wd: int = 0) -> str:
    """hero 右侧的一格。wd=1 表示值是文字（如「主要益处」），字号要降一档。"""
    cls = "st wd" if wd else "st"
    return f'<div class="{cls}"><b>{v}</b><span>{u}</span></div>'


WIND_CN = {
    "N": "北风", "NNE": "北东北风", "NE": "东北风", "ENE": "东东北风",
    "E": "东风", "ESE": "东东南风", "SE": "东南风", "SSE": "南东南风",
    "S": "南风", "SSW": "南西南风", "SW": "西南风", "WSW": "西西南风",
    "W": "西风", "WNW": "西西北风", "NW": "西北风", "NNW": "北西北风",
    "CALM": "静风",
}

WEATHER_CN = {
    "Clear": "晴", "Cloudy": "多云", "Partly Cloudy": "局部多云", "Mostly Cloudy": "多云",
    "Rain": "雨", "Light Rain": "小雨", "Heavy Rain": "大雨", "Snow": "雪",
    "Fog": "雾", "Haze": "霾", "Overcast": "阴", "Windy": "大风", "Thunderstorms": "雷雨",
}

# 主要益处：Garmin 的 trainingEffectLabel（Connect 里这一格就叫「主要益处」）。
# 只留 2~4 字，hero 右侧的格子窄，长词组会把整列撑开。
BENEFIT = {
    "RECOVERY": "恢复",
    "AEROBIC_BASE": "有氧基础",
    "TEMPO": "节奏",
    "LACTATE_THRESHOLD": "乳酸阈值",
    "VO2MAX": "最大摄氧",
    "ANAEROBIC_CAPACITY": "无氧能力",
    "SPEED": "速度",
    "MAINTAIN": "维持",
}


def benefit(raw: dict) -> str:
    """标签缺失（Garmin 自己给 UNKNOWN）时按有氧 TE 分级兜底。

    留「—」等于白占一格；TE 分级至少还能说明这次练到什么程度，
    而且是可解释的量化口径（<1 基本没效果、2~3 维持、3~4 提升）。
    """
    lab = str(raw.get("trainingEffectLabel") or "").upper()
    if lab in BENEFIT:
        return BENEFIT[lab]
    te = num(raw.get("aerobicTrainingEffect")) or 0.0
    for cut, txt in ((4.0, "大幅提升"), (3.0, "提升"), (2.0, "维持"), (1.0, "轻微")):
        if te >= cut:
            return txt
    return "热身"


def _pace_series(run: list, win: int = 8) -> list:
    """滚动窗口配速（秒/公里）：窗口内的耗时 ÷ 窗口内的位移。"""
    out = []
    for i, p in enumerate(run):
        j = max(0, i - win)
        dt = (p.get("t") or 0) - (run[j].get("t") or 0)
        dd = (p.get("dist") or 0) - (run[j].get("dist") or 0)
        # 位移太小（原地/GPS 打点漂移）就不给值，宁可断线也不画假数据
        out.append(dt / (dd / 1000.0) if dt > 2 and dd > 8 else None)
    return out


def _median_filter(vals: list, window: int = 3) -> list:
    """中值滤波：干掉单点跳变，保留真实起伏。"""
    if window <= 1 or len(vals) < window:
        return vals
    half = window // 2
    out = []
    for i in range(len(vals)):
        if vals[i] is None:
            out.append(None)
            continue
        seg = [v for v in vals[max(0, i - half): i + half + 1] if v is not None]
        out.append(sorted(seg)[len(seg) // 2] if seg else None)
    return out


def _smooth(vals: list, window: int = 7) -> list:
    if window <= 1 or len(vals) < window:
        return vals
    out, half = [], window // 2
    for i in range(len(vals)):
        lo, hi = max(0, i - half), min(len(vals), i + half + 1)
        seg = [v for v in vals[lo:hi] if v is not None]
        out.append(sum(seg) / len(seg) if seg else None)
    return out


# ------------------------------------------------------------------ 轨迹
# 底图滤镜：高德返回的是浅底图，直接铺会跟深色看板打架。压饱和 + 压亮 +
# 略微提对比，让它退成「背景」，轨迹线才是主角。特意不做反相——反相会把
# 高德的水印一起改掉，那属于修改地图内容。
#
# 注意别用 CSS `filter:grayscale()...` 写 style：无头 Chrome 对 SVG <image>
# 的 CSS filter 函数串不生效（实测底图原样透出来，亮得跟看板打架），得用
# feColorMatrix + feComponentTransfer 这套原生原语。
MAP_BRIGHT = 0.46        # 整体压暗（越小白底图越暗）
MAP_CONTRAST = 1.18      # 提对比，免得压暗后道路全糊成一片
MAP_SAT = 0.22           # 去饱和，青绿路网褪成冷灰


def _map_filter() -> str:
    """把 brightness / contrast / saturate 换算成 fe 原语。

    CSS 的 brightness(b) 是乘 b，contrast(c) 是 (x-0.5)*c+0.5，串起来就是
    线性变换 slope=b*c、intercept=0.5*(1-c)，正好对应 feFuncX 的 linear。
    """
    slope = MAP_BRIGHT * MAP_CONTRAST
    bias = 0.5 * (1 - MAP_CONTRAST)
    funcs = "".join(
        f'<feFunc{c} type="linear" slope="{slope:.3f}" intercept="{bias:.3f}"/>'
        for c in "RGBA"[:3])
    return (f'<filter id="mapdim" color-interpolation-filters="sRGB">'
            f'<feColorMatrix type="saturate" values="{MAP_SAT}"/>'
            f'<feComponentTransfer>{funcs}</feComponentTransfer></filter>')


PIN_R = 5.0      # 起终点标记半径：绿圆点用 r，红方块用边长 2r，两者视觉尺寸一致


def _pins(pts: list, r: float = PIN_R) -> str:
    """起点绿圆点 / 终点红方块，**同一个尺寸**。

    早前为了处理「起终点几乎重合」，把绿点放大到 1.9 倍垫在下面，结果两个标记
    一大一小，看着像画错。现在统一：远就各画各的；真重合（绕圈跑，差距不到
    4px）时把两个标记叠成同心的一套——绿点在里、红方块只描边罩在外面，仍然
    是同一档尺寸，起终点都认得出。
    """
    sx, sy = pts[0]
    ex, ey = pts[-1]
    side = r * 2

    def disc(x: float, y: float, c: str) -> str:
        return (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{c}" '
                f'stroke="#0F1319" stroke-width="1.8"/>')

    def block(x: float, y: float, c: str, size: float, rx: float = 1.0) -> str:
        hf = size / 2
        return (f'<rect x="{x - hf:.1f}" y="{y - hf:.1f}" width="{size}" height="{size}" '
                f'rx="{rx}" fill="{c}" stroke="#0F1319" stroke-width="1.8"/>')

    dist = math.hypot(ex - sx, ey - sy)
    # 距离小于一个标记的宽度就并成一个：红方块里嵌一颗绿点。硬画两个的话红方块
    # 会啃掉半个绿点，看着像画重了；并起来正好表达「起终点是同一个地方」。
    if dist < side + 1.5:
        mx, my = (sx + ex) / 2, (sy + ey) / 2
        box = side + 2
        hf = box / 2
        return (f'<rect x="{mx - hf:.1f}" y="{my - hf:.1f}" width="{box}" height="{box}" '
                f'rx="1.5" fill="#E05555" stroke="#0F1319" stroke-width="1.8"/>'
                f'<circle cx="{mx:.1f}" cy="{my:.1f}" r="{r * 0.62:.2f}" fill="#4FD6A6" '
                f'stroke="#0F1319" stroke-width="1"/>')
    return disc(sx, sy, "#4FD6A6") + block(ex, ey, "#E05555", side)


def _track_on_map(track: list, w: int, h: int, bm: dict, theme: dict | None = None) -> str:
    """底图版轨迹：底图当背景，折线仍由我们画。

    取景是我们自己用 location+zoom 定的（见 basemap.fit），并且那张图是按
    「回缩后的画幅」取的（meta 的 w/h 是**图片**尺寸，不是卡片画幅），所以
    这里只有一个变换 p' = p*s + t：s 取 fit 定下的回缩比例（永远 ≤1，底图
    只缩不放），t 把轨迹外框的中心对到画幅中心，再夹住保证图四边盖满。
    """
    from . import basemap as B

    th = theme or theme_for(None)
    meta = bm["meta"]
    iw, ih = meta["w"], meta["h"]
    s = meta.get("s") or min(w / iw, h / ih)

    pts0 = [B.to_px(p[0], p[1], meta) for p in track]
    xs = [p[0] for p in pts0]
    ys = [p[1] for p in pts0]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2

    # 统一用 p' = p*s + t 这一种变换，轨迹点和底图共用，绝不会两套算法打架。
    # t 先按「bbox 中心对到画幅中心」算，再夹住，保证底图四边始终盖满画幅——
    # 早前用 translate/scale/translate 三段串联，缩放锚点落在图像角上，
    # 结果底图只覆盖画幅一角，四周露出卡片底色。
    tx = min(0.0, max(w - iw * s, w / 2 - cx * s))
    ty = min(0.0, max(h - ih * s, h / 2 - cy * s))
    pts = [(x * s + tx, y * s + ty) for x, y in pts0]
    tf = f"translate({tx:.2f},{ty:.2f}) scale({s:.4f})"
    d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    return f"""
<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <clipPath id="trkclip"><rect x="0" y="0" width="{w}" height="{h}" rx="10"/></clipPath>
    <linearGradient id="mapVeil" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#101A26" stop-opacity="0.44"/>
      <stop offset="55%" stop-color="#101A26" stop-opacity="0.22"/>
      <stop offset="100%" stop-color="#101A26" stop-opacity="0.50"/>
    </linearGradient>
    <linearGradient id="mtrk" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="{th['acc']}"/><stop offset="100%" stop-color="{th['acc2']}"/>
    </linearGradient>
    <filter id="mglow" x="-30%" y="-30%" width="160%" height="160%">
      <feGaussianBlur stdDeviation="5"/>
    </filter>
    {_map_filter()}
  </defs>
  <g clip-path="url(#trkclip)">
    <g transform="{tf}">
      <image href="{esc(bm['png'])}" x="0" y="0" width="{iw}" height="{ih}"
             preserveAspectRatio="none" filter="url(#mapdim)"/>
    </g>
    <rect x="0" y="0" width="{w}" height="{h}" fill="url(#mapVeil)"/>
    <path d="{d}" fill="none" stroke="url(#mtrk)" stroke-width="9" stroke-linecap="round"
          stroke-linejoin="round" opacity="0.45" filter="url(#mglow)"/>
    <path d="{d}" fill="none" stroke="url(#mtrk)" stroke-width="3.2" stroke-linecap="round"
          stroke-linejoin="round"/>
    {_pins(pts)}
    <text x="{w - 10}" y="{h - 9}" text-anchor="end" class="src">高德地图</text>
  </g>
  <rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="10"
        fill="none" stroke="#2A3542" stroke-width="1"/>
</svg>"""


def track_svg(track: list, w: int, h: int, photo: str | None = None,
              basemap: dict | None = None, theme: dict | None = None) -> str:
    th = theme or theme_for(None)
    if basemap:
        return _track_on_map(track, w, h, basemap, th)

    pts = project(track, w, h, pad=16)
    if len(pts) < 2:
        return f'<div class="empty" style="width:{w}px;height:{h}px">无轨迹数据</div>'
    d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    bg = ""
    if photo:
        bg = (f'<image href="{esc(photo)}" x="0" y="0" width="{w}" height="{h}" '
              f'preserveAspectRatio="xMidYMid slice" opacity="0.55"/>'
              f'<rect x="0" y="0" width="{w}" height="{h}" fill="url(#photoVeil)"/>')
    return f"""
<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="trk" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="{th['acc']}"/><stop offset="100%" stop-color="{th['acc2']}"/>
    </linearGradient>
    <radialGradient id="photoVeil" cx="50%" cy="50%" r="75%">
      <stop offset="0%" stop-color="#0F1319" stop-opacity="0.35"/>
      <stop offset="100%" stop-color="#0F1319" stop-opacity="0.88"/>
    </radialGradient>
    <filter id="glow" x="-30%" y="-30%" width="160%" height="160%">
      <feGaussianBlur stdDeviation="7"/>
    </filter>
  </defs>
  {bg}
  <path d="{d}" fill="none" stroke="url(#trk)" stroke-width="12" stroke-linecap="round"
        stroke-linejoin="round" opacity="0.28" filter="url(#glow)"/>
  <path d="{d}" fill="none" stroke="url(#trk)" stroke-width="3.4" stroke-linecap="round"
        stroke-linejoin="round"/>
  {_pins(pts)}
</svg>"""


# ------------------------------------------------------------------ 曲线
def chart_svg(points: list, w: int, h: int, theme: dict | None = None) -> str:
    th = theme or theme_for(None)
    # 纵向刻度口径跟着运动走：跑步标配速，骑行/划船标时速
    if th["kind"] == "speed":
        vlab = lambda sec: (f"{kmh(sec):.1f}" if kmh(sec) else "—")
    else:
        vlab = pace_txt
    run = [p for p in points if p.get("t")]
    if len(run) < 8:
        return f'<div class="empty" style="width:{w}px;height:{h}px">无足够时序数据</div>'
    t0, t1 = run[0]["t"], run[-1]["t"]
    span = (t1 - t0) or 1
    # 配速用「一段距离 / 一段时间」算，而不是 1000/瞬时速度：
    # GPS 速度抖动会把瞬时配速放大成假的尖峰，滚动窗口能把它抹平。
    paces = (_median_filter([1000 / p["spd"] if p.get("spd") else None for p in run], 3)
             if run[0].get("source") == "coros" else _median_filter(_pace_series(run, win=8), 3))
    hrs = _smooth([p.get("hr") for p in run], 11)
    valid_p = [p for p in paces if p]
    if not valid_p:
        return f'<div class="empty" style="width:{w}px;height:{h}px">无配速数据</div>'
    # 纵向范围取 2%~98% 分位，再各留 6% 余量：既不压扁，也不会把超出的值
    # 夹成一根根「方块」。超出的部分由 clipPath 裁掉。
    sv = sorted(valid_p)
    p_lo = sv[int(len(sv) * 0.02)]          # 最快
    p_hi = sv[min(len(sv) - 1, int(len(sv) * 0.98))]   # 最慢
    if p_hi - p_lo < 20:
        mid = (p_hi + p_lo) / 2
        p_lo, p_hi = mid - 10, mid + 10
    pad = (p_hi - p_lo) * 0.10
    lo, hi = p_lo - pad, p_hi + pad          # lo=上边界(快) hi=下边界(慢)
    hr_valid = [v for v in hrs if v]
    if hr_valid:
        hs = sorted(hr_valid)
        hr_lo = hs[int(len(hs) * 0.02)] - 4
        hr_hi = hs[min(len(hs) - 1, int(len(hs) * 0.98))] + 4
        if hr_hi - hr_lo < 12:
            mid = (hr_hi + hr_lo) / 2
            hr_lo, hr_hi = mid - 6, mid + 6
    else:
        hr_lo, hr_hi = 0, 1

    top_pad, bot_pad = 8, 30
    plot_h = h - top_pad - bot_pad
    # 配速与心率两根刻度都放左侧：上泳道给配速、下泳道给心率，互不重叠，
    # 视线来回横跳的距离也短了一截。右侧只留一点出血量，曲线不贴卡片边。
    ax_l, ax_r = 62, 12
    x0 = ax_l
    pw = w - ax_l - ax_r
    # 上下两条泳道：上配速、下心率。叠在一张图里双轴会互相干扰，分开读更清楚。
    p_top, p_h = top_pad, plot_h * 0.54
    # 心率泳道整体上提一点：最低那档刻度别和底部时间轴挤在一起
    h_top, h_h = top_pad + plot_h * 0.64, plot_h * 0.32
    xs, ys, hy = [], [], []
    for p, pc, hr in zip(run, paces, hrs):
        x = x0 + (p["t"] - t0) / span * pw
        xs.append(x)
        # 配速数字越小越快，所以快在上、慢在下。超出分位范围的视为停顿，断开。
        if pc and lo <= pc <= hi:
            k = (pc - lo) / (hi - lo or 1)
            ys.append(p_top + k * p_h)
        else:
            ys.append(None)
        if hr:
            k = (hr_hi - hr) / (hr_hi - hr_lo or 1)
            hy.append(h_top + k * h_h)
        else:
            hy.append(None)

    seg_p = _path(xs, ys)
    seg_h = _path(xs, hy)
    area = _area(xs, ys, p_top + p_h)
    ticks = []
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        lbl = dur_txt(span * frac, "0:00")
        x = x0 + pw * frac
        anchor = "start" if frac == 0 else ("end" if frac == 1 else "middle")
        ticks.append(f'<text x="{x:.0f}" y="{h - 6}" class="axis" text-anchor="{anchor}">{lbl}</text>')
        if 0 < frac < 1:
            ticks.append(f'<line x1="{x:.0f}" y1="{top_pad}" x2="{x:.0f}" y2="{top_pad + plot_h}" '
                         f'stroke="{LINE}" stroke-width="1" stroke-dasharray="3 5"/>')

    # 纵向刻度：三档 + 淡横线，两根轴都在左边，靠颜色区分归属（蓝=配速 橙=心率）
    guides = []
    for pace in (p_lo, (p_lo + p_hi) / 2, p_hi):
        y = p_top + (pace - lo) / (hi - lo or 1) * p_h
        guides.append(f'<line x1="{x0}" y1="{y:.1f}" x2="{x0 + pw}" y2="{y:.1f}" '
                      f'stroke="{LINE}" stroke-width="1" stroke-dasharray="2 6"/>')
        guides.append(f'<text x="{x0 - 12:.0f}" y="{y + 5:.1f}" class="axl" text-anchor="end" '
                      f'fill="{th["acc"]}">{vlab(pace)}</text>')
    if hr_valid:
        for val in (hr_hi, (hr_hi + hr_lo) / 2, hr_lo):
            y = h_top + (hr_hi - val) / (hr_hi - hr_lo or 1) * h_h
            guides.append(f'<line x1="{x0}" y1="{y:.1f}" x2="{x0 + pw}" y2="{y:.1f}" '
                          f'stroke="{LINE}" stroke-width="1" stroke-dasharray="2 6"/>')
            guides.append(f'<text x="{x0 - 12:.0f}" y="{y + 5:.1f}" class="axl" '
                          f'text-anchor="end" fill="{AXIS_HR}">{val:.0f}</text>')
    return f"""
<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="pfill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{th['acc']}" stop-opacity="0.22"/>
      <stop offset="100%" stop-color="{th['acc']}" stop-opacity="0.02"/>
    </linearGradient>
    <clipPath id="plotclip"><rect x="{x0}" y="{top_pad}" width="{pw}" height="{plot_h + 1}"/></clipPath>
  </defs>
  {''.join(guides)}
  {''.join(ticks)}
  <line x1="{x0}" y1="{top_pad + plot_h * 0.60:.1f}" x2="{x0 + pw}" y2="{top_pad + plot_h * 0.60:.1f}"
        stroke="{LINE}" stroke-width="1"/>
  <g clip-path="url(#plotclip)">
  <path d="{area}" fill="url(#pfill)"/>
  <path d="{seg_p}" fill="none" stroke="{th['acc']}" stroke-width="2.2" stroke-linejoin="round"/>
  <path d="{seg_h}" fill="none" stroke="#E8843A" stroke-width="1.9" stroke-linejoin="round"
        opacity="0.95"/>
  </g>
</svg>"""


def _path(xs: list, ys: list) -> str:
    parts, pen = [], False
    for x, y in zip(xs, ys):
        if y is None:
            pen = False
            continue
        parts.append(("L" if pen else "M") + f"{x:.1f},{y:.1f}")
        pen = True
    return " ".join(parts)


def _area(xs: list, ys: list, base: float) -> str:
    """按「连续段」分别填充，避免停顿处被连成一块假的面积。"""
    segs, cur = [], []
    for x, y in zip(xs, ys):
        if y is None:
            if len(cur) > 1:
                segs.append(cur)
            cur = []
        else:
            cur.append((x, y))
    if len(cur) > 1:
        segs.append(cur)
    parts = []
    for seg in segs:
        d = (f"M {seg[0][0]:.1f},{base:.1f} L "
             + " L ".join(f"{x:.1f},{y:.1f}" for x, y in seg)
             + f" L {seg[-1][0]:.1f},{base:.1f} Z")
        parts.append(d)
    return " ".join(parts)


# ------------------------------------------------------------------ 分区
def zone_times(raw: dict, prefix: str) -> list[float]:
    key = f"{prefix}TimeInZone_"
    return [num(raw.get(key + str(i))) or 0.0 for i in range(1, 6)]


def zone_block(times: list[float], title: str, unit_label: str = "", *,
               coros: bool = False, descriptions: list[str] | None = None) -> str:
    """心率分区条。

    `times` 是佳明 hrTimeInZone_1..5（或高驰自己的分区时长），`descriptions`
    缺省时从 hrzones 取佳明 Connect 的 Z1–Z5 名称。高驰是另一套六区口径，
    一律不显示中文副名，免得把佳明的名字贴到高驰数据上。
    """
    if not sum(times):
        return (f'<div class="zhead">{esc(title)}<span>{esc(unit_label)}</span></div>'
                f'<div class="zempty">本次未记录分区时长</div>')
    total = sum(times)
    colors = (["#778899"] + list(ZONE_COLORS)) if coros else ZONE_COLORS
    names = [f"Z{i + 1}" for i in range(len(times))] if coros else ZONE_NAMES
    if coros:
        descriptions = ["恢复", "有氧耐力", "有氧动力", "阈值", "无氧耐力", "无氧动力"]
    else:
        src = descriptions if descriptions is not None else HRZ.zone_descriptions()
        descriptions = (list(src) + [""] * len(times))[:len(times)]
    bar = []
    for i, t in enumerate(times):
        pct = t / total * 100
        if pct < 0.4:
            continue
        bar.append(f'<i style="width:{pct:.2f}%;background:{colors[i]}"></i>')
    rows = []
    for i, t in enumerate(times):
        pct = t / total * 100
        rows.append(
            f'<div class="zrow">'
            f'<span class="zdot" style="background:{colors[i]}"></span>'
            f'<span class="zname">{names[i]}<em>{esc(descriptions[i])}</em></span>'
            f'<span class="zbar"><i style="width:{min(100, pct):.1f}%;background:{colors[i]}"></i></span>'
            f'<span class="zval">{dur_txt(t, "0:00")}</span>'
            f'<span class="zpct" style="color:{colors[i]}">{pct:.0f}%</span>'
            f'</div>')
    return f"""
<div class="zblock{' zcoros' if coros else ''}">
  <div class="zhead">{esc(title)}<span>{esc(unit_label)}</span></div>
  <div class="zbar-stack">{''.join(bar)}</div>
  {''.join(rows)}
</div>"""


def _runs(flags: list[bool], limit: int = 26) -> list[int]:
    """把连续的 True 合并成一段，返回每段中点的下标。

    逐个采样点点橙点的话，600 点里有 250 个被修正过，整条线都会被糊住；
    按「连续段」标一个点，读起来就是「这一段的数据翻转过」，清楚得多。
    """
    idx, i = [], 0
    while i < len(flags):
        if not flags[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(flags) and flags[j + 1]:
            j += 1
        idx.append((i + j) // 2)
        i = j + 1
    if len(idx) > limit:                     # 段太碎就均匀抽几个，别糊成一片
        step = len(idx) / limit
        idx = [idx[min(int(k * step), len(idx) - 1)] for k in range(limit)]
    return idx


def spark_svg(points: list, w: int, h: int, theme: dict | None = None) -> str:
    """走势小图：跑步看步频/步幅，其他运动看速度/心率。修正过的点单独标出来。"""
    th = theme or theme_for(None)
    if th["is_run"]:
        run = [p for p in points if p.get("cad") and (p.get("source") == "coros" or (p.get("spd") or 0) >= 2.2)]
        lack = "无步态时序"
    else:
        # 骑行/划船/泳池外这些没有步频，速度门槛也放低（步走/慢骑都算在动）
        run = [p for p in points if (p.get("spd") or 0) > 0.5]
        lack = "无速度时序"
    if len(run) < 10:
        return f'<div class="empty" style="width:{w}px;height:{h}px">{lack}</div>'
    # 图筛完只剩一条窄带时，600 个 1Hz 采样挤在不到 500px 里，逐点画出来全是毛刺；
    # 先中值去跳变，再按样本量自适应滑动平均，留下的才是「走势」。
    win = max(5, min(23, len(run) // 26)) | 1
    cad = _smooth(_median_filter([p["cad"] for p in run], 5), win) if th["is_run"] else []
    st = _smooth(_median_filter([p.get("stride") for p in run], 5), win) if th["is_run"] else []
    spd = [round(p["spd"] * 3.6, 1) if p.get("spd") else None for p in run]
    hrs = _smooth([p.get("hr") for p in run], win)
    fixed = [bool(p.get("fixed")) for p in run]
    marks = set(_runs(fixed))
    lab_w = 46
    plot_w = w - lab_w - 74          # 右侧留出区间标签的位置

    def band(vals, top, height, color, label, unit, cid):
        ok = sorted(v for v in vals if v)
        if not ok:
            return ""
        # 上下各留 6% 余量：只有极少数跳变点会碰到边界，不必靠 clip 硬切，
        # 线也就不会贴着框走成一截直线。
        q_lo = ok[int(len(ok) * 0.01)]
        q_hi = ok[min(len(ok) - 1, int(len(ok) * 0.99))]
        edge = (q_hi - q_lo) * 0.06 or 1
        lo, hi = q_lo - edge, q_hi + edge
        rng = (hi - lo) or 1
        box_t, box_h = top + 3, height - 6      # 线宽与圆点也要留出来
        n = len(vals) - 1 or 1
        xs = [i / n * plot_w + lab_w for i in range(len(vals))]
        ys = [box_t + min(1.0, max(0.0, (hi - v) / rng)) * box_h if v else None
              for v in vals]
        path = _path(xs, ys)
        dots = "".join(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.2" fill="#FF7A59" opacity="0.9"/>'
            for i, (x, y) in enumerate(zip(xs, ys)) if i in marks and y)
        head = (f'<text x="0" y="{top + height / 2 + 4:.0f}" class="spklab">{label}</text>'
                f'<text x="{w}" y="{top + height / 2 + 4:.0f}" class="spkval" '
                f'text-anchor="end">{lo:.0f}–{hi:.0f}{unit}</text>')
        # 超出范围的极端值已被 clamp 在框内，clip 只作为兜底
        return head + (f'<g clip-path="url(#{cid})">'
                       f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.2" '
                       f'stroke-linejoin="round" opacity="0.85"/>' + dots + '</g>')

    # 两行等高，上下各留 10px、中间隔 20px。高度不再写死比例，卡片给多少
    # 就按多少摊开——区间条删掉以后多出来的高度全给走势，曲线不必挤成窄带。
    pad_t, gap = 10, 20
    band_h = (h - pad_t * 2 - gap) / 2
    y1 = pad_t
    y2 = pad_t + band_h + gap
    b1 = band(cad, y1, band_h, th["acc"], "步频", "", "spk1")
    b2 = band(st, y2, band_h, "#E8C547", "步幅", "", "spk2")
    if not th["is_run"]:
        b1 = band(spd, y1, band_h, th["acc"], "速度", " km/h", "spk1")
        b2 = band(hrs, y2, band_h, AXIS_HR, "心率", " bpm", "spk2")
    return f"""
<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <clipPath id="spk1"><rect x="0" y="{y1}" width="{w}" height="{band_h:.0f}"/></clipPath>
    <clipPath id="spk2"><rect x="0" y="{y2:.0f}" width="{w}" height="{band_h:.0f}"/></clipPath>
  </defs>
  {b1}{b2}
</svg>"""


# ------------------------------------------------------------------ 分圈
def group_laps(laps: list[dict], max_rows: int = 10) -> list[dict]:
    """圈数过多时按整数圈合并，避免 42km 的 42 行把版面撑爆。"""
    real = [l for l in laps if (l.get("pace_s") or 0) > 0]
    if len(real) <= max_rows:
        return real
    k = math.ceil(len(real) / max_rows)
    out = []
    for i in range(0, len(real), k):
        seg = real[i:i + k]
        dist = sum(l["dist_m"] for l in seg)
        dur = sum(l["dur_s"] for l in seg)
        def weighted(key):
            valid = [l for l in seg if l.get(key) is not None and l["dur_s"] > 0]
            return (sum(l[key] * l["dur_s"] for l in valid) /
                    sum(l["dur_s"] for l in valid)) if valid else None
        out.append({
            "idx": seg[0]["idx"] if len(seg) == 1 else f"{seg[0]['idx']}-{seg[-1]['idx']}",
            "dist_m": dist, "dur_s": dur,
            "pace_s": dur / (dist / 1000) if dist > 50 else None,
            "hr_avg": weighted("hr_avg"),
            "cad": weighted("cad"),
            "is_partial": False,
        })
    return out


def lap_rows(laps: list[dict], theme: dict | None = None,
             egg: dict | None = None) -> str:
    """分圈表。`egg` 命中时把每根条换成连续彩虹（判定见 easteregg.py）。"""
    th = theme or theme_for(None)
    real = group_laps(laps)
    if not real:
        return '<div class="empty">无分圈数据</div>'
    paces = [l["pace_s"] for l in real]
    fast, slow = min(paces), max(paces)
    total_dist = sum(l["dist_m"] for l in real)
    avg = sum(l["dur_s"] for l in real) * 1000 / total_dist if total_dist else None
    span = (slow - fast) or 1
    out = []
    for i, l in enumerate(real):
        k = (l["pace_s"] - fast) / span           # 0=最快 1=最慢
        # 快 = 短条 + 冷色；慢 = 长条 + 暖色
        width = 22 + k * 78
        # 彩蛋：不再按快慢配色，按「第几圈」沿色带走，越到后段越偏紫蓝。
        # 判定用的是原始圈（group_laps 合并过的行不再重判），所以超过 10 圈的
        # 活动合并成几行后，彩虹依然落在同一张表里。
        color = EG.rainbow_bg(i, len(real)) if egg else _mix("#4FD6A6", "#E8843A", k)
        dist = f"{l['dist_m'] / 1000:.2f}"
        cad = f"{l['cad']:.0f}" if l.get("cad") else "—"
        hr = f"{l['hr_avg']:.0f}" if l.get("hr_avg") else "—"
        main = (f"{kmh(l['pace_s']):.1f}" if th["kind"] == "speed"
                else pace_txt(l["pace_s"]))
        out.append(
            f'<div class="lrow{" partial" if l.get("is_partial") else ""}">'
            f'<span class="lid">{l["idx"]}</span>'
            f'<span class="ldist">{dist} km</span>'
            f'<span class="lbar"><i style="width:{width:.1f}%;background:{color}"></i></span>'
            f'<span class="lpace">{main}</span>'
            f'<span class="lhr">{hr}</span>'
            f'<span class="lcad">{cad}</span>'
            f'</div>')
    if th["kind"] == "speed":
        head = ('<div class="lhead"><span class="lid">段</span><span class="ldist">距离</span>'
                '<span class="lbar"></span><span class="lpace">速度</span>'
                '<span class="lhr">心率</span><span class="lcad">踏频</span></div>')
        note = (f'平均 {kmh(avg):.1f}｜最快 {kmh(fast):.1f}｜最慢 {kmh(slow):.1f} km/h')
    else:
        head = ('<div class="lhead"><span class="lid">圈</span><span class="ldist">距离</span>'
                '<span class="lbar"></span><span class="lpace">配速</span>'
                '<span class="lhr">心率</span><span class="lcad">步频</span></div>')
        note = f'分段平均 {pace_txt(avg)}｜最快段 {pace_txt(fast)}｜最慢段 {pace_txt(slow)}'
    return head + ''.join(out) + f'<div class="lnote">{note}</div>'


# 强度带的配色，按「快 → 慢」取；恢复档单独一色（比慢档更灰）。
BAND_COLORS = ("#5CE1D0", "#5FA8E0", "#E8B44A", "#C4753F")
REST_COLOR = "#3C4956"
# 柱下逐组标注（配速/时间/距离）最多支持多少组：1080 宽下每组约 60px，
# 再多三行数字就挤不下，退化为纯结构条 + 逐档汇总。
MAX_LABEL_COLS = 16


def _reps_of(order: list[dict]) -> list[dict]:
    """把时序段折成「组」：一跑 + 紧随其后的恢复算一组（无恢复时一段一组）。

    结构条要给每根柱标注配速/时间/距离，柱子必须是「组」而不是「段」——
    15×400 m 是 30 段（快+休息各 15），按段标注会挤成 32px 一列、数字全被截。
    """
    reps: list[dict] = []
    for o in order:
        if o.get("rest"):
            if reps and reps[-1].get("rest") is None:
                reps[-1]["rest"] = o
            elif reps:
                r = reps[-1]["rest"]
                r["dur_s"] = (r.get("dur_s") or 0) + (o.get("dur_s") or 0)
                r["dist_m"] = (r.get("dist_m") or 0) + (o.get("dist_m") or 0)
            else:
                reps.append({"seg": None, "rest": o})
        else:
            reps.append({"seg": o, "rest": None})
    return reps


def interval_bands(iv: dict, theme: dict | None = None,
                   egg: dict | None = None) -> str:
    """间歇版式（2~4 档通用）：上结构条，下逐档汇总。

    为什么统一成这一种
    ------------------
    「快慢双栏」只是 N=2 的特例，而真实训练里三段课（400 快 + 200 中 +
    200 慢）、金字塔、变速跑都常见，中段塞进哪一栏都是错的。这里统一按
    **时间顺序**画柱，柱数 = 组数：柱高表示相对速度、颜色表示强度档，
    组间恢复是贴底的细灰条。两段课因此也走这一版式，只是只有两档颜色。

    每根柱下面标三行（配速 / 时间 / 距离）——跑者复盘时看的就是这三个数。
    组数太多（>16）时空间不够逐组标注，退化为纯结构条 + 逐档汇总。
    """
    th = theme or theme_for(None)
    speed = th["kind"] == "speed"
    bands = iv.get("bands") or []
    order = iv.get("order") or []
    reps = _reps_of(order)

    def main(pace: float | None) -> str:
        if not pace:
            return "—"
        return f"{kmh(pace):.1f}" if speed else pace_txt(pace)

    def dist_txt(m: float | None) -> str:
        m = m or 0
        return f"{m / 1000:.2f} km" if m >= 1000 else f"{m:.0f} m"

    spds = [r["seg"]["spd"] for r in reps
            if r.get("seg") and not r["seg"].get("rest")]
    lo, hi = (min(spds), max(spds)) if spds else (0.0, 1.0)
    span = hi - lo

    def bar_h(spd: float) -> float:
        k = 0.55 if span < 1e-6 else (spd - lo) / span
        return 26 + k * 74           # 26%~100%，快的柱更高

    labeled = 1 < len(reps) <= MAX_LABEL_COLS
    if labeled:
        cols: list[str] = []
        # 彩蛋按「第几组强度」沿色带走，恢复柱和空格不计入序号
        work_total = len([r for r in reps if r.get("seg")])
        wi = 0
        for r in reps:
            seg, rest = r.get("seg"), r.get("rest")
            if seg:
                color = (EG.rainbow_bg(wi, work_total) if egg else
                         BAND_COLORS[min(seg.get("band") or 0,
                                         len(BAND_COLORS) - 1)])
                wi += 1
                h = bar_h(seg["spd"])
                body = (f'<i class="ivbar" style="height:{h:.0f}%;'
                        f'background:{color}"></i>')
                lab = (f'<b>{main(seg.get("pace_s"))}</b>'
                       f'<span>{dur_txt(seg.get("dur_s"))}</span>'
                       f'<em>{dist_txt(seg.get("dist_m"))}</em>')
            else:
                body, lab = "", ('<b>—</b><span>—</span><em>—</em>')
            if rest:
                body += '<i class="ivrest"></i>'
            cols.append(f'<div class="ivc"><div class="ivcb">{body}</div>'
                        f'<div class="ivcl">{lab}</div></div>')
        strip_html = f'<div class="ivcols">{"".join(cols)}</div>'
    else:
        # 组数太多：按段的时长控制柱宽，只给结构形状，不逐组标注
        bars: list[str] = []
        work_total = len([o for o in order if not o.get("rest")])
        wi = 0
        for o in order:
            width = max(6.0, min(60.0, (o.get("dur_s") or 0) / 3.0))
            if o.get("rest"):
                bars.append(f'<i class="ivsb rest" style="flex:{width:.0f} 0 0;'
                            f'height:12%"></i>')
                continue
            color = (EG.rainbow_bg(wi, work_total) if egg else
                     BAND_COLORS[min(o.get("band") or 0, len(BAND_COLORS) - 1)])
            wi += 1
            bars.append(f'<i class="ivsb" style="flex:{width:.0f} 0 0;'
                        f'height:{bar_h(o["spd"]):.0f}%;background:{color}"></i>')
        strip_html = f'<div class="ivstrip">{"".join(bars)}</div>'

    rows: list[str] = []
    work_bands = len([b for b in bands if not b.get("rest")])
    bi = 0
    for i, b in enumerate(bands):
        if b.get("rest"):
            color = REST_COLOR
        elif egg:
            color = EG.rainbow_hex(bi, work_bands)
            bi += 1
        else:
            color = BAND_COLORS[min(i, len(BAND_COLORS) - 1)]
        if b.get("rest"):
            # 站桩恢复没有「配速」可言，给平均恢复时长；慢跑恢复给配速
            if b.get("jog"):
                val, val_lab = main(b.get("pace_s")), "恢复配速"
            else:
                val, val_lab = dur_txt(b.get("recover_s")), "平均恢复"
        else:
            val = main(b.get("pace_s"))
            val_lab = "平均速度" if speed else "平均配速"
        hr = f'{b["hr_avg"]:.0f}' if b.get("hr_avg") else "—"
        per = (b["dist_m"] / max(1, b["sets"])) if b.get("dist_m") else 0
        rows.append(
            '<div class="ivbr">'
            f'<i style="background:{color}"></i>'
            f'<b>{b["label"]}</b>'
            f'<span>{b["sets"]} 组 · {per:.0f} m</span>'
            f'<em>{val}<u>{val_lab}</u></em>'
            f'<em>{hr}<u>心率</u></em>'
            '</div>')
    # 彩蛋只加 data 属性、不改 class：老断言里 `class="ivband"` 是版式契约，
    # 换成 "ivband egg" 会让「间歇版式是否生效」的检查全部失效。
    egg_attr = ' data-egg="1"' if egg else ''
    return (f'<div class="ivband"{egg_attr}>'
            f'{strip_html}'
            f'<div class="ivbrows">{"".join(rows)}</div>'
            '</div>')


def _mix(c1: str, c2: str, k: float) -> str:
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(a[i] + (b[i] - a[i]) * k):02x}" for i in range(3))


# ------------------------------------------------------------------ 组装
def build_html(row: dict, detail: dict, photo: str | None = None,
               basemap: dict | None = None, title: str | None = None,
               privacy: str = PV.MODE_NONE) -> str:
    """组装活动图 HTML。`privacy` 是分享脱敏档位，默认原图，绝不改旧调用方行为。"""
    mode = PV.normalize(privacy)
    row = {**row, **(detail.get("row_metrics") or {})}
    source_label = "COROS 高驰" if detail.get("source") == "coros" else "Garmin"
    raw = row.get("raw")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    raw = raw or {}
    s = detail.get("summary") or {}
    w = detail.get("weather") or {}
    laps = detail.get("laps") or []
    points = ((detail.get("series") or {}).get("points")) or []
    # 脱敏在组装 HTML 之前做完：底图（含真实坐标）和照片（可能带位置）也一律不叠。
    # 出完 PNG 再打码是没用的——原始折线仍会留在 output/*.html 里。
    track, mask_info = PV.mask_track(detail.get("track") or [], mode)
    if mode != PV.MODE_NONE:
        # trim 只是裁掉首尾，底图由调用方按**裁剪后**的轨迹取景，起终点不会入图；
        # hide 档完全隐藏轨迹，底图必须一起清掉。
        if mode == PV.MODE_HIDE:
            basemap = None
        photo = None

    dist_km = (num(row.get("distance_m")) or 0) / 1000
    dur = num(row.get("duration_s")) or 0
    pace = (num(row.get("pace_s_km")) or (dur / dist_km if dist_km else None))
    th = theme_for(row.get("type"))
    # hide 档连精确起跑时刻也去掉：固定作息 + 日期同样能缩小常跑地点范围
    start = "" if mode == PV.MODE_HIDE else (row.get("start_ts") or "")[11:16]
    date_txt = (row.get("date") or "")
    wd = ""
    try:
        wd = "周" + "一二三四五六日"[dt.date.fromisoformat(date_txt).weekday()]
    except Exception:
        pass
    is_coros = detail.get("source") == "coros"
    hr_z = ([z["seconds"] for z in detail.get("hr_zones") or []]
            if is_coros else zone_times(raw, "hr"))
    drift = s.get("drift") or {}

    weather_txt = ""
    if w:
        bits = []
        if w.get("temp_c") is not None:
            bits.append(f"{w['temp_c']:.0f}℃")
        if w.get("humidity") is not None:
            bits.append(f"湿度 {w['humidity']:g}%")
        if w.get("text"):
            bits.append(WEATHER_CN.get(str(w["text"]), str(w["text"])))
        if w.get("wind_dir"):
            key = str(w["wind_dir"]).upper()
            name = WIND_CN.get(key, f"{key} 风")
            spd = num(w.get("wind_speed"))
            bits.append(f"{name} {spd:.0f} km/h" if spd is not None else name)
        elif num(w.get("wind_speed")) is not None:
            bits.append(f"风速 {num(w['wind_speed']):.0f} km/h")
        weather_txt = " · ".join(bits)

    te_a = f'{num(row["aerobic_te"]):.1f}' if num(row.get("aerobic_te")) is not None else "—"
    te_n = f'{num(row["anaerobic_te"]):.1f}' if num(row.get("anaerobic_te")) is not None else "—"
    # 佳明在活动列表 raw 里带这两个字段；高驰的跑步动态在明细 summary 里，
    # 由 coros_activity.dynamics() 归一成 ms / cm 后放在 gct_ms、vo_cm。
    gct = (num(s.get("gct_ms")) if is_coros else
           num(raw.get("avgGroundContactTime")) or num(s.get("gct_ms")))
    vo = (num(s.get("vo_cm")) if is_coros else
          num(raw.get("avgVerticalOscillation")) or num(s.get("vo_cm")))
    n_real = len([l for l in laps if (l.get("pace_s") or 0) > 0])
    # 间歇版式的唯一依据是佳明的训练模式标记（hasIntensityIntervals）：
    # 训练里设置了间歇才切「底部双栏」，其余一律走原来的分圈表。
    # **活动列表 raw 优先**：单活动接口不返回这个标记，明细里缓存的 training
    # 可能是「未知」甚至历史版本写死的 False（真机删数据重拉后正好是这种），
    # 只在「缺键」时回填会漏掉后一种，导致 10-21 的 15×400 m 永远默认版式。
    # 高驰没有这个标记 —— 走「手动计圈结构」判定（interval.detect_coros）。
    if is_coros:
        iv = IV.detect_coros(detail)
    else:
        detail = {**detail, "training": _resolve_training(detail, raw)}
        iv = IV.detect(detail)
    # 彩蛋：只看这一张图要画的那种「圈」——间歇卡用强度组，普通卡用分圈。
    # 判定放在渲染之前，两条路径共用同一枚徽章。
    egg = EG.evaluate(laps, iv)
    if iv:
        # 2~4 档统一走「结构条 + 逐档汇总」：双栏只是两档的特例，
        # 而两段课同样需要逐组看配速/时间/距离，所以不再单独保留双栏。
        lap_title = "间歇结构"
        bands = iv.get("bands") or []
        lap_html = interval_bands(iv, th, egg=egg)
        sets_total = sum(b["sets"] for b in bands if not b.get("rest"))
        if len(bands) > 2:
            lap_sub = f'{len(bands)} 档强度 · {sets_total} 组'
        elif any(b.get("rest") for b in bands):
            per = next((b["dist_m"] / max(1, b["sets"]) for b in bands
                        if not b.get("rest")), 0)
            rest_b = next((b for b in bands if b.get("rest")), {})
            lap_sub = (f'{sets_total} 组 × {per:.0f} m · 组间恢复 '
                       f'{dur_txt(rest_b.get("recover_s"))}')
        else:
            lap_sub = f'{sets_total} 组快慢交替 · 速度差 {iv["ratio"]:.2f}×'
    else:
        if n_real > 10:
            lap_title, lap_sub = "分段", f"每 {math.ceil(n_real / 10)} 圈合并"
        elif n_real <= 1:
            # 单圈活动（骑行代步、短距离）按「每公里」标题会显得莫名其妙
            lap_title, lap_sub = "全程", "未分段"
        else:
            lap_title, lap_sub = "每公里", f"{n_real} 圈"
        if is_coros and detail.get("lap_distance_m") != 1000 and n_real > 1:
            lap_title, lap_sub = "分圈", f"{n_real} 圈"
        lap_html = lap_rows(laps, th, egg=egg)
    if egg:
        unit = "组" if egg["kind"] == "interval" else "圈"
        lap_sub = (f'{lap_sub} · <em class="egg-sub">'
                   f'{egg["sets"]} {unit}完美渐加速</em>')
    head_cls = "head egg" if egg else "head"
    lp_cls = "card lp egg" if egg else "card lp"
    egg_badge = EG.badge_html(EG.badge_uid(row, title)) if egg else ""
    # 图上只用橙点标出被校准过的采样点，不再在页脚写「修正了多少点」——
    # 那是数据处理的中间量，看板上不需要（问起来反而要解释）。
    cad_avg = s.get("cad_avg")
    stride_avg = s.get("stride_avg")
    hr_avg = (num(row.get("avg_hr")) if is_coros else
              s.get("hr_run_avg") or num(row.get("avg_hr")))
    # 只有真的标了橙点才写图例，否则会出现「有图例没点」的尴尬
    fix_hint = "橙点为校准点" if any(p.get("fixed") for p in points) else ""
    css = _CSS

    # ---- 分享脱敏：轨迹区要么画裁过的折线，要么换成同尺寸占位块 ----------
    time_txt = (f" · {esc(start)} {esc('起跑' if th['is_run'] else '开始')}"
                if start else " · 时间已隐藏")
    trk_badge = PV.badge(mode, mask_info)
    trk_meta = trk_badge or (
        esc((basemap or {}).get("meta", {}).get("region") or "")
        if basemap else "无底图 · 自 GPS")
    if mode != PV.MODE_NONE and len(track) < 2:
        trk_body = PV.placeholder(mode, mask_info, TRK_CW, TRK_CH)
    else:
        trk_body = track_svg(track, TRK_CW, TRK_CH, photo, basemap, th)

    # ---- 主指标口径：跑步说配速，骑车说时速，游泳说每 100 米，力量课说时长 ----
    kind = th["kind"]
    if kind == "speed":
        hero_lab, hero_sub = "平均速度", "公里/小时"
        hero_val = f"{kmh(pace):.1f}" if kmh(pace) else "—"
        chart_title, chart_lg = "速度与心率", "速度"
    elif kind == "pace100":
        s100 = dur / (dist_km * 10) if dist_km else None
        hero_lab, hero_sub = "平均配速", "每 100 米"
        hero_val = pace_txt(s100)
        chart_title, chart_lg = "配速与心率", "配速"
    elif kind == "duration":
        hero_lab, hero_sub = "总时长", "时:分:秒"
        hero_val = dur_txt(dur)
        chart_title, chart_lg = "速度与心率", "速度"
    else:
        hero_lab, hero_sub = "平均配速", "每公里"
        hero_val = pace_txt(pace)
        chart_title, chart_lg = "配速与心率", "配速"

    # 高驰活动列表的 maxSpeed 多数是 0，最快速度要从明细 summary 换算后注入 row
    max_spd = num(row.get("max_speed_mps")) or (None if is_coros else num(raw.get("maxSpeed"))) or None  # m/s
    kcal = num(row.get("calories")) or 0
    asc = num(row.get("ascent")) or 0
    if th["is_run"]:
        stat2 = [(f"{cad_avg:.0f}" if cad_avg else "—", "步频 spm", 0),
                 (f"{stride_avg:.0f}" if stride_avg else "—", "步幅 cm", 0),
                 (f"{hr_avg:.0f}" if hr_avg else "—", "心率 bpm", 0)]
        tech_title = "跑步技术"
        tech_cells = [(f"{cad_avg:.0f}" if cad_avg else "—", "步频 spm"),
                      (f"{stride_avg:.0f}" if stride_avg else "—", "步幅 cm"),
                      (f"{gct:.0f}" if gct else "—", "触地"),
                      (f"{vo:.1f}" if vo else "—", "垂直振幅")]
    else:
        load_v = num(row.get("load")) or 0
        stat2 = [(f"{max_spd * 3.6:.1f}" if max_spd else "—", "最高 km/h", 0),
                 (f"{load_v:.0f}", "训练负荷", 0),
                 (f"{hr_avg:.0f}" if hr_avg else "—", "心率 bpm", 0)]
        tech_title = "速度与心率"
        tech_cells = [(f"{kmh(pace):.1f}" if kmh(pace) else "—", "平均 km/h"),
                      (f"{max_spd * 3.6:.1f}" if max_spd else "—", "最高 km/h"),
                      (f"{asc:.0f}", "爬升 m"),
                      (f"{hr_avg:.0f}" if hr_avg else "—", "心率 bpm")]
    # hero 右侧固定「上 3 下 3」：用时 / 爬升 / 主要益处，然后技术三项。
    # 距离已经提到左侧大数字，不再重复占格。
    head3 = [(dur_txt(dur), "用时", 0), (f"{asc:.0f}", "爬升 m", 0),
             ("—" if is_coros else benefit(raw), "主要益处", 1)]
    if kind == "duration":
        # 力量课这类主指标本身就是时长，右侧再来一格「用时」就是同一个数写两遍
        head3[0] = (f"{kcal:.0f}", "千卡", 0)
    stat_html = ''.join(_hcell(*c) for c in head3 + stat2)
    tech_html = ''.join(f'<div class="t"><b>{v}</b><span>{u}</span></div>' for v, u in tech_cells)
    # 主题变量必须排在 _CSS 之后：两处都写 :root，靠后的一条才生效
    css = _CSS + (f":root{{--acc:{th['acc']};--acc2:{th['acc2']};--grad2:{th['grad2']};"
                  f"--tagfg:{th['acc']};--tagbg:{th['tag_bg']};--tagbd:{th['tag_bd']};"
                  # 版心几何从常量注入，CSS 里不再写死——加宽画幅时只改一处
                  f"--page-w:{W}px;--pad:{PAD}px;--trk-w:{TRK_W}px;--tw-w:{TW_W}px}}")
    # 内容盒里的渐进与辉光跟随当前运动主色：跑步青、骑行橙、游泳蓝，
    # 品牌图形本身不变，一张图里不会出现两套互不相干的光源。
    # 注意不能改 body 背景（裁边契约见 brand.glow_overlay_css 的说明）。
    css += BR.glow_overlay_css("30px 36px 40px",
                               g1=BR.rgba(th["acc"], 0.16),
                               g2=BR.rgba(th["acc2"], 0.10))

    # 距离升成和配速同级的第二个大数字，放最左——跑者第一眼要「跑了多远 + 多快」，
    # 距离缩在右侧小格里等于没强调。室内/力量课没有距离，就只留主指标一块。
    dist_block = ""
    if dist_km >= 0.05:
        dist_block = ('<div class="hbig"><div class="hlab">距离</div>'
                      f'<div class="hval">{dist_km:.2f}</div>'
                      '<div class="hsub2">公里</div></div>')

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width={W},initial-scale=1,viewport-fit=cover">
<style>{css}</style></head>
<body><div class="rc-glow"></div>{BR.watermark_html(400)}
<div class="page" data-gc-canvas data-gc-privacy="{mode}">
  {egg_badge}
  <div class="{head_cls}">
    <div class="hleft">
      <div class="hname">{esc(title or row.get("name"))}</div>
      <div class="hsub">{esc(date_txt)} {esc(wd)}{time_txt}{(' · ' + esc(weather_txt)) if weather_txt else ''}</div>
    </div>
    <div class="htag">{esc(th['tag'])}</div>
  </div>

  <div class="hero">
    <div class="hduo">
      {dist_block}
      <div class="hbig">
        <div class="hlab">{esc(hero_lab)}</div>
        <div class="hval">{hero_val}</div>
        <div class="hsub2">{esc(hero_sub)}</div>
      </div>
    </div>
    <div class="hstats">
      {stat_html}
    </div>
  </div>

  <div class="mid">
    <div class="card trk">
      <div class="ctitle">轨迹<span>{trk_meta}</span></div>
      {trk_body}
    </div>
    <div class="card zc">
      {zone_block(hr_z, "心率分区", "占比 / 时长", coros=is_coros)}
    </div>
  </div>

  <div class="card ch">
    <div class="ctitle">{esc(chart_title)}<span><em class="lg pace"></em>{esc(chart_lg)}　<em class="lg hr"></em>心率</span></div>
    {chart_svg(points, CH_CW, 262, th)}
  </div>

  <div class="tech">
    <div class="card tw">
      <div class="ctitle">{esc(tech_title)}<span>{fix_hint}</span></div>
      <div class="grid">
        {tech_html}
      </div>
      <div class="spkdiv"></div>
      <div class="spark">{spark_svg(points, TW_CW, SPK_H, th)}</div>
    </div>
    <div class="card ld">
      <div class="ctitle">负荷</div>
      <div class="grid2">
        <div class="t"><b>{num(row.get("load")) or 0:.0f}</b><span>训练负荷</span></div>
        <div class="t"><b>{kcal:.0f}</b><span>千卡</span></div>
        <div class="t"><b>{te_a}</b><span>有氧 TE</span></div>
        <div class="t"><b>{te_n}</b><span>无氧 TE</span></div>
      </div>
      <div class="drift">{"—" if is_coros else _drift_txt(drift, s, th)}</div>
    </div>
  </div>

  <div class="{lp_cls}">
    <div class="ctitle">{lap_title}<span>{lap_sub}</span></div>
    {lap_html}
  </div>

  <div class="foot"><span class="rc-foot">{BR.badge_html(18, glow_color=th['acc'])}RunnerCoach</span> · 数据 {source_label}</div>
</div>
<script>
/* hero 自适应：长距离 + 带小时的用时（42.54 / 3:14:53）会把右上六格挤到换行，
   与其写死字号，不如量一次——横向放不下就按 4% 一档缩 --hs，直到刚好放得下。
   注意不能只看 scrollWidth：两块内容溢进卡片 padding 时它仍是 clientWidth，
   所以要按「子元素宽度和 + gap」跟内容盒宽度比。同步执行，赶在首次绘制前生效。 */
(function () {{
  var hero = document.querySelector('.hero');
  if (!hero) return;
  var root = document.documentElement, cs = getComputedStyle(hero);
  var inner = hero.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  var gap = parseFloat(cs.columnGap || cs.gap) || 0;
  var kids = hero.children, n = kids.length;
  function need() {{
    var t = gap * (n - 1);
    for (var i = 0; i < n; i++) t += kids[i].getBoundingClientRect().width;
    return t;
  }}
  var s = 1;
  for (var k = 0; k < 12 && s > 0.62; k++) {{
    if (need() <= inner + 0.5) break;
    s -= 0.04;
    root.style.setProperty('--hs', s.toFixed(3));
  }}
}})();
</script>
{_EXPORT_PROBE}
</body></html>"""


def _drift_txt(drift: dict, s: dict, theme: dict | None = None) -> str:
    th = theme or theme_for(None)
    if not drift:
        return "前后半程数据不足"
    dh = drift["hr_second"] - drift["hr_first"]
    dp = drift["pace_second"] - drift["pace_first"]
    if th["kind"] == "speed":
        # 骑行看时速：数字变大才是变快，跟配速的语义正好相反
        trend = "速度提升" if dp < -3 else ("速度下降" if dp > 3 else "速度持平")
        f_txt = lambda sec: f"{kmh(sec):.1f}km/h" if kmh(sec) else "—"
    else:
        trend = "配速加快" if dp < -3 else ("配速放慢" if dp > 3 else "配速持平")
        f_txt = pace_txt
    return (f'前 {f_txt(drift["pace_first"])}/{drift["hr_first"]}bpm → '
            f'后 {f_txt(drift["pace_second"])}/{drift["hr_second"]}bpm<br>'
            f'{trend}，心率{"上浮" if dh > 3 else ("回落" if dh < -3 else "持平")} {abs(dh)} bpm')


_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
/* 版心宽度必须跟着 Python 侧的常量走：加宽画幅时这里若是写死的旧值，
   中间几块卡（.mid / .tech）会撑出 body 被裁掉，右侧看着就像「数据溢出边框」。
   --page-w / --pad / --trk-w / --tw-w 由 build_html 从 W/PAD/TRK_W/TW_W 注入。 */
:root{--acc:#5CE1D0;--acc2:#3FA9F5;--grad2:#9FD8FF;--tagfg:#5CE1D0;
  --tagbg:#142234;--tagbd:#2A4056;--page-w:1080px;--pad:36px;
  --trk-w:468px;--tw-w:537px}
body{width:var(--page-w);overflow:hidden;background:#0F1319;
  font-family:"Microsoft YaHei","PingFang SC",sans-serif;color:#EDF1F5}
.page{width:var(--page-w);padding:30px var(--pad) 40px;
  display:flex;flex-direction:column;gap:16px}
/* 底部补 10px：成图正好 1080×1920，和每日看板同一个 9:16 画幅。
   之前是 1910，各家长图预览/分享缩略图对非标比例的裁切规则不一致，
   统一到标准竖屏尺寸后，微信/相册/分享面板都不会再多切一刀。 */
/* 标题与参数都可能很长（比赛名 + 日期 + 时段 + 天气）。旧版 .hleft 没有
   min-width:0，flex 子项按 max-content 撑开，右侧标签被直接挤出卡片。
   这里不用 -webkit-line-clamp（个别 WebView 版本上 display:-webkit-box 会让
   行高/换行表现不一致），改成 max-height + overflow:hidden，行为稳定。
   0.6.10：抬头的角标去掉（页脚左下角保留），左侧只剩标题与副标题；
   nowrap + min-width:0 仍让长标题自己收缩/换行，标签固定在原位。 */
.head{display:flex;align-items:flex-end;justify-content:space-between;
  gap:16px;flex-wrap:nowrap}
.hleft{flex:1 1 460px;min-width:0}
.hname{font-size:36px;font-weight:700;letter-spacing:.5px;line-height:1.3;
  overflow-wrap:anywhere;word-break:break-word;
  max-height:calc(36px * 1.3 * 2);overflow:hidden}
.hsub{font-size:21px;color:#939FAD;margin-top:10px;line-height:1.5;
  overflow-wrap:anywhere;word-break:break-word;
  max-height:calc(21px * 1.5 * 2);overflow:hidden}
.htag{flex:none;white-space:nowrap;font-size:19px;color:var(--tagfg);
  border:1px solid var(--tagbd);background:var(--tagbg);
  padding:7px 16px;border-radius:24px}
/* hero 内部一律按 --hs 缩放：左上大数字宽度随距离/配速的位数变化，
   42.54 + 3:14:53 这种长值会把右上六格挤到换行（「步幅 cm」折成两行）。
   截图前用一小段脚本量一次 scrollWidth，撑了就调小 --hs，见页尾 autofit。 */
.hero{display:flex;align-items:center;justify-content:space-between;gap:calc(32px * var(--hs,1));
  background:#121E2C;border:1px solid #23364A;border-radius:18px;padding:22px 30px}
/* 距离与配速并排两个大数字，左侧一组 */
.hduo{display:flex;align-items:flex-start;gap:calc(96px * var(--hs,1));flex:none}
.hval{font-size:calc(88px * var(--hs,1));font-weight:800;line-height:1;letter-spacing:-2px;
  background:linear-gradient(180deg,#FFFFFF,var(--grad2));-webkit-background-clip:text;
  -webkit-text-fill-color:transparent}
.hlab{font-size:calc(21px * var(--hs,1));color:#939FAD;margin-bottom:9px}
.hsub2{font-size:calc(19px * var(--hs,1));color:#76838F;margin-top:10px}
/* 右上角六项：用时/爬升/主要益处 一行，步频/步幅/心率 一行。
   max-content + flex:none：宁可让 autofit 缩字号，也不让轨道被挤到折行。 */
.hstats{display:grid;grid-template-columns:repeat(3,max-content);
  gap:calc(14px * var(--hs,1)) calc(26px * var(--hs,1));
  justify-content:end;align-items:end;flex:none}
.st{text-align:right}
.st b{display:block;font-size:calc(37px * var(--hs,1));font-weight:700;line-height:1.1;
  letter-spacing:-.5px}
.st span{font-size:calc(19px * var(--hs,1));color:#939FAD;white-space:nowrap}
/* 文字型取值（主要益处）降一档字号：4 个字按 37px 排会把整列撑开。
   nowrap 必须有——"乳酸阈值"折成两行会把行高顶起来，下面那一行被挤出卡片。
   line-height 按 37×1.1 反算，保证行盒高度和数字格一致，基线不会跳。 */
.st.wd b{font-size:calc(28px * var(--hs,1));letter-spacing:0;line-height:1.454;
  white-space:nowrap}
.mid{display:flex;gap:16px;height:310px}
.card{background:#121E2C;border:1px solid #23364A;border-radius:18px;padding:20px 24px}
.trk{width:var(--trk-w);display:flex;flex-direction:column;padding:18px 22px}
.zc{flex:1;display:flex;flex-direction:column;justify-content:center}
.ctitle{font-size:24px;color:#D3DBE4;font-weight:600;margin-bottom:14px;
  display:flex;justify-content:space-between;align-items:baseline}
.ctitle span{font-size:18px;color:#76838F;font-weight:400}
.empty{display:flex;align-items:center;justify-content:center;color:#64707E;font-size:19px}
.gc-private{display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:9px;border:1px dashed #2C3B4A;border-radius:14px;color:#8593A3;
  font-size:18px;text-align:center;box-sizing:border-box}
.gc-private b{color:#5CE1D0;font-size:23px;font-weight:600;letter-spacing:1px}
.gc-private small{font-size:14px;color:#64707E;line-height:1.6}
.src{font-size:13px;fill:#DCE6F0;opacity:0.75;paint-order:stroke;
  stroke:#101A26;stroke-width:1.2}
.pin{font-size:19px;font-weight:700;paint-order:stroke;
  stroke:#101A26;stroke-width:3.4;stroke-linejoin:round}
.ch{height:360px}
.lp{margin-top:0}
.tech{display:flex;gap:16px;height:322px}
.tw{width:var(--tw-w);display:flex;flex-direction:column}
.ld{flex:1;display:flex;flex-direction:column}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
.grid2{display:grid;grid-template-columns:repeat(2,1fr);gap:12px 18px}
.t b{display:block;font-size:38px;font-weight:700;line-height:1.15;letter-spacing:-.5px}
.t span{font-size:16px;color:#939FAD}
/* 走势图沉到卡片底部：少几行内容时多出来的空白落在分隔线上方，不会吊在半空 */
.spkdiv{height:1px;background:#23364A;margin-top:auto}
.spark{padding-top:12px}
.spklab{font-size:17px;fill:#939FAD}
.spkval{font-size:16px;fill:#76838F;font-variant-numeric:tabular-nums}
/* 与左侧技术卡对齐：底部一段「文字说明」用同一条分隔线起头，
   两卡并排时结构一致，也不会让负荷卡中间空出一大块 */
.drift{font-size:19px;color:#939FAD;line-height:1.6;margin-top:auto;
  padding-top:14px;border-top:1px solid #23364A}
.foot{font-size:16px;color:#5A6675;text-align:center;letter-spacing:.4px}
.zhead{font-size:22px;color:#D3DBE4;font-weight:600;display:flex;justify-content:space-between;
  margin-bottom:10px}
.zhead span{font-size:17px;color:#76838F;font-weight:400}
/* 心率与功率共用同一条堆叠条：同宽、同粗、同圆角，两卡并排才不显得错位 */
.zbar-stack{display:flex;height:22px;border-radius:7px;overflow:hidden;background:#10161C}
.zbar-stack i{display:block}
.zrow{display:flex;align-items:center;gap:10px;margin-top:11px}
.zdot{width:12px;height:12px;border-radius:3px;flex:none}
.zname{width:86px;font-size:20px;color:#D3DBE4;flex:none;white-space:nowrap}
.zname em{font-style:normal;font-size:15px;color:#76838F;margin-left:5px;
          display:inline-block;max-width:52px;overflow:hidden;
          text-overflow:ellipsis;white-space:nowrap}
.zcoros .zname{width:120px}
.zcoros .zname em{max-width:none}
.zbar{flex:1;height:10px;background:#10161C;border-radius:5px;overflow:hidden}
.zbar i{display:block;height:100%;border-radius:5px}
.zval{width:74px;text-align:right;font-size:19px;color:#939FAD;font-variant-numeric:tabular-nums}
.zpct{width:58px;text-align:right;font-size:20px;font-weight:700}
.zempty{font-size:19px;color:#64707E;padding:12px 0 4px}
.lhead,.lrow{display:flex;align-items:center;gap:10px}
.lhead{font-size:16px;color:#76838F;padding-bottom:8px;border-bottom:1px solid #23364A}
/* 表头里的条形格子只是占位，用来和下面的数据行对齐，不能画成一条空数据条 */
.lhead .lbar{background:none}
.lrow{padding:5px 0}
.lid{width:64px;text-align:right;font-size:19px;color:#939FAD;flex:none}
.ldist{width:82px;font-size:19px;color:#939FAD;flex:none}
.lbar{flex:1;height:12px;background:#10161C;border-radius:6px;overflow:hidden}
.lbar i{display:block;height:100%;border-radius:6px}
.lpace{width:80px;text-align:right;font-size:23px;font-weight:600;flex:none}
.lhr{width:58px;text-align:right;font-size:19px;color:#E8843A;flex:none}
.lcad{width:64px;text-align:right;font-size:19px;color:#939FAD;flex:none}
.lrow.partial{opacity:.45}
.lnote{font-size:16px;color:#76838F;margin-top:10px}
.axis{font-size:15px;fill:#64707E;font-family:"Microsoft YaHei",sans-serif}
.axl{font-size:16px;font-family:"Microsoft YaHei",sans-serif;
  font-variant-numeric:tabular-nums}
.lg{display:inline-block;width:13px;height:4px;border-radius:2px;margin-right:6px;
  vertical-align:middle}
.lg.pace{background:var(--acc)}
.lg.hr{background:#E8843A}
/* 间歇结构：柱 = 一组（间歇 + 组间恢复），柱下三行标注配速/时间/距离。
   组数是「间歇组数」：15×400 m 是 15 根柱，恢复画成柱底细灰条。 */
.ivband{background:#10161C;border:1px solid #23364A;border-radius:14px;padding:14px 16px}
.ivcols{display:flex;gap:5px;align-items:stretch;padding-bottom:10px;
  border-bottom:1px solid #1C2A38;margin-bottom:12px}
.ivc{flex:1 1 0;min-width:0;display:flex;flex-direction:column;justify-content:flex-end}
.ivcb{height:120px;display:flex;flex-direction:column-reverse;align-items:center;
  gap:3px;justify-content:flex-start}
.ivcb i{display:block;width:62%;border-radius:4px 4px 2px 2px}
.ivcb .ivrest{height:7px;background:#3C4956;border-radius:2px}
.ivcl{margin-top:7px;text-align:center;line-height:1.3;white-space:nowrap;
  overflow:hidden}
.ivcl b{display:block;font-size:16px;font-weight:700;color:#D3DBE4;
  font-variant-numeric:tabular-nums}
.ivcl span{display:block;font-size:13px;color:#939FAD;
  font-variant-numeric:tabular-nums}
.ivcl em{display:block;font-style:normal;font-size:13px;color:#76838F;
  font-variant-numeric:tabular-nums}
/* 组数超过 16 时退化为纯结构条（柱宽按时长） */
.ivstrip{display:flex;align-items:flex-end;gap:4px;height:132px;padding-bottom:10px;
  border-bottom:1px solid #1C2A38;margin-bottom:12px}
.ivsb{display:block;border-radius:4px 4px 2px 2px;min-width:5px}
.ivsb.rest{background:#3C4956;border-radius:2px}
.ivband{background:#10161C;border:1px solid #23364A;border-radius:14px;padding:14px 16px}
.ivstrip{display:flex;align-items:flex-end;gap:4px;height:132px;padding-bottom:10px;
  border-bottom:1px solid #1C2A38;margin-bottom:12px}
.ivsb{display:block;border-radius:4px 4px 2px 2px;min-width:5px}
.ivsb.rest{background:#3C4956;border-radius:2px}
.ivbrows{display:flex;flex-direction:column;gap:9px}
.ivbr{display:flex;align-items:center;gap:10px;font-size:18px}
.ivbr>i{width:14px;height:14px;border-radius:4px;flex:none}
.ivbr>b{width:44px;font-size:20px;font-weight:700;color:#D3DBE4;flex:none}
.ivbr>span{flex:1;color:#939FAD;min-width:0;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}
.ivbr>em{width:104px;text-align:right;font-style:normal;font-size:21px;flex:none;
  font-variant-numeric:tabular-nums}
.ivbr>em u{display:block;font-size:14px;color:#76838F;text-decoration:none}
""" + BR.BRAND_CSS + BR.card_css(".card", top="#18202A", bottom="#141A22") + """
/* 品牌化：卡片顶沿高光、抬头角标、右下角水印、页脚字标。
   背景渐进与辉光由 build_html 按运动主色注入，见 surface_css。 */
.card{position:relative}
.card::before{content:"";position:absolute;left:0;right:0;top:0;height:1px;
  background:linear-gradient(90deg,transparent,rgba(96,165,250,.40),transparent)}
.page{position:relative;z-index:1}
.rc-wm{right:36px;bottom:70px;opacity:.055}
.foot .rc-foot{margin-right:6px;color:#93A3B5}
.foot .rc-foot .rc-badge{width:18px;height:18px}
""" + EG.EGG_CSS


# ------------------------------------------------------------------ 出图
def shoot(html_path: Path, png_path: Path, w: int = W, h: int = H) -> Path:
    browser = _find_browser()
    if not browser:
        raise RuntimeError("找不到 Chrome / Edge，无法生成图片")
    png_path.parent.mkdir(parents=True, exist_ok=True)
    if png_path.exists():
        png_path.unlink()
    cmd = [
        browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--force-device-scale-factor=1", "--default-background-color=00000000",
        # 页面高度随内容自适应，这里给足画布，事后按实际内容高度裁掉底部空白。
        # （Windows 上无头窗口的视口比 --window-size 少 95px，所以多给一些）
        f"--window-size={w},{h + 420}", f"--screenshot={png_path.resolve()}",
        html_path.resolve().as_uri(),
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=90)
    if not png_path.exists():
        raise RuntimeError("浏览器没有产出图片：" +
                           (proc.stderr or b"").decode("utf-8", "ignore")[:300])
    from .exportspec import BG_ACT
    from .pngutil import fit_to_frame, trim_bg
    # 四边裁掉页面自带的底色留白（全部贴边，不留呼吸垫），再收进 9:16：
    # 页面 padding 经缩放后仍是成品最大的「边框」来源，先裁掉内容才能
    # 贴着安全带排，四边只剩安全带和 contain 的微差。
    trim_bg(png_path, bg=BG_ACT)
    fit_to_frame(png_path, bg=BG_ACT)
    return png_path


def render(row: dict, detail: dict, out_png: Path, photo: Path | None = None,
           use_map: bool = True, title: str | None = None,
           privacy: str = PV.MODE_NONE) -> Path:
    out_png = Path(out_png)
    html_path = out_png.with_suffix(".html")
    html_path.parent.mkdir(parents=True, exist_ok=True)
    mode = PV.normalize(privacy)
    # 脱敏版不碰底图缓存：ensure() 会把真实坐标发给高德并落盘 PNG
    photo_uri = None if mode != PV.MODE_NONE else (
        photo.resolve().as_uri() if photo and Path(photo).exists() else None)
    # 底图与隐私档位解耦：hide 不取；trim 用裁剪后的轨迹取景（起终点不入图），
    # 缓存键带档位后缀，避免与「原图」的底图混用。
    mapinfo = None
    m_track, _ = PV.mask_track(detail.get("track") or [], mode)
    if (mode != PV.MODE_HIDE and use_map and len(m_track) >= 2
            and os.environ.get("GARMIN_MAP", "1") != "0"):
        from . import basemap as _bm
        ident = str(row.get("activity_id") or "act")
        if mode != PV.MODE_NONE:
            ident += f"-{mode}"
        got = _bm.ensure(m_track, TRK_CW, TRK_CH, ident)
        if got:
            mapinfo = {"png": Path(got["png"]).resolve().as_uri(), "meta": got["meta"]}
    html_path.write_text(
        build_html(row, detail, photo_uri, mapinfo, title, privacy=mode),
        encoding="utf-8")
    return shoot(html_path, out_png)
