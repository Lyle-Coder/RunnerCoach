# -*- coding: utf-8 -*-
"""组长 Web 管理页：浏览器里审核申请、看成员、结束赛事、查审计、导出。

为什么要有它
------------
这些事 App 里都能做，但组长在电脑前一次处理十个申请、导一份表给赞助商、
翻谁被谁踢过，手机上都不顺手。这里只做服务端渲染的普通表单：没有前端
框架、没有构建步骤、关掉 JS 也能用。

登录复用现有账号体系（`/v1/sms/send` + `api_auth`），令牌放 HttpOnly
Cookie。不新造口令，也不给后台单独一套密码。

安全口径
--------
- Cookie：HttpOnly + SameSite=Lax（跨站 POST 不会带 cookie，够挡 CSRF）
  + Secure（配了 TLS 才加，否则本地 http 调试存不进去）
- 只放行 owner / admin / coach；其它成员进来只看到一句「没有权限」
- 所有写操作都调 `groups.api_*`，权限判断不在这里重写一遍——两套判断
  迟早会分叉
"""
from __future__ import annotations

import datetime as dt
import html
import json
import time
import urllib.parse

COOKIE = "rc_tok"
COOKIE_MAX = 30 * 86400

# 角色中文名。和 App 里那套保持一致，别一处叫「管理员」一处叫「副队长」。
ROLE_CN = {"owner": "组长", "admin": "管理员", "coach": "教练", "member": "成员"}

# 审计表里存的是英文动作名。直接显示 `challenge_create` 等于让组长猜，
# 所以这里翻一遍。和 App 里那份（groupui._AUDIT_TXT）保持同一套说法，
# 同一个动作在两个地方叫不同名字最容易被问「这是两件事吗」。
AUDIT_CN = {
    "create": "建了组", "join": "加入", "apply": "申请加入", "leave": "退出",
    "remove": "踢出", "transfer": "转让组长", "archive": "解散了组",
    "rotate": "换了邀请码", "share": "改了共享设置", "alias": "改了昵称",
    "report": "上报了数据", "challenge_create": "建了赛事",
    "challenge_close": "结束了赛事", "approve": "批准加入", "reject": "拒绝加入",
    "join_mode": "改了加入方式", "goal": "改了目标", "mapshare": "改了路线共享",
    "camp": "升级了营地",
}
# 教练角色还没落地（P3 第二项），先只放行组长和管理员。
# 做完角色再把 "coach" 加回来——api_pending / api_decide 那边同样要放开，
# 只改这里会出现「页面能进、操作 403」。
MANAGER_ROLES = ("owner", "admin")

CSS = """
:root{--bg:#0e1116;--card:#171b22;--line:#262c36;--fg:#e7ecf3;--dim:#8b97a8;
--brand:#4ea1ff;--ok:#3ecf8e;--bad:#ff6b6b;--warn:#f5b942}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",system-ui,sans-serif}
.wrap{max-width:880px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:16px;margin:28px 0 10px;color:var(--dim);font-weight:600}
a{color:var(--brand);text-decoration:none}
.sub{color:var(--dim);font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:14px 16px;margin-top:12px}
.row{display:flex;align-items:center;gap:10px;padding:10px 0;
border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:0}
.row .g{flex:1 1 auto;min-width:0}
.row b{font-weight:600}
.pill{font-size:12px;padding:2px 8px;border-radius:999px;background:#222834;
color:var(--dim);white-space:nowrap}
.pill.ok{color:var(--ok)}.pill.bad{color:var(--bad)}.pill.warn{color:var(--warn)}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:16px 0 0}
.tabs a{padding:7px 12px;border-radius:10px;background:#1b2029;color:var(--dim);
font-size:14px}
.tabs a.on{background:var(--brand);color:#06121f;font-weight:600}
input,select{background:#11151c;border:1px solid var(--line);color:var(--fg);
border-radius:10px;padding:10px 12px;font-size:15px;width:100%;max-width:320px}
button{background:var(--brand);color:#06121f;border:0;border-radius:10px;
padding:10px 16px;font-size:15px;font-weight:600;cursor:pointer}
button.ghost{background:#222834;color:var(--fg);font-weight:500}
button.danger{background:#3a1f24;color:var(--bad)}
form.inline{display:inline}
.msg{background:#13202b;border:1px solid #1f3a4d;color:#9fd0ff;border-radius:10px;
padding:10px 12px;margin-top:14px;font-size:14px}
.empty{color:var(--dim);padding:14px 0}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:9px 6px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:500;font-size:12px}
.right{text-align:right}
"""


# ---------------------------------------------------------------- 工具
def _e(v) -> str:
    return html.escape(str(v if v is not None else ""))


def _page(title: str, body: str, *, me=None, msg: str = "") -> bytes:
    head = ("<div class=wrap><h1>RunnerCoach 组长后台</h1><div class=sub>" +
            _e(title) + "</div>")
    if me is not None:
        head += ('<div class=sub style="margin-top:6px">登录中：'
                 + _e(me["phone"]) +
                 ' · <a href="/manage">我的组</a> · '
                 '<form class=inline method=post action="/manage/logout">'
                 '<button class=ghost>退出</button></form></div>')
    tip = f'<div class=msg>{_e(msg)}</div>' if msg else ""
    return ("<!doctype html><html lang=zh-CN><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{_e(title)} · RunnerCoach</title><style>{CSS}</style></head>"
            f"<body>{head}{tip}{body}</div></body></html>").encode("utf-8")


def _cookie_token(cookie: str | None) -> str:
    for part in (cookie or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            return urllib.parse.unquote(v)
    return ""


def me_of(cookie: str | None):
    """Cookie 里的令牌换回用户行；无效就返回 None（不是异常）。

    直接借 `app._auth`：令牌校验、吊销判断、last_seen 都只有一份实现。
    这里只是把 Bearer 头换成 Cookie 取。
    """
    tok = _cookie_token(cookie)
    if not tok:
        return None
    import app as A

    try:
        return A._auth({"Authorization": "Bearer " + tok})
    except Exception:      # noqa: BLE001 - 过期/无效都当没登录
        return None


def _cookie_header(token: str) -> str:
    import app as A

    bits = [f"{COOKIE}={urllib.parse.quote(token)}", "Path=/",
            "HttpOnly", "SameSite=Lax", f"Max-Age={COOKIE_MAX}"]
    if A.TLS_CERT:          # 本地 http 调试时不能加，否则浏览器直接丢掉
        bits.append("Secure")
    return "; ".join(bits)


def _back(gid: int, tab: str, msg: str) -> tuple:
    q = urllib.parse.urlencode({"tab": tab, "msg": msg})
    return (303, b"", "text/plain", {"Location": f"/manage/g/{gid}?{q}"})


# ---------------------------------------------------------------- 页面
def login_page(msg: str = "", account: str = "", sent: bool = False) -> bytes:
    if sent:
        body = ('<div class=card><h2 style="margin-top:0">输入验证码</h2>'
                '<form method=post action="/manage/login">'
                f'<input type=hidden name=account value="{_e(account)}">'
                '<p class=sub>验证码已发到 ' + _e(account) + '，10 分钟内有效。</p>'
                '<input name=code inputmode=numeric autocomplete=one-time-code '
                'placeholder="6 位验证码" maxlength=8 style="margin-bottom:12px">'
                '<button>登录</button>'
                '<a href="/manage" style="margin-left:12px">换个账号</a>'
                '</form></div>')
        return _page("登录", body, msg=msg)
    body = ('<div class=card><h2 style="margin-top:0">用 App 里的账号登录</h2>'
            '<form method=post action="/manage/send">'
            '<input name=account placeholder="手机号或邮箱" autocomplete=username '
            'style="margin-bottom:12px">'
            '<button>发送验证码</button></form>'
            '<p class=sub style="margin-bottom:0">账号与 App 里登录的同一个，'
            '不需要另外注册。</p></div>')
    return _page("登录", body, msg=msg)


def groups_page(me) -> bytes:
    import groups as G

    try:
        r = G.api_list(me)
    except Exception as e:      # noqa: BLE001 - 后台不该因为一个异常白屏
        return _page("我的组", '<div class=card>读不到小组：' + _e(e) + '</div>', me=me)

    rows = []
    for g in (r.get("groups") or []):
        if str(g.get("role") or "") not in MANAGER_ROLES:
            continue
        gid = int(g["id"])
        wk = g.get("week") or {}
        rows.append(
            f'<div class=row><div class=g><a href="/manage/g/{gid}">'
            f'<b>{_e(g.get("name"))}</b></a><div class=sub>'
            f'{int(g.get("member_count") or 0)} 人 · 本周全组 '
            f'{float(wk.get("total_km") or 0):g} km</div></div>'
            f'<span class=pill>{_e(ROLE_CN.get(str(g.get("role")), ""))}</span>'
            '</div>')
    body = ('<div class=card>' + ("".join(rows) or
            '<div class=empty>没有可管理的组。只有组长、管理员和教练能进这里。</div>')
            + '</div>')
    return _page("我管理的组", body, me=me)


def _tabs(gid: int, cur: str, pending: int) -> str:
    items = [("members", "成员"), ("pending", f"审核（{pending}）" if pending else "审核"),
             ("challenge", "赛事"), ("audit", "操作记录"), ("settings", "设置")]
    out = ['<div class=tabs>']
    for key, label in items:
        cls = " class=on" if key == cur else ""
        out.append(f'<a href="/manage/g/{gid}?tab={key}"{cls}>{_e(label)}</a>')
    out.append('</div>')
    return "".join(out)


def group_page(me, gid: int, tab: str = "members", msg: str = "") -> bytes:
    import groups as G

    try:
        d = G.api_get(me, gid)
    except Exception as e:      # noqa: BLE001
        return _page("小组", '<div class=card>打不开这个组：' + _e(e) + '</div>', me=me)
    if not d.get("ok"):
        return _page("小组", '<div class=card>' + _e(d.get("error") or "没有权限") + '</div>',
                     me=me)
    g = d.get("group") or {}
    role = str(g.get("role") or d.get("role") or "")
    if role not in MANAGER_ROLES:
        return _page("小组", '<div class=card>你不是这个组的管理者。</div>', me=me)

    try:
        pend = len((G.api_pending(me, gid) or {}).get("pending") or [])
    except Exception:      # noqa: BLE001 - 审核数拿不到不该让页面挂
        pend = 0

    head = (f'<div class=card><div class=row><div class=g>'
            f'<b style="font-size:18px">{_e(g.get("name"))}</b>'
            f'<div class=sub>{int(g.get("member_count") or 0)} 人 · '
            f'{_e(ROLE_CN.get(role, role))} · 邀请码 '
            f'<b>{_e(g.get("invite_code") or "仅组长可见")}</b></div></div>'
            f'<a class=pill href="/manage/export/{gid}.csv">导出 CSV</a></div></div>')

    body = head + _tabs(gid, tab, pend)
    if tab == "pending":
        body += _tab_pending(me, gid)
    elif tab == "challenge":
        body += _tab_challenge(me, gid)
    elif tab == "audit":
        body += _tab_audit(me, gid)
    elif tab == "settings":
        body += _tab_settings(me, gid, g)
    else:
        body += _tab_members(me, gid)
    return _page(str(g.get("name") or "小组"), body, me=me, msg=msg)


def _tab_members(me, gid: int) -> str:
    import groups as G

    try:
        board = G.api_board(me, gid, {"range": ["week"]})
    except Exception:      # noqa: BLE001
        board = {}
    stats = {int(x.get("user_id") or 0): x for x in (board.get("rows") or [])}
    try:
        d = G.api_get(me, gid)
    except Exception:      # noqa: BLE001
        d = {}
    mem = ((d.get("group") or {}).get("members") or [])
    if not mem:
        return '<div class=card><div class=empty>读不到成员列表。</div></div>'
    rows = []
    for m in mem:
        uid = int(m.get("user_id") or 0)
        s = stats.get(uid) or {}
        km = float(s.get("distance_m") or 0) / 1000.0
        role = str(m.get("role") or "member")
        kick = ""
        if role != "owner" and uid != int(me["user_id"]):
            kick = ('<form class=inline method=post action="/manage/act">'
                    f'<input type=hidden name=gid value="{gid}">'
                    f'<input type=hidden name=uid value="{uid}">'
                    '<input type=hidden name=tab value=members>'
                    '<button class=danger name=op value=kick '
                    'onclick="return confirm(\'移出这个组？\')">移出</button></form>')
        rows.append(
            f'<tr><td>{_e(m.get("alias"))}</td>'
            f'<td class=sub>{_e(ROLE_CN.get(role, role))}</td>'
            f'<td class=right>{km:.1f}</td>'
            f'<td class=right class=sub>{int(s.get("days") or 0)} 天</td>'
            f'<td class=right>{kick}</td></tr>')
    return ('<div class=card><table><tr><th>昵称</th><th>角色</th>'
            '<th class=right>本周 km</th><th class=right>出勤</th><th></th></tr>'
            + "".join(rows) + '</table></div>')


def _tab_pending(me, gid: int) -> str:
    import groups as G

    try:
        r = G.api_pending(me, gid)
    except Exception as e:      # noqa: BLE001
        return '<div class=card><div class=empty>读不到申请：' + _e(e) + '</div></div>'
    rows = []
    for p in (r.get("pending") or []):
        uid = int(p.get("user_id") or 0)
        rows.append(
            f'<div class=row><div class=g><b>{_e(p.get("alias"))}</b>'
            f'<div class=sub>申请加入</div></div>'
            '<form class=inline method=post action="/manage/act">'
            f'<input type=hidden name=gid value="{gid}">'
            f'<input type=hidden name=uid value="{uid}">'
            '<input type=hidden name=tab value=pending>'
            '<button name=op value=approve>批准</button></form>'
            '<form class=inline method=post action="/manage/act">'
            f'<input type=hidden name=gid value="{gid}">'
            f'<input type=hidden name=uid value="{uid}">'
            '<input type=hidden name=tab value=pending>'
            '<button class=ghost name=op value=reject>拒绝</button></form></div>')
    body = ('<div class=card>' + ("".join(rows) or
            '<div class=empty>没有待审核的申请。</div>') + '</div>')
    return body


def _tab_challenge(me, gid: int) -> str:
    import groups as G

    try:
        r = G.api_challenge_list(me, gid, {})
    except Exception as e:      # noqa: BLE001
        return '<div class=card><div class=empty>读不到赛事：' + _e(e) + '</div></div>'
    rows = []
    for c in (r.get("challenges") or []):
        cid = int(c.get("id") or 0)
        pct = float(c.get("pct") or 0)
        status = str(c.get("status") or "")
        st_cn = {"running": ("进行中", "ok"), "done": ("已结束", ""),
                 "upcoming": ("未开始", "warn")}.get(status, (status, ""))
        close = ""
        if status == "running":
            close = ('<form class=inline method=post action="/manage/act">'
                     f'<input type=hidden name=gid value="{gid}">'
                     f'<input type=hidden name=cid value="{cid}">'
                     '<input type=hidden name=tab value=challenge>'
                     '<button class=ghost name=op value=close>提前结束</button></form>')
        rows.append(
            f'<div class=row><div class=g><b>{_e(c.get("title"))}</b>'
            f'<div class=sub>{_e(c.get("start_date"))} ~ {_e(c.get("end_date"))}'
            f' · 目标 {_e(c.get("target_text") or c.get("target"))}'
            f' · 全组 {pct:.0f}%</div></div>'
            f'<span class="pill {st_cn[1]}">{_e(st_cn[0])}</span>{close}</div>')
    return ('<div class=card>' + ("".join(rows) or
            '<div class=empty>还没有赛事。</div>') + '</div>')


def _tab_audit(me, gid: int) -> str:
    import groups as G

    try:
        r = G.api_audit(me, gid, {"limit": ["50"]})
    except Exception as e:      # noqa: BLE001
        return '<div class=card><div class=empty>读不到记录：' + _e(e) + '</div></div>'
    rows = []
    for x in (r.get("rows") or []):
        at = dt.datetime.fromtimestamp(int(x.get("at") or 0)).strftime("%m-%d %H:%M")
        rows.append(f'<tr><td class=sub>{_e(at)}</td><td>{_e(x.get("actor"))}</td>'
                    f'<td>{_e(AUDIT_CN.get(str(x.get("action")), x.get("action")))}</td>'
                    f'<td class=sub>{_e(x.get("target"))}</td></tr>')
    return ('<div class=card><table><tr><th>时间</th><th>谁</th><th>做了什么</th>'
            '<th>对象</th></tr>' + "".join(rows) + '</table></div>')


def _tab_settings(me, gid: int, g) -> str:
    mode = str(g.get("join_mode") or "open")
    return (
        '<div class=card><h2 style="margin-top:0">加入方式</h2>'
        '<form method=post action="/manage/act">'
        f'<input type=hidden name=gid value="{gid}">'
        '<input type=hidden name=tab value=settings>'
        '<select name=mode style="margin-bottom:12px">'
        f'<option value=open{" selected" if mode == "open" else ""}>扫码即入</option>'
        f'<option value=approve{" selected" if mode == "approve" else ""}>需要批准</option>'
        '</select><button name=op value=join_mode>保存</button></form>'
        '<p class=sub style="margin-bottom:0">改成「需要批准」后，新申请会出现在'
        '「审核」里。</p></div>'
        '<div class=card><h2 style="margin-top:0">组名</h2>'
        '<form method=post action="/manage/act">'
        f'<input type=hidden name=gid value="{gid}">'
        '<input type=hidden name=tab value=settings>'
        f'<input name=name value="{_e(g.get("name"))}" maxlength=24 '
        'style="margin-bottom:12px"><button name=op value=rename>改名</button></form>'
        '</div>'
        '<div class=card><h2 style="margin-top:0">邀请码</h2>'
        '<form method=post action="/manage/act">'
        f'<input type=hidden name=gid value="{gid}">'
        '<input type=hidden name=tab value=settings>'
        '<button class=danger name=op value=rotate '
        'onclick="return confirm(\'换邀请码后，旧码立即失效。继续？\')">换一个邀请码</button>'
        '</form><p class=sub style="margin-bottom:0">只在有码被人转发到公开群时用。</p>'
        '</div>')


def export_csv(me, gid: int) -> tuple:
    """本周榜单导出。列与 App 里那份 CSV 一致，方便两边对账。"""
    import groups as G

    d = G.api_get(me, gid)
    g = d.get("group") or {}
    role = str(g.get("role") or "")
    if role not in MANAGER_ROLES:
        return (403, "没有权限".encode("utf-8"), "text/plain; charset=utf-8", {})
    board = G.api_board(me, gid, {"range": ["week"]})
    if not board.get("ok"):
        return (400, (board.get("error") or "拉不到榜单").encode("utf-8"),
                "text/plain; charset=utf-8", {})
    lines = ["昵称,本周里程 km,天数,次数"]
    for x in (board.get("rows") or []):
        km = float(x.get("distance_m") or 0) / 1000.0
        alias = str(x.get("alias") or "").replace('"', '""')
        lines.append(f'"{alias}",{km:.1f},{int(x.get("days") or 0)},{int(x.get("acts") or 0)}')
    if board.get("hidden"):
        lines.append(f'"#另有 {int(board["hidden"])} 人关闭了共享"')
    name = f'group-{gid}-{board.get("from") or ""}-{board.get("to") or ""}.csv'
    # BOM：Excel 打开 UTF-8 CSV 不加 BOM 会乱码
    data = ("\ufeff" + "\n".join(lines)).encode("utf-8")
    return (200, data, "text/csv; charset=utf-8",
            {"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------- 动作
def handle_post(path: str, form: dict, cookie: str | None) -> tuple:
    """返回 (code, body, ctype, headers)。写操作一律 303 回原页面。"""
    import groups as G

    def s(k, d=""):
        v = form.get(k)
        return str(v[0]) if isinstance(v, list) and v else d

    me = me_of(cookie)
    if path == "/manage" or path == "/manage/":
        return (303, b"", "text/plain", {"Location": "/manage"})
    if path == "/manage/send":
        account = s("account").strip()
        import app as A

        try:
            r = A.api_sms_send({"account": account}, "web")
        except Exception as e:      # noqa: BLE001 - Gate 也走这里
            return (200, login_page(str(getattr(e, "message", e))), "text/html; charset=utf-8", {})
        masked = account
        return (200, login_page(account=masked, sent=True), "text/html; charset=utf-8", {})
    if path == "/manage/login":
        import app as A

        account = s("account").strip()
        code = s("code").strip()
        try:
            r = A.api_auth({"account": account, "code": code,
                            "device_name": "RunnerCoach 网页后台"}, "web")
        except Exception as e:      # noqa: BLE001
            return (200, login_page(str(getattr(e, "message", e)), account, True),
                    "text/html; charset=utf-8", {})
        return (303, b"", "text/plain",
                {"Location": "/manage", "Set-Cookie": _cookie_header(str(r["token"]))})
    if path == "/manage/logout":
        return (303, b"", "text/plain",
                {"Location": "/manage",
                 "Set-Cookie": f"{COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"})
    if path == "/manage/act":
        if me is None:
            return (303, b"", "text/plain", {"Location": "/manage"})
        gid = int(s("gid") or 0)
        tab = s("tab") or "members"
        op = s("op")
        uk = s("uid")
        try:
            if op == "approve":
                G.api_decide(me, gid, int(uk), True)
                return _back(gid, "pending", "已批准")
            if op == "reject":
                G.api_decide(me, gid, int(uk), False)
                return _back(gid, "pending", "已拒绝")
            if op == "kick":
                G.api_remove(me, gid, int(uk))
                return _back(gid, "members", "已移出")
            if op == "close":
                G.api_challenge_close(me, gid, int(s("cid") or 0))
                return _back(gid, "challenge", "赛事已提前结束")
            if op == "join_mode":
                G.api_set_join_mode(me, gid, s("mode"))
                return _back(gid, "settings", "加入方式已更新")
            if op == "rename":
                G.api_patch(me, gid, {"name": s("name").strip()})
                return _back(gid, "settings", "组名已更新")
            if op == "rotate":
                G.api_rotate(me, gid)
                return _back(gid, "settings", "邀请码已更换")
        except Exception as e:      # noqa: BLE001 - Gate 带中文说明
            return _back(gid, tab, str(getattr(e, "message", e)))
        return _back(gid, tab, "没有这个操作")
    return (404, b"", "text/plain", {})


def handle_get(path: str, query: dict, cookie: str | None) -> tuple:
    me = me_of(cookie)
    if path in ("/manage", "/manage/"):
        if me is None:
            return (200, login_page(), "text/html; charset=utf-8", {})
        return (200, groups_page(me), "text/html; charset=utf-8", {})
    if path.startswith("/manage/export/"):
        if me is None:
            return (303, b"", "text/plain", {"Location": "/manage"})
        raw = path[len("/manage/export/"):]
        try:
            gid = int(raw.split(".")[0])
        except ValueError:
            return (404, b"", "text/plain", {})
        return export_csv(me, gid)
    if path.startswith("/manage/g/"):
        if me is None:
            return (303, b"", "text/plain", {"Location": "/manage"})
        try:
            gid = int(path[len("/manage/g/"):].split("/")[0])
        except ValueError:
            return (404, b"", "text/plain", {})
        tab = (query.get("tab") or ["members"])[0]
        msg = (query.get("msg") or [""])[0][:80]
        return (200, group_page(me, gid, tab, msg), "text/html; charset=utf-8", {})
    return (404, b"", "text/plain", {})
