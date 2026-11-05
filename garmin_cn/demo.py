"""生成一份仿真数据，用来看板在没有真实账号时也能预览。"""
from __future__ import annotations

import datetime as dt
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
        bb_high = int(max(38, min(98, 55 + (sleep_min - 430) / 5 + rnd.gauss(0, 8))))

        steps = int(max(2200, min(21000, rnd.gauss(9200, 3100) + (3500 if wd >= 5 else 0))))
        stress = int(max(14, min(62, 32 - (sleep_min - 450) / 12 + rnd.gauss(0, 6))))
        spo2 = round(rnd.uniform(94.2, 97.8), 1)
        resp = round(rnd.uniform(13.2, 16.2), 1)

        start_h = 23 if rnd.random() < 0.7 else 0
        start = f"{d} {start_h:02d}:{rnd.randint(0,50):02d}"
        wake = (dt.datetime.fromisoformat(f"{d} 07:00") + dt.timedelta(minutes=rnd.randint(-40, 50)))
        sleep_end = wake.strftime("%Y-%m-%d %H:%M")

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

        # 活动：每周 4-5 次
        if wd in (0, 2, 4, 6) or (wd == 5 and rnd.random() < 0.6):
            t = rnd.choice(TYPES)
            dur = t[2] + rnd.randint(-10, 15)
            dist = round(t[3] * rnd.uniform(0.9, 1.12), 2) if t[3] else 0
            avg_hr = t[4] + rnd.randint(-8, 8)
            load = round(t[6] * rnd.uniform(0.85, 1.2), 1)
            pace = round(dur * 60 / dist) if dist > 2 else None
            store.upsert_activity({
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
            })

    store.set_meta("last_sync", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

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
