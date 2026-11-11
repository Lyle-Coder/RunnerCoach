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
import sqlite3
import sys
import threading
import time
import zipfile
from pathlib import Path

from . import __version__
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


def _client(email: str | None = None, password: str | None = None,
            revalidate: bool = False,
            min_interval: float = 0.5) -> GarminCNClient:
    from .net import Net

    auth = GarminCNAuth(HOME)
    net = Net(auth.session_path, min_interval=min_interval)
    ok = bool(net.get_cookie("JWT_WEB") or net.get_cookie("SESSIONID"))

    # 有票据不等于票据还有效：Garmin 的 JWT 几小时就会失效，服务端直接 401。
    # 旧版只看「有没有」，于是带着死票据跑完整轮同步才失败，用户看到的就是
    # 「关掉再开一刷新就要重新登录」。这里先花一次轻量请求验一下，失效且
    # 本地存了凭据就当场续期，不用再让用户手输密码。
    if ok and revalidate:
        if not auth.ensure_session():
            ok = False
        else:
            # 校验过程中服务端可能轮换过 Cookie，重新从磁盘读一次最新值
            net = Net(auth.session_path, min_interval=min_interval)

    if not ok and email and password:
        auth.login(email, password)      # 可能要验证码，交给上层处理
        net = Net(auth.session_path, min_interval=min_interval)
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
    "history_stats", "backfill_next",
    "plan", "set_amap_key", "start_server", "recent", "read_html",
    "db_export", "db_import", "diag_text",
    "motion_html", "motion_status", "motion_cleanup", "motion_audio",
    "clips_list", "clips_add", "clips_remove", "clips_reprobe",
    "clips_remove_many", "clips_update",
    "set_llm_config", "get_llm_config", "ai",
    "mi_status", "mi_login", "mi_captcha", "mi_send_code", "mi_verify_code",
    "mi_logout", "mi_sync",
    "hw_status", "hw_import", "hw_login", "hw_probe", "hw_sync", "hw_logout",
    "hw_login_start", "hw_login_finish", "hw_login_cancel",
    "body_sources", "body_set_source", "check_update",
    "account_info", "garmin_logout",
    "coros_status", "coros_login", "coros_logout", "coros_sync",
    "coros_mcp_status", "coros_mcp_login_url", "coros_mcp_finish",
    "coros_mcp_sleep", "coros_mcp_logout",
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
        # 异常在 App 里只表现为一句 toast，用户复述不清。留一份到 diag.log，
        # 「我的 → 诊断日志」能一键复制，远程排查才有依据。
        from . import diag

        diag.log_exc(f"api:{name}", e)
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

    today_info = _today(store) if st.get("days") else None
    last_sync = store.get_meta("last_sync")
    # 「补全历史」是后台慢慢往前挪的游标：退到下限之前都算还欠着。
    # 下限不再固定 8 年，而是数据源里最早那条活动（缓存值，这里不联网探测）。
    _cursor = store.get_meta(BACKFILL_META)
    try:
        _cur_day = dt.date.fromisoformat(str(_cursor)) if _cursor else None
    except ValueError:
        _cur_day = None
    _earliest = _earliest_cached(store)
    need_bf = (_cur_day is None
               or _cur_day > _backfill_floor(dt.date.today(), _earliest))
    # 高驰单独一条游标：以前只有佳明会补历史，高驰用户刷新多少次都只有 30 天
    if _coros_session_ok():
        _c_cur = store.get_meta(COROS_BACKFILL_META)
        try:
            _c_day = dt.date.fromisoformat(str(_c_cur)) if _c_cur else None
        except ValueError:
            _c_day = None
        _c_ear = store.get_meta(COROS_EARLIEST_META)
        if (_c_day is None
                or _c_day > _backfill_floor(dt.date.today(), _c_ear)):
            need_bf = True
    try:
        store.conn.close()
    except Exception:
        pass

    return {
        "ok": True,
        "logged_in": session_ok,
        "email": _cfg().get("email"),
        "days": st.get("days", 0),
        "activities": st.get("activities", 0),
        "first": st.get("first"),
        "last": st.get("last"),
        "last_sync": last_sync,
        "today": today_info,
        "needs_backfill": need_bf,
        # 数据源里最早一条活动的日期（空 = 还没有记录）；界面用它说明「补到哪为止」
        "earliest_record": _earliest or "",
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


def body_sources() -> dict:
    from . import body as B
    store = Store(DB)
    try:
        return {"ok": True, **B.source_settings(store)}
    finally:
        store.conn.close()


def body_set_source(source: str) -> dict:
    from . import body as B
    store = Store(DB)
    try:
        B.set_preferred_source(store, source)
        return {"ok": True, **B.source_settings(store)}
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    finally:
        store.conn.close()


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


# ---------------------------------------------------------------- 华为运动健康
# 与小米同一套交互：状态 → 登录/导入授权 → 同步 → 退出。
# 差别只在闸门：华为没走完协议验证时，`hw_sync` 会明确拒绝，不会装作同步成功。
_HW_LOGIN: dict = {"state": "", "started": 0, "lock": threading.Lock()}


def hw_login_start() -> dict:
    import secrets
    import time
    from . import huawei as H

    with _HW_LOGIN["lock"]:
        state = secrets.token_urlsafe(32)
        _HW_LOGIN.update(state=state, started=time.monotonic())
    return {"ok": True, "url": H.login_url(state), "redirect_uri": H.LOGIN_REDIRECT}


def hw_login_cancel() -> dict:
    with _HW_LOGIN["lock"]:
        _HW_LOGIN.update(state="", started=0)
    return {"ok": True}


def hw_login_finish(callback: str) -> dict:
    import time
    from . import huawei as H

    with _HW_LOGIN["lock"]:
        state, started = _HW_LOGIN["state"], _HW_LOGIN["started"]
        _HW_LOGIN.update(state="", started=0)  # 一次性消费，拒绝重复回调
    if not state or time.monotonic() - started > 900:
        return {"ok": False, "error": "登录已过期，请重新登录"}
    try:
        code = H.login_code(callback, state)
        cloud = H.HuaweiCloud(HOME)
        cloud.login(code)
        return {"ok": True, "logged_in": True, "open_id": _mask_uid(cloud.uid)}
    except H.HwError as e:
        return {"ok": False, "error": str(e)}
    except Exception:
        return {"ok": False, "error": "华为登录未完成，请重试"}


def hw_status() -> dict:
    """华为登录态与协议状态（只看本地文件，不联网）。"""
    try:
        from . import huawei as H

        s = H.status(HOME)
        return {"ok": True, **s, "open_id": _mask_uid(s.get("uid", ""))}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def hw_import(payload: str) -> dict:
    """导入会话 JSON 文本（healthsession 的 obtain 响应即可）。"""
    import json as _json
    try:
        from . import huawei as H

        try:
            d = _json.loads(payload or "")
        except Exception:
            return {"ok": False, "error": "这不是 JSON：请粘贴华为会话响应"}
        r = H.HuaweiCloud(HOME).import_payload(d)
        return {"ok": True, "open_id": _mask_uid(r["uid"]),
                "has_refresh": r["hasRefresh"]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def hw_login(user: str, password: str, client_id: str = "") -> dict:
    """密码模式已证实走不通（App 的 client_id 不给密码授权），保留入口只为给出明确提示。"""
    return {"ok": False, "need_verify": False,
            "error": "华为不支持密码模式。请在 App 里登录一次，用 hw import 导入会话；"
                     "或直接抓包后 hw import --capture <HAR>"}


def hw_probe() -> dict:
    """跑三道闸门：账号 / 体重读取 / 会话续期，逐项报告卡在哪。"""
    try:
        from . import huawei as H

        cloud = H.HuaweiCloud(HOME)
        cloud.load()
        return {"ok": True, **cloud.probe()}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def hw_sync(days: int = 0) -> dict:
    """拉一次华为体重并入库（body 表 + 回填 daily）。"""
    from . import huawei as H

    store = Store(DB)
    try:
        n, recs = H.sync(store, HOME, days=max(int(days), 0))
    except H.HwAuthError as e:
        return {"ok": False, "need_login": True, "error": str(e)}
    except H.HwError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        store.conn.close()
    return {"ok": True, "count": n,
            "latest": recs[-1]["date"] if recs else None,
            "weight": recs[-1].get("weight_kg") if recs else None}


def hw_logout() -> dict:
    """删除 data/huawei_auth.json（云端数据不受影响）。"""
    try:
        from . import huawei as H

        H.HuaweiCloud(HOME).clear()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- 高驰
def _mask_mail(mail: str | None) -> str:
    """邮箱只露首字母：截图/录屏时别把人家的完整邮箱带出去。"""
    t = str(mail or "")
    if "@" not in t:
        return t
    head, _, domain = t.partition("@")
    if len(head) <= 2:
        return head[:1] + "*@" + domain
    return head[:2] + "***@" + domain


def _src_stats(source: str) -> dict:
    """某个数据源在本地库里的覆盖：活动数、首末日期、最近同步时间。"""
    try:
        store = Store(DB)
        try:
            row = store.conn.execute(
                "SELECT COUNT(*) AS n, MIN(date) AS first, MAX(date) AS last "
                "FROM activities WHERE source = ?", (source,)).fetchone()
            days = store.conn.execute(
                "SELECT COUNT(*) AS n FROM daily").fetchone()
            return {"activities": int(row["n"] or 0) if row else 0,
                    "first": (row["first"] if row else "") or "",
                    "last": (row["last"] if row else "") or "",
                    "days": int(days["n"] or 0) if days else 0,
                    "last_sync": store.get_meta(
                        "last_sync_coros" if source == "coros" else "last_sync") or ""}
        finally:
            store.conn.close()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _garmin_device() -> str:
    """佳明设备名（例如 Forerunner 570）。取不到就空串，不阻断任何流程。

    设备列表接口不常变，但每开一次卡片就联网没必要：缓存 7 天。
    """
    store = Store(DB)
    try:
        cached = store.get_meta("garmin_device")
        at = store.get_meta("garmin_device_at")
        if cached is not None and at:
            try:
                ts = dt.datetime.strptime(str(at), "%Y-%m-%d %H:%M:%S")
                if (dt.datetime.now() - ts).total_seconds() < 7 * 86400:
                    return str(cached)
            except ValueError:
                pass
    finally:
        store.conn.close()
    name = ""
    try:
        client = _client(min_interval=0.5)
        name = client.primary_device() or ""
    except Exception:
        return ""
    store = Store(DB)
    try:
        store.set_meta("garmin_device", name)
        store.set_meta("garmin_device_at",
                       dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    finally:
        store.conn.close()
    return name


def account_info(source: str = "garmin") -> dict:
    """账号卡片：已登录时点账号行看这个，**不再弹登录框**。

    只做只读状态汇总：账号、设备、会话能否续期、本地覆盖范围。
    换账号/退出走卡片上的按钮，不再要求用户重输一遍密码。
    """
    src = "coros" if str(source or "").lower().startswith("coros") else "garmin"
    stats = _src_stats(src)
    if src == "coros":
        from . import coros as C

        cloud = C.CorosCloud(HOME)
        ok = bool(cloud.load())
        return {"ok": True, "source": "coros", "logged_in": ok,
                "account": _mask_uid(cloud.user_id) if ok else "",
                "label": "COROS 高驰",
                "region": C.REGION_CN.get(cloud.region, cloud.region) if ok else "",
                "device": "高驰手表（由 Training Hub 提供）" if ok else "",
                "session": "token 过期后自动续期" if ok else "",
                **stats}
    cfg = _cfg()
    ok = _garmin_session_ok()
    return {"ok": True, "source": "garmin", "logged_in": ok,
            "account": _mask_mail(cfg.get("email")) if ok else "",
            "label": "Garmin 佳明",
            "region": "国区",
            "device": _garmin_device() if ok else "",
            "session": "会话可用" if ok else "会话已失效，需要重新登录",
            **stats}


def garmin_logout() -> dict:
    """退出佳明账号：删会话文件 + 清配置里的邮箱。**不动数据库**。"""
    cfg_path = HOME / "config.json"
    try:
        auth = GarminCNAuth(HOME)
        if auth.session_path.exists():
            auth.session_path.unlink()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    try:
        import json as _json

        if cfg_path.exists():
            cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg.pop("email", None)
            cfg.pop("password", None)
            cfg_path.write_text(_json.dumps(cfg, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    except Exception:
        pass
    return {"ok": True}


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


_MCP_PENDING: dict[str, object] = {}


def coros_mcp_status() -> dict:
    """官方 MCP（睡眠）的授权状态。只看本地 token，不联网。"""
    try:
        from . import coros_mcp as MC

        cloud = MC.CorosMcp(HOME, "asia")
        st = cloud.status()
        return {"ok": True, **st,
                "region_cn": MC.REGION_CN.get(st["region"], st["region"])}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def coros_mcp_login_url(region: str = "asia") -> dict:
    """生成授权页地址。verifier 留在进程里，回调回来后用 coros_mcp_finish 收尾。"""
    try:
        from . import coros_mcp as MC

        cloud = MC.CorosMcp(HOME, region or "asia")
        url = cloud.authorize_url()
        _MCP_PENDING["cloud"] = cloud
        return {"ok": True, "url": url, "callback": MC.CALLBACK,
                "region": cloud.region,
                "region_cn": MC.REGION_CN.get(cloud.region, cloud.region)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def coros_mcp_finish(callback: str) -> dict:
    """把 WebView 截到的回调地址换成 token。"""
    try:
        from . import coros_mcp as MC

        cloud = _MCP_PENDING.get("cloud")
        if not isinstance(cloud, MC.CorosMcp):
            return {"ok": False, "error": "授权会话已失效，请重新点一次授权"}
        r = cloud.finish(callback)
        _MCP_PENDING.pop("cloud", None)
        return {"ok": True, **r}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def coros_mcp_sleep(days: int = 30) -> dict:
    """拉高驰睡眠并写入日汇总（merge，不动佳明已写的字段）。"""
    try:
        from . import coros_mcp as MC
        from .store import Store

        cloud = MC.CorosMcp(HOME, "asia")
        rows = cloud.sleep(days)
        store = Store(DB)
        for row in rows:
            store.upsert_daily(dict(row), merge=True)
        hrv = cloud.hrv(min(int(days or 7), 7))
        for day, ms in hrv.items():
            store.upsert_daily({"date": day, "hrv_night": ms}, merge=True)
        return {"ok": True, "sleep_days": len(rows), "hrv_days": len(hrv),
                "first": rows[0]["date"] if rows else "",
                "last": rows[-1]["date"] if rows else "",
                # 0 条时把原始返回带出去：日志/弹窗里能直接看到接口到底给了什么
                "raw": "" if rows else (getattr(cloud, "last_text", "") or "")[:600]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def coros_mcp_logout() -> dict:
    try:
        from . import coros_mcp as MC

        MC.CorosMcp(HOME, "asia").clear()
        _MCP_PENDING.pop("cloud", None)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


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
    # 授权过官方 MCP 的话顺带补睡眠：REST 里没有睡眠，这一步失败不影响同步结果
    sleep_days = 0
    try:
        from . import coros_mcp as MC

        if MC.CorosMcp(HOME, "asia").status()["authorized"]:
            sleep_days = int(coros_mcp_sleep(days).get("sleep_days") or 0)
    except Exception as e:
        from . import diag as _dg

        _dg.log_exc("coros_mcp.sleep", e)
    return {"ok": True, "daily": r.get("daily", 0), "activities": r.get("activities", 0),
            "range": r.get("range"), "relogged": r.get("relogged", False),
            "sleep_days": sleep_days}


# ---------------------------------------------------------------- 同步
def sync(days: int = 30, email: str | None = None, password: str | None = None,
         with_body: bool = True, progress=None, force: bool = False,
         end: str | None = None, activity_pages: int = 1,
         bulk: bool = False) -> dict:
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

    # 补历史走后台批量：间隔收紧到 BULK_MIN_INTERVAL；日常同步保持 0.5s
    _gap = BULK_MIN_INTERVAL if bulk else 0.5
    try:
        client = _client(email, password, revalidate=True, min_interval=_gap)
    except MfaRequired:
        return {"ok": False, "need_mfa": True, "error": "续期需要邮箱验证码，请重新登录"}
    except AuthError as e:
        return {"ok": False, "need_login": True, "error": str(e)}

    store = Store(DB)
    svc = SyncService(client, store)
    try:
        res = svc.sync(days=days, end=end, extras=False, progress=cb,
                       force=force, activity_pages=activity_pages, bulk=bulk)
    except SessionExpired:
        # 会话刚好过期：有凭据就重登一次再试，没有就让界面去登录
        if not (email and password):
            return {"ok": False, "need_login": True, "error": "会话已过期"}
        try:
            client = _client(email, password, min_interval=_gap)
            res = SyncService(client, store).sync(
                days=days, end=end, extras=False, progress=cb,
                force=force, activity_pages=activity_pages, bulk=bulk)
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
           "activities": res["activities"], "errors": res["errors"],
           # 数据源自己交代的「最早一条记录」：列表翻到底时它才是权威值，
           # 补历史靠它决定什么时候停。
           "earliest_activity": res.get("earliest_activity"),
           "list_exhausted": bool(res.get("list_exhausted"))}

    if with_body:
        try:
            from . import body as B

            n, recs = B.sync_preferred(store, HOME, days=min(max(days, 7), 120))
            out["body"] = {"count": n, "latest": recs[-1]["date"] if recs else None}
        except Exception as e:
            out["body"] = {"count": 0, "error": f"{type(e).__name__}: {e}"}

    return out


def sync_range(start: str, end: str | None = None,
               email: str | None = None, password: str | None = None,
               with_body: bool = True, force: bool = False,
               activity_pages: int | None = None,
               bulk: bool = False) -> dict:
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
               end=e.isoformat(), activity_pages=pages, bulk=bulk)
    if out.get("ok"):
        out["start"] = s.isoformat()
        out["end"] = e.isoformat()
        out["activity_pages"] = pages
    return out


# ---------------------------------------------------------------- 产出
def _refresh_live(store, date: str) -> None:
    """出图/出报告前刷新一次「会随时间变」的指标（身体电量）。

    `daily` 里的 `bb_high / bb_low` 来自 daily summary，是当天第一次同步的
    快照；「此刻还剩多少」只存在于 bodyBattery 的日内序列里。CLI 出图前本来
    就会拉，App 侧以前没拉 —— 于是手机上一图流的身体电量永远是「未测量」。
    没登录 / 离线都静默跳过，退回库里上一次的值。

    两次提前退出，都是为了别让出图白等一次网络往返（实测一次约 280ms）：
    - **历史日期**：昨天以前的卡不存在「此刻」，库里已有曲线就直接用；
    - **TTL 内**：刚刚拉过（连点两次导出最常见），连客户端都不建。
    """
    from . import live as L

    if date < dt.date.today().isoformat():
        if L._load_series((store.day(date) or {}).get("bb_series")):
            return

    last = store.get_meta(f"bb_fetch:{date}")
    if last:
        try:
            age = (dt.datetime.now()
                   - dt.datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).total_seconds()
            if 0 <= age < L.TTL:
                return
        except ValueError:
            pass

    client = None
    try:
        client = _client()
    except Exception:
        client = None
    try:
        L.body_battery(store, date, client)
    except Exception:
        pass


def dashboard(date: str | None = None, live: bool = True) -> dict:
    """出网页看板，返回文件路径（App 用 WebView 直接读它，或走 start_server）。"""
    store = Store(DB)
    if store.stats()["days"] == 0:
        return {"ok": False, "error": "还没有数据，先同步一次"}

    d = date or dt.date.today().isoformat()
    if live:
        _refresh_live(store, d)
        try:
            from . import live as L

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

    _refresh_live(store, d)

    try:
        res = attach(derive(store, d))
        from .report import source_badge

        # 一图流是「点一下就要出图」的场景，等太久会显得卡住。
        # 这里最多等 6 秒把 AI 点评补上；等不到就不放这段文字，
        # 图本身照常出——点评是锦上添花，不能拖累出图。
        ai_note = ""
        try:
            from . import aicoach

            ai_note = (aicoach.coach_note(d, wait=6) or {}).get("text") or ""
        except Exception:
            ai_note = ""

        _label, _ts = source_badge(store)
        html = build_html(res,
                          sync_at=store.get_meta("last_sync") or store.get_meta("last_sync_coros"),
                          src=_label, ai_note=ai_note)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    out = OUT / f"card-{d}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    # width/height 是 HTML 的布局基准，旧 APK 仍按它建 View；export_* 是
    # 新 APK 用的成品画幅契约（9:16），两者含义不同，别混用。
    return {"ok": True, "html": str(out), "date": d, "width": 1080, "height": 1920,
            **export_spec(BG_CARD, trim=False)}


# ---------------------------------------------------------------- 历史补全
# 「历史补全」以前是「我的」里的一个按钮，选区间再点确定。问题是它点开只有一个
# 弹窗、跑完也只是「跳过 90 天缓存」，用户看完只觉得没反应。现在改成**游标式**：
# 每次刷新由近到远补一段，游标存进 meta，直到回到最早年为止，全程后台。
BACKFILL_META = "backfill_cursor"
# 一段半年：单段 180 天 ≈ 360 次请求，按批量节流约 1.5 分钟；
# 以前一年一段是 700+ 次、4 分钟以上，用户在这段时间里看不到任何新数据。
BACKFILL_SPAN_DAYS = 180
# 批量拉历史时的最小请求间隔（秒）。日常同步仍是 0.5s——交互场景宁可慢一点
# 也不要被限流；补历史是后台任务，密一点但保留 429 退避（net.py 里会把间隔
# 自动放大），整体快 2 倍。
BULK_MIN_INTERVAL = 0.25
BACKFILL_MAX_YEARS = 8        # 硬下限：账号再老也不翻 8 年以上
BACKFILL_PAGES = 20           # 每段最多翻 20 页活动（约 1200 条）

# 「数据到哪天为止」不再按年数猜，而是问数据源：活动列表翻到底时，
# 最早那条活动就是账号的第一条记录。缓存 30 天，避免每次刷新都翻一遍列表。
EARLIEST_META = "earliest_activity"      # 空串 = 账号里一条活动都没有
EARLIEST_AT_META = "earliest_activity_at"
EARLIEST_TTL = 30 * 86400
EARLIEST_PROBE_PAGES = 60                # 最多翻 60 页（3600 条活动）
EMPTY_STREAK_META = "backfill_empty_streak"
# 高驰自己一套游标：口径与佳明一样，但区间更短（90 天），因为它一次要拉的
# 是「区间日汇总」，一年请求在手机上又慢又容易被限流。
COROS_BACKFILL_META = "backfill_cursor_coros"
COROS_EARLIEST_META = "earliest_activity_coros"
COROS_EARLIEST_AT_META = "earliest_activity_coros_at"
COROS_EMPTY_META = "backfill_empty_streak_coros"
COROS_SPAN_DAYS = 90
# 账号里一条活动都没有时的下限：日汇总（步数/睡眠）仍有意义，但没必要翻八年
BACKFILL_NO_ACT_DAYS = 730


def _hard_floor(today: dt.date | None = None) -> dt.date:
    t = today or dt.date.today()
    try:
        return t.replace(year=t.year - BACKFILL_MAX_YEARS)
    except ValueError:                 # 2 月 29 日
        return t.replace(year=t.year - BACKFILL_MAX_YEARS, day=28)


def _backfill_floor(today: dt.date | None = None,
                    earliest: str | None = None) -> dt.date:
    """补到哪天为止。有「数据源里的第一条记录」就以它为界。

    `earliest` 为空串表示「账号里确实没有活动」，这时只往前补两年日汇总；
    `None` 表示还不知道，退回 8 年硬下限。
    """
    t = today or dt.date.today()
    hard = _hard_floor(t)
    if earliest is None:
        return hard
    if not earliest:
        return max(hard, t - dt.timedelta(days=BACKFILL_NO_ACT_DAYS))
    try:
        e = dt.date.fromisoformat(str(earliest)[:10])
    except ValueError:
        return hard
    # 留一天余量：最后一段要能包含第一条记录当天
    return max(hard, e - dt.timedelta(days=1))


def _earliest_cached(store) -> str | None:
    """读缓存的「第一条记录」。只在 meta 里有记录时返回（可能是空串）。"""
    v = store.get_meta(EARLIEST_META)
    return None if v is None else str(v)


def _earliest_fresh(store) -> bool:
    at = store.get_meta(EARLIEST_AT_META)
    if not at:
        return False
    try:
        ts = dt.datetime.strptime(str(at), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return (dt.datetime.now() - ts).total_seconds() < EARLIEST_TTL


def _probe_earliest(store, max_pages: int = EARLIEST_PROBE_PAGES) -> str | None:
    """问数据源「最早一条活动是哪天」，成功就写进 meta。

    只在缓存过期时调用，命不中（未登录 / 网络不通）就沿用旧值，
    不因为这一点失败影响补全本身。
    """
    try:
        client = _client()
        got = client.activities_earliest(max_pages=max_pages)
    except Exception:
        return _earliest_cached(store)
    if not got.get("exhausted"):
        # 没翻到底就说明更早还有，这个值不能当停止线
        return _earliest_cached(store)
    val = got.get("date") or ""
    try:
        store.set_meta(EARLIEST_META, val)
        store.set_meta(EARLIEST_AT_META,
                       dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        pass
    return val


def _backfill_segment(cur: dt.date, floor: dt.date,
                      span: int = BACKFILL_SPAN_DAYS):
    """算出这一段该补哪一截：返回 (start, end, next_cursor, done)。

    纯函数，不碰网络也不碰库 —— 自检直接测它，就不必为了验一段逻辑
    真去拉一年数据。
    """
    if cur <= floor:
        return cur, cur, floor, True
    start = max(floor, cur - dt.timedelta(days=max(1, int(span)) - 1))
    nxt = start - dt.timedelta(days=1)
    if nxt < floor:
        nxt = floor
    return start, cur, nxt, nxt <= floor


def _segment_has_data(store, start: str, end: str) -> bool:
    """这一段区间里，库里到底有没有真数据。

    sync 会给每一天写一行（哪怕全空），所以不能拿「拉了几行」判断；
    只看有意义的三项指标，外加活动表。
    """
    try:
        r = store.conn.execute(
            "SELECT COUNT(*) FROM daily WHERE date BETWEEN ? AND ? AND "
            "(steps IS NOT NULL OR sleep_total_min IS NOT NULL OR resting_hr IS NOT NULL)",
            (start, end)).fetchone()
        if r and r[0]:
            return True
        a = store.conn.execute(
            "SELECT COUNT(*) FROM activities WHERE date BETWEEN ? AND ?",
            (start, end)).fetchone()
        return bool(a and a[0])
    except Exception:
        return True          # 查不出来就当有数据，宁可多补一段也不要漏


def _garmin_session_ok() -> bool:
    """本地有没有佳明会话票据（不看是否过期，过期由 sync 自己报）。"""
    try:
        from .net import Net

        path = GarminCNAuth(HOME).session_path
        if not path.exists():
            return False
        net = Net(path)
        return bool(net.get_cookie("JWT_WEB") or net.get_cookie("SESSIONID"))
    except Exception:
        return False


def _coros_session_ok() -> bool:
    try:
        from .coros import CorosCloud

        return bool(CorosCloud(HOME).load())
    except Exception:
        return False


def _backfill_garmin(span_days: int = BACKFILL_SPAN_DAYS) -> dict:
    """由近到远补**一段**历史，返回这一段的结果和「是否已经补到头」。

    游标 `meta.backfill_cursor` 记住「下次该从哪天往前补」，默认今天；
    每段跑完把游标往前挪一年，刷新几次就把全部历史补齐。

    **补到哪为止由数据源决定**：账号第一条活动就是终点。以前只认「往前退
    8 年」，哪怕注册才两年也会一年年空拉（每段 365 次日汇总），用户看到的
    就是「一直在拉」。现在的顺序是：
      1. 活动列表翻到底 → 最早那条活动（缓存 30 天，不重复翻）；
      2. 本段同步时如果刚好翻到底，顺手更新（比专门探测更省）；
      3. 都拿不到时退回 8 年，并加一条兜底：连续两段完全没数据就收工。
    """
    today = dt.date.today()
    store = Store(DB)
    try:
        earliest = _earliest_cached(store)
        fresh = _earliest_fresh(store)
        raw = store.get_meta(BACKFILL_META) or today.isoformat()
        try:
            streak = int(store.get_meta(EMPTY_STREAK_META) or 0)
        except (TypeError, ValueError):
            streak = 0
    finally:
        store.conn.close()
    try:
        cur = dt.date.fromisoformat(str(raw))
    except ValueError:
        cur = today
    if cur > today:
        cur = today

    floor = _backfill_floor(today, earliest)
    # 只有「确实还要补」时才去探测：已经补到头的库每次刷新都不该联网。
    if cur > floor and not fresh:
        store = Store(DB)
        try:
            earliest = _probe_earliest(store)
        finally:
            store.conn.close()
        floor = _backfill_floor(today, earliest)

    if cur <= floor:
        return {"ok": True, "done": True, "ran": False, "start": "", "end": "",
                "fetched": 0, "skipped": 0, "cursor": floor.isoformat(),
                "floor": floor.isoformat(), "earliest": earliest or "",
                "reason": "reached_earliest" if earliest else "reached_floor"}

    if not _garmin_session_ok():
        # 没登录就只能算状态、不能拉数据：让高驰那条线继续跑，别被这里卡住
        return {"ok": True, "done": True, "ran": False, "fetched": 0,
                "skipped": 0, "start": "", "end": "", "earliest": earliest or "",
                "cursor": cur.isoformat(), "floor": floor.isoformat(),
                "reason": "no_garmin"}

    start, end, nxt, done = _backfill_segment(cur, floor, span_days)
    out = sync_range(start.isoformat(), end.isoformat(),
                     with_body=False, force=False, activity_pages=BACKFILL_PAGES,
                     bulk=True)
    if not out.get("ok"):
        return {"ok": False, "done": False, "ran": True,
                "start": start.isoformat(), "end": cur.isoformat(),
                "need_login": bool(out.get("need_login")),
                "need_mfa": bool(out.get("need_mfa")),
                "error": out.get("error") or "补全失败"}

    store = Store(DB)
    reason = ""
    try:
        # 这一趟如果刚好翻到活动列表底，最早那条就是账号第一条记录：
        # 比专门探测更权威，顺手写进缓存，下一次起就不再翻列表了。
        if out.get("list_exhausted"):
            got = out.get("earliest_activity") or ""
            if got or earliest is None:
                store.set_meta(EARLIEST_META, str(got))
                store.set_meta(EARLIEST_AT_META,
                               dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                earliest = str(got)
                floor = _backfill_floor(today, earliest)
                if nxt < floor:
                    nxt = floor
                done = done or nxt <= floor
                if done:
                    reason = "reached_earliest" if earliest else "empty_account"

        # 兜底：探测拿不到时，连续两段一行数据都没有，就认为早于账号起点。
        # 只认「完全没有」——哪天只有步数没有活动，也算有数据。
        if _segment_has_data(store, start.isoformat(), end.isoformat()):
            streak = 0
        else:
            streak += 1
            if streak >= 2 and earliest is None:
                done = True
                reason = "empty_segments"
        store.set_meta(EMPTY_STREAK_META, str(streak))
        store.set_meta(BACKFILL_META, nxt.isoformat())
    finally:
        store.conn.close()

    return {"ok": True, "done": done, "ran": True,
            "start": start.isoformat(), "end": end.isoformat(),
            "fetched": out.get("fetched", 0), "skipped": out.get("skipped", 0),
            "activities": out.get("activities", 0),
            "cursor": nxt.isoformat(), "floor": floor.isoformat(),
            "earliest": earliest or "", "reason": reason}


def _fresh_at(store, key: str, ttl: int = EARLIEST_TTL) -> bool:
    at = store.get_meta(key)
    if not at:
        return False
    try:
        ts = dt.datetime.strptime(str(at), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return (dt.datetime.now() - ts).total_seconds() < ttl


def _days_in(store, start: str, end: str) -> int:
    try:
        row = store.conn.execute(
            "SELECT COUNT(*) AS n FROM daily WHERE date BETWEEN ? AND ?",
            (start, end)).fetchone()
        return int(row["n"] if row else 0)
    except Exception:
        return 0


def _backfill_coros(span_days: int = COROS_SPAN_DAYS) -> dict:
    """高驰的历史补全：与佳明同一套游标，区间 90 天。

    **以前补历史是佳明专用**：`backfill_next` 直接调佳明的 `sync_range`，
    高驰用户不管刷新多少次，都只有刷新时那 30 天（上限 180）——
    真机反馈「高驰只能拉到最近 30 天」就是这个原因。

    高驰没有「列活动翻到底」那种便宜探测，停止线改用活动列表的 totalCount
    直接翻到最后一页（两次请求，缓存 30 天）；拿不到时退回「连续两段没数据」。
    """
    if not _coros_session_ok():
        return {"ok": True, "done": True, "ran": False, "fetched": 0,
                "skipped": 0, "start": "", "end": "", "earliest": "",
                "reason": "no_coros"}
    from . import coros as C
    from .coros import CorosAuthError

    today = dt.date.today()
    store = Store(DB)
    try:
        cached = store.get_meta(COROS_EARLIEST_META)      # "" / None / 日期
        raw = store.get_meta(COROS_BACKFILL_META) or today.isoformat()
        fresh = _fresh_at(store, COROS_EARLIEST_AT_META)
        try:
            streak = int(store.get_meta(COROS_EMPTY_META) or 0)
        except (TypeError, ValueError):
            streak = 0
    finally:
        store.conn.close()
    try:
        cur = dt.date.fromisoformat(str(raw))
    except ValueError:
        cur = today
    if cur > today:
        cur = today

    floor = _backfill_floor(today, cached)
    if cur > floor and not fresh:
        # 探测「最早一条活动」失败不该拖垮整段补历史：拿不到就沿用旧停止线
        # 继续补，下次刷新再试。真机上 /activity/query 的 startTime 是 int
        # 毫秒时间戳，以前会在这里抛 TypeError 一直冒到 backfill_next。
        try:
            got = C.earliest_activity(HOME)      # 两次请求；结果缓存 30 天
        except Exception as e:
            from . import diag as _dg

            _dg.log_exc("coros.earliest", e)
            got = None
        if got is not None and (got or cached is None):
            store = Store(DB)
            try:
                store.set_meta(COROS_EARLIEST_META, got or "")
                store.set_meta(COROS_EARLIEST_AT_META,
                               dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            finally:
                store.conn.close()
            cached = got or ""
            floor = _backfill_floor(today, cached)

    if cur <= floor:
        return {"ok": True, "done": True, "ran": False, "start": "", "end": "",
                "fetched": 0, "skipped": 0, "cursor": floor.isoformat(),
                "floor": floor.isoformat(), "earliest": cached or "",
                "reason": "reached_earliest" if cached else "reached_floor"}

    start, end, nxt, done = _backfill_segment(cur, floor, span_days)
    store = Store(DB)
    reason = ""
    try:
        out = C.sync_range(store, HOME, start.isoformat(), end.isoformat())
        if _days_in(store, start.isoformat(), end.isoformat()) > 0:
            streak = 0
        else:
            streak += 1
            if streak >= 2 and cached is None:
                done = True
                reason = "empty_segments"
        store.set_meta(COROS_EMPTY_META, str(streak))
        store.set_meta(COROS_BACKFILL_META, nxt.isoformat())
    except CorosAuthError as e:
        return {"ok": False, "done": False, "ran": True,
                "start": start.isoformat(), "end": cur.isoformat(),
                "need_login": True, "error": str(e)}
    except Exception as e:
        return {"ok": False, "done": False, "ran": True,
                "start": start.isoformat(), "end": cur.isoformat(),
                "error": f"{type(e).__name__}: {e}"}
    finally:
        store.conn.close()

    daily = int(out.get("daily") or 0)
    new_days = int(out.get("new_days") or 0)
    return {"ok": True, "done": done, "ran": True,
            "start": start.isoformat(), "end": end.isoformat(),
            "fetched": new_days, "skipped": max(0, daily - new_days),
            "activities": out.get("activities", 0),
            "cursor": nxt.isoformat(), "floor": floor.isoformat(),
            "earliest": cached or "", "reason": reason}


def backfill_next(span_days: int = BACKFILL_SPAN_DAYS) -> dict:
    """补**一段**历史：佳明与高驰各推一步，两个都到终点才算 done。

    两个数据源各有自己的游标与停止线（佳明看活动列表最早一条，高驰看
    列表 totalCount），互不影响；只有一个账号时另一侧直接跳过。

    **单侧异常必须就地兜住**：以前任何一侧抛出去，异常会一路冒到 `call()`，
    另一侧已经跑完的结果也一起丢掉，用户只看到一句
    "TypeError: 'int' object is not subscriptable"。现在转成该侧的
    ok=false，另一侧照常推进游标。
    """
    def _guard(tag: str, fn) -> dict:
        try:
            return fn()
        except Exception as e:
            from . import diag as _dg

            _dg.log_exc(f"backfill:{tag}", e)
            return {"ok": False, "done": False, "ran": True,
                    "start": "", "end": "", "fetched": 0, "skipped": 0,
                    "error": f"{type(e).__name__}: {e}"}

    g = _guard("garmin", lambda: _backfill_garmin(span_days))
    c = _guard("coros", lambda: _backfill_coros())
    parts = [p for p in (g, c) if p.get("ran")]
    failed = [p for p in parts if not p.get("ok")]
    if failed:
        f = failed[0]
        # 失败也要带回两侧的明细：单侧炸掉时，另一侧其实已经推进了游标，
        # App 和诊断日志都得看得见，否则「补历史没反应」无从排查。
        return {"ok": False, "done": False,
                "start": f.get("start", ""), "end": f.get("end", ""),
                "need_login": any(p.get("need_login") for p in failed),
                "need_mfa": any(p.get("need_mfa") for p in failed),
                "error": f.get("error") or "补全失败",
                "garmin": {"done": g.get("done"), "ran": g.get("ran"),
                           "ok": g.get("ok"), "start": g.get("start"),
                           "end": g.get("end")},
                "coros": {"done": c.get("done"), "ran": c.get("ran"),
                          "ok": c.get("ok"), "start": c.get("start"),
                          "end": c.get("end")}}
    done = bool(g.get("done", True)) and bool(c.get("done", True))
    ran = parts[0] if parts else {}
    return {
        "ok": True, "done": done,
        "start": ran.get("start", ""), "end": ran.get("end", ""),
        "fetched": sum(int(p.get("fetched") or 0) for p in parts),
        "skipped": sum(int(p.get("skipped") or 0) for p in parts),
        "activities": sum(int(p.get("activities") or 0) for p in parts),
        "earliest": (g.get("earliest") or c.get("earliest") or ""),
        "cursor": ran.get("cursor", ""), "floor": ran.get("floor", ""),
        "reason": ",".join(x for x in (g.get("reason"), c.get("reason"))
                           if x and not str(x).startswith("no_")),
        "garmin": {"done": g.get("done"), "ran": g.get("ran"),
                   "start": g.get("start"), "end": g.get("end")},
        "coros": {"done": c.get("done"), "ran": c.get("ran"),
                  "start": c.get("start"), "end": c.get("end")},
    }


def _activity_days(today: str) -> set[str]:
    """本地库里有活动的日期（含高驰）。

    高驰记录的活动根本不会出现在佳明日历里，只看日历会把「已经练了」算成
    「没练」——所以完成度必须以本地库为准，日历只是补充。
    """
    try:
        store = Store(DB)
        try:
            lo = (dt.date.fromisoformat(today) - dt.timedelta(days=21)).isoformat()
            hi = (dt.date.fromisoformat(today) + dt.timedelta(days=1)).isoformat()
            rows = store.conn.execute(
                "SELECT DISTINCT date FROM activities WHERE date >= ? AND date <= ?",
                (lo, hi)).fetchall()
            return {r[0] for r in rows if r[0]}
        finally:
            store.conn.close()
    except Exception:
        return set()


def _plan_local(today: str, days: set[str], note: str = "") -> dict:
    """没有佳明会话时的本地课表兜底。

    纯高驰用户拿不到佳明日历（高驰没有对外计划接口），以前这里直接
    返回 ok:false，今日页整块课表就不渲染。现在退成「备赛计划 > 今日处方」，
    至少让人看到今天该练什么。
    """
    from .plan import from_coach

    try:
        res = derive(Store(DB), today)
        data = from_coach(res, today, days)
    except Exception as e:  # noqa: BLE001 - 兜底失败就老实说，别假装有计划
        from . import diag

        diag.log_exc("plan:local", e)
        return {"ok": False, "kind": "local",
                "error": f"本地课表算不出来：{type(e).__name__}: {e}"}
    if note:
        data["note"] = note
    return {"ok": True, **data}


def plan(refresh: bool = False, date: str | None = None,
         source: str = "auto") -> dict:
    """训练安排（佳明日历优先，Garmin Coach 兜底）。

    默认读同步时落下的 `data/plan.json`——零请求，断网也能看。
    `refresh=True` 才联网拉一次，失败不影响已有快照。
    `source`：auto / calendar / coach，见 `plan.fetch()`。
    高驰没有对外计划接口，这里只处理佳明。
    """
    from .plan import annotate, fetch, load

    today = date or dt.date.today().isoformat()
    path = HOME / "data" / "plan.json"
    days = _activity_days(today)
    data = None if refresh else load(path)
    if not data:
        try:
            client = _client()
        except AuthError as e:
            # 没登录不是错误，是「换个数据源」：本地兜底比空白页有用
            return _plan_local(today, days, note=f"佳明未登录：{e}")
        except Exception as e:
            return _plan_local(today, days,
                               note=f"佳明连接失败：{type(e).__name__}")
        try:
            data = fetch(client, today, out_path=path, source=source,
                         activity_days=days)
        except SessionExpired:
            return _plan_local(today, days, note="佳明会话过期，已改用本地课表")
        except Exception as e:
            data = {"available": False, "reason": f"{type(e).__name__}: {e}"}
    if not data or not data.get("available"):
        reason = (data or {}).get("reason") or ""
        local = _plan_local(today, days, note=f"佳明无课表：{reason}" if reason else "")
        if local.get("ok"):
            return local
        return {"ok": False, "error": reason or "当前没有可用的训练安排",
                "empty": True}
    # 新拉的与缓存的都重新标注一次：同步之后「今天练没练」会变，
    # 缓存里的 done 不能一直用旧的。
    return {"ok": True, **annotate(data, days, today)}


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


def history_stats(start: str | None = None, end: str | None = None,
                  types: str = "run") -> dict:
    """历史页首的统计：本月 / 本年 / 累计 + 近 12 个月柱状图 + 周/月/年分组。

    一次把活动表全读出来在 Python 里聚合（几千行也就几十毫秒），比在 SQL 里
    拼 ISO 周函数干净——SQLite 没有 `isocalendar()`，而周分组是用户明确要的。
    `types="run"` 只看跑步（和列表的「只看跑步」同一套 `_RUN_TYPES`）；
    传空串则统计全部运动。
    """
    try:
        store = Store(DB)
        try:
            e = end or dt.date.today().isoformat()
            try:
                dt.date.fromisoformat(e)
            except (TypeError, ValueError):
                return {"ok": False, "error": "日期格式应为 YYYY-MM-DD"}
            # 起点不设上限：活动表里最早那条就是全部家当
            rows = store.activities(start or "2000-01-01", e)
        finally:
            store.conn.close()
    except Exception as ex:
        return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    kind = (types or "").strip().lower()
    allow = set(_RUN_TYPES) if kind == "run" else None
    picked = [r for r in rows if allow is None or (r.get("type") or "") in allow]

    months: dict[str, dict] = {}
    years: dict[str, dict] = {}
    weeks: dict[str, dict] = {}
    days: dict[str, dict] = {}

    def bump(bucket: dict, key: str, km: float, dur: float) -> dict:
        b = bucket.get(key)
        if b is None:
            b = bucket[key] = {"km": 0.0, "count": 0, "dur_h": 0.0}
        b["km"] += km
        b["count"] += 1
        b["dur_h"] += dur
        return b

    for r in picked:
        d = str(r.get("date") or "")
        if len(d) < 10:
            continue
        km = float(r.get("distance_m") or 0) / 1000.0
        dur = float(r.get("duration_s") or 0) / 3600.0
        bump(months, d[:7], km, dur)
        bump(years, d[:4], km, dur)
        bump(days, d, km, dur)
        try:
            iso = dt.date.fromisoformat(d).isocalendar()
        except ValueError:
            continue
        wk = f"{iso[0]}-W{iso[1]:02d}"
        b = weeks.get(wk)
        if b is None:
            b = weeks[wk] = {"km": 0.0, "count": 0, "dur_h": 0.0,
                             "start": d, "end": d}
        b["km"] += km
        b["count"] += 1
        b["dur_h"] += dur
        if d < b["start"]:
            b["start"] = d
        if d > b["end"]:
            b["end"] = d

    today = dt.date.fromisoformat(e)
    ds = de = None
    if start and end:
        try:
            ds, de = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
        except ValueError:
            ds = de = None
    # 月份序列：给了范围就按范围铺（年视图要正好 12 根），
    # 没给就是「近 12 个月」——两种都连续，没跑的月份占一根空柱
    keys: list[str] = []
    if ds and de:
        yy, mm = ds.year, ds.month
        while (yy, mm) <= (de.year, de.month):
            keys.append(f"{yy:04d}-{mm:02d}")
            mm += 1
            if mm > 12:
                mm = 1
                yy += 1
    else:
        for i in range(11, -1, -1):
            mm, yy = today.month - i, today.year
            while mm <= 0:
                mm += 12
                yy -= 1
            keys.append(f"{yy:04d}-{mm:02d}")
    months_out = [{"key": k, "label": f"{int(k[5:7])}月",
                   "km": round(months.get(k, {}).get("km", 0), 1),
                   "count": months.get(k, {}).get("count", 0),
                   "dur_h": round(months.get(k, {}).get("dur_h", 0), 1)}
                  for k in keys]

    # 周用 start 排序（ISO 周号跨年会回绕，字符串排序不可靠）
    wk_sorted = sorted(weeks.items(), key=lambda kv: kv[1]["start"], reverse=True)[:12]
    wk_sorted.reverse()
    weeks_out = [{"key": k, "label": f"{v['start'][5:]}~{v['end'][5:]}",
                  "start": v["start"], "end": v["end"],
                  "km": round(v["km"], 1), "count": v["count"],
                  "dur_h": round(v["dur_h"], 1)} for k, v in wk_sorted]

    years_out = [{"key": k, "km": round(v["km"], 1), "count": v["count"],
                  "dur_h": round(v["dur_h"], 1)}
                 for k, v in sorted(years.items(), key=lambda kv: kv[0], reverse=True)]

    # 逐日序列只给周/月视图用：范围上限 400 天，误传 3 年也不会把页面撑爆
    days_out: list[dict] = []
    if ds and de and 0 < (de - ds).days + 1 <= 400:
        cur = ds
        while cur <= de:
            k = cur.isoformat()
            b = days.get(k, {})
            days_out.append({"key": k, "label": str(cur.day),
                             "km": round(b.get("km", 0), 1),
                             "count": b.get("count", 0)})
            cur += dt.timedelta(days=1)

    cur_m = months.get(today.strftime("%Y-%m"), {})
    cur_y = years.get(today.strftime("%Y"), {})
    total_km = sum(v["km"] for v in months.values())
    total_h = sum(v["dur_h"] for v in months.values())
    return {
        "ok": True,
        "total": {"km": round(total_km, 1), "count": len(picked),
                  "dur_h": round(total_h, 1)},
        "month": {"key": today.strftime("%Y-%m"),
                  "km": round(cur_m.get("km", 0), 1),
                  "count": cur_m.get("count", 0)},
        "year": {"key": today.strftime("%Y"),
                 "km": round(cur_y.get("km", 0), 1),
                 "count": cur_y.get("count", 0)},
        "months": months_out,
        "days": days_out,
        "weeks": weeks_out,
        "years": years_out,
        "range": {"first": min((r["date"] for r in picked), default=""),
                  "last": max((r["date"] for r in picked), default="")},
    }


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

        from . import coros_activity as CA
        from .coros import CorosAuthError
        provider = CA.source(row)

        detail = store.get_detail(aid)
        if provider == "coros":
            # 高驰明细走同一条链路：缓存版本不对（旧口径把 lap 距离当成米，
            # 1 km 圈会显示成 100.00 km）就重抓一次，别让错数据一直留在库里。
            try:
                detail = CA.ensure(store, HOME, row, coros_email or None,
                                   coros_password or None)
            except CorosAuthError as e:
                return {"ok": False, "need_login": True, "source": "coros",
                        "error": f"{e}（高驰明细需联网抓一次）"}
            except Exception as e:
                return {"ok": False, "source": "coros",
                        "error": f"抓高驰明细失败（需要联网）：{type(e).__name__}: {e}"}
        elif not detail:
            try:
                from . import activity as act

                detail = act.ensure(store, _client(email, password), aid)
            except (AuthError, SessionExpired):
                return {"ok": False, "need_login": True, "source": "garmin",
                        "error": "这条活动的明细还没缓存，需要联网登录后抓一次"}
            except Exception as e:
                return {"ok": False,
                        "source": provider,
                        "error": f"获取佳明活动明细失败：{e}"}
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
        # 超时/失败后用户会反复重试，每次都是新 run，旧帧目录（一版 720 帧
        # 几百 MB）却一直留在磁盘上。开新一轮时顺手清掉过期的，正在导出的
        # run 远没到这个年龄，不会被误删。
        _sweep_motion_frames()
        out = OUT / f"motion-{aid}.html"
        # 传 rt 进去：素材按这次活动的时间窗过滤，库里残留的旧素材
        # （上一次跑步加的）不会被贴到活动两端混进成片
        clips = _clips_for_motion(rt)
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
        # 视频素材清单交给原生抽帧：WebView 里 `drawImage(<video>)` 会同步等
        # 解码，真机上一段 6 秒素材要 5 分钟，直接把导出拖到「5 分钟没有新帧」
        # 判死（提示「第 N 帧没送到」）。原生 MediaMetadataRetriever 快一两个
        # 数量级，页面只读抽好的 <img>。`ci` 与页面 payload 里的 clip.ci 一致。
        clip_list = [{"ci": (p["clip"].get("ci") if p["clip"].get("ci") is not None else -1),
                      "name": p["clip"].get("name") or "",
                      "file": p["clip"].get("local") or "",
                      "dur": float(p["clip"].get("dur") or 0),
                      "w": int(p["clip"].get("w") or 0),
                      "h": int(p["clip"].get("h") or 0),
                      "rotate": int(p["clip"].get("rotate") or 0) % 360}
                     for p in (pl.get("photos") or [])
                     if p.get("clip") and p["clip"].get("kind") != "image"]
        return {"ok": True, "html": str(out), "run": run,
                "frames": max(1, int(round(total * fps))), "fps": fps,
                "duration": round(total, 2), "play": dur,
                "width": 1080, "height": 1920, "clips": clip_list,
                "dir": str(OUT / "_frames" / run),
                "privacy": mode, "name": row.get("name") or "跑步回放",
                # 真机排查「底图缩放」用：App 会写进 Logcat
                "view": pl.get("view") or {}}
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


def _clips_for_motion(rt: dict | None = None,
                      pre_s: float = 24 * 3600, post_s: float = 24 * 3600) -> list[dict]:
    """素材（视频 / 图片）→ motion.build_payload 要的 shot 结构。

    必须带上 `t_utc`：`photo.match` 靠拍摄时间把素材挂到轨迹上。
    之前这里只给路径和时长，结果全部以「没有拍摄时间」被丢掉 ——
    表现就是「加了素材，回放里却什么都没有」。
    视频读 mvhd 的 creation_time，图片读 EXIF；都没有的留空，
    由 `motion` 那边均匀铺开，至少能出现在画面上。

    时长以**文件真实时长**为准，不信登记时记的那个值：
    登记时传的 dur 可能为 0（原生侧还没量出来），这里如果照用就退化成
    3 秒——用户加了一段 51 秒的视频，回放里只播 3 秒，等于被裁掉了。
    所以视频一律重新探文件，读不到才退回登记值。

    `rt` 是这次活动的路线（带 t0 与时序）。给了它就按**活动时间附近**过滤：
    素材登记在手机上是全局的，上一次跑步/上一次比赛加的素材还留在库里，
    不筛的话 `photo.match(clamp_ends=True)` 会把那些「时间离这次活动很远的」
    素材强行贴到活动两端塞进成片——上次比赛（11-03）的视频就混进了这次
    （11-08）的回放，看着像放错了片子。
    容差取 24 小时：差几小时的素材照旧保留（用户加的不许凭空消失），
    差好几天的才剔除。读不到拍摄时间的素材一律保留（无法判定归属）。
    """
    t0 = (rt or {}).get("t0")
    dur = 0.0
    ts = (rt or {}).get("t") or []
    if ts:
        dur = float(ts[-1])
    out = []
    for it in _clips_read():
        p = Path(str(it.get("path") or ""))
        if not p.is_file():
            continue
        info = _probe_material(p, name_hint=str(it.get("name") or ""))
        kind = it.get("kind") or info.get("kind") or "video"
        # 视频：文件真实时长优先；探不到才用登记值
        dur = float(info.get("dur") or 0) or float(it.get("dur") or 0)
        if dur <= 0:
            dur = STILL_SEC if kind == "image" else 3.0
        t_utc = _meta_val(it, info, "t_utc")
        if t0 and t_utc:
            rel = (float(t_utc) - float(t0)) / 1000.0
            if rel < -pre_s or rel > dur + post_s:
                # 时间明确不在这次活动里 → 跳过，别让它贴到活动两端
                continue
        out.append({
            "path": str(p),
            "name": it.get("name") or p.stem,
            "kind": kind,
            "dur": round(dur, 2),
            "w": int(it.get("w") or info.get("w") or 1080),
            "h": int(it.get("h") or info.get("h") or 1920),
            "orientation": int(info.get("orientation") or 1),
            "t_utc": t_utc,
            "lat": _meta_val(it, info, "lat"),
            "lon": _meta_val(it, info, "lon"),
            "codec": info.get("codec") or "",
            "bytes": info.get("bytes") or 0,
            # 显示旋转：无损剪辑不转码，方向只能在绘制/抽帧时兑现。
            # 0/90/180/270，只影响呈现，不影响锚定。
            "rotate": int(it.get("rotate") or 0) % 360,
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


def _sweep_motion_frames(max_age_h: float = 24) -> int:
    """清掉上次导出（尤其是超时重试）留下的过期帧目录，返回删掉的个数。

    只认 `output/_frames/<合法 run>` 且最后写入超过 `max_age_h` 小时的目录；
    别的目录、别的命名一律不碰。24 小时的门槛远长于一次正常导出，就算
    慢机器跑满一两个小时，重试也不会把正在收帧的目录删掉。
    """
    root = OUT / "_frames"
    if not root.is_dir():
        return 0
    cutoff = time.time() - max_age_h * 3600
    n = 0
    for d in root.iterdir():
        try:
            if not d.is_dir() or not _RUN_RE.fullmatch(d.name):
                continue
            if d.stat().st_mtime > cutoff:
                continue
            shutil.rmtree(d, ignore_errors=True)
            if not d.exists():
                n += 1
        except OSError:
            continue
    return n


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
        native = _norm_taken_ms(it.get("t_native"))
        had = bool(it.get("t_utc"))
        # 老版本登记时探不到时间。现在能从 udta / 原始文件名 / 原生兜底
        # 补回来的直接落盘，免得界面一直显示「加入」、回放一直按数量均分。
        found = info.get("t_utc") or native
        if not had and found:
            src = (info.get("t_source") or "") if info.get("t_utc") else "相册/视频日期标签"
            it["t_utc"], it["t_source"] = found, src
            repairs[str(it.get("id") or it.get("path") or "")] = (found, src)
        it["t_utc"] = _meta_val(it, info, "t_utc") or native or None
        it["t_source"] = it.get("t_source") or info.get("t_source") \
            or ("相册/视频日期标签" if native else "")
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
            if got and not x.get("t_utc"):
                x["t_utc"], x["t_source"] = got
        _clips_write(raw)
    return {"ok": True, "items": items, "dir": str(MEDIA)}


def _norm_taken_ms(v) -> int:
    """原生侧传来的拍摄时间统一成 UTC 毫秒。

    各 ROM 的 `DATE_TAKEN` 有给毫秒的、也有给秒的；`MediaMetadataRetriever`
    解析出来也是毫秒。范围外的（0、1970、加进来时的系统时间被误传）一律
    丢掉——把「加入时间」当拍摄时间会直接把素材锚到错误的轨迹位置。
    """
    try:
        n = int(float(v or 0))
    except (TypeError, ValueError):
        return 0
    if 1_000_000_000 <= n < 100_000_000_000:
        n *= 1000
    if not (946_684_800_000 <= n < 4_102_444_800_000):   # 2000-01-01 ~ 2100-01-01
        return 0
    return n


def _kind_of(p: Path) -> str:
    return "image" if p.suffix.lower() in (
        ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif") else "video"


def clips_reprobe(clip_id: str) -> dict:
    """素材列表里的「重新读取时间」：成品、原始文件名、原生兜底时间再探一遍。

    以前这个按钮只是把列表重刷一次，成品被改名成 `clip-…`、又没写回时间时，
    点多少次都一样。现在会重新探测、把补到的时间写回成品的 mvhd 并落盘。
    """
    items = _clips_read()
    for it in items:
        if str(it.get("id") or "") != str(clip_id):
            continue
        p = Path(str(it.get("path") or ""))
        if not p.is_file():
            return {"ok": False, "error": "文件已丢失，删掉重新添加一次"}
        info = _probe_material(p, name_hint=str(it.get("name") or ""))
        native = _norm_taken_ms(it.get("t_native"))
        t = info.get("t_utc") or it.get("t_utc") or native
        if not t:
            why = info.get("t_why") or "文件里没有拍摄时间，剪辑前的源文件也没留下"
            it["t_why"] = why
            _clips_write(items)
            return {"ok": True, "t_utc": 0,
                    "error": "还是读不到：" + why + "。请删掉这条，重新从相册添加一次"}
        it["t_utc"] = int(t)
        it["t_source"] = (info.get("t_source") or it.get("t_source")
                          or "相册/剪辑前")
        it["t_why"] = ""
        if (it.get("kind") or _kind_of(p)) == "video":
            from . import clip as clip_mod
            clip_mod.stamp_creation(p, int(t))
        _clips_write(items)
        return {"ok": True, "t_utc": int(t), "t_source": it["t_source"]}
    return {"ok": False, "error": "没有这段素材"}


def clips_add(path: str, name: str = "", dur: float = 0,
              w: int = 0, h: int = 0, kind: str = "",
              probe_from: str = "", taken_ms: float = 0,
              rotate: int = 0, keep_src: str = "") -> dict:
    """登记一段素材。视频的剪辑由原生侧用 MediaExtractor/Muxer 无损完成，
    图片直接拷进来即可；这里只记录结果（文件应落在 `data/media/` 下）。

    `probe_from` 是**剪辑前的源文件**：视频一旦被 MediaMuxer 重封装，
    `mvhd.creation_time` 就没了（实测转码/重封装后的 mp4 一律读不到），
    只探成品会永远拿不到拍摄时间，回放里就只能按数量均匀铺开、位置不准。

    `taken_ms` 是原生侧从**相册/视频解码器**读到的拍摄时间。真机上「相册里
    明明显示拍摄时间、App 里却是无拍摄时间」多半就是这个：时间只存在于
    MediaStore 的 DATE_TAKEN 或 MediaMetadataRetriever 的日期标签里，
    拷进 cache 的 mp4、以及裁出来的成品都不带它。所以必须在一开始就取出来。

    `rotate` 是显示旋转（0/90/180/270）。无损剪辑不能真的转码旋转，所以
    记在素材上，回放绘制和原生抽帧时再转 —— 竖拍视频才不会在卡片里被压成横屏。

    `keep_src` 是**保留下来**的原始文件（不是临时 cache）：再次剪辑要回到
    未裁的原片，否则只能在已经裁过的成品上二次裁，首段永远找不回来。
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
    # 时长也以文件为准：原生侧传的 dur 可能是剪辑结果的估算值，
    # 有真实时长就别用它——登记 0 更糟（后面会退化成 3 秒）。
    if k == "video":
        dur = float(fallback.get("dur") or meta.get("dur") or 0) or float(dur or 0)
    native = _norm_taken_ms(taken_ms)
    t_utc = meta.get("t_utc") or fallback.get("t_utc") or native or None
    if meta.get("t_utc"):
        t_src = meta.get("t_source") or "剪辑前源文件"
    elif fallback.get("t_utc"):
        t_src = fallback.get("t_source") or "成品文件"
    elif native:
        t_src = "相册/视频日期标签"
    else:
        t_src = ""
    t_why = "" if t_utc else (meta.get("t_why") or fallback.get("t_why") or "读不到拍摄时间")
    # 把时间写回成品的 mvhd：以后就算 clips.json 丢了，也还能从文件本身读回来。
    if t_utc and k == "video":
        from . import clip as clip_mod
        clip_mod.stamp_creation(p, int(t_utc))
    try:
        rot = int(rotate or 0) % 360
    except (TypeError, ValueError):
        rot = 0
    if rot not in (0, 90, 180, 270):
        rot = 0
    items.append({
        "id": cid, "path": str(p), "name": (name or p.stem)[:60],
        "kind": k,
        "dur": round(float(dur or 0), 2), "w": int(w or 0), "h": int(h or 0),
        "t_utc": t_utc, "t_source": t_src, "t_why": t_why,
        # 原生兜底时间存下来：以后「重新读取时间」、clips.json 被重写都还在
        "t_native": native or None,
        "lat": meta.get("lat") if meta.get("lat") is not None else fallback.get("lat"),
        "lon": meta.get("lon") if meta.get("lon") is not None else fallback.get("lon"),
        "added_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        # 显示旋转：无损剪辑不转码，方向只能在绘制端兑现
        "rotate": rot,
        # 原始文件（再次剪辑用）。为空表示这条是旧记录或图片，不能再改起点
        "src": str(Path(keep_src)) if keep_src else "",
    })
    _clips_write(items)
    return {"ok": True, "id": cid, "t_utc": t_utc, "t_source": t_src,
            "items": items}


def clips_remove(clip_id: str) -> dict:
    r = clips_remove_many([clip_id])
    if not r.get("ok"):
        return {"ok": False, "error": "没有这段素材"}
    return {"ok": True, "items": r.get("items") or []}


def clips_remove_many(ids: list) -> dict:
    """一次删多条：写一次盘、删一批文件。

    批量删除时逐条调 `clips_remove` 会写 N 次 clips.json，几十条就明显卡；
    而且要删的文件里还有「保留的原始片」，不一起删会一直占着几百 MB。
    """
    want = {str(x) for x in (ids or []) if x}
    if not want:
        return {"ok": False, "error": "没有选中素材"}
    items = _clips_read()
    keep, gone = [], []
    for it in items:
        if str(it.get("id") or "") in want:
            gone.append(it)
        else:
            keep.append(it)
    if not gone:
        return {"ok": False, "error": "选中的素材已经不在列表里"}
    _clips_write(keep)
    for it in gone:
        for key in ("path", "src"):
            try:
                fp = Path(str(it.get(key) or ""))
                if fp.is_file():
                    fp.unlink()
            except OSError:
                pass
    return {"ok": True, "removed": len(gone), "items": keep}


def clips_update(clip_id: str, dur: float = 0, w: int = 0, h: int = 0,
                 rotate: int = -1, name: str = "") -> dict:
    """再次剪辑 / 手动旋转后回写记录（成品文件已由原生侧替换好）。

    `rotate=-1` 表示这次不动旋转，避免只想改时长时把方向写回 0。
    """
    items = _clips_read()
    hit = None
    for it in items:
        if str(it.get("id") or "") == str(clip_id):
            hit = it
            break
    if hit is None:
        return {"ok": False, "error": "没有这段素材"}
    if dur > 0:
        hit["dur"] = round(float(dur), 2)
    if w > 0:
        hit["w"] = int(w)
    if h > 0:
        hit["h"] = int(h)
    if name:
        hit["name"] = str(name)[:60]
    if rotate is not None and int(rotate) >= 0:
        try:
            rot = int(rotate) % 360
        except (TypeError, ValueError):
            rot = 0
        hit["rotate"] = rot if rot in (0, 90, 180, 270) else 0
    _clips_write(items)
    return {"ok": True, "item": hit, "items": items}


# ---------------------------------------------------------------- 数据搬迁
# 换手机 / 重装 App 时把历史带走。App 是 allowBackup=false，不导出就等于清零。
#
# 只搬「自己练出来的东西」：日汇总、活动、明细、体成分、成绩、教练点评，
# 再带上计划快照、手工补录、饮食与素材清单。
# **账号凭据一律不进包**（session.json / coros_auth.json / mi_auth.json / amap.json）：
# 那些是登录态，换机应该重新登录，复制过去只会让两台设备抢同一个会话。

BUNDLE_KIND = "runnercoach-data"
BUNDLE_VER = 1
BUNDLE_EXTRAS = ("plan.json", "manual.json", "meals.json", "clips.json")
BUNDLE_TABLES = ("daily", "activities", "activity_detail", "body",
                 "race_results", "coach_notes")

_BUNDLE_README = """RunnerCoach 数据包
==================

这个包里是你的训练与健康数据，用来换手机或重装后恢复。

包含：
  garmin.db      日汇总、活动、活动明细、体成分、赛事成绩、教练点评
  extras/        计划快照、手工补录、饮食记录、素材清单（有就带）
  manifest.json  导出时间、版本与各表条数

不包含（故意的）：
  * 任何账号凭据与会话（换机后在新设备上重新登录即可）
  * 照片与视频原文件（相册里还有原件，导入后重新添加素材即可）
  * 高德底图 Key

怎么导入：
  App 里：我的 → 本地数据 → 导入数据，选这个 zip。
  命令行：run.bat import <这个文件.zip>

导入会用包里的库**覆盖**当前数据，覆盖前自动备份一份 garmin.db.bak-时间戳。
"""


def _db_stats(path: Path) -> dict:
    """各表条数。导出时给人看，导入后用来核对是否一致。"""
    out: dict = {}
    try:
        conn = sqlite3.connect(str(path))
    except sqlite3.Error as e:
        return {"error": str(e)}
    try:
        for t in BUNDLE_TABLES:
            try:
                out[t] = conn.execute(f"select count(*) from {t}").fetchone()[0]
            except sqlite3.Error:
                out[t] = -1
    finally:
        conn.close()
    return out


def db_export() -> dict:
    """把本地库打成一个 zip，返回路径给系统分享。"""
    if not DB.is_file():
        return {"ok": False, "error": "还没有本地数据，先同步一次"}
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    # 每次现算：OUT 会被测试/调用方替换，模块级常量会指向老路径
    out = OUT / "exports" / f"RunnerCoach-data-{stamp}.zip"
    tmp = out.with_suffix(".db.part")
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        # 用 sqlite 的 backup API 复制，避免直接抄一个正在写的库
        src = sqlite3.connect(str(DB))
        try:
            dst = sqlite3.connect(str(tmp))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        counts = _db_stats(tmp)
        extras = [n for n in BUNDLE_EXTRAS if (HOME / "data" / n).is_file()]
        manifest = {
            "kind": BUNDLE_KIND,
            "bundle": BUNDLE_VER,
            "app": __version__,
            "exported_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "counts": counts,
            "extras": extras,
        }
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.writestr("manifest.json",
                       json.dumps(manifest, ensure_ascii=False, indent=1) + "\n")
            z.write(tmp, "garmin.db")
            for n in extras:
                z.write(HOME / "data" / n, f"extras/{n}")
            z.writestr("README.txt", _BUNDLE_README)
    except (OSError, sqlite3.Error, zipfile.BadZipFile) as e:
        return {"ok": False, "error": f"导出失败：{type(e).__name__}: {e}"}
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return {"ok": True, "path": str(out), "name": out.name,
            "size": out.stat().st_size, "counts": counts, "extras": extras,
            # 明确列出「没带什么」：换机后这些要重做，界面直接照这个渲染，
            # 别让用户以为导入完就万事大吉。
            "excluded": ["佳明 / 高驰 / 小米的账号与会话", "高德底图 Key",
                         "照片与视频原文件"]}


def db_import(zip_path: str = "") -> dict:
    """用数据包覆盖本地库。覆盖前一定先备份，失败也能退回去。"""
    p = Path(str(zip_path or "")).expanduser()
    if not p.is_file():
        return {"ok": False, "error": "找不到这个文件"}
    try:
        z = zipfile.ZipFile(p)
    except (zipfile.BadZipFile, OSError):
        return {"ok": False, "error": "不是有效的 zip 文件"}
    with z:
        names = set(z.namelist())
        if "garmin.db" not in names or "manifest.json" not in names:
            return {"ok": False, "error": "包不完整：缺 garmin.db 或 manifest.json"}
        try:
            man = json.loads(z.read("manifest.json").decode("utf-8"))
        except (KeyError, ValueError):
            return {"ok": False, "error": "manifest.json 读不出来"}
        if man.get("kind") != BUNDLE_KIND:
            return {"ok": False, "error": "这不是 RunnerCoach 的数据包"}
        try:
            newer = int(man.get("bundle") or 0) > BUNDLE_VER
        except (TypeError, ValueError):
            newer = False
        if newer:
            return {"ok": False,
                    "error": "这个包来自更新的版本，请先把 App 升级到最新再导入"}

        tmp = HOME / "data" / ".import.part"
        try:
            tmp.write_bytes(z.read("garmin.db"))
        except (KeyError, OSError) as e:
            return {"ok": False, "error": f"解包失败：{type(e).__name__}"}

        backup = ""
        try:
            if DB.is_file():
                bp = DB.with_name(f"garmin.db.bak-{dt.datetime.now():%Y%m%d-%H%M%S}")
                shutil.copy2(DB, bp)
                backup = str(bp)
            src = sqlite3.connect(str(tmp))
            try:
                dst = sqlite3.connect(str(DB))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
        except (OSError, sqlite3.Error) as e:
            return {"ok": False, "error": f"写库失败：{type(e).__name__}: {e}"}
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

        restored = []
        for n in BUNDLE_EXTRAS:
            key = f"extras/{n}"
            if key in names:
                try:
                    (HOME / "data" / n).write_bytes(z.read(key))
                    restored.append(n)
                except (KeyError, OSError):
                    pass

    return {"ok": True, "counts": _db_stats(DB), "restored": restored,
            "backup": backup, "exported_at": str(man.get("exported_at") or "")}


def check_update() -> dict:
    """查 Gitee 上是否有新版本（App 侧 24 小时节流，手动检查不节流）。

    只读公开 Release 接口，不上报任何用户数据；失败按「没有新版本」处理，
    不打断任何流程。
    """
    try:
        from . import update as UP

        return UP.check()
    except Exception as e:
        return {"ok": False, "newer": False, "error": f"{type(e).__name__}: {e}"[:160]}


def diag_text(lines: int = 200) -> dict:
    """最近若干条异常，给「我的 → 诊断日志」弹窗用。"""
    from . import diag

    txt = diag.read_text(lines)
    return {"ok": True,
            "text": txt or "（还没有异常记录）",
            "path": str(diag.log_path())}


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
