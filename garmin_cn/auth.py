"""国区（connect.garmin.cn）登录与会话。

国区与国际区完全隔离，官方的 Personal API 不覆盖国区账号，
这里走的是国区 App 使用的移动端 SSO 通道：

    POST https://sso.garmin.cn/mobile/api/login   -> serviceTicketId
    GET  https://connect.garmin.cn/modern?ticket= -> 下发 JWT_WEB + csrf
    之后所有数据请求走 https://connect.garmin.cn/gc-api/...
"""
from __future__ import annotations

import json
import urllib.parse
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
        self.net = Net(self.session_path, min_interval=1.0)
        self._pending_ticket_ctx: dict | None = None

    # ---------------- 登录 ----------------
    def login(self, email: str, password: str) -> dict:
        status, payload = self.net.post_json(
            LOGIN_URL,
            {"username": email, "password": password, "rememberMe": True},
            headers={"X-Requested-With": "XMLHttpRequest", "User-Agent": "GCM-iOS/5.0"},
        )

        if payload is None:
            raise AuthError(f"登录返回空响应（HTTP {status}），请检查网络或稍后重试")

        rs = payload.get("responseStatus") or {}
        rtype = rs.get("type") or ""

        if rtype == "INVALID_USERNAME_PASSWORD":
            raise AuthError("邮箱或密码不正确（国区账号请用注册邮箱，不是手机号）")
        if rtype in ("CAPTCHA_REQUIRED", "MFA_REQUIRED_CAPTCHA"):
            raise AuthError("触发了图形验证码，请先在浏览器登录一次 connect.garmin.cn 再重试")
        if rtype and not payload.get("serviceTicketId"):
            raise AuthError(f"登录失败：{rtype} {rs.get('message', '')}".strip())

        if payload.get("customerMfaInfo") and not payload.get("serviceTicketId"):
            self._pending_ticket_ctx = payload
            raise MfaRequired(payload)

        ticket = payload.get("serviceTicketId")
        if not ticket:
            raise AuthError(f"登录未返回 ticket：{json.dumps(payload, ensure_ascii=False)[:400]}")

        return self._finish_ticket(ticket)

    def submit_mfa(self, code: str) -> dict:
        if not self._pending_ticket_ctx:
            raise AuthError("没有待完成的验证码会话，请重新调用 login()")
        last: dict | None = None
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
                return self._finish_ticket(ticket)
            if status == 404:
                continue
            rs = last.get("responseStatus") or {}
            if rs.get("type"):
                raise AuthError(f"验证码校验失败：{rs.get('type')} {rs.get('message','')}".strip())
        raise AuthError(f"验证码提交未成功：{json.dumps(last or {}, ensure_ascii=False)[:300]}")

    def _finish_ticket(self, ticket: str) -> dict:
        """用 ticket 换会话 Cookie（JWT_WEB / SESSIONID）与 csrf，并验证可用。

        国区 Web 端入口是 /app/：/modern?ticket= 会 302 到 /app?ticket=，
        真正的 Set-Cookie 发生在 /app/ 这一层。
        如果带票据都换不出来，就用已拿到的 CASTGC 直接打开应用再试一次
        （等价于浏览器里"已经登录，直接访问 connect"）。
        """
        q = urllib.parse.quote(ticket, safe="")
        nav = NAV_HEADERS
        tried: list[str] = []
        html = ""
        for url in (
            f"{CONNECT}/app/?ticket={q}",
            f"{CONNECT}/modern/?ticket={q}",
            f"{CONNECT}/modern?ticket={q}",
        ):
            status, html = self.net.request("GET", url, headers=nav)
            tried.append(f"{url.split('?')[0]} → {status}")
            if self._has_session_cookie():
                break

        if not self._has_session_cookie():  # 票据没换成会话 Cookie，试试 SSO 直开
            for url in (f"{CONNECT}/app/", f"{CONNECT}/modern/"):
                status, html = self.net.request("GET", url, headers=nav)
                tried.append(f"{url} → {status}")
                if self._has_session_cookie():
                    break

        if not self._has_session_cookie():
            raise AuthError(
                "票据没有换出会话 Cookie（" + "，".join(tried) + "）。"
                f"当前 Cookie：{', '.join(self.net.cookie_names()) or '无'}"
            )

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
        return {"csrf": csrf, "profile": profile}

    def _has_session_cookie(self) -> bool:
        names = {c.name for c in self.net.jar}
        return bool(names & {"JWT_WEB", "SESSIONID", "session"})

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

    def _verify(self) -> dict | None:
        from .client import GarminCNClient, SessionExpired

        client = GarminCNClient(self.net)
        try:
            profile = client.profile()
        except SessionExpired as e:
            self.last_verify = f"数据接口拒绝访问：{e}"
            return None
        except Exception as e:
            self.last_verify = f"{type(e).__name__}: {e}"
            return None
        if not profile:
            self.last_verify = "profile 接口没有返回 displayName（可能路径变了）"
        return profile

    # ---------------- 手动导入 Cookie ----------------
    def import_cookies(self, cookie_header: str) -> dict:
        """从浏览器 DevTools 复制的 Cookie 字符串导入会话。"""
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
        # 浏览器 Cookie 里不会有 CSRF（它在页面 meta 里），必须现取
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
    def ensure_session(self) -> bool:
        """已有会话是否仍然可用。"""
        if not self.session_path.exists():
            return False
        if not self.net.get_cookie("JWT_WEB"):
            return False
        return bool(self._verify())
