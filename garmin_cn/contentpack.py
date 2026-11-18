"""按需下发的素材：远征衬线字体、五张结局插画。

为什么不随包走
--------------
这 1.7 MB 在 APK 里躺着，但大部分人一辈子不会打开远征页；而它们又是
「改文案/换插画就要发版」的东西。放服务器上，首包小 8%，改图不用发版。

失效怎么办
----------
**没有它们页面也能看**：字体有系统衬线兜底，结局图能退化到纯 SVG 画。
所以这里的策略是「悄悄补、悄悄失败」——不弹窗、不阻塞首屏，最多是第一次
看到的样子朴素一点，第二次就有了。

和剧情不是一回事
----------------
剧情（事件池、结局正文）走的是 `/run/*` 接口，按幕下发、要登录、有指纹
护栏。这里只是静态素材，公开可下，不含任何人的数据。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.request
from pathlib import Path

_HOME_ENV = os.environ.get("GARMIN_HOME", "").strip()
HOME = (Path(_HOME_ENV).expanduser() if _HOME_ENV
        else Path(__file__).resolve().parent.parent)

# 下载缓存目录。放 GARMIN_HOME 下面（可写），别写进包目录（只读）。
DIR = HOME / "content"
# 随包自带的（现在只剩证书之类的非素材文件；留着是为了兼容旧包）
BUNDLED = Path(__file__).resolve().parent / "content"

# 需要联网取回来的素材。清单也在服务端，这里是「至少这些」的兜底：
# 服务端连不上时，仍然知道该补哪几个文件。
WANTED = ["expedition-serif.woff2",
          "ending_arrive.jpg", "ending_detour.jpg", "ending_missing.jpg",
          "ending_lost.jpg", "ending_discoverer.jpg"]

INDEX_TTL = 6 * 3600        # 清单缓存：素材随发版才变，问太勤没意义
# 自检会把它关掉：**自检必须离线**，而且不该在仓库目录里造出 content/
# 那种下载缓存（开发时 HOME 就是仓库根）。
ENABLED = True
_index_cache: dict = {"at": 0.0, "files": {}}
_lock = threading.Lock()
_busy = False


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(256 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _base() -> str:
    """服务端地址。取不到就返回空字符串（等于「没法下发」）。"""
    try:
        from . import llm
        return str(llm.load_config().get("platform_base") or "").rstrip("/")
    except Exception:      # noqa: BLE001
        return ""


def index(force: bool = False) -> dict:
    """远端素材清单 {name: {size, sha256}}。失败返回空字典。"""
    now = time.time()
    if not force and _index_cache["files"] and now - _index_cache["at"] < INDEX_TTL:
        return _index_cache["files"]
    base = _base()
    if not base.startswith("https://"):
        return {}
    try:
        from . import tlstrust
        tlstrust.install()
        req = urllib.request.Request(f"{base}/v1/content/index",
                                     headers={"User-Agent": "RunnerCoach/content"})
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:      # noqa: BLE001 - 服务器没起来 / 没发布，都算没有
        return {}
    files = {}
    for f in (data.get("files") or []):
        if isinstance(f, dict) and f.get("name"):
            files[str(f["name"])] = {"size": int(f.get("size") or 0),
                                     "sha256": str(f.get("sha256") or "")}
    _index_cache.update(at=now, files=files)
    return files


def _pkg_file(rel: str) -> Path | None:
    """在**包**里找一个文件（不是下载目录）。

    为什么要自己写一遍、不调 codepack.pkg_file：代码包走覆盖式更新，
    本模块可能是**新代码**，而装载器 `codepack.py` 永远来自 APK，可能是
    **旧版本**——旧版里没有 pkg_file，一调就 AttributeError，被 except
    吞掉之后证书又找不到，HTTPS 全挂。

    这是覆盖式更新的硬约束：**基础路径解析不能依赖任何可能缺失的 API**。
    几行代码重复换来「新旧任意组合都能跑」，值。
    """
    import sys as _s

    mod = _s.modules.get("garmin_cn")
    for base in list(getattr(mod, "__path__", None) or []):
        try:
            p = Path(base) / rel
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def local(name: str) -> Path | None:
    """本地已有的那份（下载目录优先，其次包内）。没有就 None。

    `BUNDLED` 是模块级的 `__file__` 推算值；这个模块自己也可能来自代码包，
    那时它指向下载目录、里面没有素材。所以最后一步用 _pkg_file 顺
    `__path__` 去真包里找——找得到就说明它其实一直随 APK 在。
    """
    for d in (DIR, BUNDLED):
        p = d / name
        if p.is_file() and p.stat().st_size > 0:
            return p
    q = _pkg_file(f"content/{name}")
    if q is not None and q.stat().st_size > 0:
        return q
    return None


def exists(name: str) -> bool:
    return local(name) is not None


def path(name: str) -> Path | None:
    return local(name)


def _fetch(name: str, want: dict) -> bool:
    """下一个文件。先写 .part 再改名——中途断网不能留下一个半个文件，
    否则下次 `exists()` 会以为已经下好了。"""
    base = _base()
    if not base.startswith("https://"):
        return False
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIR / (name + ".part")
    try:
        from . import tlstrust
        tlstrust.install()
        req = urllib.request.Request(f"{base}/v1/content/{name}",
                                     headers={"User-Agent": "RunnerCoach/content"})
        with urllib.request.urlopen(req, timeout=30) as r, tmp.open("wb") as fh:
            while True:
                chunk = r.read(128 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
        size = tmp.stat().st_size
        if want.get("size") and size != want["size"]:
            tmp.unlink(missing_ok=True)
            return False
        if want.get("sha256") and _sha(tmp) != want["sha256"]:
            # 校验不过就当没下过：宁可看 SVG，也不能显示一张被中间人改过的图
            tmp.unlink(missing_ok=True)
            return False
        tmp.replace(DIR / name)
        return True
    except Exception:      # noqa: BLE001
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def ensure(names=None, budget: float = 0.0) -> list[str]:
    """补齐缺的素材，返回这次真正下到的名单。

    `budget` 是总时间预算（秒）。0 表示「不限，但也别太久」——入口处统一
    按 3 秒兜底，避免一次坏网络把页面拖住。
    """
    if not ENABLED:
        return []
    names = list(names or WANTED)
    idx = index()
    todo = []
    for n in names:
        p = local(n)
        want = idx.get(n) or {}
        if p is None:
            todo.append(n)
        elif want.get("sha256") and _sha(p) != want["sha256"]:
            # 有旧版本：服务端换了图，重新取。failing 就当没这一步
            todo.append(n)
    if not todo:
        return []
    limit = budget if budget > 0 else 3.0
    deadline = time.time() + limit
    got = []
    for n in todo:
        if time.time() > deadline:
            break
        if _fetch(n, idx.get(n) or {}):
            got.append(n)
    return got


def prefetch_async(names=None) -> None:
    """后台补素材。App 启动时叫一次，等用户真进远征页时通常已经有了。"""
    global _busy

    if not ENABLED:
        return

    def _run():
        global _busy
        try:
            ensure(names, budget=60)
        finally:
            _busy = False

    with _lock:
        if _busy:
            return
        _busy = True
    threading.Thread(target=_run, name="content-prefetch", daemon=True).start()
