"""华为运动健康 · 体重同步（协议已验证，2026-11-09 实测）。

链路与小米同构：

    秤 → 运动健康App → 华为健康云 → 本模块 → body 表 → 看板 / 一图流 / AI

三家主机各管一件事（全部为真机抓包确认，不是推测）：

    healthsession.things.dbankcloud.cn   会话：授权码换 token / refresh 续期
    healthcommon.dbankcloud.cn           账号资料（昵称、手机号、uid）
    healthdata.dbankcloud.cn             健康数据（体重在此）

认证方式很朴素：**没有签名**。body 里带 `token`，请求头带 `x-huid`，
其余字段是设备与环境自述。因此只要能拿到一次 token，之后可以完全脱离 App。

已验证的三道闸门：
    A 账号     authorizationCode → userAccessToken/obtain → accessToken
    B 体重     getHealthDataByVersion(type=10006) → WEIGHT_BODYFAT_BROAD
    C 续期     userAccessToken/refresh(refreshToken) → 新 accessToken（实测不轮换）
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------- 常量
APP_ID = "10414141"                     # 运动健康自己的华为 OAuth App ID
APP_PKG = "com.huawei.health"
APP_VER = "and_health_17.0.8.300"
UA = (f"com.huawei.health/17.0.8.300 (Linux; Android 12; 2211133C) "
      f"RestClient/8.0.1.320")

SESSION_BASE = "https://healthsession.things.dbankcloud.cn"
COMMON_BASE = "https://healthcommon.dbankcloud.cn"
DATA_BASE = "https://healthdata.dbankcloud.cn"

P_OBTAIN = "/commonAbility/userAccessToken/obtain"
P_REFRESH = "/commonAbility/userAccessToken/refresh"
P_QUERY = "/commonAbility/userAccessToken/query"
P_BY_VERSION = "/dataQuery/health/getHealthDataByVersion"
P_VERSIONS = "/dataQuery/common/getSyncVersions"

AUTH_NAME = "huawei_auth.json"
PROTO_NAME = "huawei_protocol.json"

WEIGHT_TYPE = 10006                     # 华为体重/体脂的数据类型号
WEIGHT_KEY = "WEIGHT_BODYFAT_BROAD"     # 采样点里的 key

TOKEN_MARGIN = 300                      # 提前 5 分钟续，避开刚好过期的边界
MAX_PAGES = 80                          # 分页护栏：参数写错时不至于打爆对端
PAGE_ITEMS = 113                        # 单页上限（实测值，仅用于判断「还有下一页」）


class HwError(RuntimeError):
    """华为侧的通用失败。"""


class HwAuthError(HwError):
    """会话相关：没登录、过期且续不上、被拒。"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _http(method: str, url: str, *, body: dict | None = None,
          headers: dict | None = None, timeout: int = 30) -> tuple[int, dict]:
    """发一次 JSON 请求。返回 (HTTP 状态码, 解析后的 JSON)。

    网络层异常统一翻译成 HwError，调用方不用区分 urllib 的七八种异常。
    """
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    hdrs = {"User-Agent": UA, "x-version": APP_VER}
    hdrs.update(headers or {})
    if data is not None:
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "ignore")
            status = resp.status
    except urllib.error.HTTPError as e:
        text = (e.read() or b"").decode("utf-8", "ignore")
        status = e.code
    except Exception as e:                      # 网络不可达、TLS、超时
        raise HwError(f"请求不通（{url.split('/')[2]}）：{type(e).__name__} {e}") from None
    try:
        return status, (json.loads(text) if text.strip() else {})
    except Exception:
        raise HwError(f"华为返回的不是 JSON（HTTP {status}）：{text.strip()[:160]}") from None


# ---------------------------------------------------------------- 会话
class HuaweiCloud:
    """一份会话。对外方法与 `mi.MiCloud` 对齐，下游无需区分厂商。

    会话文件只放凭据，不放任何健康数值；权限 0600。
    """

    def __init__(self, home: Path):
        self.home = Path(home)
        self.auth_path = self.home / "data" / AUTH_NAME
        self.proto_path = self.home / "data" / PROTO_NAME
        self.uid = ""
        self.access_token = ""
        self.refresh_token = ""
        self.expires_at = 0.0               # epoch 秒
        self.device_id = ""
        self.diag = ""

    # ---------- 凭据 ----------
    def load(self) -> bool:
        if not self.auth_path.exists():
            return False
        try:
            d = json.loads(self.auth_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        self.uid = str(d.get("uid") or "")
        self.access_token = str(d.get("accessToken") or "")
        self.refresh_token = str(d.get("refreshToken") or "")
        try:
            self.expires_at = float(d.get("expiresAt") or 0) / 1000.0
        except (TypeError, ValueError):
            self.expires_at = 0.0
        self.device_id = str(d.get("deviceId") or "")
        return bool(self.access_token or self.refresh_token)

    def save(self, note: str = "") -> None:
        self.auth_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "uid": self.uid,
            "accessToken": self.access_token,
            "refreshToken": self.refresh_token,
            "expiresAt": int(self.expires_at * 1000),
            "deviceId": self.device_id,
            "appId": APP_ID,
            "note": note,
            "savedAt": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        self.auth_path.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        try:
            self.auth_path.chmod(0o600)
        except Exception:
            pass

    def clear(self) -> None:
        try:
            self.auth_path.unlink()
        except FileNotFoundError:
            pass

    def expired(self, margin: int = TOKEN_MARGIN) -> bool:
        return not self.access_token or self.expires_at <= time.time() + margin

    def has_auth(self) -> bool:
        return bool(self.access_token or self.refresh_token)

    # ---------- 设备标识 ----------
    def _device(self) -> str:
        """设备号沿用 App 上报的那个；没有就生成一个稳定的。

        换设备号会让云端的同步状态重新开一份，能沿用就沿用。
        """
        if not self.device_id:
            seed = f"{self.uid}|{APP_ID}".encode()
            self.device_id = hashlib.sha1(seed).hexdigest()[:16]
        return self.device_id

    # ---------- 三道闸门的原料 ----------
    def obtain(self, authorization_code: str) -> dict:
        """授权码换会话。authorization_code 来自华为账号 OAuth 登录的跳转码。"""
        if not authorization_code:
            raise HwAuthError("缺少 authorizationCode")
        status, res = _http("POST", SESSION_BASE + P_OBTAIN,
                            body={"authorizationCode": authorization_code, "appId": APP_ID},
                            headers={"x-ts": str(_now_ms())})
        self.diag = json.dumps(res, ensure_ascii=False)[:300]
        if res.get("resultCode") != 0 or not res.get("accessToken"):
            raise HwAuthError(f"授权码换 token 失败（HTTP {status}）：{self.diag}")
        self._take(res)
        return res

    def refresh(self) -> bool:
        """用 refreshToken 续期。实测服务端不轮换 refreshToken，可与 App 共存。"""
        if not self.refresh_token:
            raise HwAuthError("没有 refresh_token，无法自动续期 → 需要重新授权一次")
        if not self.uid:
            raise HwAuthError("会话文件缺 uid（x-huid 头），请重新导入")
        status, res = _http("POST", SESSION_BASE + P_REFRESH,
                            body={"refreshToken": self.refresh_token, "appId": APP_ID},
                            headers={"x-ts": str(_now_ms()), "x-huid": self.uid})
        self.diag = json.dumps(res, ensure_ascii=False)[:300]
        if res.get("resultCode") != 0 or not res.get("accessToken"):
            raise HwAuthError(f"续期失败（HTTP {status}）：{self.diag}")
        self._take(res)
        return True

    def _take(self, res: dict) -> None:
        self.access_token = str(res.get("accessToken") or self.access_token)
        # 服务端将来若轮换 refreshToken，这里自动跟着换，不会把自己锁在外面
        self.refresh_token = str(res.get("refreshToken") or self.refresh_token)
        exp = res.get("accessTokenExpireTime")
        if exp:
            self.expires_at = float(exp) / 1000.0
        else:
            self.expires_at = time.time() + 3600
        self.uid = str(res.get("uid") or self.uid)
        self._device()

    def open_session(self) -> bool:
        """确保手里有一个还能用的 accessToken。与 `mi.open_session` 同义。"""
        if self.access_token and not self.expired():
            return True
        return self.refresh()

    def import_payload(self, payload: dict, note: str = "import") -> dict:
        """导入一份会话。同时吃三种形状，免得用户还要自己转换。"""
        if not isinstance(payload, dict):
            raise HwAuthError("会话内容不是 JSON 对象")

        def pick(*names: str) -> str:
            for n in names:
                v = payload.get(n)
                if v:
                    return str(v)
            return ""

        at = pick("accessToken", "access_token", "token")
        rt = pick("refreshToken", "refresh_token")
        uid = pick("uid", "openId", "open_id", "huid")
        exp = payload.get("accessTokenExpireTime") or payload.get("expiresAt")
        dev = pick("deviceId", "device_id")
        if not at and not rt:
            raise HwAuthError(
                "这里没有 accessToken / refreshToken。\n"
                "  要的是 healthsession 的 obtain 响应，例如：\n"
                '  {"resultCode":0,"accessToken":"…","refreshToken":"…","uid":"…"}')
        self.access_token = at
        self.refresh_token = rt
        self.uid = uid
        if dev:
            self.device_id = dev
        if exp:
            try:
                exp = float(exp)
                self.expires_at = exp / 1000.0 if exp > 1e11 else exp
            except (TypeError, ValueError):
                self.expires_at = time.time() + 3600
        else:
            self.expires_at = time.time() + 3600
        self._device()
        if not self.expires_at:
            self.expires_at = time.time() + 3600
        self.save(note=note)
        return {"uid": self.uid, "hasRefresh": bool(self.refresh_token),
                "expiresAt": int(self.expires_at * 1000)}

    def whoami(self) -> dict:
        """用 query 接口确认这把 token 真的是「有健康数据权限」的那个账号。"""
        if not self.open_session():
            raise HwAuthError("没有可用会话")
        status, res = _http("POST", COMMON_BASE + P_QUERY,
                            body={"accessToken": self.access_token, "appId": APP_ID},
                            headers={"x-ts": str(_now_ms()), "x-huid": self.uid})
        if res.get("resultCode") != 0:
            raise HwAuthError(f"账号查询失败（HTTP {status}）：{res}")
        return res

    # ---------- 体重 ----------
    def _query_page(self, version: int) -> list[dict]:
        """按 version 游标取一页。服务端一次最多回一百来条，靠 max(version) 翻页。"""
        body = {
            "deviceType": "1", "sysVersion": "12", "improveState": True,
            "currentManufacturer": "Xiaomi", "dataType": 2, "language": "zh",
            "source": 1, "upDeviceType": "0", "type": WEIGHT_TYPE,
            "isTrackingEnabled": 0, "currentDeviceModel": "2211133C",
            "version": str(version), "deviceId": self._device(),
            "rom": "V417IR release-keys", "appId": APP_PKG,
            "isManually": 1, "token": self.access_token, "tokenType": 2,
            "oaId": "", "ts": str(_now_ms()),
        }
        status, res = _http("POST", DATA_BASE + P_BY_VERSION, body=body,
                            headers={"x-huid": self.uid, "x-ts": str(_now_ms())})
        code = res.get("resultCode")
        if code != 0:
            msg = json.dumps(res, ensure_ascii=False)[:200]
            if status in (401, 403) or code in (20010004, 20010005, 20010006):
                raise HwAuthError(f"健康数据被拒（HTTP {status} code={code}）：{msg}")
            raise HwError(f"体重接口报错（HTTP {status} code={code}）：{msg}")
        items = res.get("detailInfos")
        return items if isinstance(items, list) else []

    def weights(self, days: int = 0) -> list[dict]:
        """拉体重原始条目。`days=0` 表示全量，不做时间截断。"""
        if not self.open_session():
            raise HwAuthError("没有可用会话，且续期失败 → run.bat hw import --json-file …")
        cursor, out, seen = 0, [], set()
        for _ in range(MAX_PAGES):
            batch = self._query_page(cursor)
            if not batch:
                break
            fresh = 0
            for it in batch:
                rid = it.get("recordId") or f"{it.get('startTime')}|{it.get('version')}"
                if rid in seen:
                    continue
                seen.add(rid)
                out.append(it)
                fresh += 1
            nxt = max((int(it.get("version") or 0) for it in batch), default=0)
            if nxt <= cursor or fresh == 0:
                break
            cursor = nxt
        if days and days > 0:
            cutoff = _now_ms() - int(days * 86400 * 1000)
            out = [it for it in out if int(it.get("startTime") or 0) >= cutoff]
        return out

    def sync_version(self) -> int:
        """服务端当前版本号。用于「没有新数据就不必拉」的快速判断。"""
        body = {
            "deviceType": "1", "sysVersion": "12", "improveState": True,
            "currentManufacturer": "Xiaomi", "language": "zh",
            "syncKeys": [{"dataType": 2, "type": WEIGHT_TYPE}],
            "source": 1, "upDeviceType": "0", "isTrackingEnabled": 0,
            "currentDeviceModel": "2211133C", "deviceId": self._device(),
            "rom": "V417IR release-keys", "appId": APP_PKG,
            "isManually": 1, "token": self.access_token, "tokenType": 2,
            "oaId": "", "ts": str(_now_ms()),
        }
        status, res = _http("POST", DATA_BASE + P_VERSIONS, body=body,
                            headers={"x-huid": self.uid, "x-ts": str(_now_ms())})
        for v in res.get("versions") or []:
            if int(v.get("type") or 0) == WEIGHT_TYPE:
                return int(v.get("version") or 0)
        return 0

    # ---------- 体检 ----------
    def probe(self) -> dict:
        """逐项报告三道闸门。只报状态，不写任何票据或健康数值到报告里。"""
        rep: dict = {"gates": {}, "next": ""}
        if not self.has_auth():
            rep["gates"]["account"] = {"ok": False, "detail": "没有凭据"}
            rep["gates"]["renew"] = {"ok": False, "detail": "没有 refresh_token"}
            rep["gates"]["weight"] = {"ok": False, "detail": "没有会话，无法读取"}
            rep["next"] = "hw import --json-file hw_session.json"
            rep["all_ok"] = False
            return rep

        # C 先测：A 的「过期」状态要靠它救回来
        renew = {"ok": False, "detail": "没有 refresh_token"}
        if self.refresh_token:
            try:
                self.refresh()
                renew = {"ok": True, "detail": "refresh_token 续期成功"}
                self.save(note="probe-refresh")
            except HwError as e:
                renew = {"ok": False, "detail": str(e)}
        rep["gates"]["renew"] = renew

        try:
            me = self.whoami()
            rep["gates"]["account"] = {
                "ok": True,
                "detail": f"uid={me.get('uid')} 昵称={me.get('nickName') or '—'}"}
        except HwError as e:
            rep["gates"]["account"] = {"ok": False, "detail": str(e)}

        if rep["gates"]["account"].get("ok"):
            try:
                items = self.weights(days=0)
                recs = to_records(items)
                rep["gates"]["weight"] = {
                    "ok": bool(recs),
                    "detail": f"原始 {len(items)} 条，可用 {len(recs)} 条"
                              + (f"，最近 {recs[-1]['date']} {recs[-1]['weight_kg']}kg"
                                 if recs else ""),
                }
            except HwError as e:
                rep["gates"]["weight"] = {"ok": False, "detail": str(e)}
        else:
            rep["gates"]["weight"] = {"ok": False, "detail": "账号会话不可用，先解决 A"}

        rep["all_ok"] = all(g.get("ok") for g in rep["gates"].values())
        if not rep["all_ok"]:
            rep["next"] = "hw probe" if rep["gates"]["account"].get("ok") else \
                          "hw import --json-file hw_session.json"
        return rep


# ---------------------------------------------------------------- 转成 body 行
def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_records(items: list[dict]) -> list[dict]:
    """华为原始条目 → body 表要的扁平行。

    体重藏在 `samplePoints[].value` 的 JSON 字符串里，key 固定是
    WEIGHT_BODYFAT_BROAD；`bodyWeight` 单位就是公斤，不需要换算。
    """
    out: dict[str, dict] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        ts_ms = _num(it.get("startTime"))
        if not ts_ms:
            continue
        for sp in it.get("samplePoints") or []:
            if sp.get("key") != WEIGHT_KEY:
                continue
            try:
                v = json.loads(sp.get("value") or "{}")
            except Exception:
                continue
            kg = _num(v.get("bodyWeight"))
            if not kg or not 10 <= kg <= 400:
                continue
            local = dt.datetime.fromtimestamp(ts_ms / 1000)
            fat = _num(v.get("bodyFatRate"))
            muscle = _num(v.get("muscleMass"))
            water = _num(v.get("moistureRate"))
            visceral = _num(v.get("visceralFatLevel"))
            bmr = _num(v.get("basalMetabolism"))
            ts = local.strftime("%Y-%m-%d %H:%M:%S")
            out[ts] = {
                "date": local.date().isoformat(),
                "ts": ts,
                "weight_kg": round(kg, 2),
                "body_fat_pct": round(fat, 1) if fat and 0 < fat < 100 else None,
                # muscleMass 本来就是 kg；极小概率给百分比，那种情况按体重折算
                "muscle_kg": (round(muscle, 2) if muscle and muscle < kg
                              else (round(kg * muscle / 100, 2)
                                    if muscle and 0 < muscle < 100 else None)),
                "water_pct": round(water, 1) if water and 0 < water < 100 else None,
                "visceral": int(visceral) if visceral else None,
                "bmr": int(bmr) if bmr else None,
                "raw": json.dumps(v, ensure_ascii=False)[:600],
            }
    return sorted(out.values(), key=lambda r: r["ts"])


# ---------------------------------------------------------------- 对外动作
def sync(store, home: Path, days: int = 0) -> tuple[int, list[dict]]:
    """续期 → 拉体重 → 入库。与 `mi.sync` 同签名。"""
    from . import body as B

    hw = HuaweiCloud(home)
    if not hw.load():
        raise HwAuthError("还没导入华为会话 → run.bat hw import --json-file hw_session.json")
    if not hw.open_session():
        raise HwAuthError("会话已失效且续期失败 → 重新授权一次")
    recs = to_records(hw.weights(days))
    n = B.save(store, recs, "huawei") if recs else 0
    return n, recs


def verify(home: Path, days: int = 0) -> dict:
    """真实拉一次数据并转正。转正记录只写「条数 / 时间范围」，不写数值。"""
    hw = HuaweiCloud(home)
    if not hw.load():
        raise HwAuthError("还没有会话 → run.bat hw import --json-file hw_session.json")
    items = hw.weights(days)
    recs = to_records(items)
    if not recs:
        raise HwError("接口通但没解析出体重：可能这个账号没称过，或数据类型号变了")
    _mark_verified(home, len(recs))
    return {"count": len(recs), "first": recs[0]["date"], "last": recs[-1]["date"]}


def _mark_verified(home: Path, count: int) -> None:
    p = Path(home) / "data" / PROTO_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "schema": 2,
        "verified": True,
        "verifiedAt": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "sampleCount": count,
        "hosts": {"session": SESSION_BASE, "data": DATA_BASE, "common": COMMON_BASE},
        "weight": {"path": P_BY_VERSION, "type": WEIGHT_TYPE, "key": WEIGHT_KEY},
    }, ensure_ascii=False, indent=1), encoding="utf-8")


def protocol_verified(home: Path) -> bool:
    p = Path(home) / "data" / PROTO_NAME
    if not p.exists():
        return False
    try:
        return bool(json.loads(p.read_text(encoding="utf-8")).get("verified"))
    except Exception:
        return False


def status(home: Path) -> dict:
    hw = HuaweiCloud(home)
    logged = hw.load()
    return {
        "logged_in": logged,
        "uid": hw.uid,
        "has_refresh": bool(hw.refresh_token),
        "expires_at": hw.expires_at,
        "expired": hw.expired(0),
        "protocol": {"verified": protocol_verified(home),
                     "present": (Path(home) / "data" / PROTO_NAME).exists()},
    }


# ---------------------------------------------------------------- 抓包兜底
def session_from_capture(path: Path) -> dict:
    """从 HAR 里捞会话。

    两种情况都能救：
      1. 抓到过 healthsession 的 obtain 响应 → 完整会话（含 refreshToken）
      2. 只抓到 healthdata 的业务请求 → 能拿到 token 与 uid，但没有续期能力
    """
    try:
        har = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        raise HwError(f"读不了 HAR：{e}") from None
    entries = ((har.get("log") or {}).get("entries") or [])
    if not entries:
        raise HwError("这个 HAR 里没有请求记录")

    fallback: dict = {}
    for e in entries:
        req = e.get("request") or {}
        resp = e.get("response") or {}
        url = str(req.get("url") or "")
        text = ((resp.get("content") or {}).get("text") or "")
        if "healthsession" in url and P_OBTAIN in url:
            try:
                d = json.loads(text)
            except Exception:
                continue
            if d.get("accessToken"):
                sid = ""
                for h in req.get("headers") or []:
                    if str(h.get("name", "")).lower() == "x-huid":
                        sid = str(h.get("value") or "")
                d.setdefault("uid", sid)
                return d
        if "healthdata" in url and not fallback:
            try:
                body = (req.get("postData") or {}).get("text") or ""
                bid = json.loads(body) if body else {}
            except Exception:
                bid = {}
            uid = ""
            for h in req.get("headers") or []:
                if str(h.get("name", "")).lower() == "x-huid":
                    uid = str(h.get("value") or "")
            if bid.get("token") and uid:
                fallback = {"accessToken": bid["token"], "uid": uid,
                            "deviceId": bid.get("deviceId") or ""}
    if fallback:
        return fallback
    raise HwError("这个 HAR 里既没有 obtain 响应，也没有带 token 的健康数据请求。\n"
                  "  确认抓包时登录过 App，并进过体重页刷新")


# ---------------------------------------------------------------- CLI
# `run.bat hw ...` 的实现放在这里而不是 cli.py：入口文件有 2600 行上限
# （`selftest.py` 会拦），而且这些分支全是华为自己的语义，和别的数据源不共用。
GATE_LABELS = {"account": "A 账号登录", "weight": "B 体重读取",
               "renew": "C 会话续期"}


def cli(args, *, home: Path, db: Path) -> int:
    """状态 / 会话导入 / 闸门体检 / 同步。

    与 `mi` 的差别只在验证阶段：登录、读体重、续期分开报，卡在哪说哪。
    协议已验证，所以 verify 之后就能像小米那样随出图同步。
    """
    cloud = HuaweiCloud(home)
    act = (args.action or "status").lower()

    if act == "logout":
        cloud.clear()
        print("√ 已删除 data/huawei_auth.json（云端数据不受影响）")
        return 0

    if act == "import":
        payload = None
        if getattr(args, "capture", None):
            cap = Path(args.capture)
            if not cap.exists():
                print(f"× 文件不存在：{cap}")
                return 2
            try:
                payload = session_from_capture(cap)
            except HwError as e:
                print(f"× {e}")
                return 2
            print(f"√ 从抓包中提取到会话"
                  f"（{'有' if payload.get('refreshToken') else '无'} refresh_token）")
        else:
            raw = args.json_text or ""
            if getattr(args, "json_file", None):
                p = Path(args.json_file)
                if not p.exists():
                    print(f"× 文件不存在：{p}")
                    return 2
                raw = p.read_text(encoding="utf-8-sig").strip()
            if not raw:
                print("× 要给 --json-file / --json-text / --capture 之一")
                print('  会话形如 {"accessToken":"…","refreshToken":"…","uid":"…"}')
                return 2
            try:
                payload = json.loads(raw)
            except Exception:
                print("× 这不是 JSON。要会话响应，不是 Cookie 或网页源码")
                return 2
        try:
            r = cloud.import_payload(payload)
        except HwAuthError as e:
            print(f"× {e}")
            return 2
        exp = dt.datetime.fromtimestamp(r["expiresAt"] / 1000)
        print(f"√ 已保存会话（uid={r['uid'] or '未知'}）")
        print(f"  accessToken 到期 {exp:%Y-%m-%d %H:%M}；"
              + ("有 refresh_token，可自动续期" if r["hasRefresh"]
                 else "无 refresh_token，过期后需重新授权"))
        print("  下一步：run.bat hw probe")
        return 0

    if act == "verify":
        try:
            r = verify(home, days=args.days)
        except HwError as e:
            print(f"× 验证未通过：{e}")
            return 3
        print(f"√ 真实拉取成功：{r['count']} 条体重，{r['first']} → {r['last']}")
        print("  下一步：run.bat body --huawei　（入库并让看板/一图流用上）")
        return 0

    # status / probe 没有会话也要能跑：它们本身就是用来看「缺什么」的。
    cloud.load()
    if act not in ("status", "probe") and not cloud.has_auth():
        print("× 还没有华为会话 → run.bat hw import --capture <HAR>")
        return 2

    if act == "sync":
        from .store import Store

        store = Store(db)
        try:
            n, recs = sync(store, home, days=args.days)
        except HwError as e:
            print(f"× 同步失败：{e}")
            return 3
        finally:
            store.conn.close()
        if not recs:
            print("× 云端没有取到体重：这个账号可能还没称过")
            return 2
        print(f"√ 入库 {n} 条，最近一次 {recs[-1]['ts']}　"
              f"{recs[-1]['weight_kg']:.2f} kg")
        return 0

    if act == "probe":
        try:
            rep = cloud.probe()
        except HwError as e:
            print(f"× {e}")
            return 3
        print("华为运动健康 · 闸门体检")
        for key in ("account", "weight", "renew"):
            g = (rep.get("gates") or {}).get(key) or {}
            print(f"  {'√' if g.get('ok') else '×'} {GATE_LABELS[key]}　"
                  f"{g.get('detail') or ''}")
        if rep.get("next"):
            print(f"  下一步：run.bat {rep['next']}")
        return 0 if rep.get("all_ok") else 2

    st = status(home)
    exp = (dt.datetime.fromtimestamp(st["expires_at"])
           if st["expires_at"] else None)
    print("华为运动健康 · 状态")
    print(f"  会话      {'已保存' if st['logged_in'] else '无'}"
          + (f"　uid={st['uid']}" if st["uid"] else "")
          + (f"　到期 {exp:%Y-%m-%d %H:%M}" if exp else "")
          + ("" if st["has_refresh"] else "　(无 refresh_token，不能自动续期)"))
    print("  协议      " + ("已验证（healthdata.dbankcloud.cn · type=10006）"
                            if st["protocol"]["verified"] else "未验证"))
    if not st["logged_in"]:
        print("  下一步    run.bat hw import --capture <HAR>")
    elif not st["protocol"]["verified"]:
        print("  下一步    run.bat hw verify")
    else:
        print("  下一步    run.bat body --huawei")
    return 0
