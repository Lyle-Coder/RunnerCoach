"""应用内检查更新：只读 Gitee 的公开 Release 接口。

为什么走 Gitee
--------------
App 没有应用商店渠道，安装包只发在 Gitee Release（`liyufei-lyle/RunnerCoach`）。
Gitee 的 `/releases/latest` 可以匿名读、国内直连、不涉及账号，于是「有没有
新版本、新版本在哪下载」这两件事不需要自建后端，也不需要推送通道。

隐私边界
--------
请求只带 User-Agent（版本号），**不上报任何用户数据、不做设备指纹、不驻留
长连接**；每次启动最多查一次（App 侧 24 小时节流）。断网、超时、接口改版
一律静默失败，不影响任何功能——检查更新是锦上添花，不能拖慢启动。

纯标准库，零依赖；`parse()` 与 `is_newer()` 不联网，便于离线自检。
"""
from __future__ import annotations

import json
import re
import urllib.request

REPO = "liyufei-lyle/RunnerCoach"
API_LATEST = f"https://gitee.com/api/v5/repos/{REPO}/releases/latest"
PAGE = f"https://gitee.com/{REPO}/releases"
TIMEOUT = 8
# Release 说明太长会把弹窗撑爆；只取前这么多字符（保留换行，App 侧滚动显示）
NOTES_MAX = 900


def version_tuple(text: str | None) -> tuple[int, ...]:
    """'v1.0.10' → (1, 0, 10)。取不到数字的部分忽略，便于 tag 带前后缀。"""
    nums = re.findall(r"\d+", str(text or ""))
    return tuple(int(n) for n in nums[:4]) if nums else ()


def is_newer(latest: str | None, current: str | None) -> bool:
    """latest 是否严格新于 current（按数字段比较，缺位补 0）。"""
    a, b = version_tuple(latest), version_tuple(current)
    if not a or not b:
        return False
    n = max(len(a), len(b))
    a += (0,) * (n - len(a))
    b += (0,) * (n - len(b))
    return a > b


def pick_apk(assets: list | None) -> dict | None:
    """从 Release 附件里挑安装包：优先 release，绝不给 debug（包名同但签名不同）。"""
    cands = []
    for a in assets or []:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "")
        url = a.get("browser_download_url") or a.get("download_url")
        if not url or not name.lower().endswith(".apk"):
            continue
        if "debug" in name.lower():
            continue
        cands.append({"name": name, "url": str(url),
                      "release": "release" in name.lower()})
    if not cands:
        return None
    cands.sort(key=lambda c: not c["release"])   # release 排前面
    return cands[0]


def parse(payload: dict | None, current: str | None) -> dict:
    """Release JSON → 检查结果。纯函数，不联网。"""
    d = payload if isinstance(payload, dict) else {}
    tag = str(d.get("tag_name") or d.get("name") or "").strip()
    apk = pick_apk(d.get("assets"))
    notes = str(d.get("body") or "").strip()
    if len(notes) > NOTES_MAX:
        notes = notes[:NOTES_MAX].rstrip() + "…"
    url = apk["url"] if apk else (f"{PAGE}/tag/{tag}" if tag else PAGE)
    return {
        "ok": bool(tag),
        "current": str(current or ""),
        "latest": tag,
        "newer": bool(tag) and is_newer(tag, current),
        "url": url,
        "page": f"{PAGE}/tag/{tag}" if tag else PAGE,
        "apk": apk["name"] if apk else "",
        "notes": notes,
        "published": str(d.get("created_at") or ""),
        "prerelease": bool(d.get("prerelease")),
    }


def check(current: str | None = None, timeout: float = TIMEOUT) -> dict:
    """联网查最新 Release。任何异常都返回 ok=False，调用方按「不提示」处理。"""
    from . import __version__

    cur = current or __version__
    req = urllib.request.Request(
        f"{API_LATEST}?t={int(__import__('time').time())}",   # 绕过中间缓存
        headers={"User-Agent": f"RunnerCoach/{cur}",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "ignore")
        payload = json.loads(raw)
    except Exception as e:      # 断网 / 超时 / 404（还没有 Release）
        return {"ok": False, "current": cur, "newer": False,
                "error": f"{type(e).__name__}: {e}"[:160]}
    out = parse(payload, cur)
    out.setdefault("current", cur)
    return out
