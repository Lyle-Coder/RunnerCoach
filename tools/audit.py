"""版面自检：把生成的 HTML 灌进无头浏览器，读回每个区块的真实高度与溢出情况。

用处：出图后不靠肉眼也能判断「有没有内容被裁掉」「哪块撑破了卡片」。
用法：python tools/audit.py output/act-YYYY-MM-DD-<activity_id>.html
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

CHROME = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

PROBE = """
<script>
window.addEventListener('load', function(){
  var out = {sections:{}, overflow:[], vh: window.innerHeight, sh: document.documentElement.scrollHeight};
  document.querySelectorAll('.page > *').forEach(function(el, i){
    var r = el.getBoundingClientRect();
    var key = (el.className || el.tagName) + '#' + i;
    out.sections[key] = [Math.round(r.top), Math.round(r.height),
                         el.scrollHeight > el.clientHeight + 2 ? 'CLIP' : 'ok'];
  });
  document.querySelectorAll('.card,.zc,.trk,.lp,.tw,.ld,.hero,.zblock').forEach(function(el){
    if (el.scrollHeight > el.clientHeight + 2) {
      out.overflow.push((el.className||'') + ' sh=' + el.scrollHeight + ' ch=' + el.clientHeight);
    }
  });
  var last = document.querySelector('.foot');
  if (last) out.foot_bottom = Math.round(last.getBoundingClientRect().bottom);
  document.title = 'AUDIT' + JSON.stringify(out);
});
</script>
"""


def find_browser() -> str | None:
    for p in CHROME:
        if Path(p).exists():
            return p
    return None


def audit(html_path: Path, size: str = "1080,2020") -> dict:
    html = html_path.read_text(encoding="utf-8")
    tmp = html_path.with_suffix(".audit.html")
    tmp.write_text(html.replace("</body>", PROBE + "</body>"), encoding="utf-8")
    browser = find_browser()
    if not browser:
        raise RuntimeError("找不到 Chrome / Edge")
    proc = subprocess.run(
        [browser, "--headless=new", "--disable-gpu", f"--window-size={size}",
         "--virtual-time-budget=1500", "--dump-dom", tmp.resolve().as_uri()],
        capture_output=True, timeout=120,
    )
    dom = proc.stdout.decode("utf-8", "ignore")
    m = re.search(r"AUDIT(\{.*?\})</title>", dom, re.S)
    tmp.unlink(missing_ok=True)
    if not m:
        raise RuntimeError("没拿到审计数据：\n" + dom[:400])
    return json.loads(m.group(1))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    size = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--size=")),
                "1080,2020")
    if args:
        target = Path(args[0])
    else:
        # 不带参数时取 output/ 里最新的一张活动图，避免在代码里写死本机路径或活动 ID
        cands = sorted((Path(__file__).resolve().parent.parent / "output").glob("act-*.html"))
        if not cands:
            raise SystemExit("用法：python tools/audit.py <path/to/act-*.html>")
        target = cands[-1]
    data = audit(target, size)
    print(f"视口 {data['vh']}px｜文档 {data['sh']}px｜foot 底 {data.get('foot_bottom')}px")
    print("--- 区块 ---")
    for k, v in data["sections"].items():
        print(f"  top={v[0]:>5}  h={v[1]:>4}  {v[2]:<4} {k}")
    if data["overflow"]:
        print("--- 溢出 ---")
        for o in data["overflow"]:
            print("  !", o)
    else:
        print("--- 溢出：无 ---")
