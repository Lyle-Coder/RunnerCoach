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

import json
import math
import os
import subprocess
from pathlib import Path

from . import basemap, clip as clip_mod, photo as photo_mod, route as route_mod
from .metrics import num

W, H = 1080, 1920
# 静态图按 540×960 请求 + scale=2 → 1080×1920，与画布 1:1
BASE_W, BASE_H = 540, 960
PAD = 44                 # 取景内边距（BASE 坐标系）
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
        "fit": "motion", "rev": 1,
        "center": [round((min(lons) + max(lons)) / 2, 6),
                   round((min(lats) + max(lats)) / 2, 6)],
        "zoom": _fit_zoom(track), "w": BASE_W, "h": BASE_H, "s": 1.0,
        "frame": [W, H], "pad": PAD,
    }


def ensure_basemap(track: list[list[float]], ident: str) -> dict | None:
    """取底图（带缓存）。没有 Key 或接口失败返回 None，调用方回落到纯色底。"""
    if len(track) < 2:
        return None
    cache = basemap.CACHE / f"motion-{ident}.png"
    meta_f = cache.with_name(cache.stem + ".json")
    if cache.exists() and cache.stat().st_size > 1024 and meta_f.exists():
        try:
            meta = json.loads(meta_f.read_text("utf-8"))
            if meta.get("fit") == "motion" and meta.get("rev") == 1 \
                    and meta.get("frame") == [W, H]:
                return {"png": cache, "meta": meta}
        except (OSError, ValueError):
            pass
    k = basemap.key()
    if not k:
        return None
    meta = meta_of(track)
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
CLIP_LEAD = 0.15        # 卡片淡入完成后才起播，避免"贴脸突然有声"


def build_payload(row: dict, detail: dict, photos: list[dict] | None = None,
                  *, clips: list[dict] | None = None, thumb_dir: Path | None = None,
                  clip_cache: Path | None = None, duration: float = 45.0,
                  region: str = "") -> dict:
    """把活动行 + 明细 + 照片/视频整理成播放器要的一份 JSON。"""
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
    else:
        # 没底图：自己投影到画布，留出上下给文字
        pts = _project(rt, W, H)
        gps = False

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

    # ---- 视频片段：同一套吸附规则，挂到已有的照片点上；挂不上就自己成一个点
    out_clips = 0
    if clips:
        cg = photo_mod.match(clips, rt, t0_ms=rt.get("t0"),
                             keep=("dur", "codec", "bytes"))
        loose.extend(cg["loose"])
        for g in cg["points"]:
            if not g["shots"]:
                continue
            # 一段点位上可能有多段，取最长的那段（短的通常是误触）
            pick = max(g["shots"], key=lambda x: x.get("dur") or 0)
            if not pick.get("dur"):
                continue
            src = clip_mod.ensure_playable(pick, clip_cache or (ROOT / "data" / "motion_cache"))
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
    return {
        "w": W, "h": H,
        "title": row.get("name") or "跑步",
        "sub": " · ".join(x for x in [started,
                                      (bm or {}).get("meta", {}).get("region") or region] if x),
        "base": str(bm["png"].resolve()) if bm else "", "gps": gps,
        "root": str(ROOT),
        "t": rt["t"], "x": [round(p[0], 1) for p in pts],
        "y": [round(p[1], 1) for p in pts],
        "d": rt["d"], "hr": rt["hr"], "seg": rt["seg"],
        "dur": int(s["duration_s"]), "dist": round(s["distance_m"], 1),
        "hr_avg": s.get("hr_avg"), "hr_max": s.get("hr_max"),
        "play": float(duration),
        "photos": out_photos, "clips": out_clips, "loose": loose,
        "accent": ACCENT,
    }


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
// 归一化后保证总时长正好等于 D.play、终点正好落在 D.dur。
const RAMP = 0.70;                  // 加减速时长（播放秒）
const PWIN = [];                    // 每处照片的播放时间窗口 {a: 停住开始, b: 停住结束}
const TL = (function () {
  const ps = (D.photos || []).slice().sort((a, b) => a.t - b.t);
  const n = ps.length;
  // 停留时长按点算：有实况的按片段长度（要播完），照片点平分剩下的预算
  const budget = D.play * 0.40;
  let used = 0;
  for (const ph of ps) {
    if (ph.clip) {
      // 整段放完：卡片停留 = 素材本身长度 + 一点收尾，方便后期再剪
      ph._hold = Math.min(40, Math.max(1.2, ph.clip.dur + 0.6));
      used += ph._hold;
    }
  }
  const plain = ps.filter(x => !x.clip);
  const rest = Math.max(budget - used, D.play * 0.12);
  const baseHold = plain.length ? Math.min(2.8, Math.max(1.0, rest / plain.length)) : 0;
  for (const x of plain) x._hold = baseHold;

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
    if (t.total > D.play) lo = mid; else { hi = mid; best = t; }
  }
  for (const w of best.win) PWIN.push(Object.assign({}, w.ph, { a: w.a, b: w.b }));
  return best;
})();

function routeAt(p) {
  if (p <= 0) return 0;
  if (p >= D.play) return D.dur;
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
  const c = document.createElement('canvas');
  c.width = W; c.height = H;
  const g = c.getContext('2d');
  // 高德静态图是浅底，压在深色看板上会抢主体，压暗到「看得到路名」就够
  g.filter = 'brightness(.60) saturate(.72) contrast(1.04)';
  g.drawImage(im, 0, 0, W, H);
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
    const v = VIDS.get(p.clip.src), [w, h] = lay.cells[0];
    if (v && v.readyState >= 2) {
      ctx.drawImage(v, left + pad, top + pad, w, h);
    } else {
      ctx.fillStyle = '#141A22'; ctx.fillRect(left + pad, top + pad, w, h);
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
  let capTxt = isClip ? name + '  ' + (p.clip.dur || 0).toFixed(1) + 's' : name + more;
  if (ctx.measureText(capTxt).width > capW) {
    while (capTxt.length > 1 && ctx.measureText(capTxt + '…').width > capW) capTxt = capTxt.slice(0, -1);
    capTxt += '…';
  }
  ctx.save();
  ctx.beginPath();
  ctx.rect(left + pad, ty - 30, capX + capW - (left + pad), 42);
  ctx.clip();
  if (isClip) {                       // 小三角标一下这是会动的
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
async function syncVideo(p) {
  for (const [src, v] of VIDS) {
    if (!p || !p.clip || p.clip.src !== src) { if (!v.paused) v.pause(); }
  }
  if (!p || !p.clip) return;
  const v = VIDS.get(p.clip.src);
  if (!v || v.readyState < 2) return;
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
  if (videoDriven) driveVideo(pop.w);
  if (pop.w && pop.k > 0) drawPop(pop.w, smooth(pop.k));
}

// ---- 播放
let playT = 0, last = 0, playing = true, raf = null;
function loop(ts) {
  if (!last) last = ts;
  playT += (ts - last) / 1000; last = ts;
  if (playT >= D.play) playT = D.play;
  drawFrame(playT);
  if (playT < D.play) raf = requestAnimationFrame(loop);
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
  playT = Math.max(0, Math.min(t, D.play));
  await syncVideo(popAt(playT).w);
  drawFrame(playT);
  return playT;
}

// ---- 导出：页面自己逐帧渲染 + JPEG 编码，POST 回本地服务写盘。
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
      dur: w.clip ? w.clip.dur : 0,
      lead: w.clip ? (w.clip.lead || 0) : 0,
      src: w.clip ? w.clip.src : '',
    }));
    await fetch('/__timeline?run=' + run, { method: 'POST', body: JSON.stringify(rows) });
  } catch (e) { /* 没有视频就不用报 */ }
  for (let i = from; i < total; i++) {
    await seekTo(i / fps);
    const blob = await new Promise(res => cv.toBlob(res, 'image/jpeg', 0.94));
    await fetch('/__frame?i=' + i + '&run=' + run, { method: 'POST', body: blob });
  }
  await fetch('/__done?from=' + from + '&run=' + run);
}

window.__motion = {
  duration: D.play, seek: seekTo, play: play, pause: pause,
  frames: N, ready: false,
};

(async function boot() {
  // 先铺一层底色：底图要等加载+解码，这期间画布是透明的，截图/录屏会闪一下黑
  ctx.fillStyle = '#0E131A'; ctx.fillRect(0, 0, W, H);

  const need = [], clips = [];
  if (D.base) need.push(D.base);
  for (const p of (D.photos || [])) {
    for (const s of (p.shots || [])) need.push(s.src);
    if (p.clip) clips.push(p.clip);
  }

  for (let round = 0; round < 3; round++) {
    const todo = need.filter(u => !IMG.has(u));
    if (!todo.length) break;
    await Promise.all(todo.map(u => load(u).then(im => im && IMG.set(u, im))));
  }
  // 视频走 <video>，不能混在图片队列里：只等到能出画面（loadeddata）为止
  await Promise.all(clips.map(c => new Promise(res => {
    const v = ensureVideo(c.src);
    if (!v || v.readyState >= 2) return res();
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

  // 资源不齐就不出帧：缺了底图会得到一整段黑画面，而帧数还是齐的，
  // 导出端根本发现不了。宁可让这一路空着，让上层补渲。
  window.__motion.ready = !!baseBaked && missing.length === 0 && vidOk;
  drawFrame(0);

  if (QS.get('export') === '1') {
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
           clip_cache: Path | None = None) -> Path:
    """生成播放器 HTML。返回路径（同目录另存一份 payload 便于排查）。"""
    out_html = Path(out_html)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    payload = build_payload(row, detail, photos, clips=clips, thumb_dir=thumb_dir,
                            clip_cache=clip_cache, duration=duration)
    out_html.write_text(build_html(payload), encoding="utf-8")
    meta = {k: payload[k] for k in ("title", "sub", "dur", "dist", "play")}
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
