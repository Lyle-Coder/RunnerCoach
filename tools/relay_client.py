# -*- coding: utf-8 -*-
"""RunnerCoach 隧道客户端（本机侧）。

用法：
    python tools/relay_client.py <中转站URL> [网关地址]

例如：
    python tools/relay_client.py https://xxx.workbuddy.link http://127.0.0.1:8300

职责：长轮询云端中转站取活 → 打到本机 Docker 网关 → 回传响应。
3 个工作线程并发；App 端只需填中转站 URL，本机地址不出网。
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request

TUNNEL_KEY = "IXuZvrG1s49zzjOM8DY2-cJJ"       # 与 server/relay/app.py 保持一致
SHARE_CODE = ""                               # 中转站分享码（从链接 ?sharecode= 自动提取）
HOP_HEADERS = {"connection", "content-length", "transfer-encoding",
               "keep-alive", "host", "accept-encoding"}


def tunnel_post(base: str, path: str, payload: dict, timeout: int):
    req = urllib.request.Request(base.rstrip("/") + path, method="POST",
                                 data=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Tunnel-Key", TUNNEL_KEY)
    if SHARE_CODE:
        req.add_header("X-Share-Code", SHARE_CODE)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def run_job(base: str, gateway: str, job: dict) -> None:
    rid = job["id"]
    if not rid:
        return
    url = gateway.rstrip("/") + job["path"]
    req = urllib.request.Request(url, method=job["method"] or "GET")
    for k, v in (job.get("headers") or {}).items():
        if k.lower() not in HOP_HEADERS:
            req.add_header(k, v)
    body = (job.get("body") or "").encode("utf-8")
    try:
        with urllib.request.urlopen(req, body if body else None, timeout=300) as r:
            status = r.status
            headers = dict(r.headers.items())
            text = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status = e.code
        headers = dict(e.headers.items())
        text = e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        status, headers, text = 502, {"Content-Type": "application/json; charset=utf-8"}, \
            json.dumps({"ok": False, "error": f"本机转发失败：{type(e).__name__}: {e}"},
                       ensure_ascii=False)
    tunnel_post(base, "/tunnel/response",
                {"id": rid, "status": status, "headers": headers, "body": text},
                timeout=30)
    print(f"  {job['method']} {job['path']} -> {status}", flush=True)


def worker(base: str, gateway: str, idx: int) -> None:
    while True:
        try:
            job = tunnel_post(base, "/tunnel/fetch", {}, timeout=JOB_HTTP_TIMEOUT)
            run_job(base, gateway, job)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                print(f"[worker{idx}] 隧道密钥被拒（{e.code}），检查 TUNNEL_KEY 是否与中转站一致",
                      file=sys.stderr, flush=True)
                time.sleep(10)
            else:
                time.sleep(3)
        except Exception:  # noqa: BLE001
            time.sleep(3)    # 网络抖动，静默重试


JOB_HTTP_TIMEOUT = 60     # 长轮询本身 45s，留余量

def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    global SHARE_CODE
    base = sys.argv[1]
    if "?" in base:
        head, _, query = base.partition("?")
        for seg in query.split("&"):
            k, _, v = seg.partition("=")
            if k.lower() == "sharecode" and v:
                SHARE_CODE = v
        base = head
    gateway = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8300"
    print(f"√ 隧道客户端启动\n  中转站 {base}\n  网关   {gateway}\n  密钥   已配置", flush=True)
    threads = [threading.Thread(target=worker, args=(base, gateway, i), daemon=True)
               for i in range(3)]
    for t in threads:
        t.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
