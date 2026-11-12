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
        err = ""
        try:
            E.set_race(one("slug"), one("act") or "", idx=one("idx"))
        except ValueError as e:
            err = str(e)
        except Exception as e:
            err = "保存失败：%s" % e
        loc = "/event" + (("?err=" + quote(err)) if err else "")
        self.send_response(303)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        self.end_headers()

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
                E.set_target(slug, goal_h=one("goal_h"), status=one("status"),
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

        loc = "/event" + (("?err=" + quote(err)) if err else "")
        self.send_response(303)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        self.end_headers()

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
        self.send_response(303)
        self.send_header("Location", "/event")
        self.send_header("Content-Length", "0")
        self.end_headers()

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
