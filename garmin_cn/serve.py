# -*- coding: utf-8 -*-
"""手机看板服务：把 output/ 只读地挂到局域网上，手机浏览器直接看。

为什么需要这一层
----------------
看板本身就是自包含 HTML，问题只在于"怎么递到手机上"：微信传文件要手动找路径、
数据线要驱动、放到网盘又等于把健康数据交给第三方。同一 WiFi 下起一个只读服务，
手机打开一串地址就能看——iPhone 也不用装任何东西。

三条硬规则
----------
1. **局域网只读**：只读页面与产物；设置及高驰训练写入仅供本机、需页面令牌。
2. **默认只听 127.0.0.1**：本机看不需要暴露端口；`--lan` 才绑 0.0.0.0。
3. **绑 0.0.0.0 必须有访问码**：output/ 里是健康数据、体重、轨迹。
   同一 WiFi 不等于可信网络（宿舍、共享办公、咖啡馆都算）。

访问码用 cookie 记住，地址栏里的 `?k=` 会立刻重定向掉，
避免手机截图或投屏时把令牌一起带出去。
"""
from __future__ import annotations

import datetime as dt
import hmac
import json
import os
import re
import secrets
import socket
import sys
import time
from functools import partial
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

from . import history, ui

SERVE_EXT = {".html", ".htm", ".png", ".jpg", ".jpeg", ".mp4", ".json", ".csv"}
LIST_EXT = {".html", ".htm", ".png", ".mp4"}   # 手机上值得点开的；json/csv 只服务不列

# 首页自动同步的节流（秒）。一次增量同步约 8 个请求、两秒，
# 但每进一次首页都跑会白耗电，佳明那边也会觉得异常。
# 首页自动同步的节流（秒）。原来 10 分钟太长：用户 10 分钟前开过一次页
# 但还没跑步，跑完回来正好落在窗口里，那次同步被挡掉，于是又变成「手拉」。
# 3 分钟足够挡住「来回切页面」的重复请求，又不会挡掉真实的「跑完回来」。
AUTO_SYNC_GAP = 3 * 60


def _lan_ip() -> str:
    """取本机在局域网里的地址；UDP connect 不会真的发包。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{max(1, n // 1024)} KB"


def _title(name: str) -> tuple[str, str, str]:
    """文件名 → (标题, 组, 类型)。同一天既有 PNG 又有 HTML 时靠标题区分用途。"""
    stem, _, ext = name.rpartition(".")
    ext = "." + ext.lower()
    kind = "网页" if ext in (".html", ".htm") else ("视频" if ext == ".mp4" else "图片")

    if name == "latest.html":
        return "今天的看板", "今日", kind
    if stem == "demo":
        return "演示看板（仿真数据）", "今日", kind
    if stem == "card":
        return "最新每日看板", "今日", kind
    if stem == "act":
        return "最新活动图", "今日", kind
    if stem.startswith("dashboard-"):
        return f"看板 {stem[10:]}", "看板", kind
    if stem.startswith("card-"):
        tail = "" if ext == ".png" else " · 网页"
        return f"每日看板 {stem[5:]}{tail}", "图", kind
    if stem.startswith("act-"):
        day = stem[4:].rsplit("-", 1)[0]
        label = "活动图" if ext == ".png" else "活动详情"
        return f"{label} {day}", "活动", kind
    if stem.startswith("motion-"):
        label = "回放视频" if ext == ".mp4" else "轨迹回放"
        return f"{label} {stem[7:]}", "活动", kind
    if stem.startswith("plan-"):
        return f"105K 计划 {stem[5:]}", "计划", kind
    return name, "其它", kind


def _with_fix(doc: str, css: str) -> str:
    """给已有的自包含 HTML（AI 页 / 看板）补一段统一外壳样式，不动它的 DOM。"""
    return doc.replace("</head>", f"<style>{css}</style></head>", 1)


def _plan_float(v, cap: float = 600.0) -> float:
    """表单里的数字：空串、负数、超上限都当 0，不抛。"""
    try:
        f = float(str(v or "").strip() or 0)
    except ValueError:
        return 0.0
    return f if 0 < f <= cap else 0.0


def _gpx_xml(rt: dict, pts: list) -> str:
    """把一条组内共享路线拼成 GPX 1.1。

    只写轨迹点，**不写时间与心率**：`group_route.points` 里本来就只有坐标
    （上传时为隐私只传了坐标），硬塞一个 `T00:00:00Z` 会让导入方以为这是
    带时序的实录，进而算出错误的配速和分段。
    """
    from xml.sax.saxutils import escape as _x

    title = _x(str(rt.get("title") or "路线"))
    day = str(rt.get("date") or "")[:10]
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<gpx version="1.1" creator="RunnerCoach"'
           ' xmlns="http://www.topografix.com/GPX/1/1"'
           ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
           ' xsi:schemaLocation="http://www.topografix.com/GPX/1/1'
           ' http://www.topografix.com/GPX/1/1/gpx.xsd">',
           "<metadata><name>" + title + "</name>"
           + (f"<time>{day}T00:00:00Z</time>" if day else "") + "</metadata>",
           "<trk><name>" + title + "</name><type>running</type><trkseg>"]
    for p in pts:
        try:
            lat, lon = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue          # 越界点会让部分软件整条轨迹都打不开
        out.append(f'<trkpt lat="{lat:.6f}" lon="{lon:.6f}"/>')
    out.append("</trkseg></trk></gpx>")
    return "\n".join(out)


def _plan_days_from_form(values: dict) -> list[dict]:
    """把编辑页的扁平字段还原成 `[{date, items, rest}, ...]`。

    单独拿出来是为了能离线测：这一层最容易出现「存进去少了一天」「休息日变成
    一条 0 km 的课」这类静默错误，埋进 HTTP 处理里就没法验。

    跳过 `d<i>_skip=1` 的日子——那是「一天多条课次」，这一页不处理，宁可不改
    也不能悄悄把另一条删掉。
    """
    days = []
    for i in range(7):
        date = str(values.get(f"d{i}_date") or "").strip()[:10]
        if not date or str(values.get(f"d{i}_skip") or "") == "1":
            continue
        kind = str(values.get(f"d{i}_kind") or "rest").strip().lower()
        km = _plan_float(values.get(f"d{i}_km"), cap=60)
        minutes = _plan_float(values.get(f"d{i}_minutes"), cap=600)
        if kind == "rest" or (km <= 0 and minutes <= 0):
            days.append({"date": date, "items": [], "rest": True})
            continue
        item = {"km": km, "minutes": minutes, "kind": kind}
        if kind == "strength":
            item.update({"sport": "力量", "sport_key": "strength_training"})
        else:
            item.update({"sport": "跑步", "sport_key": "running"})
        days.append({"date": date, "items": [item], "rest": False})
    return days


class _Handler(SimpleHTTPRequestHandler):
    server_version = "garmin-coach/0.1"

    # AI 页面与接口：只有本机能调。局域网访问者即便拿到访问码，
    # 也进不了这条通道——它会把健康数据发给模型服务商，还会花钱。
    _AI_PAGES = ("/ai", "/ai.html")

    def _is_loopback(self) -> bool:
        ip = (self.client_address or ("",))[0]
        return ip in ("127.0.0.1", "::1", "localhost")

    def do_GET(self) -> None:  # noqa: N802
        """GET 的最外层兜底。

        原来任何一处渲染异常都会让连接**没有任何响应就断掉**，WebView 收到
        `net::ERR_EMPTY_RESPONSE`，用户看到的是一整片黑，连「哪一页错了」
        都没有。排查时只能靠猜（月榜黑屏那次就是这样，云端一条请求都没有，
        因为崩在发请求之前）。

        现在统一转成一个 500 页面，并把 traceback 打到 stderr（logcat 可见）。
        """
        try:
            self._do_GET()
        except (BrokenPipeError, ConnectionResetError):
            pass                      # 用户切走/刷新，不是错误
        except Exception as e:        # noqa: BLE001 - 兜底就是兜所有
            self._error_page(e)

    def _error_page(self, e: BaseException) -> None:
        import traceback
        from html import escape as _esc

        traceback.print_exc()
        try:
            body = ("<!doctype html><meta charset=utf-8>"
                    "<meta name=viewport content='width=device-width,initial-scale=1'>"
                    "<body style='font-family:system-ui;padding:30px 20px;"
                    "line-height:1.75;color:#dbe3ee;background:#0d1117'>"
                    "<h3 style='margin:0 0 10px'>这个页面没能打开</h3>"
                    "<p style='color:#8a93a3;margin:0 0 16px;"
                    "word-break:break-word;font-size:13px'>"
                    + _esc(f"{type(e).__name__}: {e}")[:400]
                    + "</p><p><a href='/' style='color:#e8b06a'>回首页</a></p></body>")
            b = body.encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)
        except Exception:             # noqa: BLE001 - 响应已经发出去了就没法补救
            pass

    def _auto_sync(self) -> None:
        """首页加载时自动拉一次佳明云（今天/昨天）。

        为什么需要：`onResume` 只调 `BgSync.nudge`（把**本地已有**数据补传到
        服务器），后台任务也只调 `bg_catch_up`，两者都**不拉佳明**。所以
        「跑完回来打开 App 看不到新活动」的原因不是「拿不到 Push」，而是
        连轮询都只在手动点「数据源 → 同步」时才发生。

        节流 10 分钟：一次增量约 8 个请求、两秒，但每进一次页面都跑会白耗电。
        失败（会话过期/离线）把时间戳清掉，下次打开还能再试。
        """
        from . import appapi as A
        from .store import Store

        if not self._is_loopback():
            return self._json({"ok": False, "error": "只允许本机"}, 403)
        try:
            st = Store(A.DB)
            try:
                now = time.time()
                try:
                    last = dt.datetime.strptime(
                        str(st.get_meta("auto_sync_at") or ""), "%Y-%m-%d %H:%M:%S")
                    last_ts = last.timestamp()
                except ValueError:
                    last_ts = 0.0
                if now - last_ts < AUTO_SYNC_GAP:
                    return self._json({"ok": True, "skip": "throttled"})
                # 判据是「配过源」而不是「同步成功过」：刚装完 App 或清过数据
                # 时 last_sync 还是空的，但会话票据在，这时候更该试一次。
                # Keep 只看本地登录态：last_sync_keep 可能是旧版本留下的残值，
                # 用户从没登录过时不该因为它触发一次同步（更不该提示失效）。
                if not (st.get_meta("last_sync") or st.get_meta("last_sync_coros")
                        or A.keep_status().get("logged_in")
                        or A._garmin_session_ok() or A._coros_session_ok()):
                    return self._json({"ok": True, "skip": "no-source"})
                st.set_meta("auto_sync_at", time.strftime("%Y-%m-%d %H:%M:%S"))
            finally:
                st.conn.close()
        except Exception as e:      # noqa: BLE001 - 库打不开就静默失败
            return self._json({"ok": False, "error": f"{type(e).__name__}"})

        try:
            # 佳明 / 高驰 / Keep 一起拉，按配过的源走
            r = A.sync_all(days=2)
        except Exception as e:      # noqa: BLE001 - 会话过期/离线都走这里
            try:
                st2 = Store(A.DB)
                try:
                    st2.set_meta("auto_sync_at", "")
                finally:
                    st2.conn.close()
            except Exception:
                pass
            return self._json({"ok": False, "error": f"{type(e).__name__}"})

        # 一个源都没成功（会话过期是常态）→ 把戳清掉，下次打开还能再试。
        # 不清的话这次失败会被当成「刚同步过」，白等 10 分钟才重试。
        if not r.get("ok"):
            try:
                st2 = Store(A.DB)
                try:
                    st2.set_meta("auto_sync_at", "")
                finally:
                    st2.conn.close()
            except Exception:
                pass
        out = {"ok": True, "new": int(r.get("new") or 0),
               "sources": r.get("sources") or [], "failed": r.get("failed") or []}
        _nl = [str(x) for x in (r.get("need_login") or []) if x]
        if _nl:
            # 静默过太久是这次报障的根源：会话过期 → 什么都没发生 → 用户以为
            # 「自动同步失效」。现在把这件事交给界面说清楚，让人知道该去重连。
            #
            # 但**同一条提示 6 小时内只给一次**：佳明 JWT 两小时就过期，
            # 每次打开都弹会把首页变成告警栏。戳存 meta（不是 localStorage）——
            # 本地服务每次启动换端口，localStorage 的 origin 跟着变，存不住。
            # 手动下拉走原生 doSync，不经过这里，所以手动同步永远即时反馈。
            _who = ",".join(sorted(_nl))
            _now = time.time()
            try:
                st3 = Store(A.DB)
                try:
                    _raw = str(st3.get_meta("login_nag") or "")
                    _ts, _, _prev = _raw.partition("|")
                    try:
                        _recent = (_prev == _who
                                   and _now - float(_ts or 0) < 6 * 3600)
                    except ValueError:
                        _recent = False
                    if _recent:
                        _nl = []
                    else:
                        st3.set_meta("login_nag", f"{int(_now)}|{_who}")
                finally:
                    st3.conn.close()
            except Exception:      # noqa: BLE001 - 写不进就照常提示
                pass
            if _nl:
                out["need_login"] = _nl
        else:
            # 这次没有要重连的源：清戳，下次真过期时立刻提示。
            try:
                st4 = Store(A.DB)
                try:
                    st4.set_meta("login_nag", "")
                finally:
                    st4.conn.close()
            except Exception:
                pass
        return self._json(out)

    def _do_GET(self) -> None:
        u = urlparse(self.path)
        if u.path == "/__sync/today":
            return self._auto_sync()
        if u.path in self._AI_PAGES:
            if not self._is_loopback():
                return self._json({"ok": False, "error": "AI 页面只允许本机打开"}, 403)
            from . import aiui

            # AI 页有自己的 CSS，只补统一外框与标题节奏，不动它的交互。
            return self._html(_with_fix(aiui.render(), ui.AI_FIX))
        if u.path.startswith("/fonts/"):
            return self._font(u.path.rsplit("/", 1)[-1])
        if u.path.startswith("/content/"):
            return self._content(u.path.rsplit("/", 1)[-1])
        token = getattr(self.server, "token", "")
        if token and not self._gate(u, token):
            return
        # 五个 tab 的页面：同源、只读本地库，App 的 JS 桥照常挂在上面。
        # 放在 gate 之后，局域网带访问码的访问也受同一套保护。
        root = Path(self.directory)
        if u.path == "/__progress":
            # 产物页轮询它画「正在生成」进度条：原生侧把进度写在
            # output/_progress/<run>.json，这里只负责读和清理残留。
            return self._json(self._progress(root))
        if u.path == "/view":
            return self._html(history.view_page(parse_qs(u.query)))
        if u.path in ("/history", "/history.html"):
            return self._html(history.history_page(parse_qs(u.query)))
        if u.path in ("/me", "/me.html"):
            return self._html(ui.me_page())
        if u.path in ("/run", "/run.html"):
            return self._html(self._run_page(parse_qs(u.query)))
        # 训练小组：三个页面共用同一个页面令牌，写操作全部 POST + 303
        if u.path in ("/account", "/account.html", "/groups", "/groups.html") \
                or u.path.startswith("/groups/"):
            if not self._is_loopback():
                return self._json({"ok": False, "error": "只允许本机操作"}, 403)
            if not getattr(self.server, "group_token", ""):
                self.server.group_token = secrets.token_urlsafe(32)
            tok = self.server.group_token
            q = parse_qs(u.query)
            msg = (q.get("msg") or [""])[0]
            from . import groupui
            if u.path.startswith("/account"):
                return self._html(groupui.account_page(
                    tok, msg, (q.get("account") or [""])[0],
                    (q.get("code") or [""])[0], (q.get("from") or [""])[0]))
            if u.path.rstrip("/") in ("/groups", "/groups.html"):
                # ?list=1 强制显示列表：只有一个组时默认会直达组详情，
                # 那样「再加入一个 / 建组」的入口就永远看不到。
                _force = (q.get("list") or [""])[0] == "1"
                return self._html(groupui.groups_page(tok, msg, _force))
            seg = u.path.rstrip("/").split("/")[2:]
            if len(seg) == 2 and seg[0].isdigit() and seg[1] == "bingo":
                return self._html(groupui.bingo_page(tok, int(seg[0]), msg))
            if len(seg) == 2 and seg[0].isdigit() and seg[1] == "map":
                return self._html(groupui.map_page(tok, int(seg[0]), msg))
            if len(seg) == 2 and seg[0].isdigit() and seg[1] == "shirts":
                return self._html(groupui.shirts_page(tok, int(seg[0]), msg))
            if len(seg) == 2 and seg[0].isdigit() and seg[1] == "settings":
                return self._html(groupui.settings_page(tok, int(seg[0]), msg))
            if len(seg) == 2 and seg[0].isdigit() and seg[1] == "feed":
                # 动态页已经并进组页（用户要求：底部接着数据流，滑回顶部就是小组）。
                # 旧链接（收藏、以前发的）落到对应的组页并锚到动态区。
                return self._see_other(f"/groups/{int(seg[0])}#feed")
            if len(seg) == 2 and seg[0].isdigit() and seg[1] == "audit":
                return self._html(groupui.audit_page(tok, int(seg[0]), msg))
            if len(seg) == 2 and seg[0].isdigit() and seg[1] == "export.csv":
                return self._group_csv(int(seg[0]))
            if len(seg) == 2 and seg[0].isdigit() and seg[1] == "board":
                # 完整榜单：周/月口径在这一页里切；月榜还有里程/综合两套排序
                _r = "month" if (q.get("range") or [""])[0] == "month" else "week"
                _by = "comp" if (q.get("by") or [""])[0] == "comp" else "dist"
                return self._html(groupui.board_page(tok, int(seg[0]), msg, _r, _by))
            if len(seg) == 6 and seg[0].isdigit() and seg[1] == "route" \
                    and seg[3] == "note" and seg[5] == "img":
                # 批注照片：本地代理到云端。不让浏览器直连云端地址：
                # WebView 面对自签证书会拦掉 <img>，等于每条图都要过一次错误。
                return self._note_img(int(seg[0]), int(seg[2]), int(seg[4]))
            if len(seg) == 3 and seg[0].isdigit() and seg[1] == "route" \
                    and seg[2].endswith(".gpx"):
                # 导出：同一条路线存成 GPX。放在 .isdigit() 判断之前，
                # 否则 `12.gpx` 进不了数字分支也进不了这里。
                return self._route_gpx(int(seg[0]), seg[2][:-4])
            if len(seg) == 3 and seg[0].isdigit() and seg[1] == "route" \
                    and seg[2].isdigit():
                # 单条路线：点开才取坐标，清单不带点
                return self._html(groupui.route_page(tok, int(seg[0]), int(seg[2]), msg))
            if len(seg) == 3 and seg[0].isdigit() and seg[1] == "challenge" \
                    and seg[2].isdigit():
                # 单个赛事的完成度榜：谁跑了多少、谁已达标的那个页面
                return self._html(groupui.challenge_page(tok, int(seg[0]), int(seg[2]), msg))
            if len(seg) == 1 and seg[0].isdigit():
                # 老链接的 ?range=month（组页曾支持月榜）统一送到榜单页：
                # 组页只做本周榜，月口径归 /board 管。
                if (q.get("range") or [""])[0] == "month":
                    return self._see_other(f"/groups/{int(seg[0])}/board?range=month")
                try:
                    _fn = int((q.get("feed") or ["0"])[0] or 0)
                    _fc = int((q.get("open") or ["0"])[0] or 0)
                except ValueError:
                    _fn, _fc = 0, 0
                return self._html(groupui.group_page(
                    tok, int(seg[0]), msg, _fn, _fc, (q.get("at") or [""])[0]))
            return self._json({"ok": False, "error": "没有这个页面"}, 404)
        if u.path == "/sports":
            if not self._is_loopback():
                return self._json({"ok": False, "error": "只允许本机操作"}, 403)
            if not getattr(self.server, "sports_token", ""):
                self.server.sports_token = secrets.token_urlsafe(32)
            return self._html(ui.sports_page(self.server.sports_token))
        if u.path in ("/plan", "/plan.html"):
            return self._html(ui.plan_page(parse_qs(u.query)))
        if u.path in ("/plan/edit", "/plan/edit.html", "/plan/draft", "/plan/draft.html"):
            if not self._is_loopback():
                return self._json({"ok": False, "error": "只允许本机操作"}, 403)
            # 编辑页能改课表，和运动设置一样要页面令牌 + 仅回环访问
            if not getattr(self.server, "plan_token", ""):
                self.server.plan_token = secrets.token_urlsafe(32)
            msg = (parse_qs(u.query).get("msg") or [""])[0]
            fn = ui.plan_draft_page if "/draft" in u.path else ui.plan_edit_page
            return self._html(fn(self.server.plan_token, msg))
        if u.path in ("/training", "/__training/status"):
            if not self._is_loopback():
                return self._json({"ok": False, "error": "只允许本机操作"}, 403)
            from . import trainingui
            if u.path.endswith("/status"):
                return self._json(trainingui.auth_status())
            if not getattr(self.server, "training_token", ""):
                self.server.training_token = secrets.token_urlsafe(32)
            return self._html(trainingui.render(parse_qs(u.query), self.server.training_token))
        if u.path in ("/files", "/files.html"):
            return self._html(ui.files_page(root))
        if u.path in ("/privacy", "/privacy.html"):
            return self._html(ui.privacy_page())
        if u.path in ("/event", "/event.html"):
            from . import eventui
            return self._html(eventui.render_page(parse_qs(u.query)))
        if u.path in ("/event-doc", "/event-doc.html"):
            from . import eventui
            return self._html(eventui.render_doc_page(parse_qs(u.query)))
        if u.path in ("/posture", "/posture.html"):
            # 和 AI 页面同一条闸门：它会把照片发给模型服务商，只许本机打开
            if not self._is_loopback():
                return self._json({"ok": False, "error": "姿态评估只允许本机打开"}, 403)
            from . import postureui
            return self._html(postureui.render())
        if u.path.startswith("/event-file/"):
            return self._event_file(u.path)
        if u.path.startswith("/basemap/"):
            return self._basemap(u.path)
        if u.path.startswith("/media/"):
            return self._media(u.path)
        if u.path.startswith("/data/"):
            return self._data(u.path)
        if u.path in ("", "/", "/index.html"):
            return self._send_index()
        return super().do_GET()

    def _basemap(self, path: str) -> None:
        """活动图底图：同源提供 `data/basemaps/*.png`。

        App 的 WebView 加载的是 http 页面，页面里 `file://` 图片会被拦，
        所以底图必须走同一个 127.0.0.1 服务。文件名串与尺寸/版本绑定，
        内容不变，给长缓存；路径先 resolve 再校验是否还在缓存目录内。
        """
        from . import basemap as BM

        name = path[len("/basemap/"):]
        if not re.fullmatch(r"[A-Za-z0-9._-]+\.png", name):
            return self._json({"ok": False, "error": "底图名不合法"}, 404)
        try:
            p = (BM.CACHE / name).resolve()
            p.relative_to(BM.CACHE.resolve())
        except (ValueError, OSError):
            return self._json({"ok": False, "error": "底图不存在"}, 404)
        if not p.is_file():
            return self._json({"ok": False, "error": "底图不存在"}, 404)
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=2592000")
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass

    def _progress(self, root: Path) -> dict:
        """正在生成的任务。

        判活靠 `at`（原生每涨 1% 写一次）：超过 30 分钟没动静就当作残留删掉，
        否则 App 被杀之后产物页会永远显示一条 42% 的假进度。
        """
        d = root / "_progress"
        jobs: list[dict] = []
        now = dt.datetime.now().timestamp()
        if d.is_dir():
            for f in sorted(d.glob("*.json")):
                try:
                    j = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                try:
                    age = now - float(j.get("at") or 0) / 1000
                except (TypeError, ValueError):
                    age = 1e9
                if age > 1800:
                    try:
                        f.unlink()
                    except OSError:
                        pass
                    continue
                j["pct"] = int(max(0, min(100, int(j.get("pct") or 0))))
                jobs.append(j)
        jobs.sort(key=lambda x: x.get("at") or 0)
        return {"ok": True, "jobs": jobs}

    def _frame_post(self, u) -> None:
        """回放视频的逐帧上传。

        动画页带 `?export=1` 时，每画完一帧就 POST 一张 JPEG；这里按 run 落到
        `output/_frames/<run>/f_00001.jpg`。run、帧号都做白名单与范围校验，
        避免被当成任意文件写入。
        """
        if not self._is_loopback():
            return self._json({"ok": False, "error": "只允许本机"}, 403)
        q = parse_qs(u.query)
        run = (q.get("run") or [""])[0]
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,32}", run):
            return self._json({"ok": False, "error": "run 不合法"}, 400)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > 12 * 1024 * 1024:
            return self._json({"ok": False, "error": "帧过大"}, 413)
        body = self.rfile.read(n) if n else b""
        root = Path(self.directory) / "_frames" / run
        try:
            root.mkdir(parents=True, exist_ok=True)
            if u.path.startswith("/__frame"):
                try:
                    i = int((q.get("i") or ["0"])[0])
                except ValueError:
                    i = -1
                if not 0 <= i <= 20000:
                    return self._json({"ok": False, "error": "帧号越界"}, 400)
                # 先写临时文件再原子改名：导出改成「边收边编」之后，编码器会
                # 盯着 f_XXXXX.jpg 出现就立刻读，直接 write_bytes 会读到写了一半
                # 的 JPEG（花屏甚至解码失败）。.tmp 不匹配 f_*.jpg，进度不受影响。
                target = root / f"f_{i:05d}.jpg"
                tmp = root / f".f_{i:05d}.tmp"
                tmp.write_bytes(body)
                os.replace(tmp, target)
            elif u.path.startswith("/__timeline"):
                (root / "_timeline.json").write_bytes(body or b"[]")
            elif u.path.startswith("/__diag"):
                # 页面自报「底图/照片/视频加载到哪一步」：导出卡住时
                # 原生侧靠它给出具体原因，不用再猜
                (root / "_diag.json").write_bytes(body or b"{}")
            else:
                (root / "_done.json").write_text(
                    json.dumps({"from": (q.get("from") or ["0"])[0]}),
                    encoding="utf-8")
        except OSError as e:
            return self._json({"ok": False, "error": f"写帧失败：{e}"}, 500)
        self.send_response(204)
        self.end_headers()

    def _media(self, path: str) -> None:
        """素材片段/照片：`data/media/` 下的文件，给播放器的 `<video>` 用。

        `file://` 在 WebView 里会被拦，所以和底图一样走同源 URL；`<video>`
        要 seek 就必须支持 Range，否则 `currentTime` 设不上去。
        """
        base = Path(getattr(self.server, "media_dir", self.directory))
        name = path[len("/media/"):]
        if not re.fullmatch(r"[A-Za-z0-9._-]+\.(mp4|mov|m4v|jpg|jpeg|png)", name):
            return self._json({"ok": False, "error": "素材名不合法"}, 404)
        try:
            p = (base / name).resolve()
            p.relative_to(base.resolve())
        except (ValueError, OSError):
            return self._json({"ok": False, "error": "素材不存在"}, 404)
        if not p.is_file():
            return self._json({"ok": False, "error": "素材不存在"}, 404)
        data = p.read_bytes()
        ext = p.suffix.lower()
        ctype = ("video/mp4" if ext in (".mp4", ".m4v") else
                 "video/quicktime" if ext == ".mov" else
                 "image/png" if ext == ".png" else "image/jpeg")
        return self._send_file(data, ctype, cache="public, max-age=600")

    def _data(self, path: str) -> None:
        """只读放行 `data/` 下的底图、素材与回放缓存。

        播放器 HTML 里存的是本机绝对路径，到了 http 页面会被解析成 `/data/...`。
        这里按「目录前缀 + 扩展名」双白名单放行；`config.json`、`session.json`、
        `*.db` 这些一律 404，不会因为多开一条路由就把私有数据暴露出去。
        """
        name = path[len("/data/"):]
        if not re.fullmatch(
                r"(basemaps|media|motion_cache|thumbs)/"
                r"[A-Za-z0-9._-]+\.(png|jpg|jpeg|mp4|mov|m4v)",
                name):
            return self._json({"ok": False, "error": "不允许读取"}, 404)
        base = Path(getattr(self.server, "data_dir", self.directory))
        try:
            p = (base / name).resolve()
            p.relative_to(base.resolve())
        except (ValueError, OSError):
            return self._json({"ok": False, "error": "不存在"}, 404)
        if not p.is_file():
            return self._json({"ok": False, "error": "不存在"}, 404)
        ext = p.suffix.lower()
        ctype = ("video/mp4" if ext in (".mp4", ".m4v") else
                 "video/quicktime" if ext == ".mov" else
                 "image/png" if ext == ".png" else "image/jpeg")
        return self._send_file(p.read_bytes(), ctype, cache="public, max-age=86400")

    def _send_file(self, data: bytes, ctype: str, *, cache: str) -> None:
        """带 Range 的文件响应：`<video>` 要 seek 就必须支持 206。"""
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if m and (m.group(1) or m.group(2)):
            start = max(0, min(int(m.group(1) or 0), len(data) - 1))
            end = min(int(m.group(2)) if m.group(2) else len(data) - 1, len(data) - 1)
            end = max(start, end)
            chunk = data[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            try:
                self.wfile.write(chunk)
            except BrokenPipeError:
                pass
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass

    def _event_race(self, u) -> None:
        """赛中打点：开始 / 到达 / 撤销 / 重置。

        比赛当天最怕点错，所以「撤销」必须顺手；重置要二次确认（前端用 JS 拦一道）。
        """
        if not self._is_loopback():
            return self._json({"ok": False, "error": "只允许本机操作"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 2048:
            return self._json({"ok": False, "error": "参数不合法"}, 400)
        qs = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))

        def one(k: str) -> str | None:
            v = (qs.get(k) or [""])[0].strip()
            return v or None

        from . import event as E
        err, okmsg = "", ""
        act = one("act") or ""
        try:
            slug0 = one("slug")
            st = E.set_race(slug0, act, idx=one("idx"))
            if act == "pass":
                t = (st.get("passed") or {}).get(str(int(one("idx") or 0)))
                okmsg = ("已记录 %s 到达" % (t[11:16] if t else "")) if t else "已记录"
            elif act == "unpass":
                okmsg = "已撤销该点"
            elif act == "start":
                okmsg = "已开始，每到一个 CP 把那一行向左滑"
            elif act == "reset":
                okmsg = "赛况已清空"
            elif act == "finish":
                okmsg = "已记录完赛"
        except ValueError as e:
            err = str(e)
        except Exception as e:
            err = "保存失败：%s" % e
        self._back(one("slug"), err=err, ok=okmsg, anchor="race")

    def _event_edit(self, u) -> None:
        """赛事页的编辑表单：目标 / CP 表 / 补给参数。

        统一走「表单 POST + 303 回 /event」，校验失败也用 303 带 err 参数回去——
        手机上弹 JSON 报错等于没提示，回到页面上用一条黄色横条说清楚更好。
        """
        if not self._is_loopback():
            return self._json({"ok": False, "error": "只允许本机操作"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 8192:
            return self._json({"ok": False, "error": "参数不合法"}, 400)
        qs = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))

        def one(k: str) -> str | None:
            v = (qs.get(k) or [""])[0].strip()
            return v or None

        from . import event as E
        slug = one("slug")
        err = ""
        try:
            if u.path == "/__event/target":
                # 表单已改成时/分/秒三个框；goal_h 留给旧页面与 CLI
                _gh = one("goal_h")
                if not _gh and (one("goal_hh") or one("goal_mm") or one("goal_ss")):
                    try:
                        _gh = str(float(one("goal_hh") or 0)
                                  + float(one("goal_mm") or 0) / 60
                                  + float(one("goal_ss") or 0) / 3600)
                    except ValueError:
                        raise ValueError("目标完赛要填数字") from None
                E.set_target(slug, goal_h=_gh, status=one("status"),
                             start_time=one("start_time"))
            elif u.path == "/__event/fuel":
                E.set_fuel(slug, carb_g_h=one("carb_g_h"), fluid_ml_h=one("fluid_ml_h"),
                           sodium_mg_h=one("sodium_mg_h"), carry_ratio=one("carry_ratio"),
                           carry_ml=one("carry_ml"), stop_min=one("stop_min"),
                           dropbag_stop_min=one("dropbag_stop_min"))
            else:
                changes = {}
                for key, val in qs.items():
                    m = re.fullmatch(r"km_(\d+)", key)
                    if m:
                        changes.setdefault(m.group(1), {})["km"] = val[0]
                    m = re.fullmatch(r"cut_(\d+)", key)
                    if m:
                        changes.setdefault(m.group(1), {})["cutoff"] = val[0]
                E.set_cps(slug, changes)
        except ValueError as e:
            err = str(e)
        except Exception as e:                       # 写盘失败也回到页面说清楚
            err = "保存失败：%s" % e

        # 保存成功要把人放回原来那一块（不然从页面底部提交，回到顶部像是没保存），
        # 并且给一条绿条确认。错误仍然回同一块，红条就出现在出错的地方。
        anchor = {"/__event/fuel": "fuel", "/__event/target": "adjust",
                  "/__event/course": "adjust"}.get(u.path, "")
        okmsg = ("已保存补给设置，并更新各段的建议用量" if u.path == "/__event/fuel"
                 else "已保存检查点表，并更新预计到达时间和关门时间对比" if u.path == "/__event/course"
                 else "已保存目标，并更新预计到达时间和补给安排")
        self._back(slug, err=err, ok=okmsg, anchor=anchor)

    def _event_file(self, path: str) -> None:
        """赛事目录里的附件（规程 PDF、海报、参赛号截图）。

        单独一条路由而不是放开 `/data/`：`data/` 下有 session.json 与数据库，
        白名单只放 `events/<slug>/` 这一层，扩展名再卡一遍。
        """
        from urllib.parse import unquote
        from . import event as E

        rel = unquote(path[len("/event-file/"):])
        if "/" not in rel:
            return self._json({"ok": False, "error": "路径不合法"}, 404)
        slug, fn = rel.split("/", 1)
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,60}", slug):
            return self._json({"ok": False, "error": "路径不合法"}, 404)
        if not re.fullmatch(r"[^/\\<>:*?\"|]{1,120}\.(pdf|png|jpe?g|txt|md|csv|gpx|ics|xlsx|docx)",
                            fn, re.I):
            return self._json({"ok": False, "error": "不允许读取"}, 404)
        base = E._dir(slug).resolve()
        try:
            p = (base / fn).resolve()
            p.relative_to(base)
        except (ValueError, OSError):
            return self._json({"ok": False, "error": "不存在"}, 404)
        if not p.is_file():
            return self._json({"ok": False, "error": "不存在"}, 404)
        ext = p.suffix.lower()
        ctype = {".pdf": "application/pdf", ".png": "image/png",
                 ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".txt": "text/plain; charset=utf-8",
                 ".md": "text/plain; charset=utf-8",
                 ".csv": "text/csv; charset=utf-8",
                 ".gpx": "application/gpx+xml", ".ics": "text/calendar",
                 ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                 ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                 }.get(ext, "application/octet-stream")
        return self._send_file(p.read_bytes(), ctype, cache="no-store")

    def _event_attach(self, u) -> None:
        """把手机里选中的文件（规程 PDF、参赛号截图）存进赛事目录。

        文件名只保留 basename 并按白名单卡扩展名：这个目录会通过
        `/event-file/` 对外提供下载，放进来什么就得保证能被安全地读走。
        """
        if not self._is_loopback():
            return self._json({"ok": False, "error": "只允许本机上传"}, 403)
        from urllib.parse import unquote
        from . import event as E

        qs = parse_qs(u.query)
        name = unquote((qs.get("name") or [""])[0]).strip()
        slug = (qs.get("slug") or [""])[0].strip()
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        # 请求体先读掉再校验：提前 return 而不读 body，会让 keep-alive 连接里
        # 残留一段未消费的数据，客户端读响应时会拿到 IncompleteRead（实测踩过）
        data = self.rfile.read(n) if 0 < n <= 20 * 1024 * 1024 else b""
        if not name or not data:
            return self._json({"ok": False, "error": "文件为空或超过 20 MB"}, 400)
        if not slug:
            # 不能回落到「下一场」：用户在看 105K 时「下一场」可能是 43K，
            # 静默写错档案比报错难查得多
            return self._json({"ok": False, "error": "缺少赛事参数"}, 400)

        ev = E.load_event(slug)
        base = os.path.basename(name.replace("\\", "/"))
        base = re.sub(r"[^\w.\-\u4e00-\u9fa5 ]+", "_", base).strip() or "附件"
        ext = os.path.splitext(base)[1].lower()
        if ext not in E.ATTACH_EXT:
            return self._json({"ok": False, "error": "不支持这个文件类型：%s" % (ext or "无扩展名")}, 400)
        stem = os.path.splitext(base)[0]
        dst = E._dir(ev["slug"]) / base
        k = 2
        while dst.exists():                      # 重名不覆盖，加序号
            dst = E._dir(ev["slug"]) / ("%s (%d)%s" % (stem, k, ext))
            k += 1
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
        except OSError as e:
            return self._json({"ok": False, "error": "写入失败：%s" % e}, 500)
        return self._back(ev["slug"], anchor="docs",
                          ok="已添加附件 %s（%s）"
                             % (dst.name, E.human_size(dst.stat().st_size)))

    def _posture_assess(self, u) -> None:
        """一张照片 + 一个动作 → 判读 + 方案。返回 JSON，由页面自己插进 DOM。

        照片只在内存里过一遍：base64 之后交给 llm，不落盘。姿态照片比附件敏感，
        留在磁盘上没有任何必要——用户要留证据的话，相册里有原图。
        """
        if not self._is_loopback():
            return self._json({"ok": False, "error": "只允许本机操作"}, 403)
        import base64
        from . import posture as P

        qs = parse_qs(u.query)
        move = (qs.get("move") or [""])[0].strip()
        if move not in P.MOVES:
            return self._json({"ok": False, "error": "未知动作"}, 400)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        # 先读掉 body 再校验：提前 return 会让 keep-alive 里残留一段没消费的数据，
        # 下一次请求直接 IncompleteRead（附件上传踩过同一个坑）
        data = self.rfile.read(n) if 0 < n <= 12 * 1024 * 1024 else b""
        if not data:
            return self._json({"ok": False, "error": "没收到照片，或文件超过 12 MB"}, 400)
        mime = (self.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
        if not mime.startswith("image/"):
            mime = "image/jpeg"
        from . import llm as _llm

        try:
            res = P.assess(base64.b64encode(data).decode(), move, mime=mime)
        except ValueError as e:
            # 判读内容的问题（JSON 被截断、照片没传等）：原样给，别扯模型能力
            return self._json({"ok": False, "error": str(e)}, 502)
        except _llm.LLMError as e:
            d = e.as_dict()
            d["error"] = (d.get("error") or "模型调用失败") + \
                "。确认这个模型支持图片输入（deepseek-v4.1-flash / mimo-v2.5 / kimi-k3）"
            return self._json(d, 502)
        except Exception as e:                    # noqa: BLE001
            return self._json(
                {"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        # 判读结果落盘：切出页面再回来要能接着看（照片本身不进磁盘）
        try:
            from . import posturestore as _PS
            _PS.save_result(move, res)
        except Exception:                          # noqa: BLE001
            pass                                   # 存历史失败不该让判读白跑
        return self._json({"ok": True, "result": res})

    def _posture_clear(self, u) -> None:
        """清空本机的判读记录（照片本来就没存）。"""
        if not self._is_loopback():
            return self._json({"ok": False, "error": "只允许本机操作"}, 403)
        from . import posturestore as PS

        PS.clear()
        return self._json({"ok": True})

    def _posture_plan(self, u) -> None:
        """把已完成的几张判读合成一页：逐项摘要 + 一份统一方案。

        为什么不每张各出一份方案：同一块薄弱项（比如两侧都膝内扣）会被
        重复安排三遍，用户还得自己合并。`posture.plan()` 本来就接多份结果，
        按命中次数排序，这里只负责把前端攒的结果传进去。
        """
        if not self._is_loopback():
            return self._json({"ok": False, "error": "只允许本机操作"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 256 * 1024:
            return self._json({"ok": False, "error": "参数不合法"}, 400)
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            return self._json({"ok": False, "error": "参数不是合法 JSON"}, 400)
        from . import posture as P, postureui

        raw = payload.get("results")
        if not isinstance(raw, list):
            return self._json({"ok": False, "error": "缺少 results"}, 400)
        # 结果来自前端，只用于渲染；仍然只接受本模块认识的动作 id
        clean = [r for r in raw[:8]
                 if isinstance(r, dict) and r.get("move") in P.MOVES]
        return self._json({"ok": True, "html": postureui.summary_html(clean)})

    def _event_doc(self, u) -> None:
        """资料条目：加一条 / 删一条。"""
        if not self._is_loopback():
            return self._json({"ok": False, "error": "只允许本机操作"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 16 * 1024:
            return self._json({"ok": False, "error": "参数不合法"}, 400)
        qs = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))

        def one(k: str) -> str | None:
            v = (qs.get(k) or [""])[0].strip()
            return v or None

        from . import event as E

        slug = one("slug")
        err, okmsg = "", ""
        try:
            if (one("act") or "add") == "edit":
                idx = int(one("idx") or -1)
                E.update_doc(slug, idx, title=one("title") or "",
                             body=one("body") or "", url=one("url") or "")
                okmsg = "已保存"
            elif (one("act") or "add") == "del":
                idx = int(one("idx") or -1)
                if not E.del_doc(slug, idx):
                    raise ValueError("这条资料已经不存在了")
                okmsg = "已删除"
            else:
                kind = one("kind") or "misc"
                E.add_doc(slug, kind, one("title") or "", one("body"), one("url"))
                okmsg = "已加进资料"
        except ValueError as e:
            err = str(e)
        except Exception as e:
            err = "保存失败：%s" % e
        self._back(slug, err=err, ok=okmsg, anchor="docs")

    def _event_file_op(self, u) -> None:
        """附件的写操作（改名 / 删除）。

        ⚠ 方法名不能叫 `_event_file`：同名会把上面那个 GET 读附件的
        `_event_file(path)` 整个覆盖掉，`/event-file/...` 一律 500。
        """
        if not self._is_loopback():
            return self._json({"ok": False, "error": "只允许本机操作"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 8 * 1024:
            return self._json({"ok": False, "error": "参数不合法"}, 400)
        qs = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))

        def one(k: str) -> str:
            return ((qs.get(k) or [""])[0] or "").strip()

        from . import event as E

        slug = one("slug")
        act = one("act")
        err, okmsg = "", ""
        try:
            if act == "rename":
                E.rename_file(slug, one("name"), one("new_name"))
                okmsg = "已改名"
            elif act == "del":
                E.remove_file(slug, one("name"))
                okmsg = "已删除附件"
            else:
                raise ValueError("不认识的操作")
        except ValueError as e:
            err = str(e)
        except Exception as e:
            err = ("改名失败：%s" % e) if act == "rename" else ("删除失败：%s" % e)
        self._back(slug, err=err, ok=okmsg, anchor="docs")

    def _event_trip(self, u) -> None:
        """行程与住宿：增 / 删 / 补地址 / 改提前量 / 按类型关开提醒。

        地址解析走高德 Web 服务，没 Key 或没认出来也照常存文字——
        不能因为定位失败就把用户订好的酒店信息丢掉。
        「已订」按 kind 提交：待办锚点是比赛日，不绑某条行程。
        """
        if not self._is_loopback():
            return self._json({"ok": False, "error": "只允许本机操作"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 16 * 1024:
            return self._json({"ok": False, "error": "参数不合法"}, 400)
        qs = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))

        def one(k: str) -> str | None:
            v = (qs.get(k) or [""])[0].strip()
            return v or None

        from . import event as E

        slug = one("slug")
        act = one("act") or "add"
        err, okmsg = "", ""
        try:
            if act == "del":
                if not E.trip_del(slug, int(one("idx") or -1)):
                    raise ValueError("这条行程已经不存在了")
                okmsg = "已删除这条行程"
            elif act == "geo":
                item = E.trip_geo(slug, int(one("idx") or -1), one("place") or "")
                if not item:
                    raise ValueError("这条行程已经不存在了")
                okmsg = ("已定位，赛道图上会标出来" if item.get("lat") else
                         "地址没定位到（写具体一点，或确认已配高德 Key）")
            elif act == "rules":
                E.set_booking_rules(slug, {k: one(k) for k in
                                           ("flight", "train", "stay")})
                okmsg = "已更新预订提醒时间"
            elif act in ("book", "unbook"):
                kind = one("kind") or ""
                if not kind and one("idx"):
                    # 旧页面缓存还会带 idx：按那条行程的类型还原
                    try:
                        _its = E.trip_manual(slug)
                        _i = int(one("idx") or -1)
                        if 0 <= _i < len(_its):
                            kind = str(_its[_i].get("kind") or "")
                    except (ValueError, TypeError):
                        pass
                if kind not in E.BOOKING_KINDS:
                    raise ValueError("不知道是哪一类预订，刷新页面再试一次")
                E.set_booking_done(slug, kind, done=(act == "book"))
                okmsg = ("这一类已标为已订，不再提醒" if act == "book"
                         else "这一类已恢复提醒")
            else:
                item = E.trip_add(slug, one("kind") or "other",
                                  one("at") or "", one("label") or "",
                                  one("place") or "", lead=one("lead"))
                okmsg = "已加进行程"
                if item.get("kind") in E.BOOKING_KINDS:
                    okmsg += "，对应类型的预订提醒已自动关闭"
                if one("place") and not item.get("lat"):
                    okmsg += "；地址没定位到（写具体点，或确认已配高德 Key）"
        except ValueError as e:
            err = str(e)
        except Exception as e:
            err = "保存失败：%s" % e
        self._back(slug, err=err, ok=okmsg, anchor="trip")

    def _event_remind(self, u) -> None:
        """首页「临近」里的提醒开关。开关是首页的，所以回到 / 而不是 /event。"""
        if not self._is_loopback():
            return self._json({"ok": False, "error": "只允许本机操作"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 4 * 1024:
            return self._json({"ok": False, "error": "参数不合法"}, 400)
        qs = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))

        def one(k: str) -> str:
            return ((qs.get(k) or [""])[0] or "").strip()

        from . import event as E

        try:
            E.set_remind(one("slug"), one("at"), one("label"), one("on") == "1")
        except Exception:
            pass
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _back(self, slug: str | None, err: str = "", ok: str = "",
              anchor: str = "") -> None:
        """统一的 PRG 回跳：把 slug / 结果 / 锚点一次带齐。"""
        q = []
        if slug:
            q.append("slug=" + quote(slug))
        if err:
            q.append("err=" + quote(err))
        elif ok:
            q.append("ok=" + quote(ok))
        if anchor:
            # at= 告诉页面「提示该出现哪一块」：只跳到 #anchor 的话，
            # 提示条留在页面顶端，用户在底部看不到，等于没反馈
            q.append("at=" + quote(anchor))
        loc = "/event" + (("?" + "&".join(q)) if q else "") + (("#" + anchor) if anchor else "")
        self.send_response(303)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _event_delete(self, u) -> None:
        """删除整场赛事（目录连同附件一起）。

        不可逆，页面那侧有两步确认；这里只做服务端校验——确认是 UI 的礼貌，
        真正的安全边界在这：非本机、slug 不合法、目录不在 EVENTS 下一层、
        没有 event.json，全部拒绝。
        """
        if not self._is_loopback():
            return self._json({"ok": False, "error": "只允许本机操作"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 4096:
            return self._json({"ok": False, "error": "参数不合法"}, 400)
        qs = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
        slug = (qs.get("slug") or [""])[0].strip()
        from . import event as E

        try:
            gone = E.delete_event(slug)
        except ValueError as e:
            return self._back(slug or None, err=str(e))
        rest = E.list_events()
        # 删完跳到剩下的最近一场；一场不剩就回 /event，那边会渲染「建第一场」
        return self._back(rest[0]["slug"] if rest else None,
                          ok="已删除「%s」" % gone["short"])

    def _event_ingest(self, u) -> None:
        """新建赛事 / 粘贴规程导入 CP 表 / 沿用上一届。

        三者都不弹 JSON：成功回到那一场并提示，失败回到导入区并说明原因——
        手机上看到一个 JSON 等于没提示。
        """
        if not self._is_loopback():
            return self._json({"ok": False, "error": "只允许本机操作"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 64 * 1024:          # 规程整段粘贴，比表单大得多
            return self._json({"ok": False, "error": "内容为空或过大（上限 64 KB）"}, 400)
        qs = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))

        def one(k: str) -> str | None:
            v = (qs.get(k) or [""])[0].strip()
            return v or None

        from . import event as E
        from . import ingest as I

        slug = one("slug")
        try:
            if u.path == "/__event/new":
                ev = E.new_event(one("name") or "", one("date") or "",
                                 float(one("distance_km") or 0),
                                 ascent_m=int(float(one("ascent_m") or 0)),
                                 start_time=one("start_time") or "08:00",
                                 priority=one("priority") or "C",
                                 kind=one("kind") or "trail")
                return self._back(ev["slug"], anchor="import",
                                  ok="已建好「%s」，接着可以粘贴 CP 表" % ev["name"])
            if u.path == "/__event/prev":
                src = one("from")
                if not src:
                    raise ValueError("先选一场要沿用的赛事")
                ev = E.load_event(slug)
                draft = I.from_prev(src, ev)
                r = I.apply(ev, draft, by="user")
                if not r["ok"]:
                    raise ValueError(r["error"])
                return self._back(ev["slug"], anchor="import",
                                  ok="已沿用 %d 段。核对改线后在「调整 → CP 表」改"
                                     % r["n_segments"])
            # /__event/import：粘贴规程文字
            ev = E.load_event(slug)
            draft = I.from_text(one("text") or "", ev)
            if not draft["segments"]:
                raise ValueError("没认出 CP 表：需要「CPn 地名 里程 关门」这样的行，"
                                 "整段粘贴规程里的表格最稳")
            r = I.apply(ev, draft, by="user")
            if not r["ok"]:
                bad = "；".join(i["msg"] for i in I.errors(r["issues"])[:2])
                raise ValueError("数据没通过校验，未写入：" + bad)
            return self._back(ev["slug"], anchor="import",
                              ok="已导入 %d 个 CP。往下核对「赛况与余量」"
                                 % r["n_segments"])
        except ValueError as e:
            return self._back(slug, err=str(e), anchor="import")
        except Exception as e:
            return self._back(slug, err="导入失败：%s" % e, anchor="import")

    def _event_cp_ocr(self, u) -> None:
        """CP 表截图 → 结构化 CP 行（走大模型视觉，校验不过不写盘）。

        和 GPX 一样：App 把图片原样 POST 过来，服务端只认回环。识别结果
        先过 cpocr.clean 的递增/合理范围校验，再交给 ingest.apply 走和
        「粘贴文字」完全相同的写盘路径——两条入口在档案里没有区别。
        """
        if not self._is_loopback():
            return self._json({"ok": False, "error": "只允许本机操作"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return self._json({"ok": False, "error": "没收到图片"}, 400)
        if n > 12 * 1024 * 1024:
            return self._json({"ok": False, "error": "图片太大（上限 12 MB）"}, 413)
        data = self.rfile.read(n)
        mime = (self.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
        if not mime.startswith("image/"):
            return self._json({"ok": False, "error": "只收图片"}, 400)

        import base64

        from . import cpocr
        from . import event as E
        from . import ingest as I
        from . import llm

        try:
            ev = E.load_event(parse_qs(u.query).get("slug", [None])[0])
        except Exception as e:
            return self._json({"ok": False, "error": "赛事不存在：%s" % e}, 400)

        try:
            r = cpocr.parse_image(base64.b64encode(data).decode("ascii"), mime, ev)
        except llm.LLMError as e:
            return self._json({"ok": False, "error": e.message, "code": e.kind}, 400)
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        except Exception as e:                      # noqa: BLE001
            return self._json({"ok": False, "error": "识别失败：%s" % e}, 500)

        if not r["segments"]:
            return self._json({
                "ok": False,
                "error": "这张图里没认出 CP 行。表格要尽量裁到只剩表格、别带页眉页脚；"
                         "实在认不出就手动贴文字。"}, 400)
        if r["problems"]:
            return self._json({"ok": False,
                               "error": "识别结果没通过校验，未写入："
                                        + "；".join(r["problems"][:2])}, 400)

        try:
            out = I.apply(ev, {"segments": r["segments"]}, by="ocr")
        except Exception as e:                      # noqa: BLE001
            return self._json({"ok": False, "error": "写入失败：%s" % e}, 500)
        if not out.get("ok"):
            bad = "；".join(i["msg"] for i in I.errors(out.get("issues") or [])[:2])
            return self._json({"ok": False,
                               "error": "数据没通过校验，未写入：" + (bad or "未知原因")}, 400)
        return self._json({"ok": True, "n": out.get("n_segments") or len(r["segments"]),
                           "model": r.get("model") or "",
                           "km": r["segments"][-1]["km"]})

    def _event_gear(self, u) -> None:
        """勾选一条强制装备（页面上的 checkbox 直接 POST 到这里）。

        用表单 POST + 303 回跳，不写 JS：WebView 里少一层异步，
        断网、脚本被拦也能用。body 只有 idx / done 两个字段。
        """
        if not self._is_loopback():
            return self._json({"ok": False, "error": "只允许本机操作"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 4096:
            return self._json({"ok": False, "error": "参数不合法"}, 400)
        qs = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
        try:
            idx = int((qs.get("idx") or ["-1"])[0])
        except ValueError:
            idx = -1
        done = (qs.get("done") or ["1"])[0] in ("1", "on", "true")
        from . import event as E
        ev = E.load_event((qs.get("slug") or [None])[0])
        if idx < 0 or not E.set_gear(ev["slug"], idx, done):
            return self._json({"ok": False, "error": "没有这一条"}, 404)
        # 回到赛事页（PRG：Post/Redirect/Get，避免刷新重复提交）
        g = E.gear_progress(ev["slug"])
        self._back(ev["slug"], ok="装备 %d/%d" % (g["done"], g["total"]), anchor="gear")

    def _event_gpx(self, u) -> None:
        """接收 App 从系统文件选择器 / 分享进来的 GPX，落到赛事目录并解析。

        走本机回环 + 大小上限：GPX 是几十 KB 到几 MB 的文本，给 40 MB 足够，
        再大就当异常请求拒掉（避免有人从局域网灌文件）。
        """
        if not self._is_loopback():
            return self._json({"ok": False, "error": "只允许本机导入"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return self._json({"ok": False, "error": "空文件"}, 400)
        if n > 40 * 1024 * 1024:
            return self._json({"ok": False, "error": "文件过大（上限 40 MB）"}, 413)
        raw = self.rfile.read(n)
        from . import event as E
        ev = E.load_event(parse_qs(u.query).get("slug", [None])[0])
        d = E._dir(ev["slug"])
        d.mkdir(parents=True, exist_ok=True)
        dst = d / "track.gpx"
        dst.write_bytes(raw)
        try:
            r = E.parse_gpx(dst, slug=ev["slug"])
        except Exception as e:
            try:
                dst.unlink()
            except OSError:
                pass
            return self._json({"ok": False, "error": "解析失败：%s" % e}, 400)
        off = abs(r["ascent_m"] - ev["ascent_m"]) > ev["ascent_m"] * 0.1
        return self._json({
            "ok": True, "slug": ev["slug"], "km": r["total_km"],
            "ascent": r["ascent_m"], "descent": r["descent_m"],
            "points": r["points"], "profile": len(r["profile"]),
            "official_ascent": ev["ascent_m"], "ascent_off": off,
        })

    def _files_delete(self) -> None:
        """批量删除产物。只允许本机，且只认 output 一层的产物后缀。

        校验全部放在服务端：页面上的勾选只是 UI，真正决定删什么的在这里。
        三个拒绝条件 —— 非本机、带路径分隔符或 `..`、不是产物后缀 ——
        保证最坏情况也只是删掉 output 里的一个 html/png/mp4。
        """
        if not self._is_loopback():
            return self._json({"ok": False, "error": "只允许在本机删除"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > 256 * 1024:
            return self._json({"ok": False, "error": "请求过大"}, 413)
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            names = payload.get("names") or []
            if not isinstance(names, list) or not names:
                raise ValueError("names 必须是非空数组")
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return self._json({"ok": False, "error": "参数不合法"}, 400)

        root = Path(self.directory).resolve()
        removed: list[str] = []
        freed = 0
        skipped: list[str] = []
        for raw in names[:200]:
            name = str(raw or "")
            if (not name or name.startswith("_") or "/" in name
                    or "\\" in name or ".." in name):
                skipped.append(name)
                continue
            p = (root / name).resolve()
            if p.parent != root or p.suffix.lower() not in LIST_EXT or not p.is_file():
                skipped.append(name)
                continue
            try:
                freed += p.stat().st_size
                p.unlink()
                removed.append(name)
            except OSError:
                skipped.append(name)
        self._json({"ok": True, "removed": removed, "skipped": skipped,
                    "freed": freed})

    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        if u.path == "/__route/note":
            # 批注带 base64 照片，body 远大于通用上限，这里自己读
            return self._route_note_post()
        if u.path.startswith("/__training/"):
            if not self._is_loopback():
                return self._json({"ok": False, "error": "只允许本机操作"}, 403)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 < n <= 4096:
                    raise ValueError("参数过大或为空")
                values = json.loads(self.rfile.read(n).decode("utf-8"))
                token = getattr(self.server, "training_token", "")
                if not isinstance(values, dict) or not token or not isinstance(values.get("token"), str) or not hmac.compare_digest(values["token"], token):
                    return self._json({"ok": False, "error": "页面已过期，请重新打开训练导入"}, 403)
                from . import appapi as A, trainingui
                if u.path == "/__training/auth":
                    return self._json(trainingui.start_auth())
                if u.path == "/__training/recheck":
                    return self._json(A.coros_workout_check(str(values.get("import_key") or "")))
                if u.path not in ("/__training/submit", "/__training/check"):
                    return self._json({"ok": False, "error": "不支持的操作"}, 404)
                return self._json(A.coros_workout_submit(str(values.get("preview_id") or ""), u.path.endswith("/check")))
            except (ValueError, OSError) as e:
                return self._json({"ok": False, "error": str(e)}, 400)
        if u.path == "/__sports":
            if not self._is_loopback():
                return self._json({"ok": False, "error": "只允许本机操作"}, 403)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 < n <= 4096:
                    raise ValueError("参数过大或为空")
                values = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode("utf-8"), keep_blank_values=True).items()}
                token = getattr(self.server, "sports_token", "")
                if not token or not hmac.compare_digest(values.get("token", ""), token):
                    return self._json({"ok": False, "error": "页面已过期，请重新打开运动设置"}, 403)
                from .sports import save_settings
                save_settings(values)
            except (ValueError, OSError) as e:
                return self._html(ui.sports_page(getattr(self.server, "sports_token", ""), str(e)))
            self.send_response(303)
            self.send_header("Location", "/sports")
            self.end_headers()
            return
        if u.path.startswith("/__plan/"):
            return self._plan_post(u)
        if u.path.startswith(("/__group/", "/__account/")):
            return self._group_post(u)
        if u.path.startswith("/__run/"):
            return self._run_post(u)
        # 回放视频：动画页 `?export=1` 会自己逐帧 POST 上来，这里只负责收。
        if u.path.startswith(("/__frame", "/__done", "/__timeline", "/__diag")):
            return self._frame_post(u)
        if u.path == "/__event/gpx":
            return self._event_gpx(u)
        if u.path == "/__event/cp_ocr":
            return self._event_cp_ocr(u)
        if u.path == "/__event/gear":
            return self._event_gear(u)
        if u.path in ("/__event/target", "/__event/course", "/__event/fuel"):
            return self._event_edit(u)
        if u.path == "/__event/race":
            return self._event_race(u)
        if u.path in ("/__event/new", "/__event/import", "/__event/prev"):
            return self._event_ingest(u)
        if u.path == "/__event/delete":
            return self._event_delete(u)
        if u.path == "/__event/doc":
            return self._event_doc(u)
        if u.path == "/__event/trip":
            return self._event_trip(u)
        if u.path == "/__event/attach":
            return self._event_attach(u)
        if u.path == "/__event/file":
            return self._event_file_op(u)
        if u.path == "/__event/remind":
            return self._event_remind(u)
        if u.path == "/__posture/assess":
            return self._posture_assess(u)
        if u.path == "/__posture/plan":
            return self._posture_plan(u)
        if u.path == "/__posture/clear":
            return self._posture_clear(u)
        if u.path == "/__files/delete":
            # 产物页的批量删除。放在 gate 之前反而更安全：这里自己做本机校验，
            # 不带访问码的局域网访客一律 403。
            return self._files_delete()
        if not u.path.startswith("/ai/"):
            return self._json({"ok": False, "error": "不支持的接口"}, 404)
        if not self._is_loopback():
            return self._json({"ok": False, "error": "AI 接口只允许本机调用"}, 403)

        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > 512 * 1024:          # 对话历史再长也不该超过这个量级
            return self._json({"ok": False, "error": "请求过大"}, 413)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise ValueError("body 必须是对象")
        except Exception as e:
            return self._json({"ok": False, "error": f"参数不合法：{e}"}, 400)

        action = u.path[len("/ai/"):].strip("/") or "status"
        from . import aiapi

        try:
            out = aiapi.dispatch(action, payload)
        except Exception as e:      # dispatch 内部已兜底，这里只防万一
            out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return self._json(out, 200 if out.get("ok") else 400)

    # ---------------------------------------------------------------- 计划编辑
    def _font(self, name: str) -> None:
        """包内的字体文件。

        只白名单一个文件名，不做路径拼接——`../../` 这类穿越在本地服务上
        同样能读到用户目录，没必要为了通用性留个洞。
        """
        allowed = {"expedition-serif.woff2": "font/woff2"}
        ctype = allowed.get(name)
        if not ctype:
            return self._json({"ok": False, "error": "没有这个文件"}, 404)
        from . import contentpack

        f = contentpack.path(name)
        if f is None:
            # 素材是按需下发的：第一次进远征页时可能还没下完。回 404，
            # CSS 那边有系统衬线兜底，页面照常可读。
            return self._json({"ok": False, "error": "素材还没下发"}, 404)
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 字体只在重打包时变，长缓存让 WebView 不必每次重新解析 1.5 MB
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(data)

    # 白名单：结局底图。和字体同一个理由——只认文件名，不做路径拼接，
    # 免得 `../../` 在这条本地服务上读出用户目录。
    CONTENT_TYPES = {".webp": "image/webp", ".png": "image/png",
                     ".jpg": "image/jpeg", ".woff2": "font/woff2"}

    def _content(self, name: str) -> None:
        """包内静态资源（结局底图、字体）。"""
        import os as _os
        from pathlib import Path as _P

        ctype = self.CONTENT_TYPES.get(_os.path.splitext(name)[1].lower())
        if not ctype or "/" in name or "\\" in name or name.startswith("."):
            return self._json({"ok": False, "error": "没有这个文件"}, 404)
        from . import contentpack

        f = contentpack.path(name)
        if f is None:
            return self._json({"ok": False, "error": "没有这个文件"}, 404)
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(data)

    def _run_page(self, q: dict) -> str:
        from . import group as G, runsui

        if not self._is_loopback():
            return runsui.empty_page("offline", "页面只能在手机上打开")
        # 页面令牌要在这里也生成：用户可能直接从「我的」点进 /run，
        # 没经过小组页。不生成的话页面能打开，但一选选项就报「页面已过期」。
        if not getattr(self.server, "group_token", ""):
            self.server.group_token = secrets.token_urlsafe(32)
        gid = 0
        try:
            gid = int((q.get("gid") or ["0"])[0])
        except (TypeError, ValueError):
            gid = 0
        if not gid:
            # 没指定组时取第一个——从「我的」点进来时不该再选一次
            lst = G.groups()
            items = lst.get("groups") or []
            gid = int(items[0]["id"]) if items else 0
        if not gid:
            return runsui.empty_page("nogroup")
        msg = (q.get("msg") or [""])[0]
        st = G.run_state(gid)
        if not st.get("ok"):
            # 三种打不开要分开引导：没登录去账号页、不是成员去建组、其余重试。
            # 直接把「HTTP 403」糊上去，玩家除了退回上一页没别的可做。
            if st.get("need_login"):
                return runsui.empty_page("login")
            err = str(st.get("error") or "")
            if "成员" in err or "小组" in err:
                return runsui.empty_page("nogroup")
            return runsui.empty_page("offline", err[:60])
        # 赛季结束就直接进结算页：玩家下次打开想看到的是结果，
        # 不是一条停在最后一幕、还在催他跑步的路线。
        if (st.get("season") or {}).get("ended"):
            return runsui.outcome_page(getattr(self.server, "group_token", ""),
                                       G.run_outcome(gid), gid)
        return runsui.run_page(getattr(self.server, "group_token", ""), msg, st, gid)

    def _run_post(self, u) -> None:
        """远征的写接口。和小组页同一套「本机 + 页面令牌 + 303 回跳」。"""
        from . import group as G

        if not self._is_loopback():
            return self._json({"ok": False, "error": "只允许本机操作"}, 403)
        token = getattr(self.server, "group_token", "")
        gid = 0
        note = ""
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= 65536:
                raise ValueError("参数过大或为空")
            vals = {k: v[0] for k, v in
                    parse_qs(self.rfile.read(n).decode("utf-8"),
                             keep_blank_values=True).items()}
            # gid 要**在令牌校验之前**解出来：令牌过期时也得能回到远征页
            # 显示提示，否则因为 gid 还是 0 会被踢到组列表 —— 那看起来同样像
            # 「点了一下、什么都没发生」，只是换了个页面。
            try:
                gid = int(vals.get("gid") or 0)
            except (TypeError, ValueError):
                gid = 0
            if not token or not hmac.compare_digest(vals.get("token", ""), token):
                raise PermissionError("页面已过期，请重新打开")
            act = u.path.rstrip("/").split("/")[-1]
            if u.path.rstrip("/").endswith("/camp/upgrade"):
                # 营地在远征页里，但数据存在小组库
                r = G.camp_upgrade(gid)
            elif act == "fork":
                r = G.run_fork(gid, vals.get("option") or "")
            elif act == "event":
                r = G.run_event(gid, int(vals.get("slot") or 0),
                                vals.get("option") or "")
            elif act == "trial":
                tid = vals.get("trial_id") or ""
                done = str(vals.get("done") or "") in ("1", "true", "True")
                evid, note = self._trial_evidence(gid, tid)
                r = G.run_trial(gid, tid, done, evidence=evid)
                note = note or (r.get("note") or "")
            elif act == "rescue":
                r = G.run_rescue(gid, int(vals.get("user_id") or 0))
            elif act == "target":
                r = G.run_target(gid, float(vals.get("km") or 0))
            else:
                raise ValueError("不支持的操作")
            msg = "" if r.get("ok") else (r.get("error") or "操作失败")
            if not msg and note:
                msg = note
        except PermissionError as e:
            msg = str(e)
        except (ValueError, TypeError) as e:
            msg = str(e) or "参数不对"
        except Exception as e:  # noqa: BLE001 - 网络异常不能让页面白屏
            msg = f"{type(e).__name__}: {e}"
        back = f"/run?gid={gid}" if gid else "/groups"
        sep = "&" if "?" in back else "?"
        return self._see_other(back + (sep + "msg=" + quote(msg) if msg else ""))

    def _note_img(self, gid: int, rid: int, nid: int) -> None:
        """批注照片。本地代理到云端，鉴权仍在云端做（组内成员才给）。"""
        from . import group as G

        ok, data, ctype = G.note_image(gid, rid, nid)
        if not ok or not data:
            return self._json({"ok": False, "error": "照片取不到"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", ctype or "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        # private：这是组内可见的照片，不该被任何中间缓存留下来
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass

    def _route_note_post(self) -> None:
        """批注的写接口（新建 / 删除）。

        **自己读 body**，不走 `_read_body`：照片以 base64 上送，压过的
        ~100 KB 图片转成 base64 就是 ~140 KB，超过通用请求体的上限，
        走那条路会被判「请求过大」。
        """
        import json as _js

        from . import group as G

        if not self._is_loopback():
            return self._json({"ok": False, "error": "只允许本机操作"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if not 0 < n <= 2 * 1024 * 1024:
            return self._json({"ok": False, "error": "数据过大或为空"}, 413)
        try:
            d = _js.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(d, dict):
                raise ValueError
        except Exception:      # noqa: BLE001
            return self._json({"ok": False, "error": "参数不合法"}, 400)
        tok = getattr(self.server, "group_token", "")
        if not tok or not hmac.compare_digest(str(d.get("token") or ""), tok):
            return self._json({"ok": False, "error": "页面已过期，请重新打开"}, 403)
        try:
            gid, rid = int(d.get("gid") or 0), int(d.get("rid") or 0)
        except (TypeError, ValueError):
            return self._json({"ok": False, "error": "参数不合法"}, 400)
        if d.get("op") == "del":
            r = G.note_del(gid, rid, int(d.get("nid") or 0))
        else:
            r = G.note_add(gid, rid, d.get("x") or 0, d.get("y") or 0,
                           str(d.get("text") or ""), str(d.get("image") or ""))
        return self._json(r, 200 if r.get("ok") else 400)

    def _route_gpx(self, gid: int, rid_s: str) -> None:
        """下载一条组内共享路线的 GPX。

        文件名只用 ASCII：中文名要靠 RFC 5987 的 `filename*`，而 Android
        WebView 对它的支持不一致，下载后经常变成一串百分号。用日期+id
        反而更好认。
        """
        from . import group as G

        try:
            rid = int(rid_s)
        except ValueError:
            return self._json({"ok": False, "error": "路线 id 不合法"}, 400)
        r = G.route(gid, rid)
        if not r.get("ok"):
            return self._json(
                {"ok": False, "error": r.get("error") or "打不开这条路线"}, 404)
        rt = r.get("route") or {}
        pts = rt.get("points") or []
        if len(pts) < 2:
            return self._json({"ok": False, "error": "这条路线没有轨迹点"}, 404)
        data = _gpx_xml(rt, pts).encode("utf-8")
        day = str(rt.get("date") or "")[:10] or "route"
        name = f"RunnerCoach-{day}-{rid}.gpx"
        self.send_response(200)
        self.send_header("Content-Type", "application/gpx+xml; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass

    def _see_other(self, url: str) -> None:
        """PRG 回跳。手机上按返回/下拉刷新不会重复提交上一次操作。"""
        self.send_response(303)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()
        self.close_connection = True

    def _group_csv(self, gid: int) -> None:
        """本周每人一行，给组长导到表格里看。数据只出本机，不上传。"""
        from . import group as G

        today = dt.date.today().isoformat()
        b = G.board(gid, "week", today)
        if not b.get("ok"):
            return self._json({"ok": False, "error": b.get("error") or "拉不到榜单"}, 404)
        try:
            # 每人的本周目标在 /week 的 goal_rows 里（/season 只给「我」的那一个）
            _wk = G.week(gid, today)
            _goal = ({int(r["user_id"]): float(r.get("km") or 0)
                      for r in (_wk.get("goal_rows") or [])} if _wk.get("ok") else {})
        except Exception:      # noqa: BLE001 - 导出不该因为目标读不到就失败
            _goal = {}
        _from, _to = str(b.get("from") or ""), str(b.get("to") or "")
        lines = ["昵称,本周里程 km,天数,次数,计划完成率,本周目标 km,目标完成度"]
        for x in (b.get("rows") or []):
            _km = float(x.get("distance_m") or 0) / 1000.0
            _g = _goal.get(int(x.get("user_id") or 0)) or None
            _rate = x.get("plan_rate")
            lines.append(",".join([
                '"%s"' % str(x.get("alias") or "").replace('"', '""'),
                f"{_km:.1f}", str(int(x.get("days") or 0)), str(int(x.get("acts") or 0)),
                (f"{float(_rate) * 100:.0f}%" if _rate is not None else ""),
                (f"{float(_g):g}" if _g else ""),
                (f"{min(100.0, _km / float(_g) * 100):.0f}%" if _g else ""),
            ]))
        if b.get("hidden"):
            lines.append('"#另有 %d 人关闭了共享，未列入"' % int(b["hidden"]))
        name = f"group-{gid}-{_from}-{_to}.csv"
        return self._csv("\n".join(lines), name)

    def _group_report(self, gid: int) -> dict:
        """手动上报一次。首次加入发 30 天，之后 14 天。

        顺手给「没有明细」的活动补抓一次：明细是按需懒抓的（打开详情才抓），
        骑行、走路这类不常点开的活动本来永远没有轨迹，动态里就一直是
        「无轨迹」。拿不到客户端（没登录/离线）就照旧只报里程，不阻塞。
        """
        from . import appapi as A, group as G
        from .store import Store

        store = Store(A.DB)
        client = None
        try:
            client = A._client()
        except Exception:      # noqa: BLE001 - 没客户端就不补抓，上报照常
            client = None
        try:
            return G.report_group(store, gid, first=not G.seen(gid, store),
                                  client=client)
        finally:
            store.conn.close()

    def _share_route(self, gid: int, act_id: str, confirmed: bool) -> dict:
        """把一条活动的轨迹传到组里。

        轨迹从本地库现读：点有 600 个，塞进表单会撞 64 KB 上限，也不该让
        浏览器经手这些坐标。所以表单只传活动 id，服务端读库、抽稀、再上传。
        """
        from . import appapi as A, group as G
        from .store import Store

        if not confirmed:
            return {"ok": False, "error": "要先勾「知道会包含位置」"}
        act_id = (act_id or "").strip()
        if not act_id:
            return {"ok": False, "error": "没选活动"}
        st = Store(A.DB)
        try:
            act = st.get_activity(act_id)
            if not act:
                return {"ok": False, "error": "本地找不到这条活动"}
            dist = int(float(act.get("distance_m") or 0))
            if dist < 3000:
                return {"ok": False, "error": "短于 3 km 的路线不收录"}
            det = st.get_detail(act_id) or {}
            # 缓存的明细是精简版：轨迹在 `track`（[[lat,lon],...]），
            # 不是云端原始的 geoPolylineDTO。build_track 在这里只会返回空。
            pts = det.get("track") or []
            if len(pts) > 600:                 # 服务端上限 800，600 够画
                pts = pts[::max(1, len(pts) // 600)][:600]
            if len(pts) < 3:
                return {"ok": False, "error": "这条活动没有轨迹"}
            return G.share_route(gid, act_id, str(act.get("name") or "")[:24],
                                 dist, str(act.get("date") or "")[:10], pts)
        finally:
            st.conn.close()

    def _trial_evidence(self, gid: int, trial_id: str) -> tuple[dict, str]:
        """给一条试炼配证据：从本周活动里抽原始分圈。

        返回 (evidence, 提示)。提示只用来解释「为什么没核验成」，不预判结论——
        结论由服务端算，这里说「达标了」就成了第二个判定点。
        """
        from . import appapi as A, group as G
        from .store import Store

        try:
            st = G.run_state(gid)
            season = (st or {}).get("season") or {}
            ev = next((e for e in ((st or {}).get("events") or [])
                       if e.get("id") == trial_id), None)
            q = (ev or {}).get("quality") or {}
            if not q or not season.get("week_from"):
                return {}, ""
            store = Store(A.DB)
            try:
                evid = G.trial_evidence(store, q,
                                        str(season["week_from"])[:10],
                                        str(season["week_to"])[:10])
            finally:
                store.conn.close()
        except Exception:            # noqa: BLE001 - 抽不到证据不该让上报失败
            return {}, ""
        if not evid:
            return {}, "本周还没同步到能核验的跑步记录，先按未核验记。"
        return evid, ""

    def _group_post(self, u) -> None:
        """训练小组的写接口：登录、建组、加入、共享、上报、赛事、退出。"""
        from . import account as AC, group as G

        if not self._is_loopback():
            return self._json({"ok": False, "error": "只允许本机操作"}, 403)
        token = getattr(self.server, "group_token", "")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= 65536:
                raise ValueError("参数过大或为空")
            values = {k: v[0] for k, v in
                      parse_qs(self.rfile.read(n).decode("utf-8"),
                               keep_blank_values=True).items()}
            if not token or not hmac.compare_digest(values.get("token", ""), token):
                raise PermissionError("页面已过期，请重新打开")
        except PermissionError as e:
            return self._see_other("/groups?msg=" + quote(str(e)))
        except (ValueError, OSError) as e:
            return self._see_other("/groups?msg=" + quote(str(e)))

        def as_int(v, default=0):
            try:
                return int(str(v).strip() or 0)
            except (TypeError, ValueError):
                return default

        parts = [p for p in u.path.strip("/").split("/") if p]
        msg, back = "", "/groups"
        try:
            if parts and parts[0] == "__account":
                act = parts[1] if len(parts) > 1 else ""
                acct = (values.get("account") or "").strip()
                if act == "send":
                    r = AC.send_code(acct)
                    if r.get("ok"):
                        q = {"account": acct, "sent": "1"}
                        # 从小组进来登录的，回跳后页内「返回」还要指回小组；
                        # 丢了 from 会变成「原生返回回小组、页内返回回我的」。
                        if (values.get("from") or "") == "groups":
                            q["from"] = "groups"
                        if r.get("dev_code"):
                            q["code"] = r["dev_code"]
                        return self._see_other("/account?" + urlencode(q))
                    msg, back = r.get("error") or "发送失败", "/account"
                elif act == "login":
                    # 昵称只在服务端判定为新账号时生效，老账号传了也不会被覆盖
                    r = AC.verify(acct, values.get("code"),
                                  values.get("nickname") or "")
                    if r.get("ok"):
                        return self._see_other("/groups")
                    msg = r.get("error") or "登录失败"
                    _q = {"account": acct}
                    if (values.get("from") or "") == "groups":
                        _q["from"] = "groups"
                    back = "/account?" + urlencode(_q)
                elif act == "nickname":
                    r = AC.set_nickname(values.get("nickname") or "")
                    if r.get("ok"):
                        return self._see_other(
                            "/account?" + urlencode({"msg": f"昵称已改成「{r['nickname']}」"}))
                    msg, back = r.get("error") or "改名失败", "/account"
                elif act == "logout":
                    AC.logout()
                    return self._see_other("/account")
                elif act == "delete":
                    # 注销是不可逆的：表单要勾确认，服务端也只认 confirm=1
                    if (values.get("confirm") or "") != "1":
                        raise ValueError("注销要先勾确认")
                    r = AC.delete_account()
                    if r.get("ok"):
                        return self._see_other("/me")
                    msg, back = r.get("error") or "注销失败", "/account"
                else:
                    raise ValueError("不支持的操作")
            else:
                second = parts[1] if len(parts) > 1 else ""
                if second == "create":
                    r = G.create(values.get("name"), values.get("intro"),
                                 as_int(values.get("max_members"), 20), values.get("alias"))
                    if r.get("ok"):
                        return self._see_other(f"/groups/{r['group']['id']}")
                    msg = r.get("error") or "建组失败"
                    # 失败回列表页并强制出列表：否则只有 1 个组的人会被
                    # 「单组直达」弹回组详情，错误提示连同表单一起消失
                    back = "/groups?list=1"
                elif second == "join":
                    r = G.join(values.get("code"), values.get("alias"))
                    if r.get("ok"):
                        # 需要批准的组：申请已发出但还不是成员，送去组页会看到
                        # 「你不是这个组的成员」，所以回列表并把话说清。
                        if r.get("pending"):
                            return self._see_other(
                                "/groups?list=1&msg=" + quote("申请已发出，等组长批准"))
                        return self._see_other(f"/groups/{r['group']['id']}")
                    msg = r.get("error") or "加入失败"
                    back = "/groups?list=1"
                else:
                    gid = as_int(second)
                    if not gid:
                        raise ValueError("组 id 不对")
                    act = parts[2] if len(parts) > 2 else ""
                    back = f"/groups/{gid}"
                    if act == "leave":
                        r = G.leave(gid)
                        if r.get("ok"):
                            back = "/groups"
                        else:
                            msg = r.get("error") or "退出失败"
                    elif act == "share":
                        r = G.set_share(gid, values.get("share") or "summary")
                        msg = "已保存" if r.get("ok") else (r.get("error") or "保存失败")
                        back = f"/groups/{gid}/settings"
                    elif act == "relay":
                        on = (values.get("on") or "").strip() == "1"
                        r = G.set_relayshare(gid, on)
                        if r.get("ok"):
                            msg = ("已开启：队友登录过期时可以用这台手机帮他登一次"
                                   if on else "已关闭：不再替队友登录")
                        else:
                            msg = r.get("error") or "保存失败"
                        back = f"/groups/{gid}/settings"
                    elif act == "alias":
                        r = G.set_alias(gid, values.get("alias") or "")
                        msg = (f'昵称已改成「{r.get("alias")}」' if r.get("changed")
                               else "昵称没变") if r.get("ok") \
                            else (r.get("error") or "改名失败")
                        back = f"/groups/{gid}/settings"
                    elif act == "archive":
                        # 解散是不可逆的：表单里必须勾「确认」，服务端也要求 confirm=1
                        if (values.get("confirm") or "") != "1":
                            raise ValueError("解散要先勾确认")
                        r = G.archive(gid)
                        if r.get("ok"):
                            return self._see_other(
                                "/groups?list=1&msg=" + quote("已解散这个组"))
                        msg = r.get("error") or "解散失败"
                        back = f"/groups/{gid}/settings"
                    elif act == "transfer":
                        r = G.transfer(gid, as_int(values.get("uid")))
                        msg = "已转让" if r.get("ok") else (r.get("error") or "转让失败")
                        back = f"/groups/{gid}/settings"
                    elif act == "purge":
                        # 删完这个组就不存在了，不能回 /groups/{gid}
                        r = G.purge(gid, values.get("confirm") or "")
                        if r.get("ok"):
                            return self._see_other(
                                "/groups?list=1&msg=" + quote("已彻底删除这个组"))
                        msg = r.get("error") or "删除失败"
                        back = "/groups?list=1"
                    elif act == "route_share":
                        r = self._share_route(
                            gid, values.get("act") or "",
                            (values.get("confirm") or "") == "1")
                        msg = ("路线已分享" if r.get("ok")
                               else (r.get("error") or "分享失败"))
                        back = f"/groups/{gid}/map"
                    elif act == "route_del":
                        r = G.unshare_route(gid, as_int(values.get("rid")))
                        msg = "已撤回" if r.get("ok") else (r.get("error") or "撤回失败")
                        back = f"/groups/{gid}/map"
                    elif act == "role":
                        # 任命/撤销教练。服务端只让组长调用，这里不重复判。
                        _role = values.get("role") or "member"
                        r = G.set_role(gid, as_int(values.get("uid")), _role)
                        if r.get("ok"):
                            msg = "已任命为教练" if _role == "coach" else "已撤销教练"
                        else:
                            msg = r.get("error") or "改角色失败"
                        back = f"/groups/{gid}/settings"
                    elif act == "report":
                        r = self._group_report(gid)
                        msg = (f'已上报 {r.get("saved", 0)} 天'
                               if r.get("ok") else (r.get("error") or "上报失败，已排队"))
                        back = f"/groups/{gid}/settings"
                    elif act == "goal":
                        # 目标是可选的：空着就是「只改预言」。以前这里硬要求
                        # 填 km，于是「我只想猜别人」这件事根本做不了。
                        raw = (values.get("km") or "").strip()
                        km = None
                        if raw:
                            try:
                                km = float(raw)
                            except ValueError:
                                raise ValueError("目标要是数字，单位 km") from None
                        pred = (values.get("predict") or "").strip()
                        _p = int(pred) if pred.isdigit() else None
                        r = G.set_goal(gid, km, _p, touch_predict=True)
                        if not r.get("ok"):
                            msg = r.get("error") or "保存失败"
                        elif km is not None and _p:
                            msg = f"本周目标 {r.get('km'):g} km，预言已记下"
                        elif km is not None:
                            msg = f"本周目标 {r.get('km'):g} km"
                        elif _p:
                            msg = "预言已记下"
                        else:
                            # 目标没改、也没选人 = 撤掉这周的预言（或本来就空）
                            msg = "已保存"
                    elif act == "mapshare":
                        from . import appapi as _A
                        from .store import Store as _Store

                        on = (values.get("on") or "").strip() == "1"
                        _st = _Store(_A.DB)
                        try:
                            r = G.set_mapshare(gid, on, store=_st)
                            # 刚打开就把本周已经跑过的格子推上去，不用等下次上报；
                            # 关掉时服务端已经删干净，不用再发。
                            if r.get("ok") and on:
                                from . import explore as EX

                                mm = EX.week_cell_meters(_st)
                                if mm:
                                    G.push_cells(gid, list(mm), mm)
                        finally:
                            _st.conn.close()
                        msg = ("路线共享已打开" if on else "已关闭，格子已删除") \
                            if r.get("ok") else (r.get("error") or "设置失败")
                        back = f"/groups/{gid}/map"
                    elif act == "comment":
                        # 表情和留言走同一个接口：只点了表情也要发一次空文本
                        _aid = as_int(values.get("act"))
                        if not _aid:
                            raise ValueError("找不到这条动态")
                        r = G.comment(gid, _aid,
                                      values.get("text") or "", values.get("emoji") or "")
                        if r.get("ok"):
                            # 回到**这条动态**，不是组页顶部：动态已经并进组页，
                            # 原来跳 `/feed` 会被 303 到 `#feed`，等于重新加载
                            # 整页——刚打的那条评论连同所有展开的卡片一起收起。
                            # `?open=` 让服务端把这条的评论区默认展开，滚到哪
                            # 由 KEEP_JS 记住的位置决定。
                            return self._see_other(
                                f"/groups/{gid}?open={_aid}#a{_aid}")
                        msg = r.get("error") or "发不出去"
                        back = f"/groups/{gid}?open={_aid}#a{_aid}"
                    elif act in ("comment_edit", "comment_del"):
                        # 改和删共用一张表单（按钮带 op=save / del），所以这里
                        # 先看 op 再决定走哪条。只能动自己的：服务端按 user_id
                        # 校验，别人的回 404——这里不做「是不是我的」判断。
                        _cid = as_int(values.get("id"))
                        if not _cid:
                            raise ValueError("找不到这条评论")
                        _aid = as_int(values.get("act"))
                        _del = act == "comment_del" or (values.get("op") or "save") == "del"
                        if _del:
                            r = G.comment_del(gid, _cid)
                            msg = "已删除" if r.get("ok") else (r.get("error") or "删不掉")
                        else:
                            r = G.comment_edit(gid, _cid,
                                               values.get("text") or "",
                                               values.get("emoji") or "")
                            msg = "已改好" if r.get("ok") else (r.get("error") or "改不了")
                        # 改完/删完回原动态，别把人扔回列表
                        if _aid:
                            return self._see_other(
                                f"/groups/{gid}?open={_aid}"
                                + (f"&msg={quote(msg)}" if msg and not r.get("ok") else "")
                                + f"#a{_aid}")
                    elif act == "joinmode":
                        _m = values.get("mode") or "open"
                        r = G.set_join_mode(gid, _m)
                        msg = ("已改成「需我批准」" if _m == "approve" else "已改成「扫码即入」") \
                            if r.get("ok") else (r.get("error") or "保存失败")
                        back = f"/groups/{gid}/settings"
                    elif act == "decide":
                        _u = as_int(values.get("uid"))
                        _ok = (values.get("ok") or "") == "1"
                        r = G.decide(gid, _u, _ok)
                        msg = ("已批准" if _ok else "已拒绝") if r.get("ok") \
                            else (r.get("error") or "处理失败")
                        back = f"/groups/{gid}/settings"
                    elif act == "rotate":
                        r = G.rotate(gid)
                        msg = (f'新邀请码 {r["invite_code"]}' if r.get("ok")
                               else (r.get("error") or "换码失败"))
                    elif act == "challenge":
                        # 建/结束赛事都锚回「赛事」这一块，并让错误提示就落在
                        # 那个区块里（?at=chal）：表单在页面中部，提示却渲染
                        # 在页首——不这么做的话，建失败看起来就是「点了没反应」。
                        back = f"/groups/{gid}?at=chal#chal"
                        if len(parts) > 3 and parts[3].isdigit() and parts[-1] == "close":
                            r = G.challenge_close(gid, int(parts[3]))
                            msg = "已结束" if r.get("ok") else (
                                "结束赛事失败：" + str(r.get("error") or "未知原因"))
                        else:
                            r = G.challenge_create(
                                gid, values.get("title"), values.get("kind") or "distance",
                                as_int(values.get("target")),
                                values.get("start_date") or "", values.get("end_date") or "")
                            # 前缀写「建赛事失败」而不是只给原因：`_msg` 按关键词
                            # 判红绿，光靠「标题 1–30 个字」这种句子会被渲染成
                            # 绿色的成功提示。
                            msg = "已建赛事" if r.get("ok") else (
                                "建赛事失败：" + str(r.get("error") or "未知原因"))
                    else:
                        raise ValueError("不支持的操作")
        except (ValueError, TypeError) as e:
            msg = str(e)
        except Exception as e:  # noqa: BLE001 - 网络异常不能让页面白屏
            msg = f"{type(e).__name__}: {e}"
        # back 可能带 #锚点（比如 #chal）：msg 必须拼在 # 前面，
        # 否则 query 会变成 fragment 的一部分，服务端和 JS 都读不到。
        frag = ""
        if "#" in back:
            back, frag = back.split("#", 1)
            frag = "#" + frag
        sep = "&" if "?" in back else "?"
        return self._see_other(f"{back}{sep}msg=" + quote(msg or "") + frag)

    def _plan_post(self, u) -> None:
        """计划的写接口：保存 / 生成 / 确认 / 放弃 / 推送 / 撤销 / 开关。

        全部 303 回跳（PRG）：手机上按返回、下拉刷新都不会重复提交。
        """
        from . import appapi as A

        if not self._is_loopback():
            return self._json({"ok": False, "error": "只允许本机操作"}, 403)
        token = getattr(self.server, "plan_token", "")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= 65536:
                raise ValueError("参数过大或为空")
            values = {k: v[0] for k, v in
                      parse_qs(self.rfile.read(n).decode("utf-8"),
                               keep_blank_values=True).items()}
            if not token or not hmac.compare_digest(values.get("token", ""), token):
                raise PermissionError("页面已过期，请回到计划页重新打开")
        except PermissionError as e:
            return self._html(ui.plan_edit_page(token, str(e)), 403)
        except (ValueError, OSError) as e:
            return self._html(ui.plan_edit_page(token, str(e)), 400)

        act = u.path.rsplit("/", 1)[-1]
        msg, back = "", "/plan/edit"
        try:
            if act == "save":
                days = _plan_days_from_form(values)
                if not days:
                    msg = "没有可保存的改动"
                else:
                    r = A.plan_save_week(json.dumps(days, ensure_ascii=False))
                    msg = f'已保存 {r.get("saved")} 天' if r.get("ok") \
                        else (r.get("error") or "保存失败")
            elif act == "generate":
                try:
                    run_days = int(values.get("run_days") or 0)
                except ValueError:
                    run_days = 0
                try:
                    week_km = float(values.get("week_km") or 0)
                except ValueError:
                    week_km = 0.0
                A.plan_rules_set(values.get("race_deload") == "1")
                r = A.plan_generate(run_days, week_km)
                if r.get("ok") and r.get("week_km"):
                    back = "/plan/draft"
                else:
                    msg = (r.get("error") or r.get("reason")
                           or "还没有跑量可参考。可以填写每周跑量，或在今日页同步跑步记录后重试。")
            elif act == "confirm":
                r = A.plan_confirm(values.get("override") == "1")
                back = "/plan"
                msg = (f'已保存 {r.get("saved")} 天的训练安排，下周起会自动安排训练'
                       if r.get("ok") else (r.get("error") or "确认失败"))
            elif act == "discard":
                A.plan_discard()
                msg = "草稿已放弃"
            elif act == "clear":
                r = A.plan_clear()
                msg = (f'已撤销 {r.get("cleared")} 天改动' if r.get("ok")
                       else (r.get("error") or "撤销失败"))
            elif act == "rules":
                r = A.plan_rules_set(values.get("race_deload") == "1")
                msg = ("已关闭赛前减量" if r.get("race_deload") is False
                       else "已开启赛前减量") if r.get("ok") else (r.get("error") or "保存失败")
            elif act == "push":
                r = A.plan_push(False)
                back = "/plan"
                if r.get("ok"):
                    msg = (f'已发送 {r.get("created")} 节训练到佳明日历，共 {r.get("days")} 天'
                           if r.get("created") else "没有需要推送的课")
                elif r.get("need_login"):
                    msg = r.get("error") or "佳明未登录，先去「我的」登录"
                else:
                    msg = r.get("error") or "推送失败"
            else:
                return self._json({"ok": False, "error": f"不支持的操作：{act}"}, 404)
        except Exception as e:                                  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"

        self.send_response(303)
        self.send_header("Location", f"{back}?msg={quote(msg)}")
        self.end_headers()

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _html(self, html: str, code: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _csv(self, text: str, filename: str) -> None:
        """下载一个 CSV。带 BOM：Excel 直接打开中文不乱码。"""
        body = ("\ufeff" + text).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition",
                         f"attachment; filename=\"{filename}\"")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _gate(self, u, token: str) -> bool:
        """返回 True 表示放行；False 表示已经自己响应过了。"""
        got = (parse_qs(u.query).get("k") or [""])[0]
        if got and hmac.compare_digest(got, token):
            # 种 cookie 后重定向掉 ?k=：地址栏不留令牌，截图/投屏不会带走
            self.send_response(302)
            self.send_header("Set-Cookie", f"gc={token}; Path=/; HttpOnly; SameSite=Lax")
            self.send_header("Location", u.path or "/")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return False
        c = SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except Exception:
            pass
        if "gc" in c and hmac.compare_digest(c["gc"].value, token):
            return True
        body = (
            '<!doctype html><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<body style="font-family:system-ui;padding:36px 20px;line-height:1.8;color:#333">'
            "<h3 style=\"margin:0 0 8px\">需要访问码</h3>"
            "请用电脑上打印出来的<b>完整地址</b>（带 <code>?k=…</code>）打开，"
            "或用手机扫那个地址。只输 IP 是进不来的。</body>"
        ).encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def _send_index(self) -> None:
        # 首页不再是产物文件列表：改成「今日」概览，文件清单降级到 /files。
        self._html(ui.home_page())

    def end_headers(self) -> None:
        if self.path.rsplit(".", 1)[-1].lower() in ("html", "htm"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        # 只记路径，不记查询串（里面可能带令牌）
        sys.stderr.write("  %s %s\n" % (self.address_string(), self.path.split("?")[0]))


def run_server(root: Path, lan: bool = False, port: int = 8765,
               open_: bool = False, token: str | None = None) -> int:
    root = Path(root)
    if not root.is_dir():
        print(f"× 目录不存在：{root}")
        return 2

    host = "0.0.0.0" if lan else "127.0.0.1"
    if lan and not token:
        token = secrets.token_urlsafe(5)

    handler = partial(_Handler, directory=str(root))
    srv = None
    for p in range(port, port + 12):
        try:
            srv = ThreadingHTTPServer((host, p), handler)
            port = p
            break
        except OSError:
            continue
    if srv is None:
        print(f"× {port} 起连续 12 个端口都被占用，换个 --port")
        return 3
    srv.token = token or ""  # type: ignore[attr-defined]
    srv.daemon_threads = True

    # 素材（远征字体 / 结局插画）后台补。放在服务起来之后：先让首屏能开，
    # 再慢慢下那 1.7 MB。失败也不影响任何页面（有兜底）。
    try:
        from . import contentpack
        contentpack.prefetch_async()
    except Exception:      # noqa: BLE001
        pass

    q = f"?k={token}" if token else ""
    print("√ 只读看板服务已启动（关掉本窗口即停止）")
    print(f"   本机  http://127.0.0.1:{port}/{q}")
    if lan:
        ip = _lan_ip()
        print(f"   手机  http://{ip}:{port}/{q}   ← 手机连同一个 WiFi")
        print(f"   访问码 {token}")
        print("   ⚠ 同一 WiFi 下的人拿到这串地址就能看，用完请关掉")
    else:
        print("   手机看：加 --lan 重新启动，会给出局域网地址")
    print()

    if open_:
        from .sysopen import open_any

        open_any(f"http://127.0.0.1:{port}/{q}")

    try:
        from . import races_fetch

        races_fetch.start_background()
    except Exception:
        pass

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
    return 0
