# -*- coding: utf-8 -*-
"""把接口的真实返回抓成样例，给前端页面打底。

为什么要有这个
--------------
画页面时最怕「对着代码猜字段」。猜错的代价不是改一行，而是把整块布局按错的
结构搭好，等接真数据时再推倒。这个脚本跑一遍完整流程（建组 → 四周赛季 →
受困 → 救援 → 结局），把每个接口**真实返回的 JSON 原样存下来**：

    python dump_fixtures.py

输出到 `preview/groupui/fixtures/`：

* `NN_名称.json`：接口原样返回，一个文件一次调用
* `index.md`：每个文件对应哪个接口、什么状态、哪些字段是前端要用的

样例不是手写的。手写的样例一定会跟实现漂移，而且会「恰好」避开边界情况。
这里的每个文件都是真实 HTTP 响应，包括错误码。

用法约定
--------
* 时间线固定（2026-11-16 起季），不依赖「今天」，否则隔一周跑出来的东西就变了。
* 每一步都校验状态码，失败就抛——不能让一个错误响应静静躺进 fixture，
  那会让前端照着错的东西写。
"""

import datetime as dt
import http.client
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = ROOT.parent / "preview" / "groupui" / "fixtures"

SEASON_START = "2026-11-16"   # 周一
ACC = {
    "owner": "laoli@fixture.local",
    "member": "xiaozhao@fixture.local",
    "third": "agou@fixture.local",
}

saved: list[tuple[str, str, str, int, dict]] = []   # (文件, 方法, 路径, 状态码, 说明)
# 跨函数传的上下文：错误用例要知道第一幕「真正选过的是哪个选项」，
# 否则拿一个不属于这条岔路的 id 去撞，拿到的是 400「没有这个选项」，
# 根本测不到「已经选过了」那条分支。
CTX: dict = {}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rc-fix-"))
    os.environ["DB_PATH"] = str(tmp / "app.db")
    os.environ["GROUPS_DB"] = str(tmp / "groups.db")
    os.environ["SMS_PROVIDER"] = "dev"
    os.environ.pop("SMTP_HOST", None)
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(ROOT))

    import app                                    # noqa: E402
    from http.server import ThreadingHTTPServer    # noqa: E402

    app.init_db()
    app.group_api.init_db()
    # 远征的表在另一个模块里建。漏了这一步的表现是「no such table: group_run_season」，
    # 而线上由 app.main() 统一初始化，本地跑脚本时要自己补。
    app.expedition_api.init_db()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    srv.daemon_threads = True
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    global _PORT
    _PORT = port
    toks = {k: _login(v) for k, v in ACC.items()}
    print(f"服务端口 {port}，三个账号已登录\n")

    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*.json"):
        f.unlink()

    gid = _groups(toks)
    _season(gid, toks)
    _errors(gid, toks)

    _write_index(gid)
    srv.shutdown()
    print(f"\n共 {len(saved)} 个样例 → {OUT}")
    return 0


# ---------------------------------------------------------------- HTTP
_PORT = 0


def _req(method: str, path: str, body=None, token: str | None = None):
    c = http.client.HTTPConnection("127.0.0.1", _PORT, timeout=20)
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    c.request(method, path, body=data, headers=headers)
    r = c.getresponse()
    raw = r.read().decode("utf-8", "replace")
    c.close()
    try:
        return r.status, json.loads(raw)
    except json.JSONDecodeError:
        return r.status, {"_raw": raw[:400]}


def _login(account: str) -> str:
    st, r = _req("POST", "/v1/sms/send", {"account": account})
    assert st == 200, f"验证码发送失败 {st} {r}"
    st, r = _req("POST", "/v1/auth", {"account": account, "code": r["dev_code"]})
    assert st == 200, f"登录失败 {st} {r}"
    return r["token"]


def _save(name: str, method: str, path: str, st: int, payload, note: str,
          expect: int = 200) -> None:
    assert st == expect, f"[{name}] 期望 {expect} 实际 {st}：{json.dumps(payload, ensure_ascii=False)[:300]}"
    (OUT / f"{name}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    saved.append((name, method, path, st, note))
    print(f"  {name}.json  ({method} {path} → {st})")


def _call(name: str, method: str, path: str, note: str, *,
          body=None, token=None, expect=200):
    st, payload = _req(method, path, body, token)
    _save(name, method, path, st, payload, note, expect)
    return payload


# ---------------------------------------------------------------- 试炼
def _force_trial(gid: int, uid: int, act: int, pool: dict) -> str:
    """把这个人本幕的最后一个槽位换成试炼，返回试炼 id。

    试炼是抽出来的（每槽位 40%），样例要覆盖它就不能靠运气。换掉而不是
    追加第三个槽位：追加会让 state 里出现「每人三条支线」，和文档对不上，
    前端照着样例写就会多渲染一格。
    """
    import expedition as EXP

    cand = sorted(e["id"] for e in pool["events"]
                  if int(e.get("act") or 0) == act and e.get("type") == "trial")
    if not cand:
        raise SystemExit(f"事件池里没有第 {act} 幕的试炼")
    with EXP.G.db() as c:
        rows = c.execute("SELECT slot, event_id FROM group_run_event WHERE group_id=?"
                         " AND user_id=? AND act=? ORDER BY slot", (gid, uid, act)).fetchall()
        hit = next((r["event_id"] for r in rows if r["event_id"] in cand), None)
        if hit:
            return hit
        slot = rows[-1]["slot"] if rows else 0
        c.execute("UPDATE group_run_event SET event_id=?, choice='', tag='', ts=?"
                  " WHERE group_id=? AND user_id=? AND act=? AND slot=?",
                  (cand[0], EXP.G._now(), gid, uid, act, slot))
    return cand[0]


def _quality(pool: dict, eid: str) -> dict:
    for e in pool["events"]:
        if e["id"] == eid:
            return e.get("quality") or {}
    return {}


def _trial_evidence(quality: dict, good: bool, date: str) -> dict:
    """造一份分圈证据。`good=True` 刚好达标，`False` 明确不达标。

    样例要展示的是 `level` 的三种形态，所以证据本身不用好看，只要能让
    服务端算出对应结论。
    """
    metric = str((quality or {}).get("metric") or "")
    need = float((quality or {}).get("distance_km") or 5)
    n = max(3, int(round(need)))
    laps = []
    for i in range(n):
        if metric == "negative_split":
            dur = (330 if i < n // 2 else 300) if good else 330
        elif metric == "even_pace":
            dur = 300 if good else (260 if i % 2 == 0 else 340)
        else:
            dur = 300
        laps.append({"dist_m": 1000, "dur_s": dur,
                     "cad": 178 if good else 165 + i * 6,
                     "hr": 150 if good else 140 + i * 12,
                     "ele": 50 if good else 10})
    return {"activities": [{"activity_id": "fx-act-1", "date": date,
                            "distance_km": need, "laps": laps}]}


# ---------------------------------------------------------------- 群组
def _groups(t: dict) -> int:
    print("1. 群组接口")
    r = _call("01_group_create", "POST", "/v1/group/create",
              "建组。`invite_code` 只有组长/管理员能拿到",
              body={"name": "秦岭跑团", "intro": "周末约跑 · 北郊集合",
                    "max_members": 20, "alias": "老李"}, token=t["owner"])
    gid = r["group"]["id"]
    code = r["group"]["invite_code"]

    _call("02_group_join", "POST", "/v1/group/join",
          "凭邀请码加入", body={"code": code, "alias": "小赵"}, token=t["member"])
    _call("03_group_join_third", "POST", "/v1/group/join",
          "第三人加入（后面用来测受困与救援）",
          body={"code": code, "alias": "阿狗"}, token=t["third"])

    _call("04_group_list", "GET", "/v1/group/list",
          "我的小组列表。注意 `member_count` 是人数，`members` 只在详情里才是数组",
          token=t["member"])
    _call("05_group_detail_member", "GET", f"/v1/group/{gid}",
          "组详情 · 普通成员视角：**没有 invite_code**（拿到就能无限拉人）",
          token=t["member"])
    _call("06_group_detail_owner", "GET", f"/v1/group/{gid}",
          "组详情 · 组长视角：多一个 `invite_code`", token=t["owner"])

    _call("07_group_report", "POST", f"/v1/group/{gid}/report",
          "上报日汇总。重复发同一批是幂等的",
          body={"days": [
              {"date": "2026-11-16", "distance_m": 9600, "duration_s": 3240,
               "acts": 1, "plan_done": True},
              {"date": "2026-11-17", "distance_m": 7200, "duration_s": 2500,
               "acts": 1, "plan_done": True},
              {"date": "2026-11-19", "distance_m": 15000, "duration_s": 5400,
               "acts": 1, "plan_done": True},
          ]}, token=t["owner"])
    _call("08_group_report_member", "POST", f"/v1/group/{gid}/report",
          "成员上报（里程略低，榜单才有差异）",
          body={"days": [
              {"date": "2026-11-16", "distance_m": 5000, "duration_s": 1900,
               "acts": 1, "plan_done": False},
              {"date": "2026-11-18", "distance_m": 12800, "duration_s": 4300,
               "acts": 1, "plan_done": True},
          ]}, token=t["member"])
    _call("09_group_report_third", "POST", f"/v1/group/{gid}/report",
          "第三人的上报",
          body={"days": [
              {"date": "2026-11-16", "distance_m": 6100, "duration_s": 2200,
               "acts": 1, "plan_done": True},
          ]}, token=t["third"])

    _call("10_board_week", "GET", f"/v1/group/{gid}/board?range=week",
          "本周榜。`hidden` 是按共享级别隐藏的人数", token=t["member"])
    _call("11_board_month", "GET", f"/v1/group/{gid}/board?range=month",
          "本月榜（range 取 week / month）", token=t["owner"])

    _call("12_challenge_create", "POST", f"/v1/group/{gid}/challenge/create",
          "建组内赛事",
          body={"title": "本周 40 公里", "kind": "distance", "target": 40000,
                "start_date": "2026-11-16", "end_date": "2026-11-22"}, token=t["owner"])
    _call("13_challenge_list", "GET", f"/v1/group/{gid}/challenge/list",
          "赛事列表", token=t["member"])

    _call("14_share_get", "GET", f"/v1/group/{gid}",
          "（同详情，看 me.share）默认 summary：只共享里程", token=t["member"])
    _call("15_share_set", "POST", f"/v1/group/{gid}/share",
          "关掉共享：之后不再接收上报，也不出现在榜单里（只有 summary / off 两档）",
          body={"share": "off"}, token=t["member"])
    return gid


# ---------------------------------------------------------------- 赛季
def _season(gid: int, t: dict) -> None:
    print("\n2. 远征 · 第一幕")
    w1 = "2026-11-17"       # 第一幕内（周二）
    # 种子固定：不然每次跑出来的岔路、支线、遗迹都不一样，样例就没法当参考，
    # 字段还可能在两次之间多一个少一个（比如「两条支线都有 options」那组
    # 用例时有时无）。跟 test_expedition 用同一个种子，好对照。
    import expedition as EXP

    with EXP.G.db() as c:
        c.execute("INSERT OR IGNORE INTO group_run_season(group_id,season_start,"
                  "season_end,act,morale,seed,flags,settled_act,ended,updated_at)"
                  " VALUES(?,?,?,1,5,'fixtureseed','{}',0,0,?)",
                  (gid, "2026-11-16", "2026-12-13", EXP.G._now()))
    st0 = _call("20_state_act1_fresh", "GET",
                f"/v1/group/{gid}/run/state?today={w1}",
                "刚进第一幕：岔路未选、支线未选。这是首页要渲染的形状",
                token=t["owner"])
    for who in ("member", "third"):
        _call(f"20b_state_act1_{who}", "GET",
              f"/v1/group/{gid}/run/state?today={w1}",
              "其他人的 state（抽到的支线不同）", token=t[who])

    pool_path = HERE / "content" / "expedition_events.json"
    if not pool_path.is_file():
        raise SystemExit(
            f"缺少事件池：{pool_path}\n"
            "这份内容不进仓库（见 .gitignore）。从 preview/story/pool.json 拷一份过来：\n"
            "  cp ../../preview/story/pool.json content/expedition_events.json")
    pool = json.loads(pool_path.read_text("utf-8"))
    fork = st0["fork"]
    opt = _brave_option(pool, fork["id"])
    CTX["fork_id"] = fork["id"]
    CTX["chosen"] = opt
    CTX["other"] = next(o["id"] for o in fork["options"] if o["id"] != opt)
    _call("21_fork_choose", "POST", f"/v1/group/{gid}/run/fork",
          "选岔路。返回里带 `result`（选完的结论文案），**不含 effect**",
          body={"option": opt, "today": w1}, token=t["owner"])
    _call("22_fork_repeat", "POST", f"/v1/group/{gid}/run/fork",
          "重复选同一条：200 + already=true（幂等，刷新页面不会报错）",
          body={"option": opt, "today": w1}, token=t["owner"])
    _call("23_state_act1_chosen", "GET", f"/v1/group/{gid}/run/state?today={w1}",
          "选完岔路后的 state：`fork.chosen` 有值、`fork.result` 出现",
          token=t["owner"])
    _call("23b_err_fork_twice", "POST", f"/v1/group/{gid}/run/fork",
          "已经选过、又选了另一条 → 409。必须拿**这条岔路的另一个选项**去撞，"
          "拿别的岔路的 id 会得到 400「没有这个选项」，测不到这条分支",
          body={"option": CTX["other"], "today": w1},
          token=t["owner"], expect=409)
    _call("23c_err_trial_wrong_act", "POST", f"/v1/group/{gid}/run/trial",
          "报了别的幕的试炼 → 409（第四幕的试炼，在第一幕报）",
          body={"trial_id": "snow_drift_8k", "done": True, "today": w1},
          token=t["owner"], expect=409)

    # 支线：挑一条真正带 options 的（试炼没有 options，走 /trial）
    side = next((e for e in st0["events"] if e.get("options")), None)
    if side:
        _call("24_event_choose", "POST", f"/v1/group/{gid}/run/event",
              "选支线选项",
              body={"slot": st0["events"].index(side),
                    "option": side["options"][0]["id"], "today": w1},
              token=t["owner"])
        _call("25_event_repeat", "POST", f"/v1/group/{gid}/run/event",
              "重复选：already=true",
              body={"slot": st0["events"].index(side),
                    "option": side["options"][0]["id"], "today": w1},
              token=t["owner"])

    # 试炼必须出现在样例里，但它是抽出来的（每槽 40%）。这里无条件补上：
    # 抽到了就沿用，没抽到就把最后一个槽位换成本幕的一条试炼。样例文件数是
    # 固定的，前端不会因为「这次没抽到」少一个参考。
    tid1 = _force_trial(gid, 1, 1, pool)
    trial = {"id": tid1, "quality": _quality(pool, tid1)}
    _call("26_trial_done", "POST", f"/v1/group/{gid}/run/trial",
          "试炼达标。**带分圈证据**由服务端复算：`level: verified` 拿全额格。"
          "`evidence` 只交原始分圈（距离/用时/步频/心率/爬升），不交判定结论",
          body={"trial_id": trial["id"], "done": True, "today": w1,
                "evidence": _trial_evidence(trial.get("quality"), True, w1)},
          token=t["owner"])
    _call("27_trial_repeat", "POST", f"/v1/group/{gid}/run/trial",
          "同结果重复报：already=true",
          body={"trial_id": trial["id"], "done": True, "today": w1}, token=t["owner"])

    _call("28_state_act1_done", "GET", f"/v1/group/{gid}/run/state?today={w1}",
          "第一幕做完的样子：岔路有 result、支线有 chosen、进度条动了",
          token=t["owner"])

    print("\n3. 远征 · 第二幕与受困")
    w2 = "2026-11-24"       # 第二幕内
    st2 = _call("30_state_act2", "GET", f"/v1/group/{gid}/run/state?today={w2}",
                "跨幕后进入第二幕：`season.act` 变 2，上一幕的结果已结算",
                token=t["owner"])
    _call("30b_outcome_progress", "GET", f"/v1/group/{gid}/run/outcome?today={w2}",
          "赛季未结束：只给进度，**不剧透结局**", token=t["owner"])

    # 造受困：补给见底 + 冒险分支 + 试炼失败
    # 补给用 SQL 压到 1（真实流程里连选几次扣补给的选项就能到，这里为了稳定直接设）
    import groups as G
    with G.db() as c:
        c.execute("UPDATE group_run_member SET supply=1 WHERE group_id=? AND user_id=1",
                  (gid,))
    fork2 = st2["fork"]
    brave = _brave_option(pool, fork2["id"])
    _call("31_fork_act2", "POST", f"/v1/group/{gid}/run/fork",
          "第二幕岔路（选冒险项，后面才能受困）",
          body={"option": brave, "today": w2}, token=t["owner"])
    tid2 = _force_trial(gid, 1, 2, pool)
    trial2 = {"id": tid2, "quality": _quality(pool, tid2)}
    _call("32_trial_fail_stranded", "POST", f"/v1/group/{gid}/run/trial",
          "**交了证据但没达标**：`level: failed`，扣补给 → 加上冒险分支 → "
          "当场受困，返回 `me_stranded: true`。"
          "（叫 me_ 前缀是因为 state 里的 `stranded` 是全组名单数组，同名会撞）",
          body={"trial_id": trial2["id"], "done": True, "today": w2,
                "evidence": _trial_evidence(trial2.get("quality"), False, w2)},
          token=t["owner"])
    _call("33_state_stranded", "GET", f"/v1/group/{gid}/run/state?today={w2}",
          "受困后的 state：`stranded` 非空、`me.status` 变 stranded",
          token=t["owner"])
    _call("34_rescue", "POST", f"/v1/group/{gid}/run/rescue",
          "队友救援：受困者转 rescued，救援者 −1 补给 −1 格",
          body={"user_id": 1, "today": w2}, token=t["member"])
    _call("35_state_rescued", "GET", f"/v1/group/{gid}/run/state?today={w2}",
          "救完的样子：`stranded` 空了，那个人 status 是 rescued",
          token=t["member"])

    print("\n4. 远征 · 终幕与结局")
    # 先把四周的数据灌满再走到赛季末。只灌前两周的话，后面两幕所有人都会被
    # 「连续没动」剔除，段目标缩到只有一两个人的量，结算样例看着像 bug。
    _seed_weeks(gid, t)
    w4 = "2026-12-08"
    _call("40_state_act4", "GET", f"/v1/group/{gid}/run/state?today={w4}",
          "第四幕：`season.act` = 4，进度是累计值不是当幕值",
          token=t["owner"])
    _call("41_target_set", "POST", f"/v1/group/{gid}/run/target",
          "报本周目标（只能**下调**，上调会被拒）",
          body={"week_target_km": 30, "today": w4}, token=t["owner"])
    _call("42_outcome_ended", "GET", f"/v1/group/{gid}/run/outcome?today=2026-12-20",
          "赛季结束：`ended=true` + 结局 + `relics`。`ending` 取值见 index.md",
          token=t["owner"])
    _call("43_state_ended", "GET", f"/v1/group/{gid}/run/state?today=2026-12-20",
          "**赛季结束后再打开 App** 会拿到这个：`season.ended` 为 true，"
          "`act` 停在最后一幕。前端要据此显示「本赛季结束」而不是继续催跑步",
          token=t["owner"])

    # 隐藏结局：遗迹是 flags 里的 tag 数组（relic_1/2/3），三个齐了就是「发现者」
    import groups as G2
    with G2.db() as c:
        row = c.execute("SELECT flags FROM group_run_season WHERE group_id=?", (gid,)).fetchone()
        flags = json.loads(row["flags"] or "{}")
        flags["relics"] = ["relic_1", "relic_2", "relic_3"]
        c.execute("UPDATE group_run_season SET flags=? WHERE group_id=?",
                  (json.dumps(flags, ensure_ascii=False), gid))
    _call("44_outcome_discoverer", "GET",
          f"/v1/group/{gid}/run/outcome?today=2026-12-20",
          "隐藏结局：三处遗迹集齐 → `ending: discoverer`，`relics` 是 tag 数组",
          token=t["owner"])


def _seed_weeks(gid: int, t: dict) -> None:
    """给三个人各灌满四周（每周四天），让结算有真实感。"""
    weights = {"owner": 9.0, "member": 8.0, "third": 7.0}
    for who, km in weights.items():
        days = []
        for d in range(28):
            date = (dt.date.fromisoformat(SEASON_START) + dt.timedelta(days=d))
            if d % 7 in (0, 1, 2, 4, 5):
                days.append({"date": date.isoformat(),
                             "distance_m": int(km * 1000),
                             "duration_s": int(km * 330),
                             "acts": 1, "plan_done": d % 7 != 4})
        _req("POST", f"/v1/group/{gid}/report", {"days": days}, t[who])


def _brave_option(pool: dict, fork_id: str) -> str:
    """取这条岔路里「正向 advance」的选项 id——冒险项。

    不能按 options[0] 猜：`hiker_again` 的零号是「抬人下山」（advance −1），
    那才是稳妥项。猜错会让受困永远造不出来。
    """
    for e in pool["events"]:
        if e["id"] == fork_id:
            for o in e.get("options") or []:
                if int((o.get("effect") or {}).get("advance") or 0) > 0:
                    return o["id"]
            raise AssertionError(f"{fork_id} 没有正向 advance 的选项")
    raise AssertionError(f"找不到岔路 {fork_id}")


# ---------------------------------------------------------------- 错误
def _errors(gid: int, t: dict) -> None:
    print("\n5. 错误返回（前端要按状态码分支，必须知道长什么样）")
    _call("50_err_not_member", "GET", "/v1/group/9999/run/state?today=2026-11-17",
          "不是这个组的成员 → **403**（不是 404；实现上选的是 403）",
          token=t["owner"], expect=403)
    _call("51_err_no_token", "GET", f"/v1/group/{gid}/run/state?today=2026-11-17",
          "没带令牌 → 401，前端要跳登录**不要重试**", expect=401)
    # 「已选过」「试炼不属于当前幕」这两条依赖当前幕，必须在第一幕进行时打。
    # 时间轴是单向的：赛季推进到第四幕后再传第一幕的日期，ensure 仍按第四幕算，
    # 拿第一幕的选项去撞只会得到 400「没有这个选项」。
    _call("54_err_rescue_self", "POST", f"/v1/group/{gid}/run/rescue",
          "救自己 → 400", body={"user_id": 1, "today": "2026-12-08"},
          token=t["owner"], expect=400)
    _call("55_err_target_up", "POST", f"/v1/group/{gid}/run/target",
          "目标只能下调，往上报 → 409",
          body={"week_target_km": 200, "today": "2026-12-08"},
          token=t["owner"], expect=409)
    _call("56_err_group_not_found", "GET", "/v1/group/9999",
          "组不存在或已解散 → 404", token=t["owner"], expect=404)


# ---------------------------------------------------------------- 索引
def _write_index(gid: int) -> None:
    lines = [
        "# 接口样例（真实返回，非手写）",
        "",
        "由 `server/dump_fixtures.py` 生成，跑一遍完整赛季后把每个接口的响应原样存下。",
        "**改接口后要重跑**，否则这里的结构会跟实现漂移。",
        "",
        "> 这些样例里含**剧情正文**（`state` 会把事件文案下发给客户端）。",
        "> 所以和 `server/content/` 一样**不能进仓库**——仓库有公开镜像，",
        "> 提交上去等于把整季文案公开。",
        "",
        "```bash",
        "cd server && python dump_fixtures.py",
        "```",
        "",
        f"样例组的 id = `{gid}`，三个账号：组长 `{ACC['owner']}`、成员 "
        f"`{ACC['member']}`、第三人 `{ACC['third']}`。",
        "",
        "## 文件",
        "",
        "| 文件 | 方法 | 路径 | 状态 | 说明 |",
        "|---|---|---|---|---|",
    ]
    for name, method, path, st, note in saved:
        lines.append(f"| `{name}.json` | {method} | `{path}` | {st} | {note} |")

    lines += [
        "",
        "## 枚举值（画页面时用来穷举分支）",
        "",
        "| 字段 | 取值 | 含义 |",
        "|---|---|---|",
        "| `outcome.ending` | `arrive` | 抵达望雪站，士气还够 |",
        "| | `detour` | 走到了，但队伍散了（士气 < 3） |",
        "| | `lost` | 没走完 |",
        "| | `discoverer` | 隐藏结局：三处遗迹集齐 |",
        "| `me.status` | `moving` | 正常 |",
        "| | `stranded` | 受困，本幕不产格，等队友来救 |",
        "| | `rescued` | 被救过，恢复正常 |",
        "| `event.type` | `trial` | 试炼：带 `quality`，走 `/run/trial` |",
        "| | `supply` | 补给：带 `options`，走 `/run/event` |",
        "| | `story` | 叙事：带 `options`（`kind == fork` 的是岔路） |",
        "| `share` | `summary` / `off` | 只共享里程 / 完全关闭 |",
        "| `member.role` | `owner` / `admin` / `member` | 决定能否踢人、能否看到邀请码 |",
        "| `challenge.kind` | `distance` / `acts` | 按里程还是按次数 |",
        "",
        "`ending` 只在 `outcome.ended == true` 时出现。没结束时不要猜。",
        "",
        "## 画页面时容易踩的几点",
        "",
        "1. **`member_count` 才是人数**。`members` 在列表里不存在，只在详情里是数组。",
        "   同一个名字两种类型是上一版留下的坑，别再用错。",
        "2. **选项没有 `effect`**。只有 `label` 和 `brief`（以及 `time`，表示按住几天）。",
        "   效果由服务端在选中时结算。前端**不要**根据文案去猜谁更划算。",
        "3. **试炼没有 `options`**。`type == \"trial\"` 的事件带 `quality`（要跑什么），",
        "   走 `/run/trial` 报结果，不是走 `/run/event`。",
        "4. **`chosen` 与 `result` 是同进同退的**。没选过时 `chosen` 是空串、",
        "   `result` 也是空串；选过之后两者都有值。判断「选过没有」看 `chosen`。",
        "5. **`progress.target` 是累计的**。第四幕的目标包含了前三幕，不是当幕的量。",
        "6. **`me.supply` 在没进这一幕时会返回 0**。要和 `me.status` 一起看，",
        "   不能只凭 supply 判断「没补给了」。",
        "7. **所有接口都要带 `today`**。服务器不知道用户时区，「第几幕」按用户本地日期算。",
        "   不带就按服务器当天算，跨时区会差一天。",
        "8. **本周目标要显示 `me.week_target_effective`，不是 `week_target_km`**。",
        "   后者是用户自己报的值，0 表示没报过；真正参与计算的是生效值（没报按 25 km）。",
        "   只显示前者会变成「本周目标 0 km」，而进度条按 25 算，对不上。",
        "9. **`stranded[].since` 是 unix 秒**。救援窗口 24 小时，倒计时前端自己算。",
        "10. **赛季结束后 `state` 仍返回 200**（见 `43_state_ended.json`）。",
        "   `season.ended == true` 时要显示结算页，而不是继续显示「本周还没跑」。",
        "",
        "### 两个故意的「同名不同类型」（`fields.md` 会报出来）",
        "",
        "| 字段 | 谁 | 类型 | 为什么保留 |",
        "|---|---|---|---|",
        "| `id` | `group.id` / `challenge.id` | 整数 | 数据库自增主键 |",
        "| | `events[].id` / `fork.id` | 字符串 | 事件 id 是内容里的标识（如 `snow_bridge`），",
        "| | | | 换季要能对上，不能是自增数字 |",
        "| `target` | `challenge.target` | 整数 | 赛事目标，按米或次数 |",
        "| | `progress.target` | 小数 | 段目标，按**格**（含 0.8 系数，会有小数） |",
        "",
        "客户端建模时**不要**写一个通用的 `id: Int` 或 `target: Int`，会撞。",
        "按语义分开命名（`groupId` / `eventId` / `challengeTarget` / `stageTarget`）。",
        "",
        "### 两个故意的「同名不同类型」（`fields.md` 会报出来）",
        "",
        "| 字段 | 谁 | 类型 | 为什么保留 |",
        "|---|---|---|---|",
        "| `id` | `group.id` / `challenge.id` | 整数 | 数据库自增主键 |",
        "| | `events[].id` / `fork.id` | 字符串 | 事件 id 是内容里的标识（如 `snow_bridge`），",
        "| | | | 换季要能对上，不能是自增数字 |",
        "| `target` | `challenge.target` | 整数 | 赛事目标，按米或次数 |",
        "| | `progress.target` | 小数 | 段目标，按**格**（含 0.8 系数，会有小数） |",
        "",
        "客户端建模时**不要**写一个通用的 `id: Int` 或 `target: Int`，会撞。",
        "按语义分开命名（`groupId` / `eventId` / `challengeTarget` / `stageTarget`）。",
        "",
        "## 状态码",
        "",
        "| 码 | 含义 | 前端怎么做 |",
        "|---|---|---|",
        "| 200 | 成功 | 正常渲染 |",
        "| 400 | 参数不对（如救自己） | 提示后不重试 |",
        "| 401 | 没登录 / 令牌失效 | 引导去登录，**不要**重试 |",
        "| 403 | 已登录，但不是这个组的成员 | 提示「你不在这个组」，回组列表 |",
        "| 404 | 组不存在 / 已解散 | 提示后回组列表，别反复重试 |",
        "| 409 | 状态冲突（已选过、窗口关了、目标不能上调） | 刷新 state 后重试一次 |",
        "| 503 | 组功能或赛季内容未启用 | 提示服务未就绪，稍后再试 |",
        "",
    ]
    (OUT / "index.md").write_text("\n".join(lines), encoding="utf-8")
    _write_fields()


_EMPTY = object()      # 「这个数组在这些样例里都是空的」的占位，不能当示例值


def _walk(node, prefix: str, acc: dict) -> None:
    """把 JSON 摊平成「路径 → 类型集合 + 示例」的字典。"""
    if isinstance(node, dict):
        for k, v in node.items():
            _walk(v, f"{prefix}.{k}" if prefix else k, acc)
    elif isinstance(node, list):
        if not node:
            slot = acc.setdefault(prefix + "[]", {"types": set(), "sample": _EMPTY})
            return
        if acc.get(prefix + "[]", {}).get("sample") is _EMPTY:
            acc[prefix + "[]"]["sample"] = None      # 有真值了，清掉占位
        for v in node:
            _walk(v, prefix + "[]", acc)
    else:
        slot = acc.setdefault(prefix, {"types": set(), "sample": None})
        slot["types"].add(type(node).__name__)
        if slot["sample"] is None or slot["sample"] is _EMPTY:
            slot["sample"] = node


def _write_fields() -> None:
    """扫所有样例，生成字段速查表。

    这张表的价值在于暴露**同一路径出现不同类型**的地方——那是最容易让前端
    在真机上崩溃的坑（`members` 在列表里是人数、在详情里是数组，就是这个来源）。
    """
    acc: dict[str, dict] = {}
    for f in sorted(OUT.glob("*.json")):
        try:
            _walk(json.loads(f.read_text(encoding="utf-8")), "", acc)
        except json.JSONDecodeError:
            continue

    type_zh = {"str": "字符串", "int": "整数", "float": "小数",
               "bool": "布尔", "NoneType": "null"}
    lines = [
        "# 字段速查",
        "",
        "由 `dump_fixtures.py` 扫所有样例自动生成。路径里的 `[]` 表示数组元素。",
        "",
        "> **看最后一列**。同一个路径出现多种类型的地方，画页面时必须两种都处理，",
        "> 否则会在真机上崩。",
        "",
        "| 字段 | 类型 | 示例 |",
        "|---|---|---|",
    ]
    clash = []
    for path in sorted(acc, key=lambda x: (x.count("."), x)):
        info = acc[path]
        types = info["types"] or {"NoneType"}
        shown = " / ".join(type_zh.get(t, t) for t in sorted(types))
        sample = info["sample"]
        if isinstance(sample, bool):
            text = "true" if sample else "false"
        elif sample is None:
            text = "null"
        elif isinstance(sample, float):
            text = f"{sample:g}"
        elif sample is _EMPTY:
            text = "（样例里都是空数组）"
        else:
            text = str(sample)
        text = text.replace("|", "\\|")
        if len(text) > 42:
            text = text[:39] + "…"
        lines.append(f"| `{path}` | {shown} | `{text}` |")
        if len(types) > 1:
            clash.append(f"`{path}`：{shown}")

    lines += ["", "## ⚠ 类型冲突", ""]
    if clash:
        lines += [f"- {c}" for c in clash]
        lines += ["", "上面这些地方在不同接口/状态下类型不同，**不能只按一种写**。"]
    else:
        lines += ["同一路径在所有样例里类型一致。"]

    # 名字相同、但不是同一路径（比如 `stranded` 是布尔、`stranded[]` 是数组）。
    # 这类冲突扫路径扫不出来，但客户端统一建模时会直接撞名。
    by_name: dict[str, dict[str, set]] = {}
    for path, info in acc.items():
        leaf = path.split(".")[-1]
        kind = "数组" if leaf.endswith("[]") else "/".join(
            type_zh.get(t, t) for t in sorted(info["types"] or {"NoneType"}))
        by_name.setdefault(leaf.replace("[]", ""), {}).setdefault(kind, set()).add(path)
    name_clash = [(k, v) for k, v in by_name.items() if len(v) > 1]
    lines += ["", "### 同名字段不同类型", ""]
    if name_clash:
        for name, kinds in sorted(name_clash):
            detail = "；".join(f"{k}（{'、'.join(sorted(ps))}）" for k, ps in kinds.items())
            lines.append(f"- `{name}`：{detail}")
        lines += ["", "同一份响应里不会同时出现，但客户端如果按统一模型建模会撞。"]
    else:
        lines += ["无。"]

    (OUT / "fields.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  字段速查 → fields.md（{len(acc)} 个路径，{len(clash)} 处类型冲突）")


if __name__ == "__main__":
    raise SystemExit(main())
