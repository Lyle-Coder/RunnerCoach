# -*- coding: utf-8 -*-
"""RunnerCoach 隧道客户端（本机侧）。

用法：
    python tools/relay_client.py <中转站URL> [网关地址]

例如：
    python tools/relay_client.py https://xxx.workbuddy.link http://127.0.0.1:8300

职责：长轮询云端中转站取活 → 打到本机 Docker 网关 → 回传响应。
3 个工作线程并发；App 端只需填中转站 URL，本机地址不出网。

隧道密钥注入（代码里没有任何默认值，缺省即报错退出）：
  1) 环境变量 TUNNEL_KEY
  2) 环境变量 RELAY_SECRETS 指向的 JSON 文件
  3) server/relay/secrets.json（已被 .gitignore 忽略）
密钥不得写进源码——远端仓库是公开的。
"""
import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHARE_CODE = ""                               # 中转站分享码（从链接 ?sharecode= 自动提取）
HOP_HEADERS = {"connection", "content-length", "transfer-encoding",
               "keep-alive", "host", "accept-encoding"}
MIN_KEY_LEN = 16


def load_tunnel_key() -> str:
    """按优先级取隧道密钥；取不到返回空串（由调用方报错，不回落内置值）。"""
    key = (os.environ.get("TUNNEL_KEY") or "").strip()
    if key:
        return key
    for path in (os.environ.get("RELAY_SECRETS") or "",
                 os.path.join(_ROOT, "server", "relay", "secrets.json")):
        if not path:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                key = str(json.load(f).get("tunnel_key") or "").strip()
        except (OSError, ValueError):
            continue
        if key:
            return key
    return ""


TUNNEL_KEY = load_tunnel_key()


def tunnel_post(base: str, path: str, payload: dict, timeout: int):
    req = urllib.request.Request(base.rstrip("/") + path, method="POST",
                                 data=json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Tunnel-Key", TUNNEL_KEY)
    if SHARE_CODE:
        req.add_header("X-Share-Code", SHARE_CODE)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _is_text_like(headers: dict) -> bool:
    """按 Content-Type 判断响应体是否文本；无类型时保守按文本处理。"""
    mime = ""
    for k, v in headers.items():
        if k.lower() == "content-type":
            mime = (v or "").strip().lower()
            break
    if not mime:
        return True
    return mime.startswith(("text/", "application/json", "application/javascript",
                            "application/xml", "image/svg"))


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
            raw = r.read()
    except urllib.error.HTTPError as e:
        status = e.code
        headers = dict(e.headers.items())
        raw = e.read()
    except Exception as e:  # noqa: BLE001
        status = 502
        headers = {"Content-Type": "application/json; charset=utf-8"}
        raw = json.dumps({"ok": False, "error": f"本机转发失败：{type(e).__name__}: {e}"},
                         ensure_ascii=False).encode("utf-8")
    if _is_text_like(headers):
        payload = {"id": rid, "status": status, "headers": headers,
                   "body": raw.decode("utf-8", "replace")}
    else:
        # 二进制（图片等）经 base64 过隧道，中转站见 b64 标记还原
        payload = {"id": rid, "status": status, "headers": headers,
                   "body": base64.b64encode(raw).decode("ascii"), "b64": True}
    tunnel_post(base, "/tunnel/response", payload, timeout=30)
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
    if not TUNNEL_KEY:
        print("✗ 未找到隧道密钥，无法连接中转站。\n"
              "  任选其一：\n"
              "    1) 设置环境变量 TUNNEL_KEY=<密钥>\n"
              "    2) 把 {\"tunnel_key\": \"<密钥>\"} 写到 server/relay/secrets.json\n"
              "  密钥必须与中转站（云端 relay/app.py）配置的一致。", file=sys.stderr)
        return 2
    if len(TUNNEL_KEY) < MIN_KEY_LEN:
        print(f"✗ 隧道密钥过短（{len(TUNNEL_KEY)} < {MIN_KEY_LEN}），请重新生成。",
              file=sys.stderr)
        return 2
    print(f"√ 隧道客户端启动\n  中转站 {base}\n  网关   {gateway}\n"
          f"  密钥   {TUNNEL_KEY[:4]}***（长度 {len(TUNNEL_KEY)}）", flush=True)
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
