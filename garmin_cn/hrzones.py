# -*- coding: utf-8 -*-
"""佳明 Connect 心率区间（Z1–Z5）的名称：两张一图流的唯一来源。

为什么单独一个模块
------------------
活动图里的五段时长来自佳明活动原始字段 ``hrTimeInZone_1..5``，它是**按用户在
Garmin Connect 里设置的心率区间**算出来的（可能按 %HRmax / %HRR / %LTHR，
也可能改过边界甚至名称）。所以旁边那行中文名也必须跟佳明走，不能另立一套。

此前 ``actcard.ZONE_DESC`` / ``racecard.ZONE_DESC`` 写的是
「恢复 / 有氧 / 节奏 / 阈值 / 无氧」——那是 105K 备赛课表的 E/M/I/T/R
（Daniels）五档训练强度，见 ``workout.py`` 的 ``ZONE_HR``，按乳酸阈心率锚定，
和 Z1–Z5 的边界并不重合。把这套名字贴在佳明分区时长上，就会读出
「Z2 有氧、Z3 节奏」这种和佳明 App 对不上的语义。

佳明官方中文默认名（garmin.com.cn《心率监测》/《如何使用 Garmin 设备按心率
区间进行训练》）：区间 1 热身、2 轻松、3 有氧、4 阈值、5 极限（部分页面写
「最大值」）。注意不是流传较广的「燃脂」那一套。

拿不到用户自定义名称
--------------------
国区 ``/gc-api/biometric-service/biometric/hrZones`` 常年返回 null；本地库里
活动 raw 与详情 payload 实测都只有时长，没有 zoneName / zoneFloor（本会话
再探测时返回 401）。因此无法自动同步用户在 Connect 里改过的名称，留一个显式
覆盖口：``config.json`` 的 ``hr_zone_names``（5 个字符串，按 Z1→Z5 顺序）。
没配就用官方默认名。这个文件只在本机读，不上传、不进日志。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

_env_home = os.environ.get("GARMIN_HOME")
HOME = Path(_env_home).expanduser() if _env_home else Path(__file__).resolve().parent.parent
CONFIG = HOME / "config.json"

ZONE_LABELS = ("Z1", "Z2", "Z3", "Z4", "Z5")
GARMIN_ZONE_NAMES = ("热身", "轻松", "有氧", "阈值", "极限")

# 副名和 Z1 挤在同一行 86px 里，太长会撑破卡片（渲染层另有 ellipsis 兜底）
MAX_LEN = 4
_SAFE = re.compile(r"[^\w\u4e00-\u9fff\u3400-\u4dbf ·/+\-]")


def _cfg() -> dict:
    try:
        d = json.loads(CONFIG.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _clean(value, index: int) -> str:
    """自定义名只留可读字符，避免把 HTML / 控制字符带进一图流。"""
    s = _SAFE.sub("", str(value or "").strip())
    return s[:MAX_LEN] or GARMIN_ZONE_NAMES[index]


def zone_descriptions(cfg: dict | None = None, sport: str = "") -> list[str]:
    """Z1–Z5 的中文副名；cfg=None 时读本机 config.json，读失败回退官方默认。"""
    if cfg is None:
        cfg = _cfg()
    from .sports import profile
    custom = profile(sport, cfg).get("hr_zone_names") if sport else None
    custom = custom if custom is not None else cfg.get("hr_zone_names") if isinstance(cfg, dict) else None
    if not isinstance(custom, (list, tuple)) or len(custom) != 5:
        return list(GARMIN_ZONE_NAMES)
    return [_clean(custom[i], i) for i in range(5)]


def zone_label(index: int, cfg: dict | None = None) -> str:
    """「Z5 极限」这类组合文案，给「Z5 … 占比」等句子用。"""
    i = max(0, min(4, int(index)))
    return f"{ZONE_LABELS[i]} {zone_descriptions(cfg)[i]}"
