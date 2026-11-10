# -*- coding: utf-8 -*-
"""高驰官方 MCP 客户端：只做 REST 拿不到的那部分 —— **睡眠**。

为什么单开一个模块
------------------
Training Hub 的 web REST（`coros.py`）没有睡眠：`/analyse/dayDetail/query` 只给
HRV、静息心率、负荷、VO2max。睡眠只有两条路：

  ① 移动端私有 API（apieu.coros.com）—— 数据全，但登录会把用户手机上的高驰
     App 挤下线，token 约 1 小时一换，官方随时可能改协议；
  ② **官方 MCP 网关（mcp.coros.com）** —— OAuth 2.0 授权，access 30 天 /
     refresh 180 天，不踢手机 App，稳定得多。

本模块走 ②。它讲的是 MCP（Streamable HTTP + JSON-RPC），不是 REST，所以这里
自带一个够用的 MCP 客户端，而不是去改 `coros.py` 的请求层。

授权怎么走
----------
标准 OAuth「授权码 + PKCE（S256）」，client 用动态注册（RFC 7591）现取，
公共客户端不需要密钥。回调固定 `http://127.0.0.1:53682/callback`：

  · 桌面：`login_local()` 起一个临时 HTTP 服务接住 code；
  · App：WebView 加载授权页，拦到这个地址就直接把 code 交给 `finish()`，
    不真的起服务。

设备码授权（device_code）服务端虽然声明支持，但对动态注册的公共客户端一律
401（实测），所以这里不用它。

区域
----
国内 `mcpcn.coros.com` / 欧洲 `mcpeu.coros.com` / 美国 `mcpus.coros.com`。
token 只在注册它的那个区有效，client_id 也是按区注册的。

数据口径
--------
`querySleepOverview` 返回的是「醒来那天」的睡眠：昨天的睡眠记在今天。
字段为评分、主睡眠时长、深/浅/REM 比例、清醒时长、睡眠窗口、小睡总时长；
把它折算成 store.daily 的 sleep_score / sleep_total_min / deep_min / light_min /
rem_min / awake_min / nap_min / sleep_start / sleep_end。
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CALLBACK = "http://127.0.0.1:53682/callback"
SCOPE = "openid mcp.tools offline_access"
SCOPE_FALLBACK = "mcp.tools offline_access"      # 有的区不开 openid
CLIENT_NAME = "RunnerCoach"
PROTOCOL_VERSION = "2025-06-18"

HOSTS = {
    "asia": "https://mcpcn.coros.com",
    "cn": "https://mcpcn.coros.com",
    "eu": "https://mcpeu.coros.com",
    "us": "https://mcpus.coros.com",
}
REGION_CN = {"asia": "国内", "cn": "国内", "eu": "欧洲", "us": "美国"}

# 睡眠工具：官方文档里叫 querySleepOverview，返回一整段可读文本（也可能直接
# 给结构化 JSON）。HRV 另有一个 querySleepHrv，这里只用主睡眠那一个。
SLEEP_TOOL = "querySleepOverview"
HRV_TOOL = "querySleepHrv"


class McpError(Exception):
    """接口/授权层面的失败。消息要能直接给用户看。"""


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _verifier() -> str:
    return _b64u(secrets.token_bytes(48))          # 64 字符，RFC 7636 合法区间


def _challenge(verifier: str) -> str:
    return _b64u(hashlib.sha256(verifier.encode()).digest())


def _norm_region(region: str) -> str:
    r = str(region or "").strip().lower()
    return r if r in HOSTS else "asia"


def _http(url: str, *, data: bytes | None = None, headers: dict | None = None,
          method: str | None = None, timeout: int = 30) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise McpError(f"连不上高驰授权服务：{type(e).__name__}: {e}") from None


def _json(raw: bytes, what: str) -> dict:
    text = raw.decode("utf-8", "replace")
    try:
        out = json.loads(text)
    except json.JSONDecodeError:
        raise McpError(f"{what}返回的不是 JSON：{text[:160]}") from None
    return out if isinstance(out, dict) else {"data": out}


def _form(payload: dict) -> tuple[bytes, dict]:
    return (urllib.parse.urlencode(payload).encode(),
            {"Content-Type": "application/x-www-form-urlencoded",
             "Accept": "application/json"})


class CorosMcp:
    """一个区的授权 + 一个 MCP 会话。线程内顺序调用即可，不做并发。"""

    def __init__(self, home: Path | str, region: str = "asia"):
        self.home = Path(home)
        self.region = _norm_region(region)
        self.path = self.home / "data" / "coros_mcp.json"
        self.client_id = ""
        self.access_token = ""
        self.refresh_token = ""
        self.expires_at = 0.0
        self.session_id = ""
        self.protocol = PROTOCOL_VERSION
        self._verifier = ""
        self._state = ""
        self.load()

    # ---------------- 凭证 ----------------
    def load(self) -> None:
        try:
            got = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if _norm_region(got.get("region")) != self.region:
            # 换区后旧 token 一定无效，但不能顺手删文件——用户可能只是输错区
            return
        self.client_id = str(got.get("client_id") or "")
        self.access_token = str(got.get("access_token") or "")
        self.refresh_token = str(got.get("refresh_token") or "")
        self.expires_at = float(got.get("expires_at") or 0)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"region": self.region, "client_id": self.client_id,
                   "access_token": self.access_token,
                   "refresh_token": self.refresh_token,
                   "expires_at": self.expires_at,
                   "saved_at": dt.datetime.now().isoformat(timespec="seconds")}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def clear(self) -> None:
        for attr in ("client_id", "access_token", "refresh_token"):
            setattr(self, attr, "")
        self.expires_at = 0.0
        self.session_id = ""
        try:
            self.path.unlink()
        except OSError:
            pass

    @property
    def base(self) -> str:
        return HOSTS[self.region]

    def status(self) -> dict:
        left = max(0, int((self.expires_at - dt.datetime.now().timestamp()) / 60))
        return {"authorized": bool(self.access_token),
                "region": self.region, "region_cn": REGION_CN[self.region],
                "expires_in_min": left if self.access_token else 0,
                "can_refresh": bool(self.refresh_token),
                "client": bool(self.client_id)}

    # ---------------- 注册 / 授权 ----------------
    def register(self, force: bool = False) -> str:
        """动态注册一个公共客户端（无密钥）。client_id 按区缓存，不用每次注册。"""
        if self.client_id and not force:
            return self.client_id
        body = json.dumps({
            "client_name": CLIENT_NAME,
            "redirect_uris": [CALLBACK],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": SCOPE,
        }).encode()
        st, _, raw = _http(self.base + "/connect/register", data=body,
                           headers={"Content-Type": "application/json",
                                    "Accept": "application/json"})
        out = _json(raw, "客户端注册") if raw else {}
        if st not in (200, 201) or not out.get("client_id"):
            raise McpError(f"高驰客户端注册失败（HTTP {st}）："
                           f"{out.get('error_description') or out.get('error') or raw[:120]}")
        self.client_id = str(out["client_id"])
        self.save()
        return self.client_id

    def authorize_url(self) -> str:
        """生成授权页地址。PKCE 的 verifier 存在实例上，交给 finish() 用。"""
        cid = self.register()
        self._verifier = _verifier()
        self._state = _b64u(secrets.token_bytes(16))
        q = urllib.parse.urlencode({
            "response_type": "code",
            "client_id": cid,
            "redirect_uri": CALLBACK,
            "scope": SCOPE,
            "state": self._state,
            "code_challenge": _challenge(self._verifier),
            "code_challenge_method": "S256",
        })
        return f"{self.base}/oauth2/authorize?{q}"

    def finish(self, callback_url: str) -> dict:
        """从回调 URL（或裸 code）里取授权码并换 token。"""
        code, state, err = _parse_callback(callback_url)
        if err:
            raise McpError(f"授权被拒绝：{err}")
        if not code:
            raise McpError("回调里没有 code，授权没走完")
        if state and self._state and state != self._state:
            raise McpError("回调和本次请求对不上（state 不一致），请重来一次")
        if not self._verifier:
            raise McpError("这次授权没有 PKCE 记录，请在同一个进程里重新发起")
        data, headers = _form({"grant_type": "authorization_code", "code": code,
                               "redirect_uri": CALLBACK, "client_id": self.client_id,
                               "code_verifier": self._verifier})
        st, _, raw = _http(self.base + "/oauth2/token", data=data, headers=headers)
        return self._take_token(st, raw, "换 token")

    def refresh(self) -> dict:
        if not self.refresh_token or not self.client_id:
            raise McpError("没有 refresh token，需要重新授权")
        data, headers = _form({"grant_type": "refresh_token",
                               "refresh_token": self.refresh_token,
                               "client_id": self.client_id})
        st, _, raw = _http(self.base + "/oauth2/token", data=data, headers=headers)
        return self._take_token(st, raw, "刷新 token")

    def _take_token(self, st: int, raw: bytes, what: str) -> dict:
        out = _json(raw, what) if raw else {}
        if st != 200 or not out.get("access_token"):
            msg = out.get("error_description") or out.get("error") or raw[:120]
            raise McpError(f"高驰{what}失败（HTTP {st}）：{msg}")
        self.access_token = str(out["access_token"])
        if out.get("refresh_token"):
            self.refresh_token = str(out["refresh_token"])
        ttl = out.get("expires_in")
        self.expires_at = (dt.datetime.now().timestamp() + float(ttl)) if ttl else 0.0
        self.save()
        return {"token_expires_in": ttl, "scope": out.get("scope")}

    def token(self) -> str:
        """取一个可用 token。快过期（2 分钟内）就先续，省得请求打到 401 再退。"""
        if self.access_token and self.expires_at and \
                self.expires_at - dt.datetime.now().timestamp() > 120:
            return self.access_token
        if self.access_token and not self.expires_at:
            return self.access_token
        if self.refresh_token:
            self.refresh()
            return self.access_token
        raise McpError("还没有授权过：请先在高驰授权页登录一次")

    # ---------------- MCP 传输 ----------------
    def _rpc(self, payload: dict, *, notify: bool = False) -> dict | None:
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "Authorization": f"Bearer {self.token()}",
                   "MCP-Protocol-Version": self.protocol}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        st, hd, raw = _http(self.base + "/mcp", data=json.dumps(payload).encode(),
                            headers=headers, timeout=40)
        sid = hd.get("Mcp-Session-Id") or hd.get("mcp-session-id")
        if sid:
            self.session_id = sid
        if st == 401:
            # token 被提前作废：续一次再重试，避免把用户丢回授权页
            self.refresh()
            headers["Authorization"] = f"Bearer {self.access_token}"
            st, hd, raw = _http(self.base + "/mcp", data=json.dumps(payload).encode(),
                                headers=headers, timeout=40)
            if hd.get("Mcp-Session-Id"):
                self.session_id = hd["Mcp-Session-Id"]
        if st in (202, 204) or not raw.strip():
            return None
        if st != 200:
            raise McpError(f"高驰 MCP 返回 HTTP {st}：{raw[:160].decode('utf-8', 'replace')}")
        return _read_rpc(raw)

    def initialize(self) -> dict:
        if self.session_id:
            return {"session": self.session_id, "reused": True}
        out = self._rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": self.protocol,
                                    "capabilities": {},
                                    "clientInfo": {"name": CLIENT_NAME,
                                                   "version": "1.0.0"}}}) or {}
        ver = str(((out.get("result") or {}).get("protocolVersion")) or "")
        if ver:
            self.protocol = ver
        self._rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, notify=True)
        return {"session": self.session_id, "protocol": self.protocol}

    def tools(self) -> list[dict]:
        self.initialize()
        out = self._rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                         "params": {}}) or {}
        return list(((out.get("result") or {}).get("tools")) or [])

    def call(self, name: str, arguments: dict | None = None) -> dict:
        self.initialize()
        out = self._rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": name, "arguments": arguments or {}}}) or {}
        if out.get("error"):
            e = out["error"]
            raise McpError(f"高驰 MCP 调用 {name} 失败：{e.get('message') or e}")
        res = out.get("result") or {}
        if res.get("isError"):
            raise McpError(f"高驰 MCP 调用 {name} 报错：{_text_of(res)[:200]}")
        return res

    # ---------------- 业务：睡眠 ----------------
    def sleep(self, days: int = 30) -> list[dict]:
        """近 N 天的睡眠，按日期升序。字段已折算成 store.daily 的口径。"""
        res = self.call(SLEEP_TOOL, {"days": max(1, min(int(days or 7), 30))})
        return parse_sleep(res)

    def hrv(self, days: int = 7) -> dict[str, float]:
        """睡眠 HRV（ms），按日期回填到 daily.hrv_night。拿不到就空着。"""
        try:
            res = self.call(HRV_TOOL, {"days": max(1, min(int(days or 7), 7))})
        except McpError:
            return {}
        return parse_hrv(res)


def _read_rpc(raw: bytes) -> dict:
    """MCP 可能回 application/json，也可能回 SSE（data: {...}）。两种都吃。"""
    text = raw.decode("utf-8", "replace").strip()
    if text.startswith("{") or text.startswith("["):
        return _json(raw, "MCP")
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            got = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(got, dict):
            out = got
    if not out:
        raise McpError(f"MCP 响应解析不了：{text[:160]}")
    return out


def _text_of(result: dict) -> str:
    """把 tools/call 的 content 拼成纯文本（各工具都是文本块）。"""
    bits = []
    for c in result.get("content") or []:
        if isinstance(c, dict):
            if c.get("type") == "text" and c.get("text"):
                bits.append(str(c["text"]))
            elif c.get("type") == "resource" and isinstance(c.get("resource"), dict):
                bits.append(str(c["resource"].get("text") or ""))
    if not bits and result.get("structuredContent") is not None:
        bits.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    return "\n".join(b for b in bits if b)


def _parse_callback(url: str) -> tuple[str, str, str]:
    """接受完整回调 URL、`?code=...` 查询串，或者干脆一个裸 code。"""
    s = str(url or "").strip()
    if not s:
        return "", "", ""
    if "?" in s or s.startswith("http"):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(s).query or s.lstrip("?"))
        return (q.get("code", [""])[0], q.get("state", [""])[0],
                q.get("error_description", q.get("error", [""]))[0])
    return s, "", ""


def _minutes(value) -> int | None:
    """'7h 57min' / '7:57' / '477 min' → 分钟。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(round(float(value)))
    s = str(value).strip().lower()
    h = re.search(r"(\d+(?:\.\d+)?)\s*h", s)
    m = re.search(r"(\d+(?:\.\d+)?)\s*m", s)
    if h or m:
        total = (float(h.group(1)) * 60 if h else 0.0) + (float(m.group(1)) if m else 0.0)
        return int(round(total))
    if ":" in s:
        hh, mm = s.split(":", 1)
        try:
            return int(hh) * 60 + int(mm)
        except ValueError:
            return None
    num = re.search(r"\d+(?:\.\d+)?", s)
    return int(round(float(num.group(0)))) if num else None


def _pct(value) -> float | None:
    """比例统一成百分数：0.25 和 '25%' 都返回 25。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v * 100 if 0 < v <= 1 else v
    num = re.search(r"\d+(?:\.\d+)?", str(value))
    return float(num.group(0)) if num else None


def parse_sleep(result: dict) -> list[dict]:
    """`querySleepOverview` 的结果 → [{date, sleep_score, sleep_total_min, ...}]。

    工具既可能回结构化 JSON，也可能回文档里那种可读文本（"Sleep Score: 91" 之类），
    两种都认；认不出的行直接跳过，不猜。
    """
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        rows = _sleep_from_json(structured)
        if rows:
            return rows
    text = _text_of(result)
    if not text.strip():
        return []
    try:
        as_json = json.loads(text)
    except json.JSONDecodeError:
        as_json = None
    if isinstance(as_json, (dict, list)):
        rows = _sleep_from_json(as_json)
        if rows:
            return rows
    return _sleep_from_text(text)


def _sleep_from_json(data) -> list[dict]:
    """结构化输出常见三种形状：{days:[...]} / {records:[...]} / 直接是列表。"""
    rows = []
    items = data
    if isinstance(data, dict):
        for key in ("days", "records", "sleep", "data", "items"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
        else:
            items = [data]
    if not isinstance(items, list):
        return []
    for it in items:
        if not isinstance(it, dict):
            continue
        day = str(it.get("date") or it.get("day") or it.get("wakeDate") or "").strip()
        day = re.sub(r"[^0-9]", "", day)
        if len(day) != 8:
            continue
        row = _row_from_fields(day, it)
        if row:
            rows.append(row)
    return sorted(rows, key=lambda r: r["date"])


def _row_from_fields(day: str, it: dict) -> dict | None:
    total = _minutes(it.get("total_duration_minutes") or it.get("totalSleep")
                     or it.get("mainSleep") or it.get("sleep_duration")
                     or it.get("totalDuration"))
    stages = it.get("phases") if isinstance(it.get("phases"), dict) else it
    deep_r = _pct(stages.get("deep_ratio") or stages.get("deepRatio"))
    light_r = _pct(stages.get("light_ratio") or stages.get("lightRatio"))
    rem_r = _pct(stages.get("rem_ratio") or stages.get("remRatio"))
    deep = _minutes(stages.get("deep_minutes") or stages.get("deep"))
    light = _minutes(stages.get("light_minutes") or stages.get("light"))
    rem = _minutes(stages.get("rem_minutes") or stages.get("rem"))
    awake = _minutes(stages.get("awake_minutes") or it.get("awake")
                     or it.get("awakeTime"))
    nap = _minutes(it.get("nap_minutes") or it.get("naps") or it.get("nap"))
    if total is None and deep is None and light is None and rem is None:
        return None
    # 只给比例时换算出分钟数：比例 × 主睡眠时长（睡眠分期比例按主睡眠算）
    if total and deep is None and deep_r is not None:
        deep = int(round(total * deep_r / 100))
    if total and light is None and light_r is not None:
        light = int(round(total * light_r / 100))
    if total and rem is None and rem_r is not None:
        rem = int(round(total * rem_r / 100))
    score = it.get("quality_score") or it.get("sleep_score") or it.get("score")
    start = it.get("sleep_start") or it.get("startTime") or it.get("bedtime")
    end = it.get("sleep_end") or it.get("endTime") or it.get("wakeTime")
    row = {"date": f"{day[:4]}-{day[4:6]}-{day[6:]}",
           "sleep_score": int(float(score)) if score not in (None, "") else None,
           "sleep_total_min": total, "deep_min": deep, "light_min": light,
           "rem_min": rem, "awake_min": awake, "nap_min": nap,
           "sleep_start": _iso_dt(start), "sleep_end": _iso_dt(end)}
    return {k: v for k, v in row.items() if v is not None} or None


def _iso_dt(value) -> str | None:
    s = str(value or "").strip()
    if not s:
        return None
    m = re.match(r"(\d{4})[-/]?(\d{2})[-/]?(\d{2})[ T]?(\d{2}):(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}T{m.group(4)}:{m.group(5)}:00"
    return None


_DATE_LINE = re.compile(r"^\s*(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s*$")


def _sleep_from_text(text: str) -> list[dict]:
    """解析文档里那种一段一天的文本。行不认识就跳过，绝不整段报错。"""
    rows, cur = [], None
    for line in text.splitlines():
        raw = line.strip()
        m = _DATE_LINE.match(raw)
        if m:
            if cur:
                rows.append(cur)
            cur = {"date": f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"}
            continue
        if not cur or ":" not in raw:
            continue
        key, val = raw.split(":", 1)
        k, v = key.strip().lower(), val.strip()
        if "sleep score" in k:
            cur["sleep_score"] = int(float(v)) if re.search(r"\d", v) else None
        elif "sleep window" in k or k.startswith("main sleep window"):
            # 必须排在「main sleep」前面：窗口那行也以 main sleep 开头
            bits = re.split(r"\s+-\s+|\s+to\s+", v, maxsplit=1)
            if len(bits) == 2:
                cur["sleep_start"], cur["sleep_end"] = _iso_dt(bits[0]), _iso_dt(bits[1])
        elif k.startswith("main sleep"):
            cur["sleep_total_min"] = _minutes(v)
        elif "deep sleep ratio" in k:
            cur["_deep_r"] = _pct(v)
        elif "light sleep ratio" in k:
            cur["_light_r"] = _pct(v)
        elif "rem ratio" in k:
            cur["_rem_r"] = _pct(v)
        elif "awake" in k:
            cur["awake_min"] = _minutes(v)
        elif k.startswith("naps total") or k.startswith("nap"):
            cur["nap_min"] = _minutes(v)
        elif k.startswith("daily sleep") and "sleep_total_min" not in cur:
            cur["sleep_total_min"] = _minutes(v)
    if cur:
        rows.append(cur)
    out = []
    for r in rows:
        total = r.get("sleep_total_min")
        if total:
            for key, src in (("deep_min", "_deep_r"), ("light_min", "_light_r"),
                             ("rem_min", "_rem_r")):
                if r.get(src) is not None:
                    r[key] = int(round(total * r[src] / 100))
        clean = {k: v for k, v in r.items() if not k.startswith("_") and v is not None}
        if len(clean) > 1:
            out.append(clean)
    return sorted(out, key=lambda r: r["date"])


def parse_hrv(result: dict) -> dict[str, float]:
    """`querySleepHrv` 的结果 → {YYYY-MM-DD: ms}。文本里按日期找数字即可。"""
    text = _text_of(result)
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        text = json.dumps(structured, ensure_ascii=False) + "\n" + text
    out: dict[str, float] = {}
    for line in text.splitlines():
        m = re.search(r"(\d{4})[-/]?(\d{2})[-/]?(\d{2})", line)
        if not m:
            continue
        vals = re.findall(r"(\d+(?:\.\d+)?)\s*ms", line, flags=re.I)
        if not vals:
            continue
        try:
            out[f"{m.group(1)}-{m.group(2)}-{m.group(3)}"] = float(vals[0])
        except ValueError:
            continue
    return out


def login_local(home: Path | str, region: str = "asia", timeout: int = 180,
                open_browser: bool = True, say=print) -> dict:
    """桌面端的完整授权：打印/打开授权页，本机接住回调，换 token。

    `say` 让 CLI 和别的调用方共用同一套话术；真正的网络动作都在 CorosMcp 里。
    """
    import time
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import urlsplit

    cloud = CorosMcp(home, region)
    url = cloud.authorize_url()
    got: dict[str, str] = {}

    class _Cb(BaseHTTPRequestHandler):
        def log_message(self, *a):        # 回调请求不用刷到终端
            pass

        def do_GET(self):
            if not urlsplit(self.path).path.startswith("/callback"):
                self.send_response(404)
                self.end_headers()
                return
            got["url"] = self.path
            body = ("<meta charset=utf-8><h3>授权完成</h3>"
                    "<p>可以关掉这个页面，回到终端。</p>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    say("→ 正在打开高驰授权页，登录并点「同意」即可 …")
    say(f"  浏览器没自动打开就把这个地址粘进去：\n  {url}\n")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv = HTTPServer(("127.0.0.1", 53682), _Cb)
    except OSError as e:
        raise McpError(f"本地端口 53682 被占用（{e}），关掉占用它的程序再试") from None
    srv.timeout = 5
    deadline = time.time() + timeout
    try:
        while not got.get("url") and time.time() < deadline:
            srv.handle_request()          # 超时返回 False，循环再看截止时间
    finally:
        srv.server_close()
    if not got.get("url"):
        raise McpError(f"等授权超时（{timeout} 秒），重新执行一次授权")
    cloud.finish(got["url"])
    return {"region": cloud.region, "region_cn": REGION_CN[cloud.region]}


def cli(act: str, home: Path | str, db: Path | str, region: str = "asia",
        days: int = 30, say=print) -> int:
    """`coros mcp-*` 子命令的实现。返回码与 CLI 其余命令一致。"""
    cloud = CorosMcp(home, region)
    if act == "mcp-logout":
        cloud.clear()
        say("√ 已删除高驰 MCP 授权（data/coros_mcp.json）")
        return 0
    if act == "mcp-status":
        st = cloud.status()
        if not st["authorized"]:
            say("× 还没授权高驰睡眠 → run.bat coros mcp-login")
            return 2
        say(f"√ 已授权（{st['region_cn']}，access token 还剩 {st['expires_in_min']} 分钟，"
            f"{'可自动续期' if st['can_refresh'] else '不能续期，到期要重新授权'}）")
        return 0
    if act == "mcp-login":
        r = login_local(home, region, say=say)
        say(f"√ 授权成功（{r['region_cn']}）。下一步：run.bat coros mcp-sleep")
        return 0
    # mcp-sleep
    from .store import Store

    store = Store(Path(db))
    rows = cloud.sleep(days)
    for row in rows:
        store.upsert_daily(dict(row), merge=True)
    hrv = cloud.hrv(min(int(days or 7), 7))
    for day, ms in hrv.items():
        store.upsert_daily({"date": day, "hrv_night": ms}, merge=True)
    if not rows:
        say("× 授权没问题，但这段时间没有睡眠记录")
        return 2
    say(f"√ 睡眠 {len(rows)} 天（{rows[0]['date']} ~ {rows[-1]['date']}），"
        f"睡眠 HRV {len(hrv)} 天已并入日汇总")
    return 0


def sync_sleep(store, home: Path | str, days: int = 30, region: str = "asia") -> dict:
    """拉睡眠写进 daily（merge 模式，不覆盖佳明写过的字段）。"""
    cloud = CorosMcp(home, region)
    rows = cloud.sleep(days)
    n = 0
    for row in rows:
        store.upsert_daily(dict(row), merge=True)
        n += 1
    hrv = cloud.hrv(min(days, 7))
    h = 0
    for day, ms in hrv.items():
        store.upsert_daily({"date": day, "hrv_night": ms}, merge=True)
        h += 1
    return {"sleep_days": n, "hrv_days": h, "first": rows[0]["date"] if rows else None,
            "last": rows[-1]["date"] if rows else None}
