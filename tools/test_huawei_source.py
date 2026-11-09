"""华为体重接入的离线回归。

协议已于 2026-11-09 用真机抓包验证完毕，所以这里测的不再是「猜得对不对」，
而是三件必须一直成立的事：

  1. 解析：华为的 samplePoints 嵌套 JSON → body 行，字段和单位不能错
  2. 翻页：version 游标必须能翻完，且不会死循环
  3. 闸门：没会话 / 没 refresh / 接口报错时，明确报错而不是给假数据

全部用合成数据，不联网。运行：
    python -m unittest discover -s tools -p test_huawei_source.py -v
"""
import datetime as dt
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from garmin_cn import huawei as H
from garmin_cn.store import Store


def ms(y, mo, d, h=8, mi=0, s=0):
    return int(dt.datetime(y, mo, d, h, mi, s).timestamp() * 1000)


def point(start_ms: int, weight, fat=None, muscle=None, **extra):
    """造一条华为风格的 detailInfo。"""
    v = {"bodyWeight": weight, "gender": 1, "height": 174}
    if fat is not None:
        v["bodyFatRate"] = fat
    if muscle is not None:
        v["muscleMass"] = muscle
    v.update(extra)
    return {
        "startTime": start_ms,
        "endTime": start_ms,
        "version": start_ms + 5000,
        "recordId": f"r{start_ms}",
        "type": H.WEIGHT_TYPE,
        "samplePoints": [{"startTime": start_ms, "endTime": start_ms,
                          "key": H.WEIGHT_KEY, "value": json.dumps(v)}],
    }


class RecordTest(unittest.TestCase):
    def test_weight_and_composition(self):
        items = [point(ms(2026, 11, 9, 6, 33), 71.15, 18.0, 55.18)]
        r = H.to_records(items)[0]
        self.assertEqual(r["weight_kg"], 71.15)
        self.assertEqual(r["body_fat_pct"], 18.0)
        self.assertEqual(r["muscle_kg"], 55.18)
        self.assertEqual(r["date"], "2026-11-09")
        self.assertEqual(r["ts"], "2026-11-09 06:33:00")

    def test_ignores_other_sample_keys(self):
        it = point(ms(2026, 11, 9), 70.0)
        it["samplePoints"].append({"startTime": 1, "endTime": 1,
                                   "key": "HEART_RATE", "value": "80"})
        self.assertEqual(len(H.to_records([it])), 1)

    def test_skips_unreasonable_weight(self):
        items = [point(ms(2026, 11, 1), 0), point(ms(2026, 11, 2), 900),
                 point(ms(2026, 11, 3), 70.5)]
        recs = H.to_records(items)
        self.assertEqual([r["weight_kg"] for r in recs], [70.5])

    def test_missing_composition_is_none(self):
        r = H.to_records([point(ms(2026, 11, 5), 70.0)])[0]
        self.assertIsNone(r["body_fat_pct"])
        self.assertIsNone(r["muscle_kg"])

    def test_dedup_same_timestamp(self):
        t = ms(2026, 11, 6, 6, 30)
        items = [point(t, 70.1), point(t, 70.1)]
        self.assertEqual(len(H.to_records(items)), 1)

    def test_sorted_ascending(self):
        items = [point(ms(2026, 11, 8), 70.0), point(ms(2026, 11, 1), 71.0)]
        recs = H.to_records(items)
        self.assertEqual([r["date"] for r in recs], ["2026-11-01", "2026-11-08"])

    def test_tolerates_garbage(self):
        self.assertEqual(H.to_records([{"startTime": 0}, "x", None]), [])
        bad = point(ms(2026, 11, 7), 70.0)
        bad["samplePoints"][0]["value"] = "{not json"
        self.assertEqual(H.to_records([bad]), [])


class CursorTest(unittest.TestCase):
    """version 游标翻页：真实接口一次只回一百来条。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.hw = H.HuaweiCloud(self.home)
        self.hw.uid = "u1"
        self.hw.access_token = "tok"
        self.hw.expires_at = time.time() + 3600
        self.hw.device_id = "dev1"

    def tearDown(self):
        self.tmp.cleanup()

    def test_pages_until_empty(self):
        def batch(day, n, ver):
            out = []
            for i in range(n):
                it = point(ms(2026, 3, day + i), 70.0)
                it["version"] = ver
                out.append(it)
            return out

        pages = {0: batch(1, 3, 1000), 1000: batch(10, 2, 2000), 2000: []}
        calls = []

        def fake(version):
            calls.append(version)
            return pages.get(version, [])

        with patch.object(self.hw, "_query_page", side_effect=fake):
            items = self.hw.weights()
        self.assertEqual(len(items), 5)
        self.assertEqual(calls, [0, 1000, 2000])

    def test_stops_when_cursor_not_advancing(self):
        def fake(version):
            return [point(ms(2026, 3, 1), 70.0)]   # 版本号一直不变

        with patch.object(self.hw, "_query_page", side_effect=fake):
            items = self.hw.weights()
        self.assertEqual(len(items), 1)

    def test_days_filter(self):
        old = point(ms(2020, 1, 1), 80.0)
        new = point(int(time.time() * 1000) - 86400_000, 70.0)
        with patch.object(self.hw, "_query_page", return_value=[old, new]):
            items = self.hw.weights(days=30)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["startTime"], new["startTime"])


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_obtain_shape(self):
        hw = H.HuaweiCloud(self.home)
        r = hw.import_payload({"resultCode": 0, "accessToken": "a", "refreshToken": "r",
                               "accessTokenExpireTime": int((time.time() + 3000) * 1000),
                               "uid": "2850"})
        self.assertTrue(r["hasRefresh"])
        self.assertEqual(r["uid"], "2850")
        self.assertTrue((self.home / "data" / H.AUTH_NAME).exists())
        again = H.HuaweiCloud(self.home)
        self.assertTrue(again.load())
        self.assertEqual(again.access_token, "a")
        self.assertEqual(again.uid, "2850")

    def test_import_snake_case_alias(self):
        hw = H.HuaweiCloud(self.home)
        r = hw.import_payload({"access_token": "a", "refresh_token": "r",
                               "expires_in": 3600, "open_id": "o1"})
        self.assertEqual(r["uid"], "o1")

    def test_rejects_cookie_text(self):
        with self.assertRaises(H.HwAuthError):
            H.HuaweiCloud(self.home).import_payload({"Cookie": "a=b"})

    def test_refresh_without_token_is_explicit(self):
        hw = H.HuaweiCloud(self.home)
        hw.access_token = "expired"
        hw.expires_at = 0
        with self.assertRaises(H.HwAuthError) as ctx:
            hw.open_session()
        self.assertIn("refresh", str(ctx.exception).lower())

    def test_expired_uses_margin(self):
        hw = H.HuaweiCloud(self.home)
        hw.access_token = "t"
        hw.expires_at = time.time() + 60          # 一分钟后就过期
        self.assertTrue(hw.expired())             # 提前 5 分钟续

    def test_device_id_is_stable(self):
        hw = H.HuaweiCloud(self.home)
        hw.uid = "u1"
        first = hw._device()
        again = H.HuaweiCloud(self.home)
        again.uid = "u1"
        self.assertEqual(first, again._device())
        self.assertEqual(len(first), 16)


class ProbeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.hw = H.HuaweiCloud(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_session_reports_all_gates_closed(self):
        rep = self.hw.probe()
        self.assertFalse(rep["all_ok"])
        for key in ("account", "weight", "renew"):
            self.assertFalse(rep["gates"][key]["ok"])
        self.assertIn("import", rep["next"])

    def test_refresh_failure_keeps_gates_closed(self):
        self.hw.access_token = "a"
        self.hw.refresh_token = "r"
        self.hw.uid = "u"
        self.hw.expires_at = 0
        with patch.object(self.hw, "refresh", side_effect=H.HwAuthError("续期失败")), \
             patch.object(self.hw, "whoami", side_effect=H.HwAuthError("无效")):
            rep = self.hw.probe()
        self.assertFalse(rep["gates"]["renew"]["ok"])
        self.assertFalse(rep["gates"]["weight"]["ok"])

    def test_all_gates_open(self):
        self.hw.access_token = "a"
        self.hw.refresh_token = "r"
        self.hw.uid = "u"
        self.hw.expires_at = time.time() + 3600
        with patch.object(self.hw, "refresh", return_value=True), \
             patch.object(self.hw, "whoami", return_value={"uid": "u", "nickName": "n"}), \
             patch.object(self.hw, "weights",
                          return_value=[point(ms(2026, 11, 9), 71.15)]), \
             patch.object(self.hw, "save"):
            rep = self.hw.probe()
        self.assertTrue(rep["all_ok"])


class CaptureTest(unittest.TestCase):
    """从 HAR 里救回会话：这是用户唯一必须动手的一步。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, entries):
        p = self.dir / "cap.har"
        p.write_text(json.dumps({"log": {"entries": entries}}), encoding="utf-8")
        return p

    def test_extracts_from_obtain_response(self):
        p = self._write([{
            "request": {"url": H.SESSION_BASE + H.P_OBTAIN,
                        "headers": [{"name": "x-huid", "value": "2850"}]},
            "response": {"content": {"text": json.dumps(
                {"resultCode": 0, "accessToken": "a", "refreshToken": "r",
                 "accessTokenExpireTime": 1794193957286})}},
        }])
        d = H.session_from_capture(p)
        self.assertEqual(d["accessToken"], "a")
        self.assertEqual(d["uid"], "2850")

    def test_falls_back_to_data_request(self):
        p = self._write([{
            "request": {"url": H.DATA_BASE + H.P_BY_VERSION,
                        "headers": [{"name": "x-huid", "value": "2850"}],
                        "postData": {"text": json.dumps(
                            {"token": "t", "deviceId": "dev1"})}},
            "response": {"content": {"text": "{}"}},
        }])
        d = H.session_from_capture(p)
        self.assertEqual(d["accessToken"], "t")
        self.assertNotIn("refreshToken", d)

    def test_rejects_unrelated_har(self):
        p = self._write([{"request": {"url": "https://example.com/x"},
                          "response": {"content": {"text": "{}"}}}])
        with self.assertRaises(H.HwError):
            H.session_from_capture(p)


class SyncTest(unittest.TestCase):
    """入库接线：华为 → body 表 → daily 回填。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "data").mkdir(parents=True, exist_ok=True)
        self.store = Store(self.home / "t.db")
        self.today = dt.date.today()

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()

    def _session(self):
        hw = H.HuaweiCloud(self.home)
        hw.uid = "u1"
        hw.access_token = "a"
        hw.refresh_token = "r"
        hw.expires_at = time.time() + 3600
        hw.device_id = "dev1"
        hw.save()
        return hw

    def test_sync_writes_body(self):
        self._session()
        start = ms(self.today.year, self.today.month, self.today.day, 7, 0)
        items = [point(start, 71.15, 18.0, 55.18)]
        with patch.object(H.HuaweiCloud, "weights", return_value=items):
            n, recs = H.sync(self.store, self.home, days=0)
        self.assertEqual(n, 1)
        row = self.store.conn.execute(
            "SELECT * FROM body WHERE source='huawei'").fetchone()
        self.assertEqual(row["weight_kg"], 71.15)

    def test_sync_without_session_explains(self):
        with self.assertRaises(H.HwAuthError) as ctx:
            H.sync(self.store, self.home, days=0)
        self.assertIn("hw import", str(ctx.exception))

    def test_sync_writes_daily_when_row_exists(self):
        self._session()
        self.store.conn.execute(
            "INSERT INTO daily (date, steps) VALUES (?, 8000)", (self.today.isoformat(),))
        self.store.conn.commit()
        start = ms(self.today.year, self.today.month, self.today.day, 7, 0)
        with patch.object(H.HuaweiCloud, "weights",
                          return_value=[point(start, 71.15, 18.0)]):
            H.sync(self.store, self.home, days=0)
        row = self.store.conn.execute(
            "SELECT weight_kg FROM daily WHERE date=?", (self.today.isoformat(),)).fetchone()
        self.assertEqual(row["weight_kg"], 71.15)


class GateTest(unittest.TestCase):
    """没会话 / 没验证时，不许给出「看起来成功」的结果。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_verify_requires_session(self):
        with self.assertRaises(H.HwAuthError):
            H.verify(self.home)

    def test_verify_rejects_empty_result(self):
        hw = H.HuaweiCloud(self.home)
        hw.import_payload({"accessToken": "a", "refreshToken": "r", "uid": "u"})
        with patch.object(H.HuaweiCloud, "weights", return_value=[]):
            with self.assertRaises(H.HwError):
                H.verify(self.home)

    def test_verify_marks_protocol(self):
        hw = H.HuaweiCloud(self.home)
        hw.import_payload({"accessToken": "a", "refreshToken": "r", "uid": "u"})
        with patch.object(H.HuaweiCloud, "weights",
                          return_value=[point(ms(2026, 11, 9), 71.15)]):
            r = H.verify(self.home)
        self.assertEqual(r["count"], 1)
        self.assertTrue(H.protocol_verified(self.home))
        self.assertTrue(H.status(self.home)["protocol"]["verified"])

    def test_status_without_anything(self):
        st = H.status(self.home)
        self.assertFalse(st["logged_in"])
        self.assertFalse(st["protocol"]["verified"])


if __name__ == "__main__":
    unittest.main()
