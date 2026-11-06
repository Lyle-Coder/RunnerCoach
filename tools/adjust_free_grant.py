# -*- coding: utf-8 -*-
"""把「新用户赠送」从旧额度校正到新额度（默认 2 元 → 0.5 元）。

账本是 append-only：不删不改旧流水，只补一笔 `adjust` 差额，
`users.balance` 同步到新值。脚本可重复执行（按 ref 标记判重），
默认 dry-run，加 --apply 才写库。

    python tools/adjust_free_grant.py                      # 预览
    python tools/adjust_free_grant.py --apply             # 执行
    python tools/adjust_free_grant.py --target 500000 --apply

只处理「赠送额度高于目标」的账号；余额已被消费到不足扣回差额时会跳过
并给出提示，避免把余额扣成负数。
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "server" / "data" / "data.db"
MARK = "赠送额度政策校正"          # adjust 流水的固定标记，用于幂等判重
GRANT_LIKE = "%赠送%"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--target", type=int, default=500_000,
                    help="新的赠送额度（微元，500000 = 0.5 元）")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    path = Path(args.db)
    if not path.is_file():
        print(f"× 找不到数据库：{path}")
        return 1

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    users = conn.execute("SELECT id,phone,balance FROM users ORDER BY id").fetchall()
    print(f"数据库：{path}    目标赠送额度：{args.target / 1e6:.2f} 元"
          f"    模式：{'写入' if args.apply else '预览'}")
    changed = 0
    for u in users:
        rows = conn.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM ledger "
            "WHERE user_id=? AND kind='grant' AND ref LIKE ?",
            (u["id"], GRANT_LIKE)).fetchone()["s"]
        already = conn.execute(
            "SELECT COUNT(*) n FROM ledger WHERE user_id=? AND kind='adjust' AND ref=?",
            (u["id"], MARK)).fetchone()["n"]
        over = int(rows) - args.target
        if over <= 0:
            print(f"  id={u['id']} {u['phone'] or '(匿名)':<28} 赠送 {rows / 1e6:.2f} 元 → 无需校正")
            continue
        if already:
            print(f"  id={u['id']} {u['phone'] or '(匿名)':<28} 已校正过，跳过")
            continue
        if u["balance"] < over:
            print(f"  id={u['id']} {u['phone'] or '(匿名)':<28} 余额不足扣回 "
                  f"{over / 1e6:.2f} 元，跳过（有消费记录，需人工核对）")
            continue
        after = u["balance"] - over
        print(f"  id={u['id']} {u['phone'] or '(匿名)':<28} 扣回 {over / 1e6:.2f} 元："
              f"余额 {u['balance'] / 1e6:.2f} → {after / 1e6:.2f} 元")
        if args.apply:
            conn.execute("UPDATE users SET balance=? WHERE id=?", (after, u["id"]))
            conn.execute("INSERT INTO ledger(user_id,ts,kind,amount,balance_after,ref) "
                         "VALUES(?,?,?,?,?,?)",
                         (u["id"], int(time.time()), "adjust", -over, after, MARK))
        changed += 1
    if args.apply:
        conn.commit()
        print(f"√ 已写入 {changed} 笔 adjust 流水")
    else:
        print(f"（预览）将写入 {changed} 笔，确认后加 --apply")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
