"""检查更新的离线回归：只测纯函数，不联网。

为什么值得单独一套
------------------
这段逻辑决定「用户会不会看到一个假的下载地址」。解析和版本比较错了，轻则
天天提示同一个版本，重则把 debug 包（同包名不同签名）推给用户去装。
所以把 Gitee 真实返回的形状固化成样本，纯函数逐个断言。

    python tools/test_update.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from garmin_cn import update as U  # noqa: E402

# 2026-11-10 实测的 Gitee /releases/latest 形状（字段名以真机为准）
REAL = {
    "tag_name": "v1.0.1",
    "name": "RunnerCoach v1.0.1",
    "body": "# RunnerCoach v1.0.1\n\n## 1.0.1 摘要\n\n- 睡眠接入\n- 跑步动态",
    "created_at": "2026-11-10T11:49:20+08:00",
    "prerelease": False,
    "assets": [
        {"name": "RunnerCoach-v1.0.1-source.zip",
         "browser_download_url": "https://gitee.com/x/source.zip"},
        {"name": "SHA256SUMS.txt",
         "browser_download_url": "https://gitee.com/x/SHA256SUMS.txt"},
        {"name": "RunnerCoach-v1.0.1-debug.apk",
         "browser_download_url": "https://gitee.com/x/debug.apk"},
        {"name": "RunnerCoach-v1.0.1-release.apk",
         "browser_download_url": "https://gitee.com/x/release.apk"},
        {"name": "v1.0.1.zip", "browser_download_url": "https://gitee.com/x/v1.0.1.zip"},
    ],
}


class VersionTest(unittest.TestCase):
    def test_tuple(self):
        self.assertEqual(U.version_tuple("v1.0.1"), (1, 0, 1))
        self.assertEqual(U.version_tuple("1.2"), (1, 2))
        self.assertEqual(U.version_tuple("v1.0.10-rc1"), (1, 0, 10, 1))
        self.assertEqual(U.version_tuple(None), ())
        self.assertEqual(U.version_tuple("abc"), ())

    def test_newer(self):
        self.assertTrue(U.is_newer("v1.0.2", "1.0.1"))
        self.assertTrue(U.is_newer("v1.1.0", "1.0.9"))
        self.assertTrue(U.is_newer("v1.0.10", "1.0.9"))   # 数字段比较，不是字典序
        self.assertFalse(U.is_newer("v1.0.1", "1.0.1"))
        self.assertFalse(U.is_newer("v1.0.0", "1.0.1"))
        self.assertFalse(U.is_newer("", "1.0.1"))
        self.assertFalse(U.is_newer("v1.0.1", None))

    def test_missing_segment_padding(self):
        self.assertTrue(U.is_newer("1.1", "1.0.9"))
        self.assertFalse(U.is_newer("1.0", "1.0.0"))


class AssetTest(unittest.TestCase):
    def test_prefers_release_apk(self):
        got = U.pick_apk(REAL["assets"])
        self.assertIsNotNone(got)
        self.assertEqual(got["name"], "RunnerCoach-v1.0.1-release.apk")
        self.assertTrue(got["url"].endswith("release.apk"))

    def test_never_debug(self):
        only_debug = [{"name": "RunnerCoach-v1.0.2-debug.apk",
                       "browser_download_url": "https://gitee.com/x/d.apk"}]
        self.assertIsNone(U.pick_apk(only_debug), "debug 包签名不同，绝不能推给用户")

    def test_ignores_non_apk_and_broken(self):
        self.assertIsNone(U.pick_apk([{"name": "a.zip", "browser_download_url": "u"}]))
        self.assertIsNone(U.pick_apk([{"name": "a.apk"}]))          # 没有地址
        self.assertIsNone(U.pick_apk([None, "x", 1]))
        self.assertIsNone(U.pick_apk(None))


class ParseTest(unittest.TestCase):
    def test_same_version_is_not_newer(self):
        r = U.parse(REAL, "1.0.1")
        self.assertTrue(r["ok"])
        self.assertFalse(r["newer"])
        self.assertEqual(r["latest"], "v1.0.1")
        self.assertEqual(r["apk"], "RunnerCoach-v1.0.1-release.apk")

    def test_newer_has_direct_apk_url(self):
        r = U.parse(REAL, "1.0.0")
        self.assertTrue(r["newer"])
        self.assertTrue(r["url"].endswith("release.apk"))
        self.assertIn("releases/tag/v1.0.1", r["page"])

    def test_prerelease_flag_preserved(self):
        p = dict(REAL, prerelease=True, tag_name="v1.1.0-beta1")
        r = U.parse(p, "1.0.1")
        self.assertTrue(r["prerelease"])
        self.assertTrue(r["newer"])

    def test_notes_truncated(self):
        p = dict(REAL, body="行\n" * 2000)
        r = U.parse(p, "1.0.1")
        self.assertLessEqual(len(r["notes"]), U.NOTES_MAX + 1)

    def test_no_assets_falls_back_to_page(self):
        p = {"tag_name": "v1.0.2", "assets": [], "body": ""}
        r = U.parse(p, "1.0.1")
        self.assertTrue(r["newer"])
        self.assertIn("/releases/tag/v1.0.2", r["url"])

    def test_garbage_payload(self):
        self.assertFalse(U.parse(None, "1.0.1")["ok"])
        self.assertFalse(U.parse({}, "1.0.1")["ok"])
        self.assertFalse(U.parse({"tag_name": ""}, "1.0.1")["ok"])


class OfflineTest(unittest.TestCase):
    def test_network_failure_is_quiet(self):
        import urllib.request

        orig = urllib.request.urlopen

        def boom(*a, **k):
            raise OSError("no network")

        urllib.request.urlopen = boom
        try:
            r = U.check("1.0.1", timeout=0.1)
        finally:
            urllib.request.urlopen = orig
        self.assertFalse(r["ok"])
        self.assertFalse(r["newer"])
        self.assertIn("no network", r["error"])

    def test_bad_json_is_quiet(self):
        import urllib.request

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"<html>404</html>"

        orig = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: Resp()
        try:
            r = U.check("1.0.1", timeout=0.1)
        finally:
            urllib.request.urlopen = orig
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
