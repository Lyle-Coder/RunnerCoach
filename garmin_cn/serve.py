# -*- coding: utf-8 -*-
"""手机看板服务：把 output/ 只读地挂到局域网上，手机浏览器直接看。

为什么需要这一层
----------------
看板本身就是自包含 HTML，问题只在于"怎么递到手机上"：微信传文件要手动找路径、
数据线要驱动、放到网盘又等于把健康数据交给第三方。同一 WiFi 下起一个只读服务，
手机打开一串地址就能看——iPhone 也不用装任何东西。

三条硬规则
----------
1. **只读**：只服务 output/ 下已生成的文件，没有任何写入口。
2. **默认只听 127.0.0.1**：本机看不需要暴露端口；`--lan` 才绑 0.0.0.0。
3. **绑 0.0.0.0 必须有访问码**：output/ 里是健康数据、体重、轨迹。
   同一 WiFi 不等于可信网络（宿舍、共享办公、咖啡馆都算）。

访问码用 cookie 记住，地址栏里的 `?k=` 会立刻重定向掉，
避免手机截图或投屏时把令牌一起带出去。
"""
from __future__ import annotations

import datetime as dt
import hmac
import os
import secrets
import socket
import sys
from functools import partial
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SERVE_EXT = {".html", ".htm", ".png", ".jpg", ".jpeg", ".mp4", ".json", ".csv"}
LIST_EXT = {".html", ".htm", ".png", ".mp4"}   # 手机上值得点开的；json/csv 只服务不列


def _lan_ip() -> str:
    """取本机在局域网里的地址；UDP connect 不会真的发包。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{max(1, n // 1024)} KB"


def _title(name: str) -> tuple[str, str, str]:
    """文件名 → (标题, 组, 类型)。同一天既有 PNG 又有 HTML 时靠标题区分用途。"""
    stem, _, ext = name.rpartition(".")
    ext = "." + ext.lower()
    kind = "网页" if ext in (".html", ".htm") else ("视频" if ext == ".mp4" else "图片")

    if name == "latest.html":
        return "今天的看板", "今日", kind
    if stem == "demo":
        return "演示看板（仿真数据）", "今日", kind
    if stem == "card":
        return "最新一图流", "今日", kind
    if stem == "act":
        return "最新活动图", "今日", kind
    if stem.startswith("dashboard-"):
        return f"看板 {stem[10:]}", "看板", kind
    if stem.startswith("card-"):
        tail = "" if ext == ".png" else " · 网页"
        return f"一图流 {stem[5:]}{tail}", "图", kind
    if stem.startswith("act-"):
        day = stem[4:].rsplit("-", 1)[0]
        label = "活动图" if ext == ".png" else "活动详情"
        return f"{label} {day}", "活动", kind
    if stem.startswith("motion-"):
        label = "回放视频" if ext == ".mp4" else "轨迹回放"
        return f"{label} {stem[7:]}", "活动", kind
    if stem.startswith("plan-"):
        return f"105K 计划 {stem[5:]}", "计划", kind
    return name, "其它", kind


def _index_html(root: Path) -> str:
    files = []
    for p in root.iterdir():
        if not p.is_file() or p.suffix.lower() not in LIST_EXT:
            continue
        if p.name.startswith("_") or p.stat().st_size == 0:
            continue
        files.append(p)
    files.sort(key=lambda x: (x.name != "latest.html", -x.stat().st_mtime))

    order = {"今日": 0, "看板": 1, "图": 2, "活动": 3, "计划": 4, "其它": 5}
    groups: dict[str, list] = {}
    for p in files[:60]:
        title, g, kind = _title(p.name)
        bucket = groups.setdefault(g, [])
        if len(bucket) < 12:
            bucket.append((title, p, kind))

    blocks = []
    for g in sorted(groups, key=lambda k: order.get(k, 9)):
        rows = []
        for title, p, kind in groups[g]:
            st = p.stat()
            when = dt.datetime.fromtimestamp(st.st_mtime).strftime("%m-%d %H:%M")
            rows.append(
                f'<a class="row" href="{p.name}">'
                f'<span class="n">{title}</span>'
                f'<span class="m">{when} · {kind} · {_size(st.st_size)}</span></a>'
            )
        blocks.append(f'<h2>{g}</h2>{"".join(rows)}')

    if not blocks:
        blocks.append('<p class="empty">还没有生成任何产物。<br>'
                      '跑一次同步或看板，这里就会出现文件。</p>')

    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#F4F6F2">
<title>RunnerCoach · 手机</title>
<style>
:root{{--bg:#F4F6F2;--card:#fff;--line:#E3E7DF;--ink:#1B1F1A;--muted:#6E7669;--green:#1D9E75}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
  -webkit-text-size-adjust:100%;padding:20px 14px 40px;max-width:560px;margin:0 auto}}
h1{{font-size:19px;font-weight:600;margin:0 0 4px}}
.sub{{font-size:12.5px;color:var(--muted);margin-bottom:20px}}
h2{{font-size:11.5px;font-weight:600;color:var(--muted);letter-spacing:1.1px;margin:20px 0 8px}}
.row{{display:flex;flex-direction:column;gap:3px;background:var(--card);
  border:1px solid var(--line);border-radius:12px;padding:13px 14px;margin-bottom:8px;
  text-decoration:none;color:inherit;-webkit-tap-highlight-color:transparent}}
.row:active{{background:#EDF2EA}}
.n{{font-size:15px;font-weight:500}}
.m{{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}}
.empty{{font-size:13px;color:var(--muted);line-height:1.7}}
code{{background:#E9EDE5;border-radius:4px;padding:1px 5px;font-size:12px}}
.foot{{margin-top:26px;font-size:11.5px;color:#9AA294;line-height:1.8;
  border-top:1px solid var(--line);padding-top:12px}}
</style></head><body>
<h1>RunnerCoach</h1>
<div class="sub">只读 · 数据不出这台电脑</div>
{"".join(blocks)}
<div class="foot">点开即是完整看板，可左右滑动。<br>
关掉电脑上的窗口，这些链接立刻失效。</div>
</body></html>"""


class _Handler(SimpleHTTPRequestHandler):
    server_version = "garmin-coach/0.1"

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        token = getattr(self.server, "token", "")
        if token and not self._gate(u, token):
            return
        if u.path in ("", "/", "/index.html"):
            return self._send_index()
        return super().do_GET()

    def _gate(self, u, token: str) -> bool:
        """返回 True 表示放行；False 表示已经自己响应过了。"""
        got = (parse_qs(u.query).get("k") or [""])[0]
        if got and hmac.compare_digest(got, token):
            # 种 cookie 后重定向掉 ?k=：地址栏不留令牌，截图/投屏不会带走
            self.send_response(302)
            self.send_header("Set-Cookie", f"gc={token}; Path=/; HttpOnly; SameSite=Lax")
            self.send_header("Location", u.path or "/")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return False
        c = SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except Exception:
            pass
        if "gc" in c and hmac.compare_digest(c["gc"].value, token):
            return True
        body = (
            '<!doctype html><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<body style="font-family:system-ui;padding:36px 20px;line-height:1.8;color:#333">'
            "<h3 style=\"margin:0 0 8px\">需要访问码</h3>"
            "请用电脑上打印出来的<b>完整地址</b>（带 <code>?k=…</code>）打开，"
            "或用手机扫那个地址。只输 IP 是进不来的。</body>"
        ).encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def _send_index(self) -> None:
        body = _index_html(Path(self.directory)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self) -> None:
        if self.path.rsplit(".", 1)[-1].lower() in ("html", "htm"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        # 只记路径，不记查询串（里面可能带令牌）
        sys.stderr.write("  %s %s\n" % (self.address_string(), self.path.split("?")[0]))


def run_server(root: Path, lan: bool = False, port: int = 8765,
               open_: bool = False, token: str | None = None) -> int:
    root = Path(root)
    if not root.is_dir():
        print(f"× 目录不存在：{root}")
        return 2

    host = "0.0.0.0" if lan else "127.0.0.1"
    if lan and not token:
        token = secrets.token_urlsafe(5)

    handler = partial(_Handler, directory=str(root))
    srv = None
    for p in range(port, port + 12):
        try:
            srv = ThreadingHTTPServer((host, p), handler)
            port = p
            break
        except OSError:
            continue
    if srv is None:
        print(f"× {port} 起连续 12 个端口都被占用，换个 --port")
        return 3
    srv.token = token or ""  # type: ignore[attr-defined]
    srv.daemon_threads = True

    q = f"?k={token}" if token else ""
    print("√ 只读看板服务已启动（关掉本窗口即停止）")
    print(f"   本机  http://127.0.0.1:{port}/{q}")
    if lan:
        ip = _lan_ip()
        print(f"   手机  http://{ip}:{port}/{q}   ← 手机连同一个 WiFi")
        print(f"   访问码 {token}")
        print("   ⚠ 同一 WiFi 下的人拿到这串地址就能看，用完请关掉")
    else:
        print("   手机看：加 --lan 重新启动，会给出局域网地址")
    print()

    if open_:
        from .sysopen import open_any

        open_any(f"http://127.0.0.1:{port}/{q}")

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
    return 0
