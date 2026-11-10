"""高驰（COROS）数据源：Training Hub Web API，活动 + 每日健康指标。

为什么走这条路
--------------
高驰有三条取数通路（见 docs/高驰接入评估.md）：
  ① 官方 Open API（open.coros.com，OAuth2）——正道，但要注册开发者应用、
     等审核，朋友没法马上测试；
  ② 官方 MCP 网关——OAuth 扫码授权，但只服务 MCP 协议，没法直接打 REST；
  ③ Training Hub Web API（本模块）——账号密码登录训练平台，口径与网页版
     一致，今天就能测。社区 coros-mcp 就是这条路，接口形态稳定了一年多。
本模块做 ③：登录一次，token 存 `data/coros_auth.json`（0600），之后每次
同步增量拉。等官方 Open API 过审后，只需替换本模块的请求层，下游不动。

协议长什么样
------------
比小米云朴素得多：普通 JSON + 自定义头，没有加密外壳。
    POST /account/login      {account, accountType:2, pwd: md5(密码)}
    响应 {"result":"0000","data":{"accessToken","userId"}}   1019=token 失效
    鉴权头  accessToken: <token>   yfheader: {"userId": ...}
区域决定主机（token 只在本区有效）：
    asia/cn → teamcnapi.coros.com   eu → teameuapi.coros.com   us → teamapi.coros.com

凭据与过期
----------
Web token 服务端不给 refresh，约 24 小时过期（返回 result=1019）。
所以界面要像佳明一样留着账号密码（Android Keystore），过期时传进来静默
重登一次；Python 侧不存密码，只存 token / userId / region。

睡眠分期不在这里
----------------
睡眠阶段（深/浅/REM）只在移动端 API（apieu.coros.com 一族）有，而那套登录
是 AES 加密的私有协议，且**登录一次会把用户手机上的高驰 App 挤下线**。
权衡后本轮不做：日间指标里的 avgSleepHrv / rhr / trainingLoad / vo2max
已经从 Web 侧拿到，睡眠分期列为后续（见 README 限制说明）。

单位陷阱
--------
活动列表的 calorie 是**卡（cal）**不是千卡，入库前 ÷1000；
distance 是米、totalTime 是秒——与佳明侧口径一致，直接进表。
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")

BASE_URLS = {
    "asia": "https://teamcnapi.coros.com",
    "cn": "https://teamcnapi.coros.com",
    "eu": "https://teameuapi.coros.com",
    "us": "https://teamapi.coros.com",
}
REGION_CN = {"asia": "国内", "cn": "国内", "eu": "欧洲", "us": "美国"}

# 高驰 sportType → 项目里的活动类型串（garmin typeKey 口径，看板/图都认它）
# 高驰 sportType 码表（社区整理，与 102/103/104、200/201、400/402 已实测吻合）：
#   100 户外跑 / 101 室内跑（跑步机）/ 102 越野跑 / 103 场地跑 / 104 徒步
#   200 公路骑 / 201 室内骑 / 202 电助力 / 203 碎石骑 / 204 山地骑
#   300 泳池 / 301 公开水域 / 400 健身房有氧 / 401 GPS 有氧 / 402 力量
#   500 双板 / 501 单板 / 502 越野滑雪 / 700 划船 / 701 划船机 / 900 健走 / 904 瑜伽
# 之前缺 101，跑步机被归成 other，卡片上标签写「运动」、也没有步频步幅。
SPORT_TYPE = {
    100: "running", 101: "treadmill_running", 102: "trail_running",
    103: "track_running", 104: "hiking",
    200: "road_biking", 201: "indoor_cycling", 203: "gravel_cycling",
    204: "mountain_biking",
    300: "lap_swimming", 301: "open_water_swimming",
    400: "indoor_cardio", 401: "indoor_cardio", 402: "strength_training",
    500: "skiing", 501: "skiing", 502: "skiing",
    700: "rowing", 701: "indoor_rowing",
    900: "walking", 904: "yoga",
}


class CorosError(Exception):
    pass


class CorosAuthError(CorosError):
    pass


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _looks_like_phone(account: str) -> bool:
    """账号看着像手机号（去掉空格/短横后纯数字，7~15 位）。

    高驰允许手机号注册，而手机号账号的 accountType 与邮箱不同；不判断就直接
    按邮箱类型发，服务端只会回一个笼统的 1030，用户根本不知道错在哪。
    """
    t = "".join(ch for ch in str(account or "") if ch not in " +-()")
    return t.isdigit() and 7 <= len(t) <= 15


def _login_types(account: str) -> list[int]:
    """按账号形状给 accountType 的尝试顺序。

    服务端对「账号类型不对」和「密码错」回同一个 1030，实测也看不出 1/2 哪个
    是邮箱、哪个是手机号，所以两种都试一遍（最多两次请求）：
    邮箱形状先 2 后 1，手机号形状先 1 后 2。真实用户里「手机号能进、邮箱进不去」
    就是账号根本不是用邮箱注册的，最多两次请求换一个明确结论，值得。
    """
    return [1, 2] if _looks_like_phone(account) else [2, 1]


# 1030 是「账号或密码不匹配」的笼统回复：服务端不区分「账号不存在」「密码错」
# 「账号类型不对」「区域选错」，也不说是哪个。所以把排查顺序写进报错里，
# 否则普通用户只能看到一句英文然后放弃。
LOGIN_1030_HINT = (
    "高驰账号或密码不匹配（result=1030）。按顺序确认：\n"
    "① 登录名只用注册时的手机号或邮箱；绑定的邮箱往往不能当登录名，"
    "邮箱进不去就换手机号试；\n"
    "② 账号不是手表昵称，也不要填中文；\n"
    "③ 用微信 / Apple 一键注册的账号没有密码，要先去高驰 App 里设一个；\n"
    "④ 账号是在海外注册的，把「账号区域」切到欧洲或美国（默认国内）；\n"
    "⑤ 密码区分大小写，末尾别带空格。"
)


def _iso(yyyymmdd) -> str | None:
    s = str(yyyymmdd or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    # 不同版本的 happenDay 见过 "2026-11-10" / "2026/11/10" / 带时刻的
    # "2026-11-10 00:00:00"。以前只认 8 位数字，形状一变就整天被丢掉，
    # 看起来像「这几天没有静息心率」。只补日期格式，不猜时间戳（时区会错）。
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def _day_key(iso: str) -> str:
    return iso.replace("-", "")[:8]


def _ts(v) -> str | None:
    """epoch 秒/毫秒 → 本地 "YYYY-MM-DDTHH:MM:SS"（与佳明 startTimeLocal 同形）。"""
    if v in (None, ""):
        return None
    try:
        t = float(v)
    except (TypeError, ValueError):
        return None
    if t > 1e12:              # 毫秒
        t /= 1000
    if t < 1e8:               # 不是时间戳
        return None
    return dt.datetime.fromtimestamp(t).strftime("%Y-%m-%dT%H:%M:%S")


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _tsec(v) -> float | None:
    """时间戳统一到 epoch 秒。

    高驰不同接口单位不一：活动列表是毫秒，明细（summary/lapList/frequencyList）
    是**厘秒**（社区解析器对 startTimestamp 做 value/100 换算证实），个别接口
    又给秒。按数量级区分，别把 2026 年解析成 2528 年。
    """
    if v in (None, ""):
        return None
    try:
        t = float(v)
    except (TypeError, ValueError):
        return None
    if t > 1e12:                  # 毫秒
        t /= 1000
    elif t > 1e10:                # 厘秒
        t /= 100
    if t < 1e8:                   # 不是时间戳
        return None
    return t


class CorosCloud:
    """一个高驰账号的会话。与 mi.MiCloud 同一持有方式：登录 → 存 cookie/token。"""

    def __init__(self, home: Path):
        self.home = Path(home)
        self.auth_path = self.home / "data" / "coros_auth.json"
        self.access_token = ""
        self.user_id = ""
        self.region = "asia"
        self.email = ""

    # ---------------- 凭据 ----------------
    def load(self) -> bool:
        try:
            d = json.loads(self.auth_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        self.access_token = d.get("access_token") or ""
        self.user_id = d.get("user_id") or ""
        self.region = d.get("region") or "asia"
        self.email = d.get("email") or ""
        return bool(self.access_token)

    def save(self) -> None:
        self.auth_path.parent.mkdir(parents=True, exist_ok=True)
        self.auth_path.write_text(json.dumps({
            "access_token": self.access_token,
            "user_id": self.user_id,
            "region": self.region,
            "email": self.email,
            "saved_at": dt.datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        self.auth_path.chmod(0o600)

    def clear(self) -> None:
        try:
            self.auth_path.unlink()
        except OSError:
            pass

    # ---------------- HTTP ----------------
    def _base(self) -> str:
        return BASE_URLS.get(self.region, BASE_URLS["asia"])

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "User-Agent": UA,
            "accessToken": self.access_token,
            "yfheader": json.dumps({"userId": self.user_id}),
        }

    def _req(self, method: str, path: str, *, params: dict | None = None,
             body: dict | None = None, form: dict | None = None,
             headers: dict | None = None, timeout: int = 25) -> dict:
        url = self._base() + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        data = None
        hs = dict(headers or self._headers())
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            hs.pop("Content-Type", None)     # 让 urllib 自己带 urlencoded 头
        elif body is not None:
            data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, headers=hs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                # /activity/detail/query 一类大响应会回 gzip（头里带 Content-Encoding，
                # 偶尔不带），统一在入口解掉，别让下游把压缩流当"不是 JSON"
                enc = (r.headers.get("Content-Encoding") or "").lower()
                if "gzip" in enc or raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                text = raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            snippet = e.read().decode("utf-8", "replace")[:200]
            raise CorosError(f"高驰接口 HTTP {e.code}：{snippet}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise CorosError(f"连不上高驰服务器：{type(e).__name__}: {e}") from None
        try:
            out = json.loads(text)
        except json.JSONDecodeError:
            raise CorosError(f"高驰返回的不是 JSON（可能改版）：{text[:160]}") from None
        code = str(out.get("result"))
        if code == "1019":
            raise CorosAuthError("登录已过期（token 失效），请重新登录")
        if code != "0000":
            msg = out.get("message") or out.get("msg") or code
            if code in ("1002", "1003", "1004"):
                raise CorosAuthError(f"高驰拒绝：{msg}")
            raise CorosError(f"高驰接口报错（result={code} {msg}）")
        return out

    # ---------------- 登录 ----------------
    def _login_once(self, account: str, password: str, account_type: int) -> dict:
        """一次账号密码登录。account_type 2=邮箱、1=手机号（服务端不校验格式，
        类型不对只会回笼统的 1030，所以要靠调用方按账号形状逐个试）。"""
        body = {
            "account": account,
            "accountType": account_type,
            "pwd": _md5(password),
        }
        headers = {"Content-Type": "application/json", "User-Agent": UA}
        return self._req("POST", "/account/login", body=body, headers=headers)

    def login(self, email: str, password: str, region: str = "asia") -> dict:
        """账号密码登录。密码只在本次调用里用（md5 后发出，不落盘）。"""
        if not email or not password:
            raise CorosAuthError("账号和密码都要填")
        self.region = region if region in BASE_URLS else "asia"
        self.email = email
        # 邮箱形状先 2 后 1，手机号形状先 1 后 2；两次都失败才抛带排查步骤的报错。
        types = _login_types(email)
        out = None
        last_err = ""
        for atype in types:
            try:
                out = self._login_once(email, password, atype)
                break
            except CorosAuthError as e:
                last_err = str(e)
                if "1030" not in last_err:
                    raise CorosAuthError(f"{e}（也可能是账号或密码不对）") from None
            except CorosError as e:
                last_err = str(e)
                if "1030" not in last_err:
                    raise
        if out is None:
            raise CorosAuthError(LOGIN_1030_HINT) from None
        data = out.get("data") or {}
        self.access_token = data.get("accessToken") or ""
        self.user_id = str(data.get("userId") or "")
        if not self.access_token:
            raise CorosAuthError("登录没有返回 token（高驰改版了？）")
        # 先用一次轻接口验票，坏凭据别等到同步时才发现
        try:
            self._req("GET", "/dashboard/query")
        except CorosAuthError:
            raise CorosAuthError("登录响应拿到了 token 但验证未通过，请重试") from None
        self.save()
        return {"user_id": self.user_id, "region": self.region}

    # ---------------- 数据 ----------------
    def daily_records(self, start: str, end: str) -> list[dict]:
        """每日健康指标：dayDetail 区间 + analyse 摘要合并。

        拿到 resting_hr / hrv_night / hrv_weekly / vo2max / distance_m。
        已知 dayDetail 不给步数/睡眠分期/压力（那些在移动端 API，见模块头），
        但键名万一在改版里加了就顺手吃进——upsert merge 模式不会用 None
        抹掉别的数据源写过的值。
        """
        out = self._req("GET", "/analyse/dayDetail/query",
                        params={"startDay": _day_key(start), "endDay": _day_key(end)})
        day_list = ((out.get("data") or {}).get("dayList")) or []
        # t7dayList（近 28 天）里有更权威的 vo2max / 阈值 / 耐力
        extra: dict[str, dict] = {}
        try:
            ana = self._req("GET", "/analyse/query")
            for it in ((ana.get("data") or {}).get("t7dayList")) or []:
                d = _iso(it.get("happenDay"))
                if d:
                    extra[d] = it
        except CorosError:
            pass                      # 摘要拿不到就用 dayList 自己的字段

        rows = []
        for it in day_list:
            d = _iso(it.get("happenDay"))
            if not d:
                continue
            m = extra.get(d) or {}
            # 睡眠/步数：官方文档口径没有，但不同版本 dayList 出现过
            # sleepTime/totalSleepTime/steps 这类键，见到了就收
            sl_min = _num(it.get("totalSleepTime") or it.get("sleepTime")
                          or it.get("sleepDuration") or m.get("totalSleepTime"))
            rows.append({
                "date": d,
                "resting_hr": _num(it.get("rhr")),
                "hrv_night": _num(it.get("avgSleepHrv")),
                "hrv_weekly": _num(it.get("sleepHrvBase")),
                "vo2max": _num(m.get("vo2max") or it.get("vo2max")),
                "distance_m": _num(it.get("distance")),
                "sleep_total_min": sl_min,
                "steps": _num(it.get("steps") or it.get("step") or m.get("steps")),
                "synced_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                "raw": json.dumps(it, ensure_ascii=False)[:20000],
            })
        # 静息心率口径排障：把服务端给的 happenDay / rhr 与同一条记录里所有
        # 心率相关键留一行在诊断日志。用户不用导出数据库，只要贴这几行文字，
        # 就能判断「App 52 / 这里 45」是服务端口径不同、日期错位，还是我们取错。
        try:
            from . import diag as _dg
            _dg.log_exc("coros.rhr", "dayDetail 最近: " + " | ".join(
                f"{r['date']}:rhr={r['resting_hr']}" for r in rows[-3:]))
            if day_list:
                hr_keys = {k: v for k, v in day_list[-1].items()
                           if "hr" in str(k).lower() or "heart" in str(k).lower()}
                _dg.log_exc("coros.rhr", "HR 字段: "
                            + json.dumps(hr_keys, ensure_ascii=False)[:400])
        except Exception:
            pass
        return rows

    def activities(self, start: str, end: str, limit: int = 60) -> list[dict]:
        """活动列表 → 项目 activities 表行（字段翻译见模块 docstring）。"""
        items: list = []
        page, size = 1, 30
        while len(items) < limit:
            out = self._req("GET", "/activity/query", params={
                "startDay": _day_key(start), "endDay": _day_key(end),
                "pageNumber": page, "size": size,
            })
            data = out.get("data") or {}
            batch = data.get("dataList") or data.get("list") or []
            items.extend(batch)
            total = data.get("totalCount") or data.get("count") or len(items)
            if not batch or len(items) >= total or len(items) >= limit:
                break
            page += 1
        return [r for r in (self._parse_activity(a) for a in items[:limit]) if r]

    def _parse_activity(self, a: dict) -> dict | None:
        aid = a.get("labelId")
        if aid in (None, ""):
            return None
        dist = _num(a.get("distance") or a.get("totalDistance"))
        dur = _num(a.get("totalTime"))
        pace = None
        if dist and dur and dist > 1000:
            pace = round(dur / (dist / 1000))
        start_ts = _ts(a.get("startTime"))
        cal = _num(a.get("calorie"))
        return {
            "activity_id": str(aid),
            "date": (start_ts or "")[:10] or _iso(a.get("happenDay")),
            "start_ts": start_ts,
            "type": SPORT_TYPE.get(a.get("sportType"), "other"),
            "name": a.get("name") or a.get("remark") or "高驰活动",
            "duration_s": dur,
            "distance_m": dist,
            "avg_hr": _num(a.get("avgHr")),
            "max_hr": _num(a.get("maxHr")),
            # 高驰给的是卡（cal）：60 万 ≈ 600 kcal，入库与佳明同口径用 kcal
            "calories": round(cal / 1000, 1) if cal else None,
            "load": _num(a.get("trainingLoad")),
            "pace_s_km": pace,
            "ascent": _num(a.get("ascent") or a.get("totalAscent")
                          or a.get("elevationGain")),
            "source": "coros",
            "raw": json.dumps(a, ensure_ascii=False)[:100000],
        }

    def activity_detail(self, activity_id: str, sport_type: int | None = None) -> dict:
        """单条活动详情（laps / 心率区间等）。时序大数组原样留在 payload。

        响应 data 的关键键（两个独立社区项目交叉证实）：
          summary          汇总（时间戳是厘秒，见 _tsec）
          frequencyList    时序数组 [{timestamp, heart, cadence, distance, heartLevel}]
          lapList          [{type, lapItemList: [...]}]，lap 含 avgHr/avgCadence/
                           avgStrideLength/distance/startTimestamp/endTimestamp
          graphList        更大的时序数组（含 GPS 等），本项目用 GPX 拿轨迹，不解析它
        """
        form = {"labelId": str(activity_id), "userId": self.user_id}
        if sport_type is not None:
            form["sportType"] = str(sport_type)
        out = self._req("POST", "/activity/detail/query", form=form)
        return out.get("data") or {}

    def download_file(self, activity_id: str, sport_type: int | None = None,
                      file_type: int = 1) -> bytes:
        """下载活动文件（0=CSV 1=GPX 2=KML 3=TCX 4=FIT）。

        先 POST /activity/detail/download 换临时 fileUrl，再 GET 文件本体。
        室内活动（跑步机等）没有位置数据，高驰不给 GPX——调用方自行降级。
        """
        form = {"labelId": str(activity_id), "fileType": str(file_type),
                "sportType": str(sport_type or 0)}
        out = self._req("POST", "/activity/detail/download", form=form)
        url = ((out.get("data") or {}).get("fileUrl")) or ""
        if not url:
            raise CorosError("高驰没给下载链接（该活动类型可能不支持这种文件）")
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise CorosError(f"下载活动文件失败：{type(e).__name__}: {e}") from None


# ---------------------------------------------------------------- 明细 → 出图契约
def _gpx_track(gpx_bytes: bytes) -> list[list[float]]:
    """GPX → [[lat, lon], ...]。命名空间不敏感，GPX 1.1 都认。"""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(gpx_bytes)
    except ET.ParseError:
        return []
    pts: list[list[float]] = []
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] != "trkpt":
            continue
        try:
            pts.append([float(el.get("lat")), float(el.get("lon"))])
        except (TypeError, ValueError):
            continue
    return pts


def detail_to_payload(data: dict, track: list[list[float]], row: dict) -> dict:
    """兼容旧入口：单位换算与统计统一交给 coros_activity.normalize。

    历史上这里有一份独立实现，把 lap 的厘米值当米用（1 km 圈显示成
    100.00 km、配速 0'06"），时序 distance 也没换算导致速度全被判成漂移。
    现在只保留 coros_activity 一份，两处入口不会再漂移。
    """
    from . import coros_activity as CA

    return CA.normalize(data, str(row.get("activity_id") or ""), track=track or None)


def fetch_detail(store, home: Path, row: dict,
                 email: str | None = None, password: str | None = None) -> dict:
    """高驰活动明细 → actcard 契约。token 过期且带了凭据就静默重登一次。

    轨迹走 GPX 下载（frequencyList 没有经纬度）；室内活动拿不到 GPX
    就出无轨迹版图，不报错。
    """
    cloud = CorosCloud(home)
    if not cloud.load():
        raise CorosAuthError("还没登录高驰账号 → 菜单「账号登录」选高驰")
    try:
        raw = json.loads(row.get("raw") or "{}")
    except (TypeError, ValueError):
        raw = {}
    sport = raw.get("sportType")
    sport = sport if isinstance(sport, int) else None

    def _run() -> dict:
        data = cloud.activity_detail(row["activity_id"], sport)
        track: list[list[float]] = []
        try:
            track = _gpx_track(cloud.download_file(row["activity_id"], sport, file_type=1))
        except CorosError:
            pass                  # 跑步机/室内没有位置数据：出无轨迹版
        return detail_to_payload(data, track, row)

    try:
        return _run()
    except CorosAuthError:
        if not (email and password):
            raise
        cloud.login(email, password, cloud.region)   # 24h token 过期：静默重登
        return _run()


# ---------------------------------------------------------------- 便捷入口
# 补历史时一次请求的天数。高驰的 dayDetail 给的是区间明细，一次要一年
# 在手机上又慢又容易被限流；90 天一段，八年的账号也就 30 来段。
RANGE_CHUNK_DAYS = 90


def _window(store, cloud, start: dt.date, end: dt.date,
            activity_limit: int = 400) -> dict:
    """拉一个区间的日汇总 + 活动。日汇总按 90 天切块，活动一次按区间翻页。"""
    daily_rows: list[dict] = []
    cur = start
    while cur <= end:
        stop = min(end, cur + dt.timedelta(days=RANGE_CHUNK_DAYS - 1))
        daily_rows.extend(cloud.daily_records(cur.isoformat(), stop.isoformat()))
        cur = stop + dt.timedelta(days=1)
    # 先数一下「库里原本没有的天」，这段真正新增了多少天才说得准
    new_days = 0
    for r in daily_rows:
        d = str(r.get("date") or "")[:10]
        if d and not store.day(d):
            new_days += 1
    for r in daily_rows:
        store.upsert_daily(r, merge=True)
    acts = cloud.activities(start.isoformat(), end.isoformat(), limit=activity_limit)
    for a in acts:
        store.upsert_activity(a)
    store.set_meta("last_sync_coros",
                   dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    return {"daily": len(daily_rows), "new_days": new_days,
            "activities": len(acts), "range": f"{start}~{end}"}


def _with_relogin(home: Path, email: str | None, password: str | None,
                  run) -> dict:
    """跑一次；token 过期且带了凭据就静默重登后重跑一次。"""
    cloud = CorosCloud(home)
    if not cloud.load():
        raise CorosAuthError("还没登录高驰账号 → App 里「账号登录」选高驰")
    try:
        return {**run(cloud), "relogged": False}
    except CorosAuthError:
        if not (email and password):
            raise
        cloud.login(email, password, cloud.region)   # 24h token 过期：静默重登
        return {**run(cloud), "relogged": True}


def sync(store, home: Path, days: int = 30,
         email: str | None = None, password: str | None = None) -> dict:
    """拉最近 N 天的每日指标 + 活动（下拉刷新的增量路径）。

    返回 {"daily": n, "new_days": n, "activities": n, "range": "..."}
    """
    end = dt.date.today()
    start = end - dt.timedelta(days=max(7, min(int(days), 180)))
    return _with_relogin(home, email, password,
                         lambda c: _window(store, c, start, end))


def sync_range(store, home: Path, start: str, end: str,
               email: str | None = None, password: str | None = None,
               activity_limit: int = 400) -> dict:
    """拉指定区间（补历史用）。与 sync 同一套落库口径，只是区间由调用方给。

    这里**不调用 MCP 睡眠**：高驰的日汇总（dayDetail）本身就带着睡眠字段，
    一次区间请求里顺带解析，没有额外开销；MCP 睡眠只在「下拉刷新」那一步
    对最近一段补（`coros_sync`）。所以高驰侧不存在「老数据白拉睡眠」的问题，
    每段固定 1~2 个区间请求（日汇总 + 活动列表）。
    """
    s = dt.date.fromisoformat(str(start)[:10])
    e = dt.date.fromisoformat(str(end)[:10])
    if e < s:
        s, e = e, s
    return _with_relogin(home, email, password,
                         lambda c: _window(store, c, s, e, activity_limit))


def earliest_activity(home: Path, years: int = 8) -> str:
    """账号里最早一条活动的日期（补历史的停止线）；拿不到返回空串。

    活动列表按时间倒序。先拿第一页读 totalCount，再直接请求最后一页——
    八年数据可能几十页，逐页翻在手机上要十几秒，请求两次就够。
    """
    cloud = CorosCloud(home)
    if not cloud.load():
        return ""
    end = dt.date.today()
    try:
        start = end.replace(year=end.year - max(1, int(years)))
    except ValueError:
        start = end - dt.timedelta(days=365 * max(1, int(years)))
    params = {"startDay": _day_key(start.isoformat()),
              "endDay": _day_key(end.isoformat()), "pageNumber": 1, "size": 30}

    def _batch(page: int) -> tuple[list, int]:
        out = cloud._req("GET", "/activity/query",
                         params={**params, "pageNumber": page})
        data = out.get("data") or {}
        rows = data.get("dataList") or data.get("list") or []
        total = int(_num(data.get("totalCount") or data.get("count")) or 0)
        return rows, total or len(rows)

    rows, total = _batch(1)
    if not rows:
        return ""
    size = 30
    last = max(1, -(-total // size))
    if last > 1:
        rows, _ = _batch(last)
    dates = [(r.get("startTime") or "")[:10] for r in (rows or [])
             if isinstance(r, dict)]
    dates = [d for d in dates if len(d) == 10]
    return min(dates) if dates else ""
