"""客户端代码包：从服务器下发的「覆盖式」补丁。

它解决的是**每次发版都要重下 21 MB APK** 这件事。APK 里本来就有完整代码，
这里下发的只是改动过的 .py；挂到 `garmin_cn.__path__` 最前面，同名模块就被
盖掉，其余继续用包里那份。

三条设计约束
------------
1. **包内永远保留完整代码**。首装、离线、下载失败、包损坏，全都回落到包里
   那份——不能因为「更新」把 App 弄成打不开。
2. **整包原子切换**。下载到 `code/<version>.tmp`，校验通过才改名成
   `code/<version>`，再写 `code/current`。半新半旧的模块组合会出「函数签名
   对不上」这种最难查的错。
3. **下次启动生效**。`__path__` 只在进程启动时拼一次，不做热替换。

为什么发 .py 不发 .pyc：pyc 的魔数和解释器版本绑死，升 Python 那天旧包会
**静默**失效；源码包体积差不多，还能直接 diff。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

_HOME_ENV = os.environ.get("GARMIN_HOME", "").strip()
HOME = (Path(_HOME_ENV).expanduser() if _HOME_ENV
        else Path(__file__).resolve().parent.parent)

ROOT = HOME / "code"                     # code/<version>/garmin_cn/*.py
POINTER = ROOT / "current"               # 文本：当前生效的版本号
META = ROOT / "latest.json"              # 上次问到的清单（离线也能显示状态）

ENABLED = True            # 自检会关掉：不许联网、不许在仓库里造文件
_last = {"checked": 0.0, "applied": "", "note": "", "pending": "", "error": ""}
_busy = False
_lock = threading.Lock()      # 模块级：每次 new 一个锁等于没有锁


def _base() -> str:
    try:
        from . import llm
        return str(llm.load_config().get("platform_base") or "").rstrip("/")
    except Exception:      # noqa: BLE001
        return ""


def current_version() -> str:
    try:
        return POINTER.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def current_dir() -> Path | None:
    """当前生效的代码目录。目录不完整就返回 None（回落包内代码）。

    不要求一定有 appapi.py：部分包（验证机制用）只带一两个模块，那也是合法的。
    只要求「目录在、里面至少有一个 .py」——那种才意味着下载解压真的成功过。
    """
    v = current_version()
    if not v:
        return None
    d = ROOT / v / "garmin_cn"
    if not d.is_dir():
        return None
    return d if any(d.glob("*.py")) else None


def apply() -> dict:
    """启动时调用：把已下载的代码包挂到包搜索路径最前面。没有就是 no-op。

    必须在任何业务模块被 import 之前调用（Kotlin 那边是 Python.start() 之后、
    getModule("garmin_cn.appapi") 之前）。晚了只会让一部分模块来自旧代码，
    那比不更新更糟。
    """
    import sys

    if not ENABLED:
        return {"ok": True, "applied": "", "note": "已禁用"}
    d = current_dir()
    if d is None:
        _last["note"] = "没有代码包"
        return {"ok": True, "applied": "", "note": _last["note"]}
    # 挂之前核一次基版本：APK 升级后旧代码包必须让位，否则它会把新版修复
    # 盖回去。不匹配就清掉指针，让下次启动直接用 APK 里的代码。
    meta = _meta_of(d.parent)
    ok_fit, why = _fits(meta)
    if not ok_fit:
        _last["applied"] = ""
        _last["note"] = why
        _last["pending"] = ""
        try:
            POINTER.unlink()
        except OSError:
            pass
        return {"ok": True, "applied": "", "note": why}
    pkg = sys.modules.get("garmin_cn")
    if pkg is None:      # 理论上不会：调用方就在这个包里
        return {"ok": False, "error": "garmin_cn 还没导入"}
    want = str(d)
    try:
        paths = [str(x) for x in pkg.__path__]      # 兼容 list 和 Chaquopy 的 AssetPath
    except TypeError:
        paths = []
    if want in paths:
        _last["applied"] = current_version()
        return {"ok": True, "applied": _last["applied"], "note": "已挂载"}
    try:
        pkg.__path__ = [want] + paths
    except Exception as e:  # noqa: BLE001 - 只读的 __path__ 就只能回落
        _last["note"] = f"挂不上去：{type(e).__name__}"
        return {"ok": False, "error": _last["note"]}
    _last["applied"] = current_version()
    _last["note"] = "已挂载"
    return {"ok": True, "applied": _last["applied"], "note": "已挂载"}


def _app_version() -> str:
    """当前 APK 的版本号（`garmin_cn.__version__`）。

    代码包是**覆盖式**的：它只该盖在同一版本的 APK 上。APK 升了版本还让旧
    代码包继续压着，等于把新版刚修的东西盖回去——实测过：1.5.1-1 盖在
    1.5.2 上，`apk_fetch` 直接消失，应用内更新报「未知操作」。
    """
    from . import __version__

    return str(__version__)


def _meta_of(dir_: Path) -> dict:
    """读代码包目录里的 meta.json（发布时写进包，解压时落地）。"""
    try:
        return json.loads((dir_ / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _fits(meta: dict) -> tuple[bool, str]:
    """这个代码包能不能装在当前 APK 上。

    没有 `base_version` 的一律拒绝：说不清属于哪个 APK 版本的包，无法保证
    兼容，宁可不装。老服务端/老包会被这条挡掉，但那是正确行为——它们本来
    就是给老 APK 打的。
    """
    want = str(meta.get("base_version") or "").strip()
    cur = _app_version()
    if not want:
        return False, f"代码包没有标注适用版本（当前 App {cur}）"
    if want != cur:
        return False, f"代码包是给 {want} 的，当前 App 是 {cur}"
    return True, ""


def status() -> dict:
    """给诊断日志用：当前跑的是哪份、下载到了哪份、上次检查时间。

    `pending` 单独列出来，是因为「下载好了但还没重启」和「根本没下成」
    在界面上必须分得清——否则用户只会看到一句「没有代码包」，不知道该
    再开一次还是该查网络。
    """
    applied = _last.get("applied") or ""
    base = ""
    if applied:
        # 已挂载那一份标注适用的 APK 版本。诊断里一起显示，
        # 以后「APK 是 X、代码包是给 Y 的」这种事故一眼能看出来。
        d = ROOT / applied / "garmin_cn"
        base = str(_meta_of(d.parent).get("base_version") or "") if d.is_dir() else ""
    return {"applied": applied,
            "base_version": base,
            "pending": _last.get("pending") or "",
            "error": _last.get("error") or "",
            "note": _last.get("note") or "",
            "checked_at": int(_last.get("checked") or 0)}


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(256 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _manifest(force: bool = False) -> dict:
    """问服务器「最新代码包是哪个」。失败返回空字典（等于没有更新）。"""
    now = time.time()
    if not force and _last.get("checked") and now - _last["checked"] < 600:
        try:
            return json.loads(META.read_text(encoding="utf-8"))
        except OSError:
            return {}
    base = _base()
    if not base.startswith("https://"):
        return {}
    try:
        from . import tlstrust
        tlstrust.install()
        req = urllib.request.Request(f"{base}/v1/code/latest",
                                     headers={"User-Agent": "RunnerCoach/code"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:      # noqa: BLE001
        return {}
    _last["checked"] = now
    if not data.get("ok"):
        return {}
    try:
        ROOT.mkdir(parents=True, exist_ok=True)
        META.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return data


def refresh(force: bool = False) -> dict:
    """检查并下载。返回 {ok, changed, version}。"""
    if not ENABLED:
        return {"ok": True, "changed": False, "note": "已禁用"}
    man = _manifest(force)
    if not man:
        _last["note"] = "服务器上没有代码包"
        return {"ok": True, "changed": False, "note": _last["note"]}
    ver = str(man.get("version") or "")
    if not ver or ver == current_version():
        _last["note"] = f"已是最新 {ver}" if ver else "已是最新"
        return {"ok": True, "changed": False, "version": ver}
    # 版本对不上就别下——省流量，也避免下完一个不能挂的包占着「待重启」状态
    ok_fit, why = _fits(man)
    if not ok_fit:
        _last["note"] = why
        _last["error"] = ""
        return {"ok": True, "changed": False, "error": why, "version": ver}
    base = _base()
    tmpdir = ROOT / f"{ver}.tmp"
    try:
        ROOT.mkdir(parents=True, exist_ok=True)
        if tmpdir.exists():
            shutil.rmtree(tmpdir, ignore_errors=True)
        tmpdir.mkdir(parents=True)
        url = f"{base}{man.get('url') or ''}"
        from . import tlstrust
        tlstrust.install()
        buf = io.BytesIO()
        req = urllib.request.Request(url, headers={"User-Agent": "RunnerCoach/code"})
        with urllib.request.urlopen(req, timeout=45) as r:
            while True:
                chunk = r.read(128 * 1024)
                if not chunk:
                    break
                buf.write(chunk)
        data = buf.getvalue()
        if man.get("size") and len(data) != int(man["size"]):
            raise ValueError("大小不对")
        if man.get("sha256") and hashlib.sha256(data).hexdigest() != man["sha256"]:
            raise ValueError("校验不过")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            # 只解压 garmin_cn/ 下的 .py，挡掉 ../ 之类的条目
            for info in z.infolist():
                name = info.filename
                if not name.startswith("garmin_cn/") or not name.endswith(".py"):
                    continue
                if ".." in name or name.startswith("/"):
                    continue
                dest = tmpdir / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(z.read(info))
        # 把发布时那份元数据落到包目录里：apply() 要靠它判断能不能挂。
        (tmpdir / "meta.json").write_text(
            json.dumps({k: man.get(k) for k in
                        ("version", "base_version", "published", "notes", "partial")},
                       ensure_ascii=False), encoding="utf-8")
        pkg = tmpdir / "garmin_cn"
        got = list(pkg.glob("*.py")) if pkg.is_dir() else []
        if not got:
            raise ValueError("包里没有模块")
        if not man.get("partial") and not (pkg / "appapi.py").is_file():
            # 整包缺 appapi.py 说明包不完整：挂上去会让一半模块来自旧代码
            raise ValueError("整包里没有 appapi.py")
        target = ROOT / ver
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        tmpdir.replace(target)
        POINTER.write_text(ver + "\n", encoding="utf-8")
        _last["pending"] = ver
        _last["note"] = f"已下载 {ver}，重启后生效"
        return {"ok": True, "changed": True, "version": ver}
    except Exception as e:      # noqa: BLE001
        shutil.rmtree(tmpdir, ignore_errors=True)
        _last["error"] = f"{type(e).__name__}: {e}"
        _last["note"] = f"下载失败：{type(e).__name__}"
        return {"ok": False, "changed": False, "error": _last["error"]}


def refresh_async() -> None:
    """后台检查/下载。下次启动生效，不在运行时替换代码。"""
    global _busy
    if not ENABLED:
        return
    with _lock:
        if _busy:
            return
        _busy = True

    def _run():
        global _busy
        try:
            refresh()
        finally:
            _busy = False

    threading.Thread(target=_run, name="codepack-refresh", daemon=True).start()
