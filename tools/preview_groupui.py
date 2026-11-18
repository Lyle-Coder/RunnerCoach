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
         "acts": 5, "days": 5, "plan_rate": 1.0, "updated_at": 0},
        {"alias": "老李", "is_me": True, "distance_m": 38100, "duration_s": 12600,
         "acts": 4, "days": 4, "plan_rate": 0.75, "updated_at": 0},
        {"alias": "小赵", "is_me": False, "distance_m": 12800, "duration_s": 4200,
         "acts": 2, "days": 2, "plan_rate": 0.5, "updated_at": 0},
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


def main() -> int:
    from garmin_cn import group as G
    from garmin_cn import groupui as GU

    G.detail = lambda gid: DETAIL                     # type: ignore[assignment]
    G.board = lambda gid, rng="week", today=None: BOARD   # type: ignore[assignment]
    G.challenge_list = lambda gid, today=None: CHALLENGES  # type: ignore[assignment]

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
