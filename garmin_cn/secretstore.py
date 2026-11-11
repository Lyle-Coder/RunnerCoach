# -*- coding: utf-8 -*-
"""本机凭据的对称加密：Windows 走 DPAPI，非 Windows 明确报告不可用。

为什么不用第三方加密库
----------------------
项目零第三方依赖。Windows 的 DPAPI（`CryptProtectData`）由系统托管密钥，
密文绑定当前用户账户：复制到别的机器、换个用户都解不开，正好用来存 Keep
这种「只能账密换短效 token、过期必须重登」的凭据。

非 Windows 上不假装加密：`available()` 返回 False，调用方退化为「只存 token，
过期要求重新登录」。安卓的密码由 Kotlin Keystore 管理，本来不需要这里。
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import sys

CRYPTPROTECT_UI_FORBIDDEN = 0x01


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


if sys.platform == "win32":  # pragma: no cover - 平台分支
    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _crypt32.CryptProtectData.restype = wt.BOOL
    _crypt32.CryptUnprotectData.restype = wt.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
else:  # pragma: no cover
    _crypt32 = None
    _kernel32 = None


def available() -> bool:
    """当前平台有没有可用的系统级加密。"""
    return _crypt32 is not None


def _call(fn, data: bytes) -> bytes:
    if fn is None:
        raise OSError("当前平台没有可用的系统加密（DPAPI）")
    if not data:
        raise ValueError("不能加密空内容")
    buf = ctypes.create_string_buffer(data, len(data))
    src = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _BLOB()
    ok = fn(ctypes.byref(src), None, None, None, None,
            CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise OSError(ctypes.get_last_error(), "DPAPI 调用失败")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def protect(text: str) -> str:
    """明文 → base64 密文。同一个用户下可解密，换机器/换用户解不开。"""
    return base64.b64encode(_call(_crypt32.CryptProtectData,
                                  text.encode("utf-8"))).decode("ascii")


def unprotect(token: str) -> str:
    """base64 密文 → 明文；密文损坏或换了账户会抛 OSError。"""
    raw = _call(_crypt32.CryptUnprotectData, base64.b64decode(token))
    return raw.decode("utf-8")
