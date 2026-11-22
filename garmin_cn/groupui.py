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


# 服务端把失败原因原样拼进 ?msg= 回来（「目标要在 1000–1000000 之间」这种
# 句子不含「失败」二字），只按三个词判色会把警告渲染成绿色的成功提示。
_BAD_WORDS = ("失败", "错误", "不能", "要在", "格式", "已达", "超出", "超过",
              "不支持", "过期", "无效", "找不到", "先填", "先勾", "不在")


def _msg(message: str) -> str:
    if not message:
        return ""
    tone = "nt" if any(w in message for w in _BAD_WORDS) else "nt ok"
    return f'<div class="{tone}">{esc(message)}</div>'


def _tok(token: str) -> str:
    return f'<input type="hidden" name="token" value="{esc(token)}">'


# ---------------------------------------------------------------- 评论：表情内嵌
# 表情不再是「单独一个 emoji 字段 + 一句话」，而是插进文字里当普通字符：
# 一次能插好几个、能和文字连成一句（「跑得不错👍🔥」）。存库时只写 text，
# emoji 列留给旧数据（读的时候拼回文字，保证编辑时不丢）。
CM_TEXT_MAX = 60


def _cm_full(c: dict) -> str:
    """一条评论的可编辑全文。旧数据把表情单独存在 emoji 列，这里拼回文字，
    让「编辑」时表情和文字一起显示、一起改。"""
    return (str(c.get("emoji") or "") + str(c.get("text") or "")).strip()


def _cm_bar(emojis) -> str:
    """一排表情按钮：type=button + data-ins，点一下插到同一张表单输入框的光标处。
    **不是提交按钮**——提交的话每次点表情就发一条，没法「文字 + 表情」一起发。"""
    if not emojis:
        return ""
    return ('<div class="erow">' + "".join(
        f'<button type="button" class="ebtn" data-ins="{esc(e)}" '
        f'aria-label="插入表情 {esc(e)}">{esc(e)}</button>' for e in emojis)
        + '</div>')


def _cm_say(gid: int, aid: int, token: str, emojis,
            placeholder: str = "说一句，可以插表情") -> str:
    """发言框：输入框 + 一排表情（插进光标）+ 发送。"""
    return ('<form action="/__group/' + str(gid) + '/comment" method="post" class="sayf">'
            + _tok(token)
            + f'<input type="hidden" name="act" value="{aid}">'
            '<div class="ac-say">'
            f'<input class="cm-in" type="text" name="text" maxlength="{CM_TEXT_MAX}" '
            f'placeholder="{esc(placeholder)}" data-say="1" '
            'aria-label="给这条动态留言，可以插入表情">'
            '<button class="btn sm primary" type="submit">发</button>'
            '</div>'
            + _cm_bar(emojis) + '</form>')


def _cm_edit(gid: int, aid: int, c: dict, token: str, emojis) -> str:
    """自己的评论：折在铅笔里改 / 删。

    原来的 › 看着像「展开回复」，点开只有一个输入框，用户不知道那是编辑。
    现在：铅笔图标（无障碍标签写「编辑这条评论」）+ 展开后明确一行标题 +
    输入框预填全文 + 同一排表情（可改表情）+ 保存 / 删除。
    """
    cid = int(c.get("id") or 0)
    full = _cm_full(c)
    return ('<details class="cm-more">'
            '<summary aria-label="编辑这条评论" title="编辑">'
            '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" '
            'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" '
            'stroke-linejoin="round" aria-hidden="true">'
            '<path d="M11.3 2.1l2.6 2.6L5.4 13.2l-3.3.7.7-3.3z"/></svg>'
            '</summary>'
            '<div class="cm-act">'
            '<div class="cm-ed">编辑这条评论</div>'
            f'<form action="/__group/{gid}/comment_edit" method="post">'
            + _tok(token)
            + f'<input type="hidden" name="id" value="{cid}">'
            f'<input type="hidden" name="act" value="{aid}">'
            f'<input class="cm-in" type="text" name="text" maxlength="{CM_TEXT_MAX}" '
            f'value="{esc(full)}" data-say="1" aria-label="评论内容，可以插入表情">'
            + _cm_bar(emojis)
            + '<div class="pager">'
            '<button class="btn sm" name="op" value="save" type="submit">保存</button>'
            '<button class="btn sm danger" name="op" value="del" type="submit" '
            'onclick="return confirm(\'删除这条评论？\')">删除</button>'
            '</div></form></div></details>')


# 表情插进光标：点按钮时把字符塞到输入框当前的 caret 位置。blur 时记下
# caret（blur 不冒泡，必须捕获阶段），这样「输一半 → 点表情」能插在中间，
# 而不是永远甩到末尾。全文用纯 DOM 操作，不引任何库。
SAY_JS = (
    '<script>(function(){'
    'document.addEventListener("blur",function(ev){var t=ev.target;'
    'if(t&&t.matches&&t.matches(\'input[data-say]\')&&t.selectionStart!=null)'
    't.setAttribute("data-pos",t.selectionStart);},true);'
    'document.addEventListener("click",function(ev){'
    'var b=ev.target.closest?ev.target.closest(".ebtn[data-ins]"):null;'
    'if(!b)return;ev.preventDefault();'
    'var f=b.closest("form"),i=f&&f.querySelector(\'input[data-say]\');'
    'if(!i)return;var ch=b.getAttribute("data-ins")||"";'
    'var p=i.getAttribute("data-pos"),s=(p===null||p==="")?i.value.length:parseInt(p,10);'
    'if(isNaN(s)||s<0||s>i.value.length)s=i.value.length;'
    'i.value=i.value.slice(0,s)+ch+i.value.slice(s);'
    'var n=s+ch.length;i.setAttribute("data-pos",n);i.focus();'
    'try{i.setSelectionRange(n,n);}catch(x){}'
    '});})();</script>')


# 表单提交后回到原处：PRG（POST → 303 → GET）必然整页重渲染，CSS 折叠、
# 输入焦点、滚动位置全部归零。用户发一条评论，回来时所有展开的评论都收起了、
# 页面还跳回顶部——「发完就找不到刚才那条了」。
#
# 这里不做局部刷新（改造成本大、还要服务端支持片段），只用 sessionStorage
# 把两件事记住并在下一页还原：
#   1. 哪些 <details data-k> 是展开的（key 稳定，评论按活动 id、目标行按名字）；
#   2. 滚动位置（只在表单提交/离开页面时写，回来时读一次就删）。
# 键里带 pathname，所以组页、榜单页、设置页各存各的，互不串味。
KEEP_JS = (
    '<script>(function(){'
    'var K="grpst:"+location.pathname,Y=K+":y";'
    'function restore(){'
    'var o=[];try{o=JSON.parse(sessionStorage.getItem(K)||"[]");}catch(e){}'
    'for(var i=0;i<o.length;i++){'
    'var d=document.querySelector(\'details[data-k="\'+o[i]+\'"]\');if(d)d.open=true;}'
    'var y=null;try{y=sessionStorage.getItem(Y);}catch(e){}'
    'if(y===null)return;try{sessionStorage.removeItem(Y);}catch(e){}'
    # 带 msg 回来 = 上一步有结果要说（多为失败原因）。那句提示渲染在页首，
    # 这时再还原到原来的滚动位置，人就看不见它了——表现成「点了没反应」。
    # 所以有 msg 时不还原，让浏览器按 URL 里的 #anchor 落到对应的区块。
    'if(/[?&]msg=/.test(location.search))return;'
    'var n=parseInt(y,10)||0;window.scrollTo(0,n);'
    'window.addEventListener("load",function(){window.scrollTo(0,n);});}'
    'function save(){'
    'var o=[],ds=document.querySelectorAll("details[data-k]"),i;'
    'for(i=0;i<ds.length;i++){if(ds[i].open)o.push(ds[i].getAttribute("data-k"));}'
    'try{sessionStorage.setItem(K,JSON.stringify(o));}catch(e){}}'
    'function mark(){'
    'try{sessionStorage.setItem(Y,String(window.scrollY||0));}catch(e){}}'
    'document.addEventListener("toggle",save,true);'
    'document.addEventListener("submit",function(){save();mark();},true);'
    'window.addEventListener("pagehide",function(){save();mark();});'
    'if(document.readyState==="loading")'
    'document.addEventListener("DOMContentLoaded",restore);else restore();'
    '})();</script>')


# 建赛事表单：类型 → 单位 / 示例。五种类型的单位差得远（米 / 次 / 格 / 秒每
# 公里），一个静态的「目标（米/次）」标签等于让用户自己记规矩。
CHAL_JS = (
    '<script>(function(){'
    'function sync(s){'
    'var f=s.form||(s.closest&&s.closest("form"));if(!f)return;'
    'var o=s.options[s.selectedIndex];if(!o)return;'
    # option 上存的是 data-u / data-p（不是 data-unit）：标签和 option 用同一个
    # 属性名时，querySelector("[data-unit]") 会先命中 option，把选项文字改坏。
    'var u=f.querySelector("[data-unit]");'
    'if(u)u.textContent=o.getAttribute("data-u")||"";'
    'var i=f.querySelector("[data-target]");'
    'if(i)i.placeholder=o.getAttribute("data-p")||"";}'
    'document.addEventListener("change",function(ev){'
    'var s=ev.target;'
    'if(s&&s.matches&&s.matches(\'select[name=kind]\'))sync(s);});'
    '})();</script>')


def _gname(g: dict) -> str:
    """组名 + 营地后缀。等级写进 groups.name 就再也拆不出来了，所以客户端拼。

    Lv1 是默认值（宿营），不加后缀——刚建的组不该顶着一串等级。
    """
    n = str(g.get("name") or "")
    try:
        lv = int(g.get("camp_level") or 1)
    except (TypeError, ValueError):
        lv = 1
    return f'{n} · Lv{lv} 营地' if lv >= 2 else n


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
                      '<p class="note">令牌只存在这台手机里。换手机重新登录即可'
                      '找回组和榜单。</p></div>')
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
                  '<div class="pager"><button class="btn" type="submit">退出登录</button></div></form>'
                # 注销：隐私说明里承诺「随时能删」，页面上就必须有入口。
                # 折起来 + 勾选 + 二次确认，和「解散小组」同一套处理。
                + '<details class="fold fold-gap"><summary>注销账号'
                  '<span class="fx">›</span></summary><div class="foldbody">'
                  '<p class="note">账号立即失效，你在各小组的里程、评论、目标全部删除，'
                  '邮箱/手机号清空，不能恢复。</p>'
                  f'<form action="/__account/delete" method="post">{_tok(token)}'
                  '<label class="ckline"><input type="checkbox" name="confirm" value="1">'
                  ' 我知道注销不能恢复</label>'
                  '<div class="pager"><button class="btn danger" type="submit" '
                  'onclick="return confirm(\'注销账号？这个动作不能撤销\')">注销</button>'
                  '</div></form></div></details>')
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
                 '<p class="note">首次登录自动注册，只用于小组和平台额度，'
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
        body = (msg + '<div class="card"><p class="note">小组需要登录：'
                '建组、入组、看榜。只上传每日里程合计，不上传位置。</p>'
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
        role = {"owner": "组长", "admin": "管理员", "coach": "教练"}.get(
            g.get("role") or "", "成员")
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
                  f'<span class="gc-n">{esc(_gname(g))}</span>'
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
            '<div class="card flat"><p class="note">你已经是「'
            f'{esc(owner_g.get("name"))}」的组长，一人只能当一组的组长。'
            '建新组前先解散或转让。</p>'
            f'<div class="pager"><a class="btn sm" '
            f'href="/groups/{int(owner_g["id"])}/settings">去处理</a></div></div>')
    elif len(items) >= 3:
        blocked = ('<div class="card flat"><p class="note">'
                   '已经在 3 个组里，最多同时加入 3 个。</p></div>')
    if blocked:
        create_form = blocked

    # 已解散的组：列表里看不到，但数据还在服务端。给一个彻底删除的出口，
    # 否则「解散」只是让它从列表消失，数据永远躺着——用户以为删了，其实没删。
    try:
        _arch = (G.archived() or {}).get("groups") or []
    except Exception:      # noqa: BLE001 - 拉不到就不显示这块
        _arch = []
    arch_sec = ""
    if _arch:
        _rows = ""
        for x in _arch:
            _n = int(x.get("members") or 0)
            _rows += (
                f'<details class="fold"><summary>{esc(x.get("name"))} · {_n} 人'
                '<span class="fx">›</span></summary><div class="foldbody">'
                '<p class="note">删除后成员、榜单、赛事、动态、路线全部清空，'
                '不能恢复。要删的话，把组名'
                f'「{esc(x.get("name"))}」原样输进下面的框。</p>'
                f'<form action="/__group/{int(x["id"])}/purge" method="post">'
                + _tok(token) +
                '<div class="kvline"><span>组名</span><b>'
                f'<input type="text" name="confirm" maxlength="24" '
                f'data-want="{esc(x.get("name"))}" '
                f'placeholder="在这里输入 {esc(x.get("name"))}" '
                'autocomplete="off" autocapitalize="off" spellcheck="false"></b></div>'
                '<div class="pager"><button class="btn sm danger" type="submit">'
                '彻底删除</button></div></form></div></details>')
        # 输对了组名按钮才变亮。**不用 disabled**：万一段脚本没跑起来
        # （旧 WebView、CSP 拦截），禁用会把删除永久锁死，那比误点更糟。
        # 真正的门槛在服务端，这里只做视觉提示。
        _purge_js = ('<script>(function(){'
                     'document.querySelectorAll("form[action$=\'/purge\']")'
                     '.forEach(function(f){'
                     'var i=f.querySelector("input[name=confirm]"),'
                     'b=f.querySelector("button[type=submit]");'
                     'if(!i||!b)return;'
                     'var w=i.getAttribute("data-want")||"";'
                     'function u(){b.style.opacity=i.value.trim()===w?"1":".4";}'
                     'i.addEventListener("input",u);u();});})();</script>')
        arch_sec = (f'<details class="fold fold-gap"><summary>已解散的组'
                    f'（{len(_arch)}）<span class="fx">›</span></summary>'
                    '<div class="flat"><p class="note">解散只是从列表里收起，'
                    '数据还在服务器上。</p>' + _rows + '</div></details>'
                    + _purge_js)

    chips = "".join([f'<span class="chip">{len(items)} 个组</span>']) if items else ""
    if items:
        # 已经有组的人，建组/加入是低频动作：折成一行放在底部，
        # 别让它和「我的小组」抢版面。
        body = (msg
                + sec("我的小组", cards)
                # force_list 时默认展开：用户是从「加入另一个组」点进来的，
                # 再折一次等于告诉他「这里没有」。
                + ('<details class="fold fold-lg fold-gap" open>' if force_list
                   else '<details class="fold fold-gap">')
                + ('<summary>再加入一个组' if blocked
                   else '<summary>再加入一个，或者自己建一个')
                + '<span class="fx">›</span></summary>'
                  + '<div class="flat">' + join_form + create_form + '</div></details>')
    else:
        # 一个组都没有：这两个表单就是这一页的全部意义，平铺出来
        body = (msg
                + sec("加入小组", '<div class="card">' + join_form + '</div>')
                + sec("创建小组", '<div class="card">' + create_form + '</div>'))
    body = body + arch_sec
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
    # 教练能建赛事、看操作记录、导出；人事（踢人、审批、改码）不给
    is_staff = role in ("owner", "admin", "coach")
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
            '<span class="note" style="flex:1 1 auto;margin:0">同步时自动上报；'
            '缺轨迹的会补抓，可能等几秒</span>'
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
    # 加入方式 + 待批准申请（只有管理角色看得到）
    if is_admin:
        _jm = str(g.get("join_mode") or "open")
        grp += (f'<form action="/__group/{gid}/joinmode" method="post">{_tok(token)}'
                '<div class="setrow"><span>加入</span>'
                '<select name="mode" aria-label="加入方式">'
                f'<option value="open"{" selected" if _jm == "open" else ""}>扫码即入</option>'
                f'<option value="approve"{" selected" if _jm == "approve" else ""}>需我批准</option>'
                '</select><button class="btn sm" type="submit">存</button></div></form>')
        _pend = g.get("pending") or []
        if _pend:
            grp += (f'<p class="note">{len(_pend)} 人申请加入</p>')
            for _p in _pend:
                grp += (f'<form action="/__group/{gid}/decide" method="post" class="setrow">'
                        + _tok(token)
                        + f'<input type="hidden" name="uid" value="{int(_p["user_id"])}">'
                        f'<span style="flex:1 1 auto">{esc(_p.get("alias"))}</span>'
                        '<button class="btn sm primary" name="ok" value="1" type="submit">批准</button>'
                        '<button class="btn sm" name="ok" value="0" type="submit">拒绝</button>'
                        '</form>')
    if is_owner:
        _opts = "".join(
            f'<option value="{int(m["user_id"])}">{esc(m["alias"])}</option>'
            for m in (g.get("members") or []) if not m.get("is_me"))
        if _opts:
            grp += (f'<form action="/__group/{gid}/transfer" method="post">{_tok(token)}'
                    '<div class="setrow"><span>转让组长</span>'
                    f'<select name="uid" aria-label="转给谁">{_opts}</select>'
                    '<button class="btn sm" type="submit">转让</button></div></form>')
        # 教练：只多「建赛事 / 看操作记录 / 导出」，人事不给。放在组长专属区，
        # 因为只有组长能任命——管理员能踢人却不能任命教练，这是故意的。
        _coaches = [m for m in (g.get("members") or [])
                    if str(m.get("role") or "") == "coach"]
        _cand = [m for m in (g.get("members") or [])
                 if not m.get("is_me") and str(m.get("role") or "member") == "member"]
        if _coaches or _cand:
            grp += ('<p class="note">教练能建赛事、看操作记录，不能踢人和审批。</p>')
        for _m in _coaches:
            grp += (f'<form action="/__group/{gid}/role" method="post" class="setrow">'
                    + _tok(token)
                    + f'<input type="hidden" name="uid" value="{int(_m["user_id"])}">'
                    f'<span style="flex:1 1 auto">{esc(_m.get("alias"))} · 教练</span>'
                    '<button class="btn sm" name="role" value="member" '
                    'type="submit">撤销</button></form>')
        if _cand:
            _copts = "".join(
                f'<option value="{int(m["user_id"])}">{esc(m.get("alias"))}</option>'
                for m in _cand)
            grp += (f'<form action="/__group/{gid}/role" method="post">{_tok(token)}'
                    '<div class="setrow"><span>设教练</span>'
                    f'<select name="uid" aria-label="设谁当教练">{_copts}</select>'
                    '<button class="btn sm" name="role" value="coach" '
                    'type="submit">任命</button></div></form>')
    if not is_owner:
        grp += (f'<form action="/__group/{gid}/leave" method="post">{_tok(token)}'
                '<div class="setrow"><span>退出</span>'
                '<span class="note" style="flex:1 1 auto;margin:0">'
                '退出后我在这个组的记录会删掉</span>'
                '<button class="btn sm" type="submit">退出小组</button></div></form>')
    else:
        # 组长不能直接退出，但**必须能解散**：一个人同时只能当 1 个组的组长，
        # 建新组时服务端会 409。没有解散入口的话，「建不了新组」就成了死结。
        grp += ('<p class="note">你是组长。要退出得先转让，或者解散这个组。</p>'
                f'<details class="fold"><summary>解散这个组'
                '<span class="fx">›</span></summary>'
                '<div class="foldbody">'
                '<p class="note">解散后组、榜、赛事都不再出现，且不能撤销。</p>'
                f'<form action="/__group/{gid}/archive" method="post">{_tok(token)}'
                '<label class="ckline"><input type="checkbox" name="confirm"'
                ' value="1"> 我知道解散不能撤销</label>'
                '<div class="pager"><button class="btn sm danger" type="submit">'
                '解散</button></div></form></div></details>')
    if is_staff:
        # 组长工具：谁做了什么、这一周的数据能不能导出来对账
        grp += (f'<a class="setrow" href="/groups/{gid}/audit">'
                '<span>操作记录</span>'
                '<span class="note" style="flex:1 1 auto;margin:0">谁改了设置、谁加入退出'
                '</span><span class="chev">›</span></a>')
        grp += (f'<a class="setrow" href="/groups/{gid}/export.csv">'
                '<span>导出本周数据</span>'
                '<span class="note" style="flex:1 1 auto;margin:0">每人一行，CSV'
                '</span><span class="chev">›</span></a>')
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
            # 两块各自一张卡：之前所有行直接铺在页面上，只有 1px 分隔线，
            # 输入框又都是圆角带边框的小块，一列下来像一串粘住的控件。
            + sec("我的", '<div class="card">' + mine + '</div>')
            + sec("组", '<div class="card">' + grp + '</div>')
            + '<p class="note">只收每日里程合计和活动概要，不上传轨迹点、'
              '配速和位置。</p>')
    return _with_copy(page(
        "GROUP", "小组设置",
        f'{esc(g.get("name"))} · '
        + {"owner": "我是组长", "admin": "我是管理员",
                             "coach": "我是教练"}.get(role, "我是成员"),
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

    # 路线分享挂在足迹页底部：它和格子是同一件事的两种精度——格子说
    # 「跑过哪片」，路线说「具体怎么跑」。分开放会让人以为这是两个功能。
    _today_iso = dt.date.today().isoformat()
    try:
        _route_html = _route_block(token, gid, _today_iso)
    except Exception:      # noqa: BLE001 - 路线拉不到不能把整页弄挂
        _route_html = ""
    _route_sec = (sec("路线分享", _route_html) if _route_html else "")

    body = (_msg(message)
            + _map_block
            + _route_sec
            + sec("我的设置", '<div class="flat">' + _sw + '</div>')
            + '<p class="note">每格的颜色归**跑得最多**的那个人，颜色越深说明'
              '这格被跑得越多；两个人跑量接近的格子显示为白色。'
              '地图上只有 500 米见方的格子和格内里程，没有轨迹线、没有配速、'
              '没有心率。关掉共享的那一刻，服务端会把这些格子删掉。</p>')

    title = f"{len(people)} 人 · {int(r.get('cells') or 0)} 格" if people else "这周还没有路线"
    return page("GROUP", "组内路线", title, body,
                foot="只上传 500 米网格，不上传轨迹、配速和位置点。",
                back=("返回小组", f"/groups/{gid}"))


def _route_svg(points, bm, color: str = "#4EA1FF",
               w: int = MAP_W, h: int = MAP_H) -> str:
    """底图 + 一条折线。

    投影和足迹页同一套（`BM.to_px` + `p*s + t`）。不用同一套的话，同一片
    路网上「格子在这、线在那」，用户会以为数据错了。
    """
    from . import basemap as BM

    W, H = w, h
    meta = (bm or {}).get("meta") or {}
    iw, ih = int(meta.get("w") or W), int(meta.get("h") or H)
    s = float(meta.get("s") or min(W / iw, H / ih))
    tx = ty = 0.0
    if bm:
        pxs = [BM.to_px(lat, lon, meta) for lat, lon in points]
        cx = (min(x for x, _ in pxs) + max(x for x, _ in pxs)) / 2
        cy = (min(y for _, y in pxs) + max(y for _, y in pxs)) / 2
        tx = min(0.0, max(W - iw * s, W / 2 - cx * s))
        ty = min(0.0, max(H - ih * s, H / 2 - cy * s))

        def xy(lat, lon):
            x, y = BM.to_px(lat, lon, meta)
            return x * s + tx, y * s + ty
    else:
        lats = [p[0] for p in points]
        lons = [p[1] for p in points]
        la0, la1 = min(lats), max(lats)
        lo0, lo1 = min(lons), max(lons)
        dlat = (la1 - la0) or 0.01
        dlon = (lo1 - lo0) or 0.01

        def xy(lat, lon):
            return ((lon - lo0) / dlon * (W - 24) + 12,
                    (1 - (lat - la0) / dlat) * (H - 24) + 12)

    d = " ".join(("M" if i == 0 else "L") + f"{x:.1f} {y:.1f}"
                 for i, (x, y) in enumerate(xy(la, lo) for la, lo in points))
    # 轨迹的百分比坐标，给批注吸附用：点图时找最近的一个点，钉子就落在路上，
    # 而不是飘在旁边。抽稀到约 200 个——吸附不需要亚米精度，而 5000 个点
    # 会让页面多几十 KB。
    _px_all = [xy(la, lo) for la, lo in points]
    _step = max(1, len(_px_all) // 200)
    _line = ";".join(f"{x / W * 100:.2f},{y / H * 100:.2f}"
                     for x, y in _px_all[::_step])
    bg = (f'<image href="/basemap/{esc(bm["png"].name)}" x="0" y="0" '
          f'width="{iw}" height="{ih}" preserveAspectRatio="none" '
          f'style="filter:brightness(.58) saturate(.66) contrast(1.04)" '
          f'transform="translate({tx:.2f},{ty:.2f}) scale({s:.4f})"/>' if bm else
          '<rect x="0" y="0" width="100%" height="100%" fill="#0E1621"/>')
    return (f'<svg class="gmap" viewBox="0 0 {W} {H}" '
            f'data-line="{_line}" '
            f'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="这条路线的轨迹">'
            '<defs><clipPath id="rtclip">'
            f'<rect x="0" y="0" width="{W}" height="{H}" rx="12"/></clipPath></defs>'
            f'<g clip-path="url(#rtclip)">{bg}'
            f'<path d="{d}" fill="none" stroke="{esc(color)}" stroke-width="3.2" '
            'stroke-linejoin="round" stroke-linecap="round" '
            'style="filter:drop-shadow(0 0 4px rgba(78,161,255,.55))"/>'
            f'<text x="8" y="{H - 7}" class="mapsrc">高德地图</text></g></svg>')


def _route_block(token: str, gid: int, today: str) -> str:
    """足迹页底部的「路线分享」：已分享的列表 + 可分享的活动。

    候选从**本地库**读（只有本机有轨迹），已分享的从服务端读。
    分享是逐条主动授权：这里没有「全部共享」，因为一旦上传就是真经纬度。
    """
    from . import group as G

    try:
        _r = G.routes(gid)
        rows = (_r.get("rows") or []) if _r.get("ok") else []
    except Exception:      # noqa: BLE001 - 拉不到就只显示候选
        rows = []
    mine = [x for x in rows if x.get("me")]
    others = [x for x in rows if not x.get("me")]

    shared: set[str] = set()
    try:
        shared = {str(x.get("title") or "") for x in mine}
    except Exception:      # noqa: BLE001
        shared = set()

    cands: list[dict] = []
    try:
        from . import appapi as _A
        from .store import Store as _S

        _st = _S(_A.DB)
        try:
            cut = (dt.date.fromisoformat(today) - dt.timedelta(days=14)).isoformat()
            for a in _st.conn.execute(
                    "SELECT activity_id, name, date, distance_m FROM activities"
                    " WHERE distance_m >= 3000 AND date >= ? AND date <= ?"
                    " ORDER BY date DESC, start_ts DESC LIMIT 12",
                    (cut, today)).fetchall():
                # 没有明细就没有轨迹，列出来点了也只会失败
                _det = _st.get_detail(a["activity_id"])
                if not _det:
                    continue
                # 爬楼/室内：距离是楼层折算，GPS 只剩漂移圈。这种轨迹画出来
                # 是在误导人 —— 一条 4.7 km 的「路线」实际只有操场那么大。
                try:
                    from .actcard import _track_span_m

                    # 缓存的明细是精简版：轨迹在 `track` 里，没有 geoPolylineDTO
                    _pts = _det.get("track") or []
                    if (_pts and _track_span_m(_pts) < 2000
                            and float(a["distance_m"] or 0) > 3000):
                        continue
                except Exception:      # noqa: BLE001 - 判不出来就按普通活动处理
                    pass
                cands.append(dict(a))
        finally:
            _st.conn.close()
    except Exception:      # noqa: BLE001 - 本地库读不到就只剩已分享列表
        cands = []

    body = ""
    if mine:
        # 自己分享的那条也必须能点进详情：批注入口在详情页里，
        # 原来这行只有「撤回」按钮、标题不是链接，作者反而进不去自己的路线。
        _lines = "".join(
            f'<div class="kvline"><span>{esc(str(x.get("date") or "")[5:])}</span>'
            f'<b><a href="/groups/{gid}/route/{int(x.get("id") or 0)}" '
            f'style="color:inherit;text-decoration:none">'
            f'{esc(x.get("title") or "路线")}'
            f'<s>·</s>{float(x.get("distance_m") or 0) / 1000:.1f} km'
            '<span style="color:#8a93a3;margin-left:4px">›</span></a></b>'
            f'<form action="/__group/{gid}/route_del" method="post" style="margin:0">'
            + _tok(token)
            + f'<input type="hidden" name="rid" value="{int(x.get("id") or 0)}">'
            '<button class="btn sm" type="submit">撤回</button></form></div>'
            for x in mine)
        body += ('<p class="note">我分享的（同组能看完整轨迹）</p>'
                 f'<div class="flat">{_lines}</div>')
    if others:
        _ol = "".join(
            f'<a class="kvline" href="/groups/{gid}/route/{int(x.get("id") or 0)}">'
            f'<span>{esc(x.get("alias"))}</span>'
            f'<b>{esc(x.get("title") or "路线")}'
            f'<s>·</s>{float(x.get("distance_m") or 0) / 1000:.1f} km</b>'
            '<span class="chev">›</span></a>'
            for x in others)
        body += f'<div class="flat">{_ol}</div>'

    if not cands and not mine:
        return (body or '<p class="note">近 14 天没有 3 km 以上、'
                        '带轨迹的活动可以分享。</p>')

    # 每条候选默认收起：分享动作要勾一次「知道会包含位置」，
    # 摊开一列勾选框比点开一条更容易误触。
    items = ""
    for a in cands:
        if str(a.get("name") or "") in shared:
            continue
        km = float(a.get("distance_m") or 0) / 1000.0
        items += (
            f'<details class="fold"><summary>{esc(str(a.get("date") or "")[5:])}'
            f' · {esc(a.get("name") or "活动")} {km:.1f} km<span class="fx">›</span>'
            '</summary><div class="foldbody">'
            '<p class="note">分享后同组能看到这条路的完整轨迹，包含位置。'
            '随时可以撤回。</p>'
            f'<form action="/__group/{gid}/route_share" method="post">' + _tok(token) +
            f'<input type="hidden" name="act" value="{esc(a.get("activity_id"))}">'
            '<label class="ckline"><input type="checkbox" name="confirm" value="1">'
            ' 我知道这会包含位置</label>'
            '<div class="pager"><button class="btn sm primary" type="submit">'
            '分享这条</button></div></form></div></details>')
    if items:
        body += '<p class="note">可以分享的活动</p>' + items
    return body


RT_CSS = """
.rt-wrap{position:relative;border-radius:12px;overflow:hidden}
.rt-pin{position:absolute;transform:translate(-50%,-50%);width:27px;height:27px;
  border-radius:50%;background:#e8b06a;color:#1a1206;border:2px solid #0d1117;
  font-size:12px;font-weight:700;line-height:1;padding:0;cursor:pointer;
  box-shadow:0 2px 8px rgba(0,0,0,.45)}
.rt-pin:active{transform:translate(-50%,-50%) scale(.94)}
/* 有照片的钉子直接用缩略图：一眼看到那儿是什么，比一个数字有用 */
.rt-pin.isimg{width:38px;height:38px;padding:0;overflow:hidden;background:#0d1117}
.rt-pin.isimg img{width:100%;height:100%;object-fit:cover;display:block}
/* 选位置时的预览钉：不能点，只告诉作者「会落在哪」 */
.rt-pin.ptr{background:#4EA1FF;border-color:#fff;pointer-events:none;
  box-shadow:0 0 0 3px rgba(78,161,255,.35)}
.rt-sheet{position:fixed;inset:0;z-index:60;background:rgba(4,7,12,.78);
  display:flex;align-items:center;justify-content:center;padding:18px}
.rt-sheet[hidden]{display:none}
.rt-card{background:#151b26;border:1px solid rgba(128,128,128,.2);border-radius:14px;
  width:100%;max-width:520px;max-height:82vh;overflow:auto;padding:14px}
.rt-card img{display:block;width:100%;border-radius:10px;margin-bottom:12px}
.rt-card p{margin:0 0 14px;font-size:15px;line-height:1.7}
.rt-card .rt-hd{display:flex;justify-content:space-between;align-items:center;
  margin-bottom:10px;font-size:13px;color:#8a93a3}
.rt-bar{margin-top:14px;gap:8px}
.rt-form{margin-top:12px;padding-top:12px;
  border-top:1px solid var(--line,rgba(255,255,255,.065))}
.rt-form textarea{width:100%;min-height:56px;margin:0 0 10px;padding:10px 12px;
  border-radius:12px;border:1px solid var(--line,rgba(255,255,255,.065));
  background:var(--surface-2,#111B29);color:var(--ink,#E8F0FB);font:inherit;
  line-height:1.6;resize:vertical}
/* 原生 file 框在手机上和这套胶囊按钮完全不是一个东西：藏起来，
   用 label 冒充按钮去触发它。opacity:0 而不是 display:none——
   后者在部分 WebView 里点 label 不弹选择器。 */
.rt-file{position:absolute;width:1px;height:1px;opacity:0;overflow:hidden}
.rt-pos{display:inline-flex;align-items:center;min-height:44px;font-size:12px;
  color:var(--muted,#7C8FA8);font-variant-numeric:tabular-nums;margin-left:auto}
.rt-tip{font-size:12px;color:var(--muted,#7C8FA8);margin:8px 0 0}
"""


def _notes_block(gid: int, rid: int, notes: list, author: bool, token: str,
                 svg: str, can_gpx: bool = False) -> tuple:
    """路线批注：地图上的小圆钉 + 点开看大图和文字。

    视觉参照回放视频里「展示过的照片缩成小图钉在轨迹旁」那套：同一个小圆点
    语言，只是这里是可点的。位置存 0..1 相对坐标，跟着这张图走。

    操作行把「导出 GPX」和「加批注」排在同一行：两者都是对这条路线的动作，
    分两处会把次要信息的区域撑高。
    """
    import json as _js

    pins = ""
    data = []
    for i, n in enumerate(notes, 1):
        x = float(n.get("x") or 0) * 100
        y = float(n.get("y") or 0) * 100
        # 有照片就用照片本身当钉子（缩略图），没有才退化成编号圆点。
        # 这才是「小图标注」的样子：一眼看到那儿是什么，而不是一个数字。
        if n.get("has_img"):
            pins += (f'<button class="rt-pin isimg" type="button" data-i="{i}" '
                     f'style="left:{x:.2f}%;top:{y:.2f}%" '
                     f'aria-label="第 {i} 条批注">'
                     f'<img src="/groups/{gid}/route/{rid}/note/'
                     f'{int(n["id"])}/img" alt=""></button>')
        else:
            pins += (f'<button class="rt-pin" type="button" data-i="{i}" '
                     f'style="left:{x:.2f}%;top:{y:.2f}%" '
                     f'aria-label="第 {i} 条批注">{i}</button>')
        data.append({"i": i, "text": str(n.get("text") or ""),
                     "img": (f"/groups/{gid}/route/{rid}/note/{int(n['id'])}/img"
                             if n.get("has_img") else "")})

    sheet = ('<div class="rt-sheet" id="rt-sheet" hidden><div class="rt-card">'
             '<div class="rt-hd"><span id="rt-no"></span>'
             '<button class="btn sm" type="button" id="rt-close">关闭</button></div>'
             '<img id="rt-img" alt="批注照片" hidden>'
             '<p id="rt-text"></p></div></div>')

    # 操作行：导出和加批注并排一行。两者都是对这条路线的动作，
    # 分两处会把次要区域撑高，也显得零散。
    form = '<div class="pager rt-bar">'
    if can_gpx:
        form += (f'<button class="btn sm" type="button" '
                 f'onclick="rcGpx(\'/groups/{gid}/route/{rid}.gpx\')">'
                 f'导出 GPX</button>')
    if author:
        form += ('<button class="btn sm primary" type="button" '
                 'id="rt-add">加批注</button>')
    form += '</div>'

    if author:
        # 展开后：一行文字 + 一行按钮（选照片 / 保存 / 取消 / 位置）。
        # 位置放最右，用 min-height:44 和按钮同一基线。
        form += ('<div class="rt-form" id="rt-box" hidden>'
                 '<textarea id="rt-txt" maxlength="200" rows="2" '
                 'placeholder="这段路的情况或提醒"></textarea>'
                 '<div class="pager" style="margin-top:0">'
                 '<label class="btn sm" for="rt-file" id="rt-pick">选照片</label>'
                 '<input class="rt-file" type="file" id="rt-file" '
                 'accept="image/*">'
                 '<button class="btn sm primary" type="button" id="rt-save">'
                 '保存</button>'
                 '<button class="btn sm" type="button" id="rt-cancel">取消</button>'
                 '<span class="rt-pos" id="rt-pos">未选位置</span>'
                 '</div>'
                 '<p class="rt-tip" id="rt-hint">'
                 '在图上点一下，钉子会自动贴到路上</p>'
                 '</div>')

    js = ("<script>(function(){var D=" + _js.dumps(data, ensure_ascii=False) +
          ',TK=' + _js.dumps(token) + ',GID=' + str(int(gid)) +
          ',RID=' + str(int(rid)) + ';'
          'var sheet=document.getElementById("rt-sheet");'
          'var img=document.getElementById("rt-img"),tx=document.getElementById("rt-text");'
          'function open(n){var d=null;for(var k=0;k<D.length;k++){if(D[k].i===n)d=D[k];}'
          'if(!d)return;document.getElementById("rt-no").textContent="批注 "+n;'
          'tx.textContent=d.text||"（没有文字）";'
          'if(d.img){img.hidden=false;img.src=d.img;}else{img.hidden=true;img.removeAttribute("src");}'
          'sheet.hidden=false;}'
          'document.querySelectorAll(".rt-pin").forEach(function(b){'
          'b.addEventListener("click",function(){open(parseInt(b.dataset.i,10));});});'
          'if(sheet){sheet.addEventListener("click",function(e){'
          'if(e.target===sheet){sheet.hidden=true;}});'
          'document.getElementById("rt-close").addEventListener("click",function(){sheet.hidden=true;});}'
          # ---- 作者：点图选位置 + 压图 + 提交
          'var add=document.getElementById("rt-add");'
          'if(!add)return;'
          'var bar=add.parentNode;'
          'var pickLb=document.getElementById("rt-pick");'
          'var box=document.getElementById("rt-box"),pos=document.getElementById("rt-pos");'
          'var wrap=document.querySelector(".rt-wrap"),hint=document.getElementById("rt-hint");'
          'var X=null,Y=null,B64="",pick=false,prev=null;'
          # 吸附用的轨迹点（百分比坐标，服务端抽稀到 ~200 个）
          'var se=document.querySelector(".rt-wrap svg[data-line]"),LP=[];'
          'if(se&&se.dataset.line){LP=se.dataset.line.split(";").map(function(s){'
          'var a=s.split(",");return [parseFloat(a[0]),parseFloat(a[1])];});}'
          'function snap(x,y){if(!LP.length)return [x,y];var bd=1e9,bx=x,by=y;'
          'for(var i=0;i<LP.length;i++){var dx=LP[i][0]-x,dy=LP[i][1]-y,d=dx*dx+dy*dy;'
          'if(d<bd){bd=d;bx=LP[i][0];by=LP[i][1];}}return [bx,by];}'
          'add.addEventListener("click",function(){pick=true;box.hidden=false;'
          'bar.hidden=true;hint.textContent="在图上点一下，钉子会自动贴到路上";});'
          # 取消要把所有中间态清干净：位置、预览钉、照片、文字。
          # 留着位置但看起来像取消了，下次保存会钉到上一次那个点。
          'var cancel=document.getElementById("rt-cancel");'
          'if(cancel){cancel.addEventListener("click",function(){'
          'pick=false;box.hidden=true;bar.hidden=false;X=null;Y=null;B64="";'
          'var t0=document.getElementById("rt-txt");if(t0)t0.value="";'
          'var f0=document.getElementById("rt-file");if(f0)f0.value="";'
          'var lb0=document.getElementById("rt-pick");'
          'if(lb0)lb0.textContent="选照片";'
          'pos.textContent="未选位置";'
          'hint.textContent="在图上点一下，钉子会自动贴到路上";'
          'if(prev){prev.remove();prev=null;}});}'
          'if(wrap){wrap.addEventListener("click",function(e){'
          'if(!pick)return;var r=wrap.getBoundingClientRect();'
          'var nx=Math.min(100,Math.max(0,(e.clientX-r.left)/r.width*100));'
          'var ny=Math.min(100,Math.max(0,(e.clientY-r.top)/r.height*100));'
          'var sn=snap(nx,ny);nx=sn[0];ny=sn[1];'
          'X=nx/100;Y=ny/100;'
          'pos.textContent=Math.round(nx)+"% , "+Math.round(ny)+"%";'
          'if(!prev){prev=document.createElement("div");'
          'prev.className="rt-pin ptr";wrap.appendChild(prev);}'
          'prev.style.left=nx+"%";prev.style.top=ny+"%";'
          'hint.textContent="";});}'
          # 图片在浏览器里压到 900px / JPEG 0.72：手机原图 4 MB 直接上行
          # 会把上传拖到十几秒，也没必要存那么大
          'var f=document.getElementById("rt-file");'
          'if(f){f.addEventListener("change",function(){'
          'var file=f.files&&f.files[0];if(!file){B64="";return;}'
          'var fr=new FileReader();fr.onload=function(){'
          'var im=new Image();im.onload=function(){'
          'var m=900,s=Math.min(1,m/Math.max(im.width,im.height));'
          'var c=document.createElement("canvas");'
          'c.width=Math.round(im.width*s);c.height=Math.round(im.height*s);'
          'c.getContext("2d").drawImage(im,0,0,c.width,c.height);'
          'B64=c.toDataURL("image/jpeg",0.72);'
          'if(pickLb)pickLb.textContent="已选 "+Math.round(B64.length/1400)+" KB";'
          'hint.textContent="";};'
          'im.onerror=function(){hint.textContent="这张图读不出来";B64="";'
          'if(pickLb)pickLb.textContent="选照片";};'
          'im.src=fr.result;};fr.readAsDataURL(file);});}'
          'document.getElementById("rt-save").addEventListener("click",function(){'
          'if(X===null){hint.textContent="先在图上点一下选位置";return;}'
          'var t=document.getElementById("rt-txt").value.trim();'
          'if(!t&&!B64){hint.textContent="写一句话，或者放张照片";return;}'
          'hint.textContent="保存中…";'
          'fetch("/__route/note",{method:"POST",credentials:"same-origin",'
          'headers:{"Content-Type":"application/json"},'
          'body:JSON.stringify({token:TK,gid:GID,rid:RID,x:X,y:Y,text:t,image:B64})})'
          '.then(function(r){return r.json();})'
          '.then(function(d){if(d&&d.ok){location.reload();}'
          'else{hint.textContent=(d&&d.error)||"保存失败";}})'
          '.catch(function(){hint.textContent="保存失败，检查网络";});});'
          "})();</script>")

    body = ('<div class="rt-wrap" id="rt-map">' + svg + pins + '</div>'
            + sheet)
    return body, form, js


def route_page(token: str, gid: int, rid: int, message: str = "") -> str:
    """单条路线：地图 + 一条折线 + 批注钉子。点开才取坐标，清单页不带点。"""
    from . import group as G

    r = G.route(gid, rid)
    if not r.get("ok"):
        _body = (_msg(message)
                 + f'<div class="card"><p class="note">'
                   f'{esc(r.get("error") or "打不开这条路线")}</p>'
                 f'<div class="pager"><a class="btn" href="/groups/{gid}/map">'
                 '返回路线</a></div></div>')
        return page("GROUP", "路线", "", _body, back=("返回小组", f"/groups/{gid}"))
    rt = r.get("route") or {}
    pts = rt.get("points") or []
    svg = ""
    if len(pts) >= 3:
        try:
            from . import basemap as BM

            _id = f"rt{gid}-{rid}"
            bm = BM.ensure(pts, MAP_W, MAP_H, ident=_id)
            svg = _route_svg(pts, bm)
        except Exception:      # noqa: BLE001 - 底图拿不到也要画出形状
            svg = _route_svg(pts, None)

    km = float(rt.get("distance_m") or 0) / 1000.0
    who = "我" if rt.get("me") else str(rt.get("alias") or "队友")
    notes = rt.get("notes") or []
    _author = bool(rt.get("me"))
    _js = ""
    if svg:
        _map, _form, _js = _notes_block(gid, rid, notes, _author, token, svg,
                                        can_gpx=len(pts) >= 2)
        _map_html = f'<div class="card">{_map}</div>'
    else:
        _map_html = '<div class="card"><p class="note">这条路线没有轨迹点。</p></div>'
        # 没有地图（底图取景失败）也可能有足够多的点导出，
        # 所以导出按钮不能只挂在 _notes_block 里
        _form = ((f'<div class="pager rt-bar"><button class="btn sm" type="button" '
                  f'onclick="rcGpx(\'/groups/{gid}/route/{rid}.gpx\')">'
                  f'导出 GPX</button></div>') if len(pts) >= 2 else "")
    body = (_msg(message)
            + _map_html
            + sec("这条路", f'<div class="kvrows">'
                            f'<span class="k">谁</span><span class="v">{esc(who)}</span>'
                            f'<span class="k">时间</span>'
                            f'<span class="v">{esc(str(rt.get("date") or ""))}</span>'
                            f'<span class="k">距离</span>'
                            f'<span class="v">{km:.1f} km</span></div>'
                            + (_form or "")))
    # WebView 没有 DownloadListener，`<a href>` 指向带 Content-Disposition 的
    # 地址会被**静默忽略**（点了毫无反应）。交给系统浏览器打开本地地址，
    # 由它去下载；桌面浏览器没有 GC，直接跳转即可。
    _gpx_js = ('<script>window.rcGpx=function(u){'
               'if(window.GC&&GC.action){GC.action("openUrl:"+u);}'
               'else{location.href=u;}};</script>')
    _html = page("GROUP", esc(rt.get("title") or "路线"), f"{who} · {km:.1f} km",
                 body, foot="这条路线的坐标只给组内的人看，撤回即删。",
                 css=(RT_CSS if svg else ""),
                 back=("返回小组", f"/groups/{gid}"))
    # 批注脚本和导出脚本都落在 </body> 前：page() 只负责外壳，行为在调用方拼。
    # 导出脚本即使没有批注也要有（作者没有批注时批注脚本是空的）。
    return _html.replace("</body>", _js + _gpx_js + "</body>")


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


def _chal_pair(c: dict) -> tuple[str, str]:
    """把一条进度拆成「主数 / 分母」。榜要用右侧的大数字 + 淡色分母，
    而 `_chal_prog` 是给一行纯文本用的（带「目标」字样）。"""
    kind = str(c.get("kind") or "distance")
    cur = float(c.get("current") or 0)
    tgt = float(c.get("target") or 0)
    if kind == "distance":
        return f"{cur / 1000:.1f}", f" / {tgt / 1000:.1f} km"
    if kind == "acts":
        return f"{cur:.0f}", f" / {tgt:.0f} 次"
    if kind == "elevation":
        return f"{cur:.0f}", f" / {tgt:.0f} m"
    if kind == "explore":
        return f"{cur:.0f}", f" / {tgt:.0f} 格"
    if kind == "speed":
        return _pace_str(cur), f" / {_pace_str(tgt)}"
    return f"{cur:.0f}", f"/{tgt:.0f}"


def challenge_page(token: str, gid: int, cid: int, message: str = "") -> str:
    """单个赛事的完成度榜：谁跑了多少、谁已经达标。

    组页那张赛事卡只有「全组合计」——一个人问「我在这场比赛里排第几、
    队友跑到哪了」，页面上没有答案。服务端早就按人算好了（`/challenge/{cid}`
    给每人的 current / pct / 首次达标日），缺的只是一个页面。
    """
    from . import group as G

    today = dt.date.today().isoformat()
    r = G.challenge_get(gid, cid, today)
    if not r.get("ok"):
        body = (_msg(message)
                + f'<div class="card"><p class="note">'
                  f'{esc(r.get("error") or "打不开这个赛事")}</p>'
                  f'<div class="pager"><a class="btn" href="/groups/{gid}#chal">'
                  '返回小组</a></div></div>')
        return page("GROUP", "赛事", "", body, back=("返回小组", f"/groups/{gid}#chal"))

    ch = r.get("challenge") or {}
    rows = r.get("rows") or []
    hidden = int(r.get("hidden") or 0)
    kind = str(ch.get("kind") or "distance")
    pct = float(ch.get("pct") or 0)
    done = pct >= 100
    _from, _to = str(ch.get("from") or "")[5:], str(ch.get("to") or "")[5:]
    state_txt = {"running": "进行中", "closed": "已结束", "ended": "已到期"}.get(
        str(ch.get("status") or ""), "")
    _done_n = sum(1 for x in rows if x.get("done_at"))

    # 有角色才能决定要不要给「提前结束」：detail 拿不到就退化成只读，
    # 赛事本身仍然看得到（不能因为一次请求失败把榜也藏了）。
    try:
        d = G.detail(gid) or {}
    except Exception:      # noqa: BLE001
        d = {}
    role = ((d.get("group") or {}).get("role")) or ""
    is_admin = role in ("owner", "admin")

    head = (f'<div class="card">'
            f'<div class="kvline"><span>全组进度</span><b>{esc(_chal_prog(ch))}'
            f' · {pct:.0f}%</b></div>'
            f'<div class="bar{" done" if done else ""}">'
            f'<i style="width:{min(100, pct):.0f}%"></i></div>'
            f'<div class="kvline"><span>周期</span><b>{esc(_from)} – {esc(_to)}'
            f' · {esc(state_txt)}</b></div>'
            f'<div class="kvline"><span>参与</span><b>{len(rows)} 人'
            + (f' · {_done_n} 人已达成' if _done_n else "")
            + '</b></div></div>')

    lis = ""
    for i, x in enumerate(rows, 1):
        _num, _den = _chal_pair({"kind": kind, "current": x.get("current"),
                                 "target": ch.get("target")})
        _dd = str(x.get("done_at") or "")
        mk = (f'<span class="mk"><span class="done">✓ {esc(_dd[5:])}</span></span>'
              if _dd else "")
        lis += (f'<div class="{"rank me" if x.get("is_me") else "rank"}">'
                f'<span class="no">{i}</span>'
                f'<span class="nm">{esc(x.get("alias"))}</span>{mk}'
                f'<span class="km">{_num}<i>{_den}</i></span></div>')
    if not lis:
        lis = '<p class="note">还没有人上榜。打开 App 同步一次。</p>'
    if hidden:
        lis += (f'<p class="note">另有 {hidden} 人关闭了共享，不参与这场比赛。</p>')

    close_form = ""
    if is_admin and not ch.get("status") == "closed":
        close_form = (
            '<details class="fold"><summary>提前结束这个赛事'
            '<span class="fx">›</span></summary><div class="foldbody">'
            f'<form action="/__group/{gid}/challenge/{int(cid)}/close" method="post">'
            + _tok(token)
            + '<p class="note">结束后不再累计，成绩保留。到期自动结束。</p>'
              '<div class="pager"><button class="btn danger" type="submit" '
              'onclick="return confirm(\'提前结束这个赛事？\')">结束</button></div>'
              '</form></div></details>')

    body = (_msg(message)
            + sec("这场怎么算", head)
            + sec("谁排第几", '<div class="flat">' + lis + '</div>')
            + ('<p class="note">已达成按达成日排，其余按完成度排。</p>'
               if kind != "speed" else
               '<p class="note">配速 = 累计时长 ÷ 累计距离。</p>')
            + close_form)
    return page("GROUP", str(ch.get("title") or "赛事"),
                f'{len(rows)} 人 · {pct:.0f}%', body,
                foot="只上传里程合计和脱敏轨迹剪影，不含坐标和位置。",
                back=("返回小组", f"/groups/{gid}#chal"))


def _pace_str(sec_per_km) -> str:
    """秒/公里 → 5'30"。赛事里配速是**目标值**（不是算出来的），所以单列。"""
    v = int(round(float(sec_per_km or 0)))
    if v <= 0:
        return "—"
    return f"{v // 60}'{v % 60:02d}\""


def _chal_prog(c: dict) -> str:
    """赛事的「当前 / 目标」。五种类型各说各的单位，不共用一个 km。"""
    kind = str(c.get("kind") or "distance")
    cur = float(c.get("current") or 0)
    tgt = float(c.get("target") or 0)
    if kind == "distance":
        return f"{cur / 1000:.1f} / {tgt / 1000:.1f} km"
    if kind == "acts":
        return f"{cur:.0f} / {tgt:.0f} 次"
    if kind == "elevation":
        return f"{cur:.0f} / {tgt:.0f} m 爬升"
    if kind == "explore":
        return f"{cur:.0f} / {tgt:.0f} 格新地方"
    if kind == "speed":
        return f"{_pace_str(cur)} / 目标 {_pace_str(tgt)}"
    return f"{cur:.0f} / {tgt:.0f}"


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


def _act_card(a: dict, today: str, detail: bool = False, *,
              gid: int = 0, token: str = "", emojis=None,
              open_cm: bool = False) -> str:
    """一条动态的卡片：谁 / 轨迹剪影 / 距离 / 元信息 / 最新三条评论。

    剪影是**脱敏**的：客户端把轨迹抽稀到 24 点、等比缩放到 100×100 再上传，
    不含经纬度、起终点和朝向以外的信息（见 group._shape）。
    没有 GPS 的活动（室内/器械）或旧数据没有剪影，就画占位方块写「无轨迹」——
    绝不拿一条装饰曲线冒充路线。

    `open_cm=True` 让这条的评论区默认展开：刚发完评论 303 回来时用，
    否则整页重渲染，用户刚打的字连同刚展开的评论一起消失。
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

    # 评论：**卡片内展开**，不再跳「这条」详情页。
    # 收起态只留最新一条（一眼看出有没有人说话），点开展开全部 + 发言框。
    cm = ""
    if not detail:
        rec = a.get("recent") or []
        cnt = int(a.get("comments") or 0)
        _aid = int(a.get("id") or 0)
        if rec:
            _c = rec[-1]
            head = ('<span class="ac-sumline"><b>' + esc(_c.get("alias")) + '</b>'
                    + '<span>' + esc(_cm_full(_c)) + '</span></span>')
        else:
            head = '<span class="ac-sumline none">还没有评论</span>'
        label = ("共 %d 条" % cnt if cnt > 1 else ("去留一句" if not cnt else "看评论"))

        body = ""
        for c in rec:
            _one = ('<div class="cm"><span class="cm-b"><b>'
                    + esc(c.get("alias")) + '</b>'
                    + '<span>' + esc(_cm_full(c)) + '</span></span>')
            if c.get("mine"):
                _one += _cm_edit(gid, _aid, c, token, emojis)
            _one += '</div>'
            body += _one

        # 发言区：输入框 + 一排表情（插进光标）+ 发。搬到卡片里，发完停在
        # 这条动态上，不再跳来跳去。
        body += _cm_say(gid, _aid, token, emojis)
        # 摘要行在展开态要换掉：还写着「去留一句」的话，下面又摆着表情和
        # 输入框，等于同一件事说两遍。展开时只留一个「收起」。
        # data-k 是给「回来后还原展开状态」用的稳定键（see KEEP_JS）。
        cm = ('<div class="ac-c"><details class="ac-cm" data-k="cm' + str(_aid) + '"'
              + (" open" if open_cm else "") + '><summary>'
              + head + '<span class="ac-cnt">' + label + ' ›</span>'
              + '<span class="ac-cnt up">收起 ›</span></summary>'
              + '<div class="ac-cb">' + body + '</div></details></div>')

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
        cmts = ""
        for c in (one.get("recent") or []):
            _body = (f'<b>{esc(c.get("alias"))}</b>'
                     + f'<span>{esc(_cm_full(c))}</span>')
            # 只有自己的评论给「改 / 删」，折在铅笔里：直接摆删除按钮在评论
            # 旁边，滑一下就会点到。
            more = _cm_edit(gid, act, c, token, emojis) if c.get("mine") else ""
            cmts += (f'<div class="cm"><span class="cm-b">{_body}</span>'
                     f'{more}</div>')
        body = (_msg(message)
                + sec("这条", _act_card(one, today, detail=True))
                + (sec("评论", '<div class="flat">' + cmts + '</div>') if cmts else "")
                + sec("说一句", '<div class="flat">'
                      + _cm_say(gid, act, token, emojis) + '</div>')
                + SAY_JS)
        return page("GROUP", "动态", f'{esc(one.get("alias"))} · '
                    f'{esc(one.get("kind_name"))}', body,
                    back=("返回动态", f"/groups/{gid}/feed"))

    items = ""
    for a in rows:
        items += ('<div class="acard">'
                  + _act_card(a, today, gid=gid, token=token,
                              emojis=emojis) + '</div>')
    if not items:
        items = ('<div class="card"><p class="note">最近还没有动态。'
                 '同步一次后，你和队友的活动概要会出现在这里。</p></div>')
    hidden = int(r.get("hidden") or 0)
    # 剪影从 1.5.11 才开始上传，老数据全是空的。用户一进来看到一片「无轨迹」
    # 会以为功能坏了，而补报是**手动**的（设置页那个「立即」），所以要明说。
    _no_shape = bool(rows) and not any(str(x.get("shape") or "") for x in rows)
    body = (_msg(message)
            + ('<p class="note">轨迹要补报一次：小组设置 → 上报 → 立即，'
               '补最近 14 天。</p>'
               if _no_shape else "")
            + sec("大家最近", items)
            + (f'<p class="note">另有 {hidden} 人关闭了共享，不出现在这里。</p>'
               if hidden else "")
            + '<p class="note">轨迹只有形状：没有底图，不标起终点，不含经纬度。'
              '配速按距离 ÷ 时长算。</p>'
            + SAY_JS)
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
# 组页里一屏放多少条动态。20 条约 4000px，翻两屏到赛事后面接上，
# 再多首屏 HTML 会到 200KB 以上（每张卡带一条轨迹形状）。
FEED_PAGE = 20
# 组页首屏榜单只放前 10：组里人一多，一屏十几二十行榜单会把动态整个推到
# 第三屏之后，「往下滑看动态」就变成了「往下滑看人名」。完整名次交给
# `/groups/{gid}/board` 那一页。
BOARD_TOP = 10


def _rank_row(i: int, x: dict, marks: str = "", value_m=None) -> str:
    cls = "rank me" if x.get("is_me") else "rank"
    mk = f'<span class="mk">{marks}</span>' if marks else ""
    # value_m：综合榜要显示综合距离，不能用 distance_m（那只是跑步）
    _v = x.get("distance_m") if value_m is None else value_m
    return (f'<div class="{cls}"><span class="no">{i}</span>'
            f'<span class="nm">{esc(x.get("alias"))}</span>{mk}'
            f'<span class="km">{_km(_v)}<i>km</i></span></div>')


def _rank_marks(x: dict, marks_of: dict[int, str], min_cells: int) -> str:
    """名字后面挂的图形：穿了哪几件衫 + 有没有探索徽章。

    只对**周**榜有意义（衫和徽章都是按周结算的），月榜一律不挂——
    挂上去会被读成「这个月的衫」，那是错的。

    索引键用 user_id，不用 alias：组里两个人重名是常事，按名字匹配会把
    衫挂到另一个人头上（老服务端不返回 user_id，这时退回按名字）。
    """
    uid = x.get("user_id")
    if uid is None:
        m = marks_of.get(str(x.get("alias") or ""), "")  # type: ignore[arg-type]
    else:
        m = marks_of.get(int(uid), "")
    if int(x.get("explorer") or 0) >= min_cells:
        m += _explore_icon("jr", "探索徽章")
    return m


def group_page(token: str, gid: int, message: str = "", feed_n: int = 0,
               focus: int = 0, at: str = "") -> str:
    """组页：我的名次 → 本周玩法 → 本周榜（前 10）→ 赛事 → 动态。

    `focus` 是刚从这条动态发完评论回来时要展开评论区的活动 id。
    `at` 是「这句提示属于哪一块」（`chal` = 赛事）。组页很长，提示渲染在
    页首的话，在页面中部点「建赛事」的人根本看不到——看起来就是没反应。
    """
    from . import group as G

    today = dt.date.today().isoformat()
    msg = _msg(message)
    # 赛事相关的提示直接落在赛事区块里（页首就不再重复一遍）
    chal_msg = msg if at == "chal" else ""
    if chal_msg:
        msg = ""
    # 一次拿齐四块（detail / board / challenges / season）。老服务端没有
    # /home 就退回逐个请求——四次握手约 0.5 秒，能省则省。
    _h = {}
    try:
        _h = G.home(gid, "week", today)
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
            _st.conn.close()
    except Exception:      # noqa: BLE001 - 缓存写不进去不影响页面
        pass

    b = _h.get("board") or G.board(gid, "week", today)
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
    _marks_of: dict = {}
    for _p in (_se.get("people") or []):
        _one = ""
        for _x in (_p.get("shirts") or []):
            _one += _jersey(str(_x.get("color") or ""),
                            _pat_of.get(str(_x.get("id") or ""), ""), True, "jr",
                            f'{_x.get("name") or ""}·{_x.get("title") or ""}')
        _marks_of[int(_p.get("user_id") or 0)] = _one
        _marks_of[str(_p.get("alias") or "")] = _one   # 老服务端没有 user_id 时兜底
    _min_cells = int(((_se.get("explore") or {}).get("min_cells")) or 5)

    # 本周榜。名字后面**只挂图标**：穿了哪几件衫（衣服）+ 有没有探索徽章
    # （虚线方框），原来的「我 / 组长 / N 天 / 新 N 格」四段小字全删了——
    # 四段字挤在名字后面，一屏十几行读起来像注脚；徽章本来就有图形，
    # 直接挂图形更快认。我自己那一行靠底色和品牌色区分，不用再写「我」。
    #
    # 只渲染前 BOARD_TOP 名。我排在 10 名开外时补「⋯」+ 我自己那一行：
    # 截断了却不让我看见自己，等于把榜单最该给的信息截掉了。
    _rows = b.get("rows") or []
    rank_rows = "".join(_rank_row(i, x, _rank_marks(x, _marks_of, _min_cells))
                        for i, x in enumerate(_rows[:BOARD_TOP], 1))
    _mine_at = next((i for i, x in enumerate(_rows, 1) if x.get("is_me")), 0)
    if _mine_at > BOARD_TOP:
        rank_rows += ('<div class="rk-gap" aria-hidden="true">⋯</div>'
                      + _rank_row(_mine_at, _rows[_mine_at - 1],
                                  _rank_marks(_rows[_mine_at - 1], _marks_of, _min_cells)))
    hidden = int(b.get("hidden") or 0)
    # 一个人对着排行榜跟自己比：一条数据 + 半屏空白。这不是「空位」，
    # 是页面没告诉用户下一步干什么。直接换成一句状态 + 一个邀请入口。
    # 一个人组：榜单照常渲染（自己那一行也有信息量——里程、天数），
    # 只是在后面补一段「把码发出去」。原来直接把榜整块换成空态，
    # 用户一进来看不到「本周榜」只剩一句提示，观感像页面坏了。
    if _n <= 1:
        _code = str(g.get("invite_code") or "")
        if _code:
            rank_rows += ('<p class="note">本周还没有队友。把邀请码发给他们，'
                          '加入并上报后就会上榜。</p>'
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
        rank_rows = '<p class="note">本周还没有人上报。打开 App 才会同步。</p>'
    if hidden:
        rank_rows += f'<p class="note">另有 {hidden} 人关闭了共享，不参与排名。</p>'
    # 完整榜单是**另一个页面**：组页要看动态，榜单不能无限长。
    # 只有超过 10 人才需要这个入口——两个人的组摆一个「完整榜单」很荒唐。
    if len(_rows) > BOARD_TOP:
        rank_rows += ('<div class="pager"><a class="btn sm full" '
                      f'href="/groups/{gid}/board">完整榜单 · 共 {len(_rows)} 人 →</a></div>')

    # 赛事
    ch_rows = ""
    for c in (cl.get("challenges") or []):
        pct = float(c.get("pct") or 0)
        done = pct >= 100
        state_txt = {"running": "进行中", "closed": "已结束", "ended": "已到期"}.get(
            c.get("status") or "", "")
        # 整张卡都是入口：组页只给「全组合计」，谁跑了多少在详情页里。
        # 这也解释得通为什么每行右侧要有 ›——之前没有任何可点的迹象。
        ch_rows += (f'<a class="chal" href="/groups/{gid}/challenge/{int(c.get("id") or 0)}">'
                    f'<div class="t">{esc(c.get("title"))}'
                    '<span class="chev">›</span></div>'
                    f'<div class="m">{esc(_chal_prog(c))} · '
                    f'{pct:.0f}% · {esc(state_txt)} · {esc(str(c.get("from"))[5:])}–'
                    f'{esc(str(c.get("to"))[5:])}</div>'
                    f'<div class="bar{" done" if done else ""}">'
                    f'<i style="width:{min(100, pct):.0f}%"></i></div></a>')
    if not ch_rows:
        ch_rows = '<p class="note">还没有赛事。组长可以建一个，全组自动参与。</p>'

    chal_form = ""
    if is_admin:
        # 类型一变，单位跟着变：只写「目标（米/次）」的话，选「爬升」还得
        # 自己猜是不是米、选「配速」更猜不到要填 330 这种秒数。
        # 每种类型的单位/示例挂在 option 上，选择时同步到标签和 placeholder。
        #
        # 属性名**不能**用 data-unit / data-ph：`form.querySelector("[data-unit]")`
        # 会按文档序返回第一个匹配，而第一个匹配是 <option data-unit>——
        # 于是选一次类型，「累计里程」这个选项的文字就被改成了「米」。
        # option 用 data-u / data-p，标签和输入框才用 data-unit / data-target。
        _kinds = [
            ("distance", "累计里程", "米", "20000", "20000 = 20 km"),
            ("acts", "累计次数", "次", "12", "练了几次，不分长短"),
            ("elevation", "累计爬升", "米", "3000", "3000 = 3 km 爬升"),
            ("explore", "探索新地方", "格", "40", "跑过的新格子数，500 m 一格"),
            ("speed", "平均配速", "秒/km", "330", "330 = 5'30\"/km，越小越难"),
        ]
        _opts = "".join(
            f'<option value="{k}" data-u="{u}" data-p="{ph}">{n}</option>'
            for k, n, u, ph, _x in _kinds)
        _hint = "；".join(f'{n}填{h}' for _k, n, _u, _ph, h in _kinds)
        # 结束日给个默认值（本周日）：留空直接点「建赛事」会被服务端按
        # 「end_date 格式不对」拒掉，而那句提示在页首、用户停在页面中部，
        # 看起来就是「点了没反应」。
        try:
            _d0 = dt.date.fromisoformat(today)
            _end0 = (_d0 + dt.timedelta(days=6 - _d0.weekday())).isoformat()
        except ValueError:
            _end0 = ""
        chal_form = (f'<form action="/__group/{gid}/challenge" method="post">{_tok(token)}'
                     '<div class="card">'
                     '<div class="kvline"><span>标题</span><b>'
                     '<input type="text" name="title" maxlength="30" placeholder="本周 20 公里"></b></div>'
                     '<div class="kvline"><span>类型</span><b>'
                     f'<select name="kind">{_opts}</select></b></div>'
                     '<div class="kvline"><span>目标（<b data-unit>米</b>）</span><b>'
                     '<input type="number" name="target" min="1" max="1000000" step="1" '
                     'placeholder="20000" data-target></b></div>'
                     '<div class="kvline"><span>开始日</span><b>'
                     f'<input type="text" name="start_date" value="{esc(today)}" placeholder="YYYY-MM-DD"></b></div>'
                     '<div class="kvline"><span>结束日</span><b>'
                     f'<input type="text" name="end_date" value="{esc(_end0)}" placeholder="YYYY-MM-DD"></b></div>'
                     '</div><p class="note">' + esc(_hint) + '。'
                     '周期最长 90 天，开始日不能早于 7 天前。</p>'
                     '<div class="pager"><button class="btn" type="submit">建赛事</button></div>'
                     '</form>'
                     + CHAL_JS)

    # 成员不再单开一块：榜上就是成员。「另有 N 人关闭了共享」已经跟在
    # 榜单行后面（rank_rows 里），这里不再重复一遍。

    # 昵称 / 共享 / 上报 / 邀请码 / 退出全部搬到「小组设置」页了：
    # 组页主体是排名和动态，把改昵称这种低频操作压在最下面，用户的原话是
    # 「放在最下面太突兀」。这里只留一个页头齿轮。
    _gear = ('<a class="hact" href="/groups/{0}/settings" aria-label="小组设置">'
             '<span aria-hidden="true">⚙</span></a>').format(gid)

    # 建赛事也是折叠：一个月用一次的表单不该常驻占掉半屏。
    # data-k 是给 KEEP_JS 用的：提交后 303 回来要还是展开的，否则用户
    # 建完/建失败看到的都是一行「建一个赛事」，像点击根本没生效。
    chal_fold = ""
    if chal_form:
        chal_fold = ('<details class="fold" data-k="chal"><summary>建一个赛事'
                     '<span class="fx">›</span></summary>' + chal_form + '</details>')

    # 页头胶囊只说一件新事：全组本周跑了多少。
    # 「N 人」已经写在副标题里，再放一枚同样的胶囊等于把一行字抄两遍；
    # 一个人组里「全组」就等于「我」，所以干脆不出这枚胶囊。
    chips = ""
    if _n > 1:
        chips = f'<span class="chip">全组 {_km(total_m)} km 本周</span>'

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
            if _n <= 1:
                _k = "本周成绩"
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
            # ---- 分数明细：这 50.6 分是哪几项凑出来的
            # 服务端把每项的得分（parts）和权重（weights）都下发，页面只负责
            # 摆。写死权重的后果是「改了规则页面还在说旧话」，那比不解释更糟。
            _pt = _me.get("parts") or {}
            _wt = _se.get("weights") or {}
            _mx = _se.get("max") or {}

            def _num(field: str, fmt: str) -> str:
                v = _me.get(field)
                try:
                    return fmt % float(v or 0)
                except (TypeError, ValueError):
                    return "—"

            def _top(field: str, fmt: str) -> str:
                v = _mx.get(field)
                try:
                    return fmt % float(v or 0)
                except (TypeError, ValueError):
                    return "—"

            _items = [
                ("跑量", "km", "km", _num("km", "%.1f"), _top("km", "%.1f") + " km"),
                ("出勤", "days", "days", _num("days", "%.0f"), _top("days", "%.0f") + " 天"),
                ("爬升", "elev", "elev", _num("elev", "%.0f"), _top("elev", "%.0f") + " m"),
                # 进步是比值：0.08 要显示成 +8%
                ("进步", "progress", "progress",
                 f'{float(_me.get("progress") or 0) * 100:+.0f}%',
                 f'{float(_mx.get("progress") or 0) * 100:+.0f}%'),
                ("连跑", "streak", "streak", _num("streak", "%.0f"),
                 _top("streak", "%.0f") + " 天"),
            ]
            _sp = ""
            for _label, _field, _wkey, _mine_txt, _top_txt in _items:
                _w = float(_wt.get(_wkey) or 0)
                _s = float(_pt.get(_field) or 0)
                _p = min(100.0, _s / _w * 100) if _w else 0.0
                _unit = "km" if _field == "km" else (
                    "m" if _field == "elev" else ("天" if _field in ("days", "streak") else ""))
                _sp += (f'<div class="sp"><div class="sp-h"><span>{_label}</span>'
                        f'<b>{_s:g}<i> / {_w:g} 分</i></b></div>'
                        f'<div class="bar"><i style="width:{_p:.0f}%"></i></div>'
                        f'<div class="sp-s">我 {esc(_mine_txt + (" " + _unit if _unit else ""))}'
                        f' · 组里最高 {esc(_top_txt)}</div></div>')
            _bn = float(_pt.get("bonus") or 0)
            _bp = min(100.0, _bn / float(_xb) * 100) if _xb else 0.0
            _sp += (f'<div class="sp"><div class="sp-h"><span>加分</span>'
                    f'<b>{_bn:g}<i> / {float(_xb):g} 分</i></b></div>'
                    f'<div class="bar{" done" if _bn > 0 else ""}">'
                    f'<i style="width:{_bp:.0f}%"></i></div>'
                    f'<div class="sp-s">让分赛与预言的加分</div></div>')

            # ---- 称号：为什么是「同行者」
            _tlist = ""
            for _t in (_se.get("titles") or []):
                _tn = str(_t.get("name") or "")
                _is_me_t = _tn and _tn == _tt
                _tlist += (f'<div class="kvline"><span>{esc(_tn)}</span><b>'
                           f'{esc(str(_t.get("hint") or ""))}'
                           + ('<i> · 你</i>' if _is_me_t else "") + '</b></div>')
            # 称号的判定有优先级（同行者 > 远征队长 > 先锋 > 行者），
            # 所以「我为什么是这个」不能只说门槛，要说命中的那一条。
            _why = {
                "同行者": f'全组这周都动了，你也已参与 {int(_me.get("weeks") or 1)} 周',
                "远征队长": f'综合分第 {_rk} 名，前 10%',
                "先锋": f'综合分第 {_rk} 名，前 30%',
                "行者": f'已参与 {int(_me.get("weeks") or 1)} 周',
            }.get(_tt, "")
            _how = (f'<details class="scorehow" data-k="scorehow">'
                    f'<summary>详细情况'
                    '<span class="fx">›</span></summary>'
                    f'<div class="shbody">'
                    f'<p class="note">名次按综合分排，同分并列。每项和组里'
                    f'最高的比，满分 {float(_bm):g}，加分最多 {float(_xb):g}。</p>'
                    + _sp
                    + '<div class="sp-t">称号</div>'
                    + (f'<p class="note">你现在是「<b>{esc(_tt)}</b>」：'
                       f'{esc(_why)}。称号只取最高的一个。</p>'
                       if _tt else
                       '<p class="note">这周还没有称号：综合分不在前 30%，'
                       '参与也还没满 3 周。</p>')
                    + (_tlist or "")
                    + '</div></details>')

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
                f'<span class="sc-more">荣誉 {_got}/6 ›</span></a>'
                + _how + '</section>')
    shirt_row = ""

    # ---- 本周玩法：目标 / 让分赛 / 预言
    # 一行放结论，改目标这种每周一次的动作折起来。榜单是每天看的，
    # 不能让它被表单顶下去——这是这个页面唯一的信息优先级。
    #
    # 用户反馈「看不明白这个功能怎么用，甚至不知道时间范围是什么」。
    # 所以这一块的外壳补了三样东西：①「周期」一行明写周一到周日 24:00；
    # ②让分/预言的文案从「我 30 / 他 75」改成有主语的短句；
    # ③一个「怎么算的」折叠块把三条规则讲全（数字来自服务端，不写死）。
    _wk = _h.get("week") or {}
    if not _wk.get("ok"):
        try:
            _wk = G.week(gid, today)
        except Exception:      # noqa: BLE001 - 老服务端没有 /week，整块不显示即可
            _wk = {}
    week_form = ""
    rows: list[tuple[str, str]] = []
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
        _rule = _wk.get("pair_rule") or {}

        # 周期排在最前面：不知道「到哪天截止」，目标定多高、什么时候还能改，
        # 全都没法判断。
        rows.append(("周期", f'{esc(str(_wk.get("from"))[5:])} – '
                             f'{esc(str(_wk.get("to"))[5:])}<s>·</s>'
                             '<b>周日 24:00 截止</b>'))

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

        # 让分赛：说清楚「谁多跑多少」，而不是把两个数字斜杠拼在一起。
        _pair = next((p for p in (_wk.get("pairs") or [])
                      if _me_uid in (p["small"]["user_id"], p["big"]["user_id"])), None)
        if _pair:
            _other = (_pair["big"] if _pair["small"]["user_id"] == _me_uid
                      else _pair["small"])
            _me_side = _pair["small"] if _pair["small"]["user_id"] == _me_uid else _pair["big"]
            _res = {"double": "双人徽章", "upset": "逆袭", "big_only": "让分达成",
                    "split": "一人达成", "none": "都还差一点"}.get(
                        _pair.get("result") or "", "")
            # 让出去多少 = 让分后的目标 - 自报目标。服务端有 handicap_km，
            # 但老服务端没有这个字段，所以这里也自己算一遍（别显示「多背 0」）。
            _hc = float(_pair.get("handicap_km") or 0) or max(
                0.0, float(_pair["big"]["target"]) - float(_pair["big"]["km"]))
            if _pair.get("even"):
                # 标签已经是「结对」了，值里再说一遍「和 xx 结对」就是重复。
                _ptxt = (f'<b>{esc(_other["alias"])}</b>'
                         f'<s>目标差不多，不打让分</s>各跑 '
                         f'{float(_me_side["target"]):g} km')
            else:
                # 「他 75 km」不说清 75 是怎么来的，看着像系统乱加的数。
                # 只写「多背 15 km」：怎么算出来的放在「这三个怎么算」里，
                # 这一行是 nowrap + 省略号，写太长会被截掉最关键的那半句。
                _ptxt = (f'<b>{esc(_other["alias"])}</b>'
                         f'我 {float(_me_side["target"]):g} km<s>·</s>'
                         f'他 {float(_other["target"]):g} km'
                         f'<s>（多背 {_hc:g} km）</s>')
            if _res:
                _ptxt += f'<b class="hit">{_res}</b>'
            rows.append(("结对", _ptxt))

        # 预言：猜谁完不成。命中/被打脸都写出来，不然「预言 阿狗 完不成」
        # 看不出结果是赚了还是亏了。
        _pred = (_wk.get("predict") or {}).get("mine")
        if _pred:
            _pa = next((r.get("target_alias")
                        for r in (_wk.get("predict") or {}).get("rows") or []
                        if r.get("user_id") == _me_uid), "")
            _pres = next((r.get("result")
                          for r in (_wk.get("predict") or {}).get("rows") or []
                          if r.get("user_id") == _me_uid), "")
            _ptail = {"hit": f'<b class="hit">猜中 +{float(_rule.get("predict_hit") or 2):g}</b>',
                      "wrong": f'<b>被打脸，他 +{float(_rule.get("predict_face") or 3):g}</b>',
                      "unknown": '<s>还没法结算</s>'}.get(str(_pres or ""), "")
            if _pa:
                rows.append(("预言", f'猜 <b>{esc(_pa)}</b><s>这周完不成</s>' + _ptail))

        # 表单：目标 +（可选）预言。选项只列报了 ≥5 km 目标的人——猜一个
        # 没目标的人没法判「完不成」，服务端也会拒；把 km 挂在名字后面，
        # 猜谁才有依据。
        _tg = _wk.get("targets") or []
        _opts = '<option value="">先不猜</option>'
        for t in _tg:
            _sel = " selected" if t["user_id"] == _pred else ""
            _opts += (f'<option value="{int(t["user_id"])}"{_sel}>'
                      f'{esc(t["alias"])} · {float(t.get("km") or 0):g} km</option>')
        _val = f'{float(_goal_km):g}' if _goal_km else (
            f'{_suggest:g}' if _suggest else "")
        _no_tgt = ('<p class="note">还没有队友报目标。等他们报了，这里就能选人猜了'
                   '（只能猜报了 5 km 以上目标的人）。</p>' if not _tg else "")
        # 入口行：左「目标 30 km」+ 右副文「已跑 12.3 / 30 km」，点开才是表单。
        # 48px 高、整行可点，和下面榜单条目同一节奏。
        week_form = ('<details class="goalrow" data-k="goal"><summary>'
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
                     + _no_tgt +
                     '<p class="note">只猜人、不改目标：目标框留空直接保存。</p>'
                     '</div></details>')

    # 「衫」那行内容是流式的（六件全有得主会折两行），要允许换行；
    # 其它行是单行结论，用省略号截断。
    week_line = ('<div class="kvrows">'
                 + "".join(
                     f'<span class="k">{k}</span>'
                     f'<span class="v{" wrap" if k == "衫" else ""}">{v}</span>'
                     for k, v in rows)
                 + "</div>") if rows else ""

    # 「怎么算的」：三条规则写全。用户的原话是「用户引导看不明白这个功能
    # 怎么用（甚至不知道时间范围是什么），应该怎么玩」——把规则藏在心里、
    # 只给一个输入框，等于让人猜。数字全部来自服务端下发，不写死。
    _hr = (_wk.get("pair_rule") or {}) if _wk.get("ok") else {}
    _hc_pct = int(round(float(_hr.get("handicap") or 0.5) * 100))
    _cap = float(_hr.get("cap") or 1.6)
    _hit_s = float(_hr.get("predict_hit") or 2)
    _face_s = float(_hr.get("predict_face") or 3)
    _min_km = float(_hr.get("min_km") or 5)
    week_how = (
        '<details class="fold" data-k="how"><summary>玩法说明'
        '<span class="fx">›</span></summary><div class="foldbody">'
        '<p class="note"><b>定目标</b>：这周打算跑多少 km。报了目标才会进让分赛，'
        '也才会被猜。周内可改。</p>'
        f'<p class="note"><b>让分赛</b>：两个都报了 {_min_km:g} km 以上目标的人自动配对。'
        f'目标高的多跑「对方目标的 {_hc_pct}%」，最多到自己的 {_cap:g} 倍。'
        '两人都完成 = 双人徽章；弱的完成、高的没完成 = 逆袭。</p>'
        f'<p class="note"><b>预言</b>：猜一个报了目标的队友这周完不成。'
        f'猜中 +{_hit_s:g}；他完成了 +{_face_s:g}，算他的。</p>'
        '</div></details>')

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

    # ---- 大家的动态：**并进组页，不再有独立的动态页**。
    # 用户原话：「划到小组底部后自动数据流展示所有活动，划到顶部就回到小组」。
    # 一次渲染 feed_n 条（默认 20），底部「继续往下看」再加载 20 条并锚回这里；
    # 评论在卡片里原地展开，不再跳「这条」详情页。
    feed_rows = ""
    try:
        _fd = G.feed(gid, today)
    except Exception:      # noqa: BLE001 - 老服务端没有 /feed，整块不显示
        _fd = {}
    _all = _fd.get("rows") or []
    _want = max(FEED_PAGE, min(len(_all), feed_n))
    # 发完评论回来时那条动态必须**在页面里**：它可能排在 20 条之后，
    # 光锚过去会落到一个不存在的 id 上。这里把窗口撑到它所在的位置。
    if focus:
        _fi = next((i for i, x in enumerate(_all)
                    if int(x.get("id") or 0) == int(focus)), -1)
        if _fi >= _want:
            _want = _fi + 1
    if _fd.get("ok") and _all:
        _show = _all[:_want]
        for a in _show:
            # 不能再用 <a> 包整张卡：卡片里现在有评论表单和 details，
            # <a> 里嵌 form 是非法结构，点输入框会触发跳转。
            # id="a{aid}" 让 303 回来能直接锚到这条（见 KEEP_JS）。
            feed_rows += (f'<div class="acard" id="a{int(a.get("id") or 0)}">'
                          + _act_card(a, today, gid=gid, token=token,
                                      emojis=_fd.get("emojis") or [],
                                      open_cm=(int(a.get("id") or 0) == int(focus or 0)))
                          + '</div>')
        if len(_all) > len(_show):
            _next = len(_show) + FEED_PAGE
            feed_rows += ('<div class="pager"><a class="btn sm" '
                          f'href="/groups/{gid}?feed={_next}#feed">'
                          f'继续往下看（{len(_all) - len(_show)} 条）</a></div>')
    feed_sec = (sec("大家的动态", feed_rows, anchor="feed") if feed_rows else "")

    # 完整榜单在 `/groups/{gid}/board`：周榜/月榜的切换也搬过去了——在这个
    # 200KB 的页面上重载一次只为换个口径，动态会被重刷、位置会丢。
    # 人不超过 10 时榜已经完整，这一页真正多出来的只有月口径，就直接写「月榜」；
    # 否则写「完整榜单」——两句话说的都是同一个页面，但都别撒谎。
    more = (("完整榜单", f"/groups/{gid}/board") if len(_rows) > BOARD_TOP
            else ("月榜", f"/groups/{gid}/board?range=month"))

    # 顶部一块：我的名次 + 本周状态（目标/结对/预言/Bingo），不带标题也不带卡片。
    # 顺序是刻意的——点「小组」第一眼要看到「我在组里第几」，然后才是这一周的
    # 玩法。P2 加了两块之后榜单被挤到第三屏，这条就破了，所以玩法块必须并进
    # 头部，而不是自己占一个 section。
    # 顺序：卡片 → 三个入口 → 目标 → 结对/预言 → 怎么算的。
    head = score_card + quick + week_form + week_line + week_how

    body = (msg
            + head
            + sec("本周榜", '<div class="flat">' + rank_rows + '</div>', more=more)
            + sec("赛事", chal_msg + '<div class="flat">' + ch_rows + '</div>' + chal_fold,
                  anchor="chal")
            + feed_sec
            + SAY_JS + KEEP_JS)
    return _with_copy(page(
        "GROUP", _gname(g) or "小组",
        f'{int(g.get("member_count") or 0)} 人 · '
        + {"owner": "我是组长", "admin": "我是管理员",
                             "coach": "我是教练"}.get(role, "我是成员"),
        body, chips, action=_gear,
        foot="只上传里程合计和脱敏轨迹剪影，不含坐标和位置。",
        back=("全部小组", "/groups?list=1")))


# 审计动作 → 人话。表里存的是英文动作名，直接显示 action 没人看得懂。
_AUDIT_TXT = {
    "create": "建了组", "join": "加入", "apply": "申请加入", "leave": "退出",
    "remove": "踢出", "transfer": "转让组长", "archive": "解散了组",
    "rotate": "换了邀请码", "share": "改了共享设置", "alias": "改了昵称",
    "report": "上报了数据", "challenge_create": "建了赛事",
    "challenge_close": "结束了赛事", "approve": "批准加入", "reject": "拒绝加入",
    "join_mode": "改了加入方式", "goal": "改了目标", "mapshare": "改了路线共享",
}


def audit_page(token: str, gid: int, message: str = "") -> str:
    """最近的操作记录。只给 owner/admin 看，普通成员进来会被告知没有权限。"""
    from . import group as G

    r = G.audit(gid, 50)
    if not r.get("ok"):
        body = (_msg(message)
                + f'<div class="card"><p class="note">'
                  f'{esc(r.get("error") or "打不开操作记录")}</p>'
                  f'<div class="pager"><a class="btn" href="/groups/{gid}/settings">'
                  '返回设置</a></div></div>')
        return page("GROUP", "操作记录", "", body,
                    back=("返回设置", f"/groups/{gid}/settings"))

    rows = ""
    for x in (r.get("rows") or []):
        _at = dt.datetime.fromtimestamp(int(x.get("at") or 0))
        _txt = _AUDIT_TXT.get(str(x.get("action") or ""), str(x.get("action") or ""))
        _tgt = str(x.get("target") or "")
        _line = f'<b>{esc(x.get("actor"))}</b>{esc(_txt)}'
        if _tgt:
            _line += f'<s>·</s>{esc(_tgt)}'
        rows += (f'<div class="kvline"><span>{_at.strftime("%m-%d %H:%M")}</span>'
                 f'<b>{_line}</b></div>')
    if not rows:
        rows = '<p class="note">还没有记录。</p>'

    body = (_msg(message)
            + sec("最近 50 条", '<div class="card">' + rows + '</div>')
            + '<p class="note">只记录谁做了什么，不含里程明细。</p>')
    return page("GROUP", "操作记录", f'{len(r.get("rows") or [])} 条', body,
                back=("返回设置", f"/groups/{gid}/settings"))


def board_page(token: str, gid: int, message: str = "", rng: str = "week",
               by: str = "dist") -> str:
    """完整榜单（周 / 月两个口径）。

    为什么单开一页，而不是继续把完整榜塞在组页：
    1. 组页主体是动态，榜单到 20 人就有 1100px 高，一屏看不到动态；
    2. 周/月互切原来是在组页上换 URL —— 一次 200KB 的重渲染只为换个
       聚合口径，动态重刷、滚动位置全丢。口径是「榜单内部的选择」，
       就该在榜单页里切，不牵动别的板块。

    月榜只排里程：衫（里程王/爬升王…）和探索徽章都是**按周**结算的，
    挂到月榜上会被读成「这个月的衫」，那是错的。
    """
    from . import group as G

    today = dt.date.today().isoformat()
    rng = "month" if rng == "month" else "week"
    # 综合榜只有月榜有。周榜要挂衫和探索徽章，再加一种排序会让
    # 「本周谁是第一」变得说不清。
    by = "comp" if (rng == "month" and by == "comp") else "dist"
    d = G.detail(gid)
    if not d.get("ok"):
        body = (_msg(message)
                + f'<div class="card"><p class="note">{esc(d.get("error") or "打不开榜单")}</p>'
                f'<div class="pager"><a class="btn" href="/groups/{gid}">返回小组</a></div></div>')
        return page("GROUP", "完整榜单", "", body, back=("返回小组", f"/groups/{gid}"))
    g = d["group"]
    _n = int(g.get("member_count") or 0)

    b = G.board(gid, rng, today, by)
    if not b.get("ok"):
        body = (_msg(message)
                + f'<div class="card"><p class="note">{esc(b.get("error") or "拉不到榜单")}</p>'
                f'<div class="pager"><a class="btn" href="/groups/{gid}/board">重试</a></div></div>')
        return page("GROUP", "完整榜单", "", body, back=("返回小组", f"/groups/{gid}"))

    rows = b.get("rows") or []
    _from, _to = str(b.get("from") or "")[5:], str(b.get("to") or "")[5:]
    _sum = b.get("summary") or {}
    _tot = float((_sum.get("total_comp_m") if by == "comp"
                  else _sum.get("total_distance_m")) or 0) / 1000.0
    _ride_tot = float(_sum.get("total_ride_m") or 0) / 1000.0

    # 周榜才挂衫和探索徽章。取不到 season 就整块降级（不挂图标而已）。
    _marks_of: dict = {}
    _min_cells = 5
    if rng == "week":
        try:
            _se = G.season(gid, today) or {}
        except Exception:      # noqa: BLE001
            _se = {}
        _pat_of = {str(s2.get("id") or ""): str(s2.get("pattern") or "")
                   for s2 in (_se.get("shirts") or [])}
        for _p in (_se.get("people") or []):
            _one = ""
            for _x in (_p.get("shirts") or []):
                _one += _jersey(str(_x.get("color") or ""),
                                _pat_of.get(str(_x.get("id") or ""), ""), True, "jr",
                                f'{_x.get("name") or ""}·{_x.get("title") or ""}')
            _marks_of[int(_p.get("user_id") or 0)] = _one
            # 老服务端不返回 user_id，退回按名字挂——所以这条要在循环里，
            # 每人挂自己的。原来写在循环外，只挂了最后一个人，而且
            # `people` 为空时 `_p` 未定义，整个周榜页会 500。
            _marks_of[str(_p.get("alias") or "")] = _one
        _min_cells = int(((_se.get("explore") or {}).get("min_cells")) or 5)

    if by == "comp":
        # 综合榜：主数字是综合距离，名字后面跟一句分项——
        # 只说「综合 68」而不给「跑 42 / 骑 96」，用户会以为骑行没算进去。
        def _split_note(x: dict) -> str:
            _rk = float(x.get("ride_m") or 0) / 1000.0
            if _rk <= 0:
                return ""
            return f'跑 {_km(x.get("distance_m"))} · 骑 {_rk:g}'

        rank_rows = "".join(_rank_row(i, x, _split_note(x), x.get("comp_m"))
                            for i, x in enumerate(rows, 1))
    else:
        rank_rows = "".join(_rank_row(i, x, _rank_marks(x, _marks_of, _min_cells)
                                      if rng == "week" else "")
                            for i, x in enumerate(rows, 1))
    if not rank_rows:
        rank_rows = ('<p class="note">'
                     + ("本月还没有人上报。" if rng == "month" else "本周还没有人上报。")
                     + '打开 App 才会同步，不跑后台任务。</p>')
    hidden = int(b.get("hidden") or 0)
    if hidden:
        rank_rows += f'<p class="note">另有 {hidden} 人关闭了共享，不参与排名。</p>'

    _mine = next((i for i, x in enumerate(rows, 1) if x.get("is_me")), 0)
    _my_m = next(((x.get("comp_m") if by == "comp" else x.get("distance_m"))
                  for x in rows if x.get("is_me")), 0) or 0
    _my_km = float(_my_m) / 1000.0
    _me_line = ""
    if _mine:
        _me_line = (f'<div class="kvrows"><span class="k">我</span>'
                    f'<span class="v">第 <b>{_mine}</b> 名<s>·</s>'
                    f'{_my_km:g} km<s>·</s>共 {len(rows)} 人</span></div>')
    elif rows:
        _me_line = '<p class="note">这周还没上报，榜上暂时没有你。</p>'

    # 分段控件：周 / 月。选中态用 .ok，和计划页的粒度切换同一套语义。
    tab = (f'<div class="seg">'
           f'<a href="/groups/{gid}/board"' + (' class="ok"' if rng == "week" else "")
           + '>本周</a>'
           f'<a href="/groups/{gid}/board?range=month"'
           + (' class="ok"' if rng == "month" else "") + '>本月</a></div>')
    # 月榜才有的第二层：里程（只跑步）/ 综合（骑行按 4:1 折算）。
    # 周榜不给这一层——周榜上的衫和徽章都是按跑步口径结算的，混进来会自相矛盾。
    if rng == "month":
        tab += (f'<div class="seg">'
                f'<a href="/groups/{gid}/board?range=month"'
                + (' class="ok"' if by == "dist" else "") + '>里程</a>'
                f'<a href="/groups/{gid}/board?range=month&by=comp"'
                + (' class="ok"' if by == "comp" else "") + '>综合</a></div>')

    _span = ("本周" if rng == "week" else "本月") + f" {_from} – {_to}"
    _span += "（周一–周日）" if rng == "week" else "（自然月）"
    if by == "comp":
        _span += f" · 全组综合 {_tot:g} km"
        if _ride_tot > 0:
            _span += f"（其中骑行 {_ride_tot:g} km，按 4:1 折）"
        note = ('<p class="note">综合距离 = 跑步 + 骑行 ÷ 4。'
                '这个折算只在综合榜用：衫、探索徽章和赛季积分仍按跑步算。</p>')
    elif rng == "month":
        _span += f" · 全组 {_tot:g} km"
        note = ('<p class="note">月榜只排里程。衫（里程王 / 爬升王…）和探索徽章'
                '都是按周结算的，月初到月末攒的那几件不在这一页。</p>')
    else:
        _span += f" · 全组 {_tot:g} km"
        note = '<p class="note">按里程排；里程一样时，跑的天数多的在前。</p>'

    _more = ("本周荣誉", f"/groups/{gid}/shirts") if rng == "week" else ""
    body = (_msg(message)
            + tab
            + f'<p class="note">{esc(_span)}</p>'
            + _me_line
            + sec("完整榜单", note + '<div class="flat">' + rank_rows + '</div>',
                  more=_more))
    return page("GROUP", "完整榜单",
                f'{_n} 人 · ' + ("本周" if rng == "week"
                                 else ("本月综合" if by == "comp" else "本月")), body,
                foot="只上传里程合计和脱敏轨迹剪影，不含坐标和位置。",
                back=("返回小组", f"/groups/{gid}"))
