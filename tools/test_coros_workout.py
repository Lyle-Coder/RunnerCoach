"""Offline import regressions. Fixtures are schemas from official authenticated
tools/list on 2026-11-13 (descriptions removed), not account/training data.
"""
from __future__ import annotations

import copy
import datetime as dt
import functools
import http.client
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from garmin_cn import coros_workout as C, coros_mcp as M, cycling, workout, appapi, trainingui

CATALOG = json.loads((Path(__file__).parent / "fixtures/coros_training_tools.json").read_text("utf-8"))
TODAY = dt.date.today().isoformat()


def run_info(**kw):
    return dict(kind="quality", week=1, dow="周二", abbr="4×5min@T", minutes=60,
                phase="基础", km=10, week_km=60, week_vert=0, **kw)


def run_course():
    return C.build(run_info())[0][0]


class FakeMcp:
    def __init__(self):
        self.identity = "authorization-a"
        self.calls = []
        self.courses = {}
        self.timeout = False
        self.reject = False
        self.catalog = copy.deepcopy(CATALOG)
        self.next_id = 9223372036854775000
        self.lock = threading.Lock()

    def status(self):
        return {"authorized": bool(self.identity)}

    def binding(self):
        return self.identity

    def tools(self):
        return self.catalog

    def call(self, name, args):
        with self.lock:
            self.calls.append((name, copy.deepcopy(args)))
        if name.startswith("create"):
            if self.reject:
                return {"isError": True, "content": [{"type": "text", "text": "rejected"}]}
            self.next_id += 1
            handle = str(self.next_id)
            self.courses[handle] = copy.deepcopy(args["course"])
            if self.timeout:
                raise M.McpError("timeout after save")
            field = "idInPlan" if name == "createScheduledWorkout" else "Workout ID"
            return {"content": [{"type": "text", "text": f"Created\n{field}: {handle}"}]}
        if name in ("queryWorkoutDetails", "queryScheduledWorkoutDetails"):
            handle = args.get("workoutId") or args.get("idInPlan")
            return {"content": [{"type": "text", "text": "```json\n" + json.dumps(self.courses[handle]) + "\n```"}]}
        field = "idInPlan" if name == "queryTrainingSchedule" else "workoutId"
        return {"structuredContent": {"workouts": [{field: k} for k in self.courses]}}

    @property
    def writes(self):
        return [(n, a) for n, a in self.calls if n.startswith("create")]


class ConversionTest(unittest.TestCase):
    def test_running_repeats_pace_and_hr(self):
        c = run_course()
        self.assertEqual(c["sportType"], 1)
        self.assertNotIn("intensityType", c)
        group = c["sections"][1]
        self.assertEqual(set(group), {"intervalGroup", "repeats", "sets"})
        self.assertEqual(group["repeats"], 4)
        work, recovery = group["sets"]
        self.assertEqual((work["targetType"], work["targetValue"]), (2, 300))
        self.assertEqual((work["intensityValueStart"], work["intensityValueEnd"]), (250, 265))
        self.assertEqual(recovery["sectionType"], 3)
        self.assertEqual(recovery["intensityType"], 1)
        C.validate_schema({"course": c}, next(t for t in CATALOG if t["name"] == "createSingleWorkout")["inputSchema"])

    def test_easy_distance_is_not_flattened_to_time(self):
        i = run_info()
        i.update(kind="easy", abbr="10km@E")
        s = C.build(i)[0][0]["sections"][0]
        self.assertEqual((s["targetType"], s["targetValue"]), (1, 10000))
        self.assertEqual((s["intensityValueStart"], s["intensityValueEnd"]), (300, 340))

    def test_hill_preserves_lap_targets(self):
        i = run_info()
        i.update(kind="hill", abbr="St24×2")
        c = C.build(i)[0][0]
        self.assertEqual(c["sections"][0]["targetType"], 4)
        self.assertNotIn("targetValue", c["sections"][0])
        self.assertEqual(c["sections"][1]["sets"][0]["targetValue"], 240)

    def test_strength_companion_is_reported(self):
        i = run_info()
        i.update(kind="str", dow="周三")
        courses, warnings = C.build(i)
        self.assertEqual(len(courses), 1)
        self.assertEqual(courses[0]["sportType"], 1)
        self.assertIn("力量", warnings[0])

    def test_bike_watts_and_no_pace(self):
        info = cycling.session(95, 10, 200, "intervals")
        info["ftp_w"] = 200
        c = C.build(info)[0][0]
        self.assertEqual(c["sportType"], 2)
        self.assertEqual(c["sections"][1]["intensityValueStart"], 180)
        self.assertEqual(c["sections"][1]["intensityValueEnd"], 200)
        self.assertTrue(all(s["intensityType"] == 4 for s in c["sections"]))
        self.assertEqual(sum(s["targetValue"] for s in c["sections"]), 2400)

    def test_no_ftp_requires_explicit_no_intensity(self):
        info = cycling.session(80, 0)
        with patch("garmin_cn.sports.profile", return_value={}):
            with self.assertRaisesRegex(ValueError, "强度"):
                C.build(info)
            c = C.build(info, no_intensity=True)[0][0]
        self.assertTrue(all("intensityType" not in s for s in c["sections"]))

    def test_generic_advice_never_invents_intervals_or_zones(self):
        info = {"name": "常规训练", "sport_key": "running", "minutes": 50, "effect": "Z2 + 少量 Z3"}
        with self.assertRaises(ValueError):
            C.build(info)
        c, warnings = C.build(info, no_intensity=True)
        self.assertEqual(len(c[0]["sections"]), 1)
        self.assertNotIn("intensityType", c[0]["sections"][0])
        self.assertIn("Z2", c[0]["courseDescription"])
        self.assertTrue(warnings)

    def test_rest_and_completed_advice_are_not_workouts(self):
        for name in ("数据不足", "按计划休息", "今日已训练", "课表已完成"):
            with self.assertRaises(ValueError):
                C.build({"name": name, "sport_key": "running", "minutes": 30}, no_intensity=True)

    def test_unsupported_target_and_nested_repeat_are_not_downgraded(self):
        step = workout._step(1, "interval", "time", 60, ("hr", (100, 150)))
        nested = workout._repeat(1, 2, [workout._repeat(1, 2, [step])])
        payload = workout._workout("嵌套", "说明", workout.SPORT_RUN, [workout._seg(workout.SPORT_RUN, [nested])])
        with self.assertRaisesRegex(ValueError, "嵌套"):
            C.from_dto(payload)
        c = run_course()
        c["sportType"] = 5
        with self.assertRaisesRegex(ValueError, "越野"):
            C.validate(c)

    def test_absolute_single_value_is_preserved_and_out_of_range_rejected(self):
        c = run_course()
        s = c["sections"][0]
        s["intensityValueStart"] = s["intensityValueEnd"] = 150
        C.validate(c)
        s["intensityValueEnd"] = 241
        with self.assertRaises(ValueError):
            C.validate(c)

    def test_live_coros_json_string_wrapper_is_decoded(self):
        course = run_course()
        text = 'Workout Detail\nCourse JSON: ' + json.dumps(course, ensure_ascii=False)
        result = {"content": [{"type": "text", "text": json.dumps(text, ensure_ascii=False)}], "isError": False}
        self.assertIn(course, list(C._objects(result)))
        self.assertNotIn("course", result)


class SubmitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.cloud = FakeMcp()

    def preview(self, **kw):
        return C.prepare(self.home, [run_course()], self.cloud, **kw)

    def test_offline_preview_freezes_payload(self):
        c = run_course()
        p = C.prepare(self.home, [c], self.cloud)
        c["courseName"] = "changed"
        p["courses"][0]["courseName"] = "tampered"
        self.assertFalse(self.cloud.calls)
        self.assertTrue(C.submit(self.home, p["preview_id"], self.cloud)["ok"])
        self.assertNotIn(self.cloud.writes[0][1]["course"]["courseName"], ("changed", "tampered"))

    def test_library_create_and_readback_preserve_64bit_identifier(self):
        p = self.preview()
        result = C.submit(self.home, p["preview_id"], self.cloud)
        self.assertTrue(result["ok"])
        self.assertEqual(self.cloud.writes[0][0], "createSingleWorkout")
        self.assertNotIn("date", self.cloud.writes[0][1])
        args = next(a for n, a in self.cloud.calls if n == "queryWorkoutDetails")
        self.assertEqual(args["workoutId"], "9223372036854775001")
        self.assertNotIn("922337", json.dumps(result))

    def test_schedule_date_and_readback(self):
        p = self.preview(date=TODAY)
        self.assertTrue(C.submit(self.home, p["preview_id"], self.cloud)["ok"])
        self.assertEqual(self.cloud.writes[0][0], "createScheduledWorkout")
        self.assertEqual(self.cloud.writes[0][1]["date"], TODAY.replace("-", ""))
        self.assertNotIn("dayNo", self.cloud.writes[0][1]["course"])

    def test_duplicate_is_durable_across_previews(self):
        p = self.preview()
        C.submit(self.home, p["preview_id"], self.cloud)
        p2 = self.preview()
        result = C.submit(self.home, p2["preview_id"], self.cloud)
        self.assertTrue(result["items"][0]["duplicate"])
        self.assertEqual(len(self.cloud.writes), 1)

    def test_timeout_cannot_repeat_write_and_can_reconcile(self):
        self.cloud.timeout = True
        p = self.preview(date=TODAY)
        self.assertTrue(C.submit(self.home, p["preview_id"], self.cloud)["uncertain"])
        self.assertTrue(C.submit(self.home, self.preview(date=TODAY)["preview_id"], self.cloud)["uncertain"])
        self.assertEqual(len(self.cloud.writes), 1)
        self.assertTrue(C.submit(self.home, p["preview_id"], self.cloud, check_only=True)["ok"])
        self.assertEqual(len(self.cloud.writes), 1)

    def test_restart_recovery_uses_persisted_arguments_and_current_account(self):
        self.cloud.timeout = True
        p = self.preview(date=TODAY)
        C.submit(self.home, p["preview_id"], self.cloud)
        C._PREVIEWS.clear()
        rows = C.recent_imports(self.home, self.cloud)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "uncertain")
        self.assertTrue(C.check_import(self.home, rows[0]["import_key"], self.cloud)["ok"])
        self.assertEqual(len(self.cloud.writes), 1)
        self.cloud.identity = "another-account"
        self.assertEqual(C.recent_imports(self.home, self.cloud), [])
        with self.assertRaisesRegex(ValueError, "授权"):
            C.check_import(self.home, rows[0]["import_key"], self.cloud)

    def test_result_without_saved_course_is_not_success(self):
        self.cloud.reject = True
        p = self.preview()
        self.assertFalse(C.submit(self.home, p["preview_id"], self.cloud)["ok"])
        C.submit(self.home, p["preview_id"], self.cloud)
        self.assertEqual(len(self.cloud.writes), 1)

    def test_check_before_submit_never_writes(self):
        result = C.submit(self.home, self.preview()["preview_id"], self.cloud, check_only=True)
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.cloud.writes), 0)

    def test_account_change_and_expiry_require_new_preview(self):
        p = self.preview()
        self.cloud.identity = "another-account"
        with self.assertRaisesRegex(ValueError, "授权"):
            C.submit(self.home, p["preview_id"], self.cloud)
        self.cloud.identity = "authorization-a"
        with patch.object(C.time, "time", return_value=C.time.time() + C.TTL + 1):
            with self.assertRaisesRegex(ValueError, "过期"):
                C.submit(self.home, p["preview_id"], self.cloud)
        self.assertFalse(self.cloud.calls)

    def test_server_contract_change_rejects_before_write(self):
        p = self.preview()
        t = next(t for t in self.cloud.catalog if t["name"] == "createSingleWorkout")
        t["inputSchema"]["required"].append("newField")
        with self.assertRaisesRegex(ValueError, "必填"):
            C.submit(self.home, p["preview_id"], self.cloud)
        self.assertFalse(self.cloud.writes)

    def test_concurrent_submits_reserve_before_network(self):
        p = self.preview()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: C.submit(self.home, p["preview_id"], self.cloud), range(2)))
        self.assertTrue(any(r["ok"] for r in results))
        self.assertEqual(len(self.cloud.writes), 1)

    def test_schedule_window(self):
        now = dt.date.today()
        for offset in (-1, 91):
            with self.assertRaises(ValueError):
                self.preview(date=(now + dt.timedelta(days=offset)).isoformat())
        self.preview(date=(now + dt.timedelta(days=90)).isoformat())


class ApiUiTest(unittest.TestCase):
    def test_api_dry_preview_has_no_mcp_requests(self):
        with tempfile.TemporaryDirectory() as root:
            cloud = FakeMcp()
            with patch.object(appapi, "HOME", Path(root)), patch.object(C, "local_session", return_value=run_info()), patch.object(M, "CorosMcp", return_value=cloud):
                p = appapi.coros_workout_preview(destination="library")
                self.assertTrue(p["ok"], p)
                self.assertFalse(cloud.calls)
                self.assertTrue(appapi.coros_workout_submit(p["preview_id"])["ok"])

    def test_preview_page_is_escaped_and_does_not_submit(self):
        c = run_course()
        c["courseName"] = '<script>alert("bad")</script>'
        with patch.object(appapi, "coros_mcp_status", return_value={"authorized": True}), patch.object(appapi, "coros_workout_preview", return_value={"ok": True, "preview_id": "p1", "date": TODAY, "courses": [c], "warnings": []}), patch.object(appapi, "coros_workout_submit") as submit:
            doc = trainingui.render({"preview": ["1"]}, "csrf-secret")
        self.assertIn("&lt;script&gt;", doc)
        self.assertIn('id="coros-submit"', doc)
        self.assertIn("csrf-secret", doc)
        self.assertFalse(submit.called)

    def test_cli_dry_run_never_calls_mcp(self):
        args = SimpleNamespace(sport="run", date=TODAY, all=False, week=None, start=None, end=None,
                               replace=False, library=True, no_intensity=False, dry_run=True, verbose=False)
        with tempfile.TemporaryDirectory() as root, patch.object(C, "local_session", return_value=run_info()), patch.object(M, "CorosMcp", return_value=FakeMcp()) as factory, patch("builtins.print"):
            self.assertEqual(C.push_cli(args, Path(root) / "test.db", root), 0)
            self.assertFalse(factory.return_value.calls)

    def test_training_http_preview_and_csrf_block_before_write(self):
        from garmin_cn.serve import _Handler
        with tempfile.TemporaryDirectory() as root, patch.object(appapi, "coros_mcp_status", return_value={"authorized": False}), patch.object(appapi, "coros_workout_submit", return_value={"ok": True}) as submit:
            server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Handler, directory=root))
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request("GET", "/training")
                response = connection.getresponse()
                doc = response.read().decode("utf8")
                self.assertEqual(response.status, 200)
                self.assertIn("预览训练", doc)
                self.assertFalse(submit.called)
                for token, status in (("wrong", 403), (server.training_token, 200)):
                    connection.request("POST", "/__training/submit", json.dumps({"token": token, "preview_id": "p"}), {"Content-Type": "application/json"})
                    response = connection.getresponse()
                    response.read()
                    self.assertEqual(response.status, status)
                submit.assert_called_once_with("p", False)
                with patch.object(_Handler, "_is_loopback", return_value=False):
                    connection.request("GET", "/training")
                    response = connection.getresponse()
                    response.read()
                    self.assertEqual(response.status, 403)
                    connection.request("POST", "/__training/submit", '{}')
                    response = connection.getresponse()
                    response.read()
                    self.assertEqual(response.status, 403)
                self.assertEqual(submit.call_count, 1)
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)


class StatelessMcpTest(unittest.TestCase):
    def test_stateless_initialization_pagination_and_no_notification(self):
        with tempfile.TemporaryDirectory() as root:
            c = M.CorosMcp(root)
            calls = []
            def rpc(payload, **kw):
                calls.append(payload)
                if payload["method"] == "initialize":
                    return {"result": {"protocolVersion": M.PROTOCOL_VERSION}}
                if payload["params"].get("cursor"):
                    return {"result": {"tools": [{"name": "createSingleWorkout"}]}}
                return {"result": {"tools": [{"name": "queryWorkoutDetails"}], "nextCursor": "page2"}}
            with patch.object(c, "_rpc", side_effect=rpc):
                tools = c.tools()
            self.assertEqual(len(tools), 2)
            self.assertEqual([p["method"] for p in calls], ["initialize", "tools/list", "tools/list"])
            self.assertEqual(c.session_id, "")

    def test_empty_call_result_is_not_treated_as_success(self):
        with tempfile.TemporaryDirectory() as root:
            c = M.CorosMcp(root)
            with patch.object(c, "initialize"), patch.object(c, "_rpc", return_value={"result": {}}):
                with self.assertRaisesRegex(M.McpError, "不完整"):
                    c.call("createSingleWorkout", {})

    def test_missing_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            c = M.CorosMcp(root)
            c._verifier, c._state = "test", "expected"
            with self.assertRaisesRegex(M.McpError, "state"):
                c.finish("http://127.0.0.1:53682/callback?code=test")


if __name__ == "__main__":
    unittest.main()
