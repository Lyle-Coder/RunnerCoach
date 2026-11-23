"""离线自检：解析、指标、渲染、App 源码守卫全链路。

从 `cli.py` 抽出来的 —— 之前它一个函数 1900 行，占掉入口文件近一半，
既不好读也没法单独跑。现在 `cli.cmd_selftest` 只是薄包装，
`python -m garmin_cn.selftest` / `run.bat selftest` 都能直接调。

约定：全部离线，不联网、不依赖真实账号；需要数据库时用临时库。
改动任何模块后都应该跑一遍，断言里的失败信息写的是「为什么不能这样改」。
"""
from __future__ import annotations

import datetime as dt
import inspect
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
    # 关掉素材下载：自检必须离线，也不该在仓库目录里造下载缓存。
    # 放在第一行——后面任何一次页面渲染都可能去补那 1.7 MB 素材。
    from garmin_cn import contentpack as _CPK
    _CPK.ENABLED = False
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

    # 网关层 401 必须归到凭据失效（2026-11-16 小米体重同步实测）：
    # 以前一律 MiError，上层既不换会话也不弹重新登录，只甩一段 JSON 给用户。
    e401 = MI._http_failure(401, '{"code":3,"message":"auth err"}')
    assert isinstance(e401, MI.MiAuthError), "HTTP 401 没归到 MiAuthError"
    assert "重新登录" in str(e401)
    assert isinstance(MI._http_failure(403, "auth err"), MI.MiAuthError)
    e503 = MI._http_failure(503, "gateway timeout")
    assert isinstance(e503, MI.MiError) and not isinstance(e503, MI.MiAuthError), \
        "5xx 不该被误判成登录失效"
    assert MI.is_auth_error(e401)
    assert MI.is_auth_error('小米接口 HTTP 401: {"code":3,"message":"auth err"}')
    assert not MI.is_auth_error("连不上小米服务器：URLError")

    # 同步接口：换状态码/文案也要兜底成 need_login；普通错误不把 JSON 甩给界面。
    from garmin_cn import appapi as _API
    import unittest.mock as _mock
    assert "检查网络" in _API._friendly_mi_error("连不上小米服务器：timed out")
    assert "{" not in _API._friendly_mi_error('小米接口 HTTP 401: {"code":3}')
    with _mock.patch.object(_API, "Store"), _mock.patch.object(
            MI, "sync", side_effect=MI.MiError(
                '小米接口 HTTP 401: {"code":3,"message":"auth err"}')):
        r401 = _API.mi_sync(days=7)
    assert r401.get("need_login") and not r401.get("ok"), r401
    with _mock.patch.object(_API, "Store"), _mock.patch.object(
            MI, "sync", side_effect=MI.MiAuthError(
                '小米账号接口返回异常（可能被风控）：{"code":3}')):
        r_auth = _API.mi_sync(days=7)
    assert "{" not in str(r_auth.get("error")), r_auth
    with _mock.patch.object(_API, "Store"), _mock.patch.object(
            MI, "sync", side_effect=MI.MiError(
                '小米接口 HTTP 503: {"message":"busy"}')):
        r503 = _API.mi_sync(days=7)
    assert "{" not in str(r503.get("error")) and not r503.get("need_login"), r503

    # 「已登录」只表示本地存过凭据，不表示云端还认；别让用户以为 App 自相矛盾。
    import inspect as _inspect
    from garmin_cn import ui as _U
    _me_src = _inspect.getsource(_U.me_page)
    assert 'm_ok = "已保存登录信息"' in _me_src and 'h_ok = "已保存登录信息"' in _me_src, \
        "体重行还写着「已登录」"

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

    # 网页外壳与交互树：五个 tab 共用一套 ui.page，卡片动作走 data-act，
    # 活动行走 data-aid。约束见 docs/交互树.md：一个动作只有一个入口。
    from garmin_cn import history as HW
    from garmin_cn import ui as U

    idx = HW.index_section(U.coverage())
    assert "/view?date=" in idx and "每日数据" in idx
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
    assert '>未完成</span>' in _ui_plan_src, "周视图没有「未完成」标签"
    assert "今天已完成" in _ui_plan_src, "今日卡片没有「今天已完成」分支"
    assert "今天练过" in _ui_plan_src and '>计划外</span>' in _ui_plan_src, \
        "没有把「练过」和「完成课表」分开，也没有计划外标签"
    assert "本周完成" in _ui_plan_src, "计划页没有统计本周完成情况"
    # 计划接口读缓存时也要按本地库重算：同步之后「今天练没练」会变
    _aa_plan_src = (ROOT / "garmin_cn/appapi.py").read_text(encoding="utf-8")
    assert "annotate(data, days, today, evidence)" in _aa_plan_src, \
        "计划接口直接用缓存里的完成度，同步后不会更新"
    assert PL._months("2026-11-07")[0] == (2026, 10), "要连上个月一起拉，兼容月份语义"

    # 计划层：来源必须按天标注，冲突按优先级（manual > calendar > coach > raceplan > coros > generated）
    assert PL.source_rank("manual") < PL.source_rank("calendar") < PL.source_rank("coach")
    assert PL.source_rank("raceplan") < PL.source_rank("coros") < PL.source_rank("generated")
    _cal0 = PL.from_calendar([], today="2026-11-17")
    assert len(_cal0["week"]) == 7 and all(w.get("source") == "calendar" for w in _cal0["week"]), \
        "日历计划没有按天标来源"
    # 旧快照没有 source：load 时要补默认值，否则优先级合并会算错
    _snap2 = Path(tempfile.mkdtemp())
    _p2 = _snap2 / "plan.json"
    _p2.write_text(json.dumps({"classify": PL.CLASSIFY_REV, "today": {"date": "2026-11-17"},
                               "week": [{"date": "2026-11-17", "dow": "周二",
                                         "items": [], "rest": True}]}), encoding="utf-8")
    _l2 = PL.load(_p2)
    assert _l2 and _l2.get("source") == "calendar", "旧快照顶层缺 source 时没补默认值"
    assert _l2["week"][0].get("source") == "calendar", "旧快照每天缺 source 时没补默认值"
    shutil.rmtree(_snap2, ignore_errors=True)

    # ---------------- 计划写入层 ----------------
    # 本地改动写在 overrides 里，读的时候合并；来源快照永远保持原样，
    # 所以「改完计划 → 下次同步日历」不会把改动冲掉，改动也不会被落盘固化。
    from garmin_cn import contentguard as CG
    from garmin_cn import planedit as PE
    from garmin_cn import plangen as PG
    from garmin_cn import group as GG, groupui as GU
    from garmin_cn import runsui as RU
    from garmin_cn import planpush as PP

    _pdir = Path(tempfile.mkdtemp())
    _mon = "2026-11-16"
    _pw = [{"date": (dt.date.fromisoformat(_mon) + dt.timedelta(days=i)).isoformat(),
            "dow": PL.DOW[i + 1], "items": [], "rest": True, "source": "calendar"}
           for i in range(7)]
    _psnap = {"classify": PL.CLASSIFY_REV, "source": "calendar", "week": _pw,
              "today": {"date": "2026-11-17", "items": [], "rest": True},
              "name": "佳明日历"}
    _pp = _pdir / "plan.json"
    _pp.write_text(json.dumps(_psnap, ensure_ascii=False), encoding="utf-8")

    assert PL.load(_pp)["week"][2]["rest"] is True, "空周本来该是休息日"
    PE.save_day("2026-11-18", [{"km": 8, "kind": "easy"}], note="改小", path=_pdir)
    _merged = PL.load(_pp)
    assert _merged["week"][2]["items"][0]["km"] == 8, "写入的课没合并进读取结果"
    assert _merged["week"][2]["source"] == "manual", "改过的天没标成 manual"
    assert _merged["week"][2].get("edited") is True, "改过的天没有 edited 标记"
    # 关键：来源重新落盘后改动还在（否则「改完同步就打回原形」）
    _pp.write_text(json.dumps(_psnap, ensure_ascii=False), encoding="utf-8")
    assert PL.load(_pp)["week"][2]["items"][0]["km"] == 8, "重新同步把本地改动冲掉了"
    assert PL.load(_pp)["week"][3]["rest"] is True, "没改的天被误改了"
    # 课改了，旧的完成判定必须作废，不能还挂着「已完成」
    assert PL.load(_pp)["week"][2]["done"] is False, "改过的天还留着旧的完成判定"

    _rejected = 0
    for _bad in ([{"km": -1}], [{"km": 99}], [{"km": 5, "kind": "nope"}], [{}]):
        try:
            PE.clean_day(_bad)
        except ValueError:
            _rejected += 1
    assert _rejected == 4, f"非法课次没被拦住：只拦下 {_rejected}/4"

    # 周目标只算跑步类：骑行 20 km 不能顶成跑步的格
    PE.save_day("2026-11-19", [{"km": 20, "sport": "骑行", "sport_key": "cycling"}],
                path=_pdir)
    _tg = PL.week_target(PL.load(_pp), _mon)
    assert abs(_tg["km"] - 8) < 0.01, f"周目标把骑行算进来了：{_tg['km']}"
    assert _tg["available"] is True and _tg["days"] == 1, "周目标的可用天数算错"
    PE.clear_day("2026-11-18", path=_pdir)
    PE.clear_day("2026-11-19", path=_pdir)
    assert PL.week_target(PL.load(_pp), _mon)["available"] is False, \
        "撤销改动后周目标还留着数"
    shutil.rmtree(_pdir, ignore_errors=True)

    # 日历排课只有标题、没有结构化里程：必须从课名里读，否则周目标永远是 0
    assert PL._km_in_title("W2 周二 10km@E") == 10, "课名里的 km 没读出来"
    assert PL._km_in_title("5公里") == 5, "中文「公里」没读出来"
    assert PL._km_in_title("W2 周四 St24×3") == 0, "楼梯 24 趟被读成了 24 km"
    assert PL._km_in_title("2026 秦岭山地马拉松 · 43K") == 43, "赛事名里的 43K 没读出来"
    _kdir = Path(tempfile.mkdtemp())
    _kp = _kdir / "plan.json"
    _kp.write_text(json.dumps({
        "classify": PL.CLASSIFY_REV, "source": "calendar",
        "today": {"date": "2026-11-17", "items": []},
        "week": [{"date": "2026-11-17", "dow": "周二", "rest": False,
                  "items": [{"name": "W2 周二 10km@E", "sport_key": "running"}]}],
    }, ensure_ascii=False), encoding="utf-8")
    _kl = PL.load(_kp)
    assert _kl["week"][0]["items"][0]["km"] == 10, "旧快照课名里的里程没补上"
    assert PL.week_target(_kl)["km"] == 10, "补了里程但周目标还是算不出来"
    shutil.rmtree(_kdir, ignore_errors=True)

    # 自建计划也要能被完成判定认出来：否则「改了计划」之后，完成率永远是 0
    _adir = Path(tempfile.mkdtemp())
    _ap = _adir / "plan.json"
    _ap.write_text(json.dumps({
        "classify": PL.CLASSIFY_REV, "source": "calendar",
        "today": {"date": "2026-11-17", "items": []},
        "week": [{"date": "2026-11-18", "dow": "周三", "rest": False, "source": "manual",
                  "items": [{"name": "轻松跑", "km": 8, "sport_key": "running"}]}],
    }, ensure_ascii=False), encoding="utf-8")
    _ok = PL.annotate(PL.load(_ap), {"2026-11-18"}, "2026-11-17",
                      [{"date": "2026-11-18", "type": "running", "distance_m": 8200}])
    assert _ok["week"][0]["done"] is True, "跑够了却没被判为完成"
    _short = PL.annotate(PL.load(_ap), {"2026-11-18"}, "2026-11-17",
                         [{"date": "2026-11-18", "type": "running", "distance_m": 5000}])
    assert _short["week"][0]["done"] is False, "只跑了 60% 也被判为完成"
    shutil.rmtree(_adir, ignore_errors=True)

    # ---------------- 计划编辑页与写接口 ----------------
    from garmin_cn.serve import _plan_days_from_form

    _form = {
        "d0_date": "2026-11-16", "d0_kind": "rest", "d0_km": "", "d0_minutes": "",
        "d1_date": "2026-11-17", "d1_kind": "easy", "d1_km": "8", "d1_minutes": "",
        "d2_date": "2026-11-18", "d2_kind": "strength", "d2_km": "0", "d2_minutes": "45",
        "d3_date": "2026-11-19", "d3_skip": "1", "d3_kind": "easy", "d3_km": "99",
        "d4_date": "2026-11-20", "d4_kind": "easy", "d4_km": "999", "d4_minutes": "",
    }
    _days = _plan_days_from_form(_form)
    assert len(_days) == 4, f"表单解析丢了天：出了 {len(_days)} 天"
    assert _days[0]["rest"] is True, "空数字没有当成休息"
    assert _days[1]["items"][0]["km"] == 8, "里程没解析对"
    assert _days[2]["items"][0]["minutes"] == 45 and _days[2]["items"][0]["km"] == 0, \
        "力量课应该只带分钟、不带里程"
    assert all(d["date"] != "2026-11-19" for d in _days), \
        "标了 skip 的那天仍然被写进去了（会覆盖掉同一天的另一条课）"
    assert _days[3]["rest"] is True, "离谱里程 999 没有被挡成休息"

    # kind 推断：日历课只有标题，不推断的话力量课会显示成轻松跑
    assert PE.guess_kind({"name": "力量B 核心", "sport_key": "strength_training"}) == "strength"
    assert PE.guess_kind({"name": "W2 周三 10km@E", "km": 10}) == "easy"
    assert PE.guess_kind({"name": "周六 18km@E", "km": 18}) == "long"

    # 页面渲染：拿造的数据跑，不碰真实库
    _fake = {"ok": True, "monday": "2026-11-16", "stale": False, "week_km": 42,
             "days": [{"date": f"2026-11-{16 + i:02d}", "dow": PL.DOW[i + 1],
                       "past": i == 0, "rest": False, "edited": False,
                       "source": "calendar", "note": "", "km": 8,
                       "items": [{"name": "轻松跑", "kind": "easy", "label": "轻松跑",
                                  "km": 8, "minutes": 0, "detail": ""}]} for i in range(7)]}
    _html = U.plan_edit_page("TOK123", data=_fake,
                             rules={"race_deload": True, "auto": False})
    assert "TOK123" in _html, "编辑页没带页面令牌，表单会被 CSRF 保护挡回来"
    assert _html.count('name="d') >= 7, "编辑页没渲染出七天的字段"
    assert 'action="/__plan/save"' in _html, "编辑页没有保存表单"
    assert 'name="race_deload"' in _html, "编辑页没有「按赛事减量」开关"
    assert "日期已过" in _html, "过去的日子没有说明，改动会被静默丢掉"

    _draft = {"ok": True, "monday": "2026-11-16", "week_km": 30, "days": 3,
              "factor": 0.7, "reason": ["按历史跑量", "赛前减量"],
              "week": [{"date": f"2026-11-{16 + i:02d}", "dow": PL.DOW[i + 1],
                        "items": ([{"name": "轻松跑", "km": 8, "minutes": 45,
                                    "detail": "能说话"}] if i % 2 == 0 else []),
                        "rest": i % 2 != 0} for i in range(7)]}
    _dh = U.plan_draft_page("TOK456", draft=_draft)
    assert "TOK456" in _dh and "为什么这么排" in _dh and "用这份计划" in _dh, \
        "草稿页缺令牌、理由或确认按钮"

    # 推送入口按数据源渲染：只连高驰的人不该看到「推到佳明手表」
    from garmin_cn import appapi as _API2
    _ds = _API2.data_source()
    assert set(_ds) >= {"ok", "garmin", "coros", "coros_import", "any"}, \
        f"data_source 字段不全：{sorted(_ds)}"
    _src_g = {"garmin": True, "coros": False, "coros_import": False}
    _src_c = {"garmin": False, "coros": True, "coros_import": False}
    _src_n = {"garmin": False, "coros": False, "coros_import": False}
    _hg = U.plan_edit_page("TOK", data=_fake, rules={"race_deload": True}, src=_src_g)
    _hc = U.plan_edit_page("TOK", data=_fake, rules={"race_deload": True}, src=_src_c)
    _hn = U.plan_edit_page("TOK", data=_fake, rules={"race_deload": True}, src=_src_n)
    assert "推到佳明手表" in _hg and "导入到高驰" not in _hg, \
        "只连佳明却出现了高驰导入入口"
    assert "导入到高驰" in _hc and "推到佳明手表" not in _hc, \
        "只连高驰却出现了推到佳明的按钮——高驰用户看到会很困惑"
    assert "佳明" not in _hc, "高驰用户视角的编辑页里还提到了佳明"
    assert "去「我的」连接账号" in _hn, "一个平台都没连时没有引导"

    # 服务端路由与令牌
    _sv = (ROOT / "garmin_cn/serve.py").read_text(encoding="utf-8")
    assert 'startswith("/__plan/")' in _sv, "计划写接口没接路由"
    assert "plan_token" in _sv, "编辑页没有页面令牌保护（本机其它网页也能改计划）"

    # 赛前减量开关
    _rr = Path(tempfile.mkdtemp()) / "plan_rules.json"
    PG.set_rules(path=_rr, race_deload=False)
    _off = PG.build_draft(None, today="2026-11-17", monday=_mon, run_days=3,
                          week_km=30, save=False, rules_path=_rr)
    PG.set_rules(path=_rr, race_deload=True)
    _on = PG.build_draft(None, today="2026-11-17", monday=_mon, run_days=3,
                         week_km=30, save=False, rules_path=_rr)
    assert _off["week_km"] >= _on["week_km"], \
        f"关掉赛前减量后周量反而更小：关 {_off['week_km']} / 开 {_on['week_km']}"
    assert PG.rules(_rr)["race_deload"] is True, "开关没落到规则文件里"

    # 放弃草稿
    _dd = Path(tempfile.mkdtemp()) / "plan_draft.json"
    PG.build_draft(None, today="2026-11-17", monday=_mon, run_days=3, week_km=20,
                   path=_dd)
    assert PG.load_draft(_dd) is not None, "草稿没落盘"
    PG.discard(_dd)
    assert PG.load_draft(_dd) is None, "放弃草稿后文件还在，下次确认会误用"
    shutil.rmtree(_dd.parent, ignore_errors=True)

    # ---------------- 训练小组 ----------------
    # 日汇总口径：只有跑步类算里程。骑行 40 km 不能进跑团榜，
    # 但「次数」要算全部运动——不然骑车通勤的人次数永远是 0。
    _gdir = Path(tempfile.mkdtemp())
    from garmin_cn.store import Store as _GStore

    # 必须把 HOME 指到空目录：build_days 会读 data/plan.json 判「当天有没有课」，
    # 用真实 HOME 会把本机的计划混进测试数据（第一次跑就是这么翻的）。
    _old_home = os.environ.get("GARMIN_HOME")
    os.environ["GARMIN_HOME"] = str(_gdir)
    _gs = _GStore(_gdir / "g.db")
    try:
        for _d, _t, _m, _sec in (
                ("2026-11-16", "running", 8000, 2700),
                ("2026-11-16", "cycling", 40000, 5400),
                ("2026-11-17", "trail_running", 12000, 4800),
                ("2026-11-18", "strength_training", 0, 2700)):
            _gs.conn.execute(
                "INSERT INTO activities(activity_id,date,type,distance_m,duration_s)"
                " VALUES(?,?,?,?,?)", (f"{_d}-{_t}", _d, _t, _m, _sec))
        _gs.conn.commit()
        _days = {d["date"]: d for d in GG.build_days(_gs, "2026-11-18", 7)}
        assert _days["2026-11-16"]["distance_m"] == 8000, \
            f"骑行混进了里程：{_days['2026-11-16']}"
        assert _days["2026-11-16"]["acts"] == 2, "次数没算全部运动"
        assert _days["2026-11-16"]["duration_s"] == 2700 + 5400, "时长应含全部运动"
        assert _days["2026-11-17"]["distance_m"] == 12000, "越野跑应计入里程"
        assert _days["2026-11-18"]["distance_m"] == 0, "力量课不该有里程"
        # 没有活动的日子不凭空出现（不能把「没数据」当成「跑了 0」）
        assert "2026-11-15" not in _days, "没活动的日期不该出现在上报里"
        assert _days["2026-11-16"]["plan_done"] is None, \
            "没有计划的日子 plan_done 应为 None，不能伪造成 False"

        # 队列：同组只留最新一批；退避到上限后不再往后推
        _box = GG.Outbox(_gs)
        _box.put(3, {"days": [{"date": "2026-11-16"}]})
        _box.put(3, {"days": [{"date": "2026-11-16"}, {"date": "2026-11-17"}]})
        _rows = _box.pending(now=0)
        assert len(_rows) == 1, f"同组应只有一批，实际 {len(_rows)}"
        assert '"2026-11-17"' in _rows[0]["payload"], "队列没被最新一批覆盖"
        assert _box.bump(3, 1) > 0, "第一次失败应退避"
        assert _box.bump(3, GG.MAX_TRIES) == 0, "超过上限后应立刻可重试"
        _box.drop(3)
        assert not _box.pending(now=0), "清空后队列还有残留"

        # ---------------- 后台补传 ----------------
        # 三个出口全部替换掉（拉组列表 / 上报 / 拉详情），只看调度与记账：
        # 什么时候跑、跑几次、失败了记不记时间。这一步不碰网络。
        from garmin_cn import account as _ACC
        from garmin_cn import bg as BGM

        _bd = Path(tempfile.mkdtemp())
        _bs = _GStore(_bd / "bg.db")
        _calls: list[int] = []
        _real = (GG.groups, GG.report_group, GG.detail, GG.news, _ACC.logged_in)
        try:
            GG.groups = lambda: {"ok": True, "groups": [{"id": 1}, {"id": 2}]}
            GG.report_group = lambda store, gid, today=None, first=False, client=None: (
                _calls.append(int(gid)) or {"ok": True})
            GG.detail = lambda gid: {"ok": True, "me": {"map_share": True}}

            _ACC.logged_in = lambda: False
            assert BGM.catch_up(store=_bs, force=True).get("skip") == "no_account", \
                "没登录也在跑补传"
            assert not _calls, "没登录还去上报了"

            _ACC.logged_in = lambda: True
            _r = BGM.catch_up(store=_bs, today="2026-11-18", force=True)
            assert _r.get("ok") and _r.get("sent") == 2, f"两个组没都报：{_r}"
            assert sorted(_calls) == [1, 2], f"上报的组不对：{_calls}"
            assert _bs.get_meta(BGM.KEY_AT), "跑完没记时间"
            assert _bs.get_meta(BGM.KEY_RES) == "2/2", \
                f"结果没记账：{_bs.get_meta(BGM.KEY_RES)}"

            # 20 分钟内再触发（来回切前后台）应直接跳过，不能重复上报
            _calls.clear()
            assert BGM.catch_up(store=_bs, today="2026-11-18").get("skip") == "recent", \
                "节流没生效"
            assert not _calls, "节流期间仍然发了上报"

            # 拉不到组列表：可以失败，但不能把节流时间推后——
            # 否则用户重登后还得白等 20 分钟才轮到下一轮
            _bs.set_meta(BGM.KEY_AT, "2026-01-01T00:00:00")
            GG.groups = lambda: {"ok": False, "error": "token 过期"}
            _r3 = BGM.catch_up(store=_bs, today="2026-11-18", force=True)
            assert not _r3.get("ok"), "拉不到组列表却当成成功"
            assert _bs.get_meta(BGM.KEY_AT) == "2026-01-01T00:00:00", \
                "失败也把节流推后了"

            # 没记过开关的组补一次 detail；明确关着（"0"）的不要打扰，
            # 更不能被后台任务改成开
            GG.groups = lambda: {"ok": True, "groups": [{"id": 1}, {"id": 2}]}
            # 前面几轮已经把 group 1 的开关写进本地了，这里回到「从没进过页面」
            with _bs.conn:
                _bs.conn.execute("DELETE FROM meta WHERE key LIKE 'group_map_%'")
            _bs.set_meta("group_map_2", "0")
            _bs.set_meta(BGM.KEY_AT, "2026-01-01T00:00:00")
            _asked: list[int] = []

            def _det(gid):
                _asked.append(int(gid))
                return {"ok": True, "me": {"map_share": True}}

            GG.detail = _det
            BGM.catch_up(store=_bs, today="2026-11-18", force=True)
            assert _asked == [1], f"只该问没有缓存的那个组，实际问了 {_asked}"
            assert _bs.get_meta("group_map_1") == "1", "补回来的开关没落本地"
            assert _bs.get_meta("group_map_2") == "0", "明确关着的开关被改成了开"

            # 同一时刻只跑一轮：周期任务与前台 nudge 撞上时后者直接放弃
            BGM._LOCK.acquire()
            try:
                assert BGM.catch_up(store=_bs, force=True).get("skip") == "busy", \
                    "重入时没有挡住"
            finally:
                BGM._LOCK.release()

            # 通知游标：第一次只取起点，之后只回游标之后的新东西
            with _bs.conn:
                _bs.conn.execute("DELETE FROM meta WHERE key LIKE 'news_at_%'")
            _seq = {"n": 0}

            def _gnews(gid, since=0):
                _seq["n"] += 1
                _now = 1000 + _seq["n"]
                return {"ok": True, "now": _now, "since": since,
                        "comments": ([{"id": 1, "act_id": 1, "by": "张三", "at": 995,
                                       "on_my_act": True, "text": "今晚跑吗"}]
                                     if since else []),
                        "challenges": []}

            GG.news = _gnews
            _r = BGM.catch_up(store=_bs, today="2026-11-18", force=True)
            assert not (_r.get("news") or []), "第一次就补了一屏历史通知"
            assert _bs.get_meta("news_at_1") == "1001", \
                f"游标没落库：{_bs.get_meta('news_at_1')}"

            _r = BGM.catch_up(store=_bs, today="2026-11-18", force=True)
            _nw = _r.get("news") or []
            assert len(_nw) == 2 and {x["gid"] for x in _nw} == {1, 2}, \
                f"两个组各该有一条通知：{_nw}"
            assert _nw[0]["kind"] == "comment" and _nw[0]["gname"], \
                f"通知条目缺字段：{_nw[0]}"
            assert int(_bs.get_meta("news_at_1") or 0) > 1001, \
                "拉完没推游标，下次会重复提醒"

            # 通知拉不到不能把上报拖下水
            def _boom(gid, since=0):
                raise OSError("连接被重置")

            GG.news = _boom
            _r = BGM.catch_up(store=_bs, today="2026-11-18", force=True)
            assert _r.get("ok") and _r.get("sent") == 2, \
                f"通知异常影响了上报：{_r}"
        finally:
            (GG.groups, GG.report_group, GG.detail, GG.news,
             _ACC.logged_in) = _real
            _bs.conn.close()
            shutil.rmtree(_bd, ignore_errors=True)
    finally:
        _gs.conn.close()
        if _old_home is None:
            os.environ.pop("GARMIN_HOME", None)
        else:
            os.environ["GARMIN_HOME"] = _old_home
        shutil.rmtree(_gdir, ignore_errors=True)

    # 账号页的页脚：默认那句「不上传任何第三方」在小组功能上是假的
    _acc_html = GU.account_page("TOK123", "", "", "")
    assert "TOK123" in _acc_html, "账号页没带页面令牌"
    assert "只存在这台手机" in _acc_html, "账号页缺页脚说明"
    assert "不上传任何第三方" not in _acc_html, "账号页仍在说不上传任何第三方"
    _g_html = GU.groups_page("TOK123", "")
    assert "去登录" in _g_html or "还没有加入小组" in _g_html, "小组页未登录分支不对"

    # ---------------- 教练角色 ----------------
    # 页面按角色裁权限：教练能看记录、能建赛事，但不能审批（服务端也拦）。
    # 这里只钉住「页面上不该出现的入口」，接口层的 403 由 test_groups 守。
    _real_detail = GG.detail

    def _fake_detail(role: str, members: list):
        def _d(gid):
            return {"ok": True, "group": {
                "id": 1, "name": "测试队", "role": role, "member_count": len(members),
                "invite_code": "ABCD1234", "join_mode": "open", "pending": [],
                "me": {"alias": "我", "share": "summary"}, "members": members}}
        return _d

    _m3 = [{"user_id": 1, "alias": "我", "role": "owner", "is_me": True},
           {"user_id": 2, "alias": "小王", "role": "member"},
           {"user_id": 3, "alias": "老李", "role": "coach"}]
    try:
        GG.detail = _fake_detail("owner", _m3)
        _sp = GU.settings_page("TOK", 1, "")
        assert "/__group/1/role" in _sp, "组长看不到「设教练」入口"
        assert "设教练" in _sp and "撤销" in _sp, "教练管理入口缺文案"
        assert "操作记录" in _sp, "组长看不到操作记录"

        GG.detail = _fake_detail("coach", _m3)
        _sp2 = GU.settings_page("TOK", 1, "")
        assert "操作记录" in _sp2, "教练看不到操作记录（该能看到）"
        assert "/__group/1/decide" not in _sp2, "教练看到审批入口，点进去必然 403"
        assert "撤销" not in _sp2, "教练能任命/撤销教练，越权了"
    finally:
        GG.detail = _real_detail

    # Store 只有 conn，没有 close()。写成 store.close() 会抛 AttributeError，
    # 而且两处都**有兜底**，看起来只是「设置没生效」，很难联想到是这个。
    # 跳过自己：下面这两行字符串本身就会被扫到
    _gfiles = {f.name: f.read_text(encoding="utf-8")
               for f in (ROOT / "garmin_cn").glob("*.py") if f.name != "selftest.py"}
    _badclose = [n for n, t in _gfiles.items()
                 if "_st.close()" in t or "store.close()" in t]
    assert not _badclose, f"Store 没有 close()，应写 .conn.close()：{_badclose}"

    # ---------------- 路线分享（单条折线） ----------------
    # 投影必须和足迹页同一套，否则同一片路网上「格子在这、线在那」。
    _rpts = [[31.20 + i * 0.0001, 121.50 + i * 0.0001] for i in range(50)]
    _rsvg = GU._route_svg(_rpts, None)
    assert _rsvg.count("L") == 49, f"折线点数不对：{[_rsvg.count('L')]}"
    assert "stroke-width" in _rsvg, "折线没描边，深底上会看不清"
    _rxs = [float(x) for x in re.findall(r"[ML]([\d.]+) ", _rsvg)]
    _rys = [float(y) for y in re.findall(r"[ML][\d.]+ ([\d.]+)", _rsvg)]
    assert min(_rxs) >= 0 and max(_rxs) <= GU.MAP_W, "折线 x 超出画幅"
    assert min(_rys) >= 0 and max(_rys) <= GU.MAP_H, "折线 y 超出画幅"
    assert GU._route_svg([[31.2, 121.5], [31.21, 121.51]], None).count("L") == 1, \
        "两个点也该画出一段"

    # ---------------- 综合榜：骑行单独记 ride_m ----------------
    # `distance_m` 只装跑步；骑行进 `ride_m`，综合榜才按 4:1 折算。
    # 两列不重叠是这套折算成立的前提——混在一起的话「骑行 4 km 算 1 km」
    # 会把已经算过的跑步再折一次。
    # 用独立临时库，不动上面那些基于主库的断言基线。
    import tempfile as _tf2

    from garmin_cn.demo import build as _bdemo

    _rp = Path(_tf2.mkdtemp()) / "ride.db"
    _bdemo(_rp, days=6, seed=3)
    _rs2 = Store(_rp)
    try:
        with _rs2.conn:
            _rs2.conn.execute(
                "INSERT INTO activities(activity_id,date,type,distance_m,duration_s,ascent)"
                " VALUES(?,?,?,?,?,?)",
                ("ride-selftest", today.isoformat(), "cycling", 40000, 6000, 0))
        _bd1 = {x["date"]: x for x in GG.build_days(_rs2, today.isoformat(), days=1)}
        _cell = _bd1.get(today.isoformat()) or {}
        assert _cell.get("ride_m", 0) >= 40000, f"骑行没进 ride_m：{_cell}"
        assert _cell.get("distance_m", 0) < 40000, "骑行被算进了跑步里程"
    finally:
        _rs2.conn.close()

    # onclick 属性必须整体转义。`json.dumps("a")` 产生带双引号的 "a"，
    # 而属性本身是双引号包裹 —— 直接拼会把属性提前闭合，浏览器只拿到
    # `rcPick(0,`，点击时语法错误、一行 JS 都不执行，也不发任何请求。
    # 症状是「所有按钮点了没反应」且没有任何报错，比参数缺失更彻底。
    from html import unescape as _unesc

    from garmin_cn import runsui as _RS

    _ev = {"id": "e1", "title": "岔路", "options": [
        {"id": "a", "name": "翻塌方口", "cost": 40},
        {"id": "b", "name": "走羊道", "cost": 0}]}
    _ons = re.findall(r'onclick="([^"]*)"', _RS.event_card(_ev, 0))
    assert len(_ons) >= 2, "岔路选项没有 onclick"
    for _a in _ons:
        _js = _unesc(_a)
        assert _js.strip().startswith("rcPick("), f"onclick 被截断了：{_a!r}"
        assert _js.rstrip().endswith("return false"), f"onclick 不完整：{_a!r}"

    # 岔路必须走 rcFork。/run/fork 只收 option，/run/event 要 slot；
    # 岔路卡片渲染时 slot 是 -1，按普通事件发会永远 404「没有这条事件」。
    _fk = {"id": "f1", "type": "story", "kind": "fork", "title": "岔路",
           "options": [{"id": "a", "label": "翻塌方口"},
                       {"id": "b", "label": "走羊道"}]}
    _fons = [_unesc(x) for x in
             re.findall(r'onclick="([^"]*)"', _RS.event_card(_fk, -1))]
    assert _fons, "岔路卡片没有选项按钮"
    assert all("rcFork(" in x for x in _fons), f"岔路按钮应该调 rcFork：{_fons}"

    # 远征页所有写操作都必须带 gid。漏了服务端永远收到 0，任何按钮都会
    # 静默失败——而前端失败也是 reload，用户只看到页面闪一下。
    _rsrc = (ROOT / "garmin_cn/runsui.py").read_text(encoding="utf-8")
    assert "var GID=" in _rsrc, "远征页没把 gid 传给前端"
    assert "f.append('gid'" in _rsrc, "远征的 post() 没提交 gid"
    assert _rsrc.count('name="gid"') >= 2, "远征的表单缺 gid（目标和营地升级）"
    # 请求体格式必须和服务端解析器一致。服务端 `_run_post` 用 parse_qs，
    # 只认 urlencoded；裸 FormData 会发 multipart，parse_qs 解析出垃圾键，
    # token 读不到 → 一律「页面已过期」→ 所有按钮静默失效（连云端请求都没有）。
    assert "new FormData()" not in _rsrc, \
        "远征页用 FormData 发 body 会变成 multipart，服务端 parse_qs 读不到 token"
    assert "URLSearchParams" in _rsrc, "远征页没发 urlencoded"
    # GET 必须有最外层兜底。没有的话任何渲染异常都会让连接**空响应**，
    # WebView 收到 ERR_EMPTY_RESPONSE，用户只看到一片黑，连哪页错了都不知道
    # （月榜黑屏那次就是这样，云端一条请求都没有，因为崩在发请求之前）。
    _gserv = (ROOT / "garmin_cn/serve.py").read_text(encoding="utf-8")
    assert "def _do_GET" in _gserv and "_error_page" in _gserv, \
        "GET 缺最外层异常兜底：页面崩了会变成空响应（黑屏）"

    # 路线批注：作者能加（可带照片），其他人只能看。照片必须在前端压过，
    # 不压的话手机原图 4 MB 直接上行，一条批注要传十几秒。
    _rpg = (ROOT / "garmin_cn/groupui.py").read_text(encoding="utf-8")
    assert "_notes_block" in _rpg and "rt-pin" in _rpg, "路线页没有批注钉子"
    assert 'id="rt-add"' in _rpg, "作者看不到加批注入口"
    assert "toDataURL" in _rpg, "照片没在前端压缩"
    assert "/__route/note" in _gserv, "批注写接口缺失"
    assert "note_image" in _gserv, "批注照片没有本地代理"
    # 首页要自动拉一次佳明云：onResume 只补传本地，后台只 catch_up，
    # 两者都不拉佳明——不补这一步，「跑完打开 App 看不到新活动」永远无解。
    assert '"/__sync/today"' in _gserv and "_auto_sync" in _gserv, \
        "首页没有自动同步入口"
    assert "AUTO_SYNC_GAP" in _gserv, "自动同步没有节流"
    assert "_AUTO_SYNC_JS" in (ROOT / "garmin_cn/ui.py").read_text(encoding="utf-8"), \
        "首页没挂自动同步脚本"
    # 自动同步要覆盖所有配过的源（佳明/高驰/Keep），不能只拉佳明——
    # 否则高驰和 Keep 用户永远得手动点。
    assert "def sync_all(" in (ROOT / "garmin_cn/appapi.py").read_text(encoding="utf-8"), \
        "缺少统一的 sync_all 入口"
    assert "A.sync_all" in _gserv, "首页自动同步仍只拉佳明"
    assert "sync_all" in (ROOT / "garmin_cn/bg.py").read_text(encoding="utf-8"), \
        "后台同步仍只拉佳明"

    # 自动同步这次报障的四条，全部来自「失败被当成成功」和「节流互相打架」：
    # 1) sync() 失败是**返回** ok=False，不抛异常。sync_all 只看有没有抛，
    #    于是会话过期被记成「已同步、新增 0」，前端收到就静默什么都不做——
    #    用户看到的永远是「打开软件不自动更新」。必须显式查返回值。
    _aps = (ROOT / "garmin_cn/appapi.py").read_text(encoding="utf-8")
    assert 'r.get("ok") is False' in _aps, "sync_all 没检查 sync 的返回值"
    assert '"need_login"' in _aps and '"failed"' in _aps, "sync_all 没把失败原因带出来"
    # 自动同步必须能拿到 Keystore 里的账密去静默续期，否则佳明 JWT 一过期
    # 就只能弹「连接已过期」，而手动下拉却能成功——用户看到的就是自相矛盾。
    assert "_native_creds" in _aps and "com.chaquo.python.Python" in _aps, \
        "自动同步没从 Android Keystore 取账密，过期只能反复提示"
    # Keep 的判据必须是「本地真有登录」，不能靠 last_sync_keep 反推：
    # 从没登录过 Keep 的人不该被提示「Keep 登录已失效」。
    assert 'keep_status().get("logged_in")' in _aps, \
        "Keep 还会被 last_sync_keep 骗去同步，没登录也提示失效"
    assert "login_nag" in _gserv, \
        "连接过期提示没有节流，每次打开都会弹"
    assert "关闭提示" in (ROOT / "garmin_cn/ui.py").read_text(encoding="utf-8"), \
        "连接过期提示不能收起"

    # Keystore 取账密的胶水层：桌面没有 java 模块时必须安静退回 {}，
    # Android 上必须把三个源的账密都读出来（读不出就退回只靠 token）。
    # 用假 java 模块跑一遍，守住「导入路径 / 方法名」不被改名改坏。
    import types as _types

    from garmin_cn import appapi as _crapi

    class _FakeSecrets:
        email = lambda self: "e@example.com"          # noqa: E731
        password = lambda self: "pw"                   # noqa: E731
        corosEmail = lambda self: "c@example.com"      # noqa: E731
        corosPassword = lambda self: "cp"              # noqa: E731
        keepMobile = lambda self: None                 # noqa: E731
        keepPassword = lambda self: None               # noqa: E731

    class _FakePy:
        @staticmethod
        def getPlatform():
            return _FakePy

        @staticmethod
        def getApplication():
            return object()

    def _fake_jclass(name):
        if name == "com.chaquo.python.Python":
            return _FakePy
        return lambda ctx: _FakeSecrets()

    _fake_java = _types.ModuleType("java")
    _fake_java.jclass = _fake_jclass
    _had_java = sys.modules.get("java")
    sys.modules["java"] = _fake_java
    try:
        _cr = _crapi._native_creds()
    finally:
        if _had_java is None:
            sys.modules.pop("java", None)
        else:
            sys.modules["java"] = _had_java
    assert _cr["garmin"] == ("e@example.com", "pw"), f"佳明账密没读出来：{_cr}"
    assert _cr["coros"] == ("c@example.com", "cp"), f"高驰账密没读出来：{_cr}"
    assert _cr["keep"] == (None, None), f"Keep 没登录却读出了账密：{_cr}"
    # 2) 后台与首页不能共用一个节流戳：后台每 30 分钟刷新它，首页每次打开都
    #    落在窗口里被挡掉。
    _bgs = (ROOT / "garmin_cn/bg.py").read_text(encoding="utf-8")
    assert "BG_SYNC_META" in _bgs and "bg_sync_at" in _bgs, \
        "后台和首页共用 auto_sync_at，首页自动同步会被后台一直挡掉"
    # 3) 会话过期必须让用户看见，否则「没更新」永远没有解释。
    assert "need_login" in _gserv, "首页自动同步没透传 need_login"
    assert "连接已过期" in (ROOT / "garmin_cn/ui.py").read_text(encoding="utf-8"), \
        "会话过期时首页不给提示，用户只能看到「没更新」"
    # 自己分享的那条必须能点进详情：批注入口在详情页，清单行若只是 div + 撤回
    # 按钮，作者反而进不去自己的路线（实测踩到：用户以为「没有加批注按钮」）。
    assert '/groups/{gid}/route/{int(x.get("id") or 0)}' in _rpg, \
        "自己分享的路线点不进详情"
    # 有照片的批注要用缩略图当钉子（用户明确要「小图」而不是编号圆点）；
    # 选位置要吸附到轨迹上，不然钉子会飘在路外。
    assert 'rt-pin isimg' in _rpg, "有照片的批注没用缩略图当钉子"
    assert "data-line=" in _rpg, "轨迹坐标没注入，吸附无从谈起"
    assert "function snap(" in _rpg, "批注位置没做吸附"
    # GPX 导出：WebView 不处理 Content-Disposition，必须交给系统浏览器
    assert "rcGpx" in _rpg and "openUrl:" in _rpg, "GPX 导出仍走 <a href>，点了没反应"
    # 批注界面统一到设计语言：按钮都是 .btn.sm（primary 只给主操作），
    # 原生 file 框藏起来用 label 冒充按钮，导出和加批注排在同一行。
    assert 'class="pager rt-bar"' in _rpg, "导出/加批注没有排在同一行"
    assert 'class="rt-file" type="file"' in _rpg, "原生文件框没藏，会和胶囊按钮打架"
    assert '<label class="btn sm" for="rt-file"' in _rpg, "选照片没用按钮样式"
    assert 'class="ghost"' not in _rpg, "批注区还有风格外的 ghost 按钮"

    # 路线要能导出 GPX：组内共享的坐标只有这一个出口（以前只能看不能带走）
    assert "_gpx_xml" in _gserv and '"route"' in _gserv and ".gpx" in _gserv, \
        "共享路线没有 GPX 导出"

    _ssrc = (ROOT / "garmin_cn/serve.py").read_text(encoding="utf-8")
    _rblk = _ssrc[_ssrc.index("def _run_post"):]
    _rblk = _rblk[:_rblk.index("def _see_other")]
    assert (_rblk.index('gid = int(vals.get("gid")')
            < _rblk.index("compare_digest")), \
        "gid 要在令牌校验前解析，否则令牌过期时会被踢去组列表"

    # ---------------- 远征界面 ----------------
    # 小旗必须落在**当前幕那一段**里。整季累计进度在队伍超额时会超过 100%，
    # 如果拿它定位小旗，第二幕就会顶到最右端，看起来像这一季已经走完了。
    _xs = [26 + (346 - 26) * i / 4 for i in range(5)]
    _svg = RU.route_svg(["甲", "乙", "丙", "丁", "戊"], act=2, stage_frac=0.5)
    _cx = float(re.search(r'<circle cx="([\d.]+)" cy="[\d.]+" r="5\.6"', _svg).group(1))
    assert abs(_cx - (_xs[1] + _xs[2]) / 2) < 1.5, \
        f"第二幕的小旗没落在第二段中间：cx={_cx}，应为 {(_xs[1] + _xs[2]) / 2}"
    assert _cx < _xs[3], "第二幕的小旗跑到了后面几段"
    # 第一幕在最左段，第四幕满进度才到最右
    _svg1 = RU.route_svg(["甲", "乙", "丙", "丁", "戊"], act=1, stage_frac=0.0)
    assert abs(float(re.search(r'<circle cx="([\d.]+)" cy="[\d.]+" r="5\.6"', _svg1).group(1))
               - _xs[0]) < 1.5, "第一幕的小旗没在第一段起点"
    _svg4 = RU.route_svg(["甲", "乙", "丙", "丁", "戊"], act=4, stage_frac=1.0)
    assert abs(float(re.search(r'<circle cx="([\d.]+)" cy="[\d.]+" r="5\.6"', _svg4).group(1))
               - _xs[4]) < 1.5, "第四幕满进度没到终点"
    # 超额进度要夹住，不能画到画布外
    _svgx = RU.route_svg(["甲", "乙", "丙", "丁", "戊"], act=4, stage_frac=3.0)
    assert '<circle cx="346.0"' in _svgx, "进度超额时小旗没夹在终点"

    # 地名缩写：五个站在 420 宽里排不下，五字的必然压住邻居
    assert RU._short("采药人小屋") == "小屋", "五字地名没有缩写"
    assert RU._short("老鹰嘴") == "老鹰嘴", "三字地名不该缩写"
    assert RU._short("冰河") == "冰河", "两字地名不该缩写"

    # 事件卡：试炼没有 options，必须渲染成两个按钮，而不是一张空卡
    _trial = {"id": "x_trial", "type": "trial", "act": 1, "title": "渐加速五公里",
              "text": "把烟袋收进怀里。", "quality": {"metric": "negative_split",
                                                    "distance_km": 5},
              "options": [], "chosen": "", "result": ""}
    _th = RU.event_card(_trial, 0)
    assert "跑完了" in _th and "没跑成" in _th, "试炼没渲染出上报按钮"
    assert "渐加速" in _th, "试炼没显示要跑什么"
    _trial2 = dict(_trial, chosen="success", result="最后两公里，谁也没掉队。")
    _th2 = RU.event_card(_trial2, 0)
    assert "跑完了" not in _th2 and "没掉队" in _th2, "已报的试炼还在催人跑"

    # 试炼是「数据说了算」不是「自己说了算」：点之前就要说清楚会去核，
    # 点完之后要能看出这次是核过、没核过、还是没跑成。三种说法不能混。
    assert "核一遍" in _th and "只算一半" in _th, "试炼没说明会按记录核验"
    assert "核过" not in _th, "还没报就先把结论写出来了"
    _tv = RU.event_card(dict(_trial, chosen="ok", level="verified", result="对得上"), 0)
    _tu = RU.event_card(dict(_trial, chosen="ok", level="unverified", result=""), 0)
    _tfa = RU.event_card(dict(_trial, chosen="fail", level="failed", result=""), 0)
    assert "核验通过" in _tv and "只算一半" not in _tv, "核验通过的卡没标出来"
    assert "未核验" in _tu and "只算一半" in _tu, "未核验的卡没说明打折"
    assert "没跑成" in _tfa and "核验通过" not in _tfa, "没跑成的卡写成通过了"
    # 卡片的根节点必须还是 ecard：改样式时用了一个同名的局部变量，
    # 结果 class 被覆盖成 "verified"，整张卡掉出面板变成裸文字。
    for _x in (_th, _tv, _tu, _tfa):
        assert _x.startswith('<div class="ecard'), "试炼卡丢了 ecard 外壳（会变成裸文字）"

    # 证据只抽原始分圈（距离/用时/步频/心率/爬升），绝不带判定结论。
    # 带结论等于没核验——客户端改一行就全过了。
    from garmin_cn.store import Store as _St
    _evd = Path(tempfile.mkdtemp())
    _st = _St(_evd / "s.db")
    _st.conn.execute("INSERT INTO activities(activity_id,date,type,distance_m,duration_s)"
                     " VALUES(?,?,?,?,?)", ("a1", "2026-11-17", "running", 6000, 1800))
    _st.conn.execute("INSERT INTO activities(activity_id,date,type,distance_m,duration_s)"
                     " VALUES(?,?,?,?,?)", ("a2", "2026-11-17", "cycling", 30000, 3600))
    _st.save_detail("a1", {"laps": [{"dist_m": 1000, "dur_s": 300, "cad": 178,
                                     "hr_avg": 150, "ele_gain": 10}] * 6})
    _e = GG.trial_evidence(_st, {"metric": "even_pace", "distance_km": 6},
                           "2026-11-16", "2026-11-22")
    assert len(_e.get("activities") or []) == 1, f"证据条数不对：{_e.keys()}"
    assert _e["activities"][0]["laps"][0]["dur_s"] == 300, "分圈用时没抽出来"
    assert "verdict" not in json.dumps(_e) and "level" not in json.dumps(_e), \
        "证据里混进了判定结论"
    # 骑车的里程不能算成跑的
    assert all(x["distance_km"] < 10 for x in _e["activities"]), "骑行被当成跑步交了"
    assert GG.trial_evidence(_st, {"distance_km": 6}, "2026-12-01", "2026-12-07") == {}, \
        "没有活动的周不该编出证据"
    _st.conn.close()
    shutil.rmtree(_evd, ignore_errors=True)

    # 空态：三种「打不开」要分开说。混成一句「远征还没开始」的话，
    # 玩家不知道该去点哪儿——三种情况要办的事完全不同。
    _e_login = RU.empty_page("login")
    _e_group = RU.empty_page("nogroup")
    _e_off = RU.empty_page("offline", "HTTP 500")
    assert "先知道你是谁" in _e_login and "/account" in _e_login, "未登录空态没有去账号页"
    assert "你还没有队" in _e_group and "/groups" in _e_group, "没组空态没有建组入口"
    assert "路还没开出来" in _e_off and "重试" in _e_off, "连不上空态没有重试"
    assert len({_e_login, _e_group, _e_off}) == 3, "三种空态渲染成了一样的"

    _fork = {"id": "x_fork", "type": "story", "kind": "fork", "act": 1,
             "title": "雪桥", "text": "溪面结了一层薄冰。", "chosen": "",
             "options": [{"id": "cross", "label": "直接从冰上过",
                          "brief": "快，冰要是薄就麻烦了", "time": 0},
                         {"id": "detour", "label": "绕到上游窄处",
                          "brief": "稳，多走一段", "time": 1}]}
    _fh = RU.event_card(_fork, -1)
    assert "这一刻要定" in _fh and "直接从冰上过" in _fh, "岔路卡缺标题或选项"
    assert "慢 1 天" in _fh, "带时间成本的选项没标出代价"

    # 数值绝不进页面：服务端不下发 effect，界面也不能自己写死哪个更划算。
    # 一旦页面上能看到「+3 格」这类数，选择就退化成算术题。
    assert "effect" not in _fh and "advance" not in _fh, "选项把数值写进了页面"

    # 结算页：四个结局共用一套版式，变的是文案与脚印的去向
    _oc = {"ok": True, "ended": True, "ending": "arrive",
           "ending_detail": {"key": "arrive", "title": "抵达",
                             "line": "五天，一炉火", "text": "（正文第一句）。"},
           "team": 52.0, "morale": 7, "members": 3, "relic_count": 1,
           "acts": [{"act": 1, "theme": "信任", "got": 13.0, "target": 12.0, "hit": True},
                    {"act": 2, "theme": "代价", "got": 11.0, "target": 12.0, "hit": False},
                    {"act": 3, "theme": "取舍", "got": 15.8, "target": 12.0, "hit": True},
                    {"act": 4, "theme": "选择", "got": 15.8, "target": 12.0, "hit": True}]}
    _oh = RU.outcome_page("TOK", _oc)
    assert "抵达" in _oh and "（正文第一句）" in _oh, "结算页缺结局标题或正文"
    assert _oh.count("走够") == 3 and _oh.count("没够") == 1, \
        "四段的「走够/没够」和 got/target 对不上"
    assert "0/0" not in _oh, "没结算的段被显示成了 0/0"

    # 没结算过的幕要显示「没走到」，不能显示成「0/0 没够」——
    # 那是「没走到」，不是「这一周没走够」，两者对玩家是两件事
    _oc2 = dict(_oc, acts=[{"act": 1, "theme": "信任", "got": 13.0,
                            "target": 12.0, "hit": True},
                           {"act": 2, "theme": "代价", "got": 0.0,
                            "target": 0.0, "hit": False}])
    _oh2 = RU.outcome_page("TOK", _oc2)
    assert "没走到" in _oh2 and "0/0" not in _oh2, "未结算的幕没显示成「没走到」"

    # 结算页的主角是「谁走了多少」：局内数值（士气/旧日志）只能出现在页脚
    # 那一行小字里。这一条挡的是「以后又把它加回四个大数字」。
    _oc3 = dict(_oc, km=372.0, hidden=1)
    _oc3["people"] = [
        {"alias": "队友甲", "me": False, "km": 168.0, "days": 24, "runs": 24,
         "badge": "这一段他走得最远", "badge_id": "km", "status": "moving"},
        {"alias": "小林", "me": True, "km": 140.0, "days": 28, "runs": 28,
         "badge": "一天没断", "badge_id": "full", "status": "moving"},
        {"alias": "阿灰", "me": False, "km": 0.0, "days": 0, "runs": 0,
         "badge": "", "badge_id": "", "status": "stranded"},
    ]
    _ohp = RU.outcome_page("TOK", _oc3)
    assert "谁走了多少" in _ohp and "队友甲" in _ohp and "168.0" in _ohp, \
        "结算页没列每个人的里程"
    assert "一天没断" in _ohp and "这一段他走得最远" in _ohp, "闪光点没显示"
    assert _ohp.count("没有记录") == 1, "没跑的人被写成了 0.0 km"
    assert "最后没走出来" in _ohp, "受困的人没在名单里标出来"
    assert "另有 1 人" in _ohp, "关掉共享的人没交代"
    assert _ohp.count("士气") == 1, "士气出现了不止一次，又变回主角了"
    assert "总行程" not in _ohp, "旧的四个大数字还在"
    # 没拿到旧日志时不提它：它是藏起来的东西，写「旧日志 0/3」等于
    # 替玩家宣布他错过了什么
    _oc4 = dict(_oc3, relic_count=0)
    assert "旧日志" not in RU.outcome_page("TOK", _oc4), "没拿到旧日志却提了它"
    assert "旧日志 1/3" in _ohp, "拿到了旧日志却没写"
    

    # 结局的脚印图形：四种结局必须真的不一样，否则「同一片雪地、不同的
    # 到达方式」这个说法就是空的
    _svgs = {k: RU._ending_svg(k) for k in
             ("arrive", "detour", "missing", "lost", "discoverer")}
    assert len(set(_svgs.values())) == 5, "五个结局的插画有重复"
    assert "C98B4B" in _svgs["arrive"] and "C98B4B" not in _svgs["lost"], \
        "「没走完」的窗户不该是亮的"
    assert _svgs["discoverer"].count("<rect") > 4, "隐藏结局没画出三本日志"

    # 底图模式：有 `ending_scene_lit/dark` 两版就走「底图 + 矢量脚印」，
    # 缺任何一张退回纯 SVG（半套底图会在某个结局上亮错灯，比没有更糟）。
    _pd = Path(tempfile.mkdtemp())
    _old_pd = RU.PLATE_DIR
    _old_ep = RU._ending_plate
    try:
        RU.PLATE_DIR = _pd          # 空目录：什么图都没有
        # `_ending_scene` 的第一优先级是「每个结局自己的图」
        # （`content/ending_<key>.*`，打包在 contentpack 里）。这里要测的是
        # 第二条「两版底图 + 矢量脚印」，所以必须把它屏蔽掉——否则
        # contentpack 里一有图，这个用例就永远走不到被测分支。
        RU._ending_plate = lambda key: ""
        assert RU._plates() == ("", ""), "没有底图时不该报出底图"
        assert '<div class="escene">' not in RU._ending_scene("arrive"), \
            "没有底图却走了底图分支"
        (_pd / "ending_scene_lit.png").write_bytes(b"x")
        assert RU._plates() == ("", ""), "只有亮窗版就不该启用（会亮错灯）"
        (_pd / "ending_scene_dark.png").write_bytes(b"x")
        RU.PLATE_DIR = _pd
        assert all(RU._plates()), "两版底图都在却没启用"
        _hit = RU._ending_scene("arrive")
        _drk = RU._ending_scene("lost")
        assert '<div class="escene">' in _hit and "ending_scene_lit" in _hit, \
            "亮窗的结局没用亮窗底图"
        assert '<div class="escene">' in _drk and "ending_scene_dark" in _drk, \
            "熄灯的结局没用暗窗底图"
        # 底下那层只能有脚印和道具：底图已经有天、地、屋、灯了
        assert "<rect x=\"262\"" not in _hit and "circle" not in _hit.split('class="prints"')[1], \
            "叠加层把夜空/小屋又画了一遍（会和底图叠影）"
        # 亮雪上白脚印等于没画：底图模式下脚印要压深
        assert "#8CA0BC" in _hit, "底图上的脚印还是白的，会看不见"
        assert "#DCE7F5" in RU._ending_svg("arrive"), "纯 SVG 模式的脚印依旧是白的"
        # 门口校准：换一张底图，门的位置就会变；脚印终点必须跟着走，
        # 否则会画到墙里面去
        bytes_before = RU._scene_props("arrive", on_plate=True)
        (_pd / RU.CAL_FILE).write_text('{"door_x": 0.5, "door_y": 0.8}',
                                       encoding="utf-8")
        assert abs(RU._cal()["door_x"] - 0.5) < 1e-9, "校准文件没读进来"
        assert bytes_before != RU._scene_props("arrive", on_plate=True), \
            "改了门口位置，脚印却没跟着动"
        (_pd / RU.CAL_FILE).write_text('{"door_x": 9, "door_y": "x"}',
                                       encoding="utf-8")
        assert RU._cal() == RU.CAL_DEFAULT, "越界的校准值没被丢掉（会画到画外）"
    finally:
        RU.PLATE_DIR = _old_pd
        RU._ending_plate = _old_ep
        shutil.rmtree(_pd, ignore_errors=True)

    # 每张结局自己的图（现在用的）：脚印和道具都在图里，不能再叠矢量层
    _pd2 = Path(tempfile.mkdtemp())
    _old2 = RU.PLATE_DIR
    # 素材是按需下发的，`_ending_plate` 先问 contentpack。机器上只要下载过
    # 一次图，这个用例就会拿真实素材跟临时目录比，永远对不上——所以把
    # contentpack 一起屏蔽，只测「临时目录里有没有图」这一层。
    from garmin_cn import contentpack as _CP
    _old_ce = _CP.exists
    try:
        _CP.exists = lambda name: False
        RU.PLATE_DIR = _pd2
        assert RU._ending_plate("arrive") == "", "没有图却报出图"
        (_pd2 / "ending_arrive.jpg").write_bytes(b"x")
        assert RU._ending_plate("arrive") == "/content/ending_arrive.jpg", \
            "没认出每结局的图"
        _own = RU._ending_scene("arrive")
        assert "ending_arrive.jpg" in _own and 'class="prints"' not in _own, \
            "每结局图模式下还叠了矢量脚印（成了两个人走过）"
        assert RU._ending_scene("detour").startswith("<svg"), \
            "这张没有图，应该退回纯 SVG"
        assert RU._ending_plate("nope") == "", "不认识的结局键不该去拼文件名"
    finally:
        RU.PLATE_DIR = _old2
        _CP.exists = _old_ce
        shutil.rmtree(_pd2, ignore_errors=True)

    # 部署脚本必须是 LF：Windows 上写文件默认 CRLF，`set -eu\r` 在 Linux 上
    # 会报 "Illegal option -"，而且只有真到服务器上才炸。这类问题不能靠记性。
    for _sh in sorted((Path(__file__).resolve().parent.parent / "deploy").glob("*.sh")):
        assert b"\r" not in _sh.read_bytes(), f"{_sh.name} 是 CRLF 行尾，Linux 上跑不了"

    # 换服务器之后，老用户 config.json 里存着上一代地址（platform_served_base），
    # 它优先于出厂内置地址。同步失败若也缓存 6 小时，用户就会被钉在死地址上，
    # 表现是「装上新包还是连不上」且没有任何提示。成功 6 小时 / 失败 60 秒
    # 这条差别必须守住，否则内测第一批人就会撞上。
    from garmin_cn import llm as _LSS
    _tmpcfg = Path(tempfile.mkdtemp()) / "llm_config.json"
    _old_cfg = _LSS.CONFIG_PATH
    _old_urlopen = _LSS.urllib.request.urlopen
    _calls = {"n": 0}

    def _boom(*_a, **_k):
        _calls["n"] += 1
        raise OSError("模拟断网")

    try:
        _LSS.CONFIG_PATH = _tmpcfg
        _LSS.save_config(platform_auto=True, platform_served_base="https://old.example",
                         platform_sync_at=int(time.time()) - 120, platform_sync_ok=False)
        _LSS.urllib.request.urlopen = _boom
        _LSS.sync_platform_config()      # 失败态隔了 120 秒：应该重试一次
        assert _calls["n"] == 1, "同步失败后没有按 60 秒间隔重试"
        _LSS.sync_platform_config()      # 刚失败过：60 秒内不该再打
        assert _calls["n"] == 1, "失败后 60 秒内又重复请求，白白多等一个超时"
        _c = _LSS.load_config()
        assert _c.get("platform_sync_ok") is False, "同步失败却记成了成功"
        assert _c.get("platform_served_base") == "https://old.example", \
            "失败不该清掉下发值（那是上一次的真实答案）"
        # 成功态：600 秒前同步过，仍应命中 6 小时缓存，不再发请求
        _LSS.save_config(platform_sync_at=int(time.time()) - 600, platform_sync_ok=True)
        _LSS.sync_platform_config()
        assert _calls["n"] == 1, "成功态 600 秒就重问了（应缓存 6 小时）"
    finally:
        _LSS.urllib.request.urlopen = _old_urlopen
        _LSS.CONFIG_PATH = _old_cfg
        shutil.rmtree(_tmpcfg.parent, ignore_errors=True)

    # 平台额度关闭时：界面只留"自配 Key"，不给一个点了必然报错的入口。
    # 这是和服务端 AI_PROXY=off 配套的一半，改一处忘一处就会出现
    # "能点、点了 503"的尴尬。
    from garmin_cn import llm as _L, _buildcfg as _BC
    _pc = _L.public_config()
    assert _pc.get("platform_quota") is _BC.PLATFORM_QUOTA, \
        "界面拿到的额度开关和构建配置不一致"
    assert _BC.PLATFORM_QUOTA is False, "资质没办下来之前平台额度必须关闭"
    assert _pc.get("mode") == "own", "额度关着时不该停在平台额度模式"
    from garmin_cn import aiui as _U
    assert 'id="modeSeg" style="display:none"' in _U.BODY, \
        "设置页没把「平台额度」收起来"
    assert "platform_quota" in _U.JS, "界面没读额度开关"

    # 诊断日志：能清空，而且重复清不报错。清空是对「老噪音淹掉真问题」的
    # 唯一解法——日志是 append-only 的，代码里删掉的日志行不会自己消失。
    from garmin_cn import diag as _DG
    from garmin_cn import appapi as _AA
    _old_dpath = _DG.log_path
    _dhome = Path(tempfile.mkdtemp())
    _dlog = _dhome / "diag.log"
    try:
        _DG.log_path = lambda: _dlog
        _DG.log_exc("test", "第一条")
        assert _dlog.is_file() and "第一条" in _dlog.read_text(encoding="utf-8")
        _r = _DG.clear()
        assert _r.get("ok") and not _dlog.exists(), f"没清掉：{_r}"
        _r2 = _DG.clear()
        assert _r2.get("ok") and _r2.get("bytes") == 0, f"重复清空报错：{_r2}"
        assert "diag_clear" in _AA._API, "diag_clear 没进桥的白名单，Kotlin 调不到"
    finally:
        _DG.log_path = _old_dpath
        shutil.rmtree(_dhome, ignore_errors=True)

    # 应用内更新：安装包只能走 https，且只能是 .apk。
    # 明文那条不是洁癖——targetSdk 34 下 networkSecurityConfig 会拦掉
    # DownloadManager 的 http 请求（用户看到「下载失败 0.00 B」），而自签
    # 证书下载管理器又不认，所以只能由 Python 走 TLS 下。这里守住入口。
    _af = _AA.apk_fetch
    _r5 = _af("http://39.106.174.153:8088/apk/x.apk")
    assert _r5.get("ok") is False and "https" in str(_r5.get("error")), \
        f"明文安装包地址没被拒：{_r5}"
    _r6 = _af("https://127.0.0.1:1/x.exe")
    assert _r6.get("ok") is False, f"非 apk 文件被接受了：{_r6}"

    # 代码包（覆盖式更新）：没有包时必须是 no-op，有包时要把目录挂到
    # garmin_cn.__path__ 最前面，而且用完能还原——这个 Package 对象整个进程
    # 只有一份，测试改坏了后面所有页面都会从临时目录导入。
    import garmin_cn as _pkg
    from garmin_cn import codepack as _CPK2
    _old_root, _old_ptr, _old_path = _CPK2.ROOT, _CPK2.POINTER, list(_pkg.__path__)
    _cphome = Path(tempfile.mkdtemp())
    _old_en = _CPK2.ENABLED
    try:
        # apply() 只看本地目录、不联网，所以这里可以开；refresh() 才是网络的。
        # 关掉的是「整块停用」，测试里要验的正是挂载本身。
        _CPK2.ENABLED = True
        _CPK2.ROOT = _cphome / "code"
        _CPK2.POINTER = _CPK2.ROOT / "current"
        _r = _CPK2.apply()
        assert _r.get("ok") and not _r.get("applied"), f"没有包时不该挂载：{_r}"

        from garmin_cn import __version__ as _CURVER

        def _mkpack(ver: str, base: str, modules=("appapi.py",)):
            d = _CPK2.ROOT / ver / "garmin_cn"
            d.mkdir(parents=True, exist_ok=True)
            for m in modules:
                (d / m).write_text("X = 1\n", encoding="utf-8")
            (d.parent / "meta.json").write_text(
                json.dumps({"version": ver, "base_version": base}), encoding="utf-8")
            return d

        # ① 基版本对得上 → 挂载，且在 __path__ 最前面
        _ver = "9.9.9-1"
        _d = _mkpack(_ver, _CURVER)
        _CPK2.POINTER.write_text(_ver, encoding="utf-8")
        _r = _CPK2.apply()
        assert _r.get("applied") == _ver, f"没挂上：{_r}"
        assert str(_pkg.__path__[0]) == str(_d), "挂载位置不在最前面（会被包内模块盖掉）"
        assert _CPK2.status().get("applied") == _ver, "status 没反映当前版本"

        # ② 基版本对不上（APK 升过版）→ 必须让位并清指针。
        #    真机上踩过：1.5.1-1 盖住 1.5.2，apk_fetch 直接消失，
        #    应用内更新报「未知操作」。
        _pkg.__path__ = list(_old_path)
        _bad = "0.0.0-old"
        _mkpack(_bad, "0.0.0")
        _CPK2.POINTER.write_text(_bad, encoding="utf-8")
        _r = _CPK2.apply()
        assert not _r.get("applied"), f"版本不符的包被挂上了：{_r}"
        assert not _CPK2.POINTER.exists(), "版本不符时没清指针，下次启动还会去挂"

        # ③ 没有 base_version 的老包 → 也拒绝（说不清适用版本就别装）
        _mkpack(_ver, "")
        _CPK2.POINTER.write_text(_ver, encoding="utf-8")
        _r = _CPK2.apply()
        assert not _r.get("applied"), f"没标适用版本的包被挂上了：{_r}"

        # ④ 基版本对但目录里没有 appapi.py = 坏包，不能挂
        #    （用独立版本号，否则会复用到 ① 建好的目录、appapi.py 还在）
        _empty = "9.9.9-empty"
        _mkpack(_empty, _CURVER, modules=())
        _CPK2.POINTER.write_text(_empty, encoding="utf-8")
        _pkg.__path__ = list(_old_path)          # 先还原再验「坏包不挂」
        _r = _CPK2.apply()
        assert not _r.get("applied"), f"坏包被挂上了：{_r}"
    finally:
        _pkg.__path__ = list(_old_path)
        _CPK2.ROOT, _CPK2.POINTER = _old_root, _old_ptr
        _CPK2.ENABLED = _old_en
        shutil.rmtree(_cphome, ignore_errors=True)

    # 代码包会让**所有模块**都从下载目录加载，`__file__` 全面偏移。
    # 两类东西的解法不同，两条都必须守住：
    #   包内资源（证书/条款/插画）→ 顺 __path__ 去真包里找
    #   用户数据（缓存/手册/记录）→ 跟 GARMIN_HOME
    # 1) 模拟「证书在自己旁边找不到」：这正是下载目录的处境。
    from garmin_cn import tlstrust as _T3
    _old_ca3 = _T3.CA_PATH
    _caempty = Path(tempfile.mkdtemp())
    try:
        _T3.CA_PATH = _caempty / "content" / "server-ca.pem"
        _T3.reset()
        _r3 = _T3.install()
        assert _r3.get("ok") is True, (
            f"证书路径偏到下载目录后就装不上了（{_r3}）——"
            "所有 HTTPS 会 CERTIFICATE_VERIFY_FAILED")
        assert "server-ca.pem" in str(_r3.get("path") or ""), str(_r3)
    finally:
        _T3.CA_PATH = _old_ca3
        _T3.reset()          # 真的还原 ssl，别让后面的 HTTPS 带着死路径
        shutil.rmtree(_caempty, ignore_errors=True)

    # 1b) 最阴的一种组合：**新代码 + 旧装载器**。代码包只换业务模块，
    #     codepack.py 始终来自 APK；如果新模块调了旧装载器里没有的函数，
    #     AttributeError 会被 try/except 吞掉，证书又回到「找不到」。
    #     所以路径解析必须内联。这里把 pkg_file 摘掉来模拟。
    from garmin_cn import codepack as _CPK4
    _had_pf = getattr(_CPK4, "pkg_file", None)
    try:
        if _had_pf is not None:
            del _CPK4.pkg_file
        _T3.CA_PATH = _caempty / "content" / "server-ca.pem"
        _T3.reset()
        _r4 = _T3.install()
        assert _r4.get("ok") is True, (
            "装载器缺 pkg_file 时证书找不到——基础路径解析不能依赖 codepack")
    finally:
        if _had_pf is not None:
            _CPK4.pkg_file = _had_pf          # type: ignore[attr-defined]
        _T3.CA_PATH = _old_ca3
        _T3.reset()

    # 2) 数据目录必须跟着 GARMIN_HOME。用子进程跑，因为模块级常量在
    #    import 时就定死了，本进程里改环境变量测不出来。
    import subprocess as _sp
    _dhome2 = Path(tempfile.mkdtemp())
    try:
        _probe = (
            "import sys; sys.path.insert(0, %r)\n"
            "from garmin_cn import brief, manual, nutrition, motion, video\n"
            "print(brief.ROOT)\n"                 # 地形缓存
            "print(manual._LEGACY.parent.parent)\n"   # 手册迁移源
            "print(nutrition._LEGACY_HOME)\n"     # 饮食迁移源
            "print(motion.ROOT)\n"                # 视频缓存
            "print(video.FRAMES.parent.parent)\n"  # 输出帧
        ) % str(ROOT)
        _env2 = dict(os.environ, GARMIN_HOME=str(_dhome2))
        _out2 = _sp.run([sys.executable, "-c", _probe], env=_env2,
                        capture_output=True, text=True, timeout=180)
        assert _out2.returncode == 0, (_out2.stderr or "")[-500:]
        for _line in _out2.stdout.strip().splitlines():
            assert Path(_line).resolve() == _dhome2.resolve(), (
                f"数据目录没跟 GARMIN_HOME：{_line}（应为 {_dhome2}）"
                "——代码包挂载后它会偏到下载目录，用户数据会「消失」")
    finally:
        shutil.rmtree(_dhome2, ignore_errors=True)

    # 底部导航：小组必须是一个 tab，且排在教练后面（第 3 位）。
    # 用户明确否掉了「顶栏右上角一枚胶囊」——那个位置既不像入口也不像按钮。
    _coros_src = (ROOT / "garmin_cn/coros.py").read_text(encoding="utf-8")
    assert "数值字段" in _coros_src, "诊断没 dump dayDetail 的数值字段"

    _kt5 = (KT / "MainActivity.kt").read_text(encoding="utf-8")
    assert 'arrayOf("今日", "教练", "小组", "历史", "我的")' in _kt5, \
        "底部导航没有把「小组」放进去"
    assert '"/groups"' in _kt5 and "TAB_GROUP = 2" in _kt5, \
        "小组 tab 的位置或路径不对（应在教练之后、历史之前）"
    assert "groupBtn" not in _kt5, "顶栏那个「小组」胶囊又回来了"
    assert 'path.startsWith("/run") -> TAB_GROUP' in _kt5, \
        "远征页没归到小组 tab，从组详情点进去会掉出这个分区"

    # 简洁模式要连「小组」tab 一起藏。GONE 而不是删 view：tabRoots / tabIcons
    # 是按 TAB_* 索引取的，删了会全线错位。
    # 返回键必须逐级上退，不能一步跳回「今日」——足迹页按返回飞回首页
    # 是用户报的问题，根因就是 nativeBack 里缺 /groups 分支
    assert 'path.startsWith("/groups/")' in _kt5, \
        "返回键没有处理小组二级页，会直接跳回今日"
    _nb = _kt5.split("private fun nativeBack()")[1].split("private fun isTabRoot")[0]
    assert "/groups/${seg[1]}/feed" in _nb, "动态详情的上一级不是动态列表"
    assert '"/groups/${seg[1]}"' in _nb, "小组二级页的上一级不是组详情"
    assert "/groups" in _nb and "loadUrl" in _nb, "组详情的上一级不是组列表"
    assert 'path.startsWith("/run")' in _nb, "远征页没有回到它所属的小组"

    assert "private fun applySimpleTabs()" in _kt5 and "View.GONE" in _kt5, \
        "简洁模式没有隐藏小组 tab 的逻辑"
    assert 'apiCall("simple_state")' in _kt5, \
        "原生没读简洁模式状态，重启后 tab 会又冒出来"
    assert "applySimpleTabs()\n        updateTabs()" in _kt5, \
        "建完 tab 没有立刻按简洁模式设置可见性"
    from garmin_cn import appapi as _APA

    assert hasattr(_APA, "simple_state"), "Python 侧没有 simple_state"

    # 组列表页：只有一个组时直接进详情——点「小组」最想看的是排名
    from garmin_cn import groupui as _GU
    _gsrc = inspect.getsource(_GU.groups_page)
    # 单组直达：但带了 message 就不能直达——建组/加入失败是 303 回跳
    # `/groups?msg=…`，一进列表就被弹进组详情，那句错误提示永远没人看见
    assert "if len(items) == 1 and not force_list and not message:" in _gsrc and \
        "return group_page(" in _gsrc, "只有一个组时没有直接进详情页（或没给 message 留口子）"
    # 单组直达必须能被 ?list=1 绕过，否则「加入另一个组」永远没入口
    assert "force_list: bool = False" in _gsrc, "列表页没有强制列表的开关"
    assert 'class="fold fold-lg fold-gap" open' in _gsrc, \
        "被引导进来时「再加入一个」还是折着的"
    assert '<summary>再加入一个' in _gsrc, \
        "建组/加入没有了"
    # 建组有两个服务端硬限制（一人只能当 1 个组的组长 / 最多 3 个组），
    # 命中时不能还摆一个注定 409 的表单
    assert "owner_g =" in _gsrc and "你已经是「" in _gsrc, \
        "已经是组长时还摆着建组表单"
    _ssrc = inspect.getsource(_GU.settings_page)
    assert "/archive" in _ssrc and "解散这个组" in _ssrc, \
        "组长没有解散入口——既建不了新组也解散不掉旧组，是个死结"
    # 唯独不能退回「永久折叠」：force_list 之外仍是折叠的
    assert "else '<details class=\"fold fold-gap\">'" in _gsrc, \
        "底部入组表单不再折叠，会和榜单抢版面"
    # 单组时组详情的「全部小组」必须带 list=1，否则点回去又被弹回本组
    _gpage_src = inspect.getsource(_GU.group_page)
    assert "/groups?list=1" in _gpage_src, \
        "组详情的「全部小组」没带 list=1，单组用户点了会原地弹回"
    _set_src = inspect.getsource(_GU.settings_page)
    assert "/groups?list=1" in _set_src, "设置页没有「全部小组」入口"

    # 账号昵称：建组/入组的默认名字，别再让用户每组填一次
    _asrc2 = (ROOT / "garmin_cn" / "group.py").read_text(encoding="utf-8")
    assert "def set_alias" in _asrc2, "组内昵称接口丢了"
    _accsrc = (ROOT / "garmin_cn" / "account.py").read_text(encoding="utf-8")
    assert "def set_nickname" in _accsrc, "账号昵称接口缺失"
    assert "nickname" in _accsrc, "登录没有把昵称带上（注册时要用）"

    # 组页的版式回归：数据块不许再套一圈卡、低频表单必须折叠、
    # 成员不许和榜单排两遍。这些全是「改一次就会慢慢长回去」的东西，
    # 所以在这里渲染一次真实页面来守（假数据换掉联网的三个函数）。
    from garmin_cn import group as _GP
    from garmin_cn import groupui as _GU

    _gd, _gb, _gc = _GP.detail, _GP.board, _GP.challenge_list
    _gcg = getattr(_GP, 'challenge_get', None)
    _gw = getattr(_GP, "week", None)
    _gs = getattr(_GP, "season", None)
    _gm = getattr(_GP, "map_data", None)
    _gf = getattr(_GP, "feed", None)

    def _fake_detail(_gid):
        return {"ok": True, "group": {
            "id": 1, "name": "测试队", "intro": "", "owner_id": 1,
            "max_members": 20, "member_count": 2, "created_at": 0,
            "role": "owner", "invite_code": "ABCD2345", "camp_level": 3,
            "me": {"alias": "我", "share": "summary"},
            "members": [{"user_id": 1, "alias": "我", "role": "owner",
                         "share": "summary", "is_me": True},
                        {"user_id": 2, "alias": "队友", "role": "member",
                         "share": "summary", "is_me": False}]}}

    try:
        _GP.detail = _fake_detail
        _GP.board = lambda gid, rng="week", today=None, by="dist": {
            "ok": True, "range": rng, "rows": [
                {"user_id": 2, "alias": "队友", "is_me": False, "distance_m": 12000,
                 "days": 3, "acts": 3, "explorer": 12},
                {"user_id": 1, "alias": "我", "is_me": True, "distance_m": 9000,
                 "days": 2, "acts": 2, "explorer": 3}],
            "hidden": 0,
            "summary": {"members": 2, "shown": 2, "hidden": 0,
                        "total_distance_m": 21000}}
        # 一条进行中的赛事：组页那张卡要能点进「谁排第几」
        _GP.challenge_list = lambda gid, today=None: {"ok": True, "challenges": [
            {"id": 7, "title": "本周 25 公里", "kind": "distance", "target": 25000,
             "current": 31000, "pct": 100.0, "status": "running",
             "from": "2026-11-16", "to": "2026-11-22", "unit": "km"}]}
        # 赛事详情：每人一行，已达成的人挂达成日
        _GP.challenge_get = lambda gid, cid, today=None: {
            "ok": True, "hidden": 1,
            "challenge": {"id": 7, "title": "本周 25 公里", "kind": "distance",
                          "target": 25000, "current": 31000, "pct": 100.0,
                          "from": "2026-11-16", "to": "2026-11-22",
                          "status": "running", "unit": "km"},
            "rows": [{"alias": "我", "is_me": True, "current": 25000,
                      "pct": 100.0, "done_at": "2026-11-18"},
                     {"alias": "队友", "is_me": False, "current": 6000,
                      "pct": 24.0, "done_at": None}]}
        # 六件衫：带榜名、口径、前 3 名。不 mock 的话组页那一行整块不显示，
        # 下面关于「点点是什么」的断言就测了个空。
        _shirt = lambda i, n, t, c, f, h, top: {
            "id": i, "name": n, "title": t, "color": c, "field": f,
            "unit": "", "hint": h,
            "winners": [{"user_id": 2, "alias": "队友"}] if top else [],
            "top": ([{"user_id": 2, "alias": "队友", "text": top[0]},
                     {"user_id": 1, "alias": "我", "text": top[1]}] if top else []),
            # 完整榜：荣誉页要全列出来，不是只给前 3
            "rank": ([{"user_id": 2, "alias": "队友", "text": top[0]},
                      {"user_id": 1, "alias": "我", "text": top[1]}] if top else [])}
        _GP.season = lambda gid, today=None: {
            "ok": True, "base_max": 90.0, "bonus_max": 10.0, "hidden": 0,
            "people": [
                {"user_id": 2, "alias": "队友", "rank": 1, "score": 81.2,
                 "km": 12.0, "days": 3, "elev": 0, "streak": 3, "plan_rate": 1.0,
                 "progress": 0.12, "title": "远征队长",
                 "shirts": [{"id": "yellow", "name": "黄衫", "title": "里程王",
                             "color": "#E8C43A"}]},
                {"user_id": 1, "alias": "我", "rank": 2, "score": 63.8,
                 "km": 9.0, "days": 2, "elev": 0, "streak": 2, "plan_rate": 0.5,
                 "progress": 0.05, "title": "先锋", "shirts": [],
                 "weeks": 5,
                 "parts": {"km": 22.5, "days": 13.3, "elev": 0.0,
                           "progress": 6.2, "streak": 6.7, "bonus": 0.0}}],
            "me": {"user_id": 1, "alias": "我", "rank": 2, "score": 63.8,
                   "km": 9.0, "days": 2, "elev": 0, "streak": 2, "plan_rate": 0.5,
                   "progress": 0.05, "title": "先锋", "shirts": [],
                   "weeks": 5,
                   "parts": {"km": 22.5, "days": 13.3, "elev": 0.0,
                             "progress": 6.2, "streak": 6.7, "bonus": 0.0}},
            # 分数与称号的说明：权重要下发，页面不写死
            "weights": {"km": 30, "days": 20, "elev": 15, "progress": 15,
                        "streak": 10, "bonus": 10},
            "titles": [{"name": "远征队长", "hint": "本周综合分在组里前 10%"},
                       {"name": "先锋", "hint": "本周综合分在组里前 30%"},
                       {"name": "行者", "hint": "在这个组已经参与满 3 周"},
                       {"name": "同行者", "hint": "全组这周都动了，且你已参与满 3 周"}],
            "all_active": False,
            "shirts": [
                _shirt("yellow", "黄衫", "里程王", "#E8C43A", "km",
                       "本周跑步的总里程", ("12.0 km", "9.0 km")),
                _shirt("green", "绿衫", "出勤王", "#3FA76A", "days",
                       "本周有跑步的天数（一天练两次算一天）", ("3 天", "2 天")),
                _shirt("polka", "圆点衫", "爬升王", "#D8DEE9", "elev",
                       "本周累计爬升的米数", ()),
                _shirt("white", "白衫", "进步王", "#F2F5FA", "progress",
                       "最近 7 天比之前 7 天多跑的比例", ("+12%", "+5%")),
                _shirt("red", "红衫", "计划完成王", "#D9534F", "plan_rate",
                       "有课的日子里完成的比例", ("100%", "50%")),
                _shirt("blue", "蓝衫", "连续王", "#4C8DF6", "streak",
                       "本周最长连续出勤的天数", ("3 天", "2 天"))],
            "explore": {"min_cells": 5, "badges": []},
            "max": {"km": 12.0, "days": 3, "elev": 0, "streak": 3,
                    "progress": 0.12, "explorer": 0},
            "goal": {"km": None, "max": 1000.0, "reported": False},
            "predict": {"mine": None, "bonus": {}}}
        _GP.map_data = lambda gid, today=None: {
            "ok": True, "from": "2026-11-16", "to": "2026-11-22",
            "people": [], "hidden": 2, "cells": 0}
        _GP.feed = lambda gid, today=None, act=None: {
            "ok": True, "hidden": 0, "emojis": ["👍", "🔥"],
            "rows": [{"id": 9, "user_id": 2, "alias": "队友", "color": "#3FA76A",
                      "is_me": False, "date": dt.date.today().isoformat(),
                      "kind": "run", "kind_name": "跑步", "distance_m": 12000,
                      "duration_s": 3600, "elevation_m": 60, "comments": 1,
                      "recent": [{"id": 31, "user_id": 1, "alias": "我",
                                  "mine": True, "text": "跑得不错👍🔥", "emoji": ""},
                                 {"id": 32, "user_id": 2, "alias": "队友",
                                  "mine": False, "text": "牛", "emoji": "🎉"}]}]}
        # P2 的「本周」块：目标 + 结对 + 预言。不 mock 会去连真服务器，
        # 虽然失败很快，但每次自检都白等一次连接。
        _GP.week = lambda gid, today=None: {
            "ok": True, "from": "2026-11-16", "to": "2026-11-22",
            "goal": {"km": 30, "suggest": 28.0, "reported": True},
            "pairs": [{"slot": 1,
                       "small": {"user_id": 1, "alias": "我", "km": 30,
                                 "target": 30, "actual": 9.0},
                       "big": {"user_id": 2, "alias": "队友", "km": 60,
                               "target": 75, "actual": 12.0},
                       "even": False, "result": "upset"}],
            "predict": {"mine": 2, "rows": [
                {"user_id": 1, "target_uid": 2, "result": "wrong",
                 "alias": "我", "target_alias": "队友"}]},
            "bonus": {}, "goal_rows": [],
            "bingo": {"done": 12, "total": 25, "lines": 1},
            "targets": [{"user_id": 2, "alias": "队友"}]}
        _gh = _GU.group_page("TOK", 1)
        # 六件衫页和动态页都要在 mock 还有效时渲染——放到 finally 之后就只能
        # 连真服务器、走错误分支，断言永远失败。
        _shp = _GU.shirts_page("TOK", 1)
        _fdp = _GU.feed_page("TOK", 1)
        _fdo = _GU.feed_page("TOK", 1, "", 9)
        _set = _GU.settings_page("TOK", 1)
        # 完整榜单页：周/月两个口径各渲一次（月榜不该挂衫）
        _bdw = _GU.board_page("TOK", 1)
        _bdm = _GU.board_page("TOK", 1, "", "month")
        # 评论 303 回来时要锚到这条并默认展开评论区
        _ghf = _GU.group_page("TOK", 1, "", 0, 9)
        # 建赛事的提示要落在赛事区块里，而不是只挂在页首（用户看不见）
        _ghc = _GU.group_page("TOK", 1, "同时进行的赛事已达 3 个", 0, 0, "chal")
        # 赛事详情：每人一行 + 达成日 + 管理员能提前结束
        _chp = _GU.challenge_page("TOK", 1, 7)
        # 超过 10 人：截到前 10 + 补「⋯ + 我」+ 完整榜单入口
        _GP.board = lambda gid, rng="week", today=None, by="dist": {
            "ok": True, "range": rng, "hidden": 0,
            "from": "2026-11-16", "to": "2026-11-22",
            "rows": [{"user_id": i, "alias": ("我" if i == 12 else f"队员{i}"), "is_me": i == 12,
                      "distance_m": 30000 - i * 1000, "days": 3, "acts": 3,
                      "explorer": 0} for i in range(1, 13)],
            "summary": {"members": 12, "shown": 12, "hidden": 0,
                        "total_distance_m": 300000}}
        _ghb = _GU.group_page("TOK", 1)
        _bdb = _GU.board_page("TOK", 1)
    finally:
        _GP.detail, _GP.board, _GP.challenge_list = _gd, _gb, _gc
        _GP.challenge_get = _gcg
        _GP.week = _gw
        _GP.season = _gs
        _GP.map_data = _gm
        _GP.feed = _gf

    # 榜单/赛事/设置三块直接铺在页面上，不再各发一个盒子
    assert 'class="flat"' in _gh, "组页又给数据块套上卡片了"
    # 榜单和赛事仍是无框数据块；动态改成了一条一卡（.acard），
    # 所以这里不再数「≥3 个 flat」，免得把合法的改版判成回归。
    assert _gh.count('class="flat"') >= 2, "榜单/赛事又被套上卡片盒子了"
    # 低频表单折叠：建赛事、管理
    assert "建一个赛事" in _gh and "<details" in _gh, "建赛事表单又常驻展开了"
    # 建赛事表单：提交后要还是展开的（data-k），结束日要有默认值——
    # 结束日空着直接点提交会被服务端按「格式不对」拒掉，而那句提示在页首，
    # 用户在页面中部只看到「点了没反应」。
    assert '<details class="fold" data-k="chal">' in _gh, "建赛事表单没记住展开状态"
    _end_v = re.search(r'name="end_date" value="([^"]*)"', _gh)
    assert _end_v and re.match(r"\d{4}-\d{2}-\d{2}$", _end_v.group(1)), \
        f"结束日没有默认值：{_end_v.group(1) if _end_v else '缺失'}"
    # 五种赛事类型都要能选，且单位/示例挂在 option 上（不是和标签抢同一个属性名）
    for _kv in ('value="elevation"', 'value="explore"', 'value="speed"'):
        assert _kv in _gh, f"建赛事缺类型 {_kv}"
    assert 'data-u="秒/km"' in _gh and 'data-ph=' not in _gh, \
        "option 的单位属性又叫回 data-unit 了（会把选项文字改坏）"
    # 建赛事的失败提示必须在「赛事」这一块里：组页很长，错误挂在页首
    # 就等于没提示（用户以为按钮坏了）。
    _i_chal = _ghc.find('<h2>赛事</h2>')
    assert _i_chal > 0 and "同时进行的赛事已达 3 个" in _ghc[_i_chal:], \
        "建赛事的失败提示没落在赛事区块里"
    assert _ghc.count("同时进行的赛事已达 3 个") == 1, "同一条提示渲染了两遍"
    assert 'class="nt"' in _ghc, "失败提示用了成功的绿色样式"

    # ---- 赛事详情：组页只给全组合计，谁跑了多少要能点进去看
    assert 'class="chal" href="/groups/1/challenge/7"' in _gh, \
        "组页的赛事卡不能点进完成度榜"
    assert "25.0<i> / 25.0 km</i>" in _chp, "赛事详情没按「当前/目标」显示成绩"
    assert "6.0<i> / 25.0 km</i>" in _chp, "队友的成绩没显示"
    assert "✓ 11-18" in _chp, "已达成的人没标达成日期"
    assert 'class="rank me"' in _chp, "赛事详情里没高亮我自己"
    assert "另有 1 人关闭了共享" in _chp, "关闭共享的人没交代"
    assert "提前结束这个赛事" in _chp and "/challenge/7/close" in _chp, \
        "管理员在赛事详情里没有提前结束的入口"
    # 昵称 / 共享 / 邀请码 / 退出全搬进「小组设置」了，组页只留一个页头齿轮。
    # 用户原话：「放在最下面太突兀」。
    # 营地等级只由客户端拼后缀，Lv1 不显示
    assert _GU._gname({"name": "队", "camp_level": 3}) == "队 · Lv3 营地"
    assert _GU._gname({"name": "队", "camp_level": 1}) == "队"
    assert "队 · Lv3 营地" == _GU._gname({"name": "队", "camp_level": "3"})
    assert "· Lv3 营地" in _gh, "组页标题没拼营地等级"
    assert 'class="hact"' in _gh and "/settings" in _gh, "组页页头没有设置入口"
    assert ">昵称<" not in _gh, "昵称表单又回到组页正文里了"
    assert "邀请码与管理" not in _gh, "组管理又回到组页正文里了"
    assert "<details class=\"fold\" open" not in _gh, "折叠块默认就展开了"
    # 成员不许和榜单排两遍：榜单里有名字，就不该再有「成员」区块
    assert "sec-head\"><h2>成员" not in _gh, "成员又单独占了一块（和榜单重复）"
    # 组页榜单只放前 10，完整榜单在**另一个页面**。周/月互切也搬去那页了：
    # 在这个 200KB 的页面上重载一次只为换个聚合口径，动态会重刷、位置会丢。
    assert f"/groups/1/board" in _gh, "组页没有进完整榜单的入口"
    assert 'class="seg"' in _bdw and "?range=month" in _bdw, "榜单页没有周/月切换"
    assert "本月" in _bdm and "本周" in _bdw, "榜单页标题没写清是周还是月"
    assert "月榜只排里程" in _bdm, "月榜没交代为什么没有衫"
    assert 'class="mk"' not in _bdm, "月榜又挂上了按周结算的衫/徽章"
    assert 'class="mk"' in _bdw, "周榜名字后面没有徽章"
    assert 'class="rank me"' in _bdw, "完整榜单里没标出我自己"
    # 昵称能在页面上直接改
    # 设置页本身要有这些东西，否则就是「搬走了也找不回来」
    # 假数据里我是组长：所以看到的是「转让组长」而不是「退出小组」——
    # 组长不能直接退，得先转让，这条口径在服务端也是这么实现的
    # 组长工具与加入审批：设置页要有入口，不然功能等于没做
    for _need in ("操作记录", "/audit", "导出本周数据", "export.csv", ">加入<"):
        assert _need in _set, f"设置页缺「{_need}」"
    for _need in (">昵称<", ">共享<", ">上报<", "邀请码", "转让组长"):
        assert _need in _set, f"设置页缺「{_need}」"
    assert "你是组长" in _set, "组长在设置页没看到「先转让才能退出」的提示"
    assert "/alias" in _set and "/rotate" in _set, "设置页表单没接上接口"
    assert "/groups/1/map" in _set, "设置页没有路线共享入口"

    # ---- 核心数据卡：名次 + 分数 + 称号 + 六件衫徽章
    # 原来「我·第N名」和「衫得主」是两条独立的长行，谁也收不住谁；
    # 现在合进一张卡：分数在上半区，六件衫是下半区的 6 枚圆徽章。
    assert 'class="scorecard"' in _gh, "没有核心数据卡"
    assert 'class="sc-v"' in _gh and "分</i>" in _gh, "分数没进核心卡"
    # 只数核心卡里那 6 件：周榜行现在也挂小号衫图标，全页数会数多
    _i_sc = _gh.find('class="sc-badges"')
    _j_sc = _gh.find("</section>", _i_sc)
    _sc_block = _gh[_i_sc:_j_sc] if _i_sc >= 0 and _j_sc > _i_sc else ""
    assert _sc_block.count('class="jsh') == 6, \
        f"荣誉徽章应该是 6 件衫，实际 {_sc_block.count('class="jsh')} 件"
    assert 'class="sc-shirts"' in _gh and "/shirts" in _gh, "徽章组不是进六件衫榜单的入口"
    # ---- 分数与称号：用户看到「50.6 分」「同行者」却无处确认，必须能点开看
    assert 'class="scorehow"' in _gh and 'data-k="scorehow"' in _gh, \
        "核心卡没有「分数/称号怎么来的」入口"
    for _lb in ("跑量", "出勤", "爬升", "进步", "连跑", "加分"):
        assert f'<span>{_lb}</span>' in _gh, f"分数明细缺「{_lb}」这一项"
    assert "22.5<i> / 30 分</i>" in _gh, "分数明细没按「得分 / 权重」显示"
    assert "组里最高 12.0 km" in _gh, "分数明细没写清「和谁比」"
    # 称号：说明 + 为什么是我这个
    assert "本周综合分在组里前 30%" in _gh, "称号没有对应的门槛说明"
    assert "你现在是「<b>先锋</b>」" in _gh, "没说明我为什么是这个称号"
    assert "称号只取最高的一个" in _gh, "没说清称号不叠加"

    # ---- 目标：独立入口行，不再是四行表格里的一行
    assert 'class="goalrow"' in _gh, "目标没有做成独立入口行"
    assert 'class="gk"' in _gh and 'class="gs"' in _gh, "目标入口缺主文或副文"
    assert "定本周目标" in _gh or "目标 30" in _gh, "目标入口没写清是定还是改"

    # ---- 结对 / 预言：仍是「标签 + 内容」两列对齐
    assert 'class="kvrows"' in _gh, "结对/预言没有用对齐表格"
    for _k in ("周期", "结对", "预言"):
        assert f'<span class="k">{_k}</span>' in _gh, f"「{_k}」那行没有标签列"
    # 周期必须写出来：用户原话「甚至不知道时间范围是什么」
    assert "周日 24:00 截止" in _gh, "没写这周什么时候截止"
    assert "目标 30" in _gh, "本周目标没在页面上显示"
    assert "我 30" in _gh, "结对让分没显示出来"
    assert "他 75" in _gh and "多背" in _gh, "让分只给了数字，没说多背多少"
    assert "完不成" in _gh, "我投的预言没回显"
    assert 'name="predict"' in _gh, "没有预言选择框"
    # 「怎么算的」：玩法规则要能自己读到，不能靠猜
    assert "玩法说明" in _gh, "没有玩法说明入口"
    for _h in ("定目标", "让分赛", "预言"):
        assert f"<b>{_h}</b>" in _gh, f"玩法说明缺「{_h}」"
    # 结算周期只在「周期」那一行说一次，说明里不再重复
    # 目标入口是 details：summary 那行常驻，表单点开才出现
    assert '<details class="goalrow"' in _gh and 'data-k="goal"' in _gh, \
        "目标入口不是可展开的（或没留还原状态的 key）"
    # 评论区的折叠块要带稳定 key，发完评论回来才知道该展开哪一条
    assert 'data-k="cm9"' in _gh, "动态卡片没给评论区留还原 key"
    assert 'id="a9"' in _gh, "动态卡片没有可锚定的 id"
    assert ('<details class="ac-cm" data-k="cm9" open>' in _ghf), \
        "发完评论回来，那条的评论区没有默认展开"
    # 探索徽章不占衫：board 里够 5 格才在名字后面挂一枚虚线方框图标。
    # 夹具里队友 12 格、我 3 格，所以只该有 1 枚。
    assert _gh.count('title="探索徽章"') == 1, \
        f"榜上的探索徽章图标数量不对：{_gh.count('title="探索徽章"')}"
    # 同组同名：衫必须按 user_id 挂。按 alias 挂时两个「小王」会拿到同一个图标。
    _mk = {2: "A", "小王": "WRONG"}
    assert _GU._rank_marks({"user_id": 2, "alias": "小王"}, _mk, 5).startswith("A"), \
        "徽章按名字匹配了（同组同名会挂错人）"
    # 功能入口：一排并列的图标，不再散在各处。
    # 「六件衫」不在这一排——核心数据卡的徽章行（六件衫 N/6 ›）就是它的入口，
    # 同一屏两个去同页的按钮属于重复。这里顺带守着「不要再加回来」。
    assert 'class="qgrid"' in _gh, "功能入口没有做成并列图标"
    for _ic in ("Bingo", "足迹", "远征"):
        assert f'class="qn">{_ic}</span>' in _gh, f"图标网格缺 {_ic}"
    # 三格都要带一个数（icon 变大之后光有名字太空）；Bingo/远征有分母还要带条
    assert _gh.count('class="qv"') == 3, "三格里没有都放数值"
    # Bingo 一定有分母（0/25 也算），远征在 mock 里拿不到 run_state，会退化成
    # 没有条的那一版——所以这里只守 Bingo 这条，别把断言写成 >= 2。
    assert _gh.count('class="qb"') >= 1, "Bingo 格子没有进度条"
    assert 'class="qi"' in _gh, "入口图标还是旧的 <i>，放大后的 .qi 没生效"
    # 目标行在图标之后：首屏顺序是 卡片 → 三格 → 目标
    assert (_gh.find('class="qgrid"') < _gh.find('class="goalrow"')), \
        "目标行跑到功能图标上面去了"
    # 邀请码复制：脚本必须在页面里，否则按钮点了没反应
    assert "function gcCopy" in _gh, "组页没挂复制邀请码的脚本"
    assert 'class="qn">六件衫</span>' not in _gh, \
        "六件衫入口重复了：徽章行已经是它的入口"
    assert 'class="sc-shirts"' in _gh and "荣誉 " in _gh, "徽章行不再是荣誉入口"
    assert '<div class="bgrid"' not in _gh, "Bingo 的 25 个格子被搬进组页了"

    # 信息顺序：点进小组第一眼要看到「我在组里第几」，然后才是玩法，
    # 再往下才是赛事和探索。P2 加了两块之后榜单被挤到第三屏，这条就破过。
    _i_rank = _gh.find("<h2>本周榜</h2>")
    assert _i_rank > 0, "组页没有榜单"
    assert _gh.find("我 · 第") < _i_rank, "我的名次被排到榜单后面了"
    assert _gh.find("目标 <b>30</b>") < _i_rank, "本周目标挤到榜单后面了"
    assert _i_rank < _gh.find("<h2>赛事</h2>"), "赛事排到榜单前面了"
    # 功能图标也在榜单之前（它们是入口，不该排到榜单后面去）
    assert _gh.find('class="qgrid"') < _i_rank, "功能图标被排到榜单后面了"
    assert "/map" in _gh and "足迹" in _gh, "组页没有组内足迹入口"

    # 周榜名字后面只挂图形：衫 + 探索徽章。四段小字（我/组长/N 天/新 N 格）
    # 全删了——一屏十几行时它就是注脚，而且徽章本来就有形状。
    assert 'class="mk"' in _gh, "周榜名字后面没有挂徽章图标"
    for _bad in ('class="sub">我', 'class="sub">组长', 'class="sub">新', ' 天</span>'):
        assert _bad not in _gh, f"周榜还在名字后面写「{_bad}」"
    # 颜色点不能只有颜色：每一件都要写出「衫名·榜名」，否则新来的人只看到
    # 一排小圆点，不知道黄衫是里程榜。组页那行还得能点进六件衫。
    assert "黄衫·里程王" in _gh, "衫行只写了「黄衫 阿狗」，没说是哪个榜"
    assert "红衫·计划完成王" in _gh, "长榜名（计划完成王）没显示出来"
    assert "/shirts" in _gh and "荣誉" in _gh, "组页没有进本周荣誉的入口"

    # ---- 榜单截断：组里 12 人时组页只排前 10，我排第 12 也要看得见
    _ib = _ghb.find("<h2>本周榜</h2>")
    _jib = _ghb.find("<h2>赛事</h2>")
    _blk = _ghb[_ib:_jib] if _ib >= 0 and _jib > _ib else ""
    assert _blk.count('class="no"') == 11, \
        f"组页榜单应只显示前 10 + 我自己，实际 {_blk.count('class=\"no\"')} 行"
    assert 'class="rk-gap"' in _blk, "截断处没有省略号，看着像只排了 10 名"
    assert "完整榜单" in _blk and "/groups/1/board" in _blk, "没有完整榜单入口"
    assert 'class="rank me"' in _blk, "我在 10 名之外时没把自己补到榜上"
    # 12 人的完整页要全排出来，一个人都不能少
    assert _bdb.count('class="no"') == 12, \
        f"完整榜单少排了人：{_bdb.count('class=\"no\"')} 行"

    _shp_html = _shp
    assert "六项称号" in _shp_html, "荣誉页没渲染出来"
    for _nm in ("黄衫", "绿衫", "圆点衫", "白衫", "红衫", "蓝衫"):
        assert _nm in _shp_html, f"六件衫页缺 {_nm}"
    assert "本周跑步的总里程" in _shp_html, "荣誉页没写口径"
    assert _shp_html.count('class="hon"') == 6, "荣誉页不是六张卡"
    assert '<ol class="hlist">' in _shp_html, "荣誉页没有完整榜"
    assert "class=\"me\"" in _shp_html or "class='me'" in _shp_html, \
        "完整榜里没标出我自己"
    assert "还没有数据" in _shp_html, "没数据的一项没给出说明"
    # 收起态只显示得主：六项必须是 <details>，得主在 summary 里，
    # 这样六项能在一屏里看完，点哪行展哪行的完整榜
    assert _shp_html.count("<details class=\"hon\">") == 6, \
        "六项荣誉不是可折叠的 details"
    # 6 项称号 + 1 行探索徽章（不占称号，但必须看得见）
    assert _shp_html.count('class="hw"') == 7, "收起态没显示得主/探索徽章"
    assert 'class="hon hon-x"' in _shp_html, "探索徽章没有单独一行"
    assert "探索徽章" in _shp_html and "额外" in _shp_html, \
        "探索徽章没标出「不占称号」"

    # ---- 跨省的分片：一张图装不下 900 km，格子会缩成看不见的点
    from garmin_cn import basemap as _BM
    from garmin_cn import explore as _EX

    _xa = ["7597:24229", "7599:24231"]
    _hb = ["25435:35172", "25437:35174"]
    assert len(_EX.cluster_cells(_xa + _hb)) == 2, "跨省的格子没有被分成两片"
    assert len(_EX.cluster_cells(_xa + ["7598:24230"])) == 1, "同城的格子被拆成多片"
    assert _EX.span_km(_xa + _hb) > 500, "跨省跨度算错了"
    assert _EX.span_km(_xa) < 50, "同城跨度算错了"

    # 会话判据必须全项目一致。`ensure_session` 以前只认 JWT_WEB，而
    # `_garmin_session_ok` / `_client` 认 `JWT_WEB or SESSIONID`。手机号登录
    # 拿的是 SESSIONID，于是「我的」页显示已登录、同步却报「会话已过期」，
    # 用户看到的就是「明明在线为什么说过期」。
    _aus = (ROOT / "garmin_cn/auth.py").read_text(encoding="utf-8")
    assert 'self.net.get_cookie("JWT_WEB") or self.net.get_cookie("SESSIONID")' in _aus, \
        "ensure_session 又只认 JWT_WEB 了，会和 _garmin_session_ok 打架"
    # 同名函数定义两遍，后一个覆盖前一个，改错一处不报错但行为变了。
    _apsv = (ROOT / "garmin_cn/appapi.py").read_text(encoding="utf-8")
    assert _apsv.count("def _garmin_session_ok(") == 1, \
        "_garmin_session_ok 有重复定义"
    # 失败原因要带出来，否则用户只看到「已过期」，没法判断是 401 还是网络。
    assert 'getattr(auth, "last_verify"' in _apsv, \
        "AuthError 没带上会话校验的具体原因"

    # 体重/AI 数据口径：用户只有一台秤，却被告知「另一台秤」。
    # body.stats 要给出「体脂是否与体重同源」，AI 上下文在同源时不重复传
    # body_fat_any，提示词也不再无条件说「另一台秤的读数」。
    _bps = (ROOT / "garmin_cn/body.py").read_text(encoding="utf-8")
    assert "fat_same_scale" in _bps, "body.stats 没判断体脂是否与体重同源"
    _aps2 = (ROOT / "garmin_cn/aicoach.py").read_text(encoding="utf-8")
    assert "fat_same_scale" in _aps2 and "_body_ctx" in _aps2, \
        "AI 上下文同源时仍重复传体脂，模型会当成两台秤"
    assert "是另一台秤的读数（body_fat_any_src" not in _aps2, \
        "提示词仍硬编码「另一台秤」，用户只有一台秤时必错"

    # 活动列表是**最新在前**。取 [-N:] 拿到的是最老的那批，模型会拿一个月前
    # 的训练量去解释当前的体重变化（实测踩到：简报里「最近」是 10 月的活动）。
    assert '(res.get("activities") or [])[:10]' in _aps2, \
        "weight_review 仍取最老的 10 条活动"
    assert '(res.get("activities") or [])[:4]' in _aps2, \
        "对话简报仍取最老的活动"

    # 对话简报不能只给「体重一个数」：用户问吃什么/练得怎样时模型只能说没有。
    for _kw in ("训练负荷", "营养目标", "人工记录了"):
        assert _kw in _aps2, f"对话简报缺「{_kw}」这一项"

    # 今日页要能看到体成分和摄入。四格 grid 不许新增区域，所以挂在体重格说明
    # 行 + 面板底部一行（横屏隐藏）。
    _uis = (ROOT / "garmin_cn/ui.py").read_text(encoding="utf-8")
    assert "rc-nutline" in _uis and "rc-nutline{display:none}" in \
        (ROOT / "garmin_cn/screen.py").read_text(encoding="utf-8"), \
        "今日页没有营养行或横屏没隐藏"

    # Bingo 的爬升类卡片全靠 group_daily.elevation_m。取日汇总时漏了这一列，
    # rows 里就没有这个键，elevation 一律读成 0，于是「本周爬升 / 单日爬升 /
    # 山地 12km」永远不亮（用户报：20km 爬升 1367m，爬升卡却没激活）。
    _gsv = (ROOT / "server/groups.py").read_text(encoding="utf-8")
    assert _gsv.count("duration_s, elevation_m") >= 2, \
        "bingo 取日汇总时漏了 elevation_m，所有爬升类卡片会判不出来"

    # 底图缓存必须比对点集指纹。缓存按 ident 命名，而 ident 由调用方给
    # （`rt1-5`）：同一路线重新分享、同一片格子新增时点集变了但 ident 不变，
    # 只认 ident 就会复用旧画幅，表现成「新路线看起来没更新」（用户报过）。
    # 格子里程必须按设备距离归一。轨迹折线在 GPS 断一段时会短三成
    # （实测 4.69 km 的活动轨迹只有 3.11 km），不归一就会出现
    # 「足迹图 52 km、榜单 61 km」——用户报过这个「数据对不上」。
    _exs = (ROOT / "garmin_cn/explore.py").read_text(encoding="utf-8")
    assert "def cells_meters(track, total_m" in _exs, "格子里程没按设备距离归一"
    assert 'r["distance_m"]' in _exs, "调用方没把设备距离传进 cells_meters"

    _bsrc = (ROOT / "garmin_cn/basemap.py").read_text(encoding="utf-8")
    assert "def _fp(" in _bsrc and 'meta.get("fp") == fp' in _bsrc, \
        "底图缓存没比对点集指纹，点集变了会复用旧画幅"
    assert "def _covers(" in _bsrc, "老缓存没有指纹时缺画幅覆盖判断"

    _bme = _BM.ensure
    _gmd = _GP.map_data
    try:
        _BM.ensure = lambda *a, **k: None       # 不联网，走无底图回退路径
        _GP.map_data = lambda gid, today=None: {
            "ok": True, "from": "2026-11-16", "to": "2026-11-22", "hidden": 0,
            "cells": 4,
            "people": [
                {"user_id": 1, "alias": "我", "color": "#5EA0FF",
                 "cells": _xa, "is_me": True},
                {"user_id": 2, "alias": "远方的人", "color": "#3FA76A",
                 "cells": _hb, "is_me": False}]}
        _mh = _GU.map_page("TOK", 1)
    finally:
        _BM.ensure = _bme
        _GP.map_data = _gmd

    # ---- 同一格多人跑过：不能让后画的把人吃掉，也不能混成第三种颜色
    _p1 = {"user_id": 1, "alias": "我", "color": "#5EA0FF",
           "cells": ["1:1", "1:2", "1:3"], "is_me": True}
    _p2 = {"user_id": 2, "alias": "队友", "color": "#3FA76A",
           "cells": ["1:2", "1:3", "1:4"], "is_me": False}
    # 没有跑量数据时（老客户端）：两人平摊 0.5 < 0.65，重叠格算「共同」
    _own = _GU._cell_owners([_p1, _p2])
    assert _own["1:1"][0] == 1 and _own["1:1"][1] == 1.0, f"独占格算错：{_own['1:1']}"
    assert _own["1:2"][1] < _GU.OWNER_SHARE, f"平摊时重叠格不该归某个人：{_own['1:2']}"
    assert _GU._shared_count([_p1, _p2]) == 2
    assert _GU._shared_count([_p1]) == 0, "一个人不该有「共同」格子"

    # 有跑量时：谁跑得多归谁，差距不到 2 倍仍算共同
    _q1 = {"user_id": 1, "alias": "我", "color": "#5EA0FF", "cells": ["2:2"],
           "meters": {"2:2": 3000}, "is_me": True}
    _q2 = {"user_id": 2, "alias": "队友", "color": "#3FA76A", "cells": ["2:2"],
           "meters": {"2:2": 500}, "is_me": False}
    _o2 = _GU._cell_owners([_q1, _q2])["2:2"]
    assert _o2[0] == 1 and _o2[1] > _GU.OWNER_SHARE, f"跑量多的没拿到归属：{_o2}"
    assert _o2[2] == 3500, f"该格总里程算错：{_o2[2]}"
    _q2["meters"]["2:2"] = 2400
    _o3 = _GU._cell_owners([_q1, _q2])["2:2"]
    assert _o3[1] < _GU.OWNER_SHARE, "只差 1.25 倍就归给某个人了，应当算共同"
    # 深浅：跑得越多越实
    assert (_GU._op_of(300) < _GU._op_of(1200) < _GU._op_of(9000)), \
        "格子深浅没随跑量递增"

    _bme2 = _BM.ensure
    _gmd2 = _GP.map_data
    try:
        _BM.ensure = lambda *a, **k: None
        _GP.map_data = lambda gid, today=None: {
            "ok": True, "from": "2026-11-16", "to": "2026-11-22",
            "hidden": 0, "cells": 4, "people": [_p1, _p2]}
        _mh2 = _GU.map_page("TOK", 1)
    finally:
        _BM.ensure = _bme2
        _GP.map_data = _gmd2
    # 共同格必须单独成一个白色图层，否则后画的人会把先画的覆盖掉
    assert f'fill="{_GU.SHARED_COLOR}"' in _mh2, "没有共同格子的白色图层"
    assert '<i class="sh"></i>共同' in _mh2, "图例里没有共同色块"
    assert _mh2.count("M ") >= 3, "三个图层（我 / 队友 / 共同）应该各自成 path"

    assert _mh.count('class="shard-h"') == 2, "跨省没有分片成两张图"
    assert _mh.count('class="card gmapcard"') == 2, "分片的卡片数不对"
    # 每片一条图例；多出来的那条就是修复前那个「对不上任何一张图」的全局图例
    assert _mh.count('class="lgi"') == 2, \
        f"图例数不对（{_mh.count('class="lgi"')} 条），可能又套了一层全局图例"

    # 动态：组页只给最近几条 + 入口；详情页有表情按钮和留言框
    # 动态已经并进组页：不再有指向 /feed 的链接，评论在卡片里展开
    assert "大家的动态" in _gh, "组页没有动态区"
    assert "/groups/1/feed" not in _gh, "组页还有指向独立动态页的链接"
    assert 'class="ac-cm"' in _gh, "组页动态卡片里没有评论折叠"
    # 动态从「一行一条」改成「一条一卡」：剪影 + 大字距离 + 元信息 + 三条评论
    assert 'class="acard"' in _gh, "组页动态没有按卡片渲染"
    assert 'class="ac-mid"' in _gh, "动态卡片没有剪影区"
    # 有剪影画折线，没剪影画「无轨迹」占位——两种都得在，不能拿装饰线冒充路线
    assert ('class="ac-tr"' in _gh) or ('class="ac-ph"' in _gh), \
        "动态卡片既没有剪影也没有占位"
    assert 'class="ac-km"' in _gh and 'class="ac-sub"' in _gh, \
        "动态卡片没有距离大字 / 元信息行"
    assert "大家最近" in _fdp and "跑步" in _fdp, "动态列表页没渲染出来"
    # 卡片不再是链接（里面有表单），评论折叠在卡内
    assert "/feed?act=9" not in _fdp, "动态卡片还是链接，点输入框会跳页"
    assert 'class="ac-cm"' in _fdp, "动态列表里没有评论折叠"
    assert 'data-ins="👍"' in _fdo and 'name="text"' in _fdo, \
        "详情页没有表情插入 / 留言入口"
    # 表情内嵌：不再是独立的 emoji 提交字段，而是插进文字的按钮
    assert 'name="emoji"' not in _fdo, "表情还是独立的提交字段（应内嵌到文字里）"
    assert 'data-say="1"' in _fdo, "输入框没有标记 data-say，表情插不进光标"
    # 内嵌表情原样出现在正文里（「跑得不错👍🔥」），不能被转义弄坏
    assert "跑得不错👍🔥" in _fdo, "内嵌表情的评论没正常渲染"
    # 旧数据（表情单独存 emoji 列）要拼回文字：id=32 是 emoji=🎉 + text=牛
    assert "🎉牛" in _fdo, "旧数据的 emoji 列没拼回正文"
    # 自己的评论才有「改 / 删」，且折在铅笔里（防误触）。夹具里 id=31 是 mine。
    assert _fdo.count('class="cm-more"') == 1, "自己的评论没有操作入口（或别人的也有）"
    assert "编辑这条评论" in _fdo, "编辑区没有标题，用户不知道点开的是什么"
    assert "comment_edit" in _fdo and "comment_del" not in _fdo, \
        "评论操作表单不对（改删应共用一张表单，用 op 区分）"
    assert 'name="op" value="save"' in _fdo and 'name="op" value="del"' in _fdo, \
        "改和删没有用 op 区分"
    assert "confirm(" in _fdo, "删除没有二次确认"
    # 编辑区也要能插表情：编辑表单里得有 data-say + 表情按钮
    _edit_seg = _fdo.split("编辑这条评论", 1)[1].split("</details>", 1)[0]
    assert 'data-say="1"' in _edit_seg and 'data-ins="👍"' in _edit_seg, \
        "编辑评论时不能插入表情"
    assert 'value="跑得不错👍🔥"' in _edit_seg, "编辑框没预填原有内容（含表情）"
    # 现在会**算**配速（距离÷时长），所以文案不能再写「没有配速」——
    # 那是不实陈述。要写清「配速是算的」「没有轨迹、没有位置」。
    # 剪影算法：形状归一化、去掉 (0,0) 占位点、异常输入不抛
    import math as _m
    _ring = [[31.0 + 0.002 * _m.cos(i / 40 * 2 * _m.pi) / 1.0,
              121.0 + 0.002 * _m.sin(i / 40 * 2 * _m.pi)] for i in range(41)]
    _sp = _GP._shape(_ring)
    assert _sp.count(";") + 1 == min(_GP.SHAPE_MAX_POINTS, len(_ring)), "轨迹点数不对"
    _xy = [tuple(int(v) for v in seg.split(",")) for seg in _sp.split(";")]
    assert all(0 <= x <= _GP.SHAPE_SCALE and 0 <= y <= _GP.SHAPE_SCALE
               for x, y in _xy), "剪影坐标越界"
    # 圆形轨迹归一化后应该接近正方形（长边 = SHAPE_SCALE，短边也接近它）
    assert min(max(x for x, _ in _xy), max(y for _, y in _xy)) >= _GP.SHAPE_SCALE * 0.85, \
        "圆形轨迹被拉成了扁的，等比缩放坏了"
    assert _GP._shape([]) == "" and _GP._shape([[0, 0]] * 10) == "", \
        "没轨迹时剪影该是空串"
    assert "0,0;0,0" not in _sp, "剪影里还留着占位点"
    # 方向：纬度越大越靠北，而 SVG 的 y 是向下的。少了翻转，整条轨迹南北
    # 颠倒（往北跑会画成往南跑）——这条漏过一次，专门守在这里。
    _north = [[30.0 + i * 0.0001, 120.0] for i in range(10)]
    _npt = [tuple(int(v) for v in s2.split(","))
            for s2 in _GP._shape(_north).split(";")]
    assert _npt[0][1] > _npt[-1][1], "轨迹南北倒了：往北跑画成了往下"
    _east = [[30.0, 120.0 + i * 0.0001] for i in range(10)]
    _ept = [tuple(int(v) for v in s2.split(","))
            for s2 in _GP._shape(_east).split(";")]
    assert _ept[0][0] < _ept[-1][0], "轨迹东西倒了：往东跑画成了往左"
    assert "配速按距离 ÷ 时长算" in _fdp, "动态页没写清配速是算出来的"
    # 剪影是脱敏形状，文案必须说清「不含坐标/位置」，否则等于隐瞒上传了什么
    assert "不含经纬度" in _fdp and "没有底图" in _fdp, \
        "动态页没写清轨迹不含坐标、没有底图"
    # 最新三条评论要在卡片里露出来（原来只露一个「N 条」的数字）
    assert 'class="ac-c"' in _fdp, "动态卡片没有评论区"

    # ---- P2 口径守卫：数字必须两端同一套，否则用户看到的格数和服务端记的
    # 对不上，而且这种偏差没人会主动报 bug，只会觉得「徽章不准」。
    from garmin_cn import explore as _EX
    from garmin_cn import group as _GX

    _gg = (ROOT / "server/groups.py").read_text(encoding="utf-8")
    _rk = (ROOT / "server/ranking.py").read_text(encoding="utf-8")
    assert _EX.CELL_M == 500.0, "探索网格边长不是 500 m"
    assert _EX.MIN_ACT_M == 3000.0, "单次计入门槛不是 3 km"
    assert f"DAY_EXPLORER_MAX = {_EX.DAY_CAP}" in _gg, \
        "服务端单日格数上限和客户端 explore.DAY_CAP 不一致"
    assert tuple(_EX.RUN_TYPES) == tuple(_GX.RUN_TYPES), \
        "探索和里程对「跑步类」的定义分叉了"
    assert "explorer     INTEGER NOT NULL DEFAULT 0" in _gg, \
        "group_daily 没有 explorer 列（老库迁移会漏）"
    # 赛季分：基础 90 + 让分/预言加分 10 = 100。改权重前先想清楚页面上的满分。
    assert "W_KM = 30.0" in _rk and "BONUS_MAX = 10.0" in _rk, \
        "赛季分权重或加分上限被改动，页面满分要跟着改"
    assert "PREDICT_HIT = 2.0" in _rk and "PREDICT_UNDERESTIMATED = 3.0" in _rk, \
        "预言的 +2 / +3 口径变了"
    # 圆点衫和白衫的色号都是白（#D8DEE9 / #F2F5FA），纯色画出来是两个一模一样
    # 的小点。圆点衫必须带 pattern，客户端才会画成波点。
    assert '"pattern": "dots"' in _rk, "圆点衫没标波点图案，会和白衫撞成两个白点"
    assert 'if pattern == "dots"' in (ROOT / "garmin_cn/groupui.py").read_text(
        encoding="utf-8"), "客户端没给圆点衫画波点的分支"
    # 衫型图标：圆点衫靠 SVG 的波点 pattern 画，不再靠 CSS 色点。
    # 没拿到的那 5 件必须是空心虚线，否则「1/6」这个数立不住。
    _uisrc = (ROOT / "garmin_cn/ui.py").read_text(encoding="utf-8")
    assert ".jsh{" in _uisrc, "衫型图标没有样式"
    _gsrc = (ROOT / "garmin_cn/groupui.py").read_text(encoding="utf-8")
    assert "pattern id=" in _gsrc and "jshDots" in _gsrc, \
        "圆点衫没有波点 pattern，会和白衫画成两件一模一样的衣服"
    assert "stroke-dasharray" in _gsrc, "没拿到的衫不是空心虚线"

    # Bingo：25 格、中心免费、易中难各 8。这三条一变，页面上那 5×5 就歪了。
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "server"))
    import bingo as _BG

    _bc = _BG.make_card(1, 1, "2026-11-16")
    assert len(_bc) == 25 and _bc[_BG.CENTER]["task"] == "free", \
        "Bingo 卡不是 25 格或中心不是免费格"
    _lv = [c["level"] for c in _bc]
    assert (_lv.count(1), _lv.count(2), _lv.count(3)) == (8, 8, 8), \
        "Bingo 的易/中/难不是 8/8/8"
    # 同一周同一人必须稳定：不落库靠的就是这个种子
    assert [c["task"] for c in _BG.make_card(1, 1, "2026-11-16")] == \
        [c["task"] for c in _bc], "Bingo 卡不稳定（同周同人算出两张）"

    # 无域名部署：自签证书靠「把那一张加进信任链」生效，不是关校验。
    # 没有证书时必须是 no-op——用公共 CA 的部署不该被这段影响。
    from garmin_cn import tlstrust as _TLS
    # 部署之后仓库里**会**有 content/server-ca.pem，所以"没有证书"这一态
    # 不能靠真实文件判断，得临时指到一个不存在的路径上去。
    _old_ca = _TLS.CA_PATH
    # 注意：现在 CA_PATH 找不到时还会顺 __path__ 去包里找（代码包会让
    # __file__ 偏移，所以那条兜底是必须的）。要测「真的一张都没有」，
    # 得把兜底也一起屏蔽，否则测的是「仓库里恰好有证书」。
    _old_pf = _TLS._pkg_file
    _TLS.CA_PATH = Path(tempfile.mkdtemp()) / "不存在的证书.pem"
    _TLS._pkg_file = lambda rel: None
    _TLS.reset()
    _r = _TLS.install()
    assert _r.get("ok") is False, "没有证书却报装好了"
    assert _r.get("reason"), "没装成却没说明原因"
    _TLS._pkg_file = _old_pf
    _tmp_ca = Path(tempfile.mkdtemp()) / "server-ca.pem"
    try:
        _tmp_ca.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
                           encoding="utf-8")
        _TLS.CA_PATH = _tmp_ca
        _TLS.reset()
        import ssl as _ssl
        _before = _ssl.create_default_context
        _r2 = _TLS.install()
        assert _r2.get("ok") is True, f"有证书却没装上：{_r2}"
        assert _ssl.create_default_context is not _before, "没接管默认上下文"
        # 坏证书不能让 App 起不来：装的时候要吞掉异常，只记录
        assert _TLS.install() == _r2, "install 不是幂等"
    finally:
        _TLS.CA_PATH = _old_ca
        _TLS.reset()            # 真的还原 ssl，别让后面的 HTTPS 带着死证书跑
        import ssl as _ssl2
        assert _ssl2.create_default_context is not None
        shutil.rmtree(_tmp_ca.parent, ignore_errors=True)

    # 分享图：1080×1920 的竖图，走和活动图同一套出图链路（探针 + 9:16）。
    # 它**不能带**轨迹、昵称或别人的名字——发出去最多暴露「我参加了一个跑步
    # 小组」。版式锁死高度，否则出图时上下会留一大块底色。
    from garmin_cn import runcard as RC
    assert RC.W == 1080 and RC.H == 1920 and RC.W * 16 == RC.H * 9, "分享图不是 9:16"
    _sh = RC.build_html(_oc3)
    assert "data-gc-canvas" in _sh and "__gcExportInfo" in _sh, "分享图缺出图探针"
    assert "抵达" in _sh and "372" in _sh, "分享图缺结局名或总里程"
    # 分享图也讲「人走了多远」：士气/旧日志这种局内数值不该占大数字的位置
    assert "士气" not in _sh, "分享图又把士气当成了四大数字之一"
    assert "走到的里程" in _sh and "走过的人" in _sh, "分享图缺里程或人数"
    # 旧日志没拿到时不占格子：一张「旧日志 0/3」的分享图是在替玩家
    # 宣布他错过了什么
    assert "旧日志" in _sh
    assert "旧日志" not in RC.build_html(dict(_oc3, relic_count=0)), \
        "没拿到旧日志却在分享图上写了 0/3"
    assert _sh.count('class="ar') == 4, "四段路没画全"
    assert "height:1920px" in _sh, "分享图没锁高度，出图会上下留白"
    for _bad in ("track", "latitude", "小赵", "老李"):
        assert _bad not in _sh, f"分享图里出现了不该出现的东西：{_bad}"
    # 没结算过的段：显示「没走到」，不能算成 0/0 没够
    _sh2 = RC.build_html(dict(_oc, acts=[{"act": 1, "theme": "信任", "got": 0.0,
                                          "target": 0.0, "hit": False}]))
    assert "没走到" in _sh2 and "没够" not in _sh2, "分享图把没结算的段画成了没够"

    # 增益：只写「名字 + 数值 + 单位」，绝不把 effect 这种内部字段写进页面
    _bc = RU.buff_chips([{"id": "tw", "name": "顺风", "effect": "weekly_cap_add",
                          "value": 2, "unit": "km", "desc": "本周每天行程上限提高"}])
    assert "顺风" in _bc and "+2 km/天" in _bc, "增益没渲染出数值"
    assert "weekly_cap_add" not in _bc, "增益把内部字段写进页面了"
    assert RU.buff_chips([]) == "", "没有增益时不该占位置"
    # 营地还没做：这类增益灰着显示，不装作有用
    _bd = RU.buff_chips([{"id": "v", "name": "篝火", "effect": "act_vigor_add",
                          "value": 1, "unit": "点"}])
    assert "dim" in _bd and "休整点" in _bd, "未接入的增益没区分出来"
    # 没有选项的补给事件是「抽到即生效」，得说明白，否则像张没画完的卡
    _sup = {"id": "tw", "type": "supply", "act": 1, "title": "垭口顺风",
            "text": "风从背后推着走。", "options": [], "chosen": "", "result": ""}
    assert "已经算上" in RU.event_card(_sup, 0), "白给的补给事件没说明已生效"

    # 遗迹事件：标签要是「旧物」，不能混在普通「遭遇」里被划过去
    _relic = {"id": "x_relic", "type": "story", "act": 2, "title": "门框上的字",
              "text": "有人在门框上刻了字。", "relic": True, "chosen": "",
              "options": [{"id": "take", "label": "拿走", "brief": "", "time": 0}]}
    _rh = RU.event_card(_relic, 0)
    assert "旧物" in _rh and "relic" in _rh, "遗迹事件没有视觉标识"

    # 衬线字体：正文靠它和「数据」区分开。Android 的 WebView 里 serif 对中文
    # 没有可用字体，不内嵌就 fallback 成无衬线，故事感直接消失。
    # 字体现在有两个可能的位置：随包（本地开发）和 server/assets（正式来源，
    # 由 /v1/content 按需下发）。只要有一处存在就算合格。
    _font = next((p for p in (
        ROOT / "garmin_cn" / "content" / "expedition-serif.woff2",
        ROOT / "server" / "assets" / "expedition-serif.woff2") if p.is_file()), None)
    assert _font is not None, ("衬线字体没构建：python tools/build_font.py "
                             "（缺了的话正文会静默退化成正文字体）")
    assert _font.stat().st_size > 200_000, "字体文件太小，可能只子集到几个字"
    assert 'font-family:"RC Serif"' in RU.CSS, "正文没用衬线字体族"

    # 字体覆盖：加了新剧情之后没重建字体，正文里就会出现方框
    _sys_path = sys.path[:]
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import importlib

        _bf = importlib.import_module("build_font")
        _miss = _bf.check_coverage(_font, _bf.story_text())
        assert not _miss, f"这些字不在字体里，正文会显示方框：{''.join(_miss)}"
    finally:
        sys.path[:] = _sys_path

    # ---------------- 计划生成器 ----------------
    # 分配本身是纯函数：长距离必须是这一周最长的一次，否则「长距离」名不副实。
    # 周量允许在赛事/减量周被下调，所以这里只断言「摊完后接近给定周量」，
    # 赛事系数在下一段单独验。
    for _n in (2, 3, 5):
        _sp = PG._spread(30, _n)
        _kms = [v[1] for v in _sp.values()]
        assert len(_kms) == _n, f"{_n} 天框架排出了 {len(_kms)} 天"
        assert abs(sum(_kms) - 30) <= 1.5, f"{_n} 天框架摊完是 {sum(_kms)}，离 30 太远"
        _long = [v[1] for v in _sp.values() if v[0] == "long"]
        assert _long and abs(_long[0] - max(_kms)) < 0.01, \
            f"长距离不是最长的一次：长距离 {_long} / 全部 {_kms}"
    _d = PG.build_draft(None, today="2026-11-17", monday=_mon,
                        run_days=3, week_km=30, save=False)
    assert _d["week_km"] <= 30.5, f"生成量超了给定框架：{_d['week_km']}"
    assert _d["week_km"] == round(sum(i["km"] for w in _d["week"] for i in w["items"]), 1), \
        "草稿里的周量和每天课次对不上"
    assert _d["reason"], "草稿没说明为什么这么排"
    # 能力不够的人不排强度课：一周两天只练有氧
    _d2 = PG.build_draft(None, today="2026-11-17", monday=_mon,
                         run_days=2, week_km=20, save=False)
    assert not [i for w in _d2["week"] for i in w["items"] if i["kind"] == "tempo"], \
        "一周只跑两天还排了节奏跑"
    # from_date 之后才排课：自动续不能给「今天下午」塞一节长课
    _d3 = PG.build_draft(None, today="2026-11-17", monday=_mon, run_days=3,
                         week_km=30, from_date="2026-11-18", save=False)
    assert not _d3["week"][1]["items"], "from_date 之前仍然排了课"

    # 确认草稿：不能盖掉用户手改的那天
    _gdir = Path(tempfile.mkdtemp())
    _gpath = _gdir / "plan_draft.json"
    _grules = _gdir / "plan_rules.json"
    PG.build_draft(None, today="2026-11-17", monday=_mon, run_days=3, week_km=30,
                   path=_gpath, rules_path=_grules)
    PE.save_day("2026-11-20", [{"km": 4}], path=_gdir)
    _cf = PG.confirm(path=_gpath, rules_path=_grules, today="2026-11-17")
    assert "2026-11-20" in (_cf.get("skipped_manual") or []), "草稿盖掉了用户手改的那天"
    assert PG.rules(_grules)["auto"] is True, "确认后没有开启自动续"
    _kept = json.loads((_gdir / "plan_overrides.json").read_text(encoding="utf-8"))
    assert _kept["days"]["2026-11-20"]["items"][0]["km"] == 4, "手改的课被草稿改了"
    assert _kept["days"]["2026-11-18"]["origin"] == "generated", "草稿没标成 generated"
    # 过去的日子不写：改了历史会让完成率凭空变化
    assert "2026-11-16" not in _kept["days"], "草稿把过去的日子也写了"
    shutil.rmtree(_gdir, ignore_errors=True)

    # 推送：kind 映射与周次/星期
    _pi = PP.info_of({"km": 8, "minutes": 45, "kind": "easy", "name": "轻松跑"}, "2026-11-18")
    assert _pi["week"] == 47 and _pi["dow"] == "周三", f"推送的周次/星期算错：{_pi['week']}/{_pi['dow']}"
    assert PP.info_of({"kind": "strength"}, "2026-11-18")["kind"] == "str", \
        "力量课没映射成 workout 认识的 str"
    assert PP.push(None, [], dry=True).get("ok") is False, "空计划也允许推送了"

    # ---------------- 受保护内容守卫 ----------------
    # 受保护内容只在服务端下发：客户端目录里出现数据集/正文就是泄漏
    _leak = CG.scan(ROOT)
    assert not _leak, f"客户端目录里混进了受保护内容：{_leak}"
    _g = Path(tempfile.mkdtemp())
    (_g / "garmin_cn").mkdir()
    (_g / "garmin_cn" / "act1.json").write_text('{"events": []}', encoding="utf-8")
    assert CG.scan(_g), "受保护数据放进客户端目录，守卫没拦住"
    (_g / "garmin_cn" / "act1.json").unlink()
    (_g / "garmin_cn" / "x.json").write_text('{"result_brief": {}}', encoding="utf-8")
    assert CG.scan(_g), "带受保护字段的 JSON，守卫没拦住"
    shutil.rmtree(_g, ignore_errors=True)

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

            # 「我明明练了却显示未练」的回归：日历路径产出的 week 里 done 可能
            # 为空（活动项没有距离/时长），必须用本地库活动兜底。
            _wk = {"week": [
                {"date": "2026-11-02", "dow": "周一",
                 "items": [{"name": "轻松跑"}], "acts": [], "rest": False},
                {"date": "2026-11-03", "dow": "周二",
                 "items": [{"name": "间歇"}], "acts": [], "rest": False},
                {"date": "2026-11-04", "dow": "周三",
                 "items": [], "acts": [], "rest": True},
            ]}
            _cells = _week_cells(
                _wk, "2026-11-07",
                {"2026-11-02": {"km": 8.0, "min": 45}}).split('class="wkd')[1:]
            assert "已完成" in _cells[0], \
                "本地库练过、快照里没有 done，一图流仍判成未练"
            assert "未练" in _cells[1], "真没练的那天反而不标未练"
            assert "未练" not in _cells[2], "休息日被当成漏练"
            _mix = _week_cells(
                {"week": [
                    {"date": "2026-11-02", "dow": "周一",
                     "items": [{"name": "轻松跑"}], "acts": [], "rest": False},
                    {"date": "2026-11-04", "dow": "周三",
                     "items": [], "acts": [], "rest": True}]},
                "2026-11-07", {"2026-11-02": {"km": 8.0, "min": 45},
                               "2026-11-04": {"km": 5.0, "min": 30}}
            ).split('class="wkd')[1:]
            assert "已完成" in _mix[0], "有排课且练过，该是已完成"
            assert "已练" in _mix[1] and "未练" not in _mix[1], \
                "休息日练了却写成「已完成」，会被误读成完成了课表"
            # 日历归一化也要吃本地活动，否则 week[].done 和「本周完成」都是 0
            _cal2 = PL.from_calendar(
                [{"date": "2026-11-02", "itemType": "workout", "title": "轻松跑",
                  "sportTypeKey": "running", "estimatedDistanceInMeters": 8000}],
                "2026-11-07",
                activities=[{"date": "2026-11-02", "type": "running",
                             "distance_m": 8200, "duration_s": 2700}])
            _w2 = next(x for x in _cal2["week"] if x["date"] == "2026-11-02")
            assert _w2["done"], "传了本地活动还把已完成判成未练"
            assert not _w2["missed"]
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
    assert "Number.isFinite(TL.total)" in _mo_src and "const TOTAL =" in _mo_src, \
        "页面没用真实总时长（或没做 NaN/0 兜底）"
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

    # ---- 活动图 P0（1.6.1）：曲线量程 / 分区结论 / hero 层级 / 强度角标 ----
    # 1) 配速轴用 1.5×IQR 定界。曾用 2%~98% 分位，被前几分钟的慢速起跑拉到
    #    4'52"–7'49"，后面 50 分钟全挤在顶部 1/4，E 跑最该看的稳定性看不见。
    assert "q1 - 1.5 * iqr" in _ac_src and "_med * 2.2" in _ac_src, \
        "配速轴又退回按极值定界了（会被慢速段压扁）"
    # 2) 泳道按像素留空隙。按比例时 0.38/0.42 只差 8px，两条轴的刻度会叠字。
    assert "band_gap = 30.0" in _ac_src, "泳道间隔又变回按比例了"
    # 3) 分区结论只陈述分布，不评价训练质量（评价要看课表，课表不一定有）
    assert _AC._zone_note([180, 800, 2500, 0, 0]) == "集中在 Z3（72%） · 阈值以上 0%"
    assert "Z2+Z3" in _AC._zone_note([180, 1150, 1000, 470, 0]), \
        "没有单档过半时没说占比最大的两档，一句结论会和明细打架"
    # 4) 尾部连续为 0 的档合并成一行，省下来的高度给结论句
    assert _AC.zone_block([180, 800, 2500, 0, 0], "心率分区").count('class="zrow') == 4, \
        "Z4/Z5 都为 0 时没有合并，同一件事写了两遍"
    assert _AC.zone_block([180, 1550, 1150, 470, 0], "心率分区").count('class="zrow') == 5, \
        "只有最后一档为 0 时不该合并"
    # 5) 右上角只显示运动类型：运动类型有二十多种，再混一个强度码进去，
    #    读的人分不清这格是「什么运动」还是「什么强度」。强度码已整套移除。
    assert "htag zone" not in _ac_src and "_plan_intensity" not in _ac_src, \
        "强度码角标又回来了（右上角只留运动类型）"
    # 6) hero 六格分两级：k2 = 用时/平均心率，k3 = 其余
    _ac_row = {"type": "running", "distance_m": 11000, "duration_s": 3487,
               "date": "2026-11-18", "start_ts": "2026-11-18T17:25:00",
               "avg_hr": 146}
    _ac_det = {"summary": {"cad_avg": 189, "stride_avg": 101},
               "laps": [], "series": {"points": []}}
    _ac_h = _AC.build_html(_ac_row, _ac_det, None, None, None)
    assert _ac_h.count('class="st k2"') == 2 and _ac_h.count('class="st k3"') == 3, \
        "hero 六格又变成同一级字号了"
    assert 'class="st wd k3"' in _ac_h, "文字型取值（主要益处）没有降档"

    # ---- 活动图 P1（1.6.1）----
    # 7) x 轴按公里：11 km 的图应给 0/2/4/6/8/10 六个刻度
    _pts = [{"t": 1000 + i * 30, "dist": i * 110.0} for i in range(101)]
    _mk = _AC._km_marks(_pts, 1000, 3000)
    assert _mk and _mk[0] == (0.0, "0") and [l for _, l in _mk][-1] == "10km", str(_mk)
    # 距离缺失或不单调时不给公里刻度（退回时间轴，不编假公里数）
    assert _AC._km_marks([{"t": i, "dist": None} for i in range(20)], 0, 20) is None
    _bad = [{"t": i, "dist": (i * 100 if i < 50 else 5000 - i * 100)} for i in range(100)]
    assert _AC._km_marks(_bad, 0, 100) is None, "距离不单调时仍在标公里"
    # 8) 分圈：最快/最慢行内标记，且零头段不参与评选
    _laps = [
        {"idx": 1, "dist_m": 1000, "dur_s": 300, "pace_s": 300, "hr_avg": 140, "cad": 180},
        {"idx": 2, "dist_m": 1000, "dur_s": 320, "pace_s": 320, "hr_avg": 145, "cad": 180},
        {"idx": 3, "dist_m": 1000, "dur_s": 290, "pace_s": 290, "hr_avg": 150, "cad": 180},
        {"idx": 4, "dist_m": 400, "dur_s": 100, "pace_s": 250, "hr_avg": 155,
         "cad": 180, "is_partial": True},
    ]
    _lh = _AC.lap_rows(_laps)
    assert _lh.count('class="lrow fast"') == 1 and _lh.count('class="lrow slow"') == 1, \
        "最快/最慢段没有行内标记"
    assert 'class="lrow partial"' in _lh, "零头段不再淡化"
    assert "最快段 4'50\"" in _lh, f"最快段没排除零头段：{_lh[-160:]}"
    # 9) 漂移结论排在数据前面
    _dt = _AC._drift_txt({"hr_first": 139, "hr_second": 153,
                          "pace_first": 322, "pace_second": 307}, {}, None)
    assert re.sub(r"<[^>]+>", "", _dt).startswith("心率漂移"), \
        f"漂移结论没有前置：{_dt[:40]}"
    assert "前 5'22\"/139" in _dt, "漂移数据行被改坏了"
    # 10) 爬楼课：关掉配速后不能还留着配速面积和配速刻度
    _cpts = [{"t": 1000 + i * 10, "spd": 2.0, "hr": 140 + (i % 7),
              "ele": 400 + (i % 12) * 3.0, "dist": i * 20.0} for i in range(120)]
    _c1 = _AC.chart_svg(_cpts, 900, 262)
    _c2 = _AC.chart_svg(_cpts, 900, 262, show_pace=False)
    assert 'fill="url(#pfill)"' in _c1, "正常曲线图没有配速面积"
    assert 'fill="url(#pfill)"' not in _c2, "关掉配速后还在画配速面积"
    assert _c2.count("<text") < _c1.count("<text"), "关掉配速后刻度没减少"
    # 11) 爬楼版式只对跑步类生效：徒步/骑行有真实配速，不能被套上爬楼口径
    assert 'work_b0.get("name") == "爬升" and th["is_run"]' in _ac_src, \
        "爬楼版式没有限定运动类型，徒步/骑行会被误降级"
    # 12) 越野不被误判成爬楼：轨迹横跨 2 km 以上就不走降级版式
    _far = [[34.12 + i * 0.001, 108.83 + i * 0.001] for i in range(20)]
    assert _AC._track_span_m(_far) > 2000, "跨度算错了，越野会被当成爬楼"
    assert _AC._track_span_m([]) == 0.0, "空轨迹应返回 0"
    _near = [[34.1200 + i * 0.00002, 108.8300] for i in range(20)]
    assert _AC._track_span_m(_near) < 2000, "楼里的轨迹被算大了"
    # 13) 爬楼课的走势图第二条带是心率，不是步幅
    _spts = [{"t": 1000 + i * 10, "spd": 2.6, "hr": 130 + i % 9, "cad": 188,
              "stride": 91, "dist": i * 26.0} for i in range(60)]
    _sp_stair = _AC.spark_svg(_spts, 400, 140, _AC.theme_for("running"), stair=True)
    _sp_run = _AC.spark_svg(_spts, 400, 140, _AC.theme_for("running"))
    assert "心率" in _sp_stair and "步幅" not in _sp_stair, "爬楼走势图还在画步幅"
    assert "步幅" in _sp_run, "普通跑步的步幅被误删"
    # 爬楼平均速度只有 1.9 m/s，2.2 的门槛会把整段滤掉，这里要能画出来
    _slow = [{"t": 1000 + i * 10, "spd": 1.9, "hr": 130 + i % 9, "cad": 188,
              "stride": 91, "dist": i * 19.0} for i in range(60)]
    assert "无步态时序" not in _AC.spark_svg(
        _slow, 400, 140, _AC.theme_for("running"), stair=True), \
        "慢速爬楼的走势图又被速度门槛滤空了"
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
    assert 'setNegativeButton("直接识别"' not in _kt7, \
        "拍照识别又出现了两个功能相同的按钮（识别 / 直接识别）"
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

    # ---- 日常同步的请求节奏：5 秒里大半是 sleep 和远期课表 --------------
    from garmin_cn import plan as _PLN
    from garmin_cn import appapi as _AA4

    assert _AA4.FAST_MIN_INTERVAL <= _AA4.BULK_MIN_INTERVAL < 0.5, \
        "日常请求间隔没比批量更密，下拉刷新还是被 sleep 拖住"
    assert "_gap = BULK_MIN_INTERVAL if bulk else FAST_MIN_INTERVAL" in _aa_src, \
        "sync() 没用日常间隔常量，改回去就又是 0.5 秒一刀"
    assert len(_PLN._months("2026-11-14", _PLN.NEAR_SPAN_DAYS)) == 3, \
        "日常只该拉「上月 + 当月 + 下月」三个月"
    assert len(_PLN._months("2026-11-14")) >= 6, "全量窗口没覆盖到 140 天"

    class _FakePlanClient(_FakeSyncClient):
        def __init__(self):
            super().__init__()
            self.months: list = []
            self.limits: list = []
            # sync() 末尾要读 client.net.count 对账
            self.net = type("_N", (), {"count": 0})()

        def calendar_month(self, y, m):
            self.months.append((y, m))
            return {"calendarItems": []}

        def active_plans(self, today=None):
            return []

        def activities(self, limit=60, start=0):
            self.limits.append(limit)
            return []

    import datetime as _dtx
    _pc = _FakePlanClient()
    _psvc = _SY.SyncService(_pc, _lst)
    _psvc.sync_plan("2026-11-14")
    assert len(_pc.months) == 7, f"首次该全量补齐远期：拉了 {len(_pc.months)} 个月"
    assert _lst.get_meta(_SY.PLAN_FULL_AT_META), "全量后没记时间，下次又全量"
    _pc.months.clear()
    _psvc.sync_plan("2026-11-14")
    assert len(_pc.months) == 3, f"日常还拉 {len(_pc.months)} 个月，白等往返"
    _lst.set_meta(_SY.PLAN_FULL_AT_META,
                  (_dtx.datetime.now() - _dtx.timedelta(days=8)
                   ).strftime("%Y-%m-%d %H:%M:%S"))
    _pc.months.clear()
    _psvc.sync_plan("2026-11-14")
    assert len(_pc.months) >= 6, "超过一周没全量，远期课表补不回来"
    # 日常增量只拉最近 20 条活动，不再为 60 条的大响应体买单
    _pc2 = _FakePlanClient()
    _SY.SyncService(_pc2, _lst).sync(days=1)
    assert _pc2.limits == [20], f"日常还按 60 条拉活动：{_pc2.limits}"
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
    # 批量加图：文件名只带毫秒 + 每张一个线程 → 同一毫秒的几张写同一个文件，
    # 后一张覆盖前一张，列表里成了「所有照片拼在一起」。
    assert '"img-" + System.currentTimeMillis() + ext' not in _main, \
        "图片文件名只带毫秒，批量选图同一毫秒会互相覆盖"
    assert "imgSeq.incrementAndGet()" in _main, "图片文件名没带唯一序号"
    assert "addImageNow(u, m)" in _main and "for ((u, m) in imgs) {" in _main, \
        "批量加图又改成每张一个线程，几十张大图会一起解码"
    assert 'opt.outHeight, "image", "", takenMs.toDouble()' in _main, \
        "加图没带上相册 DATE_TAKEN，HEIC 重编成 JPEG 后就没拍摄时间了"
    # 素材列表按拍摄时间排：以前按登记顺序，批量选图（相册倒序）就会
    # 「下午拍的排在早上那条前面」。
    from garmin_cn import appapi as _APc

    _ctmp = _Path(tempfile.mkdtemp()) / "clips.json"
    _ctmp.write_text(_json.dumps({"items": [
        {"id": "p", "path": "/tmp/no1", "name": "下午",
         "t_utc": 1_700_000_100_000},
        {"id": "a", "path": "/tmp/no2", "name": "早上",
         "t_utc": 1_700_000_000_000},
    ]}, ensure_ascii=False), encoding="utf-8")
    with _mock.patch.object(_APc, "CLIPS", _ctmp):
        _cl = _APc.clips_list()
    assert [x["name"] for x in _cl["items"]] == ["早上", "下午"], \
        "素材列表没按拍摄时间排序，下午的会排在早上前面"
    # 老素材没有 id：勾选后 id 是空串，批量删除必然报「不在列表里」
    _ctmp2 = _Path(tempfile.mkdtemp()) / "clips_old.json"
    _ctmp2.write_text(_json.dumps({"items": [
        {"path": "/tmp/none-old", "name": "老素材"},
    ]}, ensure_ascii=False), encoding="utf-8")
    with _mock.patch.object(_APc, "CLIPS", _ctmp2):
        _old = _APc.clips_list()["items"][0]
        assert _old["id"].startswith("c") and len(_old["id"]) > 8, \
            "老素材没补稳定 id，勾选后删不掉"
        _rr = _APc.clips_remove_many([_old["id"]])
    assert _rr.get("ok") and _rr.get("removed") == 1, \
        f"老素材批量删除仍然失败：{_rr}"
    assert '"取消全选" else "全选"' in _main, "批量管理没有全选按钮"
    # 用户在手机上的真实场景：两张图 EXIF 被剥掉（或读不到），但文件名是
    # `IMG_20261114_174028` / `IMG_20261114_095054`。文件名必须能兜回时间，
    # 否则 t_utc 为空 → 列表按登记顺序（相册倒序）→ 晚的排在早的前面。
    _late = _APc._name_time_ms("IMG_20261114_174028")
    _early = _APc._name_time_ms("IMG_20261114_095054")
    assert _late and _early and _late > _early, \
        "文件名里的拍摄时刻没解析出来，回放会继续按均布/倒序排"
    assert _APc._name_time_ms("img-1794656752041-3") == 0, \
        "把加入时间戳误当成拍摄时间了"
    _cdir = _Path(tempfile.mkdtemp())
    _cf = _cdir / "clips.json"
    _imgs = {"items": [
        # 登记顺序故意反着来：晚的先加（相册批量选择的返回顺序就是这样）
        {"path": str(_cdir / "a.jpg"), "name": "IMG_20261114_174028",
         "kind": "image"},
        {"path": str(_cdir / "b.jpg"), "name": "IMG_20261114_095054",
         "kind": "image"},
    ]}
    for _x in _imgs["items"]:
        _Path(_x["path"]).write_bytes(b"not-a-real-jpeg")
    _cf.write_text(_json.dumps(_imgs, ensure_ascii=False), encoding="utf-8")
    with _mock.patch.object(_APc, "CLIPS", _cf), \
            _mock.patch.object(_APc, "MEDIA", _cdir):
        _lst = _APc.clips_list()["items"]
    assert [x["name"] for x in _lst] == ["IMG_20261114_095054",
                                         "IMG_20261114_174028"], \
        f"列表顺序仍按登记顺序（晚的在前）：{[x['name'] for x in _lst]}"
    assert all(x.get("t_utc") for x in _lst), "文件名里的时间没写进 t_utc"
    assert all(x.get("t_source") == "文件名" for x in _lst), \
        "补到的时间没标来源，用户无法判断准不准"
    # 回放取素材也必须能靠文件名拿到时间：`img-<毫秒>` 这种没有原始名的
    # 老素材排不进时间序，会按登记顺序（相册倒序）铺开 → 晚的排在早的前面
    _pm = _APc._probe_material(_Path(_imgs["items"][1]["path"]),
                               name_hint="IMG_20261114_095054")
    assert _pm.get("t_utc") and _pm.get("t_source") == "文件名", \
        "回放取素材时图片没走文件名兜底，顺序还会倒"
    assert "_clips_for_motion" in dir(_APc) and "out.sort(" in \
        (ROOT / "garmin_cn/appapi.py").read_text(encoding="utf-8"), \
        "回放取素材没按拍摄时间排序，缺时间的会按登记顺序铺开"
    # 批量删除的 id 过 HTTP 时会被 Kotlin List 的 toString() 变成
    # `"[c1, c2]"` 一个字符串，后端必须能拆回来，否则「勾了也一条都没删掉」
    for _arg in (["c1", "c2"], '["c1", "c2"]', "[c1, c2]", "c1,c2",
                 "c1 c2", [["c1"], "c2"]):
        assert _APc._id_list(_arg) == ["c1", "c2"], \
            f"批量删除的 id 参数没拆开：{_arg!r}"
    assert "JSONArray(ids)" in _main, "批量删除没把 ids 包成 JSONArray"
    assert 'apiCall("clips_remove_many", ids)' not in _main, \
        "又把裸 Kotlin List 传给 apiCall 了，后端只会收到一个字符串"
    # 时间轴的里程必须单调：素材间隔小于加减速步长时，减速段会退回上一个
    # 位置，回放里就是轨迹「冲出去又折返回来」（只有加了照片才出现）。
    _msrc2 = (ROOT / "garmin_cn/motion.py").read_text(encoding="utf-8")
    assert "const decelTo = Math.max(r, phT);" in _msrc2, \
        "减速段还会从加速后的位置退回去"
    assert "const accelTo = Math.min(r + step, nextT, D.dur);" in _msrc2, \
        "加速段会冲出轨迹终点，或冲过下一张照片"
    assert "Number.isFinite(TL.total)" in _msrc2, \
        "总时长没兜底，播放会画一帧就停"
    assert "setTimeout(r, 800)" in _msrc2, \
        "预览定位没超时，界面会卡在某一帧"
    # 导出逐帧循环里绝不能出现裸 toBlob：离屏页面被判不可见时合成回调可能
    # 一直不来，整条循环就永久停住（用户看到「不搓屏幕进度条不动」）。
    assert "await new Promise(res => cv.toBlob" not in _msrc2, \
        "导出还在裸等 toBlob，页面被节流时会永久卡住"
    assert "cv.toDataURL('image/jpeg', 0.82)" in _msrc2, \
        "导出取帧没走同步 toDataURL 兜底"
    assert "if (exportMode) {" in _msrc2 and "await syncVideo(popAt(playT).w)" in _msrc2, \
        "导出期的 seekTo 还在用定时器竞速，会被页面节流拉长"
    assert "view.resumeTimers()" in _main, \
        "离屏 WebView 没有定期恢复定时器，页面会被节流"
    # 同一时段的多张照片聚成一簇、在一个停留窗口里轮播
    assert "(imgs, 25.0)" in _msrc2, "图片素材没按地点聚簇，一张一个点会把时间轴撑长"
    assert "ph._a = p; ph._b = p + ph._hold;" in _msrc2, \
        "没记录停留窗口，轮播取不到当前该显示第几张"
    assert "all.length > 2 && p._b > p._a" in _msrc2, "一个点多张照片没做轮播"
    assert "function popLayout(p, picked)" in _msrc2, "布局没接轮播选中的那一张"
    # 卡片必须跟着标记点走：固定居中时，标记点在哪都一样，
    # 看起来就是「照片和地图上的点没关系」。
    assert "const mky = Math.max(160, Math.min(H - 200, p.y));" in _msrc2, \
        "卡片没跟着标记点定位，展示和标注还是两张皮"
    assert "ctx.arc(mkx, mky, 9, 0, 7)" in _msrc2, \
        "没有从卡片指向标记点的引线，看不出对应关系"
    # 展示过的照片缩成小图留在轨迹旁：列表必须挂 window —— drawFrame 在另一个
    # 作用域里，直接引用 const 会报 "ps is not defined"，整帧都画不出来。
    assert "window.__shown = [];" in _msrc2, \
        "已展示照片列表没挂 window，drawFrame 里会引用不到"
    assert "const SHOWN = window.__shown || [];" in _msrc2, \
        "缩略图没从 window 取列表，跨作用域会崩"
    assert "window.__ps || []" in _msrc2, \
        "缩略图遍历的照片列表没挂 window"
    assert "window.__miniErr" in _msrc2, \
        "缩略图绘制没有异常兜底，一处出错会让车头/卡片整帧不画"
    # 照片密集时小图会叠在一起：必须有多方向 + 由近到远的避让，
    # 且位置只算一次（缓存到 _mx/_my），否则每帧重算会让画面抖。
    assert "const DIRS = [[1, 0], [-1, 0], [0, -1], [0, 1]" in _msrc2, \
        "缩略图没有多方向避让，密集时会堆成一坨"
    assert "const overlap = r =>" in _msrc2, \
        "缩略图没做重叠面积判断"
    assert "p._mx = best.x; p._my = best.y;" in _msrc2, \
        "缩略图位置没有缓存，每帧重算会抖动"
    # 加速段必须知道「下一张在哪」：照片密集时（间隔 < step）原来会冲过去，
    # 下一张只能停在「已经超过自己」的位置 —— 表现就是进度早过了标点、
    # 卡片才弹出来。实测 20 个密集点，修复后「弹出时刻对应的轨迹时间」
    # 与照片自己的 t 偏差为 0。
    assert "const nextT = idx + 1 < ps.length ? tlist[idx + 1] : D.dur;" in _msrc2, \
        "时间轴构建没取下一张的位置，密集照片会被冲过头"
    assert "Math.min(r + step, nextT, D.dur)" in _msrc2, \
        "加速段还能越过下一张照片"
    # 一簇多图要并排显示，不能只画第一张
    assert "const cells = Math.min(Math.max(ns, 1), 2);" in _msrc2, \
        "同一地点多张照片只显示了一张"
    assert "ctx.fillText('+' + (ns - 2), bx, by);" in _msrc2, \
        "多出来的照片没有 +N 角标提示"
    # 缩略图文件名必须带路径指纹：只用 stem 时不同目录的同名照片会落到
    # 同一个 png，后一张盖掉前一张 —— 表现是「同一处两张图内容一样」。
    assert 'src.stem + "-" + _tag + ".png"' in _msrc2, \
        "缩略图文件名没带路径指纹，同名照片会互相覆盖"
    # 照片簇走的是另一条分支，缩略图命名是 "c_" + stem。只修单图分支时，
    # 簇里的同名照片照样互相覆盖 —— 表现就是「并排两格画出来一模一样」。
    assert 'name = "c_" + src.stem + "-" + _tag + ".png"' in _msrc2, \
        "照片簇的缩略图没带路径指纹，两格会画成同一张"
    assert "_seen_src.add(_sk)" in _msrc2, \
        "照片簇没有按路径去重，重复登记会让角标数字虚高"
    # 避让最远只推到 80px，再远就跟标记点没关系了
    assert "for (const d of [24, 38, 52, 66, 80])" in _msrc2, \
        "缩略图避让范围太大，会飞到离标记点很远的地方"
    # 同一文件重复登记要去重
    _asrc = (ROOT / "garmin_cn/appapi.py").read_text(encoding="utf-8")
    assert "if _key in seen_path:" in _asrc, \
        "素材没按路径去重，同一张图会重复出现"
    # 手表暂停 / GPS 丢星会在轨迹上留下采样缺口；落在缺口里的照片原来被挂到
    # 「恢复之后」的位置，看着就是标点比实际拍摄点靠前一大段。
    _psrc2 = (ROOT / "garmin_cn/photo.py").read_text(encoding="utf-8")
    assert "(t[lo] - t[lo - 1]) > 120" in _psrc2, \
        "采样缺口里没取更近的一端，暂停期间的照片会挂到恢复后的位置"
    assert "\"hole_s\": (int(_hole) if _hole else 0)," in _psrc2, \
        "缺口信息没带出去，卡片无法提示位置是估算的"
    assert "_hole = 0          # 每张单独算" in _psrc2, \
        "缺口标记没有逐张重置，会串到下一张照片上"
    # App 生成回放用的是 _synth_route 现场合成的轨迹（不是 detail["route"]）。
    # 它拿 track 的 haversine 里程去 Garmin 序列里二分取时间，两把尺子不等长，
    # 不缩放的话时间轴会被整体压缩——照片「越往后挂得越晚」，13 点跑到 15 点。
    assert "scale = (sdist[-1] / dist[-1]) if dist[-1] > 0 else 1.0" in _asrc, \
        "_synth_route 没把 track 里程缩放到 Garmin 的尺子，时间轴会被压缩"
    assert "bisect.bisect_left(sdist, dd * scale)" in _asrc, \
        "二分还在用未缩放的里程，照片会整体后移"
    assert '"d": [round(x * scale, 1) for x in dist]' in _asrc, \
        "合成轨迹的里程没缩放，卡片公里数会和 Garmin 对不上"
    # 排查期的整串素材日志已经撤掉，别再灌诊断日志
    assert 'log_exc("motion clips"' not in _asrc, \
        "motion clips 的诊断日志又回来了"
    assert "Math.max(492 + ph / 2" not in _msrc2, \
        "卡片又钉回画面中央了"
    assert "1.2 * Math.max(1, (x.shots || []).length)" in _msrc2, \
        "一簇多张的停留没按张数给，会一闪而过"
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
    # token 搬进 design.py 了，断言改看运行时拼出来的 CSS（源码里已经没有裸值）
    from garmin_cn import ui as _UIX
    _css = _UIX.CSS
    assert "--bg:#070B12" in _css and "--brand:#3B82F6" in _css, \
        "页面主色不是深空蓝，科技风主题丢了"
    assert "--cyan:#22D3EE" in _css and "radial-gradient(900px 520px" in _ui_src, \
        "缺青色高光或背景径向渐变"
    assert ".card.brand" in _ui_src, "品牌卡样式没了"
    # 设计语言 §3.1.6 / §7：禁止渐变文字，大数字用实色 + 负字距
    assert "-webkit-text-fill-color:transparent" not in _ui_src, \
        "大数字又用回渐变文字了（docs/设计语言.md §3.1 规则 6 禁止）"
    assert "drop-shadow(0 0 16px" not in _ui_src, \
        "主视觉数字又加外发光了（一页只允许一处发光，且只给 hero）"
    assert ".sec-head h2::before" in _ui_src and ".row .ic{" in _ui_src, \
        "分节标记/图标底座丢了科技风样式"
    # token 已经统一到 design.py，源码里不再有裸色值，改看运行时 CSS
    from garmin_cn import aiui as _AI2
    assert "--bg:#070B12" in _AI2.CSS, "AI 页还是浅色，和 App 不一致"
    assert "D.TOKENS" in _ai_src, "AI 页没引用 design.py 的 token（又自己写了一份色板）"
    # 赛事页曾经整页裸色值（#4FD6A6 / rgba(79,214,166,…)），和主色板对不上。
    # 允许保留的只有 SVG 图表里的具体色（导出时没有 CSS 变量）和 var() 的
    # fallback —— 所以只查 CSS 声明里的裸色。
    _ev_src = (ROOT / "garmin_cn/eventui.py").read_text(encoding="utf-8")
    # 跳过 fill=/stroke= 的行：那是内联 SVG 图表，导出时没有 CSS 变量可用
    _ev_css = "\n".join(l for l in _ev_src.splitlines()
                         if "fill=" not in l and "stroke=" not in l)
    for _bare in ("#4FD6A6", "rgba(79,214,166,", "#D6A75A"):
        assert _bare not in _ev_css, f"赛事页又写了裸色 {_bare}，应走 token"
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
    _new_m = _ms[-1]
    # 最早的月份往往是空的，而空月现在会被折叠掉（设计语言 §4.10），
    # 拿它验「选中行高亮」会永远失败。挑一个有数据、又不是最新的旧月份。
    _old_m = next((m["key"] for m in _hs["months"]
                   if (m.get("count") or 0) and m["key"] != _new_m), _new_m)
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
    assert 'class="prow on"' in _pg_old and f"sel={_old_m}" in _pg_old, \
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
    assert _hb.count('opacity="1"') == 1 and _hb.count('opacity="0.44"') == 1, \
        "柱状图高亮不是「唯一一根实色」"
    assert _hb.index('opacity="1"') < _hb.index('opacity="0.44"'), \
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
    assert "点击柱子查看对应月份" in _pg_list2, "柱状图没提示可以点"
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

    # ---- 高驰：恢复程度顶替身体电量 ------------------------------------
    _b6 = _C.bb_state({"recovery": 72})
    assert _b6["val"] == "72" and _b6["kind"] == "recovery", \
        "高驰恢复程度没有顶到电量格，用户还是看到「未测量」"
    assert _b6["label"] == "恢复程度" and "身体电量" not in _b6["head"], \
        "标签没跟着换，会出现「身体电量 · 恢复程度」这种怪组合"
    _b7 = _C.bb_state({"recovery": 72, "bb_high": 90})
    assert _b7["val"] == "90" and _b7["kind"] == "bb", \
        "同时有电量与恢复程度时应优先电量（两家口径不混用）"

    # ---- 延伸阅读：内容库结构 / 抽取 / 两处渲染 -------------------------
    from garmin_cn import reading as _RD2

    _bad = _RD2.validate()
    assert not _bad, f"延伸阅读内容库不合格：{_bad[:3]}"
    _p1 = _RD2.pick(seed="2026-11-13")
    _p2 = _RD2.pick(seed="2026-11-13")
    assert _p1 and _p1["id"] == _p2["id"], "同一 seed 抽到的条目不稳定（测试没法复现）"
    assert _p1["src"] and _p1["text"], "条目缺正文或数据源"
    _its = _RD2.items(6)
    assert len(_its) == len({x["id"] for x in _its}), "首页轮播里出现了重复条目"

    # 首页「延伸阅读」可以点着换下一条：纯自动轮播有两个毛病——刚看到一半
    # 就被换走，想回头看上一条还得等一整圈。这里守三件事：绑了 click、
    # 点完重置计时（否则刚点完立刻又被自动换掉）、单条时提示不出现。
    _rcard = _UIX._reading_card()
    assert 'addEventListener("click"' in _rcard, "延伸阅读没绑点击切换"
    assert "clearInterval" in _rcard, "点完没重置计时器，会刚点完就被换走"
    assert "轻点换一条" in _rcard and "hidden" in _rcard, \
        "「轻点换一条」应默认隐藏，由 JS 确认多条后再显示"
    assert "s.length<2" in _rcard, "只有一条时不该绑事件，也不该显示提示"

    # ---- 赛事：只展示报名窗口内的 --------------------------------------
    import json as _js3

    _rdir = Path(tempfile.mkdtemp())
    _today2 = dt.date.today()

    def _dd(n: int) -> str:
        return (_today2 + dt.timedelta(days=n)).isoformat()

    (_rdir / "races.json").write_text(_js3.dumps({"version": 1, "updated": _dd(0), "items": [
        {"id": "a", "name": "报名中", "date": _dd(60), "dist_km": 50, "ascent_m": 2000,
         "place": "云南", "reg_open": _dd(-2), "reg_close": _dd(10), "source": "赛事官网"},
        {"id": "b", "name": "未开始", "date": _dd(90), "reg_open": _dd(5), "reg_close": _dd(30)},
        {"id": "c", "name": "已截止", "date": _dd(20), "reg_open": _dd(-60), "reg_close": _dd(-10)},
        {"id": "d", "name": "窗口不明", "date": _dd(40)},
    ]}, ensure_ascii=False), encoding="utf-8")
    _ov_old = _RD2.OVERRIDE
    _RD2.OVERRIDE = _rdir
    try:
        _rs = _RD2.races()
        assert [x["head"] for x in _rs] == ["报名中"], \
            f"报名窗口过滤不对：{[x['head'] for x in _rs]}"
        assert "报名剩 10 天" in _rs[0]["text"], "没写剩余天数"
        assert _RD2.items(20), "轮播池没把赛事并进去"
    finally:
        _RD2.OVERRIDE = _ov_old

    # 窗口顺序写反必须被 validate 拦下（否则「已截止」会被当成长期有效）
    _bdir = Path(tempfile.mkdtemp())
    (_bdir / "races.json").write_text(_js3.dumps({"items": [
        {"id": "x", "name": "反了", "date": "2027-06-01",
         "reg_open": "2027-02-01", "reg_close": "2027-01-15"}]}, ensure_ascii=False),
        encoding="utf-8")
    _b_old = _RD2.BUNDLED
    _RD2.BUNDLED = _bdir
    try:
        assert any("早于" in m for m in _RD2.validate()), "报名窗口顺序错了却没报"
    finally:
        _RD2.BUNDLED = _b_old

    # 抓取解析：一行给全 / 分两行 / 只有截止，都要给对结论
    import importlib.util as _ilu

    _spec = _ilu.spec_from_file_location("races_sync_t", ROOT / "tools" / "races_sync.py")
    _rsm = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_rsm)
    assert _rsm.parse_window("报名时间：2026-11-20 至 2026-12-31", 2026) is not None, \
        "一行给全的报名窗口没解析出来"
    assert _rsm.parse_window("报名开始 2026-11-20\n报名截止 2026-12-31", 2026) is not None, \
        "分两行的窗口没解析出来"
    assert _rsm.parse_window("报名截止：2026-12-31", 2026) is None, \
        "只有截止没有开始，不该当成完整窗口"
    assert _rsm.parse_window("比赛日 2027-03-28，领物 3月26日", 2027) is None, \
        "把比赛日/领物当成报名窗口了"

    # ---- 最酷抓取：解析 / 跨年 / 失败不毁数据 / 轮播配额 ------------------
    from garmin_cn import races_fetch as _RF

    _zc_card = (
        '<div class="event" style="padding-bottom:10px">'
        '<h4 class="name"><a href="https://zuicool.com/event/84480" class="event-a">'
        ' 2026舟山群岛马拉松 </a></h4>'
        '<div class="info"><p>2026.12.06 &middot; 浙江 舟山市 普陀区 普陀大剧院</p></div>'
        '<div class="meta"><p><span>报名截止：11-18 12:00</span></p></div></div>'
    )
    _zc_up = (
        '<div class="event">'
        '<h4 class="name"><a href="https://zuicool.com/event/32833" class="event-a">'
        ' 丽人18（南京站） </a></h4>'
        '<div class="info"><p>2026.12.05 &middot; 江苏 南京市 建邺区 江心洲</p></div>'
        '<div class="meta"><p><span>报名开始：11-13 12:00</span></p></div></div>'
    )
    _zc_old = (
        '<div class="event">'
        '<h4 class="name"><a href="https://zuicool.com/event/1" class="event-a">'
        ' 早就截止了 </a></h4>'
        '<div class="info"><p>2026.11.20 &middot; 北京 海淀区 大觉寺</p></div>'
        '<div class="meta"><p><span>报名截止：10-01 23:59</span></p></div></div>'
    )
    _zc_rows, _zc_st = _RF.parse_list(_zc_card + _zc_up + _zc_old, dt.date(2026, 11, 13))
    assert len(_zc_rows) == 1, f"只该留下在报名中的那一条，实际 {len(_zc_rows)} 条"
    _r0 = _zc_rows[0]
    assert _r0["id"] == "zc-84480" and _r0["reg_close"] == "2026-11-18", _r0
    assert _r0["place"] == "浙江 舟山", f"地名没去掉「市」：{_r0['place']}"
    assert _r0["dist_km"] == 42.2, f"「马拉松」没认出 42.2：{_r0['dist_km']}"
    assert _r0["reg_open_source"] == "inferred", "reg_open 是推的，必须标出来"
    assert _zc_st["upcoming"] == 1 and _zc_st["skipped"] >= 1, _zc_st

    # 跨年：赛事 2027.01.01，截止写「12-10」——年份要回到 2026
    _zc_cross = (
        '<div class="event">'
        '<h4 class="name"><a href="https://zuicool.com/event/9" class="event-a">'
        ' 元旦迎新跑 </a></h4>'
        '<div class="info"><p>2027.01.01 &middot; 江苏 无锡市 滨湖区</p></div>'
        '<div class="meta"><p><span>报名截止：12-10 23:59</span></p></div></div>'
    )
    _cx, _ = _RF.parse_list(_zc_cross, dt.date(2026, 11, 13))
    assert _cx and _cx[0]["reg_close"] == "2026-12-10", \
        f"跨年赛事的报名截止年份推错了：{_cx[0]['reg_close'] if _cx else '没解析出'}"

    # 抓取全挂：绝不能把已有数据抹掉
    _rfdir = Path(tempfile.mkdtemp())
    _rf_old = _RF.OUT
    _RF.OUT = _rfdir / "content" / "races.json"
    try:
        _RF.OUT.parent.mkdir(parents=True, exist_ok=True)
        _RF.OUT.write_text(_js3.dumps({"version": 7, "updated": "2026-11-01",
                                       "items": [{"id": "keep", "name": "旧数据",
                                                  "date": "2026-12-01",
                                                  "reg_open": "2026-11-01",
                                                  "reg_close": "2026-11-30"}]}),
                           encoding="utf-8")

        def _boom(url):
            raise OSError("网络断了")

        _bad = _RF.sync(today=dt.date(2026, 11, 13), opener=_boom)
        assert not _bad.get("ok"), "抓取失败却报成功"
        _after = _js3.loads(_RF.OUT.read_text(encoding="utf-8"))
        assert _after["version"] == 7 and _after["items"][0]["id"] == "keep", \
            "抓取失败把旧赛事数据覆盖掉了"

        # 第 1 页断一次就重试：Errno 103 这类瞬时断连不该让整轮放弃
        _tries = {"n": 0}

        def _flaky(url):
            _tries["n"] += 1
            if _tries["n"] == 1:
                raise OSError("[Errno 103] Software caused connection abort")
            return _zc_card

        _retry, _rst = _RF.fetch(pages=1, per_page=20,
                                 today=dt.date(2026, 11, 13), opener=_flaky)
        assert _retry, "第 1 页抖一次就整轮放弃，旧行为退回来了"
        assert _tries["n"] == 2, f"没有重试：只请求了 {_tries['n']} 次"
        assert not _rst.get("error"), f"重试成功却还留着错误：{_rst.get('error')}"

        # 当天已更新就不再抓
        _RF.OUT.write_text(_js3.dumps({"updated": "2026-11-13", "items": [{"id": "x"}]}),
                           encoding="utf-8")
        _skip = _RF.maybe_update(today=dt.date(2026, 11, 13), opener=_boom)
        assert _skip.get("skipped") == "今天已更新", f"当天重复抓：{_skip}"
    finally:
        _RF.OUT = _rf_old

    # 轮播配额：赛事再多也不能把术语挤出去
    _many = Path(tempfile.mkdtemp())
    _many_items = [{"id": f"r{i}", "name": f"赛事{i}", "date": "2026-12-01",
                    "reg_open": _dd(-1), "reg_close": _dd(5 + i)} for i in range(30)]
    (_many / "races.json").write_text(_js3.dumps({"items": _many_items}),
                                      encoding="utf-8")
    _ov2 = _RD2.OVERRIDE
    _RD2.OVERRIDE = _many
    try:
        _mix = _RD2.items(8)
        _nrace = sum(1 for x in _mix if x.get("kind") == "race")
        assert _nrace <= _RD2.RACE_PER_SLIDE, f"轮播里 {_nrace} 条赛事，术语被挤没了"
        assert len(_mix) == 8, f"轮播没排满：{len(_mix)}"
        assert len(_RD2.races(soon=5)) == 5, "soon 没限住参与抽取的场次"
        assert _RD2.races()[0]["text"].startswith("报名剩"), "倒计时文案不对"
    finally:
        _RD2.OVERRIDE = _ov2

    # 「今天截止」不能写成「报名剩 0 天」

    # ---- 过期清理：文件里的条目要真被删，不能只在展示时过滤 --------------
    _prdir = Path(tempfile.mkdtemp())
    _pr_old = _RF.OUT
    _RF.OUT = _prdir / "content" / "races.json"
    try:
        _RF.OUT.parent.mkdir(parents=True, exist_ok=True)
        _RF.OUT.write_text(_js3.dumps({"version": 3, "updated": _dd(-3), "items": [
            {"id": "a", "name": "还在报", "date": _dd(60),
             "reg_open": _dd(-1), "reg_close": _dd(5), "via": "zuicool"},
            {"id": "b", "name": "今天最后一天", "date": _dd(30),
             "reg_open": _dd(-5), "reg_close": _dd(0), "via": "zuicool"},
            {"id": "c", "name": "昨天截止", "date": _dd(20),
             "reg_open": _dd(-30), "reg_close": _dd(-1), "via": "zuicool"},
            {"id": "d", "name": "自动源没窗口", "date": _dd(40), "via": "zuicool"},
            {"id": "e", "name": "人工没窗口", "date": _dd(40), "via": "manual"},
            {"id": "f", "name": "人工过期", "date": _dd(10),
             "reg_close": _dd(-7), "via": "manual"},
        ]}, ensure_ascii=False), encoding="utf-8")

        _pr = _RF.prune(dt.date.today())
        assert _pr.get("removed") == 3, f"该清 3 条（昨天截止/自动源无窗口/人工过期），实际 {_pr}"
        _left = {i["id"] for i in _js3.loads(_RF.OUT.read_text(encoding="utf-8"))["items"]}
        assert _left == {"a", "b", "e"}, f"清理后剩下的不对：{_left}"
        assert "b" in _left, "今天截止的不能清掉（当天还能报）"
        assert "e" in _left, "人工填的条目即便缺窗口也不该被自动清掉"

        # 没有变化时不重写文件（避免每次启动都动盘）
        _mt = _RF.OUT.stat().st_mtime_ns
        assert _RF.prune(dt.date.today()).get("removed") == 0, "二次清理不该再删"
        assert _RF.OUT.stat().st_mtime_ns == _mt, "没有变化却重写了文件"

        # sync 覆盖写：手工条目要保住，过期的要清掉
        _RF.OUT.write_text(_js3.dumps({"version": 9, "updated": _dd(-1), "items": [
            {"id": "old", "name": "上次抓的", "date": _dd(20),
             "reg_open": _dd(-3), "reg_close": _dd(2), "via": "zuicool"},
            {"id": "keep", "name": "手工维护", "date": _dd(50),
             "reg_open": _dd(-1), "reg_close": _dd(9), "via": "manual"},
            {"id": "dead", "name": "人工但过期", "date": _dd(5),
             "reg_close": _dd(-2), "via": "manual"},
        ]}, ensure_ascii=False), encoding="utf-8")
        _card = ('<div class="event"><h4 class="name">'
                 '<a href="https://zuicool.com/event/777" class="event-a"> 新抓到的 </a></h4>'
                 '<div class="info"><p>2027.12.20 &middot; 浙江 杭州市 西湖区</p></div>'
                 '<div class="meta"><p><span>报名截止：12-01 23:59</span></p></div></div>')
        _sr = _RF.sync(opener=lambda url: _card)
        assert _sr.get("ok"), f"sync 失败：{_sr}"
        _ids = {i["id"] for i in _js3.loads(_RF.OUT.read_text(encoding="utf-8"))["items"]}
        assert "keep" in _ids, "每日覆盖抓取把手工维护的赛事抹掉了"
        assert "dead" not in _ids, "手工条目过期了还留在文件里"
        assert "old" not in _ids and "zc-777" in _ids, "旧的抓取结果没被替换"

        # 「今天已更新」时也要清：过期清理和今天抓没抓无关
        _RF.OUT.write_text(_js3.dumps({"version": 11, "updated": dt.date.today().isoformat(),
                                       "items": [{"id": "gone", "name": "过期了",
                                                  "date": _dd(5), "reg_close": _dd(-2),
                                                  "via": "zuicool"}]},
                                      ensure_ascii=False), encoding="utf-8")

        def _no_net(url):
            raise OSError("不该联网")

        _mu = _RF.maybe_update(today=dt.date.today(), opener=_no_net)
        assert _mu.get("skipped") == "今天已更新", _mu
        assert _mu.get("pruned") == 1, f"就算今天已更新，过期条目也该被清：{_mu}"
        assert not _js3.loads(_RF.OUT.read_text(encoding="utf-8"))["items"], \
            "过期条目没被清掉"
    finally:
        _RF.OUT = _pr_old
    _td = Path(tempfile.mkdtemp())
    (_td / "races.json").write_text(_js3.dumps({"items": [
        {"id": "t", "name": "今天最后一天", "date": _dd(10),
         "reg_open": _dd(-3), "reg_close": _dd(0)}]}), encoding="utf-8")
    _ov3 = _RD2.OVERRIDE
    _RD2.OVERRIDE = _td
    try:
        _one = _RD2.races()
        assert _one and _one[0]["text"].startswith("今天截止"), \
            f"当天截止的文案是「{_one[0]['text'][:12] if _one else ''}」，不该是「剩 0 天」"
    finally:
        _RD2.OVERRIDE = _ov3

    _ch = _C.build_html(res, "2026-11-13 06:24", "Garmin 国区", "自检点评")
    assert '<div class="m1">' not in _ch, "顶部右上角还留着与主图重复的恢复度数字"
    assert '<div class="rd">' in _ch and "font-style:italic" in _ch, \
        "一图流没有延伸阅读，或不是斜体小字"
    assert '<div class="rd"><span>' not in _ch, "延伸阅读还带着单独一行标记"
    # 顶部必须 flex:none：定高 1920 的列里，它一旦被压缩，meta 就会压到 hero 上
    _card_src_now = (ROOT / "garmin_cn/card.py").read_text(encoding="utf-8")
    assert re.search(r"\.top\{\{[^}]*flex:none", _card_src_now), \
        "一图流顶部会被 flex 压缩，延伸阅读会压到睡眠条上"
    # 辉光层必须有渐隐遮罩：否则它 inset 的矩形边会切出一条比底色亮 7 阶的硬边
    from garmin_cn import brand as _BR2
    assert "mask-composite" in _BR2.glow_overlay_css(), \
        "辉光层没有渐隐遮罩，内容区边界会显出一道亮边"
    assert '<div class="sl">' in _ch and '<div class="sr">' in _ch, \
        "今日处方没改成两栏，评语与 AI 点评没挪到右侧"
    _home_rd = U.home_page()
    assert 'id="rc-read"' in _home_rd and "—— " in _home_rd, \
        "首页底部没有延伸阅读卡，或没按「内容 / —— 数据源」排版"

    # ---- 简洁模式：赛事不进一图流 ----------------------------------------
    _sm_dir = Path(tempfile.mkdtemp())
    (_sm_dir / "races.json").write_text(_js3.dumps({"items": [
        {"id": f"s{i}", "name": f"赛事{i}", "date": _dd(30 + i),
         "reg_open": _dd(-1), "reg_close": _dd(2 + i)} for i in range(20)]},
        ensure_ascii=False), encoding="utf-8")
    _ov5 = _RD2.OVERRIDE
    _RD2.OVERRIDE = _sm_dir
    try:
        _hit = sum(1 for _ in range(30) if (_RD2.pick() or {}).get("kind") == "race")
        assert _hit > 0, "对照组没抽到赛事，说明 races 没进池子，后面断言等于没测"
        for _ in range(30):
            _r = _RD2.pick(with_races=False)
            assert not _r or _r.get("kind") != "race", \
                f"with_races=False 仍返回了赛事：{_r}"

        # 一图流：简洁模式下取注脚必须带 with_races=False
        _seen = []

        def _spy(**kw):
            _seen.append(kw)
            return _RD2._items()[0]

        with _mock.patch.object(_RD2, "pick", _spy), \
                _mock.patch("garmin_cn.appapi.simple_mode", lambda: True):
            _C.build_html(res, "2026-11-13 06:24", "Garmin 国区", "自检点评")
        assert _seen and _seen[0].get("with_races") is False, \
            f"简洁模式下取注脚没关掉赛事：{_seen}"

        # 关掉简洁模式要恢复（别把开关做成单向的）
        _seen.clear()
        with _mock.patch.object(_RD2, "pick", _spy), \
                _mock.patch("garmin_cn.appapi.simple_mode", lambda: False):
            _C.build_html(res, "2026-11-13 06:24", "Garmin 国区", "自检点评")
        assert _seen and _seen[0].get("with_races") is True, \
            f"完整模式下把赛事也关掉了：{_seen}"
    finally:
        _RD2.OVERRIDE = _ov5

    _rm = _MT.readiness({"date": "2026-11-13", "sleep_score": 80, "recovery": 55},
                         [])
    _names = [p["name"] for p in _rm["parts"]]
    assert "恢复程度" in _names, f"恢复度没把恢复程度纳入：{_names}"
    assert _rm["score"] is not None and _rm["score"] < 90, \
        "恢复程度偏低却没拉低恢复度"

    # 高驰接口的键名不固定，按包含 recover 的键模糊取值
    from garmin_cn import coros as _CR
    assert _CR._recovery_of({"recoverRate": 66.0}) == 66.0, "recoverRate 没认出来"
    assert _CR._recovery_of({"recoveryPercent": 71}) == 71, "recoveryPercent 没认出来"
    assert _CR._recovery_of({"recovery": 300}) is None, "超出 0-100 的值不该采信"
    assert _CR._recovery_of({}) is None, "空记录不该编一个值"

    # 主要益处：高驰以前固定写「—」，现在按有氧 TE 兜底
    assert ACTCARD.benefit_te(2.4) == "维持" and ACTCARD.benefit_te(0.6) == "热身", \
        "TE 分级兜底不对"
    _ac_src = (ROOT / "garmin_cn/actcard.py").read_text(encoding="utf-8")
    assert '"—" if is_coros else benefit(raw)' not in _ac_src, \
        "高驰的「主要益处」又变回固定的「—」了"
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
        _clip_names = sorted(p["clip"]["name"] for p in _fpl["photos"]
                             if p.get("clip"))
        _shot_names = sorted(s["name"] for p in _fpl["photos"]
                             for s in (p.get("shots") or []))
        # 图片素材现在聚成一个「照片簇」走 shots（页面轮播），视频仍是 clip
        assert _clip_names == ["a", "b", "d"], f"视频没完整插入：{_clip_names}"
        assert "c" in _shot_names, f"图片素材没进照片簇：{_shot_names}"
        # 混音要的磁盘路径必须一直在，且等于页面看到的源（web=True 时只改 src）
        assert all(_Path(p["clip"]["local"]).is_absolute()
                   and p["clip"]["local"] == p["clip"]["src"]
                   for p in _fpl["photos"] if p.get("clip")), \
            "素材没带可解码的本地路径"
        assert not _fpl["loose"], f"素材被判成挂不上了：{_fpl['loose']}"
        # 30 + 8 + 5 + 3 + 4，一张不少
        # 30 巡航 + 视频 8+5+4 + 图片簇 2.8（单张按 2.8s 停）
        assert _fpl["total"] == 49.8, _fpl["total"]
        _d = next(p for p in _fpl["photos"] if (p.get("clip") or {}).get("name") == "d")
        assert _d["clip"]["approx"], "时间对不上的素材没标「约」"
        assert 0 <= _d["t"] <= 120, "越界素材没落到轨迹上"
        # 差几小时的素材不该再挤在起点：光点会从轨道中间跳回 0 秒，
        # 看着就是「路径跑出去又回来」。
        assert _d["t"] > 0, "时间不可信的素材还被贴在起点"

        # ---- 素材锚点：顺序按真实时间、不堆端点、不倒序 ----------------
        _rt2 = {"n": 2, "t0": 1_700_000_000_000, "t": [0, 120],
                "lat": [30.0, 30.002], "lon": [120.0, 120.002],
                "d": [0, 200], "hr": [120, 130], "seg": [0], "v": [1.6, 1.7]}
        # 批量选视频时相册返回倒序：铺设必须按文件名里的拍摄时刻纠正回来
        _rev = [{"name": "VID_20261103_181500", "path": "/tmp/x"},
                {"name": "VID_20261103_070500", "path": "/tmp/y"}]
        _picked = MOT._spread_clips([dict(c) for c in _rev], _rt2)
        _early = next(c for c in _picked if "0705" in c["name"])
        _late = next(c for c in _picked if "1815" in c["name"])
        assert _early["t_utc"] < _late["t_utc"], \
            "倒序添加的无时间素材铺反了，后发生的会先出现"
        # 有真实拍摄时间、但落在吸附窗口外（赛前/赛中/赛后跨几小时）：
        # 整批按比例压进活动时间轴。不许置空均布（位置变估的），也不许
        # 贴到端点（一多就全堆在终点连播）。
        # 窗口内的**一张都不许动**；只有窗外两段压到端口附近的小区间。
        # 用户真实场景：8.6 小时徒步，照片都在活动内，整批被重排就是
        # 「还是均分、位置全错」。
        _span = [{"name": "a", "path": "/tmp/a",
                  "t_utc": _rt2["t0"] + 60_000},
                 {"name": "b", "path": "/tmp/b",
                  "t_utc": _rt2["t0"] + 4 * 3600_000},
                 {"name": "c", "path": "/tmp/c",
                  "t_utc": _rt2["t0"] + 7 * 3600_000}]
        _a_before = _span[0]["t_utc"]
        _n = MOT._fit_clip_times(_span, _rt2["t0"], 120.0,
                                 MOT.CLIP_TZ_PRE_S, MOT.CLIP_TZ_POST_S)
        assert _n == 2, f"只该动窗外那两张：{_n}"
        assert _span[0]["t_utc"] == _a_before, \
            "窗口内的真实时间被重排了（这就是「还是均分」的来源）"
        _rel = [(c["t_utc"] - _rt2["t0"]) / 1000 for c in _span]
        assert _rel == sorted(_rel), "映射把顺序搞乱了"
        assert all(0 < r <= 120 for r in _rel), "映射后跑出了活动时间轴"
        assert _rel[1] < _rel[2], "赛后素材没按先后排开"
        assert _rel[2] <= 120, "赛后素材越过终点"
        # 全都在窗口内时一张都不许动（真实时间最准）
        _inside = [{"name": "x", "path": "/tmp/x",
                    "t_utc": _rt2["t0"] + 30_000}]
        assert MOT._fit_clip_times(_inside, _rt2["t0"], 120.0,
                                   MOT.CLIP_TZ_PRE_S,
                                   MOT.CLIP_TZ_POST_S) == 0, \
            "活动窗内的真实时间被瞎改了"
        # 差整时区的**视频**先归一，不能当成「拍在别的时段」一起压缩
        _tzc = [{"name": "v", "path": "/tmp/v", "kind": "video",
                 "t_utc": _rt2["t0"] + 60_000 + 8 * 3600_000}]
        MOT._fit_clip_times(_tzc, _rt2["t0"], 120.0,
                            MOT.CLIP_TZ_PRE_S, MOT.CLIP_TZ_POST_S)
        assert abs((_tzc[0]["t_utc"] - _rt2["t0"]) / 1000 - 60) < 1, \
            "整时区偏差没在压缩前归一，位置会整体错位"
        # 照片绝不能被时区归一碰：早 7.4 小时那张 +8h 恰好落进窗口，
        # 一改就会排到晚 11 分钟那张**后面**——正是用户反复报的
        # 「下午 5 点的排到早上 10 点前面」。照片的 EXIF 本来就是真时间。
        _ord = [{"name": "m", "path": "/tmp/m", "kind": "image",
                 "t_utc": _rt2["t0"] - 7 * 3600_000 - 24 * 60_000},
                {"name": "e", "path": "/tmp/e", "kind": "image",
                 "t_utc": _rt2["t0"] - 11 * 60_000}]
        MOT._fit_clip_times(_ord, _rt2["t0"], 2886.0,
                            MOT.CLIP_TZ_PRE_S, MOT.CLIP_TZ_POST_S)
        assert _ord[0]["t_utc"] < _ord[1]["t_utc"], \
            "照片被时区归一改错了顺序（早上的排到下午后面）"
        assert "_fit_clip_times(clips, t0_ms, act_dur," in _mo_src, \
            "回放链路没接时间压缩映射，跨时段的照片还是会堆在终点"
        # 差 8 小时（整时区）：保留，交给 photo.match 修正
        _tz = [{"name": "t", "path": "/tmp/t",
                "t_utc": _rt2["t0"] + 8 * 3600_000}]
        MOT._mark_vague_times(_tz, _rt2["t0"], 120.0)
        assert _tz[0]["t_utc"], "整时区偏差被误判成不可信，素材位置会全乱"
        # 无时间的素材只能排在**最后一个可信素材之后**，不能均分到全程去插队：
        # 否则「下午 5 点加的」会被铺到「早上 10 点那张」前面。
        _mix2 = [{"name": "ok", "path": "/tmp/a",
                  "t_utc": _rt2["t0"] + 90_000},
                 {"name": "IMG_20261114_170500", "path": "/tmp/b"},
                 {"name": "IMG_20261114_100500", "path": "/tmp/c"}]
        _mixed = MOT._spread_clips([dict(c) for c in _mix2], _rt2)
        _miss_ts = sorted(c["t_utc"] for c in _mixed if c["name"] != "ok")
        assert _miss_ts[1] - _miss_ts[0] > 10_000, "无时间素材没分散开"
        assert (_miss_ts[0] - _rt2["t0"]) / 1000 < 60, \
            "无时间素材全堆到片尾了（照片一多就全压在终点）"
        _late = next(c for c in _mixed if "1705" in c["name"])["t_utc"]
        _early = next(c for c in _mixed if "1005" in c["name"])["t_utc"]
        assert _early < _late, "无时间素材没按文件名时刻排序"
        # 位置回退（时间对但落回更早的里程）必须被钳回前进方向，
        # 否则光点会「跑出去又回来」。
        _mpts = [(0, 400), (100, 400), (200, 400), (300, 400), (400, 400)]
        _mrt = {"t": [0, 30, 60, 90, 120], "d": [0, 100, 200, 300, 400],
                "lat": [30.0] * 5, "lon": [120.0] * 5}
        _mph = [{"t": 100.0, "d": 300.0, "x": 300, "y": 400},
                {"t": 30.0, "d": 100.0, "x": 100, "y": 400}]
        assert MOT._forward_only(_mrt, _mpts, _mph) == 1, "位置回退没被修正"
        assert _mph[1]["d"] >= _mph[0]["d"], "修正后仍然回退，光点会来回跳"
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
    assert "subtitle.text = \"同步历史记录 · 新增" in _main_src, "标题栏没有进度小字"
    for _gone in ("showSyncRangeDialog", "buildSyncRangeDialog", "mark_backfill",
                  "runHistorySync", "pickDateRange"):
        assert _gone not in _main_src, f"旧的补拉入口还留着：{_gone}"

    # 首页：身体 2×2 + 趋势线 + 页头小按钮，且不再有覆盖区块
    _home = U.home_page()
    assert _home.count('class="rc-cell"') == 4, "身体面板不是 2×2 四格"
    assert _home.count('class="spark"') >= 2, "身体面板没有趋势线"
    assert "7 日欠债" in _home or "评分" in _home, "睡眠格没带关键数值"
    assert "42 天均值" in _home and "28 天基准" in _home, \
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
    assert "本地训练安排" in (ROOT / "garmin_cn/ui.py").read_text(encoding="utf-8"), \
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

    # 往返/折返赛道：同一坐标会经过两次，GPS **分不出是哪一圈**（1.3.6 就是
    # 这样改错的：让 GPS 吸附点同时决定 t，最早的照片被放到最晚展示）。
    # 规则：t 永远取拍摄时间；位置只在「拍摄时刻 ± 窗口」里用 GPS 校准，
    # 距离几乎一样时取时间更近的那个点。
    _T1 = 1_700_000_000_000
    _rtw = {"t0": _T1, "n": 7,
            "t": [0.0, 60.0, 120.0, 200.0, 260.0, 320.0, 380.0],
            "lat": [34.0000, 34.0010, 34.0020, 34.0030, 34.0020, 34.0010,
                    34.0000],
            "lon": [108.0] * 7,
            "d": [0.0, 100.0, 200.0, 300.0, 400.0, 500.0, 600.0]}

    def _wshot(name, sec, gi):
        return {"name": name, "path": "/tmp/" + name, "w": 100, "h": 100,
                "t_utc": _T1 + int(sec * 1000),
                "lat": _rtw["lat"][gi], "lon": _rtw["lon"][gi]}

    _wr = _ph.match([_wshot("ret", 320, 5)], _rtw, t0_ms=_T1, pre_s=300,
                    post_s=900, cluster_m=25.0, clamp_ends=True, keep=())
    _p = _wr["points"][0]
    assert _p["t"] == 320.0, \
        f"时刻被 GPS 改了（{_p['t']}s），最早的照片会排到最后"
    assert _p["d"] == 500.0, \
        f"回程照片吸附到了另一圈（{_p['d']}m，应 500m）"
    _wf = _ph.match([_wshot("out", 60, 1)], _rtw, t0_ms=_T1, pre_s=300,
                    post_s=900, cluster_m=25.0, clamp_ends=True, keep=())
    assert _wf["points"][0]["d"] == 100.0, "去程照片被吸到回程那一圈了"
    # GPS 与时间都不缺，但完全对不上（比如照片其实拍在别处）→ 退回按时间
    _wo = _ph.match([{"name": "off", "path": "/tmp/off", "w": 100, "h": 100,
                      "t_utc": _T1 + 200_000, "lat": 35.5, "lon": 109.5}],
                    _rtw, t0_ms=_T1, pre_s=300, post_s=900, cluster_m=25.0,
                    clamp_ends=True, keep=())
    assert _wo["points"][0]["d"] == 300.0 and _wo["points"][0]["t"] == 200.0, \
        "GPS 完全对不上时没退回按时间定位"
    assert hasattr(_ph, "GPS_WINDOW_S") and _ph.GPS_WINDOW_S <= 60.0, \
        "GPS 时间窗口太宽：位置与时刻会差出几十米，卡片弹出时标注点在身后"
    # 时刻与位置必须取自**同一个轨迹点**，否则「卡片弹出时标注点在别处」。
    # 圈次由 ±60s 的时间窗口保证，不会像 1.3.6 那样吸到另一圈。
    _psrc = (ROOT / "garmin_cn/photo.py").read_text(encoding="utf-8")
    assert '"t": float(rt["t"][i])' in _psrc and '"i": i,' in _psrc, \
        "时刻和位置没取自同一个轨迹点，展示与标注会不同步"
    assert "rel - GPS_WINDOW_S, rel + GPS_WINDOW_S" in _psrc, \
        "吸附没按拍摄时刻开窗，往返赛道会吸到另一圈"
    _msrc3 = (ROOT / "garmin_cn/motion.py").read_text(encoding="utf-8")
    assert "_pos_of(rt, pts, g)" in _msrc3 and "_pos_of(rt, pts, p)" in _msrc3, \
        "回放没用吸附点下标取坐标，GPS 位置又会被按时间重算覆盖"
    assert _msrc3.count("_pos_of(rt, pts,") >= 4, \
        "有素材路径还在按 t 重算坐标，会覆盖 GPS 吸附结果"

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
    # token 拆成了两块 :root（核心 + 旧名别名），要合并后一起找，
    # 只取第一块的话别名里的值会漏掉
    _root = "".join(_re.findall(r":root\{([^}]+)\}", _u.CSS))
    _tok = dict(_re.findall(r"--([a-z0-9-]+)\s*:\s*(#[0-9A-Fa-f]{6})", _root))
    assert _contrast(_tok["ink-3"], _tok["surface"]) >= 4.5, \
        f"辅助色对比度仍不足 AA：{_contrast(_tok['ink-3'], _tok['surface']):.2f}"
    assert _contrast(_tok["ink-2"], _tok["surface"]) >= 4.5, "次要色对比度不足 AA"


    # ---- 设计语言 §7：字号与圆角白名单
    # 以前 ui.py 里 18 档字号、11 档圆角，相邻两档肉眼分不出，层级只能靠猜。
    # 现在只允许 8 档字号、4 档圆角；小元素（<8px 的进度条圆角）不受限。
    from garmin_cn import design as _DS

    # 必须扫**全部页面 CSS**：只查 ui.CSS 时，screen.py 的 13px、eventui 的
    # 8/9/12px 圆角一直是漏网的（各自有一份独立 <style>）。
    from garmin_cn import aiui as _AI3, eventui as _EV3, runsui as _RS3
    from garmin_cn import screen as _SC3

    # ui.CSS 已经拼了 BRAND_CSS + SCREEN_CSS（见 ui.py 的 CSS 定义），所以
    # screen 不单独列；下面断言拼接关系还在，防止哪天拆开后漏检。
    assert getattr(_SC3, "SCREEN_CSS", "") in _u.CSS, \
        "ui.CSS 不再拼接 SCREEN_CSS 了，白名单检查需要把 screen 单列出来"
    _pages_css = {
        "ui": _u.CSS,
        "aiui": getattr(_AI3, "CSS", ""),
        "eventui": getattr(_EV3, "CSS", ""),
        "runsui": getattr(_RS3, "CSS", "") + getattr(_RS3, "FONT_CSS", ""),
    }
    for _nm, _pcss in _pages_css.items():
        if not _pcss:
            continue
        _fss = {float(x) for x in _re.findall(r"font-size:([\d.]+)px", _pcss)}
        _bad_fs = sorted(x for x in _fss if int(x) not in _DS.FONT_SIZES
                         and x not in _DS.FONT_SIZES)
        assert not _bad_fs, \
            f"{_nm} 字号超出白名单（只允许 {_DS.FONT_SIZES}）：{_bad_fs}"
        _rds = {float(x) for x in _re.findall(r"border-radius:([\d.]+)px", _pcss)}
        _bad_rd = sorted(x for x in _rds if x >= 8 and int(x) not in _DS.RADII)
        assert not _bad_rd, \
            f"{_nm} 圆角超出白名单（只允许 {_DS.RADII}）：{_bad_rd}"
        _wts = {int(x) for x in _re.findall(r"font-weight:(\d{3})", _pcss)}
        _bad_wt = sorted(x for x in _wts if x not in (400, 500, 600))
        assert not _bad_wt, \
            f"{_nm} 字重只能用 400/500/600（§3.2）：{_bad_wt}"
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
    # 6 → 7：多出来的是「简洁模式」开关。两个开关（AI 服务 / 简洁模式）都
    # 必须常驻可见，再往上加行得先想清楚是不是该并进别的地方。
    # 7 → 8：「账号」从网格挪进来——它和数据源一样在回答「这台机器连着谁」，
    # 放网格里反而像个工具；同时删掉了网格里重复的「训练小组」入口。
    assert _me.count('class="row') <= 8, f"我的页还是长列表：{_me.count('class=\"row')} 行"
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
    # 运动设置、高驰训练导入、日历提醒分别有独立入口；副标题仍保持单行。
    # 12 → 10：「训练小组」挪到底部 tab（页内不再重复入口），「账号」挪到
    # 「账号与数据」那一组列表行——它回答的是「这台机器连着谁」，不是工具。
    # 2 列网格保持偶数，加单数会让最后一行只挂一张卡，看着像漏了一个。
    assert len(_ads) == 10 and all(len(a) <= 14 for a in _ads), \
        f"网格卡数量或说明长度不对：{_ads}"
    assert any("系统日历" in a for a in _ads), "「我的」页没有日历提醒入口"

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
    # 结论并进了 hero：原来页首一条结论条 + hero 里再说一遍，同一个结论写两遍
    assert 'class="rc-verdict"' not in _h, "独立结论条又回来了（和 hero 重复）"
    assert 'class="rc-hero-t"' in _h and 'class="rc-dot' in _h, \
        "hero 里没有「状态灯 + 结论」那一行"
    for _seg in ("rc-hero-panel", '<section class="rc-load', "rc-body-panel",
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
    for _w in ("28 天基准", "42 天均值", "7 天负荷", "最近 7 天",
               "体重 · 近 7 天"):
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
    # 每次发版要同步改这里（本次 2.0.3 → 171，应用内浏览器登录）。
    assert 'versionCode = 171' in (
        ROOT / "android/app/build.gradle.kts").read_text(encoding="utf-8")

    # 1.3.0 回归：REQ_ATTACH 与 REQ_CP_IMG 都写成 1006。
    # onActivityResult 里 REQ_CP_IMG 分支在前，「传附件」选完文件会直接
    # 走 importCpImage() → POST /__event/cp_ocr，附件根本没上传；
    # 若选的是带表格的图片，还可能把识别结果写进赛道数据。
    _req_pairs = re.findall(
        r"const val (REQ_\w+)\s*=\s*(\d+)",
        (KT / "MainActivity.kt").read_text(encoding="utf-8"))
    assert _req_pairs, "没扫到 startActivityForResult 的请求码常量"
    _used_req: dict[str, str] = {}
    for _name, _code in _req_pairs:
        assert _code not in _used_req, (
            f"请求码 {_code} 被 {_used_req[_code]} 与 {_name} 同时占用："
            "onActivityResult 只会命中靠前的分支，另一个功能会静默走错")
        _used_req[_code] = _name

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
    for _sel, _needle in ((".rc-panel{", "padding:12px"),
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
    # 顶部右上角不再有重复的「恢复度 79 / 100」，改为限宽的 meta + 延伸阅读
    assert "max-width:46%" in _card_src and '.rd{{' in _card_src, \
        "看板一图流顶部右侧没限宽，长来源会挤出画幅 / 延伸阅读没样式"
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
    assert "border-radius:var(--r-2,14px)" in _land_load and \
        "background:var(--surface-2,#111B29)" in _land_load, \
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

    # ---- 2026-11：佳明国区把登录从 /sso/login 迁到 SSO Portal ------------
    # 老接口仍返回 SUCCESSFUL + serviceTicketId，但那张票在 /app?ticket= 换
    # 不出 JWT_WEB（服务端反而 Set-Cookie 清空），网页版/App 续期都会表现为
    # 「账号密码明明对，却一直提示登录失效」。这几条守住新通道别再被改回去。
    assert 'PORTAL_LOGIN_URL = f"{PORTAL}/api/login?{PORTAL_QS}"' in _asrc, \
        "登录没走新版 /portal/api/login"
    assert "_portal_warmup" in _asrc and "PORTAL_SIGNIN_URL" in _asrc, \
        "登录前没预热登录页，会 400 captcha evaluation is mandatory"
    assert "Chrome/131" in _asrc, \
        "登录页要用桌面 Chrome UA，否则 Cloudflare 403"
    assert "self._portal_warmup()" in _asrc.split("def login", 1)[1][:600], \
        "login() 没先预热 Portal 会话"
    assert "def _accept_login" in _asrc and "_finish_ticket(ticket, payload.get(\"serviceURL\"))" in _asrc, \
        "换票没优先用登录响应里的 serviceURL"
    assert "_login_legacy" in _asrc, "新通道挂了要能退回老接口"

    # 换票顺序：serviceURL → /app → /modern（/modern 只做兼容）
    assert "_finish_ticket(self, ticket: str, service_url: str | None = None)" in _asrc

    # ---- 2026-11：机房 IP 过不了佳明登录页，登录态改由设备回传 --------------
    # 服务器（阿里云）访问 sso.garmin.cn/portal/* 被 Cloudflare 挑战，只能让
    # 手机 App 登录后把 session.json 传上去；境外中转是另一条出路。
    _netsrc = (ROOT / "garmin_cn/net.py").read_text(encoding="utf-8")
    assert "def load_cookies" in _netsrc, "没法把外部 Cookie 灌进会话（中转/导入要用）"
    assert "RC_LOGIN_RELAY" in _asrc and "X-Relay-Token" in _asrc, \
        "登录中转没接线"
    _apisrc = (ROOT / "garmin_cn/appapi.py").read_text(encoding="utf-8")
    assert "websession" in _apisrc, "同步成功后没把登录态交给平台"
    _wssrc = (ROOT / "garmin_cn/websession.py").read_text(encoding="utf-8")
    assert "JWT_WEB" in _wssrc and "/v1/web/session" in _wssrc
    # 佳明一个账号同时只认最新会话：App 一重新登录，服务器手上那份立刻 401。
    # 所以「内容变了」必须马上传，只有同一份会话才做防抖。
    assert "DEDUPE_GAP" in _wssrc and "prev == digest" in _wssrc, \
        "登录态变化没立即回传，网页版会一直停在「已过期」"
    assert "内容变了就必须马上传" in _wssrc
    _websrc = (ROOT / "server/app.py").read_text(encoding="utf-8")
    assert "def api_web_session" in _websrc and '"/v1/web/session"' in _websrc, \
        "服务端没有接登录态回传接口"
    _wapp = (ROOT / "server/webapp.py").read_text(encoding="utf-8")
    assert "def _import_post" in _wapp and '"/app/import"' in _wapp, \
        "网页版没有粘贴导入登录态的入口"
    assert "还没有连接数据源，需要先连接一次" in _wapp

    # 网页版活动图：App 靠原生桥 GC.activity(aid) 出图，网页端没有桥，
    # 必须自己接住 data-aid 并渲染 /app/act；1080 宽的整页要用 iframe 缩放，
    # 否则手机上右侧会被裁掉一条。
    assert "def _act_page" in _wapp and '"/app/act"' in _wapp, "网页版没有活动图路由"
    assert "/app/act?aid=" in _wapp, "data-aid 没接到活动图（点了没反应）"
    assert "_ACT_WRAPPER" in _wapp and "postMessage({h:" in _wapp, \
        "活动图没做等比缩放/高度回传，手机上会被裁切"
    assert "def _basemap" in _wapp and "/app/basemap/" in _wapp, \
        "活动图的底图在网页版会 404"
    _wrender = (ROOT / "server/web_render.py").read_text(encoding="utf-8")
    assert 'kind == "act"' in _wrender, "渲染子进程不认识 act"
    _wsync = (ROOT / "server/web_sync.py").read_text(encoding="utf-8")
    assert "_prefetch_details" in _wsync, \
        "同步时不缓存活动明细，登录态一过期活动图就打不开"

    # ---- 登录中转：服务器登不进佳明，靠一台可信设备代登录 -----------------
    # 只在显式配了 RC_RELAY_USER 时生效；密码只在这条链路上临时传递。
    _wsapp = (ROOT / "server/webapp.py").read_text(encoding="utf-8")
    for _need in ("def relay_claim", "def relay_finish", "def request_relay",
                  "def relay_enabled", "RC_RELAY_USER"):
        assert _need in _wsapp or _need in _apisrc, f"中转缺 {_need}"
    assert "def relay_enabled" in _wsapp and "if not relay_enabled():\n        return False" in _wsapp, \
        "没配中转机时不该排任务（会永远卡 pending）"
    _rsrc = (ROOT / "garmin_cn/relay.py").read_text(encoding="utf-8")
    assert "/v1/relay/pull" in _rsrc and "/v1/relay/push" in _rsrc
    assert "tempfile.mkdtemp" in _rsrc, "中转登录必须用临时目录，别顶掉本机会话"
    assert "print(" not in _rsrc and "logger" not in _rsrc, \
        "中转链路不许打印任何东西（会带出别人的密码）"
    _bgsrc = (ROOT / "garmin_cn/bg.py").read_text(encoding="utf-8")
    assert "relay" in _bgsrc and "R.poll()" in _bgsrc, "后台唤醒没去领中转任务"

    # 预热：会话快过期前请中转机提前续，网页才能「一打开就是新的」。
    # 三条约束（只服务活跃用户 / 白天 / App 活着就让路）都要在代码里。
    for _need in ("def preheat_tick", "PREHEAT_AHEAD", "PREHEAT_SEEN",
                  "PREHEAT_APP_ALIVE", "web_seen", "web_push", "def _jwt_exp"):
        assert _need in _wsapp, f"预热缺 {_need}"
    assert "preheat_tick()" in _wsapp.split("def _worker_loop")[1][:400], \
        "worker 循环没调预热"
    assert "note_device_push" in _wsapp and "note_device_push(uid)" in _websrc, \
        "App 推会话时没打点，预热会和它互踢"
    assert "def _fresh_line" in _wsapp and "数据更新于" in _wsapp, \
        "页面没显示数据更新时间"

    # ---- 网页版补齐（2026-11-23）----------------------------------------
    # 运动设置：App 的 /__sports 只认本机，网页版必须自己接一条。
    assert "def _sports_post" in _wsapp and '"/app/sports"' in _wsapp, \
        "网页版没有运动设置"
    assert 'kind == "sports"' in _wrender, "渲染子进程不认识 sports"
    assert 'action="/__sports"' in _wsapp, \
        "运动设置表单还指向只认本机的 /__sports"
    # /w 是独立页壳：从 /app 点「小组」跳过去后底栏会消失，回不来。
    _wsrc = (ROOT / "server/web.py").read_text(encoding="utf-8")
    assert "def _wnav" in _wsrc and "wnav" in _wsrc, \
        "只读组页没有底部导航，从 /app 跳进去就回不来"
    assert '("/w", "小组")' in _wsrc

    # 一个用户可以同时连佳明和高驰。表主键只有 user_id 时，后连的会把先连的
    # 覆盖掉，「我的」页只剩一个源——用户看到的就是「明明连了两个，只显示一个」。
    assert "PRIMARY KEY(user_id, source)" in _wsapp, \
        "web_creds 主键还是 user_id，两个数据源会互相覆盖"
    assert "web_creds_v1" in _wsapp, "老库没有从单源迁移到多源"
    assert "def _creds_all" in _wsapp and "def _creds(uid: int, source" in _wsapp
    assert "ON CONFLICT(user_id,source)" in _wsapp, \
        "保存凭据还在按 user_id 覆盖"
    assert 'for _key, _label in (("garmin", "佳明 Garmin"), ("coros", "高驰 COROS"))' in _wsapp, \
        "我的页只显示一个数据源"
    assert 'name="source" value="{_key}"' in _wsapp, "数据源不能按源单独删除"
    # 二级页必须有返回：只靠底栏的话用户找不到路（原话「进去之后返回无效」）。
    assert 'back=("返回我的", "/app/me")' in _wsapp, \
        "连接数据源页没有返回按钮"
    assert '"/app/me?msg=' in _wsapp, "保存后跳到今日页，用户在「我的」里找不到自己刚做的事"

    # 「正在同步…」不能无限转：任务排队超过 3 分钟还挂着，多半是 worker 卡住，
    # 再转下去就是骗人；同时 worker 要清理卡死的 running 记录。
    assert "began and time.time() - began < 180" in _wsapp, \
        "同步状态没有超时，任务卡住会一直显示「正在同步」"
    assert "def _reset_stuck" in _wsapp and "STUCK_AFTER" in _wsapp, \
        "卡住的 running 任务没人清理"
    # 计划页的「推到佳明手表」指向 App 本机接口，网页上点了 404。
    assert 'action="/__plan/push"' in _wsapp, "计划推送表单还指向只认本机的 /__plan/push"
    assert "def _plan_push_post" in _wsapp and '"/app/plan/push"' in _wsapp

    # ---- 多台中转机 + 无感静默（2026-11-23）--------------------------------
    # RC_RELAY_USER 支持多个邮箱：一台手机被系统冻结时另一台能接。
    assert "RELAY_USERS" in _websrc, "RC_RELAY_USER 只认单台，没有冗余"
    assert "re.split" in _websrc and "RELAY_USERS = {" in _websrc, \
        "多台中转机没做邮箱列表解析"
    assert 'row["phone"] or "").strip().lower() in RELAY_USERS' in _websrc, \
        "中转鉴权没改成多账号集合"
    # 多台机同时领任务：必须靠条件 UPDATE 的 rowcount 抢占，否则会重复登录
    assert "AND status='pending'" in _wsapp and "rowcount != 1" in _wsapp, \
        "中转任务没做抢占，两台机会领到同一个任务"
    # 无感静默：过程状态不出横幅，只有「需要本人动手」才说一句
    assert 'if j.get("status") == "error":' in _wsapp
    assert "同步排队中" not in _wsapp and "上次同步失败" not in _wsapp, \
        "同步过程还在弹横幅，用户会被噪音干扰"
    assert "status=\"wait\"" in _wsapp, "可自愈的失败没静默（还挂在 error 上）"
    assert "def _relay_fatal" in _wsapp and "def _flag_action" in _wsapp, \
        "中转失败没区分「能自愈」和「要人动手」"

    # ---- 组内互助中转（2026-11-23）--------------------------------------
    # 没有白名单的人也能帮队友，但**必须**是「同组 + 自己开了开关」，
    # 默认拒绝。这是唯一一条会把别人的佳明密码送到别人设备上的链路。
    _gsrc = (ROOT / "server/groups.py").read_text(encoding="utf-8")
    for _need in ("relay_share", "def api_relayshare", "def share_relay",
                  "def any_relay_worker", "def is_relay_worker"):
        assert _need in _gsrc, f"组内互助缺 {_need}"
    assert 'if "relay_share" not in mcols' in _gsrc, \
        "group_members.relay_share 没有老库迁移"
    assert "a.relay_share=1" in _gsrc and "a.group_id = b.group_id" in _gsrc, \
        "share_relay 没按「同组 + 开关」判，可能跨组放权"
    assert '"relayshare" and method == "POST"' in _gsrc, "relayshare 没接到路由"
    assert "def relay_claim(worker: int = 0, super_ok: bool = False)" in _wsapp, \
        "relay_claim 没带设备身份，组外设备也能领任务"
    assert "_can_relay(worker, target)" in _wsapp, "领取时没按同组筛选"
    assert "def _groups()" in _wsapp, "组服务挂了会拖死网页版同步"
    assert "worker is not None and int(row[\"worker\"] or 0) != int(worker)" in _wsapp, \
        "交结果没校验归属，猜个 job_id 就能改别人的任务"
    assert "worker: int | None = None" in _wsapp
    _apsrc2 = (ROOT / "server/app.py").read_text(encoding="utf-8")
    assert "G.is_relay_worker(uid)" in _apsrc2, "pull/push 没放开给组内志愿机"
    assert "worker=uid" in _apsrc2, "push 没把设备身份传下去"
    _gui = (ROOT / "garmin_cn/groupui.py").read_text(encoding="utf-8")
    assert "帮队友" in _gui and "/relay" in _gui, "小组设置里没有「帮队友」开关"
    _gpy = (ROOT / "garmin_cn/group.py").read_text(encoding="utf-8")
    assert "def set_relayshare" in _gpy and "relayshare" in _gpy

    # 新用户还没进组：没有任何人能替他登录。这时候**不能**排任务，
    # 否则任务永远卡在 pending，用户等半天以为坏了。
    for _need in ("def relay_workers_for", "def has_group"):
        assert _need in _gsrc, f"组模块缺 {_need}"
    assert "def relay_available" in _wsapp, \
        "只有「全网有没有志愿机」，没有「有没有人能帮我」"
    assert "if not relay_available(uid):" in _wsapp, \
        "没进组的新用户也会排任务，任务永远没人领"
    assert "if not has_group(uid):" in _wsapp, \
        "排不上任务时没说清「先加入小组」，用户不知道下一步"
    assert "你还没加入小组" in _wsapp
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
    # 更新包改由 Python 走 8443 下载（系统下载管理器不认自签证书，明文又会被
    # networkSecurityConfig 拦掉）。这里钉住三件事：走 url_tls、用 ApkProvider
    # 交给安装器、旧下载管理器那条路已经删干净。
    assert "apk_fetch" in _AA._API, "apk_fetch 没进桥接白名单，Kotlin 调不到"
    assert 'apiCall("apk_fetch"' in _kt4, "Kotlin 没走 Python 下载"
    assert "url_tls" in _kt4, "没优先用加密下载地址"
    assert 'content://$packageName.apk/' in _kt4, "安装没走 ApkProvider"
    assert "ApkDownload" not in _kt4, "还在用系统下载管理器那条路"
    _apk_provider = (KT / "ApkProvider.kt").read_text(encoding="utf-8")
    assert 'endsWith(".apk", ignoreCase = true)' in _apk_provider, \
        "ApkProvider 没限制扩展名，等于把私有目录当文件服务器"
    assert ".." in _apk_provider, "ApkProvider 没拦路径穿越"
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

    # ---- 高驰优先：同一天两边都有数据时，佳明不能把高驰的值顶掉
    # 两条同步链路语义不同（高驰 merge、佳明整行覆盖），谁后跑谁说了算。
    # 用户先同步高驰再同步佳明时静息心率会变成佳明的数，看起来像用错了源。
    import json as _json3

    _pdb = Path(tempfile.mkdtemp()) / "prefer.db"
    _ps = _Store3(_pdb)
    _pd = dt.date.today().isoformat()
    _ps.upsert_daily({"date": _pd, "resting_hr": 47, "sleep_total_min": 273,
                      "raw": _json3.dumps({"happenDay": "20261119", "rhr": 47})},
                     merge=True)
    _ps.upsert_daily({"date": _pd, "resting_hr": 52, "sleep_total_min": 300,
                      "steps": 8000,
                      "raw": _json3.dumps({"restingHeartRate": 52})})
    _pr = _ps.conn.execute("SELECT resting_hr, sleep_total_min, steps FROM daily"
                           " WHERE date=?", (_pd,)).fetchone()
    assert _pr["resting_hr"] == 47, \
        f"佳明把高驰的静息心率顶掉了：{_pr['resting_hr']}"
    assert _pr["sleep_total_min"] == 273, "佳明覆盖了高驰已有的睡眠时长"
    assert _pr["steps"] == 8000, "佳明独有的字段没补进来（高驰没给 steps）"
    # 只有佳明的日子仍然整行覆盖，同源重拉能清掉字段
    _pd2 = (dt.date.today() - dt.timedelta(days=2)).isoformat()
    _ps.upsert_daily({"date": _pd2, "resting_hr": 50, "steps": 3000})
    _ps.upsert_daily({"date": _pd2, "resting_hr": 55, "steps": 5000})
    _pr2 = _ps.conn.execute("SELECT resting_hr FROM daily WHERE date=?",
                            (_pd2,)).fetchone()
    assert _pr2["resting_hr"] == 55, "纯佳明的日子不再整行覆盖了"
    try:
        _ps.conn.close()
        _pdb.unlink(missing_ok=True)
    except OSError:
        pass

    _uisrc3 = (ROOT / "garmin_cn/ui.py").read_text(encoding="utf-8")
    _cardsrc3 = (ROOT / "garmin_cn/card.py").read_text(encoding="utf-8")
    assert 'f"静息心率 · {_dd}"' in _uisrc3, "卡片标签没写数据日期"
    assert "数据截至 {_dd} · 下拉同步" in _uisrc3, "旧数据没有可执行提示"
    assert "（未同步今日）" in _cardsrc3, "一图流没标数据日期"
    _tp3 = _UI.home_page()
    assert "静息心率 · 昨夜" not in _tp3 and "静息心率 · " in _tp3
    assert "可能和设备里设置心率区间时用的数值不同" in _tp3, "首页没解释口径差异"

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

    # ---- 简洁模式：只留首页/历史 + 教练的今日与体重 ---------------------
    from garmin_cn import appapi as _AP2
    from garmin_cn import aiui as _AIUI

    _cfgpath = Path(tempfile.mkdtemp()) / "config.json"
    _old_cfg = _AP2.CONFIG
    _AP2.CONFIG = _cfgpath
    try:
        assert _AP2.simple_mode() is False, "简洁模式默认应该是关的"
        assert _AP2.simple_toggle().get("simple_mode") is True, "开关没翻过去"
        _simple_html = _AIUI.render()
        assert _simple_html.count("<button data-v=") == 2, \
            f"简洁模式还有多余标签：{_simple_html.count('<button data-v=')} 个"
        assert ">今日<" in _simple_html and ">体重<" in _simple_html, "今日/体重被误删"
        for _gone in (">对话<", ">备赛<", ">力量<"):
            assert _gone not in _simple_html, f"简洁模式没关掉 {_gone}"
        _AP2.simple_toggle()
        assert _AIUI.render().count("<button data-v=") == 5, "关掉简洁模式后模块没回来"
    finally:
        _AP2.CONFIG = _old_cfg

    _me_simple = U.me_page()
    assert 'data-act="simpleToggle"' in _me_simple, "「我的」页没有简洁模式开关"
    assert 'data-act="simpleToggle"' in _kt7 or '"simpleToggle" -> toggleSimple()' in _kt7, \
        "App 侧没接简洁模式开关"

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

    # ---- 赛事资料：改名 / 编辑 / 时间点 / 软件内预览 / 首页临近提醒 -------
    import json as _js2
    from garmin_cn import event as _EV
    from garmin_cn import eventui as _EVUI

    _etmp = Path(tempfile.mkdtemp())
    _EV.EVENTS = _etmp
    _ed = _etmp / "self-2026"
    _ed.mkdir()
    (_ed / "event.json").write_text(_js2.dumps({
        "slug": "self-2026", "name": "自检赛", "short": "自检", "date": "2026-11-20",
        "start_time": "08:00", "status": "confirmed", "distance_km": 42.2,
        "ascent_m": 120, "duration_h": 6, "goal_s": 13500}, ensure_ascii=False),
        encoding="utf-8")
    _EV.add_doc("self-2026", "rule", "关门与罚则",
                "报名截止 2026-11-18 18:00，领物 11月19日 10:00-20:00")
    _tl = _EV.timeline("self-2026")
    _ats = [(x["at"], x["label"]) for x in _tl]
    assert ("2026-11-18T18:00", "报名截止") in _ats, f"资料里的报名截止没认出来：{_ats}"
    assert ("2026-11-19T10:00", "领物") in _ats, f"中文日期没认出来：{_ats}"
    assert ("2026-11-20T08:00", "开赛") in _ats, "赛事开赛时间没进时间线"
    assert _EV.parse_times("配速 4-5 分，心率 150-160", 2026) == [], \
        "把配速/心率区间当成日期了"

    _EV.update_doc("self-2026", 0, title="关门与罚则（改）")
    assert _EV.docs("self-2026")["items"][0]["title"] == "关门与罚则（改）", "改标题没生效"
    (_ed / "参赛号.png").write_bytes(b"\x89PNG\r\n")
    assert _EV.rename_file("self-2026", "参赛号.png", "号码布") == "号码布.png", "附件改名失败"
    assert not (_ed / "参赛号.png").exists(), "改名后旧文件还在"

    _eh = _EVUI.render_page({"slug": ["self-2026"]})
    assert 'value="edit"' in _eh, "资料页没有「改」表单，名称改不了"
    assert 'class="dtime"' in _eh, "资料页没把识别到的时间点显示出来"
    assert "/event-doc?slug=self-2026" in _eh, "附件没有软件内预览入口"
    assert 'action="/__event/file"' in _eh, "附件没有改名入口"
    assert 'class="dimg"' in _EVUI.render_doc_page(
        {"slug": ["self-2026"], "name": ["号码布.png"]}), "图片预览打不开"
    (_ed / "规程.pdf").write_bytes(b"%PDF-1.4")
    assert "系统应用打开" in _EVUI.render_doc_page(
        {"slug": ["self-2026"], "name": ["规程.pdf"]}), "PDF 没给系统打开的兜底"
    # 改名把路径成分剥掉（不报错，但绝不能把文件挪出赛事目录）
    _esc = _EV.rename_file("self-2026", "规程.pdf", "../../逃逸")
    assert _esc == "逃逸.pdf" and (_ed / _esc).is_file(), \
        "改名没剥掉路径成分，文件可能被挪出赛事目录"

    # ---- 删除：文字资料要有按钮，附件也要有，且服务端三道关 -------------
    assert 'aria-label="删除这条资料"' in _eh, "文字资料没有删除按钮"
    assert "删除附件" in _eh and 'name="act" value="del"' in _eh, \
        "附件没有删除按钮"
    assert "confirm(&quot;删掉" in _eh, "删除没带二次确认（误点就真删了）"

    # ⚠ 同名方法会静默互相覆盖：`_event_file` 是 GET 读附件的（收 path），
    # 写操作必须换个名字，否则 /event-file/... 一律 500（踩过一次）。
    import inspect as _insp

    from garmin_cn import serve as _SV

    _params = list(_insp.signature(_SV._Handler._event_file).parameters)
    assert _params == ["self", "path"], \
        f"`_event_file` 被写操作覆盖了（现在的参数是 {_params}），附件读不出来"
    assert hasattr(_SV._Handler, "_event_file_op"), "附件写操作的方法名又改了"

    _del_target = _ed / "逃逸.pdf"
    assert _EV.remove_file("self-2026", "逃逸.pdf") == "逃逸.pdf", "附件删不掉"
    assert not _del_target.exists(), "删完文件还在"
    for _bad in ("../event.json", "..", "event.json", "不存在.pdf"):
        try:
            _EV.remove_file("self-2026", _bad)
            raise AssertionError(f"remove_file 没拦住：{_bad}")
        except ValueError:
            pass
    assert (_ed / "event.json").is_file(), "删除顺带把赛事档案删了"

    # ---- GPX：只有路线、没有途经点也要能用（马拉松基本都是这样） ------
    import math as _mth

    def _mk_gpx(points, wpts=()):
        body = "".join(
            f'<trkpt lat="{a}" lon="{b}"><ele>{c}</ele></trkpt>' for a, b, c in points)
        w = "".join(
            f'<wpt lat="{a}" lon="{b}"><name>{nm}</name></wpt>'
            for a, b, nm in wpts)
        return ('<?xml version="1.0"?><gpx version="1.1" creator="selftest" '
                'xmlns="http://www.topografix.com/GPX/1/1">' + w
                + "<trk><trkseg>" + body + "</trkseg></trk></gpx>")

    # 42 km 直线，海拔锯齿状起伏（保证滤波后仍有爬升）
    _mpts = [(30.0, 0.4376 * i / 299, round(30 + 8 * _mth.sin(i / 9.0), 1))
             for i in range(300)]
    (_ed / "track.gpx").write_text(_mk_gpx(_mpts), encoding="utf-8")
    _mg = _EV.parse_gpx(_ed / "track.gpx", slug="self-2026")
    assert _mg["waypoints"] == [], "这份 GPX 本来就没有 wpt，不该解出途经点"
    assert _mg["ascent_m"] > 0, "没有途经点时爬升也要照常算"

    _mh = _EVUI.render_page({"slug": ["self-2026"]})
    assert "只有路线、没有途经点" in _mh, "无途经点的 GPX 没有给出说明，用户会以为导不进"
    assert "相差 -42" not in _mh and "相差 -4" not in _mh, \
        "没有 CP 表时算出了假的里程差异（基准取了 0）"
    assert 'stroke-dasharray="3 4"' in _mh, "剖面没有里程刻度，长坡看不出在第几公里"

    # 带途经点时不该再提示「只有路线」
    _mpts2 = [(30.0, 0.4376 * i / 299, round(30 + 8 * _mth.sin(i / 9.0), 1))
              for i in range(300)]
    (_ed / "track.gpx").write_text(
        _mk_gpx(_mpts2, [(30.0, 0.1, "10K"), (30.0, 0.3, "30K")]), encoding="utf-8")
    _wg = _EV.parse_gpx(_ed / "track.gpx", slug="self-2026")
    assert len(_wg["waypoints"]) == 2, f"途经点没解析出来：{_wg['waypoints']}"
    assert "只有路线、没有途经点" not in _EVUI.render_page({"slug": ["self-2026"]}), \
        "明明有途经点，还在提示「只有路线」"

    # 官方「路线」格式：<rte><rtept>，以及 GPX 1.0 命名空间
    _rte = "".join(f'<rtept lat="30" lon="{0.44 * i / 99}"><ele>{20 + i % 9}</ele>'
                   f'</rtept>' for i in range(100))
    for _label, _head in (
        ("rte 1.1", '<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">'),
        ("rte 1.0", '<gpx version="1.0" xmlns="http://www.topografix.com/GPX/1/0">'),
        ("无命名空间", '<gpx version="1.1">'),
    ):
        (_ed / "track.gpx").write_text(
            '<?xml version="1.0"?>' + _head + "<rte>" + _rte + "</rte></gpx>",
            encoding="utf-8")
        _rr = _EV.parse_gpx(_ed / "track.gpx", slug="self-2026")
        assert _rr["points"] == 100, f"{_label} 的 rtept 没解析出来（{_rr['points']} 点）"
        assert _rr["total_km"] > 40, f"{_label} 距离算错了：{_rr['total_km']}"

    # 只有 5 个点：报错要说清找到了几个，别只说「少于 10 个」
    (_ed / "track.gpx").write_text(
        '<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1"><rte>'
        + "".join(f'<rtept lat="30" lon="{0.1 * i}"/>' for i in range(5))
        + "</rte></gpx>", encoding="utf-8")
    try:
        _EV.parse_gpx(_ed / "track.gpx", slug="self-2026")
        raise AssertionError("5 个点的 GPX 居然通过了")
    except ValueError as _e:
        assert "5 个坐标点" in str(_e), f"报错没说清找到了几个点：{_e}"

    # 刻度疏密跟着赛程：全马 5 km、百公里 10 km
    assert _EVUI._spark([[0, 10], [20, 20], [40, 15]], 5).count('stroke-dasharray') == 7, \
        "全马该按每 5 km 打刻度"
    (_ed / "track.gpx").unlink()

    # ---- 行程与预订：分类、跨年、页面渲染 -------------------------------
    E2 = _EV  # 同一个模块
    (E2._dir("self-2026") / "docs.json").write_text(_js3.dumps({"items": [
        {"title": "去程", "kind": "logistics", "at": "", "url": "",
         "body": "航班 CA1883 3月25日 08:30 首都T3 起飞\n"
                 "酒店 3月25日 入住 都江堰锦江之星\n"
                 "高铁 G8712 3月24日 14:05 成都东站"},
        {"title": "领物", "kind": "rule", "at": "", "url": "",
         "body": "领物 3月26日 10:00 开始，17:00 截止"},
    ]}, ensure_ascii=False), encoding="utf-8")
    _ev2 = E2.load_event("self-2026")
    _ev2["date"] = "2027-03-27"
    (E2._dir("self-2026") / "event.json").write_text(
        _js3.dumps(_ev2, ensure_ascii=False), encoding="utf-8")

    _tp = E2.trips("self-2026", today="2026-11-13")
    _by_kind = {x["kind"]: x for x in _tp}
    assert _by_kind.get("flight"), f"航班没识别出来：{_tp}"
    assert _by_kind.get("train"), f"高铁没识别出来：{_tp}"
    assert _by_kind.get("stay"), f"住宿没识别出来：{_tp}"
    assert _by_kind.get("race"), f"领物/开赛没识别出来：{_tp}"
    # 跨年：资料写「3月25日」，赛事在 2027 —— 不能算成 2026-03-25（已过期）
    assert _by_kind["stay"]["at"].startswith("2027-03-25"), \
        f"跨年日期没按赛事年份推：{_by_kind['stay']['at']}"
    # 分类不能串行：酒店那行不能被当成航班
    assert _by_kind["stay"]["text"].startswith("酒店"), \
        f"住宿那条取到了别的行：{_by_kind['stay']['text']}"

    _th = _EVUI.render_page({"slug": ["self-2026"]})
    assert "行程与预订" in _th, "赛事页没有「行程与预订」区块"
    assert 'class="tk stay"' in _th and 'class="tk flight"' in _th, "行程标签没渲染"

    # 临期高亮：3 天内标红
    _soon = E2.trips("self-2026", today="2027-03-24")
    assert _soon and _soon[0]["days"] == 0, f"当天那件事没排在最前：{_soon[:1]}"

    # ---- 手填行程 / 住宿：入口、定位、坐标、提醒、地图标注 ---------------
    from garmin_cn import basemap as _BM3
    import urllib.parse as _upx

    # 高德地理编码返回 GCJ，存下来的是 WGS：正反算要能还原
    _glon, _glat = _BM3.wgs84_to_gcj02(104.05, 30.66)
    _wlon, _wlat = _BM3.gcj02_to_wgs84(_glon, _glat)
    assert abs(_wlon - 104.05) < 1e-6 and abs(_wlat - 30.66) < 1e-6, \
        "GCJ→WGS 反算不准，住宿会标偏几百米"
    assert _EV._norm_at("2027/3/25 15:00") == "2027-03-25T15:00", \
        "手填时间没归一"
    assert _EV._norm_at("2027-03-25") == "2027-03-25T00:00", \
        "只填日期时该当作 00:00"
    try:
        _EV._norm_at("下周三")
        raise AssertionError("不合法日期居然通过了")
    except ValueError:
        pass

    # 不联网：把地理编码、城市反查、合理性判断都替成固定结果
    with _mock.patch.object(_BM3, "geocode",
                            lambda *a, **k: {"lat": 30.67, "lon": 104.06,
                                             "name": "测试酒店"}), \
            _mock.patch.object(E2, "_trip_city", lambda slug: "成都"), \
            _mock.patch.object(E2, "_trip_plausible", lambda *a, **k: True):
        _ti = _EV.trip_add("self-2026", "stay", "2027-03-25 15:00",
                           "入住 都江堰锦江之星", "都江堰锦江之星")
    assert _ti.get("lat") == 30.67 and _ti.get("lon") == 104.06, \
        "住宿地址没落到坐标上"
    assert len(_EV.trip_manual("self-2026")) == 1, "手填行程没存进 trip.json"
    _tp3 = E2.trips("self-2026", today="2026-11-13")
    assert any(x.get("manual") and x["kind"] == "stay" for x in _tp3), \
        "手填住宿没出现在行程列表"
    assert sum(1 for x in _tp3
               if x["kind"] == "stay" and x["at"][:10] == "2027-03-25") == 1, \
        "资料里同一天的住宿和手填的重复了"
    assert any(r["src"] == "行程" for r in E2.timeline("self-2026")), \
        "手填行程没进首页「临近」提醒"

    # 同名地点被解析到外省时，宁可不标：否则底图取景会被拉到 2000 km 外
    _stat_backup = E2._read("self-2026", "track_stats.json")
    E2._write("self-2026", "track_stats.json",
              {"polyline": [[30.0 + i * 0.001, 120.0 + i * 0.001]
                            for i in range(20)]})
    assert _EV._trip_plausible("self-2026", 30.02, 120.02), \
        "赛道边的住宿被判成不合理坐标"
    assert not _EV._trip_plausible("self-2026", 26.36, 100.14), \
        "解析到云南的同名地点没被拦住，整张底图会被拉坏"
    if _stat_backup is not None:
        E2._write("self-2026", "track_stats.json", _stat_backup)

    # 三个点：起点、终点、住宿；每个都能点开高德，且用 GCJ 坐标
    _pins = _EVUI._course_pins("self-2026", [[30.0, 120.0], [30.1, 120.1]])
    assert [p["label"] for p in _pins[:2]] == ["起点", "终点"], \
        f"起终点没标上：{_pins[:2]}"
    assert len(_pins) == 3 and _pins[2]["kind"] == "stay", \
        f"住宿没标上：{_pins}"
    # 机场 / 车站虽然也有坐标，但不能上赛道图：几十公里外会把 105K 赛道
    # 挤成画幅一角的一小团。它们只在行程列表里各自带导航按钮。
    _raw_pois = _EV.trip_pins("self-2026")
    with _mock.patch.object(
            _EV, "trip_pins",
            lambda *a, **k: _raw_pois + [
                {"kind": "flight", "label": "返程航班", "place": "机场",
                 "lat": 30.5, "lon": 120.5, "at": ""}]):
        _pins_far = _EVUI._course_pins(
            "self-2026", [[30.0, 120.0], [30.1, 120.1]])
    assert len(_pins_far) == 3 and all(
        p["kind"] != "flight" for p in _pins_far), \
        "机场被画进赛道图，底图取景会被拉到整个城市"
    assert all("uri.amap.com/marker" in p["url"] for p in _pins), \
        "标注点该是单点「看位置」，导航留给图下的按钮"
    assert "uri.amap.com/marker" in _EVUI._amap_url(30.0, 120.0, "测试", nav=False), \
        "「看位置」应该走单点标注而不是路径规划"
    _pq = _upx.parse_qs(_upx.urlparse(
        _EVUI._amap_url(30.0, 120.0, "测试", nav=True)).query)
    _to = _pq["to"][0].split(",")
    _plon, _plat = float(_to[0]), float(_to[1])
    assert _pq["callnative"][0] == "1", "没让高德尝试唤起 App"
    _elon, _elat = _BM3.wgs84_to_gcj02(120.0, 30.0)
    assert abs(_plon - _elon) < 1e-5 and abs(_plat - _elat) < 1e-5, \
        "高德 URI 传了 WGS 坐标，导航会偏到隔壁街"
    # 原生直达：网页 URI 只作兜底，App 里点标注必须发 androidamap://
    _an = _EVUI._amap_act(30.0, 120.0, "测试,起点")
    _av = _EVUI._amap_act(30.0, 120.0, "测试,起点", nav=False)
    assert _an.startswith("amapNav:") and _av.startswith("amapView:"), \
        "高德直达动作名不对，Kotlin 侧收不到"
    _ap = _an.split(":", 1)[1].split(",")
    assert len(_ap) == 3, "载荷必须是 经度,纬度,名称 三段"
    assert abs(float(_ap[0]) - _elon) < 1e-5 and \
           abs(float(_ap[1]) - _elat) < 1e-5, \
        "原生动作传了 WGS 坐标；高德 dev=0 会偏 400~500 m"
    assert _upx.unquote(_ap[2]) == "测试 起点", \
        "地名里的逗号没清掉，Kotlin split 会切错载荷"
    assert all(p.get("act", "").startswith("amapView:") for p in _pins), \
        "赛道图标注应该只做「看位置」：导航有图下那排按钮，点标注别再直接起步"
    assert _EVUI._pin_tag([]) == ""
    assert _EVUI._pin_tag([[30.67, 104.06]]) == \
           _EVUI._pin_tag([[30.67, 104.06]]), "同样的 POI 每次换缓存名会反复取图"
    assert _EVUI._pin_tag([[30.67, 104.06]]) != \
           _EVUI._pin_tag([[30.68, 104.06]]), "POI 变了还命中旧底图，住宿会落到画幅外"

    # 没有底图 Key 时也要出标注（矢量模式），并且取景把住宿一起算进去
    from garmin_cn import actcard as _AC3

    _pin_near = [dict(p) for p in _pins]
    _pin_near[2]["lat"], _pin_near[2]["lon"] = 30.05, 120.15
    _svg_pins = _AC3.track_svg([[30.0, 120.0], [30.1, 120.1], [30.2, 120.0]],
                               720, 360, pins=_pin_near)
    assert "起点" in _svg_pins and "终点" in _svg_pins, \
        "矢量俯视图上的起终点标注没渲染"
    assert _svg_pins.count("<a href=") >= 3, \
        "三个点都得能点，不能只画个圆"
    assert _svg_pins.count('data-act="amapView:') >= 3, \
        "SVG 标注没带「看位置」动作，手机上点标注仍会跳网页"
    # 图上圆点太小，还得有一排大按钮兜底
    with _mock.patch.object(_BM3, "available", lambda: False):
        _map_html = _EVUI._course_map(
            "self-2026", {"polyline": [[30.0, 120.0], [30.1, 120.1]]})
    assert "导航到起点" in _map_html and "导航到终点" in _map_html, \
        "赛道图旁边没有起终点导航按钮"
    assert "导航到住宿" in _map_html, "住宿没进赛道图的导航按钮"
    assert "导航到高铁" not in _map_html and "导航到航班" not in _map_html, \
        "机场/车站又混进赛道图导航按钮了"
    assert 'data-act="amapNav:' in _map_html, \
        "赛道图快捷导航按钮没接原生动作"
    assert 'data-act="amapView:' in _map_html, \
        "地图标注没改成看位置，会和下面的导航按钮重复"
    _trip_html_amap = _EVUI._trips_html("self-2026", "")
    assert 'data-act="amapNav:' in _trip_html_amap and \
           'data-act="amapView:' in _trip_html_amap, \
        "行程列表的「导航 / 看位置」还是纯网页链接"
    assert 'href="https://uri.amap.com/' in _trip_html_amap, \
        "网页兜底链接被删了，桌面浏览器和没装高德的手机会点不动"
    _kt_amap = (KT / "MainActivity.kt").read_text(encoding="utf-8")
    assert 'name.startsWith("amapNav:")' in _kt_amap and \
           'name.startsWith("amapView:")' in _kt_amap, \
        "JS 桥没接高德直达动作"
    assert 'androidamap://navi?' in _kt_amap and \
           'androidamap://viewMap?' in _kt_amap and 'dev=0' in _kt_amap, \
        "没按官方 scheme 唤起高德 App（或坐标偏移标记不对）"
    assert 'setPackage(AMAP_PKG)' in _kt_amap and \
           'const val AMAP_PKG = "com.autonavi.minimap"' in _kt_amap, \
        "没限定高德包名，可能弹应用选择框或被别的 App 截走"
    _mf_amap = (ROOT / "android/app/src/main/AndroidManifest.xml").read_text(
        encoding="utf-8")
    assert "<queries>" in _mf_amap and "com.autonavi.minimap" in _mf_amap, \
        "Android 11+ 没声明包可见性：装了高德也会被 resolveActivity 判成没装"
    from garmin_cn import ui as _UI_AMAP
    assert "e.preventDefault()" in _UI_AMAP.BRIDGE_JS, \
        "没拦掉 <a href> 默认跳转，会既唤起 App 又开一次网页"
    # 空行程时「行程与预订」区块必须还在，且添加入口默认展开
    with _mock.patch.object(E2, "trips", lambda *a, **k: []), \
            _mock.patch.object(_BM3, "available", lambda: False):
        _empty_trip_html = _EVUI.render_page({"slug": ["self-2026"]})
    assert "行程与预订" in _empty_trip_html, \
        "没行程就把整块藏了，用户找不到入口"
    assert "加一条行程" in _empty_trip_html and "<details class=\"fold\" open>", \
        "空行程时添加表单没默认展开"

    with _mock.patch.object(_BM3, "geocode",
                            lambda *a, **k: {"lat": 30.68, "lon": 104.07,
                                             "name": "新地址"}), \
            _mock.patch.object(E2, "_trip_city", lambda slug: "成都"), \
            _mock.patch.object(E2, "_trip_plausible", lambda *a, **k: True):
        assert _EV.trip_geo("self-2026", 0, "换个地址"), "补地址没返回条目"
    assert _EV.trip_manual("self-2026")[0]["lat"] == 30.68, "重新定位没写回坐标"
    assert _EV.trip_del("self-2026", 0), "手填行程删不掉"
    assert not _EV.trip_manual("self-2026"), "删完还留着空壳"

    # ---- 预订提醒：按比赛日倒推、填行程自动已订、手动覆盖 --------------
    assert _EV.BOOKING_LEAD == {"flight": 60, "train": 15, "stay": 45,
                                "race": 0, "other": 0}, "默认提前量被改了"
    # 赛事日 2027-03-27。建好赛事、一条行程都没填，就该有这三条提醒——
    # 1.3.1 前反过来，必须先填好「订了哪趟航班」才生成待办。
    _bk = _EV.bookings("self-2026", today="2027-01-01")
    assert len(_bk) == 3, f"建好赛事就该有三条预订提醒：{_bk}"
    assert all(b["race"] == "2027-03-27" for b in _bk), "锚点不是比赛日期"
    _due = {b["kind"]: b["due"] for b in _bk}
    assert _due == {"flight": "2027-01-26", "stay": "2027-02-10",
                    "train": "2027-03-12"}, f"按比赛日倒推错了：{_due}"
    assert [b["kind"] for b in _bk] == ["flight", "stay", "train"], \
        "待办该按截止日排"
    assert all(not b["booked"] for b in _bk), \
        "资料里认出来的推荐航班/酒店不该被当成用户已订"
    assert all(b["label"] == _EV.BOOKING_LABELS[b["kind"]] for b in _bk), \
        "待办该是「订机票」这类通用提醒，不该绑具体行程"
    # 赛事级可改：只改航班，其它两项保持
    assert _EV.set_booking_rules("self-2026", {"flight": "10"})["flight"] == 10
    assert _EV.booking_rules("self-2026")["stay"] == 45, "改航班把住宿也改了"
    assert {b["kind"]: b["due"] for b in
            _EV.bookings("self-2026", today="2027-01-01")}["flight"] == \
        "2027-03-17", "改完规则没按比赛日重算"
    try:
        _EV.set_booking_rules("self-2026", {"train": "-3"})
        raise AssertionError("负的提前量居然收下了")
    except ValueError:
        pass
    # 填了对应类型的预订信息 → 自动视为已订，不用再手点一次
    _EV.trip_add("self-2026", "flight", "2027-03-25 08:30", "CA1883 起飞")
    _EV.trip_add("self-2026", "train", "2027-03-25 14:05", "G8712 成都东")
    _EV.trip_add("self-2026", "stay", "2027-03-26 15:00", "入住 都江堰")
    _auto = {b["kind"]: b
             for b in _EV.bookings("self-2026", today="2027-01-01")}
    assert all(b["booked"] and b["auto"] for b in _auto.values()), \
        "填了预订信息没自动视为已订"
    assert "自动视为已订" in _auto["stay"]["reason"], "没说明为什么停了提醒"
    assert not _EV.bookings("self-2026", today="2027-01-01",
                            include_booked=False), "自动已订了还在催"
    # 手动覆盖：填了但只是计划 → 仍需提醒；没填也能直接关掉
    _EV.set_booking_done("self-2026", "stay", done=False)
    assert [b["kind"] for b in _EV.bookings(
        "self-2026", today="2027-01-01", include_booked=False)] == ["stay"], \
        "手动「仍需提醒」没盖掉自动已订"
    _EV.set_booking_done("self-2026", "stay", done=True)
    assert not _EV.bookings("self-2026", today="2027-01-01",
                            include_booked=False), "标「不用提醒」没生效"
    _EV.set_booking_rules("self-2026", {"flight": "60"})
    assert "stay" in _EV._booking_prefs("self-2026")["done"], \
        "改提前量把手动覆盖抹掉了"
    # 过期不消失（最该催的就是这条），但比赛结束后不再催
    _late = _EV.bookings("self-2026", today="2027-03-20")
    assert len(_late) == 3 and all(b["overdue"] and b["days"] < 0
                                   for b in _late), \
        "截止日过了待办就消失，等于白提醒"
    assert _EV.bookings("self-2026", today="2027-04-01") == [], \
        "比赛都过了还在催订机票"
    # 只留住宿行程 → 航班/高铁该催，住宿自动已订
    _EV._write("self-2026", _EV.BOOKING_FILE,
               {"lead": dict(_EV.BOOKING_LEAD)})
    _EV._trip_save("self-2026",
                   [x for x in _EV.trip_manual("self-2026")
                    if x["kind"] == "stay"])
    _up = [r for r in _EV.upcoming(days=80, today="2027-01-01")
           if r.get("slug") == "self-2026" and r.get("booking")]
    assert {r["kind"] for r in _up} == {"flight", "train"}, \
        "首页「临近」没按自动已订过滤"
    assert all(r["label"] in ("订机票", "订高铁票") for r in _up), \
        "首页「临近」还在推已订的住宿"
    with _mock.patch.object(_BM3, "available", lambda: False):
        _bk_html = _EVUI.render_page({"slug": ["self-2026"]})
    for _txt in ("该订了", "改预订提前量", "按比赛日", "自动视为已订",
                 "已订 / 不用提醒", "仍需提醒"):
        assert _txt in _bk_html, f"赛事页缺了「{_txt}」"
    assert 'name="kind" value="flight"' in _bk_html, \
        "按钮没按类型提交（待办已不绑行程下标）"
    assert "单条行程也能在添加时" not in _bk_html, \
        "单条提前量输入已废弃，文案还在误导"
    assert 'data-act="calendarSync"' in _bk_html, \
        "赛事页没有「同步到系统日历」入口"

    # ---- 系统日历清单：过期/已订不写、uid 稳定、原生能认回自己 ---------
    _ci = [x for x in _EV.calendar_items(today="2027-01-01")
           if x["slug"] == "self-2026"]
    assert _ci, "日历清单为空"
    _cbk = [x for x in _ci if x["type"] == "booking"]
    _ctp = [x for x in _ci if x["type"] == "trip"]
    assert _cbk and _ctp, f"预订或行程没进日历清单：{[x['type'] for x in _ci]}"
    assert {x["kind"] for x in _cbk} == {"flight", "train"}, \
        "自动已订的住宿还往系统日历写提醒"
    assert "2027-03-27" in _cbk[0]["desc"], "日历描述没写清是按比赛日倒推"
    assert all(x["at"].endswith("09:00") and x["minutes"] == [0]
               for x in _cbk), "预订提醒该在截止日 09:00 弹"
    assert all(isinstance(x["minutes"], list) and x["minutes"] and
               all(isinstance(m, int) for m in x["minutes"]) for x in _ci), \
        "提醒分钟数格式不对，原生会插出 0 分钟外的怪提醒"
    assert all(("rcuid=" + x["uid"]) in x["desc"] and
               x["desc"].count("#RunnerCoach") == 1 for x in _ci), \
        "描述缺 uid 标记，下次同步会认不出自己写的事件"
    assert len({x["uid"] for x in _ci}) == len(_ci), "uid 撞了"
    assert _EV.calendar_items(today="2027-01-01") == \
        _EV.calendar_items(today="2027-01-01"), \
        "同一天的清单每次生成不同 id，日历会被反复删建"
    # 只填日期没填时刻：不能把事件定在 00:00 半夜弹
    _EV.trip_add("self-2026", "other", "2027-03-24", "领物")
    _ci2 = [x for x in _EV.calendar_items(today="2027-01-01")
            if x["slug"] == "self-2026"]
    _ling = [x for x in _ci2 if "领物" in x["title"]]
    assert len(_ling) == 1 and _ling[0]["at"].endswith("09:00"), \
        "只填日期的行程没归一到 09:00"
    assert not any(x["type"] == "booking"
                   for x in _EV.calendar_items(today="2027-03-20")), \
        "过期的待办还往过去插日历事件，只会补一条骚扰通知"
    import json as _json
    from garmin_cn import appapi as _AP

    _ap = _json.loads(_AP.call("calendar_list"))
    assert _ap["ok"] and _ap["count"] == len(_ap["items"]), \
        "App 桥没拿到日历清单"
    assert all({"uid", "at", "title", "desc", "minutes"} <= set(x)
               for x in _ap["items"]), "清单字段缺了，原生会跳过这些事件"

    # ---- CP 表截图识别：解析、校验、不猜 -------------------------------
    from garmin_cn import cpocr as _CO

    _fake = ('{"segments":[{"idx":1,"name":"CP1 仙山雅宿","km":10.9,"cutoff":"8:05"},'
             '{"idx":2,"name":"冰川瀑布景区","km":22.3,"cutoff":null},'
             '{"idx":3,"name":"","km":30.0,"cutoff":"12:00"},'
             '{"idx":4,"name":"没里程","km":null}]}')
    _obj = _CO._json_obj(_fake)
    _segs, _probs = _CO.clean(_obj["segments"], {"distance_km": 105})
    assert len(_segs) == 2, f"该丢掉没地名/没里程的行，实际留下 {len(_segs)} 条：{_segs}"
    assert _segs[0]["name"] == "仙山雅宿", "地名前面的 CP1 前缀没清掉"
    assert _segs[0]["cutoff"] == "08:05", "关门时间没补零成 HH:MM"
    assert [x["idx"] for x in _segs] == [1, 2], "编号没重排成连续"
    assert not _probs, f"正常的一批不该有问题：{_probs}"

    # 模型偶尔会把行读乱：clean 按里程重排（宁可按数字排，也不要按错误顺序编号）
    _ord_segs, _ = _CO.clean(
        [{"name": "甲", "km": 50}, {"name": "乙", "km": 40}], {"distance_km": 105})
    assert [x["km"] for x in _ord_segs] == [40, 50], "乱序没按里程重排"
    # 同一个里程出现两行（表里常有「CP5 / CP5 补给站」）只留一条
    _dup_segs, _ = _CO.clean(
        [{"name": "甲", "km": 30}, {"name": "甲补给", "km": 30}], {"distance_km": 105})
    assert len(_dup_segs) == 1, "同里程的重复行没去掉"

    _none_segs, _ = _CO.clean([{"name": "甲", "km": 9999}], {"distance_km": 42})
    assert not _none_segs, "超过赛程 1.3 倍的里程该被丢掉（多半是识别错了行）"

    # 路跑不带水：携水必须能填 0（以前下限卡 500，表单直接被原生校验顶回来）
    assert _EV.set_fuel("self-2026", carry_ml=0)["carry_ml"] == 0, \
        "携水填 0 被拒了——路跑不带水是正常配置"
    try:
        _EV.set_fuel("self-2026", carry_ml=4000)
        raise AssertionError("携水 4000 居然存进去了")
    except ValueError:
        pass

    # ---- 赛道俯视图：抽稀、底图 URL、页面渲染 -------------------------
    assert len(_EV.thin_track([(30.0, 120.0, 10)] * 5000)) == 240, "抽稀没按 240 点收口"
    _th_small = _EV.thin_track([(30.0, 120.0, 10), (30.1, 120.1, 12)])
    assert _th_small == [[30.0, 120.0], [30.1, 120.1]], f"少点数被改动了：{_th_small}"
    assert _EV.thin_track([(30.0, 120.0, 10)]) == [], "只有 1 个点时不该算轨迹"
    assert _EV.map_ident("panda-105k-2027") == "ev-panda-105k-2027"
    # slug 由 _dir/active_slug 保证合法，这里只确认前缀和字符集
    assert _EV.map_ident("mar-2026").startswith("ev-")

    # 页面里必须有俯视图；这里把底图关掉，保证测试不联网
    from garmin_cn import basemap as _BM2

    _mk = [(30.0 + i * 0.0002, 120.0 + i * 0.0003, 20) for i in range(240)]
    _st = E2._read("self-2026", "track_stats.json") or {}
    _st = dict(_st)
    _st["polyline"] = _EV.thin_track(_mk)
    E2._write("self-2026", "track_stats.json", _st)
    with _mock.patch.object(_BM2, "available", lambda: False):
        _mh2 = _EVUI.render_page({"slug": ["self-2026"]})
    assert 'class="cmap"' in _mh2, "赛事页没有赛道俯视图"
    assert "矢量轨迹" in _mh2, "没配底图时该说明可以配 Key"

    # 预览页：图片必须带「缩到一屏」的样式（否则长截图把整页拉长，像没加载出来）
    (_ed / "海报.png").write_bytes(b"\x89PNG\r\n")
    _pv = _EVUI.render_doc_page({"slug": ["self-2026"], "name": ["海报.png"]})
    assert "max-height:72vh" in _pv, "预览图没有限高，长图会铺满好几屏"
    assert "pvImg" in _pv and "full" in _pv, "预览图不能点开看原尺寸"

    # 页面：赛事页要有识别入口和文字粘贴（建完赛事之后也能补）
    assert "cpocr:self-2026" in _th, "赛事页没有 CP 表识别入口"
    assert 'action="/__event/import"' in _th, "赛事页不能粘贴 CP 表文字"

    _EV.set_remind("self-2026", "2026-11-18T18:00", "报名截止", True)
    assert _EV.reminders("self-2026"), "提醒没存下来"
    _EV.set_remind("self-2026", "2026-11-18T18:00", "报名截止", False)
    assert not _EV.reminders("self-2026"), "关掉提醒后还留着键"

    # 首页「临近」：有 30 天内的时间点时出现，且带提醒开关
    _EV.EVENTS = Path(ROOT) / "data" / "events"
    _up_html = _EVUI and U.home_page()
    if _EV.upcoming(days=30):
        assert '<section class="rc-panel rc-up-panel">' in _up_html, \
            "有临近时间点，首页却没出现「临近」块"
    assert "action=\"/__event/remind\"" in _up_html or not _EV.upcoming(days=30), \
        "首页临近块没有提醒开关"

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
