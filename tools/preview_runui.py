# -*- coding: utf-8 -*-
"""把远征页渲染成图片，用来核对排版。

为什么要单独做一个脚本
----------------------
改一行 CSS 就要看一次效果，而跑通整条链路（起服务端 → 建组 → 灌数据 →
推进到某一幕）要几十行。抽出来之后改样式只跑这一条命令：

    python tools/preview_runui.py

产物在 `preview/runui/`：`act2.html` / `act2.png` 等，外加 `stuck` 那版
（有人受困、出现救援按钮）。

两个刻意的处理：

* **字体路径换成 file://**。截图走本地文件，页里的 `/fonts/...` 是给
  App 的本地服务用的，file:// 下拿不到。不换的话正文会 fallback 到
  无衬线，而截图看起来「正常」——很容易把衬线这件事误判成没生效。
* **固定 420 宽**。手机上实际就是这个宽度（`.app` 是 820 上限，靠内边距收窄）。
  宽屏截图看不出小屏的挤压问题。
"""

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SEASON_START = dt.date(2026, 11, 16)   # 周一
OUT = ROOT.parent / "preview" / "runui"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
WIDTH = 420


def _boot():
    """起一个真实服务端（临时库），返回 (port, shutdown)。"""
    from http.server import ThreadingHTTPServer

    sys.path.insert(0, str(ROOT / "server"))
    import app

    app.init_db()
    app.group_api.init_db()
    app.expedition_api.init_db()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="rc-prev-"))
    os.environ["GARMIN_HOME"] = str(home)
    tmp = Path(tempfile.mkdtemp(prefix="rc-srv-"))
    os.environ["DB_PATH"] = str(tmp / "app.db")
    os.environ["GROUPS_DB"] = str(tmp / "groups.db")
    os.environ["SMS_PROVIDER"] = "dev"
    os.environ.pop("SMTP_HOST", None)

    srv = _boot()
    port = srv.server_address[1]
    os.environ["RC_API_BASE"] = f"http://127.0.0.1:{port}"

    import http.client as hc

    from garmin_cn import group as G, llm, runsui

    def post(path, body, token=None):
        c = hc.HTTPConnection("127.0.0.1", port, timeout=15)
        head = {"Content-Type": "application/json"}
        if token:
            head["Authorization"] = f"Bearer {token}"
        c.request("POST", path, json.dumps(body).encode(), head)
        out = json.loads(c.getresponse().read() or b"{}")
        c.close()
        return out

    def login(acct):
        code = post("/v1/sms/send", {"account": acct}).get("dev_code")
        return post("/v1/auth", {"account": acct, "code": code}).get("token")

    toks = {k: login(v) for k, v in
            (("a", "a@x.cn"), ("b", "b@x.cn"), ("c", "c@x.cn"))}
    llm.save_config(account_token=toks["a"])
    r = G.create("秦岭跑团", "周末约跑", 20, "老李")
    gid, code = r["group"]["id"], r["group"]["invite_code"]
    for k, alias in (("b", "小赵"), ("c", "阿狗")):
        llm.save_config(account_token=toks[k])
        G.join(code, alias)
    llm.save_config(account_token=toks["a"])

    def report(token, km):
        days = []
        for d in range(14):
            date = SEASON_START + dt.timedelta(days=d)
            if d % 7 in (0, 1, 2, 4, 5):
                days.append({"date": date.isoformat(), "distance_m": int(km * 1000),
                             "duration_s": int(km * 330), "acts": 1, "plan_done": True})
        post(f"/v1/group/{gid}/report", {"days": days}, token)

    report(toks["a"], 8)
    report(toks["b"], 7)
    report(toks["c"], 6)

    pool = json.loads((ROOT / "server/content/expedition_events.json").read_text("utf-8"))

    def brave(fork_id):
        for e in pool["events"]:
            if e["id"] == fork_id:
                for o in e["options"]:
                    if int((o.get("effect") or {}).get("advance") or 0) > 0:
                        return o["id"]
        return ""

    def trial_of(act):
        return next((e["id"] for e in pool["events"]
                     if e.get("act") == act and e.get("type") == "trial"), "")

    # 第一幕：把岔路和支线都走掉，让「已选」的样子也能看到
    st1 = G.run_state(gid, "2026-11-17")
    if st1.get("fork"):
        G.run_fork(gid, brave(st1["fork"]["id"]), "2026-11-17")
    for i, ev in enumerate(st1.get("events") or []):
        if ev.get("options"):
            G.run_event(gid, i, ev["options"][0]["id"], "2026-11-17")
            break
    G.run_trial(gid, trial_of(1), True, "act-1", "2026-11-17")

    # 第二幕
    st2 = G.run_state(gid, "2026-11-24")
    if st2.get("fork"):
        G.run_fork(gid, brave(st2["fork"]["id"]), "2026-11-24")

    # 造受困：补给压到 1 + 冒险分支 + 试炼失败
    import groups as GG

    with GG.db() as c:
        c.execute("UPDATE group_run_member SET supply=1 WHERE group_id=? AND user_id=1",
                  (gid,))
    G.run_trial(gid, trial_of(2), False, "", "2026-11-24")
    st2s = G.run_state(gid, "2026-11-24")

    OUT.mkdir(parents=True, exist_ok=True)
    font_url = (ROOT / "garmin_cn/content/expedition-serif.woff2").as_uri()
    wrap = (f"<style>html{{background:#05080d}}"
            f"body{{width:{WIDTH}px;margin:0 auto}}</style>")
    pages = {"act2": st2, "act2_stuck": st2s}
    for name, data in pages.items():
        h = runsui.run_page("PREVIEW", "", data)
        h = h.replace('url("/fonts/expedition-serif.woff2")', f'url("{font_url}")')
        (OUT / f"{name}.html").write_text(h.replace("</body>", wrap + "</body>"),
                                          encoding="utf-8")
        print(f"{name}.html  ({len(h)} 字节)")

    print(f"第 2 幕：{st2['season']['act']} | 岔路 "
          f"{(st2.get('fork') or {}).get('id')} | 本幕 "
          f"{st2['progress'].get('stage_team')}/{st2['progress'].get('stage_target')}")
    print(f"受困：{st2s['me']['status']} | 名单 {st2s['stranded']}")

    exe = CHROME if Path(CHROME).is_file() else "chrome"
    for name in pages:
        out = OUT / f"{name}.png"
        subprocess.run([exe, "--headless=new", "--disable-gpu", "--no-first-run",
                        "--no-default-browser-check", "--allow-file-access-from-files",
                        f"--user-data-dir={Path(tempfile.gettempdir()) / ('wb-prev-' + name)}",
                        "--hide-scrollbars", "--virtual-time-budget=4000",
                        f"--window-size={WIDTH + 40},1800",
                        f"--screenshot={out}", (OUT / f"{name}.html").as_uri()],
                       capture_output=True)
        print(f"{name}.png  {'ok' if out.is_file() else 'FAILED'}")

    srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
