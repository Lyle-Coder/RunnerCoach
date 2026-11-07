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
import json
import os
import re
import secrets
import socket
import sys
from functools import partial
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import history, ui

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


def _with_fix(doc: str, css: str) -> str:
    """给已有的自包含 HTML（AI 页 / 看板）补一段统一外壳样式，不动它的 DOM。"""
    return doc.replace("</head>", f"<style>{css}</style></head>", 1)


class _Handler(SimpleHTTPRequestHandler):
    server_version = "garmin-coach/0.1"

    # AI 页面与接口：只有本机能调。局域网访问者即便拿到访问码，
    # 也进不了这条通道——它会把健康数据发给模型服务商，还会花钱。
    _AI_PAGES = ("/ai", "/ai.html")

    def _is_loopback(self) -> bool:
        ip = (self.client_address or ("",))[0]
        return ip in ("127.0.0.1", "::1", "localhost")

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        if u.path in self._AI_PAGES:
            if not self._is_loopback():
                return self._json({"ok": False, "error": "AI 页面只允许本机打开"}, 403)
            from . import aiui

            # AI 页有自己的 CSS，只补统一外框与标题节奏，不动它的交互。
            return self._html(_with_fix(aiui.render(), ui.AI_FIX))
        token = getattr(self.server, "token", "")
        if token and not self._gate(u, token):
            return
        # 四个 tab 的页面：同源、只读本地库，App 的 JS 桥照常挂在上面。
        # 放在 gate 之后，局域网带访问码的访问也受同一套保护。
        root = Path(self.directory)
        if u.path == "/view":
            return self._html(history.view_page(parse_qs(u.query)))
        if u.path in ("/history", "/history.html"):
            return self._html(history.history_page(parse_qs(u.query)))
        if u.path in ("/me", "/me.html"):
            return self._html(ui.me_page())
        if u.path in ("/plan", "/plan.html"):
            return self._html(ui.plan_page(parse_qs(u.query)))
        if u.path in ("/files", "/files.html"):
            return self._html(ui.files_page(root))
        if u.path.startswith("/basemap/"):
            return self._basemap(u.path)
        if u.path in ("", "/", "/index.html"):
            return self._send_index()
        return super().do_GET()

    def _basemap(self, path: str) -> None:
        """活动图底图：同源提供 `data/basemaps/*.png`。

        App 的 WebView 加载的是 http 页面，页面里 `file://` 图片会被拦，
        所以底图必须走同一个 127.0.0.1 服务。文件名串与尺寸/版本绑定，
        内容不变，给长缓存；路径先 resolve 再校验是否还在缓存目录内。
        """
        from . import basemap as BM

        name = path[len("/basemap/"):]
        if not re.fullmatch(r"[A-Za-z0-9._-]+\.png", name):
            return self._json({"ok": False, "error": "底图名不合法"}, 404)
        try:
            p = (BM.CACHE / name).resolve()
            p.relative_to(BM.CACHE.resolve())
        except (ValueError, OSError):
            return self._json({"ok": False, "error": "底图不存在"}, 404)
        if not p.is_file():
            return self._json({"ok": False, "error": "底图不存在"}, 404)
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=2592000")
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass

    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        if not u.path.startswith("/ai/"):
            return self._json({"ok": False, "error": "不支持的接口"}, 404)
        if not self._is_loopback():
            return self._json({"ok": False, "error": "AI 接口只允许本机调用"}, 403)

        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > 512 * 1024:          # 对话历史再长也不该超过这个量级
            return self._json({"ok": False, "error": "请求过大"}, 413)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise ValueError("body 必须是对象")
        except Exception as e:
            return self._json({"ok": False, "error": f"参数不合法：{e}"}, 400)

        action = u.path[len("/ai/"):].strip("/") or "status"
        from . import aiapi

        try:
            out = aiapi.dispatch(action, payload)
        except Exception as e:      # dispatch 内部已兜底，这里只防万一
            out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return self._json(out, 200 if out.get("ok") else 400)

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _html(self, html: str, code: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

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
        # 首页不再是产物文件列表：改成「今日」概览，文件清单降级到 /files。
        self._html(ui.home_page())

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
