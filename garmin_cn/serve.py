# -*- coding: utf-8 -*-
"""手机看板服务：把 output/ 只读地挂到局域网上，手机浏览器直接看。

为什么需要这一层
----------------
看板本身就是自包含 HTML，问题只在于"怎么递到手机上"：微信传文件要手动找路径、
数据线要驱动、放到网盘又等于把健康数据交给第三方。同一 WiFi 下起一个只读服务，
手机打开一串地址就能看——iPhone 也不用装任何东西。

三条硬规则
----------
1. **只读**：只服务 output/ 下已生成的文件，没有任何写入口。
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
from functools import partial
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from . import history, ui

SERVE_EXT = {".html", ".htm", ".png", ".jpg", ".jpeg", ".mp4", ".json", ".csv"}
LIST_EXT = {".html", ".htm", ".png", ".mp4"}   # 手机上值得点开的；json/csv 只服务不列


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
        return "最新一图流", "今日", kind
    if stem == "act":
        return "最新活动图", "今日", kind
    if stem.startswith("dashboard-"):
        return f"看板 {stem[10:]}", "看板", kind
    if stem.startswith("card-"):
        tail = "" if ext == ".png" else " · 网页"
        return f"一图流 {stem[5:]}{tail}", "图", kind
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


class _Handler(SimpleHTTPRequestHandler):
    server_version = "garmin-coach/0.1"

    # AI 页面与接口：只有本机能调。局域网访问者即便拿到访问码，
    # 也进不了这条通道——它会把健康数据发给模型服务商，还会花钱。
    _AI_PAGES = ("/ai", "/ai.html")

    def _is_loopback(self) -> bool:
        ip = (self.client_address or ("",))[0]
        return ip in ("127.0.0.1", "::1", "localhost")

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        if u.path in self._AI_PAGES:
            if not self._is_loopback():
                return self._json({"ok": False, "error": "AI 页面只允许本机打开"}, 403)
            from . import aiui

            # AI 页有自己的 CSS，只补统一外框与标题节奏，不动它的交互。
            return self._html(_with_fix(aiui.render(), ui.AI_FIX))
        token = getattr(self.server, "token", "")
        if token and not self._gate(u, token):
            return
        # 四个 tab 的页面：同源、只读本地库，App 的 JS 桥照常挂在上面。
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
        if u.path in ("/plan", "/plan.html"):
            return self._html(ui.plan_page(parse_qs(u.query)))
        if u.path in ("/files", "/files.html"):
            return self._html(ui.files_page(root))
        if u.path in ("/privacy", "/privacy.html"):
            return self._html(ui.privacy_page())
        if u.path in ("/event", "/event.html"):
            from . import eventui
            return self._html(eventui.render_page(parse_qs(u.query)))
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
        okmsg = ("补给参数已保存，各段用量已重算" if u.path == "/__event/fuel"
                 else "CP 表已保存，余量已重算" if u.path == "/__event/course"
                 else "目标已保存，余量与补给已重算")
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
            return self._json({"ok": False, "error": "不收这种文件：%s" % (ext or "无扩展名")}, 400)
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
            if (one("act") or "add") == "del":
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
                                  ok="已沿用 %d 段 —— 核对改线后，在「调整 → CP 表」里改"
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
                raise ValueError("数据没通过校验，未写入 —— " + bad)
            return self._back(ev["slug"], anchor="import",
                              ok="已导入 %d 个 CP —— 往下滑核对「赛况与余量」里的余量"
                                 % r["n_segments"])
        except ValueError as e:
            return self._back(slug, err=str(e), anchor="import")
        except Exception as e:
            return self._back(slug, err="导入失败：%s" % e, anchor="import")

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
        # 回放视频：动画页 `?export=1` 会自己逐帧 POST 上来，这里只负责收。
        if u.path.startswith(("/__frame", "/__done", "/__timeline", "/__diag")):
            return self._frame_post(u)
        if u.path == "/__event/gpx":
            return self._event_gpx(u)
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
        if u.path == "/__event/attach":
            return self._event_attach(u)
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
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
    return 0
