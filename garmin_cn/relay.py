# -*- coding: utf-8 -*-
"""替别人登录佳明（登录中转机的客户端）。

背景
----
佳明的登录页对机房 IP 有人机校验，服务器自己登不进去；而佳明的会话只有
约 90 分钟，网页版迟早会过期。解法是让一台可信设备（通常是组长那台常开的
安卓手机）领任务：服务器把账号密码临时发过来，设备在手机流量上完成登录，
再把 Cookie 交回去。

安全性
------
* 只在**服务器显式配置了 RC_RELAY_USER 的账号**名下生效（否则 /v1/relay/pull
  返回 403），不是谁都能领别人的密码；
* 密码只在这次请求里经过内存，不落盘、不写日志；
* 登录用临时目录，**不动本机的登录态**（否则会把用户自己的会话顶掉）。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path

_DENIED = "relay_denied"     # 服务器说这台设备不是中转机，记下来别反复问
_DENIED_TTL = 24 * 3600      # 但这个记号会过期：以后被加进白名单还能接上


def poll() -> dict:
    """领一个任务并完成；没有任务就安静返回。从不抛异常。

    调用点：`bg.catch_up()`，App 每 30 分钟醒一次就顺手看一眼。
    """
    try:
        from . import account

        if not account.logged_in():
            return {"ok": True, "skip": "no_account"}
        from . import appapi as A

        store = A.Store(A.DB)
        try:
            denied_at = float(store.get_meta(_DENIED) or 0)
        except (TypeError, ValueError):
            denied_at = 0.0
        if denied_at and time.time() - denied_at < _DENIED_TTL:
            # 服务器说过「这台设备不是中转机」，别每 30 分钟再问一次。
            # 但要有期限——否则以后把它加进白名单也永远接不上。
            return {"ok": True, "skip": "not_relay"}
        ok, resp = account.request("POST", "/v1/relay/pull", {})
        if not ok or not isinstance(resp, dict):
            if "不是中转机" in str((resp or {}).get("error") or ""):
                store.set_meta(_DENIED, str(int(time.time())))
                return {"ok": True, "skip": "not_relay"}
            return {"ok": False, "skip": "pull_failed"}
        job = resp.get("job")
        if not isinstance(job, dict) or not job.get("job_id"):
            return {"ok": True, "skip": "idle"}

        jid = int(job["job_id"])
        email = str(job.get("email") or "")
        password = str(job.get("password") or "")
        if not email or not password:
            _report(jid, error="任务里没有账号或密码")
            return {"ok": False, "skip": "bad_job"}

        home = Path(tempfile.mkdtemp(prefix="rc-relay-"))
        try:
            (home / "data").mkdir(parents=True, exist_ok=True)
            from .auth import GarminCNAuth, MfaRequired

            a = GarminCNAuth(home)
            a.login(email, password)
            sess = (home / "data" / "session.json").read_text(encoding="utf-8")
            cookies = json.loads(sess)
            if not any(c.get("name") == "JWT_WEB" for c in cookies):
                _report(jid, error="登录成功但没拿到 JWT_WEB")
                return {"ok": False, "skip": "no_jwt"}
            _report(jid, cookies=cookies)
            return {"ok": True, "job": jid, "user": job.get("user_id")}
        except MfaRequired:
            # 验证码只能本人在自己手机上完成，中转替不了
            _report(jid, error="需要验证码：请本人用 App 或浏览器登录一次")
            return {"ok": False, "skip": "need_mfa"}
        except Exception as e:      # noqa: BLE001 - 单次失败不影响别的任务
            _report(jid, error=f"{type(e).__name__}: {e}")
            return {"ok": False, "skip": "login_failed"}
        finally:
            shutil.rmtree(home, ignore_errors=True)
    except Exception as e:      # noqa: BLE001 - 后台任务不能因为它崩掉
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}


def _report(job_id: int, cookies=None, error: str = "") -> None:
    try:
        from . import account

        body = {"job_id": int(job_id)}
        if cookies:
            body.update({"ok": True, "session": cookies})
        else:
            body.update({"ok": False, "error": str(error or "登录失败")[:200]})
        account.request("POST", "/v1/relay/push", body)
    except Exception:      # noqa: BLE001 - 报不回去也不能抛
        pass
