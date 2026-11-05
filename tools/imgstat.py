"""PNG 像素统计：不依赖 Pillow，用来客观校验出图结果。

能回答的问题：
  * 某个区域是不是空的（全背景色）
  * 横幅/曲线/色条到底画上没有（非背景像素数量）
  * 底部有没有异常的大片空白

用法：
  python tools/imgstat.py output/act-YYYY-MM-DD.png            # 整体分带统计
  python tools/imgstat.py output/x.png --band 340:860          # 只看某一段
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path


def load_png(path: Path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "不是 PNG"
    pos, idat, w = 8, b"", None
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", body[:10])
            assert depth == 8, f"只支持 8bit，当前 {depth}"
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
        pos += 12 + ln
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    raw = zlib.decompress(idat)
    stride = w * channels
    out = bytearray(h * stride)
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        f = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if f == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif f == 3:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif f == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return w, h, channels, out


BG = (15, 19, 25)   # 页面底色 #0F1319
TOL = 14


def is_bg(r, g, b):
    return abs(r - BG[0]) <= TOL and abs(g - BG[1]) <= TOL and abs(b - BG[2]) <= TOL


def report(path: Path, band: tuple[int, int] | None = None, steps: int = 14):
    w, h, ch, px = load_png(path)
    print(f"{path.name}  {w}x{h}  通道 {ch}")
    lo, hi = band if band else (0, h)
    step = max(1, (hi - lo) // steps)
    print("   y 范围        非背景占比   平均亮度   主色(近似)")
    for y0 in range(lo, hi, step):
        y1 = min(hi, y0 + step)
        n = ink = 0
        sr = sg = sb = 0
        for y in range(y0, y1, 2):
            base = y * w * ch
            for x in range(0, w, 3):
                i = base + x * ch
                r, g, b = px[i], px[i + 1], px[i + 2]
                n += 1
                sr += r
                sg += g
                sb += b
                if not is_bg(r, g, b):
                    ink += 1
        print(f"  {y0:>5}-{y1:<5}  {ink / n * 100:>7.1f}%   "
              f"{(sr + sg + sb) / (3 * n):>6.1f}    "
              f"#{sr // n:02x}{sg // n:02x}{sb // n:02x}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    band = None
    for a in sys.argv[1:]:
        if a.startswith("--band"):
            v = a.split("=", 1)[1]
            a0, a1 = v.split(":")
            band = (int(a0), int(a1))
    if args:
        target = Path(args[0])
    else:
        cands = sorted((Path(__file__).resolve().parent.parent / "output").glob("act-*.png"))
        if not cands:
            raise SystemExit("用法：python tools/imgstat.py <path/to/act-*.png>")
        target = cands[-1]
    report(target, band)
