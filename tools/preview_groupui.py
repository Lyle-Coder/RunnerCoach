"""把组页渲染成本地 HTML，方便截图看版式（一次性工具，不进产品路径）。

用法：
    python tools/preview_groupui.py

它把 garmin_cn.group 里会联网的三个函数换成假数据，其余走真实渲染。
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = Path("E:/Run/preview/groupui")
OUT.mkdir(parents=True, exist_ok=True)

TODAY = dt.date.today().isoformat()
GID = 1

DETAIL = {
    "ok": True,
    "group": {
        "id": GID, "name": "周末拉练队", "intro": "不卷配速，只卷到场",
        "owner_id": 1, "max_members": 20, "member_count": 4,
        "created_at": 0, "role": "owner", "invite_code": "5TCF2PPB",
        "me": {"alias": "老李", "share": "summary"},
        "members": [
            {"user_id": 1, "alias": "老李", "role": "owner", "share": "summary", "is_me": True},
            {"user_id": 2, "alias": "阿狗", "role": "member", "share": "summary", "is_me": False},
            {"user_id": 3, "alias": "小赵", "role": "member", "share": "summary", "is_me": False},
            {"user_id": 4, "alias": "周末才跑的那个人", "role": "member", "share": "off", "is_me": False},
        ],
    },
}

BOARD = {
    "ok": True, "range": "week", "from": "2026-11-16", "to": "2026-11-22",
    "rows": [
        {"alias": "阿狗", "is_me": False, "distance_m": 42300, "duration_s": 14000,
         "acts": 5, "days": 5, "plan_rate": 1.0, "explorer": 23, "updated_at": 0},
        {"alias": "老李", "is_me": True, "distance_m": 38100, "duration_s": 12600,
         "acts": 4, "days": 4, "plan_rate": 0.75, "explorer": 7, "updated_at": 0},
        {"alias": "小赵", "is_me": False, "distance_m": 12800, "duration_s": 4200,
         "acts": 2, "days": 2, "plan_rate": 0.5, "explorer": 0, "updated_at": 0},
    ],
    "hidden": 1,
    "summary": {"members": 4, "shown": 3, "hidden": 1, "total_distance_m": 93200},
}

CHALLENGES = {
    "ok": True,
    "challenges": [
        {"id": 1, "title": "本周全组 100 公里", "kind": "distance", "target": 100000,
         "current": 93200, "pct": 93.2, "status": "running",
         "from": "2026-11-16", "to": "2026-11-22"},
        {"id": 2, "title": "累计 20 次出门", "kind": "acts", "target": 20,
         "current": 11, "pct": 55.0, "status": "running",
         "from": "2026-11-01", "to": "2026-11-30"},
        {"id": 3, "title": "爬升 3000 米", "kind": "elevation", "target": 3000,
         "current": 1860, "pct": 62.0, "status": "running",
         "from": "2026-11-16", "to": "2026-11-22"},
        {"id": 4, "title": "配速 5'30 以内", "kind": "speed", "target": 330,
         "current": 348, "pct": 94.8, "status": "running",
         "from": "2026-11-16", "to": "2026-11-22"},
        {"id": 5, "title": "探索 60 格新地方", "kind": "explore", "target": 60,
         "current": 38, "pct": 63.3, "status": "ended",
         "from": "2026-11-01", "to": "2026-11-15"},
    ],
}


S = {"yellow": ("黄衫", "里程王", "#E8C43A"), "green": ("绿衫", "出勤王", "#3FA76A"),
     "polka": ("圆点衫", "爬升王", "#D8DEE9"), "white": ("白衫", "进步王", "#F2F5FA"),
     "red": ("红衫", "计划完成王", "#D9534F"), "blue": ("蓝衫", "连续王", "#4C8DF6")}


def _shirt(k):
    n, t, c = S[k]
    return {"id": k, "name": n, "title": t, "color": c}


SEASON = {
    "ok": True, "from": "2026-11-16", "to": "2026-11-22", "base_max": 90.0,
    "hidden": 1,
    "people": [
        {"user_id": 2, "alias": "阿狗", "rank": 1, "score": 81.2,
         "km": 42.3, "days": 5, "elev": 620, "streak": 5, "plan_rate": 1.0,
         "progress": 0.12, "title": "远征队长",
         "shirts": [_shirt("yellow"), _shirt("green"), _shirt("red")]},
        {"user_id": 1, "alias": "老李", "rank": 2, "score": 63.8,
         "km": 38.1, "days": 4, "elev": 410, "streak": 3, "plan_rate": 0.75,
         "progress": 0.05, "title": "先锋",
         "shirts": [_shirt("blue")]},
        {"user_id": 3, "alias": "小赵", "rank": 3, "score": 28.4,
         "km": 12.8, "days": 2, "elev": 90, "streak": 2, "plan_rate": 0.5,
         "progress": -0.2, "title": "",
         "shirts": []},
    ],
    "shirts": [
        {"id": "yellow", "name": "黄衫", "title": "里程王", "color": "#E8C43A",
         "field": "km", "unit": "km", "hint": "本周跑步里程",
         "winners": [{"user_id": 2, "alias": "阿狗"}],
         "top": [{"user_id": 2, "alias": "阿狗", "text": "42.3 km"},
                 {"user_id": 1, "alias": "老李", "text": "38.1 km"},
                 {"user_id": 3, "alias": "小赵", "text": "12.8 km"}]},
        {"id": "green", "name": "绿衫", "title": "出勤王", "color": "#3FA76A",
         "field": "days", "unit": "天", "hint": "本周有运动的天数",
         "winners": [{"user_id": 2, "alias": "阿狗"}],
         "top": [{"user_id": 2, "alias": "阿狗", "text": "5 天"},
                 {"user_id": 1, "alias": "老李", "text": "4 天"},
                 {"user_id": 3, "alias": "小赵", "text": "2 天"}]},
        {"id": "polka", "name": "圆点衫", "title": "爬升王", "color": "#D8DEE9",
         "field": "elev", "unit": "m", "hint": "本周累计爬升", "pattern": "dots",
         "winners": [{"user_id": 3, "alias": "小赵"}],
         "top": [{"user_id": 3, "alias": "小赵", "text": "620 m"},
                 {"user_id": 2, "alias": "阿狗", "text": "410 m"}]},
        {"id": "white", "name": "白衫", "title": "进步王", "color": "#F2F5FA",
         "field": "progress", "unit": "%", "hint": "比上周多跑了多少",
         "winners": [{"user_id": 2, "alias": "阿狗"}],
         "top": [{"user_id": 2, "alias": "阿狗", "text": "+12%"},
                 {"user_id": 1, "alias": "老李", "text": "+5%"}]},
        {"id": "red", "name": "红衫", "title": "计划完成王", "color": "#D9534F",
         "field": "plan_rate", "unit": "%", "hint": "有课的日子里完成了多少",
         "winners": [{"user_id": 2, "alias": "阿狗"}],
         "top": [{"user_id": 2, "alias": "阿狗", "text": "100%"},
                 {"user_id": 1, "alias": "老李", "text": "75%"}]},
        {"id": "blue", "name": "蓝衫", "title": "连续王", "color": "#4C8DF6",
         "field": "streak", "unit": "天", "hint": "本周最长连续出勤",
         "winners": [{"user_id": 1, "alias": "老李"}],
         "top": [{"user_id": 1, "alias": "老李", "text": "3 天"},
                 {"user_id": 2, "alias": "阿狗", "text": "3 天"}]},
    ],
}
SEASON["me"] = SEASON["people"][1]
# 分数明细：页面要能点开看「63.8 分是怎么来的」，所以每人都带 parts。
SEASON["weights"] = {"km": 30, "days": 20, "elev": 15, "progress": 15,
                     "streak": 10, "bonus": 10}
SEASON["titles"] = [
    {"name": "远征队长", "hint": "本周综合分在组里前 10%"},
    {"name": "先锋", "hint": "本周综合分在组里前 30%"},
    {"name": "行者", "hint": "在这个组已经参与满 3 周"},
    {"name": "同行者", "hint": "全组这周都动了，且你已参与满 3 周"}]
SEASON["all_active"] = False
SEASON["max"] = {"km": 42.3, "days": 5, "elev": 620, "streak": 5,
                 "progress": 0.12, "explorer": 51}
for _p, _parts, _wk in (
        (SEASON["people"][0], {"km": 30.0, "days": 20.0, "elev": 15.0,
                               "progress": 15.0, "streak": 10.0, "bonus": 0.0}, 8),
        (SEASON["people"][1], {"km": 25.5, "days": 15.0, "elev": 8.4,
                               "progress": 6.4, "streak": 5.5, "bonus": 3.0}, 5),
        (SEASON["people"][2], {"km": 9.1, "days": 8.0, "elev": 2.2,
                               "progress": 0.0, "streak": 4.0, "bonus": 0.0}, 2)):
    _p["parts"] = _parts
    _p["weeks"] = _wk

# P2 的「本周」块：目标 + 结对 + 预言。故意造一组「逆袭」的数据——
# 那是这三行里最不容易排版的组合（长短不一 + 有结论词）。
WEEK = {
    "ok": True, "from": "2026-11-16", "to": "2026-11-22",
    "goal": {"km": 30, "suggest": 30.4, "reported": True},
    "pairs": [{
        "slot": 1,
        "small": {"user_id": 1, "alias": "老李", "km": 30, "target": 30,
                  "actual": 38.1},
        "big": {"user_id": 2, "alias": "阿狗", "km": 60, "target": 75,
                "actual": 42.3},
        "even": False, "result": "upset",
    }],
    "predict": {"mine": 2, "rows": [
        {"user_id": 1, "target_uid": 2, "result": "wrong",
         "alias": "老李", "target_alias": "阿狗"},
    ]},
    "bonus": {2: 3.0},
    "goal_rows": [{"user_id": 2, "alias": "阿狗", "km": 60},
                  {"user_id": 1, "alias": "老李", "km": 30}],
    "targets": [{"user_id": 2, "alias": "阿狗"}],
    "bingo": {"done": 12, "total": 25, "lines": 1},
}

# Bingo 卡：用真实任务池生成，再把第 2 行整行点亮，好检查「一条线」的观感
sys.path.insert(0, str(ROOT / "server"))
import bingo as _B  # noqa: E402

_CARD = _B.make_card(1, 1, "2026-11-16")
BINGO = {"ok": True, "from": "2026-11-16", "to": "2026-11-22",
         "cells": [], "done": 0, "total": 25, "lines": [], "line_count": 0}
for _i, _c in enumerate(_CARD):
    _done = (_i // 5 == 1) or _c["task"] == "free" or _i in (0, 2, 14, 23)
    BINGO["cells"].append({"i": _i, "task": _c["task"], "text": _c["text"],
                           "level": _c["level"], "done": _done})
BINGO["done"] = sum(1 for c in BINGO["cells"] if c["done"])
BINGO["lines"] = ["r1"]
BINGO["line_count"] = 1


FEED = {
    "ok": True, "hidden": 1, "emojis": ["👍", "💪", "🔥", "😂", "👏", "🎉"],
    "rows": [
        {"id": 3, "user_id": 2, "alias": "阿狗", "color": "#3FA76A", "is_me": False,
         "date": TODAY, "kind": "run", "kind_name": "跑步", "distance_m": 12800,
         "duration_s": 4020, "elevation_m": 120, "comments": 3,
         "recent": [{"user_id": 1, "alias": "老李", "text": "这个配速可以",
                     "emoji": ""},
                    {"user_id": 3, "alias": "小赵", "text": "", "emoji": "🔥"}]},
        {"id": 2, "user_id": 1, "alias": "老李", "color": "#5EA0FF", "is_me": True,
         "date": "2026-11-18", "kind": "run", "kind_name": "跑步",
         "distance_m": 8000, "duration_s": 2700, "elevation_m": 60, "comments": 1,
         "recent": [{"user_id": 2, "alias": "阿狗", "text": "", "emoji": "💪"}]},
        {"id": 1, "user_id": 3, "alias": "小赵", "color": "#E8C43A", "is_me": False,
         "date": "2026-11-17", "kind": "ride", "kind_name": "骑行",
         "distance_m": 46000, "duration_s": 6300, "elevation_m": 380, "comments": 0,
         "recent": []},
    ],
}


def main() -> int:
    from garmin_cn import group as G
    from garmin_cn import groupui as GU

    from garmin_cn import account as AC
    AC.state = lambda: {"logged_in": True, "label": "laoli@example.com"}  # type: ignore
    AC.me = lambda: {"ok": True, "nickname": "老李"}                      # type: ignore
    G.detail = lambda gid: DETAIL                     # type: ignore[assignment]
    G.board = lambda gid, rng="week", today=None, by="dist": BOARD   # type: ignore[assignment]
    G.challenge_list = lambda gid, today=None: CHALLENGES  # type: ignore[assignment]
    G.groups = lambda: {"ok": True, "groups": [  # type: ignore[assignment]
        {"id": 1, "name": "周末拉练队", "role": "owner", "member_count": 4,
         "week": {"rank": 2, "total": 4, "km": 38.1},
         "honors": {"rank": 2, "total": 4, "total_km": 93.2, "goal_km": 39,
                    "shirts": [{"name": "黄衫", "title": "里程王", "color": "#E8C43A", "got": False},
                               {"name": "绿衫", "title": "出勤王", "color": "#3FA76A", "got": True},
                               {"name": "圆点衫", "title": "爬升王", "color": "#D8DEE9",
                                "pattern": "dots", "got": False},
                               {"name": "白衫", "title": "进步王", "color": "#F2F5FA", "got": False},
                               {"name": "红衫", "title": "计划完成王", "color": "#D9534F", "got": False},
                               {"name": "蓝衫", "title": "连续王", "color": "#4C8DF6", "got": True}],
                    "explorer": 7, "explore_min": 5}},
        {"id": 2, "name": "公司跑团", "role": "member", "member_count": 18,
         "week": {"rank": 11, "total": 18, "km": 12.4},
         "honors": {"rank": 11, "total": 18, "total_km": 612.8, "goal_km": None,
                    "shirts": [{"name": "黄衫", "title": "里程王", "color": "#E8C43A", "got": False},
                               {"name": "绿衫", "title": "出勤王", "color": "#3FA76A", "got": False},
                               {"name": "圆点衫", "title": "爬升王", "color": "#D8DEE9",
                                "pattern": "dots", "got": False},
                               {"name": "白衫", "title": "进步王", "color": "#F2F5FA", "got": False},
                               {"name": "红衫", "title": "计划完成王", "color": "#D9534F", "got": False},
                               {"name": "蓝衫", "title": "连续王", "color": "#4C8DF6", "got": False}],
                    "explorer": 0, "explore_min": 5}},
        {"id": 3, "name": "小区夜跑", "role": "member", "member_count": 2,
         "week": {"rank": 1, "total": 2, "km": 6.0},
         "honors": {"rank": 1, "total": 2, "total_km": 6.0, "goal_km": None,
                    "shirts": [], "explorer": 0, "explore_min": 5}}]}
    G.challenge_get = lambda gid, cid, today=None: {        # type: ignore[assignment]
        "ok": True, "hidden": 1,
        "challenge": next(c for c in CHALLENGES["challenges"] if c["id"] == cid),
        "rows": [
            {"alias": "阿狗", "is_me": False, "current": 42100, "pct": 100.0,
             "done_at": "2026-11-18"},
            {"alias": "老李", "is_me": True, "current": 38100, "pct": 91.0,
             "done_at": None},
            {"alias": "小赵", "is_me": False, "current": 13000, "pct": 31.0,
             "done_at": None}]}
    G.season = lambda gid, today=None: SEASON          # type: ignore[assignment]
    G.week = lambda gid, today=None: WEEK              # type: ignore[assignment]
    G.bingo = lambda gid, today=None: BINGO            # type: ignore[assignment]
    G.feed = lambda gid, today=None, act=None: FEED    # type: ignore[assignment]
    G.home = lambda gid, rng="week", today=None: {    # type: ignore[assignment]
        "ok": True, "detail": DETAIL, "board": BOARD,
        "challenges": CHALLENGES, "season": SEASON, "week": WEEK}

    html = GU.group_page("TOK", GID)
    # 站内绝对路径换成 file://，否则本地打开是一片空白
    for prefix, sub in (("/content/", "garmin_cn/content"),):
        html = html.replace(f'"{prefix}', f'"{ (ROOT / sub).as_uri()}/')
    html = html.replace("</body>", '<style>html{background:#05080d}'
                                   'body{width:420px;margin:0;padding:0 14px}</style></body>')
    p = OUT / "group.html"
    p.write_text(html, encoding="utf-8")
    print(f"{p}  {len(html)} 字节")

    # Bingo 页
    _bh = GU.bingo_page("TOK", GID)
    for prefix, sub in (("/content/", "garmin_cn/content"),):
        _bh = _bh.replace(f'"{prefix}', f'"{ (ROOT / sub).as_uri()}/')
    _bh = _bh.replace("</body>", '<style>html{background:#05080d}'
                                 'body{width:420px;margin:0;padding:0 14px}</style></body>')
    _p2 = OUT / "bingo.html"
    _p2.write_text(_bh, encoding="utf-8")
    print(f"{_p2}  {len(_bh)} 字节")

    # 六件衫页：让选中的几个衫标记成"我的"
    for _s in SEASON["shirts"]:
        _s["winners"] = [w for w in _s.get("winners") or []]
    _sh = GU.shirts_page("TOK", GID)
    for prefix, sub in (("/content/", "garmin_cn/content"),):
        _sh = _sh.replace(f'"{prefix}', f'"{ (ROOT / sub).as_uri()}/')
    _sh = _sh.replace("</body>", '<style>html{background:#05080d}'
                                 'body{width:420px;margin:0;padding:0 14px}</style></body>')
    _p3 = OUT / "shirts.html"
    _p3.write_text(_sh, encoding="utf-8")
    print(f"{_p3}  {len(_sh)} 字节")

    _st2 = GU.settings_page("TOK", GID)
    for prefix, sub in (("/content/", "garmin_cn/content"),):
        _st2 = _st2.replace(f'"{prefix}', f'"{ (ROOT / sub).as_uri()}/')
    _st2 = _st2.replace("</body>", '<style>html{background:#05080d}'
                                   'body{width:420px;margin:0;padding:0 14px}</style></body>')
    _p4 = OUT / "settings.html"
    _p4.write_text(_st2, encoding="utf-8")
    print(f"{_p4}  {len(_st2)} 字节")

    for _name, _html in (("feed", GU.feed_page("TOK", GID)),
                         ("feedone", GU.feed_page("TOK", GID, "", 3)),
                         ("board", GU.board_page("TOK", GID)),
                         ("boardmonth", GU.board_page("TOK", GID, "", "month")),
                         ("challenge", GU.challenge_page("TOK", GID, 1)),
                         ("groups", GU.groups_page("TOK", "", force_list=True))):
        for prefix, sub in (("/content/", "garmin_cn/content"),):
            _html = _html.replace(f'"{prefix}', f'"{ (ROOT / sub).as_uri()}/')
        _html = _html.replace("</body>", '<style>html{background:#05080d}'
                                         'body{width:420px;margin:0;padding:0 14px}</style></body>')
        _f = OUT / f"{_name}.html"
        _f.write_text(_html, encoding="utf-8")
        print(f"{_f}  {len(_html)} 字节")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
