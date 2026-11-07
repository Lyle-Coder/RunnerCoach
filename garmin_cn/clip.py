"""1~3 秒视频片段（实况 / 随手拍）的识别与转码。

和 `photo.py` 一套思路：从文件本身把「什么时候、在哪拍的」读出来，
交给 `photo.match()` 挂到轨迹上，而不是靠文件名猜。

MP4 / MOV 都是 ISO-BMFF，信息在 box 里：

| 需要什么 | 位置 | 说明 |
|---|---|---|
| 拍摄时间 | `moov/mvhd.creation_time` | 1904-01-01 起的秒数，UTC |
| 时长 | `mvhd.duration / mvhd.timescale` | 秒 |
| 显示尺寸 | `moov/trak/tkhd` | 16.16 定点，已含旋转 |
| GPS（安卓/ffmpeg） | `moov/udta/loci` 或 `moov/udta/©xyz` | 前者是 ISO 6709 二进制定点 |
| GPS（iPhone） | `moov/meta/keys` + `ilst` | `com.apple.quicktime.location.ISO6709` |
| 编码 | `stsd` 的样本条目 | `avc1` 才能被浏览器直接解 |

iPhone 的实况是 HEIC + HEVC 的 MOV，Windows 上的 Chrome 解不了 HEVC，
所以 `ensure_playable()` 会用 ffmpeg 转一份 H.264（顺便把 GOP 压到 0.5 秒，
逐帧 seek 才快），转完缓存，下次直接用。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

VIDEO_EXT = {".mp4", ".mov", ".m4v"}
EPOCH_1904 = dt.datetime(1904, 1, 1, tzinfo=dt.timezone.utc)
MAX_SEC = 30.0           # 超过这个长度的不当「实况」用（30s 内都按整段插进回放）
_CONTAINERS = {"moov", "trak", "mdia", "minf", "stbl", "udta", "meta", "ilst", "dinf", "edts"}


# ------------------------------------------------------------------ box 解析
def _boxes(buf: bytes, start: int, end: int):
    """逐个吐 (type, 载荷起点, 结束, 完整 box 起点)。"""
    i = start
    while i + 8 <= end:
        size = int.from_bytes(buf[i:i + 4], "big")
        typ = buf[i + 4:i + 8].decode("latin-1", "replace")
        hdr = 8
        if size == 1:                      # 64 位长度
            if i + 16 > end:
                break
            size = int.from_bytes(buf[i + 8:i + 16], "big")
            hdr = 16
        elif size == 0:                    # 一直到文件尾
            size = end - i
        if size < hdr or i + size > end:
            break
        yield typ, i + hdr, i + size
        i += size


def _walk(buf: bytes, start: int, end: int, stop: str, depth: int = 0):
    """深度优先找某个 box，返回 (载荷起点, 结束)。"""
    if depth > 6:
        return None
    for typ, a, b in _boxes(buf, start, end):
        if typ == stop:
            return a, b
        if typ in _CONTAINERS:
            sub = a + (4 if typ == "meta" else 0)     # meta 是 fullbox，多 4 字节
            got = _walk(buf, sub, b, stop, depth + 1)
            if got:
                return got
    return None


def _all(buf: bytes, start: int, end: int, want: set[str], depth: int = 0):
    """把指定类型全找出来（GPS 可能藏在 udta 或 meta/ilst 两处）。"""
    out = []
    if depth > 6:
        return out
    for typ, a, b in _boxes(buf, start, end):
        if typ in want:
            out.append((typ, a, b))
        elif typ in _CONTAINERS:
            sub = a + (4 if typ == "meta" else 0)
            out.extend(_all(buf, sub, b, want, depth + 1))
    return out


def _frac(raw: bytes) -> float | None:
    """ISO 6709 二进制里的 16.16 定点；负数用补码。"""
    if len(raw) < 4:
        return None
    v = int.from_bytes(raw, "big", signed=True)
    return v / 65536.0


def _iso6709(text: str) -> tuple[float, float] | None:
    """`+34.1311+108.8245/` 或 `+34.1311+108.8245+420.0/` → (lat, lon)。"""
    s = (text or "").strip().rstrip("/")
    if not s or s[0] not in "+-":
        return None
    parts, cur = [], s[0]
    for ch in s[1:]:
        if ch in "+-":                 # 注意：指数不会出现在 ISO 6709 里
            parts.append(cur)
            cur = ch
        else:
            cur += ch
    parts.append(cur)
    if len(parts) < 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


def _gps(path: Path, buf: bytes, moov: tuple[int, int]) -> tuple[float | None, float | None, str]:
    """返回 (lat, lon, 来源说明)。安卓、iPhone、ffmpeg 三种写法都认。"""
    a, b = moov
    # ① loci：loci 是 fullbox，载荷 = 版本/标志 4 + 语言 2 + 名字(1+n) + role 1
    #    + 经度 4 + 纬度 4 + 高度 4 （16.16 定点）
    for typ, s, e in _all(buf, a, b, {"loci"}):
        if e - s >= 20:
            p = s + 4 + 2
            nlen = buf[p]
            p += 1 + nlen + 1
            lon = _frac(buf[p:p + 4])
            lat = _frac(buf[p + 4:p + 8])
            if lat is not None and lon is not None and not (lat == 0 and lon == 0):
                return lat, lon, "loci"
    # ② ©xyz：u16 长度 + u16 语言 + ISO 6709 字符串
    for typ, s, e in _all(buf, a, b, {"©xyz"}):
        if e - s >= 4:
            n = int.from_bytes(buf[s:s + 2], "big")
            txt = buf[s + 4:s + 4 + n].decode("latin-1", "replace")
            got = _iso6709(txt)
            if got:
                return got[0], got[1], "©xyz"
    # ③ iPhone：meta/keys 里登记 key 名，ilst 里按序号取值
    keys_box = _all(buf, a, b, {"keys"})
    ilst_box = _all(buf, a, b, {"ilst"})
    if keys_box and ilst_box:
        ks, ke = keys_box[0][1], keys_box[0][2]
        names: list[str] = []
        if ke - ks >= 8:
            cnt = int.from_bytes(buf[ks + 4:ks + 8], "big")
            p = ks + 8
            for _ in range(min(cnt, 64)):
                if p + 8 > ke:
                    break
                ksz = int.from_bytes(buf[p:p + 4], "big")
                names.append(buf[p + 8:p + ksz].decode("latin-1", "replace"))
                p += ksz
        is_, ie = ilst_box[0][1], ilst_box[0][2]
        idx = -1
        for typ, s, e in _boxes(buf, is_, ie):
            idx += 1
            if typ != "----" and idx < len(names) and "location" in names[idx].lower():
                d = _walk(buf, s, e, "data")
                if d:
                    got = _iso6709(buf[d[0] + 8:d[1]].decode("latin-1", "replace"))
                    if got:
                        return got[0], got[1], "apple"
    return None, None, ""


def _codec(buf: bytes, moov: tuple[int, int]) -> str:
    got = _walk(buf, moov[0], moov[1], "stsd")
    if not got:
        return ""
    a, b = got
    # stsd: version/flags 4 + count 4 + 第一个样本条目（size 4 + 类型 4）
    p = a + 8
    if p + 8 > b:
        return ""
    return buf[p + 4:p + 8].decode("latin-1", "replace")


# 相机文件名里的时间戳：VID_20261103_180036 / MVIMG_20261103_180036 /
# PXL_20261103_180036123 / 20261103_180036 / VID20261103180036 …
_NAME_DT = re.compile(
    r"(?<!\d)(20\d{2})[-_]?(\d{2})[-_]?(\d{2})[-_T]?(\d{2})[-_:]?(\d{2})[-_:]?(\d{2})")
_ISO_DT = re.compile(
    rb"(20\d{2})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(Z|[+-]\d{2}:?\d{2})?")


def time_from_name(name: str) -> int | None:
    """相机文件名里的时间戳 → UTC 毫秒。

    文件名是**本地**时间，按 +08:00 折算（与 `photo.read` 在没有
    OffsetTimeOriginal 时的默认一致）。剪辑后的成品常被改名为
    `clip-1730...mp4`，所以调用方要传登记时保留的原始文件名。
    """
    m = _NAME_DT.search(Path(str(name or "")).name)
    if not m:
        return None
    try:
        d = dt.datetime(*(int(x) for x in m.groups()))
    except ValueError:
        return None
    utc = d - dt.timedelta(hours=8)
    return int(utc.replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def _scan_moov(path: Path, size: int):
    """顶层 box 顺序扫描，兜住 moov 落在文件中段的视频。

    手机拍的长片、分片录制的 mp4 常把 moov 放在 mdat 之间；只读文件头 1 MB
    和文件尾 8 MB 会直接判成「读不到 moov」，于是明明有拍摄时间也显示没有。
    这里只按每个顶层 box 的 8/16 字节头 seek，找到 moov 才读它的载荷。
    """
    try:
        with path.open("rb") as f:
            off = 0
            while off + 8 <= size:
                f.seek(off)
                hdr = f.read(16)
                if len(hdr) < 8:
                    return None
                n = int.from_bytes(hdr[:4], "big")
                typ = hdr[4:8].decode("latin-1", "replace")
                h = 8
                if n == 1:
                    if len(hdr) < 16:
                        return None
                    n = int.from_bytes(hdr[8:16], "big")
                    h = 16
                elif n == 0:
                    n = size - off
                if n < h or off + n > size:
                    return None
                if typ == "moov":
                    f.seek(off + h)
                    buf = f.read(n - h)
                    return buf, (0, len(buf)), off + h
                off += n
    except OSError:
        return None
    return None


def _moov_view(path: Path):
    """返回 `(buf, moov, base)`：buf 是内存片段，base 是它在文件里的起始偏移。

    `probe` 和 `stamp_creation` 共用，避免两处各写一遍「head 找不到就读尾」。
    """
    size = path.stat().st_size
    with path.open("rb") as f:
        head = f.read(1024 * 1024)
    moov = _walk(head, 0, len(head), "moov")
    if moov:
        return head, moov, 0
    with path.open("rb") as f:
        f.seek(max(0, size - 8 * 1024 * 1024))
        tail = f.read()
    moov = _walk(tail, 0, len(tail), "moov")
    if moov:
        return tail, moov, size - len(tail)
    return _scan_moov(path, size) or (b"", None, 0)


def _time_from_tags(buf: bytes, moov: tuple[int, int], out: dict,
                    name_hint: str) -> None:
    """mvhd 之外的时间来源。

    `mvhd.creation_time` 被重封装清零后，有些文件仍在 `udta/©day` 或
    `meta/ilst` 的 `com.apple.quicktime.creationdate` 里留着 ISO8601 文本；
    再不行就退到相机文件名。优先级 mvhd > 文本标签 > 文件名。
    """
    if out.get("t_utc"):
        return
    a, b = moov
    m = _ISO_DT.search(buf[a:b])
    if m:
        try:
            d = dt.datetime(*(int(x) for x in m.groups()[:6]))
            suffix = (m.group(7) or b"").decode("ascii", "ignore")
            if suffix in ("Z", "+00:00", "+0000"):
                off = 0.0
            elif suffix.startswith(("+", "-")):
                digits = suffix[1:].replace(":", "")
                off = (1 if suffix[0] == "+" else -1) * (
                    int(digits[:2]) + int(digits[2:4]) / 60)
            else:
                off = 8.0          # 没写时区就当本地(+08)
            utc = d - dt.timedelta(hours=off)
            out["t_utc"] = int(utc.replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
            out["t_source"] = "udta"
            return
        except (ValueError, IndexError):
            pass
    got = time_from_name(name_hint or out.get("name") or "")
    if got:
        out["t_utc"], out["t_source"] = got, "name(+08)"


def stamp_creation(path: Path, t_utc_ms: int) -> bool:
    """把拍摄时间写回 mp4 的 mvhd，让**成品自己**带得住时间。

    MediaMuxer / ffmpeg 重封装会把 creation_time 清成 0，只靠 clips.json
    记录一旦丢失就再也读不回来。这里原地改 creation/modification 两处
    4 或 8 字节，不改文件长度、不重编码；任何异常都返回 False，
    绝不因为打标毁掉素材。
    """
    try:
        path = Path(path)
        secs = int((dt.datetime.fromtimestamp(t_utc_ms / 1000, dt.timezone.utc)
                    - EPOCH_1904).total_seconds())
        if secs <= 0:
            return False
        buf, moov, base = _moov_view(path)
        if not moov:
            return False
        mv = _walk(buf, moov[0], moov[1], "mvhd")
        if not mv:
            return False
        s = mv[0]
        n = 8 if buf[s] == 1 else 4
        ct, mod = base + s + 4, base + s + 4 + n
        if mod + n > path.stat().st_size:
            return False
        raw = secs.to_bytes(n, "big")
        with path.open("r+b") as f:
            f.seek(ct)
            f.write(raw)
            f.seek(mod)
            f.write(raw)
        return True
    except Exception:
        return False


def probe(path: Path, *, name_hint: str = "") -> dict:
    """读一个视频文件的时间/坐标/尺寸/时长/编码。失败时带 error。

    `name_hint` 是登记时的原始文件名：成品被改名为 `clip-…` 后，
    还能靠 `VID_20261103_180036` 这类名字兜出拍摄时间。
    """
    path = Path(path)
    out: dict = {
        "name": path.name, "path": str(path), "kind": "clip",
        "lat": None, "lon": None, "t_utc": None, "t_source": "", "t_why": "",
        "w": None, "h": None, "dur": None, "codec": "", "bytes": 0, "error": "",
        "rot": 0,
    }
    try:
        out["bytes"] = path.stat().st_size
        buf, moov, _ = _moov_view(path)
        if not moov:
            out["error"] = "读不到 moov（不是 mp4/mov，或文件损坏）"
            out["t_why"] = "文件读不出元数据"
            return out
        return _probe_with(buf, moov, out, path, name_hint)
    except OSError as e:
        out["error"] = f"读不了：{e}"
        out["t_why"] = "文件读不出来"
        return out


def _probe_with(buf: bytes, moov: tuple[int, int], out: dict, path: Path,
                name_hint: str = "") -> dict:
    a, b = moov
    mv = _walk(buf, a, b, "mvhd")
    if mv:
        s, e = mv
        ver = buf[s]
        if ver == 1:
            ct = int.from_bytes(buf[s + 4:s + 12], "big")
            ts = int.from_bytes(buf[s + 20:s + 24], "big")
            du = int.from_bytes(buf[s + 24:s + 32], "big")
        else:
            ct = int.from_bytes(buf[s + 4:s + 8], "big")
            ts = int.from_bytes(buf[s + 12:s + 16], "big")
            du = int.from_bytes(buf[s + 16:s + 20], "big")
        if ct > 0:
            out["t_utc"] = int((EPOCH_1904 + dt.timedelta(seconds=ct)).timestamp() * 1000)
            out["t_source"] = "mvhd"
        if ts:
            out["dur"] = round(du / ts, 2)
    tk = _walk(buf, a, b, "tkhd")
    if tk:
        s, e = tk
        ver = buf[s]
        # width/height 在 box 末尾 8 字节，16.16 定点
        w = int.from_bytes(buf[e - 8:e - 4], "big") / 65536.0
        h = int.from_bytes(buf[e - 4:e], "big") / 65536.0
        if w >= 1 and h >= 1:
            # tkhd 的 matrix 带旋转时，w/h 是「编码尺寸」而不是「显示尺寸」。
            # 手机竖着录的片子常见 1920×1080 + 90°，直接拿来当宽高就会把
            # 竖屏塞进横格子（画面被压扁）。这里按矩阵把两者对调。
            mx = buf[e - 44:e - 8]
            if len(mx) == 36:
                a = int.from_bytes(mx[0:4], "big", signed=True) / 65536.0
                b = int.from_bytes(mx[4:8], "big", signed=True) / 65536.0
                if abs(a) < 0.5 and abs(b) > 0.5:      # 90° / 270°
                    w, h = h, w
                    out["rot"] = 90 if b > 0 else 270
            out["w"], out["h"] = int(round(w)), int(round(h))
    out["codec"] = _codec(buf, moov)
    lat, lon, src = _gps(path, buf, moov)
    if lat is not None:
        out["lat"], out["lon"], out["t_source"] = round(lat, 7), round(lon, 7), out["t_source"] or src
    _time_from_tags(buf, moov, out, name_hint)
    if not out["t_utc"]:
        out["t_why"] = ("视频元数据里没有拍摄时间（重封装会清掉 mvhd.creation_time），"
                        "文件名里也没有可用时间戳")
    if out["dur"] is None:
        out["error"] = out["error"] or "读不到时长"
    elif out["dur"] > MAX_SEC:
        out["error"] = f"时长 {out['dur']:.1f}s，超过 {MAX_SEC:.0f}s，不当实况用"
    return out


# ------------------------------------------------------------------ 实况照片（单文件）
def embedded_mp4(path: Path) -> tuple[int, int] | None:
    """在 JPEG 尾部找内嵌的 MP4，返回 (偏移, 长度)。

    安卓的「动态照片」主流做法是：一张普通 JPEG，尾部直接拼一段 MP4，
    XMP 里用 MicroVideoOffset 指向它。不去解析 XMP 是因为各家字段名不一样
    （GCamera:MicroVideoOffset / Container:Directory / 小米自己的写法都有），
    而「文件末尾有个完整的 ftyp box」这件事是共同的。
    """
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size < 4096:
        return None
    tail = min(size, 8 * 1024 * 1024)
    base = size - tail
    try:
        with path.open("rb") as f:
            f.seek(base)
            buf = f.read()
    except OSError:
        return None
    brands = {b"mp42", b"mp41", b"isom", b"iso2", b"iso5", b"iso6", b"qt  ", b"avc1"}
    idx = buf.rfind(b"ftyp")
    while idx >= 4:
        # ftyp 只是整段视频的第一个 box，后面还有 moov/mdat，
        # 所以长度一律取「从 ftyp 到文件尾」，再用 brand 排除误命中。
        if buf[idx + 4:idx + 8] in brands:
            abs_off = base + idx - 4
            length = size - abs_off
            if length >= 4096 and int.from_bytes(buf[idx - 4:idx], "big") >= 8:
                return abs_off, length
        idx = buf.rfind(b"ftyp", 0, idx)
    return None


def extract_motion(path: Path, cache: Path) -> Path | None:
    """把实况照片里的视频切出来，落到缓存目录。"""
    got = embedded_mp4(path)
    if not got:
        return None
    off, length = got
    key = hashlib.md5(f"{path}|{path.stat().st_mtime_ns}".encode()).hexdigest()[:10]
    dst = cache / f"{path.stem}-live-{key}.mp4"
    if dst.exists() and dst.stat().st_size == length:
        return dst
    cache.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("rb") as f, dst.open("wb") as o:
            f.seek(off)
            left = length
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                o.write(chunk)
                left -= len(chunk)
    except OSError:
        return None
    return dst


def probe_motion(path: Path, cache: Path) -> dict | None:
    """实况照片 → 当成一段 clip。时间/坐标优先用照片的 EXIF（比 mvhd 可靠）。"""
    from . import photo as photo_mod

    jpg = photo_mod.read(Path(path))
    if jpg.get("error") and not jpg.get("w"):
        return None
    mp4 = extract_motion(Path(path), cache)
    if not mp4:
        return None
    info = probe(mp4)
    if info.get("error"):
        return None
    info["name"] = f"{Path(path).name}（实况）"
    info["source"] = str(path)
    if jpg.get("t_utc"):
        info["t_utc"], info["t_source"] = jpg["t_utc"], "exif"
    if jpg.get("lat") is not None:
        info["lat"], info["lon"] = jpg["lat"], jpg["lon"]
    if info.get("w") and info.get("h") and jpg.get("orientation") in (5, 6, 7, 8):
        info["w"], info["h"] = info["h"], info["w"]
    return info


# ------------------------------------------------------------------ 扫描 / 转码
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}


def scan(folder: Path, *, recursive: bool = True, cache: Path | None = None,
         probe_stills: bool = True) -> list[dict]:
    """扫视频文件；顺带把「单文件实况照片」里的视频也切出来。"""
    folder = Path(folder)
    if not folder.exists():
        return []
    cache = cache or (folder.parent / "motion_cache")
    walk = folder.rglob("*") if recursive else folder.glob("*")
    out = []
    for p in sorted(walk):
        if not p.is_file():
            continue
        # `_raw` / `_raw2` 备份目录里是旧副本，扫进来会跟正片抢同一个点位
        if any(part.startswith("_") for part in p.relative_to(folder).parts[:-1]):
            continue
        suf = p.suffix.lower()
        if suf in VIDEO_EXT:
            out.append(probe(p))
        elif probe_stills and suf in IMG_EXT:
            got = probe_motion(p, cache)
            if got:
                out.append(got)
    return out


def ffmpeg() -> str | None:
    from .video import ffmpeg as _f
    return _f()


def playable(info: dict) -> bool:
    """浏览器能不能直接放。avc1/hev1/hvc1 之外一律转码。"""
    c = (info.get("codec") or "").lower()
    h = info.get("h") or 0
    return c == "avc1" and h <= 1920 and (info.get("bytes") or 0) < 40 * 1024 * 1024


def ensure_playable(info: dict, cache: Path) -> str:
    """返回可直接给浏览器用的文件路径；需要时先转码。

    转码不只是为了兼容：`-g 15` 把 GOP 压到 0.5 秒，逐帧导出时
    seek 从 20~50 ms 降到 2~3 ms。
    """
    path = Path(info["path"])
    if playable(info):
        return str(path)
    exe = ffmpeg()
    if not exe:
        return str(path)                     # 没 ffmpeg 就原样试一把
    cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(f"{path}|{path.stat().st_mtime_ns}|{info.get('bytes')}"
                      .encode()).hexdigest()[:10]
    dst = cache / f"{path.stem}-{key}.mp4"
    if dst.exists() and dst.stat().st_size > 4096:
        return str(dst)
    cmd = [exe, "-y", "-loglevel", "error", "-i", str(path),
           "-t", f"{MAX_SEC}", "-vf",
           "scale='if(gt(iw\\,ih)\\,min(1080\\,iw)\\,-2)':'if(gt(iw\\,ih)\\,-2\\,min(1080\\,ih))'",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-g", "15", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not dst.exists():
        info["error"] = (info.get("error") or "") + " 转码失败"
        return str(path)
    info["transcoded"] = str(dst)
    return str(dst)


def text(rows: list[dict]) -> str:
    if not rows:
        return "没有视频片段"
    out = [f"{len(rows)} 段视频"]
    for r in rows:
        if r.get("error"):
            out.append(f"  × {r['name']}：{r['error']}")
            continue
        when = (dt.datetime.fromtimestamp(r["t_utc"] / 1000).strftime("%m-%d %H:%M:%S")
                if r.get("t_utc") else "无时间")
        pos = (f"{r['lat']:.5f}, {r['lon']:.5f}" if r.get("lat") is not None else "无坐标")
        out.append(f"  · {r['name']}  {when}  {r['dur']}s  {r['w']}×{r['h']}  "
                   f"{r['codec'] or '?'}  {pos}")
    return "\n".join(out)
