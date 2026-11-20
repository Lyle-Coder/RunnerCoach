#!/usr/bin/env python3
"""每日备份：把 SQLite 库在线拷一份到 backup 目录，保留 N 天。

为什么不用 `cp`：库开着 WAL，直接拷可能拿到「主库 + 没写完的 -wal」，
恢复时会缺最近的写入。`sqlite3.Connection.backup()` 是 SQLite 官方的
在线备份接口，会自己处理锁与 WAL，不需要停服务。

服务器上没装 sqlite3 CLI，所以用 python3 跑（镜像里本来就有 python）。

    python3 deploy/backup.py                       # 默认 /opt/runnercoach
    python3 deploy/backup.py --root /tmp/x --keep 3

退出码 0 = 至少成功备份了一个库；1 = 一个都没成功（让 systemd 记成 failed）。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

DBS = ("data.db", "groups.db")


def backup_one(src: str, dst: str) -> None:
    # 只读打开源库：备份过程中不写任何东西回线上库
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        d = sqlite3.connect(dst)
        try:
            with d:
                s.backup(d)
        finally:
            d.close()
    finally:
        s.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/opt/runnercoach")
    ap.add_argument("--keep", type=int, default=14, help="保留天数")
    a = ap.parse_args()

    data = os.path.join(a.root, "data")
    out = os.path.join(a.root, "backup")
    os.makedirs(out, exist_ok=True)

    stamp = time.strftime("%Y%m%d-%H%M")
    ok = 0
    for name in DBS:
        src = os.path.join(data, name)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(out, f"{name[:-3]}-{stamp}.db")
        try:
            backup_one(src, dst)
        except Exception as e:                      # noqa: BLE001
            print(f"× {name}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        ok += 1
        print(f"√ {dst}  {os.path.getsize(dst) // 1024} KB")

    # 清理：按文件名里的时间戳排，只留最近 keep 天
    cutoff = time.time() - a.keep * 86400
    for f in sorted(os.listdir(out)):
        p = os.path.join(out, f)
        if f.endswith(".db") and os.path.getmtime(p) < cutoff:
            try:
                os.remove(p)
                print(f"- 清理 {f}")
            except OSError:
                pass
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
