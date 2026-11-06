# -*- coding: utf-8 -*-
"""在线支付订单链路 · 回归测试（demo 通道，跑完可删）
需先启动：DB_PATH=data/test_pay.db PORT=8399 PAY_PROVIDER=demo FREE_GRANT=2000000 ADMIN_TOKEN=testadmin python app.py
"""
import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8399"
ADMIN = {"X-Admin-Token": "testadmin"}


def call(path, body=None, token="", headers=None):
    req = urllib.request.Request(BASE + path, method="POST" if body is not None else "GET")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


ok = lambda name, cond: print(("PASS " if cond else "FAIL ") + name)

# 1 登录拿令牌（demo 库首跑赠 2 元）
r = call("/v1/sms/send", {"phone": "13800001111"})
r = call("/v1/auth", {"phone": "13800001111", "code": r["dev_code"], "device_name": "测试机"})
tok = r["token"]
ok("登录", r.get("ok") and r.get("balance") == 2.0)

# 2 未配置支付时下单应 503 —— 本库 PAY_PROVIDER=demo，跳过；参数校验先测
r = call("/v1/orders", {"amount": 0}, tok)
ok("金额 0 拒绝", not r.get("ok"))
r = call("/v1/orders", {"amount": 501}, tok)
ok("金额 501 拒绝", not r.get("ok"))
r = call("/v1/orders", {"amount": 5})
ok("无令牌下单拒绝", not r.get("ok"))

# 3 demo 下单
r = call("/v1/orders", {"amount": 5}, tok)
ok("下单成功", r.get("ok") and r.get("status") == "created" and r.get("channel") == "demo"
   and r.get("amount") == 5)
oid = r.get("order_id")

# 4 订单列表
r = call("/admin/orders", {"limit": 10}, headers=ADMIN)
ok("admin 订单列表", r.get("ok") and len(r.get("orders", [])) >= 1
   and r["orders"][0]["account"] == "138****1111")

# 5 查询未支付订单
r = call("/v1/order/query", {"order_id": oid}, tok)
ok("订单待支付", r.get("ok") and r.get("status") == "created" and r.get("balance") == 2.0)

# 6 模拟到账（重复执行验证幂等）
r = call("/admin/orders/pay", {"order_id": oid}, headers=ADMIN)
ok("模拟到账 ¥5", r.get("ok") and r.get("status") == "paid" and r.get("balance") == 7.0)
r = call("/admin/orders/pay", {"order_id": oid}, headers=ADMIN)
ok("重复到账幂等", r.get("ok") and r.get("duplicate") is True and r.get("balance") == 7.0)

# 7 支付后查询
r = call("/v1/order/query", {"order_id": oid}, tok)
ok("订单已支付", r.get("ok") and r.get("status") == "paid" and r.get("balance") == 7.0)

# 8 订单与用户隔离：注册第二个用户查不到他人订单
r = call("/v1/sms/send", {"phone": "13800002222"})
r = call("/v1/auth", {"phone": "13800002222", "code": r["dev_code"], "device_name": "手机B"})
tok2 = r["token"]
r = call("/v1/order/query", {"order_id": oid}, tok2)
ok("越权查单拒绝", not r.get("ok"))

# 9 admin 人工到账不存在的订单
r = call("/admin/orders/pay", {"order_id": 99999}, headers=ADMIN)
ok("不存在订单 404", not r.get("ok"))

# 10 流水里能看到 topup
r = call("/admin/ledger", {"limit": 5}, headers=ADMIN)
kinds = [x.get("kind") for x in r.get("entries", [])]
ok("流水含 topup", "topup" in kinds)

# 11 公网分享层会改写 Authorization：自定义令牌头必须优先采信
r = call("/v1/me", headers={"Authorization": "Bearer overwritten-by-share-gateway",
                            "X-Platform-Token": tok})
ok("自定义令牌头优先", r.get("ok") is True)
r = call("/v1/me", token="rc-wrong-token")
ok("错误令牌仍拒绝", not r.get("ok"))
