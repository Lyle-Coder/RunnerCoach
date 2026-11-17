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
        # 灌满 28 天（四季）。只灌两周的话第 3、4 幕没有里程，结算出来是 0，
        # 结算页会显示成「没走到」——那是数据不全，不是设计。
        days = []
        for d in range(28):
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

    def quality_of(tid):
        for e in pool["events"]:
            if e["id"] == tid:
                return e.get("quality") or {}
        return {}

    def assigned_trial(st, act, uid=1):
        """本幕真正抽到的那条试炼；没有就换一条上去。

        不能直接报池子里的第一条——服务端要求「这条试炼在你这次的路上」，
        挑一条没抽到的会被 404。预览要稳定出图，所以缺了就补。
        """
        hit = next((e for e in (st.get("events") or []) if e["type"] == "trial"), None)
        if hit:
            return hit["id"]
        tid = trial_of(act)
        import expedition as EXP

        with EXP.G.db() as c:
            rows = c.execute("SELECT slot FROM group_run_event WHERE group_id=?"
                             " AND user_id=? AND act=? ORDER BY slot", (gid, uid, act)).fetchall()
            if rows:
                c.execute("UPDATE group_run_event SET event_id=?, choice='', tag='', ts=?"
                          " WHERE group_id=? AND user_id=? AND act=? AND slot=?",
                          (tid, EXP.G._now(), gid, uid, act, rows[-1]["slot"]))
        return tid

    def evidence(quality, good, date):
        """一份能算出结论的分圈。预览要看到 level 的三种样子。"""
        metric = str(quality.get("metric") or "")
        n = max(3, int(round(float(quality.get("distance_km") or 5))))
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
        return {"activities": [{"activity_id": "preview-1", "date": date,
                                "distance_km": float(quality.get("distance_km") or 5),
                                "laps": laps}]}

    pages = {}

    # 第一幕：岔路、支线都走掉，试炼交证据核验通过——「已选 + 核验通过」的样子
    st1 = G.run_state(gid, "2026-11-17")
    if st1.get("fork"):
        G.run_fork(gid, brave(st1["fork"]["id"]), "2026-11-17")
    for i, ev in enumerate(st1.get("events") or []):
        if ev.get("options"):
            G.run_event(gid, i, ev["options"][0]["id"], "2026-11-17")
            break
    tv1 = assigned_trial(st1, 1)
    G.run_trial(gid, tv1, True,
                evidence=evidence(quality_of(tv1), True, "2026-11-17"),
                today="2026-11-17")
    pages["act1"] = G.run_state(gid, "2026-11-17")

    # 第二幕：试炼不交证据 → 未核验（格数对折）。这一档必须能看见，
    # 否则玩家不知道自己为什么只走了一格。
    st2 = G.run_state(gid, "2026-11-24")
    if st2.get("fork"):
        G.run_fork(gid, brave(st2["fork"]["id"]), "2026-11-24")
    tv2 = assigned_trial(st2, 2)
    G.run_trial(gid, tv2, True, today="2026-11-24")
    pages["act2"] = G.run_state(gid, "2026-11-24")

    # 第三幕：造受困——补给压到 1 + 冒险分支 + 交了证据但没达标
    import groups as GG

    st3 = G.run_state(gid, "2026-12-01")
    if st3.get("fork"):
        G.run_fork(gid, brave(st3["fork"]["id"]), "2026-12-01")
    with GG.db() as c:
        c.execute("UPDATE group_run_member SET supply=1 WHERE group_id=? AND user_id=1",
                  (gid,))
    tv3 = assigned_trial(st3, 3)
    G.run_trial(gid, tv3, True,
                evidence=evidence(quality_of(tv3), False, "2026-12-01"),
                today="2026-12-01")
    pages["act3_stuck"] = G.run_state(gid, "2026-12-01")

    # 结算页：先真跑到赛季末，让四幕都结算过——否则 targets 只有前两项，
    # 结算页会把第 3、4 段显示成「没走到」，那是数据不全，不是设计。
    G.run_state(gid, "2026-12-14")
    ended = G.run_outcome(gid, "2026-12-14")

    OUT.mkdir(parents=True, exist_ok=True)
    font_url = (ROOT / "garmin_cn/content/expedition-serif.woff2").as_uri()
    wrap = (f"<style>html{{background:#05080d}}"
            f"body{{width:{WIDTH}px;margin:0 auto}}</style>")
    for name, data in pages.items():
        h = runsui.run_page("PREVIEW", "", data)
        h = h.replace('url("/fonts/expedition-serif.woff2")', f'url("{font_url}")')
        (OUT / f"{name}.html").write_text(h.replace("</body>", wrap + "</body>"),
                                          encoding="utf-8")
        print(f"{name}.html  ({len(h)} 字节)")

    st2, st2s = pages.get("act2") or {}, pages.get("act3_stuck") or {}
    print(f"第 2 幕：{(st2.get('season') or {}).get('act')} | 岔路 "
          f"{(st2.get('fork') or {}).get('id')} | 本幕 "
          f"{(st2.get('progress') or {}).get('stage_team')}/"
          f"{(st2.get('progress') or {}).get('stage_target')}")
    print(f"受困：{(st2s.get('me') or {}).get('status')} | 名单 {st2s.get('stranded')}")
    print(f"结局：{ended.get('ending')} | {(ended.get('ending_detail') or {}).get('title')}")

    # 四个结局各出一张。用 SQL 改 flags 造状态，走的是和真实一样的分支判定，
    # 不是把文案写死在预览里。
    def outcome_for(key: str) -> dict:
        import groups as GG

        with GG.db() as c:
            row = c.execute("SELECT flags FROM group_run_season WHERE group_id=?",
                            (gid,)).fetchone()
            flags = json.loads(row["flags"] or "{}")
            stages = flags.get("stages") or {}
            targets = flags.get("targets") or {}
            # `stages[幕]` 是**这一幕的产出**，`targets[幕]` 是**到这一幕为止的
            # 累计目标**——两者不能按同一比例乘。要按「增量」算：
            # 第 i 段的份额 = targets[i] - targets[i-1]。
            orders = sorted(targets, key=lambda x: int(x))
            # arrive 用真实结算值：各幕高低不一（受困那幕会低），比统一乘个
            # 系数更像真的。其余三个按比例压/抬，用来演示分支。
            if key == "arrive" and stages:
                orders = []
            ratio = 0.5 if key == "lost" else 1.08
            for idx, k in enumerate(orders):
                prev = float(targets[orders[idx - 1]]) if idx else 0.0
                share = float(targets[k]) - prev
                stages[k] = round(share * ratio, 1)
            flags["stages"] = stages
            flags["relics"] = (["relic_1", "relic_2", "relic_3"]
                               if key == "discoverer"
                               else ([] if key == "lost" else ["relic_1"]))
            flags["stages"] = stages
            c.execute("UPDATE group_run_season SET flags=?, morale=? WHERE group_id=?",
                      (json.dumps(flags, ensure_ascii=False),
                       2 if key == "detour" else 7, gid))
        return G.run_outcome(gid, "2026-12-14")

    for key in ("arrive", "detour", "lost", "discoverer"):
        d = outcome_for(key)
        h = runsui.outcome_page("PREVIEW", d)
        h = h.replace('url("/fonts/expedition-serif.woff2")', f'url("{font_url}")')
        (OUT / f"end_{key}.html").write_text(h.replace("</body>", wrap + "</body>"),
                                            encoding="utf-8")
        print(f"end_{key}.html  结局={d.get('ending')} "
              f"遗迹={d.get('relic_count')} 士气={d.get('morale')} "
              f"acts={[(a['act'], a['got'], a['target'], a['hit']) for a in d.get('acts') or []]}")

    # 翻篇之后的第一幕：页脚多一句「上一季」。这一句是给「上周没打开」的人
    # 留的——没有它，那一季就等于没发生过。
    nxt = G.run_state(gid, "2026-12-15")
    _h = runsui.run_page("PREVIEW", "", nxt)
    _h = _h.replace('url("/fonts/expedition-serif.woff2")', f'url("{font_url}")')
    (OUT / "act_next.html").write_text(_h.replace("</body>", wrap + "</body>"),
                                       encoding="utf-8")
    print(f"act_next.html  上一季={(nxt.get('season') or {}).get('last')}")

    pages = dict(pages)
    pages.update({f"end_{k}": None for k in
                  ("arrive", "detour", "lost", "discoverer")})
    pages["act_next"] = None

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
