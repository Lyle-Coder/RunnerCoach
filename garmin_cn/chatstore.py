# -*- coding: utf-8 -*-
"""AI 对话历史：全量落盘 + 滑动窗口 + 摘要归档。

为什么要有这一层
----------------
对话原本只存在页面里的一个 JS 数组，切个 tab 回来就没了——用户看不到自己
问过什么，模型也接不上上一句。这里把历史变成**用户资产**：原文全量保存，
送进模型的只有「摘要 + 最近窗口」。

三个数字的分工（改之前先想清楚）
------------------------------
- `KEEP`（24 条 ≈ 12 轮）：每次真正发给模型的原文条数。窗口越大部分模型越
  爱跑题，而且 token 涨得快。
- `CHUNK`（12 条）：一次归档吞掉多少条。太小会频繁调模型，太大摘要会糊。
- 触发线 = `KEEP + CHUNK`，所以正常节奏是「聊到 36 条归档一次」，不是每轮都算。

原文一条都不删
--------------
归档只产出摘要，`messages` 里的原文继续留着——用户翻记录时要能看全文。
摘要里记 `upto`，表示「前 upto 条已经被摘要覆盖」，`context()` 只从 upto
之后取原文，避免同一段内容既在摘要里又在窗口里被算两遍。

摘要失败不能连累对话
--------------------
模型摘要要花钱、也可能超时。失败就退化成「机械摘录」（把用户问过的问题
串起来），至少不丢线索。摘要是让模型接得上话，不是唯一真相来源。
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import threading
from pathlib import Path

from . import llm

PATH = llm.HOME / "data" / "ai_chat.json"

KEEP = 24            # 发给模型的原文条数（12 轮）
CHUNK = 12           # 一次归档吞掉的条数（6 轮）
DIGEST_MAX = 600     # 摘要字数上限
MAX_FILE = 4000      # 文件里最多留多少条原文，超了从头裁（只影响本地存储）

_LOCK = threading.Lock()

_EMPTY = {"v": 1, "messages": [], "digest": {"text": "", "upto": 0, "at": ""}}


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load() -> dict:
    """读全量历史。文件坏了就当空——不能因为历史文件损坏让 AI 用不了。"""
    try:
        d = json.loads(PATH.read_text(encoding="utf-8"))
    except Exception:
        return json.loads(json.dumps(_EMPTY))
    if not isinstance(d, dict):
        return json.loads(json.dumps(_EMPTY))
    msgs = d.get("messages")
    d["messages"] = [m for m in msgs if isinstance(m, dict)
                     and m.get("role") in ("user", "assistant")
                     and isinstance(m.get("content"), str)] if isinstance(msgs, list) else []
    dg = d.get("digest")
    d["digest"] = dg if isinstance(dg, dict) else {"text": "", "upto": 0, "at": ""}
    d["digest"]["text"] = str(d["digest"].get("text") or "")
    try:
        d["digest"]["upto"] = max(0, min(int(d["digest"].get("upto") or 0),
                                         len(d["messages"])))
    except Exception:
        d["digest"]["upto"] = 0
    return d


def save(d: dict) -> None:
    """原子写：先写临时文件再 replace，断电/杀进程不会留下半个 JSON。"""
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, PATH)


def append(role: str, content: str) -> dict:
    """追加一条并落盘，返回最新全量。空内容直接忽略。"""
    text = (content or "").strip()
    if not text:
        return load()
    with _LOCK:
        d = load()
        d["messages"].append({"role": role, "content": text, "ts": _now()})
        # 本地文件别无限长：从最老的裁，同时把 upto 往前挪同样的量，
        # 否则 upto 会指到不存在的下标上，窗口就错位了。
        over = len(d["messages"]) - MAX_FILE
        if over > 0:
            del d["messages"][:over]
            d["digest"]["upto"] = max(0, d["digest"]["upto"] - over)
        save(d)
        return d


def drop_last_user() -> None:
    """把最后一条用户提问撤掉。模型没答上时用，免得历史里留一个孤立提问。"""
    with _LOCK:
        d = load()
        if d["messages"] and d["messages"][-1]["role"] == "user":
            d["messages"].pop()
            save(d)


def clear() -> None:
    with _LOCK:
        try:
            PATH.unlink()
        except FileNotFoundError:
            pass


def context(kind: str = "full") -> list[dict]:
    """送给模型的上下文：摘要（若有）+ 窗口内原文。

    `kind="digest"` 只给摘要（给「历史归档」展示用，不参与对话）。
    """
    d = load()
    out: list[dict] = []
    dg = d["digest"]
    if dg["text"]:
        out.append({"role": "system",
                    "content": "【更早的对话摘要】" + dg["text"]})
    if kind == "digest":
        return out
    tail = d["messages"][dg["upto"]:][-KEEP:]
    out.extend({"role": m["role"], "content": m["content"]} for m in tail)
    return out


def should_archive() -> bool:
    """未归档的原文是否已经超出窗口。

    触发线就是 `KEEP` 本身，不是 `KEEP + CHUNK`：后一种写法会让窗口外多攒
    12 条既没发给模型、也没进摘要的消息——那 6 轮等于凭空消失。
    """
    d = load()
    return len(d["messages"]) - d["digest"]["upto"] > KEEP


def archive(gen=None) -> dict | None:
    """把超出窗口的最老一批压进摘要。没有可归档的就返回 None。

    `gen(old_digest, chunk) -> str` 是用来生成摘要的注入点（真实实现走模型，
    测试里塞个假的）。为空或抛异常时退化成机械摘录。
    """
    with _LOCK:
        d = load()
        start = d["digest"]["upto"]
        batch = d["messages"][start:start + CHUNK]
        if len(batch) < CHUNK:
            return None
        old = d["digest"]["text"]
        new_text = ""
        if gen:
            try:
                new_text = (gen(old, batch) or "").strip()
            except Exception:
                new_text = ""
        if not new_text:
            new_text = _fallback(old, batch)
        d["digest"] = {"text": new_text[:DIGEST_MAX], "upto": start + CHUNK, "at": _now()}
        save(d)
        return {"upto": d["digest"]["upto"], "text": d["digest"]["text"]}


def _fallback(old: str, batch: list[dict]) -> str:
    """机械摘录：只保留用户问过什么。模型不可用时至少别让线索断掉。"""
    qs = [m["content"].replace("\n", " ")[:40] for m in batch if m["role"] == "user"]
    bits = ([old] if old else []) + (["、".join(qs)] if qs else [])
    return "；".join(bits)[-DIGEST_MAX:]


DIGEST_PROMPT = """把这段跑步教练对话压成一份摘要，供之后接着聊时参考。

只保留会长期有用的东西：
- 用户的训练目标、伤病、限制条件、偏好
- 已经给过的结论和建议（不要重复细节）
- 尚未解决的问题

不要逐句复述，不要编，不要加评论。中文，200 字以内，一段话。"""


def model_gen(old: str, batch: list[dict]) -> str:
    """用当前模型压摘要。调用方负责兜底，这里失败直接抛。"""
    from . import llm

    body = "\n".join(
        f"{'用户' if m['role'] == 'user' else '教练'}：{m['content'][:400]}"
        for m in batch)
    head = f"（之前的摘要：{old}）\n" if old else ""
    r = llm.chat([{"role": "system", "content": DIGEST_PROMPT},
                  {"role": "user", "content": head + body}],
                 temperature=0.2, max_tokens=400)
    return (r.get("text") or "").strip()
