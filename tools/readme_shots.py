# -*- coding: utf-8 -*-
"""生成 README 用的界面截图，数据**全部来自 `demo` 仿真库**。

为什么要单开一个脚本
--------------------
README 要放效果图，但活动图和轨迹回放里带 GPS 轨迹，看板里全是真实的睡眠 /
心率 / 体重数值——直接截真实数据，等于把住址和体检报告发到公开仓库。
所以这里先让 `garmin_cn.demo` 造一份几百天的仿真库，再逐页渲染截图。

用法
----
    python tools/readme_shots.py              # 输出到 docs/img/
    python tools/readme_shots.py --days 400   # 仿真天数（影响历史页图表密度）
    python tools/readme_shots.py --out 某目录

依赖本机 Chrome / Edge（与出图同一条无头截图通道）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 截图绝不能顺带去高德取底图：那会把真实地图图块和版权素材带进仓库。
os.environ.setdefault("GARMIN_MAP", "0")

CARD_W = 380          # 9:16 产物在 README 里的展示宽度
APP_W = 380           # App 页面展示宽度


def _browser() -> str:
    from garmin_cn.card import _find_browser

    got = _find_browser()
    if not got:
        raise SystemExit("× 没找到 Chrome/Edge，无法截图")
    return got


def shot(html: Path, png: Path, width: int, height: int, *,
         wait: int = 3000, query: str = "") -> Path:
    """把一个本地 HTML 截成 PNG（视口即画幅）。

    `query` 追加到 file:// URL 后面，播放器用它直接跳到指定时刻
    （`?t=18`）。不要改成页面里 `location.search = ...` —— 那会触发重载，
    脚本在重载后又执行一遍，直接进入死循环。
    """
    png.parent.mkdir(parents=True, exist_ok=True)
    if png.exists():
        png.unlink()
    url = html.resolve().as_uri() + (("?" + query) if query else "")
    subprocess.run(
        [_browser(), "--headless=new", "--disable-gpu", "--hide-scrollbars",
         f"--window-size={width},{height}", f"--virtual-time-budget={wait}",
         f"--screenshot={png.resolve()}", url],
        capture_output=True, timeout=120,
    )
    if not png.is_file():
        raise SystemExit(f"× 截图失败：{html.name}")
    return png


def png_size(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:33]
    return struct.unpack(">II", head[16:24])


def shrink(src: Path, dst: Path, width: int) -> Path:
    """按宽等比缩小。

    仓库不带 Pillow（零第三方依赖），所以让浏览器替我们做重采样：
    一个只有 <img> 的页面、视口设成目标尺寸，截图就是缩放后的结果，
    质量比手写最近邻好得多。
    """
    w, h = png_size(src)
    if w == width:
        dst.write_bytes(src.read_bytes())
        return dst
    height = max(1, round(h * width / w))
    page = src.with_name(src.stem + "-shrink.html")
    page.write_text(
        f'<!doctype html><meta charset="utf-8">'
        f'<style>html,body{{margin:0;background:#070B12}}'
        f'img{{display:block;width:{width}px;height:{height}px}}</style>'
        f'<img src="{src.resolve().as_uri()}">',
        encoding="utf-8",
    )
    return shot(page, dst, width, height, wait=1500)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 README 截图（demo 仿真数据）")
    ap.add_argument("--days", type=int, default=400, help="仿真天数，默认 400")
    ap.add_argument("--out", default=str(ROOT / "docs" / "img"), help="输出目录")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="rc-shots-"))

    from garmin_cn.demo import build

    db = tmp / "demo.db"
    print(f"→ 生成 {args.days} 天仿真数据 …", flush=True)
    store = build(db, days=args.days)

    # 所有页面都走这份仿真库：appapi 里 DB / OUT 都是模块级常量
    import garmin_cn.appapi as A
    A.DB = db
    A.OUT = tmp

    from garmin_cn import actcard, aiui, card, history, motion, ui
    from garmin_cn.metrics import derive
    from garmin_cn.plan import attach

    day = dt.date.today().isoformat()
    made: list[tuple[str, Path, int]] = []

    # ---- 一图流 ---------------------------------------------------------
    res = attach(derive(store, day))
    html = card.build_html(res, sync_at=store.get_meta("last_sync"),
                           src="DEMO 仿真数据")
    (tmp / "card.html").write_text(html, encoding="utf-8")
    raw = shot(tmp / "card.html", tmp / "card.png", 1080, 1920)
    made.append(("一图流", shrink(raw, out_dir / "card.png", CARD_W), CARD_W))

    # ---- 单次活动图 -----------------------------------------------------
    row = store.conn.execute(
        "SELECT * FROM activities WHERE type='running' AND distance_m > 8000 "
        "ORDER BY date DESC LIMIT 1").fetchone()
    if row is None:
        row = store.conn.execute(
            "SELECT * FROM activities WHERE distance_m > 2000 "
            "ORDER BY date DESC LIMIT 1").fetchone()
    row = dict(row)
    detail = store.get_detail(row["activity_id"])
    raw = actcard.render(row, detail, tmp / "activity.png", use_map=False)
    made.append(("活动图", shrink(raw, out_dir / "activity.png", CARD_W), CARD_W))

    # ---- 轨迹回放（取动画中段一帧）--------------------------------------
    detail = dict(detail)
    detail["route"] = A._synth_route(detail)
    replay_html = tmp / "replay.html"
    motion.render(row, detail, None, replay_html, duration=30.0, web=False, clips=[])
    # `?t=` 让播放器直接跳到第 18 秒，截到的才是「跑到一半」而不是起跑瞬间
    raw = shot(replay_html, tmp / "replay.png", 1080, 1920, wait=5000, query="t=18")
    made.append(("轨迹回放", shrink(raw, out_dir / "replay.png", CARD_W), CARD_W))

    # ---- 完整看板（App 里点开的那一页）----------------------------------
    page = history.view_page({"date": [day]})
    (tmp / "dash.html").write_text(page, encoding="utf-8")
    made.append(("完整看板", shot(tmp / "dash.html", out_dir / "dashboard.png", 420, 1180),
                 420))

    # ---- App 三个主要页面 -----------------------------------------------
    for name, label, html in (
        ("app-today", "今日", ui.home_page()),
        ("app-history", "历史", history.history_page({"mode": ["month"]})),
        ("app-ai", "AI 教练", aiui.render({})),
    ):
        src = tmp / f"{name}.html"
        src.write_text(html, encoding="utf-8")
        raw = shot(src, tmp / f"{name}.png", 420, 900)
        made.append((label, shrink(raw, out_dir / f"{name}.png", APP_W), APP_W))

    store.conn.close()
    print()
    total = 0
    for label, path, w in made:
        size = path.stat().st_size
        total += size
        print(f"  {label:<8} {path.relative_to(ROOT)}  {w}px  {size / 1024:.0f} KB")
    print(f"\n√ {len(made)} 张截图，合计 {total / 1024:.0f} KB → {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
