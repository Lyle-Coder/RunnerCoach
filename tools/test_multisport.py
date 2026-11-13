"""Offline regressions for running/cycling parity. Synthetic data only."""
from __future__ import annotations

import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from garmin_cn import activity, actcard, appapi, brief, card, coach, cycling, dedupe, workout
from garmin_cn import history, hrzones, metrics, plan, sports, ui, event
from garmin_cn.store import Store


def ride(kind="road_biking", **kwargs):
    day = dt.date.today().isoformat()
    return {"activity_id": "ride", "date": day, "start_ts": day + "T07:00:00",
            "type": kind, "name": "Synthetic ride", "duration_s": 3600,
            "distance_m": 30000, "pace_s_km": 120, "avg_hr": 135,
            "load": 80, "source": "garmin", **kwargs}


def points(power=180, seconds=3600, step=1):
    return [{"t": i, "pwr": power, "cad": 85, "hr": 135, "spd": 0,
             "stride": None, "ele": None, "dist": 0} for i in range(0, seconds + 1, step)]


class TaxonomyTest(unittest.TestCase):
    def test_all_bike_subtypes_have_speed(self):
        for kind in sports.BIKE_TYPES:
            self.assertEqual(sports.metric_text(ride(kind)), "30.0 km/h")
            self.assertEqual(actcard.theme_for(kind)["kind"], "speed")

    def test_running_subtypes_share_progress(self):
        for kind in sports.RUN_TYPES:
            row = ride(kind)
            self.assertEqual(card._acts_by_day([row], {row["date"]})[row["date"]]["km"], 30)

    def test_swim_units(self):
        self.assertEqual(sports.metric_text(ride("lap_swimming", distance_m=1000, duration_s=1200, pace_s_km=1200)), "2′00″/100m")

    def test_dedupe_does_not_merge_sports(self):
        self.assertEqual(len(dedupe.dedupe([ride(), ride("running", source="coros", activity_id="r")])), 2)
        self.assertEqual(len(dedupe.dedupe([ride(), ride(source="coros", activity_id="r")])), 1)


class CompletionTest(unittest.TestCase):
    def setUp(self):
        self.row = ride()
        self.day = self.row["date"]
        self.target = {"sport_key": "running", "km": 10, "minutes": 60}

    def test_bike_cannot_complete_run(self):
        self.assertFalse(sports.completion([self.target], [self.row]))

    def test_time_does_not_replace_distance(self):
        self.assertFalse(sports.completion([self.target], [ride("running", distance_m=1000)]))
        self.assertTrue(sports.completion([self.target], [ride("trail_running", distance_m=9000)]))

    def test_multi_sport_requires_every_sport(self):
        targets = [self.target, {"sport_key": "cycling", "minutes": 60}]
        self.assertFalse(sports.completion(targets, [self.row]))
        self.assertTrue(sports.completion(targets, [self.row, ride("running", distance_m=10000)]))

    def test_date_only_is_not_completion(self):
        data = {"classify": plan.CLASSIFY_REV, "today": {"date": self.day, "items": [self.target]},
                "week": [{"date": self.day, "items": [self.target]}]}
        self.assertFalse(plan.annotate(copy.deepcopy(data), {self.day}, self.day)["today"]["done"])
        self.assertFalse(plan.annotate(copy.deepcopy(data), {self.day}, self.day, [self.row])["today"]["done"])
        self.assertTrue(plan.annotate(copy.deepcopy(data), {self.day}, self.day, [ride("running", distance_m=10000)])["today"]["done"])

    def test_coach_does_not_deduct_ride_minutes(self):
        res = {"date": self.day, "readiness": {"score": 85, "parts": []},
               "load": {"tsb": 5, "acwr": 1}, "sleep": {"debt_h": 0}, "activities": [self.row]}
        with patch.object(coach, "_race_today", return_value={**self.target, "abbr": "10 km", "title": "轻松跑"}), patch.object(sports, "config", return_value={}):
            session = coach.pick_session(res)
        self.assertNotIn("已完成", session["type"])
        self.assertNotIn("课表的量已经够", session["why"])


class PowerTest(unittest.TestCase):
    def test_cycling_workout_dry_run_uses_watts(self):
        template = {**cycling.session(90, 5, 200, "intervals"), "ftp_w": 200}
        result = workout.push_day(None, template, "2026-11-13", dry=True)
        payload = result["payload"]
        self.assertEqual(payload["sportType"]["sportTypeKey"], "cycling")
        steps = payload["workoutSegments"][0]["workoutSteps"]
        self.assertEqual(steps[1]["targetType"]["workoutTargetTypeKey"], "power.zone")
        self.assertEqual((steps[1]["targetValueOne"], steps[1]["targetValueTwo"]), (180, 200))
        self.assertEqual(payload["estimatedDurationInSecs"], 2400)

    def test_constant_power_one_hour(self):
        value = cycling.analyze(points(200), ftp=200)
        self.assertEqual(value["avg_power_w"], 200)
        self.assertEqual(value["normalized_power_w"], 200)
        self.assertEqual(value["intensity_factor"], 1)
        self.assertEqual(value["tss"], 100)
        self.assertEqual(sum(z["seconds"] for z in value["power_zones"]), 3600)

    def test_sample_rate_invariant(self):
        self.assertEqual(cycling.analyze(points(200, step=5), 200)["tss"], 100)

    def test_zero_and_missing_are_different(self):
        p = points(0, seconds=60)
        self.assertEqual(cycling.analyze(p, 200)["avg_power_w"], 0)
        for v in p[:35]:
            v["pwr"] = None
        self.assertIsNone(cycling.analyze(p, 200)["tss"])

    def test_unknown_ftp_no_if_or_tss(self):
        value = cycling.analyze(points())
        self.assertIsNone(value["intensity_factor"])
        self.assertIsNone(value["tss"])
        self.assertTrue(value["normalized_power_w"])

    def test_short_activity_has_no_np(self):
        self.assertIsNone(cycling.analyze(points(seconds=20), 200)["normalized_power_w"])

    def test_sensor_gap_not_filled(self):
        p = points(seconds=20) + [{**v, "t": v["t"] + 100} for v in points(seconds=20)]
        value = cycling.analyze(p, 200)
        self.assertIsNone(value["normalized_power_w"])
        self.assertLess(value["power_coverage"], .5)

    def test_ftp_change_keeps_full_resolution_np(self):
        data = {"cycling": cycling.analyze(points(200), 200), "series": {"points": points(100, step=60)}}
        value = cycling.for_detail(data, 250)
        self.assertEqual(value["normalized_power_w"], 200)
        self.assertEqual(value["tss"], 64)
        self.assertEqual(sum(z["seconds"] for z in value["power_zones"]), 3600)

    def test_stationary_power_intervals(self):
        p = []
        for n in range(6):
            p.extend({**v, "t": v["t"] + n * 120} for v in points(250 if n % 2 == 0 else 80, seconds=119))
        result = cycling.intervals({"series": {"points": p}})
        self.assertIsNotNone(result)
        self.assertEqual(result["sets"], 3)
        self.assertIsNone(cycling.intervals({"series": {"points": points()}}))


class GarminBikeTest(unittest.TestCase):
    def test_native_rpm_never_doubled_or_turned_into_stride(self):
        keys = ["directTimestamp", "directSpeed", "directHeartRate", "directBikeCadence", "directPower"]
        raw = {"metricDescriptors": [{"key": k, "metricsIndex": i} for i, k in enumerate(keys)],
               "activityDetailMetrics": [{"metrics": [1700000000000 + i * 1000, 8, 135, 85, 180]} for i in range(31)]}
        data = activity.build_series(raw, sport="road_biking")
        self.assertEqual(data["rescaled"], 0)
        self.assertTrue(all(p["cad"] == 85 and p["stride"] is None for p in data["points"]))
        self.assertEqual(activity.aggregate(data["points"], "road_biking")["pwr_run_avg"], 180)

    def test_coros_indoor_power_and_cadence(self):
        from garmin_cn.coros_activity import normalize
        raw = {"summary": {"sportType": 201, "avgCadence": 85},
               "frequencyList": [{"timestamp": 170000000000 + i * 100, "power": 180, "cadence": 85, "heart": 135} for i in range(61)]}
        data = normalize(raw, "indoor")
        self.assertEqual(data["cycling"]["avg_power_w"], 180)
        self.assertEqual(data["summary"]["cad_avg"], 85)
        self.assertIsNone(data["summary"]["stride_avg"])


class AppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "test.db"
        self.patches = [patch.object(appapi, "DB", self.db), patch.object(sports, "HOME", self.root),
                        patch.object(event, "EVENTS", self.root / "events"),
                        patch.object(appapi, "HOME", self.root), patch.object(appapi, "OUT", self.root / "output")]
        for p in self.patches:
            p.start()
        self.s = Store(self.db)
        self.row = ride()
        self.day = self.row["date"]
        self.s.upsert_daily({"date": self.day, "sleep_total_min": 480, "sleep_score": 90, "resting_hr": 55})
        self.s.upsert_activity(self.row)
        self.s.upsert_activity(ride("trail_running", activity_id="run", distance_m=5000, duration_s=1800, pace_s_km=360))
        self.s.conn.commit()

    def tearDown(self):
        self.s.conn.close()
        for p in reversed(self.patches):
            p.stop()
        # Legacy APIs may retain connections until GC on Windows.
        import gc
        gc.collect()
        self.tmp.cleanup()

    def test_filters_and_month_totals(self):
        for kind, km in (("run", 5), ("bike", 30), ("road_biking", 30), ("all", 35)):
            self.assertEqual(appapi.history_stats(end=self.day, types=kind)["total"]["km"], km)
        self.assertEqual(appapi.activities(types="bike")["total"], 1)

    def test_history_links_keep_sport_in_every_view(self):
        for mode in ("list", "week", "month", "year"):
            doc = history.history_page({"type": ["bike"], "mode": [mode]})
            self.assertIn("骑行里程", doc)
            self.assertNotIn("每日跑量", doc)
            self.assertIn("type=bike", doc)
        doc = history.history_page({"type": ["bike"]})
        self.assertIn("30.0 km/h", doc)

    def test_display_and_power_summary(self):
        self.assertIn("30.0 km/h", ui._act_metric(self.row))
        detail = {"summary": {"cad_avg": 85}, "series": {"points": points()}, "cycling": cycling.analyze(points(), 200)}
        doc = actcard.build_html(self.row, detail)
        self.assertIn("踏频 rpm", doc)
        self.assertIn("NP W", doc)
        self.assertIn("TSS", doc)
        self.assertIn("功率与心率", doc)
        self.assertIn("踏频 85 rpm", brief._numbers(self.row, detail))
        res = metrics.derive(self.s, self.day)
        with patch.object(coach, "_race_today", return_value=None):
            self.assertIn("30.0 km/h", card.build_html(res))

    def test_shared_load_ignores_vendor_scale(self):
        rows = [{"date": self.day, "resting_hr": 55}]
        a = metrics.daily_loads(rows, [self.row], method="trimp")
        b = metrics.daily_loads(rows, [{**self.row, "load": 9999}], method="trimp")
        self.assertEqual(a, b)
        c = metrics.daily_loads(rows, [self.row], method="trimp", profiles={"bike": {"max_hr": 170}})
        self.assertGreater(c[self.day], a[self.day])
        res = metrics.derive(self.s, self.day)
        self.assertEqual(res["load_by_sport"]["bike"]["week_min"], 60)
        self.assertEqual(res["load_by_sport"]["run"]["week_min"], 30)

    def test_settings_atomic_validation_and_separate_zones(self):
        (self.root / "config.json").write_text('{"email":"preserved"}', "utf-8")
        sports.save_settings({"preferred_sport": "bike", "bike_ftp_w": "200", "bike_max_hr": "180", "bike_hr_zone_names": "恢复,耐力,节奏,阈值,极限"})
        self.assertEqual(sports.config()["email"], "preserved")
        self.assertEqual(sports.profile("bike")["ftp_w"], 200)
        self.assertEqual(hrzones.zone_descriptions(sports.config(), "road_biking")[1], "耐力")
        before = (self.root / "config.json").read_bytes()
        for invalid in ("nan", "inf", "-1", "5000"):
            with self.assertRaises(ValueError):
                sports.save_settings({"bike_ftp_w": invalid})
        self.assertEqual((self.root / "config.json").read_bytes(), before)

    def test_local_cycling_course_has_steps(self):
        sports.save_settings({"preferred_sport": "bike", "bike_ftp_w": "200"})
        res = metrics.derive(self.s, self.day)
        res["activities"] = []  # Preview a course before the workout is recorded.
        data = plan.from_coach(res, self.day)
        item = data["today"]["items"][0]
        self.assertEqual(item["sport_key"], "cycling")
        self.assertEqual(sum(s["seconds"] for s in item["steps"]), item["minutes"] * 60)

    def test_home_advice_respects_already_recorded_ride(self):
        sports.save_settings({"preferred_sport": "bike"})
        res = metrics.derive(self.s, self.day)
        self.assertIn("今日已训练", ui._session(res))

    def test_intervals_template_and_plan_page(self):
        sports.save_settings({"preferred_sport": "bike", "bike_ftp_w": "200", "bike_workout": "intervals"})
        workout = cycling.session(90, 5, 200, "intervals")
        self.assertEqual(workout["type"], "骑行功率间歇")
        self.assertEqual(sum(s["seconds"] for s in workout["steps"]), 2400)
        self.assertNotEqual(cycling.session(50, -30, 200, "intervals")["type"], "骑行功率间歇")
        data = {"ok": True, "today": {"date": self.day, "items": [workout]}, "week": []}
        with patch.object(appapi, "plan", return_value=data):
            doc = ui.plan_page()
        self.assertIn("骑行步骤", doc)
        self.assertIn("180–200 W", doc)

    def test_bundle_round_trip_preserves_all_feature_extras(self):
        data = self.root / "data"
        data.mkdir(exist_ok=True)
        extras = {
            "ai_chat.json": {"v": 1, "messages": [{"role": "user", "content": "synthetic question"}]},
            "posture.json": {"v": 1, "current": {}, "history": [{"move_id": "synthetic"}]},
        }
        for name, value in extras.items():
            (data / name).write_text(json.dumps(value), "utf-8")
        sports.save_settings({"preferred_sport": "bike", "bike_ftp_w": "200"})
        exported = appapi.db_export()
        self.assertTrue(exported["ok"], exported)
        destination = self.root / "restored"
        (destination / "data").mkdir(parents=True)
        with patch.object(appapi, "HOME", destination), patch.object(appapi, "DB", destination / "data" / "garmin.db"), patch.object(sports, "HOME", destination):
            restored = appapi.db_import(exported["path"])
            self.assertTrue(restored["ok"], restored)
            self.assertTrue({"sports.json", "ai_chat.json", "posture.json"} <= set(restored["restored"]))
            self.assertEqual(sports.profile("bike")["ftp_w"], 200)
            for name, value in extras.items():
                self.assertEqual(json.loads((destination / "data" / name).read_text("utf-8")), value)

    def test_export_preserves_profiles_without_credentials(self):
        import zipfile
        (self.root / "data").mkdir(exist_ok=True)
        (self.root / "config.json").write_text('{"email":"private@example.invalid","password":"SECRET"}', "utf-8")
        sports.save_settings({"preferred_sport": "bike", "bike_ftp_w": "200"})
        result = appapi.db_export()
        self.assertTrue(result["ok"], result)
        with zipfile.ZipFile(result["path"]) as archive:
            payload = archive.read("extras/sports.json").decode("utf-8")
        self.assertIn('"bike_ftp_w": 200.0', payload)
        self.assertNotIn("private@example", payload)
        self.assertNotIn("SECRET", payload)

    def test_ai_receives_rpm_and_power_not_running_pace(self):
        from garmin_cn import aicoach
        detail = {"summary": {"cad_avg": 85}, "cycling": cycling.analyze(points(), 200), "series": {"points": points()}}
        self.s.save_detail("ride", detail)
        with patch.object(aicoach, "_store", return_value=self.s), patch.object(aicoach, "_json_chat", return_value=({"summary": "test"}, {})):
            result = aicoach.activity_review("ride", self.day)
        ctx = result["ctx"]
        self.assertEqual(ctx["cadence_unit"], "rpm")
        self.assertEqual(ctx["avg_speed_kmh"], 30)
        self.assertIsNone(ctx["avg_pace"])
        self.assertEqual(ctx["cycling"]["normalized_power_w"], 180)

    def test_settings_http_round_trip_and_invalid_token(self):
        import functools
        import http.client
        import re
        import threading
        from http.server import ThreadingHTTPServer
        from urllib.parse import urlencode
        from garmin_cn.serve import _Handler
        server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Handler, directory=str(self.root)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request("GET", "/sports")
            response = connection.getresponse()
            doc = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            token = re.search(r'name="token" value="([^"]+)"', doc).group(1)
            for supplied, status in (("wrong", 403), (token, 303)):
                body = urlencode({"token": supplied, "preferred_sport": "bike", "bike_ftp_w": "210"})
                connection.request("POST", "/__sports", body, {"Content-Type": "application/x-www-form-urlencoded"})
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, status)
            self.assertEqual(sports.profile("bike")["ftp_w"], 210)
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
