"""Garmin 国区 MCP 服务（stdio JSON-RPC，无第三方依赖）。

注册方式：写进 ~/.workbuddy/mcp.json，然后在「连接器」里点信任启用。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import traceback
from pathlib import Path

HOME = Path(__file__).resolve().parent
sys.path.insert(0, str(HOME))

from garmin_cn.metrics import derive  # noqa: E402
from garmin_cn.store import Store  # noqa: E402

DB = Path(os.environ.get("GARMIN_COACH_DB") or (HOME / "data" / "garmin.db"))
OUT = HOME / "output"
PROTOCOL = "2024-11-05"

# 与 cli.RUN_TYPES 保持一致：这些类型才当「跑步」来出简报
RUN_TYPES = ("running", "trail_running", "treadmill_running", "track_running",
             "virtual_run", "indoor_running", "obstacle_run", "ultra_run")

TOOLS = [
    {
        "name": "garmin_status",
        "description": "查看佳明数据接入状态：是否已登录、库里有几天数据、上次同步时间。未登录时会提示先运行登录命令。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "garmin_sync",
        "description": "从 Garmin Connect 国区同步最近 N 天的健康与活动数据到本地库，并顺手拉一次小米秤的体重/体成分。返回同步范围、活动条数、体重条数与异常。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "同步最近多少天，默认 14", "default": 14},
                "extras": {"type": "boolean", "description": "是否额外拉取压力/身体电量明细（更慢）", "default": False},
                "body": {"type": "boolean", "description": "是否同时同步体重/体成分（小米秤），默认 true", "default": True},
                "body_days": {"type": "integer", "description": "体重回看天数，默认 30", "default": 30},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "garmin_today",
        "description": "读取某一天（默认今天）的结构化快照：恢复度、睡眠、HRV、静息心率、身体电量、压力、训练负荷、体重/体成分、规则给出的今日处方与营养目标（热量 + 蛋白/碳水/脂肪）。",
        "inputSchema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "YYYY-MM-DD，默认今天"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "garmin_coach_context",
        "description": "获取一份为写教练点评准备的完整上下文（Markdown）：指标、28 天基线、周对比、最近活动、负荷状态、风险标记。写点评前先调用它，不要自己猜数字。",
        "inputSchema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "YYYY-MM-DD，默认今天"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "garmin_save_note",
        "description": "把教练点评写回看板（保存到本地库，看板「教练点评」区域会显示）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "点评正文，支持换行"},
                "date": {"type": "string", "description": "归属日期 YYYY-MM-DD，默认今天"},
                "source": {"type": "string", "description": "来源标记，默认 workbuddy"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "garmin_report",
        "description": "生成当日 HTML 看板文件，返回本地路径（可直接打开预览）。",
        "inputSchema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "YYYY-MM-DD，默认今天"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "garmin_card",
        "description": "生成一图流图片看板（1080 宽竖版 PNG），把恢复度、分项、今日处方、负荷、本周课表、风险压进一张图，适合直接发到微信。返回本地 PNG 路径。",
        "inputSchema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "YYYY-MM-DD，默认今天"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "garmin_activity",
        "description": "单次活动的明细与活动图：抓分圈/1Hz 时序/轨迹/天气，修正佳明的单侧步频（乘 2 还原总步频）并用 speed/cadence 重算步幅，返回统计摘要，可选生成一张竖版活动图 PNG。传入 activity_id 或日期（默认该日最长的一次活动）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "activity_id": {"type": "string", "description": "活动 ID，优先使用"},
                "date": {"type": "string", "description": "YYYY-MM-DD，默认今天"},
                "card": {"type": "boolean", "default": False, "description": "是否生成活动图 PNG"},
                "refresh": {"type": "boolean", "default": False, "description": "忽略缓存重新抓取"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "garmin_trends",
        "description": "获取最近 N 天的指标趋势表（睡眠、HRV、静息心率、身体电量、压力、步数、每日负荷），用于分析趋势。",
        "inputSchema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "default": 14}},
            "additionalProperties": False,
        },
    },
    {
        "name": "garmin_plan",
        "description": "读取佳明教练（Garmin Run Coach 自适应计划）的训练计划与本周课表：计划名、当前阶段、逐日安排（名称、目标配速、时长、训练效果）。默认用本地快照，refresh=true 时联网刷新。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "refresh": {"type": "boolean", "description": "是否联网刷新计划快照，默认 false", "default": False},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "garmin_brief",
        "description": "把最近一次（或最近 N 次）跑步压成一段可直接发进微信的纯文本简报：日期/时段/地点、距离用时配速、心率步频步幅、主要益处、前后半程结论（掉速/心率漂移）、天气。排版不用 markdown（微信不渲染），可直接粘进聊天窗口。card=true 时顺带生成活动图并返回图片路径。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "activity_id": {"type": "string", "description": "活动 ID，优先使用"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "last": {"type": "integer", "default": 1, "description": "取最近几条，默认 1"},
                "any": {"type": "boolean", "default": False, "description": "不限运动类型（默认只要跑步）"},
                "card": {"type": "boolean", "default": False, "description": "是否生成活动图并返回路径"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "garmin_activities",
        "description": "列出最近 N 天的活动记录（类型、时长、距离、配速、心率、训练负荷）。",
        "inputSchema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "default": 30}},
            "additionalProperties": False,
        },
    },
]


def _store() -> Store:
    return Store(DB)


def _today(args) -> str:
    return args.get("date") or dt.date.today().isoformat()


def _client():
    """按已保存的会话建客户端；未登录时抛 RuntimeError。"""
    from garmin_cn.client import GarminCNClient
    from garmin_cn.net import Net

    session = HOME / "data" / "session.json"
    if not session.exists():
        raise RuntimeError("未登录，请先在终端运行：run.bat login")
    return GarminCNClient(Net(session, min_interval=1.2))


def fmt0(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.0f}"
    except (TypeError, ValueError):
        return str(v)


def tool_status(args) -> str:
    st = _store().stats()
    session = (HOME / "data" / "session.json").exists()
    cfg = HOME / "config.json"
    email = ""
    if cfg.exists():
        try:
            email = json.loads(cfg.read_text(encoding="utf-8")).get("email", "")
        except Exception:
            pass
    lines = [
        f"账号：{email or '未配置'}",
        f"会话：{'已保存' if session else '未登录'}",
        f"数据：{st['days']} 天（{st['first']} ~ {st['last']}），{st['activities']} 条活动",
        f"上次同步：{_store().get_meta('last_sync') or '从未'}",
    ]
    if not session:
        lines.append("")
        lines.append("下一步：在终端运行  run.bat login  （会提示输入邮箱、密码，可能需要邮箱验证码）")
    return "\n".join(lines)


def tool_sync(args) -> str:
    from garmin_cn.auth import AuthError
    from garmin_cn.client import GarminCNClient, SessionExpired
    from garmin_cn.net import Net
    from garmin_cn.sync import SyncService

    session = HOME / "data" / "session.json"
    if not session.exists():
        return "未登录。请先在终端运行：run.bat login"
    client = GarminCNClient(Net(session, min_interval=1.2))
    svc = SyncService(client, _store())
    try:
        res = svc.sync(days=int(args.get("days", 14)), extras=bool(args.get("extras", False)))
    except SessionExpired:
        return "会话已失效，请重新运行：run.bat login"
    except AuthError as e:
        return f"同步失败：{e}"
    out = [f"已同步 {res['range']}：{res['days']} 天，{res['activities']} 条活动"]
    if res["errors"]:
        out.append("部分接口异常：")
        out.extend(" - " + e for e in res["errors"])
    # 体重 / 体成分：秤的数据不在佳明云里，顺手带一次；失败只记一句，不影响已同步的数据
    if args.get("body", True):
        try:
            from garmin_cn import mi as mi_mod

            n, recs = mi_mod.sync(svc.store, HOME, days=int(args.get("body_days", 30)))
            out.append(f"体重/体成分 {n} 条（最新 {recs[-1]['date']}）" if n
                       else "体重：云端这段时间没有新记录")
        except Exception as e:
            out.append(f"体重未同步：{type(e).__name__}: {e}")
    return "\n".join(out)


def tool_today(args) -> str:
    from garmin_cn.coach import brief
    from garmin_cn.plan import attach

    res = attach(derive(_store(), _today(args)))
    t, r, sl, L = res["today"], res["readiness"], res["sleep"], res["load"]
    b = brief(res)
    lines = [
        f"# {res['date']} 快照",
        f"恢复度 {r['score']}/100（{r['level']}）",
    ]
    for p in r["parts"]:
        detail = p["detail"]
        if detail.startswith(p["name"]):  # 「睡眠：睡眠评分 85」这种重复
            lines.append(f"- {detail}（分项得分 {p['score']}/100）")
        else:
            lines.append(f"- {p['name']}：{detail}（分项得分 {p['score']}/100）")
    lines += [
        "",
        f"睡眠 {sl['total_h']} h（深睡 {fmt0(sl['deep_min'])} 分 / REM {fmt0(sl['rem_min'])} 分），"
        f"近 7 天睡眠债 {sl['debt_h']} h（含小睡抵扣）"
        + (f"，昨日小睡 {sl['nap_min']} 分" if sl.get("nap_min") else ""),
        f"静息心率 {t.get('resting_hr') or '—'} bpm｜HRV {fmt0(t.get('hrv_night'))} ms"
        f"（{t.get('hrv_status') or '—'}）｜身体电量 {t.get('bb_high') or '—'}"
        f"｜压力 {t.get('avg_stress') or '—'}｜步数 {t.get('steps') or '—'}",
        f"负荷：7 天 {L['acute_7d']}｜28 天 {L['chronic_28d']}｜ACWR {L['acwr']} · {L['acwr_state']}"
        f"｜CTL {L['ctl']}｜ATL {L['atl']}｜TSB {L['tsb']}",
    ]
    bt = res.get("body") or {}
    if bt.get("latest"):
        from garmin_cn import body as body_mod

        lines.append(body_mod.text(bt, bt.get("ctl_delta")))
    lines += [
        "",
        f"今日处方：{b['session']['type']}｜{b['session']['duration']}｜{b['session']['intensity']}",
        f"理由：{b['session']['why']}",
    ]
    nt = res.get("nutrition") or {}
    if nt.get("ok"):
        from garmin_cn import nutrition as nutri_mod

        lines += ["", nutri_mod.text(nt)]
    lines += [
        "",
        "风险标记：",
    ]
    lines += [f"- {f['text']}" for f in res["flags"]]
    return "\n".join(lines)


def tool_coach_context(args) -> str:
    from garmin_cn.coach import context_markdown
    from garmin_cn.plan import attach

    return context_markdown(attach(derive(_store(), _today(args))))


def tool_save_note(args) -> str:
    text = (args.get("text") or "").strip()
    if not text:
        return "点评内容不能为空"
    date = args.get("date") or dt.date.today().isoformat()
    _store().add_note(text, date=date, source=args.get("source", "workbuddy"))
    return f"已保存 {date} 的教练点评（{len(text)} 字），看板「教练点评」区域会显示。"


def tool_report(args) -> str:
    from garmin_cn.report import render

    date = _today(args)
    store = _store()
    if store.stats()["days"] == 0:
        return "库里还没有数据，先调用 garmin_sync 同步。"
    res = derive(store, date)
    path = render(res, store, OUT / f"dashboard-{date}.html")
    latest = OUT / "latest.html"
    try:
        latest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass
    return f"看板已生成：{path}"


def tool_card(args) -> str:
    from garmin_cn.card import render as render_card
    from garmin_cn.plan import attach

    date = _today(args)
    store = _store()
    if store.stats()["days"] == 0:
        return "库里还没有数据，先调用 garmin_sync 同步。"
    # 出图前刷新「会随时间变」的两项：身体电量（佳明日内序列）、体重（小米秤）
    try:
        from garmin_cn import live as livemod

        client = None
        session = HOME / "data" / "session.json"
        if session.exists():
            from garmin_cn.client import GarminCNClient
            from garmin_cn.net import Net

            client = GarminCNClient(Net(session, min_interval=1.2))
        livemod.body_battery(store, date, client)
        livemod.scale(store, HOME)
    except Exception:
        pass
    res = attach(derive(store, date))
    try:
        path = render_card(res, OUT / f"card-{date}.png", sync_at=store.get_meta("last_sync"))
    except Exception as e:
        return f"出图失败：{type(e).__name__}: {e}"
    return f"一图流已生成（1080 宽，{path.stat().st_size // 1024} KB）：{path}"


def _pace(sec) -> str:
    """秒/公里 → 4'45" 这种读法。"""
    try:
        v = float(sec)
    except (TypeError, ValueError):
        return "—"
    if not v:
        return "—"
    return f"{int(v) // 60}'{int(v) % 60:02d}\""


def tool_activity(args) -> str:
    from garmin_cn import activity as act

    store = _store()
    row = store.get_activity(activity_id=args.get("activity_id"), date=args.get("date") or _today(args))
    if not row:
        return "没找到这次活动，先调用 garmin_sync 同步，或确认活动 ID / 日期。"
    client = _client()
    detail = act.ensure(store, client, row["activity_id"], force=bool(args.get("refresh")))
    s = detail["summary"]
    w = detail.get("weather") or {}
    from garmin_cn.actcard import theme_for, kmh
    th = theme_for(row.get("type"))
    kind = th["kind"]
    pace = row.get("pace_s_km")
    # 骑行这类不适合说配速，改报时速；游泳单列每 100 米
    if kind == "speed":
        spd_txt = f"｜时速 {kmh(pace):.1f} km/h" if kmh(pace) else ""
    else:
        spd_txt = f"｜配速 {_pace(pace)}"
    lines = [
        f"{row['name']}（{row['date']}）",
        f"距离 {(row['distance_m'] or 0) / 1000:.2f} km｜用时 {(row['duration_s'] or 0) / 60:.0f} 分"
        f"{spd_txt}",
    ]
    if th["is_run"]:
        lines += [
            f"步频 {s['cad_avg']} spm（按总步频重算，P95 {s['cad_p95']}，校准 {s.get('rescaled') or 0} 点）"
            f"｜步幅 {s['stride_avg']} cm（修正 {s.get('repaired') or 0} 点）",
            f"步频分布 {s['cad_bands']}",
        ]
    if s.get("drift"):
        d = s["drift"]
        if kind == "speed":
            d0, d1 = kmh(d["pace_first"]), kmh(d["pace_second"])
            lines.append(f"前后半程：心率 {d['hr_first']}→{d['hr_second']}，"
                         f"时速 {d0:.1f}→{d1:.1f} km/h")
        else:
            lines.append(f"前后半程：心率 {d['hr_first']}→{d['hr_second']}，"
                         f"配速 {d['pace_first']}→{d['pace_second']} s/km")
    if w:
        from garmin_cn.actcard import WEATHER_CN
        wt = WEATHER_CN.get(str(w.get("text")), w.get("text"))
        lines.append(f"天气 {w.get('temp_c')}℃ 湿度 {w.get('humidity')}% {wt}")
    lines.append(f"分圈 {len(detail['laps'])}｜轨迹点 {len(detail['track'])}")

    if args.get("card"):
        from garmin_cn.actcard import render as render_act
        out = OUT / f"act-{row['date']}-{row['activity_id']}.png"
        try:
            path = render_act(row, detail, out)
            lines.append(f"活动图：{path}")
        except Exception as e:
            lines.append(f"出图失败：{type(e).__name__}: {e}")
    return "\n".join(lines)


def tool_trends(args) -> str:
    days = int(args.get("days", 14))
    store = _store()
    res = derive(store, dt.date.today().isoformat())
    rows = res["history"][-days:]
    if not rows:
        return "暂无数据"
    head = "| 日期 | 睡眠h | 小睡分 | HRV | 静息心率 | 电量 | 压力 | 步数 | 负荷 |\n|---|---|---|---|---|---|---|---|---|"
    lines = [head]
    for x in rows:
        sm = x.get("sleep_total_min")
        lines.append(
            "| {d} | {s} | {np} | {h} | {r} | {b} | {st} | {sp} | {ld} |".format(
                d=x["date"],
                s=round(sm / 60, 1) if sm else "—",
                np=x.get("nap_min") if x.get("nap_min") else "—",
                h=x.get("hrv_night") or "—",
                r=x.get("resting_hr") or "—",
                b=x.get("bb_high") or "—",
                st=x.get("avg_stress") or "—",
                sp=x.get("steps") or "—",
                ld=res["load"]["loads"].get(x["date"], 0),
            )
        )
    return "\n".join(lines)


def tool_plan(args) -> str:
    from garmin_cn.plan import attach, item_line, load

    path = _store().path.parent / "plan.json"
    data = load(path)
    if args.get("refresh") or not data:
        try:
            from garmin_cn.plan import fetch

            data = fetch(_client(), dt.date.today().isoformat(), out_path=path)
        except Exception as e:
            return f"读取佳明教练计划失败：{type(e).__name__}: {e}"

    if not data or not data.get("available"):
        reason = (data or {}).get("reason") or "未知原因"
        return f"没有可用的训练计划（{reason}）"
    ph = data.get("phase") or {}
    lines = [
        f"# 佳明教练计划：{data.get('name')}",
        f"水平 {data.get('level') or '—'}｜{data.get('weeks')} 周｜每周约 {data.get('per_week')} 次"
        f"｜{data.get('start')} ~ {data.get('end')}",
    ]
    if ph:
        lines.append(f"当前阶段：{ph.get('label') or ph.get('key')}（{ph.get('start')} ~ {ph.get('end')}）")
    lines.append("")
    lines.append("## 本周课表")
    for d in data.get("week") or []:
        if d["rest"]:
            lines.append(f"- {d['date']} {d['dow']}：休息")
        else:
            lines.append(f"- {d['date']} {d['dow']}：" + "；".join(item_line(x) for x in d["items"]))
    t = data.get("today") or {}
    lines.append("")
    if t.get("rest"):
        lines.append("今日教练安排：休息日，没有指定训练。")
        if data.get("next_date"):
            lines.append(f"下一次：{data['next_date']} " + "；".join(
                item_line(x) for x in data.get("next_items") or []))
    else:
        lines.append("今日教练安排：" + "；".join(item_line(x) for x in t.get("items") or []))
    return "\n".join(lines)


def tool_activities(args) -> str:
    days = int(args.get("days", 30))
    end = dt.date.today()
    start = (end - dt.timedelta(days=days)).isoformat()
    rows = _store().activities(start, end.isoformat())
    if not rows:
        return f"最近 {days} 天没有活动记录"
    lines = ["| 日期 | 类型 | 时长min | 距离km | 配速 | 平均心率 | 负荷 |", "|---|---|---|---|---|---|---|"]
    for a in rows[:40]:
        dur = round((a.get("duration_s") or 0) / 60)
        dist = round((a.get("distance_m") or 0) / 1000, 2)
        pace = a.get("pace_s_km")
        pace_s = f"{int(pace)//60}'{int(pace)%60:02d}\"" if pace else "—"
        lines.append(
            f"| {a['date']} | {a.get('type') or '—'} | {dur} | {dist} | {pace_s} | "
            f"{a.get('avg_hr') or '—'} | {a.get('load') or '—'} |"
        )
    return "\n".join(lines)


def tool_brief(args) -> str:
    """最近一次（或几次）跑步的微信简报：一段可直接发出去的纯文本。

    比走 CLI 更稳的地方在于不用起子进程、不依赖 `run.bat` 挑解释器——
    MCP 侧本来就跑在 Python 里。`card=true` 时顺带出活动图并附上路径。
    """
    from garmin_cn import activity as act
    from garmin_cn import brief as B

    store = _store()
    if args.get("activity_id"):
        got = store.get_activity(activity_id=args["activity_id"])
        rows = [got] if got else []
    elif args.get("date"):
        got = store.get_activity(date=args["date"])
        rows = [got] if got else []
    else:
        last = max(1, int(args.get("last") or 1))
        today = dt.date.today()
        start = (today - dt.timedelta(days=45)).isoformat()
        rows = sorted(store.activities(start, today.isoformat()),
                      key=lambda r: (r.get("start_ts") or r.get("date") or ""),
                      reverse=True)
        if not args.get("any"):
            rows = [r for r in rows if (r.get("type") or "") in RUN_TYPES]
        rows = rows[:last]
    if not rows:
        return "最近没有跑步记录。先调用 garmin_sync 同步，或确认活动 ID / 日期。"

    client = _client()
    out = []
    for row in rows:
        detail = act.ensure(store, client, row["activity_id"])
        txt = B.one(row, detail)
        if args.get("card"):
            from garmin_cn.actcard import render

            png = OUT / f"act-{row['date']}-{row['activity_id']}.png"
            if not (png.exists() and png.stat().st_size > 1024):
                render(row, detail, png)
            txt += f"\n图：{png}"
        out.append(txt)
    return ("\n\n" + "·" * 14 + "\n\n").join(out)


HANDLERS = {
    "garmin_status": tool_status,
    "garmin_sync": tool_sync,
    "garmin_today": tool_today,
    "garmin_coach_context": tool_coach_context,
    "garmin_save_note": tool_save_note,
    "garmin_report": tool_report,
    "garmin_card": tool_card,
    "garmin_activity": tool_activity,
    "garmin_trends": tool_trends,
    "garmin_plan": tool_plan,
    "garmin_activities": tool_activities,
    "garmin_brief": tool_brief,
}


def handle(req: dict) -> dict | None:
    method = req.get("method")
    rid = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": rid,
            "result": {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "garmin-cn", "version": "0.1.5"},
            },
        }
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"resources": []}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"prompts": []}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if not fn:
            return {
                "jsonrpc": "2.0", "id": rid,
                "result": {"content": [{"type": "text", "text": f"未知工具：{name}"}], "isError": True},
            }
        try:
            text = fn(args)
            return {
                "jsonrpc": "2.0", "id": rid,
                "result": {"content": [{"type": "text", "text": text}], "isError": False},
            }
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            return {
                "jsonrpc": "2.0", "id": rid,
                "result": {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True},
            }

    return {
        "jsonrpc": "2.0", "id": rid,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            ) + "\n")
            sys.stdout.flush()
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
