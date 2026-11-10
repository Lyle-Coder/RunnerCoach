# -*- coding: utf-8 -*-
"""高驰官方 MCP 客户端的离线回归：本地假服务模拟 DCR / token / MCP。

真实的 mcpcn.coros.com 需要用户登录授权才能拿到数据，CI 和本机都没法自动测，
所以这里把「协议形状」逐字节复刻一遍：动态注册、PKCE 授权码、token 换取与刷新、
MCP initialize（SSE 响应 + session 头）、tools/call 的文本与结构化两种返回。

跑法：`python tools/test_coros_mcp.py`（或 PYTHONPATH=. 后跑 unittest）
"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from garmin_cn import coros_mcp as M  # noqa: E402

SLEEP_TEXT = """Sleep Overview

2026-10-15
Sleep Score: 91
Daily Sleep: 7h 57min (incl. naps)
Main Sleep (asleep): 7h 57min
Deep Sleep Ratio: 27%
Light Sleep Ratio: 41%
REM Ratio: 31%
Awake Time: 5 min
Main Sleep Window: 2026-10-15 00:28 - 2026-10-15 08:30
Naps Total: 0 min

2026-10-16
Sleep Score: 78
Main Sleep (asleep): 6h 10min
Deep Sleep Ratio: 20%
Light Sleep Ratio: 55%
REM Ratio: 25%
Awake Time: 12 min
Naps Total: 22 min
"""


class FakeCoros(BaseHTTPRequestHandler):
    """一个假的高驰授权服务器 + MCP 端点。"""
    calls: list[dict] = []

    def log_message(self, *a):        # 别把请求日志打到测试输出里
        pass

    def _send(self, code: int, body: dict | str, ctype="application/json", extra=None):
        raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        path = urllib.parse.urlsplit(self.path).path
        if path == "/connect/register":
            body = json.loads(raw or b"{}")
            assert body.get("redirect_uris"), "DCR 必须带 redirect_uris"
            self._send(201, {"client_id": "cid-test-1", "client_name": body.get("client_name"),
                             "redirect_uris": body["redirect_uris"],
                             "grant_types": ["authorization_code", "refresh_token"],
                             "scope": body.get("scope"),
                             "token_endpoint_auth_method": "none"})
            return
        if path == "/oauth2/token":
            form = dict(urllib.parse.parse_qsl(raw.decode()))
            FakeCoros.calls.append({"path": path, "form": form})
            if form.get("grant_type") == "authorization_code":
                assert form.get("code") == "code-123", "授权码没透传"
                assert form.get("code_verifier"), "PKCE verifier 没带上"
                self._send(200, {"access_token": "at-1", "refresh_token": "rt-1",
                                 "expires_in": 3600, "scope": M.SCOPE})
            elif form.get("grant_type") == "refresh_token":
                assert form.get("refresh_token") == "rt-1", "refresh token 没透传"
                self._send(200, {"access_token": "at-2", "refresh_token": "rt-2",
                                 "expires_in": 3600})
            else:
                self._send(400, {"error": "unsupported_grant_type"})
            return
        if path == "/mcp":
            body = json.loads(raw or b"{}")
            FakeCoros.calls.append({"path": path, "mcp": body,
                                    "auth": self.headers.get("Authorization")})
            if body.get("method") == "initialize":
                payload = {"jsonrpc": "2.0", "id": 1,
                           "result": {"protocolVersion": "2025-06-18",
                                      "capabilities": {"tools": {}},
                                      "serverInfo": {"name": "coros", "version": "1"}}}
                # 用 SSE 回，覆盖真实网关最常见的响应形态
                self._send(200, "event: message\ndata: " + json.dumps(payload) + "\n\n",
                           ctype="text/event-stream", extra={"Mcp-Session-Id": "sess-9"})
                return
            if body.get("method") == "notifications/initialized":
                self._send(202, "")
                return
            if body.get("method") == "tools/list":
                self._send(200, {"jsonrpc": "2.0", "id": 2,
                                 "result": {"tools": [{"name": M.SLEEP_TOOL},
                                                      {"name": M.HRV_TOOL}]}})
                return
            if body.get("method") == "tools/call":
                name = (body.get("params") or {}).get("name")
                if name == M.SLEEP_TOOL:
                    self._send(200, {"jsonrpc": "2.0", "id": 3,
                                     "result": {"content": [{"type": "text",
                                                             "text": SLEEP_TEXT}]}})
                elif name == M.HRV_TOOL:
                    self._send(200, {"jsonrpc": "2.0", "id": 3,
                                     "result": {"content": [{"type": "text", "text":
                                                 "2026-10-15: 62 ms\n2026-10-16: 58 ms"}]}})
                else:
                    self._send(200, {"jsonrpc": "2.0", "id": 3,
                                     "result": {"isError": True,
                                                "content": [{"type": "text", "text": "no"}]}})
                return
            self._send(200, {"jsonrpc": "2.0", "id": 0, "result": {}})
            return
        self._send(404, {"error": "not found"})

    def do_GET(self):
        self._send(404, {"error": "not found"})


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeCoros)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls._orig = dict(M.HOSTS)
        for k in list(M.HOSTS):
            M.HOSTS[k] = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        M.HOSTS.clear()
        M.HOSTS.update(cls._orig)
        cls.srv.shutdown()

    def setUp(self):
        FakeCoros.calls.clear()
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def client(self) -> M.CorosMcp:
        c = M.CorosMcp(self.tmp.name, "asia")
        c.register()
        return c


class AuthTest(Base):
    def test_authorize_url_has_pkce(self):
        c = self.client()
        url = c.authorize_url()
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.assertEqual(q["client_id"], "cid-test-1")
        self.assertEqual(q["code_challenge_method"], "S256")
        self.assertEqual(q["redirect_uri"], M.CALLBACK)
        self.assertIn("mcp.tools", q["scope"])
        self.assertGreaterEqual(len(c._verifier), 43)
        self.assertEqual(q["code_challenge"], M._challenge(c._verifier))

    def test_finish_and_persist(self):
        c = self.client()
        c.authorize_url()
        state = c._state
        out = c.finish(f"{M.CALLBACK}?code=code-123&state={state}")
        self.assertEqual(c.access_token, "at-1")
        self.assertEqual(c.refresh_token, "rt-1")
        self.assertGreater(c.expires_at, 0)
        self.assertTrue(out["token_expires_in"])
        # 落到磁盘、权限收紧、能被下一个实例读回来
        self.assertTrue(c.path.is_file())
        again = M.CorosMcp(self.tmp.name, "asia")
        self.assertEqual(again.access_token, "at-1")
        self.assertTrue(again.status()["authorized"])

    def test_finish_rejects_bad_state(self):
        c = self.client()
        c.authorize_url()
        with self.assertRaises(M.McpError):
            c.finish(f"{M.CALLBACK}?code=code-123&state=wrong")

    def test_finish_reports_denied(self):
        c = self.client()
        c.authorize_url()
        with self.assertRaises(M.McpError) as e:
            c.finish(f"{M.CALLBACK}?error=access_denied&error_description=user+said+no")
        self.assertIn("user said no", str(e.exception))

    def test_refresh_when_expiring(self):
        c = self.client()
        c.authorize_url()
        c.finish(f"{M.CALLBACK}?code=code-123&state={c._state}")
        c.expires_at = 0                       # 强制过期
        c.refresh()
        self.assertEqual(c.access_token, "at-2")
        self.assertEqual(c.refresh_token, "rt-2")

    def test_region_switch_does_not_reuse_token(self):
        c = self.client()
        c.authorize_url()
        c.finish(f"{M.CALLBACK}?code=code-123&state={c._state}")
        eu = M.CorosMcp(self.tmp.name, "eu")   # 换区：旧 token 不能带过去
        self.assertFalse(eu.status()["authorized"])


class McpTest(Base):
    def test_initialize_over_sse_keeps_session(self):
        c = self.client()
        c.authorize_url()
        c.finish(f"{M.CALLBACK}?code=code-123&state={c._state}")
        out = c.initialize()
        self.assertEqual(out["session"], "sess-9")
        self.assertEqual(c.protocol, "2025-06-18")
        self.assertEqual(c.session_id, "sess-9")

    def test_tools_list(self):
        c = self.client()
        c.authorize_url()
        c.finish(f"{M.CALLBACK}?code=code-123&state={c._state}")
        names = [t["name"] for t in c.tools()]
        self.assertIn(M.SLEEP_TOOL, names)

    def test_call_requires_bearer(self):
        c = self.client()
        c.authorize_url()
        c.finish(f"{M.CALLBACK}?code=code-123&state={c._state}")
        c.tools()
        auths = [x["auth"] for x in FakeCoros.calls if x.get("mcp")]
        self.assertTrue(auths and all(a and a.startswith("Bearer at-") for a in auths))

    def test_sleep_text_parsed(self):
        c = self.client()
        c.authorize_url()
        c.finish(f"{M.CALLBACK}?code=code-123&state={c._state}")
        rows = c.sleep(3)
        self.assertEqual(len(rows), 2)
        first = rows[0]
        self.assertEqual(first["date"], "2026-10-15")
        self.assertEqual(first["sleep_score"], 91)
        self.assertEqual(first["sleep_total_min"], 477)
        self.assertEqual(first["deep_min"], 129)     # 27% × 477
        self.assertEqual(first["light_min"], 196)    # 41% × 477
        self.assertEqual(first["rem_min"], 148)      # 31% × 477
        self.assertEqual(first["awake_min"], 5)
        self.assertEqual(first["sleep_start"], "2026-10-15T00:28:00")
        self.assertEqual(rows[1]["nap_min"], 22)

    def test_hrv_parsed(self):
        c = self.client()
        c.authorize_url()
        c.finish(f"{M.CALLBACK}?code=code-123&state={c._state}")
        self.assertEqual(c.hrv(2), {"2026-10-15": 62.0, "2026-10-16": 58.0})


class ParseTest(unittest.TestCase):
    def test_minutes_variants(self):
        self.assertEqual(M._minutes("7h 57min"), 477)
        self.assertEqual(M._minutes("6:10"), 370)
        self.assertEqual(M._minutes("22 min"), 22)
        self.assertEqual(M._minutes(477), 477)
        self.assertIsNone(M._minutes(""))

    def test_structured_json(self):
        rows = M.parse_sleep({"structuredContent": {"days": [
            {"date": "20261014", "total_duration_minutes": 420, "quality_score": 84,
             "phases": {"deep_minutes": 90, "light_minutes": 220, "rem_minutes": 110,
                        "awake_minutes": 8, "nap_minutes": 0}}]}})
        self.assertEqual(rows[0]["date"], "2026-10-14")
        self.assertEqual(rows[0]["deep_min"], 90)
        self.assertEqual(rows[0]["sleep_score"], 84)

    def test_ratio_only_json(self):
        rows = M.parse_sleep({"structuredContent": [
            {"date": "2026-10-13", "total_duration_minutes": 480,
             "phases": {"deepRatio": 0.25, "lightRatio": 0.5, "remRatio": 0.25}}]})
        self.assertEqual(rows[0]["deep_min"], 120)
        self.assertEqual(rows[0]["rem_min"], 120)

    def test_garbage_is_skipped(self):
        self.assertEqual(M.parse_sleep({"content": [{"type": "text", "text": "无数据"}]}), [])
        self.assertEqual(M.parse_sleep({}), [])

    def test_callback_shapes(self):
        self.assertEqual(M._parse_callback("code-9")[0], "code-9")
        self.assertEqual(M._parse_callback("/cb?code=abc&state=s")[0], "abc")
        self.assertEqual(M._parse_callback("http://127.0.0.1:1/cb?error=denied")[2], "denied")



CN_SLEEP = """睡眠概览

2026年10月15日
睡眠评分：88
主睡眠：7小时 20分
深睡比例：24%
浅睡比例：52%
REM 比例：24%
清醒时长：9 分钟
睡眠时间：23:40 - 07:00
小睡：15 分钟
"""


class ChineseTextTest(unittest.TestCase):
    """中文账号回来的是中文键名——只认英文就会误判成「没有睡眠记录」。"""

    def test_chinese_text_parsed(self):
        rows = M.parse_sleep({"content": [{"type": "text", "text": CN_SLEEP}]})
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["date"], "2026-10-15")
        self.assertEqual(r["sleep_score"], 88)
        self.assertEqual(r["sleep_total_min"], 440)
        self.assertEqual(r["deep_min"], 106)      # 24% × 440
        self.assertEqual(r["light_min"], 229)     # 52% × 440
        self.assertEqual(r["rem_min"], 106)       # 24% × 440
        self.assertEqual(r["awake_min"], 9)
        self.assertEqual(r["nap_min"], 15)
        self.assertTrue(r["sleep_start"].endswith("23:40:00"))
        self.assertTrue(r["sleep_end"].endswith("07:00:00"))

    def test_date_variants(self):
        for line, want in (("2026-10-15", "2026-10-15"), ("2026/10/15", "2026-10-15"),
                           ("20261015", "2026-10-15")):
            rows = M.parse_sleep({"content": [{"type": "text",
                                               "text": f"{line}\n睡眠评分: 80\n主睡眠: 6小时"}]})
            self.assertEqual(rows[0]["date"], want, f"{line} 没认出来")

    def test_chinese_minutes(self):
        self.assertEqual(M._minutes("7小时 20分"), 440)
        self.assertEqual(M._minutes("45 分钟"), 45)
        self.assertEqual(M._minutes("8 小时"), 480)


# 真机拿到的原文（2026-11-10 用户朋友的高驰账号）：换行是字面量 \n，
# 且有「Main Sleep Period (incl. awake)」「Awake Ratio」「Awake Count」这类干扰行。
REAL_SLEEP_TEXT = (
    "Sleep Overview\n=============\n\nNote: each record below is dated by its "
    "wake-up day.\n\n2026-10-12\nSleep Score: 89\nDaily Sleep: 8h 47min "
    "(incl. naps)\nMain Sleep (asleep): 7h 19min\nMain Sleep Period (incl. "
    "awake): 7h 32min\nSleep metrics scope: daily\nDeep Sleep Ratio: 14%\n"
    "Light Sleep Ratio: 56%\nREM Ratio: 27%\nAwake Ratio: 3%\nAwake Time: "
    "13 min\nAwake Count (>5 min): 0\nMain Sleep Window: 2026-10-11 23:52 - "
    "2026-10-12 07:24\nNaps Total (asleep): 1h 28min\nNaps Period (incl. "
    "awake): 1h 30min\nNap Window: 2026-10-12 14:31 - 2026-10-12 16:01"
)


class RealReturnTest(unittest.TestCase):
    """真实接口返回必须能解析——「授权成功但 0 条」就是栽在这段文本上。"""

    def test_real_english_return(self):
        rows = M.parse_sleep({"content": [{"type": "text", "text": REAL_SLEEP_TEXT}]})
        self.assertEqual(len(rows), 1, "真实返回没解析出记录（字面量 \\n 未还原？）")
        r = rows[0]
        self.assertEqual(r["date"], "2026-10-12")
        self.assertEqual(r["sleep_score"], 89)
        # 用主睡眠 7h19min=439，而不是含小睡的 Daily Sleep 8h47min=527
        self.assertEqual(r["sleep_total_min"], 439)
        self.assertEqual(r["deep_min"], 61)       # 14% × 439
        self.assertEqual(r["light_min"], 246)     # 56% × 439
        self.assertEqual(r["rem_min"], 119)       # 27% × 439
        self.assertEqual(r["awake_min"], 13)      # Awake Time，不是 Ratio 的 3
        self.assertEqual(r["nap_min"], 88)        # Naps Total (asleep) 1h28min
        self.assertEqual(r["sleep_start"], "2026-10-11T23:52:00")
        self.assertEqual(r["sleep_end"], "2026-10-12T07:24:00")

    def test_period_and_count_lines_ignored(self):
        rows = M.parse_sleep({"content": [{"type": "text", "text": REAL_SLEEP_TEXT}]})
        r = rows[0]
        self.assertNotIn("sleep_total_min" if False else "_main_min", r)
        self.assertNotIn("_period" , r)
        # Period (incl. awake) 的 7h32min 不该顶掉真值
        self.assertNotEqual(r["sleep_total_min"], 452)


if __name__ == "__main__":
    unittest.main(verbosity=2)
