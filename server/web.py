# -*- coding: utf-8 -*-
"""成员视角的 Web 端：用浏览器看组里的榜单、赛事与远征。

和 `/manage` 的分工
-------------------
`/manage` 是**组长工具**（审核、导出、改设置），整页都是管理动作。
内测群里更多人需要的是另一件事：在电脑、平板，或者装不上 App 的手机上，
看一眼组里谁跑得多、赛事还差多少、远征走到哪。两类人混在一页，要么
权限说不清，要么组长在一堆信息里找按钮。

三条规矩
--------
1. **只读**。建赛事、审核、改设置仍然只在 App 和 /manage。Web 端不提供
   任何写操作，也就不存在 CSRF 面。
2. **同一套账号**：登录走 `app.api_sms_send` / `app.api_auth`（和 App、
   和 /manage 完全一样），会话直接放设备令牌，由 `manage.me_of` 校验。
   不新造一套口令，少一处可能对不上的地方。
3. **数据调内部函数**（`groups.api_*`），不绕一圈 HTTP 调自己——省一次
   序列化，也免了自己给自己发令牌。
"""
from __future__ import annotations

import datetime as dt
import urllib.parse

import groups as G
import manage as M

try:
    import expedition as X
except Exception:      # noqa: BLE001 - 远征模块没部署时其余页面照样能开
    X = None

# 在 /manage 的样式上加几行。沿用同一份 CSS 变量和组件，
# 两个后台看起来是一套东西，而不是两个拼起来的站。
EXTRA_CSS = """
/* 底部导航：/w 原来是一张独立的页，从 /app 点「小组」跳进来后底栏就没了，
   只能靠浏览器后退回去。四个 tab 指向和 /app 完全相同的地址，用户感觉
   还是同一个应用。 */
.wnav{position:fixed;left:0;right:0;bottom:0;z-index:60;display:flex;
  background:rgba(8,13,21,.96);border-top:1px solid rgba(255,255,255,.08);
  padding:6px 4px calc(6px + env(safe-area-inset-bottom));
  backdrop-filter:blur(12px)}
.wnav a{flex:1;min-height:44px;display:flex;align-items:center;
  justify-content:center;text-decoration:none;color:#7A8DA4;font-size:12px;
  font-weight:600;border-radius:12px}
.wnav a.on{color:#9CC6FF;background:rgba(59,130,246,.12)}
body{padding-bottom:calc(64px + env(safe-area-inset-bottom))!important}
.glist a{display:block;padding:14px 16px;border-radius:12px;margin-bottom:10px;
  background:var(--card,#151b26);text-decoration:none;color:inherit;
  border:1px solid rgba(128,128,128,.18)}
.glist a:hover{border-color:rgba(128,128,128,.38)}
.glist b{display:block;font-size:16px;margin-bottom:4px}
.rk{display:flex;align-items:center;gap:10px;padding:9px 0;
  border-bottom:1px solid rgba(128,128,128,.14);font-size:15px}
.rk:last-child{border-bottom:0}
.rk .no{width:26px;color:#8a93a3;font-variant-numeric:tabular-nums}
.rk .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rk .km{font-variant-numeric:tabular-nums;color:#cfd6e2}
.rk.me{font-weight:600}
.rk.me .no,.rk.me .km{color:#e8b06a}
.seg{display:flex;gap:8px;margin:14px 0}
.seg a{padding:6px 14px;border-radius:99px;text-decoration:none;
  color:inherit;border:1px solid rgba(128,128,128,.28);font-size:14px}
.seg a.ok{background:#e8b06a;color:#1a1206;border-color:#e8b06a}
.post{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:baseline}
.sub2{color:#8a93a3;font-size:13px}
"""


def _e(v) -> str:
    return M._e(v)


_WEB_TABS = (("/app", "今日"), ("/app/history", "历史"),
             ("/w", "小组"), ("/app/me", "我的"))


def _wnav(active: str = "/w") -> str:
    """底部四个 tab。地址和 /app 用的是同一套，点过去不会掉出应用。"""
    out = []
    for href, label in _WEB_TABS:
        cls = ' class="on"' if href == active else ""
        out.append(f'<a href="{href}"{cls}>{label}</a>')
    return '<nav class="wnav">' + "".join(out) + "</nav>"


def _page(title: str, body: str, me=None, msg: str = "",
          css: str = "", back: tuple | None = None) -> bytes:
    # 标题下原来固定挂一条「我的小组」链接，和底部导航的第一个 tab 重复；
    # 名字为空时还会显示成「登录中： · 退出」，中间空一截。
    head = "<div class=wrap>"
    if back:
        head += f'<a class=back href="{_e(back[1])}">‹ {_e(back[0])}</a>'
    head += f"<h1>{_e(title)}</h1>"
    if me is not None:
        who = ""
        if isinstance(me, dict):
            who = str(me.get("nickname") or me.get("phone") or "").strip()
        label = f"登录中：{_e(who)}" if who else "已登录"
        head += ('<div class=sub style="margin-top:6px">' + label +
                 ' · <form class=inline method=post action="/w/logout">'
                 '<button class=ghost>退出</button></form></div>')
    tip = f'<div class=msg>{_e(msg)}</div>' if msg else ""
    return ("<!doctype html><html lang=zh-CN><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{_e(title)} · RunnerCoach</title>"
            f"<style>{M.CSS}{EXTRA_CSS}{css}</style></head>"
            f"<body>{head}{tip}{body}{_wnav()}</div></body></html>").encode("utf-8")


def _km(m) -> str:
    return f"{float(m or 0) / 1000:.1f}"


def _today() -> str:
    return dt.date.today().isoformat()


# ---------------------------------------------------------------- 登录
def login_page(msg: str = "", account: str = "", sent: bool = False) -> bytes:
    if sent:
        body = ('<div class=card><h2 style="margin-top:0">输入验证码</h2>'
                '<form method=post action="/w/login">'
                f'<input type=hidden name=account value="{_e(account)}">'
                '<p class=sub>验证码已发到 ' + _e(account) + '，10 分钟内有效。</p>'
                '<input name=code inputmode=numeric autocomplete=one-time-code '
                'placeholder="6 位验证码" maxlength=8 style="margin-bottom:12px">'
                '<button>登录</button>'
                '<a href="/w" style="margin-left:12px">换个账号</a>'
                '</form></div>')
        return _page("RunnerCoach", body, msg=msg)
    body = ('<div class=card><h2 style="margin-top:0">用 App 的账号登录</h2>'
            '<form method=post action="/w/send">'
            '<input name=account placeholder="手机号或邮箱" autocomplete=username '
            'style="margin-bottom:12px">'
            '<button>发送验证码</button></form>'
            '<p class=sub style="margin-bottom:0">账号和 App 里登录的是同一个，'
            '不用另外注册。</p></div>'
            '<p class=sub>这里只能看小组相关的数据：榜单、赛事、远征。'
            '今日训练、历史分析这些存在手机本地，网页上没有。</p>')
    return _page("RunnerCoach", body, msg=msg)


# ---------------------------------------------------------------- 我的小组
def _my_groups(me) -> tuple[list, str]:
    """(小组列表, 错误文案)。读失败时给空列表和一句能看懂的说明。"""
    try:
        return ((G.api_list(me) or {}).get("groups") or []), ""
    except Exception as e:      # noqa: BLE001
        return [], str(e)


def home(me, lst=None) -> bytes:
    if lst is None:
        lst, err = _my_groups(me)
        if err:
            return _page("我的小组",
                         f'<div class=card><p class=sub>读不到小组：{_e(err)}</p></div>',
                         me)
    if not lst:
        return _page("我的小组",
                     '<div class=card><p class=sub>还没有加入小组。'
                     '加入或创建要在 App 里做，这里只能看。</p></div>', me)

    items = ""
    for g in lst:
        w = g.get("week") or {}
        rank, km = int(w.get("rank") or 0), float(w.get("km") or 0)
        line = (f"本周第 {rank} 名 · {km:g} km" if rank else "本周还没有记录")
        h = g.get("honors") or {}
        extra = ""
        if h.get("total_km"):
            extra = f" · 全组 {h['total_km']:g} km"
        items += (f'<a href="/w/g/{int(g["id"])}"><b>{_e(g.get("name"))}</b>'
                  f'<span class=sub2>{int(g.get("member_count") or 0)} 人 · '
                  f'{_e(line)}{extra}</span></a>')

    body = '<div class="card glist">' + items + '</div>'
    return _page("我的小组", body, me)


def _msg_box(msg: str) -> str:
    return f'<p class=sub>{_e(msg)}</p>' if msg else ""


# ---------------------------------------------------------------- 路由
def handle_get(path: str, query: dict, cookie: str | None) -> tuple:
    html = "text/html; charset=utf-8"
    me = M.me_of(cookie)
    if path in ("", "/w"):
        if me is None:
            return (200, login_page(), html, {})
        lst, err = _my_groups(me)
        # 只有一个组时直达组页，和 App 一样（带 ?list=1 才看列表）。
        # 列表页对单组用户是纯中转，点一下还是进同一个组。
        if not err and len(lst) == 1 and not (query.get("list") or [""])[0]:
            return (303, b"", "text/plain",
                    {"Location": f"/w/g/{int(lst[0]['id'])}"})
        return (200, home(me, None if err else lst), html, {})

    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) >= 3 and parts[0] == "w" and parts[1] == "g" and parts[2].isdigit():
        if me is None:
            return (303, b"", "text/plain", {"Location": "/w"})
        import webgroup as WG

        return WG.handle(me, int(parts[2]), parts[3:], query)
    return (404, "<h3>404</h3><p><a href='/w'>回到我的小组</a></p>".encode("utf-8"),
            html, {})


def handle_post(path: str, form: dict, cookie: str | None) -> tuple:
    def s(k: str, d: str = "") -> str:
        v = form.get(k)
        return str(v[0]) if isinstance(v, list) and v else d

    if path in ("/w", "/w/"):
        return (303, b"", "text/plain", {"Location": "/w"})
    if path == "/w/send":
        import app as A

        account = s("account").strip()
        try:
            A.api_sms_send({"account": account}, "web")
        except Exception as e:      # noqa: BLE001 - Gate 也走这里
            hint = str(getattr(e, "message", e))
            return (200, login_page(hint), "text/html; charset=utf-8", {})
        return (200, login_page(account=account, sent=True),
                "text/html; charset=utf-8", {})
    if path == "/w/login":
        import app as A

        account, code = s("account").strip(), s("code").strip()
        try:
            r = A.api_auth({"account": account, "code": code,
                            "device_name": "RunnerCoach 网页"}, "web")
        except Exception as e:      # noqa: BLE001
            return (200, login_page(str(getattr(e, "message", e)), account, True),
                    "text/html; charset=utf-8", {})
        return (303, b"", "text/plain",
                {"Location": "/w",
                 "Set-Cookie": M._cookie_header(str(r["token"]))})
    if path == "/w/logout":
        return (303, b"", "text/plain",
                {"Location": "/w",
                 "Set-Cookie": f"{M.COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"})
    return (404, b"<h3>404</h3>", "text/plain; charset=utf-8", {})
