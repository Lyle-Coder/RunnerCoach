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


def _page(title: str, body: str, me=None, msg: str = "") -> bytes:
    head = (f"<div class=wrap><h1>{_e(title)}</h1>"
            f"<div class=sub><a href=\"/w\">我的小组</a></div>")
    if me is not None:
        who = me["phone"] if isinstance(me, dict) else ""
        head += ('<div class=sub style="margin-top:6px">登录中：' + _e(who) +
                 ' · <form class=inline method=post action="/w/logout">'
                 '<button class=ghost>退出</button></form></div>')
    tip = f'<div class=msg>{_e(msg)}</div>' if msg else ""
    return ("<!doctype html><html lang=zh-CN><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{_e(title)} · RunnerCoach</title>"
            f"<style>{M.CSS}{EXTRA_CSS}</style></head>"
            f"<body>{head}{tip}{body}</div></body></html>").encode("utf-8")


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
def home(me) -> bytes:
    try:
        lst = (G.api_list(me) or {}).get("groups") or []
    except Exception as e:      # noqa: BLE001
        return _page("我的小组",
                     f'<div class=card><p class=sub>读不到小组：{_e(str(e))}</p></div>',
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


# ---------------------------------------------------------------- 榜单片段
def _rank_rows(rows, by: str = "dist") -> str:
    if not rows:
        return '<p class=sub>这段时间还没有人上报。</p>'
    out = []
    for i, x in enumerate(rows, 1):
        val = x.get("comp_m") if by == "comp" else x.get("distance_m")
        note = ""
        if by == "comp":
            ride = float(x.get("ride_m") or 0) / 1000.0
            if ride > 0:
                note = (f'<span class=sub2>跑 {_km(x.get("distance_m"))}'
                        f' · 骑 {ride:g}</span>')
        cls = "rk me" if x.get("is_me") else "rk"
        out.append(f'<div class="{cls}"><span class=no>{i}</span>'
                   f'<span class=nm>{_e(x.get("alias"))} {note}</span>'
                   f'<span class=km>{_km(val)} km</span></div>')
    return "".join(out)


# ---------------------------------------------------------------- 组主页
def group(me, gid: int, msg: str = "") -> bytes:
    today = _today()
    try:
        b = G.api_board(me, gid, {"range": ["week"], "today": [today]})
    except Exception as e:      # noqa: BLE001
        return _page("小组",
                     f'<div class=card><p class=sub>{_e(str(e))}</p></div>', me)
    if not b.get("ok"):
        return _page("小组",
                     f'<div class=card><p class=sub>{_e(b.get("error") or "打不开")}</p></div>',
                     me)

    rows = b.get("rows") or []
    mine = next((i for i, x in enumerate(rows, 1) if x.get("is_me")), 0)
    total = float((b.get("summary") or {}).get("total_distance_m") or 0) / 1000.0

    me_line = ""
    if mine:
        me_line = (f'<p class=sub>我本周第 <b>{mine}</b> 名，共 {len(rows)} 人 · '
                   f'全组 {total:g} km</p>')
    elif rows:
        me_line = '<p class=sub>本周还没上报，榜上暂时没有你。</p>'

    # 赛事：只列还在进行的
    ch_html = ""
    try:
        chs = (G.api_challenge_list(me, gid, {"today": [today]}) or {}).get("challenges") or []
        live = [c for c in chs if not c.get("closed_at") and str(c.get("end_date") or "") >= today]
        if live:
            items = ""
            for c in live[:5]:
                pct = float(c.get("pct") or 0)
                cur, tgt, unit = c.get("current"), c.get("target"), c.get("unit") or ""
                items += (f'<div class=post><div><b>{_e(c.get("title"))}</b>'
                          f'<div class=sub2>到 {_e(c.get("end_date"))} 结束</div></div>'
                          f'<div class=sub2>{cur}/{tgt} {_e(unit)}'
                          f'（{pct:.0f}%）</div></div>')
            ch_html = ('<div class=card><h2 style="margin-top:0">进行中的赛事</h2>'
                       + items + '</div>')
        else:
            ch_html = ('<div class=card><h2 style="margin-top:0">赛事</h2>'
                       '<p class=sub style="margin-bottom:0">暂时没有进行中的赛事。</p></div>')
    except Exception:      # noqa: BLE001 - 赛事拉不到不该让整页打不开
        pass

    # 远征：只给摘要，完整战况仍在 App 里看
    run_html = ""
    if X is not None:
        try:
            st = X.api_state(me, gid, {"today": [today]}) or {}
            if st.get("ok"):
                season = st.get("season") or {}
                mt = st.get("mine") or {}
                act = season.get("act_no") or season.get("act") or ""
                pos = mt.get("pos")
                sup = mt.get("supply")
                bits = []
                if act:
                    bits.append(f"第 {act} 幕")
                if pos is not None:
                    bits.append(f"位置 {pos}")
                if sup is not None:
                    bits.append(f"补给 {sup}")
                run_html = ('<div class=card><h2 style="margin-top:0">远征</h2>'
                            f'<p class=sub style="margin-bottom:0">'
                            f'{_e(" · ".join(str(x) for x in bits) or "进行中")}'
                            '　完整战况在 App 里</p></div>')
        except Exception:      # noqa: BLE001
            pass

    body = (_page_nav(gid)
            + _msg_box(msg)
            + me_line
            + '<div class=card><h2 style="margin-top:0">本周榜</h2>'
            + _rank_rows(rows[:10])
            + (f'<p class=sub><a href="/w/g/{gid}/board">看完整榜单 →</a></p>'
               if len(rows) > 10 or True else "")
            + '</div>'
            + ch_html + run_html)
    return _page("小组", body, me)


def _page_nav(gid: int) -> str:
    return (f'<div class=seg>'
            f'<a class=ok href="/w/g/{gid}">本周</a>'
            f'<a href="/w/g/{gid}/board?range=month">本月</a>'
            f'<a href="/w/g/{gid}/board?range=month&by=comp">综合</a></div>')


def _msg_box(msg: str) -> str:
    return f'<p class=sub>{_e(msg)}</p>' if msg else ""


# ---------------------------------------------------------------- 完整榜单
def board(me, gid: int, query: dict) -> bytes:
    today = _today()
    rng = "month" if (query.get("range") or [""])[0] == "month" else "week"
    by = "comp" if (rng == "month" and (query.get("by") or [""])[0] == "comp") else "dist"
    try:
        b = G.api_board(me, gid,
                        {"range": [rng], "by": [by], "today": [today]})
    except Exception as e:      # noqa: BLE001
        return _page("完整榜单",
                     f'<div class=card><p class=sub>{_e(str(e))}</p></div>', me)
    if not b.get("ok"):
        return _page("完整榜单",
                     f'<div class=card><p class=sub>{_e(b.get("error") or "打不开")}</p></div>',
                     me)

    rows = b.get("rows") or []
    sm = b.get("summary") or {}
    tot = float((sm.get("total_comp_m") if by == "comp"
                 else sm.get("total_distance_m")) or 0) / 1000.0
    span = ("本周" if rng == "week" else "本月") + " " + \
           f'{str(b.get("from") or "")[5:]} – {str(b.get("to") or "")[5:]}'

    if by == "comp":
        note = ('综合距离 = 跑步 + 骑行 ÷ 4。'
                '这个折算只在综合榜用：衫、徽章、赛季积分仍按跑步算。')
    elif rng == "month":
        note = '月榜只排里程。衫和探索徽章按周结算，不在这一页。'
    else:
        note = '按里程排；里程一样时，跑的天数多的在前。'

    nav = (f'<div class=seg>'
           f'<a href="/w/g/{gid}/board"' + (' class=ok' if rng == "week" else "")
           + '>本周</a>'
           f'<a href="/w/g/{gid}/board?range=month"'
           + (' class=ok' if rng == "month" and by == "dist" else "")
           + '>本月</a>'
           + (f'<a href="/w/g/{gid}/board?range=month&by=comp"'
              + (' class=ok' if by == "comp" else "") + '>综合</a>'
              if rng == "month" else "")
           + '</div>')

    mine = next((i for i, x in enumerate(rows, 1) if x.get("is_me")), 0)
    me_line = (f'<p class=sub>我第 <b>{mine}</b> 名 · {len(rows)} 人 · '
               f'全组 {tot:g} km</p>' if mine else "")

    body = (nav + f'<p class=sub>{_e(span)}</p>' + me_line
            + '<div class=card>' + _rank_rows(rows, by) + '</div>'
            + f'<p class=sub>{_e(note)}</p>'
            + f'<p class=sub><a href="/w/g/{gid}">← 返回小组</a></p>')
    return _page("完整榜单", body, me)


# ---------------------------------------------------------------- 路由
def handle_get(path: str, query: dict, cookie: str | None) -> tuple:
    me = M.me_of(cookie)
    if path in ("", "/w"):
        if me is None:
            return (200, login_page(), "text/html; charset=utf-8", {})
        return (200, home(me), "text/html; charset=utf-8", {})

    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) >= 3 and parts[0] == "w" and parts[1] == "g" and parts[2].isdigit():
        if me is None:
            return (303, b"", "text/plain", {"Location": "/w"})
        gid = int(parts[2])
        if len(parts) == 3:
            return (200, group(me, gid), "text/html; charset=utf-8", {})
        if len(parts) == 4 and parts[3] == "board":
            return (200, board(me, gid, query), "text/html; charset=utf-8", {})
    return (404, "<h3>404</h3><p><a href='/w'>回到我的小组</a></p>".encode("utf-8"),
            "text/html; charset=utf-8", {})


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
