# -*- coding: utf-8 -*-
"""给 App / 图形界面调用的接口层（不依赖终端，不打印进度条）。

为什么要有这一层
----------------
`cli.py` 的每个 cmd_* 都是「读 argparse → print 一堆 → return 退出码」，
Android 里既看不到 stdout、也不该靠退出码判断成功。这里把同样的流程重写成
「进 dict、出 dict」，异常一律转成 `{"ok": false, "error": "..."}`。

三条设计约定
------------
1. **不落密码**。`login()` 成功只保留 `data/session.json` 会话，config 里只记邮箱。
   App 那边把密码存进 Android Keystore，每次启动时传进来续期——Python 侧不留。
2. **不做交互**。需要邮箱验证码时返回 `need_mfa`，由界面再调 `submit_mfa()`。
3. **不假设有浏览器**。`card_html()` 只出 HTML，PNG 由 WebView 自己截。
   返回的 `width/height` 是 HTML 布局基准，`export_*` 是成品 9:16 画幅契约
   （见 `exportspec.py`）：旧 APK 只读前者，新 APK 两个都用。

命令行用法（调试用）：
    py -3 -m garmin_cn.appapi status
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import shutil
import sys
import threading
from pathlib import Path

from .auth import AuthError, GarminCNAuth, MfaRequired
from .client import GarminCNClient, SessionExpired
from .exportspec import BG_ACT, BG_CARD, spec as export_spec
from .metrics import derive
from .store import Store
from .sync import SyncService

# 数据放哪：桌面/ Termux 用项目目录；APK 里代码在只读的 assets 里，
# 由 Kotlin 侧在启动 Python 前设 GARMIN_HOME 指到 filesDir。
_env_home = os.environ.get("GARMIN_HOME")
HOME = Path(_env_home).expanduser() if _env_home else Path(__file__).resolve().parent.parent
DB = HOME / "data" / "garmin.db"
OUT = HOME / "output"
CONFIG = HOME / "config.json"

for _d in (HOME / "data", OUT):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

# 验证码是一次性的，`login()` 到 `submit_mfa()` 之间必须复用同一个 auth 实例
_pending: dict = {"auth": None}


# ---------------------------------------------------------------- 内部
def _cfg() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _remember_email(email: str) -> None:
    """只记邮箱，不记密码。"""
    cfg = _cfg()
    cfg["email"] = email
    cfg.pop("password", None)          # 老版本留下的明文密码，顺手清掉
    try:
        CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        CONFIG.chmod(0o600)
    except Exception:
        pass


def _client(email: str | None = None, password: str | None = None) -> GarminCNClient:
    from .net import Net

    auth = GarminCNAuth(HOME)
    net = Net(auth.session_path, min_interval=0.5)
    ok = bool(net.get_cookie("JWT_WEB") or net.get_cookie("SESSIONID"))

    if not ok and email and password:
        auth.login(email, password)      # 可能要验证码，交给上层处理
        net = Net(auth.session_path, min_interval=0.5)
        ok = bool(net.get_cookie("JWT_WEB") or net.get_cookie("SESSIONID"))

    if not ok:
        raise AuthError("还没有登录，或会话已过期")
    return GarminCNClient(net)


def _today(store: Store) -> dict:
    """一眼能用的今日摘要，给 App 首页用。算不出来就返回空壳，不抛。"""
    try:
        from .coach import brief as _brief

        res = derive(store, dt.date.today().isoformat())
        r = res.get("readiness") or {}
        s = (_brief(res) or {}).get("session") or {}
        return {
            "date": res.get("date"),
            "readiness": r.get("score"),
            "level": r.get("level"),
            "session": s.get("type") or s.get("title"),
            "flags": len(res.get("flags") or []),
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- 桥
# Kotlin 侧只调这一个函数：名字 + JSON 参数数组 → JSON 字符串。
# 为什么不直接 callAttr 再转 map：Chaquopy 不支持把 dict 转成 java.util.Map 接口，
# 会抛 "Cannot convert dict Object to java.util.Map"；而 JSON 是字符串，
# 两边都不用做类型映射，嵌套结构也不会丢。
_API = {
    "env_info", "status", "login", "submit_mfa", "sync", "sync_range",
    "dashboard", "card_html", "act_card_html", "runs", "activities", "days_index",
    "plan", "set_amap_key", "start_server", "recent", "read_html",
    "motion_html", "motion_status", "motion_cleanup", "motion_audio",
    "clips_list", "clips_add", "clips_remove",
    "set_llm_config", "get_llm_config", "ai",
    "mi_status", "mi_login", "mi_captcha", "mi_send_code", "mi_verify_code",
    "mi_logout", "mi_sync",
    "coros_status", "coros_login", "coros_logout", "coros_sync",
}

# 模型接口配置（App「设置」写入）：未来的 AI 点评等功能从这里读。
# key 属敏感信息：落盘 0600，get 时打码，永不整串返回。
LLM_CONFIG = HOME / "data" / "llm_config.json"


def set_llm_config(base_url: str = "", api_key: str = "", model: str = "") -> dict:
    """保存模型接口配置（兼容老 APK 的三参数形式，内部转发到 llm 模块）。

    新版走「模型服务」页面（ai 页面的设置抽屉），那份配置在
    `data/llm_config.json`（0600），字段更全（模式/供应商/平台地址/令牌）。
    这里保留是让旧版 APK 的「设置」菜单不至于报「没有这个接口」。
    """
    from . import llm as L

    L.save_config(base_url=base_url.strip(), api_key=api_key.strip(), model=model.strip())
    return {"ok": True, "configured": L.is_configured()}


def get_llm_config() -> dict:
    """读回模型接口配置；key 只给前 4 位 + 长度，方便确认配没配对。"""
    from . import llm as L

    return L.public_config()


def ai(action: str = "status", payload_json: str = "{}") -> dict:
    """AI 能力的统一入口（名字 + JSON 参数），与 serve 的 /ai/* 同一套动作。"""
    from . import aiapi

    try:
        payload = json.loads(payload_json or "{}")
        if not isinstance(payload, dict):
            raise ValueError("payload 必须是 JSON 对象")
    except Exception as e:
        return {"ok": False, "error": f"参数不合法：{e}"}
    return aiapi.dispatch(str(action or "status"), payload)

_RUN_TYPES = ("running", "trail_running", "treadmill_running", "track_running",
              "virtual_run", "indoor_running", "obstacle_run", "ultra_run")


def call(name: str, args_json: str = "[]") -> str:
    """按名字调用上面这些接口，参数与结果都走 JSON。

    `args_json` 是 JSON 数组，例如 `[30, "a@b.com", "pwd"]`。
    永远返回一个 JSON 对象，失败也带 ok=false，不往外抛。
    """
    if name not in _API:
        return json.dumps({"ok": False, "error": f"没有这个接口：{name}"}, ensure_ascii=False)
    try:
        args = json.loads(args_json or "[]")
        if not isinstance(args, list):
            raise ValueError("args 必须是 JSON 数组")
    except Exception as e:
        return json.dumps({"ok": False, "error": f"参数不合法：{e}"}, ensure_ascii=False)
    try:
        return json.dumps(globals()[name](*args), ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"},
                          ensure_ascii=False)


# ---------------------------------------------------------------- 环境
def env_info() -> dict:
    from . import video
    from .card import can_shot
    from .sysopen import is_termux

    return {
        "ok": True,
        "python": sys.version.split()[0],
        "home": str(HOME),
        "platform": "android" if is_termux() else sys.platform,
        "can_shot": can_shot(),
        "ffmpeg": bool(video.ffmpeg()),
        "data": str(DB),
        "output": str(OUT),
    }


def status() -> dict:
    try:
        store = Store(DB)
        st = store.stats()
    except Exception as e:
        return {"ok": False, "error": f"数据库打不开：{type(e).__name__}: {e}"}

    auth = GarminCNAuth(HOME)
    session_ok = False
    if auth.session_path.exists():
        try:
            from .net import Net

            net = Net(auth.session_path)
            session_ok = bool(net.get_cookie("JWT_WEB") or net.get_cookie("SESSIONID"))
        except Exception:
            session_ok = False

    return {
        "ok": True,
        "logged_in": session_ok,
        "email": _cfg().get("email"),
        "days": st.get("days", 0),
        "activities": st.get("activities", 0),
        "first": st.get("first"),
        "last": st.get("last"),
        "last_sync": store.get_meta("last_sync"),
        "today": _today(store) if st.get("days") else None,
    }


# ---------------------------------------------------------------- 登录
def login(email: str, password: str) -> dict:
    if not email or not password:
        return {"ok": False, "error": "邮箱和密码都要填"}

    auth = GarminCNAuth(HOME)
    try:
        result = auth.login(email, password)
    except MfaRequired:
        _pending["auth"] = auth
        _remember_email(email)
        return {"ok": True, "need_mfa": True,
                "message": "Garmin 把验证码发到了注册邮箱（也翻一下垃圾箱）"}
    except AuthError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    _pending["auth"] = None
    _remember_email(email)
    p = result.get("profile") or {}
    name = " ".join(x for x in (p.get("displayName"), p.get("fullName")) if x) or email
    return {"ok": True, "need_mfa": False, "name": name}


def submit_mfa(code: str) -> dict:
    auth = _pending.get("auth")
    if auth is None:
        return {"ok": False, "error": "没有等待验证码的登录，请重新输入账号密码"}
    if not code:
        return {"ok": False, "error": "验证码不能为空"}
    try:
        auth.submit_mfa(code)
    except AuthError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    _pending["auth"] = None
    return {"ok": True}


# ---------------------------------------------------------------- 小米健康云
# 与 Garmin 的 _pending 同一思路：短信验证链路上的图形验证码、identity_session
# 都挂在 MiCloud 实例上，`mi_login()` 到 `mi_verify_code()` 之间必须复用同一个
# 实例，否则小米会把后续请求当成新设备重新要求验证。
_MI: dict = {"cloud": None, "lock": threading.Lock()}


def _mask_uid(uid: str) -> str:
    s = str(uid or "")
    return (s[:3] + "****" + s[-2:]) if len(s) > 7 else (s or "未知")


def mi_status() -> dict:
    """小米登录态：只看凭据文件是否存在（不联网验证，别让面板卡住）。"""
    try:
        from . import mi as M

        cloud = M.MiCloud(HOME)
        loaded = cloud.load()
        return {"ok": True, "logged_in": bool(loaded),
                "user_id": _mask_uid(cloud.user_id) if loaded else ""}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def mi_login(user: str, password: str) -> dict:
    """账号密码登录小米健康云。要求安全验证时返回 need_verify=True。

    密码只在本次调用里用（MiCloud 也只在内存留一份用于验证后自动重登），
    落盘的 data/mi_auth.json 只有 userId / passToken / ssecurity。
    """
    if not user or not password:
        return {"ok": False, "error": "账号和密码都要填"}
    from . import mi as M

    cloud = M.MiCloud(HOME)
    with _MI["lock"]:
        try:
            cloud.login(user, password)
        except M.MiNeedVerify as e:
            _MI["cloud"] = cloud          # 验证材料挂在这个实例上，后续步骤要用
            info = e.info or {}
            flag = str(info.get("flag") or "")
            return {"ok": True, "need_verify": True,
                    "channel": "sms" if flag == "4" else
                               ("email" if flag == "8" else "unknown"),
                    "masked": info.get("masked") or "绑定的手机号"}
        except M.MiAuthError as e:
            _MI["cloud"] = None
            return {"ok": False, "error": str(e)}
        except M.MiError as e:
            _MI["cloud"] = None
            return {"ok": False, "error": f"连不上小米服务器：{e}"}
        _MI["cloud"] = None
    return {"ok": True, "logged_in": True, "user_id": _mask_uid(cloud.user_id)}


def mi_captcha() -> dict:
    """取一张图形验证码（JPEG），base64 给界面直接显示。"""
    cloud = _MI.get("cloud")
    if cloud is None:
        return {"ok": False, "error": "没有进行中的登录，请重新输入账号密码"}
    from . import mi as M

    with _MI["lock"]:
        try:
            img = cloud.captcha()
        except M.MiError as e:
            return {"ok": False, "error": str(e)}
    import base64

    return {"ok": True, "jpeg_b64": base64.b64encode(img).decode()}


def mi_send_code(icode: str) -> dict:
    """带图形验证码请求下发短信/邮件验证码。码不对时 need_captcha=True。"""
    cloud = _MI.get("cloud")
    if cloud is None:
        return {"ok": False, "error": "没有进行中的登录，请重新输入账号密码"}
    from . import mi as M

    with _MI["lock"]:
        try:
            r = cloud.send_code(icode or "")
        except M.MiError as e:
            return {"ok": False, "error": str(e), "need_captcha": True}
    ok = str(r.get("code")) == "0"
    out = {"ok": ok, "code": r.get("code"),
           "desc": str(r.get("desc") or r.get("reason") or "")}
    if not ok:
        out["need_captcha"] = True        # 码错/过期都要换一张图再来
    return out


def mi_verify_code(code: str) -> dict:
    """提交短信/邮件验证码。`retry=True` 表示码填错，可以重输。"""
    cloud = _MI.get("cloud")
    if cloud is None:
        return {"ok": False, "error": "没有进行中的登录，请重新输入账号密码"}
    from . import mi as M

    with _MI["lock"]:
        try:
            r = cloud.verify_code(code or "")
        except M.MiCodeError as e:
            return {"ok": False, "retry": True, "error": str(e)}
        except M.MiAuthError as e:
            return {"ok": False, "error": str(e)}
        except M.MiError as e:
            return {"ok": False, "error": f"连不上小米服务器：{e}"}
        _MI["cloud"] = None
        cloud._cred = ("", "")            # 登录链路走完，内存里的账号密码不再需要
    return {"ok": True, "user_id": _mask_uid(r.get("userId") or cloud.user_id),
            "relogged": bool(r.get("relogged")), "partial": bool(r.get("partial"))}


def mi_logout() -> dict:
    """删除 data/mi_auth.json（云端数据不受影响）。"""
    try:
        from . import mi as M

        M.MiCloud(HOME).clear()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def mi_sync(days: int = 30) -> dict:
    """拉一次小米秤的体重/体成分并入库（body 表 + 回填 daily）。"""
    from . import mi as M

    store = Store(DB)
    try:
        n, recs = M.sync(store, HOME, days=min(max(int(days), 7), 120))
    except M.MiAuthError as e:
        return {"ok": False, "need_login": True, "error": str(e)}
    except M.MiError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "count": n,
            "latest": recs[-1]["date"] if recs else None,
            "weight": recs[-1].get("weight_kg") if recs else None}


# ---------------------------------------------------------------- 高驰
def coros_status() -> dict:
    """高驰登录态（只看凭据文件，不联网）。"""
    try:
        from . import coros as C

        cloud = C.CorosCloud(HOME)
        ok = cloud.load()
        return {"ok": True, "logged_in": ok,
                "user_id": _mask_uid(cloud.user_id) if ok else "",
                "region": cloud.region if ok else "",
                "region_cn": C.REGION_CN.get(cloud.region, cloud.region) if ok else ""}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def coros_login(email: str, password: str, region: str = "asia") -> dict:
    """账号密码登录高驰（Training Hub）。密码不落盘，token 存 0600。"""
    if not email or not password:
        return {"ok": False, "error": "账号和密码都要填"}
    from . import coros as C

    cloud = C.CorosCloud(HOME)
    try:
        r = cloud.login(email, password, region or "asia")
    except C.CorosError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "user_id": _mask_uid(r.get("user_id")),
            "region": r.get("region"),
            "region_cn": C.REGION_CN.get(r.get("region"), r.get("region"))}


def coros_logout() -> dict:
    try:
        from . import coros as C

        C.CorosCloud(HOME).clear()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def coros_sync(days: int = 30, email: str = "", password: str = "") -> dict:
    """拉高驰每日指标 + 活动入库。token 24h 过期，带凭据就静默重登一次。"""
    from . import coros as C

    store = Store(DB)
    try:
        r = C.sync(store, HOME, days=days,
                   email=email or None, password=password or None)
    except C.CorosAuthError as e:
        return {"ok": False, "need_login": True, "error": str(e)}
    except C.CorosError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "daily": r.get("daily", 0), "activities": r.get("activities", 0),
            "range": r.get("range"), "relogged": r.get("relogged", False)}


# ---------------------------------------------------------------- 同步
def sync(days: int = 30, email: str | None = None, password: str | None = None,
         with_body: bool = True, progress=None, force: bool = False,
         end: str | None = None, activity_pages: int = 1) -> dict:
    """拉数据。`email/password` 只在会话过期时用来续一次，不会被写盘。

    增量是默认行为：已有数据且早已过完的日子不会再请求一遍，
    所以「下拉刷新」通常只拉今天和昨天，几秒就能结束。
    需要整段重拉（换表盘 / 补历史字段）时传 `force=True`。

    `end` 是区间结束日（默认今天），`activity_pages` 是活动列表翻页数——
    两个都是末尾新增参数，旧 APK 的四参数调用完全不受影响。
    """

    def cb(i, n, d):
        if progress:
            try:
                progress(i, n, d)
            except Exception:
                pass

    try:
        client = _client(email, password)
    except MfaRequired:
        return {"ok": False, "need_mfa": True, "error": "续期需要邮箱验证码，请重新登录"}
    except AuthError as e:
        return {"ok": False, "need_login": True, "error": str(e)}

    store = Store(DB)
    svc = SyncService(client, store)
    try:
        res = svc.sync(days=days, end=end, extras=False, progress=cb,
                       force=force, activity_pages=activity_pages)
    except SessionExpired:
        # 会话刚好过期：有凭据就重登一次再试，没有就让界面去登录
        if not (email and password):
            return {"ok": False, "need_login": True, "error": "会话已过期"}
        try:
            client = _client(email, password)
            res = SyncService(client, store).sync(
                days=days, end=end, extras=False, progress=cb,
                force=force, activity_pages=activity_pages)
        except MfaRequired:
            return {"ok": False, "need_mfa": True, "error": "续期需要验证码"}
        except Exception as e:
            return {"ok": False, "need_login": True, "error": f"{type(e).__name__}: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    out = {"ok": True, "range": res["range"], "days": res["days"],
           "fetched": res.get("fetched", res["days"]),
           "skipped": res.get("skipped", 0),
           "requests": res.get("requests", 0),
           "activities": res["activities"], "errors": res["errors"]}

    if with_body:
        try:
            from . import mi as M

            n, recs = M.sync(store, HOME, days=min(max(days, 7), 120))
            out["body"] = {"count": n, "latest": recs[-1]["date"] if recs else None}
        except Exception as e:
            out["body"] = {"count": 0, "error": f"{type(e).__name__}: {e}"}

    return out


def sync_range(start: str, end: str | None = None,
               email: str | None = None, password: str | None = None,
               with_body: bool = True, force: bool = False,
               activity_pages: int | None = None) -> dict:
    """按日期区间补历史数据（App「拉取更多历史」用）。

    `start` / `end` 是 YYYY-MM-DD，end 默认今天。一次最多 3 年，
    防止误点一次发几百个请求；活动列表按 60 条一页自动翻到区间起点。
    返回结构与 `sync()` 一致，额外带 `start` / `end` / `activity_pages`。
    """
    try:
        s = dt.date.fromisoformat(str(start))
        e = dt.date.fromisoformat(str(end)) if end else dt.date.today()
    except (TypeError, ValueError):
        return {"ok": False, "error": "日期格式应为 YYYY-MM-DD"}
    if s > e:
        return {"ok": False, "error": "开始日期不能晚于结束日期"}
    days = (e - s).days + 1
    if days > 1095:
        return {"ok": False, "error": "一次最多补 3 年（1095 天），请分段拉取"}
    pages = activity_pages if activity_pages is not None else min(
        20, max(1, (days + 29) // 30))
    out = sync(days=days, email=email, password=password,
               with_body=with_body, progress=None, force=force,
               end=e.isoformat(), activity_pages=pages)
    if out.get("ok"):
        out["start"] = s.isoformat()
        out["end"] = e.isoformat()
        out["activity_pages"] = pages
    return out


# ---------------------------------------------------------------- 产出
def dashboard(date: str | None = None, live: bool = True) -> dict:
    """出网页看板，返回文件路径（App 用 WebView 直接读它，或走 start_server）。"""
    store = Store(DB)
    if store.stats()["days"] == 0:
        return {"ok": False, "error": "还没有数据，先同步一次"}

    d = date or dt.date.today().isoformat()
    if live:
        try:
            from . import live as L

            L.body_battery(store, d, None)
            L.scale(store, HOME)
        except Exception:
            pass

    from .report import render

    out = OUT / f"dashboard-{d}.html"
    try:
        path = render(derive(store, d), store, out)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    try:
        # latest.html 代表「今天的看板」。查历史日期时不能覆盖它，否则
        # 首页第一项会悄悄变成刚看过的那天，返回今日也回不去。
        if d == dt.date.today().isoformat():
            shutil.copyfile(path, OUT / "latest.html")
    except Exception:
        pass
    return {"ok": True, "html": str(path), "date": d}


def card_html(date: str | None = None) -> dict:
    """竖版一图流的 HTML（1080×1920）。PNG 交给 WebView 截，不依赖无头浏览器。"""
    store = Store(DB)
    if store.stats()["days"] == 0:
        return {"ok": False, "error": "还没有数据，先同步一次"}

    d = date or dt.date.today().isoformat()
    from .card import build_html
    from .plan import attach

    try:
        res = attach(derive(store, d))
        from .report import source_badge

        _label, _ts = source_badge(store)
        html = build_html(res,
                          sync_at=store.get_meta("last_sync") or store.get_meta("last_sync_coros"),
                          src=_label)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    out = OUT / f"card-{d}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    # width/height 是 HTML 的布局基准，旧 APK 仍按它建 View；export_* 是
    # 新 APK 用的成品画幅契约（9:16），两者含义不同，别混用。
    return {"ok": True, "html": str(out), "date": d, "width": 1080, "height": 1920,
            **export_spec(BG_CARD, trim=False)}


def plan(refresh: bool = False, date: str | None = None,
         source: str = "auto") -> dict:
    """训练安排（佳明日历优先，Garmin Coach 兜底）。

    默认读同步时落下的 `data/plan.json`——零请求，断网也能看。
    `refresh=True` 才联网拉一次，失败不影响已有快照。
    `source`：auto / calendar / coach，见 `plan.fetch()`。
    高驰没有对外计划接口，这里只处理佳明。
    """
    from .plan import fetch, load

    today = date or dt.date.today().isoformat()
    path = HOME / "data" / "plan.json"
    data = None if refresh else load(path)
    if not data:
        try:
            client = _client()
        except AuthError as e:
            return {"ok": False, "error": f"还没登录佳明：{e}", "kind": "auth"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "kind": "auth"}
        try:
            data = fetch(client, today, out_path=path, source=source)
        except SessionExpired:
            return {"ok": False, "error": "佳明会话过期，请重新登录", "kind": "auth"}
        except Exception as e:
            data = {"available": False, "reason": f"{type(e).__name__}: {e}"}
    if not data or not data.get("available"):
        return {"ok": False,
                "error": (data or {}).get("reason") or "当前没有可用的训练安排",
                "empty": True}
    return {"ok": True, **data}


def runs(limit: int = 30, days: int = 180) -> dict:
    """最近的跑步活动，给 App 的「选一条出图」列表用。

    只查本地库，不联网。带 activity_id 才能继续出图。
    `days` 是回溯天数，补过历史之后可以传更大值（默认仍兼容旧 APK）。
    """
    try:
        store = Store(DB)
        end = dt.date.today()
        start = end - dt.timedelta(days=max(1, int(days or 180)))
        out = []
        for r in store.activities(start.isoformat(), end.isoformat()):
            if (r.get("type") or "") not in _RUN_TYPES:
                continue
            dist = (r.get("distance_m") or 0) / 1000
            pace = r.get("pace_s_km") or 0
            pace_s = f"{int(pace) // 60}'{int(pace) % 60:02d}\"" if pace else "—"
            out.append({
                "id": str(r["activity_id"]),
                "date": r.get("date") or "",
                "name": r.get("name") or "跑步",
                "dist": round(dist, 2),
                "pace": pace_s,
                "label": f"{r.get('date') or ''}  {r.get('name') or '跑步'}  "
                         f"{dist:.2f}km  {pace_s}",
            })
            if len(out) >= max(1, int(limit)):
                break
        return {"ok": True, "items": out}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def activities(start: str | None = None, end: str | None = None,
               types: str | None = None, limit: int = 100,
               offset: int = 0) -> dict:
    """查询本地库里的历史活动（不联网），给 App「历史活动」列表用。

    `start` / `end` 是 YYYY-MM-DD；`types` 传 `run` 只筛跑步，也可以传
    逗号分隔的 typeKey，留空表示全部。`limit` / `offset` 分页，界面用
    `has_more` 决定要不要显示「加载更早」。
    """
    try:
        store = Store(DB)
        st = store.stats()
        # 起点以「活动表」最早日期为准，不能用 daily.first：佳明活动列表
        # 一次返回最近 60 条，往往横跨到日汇总窗口之前（本机就有 33 条
        # 9 月的活动），按 daily.first 查会把这些已落库的历史活动漏掉。
        try:
            act_first = store.conn.execute(
                "SELECT MIN(date) FROM activities").fetchone()[0]
        except Exception:
            act_first = None
        e = end or dt.date.today().isoformat()
        s = start or act_first or st.get("first") or (
            dt.date.today() - dt.timedelta(days=3650)).isoformat()
        try:
            dt.date.fromisoformat(s)      # 非法日期直接报错，别悄悄查出空集
            dt.date.fromisoformat(e)
        except (TypeError, ValueError):
            return {"ok": False, "error": "日期格式应为 YYYY-MM-DD"}
        if s > e:
            return {"ok": False, "error": "开始日期不能晚于结束日期"}
        rows = store.activities(s, e)
        kind = (types or "").strip().lower()
        if kind == "run":
            allow = set(_RUN_TYPES)
        elif kind and kind not in ("all", "全部"):
            allow = {x.strip() for x in kind.split(",") if x.strip()}
        else:
            allow = None
        picked = [r for r in rows if allow is None or (r.get("type") or "") in allow]
        lim = max(1, min(500, int(limit or 100)))
        off = max(0, int(offset or 0))
        items = []
        for r in picked[off:off + lim]:
            dist = (r.get("distance_m") or 0) / 1000
            pace = r.get("pace_s_km") or 0
            pace_s = f"{int(pace) // 60}'{int(pace) % 60:02d}\"" if pace else "—"
            dur = r.get("duration_s") or 0
            dur_s = f"{int(dur) // 60}:{int(dur) % 60:02d}"
            items.append({
                "id": str(r.get("activity_id") or ""),
                "date": r.get("date") or "",
                "start": (r.get("start_ts") or "")[11:16],
                "name": r.get("name") or "活动",
                "type": r.get("type") or "",
                "dist": round(dist, 2),
                "pace": pace_s,
                "dur": dur_s,
                "hr": r.get("avg_hr"),
                "load": r.get("load"),
                "label": (f"{r.get('date') or ''}  "
                          f"{(r.get('start_ts') or '')[11:16]}  "
                          f"{r.get('name') or '活动'}  {dist:.2f}km  {pace_s}"),
            })
        return {"ok": True, "items": items, "total": len(picked),
                "offset": off, "has_more": off + lim < len(picked),
                "start": s, "end": e,
                "first": st.get("first"), "last": st.get("last")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def days_index(limit: int = 400) -> dict:
    """库里已有数据的日期清单（倒序），给 App 的日期选择器划范围。"""
    try:
        store = Store(DB)
        st = store.stats()
        rows = store.conn.execute(
            "SELECT date FROM daily ORDER BY date DESC LIMIT ?",
            (max(1, min(2000, int(limit or 400))),),
        ).fetchall()
        return {"ok": True, "items": [r["date"] for r in rows],
                "count": st.get("days", 0), "activities": st.get("activities", 0),
                "first": st.get("first"), "last": st.get("last")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def act_card_html(activity_id: str, title: str | None = None,
                  email: str | None = None, password: str | None = None,
                  privacy: str = "none",
                  coros_email: str = "", coros_password: str = "") -> dict:
    """单次活动图的 HTML（布局基准 1160×2250，成品统一 1080×1920），PNG 交给 WebView 截。

    `privacy` 是分享脱敏档位：`none` 原图 / `trim` 首尾各裁 200 m /
    `hide` 完全隐藏轨迹。放在参数末尾且默认 `none`，**旧 APK 传 4 个参数时
    行为完全不变**；新 APK 多传一个档位即可。脱敏在生成 HTML 前完成，
    产物里不会有原始折线、底图或精确起跑时间。

    `coros_email/coros_password` 是高驰凭据：活动行带 source=coros 时走高驰
    明细接口（GPX 轨迹 + frequencyList/lapList），绝不弹佳明登录框；token
    过期就用它们静默重登一次。旧 APK 不传也兼容（届时提示登录高驰）。

    `height=2250` 只表示 HTML 需要多高的画布，不是成品尺寸；App 会按
    `export_*` 把内容等比收进 9:16，避免分享面板对超长图二次裁切。

    与 `cli.py act --card` 的区别：直接调 `actcard.build_html`，不碰 `shoot()`
    （那要无头 Chrome），也不取高德底图（`file://` 图片在 http 页面里加载不了，
    而且离线优先）。轨迹回落到纯 SVG 矢量，断网也能出图。

    明细优先读库里的缓存；没有才联网抓一次并落库，之后就一直离线可用。
    高驰按原始活动记录识别，凭据由末尾两个独立参数传入；旧五参数调用保持兼容。
    """
    try:
        store = Store(DB)
        row = store.get_activity(activity_id=str(activity_id))
        if not row:
            return {"ok": False, "error": "库里没有这条活动，先同步一次"}
        aid = str(row["activity_id"])
        src = (row.get("source") or "garmin")

        from . import coros_activity as CA
        from .coros import CorosAuthError
        provider = CA.source(row)

        detail = store.get_detail(aid)
        if not detail and src == "coros":
            from . import coros as C

            try:
                detail = C.fetch_detail(store, HOME, row,
                                        coros_email or None, coros_password or None)
                store.save_detail(aid, detail)
            except C.CorosAuthError as e:
                return {"ok": False, "need_login": True, "source": "coros",
                        "error": f"{e}（高驰明细需联网抓一次）"}
            except Exception as e:
                return {"ok": False,
                        "error": f"抓高驰明细失败（需要联网）：{type(e).__name__}: {e}"}
        elif not detail:
            try:
                from . import activity as act

                if provider == "coros":
                    detail = CA.ensure(store, HOME, row, coros_email, coros_password)
                else:
                    detail = act.ensure(store, _client(email, password), aid)
            except CorosAuthError:
                return {"ok": False, "need_login": True, "source": "coros",
                        "error": "高驰登录已失效，请登录高驰后获取活动明细"}
            except (AuthError, SessionExpired):
                return {"ok": False, "need_login": True, "source": "garmin",
                        "error": "这条活动的明细还没缓存，需要联网登录后抓一次"}
            except Exception as e:
                return {"ok": False,
                        "source": provider,
                        "error": f"获取{'高驰' if provider == 'coros' else '佳明'}活动明细失败：{e}"}
        if not detail:
            return {"ok": False, "error": "没拿到活动明细"}

        from . import privacy as PV
        from .actcard import H, TRK_CH, TRK_CW, W, build_html

        mode = PV.normalize(privacy)
        m_track, mask_info = PV.mask_track(detail.get("track") or [], mode)

        # 底图：`hide` 档完全不碰；`trim` 只裁首尾，用**裁剪后**的轨迹取景，
        # 起终点区域不会出现在底图上。没有 Key / 断网 / 接口失败时静默回落矢量。
        # 页面走 http://127.0.0.1，`file://` 图片加载不了，所以用同源 URL。
        bm = None
        if mode != PV.MODE_HIDE and os.environ.get("GARMIN_MAP", "1") != "0":
            from . import basemap as BM

            ident = aid if mode == PV.MODE_NONE else f"{aid}-{mode}"
            got = BM.ensure(m_track, TRK_CW, TRK_CH, ident)
            if got:
                name = Path(got["png"]).name
                bm = {"png": f"/basemap/{name}", "meta": got["meta"]}
        html = build_html(row, detail, None, bm, title, privacy=mode)
        # 档位不同就换文件名：别让脱敏版和原图互相覆盖，也便于事后自证
        suffix = "" if mode == PV.MODE_NONE else f"-{mode}"
        out = OUT / f"act-{row.get('date') or 'unknown'}-{aid}{suffix}.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        return {"ok": True, "html": str(out), "name": row.get("name") or "活动",
                "date": row.get("date"), "width": W, "height": H,
                "privacy": mode, "privacy_note": PV.note(mode, mask_info),
                **export_spec(BG_ACT)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def set_amap_key(key: str = "") -> dict:
    """保存高德 Web 服务 Key（只落本机 `data/amap.json`，0600，不上传）。

    没有 Key 时活动图回落到矢量轨迹；配了之后**轨迹中心点会发给高德**换底图，
    属于「位置数据出本机」，所以只在使用者主动填写后生效。传空串表示清除。
    """
    p = HOME / "data" / "amap.json"
    k = (key or "").strip()
    if not k:
        try:
            p.unlink()
        except OSError:
            pass
        return {"ok": True, "configured": False,
                "message": "已清除，活动图回落到矢量轨迹"}
    if len(k) < 16:
        return {"ok": False, "error": "Key 看起来不完整，重新复制一次"}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"key": k}, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    except OSError as e:
        return {"ok": False, "error": f"写入失败：{e}"}
    from . import basemap as BM

    ok, msg = BM.probe(k)
    return {"ok": True, "configured": True, "probe": ok, "message": msg}


CLIPS = HOME / "data" / "clips.json"
MEDIA = HOME / "data" / "media"
_RUN_RE = re.compile(r"[A-Za-z0-9_-]{6,32}")


def _clips_read() -> list[dict]:
    try:
        data = json.loads(CLIPS.read_text("utf-8"))
        items = data.get("items") if isinstance(data, dict) else data
        return [x for x in (items or []) if isinstance(x, dict)]
    except Exception:
        return []


def _clips_write(items: list[dict]) -> None:
    CLIPS.parent.mkdir(parents=True, exist_ok=True)
    CLIPS.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=1) + "\n",
                     encoding="utf-8")


def _synth_route(detail: dict) -> dict:
    """从本地缓存的 track + 1Hz 序列合成播放器要的轨迹时间轴。

    云端原始明细里有 `geoPolylineDTO`（每个点都带时间），但缓存的精简版没有，
    只有抽稀后的经纬度和 1Hz 序列。这里按**累计里程**把两者对齐：轨迹点落在
    线上多少米，就取序列里对应里程那一秒的时间 / 心率 / 速度。
    """
    import bisect

    from .route import haversine

    track = [(float(a), float(b)) for a, b in (detail.get("track") or [])
             if a is not None and b is not None]
    pts = (detail.get("series") or {}).get("points") or []
    if len(track) < 2 or len(pts) < 2:
        return {}
    dist = [0.0]
    for i in range(1, len(track)):
        dist.append(dist[-1] + haversine(track[i - 1][0], track[i - 1][1],
                                         track[i][0], track[i][1]))
    sdist = [float(p.get("dist") or 0) for p in pts]
    for i in range(1, len(sdist)):            # 单调化，二分才成立
        if sdist[i] < sdist[i - 1]:
            sdist[i] = sdist[i - 1]
    if sdist[-1] <= 0:
        return {}
    t0 = float(pts[0].get("t") or 0)
    ts, hrs, vs = [], [], []
    for dd in dist:
        j = min(max(bisect.bisect_left(sdist, dd), 0), len(pts) - 1)
        p = pts[j]
        ts.append(int(round(float(p.get("t") or 0) - t0)))
        hrs.append(int(round(float(p["hr"]))) if p.get("hr") else None)
        vs.append(round(float(p["spd"]), 2) if p.get("spd") else None)
    for i in range(1, len(ts)):
        if ts[i] < ts[i - 1]:
            ts[i] = ts[i - 1]
    if ts[-1] <= 0:
        return {}
    # t0 必须是**毫秒**：`photo.match` 用 (t_utc_ms - t0) / 1000 算相对秒。
    # 这里原来直接给秒，照片和素材全部被判成「不在活动时段」丢掉——
    # 手机上「加了素材但回放里没有」就是这个原因（桌面路径走 route.build，
    # 那边的 t0 本来就是毫秒，只有这条合成路径漏了换算）。
    return {"t0": int(t0 * 1000), "n": len(track), "t": ts,
            "lat": [round(a, 6) for a, _ in track],
            "lon": [round(b, 6) for _, b in track],
            "d": [round(x, 1) for x in dist],
            "hr": hrs, "v": vs, "seg": [0] * len(track), "drift": 0.0}


def motion_html(activity_id: str, fps: int = 24, duration: int = 30,
                privacy: str = "none") -> dict:
    """生成回放视频用的动画 HTML 与收帧参数。

    手机上不跑 ffmpeg：App 把这个 HTML 加载到离屏 WebView
    （`?export=1&total=<frames>&fps=<fps>&run=<run>`），页面逐帧 POST 到
    `/__frame`，再由原生 `MediaCodec` 编码成 MP4。
    """
    from . import motion as M

    try:
        store = Store(DB)
        row = store.get_activity(activity_id=str(activity_id))
        if not row:
            return {"ok": False, "error": "库里没有这条活动，先同步一次"}
        aid = str(row["activity_id"])
        detail = store.get_detail(aid)
        if not detail:
            return {"ok": False, "need_login": True,
                    "error": "这条活动的明细还没缓存，先出一次图或同步一次"}
        from . import privacy as PV

        mode = PV.normalize(privacy)
        detail = dict(detail)
        track_trim, _ = PV.mask_track(detail.get("track") or [], mode)
        if len(track_trim) < 2:
            return {"ok": False, "error": "这个脱敏档位下没有可用的轨迹"}
        detail["track"] = track_trim
        # 轨迹被裁过，旧时间轴不能再用；缓存里也没有带时间的原始折线，
        # 所以统一按「累计里程」把 track 与 1Hz 序列对齐后现场合成一条。
        rt = _synth_route(detail)
        if not rt:
            return {"ok": False,
                    "error": "这次活动的轨迹点太少，做不了回放（室内跑或无 GPS）"}
        detail["route"] = rt
        try:
            fps = max(12, min(int(fps or 24), 30))
            dur = max(8, min(int(duration or 30), 60))
        except (TypeError, ValueError):
            fps, dur = 24, 30
        run = secrets.token_hex(6)
        out = OUT / f"motion-{aid}.html"
        clips = _clips_for_motion()
        pl: dict = {}
        # web=True：App 里的播放器走 http，必须把底图/照片/视频换成本源 URL，
        # 否则真机上绝对路径 404 → 页面 ready=false → 一帧都不推
        M.render(row, detail, None, out, duration=float(dur), clips=clips,
                 clip_cache=HOME / "data" / "motion_cache", web=True,
                 payload_out=pl)
        # 帧数必须按**真实总时长**算：巡航 30 秒 + 素材停留（视频自身时长、
        # 图片 3 秒/张）。以前按 30 秒算，素材一多尾巴就被截掉——最后几段
        # 视频干脆没出现在成片里，看起来就像「锚点错了」。
        total = float(pl.get("total") or dur)
        return {"ok": True, "html": str(out), "run": run,
                "frames": max(1, int(round(total * fps))), "fps": fps,
                "duration": round(total, 2), "play": dur,
                "width": 1080, "height": 1920, "clips": len(clips),
                "privacy": mode, "name": row.get("name") or "跑步回放"}
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


STILL_SEC = 3.0          # 图片素材在回放里停留多久


def _meta_val(it: dict, info: dict, key: str):
    """记录里存过就用存的，否则回落到现场探测。

    登记时从源文件读到的拍摄时间比成品的更可信（重封装会丢 creation_time），
    所以存下来的值优先。
    """
    v = it.get(key)
    return v if v is not None else info.get(key)


def _clips_for_motion() -> list[dict]:
    """素材（视频 / 图片）→ motion.build_payload 要的 shot 结构。

    必须带上 `t_utc`：`photo.match` 靠拍摄时间把素材挂到轨迹上。
    之前这里只给路径和时长，结果全部以「没有拍摄时间」被丢掉 ——
    表现就是「加了素材，回放里却什么都没有」。
    视频读 mvhd 的 creation_time，图片读 EXIF；都没有的留空，
    由 `motion` 那边均匀铺开，至少能出现在画面上。
    """
    out = []
    for it in _clips_read():
        p = Path(str(it.get("path") or ""))
        if not p.is_file():
            continue
        info = _probe_material(p, name_hint=str(it.get("name") or ""))
        kind = it.get("kind") or info.get("kind") or "video"
        dur = float(it.get("dur") or 0)
        if dur <= 0:
            dur = STILL_SEC if kind == "image" else 3.0
        out.append({
            "path": str(p),
            "name": it.get("name") or p.stem,
            "kind": kind,
            "dur": round(dur, 2),
            "w": int(it.get("w") or info.get("w") or 1080),
            "h": int(it.get("h") or info.get("h") or 1920),
            "orientation": int(info.get("orientation") or 1),
            "t_utc": _meta_val(it, info, "t_utc"),
            "lat": _meta_val(it, info, "lat"),
            "lon": _meta_val(it, info, "lon"),
            "codec": info.get("codec") or "",
            "bytes": info.get("bytes") or 0,
        })
    return out


def _probe_material(p: Path, name_hint: str = "") -> dict:
    """按扩展名判类型再读元数据。读不出来也返回带 kind 的骨架，绝不抛异常——
    素材缺元数据只该「定位不准」，不该让整次回放失败。

    `name_hint` 是登记时的原始文件名：成品被改名成 `clip-…` 之后，
    相机名里的 `20261103_180036` 还能兜出拍摄时间。
    """
    ext = p.suffix.lower()
    if ext in (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"):
        from . import photo as photo_mod
        try:
            j = photo_mod.read(p)
        except Exception as e:
            j = {"error": f"{type(e).__name__}: {e}"}
        return {"kind": "image",
                "w": j.get("w"), "h": j.get("h"),
                "orientation": j.get("orientation") or 1,
                "t_utc": j.get("t_utc"), "t_source": j.get("t_source") or "",
                "t_why": "" if j.get("t_utc") else (j.get("error") or "图片里没有 EXIF 拍摄时间"),
                "lat": j.get("lat"), "lon": j.get("lon"),
                "bytes": j.get("bytes") or 0, "dur": None}
    from . import clip as clip_mod
    try:
        v = clip_mod.probe(p, name_hint=name_hint)
    except Exception as e:
        # 以前这里静默吞掉异常，界面只显示「加入 xx」，用户完全无从判断
        # 是「文件没时间」还是「读崩了」。现在把原因带出去。
        v = {"t_why": f"读取失败：{type(e).__name__}: {e}"}
    return {"kind": "video",
            "w": v.get("w"), "h": v.get("h"), "orientation": 1,
            "t_utc": v.get("t_utc"), "t_source": v.get("t_source") or "",
            "t_why": v.get("t_why") or "",
            "lat": v.get("lat"), "lon": v.get("lon"), "dur": v.get("dur"),
            "codec": v.get("codec") or "", "bytes": v.get("bytes") or 0}


def motion_status(run: str) -> dict:
    """查收帧进度：原生拿它判断页面推到第几帧、有没有推完。"""
    run = str(run or "")
    if not _RUN_RE.fullmatch(run):
        return {"ok": False, "error": "run 不合法"}
    d = OUT / "_frames" / run
    if not d.is_dir():
        return {"ok": True, "frames": 0, "done": False, "dir": str(d)}
    diag = {}
    try:
        f = d / "_diag.json"
        if f.is_file():
            diag = json.loads(f.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        diag = {}
    return {"ok": True, "frames": len(list(d.glob("f_*.jpg"))),
            "done": (d / "_done.json").is_file(), "dir": str(d),
            "diag": diag}


def _audio_local(raw: str) -> Path | None:
    """时间轴里的素材标识 → 本机磁盘路径。

    页面给 WebView 用的是 `/data/media/xxx.mp4` 这种同源 URL（见 motion._web_url），
    MediaExtractor 打不开 URL；桌面导出的老页面则直接回传绝对路径。两种都认，
    但**只放行 data/ 下的素材目录**：时间轴是页面 POST 上来的，不能让一个
    JSON 就把任意文件交给解码器。
    """
    s = str(raw or "").strip()
    if not s:
        return None
    data = (HOME / "data").resolve()
    try:
        # 真机上素材的绝对路径是 /data/user/0/<包名>/files/data/media/...，
        # 开头也是 /data/！先按「确实存在的绝对路径」认，别把它当成同源 URL
        # 再拼一次家目录（那样拼出的文件不存在，原声会整段丢掉）。
        if Path(s).is_absolute() and Path(s).is_file():
            p = Path(s).resolve()
        elif s.startswith("/data/"):
            p = (data / s[len("/data/"):]).resolve()
        elif s.startswith("/media/"):
            p = (data / "media" / s[len("/media/"):]).resolve()
        elif s.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", s):
            return None                    # 绝对路径但文件不在，不做二次解释
        else:
            return None
        p.relative_to(data)
        # 只允许素材与转码缓存，basemaps / garmin.db 一律不碰
        if p.parent.name not in ("media", "motion_cache"):
            return None
        if p.suffix.lower() not in (".mp4", ".mov", ".m4v"):
            return None
        return p if p.is_file() else None
    except (OSError, ValueError):
        return None


def motion_audio(run: str) -> dict:
    """给原生侧的混音清单：把 `/__timeline` 回传的素材窗口翻译成磁盘文件 + 起点。

    手机端的 `MotionEncoder` 没有 ffmpeg，靠 MediaCodec 自己解码原声；它需要知道
    每段素材的**本地文件**和在成片里的**起始秒**。页面只给 URL 和窗口位置，
    这里做 URL→路径的映射与白名单校验，顺便丢掉图片段（dur=0）、越界和丢文件的。

    返回 `items: [{file, at, dur}]`；空列表是正常情况（没加视频素材 / 导出失败），
    原生侧据此退回原来的无音轨编码。
    """
    run = str(run or "")
    if not _RUN_RE.fullmatch(run):
        return {"ok": False, "error": "run 不合法"}
    f = OUT / "_frames" / run / "_timeline.json"
    if not f.is_file():
        return {"ok": True, "items": [], "total": 0}
    try:
        rows = json.loads(f.read_text("utf-8"))
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"时间轴读不出来：{e}"}
    items: list[dict] = []
    for w in rows if isinstance(rows, list) else []:
        if not isinstance(w, dict):
            continue
        try:
            dur = float(w.get("dur") or 0)
        except (TypeError, ValueError):
            continue
        if dur <= 0:
            continue                     # 图片素材没有音轨
        p = _audio_local(str(w.get("file") or w.get("src") or ""))
        if p is None:
            continue
        try:
            at = max(0.0, float(w.get("a") or 0) + float(w.get("lead") or 0))
        except (TypeError, ValueError):
            at = 0.0
        items.append({"file": str(p), "at": round(at, 3), "dur": round(dur, 3)})
    items.sort(key=lambda x: x["at"])
    return {"ok": True, "items": items, "total": len(items)}


def motion_cleanup(run: str) -> dict:
    """出片后清掉帧序列（几百张 JPEG 很占地方）。"""
    run = str(run or "")
    if not _RUN_RE.fullmatch(run):
        return {"ok": False, "error": "run 不合法"}
    shutil.rmtree(OUT / "_frames" / run, ignore_errors=True)
    return {"ok": True}


def clips_list() -> dict:
    """素材清单。顺便把拍摄时间/真实时长探出来，界面上要显示给用户看。

    文件可能被外部删掉（清缓存、换机），所以每条都带 `exists`，
    界面据此标「文件已丢失」而不是让人对着一条点不动的记录发呆。
    """
    MEDIA.mkdir(parents=True, exist_ok=True)
    items = _clips_read()
    repairs: dict[str, tuple] = {}
    for it in items:
        p = Path(str(it.get("path") or ""))
        it["kind"] = it.get("kind") or _kind_of(p)
        it["exists"] = p.is_file()
        if not it["exists"]:
            it["t_why"] = "文件已丢失"
            continue
        info = _probe_material(p, name_hint=str(it.get("name") or ""))
        if it.get("t_utc") is None and info.get("t_utc"):
            # 老版本登记时探不到时间。现在能从 udta / 原始文件名补回来的，
            # 直接落盘，免得界面一直显示「加入」，回放也一直按数量均分。
            key = str(it.get("id") or it.get("path") or "")
            repairs[key] = (info["t_utc"], info.get("t_source") or "")
            it["t_utc"], it["t_source"] = info["t_utc"], info.get("t_source") or ""
        it["t_utc"] = _meta_val(it, info, "t_utc")
        it["t_source"] = it.get("t_source") or info.get("t_source") or ""
        it["t_why"] = "" if it.get("t_utc") else (info.get("t_why") or "读不到拍摄时间")
        it["w"] = it.get("w") or info.get("w") or 0
        it["h"] = it.get("h") or info.get("h") or 0
        # 视频用文件的真实时长：登记时传的是剪辑结果，可能和时间轴差几十毫秒
        if info.get("dur"):
            it["dur"] = round(float(info["dur"]), 2)
    if repairs:
        # 只把补到的两个字段写回原始记录，别把 exists/t_why 这些临时字段落盘
        raw = _clips_read()
        for x in raw:
            got = repairs.get(str(x.get("id") or x.get("path") or ""))
            if got and x.get("t_utc") is None:
                x["t_utc"], x["t_source"] = got
        _clips_write(raw)
    return {"ok": True, "items": items, "dir": str(MEDIA)}


def _kind_of(p: Path) -> str:
    return "image" if p.suffix.lower() in (
        ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif") else "video"


def clips_add(path: str, name: str = "", dur: float = 0,
              w: int = 0, h: int = 0, kind: str = "",
              probe_from: str = "") -> dict:
    """登记一段素材。视频的剪辑由原生侧用 MediaExtractor/Muxer 无损完成，
    图片直接拷进来即可；这里只记录结果（文件应落在 `data/media/` 下）。

    `probe_from` 是**剪辑前的源文件**：视频一旦被 MediaMuxer 重封装，
    `mvhd.creation_time` 就没了（实测转码/重封装后的 mp4 一律读不到），
    只探成品会永远拿不到拍摄时间，回放里就只能按数量均匀铺开、位置不准。
    所以在登记时就把源文件的拍摄时间/坐标读出来存进记录，
    之后 `clips_list` / `_clips_for_motion` 一律优先用存下来的值。
    """
    p = Path(str(path or ""))
    if not p.is_file():
        return {"ok": False, "error": "素材文件不存在"}
    items = _clips_read()
    cid = f"c{secrets.token_hex(4)}"
    k = kind or _kind_of(p)
    if k == "image" and dur <= 0:
        dur = STILL_SEC
    # 源文件优先：它才带得住拍摄时间。name 是原始文件名，
    # 即使源片自己没写 creation_time，也能从 VID_20261103_180036 兜出来。
    src = Path(str(probe_from)) if probe_from else p
    meta = _probe_material(src, name_hint=str(name)) if src.is_file() else {}
    fallback = _probe_material(p, name_hint=str(name))
    t_utc = meta.get("t_utc") if meta.get("t_utc") is not None else fallback.get("t_utc")
    t_src = ((meta.get("t_source") or "") if meta.get("t_utc") else "") \
        or fallback.get("t_source") or ""
    t_why = "" if t_utc else (meta.get("t_why") or fallback.get("t_why") or "读不到拍摄时间")
    # 把时间写回成品的 mvhd：以后就算 clips.json 丢了，也还能从文件本身读回来。
    if t_utc and k == "video":
        from . import clip as clip_mod
        clip_mod.stamp_creation(p, int(t_utc))
    items.append({
        "id": cid, "path": str(p), "name": (name or p.stem)[:60],
        "kind": k,
        "dur": round(float(dur or 0), 2), "w": int(w or 0), "h": int(h or 0),
        "t_utc": t_utc, "t_source": t_src, "t_why": t_why,
        "lat": meta.get("lat") if meta.get("lat") is not None else fallback.get("lat"),
        "lon": meta.get("lon") if meta.get("lon") is not None else fallback.get("lon"),
        "added_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    _clips_write(items)
    return {"ok": True, "id": cid, "items": items}


def clips_remove(clip_id: str) -> dict:
    items = _clips_read()
    keep, gone = [], None
    for it in items:
        if it.get("id") == clip_id:
            gone = it
        else:
            keep.append(it)
    if gone is None:
        return {"ok": False, "error": "没有这段素材"}
    _clips_write(keep)
    try:
        Path(str(gone.get("path") or "")).unlink()
    except OSError:
        pass
    return {"ok": True, "items": keep}


def read_html(path: str) -> dict:
    """把产物原文交给 WebView（路径必须落在 output/ 里，避免被当成任意文件读取）。"""
    p = Path(path).resolve()
    try:
        p.relative_to(OUT.resolve())
    except ValueError:
        return {"ok": False, "error": "只允许读 output/ 下的文件"}
    if not p.is_file():
        return {"ok": False, "error": f"文件不存在：{p.name}"}
    return {"ok": True, "html": p.read_text(encoding="utf-8", errors="replace")}


def start_server(port: int = 0) -> dict:
    """在 127.0.0.1 起只读服务，返回实际端口。App 把 WebView 指过去。

    只绑回环地址：手机上没有「局域网」这回事，暴露出去只有风险。
    """
    from functools import partial
    from http.server import ThreadingHTTPServer

    from .serve import _Handler

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port),
                                  partial(_Handler, directory=str(OUT)))
    except OSError as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    srv.token = ""          # type: ignore[attr-defined]
    srv.media_dir = str(HOME / "data" / "media")   # type: ignore[attr-defined]
    srv.data_dir = str(HOME / "data")              # type: ignore[attr-defined]
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return {"ok": True, "port": srv.server_address[1],
            "url": f"http://127.0.0.1:{srv.server_address[1]}/"}


def recent(limit: int = 20) -> dict:
    """近期产物清单，给 App 的「历史」页用。"""
    from .serve import _title

    items = []
    if OUT.is_dir():
        for p in OUT.iterdir():
            if not p.is_file() or p.suffix.lower() not in (".html", ".png", ".mp4"):
                continue
            if p.name.startswith("_") or p.stat().st_size == 0:
                continue
            title, group, kind = _title(p.name)
            items.append({"name": p.name, "title": title, "group": group, "kind": kind,
                          "mtime": int(p.stat().st_mtime), "size": p.stat().st_size})
    items.sort(key=lambda x: -x["mtime"])
    return {"ok": True, "items": items[:limit]}


# ---------------------------------------------------------------- 自测
def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    call = {
        "env": env_info,
        "status": status,
        "dashboard": dashboard,
        "card": card_html,
        "recent": recent,
    }.get(cmd)
    if not call:
        print(f"用法：python -m garmin_cn.appapi [{'|'.join(['env', 'status', 'dashboard', 'card', 'recent'])}]")
        return 2
    print(json.dumps(call(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
