# -*- coding: utf-8 -*-
"""RunnerCoach 公网中转站（反向隧道·云端侧）。

纯标准库。部署在公网（WorkBuddy sites 沙箱），职责：
- 接收 App 的 HTTPS 请求，挂入待办队列；
- 用户电脑上的隧道客户端用共享密钥长轮询取活，回传网关的响应；
- App 只见到这个公网地址，家里电脑的 IP / 端口完全不暴露；
- 兼任「每日看板」入口：/ 与 /card.png 优先穿隧道取本机最新生成结果，
  拿不到时回落云端内置快照（index.html / card.png，随发布更新）。

安全边界：
- /tunnel/* 必须带 X-Tunnel-Key（只有本人电脑能取活/回传）；
- /admin/*、/v1/device 在云端直接拒绝——公网上匿名设备可白嫖
  FREE_GRANT，管理接口更不该出内网；
- 请求体上限 2MB，超时不收。

隧道密钥注入（代码里没有任何默认值，缺省即拒绝启动）：
  1) 环境变量 TUNNEL_KEY
  2) 环境变量 RELAY_SECRETS 指向的 JSON 文件
  3) 与本文件同目录的 secrets.json（已被 .gitignore 忽略，见 secrets.example.json）
密钥不得写进源码——远端仓库是公开的。

单请求等待上限 120s（AI 对话较慢），取活长轮询 45s。
"""
import base64
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
MIN_KEY_LEN = 16


def load_tunnel_key() -> str:
    """按优先级取隧道密钥；取不到就退出，绝不回落到内置值。"""
    key = (os.environ.get("TUNNEL_KEY") or "").strip()
    if key:
        source = "环境变量 TUNNEL_KEY"
    else:
        key, source = "", ""
        for path in (os.environ.get("RELAY_SECRETS") or "",
                     os.path.join(_HERE, "secrets.json")):
            if not path:
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    key = str(json.load(f).get("tunnel_key") or "").strip()
            except (OSError, ValueError):
                continue
            if key:
                source = path
                break
    if not key:
        raise SystemExit(
            "✗ 未配置隧道密钥，拒绝启动。\n"
            "  任选其一：\n"
            "    1) 设置环境变量 TUNNEL_KEY=<密钥>\n"
            "    2) 把 {\"tunnel_key\": \"<密钥>\"} 写到本目录 secrets.json\n"
            "  参考同目录 secrets.example.json；可用 "
            "python -c \"import secrets;print(secrets.token_urlsafe(24))\" 生成。")
    if len(key) < MIN_KEY_LEN:
        raise SystemExit(f"✗ 隧道密钥过短（{len(key)} < {MIN_KEY_LEN}），请重新生成。")
    return key


TUNNEL_KEY = load_tunnel_key()
MAX_BODY = 2 * 1024 * 1024
JOB_WAIT = 45                                 # 客户端长轮询等待（秒）
RESP_WAIT = 120                               # 公网请求等响应上限（秒）
FRESH_WAIT = 5                                # 看板等本机最新结果的秒数，超时用快照
STATIC_MAP = {"/": "index.html",              # 看板路径 -> 云端快照文件名
              "/index.html": "index.html",
              "/card.png": "card.png"}
BLOCKED = ("/admin", "/v1/device",              # 不允许经公网访问的网关路径
           "/secrets", "/.env", "/app.py")       # 本机文件，绝不外泄
HOP_HEADERS = {"connection", "content-length", "transfer-encoding",
               "keep-alive", "host", "accept-encoding"}

_pending = []                                 # [(id, job)]
_responses = {}                               # id -> (status, headers, body_text)
_lock = threading.Lock()
_cv = threading.Condition(_lock)


class RelayHandler(BaseHTTPRequestHandler):
    server_version = "rc-relay/0.1"
    protocol_version = "HTTP/1.1"

    # ---------- 基础 ----------
    def _read_body(self) -> bytes:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > MAX_BODY:
            return b"__too_large__"
        return self.rfile.read(n) if n else b""

    def _send(self, status: int, headers: dict, body: bytes) -> None:
        self.send_response(status)
        for k, v in headers.items():
            if k.lower() not in HOP_HEADERS:
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _send_json(self, obj, status: int = 200) -> None:
        self._send(status, {"Content-Type": "application/json; charset=utf-8"},
                   json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def log_message(self, fmt, *args) -> None:
        print("  %s %s" % (self.address_string(), self.path.split("?")[0]),
              flush=True)

    # ---------- 隧道侧 ----------
    def _tunnel_authorized(self) -> bool:
        import secrets as _s
        got = self.headers.get("X-Tunnel-Key") or ""
        return bool(got) and _s.compare_digest(got, TUNNEL_KEY)

    def handle_fetch(self) -> None:
        if not self._tunnel_authorized():
            return self._send_json({"error": "tunnel key 无效"}, 403)
        deadline = time.time() + JOB_WAIT
        while time.time() < deadline:
            with _cv:
                if _pending:
                    job_id, job = _pending.pop(0)
                    return self._send_json(job)
                _cv.wait(2)
        self._send_json({"id": "", "method": "", "path": ""}, 200)   # 空轮询心跳

    def handle_response(self, body: bytes) -> None:
        if not self._tunnel_authorized():
            return self._send_json({"error": "tunnel key 无效"}, 403)
        try:
            data = json.loads(body.decode("utf-8"))
            rid = data["id"]
            raw = data.get("body") or ""
            if data.get("b64"):                 # 二进制响应（图片等）经 base64 传输
                body_bytes = base64.b64decode(raw)
            else:
                body_bytes = str(raw).encode("utf-8", "replace")
            _responses[rid] = (int(data.get("status") or 502),
                               data.get("headers") or {}, body_bytes)
            while len(_responses) > 256:        # 兜底后迟到的孤儿响应，防堆积
                _responses.pop(next(iter(_responses)))
        except Exception:
            return self._send_json({"error": "bad response payload"}, 400)
        with _cv:
            _cv.notify_all()
        self._send_json({"ok": True})

    # ---------- 公网侧 ----------
    def _serve_snapshot(self, name: str) -> bool:
        """回云端内置的看板快照；文件不存在返回 False。"""
        p = os.path.join(_HERE, name)
        if not os.path.isfile(p):
            return False
        with open(p, "rb") as f:
            body = f.read()
        ctype = "image/png" if name.endswith(".png") else "text/html; charset=utf-8"
        self._send(200, {"Content-Type": ctype}, body)
        return True

    def _dashboard(self, path: str, snapshot: str) -> None:
        """看板：优先穿隧道取本机最新生成的结果（daily.bat 跑完即更新），
        FRESH_WAIT 秒内拿不到就回落云端快照——电脑关机也能看昨天的。"""
        rid = uuid.uuid4().hex
        with _cv:
            _pending.append((rid, {"id": rid, "method": "GET", "path": path,
                                   "headers": {}, "body": ""}))
            _cv.notify_all()
        deadline = time.time() + FRESH_WAIT
        while time.time() < deadline:
            with _cv:
                if rid in _responses:
                    status, rhdrs, rbody = _responses.pop(rid)
                    if status == 200 and rbody:
                        ctype = (rhdrs.get("Content-Type")
                                 or rhdrs.get("content-type")
                                 or ("image/png" if snapshot.endswith(".png")
                                     else "text/html; charset=utf-8"))
                        return self._send(200, {"Content-Type": ctype}, rbody)
                    break                       # 网关明确报错（如还没生成），走快照
                _cv.wait(0.3)
        if self._serve_snapshot(snapshot):
            return
        self._send_json({"ok": False, "error": "看板快照缺失且隧道未连通"}, 503)

    def forward(self, method: str) -> None:
        path = self.path
        if method == "GET":
            key = path.split("?")[0]
            if key in STATIC_MAP:
                # 看板 / 探活：优先最新、快照兜底，不进 120s 长等队列
                return self._dashboard(key, STATIC_MAP[key])
        if any(path.startswith(p) for p in BLOCKED):
            return self._send_json({"ok": False,
                                    "error": "该接口仅限内网使用"}, 403)
        body = self._read_body()
        if body == b"__too_large__":
            return self._send_json({"ok": False, "error": "请求过大"}, 413)
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP_HEADERS}
        rid = uuid.uuid4().hex
        with _cv:
            _pending.append((rid, {"id": rid, "method": method, "path": path,
                                   "headers": headers,
                                   "body": body.decode("utf-8", "replace")}))
            _cv.notify_all()
        deadline = time.time() + RESP_WAIT
        while time.time() < deadline:
            with _cv:
                if rid in _responses:
                    status, rhdrs, rbody = _responses.pop(rid)
                    return self._send(status, rhdrs, rbody)
                _cv.wait(1)
        self._send_json({"ok": False, "error": "网关响应超时（隧道未连通或网关过慢）"}, 504)

    # ---------- HTTP 动词 ----------
    def _dispatch(self, method: str) -> None:  # noqa: C901
        try:
            if self.path.startswith("/tunnel/fetch") and method == "POST":
                return self.handle_fetch()
            if self.path.startswith("/tunnel/response") and method == "POST":
                return self.handle_response(self._read_body())
            if self.path == "/__relay/health":
                return self._send_json({"ok": True, "role": "relay"})
            return self.forward(method)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            try:
                self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            except Exception:
                pass

    def do_GET(self):    self._dispatch("GET")       # noqa: E701
    def do_POST(self):   self._dispatch("POST")      # noqa: E701
    def do_PUT(self):    self._dispatch("PUT")       # noqa: E701
    def do_PATCH(self):  self._dispatch("PATCH")     # noqa: E701
    def do_DELETE(self): self._dispatch("DELETE")    # noqa: E701


def main() -> None:
    port = int(os.environ.get("PORT", "8400"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), RelayHandler)
    srv.daemon_threads = True
    print(f"√ RunnerCoach 中转站 :{port}（隧道密钥已从外部注入，长度 {len(TUNNEL_KEY)}）",
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
