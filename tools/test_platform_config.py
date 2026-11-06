# -*- coding: utf-8 -*-
"""平台地址「零配置」回归。

v0.4.5 之前，用户要在设置里手填带 ?sharecode= 的完整分享链接，
填错就静默收不到验证码。现在地址由出厂内置值 + 服务端下发决定，
本测试锁住这几条边界：

1. 全新安装 / 老配置升级后，自动得到可用的官方入口（含分享码）；
2. 服务端下发的 https 地址优先于内置值，脏地址一律拒绝；
3. 用户主动开启「自定义」后完全尊重手填值，关掉又能回到官方服务；
4. 下发接口断网时静默回落，绝不抛异常把登录流程打断；
5. 界面白名单放行 platform_auto，且不回显任何密钥。

用法：py -3 tools/test_platform_config.py   （纯本地，不联网）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_TMP = tempfile.TemporaryDirectory()
os.environ["GARMIN_HOME"] = _TMP.name          # 必须在 import llm 之前设好
sys.path.insert(0, str(ROOT))

from garmin_cn import llm                     # noqa: E402


def write_raw(obj: dict) -> None:
    p = llm.CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def check(name: str, fn) -> None:
    fn()
    print("√ " + name)


def fresh() -> None:
    llm.CONFIG_PATH.unlink(missing_ok=True)
    cfg = llm.load_config()
    assert cfg["platform_auto"] is True
    assert cfg["platform_base"] == llm.BUILTIN_PLATFORM_BASE
    assert cfg["platform_share_code"] == llm.BUILTIN_PLATFORM_SHARE_CODE
    assert llm.public_config()["platform_source"] == "builtin"
    assert llm.is_configured(cfg) is False      # 还没有令牌


def old_config_migrates() -> None:
    """v0.4.4 用户填了裸域名（没拆出分享码）——升级后必须自动修好。"""
    write_raw({"mode": "platform",
               "platform_base": "https://garmin-daily-dashboard.app.workbuddy.host",
               "platform_share_code": ""})
    cfg = llm.load_config()
    assert cfg["platform_share_code"] == llm.BUILTIN_PLATFORM_SHARE_CODE
    assert cfg["platform_base"] == llm.BUILTIN_PLATFORM_BASE

    # 之前填错域名 / 127.0.0.1 的老配置也一样纠正
    write_raw({"mode": "platform", "platform_base": "http://127.0.0.1:8300"})
    cfg = llm.load_config()
    assert cfg["platform_base"] == llm.BUILTIN_PLATFORM_BASE
    assert cfg["platform_share_code"] == llm.BUILTIN_PLATFORM_SHARE_CODE


def served_wins_and_validates() -> None:
    llm.CONFIG_PATH.unlink(missing_ok=True)
    llm.save_config(platform_served_base="https://new.example.com",
                    platform_served_share_code="NEWCODE123")
    cfg = llm.load_config()
    assert cfg["platform_base"] == "https://new.example.com"
    assert cfg["platform_share_code"] == "NEWCODE123"
    assert llm.public_config()["platform_source"] == "server"

    good = ["https://a.example.com", "https://a.example.com/base", "https://a.example.com:8443"]
    bad = ["http://a.example.com", "https://a.example.com/?sharecode=X",
           "https://a.example.com/#x", "https://a example.com", "", "ftp://a.example.com"]
    assert all(llm._valid_public_base(u) for u in good), good
    assert not any(llm._valid_public_base(u) for u in bad), bad


def custom_mode_respected() -> None:
    llm.CONFIG_PATH.unlink(missing_ok=True)
    llm.save_config(platform_auto=False,
                    platform_base="http://192.168.1.5:8300/?sharecode=ABC12345")
    cfg = llm.load_config()
    assert cfg["platform_base"] == "http://192.168.1.5:8300"
    assert cfg["platform_share_code"] == "ABC12345"
    assert llm.public_config()["platform_source"] == "custom"

    # 关掉自定义：回到官方服务（此时没有下发值）
    llm.save_config(platform_auto=True)
    cfg = llm.load_config()
    assert cfg["platform_base"] == llm.BUILTIN_PLATFORM_BASE
    assert cfg["platform_share_code"] == llm.BUILTIN_PLATFORM_SHARE_CODE


class _FakeResp:
    def __init__(self, payload: dict):
        self._b = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


def sync_offline_is_silent() -> None:
    llm.CONFIG_PATH.unlink(missing_ok=True)
    orig = llm.urllib.request.urlopen

    def boom(*a, **kw):
        raise OSError("offline")

    llm.urllib.request.urlopen = boom
    try:
        cfg = llm.sync_platform_config(force=True)
    finally:
        llm.urllib.request.urlopen = orig
    assert cfg["platform_base"] == llm.BUILTIN_PLATFORM_BASE
    assert cfg["platform_sync_at"] > 0        # 记下尝试时间，避免每次都白等超时


def sync_honors_server_and_rejects_dirty() -> None:
    llm.CONFIG_PATH.unlink(missing_ok=True)
    orig = llm.urllib.request.urlopen
    seen = []

    def fake(req, timeout=None):
        seen.append(req)
        return _FakeResp({"ok": True,
                          "platform_base": "https://srv.example.com",
                          "platform_share_code": "SRVCODE99"})

    llm.urllib.request.urlopen = fake
    try:
        cfg = llm.sync_platform_config(force=True)
    finally:
        llm.urllib.request.urlopen = orig
    assert seen and seen[0].get_header("X-share-code") == llm.BUILTIN_PLATFORM_SHARE_CODE
    assert cfg["platform_base"] == "https://srv.example.com"
    assert cfg["platform_share_code"] == "SRVCODE99"

    # 脏地址：清空下发值，回落内置
    llm.urllib.request.urlopen = lambda req, timeout=None: _FakeResp(
        {"ok": True, "platform_base": "http://evil.example.com",
         "platform_share_code": "BAD"})
    try:
        cfg = llm.sync_platform_config(force=True)
    finally:
        llm.urllib.request.urlopen = orig
    assert cfg["platform_base"] == llm.BUILTIN_PLATFORM_BASE
    assert cfg["platform_share_code"] == llm.BUILTIN_PLATFORM_SHARE_CODE


def public_config_hides_secrets() -> None:
    llm.CONFIG_PATH.unlink(missing_ok=True)
    llm.save_config(api_key="sk-super-secret-value", mode="platform",
                    platform_token="tok-abcdefgh")
    pub = llm.public_config()
    assert "sk-super-secret-value" not in json.dumps(pub, ensure_ascii=False)
    assert "tok-abcdefgh" not in json.dumps(pub, ensure_ascii=False)
    assert pub["has_platform_token"] is True
    assert pub["platform_auto"] is True


def aiapi_whitelist() -> None:
    from garmin_cn import aiapi
    assert "platform_auto" in aiapi._WRITABLE
    assert "platform_served_base" not in aiapi._WRITABLE   # 内部字段不许界面改
    r = aiapi.dispatch("config.set", {"platform_auto": "false",
                                      "platform_base": "http://10.0.0.2:8300",
                                      "platform_served_base": "https://evil.example.com"})
    assert r["ok"] is True
    assert r["platform_auto"] is False
    assert r["platform_base"] == "http://10.0.0.2:8300"
    assert llm.load_config()["platform_served_base"] == ""


def main() -> int:
    try:
        check("全新安装：出厂内置官方入口与分享码", fresh)
        check("老配置升级：填错的裸域名 / 127.0.0.1 自动纠正", old_config_migrates)
        check("服务端下发优先，且只接受合法 https 地址", served_wins_and_validates)
        check("自定义地址完全生效，关掉即回官方服务", custom_mode_respected)
        check("下发接口断网静默回落，不打断登录", sync_offline_is_silent)
        check("下发带引导凭证、脏配置不采用", sync_honors_server_and_rejects_dirty)
        check("public_config 不回显 key / 令牌", public_config_hides_secrets)
        check("config.set 白名单与 platform_auto 布尔转换", aiapi_whitelist)
    except Exception as e:
        print("× " + str(e), file=sys.stderr)
        return 1
    finally:
        _TMP.cleanup()
    print("√ 平台地址零配置回归全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
