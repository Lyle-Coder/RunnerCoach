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

    def report(token, km, weekdays=(0, 1, 2, 4, 5)):
        # 灌满 28 天（四季）。只灌两周的话第 3、4 幕没有里程，结算出来是 0，
        # 结算页会显示成「没走到」——那是数据不全，不是设计。
        # `weekdays` 用来造出三种不同的人：跑得勤的、单次远的、一天不断的，
        # 结算页的「每个人的闪光点」只有差分得开才看得出写对没写对。
        days = []
        for d in range(28):
            date = SEASON_START + dt.timedelta(days=d)
            if d % 7 in weekdays:
                days.append({"date": date.isoformat(), "distance_m": int(km * 1000),
                             "duration_s": int(km * 330), "acts": 1, "plan_done": True})
        post(f"/v1/group/{gid}/report", {"days": days}, token)

    # 老李：一周五天，总量最高；小赵：只跑两天但每次都远；阿狗：天天出门
    report(toks["a"], 7)
    report(toks["b"], 13, (0, 3))
    report(toks["c"], 4, (0, 1, 2, 3, 4, 5, 6))

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

    # 顺手打一次 App 真正会调的那个接口：出图链路（HTML 落盘 + 导出契约）
    # 只有这一条路走过，才算「点按钮能出图」，光有 build_html 不算。
    from garmin_cn import appapi as _A

    _card = _A.expedition_card_html(gid)
    print(f"expedition_card_html → ok={_card.get('ok')} "
          f"{Path(str(_card.get('html') or '')).name} "
          f"{_card.get('export_width')}×{_card.get('export_height')} "
          f"trim={_card.get('export_trim')} {_card.get('error') or ''}")
    assert _card.get("ok"), f"出图接口失败：{_card}"

    OUT.mkdir(parents=True, exist_ok=True)

    # 结局画面是「底图 + 矢量脚印」。如果 content/ 里已经有真底图就用真的；
    # 没有就用矢量场景合成两张（亮窗/暗窗）当占位，这样预览走的是和 App
    # 完全相同的那条路——`_ending_scene()` 只会有一个分支被跑到。
    def _plate(kind: str) -> None:
        svg = runsui._ending_svg("arrive" if kind == "lit" else "lost", props=False)
        html = (f'<!DOCTYPE html><html><head><meta charset="utf-8">'
                f'<style>html,body{{margin:0;padding:0;background:#0B1220}}'
                f'svg{{display:block;width:1920px;height:680px}}</style></head><body>'
                f'{svg}</body></html>')
        src = OUT / f"_{kind}.html"
        src.write_text(html, encoding="utf-8")
        subprocess.run([CHROME if Path(CHROME).is_file() else "chrome",
                        "--headless=new", "--disable-gpu", "--no-first-run",
                        "--allow-file-access-from-files", "--hide-scrollbars",
                        "--virtual-time-budget=2000", "--window-size=1920,680",
                        f"--screenshot={OUT / ('ending_scene_' + kind + '.png')}",
                        src.as_uri()], capture_output=True)

    if any(runsui._ending_plate(k) for k in runsui.ENDING_KEYS):
        print("结局图：content/ 里的每结局一张")
    elif all(runsui._plates()):
        print(f"底图：{runsui._plates()}")
    else:
        # 预览目录里已经有占位就沿用（可以手工换一张试效果），没有才合成
        if not all((OUT / f"ending_scene_{k}.png").is_file() for k in ("lit", "dark")):
            for _k in ("lit", "dark"):
                _plate(_k)
        runsui.PLATE_DIR = OUT
        print(f"底图：占位 → {OUT}")
    if not any(runsui._ending_plate(k) for k in runsui.ENDING_KEYS):
        assert all(runsui._plates()), "占位底图没生成出来"
    font_url = (ROOT / "garmin_cn/content/expedition-serif.woff2").as_uri()

    def _fix(h: str, font_url: str) -> str:
        """`/fonts/...`、`/content/...` 是给 App 的本地 HTTP 服务用的。
        预览走 file://，不换成本地绝对路径就都是 404——底图会变成一块空框。"""
        h = h.replace('url("/fonts/expedition-serif.woff2")', f'url("{font_url}")')
        # `/content/...` 要指向**当前真正在用的**图目录：可能是 content/，
        # 也可能是预览目录里合成的占位
        return h.replace('"/content/', f'"{runsui.PLATE_DIR.as_uri()}/')


    wrap = (f"<style>html{{background:#05080d}}"
            f"body{{width:{WIDTH}px;margin:0}}</style>")
    share_pages: list[str] = []
    for name, data in pages.items():
        h = runsui.run_page("PREVIEW", "", data)
        h = _fix(h, font_url)
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
        from expedition import ACTS as EXP_ACTS

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
            # 「少一个人」：队达标了，但有一个人赛季结束时还困在雪里。
            # 其余四种都是 0——这个数每季只由第 4 幕结算写一次。
            flags["left"] = 1 if key == "missing" else 0
            # 「少一个人」得真的有一个人没走出来，否则结算页的名单上
            # 没有那个状态标记，等于这个结局只有标题。其余四种必须清回
            # moving——不然前面造过的受困会串到别的结局里（队达标了却
            # 有人困着，那就该是 missing 了）。
            c.execute("UPDATE group_run_member SET status=CASE WHEN ? AND user_id=?"
                      " AND act=? THEN 'stranded' ELSE 'moving' END"
                      " WHERE group_id=?",
                      (1 if key == "missing" else 0, 2, EXP_ACTS, gid))
            flags["stages"] = stages
            c.execute("UPDATE group_run_season SET flags=?, morale=? WHERE group_id=?",
                      (json.dumps(flags, ensure_ascii=False),
                       2 if key == "detour" else 7, gid))
        return G.run_outcome(gid, "2026-12-14")

    for key in ("arrive", "detour", "missing", "lost", "discoverer"):
        d = outcome_for(key)
        h = runsui.outcome_page("PREVIEW", d)
        h = _fix(h, font_url)
        (OUT / f"end_{key}.html").write_text(h.replace("</body>", wrap + "</body>"),
                                            encoding="utf-8")
        print(f"end_{key}.html  结局={d.get('ending')} "
              f"遗迹={d.get('relic_count')} 士气={d.get('morale')} "
              f"acts={[(a['act'], a['got'], a['target'], a['hit']) for a in d.get('acts') or []]}")
        # 分享图：1080 宽，出图尺寸就是 1080×1920（不裁不缩），所以这里
        # 直接用 1080×1920 的窗口截，看到什么就是分享出去的样子
        from garmin_cn import runcard as RC

        c = _fix(RC.build_html(d), font_url)
        (OUT / f"share_{key}.html").write_text(c, encoding="utf-8")
        share_pages.append(f"share_{key}")

    # 翻篇之后的第一幕：页脚多一句「上一季」。这一句是给「上周没打开」的人
    # 留的——没有它，那一季就等于没发生过。
    nxt = G.run_state(gid, "2026-12-15")
    _h = runsui.run_page("PREVIEW", "", nxt)
    _h = _fix(_h, font_url)
    (OUT / "act_next.html").write_text(_h.replace("</body>", wrap + "</body>"),
                                       encoding="utf-8")
    print(f"act_next.html  上一季={(nxt.get('season') or {}).get('last')}")

    pages = dict(pages)
    pages.update({f"end_{k}": None for k in
                  ("arrive", "detour", "missing", "lost", "discoverer")})
    pages["act_next"] = None

    exe = CHROME if Path(CHROME).is_file() else "chrome"
    for name in pages:
        out = OUT / f"{name}.png"
        subprocess.run([exe, "--headless=new", "--disable-gpu", "--no-first-run",
                        "--no-default-browser-check", "--allow-file-access-from-files",
                        f"--user-data-dir={Path(tempfile.gettempdir()) / ('wb-prev-' + name)}",
                        "--hide-scrollbars", "--virtual-time-budget=4000",
                        f"--window-size={WIDTH},1800",
                        f"--screenshot={out}", (OUT / f"{name}.html").as_uri()],
                       capture_output=True)
        print(f"{name}.png  {'ok' if out.is_file() else 'FAILED'}")

    # 分享图按真实出图尺寸截：1080×1920，1 倍缩放。和手机上的成品一致，
    # 缩到聊天窗口里还能不能看清，看的就是这一版。
    for name in share_pages:
        out = OUT / f"{name}.png"
        subprocess.run([exe, "--headless=new", "--disable-gpu", "--no-first-run",
                        "--no-default-browser-check", "--allow-file-access-from-files",
                        f"--user-data-dir={Path(tempfile.gettempdir()) / ('wb-prev-' + name)}",
                        "--hide-scrollbars", "--virtual-time-budget=4000",
                        "--window-size=1080,1920",
                        f"--screenshot={out}", (OUT / f"{name}.html").as_uri()],
                       capture_output=True)
        print(f"{name}.png  {'ok' if out.is_file() else 'FAILED'}")

    srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
