# -*- coding: utf-8 -*-
"""赛事数据：从「最酷」抓「报名中」列表，落到 GARMIN_HOME/data/content/races.json。

为什么是这个源
--------------
「只展示报名窗口内的赛事」这句话，落到数据上就两个字段：`reg_open` /
`reg_close`。而公开接口里没有一家同时给「窗口 + 合法可抓」：
runchina 是 SPA 且有反爬、ITRA 只有日期没有窗口、Ahotu 要商业授权。

最酷是国内唯一满足三条的：服务端渲染（HTML 直接带数据）、每张卡片都写
「报名截止：MM-DD HH:MM」、robots.txt 只 Disallow `/news/wp-content/`。
所以它的「报名中」列表页本身就是一份现成的窗口名单，不用猜、不用爬详情。

抓取纪律
--------
每天最多一轮、每轮最多 2 页（约 200 场）、串行 + 间隔、带自家 UA。
不做并发、不做全站翻页——这是「够用」而不是「爬库」。

为什么 reg_open 是推出来的
--------------------------
列表页只给截止时间。既然一场赛事此刻出现在「报名中」名单里，就说明它的
开放日不晚于今天，所以用量时的「今天」当下界，并用 `reg_open_source:
"inferred"` 标出来是推的，不假装是抓到的。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import time
import urllib.request
from pathlib import Path

# 数据必须落在 GARMIN_HOME（APK 的 filesDir）。写死在包目录的话，
# Chaquopy 每次更新 APK 都会重建源码目录，抓来的数据跟着没。
_HOME_ENV = (os.environ.get("GARMIN_HOME") or "").strip()
HOME = (Path(_HOME_ENV).expanduser() if _HOME_ENV
        else Path(__file__).resolve().parent.parent)
OUT = HOME / "data" / "content" / "races.json"

LIST = "https://zuicool.com/events/reg?page={page}&per-page={per}"
UA = "RunnerCoach/1.0 (personal use; +https://gitee.com/liyufei-lyle/RunnerCoach)"
TIMEOUT = 12
PAGES = 2
PER_PAGE = 100
GAP_HOURS = 20          # 同一天里最多查一次（跨天就再来一轮）

_TAG_RE = re.compile(r"<[^>]+>")
_ENT = {"&middot;": "·", "&amp;": "&", "&lt;": "<", "&gt;": ">",
        "&quot;": '"', "&nbsp;": " ", "&#39;": "'"}

_LOCK = threading.Lock()
_last_try = 0.0


def _clean(text: str) -> str:
    t = _TAG_RE.sub("", str(text or ""))
    for k, v in _ENT.items():
        t = t.replace(k, v)
    return re.sub(r"\s+", " ", t).strip()


def _year_for(month: int, day: int, race_day: dt.date) -> int:
    """报名截止只给 MM-DD，年份要推：一律不晚于比赛日（跨年赛事常见）。"""
    y = race_day.year
    try:
        if dt.date(y, month, day) > race_day:
            y -= 1
    except ValueError:
        return race_day.year
    return y


def _dist_of(name: str) -> float | None:
    """从赛事名里认距离。认不出就留空，不猜。

    聚合平台的名称里通常带「10KM」「50公里」「半程马拉松」，这一条能省掉
    对每场赛事再抓一次详情页（200 次请求 vs 2 次）。
    """
    if "半程" in name:
        return 21.1
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:km|公里|KM|Km)", name)
    if m:
        try:
            v = float(m.group(1))
            if 1 <= v <= 400:
                return v
        except ValueError:
            pass
    if "马拉松" in name and "亲子" not in name:
        return 42.2
    return None


def _place_of(info: str) -> str:
    """「浙江 舟山市 普陀区 普陀大剧院」→「浙江 舟山」，去掉场地名。"""
    seg = [x for x in re.split(r"[\s·]+", info) if x]
    keep = []
    for x in seg:
        if re.search(r"(区|县|镇|街道|路|广场|公园|中心|景区|大厦|门口)", x) and keep:
            break
        keep.append(x)
        if len(keep) >= 2:
            break
    return " ".join(re.sub(r"市$", "", x) for x in keep)


def parse_list(html: str, today: dt.date) -> tuple[list[dict], dict]:
    """列表页 HTML → 记录。只收「报名截止」那一种（= 此刻真在窗口里）。

    返回 (记录, 统计)；统计里分开记「未开报」和「解析不了」，前者是正常
    状态（还没到报名时间），后者才需要看。
    """
    out, stat = [], {"open": 0, "upcoming": 0, "skipped": 0}
    for blk in str(html or "").split('<div class="event"')[1:]:
        m = re.search(r'class="event-a"[^>]*>(.*?)</a>', blk, re.S)
        if not m:
            stat["skipped"] += 1
            continue
        name = _clean(m.group(1))
        if not name:
            stat["skipped"] += 1
            continue
        mu = re.search(r'href="(https://zuicool\.com/event/(\d+))"', blk)
        race_day, place = None, ""
        mi = re.search(r'<div class="info">\s*<p>(.*?)</p>', blk, re.S)
        if mi:
            info = _clean(mi.group(1))
            md = re.search(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", info)
            if md:
                try:
                    race_day = dt.date(int(md.group(1)), int(md.group(2)), int(md.group(3)))
                except ValueError:
                    race_day = None
            place = _place_of(info.split("·", 1)[-1])
        if not race_day:
            stat["skipped"] += 1
            continue

        close_m = re.search(r"报名截止[：:]\s*(?:(20\d{2})[-./])?(\d{1,2})[-./](\d{1,2})"
                            r"(?:\s+(\d{1,2})[:：](\d{2}))?", blk)
        if not close_m:
            if re.search(r"报名开始[：:]", blk):
                stat["upcoming"] += 1          # 还没开报，不在窗口内
            else:
                stat["skipped"] += 1
            continue

        y = (int(close_m.group(1)) if close_m.group(1)
             else _year_for(int(close_m.group(2)), int(close_m.group(3)), race_day))
        try:
            reg_close = dt.date(y, int(close_m.group(2)), int(close_m.group(3)))
        except ValueError:
            stat["skipped"] += 1
            continue
        if reg_close < today:
            stat["skipped"] += 1               # 页面残留的过期条目
            continue
        out.append({
            "id": "zc-" + (mu.group(2) if mu else name[:10]),
            "name": name,
            "reg_open": today.isoformat(), "reg_open_source": "inferred",
            "reg_close": reg_close.isoformat(),
            "date": race_day.isoformat(),
            "dist_km": _dist_of(name), "place": place,
            "url": mu.group(1) if mu else "",
            "source": "最酷 zuicool", "via": "zuicool",
            "checked_at": today.isoformat(), "status": "open",
        })
        stat["open"] += 1
    return out, stat


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read()
        if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
            import gzip

            raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


def fetch(pages: int = PAGES, per_page: int = PER_PAGE,
          today: dt.date | None = None, opener=None) -> tuple[list[dict], dict]:
    """抓 1~N 页。第 1 页失败就返回空（调用方保住旧数据）。"""
    today = today or dt.date.today()
    pages = max(1, min(int(pages), 5))         # 上限 5 页，别贪
    per_page = max(20, min(int(per_page), 100))
    go = opener or _get
    rows, stat = [], {"open": 0, "upcoming": 0, "skipped": 0, "pages": 0, "error": ""}
    for page in range(1, pages + 1):
        url = LIST.format(page=page, per=per_page)
        try:
            html = go(url)
        except Exception as e:                 # noqa: BLE001 网络问题一律降级
            stat["error"] = f"第 {page} 页：{e}"
            break
        one, sk = parse_list(html, today)
        for k in ("open", "upcoming", "skipped"):
            stat[k] += sk[k]
        rows.extend(one)
        stat["pages"] = page
        if len(one) < per_page // 2:           # 这页已经不满，说明到末尾了
            break
        if page < pages:
            time.sleep(1.5)                    # 同域名串行，别压人家服务器

    # 两页之间可能重复（运营把同一场挂了两次），按 id 去重
    seen, uniq = set(), []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        uniq.append(r)
    stat["open"] = len(uniq)
    return uniq, stat


def sync(today: dt.date | None = None, pages: int = PAGES,
         opener=None) -> dict:
    """抓一轮并原子写入。失败时**不动**磁盘上的旧文件。"""
    today = today or dt.date.today()
    rows, stat = fetch(pages=pages, today=today, opener=opener)
    if not rows:
        return {"ok": False, "error": stat.get("error") or "一场都没抓到", **stat}
    data = {"version": 1, "updated": today.isoformat(),
            "source": "zuicool", "items": rows}
    try:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        tmp = OUT.with_name(OUT.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(OUT)
    except OSError as e:
        return {"ok": False, "error": f"写不进 {OUT}：{e}", **stat}
    return {"ok": True, "updated": today.isoformat(), **stat}


def _last_updated() -> str:
    try:
        return str(json.loads(OUT.read_text(encoding="utf-8")).get("updated") or "")
    except (OSError, ValueError):
        return ""


def maybe_update(force: bool = False, today: dt.date | None = None,
                 opener=None) -> dict:
    """跨天才抓一次。线程安全，失败静默——它是可选内容，不能影响主流程。"""
    global _last_try
    today = today or dt.date.today()
    now = time.time()
    if not force:
        if _last_updated()[:10] == today.isoformat():
            return {"ok": True, "skipped": "今天已更新"}
        if now - _last_try < GAP_HOURS * 3600:
            return {"ok": True, "skipped": "刚试过"}
    with _LOCK:
        if not force and _last_updated()[:10] == today.isoformat():
            return {"ok": True, "skipped": "今天已更新"}
        _last_try = now
        r = sync(today=today, opener=opener)
    if not r.get("ok"):
        try:
            from . import diag as _dg

            _dg.log_exc("races", "赛事抓取失败：" + str(r.get("error"))[:200])
        except Exception:
            pass
    return r


_bg_last = 0.0
_bg_lock = threading.Lock()


def start_background(delay: float = 6.0, min_gap_h: float = 1.0) -> None:
    """开个 daemon 线程去更新，不阻塞启动。

    页面每次刷新都会走到这里，所以不是「只起一次」而是「每小时最多起一个」：
    真正决定要不要抓的是 maybe_update 里的「今天已更新」，这里只防线程风暴。
    """
    global _bg_last
    now = time.time()
    if now - _bg_last < min_gap_h * 3600:
        return
    with _bg_lock:
        if now - _bg_last < min_gap_h * 3600:
            return
        _bg_last = now

    def _run() -> None:
        try:
            time.sleep(delay)
            maybe_update()
        except Exception:                      # noqa: BLE001 后台线程不许炸
            pass

    threading.Thread(target=_run, name="races-sync", daemon=True).start()
