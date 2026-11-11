"""营养：把体重 / 体成分 + 训练量换算成「今天吃多少」。

为什么单独一层
--------------
看板原本只回答「今天怎么练」，吃多少全靠自己估。减脂期真正容易出错的不是
赤字本身，而是**赤字里掉的是脂肪还是肌肉**——所以这里把三件事绑在一起：
消耗基线、蛋白下限、掉重速率校准。

口径（按减脂期设计，maintain / gain 只改缺口语蛋白系数）
------------------------------------------------------
- 消耗基线：近 7 天 Garmin `totalKilocalories` 的均值（已含 BMR + 日常 + 运动）。
  当天往往只过了一半，那个数只用来显示「已消耗」，**不进基线**，否则目标摄入
  会被自己的半天数据压低。
- 缺口：默认 400 kcal（约 0.35 kg/周），且目标摄入**不低于 BMR**——长期低于
  BMR 最先掉的是肌肉和恢复能力。
- 蛋白 1.8 g/kg：减脂期保肌肉的关键项，优先级高于总热量；掉肌肉时提到 2.0。
- 碳水按近 7 天日均训练时长分档（3.5 / 4.5 / 5.5 / 6.5 g/kg），而不是拍一个固定值。
- 脂肪吃掉剩余热量，下限 0.6 g/kg（激素合成需要，别为了凑热量把它压到 20 g）。
- 速率校准：实际周变化 vs 目标 −0.5% 体重/周。但如果 4 周累计几乎没动，说明这周
  的跌幅是水分，不给「加缺口」的建议——这也是 body.verdict 的同一条原则。

只做静态估算，不做饮食记录：没有食物库、不算每一餐，避免把工具做成记账软件。
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from .metrics import num

GOALS = {
    "cut": {"label": "减脂", "gap": 400, "protein": 1.8, "note": "备赛期控体重"},
    "maintain": {"label": "维持", "gap": 0, "protein": 1.6, "note": "热量平衡，冲训练量"},
    "gain": {"label": "增重", "gap": -250, "protein": 1.8, "note": "小幅盈余，优先长肌肉"},
}

CONF = Path(__file__).resolve().parent.parent / "config.json"
# 饮食记录是可选的一层：有就拿来对账，没有就只显示目标，不强制记账。
MEALS = Path(__file__).resolve().parent.parent / "data" / "meals.json"


def load_meals() -> dict:
    """读 data/meals.json（{日期: [餐次...]}），坏文件当空处理。"""
    try:
        data = json.loads(MEALS.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def add_meal(date: str, name: str, kcal: float, protein_g: float | None = None,
             carb_g: float | None = None, fat_g: float | None = None,
             clock: str | None = None) -> dict:
    """记一餐，返回当天的合计。"""
    data = load_meals()
    row = {
        "time": clock or dt.datetime.now().strftime("%H:%M"),
        "name": name,
        "kcal": round(float(kcal)),
        "protein_g": round(float(protein_g or 0)),
        "carb_g": round(float(carb_g or 0)),
        "fat_g": round(float(fat_g or 0)),
    }
    data.setdefault(date, []).append(row)
    MEALS.parent.mkdir(parents=True, exist_ok=True)
    MEALS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"row": row, "total": intake(date)}


def add_meals(date: str, items: list[dict], clock: str | None = None) -> dict:
    """批量记一餐（拍照识别常见 2~5 个菜），只写一次文件。"""
    data = load_meals()
    rows = []
    for it in items or []:
        name = str((it or {}).get("name") or "").strip()
        kcal = num((it or {}).get("kcal"))
        if not name or not kcal or kcal <= 0:
            continue
        row = {
            "time": clock or dt.datetime.now().strftime("%H:%M"),
            "name": name[:30],
            "kcal": round(kcal),
            "protein_g": round(num(it.get("protein_g")) or 0, 1),
            "carb_g": round(num(it.get("carb_g")) or 0, 1),
            "fat_g": round(num(it.get("fat_g")) or 0, 1),
        }
        data.setdefault(date, []).append(row)
        rows.append(row)
    if rows:
        MEALS.parent.mkdir(parents=True, exist_ok=True)
        MEALS.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    return {"rows": rows, "total": intake(date)}


# ---------------------------------------------------------------- 拍照识热量
MEAL_PROMPT = """你是运动营养师。看照片识别食物，估算**整份**的份量与营养。
只返回 JSON，不要解释：
{"items":[{"name":"米饭","grams":200,"kcal":232,"protein_g":4.8,"carb_g":51.6,"fat_g":0.6}],"note":"可选的一句提醒"}
规则：
- 按照片里实际可见的份量估，拿不准取常见中位数；
- 只估照片里有的食物，不要补没看到的菜；
- kcal / 蛋白 / 碳水 / 脂肪都按整份计，不是每 100 g；
- 最多 8 项；认不出食物就返回空 items。"""


def _json_obj(text: str) -> dict:
    """容忍 ```json 围栏和前后废话，取第一个 JSON 对象。"""
    t = str(text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        t = t[i:j + 1]
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else {}
    except ValueError:
        return {}


def estimate_meal(image_b64: str, mime: str = "image/jpeg") -> dict:
    """图片（base64）→ 食物清单与营养估算。失败抛 llm.LLMError / ValueError。"""
    from . import llm

    if not image_b64:
        raise ValueError("没有图片")
    msg = [
        {"role": "system", "content": MEAL_PROMPT},
        {"role": "user", "content": [
            {"type": "text", "text": "估算这一餐的热量和三大营养素。"},
            {"type": "image_url", "image_url": {
                "url": f"data:{mime or 'image/jpeg'};base64,{image_b64}"}},
        ]},
    ]
    r = llm.chat(msg, temperature=0.2, max_tokens=900)
    obj = _json_obj(r.get("text") or "")
    items = []
    for it in (obj.get("items") or [])[:8]:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        kcal = num(it.get("kcal"))
        if not name or not kcal or kcal <= 0:
            continue
        items.append({
            "name": name[:30],
            "grams": round(num(it.get("grams")) or 0),
            "kcal": round(kcal),
            "protein_g": round(num(it.get("protein_g")) or 0, 1),
            "carb_g": round(num(it.get("carb_g")) or 0, 1),
            "fat_g": round(num(it.get("fat_g")) or 0, 1),
        })
    return {
        "items": items,
        "total": {
            "kcal": round(sum(i["kcal"] for i in items)),
            "protein_g": round(sum(i["protein_g"] for i in items), 1),
            "carb_g": round(sum(i["carb_g"] for i in items), 1),
            "fat_g": round(sum(i["fat_g"] for i in items), 1),
        },
        "note": str(obj.get("note") or "")[:120],
        "model": r.get("model"),
        "elapsed": r.get("elapsed"),
    }


def intake(date: str) -> dict:
    """当天已记录的摄入合计。没记录时 ok=False，看板就退回只显示目标。"""
    items = (load_meals().get(date) or [])
    if not items:
        return {"ok": False, "items": [], "kcal": 0, "protein_g": 0, "carb_g": 0, "fat_g": 0}
    return {
        "ok": True,
        "items": items,
        "count": len(items),
        "kcal": round(sum(num(x.get("kcal")) or 0 for x in items)),
        "protein_g": round(sum(num(x.get("protein_g")) or 0 for x in items)),
        "carb_g": round(sum(num(x.get("carb_g")) or 0 for x in items)),
        "fat_g": round(sum(num(x.get("fat_g")) or 0 for x in items)),
    }


def default_goal() -> str:
    """目标写在 config.json 的 body_goal，缺省按减脂算。"""
    try:
        data = json.loads(CONF.read_text(encoding="utf-8"))
        g = str(data.get("body_goal") or "").strip().lower()
        return g if g in GOALS else "cut"
    except (OSError, ValueError):
        return "cut"


def save_goal(goal: str) -> bool:
    """把目标写回 config.json，后面的看板 / 教练上下文都用它。"""
    if goal not in GOALS:
        return False
    try:
        data = json.loads(CONF.read_text(encoding="utf-8")) if CONF.exists() else {}
    except ValueError:
        data = {}
    data["body_goal"] = goal
    CONF.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def _burn_base(store, date: str, days: int = 8) -> tuple[float | None, int]:
    """近 7 天（不含当天）的总消耗均值，剔除空值与只同步了一半的天。"""
    rows = store.recent_days(days, end=date)
    vals = []
    for r in rows:
        if (r.get("date") or "") == date:
            continue
        v = num(r.get("calories_total"))
        if v and v > 800:      # 半天数据通常只有几百，混进均值会把目标拉低
            vals.append(v)
    if not vals:
        return None, 0
    return sum(vals) / len(vals), len(vals)


def _train_base(store, date: str) -> tuple[float, float, float]:
    """碳水档位的依据：近 4 周日均训练分钟，本周明显加量时改用本周。

    只看 7 天会被「恢复周」骗到（一天没练就把档位压到 3.5），而只看 4 周又会在
    加量周偏保守。所以两者都比一遍：本周日均超过 4 周基线的 1.5 倍才算真的加量。
    """
    end = dt.date.fromisoformat(date)

    def avg(days: int) -> float:
        rows = store.activities((end - dt.timedelta(days=days - 1)).isoformat(), date)
        return sum((num(a.get("duration_s")) or 0) for a in rows) / 60 / days

    a28, a7 = avg(28), avg(7)
    return (a7 if a7 > a28 * 1.5 else a28), a28, a7


def _carb_factor(avg_min: float) -> tuple[float, str]:
    if avg_min < 22:
        return 3.5, "日均训练不足 25 分钟"
    if avg_min < 55:
        return 4.5, "日均训练 25~55 分钟"
    if avg_min < 90:
        return 5.5, "日均训练 55~90 分钟"
    return 6.5, "日均训练 90 分钟以上"


def targets(store, date: str, res: dict, goal: str | None = None) -> dict:
    """算出当天热量与三大营养素目标，外加 2~4 条可执行建议。"""
    goal = goal if goal in GOALS else default_goal()
    g = GOALS.get(goal) or GOALS["cut"]
    bd = res.get("body") or {}
    w = num(bd.get("latest"))
    bmr = num(bd.get("bmr"))
    if not w:
        return {"ok": False, "goal": goal,
                "why": "还没有体重数据：先称一次（run.bat body --mi），再谈吃多少"}

    burn_base, n_days = _burn_base(store, date)
    estimated = burn_base is None
    if estimated:
        # 没有足够的日汇总时退回活动系数估算，并在文案里标明是估算
        burn_base = (bmr or 1500) * 1.4

    burn_today = num((res.get("today") or {}).get("calories_total"))
    gap = g["gap"]
    target = round(burn_base - gap)
    floor_hit = False
    if bmr and target < bmr:
        target = round(bmr)
        floor_hit = True

    p_g = round(w * (2.0 if (bd.get("muscle_delta") or 0) <= -0.4 else g["protein"]))
    train_base, train_28, train_7 = _train_base(store, date)
    cf, cf_reason = _carb_factor(train_base)
    c_g = round(w * cf)
    # 脂肪先按「总热量 22% 与 0.6 g/kg 取大」定下限：减脂期常见错误是把脂肪压到
    # 20 g 以内去凑热量，短期掉的是水和激素水平，不是脂肪。
    fat_g = max(round(w * 0.6), round(target * 0.22 / 9))
    c_g = max(0, round((target - p_g * 4 - fat_g * 9) / 4))

    p_kcal, c_kcal, f_kcal = p_g * 4, c_g * 4, fat_g * 9

    # 速率校准：目标 −0.5% 体重/周；4 周几乎没动就不建议再加缺口
    goal_w = round(-0.005 * w, 2)
    actual = bd.get("delta_w")
    d4w = bd.get("delta_4w")
    rate = "unknown"
    if actual is not None:
        if d4w is not None and abs(d4w) < 0.5 and actual < goal_w - 0.25:
            rate = "noise"
        elif actual < goal_w - 0.25:
            rate = "fast"
        elif actual > goal_w + 0.25:
            rate = "slow"
        else:
            rate = "ok"

    advice: list[str] = []
    if rate == "noise":
        advice.append(f"这周 {actual:+.1f} kg，但 {bd.get('delta_4w_days') or 28} 天累计只有 "
                      f"{d4w:+.1f} kg：属于水分波动，别为了数字再减饭")
    elif rate == "fast":
        advice.append(f"这周 {actual:+.1f} kg，比目标 {goal_w:+.2f} kg/周 快；"
                      f"缺口先别再加大，观察 4 周趋势")
    elif rate == "slow":
        advice.append(f"这周 {actual:+.1f} kg，慢于目标 {goal_w:+.2f} kg/周；"
                      f"先把蛋白和睡眠补足，再考虑减 100~150 kcal")
    elif rate == "ok":
        advice.append(f"这周 {actual:+.1f} kg，节奏符合目标 {goal_w:+.2f} kg/周")

    advice.append(f"蛋白 {p_g} g 是保肌肉的下限（{p_g / w:.1f} g/kg），"
                  f"分 3~4 次吃，训练后 30 分钟内先补 20~30 g")
    if (bd.get("muscle_delta") or 0) <= -0.4:
        advice.append(f"肌肉 {bd.get('muscle_delta_span') or 14} 天 {bd['muscle_delta']:+.1f} kg："
                      f"蛋白已提到 2.0 g/kg，再加 1 次力量训练")
    if floor_hit:
        advice.append(f"按缺口算出的摄入低于 BMR {int(bmr)} kcal，已按下限抬回；"
                      f"长期吃不到 BMR 会先掉肌肉")
    if estimated:
        advice.append("库里还没有完整的日消耗记录，热量基线是估算值，同步 3~5 天后会更准")

    eat = intake(date)
    advice_i = []
    if eat["ok"]:
        left = target - eat["kcal"]
        advice_i.append(f"今天已记 {eat['kcal']} kcal（{eat.get('count', len(eat['items']))} 餐），"
                        + (f"离目标还差 {left} kcal" if left > 0 else f"已超目标 {abs(left)} kcal")
                        + (f"；蛋白已 {eat['protein_g']} g / {p_g} g" if eat["protein_g"] else ""))

    return {
        "ok": True,
        "goal": goal,
        "goal_label": g["label"],
        "goal_note": g["note"],
        "weight": round(w, 1),
        "bmr": int(bmr) if bmr else None,
        "burn_base": round(burn_base),
        "burn_days": n_days,
        "burn_today": burn_today,
        "estimated": estimated,
        "gap": gap,
        "target_kcal": target,
        "intake": eat,
        "left_kcal": (target - eat["kcal"]) if eat["ok"] else None,
        "floor_hit": floor_hit,
        "protein_g": p_g,
        "carb_g": c_g,
        "fat_g": fat_g,
        "protein_kcal": round(p_kcal),
        "carb_kcal": round(c_kcal),
        "fat_kcal": round(f_kcal),
        "carb_factor": cf,
        "carb_reason": cf_reason,
        "avg_train_min": round(train_base),
        "train_28": round(train_28),
        "train_7": round(train_7),
        "goal_kg_week": goal_w,
        "actual_kg_week": actual,
        "rate": rate,
        "advice": advice_i + advice,
    }


def pct_split(t: dict) -> tuple[int, int, int]:
    """三大营养素的热量占比，用于看板上的配比条。"""
    total = (t.get("protein_kcal") or 0) + (t.get("carb_kcal") or 0) + (t.get("fat_kcal") or 0)
    if not total:
        return 0, 0, 0
    p = round(t["protein_kcal"] / total * 100)
    c = round(t["carb_kcal"] / total * 100)
    return p, c, max(0, 100 - p - c)


def text(t: dict) -> str:
    """给微信简报 / 教练上下文的一段纯文本。"""
    if not t.get("ok"):
        return "营养：" + (t.get("why") or "数据不足")
    lines = [
        f"今日营养（{t['goal_label']}：{t['goal_note']}）",
        f"- 消耗基线 {t['burn_base']} kcal"
        f"（近 {t['burn_days']} 天日均{'，估算' if t['estimated'] else ''}）"
        f"｜今日已消耗 {round(t['burn_today']) if t.get('burn_today') else '—'}",
        f"- 目标摄入 {t['target_kcal']} kcal"
        f"（缺口 {abs(t['gap'])} kcal" + ("，已抬到 BMR" if t.get("floor_hit") else "") + "）",
        f"- 蛋白 {t['protein_g']} g / 碳水 {t['carb_g']} g / 脂肪 {t['fat_g']} g"
        f"（碳水 {t['carb_factor']} g/kg，{t['carb_reason']}）",
    ]
    lines += ["- " + a for a in t.get("advice") or []]
    return "\n".join(lines)
