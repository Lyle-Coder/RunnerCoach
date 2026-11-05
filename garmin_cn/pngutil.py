"""最小 PNG 工具：纯标准库读写，用于裁掉截图底部多余的空白。

Chrome 无头截图的实际高度 = `--window-size` 高度，但页面视口比它少 95px
（Windows 上固定偏差）。此前靠肉眼调高度，容易出现底部一条空白或内容被切。
这里统一处理：按视口高度裁齐，保证成品尺寸精确。
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def read_png(path: Path) -> tuple[int, int, list[bytearray]]:
    """返回 (宽, 高, 每行 RGB 字节)。支持 8bit 非隔行的真彩 / 调色板 PNG。

    调色板（ctype=3）是给高德静态地图的对齐自检用的：带 labels 的图会回成
    索引色，得展开成 RGB 才能按颜色找标记。
    """
    data = Path(path).read_bytes()
    if not data.startswith(_PNG_SIG):
        raise ValueError("不是 PNG")
    pos, idat, w, h, ctype, depth = 8, b"", 0, 0, 2, 8
    plte = b""
    while pos < len(data):
        (ln,) = struct.unpack(">I", data[pos:pos + 4])
        typ = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", body[:10])
        elif typ == b"PLTE":
            plte = body
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
    if depth != 8 or ctype not in (2, 3, 6):
        raise ValueError(f"暂不支持 depth={depth} ctype={ctype}")
    bpp = {2: 3, 3: 1, 6: 4}[ctype]
    raw = zlib.decompress(idat)
    stride = w * bpp + 1
    prev = bytearray(w * bpp)
    rows: list[bytearray] = []
    for y in range(h):
        f = raw[y * stride]
        line = bytearray(raw[y * stride + 1:(y + 1) * stride])
        if f == 1:
            for i in range(bpp, len(line)):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif f == 2:
            for i in range(len(line)):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(len(line)):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(len(line)):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(a, b, c)) & 255
        prev = bytearray(line)            # 反滤波后的整行，供下一行做预测
        if bpp == 4:                      # 丢掉 alpha，统一输出 RGB
            line = bytearray(b for i, b in enumerate(line) if i % 4 != 3)
        elif bpp == 1:                    # 调色板 → RGB
            out = bytearray(w * 3)
            for i, idx in enumerate(line):
                out[i * 3:i * 3 + 3] = plte[idx * 3:idx * 3 + 3]
            line = out
        rows.append(line)
    return w, h, rows


def write_png(path: Path, w: int, h: int, rows: list[bytearray]) -> None:
    raw = b"".join(b"\x00" + bytes(r) for r in rows[:h])

    def chunk(typ: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + typ + body
                + struct.pack(">I", zlib.crc32(typ + body) & 0xFFFFFFFF))

    out = (_PNG_SIG
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6))
           + chunk(b"IEND", b""))
    Path(path).write_bytes(out)


def crop_height(path: Path, height: int) -> Path:
    """把图片底部裁到 height 像素（超出的部分丢弃）。"""
    w, h, rows = read_png(path)
    if h <= height:
        return path
    write_png(path, w, height, rows)
    return path


def pad_to_9x16(path: Path, bg=(15, 19, 25), max_pad: int = 240) -> Path:
    """把底部用背景色补齐到标准 9:16（1080×1920）。

    成图高度按内容裁，短卡片会落到 1910 之类的不规则值。手机相册、
    分享面板、微信预览对「非 9:16 竖图」的缩略图裁切规则各不相同，
    统一画幅后各处预览都不会再多切一刀；内容本身不动，只在底部补留白。
    """
    w, h, rows = read_png(path)
    target = round(w * 16 / 9)
    if h >= target or target - h > max_pad:
        return path
    pad = bytearray(bytes(bg) * w)
    write_png(path, w, target, rows + [bytearray(pad) for _ in range(target - h)])
    return path


def content_height(path: Path, bg=(15, 19, 25), tol: int = 6) -> int:
    """从下往上找最后一行非背景像素，返回内容底边（用于诊断留白）。"""
    w, h, rows = read_png(path)
    for y in range(h - 1, -1, -1):
        row = rows[y]
        for x in range(0, w, 4):
            i = x * 3
            if (abs(row[i] - bg[0]) > tol or abs(row[i + 1] - bg[1]) > tol
                    or abs(row[i + 2] - bg[2]) > tol):
                return y + 1
    return 0
