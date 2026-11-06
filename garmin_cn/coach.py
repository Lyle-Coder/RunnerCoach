"""电子教练引擎：规则给出「今日怎么练」，并把结构化上下文交给模型写点评。

规则是可见、可改的——你完全可以按自己的训练哲学替换 SESSION_RULES。
"""
from __future__ import annotations

import datetime as dt

from .metrics import num
from . import plan as plan_mod


def _today_acts(res: dict) -> list[dict]:
    """今天的活动记录（用于判断是不是已经练过了）。"""
    date = (res.get("date") or "")[:10]
    return [a for a in (res.get("activities") or []) if (a.get("date") or "")[:10] == date]


def _race_today(res: dict) -> dict | None:
    """本地 105K 备赛课表的今日课次。

    这份课表才是「以此为准」的那一份（看板上的执行卡、今日课表都来自它），
    佳明教练里那个计划只是参考，而且随时可能被暂停成「全周休息」。
    所以处方先读本地课表，读不到再退回佳明计划。
    """
    from . import raceplan

    if not raceplan.available():
        return None
    try:
        return raceplan.day_info(dt.date.fromisoformat((res.get("date") or "")[:10]))
    except Exception:
        return None


def _plan_today(res: dict) -> tuple[bool, str, str]:
    """读今天在佳明教练计划里的安排。

    返回 (是否休息日, 今日课表文字, 下一课提示)。没有计划快照时全部为空。
    """
    plan = res.get("plan") or {}
    if not plan.get("available"):
        return False, "", ""

    today = plan.get("today") or {}
    items = [x for x in (today.get("items") or []) if not x.get("rest")]

    nxt_date = plan.get("next_date")
    nxt_items = [x for x in (plan.get("next_items") or []) if not x.get("rest")]
    hint = ""
    if nxt_date and nxt_items:
        first = nxt_items[0]
        name = first.get("name") or "训练"
        mins = f" {first['minutes']} 分钟" if first.get("minutes") else ""
        hint = f"下一次训练是 {nxt_date[5:]}（{name}{mins}）"

    if items:
        return False, "；".join(plan_mod.item_line(x) for x in items), hint
    return True, "", hint


def _fmt(v, unit="", digits=0):
    if v is None:
        return "—"
    if digits == 0:
        return f"{v:.0f}{unit}"
    return f"{v:.{digits}f}{unit}"


def pick_session(res: dict) -> dict:
    """根据恢复度 / TSB / ACWR / 睡眠债给出今天的训练处方。"""
    s = res["readiness"]["score"]
    tsb = res["load"]["tsb"]
    acwr = res["load"]["acwr"]
    debt = res["sleep"]["debt_h"] or 0

    if s is None:
        return {
            "type": "数据不足", "duration": "—", "intensity": "—",
            "why": "今天还没有同步到足够的生理数据，先按体感安排，建议同步后再看。",
        }

    plan_rest, plan_text, plan_next = _plan_today(res)

    done = _today_acts(res)
    done_min = sum((num(a.get("duration_s")) or 0) for a in done) / 60
    # 距离只算跑步：骑行、力量也记时长，但混进 km 会得出「今天已跑 8 km」的假数
    done_km = sum((num(a.get("distance_m")) or 0) for a in done
                  if (a.get("type") or "") == "running") / 1000
    done_txt = ""
    if done_min >= 5:
        done_txt = f"另外，今天已记录 {done_min:.0f} 分钟"
        if done_km >= 0.1:
            done_txt += f" / {done_km:.2f} km"
        done_txt += "，就不用再叠加了。"

    parts = {p["name"]: p for p in (res["readiness"].get("parts") or [])}
    hrv_score = (parts.get("HRV") or {}).get("score")
    soft = (s < 75 or (acwr or 0) > 1.5 or debt >= 6
            or (hrv_score is not None and hrv_score < 70))

    race = _race_today(res)

    # 今天到底练没练，是排在最前面的一件事。
    # 之前只看恢复度：跑完 10.8 km 之后状态分自然被拉低，于是看板还在说
    # 「完全休息，课表建议改期」——等于把已经完成的训练当没发生。
    done_km_txt = f"{done_km:.2f} km" if done_km >= 0.1 else ""
    done_full = f"{done_min:.0f} 分钟" + (f" / {done_km_txt}" if done_km_txt else "")
    tgt_km = num((race or {}).get("km")) or 0
    tgt_min = num((race or {}).get("minutes")) or 0
    if race and race.get("is_rest"):
        finished = done_min >= 25
    else:
        finished = done_min >= 10 and (
            (tgt_km > 0 and done_km >= tgt_km * 0.9)
            or (tgt_min > 0 and done_min >= tgt_min * 0.9)
            or (tgt_km <= 0 and tgt_min <= 0 and done_min >= 30)
        )

    if finished and not (race and race.get("is_race")):
        if race and race.get("is_rest"):
            return {
                "type": "休息日加练 · 补恢复",
                "duration": f"已练 {done_full}",
                "intensity": "Z1",
                "why": f"课表今天是休息日，实际练了 {done_full}。练都练了不用自责，"
                       "但明天别再叠加：睡前补碳水、做 10 分钟小腿和髋的放松，"
                       "把这次加练当成「提前支取」的恢复额度。",
                "source": "raceplan",
            }
        plan_txt = (f"课表{race['abbr']}" if race and race.get("abbr")
                    else (f"课表{plan_text}" if plan_text else "今天没有排课"))
        tgt_txt = ""
        if tgt_km > 0 or tgt_min > 0:
            tgt_txt = "（原排 " + " / ".join(x for x in [
                f"{tgt_km:g} km" if tgt_km else "", f"{tgt_min:.0f} 分钟" if tgt_min else ""] if x) + "）"
        tail = ("恢复度不高是这次训练的结果，不是「今天不该练」的理由——"
                "接下来只做恢复，别再补跑。" if s < 75 else
                "剩下的时间交给恢复：补水、补碳水、拉伸 10 分钟。")
        return {
            "type": "课表已完成 · 放松恢复",
            "duration": f"已完成 {done_full}",
            "intensity": "—",
            "why": f"{plan_txt}{tgt_txt}今天已经完成 {done_full}，不用再叠加训练量。{tail}",
            "source": "raceplan" if race else "garmin_plan",
        }

    if s < 60 or tsb < -25:
        tail = f"课表原排{race['abbr']}，" if race and not race.get("is_rest") else (
            f"佳明教练今天原排{plan_text}，" if plan_text else "")
        return {
            "type": "完全休息 / 主动恢复", "duration": "20–30 分钟", "intensity": "Z1",
            "why": "恢复度低或疲劳积累明显，今天的目标是让身体回到基线，而不是完成训练量。"
                   "可选散步、泡沫轴、髋踝活动度。" + (f"{tail}建议改期或降级。" if tail else "")
                   + (done_txt or ""),
            "source": "raceplan" if race else "",
        }

    # 本地备赛课表优先：它就是执行卡和看板上「以此为准」的那一份，
    # 佳明教练计划只当参考（被暂停时那份快照会整周全标休息，直接采信就错了）。
    if race and race.get("is_rest"):
        return {
            "type": "按计划休息", "duration": "0–20 分钟", "intensity": "Z1",
            "why": f"备赛课表今天是休息日（W{race['week']} {race['phase']}期），"
                   f"恢复度 {s} 只说明「可以练」，不代表「应该练」。"
                   "休息是计划的一部分，别自己加练；"
                   + (f"{race['tip']}。" if race.get("tip") else "可做 10–15 分钟活动度、泡沫轴。")
                   + (done_txt or ""),
            "source": "raceplan",
        }

    if race and race.get("is_race"):
        detail = (race.get("note") or race.get("tip") or "").strip()
        return {
            "type": f"{race.get('abbr') or '比赛'} · 比赛日",
            "duration": (race.get("sub") or (f"{int(race['minutes'])} 分钟" if race.get("minutes") else "—")),
            "intensity": "比赛强度",
            "why": "今天是目标赛事，赛前只做热身与激活，不要加任何额外训练量。"
                   + (f"课表说明：{detail}。" if detail else "")
                   + (f"恢复度 {s}" + (f"、HRV 分项 {hrv_score}" if hrv_score else "")
                      + "，状态够用就别在赛前改计划。" if s >= 75 else
                      f"恢复度 {s} 偏低，赛前把目标放保守一档，前程宁慢勿快。"),
            "source": "raceplan",
        }

    if race:
        abbr = race.get("abbr") or race.get("title") or "训练"
        kind_cn = race.get("title") or ""
        mins = int(race.get("minutes") or 0)
        hr = (race.get("hr") or "").strip()
        detail = (race.get("note") or race.get("tip") or "").strip()
        head = f"{abbr}" + (f" · {kind_cn}" if kind_cn and kind_cn not in abbr else "")
        # 状态一般时不动配速要求，只把量往下收一档，并在依据里写清楚为什么
        dur_txt = f"{mins} 分钟" if mins else (race.get("dur") or "—")
        why_bits = []
        if soft:
            low = max(20, mins - 15) if mins else 0
            if low and low < mins:
                dur_txt = f"{low}–{mins} 分钟"
            reasons = []
            if hrv_score is not None and hrv_score < 70:
                reasons.append("HRV 还在基线下")
            if debt >= 6:
                reasons.append(f"睡眠缺口 {debt:.1f} h")
            if s < 75:
                reasons.append(f"恢复度只有 {s}")
            why_bits.append(("状态一般（" + "、".join(reasons) + "），" if reasons else "状态一般，")
                            + "先按短的一端跑，全程守住心率上限，不追配速。")
        else:
            why_bits.append("状态在线，按课表原样执行。")
        if detail:
            why_bits.append(f"课表说明：{detail}。")
        if mins and done_min >= 5:
            left = max(0, mins - done_min)
            why_bits.append(
                f"今天已记录 {done_min:.0f} 分钟"
                + (f" / {done_km:.2f} km" if done_km >= 0.1 else "")
                + (f"，课表还差约 {left:.0f} 分钟。" if left >= 5 else "，课表的量已经够了，剩下的时间做放松。"))
        elif done_txt:
            why_bits.append(done_txt)
        return {
            "type": head,
            "duration": dur_txt,
            "intensity": hr if "Z" in hr.upper() else (f"Z2 ≤{hr}" if hr else (race.get("sub") or "Z2")),
            "why": "".join(why_bits),
            "source": "raceplan",
        }

    if plan_rest:
        return {
            "type": "按计划休息", "duration": "0–20 分钟", "intensity": "Z1",
            "why": f"佳明教练今天排的是休息日，恢复度 {s} 只说明「可以练」，不代表「应该练」。"
                   "休息是计划的一部分，别自己加练；可做 10–15 分钟活动度、泡沫轴。"
                   + (f"{plan_next}，把力气留到那天。" if plan_next else "")
                   + (done_txt or ""),
            "source": "garmin_plan",
        }
    if s < 75 or (acwr or 0) > 1.5 or debt >= 6:
        extra = "，另外近 7 天睡眠缺口偏大" if debt >= 6 else ""
        plan_hint = f"今天计划是{plan_text}，建议按状态降一档执行。" if plan_text else ""
        return {
            "type": "轻松有氧", "duration": "30–50 分钟", "intensity": "Z1–Z2",
            "why": "状态一般" + extra + "。用低强度维持有氧基础，全程保持能正常说话的强度，"
                   "心率不超 Z2 上限；今天不适合加码。" + plan_hint,
        }
    if s < 88:
        return {
            "type": "常规训练", "duration": "50–70 分钟", "intensity": "Z2 + 少量 Z3",
            "why": "恢复良好，可以安排正常训练量；主体放在有氧，末尾加 2–3 组短爬坡或节奏段。"
                   + (f"今天计划是{plan_text}。" if plan_text else ""),
        }
    if tsb > 8:
        return {
            "type": "质量课", "duration": "60–75 分钟", "intensity": "Z3–Z4",
            "why": "恢复度和新鲜度都到位，这是本周最适合上强度的窗口，好好利用。"
                   + (f"今天计划是{plan_text}。" if plan_text else ""),
        }
    return {
        "type": "中等强度训练", "duration": "50–70 分钟", "intensity": "Z2–Z3",
        "why": "身体状态在线，按计划推进即可，注意把强度控制在能完成全程的程度。"
               + (f"今天计划是{plan_text}。" if plan_text else ""),
    }


def blocks(res: dict) -> list[dict]:
    out = []
    r = res["readiness"]
    parts = {p["name"]: p for p in r["parts"]}

    if "HRV" in parts:
        out.append({
            "title": "HRV",
            "text": f"{parts['HRV']['detail']}，自主神经状态{'在基线上' if parts['HRV']['score'] >= 95 else ('略偏紧' if parts['HRV']['score'] >= 70 else '偏紧')}。",
        })
    if "静息心率" in parts:
        out.append({
            "title": "静息心率",
            "text": f"{parts['静息心率']['detail']}，{'不高于' if parts['静息心率']['score'] >= 99 else '高出'}个人基线。",
        })

    sl = res["sleep"]
    if sl["total_min"]:
        text = f"昨晚睡了 {sl['total_h']} 小时"
        if sl["score"]:
            text += f"，评分 {sl['score']:.0f}"
        if sl["debt_h"] and sl["debt_h"] > 0:
            text += f"；近 7 天累计缺口 {sl['debt_h']} 小时"
        out.append({"title": "睡眠", "text": text + "。"})

    ld = res["load"]
    out.append({
        "title": "训练负荷",
        "text": f"急性负荷 {ld['acute_7d']}，慢性负荷 {ld['chronic_28d']}，ACWR {ld['acwr'] or '—'}（{ld['acwr_state']}）；"
                f"CTL {ld['ctl']} / ATL {ld['atl']} / TSB {ld['tsb']}。",
    })
    return out


def brief(res: dict) -> dict:
    session = pick_session(res)
    r = res["readiness"]
    return {
        "date": res["date"],
        "headline": f"{r['level']} · 恢复度 {r['score'] if r['score'] is not None else '—'}",
        "session": session,
        "blocks": blocks(res),
        "flags": res["flags"],
    }


def context_markdown(res: dict) -> str:
    """给模型看的紧凑上下文。写点评时优先引用这里的数字，不要自己编。"""
    d = res["date"]
    t = res["today"]
    r = res["readiness"]
    L = res["load"]
    sl = res["sleep"]

    lines = [f"# Garmin 数据上下文 · {d}", ""]
    lines.append(f"恢复度：{r['score']}（{r['level']}）")
    for p in r["parts"]:
        lines.append(f"- {p['name']}：{p['detail']}（{p['score']}/100）")
    lines.append("")
    lines.append("## 今日指标")
    lines.append(f"- 步数 {_fmt(t.get('steps'))}｜静息心率 {_fmt(t.get('resting_hr'),' bpm')}｜"
                 f"最高心率 {_fmt(t.get('max_hr'),' bpm')}")
    lines.append(f"- 睡眠 {_fmt(sl['total_min'] and sl['total_min']/60, ' h', 1)}（深睡 {_fmt(sl['deep_min'])} 分，"
                 f"REM {_fmt(sl['rem_min'])} 分，清醒 {_fmt(sl['awake_min'])} 分），睡眠债 {sl['debt_h']} h")
    if sl.get("nap_min"):
        lines.append(f"- 昨日小睡 {sl['nap_min']} 分（已计入睡眠债抵扣）")
    if (sl.get("debt") or {}).get("need_avg_min"):
        lines.append(f"- 佳明动态睡眠需求均值 {_fmt(sl['debt']['need_avg_min']/60, ' h', 1)}/晚"
                     f"（缺口已按此口径计算，非固定 8 小时）")
    lines.append(f"- 身体电量 {_fmt(t.get('bb_high'))}/{_fmt(t.get('bb_low'))}｜"
                 f"压力 {_fmt(t.get('avg_stress'))}｜SpO2 {_fmt(t.get('spo2_avg'),'%')}｜"
                 f"呼吸 {_fmt(t.get('resp_avg'),' 次/分')}")
    lines.append(f"- HRV {_fmt(t.get('hrv_night'),' ms')}｜周均 {_fmt(t.get('hrv_weekly'),' ms')}｜状态 {t.get('hrv_status') or '—'}")
    bt = res.get("body") or {}
    if bt.get("latest"):
        from . import body as body_mod

        lines += ["", "## 体重与体成分"]
        lines += ["- " + x for x in body_mod.text(bt, bt.get("ctl_delta")).splitlines()]

    # 营养目标：模型写点评时经常顺手给饮食建议，这里把算好的数字摆出来，
    # 免得它自己拿体重乘以一个想当然的系数。
    nt = res.get("nutrition") or {}
    if nt.get("ok"):
        from . import nutrition as nutri_mod

        body_lines = nutri_mod.text(nt).splitlines()
        lines += ["", "## 营养参考（已按体重与训练量算好）"]
        # text() 的每一行本身已带 "- "，别再加一次
        lines += [x if x.lstrip().startswith("-") else "- " + x
                  for x in body_lines[1:]]
    lines.append("")
    lines.append("## 训练负荷")
    lines.append(f"- 7 天负荷 {L['acute_7d']}｜28 天负荷 {L['chronic_28d']}｜ACWR {L['acwr']}（{L['acwr_state']}）")
    lines.append(f"- CTL {L['ctl']}｜ATL {L['atl']}｜TSB {L['tsb']}")

    wk = res["week"]

    def _w(key, label, unit=""):
        x = wk.get(key) or {"now": None, "delta": None}
        if x["now"] is None:
            return f"- {label}：数据不足"
        delta = f"，较上周 {x['delta']:+}" if x.get("delta") is not None else ""
        return f"- {label}：本周均值 {x['now']}{unit}{delta}"

    lines.append("")
    lines.append("## 周对比")
    lines.append(_w("sleep_h", "睡眠", " 分钟/晚"))
    lines.append(_w("hrv", "HRV", " ms"))
    lines.append(_w("rhr", "静息心率", " bpm"))
    lines.append("")
    lines.append("## 最近活动")
    for a in res["activities"][:6]:
        dur = num(a.get("duration_s"), 0) / 60
        dist = num(a.get("distance_m"), 0) / 1000
        pace = a.get("pace_s_km")
        pace_s = ""
        if pace:
            pace_s = f"，配速 {int(pace)//60}'{int(pace)%60:02d}\"/km"
        lines.append(f"- {a.get('date')} {a.get('type')} {a.get('name') or ''}：{dur:.0f} 分钟，"
                     f"{dist:.2f} km，均心率 {_fmt(a.get('avg_hr'))}{pace_s}")
    if not res["activities"]:
        lines.append("- 近 5 周无记录")

    lines.append("")
    lines.append("## 规则引擎给出今日处方")
    s = brief(res)["session"]
    lines.append(f"{s['type']}｜{s['duration']}｜{s['intensity']}")
    lines.append(f"依据：{s['why']}")
    lines.append("")
    lines.append("## 风险标记")
    for f in res["flags"]:
        lines.append(f"- [{f['level']}] {f['text']}")

    race = _race_today(res)
    if race:
        from . import raceplan as _rp

        lines.append("")
        lines.append(f"## 本地备赛课表（以此为准）W{race['week']} {race['phase']}期 · {race['focus']}")
        lines.append(f"- 今日课次：{race['abbr']}（{race['dur']}，心率 {race.get('hr') or '—'}）"
                     + (f"｜{race['note']}" if race.get("note") else ""))
        for r in _rp.week_rows(dt.date.fromisoformat(res["date"][:10])):
            lines.append(f"- {r['date']} {r['dow']}：{r['main']}"
                         + (f" · {r['sub']}" if r.get("sub") else ""))

    plan = res.get("plan")
    if plan and plan.get("available"):
        from .plan import item_line

        ph = plan.get("phase") or {}
        lines.append("")
        lines.append(f"## 佳明教练计划：{plan.get('name')}"
                     + (f"（{ph.get('label')}，{ph.get('start')} ~ {ph.get('end')}）" if ph else ""))
        lines.append(f"计划：{plan.get('weeks')} 周，每周约 {plan.get('per_week')} 次，"
                     f"{plan.get('start')} ~ {plan.get('end')}")
        for d in plan.get("week") or []:
            if d.get("rest"):
                lines.append(f"- {d['date']} {d['dow']}：休息")
            else:
                lines.append(f"- {d['date']} {d['dow']}：" + "；".join(item_line(x) for x in d["items"]))
        t = plan.get("today") or {}
        if t.get("rest"):
            lines.append("今日教练安排：休息日，没有指定训练。")
        else:
            lines.append("今日教练安排：" + "；".join(item_line(x) for x in t.get("items") or []))
        lines.append("说明：今日处方优先取本地 105K 备赛课表（与执行卡、看板同源）；"
                     "课表休息日就休息，不因恢复度尚可而加练；状态一般时只收量、不动配速要求，"
                     "并在依据里写清原因。佳明教练计划只作参考——它被暂停时整周都会标成休息，不能直接采信。")

    return "\n".join(lines)


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")
