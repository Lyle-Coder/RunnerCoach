# -*- coding: utf-8 -*-
"""远征运行时端到端测试：抽签、岔路、支线、试炼、结算、结局。

    python test_expedition.py

时间靠 `today` 参数推进，不依赖真实日期——否则这些用例只能在特定某天通过。
"""
from __future__ import annotations

import datetime as dt
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="rc-exp-"))
os.environ["DB_PATH"] = str(TMP / "app.db")
os.environ["GROUPS_DB"] = str(TMP / "groups.db")
os.environ["SMS_PROVIDER"] = "dev"
os.environ.pop("SMTP_HOST", None)

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import app  # noqa: E402
import expedition as EXP  # noqa: E402
import tick as TICK  # noqa: E402

PORT = 0
ok = fail = 0
POOL = json.loads((HERE / "content" / "expedition_events.json").read_text(encoding="utf-8"))


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [ok] {name}")
    else:
        fail += 1
        print(f"  [x]  {name}  {extra}")


def req(method, path, body=None, token=""):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=15)
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    raw = r.read().decode("utf-8", "ignore")
    c.close()
    try:
        return r.status, json.loads(raw)
    except json.JSONDecodeError:
        return r.status, {"_raw": raw}


def login(email):
    st, r = req("POST", "/v1/sms/send", {"account": email})
    assert st == 200 and r.get("dev_code"), f"验证码失败 {st} {r}"
    st, r = req("POST", "/v1/auth", {"account": email, "code": r["dev_code"]})
    assert st == 200 and r.get("token"), f"登录失败 {st} {r}"
    return r["token"]


def state(gid, tok, today):
    return req("GET", f"/v1/group/{gid}/run/state?today={today}", None, tok)


def main() -> int:
    global PORT
    app.init_db()
    app.group_api.init_db()
    EXP.init_db()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    srv.daemon_threads = True
    PORT = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"临时库 {TMP}｜端口 {PORT}\n")

    a, b = login("laoli@test.local"), login("xiaozhao@test.local")
    c3 = login("outsider@test.local")
    st, r = req("POST", "/v1/group/create", {"name": "秦岭跑团", "max_members": 10, "alias": "老李"}, a)
    gid, code = r["group"]["id"], r["group"]["invite_code"]
    req("POST", "/v1/group/join", {"code": code, "alias": "小赵"}, b)
    # 固定 seed：抽签是确定性的，但种子本身随机。不钉住的话每次跑抽到的
    # 事件不同，带 if 的断言数量会漂移，失败也复现不了。
    with EXP.G.db() as c:
        c.execute("INSERT INTO group_run_season(group_id,season_start,season_end,act,morale,"
                  "seed,flags,settled_act,ended,updated_at) VALUES(?,?,?,1,5,'fixtureseed','{}',0,0,?)",
                  (gid, "2026-11-16", "2026-12-13", 1))
    print(f"组 {gid} 建好，两人加入\n")

    # 另起一个组测「自动开季」——主组用了固定赛季，测不到这条
    st, r = req("POST", "/v1/group/create", {"name": "自动开季组", "alias": "阿甘"}, c3)
    gid_auto = r["group"]["id"]
    st, d_auto = state(gid_auto, c3, "2026-11-16")
    check("没有赛季时自动开季",
          st == 200 and d_auto.get("season", {}).get("act") == 1, f"{st} {d_auto}")

    # ---------------- 开季
    print("1. 开季与抽签")
    st, d = state(gid, a, "2026-11-16")
    check("读取赛季 200", st == 200, f"{st} {d}")
    check("当前是第一幕", d["season"]["act"] == 1, str(d.get("season")))
    check("周区间是 11-16~11-22",
          d["season"]["week_from"] == "2026-11-16" and d["season"]["week_to"] == "2026-11-22",
          f'{d["season"]["week_from"]}~{d["season"]["week_to"]}')
    check("抽到岔路", bool(d.get("fork")), str(d.get("fork")))
    check("每人两条支线", len(d["events"]) == 2, f'{len(d["events"])} 条')
    check("段目标 > 0（按人数算）", d["progress"]["target"] > 0, str(d["progress"]))
    check("初始补给 3", d["me"]["supply"] == 3, str(d["me"]))

    st, d2 = state(gid, a, "2026-11-16")
    check("抽签确定：两次同一岔路", d["fork"]["id"] == d2["fork"]["id"],
          f'{d["fork"]["id"]} vs {d2["fork"]["id"]}')
    check("抽签确定：两次同一支线", [x["id"] for x in d["events"]] == [x["id"] for x in d2["events"]])
    st, db_ = state(gid, b, "2026-11-16")
    check("同一组共享一个岔路", db_["fork"]["id"] == d["fork"]["id"])
    check("不同人的支线不同", [x["id"] for x in db_["events"]] != [x["id"] for x in d["events"]],
          f'{" vs ".join(str([x["id"] for x in d["events"]]) for _ in range(1))}')

    # ---------------- 岔路
    print("\n2. 岔路")
    fork = d["fork"]
    opt0, opt1 = fork["options"][0]["id"], fork["options"][1]["id"]
    st, r = req("POST", f"/v1/group/{gid}/run/fork",
                {"option": opt0, "today": "2026-11-16"}, a)
    check("选岔路 200", st == 200, f"{st} {r}")
    check("返回了结果文案", bool(r.get("result")), str(r)[:80])
    st, r2 = req("POST", f"/v1/group/{gid}/run/fork",
                 {"option": opt0, "today": "2026-11-16"}, a)
    check("重复选同一个是幂等", st == 200 and r2.get("already") is True, f"{st} {r2}")
    st, r3 = req("POST", f"/v1/group/{gid}/run/fork",
                 {"option": opt1, "today": "2026-11-16"}, a)
    check("选过就不能改（409）", st == 409, f"{st} {r3}")

    st, d = state(gid, a, "2026-11-16")
    check("state 里带上已选结果", d["fork"].get("chosen") == opt0, str(d["fork"].get("chosen")))
    check("岔路的格已生效", d["me"]["pos"] != 0 or d["me"]["supply"] != 3,
          f'pos={d["me"]["pos"]} supply={d["me"]["supply"]}')

    # ---------------- 支线
    print("\n3. 支线事件")
    # 支线可能是试炼（没有 options，走 /trial），所以挑一条真正带选项的来测
    slot, ev = next(((i, x) for i, x in enumerate(d["events"]) if x.get("options")), (None, None))
    trials = [x for x in d["events"] if x["type"] == "trial"]
    check("试炼不返回 options", all(not t["options"] for t in trials),
          str([t["id"] for t in trials]))
    if ev:
        pick = ev["options"][0]["id"]
        st, r = req("POST", f"/v1/group/{gid}/run/event",
                    {"slot": slot, "option": pick, "today": "2026-11-16"}, a)
        check("选支线 200", st == 200, f"{st} {r}")
        st, r2 = req("POST", f"/v1/group/{gid}/run/event",
                     {"slot": slot, "option": pick, "today": "2026-11-16"}, a)
        check("重复选是幂等", st == 200 and r2.get("already") is True, f"{st} {r2}")
        if len(ev["options"]) > 1:
            st, r3 = req("POST", f"/v1/group/{gid}/run/event",
                         {"slot": slot, "option": ev["options"][1]["id"],
                          "today": "2026-11-16"}, a)
            check("选过不能改（409）", st == 409, f"{st} {r3}")
        st, d = state(gid, a, "2026-11-16")
        check("state 里带 chosen", d["events"][slot].get("chosen") == pick,
              str(d["events"][slot]))
        check("选项不下发 effect（不能当数值题算）",
              "effect" not in d["events"][slot]["options"][0],
              str(d["events"][slot]["options"][0]))
    else:
        check("本幕两条支线都是试炼（跳过选项用例）", True)

    # ---------------- 试炼
    print("\n4. 试炼")
    trial_id = next((e["id"] for e in POOL["events"]
                     if e["act"] == 1 and e["type"] == "trial"), None)
    print(f"  （抽到的试炼：{[t['id'] for t in trials] or '无'}）")
    st, r = req("POST", f"/v1/group/{gid}/run/trial",
                {"trial_id": "not_a_trial", "done": True, "today": "2026-11-16"}, a)
    check("乱报 id 被拒（400）", st == 400, f"{st} {r}")
    # 池子里有、但这条不在你这次的路上：不能挑一条最划算的来报
    if trial_id and trial_id not in [x["id"] for x in d["events"]]:
        st, r = req("POST", f"/v1/group/{gid}/run/trial",
                    {"trial_id": trial_id, "done": True, "today": "2026-11-16"}, a)
        check("没抽到的试炼被拒（404）", st == 404, f"{st} {r}")

    # ---------------- 周目标
    print("\n5. 周目标")
    st, r = req("POST", f"/v1/group/{gid}/run/target",
                {"week_target_km": 40, "today": "2026-11-16"}, a)
    check("报 40 km", st == 200, f"{st} {r}")
    st, r = req("POST", f"/v1/group/{gid}/run/target",
                {"week_target_km": 80, "today": "2026-11-16"}, a)
    check("只能下调不能上调（409）", st == 409, f"{st} {r}")
    st, r = req("POST", f"/v1/group/{gid}/run/target",
                {"week_target_km": 30, "today": "2026-11-16"}, a)
    check("下调可以", st == 200, f"{st} {r}")

    # ---------------- 上报里程并结算
    print("\n6. 结算与推进")
    req("POST", f"/v1/group/{gid}/report", {"days": [
        {"date": "2026-11-17", "distance_m": 20000, "duration_s": 6600, "acts": 1}]}, a)
    req("POST", f"/v1/group/{gid}/report", {"days": [
        {"date": "2026-11-18", "distance_m": 18000, "duration_s": 6000, "acts": 1}]}, b)
    st, d1 = state(gid, a, "2026-11-20")
    team_before = d1["progress"]["team"]
    check("实时进度包含训练格", team_before > 0, str(d1["progress"]))

    st, d = state(gid, a, "2026-11-23")
    check("跨周推进到第二幕", d["season"]["act"] == 2, str(d.get("season")))
    check("第一幕结算后有累计量", d["progress"]["team"] > 0, str(d["progress"]))
    check("段目标随之变大", d["progress"]["target"] > d1["progress"]["target"],
          f'{d1["progress"]["target"]} -> {d["progress"]["target"]}')

    # ---------------- 救援
    print("\n7. 救援")
    st, r = req("POST", f"/v1/group/{gid}/run/rescue",
                {"user_id": 99999, "today": "2026-11-23"}, a)
    check("救不存在的人（404）", st == 404, f"{st} {r}")
    st, r = req("POST", f"/v1/group/{gid}/run/rescue",
                {"user_id": 1, "today": "2026-11-23"}, a)
    check("不能救自己（400/404）", st in (400, 404), f"{st} {r}")

    # ---------------- 权限
    print("\n8. 权限")
    st, r = state(gid, c3, "2026-11-23")
    check("非成员看不到赛季（403）", st == 403, f"{st} {r}")

    # ---------------- 结局
    print("\n9. 结局")
    # 时间轴单向：先确认「还没结束时不剧透」，再推到赛季末
    st, r = req("GET", f"/v1/group/{gid}/run/outcome?today=2026-11-23", None, a)
    check("没结束时不剧透", st == 200 and r.get("ended") is False, f"{st} {r}")

    st, d = state(gid, a, "2026-12-14")
    check("赛季结束", d["season"]["ended"] is True, str(d.get("season")))
    st, r = req("GET", f"/v1/group/{gid}/run/outcome?today=2026-12-14", None, a)
    check("结局 200", st == 200 and r.get("ended") is True, f"{st} {r}")
    check("结局是四种之一",
          r.get("ending") in ("arrive", "detour", "lost", "discoverer"), str(r.get("ending")))
    check("结局带团队总量", "team" in r, str(r))

    # ---------------- 翻篇
    print("\n9b. 翻篇（结束后留一天看结果，第二天开新的一季）")
    # 拿自动开季那个组：它的赛季是 11-16~12-13，12-14 是结束后第一天
    st, dg = state(gid_auto, c3, "2026-12-14")
    check("赛季结束后第一天还能看到结果", dg["season"]["ended"] is True,
          str(dg.get("season")))
    st, dg = state(gid_auto, c3, "2026-12-15")
    check("第二天自动开新的一季",
          dg["season"]["ended"] is False and dg["season"]["act"] == 1,
          str(dg.get("season")))
    check("新的一季从当周周一开始",
          dg["season"]["start"] == "2026-12-14", str(dg["season"].get("start")))
    last = dg["season"].get("last") or {}
    check("上一季的结论带过来了", last.get("ending") in
          ("arrive", "detour", "lost", "discoverer"), str(last))
    check("翻篇后补给回 3、行程回 0",
          dg["me"]["supply"] == 3 and dg["me"]["pos"] == 0,
          f'supply={dg["me"]["supply"]} pos={dg["me"]["pos"]}')
    st, r = req("GET", f"/v1/group/{gid_auto}/run/outcome?today=2026-12-15", None, c3)
    check("新季里 outcome 是进度 + 上一季",
          r.get("ended") is False and bool(r.get("last")), f"{st} {r}")

    # ---------------- cron 预热
    print("\n10. cron 预热")
    # a 已经是组长，按「一人不能当两个组长」会被拒，用 b 建
    st, r = req("POST", "/v1/group/create", {"name": "预热组", "alias": "队长"}, b)
    gid_t = r["group"]["id"]
    req("POST", f"/v1/group/{gid_t}/report", {"days": [
        {"date": "2026-11-16", "distance_m": 12000, "duration_s": 4000, "acts": 1}]}, b)
    state(gid_t, b, "2026-11-16")          # 让赛季开起来
    T2 = dt.date(2026, 11, 23)
    check("tick 能识别需要结算的组", gid_t in TICK.due(T2), str(TICK.due(T2)))
    rep = TICK.run(today=T2, quiet=True)
    check("tick 结算成功", rep["settled"] >= 1 and rep["failed"] == 0, str(rep))
    st, d3 = state(gid_t, b, "2026-11-23")
    check("tick 之后直接是第二幕", d3["season"]["act"] == 2, str(d3.get("season")))
    rep2 = TICK.run(today=T2, quiet=True)
    check("重复 tick 不再结算（幂等）", rep2["due"] == 0, str(rep2))
    # 其他组也在跑，所以只断言「本组又需要结算了」，不断言列表恰好等于它
    check("下一周才会重新需要结算",
          gid_t in TICK.due(dt.date(2026, 11, 30)), str(TICK.due(dt.date(2026, 11, 30))))

    # 并发：cron 和 web 抢同一组，只能算一次
    errs = []
    def hammer():
        try:
            EXP.ensure(gid_t, dt.date(2026, 11, 30))
        except Exception as e:  # noqa: BLE001
            errs.append(f"{type(e).__name__}: {e}")
    ts = [threading.Thread(target=hammer) for _ in range(10)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("10 线程并发结算无报错", not errs, str(errs[:2]))
    with EXP.G.db() as c:
        srow = EXP._season_of(c, gid_t)
        flags = json.loads(srow["flags"])
    check("并发只推进一幕", int(srow["settled_act"]) == 2, str(srow["settled_act"]))
    check("各幕只记一次（没有被重复结算）",
          len(flags.get("stages") or {}) == 2, json.dumps(flags.get("stages")))

    # ---------------- 试炼核验
    print("\n11. 试炼核验（数据说了算）")

    def _ev(eid):
        return next(e for e in POOL["events"] if e["id"] == eid)

    def _mk(metric, need_km, good, date="2026-11-17", aid="ev-1"):
        """造一份分圈：`good=True` 是刚好达标，`False` 是明确不达标。

        每公里一圈、每圈 1000 m，这是「至少有 3 个整圈」那条门槛的正常形态。
        """
        n = max(3, int(round(need_km)))
        laps = []
        for i in range(n):
            if metric == "negative_split":
                t = (330 if i < n // 2 else 300) if good else 330
            elif metric == "even_pace":
                t = 300 if good else (260 if i % 2 == 0 else 340)
            else:
                t = 300
            cad = 178 if good else 165 + i * 6
            hr = 150 if good else 140 + i * 12
            ele = 50 if good else 10
            laps.append({"dist_m": 1000, "dur_s": t, "cad": cad, "hr": hr, "ele": ele})
        return {"activities": [{"activity_id": aid, "date": date,
                                "distance_km": need_km, "laps": laps}]}

    start, end = dt.date(2026, 11, 16), dt.date(2026, 11, 22)
    for metric, tol, need in (("negative_split", 15, 5), ("even_pace", 5, 6),
                              ("cadence_stable", 3, 5), ("hr_drift", 5, 8),
                              ("climb_steady", 30, 5)):
        qz = {"metric": metric, "distance_km": need, "tolerance_s": tol}
        got = EXP.verify_trial(qz, _mk(metric, need, True), start, end)[0]
        check(f"{metric}：达标判 verified", got == "verified", got)
        got = EXP.verify_trial(qz, _mk(metric, need, False), start, end)[0]
        check(f"{metric}：不达标判 failed", got == "failed", got)

    check("没有证据判 unverified",
          EXP.verify_trial({"metric": "even_pace", "distance_km": 6, "tolerance_s": 5},
                           {}, start, end)[0] == "unverified")
    few = {"activities": [{"activity_id": "x", "date": "2026-11-17", "distance_km": 6,
                           "laps": [{"dist_m": 1000, "dur_s": 300},
                                    {"dist_m": 1000, "dur_s": 300}]}]}
    check("圈数不够判 unverified（不当失败处理）",
          EXP.verify_trial({"metric": "even_pace", "distance_km": 6, "tolerance_s": 5},
                           few, start, end)[0] == "unverified")
    out_of_week = _mk("even_pace", 6, True, date="2026-11-10")
    check("上周的活动不认",
          EXP.verify_trial({"metric": "even_pace", "distance_km": 6, "tolerance_s": 5},
                           out_of_week, start, end)[0] == "unverified")
    # 跑 10 km 去应 5 km 的试炼：只截前 5 km。整条跑看「后半比前半快」是成立的，
    # 但前 5 km 全程匀速——不截窗口，长距离跑能白捡任何试炼。
    laps = ([{"dist_m": 1000, "dur_s": 330} for _ in range(5)]
            + [{"dist_m": 1000, "dur_s": 280} for _ in range(5)])
    ten = {"activities": [{"activity_id": "long", "date": "2026-11-17",
                           "distance_km": 10, "laps": laps}]}
    check("10 km 跑不动 5 km 的负分割（只截前 5 km）",
          EXP.verify_trial({"metric": "negative_split", "distance_km": 5,
                            "tolerance_s": 15}, ten, start, end)[0] == "failed")
    # 三份候选里只要有一份达标就算过——本周跑了好几次的人不该被第一条拖死
    mix = _mk("even_pace", 6, False, aid="bad")
    mix["activities"].append(dict(_mk("even_pace", 6, True, aid="good")["activities"][0]))
    lvl, aid = EXP.verify_trial({"metric": "even_pace", "distance_km": 6,
                                 "tolerance_s": 5}, mix, start, end)
    check("多条候选里有一条达标就算过", lvl == "verified" and aid == "good", f"{lvl} {aid}")

    # ---- 端到端：真的走一遍 /run/trial
    d4 = login("trial@test.local")
    # 把抽签逼成「必出试炼」，否则这一节要靠运气才跑得起来
    old_w = EXP.TYPE_W
    EXP.TYPE_W = (("trial", 1),)
    try:
        st, r = req("POST", "/v1/group/create", {"name": "试炼组", "alias": "阿试"}, d4)
        gid_v = r["group"]["id"]
        st, dv = state(gid_v, d4, "2026-11-16")
    finally:
        EXP.TYPE_W = old_w
    tvs = [x for x in dv["events"] if x["type"] == "trial"]
    check("逼出来的支线里有试炼", bool(tvs), str([x["id"] for x in dv["events"]]))

    def post_trial(tid, done, evidence, today="2026-11-16"):
        body = {"trial_id": tid, "done": done, "today": today}
        if evidence:
            body["evidence"] = evidence
        return req("POST", f"/v1/group/{gid_v}/run/trial", body, d4)

    if tvs:
        tv = tvs[0]
        q = tv["quality"]
        full = int(_ev(tv["id"]).get("success", {}).get("advance") or 0)
        st, r = post_trial(tv["id"], True, _mk(q["metric"], q["distance_km"], True))
        check("交分圈且达标 → verified", st == 200 and r.get("level") == "verified",
              f"{st} {r}")
        check("核验通过拿全额格", r.get("advance") == full, f'{r.get("advance")} vs {full}')
        st, dv = state(gid_v, d4, "2026-11-16")
        card = next((x for x in dv["events"] if x["id"] == tv["id"]), {})
        check("核验后卡片显示已选（不再给两个按钮）", card.get("chosen") == "ok", str(card)[:120])
        check("卡片带 level，界面能写「核验通过」", card.get("level") == "verified",
              str(card.get("level")))
        st, r = post_trial(tv["id"], True, _mk(q["metric"], q["distance_km"], True))
        check("重复报同结果是幂等", st == 200 and r.get("already") is True, f"{st} {r}")

    if len(tvs) > 1:
        tv = tvs[1]
        half = max(1, int(_ev(tv["id"]).get("success", {}).get("advance") or 0) // 2)
        st, r = post_trial(tv["id"], True, None)
        check("没交证据 → unverified", st == 200 and r.get("level") == "unverified",
              f"{st} {r}")
        check("未核验只给一半格", r.get("advance") == half,
              f'{r.get("advance")} vs {half}')
    else:
        check("本幕只有一条试炼（跳过未核验用例）", True)

    # 失败场景放到第二幕：一幕只有两三条试炼，不能全用在同一个分支上
    EXP.TYPE_W = (("trial", 1),)
    try:
        st, dv2 = state(gid_v, d4, "2026-11-23")
    finally:
        EXP.TYPE_W = old_w
    tvs2 = [x for x in dv2["events"] if x["type"] == "trial"]
    if tvs2:
        q2 = tvs2[0]["quality"]
        sup = int(dv2["me"]["supply"])
        st, r = post_trial(tvs2[0]["id"], True,
                           _mk(q2["metric"], q2["distance_km"], False, date="2026-11-24"),
                           today="2026-11-23")
        check("交分圈但没达标 → failed", st == 200 and r.get("level") == "failed",
              f"{st} {r}")
        check("failed 按失败扣补给", r.get("supply") == sup - 1, f'{sup} -> {r.get("supply")}')
    else:
        check("第二幕没试炼（跳过失败用例）", True)

    print(f"\n通过 {ok} 项，失败 {fail} 项")
    srv.shutdown()
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
