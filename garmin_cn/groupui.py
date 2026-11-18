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
    # 只有一个组就直接进详情：点「小组」最想看的是排名，而不是一行组名。
    # 组多了才需要「先选哪个」这一步，那时列表本身就有意义。
    if len(items) == 1:
        try:
            return group_page(token, int(items[0]["id"]))
        except Exception:      # noqa: BLE001 - 详情页挂了也要能回到列表
            pass
    rows = ""
    for g in items:
        role = {"owner": "组长", "admin": "管理员"}.get(g.get("role") or "", "成员")
        # 名次优先于人数：点「小组」想知道的是「我在组里怎么样」，
        # 而不是这个组有几口人。人数退到括号里。
        wk = g.get("week") or {}
        if wk.get("rank"):
            meta = (f'本周第 {int(wk["rank"])}/{int(wk["total"])} 名'
                    f' · {float(wk["km"]):g} km')
        else:
            meta = f'本周还没有里程 · {int(g.get("member_count") or 0)} 人'
        rows += (f'<a class="grow" href="/groups/{g["id"]}">'
                 f'<span class="gname">{esc(g.get("name"))}'
                 f'<span class="sub">{esc(role)}</span></span>'
                 f'<span class="gmeta">{meta}</span>'
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
    if items:
        # 已经有组的人，建组/加入是低频动作：折成一行放在底部，
        # 别让它和「我的小组」抢版面。
        body = (msg
                + sec("我的小组", '<div class="card">' + rows + '</div>')
                + '<details class="fold"><summary>再加入一个，或者自己建一个'
                  '<span class="fx">›</span></summary>'
                  + '<div class="flat">' + join_form + create_form + '</div></details>')
    else:
        # 一个组都没有：这两个表单就是这一页的全部意义，平铺出来
        body = (msg
                + sec("加入小组", '<div class="card">' + join_form + '</div>')
                + sec("创建小组", '<div class="card">' + create_form + '</div>'))
    return page("GROUP", "训练小组", "本周榜只在组内可见", body, chips,
                foot="只上传每日里程合计到你的账号，不上传轨迹、配速和位置。",
                back=("返回我的", "/me"))


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
                foot="只上传每日里程合计到你的账号，不上传轨迹、配速和位置。",
                back=("返回小组", f"/groups/{gid}"))


# ---------------------------------------------------------------- 组内路线
MAP_W, MAP_H = 392, 300


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
    if all_cells:
        # 取景点用格子四角，不用中心：只用中心的活，最外圈只露出一半
        pts: list[list[float]] = []
        for c in all_cells:
            pts.extend([[lat, lon] for lat, lon in EX.cell_corners(c)])
        ident = "gmap%d-%s-%s" % (
            gid, str(r.get("from") or "")[:10],
            hashlib.md5(",".join(sorted(all_cells)).encode()).hexdigest()[:10])
        bm = BM.ensure(pts, MAP_W, MAP_H, ident=ident)
        svg = _map_svg(pts, people, bm, EX, BM)

    # 图例：颜色 + 名字 + 格数。人多了会换行，不挤成一条。
    legend = ""
    for p in people:
        legend += (f'<span class="lgi"><i style="background:{esc(p["color"])}"></i>'
                   f'{esc(p["alias"])}<b>{len(p["cells"])}</b></span>')

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

    body = (_msg(message)
            + (_map_card(svg, legend) if svg else
               '<div class="card"><p class="note">这周还没有人共享路线。'
               '打开下面的开关，你的足迹会以 500 米格子的形式出现在这里。</p></div>')
            + sec("我的设置", '<div class="flat">' + _sw + '</div>')
            + '<p class="note">地图上只有 500 米见方的格子，没有轨迹线、'
              '没有配速、没有心率。关掉共享的那一刻，服务端会把这些格子删掉。</p>')

    title = f"{len(people)} 人 · {int(r.get('cells') or 0)} 格" if people else "这周还没有路线"
    return page("GROUP", "组内路线", title, body,
                foot="只上传 500 米网格，不上传轨迹、配速和位置点。",
                back=("返回小组", f"/groups/{gid}"))


def _map_card(svg: str, legend: str) -> str:
    lg = f'<div class="lgs">{legend}</div>' if legend else ""
    return ('<div class="card gmapcard">' + svg + lg + '</div>')


def _map_svg(pts, people, bm, EX, BM) -> str:
    """底图 + 每人一条彩色格子带。

    变换和活动图用同一套 `p' = p*s + t`（见 actcard._track_on_map 的注释）：
    底图和格子共用一个变换，才不会出现「路网在左、脚印在右」。
    """
    meta = (bm or {}).get("meta") or {}
    iw, ih = int(meta.get("w") or MAP_W), int(meta.get("h") or MAP_H)
    s = float(meta.get("s") or min(MAP_W / iw, MAP_H / ih))

    if bm:
        pxs = [BM.to_px(lat, lon, meta) for lat, lon in pts]
        cx = (min(x for x, _ in pxs) + max(x for x, _ in pxs)) / 2
        cy = (min(y for _, y in pxs) + max(y for _, y in pxs)) / 2
        tx = min(0.0, max(MAP_W - iw * s, MAP_W / 2 - cx * s))
        ty = min(0.0, max(MAP_H - ih * s, MAP_H / 2 - cy * s))

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
            return ((lon - lo0) / dlon * (MAP_W - 24) + 12,
                    (1 - (lat - la0) / dlat) * (MAP_H - 24) + 12)

    # 自己最后画：压在别人上面，才看得清「我这条」
    order = sorted(people, key=lambda p: bool(p.get("is_me")))
    paths = ""
    for p in order:
        d = ""
        for c in p["cells"]:
            quad = [xy(lat, lon) for lat, lon in EX.cell_corners(c)]
            d += ("M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in quad) + " Z ")
        # 描边比填充更深更实：500 m 的方块之间会有亚像素缝，靠描边补掉；
        # 而且取景拉远时每格只有几个像素，全靠描边才看得出「这是一条带」。
        paths += (f'<path d="{d}" fill="{esc(p["color"])}" fill-opacity="0.62"'
                  f' stroke="{esc(p["color"])}" stroke-opacity="0.95"'
                  f' stroke-width="0.45" stroke-linejoin="round"/>')

    # 底图必须压暗：高德给的是浅色图，直接铺在暗色卡片上像一张纸糊在屏幕上，
    # 而且格子的颜色会被底图抢掉。
    bg = (f'<image href="/basemap/{esc(bm["png"].name)}" x="0" y="0" '
          f'width="{iw}" height="{ih}" preserveAspectRatio="none" '
          f'style="filter:brightness(.58) saturate(.66) contrast(1.05)" '
          f'transform="translate({tx:.2f},{ty:.2f}) scale({s:.4f})"/>' if bm else
          '<rect x="0" y="0" width="100%" height="100%" fill="#0E1621"/>')
    # 再罩一层深蓝，把底图的色温拉回到 App 的冷色调
    veil = ('<rect x="0" y="0" width="100%" height="100%" fill="#0A1424" '
            'fill-opacity="0.30"/>')
    credit = ('<text x="' + str(MAP_W - 8) + '" y="' + str(MAP_H - 7) +
              '" text-anchor="end" class="mapsrc">高德地图</text>')
    return (f'<svg class="gmap" viewBox="0 0 {MAP_W} {MAP_H}" '
            f'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="组内本周路线">'
            f'<defs><clipPath id="gmclip"><rect x="0" y="0" width="{MAP_W}" '
            f'height="{MAP_H}" rx="12"/></clipPath></defs>'
            f'<g clip-path="url(#gmclip)">{bg}{veil}{paths}{credit}</g></svg>')


# ---------------------------------------------------------------- 六件衫
def _shirt_dot(color: str, pattern: str = "", cls: str = "") -> str:
    """衫的色点。`cls` 是附加类名（.sbit 里不用传，那里靠 `.sbit i` 给尺寸）。

    圆点衫要画波点而不是纯色：它和白衫的色号（#D8DEE9 / #F2F5FA）在暗底上
    都是「一个白点」，调浅调深都分不出来。公路车赛里圆点衫本来就是白底红点，
    照这个画既分得开，也不用解释。
    """
    c = ("pd " + cls).strip()
    if pattern == "dots":
        return f'<i class="{c}" aria-hidden="true"></i>'
    return f'<i class="{c}" style="background:{esc(color)}" aria-hidden="true"></i>'


def shirts_page(token: str, gid: int, message: str = "") -> str:
    """六件衫：每件在比什么、现在谁领跑、前 3 名是谁。

    单独一页，是因为组页那一行只说得出「黄衫 阿狗」。颜色和名字本身不表意，
    不给口径和前几名，新来的人只会看到一排小圆点。
    """
    from . import group as G

    today = dt.date.today().isoformat()
    se = G.season(gid, today)
    if not se.get("ok"):
        body = (_msg(message)
                + f'<div class="card"><p class="note">{esc(se.get("error") or "打不开六件衫")}</p>'
                f'<div class="pager"><a class="btn" href="/groups/{gid}">返回小组</a></div></div>')
        return page("GROUP", "六件衫", "", body, back=("返回小组", f"/groups/{gid}"))

    rows = ""
    for sh in se.get("shirts") or []:
        top = sh.get("top") or []
        names = [str(w.get("alias") or "") for w in (sh.get("winners") or [])]
        if names and top:
            head = (f'<b>{esc("/".join(names))}</b>'
                    f'<i>{esc(top[0].get("text") or "")}</i>')
        elif top:
            # 有数据但不算得主（白衫要求本周确实涨了）：说清楚为什么没发
            head = '<span class="none">本周没人达标</span>'
        else:
            head = '<span class="none">本周没有数据</span>'
        rest = ""
        for i, t in enumerate(top[1:], 2):
            rest += f'<span class="rest">{i} {esc(t.get("alias"))} {esc(t.get("text"))}</span>'
        rows += ('<div class="shr">'
                 + _shirt_dot(str(sh.get("color") or ""), str(sh.get("pattern") or ""), "dot")
                 + f'<div class="t">{esc(sh.get("name"))} · {esc(sh.get("title"))}</div>'
                 f'<div class="v">{head}</div>'
                 f'<div class="h">{esc(sh.get("hint"))}'
                 + (f'<span class="r3">{rest}</span>' if rest else "")
                 + '</div></div>')

    body = (_msg(message)
            + sec("本周六件衫", '<div class="shirtlist">' + rows + '</div>')
            + '<p class="note">每人每周最多穿几件都可以。每件都只算本周、只算跑步类；'
              '关掉共享的人不参与评衫。探索徽章（新踩 5 格）不占衫，单独发。</p>')

    mine = (se.get("me") or {})
    got = [s["name"] for s in (mine.get("shirts") or [])]
    sub = ("我穿着 " + "、".join(got)) if got else "本周还没穿上衫"
    return page("GROUP", "六件衫", sub, body,
                foot="只上传每日里程合计到你的账号，不上传轨迹、配速和位置。",
                back=("返回小组", f"/groups/{gid}"))


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
        # 探索徽章不占衫，所以没有色块可挂；在名字后面留一句「新 N 格」，
        # 让「这周换了地方跑」这件事在榜上是看得见的。
        _new_cells = int(x.get("explorer") or 0)
        if _new_cells >= 5:
            name += f'<span class="sub">新 {_new_cells} 格</span>'
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
                f'<span class="sbit">'
                + _shirt_dot(str(x.get("color") or ""), str(x.get("pattern") or ""))
                + f'{esc(x.get("name"))}</span>'
                for x in (shirts or []))

        if _me:
            _tt = str(_me.get("title") or "")
            score_line = (
                '<div class="sline">'
                f'<span class="rk">我 · 第 {int(_me.get("rank") or 0)} 名</span>'
                f'<b>{float(_me.get("score") or 0):g}<i>分</i></b>'
                + (f'<span class="tt">{esc(_tt)}</span>' if _tt else "")
                + _bits(_me.get("shirts"))
                # 入口推在这一行最右：放到衫行末尾会自己换行，变成一个孤零零
                # 的蓝色小标题，看着像还有个区块没加载出来
                + f'<a class="sline-jump" href="/groups/{gid}/shirts">六件衫 ›</a>'
                + "</div>")
        win = []
        for sh in _se.get("shirts") or []:
            names = [str(w.get("alias") or "") for w in (sh.get("winners") or [])]
            if names:
                # 带上榜名：只写「黄衫 阿狗」的话，没看过规则的人不知道那是里程榜
                win.append(f'<span class="sbit"><i style="background:'
                           f'{esc(sh.get("color"))}"></i>{esc(sh.get("name"))}·'
                           f'{esc(sh.get("title"))} {esc("/".join(names))}</span>')
        if win:
            # 整行可点进「六件衫」。箭头在上面那行最右，这里不再重复一个
            score_line += ('<div class="sline2"><a class="shirtlink" href="'
                           f'/groups/{gid}/shirts">' + "".join(win) + "</a></div>")
    if not score_line:
        score_line = ""

    # ---- 本周玩法：目标 / 对子 / 预言
    # 一行放结论，改目标这种每周一次的动作折起来。榜单是每天看的，
    # 不能让它被表单顶下去——这是这个页面唯一的信息优先级。
    _wk = _h.get("week") or {}
    if not _wk.get("ok"):
        try:
            _wk = G.week(gid, today)
        except Exception:      # noqa: BLE001 - 老服务端没有 /week，整块不显示即可
            _wk = {}
    week_line = ""
    week_form = ""
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

        bits = []
        if _goal_km:
            _hit = _done_km >= float(_goal_km)
            # 每项自带前缀（目标 / 结对 / 预言）：只靠 10px 的间距，三项挤在
            # 一行时读起来像一句话（「…达成 与 阿狗 结对…」），手机上看不清。
            bits.append(f'目标 <b>{float(_goal_km):g}</b> km · 已跑 {_done_km:.1f}'
                        + (' · <b class="hit">达成</b>' if _hit else ""))
        elif _suggest:
            bits.append(f'还没定目标 · 建议 <b>{_suggest:g}</b> km')
        else:
            bits.append("还没定目标")

        _pair = next((p for p in (_wk.get("pairs") or [])
                      if _me_uid in (p["small"]["user_id"], p["big"]["user_id"])), None)
        if _pair:
            _other = (_pair["big"] if _pair["small"]["user_id"] == _me_uid
                      else _pair["small"])
            _me_side = _pair["small"] if _pair["small"]["user_id"] == _me_uid else _pair["big"]
            _res = {"double": "双人徽章", "upset": "逆袭", "big_only": "让分达成",
                    "split": "一人达成", "none": "都还差一点"}.get(
                        _pair.get("result") or "", "")
            bits.append(f'结对 {esc(_other["alias"])} · 我 {float(_me_side["target"]):g}'
                        f' / 他 {float(_other["target"]):g}'
                        + (f' · {_res}' if _res else ""))

        _pred = (_wk.get("predict") or {}).get("mine")
        if _pred:
            _pa = next((r.get("target_alias") for r in (_wk.get("predict") or {}).get("rows") or []
                        if r.get("user_id") == _me_uid), "")
            if _pa:
                bits.append(f'预言 {esc(_pa)} 完不成')

        # Bingo 只报一行进度，点进去看整张卡。25 个格子塞进组页会把榜单挤走，
        # 而榜单才是这个页面存在的理由。
        _bg = _wk.get("bingo") or {}
        if _bg.get("total"):
            _ln = int(_bg.get("lines") or 0)
            bits.append(f'<a class="blink" href="/groups/{gid}/bingo">Bingo '
                        f'{int(_bg.get("done") or 0)}/{int(_bg["total"])}'
                        + (f' · {_ln} 线' if _ln else "") + '</a>')

        week_line = ('<div class="sline2 weekline">'
                     + "".join(f'<span class="sbit">{x}</span>' for x in bits)
                     + "</div>")

        # 表单：目标 +（可选）预言。选项只列已报目标的人——猜一个没目标的人
        # 没法判「完不成」，服务端也会拒。
        _opts = '<option value="">先不猜</option>'
        for t in (_wk.get("targets") or []):
            _sel = " selected" if t["user_id"] == _pred else ""
            _opts += (f'<option value="{int(t["user_id"])}"{_sel}>'
                      f'{esc(t["alias"])}</option>')
        _val = f'{float(_goal_km):g}' if _goal_km else (
            f'{_suggest:g}' if _suggest else "")
        week_form = ('<details class="fold"><summary>定本周目标'
                     + ("（可改）" if _goal_km else "")
                     + '<span class="fx">›</span></summary>'
                     f'<form action="/__group/{gid}/goal" method="post">{_tok(token)}'
                     '<div class="setrow"><span>目标 km</span>'
                     f'<input type="number" name="km" min="1" max="1000" step="0.5" '
                     f'value="{esc(_val)}" placeholder="这周打算跑多少" aria-label="本周目标"></div>'
                     '<div class="setrow"><span>预言</span>'
                     f'<select name="predict" aria-label="猜谁完不成">{_opts}</select></div>'
                     '<div class="pager"><button class="btn sm primary" type="submit">保存'
                     '</button></div></form>'
                     '<p class="note">目标用于结对让分和预言结算；填低一点也没关系，'
                     '达成比好看重要。</p></details>')

    # 「组内足迹」和「雪线之前」是同一类东西：都是「这段时间我们走过哪儿」，
    # 一行一个入口，共用一张卡。分开两个 section 会多两行标题，占掉榜单的位置。
    trail = ('<div class="card">'
             f'<a class="grow" href="/groups/{gid}/map">'
             '<span class="gname">组内足迹</span>'
             '<span class="gmeta">本周谁跑过哪片</span>'
             '<span class="chev">›</span></a>'
             '<a class="grow" href="/run?gid=' + str(gid) + '">'
             '<span class="gname">雪线之前</span>'
             '<span class="gmeta">这一季的路</span>'
             '<span class="chev">›</span></a></div>')

    # 月榜/周榜互切。服务端一直支持 range=month，只是页面以前不读这个参数，
    # 入口就被写死成关闭（`if False`）——那是死开关，不是功能。
    other = "month" if rng == "week" else "week"
    more = ("月榜" if other == "month" else "周榜", f"/groups/{gid}?range={other}")

    # 顶部一块：我的名次 + 本周状态（目标/结对/预言/Bingo），不带标题也不带卡片。
    # 顺序是刻意的——点「小组」第一眼要看到「我在组里第几」，然后才是这一周的
    # 玩法。P2 加了两块之后榜单被挤到第三屏，这条就破了，所以玩法块必须并进
    # 头部，而不是自己占一个 section。
    head = score_line + week_line + week_form

    body = (msg
            + head
            + sec("月榜" if rng == "month" else "本周榜",
                  '<div class="flat">' + rank_rows + '</div>',
                  more=more)
            + sec("赛事", '<div class="flat">' + ch_rows + '</div>' + chal_fold)
            + sec("探索", trail)
            # 设置和管理都是低频：合成一块，管理再折一层
            + sec("设置", '<div class="flat">' + mine + '</div>' + admin_fold))
    return page("GROUP", g.get("name") or "小组",
                f'{int(g.get("member_count") or 0)} 人 · '
                + {"owner": "我是组长", "admin": "我是管理员"}.get(role, "我是成员"),
                body, chips,
                foot="只上传每日里程合计到你的账号，不上传轨迹、配速和位置。",
                back=("返回小组", "/groups"))
