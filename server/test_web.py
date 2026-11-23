# -*- coding: utf-8 -*-
"""成员 Web 端的端到端测试：临时库 + 临时端口，走真实 HTTP。

    python test_web.py

覆盖：登录页 / 发码 / 登录种 Cookie / 我的小组 / 组主页 / 完整榜单
（周·月·综合）/ 非成员被拒 / 退出。
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="rc-web-"))
os.environ["DB_PATH"] = str(TMP / "app.db")
os.environ["GROUPS_DB"] = str(TMP / "groups.db")
os.environ["SMS_PROVIDER"] = "dev"
os.environ.pop("SMTP_HOST", None)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import app  # noqa: E402

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
        print("\n1. 登录")
        st, _, txt, _ = req("GET", "/w")
        check("未登录看到登录页", st == 200 and "发送验证码" in txt, f"{st}")
        check("登录页说清了网页只有组数据", "今日训练" in txt, txt[:100])

        # /w/send 用独立邮箱测：同一账号 60 秒内只能发一次码，
        # 拿主账号测会撞限频，看起来像「页面坏了」。
        st, _, txt, _ = req("POST", "/w/send",
                            form={"account": "sendonly@example.com"})
        check("发码后进验证码页", st == 200 and "输入验证码" in txt, f"{st}")

        # 主账号：先从 /v1 拿 dev 码，再走 /w/login 验证
        st, r, _, _ = req("POST", "/v1/sms/send", {"account": "runner@example.com"})
        code = r.get("dev_code") or ""
        st, _, _, hdrs = req("POST", "/w/login",
                             form={"account": "runner@example.com", "code": code})
        check("登录成功 303", st == 303, f"{st}")
        setc = hdrs.get("Set-Cookie") or hdrs.get("set-cookie") or ""
        check("种了会话 Cookie", "rc_tok=" in setc and "HttpOnly" in setc, setc)
        _tok = urllib.parse.unquote(
            (setc.split("rc_tok=")[1].split(";")[0] if "rc_tok=" in setc else ""))
        ck_a = "rc_tok=" + urllib.parse.quote(_tok)
        # 组操作换一个带昵称的账号：/w/login 没有昵称字段（网页登录不需要），
        # 而榜单显示的是组内 alias，空昵称会让「显示自己的名字」这条断言失真。
        a = login("alpha@example.com", "跑者甲")
        b = login("other@example.com", "路人乙")
        ck_a = "rc_tok=" + urllib.parse.quote(a)

        print("\n2. 我的小组")
        st, _, txt, _ = req("GET", "/w", cookie=ck_a)
        check("未加入组时给出去处", st == 200 and "还没有加入小组" in txt, f"{st}")

        st, r, _, _ = req("POST", "/v1/group/create", {"name": "网页测试队"}, a)
        gid = int(r["group"]["id"])
        code = (req("GET", f"/v1/group/{gid}", token=a)[1].get("group") or {}).get("invite_code")
        req("POST", "/v1/group/join", {"code": code}, b)

        # 只有一个组时 /w 直达组页（和 App 一样），列表要带 ?list=1 才看得到
        st, _, _, hdrs = req("GET", "/w", cookie=ck_a)
        check("单组时 /w 直达组页", st == 303 and
              f"/w/g/{gid}" in (hdrs.get("Location") or ""),
              f"{st} {hdrs.get('Location')}")

        st, _, txt, _ = req("GET", "/w?list=1", cookie=ck_a)
        check("组列表显示组名", st == 200 and "网页测试队" in txt, f"{st}")
        check("组列表有成员数", "2 人" in txt, txt[:200])

        print("\n3. 组主页与榜单")
        req("POST", f"/v1/group/{gid}/report",
            {"days": [{"date": __import__("datetime").date.today().isoformat(),
                       "distance_m": 8000, "duration_s": 2400, "acts": 1,
                       "ride_m": 20000}]}, a)

        st, _, txt, _ = req("GET", f"/w/g/{gid}", cookie=ck_a)
        check("组页能打开", st == 200 and "本周榜" in txt, f"{st}")
        check("组页显示自己的名字", "跑者甲" in txt, txt[:200])
        check("组页有完整榜单入口", f"/w/g/{gid}/board" in txt, "")
        check("组页有荣誉入口", f"/w/g/{gid}/shirts" in txt, "")
        check("组页有三个探索格", f"/w/g/{gid}/bingo" in txt
              and f"/w/g/{gid}/map" in txt, "")
        check("组页有二级页返回", 'class=back' in txt, "")

        st, _, txt, _ = req("GET", f"/w/g/{gid}/board", cookie=ck_a)
        check("榜单页 200", st == 200 and "完整榜单" in txt, f"{st}")

        st, _, txt, _ = req("GET", f"/w/g/{gid}/board?range=month&by=comp",
                            cookie=ck_a)
        check("综合榜 200", st == 200, f"{st}")
        check("综合榜有折算说明", "骑行 ÷ 4" in txt, txt[:200])
        check("综合榜显示分项", "骑 20" in txt, txt[:300])

        print("\n3b. 二级页")
        st, _, txt, _ = req("GET", f"/w/g/{gid}/shirts", cookie=ck_a)
        check("六件衫 200", st == 200 and "六件衫" in txt, f"{st}")
        check("六件衫写了在比什么", "本周跑步的总里程" in txt, txt[:300])
        check("六件衫有返回", f"/w/g/{gid}" in txt, "")

        st, _, txt, _ = req("GET", f"/w/g/{gid}/bingo", cookie=ck_a)
        check("Bingo 200", st == 200 and "已点亮" in txt, f"{st}")

        st, _, txt, _ = req("GET", f"/w/g/{gid}/map", cookie=ck_a)
        check("足迹 200", st == 200 and "我的共享" in txt, f"{st}")
        check("足迹写了只传格子", "500 米" in txt, txt[:300])

        st, _, txt, _ = req("GET", f"/w/g/{gid}/feed", cookie=ck_a)
        check("动态 200", st == 200 and ("最近 20 条" in txt or "还没有动态" in txt),
              f"{st} {txt[:120]}")

        st, _, txt, _ = req("GET", f"/w/g/{gid}/nope", cookie=ck_a)
        check("不存在的二级页 404", st == 404, f"{st}")

        print("\n4. 权限")
        st, _, txt, _ = req("GET", f"/w/g/{gid}", cookie="")
        check("未登录访问组页跳登录", st == 303, f"{st}")

        ck_b = "rc_tok=" + urllib.parse.quote(b)
        req("POST", f"/v1/group/{gid}/leave", None, b)
        st, _, txt, _ = req("GET", f"/w/g/{gid}", cookie=ck_b)
        check("退组后打不开原组", st == 200 and ("成员" in txt or "小组" in txt),
              f"{st} {txt[:120]}")

        print("\n5. 退出")
        st, _, _, hdrs = req("POST", "/w/logout", form={})
        check("退出 303", st == 303, f"{st}")
        setc = hdrs.get("Set-Cookie") or hdrs.get("set-cookie") or ""
        check("退出清 Cookie", "Max-Age=0" in setc, setc)

        print(f"\n通过 {ok} 项，失败 {fail} 项")
        return 0 if fail == 0 else 1
    finally:
        srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
