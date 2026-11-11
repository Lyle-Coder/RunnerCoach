# -*- coding: utf-8 -*-
"""Keep 数据源：账号密码登录 → Bearer token → 运动记录摘要。

Keep 没有面向个人的开放接口（开放平台要企业实名 + 审核），这里走的是 App
的私有 API `api.gotokeep.com`，和高驰的 Training Hub 是同一类做法：
登录一次拿 token，之后带着 `Authorization: Bearer ...` 取数据。

凭据与续期
----------
`data/keep_auth.json` 里的手机号/密码/token 整块用 `secretstore` 加密
（Windows 是 DPAPI，绑定当前用户，换机器解不开）。没有系统加密的平台上
只存 token、不落密码，过期就要求重新登录；安卓侧密码由 Kotlin Keystore
保管，调用时传进来，不经过这个文件。

token 是短效的，接口一旦回 401/403 就自动用存着的账密重登一次并重试，
调用方只会看到 `relogged=True`。

只取摘要，不解轨迹
------------------
Keep 的 `geoPoints` / `heartRates` 是 base64 → AES-CBC → gzip 的多层密文，
密钥来自第三方逆向。写进仓库既不稳妥也没必要：第一版只取运动摘要
（开始时间、距离、时长、平均心率、卡路里），活动图按「无轨迹」降级。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import secretstore

BASE = "https://api.gotokeep.com"
LOGIN_API = "/v1.1/users/login"
STATS_API = "/pd/v3/stats/detail"
LOG_API = "/pd/v3/{kind}log/{run_id}"

UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36 Keep/8.4.0")

# Keep 的 token 没有公开有效期，保守按 6 小时算；到期前主动重登，
# 避免每次同步都先撞一次 401。
TOKEN_TTL = 6 * 3600
PAGE_SLEEP = 0.6
MAX_PAGES = 40

TYPE_MAP = {
    "outdoorrunning": "running",
    "indoorrunning": "treadmill_running",
    "outdoorcycling": "cycling",
    "indoorcycling": "indoor_cycling",
    "outdoorwalking": "walking",
    "indoorwalking": "walking",
    "mountaineering": "hiking",
}
TYPE_CN = {
    "running": "跑步",
    "treadmill_running": "跑步机",
    "cycling": "骑行",
    "indoor_cycling": "室内骑行",
    "walking": "健走",
    "hiking": "徒步",
    "other": "运动",
}


class KeepError(Exception):
    pass


class KeepAuthError(KeepError):
    pass


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _secs(v) -> float | None:
    """Keep 的时间戳有毫秒、厘秒两种写法，按数量级归一化到秒。"""
    t = _num(v)
    if t is None:
        return None
    if t > 1e12:
        t /= 1000
    elif t > 1e10:
        t /= 100
    if t < 1e8:
        return None
    return t


class KeepCloud:
    """一个 Keep 账号的会话；用法与 `CorosCloud` 保持一致。"""

    def __init__(self, home: Path | str):
        self.home = Path(home)
        self.auth_path = self.home / "data" / "keep_auth.json"
        self.mobile = ""
        self.password = ""
        self.token = ""
        self.token_at = 0.0
        self.device_id = ""
        self.user_id = ""
        self.relogged = False

    # ---------------- 凭据 ----------------
    def load(self) -> bool:
        try:
            d = json.loads(self.auth_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if isinstance(d.get("enc"), str):
            try:
                d = json.loads(secretstore.unprotect(d["enc"]))
            except Exception:
                return False
        self.mobile = str(d.get("mobile") or "")
        self.password = str(d.get("password") or "")
        self.token = str(d.get("token") or "")
        self.token_at = _num(d.get("token_at")) or 0.0
        self.device_id = str(d.get("device_id") or "")
        self.user_id = str(d.get("user_id") or "")
        return bool(self.token or (self.mobile and self.password))

    def save(self) -> None:
        self.auth_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "mobile": self.mobile,
            "password": self.password,
            "token": self.token,
            "token_at": self.token_at,
            "device_id": self.device_id,
            "user_id": self.user_id,
        }
        if secretstore.available() and (self.mobile or self.password):
            body = {"v": 1, "enc": secretstore.protect(
                json.dumps(payload, ensure_ascii=False))}
        else:
            # 没有系统加密就不落密码：只留 token，过期让用户重登
            payload.pop("password", None)
            body = {"v": 1, **payload}
        self.auth_path.write_text(json.dumps(body, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        try:
            self.auth_path.chmod(0o600)
        except OSError:
            pass

    def clear(self) -> None:
        try:
            self.auth_path.unlink()
        except OSError:
            pass

    # ---------------- HTTP ----------------
    def _headers(self) -> dict:
        h = {"Accept": "application/json", "User-Agent": UA}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _req(self, method: str, path: str, *, params: dict | None = None,
             form: dict | None = None, _retry: bool = True) -> dict:
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        headers = self._headers()
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded;charset=utf-8"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                text = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and _retry:
                self._relogin()
                return self._req(method, path, params=params, form=form, _retry=False)
            snippet = e.read().decode("utf-8", "replace")[:160]
            raise KeepError(f"Keep 接口 HTTP {e.code}：{snippet}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise KeepError(f"连不上 Keep 服务器：{type(e).__name__}: {e}") from None
        try:
            out = json.loads(text)
        except json.JSONDecodeError:
            raise KeepError(f"Keep 返回的不是 JSON（可能改版）：{text[:160]}") from None
        if isinstance(out, dict) and out.get("ok") is False:
            if _retry and str(out.get("errorCode")) in ("401", "403", "1001"):
                self._relogin()
                return self._req(method, path, params=params, form=form, _retry=False)
            msg = out.get("message") or out.get("errorCode") or "未知错误"
            raise KeepError(f"Keep 返回错误：{msg}")
        return out

    # ---------------- 登录 ----------------
    def login(self, mobile: str, password: str) -> dict:
        mobile, password = (mobile or "").strip(), password or ""
        if not mobile or not password:
            raise KeepAuthError("Keep 账号和密码都要填")
        if not self.device_id:
            self.device_id = uuid.uuid4().hex
        r = self._req("POST", LOGIN_API, form={
            "mobile": mobile,
            "password": password,
            "deviceId": self.device_id,
            "platform": "web",
        }, _retry=False)
        data = r.get("data") or {}
        token = data.get("token") or data.get("accessToken") or ""
        if not token:
            raise KeepAuthError("Keep 登录没有返回 token，账号或密码可能不对")
        self.mobile = mobile
        self.password = password
        self.token = token
        self.token_at = time.time()
        user = data.get("user") or {}
        self.user_id = str(data.get("userId") or user.get("_id") or "")
        self.save()
        return {"user_id": self.user_id, "token": token}

    def _relogin(self) -> None:
        if not (self.mobile and self.password):
            raise KeepAuthError("Keep 登录已过期，请重新登录")
        self.login(self.mobile, self.password)
        self.relogged = True

    def ensure_token(self) -> None:
        if self.token and time.time() - self.token_at < TOKEN_TTL:
            return
        if self.mobile and self.password:
            self._relogin()
            return
        if self.token:
            return
        raise KeepAuthError("还没登录 Keep 账号")

    # ---------------- 数据 ----------------
    def _detail(self, kind: str, stats_id: str) -> dict:
        run_id = stats_id.split("_")[1] if "_" in stats_id else stats_id
        r = self._req("GET", LOG_API.format(kind=kind, run_id=run_id))
        return r.get("data") or {}

    def activities(self, days: int = 30) -> list[dict]:
        """最近 `days` 天的运动摘要（无轨迹、无分圈）。"""
        self.ensure_token()
        cutoff = time.time() - max(1, int(days)) * 86400
        out: list[dict] = []
        for kind in ("running", "hiking", "cycling"):
            last, pages = 0, 0
            while True:
                try:
                    r = self._req("GET", STATS_API, params={
                        "dateUnit": "all", "type": kind, "lastDate": last})
                except KeepAuthError:
                    raise
                except KeepError:
                    break
                data = r.get("data") or {}
                records = data.get("records") or []
                oldest = None
                for rec in records:
                    for log in rec.get("logs") or []:
                        st = log.get("stats") or {}
                        sid = str(st.get("id") or "")
                        if not sid or st.get("isDoubtful"):
                            continue
                        t = _secs(st.get("startTime"))
                        if t:
                            oldest = t if oldest is None else min(oldest, t)
                            if t < cutoff:
                                continue
                        try:
                            d = self._detail(kind, sid)
                            row = parse(d, sid)
                        except KeepAuthError:
                            raise
                        except KeepError:
                            continue
                        if row:
                            out.append(row)
                last = data.get("lastTimestamp") or 0
                pages += 1
                if not last or pages >= MAX_PAGES:
                    break
                if oldest is not None and oldest < cutoff:
                    break
                time.sleep(PAGE_SLEEP)
        # 同一 id 只留一条（不同运动类型接口偶有交叉）
        seen, uniq = set(), []
        for r in out:
            if r["activity_id"] in seen:
                continue
            seen.add(r["activity_id"])
            uniq.append(r)
        uniq.sort(key=lambda r: r.get("start_ts") or "", reverse=True)
        return uniq


def parse(detail: dict, stats_id: str) -> dict | None:
    """Keep 详情 → activities 行。缺时长/距离的直接丢（室内拉伸、课程记录）。"""
    if not isinstance(detail, dict):
        return None
    start = _secs(detail.get("startTime"))
    dur = _num(detail.get("duration"))
    dist = _num(detail.get("distance"))
    if not start or not dur or not dist or dist < 200:
        return None
    local = time.localtime(start)
    dtype = str(detail.get("dataType") or "").strip()
    typ = TYPE_MAP.get(dtype.lower(), "other")
    hr = detail.get("heartRate") or {}
    avg_hr = _num(hr.get("averageHeartRate"))
    if avg_hr is not None and avg_hr < 0:
        avg_hr = None
    run_id = stats_id.split("_")[1] if "_" in stats_id else stats_id
    return {
        "activity_id": f"keep_{run_id}",
        "date": time.strftime("%Y-%m-%d", local),
        "start_ts": time.strftime("%Y-%m-%d %H:%M:%S", local),
        "type": typ,
        "name": f"Keep {TYPE_CN.get(typ, '运动')}",
        "duration_s": dur,
        "distance_m": dist,
        "avg_hr": avg_hr,
        "max_hr": None,
        "calories": _num(detail.get("calorie")),
        "load": None,
        "aerobic_te": None,
        "anaerobic_te": None,
        "pace_s_km": (dur / (dist / 1000.0)) if dist else None,
        "ascent": None,
        "raw": json.dumps(detail, ensure_ascii=False)[:20000],
        "source": "keep",
    }


def _prompt(text: str, secret: bool = False) -> str:
    if secret:
        import getpass

        try:
            return getpass.getpass(text)
        except Exception:
            pass
    return input(text).strip()


def cli(args, home: Path | str, db) -> int:
    """命令行入口。主体放在这里，`cli.py` 只留薄包装（它有行数上限）。"""
    act = (getattr(args, "action", None) or "status").lower()
    cloud = KeepCloud(home)

    if act == "logout":
        cloud.clear()
        print("√ 已删除 data/keep_auth.json")
        return 0

    if act == "login":
        mobile = getattr(args, "user", None) or _prompt("Keep 手机号: ")
        pwd = getattr(args, "password", None) or _prompt(
            "密码（输入时不显示）: ", secret=True)
        print("→ 登录 Keep…", flush=True)
        try:
            r = cloud.login(mobile, pwd)
        except KeepError as e:
            print(f"× {e}")
            return 2
        print(f"√ 登录成功（userId={r['user_id']}），"
              f"凭据已加密存 data/keep_auth.json")
        return 0

    if not cloud.load():
        print("× 还没登录 Keep 账号 → run.bat keep login")
        return 2
    print(f"√ 已登录（{cloud.mobile or '凭据已保存'}）")

    if act == "sync":
        from .store import Store

        store = Store(db)
        try:
            r = sync(store, home, days=getattr(args, "days", 30) or 30,
                     mobile=getattr(args, "user", "") or "",
                     password=getattr(args, "password", "") or "")
        except KeepError as e:
            print(f"× 同步失败：{e}")
            return 2
        finally:
            store.conn.close()
        tail = "（已静默重登）" if r.get("relogged") else ""
        print(f"√ 最近 {r['days']} 天：运动 {r['activities']} 条{tail}")
    return 0


def register(sub, home: Path | str, db) -> None:
    """把 `keep` 子命令挂到参数解析器上（cli.py 只留一行调用）。"""
    q = sub.add_parser("keep", help="Keep：登录 / 状态 / 同步（运动记录摘要）")
    q.add_argument("action", nargs="?", default="status",
                   choices=["login", "status", "sync", "logout"])
    q.add_argument("--user", help="Keep 手机号（sync 时带上可自动重登）")
    q.add_argument("--password", help="密码（不推荐写在命令行历史里）")
    q.add_argument("--days", type=int, default=30, help="同步回溯天数，默认 30")
    q.set_defaults(func=lambda a: cli(a, home, db))


def sync(store, home: Path | str, days: int = 30, mobile: str = "",
         password: str = "") -> dict:
    """拉取并写入 activities。返回统计信息，失败抛 KeepError。"""
    cloud = KeepCloud(home)
    cloud.load()
    if mobile and password:
        # 安卓每次同步都会把 Keystore 里的账密传进来：token 还新鲜就直接复用，
        # 不要每次都撞登录（频繁登录最容易触发风控）；401 时再自动重登。
        if not (cloud.token and time.time() - cloud.token_at < TOKEN_TTL):
            cloud.login(mobile, password)
        else:
            cloud.mobile, cloud.password = mobile.strip(), password
    rows = cloud.activities(days)
    for r in rows:
        store.upsert_activity(r)
    store.set_meta("last_sync_keep", time.strftime("%Y-%m-%d %H:%M"))
    return {"activities": len(rows), "days": days,
            "relogged": bool(cloud.relogged),
            "first": rows[-1]["date"] if rows else "",
            "last": rows[0]["date"] if rows else ""}
