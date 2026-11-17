# -*- coding: utf-8 -*-
"""远征运行时端到端测试：抽签、岔路、支线、试炼、结算、结局。

    python test_expedition.py

时间靠 `today` 参数推进，不依赖真实日期——否则这些用例只能在特定某天通过。
"""
from __future__ import annotations

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
    print(f"组 {gid} 建好，两人加入\n")

    # ---------------- 开季
    print("1. 开季与抽签")
    st, d = state(gid, a, "2026-11-16")
    check("自动开季 200", st == 200, f"{st} {d}")
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
    before = d["me"]["pos"]
    print(f"  （抽到的试炼：{[t['id'] for t in trials] or '无'}）")
    st, r = req("POST", f"/v1/group/{gid}/run/trial",
                {"trial_id": trial_id, "done": True, "today": "2026-11-16"}, a)
    check("报试炼成功 200", st == 200 and r.get("done") is True, f"{st} {r}")
    st, d = state(gid, a, "2026-11-16")
    check("成功后加 3 格", d["me"]["pos"] == before + 3, f'{before} -> {d["me"]["pos"]}')
    st, r = req("POST", f"/v1/group/{gid}/run/trial",
                {"trial_id": trial_id, "done": True, "today": "2026-11-16"}, a)
    check("重复报同结果是幂等", st == 200 and r.get("already") is True, f"{st} {r}")
    st, r = req("POST", f"/v1/group/{gid}/run/trial",
                {"trial_id": "not_a_trial", "done": True, "today": "2026-11-16"}, a)
    check("乱报 id 被拒（400）", st == 400, f"{st} {r}")

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

    st, d = state(gid, a, "2026-12-20")
    check("赛季结束", d["season"]["ended"] is True, str(d.get("season")))
    st, r = req("GET", f"/v1/group/{gid}/run/outcome?today=2026-12-20", None, a)
    check("结局 200", st == 200 and r.get("ended") is True, f"{st} {r}")
    check("结局是四种之一",
          r.get("ending") in ("arrive", "detour", "lost", "discoverer"), str(r.get("ending")))
    check("结局带团队总量", "team" in r, str(r))

    print(f"\n通过 {ok} 项，失败 {fail} 项")
    srv.shutdown()
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
