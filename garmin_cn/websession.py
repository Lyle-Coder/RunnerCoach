# -*- coding: utf-8 -*-
"""把本机佳明登录态回传给平台，供网页版复用。

为什么要有这个模块
------------------
2026-11 佳明把国区登录迁到 SSO Portal 之后，机房 IP 访问
`sso.garmin.cn/portal/*` 会被 Cloudflare 挑战（403 + cf-mitigated: challenge），
服务器**没法代登录**；但 `connect.garmin.cn/gc-api` 不受影响——只要有一份有效
JWT_WEB，同步照跑。

手机（移动网络/家宽）能过 Cloudflare，所以由 App 在登录成功后把
`data/session.json` 交上去，网页版就能用同一份登录态同步。

只传 Cookie，不传密码；同一份登录态 3 小时内不重复上传。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

DEDUPE_GAP = 60             # 同一份会话的防抖间隔（秒）
_META = "web_session_sent"


def push() -> dict:
    """上传当前登录态。返回 `{ok, skipped?, error?}`，**从不抛异常**。

    调用点：`appapi.sync()` 成功之后（含后台补传）。桌面 / 未登录平台时
    `account.request` 会因为没令牌返回 need_login，这里静默跳过。
    """
    try:
        from . import appapi as A

        path = Path(A.HOME) / "data" / "session.json"
        if not path.exists():
            return {"ok": False, "skipped": "没有登录态"}
        try:
            cookies = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, OSError):
            return {"ok": False, "skipped": "登录态读不出来"}
        if not isinstance(cookies, list) or not cookies:
            return {"ok": False, "skipped": "登录态格式不对"}
        if not any(isinstance(c, dict) and c.get("name") == "JWT_WEB"
                   for c in cookies):
            return {"ok": False, "skipped": "登录态里没有 JWT_WEB"}

        digest = hashlib.sha256(
            json.dumps(cookies, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        store = A.Store(A.DB)
        prev = store.get_meta(_META) or ""
        if prev == digest:
            return {"ok": True, "skipped": "没有变化"}
        try:
            gap = time.time() - float(store.get_meta(_META + "_at") or 0)
        except (TypeError, ValueError):
            gap = DEDUPE_GAP
        # **内容变了就必须马上传**：佳明一个账号同一时间只认最新那份会话，
        # App 一重新登录，服务器手上这份立刻 401。以前这里不管内容变没变都按
        # 3 小时节流，于是新会话传不上去，网页版一直停在「登录态已过期」。
        # 现在只对「同一份会话」做 1 分钟防抖。
        if gap < DEDUPE_GAP:
            return {"ok": True, "skipped": "刚传过"}

        from . import account

        ok, resp = account.request("POST", "/v1/web/session", {"session": cookies})
        if ok and isinstance(resp, dict) and resp.get("ok"):
            store.set_meta(_META, digest)
            store.set_meta(_META + "_at", str(int(time.time())))
            return {"ok": True, "cookies": len(cookies)}
        return {"ok": False,
                "error": str((resp or {}).get("error") or "上传失败")[:200]}
    except Exception as e:      # noqa: BLE001 - 回传失败不能影响同步
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
