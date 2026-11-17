# -*- coding: utf-8 -*-
"""内容守卫：受保护内容不许跟着安装包走。

有些内容按设计只在服务端维护、运行时下发，不进任何分发包——一旦进包，
安装包本身就是一份可解压的副本，等于公开。

所以定一条硬规矩：

    客户端只有「怎么演」，没有「演什么」。
    受保护内容全部运行时获取，本地只留当次缓存。

这条规矩靠人记是记不住的：只要有人把数据文件放进 `garmin_cn/`，
下次打包就带出去了，而且没人会察觉。所以用代码守：构建前扫一遍，
发现就失败。

判定方式刻意收得很窄，只认「这一定是受保护内容」的特征，避免误报把正常
开发卡住：

* 文件名带 `expedition` / `act1`~`act9` / `story_events`；
* JSON 里出现 `result_brief`（该类数据独有字段，普通数据没有）；
* JSON 同时有 `options` 与 `advance`（选项 + 推进格数）。

赛事资料（`data/events/<slug>/event.json`）不算：那是用户自己的比赛信息，
本来就在手机上。这里只拦服务端下发的运营内容。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# 名字就像运营数据文件：出现即拦，不看内容
NAME_PATTERN = re.compile(
    r"(expedition|story_events|season_events|act[-_]?[1-9]\b)", re.IGNORECASE)

# 该类数据独有的字段
STORY_KEYS = ("result_brief",)

# 事件池的组合特征：有选项、有推进格数
COMBO_KEYS = ("options", "advance")

# 扫这些目录（相对项目根）
DEFAULT_DIRS = (
    "garmin_cn",
    "android/app/src/main/python",
    "android/app/src/main/assets",
)

# 这些目录里可以有受保护内容：服务端下发源、设计稿、预览图
ALLOWED_DIRS = ("server", "preview", "docs", "tests")


def _iter_files(root: Path, dirs=DEFAULT_DIRS):
    for rel in dirs:
        base = root / rel
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            if "__pycache__" in p.parts:
                continue
            if any(part in ALLOWED_DIRS for part in p.relative_to(root).parts):
                continue
            if p.suffix.lower() not in (".json", ".py", ".html", ".htm", ".js"):
                continue
            yield p


def _json_hit(p: Path) -> str | None:
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    if '"result_brief"' in text:
        return "含受保护字段 result_brief"
    if all(f'"{k}"' in text for k in COMBO_KEYS):
        return "同时含 options 与 advance（事件选项 + 推进格数）"
    return None


def _text_hit(p: Path) -> str | None:
    """非 JSON：只看是不是把正文硬编码进了代码或页面。"""
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    if "result_brief" in text:
        return "代码里出现 result_brief"
    # 事件正文的写法特征：三个以上选项 + 推进格数一起硬编码
    if text.count('"advance"') >= 3 and text.count('"brief"') >= 3:
        return "代码里硬编码了多条事件选项"
    return None


def scan(root: Path | None = None, dirs=DEFAULT_DIRS) -> list[dict]:
    """返回问题清单；空列表 = 干净。"""
    root = Path(root) if root else Path(__file__).resolve().parent.parent
    issues = []
    for p in _iter_files(root, dirs):
        # 这两个文件不拦：contentguard.py 自己就写着判定特征词，扫到自己必然误报；
        # selftest.py 不会进包（`sync_python` 打包前会删掉），留它的测试数据无妨。
        if p.name in ("contentguard.py", "selftest.py"):
            continue
        rel = str(p.relative_to(root)).replace("\\", "/")
        if NAME_PATTERN.search(p.stem):
            issues.append({"file": rel, "why": "文件名像受保护数据"})
            continue
        why = _json_hit(p) if p.suffix.lower() == ".json" else _text_hit(p)
        if why:
            issues.append({"file": rel, "why": why})
    return issues


def check(root: Path | None = None) -> dict:
    issues = scan(root)
    return {"ok": not issues, "issues": issues,
            "hint": "" if not issues else
                    "受保护内容只放服务端，客户端运行时拉取。"
                    "确认误报请在 contentguard.py 里收窄规则，不要直接放行。"}


def manifest(root: Path | None = None) -> dict:
    """给发布流程用：打一行结论，别让人去数文件。"""
    root = Path(root) if root else Path(__file__).resolve().parent.parent
    r = check(root)
    n = sum(1 for _ in _iter_files(root))
    return {"ok": r["ok"], "scanned": n, "issues": r["issues"], "hint": r["hint"]}
