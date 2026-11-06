# -*- coding: utf-8 -*-
"""RunnerCoach 公网中转站（反向隧道·云端侧）。

纯标准库。部署在公网（WorkBuddy sites 沙箱），职责：
- 接收 App 的 HTTPS 请求，挂入待办队列；
- 用户电脑上的隧道客户端用共享密钥长轮询取活，回传网关的响应；
- App 只见到这个公网地址，家里电脑的 IP / 端口完全不暴露。

安全边界：
- /tunnel/* 必须带 X-Tunnel-Key（只有本人电脑能取活/回传）；
- /admin/*、/v1/device 在云端直接拒绝——公网上匿名设备可白嫖
  FREE_GRANT，管理接口更不该出内网；
- 请求体上限 2MB，超时不收。

单请求等待上限 120s（AI 对话较慢），取活长轮询 45s。
"""
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TUNNEL_KEY = "IXuZvrG1s49zzjOM8DY2-cJJ"      # 与本机 tools/relay_client.py 保持一致
MAX_BODY = 2 * 1024 * 1024
JOB_WAIT = 45                                 # 客户端长轮询等待（秒）
RESP_WAIT = 120                               # 公网请求等响应上限（秒）
BLOCKED = ("/admin", "/v1/device")            # 不允许经公网访问的网关路径
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
            _responses[rid] = (int(data.get("status") or 502),
                               data.get("headers") or {},
                               data.get("body") or "")
        except Exception:
            return self._send_json({"error": "bad response payload"}, 400)
        with _cv:
            _cv.notify_all()
        self._send_json({"ok": True})

    # ---------- 公网侧 ----------
    def forward(self, method: str) -> None:
        path = self.path
        if method == "GET" and path.split("?")[0] in ("", "/"):
            # 探活/浏览器直接访问根路径：快速返回，不进转发队列
            return self._send_json({"ok": True, "role": "relay",
                                    "hint": "RunnerCoach 网关中转站，App 里填本地址即可"})
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
                    return self._send(status, rhdrs, rbody.encode("utf-8"))
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
    import os
    port = int(os.environ.get("PORT", "8400"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), RelayHandler)
    srv.daemon_threads = True
    print(f"√ RunnerCoach 中转站 :{port}（隧道密钥已内置）", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
