"""高驰补历史的离线回归：区间切块、停止线探测、两个数据源的结果合并。

为什么单独一套
--------------
高驰以前**根本没有补历史**：`backfill_next` 只调佳明的 `sync_range`，
高驰用户刷新多少次都只有最近 30 天（真机反馈）。这段逻辑一旦回退，
症状是「历史又不动了」，而且只在高驰账号上出现，很容易漏。

    PYTHONPATH=. python tools/test_coros_backfill.py
"""
from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from garmin_cn import appapi  # noqa: E402
from garmin_cn import coros as C  # noqa: E402


class FakeStore:
    def __init__(self):
        self.days: set[str] = set()
        self.daily: list[dict] = []
        self.acts: list[dict] = []
        self.meta: dict[str, str] = {}

    def day(self, d):
        return {"date": d} if d in self.days else None

    def upsert_daily(self, row, merge=True):
        self.daily.append(row)
        self.days.add(str(row.get("date"))[:10])

    def upsert_activity(self, a):
        self.acts.append(a)

    def set_meta(self, k, v):
        self.meta[k] = str(v)


class FakeCloud:
    def __init__(self):
        self.chunks: list[tuple[str, str]] = []
        self.acts_call = None

    def daily_records(self, start, end):
        self.chunks.append((start, end))
        return [{"date": start}]

    def activities(self, start, end, limit=60):
        self.acts_call = (start, end, limit)
        return [{"activity_id": "1"}]


class WindowTest(unittest.TestCase):
    def test_chunks_are_90_days(self):
        store, cloud = FakeStore(), FakeCloud()
        out = C._window(store, cloud, dt.date(2026, 1, 1), dt.date(2026, 6, 30))
        # 90 天一段，最后一段是零头；不能出现空区间或倒序
        self.assertEqual(cloud.chunks, [
            ("2026-01-01", "2026-03-31"),
            ("2026-04-01", "2026-06-29"),
            ("2026-06-30", "2026-06-30"),
        ])
        self.assertEqual(out["daily"], 3)
        self.assertEqual(out["new_days"], 3, "首次拉取时每天都是新的")
        self.assertEqual(cloud.acts_call[2], 400, "补历史时活动上限要放大")

    def test_second_run_counts_zero_new_days(self):
        store, cloud = FakeStore(), FakeCloud()
        C._window(store, cloud, dt.date(2026, 1, 1), dt.date(2026, 3, 31))
        out = C._window(store, cloud, dt.date(2026, 1, 1), dt.date(2026, 3, 31))
        self.assertGreater(out["daily"], 0)
        self.assertEqual(out["new_days"], 0, "重复区间不该算成新数据")

    def test_single_day_range(self):
        store, cloud = FakeStore(), FakeCloud()
        out = C._window(store, cloud, dt.date(2026, 5, 5), dt.date(2026, 5, 5))
        self.assertEqual(cloud.chunks, [("2026-05-05", "2026-05-05")])
        self.assertEqual(out["daily"], 1)


class EarliestTest(unittest.TestCase):
    """停止线：先读 totalCount，再直接要最后一页（两次请求）。"""

    def setUp(self):
        self.pages: list[int] = []
        outer = self

        class FakeCoros:
            def __init__(self, home):
                pass

            def load(self):
                return True

            def _req(self, method, path, params=None):
                outer.pages.append(int(params["pageNumber"]))
                if params["pageNumber"] == 1:
                    return {"data": {"totalCount": 65, "dataList": [
                        {"startTime": "2026-11-01 08:00:00"}]}}
                return {"data": {"dataList": [
                    {"startTime": "2024-02-03 07:00:00"},
                    {"startTime": "2024-02-01 07:00:00"}]}}

        self._orig = C.CorosCloud
        C.CorosCloud = FakeCoros
        self.home = ROOT

    def tearDown(self):
        C.CorosCloud = self._orig

    def test_last_page_gives_earliest(self):
        got = C.earliest_activity(self.home)
        self.assertEqual(got, "2024-02-01")
        self.assertEqual(self.pages, [1, 3], "65 条 / 30 一页 → 最后一页是第 3 页")

    def test_empty_account(self):
        class Empty:
            def __init__(self, home):
                pass

            def load(self):
                return True

            def _req(self, method, path, params=None):
                return {"data": {"totalCount": 0, "dataList": []}}

        C.CorosCloud = Empty
        self.assertEqual(C.earliest_activity(self.home), "")


class MergeTest(unittest.TestCase):
    """backfill_next 的合并：两个源各自 done，才算全补完。"""

    def setUp(self):
        self._g = appapi._backfill_garmin
        self._c = appapi._backfill_coros

    def tearDown(self):
        appapi._backfill_garmin = self._g
        appapi._backfill_coros = self._c

    def _stub(self, g, c):
        appapi._backfill_garmin = lambda span=None: g
        appapi._backfill_coros = lambda span=None: c

    def test_done_only_when_both_done(self):
        self._stub({"ok": True, "done": True, "ran": False, "fetched": 0,
                    "skipped": 0, "start": "", "end": "", "earliest": ""},
                   {"ok": True, "done": False, "ran": True, "fetched": 12,
                    "skipped": 78, "start": "2025-01-01", "end": "2025-03-31",
                    "earliest": "2019-05-01"})
        out = appapi.backfill_next()
        self.assertFalse(out["done"], "高驰还没补完就报 done，历史会停在中途")
        self.assertEqual(out["fetched"], 12)
        self.assertEqual(out["start"], "2025-01-01")

    def test_both_done(self):
        self._stub({"ok": True, "done": True, "ran": False, "fetched": 0,
                    "skipped": 0, "start": "", "end": "", "earliest": ""},
                   {"ok": True, "done": True, "ran": False, "fetched": 0,
                    "skipped": 0, "start": "", "end": "", "earliest": ""})
        out = appapi.backfill_next()
        self.assertTrue(out["done"])
        self.assertEqual(out["fetched"], 0)

    def test_failure_propagates_login(self):
        self._stub({"ok": True, "done": True, "ran": False},
                   {"ok": False, "done": False, "ran": True,
                    "need_login": True, "error": "token 过期",
                    "start": "2025-01-01", "end": "2025-03-31"})
        out = appapi.backfill_next()
        self.assertFalse(out["ok"])
        self.assertTrue(out["need_login"])

    def test_sums_fetched_from_both(self):
        self._stub({"ok": True, "done": False, "ran": True, "fetched": 30,
                    "skipped": 5, "start": "2025-06-01", "end": "2026-05-31",
                    "earliest": "2020-01-01"},
                   {"ok": True, "done": False, "ran": True, "fetched": 7,
                    "skipped": 83, "start": "2025-01-01", "end": "2025-03-31",
                    "earliest": "2019-05-01"})
        out = appapi.backfill_next()
        self.assertEqual(out["fetched"], 37)
        self.assertEqual(out["skipped"], 88)
        self.assertEqual(out["earliest"], "2020-01-01", "佳明的停止线优先展示")


if __name__ == "__main__":
    unittest.main(verbosity=2)
