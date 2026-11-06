"""底图对齐自检：把高德自己画的折线和我方投影的折线叠在一张图上。

为什么需要这个
--------------
「地图位置错了」这类问题肉眼很难判断到底是投影错、缩放错还是平移错。高德静态
地图支持 `paths` 参数，让它自己按它的投影把轨迹画在图上；我们再按自己的
`to_px` 把同一条轨迹画一遍。两张线重合 → 投影与取景都对；分叉 → 比例错；
整体平移 → 中心/坐标系错。

用法：
    py tools/mapcheck.py --id <activity_id>
输出：
    output/_mapcheck_raw.png    高德自己画折线的原图（无滤镜）
    output/_mapcheck_shot.png   我方折线叠在上面的对照图（洋红=我方）
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from garmin_cn import basemap as B            # noqa: E402
from garmin_cn.actcard import TRK_CH, TRK_CW, _find_browser  # noqa: E402

DB = ROOT / "data" / "garmin.db"


def load_track(aid: int) -> list:
    c = sqlite3.connect(DB)
    row = c.execute("select payload from activity_detail where activity_id=?",
                    (aid,)).fetchone()
    if not row:
        raise SystemExit(f"数据库里没有 {aid} 的明细，先跑 act --id {aid}")
    return json.loads(row[0])["track"]


def fetch_with_paths(track: list, meta: dict, key: str, out: Path) -> Path:
    """让高德自己把轨迹画上去，用来对照我方投影。"""
    pts = [B.to_gcj(p[0], p[1]) for p in track]
    step = max(1, len(pts) // 180)                    # 控制 URL 长度
    pts = pts[::step] + [pts[-1]]
    seq = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in pts)
    # paths 格式：paths=weight,color,transparency,fillcolor,fillTransparency:lon,lat;lon,lat
    url = (f"{B.API}?location={meta['center'][0]:.6f},{meta['center'][1]:.6f}"
           f"&zoom={meta['zoom']}&size={meta['w']}*{meta['h']}&scale=2"
           f"&paths=6,0xEE1111,1,,:{seq}&key={key}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = resp.read()
    if not data.startswith(b"\x89PNG"):
        raise SystemExit("高德没回图片：" + data[:160].decode("utf-8", "ignore"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return out


def build_html(track: list, meta: dict, png: Path) -> str:
    iw, ih = meta["w"], meta["h"]
    pts = [B.to_px(p[0], p[1], meta) for p in track]
    d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    # 我方折线画成洋红细线，压在红色粗线（高德画的）上面，是否贴合一眼可见
    return f"""<!doctype html><meta charset="utf-8">
<body style="margin:0;background:#111">
<svg width="{iw}" height="{ih}" viewBox="0 0 {iw} {ih}"
     xmlns="http://www.w3.org/2000/svg">
  <image href="{png.resolve().as_uri()}" x="0" y="0" width="{iw}" height="{ih}"
         preserveAspectRatio="none"/>
  <path d="{d}" fill="none" stroke="#FF00FF" stroke-width="1.4" opacity="0.95"/>
</svg>
<div style="color:#ddd;font:14px monospace;padding:6px">
 zoom={meta['zoom']} s={meta['s']} img={iw}x{ih} center={meta['center']}
</div></body>"""


def px_raw(lat: float, lon: float, meta: dict) -> tuple[float, float]:
    """已经是 GCJ 的坐标直接投影（不再纠偏）。

    高德返回的路线/POI 本来就是 GCJ，若再过一遍 `to_px`（里面含 WGS→GCJ）
    会被再推 400 m，比例尺一错就永远对不上——这是坐标出问题时最容易犯的错。
    """
    x, y = B._world(lat, lon, meta["zoom"])
    cx, cy = B._world(meta["center"][1], meta["center"][0], meta["zoom"])
    return x - cx + meta["w"] / 2, y - cy + meta["h"] / 2


def walking(a: tuple, b: tuple, key: str) -> list[tuple[float, float]]:
    """高德步行规划：返回的折线一定压在它自己的路网上，可当「路面金标准」。"""
    url = (f"https://restapi.amap.com/v3/direction/walking"
           f"?origin={a[1]:.6f},{a[0]:.6f}&destination={b[1]:.6f},{b[0]:.6f}&key={key}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        j = json.loads(resp.read().decode("utf-8", "ignore"))
    if str(j.get("status")) != "1":
        raise SystemExit("步行规划失败：" + json.dumps(j, ensure_ascii=False)[:200])
    pts: list[tuple[float, float]] = []
    for st in ((j.get("route") or {}).get("paths") or [{}])[0].get("steps", []):
        for pair in (st.get("polyline") or "").split(";"):
            if "," in pair:
                lon, lat = pair.split(",")
                pts.append((float(lat), float(lon)))
    return pts


def _seg_dist(p, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / (dx * dx + dy * dy or 1e-9)))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(p[0] - cx, p[1] - cy), (cx - p[0], cy - p[1])


def roadcheck(track: list, meta: dict, key: str, out_dir: Path) -> None:
    """拿高德步行规划当路网标准，量「轨迹相对路面偏了多少」。

    只验几何的 mapcheck（paths 对照）看不出坐标系错——两套线都用同一个
    变换，错也一起错。这里换成高德自己算的路：它必然贴在底图路网上，于是
    轨迹与它的**横向**距离就等价于「坐标系是否对」。
    """
    i0, i1 = 0, min(len(track) - 1, int(len(track) * 0.4))
    route = walking(track[i0], track[i1], key)
    seg = track[i0:i1 + 1]

    iw, ih = meta["w"], meta["h"]
    wl = [px_raw(p[0], p[1], meta) for p in route]
    pts = [B.to_px(p[0], p[1], meta) for p in seg]

    def stats(pl: list) -> tuple[float, float, float]:
        rows = [min((_seg_dist(p, wl[i], wl[i + 1]) for i in range(len(wl) - 1)),
                    key=lambda t: t[0]) for p in pl[::4]]
        d = statistics.median(r[0] for r in rows)
        return d, statistics.median(r[1][0] for r in rows), statistics.median(r[1][1] for r in rows)

    d, vx, vy = stats(pts)
    bad = [px_raw(p[0], p[1], meta) for p in seg]        # 原样直投（= 不纠偏）作对照
    d0, _, _ = stats(bad)
    m_per_px = 78271.51696 * math.cos(math.radians(meta["center"][1])) / (2 ** meta["zoom"])
    print(f"路面距离中位数 {d:.1f} px（≈{d * m_per_px:.0f} m），"
          f"横向偏移 ({vx:+.1f},{vy:+.1f}) px；不纠偏时 {d0:.1f} px（≈{d0 * m_per_px:.0f} m）")
    if d > d0 * 0.6 and d0 > 10:
        print("  ⚠ 纠偏反而更远：数据可能已经是 GCJ，设 AMAP_DATUM=gcj")

    html = out_dir / "_roadcheck.html"
    d1 = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    d2 = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in wl)
    html.write_text(f"""<!doctype html><meta charset="utf-8">
<body style="margin:0;background:#111">
<svg width="{iw}" height="{ih}" viewBox="0 0 {iw} {ih}" xmlns="http://www.w3.org/2000/svg">
  <image href="{(out_dir / '_roadcheck_bm.png').resolve().as_uri()}"
         x="0" y="0" width="{iw}" height="{ih}" preserveAspectRatio="none"/>
  <path d="{d2}" fill="none" stroke="#00C853" stroke-width="2.4" opacity="0.9"/>
  <path d="{d1}" fill="none" stroke="#FF00FF" stroke-width="1.6" opacity="0.95"/>
</svg></body>""", encoding="utf-8")
    shot = out_dir / "_roadcheck.png"
    subprocess.run([_find_browser(), "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--force-device-scale-factor=2",
                    f"--window-size={iw},{ih}", f"--screenshot={shot}",
                    html.resolve().as_uri()], capture_output=True, timeout=90)
    print("路面金标准对照图:", shot)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=int, required=True)
    ap.add_argument("--w", type=int, default=TRK_CW)
    ap.add_argument("--h", type=int, default=TRK_CH)
    ap.add_argument("--road", action="store_true",
                    help="用高德步行规划当路网标准，检查坐标系（推荐）")
    a = ap.parse_args()

    key = B.key()
    if not key:
        raise SystemExit("没有高德 Key，先跑 run.bat key --set ...")
    track = load_track(a.id)
    meta = B.fit(track, a.w, a.h)
    print("取景:", {k: meta[k] for k in ("zoom", "s", "w", "h", "center")})
    print("坐标基准:", "WGS→GCJ 纠偏" if B.DATUM == "wgs84" else "原样（视为已是 GCJ）")

    if a.road:
        bm = ROOT / "output" / "_roadcheck_bm.png"
        bm.write_bytes(urllib.request.urlopen(urllib.request.Request(
            B.static_url(meta, key), headers={"User-Agent": "Mozilla/5.0"}),
            timeout=30).read())
        roadcheck(track, meta, key, ROOT / "output")
        return

    raw = fetch_with_paths(track, meta, key, ROOT / "output" / "_mapcheck_raw.png")
    html = ROOT / "output" / "_mapcheck.html"
    html.write_text(build_html(track, meta, raw), encoding="utf-8")
    out = ROOT / "output" / "_mapcheck_shot.png"
    browser = _find_browser()
    subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--force-device-scale-factor=2",
                    f"--window-size={meta['w']},{meta['h'] + 60}",
                    f"--screenshot={out}", html.resolve().as_uri()],
                   capture_output=True, timeout=90)
    print("对照图:", out)


if __name__ == "__main__":
    main()
