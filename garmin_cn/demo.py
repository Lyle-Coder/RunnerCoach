"""生成一份仿真数据，用来看板在没有真实账号时也能预览。

除了日汇总和活动，还会给有距离的活动造一份**合成明细**（环线轨迹 + 1Hz
序列 + 分圈 + 天气）。没有它，demo 只能出看板：活动图和轨迹回放都要读
`activity_detail`，README 想放这两张截图就只能用真实数据 —— 而截图里
带着 GPS 轨迹，等于把家附近的路发到公开仓库。
"""
from __future__ import annotations

import datetime as dt
import json
import math
import random

from .store import Store

TYPES = [
    ("running", "晨跑", 45, 8.2, 148, 520, 62),
    ("running", "轻松跑", 38, 6.4, 138, 380, 45),
    ("trail_running", "山路越野", 95, 13.5, 152, 980, 118),
    ("cycling", "骑行", 80, 32.0, 135, 760, 88),
    ("strength_training", "力量训练", 50, 0, 118, 320, 40),
    ("hiking", "徒步", 140, 9.8, 112, 720, 70),
]


# 演示轨迹的基准点：只用于生成一条「城市公园环线」的几何形状，
# 与维护者的任何真实活动无关；没有高德 Key 时活动图/回放都只画矢量折线。
_DEMO_CENTER = (31.2304, 121.4737)


def _loop_track(km: float, rnd: random.Random) -> list[list[float]]:
    """一条闭合环线，周长约 `km` 公里。

    用三段谐波叠在圆上，看起来像绕着公园跑，而不是标准圆；同一次 build
    里随机源是固定的，所以每次生成的形状一致。
    """
    n = 150
    lat0 = _DEMO_CENTER[0] + rnd.uniform(-0.025, 0.025)
    lon0 = _DEMO_CENTER[1] + rnd.uniform(-0.025, 0.025)
    lat_per_m = 1 / 111_320.0
    lon_per_m = 1 / (111_320.0 * math.cos(math.radians(lat0)))
    radius = max(120.0, km * 1000 / (2 * math.pi))
    ph1, ph2 = rnd.uniform(0, 6.28), rnd.uniform(0, 6.28)
    out: list[list[float]] = []
    for i in range(n + 1):                     # 末点闭合，轨迹才是一条环路
        t = 2 * math.pi * (i % n) / n
        r = radius * (1 + 0.16 * math.sin(3 * t + ph1) + 0.08 * math.sin(5 * t + ph2))
        out.append([round(lat0 + r * math.cos(t) * lat_per_m, 6),
                    round(lon0 + r * math.sin(t) * lon_per_m, 6)])
    return out


def _bb_curve(day: str, high: int, low: int,
              rnd: random.Random) -> tuple[list[list[int]], int, str]:
    """一天的合成身体电量曲线：夜里充到最高、白天放电、午后小睡回一格。

    没有曲线时一图流只能显示「今日最高」——那是 v58 的兜底，真实场景里
    用户看到的是「此刻还剩多少」+ 一条日内曲线。demo 要能演示这个主场景。
    返回 ([[毫秒时间戳, 值], ...], 当前值, 当前时刻 HH:MM)。
    """
    pts: list[list[int]] = []
    for i in range(48):
        hh, mm = divmod(i * 30, 60)
        if hh < 7:                                  # 睡眠中充电
            k = 0.18 + 0.82 * (hh + mm / 60) / 7
        else:                                       # 白天缓慢放电
            k = 1.0 - 0.72 * ((hh - 7) + mm / 60) / 17
            if 14 <= hh < 15:                       # 午后小睡回一点
                k += 0.18
        v = int(max(low, min(high, low + (high - low) * k + rnd.uniform(-2, 2))))
        ts = int(dt.datetime.fromisoformat(f"{day} {hh:02d}:{mm:02d}").timestamp() * 1000)
        pts.append([ts, v])

    now_ms = dt.datetime.now().timestamp() * 1000
    cut = [p for p in pts if p[0] <= now_ms] or pts[:1]
    at = dt.datetime.fromtimestamp(cut[-1][0] / 1000).strftime("%H:%M")
    return pts, cut[-1][1], at


def _demo_detail(row: dict, rnd: random.Random) -> dict:
    """合成一份活动明细：轨迹 + 序列 + 分圈 + 天气。"""
    dist_m = float(row.get("distance_m") or 0)
    dur_s = max(300.0, float(row.get("duration_s") or 0))
    avg_hr = float(row.get("avg_hr") or 140)
    kind = row.get("type") or "running"
    cycling = "cycling" in kind or "biking" in kind
    km = dist_m / 1000.0

    start = dt.datetime.strptime(str(row.get("start_ts"))[:16], "%Y-%m-%d %H:%M")
    t0 = start.timestamp()

    n = 420                                    # 约 4 分钟一个点，长时间活动也不爆
    step = dur_s / (n - 1)
    pts = []
    cad_base = 82.0 if cycling else 174.0
    for i in range(n):
        p = i / (n - 1)
        # 配速有起伏：中间段快一点、收尾略掉，才像真人跑出来的
        wav = 1 + 0.06 * math.sin(p * math.pi * 3) - 0.05 * max(0.0, p - 0.85)
        spd = (dist_m / dur_s) * wav
        hr = avg_hr - 22 + 34 * p + 6 * math.sin(p * math.pi * 6)
        pts.append({
            "t": round(t0 + i * step, 1),
            "dist": round(dist_m * p, 1),
            "hr": round(hr + rnd.uniform(-3, 3)),
            "spd": round(spd, 2),
            "ele": round(18 + 14 * math.sin(p * math.pi * 2) + rnd.uniform(-1, 1), 1),
            "cad": round(cad_base + 4 * math.sin(p * math.pi * 5) + rnd.uniform(-2, 2), 1),
            "stride": None if cycling else round(spd / (cad_base / 60), 2),
            "pwr": None if cycling else round(240 + 60 * math.sin(p * math.pi * 3)),
            "bb": None,
            "gap": 0,
        })

    # 分圈：跑步按公里、骑行按 5 km；不足一圈就只给一段（is_partial）
    lap_km = 5.0 if cycling else 1.0
    laps = []
    if km >= lap_km:
        n_lap = int(km // lap_km)
        for i in range(n_lap):
            laps.append({
                "idx": i + 1,
                "dist_m": lap_km * 1000,
                "dur_s": dur_s * lap_km / km,
                "pace_s": dur_s / km,
                "hr_avg": round(avg_hr + rnd.uniform(-4, 6)),
                "cad": round(cad_base + rnd.uniform(-2, 2), 1),
                "is_partial": False,
            })
        rest = km - n_lap * lap_km
        if rest > 0.2:
            laps.append({
                "idx": n_lap + 1,
                "dist_m": round(rest * 1000, 1),
                "dur_s": dur_s * rest / km,
                "pace_s": dur_s / km,
                "hr_avg": round(avg_hr + rnd.uniform(0, 4)),
                "cad": round(cad_base, 1),
                "is_partial": True,
            })
    else:
        laps.append({
            "idx": 1, "dist_m": round(dist_m, 1), "dur_s": dur_s,
            "pace_s": dur_s / km, "hr_avg": round(avg_hr),
            "cad": round(cad_base, 1), "is_partial": False,
        })

    return {
        "activity_id": row.get("activity_id"),
        "fetched_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            "cad_avg": round(cad_base, 1),
            "cad_max": round(cad_base + 14, 1),
            "stride_avg": None if cycling else round(dist_m / dur_s / (cad_base / 60), 2),
            "hr_run_avg": round(avg_hr),
            "drift": {},
        },
        "laps": laps,
        "series": {"points": pts},
        "track": _loop_track(km, rnd),
        "weather": {
            "temp_c": round(rnd.uniform(14, 24), 1),
            "humidity": rnd.randint(45, 78),
            "text": rnd.choice(["多云", "晴", "阴"]),
        },
    }


def build(path, days: int = 60, seed: int = 7) -> Store:
    rnd = random.Random(seed)
    store = Store(path)
    end = dt.date.today()
    dates = [(end - dt.timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]

    hrv_base = 58.0
    for idx, d in enumerate(dates):
        wd = dt.date.fromisoformat(d).weekday()
        progress = idx / max(1, len(dates) - 1)

        # 周末睡得久一点，周中偏短
        sleep_min = int(rnd.gauss(468 + (14 if wd >= 5 else -4), 26))
        sleep_min = max(360, min(560, sleep_min))
        score = int(max(48, min(96, 50 + (sleep_min - 400) / 3 + rnd.gauss(0, 4))))

        deep = int(sleep_min * rnd.uniform(0.14, 0.21))
        rem = int(sleep_min * rnd.uniform(0.18, 0.24))
        awake = int(sleep_min * rnd.uniform(0.02, 0.07))
        light = max(0, sleep_min - deep - rem - awake)

        # 周中午后偶尔小睡，周末更容易午睡
        nap = rnd.choice([0, 0, 0, 20, 30, 45, 90]) if wd >= 5 else rnd.choice([0, 0, 0, 0, 20, 30])
        nap_min = nap or None

        hrv_base += rnd.gauss(0.12, 0.55) + progress * 0.05
        hrv = round(max(35, min(95, hrv_base + rnd.gauss(0, 5))), 1)
        rhr = int(max(44, min(62, 54.5 - progress * 1.5 + rnd.gauss(0, 1.6))))
        # 基准取 66：真实手表的日内最高多在 70~90，太低的电量截图看着像「一直没缓过来」
        bb_high = int(max(45, min(96, 66 + (sleep_min - 430) / 5 + rnd.gauss(0, 8))))

        steps = int(max(2200, min(21000, rnd.gauss(9200, 3100) + (3500 if wd >= 5 else 0))))
        stress = int(max(14, min(62, 32 - (sleep_min - 450) / 12 + rnd.gauss(0, 6))))
        spo2 = round(rnd.uniform(94.2, 97.8), 1)
        resp = round(rnd.uniform(13.2, 16.2), 1)

        start_h = 23 if rnd.random() < 0.7 else 0
        start = f"{d} {start_h:02d}:{rnd.randint(0,50):02d}"
        wake = (dt.datetime.fromisoformat(f"{d} 07:00") + dt.timedelta(minutes=rnd.randint(-40, 50)))
        sleep_end = wake.strftime("%Y-%m-%d %H:%M")

        bb_curve, bb_now, bb_at = _bb_curve(d, bb_high, max(5, bb_high - rnd.randint(30, 55)), rnd)
        store.upsert_daily({
            "date": d, "steps": steps, "step_goal": 10000,
            "distance_m": round(steps * 0.72, 1),
            "calories_total": int(1900 + steps * 0.05),
            "calories_active": int(steps * 0.045),
            "floors": rnd.randint(2, 18),
            "resting_hr": rhr, "min_hr": rhr - rnd.randint(2, 6), "max_hr": rnd.randint(120, 178),
            "avg_stress": stress, "max_stress": min(95, stress + rnd.randint(20, 45)),
            "bb_high": bb_high, "bb_low": max(5, bb_high - rnd.randint(30, 55)),
            "bb_charged": rnd.randint(35, 70), "bb_drained": rnd.randint(30, 65),
            "spo2_avg": spo2, "resp_avg": resp,
            "intensity_mod": rnd.randint(0, 45), "intensity_vig": rnd.randint(0, 18),
            "sleep_score": score, "sleep_total_min": sleep_min,
            "deep_min": deep, "light_min": light, "rem_min": rem, "awake_min": awake,
            "nap_min": nap_min, "naps": 1 if nap_min else None,
            "sleep_start": start, "sleep_end": sleep_end,
            "hrv_night": hrv, "hrv_weekly": round(hrv + rnd.gauss(0, 3), 1),
            "hrv_status": "BALANCED" if hrv > 50 else "LOW",
            "synced_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        })
        # bb_now / bb_now_at / bb_series 不在 DAILY_COLS 里，upsert_daily 会丢掉，
        # 必须走 live 的写入口（真实链路里这三列也是 live 拉的）。
        from . import live as live_mod

        live_mod.save_bb(store, d, {
            "bb_now": bb_now, "bb_now_at": bb_at,
            "bb_series": json.dumps(bb_curve, separators=(",", ":")),
        })

        # 活动：每周 4-5 次
        if wd in (0, 2, 4, 6) or (wd == 5 and rnd.random() < 0.6):
            t = rnd.choice(TYPES)
            dur = t[2] + rnd.randint(-10, 15)
            dist = round(t[3] * rnd.uniform(0.9, 1.12), 2) if t[3] else 0
            avg_hr = t[4] + rnd.randint(-8, 8)
            load = round(t[6] * rnd.uniform(0.85, 1.2), 1)
            pace = round(dur * 60 / dist) if dist > 2 else None
            row = {
                "activity_id": f"demo-{d}-{t[0]}",
                "date": d,
                "start_ts": f"{d} 0{rnd.randint(6,8)}:{rnd.randint(10,55)}",
                "type": t[0], "name": t[1],
                "duration_s": dur * 60, "distance_m": dist * 1000,
                "avg_hr": avg_hr, "max_hr": avg_hr + rnd.randint(12, 30),
                "calories": int(t[5] * rnd.uniform(0.9, 1.1)),
                "load": load,
                "aerobic_te": round(rnd.uniform(2.2, 4.4), 1),
                "anaerobic_te": round(rnd.uniform(0.0, 2.2), 1),
                "pace_s_km": pace, "ascent": rnd.randint(0, 320),
            }
            store.upsert_activity(row)
            # 有距离的活动补一份合成明细：活动图与轨迹回放都靠它，
            # 缺了就只能拿真实 GPS 出截图（见本模块顶部说明）。
            if dist >= 2:
                store.save_detail(row["activity_id"], _demo_detail(row, rnd))

    store.set_meta("last_sync", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    # 打上标记：看板/卡片的数据源徽标会写「DEMO 仿真数据」，
    # 免得截图或演示被当成真实账号数据。
    store.set_meta("demo", "1")

    # 体重/体成分：隔三天称一次，缓慢下降 + 体脂同步降，用来验证趋势卡
    from . import body as body_mod

    body_mod.ensure(store)
    w = 67.4
    for i, d in enumerate(dates):
        if i % 3:
            continue
        w -= rnd.gauss(0.12, 0.1)          # 约 -0.4 kg/周
        body_mod.save(store, [{
            "date": d, "ts": f"{d} 07:0{rnd.randint(2, 9)}",
            "weight_kg": round(w, 2),
            "body_fat_pct": round(19.4 - (len(dates) - i) * 0.012 + rnd.gauss(0, .12), 1),
            "muscle_kg": round(51.6 + rnd.gauss(0, .15), 1),
            "water_pct": round(55.2 + rnd.gauss(0, .5), 1),
            "visceral": 8, "bmr": 1516, "raw": "{}",
        }], source="demo")
    return store
