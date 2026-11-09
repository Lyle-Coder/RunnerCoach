"""UI 体检：字号、触控目标、横向溢出、禁用文案。

这些指标肉眼看不出来，但直接决定「费不费眼、点不点得中」。以前只能靠截图
对比，人眼判断；现在每次改完样式跑一遍，数字会说话。

    python tools/audit_ui.py                  # 四个页面 × 390 宽
    python tools/audit_ui.py --width 360 430  # 多档宽度（小屏最容易出事）
    python tools/audit_ui.py --json           # 给 CI / selftest 用

只读：渲染到临时文件，用无头浏览器量完就删，不碰 data/ 与 output/。

为什么开 iframe：Chrome headless 的最小布局宽度是 500 px，直接
`--window-size=390` 时浏览器仍按 500 排版，截图只截左半边（README 截图被裁
就是这个原因）。外面套一层固定宽度的 iframe，才能量到真实的 390。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 页面里不该出现的东西：开发信息、内部黑话、原始异常
BANNED = ("未标注强度", "TypeError", "出图缩放", "W2 周", "None", "Traceback")

# 视口宽度。360 是仍然在用的老机型，390 是主流，430 是大屏。
WIDTHS = (360, 390, 430)

MEASURE = """
<script>
function audit() {
  var f = document.querySelector('iframe');
  if (!f || !f.contentDocument) return;
  var d = f.contentDocument, w = f.contentWindow;
  var de = d.documentElement;
  var out = {
    overflow: Math.max(0, de.scrollWidth - de.clientWidth),
    tiny: [],          // <11px 的文本
    clickable: [],     // 可点元素高度 <40
    clipped: [],       // 被 nowrap/ellipsis 截掉的关键数值
    banned: []
  };
  // 字号：走文本节点，避免量到空容器
  var tw = d.createTreeWalker(d.body, NodeFilter.SHOW_TEXT, null);
  var seen = {};
  while (tw.nextNode()) {
    var txt = (tw.currentNode.nodeValue || '').trim();
    if (!txt) continue;
    var el = tw.currentNode.parentElement;
    if (!el) continue;
    var cs = w.getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    var fs = parseFloat(cs.fontSize) || 0;
    if (fs > 0 && fs < 11) {
      var k = fs.toFixed(1) + '|' + txt.slice(0, 18);
      if (!seen[k]) { seen[k] = 1; out.tiny.push({px: fs, text: txt.slice(0, 24)}); }
    }
  }
  // 可点元素：链接/按钮/带 data-act 的容器/表单控件
  var sel = 'a[href],button,[data-act],[onclick],input,select,label.row.pick';
  var nodes = d.querySelectorAll(sel);
  for (var i = 0; i < nodes.length; i++) {
    var e = nodes[i];
    var r = e.getBoundingClientRect();
    var cs2 = w.getComputedStyle(e);
    if (r.width <= 0 || r.height <= 0) continue;
    if (cs2.display === 'none' || cs2.visibility === 'hidden') continue;
    if (r.height < 40) {
      out.clickable.push({
        h: Math.round(r.height), w: Math.round(r.width),
        text: (e.innerText || e.getAttribute('aria-label') || e.className || '')
              .trim().slice(0, 20)
      });
    }
  }
  // 关键数值被截断：.val / .vital .k 里有 scrollWidth 超出的，
  // 说明字太大或单位没拆开（「13.5 km」挤不进三列卡就是这个）
  var vals = d.querySelectorAll('.val');
  for (var vi = 0; vi < vals.length; vi++) {
    var ve = vals[vi];
    if (ve.scrollWidth > ve.clientWidth + 1) {
      out.clipped.push((ve.innerText || '').trim().slice(0, 16));
    }
  }
  var body = (d.body.innerText || '');
  var bannedList = %s;
  for (var j = 0; j < bannedList.length; j++) {
    if (body.indexOf(bannedList[j].slice(0, 2)) >= 0 && body.indexOf(bannedList[j]) >= 0) {
      out.banned.push(bannedList[j]);
    }
  }
  document.body.setAttribute('data-audit', JSON.stringify(out));
}
window.addEventListener('load', function () { setTimeout(audit, 500); });
</script>
"""


def find_browser() -> str | None:
    """Chrome / Edge 都行，先看环境变量再看常见安装路径。"""
    env = os.environ.get("RUNNERCOACH_BROWSER")
    if env and Path(env).is_file():
        return env
    cands = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    for c in cands:
        if Path(c).is_file():
            return c
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    return None


def render_pages(tmp: Path) -> dict[str, Path]:
    """把四个 tab 页渲染成静态 HTML（只读本地库，不联网）。"""
    from garmin_cn import ui

    out = {}
    out["home"] = tmp / "home.html"
    out["home"].write_text(ui.home_page(), encoding="utf-8")
    out["history"] = tmp / "history.html"
    out["history"].write_text(_history(), encoding="utf-8")
    out["plan"] = tmp / "plan.html"
    out["plan"].write_text(ui.plan_page({}), encoding="utf-8")
    out["me"] = tmp / "me.html"
    out["me"].write_text(ui.me_page(), encoding="utf-8")
    return out


def _history() -> str:
    """历史页按月视图：既覆盖列表也覆盖柱状图。"""
    from garmin_cn import history

    try:
        return history.history_page({"mode": ["month"]})
    except Exception:
        return "<html><body>history 渲染失败</body></html>"


def measure(browser: str, page: Path, width: int, tmp: Path) -> dict:
    wrap = tmp / f"wrap-{page.stem}-{width}.html"
    wrap.write_text(
        "<!doctype html><meta charset='utf-8'>"
        "<style>html,body{margin:0;background:#070B12;overflow:hidden}"
        f"iframe{{width:{width}px;height:900px;border:0;display:block}}"
        f"</style><iframe src='{page.resolve().as_uri()}'></iframe>"
        + (MEASURE % json.dumps(list(BANNED), ensure_ascii=False)),
        encoding="utf-8",
    )
    png = tmp / f"shot-{page.stem}-{width}.png"
    try:
        r = subprocess.run(
            [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
             "--allow-file-access-from-files",
             f"--window-size={max(width, 520)},900",
             "--virtual-time-budget=2500",
             f"--screenshot={png}",
             wrap.resolve().as_uri()],
            capture_output=True, timeout=90,
        )
        dom = subprocess.run(
            [browser, "--headless=new", "--disable-gpu", "--allow-file-access-from-files",
             f"--window-size={max(width, 520)},900",
             "--virtual-time-budget=2500",
             "--dump-dom", wrap.resolve().as_uri()],
            capture_output=True, timeout=90,
        ).stdout.decode("utf-8", "replace")
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"error": f"{type(e).__name__}: {e}"}
    if r.returncode != 0 and not png.is_file():
        err = r.stderr.decode("utf-8", "replace")[:200]
        return {"error": f"浏览器退出码 {r.returncode}：{err}"}
    import re

    m = re.search(r'data-audit="([^"]*)"', dom)
    if not m:
        return {"error": "页面没有回报测量结果（脚本未执行完）"}
    raw = m.group(1).replace("&quot;", '"').replace("&amp;", "&")
    try:
        return json.loads(raw)
    except ValueError:
        return {"error": "测量结果不是合法 JSON"}


def contrast(fg: str, bg: str) -> float:
    """WCAG 相对对比度。fg/bg 是 #RRGGBB。"""
    def lum(h: str) -> float:
        h = h.lstrip("#")
        c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

    a, b = lum(fg), lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def token_colors() -> dict[str, str]:
    """从 ui.py 的 CSS 里抠出 :root 令牌，用来算对比度（不重复写色值）。"""
    import re

    from garmin_cn import ui

    out = {}
    m = re.search(r":root\{(.+?)\}", ui.CSS, re.S)
    if not m:
        return out
    for k, v in re.findall(r"--([a-z0-9-]+)\s*:\s*(#[0-9A-Fa-f]{6})", m.group(1)):
        out[k] = v
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="UI 体检（字号/触控/溢出/文案）")
    ap.add_argument("--width", type=int, nargs="*", default=list(WIDTHS))
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    browser = find_browser()
    if not browser:
        print("× 没找到 Chrome / Edge，先装一个或设 RUNNERCOACH_BROWSER=<路径>")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="rc-audit-"))
    try:
        pages = render_pages(tmp)
        report = {"browser": browser, "widths": args.width, "pages": {}}
        for name, path in pages.items():
            report["pages"][name] = {}
            for w in args.width:
                report["pages"][name][w] = measure(browser, path, w, tmp)
        cols = token_colors()
        if "faint" in cols and "surface" in cols:
            report["contrast"] = {
                "faint_on_surface": round(contrast(cols["faint"], cols["surface"]), 2),
                "muted_on_surface": round(contrast(cols["muted"], cols["surface"]), 2),
                "ink_on_surface": round(contrast(cols["ink"], cols["surface"]), 2),
            }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"浏览器  {Path(browser).name}")
    print(f"{'页面':10}{'宽':>5}  {'溢出':>4}  {'<11px':>6}  {'矮可点':>6}  {'截断':>4}  禁用词")
    bad = 0
    for name, by_w in report["pages"].items():
        for w, r in by_w.items():
            if r.get("error"):
                print(f"{name:10}{w:>5}  × {r['error']}")
                bad += 1
                continue
            tiny, small_click = len(r["tiny"]), len(r["clickable"])
            clipped = len(r.get("clipped") or [])
            print(f"{name:10}{w:>5}  {r['overflow']:>4}  {tiny:>6}  {small_click:>6}"
                  f"  {clipped:>4}  {'、'.join(r['banned']) if r['banned'] else '—'}")
            if r["overflow"] > 0 or tiny or small_click or clipped or r["banned"]:
                bad += 1
    if report.get("contrast"):
        c = report["contrast"]
        print(f"\n对比度  faint {c['faint_on_surface']}:1 · muted {c['muted_on_surface']}:1"
              f" · ink {c['ink_on_surface']}:1（AA 要求 4.5:1）")

    # 明细：只列第一档宽度，避免刷屏
    for name, by_w in report["pages"].items():
        r = by_w.get(args.width[0]) or {}
        if r.get("error"):
            continue
        if r.get("tiny"):
            sample = " / ".join(f"{t['px']:.1f}px「{t['text']}」" for t in r["tiny"][:4])
            print(f"\n{name} <11px 示例：{sample}（共 {len(r['tiny'])} 处）")
        if r.get("clipped"):
            print(f"{name} 数值被截：{'、'.join(r['clipped'])}")
        if r.get("clickable"):
            sample = " / ".join(f"{t['h']}×{t['w']}「{t['text']}」" for t in r["clickable"][:4])
            print(f"{name} 矮可点示例：{sample}（共 {len(r['clickable'])} 处）")

    print(f"\n{'√ 全部达标' if bad == 0 else f'× {bad} 项不达标'}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
