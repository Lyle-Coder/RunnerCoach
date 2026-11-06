# AI 平台网关部署指南

网关的作用：App 不接触上游 API Key，用手机号或邮箱验证码登录拿设备令牌打你的
网关；网关验令牌、扣余额、转发请求。「充值」= 你批量生成兑换码，用户在
App 内输入即加额。

## 数据安全设计（为什么不会丢充值）

- **账号即手机号或邮箱**：余额挂在账号上，设备令牌只是登录态。卸载重装、
  换手机，重新登录同一账号，余额原样找回；一个账号可绑多台设备。
- **append-only 流水账本**：每一笔余额变动（赠送/兑换/消费/调整）都是
  不可变流水，带变动后余额（balance_after）。`users.balance` 只是缓存，
  账目以流水为准，`/admin/rebuild` 可随时重建。
- **备份三件套**：
  - `GET /admin/export` —— 全量数据 JSON（人能读，便于核对）
  - `GET /admin/db` —— SQLite 快照直接下载（恢复最快）
  - 服务器上 `server/data/`（compose 挂载的持久卷）纳入常规备份即可
- **兑换码留痕**：码带批次号，兑换记录记用户和时间，手里永远有底账。

## 一、本地先跑通（dev 短信模式）

```bash
cd server
export ADMIN_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(24))")
export UPSTREAM_API_KEY=sk-你的DeepSeekKey
python app.py        # 默认 SMS_PROVIDER=dev：验证码打印到日志并回显给客户端
```

自测：

```bash
# 1. 发验证码（dev 模式响应里直接带 dev_code）
curl -s -X POST http://127.0.0.1:8300/v1/sms/send \
  -H 'Content-Type: application/json' -d '{"phone":"13800138000"}'

# 2. 登录（新号自动注册 + 赠送额度；响应里的 token 只显示这一次）
curl -s -X POST http://127.0.0.1:8300/v1/auth -H 'Content-Type: application/json' \
  -d '{"phone":"13800138000","code":"dev_code里的6位数","device_name":"测试机"}'

# 3. 查余额（含设备列表）
curl -s http://127.0.0.1:8300/v1/me -H 'Authorization: Bearer rc_xxx'

# 4. 走网关调模型
curl -s -X POST http://127.0.0.1:8300/v1/chat/completions \
  -H 'Authorization: Bearer rc_xxx' -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-flash","messages":[{"role":"user","content":"你好"}]}'

# 5. 生成 10 个 5 元兑换码（带批次号）
curl -s -X POST http://127.0.0.1:8300/admin/codes \
  -H 'X-Admin-Token: 你的ADMIN_TOKEN' -H 'Content-Type: application/json' \
  -d '{"amount":5,"count":10,"batch":"首发放量"}'

# 6. 备份快照
curl -s -OJ http://127.0.0.1:8300/admin/db -H 'X-Admin-Token: 你的ADMIN_TOKEN'
```

## 二、放到服务器（Docker Compose，推荐）

```bash
scp -r server/ 服务器:~/gateway/
ssh 服务器
cd ~/gateway
cp .env.example .env && vim .env     # 填 ADMIN_TOKEN / UPSTREAM_API_KEY / 短信配置
docker compose up -d --build
curl http://127.0.0.1:8300/health
```

数据落在 `./data/data.db`（compose 挂载的持久卷），升级镜像、重启容器都不丢。

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
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;  # 短信限频按真实 IP
        proxy_buffering off;                 # SSE 流式必须
        proxy_read_timeout 300s;
        client_max_body_size 2m;
    }
}
```

没域名也可以用 IP + 自签或 Cloudflare 免费证书，App 设置里填的地址支持 https。

## 四、接入正式验证码通道（上线前必做其一）

### 方案 A：邮箱验证码（推荐，零成本）

1. 用一个 QQ 邮箱（或 163/Gmail）：设置 → 账户 → 开启 SMTP 服务 →
   生成**授权码**（不是邮箱登录密码）；
2. `.env` 里配置并重启：

```
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_USER=你的邮箱@qq.com
SMTP_PASS=你的授权码
```

发信免费，正常用户随便用；限频仍内置（同账号 60 秒 1 条 / 每天 10 条，
同 IP 每天 30 条），刷子打不出压力。QQ 邮箱每天自发信上限远高于此。
提醒用户看垃圾邮件夹即可。

### 方案 B：短信验证码（可选，按条计费）

仅当必须支持手机号注册时才开：

1. 阿里云开通短信服务，申请**签名**（如「RunnerCoach」）和**验证码模板**
   （模板如 `您的验证码${code}，5分钟内有效`），等审核通过；
2. 创建 AccessKey（建议 RAM 子账号，只授 `AliyunDysmsapiFullAccess`）；
3. `.env` 里配置并重启：

```
SMS_PROVIDER=aliyun
SMS_ACCESS_KEY_ID=LTAI…
SMS_ACCESS_KEY_SECRET=…
SMS_SIGN_NAME=RunnerCoach
SMS_TEMPLATE_CODE=SMS_xxxxxxxx
```

费用参考：约 0.045 元/条。两个通道可以同时开——用户输入手机号走短信、
输入邮箱走 SMTP，按格式自动路由。

## 五、日常运维

| 操作 | 命令 |
|---|---|
| 生成兑换码 | `POST /admin/codes {"amount":5,"count":10,"batch":"批次名"}` |
| 给账号直接加额 | `POST /admin/topup {"phone":"138…","amount":10}` |
| 查用户与余额 | `GET /admin/accounts` |
| 查流水账 | `POST /admin/ledger {"user_id":1,"limit":100}` |
| 吊销设备（丢机/异常） | `POST /admin/revoke {"device_id":3}` |
| 余额缓存重建 | `POST /admin/rebuild` |
| 全量导出 | `GET /admin/export` |
| SQLite 快照 | `GET /admin/db` |
| 换模型 | 改环境变量 `UPSTREAM_MODEL` 并在 `PRICE_PER_1M` 补价格，重启 |

所有管理接口都要带 `X-Admin-Token` 头。

## 六、App 端接入

App → 菜单 → 模型服务设置 → 切「平台额度」→ 填 `https://ai.你的域名.com`
→ 输手机号或邮箱发验证码 → 登录/注册 → 完成，余额显示在面板上。
旧版 v0.4.0 的「绑定本机」（匿名设备接口 `/v1/device`）仍然兼容：匿名设备
会建一个 `phone=''` 的孤立账号，用户升级登录后用 `admin/topup` 把旧余额
手工并入新账号即可。

## 七、后续升级路径

- **微信支付**：加 `POST /v1/orders` 创建订单 + 微信回调写流水，兑换码体系完全复用。
- **多模型**：把 `UPSTREAM_MODEL` 变成按账号可配，`PRICE_PER_1M` 扩成表即可。
- **对账日报**：`ledger` + `usage_log` 都在库里，接一个日报脚本即可。
