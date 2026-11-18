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


def login(email: str) -> str:
    st, r = req("POST", "/v1/sms/send", {"account": email})
    assert st == 200 and r.get("dev_code"), f"验证码发送失败：{st} {r}"
    st, r2 = req("POST", "/v1/auth", {"account": email, "code": r["dev_code"]})
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

    # ---------------- 汇总
    print(f"\n通过 {ok} 项，失败 {fail} 项")
    srv.shutdown()
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
