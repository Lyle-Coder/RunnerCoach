# -*- coding: utf-8 -*-
"""RunnerCoach 平台网关：账号、余额、兑换码、计费、OpenAI 兼容转发。

纯标准库实现（http.server + sqlite3 + urllib），部署零依赖：
    python app.py
    # 或设好环境变量后 systemd/Docker 常驻（见 docker-compose.yml）

架构一句话
----------
App 不直接持有上游 API Key；它用手机号或邮箱验证码登录拿到设备令牌，打这台网关，
网关验令牌、查余额、转发请求给真正的模型商、按 token 用量扣钱。
充值 = 管理员生成兑换码 → 用户在 App 里输入 → 余额增加。

账号与数据安全（防「充值丢失」）
--------------------------------
- 用户以手机号或邮箱注册，设备令牌只是登录态：卸载重装 / 换手机后重新登录，
  余额原样找回；一个账号可绑多台设备，后台可吊销任意设备。
- 余额不是裸数字，而是 append-only 流水（grant/redeem/consume/adjust，
  每笔带 balance_after）推导出来的，列里的 balance 只是缓存；
  管理端 /admin/rebuild 可随时用流水重建，账永远对得上。
- /admin/export 导出全量 JSON、/admin/db 直接下载 SQLite 快照——
  定期异地备份，服务器没了也能恢复。

验证码通道（手机号 / 邮箱二选一，按账号格式自动路由）
--------------------------------------------------------
- 邮箱（推荐，默认）：SMTP_HOST/SMTP_USER/SMTP_PASS 配好即走邮箱发信，
  QQ/163 等个人邮箱开 SMTP 授权码即可，发信免费；不配 SMTP 时进入
  开发模式，验证码打印到日志并随响应回显（仅内测）。
- 手机号：SMS_PROVIDER 三选一
  dev     （默认）验证码打印到服务端日志并随响应返回，仅用于本地/内测
  aliyun  阿里云短信（需 SMS_ACCESS_KEY_ID / SMS_ACCESS_KEY_SECRET /
          SMS_SIGN_NAME / SMS_TEMPLATE_CODE）——按条计费，仅在必须用
          手机号注册时才开
验证码 6 位、5 分钟有效、限尝试 5 次；发送限频：同号 60 秒 1 条、
每天 10 条，同 IP 每天 30 条——验证码通道不能被刷。

计费口径
--------
余额用「微元」（1 元 = 1_000_000 micro）存整数，避免浮点误差。
上游返回 usage 就按实际 token 计；流式响应拿不到 usage 时按输出字符数
粗估（中文约 1 token ≈ 1.5 字符），宁多扣不少扣，误差 10% 以内。

安全
----
- 设备令牌只存 SHA-256，库泄露也换不出原文；
- 管理接口靠 X-Admin-Token（环境变量，启动时必填才能启用）；
- 余额不足直接 402，不欠账运行（超支只可能发生在预检估不准时）；
- 全部余额变动走流水表，对得上账。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
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
FREE_GRANT = int(os.environ.get("FREE_GRANT", "2_000_000")) # 新用户赠送（微元，默认 2 元）
HOST = os.environ.get("BIND_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8300"))
# 每日看板静态目录（site/index.html、site/card.png，由 daily.bat 生成）
SITE_DIR = Path(os.environ.get("RC_SITE_DIR",
                               str(Path(__file__).resolve().parent.parent / "site")))

SMS_PROVIDER = os.environ.get("SMS_PROVIDER", "dev")        # dev / aliyun
SMS_KEY_ID = os.environ.get("SMS_ACCESS_KEY_ID", "")
SMS_KEY_SECRET = os.environ.get("SMS_ACCESS_KEY_SECRET", "")
SMS_SIGN = os.environ.get("SMS_SIGN_NAME", "")
SMS_TEMPLATE = os.environ.get("SMS_TEMPLATE_CODE", "")

# ── 在线支付（可选）：未配置时 /v1/orders 返回 503，兑换码通道不受影响 ──
# demo 模式用于测试完整下单→到账→扣费链路，admin/orders/pay 模拟到账
PAY_PROVIDER = os.environ.get("PAY_PROVIDER", "")            # '' / demo / wechat_native
WXPAY_MCHID = os.environ.get("WXPAY_MCHID", "")              # 商户号
WXPAY_APPID = os.environ.get("WXPAY_APPID", "")              # 公众号/开放平台 appid
WXPAY_APIV3_KEY = os.environ.get("WXPAY_APIV3_KEY", "")      # APIv3 密钥（32 位）
WXPAY_CERT_SERIAL = os.environ.get("WXPAY_CERT_SERIAL", "")  # 商户证书序列号
WXPAY_KEY_PATH = os.environ.get("WXPAY_PRIVATE_KEY_PATH", "")  # apiclient_key.pem 路径
WXPAY_NOTIFY_URL = os.environ.get("WXPAY_NOTIFY_URL", "")    # 回调地址，须 https
ORDER_EXPIRE = int(os.environ.get("ORDER_EXPIRE", "1800"))   # 订单有效期（秒）

# 邮箱验证码（推荐：零成本，QQ/163 开 SMTP 授权码即可）
SMTP_HOST = os.environ.get("SMTP_HOST", "")                 # 如 smtp.qq.com
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))         # 465=SSL / 587=STARTTLS
SMTP_USER = os.environ.get("SMTP_USER", "")                 # 发件邮箱
SMTP_PASS = os.environ.get("SMTP_PASS", "")                 # SMTP 授权码（不是登录密码）
SMTP_FROM = os.environ.get("SMTP_FROM", "") or SMTP_USER    # 显示的发件人，默认同 SMTP_USER

# 单价表：元 / 百万 token，只列平台实际会跑的模型
# deepseek-flash 取高峰价（闲时减半），按贵的扣不会亏
PRICE_PER_1M = {
    "deepseek-flash": (2.0, 8.0),       # V4.1-Flash：原生多模态（支持读图）
    "deepseek-chat": (2.0, 8.0),
    "deepseek-reasoner": (4.0, 16.0),
}
DEFAULT_PRICE = (2.0, 8.0)

_PHONE_RE = re.compile(r"^1\d{10}$")
# 邮箱校验：够用即可（本地部分 @ 域名，域名至少带一个点），不做完整 RFC 解析
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def account_kind(account: str) -> str | None:
    """识别账号类型：phone / email / None（不认识）。"""
    if _PHONE_RE.match(account):
        return "phone"
    if _EMAIL_RE.match(account) and len(account) <= 254:
        return "email"
    return None


def _price(model: str) -> tuple[float, float]:
    return PRICE_PER_1M.get(model, DEFAULT_PRICE)


def _micro(v: float) -> int:
    return int(round(v * 1_000_000))


def _mask(account: str) -> str:
    """脱敏显示：手机号 138****5678；邮箱 runner***@qq.com。"""
    if len(account) == 11 and account.isdigit():
        return account[:3] + "****" + account[7:]
    if "@" in account:
        local, _, domain = account.partition("@")
        return (local[:2] if local else "*") + "***@" + domain
    return account or "匿名设备"


# ---------------------------------------------------------------- 短信
class SmsError(Exception):
    pass


def _aliyun_sms(phone: str, code: str) -> None:
    """阿里云短信 Dysmsapi RPC 签名请求（纯标准库实现）。"""
    params = {
        "AccessKeyId": SMS_KEY_ID,
        "Action": "SendSms",
        "Format": "JSON",
        "PhoneNumbers": phone,
        "RegionId": "cn-hangzhou",
        "SignName": SMS_SIGN,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": secrets.token_hex(16),
        "SignatureVersion": "1.0",
        "TemplateCode": SMS_TEMPLATE,
        "TemplateParam": json.dumps({"code": code}, separators=(",", ":")),
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "Version": "2017-05-25",
    }
    enc = lambda s: urllib.parse.quote(str(s), safe="-_.~")  # noqa: E731
    query = "&".join(f"{enc(k)}={enc(v)}" for k, v in sorted(params.items()))
    string_to_sign = "GET&%2F&" + enc(query)
    sig = base64_hmac_sha1(SMS_KEY_SECRET + "&", string_to_sign)
    url = f"https://dysmsapi.aliyuncs.com/?Signature={enc(sig)}&{query}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise SmsError(f"短信服务请求失败：{type(e).__name__}: {e}") from None
    if data.get("Code") != "OK":
        raise SmsError(f"短信发送失败：{data.get('Code')} {data.get('Message', '')}".strip())


def base64_hmac_sha1(key: str, msg: str) -> str:
    import base64
    return base64.b64encode(
        hmac.new(key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha1).digest()).decode()


def send_sms(phone: str, code: str) -> None:
    if SMS_PROVIDER == "aliyun":
        if not (SMS_KEY_ID and SMS_KEY_SECRET and SMS_SIGN and SMS_TEMPLATE):
            raise SmsError("短信未配置完整（需要 SMS_ACCESS_KEY_ID/SECRET/SIGN_NAME/TEMPLATE_CODE）")
        _aliyun_sms(phone, code)
    else:  # dev
        print(f"  [sms:dev] {phone} 验证码 {code}", file=sys.stderr, flush=True)


def smtp_ready() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASS)


def send_email(to: str, code: str) -> None:
    """SMTP 发验证码邮件（纯标准库 smtplib）。未配置 SMTP 时走开发模式回显。"""
    if not smtp_ready():
        print(f"  [email:dev] {to} 验证码 {code}", file=sys.stderr, flush=True)
        return
    import smtplib
    from email.header import Header
    from email.mime.text import MIMEText
    from email.utils import formataddr
    body = (
        f"<div style=\"font-family:-apple-system,sans-serif;max-width:420px;margin:0 auto\">"
        f"<h2 style=\"color:#1a7f5e\">RunnerCoach</h2>"
        f"<p>你的登录验证码：</p>"
        f"<p style=\"font-size:30px;font-weight:700;letter-spacing:6px;color:#111\">{code}</p>"
        f"<p style=\"color:#888\">5 分钟内有效。不是本人操作请忽略本邮件。</p></div>")
    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"] = Header(f"RunnerCoach 验证码 {code}", "utf-8")
    msg["From"] = formataddr((str(Header("RunnerCoach", "utf-8")), SMTP_FROM))
    msg["To"] = to
    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as s:
                s.login(SMTP_USER, SMTP_PASS)
                s.sendmail(SMTP_FROM, [to], msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASS)
                s.sendmail(SMTP_FROM, [to], msg.as_string())
    except Exception as e:  # noqa: BLE001
        raise SmsError(f"邮件发送失败：{type(e).__name__}: {e}") from None


# ---------------------------------------------------------------- 存储
def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            phone TEXT UNIQUE NOT NULL,              -- 账号：手机号或邮箱；匿名兼容账号为 ''
            balance INTEGER NOT NULL DEFAULT 0,      -- 微元（流水推导的缓存）
            created_at INTEGER NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS devices(
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            name TEXT DEFAULT '',
            token_hash TEXT UNIQUE NOT NULL,
            created_at INTEGER NOT NULL,
            last_seen INTEGER NOT NULL DEFAULT 0,
            revoked INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS ledger(
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            ts INTEGER NOT NULL,
            kind TEXT NOT NULL,                      -- grant/redeem/consume/adjust
            amount INTEGER NOT NULL,                 -- 微元，带符号
            balance_after INTEGER NOT NULL,
            ref TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(user_id, id);
        CREATE TABLE IF NOT EXISTS codes(
            code TEXT PRIMARY KEY,
            amount INTEGER NOT NULL,
            batch TEXT DEFAULT '',
            used_by_user INTEGER, used_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS usage_log(
            id INTEGER PRIMARY KEY,
            user_id INTEGER, ts INTEGER, model TEXT,
            prompt_tokens INTEGER, completion_tokens INTEGER,
            cost INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY,
            out_trade_no TEXT UNIQUE NOT NULL,   -- 商户订单号，微信侧唯一
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,             -- 微元（实付金额）
            status TEXT NOT NULL DEFAULT 'created',  -- created/paid/expired
            channel TEXT DEFAULT '',             -- wechat_native / demo
            code_url TEXT DEFAULT '',            -- Native 二维码链接
            transaction_id TEXT DEFAULT '',      -- 微信支付单号
            created_at INTEGER NOT NULL,
            paid_at INTEGER,
            ledger_id INTEGER                    -- 到账流水 id（对账用）
        );
        CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, id);
        CREATE TABLE IF NOT EXISTS sms_pending(
            phone TEXT PRIMARY KEY,
            code_hash TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            sent_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sms_log(
            id INTEGER PRIMARY KEY,
            phone TEXT NOT NULL, ip TEXT DEFAULT '', ts INTEGER NOT NULL
        );
        """)


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
    """验证设备令牌，返回 device 行（JOIN users）。"""
    raw = headers.get("Authorization") or ""
    if not raw.lower().startswith("bearer "):
        raise Gate(401, "缺少 Bearer 令牌")
    token = raw[7:].strip()
    with db() as c:
        row = c.execute("""
            SELECT d.id AS dev_id, d.user_id, d.name AS dev_name, d.revoked AS dev_revoked,
                   d.last_seen, u.phone, u.balance, u.revoked
            FROM devices d JOIN users u ON u.id=d.user_id WHERE d.token_hash=?
        """, (_hash(token),)).fetchone()
    if not row or row["dev_revoked"] or row["revoked"]:
        raise Gate(401, "令牌无效或已吊销")
    if int(time.time()) - int(row["last_seen"] or 0) > 3600:
        with db() as c:
            c.execute("UPDATE devices SET last_seen=? WHERE id=?", (int(time.time()), row["dev_id"]))
    return row


def _admin(headers) -> None:
    if not ADMIN_TOKEN:
        raise Gate(503, "网关未配置 ADMIN_TOKEN，管理接口关闭")
    got = headers.get("X-Admin-Token") or ""
    if not got or not secrets.compare_digest(got, ADMIN_TOKEN):
        raise Gate(403, "管理令牌错误")


def _ledger(c, user_id: int, kind: str, amount: int, ref: str = "") -> int:
    """记一笔流水并把 users.balance 缓存同步到新值，返回 balance_after。

    必须在调用方的事务里执行——流水和余额永远同事务落库。
    """
    cur = c.execute("SELECT balance FROM users WHERE id=?", (user_id,)).fetchone()
    after = int(cur["balance"]) + amount
    c.execute("UPDATE users SET balance=? WHERE id=?", (after, user_id))
    c.execute("INSERT INTO ledger(user_id,ts,kind,amount,balance_after,ref) VALUES(?,?,?,?,?,?)",
              (user_id, int(time.time()), kind, amount, after, ref))
    return after


def _charge(user_id: int, model: str, pt: int, ct: int) -> int:
    """按用量扣费，返回本次花费（微元）。

    扣到负数是刻意的：中途掐断用户的回答比超支几分钱伤害更大，
    负余额会在 /v1/me 和后台列表里直接暴露，管理员再处理。
    """
    pin, pout = _price(model)
    cost = int((pt * pin + ct * pout) / 1_000_000 * MARGIN * 1_000_000)
    cost = max(cost, 1)
    with db() as c:
        after = _ledger(c, user_id, "consume", -cost, f"{model} {pt}+{ct}tok")
        c.execute("INSERT INTO usage_log(user_id,ts,model,prompt_tokens,completion_tokens,cost)"
                  " VALUES(?,?,?,?,?,?)", (user_id, int(time.time()), model, pt, ct, cost))
    return cost


def _balance_of(user_id: int) -> int:
    with db() as c:
        return c.execute("SELECT balance FROM users WHERE id=?", (user_id,)).fetchone()["balance"]


# ---------------------------------------------------------------- 验证码（手机号 / 邮箱）
def api_sms_send(body: dict, ip: str) -> dict:
    """发送验证码。account 是手机号或邮箱，按格式自动选通道。

    兼容旧字段名 phone；接口路径 /v1/sms/send 沿用未改，客户端零成本升级。
    """
    raw = str(body.get("account") or body.get("phone") or "").strip()
    account = raw.lower() if "@" in raw else raw
    kind = account_kind(account)
    if kind == "phone":
        if not _PHONE_RE.match(account):
            raise Gate(400, "手机号格式不对（大陆 11 位）")
    elif kind == "email":
        pass                                   # account_kind 已校验
    else:
        raise Gate(400, "请输入正确的手机号或邮箱")
    now = int(time.time())
    with db() as c:
        # 60 秒限频看 sms_log（验证码登录成功会删 sms_pending，不能用它的 sent_at）
        last = c.execute("SELECT MAX(ts) t FROM sms_log WHERE phone=?", (account,)).fetchone()["t"]
        if last and now - last < 60:
            raise Gate(429, "发送太频繁，一分钟后再试")
        day = now - now % 86400
        n_acc = c.execute("SELECT COUNT(*) n FROM sms_log WHERE phone=? AND ts>=?",
                          (account, day)).fetchone()["n"]
        n_ip = c.execute("SELECT COUNT(*) n FROM sms_log WHERE ip=? AND ts>=?",
                         (ip, day)).fetchone()["n"]
        if n_acc >= 10:
            raise Gate(429, "该账号今天验证码次数用完了")
        if n_ip >= 30:
            raise Gate(429, "该网络今天验证码次数用完了")
        code = f"{secrets.randbelow(1_000_000):06d}"
        c.execute("INSERT INTO sms_pending(phone,code_hash,expires_at,sent_at) VALUES(?,?,?,?) "
                  "ON CONFLICT(phone) DO UPDATE SET code_hash=excluded.code_hash,"
                  " expires_at=excluded.expires_at, attempts=0, sent_at=excluded.sent_at",
                  (account, _hash(code), now + 300, now))
        c.execute("INSERT INTO sms_log(phone,ip,ts) VALUES(?,?,?)", (account, ip, now))
    if kind == "email":
        try:
            send_email(account, code)
        except SmsError as e:
            raise Gate(502, str(e)) from None
        dev = not smtp_ready()
        out = {"ok": True, "ttl": 300, "channel": "email"}
        if dev:
            out["dev_code"] = code
            out["notice"] = "开发模式：服务端未配置 SMTP，验证码仅供调试，上线前配置 SMTP_HOST/USER/PASS"
        return out
    try:
        send_sms(account, code)
    except SmsError as e:
        raise Gate(502, str(e)) from None
    out = {"ok": True, "ttl": 300, "channel": "sms"}
    if SMS_PROVIDER == "dev":                    # 仅开发模式回显验证码
        out["dev_code"] = code
        out["notice"] = "开发模式：验证码仅供调试，上线前切换 SMS_PROVIDER=aliyun"
    return out


def api_auth(body: dict, ip: str) -> dict:
    """验证码登录：新账号自动注册并赠送 FREE_GRANT，老号直接登录。

    账号是手机号或邮箱；返回设备令牌——令牌只是登录态，余额在账号上，
    换机重登不丢。
    """
    raw = str(body.get("account") or body.get("phone") or "").strip()
    account = raw.lower() if "@" in raw else raw
    code = str(body.get("code") or "").strip()
    dev_name = str(body.get("device_name") or "")[:40]
    if account_kind(account) is None:
        raise Gate(400, "请输入正确的手机号或邮箱")
    if not code:
        raise Gate(400, "请输入验证码")
    now = int(time.time())
    with db() as c:
        row = c.execute("SELECT * FROM sms_pending WHERE phone=?", (account,)).fetchone()
        if not row or row["expires_at"] < now:
            raise Gate(401, "验证码已过期，请重新发送")
        if row["attempts"] >= 5:
            raise Gate(429, "尝试次数过多，请重新发送验证码")
        if not secrets.compare_digest(row["code_hash"], _hash(code)):
            c.execute("UPDATE sms_pending SET attempts=attempts+1 WHERE phone=?", (account,))
            raise Gate(401, "验证码不对")
        c.execute("DELETE FROM sms_pending WHERE phone=?", (account,))
        u = c.execute("SELECT * FROM users WHERE phone=?", (account,)).fetchone()
        if u is None:
            cur = c.execute("INSERT INTO users(phone,balance,created_at) VALUES(?,?,?)",
                            (account, 0, now))
            user_id = cur.lastrowid
            after = _ledger(c, user_id, "grant", FREE_GRANT, "新用户赠送")
            created = True
        else:
            if u["revoked"]:
                raise Gate(403, "账号已被停用，请联系管理员")
            user_id, after, created = u["id"], u["balance"], False
        token = "rc_" + secrets.token_urlsafe(24)
        c.execute("INSERT INTO devices(user_id,name,token_hash,created_at,last_seen)"
                  " VALUES(?,?,?,?,?)", (user_id, dev_name or "RunnerCoach", _hash(token), now, now))
    return {"ok": True, "token": token, "created": created,
            "balance": after / 1e6, "phone_masked": _mask(account)}


# ---------------------------------------------------------------- 业务
def api_device(row: sqlite3.Row | None, body: dict) -> dict:
    """给账号加一台设备（多机共用一个余额）。

    带令牌调用 = 给当前账号加新设备；不带令牌 = 兼容旧版 App 的匿名设备，
    创建 phone='' 的孤立账号（后台可手工并入正式账号）。
    """
    name = str(body.get("name") or "")[:40]
    now = int(time.time())
    token = "rc_" + secrets.token_urlsafe(24)
    with db() as c:
        if row is not None:
            user_id = row["user_id"]
        else:
            # 匿名账号 phone='' 唯一：已存在就复用（不再重复赠送，避免撞 UNIQUE）
            u = c.execute("SELECT id FROM users WHERE phone=''").fetchone()
            if u:
                user_id = u["id"]
            else:
                cur = c.execute("INSERT INTO users(phone,balance,created_at) VALUES('',0,?)", (now,))
                user_id = cur.lastrowid
                _ledger(c, user_id, "grant", FREE_GRANT, "匿名设备赠送")
        c.execute("INSERT INTO devices(user_id,name,token_hash,created_at,last_seen)"
                  " VALUES(?,?,?,?,?)", (user_id, name or "RunnerCoach", _hash(token), now, now))
        balance = c.execute("SELECT balance FROM users WHERE id=?", (user_id,)).fetchone()["balance"]
    return {"ok": True, "token": token, "balance": balance / 1e6}


def api_me(row: sqlite3.Row) -> dict:
    now = int(time.time())
    month_start = now - now % 2_592_000
    with db() as c:
        used = c.execute("SELECT COALESCE(SUM(cost),0) s FROM usage_log"
                         " WHERE user_id=? AND ts>=?", (row["user_id"], month_start)).fetchone()["s"]
        devices = c.execute("SELECT id,name,last_seen,revoked FROM devices"
                            " WHERE user_id=? ORDER BY id DESC", (row["user_id"],)).fetchall()
    return {"ok": True, "phone_masked": _mask(row["phone"]),
            "balance": row["balance"] / 1e6, "currency": "CNY",
            "used_month": used / 1e6,
            "devices": [{"id": d["id"], "name": d["name"],
                         "last_seen": d["last_seen"], "revoked": bool(d["revoked"])}
                        for d in devices]}


def api_redeem(row: sqlite3.Row, body: dict) -> dict:
    code = str(body.get("code") or "").strip().upper()
    if not code:
        raise Gate(400, "请输入兑换码")
    with db() as c:
        cr = c.execute("SELECT * FROM codes WHERE code=?", (code,)).fetchone()
        if not cr:
            raise Gate(404, "兑换码不存在")
        if cr["used_by_user"]:
            raise Gate(409, "兑换码已被使用")
        c.execute("UPDATE codes SET used_by_user=?, used_at=? WHERE code=?",
                  (row["user_id"], int(time.time()), code))
        after = _ledger(c, row["user_id"], "redeem", cr["amount"], f"码 {code}")
    return {"ok": True, "added": cr["amount"] / 1e6, "balance": after / 1e6}


# ---------------------------------------------------------------- 在线支付
# 充值三通道：兑换码（人工发码）→ demo（全链路测试）→ wechat_native（正式）。
# 订单唯一入账口是 _order_set_paid：先占位更新状态再写流水，天然防重复回调。
def _order_no() -> str:
    return "RC" + time.strftime("%Y%m%d%H%M%S") + secrets.token_hex(4).upper()


def _wx_sign(message: str) -> str:
    """SHA256withRSA（调 openssl 命令行，网关保持零第三方依赖）。"""
    key = Path(WXPAY_KEY_PATH)
    if not key.is_file():
        raise Gate(503, f"微信支付私钥不存在：{key}")
    r = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key)],
                       input=message.encode(), capture_output=True)
    if r.returncode != 0:
        raise Gate(503, "签名失败：" + r.stderr.decode("utf-8", "replace")[:150])
    return base64.b64encode(r.stdout).decode()


def _wx_native_create(out_no: str, yuan: int) -> str:
    """微信 Native 下单，返回二维码链接 code_url。"""
    if not (WXPAY_MCHID and WXPAY_APPID and WXPAY_APIV3_KEY
            and WXPAY_CERT_SERIAL and WXPAY_KEY_PATH):
        raise Gate(503, "微信支付配置不完整（MCHID/APPID/APIV3_KEY/CERT_SERIAL/PRIVATE_KEY）")
    if not WXPAY_NOTIFY_URL.startswith("https://"):
        raise Gate(503, "WXPAY_NOTIFY_URL 必须是 https 回调地址")
    payload = {"appid": WXPAY_APPID, "mchid": WXPAY_MCHID,
               "description": "RunnerCoach 额度充值",
               "out_trade_no": out_no,
               "time_expire": time.strftime(
                   "%Y-%m-%dT%H:%M:%S+08:00",
                   time.gmtime(int(time.time()) + ORDER_EXPIRE + 8 * 3600)),
               "notify_url": WXPAY_NOTIFY_URL,
               "amount": {"total": yuan * 100, "currency": "CNY"}}
    body = json.dumps(payload, ensure_ascii=False)
    ts = str(int(time.time()))
    nonce = secrets.token_hex(8)
    sig = _wx_sign(f"POST\n/v3/pay/transactions/native\n{ts}\n{nonce}\n{body}\n")
    req = urllib.request.Request("https://api.mch.weixin.qq.com/v3/pay/transactions/native",
                                 data=body.encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("Authorization",
                   f'WECHATPAY2-SHA256-RSA2048 mchid="{WXPAY_MCHID}",'
                   f'nonce_str="{nonce}",signature="{sig}",'
                   f'timestamp="{ts}",serial_no="{WXPAY_CERT_SERIAL}"')
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise Gate(502, "微信下单失败：" + e.read().decode("utf-8", "replace")[:200]) from None
    except Exception as e:  # noqa: BLE001
        raise Gate(502, f"微信下单失败：{type(e).__name__}: {e}") from None
    if not data.get("code_url"):
        raise Gate(502, "微信下单未返回 code_url")
    return data["code_url"]


def api_order_create(row: sqlite3.Row, body: dict) -> dict:
    try:
        yuan = int(body.get("amount"))
    except (TypeError, ValueError):
        raise Gate(400, "amount 必须是整数（元）") from None
    if not 1 <= yuan <= 500:
        raise Gate(400, "单笔限额 1~500 元")
    no = _order_no()
    if PAY_PROVIDER == "wechat_native":
        code_url = _wx_native_create(no, yuan)
    elif PAY_PROVIDER == "demo":
        code_url = "demo://pending"              # admin/orders/pay 模拟到账
    else:
        raise Gate(503, "支付通道未配置（服务端 PAY_PROVIDER 未设置）")
    with db() as c:
        cur = c.execute("INSERT INTO orders(out_trade_no,user_id,amount,status,channel,"
                        "code_url,created_at) VALUES(?,?,?,?,?,?,?)",
                        (no, row["user_id"], yuan * 1_000_000, "created",
                         PAY_PROVIDER, code_url, int(time.time())))
        oid = cur.lastrowid
    out = {"ok": True, "order_id": oid, "out_trade_no": no, "amount": yuan,
           "status": "created", "channel": PAY_PROVIDER, "expire_in": ORDER_EXPIRE}
    if code_url and PAY_PROVIDER != "demo":
        out["code_url"] = code_url
    if PAY_PROVIDER == "demo":
        out["notice"] = "demo 通道：admin/orders/pay 可模拟到账"
    return out


def api_order_query(row: sqlite3.Row, body: dict) -> dict:
    try:
        oid = int(body.get("order_id") or 0)
    except (TypeError, ValueError):
        raise Gate(400, "order_id 不对") from None
    now = int(time.time())
    with db() as c:
        o = c.execute("SELECT * FROM orders WHERE id=? AND user_id=?",
                      (oid, row["user_id"])).fetchone()
        if not o:
            raise Gate(404, "订单不存在")
        if o["status"] == "created" and o["created_at"] + ORDER_EXPIRE < now:
            c.execute("UPDATE orders SET status='expired' WHERE id=? AND status='created'",
                      (oid,))
            o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        bal = c.execute("SELECT balance FROM users WHERE id=?",
                        (row["user_id"],)).fetchone()["balance"]
    out = {"ok": True, "order_id": oid, "status": o["status"],
           "amount": o["amount"] / 1e6, "channel": o["channel"],
           "balance": bal / 1e6}
    if o["status"] == "paid":
        out["paid_at"] = o["paid_at"]
    return out


def _order_set_paid(order_id: int, transaction_id: str) -> dict:
    """订单唯一入账口：状态从 created 一次性翻到 paid 并写流水（幂等）。"""
    with db() as c:
        o = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not o:
            raise Gate(404, "订单不存在")
        if o["status"] == "paid":
            return {"ok": True, "status": "paid", "duplicate": True,
                    "balance": c.execute("SELECT balance FROM users WHERE id=?",
                                         (o["user_id"],)).fetchone()["balance"] / 1e6}
        cur = c.execute("UPDATE orders SET status='paid', transaction_id=?, paid_at=? "
                        "WHERE id=? AND status='created'",
                        (transaction_id, int(time.time()), order_id))
        if cur.rowcount != 1:
            raise Gate(409, "订单状态已变化（可能已过期），请联系管理员核对")
        after = _ledger(c, o["user_id"], "topup", o["amount"],
                        f"订单 {o['out_trade_no']} {transaction_id}".strip())
        ledger_id = c.execute("SELECT id FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 1",
                              (o["user_id"],)).fetchone()["id"]
        c.execute("UPDATE orders SET ledger_id=? WHERE id=?", (ledger_id, order_id))
    return {"ok": True, "status": "paid", "amount": o["amount"] / 1e6,
            "balance": after / 1e6}


def _wx_verify_and_decrypt(body: bytes, headers) -> dict:
    """验签（平台证书）+ AES-256-GCM 解密回调报文。需要 cryptography。"""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
        from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
        from cryptography.hazmat.primitives.hashes import SHA256
        from cryptography.exceptions import InvalidSignature
    except ImportError:
        raise Gate(503, "服务端缺少 cryptography，无法验证支付回调") from None
    ts = headers.get("Wechatpay-Timestamp") or ""
    nonce = headers.get("Wechatpay-Nonce") or ""
    serial = headers.get("Wechatpay-Serial") or ""
    sig = headers.get("Wechatpay-Signature") or ""
    if not (ts and nonce and serial and sig):
        raise Gate(400, "回调缺少微信签名头")
    if abs(time.time() - int(ts or 0)) > 300:
        raise Gate(400, "回调时间戳过期")
    cert_pem = _wx_platform_cert(serial)                    # bytes (PEM)
    pub = load_pem_public_key(cert_pem)
    try:
        pub.verify(base64.b64decode(sig),
                   f"{ts}\n{nonce}\n".encode() + body + b"\n",
                   PKCS1v15(), SHA256())
    except InvalidSignature:
        raise Gate(401, "回调验签失败") from None
    res = body and json.loads(body.decode("utf-8", "replace")).get("resource") or {}
    plain = AESGCM(WXPAY_APIV3_KEY.encode()).decrypt(
        res.get("nonce", "").encode(),
        base64.b64decode(res.get("ciphertext") or ""),
        (res.get("associated_data") or "").encode())
    return json.loads(plain.decode("utf-8"))                # {"out_trade_no","transaction_id","trade_state",…}


_WX_CERTS: dict[str, bytes] = {}                             # serial -> PEM


def _wx_platform_cert(serial: str) -> bytes:
    """拉取并解密微信平台证书（按 serial 缓存）。"""
    if serial in _WX_CERTS:
        return _WX_CERTS[serial]
    ts = str(int(time.time()))
    nonce = secrets.token_hex(8)
    sig = _wx_sign(f"GET\n/v3/certificates\n{ts}\n{nonce}\n\n")
    req = urllib.request.Request("https://api.mch.weixin.qq.com/v3/certificates", method="GET")
    req.add_header("Authorization",
                   f'WECHATPAY2-SHA256-RSA2048 mchid="{WXPAY_MCHID}",'
                   f'nonce_str="{nonce}",signature="{sig}",'
                   f'timestamp="{ts}",serial_no="{WXPAY_CERT_SERIAL}"')
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        raise Gate(502, f"拉取微信平台证书失败：{type(e).__name__}: {e}") from None
    for item in data.get("data", []):
        res = item.get("encrypt_certificate", {})
        pem = AESGCM(WXPAY_APIV3_KEY.encode()).decrypt(
            res.get("nonce", "").encode(),
            base64.b64decode(res.get("ciphertext") or ""),
            (res.get("associated_data") or "").encode())
        _WX_CERTS[item.get("serial_no", "")] = pem
    if serial not in _WX_CERTS:
        raise Gate(400, f"平台证书 {serial} 不在列表中")
    return _WX_CERTS[serial]


def api_wechat_notify(body: bytes, headers) -> dict:
    if PAY_PROVIDER != "wechat_native":
        raise Gate(503, "支付通道未启用")
    res = _wx_verify_and_decrypt(body, headers)
    if res.get("trade_state") != "SUCCESS":
        return {"code": "SUCCESS", "message": f"忽略 {res.get('trade_state')}"}
    with db() as c:
        o = c.execute("SELECT id FROM orders WHERE out_trade_no=?",
                      (res.get("out_trade_no", ""),)).fetchone()
    if not o:
        raise Gate(404, "回调订单不存在")
    _order_set_paid(o["id"], res.get("transaction_id", ""))
    return {"code": "SUCCESS", "message": "OK"}


def admin_orders(body: dict) -> dict:
    limit = min(int(body.get("limit") or 50), 200)
    with db() as c:
        rows = c.execute("SELECT o.id,o.out_trade_no,o.amount,o.status,o.channel,"
                         "o.transaction_id,o.created_at,o.paid_at,u.phone "
                         "FROM orders o LEFT JOIN users u ON u.id=o.user_id "
                         "ORDER BY o.id DESC LIMIT ?", (limit,)).fetchall()
    return {"ok": True, "pay_provider": PAY_PROVIDER or "（未配置）",
            "orders": [{"id": r["id"], "no": r["out_trade_no"],
                        "amount": r["amount"] / 1e6, "status": r["status"],
                        "channel": r["channel"], "account": _mask(r["phone"]),
                        "transaction_id": r["transaction_id"],
                        "created_at": r["created_at"], "paid_at": r["paid_at"]}
                       for r in rows]}


def admin_order_pay(body: dict) -> dict:
    """人工确认到账：收款码收款的兜底通道，也是 demo 模式的模拟到账。"""
    try:
        oid = int(body.get("order_id") or 0)
    except (TypeError, ValueError):
        raise Gate(400, "order_id 不对") from None
    with db() as c:
        o = c.execute("SELECT id,status FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        raise Gate(404, "订单不存在")
    return _order_set_paid(oid, "MANUAL")


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
        cost = _charge(row["user_id"], model, int(usage.get("prompt_tokens") or 0),
                       int(usage.get("completion_tokens") or 0))
        data["x_billing"] = {"cost_cny": cost / 1e6, "balance_cny": _balance_of(row["user_id"]) / 1e6}
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
    _charge(row["user_id"], model, pt, ct)


# ---------------------------------------------------------------- 管理端
def admin_codes(body: dict) -> dict:
    amount = _micro(float(body.get("amount") or 0))
    count = max(1, min(200, int(body.get("count") or 10)))
    batch = str(body.get("batch") or time.strftime("%Y%m%d%H%M"))[:40]
    if amount <= 0:
        raise Gate(400, "amount 必须大于 0")
    codes = []
    with db() as c:
        for _ in range(count):
            code = "RC-" + "-".join(secrets.token_hex(2).upper() for _ in range(2))
            c.execute("INSERT INTO codes(code,amount,batch) VALUES(?,?,?)", (code, amount, batch))
            codes.append(code)
    return {"ok": True, "amount_cny": amount / 1e6, "batch": batch, "codes": codes}


def admin_topup(body: dict) -> dict:
    amount = _micro(float(body.get("amount") or 0))
    if amount <= 0:
        raise Gate(400, "amount 必须大于 0")
    with db() as c:
        if body.get("account_id"):
            r = c.execute("SELECT * FROM users WHERE id=?", (int(body["account_id"]),)).fetchone()
        elif body.get("phone"):
            r = c.execute("SELECT * FROM users WHERE phone=?",
                          (str(body["phone"]).strip(),)).fetchone()
        else:
            raise Gate(400, "需要 account_id 或 phone")
        if not r:
            raise Gate(404, "账号不存在")
        after = _ledger(c, r["id"], "adjust", amount, str(body.get("note") or "管理员加额"))
    return {"ok": True, "account": r["id"], "balance_cny": after / 1e6}


def admin_accounts() -> dict:
    with db() as c:
        rows = c.execute("""
            SELECT u.id, u.phone, u.balance, u.created_at, u.revoked,
                   COALESCE(SUM(lg.cost),0) spent,
                   (SELECT COUNT(*) FROM devices d WHERE d.user_id=u.id AND d.revoked=0) ndev
            FROM users u LEFT JOIN usage_log lg ON lg.user_id=u.id
            GROUP BY u.id ORDER BY u.id DESC LIMIT 500
        """).fetchall()
    return {"ok": True, "accounts": [
        {"id": r["id"], "phone_masked": _mask(r["phone"]), "balance_cny": r["balance"] / 1e6,
         "spent_cny": r["spent"] / 1e6, "devices": r["ndev"],
         "created_at": r["created_at"], "revoked": bool(r["revoked"])} for r in rows]}


def admin_ledger(body: dict) -> dict:
    user_id = int(body.get("user_id") or 0) or None
    limit = max(1, min(2000, int(body.get("limit") or 200)))
    with db() as c:
        if user_id:
            rows = c.execute("SELECT * FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT ?",
                             (user_id, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM ledger ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return {"ok": True, "entries": [dict(r) for r in rows]}


def admin_rebuild() -> dict:
    """用流水重建所有余额缓存——账本是对的，缓存只是加速。"""
    with db() as c:
        rows = c.execute("SELECT id FROM users").fetchall()
        for r in rows:
            s = c.execute("SELECT COALESCE(SUM(amount),0) s FROM ledger WHERE user_id=?",
                          (r["id"],)).fetchone()["s"]
            c.execute("UPDATE users SET balance=? WHERE id=?", (s, r["id"]))
    return {"ok": True, "rebuilt": len(rows)}


def admin_export() -> dict:
    """全量数据导出（JSON）——备份 / 迁移用。"""
    with db() as c:
        out = {}
        for t in ("users", "devices", "ledger", "codes", "usage_log"):
            out[t] = [dict(r) for r in c.execute(f"SELECT * FROM {t}").fetchall()]
    out["exported_at"] = int(time.time())
    return {"ok": True, **out}


def admin_revoke(body: dict) -> dict:
    dev_id = int(body.get("device_id") or 0)
    with db() as c:
        r = c.execute("SELECT user_id FROM devices WHERE id=?", (dev_id,)).fetchone()
        if not r:
            raise Gate(404, "设备不存在")
        c.execute("UPDATE devices SET revoked=1 WHERE id=?", (dev_id,))
    return {"ok": True, "device_id": dev_id, "user_id": r["user_id"]}


# ---------------------------------------------------------------- HTTP 壳
class Handler(BaseHTTPRequestHandler):
    server_version = "rc-gateway/0.2"

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

    def _file(self, data: bytes, filename: str, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def _static(self, data: bytes, ctype: str) -> None:
        """内联展示（无附件头）：每日看板经公网中转站转发时用。"""
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
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
                channel = "email" + ("" if smtp_ready() else "（开发模式）")
                if not smtp_ready():
                    channel = "email(dev)/sms:" + SMS_PROVIDER
                return self._json({"ok": True, "model": UPSTREAM_MODEL,
                                   "sms": SMS_PROVIDER, "email": smtp_ready(),
                                   "channel": channel})
            if self.path == "/v1/me":
                return self._json(api_me(_auth(self.headers)))
            if self.path == "/admin/accounts":
                _admin(self.headers)
                return self._json(admin_accounts())
            if self.path == "/admin/export":
                _admin(self.headers)
                return self._json(admin_export())
            if self.path == "/admin/db":
                _admin(self.headers)
                Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
                # 先 checkpoint 保证快照完整
                with db() as c:
                    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                return self._file(Path(DB_PATH).read_bytes(), "gateway-snapshot.db",
                                  "application/x-sqlite3")
            # 每日看板（公网中转站的 / 与 /card.png 会转发到这里，取最新生成结果）
            key = self.path.split("?")[0]
            if key in ("/", "/index.html", "/dashboard"):
                page = SITE_DIR / "index.html"
                if page.is_file():
                    return self._static(page.read_bytes(), "text/html; charset=utf-8")
                raise Gate(404, "看板尚未生成：先在本机运行 daily.bat")
            if key == "/card.png":
                card = SITE_DIR / "card.png"
                if card.is_file():
                    return self._static(card.read_bytes(), "image/png")
                raise Gate(404, "一图流尚未生成：先在本机运行 daily.bat")
            raise Gate(404, "没有这个接口")
        except Gate as e:
            self._json({"ok": False, "error": e.message}, e.code)

    def do_POST(self) -> None:  # noqa: N802
        try:
            body = self._read_body()
            ip = self.headers.get("X-Forwarded-For", "").split(",")[0].strip() \
                or self.client_address[0]
            if self.path == "/v1/sms/send":
                return self._json(api_sms_send(body, ip))
            if self.path == "/v1/auth":
                return self._json(api_auth(body, ip))
            if self.path == "/v1/device":
                # 带令牌 = 给账号加设备；不带 = 旧版匿名兼容
                try:
                    row = _auth(self.headers)
                except Gate:
                    row = None
                return self._json(api_device(row, body))
            if self.path == "/v1/redeem":
                return self._json(api_redeem(_auth(self.headers), body))
            if self.path == "/v1/orders":
                return self._json(api_order_create(_auth(self.headers), body))
            if self.path == "/v1/order/query":
                return self._json(api_order_query(_auth(self.headers), body))
            if self.path == "/v1/wechat/notify":
                return self._json(api_wechat_notify(body, self.headers))
            if self.path == "/v1/chat/completions":
                row = _auth(self.headers)
                return api_chat(row, body, self)   # 流式时直接写完响应
            if self.path == "/admin/codes":
                _admin(self.headers)
                return self._json(admin_codes(body))
            if self.path == "/admin/topup":
                _admin(self.headers)
                return self._json(admin_topup(body))
            if self.path == "/admin/ledger":
                _admin(self.headers)
                return self._json(admin_ledger(body))
            if self.path == "/admin/rebuild":
                _admin(self.headers)
                return self._json(admin_rebuild())
            if self.path == "/admin/revoke":
                _admin(self.headers)
                return self._json(admin_revoke(body))
            if self.path == "/admin/orders":
                _admin(self.headers)
                return self._json(admin_orders(body))
            if self.path == "/admin/orders/pay":
                _admin(self.headers)
                return self._json(admin_order_pay(body))
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
    if smtp_ready():
        print(f"  邮箱验证码 {SMTP_USER} → {SMTP_HOST}:{SMTP_PORT}")
    else:
        print("  邮箱验证码 未配置 SMTP（开发模式：验证码回显）")
    print(f"  短信通道 {SMS_PROVIDER}{'（开发模式：验证码回显，上线前务必切换）' if SMS_PROVIDER == 'dev' else ''}")
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
