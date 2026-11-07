"""高德静态地图底图（只给本地出的活动图用）。

为什么用静态地图
----------------
`restapi.amap.com/v3/staticmap` 是 Web 服务接口，一次 GET 就能拿回一张带
折线的 PNG，不用自己算瓦片、不用引 JS SDK，也不吃 jscode 安全密钥（那是
JS API 2021-12 之后新 Key 才强制的）。个人认证每日 5000 次，出图这点量用不完。

合规
----
- 底图必须走持证服务商的在线接口。自己画国界/行政区划是红线，接高德不是。
- 返回的图带高德版权水印，不能裁掉，也不能扒下来当自己的素材分发。
- 轨迹 + 底图 = 更精确的个人位置信息，因此只在本地出图，不随日看板上线。

坐标（重要，踩过两次坑）
------------------------
Garmin 返回的经纬度是 **WGS-84**，高德底图是 **GCJ-02**，两者在国内差 400~500 m，
**必须纠偏**（`DATUM` 默认 `wgs84` 就是「输入是 WGS、要转成 GCJ」）。

曾经判断反过一次：当时把高德 POI 的 GCJ 坐标直接投到底图上，落在图标上，就认定
「数据已经是 GCJ」。这个判据是自证的——POI 坐标和底图都是 GCJ，当然对得上。
真正的判据是拿**高德自己的路径规划**（walking，返回的 polyline 必然压在路网上）
去比：不纠偏时轨迹与规划线**平行错开约 400 m（图上 50 px）**，纠偏后两者贴合。
`tools/mapcheck.py --road` 就是这个检查，改坐标系/取景后跑一遍。

若哪天数据源换成已经是 GCJ 的（例如某些国内第三方接口），设 `AMAP_DATUM=gcj`
关掉纠偏。

配置
----
把 Key 写进 `data/amap.json`：

    {"key": "你的 Web 服务 Key"}

或者设环境变量 `AMAP_KEY`。没有 Key 时 `ensure()` 直接返回 None，
调用方自动回落到现在的矢量轨迹，行为完全不变。
"""
from __future__ import annotations

import json
import math
import os
import urllib.request
from pathlib import Path

def _home() -> Path:
    """数据目录：桌面用项目根，APK 里由 Kotlin 侧用 GARMIN_HOME 指到 filesDir。

    和 plan.attach() 同一个坑：APK 里 `__file__` 在 Chaquopy 的只读 assets 内，
    用它推 data/ 会把缓存和 Key 都指到读不到、也写不进的地方。
    """
    env = os.environ.get("GARMIN_HOME")
    return Path(env).expanduser() if env else Path(__file__).resolve().parent.parent


ROOT = _home()
CONF = ROOT / "data" / "amap.json"
CACHE = ROOT / "data" / "basemaps"

API = "https://restapi.amap.com/v3/staticmap"
GEO = "https://restapi.amap.com/v3/geocode/regeo"


def key() -> str | None:
    """读 Key：先看环境变量，再看 data/amap.json。"""
    k = (os.environ.get("AMAP_KEY") or "").strip()
    if k:
        return k
    try:
        data = json.loads(CONF.read_text("utf-8"))
    except Exception:
        return None
    k = str(data.get("key") or "").strip()
    return k or None


def available() -> bool:
    return key() is not None


# ------------------------------------------------------------------ 坐标纠偏
_A = 6378245.0
_EE = 0.00669342162296594323


def _out_of_china(lon: float, lat: float) -> bool:
    return not (73.66 < lon < 135.05 and 3.86 < lat < 53.55)


def _t_lat(x: float, y: float) -> float:
    r = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
         + 0.2 * math.sqrt(abs(x)))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    r += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    r += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return r


def _t_lon(x: float, y: float) -> float:
    r = (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y
         + 0.1 * math.sqrt(abs(x)))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    r += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    r += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return r


def wgs84_to_gcj02(lon: float, lat: float) -> tuple[float, float]:
    if _out_of_china(lon, lat):
        return lon, lat
    dlat = _t_lat(lon - 105.0, lat - 35.0)
    dlon = _t_lon(lon - 105.0, lat - 35.0)
    rad = lat / 180.0 * math.pi
    magic = math.sin(rad)
    magic = 1 - _EE * magic * magic
    sq = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sq) * math.pi)
    dlon = (dlon * 180.0) / (_A / sq * math.cos(rad) * math.pi)
    return lon + dlon, lat + dlat


# ------------------------------------------------------------------ 取图
# 高德静态地图一个 zoom 级别对应 **512** 像素的世界宽度，不是 Web 墨卡托标准的
# 256（等价于别人家的 zoom+1）。这条是实测出来的：把一条已知经纬度的短线交给
# 高德自己画（paths），再和我们算出的像素长度比，比值稳定在 2.0。早前按 256 算，
# 整张底图就比轨迹「放大了一倍」，位置上怎么调都对不上。
WORLD_TILE = 512.0


def _world(lat: float, lon: float, z: int) -> tuple[float, float]:
    """世界坐标（CSS 像素）。高德底图用的就是这套投影，只是世界宽度是 512·2^z。"""
    size = WORLD_TILE * (2 ** z)
    x = (lon + 180.0) / 360.0 * size
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * size
    return x, y


# Garmin 返回 WGS-84，高德底图是 GCJ-02 —— 默认**要纠偏**。
# AMAP_DATUM=gcj 表示「数据已是 GCJ，别再转」（详见文件头「坐标」一节）。
DATUM = (os.environ.get("AMAP_DATUM") or "wgs84").lower()


def to_gcj(lat: float, lon: float) -> tuple[float, float]:
    """统一入口：按 DATUM 决定要不要纠偏，返回 (lat, lon)。"""
    if DATUM == "wgs84":
        glon, glat = wgs84_to_gcj02(lon, lat)
        return glat, glon
    return lat, lon


def _gcj(track: list[list[float]]) -> list[tuple[float, float]]:
    """轨迹点 → 底图坐标系（lon, lat）。"""
    return [to_gcj(p[0], p[1])[::-1] for p in track]


MAX_ZOOM = 17
REV = 4          # 取景/坐标版本，变了就让 ensure() 把老缓存重取一遍
                 # rev4：默认改为 WGS→GCJ 纠偏（此前按「已是 GCJ」处理，整条轨迹偏西）


def _span(track: list, z: int) -> tuple[float, float]:
    """整条轨迹在 zoom=z 下的外框像素尺寸（宽, 高）。"""
    pts = _gcj(track)
    x0, y0 = _world(max(p[1] for p in pts), min(p[0] for p in pts), z)
    x1, y1 = _world(min(p[1] for p in pts), max(p[0] for p in pts), z)
    return max(x1 - x0, 1e-6), max(y1 - y0, 1e-6)


def fit(track: list[list[float]], w: int, h: int, pad: float = 18.0) -> dict:
    """按轨迹范围定取景：算中心经纬度、一个整数 zoom，以及一个回缩比例 s。

    为什么不用「自动取景」（不传 location/zoom）：它留的内边距是黑盒，
    我们没法把自绘的轨迹叠上去——第一版就是这么错开的。现在取景完全由
    location+zoom 决定，几何可算，于是底图只当背景，轨迹由我们自己画。

    缩放一定要往「缩」的方向走
    --------------------------
    整数 zoom 的粒度是 2 倍，所以「刚好装下」的那一档一定留着大半空闲。早前
    为了把轨迹撑满画幅，把底图整体放大（上限 1.2）——底图一放大，路网看着就
    像被「推近」了一层，跟用户脑子里的实际范围对不上，而且放大必糊。

    现在的做法：跳到**深一档** zoom（轨迹比画幅略大），再整体缩小 s≤1 装回来。
    底图只会被缩小（缩不会糊，高德 scale=2 给了 2 倍像素），轨迹占位更满，
    而且底图永远是「够大再裁」的，绝不会因为 s<1 而露出卡片底色。
    """
    pts = _gcj(track)
    if len(pts) < 2:
        raise ValueError("轨迹点不足，无法取景")
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    cx, cy = (min(lons) + max(lons)) / 2, (min(lats) + max(lats)) / 2
    need_w, need_h = w - 2 * pad, h - 2 * pad

    z = 4
    for cand in range(MAX_ZOOM, 3, -1):
        bw, bh = _span(track, cand)
        if bw <= need_w and bh <= need_h:
            z = cand
            break
    if z < MAX_ZOOM:
        z += 1                      # 深一档 → 保证下面算出来的 s 落在 (0.5, 1]

    bw, bh = _span(track, z)
    s = max(min(need_w / bw, need_h / bh, 1.0), 0.5)
    # 底图要按「回缩后的画幅」取，才能整张贴满卡片：s<1 时取更大的图再缩小
    iw, ih = math.ceil(w / s), math.ceil(h / s)
    return {"fit": "fixed", "rev": REV, "center": [round(cx, 6), round(cy, 6)],
            "zoom": z, "w": iw, "h": ih, "s": round(s, 4),
            "frame": [w, h], "pad": pad}


def to_px(lat: float, lon: float, meta: dict) -> tuple[float, float]:
    """经纬度（WGS-84，跟轨迹数据一致）→ 底图画幅内的 CSS 像素坐标。"""
    glat, glon = to_gcj(lat, lon)
    cx, cy = _world(meta["center"][1], meta["center"][0], meta["zoom"])
    x, y = _world(glat, glon, meta["zoom"])
    return x - cx + meta["w"] / 2, y - cy + meta["h"] / 2


# ------------------------------------------------------------------ 地名
_SUF = ("维吾尔自治区", "壮族自治区", "回族自治区", "自治区",
        "特别行政区", "省", "市")


def _short(name: str) -> str:
    for suf in _SUF:
        if name.endswith(suf):
            return name[:-len(suf)]
    return name


def regeo(lat: float, lon: float, k: str | None = None) -> str:
    """逆地理编码取「省·市」，如「陕西·西安」。失败返回空串（图上去掉这一段）。"""
    k = k or key()
    if not k:
        return ""
    lat, lon = to_gcj(lat, lon)
    url = f"{GEO}?location={lon:.6f},{lat:.6f}&extensions=base&key={k}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            j = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception:
        return ""
    if str(j.get("status")) != "1":
        return ""
    ac = ((j.get("regeocode") or {}).get("addressComponent") or {})
    prov = _short(str(ac.get("province") or ""))
    city = ac.get("city") or ""
    if isinstance(city, list):          # 直辖市返回空数组
        city = ""
    city = _short(str(city))
    if not prov:
        return city
    if not city or city == prov:
        return prov
    return f"{prov}·{city}"


def static_url(meta: dict, k: str) -> str:
    """按给定取景拼 URL：只有底图，不带 paths（折线我们自己画）。"""
    return (f"{API}?location={meta['center'][0]:.6f},{meta['center'][1]:.6f}"
            f"&zoom={meta['zoom']}&size={meta['w']}*{meta['h']}"
            f"&scale=2&key={k}")


def save_key(k: str) -> Path:
    """把 Key 写进 data/amap.json（其它字段保留），返回配置文件路径。"""
    data = {}
    try:
        data = json.loads(CONF.read_text("utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        pass
    data["key"] = k.strip()
    CONF.parent.mkdir(parents=True, exist_ok=True)
    CONF.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return CONF


# 高德错误码 → 人话。出问题时最常撞的就是前四个。
_ERR = {
    "10001": "Key 无效（或已被删除）",
    "10002": "Key 被停用 / 服务已下架",
    "10003": "当日配额已用完",
    "10004": "单位时间内请求过于频繁",
    "10005": "IP 白名单限制：控制台里把「IP 白名单」改成「无」，或加上本机公网 IP",
    "10006": "绑定的域名 / 数字签名不匹配",
    "10007": "数字签名未通过（Key 配了「数字签名」但没带 sig）",
    "10008": "Key 绑定的平台不对",
    "10009": "Key 的服务平台类型不匹配——必须选「Web服务」，不能是「Web端(JS API)」",
    "10044": "该 Key 的日调用量已达上限",
    "20003": "缺少必填参数",
}
# 用来探活的一小段折线（上海世纪公园一带），只为让高德回一张图
_PROBE_TRACK = [[31.2190, 121.5520], [31.2210, 121.5560], [31.2230, 121.5600]]


def probe(k: str | None = None, save: Path | None = None) -> tuple[bool, str]:
    """真打一次 staticmap，判断 Key 能不能用。返回 (是否可用, 说明)。"""
    k = (k or key() or "").strip()
    if not k:
        return False, "还没有 Key"
    url = static_url(fit(_PROBE_TRACK, 200, 120), k)
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
    except Exception as e:  # 网络层失败
        return False, f"请求失败：{type(e).__name__} {e}（本机能否访问 restapi.amap.com？）"
    if data.startswith(b"\x89PNG"):
        if save:
            CACHE.mkdir(parents=True, exist_ok=True)
            save.write_bytes(data)
        return True, f"可用（返回 {len(data) // 1024} KB 图片）"
    try:
        j = json.loads(data.decode("utf-8", "ignore"))
    except Exception:
        return False, f"返回了非图片内容：{data[:120]!r}"
    code = str(j.get("infocode") or "")
    return False, f"{_ERR.get(code, j.get('info') or '未知错误')}（infocode={code}）"


def ensure(track: list[list[float]], w: int, h: int, ident: str = "act") -> dict | None:
    """拿一张底图 + 它的取景参数：先看缓存，再打接口。

    返回 `{"png": 路径, "meta": {...}}`；任何一步不顺返回 None，调用方回落到
    矢量轨迹。meta 跟图片一起缓存，因为叠轨迹要靠它算投影。
    """
    if len(track) < 2:
        return None
    cache = CACHE / f"{ident}-{w}x{h}.png"
    meta_f = cache.with_name(cache.stem + ".json")
    if cache.exists() and cache.stat().st_size > 1024 and meta_f.exists():
        try:
            meta = json.loads(meta_f.read_text("utf-8"))
            # 只认当前取景算法出的图：老版本是「自动取景」或「放大底图」，
            # 几何对不上，直接重取，免得叠出来的轨迹跟路网错开。
            if meta.get("fit") == "fixed" and meta.get("rev") == REV:
                if "region" not in meta:
                    meta["region"] = regeo(meta["center"][1], meta["center"][0])
                    meta_f.write_text(
                        json.dumps(meta, ensure_ascii=False, indent=1) + "\n", "utf-8")
                return {"png": cache, "meta": meta}
        except Exception:
            pass
    k = key()
    if not k:
        return None
    meta = fit(track, w, h)
    meta["region"] = regeo(meta["center"][1], meta["center"][0])
    try:
        req = urllib.request.Request(static_url(meta, k), headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = resp.read()
    except Exception:
        return None
    # 高德出错时返回 JSON 而不是图片，这里挡一下别把 JSON 当 PNG 存下来
    if not data.startswith(b"\x89PNG"):
        return None
    CACHE.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(data)
    meta_f.write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", "utf-8")
    return {"png": cache, "meta": meta}
