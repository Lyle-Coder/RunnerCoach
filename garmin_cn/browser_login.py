"""浏览器托管登录：让人在真浏览器里登录一次，程序只负责把 Cookie 收回来。

为什么必须走浏览器（2026-11 实测）：

1. connect.garmin.cn 的网页端已经换成新前端（`/_astro/signin…js`），
   登录页改跳新门户 SSO：
   `https://sso.garmin.cn/portal/sso/GarminConnect/sign-in?clientId=GarminConnect`
2. 这个门户页对普通 HTTP 客户端直接返回 `403 cf-mitigated: challenge`
   （Cloudflare 托管验证），urllib 过不去——不是加个 UA 就能骗过去的。
3. 旧的移动通道 `POST /mobile/api/login?clientId=GCM_IOS_DARK` 仍然返回
   `SUCCESSFUL` 并给出 ticket，但网页端已经不认这类票据：
   `GET /app/?ticket=…` 一律 `302 → /signin/`，并顺手用 `Max-Age=0`
   把 `JWT_WEB / session / SESSIONID` 全删掉。换 clientId、换 service 都一样。

所以密码只能由人在浏览器里输一次；验证码、邮箱码、Cloudflare 全交给真浏览器。
程序这边只做两件事：盯住 `JWT_WEB` 出现，然后把 Cookie 交给 auth 落盘。

依赖 playwright（可选）：装了就用；没装就提示怎么装，并告诉用户还能用
`import-cookies` 手动粘 Cookie。浏览器优先用本机已装的 Chrome / Edge
（`channel`），不额外下载 Chromium —— 少 150MB，也少一个版本坑。
"""
from __future__ import annotations

import importlib.util
import urllib.parse
from pathlib import Path

SIGNIN_URL = (
    "https://sso.garmin.cn/portal/sso/GarminConnect/sign-in?clientId=GarminConnect"
    "&service=" + urllib.parse.quote("https://connect.garmin.cn/app", safe="")
)

# 登录成功后必须出现的 Cookie。国区网页会话就是这两枚之一。
SESSION_COOKIES = ("JWT_WEB", "SESSIONID", "session")

INSTALL_HINT = (
    "× 没装 playwright，浏览器登录用不了。装它（一次性，之后不用再装）：\n"
    "    py -3 -m pip install playwright\n"
    "  浏览器的内核会用本机已装的 Chrome / Edge，不需要再 playwright install。\n"
    "  不想装也行：浏览器里登录 connect.garmin.cn，复制 Cookie 后跑 run.bat import-cookies"
)


class BrowserLoginError(RuntimeError):
    pass


def available() -> bool:
    return importlib.util.find_spec("playwright") is not None


def garmin_cookies(items) -> list[dict]:
    """只留 garmin.cn 的 Cookie，并去重（同名同域只留最后一条）。

    浏览器会同时给出 connect.garmin.cn / sso.garmin.cn / .garmin.cn 几套作用域，
    落盘时统一挂到 `.garmin.cn`，这样 connect 与 sso 都会带上。
    """
    out: dict[tuple[str, str], dict] = {}
    for it in items or []:
        name = (it.get("name") or "").strip()
        value = it.get("value") or ""
        domain = (it.get("domain") or "").strip()
        if not name or not value:
            continue
        if not domain.endswith("garmin.cn"):
            continue
        out[(name, domain)] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": it.get("path") or "/",
        }
    return list(out.values())


def has_session(items) -> bool:
    return any((it.get("name") in SESSION_COOKIES and it.get("value")) for it in items or [])


def _launch(pw, profile: Path, headless: bool):
    """按 chrome → msedge → 自带 chromium 的顺序试，谁能起用谁。"""
    kwargs = dict(
        user_data_dir=str(profile),
        headless=headless,
        args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
        no_viewport=True,
    )
    errors = []
    for channel in ("chrome", "msedge", None):
        try:
            if channel:
                return pw.chromium.launch_persistent_context(channel=channel, **kwargs)
            return pw.chromium.launch_persistent_context(**kwargs)
        except Exception as e:  # 没装这个浏览器 / 内核不匹配
            errors.append(f"{channel or 'chromium'}: {type(e).__name__}")
    raise BrowserLoginError(
        "起不来浏览器（" + "；".join(errors) + "）。装个 Chrome 或 Edge 再试。"
    )


def _try_fill(page, email: str | None, password: str | None, log=print) -> None:
    """能自动填就填、能自动提交就提交（省一次手输），剩下的验证码人自己点。

    填不全（比如页面结构变了）就安静跳过——用户还能手输，不值得为此报错。
    """
    if not email and not password:
        return
    filled_mail = filled_pwd = False
    if email:
        for sel in ('input[type="email"]', 'input[name="username"]', "#username", 'input[name="email"]'):
            try:
                box = page.locator(sel).first
                if box.count():
                    box.fill(email, timeout=1500)
                    filled_mail = True
                    break
            except Exception:
                continue
    if password:
        try:
            box = page.locator('input[type="password"]').first
            if box.count():
                box.fill(password, timeout=1500)
                filled_pwd = True
        except Exception:
            pass
    if not (filled_mail and filled_pwd):
        return
    try:  # 「记住我」勾上，下次启动新窗口也还是登录态
        page.get_by_label("记住我").check(timeout=1000)
    except Exception:
        pass
    try:
        btn = page.locator('button[type="submit"]').first
        if btn.count():
            btn.click(timeout=2000)
        else:
            page.keyboard.press("Enter")
        log("· 已自动填好账号密码并提交；要是弹出验证码，在窗口里点一下就行")
    except Exception:
        log("· 账号密码已填好，点一下「登录」继续")


def login(
    home: Path,
    *,
    email: str | None = None,
    password: str | None = None,
    timeout: int = 300,
    headless: bool = False,
    log=print,
) -> list[dict]:
    """打开浏览器等用户登录，返回 garmin.cn 的 Cookie 列表。

    等待期间每秒看一次 Cookie；`JWT_WEB` 一出现就收工。
    超时或用户关掉窗口都抛 BrowserLoginError，由调用方给下一步建议。
    """
    if not available():
        raise BrowserLoginError(INSTALL_HINT)

    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    home = Path(home)
    profile = home / "data" / "browser_profile"
    profile.mkdir(parents=True, exist_ok=True)

    log("→ ① 打开浏览器…（第一次要手输密码，之后这个目录会记住登录）")
    with sync_playwright() as pw:
        ctx = _launch(pw, profile, headless)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                log(f"· 打开登录页有点慢（{type(e).__name__}），不影响，继续等")
            try:
                # 门户页是前端渲染的，domcontentloaded 之后表单还要一会儿才挂上
                page.wait_for_selector('input[type="email"], input[type="password"]', timeout=20000)
            except Exception:
                log("· 没等到登录表单，可能在过 Cloudflare 验证；窗口里看到就继续填")
            _try_fill(page, email, password, log=log)

            log(f"→ ② 请在浏览器窗口里完成登录（验证码 / 邮箱验证码都行），最多等 {timeout} 秒…")
            waited = 0
            while waited < timeout:
                try:
                    if not ctx.pages:
                        raise BrowserLoginError("浏览器窗口被关掉了")
                    fresh = ctx.cookies("https://connect.garmin.cn")
                except PlaywrightError:
                    raise BrowserLoginError("浏览器窗口被关掉了") from None
                if has_session(fresh):
                    log(f"√ 拿到会话 Cookie（等了 {waited}s）")
                    return garmin_cookies(ctx.cookies())
                if waited and waited % 15 == 0:
                    where = urllib.parse.urlsplit(page.url)
                    log(f"  …还在等（{waited}s / {timeout}s，当前停在 {where.path[:40]}）")
                page.wait_for_timeout(1000)
                waited += 1
            raise BrowserLoginError(
                f"等了 {timeout} 秒还没登录成功。可以重跑并加长等待："
                "run.bat login --timeout 600；或者确认账号密码在浏览器里能登上。"
            )
        finally:
            try:
                ctx.close()
            except Exception:
                pass
