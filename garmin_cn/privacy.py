"""分享前轨迹脱敏：把「精确行踪」挡在分享图之外。

为什么要单独一个模块
--------------------
``detail["track"]`` 是 WGS-84 经纬度点列。画成 SVG 折线后虽然不写坐标数字，
但**起终点和路线形状本身**就能反推住处、单位或常跑路线，尤其是每天同一时间
出门的通勤跑。系统分享面板、微信、相册都是「发出去就收不回」，所以脱敏必须
在**生成 HTML 之前**做——出完 PNG 再打码是没用的，原始折线仍留在
``output/*.html`` 里，也可能被一起转发。

三档口径（对齐 docs/交付形态调研.md §5.5）
    ``none``  原图，什么都不改
    ``trim``  首尾各裁 200 m，藏住家门口和单位门口，保留中段形状（推荐）
    ``hide``  完全不画轨迹，也不叠底图/照片，连路线轮廓都不出现在文件里

纯标准库、纯函数、零 IO：不读配置、不写库、不联网，也不依赖高德 Key。
档位由 ``appapi`` 传进来，画不画由 ``actcard`` 决定。
"""
from __future__ import annotations

import bisect

from .route import haversine

MODE_NONE = "none"
MODE_TRIM = "trim"
MODE_HIDE = "hide"
MODES = (MODE_NONE, MODE_TRIM, MODE_HIDE)

# 默认裁掉首尾各 200 m：足够跨出小区/园区，又不至于把 3 km 的短跑裁光
DEFAULT_TRIM_M = 200.0


def normalize(mode) -> str:
    """把外部传来的档位收敛到三档之一；未知值按较保守的裁剪处理。"""
    s = str(mode or "").strip().lower()
    if s in MODES:
        return s
    # 新版本可能传更严格的档位，落到 none 反而会把原图发出去，所以取 trim
    return MODE_NONE if not s else MODE_TRIM


def _points(track) -> list[tuple[float, float]]:
    """只留合法的 [lat, lon] 数值对，脏数据直接跳过而不让整张图崩掉。"""
    out: list[tuple[float, float]] = []
    for p in track or []:
        try:
            lat, lon = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            out.append((lat, lon))
    return out


def mask_track(track, mode: str = MODE_TRIM,
               head_m: float = DEFAULT_TRIM_M,
               tail_m: float = DEFAULT_TRIM_M) -> tuple[list, dict]:
    """返回 ``(新轨迹, info)``。不修改入参；``info`` 只含统计，绝不含坐标。

    ``trim`` 用 haversine 逐段累加几何距离，取累计里程落在
    ``[head_m, total - tail_m]`` 区间的点。不依赖 ``detail["route"]``：
    旧缓存可能压根没有这个键（见 activity.py 的说明）。
    """
    mode = normalize(mode)
    pts = _points(track)
    n = len(pts)
    head = max(0.0, float(head_m or 0))
    tail = max(0.0, float(tail_m or 0))
    info = {
        "mode": mode, "points": n, "kept": n, "head_m": head, "tail_m": tail,
        "cut_head_m": 0.0, "cut_tail_m": 0.0, "total_m": 0.0, "reason": "",
    }
    if mode == MODE_NONE:
        return list(pts), info
    if mode == MODE_HIDE:
        info.update(kept=0, reason="轨迹已隐藏")
        return [], info
    if n < 2:
        info.update(kept=0, reason="无轨迹数据")
        return [], info

    acc = [0.0]
    for i in range(1, n):
        a, b = pts[i - 1], pts[i]
        acc.append(acc[-1] + haversine(a[0], a[1], b[0], b[1]))
    total = acc[-1]
    info["total_m"] = round(total, 1)
    lo = bisect.bisect_left(acc, head)
    hi = bisect.bisect_right(acc, total - tail) - 1
    kept = pts[lo:hi + 1] if hi >= lo else []
    info["kept"] = len(kept)
    info["cut_head_m"] = round(acc[lo], 1) if lo < n else round(total, 1)
    info["cut_tail_m"] = round(total - acc[hi], 1) if hi >= 0 else round(total, 1)
    if len(kept) < 2:
        info["reason"] = "活动太短，裁掉首尾后无剩余轨迹"
    else:
        info["reason"] = f"首尾各裁 {head:.0f} / {tail:.0f} m"
    return kept, info


def badge(mode: str, info: dict | None = None) -> str:
    """轨迹卡右上角角标；原图返回空串（沿用「无底图 · 自 GPS」）。"""
    mode = normalize(mode)
    if mode == MODE_NONE:
        return ""
    if mode == MODE_HIDE or (info and info.get("kept", 0) < 2):
        return "已脱敏 · 轨迹已隐藏"
    return "已脱敏 · 首尾各 200 m"


def note(mode: str, info: dict | None = None) -> str:
    """给 App 提示 / 日志用的一句话，不含坐标。"""
    mode = normalize(mode)
    if mode == MODE_NONE:
        return "原图（含完整 GPS 轨迹）"
    if mode == MODE_HIDE:
        return "已脱敏：轨迹、底图与精确起跑时间均已隐藏"
    if info and info.get("kept", 0) < 2:
        return "已脱敏：活动较短，轨迹已整体隐藏"
    return "已脱敏：轨迹首尾各裁 200 m"


def placeholder(mode: str, info: dict | None, w: int, h: int) -> str:
    """占位块尺寸与轨迹 SVG 一致，避免 .mid 的 flex 布局塌掉。

    刻意不用 ``display:none``：右边的心率分区卡是 flex:1，隐藏左卡会让它
    撑满整行，成品版式跟着变，9:16 归一后容易露出大块空白。
    """
    mode = normalize(mode)
    why = (info or {}).get("reason") or "保护常跑路线与起终点"
    return (
        f'<div class="gc-private" data-gc-private="{mode}" '
        f'style="width:{w}px;height:{h}px">'
        '<b>轨迹已隐藏</b>'
        f'<span>{why}</span>'
        '<small>分享图不含 GPS 路线、底图与起终点标记</small>'
        '</div>'
    )
