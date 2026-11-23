# -*- coding: utf-8 -*-
"""网页版（iPhone / 浏览器）的账号、数据源与页面路由。

边界
----
- **Android 仍走 APK**，本地同步；这里只服务浏览器用户。
- 每个用户一个 `GARMIN_HOME=/opt/runnercoach/users/<uid>`，渲染/同步用短命
  子进程，避免 `garmin_cn.appapi` 的模块级 HOME 在多用户间串数据。
- 账密 AES-GCM 加密，主密钥 `/opt/runnercoach/data/web.key`（0600）。
  不写日志、不回显；用户可在「我的」里删除。
- 页面 HTML 复用 `garmin_cn` 的渲染函数，外面套一层移动端 PWA 壳。

不作为
------
不做视频/长图渲染、不做实时并发、不把 `/manage` 暴露给普通用户。
"""
from __future__ import annotations

import base64
import html as H
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
WEB_DIR = APP_DIR / "web"
USERS_DIR = Path(os.environ.get("RC_WEB_USERS", str(APP_DIR / "users")))
KEY_PATH = Path(os.environ.get("RC_WEB_KEY", str(APP_DIR / "data" / "web.key")))
COOKIE = "rc_tok"
COOKIE_MAX = 30 * 86400
WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()


# ---------------------------------------------------------------- 基础设施
def _A():
    import app as A

    return A


def _M():
    import manage as M

    return M


def _Q():
    return urllib.parse.parse_qs


def init() -> None:
    """建表、建目录、把上次进程崩掉时卡在 running 的任务放回 pending。"""
    USERS_DIR.mkdir(parents=True, exist_ok=True)
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _A().db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS web_creds(
                user_id INTEGER PRIMARY KEY,
                source TEXT NOT NULL,
                email TEXT NOT NULL,
                nonce TEXT NOT NULL,
                ct TEXT NOT NULL,
                updated INTEGER NOT NULL
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS web_jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                created INTEGER NOT NULL,
                started INTEGER NOT NULL DEFAULT 0,
                finished INTEGER NOT NULL DEFAULT 0
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS web_jobs_uid ON web_jobs(user_id,status)")
        c.execute("UPDATE web_jobs SET status='pending', started=0 WHERE status='running'")


def _key() -> bytes:
    """主密钥：首次启动生成 32 字节，0600。不打印、不进仓库。"""
    try:
        data = KEY_PATH.read_bytes()
        if len(data) == 32:
            return data
    except OSError:
        pass
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = os.urandom(32)
    fd = os.open(str(KEY_PATH), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    try:
        os.chmod(KEY_PATH, 0o600)
    except OSError:
        pass
    return data


def _enc(text: str) -> tuple[str, str]:
    """AES-GCM（服务器）。Windows 本机没有 cryptography 时退回 DPAPI，只为开发联调。"""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ModuleNotFoundError:
        from garmin_cn import secretstore

        if not secretstore.available():
            raise RuntimeError("没有可用的加密后端（cryptography/DPAPI）")
        return "dpapi", secretstore.protect(text)
    nonce = os.urandom(12)
    ct = AESGCM(_key()).encrypt(nonce, text.encode("utf-8"), b"")
    return (base64.b64encode(nonce).decode("ascii"),
            base64.b64encode(ct).decode("ascii"))


def _dec(nonce_b64: str, ct_b64: str) -> str:
    if nonce_b64 == "dpapi":
        from garmin_cn import secretstore

        return secretstore.unprotect(ct_b64)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = base64.b64decode(nonce_b64)
    ct = base64.b64decode(ct_b64)
    return AESGCM(_key()).decrypt(nonce, ct, b"").decode("utf-8")


def _home(uid: int) -> Path:
    p = USERS_DIR / str(int(uid))
    (p / "data").mkdir(parents=True, exist_ok=True)
    return p


def _creds(uid: int) -> sqlite3.Row | None:
    with _A().db() as c:
        return c.execute("SELECT * FROM web_creds WHERE user_id=?", (int(uid),)).fetchone()


def _save_creds(uid: int, source: str, email: str, password: str) -> None:
    nonce, ct = _enc(password)
    with _A().db() as c:
        c.execute("""
            INSERT INTO web_creds(user_id,source,email,nonce,ct,updated)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                source=excluded.source, email=excluded.email,
                nonce=excluded.nonce, ct=excluded.ct, updated=excluded.updated
        """, (int(uid), source, email, nonce, ct, int(time.time())))


def _del_creds(uid: int) -> None:
    with _A().db() as c:
        c.execute("DELETE FROM web_creds WHERE user_id=?", (int(uid),))
        c.execute("DELETE FROM web_jobs WHERE user_id=? AND status IN ('pending','running')",
                  (int(uid),))
    try:
        (_home(uid) / "data" / "session.json").unlink()
    except OSError:
        pass


def _me(handler):
    try:
        return _M().me_of(handler.headers.get("Cookie"))
    except Exception:      # noqa: BLE001
        return None


def _set_cookie(token: str) -> str:
    bits = [f"{COOKIE}={urllib.parse.quote(token)}", "Path=/",
            "HttpOnly", "SameSite=Lax", f"Max-Age={COOKIE_MAX}"]
    if _A().TLS_CERT:
        bits.append("Secure")
    return "; ".join(bits)


def _send_html(handler, body: str | bytes, code: int = 200,
               headers: list[tuple[str, str]] | None = None) -> None:
    raw = body.encode("utf-8") if isinstance(body, str) else body
    handler.send_response(code)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Connection", "close")
    for k, v in (headers or []):
        handler.send_header(k, v)
    handler.end_headers()
    try:
        handler.wfile.write(raw)
    except (BrokenPipeError, ConnectionResetError):
        pass
    handler.close_connection = True


def _redirect(handler, location: str, code: int = 303,
              headers: list[tuple[str, str]] | None = None) -> None:
    handler.send_response(code)
    handler.send_header("Location", location)
    handler.send_header("Content-Length", "0")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Connection", "close")
    for k, v in (headers or []):
        handler.send_header(k, v)
    handler.end_headers()
    handler.close_connection = True


def _ip(handler) -> str:
    return (handler.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or handler.client_address[0])


# ---------------------------------------------------------------- 页面壳
_NAV = (
    ("/app", "今日"), ("/app/history", "历史"),
    ("/w", "小组"), ("/app/me", "我的"),
)

_SHELL_CSS = """
.webnav{position:fixed;left:0;right:0;bottom:0;z-index:60;display:flex;
  background:rgba(8,13,21,.96);border-top:1px solid var(--line,rgba(255,255,255,.08));
  padding:6px 4px calc(6px + env(safe-area-inset-bottom));
  backdrop-filter:blur(12px)}
.webnav a{flex:1;min-height:44px;display:flex;align-items:center;justify-content:center;
  text-decoration:none;color:var(--ink-3,#7A8DA4);font-size:12px;font-weight:600;
  border-radius:12px}
.webnav a.on{color:var(--brand-ink,#9CC6FF);background:var(--brand-soft,rgba(59,130,246,.12))}
body{padding-bottom:calc(64px + env(safe-area-inset-bottom))!important}
.webbanner{margin:0 0 12px;padding:10px 12px;border-radius:12px;font-size:12px;
  background:var(--brand-soft,rgba(59,130,246,.12));color:var(--brand-ink,#9CC6FF);
  border:1px solid var(--line-brand,rgba(96,165,250,.22))}
.webbanner.warn{background:var(--warn-soft,rgba(245,165,36,.10));
  color:var(--warn,#F5A524);border-color:rgba(245,165,36,.28)}
.webbanner a{color:inherit;text-decoration:underline}
.webform{display:block}
.webform input[type=text],.webform input[type=password],.webform input[type=email],
.webform select{width:100%;min-height:44px;margin:6px 0 12px;padding:0 12px;
  border-radius:12px;border:1px solid var(--line,rgba(255,255,255,.12));
  background:var(--surface-2,#111B29);color:var(--ink,#E9F0F8);font-size:14px}
.webform label{font-size:12px;color:var(--ink-3,#7A8DA4)}
.webform textarea{width:100%;min-height:96px;margin:6px 0 12px;padding:10px 12px;
  border-radius:12px;border:1px solid var(--line,rgba(255,255,255,.12));
  background:var(--surface-2,#111B29);color:var(--ink,#E9F0F8);font-size:12px;
  line-height:1.6;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  resize:vertical}
.webform .webrow{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}
.webform .webrow .btn{flex:1}
.webk{font-size:11px;color:var(--ink-3,#7A8DA4);line-height:1.7;margin-top:10px}
"""


def _webify(html: str) -> str:
    """把 App 里的「本机」文案换成网页版的真实口径。

    直接复用 `garmin_cn` 的页面会带出「下拉同步」「数据只保存在本机」这类
    只对 Android 成立的话；网页版是服务器同步，照抄会自相矛盾。
    """
    repl = (
        ("下拉同步", "去「我的」同步"),
        ("数据在本机计算，离线也能查看，轨迹不会上传。",
         "数据由服务器代为同步，只对你本人可见。"),
        ("数据只保存在本机，不上传任何第三方。",
         "数据保存在你使用的服务器上，只对你本人可见。"),
        ("把设备里的记录整理成每天看得懂的一张图。",
         "把设备里的记录整理成每天看得懂的一张图。"),
    )
    for a, b in repl:
        html = html.replace(a, b)
    # App 的链接指向 /history、/view、/plan、/event；网页版要收进 /app 下。
    links = (
        ('href="/history?', 'href="/app/history?'),
        ('href="/history"', 'href="/app/history"'),
        ('href="/view?', 'href="/app/view?'),
        ('action="/view"', 'action="/app/view"'),
        ('href="/plan?', 'href="/app/plan?'),
        ('href="/plan"', 'href="/app/plan"'),
        ('href="/event?', 'href="/app/event?'),
        ('href="/event"', 'href="/app/event"'),
        ('href="/me"', 'href="/app/me"'),
        ('href="/privacy"', 'href="/app/privacy"'),
        ('href="/sports"', 'href="/app/me"'),
        ('href="/files"', 'href="/app/me"'),
        ('href="/ai"', 'href="/app/me"'),
        ('href="/groups', 'href="/w'),
        ('href="/"', 'href="/app"'),
    )
    for a, b in links:
        html = html.replace(a, b)
    return html


def _shell(html: str, active: str, banner: str = "") -> str:
    html = _webify(html)
    if banner and "<main>" in html:
        html = html.replace("<main>", "<main>" + banner, 1)
    elif banner:
        html = html.replace("</body>", banner + "</body>", 1)
    head = ('<meta name="apple-mobile-web-app-capable" content="yes">'
            '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
            '<link rel="manifest" href="/app/manifest.webmanifest">'
            f"<style>{_SHELL_CSS}</style>")
    if "</head>" in html:
        html = html.replace("</head>", head + "</head>", 1)
    else:
        html = head + html
    nav = '<nav class="webnav">' + "".join(
        '<a href="%s"%s>%s</a>' % (href, ' class="on"' if href == active else "", label)
        for href, label in _NAV) + "</nav>"
    sw = ("<script>if('serviceWorker' in navigator){"
          "navigator.serviceWorker.register('/app/sw.js').catch(function(){});}"
          "document.addEventListener('click',function(e){var n=e.target;"
          "while(n&&n!==document){if(n.getAttribute){"
          "var aid=n.getAttribute('data-aid');"
          "if(aid){location.href='/app/act?aid='+encodeURIComponent(aid);return;}}"
          "n=n.parentNode;}},false);</script>")
    html = html.replace("</body>", nav + sw + "</body>", 1)
    return html


def _U():
    from garmin_cn import ui as U

    return U


def _error_page(msg: str, code: int = 200) -> str:
    U = _U()
    body = ('<div class="card"><div class="empty">' + H.escape(msg) + "</div>"
            '<div class="pager"><a class="btn" href="/app">返回今日</a></div></div>')
    return _shell(U.page("APP · 出错", "页面打不开", "", body, ""), "")


# ---------------------------------------------------------------- 登录
def _login_page(handler, qs: dict) -> None:
    U = _U()
    msg = (qs.get("msg") or [""])[0]
    email = (qs.get("account") or [""])[0]
    dev = (qs.get("dev") or [""])[0]
    note = f'<div class="webbanner">{H.escape(msg)}</div>' if msg else ""
    if dev:
        note += f'<div class="webbanner warn">开发模式验证码：{H.escape(dev)}</div>'
    body = (
        note
        + '<div class="card"><form class="webform" method="post" action="/app/login">'
        '<label>邮箱</label>'
        f'<input type="email" name="account" value="{H.escape(email)}" '
        'autocomplete="username" placeholder="you@example.com" required>'
        '<label>验证码</label>'
        '<input type="text" name="code" inputmode="numeric" maxlength="6" '
        'autocomplete="one-time-code" placeholder="6 位数字">'
        '<div class="webrow">'
        '<button class="btn" type="submit" name="action" value="send" '
        'formnovalidate>发送验证码</button>'
        '<button class="btn primary" type="submit" name="action" value="login">登录</button>'
        '</div></form>'
        '<div class="webk">首次登录自动注册。登录后可在「我的」里连接佳明账号，'
        '服务器会为你在后台同步。账密用 AES-GCM 加密，可随时删除。</div></div>'
    )
    html = U.page("APP · 登录", "登录", "用邮箱验证码登录网页版", body,
                  U.chip("网页版") + U.chip("数据在服务器"),
                  foot="网页版只保存账号与同步数据；不售卖、不投放广告。")
    _send_html(handler, _shell(html, ""))


def _login_post(handler, form: dict) -> None:
    A = _A()
    action = (form.get("action") or [""])[0]
    account = (form.get("account") or [""])[0].strip()
    code = (form.get("code") or [""])[0].strip()
    if not account:
        return _redirect(handler, "/app/login?msg=" + urllib.parse.quote("先填邮箱"))
    if action == "send":
        try:
            r = A.api_sms_send({"account": account}, _ip(handler))
            q = {"account": account, "msg": "验证码已发送"}
            if r.get("dev_code"):
                q["dev"] = r["dev_code"]
            return _redirect(handler, "/app/login?" + urllib.parse.urlencode(q))
        except A.Gate as e:
            return _redirect(handler, "/app/login?account=" + urllib.parse.quote(account)
                             + "&msg=" + urllib.parse.quote(e.message))
        except Exception as e:      # noqa: BLE001
            return _redirect(handler, "/app/login?account=" + urllib.parse.quote(account)
                             + "&msg=" + urllib.parse.quote(f"发送失败：{type(e).__name__}"))
    if not code:
        return _redirect(handler, "/app/login?account=" + urllib.parse.quote(account)
                         + "&msg=" + urllib.parse.quote("填验证码"))
    try:
        r = A.api_auth({"account": account, "code": code,
                        "device_name": "Web"}, _ip(handler))
    except A.Gate as e:
        return _redirect(handler, "/app/login?account=" + urllib.parse.quote(account)
                         + "&msg=" + urllib.parse.quote(e.message))
    except Exception as e:      # noqa: BLE001
        return _redirect(handler, "/app/login?account=" + urllib.parse.quote(account)
                         + "&msg=" + urllib.parse.quote(f"登录失败：{type(e).__name__}"))
    tok = str(r.get("token") or "")
    if not tok:
        return _redirect(handler, "/app/login?msg=" + urllib.parse.quote("登录失败"))
    try:
        uid = int(r.get("user_id") or 0)
    except (TypeError, ValueError):
        uid = 0
    # api_auth 没回 user_id：用令牌换一次
    if not uid:
        try:
            row = A._auth({"Authorization": "Bearer " + tok})
            uid = int(row["user_id"])
        except Exception:      # noqa: BLE001
            uid = 0
    if uid:
        _home(uid)
    _redirect(handler, "/app", headers=[("Set-Cookie", _set_cookie(tok))])


# ---------------------------------------------------------------- 我的
def _stats(uid: int) -> dict:
    p = _home(uid) / "data" / "garmin.db"
    out = {"days": 0, "acts": 0, "last": "", "exists": p.is_file()}
    if not p.is_file():
        return out
    try:
        c = sqlite3.connect(str(p), timeout=5)
        c.row_factory = sqlite3.Row
        try:
            out["days"] = int(c.execute("SELECT COUNT(*) FROM daily").fetchone()[0])
            out["acts"] = int(c.execute("SELECT COUNT(*) FROM activities").fetchone()[0])
            r = c.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone()
            out["last"] = str(r[0] if r else "")
        finally:
            c.close()
    except sqlite3.Error:
        pass
    return out


def _job_latest(uid: int) -> dict:
    try:
        with _A().db() as c:
            r = c.execute("SELECT * FROM web_jobs WHERE user_id=? ORDER BY id DESC LIMIT 1",
                          (int(uid),)).fetchone()
            return dict(r) if r else {}
    except sqlite3.Error:
        return {}


def _banner(uid: int, msg: str = "") -> str:
    if msg:
        return f'<div class="webbanner">{H.escape(msg)}</div>'
    j = _job_latest(uid)
    if j.get("status") == "pending":
        return '<div class="webbanner">同步排队中…</div>'
    if j.get("status") == "running":
        return '<div class="webbanner">正在同步，页面会在完成后更新。'
        '</div>'
    if j.get("status") == "error":
        return ('<div class="webbanner warn">上次同步失败：'
                + H.escape(str(j.get("message") or "未知原因"))
                + '</div>')
    if j.get("status") == "done" and int(j.get("finished") or 0) > time.time() - 180:
        return ('<div class="webbanner">同步完成：'
                + H.escape(str(j.get("message") or ""))
                + '</div>')
    return ""


def _sync_on_load(handler, uid: int) -> str:
    j = _job_latest(uid)
    if j.get("status") in ("pending", "running"):
        return "<script>setTimeout(function(){location.reload()},6000);</script>"
    return ""


def _me_page(handler, me, qs: dict) -> None:
    U = _U()
    uid = int(me["user_id"])
    st = _stats(uid)
    cr = _creds(uid)
    msg = (qs.get("msg") or [""])[0]
    note = f'<div class="webbanner">{H.escape(msg)}</div>' if msg else ""
    sess_ok = (_home(uid) / "data" / "session.json").exists()
    if cr or sess_ok:
        if cr:
            src = {"garmin": "佳明 Garmin", "coros": "高驰 COROS"}.get(
                str(cr["source"]), str(cr["source"]))
            src_html = (f'<div class="kvline"><span>数据源</span><b>{H.escape(src)}</b></div>'
                        f'<div class="kvline"><span>账号</span><b>{H.escape(str(cr["email"]))}</b></div>')
        else:
            src_html = ('<div class="kvline"><span>数据源</span><b>佳明 Garmin</b></div>')
        if sess_ok:
            # 带时间：佳明 JWT 几小时就过期，用户看到「12:55 导入」才知道要不要
            # 去 App 里再同步一次，比只写「已导入」有用。
            try:
                import datetime as _dt

                _mt = (_home(uid) / "data" / "session.json").stat().st_mtime
                _when = _dt.datetime.fromtimestamp(_mt).strftime("%m-%d %H:%M")
            except OSError:
                _when = ""
            src_html += ('<div class="kvline"><span>登录态</span><b>已导入'
                         + (f"（{_when}）" if _when else "") + '</b></div>')
        actions = (
            '<form method="post" action="/app/sync" style="display:inline">'
            '<button class="btn primary" type="submit">立即同步</button></form> '
            '<a class="btn" href="/app/connect">更换账号 / 登录态</a> '
            '<form method="post" action="/app/forget" style="display:inline" '
            'onsubmit="return confirm(\'删除数据源后需要重新登录佳明，确定？\')">'
            '<button class="btn danger" type="submit">删除数据源</button></form>'
        )
    else:
        src_html = ('<div class="kvline"><span>数据源</span><b>未连接</b></div>'
                    '<div class="kvline"><span>登录态</span><b>没有</b></div>')
        actions = '<a class="btn primary" href="/app/connect">连接佳明账号</a>'
    stats = (f'<div class="kvline"><span>每日数据</span><b>{st["days"]} 天</b></div>'
             f'<div class="kvline"><span>活动</span><b>{st["acts"]} 条</b></div>'
             f'<div class="kvline"><span>上次同步</span><b>{H.escape(st["last"] or "—")}</b></div>')
    body = (note
            + U.sec("账号与数据源", '<div class="card">' + src_html
                    + '<div class="pager">' + actions + '</div></div>')
            + U.sec("训练数据", '<div class="card">' + stats + '</div>')
            + U.sec("小组", '<div class="card"><p class="note">'
                    '网页端的小组是只读页（榜单、赛事、远征）。建赛事、审核仍在 App。'
                    '</p><div class="pager"><a class="btn" href="/w">打开小组页</a></div></div>')
            + U.sec("关于", '<div class="card"><p class="note">'
                    '网页版由服务器代为同步，数据保存在这台服务器上。'
                    '删除数据源会移除加密账密与佳明会话，训练数据保留在账号目录。'
                    f'</p><div class="pager"><a class="btn" href="/app/privacy">隐私说明</a>'
                    '<form method="post" action="/app/logout" style="display:inline">'
                    '<button class="btn" type="submit">退出登录</button></form>'
                    '</div></div>'))
    html = U.page("APP · 我的", "我的", "账号、数据源与隐私", body,
                  U.chip("网页版") + U.chip(st["last"] or "未同步"),
                  foot="账号与训练数据保存在你使用的服务器上；可随时删除。")
    _send_html(handler, _shell(html, "/app/me"))


def _act_page(handler, me, qs: dict) -> None:
    """单次活动图。actcard 出的是独立整页（1080 宽），不能再套网页外壳。

    App 里靠原生桥 `GC.activity(aid)` 打开这张图；网页版没有桥，所以由
    `_shell` 注入的点击脚本把带 `data-aid` 的行转到 `/app/act?aid=`。

    手机上不能直接把 1080 宽的整页塞进 390 的视口（会横向溢出、右侧被切）。
    `raw=1` 出原始卡片，外层用 iframe 按 `1080 → 视口宽` 等比缩放，并把
    iframe 的滚动高度回传，保证长卡片能滚到底。
    """
    uid = int(me["user_id"])
    aid = (qs.get("aid") or [""])[0].strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,40}", aid or ""):
        return _send_html(handler, _error_page("活动编号不对", 400), 400)
    raw = (qs.get("raw") or [""])[0] == "1"
    html = _render(uid, "act", {"aid": [aid],
                                "privacy": qs.get("privacy") or ["none"]})
    if "页面渲染失败" in html or "页面渲染超时" in html:
        # 子进程失败时 _render 已经给了一整页说明，别再包一层 iframe：
        # 包了之后 390 宽的视口里塞一张 1080 的错误页，字会小到看不见。
        if "库里没有这条活动" in html or "没拿到活动明细" in html:
            return _send_html(handler, _error_page(
                "这条活动的明细还没取到：确认登录态有效（用 App 同步一次），再点「立即同步」。"))
        return _send_html(handler, html)
    if "<html" not in html[:200].lower():
        return _send_html(handler, html)      # 已经是错误页
    # 底图在 App 里由本机服务 `/basemap/<png>` 提供；网页版换成 /app/basemap/
    html = html.replace('src="/basemap/', 'src="/app/basemap/')
    html = html.replace("src='/basemap/", "src='/app/basemap/")

    if raw:
        # 卡片自身用 meta viewport width=1080 排版，父页用 transform 缩放。
        # 这里只负责把真实高度报给父页（卡片高度随数据条数变化）。
        report = ("<script>(function(){function P(){try{var w=Math.max("
                  "document.documentElement.scrollWidth,document.body.scrollWidth,1080);"
                  "parent.postMessage({h:document.documentElement.scrollHeight,w:w},'*');}"
                  "catch(e){}}window.addEventListener('load',P);setTimeout(P,200);"
                  "setTimeout(P,800);})();</script>")
        if "</body>" in html:
            html = html.replace("</body>", report + "</body>", 1)
        else:
            html += report
        return _send_html(handler, html)

    page = _ACT_WRAPPER.replace("__AID__", urllib.parse.quote(aid))
    _send_html(handler, page)


_ACT_WRAPPER = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0F1319">
<title>活动图 · RunnerCoach</title>
<style>
html,body{margin:0;background:#0F1319}
#box{position:relative;overflow:hidden;width:100%}
#box iframe{position:absolute;left:0;top:0;width:1080px;height:2250px;border:0;
  transform-origin:top left}
#back{position:fixed;left:12px;bottom:14px;z-index:9;display:inline-flex;
  align-items:center;gap:6px;padding:9px 14px;border-radius:999px;
  background:rgba(10,16,26,.86);border:1px solid rgba(255,255,255,.18);
  color:#E9F0F8;font:13px/1 -apple-system,system-ui,sans-serif;
  text-decoration:none;-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px)}
</style></head><body>
<div id="box"></div>
<a id="back" href="/app/history">‹ 返回</a>
<script>
(function(){
  var W = 1080, box = document.getElementById('box');
  var f = document.createElement('iframe');
  f.setAttribute('scrolling', 'no');
  f.setAttribute('referrerpolicy', 'same-origin');
  f.src = '/app/act?aid=__AID__&raw=1';
  box.appendChild(f);
  function fit(){
    // 卡片版心可能比 1080 略宽（.page 的左右留白不在 box-sizing 里），
    // 用子页上报的真实宽度定画布，否则右侧会被裁掉一条。
    var cw = parseFloat(f.getAttribute('data-w') || '0') || W;
    f.style.width = cw + 'px';
    var s = Math.min(1, (document.documentElement.clientWidth || W) / cw);
    f.style.transform = 'scale(' + s + ')';
    var h = parseFloat(f.getAttribute('data-h') || '0') || 2250;
    box.style.height = Math.ceil(h * s) + 'px';
  }
  window.addEventListener('resize', fit);
  window.addEventListener('message', function(e){
    var d = (e && e.data) || {};
    var h = parseFloat(d.h), w = parseFloat(d.w);
    if (h > 100) { f.setAttribute('data-h', String(h)); }
    if (w > 100) { f.setAttribute('data-w', String(w)); }
    if (h > 100 || w > 100) { fit(); }
  });
  fit();
})();
</script></body></html>"""


def _basemap(handler, me, name: str) -> None:
    """活动图用的底图切片：只从该用户自己的缓存目录取，文件名白名单校验。"""
    uid = int(me["user_id"])
    name = urllib.parse.unquote(name or "")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}\.png", name):
        return _send_html(handler, _error_page("没有这张图", 404), 404)
    path = _home(uid) / "data" / "basemaps" / name
    if not path.is_file():
        return _send_html(handler, _error_page("没有这张图", 404), 404)
    raw = path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", "image/png")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "private, max-age=86400")
    handler.send_header("Connection", "close")
    handler.end_headers()
    try:
        handler.wfile.write(raw)
    except (BrokenPipeError, ConnectionResetError):
        pass


def _connect_page(handler, me, qs: dict) -> None:
    U = _U()
    uid = int(me["user_id"])
    cr = _creds(uid)
    msg = (qs.get("msg") or [""])[0]
    note = f'<div class="webbanner">{H.escape(msg)}</div>' if msg else ""
    existing = ""
    if cr:
        existing = ('<div class="webbanner warn">已连接 '
                    + H.escape(str(cr["email"]))
                    + '，保存新账号会覆盖。</div>')
    body = (
        note + existing
        + '<div class="card"><h3 style="margin:0 0 6px">佳明：用 App 同步登录态</h3>'
        '<p style="margin:0 0 8px;color:var(--muted);font-size:13px;line-height:1.7">'
        '佳明登录页对机房网络有人机校验，服务器直接登录会被挡。'
        '用安卓 App 登录佳明后，App 会把登录态自动传上来；'
        '桌面浏览器也可以在下面粘贴一份。</p>'
        '<p style="margin:0 0 8px;color:var(--muted);font-size:13px;line-height:1.7">'
        '<b>网页和 App 必须用同一个账号登录（邮箱一致）</b>，'
        'App 才会把登录态传到这个账号下；两个邮箱算两个账号，互相看不到。</p>'
        '<form class="webform" method="post" action="/app/import">'
        '<label>登录态（session.json 内容）</label>'
        '<textarea name="session" rows="4" placeholder="[{\"name\":\"JWT_WEB\",...}]"></textarea>'
        '<div class="webrow"><button class="btn" type="submit">导入登录态</button></div>'
        '</form></div>'
        + '<div class="card"><h3 style="margin:0 0 6px">高驰：账号密码</h3>'
        '<form class="webform" method="post" action="/app/connect">'
        '<input type="hidden" name="source" value="coros">'
        '<label>账号（邮箱）</label>'
        '<input type="email" name="email" autocomplete="username" '
        f'value="{H.escape(str(cr["email"]) if cr and cr["source"] == "coros" else "")}">'
        '<label>密码</label>'
        '<input type="password" name="password" autocomplete="current-password" '
        'placeholder="AES-GCM 加密后保存">'
        '<label style="display:flex;gap:8px;align-items:flex-start;margin:6px 0 4px">'
        '<input type="checkbox" name="consent" value="1" style="width:18px;height:18px;margin-top:2px">'
        '<span>我同意服务器保存加密后的账号密码，并每天替我在后台同步训练数据；'
        '可随时在「我的」删除。</span></label>'
        '<div class="webrow"><button class="btn primary" type="submit">保存并同步</button>'
        '<a class="btn" href="/app">取消</a></div></form>'
        '<div class="webk">只保存加密后的密码，不出现在日志、页面或接口返回里。</div></div>'
    )
    html = U.page("APP · 连接数据源", "连接数据源", "服务器代你同步", body,
                  U.chip("AES-GCM") + U.chip("可删除"),
                  foot="账密只用于同步，不用于其他用途；删除后立即失效。")
    _send_html(handler, _shell(html, "/app/me"))


def _import_post(handler, form: dict, me) -> None:
    """导入佳明登录态（session.json 内容）。

    佳明登录页对机房 IP 有 Cloudflare 挑战，服务器登录不了；手机 App 能过，
    所以登录态由 App 回传，桌面用户也可以在这里手动粘贴一份。
    """
    uid = int(me["user_id"])
    raw = (form.get("session") or [""])[0].strip()
    if not raw:
        return _redirect(handler, "/app/connect?msg="
                         + urllib.parse.quote("先粘贴登录态内容"))
    try:
        cookies = json.loads(raw)
    except json.JSONDecodeError:
        return _redirect(handler, "/app/connect?msg="
                         + urllib.parse.quote("粘贴的内容不是合法 JSON"))
    if not isinstance(cookies, list) or not cookies:
        return _redirect(handler, "/app/connect?msg="
                         + urllib.parse.quote("登录态格式不对（要数组）"))
    names = {str(c.get("name") or "") for c in cookies if isinstance(c, dict)}
    if "JWT_WEB" not in names:
        return _redirect(handler, "/app/connect?msg="
                         + urllib.parse.quote("里面没有 JWT_WEB，不是佳明登录态"))
    home = _home(uid)
    (home / "data").mkdir(parents=True, exist_ok=True)
    path = home / "data" / "session.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cookies, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    enqueue(uid)
    return _redirect(handler, "/app?msg="
                     + urllib.parse.quote("登录态已导入，正在同步"))


def _connect_post(handler, form: dict, me) -> None:
    uid = int(me["user_id"])
    source = (form.get("source") or ["garmin"])[0]
    email = (form.get("email") or [""])[0].strip()
    password = (form.get("password") or [""])[0]
    consent = (form.get("consent") or [""])[0] == "1"
    if source not in ("garmin", "coros"):
        source = "garmin"
    if not consent:
        return _redirect(handler, "/app/connect?msg=" + urllib.parse.quote("要先勾选同意"))
    if not email or "@" not in email or not password:
        return _redirect(handler, "/app/connect?msg=" + urllib.parse.quote("账号和密码都要填"))
    try:
        _save_creds(uid, source, email, password)
    except Exception as e:      # noqa: BLE001
        return _redirect(handler, "/app/connect?msg="
                         + urllib.parse.quote(f"保存失败：{type(e).__name__}"))
    # 换账号必须清掉旧会话，否则会拿旧 cookie 同步到新账号的数据里
    try:
        (_home(uid) / "data" / "session.json").unlink()
    except OSError:
        pass
    enqueue(uid)
    _redirect(handler, "/app?msg=" + urllib.parse.quote("已保存，正在同步"))


# ---------------------------------------------------------------- 渲染
def _render(uid: int, kind: str, qs: dict) -> str:
    env = os.environ.copy()
    env["GARMIN_HOME"] = str(_home(uid))
    env["PYTHONPATH"] = str(APP_DIR) + os.pathsep + str(APP_DIR.parent)
    env["PYTHONUNBUFFERED"] = "1"
    try:
        p = subprocess.run(
            [sys.executable, str(APP_DIR / "web_render.py"), kind,
             json.dumps(qs, ensure_ascii=False)],
            env=env, cwd=str(APP_DIR), capture_output=True, text=True, timeout=40)
    except subprocess.TimeoutExpired:
        return _error_page("页面渲染超时，稍后再试")
    if p.returncode != 0 or not p.stdout.strip():
        err = (p.stderr or "").strip().splitlines()
        return _error_page("页面渲染失败：" + (err[-1] if err else "未知错误"))
    return p.stdout


def _today_page(handler, me, qs: dict) -> None:
    uid = int(me["user_id"])
    msg = (qs.get("msg") or [""])[0]
    if not _creds(uid) and not (_home(uid) / "data" / "session.json").is_file():
        return _redirect(handler, "/app/connect")
    html = _render(uid, "today", {})
    html = _shell(html, "/app", _banner(uid, msg))
    html = html.replace("</body>", _sync_on_load(handler, uid) + "</body>", 1)
    _send_html(handler, html)


def _history_page(handler, me, qs: dict) -> None:
    uid = int(me["user_id"])
    html = _render(uid, "history", qs)
    _send_html(handler, _shell(html, "/app/history", _banner(uid)))


def _view_page(handler, me, qs: dict) -> None:
    uid = int(me["user_id"])
    html = _render(uid, "view", qs)
    _send_html(handler, _shell(html, "/app/history", _banner(uid)))


def _privacy_page(handler) -> None:
    U = _U()
    body = ('<div class="card"><p class="note" style="line-height:1.9">'
            '网页版会保存你的登录邮箱、加密后的运动账号密码，以及从佳明/高驰同步的'
            '训练数据（每日汇总、活动、睡眠、体重等）。这些数据只用于给你本人展示和'
            '生成训练建议，不出售、不投放广告、不提供给无关第三方。<br><br>'
            '你可以在「我的」里删除数据源（删除加密账密与会话），或退出登录。'
            '需要删除全部训练数据时，发邮件联系管理员，确认身份后处理。<br><br>'
            '训练建议仅用于个人参考，不构成医疗建议。</p></div>')
    html = U.page("APP · 隐私", "隐私说明", "网页版的数据怎么处理", body,
                  U.chip("不售卖"), foot="最后更新：2026-11-23")
    _send_html(handler, _shell(html, "/app/me"))


# ---------------------------------------------------------------- 清单/静态
def _manifest(handler) -> None:
    obj = {
        "name": "RunnerCoach", "short_name": "RunnerCoach",
        "start_url": "/app", "scope": "/",
        "display": "standalone", "background_color": "#070B12",
        "theme_color": "#070B12", "lang": "zh-CN",
        "icons": [
            {"src": "/app/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/app/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/manifest+json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "public, max-age=3600")
    handler.end_headers()
    handler.wfile.write(raw)
    handler.close_connection = True


def _sw(handler) -> None:
    js = """const C='rc-web-v1';
self.addEventListener('install',e=>{self.skipWaiting()});
self.addEventListener('activate',e=>{e.waitUntil(clients.claim())});
self.addEventListener('fetch',e=>{
  const u=new URL(e.request.url);
  if(e.request.method!=='GET')return;
  if(u.pathname.startsWith('/app/icon-')||u.pathname==='/app/manifest.webmanifest'){
    e.respondWith(caches.open(C).then(c=>c.match(e.request).then(r=>r||fetch(e.request).then(x=>{c.put(e.request,x.clone());return x}))));
  }
});"""
    raw = js.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/javascript; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    handler.wfile.write(raw)
    handler.close_connection = True


def _icon(handler, name: str) -> None:
    f = WEB_DIR / "icons" / name
    if not f.is_file():
        return _send_html(handler, "not found", 404)
    raw = f.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", "image/png")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "public, max-age=86400")
    handler.end_headers()
    handler.wfile.write(raw)
    handler.close_connection = True


# ---------------------------------------------------------------- 同步队列
def enqueue(uid: int) -> None:
    with _A().db() as c:
        row = c.execute("SELECT id FROM web_jobs WHERE user_id=? AND status IN "
                        "('pending','running') LIMIT 1", (int(uid),)).fetchone()
        if row:
            return
        c.execute("INSERT INTO web_jobs(user_id,status,created) VALUES(?,?,?)",
                  (int(uid), "pending", int(time.time())))


def _claim():
    A = _A()
    with A.db() as c:
        try:
            c.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return None
        row = c.execute("SELECT * FROM web_jobs WHERE status='pending' "
                        "ORDER BY id LIMIT 1").fetchone()
        if not row:
            return None
        c.execute("UPDATE web_jobs SET status='running', started=? WHERE id=?",
                  (int(time.time()), int(row["id"])))
        return dict(row)


def _finish(job_id: int, ok: bool, message: str) -> None:
    with _A().db() as c:
        c.execute("UPDATE web_jobs SET status=?, message=?, finished=? WHERE id=?",
                  ("done" if ok else "error", message[:300], int(time.time()),
                   int(job_id)))


def _run_job(job: dict) -> None:
    uid = int(job["user_id"])
    cr = _creds(uid)
    # 佳明现在只认「登录态」：服务器登录会被佳明的人机校验挡住，登录态由手机
    # App 回传或用户在连接页粘贴。没有账密但有登录态时，照样能同步。
    has_session = (_home(uid) / "data" / "session.json").exists()
    if not cr and not has_session:
        return _finish(int(job["id"]), False,
                       "还没有登录态：用安卓 App 登录佳明后会自动传上来（App 的账号邮箱要和网页一致）")
    if cr:
        try:
            password = _dec(str(cr["nonce"]), str(cr["ct"]))
        except Exception as e:      # noqa: BLE001
            return _finish(int(job["id"]), False, f"解密失败：{type(e).__name__}")
        source, email = str(cr["source"]), str(cr["email"])
    else:
        password, source, email = "", "garmin", ""
    st = _stats(uid)
    days = 30 if st["days"] == 0 else 3
    env = os.environ.copy()
    env["GARMIN_HOME"] = str(_home(uid))
    env["PYTHONPATH"] = str(APP_DIR) + os.pathsep + str(APP_DIR.parent)
    env["PYTHONUNBUFFERED"] = "1"
    payload = json.dumps({"source": source, "email": email,
                          "password": password, "days": days}, ensure_ascii=False)
    try:
        p = subprocess.run([sys.executable, str(APP_DIR / "web_sync.py")],
                           input=payload, env=env, cwd=str(APP_DIR),
                           capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return _finish(int(job["id"]), False, "同步超时（5 分钟）")
    out = {}
    for line in reversed((p.stdout or "").strip().splitlines()):
        try:
            out = json.loads(line)
            break
        except ValueError:
            continue
    if out.get("ok"):
        return _finish(int(job["id"]), True,
                       f"新增 {int(out.get('new') or 0)} 条，{out.get('seconds', '?')} 秒")
    msg = str(out.get("error") or "").strip()
    if not msg:
        lines = (p.stderr or "").strip().splitlines()
        msg = lines[-1] if lines else "同步失败"
    if out.get("need_mfa"):
        msg = "佳明要求验证码，请重新登录或稍后再试"
    elif out.get("need_login"):
        msg = "登录态已过期：用安卓 App 同步一次就会自动更新"
    return _finish(int(job["id"]), False, msg)


def _worker_loop() -> None:
    while True:
        job = _claim()
        if not job:
            time.sleep(5)
            continue
        try:
            _run_job(job)
        except Exception as e:      # noqa: BLE001 - worker 不能死
            try:
                _finish(int(job["id"]), False, f"{type(e).__name__}: {e}")
            except Exception:      # noqa: BLE001
                pass


def start_worker() -> None:
    global WORKER_STARTED
    with _WORKER_LOCK:
        if WORKER_STARTED:
            return
        WORKER_STARTED = True
        threading.Thread(target=_worker_loop, daemon=True, name="web-sync").start()


# ---------------------------------------------------------------- 路由入口
def handle_get(handler) -> None:
    u = urllib.parse.urlsplit(handler.path)
    path = u.path.rstrip("/") or "/app"
    qs = urllib.parse.parse_qs(u.query)
    me = _me(handler)

    if path == "/app/manifest.webmanifest":
        return _manifest(handler)
    if path == "/app/sw.js":
        return _sw(handler)
    if path.startswith("/app/icon-") and path.endswith(".png"):
        return _icon(handler, path.rsplit("/", 1)[-1])
    if path == "/app/login":
        if me:
            return _redirect(handler, "/app")
        return _login_page(handler, qs)
    if path == "/app/privacy":
        return _privacy_page(handler)
    if path == "/app/logout":
        return _redirect(handler, "/app/login",
                         headers=[("Set-Cookie", f"{COOKIE}=; Path=/; HttpOnly; "
                                   "SameSite=Lax; Max-Age=0")])
    if not me:
        return _redirect(handler, "/app/login")

    if path == "/app":
        return _today_page(handler, me, qs)
    if path == "/app/history":
        return _history_page(handler, me, qs)
    if path == "/app/view":
        return _view_page(handler, me, qs)
    if path == "/app/me":
        return _me_page(handler, me, qs)
    if path == "/app/plan":
        return _send_html(handler, _shell(_render(int(me["user_id"]), "plan", qs),
                                          "/app", _banner(int(me["user_id"]))))
    if path == "/app/event":
        return _send_html(handler, _shell(_render(int(me["user_id"]), "event", qs),
                                          "/app", _banner(int(me["user_id"]))))
    if path == "/app/act":
        return _act_page(handler, me, qs)
    if path.startswith("/app/basemap/"):
        return _basemap(handler, me, path[len("/app/basemap/"):])
    if path == "/app/connect":
        return _connect_page(handler, me, qs)
    _send_html(handler, _error_page("这个页面不存在", 404), 404)


def _read_form(handler) -> dict:
    try:
        n = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        n = 0
    if n > 65536:
        return {}
    raw = handler.rfile.read(n) if n else b""
    return urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)


def handle_post(handler) -> None:
    u = urllib.parse.urlsplit(handler.path)
    path = u.path.rstrip("/") or "/app"
    form = _read_form(handler)
    if path == "/app/login":
        return _login_post(handler, form)
    me = _me(handler)
    if not me:
        return _redirect(handler, "/app/login")
    if path == "/app/connect":
        return _connect_post(handler, form, me)
    if path == "/app/import":
        return _import_post(handler, form, me)
    if path == "/app/sync":
        # 不写 msg：让顶部横幅去说真实状态。以前这里发一条固定的
        # 「已加入同步队列」，任务 1 秒后失败也还是这句话，用户看到的永远是
        # 「没反应」；现在排队/进行中/失败/完成都由 _banner 按 job 状态显示。
        enqueue(int(me["user_id"]))
        return _redirect(handler, "/app")
    if path == "/app/forget":
        _del_creds(int(me["user_id"]))
        try:      # 连登录态一起删：只删账密、留着会话等于没删
            (_home(int(me["user_id"])) / "data" / "session.json").unlink()
        except OSError:
            pass
        return _redirect(handler, "/app/me?msg=" + urllib.parse.quote("已删除数据源"))
    if path == "/app/logout":
        return _redirect(handler, "/app/login",
                         headers=[("Set-Cookie", f"{COOKIE}=; Path=/; HttpOnly; "
                                   "SameSite=Lax; Max-Age=0")])
    _send_html(handler, _error_page("不支持的操作", 404), 404)
