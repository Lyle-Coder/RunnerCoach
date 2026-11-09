"""Generate RunnerCoach launcher icons.

Design: deep-navy rounded square, glowing blue stadium track,
white-blue heartbeat pulse across the middle, green circle = start,
red square = finish. Palette aligned with card.py (one-image board).

Outputs:
  android/app/src/main/res/mipmap-{m,h,xh,xxh,xxxh}dpi/ic_launcher.png
  android/app/src/main/res/drawable-{m,h,xh,xxh,xxxh}dpi/ic_launcher_fg.png
  docs/icon/runnercoach-icon.svg            (master, 130-unit viewBox)
  docs/icon/runnercoach-icon-1024.png / -512.png / -192.png
"""
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
RES = ROOT / "android" / "app" / "src" / "main" / "res"
OUT_ICON = ROOT / "docs" / "icon"
# 图形只认 garmin_cn/brand.py 这一份：开屏、网页角标、一图流都从那里取，
# 启动器图标不能再自己维护一套坐标。
sys.path.insert(0, str(ROOT))
from garmin_cn.brand import (  # noqa: E402
    NAVY, filter_def, glow_layer, core_layer,
)

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# ---------------------------------------------------------------- artwork --
# 图形与网页/开屏共用 garmin_cn/brand.py；这里只负责把它渲染成各档 PNG。
GLOW_ID = "glow"
ART_GLOW = glow_layer(GLOW_ID)
ART_CORE = core_layer()
FILTER = filter_def(GLOW_ID)


def svg_legacy() -> str:
    """Full launcher tile: rounded navy square + glowing artwork."""
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 130 130">
  <defs>{FILTER}</defs>
  <rect width="130" height="130" rx="29" fill="{NAVY}"/>
  {ART_GLOW}
  {ART_CORE}
</svg>'''


def svg_foreground() -> str:
    """Adaptive-icon foreground: transparent, artwork inside the 66/108 safe zone."""
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 130 130">
  <defs>{FILTER}</defs>
  <g transform="translate(31.2 31.2) scale(0.52)">
    {ART_GLOW}
    {ART_CORE}
  </g>
</svg>'''


def svg_plain() -> str:
    """Master artwork without the rounded tile (for docs/README)."""
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 130 130">
  <defs>{FILTER}</defs>
  <rect width="130" height="130" fill="{NAVY}"/>
  {ART_GLOW}
  {ART_CORE}
</svg>'''


# ---------------------------------------------------------------- render ---

def find_chrome() -> str:
    for path in CHROME_CANDIDATES:
        if pathlib.Path(path).exists():
            return path
    for name in ("chrome", "msedge"):
        try:
            out = subprocess.run(["where", name], capture_output=True, text=True)
            first = (out.stdout or "").strip().splitlines()
            if first:
                return first[0]
        except OSError:
            pass
    raise SystemExit("chrome/edge not found")


def shoot(chrome: str, svg: str, size: int, out_png: pathlib.Path) -> None:
    html = ('<html><body style="margin:0;background:transparent">'
            f'<div style="width:{size}px;height:{size}px">{svg}'
            '</div></body></html>')
    # SVG has no width/height attrs; wrap scale via CSS transform is messy.
    # Instead give the svg intrinsic size by string substitution.
    html = html.replace("<svg ", f'<svg width="{size}" height="{size}" ', 1)
    with tempfile.TemporaryDirectory() as td:
        page = pathlib.Path(td) / "page.html"
        page.write_text(html, encoding="utf-8")
        shot = pathlib.Path(td) / "shot.png"
        subprocess.run(
            [chrome,
             "--headless=new",
             "--disable-gpu",
             "--hide-scrollbars",
             "--force-device-scale-factor=1",
             "--default-background-color=00000000",
             f"--screenshot={shot}",
             f"--window-size={size},{size}",
             page.as_uri()],
            capture_output=True, timeout=60, check=True)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        out_png.write_bytes(shot.read_bytes())
    print(f"ok {out_png.relative_to(ROOT)}  {size}px")


def main() -> None:
    chrome = find_chrome()
    legacy = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
    for dpi, size in legacy.items():
        shoot(chrome, svg_legacy(), size, RES / f"mipmap-{dpi}" / "ic_launcher.png")
    fg = {"mdpi": 108, "hdpi": 162, "xhdpi": 216, "xxhdpi": 324, "xxxhdpi": 432}
    for dpi, size in fg.items():
        shoot(chrome, svg_foreground(), size,
              RES / f"drawable-{dpi}" / "ic_launcher_fg.png")

    OUT_ICON.mkdir(parents=True, exist_ok=True)
    (OUT_ICON / "runnercoach-icon.svg").write_text(svg_plain(), encoding="utf-8")
    print("ok docs/icon/runnercoach-icon.svg")
    for size in (1024, 512, 192):
        shoot(chrome, svg_plain(), size, OUT_ICON / f"runnercoach-icon-{size}.png")


if __name__ == "__main__":
    main()
