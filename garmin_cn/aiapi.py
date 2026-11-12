# -*- coding: utf-8 -*-
"""AI 接口层：把 `llm` / `aicoach` 的调用收成一组「动作名 → 数据」的入口。

为什么要单独一层
----------------
同一套能力有三个调用方：WebView 里的 fetch、Kotlin 的 JS 桥、命令行调试。
如果各自写一遍参数拼装，迟早出现「App 里能用、命令行不能用」这类问题。
这里统一成 `dispatch(action, payload)`，HTTP 层和 appapi 都只做转发。

安全边界
--------
这些动作会**花钱**、会**把健康数据发出去**，所以：
- HTTP 层只允许 127.0.0.1 调用（见 serve.py），局域网访问者拿不到；
- `config.set` 的字段走白名单，界面传什么脏键都不会写进配置；
- key / 令牌永远不回显完整值，只回前 4 位。
"""
from __future__ import annotations

import datetime as dt

from . import aicoach, llm

# 界面能改的配置字段（白名单）。故意不含 updated_at 之类内部字段。
_WRITABLE = ("mode", "provider", "base_url", "model", "api_key",
             "platform_base", "platform_auto", "platform_token",
             "temperature", "auto_advice", "auth_style")


def _truthy(v) -> bool:
    return v is True or str(v).lower() in ("1", "true", "yes", "on")


def dispatch(action: str, payload: dict | None = None) -> dict:
    """执行一个 AI 动作。任何异常都转成 ok=false，调用方不用 try。"""
    p = payload or {}
    try:
        if action == "status":
            return aicoach.status()

        if action == "config.get":
            return llm.public_config()

        if action == "config.set":
            patch = {k: p[k] for k in _WRITABLE if k in p}
            if "temperature" in patch:
                try:
                    patch["temperature"] = max(0.0, min(1.5, float(patch["temperature"])))
                except Exception:
                    patch.pop("temperature")
            if "auto_advice" in patch:
                patch["auto_advice"] = _truthy(patch["auto_advice"])
            if "platform_auto" in patch:
                patch["platform_auto"] = _truthy(patch["platform_auto"])
            # 切换供应商时清掉手填的地址/模型，否则会继续用上一家的
            if p.get("reset_provider"):
                patch["base_url"] = ""
                patch["model"] = ""
            llm.save_config(**patch)
            cfg = llm.public_config()
            cfg["msg"] = "已保存"
            return cfg

        if action == "test":
            return llm.ping()

        if action == "daily":
            return aicoach.daily_advice(force=_truthy(p.get("force")))
        if action == "note":
            # 看板的「教练点评」用它轮询：没写好就返回 pending，页面继续等。
            return aicoach.coach_note(
                str(p.get("date") or "").strip() or None,
                force=_truthy(p.get("force")))
        if action == "meal.today":
            from . import appapi as _A

            return _A.meal_today(str(p.get("date") or ""))

        if action == "weight":
            try:
                days = max(14, min(365, int(p.get("days") or 60)))
            except Exception:
                days = 60
            return aicoach.weight_review(
                days=days, force=_truthy(p.get("force")))
        if action == "activity":
            # id 可空：空表示「最近一次训练」，界面上的快捷按钮就走这条
            return aicoach.activity_review(str(p.get("id") or "").strip())

        if action == "chat":
            msgs = p.get("messages")
            if not isinstance(msgs, list) or not msgs:
                return {"ok": False, "kind": "input", "error": "没有要问的内容"}
            return aicoach.chat(msgs)

        if action == "balance":
            return llm.balance()
        if action == "redeem":
            return llm.redeem(str(p.get("code") or ""))
        if action == "order.create":
            return llm.order_create(p.get("amount"))
        if action == "order.query":
            return llm.order_query(p.get("order_id"))
        if action == "device":
            # 旧版兼容：匿名设备令牌。新界面走 sms + auth（余额挂账号，不丢）
            return llm.device_login(str(p.get("name") or ""))
        if action == "sms":
            return llm.sms_send(str(p.get("account") or p.get("phone") or ""))
        if action == "auth":
            return llm.auth_login(str(p.get("account") or p.get("phone") or ""),
                                  str(p.get("code") or ""),
                                  str(p.get("device_name") or ""))
        if action == "logout":
            llm.save_config(platform_token="")
            return {"ok": True}

        if action == "clear_cache":
            n = 0
            if aicoach.CACHE_DIR.is_dir():
                for f in aicoach.CACHE_DIR.glob("*.json"):
                    try:
                        f.unlink()
                        n += 1
                    except Exception:
                        pass
            return {"ok": True, "cleared": n}

        return {"ok": False, "error": f"没有这个动作：{action}"}
    except llm.LLMError as e:
        return e.as_dict()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def page_state() -> dict:
    """页面首屏要的一次性状态，省掉三次往返。"""
    st = aicoach.status()
    cfg = llm.public_config()
    st["config"] = cfg
    st["today"] = dt.date.today().isoformat()
    return st
