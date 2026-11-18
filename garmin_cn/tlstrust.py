# -*- coding: utf-8 -*-
"""把内置的自签证书加进 HTTPS 信任链。

为什么需要
----------
无域名部署用的是「自签证书 + IP SAN」。客户端要做两件事才连得上：

1. 地址是 `https://1.2.3.4:8443`，证书的 subjectAltName 里必须有这个 IP
   ——校验的是「证书上的名字」和「你连的地址」是否一致；
2. 签发者得是客户端认识的。自签证书不在系统根证书里，所以要把它**当根**
   装进来，否则一律 `CERTIFICATE_VERIFY_FAILED`。

做不到第 2 点，很多人第一反应是「关掉校验」（`ssl._create_unverified_context`），
那等于把令牌明文交给同网络的任何人，比用 HTTP 还糟。所以这里只做加法：
在默认信任链上**多认这一张**，其它主机照旧走系统根证书。

放在哪
------
`garmin_cn/content/server-ca.pem`（由 `tools/set_server.py` 从服务器端拷来）。
没有这个文件就什么都不做——用公共 CA 域名部署时不需要它。
"""

from __future__ import annotations

from pathlib import Path

CA_PATH = Path(__file__).resolve().parent / "content" / "server-ca.pem"


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


def _resolve() -> Path:
    """取实际的证书路径。

    代码包走的是覆盖式更新：`tlstrust.py` 自己也可能来自下载目录，那时
    `__file__` 指向 `HOME/code/<ver>/garmin_cn/`，那里**没有** content/。
    找不到就是「没有内置证书」，所有 HTTPS 直接 CERTIFICATE_VERIFY_FAILED
    ——看起来像「服务器挂了」，实际只是路径算错。

    所以先看 CA_PATH（兼容测试里 monkeypatch 成临时文件），不存在再去包里找。
    """
    if CA_PATH.is_file():
        return CA_PATH
    p = _pkg_file("content/server-ca.pem")
    return p if p is not None else CA_PATH

_done: dict | None = None
_orig = None      # 被替换之前的 ssl.create_default_context，reset() 用


def install() -> dict:
    """幂等。返回 {ok, path/reason}，不抛异常——连不上网时也不该让 App 崩。"""
    global _done
    if _done is not None:
        return _done
    ca = _resolve()
    if not ca.is_file():
        _done = {"ok": False, "reason": "没有内置 server-ca.pem（用公共 CA 时正常）"}
        return _done

    import ssl

    global _orig
    orig = ssl.create_default_context
    _orig = orig

    def _ctx(*args, **kwargs):
        ctx = orig(*args, **kwargs)
        try:
            ctx.load_verify_locations(cafile=str(ca))
        except Exception:  # noqa: BLE001 - 证书坏了也不能让整个 App 起不来
            pass
        return ctx

    # urllib 走的是 `ssl.create_default_context`，但 http.client 会先取
    # `_create_default_https_context` 这个模块属性，两个都换掉才稳。
    ssl.create_default_context = _ctx          # type: ignore[assignment]
    ssl._create_default_https_context = _ctx   # type: ignore[attr-defined]

    _done = {"ok": True, "path": str(ca)}
    return _done


def reset() -> None:
    """测试用：撤掉接管，把状态清掉，让下一次 install() 重新执行。

    一定要真的还原：测试跑完之后整个进程还在用 ssl，留着一个指向已删除
    证书文件的上下文，后面每一次 HTTPS 都会白试一遍。
    """
    global _done, _orig
    if _orig is not None:
        import ssl

        ssl.create_default_context = _orig        # type: ignore[assignment]
        ssl._create_default_https_context = _orig  # type: ignore[attr-defined]
        _orig = None
    _done = None
