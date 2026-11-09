"""磁盘占用治理：缓存该有多大、旧文件谁先走。

只处理**可再生的缓存**，不碰任何交付物或用户素材：

| 目录 | 能否删 | 说明 |
|---|---|---|
| `data/motion_cache/` | 可以 | 回放导出用的转码/抽帧中间文件，删了下次导出会重建 |
| `output/thumbs/` | 可以 | 成片封面，重新导出即可再生 |
| `output/` 其它 | **不动** | 卡片、活动图、回放成片都是用户要的东西 |
| `data/photos/` | **不动** | 用户从相册导入的原始照片/视频，删了不可恢复 |
| `data/garmin.db` | **不动** | 全部训练数据 |

`motion_cache` 以前没有任何上限：一段 30 分钟的 4K 素材能烘出几百 MB，
连续导几次就能把手机存储吃掉。这里按「先看总量、超额再从最旧的删」处理，
而不是无脑按天数删 —— 一周不导出不该被清空，一周导出二十次也不该留满。
"""
from __future__ import annotations

import time
from pathlib import Path

# 缓存目录的上限。手机端 `data/` 在应用私有空间里，几百 MB 已经很占地方；
# 桌面端重建一次也就几秒，宁可小一点。
CACHE_CAP_MB = 240
CACHE_SUFFIXES = (".mp4", ".mov", ".m4v", ".jpg", ".jpeg", ".png")

# 最近几小时内动过的文件不删：导出流程可能正拿着它（抽帧、转码都在读）。
# 只保护「小时级」，不按天 —— 否则缓存超限也清不动，等于没有上限。
CACHE_PROTECT_HOURS = 6


def dir_size(path: Path) -> int:
    """目录总字节数（不跟随符号链接，目录不存在返回 0）。"""
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    except OSError:
        return 0
    return total


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def _candidates(cache: Path, protect_hours: float) -> list[tuple[float, int, Path]]:
    """可回收的缓存文件，按「最久没动」排前面。"""
    cutoff = time.time() - protect_hours * 3600
    out: list[tuple[float, int, Path]] = []
    for p in cache.iterdir():
        if not p.is_file() or p.suffix.lower() not in CACHE_SUFFIXES:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime >= cutoff:
            continue                    # 可能正在被导出流程读写
        out.append((st.st_mtime, st.st_size, p))
    out.sort()
    return out


def prune_motion_cache(cache: Path, *, cap_mb: int = CACHE_CAP_MB,
                       protect_hours: float = CACHE_PROTECT_HOURS,
                       apply: bool = False) -> dict:
    """把缓存压到 `cap_mb` 以内，返回 {freed, removed, total, cap}。

    只有**超过上限**才动手；从最旧的开始删，直到回到上限以内。
    `apply=False` 只算不删（`run.bat clean` 默认预览，避免手滑删掉正在导出的素材）。
    """
    cache = Path(cache)
    cap = cap_mb * 1024 * 1024
    if not cache.is_dir():
        return {"freed": 0, "removed": 0, "total": 0, "cap": cap}

    total = dir_size(cache)
    if total <= cap:
        return {"freed": 0, "removed": 0, "total": total, "cap": cap}

    freed = removed = 0
    for _mtime, size, p in _candidates(cache, protect_hours):
        if total - freed <= cap:
            break
        if apply:
            try:
                p.unlink()
            except OSError:
                continue
        freed += size
        removed += 1

    return {"freed": freed, "removed": removed, "total": total - freed, "cap": cap}


def prune_thumbs(thumbs: Path, *, cap_mb: int = 80, protect_hours: float = 48,
                 apply: bool = False) -> dict:
    """封面图缓存：同样是可再生的，但比 motion_cache 便宜，上限给宽一点。"""
    return prune_motion_cache(thumbs, cap_mb=cap_mb, protect_hours=protect_hours,
                              apply=apply)


def survey(root: Path) -> list[tuple[str, int, str]]:
    """给 `clean` 用的体检表：(目录, 字节数, 备注)。"""
    root = Path(root)
    rows: list[tuple[str, int, str]] = []
    for rel, note in (
        ("data/motion_cache", "回放缓存 · 可清"),
        ("output/thumbs", "封面缓存 · 可清"),
        ("output", "交付物 · 不动"),
        ("data/photos", "原始素材 · 不动"),
        ("data", "数据库等 · 不动"),
        ("dist", "本地快照 · 可删旧的"),
    ):
        p = root / rel
        if p.exists():
            rows.append((rel, dir_size(p), note))
    return rows
