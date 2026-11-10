"""高驰取数的离线回归：单位是这里唯一的事故源。

2026-11-09 线上图暴露过一次：跑步机 3.81 km 的活动，"每公里"显示成
100.00 / 380.58 km、配速 0'06"。根因是 `coros.py` 里另有一份解析把
lap 的**厘米**值直接当米用，时序 distance 也没换算，速度被整段判成漂移。
本文件钉住这些口径，防止两份实现再漂移：

  * 明细：distance 厘米、altitude 米、speed 秒/公里、时间厘秒
  * 分圈：只取第一组（避免"全程汇总"重复计入），距离 > 50 m 才算配速
  * 活动列表：distance 米、totalTime 秒、calorie 卡
  * 缓存：无版本号 / 旧版本号的明细必须判为过期
  * 卡片：每公里行显示 1.00 km 而不是 100.00 km；最快速度可用

全部用合成数据，不联网。运行：
    python -m unittest discover -s tools -p test_coros_source.py -v
"""
import json
import unittest

from garmin_cn import actcard
from garmin_cn import coros as C
from garmin_cn import coros_activity as CA

BASE = 179387563300          # 厘秒时间戳（2026-11-05 18:47 前后）


def sample(i: int, dist_cm: float, speed_spm: float, hr=130, cad=170, alt=10.0, power=180):
    """frequencyList 的一条：distance 厘米、speed 秒/公里、altitude 米。"""
    return {"timestamp": BASE + i * 100, "distance": dist_cm, "speed": speed_spm,
            "heart": hr, "cadence": cad, "altitude": alt, "power": power,
            "gpsLat": 0, "gpsLon": 0}


def lap(dist_cm, time_cs, hr=130, cad=170, stride=76, idx=1):
    """lapItemList 的一条：distance 厘米、time 厘秒、avgStrideLength 厘米。"""
    return {"lapIndex": idx, "distance": dist_cm, "time": time_cs,
            "startTimestamp": BASE, "endTimestamp": BASE + time_cs,
            "avgHr": hr, "maxHr": hr + 5, "avgCadence": cad,
            "avgStrideLength": stride, "elevGain": 0.0}


def detail(laps=None):
    """一次 2.81 km、约 20 分钟的跑步机活动（无 GPS，模拟真实跑步机结构）。"""
    freq = []
    # 每 100 秒前进 500 m（速度 200 s/km）；distance 是累计厘米
    for i in range(1, 13):
        freq.append(sample(i, i * 50000, 200, hr=120 + i, cad=168 + (i % 3)))
    native = {
        "distance": 281000, "totalTime": 225000, "workoutTime": 225000,
        "calories": 249000, "avgSpeed": 200.0, "maxSpeed": 180,
        "avgHr": 126, "maxHr": 141, "avgCadence": 170, "maxCadence": 178,
        "aerobicEffect": 2.3, "anaerobicEffect": 0.4, "trainingLoad": 36,
        "elevGain": 0.0, "totalDescent": 1.0,
    }
    return {"frequencyList": freq,
            "lapList": laps if laps is not None else [
                {"type": 0, "lapDistance": 100000, "lapItemList": [
                    lap(100000, 20000, cad=168, idx=1),
                    lap(100000, 19800, cad=172, idx=2),
                    lap(81000, 16200, cad=170, idx=3),
                ]},
                # 全程汇总组：混进来会让总距离翻倍
                {"type": 1, "lapDistance": 281000, "lapItemList": [
                    lap(281000, 56000, cad=170, idx=1),
                ]},
            ],
            "summary": native}


class DetailUnitsTest(unittest.TestCase):
    def setUp(self):
        self.p = CA.normalize(detail(), "a1")

    def test_lap_distance_is_metres(self):
        self.assertEqual([l["dist_m"] for l in self.p["laps"]], [1000.0, 1000.0, 810.0])

    def test_lap_duration_is_seconds(self):
        self.assertAlmostEqual(self.p["laps"][0]["dur_s"], 200.0, places=1)

    def test_lap_pace_is_seconds_per_km(self):
        self.assertEqual(self.p["laps"][0]["pace_s"], 200)
        self.assertEqual(self.p["laps"][2]["pace_s"], 200)

    def test_partial_last_lap_flagged(self):
        self.assertTrue(self.p["laps"][-1]["is_partial"])
        self.assertFalse(self.p["laps"][0]["is_partial"])

    def test_summary_group_not_double_counted(self):
        # 只认第一组：3 圈，不是 4 圈，也不是 5.62 km
        self.assertEqual(len(self.p["laps"]), 3)
        self.assertAlmostEqual(sum(l["dist_m"] for l in self.p["laps"]), 2810.0)

    def test_series_distance_and_speed_units(self):
        pts = self.p["series"]["points"]
        self.assertAlmostEqual(pts[0]["dist"], 500.0, places=1)      # 厘米 → 米
        # speed 200 s/km → 5 m/s；步频 168+1（第一条样本 cadence=168+(i%3), i=1）
        self.assertAlmostEqual(pts[0]["spd"], 5.0, places=2)
        self.assertEqual(pts[0]["cad"], 169)

    def test_altitude_not_scaled(self):
        self.assertAlmostEqual(self.p["series"]["points"][0]["ele"], 10.0, places=2)

    def test_cadence_not_doubled(self):
        # 高驰 avgCadence 是总步频，106 就是 106，不是 212
        p = CA.normalize(detail(laps=[{"lapItemList": [
            lap(100000, 43000, cad=106, idx=1)]}]), "a2")
        self.assertEqual(p["laps"][0]["cad"], 106)

    def test_row_metrics(self):
        rm = self.p["row_metrics"]
        self.assertEqual(rm["aerobic_te"], 2.3)
        self.assertEqual(rm["anaerobic_te"], 0.4)
        # summary.maxSpeed 是秒/公里：180 s/km → 5.556 m/s
        self.assertAlmostEqual(rm["max_speed_mps"], 5.556, places=3)

    def test_adapter_version(self):
        self.assertEqual(self.p["adapter_version"], CA.ADAPTER_VERSION)
        self.assertTrue(CA.is_current(self.p))

    def test_old_cache_is_stale(self):
        self.assertFalse(CA.is_current({"source": "coros"}))          # 旧实现没有版本号
        self.assertFalse(CA.is_current({"source": "coros", "adapter_version": 1}))
        self.assertFalse(CA.is_current({"source": "garmin", "adapter_version": CA.ADAPTER_VERSION}))
        self.assertFalse(CA.is_current({}))

    def test_bad_payload_raises(self):
        for bad in ({}, {"frequencyList": "x"}, {"frequencyList": [1, 2]}):
            with self.assertRaises(C.CorosError):
                CA.normalize(bad, "a3")

    def test_legacy_entry_matches(self):
        # coros.detail_to_payload 只是薄封装，不能再有自己的一套换算
        a = C.detail_to_payload(detail(), [], {"activity_id": "a1"})
        b = CA.normalize(detail(), "a1")
        self.assertEqual([l["dist_m"] for l in a["laps"]],
                         [l["dist_m"] for l in b["laps"]])
        self.assertEqual(a["adapter_version"], b["adapter_version"])


class NativeCorosTest(unittest.TestCase):
    def test_native_summary_and_walking_samples_are_preserved(self):
        d = detail()
        d["summary"].update(avgCadence=130, avgStepLen=81, avgHr=145)
        d["frequencyList"] = [dict(sample(i, i * 300, 600, cad=90), cadenceLength=42)
                              for i in range(30)]
        p = CA.normalize(d, "trail")
        self.assertEqual(p["summary"]["cad_avg"], 130)
        self.assertEqual(p["summary"]["stride_avg"], 81)
        self.assertEqual(p["summary"]["hr_run_avg"], 145)
        self.assertTrue(all(x["cad"] == 90 and x["stride"] == 42
                            and not x.get("fixed") for x in p["series"]["points"]))
        self.assertIsNone(p["summary"]["drift"])

    def test_missing_stride_not_invented(self):
        p = CA.normalize(detail(), "a")
        self.assertIsNone(p["summary"]["stride_avg"])
        self.assertIsNone(p["series"]["points"][0]["stride"])

    def test_kilometre_group_wins_over_first_group(self):
        d = detail()
        d["lapList"].insert(0, {"type": 10, "lapDistance": 500000,
                               "lapItemList": [lap(500000, 200000)]})
        p = CA.normalize(d, "a")
        self.assertEqual(p["lap_distance_m"], 1000)
        self.assertEqual(len(p["laps"]), 3)

    def test_six_native_zones_and_no_inferred_training_claims(self):
        d = detail()
        d["zoneList"] = [{"type": 126, "zoneItemList": [
            {"zoneIndex": i, "second": 60 * (i + 1)} for i in range(6)]},
            {"type": 130, "zoneItemList": [{"zoneIndex": 0, "second": 99999}]}]
        p = CA.normalize(d, "a")
        self.assertEqual(sum(z["seconds"] for z in p["hr_zones"]), 1260)
        from unittest.mock import patch
        with patch.object(actcard.IV, "detect", side_effect=AssertionError("inference called")):
            html = actcard.build_html(CardRenderTest()._row(), p)
        self.assertIn("Z6", html)
        self.assertNotIn("本次未记录分区时长", html)
        self.assertNotIn("热身", html)
        self.assertNotIn(">间歇结构<", html)

    def test_grouped_averages_are_duration_weighted(self):
        laps = [{"idx": 1, "dist_m": 1000, "dur_s": 300, "pace_s": 300,
                 "hr_avg": 160, "cad": 180},
                {"idx": 2, "dist_m": 100, "dur_s": 100, "pace_s": 1000,
                 "hr_avg": 120, "cad": 100}]
        grouped = actcard.group_laps(laps, max_rows=1)[0]
        self.assertEqual(grouped["hr_avg"], 150)
        self.assertEqual(grouped["cad"], 160)
        self.assertAlmostEqual(grouped["pace_s"], 400 / 1.1)

    def test_authoritative_summary_units_and_zero(self):
        d = detail()
        d["summary"].update(avgStepLen=81, aerobicEffect=0, elevGain=0)
        p = CA.normalize(d, "a")
        m = p["row_metrics"]
        self.assertEqual(m["distance_m"], 2810)
        self.assertEqual(m["duration_s"], 2250)
        self.assertEqual(m["calories"], 249)
        self.assertEqual(m["pace_s_km"], 200)
        self.assertEqual(m["ascent"], 0)
        self.assertEqual(m["aerobic_te"], 0)


class ActivityListTest(unittest.TestCase):
    """活动列表口径：distance 米、totalTime 秒、calorie 卡。"""

    def test_sport_type_mapping(self):
        # 101 是高驰的室内跑/跑步机：映射不到就只剩标签「运动」、也没有步频步幅
        for code, want in ((100, "running"), (101, "treadmill_running"),
                           (102, "trail_running"), (103, "track_running"),
                           (200, "road_biking"), (203, "gravel_cycling"),
                           (300, "lap_swimming"), (904, "yoga")):
            self.assertEqual(C.SPORT_TYPE.get(code), want, code)

    def test_units(self):
        row = C.CorosCloud._parse_activity(None, {
            "labelId": "1", "name": "跑步机", "sportType": 8,
            "distance": 3810.0, "totalTime": 1632, "calorie": 249000,
            "trainingLoad": 36, "avgHr": 135, "maxHr": 151,
            "startTime": 1793871200,
            "raw": "{}",
        })
        self.assertEqual(row["distance_m"], 3810.0)
        self.assertEqual(row["duration_s"], 1632.0)
        self.assertEqual(row["calories"], 249.0)        # 卡 → 千卡
        self.assertEqual(row["load"], 36.0)
        self.assertEqual(row["pace_s_km"], round(1632 / 3.81))


class CardRenderTest(unittest.TestCase):
    """出图这一层的口径：每公里行、最快速度、有氧/无氧效果。"""

    def _row(self):
        return {"activity_id": "a1", "date": "2026-11-08", "start_ts": "2026-11-08T09:34:00",
                "type": "treadmill_running", "name": "跑步机", "duration_s": 1632.0,
                "distance_m": 3810.0, "avg_hr": 135, "max_hr": 151, "calories": 249.0,
                "load": 36.0, "pace_s_km": 428.0, "ascent": 0.0, "source": "coros",
                "raw": json.dumps({"labelId": "1", "sportType": 8, "maxSpeed": 0})}

    def test_lap_rows_use_km(self):
        d = CA.normalize(detail(), "a1")
        html = actcard.build_html(self._row(), d)
        self.assertIn("1.00 km", html)
        self.assertNotIn("100.00 km", html)
        self.assertNotIn("0'06\"", html)

    def test_max_speed_from_detail(self):
        d = CA.normalize(detail(), "a1")
        row = self._row()
        row["type"] = "road_biking"        # 非跑步才把「最高 km/h」放进卡面
        html = actcard.build_html(row, d)
        # 列表接口 maxSpeed=0，明细 180 s/km = 20 km/h
        self.assertIn("20.0", html)

    def test_te_from_detail(self):
        d = CA.normalize(detail(), "a1")
        html = actcard.build_html(self._row(), d)
        self.assertIn("2.3", html)


if __name__ == "__main__":
    unittest.main()
