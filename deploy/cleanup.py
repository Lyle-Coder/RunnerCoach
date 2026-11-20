#!/usr/bin/env python3
"""清理发版中间产物，避免磁盘被历史版本堆满。

    python3 cleanup.py --dry-run     # 只看会删什么
    python3 cleanup.py               # 真删

保留规则
--------
- **APK**：`latest.json` 指向的那个 + 最近 N 个（默认 2）。
- **代码包**：按 `base_version` 分组，每组保留最近 K 个（默认 3），
  外加 `latest.json` 指向的那个。
  为什么要分组：客户端 `_fits()` 会校验 base_version，还停在老 APK 上的
  用户只能下载对应 base 的包。一刀切删旧的会让那批人更新失败。
- **备份**：按天数保留（默认 14 天），和 `backup.py` 的口径一致。
- **journald**：vacuum 到 200M。

磁盘水位
--------
>80% 时 APK 只留 1 个、代码包每组留 1 个；>90% 时 journal 再收到 100M。
阈值低的时候不主动删，避免为了省空间把能用的版本删掉。

只删**文件名匹配**的产物（`RunnerCoach-v*.apk`、`code-*.zip`、
`data-*.db`、`groups-*.db`），`latest.json` 和脚本永不删。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path(os.environ.get("RC_APP_DIR", "/opt/runnercoach"))
APK_DIR = Path(os.environ.get("RC_APK_DIR", APP_DIR / "apk"))
CODE_DIR = Path(os.environ.get("RC_CODEPACK_DIR", APP_DIR / "codepack"))
BAK_DIR = Path(os.environ.get("RC_BACKUP_DIR", APP_DIR / "backup"))

KEEP_APK = 2
KEEP_CODE_PER_BASE = 3
KEEP_BAK_DAYS = 14
JOURNAL_MAX = "200M"
JOURNAL_MAX_TIGHT = "100M"

APK_RE = re.compile(r"^RunnerCoach-v[\w.\-]+\.apk$")
CODE_RE = re.compile(r"^code-(?P<base>.+?)-\d+\.zip$")
BAK_RE = re.compile(r"^(?:data|groups)-(\d{8}-\d{4})\.db$")


def _read_json(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:      # noqa: BLE001
        return {}


def _disk_pct() -> float:
    """根分区使用率。拿不到就当成 0，不让清理因为测不到空间而中断。"""
    try:
        u = shutil.disk_usage("/")
        return u.used / u.total * 100 if u.total else 0.0
    except Exception:      # noqa: BLE001
        return 0.0


def _rm(p: Path, dry: bool, log: list[str]) -> int:
    try:
        size = p.stat().st_size
    except OSError:
        size = 0
    log.append(f"  {'[dry] ' if dry else ''}删除 {p.name}  {size / 1048576:.1f} MB")
    if not dry:
        try:
            p.unlink()
        except OSError as e:
            log.append(f"    失败：{e}")
            return 0
    return size


def clean_apk(dry: bool, keep: int, log: list[str]) -> int:
    if not APK_DIR.is_dir():
        return 0
    latest = _read_json(APK_DIR / "latest.json")
    keep_name = str(latest.get("file") or "")
    files = [p for p in APK_DIR.iterdir() if p.is_file() and APK_RE.match(p.name)]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    kept = {keep_name} if keep_name else set()
    kept |= {p.name for p in files[:keep]}
    freed, local = 0, []
    for p in files:
        if p.name in kept:
            continue
        freed += _rm(p, dry, local)
    log.extend(local)
    log.append(f"APK：{len(files)} 个，保留 {len(kept)} 个")
    return freed


def clean_code(dry: bool, keep: int, log: list[str]) -> int:
    if not CODE_DIR.is_dir():
        return 0
    latest = _read_json(CODE_DIR / "latest.json")
    keep_name = str(latest.get("file") or "")
    groups: dict[str, list[Path]] = {}
    for p in CODE_DIR.iterdir():
        if not p.is_file():
            continue
        m = CODE_RE.match(p.name)
        if not m:
            continue
        # base 形如 1.6.0；万一有人把 base 里带了 `-`，取最后一段版本号拼接
        groups.setdefault(m.group("base"), []).append(p)
    freed = 0
    for base, files in sorted(groups.items()):
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        kept = {p.name for p in files[:keep]}
        if keep_name:
            kept.add(keep_name)
        local = []
        for p in files:
            if p.name in kept:
                continue
            freed += _rm(p, dry, local)
        log.extend(local)
        log.append(f"代码包 base={base}：{len(files)} 个，保留 {len(kept)} 个")
    return freed


def clean_backup(dry: bool, days: int, log: list[str]) -> int:
    if not BAK_DIR.is_dir():
        return 0
    cutoff = time.time() - days * 86400
    freed, local, n = 0, [], 0
    for p in BAK_DIR.iterdir():
        if not p.is_file() or not BAK_RE.match(p.name):
            continue
        n += 1
        if p.stat().st_mtime >= cutoff:
            continue
        freed += _rm(p, dry, local)
    log.extend(local)
    log.append(f"备份：{n} 个，保留 {days} 天")
    return freed


def clean_journal(dry: bool, size: str, log: list[str]) -> None:
    if dry:
        log.append(f"journald：dry-run 跳过 vacuum（目标 {size}）")
        return
    try:
        subprocess.run(["journalctl", f"--vacuum-size={size}"],
                       capture_output=True, timeout=60, check=False)
        log.append(f"journald：已收 to {size}")
    except Exception as e:      # noqa: BLE001
        log.append(f"journald：跳过（{type(e).__name__}）")


def clean_group_data(dry_run: bool, log: list[str]) -> None:
    """组数据的保留期清理（日汇总 / 动态 / 评论 / 路线 / 审计 / 格子）。

    和产物清理挤在同一个 04:10 窗口里：两个脚本同时写同一个 sqlite 库会
    `database is locked`，串在一个进程里最省事。

    清理规则在 `server/groups.py` 的 `RETENTION_DAYS`，这里不重复定义——
    两个地方各写一份保留期，迟早会有一条对不上。
    """
    try:
        sys.path.insert(0, str(APP_DIR))
        import groups  # noqa: PLC0415  在函数里导入：APP_DIR 要先就位

        r = groups.cleanup(dry_run=dry_run)
        rows = r.get("rows") or {}
        total = sum(int(v or 0) for v in rows.values())
        if not total:
            log.append("组数据：没有超期记录")
            return
        for k, v in rows.items():
            if v:
                log.append(f"组数据 {k}：{'预计删' if dry_run else '已删'} {v} 行")
    except Exception as e:      # noqa: BLE001 - 组数据清理失败不该连累产物清理
        log.append(f"组数据：清理失败 {type(e).__name__}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-apk", type=int, default=KEEP_APK)
    ap.add_argument("--keep-code", type=int, default=KEEP_CODE_PER_BASE)
    ap.add_argument("--keep-days", type=int, default=KEEP_BAK_DAYS)
    ap.add_argument("--skip-groups", action="store_true",
                    help="不清理组数据（只处理发版产物）")
    a = ap.parse_args()

    pct = _disk_pct()
    log: list[str] = [f"磁盘使用 {pct:.1f}%"]
    keep_apk, keep_code = a.keep_apk, a.keep_code
    journal_max = JOURNAL_MAX
    if pct >= 90:
        keep_apk, keep_code, journal_max = 1, 1, JOURNAL_MAX_TIGHT
        log.append("水位 ≥90%，按最激进策略清理")
    elif pct >= 80:
        keep_apk, keep_code = 1, 1
        log.append("水位 ≥80%，减少保留版本")

    freed = 0
    freed += clean_code(a.dry_run, keep_code, log)
    freed += clean_apk(a.dry_run, keep_apk, log)
    freed += clean_backup(a.dry_run, a.keep_days, log)
    clean_journal(a.dry_run, journal_max, log)
    if not a.skip_groups:
        clean_group_data(a.dry_run, log)

    log.append(f"{'预计释放' if a.dry_run else '已释放'} {freed / 1048576:.1f} MB")
    print("\n".join(log))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
