# -*- coding: utf-8 -*-
"""静态姿态评估 → 薄弱项 → 4 周力量方案。

为什么只做「静态照片」，不做跑姿视频
----------------------------------
issue 里原本写的是三角度跑姿视频。视频要端上模型（包体积、耗电）或上传云端，
两条路都不轻，而且跑姿是动态的，单帧判读反而更不稳。
静态筛查动作（过头深蹲、单腿蹲）本来就是「站着不动让人看」的设计，
拍照不影响判读，闭环能先跑通。

模型能看什么、不能看什么（本模块最需要守住的边界）
------------------------------------------------
能看：对齐关系——膝盖是否落在脚尖内侧、足跟是否离地、躯干是否明显前倾、
      两侧骨盆是否一高一低。这些是几何关系，一张正/侧面照足够。
不能看：关节角度精确值、动态过程、左右差多少度、疼不疼。
所以输出只有四档 ok / mild / clear / unclear，**从不给度数**；
疼痛（FMS 的 0 分）永远由人勾选，不由模型判——模型看不到疼。

一条照片只评一个动作、一个拍摄角度。多动作混在一张里，模型会给「平均印象」，
那是最没有用的东西。
"""
from __future__ import annotations

import json
import re
from typing import Any

# ---- 可评估的动作 -----------------------------------------------------------
# 选这三个是因为：它们有公开的观察点标准（FMS 过头深蹲、NASM-CES 单腿蹲），
# 且都是跑者薄弱项的直接投影。每张照片只评一个动作、一个角度。
MOVES: dict[str, dict] = {
    "ohs_front": {
        "name": "过头深蹲 · 正面",
        "how": "双脚与肩同宽，脚尖朝前，双手举过头顶；蹲到大腿接近水平，停 2 秒。",
        "shot": "手机放膝盖高度、正前方 2–3 米，全身入镜；穿短裤或紧身裤，别穿宽腿裤。",
        "checks": ["knee_valgus", "arch_collapse", "heel_lift", "pelvis_level"],
    },
    "ohs_side": {
        "name": "过头深蹲 · 侧面",
        "how": "同上，但侧对镜头做一次，蹲到最低停 2 秒。",
        "shot": "手机放膝盖高度、正侧方 2–3 米，拍到整个人（含头顶和脚）。",
        "checks": ["torso_lean", "depth", "heel_lift", "lumbar_arch"],
    },
    # 单腿蹲拆成左右两条：一张照片只能判一条支撑腿，合成一条却写
    # 「左右各拍一张」会让用户以为能传多张（2026-11-12 用户反馈）。
    # 骨盆侧倾要左右两张对比才有意义，单张判不出来，所以不在这两条的观察点里。
    "sls_left": {
        "name": "单腿蹲 · 左腿支撑",
        "how": "双手前平举，抬起右腿伸直；左腿慢慢蹲到大腿接近水平，停 1 秒再站起。",
        "shot": "正前方 2–3 米，膝盖高度；这一张只拍左腿支撑那一次。",
        "checks": ["knee_valgus", "trunk_lateral", "heel_lift"],
    },
    "sls_right": {
        "name": "单腿蹲 · 右腿支撑",
        "how": "双手前平举，抬起左腿伸直；右腿慢慢蹲到大腿接近水平，停 1 秒再站起。",
        "shot": "正前方 2–3 米，膝盖高度；这一张只拍右腿支撑那一次。",
        "checks": ["knee_valgus", "trunk_lateral", "heel_lift"],
    },
}

# ---- 观察点 ----------------------------------------------------------------
# `see` 是给模型看的「判读依据」，也直接显示给用户——用户要能自己复核，
# 不能只有一个结论没有理由。
CHECKS: dict[str, dict] = {
    "knee_valgus": {
        "label": "膝内扣",
        "see": "膝盖连线是否落在第二、三脚趾内侧（膝往中间塌）",
        "weak": "hip_abduct",
    },
    "arch_collapse": {
        "label": "足弓塌陷",
        "see": "内侧足弓是否贴地、脚踝是否向内倒",
        "weak": "ankle_rom",
    },
    "heel_lift": {
        "label": "足跟离地",
        "see": "蹲到最低时足跟是否离开地面",
        "weak": "ankle_rom",
    },
    "pelvis_level": {
        "label": "骨盆不平",
        "see": "两侧髂前上棘是否一高一低（腰带线是否水平）",
        "weak": "hip_abduct",
    },
    "torso_lean": {
        "label": "躯干过度前倾",
        "see": "躯干与小腿是否明显不平行（上身快趴到大腿上）",
        "weak": "eccentric",
    },
    "depth": {
        "label": "蹲不到深度",
        "see": "大腿是否没到水平线（髋褶高于膝盖）",
        "weak": "eccentric",
    },
    "lumbar_arch": {
        "label": "腰椎过度前凸",
        "see": "下背部是否塌成一个大弧、肋骨外翻",
        "weak": "core_anti",
    },
    "pelvis_drop": {
        "label": "骨盆侧倾",
        "see": "抬起那侧的骨盆是否往下掉（非支撑侧髋下沉）",
        "weak": "hip_abduct",
    },
    "trunk_lateral": {
        "label": "躯干侧倾",
        "see": "上身是否明显倒向支撑腿一侧",
        "weak": "core_anti",
    },
}

STATES = {
    "ok": "正常",
    "mild": "轻度",
    "clear": "明显",
    "unclear": "看不清",
}

# ---- 薄弱项 → 训练方向 ------------------------------------------------------
# 动作全部取自 workout.POWER 那套动作池，保证「评估出来的弱项」和
# 「课表里练的东西」是同一批动作，不会出现评估说练 A、课表练 B。
WEAK: dict[str, dict] = {
    "hip_abduct": {
        "label": "髋外展 / 骨盆控制",
        "why": "臀中肌撑不住，膝盖就会往里塌，跑步时落地缓冲全压在膝内侧。",
        "moves": [
            ("弹力带侧向走", 3, "12 步/侧", "臀中肌发力，膝盖别内扣"),
            ("保加利亚分腿蹲", 3, "10 次/侧", "后脚垫高，前膝对准第二脚趾"),
            ("单腿硬拉", 3, "10 次/侧", "慢下快起，骨盆保持水平"),
        ],
    },
    "ankle_rom": {
        "label": "踝背屈活动度",
        "why": "踝背屈不够，蹲到底只能靠足跟离地或膝内扣代偿，下坡和落地都吃亏。",
        "moves": [
            ("提踵", 3, "15 次", "数 4 秒慢慢落下，顶端停 1 秒"),
            ("靠墙踝背屈推", 3, "10 次/侧", "膝盖压过脚尖，足跟不离地"),
            ("台阶落跟", 3, "12 次/侧", "前掌踩台阶，脚跟慢慢下沉"),
        ],
    },
    "single_leg": {
        "label": "单腿稳定性",
        "why": "单腿站不稳，每一步落地都在微调，长距离后段最先崩的就是这个。",
        "moves": [
            ("单脚站", 3, "45 秒/侧", "闭眼更难，脚趾别抓地"),
            ("台阶上步", 3, "10 次/侧", "全脚踩实，不借反弹"),
            ("单腿硬拉", 3, "10 次/侧", "支撑腿微屈，骨盆不转"),
        ],
    },
    "core_anti": {
        "label": "核心抗旋",
        "why": "核心抗不住旋，上下坡时骨盆会跟着腿转，力量传到地面就散了。",
        "moves": [
            ("死虫", 3, "10 次/侧", "腰贴地，对侧手脚同时伸"),
            ("鸟狗", 3, "10 次/侧", "骨盆不转，伸到最远停 1 秒"),
            ("侧平板", 3, "30 秒/侧", "髋别往下掉"),
        ],
    },
    "eccentric": {
        "label": "离心力量",
        "why": "下坡靠的是股四头离心刹车，离心不够就是「上坡还行、下坡腿软」。",
        "moves": [
            ("离心下蹲", 3, "8 次", "数 4 秒下，到底立刻起"),
            ("保加利亚分腿蹲", 3, "10 次/侧", "慢下，前膝别内扣"),
            ("台阶上步", 3, "10 次/侧", "下去那一下控制住"),
        ],
    },
}

# 4 周周期：第 4 周减量并复测，避免「练满四周直接加量」把疲劳堆上去
WEEKS = [
    {"wk": 1, "note": "适应：动作做对优先于重量", "sets": 3, "reps": "8"},
    {"wk": 2, "note": "加量：次数上去，节奏不变", "sets": 3, "reps": "10–12"},
    {"wk": 3, "note": "加强：组数加一组，最后两组接近力竭", "sets": 4, "reps": "8"},
    {"wk": 4, "note": "减量 + 复测：同一套动作重拍对比", "sets": 2, "reps": "8"},
]

SYSTEM = """你是跑步力量教练，正在看一张「静态动作筛查」照片。任务不是夸人，是找代偿。

铁律：
1. 只看几何对齐关系，不猜肌肉、不猜角度、不给度数。
2. 看不清就填 unclear，绝对不许猜。照片模糊、遮挡、角度不对、衣服太宽，
   都是 unclear 的正当理由。
3. 每个判断必须写 evidence：照片里你实际看到的画面。看不出依据的不要写。
4. 不诊断疾病、不判断疼痛。疼痛只在用户自述里出现，不归你判。
5. 只输出 JSON，不要解释、不要 markdown。

输出格式：
{"checks": {"<观察点id>": {"state": "ok|mild|clear|unclear", "evidence": "一句话"}},
 "shot_quality": "good|usable|bad",
 "note": "如果 shot_quality 不是 good，说明哪里影响判读"}"""


def _clean_json(text: str) -> dict:
    """模型偶尔会套 ```json 围栏或前后加话，这里只取第一个 JSON 对象。

    输出被 max_tokens 截断时（实测 900 tokens 就够呛），`raw_decode` 会抛
    「Unterminated string」——整页报错太亏，前面判完的观察点是有效信息。
    所以先正常解析，失败就按括号栈补齐再试一次。
    """
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.M)
    i = t.find("{")
    if i < 0:
        raise ValueError("模型没有返回 JSON，换一张再试")
    body = t[i:]
    try:
        obj, _ = json.JSONDecoder().raw_decode(body)
    except json.JSONDecodeError:
        obj = _repair_truncated(body)
        if obj is None:
            raise ValueError(
                "模型返回的 JSON 不完整（输出被截断）。换一张小一点的照片再试，"
                "或在设置里换一个支持图片输入的模型") from None
    if not isinstance(obj, dict):
        raise ValueError("模型返回的不是对象")
    return obj


def _repair_truncated(t: str) -> dict | None:
    """把被截断的 JSON 补齐：闭合未结束的字符串，再按栈补括号。

    只在 `_clean_json` 的异常路径里用。补出来的 evidence 可能少半句，
    但 state 通常是完整的；normalize 会丢掉不合法的值，不会把半句话写进方案。
    """
    depth: list[str] = []
    in_str = esc = False
    end = 0
    for i, ch in enumerate(t):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            end = i + 1
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth.append(ch)
        elif ch in "}]":
            if depth:
                depth.pop()
        end = i + 1
    fixed = t[:end] + ('"' if in_str else "")
    for op in reversed(depth):
        fixed += "}" if op == "{" else "]"
    try:
        obj = json.loads(fixed)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def normalize(raw: dict, move_id: str) -> dict:
    """把模型输出收敛到「这个动作允许的观察点 + 四个状态」。

    模型很爱自由发挥：多编几个观察点、用「中等」「偏重」这种词。
    一律丢弃或折算，宁可少判也不把噪声写进方案。
    """
    move = MOVES.get(move_id)
    if not move:
        raise ValueError(f"未知动作：{move_id}")
    allowed = set(move["checks"])
    got = raw.get("checks") if isinstance(raw.get("checks"), dict) else {}
    alias = {"normal": "ok", "good": "ok", "none": "ok", "slight": "mild",
             "moderate": "mild", "偏": "mild", "明显": "clear", "严重": "clear",
             "good_but": "mild", "unknown": "unclear", "n/a": "unclear"}
    out: dict[str, dict] = {}
    for cid in move["checks"]:
        it = got.get(cid)
        if not isinstance(it, dict):
            out[cid] = {"state": "unclear", "evidence": "模型没提这一项"}
            continue
        s = str(it.get("state") or "").strip().lower()
        s = alias.get(s, s)
        if s not in STATES:
            s = "unclear"
        out[cid] = {"state": s, "evidence": str(it.get("evidence") or "")[:120]}
    quality = str(raw.get("shot_quality") or "usable").strip().lower()
    if quality not in ("good", "usable", "bad"):
        quality = "usable"
    return {"move": move_id, "name": move["name"], "checks": out,
            "shot_quality": quality, "note": str(raw.get("note") or "")[:200],
            "extra_ignored": sorted(set(got) - allowed)}


def assess(image_b64: str, move_id: str, note: str = "",
           mime: str = "image/jpeg") -> dict:
    """一张照片 + 一个动作 → 结构化判读。失败抛 llm.LLMError / ValueError。

    `note` 可以不传。传了只用来补「照片看不出的东西」（比如「今天右膝有点胀」），
    但**不要**在 note 里说「我觉得我膝内扣」——那会把答案喂给模型。
    """
    from . import llm

    move = MOVES.get(move_id)
    if not move:
        raise ValueError(f"未知动作：{move_id}")
    if not image_b64:
        raise ValueError("没有照片")
    lines = "\n".join(f"- {cid}（{CHECKS[cid]['label']}）：{CHECKS[cid]['see']}"
                      for cid in move["checks"])
    ask = (f"动作：{move['name']}\n标准做法：{move['how']}\n"
           f"需要逐个判断这些观察点：\n{lines}\n")
    if note.strip():
        ask += f"\n用户补充（只作背景，不要据此下结论）：{note.strip()[:200]}\n"
    ask += "\n按系统提示的 JSON 格式输出，观察点 id 必须用上面列出的这些。"
    msg = [{"role": "system", "content": SYSTEM},
           {"role": "user", "content": [
               {"type": "text", "text": ask},
               {"type": "image_url", "image_url": {
                   "url": f"data:{mime or 'image/jpeg'};base64,{image_b64}"}}]}]
    # 两次尝试：第一次带 response_format（约束输出），第二次不带。
    # 有些服务在多模态 + json_object 一起用时会返回空内容——用户实测同一张图
    # 之前成功过、这次报「模型没有返回 JSON」，那是上游偶发，不是图的问题。
    # 第二次退化成「普通文本 + 自己找 JSON」，成功率明显更高。
    last_err = "未知"
    for attempt in range(2):
        r = llm.chat(msg, temperature=0.2, max_tokens=1600,
                     json_mode=(attempt == 0))
        text = (r.get("text") or "").strip()
        if not text:
            last_err = "模型返回了空内容"
            continue
        try:
            return normalize(_clean_json(text), move_id)
        except ValueError as e:
            last_err = str(e)
            continue
    raise ValueError(
        f"模型连着两次都没给出可用结果（{last_err}）。这多半是上游偶发，"
        "隔一会儿重拍再试；如果一直这样，在设置里换一个支持图片输入的模型")
    return normalize(_clean_json(r.get("text") or ""), move_id)


def weak_points(result: dict) -> list[str]:
    """判读结果 → 薄弱项（去重、保持出现顺序）。

    只有 mild / clear 才算「弱」：mild 说明有代偿趋势，值得练；
    unclear 一律不算——把「看不清」当弱项，会让方案凭空调量。
    """
    seen: list[str] = []
    for cid, it in (result.get("checks") or {}).items():
        if it.get("state") not in ("mild", "clear"):
            continue
        w = (CHECKS.get(cid) or {}).get("weak")
        if w and w not in seen:
            seen.append(w)
    return seen


def plan(results: list[dict], *, per_session: int = 5) -> dict:
    """多张判读 → 一份 4 周方案。

    动作按「触发它的观察点数量」排序：两个动作都指向同一个弱项时，
    说明这个弱项更硬，排前面。没有弱项时不硬凑，直接说「暂不需要专项」。
    """
    hit: dict[str, list[str]] = {}
    for r in results or []:
        for cid, it in (r.get("checks") or {}).items():
            if it.get("state") not in ("mild", "clear"):
                continue
            w = (CHECKS.get(cid) or {}).get("weak")
            if not w:
                continue
            tag = f"{r.get('name', '')} · {CHECKS[cid]['label']}"
            hit.setdefault(w, [])
            if tag not in hit[w]:
                hit[w].append(tag)
    order = sorted(hit, key=lambda k: (-len(hit[k]), list(WEAK).index(k)))
    moves: list[dict] = []
    used: set[str] = set()
    for w in order:
        for name, sets, reps, cue in WEAK[w]["moves"]:
            if name in used or len(moves) >= per_session:
                continue
            used.add(name)
            # 依据只写观察点名（「膝内扣」而不是「单腿蹲 · 正面 · 膝内扣」）：
            # 动作名在卡片标题里已经有了，重复一遍会把「为什么」撑成两行
            short = [x.split(" · ")[-1] for x in hit[w][:2]]
            moves.append({"name": name, "sets": sets, "reps": reps, "cue": cue,
                          "why": f"补{WEAK[w]['label']}：" + "、".join(short)})
    unclear_n = sum(1 for r in results or [] for it in (r.get("checks") or {}).values()
                    if it.get("state") == "unclear")
    bad = [r.get("name") for r in results or [] if r.get("shot_quality") == "bad"]
    return {
        "weak": [{"id": w, "label": WEAK[w]["label"], "why": WEAK[w]["why"],
                  "from": hit[w]} for w in order],
        "moves": moves,
        "weeks": [dict(x) for x in WEEKS],
        "unclear": unclear_n,
        "resheet": bad,
        "empty": not moves,
    }


def text_report(res: dict) -> str:
    """一句话版，给 AI 教练上下文和 CLI 用。"""
    p = plan(res.get("results") or [])
    if p["empty"]:
        return "姿态筛查：没发现需要专项补的弱项"
    labs = "、".join(w["label"] for w in p["weak"])
    return f"姿态筛查：{labs}（{len(p['moves'])} 个动作 / 4 周）"


# ---- 命令行 ----------------------------------------------------------------
def _list_text() -> str:
    out = ["可评估的动作（一张照片只评一个动作、一个角度）", ""]
    for mid, m in MOVES.items():
        out.append(f"{mid}　{m['name']}")
        out.append(f"  怎么做：{m['how']}")
        out.append(f"  怎么拍：{m['shot']}")
        out.append("  看什么：" + "、".join(CHECKS[c]["label"] for c in m["checks"]))
        out.append("")
    return "\n".join(out)


def _demo() -> None:
    """不调模型，用一份假判读跑通「薄弱项 → 方案」这段。"""
    fake = {"move": "sls_left", "name": MOVES["sls_left"]["name"],
            "checks": {"knee_valgus": {"state": "clear", "evidence": "支撑膝明显向内塌"},
                       "trunk_lateral": {"state": "ok", "evidence": "躯干基本竖直"},
                       "heel_lift": {"state": "unclear", "evidence": "脚被裤脚挡住"}},
            "shot_quality": "usable", "note": ""}
    p = plan([fake])
    print(text_report({"results": [fake]}))
    for w in p["weak"]:
        print(f"  弱项 {w['label']}：{w['why']}")
        print(f"       依据：{'、'.join(w['from'])}")
    print()
    for m in p["moves"]:
        print(f"  {m['name']}　{m['sets']} 组 × {m['reps']}　{m['cue']}")
        print(f"       为什么：{m['why']}")
    print()
    for w in p["weeks"]:
        print(f"  W{w['wk']}　{w['sets']} 组 × {w['reps']}　{w['note']}")


def main(argv: list[str] | None = None) -> int:
    import argparse
    import base64
    import sys
    from pathlib import Path

    ap = argparse.ArgumentParser(prog="posture", description="静态姿态评估 → 力量方案")
    ap.add_argument("--list", action="store_true", help="列出可评估的动作与拍摄要点")
    ap.add_argument("--demo", action="store_true", help="不调模型，演示方案生成")
    ap.add_argument("--move", help="动作 id（见 --list）")
    ap.add_argument("--photo", help="照片路径")
    ap.add_argument("--note", default="", help="补充说明（只作背景，别写结论）")
    ap.add_argument("--json", action="store_true", help="输出原始 JSON")
    a = ap.parse_args(argv)

    if a.list:
        print(_list_text())
        return 0
    if a.demo:
        _demo()
        return 0
    if not a.move or not a.photo:
        ap.print_help()
        return 2
    p = Path(a.photo)
    if not p.exists():
        print(f"× 照片不存在：{p}")
        return 2
    b64 = base64.b64encode(p.read_bytes()).decode()
    mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
    try:
        res = assess(b64, a.move, a.note, mime)
    except Exception as e:                       # noqa: BLE001
        print(f"× 评估失败：{e}")
        print("  模型需要支持图片输入（deepseek-v4.1-flash / mimo-v2.5 / kimi-k3）")
        return 1
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    print(f"{res['name']}　（照片质量 {res['shot_quality']}）")
    for cid, it in res["checks"].items():
        print(f"  {CHECKS[cid]['label']:<6} {STATES[it['state']]:<4} {it['evidence']}")
    if res.get("note"):
        print(f"  备注：{res['note']}")
    print()
    print(text_report({"results": [res]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
