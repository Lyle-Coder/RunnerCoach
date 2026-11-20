# -*- coding: utf-8 -*-
"""组长网页后台的端到端测试：临时库 + 临时端口，走真实 HTTP。

    python test_manage.py

覆盖：登录页 / 组列表 / 成员表 / 审核流 / 导出 CSV / 非管理者被拒。
表单那一条链最容易坏在「do_POST 在 _read_body 之后」——现象是页面能打开、
点任何按钮都 400 或空表单，本地测一遍比部署后猜快得多。
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import sys
import tempfile
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="rc-manage-"))
os.environ["DB_PATH"] = str(TMP / "app.db")
os.environ["GROUPS_DB"] = str(TMP / "groups.db")
os.environ["SMS_PROVIDER"] = "dev"
os.environ.pop("SMTP_HOST", None)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import app  # noqa: E402
import groups  # noqa: E402

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


def req(method: str, path: str, body=None, token: str = "", cookie: str = "",
        form: dict | None = None):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=15)
    h = {}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body, ensure_ascii=False).encode()
        h["Content-Type"] = "application/json"
    if token:
        h["Authorization"] = f"Bearer {token}"
    if cookie:
        h["Cookie"] = cookie
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    raw = r.read()
    hdrs = dict(r.getheaders())
    c.close()
    text = raw.decode("utf-8", "ignore")
    try:
        return r.status, json.loads(text), text, hdrs
    except json.JSONDecodeError:
        return r.status, {}, text, hdrs


def login(email: str, nick: str = "") -> str:
    st, r, _, _ = req("POST", "/v1/sms/send", {"account": email})
    assert st == 200 and r.get("dev_code"), f"发码失败 {st} {r}"
    body = {"account": email, "code": r["dev_code"]}
    if nick:
        body["nickname"] = nick
    st, r2, _, _ = req("POST", "/v1/auth", body)
    assert st == 200 and r2.get("token"), f"登录失败 {st} {r2}"
    return r2["token"]


def main() -> int:
    global PORT
    app.init_db()
    if app.group_api is None:
        print("groups 模块没加载")
        return 1
    app.group_api.init_db()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    srv.daemon_threads = True
    PORT = srv.server_address[1]
    import threading

    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        a = login("boss@example.com", "组长")
        b = login("newbie@example.com", "新人")
        ck_a = "rc_tok=" + urllib.parse.quote(a)
        ck_b = "rc_tok=" + urllib.parse.quote(b)

        print("\n1. 登录页")
        st, _, txt, _ = req("GET", "/manage")
        check("未登录看到登录页", st == 200 and "发送验证码" in txt, f"{st}")
        check("登录页没有组名泄漏", "我的组" not in txt, txt[:120])

        st, r, _, _ = req("POST", "/v1/group/create", {"name": "网页后台测试队"}, a)
        gid = int(r["group"]["id"])
        check("建组成功", st == 200, f"{st} {r}")

        print("\n2. 组列表与成员")
        st, _, txt, _ = req("GET", "/manage", cookie=ck_a)
        check("登录后看到组名", st == 200 and "网页后台测试队" in txt, f"{st}")
        st, _, txt, _ = req("GET", "/manage", cookie=ck_b)
        check("非管理者看不到别人的组", "网页后台测试队" not in txt, txt[:120])

        st, _, txt, _ = req("GET", f"/manage/g/{gid}?tab=members", cookie=ck_a)
        check("成员表有本周里程", "本周 km" in txt, f"{st}")
        check("成员表有自己", "组长" in txt, txt[:200])
        check("成员表带导出入口", f"/manage/export/{gid}.csv" in txt, "")

        st, _, txt, _ = req("GET", f"/manage/g/{gid}?tab=audit", cookie=ck_a)
        check("审计页有表头", "做了什么" in txt and "建了组" in txt, f"{st}")

        st, _, txt, _ = req("GET", f"/manage/g/{gid}?tab=challenge", cookie=ck_a)
        check("赛事页能打开", st == 200 and "还没有赛事" in txt, f"{st}")

        print("\n3. 审核流（表单 POST）")
        st, _, _, hdrs = req("POST", "/manage/act",
                             form={"gid": gid, "tab": "settings", "op": "join_mode",
                                   "mode": "approve"}, cookie=ck_a)
        check("改成需要批准", st == 303 and hdrs.get("Location", "").startswith(
            f"/manage/g/{gid}"), f"{st} {hdrs.get('Location')}")
        _jm = (req("GET", f"/v1/group/{gid}", token=a)[1].get("group") or {}).get("join_mode")
        check("加入方式落库为 approve", _jm == "approve", str(_jm))

        code = (req("GET", f"/v1/group/{gid}", token=a)[1].get("group") or {}).get("invite_code")
        st, r, _, _ = req("POST", "/v1/group/join", {"code": code}, b)
        check("新人申请加入", st == 200, f"{st} {r}")

        _pend = (req("GET", f"/v1/group/{gid}", token=a)[1].get("group") or {}).get("pending") or []
        _uid = int(_pend[0]["user_id"]) if _pend else 0
        check("申请进了待审核队列", bool(_uid), str(_pend))

        st, _, txt, _ = req("GET", f"/manage/g/{gid}?tab=pending", cookie=ck_a)
        check("审核页看到申请人", "新人" in txt and "批准" in txt, f"{st}")

        st, _, _, hdrs = req("POST", "/manage/act",
                             form={"gid": gid, "tab": "pending", "op": "approve",
                                   "uid": _uid},
                             cookie=ck_a)
        check("批准走 303", st == 303, f"{st}")
        st, _, txt, _ = req("GET", f"/manage/g/{gid}?tab=pending", cookie=ck_a)
        check("批准后审核页空了", "没有待审核" in txt, txt[:200])

        print("\n4. 导出")
        st, _, txt, hdrs = req("GET", f"/manage/export/{gid}.csv", cookie=ck_a)
        check("导出 200", st == 200, f"{st}")
        check("CSV 带 BOM（Excel 不乱码）", txt.startswith("\ufeff"), repr(txt[:6]))
        check("CSV 有表头与数据", "昵称" in txt and "新人" in txt, txt[:160])
        check("导出是附件下载", "attachment" in (hdrs.get("Content-Disposition") or ""),
              str(hdrs.get("Content-Disposition")))

        print("\n5. 越权与未登录")
        st, _, txt, _ = req("GET", f"/manage/g/{gid}", cookie=ck_b)
        check("非成员进组管理页被拒", st == 200 and "管理者" in txt, f"{st}")
        st, _, _, hdrs = req("GET", f"/manage/export/{gid}.csv", cookie=ck_b)
        check("非成员导出被拒", st == 403, f"{st}")
        st, _, _, hdrs = req("GET", f"/manage/g/{gid}")
        check("未登录访问组页跳登录", st == 303, f"{st}")

        print("\n6. 退出")
        st, _, _, hdrs = req("POST", "/manage/logout", cookie=ck_a, form={})
        check("退出清 cookie", "Max-Age=0" in (hdrs.get("Set-Cookie") or ""),
              str(hdrs.get("Set-Cookie")))
    finally:
        srv.shutdown()
        shutil.rmtree(TMP, ignore_errors=True)

    print(f"\n通过 {ok} 项，失败 {fail} 项")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
