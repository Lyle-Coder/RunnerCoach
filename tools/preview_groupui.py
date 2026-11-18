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
         "winners": [{"user_id": 2, "alias": "阿狗"}]},
        {"id": "green", "name": "绿衫", "title": "出勤王", "color": "#3FA76A",
         "winners": [{"user_id": 2, "alias": "阿狗"}]},
        {"id": "polka", "name": "圆点衫", "title": "爬升王", "color": "#D8DEE9",
         "winners": []},
        {"id": "white", "name": "白衫", "title": "进步王", "color": "#F2F5FA",
         "winners": [{"user_id": 2, "alias": "阿狗"}]},
        {"id": "red", "name": "红衫", "title": "计划完成王", "color": "#D9534F",
         "winners": [{"user_id": 2, "alias": "阿狗"}]},
        {"id": "blue", "name": "蓝衫", "title": "连续王", "color": "#4C8DF6",
         "winners": [{"user_id": 1, "alias": "老李"}]},
    ],
}
SEASON["me"] = SEASON["people"][1]

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
}


def main() -> int:
    from garmin_cn import group as G
    from garmin_cn import groupui as GU

    G.detail = lambda gid: DETAIL                     # type: ignore[assignment]
    G.board = lambda gid, rng="week", today=None: BOARD   # type: ignore[assignment]
    G.challenge_list = lambda gid, today=None: CHALLENGES  # type: ignore[assignment]
    G.season = lambda gid, today=None: SEASON          # type: ignore[assignment]
    G.week = lambda gid, today=None: WEEK              # type: ignore[assignment]
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
