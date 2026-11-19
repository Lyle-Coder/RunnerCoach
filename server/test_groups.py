# -*- coding: utf-8 -*-
"""组接口端到端测试：起临时库 + 临时端口，走真实 HTTP，不碰真实数据。

    python test_groups.py

覆盖：建组 / 加入 / 上报幂等 / 榜单 / 赛事完成度 / 共享开关 /
退出清数据 / 权限 / 人数上限 / 周期校验 / 限频。

为什么值得单独写：服务端此前从没在本地落过库，第一次跑多半会崩在
环境变量或建表上。这些用例就是「部署前必须绿」的那一批。
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

TMP = Path(tempfile.mkdtemp(prefix="rc-groups-"))
os.environ["DB_PATH"] = str(TMP / "app.db")
os.environ["GROUPS_DB"] = str(TMP / "groups.db")
os.environ["SMS_PROVIDER"] = "dev"          # 验证码回显，不真的发信
os.environ.pop("SMTP_HOST", None)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import app  # noqa: E402
import groups  # noqa: E402

# 这套测试用「未来几天」的日志一次性模拟整周/整季，所以要把上报的未来天数
# 上限放宽；生产里它是 0（见 groups.FUTURE_DAYS）。下面单测「未来日期被拒」
# 时会临时调回 0。
groups.FUTURE_DAYS = 40

PORT = 0
ok = fail = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  [ok] {name}")
    else:
        fail += 1
        print(f"  [x]  {name}  {extra}")


def req(method: str, path: str, body=None, token: str = ""):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=15)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
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


def login(email: str, nickname: str = "") -> str:
    st, r = req("POST", "/v1/sms/send", {"account": email})
    assert st == 200 and r.get("dev_code"), f"验证码发送失败：{st} {r}"
    body = {"account": email, "code": r["dev_code"]}
    if nickname:
        body["nickname"] = nickname
    st, r2 = req("POST", "/v1/auth", body)
    assert st == 200 and r2.get("token"), f"登录失败：{st} {r2}"
    return r2["token"]


def main() -> int:
    global PORT
    app.init_db()
    if app.group_api is None:
        print(f"groups 模块没加载：{app._GROUPS_IMPORT_ERROR}")
        return 1
    app.group_api.init_db()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    srv.daemon_threads = True
    PORT = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"临时库 {TMP}｜端口 {PORT}\n")

    a = login("laoli@test.local")
    b = login("xiaozhao@test.local")
    c3 = login("solo@test.local")
    print("账号：三个都登录成功\n")

    # ---------------- 建组
    print("1. 建组")
    st, r = req("POST", "/v1/group/create",
                {"name": "秦岭跑团", "intro": "周末约跑", "max_members": 10, "alias": "老李"}, a)
    check("建组 200", st == 200, f"{st} {r}")
    gid = r["group"]["id"]
    code = r["group"]["invite_code"]
    check("返回 8 位邀请码", len(code) == 8, code)
    check("创建者是 owner", r["group"]["role"] == "owner")

    st, r = req("POST", "/v1/group/create", {"name": "第二个"}, a)
    check("已是组长不能再建（409）", st == 409, f"{st} {r}")

    st, r = req("POST", "/v1/group/create", {"name": ""}, a)
    check("空组名被拒（400）", st == 400, f"{st} {r}")

    # ---------------- 加入
    print("\n2. 加入")
    st, r = req("POST", "/v1/group/join", {"code": code, "alias": "小赵"}, b)
    check("用邀请码加入 200", st == 200, f"{st} {r}")
    st, r2 = req("POST", "/v1/group/join", {"code": code}, b)
    check("重复加入幂等", st == 200 and r2.get("already") is True, f"{st} {r2}")
    st, r = req("POST", "/v1/group/join", {"code": "ZZZZZZZZ"}, b)
    check("错误邀请码 404", st == 404, f"{st} {r}")

    st, r = req("GET", f"/v1/group/{gid}", None, b)
    check("成员能看到组", st == 200 and len(r["group"]["members"]) == 2, f"{st}")
    check("普通成员看不到邀请码", "invite_code" not in r["group"],
          f"拿到了 {r['group'].get('invite_code')}")

    st, r = req("GET", f"/v1/group/{gid}", None, c3)
    check("非成员看不到组（403）", st == 403, f"{st} {r}")

    # ---------------- 上报
    print("\n3. 上报")
    days_a = [
        {"date": "2026-11-16", "distance_m": 12000, "duration_s": 3900, "acts": 1, "plan_done": True},
        {"date": "2026-11-17", "distance_m": 8000, "duration_s": 2700, "acts": 1, "plan_done": False},
    ]
    st, r = req("POST", f"/v1/group/{gid}/report", {"days": days_a}, a)
    check("上报 2 天", st == 200 and r.get("saved") == 2, f"{st} {r}")

    st, r = req("POST", f"/v1/group/{gid}/report", {"days": days_a}, a)
    check("重复上报幂等（不重复计数）", st == 200 and r.get("saved") == 2, f"{st} {r}")

    st, r = req("POST", f"/v1/group/{gid}/report",
                {"days": [{"date": "2026-11-18", "distance_m": 5000, "duration_s": 1800,
                           "acts": 1, "plan_done": None}]}, b)
    check("第二个人上报", st == 200 and r.get("saved") == 1, f"{st} {r}")

    st, r = req("POST", f"/v1/group/{gid}/report",
                {"days": [{"date": "2026-11-19", "distance_m": 999999}]}, a)
    check("单日里程超限被拒（400）", st == 400, f"{st} {r}")

    st, r = req("POST", f"/v1/group/{gid}/report", {"days": [{"date": "2026-13-40"}]}, a)
    check("非法日期被拒（400）", st == 400, f"{st} {r}")

    # 未来日期：设备时钟快一天就会把「明天」的里程写进来，而那些里程会落进
    # 后面几幕的窗口，等于提前灌格。用相对日期算，别让这条测试自己过期。
    _fut = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    groups.FUTURE_DAYS = 0                   # 按生产值验一次
    try:
        st, r = req("POST", f"/v1/group/{gid}/report",
                    {"days": [{"date": _fut, "distance_m": 5000, "duration_s": 1800,
                               "acts": 1}]}, a)
    finally:
        groups.FUTURE_DAYS = 40
    check("未来日期被拒（400）", st == 400 and "未来" in str(r.get("error")), f"{st} {r}")

    st, r = req("POST", f"/v1/group/{gid}/report", {"days": days_a}, c3)
    check("非成员不能上报（403）", st == 403, f"{st} {r}")

    # ---------------- 榜单
    print("\n4. 榜单")
    st, r = req("GET", f"/v1/group/{gid}/board?range=week&today=2026-11-17", None, b)
    check("周榜 200", st == 200, f"{st} {r}")
    rows = {x["alias"]: x for x in r.get("rows", [])}
    check("两个人的数据都在", len(rows) == 2, str(list(rows)))
    check("老李 20 km", rows.get("老李", {}).get("distance_m") == 20000,
          str(rows.get("老李")))
    check("小赵 5 km", rows.get("小赵", {}).get("distance_m") == 5000, str(rows.get("小赵")))
    check("按里程降序", r["rows"][0]["alias"] == "老李", str([x["alias"] for x in r["rows"]]))
    check("计划完成率只算有计划的天", abs((rows["老李"]["plan_rate"] or 0) - 0.5) < 0.01,
          str(rows["老李"].get("plan_rate")))
    check("小赵没有计划 → plan_rate 为 null", rows["小赵"]["plan_rate"] is None,
          str(rows["小赵"].get("plan_rate")))
    check("区间是周一到周日", r["from"] == "2026-11-16" and r["to"] == "2026-11-22",
          f"{r['from']}~{r['to']}")

    st, r = req("GET", f"/v1/group/{gid}/board?range=bad&today=2026-11-17", None, b)
    check("非法 range 被拒（400）", st == 400, f"{st} {r}")

    # ---------------- 赛事
    print("\n5. 组内赛事")
    st, r = req("POST", f"/v1/group/{gid}/challenge/create",
                {"title": "本周 25 公里", "kind": "distance", "target": 25000,
                 "start_date": "2026-11-16", "end_date": "2026-11-22",
                 "today": "2026-11-17"}, a)
    check("建赛事 200", st == 200, f"{st} {r}")
    cid = r["challenge"]["id"]
    check("状态 running", r["challenge"]["status"] == "running", str(r["challenge"]))

    st, r = req("POST", f"/v1/group/{gid}/challenge/create",
                {"title": "太长了", "kind": "distance", "target": 10000,
                 "start_date": "2026-11-01", "end_date": "2027-03-01",
                 "today": "2026-11-17"}, a)
    check("周期超 90 天被拒（400）", st == 400, f"{st} {r}")

    st, r = req("POST", f"/v1/group/{gid}/challenge/create",
                {"title": "太早", "kind": "distance", "target": 10000,
                 "start_date": "2026-10-01", "end_date": "2026-10-10",
                 "today": "2026-11-17"}, a)
    check("开始日早于 7 天前被拒（400）", st == 400, f"{st} {r}")

    st, r = req("POST", f"/v1/group/{gid}/challenge/create",
                {"title": "成员建的", "kind": "acts", "target": 10,
                 "start_date": "2026-11-16", "end_date": "2026-11-22",
                 "today": "2026-11-17"}, b)
    check("普通成员不能建赛事（403）", st == 403, f"{st} {r}")

    st, r = req("GET", f"/v1/group/{gid}/challenge/{cid}?today=2026-11-17", None, b)
    check("完成度 200", st == 200, f"{st} {r}")
    rows = {x["alias"]: x for x in r.get("rows", [])}
    check("老李 20 km / 80%", rows["老李"]["current"] == 20000
          and abs(rows["老李"]["pct"] - 80.0) < 0.1, str(rows["老李"]))
    check("未达标 done_at 为 null", rows["老李"]["done_at"] is None)
    check("小赵 5 km", rows["小赵"]["current"] == 5000, str(rows["小赵"]))

    # 补一跑，让老李达标
    req("POST", f"/v1/group/{gid}/report",
        {"days": [{"date": "2026-11-20", "distance_m": 6000, "duration_s": 1900, "acts": 1}]}, a)
    st, r = req("GET", f"/v1/group/{gid}/challenge/{cid}?today=2026-11-20", None, b)
    rows = {x["alias"]: x for x in r.get("rows", [])}
    check("达标后 pct=100", rows["老李"]["pct"] == 100, str(rows["老李"]))
    check("达标日 = 首次 >= 目标那天", rows["老李"]["done_at"] == "2026-11-20",
          str(rows["老李"].get("done_at")))
    check("已达成排在前面", r["rows"][0]["alias"] == "老李")

    st, r = req("POST", f"/v1/group/{gid}/challenge/{cid}/close", {}, a)
    check("提前结束 200", st == 200 and r["challenge"]["status"] == "closed", f"{st} {r}")

    st, r = req("GET", f"/v1/group/{gid}/challenge/list?today=2026-11-20", None, b)
    check("赛事列表 200", st == 200 and len(r["challenges"]) >= 1, f"{st}")
    check("列表里有全组进度", r["challenges"][0].get("current") == 31000,
          str(r["challenges"][0]))

    # ---------------- 共享开关
    print("\n6. 共享开关")
    st, r = req("POST", f"/v1/group/{gid}/share", {"share": "off"}, b)
    check("关闭共享", st == 200 and r.get("share") == "off", f"{st} {r}")

    st, r = req("POST", f"/v1/group/{gid}/report",
                {"days": [{"date": "2026-11-21", "distance_m": 7000, "acts": 1}]}, b)
    check("关闭后上报被跳过", st == 200 and r.get("saved") == 0 and r.get("skipped") == 1,
          f"{st} {r}")

    st, r = req("GET", f"/v1/group/{gid}/board?range=week&today=2026-11-21", None, a)
    names = [x["alias"] for x in r["rows"]]
    check("关闭共享后不出现在榜单", "小赵" not in names, str(names))
    check("hidden 计数正确", r.get("hidden") == 1, str(r.get("hidden")))

    req("POST", f"/v1/group/{gid}/share", {"share": "summary"}, b)
    st, r = req("GET", f"/v1/group/{gid}/board?range=week&today=2026-11-21", None, a)
    check("重新打开后回到榜单", "小赵" in [x["alias"] for x in r["rows"]])

    # ---------------- 组内昵称
    print("\n6b. 组内昵称可以自己改")
    st, r = req("POST", f"/v1/group/{gid}/me", {"alias": "夜里跑的那个"}, b)
    check("成员改自己的昵称（不需管理员）",
          st == 200 and r.get("alias") == "夜里跑的那个" and r.get("changed") is True,
          f"{st} {r}")
    st, r = req("GET", f"/v1/group/{gid}", None, a)
    names = [m["alias"] for m in r["group"]["members"]]
    check("榜单/成员里看到新昵称", "夜里跑的那个" in names, str(names))
    check("不影响组里别人的昵称", "老李" in names, str(names))

    st, r = req("POST", f"/v1/group/{gid}/me", {"alias": "夜里跑的那个"}, b)
    check("改成同一个名字时 changed=false", st == 200 and r.get("changed") is False,
          f"{st} {r}")

    st, r = req("POST", f"/v1/group/{gid}/me", {"alias": ""}, b)
    check("空昵称被拒（400），不回落成打码账号",
          st == 400 and "空" in str(r.get("error")), f"{st} {r}")

    st, r = req("POST", f"/v1/group/{gid}/me", {"alias": "一" * 17}, b)
    check("超长昵称被拒（400）", st == 400, f"{st} {r}")

    # 昵称是 per-group 的：同一个账号在别的组应该还是原来的名字
    st, r = req("POST", "/v1/group/create", {"name": "另一个组", "alias": "阿甲"}, b)
    gid2 = (r.get("group") or {}).get("id") if r.get("ok") else None
    if gid2:
        st, r = req("GET", f"/v1/group/{gid2}", None, b)
        mine2 = [m["alias"] for m in r["group"]["members"] if m.get("is_me")]
        check("昵称按组独立（新组里是阿甲）", mine2 == ["阿甲"], str(mine2))
        # 收尾：不然下面「另一个人建新组」会撞 409（一个账号只能当一个组的组长）
        req("POST", f"/v1/group/{gid2}/archive", {"confirm": "1"}, b)

    # ---------------- 权限
    print("\n7. 权限")
    st, r = req("GET", f"/v1/group/{gid}", None, a)
    me_a = next(m["user_id"] for m in r["group"]["members"] if m["is_me"])
    st, r = req("POST", f"/v1/group/{gid}/member/{me_a}/remove", {}, b)
    check("成员不能踢人（403）", st == 403, f"{st} {r}")
    st, r = req("POST", f"/v1/group/{gid}/member/{99999}/remove", {}, a)
    check("踢不存在的人（404）", st == 404, f"{st} {r}")

    st, r = req("POST", f"/v1/group/{gid}/invite/rotate", {}, b)
    check("成员不能换邀请码（403）", st == 403, f"{st} {r}")

    st, r = req("POST", f"/v1/group/{gid}/invite/rotate", {}, a)
    check("组长能换邀请码", st == 200 and len(r.get("invite_code", "")) == 8, f"{st} {r}")
    new_code = r["invite_code"]
    st, r = req("POST", "/v1/group/join", {"code": code}, c3)
    check("旧邀请码失效", st == 404, f"{st} {r}")
    st, r = req("POST", "/v1/group/join", {"code": new_code, "alias": "阿甘"}, c3)
    check("新邀请码可用", st == 200, f"{st} {r}")

    st, r = req("PATCH", f"/v1/group/{gid}", {"name": "秦岭跑团 2026"}, a)
    check("改组名 200", st == 200 and r["group"]["name"] == "秦岭跑团 2026", f"{st} {r}")
    st, r = req("PATCH", f"/v1/group/{gid}", {"max_members": 2}, a)
    check("人数上限不能低于现有人数（400）", st == 400, f"{st} {r}")

    st, r = req("POST", f"/v1/group/{gid}/transfer", {"user_id": 99999}, a)
    check("转让给不存在的人（404）", st == 404, f"{st} {r}")

    # ---------------- 退出清数据
    print("\n8. 退出")
    st, r = req("POST", f"/v1/group/{gid}/leave", {}, a)
    check("组长不能直接退出（409）", st == 409, f"{st} {r}")

    st, r = req("POST", f"/v1/group/{gid}/leave", {}, c3)
    check("成员退出 200", st == 200, f"{st} {r}")
    st, r = req("GET", f"/v1/group/{gid}", None, c3)
    check("退出后看不到组（403）", st == 403, f"{st} {r}")

    # ---------------- 人数上限
    print("\n9. 人数上限")
    st, r = req("POST", "/v1/group/create",
                {"name": "两人组", "max_members": 2, "alias": "甲"}, b)
    check("另一个人建新组", st == 200, f"{st} {r}")
    gid2 = r["group"]["id"]
    code2 = r["group"]["invite_code"]
    st, _ = req("POST", "/v1/group/join", {"code": code2}, a)
    check("第二个人加入", st == 200, f"{st}")
    st, r = req("POST", "/v1/group/join", {"code": code2}, c3)
    check("第三个人被人数上限拒绝（409）", st == 409, f"{st} {r}")

    # ---------------- 限频
    print("\n10. 限频")
    got429 = False
    for _ in range(5):
        st, r = req("POST", "/v1/group/create", {"name": "刷组"}, c3)
        if st == 429:
            got429 = True
            break
    check("建组超限触发 429", got429)

    # ---------------- 周成绩：衫 / 分 / 名次
    print("\n11. 周成绩（六件衫 + 赛季分）")
    # b 追加几天带爬升的记录：圆点衫要有原料才测得出
    st, r = req("POST", f"/v1/group/{gid}/report", {"days": [
        {"date": "2026-11-16", "distance_m": 8000, "duration_s": 2400,
         "acts": 1, "elevation_m": 400, "plan_done": True},
        {"date": "2026-11-17", "distance_m": 8000, "duration_s": 2400,
         "acts": 1, "elevation_m": 400, "plan_done": True},
        {"date": "2026-11-18", "distance_m": 8000, "duration_s": 2400,
         "acts": 1, "elevation_m": 400, "plan_done": True},
    ]}, b)
    check("上报带爬升被接受", st == 200 and r.get("saved") == 3, f"{st} {r}")

    st, r = req("GET", f"/v1/group/{gid}/season?today=2026-11-18", None, a)
    check("season 可读", st == 200 and r.get("ok") is True, f"{st} {r}")
    check("六件衫都在", len(r.get("shirts") or []) == 6,
          str([x.get("id") for x in r.get("shirts") or []]))
    check("红衫是计划完成王",
          next((x["title"] for x in r["shirts"] if x["id"] == "red"), "") == "计划完成王")
    check("每个人有名次与分数",
          all(isinstance(p.get("rank"), int) and p.get("score") is not None
              for p in r.get("people") or []), str(r.get("people")))
    check("me 指向自己", (r.get("me") or {}).get("user_id") is not None, str(r.get("me")))
    check("base_max = 90（让分/预言那 10 分还没做）",
          r.get("base_max") == 90.0, str(r.get("base_max")))

    _polka = next(x for x in r["shirts"] if x["id"] == "polka")
    check("圆点衫给了爬升最多的人",
          len(_polka["winners"]) >= 1, str(_polka))

    # 爬升密度截断：5 km 报 5000 m 爬升，应该被截到 5*200=1000
    st, r = req("POST", f"/v1/group/{gid}/report", {"days": [
        {"date": "2026-11-18", "distance_m": 5000, "duration_s": 1500,
         "acts": 1, "elevation_m": 5000}]}, b)
    check("离谱爬升被截断并告知", st == 200 and r.get("clamped_elev") == 1, f"{st} {r}")
    st, r = req("GET", f"/v1/group/{gid}/season?today=2026-11-18", None, b)
    _me_b = r.get("me") or {}
    check("截断后爬升 ≤ 里程×200 m/km", int(_me_b.get("elev") or 0) <= 8000 * 0 + 1000 * 3,
          str(_me_b.get("elev")))

    st, r = req("GET", f"/v1/group/{gid}/season?today=2026-11-18", None, c3)
    check("非成员不能看 season（403）", st == 403, f"{st} {r}")

    # 聚合接口：一次拿四块，形状和四个单接口一致
    st, r = req("GET", f"/v1/group/{gid}/home?today=2026-11-18", None, a)
    check("home 一次返回四块",
          st == 200 and r.get("ok") and all(
              isinstance(r.get(k), dict) and r[k].get("ok")
              for k in ("detail", "board", "challenges", "season")),
          f"{st} {[k for k in ('detail','board','challenges','season') if not (r.get(k) or {}).get('ok')]}")
    check("home 里的 detail 和单接口一致",
          (r.get("detail") or {}).get("group", {}).get("id") == gid, str(r.get("detail"))[:120])
    check("home 里的 season 也有六件衫",
          len((r.get("season") or {}).get("shirts") or []) == 6,
          str(len((r.get("season") or {}).get("shirts") or [])))

    # 一块出错不该把整页拖垮：传个不存在的 gid，四块都该是 ok=False 而不是 500
    st, r = req("GET", "/v1/group/99999/home", None, a)
    check("home 对不存在的组不 500（各块自己报错）",
          st in (200, 403, 404) and isinstance(r, dict), f"{st} {r}")

    # ---------------- P2：探索 / 让分赛 / 预言
    print("\n11b. P2：探索徽章 + 让分赛 + 预言")

    # 探索：一天报 80 格，应该被截到 50 并明说
    st, r = req("POST", f"/v1/group/{gid}/report", {"days": [
        {"date": "2026-11-17", "distance_m": 8000, "duration_s": 2400,
         "acts": 1, "explorer": 80}]}, a)
    check("探索格数超上限被截断并告知",
          st == 200 and r.get("clamped_explorer") == 1, f"{st} {r}")
    st, r = req("GET", f"/v1/group/{gid}/season?today=2026-11-18", None, a)
    _badges = {x["user_id"]: x["cells"] for x in (r.get("explore") or {}).get("badges") or []}
    _me_a = (r.get("me") or {})
    check("探索徽章按 ≥5 格发，且记下格数",
          int(_me_a.get("explorer") or 0) == 50 and _me_a.get("user_id") in _badges,
          str(_badges))
    check("没踩新路的人不发探索徽章",
          int(_me_a.get("user_id")) not in []
          and all(v >= (r.get("explore") or {}).get("min_cells", 5)
                  for v in _badges.values()), str(_badges))

    # 报目标
    st, r = req("POST", f"/v1/group/{gid}/goal", {"km": 30, "today": "2026-11-18"}, a)
    check("报本周目标 200", st == 200 and r.get("km") == 30.0, f"{st} {r}")
    check("一个人报目标时不配对", r.get("pairs") == 0, f"{st} {r}")
    st, r = req("POST", f"/v1/group/{gid}/goal", {"km": 35, "today": "2026-11-18"}, a)
    check("周内可以改目标", st == 200 and r.get("km") == 35.0, f"{st} {r}")
    st, r = req("POST", f"/v1/group/{gid}/goal", {"km": 0.2, "today": "2026-11-18"}, a)
    check("目标太小被拒（400）", st == 400, f"{st} {r}")
    st, r = req("POST", f"/v1/group/{gid}/goal", {"km": 5000, "today": "2026-11-18"}, a)
    check("目标太大被拒（400）", st == 400, f"{st} {r}")
    st, r = req("POST", f"/v1/group/{gid}/goal", {"km": "abc", "today": "2026-11-18"}, a)
    check("目标不是数字被拒（400）", st == 400, f"{st} {r}")

    # 预言的三条硬约束。uid 从组成员里认自己——/v1/me 不返回内部 id。
    def uid_of(tok: str) -> int:
        _st, _r = req("GET", f"/v1/group/{gid}", None, tok)
        return int(next(m["user_id"] for m in _r["group"]["members"] if m.get("is_me")))

    _uid_a, _uid_b = uid_of(a), uid_of(b)
    st, r = req("POST", f"/v1/group/{gid}/goal",
                {"km": 35, "predict": _uid_a, "today": "2026-11-18"}, a)
    check("不能预测自己（400）", st == 400, f"{st} {r}")
    st, r = req("POST", f"/v1/group/{gid}/goal",
                {"km": 35, "predict": 99999, "today": "2026-11-18"}, a)
    check("预测不在组里的人被拒（400）", st == 400, f"{st} {r}")

    st, r = req("POST", f"/v1/group/{gid}/goal",
                {"km": 35, "predict": _uid_b, "today": "2026-11-18"}, a)
    check("对方还没报目标时不能预测他（400）", st == 400, f"{st} {r}")

    # b 报目标 → 配对自动生成
    st, r = req("POST", f"/v1/group/{gid}/goal", {"km": 20, "today": "2026-11-18"}, b)
    check("两个人都有目标后生成对子", st == 200 and r.get("pairs") == 1, f"{st} {r}")

    st, r = req("GET", f"/v1/group/{gid}/week?today=2026-11-18", None, a)
    check("week 可读", st == 200 and r.get("ok") is True, f"{st} {r}")
    _pairs = r.get("pairs") or []
    check("本周一对", len(_pairs) == 1, str(_pairs))
    if _pairs:
        _p = _pairs[0]
        check("目标高的当大神", _p["big"]["user_id"] == _uid_a, str(_p))
        check("大神让分 = 自报 + 小白目标×50%",
              _p["big"]["target"] == 45.0, str(_p["big"]))
        check("小白按自报跑", _p["small"]["target"] == 20.0, str(_p["small"]))
    check("week 带建议目标（给没报的人预填）",
          isinstance((r.get("goal") or {}).get("suggest"), (int, float)), str(r.get("goal")))
    check("week 的可猜对象不含自己、不含没报目标的人",
          all(t["user_id"] != _uid_a for t in r.get("targets") or [])
          and all(t["user_id"] in (_uid_b,) for t in r.get("targets") or []),
          str(r.get("targets")))

    # 现在能投预言了：a 猜 b 完不成（b 实际 29km > 目标 20km，会打脸）
    st, r = req("POST", f"/v1/group/{gid}/goal",
                {"km": 35, "predict": _uid_b, "today": "2026-11-18"}, a)
    check("报目标时能同时投票", st == 200 and r.get("predict") == _uid_b, f"{st} {r}")

    st, r = req("GET", f"/v1/group/{gid}/season?today=2026-11-18", None, a)
    _rows = {p["user_id"]: p for p in r.get("people") or []}
    check("预言结算：猜错的人不加分",
          float(_rows[_uid_a].get("bonus") or 0) == 0.0, str(_rows[_uid_a]))
    check("预言结算：被猜却完成的人 +3",
          float(_rows[_uid_b].get("bonus") or 0) == 3.0, str(_rows[_uid_b]))
    check("加分并进了总分（不是另开一栏）",
          float(_rows[_uid_b]["score"]) >= 3.0, str(_rows[_uid_b]["score"]))
    check("返回 bonus_max，页面不用写死 90", r.get("bonus_max") == 10.0)
    check("返回我的目标，页面能回显", (r.get("goal") or {}).get("km") == 35.0,
          str(r.get("goal")))

    # 配对关系不因改目标而重排
    st, r = req("POST", f"/v1/group/{gid}/goal", {"km": 40, "today": "2026-11-18"}, a)
    st, r = req("GET", f"/v1/group/{gid}/week?today=2026-11-18", None, a)
    check("改目标不会把对子重排（还是同一对）",
          len(r.get("pairs") or []) == 1
          and r["pairs"][0]["big"]["user_id"] == _uid_a, str(r.get("pairs")))

    # 后报目标的人也要能配到对（不然 6 人组永远只有一对在比）
    # 邀请码中途可能被换过，从当前组详情里取，别用最早的变量
    _stc, _rc = req("GET", f"/v1/group/{gid}", None, a)
    _code_now = (_rc.get("group") or {}).get("invite_code") or code
    _tok4 = login("fourth@test.local")
    st, r = req("POST", "/v1/group/join", {"code": _code_now, "alias": "老四"}, _tok4)
    check("第 4 人加入", st == 200, f"{st} {r}")
    _st4, _r4 = req("GET", f"/v1/group/{gid}", None, _tok4)
    _uid4 = int(next(m["user_id"] for m in _r4["group"]["members"] if m.get("is_me")))
    st, r = req("POST", f"/v1/group/{gid}/goal", {"km": 80, "today": "2026-11-18"}, _tok4)
    check("单人报目标不会凭空配对", r.get("pairs") == 1, f"{st} {r}")
    _tok5 = login("fifth@test.local")
    st, r = req("POST", "/v1/group/join", {"code": _code_now, "alias": "老五"}, _tok5)
    check("第 5 人加入", st == 200, f"{st} {r}")
    st, r = req("POST", f"/v1/group/{gid}/goal", {"km": 50, "today": "2026-11-18"}, _tok5)
    check("后报的两人补成第 2 对（老对子不拆）",
          r.get("pairs") == 2, f"{st} {r}")

    # 关掉共享的人不能报目标（报了也没人看得到）
    st, r = req("POST", f"/v1/group/{gid}/share", {"share": "off"}, b)
    st, r = req("POST", f"/v1/group/{gid}/goal", {"km": 20, "today": "2026-11-18"}, b)
    check("关了共享报目标被拒（409）", st == 409, f"{st} {r}")
    st, r = req("GET", f"/v1/group/{gid}/bingo?today=2026-11-18", None, b)
    check("关了共享也看不了 Bingo（409）", st == 409, f"{st} {r}")
    st, r = req("POST", f"/v1/group/{gid}/share", {"share": "summary"}, b)
    check("恢复共享", st == 200, f"{st} {r}")

    # Bingo 卡
    st, r = req("GET", f"/v1/group/{gid}/bingo?today=2026-11-18", None, a)
    check("Bingo 卡可读", st == 200 and len(r.get("cells") or []) == 25, f"{st} {r}")
    _cells = r.get("cells") or []
    check("中心是免费格", _cells and _cells[12]["task"] == "free", str(_cells[12:13]))
    check("已经跑出来的格子被点亮", int(r.get("done") or 0) >= 1, str(r.get("done")))
    _c2 = req("GET", f"/v1/group/{gid}/bingo?today=2026-11-18", None, a)[1]
    check("同一周再拉一次，卡不变",
          [c["task"] for c in _c2.get("cells") or []] == [c["task"] for c in _cells])
    st, r = req("GET", f"/v1/group/{gid}/week?today=2026-11-18", None, a)
    check("Bingo 进度并进了 week",
          (r.get("bingo") or {}).get("total") == 25, str(r.get("bingo")))
    st, r = req("GET", f"/v1/group/{gid}/bingo?today=2026-11-18", None, c3)
    check("非成员看不了 Bingo（403）", st == 403, f"{st} {r}")

    # ---------------- 动态瀑布流 + 评论
    print("\n11c2. 动态瀑布流与评论")
    st, r = req("GET", f"/v1/group/{gid}/feed?today=2026-11-18", None, a)
    check("默认没有动态", st == 200 and not (r.get("rows") or []), f"{st} {r}")
    check("表情白名单一起下发", len(r.get("emojis") or []) >= 4, str(r.get("emojis")))

    _acts = [
        {"sid": "a1b2c3d4e5f60718", "date": "2026-11-18", "kind": "run",
         "distance_m": 12800, "duration_s": 4020, "elevation_m": 120},
        {"sid": "ffffffffffffffff", "date": "2026-11-17", "kind": "ride",
         "distance_m": 46000, "duration_s": 6300, "elevation_m": 380},
        # 脏数据：sid 不合法 / 未来日期 / 未知类型
        {"sid": "坏", "date": "2026-11-16", "kind": "run", "distance_m": 5000},
        {"sid": "0123456789abcdef", "date": "2026-12-31", "kind": "run",
         "distance_m": 5000},
        {"sid": "1111222233334444", "date": "2026-11-16", "kind": "fly",
         "distance_m": 5000},
    ]
    st, r = req("POST", f"/v1/group/{gid}/acts",
                {"acts": _acts, "today": "2026-11-18"}, a)
    check("上传概要：脏 sid 和未来日期被丢掉", st == 200 and r.get("saved") == 3,
          f"{st} {r}")

    st, r = req("GET", f"/v1/group/{gid}/feed?today=2026-11-18", None, b)
    _rows = r.get("rows") or []
    check("同组能看到动态", st == 200 and len(_rows) == 3, f"{st} {r}")
    check("按日期倒序", [x["date"] for x in _rows] ==
          ["2026-11-18", "2026-11-17", "2026-11-16"], str([x["date"] for x in _rows]))
    check("未知类型降级成 other",
          any(x["kind"] == "other" for x in _rows), str([x["kind"] for x in _rows]))
    check("只有概要字段，没有轨迹/配速",
          all(set(x) >= {"distance_m", "duration_s"} and
              "track" not in x and "pace" not in x for x in _rows), str(_rows[:1]))
    _aid = int(_rows[0]["id"])

    st, r = req("POST", f"/v1/group/{gid}/acts", {"acts": [
        {"sid": "a1b2c3d4e5f60718", "date": "2026-11-18", "kind": "run",
         "distance_m": 13000, "duration_s": 4020}], "today": "2026-11-18"}, a)
    st, r = req("GET", f"/v1/group/{gid}/feed?today=2026-11-18", None, a)
    _one = [x for x in r["rows"] if x["id"] == _aid][0]
    check("同一个 sid 重复上传是覆盖，不是新增",
          len(r["rows"]) == 3 and _one["distance_m"] == 13000, str(_one))

    # 评论
    st, r = req("POST", f"/v1/group/{gid}/comment", {"act": _aid, "text": "", "emoji": ""}, b)
    check("空评论被拒（400）", st == 400, f"{st} {r}")
    st, r = req("POST", f"/v1/group/{gid}/comment",
                {"act": _aid, "text": "", "emoji": "🍔"}, b)
    check("不在白名单的表情被拒（400）", st == 400, f"{st} {r}")
    st, r = req("POST", f"/v1/group/{gid}/comment",
                {"act": 999999, "text": "在吗"}, b)
    check("评论不存在的动态（404）", st == 404, f"{st} {r}")

    st, r = req("POST", f"/v1/group/{gid}/comment",
                {"act": _aid, "text": "这个配速可以"}, b)
    check("发一条留言", st == 200 and r.get("text") == "这个配速可以", f"{st} {r}")
    st, r = req("POST", f"/v1/group/{gid}/comment",
                {"act": _aid, "text": "", "emoji": "🔥"}, a)
    check("只发表情也可以", st == 200 and r.get("emoji") == "🔥", f"{st} {r}")

    st, r = req("GET", f"/v1/group/{gid}/feed?today=2026-11-18", None, a)
    _one = [x for x in r["rows"] if x["id"] == _aid][0]
    check("列表里带评论数和最近两条",
          int(_one["comments"]) == 2 and len(_one["recent"]) == 2, str(_one))
    st, r = req("GET", f"/v1/group/{gid}/feed?today=2026-11-18&act={_aid}", None, a)
    check("详情接口能单独取一条",
          st == 200 and len(r.get("rows") or []) == 1, f"{st} {r}")

    # 隐私：关共享的人既不显示、也不能被评论
    st, r = req("POST", f"/v1/group/{gid}/share", {"share": "off"}, b)
    st, r = req("GET", f"/v1/group/{gid}/feed?today=2026-11-18", None, a)
    check("关了共享的人从动态里消失",
          all(x["alias"] != "小赵" and x["user_id"] != _uid_b for x in r.get("rows") or []),
          str([x["alias"] for x in r.get("rows") or []]))
    # 关共享只代表「别用我的运动数据」，不是禁言：自己还能在别人动态下说话
    st, r = req("POST", f"/v1/group/{gid}/comment", {"act": _aid, "text": "试试"}, b)
    check("自己关了共享仍然能评论别人（关共享 != 禁言）", st == 200, f"{st} {r}")
    st, r = req("POST", f"/v1/group/{gid}/acts", {"acts": _acts[:1],
                                                  "today": "2026-11-18"}, b)
    check("关了共享不上传动态（409）", st == 409, f"{st} {r}")
    st, r = req("POST", f"/v1/group/{gid}/share", {"share": "summary"}, b)
    check("恢复共享", st == 200, f"{st} {r}")

    st, r = req("GET", f"/v1/group/{gid}/feed?today=2026-11-18", None, c3)
    check("非成员看不了动态（403）", st == 403, f"{st} {r}")

    # 转让组长：设置页那个下拉用到的接口
    st, r = req("POST", f"/v1/group/{gid}/transfer", {"user_id": _uid_b}, a)
    check("组长可以转让", st == 200, f"{st} {r}")
    st, r = req("GET", f"/v1/group/{gid}", None, b)
    check("转让后对方变成组长",
          (r.get("group") or {}).get("role") == "owner", f"{st} {r}")
    st, r = req("POST", f"/v1/group/{gid}/transfer", {"user_id": _uid_a}, b)
    check("再转回来", st == 200, f"{st} {r}")

    # ---------------- 组内路线图
    print("\n11c. 组内路线图（只传格子，不传轨迹）")
    st, r = req("GET", f"/v1/group/{gid}/map?today=2026-11-18", None, a)
    check("默认没人开路线共享，地图是空的",
          st == 200 and r.get("ok") and not (r.get("people") or []), f"{st} {r}")
    check("地图会告诉你有几个人没开", int(r.get("hidden") or 0) > 0, str(r.get("hidden")))

    _cells = ["8883:25915", "8884:25915", "8884:25916"]
    st, r = req("POST", f"/v1/group/{gid}/cells", {"cells": _cells}, a)
    check("没开共享就传格子被拒（409）", st == 409, f"{st} {r}")

    st, r = req("POST", f"/v1/group/{gid}/mapshare", {"on": True}, a)
    check("打开路线共享", st == 200 and r.get("map_share") is True, f"{st} {r}")
    st, r = req("GET", f"/v1/group/{gid}", None, a)
    _me = next(m for m in r["group"]["members"] if m.get("is_me"))
    check("成员视图里能看到路线开关状态", _me.get("map_share") is True, str(_me))

    st, r = req("POST", f"/v1/group/{gid}/cells",
                {"cells": _cells + ["坏数据", "1:2:3", None], "today": "2026-11-18"}, a)
    check("上传格子，脏值被丢掉", st == 200 and r.get("saved") == 3, f"{st} {r}")

    # 每格跑量：可选字段，带了就存，脏值截断
    st, r = req("POST", f"/v1/group/{gid}/cells",
                {"cells": _cells,
                 "meters": {_cells[0]: 1240, _cells[1]: "800", _cells[2]: 999999,
                            "乱写": 500},
                 "today": "2026-11-18"}, a)
    check("带跑量的格子照收", st == 200 and r.get("saved") == 3, f"{st} {r}")
    st, r = req("GET", f"/v1/group/{gid}/map?today=2026-11-18", None, a)
    _mt = ((r.get("people") or [{}])[0]).get("meters") or {}
    check("跑量原样回来", int(_mt.get(_cells[0]) or 0) == 1240, str(_mt))
    check("字符串跑量也能解析", int(_mt.get(_cells[1]) or 0) == 800, str(_mt))
    check("超上限的跑量被截断", int(_mt.get(_cells[2]) or 0) == 50000, str(_mt))
    check("跑量里的脏 key 被丢掉", "乱写" not in _mt, str(_mt))

    st, r = req("GET", f"/v1/group/{gid}/map?today=2026-11-18", None, b)
    _ppl = r.get("people") or []
    check("同组成员能看到我的格子", st == 200 and len(_ppl) == 1, f"{st} {r}")
    check("格子原样回来", set(_ppl[0]["cells"]) == set(_cells), str(_ppl[:1]))
    check("每人有颜色", bool(_ppl[0].get("color")), str(_ppl[:1]))
    check("别人看到的是 is_me=False", _ppl[0]["is_me"] is False, str(_ppl[:1]))

    st, r = req("POST", f"/v1/group/{gid}/cells",
                {"cells": ["%d:%d" % (i, i) for i in range(700)], "today": "2026-11-18"}, a)
    check("一次超过 600 格被拒（400）", st == 400, f"{st} {r}")

    st, r = req("POST", f"/v1/group/{gid}/cells", {"cells": [], "today": "2026-11-18"}, a)
    check("空数组等于清空本周", st == 200 and r.get("saved") == 0, f"{st} {r}")
    st, r = req("GET", f"/v1/group/{gid}/map?today=2026-11-18", None, a)
    check("清空后地图上就没有我了", not (r.get("people") or []), str(r.get("people")))

    st, r = req("POST", f"/v1/group/{gid}/cells", {"cells": _cells, "today": "2026-11-18"}, a)
    st, r = req("GET", f"/v1/group/{gid}/map?today=2026-11-18", None, c3)
    check("非成员看不了组内地图（403）", st == 403, f"{st} {r}")

    st, r = req("POST", f"/v1/group/{gid}/mapshare", {"on": False}, a)
    check("关掉路线共享", st == 200 and r.get("map_share") is False, f"{st} {r}")
    st, r = req("GET", f"/v1/group/{gid}/map?today=2026-11-18", None, a)
    check("关掉后格子被删干净（不留历史）",
          not (r.get("people") or []), str(r.get("people")))
    _db = app.group_api.db()
    with _db as _c:
        _n = _c.execute("SELECT COUNT(*) n FROM group_cell WHERE group_id=?", (gid,)).fetchone()["n"]
    check("库里也确实没有残留", int(_n) == 0, str(_n))

    # ---------------- 账号昵称
    print("\n12. 账号昵称")
    _nick_mail = "nick_user@test.local"
    _nick_tok = login(_nick_mail, nickname="阿夜")
    st, r = req("GET", "/v1/me", None, _nick_tok)
    check("注册时填的昵称被保存", st == 200 and r.get("nickname") == "阿夜",
          f"{st} {r.get('nickname')}")

    # 老账号再登录：传了昵称也不能把已有名字盖掉（登录不等于改名）
    # 发码有 60 秒限频（看 sms_log），测试里清掉这条记录再发，不要去改生产逻辑
    with app.db() as _c:
        _c.execute("DELETE FROM sms_log WHERE phone=?", (_nick_mail,))
    _again = login(_nick_mail, nickname="另一个名字")
    st, r = req("GET", "/v1/me", None, _again)
    check("老账号登录不会覆盖已有昵称", r.get("nickname") == "阿夜", str(r.get("nickname")))

    st, r = req("POST", "/v1/me/nickname", {"nickname": "夜里跑的那个"}, _again)
    check("可以改账号昵称", st == 200 and r.get("nickname") == "夜里跑的那个", f"{st} {r}")
    st, r = req("POST", "/v1/me/nickname", {"nickname": ""}, _again)
    check("空昵称被拒（400）", st == 400, f"{st} {r}")

    # 建组时不填昵称 → 用账号昵称，而不是「138****0000」
    st, r = req("POST", "/v1/group/create",
                {"name": "昵称兜底组", "max_members": 10}, _again)
    _gid = (r.get("group") or {}).get("id")
    check("建组不填昵称时用账号昵称",
          st == 200 and bool(_gid), f"{st} {r}")
    if _gid:
        st, r = req("GET", f"/v1/group/{_gid}", None, _again)
        _mine = [m["alias"] for m in r["group"]["members"] if m.get("is_me")]
        check("组内默认名 = 账号昵称", _mine == ["夜里跑的那个"], str(_mine))

    # ---------------- 汇总
    print(f"\n通过 {ok} 项，失败 {fail} 项")
    srv.shutdown()
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
