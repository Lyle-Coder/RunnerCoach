# -*- coding: utf-8 -*-
"""网页版的小组：名次、六件衫、足迹、Bingo、赛事、动态。

为什么单独一个模块
------------------
`web.py` 里有登录和「我的小组」列表，那部分只有一百多行。组里的东西信息量
大得多（核心卡 / 榜单 / 赛事 / 三个玩法格 / 动态 / 五个二级页），塞在一起
会到一千行，改一条文案要滚很久。这里只管组，`web.py` 只管壳。

四条规矩
--------
1. **只读**。建赛事、审核、改设置、开关共享都在 App 里。网页这一侧没有
   任何写操作，也就没有 CSRF 面。
2. **二级页一定有返回**：标题上方一条 `‹ 小组`，和 App 的逐级上退对齐
   （`docs/交互树.md` §3）。
3. **榜单第一屏可见**：核心卡下面紧跟本周榜，探索格和动态排后面。
4. **不自己算排名**。全部走 `groups.api_*`，网页和 App 必须是同一份数字。
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import groups as G
import manage as M

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
try:
    # 格子几何（500 m 网格）和客户端共用一份：自己再写一遍 CELL_M，
    # 两边的「格」迟早会对不上。
    from garmin_cn import explore as EX
except Exception:      # noqa: BLE001 - 没带上 garmin_cn 时足迹页降级成统计
    EX = None

try:
    import expedition as X
except Exception:      # noqa: BLE001 - 远征没部署时其它页照样能开
    X = None

MAP_W, MAP_H = 660, 420
SHARED_COLOR = "#F4F7FC"
OWNER_SHARE = 0.65

CSS = """
.back{display:inline-flex;align-items:center;gap:4px;color:var(--dim);
  font-size:13px;text-decoration:none;margin-bottom:6px;padding:4px 0}
.back:hover{color:var(--fg)}
.mecard{background:linear-gradient(180deg,#1b2433,#161a21);border:1px solid var(--line);
  border-radius:16px;padding:16px;margin-top:12px}
.metop{display:flex;align-items:flex-end;gap:14px}
.metop .rk1{line-height:1;min-width:0}
.metop .rk1 b{font-size:42px;font-variant-numeric:tabular-nums;color:#e8b06a}
.metop .rk1 span{display:block;color:var(--dim);font-size:12px;margin-top:4px}
.metop .nums{display:flex;gap:16px;margin-left:auto;text-align:right}
.metop .nums b{display:block;font-size:19px;font-variant-numeric:tabular-nums}
.metop .nums span{display:block;color:var(--dim);font-size:12px}
.mefoot{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px;align-items:center}
.mefoot .pill.a{color:#9fd0ff;text-decoration:none}
.bar{height:6px;border-radius:99px;background:#232a35;overflow:hidden;margin-top:7px}
.bar i{display:block;height:100%;background:var(--brand);border-radius:99px}
.bar i.ok{background:var(--ok)}
.sd{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--c);
  margin-left:5px;vertical-align:middle}
.sd.dots{background:radial-gradient(circle,#fafcff 34%,#d9534f 36%)}
.g3{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:12px}
.g3 a{display:block;background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:12px;text-decoration:none;color:inherit;min-height:88px}
.g3 a:hover{border-color:#33404f}
.g3 b{display:block;font-size:15px;margin-bottom:6px}
.g3 .v{font-size:18px;font-variant-numeric:tabular-nums;color:#cfe6ff}
.g3 .s{display:block;color:var(--dim);font-size:12px;margin-top:4px;line-height:1.4}
.sec{margin-top:22px}
.sec h2{margin:0 0 8px}
.chal{padding:12px 0;border-bottom:1px solid var(--line)}
.chal:last-child{border-bottom:0;padding-bottom:0}
.chal .t{display:flex;align-items:baseline;gap:8px}
.chal .t b{flex:1 1 auto;min-width:0;overflow-wrap:anywhere}
.bg{display:grid;grid-template-columns:repeat(5,1fr);gap:6px;margin-top:12px}
.bg div{aspect-ratio:1;border-radius:10px;background:#1b2029;border:1px solid var(--line);
  display:flex;align-items:center;justify-content:center;text-align:center;
  font-size:11px;line-height:1.25;padding:3px;color:var(--dim);overflow-wrap:anywhere}
.bg div.on{background:rgba(78,161,255,.18);border-color:rgba(78,161,255,.55);
  color:#cfe6ff;font-weight:600}
.gmap{width:100%;height:auto;display:block;border-radius:12px;background:#0b1420}
.lgs{display:flex;flex-wrap:wrap;gap:10px 14px;margin-top:12px}
.lgi{display:inline-flex;align-items:center;gap:6px;font-size:13px;color:var(--dim)}
.lgi i{width:10px;height:10px;border-radius:3px;display:inline-block}
.lgi b{color:var(--fg);font-weight:600;font-variant-numeric:tabular-nums}
.sh{display:flex;align-items:flex-start;gap:10px;padding:14px 0;border-bottom:1px solid var(--line)}
.sh:last-child{border-bottom:0}
.sh .dot{width:14px;height:14px;border-radius:50%;flex:0 0 auto;margin-top:5px}
.sh .dot.dots{background:radial-gradient(circle,#fafcff 34%,#d9534f 36%)}
.sh .h{flex:1 1 auto;min-width:0}
.sh .h b{font-size:15px}
.sh .h span{display:block;color:var(--dim);font-size:12px;line-height:1.45}
.sh .win{text-align:right;font-size:13px;min-width:0}
details.rank{margin:6px 0 0}
details.rank summary{color:var(--dim);font-size:12px;cursor:pointer;list-style:none}
details.rank summary::-webkit-details-marker{display:none}
details.rank .in{padding:4px 0 0}
.fd{padding:11px 0;border-bottom:1px solid var(--line)}
.fd:last-child{border-bottom:0}
.fd .a{display:flex;gap:8px;align-items:baseline}
.fd .a b{font-size:14px}
.fd .a .v{margin-left:auto;font-variant-numeric:tabular-nums;font-size:14px}
.fd .c{color:var(--dim);font-size:13px;margin-top:4px;overflow-wrap:anywhere}
.fd .c i{color:#9fd0ff;font-style:normal}
.kv{display:flex;gap:10px;padding:7px 0;font-size:14px}
.kv .k{color:var(--dim);flex:0 0 auto}
.kv .v{flex:1 1 auto;min-width:0;text-align:right;overflow-wrap:anywhere}
"""


# ---------------------------------------------------------------- 小工具
def _W():
    import web as W

    return W


def _e(v) -> str:
    return M._e(v)


def _page(title, body, me, back=None, msg=""):
    return _W()._page(title, body, me, msg, css=CSS, back=back)


def _km(m) -> str:
    try:
        return f"{float(m or 0) / 1000:.1f}"
    except (TypeError, ValueError):
        return "0.0"


def _pace(distance_m, duration_s) -> str:
    try:
        d, t = float(distance_m or 0), float(duration_s or 0)
    except (TypeError, ValueError):
        return ""
    if d < 100 or t <= 0:
        return ""
    sec = t / (d / 1000.0)
    if sec <= 0 or sec > 3600:
        return ""
    return f"{int(sec // 60)}'{int(sec % 60):02d}\""


def _rel_day(d: str, today: str) -> str:
    try:
        dd = dt.date.fromisoformat(str(d)[:10])
        t0 = dt.date.fromisoformat(str(today)[:10])
    except ValueError:
        return str(d or "")
    n = (t0 - dd).days
    if n == 0:
        return "今天"
    if n == 1:
        return "昨天"
    if 0 < n < 7:
        return f"{n} 天前"
    return f"{dd.month}/{dd.day}"


def _pace_str(sec_per_km) -> str:
    """秒/公里 → 5'30"。赛事里的配速是目标值，和轨迹算出来的不是一回事。"""
    try:
        v = int(round(float(sec_per_km or 0)))
    except (TypeError, ValueError):
        return "—"
    return f"{v // 60}'{v % 60:02d}\"" if v > 0 else "—"


def _chal_pair(c: dict) -> str:
    """赛事的「当前 / 目标」。五种类型各说各的单位，不能一律当 km。

    服务端给的是**米**，页面上直接印出来会变成「69000/10000 km」——
    线上这种数字一眼就会被当成 bug。
    """
    kind = str(c.get("kind") or "distance")
    try:
        cur = float(c.get("current") or 0)
        tgt = float(c.get("target") or 0)
    except (TypeError, ValueError):
        return "—"
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


def _bar(pct, ok: bool = False) -> str:
    try:
        p = max(0.0, min(100.0, float(pct or 0)))
    except (TypeError, ValueError):
        p = 0.0
    cls = " class=ok" if ok else ""
    return f'<div class="bar"><i{cls} style="width:{p:.0f}%"></i></div>'


def _fail(me, gid, err, title="小组"):
    msg = str(getattr(err, "message", err) or "打不开这个小组")
    body = ('<div class="card"><p class="sub" style="margin:0">' + _e(msg) + "</p></div>"
            '<p class="sub"><a href="/w?list=1">← 我的小组</a></p>')
    return _page(title, body, me, back=("我的小组", "/w"))


# ---------------------------------------------------------------- 片段
def _rank_rows(rows, by: str = "dist", shirts=None) -> str:
    """榜单行。`shirts` 是 uid → 衫列表，用来在名字后面点几个小色点。"""
    if not rows:
        return '<p class="sub" style="margin:0">这段时间还没有人上报。</p>'
    shirts = shirts or {}
    out = []
    for i, x in enumerate(rows, 1):
        val = x.get("comp_m") if by == "comp" else x.get("distance_m")
        note = ""
        if by == "comp":
            ride = float(x.get("ride_m") or 0) / 1000.0
            if ride > 0:
                note = (f'<span class="sub">跑 {_km(x.get("distance_m"))}'
                        f' · 骑 {ride:g}</span> ')
        dots = ""
        for s in shirts.get(int(x.get("user_id") or 0)) or []:
            cls = "sd dots" if s.get("id") == "polka" else "sd"
            dots += f'<i class="{cls}" style="--c:{_e(s.get("color"))}"></i>'
        expl = int(x.get("explorer") or 0)
        badge = f'<span class="sub"> 新 {expl} 格</span>' if expl >= 8 else ""
        cls = "rk me" if x.get("is_me") else "rk"
        out.append(f'<div class="{cls}"><span class=no>{i}</span>'
                   f'<span class="nm">{_e(x.get("alias"))}{dots} {note}{badge}</span>'
                   f'<span class="km">{_km(val)} km</span></div>')
    return "".join(out)


def _goal_line(week: dict, km: float) -> str:
    goal = week.get("goal") or {}
    g = goal.get("km")
    if not g:
        return ""
    try:
        g = float(g)
        pct = (km / g * 100.0) if g > 0 else 0.0
    except (TypeError, ValueError):
        return ""
    return (f'<span class="pill">目标 {g:g} km · {pct:.0f}%</span>')


def _challenge_rows(chs, gid: int) -> str:
    if not chs:
        return ('<p class="sub" style="margin:0">这周没有赛事。建赛事在 App 的小组页里，'
                "网点一下就行。</p>")
    out = ""
    for c in chs:
        pct = float(c.get("pct") or 0)
        out += (f'<a class="chal" style="display:block;text-decoration:none;color:inherit"'
                f' href="/w/g/{gid}/challenge/{int(c["id"])}">'
                f'<div class="t"><b>{_e(c.get("title"))}</b>'
                f'<span class="sub">{_e(_chal_pair(c))} · {pct:.0f}%</span></div>'
                + _bar(pct, pct >= 100)
                + f'<div class="sub" style="margin-top:6px">到 {_e(c.get("to"))} 结束</div>'
                "</a>")
    return out


# ---------------------------------------------------------------- 组主页
def group(me, gid: int, msg: str = "", qs: dict | None = None):
    today = dt.date.today().isoformat()
    q = {"today": [today]}
    try:
        home = G.api_home(me, gid, q)
    except Exception as e:      # noqa: BLE001
        return _fail(me, gid, e)

    detail = home.get("detail") or {}
    if not detail.get("ok"):
        return _fail(me, gid, detail.get("error") or "看不到这个小组")
    g = detail.get("group") or {}
    board = home.get("board") or {}
    rows = board.get("rows") or []
    season = home.get("season") or {}
    week = home.get("week") or {}
    people = season.get("people") or []
    mine = season.get("me") or {}

    # 我的名次：优先用赛季口径（含称号和衫），关掉共享的人退回到榜单行
    rank = int(mine.get("rank") or 0)
    km = float(mine.get("km") or 0)
    score = mine.get("score")
    title_txt = mine.get("title") or ""
    my_shirts = mine.get("shirts") or []
    if not rank:
        row = next((x for x in rows if x.get("is_me")), None)
        if row:
            rank = next((i for i, x in enumerate(rows, 1) if x.get("is_me")), 0)
            km = float(row.get("distance_m") or 0) / 1000.0

    total_people = len(people) or int((board.get("summary") or {}).get("shown") or 0)
    total_km = float((board.get("summary") or {}).get("total_distance_m") or 0) / 1000.0

    if rank:
        core = (f'<div class="mecard"><div class="metop">'
                f'<div class="rk1"><b>{rank}</b>'
                f'<span>本周第 {rank} 名 · 共 {total_people} 人</span></div>'
                f'<div class="nums"><div><b>{km:.1f}</b><span>公里</span></div>'
                + (f'<div><b>{float(score):.0f}</b><span>综合分</span></div>'
                   if score is not None else "")
                + "</div></div>")
        foot = ""
        if title_txt:
            foot += f'<span class="pill">{_e(title_txt)}</span>'
        foot += (f'<a class="pill a" href="/w/g/{gid}/shirts">荣誉 '
                 f'{len(my_shirts)}/6 ›</a>')
        foot += _goal_line(week, km)
        if foot:
            core += f'<div class="mefoot">{foot}</div>'
        core += "</div>"
    else:
        core = ('<div class="mecard"><p class="sub" style="margin:0">'
                "本周还没有你的记录。共享打开、App 同步过之后，这里就有名次和分数了。"
                "</p></div>")

    # 衫的归属：赛季口径里的人 → 他们穿的衫
    shirts_of = {int(p.get("user_id") or 0): (p.get("shirts") or []) for p in people}
    # 榜单行是本周里程，衫按同一周结算，这里直接用榜单顺序
    _rows = rows[:10]
    board_html = ('<div class="sec"><h2>本周榜</h2><div class="card">'
                  + _rank_rows(_rows, "dist", shirts_of)
                  + "</div>")
    more = ""
    if len(rows) > 10:
        more = f'<p class="sub"><a href="/w/g/{gid}/board">完整榜单 · {len(rows)} 人 ›</a></p>'
    else:
        more = (f'<p class="sub"><a href="/w/g/{gid}/board">换个周期看 ›</a></p>')
    board_html += more
    if total_km > 0:
        board_html += f'<p class="sub">全组本周 {total_km:.0f} km</p>'
    board_html += "</div>"

    # 赛事
    chs = ((home.get("challenges") or {}).get("challenges")) or []
    live = [c for c in chs if c.get("status") == "running"][:3]
    chal_html = ('<div class="sec"><h2>赛事</h2><div class="card">'
                 + _challenge_rows(live, gid) + "</div></div>")

    # 探索三格
    try:
        mp = G.api_map(me, gid, q) or {}
    except Exception:      # noqa: BLE001 - 地图拉不到不该让整页打不开
        mp = {}
    mp_people = mp.get("people") or []
    mp_cells = int(mp.get("cells") or 0)
    bingo = week.get("bingo") or {}
    b_done = int(bingo.get("done") or 0)
    b_total = int(bingo.get("total") or 25)
    b_lines = int(bingo.get("lines") or 0)

    exp_tile = ""
    if X is not None:
        try:
            st = X.api_state(me, gid, q) or {}
            mine_x = (st or {}).get("mine") or {}
            sea = (st or {}).get("season") or {}
            act = sea.get("act_no") or sea.get("act") or ""
            pos = mine_x.get("pos")
            bits = []
            if act:
                bits.append(f"第 {act} 幕")
            if pos is not None:
                bits.append(f"位置 {pos}")
            exp_tile = (f'<a href="/w/g/{gid}/exp"><b>远征</b>'
                        f'<span class="v">{_e(" · ".join(str(x) for x in bits) or "行进中")}</span>'
                        '<span class="s">全组一起往终点走 ›</span></a>')
        except Exception:      # noqa: BLE001
            exp_tile = ""
    if not exp_tile:
        exp_tile = ('<a href="/w/g/{gid}"><b>远征</b><span class="v">—</span>'
                    '<span class="s">在 App 里看</span></a>').replace("{gid}", str(gid))

    explore = ('<div class="sec"><h2>探索</h2><div class="g3">'
               f'<a href="/w/g/{gid}/bingo"><b>Bingo</b>'
               f'<span class="v">{b_done}/{b_total}</span>'
               f'<span class="s">{b_lines} 条线</span></a>'
               f'<a href="/w/g/{gid}/map"><b>足迹</b>'
               f'<span class="v">{len(mp_people)} 人</span>'
               f'<span class="s">{mp_cells} 个格子</span></a>'
               + exp_tile + "</div></div>")

    # 动态（只读）
    try:
        fd = G.api_feed(me, gid, q) or {}
    except Exception:      # noqa: BLE001
        fd = {}
    frows = (fd.get("rows") or [])[:4]
    feed = ""
    if frows:
        items = ""
        for a in frows:
            pace = _pace(a.get("distance_m"), a.get("duration_s"))
            items += (f'<div class="fd"><div class="a"><b>{_e(a.get("alias"))}</b>'
                      f'<span class="sub">{_e(a.get("kind_name"))} · '
                      f'{_rel_day(a.get("date"), today)}</span>'
                      f'<span class="v">{_km(a.get("distance_m"))} km'
                      + (f' · {pace}' if pace else "") + "</span></div>")
            cm = a.get("recent") or []
            if cm:
                items += '<div class="c">' + "　".join(
                    f'<i>{_e(c.get("alias"))}</i> {_e(c.get("text"))}' for c in cm[-2:]
                ) + "</div>"
            items += "</div>"
        feed = ('<div class="sec"><h2>大家的动态</h2><div class="card">' + items + "</div>"
                f'<p class="sub"><a href="/w/g/{gid}/feed">看全部动态 ›</a></p></div>')

    body = (_W()._msg_box(msg) if msg else "") + core + board_html + chal_html + explore + feed
    body += ('<p class="sub" style="margin-top:18px">榜上的数字来自每天的总里程、时长和爬升，'
             "没有轨迹、配速和心率。关掉共享的人不出现在榜上。"
             "建赛事、改设置、开关共享在 App 的小组页里。</p>")
    return _page(str(g.get("name") or "小组"), body, me, back=("我的小组", "/w"))


# ---------------------------------------------------------------- 六件衫
def shirts(me, gid: int, msg: str = ""):
    today = dt.date.today().isoformat()
    try:
        st = G.api_season(me, gid, {"today": [today]})
    except Exception as e:      # noqa: BLE001
        return _fail(me, gid, e, "荣誉衫")
    if not st.get("ok"):
        return _fail(me, gid, st.get("error") or "看不到", "荣誉衫")

    mine = st.get("me") or {}
    my_ids = {s.get("id") for s in (mine.get("shirts") or [])}
    shirts = st.get("shirts") or []
    explore = st.get("explore") or {}
    weights = st.get("weights") or {}
    titles = st.get("titles") or []

    name_of = {s.get("id"): s.get("name") for s in shirts}
    mine_pills = "".join(f'<span class="pill">{_e(name_of.get(i) or i)}</span>'
                         for i in ("yellow", "green", "polka", "white", "red", "blue")
                         if i in my_ids)
    head = ('<div class="mecard"><div class="metop">'
            f'<div class="rk1"><b>{len(my_ids)}</b><span>本周我的衫 · 共 6 件</span></div>'
            f'<div class="nums"><div><b>{int(mine.get("rank") or 0) or "—"}</b>'
            '<span>本周名次</span></div>'
            f'<div><b>{float(mine.get("score") or 0):.0f}</b><span>综合分</span></div>'
            "</div></div>"
            + (f'<div class="mefoot">{mine_pills}</div>' if mine_pills
               else '<p class="sub" style="margin:12px 0 0">这周还没穿上任何一件，'
                    "下面每一件都写着在比什么。</p>")
            + "</div>")

    # 六件衫：每件一行（得主 + 前 5 + 全量折叠）
    rows_html = ""
    for s in shirts:
        winners = s.get("winners") or []
        rank = s.get("rank") or []
        top = rank[:5]
        win = "、".join(_e(w.get("alias")) for w in winners) or "本周空缺"
        lst = "".join(
            f'<div class="kv"><span class="k">{i}</span>'
            f'<span class="v">{_e(x.get("alias"))} · {_e(x.get("text"))}</span></div>'
            for i, x in enumerate(top, 1))
        rest = rank[5:]
        more = ""
        if rest:
            more = ('<details class="rank"><summary>看完整榜（' + str(len(rank)) + ' 人）</summary>'
                    '<div class="in">'
                    + "".join(
                        f'<div class="kv"><span class="k">{i}</span>'
                        f'<span class="v">{_e(x.get("alias"))} · {_e(x.get("text"))}</span></div>'
                        for i, x in enumerate(rest, 6))
                    + "</div></details>")
        rows_html += (f'<div class="sh"><span class="dot'
                      + (" dots" if s.get("id") == "polka" else "")
                      + f'" style="background:{_e(s.get("color"))}"></span>'
                      f'<div class="h"><b>{_e(s.get("name"))} · {_e(s.get("title"))}</b>'
                      f'<span>{_e(s.get("hint"))}</span>'
                      f'<span>得主：{win}</span>{lst}{more}</div></div>')

    badges = explore.get("badges") or []
    min_cells = int(explore.get("min_cells") or 0)
    badge_html = ('<div class="card">'
                  + ("".join(
                      f'<div class="kv"><span class="k">{_e(b.get("alias"))}</span>'
                      f'<span class="v">新 {int(b.get("cells") or 0)} 格</span></div>'
                      for b in badges) or
                     '<p class="sub" style="margin:0">本周还没有人拿到探索徽章。</p>')
                  + f'<p class="sub" style="margin:8px 0 0">一周里新跑过 '
                  f"{min_cells} 个以上的 500 米格子就有。这个数只在本机算，"
                  "服务端只收到一个整数。</p></div>")

    pts = []
    if weights:
        pts.append(f'里程 {weights.get("km")} 分')
        pts.append(f'出勤 {weights.get("days")} 分')
        pts.append(f'爬升 {weights.get("elev")} 分')
        pts.append(f'进步 {weights.get("progress")} 分')
        pts.append(f'连续 {weights.get("streak")} 分')
        if weights.get("bonus"):
            pts.append(f'让分/预言加分上限 {weights.get("bonus")} 分')
    w_html = ('<div class="card"><p class="sub" style="margin:0">满分 100：'
              + "、".join(_e(x) for x in pts)
              + "。综合分只用来排名，六件衫按各自那一项单独比。</p></div>")

    t_html = ""
    if titles:
        t_html = ('<div class="card">'
                  + "".join(
                      f'<div class="kv"><span class="k">{_e(t.get("name"))}</span>'
                      f'<span class="v">{_e(t.get("hint"))}</span></div>'
                      for t in titles if isinstance(t, dict))
                  + "</div>")

    body = (head
            + '<div class="sec"><h2>六件衫</h2><div class="card">' + rows_html + "</div>"
            + '<p class="sub">一周一结算，周一归零；里程一样时，跑的天数多的在前。'
              "骑行不算。</p></div>"
            + '<div class="sec"><h2>探索徽章</h2>' + badge_html + "</div>"
            + '<div class="sec"><h2>综合分</h2>' + w_html + "</div>"
            + ('<div class="sec"><h2>称号</h2>' + t_html + "</div>" if t_html else ""))
    return _page("六件衫", body, me, back=("小组", f"/w/g/{gid}"))


# ---------------------------------------------------------------- Bingo
def bingo(me, gid: int, msg: str = ""):
    today = dt.date.today().isoformat()
    try:
        card = G.api_bingo(me, gid, {"today": [today]})
    except Exception as e:      # noqa: BLE001
        return _fail(me, gid, e, "Bingo")
    if not card.get("ok"):
        return _fail(me, gid, card.get("error") or "打不开", "Bingo")

    cells = card.get("cells") or []
    done = int(card.get("done") or 0)
    total = int(card.get("total") or len(cells) or 25)
    lines = int(card.get("line_count") or 0)

    grid = "".join(
        f'<div class="{"on" if c.get("done") else ""}">{_e(c.get("text"))}</div>'
        for c in cells)

    body = ('<div class="mecard"><div class="metop">'
            f'<div class="rk1"><b>{done}</b><span>已点亮 · 共 {total} 格</span></div>'
            f'<div class="nums"><div><b>{lines}</b><span>连线</span></div></div>'
            "</div></div>"
            '<div class="card"><div class="bg">' + grid + "</div></div>"
            '<p class="sub">一行、一列或一条对角线全亮算一条线。中心格开卡即得。'
            "卡由这一周的数据判定，周一到周日，下周换一张。"
            "Bingo 只发荣誉，不进综合分。</p>")
    return _page("Bingo", body, me, back=("小组", f"/w/g/{gid}"))


# ---------------------------------------------------------------- 足迹
def _cells_meters(people) -> dict:
    """每格 → (代表者 uid, 占比, 该格总里程)。和客户端 _cell_owners 同一套口径。"""
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
    out = {}
    for c, d in per.items():
        tot = sum(d.values())
        best_u, best_m = max(d.items(), key=lambda x: x[1])
        share = (best_m / tot) if tot > 0 else (1.0 / len(d))
        out[c] = (best_u, share, tot)
    return out


def _op_of(meters: float) -> float:
    if meters <= 0:
        return 0.52
    if meters < 800:
        return 0.30
    if meters < 2000:
        return 0.52
    return 0.78


def _map_svg(people) -> str:
    """格子图。没有底图，按经纬度等比铺满画幅。

    网页版不铺高德底图：底图 Key 是每个用户在本机配的（`data/amap.json`），
    服务端没有。没底图时形状仍然是对的，所以照画，只在页脚说清这一点。
    """
    if EX is None:
        return ""
    owners = _cells_meters(people)
    if not owners:
        return ""
    color_of = {p.get("user_id"): p.get("color") for p in people}
    mine_uid = next((p.get("user_id") for p in people if p.get("is_me")), None)

    corners = {c: EX.cell_corners(c) for c in owners}
    pts = [p for cs in corners.values() for p in cs]
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    la0, la1 = min(lats), max(lats)
    lo0, lo1 = min(lons), max(lons)
    mid = (la0 + la1) / 2.0
    import math

    kx = max(0.05, math.cos(math.radians(mid)))
    span_x = max((lo1 - lo0) * kx, 1e-6)
    span_y = max(la1 - la0, 1e-6)
    s = min((MAP_W - 28) / span_x, (MAP_H - 28) / span_y)
    ox = (MAP_W - span_x * s) / 2.0
    oy = (MAP_H - span_y * s) / 2.0

    def xy(lat, lon):
        return (ox + (lon - lo0) * kx * s, oy + (la1 - lat) * s)

    buckets: dict[tuple, list[str]] = {}
    for c, (u, share, tot) in owners.items():
        key = (u if share >= OWNER_SHARE else None, round(_op_of(tot), 2))
        buckets.setdefault(key, []).append(c)

    order = sorted(buckets.items(),
                   key=lambda kv: (kv[0][0] is None, 0 if kv[0][0] == mine_uid else 1))
    # 格子的最小可见尺寸：1 格跑 5 km 也才 500 m 见方，取景拉到城市级时
    # 只有一两像素，加描边才看得出「这是一条带」。
    paths = ""
    for (u, op), cs in order:
        col = SHARED_COLOR if u is None else str(color_of.get(u) or SHARED_COLOR)
        d = ""
        for c in cs:
            quad = [xy(lat, lon) for lat, lon in corners[c]]
            d += "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in quad) + " Z "
        paths += (f'<path d="{d}" fill="{_e(col)}" fill-opacity="{op:g}"'
                  f' stroke="{_e(col)}" stroke-opacity=".95" stroke-width=".5"'
                  ' stroke-linejoin="round"/>')

    return (f'<svg class="gmap" viewBox="0 0 {MAP_W} {MAP_H}" '
            'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="组内足迹格子">'
            f'<defs><clipPath id="gm"><rect x="0" y="0" width="{MAP_W}" '
            f'height="{MAP_H}" rx="12"/></clipPath></defs>'
            '<rect x="0" y="0" width="100%" height="100%" fill="#0b1420"/>'
            f'<g clip-path="url(#gm)">{paths}</g></svg>')


def map_page(me, gid: int, msg: str = ""):
    today = dt.date.today().isoformat()
    try:
        mp = G.api_map(me, gid, {"today": [today]})
    except Exception as e:      # noqa: BLE001
        return _fail(me, gid, e, "足迹")
    if not mp.get("ok"):
        return _fail(me, gid, mp.get("error") or "打不开", "足迹")

    people = mp.get("people") or []
    st = mp.get("me") or {}
    on = bool(st.get("map_share"))
    shared = sum(1 for _, sh, _ in _cells_meters(people).values() if sh < OWNER_SHARE)

    legend = ""
    for p in people:
        mm = p.get("meters") or {}
        try:
            km = sum(float(v or 0) for v in mm.values()) / 1000.0
        except (TypeError, ValueError):
            km = 0.0
        val = f"{km:.1f}km" if km > 0.05 else f'{len(p.get("cells") or [])} 格'
        legend += (f'<span class="lgi"><i style="background:{_e(p.get("color"))}"></i>'
                   f'{_e(p.get("alias"))}<b>{val}</b></span>')
    if shared:
        legend += f'<span class="lgi"><i style="background:{SHARED_COLOR}"></i>共同<b>{shared}</b></span>'

    svg = _map_svg(people)
    if svg:
        card = f'<div class="card">{svg}<div class="lgs">{legend}</div></div>'
    else:
        card = ('<div class="card"><p class="sub" style="margin:0">'
                "这周还没有人共享足迹。在 App 的足迹页打开开关，本周跑过的 500 米"
                "格子就会出现在这里。</p></div>")

    state = (f'<div class="kv"><span class="k">我的共享</span>'
             f'<span class="v">{"已开启" if on else "已关闭"} · 本周 '
             f'{int(st.get("cells") or 0)} 格</span></div>')
    body = (f'<div class="card">{state}'
            '<p class="sub" style="margin:8px 0 0">开关在 App 的足迹页；网页这一侧只能看。'
            "</p></div>" + card
            + '<p class="sub">每格的颜色归跑得最多的那个人；两个人跑量接近的格子显示为白色。'
              "地图上只有 500 米见方的格子和格内里程，没有轨迹线、没有配速、没有心率。"
              "网页版不铺地图底图，格子形状和相对位置是准的。"
              "关掉共享的那一刻，服务端会把这些格子删掉。</p>")
    return _page("足迹", body, me, back=("小组", f"/w/g/{gid}"))


# ---------------------------------------------------------------- 赛事
def challenge(me, gid: int, cid: int, msg: str = ""):
    today = dt.date.today().isoformat()
    try:
        r = G.api_challenge_get(me, gid, cid, {"today": [today]})
    except Exception as e:      # noqa: BLE001
        return _fail(me, gid, e, "赛事")
    if not r.get("ok"):
        return _fail(me, gid, r.get("error") or "打不开", "赛事")
    c = r.get("challenge") or {}
    rows = r.get("rows") or []
    pct = float(c.get("pct") or 0)
    status = {"running": "进行中", "ended": "已结束", "closed": "已结束"}.get(
        str(c.get("status")), "")

    body = ('<div class="mecard"><div class="metop">'
            f'<div class="rk1"><b>{pct:.0f}%</b>'
            f'<span>全组 {_e(_chal_pair(c))}</span></div>'
            f'<div class="nums"><div><b style="font-size:15px">{_e(status)}</b>'
            f'<span>{_e(c.get("from"))} – {_e(c.get("to"))}</span></div></div>'
            "</div>" + _bar(pct, pct >= 100) + "</div>")

    if rows:
        lst = ""
        for x in rows:
            p = float(x.get("pct") or 0)
            tag = f'<span class="sub">· {_e(x.get("done_at"))} 达成</span>' if x.get("done_at") else ""
            cur = _chal_pair({"kind": c.get("kind"), "current": x.get("current"),
                              "target": c.get("target")})
            lst += (f'<div class="chal"><div class="t">'
                    f'<b>{_e(x.get("alias"))} {tag}</b>'
                    f'<span class="sub">{_e(cur)} · {p:.0f}%</span></div>'
                    + _bar(p, p >= 100) + "</div>")
        card = f'<div class="card">{lst}</div>'
    else:
        card = ('<div class="card"><p class="sub" style="margin:0">'
                "还没有人上报，等这周的数据同步过来。</p></div>")

    body += card + '<p class="sub">进度按每天的总里程、时长、爬升或新踩格数累加，'
    body += "关掉共享的人不计入全组进度。</p>"
    return _page(str(c.get("title") or "赛事"), body, me, back=("小组", f"/w/g/{gid}"))


# ---------------------------------------------------------------- 动态
def feed(me, gid: int, msg: str = ""):
    today = dt.date.today().isoformat()
    try:
        r = G.api_feed(me, gid, {"today": [today]})
    except Exception as e:      # noqa: BLE001
        return _fail(me, gid, e, "动态")
    if not r.get("ok"):
        return _fail(me, gid, r.get("error") or "打不开", "动态")

    rows = (r.get("rows") or [])[:20]
    if not rows:
        body = ('<div class="card"><p class="sub" style="margin:0">'
                "组里还没有动态。App 同步之后，每天跑完就会有一条。</p></div>")
        return _page("大家的动态", body, me, back=("小组", f"/w/g/{gid}"))

    items = ""
    for a in rows:
        pace = _pace(a.get("distance_m"), a.get("duration_s"))
        bits = [f'{_km(a.get("distance_m"))} km']
        if pace:
            bits.append(pace)
        if int(a.get("elevation_m") or 0) >= 20:
            bits.append(f'爬升 {int(a.get("elevation_m"))} m')
        items += (f'<div class="fd"><div class="a"><b>{_e(a.get("alias"))}</b>'
                  f'<span class="sub">{_e(a.get("kind_name"))} · '
                  f'{_rel_day(a.get("date"), today)}</span>'
                  f'<span class="v">{" · ".join(bits)}</span></div>')
        cm = a.get("recent") or []
        if cm:
            items += '<div class="c">' + "　".join(
                f'<i>{_e(c.get("alias"))}</i> {_e(c.get("text"))}' for c in cm) + "</div>"
        if int(a.get("comments") or 0) > len(cm):
            items += f'<div class="c">还有 {int(a.get("comments")) - len(cm)} 条评论</div>'
        items += "</div>"

    body = ('<div class="card">' + items + "</div>"
            '<p class="sub">最近 20 条。评论和表情在 App 里发，网页这一侧只能看。</p>')
    return _page("大家的动态", body, me, back=("小组", f"/w/g/{gid}"))


# ---------------------------------------------------------------- 完整榜单
def board(me, gid: int, query: dict):
    today = dt.date.today().isoformat()
    rng = "month" if (query.get("range") or [""])[0] == "month" else "week"
    by = "comp" if (rng == "month" and (query.get("by") or [""])[0] == "comp") else "dist"
    try:
        b = G.api_board(me, gid, {"range": [rng], "by": [by], "today": [today]})
    except Exception as e:      # noqa: BLE001
        return _fail(me, gid, e, "完整榜单")
    if not b.get("ok"):
        return _fail(me, gid, b.get("error") or "打不开", "完整榜单")

    rows = b.get("rows") or []
    sm = b.get("summary") or {}
    tot = float((sm.get("total_comp_m") if by == "comp"
                 else sm.get("total_distance_m")) or 0) / 1000.0
    span = ("本周" if rng == "week" else "本月") + " " + \
           f'{str(b.get("from") or "")[5:]} – {str(b.get("to") or "")[5:]}'

    if by == "comp":
        note = ("综合距离 = 跑步 + 骑行 ÷ 4。这个折算只在综合榜用："
                "衫、徽章、赛季积分仍按跑步算。")
    elif rng == "month":
        note = "月榜只排里程。衫和探索徽章按周结算，不在这一页。"
    else:
        note = "按里程排；里程一样时，跑的天数多的在前。"

    # 衫的点只对周榜有意义：月榜不结算衫
    shirts_of = {}
    if rng == "week":
        try:
            st = G.api_season(me, gid, {"today": [today]})
            shirts_of = {int(p.get("user_id") or 0): (p.get("shirts") or [])
                         for p in (st.get("people") or [])}
        except Exception:      # noqa: BLE001
            shirts_of = {}

    nav = (f'<div class="seg">'
           f'<a href="/w/g/{gid}/board"' + (" class=ok" if rng == "week" else "") + ">本周</a>"
           f'<a href="/w/g/{gid}/board?range=month"'
           + (" class=ok" if rng == "month" and by == "dist" else "") + ">本月</a>"
           + (f'<a href="/w/g/{gid}/board?range=month&by=comp"'
              + (" class=ok" if by == "comp" else "") + ">综合</a>"
              if rng == "month" else "")
           + "</div>")

    mine = next((i for i, x in enumerate(rows, 1) if x.get("is_me")), 0)
    me_line = (f'<p class="sub">我第 <b>{mine}</b> 名 · {len(rows)} 人 · '
               f"全组 {tot:g} km</p>" if mine else "")

    body = (nav + f'<p class="sub">{_e(span)}</p>' + me_line
            + '<div class="card">' + _rank_rows(rows, by, shirts_of) + "</div>"
            + f'<p class="sub">{_e(note)}</p>')
    return _page("完整榜单", body, me, back=("小组", f"/w/g/{gid}"))


# ---------------------------------------------------------------- 路由
def handle(me, gid: int, rest: list[str], query: dict):
    """`/w/g/{gid}/{...}` 之后的路径。返回 (code, body, ctype, headers)。"""
    html = "text/html; charset=utf-8"
    if not rest:
        return (200, group(me, gid, "", query), html, {})
    head = rest[0]
    if head == "board" and len(rest) == 1:
        return (200, board(me, gid, query), html, {})
    if head == "shirts" and len(rest) == 1:
        return (200, shirts(me, gid), html, {})
    if head == "bingo" and len(rest) == 1:
        return (200, bingo(me, gid), html, {})
    if head == "map" and len(rest) == 1:
        return (200, map_page(me, gid), html, {})
    if head == "feed" and len(rest) == 1:
        return (200, feed(me, gid), html, {})
    if head == "challenge" and len(rest) == 2 and rest[1].isdigit():
        return (200, challenge(me, gid, int(rest[1])), html, {})
    if head == "exp" and len(rest) == 1:
        return (200, _expedition(me, gid), html, {})
    return (404, ("<h3>404</h3><p><a href='/w/g/%d'>回到小组</a></p>" % gid).encode(),
            html, {})


def _expedition(me, gid: int):
    """远征在网页上只有摘要：全图、补给、事件都要交互，App 里做。"""
    today = dt.date.today().isoformat()
    body = ""
    if X is not None:
        try:
            st = X.api_state(me, gid, {"today": [today]}) or {}
        except Exception as e:      # noqa: BLE001
            return _fail(me, gid, e, "远征")
        sea = st.get("season") or {}
        mine = st.get("mine") or {}
        if st.get("ok"):
            kv = []
            act = sea.get("act_no") or sea.get("act")
            if act:
                kv.append(("当前幕", f"第 {act} 幕"))
            for k, label in (("pos", "我的位置"), ("supply", "补给")):
                if mine.get(k) is not None:
                    kv.append((label, str(mine.get(k))))
            rows = "".join(f'<div class="kv"><span class="k">{_e(k)}</span>'
                           f'<span class="v">{_e(v)}</span></div>' for k, v in kv)
            body = f'<div class="card">{rows}</div>' if rows else ""
        else:
            body = ('<div class="card"><p class="sub" style="margin:0">'
                    + _e(st.get("error") or "远征还没开始") + "</p></div>")
    else:
        body = ('<div class="card"><p class="sub" style="margin:0">'
                "服务端没装远征模块。</p></div>")
    body += ('<p class="sub">远征要在地图上走、选补给、看事件，这些都在 App 的小组页里。'
             "网页这一侧只能看当前进度。</p>")
    return _page("远征", body, me, back=("小组", f"/w/g/{gid}"))
