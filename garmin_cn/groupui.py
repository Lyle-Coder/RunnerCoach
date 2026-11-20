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


# 复制邀请码：**不能走 Kotlin 桥**（Bridge 里没有剪贴板动作，加一个就得重打 APK），
# 所以用 WebView 认的老办法：造一个隐藏 textarea + execCommand('copy')，
# 它必须在用户手势里同步执行，所以直接挂在 onclick 上，不做异步。
# navigator.clipboard 只有 https/localhost 才是安全上下文，file:// 下多半没有，只作兜底。
_COPY_JS = """<script>
function gcCopy(btn, txt){
  var t = document.createElement('textarea');
  t.value = txt; t.setAttribute('readonly', '');
  t.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0';
  document.body.appendChild(t);
  t.select(); t.setSelectionRange(0, txt.length);
  var ok = false;
  try { ok = document.execCommand('copy'); } catch (e) {}
  document.body.removeChild(t);
  if (!ok && navigator.clipboard) { navigator.clipboard.writeText(txt); ok = true; }
  var old = btn.getAttribute('data-t') || btn.textContent;
  btn.setAttribute('data-t', old);
  btn.textContent = ok ? '已复制' : '请长按选中';
  btn.disabled = true;
  setTimeout(function(){ btn.textContent = old; btn.disabled = false; }, 1400);
}
</script>"""


def _with_copy(html: str) -> str:
    """给页面挂上 gcCopy。脚本贴在 </body> 前，只挂用得着的页面。"""
    return html.replace("</body>", _COPY_JS + "</body>", 1)


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
        _me = AC.me()
        _nick = str((_me or {}).get("nickname") or "") if _me.get("ok") else ""
        body = (msg
                + sec("当前账号", '<div class="card">'
                      f'<div class="kvline"><span>账号</span><b>{esc(st["label"] or "已登录")}</b></div>'
                      '<p class="note">令牌只存在这台手机里。换手机重新登录就能找回组和榜单，'
                      '不需要备份什么。</p></div>')
                # 账号昵称是建组/入组时的默认名字，在这里改一次就够
                + sec("昵称", '<div class="flat">'
                      f'<form action="/__account/nickname" method="post">{_tok(token)}'
                      '<div class="setrow"><span>昵称</span>'
                      f'<input type="text" name="nickname" maxlength="16" value="{esc(_nick)}" '
                      'placeholder="榜上显示的名字" aria-label="账号昵称">'
                      '<button class="btn sm" type="submit">改</button></div></form>'
                      '<div class="tinyline">建组、入组时默认用它；组里还可以单独改。</div>'
                      '</div>')
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
                  '</b></div>'
                  # 昵称放在这里而不是建组时问：一个人只有一个名字，
                  # 每建一个组都重新填一次，只会得到「aa」「111」这种。
                  '<div class="kvline"><span>昵称</span><b>'
                  '<input type="text" name="nickname" maxlength="16" '
                  'placeholder="榜上显示的名字（首次注册用）">'
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
def groups_page(token: str, message: str = "", force_list: bool = False) -> str:
    """小组列表。

    `force_list=True`（URL 带 `?list=1`）时**不做单组直达**。单组直达是为了
    「点小组最想看排名」，但它把列表页整个跳过了——只加了一个组的用户
    永远看不到「再加入一个 / 建组」的入口，从组详情点「全部小组」也会
    立刻被弹回同一个组，看起来像按钮没反应。
    """
    from . import account as AC, group as G

    st = AC.state()
    msg = _msg(message)
    if not st["logged_in"]:
        body = (msg + '<div class="card"><p class="note">训练小组需要一个账号：'
                '建组、加入朋友的组、看本周榜都靠它。只上传每日里程合计，'
                '不上传原始轨迹和位置。</p>'
                '<div class="pager"><a class="btn primary" href="/account">去登录</a></div>'
                '</div>')
        return page("GROUP", "训练小组", "还没登录", body,
                    foot="只上传里程合计和脱敏轨迹剪影，不含坐标和位置。",
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
                    foot="只上传里程合计和脱敏轨迹剪影，不含坐标和位置。",
                    back=("返回我的", "/me"))

    items = r.get("groups") or []
    # 只有一个组就直接进详情：点「小组」最想看的是排名，而不是一行组名。
    # 组多了才需要「先选哪个」这一步，那时列表本身就有意义。
    # **带了 message 就不能直达**：建组/加入失败是 303 回跳 `/groups?msg=…`，
    # 一进列表就被弹到组详情，那句错误提示永远没人看见——
    # 「点了建组没反应、也建不出来」就是这么来的。
    if len(items) == 1 and not force_list and not message:
        try:
            return group_page(token, int(items[0]["id"]))
        except Exception:      # noqa: BLE001 - 详情页挂了也要能回到列表
            pass
    # 一组一卡。卡上四层：组名+角色 → 概述（人数·全组里程）→ 我的名次与里程
    # → 目标进度与成就（六件衫 + 探索徽章）。原来一行一条，三个组看完只知道
    # 「我有三个组」，不知道每个组里我过得怎么样。
    cards = ""
    for g in items:
        role = {"owner": "组长", "admin": "管理员"}.get(g.get("role") or "", "成员")
        h = g.get("honors") or {}
        wk = g.get("week") or {}
        n = int(g.get("member_count") or 0)
        tot = float(h.get("total_km") or 0)
        km = float(wk.get("km") or 0)
        rk, tt = h.get("rank"), int(h.get("total") or 0)

        meta = (f'{n} 人 · 本周全组 {tot:g} km' if tot > 0
                else f'{n} 人 · 本周还没有里程')

        # 名次：一个人组里「第 1 名」没有信息量，改说「本周成绩」
        if rk and tt > 1:
            who = f'第 {int(rk)} 名<i>共 {tt} 人</i>'
        elif rk and tt == 1:
            who = '本周成绩'
        elif km > 0:
            who = '本周还没有名次'
        else:
            who = '本周还没有记录'
        kmtxt = f'{km:g}<i>km</i>' if km > 0 else ""

        # 目标：有分母才画条，没定目标就整块不出现（不留一条空条）
        goal = ""
        gk = h.get("goal_km")
        if gk:
            gk = float(gk)
            pct = max(0.0, min(1.0, km / gk)) if gk > 0 else 0.0
            goal = ('<div class="gc-goal">'
                    f'<div class="bar"><i style="width:{pct * 100:.0f}%"></i></div>'
                    f'<p>本周目标 {gk:g} km · 完成 {pct * 100:.0f}%</p></div>')

        # 成就：六件衫画满六个位置，没拿到的空心——「缺几件」得数得出来
        hon = ""
        shirts = h.get("shirts") or []
        if shirts:
            got = sum(1 for x in shirts if x.get("got"))
            ic = "".join(
                _jersey(str(x.get("color") or ""), str(x.get("pattern") or ""),
                        bool(x.get("got")), "",
                        f'{x.get("name") or ""}·{x.get("title") or ""}')
                for x in shirts)
            ex = ""
            if int(h.get("explorer") or 0) >= int(h.get("explore_min") or 5):
                ex = _explore_icon("jx", "探索徽章")
            hon = ('<div class="gc-hon">' + ic
                   + f'<span class="hn">荣誉 {got}/{len(shirts)}</span>{ex}</div>')

        cards += (f'<a class="gcard" href="/groups/{int(g["id"])}">'
                  '<div class="gc-h">'
                  f'<span class="gc-n">{esc(g.get("name"))}</span>'
                  f'<span class="pill ghost">{esc(role)}</span>'
                  '<span class="chev">›</span></div>'
                  f'<p class="gc-m">{esc(meta)}</p>'
                  f'<div class="gc-me"><span class="rk">{who}</span>'
                  f'<span class="km">{kmtxt}</span></div>'
                  + goal + hon + '</a>')
    if not cards:
        cards = '<p class="note">还没有加入小组。自己建一个，或者拿邀请码加入。</p>'

    join_form = (f'<form action="/__group/join" method="post">{_tok(token)}'
                 '<div class="card">'
                 '<div class="kvline"><span>邀请码</span><b>'
                 '<input type="text" name="code" maxlength="8" placeholder="8 位" '
                 'autocapitalize="characters" autocomplete="off"></b></div>'
                 '<div class="kvline"><span>我的昵称</span><b>'
                 '<input type="text" name="alias" maxlength="16" placeholder="榜上显示的名字"></b></div>'
                 '<div class="pager"><button class="btn" type="submit">加入</button></div>'
                 '</div></form>')
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
                   '<div class="pager"><button class="btn primary" type="submit">建组</button></div>'
                   '</div></form>')

    # 建组的两个硬限制（服务端）：一个人同时只能当**一个**组的组长；
    # 最多同时加入 3 个组。命中任一条还摆着表单，用户填完只会拿到 409，
    # 而且（改之前）连那句 409 都看不见。这里直接把表单换成原因 + 出口。
    owner_g = next((g for g in items if (g.get("role") or "") == "owner"), None)
    blocked = ""
    if owner_g:
        blocked = (
            '<div class="card flat"><p class="note">你已经当着「'
            f'{esc(owner_g.get("name"))}」的组长，一个人同时只能当一个组的组长。'
            '想建新组，先去那个组的设置里解散或转让。</p>'
            f'<div class="pager"><a class="btn sm" '
            f'href="/groups/{int(owner_g["id"])}/settings">去处理</a></div></div>')
    elif len(items) >= 3:
        blocked = ('<div class="card flat"><p class="note">'
                   '已经在 3 个组里，最多同时加入 3 个。</p></div>')
    if blocked:
        create_form = blocked

    chips = "".join([f'<span class="chip">{len(items)} 个组</span>']) if items else ""
    if items:
        # 已经有组的人，建组/加入是低频动作：折成一行放在底部，
        # 别让它和「我的小组」抢版面。
        body = (msg
                + sec("我的小组", cards)
                # force_list 时默认展开：用户是从「加入另一个组」点进来的，
                # 再折一次等于告诉他「这里没有」。
                + ('<details class="fold fold-lg" open>' if force_list
                   else '<details class="fold">')
                + ('<summary>再加入一个组' if blocked
                   else '<summary>再加入一个，或者自己建一个')
                + '<span class="fx">›</span></summary>'
                  + '<div class="flat">' + join_form + create_form + '</div></details>')
    else:
        # 一个组都没有：这两个表单就是这一页的全部意义，平铺出来
        body = (msg
                + sec("加入小组", '<div class="card">' + join_form + '</div>')
                + sec("创建小组", '<div class="card">' + create_form + '</div>'))
    return page("GROUP", "训练小组", "本周榜只在组内可见", body, chips,
                foot="只上传里程合计和脱敏轨迹剪影，不含坐标和位置。",
                back=("返回我的", "/me"))


# ---------------------------------------------------------------- 设置
def settings_page(token: str, gid: int, message: str = "") -> str:
    """小组设置：我的（昵称 / 共享 / 上报）+ 组（邀请码 / 换码 / 转让 / 退出）。

    这些原来是组页最底下的一块。组页主体是排名和动态，把「改昵称」这种
    低频操作压在最下面，既挤，也不像"随时能改"的东西——用户的原话是
    「放在最下面太突兀」。挪到页头右上角一个齿轮里。
    """
    from . import group as G

    d = G.detail(gid)
    if not d.get("ok"):
        body = (_msg(message)
                + f'<div class="card"><p class="note">{esc(d.get("error") or "打不开设置")}</p>'
                f'<div class="pager"><a class="btn" href="/groups/{gid}">返回小组</a></div></div>')
        return page("GROUP", "小组设置", "", body, back=("返回小组", f"/groups/{gid}"))

    g = d["group"]
    role = g.get("role") or "member"
    is_owner = role == "owner"
    is_admin = role in ("owner", "admin")
    my_share = (g.get("me") or {}).get("share") or "summary"
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
            '<span class="note" style="flex:1 1 auto;margin:0">同步时自动上报，'
            '这里是不想等的时候用</span>'
            '<button class="btn sm" type="submit">立即</button></div></form>')

    grp = ""
    if is_admin and g.get("invite_code"):
        _code = esc(g["invite_code"])
        # 「复制」放前面、「换一个」放后面：发码是高频动作，换码一年一次。
        # 换码是 POST 表单（会作废旧码），所以它仍然得留在 form 里。
        grp += (f'<div class="setrow"><span>邀请码</span>'
                f'<b style="letter-spacing:1.2px;font-size:13px">{_code}</b>'
                '<button class="btn sm" type="button" '
                f'onclick="gcCopy(this,\'{_code}\')">复制</button>'
                f'<form action="/__group/{gid}/rotate" method="post">{_tok(token)}'
                '<button class="btn sm" type="submit">换一个</button></form></div>')
    # 转让：只有组长能转，而且是可逆的（转回来就行），所以放在普通设置里，
    # 解散这种不可逆的没放——它该有自己的二次确认流程，不该和改昵称并排。
    if is_owner:
        _opts = "".join(
            f'<option value="{int(m["user_id"])}">{esc(m["alias"])}</option>'
            for m in (g.get("members") or []) if not m.get("is_me"))
        if _opts:
            grp += (f'<form action="/__group/{gid}/transfer" method="post">{_tok(token)}'
                    '<div class="setrow"><span>转让组长</span>'
                    f'<select name="uid" aria-label="转给谁">{_opts}</select>'
                    '<button class="btn sm" type="submit">转让</button></div></form>')
    if not is_owner:
        grp += (f'<form action="/__group/{gid}/leave" method="post">{_tok(token)}'
                '<div class="setrow"><span>退出</span>'
                '<span class="note" style="flex:1 1 auto;margin:0">'
                '退出后我在这个组的记录会删掉</span>'
                '<button class="btn sm" type="submit">退出小组</button></div></form>')
    else:
        # 组长不能直接退出，但**必须能解散**：一个人同时只能当 1 个组的组长，
        # 建新组时服务端会 409。没有解散入口的话，「建不了新组」就成了死结。
        grp += ('<p class="note">你是组长：先把组长转给别人才能退出，'
                '或者直接解散这个组。</p>'
                f'<details class="fold"><summary>解散这个组'
                '<span class="fx">›</span></summary>'
                '<div class="foldbody">'
                '<p class="note">解散后这个组、榜和赛事都不再出现，成员也看不到；'
                '这个动作不能撤销。</p>'
                f'<form action="/__group/{gid}/archive" method="post">{_tok(token)}'
                '<label class="ckline"><input type="checkbox" name="confirm"'
                ' value="1"> 我知道解散不能撤销</label>'
                '<div class="pager"><button class="btn sm danger" type="submit">'
                '解散</button></div></form></div></details>')
    grp += (f'<a class="setrow" href="/groups/{gid}/map">'
            '<span>路线共享</span>'
            '<span class="note" style="flex:1 1 auto;margin:0">'
            '在足迹地图里开关</span><span class="chev">›</span></a>')
    # 加入别的组 / 新建一个：只有 1 个组的人，`/groups` 会直接被他自己的组
    # 接管，所以列表页那个入口永远看不到。这里给一条明确的路。
    # 带 list=1 强制出列表，并且表单是展开的。
    grp += ('<a class="setrow" href="/groups?list=1">'
            '<span>全部小组</span>'
            '<span class="note" style="flex:1 1 auto;margin:0">'
            '加入别的组，或者新建一个</span><span class="chev">›</span></a>')

    body = (_msg(message)
            + sec("我的", '<div class="flat">' + mine + '</div>')
            + sec("组", '<div class="flat">' + grp + '</div>')
            + '<p class="note">组里只收每日里程合计和活动概要；轨迹、配速、'
              '位置都不上传。关掉共享之后，这些数据在组内立刻不可见。</p>')
    return _with_copy(page(
        "GROUP", "小组设置",
        f'{esc(g.get("name"))} · '
        + {"owner": "我是组长", "admin": "我是管理员"}.get(role, "我是成员"),
        body, foot="只上传里程合计和脱敏轨迹剪影，不含坐标和位置。",
        back=("返回小组", f"/groups/{gid}")))


# ---------------------------------------------------------------- Bingo
def bingo_page(token: str, gid: int, message: str = "") -> str:
    """本周 Bingo 卡：5×5 格，连成一行/列/对角线算一条线。

    格子里只有「任务 + 是否点亮」，没有按钮——任务是根据每天上报的数据自动
    判的，用户不需要也不该手动勾选。手勾的 Bingo 就只是待办清单了。
    """
    from . import group as G

    today = dt.date.today().isoformat()
    r = G.bingo(gid, today)
    if not r.get("ok"):
        body = (_msg(message)
                + f'<div class="card"><p class="note">{esc(r.get("error") or "打不开这张卡")}</p>'
                f'<div class="pager"><a class="btn" href="/groups/{gid}">返回小组</a></div></div>')
        return page("GROUP", "Bingo", "", body, back=("返回小组", f"/groups/{gid}"))

    cells = r.get("cells") or []
    done = int(r.get("done") or 0)
    total = int(r.get("total") or 25)
    lines = int(r.get("line_count") or 0)

    grid = ""
    for c in cells:
        cls = "bcell"
        if c.get("task") == "free":
            cls += " on free"
        elif c.get("done"):
            cls += " on"
        grid += f'<div class="{cls}">{esc(c.get("text"))}</div>'

    body = (_msg(message)
            + sec("本周卡", f'<div class="bgrid">{grid}</div>'
                  '<p class="note">任务只看每天上报的跑步数据，跑完自动点亮。'
                  '连成一行、一列或一条对角线就算一条线；只做荣誉，不加赛季分。</p>'))
    # 不传 chips：副标题已经写了「10/25 格 · 1 条线」，再加两个 chip 是同一句话
    return page("GROUP", "Bingo", f"{done}/{total} 格 · {lines} 条线", body,
                foot="只上传里程合计和脱敏轨迹剪影，不含坐标和位置。",
                back=("返回小组", f"/groups/{gid}"))


# ---------------------------------------------------------------- 组内路线
MAP_W, MAP_H = 392, 300
# 分片时用矮一点的图幅：一片一张，三片就顶两屏，太高了反而看不到全貌。
SHARD_H = 190
# 「共同跑过」的格子上不是任何人的颜色，而是一个独立的信息层。
# 用白色而不是混色：蓝+绿混出来是青，看不出是两个人；白色在暗底上最亮，
# 一眼就知道「这块是两个人都踩过」。
SHARED_COLOR = "#F4F7FC"


# 一格由谁代表：跑量第一的人，且他的占比要到这个线。达不到就是「共同」。
# 0.65 而不是 0.5 以上随便一个数：两人各跑 900 / 800 米时，说「这块是 A 的」
# 没有意义；差到 2 倍以上才值得标成某个人的颜色。
OWNER_SHARE = 0.65


def _cell_owners(people) -> dict[str, tuple]:
    """每格 → (代表者 uid, 该人占比, 该格总里程)。

    「代表者」由跑量决定，不是由谁最后画决定。没有跑量数据（老客户端只发
    格子列表）时退回按人数平摊：两人各 0.5 < OWNER_SHARE，于是归为共同，
    和之前的行为一致。
    """
    per: dict[str, dict[int, float]] = {}
    for p in people:
        u = p.get("user_id")
        mm = p.get("meters") or {}
        for c in p.get("cells") or []:
            try:
                m = float(mm.get(c, 0) or 0)
            except (TypeError, ValueError):
                m = 0.0
            per.setdefault(c, {})[u] = m
    out: dict[str, tuple] = {}
    for c, d in per.items():
        tot = sum(d.values())
        best_u, best_m = max(d.items(), key=lambda x: x[1])
        share = (best_m / tot) if tot > 0 else (1.0 / len(d))
        out[c] = (best_u, share, tot)
    return out


def _shared_count(people) -> int:
    """共同格数（没有单一代表者的格子）。"""
    return sum(1 for _, sh, _ in _cell_owners(people).values() if sh < OWNER_SHARE)


def _weight_label(p: dict) -> str:
    """图例上的数字：有跑量就写里程，没有（老数据）退回格数。"""
    mm = p.get("meters") or {}
    try:
        km = sum(float(v or 0) for v in mm.values()) / 1000.0
    except (TypeError, ValueError):
        km = 0.0
    return f"{km:.1f}km" if km > 0.05 else f'{len(p.get("cells") or [])} 格'


def _op_of(meters: float) -> float:
    """该格总跑量 → 填充不透明度。跑得越多越实，一眼看出「这块是热区」。

    档位间距拉大（0.30 / 0.52 / 0.78）：0.42/0.60/0.80 在蓝色上几乎看不出
    差别，而「深浅代表跑量」这件事只有对比够强才成立。
    """
    if meters <= 0:
        return 0.52          # 老数据没有跑量，给个中间值
    if meters < 800:
        return 0.30
    if meters < 2000:
        return 0.52
    return 0.78


def map_page(token: str, gid: int, message: str = "") -> str:
    """组内路线图：每人一种颜色，把本周跑过的 500 m 格子画在底图上。

    这里画的是**格子**不是轨迹。格号是 500 m 见方的一片区域，还原不出你走的
    是哪条人行道；轨迹点是精确到米的行踪，两者不是一个量级。组页的承诺因此
    只在「格子」这一层成立，页面底部要把这句话写清楚。
    """
    import hashlib

    from . import basemap as BM
    from . import explore as EX
    from . import group as G

    today = dt.date.today().isoformat()
    r = G.map_data(gid, today)
    if not r.get("ok"):
        body = (_msg(message)
                + f'<div class="card"><p class="note">{esc(r.get("error") or "打不开地图")}</p>'
                f'<div class="pager"><a class="btn" href="/groups/{gid}">返回小组</a></div></div>')
        return page("GROUP", "组内路线", "", body, back=("返回小组", f"/groups/{gid}"))

    people = r.get("people") or []
    mine = next((p for p in people if p.get("is_me")), None)
    others = [p for p in people if not p.get("is_me")]

    all_cells: list[str] = []
    for p in people:
        all_cells.extend(p["cells"])

    svg = ""
    sharded = False
    if all_cells:
        # 跨省的组不能用一张图：取景要装下 900 km，zoom 只到 5 级，500 m 的
        # 格子剩不到一个像素。按地理分片，一片一张图。
        shards = EX.cluster_cells(all_cells)
        if len(shards) <= 1:
            pts: list[list[float]] = []
            for c in all_cells:
                pts.extend([[lat, lon] for lat, lon in EX.cell_corners(c)])
            ident = "gmap%d-%s-%s" % (
                gid, str(r.get("from") or "")[:10],
                hashlib.md5(",".join(sorted(all_cells)).encode()).hexdigest()[:10])
            bm = BM.ensure(pts, MAP_W, MAP_H, ident=ident)
            svg = _map_svg(pts, people, bm, EX, BM)
        else:
            # 我那片排第一：跨省的组里，先看到的应该是自己所在的城市。
            # 稳定排序，同组内仍按格子数从多到少。
            if mine:
                _myset = set(mine["cells"])
                shards.sort(key=lambda sh: 0 if _myset & set(sh) else 1)
            svg = _shards_html(gid, r, shards, people, EX, BM, hashlib)
            sharded = True

    # 图例：颜色 + 名字 + 格数。人多了会换行，不挤成一条。
    legend = ""
    for p in people:
        legend += (f'<span class="lgi"><i style="background:{esc(p["color"])}"></i>'
                   f'{esc(p["alias"])}<b>{_weight_label(p)}</b></span>')
    _shared_n = _shared_count(people)
    if _shared_n:
        legend += (f'<span class="lgi"><i class="sh"></i>共同<b>{_shared_n}</b></span>')

    # 我自己的开关。默认关，打开前把「上传什么」讲明白。
    if mine:
        _sw = (f'<form action="/__group/{gid}/mapshare" method="post">{_tok(token)}'
               '<input type="hidden" name="on" value="0">'
               '<div class="setrow"><span class="nowrap">路线共享</span>'
               '<span class="note" style="flex:1 1 auto;margin:0">'
               f'已开启 · 本周 {len(mine["cells"])} 格</span>'
               '<button class="btn sm" type="submit">关掉</button></div></form>')
    else:
        _sw = (f'<form action="/__group/{gid}/mapshare" method="post">{_tok(token)}'
               '<input type="hidden" name="on" value="1">'
               '<div class="setrow"><span class="nowrap">路线共享</span>'
               '<span class="note" style="flex:1 1 auto;margin:0">'
               '开启后同组能看到你本周跑过哪些 500 米格子</span>'
               '<button class="btn sm primary" type="submit">开启</button></div></form>')

    # 分片时 _shards_html 里每片自带卡片和图例，这里不能再套一层——
    # 套了会在最下面多出一条对不上任何一张图的图例。
    if not svg:
        _map_block = ('<div class="card"><p class="note">这周还没有人共享路线。'
                      '打开下面的开关，你的足迹会以 500 米格子的形式出现在这里。</p></div>')
    elif sharded:
        _map_block = svg
    else:
        _map_block = _map_card(svg, legend)

    body = (_msg(message)
            + _map_block
            + sec("我的设置", '<div class="flat">' + _sw + '</div>')
            + '<p class="note">每格的颜色归**跑得最多**的那个人，颜色越深说明'
              '这格被跑得越多；两个人跑量接近的格子显示为白色。'
              '地图上只有 500 米见方的格子和格内里程，没有轨迹线、没有配速、'
              '没有心率。关掉共享的那一刻，服务端会把这些格子删掉。</p>')

    title = f"{len(people)} 人 · {int(r.get('cells') or 0)} 格" if people else "这周还没有路线"
    return page("GROUP", "组内路线", title, body,
                foot="只上传 500 米网格，不上传轨迹、配速和位置点。",
                back=("返回小组", f"/groups/{gid}"))


def _shards_html(gid: int, r: dict, shards, people, EX, BM, hashlib) -> str:
    """跨省时一片一张小地图。

    片是按 0.5° 网格聚出来的，标题取该片第一个格子的逆地理编码（「西安」），
    取不到就写「第 N 片」。每片自己的图例——不然一个组四片，图例全堆在最后
    根本对不上哪块颜色属于哪张图。
    """
    out = ""
    for idx, shard in enumerate(shards, 1):
        sset = set(shard)
        sub = [dict(p, cells=[c for c in p["cells"] if c in sset]) for p in people]
        sub = [p for p in sub if p["cells"]]
        if not sub:
            continue
        pts: list[list[float]] = []
        for c in shard:
            pts.extend([[lat, lon] for lat, lon in EX.cell_corners(c)])
        ident = "gmaps%d-%s-%s" % (
            gid, str(r.get("from") or "")[:10],
            hashlib.md5(",".join(sorted(shard)).encode()).hexdigest()[:10])
        bm = BM.ensure(pts, MAP_W, SHARD_H, ident=ident)
        legend = "".join(
            f'<span class="lgi"><i style="background:{esc(p["color"])}"></i>'
            f'{esc(p["alias"])}<b>{_weight_label(p)}</b></span>' for p in sub)
        _sc = _shared_count(sub)
        if _sc:
            legend += f'<span class="lgi"><i class="sh"></i>共同<b>{_sc}</b></span>'
        name = _shard_name(shard, EX, BM) or f"片区 {idx}"
        n = sum(len(p["cells"]) for p in sub)
        out += (f'<div class="shard-h"><span>{esc(name)}</span>'
                f'<i>{n} 格</i></div>')
        out += _map_card(_map_svg(pts, sub, bm, EX, BM, w=MAP_W, h=SHARD_H), legend)
    return out


def _shard_name(shard, EX, BM) -> str:
    """片的名字：取该片第一个格子的「省·市」，只要市名。没有 Key 时返回空串。"""
    try:
        lat, lon = EX.cell_center(shard[0])
        region = BM.regeo(lat, lon)
    except Exception:      # noqa: BLE001 - 逆地理编码失败不该让整页挂掉
        return ""
    return str(region).split("·")[-1] if region else ""


def _map_card(svg: str, legend: str) -> str:
    lg = f'<div class="lgs">{legend}</div>' if legend else ""
    return ('<div class="card gmapcard">' + svg + lg + '</div>')


def _map_svg(pts, people, bm, EX, BM, w: int = MAP_W, h: int = MAP_H) -> str:
    """底图 + 每人一条彩色格子带。

    变换和活动图用同一套 `p' = p*s + t`（见 actcard._track_on_map 的注释）：
    底图和格子共用一个变换，才不会出现「路网在左、脚印在右」。
    """
    W, H = w, h
    meta = (bm or {}).get("meta") or {}
    iw, ih = int(meta.get("w") or W), int(meta.get("h") or H)
    s = float(meta.get("s") or min(W / iw, H / ih))

    if bm:
        pxs = [BM.to_px(lat, lon, meta) for lat, lon in pts]
        cx = (min(x for x, _ in pxs) + max(x for x, _ in pxs)) / 2
        cy = (min(y for _, y in pxs) + max(y for _, y in pxs)) / 2
        tx = min(0.0, max(W - iw * s, W / 2 - cx * s))
        ty = min(0.0, max(H - ih * s, H / 2 - cy * s))

        def xy(lat, lon):
            x, y = BM.to_px(lat, lon, meta)
            return x * s + tx, y * s + ty
    else:
        # 没有底图（没配 Key / 网络失败）：仍然画格子，按经纬度线性铺满画幅。
        # 形状还在，只是没有路网——比整块空白强。
        lats = [p[0] for p in pts]
        lons = [p[1] for p in pts]
        la0, la1 = min(lats), max(lats)
        lo0, lo1 = min(lons), max(lons)
        dlat = (la1 - la0) or 0.01
        dlon = (lo1 - lo0) or 0.01

        def xy(lat, lon):
            return ((lon - lo0) / dlon * (W - 24) + 12,
                    (1 - (lat - la0) / dlat) * (H - 24) + 12)

    # 每格的归属由**跑量**决定：谁在这格里跑得最多、且占到 OWNER_SHARE 以上，
    # 这块就归他的颜色；达不到就是「共同」（白色）。深浅再按该格总跑量走，
    # 一眼看出哪片是热区。
    #
    # 为什么不能靠绘制顺序：那样后画的会把先画的盖掉，A 和 B 跑同一条路时
    # A 等于从图上消失了。
    owners = _cell_owners(people)
    color_of = {p.get("user_id"): p["color"] for p in people}

    # 按（颜色, 深浅档）分组，一组一条 path——否则每格一条 path，
    # 几百个格子就是几百个元素。
    buckets: dict[tuple, list[str]] = {}
    for c, (u, share, tot) in owners.items():
        key = (u if share >= OWNER_SHARE else None, round(_op_of(tot), 2))
        buckets.setdefault(key, []).append(c)

    def _quad(cells) -> str:
        d = ""
        for c in cells:
            quad = [xy(lat, lon) for lat, lon in EX.cell_corners(c)]
            d += "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in quad) + " Z "
        return d

    # 共同格压在最上层：它是「两个人都跑过」这个结论，不该被任何一个单人的
    # 颜色盖住。同类里自己的颜色放后面，画在别人上面。
    mine_uid = next((p.get("user_id") for p in people if p.get("is_me")), None)

    def _z(kv):
        u = kv[0][0]
        return (u is None, 0 if u == mine_uid else 1)

    order = sorted(buckets.items(), key=_z)
    paths = ""
    for (u, op), cells in order:
        col = SHARED_COLOR if u is None else str(color_of.get(u) or SHARED_COLOR)
        # 描边比填充更实：500 m 的方块之间会有亚像素缝，靠描边补掉；
        # 而且取景拉远时每格只有几个像素，全靠描边才看得出「这是一条带」。
        paths += (f'<path d="{_quad(cells)}" fill="{esc(col)}" fill-opacity="{op:g}"'
                  f' stroke="{esc(col)}" stroke-opacity="0.95"'
                  f' stroke-width="0.45" stroke-linejoin="round"/>')

    # 底图必须压暗：高德给的是浅色图，直接铺在暗色卡片上像一张纸糊在屏幕上，
    # 而且格子的颜色会被底图抢掉。
    # 亮度压到 .46 而不是 .58：.58 是按城市级（zoom 11 左右）的灰调底图调的，
    # 分片之后每片 zoom 更高、是街道级配色，底子本身就是白的，.58 压不住。
    bg = (f'<image href="/basemap/{esc(bm["png"].name)}" x="0" y="0" '
          f'width="{iw}" height="{ih}" preserveAspectRatio="none" '
          f'style="filter:brightness(.46) saturate(.6) contrast(1.04)" '
          f'transform="translate({tx:.2f},{ty:.2f}) scale({s:.4f})"/>' if bm else
          '<rect x="0" y="0" width="100%" height="100%" fill="#0E1621"/>')
    # 再罩一层深蓝，把底图的色温拉回到 App 的冷色调
    veil = ('<rect x="0" y="0" width="100%" height="100%" fill="#0A1424" '
            'fill-opacity="0.34"/>')
    credit = ('<text x="' + str(W - 8) + '" y="' + str(H - 7) +
              '" text-anchor="end" class="mapsrc">高德地图</text>')
    return (f'<svg class="gmap" viewBox="0 0 {W} {H}" '
            f'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="组内本周路线">'
            f'<defs><clipPath id="gmclip"><rect x="0" y="0" width="{W}" '
            f'height="{H}" rx="12"/></clipPath></defs>'
            f'<g clip-path="url(#gmclip)">{bg}{veil}{paths}{credit}</g></svg>')


# ---------------------------------------------------------------- 动态
def _rel_day(d: str, today: str) -> str:
    try:
        n = (dt.date.fromisoformat(today) - dt.date.fromisoformat(d)).days
    except (TypeError, ValueError):
        return d[5:]
    return "今天" if n == 0 else "昨天" if n == 1 else f"{n} 天前"


def _hm(sec: int) -> str:
    """统一成 h:mm / m:ss。

    以前 60 分钟以上写 `1:07`、以下写 `45 分钟`，同一列里两种量纲，
    扫下来眼睛要换算。现在全是时钟写法。
    """
    t = max(0, int(sec or 0))
    h, m, r = t // 3600, (t % 3600) // 60, t % 60
    return f"{h}:{m:02d}" if h else f"{m}:{r:02d}"


def _pace(distance_m: float, duration_s: float) -> str:
    """平均配速 m'ss"。由距离和时长**算**出来的，不是上传的配速。"""
    km = float(distance_m or 0) / 1000.0
    sec = float(duration_s or 0)
    if km <= 0 or sec <= 0:
        return ""
    spk = sec / km
    m, sec2 = int(spk // 60), int(round(spk % 60))
    if sec2 == 60:
        m, sec2 = m + 1, 0
    return f"{m}'{sec2:02d}\""


def _shape_svg(shape: str, color: str) -> str:
    """轨迹形状。`shape` 是 "x,y;x,y;..."，坐标 0..SHAPE_SCALE（1000）。

    客户端归一化过（长边 = 1000，0.1% 精度，120 个点），这里只负责画。
    线宽用 `vector-effect="non-scaling-stroke"` 固定成屏幕像素——同一个
    1.8px 在 46 km 的长条和 1 km 的操场上都一样粗细，不会因为 viewBox 大小
    忽粗忽细。
    """
    pts: list[tuple[int, int]] = []
    for seg in (shape or "").split(";"):
        if "," not in seg:
            continue
        x, y = seg.split(",", 1)
        try:
            xi, yi = int(float(x)), int(float(y))
        except ValueError:
            continue
        if 0 <= xi <= 1000 and 0 <= yi <= 1000:
            pts.append((xi, yi))
    if len(pts) < 3:
        return ""
    mx = max(p[0] for p in pts) or 1
    my = max(p[1] for p in pts) or 1
    # 兼容 1.5.11 早期版本的 24 点 / 0..100 坐标：等比放大，形状不变
    if mx <= 100 and my <= 100:
        pts = [(x * 10, y * 10) for x, y in pts]
        mx, my = mx * 10, my * 10
    # 边距按长边 4% 留：线宽是屏幕像素、不随 viewBox 缩放，所以边距得给够，
    # 否则线头/线宽会被 viewBox 裁掉半截。
    pad = max(10, round(max(mx, my) * 0.04))
    stroke = esc(color) if color else "var(--brand)"
    poly = " ".join(f"{x},{y}" for x, y in pts)
    return (f'<svg class="ac-tr" viewBox="{-pad} {-pad} {mx + pad * 2} {my + pad * 2}"'
            ' preserveAspectRatio="xMidYMid meet" aria-hidden="true">'
            f'<polyline points="{poly}" stroke="{stroke}"'
            ' vector-effect="non-scaling-stroke"/></svg>')


def _act_card(a: dict, today: str, detail: bool = False) -> str:
    """一条动态的卡片：谁 / 轨迹剪影 / 距离 / 元信息 / 最新三条评论。

    剪影是**脱敏**的：客户端把轨迹抽稀到 24 点、等比缩放到 100×100 再上传，
    不含经纬度、起终点和朝向以外的信息（见 group._shape）。
    没有 GPS 的活动（室内/器械）或旧数据没有剪影，就画占位方块写「无轨迹」——
    绝不拿一条装饰曲线冒充路线。
    """
    km = float(a.get("distance_m") or 0) / 1000.0
    dur = int(a.get("duration_s") or 0)
    elev = int(a.get("elevation_m") or 0)

    art = _shape_svg(str(a.get("shape") or ""), str(a.get("color") or ""))
    if not art:
        art = '<span class="ac-ph">无轨迹</span>'

    if km > 0:
        main = f'<div class="ac-km">{km:.1f}<i>km</i></div>'
        bits = []
        if dur:
            bits.append(_hm(dur))
        pace = _pace(km * 1000, dur)
        if pace:
            bits.append(pace)
        if elev:
            bits.append(f'爬升 {elev} m')
    else:
        # 没有距离的活动不能显示「0.0 km」——那看着像数据坏了。
        # 主数字改成时长，元信息写类型。
        main = f'<div class="ac-km">{_hm(dur) if dur else "—"}<i>时长</i></div>'
        bits = [str(a.get("kind_name") or "运动")]
        if elev:
            bits.append(f'爬升 {elev} m')
    sub = f'<p class="ac-sub">{" · ".join(bits)}</p>' if bits else ""

    # 评论：列表里给最新三条；详情页由下面单独渲染全量，这里不重复
    cm = ""
    if not detail:
        rec = a.get("recent") or []
        cnt = int(a.get("comments") or 0)
        lines = ""
        for c in rec:
            _e = esc(c.get("emoji") or "")
            _t = esc(c.get("text") or "")
            lines += (f'<p><b>{esc(c.get("alias"))}</b>'
                      + (f'<em>{_e}</em>' if _e else "")
                      + (f'<span>{_t}</span>' if _t else "") + '</p>')
        if cnt > len(rec):
            more = f'<span class="ac-more">还有 {cnt - len(rec)} 条评论 ›</span>'
        elif rec:
            # 有评论就别再挂「去留一句」：整张卡本来就是入口，多这一行像按钮
            more = ""
        else:
            more = '<span class="ac-more">还没有评论 · 说一句 ›</span>'
        cm = f'<div class="ac-c">{lines}{more}</div>'

    return ('<div class="ac-h">'
            f'<span class="fd" style="background:{esc(a.get("color"))}"></span>'
            f'<span class="ac-n">{esc(a.get("alias"))}</span>'
            f'<i class="fk">{esc(a.get("kind_name"))}</i>'
            f'<span class="ac-d">{esc(_rel_day(str(a.get("date")), today))}</span>'
            '</div>'
            f'<div class="ac-mid">{art}<div class="ac-body">{main}{sub}</div></div>'
            + cm)


def feed_page(token: str, gid: int, message: str = "", act: int = 0) -> str:
    """大家的动态：活动概要瀑布流。点一条进详情，在详情里留言或挑表情。"""
    from . import group as G

    today = dt.date.today().isoformat()
    r = G.feed(gid, today, act or None)
    if not r.get("ok"):
        body = (_msg(message)
                + f'<div class="card"><p class="note">{esc(r.get("error") or "打不开动态")}</p>'
                f'<div class="pager"><a class="btn" href="/groups/{gid}">返回小组</a></div></div>')
        return page("GROUP", "动态", "", body, back=("返回小组", f"/groups/{gid}"))

    rows = r.get("rows") or []
    emojis = r.get("emojis") or []

    if act:
        # 单条详情：概要 + 全部评论 + 输入框
        one = next((x for x in rows if int(x.get("id") or 0) == act), None)
        if not one:
            body = (_msg(message)
                    + '<div class="card"><p class="note">这条动态不在了</p>'
                    f'<div class="pager"><a class="btn" href="/groups/{gid}/feed">'
                    '返回动态</a></div></div>')
            return page("GROUP", "动态", "", body, back=("返回动态", f"/groups/{gid}/feed"))
        emo = ""
        for e in emojis:
            emo += (f'<form action="/__group/{gid}/comment" method="post" class="eform">'
                    f'{_tok(token)}<input type="hidden" name="act" value="{act}">'
                    f'<input type="hidden" name="emoji" value="{esc(e)}">'
                    f'<button class="ebtn" type="submit">{esc(e)}</button></form>')
        cmts = ""
        for c in (one.get("recent") or []):
            _e = esc(c.get("emoji") or "")
            _t = esc(c.get("text") or "")
            cmts += (f'<div class="cm"><b>{esc(c.get("alias"))}</b>'
                     + (f'<span class="ce">{_e}</span>' if _e else "")
                     + (f'<span>{_t}</span>' if _t else "")
                     + "</div>")
        body = (_msg(message)
                + sec("这条", _act_card(one, today, detail=True))
                + (sec("评论", '<div class="flat">' + cmts + '</div>') if cmts else "")
                + sec("说一句", '<div class="flat">'
                      '<div class="erow">' + emo + '</div>'
                      f'<form action="/__group/{gid}/comment" method="post">{_tok(token)}'
                      f'<input type="hidden" name="act" value="{act}">'
                      '<div class="setrow"><span class="nowrap">留言</span>'
                      '<input type="text" name="text" maxlength="60" '
                      'placeholder="一句话" aria-label="留言"></div>'
                      '<div class="pager"><button class="btn sm primary" type="submit">'
                      '发出去</button></div></form></div>'))
        return page("GROUP", "动态", f'{esc(one.get("alias"))} · '
                    f'{esc(one.get("kind_name"))}', body,
                    back=("返回动态", f"/groups/{gid}/feed"))

    items = ""
    for a in rows:
        items += (f'<a class="acard" href="/groups/{gid}/feed?act={int(a["id"])}">'
                  + _act_card(a, today) + "</a>")
    if not items:
        items = ('<div class="card"><p class="note">最近还没有动态。'
                 '同步一次后，你和队友的活动概要会出现在这里。</p></div>')
    hidden = int(r.get("hidden") or 0)
    # 剪影从 1.5.11 才开始上传，老数据全是空的。用户一进来看到一片「无轨迹」
    # 会以为功能坏了，而补报是**手动**的（设置页那个「立即」），所以要明说。
    _no_shape = bool(rows) and not any(str(x.get("shape") or "") for x in rows)
    body = (_msg(message)
            + ('<p class="note">剪影要补报一次才有：小组设置 → 上报 →「立即」，'
               '会补最近 14 天的活动；更早的只显示「无轨迹」。</p>'
               if _no_shape else "")
            + sec("大家最近", items)
            + (f'<p class="note">另有 {hidden} 人关闭了共享，不出现在这里。</p>'
               if hidden else "")
            + '<p class="note">轨迹就是这次跑过的路线本身：没有底图、不标起终点、'
              '不含经纬度——看得到怎么跑的，看不出跑在哪条街。'
              '配速是按距离÷时长算的，不是上传的原始配速。</p>')
    return page("GROUP", "动态", f"{len(rows)} 条", body,
                foot="只上传里程合计和脱敏轨迹剪影，不含坐标和位置。",
                back=("返回小组", f"/groups/{gid}"))


# ---------------------------------------------------------------- 本周荣誉
# 改名：六件衫 → 本周荣誉。衫是公路自行车的说法（领骑黄衫、爬坡圆点衫），
# 跑步群里没几个人知道，进来只看到一排彩色圆点。称号（里程王/出勤王…）
# 自己就说得清，颜色退成第二层标识。
#
# 图标也换了：六个圆点说不清是「衫」还是标签，圆点衫的波点在 26px 的圆上
# 又和白衫糊在一起（1/6 看着像 6 件全有）。现在画一件衣服的剪影。
_JERSEY = ("M7.6 3.6C9.4 3.6 10.4 5.6 12 5.6C13.6 5.6 14.6 3.6 16.4 3.6"
           "L21.9 6.3L20.1 9.5L17.5 8.5L17.5 20.4L6.5 20.4L6.5 8.5"
           "L3.9 9.5L2.1 6.3Z")


def _jersey(color: str, pattern: str = "", on: bool = True,
            cls: str = "", tip: str = "") -> str:
    """一件「衫」的图标。没拿到的是空心虚线衫，拿到的是实色衫。

    圆点衫必须画波点：它和白衫的色号都是白，纯色画出来是两件一模一样的衣服。
    """
    c = esc(color or "#3FA76A")
    extra = f" {esc(cls)}" if cls else ""
    tt = f' title="{esc(tip)}"' if tip else ""
    # viewBox 贴着衣服轮廓裁（1.6 3 20.8 18），不然 26px 的盒子里
    # 上下各留 3px 白边，图标看着比旁边的字还小
    if not on:
        return (f'<svg class="jsh off{extra}" viewBox="1.6 3 20.8 18" aria-hidden="true"{tt}>'
                f'<path d="{_JERSEY}" fill="none" stroke="rgba(255,255,255,.26)"'
                ' stroke-width="1" stroke-dasharray="2 1.6" stroke-linejoin="round"/>'
                '</svg>')
    pid = "jshDots" if pattern == "dots" else ""
    defs = ""
    if pid:
        defs = (f'<defs><pattern id="{pid}" width="3.4" height="3.4" '
                'patternUnits="userSpaceOnUse"><rect width="3.4" height="3.4" '
                'fill="#F4F7FC"/><circle cx="1.2" cy="1.2" r=".9" fill="#D9534F"/>'
                '</pattern></defs>')
    fill = f"url(#{pid})" if pid else c
    return (f'<svg class="jsh{extra}" viewBox="1.6 3 20.8 18" aria-hidden="true"{tt}>'
            f'{defs}<path d="{_JERSEY}" fill="{fill}"'
            ' stroke="rgba(255,255,255,.18)" stroke-width=".8" '
            'stroke-linejoin="round"/></svg>')


def _explore_icon(cls: str = "jx", tip: str = "") -> str:
    """探索徽章的小图标：虚线方框 + 定位点。

    它不是「衫」，所以不能画衣服；字形「⌖」在 15px 上糊成一个小点，
    也看不出「换地方跑」这件事，所以用 SVG 画。
    """
    extra = f" {esc(cls)}" if cls else ""
    tt = f' title="{esc(tip)}"' if tip else ""
    return (f'<svg class="jsh off{extra}" viewBox="1.6 1.6 20.8 20.8" '
            f'aria-hidden="true"{tt}>'
            '<rect x="2.2" y="2.2" width="19.6" height="19.6" rx="5.6" fill="none"'
            ' stroke="rgba(255,255,255,.32)" stroke-width="1.3"'
            ' stroke-dasharray="2.8 2.2"/>'
            '<circle cx="12" cy="12" r="2" fill="currentColor"/>'
            '<path d="M12 6v2M12 16v2M6 12h2M16 12h2" stroke="currentColor"'
            ' stroke-width="1.4" stroke-linecap="round"/></svg>')


def shirts_page(token: str, gid: int, message: str = "") -> str:
    """本周荣誉：六项称号，每项一张卡 + 组内完整榜。

    上一版是一行一件的窄表，前 3 名塞在一行灰字里，用户点进去看不到
    「每项到底在比什么」「排到第几」，也看不懂「衫」这个词。这一版：
    称号当主标、衫型图标当识别、每项一句口径、下面列完整榜（我那一行高亮）。
    """
    from . import group as G

    today = dt.date.today().isoformat()
    se = G.season(gid, today)
    if not se.get("ok"):
        body = (_msg(message)
                + f'<div class="card"><p class="note">'
                  f'{esc(se.get("error") or "打不开本周荣誉")}</p>'
                  f'<div class="pager"><a class="btn" href="/groups/{gid}">返回小组</a>'
                  '</div></div>')
        return page("GROUP", "本周荣誉", "", body, back=("返回小组", f"/groups/{gid}"))

    me_uid = int((se.get("me") or {}).get("user_id") or 0)
    cards = ""
    for sh in se.get("shirts") or []:
        _name = str(sh.get("name") or "")
        _title = str(sh.get("title") or "")
        _color = str(sh.get("color") or "")
        _field = str(sh.get("field") or "")
        winners = [str(w.get("alias") or "") for w in (sh.get("winners") or [])]
        # 完整榜：服务端下发 rank（全组、按该项排序）；老服务端只有 top（前 3）
        rank = sh.get("rank") or sh.get("top") or []

        # 收起态只显示得主：一个人名 + 成绩，最多三人并列（再多就省略）
        if winners:
            _w = next((x for x in rank if x.get("alias") in winners), {})
            _who = "/".join(winners[:3]) + ("…" if len(winners) > 3 else "")
            _val = str((_w or {}).get("text") or "")
            win = (f'<span class="hw"><b>{esc(_who)}</b>'
                   + (f' · {esc(_val)}' if _val else "") + '</span>')
        elif rank:
            # 白衫要求「确实涨了」，一个没涨就不发——要说清为什么，而不是留白
            _why = ("还没人比前 7 天跑得多" if _field == "progress"
                    else "还没有数据")
            win = f'<span class="hw">{_why}</span>'
        else:
            win = '<span class="hw">还没有数据</span>'

        lis = ""
        for i, x in enumerate(rank, 1):
            _me = int(x.get("user_id") or 0) == me_uid
            cls = "me" if _me else ("win" if winners and x.get("alias") in winners else "")
            lis += (f'<li{" class=" + chr(34) + cls + chr(34) if cls else ""}>'
                    f'<span class="n">{i}</span>'
                    f'<span class="a">{esc(x.get("alias"))}</span>'
                    f'<span class="v">{esc(str(x.get("text") or ""))}</span></li>')
        lis = f'<ol class="hlist">{lis}</ol>' if lis else ""

        cards += ('<details class="hon">'
                  '<summary>'
                  + _jersey(_color, str(sh.get("pattern") or ""), True, "lg")
                  + f'<span class="hn">{esc(_title)}</span>'
                  + win
                  + f'<span class="hon-tag" style="--c:{esc(_color)}"><i></i>'
                  + f'{esc(_name)}</span>'
                  + '<span class="chev">›</span></summary>'
                  + f'<div class="hbody"><p class="hint">{esc(sh.get("hint"))}</p>'
                  + lis + '</div></details>')

    # ---- 探索徽章：不占称号，但**必须看得见**。
    # 服务端一直在下发 `explore.badges`（谁够 5 格），客户端以前只在榜单行
    # 后面缀一句「新 N 格」，荣誉页正文里根本没提——脚注写着「单独发」，
    # 却没有任何地方显示谁拿到了。现在补一行，和六项用同一套折叠行。
    _ex = se.get("explore") or {}
    _min_cells = int(_ex.get("min_cells") or 5)
    _ex_rows = sorted(
        ((int(p2.get("user_id") or 0), str(p2.get("alias") or ""),
          int(p2.get("explorer") or 0)) for p2 in (se.get("people") or [])),
        key=lambda x: -x[2])
    _hit = [r for r in _ex_rows if r[2] >= _min_cells]
    if _ex_rows:
        _top_cells = _ex_rows[0][2]
        _hw = (f'{len(_hit)} 人 · 最多 {_top_cells} 格' if _hit
               else f'还没人够 {_min_cells} 格')
        _lis = ""
        for i, (u, a, c) in enumerate(_ex_rows, 1):
            cls = "me" if u == me_uid else ("win" if c >= _min_cells else "")
            _lis += (f'<li{" class=" + chr(34) + cls + chr(34) if cls else ""}>'
                     f'<span class="n">{i}</span>'
                     f'<span class="a">{esc(a)}</span>'
                     f'<span class="v">{c} 格</span></li>')
        cards += ('<details class="hon hon-x"><summary>'
                  + _explore_icon() +
                  '<span class="hn">探索徽章</span>'
                  f'<span class="hw">{_hw}</span>'
                  '<span class="hon-tag">额外</span>'
                  '<span class="chev">›</span></summary>'
                  f'<div class="hbody"><p class="hint">本周新踩 {_min_cells} 个 '
                  '500 米的格子就算达标，不占称号。跑步路线换得多才有。</p>'
                  f'<ol class="hlist">{_lis}</ol></div></details>')

    # 规则放前面（一屏之内只留最有用的两行），「衫」的来历折到最下面，
    # 想看的人点一下就有——它是谈资，不是每次进来都要读一遍的东西。
    # 首屏只有两行：一屏要装下六项称号 + 探索徽章，说明文字越长越挤。
    # 「进步王为什么例外」放进那一项的展开态（hint 里已经写了口径）。
    intro = ('<p class="hintro">六项各比一件事，第一名拿对应颜色，可以同时拿几项。'
             '只算跑步类、只算本周。</p>')

    body = (_msg(message) + intro
            + sec("六项称号", '<div class="honbox">' + cards + '</div>')
            + '<details class="hwhy"><summary>为什么叫「衫」？</summary>'
              '<p class="hintro">这个叫法借自公路自行车：总成绩第一穿黄衫，'
              '爬坡王穿红点衫。跑步组沿用这套颜色——六个称号配六种颜色，'
              '扫一眼就知道自己缺哪几件。</p></details>'
            + '<p class="note">关掉共享的人不参与。探索徽章不占称号，'
              '单独列在最后一行。</p>')

    mine = (se.get("me") or {})
    got = [str(s2.get("title") or "") for s2 in (mine.get("shirts") or [])]
    if int(mine.get("explorer") or 0) >= _min_cells:
        got.append("探索徽章")
    sub = ("我拿到 " + "、".join(got)) if got else "本周还没拿到"
    return _with_copy(page("GROUP", "本周荣誉", sub, body,
                           foot="只上传里程合计和脱敏轨迹剪影，不含坐标和位置。",
                           back=("返回小组", f"/groups/{gid}")))


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
                '<div class="pager"><a class="btn" href="/groups">返回小组列表</a></div></div>')
        return page("GROUP", "训练小组", "", body, back=("全部小组", "/groups?list=1"))

    g = d["group"]
    role = g.get("role") or "member"
    is_admin = role in ("owner", "admin")
    # 人数要在好几处判断（页头胶囊、名次文案、空榜），先取一次。
    _n = int(g.get("member_count") or 0)
    my_share = (g.get("me") or {}).get("share") or "summary"

    # 把「路线共享开着吗」记到本地：上报时靠这个决定要不要多发一次格子，
    # 不记的话每次上报都得先拉一遍 detail。
    try:
        from . import appapi as _A
        from .store import Store as _Store

        _st = _Store(_A.DB)
        try:
            G.note_map(_st, gid, bool((g.get("me") or {}).get("map_share")))
        finally:
            _st.close()
    except Exception:      # noqa: BLE001 - 缓存写不进去不影响页面
        pass

    b = _h.get("board") or G.board(gid, rng, today)
    cl = _h.get("challenges") or G.challenge_list(gid, today)
    total_m = (b.get("summary") or {}).get("total_distance_m") or 0

    # 赛季数据要**提前**拿：周榜每行要挂「穿了哪几件衫 + 有没有探索徽章」，
    # 这些都在 season 里。取不到就整块降级，组页不能因为一个新接口挂掉。
    try:
        _se = _h.get("season") or G.season(gid, today)
    except Exception:      # noqa: BLE001 - 老服务端没有 /season，降级即可
        _se = {}
    # 谁的衫：按名字索引（board 行里没有 user_id）。衫的 pattern 只在
    # season.shirts 里给（圆点衫），按 id 查一次。
    _pat_of = {str(s2.get("id") or ""): str(s2.get("pattern") or "")
               for s2 in (_se.get("shirts") or [])}
    _marks_of: dict[str, str] = {}
    for _p in (_se.get("people") or []):
        _one = ""
        for _x in (_p.get("shirts") or []):
            _one += _jersey(str(_x.get("color") or ""),
                            _pat_of.get(str(_x.get("id") or ""), ""), True, "jr",
                            f'{_x.get("name") or ""}·{_x.get("title") or ""}')
        _marks_of[str(_p.get("alias") or "")] = _one
    _min_cells = int(((_se.get("explore") or {}).get("min_cells")) or 5)

    # 本周榜。名字后面**只挂图标**：穿了哪几件衫（衣服）+ 有没有探索徽章
    # （虚线方框），原来的「我 / 组长 / N 天 / 新 N 格」四段小字全删了——
    # 四段字挤在名字后面，一屏十几行读起来像注脚；徽章本来就有图形，
    # 直接挂图形更快认。我自己那一行靠底色和品牌色区分，不用再写「我」。
    rank_rows = ""
    for i, x in enumerate(b.get("rows") or [], 1):
        cls = "rank me" if x.get("is_me") else "rank"
        _alias = str(x.get("alias") or "")
        # 衫和探索徽章都是**按周结算**的（season 只有 /season，没有月口径），
        # 挂到月榜上会被读成「这个月的衫」——那是错的。所以月榜只排里程。
        marks = ""
        if rng == "week":
            marks = _marks_of.get(_alias, "")
            if int(x.get("explorer") or 0) >= _min_cells:
                marks += _explore_icon("jr", "探索徽章")
        marks = f'<span class="mk">{marks}</span>' if marks else ""
        rank_rows += (f'<div class="{cls}"><span class="no">{i}</span>'
                      f'<span class="nm">{esc(_alias)}</span>{marks}'
                      f'<span class="km">{_km(x.get("distance_m"))}<i>km</i></span></div>')
    hidden = int(b.get("hidden") or 0)
    # 一个人对着排行榜跟自己比：一条数据 + 半屏空白。这不是「空位」，
    # 是页面没告诉用户下一步干什么。直接换成一句状态 + 一个邀请入口。
    # 一个人组：榜单照常渲染（自己那一行也有信息量——里程、天数），
    # 只是在后面补一段「把码发出去」。原来直接把榜整块换成空态，
    # 用户一进来看不到「本周榜」只剩一句提示，观感像页面坏了。
    if _n <= 1:
        _code = str(g.get("invite_code") or "")
        if _code:
            rank_rows += ('<p class="note">本周还没有队友上榜。把邀请码发出去，'
                          '他们加入并上报后就会排在这里。</p>'
                          '<div class="invrow">'
                          f'<span class="iv">{esc(_code)}</span>'
                          '<button class="btn sm" type="button" '
                          f'onclick="gcCopy(this,\'{esc(_code)}\')">复制邀请码</button>'
                          '</div>')
        else:
            rank_rows += ('<p class="note">本周还没有队友上榜。</p>'
                          f'<div class="pager"><a class="btn sm primary" '
                          f'href="/groups/{gid}/settings">邀请队友</a></div>')
    elif not rank_rows:
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

    # 昵称 / 共享 / 上报 / 邀请码 / 退出全部搬到「小组设置」页了：
    # 组页主体是排名和动态，把改昵称这种低频操作压在最下面，用户的原话是
    # 「放在最下面太突兀」。这里只留一个页头齿轮。
    _gear = ('<a class="hact" href="/groups/{0}/settings" aria-label="小组设置">'
             '<span aria-hidden="true">⚙</span></a>').format(gid)

    # 建赛事也是折叠：一个月用一次的表单不该常驻占掉半屏。
    chal_fold = ""
    if chal_form:
        chal_fold = ('<details class="fold"><summary>建一个赛事'
                     '<span class="fx">›</span></summary>' + chal_form + '</details>')

    # 页头胶囊只说一件新事：全组本周跑了多少。
    # 「N 人」已经写在副标题里，再放一枚同样的胶囊等于把一行字抄两遍；
    # 一个人组里「全组」就等于「我」，所以干脆不出这枚胶囊。
    chips = ""
    if _n > 1:
        chips = (f'<span class="chip">全组 {_km(total_m)} km '
                 + ("本月" if rng == "month" else "本周") + '</span>')

    # ---- 核心数据卡：名次 + 分数 + 称号 / 六件衫徽章
    # 原来是两行：一行「我 · 第 2 名 63.8 分 [先锋] ●黄衫 ●绿衫」，紧接着
    # 又是一整行「黄衫·里程王 阿狗  ●绿衫·出勤王 阿狗 …」。六件衫各写一遍
    # 榜名和得主，两行都在抢注意力，而且都没有容器收着。
    # 现在：分数进卡的上半区，六件衫收成 6 枚 28px 圆徽章放下半区，
    # 得主信息进 title 和点击后的榜单页。
    score_card = ""
    if _se.get("ok") and (_se.get("people") or []):
        _me = _se.get("me") or {}
        _mine_names = {str(x.get("name") or "") for x in (_me.get("shirts") or [])}
        if _me:
            _rk = int(_me.get("rank") or 0)
            _sc = float(_me.get("score") or 0)
            _tt = str(_me.get("title") or "")
            _badges = ""
            for sh in (_se.get("shirts") or []):
                _on = str(sh.get("name") or "") in _mine_names
                _nm = str(sh.get("name") or "")
                _tl = str(sh.get("title") or "")
                _who = "/".join(str(w.get("alias") or "")
                                for w in (sh.get("winners") or []))
                _tip = f"{_nm}·{_tl}" + (f" {_who}" if _who else " 暂时没人")
                _badges += _jersey(str(sh.get("color") or ""),
                                   str(sh.get("pattern") or ""), _on, "", _tip)
            # 一个人组里「第 1 名」没有信息量，直接说这是「本周成绩」。
            # 月榜页里这张卡还是本周的（season 只有周口径），所以要写明「本周」，
            # 否则「我 · 第 9 名」会被读成本月名次。
            if _n <= 1:
                _k = "本周成绩"
            elif rng == "month":
                _k = f'本周 · 第 {_rk} 名'
            else:
                _k = f'我 · 第 {_rk} 名'
            # 口径要自己交代：这是全页最大的数字，不说清是什么分，
            # 用户只能猜「是跑量？出勤？还是综合」。满分和加分上限由服务端
            # 下发（base_max / bonus_max），不在客户端写死——权重改了这里跟着变。
            _bm = _se.get("base_max") or 90
            _xb = _se.get("bonus_max") or 10
            _note = (f'综合分 · 满分 {float(_bm):g}（跑量 · 出勤 · 爬升 · 进步 · 连跑）'
                     f' + 加分 {float(_xb):g}')
            _got = sum(1 for sh in (_se.get("shirts") or [])
                       if str(sh.get("name") or "") in _mine_names)
            score_card = (
                f'<section class="scorecard">'
                f'<div class="sc-top"><div class="sc-l">'
                f'<span class="sc-k">{esc(_k)}</span>'
                f'<div class="sc-v">{_sc:g}<i>分</i></div></div>'
                # 称号是身份，不是警告：琥珀在体系里专给「逾期 / 超限」，
                # 拿它标「先锋」会被读成「你这周有问题」。身份类一律中性。
                + (f'<span class="pill ghost">{esc(_tt)}</span>' if _tt else "")
                + '</div>'
                f'<div class="sc-note">{esc(_note)}</div>'
                f'<a class="sc-shirts" href="/groups/{gid}/shirts">'
                f'<span class="sc-badges">{_badges}</span>'
                f'<span class="sc-more">荣誉 {_got}/6 ›</span></a></section>')
    shirt_row = ""

    # ---- 本周玩法：目标 / 对子 / 预言
    # 一行放结论，改目标这种每周一次的动作折起来。榜单是每天看的，
    # 不能让它被表单顶下去——这是这个页面唯一的信息优先级。
    _wk = _h.get("week") or {}
    if not _wk.get("ok"):
        try:
            _wk = G.week(gid, today)
        except Exception:      # noqa: BLE001 - 老服务端没有 /week，整块不显示即可
            _wk = {}
    # 三行「标签 + 内容」对齐。原来是把三项塞进一个 flex 换行里，每项的
    # 语序还不一样（「目标 30 km · 已跑 38.1 · 达成」/「结对 阿狗 · 我 30 /
    # 他 75 · 逆袭」），读起来像三句不相干的话。对齐成两列之后，扫一眼就知道
    # 哪一行是什么。衫得主也从上面挪进来当第一行，形状统一。
    week_form = ""
    rows: list[tuple[str, str]] = []
    if shirt_row:
        rows.append(("衫", shirt_row))
    if _wk.get("ok"):
        _g = _wk.get("goal") or {}
        # 自己的 uid 从成员表里认（detail 里就有），不依赖 season 成功——
        # season 一挂，结对和预言就整块不显示了，那太脆。
        _me_uid = next((int(m["user_id"]) for m in (g.get("members") or [])
                        if m.get("is_me")), None)
        if _me_uid is None and (_se.get("me") or {}).get("user_id"):
            _me_uid = int(_se["me"]["user_id"])
        _mine_row = next((x for x in (b.get("rows") or []) if x.get("is_me")), None)
        _done_km = float((_mine_row or {}).get("distance_m") or 0) / 1000.0
        _goal_km = _g.get("km")
        _suggest = float(_g.get("suggest") or 0)

        # 目标单独成一行入口（56pt）：它是每周一次的动作，混在四行表格里
        # 既不像「可点」，也把「已跑多少」这种每天看的信息压成了灰字。
        # 副文必须有单位和进度百分比——「已跑 6.2 / 38.6 km」还得自己心算
        # 才知完成几成，那是用户最想知道的那个数。
        if _goal_km:
            # 达成之后就不再报百分比：127% 这种数看着像出错了，
            # 而且「超了多少」对下周排目标没有指导意义，说「已达成」就够。
            _hit = _done_km >= float(_goal_km)
            _gsub = f'已跑 {_done_km:.1f} / {float(_goal_km):g} km · ' + (
                "已达成" if _hit else f'{_done_km / float(_goal_km) * 100:.0f}%')
        elif _suggest:
            # 建议值的口径来自服务端 `_suggest_goal`：近 4 周周均 × 0.8。
            # 不写「按上周 ×1.14」那种自创说法——算出来的数对不上，
            # 用户一眼就能看出这个建议是编的。
            _gsub = f'还没定 · 建议 {_suggest:g} km（近 4 周周均 × 0.8）'
        else:
            _gsub = "还没定"
        _gtxt = f'目标 {float(_goal_km):g} km' if _goal_km else "定本周目标"

        _pair = next((p for p in (_wk.get("pairs") or [])
                      if _me_uid in (p["small"]["user_id"], p["big"]["user_id"])), None)
        if _pair:
            _other = (_pair["big"] if _pair["small"]["user_id"] == _me_uid
                      else _pair["small"])
            _me_side = _pair["small"] if _pair["small"]["user_id"] == _me_uid else _pair["big"]
            _res = {"double": "双人徽章", "upset": "逆袭", "big_only": "让分达成",
                    "split": "一人达成", "none": "都还差一点"}.get(
                        _pair.get("result") or "", "")
            rows.append(("结对", f'{esc(_other["alias"])}'
                                 f'<s>我 {float(_me_side["target"]):g}'
                                 f' / 他 {float(_other["target"]):g}</s>'
                                 + (f'<b>{_res}</b>' if _res else "")))

        _pred = (_wk.get("predict") or {}).get("mine")
        if _pred:
            _pa = next((r.get("target_alias") for r in (_wk.get("predict") or {}).get("rows") or []
                        if r.get("user_id") == _me_uid), "")
            if _pa:
                rows.append(("预言", f'{esc(_pa)}<s>完不成</s>'))

        # 表单：目标 +（可选）预言。选项只列已报目标的人——猜一个没目标的人
        # 没法判「完不成」，服务端也会拒。
        _opts = '<option value="">先不猜</option>'
        for t in (_wk.get("targets") or []):
            _sel = " selected" if t["user_id"] == _pred else ""
            _opts += (f'<option value="{int(t["user_id"])}"{_sel}>'
                      f'{esc(t["alias"])}</option>')
        _val = f'{float(_goal_km):g}' if _goal_km else (
            f'{_suggest:g}' if _suggest else "")
        # 入口行：左「目标 30 km」+ 右副文「已跑 12.3 / 30 km」，点开才是表单。
        # 48px 高、整行可点，和下面榜单条目同一节奏。
        week_form = ('<details class="goalrow"><summary>'
                     f'<span class="gk">{esc(_gtxt)}</span>'
                     f'<span class="gs">{esc(_gsub)}</span>'
                     '<span class="chev">›</span></summary>'
                     '<div class="gbody">'
                     f'<form action="/__group/{gid}/goal" method="post">{_tok(token)}'
                     '<div class="setrow"><span>目标 km</span>'
                     f'<input type="number" name="km" min="1" max="1000" step="0.5" '
                     f'value="{esc(_val)}" placeholder="这周打算跑多少" aria-label="本周目标"></div>'
                     '<div class="setrow"><span>预言</span>'
                     f'<select name="predict" aria-label="猜谁完不成">{_opts}</select></div>'
                     '<div class="pager"><button class="btn sm primary" type="submit">保存'
                     '</button></div></form>'
                     '<p class="note">目标用于结对让分和预言结算；填低一点也没关系，'
                     '达成比好看重要。</p></div></details>')

    # 「衫」那行内容是流式的（六件全有得主会折两行），要允许换行；
    # 其它行是单行结论，用省略号截断。
    week_line = ('<div class="kvrows">'
                 + "".join(
                     f'<span class="k">{k}</span>'
                     f'<span class="v{" wrap" if k == "衫" else ""}">{v}</span>'
                     for k, v in rows)
                 + "</div>") if rows else ""

    # ---- 功能入口：一排并列的小图标
    # 之前这些入口散在三处：六件衫是行尾一个蓝色文字、Bingo 塞在状况行里、
    # 足迹和远征是「探索」区的两行卡片。形态各不相同，页面就没有规矩。
    # 现在数据用左对齐的行、功能用图标，两类东西长得不一样，才好扫。
    # 六件衫**不在这里**：上面那张核心数据卡的徽章行本身就是它的入口
    # （`荣誉 1/6 ›`），同一屏放两个去同一页的按钮，等于让人选「点哪个」。
    # 徽章在那儿还是看得见的状态，比一个文字图标信息量大。
    #
    # 三格空着太浪费，每格补一句「这一周正在发生什么」：
    # Bingo 点了几格、足迹新踩几格、远征走到本段第几格。
    # 都不多要接口：Bingo 在 /home 的 week 里，足迹在 season 的 me.explorer，
    # 只有远征要单独问一次 run_state（失败就退化成纯图标，不影响整页）。
    _bg = (_wk.get("bingo") or {}) if isinstance(_wk, dict) else {}
    _bd, _bt = int(_bg.get("done") or 0), int(_bg.get("total") or 25)
    _bl = int(_bg.get("lines") or 0)
    # 足迹用**全组**本周新踩的格子数：一个人这周常跑老路线，自己的数经常是 0，
    # 「0 格」看着像坏了。组内合计才是这个入口（组足迹地图）真正在讲的事。
    _ex = sum(int(x.get("explorer") or 0) for x in (_se.get("people") or []))
    _rs = {}
    try:
        _rs = G.run_state(gid, today) or {}
    except Exception:      # noqa: BLE001 - 远征挂了不该让组页打不开
        _rs = {}
    _rse = _rs.get("season") or {}
    _rpg = _rs.get("progress") or {}
    _act = int(_rse.get("act") or 0)
    _st = float(_rpg.get("stage_team") or 0)
    _stt = float(_rpg.get("stage_target") or 0)

    def _tile(icon: str, name: str, href: str, val: str, sub: str,
              frac: float | None = None) -> str:
        # 没有分母的格子（足迹）不画条：画一条没有意义的条比不画更糟。
        # 但它要加 .nobar 让内容垂直居中，否则三格里只有它顶在上面、底下空一块。
        bar = (""
               if frac is None else
               f'<span class="qb"><i style="width:{max(0.0, min(100.0, frac * 100)):.0f}%">'
               '</i></span>')
        cls = "qg nobar" if frac is None else "qg"
        return (f'<a class="{cls}" href="{href}"><i class="qi" aria-hidden="true">{icon}</i>'
                f'<span class="qn">{esc(name)}</span>'
                f'<span class="qv">{esc(val)}</span>'
                f'<span class="qs">{esc(sub)}</span>{bar}</a>')

    _tiles = [
        _tile("⊞", "Bingo", f"/groups/{gid}/bingo",
              f"{_bd}/{_bt}", f"{_bl} 条线", _bd / _bt if _bt else None),
        _tile("⌖", "足迹", f"/groups/{gid}/map", f"{_ex} 格", "全组本周新踩"),
        # 远征的主数用「第几幕」而不是「6.3/2.4 格」：刚开赛时本段目标很小
        # （2.4 格），一跑就超过，比分式显示成 6.3/2.4 像是算错了。
        # 进度条按 min(1, 走/目标) 封顶，超了就满格。
        (_tile("◭", "远征", f"/run?gid={gid}",
               f"第 {_act} 幕", f"本段 {_st:g} 格", min(1.0, _st / _stt))
         if _stt > 0 else
         _tile("◭", "远征", f"/run?gid={gid}", f"第 {_act} 幕" if _act else "去看看",
               "本周远征")),
    ]
    quick = '<div class="qgrid">' + "".join(_tiles) + "</div>"

    # ---- 大家的动态：和 /feed 用同一种卡片，但组页只放**两条**。
    # 一条卡约 200px（大字 + 标尺 + 三项 + 三条评论），放三条就把「看全部」
    # 挤到第三屏去了；组页的主角是榜单，动态是「顺手看一眼」。
    feed_rows = ""
    try:
        _fd = G.feed(gid, today)
    except Exception:      # noqa: BLE001 - 老服务端没有 /feed，整块不显示
        _fd = {}
    if _fd.get("ok") and (_fd.get("rows") or []):
        for a in (_fd.get("rows") or [])[:2]:
            feed_rows += (f'<a class="acard" href="/groups/{gid}/feed?act={int(a["id"])}">'
                          + _act_card(a, today) + "</a>")
    feed_sec = (sec("大家的动态",
                    feed_rows
                    + f'<div class="pager"><a class="btn sm" href="/groups/{gid}/feed">'
                    f'看全部 {len(_fd.get("rows") or [])} 条</a></div>')
                if feed_rows else "")

    # 月榜/周榜互切。服务端一直支持 range=month，只是页面以前不读这个参数，
    # 入口就被写死成关闭（`if False`）——那是死开关，不是功能。
    other = "month" if rng == "week" else "week"
    more = ("月榜" if other == "month" else "周榜", f"/groups/{gid}?range={other}")

    # 顶部一块：我的名次 + 本周状态（目标/结对/预言/Bingo），不带标题也不带卡片。
    # 顺序是刻意的——点「小组」第一眼要看到「我在组里第几」，然后才是这一周的
    # 玩法。P2 加了两块之后榜单被挤到第三屏，这条就破了，所以玩法块必须并进
    # 头部，而不是自己占一个 section。
    # 顺序：卡片 → 三个入口 → 目标 → 结对/预言。
    # 目标行原来在这排图标上面，试了一版之后放回来：目标是一行“输入”，
    # 夹在卡片和图标之间会把首屏切成「数据 / 表单 / 入口」三段，
    # 三个入口连成一条反而更像一组功能。
    head = score_card + quick + week_form + week_line

    body = (msg
            + head
            + sec("月榜" if rng == "month" else "本周榜",
                  ('<p class="note">衫和探索徽章按周结算，月榜只排里程。</p>'
                   if rng == "month" else "")
                  + '<div class="flat">' + rank_rows + '</div>',
                  more=more)
            + sec("赛事", '<div class="flat">' + ch_rows + '</div>' + chal_fold)
            + feed_sec)
    return _with_copy(page(
        "GROUP", g.get("name") or "小组",
        f'{int(g.get("member_count") or 0)} 人 · '
        + {"owner": "我是组长", "admin": "我是管理员"}.get(role, "我是成员"),
        body, chips, action=_gear,
        foot="只上传里程合计和脱敏轨迹剪影，不含坐标和位置。",
        back=("全部小组", "/groups?list=1")))
