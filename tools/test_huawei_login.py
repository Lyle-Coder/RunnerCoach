"""网页登录桥回归；不使用真实账号，不能替代真机端到端授权验证。"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from garmin_cn import appapi as A, huawei as H


class HuaweiLoginTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.home_patch = patch.object(A, "HOME", self.home)
        self.home_patch.start()
        A.hw_login_cancel()

    def tearDown(self):
        A.hw_login_cancel()
        self.home_patch.stop()
        self.tmp.cleanup()

    def start(self):
        result = A.hw_login_start()
        self.assertTrue(result["ok"])
        return parse_qs(urlsplit(result["url"]).query)["state"][0]

    def callback(self, state, **kwargs):
        return H.LOGIN_REDIRECT + "?" + urlencode({"state": state, "code": "test-code", **kwargs})

    def response(self):
        return 200, {"resultCode": 0, "uid": "123456789", "accessToken": "test-access",
                     "refreshToken": "test-refresh", "accessTokenExpireTime": (time.time() + 3600) * 1000}

    def test_authorize_parameters(self):
        url = urlsplit(H.login_url("state + /"))
        self.assertEqual(url.scheme, "https")
        self.assertEqual(url.hostname, "oauth-login.cloud.huawei.com")
        q = parse_qs(url.query)
        self.assertEqual(q["client_id"], [H.APP_ID])
        # 协议常量必须独立断言，避免 URL 和校验器同时写错仍然通过。
        self.assertEqual(q["redirect_uri"], ["hms://redirect_url"])
        self.assertEqual(set(q["scope"][0].split()), {
            "openid", "https://www.huawei.com/auth/account/base.profile"})
        self.assertEqual(q["ui_locales"], ["zh-cn"])
        self.assertEqual(q["include_granted_scopes"], ["true"])
        self.assertTrue(q["nonce"][0])
        self.assertEqual(q["state"], ["state + /"])
        self.assertEqual(q["response_type"], ["code"])

    def test_success_saves_session_without_exposing_tokens_and_rejects_replay(self):
        callback = self.callback(self.start())
        with patch.object(H, "_http", return_value=self.response()) as http:
            result = A.hw_login_finish(callback)
            self.assertTrue(result["logged_in"])
            self.assertEqual(result["open_id"], "123****89")
            self.assertNotIn("test-access", json.dumps(result))
            cloud = H.HuaweiCloud(self.home)
            self.assertTrue(cloud.load())
            self.assertEqual(cloud.refresh_token, "test-refresh")
            self.assertEqual(http.call_args.kwargs["body"],
                             {"authorizationCode": "test-code", "appId": H.APP_ID})
            self.assertFalse(A.hw_login_finish(callback)["ok"])
            http.assert_called_once()

    def test_callback_preserves_encoded_code_and_matches_android_redirect(self):
        start = A.hw_login_start()
        query = parse_qs(urlsplit(start["url"]).query)
        self.assertEqual(start["redirect_uri"], "hms://redirect_url")
        self.assertEqual(start["redirect_uri"], query["redirect_uri"][0])
        state = query["state"][0]
        code = "a+b/c=%2B"
        callback = start["redirect_uri"] + "/?" + urlencode({"state": state, "code": code})
        with patch.object(H, "_http", return_value=self.response()) as http:
            self.assertTrue(A.hw_login_finish(callback)["ok"])
            self.assertEqual(http.call_args.kwargs["body"]["authorizationCode"], code)

    def test_invalid_code_explains_stage_without_retrying_or_saving(self):
        response = {"resultCode": 20020001, "resultDesc": "invalid code",
                    "accessToken": "secret-do-not-display"}
        with patch.object(H, "_http", return_value=(200, response)) as http:
            result = A.hw_login_finish(self.callback(self.start()))
            self.assertFalse(result["ok"])
            self.assertIn("20020001", result["error"])
            self.assertIn("授权码", result["error"])
            self.assertIn("不是密码错误", result["error"])
            self.assertNotIn("secret-do-not-display", str(result))
            self.assertFalse(H.HuaweiCloud(self.home).auth_path.exists())
            http.assert_called_once()

    def test_invalid_callbacks_never_exchange_code(self):
        for callback in (
            "https://example.com/?state=valid&code=secret",
            "hms://redirect_uri?state=valid&code=secret",
            "hms://redirect_url.evil/?state=valid&code=secret",
            "hms://redirect_url/path?state=valid&code=secret",
            "hms://redirect_url?state=wrong&code=secret",
            "hms://redirect_url?code=secret",
            "hms://redirect_url?state=valid&state=valid&code=secret",
            "hms://redirect_url?state=valid&code=secret&code=other",
            "hms://redirect_url?state=valid&code=",
            "hms://redirect_url?state=valid&error=1107&code=secret",
            "hms://redirect_url?state=valid&code=secret#fragment",
        ):
            with self.subTest(callback=callback), self.assertRaises(H.HwAuthError) as err:
                H.login_code(callback, "valid")
            self.assertNotIn("secret", str(err.exception))

    def test_cancel_expire_and_restart_invalidate_state(self):
        with patch.object(H, "_http") as http:
            old = self.start()
            self.assertNotEqual(self.start(), old)
            self.assertFalse(A.hw_login_finish(self.callback(old))["ok"])
            state = self.start()
            A.hw_login_cancel()
            self.assertFalse(A.hw_login_finish(self.callback(state))["ok"])
            state = self.start()
            with patch("time.monotonic", return_value=time.monotonic() + 901):
                self.assertFalse(A.hw_login_finish(self.callback(state))["ok"])
            http.assert_not_called()

    def test_failed_exchange_preserves_existing_account(self):
        cloud = H.HuaweiCloud(self.home)
        cloud.import_payload({"uid": "old", "accessToken": "old-token"})
        original = cloud.auth_path.read_bytes()
        for response in ((200, {"resultCode": 5, "accessToken": "secret"}),
                         (200, {"resultCode": 0, "accessToken": "secret"})):
            with patch.object(H, "_http", return_value=response):
                result = A.hw_login_finish(self.callback(self.start()))
            self.assertFalse(result["ok"])
            self.assertNotIn("secret", str(result))
            self.assertEqual(cloud.auth_path.read_bytes(), original)

    def test_refresh_persists_new_credentials(self):
        cloud = H.HuaweiCloud(self.home)
        cloud.import_payload({"uid": "123456789", "refreshToken": "old-refresh"})
        with patch.object(H, "_http", return_value=self.response()):
            cloud.refresh()
        loaded = H.HuaweiCloud(self.home)
        self.assertTrue(loaded.load())
        self.assertEqual(loaded.refresh_token, "test-refresh")


if __name__ == "__main__":
    unittest.main()
