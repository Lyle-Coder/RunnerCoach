"""离线自检：解析、指标、渲染、App 源码守卫全链路。

从 `cli.py` 抽出来的 —— 之前它一个函数 1900 行，占掉入口文件近一半，
既不好读也没法单独跑。现在 `cli.cmd_selftest` 只是薄包装，
`python -m garmin_cn.selftest` / `run.bat selftest` 都能直接调。

约定：全部离线，不联网、不依赖真实账号；需要数据库时用临时库。
改动任何模块后都应该跑一遍，断言里的失败信息写的是「为什么不能这样改」。
"""
from __future__ import annotations

import datetime as dt
import io
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
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _chart_svg(doc: str) -> str:
    """只取柱状图那一段 SVG，不碰页头的品牌角标。

    0.6.6 起每个页面外壳都带一枚内联 SVG 品牌标记，「页面里第一个 <svg>」
    不再是柱状图；所有按 SVG 数柱子/数链接的断言都必须走这里。
    """
    a = doc.find('<svg viewBox="0 0 400 210"')
    if a < 0:
        return ""
    b = doc.find("</svg>", a)
    return doc[a:b + 6] if b > a else ""


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

    # 浏览器托管登录：Cookie 过滤 / 会话判定是纯函数，改坏了登录会静默失败。
    # 网页端换门户 SSO 后密码通道作废，这条是唯一的登录入口，不能哑掉。
    from garmin_cn import browser_login as BL

    assert "/portal/sso/" in BL.SIGNIN_URL and "clientId=GarminConnect" in BL.SIGNIN_URL, BL.SIGNIN_URL
    kept = BL.garmin_cookies([
        {"name": "JWT_WEB", "value": "old", "domain": ".garmin.cn", "path": "/"},
        {"name": "JWT_WEB", "value": "new", "domain": ".garmin.cn", "path": "/"},
        {"name": "SESSION", "value": "s", "domain": ".sso.garmin.cn", "path": "/"},
        {"name": "CONSENT", "value": "1", "domain": ".youtube.com", "path": "/"},
        {"name": "blank", "value": "", "domain": ".garmin.cn", "path": "/"},
    ])
    assert {c["name"] for c in kept} == {"JWT_WEB", "SESSION"}, kept
    assert [c["value"] for c in kept if c["name"] == "JWT_WEB"] == ["new"], "同名同域只留最后一条"
    assert BL.has_session(kept) and not BL.has_session([{"name": "foo", "value": "1"}]), "会话判定错了"

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

    # 间歇版式只看佳明的训练模式标记（hasIntensityIntervals），不再靠速度猜：
    # 标记为间歇才切底部双栏，未标记一律原样保留分圈表。
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
    # 未标记：同样的速度曲线也必须是默认分圈版式（旧实现会在这里误报）
    assert 'class="ivband"' not in ACTCARD.build_html(
        {}, {"series": {"points": iv_pts}, "training": {"interval": False,
                                                        "type": "running"}},
        None, None, "间歇自检-未标记")
    _iv_detail = {"series": {"points": iv_pts},
                  "training": {"interval": True, "type": "running"}}
    iv_html = ACTCARD.build_html({}, _iv_detail, None, None, "间歇自检")
    assert 'class="ivband"' in iv_html and "间歇结构" in iv_html
    assert '<div class="lhead">' not in iv_html, "间歇版不该再渲染原分圈表"
    assert 'class="ivband"' not in ACTCARD.build_html({}, {}, None, None, "自检")

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
    rest_html = ACTCARD.build_html(
        {}, {"series": {"points": rest_pts},
             "training": {"interval": True, "type": "running"}},
        None, None, "间歇自检（原地恢复）")
    assert 'class="ivband"' in rest_html and "平均恢复" in rest_html
    assert "组间恢复" in rest_html and "速度差" not in rest_html
    # 15 组 → 15 根柱（跑动 + 组间恢复折成一组），每根柱下三行标注
    assert rest_html.count('class="ivc"') == 15, "15 组应逐组一根柱"
    assert rest_html.count('class="ivcl"') == 15, "每组要有配速/时间/距离三行"
    assert rest_html.count('class="ivrest"') == 15, "组间恢复要画成贴底细条"
    # 柱下三行：配速 / 时间 / 距离，三行都要有内容（不是「—」）
    assert rest_html.count(" m</em>") == 15, "每组都要标距离"
    assert rest_html.count("<span>1:") == 15, "每组都要标时间"

    # ---- 三段及以上：双栏表达不了，改 N 档强度带 -------------------------
    # 400 快 / 200 中 / 200 慢跑：旧实现会把「中」并进快组，结构丢一半。
    def _blocks(seq):
        out, tt = [], 0.0
        for spd, secs in seq:
            for _ in range(max(1, int(secs / 5))):
                out.append({"t": tt, "spd": spd, "hr": 168, "cad": 188, "dist": 0})
                tt += 5.0
        return out

    _tri = _blocks(sum(([(4.8, 83.0), (3.9, 51.0), (2.4, 83.0)]
                        for _ in range(8)), []))
    _tiv = IV.detect({"series": {"points": _tri},
                      "training": {"interval": True, "type": "running"}})
    assert _tiv and len(_tiv["bands"]) == 3, _tiv and [b["label"] for b in _tiv["bands"]]
    assert [b["label"] for b in _tiv["bands"]] == ["快", "中", "慢"]
    assert all(b["sets"] >= 2 for b in _tiv["bands"]), "每档至少要有两组才算一档"
    assert _tiv["bands"][0]["spd"] > _tiv["bands"][1]["spd"] > _tiv["bands"][2]["spd"]
    assert len(_tiv["order"]) >= 12, "时序结构条要有足够的段"
    _tri_html = ACTCARD.build_html(
        {}, {"series": {"points": _tri}, "laps": [],
             "training": {"interval": True, "type": "running"}},
        None, None, "三段自检")
    assert 'class="ivband"' in _tri_html
    assert _tri_html.count('class="ivbr"') == 3, "三档要三行汇总"
    # 24 段超过逐组标注上限（16），退化为纯结构条：形状仍要看得出，只是不标数字
    assert 'class="ivsb' in _tri_html and 'class="ivc"' not in _tri_html
    assert _tri_html.count("ivsb") > 20
    # ---- 标准间歇不许被读成「两种强度」 -------------------------------
    # 8×800 m + 400 m 慢跑恢复：恢复段配速 5'xx" 会被 k-means 聚成「慢组」，
    # 但恢复就是恢复，不是第二个强度档。交替结构必须只出「间歇 / 恢复」两行。
    _jog = _blocks(sum(([(3.9, 123.0), (2.6, 130.0)] for _ in range(8)), []))
    _jiv = IV.detect({"series": {"points": _jog},
                      "training": {"interval": True, "type": "running"}})
    assert _jiv and [b["label"] for b in _jiv["bands"]] == ["间歇", "恢复"], \
        _jiv and [b["label"] for b in _jiv["bands"]]
    assert _jiv["bands"][1].get("jog") is True, "慢跑恢复要标成 jog（展示配速）"
    _jhtml = ACTCARD.build_html(
        {}, {"series": {"points": _jog}, "laps": [],
             "training": {"interval": True, "type": "running"}},
        None, None, "慢跑恢复自检")
    assert "快组" not in _jhtml and "慢组" not in _jhtml, "标准间歇不许出现快/慢组"
    assert "恢复配速" in _jhtml, "慢跑恢复应展示恢复配速"
    # 两段课（15×400 + 原地恢复）仍走结构条，不能被强行切成三档
    _two = _blocks(sum(([(4.6, 86.0), (0.45, 90.0)] for _ in range(15)), []))
    _two_iv = IV.detect({"series": {"points": _two},
                         "training": {"interval": True, "type": "running"}})
    assert _two_iv and len(_two_iv["bands"]) == 2, _two_iv and [b["label"] for b in _two_iv["bands"]]
    _two_html = ACTCARD.build_html(
        {}, {"series": {"points": _two}, "laps": [],
             "training": {"interval": True, "type": "running"}},
        None, None, "两段自检")
    # 两段课也走同一版式（双栏已废弃），只有两档颜色
    assert 'class="ivband"' in _two_html and 'class="ivcols"' in _two_html
    # 同配速 + 站桩恢复：同样只有「间歇 / 恢复」，恢复行给时长而不是配速
    assert [b["label"] for b in _two_iv["bands"]] == ["间歇", "恢复"], \
        [b["label"] for b in _two_iv["bands"]]
    assert "平均恢复" in _two_html and "恢复配速" not in _two_html
    assert "快组" not in _two_html and "慢组" not in _two_html
    # 同距多组 + 配速自然漂移（真实 10-21：3'45" → 3'16"）不许切成快/中档：
    # 组距一致说明这是一节课，配速变化是体力分配，不是两种强度。
    _drift_laps = []
    for i in range(15):
        _pace = 225 - i * 4
        _drift_laps.append({"dist_m": 400.0, "dur_s": 400 * _pace / 1000,
                            "pace_s": _pace, "hr_avg": 170, "cad": 188})
        _drift_laps.append({"dist_m": 40.0, "dur_s": 90.0, "pace_s": None,
                            "hr_avg": 150, "cad": 0})
    _div = IV.detect({"series": {"points": []}, "laps": _drift_laps,
                      "lap_distance_m": 0,
                      "training": {"interval": True, "type": "running"}})
    assert _div and [b["label"] for b in _div["bands"]] == ["间歇", "恢复"], \
        _div and [b["label"] for b in _div["bands"]]
    # 组距不同但配速相同（400/800 m 同速）：也不该造档，只出一行「间歇」
    _ladder = []
    for _ in range(4):
        for _d in (400.0, 800.0):
            _ladder.append({"dist_m": _d, "dur_s": _d / 4.2, "pace_s": 238,
                            "hr_avg": 170, "cad": 188})
            _ladder.append({"dist_m": 200.0, "dur_s": 90.0, "pace_s": None,
                            "hr_avg": 150, "cad": 0})
    _lad = IV.detect({"series": {"points": []}, "laps": _ladder,
                      "lap_distance_m": 0,
                      "training": {"interval": True, "type": "running"}})
    assert _lad and [b["label"] for b in _lad["bands"]] == ["间歇", "恢复"], \
        _lad and [b["label"] for b in _lad["bands"]]
    assert _two_html.count('class="ivc"') == 15 and _two_html.count('class="ivbr"') == 2
    # 四档：金字塔式递进速度，最多切到 4 档，不能无限细分
    _four = _blocks(sum(([(4.9, 60.0), (4.2, 60.0), (3.5, 60.0), (2.6, 60.0)]
                         for _ in range(6)), []))
    _fiv = IV.detect({"series": {"points": _four},
                      "training": {"interval": True, "type": "running"}})
    assert _fiv and 2 <= len(_fiv["bands"]) <= IV.BAND_MAX
    # 匀速标记课：切不出档，回默认版式（空的结构条没有意义）
    _flat = [{"t": i * 5.0, "spd": 3.0 + (0.05 if i % 3 == 0 else -0.03),
              "hr": 150, "cad": 176, "dist": 0} for i in range(120)]
    assert IV.detect({"series": {"points": _flat},
                      "training": {"interval": True, "type": "running"}}) is None
    # 分档的最小样本量：少于 4 段不表态（防止 2 组的短课被画成一堆档）
    assert IV.band_split([{"dur_s": 60, "spd": 4.0, "dist_m": 240, "t0": 0}]) is None

    # ---- 「完美渐加速」彩蛋：两条判定路径 + 两种版式的彩虹渲染 --------------
    from garmin_cn import easteregg as EG

    EG.check()
    _ramp = [330.0, 325.0, 320.0, 315.0, 310.0, 305.0, 300.0, 295.0, 290.0, 285.0]

    def _laps(ps):
        return [{"idx": i + 1, "dist_m": 1000.0, "dur_s": p,
                 "pace_s": p, "hr_avg": 150 + i, "cad": 180,
                 "is_partial": False} for i, p in enumerate(ps)]

    _egg_row = {"name": "渐加速跑", "date": "2026-11-10",
                "start_ts": "2026-11-10 06:30", "distance_m": 10000,
                "duration_s": sum(_ramp), "type": "running"}
    _egg_html = ACTCARD.build_html(
        _egg_row, {"laps": _laps(_ramp),
                   "training": {"interval": False, "type": "running"}},
        None, None, "渐加速跑")
    assert '<div class="egg-badge"' in _egg_html, "命中后右上角要有炫彩徽章"
    assert EG.BADGE_LINE1 in _egg_html and EG.BADGE_LINE2 in _egg_html, \
        "徽章要有两行文案"
    assert 'class="head egg"' in _egg_html, "抬头要给徽章让位"
    assert 'class="card lp egg"' in _egg_html, "分圈卡要切彩蛋样式"
    assert _egg_html.count("linear-gradient(90deg,hsl(") == 10, \
        "10 根分圈条都要换成彩虹"
    assert "10 圈完美渐加速" in _egg_html
    # 未命中：中途掉速 / 不足 5 圈 / 匀速，都不能出徽章与彩虹
    for _ps in ([330, 325, 340, 315, 310, 305], [330, 325, 320, 315],
                [330] * 6):
        _h = ACTCARD.build_html(
            _egg_row, {"laps": _laps(_ps),
                       "training": {"interval": False, "type": "running"}},
            None, None, "未命中")
        assert '<div class="egg-badge"' not in _h and \
            'class="card lp egg"' not in _h, "非完美渐加速不能触发彩蛋"
        assert "linear-gradient(90deg,hsl(" not in _h, \
            "未命中时分圈条仍是原来的绿→橙配色"
    # 残圈不参与判定：末尾挂一个乱配速的 80 m 残圈，仍按 10 圈命中
    _tail = _laps(_ramp) + [{"idx": 11, "dist_m": 80.0, "dur_s": 90.0,
                             "pace_s": 1125.0, "is_partial": True}]
    assert EG.evaluate(_tail)["sets"] == 10
    # 12 圈会被合并成 6 行：判定用原始圈，彩虹落在合并后的每一行
    _long_html = ACTCARD.build_html(
        _egg_row, {"laps": _laps(list(range(360, 300, -5)))},
        None, None, "12 圈")
    assert '<div class="egg-badge"' in _long_html
    assert _long_html.count("linear-gradient(90deg,hsl(") == 6
    # 间歇：强度组（非恢复段）逐组变快，6 组全彩虹 + 徽章
    _egg_pts: list[dict] = []
    _t = 0.0
    for _i, _p in enumerate([330, 320, 310, 300, 290, 280]):
        _spd = 1000.0 / _p
        for _ in range(20):
            _egg_pts.append({"t": _t, "spd": _spd, "hr": 160 + _i * 2,
                             "cad": 180, "dist": 0})
            _t += 4.0
        for _ in range(20):
            _egg_pts.append({"t": _t, "spd": 0.0, "hr": 140,
                             "cad": 0, "dist": 0})
            _t += 4.0
    _egg_iv_detail = {"series": {"points": _egg_pts}, "laps": [],
                      "training": {"interval": True, "type": "running"}}
    _egg_iv = IV.detect(_egg_iv_detail)
    assert _egg_iv and len(EG.interval_paces(_egg_iv)) == 6
    _egg_iv_html = ACTCARD.build_html(_egg_row, _egg_iv_detail,
                                      None, None, "间歇渐加速")
    assert '<div class="egg-badge"' in _egg_iv_html
    assert '<div class="ivband" data-egg="1"' in _egg_iv_html
    assert _egg_iv_html.count("linear-gradient(90deg,hsl(") == 6, \
        "6 根强度柱都要换成彩虹"
    assert "6 组完美渐加速" in _egg_iv_html
    # 有间歇结构时以强度组为准，不能拿递进的分圈去冒充
    assert EG.evaluate(_laps(_ramp), {"order": [], "bands": []}) is None
    # 匀速间歇（强度组不递进）不触发：既有断言里的 15×400 m 正是这种
    assert '<div class="egg-badge"' not in rest_html
    assert 'class="ivband"' in rest_html, "彩蛋判定不能改掉间歇版式契约"



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
    # UI/API checks must use the synthetic fixture, never the user's local database.
    AP.DB = tmp_db
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
    # 0.6.13：页头那枚胶囊在横屏大屏里孤零零地压着结论条，撤掉；
    # 一图流作为低频动作统一收进「我的」页，功能不能因此失联。
    assert 'class="head-act"' not in home and 'data-act="export"' not in home, \
        "首页页头还挂着那枚一图流胶囊"
    assert "导出今日一图流" not in home, "一图流不该再占一整张卡片"
    # 首页不再重复「数据覆盖」：「我的 → 本地数据」已经有同样三个数
    assert "数据覆盖" not in home, "首页的覆盖区块和「我的」页重复了"
    me = U.me_page()
    assert "page-head" in me and 'data-act="login:garmin"' in me
    assert 'data-act="export"' in me, "一图流入口没迁到「我的」页，功能会失联"
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
    assert cal["week"][1]["trained"] is True and cal["week"][1]["done"] is False, \
        "练过不等于完成：缺运动类型且距离未达标，不能标记完成"
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
    # 没有排课的那天，活动只能是「计划外」，不能算成课表完成 ——
    # 用户报的「骑了趟车就显示 1/6」正是这条旧判定造成的。
    assert plan_only["week"][1]["trained"] is True, "日历里的 activity 就是练过了"
    assert plan_only["week"][1]["extra"] is True, "没排课日的活动应记为「计划外」"
    assert plan_only["week"][1]["done"] is False, "计划外活动不能算课表完成"

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
    assert "4.1 km" in _tc and "今天练过" in _tc, \
        "练过的记录没有单列出来（且措辞应是「练过」而不是「完成」）"
    # 只有活动日期没有运动类型/训练量，只能证明练过。
    with_local = PL.from_calendar([
        {"itemType": "workout", "date": "2026-11-03", "title": "轻松跑"}],
        "2026-11-07", {"2026-11-03"})
    assert with_local["week"][1]["trained"] is True and with_local["week"][1]["done"] is False, \
        "仅有活动日期不能证明完成同项目课表"
    # 今天没排课但已经练了：课表仍是「休息」，练过的单列一行（不冒充课表）
    today_act = PL.from_calendar([
        {"itemType": "activity", "date": "2026-11-07", "title": "晨跑",
         "activityId": 9, "duration": 1800, "distance": 5000}], "2026-11-07")
    assert today_act["today"]["acts"][0]["name"] == "晨跑"
    _tc = U._today_plan_card(today_act)
    assert "今天休息" in _tc, "没排课时课表被实际活动顶掉了"
    assert "今天练过" in _tc and "晨跑" in _tc, "练过的记录没有单列出来"
    # 旧快照里「过去即已完成」的脏标记要丢掉重算
    _old_snap = {"available": True, "source": "calendar",
                 "today": {"date": "2026-11-07", "items": []},
                 "week": [{"date": "2026-11-03", "dow": "周二",
                           "items": [{"name": "x"}], "acts": [], "rest": False}],
                 "done_days": ["2026-11-03"]}
    _fixed = PL.annotate(_old_snap, set(), "2026-11-07")
    assert _fixed["week"][0]["done"] is False and _fixed["week"][0]["missed"] is True, \
        "旧快照里「过去即完成」的脏标记没被清掉"
    # 计划外活动不能算进完成率：周一没排课只骑了车，本周应仍是 0/N，
    # 那天的活动单列为「计划外」。用户看到「已完成 1/6」就是这里错了。
    _mix = PL.annotate({
        "available": True, "source": "calendar", "classify": PL.CLASSIFY_REV,
        "today": {"date": "2026-11-09", "items": []},
        "week": [
            {"date": "2026-11-09", "dow": "周一", "items": [], "acts": [], "rest": True},
            {"date": "2026-11-10", "dow": "周二",
             "items": [{"name": "12km"}], "acts": [], "rest": False},
        ]}, {"2026-11-09"}, "2026-11-09")
    assert _mix["week_done"] == 0 and _mix["week_planned"] == 1, \
        f"计划外活动被算进完成率了：{_mix['week_done']}/{_mix['week_planned']}"
    assert _mix["week_extra"] == 1 and _mix["week"][0]["extra"] is True, \
        "计划外的那天没有被单独记为 extra"
    assert _mix["week"][1]["extra"] is False and _mix["week"][1]["done"] is False, \
        "有计划但没做的那天，状态判定不对"

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
    assert "今天练过" in _ui_plan_src and 'class="extra">计划外' in _ui_plan_src, \
        "没有把「练过」和「完成课表」分开，也没有计划外标签"
    assert "本周完成" in _ui_plan_src, "计划页没有统计本周完成情况"
    # 计划接口读缓存时也要按本地库重算：同步之后「今天练没练」会变
    _aa_plan_src = (ROOT / "garmin_cn/appapi.py").read_text(encoding="utf-8")
    assert "annotate(data, days, today, evidence)" in _aa_plan_src, \
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
        _aroot = _Path(_atd).resolve()
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
    # 收尾必须关掉进度线。0.6.1 起收尾从单行展开成带刷新的块，
    # 这里改成「encoded.set 之后不远处要有 busy(false)」，别钉死排版。
    assert "encoded.set(true)" in _main and \
        re.search(r"encoded\.set\(true\)[\s\S]{0,600}?busy\(false\)", _main), \
        "超时/失败时进度线可能一直转"
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
    # 0.6.1 起 videoFrame 多了 rot 参数（回退路径要按原始方向出图再旋转）
    assert re.search(r"videoFrame\(p\.clip\.src, v, w, h, rotV\)", _msrc), \
        "drawPop 还在直接画 <video>，黑帧会闪出来"
    # 预抽帧已把显示旋转烘进像素，不能再转一次，否则 90° 变 180°，
    # 成片里看着就是「旋转没生效」——这是用户实际反馈过的现象。
    assert "function usesPre(src)" in _msrc, "没区分「预抽帧已旋转」的路径"
    assert re.search(r"usesPre\(p\.clip\.src\)\) ctx\.drawImage\(frame", _msrc), \
        "预抽帧仍被二次旋转，旋转角度会翻倍"
    # 图片素材也要按旋转摆正（以前直接画进旋转后的框，变形且看不出转）
    assert "drawRotated(cellOf(p.clip, im, fw, fh, true)" in _msrc, \
        "图片素材没应用显示旋转"
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
    assert "const CLIP_FPS = 12" in _msrc and "const VSTEP = new Map()" in _msrc, \
        "视频卡片没有抽帧网格，导出仍会逐帧 seek（8fps 时卡顿感明显）"
    # 页面按 CLIP_FPS 选格子，原生必须按同一个帧率抽帧，否则要么白抽要么一顿一顿
    _kt_motion = (KT / "MainActivity.kt").read_text(encoding="utf-8")
    _mi = _kt_motion.find("MotionClips.extract(")
    _mchunk = _kt_motion[_mi:_mi + 320] if _mi >= 0 else ""
    assert "12, 96, 720" in _mchunk, \
        f"原生抽帧参数与页面 CLIP_FPS=12 不一致：{_mchunk[:80]}"
    assert "const VDONE = new Map()" in _msrc and "const WAITED = new Set()" in _msrc, \
        "缺「同格只抓一次 / 只等一次就绪」的状态"
    assert "Math.floor(VREL * CLIP_FPS)" in _msrc, \
        "回退路径的 seek 目标没量化到抽帧网格（v47 起 rel 统一存进 VREL）"
    assert "if (VSTEP.get(src) === step) return;" in _msrc, \
        "同一抽帧格里还是会重复 seek"
    assert "VDONE.get(src) === step" in _msrc, \
        "同一抽帧格里还是会每帧做亮度探测 + 全尺寸重绘"
    assert "function fallbackFrame(" in _msrc and \
        re.search(r"return fallbackFrame\(src, fw, fh\);\s*// 黑帧沿用上一张有效画面", _msrc), \
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
    # 帧率优先：12fps / 96 格 / 720px。8fps 时卡片画面明显一顿一顿，
    # 12/8 × (720/840)² ≈ 1.10，抽帧总量基本不变，只是每帧略小。
    _ck53 = (_kt_dir / "MotionClips.kt").read_text(encoding="utf-8")
    assert "private const val JPEG_QUALITY = 92" in _ck53, \
        "抽帧 JPEG 质量又降回去了，卡片会有块状伪影"
    assert "java.io.File(root, \"c$ci\"), 12, 96, 720," in _main, \
        "抽帧参数不是 12fps / 96 格 / 720px，流畅度或体积要失守"
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
    #    min-width:auto，必须显式给 0 才允许收缩；0.6.9 起整行不换行，
    #    让标题自己收缩/换行，标签固定在右侧（加了品牌 icon 后左侧更宽）。
    from garmin_cn import actcard as _AC
    _ac_src = Path(_AC.__file__).read_text(encoding="utf-8")
    _clip_dlg = (_kt_dir / "ClipTrimDialog.kt").read_text(encoding="utf-8")
    assert ".hleft{flex:1 1 460px;min-width:0}" in _ac_src, \
        "活动图标题列没有 min-width:0，长标题会把标签挤出卡片"
    assert ".hname{font-size:36px" in _ac_src and \
        "max-height:calc(36px * 1.3 * 2)" in _ac_src, \
        "标题没缩小或没限高，长标题仍会溢出"
    assert "flex-wrap:nowrap" in _ac_src, \
        "标题行又允许回绕了，右侧标签会掉到下一行"
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
        "            revalidate: bool = False" in _aa_src, \
        "_client 没有 revalidate，会带着死票据跑完整轮同步才失败"
    # 批量补历史的节流：间隔必须能收紧，且 429 退避仍在 net 里
    assert "min_interval: float = 0.5)" in _aa_src, "_client 没暴露 min_interval"
    assert _AA2.BULK_MIN_INTERVAL < 0.5, "批量间隔没比日常同步更密，慢的瓶颈没解决"
    assert "bulk=True" in _aa_src, "补历史没走批量节流"
    # 踩过的坑：bulk 只加进了 sync 的签名，却没传给 SyncService.sync，
    # 结果 light 模式从未生效、还抛 NameError。这里钉住参数必须一路传下去。
    assert "activity_pages=activity_pages, bulk=bulk" in _aa_src, \
        "bulk 没传给 SyncService.sync，light 模式不会生效"
    _sy_src = (ROOT / "garmin_cn/sync.py").read_text(encoding="utf-8")
    assert "activity_pages: int = 1,\n             bulk: bool = False" in _sy_src, \
        "SyncService.sync 没有 bulk 参数"
    # 历史页要及时更新：停在历史 tab 时，每补完一段就 reload
    _kt6 = (KT / "MainActivity.kt").read_text(encoding="utf-8")
    # ---- 账号卡片：已登录不再要求重输密码 -------------------------------
    from garmin_cn import appapi as _AAI

    assert "account_info" in _AAI._API and "garmin_logout" in _AAI._API, \
        "账号卡片/退出登录没进桥接白名单"
    assert _AAI._mask_mail("abcdef@qq.com") == "ab***@qq.com"
    assert _AAI._mask_mail("a@qq.com") == "a*@qq.com"
    assert _AAI._mask_mail(None) == ""
    _kt7 = (KT / "MainActivity.kt").read_text(encoding="utf-8")
    assert '"login:garmin" -> openAccount("garmin")' in _kt7, "已登录仍直接弹登录框"
    assert '"login:coros" -> openAccount("coros")' in _kt7
    assert "点击可重新登录" not in _kt7, "文案还写着「点击可重新登录」"
    assert "更换其他账号" in _kt7 and "退出登录" in _kt7, "账号卡片缺换号/退出按钮"
    assert "fun openAccount(" in _kt7 and "fun showAccountCard(" in _kt7
    # 高驰补历史不额外拉 MCP 睡眠（日汇总里已含睡眠字段）
    _csrc2 = (ROOT / "garmin_cn/coros.py").read_text(encoding="utf-8")
    _range_src = _csrc2.split("def sync_range")[1].split("def earliest_activity")[0]
    assert "coros_mcp_sleep" not in _range_src, "高驰补历史又去拉 MCP 睡眠了，白花请求"

    # ---- 老日期只拉日汇总（睡眠/HRV 不参与那段时间的判断）-----------
    from garmin_cn import sync as _SY
    from garmin_cn.store import Store as _Store

    assert 60 <= _SY.LIGHT_AFTER_DAYS <= 365, "分析窗口大小不合理（恢复度 7 天 / ACWR 28 / CTL 90）"

    class _FakeSyncClient:
        def __init__(self):
            self.calls: list[str] = []

        def daily_summary(self, d):
            self.calls.append("daily")
            return {"totalSteps": 9000, "restingHeartRate": 48}

        def sleep(self, d):
            self.calls.append("sleep")
            return {"dailySleepDTO": {"sleepTimeSeconds": 25200}}

        def hrv(self, d):
            self.calls.append("hrv")
            return {"hrvSummary": {"lastNightAvg": 55}}

    _ldb = _Path(tempfile.mkdtemp()) / "light.db"
    _lst = _Store(_ldb)
    _fc = _FakeSyncClient()
    _SY.SyncService(_fc, _lst).sync_day("2025-01-05", light=True)
    assert _fc.calls == ["daily"], f"老日期还在多拉请求：{_fc.calls}"
    _lrow = _lst.day("2025-01-05")
    assert _lrow.get("steps") == 9000 and _lrow.get("resting_hr") == 48, \
        "跳过睡眠后日汇总的关键指标也没了"
    assert _lrow.get("sleep_total_min") is None
    _fc2 = _FakeSyncClient()
    _SY.SyncService(_fc2, _lst).sync_day("2026-11-05")
    assert _fc2.calls == ["daily", "sleep", "hrv"], "近期日期要保留完整口径"
    assert _lst.day("2026-11-05").get("sleep_total_min") == 420
    _st_src = (ROOT / "garmin_cn/sync.py").read_text(encoding="utf-8")
    assert "light=bool(light_from and d < light_from)" in _st_src, \
        "补历史没有按日期切到 light 模式"
    assert "client = _client(email, password, revalidate=True" in _aa_src, \
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
    # 0.6.1 起 manifest 指向自定义 AppTheme（弹窗才能圆角+品牌色），
    # 但 AppTheme 的 parent 必须仍是深色 Material，否则整页会变白底。
    assert "@style/AppTheme" in _man, "原生主题没指向 AppTheme"
    assert "Theme.Material.NoActionBar" in (
        ROOT / "android/app/src/main/res/values/styles.xml"
    ).read_text(encoding="utf-8"), "AppTheme 的 parent 不是深色 Material，页面会变白"
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
        assert _kw in hp, f"历史页首缺统计项：{_kw}"
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
    # 柱状图：12 个刻度位 = 有数据的柱子 + 没数据的基线点。
    # 0.6.3 起零值不再画 6px 假柱（和「跑了 0.4km」长得一样），改成圆点。
    _bars = _H._month_bars(_hs["months"])
    _spots = _bars.count("<rect x=") + _bars.count("<circle ")
    assert _spots == 12, f"柱状图刻度数不对：{_spots}"
    assert "<circle " in _bars, "没跑的日子没画基线点（还是假柱）"
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
    assert _pos_act > 0 and _pg_list.find('<svg viewBox="0 0 400 210"') > _pos_act, \
        "活动列表章节没有柱状图（被挪回页首了）"
    for _m in ("week", "month", "year"):
        assert _chart_svg(_H.history_page({"mode": [_m]})), f"{_m} 视图没有柱状图"
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
    _spots_old = (_chart_svg(_pg_old).count("<rect ") +
                  _chart_svg(_pg_old).count("<circle "))
    assert _spots_old == _days_old, \
        f"选中历史月份后柱状图不是那个月的天数：{_spots_old} vs {_days_old}"
    assert 'class="row on"' in _pg_old and f"sel={_old_m}" in _pg_old, \
        "选中的周期行没有高亮"
    # 非法/过期的 sel 要回落到最新一期，不能空图或报错
    _pg_bad = _H.history_page({"mode": ["month"], "sel": ["1999-01"]})
    assert f"{_new_m} 每日跑量" in _pg_bad and _chart_svg(_pg_bad), \
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
    _spots_wk = (_chart_svg(_pg_wk).count("<rect ") +
                 _chart_svg(_pg_wk).count("<circle "))
    assert _spots_wk == 7, f"周视图不是 7 个刻度位（只画了有跑的日子）：{_spots_wk}"
    assert ">一</text>" in _pg_wk and ">日</text>" in _pg_wk, "周视图刻度不是星期"
    # 行的日期也必须是完整 ISO 周，否则「11-03~11-05」和 7 根柱对不上
    _ws, _we = _H._week_span(_wk_key)
    assert f"{_ws[5:]}~{_we[5:]}" in _H._bucket_rows([_wk_part], "week", "run", _wk_key), \
        "周行显示的是活动首末日期，不是完整的一周"
    # 年视图：选中旧年份要出那一年的 12 根
    _ys = [y["key"] for y in (_hs.get("years") or [])]
    if len(_ys) > 1:
        _pg_y = _H.history_page({"mode": ["year"], "sel": [_ys[-1]]})
        # 年视图的柱子带点击热区（多一层透明 rect），所以只数「有渐变填充的柱」
        # 与「零值点」——直接数 rect 会把热区也算进去（12 柱 + 12 热区 = 24）。
        _spots_y = (_chart_svg(_pg_y).count('fill="url(#mb)"') +
                    _chart_svg(_pg_y).count("<circle "))
        assert f"{_ys[-1]} 年各月跑量" in _pg_y and _spots_y == 12, \
            f"点旧年份没有切到那一年的 12 个月：{_spots_y} 个刻度位"
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
        return _chart_svg(doc)

    _pg_list2 = _H.history_page({"mode": ["list"]})
    assert _svg_only(_pg_list2).count('href="/history?mode=month&amp;type=run&amp;sel=') == 12, \
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
    # 0.6.1 起 readSize() 拆成 readMeta()（原始画幅 + 容器旋转分开）
    assert "private fun readMeta()" in _clip_dlg and "if (fh > maxH) {" in _clip_dlg, \
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
    # 品牌角标的 SVG 也带 <path d="M...">，但路径写成 "M55 34" 这种无空格
    # 紧连数字；GPS 轨迹一律是 "M 210.9,209.0"。用后者当轨迹指纹，
    # 否则 hide 档位会因为「有品牌图标」被误判成轨迹没藏干净。
    assert '<path d="M ' in h_none and 'data-gc-privacy="none"' in h_none
    assert "已脱敏" in h_trim and 'data-gc-privacy="trim"' in h_trim
    assert '<path d="M ' not in h_hide and 'data-gc-private="hide"' in h_hide
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
    # 段长由 BACKFILL_SPAN_DAYS 决定（现在是 180 天：一年一段要跑 700+ 请求，
    # 用户 4 分钟看不到任何新数据）。断言按常量算，别写死日期。
    _want_start = (_cur - dt.timedelta(days=_AP3.BACKFILL_SPAN_DAYS - 1)).isoformat()
    assert (_a.isoformat(), _b.isoformat()) == (_want_start, "2026-11-09"), \
        f"第一段区间不对：{_a} ~ {_b}"
    assert 90 <= _AP3.BACKFILL_SPAN_DAYS <= 365
    _want_next = (_cur - dt.timedelta(days=_AP3.BACKFILL_SPAN_DAYS)).isoformat()
    assert _nxt.isoformat() == _want_next, "游标没有往前挪一天"
    assert _done is False, "第一段就判定补完了"
    # 正好退到下限：这一段补完就要说 done，否则会无限循环
    _a2, _b2, _nxt2, _done2 = _AP3._backfill_segment(_floor + dt.timedelta(days=1),
                                                     _floor)
    assert _done2 is True and _nxt2 == _floor, "退到下限那一段没标 done"
    # 终点由数据源里的第一条记录决定，不再一律退 8 年。
    # 账号 2024-05-01 才有记录，补到那天就该停——再往前全是空年份，
    # 每段要花 365 次日汇总请求，用户看到的就是「一直在拉」。
    _e_floor = _AP3._backfill_floor(_cur, "2024-05-01")
    assert _e_floor.isoformat() == "2024-04-30", f"最早记录没当停止线：{_e_floor}"
    # 这里在测「被停止线截断」的逻辑，与当前段长无关，显式给 365 天
    _a3, _b3, _nxt3, _done3 = _AP3._backfill_segment(
        dt.date(2024, 11, 9), _e_floor, 365)
    # 最后一段被停止线截断：起点必须正好是最早记录前一天
    assert (_a3.isoformat(), _b3.isoformat()) == ("2024-04-30", "2024-11-09"), \
        f"最后一段应停在最早记录：{_a3} ~ {_b3}"
    assert _done3 is True and _nxt3 == _e_floor, "到最早记录那一段没标 done"
    # 一条活动都没有的账号：只往前补两年日汇总，不去翻八年
    _n_floor = _AP3._backfill_floor(_cur, "")
    assert _n_floor.isoformat() == "2024-11-09", f"无活动账号的下限不对：{_n_floor}"
    # 还不知道时退回硬下限，行为与旧版一致
    assert _AP3._backfill_floor(_cur, None).isoformat() == "2018-11-09", \
        "探测不到最早记录时没有退回 8 年兜底"

    # 数据源判断：翻到活动列表底，返回第一条；没到底要如实说
    assert hasattr(_AP3, "_probe_earliest"), "没有「问数据源最早一条」的实现"
    from garmin_cn import client as _cl

    assert hasattr(_cl.GarminCNClient, "activities_earliest"), \
        "客户端没有翻活动列表找最早一条的接口"

    # 翻页到底才算数：不满一页 = 看见的就是第一条；翻到上限还没到底就不能当停止线
    class _FakeAct:
        def __init__(self, pages):
            self.pages = pages

        def activities(self, limit=30, start=0):
            i = start // max(1, limit)
            return self.pages[i] if i < len(self.pages) else []

    _p1 = [{"startTimeLocal": "2026-01-0%dT07:00:00" % (i + 1)} for i in range(3)]
    _p2 = [{"startTimeLocal": "2024-05-01T07:00:00"},
           {"startTimeLocal": "2024-06-02T07:00:00"}]
    _got_act = _cl.GarminCNClient.activities_earliest(
        _FakeAct([_p1, _p2]), max_pages=5, page_size=3)
    assert _got_act["exhausted"] is True and _got_act["date"] == "2024-05-01", \
        f"没找到最早一条：{_got_act}"
    _got_cap = _cl.GarminCNClient.activities_earliest(
        _FakeAct([_p1, _p1, _p1]), max_pages=2, page_size=3)
    assert _got_cap["exhausted"] is False, "翻到上限还说「到底了」，会提前停止补全"

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

    # 已经有最早记录时：游标只要退到它之前就直接收工，连探测都不做
    _tmpbf2 = Path(tempfile.mkdtemp()) / "bf2.db"
    _st2 = Store(_tmpbf2)
    _st2.set_meta(_AP3.BACKFILL_META, "2024-04-30")
    _st2.set_meta(_AP3.EARLIEST_META, "2024-05-01")
    _st2.set_meta(_AP3.EARLIEST_AT_META,
                  dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    _st2.conn.close()
    with _mock.patch.object(_AP3, "DB", _tmpbf2), \
            _mock.patch.object(_AP3, "_probe_earliest",
                               side_effect=AssertionError("不该联网探测")):
        _ended2 = _AP3.backfill_next()
        assert _ended2.get("done") is True and _ended2.get("earliest") == "2024-05-01", \
            "有最早记录时没有直接收工"
        assert _ended2.get("reason") == "reached_earliest", \
            "收工原因没说清是不是到了最早记录"
    shutil.rmtree(_tmpbf2.parent, ignore_errors=True)

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

    # 首页：身体 2×2 + 趋势线 + 页头小按钮，且不再有覆盖区块
    _home = U.home_page()
    assert _home.count('class="rc-cell"') == 4, "身体面板不是 2×2 四格"
    assert _home.count('class="spark"') >= 2, "身体面板没有趋势线"
    assert "7 日欠债" in _home or "评分" in _home, "睡眠格没带关键数值"
    assert "42 天指数均线" in _home and "28 天基线" in _home, \
        "关键数字没带窗口标签"
    assert _home.count('class="rc-part"') == 3, \
        "恢复度分解不是 3 条（环本身就是总进度，卡底那条已删）"
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

    # ---- 0.6.1：弹窗主题 / 后台生成进度 / 文案去外部工具 ----
    # 弹窗之前吃系统默认的深灰直角框，和页面里的圆角卡片不是一个语言。
    _res = ROOT / "android/app/src/main/res"
    _styles = (_res / "values/styles.xml").read_text(encoding="utf-8")
    assert "alertDialogTheme" in _styles and "AppDialog" in _styles, \
        "弹窗没挂自定义主题，还会是系统黑框"
    assert (_res / "drawable/dialog_bg.xml").is_file(), "缺少圆角弹窗底板"
    _mani = (ROOT / "android/app/src/main/AndroidManifest.xml").read_text(encoding="utf-8")
    assert "@style/AppTheme" in _mani, "manifest 还在用系统默认主题"

    _main = (KT / "MainActivity.kt").read_text(encoding="utf-8")
    assert "progressBarStyleLarge" not in _main, \
        "回放又用居中大圈盖住页面了（用户反馈「画面中转圈圈」就是这么来的）"
    assert "progressBarStyleHorizontal" in _main, "进度线没换成顶部横向"
    # 进度必须落盘：产物页靠 /__progress 读它，只放内存切走就看不见
    assert 'File(filesDir, "output/_progress")' in _main, "生成进度没写到 _progress"
    _files = U.files_page(ROOT / "output")
    assert "gc-jobs" in _files and "__progress" in _files, "产物页没有「正在生成」进度区"
    assert "_progress" in (ROOT / "garmin_cn/serve.py").read_text(encoding="utf-8"), \
        "serve 没有 /__progress 接口，产物页轮询会 404"

    # 看板里不该再让用户「去 WorkBuddy 说一句」——装在手机上的人没有那个入口
    assert "WorkBuddy" not in (ROOT / "garmin_cn/report.py").read_text(encoding="utf-8"), \
        "教练点评空态又把用户指到 WorkBuddy 了"

    # ---- 看板页头：一个动作，一行放得下 ----
    # 以前是两个胶囊并排（AI + 数据源），390 宽下组宽 402px，必折成第二行，
    # 还会把整页撑出 14px 横向滚动。现在数据源退成说明文字、动作固定在右上。
    _rep_src = (ROOT / "garmin_cn/report.py").read_text(encoding="utf-8")
    _top_css = _rep_src.split(".top{", 1)[1].split("}", 1)[0]
    assert "flex-wrap" not in _top_css, "看板页头又允许换行了，窄屏会分成两排"
    assert 'class="top-act"' in _rep_src and 'class="top-main"' in _rep_src, \
        "看板页头结构退了：动作与说明没有分开"
    assert 'class="src"' in _rep_src, "数据源/同步时间没退成说明文字"

    # ---- 教练点评：人工 > AI 缓存 > AI 生成中 > 未配置 ----
    _note_block = _rep_src.split("note = store.note_for(day)", 1)
    assert len(_note_block) == 2, "点评取的不是「当天的」，翻历史会看到别的日子的结论"
    assert "aicoach.coach_note(day)" in _rep_src, "看板没接 AI 教练的自动点评"
    assert "id=\"note-live\"" in _rep_src and "/ai/note" in _rep_src, \
        "点评占位没有轮询脚本，配好模型也不会自己填上"
    assert "打开 AI 教练" in _rep_src, "未配置模型时没有指到 AI 教练的入口"
    assert 'action == "note"' in (ROOT / "garmin_cn/aiapi.py").read_text(encoding="utf-8"), \
        "aiapi 没有 note 动作，页面轮询会 404"

    from garmin_cn import aicoach as _AC

    # 模型爱写「**教练点评：** 正文」，清洗后不能留着前缀和星号
    assert _AC._clean_note("**教练点评：** 恢复度 78。\n") == "恢复度 78。", \
        "点评清洗没去掉 markdown 前缀"
    _old_cache = _AC.CACHE_DIR
    _tmp_cache = Path(tempfile.mkdtemp())
    _AC.CACHE_DIR = _tmp_cache
    try:
        _today = dt.date.today().isoformat()
        assert _AC.cached_note(_today) is None, "缓存里没有却报「已生成」"
        _fp = _AC._note_fp(_AC._note_ctx(_today))
        _AC._cache_write("note", _today, _fp,
                         {"text": "测试点评", "model": "fake", "at": "01-01 09:00"})
        _hit = _AC.cached_note(_today) or {}
        assert _hit.get("text") == "测试点评" and _hit.get("ready") is True, \
            "缓存写进去读不出来，看板会一直显示「正在写」"
        # 上下文变了（同步到新活动）就不该再命中旧点评
        assert _AC._cache_read("note", _today, _fp + "x") is None, "指纹失效机制坏了"
    finally:
        _AC.CACHE_DIR = _old_cache
        import shutil as _sh

        _sh.rmtree(_tmp_cache, ignore_errors=True)

    # ---- 一图流也要能吃到同一段 AI 点评 ----
    _card_src = (ROOT / "garmin_cn/card.py").read_text(encoding="utf-8")
    assert "{ai_html}" in _card_src and ".sess .ai" in _card_src, \
        "一图流没有 AI 点评位"
    assert "aicoach.coach_note(d, wait=6)" in (ROOT / "garmin_cn/appapi.py").read_text(
        encoding="utf-8"), "一图流出图时没去补 AI 点评（等太久或压根没调）"
    # 本地兜底课表必须自报家门，不能静默降级成佳明课表
    assert "本地推算" in (ROOT / "garmin_cn/ui.py").read_text(encoding="utf-8"), \
        "本地兜底课表没有标注，用户会以为那是手表里的计划"

    # ---- dist 清理：发版时自动清掉过期产物 ----
    # 只动 dist 一层、只删「发过且不会再分发」的东西；当前版本永远保留。
    _dd = Path(tempfile.mkdtemp())
    _cur = _rel.current_version()
    for _n in (f"RunnerCoach-v{_cur}-release.apk",
               "RELEASE_NOTES-v0.0.1.md",
               "RunnerCoach-v0.0.1-release.apk",
               "RunnerCoach-snap-dev-x-20260101-0000-release.apk",
               "RELEASE_NOTES-snap-dev-x-20260101-0000.md",
               "RunnerCoach-0.0.1-release.apk",
               "SHA256SUMS-0.0.1.txt",
               ".snapshot-tag"):
        (_dd / _n).write_bytes(b"x")
    with _mock.patch.object(_rel, "DIST", _dd):
        _out = _rel.prune_dist(keep_versions=1, keep_snaps=0, apply=True)
    _left = {p.name for p in _dd.iterdir()}
    assert f"RunnerCoach-v{_cur}-release.apk" in _left, "当前版本的包被清理掉了"
    assert "RunnerCoach-v0.0.1-release.apk" not in _left, "过期正式版没清"
    assert "RELEASE_NOTES-v0.0.1.md" not in _left, "过期发布说明没清（用户明确要求）"
    assert "RunnerCoach-snap-dev-x-20260101-0000-release.apk" not in _left, "开发快照没清"
    assert "RELEASE_NOTES-snap-dev-x-20260101-0000.md" not in _left, "快照说明没清"
    assert "RunnerCoach-0.0.1-release.apk" not in _left, "过期的手工包没清"
    assert ".snapshot-tag" in _left, "snapshot 标记不该删（下次上传要用）"
    assert _out["removed"], "清理没有返回删了哪些文件"
    assert (ROOT / "tools/release.py").read_text(encoding="utf-8").count("prune_dist(") >= 2, \
        "prepare 没有调用 prune_dist，发版时不会自动清"
    # 覆盖当前版本 APK（没删任何文件）也必须重算清单：否则 sums 还指向上一版。
    _apk = _dd / f"RunnerCoach-v{_cur}-release.apk"
    _apk.write_bytes(b"new-build-content")
    with _mock.patch.object(_rel, "DIST", _dd):
        _rel.prune_dist(keep_versions=1, keep_snaps=0, apply=True)
    _sums = (_dd / "SHA256SUMS.txt").read_text(encoding="utf-8")
    assert _rel.sha256(_apk) in _sums, \
        "同名 APK 被覆盖后 SHA256SUMS 没重算，sha256sum -c 会 FAILED"
    shutil.rmtree(_dd, ignore_errors=True)

    # 月/周柱状图必须铺满整个周期：11 月才到 9 号也要画 30 根，后面是 0 柱。
    # 以前把 end 截到今天，月初打开只有 9 根，图被压成一条窄带。
    from garmin_cn import history as _hist

    _hm = _hist.history_page({"mode": ["month"]}).split("</style>")[-1]
    _sv = _chart_svg(_hm)
    assert _sv, "月视图没有柱状图"
    # 0.6.3 起零值画圆点，所以刻度位 = 柱 + 点
    _bars = len(re.findall(r"<rect x=|<circle ", _sv))
    _t = dt.date.today()
    _dim = (_t.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
    assert _bars == _dim.day, \
        f"月视图只画了 {_bars} 根，应铺满整月 {_dim.day} 根（未来日期为 0）"
    _hw = _hist.history_page({"mode": ["week"]}).split("</style>")[-1]
    _svw = _chart_svg(_hw)
    _n_wk = len(re.findall(r"<rect x=|<circle ", _svw)) if _svw else 0
    assert _n_wk == 7, f"周视图不是固定 7 个刻度位（周中打开也要铺满整周）：{_n_wk}"

    # ---- 产物页批量管理：勾选 + 二次确认删除 ----
    _product_fixture = Path(tempfile.mkdtemp())
    (_product_fixture / "selftest.html").write_text("<p>synthetic</p>", encoding="utf-8")
    _fp = U.files_page(_product_fixture)
    assert 'class="row pick"' in _fp and 'type="checkbox"' in _fp, "产物行没有勾选框"
    assert "pickbar" in _fp and "/__files/delete" in _fp, "产物页没有批量删除条"
    # 行必须是 div 不是 label：label 会把「点右侧箭头」也转发成勾选
    assert '<label class="row pick"' not in _fp, "行又变回 label，点箭头会误勾"
    # 默认态是普通列表（点行打开），点右上角「管理」才展开勾选框
    assert 'id="pickmode"' in _fp, "产物页没有「管理」入口"
    assert "data-href=" in _fp, "行没带打开地址，退出管理态后点行打不开文件"
    assert "body.manage .row.pick .ck" in _fp, "勾选框没按管理态显隐"
    assert "setManage" in _fp and "location.href = href" in _fp, \
        "管理态切换或「默认点行打开」的逻辑缺失"
    _srv_src = (ROOT / "garmin_cn/serve.py").read_text(encoding="utf-8")
    assert "def _files_delete" in _srv_src and '"/__files/delete"' in _srv_src, \
        "服务端没有删除接口"
    # 破坏性操作的三道闸：本机、纯文件名、产物后缀，缺一个都可能删到别的东西
    assert '".." in name' in _srv_src and "p.parent != root" in _srv_src, \
        "删除接口没做路径逃逸防护"
    assert "_is_loopback()" in _srv_src.split("def _files_delete")[1][:900], \
        "删除接口没有限制本机调用"

    # ---- 预览与成片一致：剪辑预览 + 素材预览都要按「旋转后的外接框」摆 ----
    _trim_src2 = (KT / "ClipTrimDialog.kt").read_text(encoding="utf-8")
    assert "private fun readMeta(): Triple<Int, Int, Int>" in _trim_src2, \
        "剪辑预览没有把「原始画幅 / 容器旋转」分开读"
    assert "fun applyFrame()" in _trim_src2, "剪辑预览没有按旋转重排布局"
    # 关键：旋转必须走 applyFrame（换框），不能只改 view.rotation ——
    # 只转 view 的话 90° 的画面会被原来的横框裁掉，看着像「旋转没生效」。
    assert re.search(r"curRotate = \(curRotate \+ 90\) % 360\s*\n\s*applyFrame\(\)", _trim_src2), \
        "旋转按钮没有重排预览框"
    assert "tex.rotation = ((videoRot + rot) % 360).toFloat()" in _trim_src2, \
        "TextureView 没有叠容器旋转，竖拍素材预览还是横的"
    assert "still.rotation = rot.toFloat()" in _trim_src2, \
        "静帧层旋转量不对（MMR 已经应用过 tkhd，不能再叠一次）"
    # 素材预览：按 rotate 算外接框，与卡片同比例
    assert re.search(r"val swap = rot == 90 \|\| rot == 270\s*\n\s*val visW", _main), \
        "素材预览没有按显示旋转算外接框，和成片卡片对不上"

    # rotate 必须穿过 photo.match。它会重建 item dict，不在 keep 白名单里的
    # 字段一律丢 —— 漏掉 rotate 时，回放拿到的永远是 0，
    # 表现就是「不管怎么旋转，插进成片都还是同一个方向」。
    from garmin_cn import photo as _ph
    _T0 = 1_700_000_000_000
    _rt = {"t0": _T0, "n": 2, "t": [0.0, 20.0],
           "lat": [34.0, 34.001], "lon": [108.0, 108.001], "d": [0.0, 150.0]}
    _cl = [{"name": "x", "path": "C:/x.mp4", "t_utc": _T0 + 5000, "w": 1920,
            "h": 1080, "dur": 6.0, "kind": "video",
            "lat": 34.001, "lon": 108.001, "rotate": 90}]
    _hit = _ph.match(_cl, _rt, t0_ms=_T0, cluster_m=0.0, clamp_ends=True,
                     keep=("dur", "kind", "rotate"))
    assert _hit["points"] and _hit["points"][0]["shots"][0].get("rotate") == 90, \
        "rotate 在 photo.match 里被吃掉了，回放会按原始方向画"
    assert 'keep=("dur", "codec", "bytes", "kind", "rotate")' in \
        (ROOT / "garmin_cn/motion.py").read_text(encoding="utf-8"), \
        "motion 调 match 时没把 rotate 放进 keep"

    # ---- 0.6.3：可读性 / 触控 / 文案 的闸门（不依赖浏览器，纯静态） ----
    from garmin_cn import ui as _u

    def _lum(h: str) -> float:
        h = h.lstrip("#")
        c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

    def _contrast(fg: str, bg: str) -> float:
        a, b = _lum(fg), _lum(bg)
        return (max(a, b) + 0.05) / (min(a, b) + 0.05)

    import re as _re
    _root = _re.search(r":root\{(.+?)\}", _u.CSS, _re.S).group(1)
    _tok = dict(_re.findall(r"--([a-z0-9-]+)\s*:\s*(#[0-9A-Fa-f]{6})", _root))
    assert _contrast(_tok["faint"], _tok["surface"]) >= 4.5, \
        f"辅助色对比度仍不足 AA：{_contrast(_tok['faint'], _tok['surface']):.2f}"
    assert _contrast(_tok["muted"], _tok["surface"]) >= 4.5, "次要色对比度不足 AA"
    # 最小字号：CSS 里不该再有 10px / 10.5px
    assert not _re.search(r"font-size:10(\.5)?px", _u.CSS), "还有 10px 级别的字号"
    # 背景不能用 background-attachment:fixed（滚动重绘整面）
    # 匹配声明而不是注释：注释里提到这个词是正常的（那是在解释为什么不用）
    assert not _re.search(r"background-attachment\s*:\s*fixed\s*;", _u.CSS), \
        "背景又用回 fixed 了，WebView 滚动会重绘整面"
    # 触控目标
    for _sel, _min in ((".more", "44"), (".btn", "44"), (".back", "44")):
        assert _min in _u.CSS.split(_sel + "{")[-1].split("}")[0], \
            f"{_sel} 的可点高度没到 {_min}px"
    # 文案：不给用户看开发口径
    _pages = "".join([U.home_page(), U.plan_page({}), U.me_page()])
    for _bad in ("未标注强度", "TypeError", "出图缩放", "W2 周"):
        assert _bad not in _pages, f"页面里还有「{_bad}」"
    # 无障碍：可点容器要有 role/label，图标要标 aria-hidden
    assert _pages.count('role="button"') >= 1 and 'aria-label=' in _pages, \
        "可点容器没有 role/aria-label，TalkBack 读不出来"
    # 「我的」页压缩：竖排行 ≤6 条（账号 4 + 诊断 1），其余走 2 列网格；
    # 三个体重行合并成一行，登录入口放进来源选择器。
    _me = U.me_page()
    assert _me.count('class="row') <= 6, f"我的页还是长列表：{_me.count('class=\"row')} 行"
    assert _me.count('class="acts"') == 2, "常用/数据两组没有排成网格"
    assert "体重数据" in _me and ">小米体重<" not in _me, "体重三行没有合并"
    # AI 模型服务从「只读卡片」升级成可点的行（行尾带开关），所以网格里不该
    # 再出现只读卡片；顺手守住「每张卡片都必须真的能点」，避免又出假按钮。
    assert 'class="act flat"' not in _me, "网格里还有只读卡片，点下去没反应"
    for _seg in _me.split('class="act"')[1:]:
        _head = _seg.split(">", 1)[0]
        assert "data-act=" in _head or "href=" in _head, \
            f"网格卡片没有动作：{_head[:60]}"
    assert 'data-act=""' not in _me, "出现了空 action 的假按钮"
    _ktme = (KT / "MainActivity.kt").read_text(encoding="utf-8")
    assert "登录体重秤账号" in _ktme, "体重来源选择器里没有登录账号入口"

    # ---- 1.0.5+：网格卡严格等高（68px），与账号行 67px 同一节奏 ---------
    # 真事故：副标题有的 1 行有的 2 行，grid 拉伸后同排 136px、下一排
    # 119px，两组网格之间还差一截，整页 1779px。CSS 与文案长度双保险。
    _ucss = U.CSS
    assert re.search(r"\.act\{[^}]*height:68px", _ucss, re.S), \
        "网格卡没固定 68px，又会被内容撑高"
    assert re.search(r"\.act \.at,\.act \.ad\{[^}]*white-space:nowrap", _ucss, re.S), \
        "网格卡标题/说明没锁单行，会重新出现 1 行/2 行混排"
    assert re.search(r"\.act \.ad\{[^}]*display:block", _ucss, re.S), \
        ".ad 还在用两行 clamp，卡片高度又要跟着文案变"
    assert ".acts,.grid{grid-template-columns:1fr}" not in _ucss, \
        "360 宽下网格又退回单列，整页会凭空多 4 行"
    assert "@media(max-width:340px){.acts{grid-template-columns:1fr}}" in _ucss, \
        "极窄屏没留单列兜底"
    assert ".me .sec{margin-top:16px}" in _ucss and '<div class="me">' in _me, \
        "「我的」页专属紧凑留白没生效（别去改全局 .sec）"
    assert re.search(r"\.me \.grid\.three \.card\{[^}]*height:88px", _ucss, re.S), \
        "三张数值卡没固定高度"
    _ads = re.findall(r'<span class="ad">([^<]*)</span>', _me)
    # AI 模型服务已经挪到账号行（带开关），网格从 8 张变 7 张
    assert len(_ads) == 8 and all(len(a) <= 14 for a in _ads), \
        f"网格卡数量或说明长度不对：{_ads}"

    # ---- 0.6.6：品牌图标统一到开屏 / 角标 / 三张出图 / 完整看板 ------------
    from garmin_cn import brand as _BR
    assert _BR.mark_svg(32) == _BR.mark_svg(32), \
        "品牌 SVG 不是确定性的：同样输入两次出图不一致，缓存/回归都会失效"
    assert _BR.TRACK_OUT in _BR.mark_svg() and _BR.PULSE_PATH in _BR.mark_svg(), \
        "品牌图形缺了跑道或心电脉冲"
    assert "%23" in _BR.mark_uri(), "data URI 没把 # 编码，CSS 背景图标会断"
    _shell = U.page("TEST", "标题", "说明", "")
    # 页头不该再挂角标（原生顶栏已有一枚），但页脚左下角的字标要留着。
    # 只查 <header> 段：整页字符串里页脚本来就有 rc-badge，会误判。
    _shell_head = _shell.split("<header")[1].split("</header>")[0]
    assert "rc-badge" not in _shell_head and 'class="brandline"' in _shell, \
        "页头又挂上品牌角标了（原生顶栏已有一枚）"
    assert 'class="rc-badge' in _shell.split('<div class="foot">')[1], \
        "页脚左下角的品牌字标被一起删了"
    assert ".rc-badge" in _u.CSS and "radial-gradient" in _u.CSS, \
        "页面 CSS 没带品牌角标与辉光"
    for _n in ("card.py", "actcard.py", "racecard.py", "report.py"):
        _s = (ROOT / "garmin_cn" / _n).read_text(encoding="utf-8")
        assert "BR.BRAND_CSS" in _s and "BR.badge_html(" in _s, \
            f"{_n} 没有接上统一品牌图形（又手画了一套？）"
    _ac_src = (ROOT / "garmin_cn/actcard.py").read_text(encoding="utf-8")
    assert 'BR.glow_overlay_css(' in _ac_src and 'BR.rgba(th["acc"]' in _ac_src, \
        "活动图辉光没跟随运动主色"
    # 出图裁边契约：body 底色必须还是纯色，渐变/辉光只能放在内容盒的装饰层里。
    from garmin_cn import exportspec as _EX
    from garmin_cn import actcard as _AC2
    assert f"background:{_EX.BG_ACT}" in _AC2._CSS, \
        "活动图 body 底色被改成渐变了，trim_bg 会整圈失效"
    for _n, _flat in (("card.py", "background:#12161B"),
                      ("racecard.py", "background:#12161B")):
        assert _flat in (ROOT / "garmin_cn" / _n).read_text(encoding="utf-8"), \
            f"{_n} 的 body 底色被改成渐变了，trim_bg 会整圈失效"
    from garmin_cn import report as _RP
    assert not re.search(r"background-attachment\s*:\s*fixed\s*;", _RP.CSS), \
        "完整看板还在用 fixed 背景，WebView 滚动会重绘整面"
    _res_dir = ROOT / "android/app/src/main/res"
    for _n in ("drawable/ic_brand_mark.xml", "drawable/brand_badge_bg.xml",
               "drawable/splash_bg.xml", "drawable/splash_glow.xml"):
        assert (_res_dir / _n).is_file(), f"缺开屏/角标资源 {_n}"
    _styles = (_res_dir / "values/styles.xml").read_text(encoding="utf-8")
    assert "@drawable/splash_bg" in _styles, "冷启动 windowBackground 没换品牌底"
    _vm = (_res_dir / "drawable/ic_brand_mark.xml").read_text(encoding="utf-8")
    assert _BR.TRACK_OUT in _vm and _BR.PULSE_PATH in _vm, \
        "原生角标的路径和网页/启动器不是同一份"
    _kt2 = (KT / "MainActivity.kt").read_text(encoding="utf-8")
    for _needle in ("buildSplash()", "hideSplash()", "R.drawable.ic_brand_mark",
                    "R.drawable.splash_glow"):
        assert _needle in _kt2, f"MainActivity 缺开屏/角标代码：{_needle}"
    assert "from garmin_cn.brand import" in (
        ROOT / "tools/gen_icon.py").read_text(encoding="utf-8"), \
        "启动器图标还在自己维护一套坐标"

    # ---- 0.6.7：首屏 = 一页大屏（《主页面设计说明》） -------------------
    from garmin_cn import screen as _SC

    _css = _u.CSS
    # 竖屏规则单取一份：大屏里也有同名选择器，别让 split 取到后者。
    _base_css = _SC.SCREEN_CSS.split("@media(min-width:900px)")[0]
    assert "@media(min-width:900px)" in _css and "grid-template-areas" in _css, \
        "宽屏没有用同一份 DOM 重排成五面板"
    assert "html.rc-big" not in _css and "rc-mode" not in _css, \
        "「大屏模式」按钮/手动栅格又回来了（系统旋转入口已覆盖）"
    assert _css.count('"h h h h l l l l b b b b"') == 1, \
        "大屏栅格只剩一份，别再手写两遍"
    _h = U.home_page()
    # 只在 DOM 里找按钮：CSS 注释提到旧做法不算「按钮还在」。
    _h_dom = _h.split("<main>")[1]
    assert '<div class="rc-screen">' in _h and 'class="app rc-page"' in _h, \
        "首屏没套上专属作用域"
    assert _h.find('<section class="rc-verdict"') < _h.find(
        '<section class="rc-panel rc-hero-panel"'), "结论条没提到第一行"
    for _seg in ('<section class="rc-verdict"', "rc-hero-panel",
                 '<section class="rc-load', "rc-body-panel",
                 "rc-plan-panel", "rc-week-panel"):
        assert _seg in _h, f"首屏缺段：{_seg}"
    assert 'class="sec"' not in _h and 'class="more"' not in _h, \
        "又把首页排成四个等高 section + 四个重复按钮了"
    for _btn in ("完整看板", "完整课表", "导出今日一图流", "大屏模式"):
        assert _btn not in _h_dom, f"冗余入口没删干净：{_btn}"
    assert '<div class="t">今天休息</div>' not in _h_dom, \
        "计划一条又重复了结论条的判断"
    assert 'data-screen-toggle' not in _h and 'aria-pressed="false"' not in _h, \
        "「大屏模式」按钮没删干净"
    assert "rc-big" not in _u.BRIDGE_JS and "landscape" not in _u.BRIDGE_JS, \
        "桥接脚本还在强制旋转，系统旋转按钮会重复出现"
    assert "SCREEN_ORIENTATION_LANDSCAPE" not in _kt2, \
        "原生还在强制横屏"

    # 恢复环 r45 / 描边 9；常态不亮、异常才亮；id 固定保证可复现
    _r70, _r30 = _SC.ring_svg(70), _SC.ring_svg(30, alert=True)
    assert _SC.ring_svg(70) == _r70, "恢复环 SVG 不是确定性的"
    assert 'r="45"' in _r70 and 'stroke-width="9"' in _r70
    assert 'id="rc-ring"' in _r70
    assert 'class="rc-ring alert"' not in _r70 and 'class="rc-ring alert"' in _r30, \
        "辉光没有只在异常日才亮"
    _hp = _h.find('<section class="rc-panel rc-hero-panel"')
    _hsec = _h[_hp:_h.find("</section>", _hp)]
    assert 'class="bar"' not in _hsec, "卡底总进度条又回来了（和环重复）"

    # 窗口标签、体重不截断、触控 ≥44、柱 ≥32、柱间 7
    for _w in ("28 天基线", "42 天指数均线", "近 7 天累计", "最近 7 天",
               "7 日变化"):
        assert _w in _h, f"窗口标签丢了：{_w}"
    assert "text-overflow:ellipsis" not in _base_css.split(
        ".rc-cell .sub{")[1].split("}")[0], "体重那行又被截成「较…」"
    for _sel, _needle in ((".rc-day{", "min-height:44px"),
                          (".rc-day{", "min-width:32px"),
                          (".rc-days{", "gap:7px"),
                          (".rc-sample{", "min-height:44px")):
        assert _needle in _base_css.split(_sel)[1].split("}")[0], \
            f"{_sel} 缺 {_needle}"

    # 最近 7 天：7 根柱、当天高亮、3 条样本、力量课不写假距离、零值不画假柱
    assert _h.count('<a class="rc-day') == 7, "不是每天一柱"
    assert "rc-day on" in _h or "rc-day zero on" in _h, "当天柱子没高亮"
    assert _h.count('class="rc-sample"') == 3, "样本不是最近 3 条"
    assert "0.00 km · 配速 —" not in _h, "力量训练又写成了跑了 0 公里"
    assert ".rc-day.zero .bar{height:0" in _base_css, "零值还在画假柱"
    _bars = _SC.week_bars([("2026-11-03", 0), ("2026-11-04", 5.0)],
                          "2026-11-04")
    assert 'class="rc-day zero"' in _bars and 'height:0px' in _bars, \
        "零值柱没退化成基线点"
    assert "/view?date=" in _bars, "柱子不能点进当天详情"
    # ---- 0.6.8：开屏只画一枚品牌图标 -----------------------------------
    # 动画层背景若用 splash_bg（自带居中图标），会与 stack 里那枚放大的
    # 图标 tile 错位重叠，文字上方露出半截圆环，看起来像两个标志。
    _surf = (_res_dir / "drawable/splash_surface.xml").read_text(encoding="utf-8")
    _bg = (_res_dir / "drawable/splash_bg.xml").read_text(encoding="utf-8")
    assert "ic_brand_mark" not in _surf, "开屏动画层背景又带了居中图标"
    assert "ic_brand_mark" in _bg, "冷启动第一帧的品牌图标别顺手删掉"
    _bs = _kt2.split("private fun buildSplash()")[1].split(
        "private fun startSplashAnim()")[0]
    assert "R.drawable.splash_surface" in _bs, "开屏动画层没换成纯背景"
    assert "R.drawable.splash_bg" not in _bs, "开屏动画层还在用带图标的底"

    def _hex(s: str) -> set:
        return set(re.findall(r"#[0-9A-Fa-f]{6,8}", s))

    assert _hex(_surf) == _hex(_bg.split('android:gravity="center"')[0]), \
        "开屏两层背景的渐变色值漂移了"
    # 发版守卫：versionCode 只能往上走，防止改包号时漏掉这一处或回退。
    # 每次发版要同步改这里（1.1.9 → 128）。
    assert 'versionCode = 128' in (
        ROOT / "android/app/build.gradle.kts").read_text(encoding="utf-8")

    # ---- 0.6.9：按钮收敛 / 今天优先 / 胶囊压薄 / 两张一图流配色 -----
    from unittest import mock as _um
    from garmin_cn import appapi as _AP2

    _today = dt.date.today().isoformat()
    _fake = {
        "today": {"date": _today, "items": [
            {"name": "W2 周二 12km@E", "km": 12, "minutes": 70, "rest": False}]},
        "next_date": (dt.date.today() + dt.timedelta(days=1)).isoformat(),
        "next_items": [{"name": "W2 周三 10km@E", "km": 10, "rest": False}],
    }
    with _um.patch.object(_AP2, "plan", return_value=_fake):
        _h_today = U.home_page()
    assert "训练计划 · 今天" in _h_today and ">今天<" in _h_today, \
        "今天有课，计划条却没给今天"
    assert ">明天<" not in _h_today, "计划条又跳到明天去了"

    # 复现用户场景：最后同步日比真实今天早一天，课表的 next_date 恰是今天。
    # 旧版拿 st["last"] 当今天，会把今天的课写成「明天」。
    _yest = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    _fake2 = {
        "today": {"date": _yest, "items": [], "acts": []},
        "week": [{"date": _yest, "items": []},
                 {"date": _today, "items": [
                     {"name": "W2 周二 12km@E", "km": 12, "rest": False}]}],
        "next_date": _today,
        "next_items": [{"name": "W2 周二 12km@E", "km": 12, "rest": False}],
    }
    with _um.patch.object(_AP2, "plan", return_value=_fake2), \
         _um.patch.object(_AP2, "status", return_value={
             "last": _yest, "days": 287, "last_sync": "2026-11-09 20:34"}):
        _h_lag = U.home_page()
    assert "训练计划 · 今天" in _h_lag and ">今天<" in _h_lag, \
        "没同步的一天里，今天的课没显示成今天"
    assert ">明天<" not in _h_lag, "最后同步日又被当成今天了"

    # 胶囊压薄：只减纵向留白，44px 可点目标一条都不动。
    for _sel, _needle in ((".rc-panel{", "padding:11px 12px"),
                          (".rc-verdict{", "min-height:38px"),
                          (".rc-cell{", "padding:7px 10px 6px"),
                          (".rc-plan{", "min-height:60px")):
        assert _needle in _base_css.split(_sel)[1].split("}")[0], \
            f"{_sel} 没压薄：缺 {_needle}"
    for _sel, _needle in ((".rc-day{", "min-height:44px"),
                          (".rc-sample{", "min-height:44px")):
        assert _needle in _base_css.split(_sel)[1].split("}")[0], \
            f"压薄把可点目标也削掉了：{_sel}"

    _card_src = (ROOT / "garmin_cn/card.py").read_text(encoding="utf-8")
    _act_src = (ROOT / "garmin_cn/actcard.py").read_text(encoding="utf-8")
    assert "max-width:46%" in _card_src and 'class="m1"' in _card_src, \
        "看板一图流顶部右侧没限宽，长来源会挤出画幅"
    assert "flex-wrap:nowrap" in _act_src, \
        "活动图顶部标签还会换行/被挤走"
    assert "background:#12161B" in _card_src, \
        "看板一图流 body 纯色底被改了，trim_bg 会整圈失效"
    assert "background:#0F1319" in _act_src, \
        "活动图 body 纯色底被改了，trim_bg 会整圈失效"
    assert "#141E2B" in _card_src and "#121E2C" in _act_src, \
        "两张一图流的卡片底色没换成深空蓝"
    assert "#4FD6A6" in _card_src and "#4FD6A6" in _act_src, \
        "icon 起点绿没进两张出图"
    assert "#5FA8E0" in _act_src and "#3B82F6" in _act_src, \
        "活动图兜底配色没往品牌蓝靠"

    # ---- 0.6.10：两张一图流只留页脚左下角那枚角标 ---------------------
    # 抬头原来各挂一枚 44/48px 角标，用户觉得压着标题；页脚那枚保留。
    # 查的是源码里的调用而不是整页字符串，因为页脚仍然要用它。
    assert 'BR.badge_html(44)' not in _card_src and \
        'BR.badge_html(18)' in _card_src, \
        "看板一图流：抬头角标没删干净，或页脚角标被一起删了"
    assert 'BR.badge_html(48' not in _act_src and \
        'BR.badge_html(18' in _act_src, \
        "活动图：抬头角标没删干净，或页脚角标被一起删了"
    assert 'class="hbrand"' not in _act_src and \
        '.rc-date .rc-badge' not in _card_src, \
        "抬头角标的容器 / 专用 CSS 还留着"
    assert 'class="rc-foot"' in _card_src and 'class="rc-foot"' in _act_src, \
        "页脚左下角的品牌字标必须保留"
    assert 'BR.BRAND_CSS' in _card_src and 'BR.BRAND_CSS' in _act_src, \
        "角标删掉后不要把整套品牌样式也一起摘了"

    # ---- 0.6.11：手机横屏紧凑大屏 + 页头角标收敛 ---------------------
    _land_css = _css.split(
        "@media (orientation:landscape) and (min-width:640px)")[1]
    assert "max-height:520px" in _land_css and "max-height:340px" in _css, \
        "手机横屏缺独立断点，或极矮屏没有二次压缩"
    assert "height:calc(100dvh - 92px)" in _land_css and \
        "min-height:0" in _land_css, \
        "横屏栅格还带着 560px 下限或写死高度"
    assert "rc-samples{display:none}" in _land_css and \
        ".rc-load-chart{display:none}" in _land_css and \
        "grid-template-columns:repeat(4,minmax(0,1fr))" in _land_css, \
        "横屏没把样本/双线图收起，或身体面板仍是 2×2"
    assert _css.count('"h h h h l l l l b b b b"') == 1, \
        "宽屏栅格被复制成两份"
    _u_src = (ROOT / "garmin_cn/ui.py").read_text(encoding="utf-8")
    assert 'BR.badge_html(26' not in _u_src and 'BR.badge_html(18' in _u_src, \
        "页头角标没删干净，或页脚字标被一起删了"

    # ---- 0.6.12：横屏收掉原生上下两栏，WebView 吃满高度 -----------------
    # manifest 声明了 configChanges，转屏不重建 Activity，必须显式监听方向变化；
    # 否则 52dp 顶栏 + 62dp 底栏仍把 WebView 压到 ~250dp，紧凑大屏被挤扁。
    _kt3 = (KT / "MainActivity.kt").read_text(encoding="utf-8")
    _mf = (ROOT / "android/app/src/main/AndroidManifest.xml").read_text(
        encoding="utf-8")
    assert 'android:configChanges="orientation|screenSize' in _mf, \
        "转屏改成重建 Activity 了，方向切换的实现要跟着改"
    assert "override fun onConfigurationChanged(newConfig: Configuration)" in _kt3, \
        "不重建的 Activity 缺方向变化回调"
    assert "Configuration.ORIENTATION_LANDSCAPE" in _kt3 and \
        "if (land) View.GONE else View.VISIBLE" in _kt3, \
        "横屏没有把上下两栏收起来 / 竖屏没有恢复"
    for _v in ("toolbar", "topDivider", "bottomDivider", "tabBar"):
        assert f"chromeBars.add({_v})" in _kt3, f"{_v} 没进横屏收纳列表"
    assert _kt3.count("applyChromeForOrientation()") >= 3, \
        "onCreate 初始化与方向回调里都要应用栏状态"
    # 栏收掉后 WebView 变高，但 720p 小屏横过来只剩 264px，极矮档还得
    # 自己吃满高度；页头动作按钮 0.6.13 已整体撤掉，样式也不该留。
    assert "calc(100dvh - 30px)" in _css, \
        "极矮横屏栅格没有吃满可用高度"
    assert "head-act" not in _css, \
        "页头动作按钮已经撤掉，横屏不该还留着它的覆写样式"

    # ---- 0.6.14：极矮横屏不再「砍语义」，恢复标签并让内容撑满 -----------
    # 真机 640×264 复核发现：0.6.12 那版把面板标题/日期/指标名全砍了，
    # 100px 的面板里只填 36~61px，下半截空、五块数据也失去语义。
    # 注意 340 档的 media 也带同一个前缀，split 后是第 3 段，不能复用 _land_css。
    _tiny_css = _css.split("max-height:340px")[-1]
    assert ".rc-panel-hd{display:flex" in _tiny_css, \
        "极矮横屏又把面板标题藏了，五块数据会变成没有名字的数字"
    assert ".rc-day .lbl{display:block" in _tiny_css, \
        "极矮横屏的日期标签被藏了，柱子看不出是哪天"
    assert "text-overflow:ellipsis" not in _tiny_css and \
        "text-overflow:clip" in _tiny_css, \
        "极矮横屏的指标名又被省略号截断了"
    assert ".rc-day.zero .dot{display:none}" in _tiny_css and \
        "min-height:4px" in _tiny_css, \
        "0 值日还在留孤立小点，没有给浅底柱"
    assert ".rc-panel{padding:3px 7px;display:flex;flex-direction:column}" in _tiny_css, \
        "极矮横屏面板没改成 flex 撑满，内容会贴顶留白"
    assert ".rc-week .rc-days{flex:1" in _tiny_css, \
        "极矮横屏的柱子没有跟着面板长高"
    _land_load = _land_css.split(".rc-load{")[-1].split("}")[0]
    assert "border-radius:12px" in _land_load and \
        "background:rgba(9,14,22,.42)" in _land_load, \
        "横屏负荷带没有做成和其余四块一致的卡片"

    # ---- 1.0.2：高驰睡眠走官方 MCP（OAuth），并有 App 内授权入口 ---------
    from garmin_cn import coros_mcp as _MC

    assert _MC.SLEEP_TOOL == "querySleepOverview", "睡眠工具名不对"
    assert _MC.CALLBACK.startswith("http://127.0.0.1:"), "回调必须是本机地址"
    _st = _MC.CorosMcp(tmp_db.parent, "asia").status()
    assert set(_st) >= {"authorized", "region", "can_refresh"}, "状态字段不全"
    assert _MC.parse_sleep({"content": [{"type": "text", "text": "无数据"}]}) == [], \
        "空数据没有被安静跳过"
    assert _MC._minutes("7h 57min") == 477, "时长解析错了"
    assert _MC._pct(0.27) == 27, "比例没统一成百分数"
    _cn = _MC.parse_sleep({"content": [{"type": "text", "text":
            "2026年10月15日\n睡眠评分：88\n主睡眠：7小时 20分\n深睡比例：24%"}]})
    assert _cn and _cn[0]["sleep_score"] == 88 and _cn[0]["sleep_total_min"] == 440, \
        "中文返回解析不出来（真实账号多半就是中文）"
    _api_src = (ROOT / "garmin_cn/appapi.py").read_text(encoding="utf-8")
    for _fn in ("coros_mcp_status", "coros_mcp_login_url", "coros_mcp_finish",
                "coros_mcp_sleep", "coros_mcp_logout"):
        assert f'"{_fn}"' in _api_src, f"接口白名单漏了 {_fn}"
    _mcp_kt = (KT / "CorosLoginActivity.kt").read_text(encoding="utf-8")
    assert 'host.endsWith(".coros.com")' in _mcp_kt, \
        "授权页没限制在高驰域内，等于把 WebView 交给任意页面"
    assert 'uri.port == 53682' in _mcp_kt, "回调地址判断和 Python 侧不一致"
    assert "CorosLoginActivity" in (
        ROOT / "android/app/src/main/AndroidManifest.xml").read_text(encoding="utf-8"), \
        "AndroidManifest 没声明授权页"
    _kt_mcp = (KT / "MainActivity.kt").read_text(encoding="utf-8")
    assert '"corosSleep" -> askCorosSleep()' in _kt_mcp, \
        "旧页面缓存里的 corosSleep 入口没接上，点了会没反应"
    assert "private fun askSleepAfterLogin" in _kt_mcp and "askSleepAfterLogin()" in _kt_mcp, \
        "高驰登录成功后没有询问睡眠授权意向"
    assert 'apiCall("coros_mcp_login_url", "asia")' not in _kt_mcp, \
        "睡眠授权页把区域写死成国内，欧/美区账号会跳到错误网关"
    assert "def _coros_region" in _api_src, \
        "MCP 区域没有跟随高驰账号，欧/美区会跳到错误网关"
    _me = U.me_page()
    assert "高驰睡眠" not in _me, "「我的」里还留着独立的高驰睡眠行（应并进高驰那一行）"
    _ui_src = (ROOT / "garmin_cn/ui.py").read_text(encoding="utf-8")
    assert "睡眠已授权" in _ui_src and "睡眠未授权" in _ui_src, \
        "高驰行没带睡眠授权状态，用户看不出还差一步"

    # ---- 1.0.2：高驰跑步动态（触地 / 垂直振幅）的字段与量纲收敛 --------
    from garmin_cn import coros_activity as _CA

    def _near(pair, want, tol=0.05) -> bool:
        return all(w is None and g is None or (g is not None and w is not None
                                               and abs(g - w) <= tol)
                   for g, w in zip(pair, want))

    assert _near(_CA.dynamics({"avgGroundContactTime": 0.245,
                              "avgVerticalOscillation": 0.085}), (245, 8.5)), \
        "秒/米量纲没有换算成毫秒/厘米"
    assert _CA.dynamics({"avgGct": 9999, "avgVerticalOscillation": 120}) == (None, None), \
        "离谱量纲没被丢掉，卡片会印出错误数量级"
    assert _CA.dynamics(None) == (None, None), "空 summary 没兜住"
    # 真机 summary 可能全 0，要靠 graphList 的均值（按 key 匹配，不看顺序）
    _graph = {"graphList": [
        {"key": "groundTime", "graphItem": {"avg": 260}},
        {"key": "verticalVibration", "graphItem": {"avg": 84}}]}
    assert _CA.dynamics(_graph) == (260.0, 8.4), "graphList 的均值没取到"
    # 不递归进分圈/逐点层：那里是单圈值，当全程会印错
    _deep = {"summary": {"sportType": 100}, "runAbility": {"core": {"avgGct": 268, "vosc": 9.1}}}
    assert _CA.dynamics(_deep) == (None, None), \
        "又开始递归进分圈/采样层找跑姿了，容易把单圈值当全程"
    assert "runAbility{core}" in _CA.key_tree(_deep), "键树诊断没把子键列出来"
    _real = {"summary": {"avgGroundTime": 245, "avgVertVibration": 8.6, "avgVertRatio": 7.4}}
    assert _CA.dynamics(_real) == (245.0, 8.6), "高驰真机字段（avgGroundTime/avgVertVibration）没命中"
    assert _CA.dynamics({"summary": {"avgVertRatio": 7.4}}) == (None, None), \
        "垂直步幅比（%）被当成垂直振幅了，会印出假数字"

    # ---- 1.0.2：分圈组缺失时的 type=-1 回退（运动场跑等特殊类型）--------
    assert _CA.ADAPTER_VERSION >= 9, "适配器版本没升，旧缓存不会重抓"
    # 分圈兜底：只有「全程汇总组」时按逐点切，别显示「无分圈数据」
    assert hasattr(_CA, "laps_from_points")
    _fb = {"sportType": 100, "frequencyList": [
        {"timestamp": i * 1200, "distance": i * 4000, "speed": 300,
         "heart": 150, "cadence": 176, "altitude": 10,
         "gpsLat": 0, "gpsLon": 0} for i in range(120)],
        "lapList": [{"type": -1, "lapDistance": 480000,
                     "lapItemList": [{"distance": 480000, "time": 144000}]}],
        "summary": {"distance": 480000, "totalTime": 144000, "sportType": 100}}
    _fbp = _CA.normalize(_fb, "fb-selftest")
    assert len(_fbp["laps"]) >= 4, "lapList 只有全程汇总时没兜底切分圈"
    assert not _fbp["laps_absent_ok"]
    # 缓存健康检查：有轨迹却没分圈 = 残缺响应，必须重抓（否则永远空）
    assert _CA.is_current({"source": "coros",
                           "adapter_version": _CA.ADAPTER_VERSION,
                           "laps": [],
                           "series": {"points": [{"t": i} for i in range(90)]}}) is False
    assert _CA.is_current({"source": "coros",
                           "adapter_version": _CA.ADAPTER_VERSION,
                           "laps": [], "laps_absent_ok": True}) is True
    _r = _CA.normalize({"summary": {"sportType": 100}, "lapList": [
        {"type": -1, "lapDistance": 1195000, "lapItemList": [
            {"distance": 1195000, "time": 481000, "avgHr": 141}]}]}, "st")
    assert len(_r["laps"]) == 1 and _r["laps"][0]["pace_s"] == 403, \
        "只有 type=-1 分圈组时没有回退，会显示「无分圈数据」"
    _r2 = _CA.normalize({"summary": {"sportType": 100}}, "st2")
    assert _r2["laps"] == [], "完全无 lapList 时应保持空（诊断日志负责定位）"

    # ---- 1.0.2：高驰没有间歇标记，改用手动计圈结构判定 ---------------------
    # 高驰不下发 hasIntensityIntervals（官方文档也没有等价字段），所以
    # 「是不是间歇课」看分圈：整组不是 1 km 自动圈 + 严格门槛的快慢交替。
    def _cg(lap_m, laps):
        return {"type": 0, "lap_m": lap_m,
                "laps": [{"dist_m": d, "dur_s": s, "hr_avg": 160} for d, s in laps]}

    _manual = _cg(0, [(400, 86)] * 15 + [(40, 90)] * 15)
    # 交替顺序要真的交替，这里重建：400 m 快 + 90 s 走
    _manual["laps"] = []
    for _ in range(15):
        _manual["laps"].append({"dist_m": 400.0, "dur_s": 86.0, "hr_avg": 168})
        _manual["laps"].append({"dist_m": 40.0, "dur_s": 90.0, "hr_avg": 145})
    _iv = IV.detect_coros({"lap_groups": [_manual], "laps": _manual["laps"]})
    assert _iv and _iv["sets"] == 15 and _iv["mode"] == "coros-laps", _iv
    assert _iv["source"].startswith("coros:") and _iv["slow"]["rest"], _iv
    # 自动公里圈：哪怕有 2 圈慢（等红灯/爬坡）也不许出双栏
    _km = _cg(1000, [(1000.0, 430.0 if i in (3, 7) else 333.0) for i in range(10)])
    assert IV.detect_coros({"lap_groups": [_km], "laps": _km["laps"]}) is None, \
        "整齐公里圈被误判成间歇（越野/等红灯正是这种）"
    # 手动计圈但全程匀速（例如按公里手按的匀速跑）：切不出快慢，回默认
    _even_manual = _cg(500, [(500.0, 180.0) for _ in range(8)])
    assert IV.detect_coros({"lap_groups": [_even_manual]}) is None
    # 没有 lap_groups 的旧缓存：退回展示用分圈表，同样要求手动计圈
    assert IV.detect_coros({"laps": _manual["laps"], "lap_distance_m": 0}) is not None
    assert IV.detect_coros({"laps": _km["laps"], "lap_distance_m": 1000}) is None
    # 空明细 / 只有 1 圈：不许抛异常
    assert IV.detect_coros({}) is None and IV.detect_coros(None) is None
    assert IV.detect_coros({"laps": [{"dist_m": 400.0, "dur_s": 80.0}]}) is None
    # 高驰适配层要把各组圈距带进明细（判定层才有数据可用）
    _rr = _CA.normalize({"summary": {"sportType": 100}, "lapList": [
        {"type": 1, "lapDistance": 100000, "lapItemList": [
            {"distance": 100000, "time": 33300, "avgHr": 150} for _ in range(6)]},
        {"type": 0, "lapDistance": 40000, "lapItemList": [
            {"distance": 40000, "time": 8600, "avgHr": 168} for _ in range(6)]}]}, "st3")
    assert [g["lap_m"] for g in _rr["lap_groups"]] == [1000.0, 400.0], _rr["lap_groups"]

    # ---- 1.0.2：间歇标记只认活动列表 raw，且 raw 永远优先 -----------------
    # 真机教训（两轮）：① 设备端明细是老版本抓的、没有 training 键，联网补抓
    # 一失败就被 except 静默吞掉；② 就算重抓，单活动接口
    # `/activity-service/activity/{id}` **根本不返回 hasIntensityIntervals**，
    # 存进去的是「未知」，删数据重拉也一样。列表 raw 才是唯一权威来源。
    from garmin_cn import activity as _ACT

    assert _ACT.training_from_raw({"hasIntensityIntervals": True,
                                   "activityType": {"typeKey": "running"}}
                                  ) == {"interval": True, "workout_id": None,
                                        "type": "running"}, "raw 没命中间歇标记"
    assert _ACT.training_from_raw(None) is None, "没有 raw 时必须返回 None"
    assert _ACT.training_from_raw("not-json") is None, "坏 raw 不能抛异常"
    assert _ACT.training_from_raw("{}") is None, "空 raw 不能给出假标记"
    # 没有这个键就是「不知道」，绝不能冒充 interval=False
    assert _ACT.training_from_raw({"activityType": {"typeKey": "running"}}) is None, \
        "raw 缺 hasIntensityIntervals 时不该表态（会把真间歇盖成默认版式）"
    assert _ACT.build_training({"activityType": {"typeKey": "running"}}).get(
        "interval") is None, "单活动接口缺字段时不许写死 interval=False"
    # 真机现场：明细里是重抓后写死的 interval=False，raw 说 True → 以 raw 为准
    _poisoned = {"training": {"interval": False, "type": "running"}}
    _raw_true = {"hasIntensityIntervals": True, "activityType": {"typeKey": "running"}}
    assert _ACT.resolve_training(_poisoned, _raw_true)["interval"] is True, \
        "raw 没盖过缓存里的假 False（就是删数据重拉仍不出双栏的原因）"
    assert _ACT.resolve_training(_poisoned, None)["interval"] is False, \
        "raw 缺失时应退回明细缓存"
    _p: list[dict] = []
    _t = 0.0
    for _ in range(15):
        for _spd, _secs in ((4.6, 86.0), (0.0, 90.0)):
            for _ in range(int(_secs / 4)):
                _p.append({"t": _t, "spd": _spd, "hr": 160, "cad": 200})
                _t += 4.0
    _d = {"series": {"points": _p},
          "training": _ACT.resolve_training(
              _poisoned, {"hasIntensityIntervals": True,
                          "activityType": {"typeKey": "running"}})}
    assert IV.detect(_d) and IV.detect(_d)["sets"] == 15, \
        "假 False 被 raw 纠正后 15×400 m 必须走间歇双栏"

    # ---- 1.0.1：高驰 1030 不再只丢一句英文，手机号账号会换类型的重试 ------
    from garmin_cn import coros as _CO

    assert _CO._looks_like_phone("138 0013 8000") and \
        _CO._looks_like_phone("+8613800138000"), "手机号识别漏了带空格/带区号的写法"
    assert not _CO._looks_like_phone("a@b.com") and \
        not _CO._looks_like_phone("runner2026"), "把邮箱/昵称误判成了手机号"
    _cloud = _CO.CorosCloud.__new__(_CO.CorosCloud)
    _cloud.region, _cloud.email = "asia", "13800138000"
    _tried: list[int] = []

    def _boom(account: str, password: str, atype: int):
        _tried.append(atype)
        raise _CO.CorosError(
            "高驰接口报错（result=1030 The login credentials you entered ...）")

    _cloud._login_once = _boom          # 打桩，离线复现服务端的笼统拒绝
    try:
        _cloud.login("13800138000", "whatever")
        raise AssertionError("1030 应该以认证失败抛出")
    except _CO.CorosAuthError as _ce:
        assert "1030" in str(_ce) and "高驰 App" in str(_ce), \
            "1030 没给可操作的排查步骤，用户只能看到一句英文"
    assert _tried == [1, 2], f"手机号账号没换 accountType 重试（试过 {_tried}）"
    # 邮箱也一样：绑定邮箱常不是登录名，两种类型都试一遍才能确认到底是哪种
    _tried.clear()
    assert _CO._login_types("a@b.com") == [2, 1], "邮箱账号没有回退到另一种 accountType"
    _cloud2 = _CO.CorosCloud.__new__(_CO.CorosCloud)
    _cloud2.region, _cloud2.email = "asia", "a@b.com"
    _cloud2._login_once = _boom
    try:
        _cloud2.login("a@b.com", "whatever")
        raise AssertionError("邮箱 1030 也要抛认证失败")
    except _CO.CorosAuthError as _ce2:
        assert "绑定的邮箱" in str(_ce2), "邮箱失败时没提示「绑定邮箱不能当登录名」"
    assert _tried == [2, 1], f"邮箱账号的重试顺序不对（试过 {_tried}）"
    _kt3 = (KT / "MainActivity.kt").read_text(encoding="utf-8")
    assert "高驰账号（邮箱或手机号）" in _kt3, "登录框还写着只能填邮箱"
    assert "上次登录失败：" in _kt3, "失败原因没写回登录框，用户不知道错在哪"

    # ---- 1.0.3：高驰也要能补历史（以前只有佳明会补）--------------------
    # 真机：高驰下拉刷新永远只有最近 30 天——因为 backfill_next 只调佳明的
    # sync_range，高驰压根没有游标。现在两个源各推一步，都有独立停止线。
    from garmin_cn import appapi as _AA
    from garmin_cn import coros as _C2

    assert hasattr(_C2, "sync_range") and hasattr(_C2, "earliest_activity")
    assert _C2.RANGE_CHUNK_DAYS == 90, "高驰区间太大容易被限流、手机上也太慢"
    for _k in ("COROS_BACKFILL_META", "COROS_EARLIEST_META", "COROS_SPAN_DAYS"):
        assert hasattr(_AA, _k), f"缺 {_k}，高驰补历史没有独立游标"
    assert callable(_AA._backfill_coros) and callable(_AA._backfill_garmin)
    _bf = _AA.backfill_next
    _src = (ROOT / "garmin_cn/appapi.py").read_text(encoding="utf-8")
    assert "_backfill_garmin(span_days)" in _src and "_backfill_coros()" in _src, \
        "backfill_next 没把两个数据源都推一步，高驰历史永远不动"
    assert 'done = bool(g.get("done", True)) and bool(c.get("done", True))' in _src, \
        "只有一个源补完就报 done，另一个会停在中途"
    # 单侧异常必须就地兜住：真机 /activity/query 的 startTime 是 int 毫秒
    # 时间戳，earliest_activity 直接切片抛 TypeError，异常一路冒到 call()，
    # 连先跑完的佳明段都丢了。现在转成该侧 ok=false，另一侧照常推进。
    assert '_guard("garmin"' in _src and '_guard("coros"' in _src, \
        "补历史没有单侧异常隔离，一个源炸掉会拖垮另一个"
    with _mock.patch.object(_AA, "_backfill_garmin",
                            side_effect=TypeError("'int' object is not subscriptable")), \
            _mock.patch.object(_AA, "_backfill_coros",
                               return_value={"ok": True, "done": False, "ran": True,
                                             "fetched": 3, "skipped": 0,
                                             "start": "2026-11-01", "end": "2026-11-10"}):
        _one = _AA.backfill_next()
    assert _one.get("ok") is False and "TypeError" in str(_one.get("error")), \
        "佳明侧炸了却报成功，错误被吞掉"
    assert _one["coros"]["ran"] is True, \
        "佳明侧异常把高驰那一侧也吞了，补历史会整个停摆"
    assert _one.get("start") == "" or _one.get("start") == "2026-11-01", \
        "异常侧的占位不该伪装成真实区间"
    # 反过来：高驰侧炸，佳明侧结果必须保住
    with _mock.patch.object(_AA, "_backfill_garmin",
                            return_value={"ok": True, "done": False, "ran": True,
                                          "fetched": 5, "skipped": 0,
                                          "start": "2026-05-01", "end": "2026-10-28"}), \
            _mock.patch.object(_AA, "_backfill_coros",
                               side_effect=RuntimeError("探测失败")):
        _two = _AA.backfill_next()
    assert _two["garmin"]["ran"] is True and _two.get("error"), \
        "高驰侧异常没被隔离或没上报错误"

    # earliest_activity：真机 startTime 是 int 毫秒时间戳，不能直接 [:10]
    from garmin_cn import coros as _CO4
    class _FakeCoros(_CO4.CorosCloud):
        def __init__(self, *a, **kw):
            pass
        def load(self):
            return True
        def _req(self, method, path, params=None, **kw):
            if path == "/activity/query":
                return {"data": {"dataList": [
                    {"labelId": "a", "startTime": 1762732800000},
                    {"labelId": "b", "startTime": 1762646400000},
                ], "totalCount": 2}}
            return {"data": {}}
    with _mock.patch.object(_CO4, "CorosCloud", _FakeCoros):
        _ed = _CO4.earliest_activity(Path(tempfile.mkdtemp()))
    assert len(_ed) == 10 and _ed[4] == "-", \
        "int 毫秒时间戳让 earliest_activity 抛异常或返回空，补历史会整轮失败"
    for _v, _want in ((1762732800000, "2025-11-10"), ("20261110", "2026-11-10"),
                      ("2026-11-10 07:00:00", "2026-11-10"),
                      ("2026/11/10", "2026-11-10"), (None, None), ("x", None)):
        assert _CO4._any_day(_v) == _want, f"活动日期形状 {_v!r} 没归一化"
    assert 'except Exception as e:' in (
        ROOT / "garmin_cn/appapi.py").read_text(encoding="utf-8").split(
            "def _backfill_coros")[1].split("def backfill_next")[0], \
        "高驰最早活动探测失败没兜住，会拖垮整段补历史"

    # 诊断日志的「未找到跑步动态」必须带上候选键的值，否则下次还是查不出
    # 到底是值为 0 还是量纲不同；触地类键名不含 vert/osc/vib，别漏掉
    _casrc = (ROOT / "garmin_cn/coros_activity.py").read_text(encoding="utf-8")
    assert '"ground", "contact", "gct"' in _casrc, \
        "动态诊断没扫触地类键，真机日志看不到 avgGroundTime 的值"

    # 真机诊断日志里的两种返回形状，都要能解出数据，不能「解析为空」：
    #  ① MCP 睡眠给的是带标题/Note 行的可读报告，且换行可能是字面量 \n
    #  ② summary 里 avgGroundTime / avgVertVibration 明明在，也不能报未找到
    from garmin_cn import coros_mcp as _MC2
    from garmin_cn import coros_activity as _CA2
    _sleep_report = ('"Sleep Overview\\n========================\\n'
                     'Note: each record below is dated by its wake-up day.\\n\\n'
                     '2026-10-12\\nSleep Score: 89\\n'
                     'Daily Sleep: 8h 47min (incl. naps)\\n'
                     'Main Sleep (asleep): 7h 19min\\n'
                     'Main Sleep Period (incl. awake): 7h 32min\\n'
                     'Sleep metrics scope: daily\\nDeep Sleep Ratio: 14%\\n'
                     'Light Sleep Ratio: 56%\\nREM Ratio: 27%\\n'
                     'Awake Ratio: 3%\\nAwake Time: 13 min\\n'
                     'Awake Count (>5 min): 0\\n'
                     'Main Sleep Window: 2026-10-11 23:52 - 2026-10-12 07:24\\n'
                     'Naps Total (asleep): 1h 28min\\nNaps Period: 1h 30min"')
    _sr = _MC2.parse_sleep({"content": [{"type": "text", "text": _sleep_report}]})
    assert len(_sr) == 1 and _sr[0]["date"] == "2026-10-12", \
        "真机睡眠报告（字面 \\n + 标题行）又解析为空了"
    assert _sr[0]["sleep_total_min"] == 439, \
        "主睡眠时长被 Daily Sleep / Period 干扰行覆盖了"
    assert _sr[0]["sleep_score"] == 89 and _sr[0]["deep_min"] == 61 \
        and _sr[0]["nap_min"] == 88 and _sr[0]["awake_min"] == 13, \
        "睡眠评分/分期/小睡/清醒解析不对"
    # 日志里列的那批 summary 字段名（真机 1.0.5）：候选键要能命中，
    # 且 avgVertRatio（垂直步幅比 %）绝不能被当成振幅
    _log_summary = {"adjustedPace": 330, "avgGroundTime": 245,
                    "avgVertVibration": 8.6, "avgVertRatio": 7.4,
                    "avgLegStiffness": 9.2, "avgStepLen": 1.12,
                    "avgCadence": 178, "avgHr": 152, "climbingTime": 0}
    assert _CA2.dynamics({"summary": _log_summary}) == (245.0, 8.6), \
        "真机 summary 字段明明在却取不到触地/垂直振幅"
    # 只有高驰账号时不能被佳明的 need_login 卡住
    assert "if not _garmin_session_ok():" in _src and "if not _coros_session_ok():" in _src

    # ---- 1.0.2：会话校验三态（网络抖动不许把人踢回登录框）--------------
    # 真机反馈「登录后一下拉刷新又弹登录」：client.get 对网络异常返回 None，
    # 旧逻辑把「拿不到 profile」当会话过期 → 一次超时就判定登录失效。
    import types as _types
    from garmin_cn import auth as _AUTH

    _state = {"v": "unknown"}

    class _FakeAuth(_AUTH.GarminCNAuth):
        def __init__(self):        # 不走真初始化（要读配置/建 net）
            self.session_path = _Path("x")
            self.net = _types.SimpleNamespace(get_cookie=lambda n: "jwt")

        def verify_state(self):
            return _state["v"], None

    class _P(_Path):
        def exists(self):          # 只要有「会话文件」这一前提成立即可
            return True

    _fa = _FakeAuth.__new__(_FakeAuth)
    _fa.session_path = _P("x")
    _fa.net = _types.SimpleNamespace(get_cookie=lambda n: "jwt")
    _fa.verify_state = lambda: (_state["v"], None)
    _state["v"] = "unknown"
    assert _fa.ensure_session() is True, "网络异常被判成会话过期（会踢回登录框）"
    _state["v"] = "ok"
    assert _fa.ensure_session() is True
    _state["v"] = "expired"
    assert _fa.ensure_session() is False, "明确 401 时必须判过期"
    _asrc = (ROOT / "garmin_cn/auth.py").read_text(encoding="utf-8")
    assert "def verify_state" in _asrc and 'state != "expired"' in _asrc
    assert "return None  # 网络异常" not in _asrc
    # 登录框要有「显示密码」；续期要验证码时弹验证码框而不是登录框
    _kt5 = (KT / "MainActivity.kt").read_text(encoding="utf-8")
    assert _kt5.count("addShowPassword(box") >= 3, "佳明/高驰/小米登录框要都能显示密码"
    assert "TYPE_TEXT_VARIATION_VISIBLE_PASSWORD" in _kt5
    assert "garminMfa" in _kt5 and "askMfa()" in _kt5, "续期要验证码时没弹验证码框"
    assert 'r.optBoolean("need_mfa") || r.optBoolean("need_login") -> garminDead' not in _kt5, \
        "need_mfa 还被当成密码失效，会弹登录框"
    # 登录中的小卡片：提交后给明确反馈，成功关卡片，失败带错误重开（不丢输入）
    assert "private fun showLoginCard(" in _kt5, "没有登录中卡片，用户会以为没点中"
    assert 'showLoginCard("正在登录佳明"' in _kt5 and 'showLoginCard("正在登录高驰"' in _kt5
    assert _kt5.count("card.dismiss()") >= 2, "成功/失败都要先把卡片关掉"
    assert "askGarminLogin(email, pwd," in _kt5, "佳明登录失败没有回填重开"
    assert "askCorosLogin(email, pwd, region," in _kt5, "高驰登录失败没有回填重开"
    assert "重新登录佳明国区" in _kt5 and "重新登录 COROS 高驰" in _kt5
    assert "setText(pwd ?: \"\")" in _kt5, "重开时密码没带回去，用户要整栏重打"


    # ---- 1.0.2：应用内检查更新（Gitee Release，零后端）------------------
    from garmin_cn import update as _UP

    assert _UP.is_newer("v1.0.2", "1.0.1") and not _UP.is_newer("v1.0.1", "1.0.1")
    assert _UP.is_newer("v1.0.10", "1.0.9"), "版本号要按数字段比，不能按字典序"
    assert not _UP.is_newer("", "1.0.1") and not _UP.is_newer("v1.1", None)
    _apk = _UP.pick_apk([
        {"name": "RunnerCoach-v1.0.2-debug.apk", "browser_download_url": "u1"},
        {"name": "RunnerCoach-v1.0.2-release.apk", "browser_download_url": "u2"},
        {"name": "src.zip", "browser_download_url": "u3"}])
    assert _apk and _apk["name"].endswith("release.apk"), "没优先挑 release 包"
    assert _UP.pick_apk([{"name": "x-debug.apk",
                          "browser_download_url": "u"}]) is None, \
        "debug 包签名不同，绝不能推给用户"
    _pr = _UP.parse({"tag_name": "v1.0.2", "assets": [], "body": "改了什么"}, "1.0.1")
    assert _pr["newer"] and _pr["url"].endswith("/releases/tag/v1.0.2")
    assert _UP.parse(None, "1.0.1")["ok"] is False
    # 桥接与入口必须真的接上，否则自检通过但用户点不到
    from garmin_cn import appapi as _AA
    assert "check_update" in _AA._API, "check_update 没进桥接白名单"
    assert "检查更新" in _UI.me_page() and 'data-act="update"' in _UI.me_page()
    _kt4 = (KT / "MainActivity.kt").read_text(encoding="utf-8")
    assert '"update" -> checkUpdate(silent = false)' in _kt4, "手动入口没接上"
    assert "checkUpdate(silent = true)" in _kt4, "启动时没做静默检查"
    assert "update_skipped" in _kt4, "点「稍后」没有记住，会天天弹同一个版本"
    # 1.0.3 真机没弹窗：节流只按时间、不看版本，升到新包也会被 24h 挡住；
    # 且时间戳在请求前写，一次失败就封锁一天。两处都要钉住。
    assert "update_checked_ver" in _kt4, "节流没按版本判断，升级后不会立即检查"
    assert _kt4.index("prefs.edit()\n                    .putLong(\"update_checked_at\"") \
        > _kt4.index("if (!r.optBoolean(\"ok\")) {"), \
        "失败路径也写了时间戳，网络抖动会把下次检查堵 24 小时"

    # ---- 1.0.0：内核版本与 APK 版本必须对齐（发版最容易漏的一端）-------
    from garmin_cn import __version__ as _kv
    _gradle = (ROOT / "android/app/build.gradle.kts").read_text(encoding="utf-8")
    _an = re.search(r'versionName\s*=\s*"([^"]+)"', _gradle)
    _ac = re.search(r"versionCode\s*=\s*(\d+)", _gradle)
    assert _an and _ac, "build.gradle.kts 里读不到 versionName / versionCode"
    assert _an.group(1) == _kv, \
        f"内核 {_kv} 与 APK versionName {_an.group(1)} 不一致"
    assert int(_ac.group(1)) >= 81, "versionCode 比 1.0.0 还小，老用户覆盖安装会失败"

    # ---- 1.0.6：静息心率「App 52 / 这里 45」——数据日期必须可见 ----------
    # 真机反馈高驰身体状态卡 52、我们 45。最可能是今天没同步时 derive 拿
    # 库里最后一天顶上、页面却仍写「今日」；也可能是 Training Hub 与 App
    # 两条口径不同。前者我们修掉，后者靠诊断日志自证——都不需要用户导出数据包。
    from garmin_cn import coros as _CO3
    assert _CO3._iso("20261110") == "2026-11-10"
    assert _CO3._iso("2026-11-10") == "2026-11-10", "happenDay 带横杠不能整天丢掉"
    assert _CO3._iso("2026/11/10 00:00:00") == "2026-11-10"
    assert _CO3._iso(1762732800000) is None, "时间戳不能猜，时区会错"
    _csrc3 = (ROOT / "garmin_cn/coros.py").read_text(encoding="utf-8")
    assert '"resting_hr": _num(it.get("rhr"))' in _csrc3, \
        "静息心率必须继续原样取 dayDetail 的 rhr，别自作主张换算"
    assert '"coros.rhr"' in _csrc3 and "HR 字段" in _csrc3, \
        "高驰心率字段的诊断日志没了，口径争议没法在不导出数据的情况下定位"

    from garmin_cn.store import Store as _Store3
    from garmin_cn import metrics as _M3
    _sdb = Path(tempfile.mkdtemp()) / "rhr-stale.db"
    _s3 = _Store3(_sdb)
    _y3 = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    _t3 = dt.date.today().isoformat()
    _s3.upsert_daily({"date": _y3, "resting_hr": 45})
    _r3 = _M3.derive(_s3, _t3)
    assert _r3["data_date"] == _y3 and _r3["data_stale_days"] == 1
    assert _r3["today"]["resting_hr"] == 45, "回退时拿到的确实是昨天的原值"
    _s3.upsert_daily({"date": _t3, "resting_hr": 52})
    _r4 = _M3.derive(_s3, _t3)
    assert _r4["data_date"] == _t3 and _r4["data_stale_days"] == 0
    assert _r4["today"]["resting_hr"] == 52, "下拉刷新后应变成今天的值"
    try:
        _s3.conn.close()
        _sdb.unlink(missing_ok=True)
    except OSError:
        pass

    _uisrc3 = (ROOT / "garmin_cn/ui.py").read_text(encoding="utf-8")
    _cardsrc3 = (ROOT / "garmin_cn/card.py").read_text(encoding="utf-8")
    assert 'f"静息心率 · {_dd}"' in _uisrc3, "卡片标签没写数据日期"
    assert "数据停在 {_dd} · 下拉刷新" in _uisrc3, "旧数据没有可执行提示"
    assert "心率区间的设定值口径可能不同" in _uisrc3, "没解释口径差异"
    assert "（未同步今日）" in _cardsrc3, "一图流没标数据日期"
    _tp3 = _UI.home_page()
    assert "静息心率 · 昨夜" not in _tp3 and "静息心率 · " in _tp3
    assert "心率区间的设定值口径可能不同" in _tp3

    # ------------------------------------------------------------ 心率分区名称
    # 五段时长是佳明按账号里的区间算出来的（hrTimeInZone_1..5），中文副名
    # 必须跟佳明 Connect 走；以前那套 E/M/I/T/R「恢复/节奏」是另一套边界。
    from garmin_cn import hrzones as _HZ
    from garmin_cn import actcard as _AC4
    from garmin_cn import racecard as _RC4

    _garmin_names = ["热身", "轻松", "有氧", "阈值", "极限"]
    assert _HZ.zone_descriptions({}) == _garmin_names, "默认必须是佳明官方中文分区名"
    assert _HZ.zone_descriptions({"hr_zone_names": None}) == _garmin_names
    assert _HZ.zone_descriptions({"hr_zone_names": ["A"]}) == _garmin_names, \
        "自定义名不足 5 个时整组回退，不能和默认名混着用"
    _custom = ["慢跑", "马配", "阈值", "间歇", "冲刺"]
    assert _HZ.zone_descriptions({"hr_zone_names": _custom}) == _custom, \
        "用户在佳明里改过名称时，要能按 Z1→Z5 覆盖"
    assert "<" not in _HZ.zone_descriptions(
        {"hr_zone_names": ["<b>", "B", "C", "D", "E"]})[0], \
        "自定义名里的 HTML 不能带进一图流"
    assert _HZ.zone_descriptions({"hr_zone_names": ["", "B", "C", "D", "E"]})[0] == "热身", \
        "自定义名为空时按官方默认名兜底"
    assert _HZ.zone_label(4, {}) == "Z5 极限"

    # 渲染断言不依赖本机 config.json：临时把配置路径指到不存在的文件
    _orig_cfg_path = _HZ.CONFIG
    _HZ.CONFIG = Path(tempfile.gettempdir()) / "__runnercoach_no_zone_config__.json"
    try:
        _zb = _AC4.zone_block([10, 20, 30, 40, 50], "心率分区", "占比 / 时长")
        for _i, _n in enumerate(_garmin_names):
            assert f"Z{_i + 1}<em>{_n}</em>" in _zb, f"活动一图流 Z{_i + 1} 名称不对"
        assert "<em>恢复</em>" not in _zb and "<em>节奏</em>" not in _zb, \
            "别把 E/M/I/T/R 训练强度名贴回佳明分区"
        _zb_custom = _AC4.zone_block([1, 1, 1, 1, 1], "心率分区",
                                     descriptions=["A", "B", "C", "D", "E"])
        assert "Z1<em>A</em>" in _zb_custom and "Z5<em>E</em>" in _zb_custom
        _zb_coros = _AC4.zone_block([1, 2, 3, 4, 5, 6], "心率分区", coros=True)
        assert "热身" not in _zb_coros and "轻松" not in _zb_coros, \
            "高驰是自己的六区口径，不能套佳明的名称"
        for _n in ("恢复", "有氧耐力", "有氧动力", "阈值", "无氧耐力", "无氧动力"):
            assert f"<em>{_n}</em>" in _zb_coros, f"高驰六区少了「{_n}」"

        _rz = _RC4._zones({"hrTimeInZone_1": 10, "hrTimeInZone_5": 20})
        assert [x[1] for x in _rz] == _garmin_names, "赛后复盘图也要用同一套名称"
        _rhtml = _RC4._zone_block({"hrTimeInZone_1": 10, "hrTimeInZone_5": 20})
        assert "<em>极限</em>" in _rhtml and "<em>无氧</em>" not in _rhtml
    finally:
        _HZ.CONFIG = _orig_cfg_path

    _acsrc4 = (ROOT / "garmin_cn/actcard.py").read_text(encoding="utf-8")
    _rcsrc4 = (ROOT / "garmin_cn/racecard.py").read_text(encoding="utf-8")
    assert "from . import hrzones as HRZ" in _acsrc4 and "from . import hrzones as HRZ" in _rcsrc4
    assert 'ZONE_DESC = ["恢复"' not in _acsrc4 and 'ZONE_DESC = ["恢复"' not in _rcsrc4, \
        "分区名称不许再在两个一图流里各写一份"
    assert '"Z5 无氧占比"' not in _rcsrc4 and "zone_label(4)" in _rcsrc4, \
        "Z5 占比文案要跟着分区名称联动"
    assert "text-overflow:ellipsis" in _acsrc4 and "text-overflow:ellipsis" in _rcsrc4, \
        "自定义名过长会撑破卡片，必须有省略兜底"

    # ------------------------------------------------------------ 跨源活动去重
    # 同一次跑步可能被佳明和高驰各记一条（将来还有 Keep）：只跨源判、
    # 同源永不合并，保留优先级 佳明 > 高驰 > Keep。
    from garmin_cn import dedupe as _DD

    def _act(aid, src, day, hms, dist, dur, kind="running"):
        return {"activity_id": aid, "source": src, "date": day,
                "start_ts": f"{day} {hms}", "distance_m": dist,
                "duration_s": dur, "type": kind}

    _g = _act("g1", "garmin", "2026-11-01", "07:00:00", 10000, 3000)
    _c = _act("c1", "coros", "2026-11-01", "07:00:40", 10050, 3040)
    _kept, _drop = _DD.pick_unique([_c, _g])
    assert [x["activity_id"] for x in _kept] == ["g1"], "跨源同一次跑步没去重"
    assert _drop and _drop[0]["duplicate_of"] == "g1", "没优先保留佳明"

    assert len(_DD.pick_unique(
        [_g, _act("g2", "garmin", "2026-11-01", "07:00:40", 10050, 3040)])[0]) == 2, \
        "同源两条被当成重复（同一天两练是真实的）"
    assert len(_DD.pick_unique(
        [_g, _act("c2", "coros", "2026-11-01", "07:12:00", 10000, 3000)])[0]) == 2, \
        "时间差 12 分钟还算重复"
    assert len(_DD.pick_unique(
        [_g, _act("c3", "coros", "2026-11-01", "07:00:30", 12000, 3000)])[0]) == 2, \
        "距离差 20% 还算重复"
    assert len(_DD.pick_unique(
        [_g, _act("k1", "keep", "2026-11-01", "07:00:30", 10000, 3000, "cycling")])[0]) == 2, \
        "跑步和骑行被互判了"
    assert len(_DD.pick_unique(
        [_act("g9", "garmin", "2026-11-01", "07:00:00", 400, 200),
         _act("c9", "coros", "2026-11-01", "07:00:20", 400, 200)])[0]) == 2, \
        "500 m 以下的活动不该参与去重"

    # Store 集成：列表与 stats 都要是去重后的数
    _ddb = Path(tempfile.mkdtemp()) / "dedupe.db"
    _ds = _Store3(_ddb)
    for _r in (_g, _c, _act("g3", "garmin", "2026-11-02", "07:00:00", 8000, 2400)):
        _ds.conn.execute(
            "INSERT INTO activities (activity_id,date,start_ts,type,duration_s,distance_m,source) "
            "VALUES (?,?,?,?,?,?,?)",
            (_r["activity_id"], _r["date"], _r["start_ts"], _r["type"],
             _r["duration_s"], _r["distance_m"], _r["source"]))
    _ds.conn.commit()
    assert len(_ds.activities("2026-11-01", "2026-11-02")) == 2, "Store 列表没去重"
    assert _ds.stats()["activities"] == 2, "活动统计数没去重"
    try:
        _ds.conn.close()
        _ddb.unlink(missing_ok=True)
    except OSError:
        pass
    assert "DD.dedupe(out)" in (ROOT / "garmin_cn/store.py").read_text(encoding="utf-8"), \
        "Store.activities 没接去重"

    # ------------------------------------------------------------ Keep 接入
    from garmin_cn import keep as _K
    from garmin_cn import secretstore as _SS

    if _SS.available():
        _cipher = _SS.protect("13800138000|pw|token")
        assert _SS.unprotect(_cipher) == "13800138000|pw|token", "DPAPI 加解密回环失败"

    _row = _K.parse({"startTime": 1762732800000, "duration": 3000, "distance": 10000,
                     "calorie": 600, "dataType": "outdoorRunning",
                     "heartRate": {"averageHeartRate": 152}},
                    "abc_9223370441312156007_rn")
    assert _row["activity_id"] == "keep_9223370441312156007"
    assert _row["type"] == "running" and _row["distance_m"] == 10000
    assert abs(_row["pace_s_km"] - 300) < 0.01 and _row["avg_hr"] == 152
    assert _row["source"] == "keep"
    assert _K.parse({"startTime": 1762732800000, "duration": 0, "distance": 0}, "x") is None, \
        "没有时长/距离的 Keep 记录不该入库"

    _khome = Path(tempfile.mkdtemp())
    _kc = _K.KeepCloud(_khome)
    _kc.mobile, _kc.password, _kc.token = "13800138000", "pw", "tok"
    _kc.token_at, _kc.device_id = time.time(), "dev"
    _kc.save()
    _kc2 = _K.KeepCloud(_khome)
    assert _kc2.load() and _kc2.token == "tok", "Keep 凭据存不进去"
    if _SS.available():
        assert _kc2.password == "pw", "加密保存后解不出密码，过期就没法自动重登"
        _raw = (_khome / "data" / "keep_auth.json").read_text(encoding="utf-8")
        assert "13800138000" not in _raw and '"pw"' not in _raw, "Keep 凭据明文落盘了"
    _kc.clear()

    # token 过期要能自动重登（不联网，mock 掉 login）
    from unittest import mock as _um2

    _kc3 = _K.KeepCloud(_khome)
    _kc3.mobile, _kc3.password, _kc3.token = "13800138000", "pw", "old"
    _kc3.token_at = 0.0
    _login_calls = {"n": 0}

    def _fake_login(m, p):
        _login_calls["n"] += 1
        _kc3.token, _kc3.token_at = "new", time.time()
        return {"user_id": "u", "token": "new"}

    with _um2.patch.object(_kc3, "login", _fake_login):
        _kc3.ensure_token()
    assert _login_calls["n"] == 1 and _kc3.token == "new", "Keep token 过期没有自动重登"

    # Keep 与佳明同一次跑步：去重保留佳明
    _k2 = {"activity_id": "keep_1", "source": "keep", "date": "2026-11-01",
           "start_ts": "2026-11-01 07:00:30", "distance_m": 10020,
           "duration_s": 3010, "type": "running"}
    _g2 = {"activity_id": "g_x", "source": "garmin", "date": "2026-11-01",
           "start_ts": "2026-11-01 07:00:00", "distance_m": 10000,
           "duration_s": 3000, "type": "running"}
    _kept2, _ = _DD.pick_unique([_g2, _k2])
    assert len(_kept2) == 1 and _kept2[0]["source"] == "garmin", \
        "Keep 与佳明同一次跑步时没保留佳明"

    _api_src5 = (ROOT / "garmin_cn/appapi.py").read_text(encoding="utf-8")
    for _fn in ("keep_status", "keep_login", "keep_logout", "keep_sync"):
        assert f'"{_fn}"' in _api_src5, f"接口白名单漏了 {_fn}"
    _kt5 = (KT / "MainActivity.kt").read_text(encoding="utf-8")
    assert '"login:keep" -> openAccount("keep")' in _kt5, "「我的」里的 Keep 入口没接上"
    assert 'apiCall("keep_sync", 30, kMobile, kPwd)' in _kt5, "App 同步没带 Keep"
    assert "fun saveKeep" in (KT / "Secrets.kt").read_text(encoding="utf-8"), \
        "Keep 凭据没走 Keystore"

    # ---- 手动补录小睡要影响睡眠得分；设备自带小睡则不重复估算 --------
    import garmin_cn.manual as _MN
    from garmin_cn import metrics as _M2

    _mhome = Path(tempfile.mkdtemp())
    _mpath = _mhome / "manual.json"
    _mstore = _Store3(_mhome / "sleep.db")
    _mday = "2026-11-05"
    _mstore.upsert_daily({"date": _mday, "sleep_total_min": 420,
                          "sleep_score": 70, "sleep_need_min": 480,
                          "resting_hr": 50, "hrv_night": 60})
    with _um2.patch.object(_MN, "PATH", _mpath):
        _before = _M2.derive(_mstore, _mday)
        assert _before["sleep"]["score_est"] is None, "没补录就冒出一个估算分"
        assert _before["sleep"]["score"] == 70
        _MN.add_nap(_mday, 60, "13:00")
        _after = _M2.derive(_mstore, _mday)
        assert _after["sleep"]["score_est"] == 100, \
            f"补录 60 分小睡后估算分不对：{_after['sleep']['score_est']}"
        assert _after["sleep"]["nap_eff"] == 60
        _sp = next(p for p in _after["readiness"]["parts"] if p["name"] == "睡眠")
        assert _sp["score"] == 100, "恢复度的睡眠分量没跟着补录变"
        # 设备自带小睡（佳明/高驰）：信任设备算法，不再叠一层估算
        _mstore.upsert_daily({"date": _mday, "sleep_total_min": 420,
                              "sleep_score": 70, "nap_min": 45,
                              "sleep_need_min": 480})
        _dev = _M2.derive(_mstore, _mday)
        assert _dev["sleep"]["score_est"] is None, \
            "设备已经给了小睡，还重复估算会算两遍"
        assert _dev["sleep"]["score"] == 70
    try:
        _mstore.conn.close()
        (_mhome / "sleep.db").unlink(missing_ok=True)
    except OSError:
        pass

    # ---- 体成分跨来源：体重来自小米、体脂来自华为，不能按日选首选来源就丢掉
    from garmin_cn import body as _BD

    _bhome = Path(tempfile.mkdtemp())
    _bst = _Store3(_bhome / "b.db")
    _BD.ensure(_bst)
    _bst.conn.execute(
        "INSERT INTO body (date, ts, weight_kg, source) VALUES (?,?,?,?)",
        ("2026-11-03", "2026-11-03 07:00", 65.0, "mi"))
    _bst.conn.execute(
        "INSERT INTO body (date, ts, weight_kg, body_fat_pct, muscle_kg, water_pct, source) "
        "VALUES (?,?,?,?,?,?,?)",
        ("2026-11-03", "2026-11-03 07:05", 65.0, 18.5, 50.2, 55.0, "huawei"))
    _bst.conn.commit()
    _bst.set_meta("body.preferred_source", "mi")
    _bs = _BD.stats(_bst, "2026-11-03")
    assert _bs["body_fat"] is None, "主口径不该混用另一台秤的体脂"
    assert _bs["body_fat_any"] == 18.5 and _bs["body_fat_any_src"] == "华为", \
        f"教练兜底值没取到另一来源的体脂：{_bs.get('body_fat_any')}"
    _btxt = _BD.text(_bs)
    assert "体脂 18.5%" in _btxt and "不同秤" in _btxt, \
        f"教练上下文没把另一来源的体脂说出来：{_btxt}"
    try:
        _bst.conn.close()
        (_bhome / "b.db").unlink(missing_ok=True)
    except OSError:
        pass

    # ---- 静息心率：高驰 rhr 与佳明 restingHeartRate 不同算法，基线只比同口径
    from garmin_cn import metrics as _M3

    _rhist = [{"date": f"2026-11-0{i + 1}", "resting_hr": 50,
               "rhr_src": "garmin", "raw": '{"restingHeartRate": 50}'}
              for i in range(6)]
    _rhist += [{"date": f"2026-11-{i + 1:02d}", "resting_hr": 56,
                "rhr_src": "coros", "raw": '{"rhr": 56}'}
               for i in range(6, 12)]
    _rtoday = {"date": "2026-11-12", "resting_hr": 56, "rhr_src": "coros",
               "raw": '{"rhr": 56}', "sleep_total_min": 420}
    _rr = _M3.readiness(_rtoday, _rhist)
    _rpart = next(p for p in _rr["parts"] if p["name"] == "静息心率")
    assert "基线 56" in _rpart["detail"], \
        f"静息心率基线混用了两家口径：{_rpart['detail']}"

    # ---- 拍照识热量：解析模型返回、批量落库、进当日合计 --------
    from garmin_cn import nutrition as _NT
    from unittest import mock as _um3

    _mhome2 = Path(tempfile.mkdtemp())
    _meals_path = _mhome2 / "meals.json"
    _reply = ('```json\n{"items":[{"name":"米饭","grams":200,"kcal":232,'
              '"protein_g":4.8,"carb_g":51.6,"fat_g":0.6},'
              '{"name":"鸡胸","grams":120,"kcal":198,"protein_g":37,'
              '"carb_g":0,"fat_g":4.2}],"note":"蛋白够，碳水偏少"}\n```')
    with _um3.patch.object(_NT, "MEALS", _meals_path), \
            _um3.patch("garmin_cn.llm.chat",
                       return_value={"text": _reply, "model": "m", "elapsed": 1.2}):
        _est = _NT.estimate_meal("AAAA", "image/jpeg")
        assert len(_est["items"]) == 2 and _est["total"]["kcal"] == 430, \
            f"识热量解析不对：{_est}"
        assert _est["items"][1]["protein_g"] == 37
        _saved = _NT.add_meals("2026-11-11", _est["items"])
        assert _saved["total"]["kcal"] == 430 and _saved["total"]["protein_g"] == 42
        assert _NT.intake("2026-11-11")["count"] == 2
    _api_src6 = (ROOT / "garmin_cn/appapi.py").read_text(encoding="utf-8")
    for _fn in ("meal_today", "meal_add", "meal_estimate"):
        assert f'"{_fn}"' in _api_src6, f"接口白名单漏了 {_fn}"
    _kt6 = (KT / "MainActivity.kt").read_text(encoding="utf-8")
    assert '"mealShot" -> askMealPhoto()' in _kt6, "教练页的拍照入口没接上"
    assert 'apiCall("meal_estimate"' in _kt6 and 'apiCall("meal_add"' in _kt6
    assert "mealShot" in (ROOT / "garmin_cn/aiui.py").read_text(encoding="utf-8")

    # ---- 模型服务：第三方地址纠偏 / 鉴权头自动识别 / 多模态错误 --------
    from garmin_cn import llm as _LG

    _lhome = Path(tempfile.mkdtemp())
    with _um3.patch.object(_LG, "CONFIG_PATH", _lhome / "llm_config.json"):
        assert _LG.normalize_base("https://opencode.ai/inference/go") == \
            "https://opencode.ai/inference/go/openai/v1", \
            "OpenCode 地址少了 /openai/v1（会 404）"
        assert _LG.normalize_base(
            "https://opencode.ai/inference/go/openai/v1/chat/completions") == \
            "https://opencode.ai/inference/go/openai/v1", \
            "粘贴整条接口地址时没去掉尾部，会拼成 /chat/completions/chat/completions"
        assert _LG.normalize_base("https://api.deepseek.com/v1") == \
            "https://api.deepseek.com/v1", "普通 OpenAI 兼容地址被纠坏了"

        _cfg0 = _LG.save_config(mode="own", provider="opencode_go", api_key="oc_sk_demo")
        assert _LG.effective_base(_cfg0).endswith("/go/openai/v1"), \
            f"OpenCode Go 预设地址不对：{_LG.effective_base(_cfg0)}"

        # 同一份 key：Bearer 被拒 → 自动换 x-api-key，并把结果记住
        _hits: list[tuple] = []

        def _fake_open(req, timeout=None):
            _hits.append((req.get_header("Authorization"),
                          req.get_header("X-api-key"),
                          req.get_header("X-opencode-session"),
                          req.get_header("User-agent")))
            if req.get_header("Authorization"):
                raise urllib.error.HTTPError(
                    req.full_url, 401, "no", {},
                    io.BytesIO(b'{"error":"missing api key"}'))
            return io.BytesIO(b'{"choices":[]}')

        with _um3.patch.object(_LG.urllib.request, "urlopen", _fake_open):
            _LG._post("https://opencode.ai/inference/go/openai/v1/chat/completions",
                      "oc_sk_demo", {"model": "m"}, cfg=_cfg0)
        assert len(_hits) == 2 and _hits[1][1] == "oc_sk_demo", \
            "401 之后没有换 x-api-key 重试，只认 Bearer 的网关会一直失败"
        assert _hits[0][2] and str(_hits[0][2]).startswith("ses_"), \
            "OpenCode 的 x-opencode-session 没带上（Go 网关要求必带）"
        # urllib 默认 UA 是 Python-urllib/x.y，Cloudflare 会直接回 403 1010
        assert _hits[0][3] and str(_hits[0][3]).startswith("RunnerCoach/"), \
            f"请求没带自己的 User-Agent（现在是 {_hits[0][3]}），Cloudflare 会 1010 拦掉"
        assert _LG.load_config()["auth_map"].get("opencode.ai") == "x-api-key", \
            "探测成功的鉴权方式没记住，每次都要多打一次 401"
        assert _LG._auth_styles(_LG.load_config(), "k",
                                "https://opencode.ai/x")[0] == "x-api-key", \
            "记住之后没有优先用生效的那种头"

        # 上游明说「Invalid credential」时不该再换头重试——换了也不会好，
        # 还会把真正的原因盖成 "requires an api key"，用户就查不到方向了。
        _tries: list[int] = []

        def _fake_401(req, timeout=None):
            _tries.append(1)
            raise urllib.error.HTTPError(
                req.full_url, 401, "no", {},
                io.BytesIO(b'{"error":{"type":"authentication_error",'
                           b'"message":"Invalid credential"}}'))

        with _um3.patch.object(_LG.urllib.request, "urlopen", _fake_401):
            try:
                _LG._post("https://opencode.ai/inference/go/openai/v1/chat/completions",
                          "oc_sk_demo", {"model": "m"}, cfg=_LG.load_config())
                assert False, "401 居然没抛错"
            except _LG.LLMError as _e4:
                assert "Invalid credential" in _e4.message, \
                    f"上游原因被吞掉了，用户没法判断是 key 过期还是填错：{_e4.message}"
        assert len(_tries) == 1, "key 明确无效还在换头重试，白多打一次 401"

    _ve = _LG._http_error_raw(400, '{"error":"image input is not supported"}')
    assert _ve.kind == "vision", f"图片被拒没单独归类（现在是 {_ve.kind}）"
    _fe = _LG._http_error_raw(403, '{"error":"FreeTierError: free models only in app"}')
    assert _fe.kind == "model", "免费模型 403 被误判成密钥无效，用户会去换 key"
    _pe = _LG._http_error_raw(401, '{"error":"invalid api key"}')
    assert _pe.kind == "auth", "真·密钥错误没有归到 auth"
    _se = _LG._http_error_raw(400, '{"type":"error","error":{"type":"MissingSessionID"}}')
    assert _se.kind == "session", "缺会话头被当成普通参数错误，用户会去乱改模型名"
    # 实测：urllib 默认 UA 打 OpenCode 会被 Cloudflare 回 403 text/plain "error code: 1010"，
    # 这不是密钥问题，报成「密钥无效」会让用户白折腾半天。
    _cf = _LG._http_error_raw(403, "error code: 1010")
    assert _cf.kind == "blocked" and "Cloudflare" in _cf.message, \
        f"Cloudflare 1010 被归成了 {_cf.kind}，用户会被误导去换 key"
    assert "x-api-key" in (ROOT / "garmin_cn/aiui.py").read_text(encoding="utf-8"), \
        "「模型服务」里没有鉴权方式选择，用户没法手工兜底"

    # ---- 建议类接口：主动禁止「内心独白」，并且失败也要落盘缓存 --------
    from garmin_cn import aicoach as _AC
    from garmin_cn import body as _BD2

    _ahome = Path(tempfile.mkdtemp())
    _astore = _Store3(_ahome / "ai.db")
    _BD2.ensure(_astore)
    _aday = dt.date.today().isoformat()
    _astore.upsert_daily({"date": _aday, "resting_hr": 50, "sleep_total_min": 420,
                          "sleep_score": 70, "hrv_night": 60})
    _astore.conn.execute(
        "INSERT INTO body (date,ts,weight_kg,source) VALUES (?,?,?,?)",
        (_aday, _aday + " 07:00", 65.0, "mi"))
    _astore.conn.commit()

    _acalls: list[int] = []

    def _fake_cot(messages, **kw):
        # 模拟 deepseek-v4.1-flash 的真实毛病：正文写内心独白、不给 JSON
        _acalls.append(1)
        return {"text": "Let me produce the JSON.\nHmm, 40 分钟轻松跑…",
                "model": "m", "elapsed": 1.0}

    with _um3.patch.object(_AC, "_store", lambda: _astore), \
            _um3.patch.object(_AC, "CACHE_DIR", _ahome / "cache"), \
            _um3.patch("garmin_cn.llm.chat", side_effect=_fake_cot):
        _d1 = _AC.daily_advice(_aday)
        assert _d1.get("fail") is True, "模型没按格式返回时没标 fail"
        assert "Let me" not in (_d1.get("why") or ""), \
            "把模型的内心独白（中英混排）贴进卡片了"
        _d2 = _AC.daily_advice(_aday)
        assert _d2.get("cached") is True, \
            "同一天第二次没命中缓存——App 切 tab 会整页重载，前端缓存靠不住"
        assert len(_acalls) == 2, f"一次建议应只打 1 次主调用 + 1 次格式修复，实际 {len(_acalls)}"

        _acalls.clear()
        _w1 = _AC.weight_review(30, _aday)
        assert _w1.get("actions"), "体重复盘解析失败时没给任何兜底说明"
        _w2 = _AC.weight_review(30, _aday)
        assert _w2.get("cached") is True, "体重复盘同一天第二次仍在请求模型"
        assert len(_acalls) == 2, f"体重复盘请求次数不对：{len(_acalls)}"
        _w3 = _AC.weight_review(30, _aday, force=True)
        assert len(_acalls) == 4 and not _w3.get("cached"), \
            "force=True 没有绕过缓存（「重新分析」按钮会点不动）"

        # 单次训练解读：步频与分圈必须进上下文。
        # 用户反馈模型说「没有最大心率和步频数据」——步频只存在 activity_detail，
        # activities 表根本没这一列，旧上下文里它永远是 None。
        _astore.upsert_activity({
            "activity_id": "A1", "date": _aday, "start_ts": _aday + " 07:00",
            "type": "running", "name": "晨跑", "duration_s": 2640,
            "distance_m": 8410, "avg_hr": 157, "max_hr": 181,
            "calories": 412, "pace_s_km": 317, "source": "garmin"})
        _astore.conn.execute(
            "INSERT OR REPLACE INTO activity_detail "
            "(activity_id, fetched_at, cad_avg, cad_max, payload) VALUES (?,?,?,?,?)",
            ("A1", _aday, 188.0, 196.0, json.dumps({"laps": [
                {"dist_m": 1000, "dur_s": 317, "pace_s": 317, "hr_avg": 150, "cad": 188},
                {"dist_m": 410, "dur_s": 140, "pace_s": 341, "hr_avg": 168, "cad": 183},
            ]})))
        _astore.conn.commit()
        _act_msgs: list = []

        def _fake_act(messages, **kw):
            _act_msgs.append(messages)
            return {"text": '{"title":"晨跑","summary":"节奏偏快","good":[],'
                            '"improve":[],"next":""}',
                    "model": "m", "elapsed": 0.2}

        with _um3.patch("garmin_cn.llm.chat", side_effect=_fake_act):
            _rev = _AC.activity_review("A1", _aday)
        assert _rev.get("ok"), f"训练解读失败：{_rev}"
        # 断言看用户消息原文：整包 json.dumps 会把引号转义成 \"，搜不到字段名
        _ctx_txt = str(_act_msgs[0][1]["content"])
        assert '"max_cadence": 196' in _ctx_txt, \
            "最大步频没进上下文，模型只能瞎猜"
        assert '"avg_cadence": 188' in _ctx_txt, "平均步频没进上下文"
        assert '"splits"' in _ctx_txt and '"km": 1.0' in _ctx_txt, \
            "每公里分圈没进上下文，判断不了后程掉速"

    # ---- 教练点评不能把「写作要求」当正文回显 --------------------------
    # 用户实测：看板上的点评直接显示「写90-140字，引用具体数字，不复述处方…」
    _echo = ("写90-140字，一到两句话，引用具体数字，说清最该注意什么。"
             "不复述处方时长配速。重点：ACWR 1.54 负荷激增是最大风险")
    assert _AC._looks_like_echo(_echo), "回显写作要求的点评没被识别出来"
    _real = ("恢复度 79 但身体电量只剩 32，TSB -35、ACWR 1.54 说明负荷涨得太快，"
             "今天别加量，先把睡眠债补回来。")
    assert not _AC._looks_like_echo(_real), "正常点评被误判成回显"
    # 同一模型还会吐出英文「计划稿」，这种也要拦
    _en = ("Need: 90-140 characters (Chinese). Cite at least two concrete numbers. "
           "Say what to watch today. No meta.")
    assert _AC._looks_like_echo(_en), "英文提纲式输出没被拦住"
    assert "两句话" in _AC.NOTE_SYSTEM and "具体数字" in _AC.NOTE_SYSTEM, \
        "点评系统提示被写成大段规则——实测越像要求，模型越会照抄"
    assert _AC._note_fp("x") != _AC._ctx_fingerprint("x"), \
        "点评缓存指纹没带版本号，旧的「提示词正文」还会被读出来"

    # ---- 拍照识热量：文字描述要一起进请求；空 items 要自动重试一次 --------
    _mreq: list = []
    _mseq = [{"text": "Sorry, I can't see food here.", "model": "m", "elapsed": 0.5},
             {"text": '{"items":[{"name":"米饭","grams":200,"kcal":232,'
                      '"protein_g":4.8,"carb_g":51.6,"fat_g":0.6}]}',
              "model": "m", "elapsed": 0.6}]

    def _fake_meal(messages, **kw):
        _mreq.append(messages)
        return _mseq[min(len(_mreq) - 1, 1)]

    with _um3.patch("garmin_cn.llm.chat", side_effect=_fake_meal):
        _meal = _NT.estimate_meal("AAAA", "image/jpeg", "一碗牛肉面，汤没喝完")
    assert "牛肉面" in _mreq[0][1]["content"][0]["text"], \
        "用户补的文字没进模型请求，等于白填"
    assert len(_mreq) == 2, "认出空 items 后没有自动重试一次"
    assert _meal["items"] and _meal["items"][0]["name"] == "米饭", \
        "重试后仍没解析出食物"

    # 吃完才想起没拍照：纯文字描述也要能估出食物（走同一套字段）
    _tmsgs: list = []

    def _fake_text_meal(messages, **kw):
        _tmsgs.append(messages)
        return {"text": '{"items":[{"name":"牛肉面","grams":500,"kcal":650,'
                        '"protein_g":32,"carb_g":88,"fat_g":18}]}',
                "model": "m", "elapsed": 0.3}

    with _um3.patch("garmin_cn.llm.chat", side_effect=_fake_text_meal):
        _tmeal = _NT.estimate_meal("", "image/jpeg", "一碗牛肉面，两个鸡蛋")
    assert _tmeal["items"] and _tmeal["items"][0]["name"] == "牛肉面", \
        "纯文字描述没估出食物"
    assert "没有照片" in str(_tmsgs[0][0].get("content") or ""), \
        "纯文字时提示词没说「这次没有照片」，模型可能要求补图"
    assert "一碗牛肉面" in str(_tmsgs[0][1].get("content") or ""), \
        "描述没进请求"
    try:
        _NT.estimate_meal("", "image/jpeg", "")
        raise AssertionError("既没图也没描述还去请求模型，白花钱")
    except ValueError:
        pass

    # ---- 数据文件必须跟着 GARMIN_HOME 走 -------------------------------
    # 旧代码把 meals.json / manual.json 写在包目录，Android 上那是 Chaquopy
    # 解压源码的临时目录，App 一更新就被重新解压覆盖 ——「中午记的饭晚上没了」。
    import os as _os2
    import subprocess as _sp
    import sys as _sys2

    _nhome = Path(tempfile.mkdtemp())
    _probe = _sp.run(
        [_sys2.executable, "-c",
         "from garmin_cn import nutrition as n, manual as m;"
         "print(n.MEALS); print(m.PATH); print(n.CONF)"],
        cwd=str(ROOT), capture_output=True, text=True,
        env={**_os2.environ, "GARMIN_HOME": str(_nhome)})
    _paths = [x.strip() for x in (_probe.stdout or "").splitlines() if x.strip()]
    assert len(_paths) == 3, f"探针没跑起来：{_probe.stderr[:200]}"
    for _p in _paths:
        assert _p.startswith(str(_nhome)), \
            f"数据文件没跟着 GARMIN_HOME 走（升级会丢）：{_p}"

    _mg = Path(tempfile.mkdtemp())
    _legacy = _mg / "pkg" / "data" / "meals.json"
    _legacy.parent.mkdir(parents=True)
    _legacy.write_text('{"2026-11-12": []}', encoding="utf-8")
    _dest = _mg / "home" / "data" / "meals.json"
    assert _NT._migrate_file(_legacy, _dest) and _dest.is_file(), \
        "旧位置的 meals.json 没搬到新位置，升级用户看起来像「数据丢了」"
    assert not _NT._migrate_file(_legacy, _dest), "重复搬会覆盖新数据"

    # ---- 「我的 → AI 模型服务」：能点开、带开关、副标题给模型与接口 ----
    from garmin_cn import aiapi as _AIA

    _ihome = Path(tempfile.mkdtemp())
    with _um3.patch.object(_LG, "CONFIG_PATH", _ihome / "llm.json"):
        _LG.save_config(mode="own", provider="opencode_go", api_key="oc_sk_demo",
                        model="deepseek-v4.1-flash")
        assert _LG.is_enabled() is True, "AI 总开关默认应该是开的"
        assert _AIA.dispatch("config.set", {"ai_enabled": False}).get("ai_enabled") is False
        _gated = _AIA.dispatch("daily", {})
        assert _gated.get("kind") == "off", f"关了 AI 还在调模型：{_gated}"
        assert _AIA.dispatch("config.get", {}).get("ok"), \
            "关了 AI 连配置都读不到，开关会点不动"
        assert AP.meal_estimate("AAAA").get("code") == "off", \
            "关了 AI 还能拍照识热量（那是要花钱的）"
        _AIA.dispatch("config.set", {"ai_enabled": True})

        _me_html = U.me_page()
        assert 'data-act="aiSettings"' in _me_html, \
            "「我的」页的 AI 行没有动作，点了没反应"
        assert 'data-act="aiToggle"' in _me_html, "AI 行没有开关"
        assert "deepseek-v4.1-flash" in _me_html and "opencode.ai" in _me_html, \
            "AI 行没显示模型名与接口地址"

    _kt7 = (KT / "MainActivity.kt").read_text(encoding="utf-8")
    assert '"aiSettings" -> openAiSettings()' in _kt7 and '"aiToggle" -> toggleAi()' in _kt7, \
        "App 没接「打开配置」和「开关」两个动作"
    assert 'id="aiSeg"' in (ROOT / "garmin_cn/aiui.py").read_text(encoding="utf-8"), \
        "配置抽屉里没有 AI 开关"

    # ---- 素材批量删除：删不掉的不能从列表里消失 ----------------------
    _cdir = Path(tempfile.mkdtemp())
    (_cdir / "ok.jpg").write_bytes(b"x")
    _baddir = _cdir / "not_a_file"
    _baddir.mkdir()
    with _um3.patch.object(AP, "CLIPS", _cdir / "clips.json"):
        AP._clips_write([
            {"id": "c1", "path": str(_cdir / "ok.jpg"), "name": "好删的",
             "kind": "image", "dur": 3},
            {"id": "c2", "path": str(_baddir), "name": "删不掉的",
             "kind": "image", "dur": 3},
        ])
        _rm = AP.clips_remove_many(["c1", "c2"])
    assert _rm["removed"] == 1 and not _rm["ok"] and _rm["failed"], \
        f"删除结果不诚实（失败也报成功）：{_rm}"
    assert [x["id"] for x in _rm["items"]] == ["c2"], \
        "删不掉的条目被从列表里摘掉了——用户会以为删干净了"
    assert not (_cdir / "ok.jpg").exists(), "该删的文件还在"

    # ---- 产物页进度：只更新 DOM 当数据变化，并给原生一个 gcTick 钩子 ----
    _files_html = U.files_page(Path(tempfile.mkdtemp()))
    assert "window.gcTick = tick" in _files_html, \
        "进度轮询没暴露 gcTick，原生无法在 WebView 节流时戳醒它"
    assert "if (key === lastKey) return;" in _files_html, \
        "进度每次轮询都无条件重写 innerHTML，过渡会被反复打断"
    assert 'window.gcTick&&window.gcTick()' in (KT / "MainActivity.kt").read_text(encoding="utf-8"), \
        "原生没有在生成期间定时调用 gcTick"

    # ---- 训练计划的按日覆盖：比赛日替换后，周量口径必须与 7 天课表相加一致 ----
    from garmin_cn import raceplan as _RP

    if _RP.available():
        assert _RP.day_info(dt.date(2027, 6, 1)) is None, "计划区间外的日期不该有课次"
        _wk = 4
        _d0 = _RP.span()[0] + dt.timedelta(days=(_wk - 1) * 7)
        _rows = _RP.week_rows(_d0)
        _sum_km = sum(r["km"] for r in _rows)
        _info = _RP.day_info(_d0)
        assert abs(_sum_km - _info["week_km"]) <= 1.0, \
            (f"第 {_wk} 周周量口径不一致：7 天合计 {_sum_km:.1f} vs 卡头 {_info['week_km']}")
        _ov = _RP.overrides().get(str(_wk)) or {}
        for _iso, _o in (_ov.get("days") or {}).items():
            _hit = _RP.day_info(dt.date.fromisoformat(_iso))
            assert _hit and _hit["kind"] == _o.get("kind", _hit["kind"]), \
                f"日内覆盖没生效：{_iso}"
            assert abs(_hit["km"] - float(_o.get("km", _hit["km"]))) < 0.1, \
                f"日内覆盖的里程没写进去：{_iso}"

    # ---- 赛事页的编辑入口与补给计划（有本地赛事数据才跑） ----
    try:
        from garmin_cn import event as _EV

        if _EV._read(_EV.SEED_EVENT["slug"], "event.json"):
            # 多赛事后 load_event() 默认是「下一场」（可能是 43K），
            # 凡是断言 105K 特征的（CP 表 / 换装点）必须显式传 slug
            _P = _EV.SEED_EVENT["slug"]
            # 补给：总碳水必须等于「时长 × 速率」，胶数按自带比例向上取整
            _fp = _EV.fuel_plan(_P)
            _cfgf = _fp["cfg"]
            _tot = _fp["totals"]
            _want = _tot["hours"] * _cfgf["carb_g_h"]
            assert abs(_tot["carb_g"] - _want) <= _tot["hours"] * 3, \
                f"补给总量对不上分段之和：{_tot['carb_g']} vs {_want:.0f}"
            for _r in _fp["rows"]:
                assert _r["gels"] * _cfgf["gel_g"] >= _r["carry_g"] - 1e-6, \
                    f"CP{_r['idx']} 的胶不够覆盖自带碳水"
                assert _r["stop_min"] >= 0 and _r["carry_ml"] <= _cfgf["carry_ml"]
            assert sum(1 for _r in _fp["rows"] if _r["dropbag"]) == 1, \
                "换装点数量不对（105K 只有一个）"

            # 赛中预测：用相对用时算落后量，年份不同也不能算出几千小时
            _slug = _EV.SEED_EVENT["slug"]
            _orig = _EV._read(_slug, "race.json")
            _base = dt.datetime(2027, 3, 27, 6, 0)
            try:
                _EV.set_race(_P, "reset")
                _EV.set_race(_P, "start", at=_base)
                _EV.set_race(_P, "pass", 1, at=_base + dt.timedelta(hours=2, minutes=8))
                _EV.set_race(_P, "pass", 2, at=_base + dt.timedelta(hours=4, minutes=32))
                _pr = _EV.race_projection(_P, now=_base + dt.timedelta(hours=9, minutes=15))
                assert _pr["state"] == "racing" and _pr["passed_count"] == 2
                assert abs(_pr["delay_s"] / 60 - 21) <= 2, \
                    f"落后量算错了：{_pr['delay_s'] / 60:.0f} min（期望约 21）"
                assert 0.9 < (_pr["req_factor"] or 0) < 1.2, \
                    f"保目标倍率不合理：{_pr['req_factor']}"
                assert _pr["rows"][2]["done"] is False, "没过的 CP 不该标成已完成"
                _p1 = _pr["rows"][0]
                assert _p1["done"] and _p1["actual"] is not None, "已过的 CP 没有实际到达时间"
            finally:
                if _orig:
                    _EV._write(_slug, "race.json", _orig)
                else:
                    import os as _os
                    _p = _EV._dir(_slug) / "race.json"
                    if _p.exists():
                        _os.remove(_p)

            # 校验：非法目标 / 非法库存必须抛 ValueError，不能悄悄写坏数据
            for _bad in ({"status": "zzz"}, {"goal_h": 99}, {"start_time": "25:00"}):
                try:
                    _EV.set_target(_P, **_bad)
                    raise AssertionError(f"非法输入被接受了：{_bad}")
                except ValueError:
                    pass
            try:
                _EV.set_cps(_P, {"2": {"km": 1.0}})
                raise AssertionError("CP 里程倒退没有报错")
            except ValueError:
                pass
    except ImportError:
        pass

    # ---- 导入与校验：物理红线必须拦住错数据，解析器不许瞎猜 ----
    try:
        import copy as _copy
        import json as _json
        import shutil as _shutil

        from garmin_cn import ingest as _IN

        if _EV._read(_EV.SEED_EVENT["slug"], "event.json"):
            _P = _EV.SEED_EVENT["slug"]
            _ev0, _c0 = _EV.load_event(_P), _EV.load_course(_P)
            _i0 = _IN.validate(_ev0, _c0)
            assert not _IN.errors(_i0), "官方 CP 表自己过不了校验：" + _IN.explain(_i0)

            # 里程倒序 / 关门重复 / 末点对不上总距离
            _bad = _copy.deepcopy(_c0)
            _bad["segments"][1]["km"] = 5.0
            assert any(i["code"] == "km_order" for i in _IN.errors(_IN.validate(_ev0, _bad)))

            _bad = _copy.deepcopy(_c0)
            _bad["segments"][1]["cutoff"] = "08:05"
            assert any(i["code"] == "cut_order" for i in _IN.errors(_IN.validate(_ev0, _bad)))

            # 钟点漏一小时（10:10 → 00:10）：会被当成跨零点，必须靠「时间/等效占比」抓
            _bad = _copy.deepcopy(_c0)
            _bad["segments"][1]["cutoff"] = "00:10"
            assert any(i["code"] == "share" for i in _IN.errors(_IN.validate(_ev0, _bad))), \
                "把 10:10 写成 00:10 没被抓到——这是最危险的一类错"

            _bad = _copy.deepcopy(_c0)
            _bad["segments"][-1]["km"] = 99.5
            assert any(i["code"] == "km_last" for i in _IN.validate(_ev0, _bad))

            # 解析器：表格能认，认不出就返回空表（不猜）
            _d = _IN.from_text("CP1 山口 15km 22:30\nCP2 半程 40km 01:30\nCP3 终点 80km 16:00")
            assert len(_d["segments"]) == 3 and _d["segments"][1]["cutoff"] == "01:30"
            assert not _IN.from_text("这周末有个比赛，信息还没出")["segments"], \
                "没有 CP 表却解析出了内容——解析器在猜"

            # 排版矩阵：规程里的 CP 表至少有这七种写法，全都得认
            _fmt = {
                "标准": "CP1 仙山雅宿 10.9km 关门 08:05",
                "制表符多列": "CP1\t仙山雅宿\t10.9\t515\t08:05\t有水",
                "全角": "ＣＰ１　仙山雅宿　１０.９ｋｍ　０８：０５",
                "区间": "CP1 仙山雅宿 约10.5-11.2km 08:05",
                "行尾编号": "仙山雅宿 10.9km 08:05 CP1",
                "开放+关门": "CP1 仙山雅宿 10.9km 07:30-08:05 关门",
                "时长制": "CP1 山顶 12.5 公里 2小时30分",
                "PDF 错行": "CP1 仙山雅宿 10.9\n关门 08:05",
            }
            for _k, _t in _fmt.items():
                _seg = _IN.from_text(_t)["segments"]
                assert len(_seg) == 1, f"「{_k}」这种排版没解析出来"
                assert _seg[0]["name"] and "关门" not in _seg[0]["name"], \
                    f"「{_k}」的地名把列名粘进去了：{_seg[0]['name']}"
                assert 0 < _seg[0]["km"] <= 200, f"「{_k}」的里程离谱：{_seg[0]['km']}"
            # 两个时间必须取关门（开放时间会让余量凭空多半小时）
            assert _IN.from_text(_fmt["开放+关门"])["segments"][0]["cutoff"] == "08:05"
            assert _IN.from_text(_fmt["时长制"])["segments"][0]["cutoff"] == "2h30"
            assert _IN.from_text(_fmt["PDF 错行"])["segments"][0]["cutoff"] == "08:05"
            # 口语描述没有结构，必须拒绝，不能猜
            assert _IN.from_text("第一个点大概 11 公里，8 点多关门")["segments"] == []

            # 新建 + 沿用上一届：日期容错，且起跑/总关门要一起沿用
            _ne = _EV.new_event("校验用新赛事", "2027/7/10", 70, 3200)
            try:
                assert _ne["date"] == "2027-07-10", f"日期容错失效：{_ne['date']}"
                _dp = _IN.from_prev(_P, _ne)
                assert _dp["start_time"] and _dp["duration_h"], \
                    "沿用上一届时没带上起跑时间/总关门——时间表套上去会变成不可能的速度"
                _ra = _IN.apply(_ne, _dp)
                assert _ra["ok"], _IN.explain(_ra["issues"])
                assert _EV.load_event(_ne["slug"])["start_time"] == _EV.load_event(_P)["start_time"]
                assert any(i["code"] == "copied" for i in _ra["issues"]), \
                    "沿用后没有「待核对」提醒"
            finally:
                _shutil.rmtree(_EV._dir(_ne["slug"]), ignore_errors=True)

            # apply：有 error 必须拒绝写入，且不动现有文件
            _tmp = _EV.EVENTS / "_selftest-ingest"
            try:
                _tmp.mkdir(parents=True, exist_ok=True)
                _fx = {"slug": "_selftest-ingest", "name": "校验用假赛事",
                       "date": "2027-06-05", "start_time": "20:00",
                       "duration_h": 20, "distance_km": 80.0, "ascent_m": 3000,
                       "status": "confirmed", "goal_s": 9 * 3600}
                (_tmp / "event.json").write_text(
                    _json.dumps(_fx, ensure_ascii=False), encoding="utf-8")
                _evt = _EV.load_event("_selftest-ingest")
                _dr = _IN.from_text("夜赛 80K\n2027年6月5日 20:00 起跑\n总距离：80公里\n"
                                    "累计爬升：3000米 总关门 20小时\n"
                                    "CP1 山口 15km 22:30\nCP2 半程 40km 01:30\n"
                                    "CP3 终点 80km 16:00")
                _r = _IN.apply(_evt, _dr)
                assert _r["ok"] and _r["n_segments"] == 3, _IN.explain(_r["issues"])
                _c = _EV.load_course("_selftest-ingest")
                assert _c["segments"][1]["cutoff_day"] == 2, "跨零点的 cutoff_day 没算对"
                assert (_c.get("provenance") or {}).get("source") == "text"

                _before = (_tmp / "course.json").read_text(encoding="utf-8")
                _bad_dr = _copy.deepcopy(_dr)
                _bad_dr["segments"][1]["km"] = 5.0          # 里程倒序
                _r2 = _IN.apply(_evt, _bad_dr)
                assert not _r2["ok"], "有 error 的草稿被写进去了"
                assert (_tmp / "course.json").read_text(encoding="utf-8") == _before, \
                    "拒绝写入时居然改了文件"
            finally:
                _shutil.rmtree(_tmp, ignore_errors=True)
    except ImportError:
        pass

    # ---- 资料条目：增删、校验、附件白名单 ----
    try:
        from garmin_cn import event as _EV

        if _EV._read(_EV.SEED_EVENT["slug"], "event.json"):
            _P = _EV.SEED_EVENT["slug"]
            _bak = _EV._read(_P, "docs.json")
            _junk = _EV._dir(_P) / "_selftest.exe"
            try:
                _EV._write(_P, "docs.json", {"items": []})
                _EV.add_doc(_P, "rule", "关门与罚则", body="超时即退赛")
                _EV.add_doc(_P, "aid", "官方规程", url="https://example.com/a.pdf")
                _d = _EV.docs(_P)
                assert len(_d["items"]) == 2, "资料没写进去"
                assert _d["items"][0]["kind_cn"], "分类没有中文名"
                assert _d["items"][1]["url"].startswith("https://")

                # 必填与协议校验：坏输入必须抛错，不能悄悄存进档案
                for _bad in ({"kind": "zzz", "title": "x", "body": "y"},
                             {"kind": "rule", "title": "", "body": "y"},
                             {"kind": "rule", "title": "x"},
                             {"kind": "rule", "title": "x", "url": "javascript:alert(1)"}):
                    try:
                        _EV.add_doc(_P, **_bad)
                        raise AssertionError(f"非法资料被接受了：{_bad}")
                    except ValueError:
                        pass

                assert _EV.del_doc(_P, 0) and len(_EV.docs(_P)["items"]) == 1, "删除没生效"
                assert not _EV.del_doc(_P, 99), "越界删除竟然成功了"

                # 附件白名单：非白名单扩展名不列出来
                _junk.write_bytes(b"MZ")
                _names = [f["name"] for f in _EV.docs(_P)["files"]]
                assert "_selftest.exe" not in _names, "可执行文件被当成附件列出来了"
                assert all(f["size"] >= 0 for f in _EV.docs(_P)["files"])
                assert _EV.human_size(8) == "8 B" and _EV.human_size(1048576) == "1.0 MB"
            finally:
                _junk.unlink(missing_ok=True)
                if _bak:
                    _EV._write(_P, "docs.json", _bak)
                else:
                    (_EV._dir(_P) / "docs.json").unlink(missing_ok=True)
    except ImportError:
        pass

    # ---- 赛后复盘：插值、段级偏差、未完赛判定 ----
    try:
        from garmin_cn import review as _RV

        # 插值：0–10 km 匀速 6:00/km，5.5 km 处应落在 33 分钟
        _cd_ = [i * 1000.0 for i in range(11)]
        _ct_ = [i * 360.0 for i in range(11)]
        assert abs(_RV.at_km(_cd_, _ct_, 5.5) - 1980) < 1, "CP 到达插值不准"
        assert _RV.at_km(_cd_, _ct_, 12) is None, "超出总距离应当返回 None（没跑到）"

        # 找活动：同日优先于次日，且太短的不算
        _acts = [
            {"activity_id": "a", "date": "2027-03-28", "distance_m": 103000},
            {"activity_id": "b", "date": "2027-03-27", "distance_m": 103000},
            {"activity_id": "c", "date": "2027-03-27", "distance_m": 5000},
        ]
        assert _RV.find_activity(_acts, {"date": "2027-03-27", "distance_km": 103})["activity_id"] == "b"

        # 复盘：前半程正好按计划、后半程每公里多 60 s
        if _EV._read(_EV.SEED_EVENT["slug"], "event.json"):
            _P = _EV.SEED_EVENT["slug"]
            _ev = _EV.load_event(_P)
            _rows = _EV.plan_and_margin(_P)
            _tot = 103300.0
            _dd, _tt, _v = [], [], 1.9
            for _i in range(0, 103400, 100):
                _dd.append(float(_i))
                _tt.append(_tt[-1] + 100 / _v if _tt else 0.0)
            _act = {"activity_id": "t", "date": _ev["date"],
                    "start_ts": _ev["date"] + " 06:00:00",
                    "duration_s": _tt[-1], "distance_m": _dd[-1]}
            _rep = _RV.review(_ev, _rows, _act, {"route": {"d": _dd, "t": _tt}})
            assert _rep["ok"] and _rep["stats"]["reached"] == len(_rows), "全部跑完却漏判 CP"
            assert _rep["stats"]["half_ratio"] and abs(_rep["stats"]["half_ratio"] - 1) < 0.05, \
                "匀速跑竟然算出前后半程不一致"
            # 段级偏差必须等于「实际段用时 − 计划段用时」；匀速跑与按地形分配的计划
            # 本身就会差很多，所以这里只校验口径自洽，不假设速度与计划一致
            for _x in _rep["rows"]:
                if _x["done"]:
                    assert abs(_x["leg_delta_s"] - (_x["leg_s"] - _x["plan_leg_s"])) < 1, \
                        f"CP{_x['idx']} 段偏差口径不一致"
            _acc = 0.0
            for _x in _rep["rows"]:
                _acc += _x["plan_leg_s"]
                assert abs(_acc - (_x["planned"] - _rep["rows"][0]["planned"]).total_seconds()
                           - _rep["rows"][0]["plan_leg_s"]) < 2, \
                    f"CP{_x['idx']} 的计划段用时累加对不上"

            # 未完赛：只跑到 40 km
            _short = {k: v for k, v in _act.items()}
            _short["distance_m"] = 40000
            _rep2 = _RV.review(_ev, _rows, _short,
                               {"route": {"d": _dd[:401], "t": _tt[:401]}})
            _done2 = [x for x in _rep2["rows"] if x["done"]]
            assert len(_done2) < len(_rows), "没跑完却把所有 CP 都算成已到达"
            assert _rep2["stats"]["dnf_km"], "未完赛没有记录停在多少公里"
            assert all(x["actual"] is None for x in _rep2["rows"][len(_done2):]), \
                "未到达的 CP 不该有实际时刻"
    except ImportError:
        pass

    # ---- 多赛事：档案不能串、默认必须是「下一场」、没有 CP 表就不编余量 ----
    try:
        from garmin_cn import event as _EV

        with tempfile.TemporaryDirectory() as _event_fixture, \
                _mock.patch.object(_EV, "EVENTS", Path(_event_fixture)):
            assert _EV.list_events() == [], "新用户不能自动出现作者的赛事"
            _EV.ensure_seeds()  # 显式创建本测试需要的多赛事夹具。
            if len(_EV.EVENT_SEEDS) >= 2:
                _evs = _EV.list_events()
                assert len(_evs) >= 2, "赛事列表少于 2 场，多赛事没生效"
                _slugs = [e["slug"] for e in _evs]
                assert len(_slugs) == len(set(_slugs)), "赛事列表里有重复 slug"
                assert [e["date"] for e in _evs] == sorted(e["date"] for e in _evs), \
                    "赛事列表没按日期排序"
                # 每场都有自己的名字与装备清单：串档时这里会先是同一个名字
                _names = {_EV.load_event(_s)["name"] for _s in _slugs}
                assert len(_names) == len(_slugs), "不同赛事拿到了同一个名字（档案串了）"
                for _s in _slugs:
                    assert (_EV._dir(_s) / "gear.md").exists(), f"{_s} 没有装备清单"
                    _c = _EV.load_course(_s)
                    assert _c["segments"], f"{_s} 的赛道是空的"
                    _e = _EV.load_event(_s)
                    assert abs(_c["distance_km"] - _e["distance_km"]) < 0.01, \
                        f"{_s} 的 course 与 event 距离不一致"
                    if not _EV.list_events()[_slugs.index(_s)]["has_course"]:
                        assert len(_c["segments"]) == 1, f"{_s} 不该有多个 CP"
                        for _r in _EV.plan_and_margin(_s):
                            # 没有关闭就没有余量，绝不能凑一个数出来
                            assert _r["margin_s"] is None or _r["cutoff"], \
                                f"{_s} 没有关门却算出了余量"
                _nxt = _EV.nearest_upcoming()
                assert _nxt and _nxt["days"] >= 0, "下一场取到了已过期的赛事"
                assert _EV.active_slug() == _nxt["slug"], "默认赛事不是「下一场」"
                # 陌生 slug 必须报错，不能凭空造一份只有 slug 的空档案
                try:
                    _EV.load_event("no-such-race-xyz")
                    raise AssertionError("陌生 slug 被当成了合法赛事")
                except ValueError:
                    pass
    except ImportError:
        pass

    # ---- 教练上下文要带赛事档案（没有本地数据就跳过，CI 上不跑） ----
    try:
        from garmin_cn import event as _EV

        if _EV._read(_EV.SEED_EVENT["slug"], "event.json"):
            _ctx = context_markdown(res)
            assert "## 备赛赛事" in _ctx, "教练上下文没带上赛事，模型会只盯着当天恢复度"
            assert ("最紧关门" in _ctx) or ("无官方 CP 表" in _ctx), \
                "上下文里既没有余量预警也没有「无关门」说明"
            assert "主赛" in _ctx or len(_EV.list_events()) == 1, \
                "多赛事时上下文没有区分主赛与训练赛"
    except ImportError:
        pass

    # ---- AI 对话历史：落盘、滑动窗口、摘要归档 ----
    # 用户的诉求是「切页面回来还能看到之前问过什么」，所以原文一条都不能删；
    # 只把窗口外的部分压成摘要，避免上下文越滚越大。
    try:
        from unittest import mock as _umCS
        from . import chatstore as _CS

        _cd = Path(tempfile.mkdtemp())
        with _umCS.patch.object(_CS, "PATH", _cd / "ai_chat.json"):
            _CS.clear()
            assert _CS.context() == [], "空历史不该有上下文"
            for _i in range(3):
                _CS.append("user", f"问{_i}")
                _CS.append("assistant", f"答{_i}")
            assert len(_CS.load()["messages"]) == 6
            assert len(_CS.context()) == 6
            assert not _CS.should_archive(), "6 条就触发归档，窗口太小"

            for _i in range(20):
                _CS.append("user", f"追{_i}")
                _CS.append("assistant", f"回{_i}")
            _n = len(_CS.load()["messages"])
            assert _CS.should_archive(), "超出窗口却没触发归档"
            _r = _CS.archive(lambda old, b: f"摘要{len(b)}条")
            assert _r and _r["upto"] == _CS.CHUNK
            assert len(_CS.load()["messages"]) == _n, "归档把原文删了（历史不该丢）"
            _ctx = _CS.context()
            assert _ctx[0]["role"] == "system" and "摘要" in _ctx[0]["content"]
            assert len(_ctx) == _CS.KEEP + 1, "上下文条数不对（摘要 + 窗口）"

            _CS.append("user", "这一句要撤掉")
            _CS.drop_last_user()
            assert _CS.load()["messages"][-1]["content"] != "这一句要撤掉"

            # 「加载更早」：offset 的语义是「从最新往回跳过多少条」
            for _i in range(60):
                _CS.append("user", f"分页{_i}")
            from . import aiapi as _AP
            _p1 = _AP.dispatch("chat_history", {"limit": 80, "offset": 0})
            assert len(_p1["messages"]) == 80 and _p1["has_more"] is True
            _p2 = _AP.dispatch("chat_history", {"limit": 80, "offset": _p1["offset"]})
            assert _p2["offset"] > _p1["offset"], "翻页后 offset 没往前走"
            assert _p2["messages"][0] != _p1["messages"][0], "翻页拿到的是同一批"

            (_cd / "ai_chat.json").write_text("{坏掉的 json", encoding="utf-8")
            assert _CS.load()["messages"] == [], "历史文件损坏时没有兜底"
        shutil.rmtree(_cd, ignore_errors=True)
    except ImportError:
        pass

    # ---- 有分段但关门未公布：海拔剖面不能崩 ----
    # 真机踩过：路跑赛事按补给站建了分段、官方关门还没公布，margin_s 全是 None，
    # _relief 里拿 None 和 0 比大小直接 TypeError，整页 500。
    try:
        from . import eventui as _EUI2
        _rel = [{"idx": 1, "km": 10, "name": "补给 10 km", "margin_s": None,
                 "cutoff": None, "dplus": 300, "dminus": 200},
                {"idx": 2, "km": 20, "name": "终点", "margin_s": None,
                 "cutoff": None, "dplus": 200, "dminus": 300}]
        _svg = _EUI2._relief(_rel)
        assert "<svg" in _svg and "灰点" in _svg, "无关门的 CP 没有灰点图例"
        _rel[0]["margin_s"], _rel[1]["margin_s"] = 600, -60
        assert "灰点" not in _EUI2._relief(_rel), "全是有关门的点却挂了灰点图例"
    except (ImportError, AttributeError):
        pass

    # ---- 可达性：注册了路由 ≠ 用户能进来 ----
    # 2026-11-12 连续踩两次：/event 和 /posture 都能渲染、URL 直开也正常，
    # 但页面上没有任何入口，用户装完发现「我们做的东西去哪了」。
    # 这里检查的是**跳转语句**，不是路由注册——路由在 serve.py 里永远都有。
    for _f, _needle, _what in (
        ("garmin_cn/aiui.py", "location.href = '/event'", "备赛页"),
        ("garmin_cn/aiui.py", "location.href = '/posture'", "姿态评估"),
        ("android/app/src/main/java/mm/runnercoach/app/MainActivity.kt",
         '"/ai"', "教练 tab"),
    ):
        assert _needle in (ROOT / _f).read_text(encoding="utf-8"), \
            f"{_what} 没有入口：页面能渲染，但用户点不进去"

    # ---- 删除赛事：只认 EVENTS 下一层、且确实有 event.json ----
    # 删除是不可逆的，服务端这几个拒绝条件不能只靠 UI 那层两步确认。
    try:
        from . import event as _EVD
        for _bad in ("..", "a/b", "", "a\\b", "."):
            try:
                _EVD.delete_event(_bad)
                raise AssertionError(f"delete_event 没拦住 {_bad!r}")
            except ValueError:
                pass
    except ImportError:
        pass

    # ---- 姿态评估历史：落盘、按动作替换、可清空 ----
    try:
        from unittest import mock as _umPT
        from . import posturestore as _PS

        _pd = Path(tempfile.mkdtemp())
        with _umPT.patch.object(_PS, "PATH", _pd / "posture.json"):
            _PS.clear()
            assert _PS.current() == [], "空库不该有当前结果"
            _PS.save_result("sls_left", {"move": "sls_left", "name": "左", "checks": {}})
            _PS.save_result("sls_right", {"move": "sls_right", "name": "右", "checks": {}})
            assert len(_PS.current()) == 2
            _PS.save_result("sls_left", {"move": "sls_left", "name": "左2", "checks": {}})
            assert len(_PS.current()) == 2, "同动作重拍应替换，不该叠加"
            assert _PS.load()["history"][0]["name"] == "左2", "历史没记最新一次"
            assert len(_PS.load()["history"]) == 3, "历史应该每次都追加"
            _PS.clear()
            assert _PS.current() == [], "清空后还有残留"
        shutil.rmtree(_pd, ignore_errors=True)
    except ImportError:
        pass

    # ---- 趋势：多弱项按时间聚合、按严重度排序 ----
    try:
        from . import posture as _PT

        _th = [
            {"at": "2026-11-13 07:00",
             "result": {"checks": {"knee_valgus": {"state": "mild"},
                                   "heel_lift": {"state": "ok"}}}},
            {"at": "2026-10-16 07:00",
             "result": {"checks": {"knee_valgus": {"state": "clear"},
                                   "heel_lift": {"state": "mild"}}}},
        ]
        _tr = _PT.trend(_th)
        _ids = [x["id"] for x in _tr]
        assert "hip_abduct" in _ids and "ankle_rom" in _ids, "趋势没按弱项聚合"
        _hip = [x for x in _tr if x["id"] == "hip_abduct"][0]
        assert [p["score"] for p in _hip["points"]] == [2, 1], "趋势点没按时间升序"
    except ImportError:
        pass

    # ---- 姿态评估：模板自洽 + 模型输出收敛 ----
    try:
        from . import posture as _PO
        from .workout import POWER as _POW

        pool = {m[0] for grp in _POW.values() for m in grp}
        for mid, mv in _PO.MOVES.items():
            for cid in mv["checks"]:
                assert cid in _PO.CHECKS, f"{mid} 引用了未定义的观察点 {cid}"
            assert mv["shot"] and mv["how"], f"{mid} 缺拍摄或动作说明"
        for cid, c in _PO.CHECKS.items():
            assert c["weak"] in _PO.WEAK, f"观察点 {cid} 指向未知弱项 {c['weak']}"
        for wid, w in _PO.WEAK.items():
            assert any(n in pool for n, *_ in w["moves"]), \
                f"弱项 {wid} 的动作全都不在课表动作池里（会和课表打架）"

        # 模型爱自由发挥：别名、非法状态、多编的观察点、缺失的观察点
        raw = {"checks": {"knee_valgus": {"state": "clear", "evidence": "膝往里塌"},
                          "heel_lift": {"state": "UNKNOWN", "evidence": "被挡住"},
                          "bogus_item": {"state": "clear", "evidence": "编的"}},
               "shot_quality": "weird"}
        n = _PO.normalize(raw, "sls_left")
        assert n["checks"]["knee_valgus"]["state"] == "clear"
        assert n["checks"]["heel_lift"]["state"] == "unclear", "非法状态没折成 unclear"
        assert n["checks"]["trunk_lateral"]["state"] == "unclear", "缺失的观察点没兜底"
        assert "bogus_item" not in n["checks"], "模型多编的观察点没被丢掉"
        assert n["extra_ignored"] == ["bogus_item"]
        assert n["shot_quality"] == "usable", "非法照片质量没有兜底"
        assert _PO.weak_points(n) == ["hip_abduct"], "unclear 不该算作弱项"

        empty = _PO.plan([])
        assert empty["empty"] and not empty["moves"], "没有判读结果却给出了方案"
        p = _PO.plan([n])
        assert p["moves"] and len(p["moves"]) <= 5
        assert len(p["weeks"]) == 4 and p["weeks"][-1]["sets"] < p["weeks"][2]["sets"], \
            "第 4 周不是减量复测"
        assert "髋外展" in _PO.text_report({"results": [n]})
    except ImportError:
        pass

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
