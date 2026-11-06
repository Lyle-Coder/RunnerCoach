"""造测试照片：把任意图片转成带 GPS EXIF 的 JPEG，按轨迹位置摆好。

用途：没有真实照片时验证「轨迹回放到照片点 → 镜头推近 → 弹出照片」链路。
真实照片进来后这个脚本就不用了，所以放在 tools/ 而不是 garmin_cn/。

    python tools/mk_test_photos.py --id <activity_id> --src output/_gen
    python tools/mk_test_photos.py --id <activity_id> --src output/_gen --clean

它会读库里的轨迹（activity_detail.payload.route），按「目标里程 + 偏离距离」
反推经纬度，写进 EXIF 的 GPS IFD，拍摄时间取该点的 UTC 时间。这样照片
一定会落在活动时段与 80 m 吸附半径内——除非你刻意让它不落。
"""
from __future__ import annotations

import argparse
import datetime as dt
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from garmin_cn import photo as PH  # noqa: E402
from garmin_cn.store import Store  # noqa: E402

EARTH_M = 111320.0        # 1° 纬度对应的米数（够用，别当真值）

# (目标里程 m, 偏离轨迹 m, 方位角°, 画幅, 是否旋转存盘)
# 旋转存盘 = 手机竖拍最常见的那种：像素横着存、靠 EXIF Orientation=6 转回来
SHOTS = [
    (1600, 12, 35, "land", False),
    (4200, 28, 200, "port", False),
    (7000, 46, 110, "land", False),
    (7010, 52, 115, "port", True),      # 与前一张同地 → 并排显示
    (3000, 15, 80, "land", False),      # 故意迟到 3h45m → 应判「不在活动时段」
]
LATE = dt.timedelta(hours=3, minutes=45)


def ffmpeg() -> str:
    for c in (ROOT / "tools" / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"),
              Path("ffmpeg")):
        if Path(c).exists():
            return str(c)
    raise SystemExit("找不到 ffmpeg：把 ffmpeg.exe 放到 tools/ 下")


def dims(path: Path) -> tuple[int, int]:
    """用 ffmpeg 的 stderr 读尺寸（没装 ffprobe，这样够用）。"""
    out = subprocess.run([ffmpeg(), "-hide_banner", "-i", str(path)],
                         capture_output=True, text=True).stderr
    for line in out.splitlines():
        if "Video:" in line:
            for tok in line.replace(",", " ").split():
                if "x" in tok and tok.replace("x", "").isdigit():
                    w, h = tok.split("x")
                    return int(w), int(h)
    raise SystemExit(f"读不出尺寸：{path.name}")


def to_jpeg(src: Path, dst: Path, rotate: bool) -> None:
    """PNG 转 JPEG 并缩到 1600 长边（模拟手机原图的量级，不至于拖慢截帧）。

    先切掉底部 8%：生成图右下角有「AI 生成」角标，留着会像成片的水印。
    旋转必须在裁切之后——旋转存盘的文件用 EXIF 转回来会精确还原原图，
    所以只有「原图底部」裁掉才等于「看到的底部」裁掉。
    """
    vf = "crop=iw:floor(ih*0.92):0:0,"
    if rotate:
        vf += "transpose=2,"
    vf += "scale='if(gt(iw,ih),min(1600,iw),-2)':'if(gt(iw,ih),-2,min(1600,ih))'"
    r = subprocess.run([ffmpeg(), "-y", "-loglevel", "error", "-i", str(src),
                        "-vf", vf, "-q:v", "3", str(dst)], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"转码失败 {src.name}：{r.stderr[-300:]}")


def _rat(v: float) -> bytes:
    d = int(v)
    m = int((v - d) * 60)
    s = round(((v - d) * 60 - m) * 60, 4)
    return struct.pack("<IIIIII", d, 1, m, 1, int(s * 10000), 10000)


def build_app1(*, lat: float, lon: float, alt: float, when_utc: dt.datetime,
               when_local: dt.datetime, w: int, h: int, orient: int,
               make: str = "Xiaomi", model: str = "Xiaomi 17 Pro") -> bytes:
    """拼一段 APP1（Exif）载荷：IFD0 + ExifIFD + GPSIFD。"""
    def asc(x: str) -> bytes:
        return x.encode() + b"\x00"

    n0, nex, ng = 6, 5, 8
    o0 = 8
    oe = o0 + (2 + n0 * 12 + 4)
    og = oe + (2 + nex * 12 + 4)
    od = og + (2 + ng * 12 + 4)
    pool = bytearray()

    def add(v: bytes) -> int:
        off = od + len(pool)
        pool.extend(v)
        if len(v) % 2:
            pool.append(0)
        return off

    mk, md = add(asc(make)), add(asc(model))
    dto = add(asc(when_local.strftime("%Y:%m:%d %H:%M:%S")))
    offs = add(asc("+08:00"))
    la, lo = add(_rat(lat)), add(_rat(lon))
    tm = add(struct.pack("<IIIIII", when_utc.hour, 1, when_utc.minute, 1,
                         int(when_utc.second * 100), 100))
    gd = add(asc(when_utc.strftime("%Y:%m:%d")))
    altb = add(struct.pack("<II", int(alt * 100), 100))

    def ent(tag: int, typ: int, cnt: int, val: bytes) -> bytes:
        return struct.pack("<HHI", tag, typ, cnt) + val

    ifd0 = (struct.pack("<H", n0)
            + ent(0x010F, 2, len(asc(make)), struct.pack("<I", mk))
            + ent(0x0110, 2, len(asc(model)), struct.pack("<I", md))
            + ent(0x0112, 3, 1, struct.pack("<HH", orient, 0))
            + ent(0x0131, 2, 4, b"\x02\x03\x00\x00")          # Software
            + ent(0x8769, 4, 1, struct.pack("<I", oe))
            + ent(0x8825, 4, 1, struct.pack("<I", og))
            + struct.pack("<I", 0))
    ex = (struct.pack("<H", nex)
          + ent(0x9003, 2, 20, struct.pack("<I", dto))        # "YYYY:MM:DD HH:MM:SS"+NUL
          + ent(0x9011, 2, 7, struct.pack("<I", offs))        # "+08:00"+NUL
          + ent(0xA002, 4, 1, struct.pack("<I", w))
          + ent(0xA003, 4, 1, struct.pack("<I", h))
          + ent(0xA403, 3, 1, struct.pack("<HH", 0, 0))       # WhiteBalance 自动
          + struct.pack("<I", 0))
    gp = (struct.pack("<H", ng)
          + ent(0x0001, 2, 2, b"N\x00\x00\x00")
          + ent(0x0002, 5, 3, struct.pack("<I", la))
          + ent(0x0003, 2, 2, b"E\x00\x00\x00")
          + ent(0x0004, 5, 3, struct.pack("<I", lo))
          + ent(0x0005, 1, 1, b"\x00\x00\x00\x00")
          + ent(0x0006, 5, 1, struct.pack("<I", altb))
          + ent(0x0007, 5, 3, struct.pack("<I", tm))
          + ent(0x001D, 2, 11, struct.pack("<I", gd))         # "YYYY:MM:DD"+NUL
          + struct.pack("<I", 0))
    tiff = b"II*\x00" + struct.pack("<I", o0) + ifd0 + ex + gp + bytes(pool)
    app1 = b"Exif\x00\x00" + tiff
    return b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1


def inject(src: Path, dst: Path, **kw) -> dict:
    """在 SOI 之后插入 APP1，其余字节原样保留（不重编码，画质零损失）。"""
    buf = src.read_bytes()
    if not buf.startswith(b"\xff\xd8"):
        raise SystemExit(f"{src.name} 不是 JPEG")
    w, h = PH._jpeg_size(buf)
    seg = build_app1(w=w, h=h, **kw)
    dst.write_bytes(buf[:2] + seg + buf[2:])
    return {"w": w, "h": h, "bytes": dst.stat().st_size}


def offset(lat: float, lon: float, meters: float, bearing: float) -> tuple[float, float]:
    import math

    b = math.radians(bearing)
    dlat = meters * math.cos(b) / EARTH_M
    dlon = meters * math.sin(b) / (EARTH_M * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def at_distance(rt: dict, meters: float) -> int:
    """里程最接近 meters 的轨迹下标。"""
    best, bi = 1e18, 0
    for i, d in enumerate(rt["d"]):
        if abs(d - meters) < best:
            best, bi = abs(d - meters), i
    return bi


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="造带 GPS 的测试照片")
    ap.add_argument("--id", required=True, help="活动 ID")
    ap.add_argument("--src", default="output/_gen", help="素材图目录（png/jpg）")
    ap.add_argument("--out", default="", help="输出目录，默认 data/photos/<id>")
    ap.add_argument("--clean", action="store_true", help="先清空输出目录里的旧样张")
    a = ap.parse_args(argv)

    store = Store(ROOT / "data" / "garmin.db")
    detail = store.get_detail(a.id)
    rt = (detail or {}).get("route") or {}
    if not rt.get("n"):
        print("× 这次活动没有轨迹时间轴，先跑：run.bat motion --id " + a.id)
        return 2

    srcs = sorted(p for p in Path(a.src).iterdir()
                  if p.suffix.lower() in {".png", ".jpg", ".jpeg"})
    if not srcs:
        print(f"× {a.src} 下没有素材图")
        return 2
    # 按画幅分组：横图配横拍、竖图配竖拍，别把 16:9 硬转成 9:16 糊一片
    land = [p for p in srcs if dims(p)[0] > dims(p)[1]]
    port = [p for p in srcs if dims(p)[1] >= dims(p)[0]]
    pool = {"land": land or port, "port": port or land}
    print(f"· 素材横图 {len(land)} 张、竖图 {len(port)} 张")

    out = Path(a.out) if a.out else (ROOT / "data" / "photos" / a.id)
    out.mkdir(parents=True, exist_ok=True)
    if a.clean:
        for p in out.glob("IMG_*.jpg"):
            p.unlink()

    t0 = dt.datetime.fromtimestamp(rt["t0"] / 1000, dt.timezone.utc)
    stamp = t0.astimezone(dt.timezone(dt.timedelta(hours=8)))
    print(f"· 轨迹 {rt['n']} 点，{rt['d'][-1] / 1000:.2f} km，"
          f"开始 {stamp:%m-%d %H:%M:%S}（本地）")

    made = []
    seen = {"land": 0, "port": 0}
    for n, (meters, gap, bearing, want, rotated) in enumerate(SHOTS):
        i = at_distance(rt, meters)
        lat, lon = offset(rt["lat"][i], rt["lon"][i], gap, bearing)
        when = t0 + dt.timedelta(seconds=rt["t"][i]) + dt.timedelta(seconds=6)
        if n == 4:
            when += LATE                      # 刻意错开时段
        local = when.astimezone(dt.timezone(dt.timedelta(hours=8)))
        # 文件名模仿小米相机：IMG_yyyyMMdd_HHmmss
        name = f"IMG_{local:%Y%m%d_%H%M%S}.jpg"
        tmp = out / ("_tmp_" + name)
        cand = pool[want]
        src = cand[seen[want] % len(cand)]
        seen[want] += 1
        to_jpeg(src, tmp, rotate=rotated)
        orient = 6 if rotated else 1
        info = inject(tmp, out / name, lat=lat, lon=lon, alt=420.0,
                      when_utc=when, when_local=local, orient=orient)
        tmp.unlink(missing_ok=True)
        made.append((name, local, meters, gap, orient, info))
        print(f"  √ {name}  {local:%H:%M:%S}  {meters / 1000:.2f} km处"
              f"  偏离 {gap} m  Orientation={orient}  "
              f"存 {info['w']}×{info['h']}  {info['bytes'] // 1024} KB"
              f"  ← {src.name[:12]}")

    (out / "_TEST_PHOTOS.txt").write_text(
        "这些是 tools/mk_test_photos.py 生成的合成样张，用来测试轨迹回放的照片链路。\n"
        "GPS 坐标按轨迹里程反推、时间取该点时刻，并非真实拍摄，请勿当作真实素材。\n"
        "真实照片放进来后删掉本目录全部 IMG_*.jpg 即可。\n", encoding="utf-8")
    print(f"\n√ 共 {len(made)} 张 → {out}")
    print("  下一步：run.bat photo --id " + a.id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
