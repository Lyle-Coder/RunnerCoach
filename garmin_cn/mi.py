"""小米健康云（hlth.io.mi.com）取体重 / 体成分。

为什么走云端
------------
ms116（米家体脂秤 S400）没有开放 API，也不像佳明那样有 Personal API。
秤只做两件事：蓝牙广播原始阻抗，App 联网把算好的结果传云端。所以：
  ① 官方数据副本 —— App 里导出文件，最省事但得手动；
  ② 云端逆向     —— 拿账号凭据直接读，能自动跑（本模块）；
  ③ BLE 直读     —— 要电脑离秤够近，且四电极阻抗算法得自己复刻。
本模块做 ②：一次登录，之后每次 `run.bat body --mi` 增量拉。

协议长什么样
------------
小米云有一套自己的「加密 + 双签名」外壳，所有业务参数都在 `data` 里：

    data      = JSON 明文
    rc4_hash__= sign(POST, path, {data}, sn)            先给明文算一次
    data/rc4_hash__ → 各自 RC4(sn) → base64             再整体加密
    signature = sign(POST, path, {data, rc4_hash__}, sn) 给密文算第二次
    _nonce    = base64(nonce)

    sn（signed nonce）= sha256(ssecurity + nonce)
    sign     = base64(sha1("POST&<path>&data=<v>[&rc4_hash__=<v>]&<base64(sn)>"))

RC4 是**丢弃前 1024 字节密钥流**的变体（RC4-drop1024），照普通 RC4 实现会解出乱码。
响应体同样是 base64 + RC4，解完才是 JSON。

凭据
----
`passToken` 是长效票据（半年量级），换 ssecurity 只花一次请求，所以本地只存
`data/mi_auth.json`（userId + passToken，权限 600），不存密码。
密码登录走 serviceLoginAuth2，只在首次 `mi login` 用一次。

单位
----
云端给的是 `body_fat_rate / muscle_rate / moisture_rate` 三个**百分比**，
不是 kg。项目里 `muscle_kg` 要的是重量，所以按 体重 × 肌肉率% 换算——
参考实现直接把 muscle_rate 塞进 muscle_mass_kg，是个常见坑（会得到 26 这种
"26 kg 肌肉"其实是对的，但 40 就是错的了，看机型）。
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

from .tls import https_handler
from pathlib import Path

ACCOUNT = "https://account.xiaomi.com"
SID = "miothealth"
# 国区走裸域名；其它区要加 <region>. 前缀（本项目只做国区，留了口子）
HEALTH_BASE = {"cn": "https://hlth.io.mi.com"}
LOGIN_PREFIX = b"&&&START&&&"
UA = "MiFit/6.5.0 (Android 13; zh_CN)"
TIMEOUT = 20
RETRIES = 3

AUTH_ERROR_MARKERS = ("登录", "login", "token", "credential", "unauthorized",
                      "session", "expired", "auth")

# 二次验证（新设备 / 异地登录时小米要求短信验证码）——2026-11 实测出来的四个接口
IDENTITY_LIST = "/identity/list"
IDENTITY_SEND = "/identity/auth/sendPhoneTicket"
IDENTITY_VERIFY = "/identity/auth/verifyPhone"
CAPTCHA_PATH = "/pass/getCode?icodeType=login"

# 身份验证那几个接口要求 XMLHttpRequest 标记，否则返回前端页面外壳
XHR_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{ACCOUNT}/fe/service/identity/authStart",
}


class MiError(RuntimeError):
    pass


class MiAuthError(MiError):
    """凭据失效 / 没登录 / 被风控拦下。"""


class MiCodeError(MiAuthError):
    """短信码 / 图形码填错——这是唯一值得让用户重输的情况。

    与其它 MiAuthError 区分开：验证码错可以重试，而「验证通过但换不到密钥」
    再问一遍验证码没用（码是一次性的），必须换路径。
    """


class MiNeedVerify(MiAuthError):
    """密码已验证通过，但小米要求先过安全验证（短信验证码）。

    `info` 里带着走完验证所需的材料，由调用方决定怎么和用户交互：
    context / session / flag / apply_token / masked（手机尾号）。
    """

    def __init__(self, msg: str, info: dict):
        super().__init__(msg)
        self.info = info


# ---------------------------------------------------------------- 底层密码学
def rc4_drop1024(key: bytes, data: bytes) -> bytes:
    """小米的 RC4 变体：KSA 之后先空转 1024 字节再开始异或。"""
    s = list(range(256))
    j = 0
    klen = len(key)
    for i in range(256):
        j = (j + s[i] + key[i % klen]) % 256
        s[i], s[j] = s[j], s[i]
    i = 0
    j = 0

    def step() -> int:
        nonlocal i, j
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        return s[(s[i] + s[j]) % 256]

    for _ in range(1024):
        step()
    return bytes(b ^ step() for b in data)


def _nonce() -> bytes:
    """8 字节随机 + 4 字节「当前分钟」，够防重放。"""
    return os.urandom(8) + struct.pack(">I", int(time.time() // 60))


def _signed_nonce(ssecurity: bytes, nonce: bytes) -> bytes:
    return hashlib.sha256(ssecurity + nonce).digest()


def _sign(method: str, path: str, values: dict, sn: bytes) -> str:
    base = method + "&" + path + "&data=" + values["data"]
    if "rc4_hash__" in values:
        base += "&rc4_hash__=" + values["rc4_hash__"]
    base += "&" + base64.b64encode(sn).decode()
    return base64.b64encode(hashlib.sha1(base.encode()).digest()).decode()


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode()


# ---------------------------------------------------------------- HTTP
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """登录跳转要自己看 Set-Cookie，不能让 urllib 自动跟过去。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect, https_handler())


def _http(method: str, url: str, *, body: str | None = None,
          headers: dict | None = None,
          raw: bool = False) -> tuple[int, dict, str | bytes]:
    """返回 (status, headers, text)。3xx 不跟随，当普通响应处理。

    `raw=True` 时第三项是原始 bytes——图形验证码是 JPEG，按 utf-8 解码会烂掉。
    """
    hdrs = {"User-Agent": UA, "Accept-Encoding": "identity", "Accept": "*/*"}
    if body is not None:
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    if headers:
        hdrs.update(headers)

    def _body(data: bytes):
        return data if raw else data.decode("utf-8", "replace")

    last: Exception | None = None
    for attempt in range(RETRIES):
        req = urllib.request.Request(
            url, data=body.encode() if body is not None else None,
            headers=hdrs, method=method)
        try:
            with _OPENER.open(req, timeout=TIMEOUT) as resp:
                # headers 原样返回（HTTPMessage 支持 get_all，能保住多个 Set-Cookie）
                return resp.status, resp.headers, _body(resp.read())
        except urllib.error.HTTPError as e:
            text: str | bytes = b"" if raw else ""
            try:
                text = _body(e.read())
            except Exception:
                pass
            if e.code in (429, 500, 502, 503, 504) and attempt < RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
                last = e
                continue
            return e.code, e.headers, text
        except Exception as e:
            last = e
            if attempt < RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise MiError(f"连不上小米服务器：{type(e).__name__}: {e}") from None
    raise MiError(f"请求失败：{last}")


def _login_payload(text: str) -> dict:
    raw = text.encode()
    if not raw.startswith(LOGIN_PREFIX):
        snippet = text.strip()[:200].replace("\n", " ")
        raise MiAuthError(f"小米账号接口返回异常（可能被风控）：{snippet}")
    return json.loads(raw[len(LOGIN_PREFIX):].decode("utf-8", "replace"))


def _json_of(text: str | bytes) -> dict:
    """身份验证那几个接口：有的带 &&&START&&& 前缀，有的不带，统一吃下。"""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    body = text.strip()
    if body.startswith("&&&START&&&"):
        body = body[len("&&&START&&&"):]
    if not body.startswith("{"):
        snippet = body[:160].replace("\n", " ")
        raise MiAuthError(f"小米返回的不是 JSON（可能改版了）：{snippet}")
    try:
        return json.loads(body)
    except Exception:
        raise MiAuthError(f"小米返回的 JSON 解析不了：{body[:160]}") from None


def _ctx_of(url: str) -> str:
    """从 authStart 链接里取 context——它是整段验证流程的凭据。"""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url or "").query)
    return (q.get("context") or [""])[0]


def _mask_phone(raw: str) -> str:
    """externalId 是账号 ID，不是手机号；能拿到手机号时才打码。"""
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(digits) == 11:
        return f"{digits[:3]}****{digits[-4:]}"
    return "绑定的手机号"


def _verify_error(d: dict) -> str:
    code = d.get("code")
    desc = str(d.get("desc") or d.get("description") or d.get("reason") or "").strip()
    table = {
        87001: "图形验证码不对，换一张再试",
        70016: "验证失败，重新登录一次",
        10001: "提交的验证码不对或已过期",
        87002: "验证码错误次数过多，稍后再试",
    }
    base = table.get(code) or f"验证失败（code={code}）"
    return f"{base}{('：' + desc) if desc and desc not in base else ''}"


def _verify_retryable(d: dict) -> bool:
    """码填错可以重输；其它错（会话过期、已被消费）重输没用。"""
    return str(d.get("code")) in ("87001", "87002", "10001", "-1")


def _header_values(headers, name: str) -> list[str]:
    """取某个头的**全部**值。HTTPMessage 有 get_all，dict 只能退化成拆分。"""
    getter = getattr(headers, "get_all", None)
    if getter:
        return list(getter(name) or [])
    for k, v in headers.items():
        if k.lower() == name.lower():
            return [v] if isinstance(v, str) else list(v)
    return []


def _location(headers) -> str:
    for k, v in headers.items():
        if k.lower() == "location":
            return str(v)
    return ""


def _cookie_dict(headers) -> dict[str, str]:
    """把一组 Set-Cookie 压成 {name: value}，同名后者覆盖。"""
    jar: dict[str, str] = {}
    for line in _header_values(headers, "Set-Cookie"):
        first = line.split(";", 1)[0]
        if "=" in first:
            k, _, v = first.partition("=")
            jar[k.strip()] = v.strip()
    return jar


def _cookie_str(jar: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in jar.items() if v)


def _cookie_parse(text: str) -> dict[str, str]:
    """把一条 Cookie 头拆回 {name: value}。"""
    jar: dict[str, str] = {}
    for part in (text or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            jar[k.strip()] = v.strip()
    return jar


def _bare_ssecurity(text: str) -> str:
    """从任意文本里捞一个 ssecurity（响应头、日志、单行粘贴都能用）。"""
    for pat in (r'"ssecurity"\s*:\s*"([A-Za-z0-9+/=]{16,})"',
                r"\bssecurity\s*[:=]\s*\"?([A-Za-z0-9+/=]{16,})"):
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return ""


class _TextHeaders:
    """把一段「响应头文本」伪装成 headers 对象，复用 _pragma_ssecurity。"""

    def __init__(self, text: str):
        self._text = text

    def items(self):
        for line in self._text.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                yield k.strip(), v.strip()


def _pragma_ssecurity(headers) -> str:
    """从响应头 extension-pragma 里抠 ssecurity。

    小米把 ssecurity 放在 `serviceLoginAuth2/end` 这类跳转响应的
    `extension-pragma: {"ssecurity": "..."}` 头里，**不在 JSON body 里**。
    浏览器跳转时它天然被消费掉，所以只抓 body 的实现永远拿不到。
    """
    for k, v in headers.items():
        if k.lower() != "extension-pragma":
            continue
        raw = str(v).strip()
        for candidate in (raw, urllib.parse.unquote(raw)):
            try:
                d = json.loads(candidate)
            except Exception:
                continue
            if isinstance(d, dict) and d.get("ssecurity"):
                return str(d["ssecurity"])
    return ""


# ---------------------------------------------------------------- 客户端
class MiCloud:
    def __init__(self, home: Path, region: str = "cn"):
        self.home = Path(home)
        self.auth_path = self.home / "data" / "mi_auth.json"
        self.region = region
        self.user_id = ""
        self.pass_token = ""
        self.cookie_raw = ""
        self._ssecurity = b""
        self._cookie = ""
        self._sn = b""
        self.diag = ""
        self.pragma_security = ""
        self.notify_url = ""
        self.captcha_url = ""
        # 安全验证（短信）用到的状态
        self.verify_url = ""
        self._verify: dict = {}
        self._ick = ""
        self._id_jar: dict[str, str] = {}  # 验证链路上的 Cookie（identity_session / deviceId 等）
        # 本次登录用的账号密码只留在内存，短信验证通过后自动重登要用
        self._cred: tuple[str, str] = ("", "")
        self._relogin = False  # 防止验证 → 重登 → 再验证的死循环

    # ---------- 凭据 ----------
    def load(self) -> bool:
        if not self.auth_path.exists():
            return False
        try:
            d = json.loads(self.auth_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        self.user_id = str(d.get("userId") or "")
        self.pass_token = str(d.get("passToken") or "")
        self.cookie_raw = str(d.get("cookie") or "")
        self._cookie = str(d.get("session") or "")
        self.region = d.get("region") or self.region
        # 短信验证那一步拿到的 Cookie 里其实带着 userId / passToken，但 body 里没有，
        # 当初只从 body 取就会存成空——回头从 Cookie 补齐，省得显示「userId=未知」。
        jar = _cookie_parse(self.cookie_raw or self._cookie)
        self.user_id = self.user_id or jar.get("userId", "")
        self.pass_token = self.pass_token or jar.get("passToken", "")
        if d.get("ssecurity"):
            try:
                self._ssecurity = base64.b64decode(d["ssecurity"])
            except Exception:
                self._ssecurity = b""
        return bool(self.cookie_raw or (self.user_id and self.pass_token) or self._ssecurity)

    def save(self, note: str = "") -> None:
        self.auth_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "userId": self.user_id,
            "passToken": self.pass_token,
            "region": self.region,
            "note": note,
            "saved_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        if self.cookie_raw:
            data["cookie"] = self.cookie_raw
        if self._cookie:
            data["session"] = self._cookie
        if self._ssecurity:
            data["ssecurity"] = base64.b64encode(self._ssecurity).decode()
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

    @property
    def base(self) -> str:
        return HEALTH_BASE.get(self.region, f"https://{self.region}.hlth.io.mi.com")

    def cookie_header(self) -> str:
        """送给 account.xiaomi.com 的 Cookie。整串优先，其次拼 userId+passToken。"""
        if self.cookie_raw:
            return self.cookie_raw
        parts = []
        if self.user_id:
            parts.append(f"userId={self.user_id}")
        if self.pass_token:
            parts.append(f"passToken={self.pass_token}")
        return "; ".join(parts)

    def import_cookie(self, header: str) -> None:
        """照收浏览器里那一整串 Cookie。

        新版登录不一定再下发 passToken（只有 serviceToken / userId / cUserId），
        所以不能只认 passToken——只要有一条能当会话用的就收，让 serviceLogin 去判。
        """
        header = " ".join(str(header).split())
        jar = {}
        for part in header.split(";"):
            part = part.strip()
            if "=" in part:
                k, _, v = part.partition("=")
                jar[k.strip()] = v.strip()
        if not jar:
            raise MiAuthError("这行不是 Cookie：要有 name=value; name=value 的格式")
        names = set(jar)
        if not names & {"userId", "cUserId", "serviceToken", "passToken"}:
            raise MiAuthError(
                f"这串 Cookie 里没有小米的会话（只有 {', '.join(sorted(names))[:120]}）。"
                "要的是 account.xiaomi.com 域下的，不是 i.mi.com 页面的其它 cookie")
        self.cookie_raw = "; ".join(f"{k}={v}" for k, v in jar.items())
        self.user_id = jar.get("userId") or jar.get("cUserId") or ""
        self.pass_token = jar.get("passToken") or ""
        self.save(note="cookie")

    # ---------- 登录 ----------
    def login(self, user: str, password: str, wait: int = 0, on_wait=None,
              seed: str = "") -> dict:
        """serviceLoginAuth2：账号 + MD5(密码) 换会话。

        `wait>0` 时，若小米要求二次确认（手机弹窗），会在这段时间里轮询
        serviceLogin，等用户在手机上点「允许」后自动接上，不用重跑。

        `seed` 是随请求一起带的 Cookie。短信验证通过后要用同一账号再提交一次
        auth2（此时 `securityStatus` 已不再拦），靠它把 `identity_session`
        一并带上，否则小米会当成一台全新设备重新要求验证。
        """
        # 只在内存里留一份，短信验证通过后自动重登要用；不落盘
        self._cred = (user, password)
        if not seed:
            self.cookie_raw = ""  # 密码登录会重建会话，别把之前导入的 cookie 混进来
        extra = {"Cookie": seed} if seed else {}
        _, _, text = _http(
            "GET", f"{ACCOUNT}/pass/serviceLogin?_json=true&sid={SID}&_locale=zh_CN",
            headers=extra)
        info = _login_payload(text)
        sign = info.get("_sign")
        if not sign:
            raise MiAuthError(f"没拿到登录签名：{json.dumps(info, ensure_ascii=False)[:200]}")

        form = {
            "sid": SID,
            "user": user,
            "hash": hashlib.md5(password.encode()).hexdigest().upper(),
            "callback": info.get("callback", ""),
            "qs": info.get("qs", ""),
            "_sign": sign,
            "_json": "true",
            "_locale": "zh_CN",
        }
        _, hd, text = _http(
            "POST", f"{ACCOUNT}/pass/serviceLoginAuth2?_json=true",
            body=urllib.parse.urlencode(form),
            headers={"Referer": f"{ACCOUNT}/pass/serviceLogin", **extra})
        res = _login_payload(text)
        code = res.get("code", 0)
        self.diag = _diag(res)
        self.notify_url = res.get("notificationUrl") or ""
        self.captcha_url = res.get("captchaUrl") or ""
        if code != 0:
            raise MiAuthError(_login_error(code, res))

        # passToken 常常只出现在 Set-Cookie 里，body 里没有；两边都收。
        jar = _cookie_dict(hd)
        for k in ("userId", "passToken"):
            if res.get(k):
                jar[k] = str(res[k])
        self.user_id = jar.get("userId") or ""
        self.pass_token = jar.get("passToken") or ""
        seed = _cookie_str(jar)
        loc = res.get("location") or ""
        ssec = res.get("ssecurity") or _pragma_ssecurity(hd) or ""

        if loc:
            sess = self._follow(loc, seed=seed)
            ssec = ssec or self.pragma_security
            if ssec:
                self._ssecurity = base64.b64decode(ssec)
                self._cookie = sess
                self.save(note="password")
                return {"userId": self.user_id, "region": self.region}
            raise MiAuthError(
                "登录请求成功了，但跳转链里没带 ssecurity（extension-pragma 缺失）。\n"
                "   改用浏览器路线：F12 → Network → 过滤 serviceLoginAuth2 → 最后一条\n"
                "   → Response Headers 整段复制存成 mi_login.txt → "
                "run.bat mi login --json-file mi_login.txt")

        # 没跳转 = 二次验证。带 passToken 的话先直接试一次换票。
        if seed and self.pass_token:
            self.cookie_raw = seed
            if self.open_session():
                self.save(note="password-partial")
                return {"userId": self.user_id, "region": self.region, "partial": True}
            self.cookie_raw = ""

        if self.notify_url and wait > 0:
            if self._poll_verify(seed, wait, on_wait):
                self.save(note="password-2fa")
                return {"userId": self.user_id, "region": self.region, "verified": True}

        # 走到这里说明必须过短信验证。把材料探好交给上层，由它去和用户交互。
        if self.notify_url:
            self.verify_url = self.notify_url
            info = self.begin_verify()
            raise MiNeedVerify(
                f"密码已验证通过，但小米要求短信验证（{info['masked']}）", info)

        raise MiAuthError(
            "登录没有返回跳转地址，也没给验证入口（小米改版了？）。\n"
            "   兜底：浏览器登录 i.mi.com 后，F12 → Network → 过滤 serviceLoginAuth2\n"
            "   → 最后一条 → Response Headers 整段复制存成 mi_login.txt\n"
            "   → run.bat mi login --json-file mi_login.txt")

    # ---------- 安全验证（短信验证码） ----------
    def begin_verify(self) -> dict:
        """探一次验证方式：GET /identity/list，拿 identity_session 与 applyToken。

        返回 {"context", "session", "flag", "apply_token", "masked", "options"}；
        `flag=4` 是手机短信，`flag=8` 是邮箱。
        """
        if not self.verify_url:
            raise MiAuthError("没有验证入口地址 → run.bat mi login 重来一次")
        ctx = _ctx_of(self.verify_url)
        if not ctx:
            raise MiAuthError("验证地址里没有 context（小米改版了）")

        url = (f"{ACCOUNT}{IDENTITY_LIST}?sid={SID}"
               f"&context={urllib.parse.quote(ctx)}"
               f"&_json=true&_dc={int(time.time() * 1000)}")
        _, hd, text = _http("GET", url, headers=XHR_HEADERS)
        jar = _cookie_dict(hd)
        sess = jar.get("identity_session", "")
        if not sess:
            raise MiAuthError("小米没下发 identity_session，验证流程走不下去")
        # 整串都留着：deviceId 之类的 Cookie 决定了后面还算不算「这台设备」
        self._id_jar = {**self._id_jar, **jar}

        d = _json_of(text)
        options = d.get("options") or [d.get("flag") or 4]
        self._verify = {
            "context": ctx,
            "session": sess,
            "flag": int(d.get("flag") or options[0]),
            "options": [int(x) for x in options],
            "apply_token": d.get("applyToken") or "",
            "masked": _mask_phone(d.get("externalId") or ""),
            "retrieve_type": d.get("retrieveType") or "",
        }
        return self._verify

    def _verify_headers(self) -> dict:
        hdrs = dict(XHR_HEADERS)
        jar = dict(self._id_jar)
        jar.setdefault("identity_session", self._verify.get("session", ""))
        if self._ick:
            jar["ick"] = self._ick
        hdrs["Cookie"] = _cookie_str(jar)
        return hdrs

    def captcha(self) -> bytes:
        """取一张图形验证码，返回 JPEG 原始字节（调用方存盘给用户看）。"""
        if not self._verify:
            self.begin_verify()
        _, hd, img = _http("GET", ACCOUNT + CAPTCHA_PATH,
                           headers={"Referer": XHR_HEADERS["Referer"]}, raw=True)
        jar = _cookie_dict(hd)
        self._ick = jar.get("ick", "")
        if not isinstance(img, bytes) or len(img) < 200:
            raise MiAuthError("图形验证码没取到（返回内容太短）")
        if img[:2] != b"\xff\xd8":  # JPEG 魔数，不是图片说明被风控了
            raise MiAuthError(f"图形验证码不是图片：{img[:60]!r}")
        return img

    def send_code(self, icode: str) -> dict:
        """带图形验证码去要短信。返回原始 JSON，`code=0` 才算发出去。"""
        if not self._verify:
            raise MiAuthError("还没探过验证方式 → 先调 begin_verify()")
        form = {
            "_flag": str(self._verify["flag"]),
            "context": self._verify["context"],
            "applyToken": self._verify["apply_token"],
            "icode": (icode or "").strip(),
            "sid": SID,
            "_locale": "zh_CN",
            "_json": "true",
            "_dc": str(int(time.time() * 1000)),
        }
        if self._ick:
            form["ick"] = self._ick  # 图里那串的配对标识，Cookie 里也有一份，两处都带
        _, _, text = _http("POST", ACCOUNT + IDENTITY_SEND,
                           body=urllib.parse.urlencode(form),
                           headers=self._verify_headers())
        return _json_of(text)

    def verify_code(self, code: str) -> dict:
        """提交短信验证码。成功后顺带把会话与 ssecurity 都落到 self 上。"""
        if not self._verify:
            raise MiAuthError("还没探过验证方式 → 先调 begin_verify()")
        self.pragma_security = ""  # 别把上一轮登录残留的 ssecurity 当成本次结果
        form = {
            "_flag": str(self._verify["flag"]),
            "ticket": (code or "").strip(),
            "trust": "false",
            "context": self._verify["context"],
            "applyToken": self._verify["apply_token"],
            "sid": SID,
            "_locale": "zh_CN",
            "_json": "true",
            "_dc": str(int(time.time() * 1000)),
        }
        status, hd, text = _http("POST", ACCOUNT + IDENTITY_VERIFY,
                                 body=urllib.parse.urlencode(form),
                                 headers=self._verify_headers())
        body = (text if isinstance(text, str) else text.decode("utf-8", "replace")).strip()
        if body.startswith("&&&START&&&"):
            body = body[len("&&&START&&&"):]
        if body.startswith("{"):
            d = _json_of(body)
            if d.get("code") not in (0, "0"):
                err = _verify_error(d)
                # 码填错可以重输；「会话过期 / 码已被消费」重输没用，别让用户白等短信
                raise (MiCodeError(err) if _verify_retryable(d) else MiAuthError(err))
        elif status in (301, 302, 303, 307) and _location(hd):
            # 有些分支不返回 JSON，直接 302 到落地页
            d = {"code": 0, "location": _location(hd)}
        else:
            raise MiAuthError(
                f"验证返回看不懂（HTTP {status}）：{body[:120] or '空响应'}")

        # 验证通过后：先收下 body / 响应头里的 ssecurity，再跟 location 换会话
        sec = d.get("ssecurity") or _pragma_ssecurity(hd) or ""
        loc = str(d.get("location") or "")
        # 这一跳的 Set-Cookie 也要带上（有的分支在这里就种好 serviceToken）；
        # 验证链路上的 identity_session / deviceId 一并带上，否则重登会被当新设备
        seed = _cookie_str({**self._id_jar, **_cookie_dict(hd)})
        sess = ""
        if loc.startswith("http"):
            try:
                sess = self._follow(loc, seed=seed, require="")
            except MiAuthError:
                sess = ""  # 有些分支 location 只用来标记完成，没有 Cookie 可收
        elif seed:
            sess = seed

        # ssecurity 也可能在跟跳途中那一跳的 extension-pragma 里
        sec = sec or self.pragma_security
        if sess:
            self._cookie = sess      # 业务请求用的是这个健康服务会话
            self.cookie_raw = sess
        if sec:
            self._ssecurity = base64.b64decode(sec)

        # 还缺密钥：用同一账号再提交一次 auth2。验证已过，这次不会再拦，
        # 而 auth2 的跳转响应头里一定带 extension-pragma（ssecurity 的真正出处）。
        if not self._ssecurity:
            user, pwd = self._cred
            if user and pwd and not self._relogin:
                self._relogin = True
                try:
                    r = self.login(user, pwd, seed=self.cookie_raw or sess)
                    r["verified"] = True
                    r["relogged"] = True
                    self._verify = {}
                    return r
                except MiNeedVerify:
                    pass  # 还是要求验证，落到下面的提示
                except MiAuthError:
                    pass
            if self.cookie_raw:
                self.save(note="sms-partial")  # 会话先留着，下次运行可能直接用
            raise MiAuthError(
                "验证通过了，但没能换到签名密钥 ssecurity。\n"
                "   先直接重跑一次 run.bat mi login（这台设备已经验证过，通常就直接过）。\n"
                "   还被拦就走浏览器（一次到位）：F12 → Network → 过滤 serviceLoginAuth2\n"
                "   → 最后一条 → Response Headers 整段复制存成 mi_login.txt\n"
                "   → run.bat mi login --json-file mi_login.txt")

        if not self.user_id:
            # 验证后这一跳把 userId / passToken 放在 Cookie 里，body 里没有
            jar2 = _cookie_parse(self.cookie_raw)
            self.user_id = str(d.get("userId") or jar2.get("userId") or "")
            self.pass_token = self.pass_token or jar2.get("passToken", "")
        self.verify_url = ""
        self._verify = {}
        self.save(note="sms-2fa")
        return {"userId": self.user_id, "region": self.region, "verified": True}

    def _poll_verify(self, seed: str, wait: int, on_wait=None) -> bool:
        """二次验证期间反复问 serviceLogin：拿到 ssecurity 就说明手机那边点了允许。"""
        deadline = time.time() + wait
        step = 3
        while time.time() < deadline:
            time.sleep(step)
            try:
                _, _, text = _http(
                    "GET", f"{ACCOUNT}/pass/serviceLogin?_json=true&sid={SID}",
                    headers={"Cookie": seed} if seed else {})
                info = _login_payload(text)
            except (MiError, MiAuthError):
                continue
            if on_wait:
                on_wait(info)
            if info.get("ssecurity") and info.get("location"):
                self._ssecurity = base64.b64decode(info["ssecurity"])
                self.user_id = str(info.get("userId") or self.user_id)
                self._cookie = self._follow(info["location"], seed=seed)
                self.cookie_raw = seed
                self.pass_token = info.get("passToken") or self.pass_token
                return True
        return False

    def _ask_login(self, cookie: str) -> tuple[dict, object]:
        """问一次 serviceLogin，返回 (JSON, 响应头)。"""
        _, hd, text = _http(
            "GET", f"{ACCOUNT}/pass/serviceLogin?_json=true&sid={SID}&_locale=zh_CN",
            headers={"Cookie": cookie} if cookie else {})
        return _login_payload(text), hd

    def open_session(self) -> bool:
        """用现有凭据换 ssecurity + serviceToken。返回是否真的换到了新会话。

        三条容易踩空的路，这里都兜住了：
        ① `serviceLogin` 未登录时**也回 location**（指向登录页），不能当成功标志；
        ② `ssecurity` 有时只在跳转响应的 `extension-pragma` 头里，body 里没有；
        ③ 浏览器登录态给的是 `location`（带 ticket），要先换 serviceToken 再问一次。
        """
        cookie = self.cookie_header()
        if not cookie:
            if self._ssecurity:
                return False  # 只有缓存的 ssecurity，凑合用，请求时会给出明确错误
            raise MiAuthError("还没登录过小米账号 → run.bat mi login")

        info, hd = self._ask_login(cookie)
        sec = info.get("ssecurity") or _pragma_ssecurity(hd)
        if sec:
            self._ssecurity = base64.b64decode(sec)
            if info.get("location"):
                self._cookie = self._follow(info["location"], seed=cookie)
            self.pass_token = info.get("passToken") or self.pass_token
            return True

        # 没有 ssecurity：如果给的是带 ticket 的落点，先换 serviceToken，再拿它问一次
        loc = str(info.get("location") or "")
        if loc.startswith("http") and "ticket=" in loc:
            try:
                sess = self._follow(loc, seed=cookie)
            except MiAuthError:
                sess = ""
            if sess:
                self._cookie = sess
                info2, hd2 = self._ask_login(sess)
                sec2 = info2.get("ssecurity") or _pragma_ssecurity(hd2)
                if sec2:
                    self._ssecurity = base64.b64decode(sec2)
                    self.pass_token = info2.get("passToken") or self.pass_token
                    return True
                if self.pragma_security:
                    self._ssecurity = base64.b64decode(self.pragma_security)
                    return True

        if self._ssecurity:
            self.diag = "换票没拿到 ssecurity，改用缓存值"
            return False

        code = info.get("code")
        if code in (70016, 70014):
            raise MiAuthError(
                "这份凭据不能用了（小米返回 code=70016）。\n"
                "   重跑一次 run.bat mi login —— 现在它会自己走完安全验证：\n"
                "   输账号密码 → 弹一张图形验证码让你认字 → 收短信验证码 → 自动换到会话。")
        raise MiAuthError(
            f"换会话失败（code={code or '无'}）：Cookie 里没有能用的登录态 → run.bat mi login")

    def _follow(self, url: str, seed: str = "", max_hops: int = 6,
                require: str = "serviceToken") -> str:
        """跟着 location 走几跳，收齐 Set-Cookie。

        两个必须做对的地方：
        ① 每一跳都要**带上已收到的 Cookie**——`serviceToken` 是第一跳种下的，
           `sts-hlth.io.mi.com/healthapp/sts` 这一跳（真正的落点）要读到它才放行；
        ② Set-Cookie 常分多次下发，必须逐跳累积，不能只看最后一跳。

        `require=""` 表示不强制某个 Cookie——短信验证后的那一跳只种健康服务会话，
        本来就没有 serviceToken。
        """
        jar: dict[str, str] = {}
        for part in (seed or "").split(";"):
            part = part.strip()
            if "=" in part:
                k, _, v = part.partition("=")
                jar[k.strip()] = v.strip()
        seen: set[str] = set()
        last_status = 0
        for _ in range(max_hops):
            hdrs = {"Cookie": "; ".join(f"{k}={v}" for k, v in jar.items())} if jar else {}
            last_status, headers, _ = _http("GET", url, headers=hdrs)
            sec = _pragma_ssecurity(headers)
            if sec:
                self.pragma_security = sec
            for line in _header_values(headers, "Set-Cookie"):
                first = line.split(";", 1)[0]
                if "=" in first:
                    k, _, v = first.partition("=")
                    jar[k.strip()] = v.strip()
            loc = _location(headers)
            if last_status not in (301, 302, 303, 307) or not loc or loc in seen:
                break
            seen.add(loc)
            url = loc if loc.startswith("http") else urllib.parse.urljoin(url, loc)

        if require and require not in jar:
            raise MiAuthError(
                f"登录跳转没下发 {require}（HTTP {last_status}，"
                f"拿到 {', '.join(jar) or '无 Cookie'}）→ 改用 --json-file 导入")
        return "; ".join(f"{k}={v}" for k, v in jar.items())

    def import_login_json(self, text: str) -> dict:
        """吃浏览器 F12 里抄回来的登录材料，形式不限：

        - `serviceLogin` 的 JSON（有 `ssecurity` 最好，只有 `location` 也能接着走）
        - `serviceLoginAuth2` 的**响应头**整段（`extension-pragma` 里有 ssecurity）
        - 光秃秃一行 `ssecurity: xxx`

        ssecurity 是唯一的硬门槛；location / ticket 都能靠跟跳转补出 serviceToken。
        """
        text = str(text or "").strip()
        if not text:
            raise MiAuthError("文件是空的")
        if text.startswith("&&&START&&&"):
            text = text[len("&&&START&&&"):]

        d, ssec, loc = {}, "", ""
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                d = parsed
                ssec = str(d.get("ssecurity") or "")
                loc = str(d.get("location") or "")
            else:
                raise ValueError("not dict")
        except Exception:
            parsed = None

        if not ssec:
            ssec = _pragma_ssecurity(_TextHeaders(text)) or _bare_ssecurity(text)
        if not d and not ssec:
            head = text[:80].replace("\n", " ")
            raise MiAuthError(
                f"这段内容里既没有 JSON 也没有 ssecurity：{head}…\n"
                "要的是 ① serviceLogin 页面的 JSON，或 ② F12 里 serviceLoginAuth2 的响应头")

        if ssec:
            self._ssecurity = base64.b64decode(ssec)
        if d.get("userId"):
            self.user_id = str(d["userId"])
        if d.get("passToken"):
            self.pass_token = str(d["passToken"])

        # 有落点就换 serviceToken；没有落点则用已有凭据再问一次
        if loc:
            self.cookie_raw = ""  # 旧 Cookie 可能属于别的服务，别带进新会话
            try:
                self._cookie = self._follow(loc)
            except MiAuthError:
                self._cookie = ""
        if not self._ssecurity and self.pragma_security:
            self._ssecurity = base64.b64decode(self.pragma_security)
        if not self._ssecurity:
            self.cookie_raw = self.cookie_raw or self._cookie
            if not self.open_session():
                raise MiAuthError(
                    "拿到的是「已登录但没给 ssecurity」的返回。补一步就能通：\n"
                    "   F12 → Network → 过滤 serviceLoginAuth2 → 点最后一条\n"
                    "   → Response Headers 整段复制 → 存成 mi_login.txt\n"
                    "   → run.bat mi login --json-file mi_login.txt")
        self.save(note="browser-json")
        return {"userId": self.user_id, "region": self.region}

    # ---------- 业务请求 ----------
    def post(self, path: str, payload: dict) -> dict:
        if not self._ssecurity:
            raise MiAuthError("会话还没建立 → run.bat mi login")
        nonce = _nonce()
        sn = _signed_nonce(self._ssecurity, nonce)
        self._sn = sn

        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        form = {"data": data}
        form["rc4_hash__"] = _sign("POST", path, form, sn)
        enc = {k: _b64e(rc4_drop1024(sn, v.encode())) for k, v in form.items()}
        enc["signature"] = _sign("POST", path, enc, sn)
        enc["_nonce"] = _b64e(nonce)

        status, _, text = _http("POST", self.base + path,
                                body=urllib.parse.urlencode(enc),
                                headers={"Cookie": self._cookie})
        if status != 200:
            raise MiError(f"小米接口 HTTP {status}：{text.strip()[:200]}")
        body = self._decode(sn, text)
        code = body.get("code")
        if code != 0:
            msg = str(body.get("message") or body.get("desc") or "")
            if _looks_auth(code, msg):
                raise MiAuthError(f"小米接口拒绝（code={code} {msg}）→ 凭据可能过期")
            raise MiError(f"小米接口报错（code={code} {msg}）")
        return body.get("result") or {}

    @staticmethod
    def _decode(sn: bytes, text: str) -> dict:
        raw = text.strip()
        try:
            plain = rc4_drop1024(sn, base64.b64decode(raw))
            return json.loads(plain)
        except Exception:
            pass
        try:  # 有些错误响应是明文
            return json.loads(raw)
        except Exception:
            raise MiError(f"响应无法解密：{raw[:160]}") from None

    def fetch(self, key: str, start_ts: int, end_ts: int, limit_pages: int = 60) -> list[dict]:
        items: list[dict] = []
        next_key = ""
        seen: set[str] = set()
        for _ in range(limit_pages):
            payload = {"start_time": int(start_ts), "end_time": int(end_ts), "key": key}
            if next_key:
                payload["next_key"] = next_key
            res = self.post("/app/v1/data/get_fitness_data_by_time", payload)
            items.extend(res.get("data_list") or [])
            nk = res.get("next_key")
            if not res.get("has_more") or not nk:
                break
            nk = str(nk)
            if nk in seen:
                break
            seen.add(nk)
            next_key = nk
        return items

    def weights(self, days: int = 90) -> list[dict]:
        end = int(time.time())
        start = end - int(days * 86400)
        return self.fetch("weight", start, end)

    def probe(self) -> dict:
        """诊断：账号通不通、云端到底有没有 weight。

        只看 weight 是分不清「登录/会话有问题」和「这个云里根本没有秤的数据」的，
        所以顺手查一下 steps 当对照——有步数就说明账号、会签、接口全对，
        缺的只是体重那一路。
        """
        out = {"user": self.user_id, "region": self.region, "windows": [], "alive": 0}
        for d in (30, 365, 1500):
            items = self.weights(d)
            out["windows"].append({"days": d, "count": len(items)})
            if items:
                out["sample"] = items[-1]
                out["first"] = items[0]
                break
        try:
            now = int(time.time())
            # 只取第一页就够判断账号活不活，不用把几千条步数全拉下来
            out["alive"] = len(self.fetch("steps", now - 30 * 86400, now, limit_pages=1))
        except MiError:
            pass
        return out


def _looks_auth(code, msg: str) -> bool:
    if code in (401, 403, -6, -10001, 70016):
        return True
    low = str(msg).lower()
    return any(m in low for m in AUTH_ERROR_MARKERS)


def _diag(res: dict) -> str:
    """登录返回的诊断摘要：只列字段名与状态，**绝不**打印票据本身。"""
    have = [k for k in ("passToken", "ssecurity", "location", "userId", "cUserId",
                        "notificationUrl", "_sign", "captchaUrl") if res.get(k)]
    info = {k: res.get(k) for k in ("code", "desc", "description", "securityStatus")
            if res.get(k)}
    return json.dumps({"fields": sorted(res), "has": have, "info": info},
                      ensure_ascii=False)


def _login_error(code, res: dict) -> str:
    table = {
        70014: "账号或密码不对（手机号 / 邮箱 / 小米 ID 都可以试）",
        70016: "登录验证失败：密码不对，或触发了二次验证（新设备 / 异地）",
        70017: "账号被锁，去 i.mi.com 解锁",
        70022: "登录过于频繁，等十分钟再试",
        1: "小米改了登录接口参数，改用 --cookie",
    }
    base = table.get(code)
    desc = str(res.get("desc") or res.get("description") or "").strip()
    if base:
        return f"{base}（code={code}）"
    return f"登录失败 code={code}{('：' + desc) if desc else ''}"


# ---------------------------------------------------------------- 转成 body 行
def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_records(items: list[dict]) -> list[dict]:
    """云端条目 → body 表要的扁平行。保留 raw 供以后补字段。"""
    out = []
    for it in items:
        try:
            v = it.get("value")
            v = json.loads(v) if isinstance(v, str) else (v or {})
        except Exception:
            continue
        kg = _num(v.get("weight"))
        if kg is None or not 20 <= kg <= 300:
            continue
        ts = int(it.get("time") or 0)
        if not ts:
            continue
        off = int(it.get("zone_offset") or 28800)
        local = dt.datetime.fromtimestamp(ts, dt.timezone(dt.timedelta(seconds=off)))
        fat = _num(v.get("body_fat_rate"))
        mr = _num(v.get("muscle_rate"))
        out.append({
            "date": local.date().isoformat(),
            "ts": local.strftime("%Y-%m-%d %H:%M"),
            "weight_kg": round(kg, 2),
            # 肌肉率是百分比，项目里要 kg —— 按体重换算，raw 里留原值
            "muscle_kg": round(kg * mr / 100, 2) if mr and 0 < mr < 100 else None,
            "body_fat_pct": round(fat, 1) if fat and 0 < fat < 100 else None,
            "water_pct": _num(v.get("moisture_rate")) or None,
            "visceral": int(_num(v.get("visceral_fat")) or 0) or None,
            "bmr": int(_num(v.get("basal_metabolism")) or 0) or None,
            "raw": json.dumps(v, ensure_ascii=False)[:600],
        })
    return out


def sync(store, home: Path, days: int = 120) -> tuple[int, list[dict]]:
    """登录 → 拉 weight → 入库。返回 (条数, 记录)。"""
    from . import body as B

    mi = MiCloud(home)
    if not mi.load():
        raise MiAuthError("还没登录过小米账号 → run.bat mi login")
    mi.open_session()
    items = mi.weights(days)
    recs = to_records(items)
    n = B.save(store, recs, "mi") if recs else 0
    return n, recs
