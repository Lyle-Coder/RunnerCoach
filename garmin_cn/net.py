"""极简 HTTP 层：只依赖标准库，带 Cookie 会话持久化、限速与重试。"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import Cookie, CookieJar
from pathlib import Path

from .tls import https_handler

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept-Encoding": "identity",
    "Origin": "https://connect.garmin.cn",
    "Referer": "https://connect.garmin.cn/modern/",
}

# connect.garmin.cn 的 /gc-api 前面有一层网关：请求缺少 Sec-Fetch-Site 时
# 不论有没有会话，一律返回 **空 403**（会把"未登录"和"被拦截"混成一样）。
# 带上 same-origin 之后才会真正校验会话：无会话 = 空 401，有会话 = 200。
GATE_HEADERS = {
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "NK": "NT",
}


def _short(url: str, n: int = 96) -> str:
    """日志用：去掉 query 里的长票据，只留主机+路径。"""
    return url.split("?")[0][:n]


# 国区的 CSRF 令牌藏在应用页的 <meta name="csrf-token"> 里（不是 JSON、不是 _csrf 表单）。
# /gc-api 请求必须带 Connect-Csrf-Token 头，缺了就是空 403（看起来像"没权限"）。
CSRF_PATTERNS = [
    r'<meta[^>]+name=["\']csrf-token["\'][^>]*content=["\']([^"\']+)["\']',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*name=["\']csrf-token["\']',
    r'"csrfToken"\s*:\s*"([^"]+)"',
    r'csrfToken\s*[:=]\s*[\'"]([^\'"]+)[\'"]',
    r'name="_csrf"[^>]*value="([^"]+)"',
    r'name="_csrf"\s+content="([^"]+)"',
    r'content="([^"]+)"\s+name="_csrf"',
    r'"csrf"\s*:\s*"([^"]+)"',
]


def extract_csrf(html: str) -> str | None:
    for pat in CSRF_PATTERNS:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


NAV_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
}


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status} {url} :: {(body or '')[:300]}")


class Net:
    """带 Cookie 会话的请求器。

    - 请求间隔默认 >= 0.5s（0.3 在实测中也不会触发限流，0.5 留足余量）；
      一旦真的吃到 429，自动把间隔抬上去（×1.5，上限 3s），后续请求沿用
      更高的节奏，不会因为一次限流把整轮同步拖垮
    - 429 / 5xx 自动退避重试
    - 会话可落盘到 data/session.json
    - `count` 记录本实例发出的请求数，同步结果里带出来便于对账
    """

    def __init__(self, session_path: Path | None = None, min_interval: float = 0.5,
                 trace=None):
        self.jar = CookieJar()
        self.session_path = session_path
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            https_handler(),
        )
        self.min_interval = min_interval
        self._last = 0.0
        self.count = 0
        self.trace = trace
        # 上一次落盘时的 Cookie 快照。Garmin 会在请求过程中轮换 JWT_WEB，
        # 只有变化了才写盘，避免每个请求都落一次。
        self._sig: tuple | None = None
        if session_path and session_path.exists():
            try:
                self.load_session()
            except Exception:
                pass

    def cookie_names(self) -> list[str]:
        return [c.name for c in self.jar]

    # ---------- 会话 ----------
    def cookies_as_dict(self) -> dict[str, str]:
        return {c.name: c.value for c in self.jar}

    def get_cookie(self, name: str) -> str | None:
        for c in self.jar:
            if c.name == name:
                return c.value
        return None

    def drop_garmin_cookies(self) -> None:
        """清掉 garmin.cn 的旧 Cookie。

        重新登录 / 接管浏览器会话前先打扫：磁盘里那份可能带着过期的
        `JWT_WEB`、`login_attempt` 和一堆同名的旧作用域 Cookie，
        不清就会攒出两三条 `SESSION`、`__cf_bm`，请求头越带越长。
        非 garmin 的 Cookie 原样留着（同一个 jar 里可能有别的服务）。
        """
        kept = [c for c in self.jar if not (c.domain or "").endswith("garmin.cn")]
        self.jar.clear()
        for c in kept:
            self.jar.set_cookie(c)
        self._sig = self._cookie_sig()

    def set_cookie(self, name: str, value: str, domain: str = ".garmin.cn", path: str = "/"):
        self.jar.set_cookie(
            Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=True,
                domain_initial_dot=domain.startswith("."),
                path=path,
                path_specified=True,
                secure=True,
                expires=None,
                discard=False,
                comment=None,
                comment_url=None,
                rest={},
            )
        )

    def save_session(self) -> None:
        if not self.session_path:
            return
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        data = [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
            for c in self.jar
        ]
        self.session_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        try:
            self.session_path.chmod(0o600)
        except Exception:
            pass

    def load_session(self) -> None:
        if not self.session_path or not self.session_path.exists():
            return
        for item in json.loads(self.session_path.read_text(encoding="utf-8")):
            self.set_cookie(
                item["name"], item["value"], item.get("domain", ".garmin.cn"), item.get("path", "/")
            )
        self._sig = self._cookie_sig()

    def _cookie_sig(self) -> tuple:
        return tuple(sorted((c.name, c.value) for c in self.jar))

    def _save_if_changed(self) -> None:
        """Cookie 有变化就落盘。

        Garmin 的 `JWT_WEB` 会在普通请求里被服务端轮换。旧版只在「登录成功」
        和「刷新 csrf」时保存，轮换出来的新票据一直留在内存里 —— 程序一关，
        磁盘上还是那张旧票据，下次启动校验必然失效。表现就是「关掉再开、
        或者隔一会儿刷新一下，又要重新输密码」。这里按快照比对，变了就写。
        """
        if not self.session_path:
            return
        sig = self._cookie_sig()
        if sig == self._sig:
            return
        self._sig = sig
        try:
            self.save_session()
        except Exception:
            pass

    # ---------- 请求 ----------
    def _throttle(self) -> None:
        delta = self.min_interval - (time.time() - self._last)
        if delta > 0:
            time.sleep(delta)
        self._last = time.time()

    def request(
        self,
        method: str,
        url: str,
        *,
        json_body=None,
        form_body: dict | None = None,
        headers: dict | None = None,
        timeout: int = 30,
        retries: int = 3,
        raise_for_status: bool = False,
    ) -> tuple[int, str]:
        body = None
        hdrs = dict(DEFAULT_HEADERS)
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        elif form_body is not None:
            body = urllib.parse.urlencode(form_body).encode("utf-8")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        if headers:
            hdrs.update(headers)

        last_err: Exception | None = None
        try:
            for attempt in range(retries):
                self._throttle()
                self.count += 1
                req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
                try:
                    with self.opener.open(req, timeout=timeout) as resp:
                        text = resp.read().decode("utf-8", "replace")
                        if self.trace:
                            self.trace(f"    {resp.status} {method} {_short(url)}")
                        return resp.status, text
                except urllib.error.HTTPError as e:
                    text = ""
                    try:
                        text = e.read().decode("utf-8", "replace")
                    except Exception:
                        pass
                    if e.code == 429:
                        # 被限流就把节奏整体放慢，后面的请求都按新间隔走
                        self.min_interval = min(3.0, max(self.min_interval, 0.5) * 1.5)
                        if self.trace:
                            self.trace(f"    429 限流，间隔调到 {self.min_interval:.2f}s")
                    if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                        if self.trace:
                            self.trace(f"    {e.code} {method} {_short(url)}  (重试)")
                        time.sleep(2 * (attempt + 1))
                        last_err = HttpError(e.code, url, text)
                        continue
                    if self.trace:
                        self.trace(f"    {e.code} {method} {_short(url)}  len={len(text)}")
                    if raise_for_status:
                        raise HttpError(e.code, url, text) from None
                    return e.code, text
                except Exception as e:  # 网络层错误
                    last_err = e
                    if attempt < retries - 1:
                        time.sleep(2 * (attempt + 1))
                        continue
                    if raise_for_status:
                        raise
            raise last_err if last_err else RuntimeError("request failed")
        finally:
            # 成功、失败、重试都要走：服务端轮换的 Cookie 必须马上落盘
            self._save_if_changed()

    def get_json(self, url: str, *, headers: dict | None = None, retries: int = 3):
        status, text = self.request("GET", url, headers=headers, retries=retries)
        if status == 200 and text.strip():
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return None
        return None

    def post_json(self, url: str, payload, *, headers: dict | None = None):
        status, text = self.request("POST", url, json_body=payload, headers=headers)
        try:
            return status, json.loads(text) if text.strip() else None
        except json.JSONDecodeError:
            return status, None
