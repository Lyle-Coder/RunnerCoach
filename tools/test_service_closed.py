# -*- coding: utf-8 -*-
"""维护态回归：两处关闭文案一致 + 占位服务/网关开关都真的挡住业务接口。

用法： python tools/test_service_closed.py
不需要 Docker、不需要公网，全部在本机随机端口真打，结束自动清理子进程。
"""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAINT = ROOT / "tools" / "maintenance_server.py"
GATEWAY = ROOT / "server" / "app.py"
MESSAGE_RE = re.compile(r'^SERVICE_CLOSED_MESSAGE\s*=\s*"([^"]+)"', re.M)

_results = []


def ok(name, cond, detail=""):
    if not cond:
        raise AssertionError(f"{name} — {detail}")
    _results.append(name)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def call(base, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data,
                                 method="POST" if body is not None else "GET")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def wait_ready(base, timeout=12):
    end = time.time() + timeout
    while time.time() < end:
        try:
            call(base, "/health")
            return True
        except Exception:
            time.sleep(0.2)
    return False


def start(script, cwd, port, extra=None):
    env = dict(os.environ)
    env.update({"PORT": str(port), "PYTHONDONTWRITEBYTECODE": "1"})
    env.update(extra or {})
    return subprocess.Popen([sys.executable, str(script)], cwd=str(cwd),
                            env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def message_from(path):
    m = MESSAGE_RE.search(path.read_text(encoding="utf-8"))
    assert m, f"{path.name} 里找不到 SERVICE_CLOSED_MESSAGE"
    return m.group(1)


def check_message():
    a, b = message_from(GATEWAY), message_from(MAINT)
    ok("网关与维护态文案逐字一致", a == b, f"{a!r} != {b!r}")
    return a


def check_maintenance(msg):
    port = free_port()
    p = start(MAINT, ROOT, port, {"RC_SITE_DIR": str(ROOT / "site"),
                                  "RC_ENV_FILE": str(ROOT / "server" / ".env"),
                                  "RC_MAINT_PID_FILE": str(Path(tempfile.gettempdir())
                                                           / f"rc_maint_test_{port}.pid")})
    base = f"http://127.0.0.1:{port}"
    try:
        assert wait_ready(base), "维护服务没起来"
        s, _, b = call(base, "/health")
        j = json.loads(b)
        ok("维护态 /health 标注 closed", s == 200 and j.get("service") == "closed"
           and j.get("ok") is False, j)
        ok("维护态 /health 逐项显示服务关闭",
           all(j.get(k) is False for k in ("email", "chat", "sms", "pay")), j)

        s, _, b = call(base, "/v1/client/config")
        j = json.loads(b)
        ok("配置下发仍可用且不破坏内置地址",
           s == 200 and j.get("service") == "closed"
           and str(j.get("platform_base", "")).startswith("https://"), j)

        s, ctype, b = call(base, "/")
        html = b.decode("utf-8", "replace")
        ok("看板首页带『服务已关闭』横幅",
           s == 200 and "text/html" in ctype
           and "rc-service-closed" in html and "服务已关闭" in html
           and "RunnerCoach" in html, ctype)

        s, ctype, b = call(base, "/card.png")
        ok("一图流仍原样可读",
           s == 200 and "image/png" in ctype
           and b == (ROOT / "site" / "card.png").read_bytes(), ctype)

        for path, body in [("/v1/sms/send", {"account": "a@b.com"}),
                           ("/v1/auth", {"account": "a@b.com", "code": "123456"}),
                           ("/v1/chat/completions", {"messages": []}),
                           ("/v1/orders", {"amount": 5}),
                           ("/v1/redeem", {"code": "X"}),
                           ("/admin/topup", {"amount": 5})]:
            s, _, b = call(base, path, body)
            j = json.loads(b)
            ok("业务接口立即拒绝：" + path,
               s == 400 and j.get("service") == "closed"
               and msg in j.get("error", ""), (s, j))
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


def check_gateway_closed(msg):
    tmp = tempfile.TemporaryDirectory()
    port = free_port()
    env = {"DB_PATH": str(Path(tmp.name) / "closed.db"),
           "RC_SITE_DIR": str(ROOT / "site"),
           "RC_SERVICE_CLOSED": "1"}
    p = start(GATEWAY, ROOT / "server", port, env)
    base = f"http://127.0.0.1:{port}"
    try:
        assert wait_ready(base), "网关（关闭态）没起来"
        s, _, b = call(base, "/health")
        j = json.loads(b)
        ok("网关关闭态 /health 标注 closed",
           s == 200 and j.get("ok") is False and j.get("service") == "closed", j)
        for path, body in [("/v1/sms/send", {"account": "a@b.com"}),
                           ("/v1/chat/completions", {"messages": []}),
                           ("/v1/orders", {"amount": 5})]:
            s, _, b = call(base, path, body)
            j = json.loads(b)
            ok("网关关闭态挡住：" + path,
               s == 400 and msg in j.get("error", ""), (s, j))
        s, _, b = call(base, "/")
        ok("网关关闭态看板仍可看", s == 200 and b"RunnerCoach" in b, s)
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
        tmp.cleanup()


def check_gateway_open():
    """开关缺省时行为不变，避免把正式服务一起关死。"""
    tmp = tempfile.TemporaryDirectory()
    port = free_port()
    env = {"DB_PATH": str(Path(tmp.name) / "open.db"),
           "RC_SITE_DIR": str(ROOT / "site"),
           "RC_SERVICE_CLOSED": "",
           # 这一组只验证开关不误伤，强制 dev 模式，绝不触发真实发信
           "SMS_PROVIDER": "dev", "SMTP_HOST": "", "SMTP_USER": ""}
    p = start(GATEWAY, ROOT / "server", port, env)
    base = f"http://127.0.0.1:{port}"
    try:
        assert wait_ready(base), "网关（正常态）没起来"
        s, _, b = call(base, "/health")
        j = json.loads(b)
        ok("未设开关时 /health 仍为 ok", s == 200 and j.get("ok") is True
           and j.get("service") == "open", j)
        s, _, b = call(base, "/v1/sms/send", {"account": "a@b.com"})
        ok("未设开关时业务接口照常处理", s != 400
           or "平台服务已关闭" not in b.decode("utf-8", "replace"), (s, b[:120]))
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
        tmp.cleanup()


def main():
    msg = check_message()
    check_maintenance(msg)
    check_gateway_closed(msg)
    check_gateway_open()
    for name in _results:
        print("  PASS " + name)
    print(f"SERVICE_CLOSED_OK  {len(_results)} 组")
    return 0


if __name__ == "__main__":
    sys.exit(main())
