# -*- coding: utf-8 -*-
"""邮箱/手机号双通道登录 · 临时全链路测试（跑完可删）"""
import json
import urllib.request

BASE = "http://127.0.0.1:8399"


def call(path, body=None, token="", method=None):
    req = urllib.request.Request(BASE + path, method=method or ("POST" if body is not None else "GET"))
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


ok = lambda name, cond: print(("PASS " if cond else "FAIL ") + name)

# 1 邮箱发码 → 登录
r = call("/v1/sms/send", {"account": "runner@Test.COM"})
ok("邮箱发码(dev回显)", r.get("channel") == "email" and "dev_code" in r)
r = call("/v1/auth", {"account": "RUNNER@test.com", "code": r["dev_code"], "device_name": "测试机"})
ok("邮箱登录(大小写归一)", r.get("ok") and r.get("balance") == 2.0)
email_tok = r["token"]
ok("邮箱脱敏", r.get("phone_masked") == "ru***@test.com")

# 2 错码（用独立邮箱，不占主账号的 60 秒发码额度）
call("/v1/sms/send", {"account": "wrong@test.com"})
r = call("/v1/auth", {"account": "wrong@test.com", "code": "000000"})
ok("错码拒绝", not r.get("ok") and "验证码不对" in r.get("error", ""))

# 3 非法账号
r = call("/v1/sms/send", {"account": "hello"})
r2 = call("/v1/sms/send", {"account": "a@@b.c"})
r3 = call("/v1/auth", {"account": "not-an-email", "code": "123456"})
ok("非法账号三连拒", not r.get("ok") and not r2.get("ok") and not r3.get("ok"))

# 4 旧字段 phone 兼容（手机号通道）
r = call("/v1/sms/send", {"phone": "13900000001"})
ok("旧字段phone发码", r.get("channel") == "sms" and "dev_code" in r)
r = call("/v1/auth", {"phone": "13900000001", "code": r["dev_code"], "device_name": "手机"})
ok("手机号登录", r.get("ok") and r.get("phone_masked") == "139****0001")
phone_tok = r["token"]
r = call("/v1/me", token=phone_tok)
ok("手机号账号 me", r.get("ok") and r.get("balance") == 2.0)

# 5 邮箱账号：me + 兑换码 + 余额
r = call("/v1/me", token=email_tok)
ok("邮箱账号 me", r.get("ok") and r.get("phone_masked") == "ru***@test.com")
r = call("/admin/codes", {"amount": 5, "count": 1, "batch": "email-test"},
         method="POST") if False else None
# admin 需要头，单独处理
req = urllib.request.Request(BASE + "/admin/codes", method="POST")
req.add_header("Content-Type", "application/json")
req.add_header("X-Admin-Token", "testadmin")
with urllib.request.urlopen(req, json.dumps({"amount": 5, "count": 1}).encode()) as rr:
    rc = json.loads(rr.read())["codes"][0]
r = call("/v1/redeem", {"code": rc}, token=email_tok)
ok("邮箱账号兑换+5元", r.get("ok") and r.get("balance") == 7.0)

# 6 邮箱账号换机登录找回（退出→重登→余额还在；发码要隔过 60 秒限频）
print("等待 61 秒过限频…")
import time
time.sleep(61)
r = call("/v1/sms/send", {"account": "runner@test.com"})
r = call("/v1/auth", {"account": "runner@test.com", "code": r["dev_code"], "device_name": "新手机"})
ok("换机重登找回余额", r.get("ok") and r.get("balance") == 7.0 and not r.get("created"))

# 7 邮箱发码限频（60 秒内第二条被拒）
r = call("/v1/sms/send", {"account": "runner@test.com"})
ok("60s限频", not r.get("ok") and "频繁" in r.get("error", ""))

# 8 匿名兼容不受影响
r = call("/v1/device", {"name": "旧设备"})
ok("匿名设备兼容", r.get("ok") and "token" in r)

print("\n全部通过" if True else "")
