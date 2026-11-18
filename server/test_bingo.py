# -*- coding: utf-8 -*-
"""Bingo 卡的离线单测（不启动服务、不碰数据库）。

    python test_bingo.py

这套东西最容易错的地方是「单日」和「本周」两个口径串了：4 天各 3 km 的周，
周合计 12 km 够 15 km 的格子吗？不够；但「单日 5 km」也不能因为合计够了就点亮。
所以下面专门用「均匀小量」和「单日大单」两种数据各跑一遍。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bingo as B  # noqa: E402
import ranking as R  # noqa: E402

ok = fail = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  [ok] {name}")
    else:
        fail += 1
        print(f"  [xx] {name}  {extra}")


def day(d: str, km=0.0, elev=0, acts=1, plan=None, expl=0) -> dict:
    return {"date": d, "distance_m": int(km * 1000), "duration_s": int(km * 330),
            "acts": acts, "elevation_m": elev, "plan_done": plan, "explorer": expl}


def main() -> int:
    print("\n1. 卡的形状")
    card = B.make_card(1, 7, "2026-11-16")
    check("25 格", len(card) == 25, str(len(card)))
    check("中心是免费格", card[B.CENTER]["task"] == "free", str(card[B.CENTER]))
    check("格子不重复", len({c["task"] for c in card}) == 25, "有重复任务")
    lv = [c["level"] for c in card]
    check("易/中/难各 8 格",
          lv.count(B.LEVEL_EASY) == 8 and lv.count(B.LEVEL_MID) == 8
          and lv.count(B.LEVEL_HARD) == 8,
          f"{lv.count(1)}/{lv.count(2)}/{lv.count(3)}")

    print("\n2. 确定性：同组同人同周，卡一样")
    a1 = [c["task"] for c in B.make_card(1, 7, "2026-11-16")]
    a2 = [c["task"] for c in B.make_card(1, 7, "2026-11-16")]
    check("重算两次完全一致", a1 == a2)
    b1 = [c["task"] for c in B.make_card(1, 8, "2026-11-16")]
    check("换个人，卡不一样", a1 != b1)
    c1 = [c["task"] for c in B.make_card(1, 7, "2026-11-23")]
    check("换一周，卡不一样", a1 != c1)

    print("\n3. 单日口径 vs 周合计口径")
    # 四天各 3 km：周合计 12 km，但没有任何一天到 5 km
    flat = [day("2026-11-16", 3, plan=True), day("2026-11-17", 3, plan=True),
            day("2026-11-18", 3, plan=True), day("2026-11-19", 3, plan=True)]
    m_flat = R.metrics(flat, [])
    res = B.card_for(flat, m_flat, 1, 7, "2026-11-16")
    # 造一张「全是 d5 + 全是 w15」的对照卡，直接判
    d5 = [{"task": "d5", "text": "", "level": 1}] * 25
    w15 = [{"task": "w15", "text": "", "level": 1}] * 25
    check("四天各 3 km 拿不到「单日 5 km」",
          B.evaluate(d5, flat, m_flat)["done"] == 0)
    check("但「本周 15 km」也不够（合计 12）",
          B.evaluate(w15, flat, m_flat)["done"] == 0)
    big = [day("2026-11-16", 16, plan=True)]
    m_big = R.metrics(big, [])
    check("一天 16 km 才算「单日 5 km」", B.evaluate(d5, big, m_big)["done"] == 25)
    check("一天 16 km 也够「本周 15 km」", B.evaluate(w15, big, m_big)["done"] == 25)
    check("真卡上用均匀小量不会全亮", res["done"] < 20, str(res["done"]))

    print("\n4. 各项任务口径")
    rows = [day("2026-11-16", 10, elev=350, acts=2, plan=True, expl=6),
            day("2026-11-17", 8, elev=200, plan=True, expl=4),
            day("2026-11-21", 9, elev=120, plan=True, expl=10)]
    m = R.metrics(rows, [])

    def one(tid):
        return B.evaluate([{"task": tid, "text": "", "level": 1}] * 25, rows, m)["done"] == 25

    check("单日 10 km 达标", one("d10"))
    check("单日 15 km 不达标", not one("d15"))
    check("出勤 3 天达标", one("days3"))
    check("出勤 4 天不达标", not one("days4"))
    check("连续 2 天达标", one("streak2"))
    check("连续 3 天不达标（中间断了一天）", not one("streak3"))
    check("单日爬升 300 m 达标", one("dayelev300"))
    check("单日爬升 600 m 不达标", not one("dayelev600"))
    check("新踩 8 格达标（6+4+10）", one("explore8"))
    check("新踩 18 格达标（合计 20）", one("explore18"))
    check("一天跑两次达标", one("twice"))
    check("周末跑一次达标（11-21 是周六）", one("weekend"))
    check("按计划完成 1 次达标", one("plan1"))
    m_plan = R.metrics([day("2026-11-16", 5, plan=False)], [])
    check("「有课没跑」不算按计划完成",
          B.evaluate([{"task": "plan1", "text": "", "level": 1}] * 25,
                     [day("2026-11-16", 5, plan=False)], m_plan)["done"] == 0)

    print("\n5. 连线")
    full = [{"task": "free", "text": "", "level": 0}] * 25
    r_full = B.evaluate(full, rows, m)
    check("全亮 = 12 条线（5 行 + 5 列 + 2 对角）", r_full["line_count"] == 12,
          str(r_full["lines"]))
    # 只让第 2 行亮：把 d5 判完成、其余判失败
    row2 = []
    for i in range(25):
        if i // 5 == 1:
            row2.append({"task": "days3", "text": "", "level": 1})   # 这个会亮
        else:
            row2.append({"task": "d18", "text": "", "level": 3})     # 这个不亮
    r2 = B.evaluate(row2, rows, m)
    check("一行 5 格全亮 = 1 条线", r2["line_count"] == 1 and r2["lines"] == ["r1"],
          str(r2["lines"]))
    # 对角
    diag = []
    for i in range(25):
        if i % 6 == 0:
            diag.append({"task": "days3", "text": "", "level": 1})
        else:
            diag.append({"task": "d18", "text": "", "level": 3})
    rd = B.evaluate(diag, rows, m)
    check("主对角亮 = 1 条线", rd["lines"] == ["d0"], str(rd["lines"]))

    print("\n6. 空数据")
    r0 = B.card_for([], R.metrics([], []), 1, 7, "2026-11-16")
    check("没数据时只有中心格亮", r0["done"] == 1, str(r0["done"]))
    check("没数据时没有线", r0["line_count"] == 0)
    check("空数据不崩", r0["total"] == 25)

    print("\n7. 脏数据不炸整张卡")
    dirty = [{"date": "不是日期", "distance_m": "abc", "acts": None}]
    rd2 = B.card_for(dirty, R.metrics([], []), 1, 7, "2026-11-16")
    check("坏行被跳过，卡还能算", rd2["total"] == 25 and rd2["done"] >= 1, str(rd2["done"]))

    print(f"\n通过 {ok} 项，失败 {fail} 项")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
