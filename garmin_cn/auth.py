"""国区（connect.garmin.cn）登录与会话。

2026-11 佳明把登录迁到 SSO Portal（Vue SPA），流程变成：

    GET  https://sso.garmin.cn/portal/sso/zh-CN/sign-in?...  （预热，拿验证码评估）
    POST https://sso.garmin.cn/portal/api/login?...           -> serviceTicketId
    GET  <serviceURL>?ticket=...                              -> 下发 JWT_WEB
    之后所有数据请求走 https://connect.garmin.cn/gc-api/...

老的 `/mobile/api/login` 仍返回票据，但票据在 /app 换不出 Cookie，已废弃
（只在 portal 接口不可用时作为兜底）。老 CAS 表单 `/sso/login` 对 service=/app
直接 500，也不可用。

`RC_LOGIN_RELAY` 可选：把登录交给一个能过 Cloudflare 的中转（例如 Cloudflare
Worker，见 deploy/cf-login-relay.js）。机房 IP 用它才能替网页版登录。
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

from .net import NAV_HEADERS, Net, extract_csrf

SSO = "https://sso.garmin.cn"
CONNECT = "https://connect.garmin.cn"
MODERN = f"{CONNECT}/modern"
CLIENT_ID = "GCM_IOS_DARK"
SERVICE = f"{CONNECT}/modern"

LOGIN_URL = (
    f"{SSO}/mobile/api/login?clientId={CLIENT_ID}"
    f"&locale=zh-CN&service={urllib.parse.quote(SERVICE, safe='')}"
)
MFA_URLS = [
    f"{SSO}/mobile/api/mfa/verifyCode?clientId={CLIENT_ID}"
    f"&locale=zh-CN&service={urllib.parse.quote(SERVICE, safe='')}",
    f"{SSO}/mobile/api/mfa/verify?clientId={CLIENT_ID}"
    f"&locale=zh-CN&service={urllib.parse.quote(SERVICE, safe='')}",
]

# ---- 新版 SSO Portal（2026-11 起生效）--------------------------------------
# 佳明把国区登录从 /sso/login 迁到了 Vue SPA 的 SSO Portal。老接口
# `/mobile/api/login` 仍然返回 SUCCESSFUL + serviceTicketId，但那张票在
# `/app?ticket=` 换不出 JWT_WEB——服务端反而会 Set-Cookie 清空会话，
# 于是「账号密码完全正确、却一直提示登录失效」。
#
# 新流程（与 /portal/assets/index-*.js 里的 signinStore 一致）：
#   POST /portal/api/login?clientId=GarminConnect&service=<connect>/app
#        {"username","password","rememberMe","captchaToken"} -> serviceTicketId
#   GET  <serviceURL>?ticket=...  -> 下发 JWT_WEB + session
# 验证码走 /portal/api/mfa/verifyCode，参数带 mfaSetup。
PORTAL = f"{SSO}/portal"
PORTAL_SERVICE = f"{CONNECT}/app"
PORTAL_QS = "clientId=GarminConnect&service=" + urllib.parse.quote(
    PORTAL_SERVICE, safe="")
PORTAL_LOGIN_URL = f"{PORTAL}/api/login?{PORTAL_QS}"
PORTAL_SIGNIN_URL = f"{SSO}/portal/sso/zh-CN/sign-in?{PORTAL_QS}"
PORTAL_MFA_URL = f"{PORTAL}/api/mfa/verifyCode?{PORTAL_QS}"
# 必须要桌面 Chrome UA：用 GCM-iOS 或 Python 默认 UA 打开登录页，Cloudflare
# 直接回 403，紧接着的 /portal/api/login 会报
# 「Previous captcha evaluation is mandatory for this request」。
PORTAL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
PORTAL_HEADERS = {
    "User-Agent": PORTAL_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": SSO,
    "Referer": PORTAL_SIGNIN_URL,
}
PORTAL_PAGE_HEADERS = {
    "User-Agent": PORTAL_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


class AuthError(RuntimeError):
    pass


class MfaRequired(RuntimeError):
    """需要邮箱验证码。调用方拿到异常后调用 submit_mfa(code)。"""

    def __init__(self, payload: dict | None = None):
        self.payload = payload or {}
        super().__init__("需要邮箱验证码（Garmin 已发送到注册邮箱）")


class GarminCNAuth:
    def __init__(self, home: Path):
        self.home = home
        self.session_path = home / "data" / "session.json"
        self.net = Net(self.session_path, min_interval=0.5)
        self._pending_ticket_ctx: dict | None = None
        # 本次登录到底有没有换出「新」会话（False = 只是旧会话还活着）
        self.fresh_exchange = False

    # ---------------- 登录 ----------------
    def login(self, email: str, password: str) -> dict:
        """新版 SSO Portal 登录；接口不可用（404/非 JSON）时退回老通道。

        返回空响应只有两种情况：老接口被下线，或者网络/网关挂了。两种都值得
        再试一次老通道——只是换票同样会失败，最后抛出的错误会更具体。

        配了 `RC_LOGIN_RELAY` 就先走中转：机房 IP 打不开佳明登录页
        （Cloudflare 挑战），中转（境外 Worker/家宽）能过。
        """
        relay = (os.environ.get("RC_LOGIN_RELAY") or "").strip()
        if relay:
            return self._login_relay(relay, email, password)
        self._portal_warmup()
        status, payload = self.net.post_json(
            PORTAL_LOGIN_URL,
            {"username": email, "password": password, "rememberMe": True,
             "captchaToken": None},
            headers=PORTAL_HEADERS,
        )
        if payload is None:
            if status == 403:
                # Cloudflare 的「cf-mitigated: challenge」：机房/数据中心 IP
                # 打开 sso.garmin.cn/portal 会被挡在 JS 验证页外，老通道也
                # 拿不到能换会话的票据，再试只是白等。
                raise AuthError(
                    "佳明登录页拒绝了这台设备（Cloudflare 403）。"
                    "机房 IP 无法代登录，改用手机 App 登录并同步登录态。")
            return self._login_legacy(email, password, status)
        return self._accept_login(payload)

    def _login_relay(self, relay: str, email: str, password: str) -> dict:
        """通过中转登录：中转替我们过 Cloudflare，只把 Cookie 交回来。

        约定（deploy/cf-login-relay.js 实现同一份）：
          POST <relay>  {"username","password"} -> {"ok":true,"cookies":[...]}
        中转拿不到结果时返回 `{ok:false,error}`，这里原样抛出，不猜。
        """
        if not relay.startswith(("http://", "https://")):
            raise AuthError("RC_LOGIN_RELAY 要写成 https:// 开头的地址")
        body = json.dumps({"username": email, "password": password}).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8",
                   "Accept": "application/json"}
        tok = (os.environ.get("RC_LOGIN_RELAY_TOKEN") or "").strip()
        if tok:
            headers["X-Relay-Token"] = tok
        req = urllib.request.Request(relay, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raw = ""
            try:
                raw = e.read().decode("utf-8", "replace")
            except Exception:      # noqa: BLE001
                pass
            raise AuthError(f"登录中转返回 HTTP {e.code}：{raw[:200]}")
        except Exception as e:      # noqa: BLE001
            raise AuthError(f"连不上登录中转：{type(e).__name__}: {e}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise AuthError(f"登录中转返回的不是 JSON：{raw[:200]}")
        if not data.get("ok"):
            raise AuthError(str(data.get("error") or "登录中转失败")[:300])
        cookies = data.get("cookies")
        if not isinstance(cookies, list) or not cookies:
            raise AuthError("登录中转没有返回 Cookie")
        if not self.net.load_cookies(cookies):
            raise AuthError("登录中转返回的 Cookie 里没有会话")
        self.fresh_exchange = True
        self.net.save_session()
        profile = self._verify()
        if not profile:
            raise AuthError("中转拿到了 Cookie，但数据接口仍拒绝访问")
        return {"csrf": self.net.get_cookie("connect-csrf-token"),
                "profile": profile, "fresh": True}

    def _portal_warmup(self) -> None:
        """先打开一次登录页，让 Cloudflare/Portal 建立「验证码已评估」的会话。

        少了这一步，`/portal/api/login` 直接 400：
        `Previous captcha evaluation is mandatory for this request`。
        失败不抛——接口真的迁走了还要走老通道兜底。
        """
        try:
            self.net.request("GET", PORTAL_SIGNIN_URL, headers=PORTAL_PAGE_HEADERS)
        except Exception:      # noqa: BLE001 - 预热失败就交给后面的接口报错
            pass

    def _login_legacy(self, email: str, password: str, portal_status=None) -> dict:
        status, payload = self.net.post_json(
            LOGIN_URL,
            {"username": email, "password": password, "rememberMe": True},
            headers={"X-Requested-With": "XMLHttpRequest", "User-Agent": "GCM-iOS/5.0"},
        )
        if payload is None:
            raise AuthError(
                f"登录接口没有返回数据（新通道 {portal_status}、老通道 {status}）。"
                "请检查网络，或稍后再试")
        return self._accept_login(payload)

    def _accept_login(self, payload: dict) -> dict:
        rs = payload.get("responseStatus") or {}
        rtype = rs.get("type") or ""
        ticket = payload.get("serviceTicketId")

        if rtype == "SUCCESSFUL" and ticket:
            return self._finish_ticket(ticket, payload.get("serviceURL"))

        if payload.get("customerMfaInfo") and not ticket:
            self._pending_ticket_ctx = payload
            raise MfaRequired(payload)

        if rtype == "INVALID_USERNAME_PASSWORD":
            raise AuthError("邮箱或密码不正确（国区账号请用注册邮箱，不是手机号）")
        if rtype in ("CAPTCHA_REQUIRED", "MFA_REQUIRED_CAPTCHA"):
            raise AuthError("触发了图形验证码，请先在浏览器登录一次 connect.garmin.cn 再重试")
        if rtype in ("ACCOUNT_LOCKED", "ACCOUNT_DISABLED", "ACCOUNT_DEACTIVATED",
                     "ACCOUNT_DEACTIVATED_PHONE_OR_EMAIL"):
            raise AuthError(f"账号状态异常：{rtype}，请到佳明官网处理后再试")
        if rtype in ("PHONE_VERIFICATION_REQUIRED", "PHONE_REGISTRATION_REQUIRED",
                     "LOGIN_IDENTIFIER_SETUP"):
            raise AuthError("账号需要在佳明 App 里先补齐手机号/登录名验证")
        if rtype in ("PERMANENT_MFA_SETUP_REQUIRED",
                     "PERMANENT_MFA_SETUP_REQUIRED_RESET_PASSWORD"):
            raise AuthError("账号被要求设置永久两步验证，需先到官网完成设置")

        # 新版 Portal 的 400 长这样：{"error":"…InvalidRequestException",
        # "errorText":"Previous captcha evaluation is mandatory for this request"}
        # 没有 responseStatus，直接把它抛出来，别吞成「登录失败：」。
        err = str(payload.get("errorText") or payload.get("error") or "").strip()
        if err:
            if "captcha" in err.lower():
                # 2026-11-24 起登录页上了 reCAPTCHA Enterprise（可见复选框），
                # 人在页面上点一下才算「评估过」。程序拿不到那个 token，
                # 密码登录这条路在 App 里彻底走不通，跟账号密码无关。
                raise AuthError(
                    "佳明现在要求人机验证（reCAPTCHA），密码只能由人在浏览器里"
                    "点一次。账号和密码没有问题，旧会话到期前仍能同步。"
                    "要在电脑上登录 connect.garmin.cn，把 Cookie 导出成 JSON，"
                    "粘到网页版「连接数据源 → 导入登录态」。")
            raise AuthError(f"登录被拒绝：{err}")

        raise AuthError(
            f"登录失败：{rtype} {rs.get('message', '')}".strip()
            or f"登录未返回 ticket：{json.dumps(payload, ensure_ascii=False)[:300]}")

    def submit_mfa(self, code: str) -> dict:
        if not self._pending_ticket_ctx:
            raise AuthError("没有待完成的验证码会话，请重新调用 login()")
        info = self._pending_ticket_ctx.get("customerMfaInfo") or {}
        method = (info.get("mfaLastMethodUsed") or info.get("defaultMfaMethod")
                  or "email")
        status, payload = self.net.post_json(
            PORTAL_MFA_URL,
            {"mfaMethod": method, "mfaCode": code, "mfaSetup": False},
            headers=PORTAL_HEADERS,
        )
        if payload and payload.get("serviceTicketId"):
            self._pending_ticket_ctx = None
            return self._finish_ticket(payload["serviceTicketId"],
                                       payload.get("serviceURL"))
        if payload:
            rs = payload.get("responseStatus") or {}
            if rs.get("type") and rs.get("type") != "SUCCESSFUL":
                raise AuthError(
                    f"验证码校验失败：{rs.get('type')} {rs.get('message', '')}".strip())

        last: dict | None = payload
        for url in MFA_URLS:
            status, payload = self.net.post_json(
                url,
                {"mfaMethod": "email", "mfaCode": code},
                headers={"X-Requested-With": "XMLHttpRequest", "User-Agent": "GCM-iOS/5.0"},
            )
            last = payload or {}
            ticket = last.get("serviceTicketId")
            if ticket:
                self._pending_ticket_ctx = None
                return self._finish_ticket(ticket, last.get("serviceURL"))
            if status == 404:
                continue
            rs = last.get("responseStatus") or {}
            if rs.get("type"):
                raise AuthError(f"验证码校验失败：{rs.get('type')} {rs.get('message','')}".strip())
        raise AuthError(f"验证码提交未成功：{json.dumps(last or {}, ensure_ascii=False)[:300]}")

    def _finish_ticket(self, ticket: str, service_url: str | None = None) -> dict:
        """用 ticket 换会话 Cookie（JWT_WEB / session）与 csrf，并验证可用。

        换票地址优先用登录响应里的 `serviceURL`（新版 Portal 会明确给
        `https://connect.garmin.cn/app`）；老流程下它是空的，就退回固定的
        `/app?ticket=`。`/modern?ticket=` 只作为最后的兼容项保留——
        2026-11 之后 `/app` 已经换成新的前端，`/modern` 换不出 Cookie。
        """
        q = urllib.parse.quote(ticket, safe="")
        nav = NAV_HEADERS
        tried: list[str] = []
        html = ""
        before = self._session_sig()  # 用于判断这次到底有没有换出「新」会话

        urls: list[str] = []
        # 只认 connect.garmin.cn 的 serviceURL。老 mobile 接口会返回
        # `https://mobile.integration.garmin.cn/gcm/ios`（佳明内部测试域），
        # 解析不了，会把「登录成功」变成 DNS 报错。
        if service_url and service_url.startswith(f"{CONNECT}/"):
            sep = "&" if "?" in service_url else "?"
            urls.append(f"{service_url}{sep}ticket={q}")
        for base in (PORTAL_SERVICE, f"{PORTAL_SERVICE}/", f"{CONNECT}/modern"):
            u = f"{base}?ticket={q}"
            if u not in urls:
                urls.append(u)

        for url in urls:
            status, html = self.net.request("GET", url, headers=nav)
            tried.append(f"{url.split('?')[0]} → {status}")
            if self._has_session_cookie():
                break

        if not self._has_session_cookie():  # 票据没换成会话 Cookie，试试 SSO 直开
            for url in (f"{PORTAL_SERVICE}", f"{CONNECT}/modern/"):
                status, html = self.net.request("GET", url, headers=nav)
                tried.append(f"{url} → {status}")
                if self._has_session_cookie():
                    break

        if not self._has_session_cookie():
            raise AuthError(
                "票据没有换出会话 Cookie（" + "，".join(tried) + "）。"
                f"当前 Cookie：{', '.join(self.net.cookie_names()) or '无'}"
            )

        # 只有 Cookie 真的变了才算「换票成功」。没变说明用的是磁盘里带进来的旧会话，
        # 后面接口哪怕能通也是旧会话在撑——不标出来就会误报「登录成功」。
        self.fresh_exchange = self._session_sig() != before

        csrf = self._extract_csrf(html) or self._csrf_from_cookies()
        if not csrf:
            csrf = self.refresh_csrf()
        if csrf:
            self.net.set_cookie("connect-csrf-token", csrf)

        self.net.save_session()

        profile = self._verify()
        if not profile:
            raise AuthError(
                "会话 Cookie 拿到了，但数据接口仍拒绝访问"
                f"（{getattr(self, 'last_verify', '未知原因')}）。"
            )
        return {"csrf": csrf, "profile": profile, "fresh": self.fresh_exchange}

    def _has_session_cookie(self) -> bool:
        names = {c.name for c in self.net.jar}
        return bool(names & {"JWT_WEB", "SESSIONID", "session"})

    def _session_sig(self) -> tuple:
        """会话 Cookie 的 (名字, 值) 快照，用来判断会话有没有被真正换掉。

        只看 JWT_WEB / SESSIONID / session 这几枚，别把 CASTGC 之类
        每次登录都会变的 Cookie 算进来——否则「没换会话」会被误判成「换了」。
        """
        names = {"JWT_WEB", "SESSIONID", "session"}
        return tuple(sorted((c.name, c.value) for c in self.net.jar if c.name in names))

    def _extract_csrf(self, html: str) -> str | None:
        return extract_csrf(html)

    def refresh_csrf(self) -> str | None:
        """重开应用页，从 <meta name="csrf-token"> 取最新令牌（令牌随会话轮换）。"""
        status, html = self.net.request("GET", f"{CONNECT}/app/", headers=NAV_HEADERS)
        token = extract_csrf(html) if status == 200 else None
        if token:
            self.net.set_cookie("connect-csrf-token", token)
        return token

    def _csrf_from_cookies(self) -> str | None:
        for c in self.net.jar:
            if "csrf" in c.name.lower():
                return c.value
        return None

    def verify_state(self) -> tuple[str, dict | None]:
        """会话状态三态：`ok` / `expired` / `unknown`。

        为什么必须区分 unknown
        ----------------------
        `client.get()` 对网络异常是**返回 None**（不抛），只有 401/403 才抛
        SessionExpired。旧实现把「拿不到 profile」一律当成会话过期，于是手机
        网络抖一下、或 profile 请求超时一次，App 就会判定「登录已失效」并把
        用户踢回登录框——真机反馈的「登录后一下拉刷新就弹登录」正是这个。

        `unknown` 表示「这次没验成功，但没有证据说明过期」，调用方应当继续用
        现有 Cookie 干活（真过期的话，下一个数据请求会以 401 明确报出来）。
        """
        from .client import GarminCNClient, SessionExpired

        client = GarminCNClient(self.net)
        try:
            profile = client.profile()
        except SessionExpired as e:
            self.last_verify = f"数据接口拒绝访问：{e}"
            return "expired", None
        except Exception as e:
            self.last_verify = f"{type(e).__name__}: {e}"
            return "unknown", None
        if profile and profile.get("displayName"):
            self.last_verify = ""
            return "ok", profile

        status = getattr(client, "last_status", None)
        if status in (401, 403):
            self.last_verify = f"HTTP {status}"
            return "expired", None
        # 5xx / 超时 / 网关异常 / 路径变化：都没资格判定「会话过期」
        self.last_verify = (f"HTTP {status}" if status else
                            "profile 接口没有返回 displayName（可能路径变了或网络不稳）")
        return "unknown", None

    def _verify(self) -> dict | None:
        """严格校验：只有拿到 displayName 才算成功（登录/导入 Cookie 后用）。"""
        state, profile = self.verify_state()
        return profile if state == "ok" else None

    # ---------------- 手动导入 Cookie ----------------
    def import_cookies(self, cookie_header: str) -> dict:
        """从浏览器 DevTools 复制的 Cookie 字符串导入会话。"""
        self.net.drop_garmin_cookies()  # 先扔掉旧的，免得新旧混在一起
        count = 0
        for part in cookie_header.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, _, value = part.partition("=")
            self.net.set_cookie(name.strip(), value.strip())
            count += 1
        if not count:
            raise AuthError("没有解析到任何 Cookie")
        return self._settle(count)

    def adopt_cookies(self, items) -> dict:
        """接浏览器托管登录拿回来的 Cookie（[{name,value,domain,path}]）。

        浏览器给的作用域是 connect / sso / .garmin.cn 好几套，这里统一挂到
        `.garmin.cn`：connect 的数据接口和 sso 的续期都能带上，省得漏发。
        """
        count = 0
        self.net.drop_garmin_cookies()  # 浏览器这套是完整的，旧的全扔掉
        for it in items or []:
            name = (it.get("name") or "").strip()
            value = it.get("value") or ""
            if not name or not value:
                continue
            if not (it.get("domain") or "").endswith("garmin.cn"):
                continue
            self.net.set_cookie(name, value)
            count += 1
        if not count:
            raise AuthError("浏览器里没抓到任何 garmin.cn 的 Cookie")
        return self._settle(count)

    def _settle(self, count: int) -> dict:
        """Cookie 就位后的收尾：补 CSRF → 落盘 → 用数据接口验一次。"""
        # 浏览器登录一般自带 connect-csrf-token；没有才去页面 meta 里现取
        csrf = self._csrf_from_cookies() or self.refresh_csrf()
        self.net.save_session()
        profile = self._verify()
        if not profile:
            raise AuthError(
                f"Cookie 导入后接口仍拒绝（{getattr(self, 'last_verify', '未知原因')}），"
                "请确认复制的是 connect.garmin.cn 的完整 Cookie（要含 JWT_WEB 和 session）"
            )
        return {"cookies": count, "csrf": csrf, "profile": profile}

    # ---------------- 复用 ----------------
    def renew(self) -> bool:
        """不碰密码，看看能不能把过期的票续回来。

        为什么会「过一会就要重新登录」：`JWT_WEB` 只有几小时，而 SSO 那边的
        会话（`CASTGC` / `session`）活得更久。以前 JWT 一过期就静默用存的密码
        重登，用户完全无感；现在登录页强制人机验证，那条路走不通了，于是每次
        JWT 到期都会弹一次登录框。

        这里走的是浏览器里最普通的那条路：带着 SSO 会话去访问一次登录页，
        CAS 会自己 302 到 `service?ticket=...`，跟下来就换到一枚新的 JWT_WEB。
        换到了就落盘，换不到就返回 False（调用方再去要密码或让用户点一次）。

        只在**已有会话 Cookie** 时尝试：没有任何 Cookie 时去访问登录页，只会
        拿到一个未登录的登录页，白白多一次请求。
        """
        if not (self.net.get_cookie("JWT_WEB") or self.net.get_cookie("SESSIONID")
                or self.net.get_cookie("CASTGC") or self.net.get_cookie("session")):
            return False
        before = self._session_sig()
        try:
            self.net.request("GET", PORTAL_SIGNIN_URL, headers=PORTAL_PAGE_HEADERS)
        except Exception:      # noqa: BLE001 - 网络问题不该被当成「需要重登」
            return False
        changed = self._session_sig() != before
        state, _ = self.verify_state()
        # 只认两种结论：验过是好的，或者票确实被换过只是这次没验成（网络）。
        # 「服务端明说 401」时一律不写盘——那张死票盖下去，下一次连原始 Cookie
        # 都没了，只能重新登录，比不续还糟。
        if state == "expired":
            return False
        if state == "unknown" and not changed:
            return False
        csrf = self._csrf_from_cookies() or self.refresh_csrf()
        if csrf:
            self.net.set_cookie("connect-csrf-token", csrf)
        self.net.save_session()
        return True

    def ensure_session(self) -> bool:
        """已有会话是否仍然可用。

        **只有明确被服务端拒绝（401/403）才算过期**；网络异常一律放行，
        免得一次请求抖动就把人踢回登录框。

        票据判据必须和 `_garmin_session_ok()` / `_client()` 一致：
        那里认 `JWT_WEB or SESSIONID`，这里以前只认 `JWT_WEB`。手机号登录
        走的是 SESSIONID，于是「我的」页显示已登录、同步却一律报「会话已
        过期」——用户看到的自相矛盾就是这个。
        """
        if not self.session_path.exists():
            return False
        if not (self.net.get_cookie("JWT_WEB") or self.net.get_cookie("SESSIONID")):
            return False
        state, _ = self.verify_state()
        return state != "expired"
