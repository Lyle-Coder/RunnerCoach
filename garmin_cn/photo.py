"""读照片 EXIF（纯标准库），并把照片吸附到轨迹上。

为什么自己解析
--------------
项目一贯不引第三方依赖（连 PNG 都是 `pngutil.py` 手写的），而读 GPS 只需要
JPEG APP1 里那段 TIFF。装 Pillow 要 40 MB，装 exiftool 还要额外进程，为两个
浮点数不值当。

三个必须处理对的点
------------------
1. **时间基准**：`DateTimeOriginal` 是相机本地时间，**不带时区**；而
   `GPSTimeStamp` / `GPSDateStamp` 是 UTC。两者都用时优先 GPS——相机时钟跑偏
   十几分钟是常事，而轨迹是 UTC 毫秒，错位会直接把照片挂到错误的位置。
   没有 GPS 时间时退回 `DateTimeOriginal + OffsetTimeOriginal`，再没有就按
   +08:00 处理（国内无夏令时，固定偏移安全）。
2. **方向**：手机竖拍常存成横向 + `Orientation` 6/8，宽高在 EXIF 里是**存储**
   方向的尺寸。算缩略图盒子时要换成视觉方向，否则 16:9 的照片会被塞进 9:16 框。
3. **HEIF**：小米可能存 `.heic`，纯 Python 解不了（不是 TIFF 结构），直接报错
   让人先转 JPG，别静默失败。

坐标是 **WGS-84**，和高德底图的 GCJ-02 差 400~500 m，投图前必须走
`basemap.to_gcj()`——这一步留给调用方，本模块只负责把原始值读准。
"""
from __future__ import annotations

import datetime as dt
import math
import os
import struct
from pathlib import Path

# EXIF 类型 → 字节数
_TYPE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}
# 需要的标签
T_ORIENT = 0x0112
T_MAKE, T_MODEL = 0x010F, 0x0110
T_EXIF_IFD, T_GPS_IFD = 0x8769, 0x8825
T_DT_ORIG = 0x9003
T_OFFSET_ORIG = 0x9011
T_PIX_W, T_PIX_H = 0xA002, 0xA003
G_LAT_REF, G_LAT = 0x0001, 0x0002
G_LON_REF, G_LON = 0x0003, 0x0004
G_ALT_REF, G_ALT = 0x0005, 0x0006
G_TIME, G_DATE = 0x0007, 0x001D

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}


# ------------------------------------------------------------------ TIFF 基础
class _Tiff:
    __slots__ = ("b", "bo")

    def __init__(self, buf: bytes):
        self.b = buf
        if buf[:2] == b"II":
            self.bo = "<"
        elif buf[:2] == b"MM":
            self.bo = ">"
        else:
            raise ValueError("不是 TIFF：字节序标识不认识")
        if self.u16(2) != 42:
            raise ValueError("不是 TIFF：magic != 42")

    def u16(self, o: int) -> int:
        return struct.unpack_from(self.bo + "H", self.b, o)[0]

    def u32(self, o: int) -> int:
        return struct.unpack_from(self.bo + "I", self.b, o)[0]

    def s32(self, o: int) -> int:
        return struct.unpack_from(self.bo + "i", self.b, o)[0]

    def ifd(self, off: int) -> dict[int, tuple[int, int, int, int]]:
        """读一个 IFD：tag → (类型, 数量, 值字段偏移, 数据起始偏移)。"""
        out: dict[int, tuple[int, int, int, int]] = {}
        if off <= 0 or off + 2 > len(self.b):
            return out
        n = self.u16(off)
        for i in range(n):
            e = off + 2 + i * 12
            if e + 12 > len(self.b):
                break
            tag, typ, cnt = self.u16(e), self.u16(e + 2), self.u32(e + 4)
            size = _TYPE.get(typ, 1) * cnt
            voff = e + 8 if size <= 4 else self.u32(e + 8)
            out[tag] = (typ, cnt, e + 8, voff)
        return out

    def raw(self, ent: tuple[int, int, int, int]) -> bytes:
        typ, cnt, _, voff = ent
        return self.b[voff:voff + _TYPE.get(typ, 1) * cnt]

    def nums(self, ent: tuple[int, int, int, int]) -> list[float]:
        """读数值数组。

        `ent` 的第 3 项是**值字段**地址（entry+8），第 4 项是**数据**地址。
        两者只有在数据 >4 字节时才不同——RATIONAL（GPS 经纬度）全部走数据
        地址，早期版本混用，结果是拿指针字节当坐标，位置偏了 600 km。
        """
        typ, cnt, inline, dataoff = ent
        base = dataoff if _TYPE.get(typ, 1) * cnt > 4 else inline
        out: list[float] = []
        for i in range(cnt):
            if typ == 5:      # RATIONAL
                num, den = self.u32(base + i * 8), self.u32(base + i * 8 + 4)
                out.append(num / den if den else 0.0)
            elif typ == 10:   # SRATIONAL
                num, den = self.s32(base + i * 8), self.s32(base + i * 8 + 4)
                out.append(num / den if den else 0.0)
            elif typ == 3:
                out.append(self.u16(base + i * 2))
            elif typ == 4:
                out.append(self.u32(base + i * 4))
            elif typ in (1, 6, 7):
                out.append(self.b[base + i])
        return out

    def text(self, ent: tuple[int, int, int, int]) -> str:
        return self.raw(ent).split(b"\x00")[0].decode("utf-8", "ignore").strip()


# ------------------------------------------------------------------ 对外接口
def _jpeg_size(buf: bytes) -> tuple[int | None, int | None]:
    """从 SOF 段读存储方向的宽高（不依赖 EXIF，兜个底）。"""
    i = 2
    while i < len(buf) - 9:
        if buf[i] != 0xFF:
            i += 1
            continue
        m = buf[i + 1]
        if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack_from(">HH", buf, i + 5)
            return w, h
        if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        i += 2 + struct.unpack_from(">H", buf, i + 2)[0]
    return None, None


def _app1(buf: bytes) -> bytes | None:
    """找 APP1 里的 Exif 块，返回从 "II"/"MM" 开始的 TIFF 字节。"""
    if buf[:2] != b"\xff\xd8":
        return None
    i = 2
    while i < len(buf) - 4:
        if buf[i] != 0xFF:
            i += 1
            continue
        m = buf[i + 1]
        if m == 0xE1:
            ln = struct.unpack_from(">H", buf, i + 2)[0]
            seg = buf[i + 4:i + 2 + ln]
            if seg[:6] == b"Exif\x00\x00":
                return seg[6:]
            i += 2 + ln
            continue
        if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        if m == 0xDA:          # 图像数据开始，后面没有 EXIF 了
            break
        i += 2 + struct.unpack_from(">H", buf, i + 2)[0]
    return None


def _dms(vals: list[float], ref: str) -> float | None:
    if len(vals) < 3:
        return None
    d = vals[0] + vals[1] / 60.0 + vals[2] / 3600.0
    return -d if ref.upper() in ("S", "W") else d


def _parse_dt(s: str) -> dt.datetime | None:
    """EXIF 的两种写法：`YYYY:MM:DD HH:MM:SS`（DateTimeOriginal）和只有日期的
    `YYYY:MM:DD`（GPSDateStamp）。只支持前者会让 GPS 时间整条失效。"""
    s = (s or "").strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y:%m:%d", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def read(path: Path) -> dict:
    """读一张照片的 EXIF。永远返回 dict，失败时带 error 字段。"""
    path = Path(path)
    out: dict = {
        "path": str(path), "name": path.name,
        "lat": None, "lon": None, "alt": None,
        "t_utc": None, "t_source": "", "t_local": None,
        "orientation": 1, "w": None, "h": None,
        "make": "", "model": "", "bytes": 0, "error": "",
    }
    try:
        out["bytes"] = path.stat().st_size
        with path.open("rb") as f:
            buf = f.read(256 * 1024)
    except OSError as e:
        out["error"] = f"读不了：{e}"
        return out
    if buf[:4] == b"ftyp" or b"ftypheic" in buf[:32] or b"ftypmif1" in buf[:32]:
        out["error"] = "HEIF/HEIC：纯 Python 解不了，先转成 JPG"
        return out
    if out["bytes"] > len(buf):
        with path.open("rb") as f:
            buf = f.read()          # EXIF 偏移可能落在文件后段，整读一次

    tiff_raw = _app1(buf)
    if not tiff_raw:
        out["error"] = "没有 EXIF（转发的图 / 截图 / 已剥离）"
        w, h = _jpeg_size(buf)
        out["w"], out["h"] = w, h
        return out
    try:
        tf = _Tiff(tiff_raw)
    except (ValueError, struct.error) as e:
        out["error"] = f"EXIF 损坏：{e}"
        return out

    ifd0 = tf.ifd(tf.u32(4))
    if T_MAKE in ifd0:
        out["make"] = tf.text(ifd0[T_MAKE])
    if T_MODEL in ifd0:
        out["model"] = tf.text(ifd0[T_MODEL])
    if T_ORIENT in ifd0:
        out["orientation"] = int(tf.nums(ifd0[T_ORIENT])[0] or 1)

    exif = tf.ifd(tf.u32(ifd0[T_EXIF_IFD][3]) if T_EXIF_IFD in ifd0 else 0)
    if T_PIX_W in exif:
        out["w"] = int(tf.nums(exif[T_PIX_W])[0] or 0) or None
    if T_PIX_H in exif:
        out["h"] = int(tf.nums(exif[T_PIX_H])[0] or 0) or None
    if not (out["w"] and out["h"]):
        out["w"], out["h"] = _jpeg_size(buf)

    local = _parse_dt(tf.text(exif[T_DT_ORIG])) if T_DT_ORIG in exif else None
    out["t_local"] = local.isoformat() if local else None

    gps = tf.ifd(tf.u32(ifd0[T_GPS_IFD][3]) if T_GPS_IFD in ifd0 else 0)
    if G_LAT in gps and G_LON in gps:
        lat = _dms(tf.nums(gps[G_LAT]), tf.text(gps[G_LAT_REF]) if G_LAT_REF in gps else "N")
        lon = _dms(tf.nums(gps[G_LON]), tf.text(gps[G_LON_REF]) if G_LON_REF in gps else "E")
        if lat is not None and lon is not None and not (lat == 0 and lon == 0):
            out["lat"], out["lon"] = round(lat, 7), round(lon, 7)
        if G_ALT in gps:
            a = tf.nums(gps[G_ALT])[0] if tf.nums(gps[G_ALT]) else None
            if a is not None:
                ref = tf.nums(gps[G_ALT_REF])[0] if G_ALT_REF in gps else 0
                out["alt"] = round(-a if ref==1 else a, 1)

    # 时间：GPS UTC 优先，其次 本地时间 + 时区偏移
    gps_dt = None
    if G_DATE in gps and G_TIME in gps:
        ds, tsv = tf.text(gps[G_DATE]), tf.nums(gps[G_TIME])
        d = _parse_dt(ds)
        if d and len(tsv) >= 3:
            gps_dt = dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc) + dt.timedelta(
                hours=tsv[0], minutes=tsv[1], seconds=tsv[2])
    if gps_dt:
        out["t_utc"] = int(gps_dt.timestamp() * 1000)
        out["t_source"] = "gps"
    elif local:
        off = dt.timedelta(hours=8)
        if T_OFFSET_ORIG in exif:
            raw = tf.text(exif[T_OFFSET_ORIG])     # 形如 "+08:00"
            try:
                sign = -1 if raw.startswith("-") else 1
                hh, mm = raw.lstrip("+-").split(":")[:2]
                off = sign * dt.timedelta(hours=int(hh), minutes=int(mm))
            except (ValueError, IndexError):
                pass
        out["t_utc"] = int((local - off).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
        out["t_source"] = "local+offset" if T_OFFSET_ORIG in exif else "local(+08)"
    else:
        out["error"] = out["error"] or "没有拍摄时间"
    return out


def scan(folder: Path, *, recursive: bool = True) -> list[dict]:
    """扫描目录，读所有照片的 EXIF（跳过 .thumb 缓存目录）。"""
    folder = Path(folder)
    if not folder.exists():
        return []
    walk = folder.rglob("*") if recursive else folder.glob("*")
    out = []
    for p in sorted(walk):
        if not p.is_file() or p.suffix.lower() not in IMAGE_EXT:
            continue
        if ".thumbs" in p.parts:
            continue
        # `_raw` / `_raw2` 这类备份目录里是旧副本，扫进来会跟正片抢同一个点位
        if any(part.startswith("_") for part in p.relative_to(folder).parts[:-1]):
            continue
        out.append(read(p))
    return out


# ------------------------------------------------------------------ 吸附
def _nearest_on_route(rt: dict, lat: float, lon: float) -> tuple[float, int]:
    """轨迹上离 (lat, lon) 最近的点：返回 (距离米, 下标)。

    用轨迹中心纬度的等距圆柱近似（本地 5 km 内误差 <0.1 m），比逐点开
    haversine 快一个量级——一张照片对 3000 个点，10 张就是 3 万次三角函数。
    """
    lats, lons = rt.get("lat") or [], rt.get("lon") or []
    if not lats:
        return float("inf"), -1
    kx = 111320.0 * math.cos(math.radians(sum(lats) / len(lats)))
    best, bi = float("inf"), -1
    for i, (la, lo) in enumerate(zip(lats, lons)):
        dx = (lo - lon) * kx
        dy = (la - lat) * 110540.0
        d2 = dx * dx + dy * dy
        if d2 < best:
            best, bi = d2, i
    return math.sqrt(best), bi


def _at_time(rt: dict, rel: float) -> int:
    """轨迹上「活动开始后 rel 秒」那个点的下标；越界返回 -1。"""
    t = rt.get("t") or []
    if not t or rel < 0 or rel > t[-1]:
        return -1
    lo, hi = 0, len(t) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if t[mid] < rel:
            lo = mid + 1
        else:
            hi = mid
    return lo


def match(photos: list[dict], rt: dict, *, t0_ms: int | None = None,
          pre_s: int = 300, post_s: int = 900, max_m: float = 80.0,
          cluster_m: float = 25.0, keep: tuple[str, ...] = (),
          allow_time_only: bool = True) -> dict:
    """把照片挂到轨迹上。

    判定三条同时满足才吸附：落在活动时间窗内（前后各留一段余量）、到轨迹
    最近点 ≤ `max_m`、有坐标。不满足的进 `loose` 清单并带上原因——真实情况里
    照片经常对不上（跑完散步拍的、路上顺手拍风景），直接丢掉会让人以为程序坏了。

    `cluster_m`：相距 25 m 以内的多张照片合成一个点，播放时并排显示。
    同地连拍是常态，一个点弹 5 张卡会把节奏打乱。
    """
    t0 = t0_ms if t0_ms is not None else rt.get("t0")
    dur = (rt.get("t") or [0])[-1] if rt.get("n") else 0
    hit: list[dict] = []
    loose: list[dict] = []
    for ph in photos:
        name = ph.get("name") or Path(ph.get("path", "")).name
        if ph.get("error"):
            loose.append({"name": name, "why": ph["error"]})
            continue
        if ph.get("t_utc") is None:
            loose.append({"name": name, "why": "没有拍摄时间"})
            continue
        if ph.get("lat") is None and not allow_time_only:
            loose.append({"name": name, "why": "没有 GPS 坐标"})
            continue
        if not t0:
            loose.append({"name": name, "why": "活动没有轨迹"})
            continue
        rel = (ph["t_utc"] - t0) / 1000.0
        tz_shift = 0
        if rel < -pre_s or rel > dur + post_s:
            # 有些相机把「本地时间」原样写进 creation_time 却当 UTC 解释，
            # 结果整整差一个时区（最常见就是 8 小时）。照片 EXIF 一般不会，
            # 视频的 mvhd 很常见——真按 UTC 判就永远"不在活动时段"。
            for hrs in (8, -8, 9, -9):
                alt = rel - hrs * 3600
                if -pre_s <= alt <= dur + post_s:
                    rel, tz_shift = alt, hrs
                    break
            else:
                when = dt.datetime.fromtimestamp(ph["t_utc"] / 1000).strftime("%m-%d %H:%M")
                loose.append({"name": name,
                              "why": f"不在活动时段（文件时间 {when}，活动时长 {int(dur // 60)} 分钟）"})
                continue

        lat, lon = ph.get("lat"), ph.get("lon")
        approx = False
        if lat is None or lon is None:
            # 没有坐标：跑步时拍的东西，时间本身就定位了位置
            # （轨迹在那一刻只有一个点），坐标只是用来交叉验证的。
            if not allow_time_only:
                loose.append({"name": name, "why": "没有 GPS 坐标"})
                continue
            # rel 允许落在终点之后（post_s 窗口内，跑完拍的）：时间上已经
            # 出了轨迹范围，但那一刻人只可能在终点附近，直接吸附到末点。
            i = _at_time(rt, min(rel, dur)) if rel > dur else _at_time(rt, rel)
            if i < 0:
                loose.append({"name": name, "why": "没有 GPS 坐标，时间也对不上轨迹"})
                continue
            gap, approx = None, True
        else:
            gap, i = _nearest_on_route(rt, lat, lon)
            # 环线/折返赛道：起点和终点几乎重合，GPS 精度差一点就把「刚起跑」
            # 判到「快到终点」。活动内 1Hz 的时间轴是唯一的，所以两者里程差
            # 悬殊时以时间点为准（GPS 只用来兜住「时间不准」的情况）。
            j = _at_time(rt, min(max(rel, 0.0), dur))
            if i >= 0 and j >= 0 and abs(rt["d"][i] - rt["d"][j]) > 300:
                i, gap = j, None
                approx = False
            elif i < 0:
                if j < 0:
                    loose.append({"name": name, "why": "坐标和时间都对不上轨迹"})
                    continue
                i, gap = j, None
            if gap is not None and gap > max_m:
                loose.append({"name": name,
                              "why": f"离轨迹 {gap:.0f} m（阈值 {max_m:.0f} m）"})
                continue
        item = {
            "name": name, "path": ph.get("path"),
            "t": float(max(min(rel, dur), 0)),
            "lat": rt["lat"][i], "lon": rt["lon"][i],
            "d": rt["d"][i],
            "gap_m": (round(gap, 1) if gap is not None else None),
            "w": ph.get("w"), "h": ph.get("h"),
            "orientation": ph.get("orientation") or 1,
            "t_source": (ph.get("t_source") or "") + (f"+{tz_shift}h" if tz_shift else ""),
            "approx": approx,
        }
        # 视频要带时长，照片不带——但这里重建了 dict，额外字段得显式透传
        for k in keep:
            if k in ph:
                item[k] = ph[k]
        hit.append(item)

    # 同地合并：按时间排序后，与上一个点距离 < cluster_m 就并进去
    hit.sort(key=lambda p: p["t"])
    groups: list[dict] = []
    for p in hit:
        if groups:
            g = groups[-1]
            d = math.hypot((p["lat"] - g["lat"]) * 110540,
                           (p["lon"] - g["lon"]) * 111320 * math.cos(math.radians(p["lat"])))
            if d < cluster_m:
                g["shots"].append(p)
                continue
        groups.append({**p, "shots": [p]})
    for g in groups:
        g.pop("path", None)
    return {"points": groups, "loose": loose, "total": len(photos)}