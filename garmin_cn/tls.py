"""进程内共享的 SSLContext。

`urllib` 默认每个 opener 各自 `ssl.create_default_context()`，而这在 Windows
上要枚举系统证书库（本机 500+ 张），单次约 34ms。`_client()` 一次会建两个
`Net`（auth 自己的 + 返回的），再加上小米云 / 高驰各自的 opener，光建 TLS
上下文的开销就够把首页拖到 60ms 以上。

context 与 Cookie、代理都不相干（那些在各实例自己的 handler 里），所以整个
进程共用一个即可。首次创建之后是引用传递，几乎零成本。
"""
from __future__ import annotations

import ssl

_CTX: ssl.SSLContext | None = None


def ssl_context() -> ssl.SSLContext:
    """返回进程级共享的 SSLContext（首次调用时创建）。"""
    global _CTX
    if _CTX is None:
        _CTX = ssl.create_default_context()
    return _CTX


def https_handler() -> "object":
    """给 `build_opener()` 用的 HTTPSHandler，复用共享 context。"""
    import urllib.request

    return urllib.request.HTTPSHandler(context=ssl_context())
