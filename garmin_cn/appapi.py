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
    "start_server", "recent", "read_html",
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
        from .actcard import H, W, build_html

        mode = PV.normalize(privacy)
        _, mask_info = PV.mask_track(detail.get("track") or [], mode)
        html = build_html(row, detail, None, None, title, privacy=mode)
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
