# -*- coding: utf-8 -*-
"""用系统默认程序打开文件或网址，跨平台。

Windows 用 `os.startfile`；Termux 用 `termux-open`（走 Android Intent，
网页丢给 Chrome、PNG 丢给相册）；Linux 用 `xdg-open`；macOS 用 `open`。

为什么单独一层：`os.startfile` 在 POSIX 上根本不存在，直接调用会
AttributeError——而 `--open` 散在 report / card / act / brief / demo 好几处，
每处都写 if 判断会重复五遍。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys


def open_any(target) -> bool:
    """打开成功返回 True；找不到任何打开方式时只提示，不抛异常。"""
    p = str(target)

    if hasattr(os, "startfile"):
        try:
            os.startfile(p)  # type: ignore[attr-defined]
            return True
        except OSError:
            pass

    for cmd in (["termux-open", p], ["xdg-open", p], ["open", p]):
        if not shutil.which(cmd[0]):
            continue
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        except OSError:
            continue

    print(f"  这台机器没有可用的打开方式，自己复制这个路径：{p}")
    return False


def is_termux() -> bool:
    """判断是不是在 Termux（Android）里跑。"""
    if os.environ.get("TERMUX_VERSION"):
        return True
    return "com.termux" in str(getattr(sys, "prefix", ""))
