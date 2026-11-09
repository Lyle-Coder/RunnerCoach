# -*- coding: utf-8 -*-
"""异常留档：出问题时用户能把「刚才发生了什么」直接发出来。

为什么不用 logging
------------------
只需要最近几百行、固定格式、能一键复制。logging 的 handler/Formatter
在手机上既没法看也没法发，反而多一层配置。
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

# 超过这个大小就只留后半段：日志是排障用的，不是账本，不能无限长。
MAX_BYTES = 128 * 1024
KEEP_LINES = 400


def log_path() -> Path:
    """与 appapi 的 HOME 规则保持一致：GARMIN_HOME 优先，否则项目根。"""
    env = os.environ.get("GARMIN_HOME")
    root = Path(env).expanduser() if env else Path(__file__).resolve().parent.parent
    return root / "data" / "diag.log"


def log_exc(scope: str, exc: BaseException | str) -> None:
    """记一条。写不进去也绝不抛——日志不该把主流程拖垮。"""
    p = log_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.is_file() and p.stat().st_size > MAX_BYTES:
            tail = p.read_text("utf-8", errors="replace").splitlines()[-KEEP_LINES:]
            p.write_text("\n".join(tail) + "\n", encoding="utf-8")
        if isinstance(exc, BaseException):
            detail = f"{type(exc).__name__}: {exc}"
        else:
            detail = str(exc)
        # 一行一条：多行异常在手机弹窗里会撑成一屏，摘要就够定位
        detail = " ".join(detail.split())[:500]
        ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{ts} [{scope}] {detail}\n")
    except OSError:
        pass


def read_text(lines: int = 200) -> str:
    p = log_path()
    if not p.is_file():
        return ""
    try:
        data = p.read_text("utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(data[-max(1, int(lines)):])
