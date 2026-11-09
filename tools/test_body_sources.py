"""本地体重来源口径回归；合成记录不验证任何华为云协议。

运行：python -m unittest discover -s tools -p test_body_sources.py -v
"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from garmin_cn import body as B
from garmin_cn.store import Store


class BodySourcesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.db")
        self.today = dt.date.today().isoformat()
        self.store.conn.execute("INSERT INTO daily(date) VALUES (?)", (self.today,))
        self.store.conn.commit()

    def tearDown(self):
        self.store.conn.close()
        self.temp.cleanup()

    def add(self, source, weight, fat=None, ts="08:00:01", date=None):
        date = date or self.today
        B.save(self.store, [{"date": date, "ts": f"{date} {ts}",
                            "weight_kg": weight, "body_fat_pct": fat}], source)

    def test_huawei_only(self):
        self.add("huawei", 65, 18)
        s = B.stats(self.store)
        self.assertEqual((s["latest"], s["body_fat"], s["source"]), (65, 18, "huawei"))
        self.assertEqual(self.store.day(self.today)["weight_kg"], 65)

    def test_default_mi_and_explicit_huawei(self):
        self.add("mi", 70, 22)
        self.add("huawei", 65, 18, "09:00:00")
        self.assertEqual(B.stats(self.store)["body_fat"], 22)
        B.set_preferred_source(self.store, "huawei")
        s = B.stats(self.store)
        self.assertEqual((s["latest"], s["body_fat"]), (65, 18))
        self.assertEqual(s["fat_series"], [(self.today, 18)])
        self.assertEqual(self.store.day(self.today)["body_fat_pct"], 18)
        B.set_preferred_source(self.store, "auto")
        self.assertEqual(B.stats(self.store)["latest"], 70)

    def test_no_fat_from_rejected_same_day_source(self):
        self.add("mi", 70, 22)
        self.add("huawei", 65, None)
        B.set_preferred_source(self.store, "huawei")
        s = B.stats(self.store)
        self.assertIsNone(s["body_fat"])
        self.assertIsNone(s["fat_mass"])
        self.assertIsNone(self.store.day(self.today)["body_fat_pct"])

    def test_fallback_on_days_without_preferred_source(self):
        self.add("mi", 70, 22)
        B.set_preferred_source(self.store, "huawei")
        self.assertEqual(B.stats(self.store)["source"], "mi")

    def test_duplicate_update_and_second_precision(self):
        self.add("huawei", 65)
        self.add("huawei", 66)
        self.add("huawei", 68, ts="08:00:02")
        self.assertEqual(len(B.rows(self.store)), 2)
        self.assertEqual(B.stats(self.store)["latest"], 67)
        self.assertEqual(self.store.day(self.today)["weight_kg"], 67)

    def test_preference_persists_and_recalculates_old_daily(self):
        old = "2020-01-01"
        self.store.conn.execute("INSERT INTO daily(date) VALUES (?)", (old,))
        self.store.conn.commit()
        self.add("mi", 70, date=old)
        self.add("huawei", 65, date=old)
        B.set_preferred_source(self.store, "huawei")
        other = Store(self.store.path)
        try:
            self.assertEqual(B.preferred_source(other), "huawei")
            self.assertEqual(other.day(old)["weight_kg"], 65)
        finally:
            other.conn.close()

    def test_unknown_source_and_invalid_preference(self):
        self.add("custom-scale", 65)
        B.set_preferred_source(self.store, "custom-scale")
        with self.assertRaises(ValueError):
            B.set_preferred_source(self.store, "typo")
        self.assertEqual(B.preferred_source(self.store), "custom-scale")

    def test_existing_source_order_and_no_same_rank_mixing(self):
        self.add("garmin", 80)
        self.add("csv", 75)
        self.add("ble", 71)
        self.add("mi", 70)
        self.assertEqual(B.stats(self.store)["latest"], 70)

    def test_empty_save_preserves_existing_data(self):
        self.add("huawei", 65)
        B.save(self.store, [], "huawei")
        self.assertEqual(B.stats(self.store)["latest"], 65)

    def test_bridge_round_trip(self):
        from garmin_cn import appapi
        self.add("huawei", 65)
        with patch.object(appapi, "DB", self.store.path):
            self.assertTrue(appapi.body_set_source("huawei")["ok"])
            self.assertEqual(appapi.body_sources()["preferred"], "huawei")
            self.assertFalse(appapi.body_set_source("typo")["ok"])

    def test_dashboard_source_label_is_escaped(self):
        from garmin_cn.report import body_block
        self.add("<custom>", 65)
        rendered = body_block(B.stats(self.store))
        self.assertIn("&lt;custom&gt;", rendered)
        self.assertNotIn("<custom>", rendered)

    def test_ai_review_uses_selected_source(self):
        from garmin_cn import aicoach, nutrition
        self.add("mi", 70, 22)
        self.add("huawei", 65, 18)
        B.set_preferred_source(self.store, "huawei")
        with patch.object(aicoach, "_store", return_value=self.store), \
             patch.object(aicoach, "derive", return_value={"body": B.stats(self.store)}), \
             patch.object(nutrition, "targets", return_value={}), \
             patch.object(aicoach.llm, "chat", return_value={"text": "{}", "model": "test", "elapsed": 0}):
            result = aicoach.weight_review()
        self.assertEqual(result["points"], [{"date": self.today, "weight": 65}])
        self.assertEqual(result["fat_points"], [{"date": self.today, "fat": 18}])


if __name__ == "__main__":
    unittest.main()
