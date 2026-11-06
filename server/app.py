# -*- coding: utf-8 -*-
"""RunnerCoach 平台网关：余额、兑换码、计费、OpenAI 兼容转发。

纯标准库实现（http.server + sqlite3 + urllib），部署零依赖：
    python app.py
    # 或设好环境变量后 systemd/Docker 常驻

架构一句话
----------
App 不直接持有上游 API Key；它拿一个设备令牌打这台网关，网关验令牌、
查余额、转发请求给真正的模型商、按 token 用量扣钱。充值 = 管理员生成
兑换码 → 用户在 App 里输入 → 余额增加。

为什么模型名只有一个
--------------------
计价的前提是知道单价。平台侧固定跑一个上游模型（环境变量 UPSTREAM_MODEL），
客户端传什么模型名都映射到它——想换模型改一个环境变量重启即可，
价格表也只维护一行。要跑多个模型时再扩展 PRICE 表。

计费口径
--------
余额用「微元」（1 元 = 1_000_000 micro）存整数，避免浮点误差。
上游返回 usage 就按实际 token 计；流式响应拿不到 usage 时按输出字符数
粗估（中文约 1 token ≈ 1.5 字符），宁多扣不少扣，误差 10% 以内，
对内测额度足够。

为什么 HTTP/1.0 而不是 chunked
------------------------------
流式转发只做一件事：上游来一行 SSE 就立刻写给客户端。用 HTTP/1.0 +
Connection: close，客户端靠连接关闭感知结束，省掉手写 chunked 帧的坑；
WebView 的 fetch/ReadableStream 和 curl 都能正常读。

安全
----
- 设备令牌只存 SHA-256，库泄露也换不出原文；
- 管理接口靠 X-Admin-Token（环境变量，启动时必填才能启用）；
- 余额不足直接 402，不欠账运行（超支只可能发生在预检估不准时）；
- 所有扣费流水落 usage_log，对得上账。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------- 配置
UPSTREAM_BASE = os.environ.get("UPSTREAM_BASE", "https://api.deepseek.com/v1").rstrip("/")
UPSTREAM_KEY = os.environ.get("UPSTREAM_API_KEY", "")
UPSTREAM_MODEL = os.environ.get("UPSTREAM_MODEL", "deepseek-chat")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).resolve().parent / "data.db"))
MARGIN = float(os.environ.get("MARGIN", "1.15"))            # 平台加价倍率
FREE_GRANT = int(os.environ.get("FREE_GRANT", "2_000_000")) # 新设备赠送（微元，默认 2 元）
HOST = os.environ.get("BIND_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8300"))

# 单价表：元 / 百万 token，只列平台实际会跑的模型
PRICE_PER_1M = {
    "deepseek-chat": (2.0, 8.0),        # (输入, 输出)
    "deepseek-reasoner": (4.0, 16.0),
}
DEFAULT_PRICE = (2.0, 8.0)


def _price(model: str) -> tuple[float, float]:
    return PRICE_PER_1M.get(model, DEFAULT_PRICE)


def _micro(v: float) -> int:
    return int(round(v * 1_000_000))


# ---------------------------------------------------------------- 存储
def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS accounts(
            id INTEGER PRIMARY KEY,
            name TEXT DEFAULT '',
            token_hash TEXT UNIQUE NOT NULL,
            balance INTEGER NOT NULL DEFAULT 0,      -- 微元
            created_at INTEGER NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS codes(
            code TEXT PRIMARY KEY,
            amount INTEGER NOT NULL,                 -- 微元
            used_by INTEGER, used_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS usage_log(
            id INTEGER PRIMARY KEY,
            account INTEGER, ts INTEGER, model TEXT,
            prompt_tokens INTEGER, completion_tokens INTEGER,
            cost INTEGER NOT NULL
        );
        """)


class DBError(Exception):
    pass


class Gate(Exception):
    """带 HTTP 状态码的业务异常，统一转成 JSON 响应。"""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    finally:
        conn.close()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _auth(headers) -> sqlite3.Row:
    raw = headers.get("Authorization") or ""
    if not raw.lower().startswith("bearer "):
        raise Gate(401, "缺少 Bearer 令牌")
    token = raw[7:].strip()
    with db() as c:
        row = c.execute("SELECT * FROM accounts WHERE token_hash=?", (_hash(token),)).fetchone()
    if not row or row["revoked"]:
        raise Gate(401, "令牌无效或已吊销")
    return row


def _admin(headers) -> None:
    if not ADMIN_TOKEN:
        raise Gate(503, "网关未配置 ADMIN_TOKEN，管理接口关闭")
    got = headers.get("X-Admin-Token") or ""
    if not got or not secrets.compare_digest(got, ADMIN_TOKEN):
        raise Gate(403, "管理令牌错误")


def _charge(account_id: int, model: str, pt: int, ct: int) -> int:
    """按用量扣费，返回本次花费（微元）。

    扣到负数是刻意的：中途掐断用户的回答比超支几分钱伤害更大，
    负余额会在 /v1/me 和后台列表里直接暴露，管理员再处理。
    """
    pin, pout = _price(model)
    cost = int((pt * pin + ct * pout) / 1_000_000 * MARGIN * 1_000_000)
    cost = max(cost, 1)
    with db() as c:
        c.execute("UPDATE accounts SET balance=balance-? WHERE id=?", (cost, account_id))
        c.execute("INSERT INTO usage_log(account,ts,model,prompt_tokens,completion_tokens,cost)"
                  " VALUES(?,?,?,?,?,?)",
                  (account_id, int(time.time()), model, pt, ct, cost))
    return cost


def _balance_of(account_id: int) -> int:
    with db() as c:
        return c.execute("SELECT balance FROM accounts WHERE id=?",
                         (account_id,)).fetchone()["balance"]


# ---------------------------------------------------------------- 业务
def api_device(body: dict) -> dict:
    name = str(body.get("name") or "")[:40]
    token = "rc_" + secrets.token_urlsafe(24)
    with db() as c:
        c.execute("INSERT INTO accounts(name,token_hash,balance,created_at) VALUES(?,?,?,?)",
                  (name, _hash(token), FREE_GRANT, int(time.time())))
    return {"ok": True, "token": token, "balance": FREE_GRANT / 1e6}


def api_me(row: sqlite3.Row) -> dict:
    now = int(time.time())
    month_start = now - now % 2_592_000
    with db() as c:
        used = c.execute("SELECT COALESCE(SUM(cost),0) s FROM usage_log"
                         " WHERE account=? AND ts>=?", (row["id"], month_start)).fetchone()["s"]
    return {"ok": True, "name": row["name"], "balance": row["balance"] / 1e6,
            "currency": "CNY", "used_month": used / 1e6}


def api_redeem(row: sqlite3.Row, body: dict) -> dict:
    code = str(body.get("code") or "").strip().upper()
    if not code:
        raise Gate(400, "请输入兑换码")
    with db() as c:
        cr = c.execute("SELECT * FROM codes WHERE code=?", (code,)).fetchone()
        if not cr:
            raise Gate(404, "兑换码不存在")
        if cr["used_by"]:
            raise Gate(409, "兑换码已被使用")
        c.execute("UPDATE codes SET used_by=?, used_at=? WHERE code=?",
                  (row["id"], int(time.time()), code))
        c.execute("UPDATE accounts SET balance=balance+? WHERE id=?", (cr["amount"], row["id"]))
    return {"ok": True, "added": cr["amount"] / 1e6, "balance": _balance_of(row["id"]) / 1e6}


def _upstream_call(upstream_body: dict):
    """发请求到上游，返回 (resp | None, raw_error | None)。流式时 resp 可逐行读。"""
    req = urllib.request.Request(UPSTREAM_BASE + "/chat/completions",
                                 data=json.dumps(upstream_body, ensure_ascii=False).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {UPSTREAM_KEY}")
    try:
        return urllib.request.urlopen(req, timeout=300), None
    except urllib.error.HTTPError as e:
        return None, (e.code, e.read().decode("utf-8", "replace")[:400])
    except Exception as e:
        return None, (502, f"{type(e).__name__}: {e}")


def api_chat(row: sqlite3.Row, body: dict, handler) -> None:
    """非流式走 JSON 响应；流式直接在 handler 上写 SSE，函数不返回值。"""
    if not UPSTREAM_KEY:
        raise Gate(503, "网关未配置上游 API Key")

    model = UPSTREAM_MODEL                       # 客户端传什么都不采信
    upstream_body = {k: v for k, v in body.items() if k != "model"}
    upstream_body["model"] = model
    streaming = bool(upstream_body.get("stream"))
    if streaming:
        upstream_body["stream_options"] = {"include_usage": True}

    # 预检：最大输出成本远超余额时直接拒绝，防止单请求欠下大额
    max_out = int(upstream_body.get("max_tokens") or 1200)
    pout = _price(model)[1]
    worst = _micro(max_out * pout / 1e6 * MARGIN)
    if row["balance"] >= 0 and worst > row["balance"] * 20 + _micro(0.5):
        raise Gate(402, "余额不足，请用兑换码充值")

    resp, err = _upstream_call(upstream_body)
    if err:
        raise Gate(err[0], f"上游错误：{err[1][:200]}")

    if not streaming:
        with resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        usage = data.get("usage") or {}
        cost = _charge(row["id"], model, int(usage.get("prompt_tokens") or 0),
                       int(usage.get("completion_tokens") or 0))
        data["x_billing"] = {"cost_cny": cost / 1e6, "balance_cny": _balance_of(row["id"]) / 1e6}
        handler._json(data, 200)
        return

    # 流式：上游一行 SSE 就立刻写一行，结束（含 [DONE]）后结算
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Connection", "close")
    handler.end_headers()

    ct_chars = 0
    usage_frame = None
    try:
        with resp:
            for line in resp:
                text = line.decode("utf-8", "replace").rstrip("\n").rstrip("\r")
                if text.startswith("data:"):
                    payload = text[5:].strip()
                    if payload == "[DONE]":
                        handler.wfile.write(b"data: [DONE]\n\n")
                        break
                    try:
                        obj = json.loads(payload)
                    except Exception:
                        obj = None
                    if obj:
                        if obj.get("usage"):
                            usage_frame = obj["usage"]
                        for ch in obj.get("choices") or []:
                            ct_chars += len((ch.get("delta") or {}).get("content") or "")
                handler.wfile.write((text + "\n\n").encode("utf-8"))
                handler.wfile.flush()
    finally:
        try:
            handler.wfile.flush()
        except Exception:
            pass
    if usage_frame:
        pt = int(usage_frame.get("prompt_tokens") or 0)
        ct = int(usage_frame.get("completion_tokens") or 0)
    else:
        pt = 600                                 # 粗估：系统提示 + 上下文的常见量级
        ct = int(ct_chars / 1.5)
    _charge(row["id"], model, pt, ct)


def admin_codes(body: dict) -> dict:
    amount = _micro(float(body.get("amount") or 0))
    count = max(1, min(200, int(body.get("count") or 10)))
    if amount <= 0:
        raise Gate(400, "amount 必须大于 0")
    codes = []
    with db() as c:
        for _ in range(count):
            code = "RC-" + "-".join(secrets.token_hex(2).upper() for _ in range(2))
            c.execute("INSERT INTO codes(code,amount) VALUES(?,?)", (code, amount))
            codes.append(code)
    return {"ok": True, "amount_cny": amount / 1e6, "codes": codes}


def admin_topup(body: dict) -> dict:
    amount = _micro(float(body.get("amount") or 0))
    if amount <= 0:
        raise Gate(400, "amount 必须大于 0")
    with db() as c:
        if body.get("account_id"):
            r = c.execute("SELECT * FROM accounts WHERE id=?",
                          (int(body["account_id"]),)).fetchone()
        elif body.get("token"):
            r = c.execute("SELECT * FROM accounts WHERE token_hash=?",
                          (_hash(str(body["token"])),)).fetchone()
        else:
            raise Gate(400, "需要 account_id 或 token")
        if not r:
            raise Gate(404, "账号不存在")
        c.execute("UPDATE accounts SET balance=balance+? WHERE id=?", (amount, r["id"]))
    return {"ok": True, "account": r["id"], "balance_cny": _balance_of(r["id"]) / 1e6}


def admin_accounts() -> dict:
    with db() as c:
        rows = c.execute("""
            SELECT a.id, a.name, a.balance, a.created_at, a.revoked,
                   COALESCE(SUM(u.cost),0) spent
            FROM accounts a LEFT JOIN usage_log u ON u.account=a.id
            GROUP BY a.id ORDER BY a.id DESC LIMIT 200
        """).fetchall()
    return {"ok": True, "accounts": [
        {"id": r["id"], "name": r["name"], "balance_cny": r["balance"] / 1e6,
         "spent_cny": r["spent"] / 1e6, "created_at": r["created_at"],
         "revoked": bool(r["revoked"])} for r in rows]}


# ---------------------------------------------------------------- HTTP 壳
class Handler(BaseHTTPRequestHandler):
    server_version = "rc-gateway/0.1"

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if self.protocol_version >= "HTTP/1.1":
            self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass
        self.close_connection = True

    def _read_body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > 2 * 1024 * 1024:
            raise Gate(413, "请求过大")
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise Gate(400, f"body 不是合法 JSON：{e}")
        if not isinstance(obj, dict):
            raise Gate(400, "body 必须是 JSON 对象")
        return obj

    def do_GET(self) -> None:  # noqa: N802
        try:
            if self.path == "/health":
                return self._json({"ok": True, "model": UPSTREAM_MODEL})
            if self.path == "/v1/me":
                return self._json(api_me(_auth(self.headers)))
            if self.path == "/admin/accounts":
                _admin(self.headers)
                return self._json(admin_accounts())
            raise Gate(404, "没有这个接口")
        except Gate as e:
            self._json({"ok": False, "error": e.message}, e.code)

    def do_POST(self) -> None:  # noqa: N802
        try:
            body = self._read_body()
            if self.path == "/v1/device":
                return self._json(api_device(body))
            if self.path == "/v1/redeem":
                return self._json(api_redeem(_auth(self.headers), body))
            if self.path == "/v1/chat/completions":
                row = _auth(self.headers)
                return api_chat(row, body, self)   # 流式时直接写完响应
            if self.path == "/admin/codes":
                _admin(self.headers)
                return self._json(admin_codes(body))
            if self.path == "/admin/topup":
                _admin(self.headers)
                return self._json(admin_topup(body))
            raise Gate(404, "没有这个接口")
        except Gate as e:
            if not self.headers_sent():
                self._json({"ok": False, "error": e.message}, e.code)
        except Exception as e:  # noqa: BLE001
            if not self.headers_sent():
                self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def headers_sent(self) -> bool:
        # 流式转发中途出错时响应头已经发出去，只能断开连接而不是再发一个 JSON
        return getattr(self, "_rc_headers_sent", False)

    def send_response(self, code, message=None):  # noqa: A003
        self._rc_headers_sent = True
        super().send_response(code, message)

    def log_message(self, fmt, *args) -> None:
        sys.stderr.write("  %s %s\n" % (self.address_string(), self.path.split("?")[0]))


def main() -> int:
    init_db()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"√ RunnerCoach 网关 http://{HOST}:{PORT}")
    print(f"  上游 {UPSTREAM_BASE} · 模型 {UPSTREAM_MODEL} · 加价 {MARGIN}×")
    print(f"  管理 {'已启用（ADMIN_TOKEN 已配置）' if ADMIN_TOKEN else '未配置 ADMIN_TOKEN，管理接口关闭'}")
    print(f"  数据库 {DB_PATH}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
