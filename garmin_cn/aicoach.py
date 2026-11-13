# -*- coding: utf-8 -*-
"""AI 教练的业务层：今日建议、体重复盘、单次活动解读、自由对话。

和规则引擎的分工
----------------
`coach.py` 的规则引擎负责「今天该练什么」——这是可复现、可审计的处方，
模型不参与决策，只负责**把理由讲清楚**。这条边界很重要：
模型算不出恢复度，也不该决定训练量；它能做的是把已有数字翻译成人话，
指出数据之间的矛盾（比如恢复度 82 却连续三晚睡不到 6 小时）。

为什么都要结构化输出
--------------------
自由文本在手机上没法排版：一段 500 字的话读起来像罚抄。所以三个场景都要求
模型返回固定字段的 JSON，前端按字段渲染成「结论 → 做什么 → 别做什么 → 盯什么」。
解析失败时有兜底：把原文塞进 `summary`，界面照样能看，不会白花钱。

缓存
----
今日建议按 (日期, 上下文指纹) 缓存。同一天反复打开看板不重复调用——
一次几百 token，白花钱还慢。上下文变了（同步到新活动）指纹就变，自动失效。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import threading
import time
from pathlib import Path

from . import llm
from .coach import brief, context_markdown, event_brief
from .metrics import derive, num

ROOT = llm.HOME
CACHE_DIR = ROOT / "data" / "ai"

# 系统提示词：人设 + 数据纪律。写死在这里，业务层不各写一份，
# 免得三个场景的教练口径互相打架（一个说加练、一个说休息）。
SYSTEM = """你是 RunnerCoach 的私人耐力运动教练，支持跑步、骑行与交叉训练。
训练目标、主要运动和赛事只依据本次上下文；不要默认用户备战 105K。
骑行用 km/h、踏频 rpm 和功率 W，跑步用配速和步频 spm；不要混淆。

工作原则：
1. **只依据给定数据说话**。数字必须来自上下文；没给的就说"数据里没有"，绝不编造。
2. **结论先行**。第一句就给判断，不要铺垫、不要复述数据。
3. **具体可执行**。写"今天 40 分钟轻松跑，心率压在 140 以内"，不写"注意适度训练"。
4. **尊重规则引擎的处方**。上下文里的"今日处方"是定稿，你的任务是解释它、提醒风险，
   不是推翻它。若数据明显矛盾（如处方强度跑但恢复度 30），指出来并建议降级执行。
5. **第二人称**，像教练当面说话。不用"您"，不用敬语堆砌，不喊口号。
6. 中文，短句。不用 markdown 记号（**、#、-），这些字段会被渲染成卡片。
7. 拿不准就说不确定，不要为了显得专业而编造因果关系。
8. 上下文出现「备赛赛事」时，训练建议要与它对齐：余量为负先提醒风险，
   距比赛 3 周内不加量；不要在比赛中期建议"再上一次强度课"。"""


# ---------------------------------------------------------------- 工具
def _parse_json(text: str) -> dict:
    """从模型输出里抠 JSON。容忍 ```json 围栏、前后废话、单引号。"""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        chunk = t[i:j + 1]
        try:
            obj = json.loads(chunk)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            # 尾逗号是模型最常见的笔误，清一遍再试
            try:
                obj = json.loads(re.sub(r",\s*([}\]])", r"\1", chunk))
                return obj if isinstance(obj, dict) else {}
            except Exception:
                pass
    return {}


# 有的模型（deepseek 系）会把思考过程写进正文再来 JSON，一旦被 max_tokens
# 截断就永远解析不出对象——界面上就出现「中英混排的内心独白」。统一在提示词
# 里禁掉，解析失败再补一次「只做格式转换」的请求，比让用户点重新生成可控。
_JSON_RULES = ("\n\n输出要求（必须遵守）：只输出 JSON 对象本身。不要写思考过程、"
               "不要解释、不要英文、不要 markdown 围栏；第一个字符必须是 {。")

# 格式转换用的字段清单（第二次调用时告诉模型要哪些键，免得转出一堆自创字段）
SCHEMA_DAILY = ('{"headline":"18字以内","focus":"25字以内","dos":["2-4条"],'
                '"donts":["1-3条"],"watch":"30字以内","why":"80-140字"}')
SCHEMA_WEIGHT = ('{"summary":"25字以内","trend":"down|up|flat","rate_verdict":"40字以内",'
                 '"actions":["2-4条"],"cautions":["1-3条"],"nutrition":"50字以内"}')
SCHEMA_ACT = ('{"title":"20字以内","summary":"80字以内","good":["1-3条"],'
              '"improve":["1-3条"],"next":"60字以内"}')


def _json_chat(messages: list[dict], *, schema: str = "", max_tokens: int = 1600,
               temperature: float = 0.4) -> tuple[dict, dict]:
    """要求 JSON；拿不到就再要一次（第二次只做格式转换，不重新分析）。"""
    r = llm.chat(messages, max_tokens=max_tokens, json_mode=True,
                 temperature=temperature)
    obj = _parse_json(r.get("text") or "")
    if obj:
        return obj, r
    fix = [
        {"role": "system",
         "content": "你是格式转换器。把用户给的内容改写成合法 JSON 对象，"
                    "只输出 JSON，不要解释、不要英文、不要 markdown。"},
        {"role": "user",
         "content": f"JSON 字段要求：\n{schema}\n\n待转换内容：\n"
                    f"{(r.get('text') or '')[:3000]}"},
    ]
    try:
        r2 = llm.chat(fix, max_tokens=max_tokens, json_mode=True, temperature=0)
    except llm.LLMError:
        return {}, r
    return _parse_json(r2.get("text") or ""), r2


def _slist(v, limit: int = 6) -> list[str]:
    """把模型给的「列表」洗成干净的字符串数组。

    模型有时返回 ["a","b"]，有时返回 "1. a 2. b"，有时返回 [{"text":"a"}]。
    界面只认字符串数组，所以这里统一。
    """
    out: list[str] = []
    if isinstance(v, str):
        parts = re.split(r"[\n；;]+|(?<=[。！？])\s*|\d+[.、)]\s*", v)
        out = [p.strip(" ·-—") for p in parts if p and p.strip(" ·-—")]
    elif isinstance(v, list):
        for x in v:
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
            elif isinstance(x, dict):
                s = x.get("text") or x.get("content") or x.get("title")
                if s:
                    out.append(str(s).strip())
    return [s for s in out if s][:limit]


def _ctx_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# 点评的提示词口径一变就加这个号：指纹里带上它，旧缓存会自然失效。
# 之前提示词写得太像「正文」，模型把「写 90 到 140 字、引用具体数字、不复述处方」
# 当成点评内容回显，看板上直接把写作要求摊给用户看了。
NOTE_PROMPT_VER = 2


def _note_fp(ctx: str) -> str:
    return _ctx_fingerprint(f"note-v{NOTE_PROMPT_VER}|{ctx}")


def _cache_path(kind: str, date: str) -> Path:
    return CACHE_DIR / f"{kind}-{date}.json"


def _cache_read(kind: str, date: str, fp: str) -> dict | None:
    p = _cache_path(kind, date)
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if obj.get("fp") != fp:
        return None
    return obj


def _cache_write(kind: str, date: str, fp: str, data: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = dict(data)
        payload["fp"] = fp
        payload["at"] = dt.datetime.now().strftime("%m-%d %H:%M")
        _cache_path(kind, date).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _store():
    from .store import Store

    return Store(ROOT / "data" / "garmin.db")


# ---------------------------------------------------------------- 今日建议
def daily_advice(date: str | None = None, force: bool = False) -> dict:
    """今日建议：把规则处方 + 恢复度 + 近期负荷交给模型，产出一张可读的卡片。

    返回字段固定，前端不猜：headline / focus / dos / donts / watch / why。
    """
    d = date or dt.date.today().isoformat()
    try:
        store = _store()
        res = derive(store, d)
    except Exception as e:
        return {"ok": False, "kind": "data", "error": f"读不到今天的数据：{e}"}

    ctx = context_markdown(res)
    fp = _ctx_fingerprint(ctx)
    if not force:
        hit = _cache_read("daily", d, fp)
        if hit:
            hit["cached"] = True
            return hit

    b = brief(res)
    sess = b.get("session") or {}
    instruction = f"""以下是运动者今天的数据与规则引擎给出的处方。

{ctx}

请输出一个 JSON 对象，字段如下（全部中文字符串，不要 markdown）：
{{
  "headline": "一句话结论，18 字以内，说清今天该干什么",
  "focus": "今天最该盯的一点，25 字以内",
  "dos": ["2 到 4 条今天要做的事，每条 24 字以内，必须具体到时长/心率/配速/时间点"],
  "donts": ["1 到 3 条今天不要做的事，每条 24 字以内"],
  "watch": "一句提醒：最需要留意的风险信号，30 字以内",
  "why": "为什么这么安排，引用上下文里的具体数字，80 到 140 字"
}}

注意：
- 处方类型是「{sess.get('type') or '未知'}」，强度「{sess.get('intensity') or '未知'}」，
  时长「{sess.get('duration') or '未知'}」。dos 第一条要围绕它展开，不要另外发明训练。
- 如果上下文标了休息日，dos 就写恢复相关的事（睡眠、拉伸、补给），不要塞训练。
- 如果风险标记里有 alert/warn，watch 必须对应到其中最严重的一条。"""

    try:
        obj, r = _json_chat([{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": instruction + _JSON_RULES}],
                            schema=SCHEMA_DAILY, max_tokens=1600, temperature=0.5)
    except llm.LLMError as e:
        return e.as_dict()

    if not obj:
        # 解析不出来也要缓存：否则每次切 tab 都会重新请求，白花钱还慢。
        # 真正的「内心独白」不往卡片里塞，用户要看就点重新生成。
        out = {"ok": True, "date": d, "headline": "今日建议",
               "focus": "这次没拿到结构化建议", "dos": [], "donts": [], "watch": "",
               "why": "模型这次没按格式返回（多数是思考过程太长被截断）。"
                      "点下面的「重新生成」再试一次；反复这样就在「模型服务」里换个模型。",
               "fail": True, "model": r.get("model"), "elapsed": r.get("elapsed")}
        _cache_write("daily", d, fp, out)
        out["cached"] = False
        return out

    out = {"ok": True, "date": d,
           "headline": str(obj.get("headline") or "今日建议")[:40],
           "focus": str(obj.get("focus") or "")[:60],
           "dos": _slist(obj.get("dos")),
           "donts": _slist(obj.get("donts")),
           "watch": str(obj.get("watch") or "")[:80],
           "why": str(obj.get("why") or "")[:400],
           "model": r["model"], "elapsed": r["elapsed"]}
    _cache_write("daily", d, fp, out)
    out["cached"] = False
    return out


# ---------------------------------------------------------------- 教练点评
# 提示词越「像要求」，模型越容易把要求当正文抄回来——长规则版实测会让
# deepseek 把「写 90 到 140 字、引用数字、不复述处方」整段输出到看板上。
# 现在只留最短的一句任务，其余靠 _looks_like_echo 兜底。
NOTE_SYSTEM = ("你是 RunnerCoach 的跑步与骑行教练。按上下文的运动类型，用中文写两句话："
               "今天身体状态如何、最该注意什么。要引用具体数字。只输出这两句正文。")

# 同一日期只允许一个后台任务在跑：看板会被反复打开，不挡住就会
# 同时发好几个请求，白花钱还互相覆盖缓存。
_inflight: dict[str, float] = {}
_inflight_lock = threading.Lock()


def _clean_note(text: str, limit: int = 240) -> str:
    """把模型输出洗成一段能直接放进卡片的话。

    模型常带 ``` 围栏、「教练点评：」前缀、加粗星号、软换行。这些在
    `white-space:pre-wrap` 的卡片里会变成突兀的空行和星号。
    """
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    # 先去记号再去前缀：模型常写「**教练点评：** 正文」，顺序反了就切不掉前缀
    t = t.replace("**", "").replace("__", "").replace("`", "")
    t = re.sub(r"^\s*(教练点评|点评|Coach)\s*[:：]\s*", "", t)
    t = re.sub(r"\s*\n+\s*", " ", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    if len(t) > limit:
        cut = max(t.rfind("。", 0, limit), t.rfind("！", 0, limit),
                  t.rfind("？", 0, limit))
        t = t[:cut + 1] if cut >= 60 else t[:limit].rstrip() + "…"
    return t


def _note_ctx(date: str) -> str:
    """点评用的上下文。

    这里**总是自己 derive**，不接受调用方传进来的 res：看板里的 res 已经
    `attach()` 过计划，上下文会长出两百字。缓存指纹按上下文算，两处口径
    不一致就会「看板写了、轮询又查不到」，永远在重写、永远看不到结果。
    derive 只要几毫秒，统一口径比省这点时间重要。
    """
    return context_markdown(derive(_store(), date))


# 模型把「写作要求」当正文回显时，正文里会成片出现这些短语。
# 真实点评不会同时出现两个以上——它只会说数据。英文的「计划稿」也要拦：
# 实测同一模型会吐出 "Need: 90-140 characters. Cite at least two…" 这种。
_ECHO_MARKS = ("90 到 140", "90-140", "引用具体数字", "引用两个", "引用两个上下文",
               "不要复述", "不复述", "最该注意什么", "输出纯文本", "一句长的或两句",
               "以下是一段点评", "我来写", "字数要求",
               "characters", "cite at least", "don't repeat", "concrete numbers",
               "no meta", "say what to watch", "key data:", "need:")


def _looks_like_echo(text: str) -> bool:
    """判断这段「点评」其实是在复述写作要求、或者在拟提纲。"""
    t = str(text or "")
    low = t.lower()
    if sum(1 for m in _ECHO_MARKS if m in t or m in low) >= 2:
        return True
    # 中文点评的汉字占比很高；英文提纲或「计划稿」几乎没有汉字。
    # 用占比而不是绝对字数：模型偶尔只写五六十字，不该因此被丢掉。
    if len(t) >= 30:
        cjk = sum(1 for ch in t if "\u4e00" <= ch <= "\u9fff")
        return cjk / len(t) < 0.4
    return False


def _fallback_note(date: str) -> str:
    """模型两次都跑偏时的兜底：用规则引擎的数字拼一句。

    宁可给一句「机器味」的话，也不能把提示词摊给用户看，或者干脆空着——
    看板上「教练点评」那块空掉，用户只会以为功能坏了。
    """
    try:
        res = derive(_store(), date)
        sess = brief(res).get("session") or {}
        r = res.get("readiness") or {}
        load = res.get("load") or {}
        sl = res.get("sleep") or {}
        bits = []
        if r.get("score") is not None:
            bits.append(f"恢复度 {r['score']}")
        if load.get("acwr"):
            bits.append(f"ACWR {load['acwr']}")
        if sl.get("total_h"):
            bits.append(f"昨夜睡眠 {sl['total_h']} 小时")
        head = "、".join(bits) if bits else "今天的数据还不太全"
        return (f"{head}；今天的安排是「{sess.get('type') or '按处方'}」，"
                "别在处方之外加量，先把睡眠和蛋白补上。")[:200]
    except Exception:
        return ""


def _gen_note(date: str, ctx: str, fp: str, timeout: int = 40) -> dict:
    """真正调模型写点评。失败返回 ready=False + 原因，不抛。

    要求写在 system 里，user 只给数据 + 一句「直接写正文」：
    上一版把「90 到 140 字、引用数字、不复述处方」全塞进 user 消息，
    模型直接把它们当内容抄了回来，看板上就出现了写作要求。
    """
    user = f"今天的上下文：\n\n{ctx}\n\n直接给出点评正文。"
    # 重试时别再堆「不要复述要求」这类元指令——上一版就是被这些词带偏，
    # 模型转头把它们抄进正文。只给数据 + 一句最朴素的写法要求。
    told = {"role": "user",
            "content": user + "\n\n（先别管字数，直接用中文写两句给跑者看的话。）"}
    try:
        r = llm.chat([{"role": "system", "content": NOTE_SYSTEM},
                      {"role": "user", "content": user}],
                     max_tokens=320, temperature=0.5, timeout=timeout)
    except llm.LLMError as e:
        bad = e.as_dict()
        return {"ready": False, "error": bad.get("error") or "模型调用失败"}
    text = _clean_note(r["text"])
    if text and _looks_like_echo(text):
        # 回显了写作要求：降温度、明确点一下再试一次；还不行就当没写成，
        # 宁可看板上不出现点评，也不能把提示词摊给用户。
        try:
            r2 = llm.chat([{"role": "system", "content": NOTE_SYSTEM},
                           {"role": "user", "content": told}],
                          max_tokens=320, temperature=0.2, timeout=timeout)
            alt = _clean_note(r2["text"])
            text = alt if alt and not _looks_like_echo(alt) else ""
            if text:
                r = r2
        except llm.LLMError:
            text = ""
    if not text:
        # 两次都没写出能用的：退回规则拼句。空着比出错更糟——看板上
        # 「教练点评」整块消失，用户会以为功能坏了。
        fb = _fallback_note(date)
        if not fb:
            return {"ready": False, "error": "模型这次没写出能用的点评"}
        text, model = fb, "规则引擎"
    else:
        model = r["model"]
    _cache_write("note", date, fp,
                 {"text": text, "model": model,
                  "at": dt.datetime.now().strftime("%m-%d %H:%M")})
    return {"ready": True, "text": text, "model": model,
            "at": dt.datetime.now().strftime("%m-%d %H:%M")}


def _warm_note(date: str, ctx: str, fp: str, force: bool = False) -> None:
    """起一个后台线程去写，页面不等它。"""
    now = time.time()
    with _inflight_lock:
        started = _inflight.get(date)
        if started and now - started < 90:      # 90 秒还没回来说明卡死了，允许重来
            return
        _inflight[date] = now

    def _run():
        try:
            if not force:
                hit = _cache_read("note", date, fp)
                if hit and hit.get("text"):
                    return
            _gen_note(date, ctx, fp)
        except Exception:
            pass
        finally:
            with _inflight_lock:
                _inflight.pop(date, None)

    threading.Thread(target=_run, name=f"note-{date}", daemon=True).start()


def _await_cache(date: str, fp: str, secs: int) -> dict | None:
    """等后台任务把缓存写出来，最多等 secs 秒。"""
    end = time.time() + max(1, secs)
    while time.time() < end:
        hit = _cache_read("note", date, fp)
        if hit and hit.get("text"):
            return hit
        time.sleep(0.25)
    return None


def cached_note(date: str | None = None) -> dict | None:
    """只读缓存，不触发任何生成。

    翻历史日期时用它：宁可空着，也不该为了看一眼 11-03 的看板
    就去调一次模型（那是按日期计的，翻十几天就是十几次）。
    """
    d = date or dt.date.today().isoformat()
    if not llm.is_enabled():
        return None
    try:
        fp = _note_fp(_note_ctx(d))
    except Exception:
        return None
    hit = _cache_read("note", d, fp)
    if hit and hit.get("text"):
        return {"ok": True, "date": d, "configured": llm.is_configured(),
                "ready": True, "text": hit["text"], "at": hit.get("at") or "",
                "model": hit.get("model") or "", "cached": True}
    return None


def coach_note(date: str | None = None, *, wait: int = 0,
               force: bool = False) -> dict:
    """教练点评。**默认不阻塞**：命中缓存就返回，没命中就丢后台去写。

    `wait > 0` 时最多阻塞这么多秒（一图流出图时用：那本来就要等几秒，
    顺手把点评补上，用户下次打开看板就不用等）。

    返回固定字段：ok / date / configured / ready / text / at / model / error。
    """
    d = date or dt.date.today().isoformat()
    base: dict = {"ok": True, "date": d, "configured": False,
                  "ready": False, "text": ""}
    if not llm.is_enabled():
        base["off"] = True
        return base
    try:
        ctx = _note_ctx(d)
    except Exception as e:
        base.update(ok=False, error=f"读不到这天的数据：{e}")
        return base
    fp = _note_fp(ctx)
    base["configured"] = llm.is_configured()

    if not force:
        hit = _cache_read("note", d, fp)
        if hit and hit.get("text"):
            base.update(ready=True, text=hit["text"],
                        at=hit.get("at") or "", model=hit.get("model") or "",
                        cached=True)
            return base

    if not base["configured"]:
        return base

    if wait and wait > 0:
        # 已经有一个后台任务在写（多半是看板刚触发过）就别再发一次请求，
        # 等它的结果就行——同一段点评付两遍钱没有意义。
        with _inflight_lock:
            busy = bool(_inflight.get(d))
        if busy:
            hit = _await_cache(d, fp, wait)
            if hit:
                base.update(ready=True, text=hit["text"],
                            at=hit.get("at") or "", model=hit.get("model") or "",
                            cached=True)
            return base
        out = _gen_note(d, ctx, fp, timeout=max(6, int(wait)))
        if out.get("ready"):
            base.update(out, cached=False)
            return base
        base["error"] = out.get("error")
        # 阻塞失败也让后台再试一次：多半是这一次网络抖
    _warm_note(d, ctx, fp, force=force)
    return base


# ---------------------------------------------------------------- 体重复盘
def weight_review(days: int = 60, date: str | None = None,
                  force: bool = False) -> dict:
    """体重与体成分复盘：趋势、速率是否合理、和训练负荷是否匹配。

    数据来自本地 body 表，按用户的体重来源偏好逐日筛选。秤不一定在佳明云里，
    所以这段经常是「只有体重没有体脂」——提示词要求模型区分，别假装有体脂数据。
    """
    from . import body as B
    from . import nutrition as N

    d = date or dt.date.today().isoformat()
    try:
        store = _store()
        res = derive(store, d)
        rows = B.selected_rows(store, end=d, days=days)
    except Exception as e:
        return {"ok": False, "kind": "data", "error": f"读不到体重数据：{e}"}

    # 按日聚合，同一天多次称重取均值（和 body 模块口径一致）
    daily: dict[str, list[float]] = {}
    fat: dict[str, list[float]] = {}
    muscle: dict[str, list[float]] = {}
    for r in rows:
        w = num(r.get("weight_kg"))
        if w:
            daily.setdefault(r["date"], []).append(w)
    for r in rows:
        # body 表的列名是 body_fat_pct / muscle_kg（不是 fat_pct）
        f = num(r.get("body_fat_pct"))
        if f:
            fat.setdefault(r["date"], []).append(f)
        m = num(r.get("muscle_kg"))
        if m:
            muscle.setdefault(r["date"], []).append(m)

    if not daily:
        return {"ok": False, "kind": "data",
                "error": "还没有体重记录。请同步已连接的体重来源，或手动导入 CSV。"}

    pts = [{"date": k, "weight": round(sum(v) / len(v), 1)} for k, v in sorted(daily.items())]
    first, last = pts[0], pts[-1]
    span_days = max(1, (dt.date.fromisoformat(last["date"])
                        - dt.date.fromisoformat(first["date"])).days)
    delta = round(last["weight"] - first["weight"], 1)
    per_week = round(delta / span_days * 7, 2)

    bd = res.get("body") or {}
    nt = {}
    try:
        nt = N.targets(store, d, res)
    except Exception:
        nt = {}

    ctx = {
        "today": d,
        "span_days": span_days,
        "points": pts[-40:],                      # 最多 40 个点，够看趋势又不淹模型
        "delta_total": delta,
        "delta_per_week": per_week,
        # 字段名以 metrics.derive 的 body 段为准（body_fat / muscle，不是 fat / muscle_kg）
        "body": {k: bd.get(k) for k in
                 ("latest", "delta_w", "delta_4w", "body_fat", "fat_delta", "muscle",
                  "muscle_delta", "bmr", "water", "visceral", "verdict", "source", "source_label",
                  "body_fat_any", "body_fat_any_src", "muscle_any", "water_any")
                 if bd.get(k) is not None},
        # nutrition 的目标热量叫 target_kcal；没有体重时 ok=False、这些字段全缺
        "nutrition_target": {k: nt.get(k) for k in
                             ("ok", "goal", "goal_label", "target_kcal", "protein_g",
                              "carb_g", "fat_g", "rate", "goal_note", "advice")
                             if nt.get(k) is not None},
        "training": {
            "ctl": (res.get("load") or {}).get("ctl"),
            "atl": (res.get("load") or {}).get("atl"),
            "weekly_min": (res.get("load") or {}).get("week_min"),
        },
        "recent_activities": [
            {"date": a.get("date"), "type": a.get("type"), "dist_km":
             round((a.get("distance_m") or 0) / 1000, 1),
             "dur_min": round((a.get("duration_s") or 0) / 60)}
            for a in (res.get("activities") or [])[-10:]
        ],
    }

    # 同一天、同一份上下文只算一次：App 切 tab 会整页重载，前端缓存靠不住，
    # 必须落盘缓存，否则每次切过来都重新问一遍模型。
    fp = _ctx_fingerprint(json.dumps(ctx, ensure_ascii=False, sort_keys=True))
    if not force:
        hit = _cache_read("weight", d, fp)
        if hit:
            hit["cached"] = True
            return hit

    instruction = f"""以下是这位跑者最近 {span_days} 天的体重与体成分记录，以及训练负荷。
{json.dumps(ctx, ensure_ascii=False, indent=1)}

请输出 JSON（全部中文，不要 markdown）：
{{
  "summary": "一句话概括体重趋势，含具体数字，25 字以内",
  "trend": "down / up / flat 三选一",
  "rate_verdict": "当前变化速率是否合理，40 字以内",
  "actions": ["2 到 4 条接下来两周的具体动作，每条 30 字以内"],
  "cautions": ["1 到 3 条需要注意的，每条 30 字以内"],
  "nutrition": "结合上面的热量/蛋白目标，给一句吃法上的建议，50 字以内"
}}

纪律：
- body_fat_any / muscle_any 是另一台秤的读数（body_fat_any_src 写明来源）：
  可以提，但要说清「与体重不是同一台秤」，别当成同一天的同一口径。
- 连 body_fat_any 都没有，才不要提体脂/肌肉量，只说体重。
- 变化速率要和训练量对照：周跑量涨了 20% 还在掉体重，要提醒补碳水和睡眠。
- 不做医疗诊断。出现连续快速下降（周降幅超过体重 1%）只提醒，不下结论。"""

    try:
        obj, r = _json_chat([{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": instruction + _JSON_RULES}],
                            schema=SCHEMA_WEIGHT, max_tokens=1600, temperature=0.4)
    except llm.LLMError as e:
        return e.as_dict()

    out = {"ok": True, "date": d, "days": span_days,
           "points": pts[-60:],
           "fat_points": [{"date": k, "fat": round(sum(v) / len(v), 1)}
                          for k, v in sorted(fat.items())][-60:],
           "delta_total": delta, "delta_per_week": per_week,
           "summary": str(obj.get("summary") or "")[:60],
           "trend": str(obj.get("trend") or "flat")[:10],
           "rate_verdict": str(obj.get("rate_verdict") or "")[:120],
           "actions": _slist(obj.get("actions")),
           "cautions": _slist(obj.get("cautions")),
           "nutrition": str(obj.get("nutrition") or "")[:160],
           "target": {k: nt.get(k) for k in
                      ("target_kcal", "protein_g", "carb_g", "fat_g", "goal_label")},
           "model": r.get("model"), "elapsed": r.get("elapsed")}
    if not obj:
        # 解析失败时也别留一张空卡：明确告诉用户是格式问题，而不是「没建议」
        out["summary"] = out["summary"] or "趋势如下，这次没生成解读"
        out["actions"] = ["模型这次没按格式返回，点「重新分析」再试一次"]
        out["fail"] = True
    _cache_write("weight", d, fp, out)
    out["cached"] = False
    return out


# ---------------------------------------------------------------- 活动解读
def activity_review(activity_id: str, date: str | None = None) -> dict:
    """单次活动的对话式解读：跑得怎么样、下次怎么改。

    复用 `brief` 里已有的配速/心率漂移判断，让模型解释「为什么」，
    而不是让它从原始流数据里自己找规律——那是规则引擎更擅长的事。
    `activity_id` 留空时取最近的一条（界面上「分析最近一次训练」就是这个用法）。
    """
    d = date or dt.date.today().isoformat()
    try:
        store = _store()
        res = derive(store, d)
    except Exception as e:
        return {"ok": False, "kind": "data", "error": f"读不到数据：{e}"}

    acts = list(res.get("activities") or [])
    act = None
    if activity_id:
        for a in acts:
            if str(a.get("activity_id")) == str(activity_id):
                act = a
                break
        if act is None:
            # 不在今天的上下文里就去库里找（最多回溯半年）
            try:
                start = (dt.date.fromisoformat(d) - dt.timedelta(days=180)).isoformat()
                for a in store.activities(start, d):
                    if str(a.get("activity_id")) == str(activity_id):
                        act = a
                        break
            except Exception:
                pass
    else:
        if acts:
            act = max(acts, key=lambda x: (x.get("date") or "", x.get("activity_id") or ""))
        else:
            try:
                start = (dt.date.fromisoformat(d) - dt.timedelta(days=60)).isoformat()
                rows = list(store.activities(start, d))
                act = max(rows, key=lambda x: (x.get("date") or "")) if rows else None
            except Exception:
                act = None
    if act is None:
        return {"ok": False, "kind": "data",
                "error": "最近没有训练记录。同步一次，或先跑一次再来分析。"}
    activity_id = str(act.get("activity_id") or "")

    from .actcard import pace_txt

    # 步频不在这条 activity 行里：activities 表只有时长/距离/心率，步频与分圈
    # 存在 activity_detail。不读出来，模型就会说「没有步频数据」——而数据其实在。
    # 分圈同理：判断「后程是否掉速」必须给每公里的配速。
    from . import sports
    bike = sports.family(act.get("type")) == "bike"
    cad_avg, cad_max = None, None
    _detail = {}
    splits: list[dict] = []
    try:
        _row = store.conn.execute(
            "SELECT cad_avg, cad_max, payload FROM activity_detail WHERE activity_id = ?",
            (activity_id,)).fetchone()
        if _row:
            cad_avg, cad_max = num(_row["cad_avg"]), num(_row["cad_max"])
            _detail = json.loads(_row["payload"] or "{}")
            for _lap in (_detail.get("laps") or [])[:16]:
                _p, _d = num(_lap.get("pace_s")), num(_lap.get("dist_m"))
                if not bike and (not _p or (_d or 0) < 200):
                    continue
                splits.append({"km": round((_d or 0) / 1000, 2),
                               "pace": None if bike else pace_txt(_p),
                               "speed_kmh": round(3600 / _p, 1) if bike and _p else None,
                               "power_w": num(_lap.get("pwr_avg")),
                               "duration_s": num(_lap.get("dur_s")),
                               "hr": num(_lap.get("hr_avg")),
                               "cad": num(_lap.get("cad"))})
    except Exception:
        pass

    if bike and (_detail.get("row_metrics") or {}).get("avg_speed_mps") is not None:
        act = {**act, **_detail["row_metrics"]}
    pace = act.get("pace_s_km")
    ctx = {
        "date": (act.get("date") or "")[:10],
        "type": act.get("type"),
        "name": act.get("name"),
        "dist_km": round((act.get("distance_m") or 0) / 1000, 2),
        "duration_min": round((act.get("duration_s") or 0) / 60),
        "avg_pace": pace_txt(pace) if pace and not bike else None,
        "avg_speed_kmh": round(3600 / sports.pace_seconds(act), 1) if bike and sports.pace_seconds(act) else None,
        "cadence_unit": "rpm" if bike else "spm",
        "avg_hr": act.get("avg_hr"),
        "max_hr": act.get("max_hr"),
        "avg_cadence": cad_avg if cad_avg is not None else act.get("avg_cadence"),
        "max_cadence": cad_max,
        "splits": splits[:12],
        "elev_gain_m": act.get("ascent"),
        "calories": act.get("calories"),
        "training_effect": {"aerobic": act.get("aerobic_te"), "anaerobic": act.get("anaerobic_te")},
        "today_readiness": (res.get("readiness") or {}).get("score"),
        "week_load": {
            "ctl": (res.get("load") or {}).get("ctl"),
            "atl": (res.get("load") or {}).get("atl"),
        },
    }
    if bike:
        from .cycling import for_detail, intervals
        ctx["cycling"] = for_detail(_detail)
        ctx["cycling"].pop("power_histogram", None)
        ctx["power_intervals"] = intervals(_detail)
        ctx["sport_load"] = (res.get("load_by_sport") or {}).get("bike")

    instruction = f"""以下是这位运动者的一次训练记录及当时的身体状态。

{json.dumps(ctx, ensure_ascii=False, indent=1)}

请输出 JSON（全部中文，不要 markdown）：
{{
  "title": "给这次训练起个 12 字以内的名字，比如「配速稳，后程略掉」",
  "summary": "两句话总结这次训练质量，必须引用具体数字，70 字以内",
  "good": ["1 到 3 条做得好的地方"],
  "improve": ["1 到 3 条可以改进的地方"],
  "next": "下一次同类训练的具体建议，按运动类型使用指标，45 字以内"
}}

纪律：
- 越野跑要在意爬升与后程掉速；路跑要看配速漂移；心率数据缺失就别分析心率。
- 骑行看踏频、功率、功率间歇和路况；不要仅凭均速判断训练强度或耐力，也不要评价跑步步幅。
- FTP 未设置不推断 IF/TSS；功率缺失不臆测功率区间。跨项目的活动负荷不等于完成课表。
- **avg_cadence / max_cadence / max_hr / splits 已经给出来了就必须用**：
  跑步参考分圈配速；骑行参考功率与均速，并承认地形、风阻影响；
  不要写「没有步频数据」「没有最大心率」这类与上文矛盾的话。
- 只有字段确实是 null 才可以说缺数据，并且要点明缺的是哪一个。
- 只有一次数据时不要谈「趋势」，只谈这一次。
- 别把正常的长距离掉速说成问题，也别把明显的过速说成「状态好」。"""

    try:
        obj, r = _json_chat([{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": instruction + _JSON_RULES}],
                            schema=SCHEMA_ACT, max_tokens=1600, temperature=0.5)
    except llm.LLMError as e:
        return e.as_dict()

    out = {"ok": True, "activity_id": activity_id, "ctx": ctx,
           "title": str(obj.get("title") or "训练解读")[:30],
           "summary": str(obj.get("summary") or "")[:200],
           "good": _slist(obj.get("good")),
           "improve": _slist(obj.get("improve")),
           "next": str(obj.get("next") or "")[:120],
           "model": r.get("model"), "elapsed": r.get("elapsed")}
    if not obj:
        out["summary"] = "这次没拿到结构化解读，点上面的按钮再试一次。"
        out["fail"] = True
    return out


# ---------------------------------------------------------------- 对话
# 上下文窗口不在这里定义：条数与摘要都在 chatstore（KEEP / digest）。
# 以前这里是 MAX_TURNS=16，和 chatstore 的窗口并存会出现两套口径，
# 一个改了另一个没改，上下文就会悄悄变短——所以只留一份。


def chat(messages: list[dict] | None = None, date: str | None = None,
         text: str | None = None) -> dict:
    """自由对话。系统提示里塞一份「今日简报」摘要，模型不用每次都问数据。

    历史的唯一真相在服务端（`chatstore`）：前端传的 messages 只用来取「这一句
    新问题」，不再作为上下文来源——否则切一次 tab 就断片，而且客户端能随意
    篡改历史。落盘先于调用，模型失败再把这句撤回去。
    """
    d = date or dt.date.today().isoformat()
    brief_txt = ""
    try:
        store = _store()
        res = derive(store, d)
        b = brief(res)
        sess = b.get("session") or {}
        r = res.get("readiness") or {}
        s = res.get("sleep") or {}
        bd = res.get("body") or {}
        brief_txt = (
            f"今天是 {d}。恢复度 {r.get('score')}（{r.get('level')}），"
            f"昨夜睡眠 {s.get('total_h')} 小时、睡眠分 {s.get('score')}。"
            f"今日处方：{sess.get('type')}｜{sess.get('duration')}｜{sess.get('intensity')}。"
            f"体重 {bd.get('latest') or '无记录'} kg。"
            f"风险：{'；'.join(f['text'] for f in (b.get('flags') or [])[:3]) or '无'}。"
        )
        _eb = event_brief(d)
        if _eb:
            brief_txt += " 备赛：" + _eb + "。"
    except Exception:
        brief_txt = f"今天是 {d}。（数据读取失败，只做通用建议）"

    # 新问题：优先 text（新前端只发这一句），退回 messages 的最后一条用户消息
    q = (text or "").strip()
    if not q:
        for m in reversed(messages or []):
            if m.get("role") == "user" and (m.get("content") or "").strip():
                q = (m.get("content") or "").strip()
                break
    if not q:
        return {"ok": False, "kind": "input", "error": "没有要回答的问题"}

    from . import chatstore as CS

    CS.append("user", q)
    hist = CS.context()

    system = SYSTEM + "\n\n【今日简报】\n" + brief_txt + \
        "\n\n用户问的数据如果简报里没有，就明说「今天的数据里没有这一项」，不要猜。"

    try:
        r = llm.chat([{"role": "system", "content": system}] + hist,
                     max_tokens=1000, temperature=0.65)
    except llm.LLMError as e:
        CS.drop_last_user()          # 没答上就撤掉，别在历史里留孤立的提问
        return e.as_dict()
    CS.append("assistant", r["text"])
    # 超出窗口就把最老的一批压进摘要。失败不影响这次回答——
    # 摘要只是让下次接得上话，不是对话本身的一部分。
    archived = False
    try:
        if CS.should_archive():
            archived = bool(CS.archive(CS.model_gen))
    except Exception:
        archived = False
    return {"ok": True, "reply": r["text"], "model": r["model"],
            "elapsed": r["elapsed"], "archived": archived}


# ---------------------------------------------------------------- 状态
def status() -> dict:
    """给界面判断「能不能用、要不要引导配置」。"""
    cfg = llm.load_config()
    st = {"ok": True, "configured": llm.is_configured(cfg), "mode": cfg.get("mode"),
          "model": llm.effective_model(cfg), "provider": cfg.get("provider")}
    if cfg.get("mode") == "platform":
        b = llm.balance()
        st["balance"] = b.get("balance") if b.get("ok") else None
        st["balance_error"] = None if b.get("ok") else b.get("error")
    try:
        today = dt.date.today().isoformat()
        st["daily_cached"] = _cache_path("daily", today).is_file()
    except Exception:
        st["daily_cached"] = False
    return st
