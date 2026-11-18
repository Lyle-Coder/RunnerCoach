# -*- coding: utf-8 -*-
"""账号与服务器接口的 HTTP 客户端。

为什么要单独一层账号令牌
------------------------
`platform_token` 原本是「AI 网关令牌」，躺在 AI 设置里。用户关掉 AI 平台模式后
既找不到登录入口，也进不了组——这是设计文档里点名的最容易踩的坑。

所以拆出账号级字段 `account_token` / `account_label`：
- 首次读取时从 `platform_token` 自动迁移（幂等，只做一次）
- AI 平台模式改为读账号令牌，旧字段保留一版做兼容
- 「我的」页有一行「账号」，未登录就登录，已登录就显示脱敏账号

令牌只存在本机配置文件里，不上传到任何地方。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from . import llm
from . import tlstrust as _TLS

# 本地/内测时可以指到 http://192.168.x.x:8300，正式环境留空走配置里的平台地址
ENV_BASE = "RC_API_BASE"
TIMEOUT = 15


# ---------------------------------------------------------------- 令牌
def _cfg() -> dict:
    return llm.load_config()


def _save(**patch) -> dict:
    return llm.save_config(**patch)


def migrate() -> dict:
    """把老的 platform_token 迁到 account_token。幂等。

    只在「账号令牌为空、平台令牌非空」时动手，之后再也不碰——
    否则用户退出登录后，下一次读配置又会把旧令牌搬回来。
    """
    cfg = _cfg()
    if not str(cfg.get("account_token") or "") and str(cfg.get("platform_token") or ""):
        _save(account_token=cfg["platform_token"])
        cfg = _cfg()
    return cfg


def token() -> str:
    return str(migrate().get("account_token") or "")


def label() -> str:
    return str(migrate().get("account_label") or "")


def logged_in() -> bool:
    return bool(token())


def state() -> dict:
    """给界面用。不返回令牌本身，只给前四位做提示。"""
    migrate()
    cfg = _cfg()
    tok = str(cfg.get("account_token") or "")
    return {
        "logged_in": bool(tok),
        "label": str(cfg.get("account_label") or ""),
        "hint": (tok[:4] + "…" + str(len(tok)) + " 位") if tok else "",
        "base": _base(),
    }


def logout() -> dict:
    _save(account_token="", account_label="")
    return {"ok": True}


# ---------------------------------------------------------------- 请求
def _base() -> str:
    env = os.environ.get(ENV_BASE)
    if env:
        return env.rstrip("/")
    return str(_cfg().get("platform_base") or "").rstrip("/")


def request(method: str, path: str, body=None, *, need_auth: bool = True,
            timeout: int = TIMEOUT) -> tuple[bool, dict]:
    """返回 (是否成功, 响应体)。网络错误也当成「失败 + 有话说」，不抛异常。

    调用方全是界面逻辑，抛异常只会变成 500 页面；统一返回要比让人到处 try 好。
    """
    # 自签证书要在第一次 HTTPS 之前装进信任链，否则报 CERTIFICATE_VERIFY_FAILED
    _TLS.install()
    base = _base()
    if not base:
        return False, {"ok": False, "error": "还没有服务器地址，先在「模型服务」里配好"}
    tok = token()
    if need_auth and not tok:
        return False, {"ok": False, "error": "还没登录", "need_login": True}

    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method.upper())
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if tok:
        # Authorization 会被某些公网分享层改写，所以同时带一个自定义头；
        # 网关那边优先认 X-Platform-Token（与 AI 转发同一套约定）。
        req.add_header("Authorization", f"Bearer {tok}")
        req.add_header("X-Platform-Token", tok)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "ignore")
            try:
                return True, json.loads(raw)
            except json.JSONDecodeError:
                return False, {"ok": False, "error": "服务器返回的不是 JSON"}
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            pass
        try:
            d = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            d = {}
        d.setdefault("ok", False)
        d.setdefault("error", f"HTTP {e.code}")
        if e.code == 401:
            d["need_login"] = True
        return False, d
    except Exception as e:  # noqa: BLE001 - 断网/超时/证书错误都走这里
        return False, {"ok": False, "error": f"连不上服务器：{type(e).__name__}"}


# ---------------------------------------------------------------- 登录
def send_code(account: str) -> dict:
    account = str(account or "").strip()
    if not account:
        return {"ok": False, "error": "请输入手机号或邮箱"}
    ok, d = request("POST", "/v1/sms/send", {"account": account}, need_auth=False)
    if ok and d.get("ok"):
        # 开发模式下服务端会把验证码回显，界面直接提示，省得去翻日志
        return {"ok": True, "dev_code": d.get("dev_code") or "",
                "channel": d.get("channel") or "", "notice": d.get("notice") or ""}
    return d


def verify(account: str, code: str, nickname: str = "") -> dict:
    """验证码登录。`nickname` 只在服务端判定为**新账号**时写入。

    老账号传了也不会覆盖——登录接口分不清「他想改名」和「他随手填了一下」，
    真改名走 `set_nickname()`。
    """
    account = str(account or "").strip().lower() if "@" in str(account) else str(account).strip()
    code = str(code or "").strip()
    if not code:
        return {"ok": False, "error": "请输入验证码"}
    body = {"account": account, "code": code, "device_name": "app"}
    nick = str(nickname or "").strip()[:16]
    if nick:
        body["nickname"] = nick
    ok, d = request("POST", "/v1/auth", body, need_auth=False)
    if ok and d.get("ok") and d.get("token"):
        _save(account_token=d["token"], account_label=account)
        # 迁移完成后把老字段清掉，避免两处令牌互相打脸
        if str(_cfg().get("platform_token") or ""):
            _save(platform_token="")
        return {"ok": True, "label": account}
    return d


def me() -> dict:
    ok, d = request("GET", "/v1/me")
    return d if ok else d


def set_nickname(nickname: str) -> dict:
    """改账号昵称。组内昵称是另一件事（组页设置里改），互不影响。"""
    nick = str(nickname or "").strip()[:16]
    if not nick:
        return {"ok": False, "error": "昵称不能为空"}
    ok, d = request("POST", "/v1/me/nickname", {"nickname": nick})
    return d if ok else d
