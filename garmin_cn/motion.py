"""动态轨迹回放：竖版 1080×1920，可播放、可拖、可逐帧导出。

为什么是网页而不是直接出视频
----------------------------
成片要逐帧渲染：1080×1920 一帧在 Python 里用 `pngutil` 画（纯标准库）得几十
毫秒到上百毫秒，1350 帧就是十几分钟，而且之后改一版颜色要重画一遍。放到浏览器
里用 canvas 画，同样的画面 60fps，改样式即时可见；要 MP4 时用同一套 `seek()`
逐帧截图再合，两套出口一套代码。

底图为什么只取一张
------------------
`basemap` 走的是高德**静态图**（Web 服务 Key，5000 次/日）。逐帧取图不可能，
所以整体只取一张全览图，用 canvas 缩放做「镜头推近」。静态图 size 上限 1024，
这里按 540×960 请求、`scale=2` 拿回 1080×1920，正好等于画布原生尺寸——这是
能在 9:16 下拿到 1:1 像素的唯一取法，4 张 3:4 的拼图反而会糊。

坐标
----
轨迹是 WGS-84，底图是 GCJ-02，投影全部交给 `basemap.to_px()`（内部纠偏），
这里只把 CSS 像素乘 2 变成画布像素。**不要在 JS 里重算投影**，否则两套
实现迟早错开。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
from pathlib import Path

from . import basemap, clip as clip_mod, photo as photo_mod, route as route_mod
from .metrics import num

W, H = 1080, 1920

# `data/` 下能通过 http 放行的子目录（与 serve._data 的白名单保持一致）
_WEB_RE = re.compile(r"(basemaps|media|motion_cache|thumbs)/"
                     r"[A-Za-z0-9._-]+\.(png|jpg|jpeg|mp4|mov|m4v)")


def _web_url(path: str) -> str:
    """本机绝对路径 → 同源 URL；不在 `data/` 下就原样返回。

    桌面用 file:// 打开播放器，绝对路径能直接加载；但 App 里页面是
    `http://127.0.0.1:port/xxx.html`，绝对路径会被当成 URL 路径——
    真机上 `base` 是 `/data/user/0/<包名>/files/data/basemaps/x.png`，
    服务端白名单只认 `/data/basemaps/...`，于是 404，底图加载不出来，
    页面 `ready` 永远是 false，导出直接静默返回（表现为一直 0 帧）。
    """
    if not path:
        return path
    try:
        rel = Path(str(path)).resolve().relative_to(
            (basemap.ROOT / "data").resolve()).as_posix()
    except (ValueError, OSError):
        return path
    return "/data/" + rel if _WEB_RE.fullmatch(rel) else path
# 静态图按 540×960 请求 + scale=2 → 1080×1920，与画布 1:1
BASE_W, BASE_H = 540, 960
PAD = 44                 # 取景内边距（BASE 坐标系）
MOTION_REV = 2           # rev2：缓存键纳入取景指纹，杜绝串档（见 ensure_basemap）
ROOT = Path(__file__).resolve().parent.parent
ACCENT = "#34D399"       # 与一图流同一支绿
INK = "#0B0F14"

# 单张照片在成片里停留的秒数（对 45 秒成片来说约 3 秒 × 张数）
PHOTO_HOLD = 3.0


# ------------------------------------------------------------------ 取景 / 底图
def _fit_zoom(track: list[list[float]]) -> int:
    """选一个能把整条轨迹装进画幅的最大 zoom（整数档）。"""
    aw, ah = BASE_W - 2 * PAD, BASE_H - 2 * PAD
    for z in range(basemap.MAX_ZOOM, 3, -1):
        bw, bh = basemap._span(track, z)
        if bw <= aw and bh <= ah:
            return z
    return 4


def meta_of(track: list[list[float]]) -> dict:
    """固定取景：中心取外框中心，zoom 由 `_fit_zoom` 定，不做 s<1 的回缩。

    不用 `basemap.fit()` 是因为它为了让轨迹撑满画幅会「深一档再缩回」，回缩后
    请求尺寸 w/s 会超过静态图 1024 的上限，9:16 尤其明显（960/0.8=1200）。
    这里宁可留白，也不要请求失败。
    """
    pts = basemap._gcj(track)
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return {
        "fit": "motion", "rev": MOTION_REV,
        "center": [round((min(lons) + max(lons)) / 2, 6),
                   round((min(lats) + max(lats)) / 2, 6)],
        "zoom": _fit_zoom(track), "w": BASE_W, "h": BASE_H, "s": 1.0,
        "frame": [W, H], "pad": PAD,
    }


def _fit_key(meta: dict, npts: int) -> str:
    """取景指纹：换成别的隐私档位 / 轨迹更新 / 取景算法改版，都必须换底图。

    以前缓存文件名只有 `motion-<activity_id>.png`，而 App 默认档位是
    `trim`（首尾各裁 200 m）。同一条活动先用「不脱敏」生成过一次，缓存的
    就是**完整轨迹**的 zoom；之后用默认档生成，轨迹被裁短了，却仍拿那张
    底图和它的 meta 来投影 —— 轨迹缩在画面一角，看起来就是「底图缩放错了」。
    两种档位的取景中心、zoom 都不同，把它们编进文件名就再也不会串。
    """
    raw = (f"rev{meta['rev']}|{meta['center'][0]:.6f},{meta['center'][1]:.6f}"
           f"|z{meta['zoom']}|{meta['w']}x{meta['h']}|pad{meta['pad']}"
           f"|frame{meta['frame'][0]}x{meta['frame'][1]}|n{npts}")
    return hashlib.sha1(raw.encode("ascii")).hexdigest()[:10]


def ensure_basemap(track: list[list[float]], ident: str) -> dict | None:
    """取底图（带缓存）。没有 Key 或接口失败返回 None，调用方回落到纯色底。

    缓存必须跟着**这次的取景**走：命中时逐个核对 center / zoom / 画幅，
    旧版本留下的 `motion-<id>.png` 因为文件名不带指纹，自然不再被读取。
    """
    if len(track) < 2:
        return None
    meta = meta_of(track)
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(ident))[:48]
    cache = basemap.CACHE / f"motion-{safe_id}-{_fit_key(meta, len(track))}.png"
    meta_f = cache.with_name(cache.stem + ".json")
    if cache.exists() and cache.stat().st_size > 1024 and meta_f.exists():
        try:
            old = json.loads(meta_f.read_text("utf-8"))
            same = (old.get("fit") == "motion" and old.get("rev") == MOTION_REV
                    and old.get("frame") == meta["frame"]
                    and old.get("center") == meta["center"]
                    and old.get("zoom") == meta["zoom"]
                    and old.get("w") == meta["w"] and old.get("h") == meta["h"]
                    and old.get("pad") == meta["pad"])
            if same:
                return {"png": cache, "meta": old}
        except (OSError, ValueError):
            pass
    k = basemap.key()
    if not k:
        return None
    meta["region"] = basemap.regeo(meta["center"][1], meta["center"][0])
    import urllib.request

    url = basemap.static_url(meta, k)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = resp.read()
    except Exception:
        return None
    if not data.startswith(b"\x89PNG"):
        return None
    basemap.CACHE.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(data)
    meta_f.write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", "utf-8")
    return {"png": cache, "meta": meta}


# ------------------------------------------------------------------ 缩略图
def _shoot(html: Path, png: Path, w: int, h: int) -> Path | None:
    """调浏览器截一张精确尺寸的 PNG（视口比 window-size 少 95px，裁掉）。"""
    from .card import _find_browser

    browser = _find_browser()
    if not browser:
        return None
    png.parent.mkdir(parents=True, exist_ok=True)
    if png.exists():
        png.unlink()
    subprocess.run([
        browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--force-device-scale-factor=1", "--allow-file-access-from-files",
        f"--window-size={w},{h + 95}", f"--screenshot={png}",
        html.resolve().as_uri(),
    ], capture_output=True, timeout=90)
    if not png.exists():
        return None
    try:
        from .pngutil import crop_height

        crop_height(png, h)
    except Exception:
        pass
    return png


def visible_size(ph: dict, max_w: int = 1200) -> tuple[int, int]:
    """照片的「视觉」尺寸：Orientation 5~8 是 90° 旋转，宽高要对调。"""
    w = num(ph.get("w")) or 1200
    h = num(ph.get("h")) or 900
    o = int(ph.get("orientation") or 1)
    if o in (5, 6, 7, 8):
        w, h = h, w
    k = min(max_w / w, 1.0)
    return max(int(w * k), 1), max(int(h * k), 1)


def make_thumb(src: Path, dst: Path, ph: dict, max_w: int = 1200) -> Path | None:
    """把 4 MB / 4096px 的原图缩成播放用的缩略图。

    不缩的话每次截图都要重新解一张 4096×2304 的 JPEG——逐帧导出时这是
    主要耗时，且 10 张常驻内存要几百 MB。
    """
    if dst.exists() and dst.stat().st_size > 1024:
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    w, h = visible_size(ph, max_w)
    tmp = dst.with_suffix(".html")
    tmp.write_text(
        '<!doctype html><meta charset="utf-8">'
        '<style>html,body{margin:0;padding:0;background:#000;overflow:hidden}'
        f'img{{width:{w}px;height:{h}px;object-fit:cover;display:block}}</style>'
        f'<img src="{Path(src).resolve().as_uri()}">', encoding="utf-8")
    got = _shoot(tmp, dst, w, h)
    try:
        tmp.unlink()
    except OSError:
        pass
    return got


# ------------------------------------------------------------------ 数据打包
CLIP_LEAD = 0.0         # 素材延迟起播的秒数：0 = 卡片一出现就播
                        # 原来是 0.15（怕"贴脸突然有声"），但那会让停留必须多留 0.15s
                        # 收尾，总时长就对不上「30s + 素材总长」的要求了。
RAMP = 0.70             # 每处素材「停住」前后各 0.7s 加减速（播放秒）
# 上限只是防呆，不是裁剪。素材要「完整插入」：视频按自身时长整段播完，
# 所以这里的上限必须 ≥ clip.py 的 MAX_VIDEO_SEC，否则长视频会被截断播放
# （页面时长标签写的是原时长，实际只播了前面一段，两边对不上）。
HOLD_MIN, HOLD_MAX = 0.6, 3600.0


def _timeline_total(play: float, photos: list[dict]) -> float:
    """页面时间轴的真实总时长，算法必须与 JS 里的 `TL` / `holdsSum` 逐字一致。

    总时长 = 巡航预算 + 素材停留。素材停留是**额外叠加**的，不挤占 `play`：
    30 秒巡航 + 视频素材总长 + 3 秒 × 图片素材数。

    App 靠这个数算要渲染多少帧，算小了片子尾巴会被截掉（素材越多截得越狠）。
    """
    holds = 0.0
    plain = 0
    for ph in photos:
        clip = ph.get("clip")
        if clip:
            holds += max(HOLD_MIN, min(HOLD_MAX, float(clip.get("dur") or 0)))
        else:
            plain += 1
    if plain:
        holds += min(2.8, max(1.0, play * 0.40 / plain)) * plain
    # 加减速本身也占播放时间。素材多到「光加减速就超过 play」时，巡航被压成 0，
    # 总长以加减速兜底 —— 对应页面里那条 build(8000) 的退化分支。
    return max(play, 2 * RAMP * len(photos)) + holds


def _spread_clips(clips: list[dict], rt: dict) -> list[dict]:
    """给没有拍摄时间的素材补一个位置，均匀铺在活动区间里。

    用户从相册随手加一段素材，文件里往往既没有可信时间也没有 GPS
    （转发来的图、截屏、剪辑过的视频，元数据都被剥掉了）。
    `photo.match` 会以「没有拍摄时间」把它们全部丢掉，表现就是
    「加了素材，回放里却什么都没有」。这里按数量均匀铺开，
    宁可位置不准，也别让人以为功能坏了。
    """
    if not clips or not rt.get("n"):
        return clips
    t0 = rt.get("t0")
    span = float((rt.get("t") or [0])[-1] or 0)
    if not t0 or span <= 0:
        return clips
    miss = [c for c in clips if c.get("t_utc") is None]
    if not miss:
        return clips
    n = len(miss)
    for i, c in enumerate(miss):
        c["t_utc"] = int(t0 + span * 1000.0 * (i + 1) / (n + 1))
        c["approx"] = True
    return clips


def build_payload(row: dict, detail: dict, photos: list[dict] | None = None,
                  *, clips: list[dict] | None = None, thumb_dir: Path | None = None,
                  clip_cache: Path | None = None, duration: float = 45.0,
                  region: str = "", web: bool = False) -> dict:
    """把活动行 + 明细 + 照片/视频整理成播放器要的一份 JSON。

    `web=True` 时把本机绝对路径换成 `/data/...` 同源 URL——App 里的播放器
    走 http，绝对路径加载不出来（见 `_web_url`）。桌面 file:// 场景保持原样。
    """
    rt = detail.get("route") or {}
    if not rt.get("n"):
        raise ValueError("这次活动没有轨迹数据（室内跑 / 无 GPS / 未重抓明细）")
    track = detail.get("track") or [[la, lo] for la, lo in zip(rt["lat"], rt["lon"])]
    bm = ensure_basemap(track, str(row.get("activity_id")))

    if bm:
        meta = bm["meta"]
        # to_px 返回的是 BASE 坐标系（540×960），乘 2 得到画布像素
        pts = [(basemap.to_px(la, lo, meta)[0] * 2,
                basemap.to_px(la, lo, meta)[1] * 2)
               for la, lo in zip(rt["lat"], rt["lon"])]
        gps = True
        # 同一组变换也会在 JS 的 bakeBase 里作用到底图位图上，两者必须同源，
        # 否则轨迹就会和路网错开。无底图时 _project 已排好版，不要动。
        view = _view_fit(pts)
        pts = [(x * view["k"] + view["dx"], y * view["k"] + view["dy"])
               for x, y in pts]
    else:
        # 没底图：自己投影到画布，留出上下给文字
        pts = _project(rt, W, H)
        gps = False
        view = {"k": 1.0, "dx": 0.0, "dy": 0.0, "ratio": [0.0, 0.0]}

    out_photos = []
    if photos:
        got = photo_mod.match(photos, rt, t0_ms=rt.get("t0"))
        for g in got["points"]:
            x, y = _xy_at(rt, pts, g["t"])
            shots = []
            for sh in g["shots"]:
                src = Path(sh["path"])
                thumb = None
                if thumb_dir is not None:
                    dst = thumb_dir / (src.stem + ".png")
                    thumb = make_thumb(src, dst, sh)
                # 存**文件路径**而不是 file:// URI：导出 MP4 时页面从本地
                # HTTP 服务加载同一批文件，URI 形式会让 canvas 被污染，
                # 浏览器就拒绝 toBlob
                shots.append({
                    "src": str((thumb or src).resolve()),
                    "w": visible_size(sh)[0], "h": visible_size(sh)[1],
                    "name": sh["name"], "gap": sh["gap_m"],
                    "approx": bool(sh.get("approx")),
                })
            out_photos.append({"t": round(g["t"], 1), "x": round(x, 1),
                               "y": round(y, 1), "d": round(g["d"], 1),
                               "shots": shots})
        loose = got["loose"]
    else:
        out_photos, loose = [], []

    # ---- 素材片段（视频 / 图片）：同一套吸附规则，挂到已有的照片点上；
    # 挂不上就自己成一个点
    out_clips = 0
    if clips:
        clips = _spread_clips(clips, rt)
        # cluster_m=0：素材**不合并**。两段视频拍在同一地点很常见（同路口前后各
        # 一段），合并后只会保留最长的一段，另一段就凭空消失了——用户要的是
        # 「素材完整插入」。clamp_ends：时间明显不在这次活动里的也贴到最近的
        # 一端，不许丢。
        cg = photo_mod.match(clips, rt, t0_ms=rt.get("t0"),
                             cluster_m=0.0, clamp_ends=True,
                             keep=("dur", "codec", "bytes", "kind"))
        loose.extend(cg["loose"])
        for g in cg["points"]:
            if not g["shots"]:
                continue
            # 一段点位上可能有多段，取最长的那段（短的通常是误触）
            pick = max(g["shots"], key=lambda x: x.get("dur") or 0)
            if not pick.get("dur"):
                continue
            kind = pick.get("kind") or "video"
            if kind == "image":
                # 图片素材不做转码，直接把原图交给页面画（Ken Burns 之外不再加工）
                src = pick["path"]
                vw, vh = visible_size(pick)
            else:
                src = clip_mod.ensure_playable(
                    pick, clip_cache or (ROOT / "data" / "motion_cache"))
                # 尺寸以「真正送进浏览器的那个文件」为准：转码时 ffmpeg 已经把旋转
                # 烘进像素、矩阵归一了，拿它的 tkhd 才是画面真实朝向。
                # 用原文件的 w/h 会把竖屏塞进横格子（画面被压扁）。
                real = clip_mod.probe(Path(src))
                if real.get("w") and real.get("h"):
                    vw, vh = real["w"], real["h"]
                    k = min(1200 / max(vw, vh), 1.0)
                    vw, vh = max(int(vw * k), 1), max(int(vh * k), 1)
                else:
                    vw, vh = visible_size(pick)
            x, y = _xy_at(rt, pts, g["t"])
            item = {
                "src": str(Path(src).resolve()),
                # 同源 URL 只给页面看；混原声时原生编解码器要的是磁盘路径。
                # `web=True` 会把 src 改成 /data/...，这个字段必须原样留着，
                # 否则手机上 `/__timeline` 回传的 src 是 URL，MediaExtractor 打不开。
                "local": str(Path(src).resolve()),
                "kind": kind,
                "w": vw, "h": vh,
                "dur": round(float(pick["dur"]), 2), "name": pick["name"],
                "gap": pick["gap_m"], "approx": bool(pick.get("approx")),
                "lead": CLIP_LEAD,
            }
            # 已有同点照片 → 合并（视频优先，照片当封面/备用）
            host = None
            for p0 in out_photos:
                if host is None and "clip" not in p0 and abs(p0["t"] - g["t"]) <= 4.0 \
                        and abs(p0["d"] - g["d"]) <= 40:
                    host = p0
            if host is not None:
                host.pop("shots", None)      # 视频优先，不再并排照片
                host["clip"] = item
            else:
                out_photos.append({"t": round(g["t"], 1), "x": round(x, 1),
                                   "y": round(y, 1), "d": round(g["d"], 1),
                                   "shots": [], "clip": item})
            out_clips += 1
        out_photos.sort(key=lambda p: p["t"])

    s = route_mod.summary(rt)
    started = (row.get("start_ts") or "")[5:16]
    pl = {
        "w": W, "h": H,
        "title": row.get("name") or "跑步",
        "sub": " · ".join(x for x in [started,
                                      (bm or {}).get("meta", {}).get("region") or region] if x),
        "base": str(bm["png"].resolve()) if bm else "", "gps": gps,
        "view": view,
        "root": str(ROOT),
        "t": rt["t"], "x": [round(p[0], 1) for p in pts],
        "y": [round(p[1], 1) for p in pts],
        "d": rt["d"], "hr": rt["hr"], "seg": rt["seg"],
        "dur": int(s["duration_s"]), "dist": round(s["distance_m"], 1),
        "hr_avg": s.get("hr_avg"), "hr_max": s.get("hr_max"),
        "play": float(duration),
        # 真实总时长（见 `_timeline_total`）。App 按它算渲染帧数。
        "total": round(_timeline_total(float(duration), out_photos), 3),
        "spread": sum(1 for p in out_photos
                      if p.get("clip") and p["clip"].get("approx")),
        "photos": out_photos, "clips": out_clips, "loose": loose,
        "accent": ACCENT,
    }
    if web:
        pl["base"] = _web_url(pl["base"])
        for p in pl["photos"]:
            for sh in p.get("shots") or []:
                sh["src"] = _web_url(sh["src"])
            if p.get("clip"):
                p["clip"]["src"] = _web_url(p["clip"]["src"])
    return pl


# 视口自适应（v45）：高德静态图的 zoom 只能是整数，粒度是 2 倍。
# `_fit_zoom` 选「刚好装得下整条轨迹」的那一档，于是轨迹外框会落在
# 画幅的 50%~100% 之间：偏大的一边就**顶到甚至超出画面边缘**，看起来
# 像「底图被放大裁切」；偏小的一边又缩在一角。两种都是同一件事。
# 这里把底图和轨迹用**同一组**仿射变换一起缩放到目标占比并居中：
# 变换同时作用于底图位图与所有轨迹/照片坐标，地理对齐一点不变。
VIEW_W, VIEW_H = 0.66, 0.70     # 轨迹外框目标占画布宽 / 高的比例
VIEW_CY = 0.53                  # 目标中心略低于正中，给顶部 HUD 让位
VIEW_K_MIN, VIEW_K_MAX = 0.60, 1.50


def _view_fit(pts: list[tuple[float, float]]) -> dict:
    """算 (k, dx, dy)：`(x,y) -> (x*k+dx, y*k+dy)`，底图与轨迹一起用。"""
    if len(pts) < 2:
        return {"k": 1.0, "dx": 0.0, "dy": 0.0, "ratio": [0.0, 0.0]}
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    bw, bh = max(x1 - x0, 1.0), max(y1 - y0, 1.0)
    k = min(W * VIEW_W / bw, H * VIEW_H / bh)
    k = max(VIEW_K_MIN, min(VIEW_K_MAX, k))
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    tx, ty = W / 2, H * VIEW_CY
    return {"k": round(k, 4),
            "dx": round(tx - cx * k, 1), "dy": round(ty - cy * k, 1),
            "ratio": [round(bw / W, 3), round(bh / H, 3)]}


def _project(rt: dict, w: int, h: int) -> list[tuple[float, float]]:
    """无底图时把经纬度等距投影到画布（等比、居中、留边）。"""
    lats, lons = rt["lat"], rt["lon"]
    if not lats:
        return []
    lat0 = sum(lats) / len(lats)
    kx = 111320.0 * math.cos(math.radians(lat0))
    xs = [(lo - lons[0]) * kx for lo in lons]
    ys = [(la - lats[0]) * 110540.0 for la in lats]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    spx, spy = (maxx - minx) or 1.0, (maxy - miny) or 1.0
    pad_x, pad_top, pad_bot = 90, 520, 360
    sc = min((w - 2 * pad_x) / spx, (h - pad_top - pad_bot) / spy)
    ox = (w - spx * sc) / 2
    oy = pad_top + (h - pad_top - pad_bot - spy * sc) / 2
    return [(ox + (x - minx) * sc, oy + (maxy - y) * sc) for x, y in zip(xs, ys)]


def _xy_at(rt: dict, pts: list[tuple[float, float]], sec: float) -> tuple[float, float]:
    """按时间在相邻采样点间插值取画布坐标（照片标记要落在线上）。"""
    t = rt.get("t") or []
    if not t or not pts:
        return (0.0, 0.0)
    import bisect

    i = min(max(bisect.bisect_left(t, sec), 1), len(t) - 1)
    j = i - 1
    span = (t[i] - t[j]) or 1
    wgt = min(max((sec - t[j]) / span, 0.0), 1.0)
    return (pts[j][0] + (pts[i][0] - pts[j][0]) * wgt,
            pts[j][1] + (pts[i][1] - pts[j][1]) * wgt)


# ------------------------------------------------------------------ HTML
def build_html(payload: dict) -> str:
    return (_HTML
            .replace("__DATA__", json.dumps(payload, ensure_ascii=False,
                                            separators=(",", ":")))
            .replace("__W__", str(W)).replace("__H__", str(H))
            .replace("__ACCENT__", payload.get("accent") or ACCENT))


_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>轨迹回放</title>
<style>
*{margin:0;padding:0}
html,body{width:__W__px;height:__H__px;overflow:hidden;background:#080B10}
canvas{display:block}
</style></head><body>
<canvas id="c" width="__W__" height="__H__"></canvas>
<script>
/* 全部画在 canvas 里，没有 DOM 层。
   起因：`canvas.toBlob()` 只能拿到 canvas 自己，DOM 上的角标、进度条、照片卡
   在导出的视频里会整块消失。要么两套渲染逻辑（迟早走形），要么统一到 canvas。
   选后者：播放器和 MP4 是同一份像素。 */
const D = __DATA__;
const cv = document.getElementById('c'), ctx = cv.getContext('2d');
const W = D.w, H = D.h, N = D.t.length;
const px = D.x, py = D.y, T = D.t, DIST = D.d, HR = D.hr;
const ACCENT = D.accent || '__ACCENT__';
const F = '"Microsoft YaHei UI","PingFang SC","Segoe UI",system-ui,sans-serif';

const QS = new URLSearchParams(location.search);
const HTTP = location.protocol === 'http:';
const IMG = new Map();

// 页面由本地 HTTP 服务提供时走相对路径；本地双击打开走 file://
// （payload 里存的是绝对文件路径，不是 URI——URI 会让导出时画布被污染）
function toURL(p) {
  if (!p) return '';
  if (/^(https?|file|data):/i.test(p)) return p;
  const n = p.replace(/\\/g, '/');
  // 已经是同源绝对路径（App 里后端会直接把 `data/` 下的资源改写成
  // `/data/xxx.png`）。这里若再补一个 `/` 就变成 `//data/xxx.png`——
  // 双斜杠是协议相对 URL，浏览器会去找名为 data 的主机，请求压根到不了
  // 本机服务，底图永远加载不出来。
  if (n.startsWith('/')) return n;
  if (!HTTP) return 'file:///' + n;
  const root = (D.root || '').replace(/\\/g, '/');
  return '/' + (n.toLowerCase().startsWith(root.toLowerCase()) ? n.slice(root.length + 1) : n);
}
async function load(src, tries = 3) {
  if (!src) return null;
  // 六路并发向本地服务要同一批大图时，偶尔会有一次请求直接失败（不是慢，是断）。
  // 不重试的后果很隐蔽：这一路的所有帧都少了底图，但帧数齐全，导出不会报错。
  for (let t = 0; t < tries; t++) {
    const im = new Image();
    im.src = toURL(src) + (HTTP && t ? '?r=' + t : '');   // file:// 加 query 会找不到文件
    try {
      await new Promise((res, rej) => { im.onload = res; im.onerror = rej; });
      // 只等 onload 不够：大图可能还没解码完，逐帧导出时 drawImage 会静默跳过
      if (im.decode) await im.decode().catch(() => {});
      return im;
    } catch (e) {
      await new Promise(r => setTimeout(r, 120));
    }
  }
  return null;
}

function rr(x, y, w, h, r) {
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(x, y, w, h, r);
  else ctx.rect(x, y, w, h);
}
function fmtPace(s) {
  if (!s || !isFinite(s)) return '—';
  let m = Math.floor(s / 60), q = Math.round(s - m * 60);
  if (q === 60) { m++; q = 0; }
  return m + "'" + String(q).padStart(2, '0') + '"';
}
function fmtClock(s) {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), q = s % 60;
  return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(q).padStart(2, '0');
}
function idxAt(rt) {
  let lo = 0, hi = N - 1;
  while (lo < hi) { const m = (lo + hi) >> 1; if (T[m] < rt) lo = m + 1; else hi = m; }
  return lo;
}
function paceAt(rt) {
  // 15 秒窗口：1Hz 瞬时配速在 ±40s/km 抖，直接用角标会跳成噪音
  const a = Math.max(rt - 15, 0), ia = idxAt(a), ib = idxAt(rt);
  const dd = DIST[ib] - DIST[ia], dt = T[ib] - T[ia];
  return (dd > 5 && dt > 0) ? dt / dd * 1000 : null;
}

// ---- 时间轴：给「速度」设计曲线，再积分成位置
// 原来是位置折线（KEYS），速度在进出照片处从 1x 硬切到 0.0125x——位置连着，
// 但速度不连续，看起来就是「猛刹一脚、再猛窜出去」。现在改成速度剖面：
// 匀速 → 半余弦减速 0.7s → 完全静止 hold 秒 → 半余弦加速 0.7s，
// 归一化后保证「巡航 + 加减速」正好等于 D.play、终点正好落在 D.dur；
// 素材停留是额外叠加的，见下面 holdsSum。
const RAMP = 0.70;                  // 加减速时长（播放秒）
const PWIN = [];                    // 每处照片的播放时间窗口 {a: 停住开始, b: 停住结束}
const TL = (function () {
  const ps = (D.photos || []).slice().sort((a, b) => a.t - b.t);
  const n = ps.length;
  // 素材停留**叠加**在巡航时长之上，不挤占轨迹节奏：
  //   视频按自身时长整段播完，图片/照片按固定停留。
  // 于是总时长 = D.play（轨迹巡航 + 加减速）+ 所有停留，
  // 也就是「30s + 视频素材总时长 + 3s × 图片素材数」。早期版本把停留塞进
  // D.play 里，素材一多轨迹就被压成几秒飞完全程，看起来像"时长算错了"。
  for (const ph of ps) {
    if (ph.clip) {
      const d = ph.clip.dur || 0;
      // 上限与 Python 侧 HOLD_MAX 保持一致（3600s），只防呆不裁剪：
      // 视频按自身时长整段播完，标签写多久就播多久。
      ph._hold = Math.max(0.6, Math.min(3600, d));
    }
  }
  // 纯照片点（没挂素材）仍按预算平分，同样叠加在 D.play 之上
  const plain = ps.filter(x => !x.clip);
  const clipHold = ps.reduce((a, ph) => a + (ph.clip ? ph._hold : 0), 0);
  const rest = D.play * 0.40;
  const baseHold = plain.length ? Math.min(2.8, Math.max(1.0, rest / plain.length)) : 0;
  for (const x of plain) x._hold = baseHold;
  const holdsSum = clipHold + baseHold * plain.length;

  // 给定 k（轨迹秒/播放秒），排出整条时间轴；总时长随 k 单调递减，二分求 k
  function build(k) {
    const segs = [], win = [];
    let p = 0, r = 0;
    const step = k * RAMP / 2;      // 半余弦段走的距离：平均速度 k/2
    for (const ph of ps) {
      const cruiseTo = Math.max(r, ph.t - step);
      const cruiseDur = (cruiseTo - r) / k;
      if (cruiseDur > 1e-6) segs.push({ p0: p, p1: p + cruiseDur, r0: r, r1: cruiseTo, e: 0 });
      p += cruiseDur; r = cruiseTo;
      segs.push({ p0: p, p1: p + RAMP, r0: r, r1: ph.t, e: -1 });   // 减速
      p += RAMP; r = ph.t;
      win.push({ ph, a: p, b: p + ph._hold });
      segs.push({ p0: p, p1: p + ph._hold, r0: r, r1: r, e: 0 });   // 停住
      p += ph._hold;
      segs.push({ p0: p, p1: p + RAMP, r0: r, r1: r + step, e: 1 });  // 加速
      p += RAMP; r += step;
    }
    if (r < D.dur) segs.push({ p0: p, p1: p + (D.dur - r) / k, r0: r, r1: D.dur, e: 0 });
    p = segs.length ? segs[segs.length - 1].p1 : 0;
    return { segs, win, total: p };
  }

  let lo = 0.5, hi = 8000, best = build(10);
  for (let i = 0; i < 48; i++) {
    const mid = (lo + hi) / 2, t = build(mid);
    // 只把「巡航 + 加减速」压进 D.play；停留是额外加的
    if (t.total - holdsSum > D.play) lo = mid; else { hi = mid; best = t; }
  }
  // 极端情况：素材多到「光加减速就超过巡航预算」，二分无解（best 还停在
  // 初始的 build(10)，会把片子拉成几十秒的慢动作）。退化成轨迹瞬间走完，
  // 至少每条素材都完整放到。
  if (best.total - holdsSum > D.play + 0.05) best = build(8000);
  for (const w of best.win) PWIN.push(Object.assign({}, w.ph, { a: w.a, b: w.b }));
  return best;
})();

// 整条片子的真实总时长：巡航预算 + 所有素材停留
const TOTAL = TL.total;

function routeAt(p) {
  if (p <= 0) return 0;
  if (p >= TOTAL) return D.dur;
  const segs = TL.segs;
  let lo = 0, hi = segs.length - 1;
  while (lo < hi) {                    // 找 p 落在哪一段
    const m = (lo + hi) >> 1;
    if (p > segs[m].p1) lo = m + 1; else hi = m;
  }
  const g = segs[lo];
  const span = g.p1 - g.p0;
  if (!(span > 1e-9)) return g.r1;
  let u = (p - g.p0) / span;
  u = u < 0 ? 0 : u > 1 ? 1 : u;
  if (g.e === 0) return g.r0 + (g.r1 - g.r0) * u;
  // 半余弦：起点速度 = 2x 平均、终点 = 0（减速），反之即加速
  const f = g.e < 0 ? u + Math.sin(Math.PI * u) / Math.PI
                    : u - Math.sin(Math.PI * u) / Math.PI;
  return g.r0 + (g.r1 - g.r0) * f;
}

// 车头位置：在 i-1 与 i 之间按时间插值。直接用 px[i] 是 1 Hz 采样，
// 屏幕上每点差 1.8 px，慢速段会变成「每 2.5 秒跳一下」的台阶。
function headAt(rt) {
  const i = idxAt(rt);
  if (i <= 0) return { i: 0, j: 0, f: 0, x: px[0], y: py[0] };
  const a = T[i - 1], b2 = T[i];
  let f = b2 > a ? (rt - a) / (b2 - a) : 1;
  f = f < 0 ? 0 : f > 1 ? 1 : f;
  return { i: i, j: i - 1, f: f,
           x: px[i - 1] + (px[i] - px[i - 1]) * f,
           y: py[i - 1] + (py[i] - py[i - 1]) * f };
}

// ---- 路径（分段：GPS 丢失处不连直线）
function pathOf(from, to) {
  const p = new Path2D();
  let started = false;
  for (let i = from; i < to; i++) {
    if (i > from && D.seg[i] !== D.seg[i - 1]) started = false;
    if (!started) { p.moveTo(px[i], py[i]); started = true; }
    else p.lineTo(px[i], py[i]);
  }
  return p;
}
let restPath = null, donePath = new Path2D(), doneIdx = 0;

// ---- 底图
// 压暗在开机时烘焙进一张离屏画布：每帧对 1080×1920 做一次 filter 是纯浪费，
// 而且滤镜+缩放在同一帧里叠着做，交互播放会掉帧。
let baseBaked = null;
function bakeBase() {
  const im = IMG.get(D.base);
  if (!im) return;
  const V = D.view || {k: 1, dx: 0, dy: 0};
  const c = document.createElement('canvas');
  c.width = W; c.height = H;
  const g = c.getContext('2d');
  // 视口 k<1 时底图盖不满画布，先铺一层看板底色，边缘才不会露出纯黑
  g.fillStyle = '#0E131A'; g.fillRect(0, 0, W, H);
  // 高德静态图是浅底，压在深色看板上会抢主体，压暗到「看得到路名」就够。
  // 9 参 drawImage：源是全图，目标按 view 变换；Python 侧的轨迹点已用
  // 同一组 (k,dx,dy) 变换过，两者必须严格同源，否则路网和轨迹会错开。
  g.filter = 'brightness(.60) saturate(.72) contrast(1.04)';
  const nw = im.naturalWidth || W, nh = im.naturalHeight || H;
  g.drawImage(im, 0, 0, nw, nh, V.dx, V.dy, W * V.k, H * V.k);
  g.filter = 'none';
  g.fillStyle = 'rgba(9,13,19,.30)';
  g.fillRect(0, 0, W, H);
  baseBaked = c;
}

function drawMap(scale, cx, cy) {
  ctx.save();
  if (scale !== 1) {
    ctx.translate(W / 2, H / 2); ctx.scale(scale, scale); ctx.translate(-cx, -cy);
  }
  if (baseBaked) {
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(baseBaked, 0, 0);
    ctx.imageSmoothingQuality = 'low';
  } else {
    ctx.fillStyle = '#0E131A'; ctx.fillRect(0, 0, W, H);
  }
  ctx.restore();
}

function drawTrack(rt, hd) {
  const i = hd.i;
  if (!restPath) restPath = pathOf(0, N);
  ctx.lineCap = 'round'; ctx.lineJoin = 'round';
  ctx.strokeStyle = 'rgba(6,10,14,.55)'; ctx.lineWidth = 10; ctx.stroke(restPath);
  ctx.strokeStyle = 'rgba(255,255,255,.42)'; ctx.lineWidth = 6; ctx.stroke(restPath);

  // donePath 只走到 i-1：i-1 与车头之间那半个点由车头圆点盖住
  const last = Math.max(i - 1, 0);
  if (last < doneIdx) { donePath = pathOf(0, last + 1); doneIdx = last; }
  for (let k = doneIdx + 1; k <= last; k++) donePath.lineTo(px[k], py[k]);
  if (last > doneIdx) doneIdx = last;
  // 阴影模糊很贵（2856 点 × 26px 模糊），用两层递减 alpha 的描边代替，远看一样
  ctx.strokeStyle = 'rgba(52,211,153,.22)'; ctx.lineWidth = 22; ctx.stroke(donePath);
  ctx.strokeStyle = 'rgba(52,211,153,.30)'; ctx.lineWidth = 15; ctx.stroke(donePath);
  ctx.strokeStyle = ACCENT; ctx.lineWidth = 10; ctx.stroke(donePath);
  ctx.beginPath(); ctx.moveTo(px[last], py[last]); ctx.lineTo(hd.x, hd.y);
  ctx.lineWidth = 10; ctx.stroke();

  // 起点 / 终点：与活动图（actcard._pins）同款——绿圆起、红方终，深色描边；
  // 起终点几乎重合（绕圈）时并成一套：红方块罩在外面、绿点嵌在中心。
  ctx.lineWidth = 3; ctx.strokeStyle = '#0F1319';
  const sx0 = px[0], sy0 = py[0], ex0 = px[N - 1], ey0 = py[N - 1];
  if (Math.hypot(ex0 - sx0, ey0 - sy0) < 26) {
    const hf0 = 13;
    ctx.fillStyle = '#E05555';
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(ex0 - hf0, ey0 - hf0, hf0 * 2, hf0 * 2, 3);
    else ctx.rect(ex0 - hf0, ey0 - hf0, hf0 * 2, hf0 * 2);
    ctx.fill(); ctx.stroke();
    ctx.beginPath(); ctx.arc(ex0, ey0, 7, 0, 7);
    ctx.fillStyle = '#2FBF8F'; ctx.fill();
    ctx.lineWidth = 1.5; ctx.stroke();
  } else {
    ctx.beginPath(); ctx.arc(sx0, sy0, 11, 0, 7);
    ctx.fillStyle = '#2FBF8F'; ctx.fill(); ctx.stroke();
    const hf0 = 11;
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(ex0 - hf0, ey0 - hf0, hf0 * 2, hf0 * 2, 2.5);
    else ctx.rect(ex0 - hf0, ey0 - hf0, hf0 * 2, hf0 * 2);
    ctx.fillStyle = '#E05555'; ctx.fill(); ctx.stroke();
  }

  // 照片点
  for (const p of (D.photos || [])) {
    const done = rt >= p.t;
    ctx.beginPath(); ctx.arc(p.x, p.y, done ? 15 : 11, 0, 7);
    ctx.fillStyle = done ? '#fff' : 'rgba(255,255,255,.6)'; ctx.fill();
    ctx.lineWidth = 5; ctx.strokeStyle = ACCENT; ctx.stroke();
  }

  // 车头（插值位置，不是离散轨迹点）
  ctx.beginPath(); ctx.arc(hd.x, hd.y, 26, 0, 7);
  ctx.fillStyle = 'rgba(52,211,153,.16)'; ctx.fill();
  ctx.beginPath(); ctx.arc(hd.x, hd.y, 21, 0, 7); ctx.fillStyle = '#fff'; ctx.fill();
  ctx.beginPath(); ctx.arc(hd.x, hd.y, 11, 0, 7); ctx.fillStyle = ACCENT; ctx.fill();
  return i;
}

function drawShades() {
  let g = ctx.createLinearGradient(0, 0, 0, 520);
  g.addColorStop(0, 'rgba(6,9,13,.93)');
  g.addColorStop(.45, 'rgba(6,9,13,.72)');
  g.addColorStop(1, 'rgba(6,9,13,0)');
  ctx.fillStyle = g; ctx.fillRect(0, 0, W, 520);
  g = ctx.createLinearGradient(0, H - 430, 0, H);
  g.addColorStop(0, 'rgba(6,9,13,0)');
  g.addColorStop(.55, 'rgba(6,9,13,.72)');
  g.addColorStop(1, 'rgba(6,9,13,.95)');
  ctx.fillStyle = g; ctx.fillRect(0, H - 430, W, 430);
}

// ---- 左上角数据卡
function drawHud(hd, rt) {
  const i = hd.i, j = hd.j, f = hd.f;
  ctx.textBaseline = 'alphabetic'; ctx.textAlign = 'left';
  ctx.letterSpacing = '4px';
  ctx.font = '600 22px ' + F; ctx.fillStyle = '#7C8A99';
  ctx.fillText('轨迹回放', 52, 100);
  ctx.letterSpacing = '0px';

  ctx.shadowColor = 'rgba(0,0,0,.6)'; ctx.shadowBlur = 18;
  ctx.font = '700 44px ' + F; ctx.fillStyle = '#EEF3F8';
  ctx.fillText(D.title, 52, 156);
  ctx.font = '400 21px ' + F; ctx.fillStyle = '#8C99A8';
  ctx.fillText(D.sub, 52, 194);
  ctx.shadowBlur = 0; ctx.shadowColor = 'transparent';

  // 面板
  ctx.save();
  ctx.shadowColor = 'rgba(0,0,0,.45)'; ctx.shadowBlur = 50; ctx.shadowOffsetY = 18;
  rr(52, 220, 506, 238, 26);
  ctx.fillStyle = 'rgba(16,21,28,.76)'; ctx.fill();
  ctx.restore();
  rr(52, 220, 506, 238, 26);
  ctx.strokeStyle = 'rgba(255,255,255,.08)'; ctx.lineWidth = 1.5; ctx.stroke();

  // 里程（同样插值：1 Hz 采样直接取值会一秒一跳）
  const km = ((DIST[j] + (DIST[i] - DIST[j]) * f) / 1000).toFixed(2);
  ctx.font = '700 92px ' + F; ctx.fillStyle = '#fff';
  ctx.fillText(km, 78, 328);
  const kw = ctx.measureText(km).width;
  ctx.font = '600 30px ' + F; ctx.fillStyle = '#93A1B0';
  ctx.fillText('km', 78 + kw + 8, 328);

  // 三格
  const hrAt = (k) => { const v = HR[k]; return v == null ? null : v; };
  const hrNow = () => {
    const a = hrAt(j), b2 = hrAt(i);
    if (a == null && b2 == null) return '—';
    if (a == null) return String(Math.round(b2));
    if (b2 == null) return String(Math.round(a));
    return String(Math.round(a + (b2 - a) * f));
  };
  const cells = [
    ['配速', fmtPace(paceAt(rt)), '/km', '#E9EFF6'],
    ['心率', hrNow(), 'bpm', '#FF7A6B'],
    ['用时', fmtClock(rt), '', '#E9EFF6'],
  ];
  let x = 78;
  for (const [k, v, u, color] of cells) {
    ctx.font = '400 18px ' + F; ctx.fillStyle = '#788696';
    ctx.fillText(k, x, 386);
    ctx.font = '700 37px ' + F; ctx.fillStyle = color;
    ctx.fillText(v, x, 428);
    if (u) {
      const vw = ctx.measureText(v).width;
      ctx.font = '600 20px ' + F; ctx.fillStyle = '#8494A3';
      ctx.fillText(u, x + vw + 4, 428);
    }
    x += k === '配速' ? 168 : 172;
  }
}

// ---- 底部进度
function drawFoot(rt) {
  const x0 = 52, x1 = W - 52, y = 1806;
  ctx.textAlign = 'right'; ctx.font = '400 14px ' + F; ctx.fillStyle = '#4E5A68';
  ctx.fillText('地图数据 © 高德地图', x1, 1778);
  if (VIDS.size && !audioOn && QS.get('export') !== '1') {
    ctx.font = '400 16px ' + F; ctx.fillStyle = 'rgba(255,255,255,.38)';
    ctx.fillText('点击画面开启原声（浏览器默认静音自动播放）', x1, 1746);
  }
  ctx.textAlign = 'left';

  rr(x0, y, x1 - x0, 8, 4); ctx.fillStyle = 'rgba(255,255,255,.16)'; ctx.fill();
  const pct = Math.max(0, Math.min(1, rt / Math.max(D.dur, 1)));
  const wx = (x1 - x0) * pct;
  if (wx > 2) {
    const g = ctx.createLinearGradient(x0, 0, x0 + wx, 0);
    g.addColorStop(0, 'rgba(52,211,153,.55)'); g.addColorStop(1, ACCENT);
    rr(x0, y, wx, 8, 4); ctx.fillStyle = g; ctx.fill();
  }
  const hx = x0 + wx;
  ctx.beginPath(); ctx.arc(hx, y + 4, 11, 0, 7); ctx.fillStyle = '#fff';
  ctx.shadowColor = 'rgba(52,211,153,.85)'; ctx.shadowBlur = 26; ctx.fill();
  ctx.shadowBlur = 0;

  ctx.font = '400 19px ' + F; ctx.fillStyle = '#7E8C9B';
  ctx.fillText('起点', x0, 1852);
  ctx.textAlign = 'right'; ctx.fillText('终点', x1, 1852);
  ctx.textAlign = 'center';
  ctx.fillText((D.dist / 1000).toFixed(2) + ' km · ' + fmtClock(D.dur), (x0 + x1) / 2, 1852);
  ctx.textAlign = 'left';
}

// ---- 照片卡 / 实况卡
// 视频用 <video> + drawImage 直接上画布：实测 1080×1920 逐帧 seek 2.6 ms，
// 比"先抽成帧序列"省得多（抽帧 389 KB/帧，2 秒就 23 MB），也不用担心解码内存。
const VIDS = new Map();
function ensureVideo(src) {
  if (!src) return null;
  let v = VIDS.get(src);
  if (v) return v;
  v = document.createElement('video');
  v.src = toURL(src);
  v.muted = true;                 // 自动播放得先静音，用户点了再开声音
  v.playsInline = true; v.preload = 'auto';
  VIDS.set(src, v);
  return v;
}

// 真机 WebView 里 `seeked` 只代表定位完成，不代表这一帧已经解码好；
// 直接 drawImage(video) 会画出黑帧，成片里就是「视频卡片黑屏频闪」。
// 每帧先用 24×24 小画布探一下平均亮度，只有确实有画面才更新缓存；
// 黑帧一律复用上一张有效画面 —— 最坏是短暂停住，绝不会闪黑。
const VLAST = new Map();          // src -> {c, w, h}
const VPROBE = document.createElement('canvas');
VPROBE.width = 24; VPROBE.height = 24;
const vpctx = VPROBE.getContext('2d', {willReadFrequently: true});

// 首帧兜底：视频元素 loadeddata 之后先抓一帧存住。
// 导出时是多路 Chrome 并行渲染，每路进程各自维护 VLAST——某一路的
// 视频元素还没解出当前帧时，VLAST 里是空的，videoFrame 返回 null，
// 卡片就画成空框（成片里表现为「某几处视频黑屏」）。
// 首帧兜底保证任何时刻都有画面可画，最差也只是画面停在开头那一帧。
const VFIRST = new Map();         // src -> {c, w, h}
function grabFirst(v, src) {
  try {
    const w = v.videoWidth || 1080, h = v.videoHeight || 1920;
    const c = document.createElement('canvas');
    c.width = w; c.height = h;
    c.getContext('2d').drawImage(v, 0, 0, w, h);
    if (!VFIRST.has(src)) VFIRST.set(src, {c: c, w: w, h: h});
  } catch (e) { /* 首帧抓不到就等后面的兜底 */ }
}

function videoFrame(src, v, w, h) {
  let lit = false;
  try {
    // readyState < 2 时 drawImage 会抛，或画出一张空图——两种都当「没画面」
    if (v.readyState >= 2) {
      vpctx.drawImage(v, 0, 0, 24, 24);
      const d = vpctx.getImageData(0, 0, 24, 24).data;
      let sum = 0;
      for (let i = 0; i < d.length; i += 4) sum += d[i] + d[i + 1] + d[i + 2];
      lit = sum > 24 * 24 * 3 * 8;   // 平均亮度 > 8/255 才算有画面
    }
  } catch (e) { lit = false; }
  if (lit) {
    let rec = VLAST.get(src);
    if (!rec || rec.w !== w || rec.h !== h) {
      const c = document.createElement('canvas');
      c.width = w; c.height = h;
      rec = {c: c, w: w, h: h};
      VLAST.set(src, rec);
    }
    rec.c.getContext('2d').drawImage(v, 0, 0, w, h);
    return rec.c;
  }
  const rec = VLAST.get(src);
  if (rec) return rec.c;           // 沿用上一张有效画面
  const first = VFIRST.get(src);
  if (first) {
    // 首帧按目标尺寸重画一遍：首帧画布是原始分辨率，直接拉伸会变形
    if (first.w !== w || first.h !== h) {
      const c = document.createElement('canvas');
      c.width = w; c.height = h;
      c.getContext('2d').drawImage(first.c, 0, 0, w, h);
      return c;
    }
    return first.c;
  }
  return null;
}

function popLayout(p) {
  if (p.clip) {
    const c = p.clip;
    const k = Math.min(840 / c.w, 780 / c.h, 1.5);
    return { cells: [[Math.round(c.w * k), Math.round(c.h * k)]], crop: false };
  }
  const shots = (p.shots || []).slice(0, 2);
  // 单张等比完整显示（裁掉内容最可惜）；两张统一成同样大小的格子 + 中心裁切，
  // 尺寸不一致的两张并排会像贴歪的照片墙。
  if (shots.length <= 1) {
    const s0 = shots[0] || { w: 1200, h: 900 };
    const iw = s0.w || 1200, ih = s0.h || 900;
    const k = Math.min(840 / iw, 760 / ih, 1.5);
    return { cells: [[Math.round(iw * k), Math.round(ih * k)]], crop: false };
  }
  const w = 404, allLand = shots.every(s => s.w > s.h), allPort = shots.every(s => s.h >= s.w);
  const h = allLand ? Math.round(w * 0.67) : (allPort ? Math.round(w * 1.38) : w);
  return { cells: [[w, h], [w, h]], crop: true };
}

// 每张照片按「格子的尺寸 + 裁切方式」预渲染成一块小画布，之后每帧只做一次拷贝。
// 不这么做的话，每帧都要把 3 MB 的原图缩放着画一遍：慢，而且多开浏览器时
// Chrome 会把解码数据回收掉，drawImage 静默跳过——成片里就会少一张。
const CELL = new Map();
function cellOf(s, im, w, h, crop) {
  const key = s.src + '|' + w + 'x' + h + (crop ? '|c' : '');
  let cv2 = CELL.get(key);
  if (cv2) return cv2;
  cv2 = document.createElement('canvas');
  cv2.width = w; cv2.height = h;
  const c2 = cv2.getContext('2d');
  const nw = im.naturalWidth || s.w, nh = im.naturalHeight || s.h;
  if (crop) {
    const k = Math.max(w / nw, h / nh), sw = w / k, sh = h / k;
    c2.drawImage(im, (nw - sw) / 2, (nh - sh) / 2, sw, sh, 0, 0, w, h);
  } else {
    c2.drawImage(im, 0, 0, w, h);
  }
  CELL.set(key, cv2);
  return cv2;
}

function drawCell(im, s, x, y, w, h, crop) {
  if (!im) return;
  ctx.drawImage(cellOf(s, im, w, h, crop), x, y);
}

function drawPop(p, e) {
  const isClip = !!(p.clip);
  const shots = isClip ? [] : (p.shots || []).slice(0, 2);
  const lay = popLayout(p);
  const gap = 10, pad = 16, capH = 88;
  const iw = lay.cells.reduce((a, c) => a + c[0], 0) + gap * (lay.cells.length - 1);
  const ih = Math.max(...lay.cells.map(c => c[1]));
  const pw = iw + pad * 2, ph = ih + pad * 2 + capH;
  const cx = W / 2;
  const cy = Math.max(492 + ph / 2, Math.min(1030, 1660 - ph / 2));
  const left = cx - pw / 2, top = cy - ph / 2;
  const sc = 0.90 + 0.10 * e;

  ctx.save();
  ctx.globalAlpha = e;
  ctx.translate(cx, cy); ctx.scale(sc, sc); ctx.translate(-cx, -cy);

  // 底板：说明文字得落在深色上，直接压亮色底图根本看不清
  ctx.save();
  ctx.shadowColor = 'rgba(0,0,0,.8)'; ctx.shadowBlur = 70; ctx.shadowOffsetY = 26;
  rr(left, top, pw, ph, 30); ctx.fillStyle = 'rgba(13,18,24,.97)'; ctx.fill();
  ctx.restore();

  ctx.save();
  rr(left, top, pw, ph, 30); ctx.clip();
  if (isClip) {
    const [w, h] = lay.cells[0];
    if (p.clip.kind === 'image') {
      // 图片素材：和照片走同一套裁剪铺满卡片，不走 <video>
      const im = IMG.get(p.clip.src);
      if (im) drawCell(im, p.clip, left + pad, top + pad, w, h, true);
      else { ctx.fillStyle = '#141A22'; ctx.fillRect(left + pad, top + pad, w, h); }
    } else {
      const v = VIDS.get(p.clip.src);
      // readyState < 2 也不直接画空框：videoFrame 还有首帧兜底，
      // 画出来总比空框好（空框在成片里就是一处黑屏）。
      const frame = v ? videoFrame(p.clip.src, v, w, h) : null;
      if (frame) ctx.drawImage(frame, left + pad, top + pad, w, h);
      else { ctx.fillStyle = '#141A22'; ctx.fillRect(left + pad, top + pad, w, h); }
    }
  } else {
    let x = left + pad;
    for (let k = 0; k < shots.length; k++) {
      const [w, h] = lay.cells[k];
      drawCell(IMG.get(shots[k].src), shots[k], x, top + pad, w, h, lay.crop);
      x += w + gap;
    }
  }
  ctx.restore();

  rr(left, top, pw, ph, 30);
  ctx.strokeStyle = 'rgba(255,255,255,.14)'; ctx.lineWidth = 1.5; ctx.stroke();

  // 说明文字贴在底板下沿；右侧里程是后画的右对齐文本——先量出它的左缘，
  // 说明文字超宽就截断加省略号，再拿裁剪兜底，保证永不叠字
  const ty = top + ph - 32;
  const name = isClip ? p.clip.name : ((shots[0] && shots[0].name) || '');
  const more = !isClip && (p.shots || []).length > 2 ? '  +' + (p.shots.length - 2) : '';
  ctx.font = '700 28px ' + F;
  const kmTxt = (p.d / 1000).toFixed(2) + ' km';
  const kmLeft = left + pw - pad - 2 - ctx.measureText(kmTxt).width;
  ctx.font = '400 21px ' + F;
  ctx.textAlign = 'left';
  const capX = left + pad + (isClip ? 20 : 2);
  const capW = kmLeft - capX - 10;
  const clipTag = isClip
    ? (p.clip.kind === 'image' ? '图片' : (p.clip.dur || 0).toFixed(1) + 's')
    : '';
  let capTxt = isClip ? name + '  ' + clipTag : name + more;
  if (ctx.measureText(capTxt).width > capW) {
    while (capTxt.length > 1 && ctx.measureText(capTxt + '…').width > capW) capTxt = capTxt.slice(0, -1);
    capTxt += '…';
  }
  ctx.save();
  ctx.beginPath();
  ctx.rect(left + pad, ty - 30, capX + capW - (left + pad), 42);
  ctx.clip();
  if (isClip && p.clip.kind !== 'image') {   // 小三角标一下这是会动的
    ctx.fillStyle = ACCENT;
    ctx.beginPath();
    ctx.moveTo(left + pad + 2, ty - 16);
    ctx.lineTo(left + pad + 14, ty - 10);
    ctx.lineTo(left + pad + 2, ty - 4);
    ctx.closePath(); ctx.fill();
  }
  ctx.fillStyle = isClip ? '#A9B7C6' : '#8D9AA8';
  ctx.fillText(capTxt, capX, ty);
  ctx.restore();
  ctx.textAlign = 'right'; ctx.font = '700 28px ' + F; ctx.fillStyle = '#E6EDF5';
  ctx.fillText(kmTxt, left + pw - pad - 2, ty);

  const approx = isClip ? !!p.clip.approx : !!(shots[0] && shots[0].approx);
  const gapM = isClip ? p.clip.gap : (shots[0] ? shots[0].gap : 0);
  if (approx) {
    ctx.textAlign = 'center'; ctx.font = '400 18px ' + F; ctx.fillStyle = '#7E8C9B';
    ctx.fillText('文件里没有坐标，按拍摄时间定位', cx, top - 26);
  } else if (gapM > 40) {
    ctx.textAlign = 'center'; ctx.font = '400 18px ' + F; ctx.fillStyle = '#8B98A7';
    ctx.fillText('定位偏离轨迹 ' + Math.round(gapM) + ' m，已吸附到最近点', cx, top - 26);
  }
  ctx.restore();
  ctx.textAlign = 'left';
}

// 导出走"精确 seek"，播放走"让视频自己跑"——每帧都 seek 的话播放会卡成幻灯
let videoDriven = false;
let lastClipSrc = null;

// 等视频能出画面（readyState >= 2）。
// 以前 readyState < 2 就直接返回，结果是：不 seek → 画不出帧 → 下一帧继续
// 这么等，循环往复到 60 秒超时。表现就是「某一段帧一直渲不出来」，
// 而页面诊断里 vidOk 还是 true（就绪检查是在 boot 时做的，早就过了）。
function waitReady(v, ms) {
  if (!v || v.readyState >= 2) return Promise.resolve();
  return new Promise(res => {
    let hit = false;
    const done = () => { if (!hit) { hit = true; res(); } };
    v.addEventListener('loadeddata', done, { once: true });
    v.addEventListener('canplay', done, { once: true });
    v.addEventListener('error', done, { once: true });
    setTimeout(done, ms || 4000);
    try { if (v.networkState === 3) v.load(); } catch (e) { done(); }
  });
}

async function syncVideo(p) {
  for (const [src, v] of VIDS) {
    if (!p || !p.clip || p.clip.src !== src) { if (!v.paused) v.pause(); }
  }
  if (!p || !p.clip) return;
  const v = VIDS.get(p.clip.src);
  if (!v) return;
  // 视频还没解出画面：先等它就绪，否则后面的 seek 无从谈起
  if (v.readyState < 2) await waitReady(v, 4000);
  if (v.readyState < 2) return;
  const rel = playT - p.a - (p.clip.lead || 0);
  const want = Math.max(0, Math.min(rel, (p.clip.dur || 1) - 0.02));
  if (Math.abs(v.currentTime - want) <= 0.04) return;
  await new Promise(res => {
    let tm = null;
    const done = () => { if (tm) clearTimeout(tm); res(); };
    tm = setTimeout(done, 350);
    v.addEventListener('seeked', done, { once: true });
    try { v.currentTime = want; } catch (err) { done(); }
  });
}

function driveVideo(p) {
  for (const [src, v] of VIDS) {
    const on = p && p.clip && p.clip.src === src;
    if (!on) { if (!v.paused) v.pause(); continue; }
    const rel = playT - p.a - (p.clip.lead || 0);
    if (rel < 0 || rel >= (p.clip.dur || 1)) {
      if (!v.paused) v.pause();
      if (rel < 0 && v.currentTime > 0.05) v.currentTime = 0;
    } else if (v.paused) {
      v.play().catch(() => {});
    }
  }
}

const ZOOM = 0.12;                 // 镜头最多推近 12%：再多静态图就糊了
const FADE_IN = 0.22, FADE_OUT = 0.18;
const smooth = u => u <= 0 ? 0 : u >= 1 ? 1 : u * u * (3 - 2 * u);

// 镜头只由「播放时间」决定：
//   [a-RAMP, a] 推近   [a, b] 完全静止（这一整段画面逐像素相同）   [b, b+RAMP] 退回
// 锚点固定在照片点，不做平移——锚点在动的话，缩放和位移会同时发生，眼睛跟不住。
function camAt(p) {
  let e = 0, w0 = null;
  for (const w of PWIN) {
    if (p < w.a - RAMP || p > w.b + RAMP) continue;
    if (p <= w.a) e = smooth((p - (w.a - RAMP)) / RAMP);
    else if (p >= w.b) e = smooth(1 - (p - w.b) / RAMP);
    else e = 1;
    w0 = w;
    break;
  }
  const scale = 1 + ZOOM * e;
  let cx = W / 2, cy = H / 2;
  if (w0) { cx = w0.x; cy = w0.y; }
  const mx = W * (1 - 1 / scale) / 2, my = H * (1 - 1 / scale) / 2;
  cx = Math.min(Math.max(cx, W / 2 - mx), W / 2 + mx);
  cy = Math.min(Math.max(cy, H / 2 - my), H / 2 + my);
  return { scale, cx, cy };
}

function popAt(p) {
  let k = 0, w0 = null;
  for (const w of PWIN) {
    if (p < w.a || p > w.b) continue;
    k = Math.min(1, (p - w.a) / FADE_IN, (w.b - p) / FADE_OUT);
    w0 = w;
    break;
  }
  return { k: Math.max(0, Math.min(1, k)), w: w0 };
}

function drawFrame(playT) {
  const rt = routeAt(playT);
  const hd = headAt(rt);
  const cam = camAt(playT);

  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, W, H);
  drawMap(cam.scale, cam.cx, cam.cy);

  ctx.save();
  if (cam.scale !== 1) {
    ctx.translate(W / 2, H / 2); ctx.scale(cam.scale, cam.scale);
    ctx.translate(-cam.cx, -cam.cy);
  }
  drawTrack(rt, hd);
  ctx.restore();

  drawShades();
  drawHud(hd, rt);
  drawFoot(rt);
  const pop = popAt(playT);
  // 换素材窗口（或退出窗口）时丢掉上一张缓存画面，否则新窗口第一帧
  // 若是黑的，会闪一下上一段素材的末帧。播放与导出都会走这里。
  const curClip = (pop.w && pop.w.clip) ? pop.w.clip.src : null;
  if (curClip !== lastClipSrc) { VLAST.clear(); lastClipSrc = curClip; }
  if (videoDriven) driveVideo(pop.w);
  if (pop.w && pop.k > 0) drawPop(pop.w, smooth(pop.k));
}

// ---- 播放
let playT = 0, last = 0, playing = true, raf = null;
function loop(ts) {
  if (!last) last = ts;
  playT += (ts - last) / 1000; last = ts;
  if (playT >= TOTAL) playT = TOTAL;
  drawFrame(playT);
  if (playT < TOTAL) raf = requestAnimationFrame(loop);
  else playing = false;
}
function play() {
  playing = true; last = 0; videoDriven = true;
  if (raf) cancelAnimationFrame(raf);
  raf = requestAnimationFrame(loop);
}
function pause() {
  playing = false; videoDriven = false;
  if (raf) cancelAnimationFrame(raf);
  for (const v of VIDS.values()) if (!v.paused) v.pause();
}
// 导出的每一帧都要求"画面停在某个确定时刻"，所以这里要等视频 seek 到位再画
async function seekTo(t) {
  pause();
  playT = Math.max(0, Math.min(t, TOTAL));
  await syncVideo(popAt(playT).w);
  drawFrame(playT);
  return playT;
}

// ---- 导出：页面自己逐帧渲染 + JPEG 编码，POST 回本地服务写盘。
// 质量 0.82：0.94 时 1080×1920 单帧约 455KB，手机端 480 帧要占 200MB+；
// 0.82 约 180KB，肉眼几乎无差，编码器还会再压一道。
// 比「每帧起一个 Chrome 截图」快一个数量级（后者约 1.8 秒/帧）。
async function runExport() {
  const total = parseInt(QS.get('total') || '0', 10);
  const fps = parseFloat(QS.get('fps') || '30');
  const from = parseInt(QS.get('from') || '0', 10);
  pause();
  const run = QS.get('run') || '';
  // 每段视频在播放时间轴上到底从第几秒开始，由这里算完回传——
  // 音频混流要用它，省得 Python 再照抄一份二分算法
  try {
    const rows = PWIN.map(w => ({
      t: w.t, a: +w.a.toFixed(3), b: +w.b.toFixed(3),
      // 图片素材没有音轨，dur 必须给 0，否则 ffmpeg 会去一个 jpg 里抽音频
      dur: (w.clip && w.clip.kind !== 'image') ? w.clip.dur : 0,
      lead: w.clip ? (w.clip.lead || 0) : 0,
      src: (w.clip && w.clip.kind !== 'image') ? w.clip.src : '',
      // 磁盘绝对路径：桌面 ffmpeg 与手机 MediaCodec 都从它读原声。
      // src 在 App 里是 /data/... 的同源 URL，只能给 WebView，不能拿去解码。
      file: (w.clip && w.clip.kind !== 'image') ? (w.clip.local || w.clip.src) : '',
    }));
    await fetch('/__timeline?run=' + run, { method: 'POST', body: JSON.stringify(rows) });
  } catch (e) { /* 没有视频就不用报 */ }
  for (let i = from; i < total; i++) {
    await seekTo(i / fps);
    const blob = await new Promise(res => cv.toBlob(res, 'image/jpeg', 0.82));
    await fetch('/__frame?i=' + i + '&run=' + run, { method: 'POST', body: blob });
  }
  // 必须是 POST：服务端只在 do_POST 里写完成标记，GET 会 404，
  // 调用方就会一直等 done 而等不到
  await fetch('/__done?from=' + from + '&run=' + run, { method: 'POST' });
}

window.__motion = {
  duration: TOTAL, seek: seekTo, play: play, pause: pause,
  frames: N, ready: false,
};

(async function boot() {
  // 先铺一层底色：底图要等加载+解码，这期间画布是透明的，截图/录屏会闪一下黑
  ctx.fillStyle = '#0E131A'; ctx.fillRect(0, 0, W, H);

  const need = [], clips = [];
  if (D.base) need.push(D.base);
  for (const p of (D.photos || [])) {
    for (const s of (p.shots || [])) need.push(s.src);
    if (p.clip) {
      // 图片素材进图片队列，视频才进 <video>；两条加载路径不能混
      if (p.clip.kind === 'image') need.push(p.clip.src);
      else clips.push(p.clip);
    }
  }

  for (let round = 0; round < 3; round++) {
    const todo = need.filter(u => !IMG.has(u));
    if (!todo.length) break;
    await Promise.all(todo.map(u => load(u).then(im => im && IMG.set(u, im))));
  }
  // 视频走 <video>，不能混在图片队列里：只等到能出画面（loadeddata）为止
  await Promise.all(clips.map(c => new Promise(res => {
    const v = ensureVideo(c.src);
    if (!v) return res();
    // loadeddata 一到就抓首帧存住：多路并行渲染时，某一路进程可能还没
    // 解出当前帧，没有首帧兜底那几处卡片就会画成空框（成片里是黑屏）。
    v.addEventListener('loadeddata', () => grabFirst(v, c.src), { once: true });
    v.addEventListener('canplay', () => grabFirst(v, c.src), { once: true });
    if (v.readyState >= 2) { grabFirst(v, c.src); return res(); }
    let hit = false;
    const done = () => { if (!hit) { hit = true; res(); } };
    v.addEventListener('loadeddata', done, { once: true });
    v.addEventListener('error', done, { once: true });
    setTimeout(done, 6000);
    try { v.load(); } catch (e) { done(); }
  })));
  bakeBase();

  const missing = need.filter(u => !IMG.has(u));
  const vidOk = clips.every(c => {
    const v = VIDS.get(c.src);
    return v && v.readyState >= 2;
  });
  if (missing.length) console.warn('资源没加载全：' + missing.join(', '));
  if (clips.length && !vidOk) console.warn('视频没能解码：' + clips.map(c => c.name).join(', '));

  // 只有底图是硬条件：缺了它整段都是黑画面，而帧数照样齐，导出端发现不了。
  // 照片/视频缺了只是那几处难看，不能因此整段不出片——之前把三者绑在一起，
  // 手机上底图 404 就表现为「一直 0 帧，页面也不报错」。
  window.__motion.ready = !!baseBaked;
  const bim = IMG.get(D.base);
  window.__motion.diag = {
    base: !!baseBaked, missing: missing.length, missingOne: missing.slice(0, 3),
    clips: clips.length, vidOk: vidOk,
    // 真机上「底图缩放错」时靠这两个数定位：view 是本次的视口变换，
    // baseSize 是 WebView 实际解码出来的底图像素尺寸。
    view: D.view || null,
    baseSize: bim ? [bim.naturalWidth, bim.naturalHeight] : [0, 0],
    names: clips.filter(c => {
      const v = VIDS.get(c.src); return !v || v.readyState < 2;
    }).map(c => c.name).slice(0, 3),
  };
  drawFrame(0);

  if (QS.get('export') === '1') {
    // 先把诊断回传：出了问题时原生能说清是底图、照片还是视频，
    // 而不是让用户对着转圈猜。
    try {
      await fetch('/__diag?run=' + (QS.get('run') || ''), {
        method: 'POST', body: JSON.stringify(window.__motion.diag) });
    } catch (e) { /* 上报失败不影响导出 */ }
    if (!window.__motion.ready) return;
    runExport(); return;
  }
  if (QS.has('t')) { await seekTo(parseFloat(QS.get('t')) || 0); return; }
  play();
})();

// 浏览器默认禁止带声音的自动播放：点一下画面即可开原声
let audioOn = false;
window.addEventListener('pointerdown', () => {
  audioOn = true;
  for (const v of VIDS.values()) v.muted = false;
}, { once: true });

window.addEventListener('keydown', e => {
  if (e.code === 'Space') { playing ? pause() : play(); e.preventDefault(); }
  if (e.code === 'ArrowRight') seekTo(playT + 2);
  if (e.code === 'ArrowLeft') seekTo(playT - 2);
});
</script></body></html>
"""


# ------------------------------------------------------------------ 对外入口
def render(row: dict, detail: dict, photos: list[dict] | None,
           out_html: Path, *, duration: float = 45.0,
           thumb_dir: Path | None = None, clips: list[dict] | None = None,
           clip_cache: Path | None = None, web: bool = False,
           payload_out: dict | None = None) -> Path:
    """生成播放器 HTML。返回路径（同目录另存一份 payload 便于排查）。

    `payload_out`：传一个 dict 进来，就把这次实际用的 payload 填进去。
    调用方要拿 `total`（真实总时长）算帧数时用得上，比再从 HTML 里正则抠可靠。
    """
    out_html = Path(out_html)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    payload = build_payload(row, detail, photos, clips=clips, thumb_dir=thumb_dir,
                            clip_cache=clip_cache, duration=duration, web=web)
    if payload_out is not None:
        payload_out.update(payload)
    out_html.write_text(build_html(payload), encoding="utf-8")
    meta = {k: payload[k] for k in ("title", "sub", "dur", "dist", "play", "total")}
    meta["photos"] = [{"t": p["t"], "d": p["d"],
                       "n": len(p.get("shots") or []),
                       "clip": (p.get("clip") or {}).get("name")}
                      for p in payload["photos"]]
    # 音频混流要用：每段视频的源文件与自身时长（具体在播放时间轴的哪个
    # 位置开始播，由页面算完时间轴后回传，免得两边各写一份二分算法）
    meta["clip_src"] = [p["clip"] for p in payload["photos"] if p.get("clip")]
    meta["loose"] = payload["loose"]
    out_html.with_suffix(".json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return out_html


def open_in_browser(path: Path) -> None:
    from .sysopen import open_any

    open_any(Path(path).resolve())
