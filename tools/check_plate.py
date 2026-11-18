# -*- coding: utf-8 -*-
"""换底图自检：拿一张候选底图，看矢量脚印落得对不对。

为什么要这个工具
----------------
底图和脚印是两层：底图是小屋+雪地+夜空，脚印是代码画的矢量。换一张图，
第一件要确认的事不是"好不好看"，而是**脚印有没有落在雪地里、有没有走到门口**。
这件事只能靠看，而看一次要跑起服务、造一个赛季、跳到结算页——太远。

所以这里跳过服务端：直接拿结局数据造五张结算页 + 一张分享卡，把候选图当
底图，输出到 `preview/plate/`。

    python tools/check_plate.py 亮窗.png [暗窗.png] [--door 0.63 0.70]

只给一张时，暗窗的三种也会用这张（灯会是错的，但位置和脚印能看）。

`--door` 是底图上**门的位置**（相对宽高的 0-1 比例）。它会写进
`ending_scene.json`，脚印的终点就落在那里。不知道填什么就先不填，
看 `guides.png`：红线是雪线、绿线是门的位置、圆圈是脚印终点。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT.parent / "preview" / "plate"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
WIDTH = 420

# 结局文案**从服务端内容文件读**，不写在这里。
# 这个文件是公开仓库的一部分：把五段结局正文硬编码进来，等于把最值钱的那点
# 东西直接印在 README 旁边。内容在 server/content/（gitignore），本机跑工具时
# 读得到，别人 clone 下来只看到这个函数。
POOL_PATH = ROOT / "server" / "content" / "expedition_events.json"

PLACEHOLDER = {
    "arrive": {"title": "（结局一）", "line": "（一句）", "text": "（正文）"},
    "detour": {"title": "（结局二）", "line": "（一句）", "text": "（正文）"},
    "missing": {"title": "（结局三）", "line": "（一句）", "text": "（正文）"},
    "lost": {"title": "（结局四）", "line": "（一句）", "text": "（正文）"},
    "discoverer": {"title": "（结局五）", "line": "（一句）", "text": "（正文）"},
}


def load_cases() -> dict:
    """读服务端内容里的 endings；读不到就用占位——版面照样能看。"""
    import json

    try:
        data = json.loads(POOL_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        print(f"！没有 {POOL_PATH.name}，用占位文案（版面可看，文案不是你写的）")
        data = {}
    ends = data.get("endings") or {}
    out = {}
    for key in ("arrive", "detour", "missing", "lost", "discoverer"):
        e = ends.get(key) or {}
        base = dict(PLACEHOLDER[key])
        base.update({k: e[k] for k in ("title", "line", "text") if e.get(k)})
        # 版面测试要的四个数字，跟文案无关，写死无妨
        base.update({"team": 52.0, "morale": 2 if key == "detour" else 7,
                     "relics": {"lost": 0, "discoverer": 3, "arrive": 1,
                                "detour": 1, "missing": 0}[key]})
        out[key] = base
    return out


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    from garmin_cn import runsui as RU

    CASES = load_cases()

    door = None
    if "--door" in argv:
        i = argv.index("--door")
        try:
            door = (float(argv[i + 1]), float(argv[i + 2]))
        except (IndexError, ValueError):
            print("--door 要两个 0-1 的数，比如 --door 0.63 0.70")
            return 2
        argv = argv[:i] + argv[i + 3:]

    src = [Path(p).expanduser() for p in argv[:2]]
    for p in src:
        if not p.is_file():
            print(f"找不到图：{p}")
            return 2

    OUT.mkdir(parents=True, exist_ok=True)
    # 拷进一个临时目录，按约定命名，这样 `_plates()` 会认
    plate_dir = Path(tempfile.mkdtemp(prefix="rc-plate-"))
    shutil.copyfile(src[0], plate_dir / "ending_scene_lit.png")
    shutil.copyfile(src[-1], plate_dir / "ending_scene_dark.png")
    # 先切到临时目录再读校准：`_cal()` 是从 PLATE_DIR 里读的
    RU.PLATE_DIR = plate_dir
    if door:
        import json as _json

        (plate_dir / RU.CAL_FILE).write_text(
            _json.dumps({"door_x": door[0], "door_y": door[1]}), encoding="utf-8")
    cal = RU._cal()
    print(f"门口：x={cal['door_x']:.3f} y={cal['door_y']:.3f}（改 --door 调整）")

    font = (ROOT / "garmin_cn/content/expedition-serif.woff2").as_uri()
    wrap = (f"<style>html{{background:#05080d}}"
            f"body{{width:{WIDTH}px;margin:0 auto}}</style>")

    def fix(h: str) -> str:
        h = h.replace('url("/fonts/expedition-serif.woff2")', f'url("{font}")')
        return h.replace('"/content/', f'"{plate_dir.as_uri()}/')

    pages = {}
    for key, c in CASES.items():
        d = {"ok": True, "ended": True, "ending": key,
             "ending_detail": {"key": key, "title": c["title"],
                               "line": c["line"], "text": c["text"]},
             "team": c["team"], "morale": c["morale"], "members": 3,
             "relic_count": c["relics"], "relics": [], "acts": ACTS,
             "start": "2026-11-16", "end": "2026-12-13", "season": "雪线之前"}
        (OUT / f"end_{key}.html").write_text(
            fix(RU.outcome_page("T", d)).replace("</body>", wrap + "</body>"),
            encoding="utf-8")
        pages[f"end_{key}"] = True

    # 分享卡：底图在 1080 宽时是什么样，和手机上看到的成品一致
    from garmin_cn import runcard as RC

    d = {"ok": True, "ended": True, "ending": "arrive",
         "ending_detail": {"key": "arrive", "title": CASES["arrive"]["title"],
                           "line": CASES["arrive"]["line"],
                           "text": CASES["arrive"]["text"]},
         "team": 52.0, "morale": 7, "members": 3, "relic_count": 1,
         "acts": ACTS, "start": "2026-11-16", "end": "2026-12-13",
         "season": "雪线之前"}
    (OUT / "share.html").write_text(fix(RC.build_html(d)), encoding="utf-8")

    # 引导图：同一张底图，叠上雪线（红）、门口（绿）和脚印终点（黄圈）
    _dx, _dy = cal["door_x"] * 372, cal["door_y"] * 132
    (OUT / "guides.html").write_text(
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<style>html,body{margin:0;background:#0B1220}</style></head><body>'
        '<div style="position:relative;width:1920px;height:680px">'
        f'<img src="{plate_dir.as_uri()}/ending_scene_lit.png" '
        'style="width:100%;height:100%;display:block;object-fit:cover">'
        '<svg viewBox="0 0 372 132" preserveAspectRatio="none" '
        'style="position:absolute;inset:0;width:100%;height:100%">'
        f'<line x1="0" y1="{RU.HORIZON_Y}" x2="372" y2="{RU.HORIZON_Y}" '
        'stroke="#FF5B5B" stroke-width="0.35" stroke-dasharray="2 1.5"/>'
        f'<line x1="{_dx}" y1="0" x2="{_dx}" y2="132" stroke="#3EE08A" '
        'stroke-width="0.35" stroke-dasharray="2 1.5"/>'
        f'<circle cx="{_dx}" cy="{_dy}" r="2.6" fill="none" stroke="#FFD34E" '
        'stroke-width="0.5"/>'
        + RU._scene_props("arrive", on_plate=True) +
        '</svg></div></body></html>', encoding="utf-8")
    pages["guides"] = True

    exe = CHROME if Path(CHROME).is_file() else "chrome"
    for name in list(pages) + ["share"]:
        png = OUT / f"{name}.png"
        # 引导图必须按底图的原始尺寸截（1920×680），否则会被窗口裁掉一半
        size = {"share": "1080,1920", "guides": "1920,680"}.get(
            name, f"{WIDTH},1400")
        subprocess.run([exe, "--headless=new", "--disable-gpu", "--no-first-run",
                        "--no-default-browser-check", "--allow-file-access-from-files",
                        f"--user-data-dir={Path(tempfile.gettempdir()) / ('rc-pl-' + name)}",
                        "--hide-scrollbars", "--virtual-time-budget=3000",
                        f"--window-size={size}",
                        f"--screenshot={png}", (OUT / f"{name}.html").as_uri()],
                       capture_output=True)
        print(f"{name}.png  {'ok' if png.is_file() else 'FAILED'}")

    print(f"\n看这几件事：脚印是否全在雪地里 / 是否走到门口 / 缩小后还看不看得清")
    print(f"产物：{OUT}")
    shutil.rmtree(plate_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
