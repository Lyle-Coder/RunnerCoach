# -*- coding: utf-8 -*-
"""训练小组的页面：账号、组列表、组详情。

放在独立模块而不是塞进 ui.py，理由和 plan 那几个页面一样——ui.py 已经
承担了今日/历史/计划/我的四套页面，再往里加会让「改一处影响一片」。
这里只依赖 `ui.page/sec/esc` 三个模板函数，不反向依赖。

写表单一律 POST + 303 回跳（PRG）：刷新页面不会重复提交上一次的操作，
这在「建组」「解散」这类不可逆动作上尤其重要。
"""
from __future__ import annotations

import datetime as dt

from .ui import esc, page, sec


def _msg(message: str) -> str:
    if not message:
        return ""
    tone = "nt" if "失败" in message or "错误" in message or "不能" in message else "nt ok"
    return f'<div class="{tone}">{esc(message)}</div>'


def _tok(token: str) -> str:
    return f'<input type="hidden" name="token" value="{esc(token)}">'


def _km(m: int | float | None) -> str:
    v = float(m or 0) / 1000
    return f"{v:.1f}" if v < 100 else f"{v:.0f}"


# ---------------------------------------------------------------- 账号
def account_page(token: str, message: str = "", account: str = "",
                 dev_code: str = "") -> str:
    from . import account as AC

    st = AC.state()
    msg = _msg(message)
    if st["logged_in"]:
        body = (msg
                + sec("当前账号", '<div class="card">'
                      f'<div class="kvline"><span>账号</span><b>{esc(st["label"] or "已登录")}</b></div>'
                      '<p class="note">令牌只存在这台手机里。换手机重新登录就能找回组和榜单，'
                      '不需要备份什么。</p></div>')
                + f'<form action="/__account/logout" method="post">{_tok(token)}'
                  '<div class="pager"><button class="btn" type="submit">退出登录</button></div></form>')
        return page("ACCOUNT", "账号", "已登录", body,
                    foot="登录令牌只存在这台手机里，不会上传到别处。",
                    back=("返回我的", "/me"))

    tip = ""
    if dev_code:
        tip = (f'<p class="note">开发模式验证码：<b>{esc(dev_code)}</b>'
               '（正式环境会发到邮箱/短信，不会显示在这里）</p>')
    form_send = (f'<form action="/__account/send" method="post">{_tok(token)}'
                 '<div class="card">'
                 '<div class="kvline"><span>手机号或邮箱</span><b>'
                 f'<input type="text" name="account" value="{esc(account)}" '
                 'placeholder="you@example.com" autocomplete="username">'
                 '</b></div></div>'
                 '<p class="note">首次登录会自动注册。登录只用于训练小组与平台额度，'
                 '不绑定跑步数据。</p>'
                 '<div class="pager"><button class="btn primary" type="submit">发送验证码</button>'
                 '</div></form>')
    form_login = (f'<form action="/__account/login" method="post">{_tok(token)}'
                  f'<input type="hidden" name="account" value="{esc(account)}">'
                  '<div class="card">'
                  '<div class="kvline"><span>验证码</span><b>'
                  '<input type="text" name="code" inputmode="numeric" maxlength="6" '
                  'placeholder="6 位数字" autocomplete="one-time-code">'
                  '</b></div></div>'
                  + tip +
                  '<div class="pager"><button class="btn primary" type="submit">登录</button></div>'
                  '</form>')
    body = (msg
            + sec("登录", '<div class="card">' + form_send + '</div>')
            + (sec("验证码", '<div class="card">' + form_login + '</div>') if account else ""))
    return page("ACCOUNT", "账号", "登录后可以建组、加入朋友的组", body,
                foot="登录令牌只存在这台手机里，不会上传到别处。",
                back=("返回我的", "/me"))


# ---------------------------------------------------------------- 组列表
def groups_page(token: str, message: str = "") -> str:
    from . import account as AC, group as G

    st = AC.state()
    msg = _msg(message)
    if not st["logged_in"]:
        body = (msg + '<div class="card"><p class="note">训练小组需要一个账号：'
                '建组、加入朋友的组、看本周榜都靠它。只上传每日里程合计，'
                '不上传轨迹和配速。</p>'
                '<div class="pager"><a class="btn primary" href="/account">去登录</a></div>'
                '</div>')
        return page("GROUP", "训练小组", "还没登录", body,
                    foot="只上传每日里程合计到你的账号，不上传轨迹、配速和位置。",
                    back=("返回我的", "/me"))

    r = G.groups()
    if not r.get("ok"):
        err = r.get("error") or "拉不到小组列表"
        if r.get("need_login"):
            body = (msg + '<div class="card"><p class="note">登录已过期，重新登录一下。</p>'
                    '<div class="pager"><a class="btn primary" href="/account">去登录</a></div>'
                    '</div>')
        else:
            body = (msg + f'<div class="card"><p class="note">{esc(err)}</p>'
                    '<div class="pager"><a class="btn" href="/groups">重试</a></div></div>')
        return page("GROUP", "训练小组", "连接有问题", body,
                    foot="只上传每日里程合计到你的账号，不上传轨迹、配速和位置。",
                    back=("返回我的", "/me"))

    items = r.get("groups") or []
    rows = ""
    for g in items:
        role = {"owner": "组长", "admin": "管理员"}.get(g.get("role") or "", "成员")
        rows += (f'<a class="grow" href="/groups/{g["id"]}">'
                 f'<span class="gname">{esc(g.get("name"))}</span>'
                 f'<span class="gmeta">{int(g.get("member_count") or 0)} 人 · {esc(role)}</span>'
                 '<span class="chev">›</span></a>')
    if not rows:
        rows = '<p class="note">还没有加入小组。自己建一个，或者拿邀请码加入。</p>'

    join_form = (f'<form action="/__group/join" method="post">{_tok(token)}'
                 '<div class="card">'
                 '<div class="kvline"><span>邀请码</span><b>'
                 '<input type="text" name="code" maxlength="8" placeholder="8 位" '
                 'autocapitalize="characters" autocomplete="off"></b></div>'
                 '<div class="kvline"><span>我的昵称</span><b>'
                 '<input type="text" name="alias" maxlength="16" placeholder="榜上显示的名字"></b></div>'
                 '</div>'
                 '<div class="pager"><button class="btn" type="submit">加入</button></div></form>')
    create_form = (f'<form action="/__group/create" method="post">{_tok(token)}'
                   '<div class="card">'
                   '<div class="kvline"><span>组名</span><b>'
                   '<input type="text" name="name" maxlength="20" placeholder="例如 秦岭跑团"></b></div>'
                   '<div class="kvline"><span>简介</span><b>'
                   '<input type="text" name="intro" maxlength="40" placeholder="选填"></b></div>'
                   '<div class="kvline"><span>人数上限</span><b>'
                   '<input type="number" name="max_members" min="2" max="200" step="1" '
                   'value="20"></b></div>'
                   '<div class="kvline"><span>我的昵称</span><b>'
                   '<input type="text" name="alias" maxlength="16" placeholder="榜上显示的名字"></b></div>'
                   '</div>'
                   '<div class="pager"><button class="btn primary" type="submit">建组</button></div>'
                   '</form>')

    chips = "".join([f'<span class="chip">{len(items)} 个组</span>']) if items else ""
    body = (msg
            + sec("我的小组", '<div class="card">' + rows + '</div>')
            + sec("加入小组", '<div class="card">' + join_form + '</div>')
            + sec("创建小组", '<div class="card">' + create_form + '</div>'))
    return page("GROUP", "训练小组", "本周榜只在组内可见", body, chips,
                foot="只上传每日里程合计到你的账号，不上传轨迹、配速和位置。",
                back=("返回我的", "/me"))


# ---------------------------------------------------------------- 组详情
def group_page(token: str, gid: int, message: str = "", rng: str = "week") -> str:
    from . import group as G

    today = dt.date.today().isoformat()
    msg = _msg(message)
    # 一次拿齐四块（detail / board / challenges / season）。老服务端没有
    # /home 就退回逐个请求——四次握手约 0.5 秒，能省则省。
    _h = {}
    try:
        _h = G.home(gid, rng, today)
    except Exception:      # noqa: BLE001
        _h = {}
    if not _h.get("ok"):
        _h = {}

    d = _h.get("detail") or G.detail(gid)
    if not d.get("ok"):
        err = d.get("error") or "打不开这个组"
        body = (msg + f'<div class="card"><p class="note">{esc(err)}</p>'
                '<div class="pager"><a class="btn" href="/groups">返回小组</a></div></div>')
        return page("GROUP", "训练小组", "", body, back=("返回小组", "/groups"))

    g = d["group"]
    role = g.get("role") or "member"
    is_admin = role in ("owner", "admin")
    my_share = (g.get("me") or {}).get("share") or "summary"

    b = _h.get("board") or G.board(gid, rng, today)
    cl = _h.get("challenges") or G.challenge_list(gid, today)
    total_m = (b.get("summary") or {}).get("total_distance_m") or 0

    # 本周榜。「组长」跟着名字走——以前单开一个「成员」区块专门说这件事，
    # 而成员就是榜上这些人，等于同一批名字排两遍。
    owner_alias = next((m.get("alias") for m in (g.get("members") or [])
                        if m.get("role") == "owner"), "")
    rank_rows = ""
    for i, x in enumerate(b.get("rows") or [], 1):
        cls = "rank me" if x.get("is_me") else "rank"
        name = esc(x.get("alias"))
        if x.get("is_me"):
            name += '<span class="sub">我</span>'
        if owner_alias and x.get("alias") == owner_alias and not x.get("is_me"):
            name += '<span class="sub">组长</span>'
        days = int(x.get("days") or 0)
        rank_rows += (f'<div class="{cls}"><span class="no">{i}</span>'
                      f'<span class="nm">{name}<span class="sub">{days} 天</span></span>'
                      f'<span class="km">{_km(x.get("distance_m"))}</span></div>')
    hidden = int(b.get("hidden") or 0)
    if not rank_rows:
        rank_rows = '<p class="note">本周还没有人上报。打开 App 才会同步，不跑后台任务。</p>'
    if hidden:
        rank_rows += f'<p class="note">另有 {hidden} 人关闭了共享，不参与排名。</p>'

    # 赛事
    ch_rows = ""
    for c in (cl.get("challenges") or []):
        pct = float(c.get("pct") or 0)
        done = pct >= 100
        div = 1000 if c.get("kind") == "distance" else 1
        unit = "km" if c.get("kind") == "distance" else "次"
        cur = float(c.get("current") or 0) / div
        tgt = float(c.get("target") or 0) / div
        fmt = "{:.1f}" if unit == "km" else "{:.0f}"
        state_txt = {"running": "进行中", "closed": "已结束", "ended": "已到期"}.get(
            c.get("status") or "", "")
        ch_rows += (f'<div class="chal"><div class="t">{esc(c.get("title"))}</div>'
                    f'<div class="m">{fmt.format(cur)} / {fmt.format(tgt)} {unit} · '
                    f'{pct:.0f}% · {esc(state_txt)} · {esc(str(c.get("from"))[5:])}–'
                    f'{esc(str(c.get("to"))[5:])}</div>'
                    f'<div class="bar{" done" if done else ""}">'
                    f'<i style="width:{min(100, pct):.0f}%"></i></div></div>')
    if not ch_rows:
        ch_rows = '<p class="note">还没有赛事。组长可以建一个共同目标，全组自动参与。</p>'

    chal_form = ""
    if is_admin:
        chal_form = (f'<form action="/__group/{gid}/challenge" method="post">{_tok(token)}'
                     '<div class="card">'
                     '<div class="kvline"><span>标题</span><b>'
                     '<input type="text" name="title" maxlength="30" placeholder="本周 20 公里"></b></div>'
                     '<div class="kvline"><span>类型</span><b>'
                     '<select name="kind"><option value="distance">累计里程</option>'
                     '<option value="acts">累计次数</option></select></b></div>'
                     '<div class="kvline"><span>目标（米/次）</span><b>'
                     '<input type="number" name="target" min="1" max="1000000" step="1" '
                     'placeholder="20000"></b></div>'
                     '<div class="kvline"><span>开始日</span><b>'
                     f'<input type="text" name="start_date" value="{esc(today)}" placeholder="YYYY-MM-DD"></b></div>'
                     '<div class="kvline"><span>结束日</span><b>'
                     '<input type="text" name="end_date" placeholder="YYYY-MM-DD"></b></div>'
                     '</div><p class="note">里程按米填（20000 = 20 km）。'
                     '周期最长 90 天，开始日不能早于 7 天前。</p>'
                     '<div class="pager"><button class="btn" type="submit">建赛事</button></div>'
                     '</form>')

    # 成员不再单开一块：榜上就是成员。「另有 N 人关闭了共享」已经跟在
    # 榜单行后面（rank_rows 里），这里不再重复一遍。

    # 设置：一行一项，不再是一条注释 + 一个按钮 + 一圈边框。
    my_alias = (g.get("me") or {}).get("alias") or ""
    mine = (f'<form action="/__group/{gid}/alias" method="post">{_tok(token)}'
            '<div class="setrow"><span>昵称</span>'
            f'<input type="text" name="alias" maxlength="16" value="{esc(my_alias)}" '
            'placeholder="组里怎么叫你" aria-label="组内昵称">'
            '<button class="btn sm" type="submit">改</button></div></form>'
            f'<form action="/__group/{gid}/share" method="post">{_tok(token)}'
            '<div class="setrow"><span>共享</span>'
            '<select name="share" aria-label="共享档位">'
            f'<option value="summary"{" selected" if my_share == "summary" else ""}>共享里程</option>'
            f'<option value="off"{" selected" if my_share == "off" else ""}>不共享</option>'
            '</select>'
            '<button class="btn sm" type="submit">存</button></div></form>'
            f'<form action="/__group/{gid}/report" method="post">{_tok(token)}'
            '<div class="setrow"><span>上报</span>'
            '<span class="note" style="flex:1 1 auto;margin:0">同步时会自动上报，'
            '这里是不想等的时候用</span>'
            '<button class="btn sm" type="submit">立即</button></div></form>')

    # 管理折叠：邀请码、换码、退出都是低频动作，默认收起。
    admin_body = ""
    if is_admin and g.get("invite_code"):
        admin_body += (f'<div class="kvline"><span>邀请码</span>'
                       f'<b>{esc(g["invite_code"])}</b></div>')
    admin_body += '<div class="pager">'
    if is_admin:
        admin_body += (f'<form action="/__group/{gid}/rotate" method="post">{_tok(token)}'
                       '<button class="btn sm" type="submit">换邀请码</button></form>')
    if role != "owner":
        admin_body += (f'<form action="/__group/{gid}/leave" method="post">{_tok(token)}'
                       '<button class="btn sm" type="submit">退出小组</button></form>')
    admin_body += '</div>'
    if role == "owner":
        admin_body += '<p class="note">你是组长。要先转让给别人，或者解散这个组。</p>'
    admin_fold = ('<details class="fold"><summary>邀请码与管理'
                  '<span class="fx">›</span></summary>' + admin_body + '</details>')

    # 建赛事也是折叠：一个月用一次的表单不该常驻占掉半屏。
    chal_fold = ""
    if chal_form:
        chal_fold = ('<details class="fold"><summary>建一个赛事'
                     '<span class="fx">›</span></summary>' + chal_form + '</details>')

    chips = (f'<span class="chip">{int(g.get("member_count") or 0)} 人</span>'
             f'<span class="chip">{_km(total_m)} km '
             + ("本月" if rng == "month" else "本周") + '</span>')

    # 本周成绩条：我在组里第几、多少分、穿着哪件衫。
    # 这是「竞赛感」最省地方的表达——一行字，不做榜单页。取不到就整块不显示，
    # 组页不能因为一个新接口挂掉就打不开。
    try:
        _se = _h.get("season") or G.season(gid, today)
    except Exception:      # noqa: BLE001 - 老服务端没有 /season，降级即可
        _se = {}
    score_line = ""
    if _se.get("ok") and (_se.get("people") or []):
        _me = _se.get("me") or {}

        def _bits(shirts):
            return "".join(
                f'<span class="sbit"><i style="background:{esc(x.get("color"))}"></i>'
                f'{esc(x.get("name"))}</span>'
                for x in (shirts or []))

        if _me:
            _tt = str(_me.get("title") or "")
            score_line = (
                '<div class="sline">'
                f'<span class="rk">我 · 第 {int(_me.get("rank") or 0)} 名</span>'
                f'<b>{float(_me.get("score") or 0):g}<i>分</i></b>'
                + (f'<span class="tt">{esc(_tt)}</span>' if _tt else "")
                + _bits(_me.get("shirts")) + "</div>")
        win = []
        for sh in _se.get("shirts") or []:
            names = [str(w.get("alias") or "") for w in (sh.get("winners") or [])]
            if names:
                win.append(f'<span class="sbit"><i style="background:'
                           f'{esc(sh.get("color"))}"></i>{esc(sh.get("name"))} '
                           f'{esc("/".join(names))}</span>')
        if win:
            score_line += '<div class="sline2">' + "".join(win) + "</div>"
    if not score_line:
        score_line = ""

    # 远征入口。放在榜单之前：榜单是每天看的，远征是这一季的进度，
    # 两者都不该埋在页面底部。只做入口不拉 state——组详情页要秒开。
    trail = ('<div class="card"><a class="grow" href="/run?gid=' + str(gid) + '">'
             '<div class="gname">雪线之前</div>'
             '<div class="gmeta">这一季的路</div>'
             '<div class="chev">›</div></a></div>')

    # 月榜/周榜互切。服务端一直支持 range=month，只是页面以前不读这个参数，
    # 入口就被写死成关闭（`if False`）——那是死开关，不是功能。
    other = "month" if rng == "week" else "week"
    more = ("月榜" if other == "month" else "周榜", f"/groups/{gid}?range={other}")

    body = (msg
            + score_line
            + sec("远征", trail)
            + sec("月榜" if rng == "month" else "本周榜",
                  '<div class="flat">' + rank_rows + '</div>',
                  more=more)
            + sec("赛事", '<div class="flat">' + ch_rows + '</div>' + chal_fold)
            + sec("设置", '<div class="flat">' + mine + '</div>')
            + sec("管理", admin_fold))
    return page("GROUP", g.get("name") or "小组",
                f'{int(g.get("member_count") or 0)} 人 · '
                + {"owner": "我是组长", "admin": "我是管理员"}.get(role, "我是成员"),
                body, chips,
                foot="只上传每日里程合计到你的账号，不上传轨迹、配速和位置。",
                back=("返回小组", "/groups"))
