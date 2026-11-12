# -*- coding: utf-8 -*-
"""姿态评估的历史：当前这一轮的结果 + 每次评估的记录。

为什么要有这一层
----------------
判读结果原本只活在前端 `RES` 对象里，切出页面（WebView 重建）就没了，
用户拍的几张、算出来的方案全丢。和 AI 对话是同一类问题：结果属于用户，
不该存在易失的地方。

两层结构
--------
- `current`：每个动作最近一份判读，按拍摄顺序保序。页面重开时直接恢复，
  汇总方案能立刻重算出来。
- `history`：每次评估追加一条（时间 + 动作 + 结果），上限 `MAX_HISTORY` 条。
  用来回答「我上个月拍的是什么样」——只留 current 是回答不了的。

为什么同一个动作重拍是替换而不是并存
------------------------------------
`current` 按动作存，重拍就是认为新的这张更准。并存会让「当前方案」说不清
到底以哪张为准。但 history 里每次都有记录，想回头看旧的不受影响。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading

from . import llm

PATH = llm.HOME / "data" / "posture.json"
MAX_HISTORY = 60

_LOCK = threading.Lock()
_EMPTY = {"v": 1, "current": {}, "history": []}


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def load() -> dict:
    """读历史。文件坏了就当空——不能因为一个坏 JSON 让整页用不了。"""
    try:
        d = json.loads(PATH.read_text(encoding="utf-8"))
    except Exception:
        return json.loads(json.dumps(_EMPTY))
    if not isinstance(d, dict):
        return json.loads(json.dumps(_EMPTY))
    cur = d.get("current")
    d["current"] = cur if isinstance(cur, dict) else {}
    hist = d.get("history")
    d["history"] = [h for h in hist if isinstance(h, dict)] if isinstance(hist, list) else []
    return d


def save(d: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, PATH)


def save_result(move_id: str, result: dict) -> dict:
    """记一次判读：更新 current 里那一格，同时往 history 追加一条。"""
    with _LOCK:
        d = load()
        cur = d["current"]
        # 重拍要保留原来的顺序（dict 保序），否则页面上顺序会跳来跳去
        cur[move_id] = result
        d["history"].insert(0, {
            "at": _now(), "move": move_id,
            "name": result.get("name") or "", "result": result,
        })
        d["history"] = d["history"][:MAX_HISTORY]
        save(d)
        return d


def current() -> list[dict]:
    """当前这一轮的判读，按拍摄顺序。"""
    return [v for v in load()["current"].values() if isinstance(v, dict)]


def clear() -> None:
    with _LOCK:
        try:
            PATH.unlink()
        except FileNotFoundError:
            pass
