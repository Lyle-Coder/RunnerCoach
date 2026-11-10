# -*- coding: utf-8 -*-
"""每周日校准：读恢复指标 + 上周执行度，决定下周课表怎么调。

用法：
    python tools/weekly_review.py              # 评估「下一周」，打印结论
    python tools/weekly_review.py --week 5     # 指定评估第 5 周
    python tools/weekly_review.py --write      # 同时输出 HTML 报告到 output/
    python tools/weekly_review.py --json       # 机读输出（给定时任务用）

判定逻辑（三个开关，,任何一个触发就降级，不会互相抵消）：
    A 负荷开关：近 7 天 HRV 均值 < 28 天基线 90%，或连续 ≥3 天 LOW，或静息心率
               比基线高 5 次以上  → 下周量 ×0.85，质量课降为轻松跑
    B 强度开关：上周「轻松跑」平均心率超过目标 8 次以上  → 提示收紧配速，不改量
    C 完成度开关：上周实际跑量 < 计划 70%  → 下周不加量，维持原样
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import sqlite3
import statistics as st
import sys
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOME))

from garmin_cn import raceplan  # noqa: E402

DB = HOME / "data" / "garmin.db"

# 轻松跑的心率兜底上限（与 workout.EASY_HR 一致）
EASY_HR_CAP = 166


def _q(sql: str, args: tuple = ()) -> list[tuple]:
    if not DB.exists():
        return []
    con = sqlite3.connect(DB)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


# ---------------- 恢复指标 ----------------
def recovery(today: dt.date) -> dict:
    d28 = (today - dt.timedelta(days=27)).isoformat()
    d7 = (today - dt.timedelta(days=6)).isoformat()
    rows = _q("""select date, hrv_night, hrv_weekly, hrv_status, resting_hr,
                        sleep_total_min, sleep_score, bb_high
                 from daily where date >= ? and date <= ? order by date""",
              (d28, today.isoformat()))
    if not rows:
        return {"ok": False}
    hrv7 = [r[1] for r in rows if r[0] >= d7 and r[1]]
    hrv28 = [r[1] for r in rows if r[1]]
    rhr7 = [r[4] for r in rows if r[0] >= d7 and r[4]]
    rhr28 = [r[4] for r in rows if r[4]]
    sleep7 = [r[5] for r in rows if r[0] >= d7 and r[5]]
    low_streak = 0
    for r in reversed(rows):
        if (r[3] or "").upper() == "LOW":
            low_streak += 1
        else:
            break
    return {
        "ok": True,
        "hrv7": round(st.mean(hrv7), 1) if hrv7 else None,
        "hrv28": round(st.mean(hrv28), 1) if hrv28 else None,
        "rhr7": round(st.mean(rhr7), 1) if rhr7 else None,
        "rhr28": round(st.mean(rhr28), 1) if rhr28 else None,
        "sleep7_h": round(st.mean(sleep7) / 60.0, 1) if sleep7 else None,
        "low_streak": low_streak,
        "last_status": (rows[-1][3] or "").upper(),
        "last_date": rows[-1][0],
    }


# ---------------- 上周执行度 ----------------
def execution(week_no: int) -> dict:
    mp = raceplan._mp()
    if not (1 <= week_no <= len(mp.WEEKS)):
        return {"ok": False}
    w = mp.WEEKS[week_no - 1]
    start = mp.date_of(week_no, 0)
    end = mp.date_of(week_no, 6)

    plan_km = rounded = 0.0
    plan_days = 0
    easy_days = 0
    plan = []
    for i in range(7):
        info = raceplan.day_info(start + dt.timedelta(days=i))
        if not info:
            continue
        plan_km += info["km"] or 0
        if not info["is_rest"]:
            plan_days += 1
        if info["kind"] in ("easy", "str"):
            easy_days += 1
        plan.append(info)

    rows = _q("""select date, round(distance_m/1000.0, 2), avg_hr, pace_s_km
                 from activities where type='running' and date >= ? and date <= ?
                 order by date""", (start.isoformat(), end.isoformat()))
    act_km = sum((r[1] or 0) for r in rows)
    by_date = {}
    for d, km, hr, pace in rows:
        a = by_date.setdefault(d, [0.0, []])
        a[0] += km or 0
        if hr:
            a[1].append(hr)

    # 轻松跑心率：找出计划是 easy/str 的日子，看实际均心率
    easy_hr = []
    for info in plan:
        if info["kind"] in ("easy", "str") and info["date"] in by_date:
            hrs = by_date[info["date"]][1]
            if hrs:
                easy_hr.append(st.mean(hrs))

    ran_days = len(by_date)
    has_race = any(p["is_race"] for p in plan)
    return {
        "ok": True,
        "has_race": has_race,
        "week": week_no,
        "range": [start.isoformat(), end.isoformat()],
        "plan_km": round(plan_km, 1),
        "act_km": round(act_km, 1),
        "plan_days": plan_days,
        "ran_days": ran_days,
        "easy_hr_avg": round(st.mean(easy_hr), 1) if easy_hr else None,
        "easy_days": easy_days,
        "focus": w["focus"],
        "phase": w["phase"],
    }


# ---------------- 三开关 ----------------
def decide(rec: dict, ex: dict) -> dict:
    reasons = []
    downgrade = 1.0
    quality_off = False

    # 赛后半马的 HRV 必然掉，这时候拿它判「过训」会误伤——只要上周有比赛就跳过降级判定
    post_race = bool(ex.get("ok") and ex.get("has_race"))
    if rec.get("ok") and not post_race:
        hrv7, hrv28 = rec.get("hrv7"), rec.get("hrv28")
        rhr7, rhr28 = rec.get("rhr7"), rec.get("rhr28")
        if hrv7 and hrv28 and hrv7 < hrv28 * 0.90:
            downgrade = min(downgrade, 0.85)
            reasons.append("HRV 7 日均 %.0f，比 28 天基线 %.0f 低 %.0f%%"
                           % (hrv7, hrv28, (1 - hrv7 / hrv28) * 100))
        if rec.get("low_streak", 0) >= 3:
            downgrade = min(downgrade, 0.85)
            reasons.append("HRV 状态连续 %d 天 LOW" % rec["low_streak"])
        if rhr7 and rhr28 and rhr7 >= rhr28 + 5:
            downgrade = min(downgrade, 0.85)
            reasons.append("静息心率 %.0f，比基线 %.0f 高 %.0f 次" % (rhr7, rhr28, rhr7 - rhr28))
        if rec.get("sleep7_h") and rec["sleep7_h"] < 6.5:
            reasons.append("近 7 天日均睡眠只有 %.1f h" % rec["sleep7_h"])

    if downgrade < 1.0:
        quality_off = True

    pace_warn = None
    if ex.get("ok") and ex.get("easy_hr_avg"):
        if ex["easy_hr_avg"] > EASY_HR_CAP + 8:
            pace_warn = "上周轻松跑均心率 %.0f，比上限 %d 高 %.0f 次——不是降量问题，是跑太快"
            pace_warn = pace_warn % (ex["easy_hr_avg"], EASY_HR_CAP,
                                     ex["easy_hr_avg"] - EASY_HR_CAP)

    hold = False
    if ex.get("ok") and ex.get("plan_km"):
        ratio = ex["act_km"] / ex["plan_km"]
        if ratio < 0.70:
            hold = True
            reasons.append("上周只完成计划的 %.0f%%（%.0f / %.0f km）"
                           % (ratio * 100, ex["act_km"], ex["plan_km"]))

    if post_race:
        reasons.append("上周有比赛，HRV 下滑属于正常赛后反应，不据此降量")
    if downgrade < 1.0:
        advice = "下周量 ×%.2f（%s），质量课改为轻松跑，爬升同步按比例降" % (
            downgrade, ex.get("phase") or "")
    elif hold:
        advice = "下周不加量：按原计划执行，先把完成度拉回 85% 以上"
    else:
        advice = "按原计划执行，不用改"

    return {"scale": downgrade, "quality_off": quality_off,
            "pace_warn": pace_warn, "hold": hold,
            "reasons": reasons, "advice": advice}


# ---------------- 输出 ----------------
def weeks_for(today: dt.date) -> tuple[int, int]:
    """返回 (被评估的上一周, 要调整的目标周)。

    周日跑 = 本周刚结束，评估本周、调下一周；
    周中跑 = 本周还没结束，评估上一周、调本周剩下的部分。
    """
    mp = raceplan._mp()
    n = len(mp.WEEKS)
    w, _ = raceplan.week_of(today)
    if w is None:
        return (1, 1) if today < mp.WEEK1 else (n, n)
    no = w["no"]
    if today.weekday() == 6:
        return no, min(n, no + 1)
    return max(1, no - 1), no


def render_text(rec, ex, dec, target_week) -> str:
    out = []
    out.append("恢复状态（近 7 天 / 28 天基线）")
    if rec.get("ok"):
        out.append("  HRV      %s / %s　状态 %s（连续 LOW %d 天）"
                   % (rec["hrv7"], rec["hrv28"], rec["last_status"], rec["low_streak"]))
        out.append("  静息心率 %s / %s" % (rec["rhr7"], rec["rhr28"]))
        out.append("  睡眠     %s h/天" % rec["sleep7_h"])
    else:
        out.append("  （本地没有恢复数据，先跑 run.bat sync）")

    out.append("")
    out.append("上周执行（W%s）" % (ex.get("week") if ex.get("ok") else "?"))
    if ex.get("ok"):
        out.append("  计划 %.0f km / %d 天　实际 %.0f km / %d 天（%.0f%%）"
                   % (ex["plan_km"], ex["plan_days"], ex["act_km"], ex["ran_days"],
                      (ex["act_km"] / ex["plan_km"] * 100) if ex["plan_km"] else 0))
        out.append("  轻松跑均心率 %s（上限 %d）" % (ex["easy_hr_avg"], EASY_HR_CAP))
    else:
        out.append("  （没有可对照的周）")

    out.append("")
    out.append("判定 → 下一周 W%d" % target_week)
    out.append("  系数 ×%.2f　质量课%s" % (dec["scale"], "取消" if dec["quality_off"] else "照常"))
    for r in dec["reasons"]:
        out.append("  · " + r)
    if dec["pace_warn"]:
        out.append("  · " + dec["pace_warn"])
    out.append("  建议：" + dec["advice"])
    return "\n".join(out)


def render_html(rec, ex, dec, target_week) -> str:
    def row(k, v):
        return "<tr><td>%s</td><td class='num'>%s</td></tr>" % (k, v)

    rs = "".join([
        row("HRV 7 日均 / 28 天基线", "%s / %s" % (rec.get("hrv7"), rec.get("hrv28"))),
        row("HRV 状态", "%s（连续 LOW %s 天）" % (rec.get("last_status") or "—",
                                                rec.get("low_streak", 0))),
        row("静息心率 7 日均 / 基线", "%s / %s" % (rec.get("rhr7"), rec.get("rhr28"))),
        row("睡眠 7 日均", "%s h" % rec.get("sleep7_h")),
    ]) if rec.get("ok") else "<tr><td colspan='2'>暂无数据</td></tr>"

    es = ""
    if ex.get("ok"):
        ratio = (ex["act_km"] / ex["plan_km"] * 100) if ex["plan_km"] else 0
        es = "".join([
            row("计划", "%.0f km / %d 天" % (ex["plan_km"], ex["plan_days"])),
            row("实际", "%.0f km / %d 天" % (ex["act_km"], ex["ran_days"])),
            row("完成度", "%.0f%%" % ratio),
            row("轻松跑均心率", "%s（上限 %d）" % (ex["easy_hr_avg"], EASY_HR_CAP)),
        ])

    why = "".join("<li>%s</li>" % r for r in dec["reasons"]) or "<li>各项指标在范围内</li>"
    warn = ("<p class='warn'>%s</p>" % dec["pace_warn"]) if dec["pace_warn"] else ""

    return """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>每周校准 %(tw)s</title><style>
body{font-family:-apple-system,"Segoe UI","PingFang SC",sans-serif;background:#f7f6f2;color:#1f1f1e;
  margin:0;padding:32px 20px 60px}
.wrap{max-width:760px;margin:0 auto}
h1{font-size:19px;font-weight:600;margin:0 0 4px}
.sub{font-size:12px;color:#8a877c;margin-bottom:22px}
h2{font-size:14px;font-weight:600;margin:26px 0 8px}
table{width:100%%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;
  font-size:13px}
td{padding:9px 14px;border-bottom:1px solid #eeece6}
td.num{text-align:right;font-variant-numeric:tabular-nums;color:#3a3935}
td:first-child{color:#5f5e5a}
.verdict{background:#fff;border-left:3px solid %(bc)s;border-radius:0 8px 8px 0;
  padding:14px 16px;margin:18px 0;font-size:13px;line-height:1.7}
.big{font-size:15px;font-weight:600;margin-bottom:6px}
ul{margin:6px 0 0 18px;padding:0;font-size:12.5px;color:#5f5e5a;line-height:1.7}
.warn{color:#a32d2d;font-size:12.5px;margin:8px 0 0}
</style></head><body><div class="wrap">
<h1>每周校准 · 目标周 W%(tw)d</h1>
<div class="sub">%(today)s 生成　·　数据源：佳明日指标 + 本地活动库</div>
<h2>恢复状态</h2><table>%(rs)s</table>
<h2>上周执行</h2><table>%(es)s</table>
<div class="verdict"><div class="big">下周量 ×%(scale).2f　·　质量课%(q)s</div>
<ul>%(why)s</ul>%(warn)s
<p style="margin:8px 0 0;font-size:12.5px">%(advice)s</p></div>
</div></body></html>""" % {
        "tw": target_week, "today": dt.date.today().isoformat(),
        "rs": rs, "es": es,
        "bc": "#a32d2d" if dec["scale"] < 1 else ("#ba7517" if dec["pace_warn"] else "#3b6d11"),
        "scale": dec["scale"], "q": "取消" if dec["quality_off"] else "照常",
        "why": why, "warn": warn, "advice": dec["advice"],
    }


OVERRIDES = HOME / "data" / "week_overrides.json"


def apply_override(target_week: int, dec: dict) -> Path:
    """把判定写进 data/week_overrides.json，推送时按它打折。恢复就撤掉覆盖。"""
    data = {}
    if OVERRIDES.exists():
        try:
            data = json.loads(OVERRIDES.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    key = str(target_week)
    if dec["scale"] < 1.0 or dec["quality_off"]:
        data[key] = {"scale": dec["scale"], "quality_off": dec["quality_off"],
                     "reasons": dec["reasons"],
                     "set_at": dt.date.today().isoformat()}
    else:
        data.pop(key, None)
    OVERRIDES.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDES.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return OVERRIDES


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int, help="评估哪一周（默认下一周）")
    ap.add_argument("--date", help="以哪天为今天")
    ap.add_argument("--write", action="store_true", help="写出 HTML 报告")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--apply", action="store_true",
                    help="把判定写进 data/week_overrides.json（推送时生效）")
    a = ap.parse_args(argv)

    today = dt.date.fromisoformat(a.date) if a.date else dt.date.today()
    last, target = weeks_for(today)
    if a.week:
        target = a.week
        last = max(1, target - 1)

    rec = recovery(today)
    ex = execution(last)
    dec = decide(rec, ex)

    if a.json:
        print(json.dumps({"recovery": rec, "execution": ex, "decision": dec,
                          "target_week": target}, ensure_ascii=False, indent=2))
    else:
        print(render_text(rec, ex, dec, target))

    if a.apply:
        p = apply_override(target, dec)
        print("\n已写入周覆盖：%s" % p)

    if a.write:
        p = HOME / "output" / ("review-%s.html" % today.isoformat())
        p.write_text(render_html(rec, ex, dec, target), encoding="utf-8")
        print("\n报告：%s" % p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
