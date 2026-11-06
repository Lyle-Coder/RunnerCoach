# AI 平台网关部署指南

网关的作用：App 不接触上游 API Key，只拿设备令牌打你的网关；网关验令牌、
扣余额、转发请求。「充值」= 你批量生成兑换码，用户在 App 内输入即加额。

## 一、本地先跑通

```bash
cd server
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export ADMIN_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(24))")
export UPSTREAM_API_KEY=sk-你的DeepSeekKey
uvicorn app:app --port 8300
```

自测：

```bash
# 1. 注册一台设备（响应里的 token 只显示这一次）
curl -s -X POST http://127.0.0.1:8300/v1/device -d '{"name":"test"}' -H 'Content-Type: application/json'

# 2. 查余额
curl -s http://127.0.0.1:8300/v1/me -H 'Authorization: Bearer rc_xxx'

# 3. 走网关调模型
curl -s -X POST http://127.0.0.1:8300/v1/chat/completions \
  -H 'Authorization: Bearer rc_xxx' -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"你好"}]}'

# 4. 生成 10 个 5 元兑换码
curl -s -X POST http://127.0.0.1:8300/admin/codes \
  -H 'X-Admin-Token: 你的ADMIN_TOKEN' -H 'Content-Type: application/json' \
  -d '{"amount":5,"count":10}'
```

## 二、放到服务器（Docker，推荐）

```bash
scp server/ 服务器:~/gateway/
ssh 服务器
cd ~/gateway
docker build -t rc-gateway .
docker run -d --name rc-gateway --restart unless-stopped \
  -p 127.0.0.1:8300:8300 \
  -v /srv/rc-gateway:/data \
  -e UPSTREAM_API_KEY=sk-xxx \
  -e UPSTREAM_MODEL=deepseek-chat \
  -e ADMIN_TOKEN=一串随机值 \
  -e FREE_GRANT=2000000 \
  rc-gateway
```

`-p 127.0.0.1:8300:8300` 是刻意的：端口只对本机开放，外网一律走 nginx 加 HTTPS。

## 三、nginx + HTTPS

```nginx
server {
    listen 443 ssl;
    server_name ai.你的域名.com;
    ssl_certificate     /etc/letsencrypt/live/ai.你的域名.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/ai.你的域名.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8300;
        proxy_http_version 1.1;
        proxy_set_header Connection '';      # SSE 流式必须
        proxy_buffering off;                 # SSE 流式必须
        proxy_read_timeout 300s;
        client_max_body_size 1m;
    }
}
```

没域名也可以用 IP + 自签或 Cloudflare 免费证书，App 设置里填的地址支持 https。

## 四、日常运营

| 事项 | 操作 |
|---|---|
| 生成兑换码 | `POST /admin/codes {"amount":5,"count":20}`（X-Admin-Token 头）|
| 给某设备手动加额 | `POST /admin/topup {"token":"rc_xxx","amount":10}` |
| 看谁花了多少 | `GET /admin/accounts` |
| 换模型 | 改环境变量 `UPSTREAM_MODEL` 并在 `PRICE_PER_1M` 补价格，重启 |
| 吊销某设备 | 直接操作 sqlite：`UPDATE accounts SET revoked=1 WHERE id=?` |

## 五、App 端接入

App → 菜单 → 模型服务设置 → 切「平台额度」→ 填 `https://ai.你的域名.com`
→ 点「绑定本机」（自动调 `/v1/device` 换令牌）→ 完成，余额会显示在面板上。

## 六、后续升级路径

- **微信支付**：加 `POST /v1/orders` 创建订单 + 微信回调给余额，兑换码体系完全复用。
- **多模型**：把 `UPSTREAM_MODEL` 变成按账号可配，`PRICE_PER_1M` 扩成表即可。
- **对账**：`usage_log` 有每笔流水，接一个日报脚本即可。
