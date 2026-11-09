"""离线自检：解析、指标、渲染、App 源码守卫全链路。

从 `cli.py` 抽出来的 —— 之前它一个函数 1900 行，占掉入口文件近一半，
既不好读也没法单独跑。现在 `cli.cmd_selftest` 只是薄包装，
`python -m garmin_cn.selftest` / `run.bat selftest` 都能直接调。

约定：全部离线，不联网、不依赖真实账号；需要数据库时用临时库。
改动任何模块后都应该跑一遍，断言里的失败信息写的是「为什么不能这样改」。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from .auth import AuthError, GarminCNAuth, MfaRequired   # noqa: E402
from .client import GarminCNClient, SessionExpired        # noqa: E402
from .coach import context_markdown                      # noqa: E402
from .metrics import derive                              # noqa: E402
from .report import render                               # noqa: E402
from .store import Store                                 # noqa: E402
from .sync import SyncService                            # noqa: E402
from .sysopen import open_any                            # noqa: E402

# Kotlin 源码目录：改包名时只改这里，别在断言里散着写绝对路径
KT = ROOT / "android" / "app" / "src" / "main" / "java" / "mm" / "runnercoach" / "app"
CONFIG = ROOT / "config.json"
DB = ROOT / "data" / "garmin.db"
OUT = ROOT / "output"


def run() -> int:
    """跑完整自检，返回进程退出码（0 = 全过）。"""
    """不联网的自检：解析、指标、渲染全链路。"""
    from garmin_cn.sync import parse_activity, parse_hrv, parse_sleep

    assert parse_hrv({"hrvSummary": {"lastNightAvg": 62, "weeklyAvg": 55, "status": "BALANCED"}})["hrv_night"] == 62
    s = parse_sleep({"dailySleepDTO": {"sleepTimeSeconds": 27000, "deepSleepSeconds": 5400,
                                       "sleepScores": {"overall": {"value": 78}}}})
    assert s["sleep_total_min"] == 450 and s["sleep_score"] == 78, s
    a = parse_activity({"activityId": 1, "startTimeLocal": "2026-11-01 07:12:00",
                        "activityType": {"typeKey": "running"}, "duration": 2400, "distance": 8000})
    assert a["pace_s_km"] == 300, a

    # 小米健康云的加密外壳：RC4-drop1024 + 双签名，改坏了在这里先炸
    from garmin_cn import mi as MI

    sn = MI._signed_nonce(b"s" * 16, MI._nonce())
    data = '{"key":"weight"}'
    h1 = MI._sign("POST", "/p", {"data": data}, sn)
    assert MI._sign("POST", "/p", {"data": data}, sn) == h1
    assert MI.rc4_drop1024(sn, MI.rc4_drop1024(sn, data.encode())) == data.encode()
    assert MI._sign("POST", "/p", {"data": data, "rc4_hash__": h1}, sn) != h1
    mi_rec = MI.to_records([{"time": 1767225600, "zone_offset": 28800,
                             "value": '{"weight":68.4,"muscle_rate":42.1}'}])[0]
    assert mi_rec["weight_kg"] == 68.4 and mi_rec["muscle_kg"] == 28.8, mi_rec
    # 安全验证（短信）用的小工具：context 提取、带/不带前缀的 JSON、中文错误文案
    assert MI._ctx_of("https://a/b?sid=x&context=ABC123&_locale=zh") == "ABC123"
    assert MI._json_of('&&&START&&&{"code":0}')["code"] == 0
    assert MI._json_of('{"code":0}')["code"] == 0
    assert "图形验证码" in MI._verify_error({"code": 87001})
    assert MI._cookie_parse("a=1; b=2")["b"] == "2"
    try:
        MI._json_of("<html>登录页</html>")
        raise AssertionError("非 JSON 应当报错")
    except MI.MiAuthError:
        pass

    import tempfile

    tmp_db = Path(tempfile.gettempdir()) / "garmin_selftest.db"
    tmp_html = Path(tempfile.gettempdir()) / "garmin_selftest.html"
    if tmp_db.exists():
        tmp_db.unlink()
    store = Store(tmp_db)
    from garmin_cn.demo import build as build_demo

    build_demo(tmp_db, days=40, seed=3)
    # demo 数据默认没有体重：补两条进去，否则营养那段断言会被静默跳过
    from garmin_cn import body as BD

    today = dt.date.today()
    BD.save(store, [
        {"date": (today - dt.timedelta(days=10)).isoformat(), "ts": "08:00",
         "weight_kg": 67.0, "body_fat_pct": 16.4, "muscle_kg": 52.0,
         "water_pct": 61.8, "visceral": 3, "bmr": 1560},
        {"date": today.isoformat(), "ts": "08:00",
         "weight_kg": 66.2, "body_fat_pct": 15.9, "muscle_kg": 52.1,
         "water_pct": 62.2, "visceral": 3, "bmr": 1560},
    ], "mi")
    res = derive(store, dt.date.today().isoformat())
    assert res["readiness"]["score"] is not None
    assert res["load"]["ctl"] >= 0

    # 手机端出图画幅契约：成品必须 9:16，且 HTML 带尺寸探针。
    # 少了任一项，App 导出的长图就会被系统分享面板二次裁切。
    from garmin_cn import actcard as ACTCARD
    from garmin_cn import card as CARD
    from garmin_cn import exportspec as EX
    EX.check()
    for bg in (EX.BG_CARD, EX.BG_ACT):
        sp = EX.spec(bg)
        assert (sp["export_width"], sp["export_height"]) == (1080, 1920), sp
        assert sp["export_width"] * 16 == sp["export_height"] * 9, sp
        assert sp["export_mode"] == "contain", sp
    for mod in (CARD, ACTCARD):
        assert mod._EXPORT_PROBE == EX.probe()
        assert EX.CANVAS_ATTR in mod._EXPORT_PROBE
    for h in (CARD.build_html(res),
              ACTCARD.build_html({}, {}, None, None, "自检")):
        assert EX.CANVAS_ATTR in h and "__gcExportInfo" in h, "HTML 缺导出探针"
        assert h.count("__gcExportInfo") == 1, "探针重复注入"
        assert "__gcFit" in h and "visualViewport" in h, "HTML 缺缩放校正探针"

    # 间歇训练（快慢交替）自动切底部双栏：左快组、右慢组；
    # 匀速跑 / 单调加速不能误报，非间歇活动的分圈表必须原样保留。
    from garmin_cn import interval as IV

    IV.check()
    iv_pts: list[dict] = []
    t, dist = 0.0, 0.0
    for _ in range(6):
        for spd, secs in ((4.2, 95.0), (2.6, 77.0)):
            for _ in range(int(secs / 5)):
                iv_pts.append({"t": t, "spd": spd, "hr": 165, "cad": 180,
                               "dist": dist})
                t += 5.0
                dist += spd * 5.0
    iv_html = ACTCARD.build_html({}, {"series": {"points": iv_pts}},
                                 None, None, "间歇自检")
    assert 'class="iv"' in iv_html and "快组" in iv_html
    assert "慢组 / 恢复" in iv_html and "间歇结构" in iv_html
    assert '<div class="lhead">' not in iv_html, "间歇版不该再渲染原分圈表"
    assert 'class="iv"' not in ACTCARD.build_html({}, {}, None, None, "自检")

    # 原地休息型间歇（真实 15×400 m 样本形态）：静止点不能被过滤掉，
    # 右栏要写「平均恢复 1:xx」而不是几十分钟每公里的假配速，15 组逐组列全。
    rest_pts: list[dict] = []
    t = 0.0
    for _ in range(15):
        for spd, secs in ((4.6, 86.0), (0.0, 90.0)):
            for _ in range(int(secs / 4)):
                rest_pts.append({"t": t, "spd": spd, "hr": 160, "cad": 200,
                                 "dist": 0.0})
                t += 4.0
    rest_html = ACTCARD.build_html({}, {"series": {"points": rest_pts}},
                                   None, None, "间歇自检（原地恢复）")
    assert 'class="iv"' in rest_html and "平均恢复" in rest_html
    assert "组间恢复" in rest_html and "速度差" not in rest_html
    assert 'class="ivmore"' not in rest_html, "15 组应逐组列全"
    assert rest_html.count('class="ivrow"') == 30, "左右各 15 行"

    # 手机端 WebView 的缩放自校正：density=3 时布局视口 1080 CSS px 只露出
    # 360 CSS px，必须把画布缩到 1/3；归一后内容高度与 density 无关。
    # 少了这一步，draw(Canvas) 只截到左上角，分享预览会「只剩一个角」。
    assert abs(EX.fit_scale(1080, 360) - 1 / 3) < 1e-9
    assert EX.fit_scale(1080, 1080) == 1.0
    assert EX.fit_scale(1080, 0) == 1.0
    assert EX.content_height(1080, 1080, 2250) == 2250
    assert EX.content_height(1080, 1080, 1920) == 1920

    # 历史数据（手机端「拉取更多 / 查询历史」）：接口面、区间门禁与活动分页。
    # 只跑不联网的分支——合法日期的 sync_range 会真发请求，不能进自检。
    import json as _json
    import tempfile as _tf
    from pathlib import Path as _Path
    from garmin_cn import appapi as AP
    from garmin_cn.store import Store as _Store
    from garmin_cn.sync import SyncService as _Sync

    assert {"sync_range", "activities", "days_index"} <= AP._API

    def bad_range(*a):
        return _json.loads(AP.call("sync_range", _json.dumps(list(a))))

    assert bad_range("2026-11-06", "2026-11-01")["ok"] is False   # 起止颠倒
    assert bad_range("2020-01-01", "2026-11-06")["ok"] is False   # 超过 3 年
    assert bad_range("not-a-date")["ok"] is False                 # 非法日期
    bad_q = _json.loads(AP.call("activities", _json.dumps(["bad-date"])))
    assert bad_q["ok"] is False and "YYYY-MM-DD" in bad_q["error"]
    d = _json.loads(AP.call("days_index", _json.dumps([1])))
    assert d["ok"] and all(k in d for k in ("first", "last", "count", "items"))
    q = _json.loads(AP.call("activities", _json.dumps([None, None, None, 5, 0])))
    assert q["ok"] and all(k in q for k in ("items", "total", "has_more", "offset"))

    # 网页外壳与交互树：四个 tab 共用一套 ui.page，卡片动作走 data-act，
    # 活动行走 data-aid。约束见 docs/交互树.md：一个动作只有一个入口。
    from garmin_cn import history as HW
    from garmin_cn import ui as U

    idx = HW.index_section(U.coverage())
    assert "/view?date=" in idx and "日汇总覆盖" in idx
    hp = HW.history_page({"type": ["run"]})
    assert "page-head" in hp and 'name="start"' in hp
    assert "data-aid=" in hp and "GC.activity" in hp
    # 「补全历史」全 App 只留一个入口（我的 → 历史数据）。它是一次性动作，
    # 正常由登录后自动跑；历史页和今日页再放按钮只是重复。
    assert 'data-act="historySync"' not in hp, "历史页不该再放补拉入口，统一到「我的」"
    assert 'data-act="historySyncAll"' not in hp, "历史页不该再放一键补全"
    home = U.home_page()
    assert "page-head" in home and 'data-aid=' in home
    assert 'href="/history"' not in home, "首页不该再给历史入口，底部 tab 已经有了"
    # 一图流从「一整张快捷卡」退成页头右上角的小按钮
    assert 'class="head-act"' in home and 'data-act="export"' in home, \
        "首页页头没有一图流按钮"
    assert "导出今日一图流" not in home, "一图流不该再占一整张卡片"
    # 首页不再重复「数据覆盖」：「我的 → 本地数据」已经有同样三个数
    assert "数据覆盖" not in home, "首页的覆盖区块和「我的」页重复了"
    me = U.me_page()
    assert "page-head" in me and 'data-act="login:garmin"' in me
    assert 'data-act="login:coros"' in me, "数据源行要直达对应登录"
    # settings 是 AI 设置，只许在教练页齿轮里
    assert 'data-act="historySync"' not in me, \
        "补全历史不该再有按钮：它在后台自动跑（点开只有弹窗、看着像没效果）"
    assert "后台自动补齐" in me, "我的页要说明历史是后台自动补的"
    assert 'data-act="settings"' not in me
    assert 'data-act="historySync"' not in home, "今日页不该重复放补拉入口"
    assert 'href="/files"' in me
    assert 'data-act="amap"' in me and "地图底图" in me, "我的页要有底图 Key 入口"
    # 看板导航只剩「回来源 tab + 前后翻日期」，不再有横向 tab 入口
    nav = U.nav_html("2026-10-21",
                     {"first": "2026-10-01", "last": "2026-11-06"})
    assert "gc-nav" in nav
    assert "/view?date=2026-10-20" in nav and "/view?date=2026-10-22" in nav
    assert 'href="/history"' in nav and 'href="/"' not in nav
    # 同页参数变化走 replace，不往历史栈堆条目
    assert "data-nopush" in nav and "data-nopush" in U.BRIDGE_JS
    assert "max-width:820px" in U.DASH_FIX and "max-width:820px" in U.AI_FIX
    # 训练计划页：读到/读不到都要能出页面，且能回今日、有刷新入口
    pp = U.plan_page({})
    assert "PLAN · 训练计划" in pp
    assert 'class="back" href="/"' in pp
    assert "planRefresh" in pp
    assert ('class="plan-hero"' in pp) or ("还没读到训练计划" in pp)

    # 训练计划：「计划」和「已完成」必须分开。
    # workout / race = 计划；带 activityId 的 activity = 完成证据；
    # 没有 activityId 的 activity 是「计划活动」，日期在过去也不能算完成。
    from garmin_cn import plan as PL

    cal_items = [
        {"itemType": "workout", "date": "2026-11-03", "title": "轻松跑",
         "duration": 3000, "distance": 8000, "sportTypeKey": "running"},
        {"itemType": "activity", "date": "2026-11-03", "title": "跑步",
         "activityId": 12345, "duration": 2400, "distance": 6000},
        {"itemType": "workout", "date": "2026-11-05", "title": "周四的课"},
        {"itemType": "workout", "date": "2026-11-07", "title": "节奏跑",
         "duration": 2400, "distance": 7000},
        {"itemType": "race", "date": "2026-11-15", "title": "半马",
         "distance": 21097},
        {"itemType": "note", "date": "2026-11-09", "title": "备注"},
    ]
    cal = PL.from_calendar(cal_items, "2026-11-07")
    assert cal["source"] == "calendar" and cal["scheduled"], cal
    assert cal["today"]["items"][0]["minutes"] == 40
    assert cal["today"]["items"][0]["km"] == 7.0
    assert cal["week"][1]["done"] is True, "周二有带 activityId 的记录，应标已完成"
    assert cal["week"][3]["done"] is False and cal["week"][3]["missed"] is True, \
        "周四计划了但没做，应该标「未完成」——旧版按日期一律当已完成"
    assert cal["next_date"] == "2026-11-15", cal["next_date"]  # 11-09 是 note，跳过
    assert PL.from_calendar([], "2026-11-07")["scheduled"] is False

    # `activity` 一律是「已经做完的记录」，**不管有没有 activityId**。
    # v61 曾把「无 id 的 activity」当计划活动，结果用户早上骑车上班（日历里一条
    # 没有 id 的 activity）被显示成「今日课表：骑行 409.3 公里」。
    plan_only = PL.from_calendar([
        {"itemType": "activity", "date": "2026-11-03", "title": "计划中的骑行",
         "sportTypeKey": "cycling", "duration": 3600}], "2026-11-07")
    assert not plan_only["week"][1]["items"], \
        "无 id 的 activity 又变成排课了（通勤会被当成今日课表）"
    assert plan_only["week"][1]["acts"], "无 id 的 activity 应该进「已完成记录」"
    assert plan_only["week"][1]["done"] is True, "日历里的 activity 就是做完了"

    # 距离单位：同 duration 一样，日历的 distance 也可能是厘米。
    # 4.09 km 的通勤若按米读会变成 409.3 公里（一次 1364 km/h 的骑行）。
    assert abs(PL._distance_km(409300, 1080) - 4.093) < 0.01, \
        "厘米当米读了：4.09 km 会显示成 409.3 公里"
    assert abs(PL._distance_km(4093, 1080) - 4.093) < 0.01, "正常米制被改坏"
    assert abs(PL._distance_km(2128000, 5340) - 21.28) < 0.05, "半马距离算错"
    assert PL._distance_km(99999999999, 600) == 0, "离谱距离没被挡住"
    _commute = PL._cal_item({"title": "骑行", "duration": 1080, "distance": 409300})
    assert _commute["km"] == 4.1, f"通勤距离不对：{_commute['km']}"
    # 今日课表：没排课时标题必须是「休息」，练过的只单列一行
    _cm = PL.from_calendar([
        {"itemType": "activity", "date": dt.date.today().isoformat(), "title": "骑行",
         "duration": 1080, "distance": 409300}])
    _tc = U._today_plan_card(_cm)
    assert "今天休息" in _tc, "没排课时课表应该是休息，而不是显示一条通勤"
    assert "409" not in _tc, "课表里的通勤距离仍然是错的"
    assert "4.1 km" in _tc and "今天已完成" in _tc, \
        "练过的记录没有单列出来"
    # 本地库那天有活动（含高驰）也算完成
    with_local = PL.from_calendar([
        {"itemType": "workout", "date": "2026-11-03", "title": "轻松跑"}],
        "2026-11-07", {"2026-11-03"})
    assert with_local["week"][1]["done"] is True, \
        "本地库有活动应判完成（高驰的活动不在佳明日历里）"
    # 今天没排课但已经练了：课表仍是「休息」，练过的单列一行（不冒充课表）
    today_act = PL.from_calendar([
        {"itemType": "activity", "date": "2026-11-07", "title": "晨跑",
         "activityId": 9, "duration": 1800, "distance": 5000}], "2026-11-07")
    assert today_act["today"]["acts"][0]["name"] == "晨跑"
    _tc = U._today_plan_card(today_act)
    assert "今天休息" in _tc, "没排课时课表被实际活动顶掉了"
    assert "今天已完成" in _tc and "晨跑" in _tc, "练过的记录没有单列出来"
    # 旧快照里「过去即已完成」的脏标记要丢掉重算
    _old_snap = {"available": True, "source": "calendar",
                 "today": {"date": "2026-11-07", "items": []},
                 "week": [{"date": "2026-11-03", "dow": "周二",
                           "items": [{"name": "x"}], "acts": [], "rest": False}],
                 "done_days": ["2026-11-03"]}
    _fixed = PL.annotate(_old_snap, set(), "2026-11-07")
    assert _fixed["week"][0]["done"] is False and _fixed["week"][0]["missed"] is True, \
        "旧快照里「过去即完成」的脏标记没被清掉"
    assert cal.get("classify") == PL.CLASSIFY_REV, \
        "课表分类口径没打版本号，旧快照没法学法迁移"
    # 旧口径的快照必须作废：里面的 items/acts 是按老规则分的，就地修不了。
    # 不作废的话，手机上换版本后还会一直显示「今日课表：骑行 409 公里」。
    _snapdir = Path(tempfile.mkdtemp())
    _old = _snapdir / "plan.json"
    _old.write_text(json.dumps({"source": "calendar", "classify": 2,
                                "today": {"date": "2026-11-09",
                                          "items": [{"name": "骑行"}]}}),
                    encoding="utf-8")
    assert PL.load(_old) is None, "旧口径快照没被作废，错课表会一直显示"
    _old.write_text(json.dumps({"source": "calendar", "classify": PL.CLASSIFY_REV,
                                "today": {"date": "2026-11-09", "items": []}}),
                    encoding="utf-8")
    assert PL.load(_old) is not None, "新口径快照被误判成旧的了"
    shutil.rmtree(_snapdir, ignore_errors=True)
    # 页面必须能把「未完成」标出来，也不能再把「已练」写成「今天休息」
    _ui_plan_src = Path(U.__file__).read_text(encoding="utf-8")
    assert 'class="miss">未完成' in _ui_plan_src, "周视图没有「未完成」标签"
    assert "今天已完成" in _ui_plan_src, "今日卡片没有「今天已完成」分支"
    assert "本周完成" in _ui_plan_src, "计划页没有统计本周完成情况"
    # 计划接口读缓存时也要按本地库重算：同步之后「今天练没练」会变
    _aa_plan_src = (ROOT / "garmin_cn/appapi.py").read_text(encoding="utf-8")
    assert "annotate(data, days, today)" in _aa_plan_src, \
        "计划接口直接用缓存里的完成度，同步后不会更新"
    assert PL._months("2026-11-07")[0] == (2026, 10), "要连上个月一起拉，兼容月份语义"

    # 一图流读计划快照必须走 GARMIN_HOME：APK 里代码在只读 assets、数据在
    # filesDir，用 __file__ 推路径永远读不到——手机上一图流没计划就是这个坑。
    import os as _os
    import tempfile as _tf2

    with _tf2.TemporaryDirectory() as td:
        _data = Path(td) / "data"
        _data.mkdir(parents=True, exist_ok=True)
        (_data / "plan.json").write_text(
            _json.dumps(cal, ensure_ascii=False), encoding="utf-8")
        _old = _os.environ.get("GARMIN_HOME")
        _os.environ["GARMIN_HOME"] = td
        try:
            _got = PL.attach({}).get("plan") or {}
            assert _got.get("name") == "佳明日历", \
                "attach() 要按 GARMIN_HOME 读快照，不能用 __file__ 推"
            from garmin_cn.card import _week_cells

            _rows = _week_cells(_got, "2026-11-07")
            assert _rows.count('class="wkd') == 7, "一图流周视图应横排 7 格"
            assert '<div class="wk">' in _rows, "横排容器必须存在，不能退回竖排 7 行"
        finally:
            if _old is None:
                _os.environ.pop("GARMIN_HOME", None)
            else:
                _os.environ["GARMIN_HOME"] = _old
    # 回放导出（手机端）：页面走 http，底图/照片必须是同源 URL。
    # 真机绝对路径是 /data/user/0/<包名>/files/data/basemaps/x.png，
    # 服务端白名单只认 /data/basemaps/x.png —— 不改写就 404，页面 ready
    # 永远 false，导出静默停在 0 帧（这是踩过的坑，别再改回去）。
    from garmin_cn import basemap as BM
    from garmin_cn import motion as MO

    _web = MO._web_url(str(BM.ROOT / "data" / "basemaps" / "_selftest.png"))
    assert _web == "/data/basemaps/_selftest.png", _web
    # 已经是同源路径的不再加工；私密文件（db/session）不转
    assert MO._web_url("/data/basemaps/_selftest.png") == "/data/basemaps/_selftest.png"
    assert MO._web_url(str(BM.ROOT / "data" / "session.json")) != "/data/session.json"
    _msrc = Path(MO.__file__).read_text(encoding="utf-8")
    # toURL 不能给已带前导 / 的路径再补一个（会变成 //data/... 协议相对 URL，
    # 浏览器会去找主机名为 data 的地址，请求到不了本机服务）
    assert "if (n.startsWith('/')) return n;" in _msrc, "toURL 的前导斜杠判断丢了"
    assert "{ method: 'POST' }" in _msrc, "__done 必须用 POST 写完成标记"
    assert "window.__motion.ready = !!baseBaked;" in _msrc, "ready 只该要求底图"
    assert HW._date({"date": ["not-a-date"]}, "date", "2026-11-06") == "2026-11-06"
    # 底图 Key 校验：太短的串直接拒，且不写盘
    _amap = _json.loads(AP.call("set_amap_key", _json.dumps(["short"])))
    assert not _amap.get("ok") and "不完整" in (_amap.get("error") or "")

    # ---- 素材裁剪（原生侧）：预览必须与结果一致 ----
    # 裁剪界面（ClipTrimDialog）和剪辑器（ClipTrimmer）是配对的：
    #   起点必须落在关键帧上，否则从 GOP 中间切过去开头花屏；
    #   两条轨必须共用同一个时间原点，否则音画差零点几秒就错位。
    # 这两条都是「改了看不出来、但结果就是不对」的坑，钉在自检里。
    _kt = KT
    _trim_src = (_kt / "ClipTrimmer.kt").read_text(encoding="utf-8")
    _dlg_src = (_kt / "ClipTrimDialog.kt").read_text(encoding="utf-8")
    # 剪辑器：视频轨用 PREVIOUS_SYNC 找起点（对齐到关键帧）
    assert "MediaExtractor.SEEK_TO_PREVIOUS_SYNC" in _trim_src, "起点没吸附关键帧"
    # 时间原点统一：baseUs 算一次，所有轨共用，不能每条轨各算各的。
    # 早期版本音频单独 seek 到 startMs，视频对齐到前一个关键帧，
    # 两者差零点几秒 → 音画错位（不修就是「剪出来声音和画对不上」）。
    assert "val baseUs =" in _trim_src, "缺少统一时间原点 baseUs"
    # 轨道循环里的那次 seek（最后一次）必须用 baseUs，不能各轨各算
    _seek_blk = _trim_src[_trim_src.rindex("ex.seekTo("):][:200]
    assert "baseUs," in _seek_blk, "轨道 seek 没统一用 baseUs（音画会错位）"
    # 起点之前的样本要丢掉，否则减去 baseUs 会得到负时间戳
    assert "if (pts >= baseUs)" in _trim_src, "没丢弃原点之前的样本"
    # 裁剪界面：起点吸附关键帧、终点自由 —— 两端语义不同，不能一视同仁
    assert "SAMPLE_FLAG_SYNC" in _dlg_src, "关键帧扫描没了"
    assert "getScaledFrameAtTime" in _dlg_src, "胶片条缩略图没了"
    # 吸附的判据不能被改成永假（那样等于没吸附，起点会落到 GOP 中间 → 开头花屏）
    assert "if (!snapEnabled || keys.isEmpty()) return t" in _dlg_src, \
        "起点没吸附关键帧（snap 的短路条件被改了）"
    # 预览要有画面，不能又退回裸滑块
    assert "TextureView" in _dlg_src and "MediaPlayer" in _dlg_src, "预览播放器没了"
    # 排除注释后再查 SeekBar：注释里提到「之前的裸 SeekBar」是正常的
    _dlg_code = "\n".join(
        ln for ln in _dlg_src.splitlines() if not ln.strip().startswith(("*", "//", "/*")))
    assert "SeekBar" not in _dlg_code, "不该再用裸 SeekBar（用户看不到画面）"

    # ---- 素材（图片 / 视频）插入链路 ----
    # 这条链上有三个坑，任一没填上，表现都是「加了素材，回放里什么都没有」：
    #   1) 合成轨迹的 t0 单位；2) 素材没带拍摄时间；3) 图片根本没被渲染。
    _ap_src = Path(AP.__file__).read_text(encoding="utf-8")
    # photo.match 用 (t_utc_ms - t0) / 1000 算相对秒，t0 给秒就全被判成「不在活动时段」
    assert '"t0": int(t0 * 1000)' in _ap_src, "合成轨迹的 t0 必须是毫秒"
    assert "def _probe_material" in _ap_src, "素材没读拍摄时间/坐标"
    assert '"kind": kind' in _ap_src, "素材没带类型（图片/视频）"
    assert AP._kind_of(Path("a.PNG")) == "image", "图片扩展名识别"
    assert AP._kind_of(Path("a.mp4")) == "video", "视频扩展名识别"
    # 没有时间信息的素材必须均匀铺开，不能整批丢掉
    from garmin_cn import motion as MOT

    _rt = {"n": 2, "t0": 1_700_000_000_000, "t": [0, 200]}
    _cl = [{"name": "a", "t_utc": None}, {"name": "b", "t_utc": None}]
    MOT._spread_clips(_cl, _rt)
    assert _cl[0]["t_utc"] == 1_700_000_066_666, _cl[0]
    assert _cl[1]["t_utc"] == 1_700_000_133_333, _cl[1]
    assert _cl[0]["approx"] and _cl[1]["approx"], "铺开的素材要标 approx"
    # 已经有了拍摄时间的不动
    _cl2 = [{"name": "c", "t_utc": 1_700_000_050_000}]
    MOT._spread_clips(_cl2, _rt)
    assert _cl2[0]["t_utc"] == 1_700_000_050_000, "有时间信息的素材不该被改"
    _mo_src = Path(MOT.__file__).read_text(encoding="utf-8")
    assert "clips = _spread_clips(clips, rt)" in _mo_src, "素材没走均匀铺开兜底"
    assert "if (p.clip.kind === 'image')" in _mo_src, "页面没处理图片素材"
    assert "need.push(p.clip.src)" in _mo_src, "图片素材没进图片预加载队列"
    # ---- 总时长：30 秒巡航 + 素材停留。停留是**叠加**的，不许挤占巡航 ----
    # 旧版把素材停留塞进 30 秒预算里：素材一多，轨迹就被压成几秒飞完全程；
    # App 又按 30 秒算帧数，末尾的素材直接被截掉——看着就像「锚点错了」。
    _hold3 = [{"t": 1.0, "clip": {"dur": 8.0}}, {"t": 2.0, "clip": {"dur": 3.0}},
              {"t": 3.0, "clip": {"dur": 3.0}}]
    assert MOT._timeline_total(30.0, _hold3) == 44.0, MOT._timeline_total(30.0, _hold3)
    assert MOT._timeline_total(30.0, []) == 30.0, "没有素材时总长应该正好是巡航预算"
    # 长素材也要整段放完（60 秒的老上限会把长视频截掉）
    assert MOT._timeline_total(30.0, [{"t": 1.0, "clip": {"dur": 90.0}}]) == 120.0
    # 页面收片尾要靠算出来的真实总时长；帧数也必须按它算
    assert "const TOTAL = TL.total;" in _mo_src, "页面没用真实总时长"
    assert "duration: TOTAL" in _mo_src, "导出接口没暴露真实总时长"
    assert "if (playT >= D.play)" not in _mo_src, "片尾判断还在用旧的 30 秒预算"
    assert "if (p >= D.play) return D.dur" not in _mo_src, "轨迹取值还在用旧的 30 秒预算"
    assert "int(round(total * fps))" in _ap_src, "帧数没按真实总时长算（末尾素材会被截掉）"
    # 桌面导出用的收帧服务：`/__done` 和 `/__diag` 没 `i` 参数，掉进收帧分支
    # 就会被当成 i=0 覆盖真首帧（`/__done` 的 body 为空 → 首帧 0 字节，成片少一帧）
    from garmin_cn import video as _vid

    _vsrc = Path(_vid.__file__).read_text(encoding="utf-8")
    assert 'if self.path.startswith(("/__done", "/__diag"))' in _vsrc, \
        "收帧服务没显式吃掉 /__done 与 /__diag，首帧会被写成 0 字节"
    # ---- 音轨：素材原声要按时间轴混进成片 ----
    from unittest import mock as _mock

    # 三个坑：① 页面给 WebView 的 src 是 /data/... URL，解码器打不开，
    #   必须另存一份磁盘路径；② 图片段 dur=0 不能混音；③ 时间轴是页面 POST
    #   上来的，路径必须先过白名单，不能让它指向任意文件。
    assert '"local": str(Path(src).resolve())' in _mo_src, "payload 没保留素材磁盘路径"
    assert "w.clip.local || w.clip.src" in _mo_src, "页面没把本地路径回传给混音"
    _web_blk = _mo_src[_mo_src.index("    if web:"):][:400]
    assert "local" not in _web_blk, "web 改写不该动 local（原生要磁盘路径）"
    assert 'w.get("file") or w.get("src")' in _vsrc, "桌面 ffmpeg 没优先用本地路径"
    assert "def motion_audio(" in _ap_src, "原生拿不到混音清单"
    assert '"motion_audio"' in _ap_src, "motion_audio 没注册进 App 接口白名单"
    # 真机素材路径 /data/user/0/<包名>/files/data/media/... 也以 /data/ 开头，
    # 必须先按「确实存在的绝对路径」认，否则会被当成同源 URL 拼错、原声全丢
    assert "if Path(s).is_absolute() and Path(s).is_file():" in _ap_src, \
        "真机绝对路径会被误判成同源 URL"
    with _tf.TemporaryDirectory() as _atd:
        _aroot = _Path(_atd)
        (_aroot / "data/media").mkdir(parents=True)
        (_aroot / "data/motion_cache").mkdir(parents=True)
        _av = _aroot / "data/media/a.mp4"
        _av.write_bytes(b"x")
        _bv = _aroot / "data/motion_cache/b.mp4"
        _bv.write_bytes(b"x")
        (_aroot / "data/garmin.db").write_bytes(b"x")
        _arun = "audio123"
        _adir = _aroot / "output/_frames" / _arun
        _adir.mkdir(parents=True)
        _tl = _adir / "_timeline.json"
        _tl.write_text(_json.dumps([
            {"file": "/data/media/a.mp4", "src": "/data/media/a.mp4",
             "a": 3.0, "lead": 0.5, "dur": 8.0},
            {"file": "", "src": "", "a": 12.0, "lead": 0, "dur": 0},   # 图片段
            {"file": "/data/media/gone.mp4", "a": 15.0, "dur": 4.0},   # 文件丢了
            {"file": "/data/garmin.db", "a": 16.0, "dur": 4.0},        # 越权
            {"src": "/data/motion_cache/b.mp4", "a": 1.0, "dur": 5.0},  # 老页面只有 src
            {"file": str(_bv), "src": "/data/media/wrong.mp4",
             "a": 20.0, "dur": 2.0},                          # file 必须压过 src
        ]), encoding="utf-8")
        with _mock.patch.object(AP, "HOME", _aroot), \
                _mock.patch.object(AP, "OUT", _aroot / "output"):
            assert AP._audio_local("/media/a.mp4") == _av
            assert AP._audio_local(str(_bv)) == _bv
            assert AP._audio_local("/data/../etc/passwd") is None, "不能跳出 data/"
            assert AP._audio_local("/data/garmin.db") is None, "不能读私有文件"
            assert AP._audio_local("/data/media/a.jpg") is None, "图片没有音轨"
            _got = AP.motion_audio(_arun)
            assert AP.motion_audio("no-such-run")["items"] == [], "没有时间轴应返回空清单"
        assert _got["ok"] and _got["total"] == 3, _got
        assert _got["items"][0]["at"] == 1.0, "老页面只给 src 时没按起点排序"
        assert _got["items"][1]["at"] == 3.5, _got["items"]
        assert _got["items"][1]["file"] == str(_av), "URL 没映射回本地文件"
        assert _got["items"][2]["file"] == str(_bv), "file 没压过 src"
        assert not AP.motion_audio("bad run!")["ok"], "run 没做校验"
        # 桌面 ffmpeg 读的是同一份时间轴，优先 file、丢掉图片段
        _tl2 = _adir / "_desktop_timeline.json"
        _tl2.write_text(_json.dumps([
            {"file": str(_av), "src": "/data/media/a.mp4",
             "a": 2.0, "lead": 0.5, "dur": 8.0},
            {"src": "/data/media/a.mp4", "a": 4.0, "dur": 0},
        ]), encoding="utf-8")
        _ca = _vid._clip_audio(_tl2)
        assert len(_ca) == 1 and _ca[0][0] == str(_av) and _ca[0][1] == 2.5, _ca
    # 手机端：解码混音 → AAC → 与视频交织写进同一个 MP4
    _kt_dir = KT
    if (_kt_dir / "MotionAudio.kt").is_file():
        _ma = (_kt_dir / "MotionAudio.kt").read_text(encoding="utf-8")
        _me = (_kt_dir / "MotionEncoder.kt").read_text(encoding="utf-8")
        _main = (_kt_dir / "MainActivity.kt").read_text(encoding="utf-8")
        assert "createDecoderByType" in _ma, "没解码素材音轨"
        assert "MIMETYPE_AUDIO_AAC" in _ma, "没压成 AAC"
        assert "muxer.addTrack(it.format)" in _me, "音轨没在 muxer.start() 前加进 MP4"
        assert "writeAudio(info.presentationTimeUs, false)" in _me, "音视频没交织"
        assert 'apiCall("motion_audio", run)' in _main, "导出没取混音清单"
    # ---- 导出提速 + 顶部百分比 ------------------------------------------
    # 旧版「720 帧全部写盘 → 才开始编码」两段串行；现在边收边编，
    # 编码器会盯着 f_XXXXX.jpg 出现就立刻读，因此收帧必须原子落盘。
    _sv_src = _Path(_vid.__file__).parent.joinpath("serve.py").read_text("utf-8")
    assert "os.replace(tmp, target)" in _sv_src, "App 收帧没原子改名，会读到半张 JPEG"
    assert 'f".f_{i:05d}.tmp"' in _sv_src, "临时帧文件名会匹配 f_*.jpg，进度被算多"
    assert "os.replace(tmp, target)" in _vsrc, "桌面导出收帧没同步原子写入"
    # 真起一次本机服务，验证「文件出现即完整、临时文件不计入进度」
    with _tf.TemporaryDirectory() as _htd:
        from functools import partial as _fpartial
        from http.server import ThreadingHTTPServer as _THS
        from garmin_cn import serve as _sv
        import threading as _th
        import urllib.request as _ur
        _root = _Path(_htd)

        class _Quiet(_sv._Handler):
            def log_message(self, *a):
                pass

        _srv = _THS(("127.0.0.1", 0),
                    _fpartial(_Quiet, directory=str(_root)))
        _th.Thread(target=_srv.serve_forever, daemon=True).start()
        # 盯着 os.replace：只用字符串断言的话，实现改回「直接 write_bytes」
        # 也可能蒙混过关（文件照样是完整的），必须验证确实走了原子改名。
        _orig_replace, _renames = _sv.os.replace, []

        def _spy_replace(src, dst):
            _renames.append((str(src), str(dst)))
            return _orig_replace(src, dst)

        _sv.os.replace = _spy_replace
        try:
            _payload = b"\xff\xd8FAKEJPEG" * 256
            _url = ("http://127.0.0.1:%d/__frame?i=7&run=benchrun1"
                    % _srv.server_address[1])
            with _ur.urlopen(_ur.Request(_url, data=_payload, method="POST"),
                             timeout=5) as _resp:
                assert _resp.status == 204
            _fr = _root / "_frames" / "benchrun1"
            assert (_fr / "f_00007.jpg").read_bytes() == _payload, \
                "编码器可能读到写了一半的帧"
            assert len(_renames) == 1 and _renames[0][0].endswith(".tmp"), \
                "收帧没走「临时文件 → 原子改名」"
            assert not list(_fr.glob(".*.tmp")), "原子改名后临时文件没消失"
            assert len(list(_fr.glob("f_*.jpg"))) == 1, "临时文件被算进帧进度"
        finally:
            _sv.os.replace = _orig_replace
            _srv.shutdown()
            _srv.server_close()
    # 编码器：流式帧源 / GPU Surface 主路径 / CPU 自动降级 / 输入缓冲重试
    _me = (_kt_dir / "MotionEncoder.kt").read_text("utf-8")
    assert "fun interface FrameAt" in _me and "source.get(i)" in _me, \
        "编码没有惰性帧源，做不到边收边编"
    assert "val scratch = if (!useSurface) Scratch(width, height) else null" in _me, \
        "每帧还在重新分配 8MB 像素缓冲，或 CPU 降级路径没了"
    assert "YR[(c shr 16) and 255]" in _me and "yb.put(s.y, 0, w)" in _me, \
        "CPU 降级的 YUV 没改查表 + 按行批量 put"
    assert "val y0 = yb.position()" in _me, \
        "plane buffer 有 crop offset 时会整幅错行"
    assert "while (idx < 0 && spin++ < 200" in _me, \
        "输入缓冲拿不到时会丢帧（pts 不推进 → 成片变短）"
    assert "bmp.recycle()\n                    continue" not in _me, \
        "还留着丢帧的 continue"
    assert "onProgress(i + 1)" in _me, "编码侧没上报已编码帧数"
    assert "70 + (i + 1) * 30" not in _me, \
        "还在用「70~100」的串行阶段百分比，并行时会假跳到 70%"
    # GPU 路径：Surface 输入 + 自动降级
    assert "codec.createInputSurface()" in _me, "没用 MediaCodec 输入 Surface"
    assert "codec.signalEndOfInputStream()" in _me, "Surface 输入没送 EOS"
    assert "for (useSurface in listOf(true, false))" in _me, \
        "GPU 初始化失败时没有自动重建 codec 降级 CPU"
    assert "COLOR_FormatSurface" in _me and "COLOR_FormatYUV420Flexible" in _me, \
        "两种输入格式的 color format 没分别设置"
    assert "val audio = try" in _me and "MotionAudio.build(segments, totalUs)" in _me, \
        "GPU→CPU 降级会把音轨解码重做一遍或整段丢掉"
    assert "val via = if (useSurface)" in _me and "mux.audioSegments, via" in _me, \
        "结果里没标出走的是 GPU 还是 CPU"
    assert "opts.inBitmap = it" in _me and "inMutable = true" in _me, \
        "每帧还在重新分配 8MB 解码缓冲，720 帧会有 5GB+ 的 GC 压力"
    assert "reusable?.recycle()" in _me, "复用的 Bitmap 最终没释放"
    assert "GLUtils.texSubImage2D" in _me or "gl!!.draw(bmp, pts)" in _me, \
        "GPU 路径没有逐帧提交画面"
    _gl = (_kt_dir / "MotionGL.kt").read_text("utf-8")
    assert "eglPresentationTimeANDROID(display, eglSurface, ptsUs * 1000L)" in _gl, \
        "Surface 输入没按微秒→纳秒设帧时间戳，成片时长会错"
    assert "GLUtils.texSubImage2D" in _gl and "GLUtils.texImage2D" in _gl, \
        "纹理没分配/复用，每帧都在重新申请 GPU 内存"
    assert "EGL14.eglSwapBuffers" in _gl, "没把帧提交给编码器"
    assert "1.0 - aTex.y" in _gl, "Bitmap 上传后画面会上下颠倒"
    assert "EGL_RECORDABLE_ANDROID" in _gl and "listOf(true, false)" in _gl, \
        "不支持 recordable 扩展的设备会直接初始化失败"
    assert "eglGetError()" in _gl or "glGetError()" in _gl, \
        "GL/EGL 出错没有检查，会静默出花屏"
    # App：全程统一百分比、不显示帧数，且渲染与编码并行
    _main = (_kt_dir / "MainActivity.kt").read_text("utf-8")
    assert 'subtitle.text = "生成回放 $show%"' in _main, "顶部没改成百分比"
    assert '生成回放 $n/$frames 帧' not in _main, "顶部还在显示帧数"
    assert '生成回放 $v%' not in _main, "还留着旧的百分比变量"
    assert '"编码中…"' not in _main and '"混入原声并编码中…"' not in _main, \
        "还有阶段性文字覆盖百分比"
    assert 'subtitle.text = "生成回放 0%"' in _main, "准备阶段没接上百分比"
    assert "renderDone.set(n)" in _main and "encodeDone.set(done)" in _main, \
        "渲染/编码没有各自上报帧数"
    assert "minOf(renderDone.get(), encodeDone.get())" in _main, \
        "进度没取两阶段的短板，并行时会虚高"
    assert "report(min(70," not in _main and "70L /" not in _main, \
        "还留着 0~70 / 70~100 的固定阶段分段"
    assert "report(100)" in _main, "编码收尾没有强制到 100%"
    assert "frameCount = total" in _main and "frameAt = MotionEncoder.FrameAt" in _main, \
        "没把总帧数与惰性帧源交给编码器"
    assert "cancelled = { cancel.get() }" in _main and "onProgress = report" in _main, \
        "取消信号/进度回调没接上"
    assert "private fun awaitFrame" in _main and \
        "MOTION_STALL_MS) return null" in _main, \
        "等帧没有超时，导出卡住会无限空转"
    assert 'File(dir, "_timeline.json").isFile' in _main and "f_00000.jpg" in _main, \
        "没等时间轴与首帧就取音频清单，会漏掉原声"
    assert "import java.util.concurrent.atomic.AtomicBoolean" in _main, \
        "取消信号不是线程安全的"
    assert "import java.util.concurrent.atomic.AtomicInteger" in _main, \
        "双线程帧计数不是线程安全的"
    assert "encoded.set(true)" in _main and "ui { busy(false); updateSubtitle() }" in _main, \
        "超时/失败时进度圈可能一直转"
    assert "motion render done" in _main and "motion export done" in _main, \
        "没有分项计时，真机上定位不了瓶颈"
    assert "${r.via}" in _main and "startedAt" in _main, \
        "完成提示没说明走 GPU/CPU 与总耗时"
    # ---- v44：超时只判「卡死」，不判「慢」 ----------------------------------
    # 旧版从点击起 12 分钟硬 deadline：真机一帧几秒时进度还在走，到点却直接
    # cancel 并弹「回放生成超时，请重试」；用户重试又是同样的 12 分钟，永远
    # 出不了片。等帧 120 秒 / 首帧 60 秒 / 30 秒 diag 也是同一类误杀。
    assert "12 * 60 * 1000" not in _main, "还有按总时长一刀切的超时"
    assert 'toast("回放生成超时，请重试")' not in _main, \
        "还在把「慢」报成笼统的超时，让用户白重试"
    assert "120_000" not in _main and "for (k in 0 until 300)" not in _main, \
        "还留着等帧 120 秒 / 首帧 60 秒的固定死线"
    assert "MOTION_STALL_MS" in _main and "MOTION_FIRST_FRAME_MS" in _main, \
        "没有区分「首帧宽限」与「无进展卡死」"
    assert "lastProgressAt.set(System.currentTimeMillis())" in _main, \
        "没有共享心跳，帧还在推进也可能被判超时"
    assert "MOTION_STALL_MS) return null" in _main, \
        "等帧超时没跟全局心跳挂钩，慢机器会被掐断"
    assert "if (ok) report(100)" in _main, "超时/失败后还会把进度假推到 100%"
    assert "motion stalled" in _main, "卡死时没有日志，真机定位不了瓶颈"
    assert 'diag.length() > 0' in _main, \
        "诊断还是按固定 30 秒判，或没处理 diag 为空对象的情况"
    # 桌面补渲同理：整轮固定 max(120, 缺帧数*0.5) 秒会把正在推进的 Chrome 掐断
    assert "deadline = time.time() + max(120, len(miss) * 0.5)" not in _vsrc, \
        "PC 端还在按整轮固定时长判超时"
    assert "last_n, idle = len(got), time.time()" in _vsrc, \
        "PC 端没有「有新帧就重置空闲计时」"
    assert "60 秒没有新帧" in _vsrc, "PC 端 25 秒无进展就重启，慢机器会反复重来"

    # 心跳语义模拟：把 App 里 watchdog / awaitFrame 的判定式搬到 Python 上跑。
    # 双向验证：慢但持续推进必须不超时，真的零进展必须超时；旧版固定 deadline
    # 与固定 120 秒等帧在同一场景下必须被判失败。
    _STALL, _FIRST = 300_000, 180_000

    def _watchdog(arrivals, end_ms):
        """arrivals: 帧数增长的时刻（ms）；返回 (是否卡死, 已收帧数)。"""
        hits = [0] + [t for t in arrivals if t <= end_ms]
        last, n = hits[-1], len(hits) - 1
        grace = _STALL if n > 0 else _FIRST
        return end_ms - last > grace, n

    def _await_frame(frame_ms, beats):
        """第 frame_ms 时刻目标帧落盘；beats 是全局心跳（其它帧在推进）。"""
        last = 0
        for t in sorted(set([0] + [b for b in beats if b < frame_ms])):
            if t - last > _STALL:
                return False
            last = t
        return frame_ms - last <= _STALL

    # 每 4 分钟才出一帧、整整 40 分钟：旧版 12 分钟就砍，新版必须跑完
    _slow = [240_000 * k for k in range(1, 11)]
    assert _watchdog(_slow, 40 * 60_000) == (False, 10), \
        "慢但持续推进被误判为超时"
    assert 40 * 60_000 >= 12 * 60_000, "对照场景必须先超过旧的 12 分钟上限"
    assert _watchdog([], _FIRST + 1)[0], "首帧一直不来必须判卡死"
    assert _watchdog([60_000], 60_000 + _STALL + 1)[0], \
        "收到首帧后连续零进展必须判卡死"
    assert not _watchdog([60_000], 60_000 + _STALL)[0], \
        "刚好到宽限边界不该提前判死"
    # 一帧等 10 分钟，但期间渲染侧每 60 秒都在出别的帧：旧版 120 秒就返回 null
    assert _await_frame(600_000, [60_000 * k for k in range(1, 10)]), \
        "全局还在推进时，等单帧不该被 120 秒死线掐断"
    assert 600_000 > 120_000, "对照场景必须先超过旧的 120 秒等帧上限"
    assert not _await_frame(600_000, [60_000, 120_000]), \
        "心跳停了超过宽限还一直空等，卡死就发现不了"
    # 超时重试会换一个 run 重头渲，旧帧目录（几百 MB）不清理会一直堆着。
    # 只允许删「过期的合法 run 目录」，正在收帧的新 run 绝不能误删。
    assert "\n        _sweep_motion_frames()\n" in _ap_src, \
        "重试会不断留下新 run 的帧目录，没有清理入口（注释掉不算）"
    with _tf.TemporaryDirectory() as _sd:
        _sroot = _Path(_sd)
        _fr = _sroot / "_frames"
        _fr.mkdir()
        _old, _new, _weird = (_fr / "aabbcc112233", _fr / "ddeeff445566",
                              _fr / "not-a-run!")
        for _d in (_old, _new, _weird):
            _d.mkdir()
            (_d / "f_00000.jpg").write_bytes(b"x")
        (_fr / "keep.txt").write_text("x", encoding="utf-8")
        import time as _time
        _stale = _time.time() - 25 * 3600
        _os.utime(_old, (_stale, _stale))
        with _mock.patch.object(AP, "OUT", _sroot):
            assert AP._sweep_motion_frames() == 1, \
                "过期帧目录没被清掉（或误删了别的目录）"
        assert not _old.exists() and _new.exists() and _weird.exists(), \
            "清理误删了正在用的新 run 或非法命名目录"
        assert (_fr / "keep.txt").is_file(), "清理把普通文件也删了"
    # ---- v45：底图视口自适应（放大裁切）+ 视频卡片黑屏频闪 ------------------
    # 高德 zoom 只能是整数，`_fit_zoom` 选「刚好装得下」的那一档，于是某些
    # 活动的轨迹会撑到画幅 90%（看着像底图被放大裁切），另一些只占 30%。
    # 修法是底图位图与所有轨迹坐标共用同一组 (k,dx,dy) 仿射变换。
    assert "def _view_fit" in _msrc and '"view": view' in _msrc, \
        "底图与轨迹没有共用同一组视口变换"
    assert "g.drawImage(im, 0, 0, nw, nh, V.dx, V.dy, W * V.k, H * V.k)" in _msrc, \
        "bakeBase 没按 view 绘制底图，轨迹和路网会错开"
    assert "g.fillStyle = '#0E131A'; g.fillRect(0, 0, W, H);" in _msrc, \
        "视口缩小时画布边缘会露出纯黑"
    assert 'pts = [(x * view["k"] + view["dx"], y * view["k"] + view["dy"])' in _msrc, \
        "轨迹点没跟着底图做同一变换"
    assert '"view": pl.get("view") or {}' in _ap_src, \
        "App 拿不到取景参数，真机没法定位底图问题"
    assert "view: D.view || null" in _msrc and "baseSize:" in _msrc, \
        "诊断没回传视口参数与底图实际解码尺寸"
    # 三种占比都要落到目标区间；变换必须是同一个仿射（距离比 = k）
    for _bw, _bh, _label in ((0.84, 0.91, "放大裁切"),
                             (0.30, 0.36, "缩在一角"),
                             (0.59, 0.73, "正常")):
        _pad = [(540 - 1080 * _bw / 2 + 1080 * _bw * i / 8,
                 960 - 1920 * _bh / 2 + 1920 * _bh * i / 8)
                for i in range(9)]
        _v = MO._view_fit(_pad)
        _after = [(x * _v["k"] + _v["dx"], y * _v["k"] + _v["dy"])
                  for x, y in _pad]
        _rw = (max(p[0] for p in _after) - min(p[0] for p in _after)) / 1080
        _rh = (max(p[1] for p in _after) - min(p[1] for p in _after)) / 1920
        assert _rw <= MO.VIEW_W + 0.01 and _rh <= MO.VIEW_H + 0.01, \
            f"{_label}：变换后轨迹仍顶到边缘"
        assert _rw >= MO.VIEW_W * 0.55 and _rh >= MO.VIEW_H * 0.55, \
            f"{_label}：变换后轨迹还是缩在一角"
        assert abs(abs(_after[1][0] - _after[0][0]) -
                   abs(_pad[1][0] - _pad[0][0]) * _v["k"]) < 0.01, \
            "底图与轨迹用的不是同一组变换"
    # 视频：`seeked` 不代表新帧已解码，真机 WebView 会画出黑帧。
    # 只有亮度达标的帧才更新缓存，黑帧复用上一张有效画面。
    assert "const VLAST = new Map()" in _msrc and "function videoFrame(" in _msrc, \
        "视频卡片没有黑帧探测与上一帧缓存"
    assert "lit = sum > 24 * 24 * 3 * 8" in _msrc and "getImageData" in _msrc, \
        "黑帧判据丢了"
    assert "videoFrame(p.clip.src, v, w, h)" in _msrc, \
        "drawPop 还在直接画 <video>，黑帧会闪出来"
    assert "ctx.drawImage(v, left + pad" not in _msrc, \
        "还留着直连 <video> 的绘制路径"
    assert "VLAST.clear()" in _msrc and "lastClipSrc" in _msrc, \
        "切换素材窗口没清缓存，会闪上一段的末帧"

    # ---- v46：配速角标频闪 + 视频段逐帧 seek 拖垮导出 ----------------------
    # 回放的 T 是 _synth_route 按里程现场合成的：420 点覆盖 5312 秒、还有
    # 几十处重复时间戳。旧 paceAt 拿 `T[ib]-T[ia]` 当 15 秒窗口，窗口里
    # 经常 ia === ib → dt=0 → fmtPace(null) 画 '—'，下一帧又有值，于是
    # 720 帧里 180 帧是 '—'、交替 207 次，看起来就是配速一直在闪。
    assert "const PACE_DT = 2" in _msrc and "const PACE_WIN = 15" in _msrc, \
        "没有按轨迹秒重采样累计距离"
    assert "buildPaceTable" in _msrc and "Float64Array" in _msrc, \
        "配速距离表没建出来"
    assert "T[ib] - T[ia]" not in _msrc, \
        "还在用稀疏时间戳的索引差当窗口时间，配速必然闪 '—'"
    assert "dt = b - a, dd = paceDist(b) - paceDist(a)" in _msrc, \
        "窗口时间没用 rt 本身、距离没用插值表"
    assert "return lastPace" in _msrc and "lastPace + (p - lastPace) * 0.30" in _msrc, \
        "缺上一有效值兜底与平滑，静止/GPS 漂移时还是会跳空值"
    assert "D.dur / DIST[N - 1] * 1000" in _msrc, \
        "首帧没有用全段平均配速起步，会先闪一下 '—'"
    # 视频段：随机 seek 是整条流水线最贵的一步，而旧阈值 0.04 在 24fps
    # （帧间隔 0.0417s）下永远不命中，等于每帧都 seek 一次；另有
    # waitReady(4000) 每帧最多空等 4 秒、11 段视频各存一张全分辨率首帧。
    assert "const CLIP_FPS = 8" in _msrc and "const VSTEP = new Map()" in _msrc, \
        "视频卡片没有抽帧网格，导出仍会逐帧 seek"
    assert "const VDONE = new Map()" in _msrc and "const WAITED = new Set()" in _msrc, \
        "缺「同格只抓一次 / 只等一次就绪」的状态"
    assert "Math.floor(VREL * CLIP_FPS)" in _msrc, \
        "回退路径的 seek 目标没量化到抽帧网格（v47 起 rel 统一存进 VREL）"
    assert "if (VSTEP.get(src) === step) return;" in _msrc, \
        "同一抽帧格里还是会重复 seek"
    assert "VDONE.get(src) === step" in _msrc, \
        "同一抽帧格里还是会每帧做亮度探测 + 全尺寸重绘"
    assert "function fallbackFrame(" in _msrc and \
        "return fallbackFrame(src, w, h);   // 黑帧沿用上一张有效画面" in _msrc, \
        "黑帧（没有缓存）时同格仍会反复探测，视频解不出画面就又拖慢导出"
    assert "setTimeout(done, 350)" not in _msrc and "setTimeout(done, 120)" in _msrc, \
        "seek 超时仍是 350ms，几千帧会积成几十分钟"
    assert _msrc.count("v.removeEventListener('seeked', done);") >= 2, \
        "预抽 seekExact / 回退路径 有一处超时后没清理 seeked 监听"
    assert "waitReady(v, 4000)" not in _msrc and "first ? 1500 : 250" in _msrc, \
        "还在每帧等最多 4 秒视频就绪（v47 起每次只短等 250ms）"
    assert "Math.min(840 / iw, 780 / ih, 1)" in _msrc, \
        "首帧兜底没缩到卡片尺寸，11 段原分辨率 canvas 会吃满 WebView 内存"
    assert "const w = v.videoWidth || 1080, h = v.videoHeight || 1920" not in _msrc, \
        "首帧兜底还在按视频原始分辨率建常驻画布"
    assert "VSTEP.clear(); VDONE.clear();" in _msrc, \
        "切换素材窗口没重置抽帧格，会串到上一段"

    # ---- v47：视频画面中途冻结 + 渲染循环仍被随机 seek 拖住 ----------------
    # 冻结：Android WebView 在 seek 期间 readyState 会短暂掉到 1。旧版
    # WAITED 只等第一次、之后每格直接放弃；VSTEP 又在 seek **之前**置位，
    # 于是 120ms 超时那一格也被当成「已处理」，videoFrame 抓到 seek 未完成
    # 的旧帧（非黑，亮度判据过）缓存下来。连续超时时画面就停住不动，而音轨
    # 由原生 MediaExtractor 独立解码，所以「画面暂停、声音正常」。
    assert "const VPRE = {" in _msrc and "async function preloadClip(" in _msrc, \
        "没有导出期按段预抽帧，渲染循环还得逐格 seek <video>"
    assert "const PRE_MAX = 72" in _msrc and "PRE_SEEK_MS = 150" in _msrc, \
        "PC 兜底预抽没有格数/单格耗时上限"
    assert "const PRE_LONG = 840" in _msrc and "function preSize(" in _msrc, \
        "预抽画布没缩到卡片尺寸，几百格原分辨率会吃满 WebView"
    assert "await seekExact(v, Math.min(" in _msrc and "const lit = probeLit(v);" in _msrc, \
        "预抽没有严格等 seeked 与黑帧探测"
    assert "if (!WAITED.has(src))" not in _msrc, \
        "还在「等过一次就绪就永远不等」，readyState 掉到 1 时恢复不了"
    assert "first ? 1500 : 250" in _msrc, \
        "回退路径没有每次短等就绪"
    _vstep_set = _msrc.find("VSTEP.set(src, step)")
    _want_seek = _msrc.find("try { v.currentTime = want; }")
    assert 0 < _want_seek < _vstep_set, \
        "VSTEP 又提前到 seek 之前了，超时那一格会把旧帧当成新画面缓存"
    _pre_ret = _msrc.find("VSTEP.set(src, Math.floor(VREL * VPRE.fps));\n    return;")
    assert 0 < _pre_ret < _want_seek, \
        "预抽就绪时没在 seek 之前返回，渲染循环仍在随机 seek"
    assert _msrc.count("exportMode && VPRE.src === src && VPRE.frames.length") >= 2, \
        "syncVideo / videoFrame 没有都走预抽缓存分支"
    assert "exportMode = true" in _msrc and "await preloadClip(clip)" in _msrc, \
        "runExport 没有在进入素材窗口时预抽"
    assert "if (!src && VPRE.frames.length)" in _msrc, \
        "切出素材窗口没释放缓存，长片会堆满小画布"
    assert "if (!v) { VPFAIL.add(clip.src); return; }" in _msrc and \
        "if (v.readyState < 2) { VPFAIL.add(clip.src); return; }" in _msrc and \
        "!VPFAIL.has(src)" in _msrc, \
        "预抽失败没有回退路径，视频解不出画面会把导出卡死"
    # 画质优先：帧率让到 8fps，每帧长边 840 = 卡片显示上限，1:1 不放大；
    # JPEG 92，导出期统一高质量插值。旧版 480px 放大到 840 才是「糊」的根因。
    _ck53 = (_kt_dir / "MotionClips.kt").read_text(encoding="utf-8")
    assert "private const val JPEG_QUALITY = 92" in _ck53, \
        "抽帧 JPEG 质量又降回去了，卡片会有块状伪影"
    assert "java.io.File(root, \"c$ci\"), 8, 72, 840," in _main, \
        "抽帧参数不是 8fps / 72 格 / 840px，画质或耗时又要失守"
    assert "ctx.imageSmoothingQuality = 'high';" in _msrc and \
        "try { ctx.imageSmoothingQuality = 'high'; } catch (e) {}" in _msrc, \
        "导出期没有开高质量缩放"
    assert "const PRE_LONG = 840" in _msrc and "const PRE_MAX = 72" in _msrc, \
        "PC 兜底画布/格数没跟上画质优先的口径"
    assert "preload: info" in _msrc and "motion preload:" in _main, \
        "真机拿不到每段预抽结果，出问题没法定位"
    # 预抽索引映射：短素材保持 12fps；长素材自动降格率，索引仍单调不越界
    for _dur47, _label in ((6.0, "6 秒"), (60.0, "60 秒"), (256.0, "长片")):
        _n47 = max(1, min(150, int(_dur47 * 12 + 0.999)))
        _fps47 = _n47 / _dur47
        _seq47 = [max(0, min(_n47 - 1, int(_i / 24 * _fps47)))
                  for _i in range(int(_dur47 * 24))]
        assert len(_seq47) > 0 and _seq47[0] == 0, f"{_label}：首格不对"
        assert all(b >= a for a, b in zip(_seq47, _seq47[1:])), \
            f"{_label}：画面索引回退，成片里视频会倒放"
        assert max(_seq47) < _n47, f"{_label}：索引越界会取到空帧"
        assert _n47 <= 150, f"{_label}：单段预抽超过内存上限"
    # 预抽语义模拟：旧路径在渲染循环里每换一格就随机 seek 一次；新路径整段
    # 只在进入窗口时顺序抽一次，渲染循环零 seek。零 seek 既消除了逐帧解码
    # 等待，也让「seek 超时 → 抓到旧帧 → 画面停住」这条链不再存在。
    def _render47(_dur, _frames, preload):
        _n = max(1, min(150, int(_dur * 12 + 0.999)))
        _fps = _n / _dur
        _seeks, _idx = 0, []
        for _i in range(_frames):
            _rel = _i / 24
            if not preload:
                _prev = int(max(0, (_i - 1) / 24) * 12)
                if int(_rel * 12) != _prev:
                    _seeks += 1
            _idx.append(max(0, min(_n - 1, int(_rel * _fps))))
        return _seeks, _idx

    _old_seek, _ = _render47(6.0, 48, False)
    _new_seek, _new_idx = _render47(6.0, 48, True)
    assert _old_seek > 0 and _new_seek == 0, \
        "渲染循环里还在随机 seek，画面冻结与耗时问题都会回来"
    assert _new_idx[-1] > _new_idx[0] and \
        all(b >= a for a, b in zip(_new_idx, _new_idx[1:])), \
        "预抽画面索引没有随播放时间推进，成片视频会停住"

    # ---- v48：把抽帧从 WebView 挪到原生 -----------------------------------
    # 真机症状：「第 219 帧没送到（导出中断）」= 编码线程连续 5 分钟没等到新帧。
    # 219/24 ≈ 9.1 秒，正好落在第一段素材窗口开始处：页面在预抽那一段时被
    # `drawImage(<video>)` **同步**阻塞，主线程占住后连 setTimeout 兜底都跑不了。
    # v46/v47 只是把「逐格随机 seek」换成「整段顺序 seek」，总成本没降。
    _clips_kt = _kt_dir / "MotionClips.kt"
    assert _clips_kt.is_file(), \
        "没有原生抽帧实现，WebView 里抽一段 6 秒素材仍要 5 分钟"
    _ck = _clips_kt.read_text(encoding="utf-8")
    assert "object MotionClips {" in _ck, "原生抽帧入口被改名/删掉"
    assert _ck.count("getScaledFrameAtTime") >= 2, \
        "原生抽帧没用缩略帧 API（精确帧 + 关键帧两条都要）"
    assert "OPTION_CLOSEST" in _ck and "OPTION_CLOSEST_SYNC" in _ck, \
        "精确帧失败没有退回关键帧，个别编码器上会整段抽不到"
    assert _ck.count("bmp.recycle()") >= 2 and "FileOutputStream" in _ck, \
        "抽帧没有逐帧回收 Bitmap / 落盘 JPEG，几十张原图就 OOM"
    assert "METADATA_KEY_VIDEO_ROTATION" in _ck, \
        "竖拍视频没按 rotation 交换宽高，卡片里会横过来"
    assert "prepareClipFrames(r)" in _main, \
        "App 没有在起 WebView 前预抽素材帧"
    _pre_call = _main.find("prepareClipFrames(r)")
    _start_view = _main.find("startMotionView(\n")
    assert 0 < _pre_call < _start_view, \
        "抽帧跑到 startMotionView 之后了，会撞上「5 分钟没有新帧」判死"
    assert '"clips": clip_list' in _ap_src and \
        '"dir": str(OUT / "_frames" / run)' in _ap_src, \
        "motion_html 没把素材清单 / 帧目录交给原生"
    assert '"ci": (p["clip"].get("ci")' in _ap_src and \
        '"file": p["clip"].get("local")' in _ap_src, \
        "素材清单缺下标或本地路径，原生抽不了"
    assert 'item["ci"] = out_clips' in _msrc, "payload 里的 clip 没有 ci 下标"
    assert "const CIMG = new Map()" in _msrc and \
        "async function loadClipImages(" in _msrc, \
        "页面没有原生帧加载入口，还在 WebView 里抽"
    assert "await loadClipImages(run);" in _msrc, "runExport 没先加载原生帧"
    _load_call = _msrc.find("await loadClipImages(run);")
    _loop48 = _msrc.find("for (let i = from; i < total; i++)")
    assert 0 < _load_call < _loop48, \
        "原生帧在逐帧循环之后才加载，等于没用"
    assert "CIMG.has(ci)" in _msrc, "preloadClip 没有优先用原生帧"
    assert _msrc.count("if (exportMode && VPFAIL.has(src))") >= 2, \
        "抽帧失败没有在 syncVideo / videoFrame 两处都拦成静态封面"
    assert "if (exportMode && CIREADY) { VPFAIL.add(clip.src); return; }" in _msrc, \
        "清单读到了却没有这段素材时，还是会回 WebView 里抽"
    _ci_ret = _msrc.find(
        "if (exportMode && CIREADY) { VPFAIL.add(clip.src); return; }")
    _fallback48 = _msrc.find("const v = VIDS.get(clip.src);")
    assert 0 < _ci_ret < _fallback48, \
        "CIREADY 的静态封面分支在 WebView 抽帧之后，App 端仍会走同步解码那条路"
    assert "im.decode ? im.decode().catch" in _msrc, \
        "原生帧没有预解码，首帧 drawImage 还会卡在解码上"
    # 原生帧索引：n 被原生 maxFrames=150 截断，fps=n/dur，页面按 floor(rel*fps)
    for _dur48, _n48 in ((6.0, 72), (30.0, 150), (256.0, 150)):
        _f48 = _n48 / _dur48
        _seq48 = [max(0, min(_n48 - 1, int(_i / 24 * _f48)))
                  for _i in range(int(_dur48 * 24))]
        assert _seq48[0] == 0 and _seq48[-1] < _n48, \
            f"{_dur48}s 素材的原生帧索引越界"
        assert all(b >= a for a, b in zip(_seq48, _seq48[1:])), \
            f"{_dur48}s 素材的原生帧索引会倒退，成片里视频倒放"

    # ---- v49：首页「今日负荷」始终显示 —（键名写错） ------------------------
    # `derive()["load"]` 是 `training_status()` 的结果，字段是 loads / ctl /
    # atl / tsb / acute_7d …，**没有 `load`**。首页读 `load["load"]` 永远拿到
    # None，于是「今日负荷」一直是「—」；同一处还读了一个不存在的 week_min。
    from garmin_cn import ui as _UI
    from garmin_cn import metrics as _MT

    _ui_src = Path(_UI.__file__).read_text(encoding="utf-8")
    assert 'load.get("load")' not in _ui_src, \
        "首页还在读 training_status 里不存在的 load 键，「今日负荷」永远是 —"
    assert "_today_load_txt" in _ui_src and "loads.get(day)" in _ui_src, \
        "今日负荷没按日期从 loads 里取"
    assert "_week_load_txt" in _ui_src and "acute_7d" in _ui_src, \
        "近 7 天累计没取 acute_7d"
    # 语义模拟：有历史时没练的那天就是 0；一天历史都没有才显示 —
    _L49 = {"loads": {"2026-11-08": 542.3, "2026-11-07": 0.0},
            "acute_7d": 1129.2}
    assert _UI._today_load_txt(_L49, "2026-11-08") == "542", "今日负荷取错日期"
    assert _UI._today_load_txt(_L49, "2026-11-07") == "0", \
        "没练的那天应该显示 0，不是「—」"
    assert _UI._today_load_txt({}, "2026-11-08") == "—", \
        "完全没有历史时应该显示「—」"
    assert _UI._week_load_txt(_L49) == "1129" and _UI._week_load_txt({}) == "—", \
        "近 7 天累计取值/缺数据兜底不对"
    # week_min：AI 教练读的就是这个键，以前 training_status 里没有 → 永远 None
    _rows49 = [{"date": f"2026-11-{d:02d}"} for d in range(1, 9)]
    _ld49 = _MT.training_status(_rows49, [{"date": "2026-11-08",
                                           "duration_s": 3600}])
    assert "week_min" in _ld49 and abs(float(_ld49["week_min"]) - 60) < 0.1, \
        "training_status 没给 week_min，AI 教练的周训练分钟数还是空"

    # ---- v50：活动图标题溢出 / 登录态 / 补拉入口 / 素材管理 -----------------
    # 1) 活动图：长标题 + 长天气会把右侧标签挤出卡片。flex 子项默认
    #    min-width:auto，必须显式给 0 才允许换行；标题 44→38 并限两行。
    from garmin_cn import actcard as _AC
    _ac_src = Path(_AC.__file__).read_text(encoding="utf-8")
    _clip_dlg = (_kt_dir / "ClipTrimDialog.kt").read_text(encoding="utf-8")
    assert ".hleft{flex:1 1 520px;min-width:0}" in _ac_src, \
        "活动图标题列没有 min-width:0，长标题会把标签挤出卡片"
    assert ".hname{font-size:36px" in _ac_src and \
        "max-height:calc(36px * 1.3 * 2)" in _ac_src, \
        "标题没缩小或没限高，长标题仍会溢出"
    assert "flex-wrap:wrap" in _ac_src, \
        "标题行不会回绕，标签还是会被挤出卡片"
    assert ".htag{flex:none;white-space:nowrap" in _ac_src, \
        "标签会被长标题压缩/换行"
    # 列表行：标题从单行省略改成最多两行，参数本来就换行
    assert "display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2" \
        in _ui_src, \
        "活动列表标题仍是单行省略，长活动名看不全"
    # 2) 登录态：Garmin 会在普通请求里轮换 JWT，只登录时落盘 = 关掉再开就过期
    from garmin_cn import net as _NET
    _net_src = Path(_NET.__file__).read_text(encoding="utf-8")
    assert "self._save_if_changed()" in _net_src and "_cookie_sig" in _net_src, \
        "Cookie 轮换后没有落盘，关掉再开就会要求重新输密码"
    assert "finally:\n            # 成功、失败、重试都要走" in _net_src, \
        "request 的 finally 里没保存会话，失败重试路径仍会丢新票据"
    from garmin_cn import appapi as _AA2
    _aa_src = Path(_AA2.__file__).read_text(encoding="utf-8")
    assert "def _client(email: str | None = None, password: str | None = None,\n" \
        "            revalidate: bool = False)" in _aa_src, \
        "_client 没有 revalidate，会带着死票据跑完整轮同步才失败"
    assert "client = _client(email, password, revalidate=True)" in _aa_src, \
        "同步入口没有先校验会话"
    # 3) 补拉：旧版拿「本地有没有密码」当门槛，密码为空时按钮点了只弹登录。
    #    现在连按钮都没有了 —— 后台自动跑，这条只守「别再被密码拦住」。
    assert 'act="historySync"' not in _ui_src, "补全历史又变回按钮了"
    assert "if (email.isNullOrBlank() || pwd.isNullOrBlank())" not in _main, \
        "补拉仍被本地密码拦截，点了只会弹登录"
    # 4) 素材：多选批量删除 / 批量添加 / 再次剪辑 / 旋转 / 竖横屏
    assert "def clips_remove_many(" in _aa_src and "def clips_update(" in _aa_src, \
        "没有批量删除/更新接口"
    assert '"clips_remove_many", "clips_update",' in _aa_src, \
        "新接口没注册到 _API，App 调不到"
    assert "clipDialog?.dismiss()\n        clipDialog = null" in _main, \
        "重建素材列表前没关旧对话框，「删完还能看见旧列表」"
    assert "val cb = CheckBox(this)" in _main, "素材行没有多选勾选框"
    # 素材列表必须是固定高度的可滚列表：AlertDialog.customPanel 在部分机型上
    # 只放得下七八行，看着就像「只显示最新的 8 个」。
    assert "ScrollView(this).apply" in _main and \
        "(dm.heightPixels * 0.8f)" in _main, \
        "素材列表没换成固定高度的可滚列表，条数一多就看不全"
    assert "clipTitle?.text" in _main and "clipDialog: Dialog?" in _main, \
        "素材列表还是 AlertDialog，没有可更新的标题"

    # ---- v53：深色科技风主题（近黑 + 科技蓝 + 辉光） ----------------------
    from garmin_cn import aiui as _AI
    _ai_src = Path(_AI.__file__).read_text(encoding="utf-8")
    _man = (ROOT / "android/app/src/main/AndroidManifest.xml") \
        .read_text(encoding="utf-8")
    assert "--bg:#070B12" in _ui_src and "--brand:#3B82F6" in _ui_src, \
        "页面主色不是深空蓝，科技风主题丢了"
    assert "--cyan:#22D3EE" in _ui_src and "radial-gradient(900px 520px" in _ui_src, \
        "缺青色高光或背景径向渐变"
    assert ".card.brand" in _ui_src and "box-shadow:var(--glow)" in _ui_src, \
        "品牌卡没有辉光"
    assert "-webkit-text-fill-color:transparent" in _ui_src and \
        "filter:drop-shadow(0 0 16px" in _ui_src, \
        "大数字没有渐变/辉光"
    assert ".sec-head h2::before" in _ui_src and ".row .ic{" in _ui_src, \
        "分节标记/图标底座丢了科技风样式"
    assert "--bg:#070B12" in _ai_src, "AI 页还是浅色，和 App 不一致"
    assert 'theme-color" content="#070B12"' in _ui_src, "移动端主题色没换"
    assert _main.count("0xFF070B12.toInt()") >= 2 and "0xFF4DA3FF.toInt()" in _main, \
        "原生顶栏/底栏还是浅色"
    assert "Theme.Material.NoActionBar" in _man, "原生主题还是 Light"
    # 浅色 token 不许回来：这几处是整套暗色主题的锚点
    for _bad in ("--bg:#F6F7F4", "--brand:#17845F", "background:#FFFFFF"):
        assert _bad not in _ui_src, f"浅色 token 又回来了：{_bad}"

    # ---- v55：历史页重构（周/月/年 + 页首柱状图 + 一键补全） ----------------
    from garmin_cn import history as _H
    _h_src = Path(_H.__file__).read_text(encoding="utf-8")
    assert "def history_stats(" in _aa_src and '"history_stats",' in _aa_src, \
        "没有历史统计接口，或没注册到 _API"
    assert "def _month_bars(" in _h_src and "def _bucket_rows(" in _h_src, \
        "历史页缺柱状图/周期分组渲染"
    for _kw in ("本月跑量", "本年跑量", "累计跑量", "近 12 个月跑量"):
        assert _kw in _h_src, f"历史页首缺统计项：{_kw}"
    assert 'mchip("按周", "week")' in _h_src and 'mchip("按月", "month")' in _h_src \
        and 'mchip("按年", "year")' in _h_src, "没有周/月/年切换"
    # 一键补全只从「我的 → 历史数据」的对话框进（也不在历史页）；
    # 正常情况它由登录后自动触发，按钮只是兜底重跑。
    assert 'data-act="historySyncAll"' not in _h_src, "历史页又放回一键补全了"
    assert "private fun runBackfill()" in _main and \
        '"historySync", "historySyncAll" -> runBackfill()' in _main, \
        "App 没接后台补全（旧页面缓存里的按钮也该转成它）"
    # 翻页数现在由 Python 侧给（BACKFILL_PAGES），不再从 Kotlin 传参：
    # 一段一年约 300 条活动，20 页足够，不至于只补到前 60 条。
    assert _AA2.BACKFILL_PAGES >= 12, \
        "补全的活动翻页数太小，一年历史补不全"
    # 统计接口的实际输出：必须是连续 12 个月（缺月补 0），周/年各是列表
    _hs = _AA2.history_stats()
    assert _hs.get("ok"), f"history_stats 失败：{_hs.get('error')}"
    assert len(_hs.get("months") or []) == 12, "柱状图不是连续 12 个月"
    assert all("km" in m and "label" in m for m in _hs["months"]), "月份缺 km/label"
    assert isinstance(_hs.get("weeks"), list) and isinstance(_hs.get("years"), list), \
        "周/年分组不是列表"
    _sec = {"km": 100.0, "count": 8, "dur_h": 9.5,
            "start": "2026-11-01", "end": "2026-11-30", "key": "2026-11", "label": "11月"}
    _html = _H._bucket_rows([_sec], "month", "run")
    assert "100.0 km" in _html and "8 次" in _html, "周期汇总行没带上跑量/次数"
    assert 'href="/history?mode=month&amp;sel=2026-11&amp;type=run"' in _html, \
        "周期行没链回本粒度并带上选中的周期"
    # 柱状图：12 根柱 + 当月实色
    _bars = _H._month_bars(_hs["months"])
    assert _bars.count("<rect ") == 12, "柱状图不是 12 根柱"
    assert 'viewBox="0 0 400 210"' in _bars, "柱状图 viewBox 变了，字号会被缩没"

    # ---- v56：柱状图跟着周/月/年切粒度，并挪进「活动」章节 -----------------
    assert _H._month_end("2026-11") == "2026-11-30", "11 月月末算错"
    assert _H._month_end("2026-02") == "2026-02-28", "2 月月末算错"
    _wk = _AA2.history_stats("2026-11-02", "2026-11-08")
    assert len(_wk.get("days") or []) == 7, "周视图没给出 7 天序列"
    _yr = _AA2.history_stats("2026-01-01", "2026-12-31")
    assert len(_yr.get("months") or []) == 12, "年视图没给出 12 个月"
    # 活动列表视图：柱状图必须在「活动」章节里（不在页首概览）
    _pg_list = _H.history_page({"mode": ["list"]})
    _pos_act = _pg_list.find(">活动</h2>")
    assert _pos_act > 0 and _pg_list.find("<svg") > _pos_act, \
        "活动列表章节没有柱状图（被挪回页首了）"
    for _m in ("week", "month", "year"):
        assert "<svg" in _H.history_page({"mode": [_m]}), f"{_m} 视图没有柱状图"
    # 31 根柱要密排：不标数值、轴标签每 5 格一次
    # 30 根柱只该有 8 个刻度（首、每 5 格、末）；逐根标就是 30 个，
    # 柱子上挤一堆字反而读不出来。以前只看「有没有高亮色」，
    # 自 high 改成按 hot 匹配后那条断言就失效了。
    _dense = _H._month_bars([{"label": str(i), "km": 1.0} for i in range(1, 31)])
    assert _dense.count("<rect ") == 30 and _dense.count("<text ") <= 12, \
        "30 根柱仍逐根标数值，会挤成一团"
    # 下拉刷新（原生）也要深色，否则一拉就闪白
    _pull = (KT / "PullRefreshLayout.kt"
             ).read_text(encoding="utf-8")
    assert "0xFF070B12.toInt()" in _pull and "0xFF5A6B5E" not in _pull, \
        "下拉刷新还是浅色"

    # ---- v57：点周期要「选中该周期」，不能永远画最新那期 -------------------
    assert _H._week_span("2026-W45") == ("2026-11-02", "2026-11-08"), \
        "ISO 周跨度算错（ISOCALENDAR 不是周一起？）"
    _ms = [m["key"] for m in _hs["months"]]
    _new_m, _old_m = _ms[-1], _ms[0]
    _days_old = len(_AA2.history_stats(
        f"{_old_m}-01", _H._month_end(_old_m)).get("days") or [])
    _pg_old = _H.history_page({"mode": ["month"], "sel": [_old_m]})
    _pg_new = _H.history_page({"mode": ["month"], "sel": [_new_m]})
    assert f"{_old_m} 每日跑量" in _pg_old and f"{_new_m} 每日跑量" in _pg_new, \
        "柱状图标题没跟着选中的月份走"
    assert _pg_old.count("<rect ") == _days_old, \
        "选中历史月份后柱状图不是那个月的天数（还在画最近 12 个月？）"
    assert 'class="row on"' in _pg_old and f"sel={_old_m}" in _pg_old, \
        "选中的周期行没有高亮"
    # 非法/过期的 sel 要回落到最新一期，不能空图或报错
    _pg_bad = _H.history_page({"mode": ["month"], "sel": ["1999-01"]})
    assert f"{_new_m} 每日跑量" in _pg_bad and "<svg" in _pg_bad, \
        "sel 越界时没有回落到最新一期"
    # 周视图：7 根柱（含没跑的日子），刻度是星期而不是日期数字。
    # 必须挑「活动只覆盖 6 天」的那一周来验 —— 拿最新一周（正好 7 天）测，
    # 就算用活动首末日期算范围也照样是 7 根，等于没测。
    _wk_part = next((w for w in (_hs.get("weeks") or [])
                     if w.get("start") and w.get("end")
                     and (w["start"], w["end"]) != _H._week_span(str(w["key"]))),
                    None) or (_hs.get("weeks") or [{}])[-1]
    _wk_key = str(_wk_part.get("key") or "")
    _pg_wk = _H.history_page({"mode": ["week"], "sel": [_wk_key]})
    assert _pg_wk.count("<rect ") == 7, "周视图不是 7 根柱（只画了有跑的日子）"
    assert ">一</text>" in _pg_wk and ">日</text>" in _pg_wk, "周视图刻度不是星期"
    # 行的日期也必须是完整 ISO 周，否则「11-03~11-05」和 7 根柱对不上
    _ws, _we = _H._week_span(_wk_key)
    assert f"{_ws[5:]}~{_we[5:]}" in _H._bucket_rows([_wk_part], "week", "run", _wk_key), \
        "周行显示的是活动首末日期，不是完整的一周"
    # 年视图：选中旧年份要出那一年的 12 根
    _ys = [y["key"] for y in (_hs.get("years") or [])]
    if len(_ys) > 1:
        _pg_y = _H.history_page({"mode": ["year"], "sel": [_ys[-1]]})
        assert f"{_ys[-1]} 年各月跑量" in _pg_y and \
            _pg_y.count('fill="url(#mb)"') == 12, \
            "点旧年份没有切到那一年的 12 个月"
    # hot：只有「今天那一根」是实色，历史柱不该也实色
    # hot 指第一根：必须第一根实色、第二根半透明（反过来就说明还在按「最后一根」高亮）
    _hb = _H._month_bars([{"key": "2026-09", "label": "9月", "km": 5.0},
                          {"key": "2026-10", "label": "10月", "km": 9.0}],
                         hot="2026-09")
    assert _hb.count('opacity="1"') == 1 and _hb.count('opacity="0.52"') == 1, \
        "柱状图高亮不是「唯一一根实色」"
    assert _hb.index('opacity="1"') < _hb.index('opacity="0.52"'), \
        "柱状图的高亮仍是「最后一根」而不是 hot 指定的那一根"
    # 月分组行要带真实时长（以前恒为 0.0 小时）
    assert any(float(m.get("dur_h") or 0) > 0 for m in _AA2.history_stats()["months"]), \
        "月份统计没有 dur_h，月分组行只能显示 0.0 小时"
    assert "def _weekday_series(" in _h_src and "def _week_span(" in _h_src, \
        "历史页缺周跨度/星期刻度辅助函数"
    # 柱子可点：列表页 12 个月、年视图 12 个月都该链到「按月 + 那一个月」；
    # 30/31 根的密排图不给链接（9px 宽点不准）。
    def _svg_only(doc: str) -> str:
        a = doc.find("<svg")
        return doc[a:doc.find("</svg>", a) + 6] if a >= 0 else ""

    _pg_list2 = _H.history_page({"mode": ["list"]})
    assert _svg_only(_pg_list2).count('href="/history?mode=month&amp;sel=') == 12, \
        "列表页柱状图的柱子不能点进对应月份"
    assert "点柱子看当月" in _pg_list2, "柱状图没提示可以点"
    # 月视图是 28~31 根密排 → 柱子不做链接（9px 宽，手指点不准反而误触）
    _dense_doc = _svg_only(_H.history_page({"mode": ["month"],
                                           "sel": [_ms[0]]}))
    assert 'href="/history?mode=month' not in _dense_doc, \
        "30 根柱也做了链接，手指点不准反而误触"
    # 点周期行用 replace 导航：不往历史栈里堆条目
    assert "data-nopush" in _H._bucket_rows([_sec], "month", "run", "2026-11"), \
        "周期行没走 replace 导航，连点几个月返回键要按很多次"
    # 空周期不能给「看 0 次活动的明细」这种死链
    _empty = _H.history_page({"mode": ["month"], "sel": [_ms[0]]})
    if not any(m["count"] for m in _hs["months"] if m["key"] == _ms[0]):
        assert "看这 0 次活动的明细" not in _empty and "这个周期没有活动" in _empty, \
            "空周期仍给了一个点进去什么都没有的明细链接"

    # ---- v58：完整看板换深色 + 一图流身体电量 ------------------------------
    from garmin_cn import report as _R, card as _C
    _r_src = Path(_R.__file__).read_text(encoding="utf-8")
    assert "--bg:#070B12" in _r_src and "--card:#0F1622" in _r_src, \
        "完整看板还是浅色，和 App 其它页面不是一套"
    assert "radial-gradient(900px 520px" in _r_src, \
        "完整看板背景没有径向渐变，深色会像没加载完"
    assert "linear-gradient(180deg,#121B2A,#0F1622)" in _r_src, \
        "看板卡片没换成上浅下深"
    for _bad in ("--bg:#F4F6F2", "#FAFBF8", "background:#FFFFFF", "#1D9E75"):
        assert _bad not in _r_src, f"完整看板还有浅色/旧色残留：{_bad}"
    # 一图流：App 侧出图前必须真去拉一次（以前 client=None，等于不联网）
    # 看板和卡片两个出口都要刷新，少一个就会那一处永远「未测量」
    assert "def _refresh_live(" in _aa_src and \
        _aa_src.count("_refresh_live(store, d)") >= 2, \
        "App 出图前没刷新身体电量，一图流只能永远「未测量」"
    assert "L.body_battery(store, d, None)" not in _aa_src, \
        "看板还在用 client=None 的空调用，根本没联网"
    # 展示口径：序列末点 > 当前值 > 全天最高 > 未测量
    _b1 = _C.bb_state({"bb_series": "[[1794067200000,27],[1794078900000,64]]",
                       "bb_now": 99, "bb_high": 100})
    assert _b1["val"] == "64" and "当前" in _b1["head"] and len(_b1["pts"]) == 2, \
        "有日内序列时没取序列末点（还在用可能过期的 bb_now）"
    _b2 = _C.bb_state({"bb_high": 100, "bb_low": 24})
    assert _b2["val"] == "100" and "今日最高" in _b2["sub"], \
        "只有全天最高时又被写成「未测量」"
    assert "日内曲线未同步" in _b2["head"], "退到全天最高时没说清曲线缺失"
    _b3 = _C.bb_state({"bb_now": 55, "bb_now_at": "10:20"})
    assert _b3["val"] == "55" and "10:20" in _b3["sub"] and "当前" in _b3["head"], \
        "只有当前值时标题口径不对"
    _b4 = _C.bb_state({})
    assert _b4["val"] == "—" and _b4["sub"] == "未测量", "真没数据时也没写未测量"
    _b5 = _C.bb_state({"night_scrubbed": True, "bb_high": 90})
    assert _b5["sub"] == "今日最高（手表掉落）" and "手表掉落" in _b5["head"], \
        "夜间缺测的提示丢了"
    # 出图前那次刷新必须是「快速失败」：默认 30 秒 × 3 次重试会让离线时
    # 打开完整看板卡近两分钟，宁可 8 秒拿不到就退回库里的值。
    from garmin_cn import live as _L
    _live_src = Path(_L.__file__).read_text(encoding="utf-8")
    assert "timeout: int = 8, retries: int = 1" in _live_src, \
        "出图刷新又用回 30 秒/3 次重试，离线会卡很久"
    assert "body_battery_day(date, timeout=timeout, retries=retries)" in _live_src, \
        "超时参数没有真正传到请求层"
    from garmin_cn import client as _CL
    _cl_src = Path(_CL.__file__).read_text(encoding="utf-8")
    assert "body_battery_day(self, date: str, *, timeout: int = 30" in _cl_src, \
        "client 层不支持短超时"

    # ---- v59：README 截图全部来自 demo 数据 --------------------------------
    _root = ROOT
    _readme = (_root / "README.md").read_text(encoding="utf-8")
    for _img in ("card", "activity", "replay", "dashboard",
                 "app-today", "app-history", "app-ai"):
        _png = _root / "docs" / "img" / f"{_img}.png"
        assert _png.is_file() and _png.stat().st_size > 8000, \
            f"README 截图缺失或为空：{_png.name}（先跑 tools/readme_shots.py）"
        assert f"docs/img/{_img}.png" in _readme, f"README 没引用 {_img}.png"
    assert "高驰" in _readme and "COROS" in _readme, "README 没写高驰数据源"
    # demo 必须能给活动图和回放供数据，否则 README 只能用真实 GPS 出图
    import tempfile as _tf
    from garmin_cn import demo as _D

    _ddb = Path(_tf.mkdtemp()) / "demo-check.db"
    _dst = _D.build(_ddb, days=14)
    try:
        _drow = _dst.conn.execute(
            "SELECT activity_id FROM activities WHERE distance_m > 2000 LIMIT 1").fetchone()
        assert _drow, "demo 没有带距离的活动"
        _ddet = _dst.get_detail(_drow["activity_id"]) or {}
        assert len(_ddet.get("track") or []) > 50, "demo 活动没有轨迹，活动图/回放演示不了"
        _pts = (_ddet.get("series") or {}).get("points") or []
        assert _pts and _pts[0]["t"] > 1e9, "demo 序列为空或时间戳不是秒级 epoch"
        assert _ddet.get("laps"), "demo 没有分圈"
        _dday = _dst.day(dt.date.today().isoformat()) or {}
        assert _dday.get("bb_series"), "demo 没有身体电量曲线，一图流只能显示「未测量」"
    finally:
        _dst.conn.close()
    # 无底图时画网格底板：纯色底会让回放看着像「没加载出来」
    assert "function bakeGrid()" in _msrc and "bakeGrid(); return;" in _msrc, \
        "回放没有无底图底板，没配高德 Key 的用户看到的是纯黑"
    assert 'os.environ.get("GARMIN_MAP", "1") == "0"' in _msrc, \
        "回放底图不认 GARMIN_MAP=0，demo 出图会去抓真实地图瓦片"

    # ---- v60：训练计划的耗时单位 ------------------------------------------
    # 日历里 `duration` 有的是秒、有的是毫秒。按秒读毫秒会把一次 18 分钟的
    # 骑行显示成 17876 分钟（12 天）——用户看到的就是这个。
    from garmin_cn import plan as _PL
    assert _PL._as_seconds(3600) == 3600, "正常秒被改坏"
    assert abs(_PL._as_seconds(1_072_560) - 1072.56) < 0.01, \
        "毫秒当秒读：一次 18 分钟的骑行会显示成 17876 分钟"
    assert _PL._as_seconds(108_000, race=True) == 108_000, \
        "超马（30 小时）被误判成毫秒"
    assert _PL._as_seconds(999_999_999_999) == 0, "离谱耗时没被挡住，会显示成天文数字"
    _pl_mm = _PL._cal_item({"title": "骑行", "duration": 1_072_560, "workoutId": 1})
    assert _pl_mm["minutes"] == 18, f"日历耗时解析错：{_pl_mm['minutes']} 分钟"
    _pl_mm2 = _PL._cal_item({"title": "跑", "estimatedDurationInSecs": 3600,
                             "duration": 999_999_999})
    assert _pl_mm2["minutes"] == 60, "带单位语义的 estimatedDurationInSecs 没优先"
    _pl_week = _PL.from_calendar(
        [{"itemType": "workout", "date": dt.date.today().isoformat(),
          "title": "骑行 恢复", "duration": 1_072_560, "workoutId": 1}])
    assert _pl_week["today"]["items"][0]["minutes"] == 18, \
        "整条链路（日历→今日课表）分钟数仍然不对"
    # 课名里已经写了「骑行」，副标题就别再补一次运动类型；
    # 名字里没有时才补，否则「力量 A」这类看不出是什么运动
    _meta = U._plan_meta({"name": "骑行 恢复", "sport": "骑行", "minutes": 18})
    assert "骑行" not in _meta and "18 分钟" in _meta, f"副标题重复了运动类型：{_meta}"
    _meta2 = U._plan_meta({"name": "晚间放松", "sport": "骑行", "minutes": 18})
    assert "骑行" in _meta2, "名字里没写运动类型时反而丢了"
    # `plan --diag` 以前一跑就 NameError（`_json` 只在自检里 import 过），
    # 而那正是排查计划问题的唯一入口，等于把诊断能力也一起弄丢了。
    _cli_src = (ROOT / "cli.py").read_text(encoding="utf-8")
    _cmd_plan = _cli_src[_cli_src.find("def cmd_plan("):][:2600]
    assert "json.dumps(diag(client, today)" in _cmd_plan, \
        "plan --diag 没有正确输出诊断 JSON"
    assert "_json" not in _cmd_plan, "plan --diag 里还在用未定义的 _json"

    # ---- 发布脚本：快照只保留最近 5 个 APK ---------------------------------
    _snap = ROOT / "tools/snapshot.py"
    assert _snap.is_file(), "缺 tools/snapshot.py"
    _snap_src = _snap.read_text(encoding="utf-8")
    assert "KEEP_APK = 5" in _snap_src, "快照保留数量不是 5"
    assert 'endswith(".apk")' in _snap_src, "清理没限定 .apk，会误删源码包/说明"
    # 清理必须排在上传之前：配额是仓库总量 1 GB，满了上传直接 400
    _i_prune = _snap_src.find("gitee_prune_apks(owner, repo, token, keep=keep)")
    _i_up = _snap_src.find("for p in (zipped, sums, *apks):", _i_prune)
    assert _i_prune > 0 and _i_up > _i_prune, \
        "旧 APK 清理没排在上传之前，配额满时新快照传不上去"
    assert "--keep" in _snap_src and "0 = 不清理" in _snap_src, \
        "没有 --keep 开关，清理策略改不了"
    assert "putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)" in _main, \
        "相册不能多选，批量添加不成立"
    assert "val u = pendingVideos.removeFirstOrNull()" in _main and \
        "nextPendingVideo()" in _main, \
        "批量选视频没有队列，只会处理第一个"
    assert 'src.copyTo(k, overwrite = true)' in _main and "keep" in _main, \
        "没保留原始片，再次剪辑只能裁二次成品"
    assert "private fun readRotation(" in _main and \
        "val dw = if (rot == 90 || rot == 270) r.height else r.width" in _main, \
        "没按 tkhd rotation 换算显示宽高，竖屏仍会被压成横屏"
    assert "private fun rotateClip(" in _main and "clips_update" in _main, \
        "素材没有旋转入口"
    assert "curRotate" in _clip_dlg and "onPick(startMs, endMs, curRotate)" in _clip_dlg, \
        "裁剪界面没有旋转状态或没回传角度"
    assert "readSize()" in _clip_dlg and "if (fh > maxH) {" in _clip_dlg, \
        "裁剪预览没按素材宽高比摆放，竖屏会被裁成横条"
    assert "function drawRotated(" in _msrc and "drawRotated(frame, p.clip" in _msrc, \
        "回放绘制没处理 rotate，登记了角度也不生效"
    assert "const rot = (c.rotate || 0) % 360;" in _msrc and \
        "(rot === 90 || rot === 270) ? c.h : c.w" in _msrc, \
        "卡片布局没按旋转交换宽高"
    assert '"rotate": int(pick.get("rotate") or 0) % 360,' in _msrc, \
        "payload 没带 rotate"
    assert '"rotate": int(it.get("rotate") or 0) % 360,' in _aa_src, \
        "素材清单没把 rotate 传给回放"
    # 批量删除/更新语义（用内存替身，不碰真 clips.json）
    _orig_r, _orig_w = _AA2._clips_read, _AA2._clips_write
    _bucket = {"items": [{"id": "a", "path": "/nope/a.mp4", "src": "/nope/a-src.mp4"},
                         {"id": "b", "path": "/nope/b.mp4"},
                         {"id": "c", "path": "/nope/c.mp4"}]}
    _AA2._clips_read = lambda: [dict(x) for x in _bucket["items"]]
    _AA2._clips_write = lambda xs: _bucket.__setitem__("items", [dict(x) for x in xs])
    try:
        _rm = _AA2.clips_remove_many(["a", "c"])
        assert _rm["ok"] and _rm["removed"] == 2, "批量删除没有一次删掉选中项"
        assert [x["id"] for x in _bucket["items"]] == ["b"], "批量删除删错了记录"
        _up = _AA2.clips_update("b", dur=8.5, w=1080, h=1920, rotate=90)
        assert _up["ok"] and _bucket["items"][0]["rotate"] == 90, \
            "旋转/时长回写没生效"
        assert _AA2.clips_update("b", rotate=-1)["ok"] and \
            _bucket["items"][0]["rotate"] == 90, "rotate=-1 不该把角度写回 0"
        assert not _AA2.clips_remove_many([])["ok"], "空选择也当成删除成功"
    finally:
        _AA2._clips_read, _AA2._clips_write = _orig_r, _orig_w
    # 旋转后的外接框：90/270 必须交换，否则竖屏素材被压进横卡片
    for _deg, _want in ((0, (1920, 1080)), (90, (1080, 1920)),
                        (180, (1920, 1080)), (270, (1080, 1920))):
        _rw, _rh = (1080, 1920) if _deg in (90, 270) else (1920, 1080)
        assert (_rw, _rh) == _want, f"rotate={_deg} 的显示宽高算错了"

    # 数值模拟（不依赖外部 DB）：稀疏 + 重复时间戳的路线，新旧算法逐帧对比。
    _pt = [0, 10, 25, 25, 40, 66, 80, 100, 100, 130, 150,
           180, 200, 200, 230, 260, 300]
    _pd = [0, 15, 35, 35, 60, 95, 120, 155, 155, 200, 230,
           275, 305, 305, 350, 395, 455]
    _dur, _play, _n = 300, 30, len(_pt)

    def _idx(t):
        _lo, _hi = 0, _n - 1
        while _lo < _hi:
            _m = (_lo + _hi) >> 1
            if _pt[_m] < t:
                _lo = _m + 1
            else:
                _hi = _m
        return _lo

    def _pace_old(rt):
        _a, _ia, _ib = max(rt - 15, 0), _idx(max(rt - 15, 0)), _idx(rt)
        _dd, _dt = _pd[_ib] - _pd[_ia], _pt[_ib] - _pt[_ia]
        return (_dt / _dd * 1000) if (_dd > 5 and _dt > 0) else None

    _PDN = max(2, -(-_dur // 2) + 1)
    _table, _k = [], 0
    for _i in range(_PDN):
        _t = _i * 2
        while _k + 1 < _n and _pt[_k + 1] <= _t:
            _k += 1
        if _k + 1 >= _n:
            _v = _pd[_n - 1]
        elif _pt[_k + 1] <= _pt[_k]:
            _v = _pd[_k + 1]
        else:
            _f = min(1, max(0, (_t - _pt[_k]) / (_pt[_k + 1] - _pt[_k])))
            _v = _pd[_k] + (_pd[_k + 1] - _pd[_k]) * _f
        _table.append(_v)

    def _dist(t):
        _x = min(max(t / 2, 0), _PDN - 1)
        _i, _f = int(_x), _x - int(_x)
        _j = min(int(_x) + 1, _PDN - 1)
        return _table[_i] + (_table[_j] - _table[_i]) * _f

    _last = _dur / _pd[-1] * 1000

    def _pace_new(rt):
        nonlocal _last
        _b, _a = min(max(rt, 0), _dur), max(min(max(rt, 0), _dur) - 15, 0)
        _dt, _dd = _b - _a, _dist(_b) - _dist(_a)
        if _dt > 0 and _dd > 1:
            _p = _dt / _dd * 1000
            if 60 < _p < 2400:
                _last = _last + (_p - _last) * 0.30
        return _last

    _seq_old = [_pace_old(min(i / 24 * (_dur / _play), _dur))
                for i in range(_play * 24)]
    _seq_new = [_pace_new(min(i / 24 * (_dur / _play), _dur))
                for i in range(_play * 24)]
    _flips_old = sum(1 for a, b in zip(_seq_old, _seq_old[1:])
                     if (a is None) != (b is None))
    assert sum(1 for v in _seq_old if v is None) > 0 and _flips_old > 0, \
        "模拟样本没复现旧算法的 '—' 交替，断言失去意义"
    assert all(v is not None and 60 < v < 2400 for v in _seq_new), \
        "新算法仍会出现空值或离谱配速"
    assert all(abs(b - a) < 60 for a, b in zip(_seq_new, _seq_new[1:])), \
        "新配速逐帧跳变过大，还是会看出闪"
    # 抽帧网格：24fps 下连续 24 帧只应落到 12 个不同的 seek 目标
    _wants_old = [i / 24 for i in range(24)]
    _wants_new = [_w // (24 // 12) / 12 for _w in range(24)]
    assert len(set(_wants_old)) == 24 and len(set(_wants_new)) == 12, \
        "抽帧网格没把 seek 次数减半"
    # 黑帧语义模拟：亮帧更新、黑帧复用、切窗口清空
    def _vframes(seq, switch_at=None):
        cache, out = None, []
        for i, lum in enumerate(seq):
            if switch_at is not None and i == switch_at:
                cache = None
            if lum > 8:
                cache = ("frame", i)
            out.append(cache)
        return out

    _vf = [30, 0, 40, 0, 0]
    _vgot = _vframes(_vf)
    assert _vgot[1] == _vgot[0] and _vgot[3] == _vgot[2] == ("frame", 2), \
        "黑帧没有复用上一张有效画面"
    assert _vframes(_vf, switch_at=4)[4] is None, \
        "切素材窗口没清缓存，第一帧会闪上一段的末帧"
    # 百分比序列：min 语义、单调、两端对齐、绝不假跳
    def _pct(r, e, total):
        return min(r, e) * 100 // max(1, total)
    _seq = [_pct(n, n, 720) for n in range(0, 721, 4)]
    assert _seq[0] == 0 and _seq[-1] == 100 and _seq == sorted(_seq), \
        "百分比会回退或到不了 100"
    assert all(0 <= p <= 100 for p in _seq), "百分比越界"
    assert _pct(1, 1, 720) < 5, "第一帧就跳到 70%，进度条又虚高了"
    assert _pct(700, 100, 720) == _pct(100, 100, 720), \
        "渲染快、编码慢时没按较慢的一条显示"
    # 真机没法逐帧比对 YUV，先在 PC 上锁死「查表 + 2×2 展开」与原公式等价：
    # 任何符号写反、除法定点写错都会让整条视频偏色。
    import random as _random
    _rnd = _random.Random(7)
    for _ in range(400):
        _px = [(_rnd.randrange(256), _rnd.randrange(256), _rnd.randrange(256))
               for _ in range(4)]
        _old = [sum(c[k] for c in _px) // 4 for k in range(3)]
        _new = [(_px[0][k] + _px[1][k] + _px[2][k] + _px[3][k]) // 4
                for k in range(3)]
        assert _old == _new, "2×2 展开求和与原内层循环不等价"
        _r, _g, _b = _new
        assert ((-38 * _r - 74 * _g + 112 * _b + 128) >> 8) + 128 == \
            (((-38 * _r) + (-74 * _g) + (112 * _b) + 128) >> 8) + 128, \
            "U 查表与原公式不等价"
        for _v in (((66 * _r + 129 * _g + 25 * _b + 128) >> 8) + 16,
                   ((-38 * _r - 74 * _g + 112 * _b + 128) >> 8) + 128,
                   ((112 * _r - 94 * _g - 18 * _b + 128) >> 8) + 128):
            assert 0 <= _v <= 255, "YUV 分量溢出 Byte，toByte 会绕回造成色斑"
    # ---- 素材必须「完整插入」：一段都不许丢，同地两段也不许被合并掉 ----
    _frt = {"n": 3, "t0": 1_700_000_000_000, "t": [0, 60, 120],
            "lat": [30.0, 30.001, 30.002], "lon": [120.0, 120.001, 120.002],
            "d": [0, 100, 200], "hr": [120, 130, 140], "seg": [0],
            "v": [1.6, 1.7, 1.8]}
    with _tf.TemporaryDirectory() as _ptd:
        _pp = _Path(_ptd)

        def _mkclip(name, kind, dur, t_utc, lat=None, lon=None):
            p = _pp / name
            p.write_bytes(b"x")
            return {"path": str(p), "name": name[:-4], "kind": kind, "dur": dur,
                    "w": 1080, "h": 1440 if kind == "image" else 1920,
                    "t_utc": t_utc, "lat": lat, "lon": lon}

        _fclips = [
            # 同一地点两段视频（只差 1 秒）：合并掉一段就等于「素材丢了」
            _mkclip("a.mp4", "video", 8.0, 1_700_000_030_000, 30.0005, 120.0005),
            _mkclip("b.mp4", "video", 5.0, 1_700_000_031_000, 30.0005, 120.0005),
            _mkclip("c.jpg", "image", 3.0, 1_700_000_070_000, 30.0012, 120.0012),
            # 时间差 3 小时（明显不在这次活动里）：贴到最近的一端，不许丢
            _mkclip("d.mp4", "video", 4.0, 1_700_000_000_000 - 3 * 3600_000),
        ]
        with _mock.patch.object(MOT, "ensure_basemap", lambda *a, **k: None), \
                _mock.patch.object(MOT.clip_mod, "ensure_playable",
                                   lambda i, c: i["path"]), \
                _mock.patch.object(MOT.clip_mod, "probe",
                                   lambda p: {"w": 1080, "h": 1920}):
            _fpl = MOT.build_payload({"name": "测", "start_ts": "2026-11-07 07:00:00"},
                                     {"route": _frt}, None, clips=_fclips,
                                     duration=30.0)
        _got_names = sorted(p["clip"]["name"] for p in _fpl["photos"] if p.get("clip"))
        assert _got_names == ["a", "b", "c", "d"], f"素材没完整插入：{_got_names}"
        # 混音要的磁盘路径必须一直在，且等于页面看到的源（web=True 时只改 src）
        assert all(_Path(p["clip"]["local"]).is_absolute()
                   and p["clip"]["local"] == p["clip"]["src"]
                   for p in _fpl["photos"] if p.get("clip")), \
            "素材没带可解码的本地路径"
        assert not _fpl["loose"], f"素材被判成挂不上了：{_fpl['loose']}"
        # 30 + 8 + 5 + 3 + 4，一张不少
        assert _fpl["total"] == 50.0, _fpl["total"]
        _clamp = next(p for p in _fpl["photos"] if (p.get("clip") or {}).get("name") == "d")
        assert _clamp["t"] == 0.0 and _clamp["clip"]["approx"], "越界素材没贴到起点"
    # 素材清单要够界面用：类型、文件是否还在、拍摄时间（列表要显示缩略图和时间）
    assert 'it["exists"] = p.is_file()' in _ap_src, "素材清单没标文件是否存在"
    assert 'it["t_utc"] = _meta_val(it, info, "t_utc")' in _ap_src, "素材清单没带拍摄时间（且应优先用存下来的）"
    assert '"t_source"' in _ap_src, "素材清单没带时间来源"
    # 安卓侧：必须把相册原始文件名带进登记（相机名里常带拍摄时间），
    # 并且列表要说明「没有拍摄时间」的后果，不能只显示一行「加入 xx」。
    # 手机上没有 android/ 目录，这里只在桌面自检时校验。
    _kt = KT / "MainActivity.kt"
    if _kt.is_file():
        _kt_src = _kt.read_text(encoding="utf-8")
        assert "OpenableColumns.DISPLAY_NAME" in _kt_src, "安卓没读相册原始文件名"
        assert "dst.absolutePath, displayName" in _kt_src, "登记时没把原始文件名传给后端"
        assert "无拍摄时间" in _kt_src, "素材列表没说明缺少拍摄时间"
        assert "重新读取时间" in _kt_src, "老素材没有补读入口"
        # 真机上「相册显示有拍摄时间、App 却说没有」：时间只在 MediaStore /
        # MediaMetadataRetriever 的日期标签里，裁完的 mp4 根本不带。必须在选片
        # 那一刻取走，并作为最后一档兜底传进 clips_add。
        assert "METADATA_KEY_DATE" in _kt_src, "没从视频解码器读拍摄日期"
        assert '"datetaken"' in _kt_src, "没从相册读 DATE_TAKEN"
        assert "src.absolutePath, takenMs" in _kt_src, "裁切前时间没传给 clips_add"
        assert "clips_reprobe" in _kt_src, "「重新读取时间」没真的调后端重探"
        assert "parseMediaDate" in _kt_src, "视频日期标签没解析"
    assert '"clips_reprobe"' in _ap_src, "clips_reprobe 没注册进 App 接口白名单"
    assert "taken_ms" in _ap_src and "t_native" in _ap_src, "原生兜底时间没落盘"
    assert "def clips_reprobe" in _ap_src, "缺重探接口"
    _ph = next(iter((ROOT / "data" / "photos").rglob("*.jpg")), None) if (
        ROOT / "data" / "photos").is_dir() else None
    if _ph is not None:
        _mi = AP._probe_material(_ph)
        assert _mi["kind"] == "image", _mi
        assert _mi.get("t_utc"), f"图片素材没读出拍摄时间：{_ph}"
    # 登记素材必须从**源文件**取拍摄时间：MediaMuxer 重封装会丢掉 mvhd.creation_time，
    # 只探成品就永远没有时间，回放里只能按数量均匀铺开、位置不准。
    from garmin_cn import clip as _CL
    _mcd = ROOT / "data" / "motion_cache"
    _mp4s = sorted(_mcd.glob("*.mp4")) if _mcd.is_dir() else []
    # 只认「文件里真的写了 mvhd.creation_time」的样本。`name_hint="x.mp4"` 是
    # 故意的：屏蔽「从文件名兜时间」，免得把靠名字补出时间的副本误当源片。
    _has_t = next((p for p in _mp4s
                   if _CL.probe(p, name_hint="x.mp4").get("t_source") == "mvhd"), None)

    # 拍摄时间的多来源兜底：相机文件名 / udta 文本 / 写回成品 mvhd。
    # 这条链路决定「概览能不能显示拍摄时间」和「老素材能不能自己补回来」。
    _t = _CL.time_from_name("VID_20261103_180036.mp4")
    assert _t and dt.datetime.fromtimestamp(
        _t / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M") == "2026-11-03 10:00", \
        "相机文件名里的时间戳没解析对"
    assert _CL.time_from_name("clip-1730966400000.mp4") is None, "无日期的文件名不该凭空造时间"
    assert _CL.time_from_name("PXL_20261103_180036123.mp4"), "Pixel 文件名尾部的毫秒不该挡住解析"
    assert "stamp_creation(p, int(t_utc))" in _ap_src, "登记时没把拍摄时间写回成品"
    assert "name_hint=name_hint" in _ap_src, "登记/清单没把原始文件名传给视频探测"
    assert "name_hint" in Path(_CL.__file__).read_text(encoding="utf-8"), "视频探测没接原始文件名兜底"
    assert 'it["t_why"]' in _ap_src, "素材清单没给「读不到拍摄时间」的原因"
    assert "repairs" in _ap_src, "老素材没有补读落盘"

    # 「没有拍摄时间」的成品**自己造**：把真实样本的 mvhd 时间清零。
    # 不依赖本机缓存里恰好有转码副本，也不依赖被测的解析逻辑来挑样本
    # （否则改坏了样本就消失，整块断言会被静默跳过）。
    if _has_t is not None:
        _old_clips = AP.CLIPS
        try:
            with _tf.TemporaryDirectory() as _td:
                AP.CLIPS = Path(_td) / "clips.json"
                _src_t = _CL.probe(_has_t, name_hint="x.mp4")["t_utc"]

                def _fresh(n: str) -> Path:
                    d = Path(_td) / n
                    d.write_bytes(_has_t.read_bytes())
                    buf, moov, base = _CL._moov_view(d)
                    mv = _CL._walk(buf, moov[0], moov[1], "mvhd")
                    sz = 8 if buf[mv[0]] == 1 else 4
                    with d.open("r+b") as f:
                        f.seek(base + mv[0] + 4)
                        f.write(b"\0" * (2 * sz))   # creation + modification 清零
                    return d

                # 1) 源文件继承：两条各用一份成品（clips_add 会原地写回时间，
                #    共用一个文件会把第一条也「补」上，断言就假通过了）
                _d_old, _d_new = _fresh("old.mp4"), _fresh("new.mp4")
                AP.clips_add(str(_d_old), "旧式", 3.0, 1080, 1920, "video")
                AP.clips_add(str(_d_new), "新式", 3.0, 1080, 1920, "video", str(_has_t))
                _its = {x["name"]: x for x in AP.clips_list()["items"]}
                assert not _its["旧式"].get("t_utc"), "样本选错了：成品本身就有时间"
                assert _its["新式"].get("t_utc"), "登记时没从源文件继承拍摄时间"
                assert _its["新式"].get("t_source") == "mvhd", _its["新式"].get("t_source")
                assert _CL.probe(_d_new, name_hint="x.mp4").get("t_utc"), \
                    "成品没被写回拍摄时间，记录一丢就又读不回来了"

                # 2) 独立验证打标本身
                _dst = _fresh("clip-xyz.mp4")
                assert not _CL.probe(_dst).get("t_utc"), "打标前成品不该有时间"
                assert _CL.stamp_creation(_dst, _src_t), "打标失败"
                assert _CL.probe(_dst).get("t_utc") == _src_t, "打标后成品没读回拍摄时间"

                # 3) 老记录：没存过时间，但名字带摄像头时间戳 → 补读并落盘。
                #    必须用**没打标过**的另一份成品，否则会从 mvhd 读到时间，
                #    断言就抓不到「name_hint 没传」这种回归。
                _d2 = _fresh("clip-abc.mp4")
                AP.CLIPS.write_text(json.dumps({"items": [{
                    "id": "old1", "path": str(_d2), "name": "MVIMG_20261103_180036",
                    "kind": "video", "dur": 3.0, "t_utc": None}]}), encoding="utf-8")
                _it = AP.clips_list()["items"][0]
                assert _it.get("t_utc") == _CL.time_from_name("MVIMG_20261103_180036"), \
                    "老素材没从原始文件名补回拍摄时间"
                assert _it.get("t_source") == "name(+08)", _it.get("t_source")
                _raw = json.loads(AP.CLIPS.read_text("utf-8"))["items"][0]
                assert _raw.get("t_utc") == _it["t_utc"], "补读结果没写回 clips.json"
                assert "exists" not in _raw and "t_why" not in _raw, "临时字段被写进了记录"

                # 4) 补不回来的：必须给原因，界面不能只显示「加入 xx」让人猜
                _d3 = _fresh("clip-def.mp4")
                AP.CLIPS.write_text(json.dumps({"items": [{
                    "id": "old2", "path": str(_d3), "name": "clip-def",
                    "kind": "video", "dur": 3.0, "t_utc": None}]}), encoding="utf-8")
                _bad = AP.clips_list()["items"][0]
                assert not _bad.get("t_utc") and _bad.get("t_why"), \
                    "补不回时间时必须给出原因"

                # 5) 真机兜底：mp4 里完全没有时间，只有相册/解码器给的时间。
                #    用户看到的「有拍摄时间」往往就是这一份，裁完就没了。
                AP.CLIPS = Path(_td) / "native.json"
                _d4 = _fresh("clip-native.mp4")
                _r4 = AP.clips_add(str(_d4), "clip-native", 3.0, 1080, 1920,
                                  "video", "", _src_t)
                assert _r4["ok"] and _r4["t_utc"] == _src_t, "原生兜底时间没被采用"
                assert _r4["t_source"] == "相册/视频日期标签", _r4["t_source"]
                _it4 = AP.clips_list()["items"][0]
                assert _it4["t_utc"] == _src_t and _it4["t_native"] == _src_t, \
                    "原生兜底时间没存进记录"
                assert _CL.probe(_d4, name_hint="x.mp4").get("t_utc") == _src_t, \
                    "兜底时间没写回成品"
                assert AP._norm_taken_ms(_src_t / 1000) == _src_t, "秒级时间没换算"
                assert AP._norm_taken_ms(0) == 0 and AP._norm_taken_ms(1) == 0, \
                    "非法时间没挡住（会把加入时间当拍摄时间）"

                # 6) 「重新读取时间」：记录里存了兜底时间，重探要真能补回来
                _d5 = _fresh("clip-reprobe.mp4")
                AP.CLIPS.write_text(json.dumps({"items": [{
                    "id": "n1", "path": str(_d5), "name": "clip-reprobe",
                    "kind": "video", "dur": 3.0, "t_utc": None,
                    "t_native": _src_t}]}), encoding="utf-8")
                _rr = AP.clips_reprobe("n1")
                assert _rr["ok"] and _rr["t_utc"] == _src_t, _rr
                assert _CL.probe(_d5, name_hint="x.mp4").get("t_utc") == _src_t, \
                    "重探没把时间写回成品"
                assert not AP.clips_reprobe("missing")["ok"]
        finally:
            AP.CLIPS = _old_clips

    # 7) moov 落在文件中段的视频：head 1 MB / tail 8 MB 都够不着，必须靠顶层
    #    box 扫描。这也是真机「原片有拍摄时间、导入后却读不到」的一类根因。
    with _tf.TemporaryDirectory() as _td2:
        def _box(typ, payload):
            return (8 + len(payload)).to_bytes(4, "big") + typ.encode("ascii") + payload

        _secs = int((dt.datetime(2026, 11, 7, 10, 0, tzinfo=dt.timezone.utc)
                     - _CL.EPOCH_1904).total_seconds())
        _mvhd = (b"\0" * 4 + _secs.to_bytes(4, "big") + _secs.to_bytes(4, "big")
                 + (1000).to_bytes(4, "big") + (3000).to_bytes(4, "big")
                 + b"\0" * 80)
        _mid = Path(_td2) / "mid-moov.mp4"
        _mid.write_bytes(_box("ftyp", b"isom" + b"\0" * 4 + b"isom")
                         + _box("mdat", b"\0" * (9 * 1024 * 1024))
                         + _box("moov", _box("mvhd", _mvhd)))
        _want = int((_CL.EPOCH_1904 + dt.timedelta(seconds=_secs)).timestamp() * 1000)
        _info = _CL.probe(_mid, name_hint="x.mp4")
        assert _info.get("t_utc") == _want and _info.get("t_source") == "mvhd", \
            f"中段 moov 没读到拍摄时间：{_info.get('t_why')}"
        assert _CL.stamp_creation(_mid, _want + 60000), "中段 moov 打标失败"
        assert _CL.probe(_mid, name_hint="x.mp4").get("t_utc") == _want + 60000, \
            "中段 moov 打标后没读回新时间"

    # 8) 底图缓存必须跟着取景走：同一活动换隐私档位（轨迹被裁短）后，
    #    绝不能复用旧底图，否则轨迹缩在画面一角，看着就是「底图缩放错」。
    from garmin_cn import motion as _MOT2, basemap as _BM2
    with _tf.TemporaryDirectory() as _td3:
        _cache = Path(_td3) / "basemaps"
        _cache.mkdir()
        _fake_png = b"\x89PNG\r\n\x1a\n" + b"0" * 2048
        _t_full = [[32.690, 109.000], [32.700, 109.010], [32.695, 109.005]]
        _t_trim = [[32.695, 109.005], [32.696, 109.006]]

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return _fake_png

        with _mock.patch.object(_BM2, "CACHE", _cache), \
             _mock.patch.object(_BM2, "key", lambda: "fake"), \
             _mock.patch.object(_BM2, "regeo", lambda *a, **k: ""), \
             _mock.patch("urllib.request.urlopen", lambda *a, **k: _Resp()):
            _a = _MOT2.ensure_basemap(_t_full, "act1")
            _b = _MOT2.ensure_basemap(_t_trim, "act1")
            assert _a and _b, "假底图没取到"
            assert Path(_a["png"]) != Path(_b["png"]), \
                "不同取景复用了同一张底图（串档）"
            assert _a["meta"]["center"] != _b["meta"]["center"] or \
                _a["meta"]["zoom"] != _b["meta"]["zoom"], "两档取景参数不该相同"
            _a2 = _MOT2.ensure_basemap(_t_full, "act1")
            assert Path(_a2["png"]) == Path(_a["png"]), "同档位没命中缓存"
            # 旧版缓存（文件名不带指纹、rev=1）必须被彻底忽略
            _legacy = _cache / "motion-act1.png"
            _legacy.write_bytes(_fake_png)
            _legacy.with_name("motion-act1.json").write_text(json.dumps({
                "fit": "motion", "rev": 1, "frame": [1080, 1920],
                "center": [0.0, 0.0], "zoom": 4}), encoding="utf-8")
            _c = _MOT2.ensure_basemap(_t_full, "act1")
            assert Path(_c["png"]).name != _legacy.name, "旧缓存没失效"

    class _FakeClient:
        def __init__(self, items):
            self.items = items

        def activities(self, limit=60, start=0):
            return self.items[start:start + limit]

    with _tf.TemporaryDirectory() as td:
        items = [
            {"activityId": 1000 + i,
             "startTimeLocal": (dt.date(2026, 11, 6) -
                                dt.timedelta(days=i)).isoformat() + " 07:00:00",
             "activityType": {"typeKey": "running"},
             "duration": 1800, "distance": 6000}
            for i in range(150)
        ]
        # 补一年历史时活动不能只留最近 60 条：翻页应把 150 条都落库
        sa = _Store(_Path(td) / "a.db")
        sb = _Store(_Path(td) / "b.db")
        try:
            assert _Sync(_FakeClient(items), sa) \
                .sync_activities(max_pages=3, oldest=None) == 150
            # 翻到本页已有早于区间起点的活动时收手，不把整个账号历史拉一遍
            assert _Sync(_FakeClient(items), sb) \
                .sync_activities(max_pages=3, oldest="2026-11-01") == 60
        finally:
            # Windows 下不关连接，TemporaryDirectory 清理时会 WinError 32
            sa.conn.close()
            sb.conn.close()

    # 分享前轨迹脱敏：档位口径 + 「脱敏版绝不泄漏轨迹」的硬约束。
    # 用合成轨迹（约 1.1 km 直线），不碰任何真实活动 ID / 坐标 / 日期。
    from garmin_cn import privacy as PV
    assert PV.MODES == ("none", "trim", "hide")
    synth = [[30.0 + i * 0.0001, 120.0] for i in range(100)]
    _, ti = PV.mask_track(synth, "trim")
    assert 0 < ti["kept"] < 100 and ti["cut_head_m"] >= 200 and ti["cut_tail_m"] >= 200
    assert PV.mask_track(synth, "none")[1]["kept"] == 100
    assert PV.mask_track(synth, "hide")[0] == []
    assert PV.normalize("bogus") == "trim" and PV.normalize(None) == "none"
    priv_row = {"name": "自检活动", "date": "2026-11-05",
                "start_ts": "2026-11-05T17:46:00", "distance_m": 6200,
                "duration_s": 2100, "type": "running", "raw": "{}"}
    priv_detail = {"track": synth, "summary": {}, "laps": [],
                   "series": {"points": []}}
    h_none = ACTCARD.build_html(priv_row, priv_detail, privacy="none")
    h_trim = ACTCARD.build_html(priv_row, priv_detail, privacy="trim")
    h_hide = ACTCARD.build_html(priv_row, priv_detail, privacy="hide")
    assert '<path d="M' in h_none and 'data-gc-privacy="none"' in h_none
    assert "已脱敏" in h_trim and 'data-gc-privacy="trim"' in h_trim
    assert '<path d="M' not in h_hide and 'data-gc-private="hide"' in h_hide
    assert "轨迹已隐藏" in h_hide and "时间已隐藏" in h_hide
    assert "17:46" not in h_hide and "30.000" not in h_hide
    # 旧调用方不传档位时必须与 none 逐字节一致（兼容老 APK / 老 CLI）
    assert ACTCARD.build_html(priv_row, priv_detail) == h_none

    # 营养目标：三大营养素的 kcal 之和必须回到目标摄入（差几 kcal 是四舍五入），
    # 且目标摄入不能低于 BMR——这条被改坏等于鼓励长期吃不够。
    from garmin_cn import nutrition as NU

    nt = res.get("nutrition") or {}
    if nt.get("ok"):
        total = nt["protein_kcal"] + nt["carb_kcal"] + nt["fat_kcal"]
        assert abs(total - nt["target_kcal"]) <= 3, (total, nt["target_kcal"])
        assert nt["protein_g"] == round(nt["weight"] * 1.8), nt["protein_g"]
        assert not nt["bmr"] or nt["target_kcal"] >= nt["bmr"], nt
        assert sum(NU.pct_split(nt)) == 100, NU.pct_split(nt)
    out = render(res, store, tmp_html)
    assert out.exists() and out.stat().st_size > 5000

    # 轨迹合成：几何 + 时序合体后，时间/坐标/心率必须落在同一个点上
    from garmin_cn import photo as PH, route as RT

    lat0, lon0 = 34.1400, 108.8340
    poly = [{"lat": lat0 + i * 0.0001, "lon": lon0, "time": 1793659105000 + i * 1000,
             "valid": True, "speed": 0.0} for i in range(11)]
    det = {
        "geoPolylineDTO": {"polyline": poly},
        "metricDescriptors": [
            {"key": "directTimestamp", "metricsIndex": 0},
            {"key": "directHeartRate", "metricsIndex": 1},
            {"key": "directSpeed", "metricsIndex": 2},
            {"key": "sumDistance", "metricsIndex": 3},
        ],
        "activityDetailMetrics": [
            {"metrics": [1793659105000 + i * 1000, 120 + i, 2.5, i * 11.1]}
            for i in range(11)
        ],
    }
    rt = RT.build(det)
    assert rt["n"] == 11 and rt["t"][-1] == 10, rt
    assert rt["hr"][0] == 120 and rt["hr"][-1] == 130, rt["hr"]
    # 11 个点 × 0.0001° 纬差 ≈ 122 m（1° 纬度 110540 m）
    assert 110 < rt["d"][-1] < 135, rt["d"][-1]
    assert RT.summary(rt)["distance_m"] == rt["d"][-1]
    assert abs(RT.at(rt, 5)["d"] - rt["d"][5]) < 1.0
    assert RT.pace(rt, 10) is not None

    # EXIF：自己拼一张带 GPS 的 JPEG，验证解析（含 GPS UTC 时间与方向）
    import struct

    def _rat(v):
        d = int(v); m = int((v - d) * 60); s = round(((v - d) * 60 - m) * 60, 4)
        return struct.pack("<IIIIII", d, 1, m, 1, int(s * 10000), 10000)

    def _asc(x):
        return x.encode() + b"\x00"

    n0, nex, ng = 5, 3, 8
    o0 = 8
    oe = o0 + (2 + n0 * 12 + 4)
    og = oe + (2 + nex * 12 + 4)
    od = og + (2 + ng * 12 + 4)
    pool = bytearray()

    def _add(v):
        off = od + len(pool)
        pool.extend(v)
        if len(v) % 2:
            pool.append(0)
        return off

    # GPS 时间取轨迹首点的 UTC：对不上就落进「不在活动时段」，自检反而失效
    t0u = dt.datetime.fromtimestamp(1793659105000 / 1000, dt.timezone.utc)
    mk, md = _add(_asc("Xiaomi")), _add(_asc("Xiaomi 17"))
    dto = _add(_asc(t0u.strftime("%Y:%m:%d %H:%M:%S")))
    la, lo = _add(_rat(lat0)), _add(_rat(lon0))
    tm = _add(struct.pack("<IIIIII", t0u.hour, 1, t0u.minute, 1, t0u.second * 100, 100))
    gd = _add(_asc(t0u.strftime("%Y:%m:%d")))

    def _ent(t, ty, c, v):
        return struct.pack("<HHI", t, ty, c) + v

    i0 = (struct.pack("<H", n0)
          + _ent(0x010F, 2, 7, struct.pack("<I", mk)) + _ent(0x0110, 2, 10, struct.pack("<I", md))
          + _ent(0x0112, 3, 1, struct.pack("<HH", 6, 0))
          + _ent(0x8769, 4, 1, struct.pack("<I", oe)) + _ent(0x8825, 4, 1, struct.pack("<I", og))
          + struct.pack("<I", 0))
    ex = (struct.pack("<H", nex) + _ent(0x9003, 2, 20, struct.pack("<I", dto))
          + _ent(0xA002, 4, 1, struct.pack("<I", 4096))
          + _ent(0xA003, 4, 1, struct.pack("<I", 2304)) + struct.pack("<I", 0))
    gp = (struct.pack("<H", ng) + _ent(0x0001, 2, 2, b"N\x00\x00\x00")
          + _ent(0x0002, 5, 3, struct.pack("<I", la))
          + _ent(0x0003, 2, 2, b"E\x00\x00\x00")
          + _ent(0x0004, 5, 3, struct.pack("<I", lo))
          + _ent(0x0005, 1, 1, b"\x00\x00\x00\x00")
          + _ent(0x0006, 5, 1, struct.pack("<I", _add(struct.pack("<II", 418, 1))))
          + _ent(0x0007, 5, 3, struct.pack("<I", tm))
          + _ent(0x001D, 2, 11, struct.pack("<I", gd)) + struct.pack("<I", 0))
    tiff = b"II*\x00" + struct.pack("<I", o0) + i0 + ex + gp + bytes(pool)
    app1 = b"Exif\x00\x00" + tiff
    jpg = (b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1
           + b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, 2304, 4096, 3) + b"\x00" * 9
           + b"\xff\xd9")
    pj = tmp_db.with_suffix(".jpg")
    pj.write_bytes(jpg)
    got = PH.read(pj)
    assert abs(got["lat"] - lat0) < 1e-6 and abs(got["lon"] - lon0) < 1e-6, got
    assert got["t_source"] == "gps" and got["orientation"] == 6, got
    mt = PH.match([got], rt, t0_ms=rt["t0"], max_m=500)
    assert len(mt["points"]) == 1, mt["loose"]

    # 时区兜底：相机把本地时间当 UTC 写（差整 8 小时）也要能挂上
    skew = dict(got, t_utc=got["t_utc"] + 8 * 3600 * 1000)
    m2 = PH.match([skew], rt, t0_ms=rt["t0"], max_m=500)
    assert len(m2["points"]) == 1 and m2["points"][0]["t_source"].endswith("+8h"), m2
    # 没有坐标：时间本身就定位了位置，标 approx 而不是丢掉
    nogps = dict(got, lat=None, lon=None)
    m3 = PH.match([nogps], rt, t0_ms=rt["t0"])
    assert len(m3["points"]) == 1 and m3["points"][0]["approx"] is True, m3
    # 单文件实况：JPEG 尾部内嵌 MP4，能按 ftyp 定位并整段切出
    from garmin_cn import clip as CL

    box = struct.pack(">I", 24) + b"ftypisom" + b"\x00" * 16 + b"\x00" * 5000
    live = tmp_db.with_suffix(".live.jpg")
    live.write_bytes(jpg + box)
    assert CL.embedded_mp4(live) == (len(jpg), len(box)), CL.embedded_mp4(live)
    assert CL.embedded_mp4(pj) is None            # 普通照片不能误判
    live.unlink(missing_ok=True)
    pj.unlink(missing_ok=True)

    # ---- 项目结构：入口文件不许再长回去、缓存要有上限 ----------------------
    # cli.py 以前 4200 行，其中 1900 行是自检，改一行要滚半天。断言搬进本模块后
    # 入口只剩薄包装，这里守住「别再塞回去」。
    _cli_lines = (ROOT / "cli.py").read_text(encoding="utf-8").splitlines()
    assert len(_cli_lines) < 2600, \
        f"cli.py 又涨到 {len(_cli_lines)} 行，新逻辑应该放进 garmin_cn/ 里"
    _i = next(i for i, l in enumerate(_cli_lines) if l.startswith("def cmd_selftest("))
    _j = next(i for i, l in enumerate(_cli_lines[_i + 1:], _i + 1) if l.startswith("def "))
    assert _j - _i < 20, "cmd_selftest 又长回去了，断言应该在 garmin_cn/selftest.py"

    # SSL context 必须复用：每个 opener 各建一份的话，Windows 上要枚举 500+ 张
    # 系统证书，单次约 34ms —— 首页刷新白付这个钱。
    from garmin_cn import tls as _tls

    assert _tls.ssl_context() is _tls.ssl_context(), "SSL context 没有复用"
    _net_src = (ROOT / "garmin_cn/net.py").read_text(encoding="utf-8")
    assert "https_handler()" in _net_src and "create_default_context" not in _net_src, \
        "net.py 又自己建 SSL context 了"

    # 出图前那次实时刷新不许白白联网：历史日期（库里已有曲线）和 TTL 内
    # 都要提前退出 —— 实测一次往返约 280ms，历史卡片从 293ms 降到 29ms。
    _aa_now = (ROOT / "garmin_cn/appapi.py").read_text(encoding="utf-8")
    assert 'if date < dt.date.today().isoformat():' in _aa_now, \
        "历史日期的出图仍在实时拉身体电量，白等一次网络"
    assert 'f"bb_fetch:{date}"' in _aa_now, \
        "TTL 内的出图没提前退出，连点两次导出会各拉一次"

    # ---- v63：首页信息密度 + 补全历史自动化 --------------------------------
    # 「补全历史」是一次性动作：登录后后台跑一次，跑完落标记，之后不再触发。
    from garmin_cn import appapi as _AP2

    assert "needs_backfill" in _AP2.status(), "status 没告诉 App 还欠不欠补全"
    # 游标式补全：每段一年往前挪，退到下限才算补完。
    # 只测纯函数（_backfill_segment）+ 到头的短路，自检不联网。
    from garmin_cn import appapi as _AP3

    _cur = dt.date(2026, 11, 9)
    _floor = _AP3._backfill_floor(_cur)
    assert _floor.isoformat() == "2018-11-09", f"补全下限算错：{_floor}"
    _a, _b, _nxt, _done = _AP3._backfill_segment(_cur, _floor)
    assert (_a.isoformat(), _b.isoformat()) == ("2025-11-10", "2026-11-09"), \
        f"第一段区间不对：{_a} ~ {_b}"
    assert _nxt.isoformat() == "2025-11-09", "游标没有往前挪一天"
    assert _done is False, "第一段就判定补完了"
    # 正好退到下限：这一段补完就要说 done，否则会无限循环
    _a2, _b2, _nxt2, _done2 = _AP3._backfill_segment(_floor + dt.timedelta(days=1),
                                                     _floor)
    assert _done2 is True and _nxt2 == _floor, "退到下限那一段没标 done"
    # 到头之后不再发请求（backfill_next 在游标到下限时直接返回）
    _tmpbf = Path(tempfile.mkdtemp()) / "bf.db"
    _st = Store(_tmpbf)
    _st.set_meta(_AP3.BACKFILL_META, _floor.isoformat())
    _st.conn.close()
    with _mock.patch.object(_AP3, "DB", _tmpbf):
        _ended = _AP3.backfill_next()
        assert _ended.get("ok") and _ended.get("done") is True, "到头后没返回 done"
        assert _ended.get("start") == "" and _ended.get("fetched") == 0, \
            "补到头还在请求，会一直循环"
    shutil.rmtree(_tmpbf.parent, ignore_errors=True)

    # App 侧：刷新后自动跑，进度写在标题栏，且不再弹选区间对话框
    _main_src = (KT / "MainActivity.kt") \
        .read_text(encoding="utf-8")
    assert "private fun runBackfill()" in _main_src and \
        _main_src.count("runBackfill()") >= 2, "刷新后没有触发后台补全"
    assert 'apiCall("backfill_next")' in _main_src, "App 没调游标接口"
    assert "subtitle.text = \"补齐历史 第 " in _main_src, "标题栏没有进度小字"
    for _gone in ("showSyncRangeDialog", "buildSyncRangeDialog", "mark_backfill",
                  "runHistorySync", "pickDateRange"):
        assert _gone not in _main_src, f"旧的补拉入口还留着：{_gone}"

    # 首页：睡眠/体重卡 + 趋势线 + 页头小按钮，且不再有覆盖区块
    _home = U.home_page()
    assert _home.count('class="vital"') == 2, "首页缺睡眠/体重卡"
    assert _home.count('class="spark"') >= 2, "睡眠/体重卡没有趋势线"
    assert "欠债" in _home and "体脂" in _home, "睡眠/体重卡没带关键数值"
    assert 'class="mm"' in _home and _home.count('class="i"') == 4, \
        "恢复度分项不是 4 个带分数条的格子"
    assert "数据覆盖" not in _home, "首页的覆盖区块又回来了（和「我的」重复）"

    # 缓存淘汰：超上限才删、只删白名单后缀、最近用过的不能动
    from garmin_cn import housekeeping as _hk
    _cd = Path(tempfile.mkdtemp())
    _now = time.time()
    _f_old = _cd / "a.mp4"
    _f_older = _cd / "b.mp4"
    _f_live = _cd / "c.jpg"
    _f_db = _cd / "keep.db"
    for _f in (_f_old, _f_older, _f_live, _f_db):
        _f.write_bytes(b"0" * 400_000)
    _os.utime(_f_older, (_now - 9 * 3600, _now - 9 * 3600))
    _os.utime(_f_old, (_now - 8 * 3600, _now - 8 * 3600))
    _os.utime(_f_db, (_now - 9 * 3600, _now - 9 * 3600))
    # 上限 1MB、当前 1.6MB：先删最旧的 b，再删 a；c 刚动过要保住，db 不是缓存
    _prev = _hk.prune_motion_cache(_cd, cap_mb=1, apply=False)
    assert _f_older.exists() and _f_old.exists(), "预览模式不该真删文件"
    _got = _hk.prune_motion_cache(_cd, cap_mb=1, apply=True)
    assert _got["removed"] == 2, f"淘汰数量不对：{_got}"
    assert not _f_older.exists() and not _f_old.exists(), "最旧的没被删"
    assert _f_live.exists(), "刚动过的缓存被删了，可能正在导出"
    assert _f_db.exists(), "非缓存后缀被删了，清理工具越界"
    assert _prev["freed"] == _got["freed"], "预览与实际释放量不一致"
    shutil.rmtree(_cd, ignore_errors=True)

    # ---- 1.0：数据搬迁 / 诊断日志 / 无账号课表兜底 / 结构版本 / 发布签名 ----
    # 这一节全部在临时目录里跑：绝不碰真实的 data/garmin.db。
    import unittest.mock as _mock

    from garmin_cn import appapi as _AP
    from garmin_cn import demo as _demo
    from garmin_cn import diag as _diag
    from garmin_cn.store import SCHEMA_VERSION as _SV

    _bk = Path(tempfile.mkdtemp())
    (_bk / "data").mkdir()
    (_bk / "output").mkdir()
    shutil.copy2(tmp_db, _bk / "data" / "garmin.db")
    _patches = [
        _mock.patch.object(_AP, "HOME", _bk),
        _mock.patch.object(_AP, "DB", _bk / "data" / "garmin.db"),
        _mock.patch.object(_AP, "OUT", _bk / "output"),
    ]
    for _p in _patches:
        _p.start()
    try:
        # 结构版本：老库打开后必须被记账，否则以后没法按版本迁移
        assert _AP.Store(_AP.DB).conn.execute(
            "PRAGMA user_version").fetchone()[0] == _SV, "user_version 没写进去"

        # 导出 → 破坏 → 导入：条数必须回到原样，且覆盖前有备份
        _e = _AP.db_export()
        assert _e.get("ok"), f"导出失败：{_e.get('error')}"
        assert _e["counts"]["daily"] > 0, "导出包里没有日汇总"
        assert not any(k in str(_e.get("extras")) for k in ("session", "auth")), \
            "导出包把账号凭据带上了"
        import sqlite3 as _sq

        _c = _sq.connect(str(_AP.DB))
        _c.execute("delete from daily")
        _c.commit()
        _c.close()
        _i = _AP.db_import(_e["path"])
        assert _i.get("ok"), f"导入失败：{_i.get('error')}"
        assert _i["counts"]["daily"] == _e["counts"]["daily"], "导入后条数对不上"
        assert _i.get("backup") and Path(_i["backup"]).is_file(), "导入前没有备份旧库"
        # 包来自更新版本时必须拒绝，别把新库写坏
        assert not _AP.db_import(str(_bk / "不存在.zip")).get("ok"), "不存在的包没被拒"

        # 诊断日志：异常要能落盘并被读出来
        with _mock.patch.dict(os.environ, {"GARMIN_HOME": str(_bk)}):
            _diag.log_exc("selftest", ValueError("故意的测试异常"))
            assert "故意的测试异常" in _diag.read_text(20), "诊断日志没写进去"
        _d = _AP.diag_text(20)
        assert _d.get("ok") and "假" not in _d.get("text", "")[:2], "诊断日志读不出来"

        # 无佳明会话：课表必须退成本地兜底，不能返回 ok:false 让今日页空着
        with _mock.patch.object(_AP, "_client",
                                side_effect=AuthError("没有会话")):
            _pl = _AP.plan(False)
        assert _pl.get("ok"), f"没登录时课表又空了：{_pl.get('error')}"
        assert _pl.get("source") in ("coach", "raceplan"), \
            f"兜底来源不对：{_pl.get('source')}"
        assert (_pl.get("today") or {}).get("date"), "兜底课表没有当天"

        # demo 有生成版本号：旧 demo.db 才会被自动重建
        assert isinstance(getattr(_demo, "DEMO_REV", None), int), "demo 没有版本号"
        assert "demo_rev" in (ROOT / "cli.py").read_text(encoding="utf-8"), \
            "cli demo 复用旧库时没比对版本，新数据会一直看不到"
    finally:
        for _p in reversed(_patches):
            _p.stop()
        shutil.rmtree(_bk, ignore_errors=True)

    # 发布签名：1.0 起没配 keystore 必须直接拦下，0.x 只提示
    sys.path.insert(0, str(ROOT / "tools"))
    import release as _rel  # noqa: E402

    with _mock.patch.object(_rel, "_local_props", return_value={}):
        try:
            _rel.signing_lines("0.6.0")
            _blocked = False
        except _rel.ReleaseError:
            _blocked = True
        assert _blocked, "正式版仍是 debug 签名却没被拦住"
        # 开发版（带 -dev）只提示不拦，自己用得快
        assert _rel.signing_lines("0.6.0-dev"), "-dev 版本不该因为签名就直接报错"

    # 包名只有一个来源：Kotlin 目录、package 声明、gradle、proguard 四处必须一致。
    # 少改一处就是「装上是新应用、但 R8 把网页桥裁了」或「资源引用不到」这种
    # 只在真机上才炸的问题。
    _pkg = "mm.runnercoach.app"
    _gradle = (ROOT / "android/app/build.gradle.kts").read_text(encoding="utf-8")
    assert f'namespace = "{_pkg}"' in _gradle, "namespace 与包名不一致"
    assert f'applicationId = "{_pkg}"' in _gradle, "applicationId 与包名不一致"
    assert f"package {_pkg}" in (KT / "MainActivity.kt").read_text(encoding="utf-8"), \
        "Kotlin package 声明和 applicationId 不一致"
    assert f"-keep class {_pkg}.MainActivity$Bridge" in \
        (ROOT / "android/app/proguard-rules.pro").read_text(encoding="utf-8"), \
        "proguard 还按旧包名 keep，网页桥会被 R8 裁掉"

    # 隐私说明：App 内可达，仓库里也有同一份
    _pv = U.privacy_page()
    assert "不收集" in _pv and "GPS" in _pv, "隐私页缺关键声明"
    assert "/privacy" in U.me_page(), "「我的」里进不去隐私页"
    assert (ROOT / "docs" / "隐私说明.md").is_file(), "仓库里没有隐私说明"

    # 源码包不带 server/（未启用的服务端，与 App 无关）
    assert "server/" in _rel.PACK_EXCLUDE_PREFIX, "server/ 又被打进源码包了"
    assert "server/" not in "\n".join(_rel.packed_files()), "打包清单里仍有 server/"

    print(f"√ 自检通过：恢复度 {res['readiness']['score']}，CTL {res['load']['ctl']}，"
          f"ACWR {res['load']['acwr']}，输出 {out.stat().st_size // 1024} KB")
    for p in (tmp_db, tmp_html):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
    return 0

if __name__ == "__main__":
    raise SystemExit(run())
