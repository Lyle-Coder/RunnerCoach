# -*- coding: utf-8 -*-
"""RunnerCoach 维护态占位服务（关闭邮箱认证 / 大模型 / 支付时顶在 8300）。

用途：真实网关容器 `docker compose stop` 之后，公网中转站仍会把请求经隧道
转发到 127.0.0.1:8300。如果端口直接空着，请求会挂满 120 秒才报 504，
看板每次也要白等 5 秒才回落快照，App 只会显示「连不上」而不是「已关闭」。

这个服务只做三件事，全部立即应答，绝不碰数据库、SMTP 或上游模型：
  1) /health、/v1/*、/admin/*  -> 明确返回「平台服务已关闭」；
  2) /、/index.html、/dashboard -> 本机看板快照 + 顶部「服务已关闭」横幅；
  3) /card.png                 -> 本机一图流快照（原样返回）。

它不读取任何密钥，不写数据库，也不改变公网地址或分享码；隧道客户端
保持原样继续指向 8300 即可。恢复服务时停掉本进程并 `docker compose start`。

启动： python tools/maintenance_server.py
      PORT=8300（默认）、RC_SITE_DIR / RC_ENV_FILE 可覆盖路径。
"""
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 必须与 server/app.py 的 SERVICE_CLOSED_MESSAGE 逐字一致（tools/test_service_closed.py 会校验）
SERVICE_CLOSED_MESSAGE = "平台服务已关闭（维护中）：邮箱认证、大模型问答与在线充值均已停用"

_ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = Path(os.environ.get("RC_SITE_DIR") or (_ROOT / "site"))
ENV_FILE = Path(os.environ.get("RC_ENV_FILE") or (_ROOT / "server" / ".env"))
PID_FILE = Path(os.environ.get("RC_MAINT_PID_FILE")
                or (_ROOT / "server" / "data" / "maintenance.pid"))
PORT = int(os.environ.get("PORT", "8300"))

BANNER = (
    '<div id="rc-service-closed" style="max-width:1120px;margin:18px auto -6px;'
    'padding:13px 18px;background:#FAE7E5;border:1px solid #E9C4C1;'
    'border-left:4px solid #C2453F;border-radius:12px;color:#1B1F1A;'
    'font-size:13px;line-height:1.7">'
    '<strong style="color:#C2453F;font-size:14px">服务已关闭</strong>'
    '<span style="margin-left:10px">邮箱认证、AI 教练问答与在线充值已停用（维护中）。'
    '下方看板是最后一次本地生成的快照，数据不再更新。</span></div>'
)

FALLBACK_PAGE = (
    '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    '<title>RunnerCoach · 服务已关闭</title></head>'
    '<body style="margin:0;background:#F4F6F2;font-family:-apple-system,'
    'BlinkMacSystemFont,\'Segoe UI\',\'PingFang SC\',sans-serif;color:#1B1F1A">'
    + BANNER.replace('margin:18px auto -6px', 'margin:40px auto 0') +
    '<div style="max-width:1120px;margin:18px auto;padding:18px;background:#fff;'
    'border:1px solid #E3E7DF;border-radius:16px">看板快照尚未生成，'
    '本机运行 <code>daily.bat</code> 后即可显示。</div></body></html>'
)

OPEN_PATHS = ("/health", "/v1/client/config", "/", "/index.html",
              "/dashboard", "/card.png")


def _env_value(name: str, default: str = "") -> str:
    """只读两个公开字段（平台地址 / 分享码），不解析任何密钥。"""
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            if k.strip() == name:
                return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return default


def closed_health() -> dict:
    return {"ok": False, "service": "closed", "model": None,
            "email": False, "chat": False, "sms": False, "pay": False,
            "message": SERVICE_CLOSED_MESSAGE}


def closed_error() -> dict:
    return {"ok": False, "service": "closed", "error": SERVICE_CLOSED_MESSAGE}


def served_config() -> dict:
    base = _env_value("RC_PUBLIC_BASE").rstrip("/")
    code = _env_value("RC_SHARE_CODE")
    return {"ok": True, "platform_base": base, "platform_share_code": code,
            "source": "server" if base else "builtin", "service": "closed",
            "email": False, "chat": False}


def dashboard_html() -> bytes:
    page = SITE_DIR / "index.html"
    if not page.is_file():
        return FALLBACK_PAGE.encode("utf-8")
    html = page.read_text(encoding="utf-8", errors="replace")
    if "rc-service-closed" not in html:
        html, n = re.subn(r"(<body[^>]*>)", lambda m: m.group(1) + BANNER,
                          html, count=1, flags=re.I)
        if not n:
            html = BANNER + html
    return html.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "rc-maintenance/0.1"
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, ctype: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def _json(self, obj: dict, status: int = 200) -> None:
        self._send(status, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def log_message(self, fmt, *args) -> None:
        print("  %s %s" % (self.address_string(), self.path.split("?")[0]),
              flush=True)

    def _dispatch(self, method: str = "GET") -> None:
        # 先读干净请求体再回绝，否则隧道客户端可能撞上 BrokenPipe 收不到响应
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if 0 < n <= 2 * 1024 * 1024:
                self.rfile.read(n)
        except (ValueError, OSError):
            pass
        path = self.path.split("?")[0]
        if path == "/health":
            return self._json(closed_health())
        if path == "/v1/client/config":
            return self._json(served_config())
        if path in ("/", "/index.html", "/dashboard"):
            return self._send(200, "text/html; charset=utf-8", dashboard_html())
        if path == "/card.png":
            card = SITE_DIR / "card.png"
            if card.is_file():
                return self._send(200, "image/png", card.read_bytes())
            return self._json(closed_error(), 400)
        # 其余所有接口（含 /admin、/v1/*）一律快速拒绝，不进任何业务逻辑
        return self._json(closed_error(), 400)

    def do_GET(self):     self._dispatch("GET")     # noqa: E701
    def do_POST(self):    self._dispatch("POST")    # noqa: E701
    def do_PUT(self):     self._dispatch("PUT")     # noqa: E701
    def do_PATCH(self):   self._dispatch("PATCH")   # noqa: E701
    def do_DELETE(self):  self._dispatch("DELETE")  # noqa: E701


def main() -> None:
    # 自己登记 PID，避免由父进程代写时写到已经退出的进程号
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print(f"√ RunnerCoach 维护态 :{PORT}（邮箱认证 / 大模型 / 支付均已关闭）", flush=True)
    print(f"  看板快照: {SITE_DIR}", flush=True)
    try:
        srv.serve_forever()
    finally:
        try:
            if PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
                PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n维护态服务已停止", flush=True)
        sys.exit(0)
