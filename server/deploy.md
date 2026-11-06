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

## 六、公网测试：反向隧道中转（不暴露家庭 IP）

没有公网服务器、又想让手机在 4G 下测试时，用 `server/relay/` 这套反向隧道：

```
App ──HTTPS──▶ 公网中转站 ◀──长轮询取活── 本机隧道客户端 ──▶ 本机网关 :8300
```

1. **先配隧道密钥**（源码里不含任何密钥，缺省拒绝启动）：

```
# 生成一把（同一把要同时给中转站和客户端用）
python -c "import secrets;print(secrets.token_urlsafe(24))"

# 方式一：写文件（推荐，随目录上传到云端沙箱；secrets.json 已被 .gitignore 忽略）
cp server/relay/secrets.example.json server/relay/secrets.json
#   编辑 secrets.json 填 "tunnel_key": "<刚才生成的值>"

# 方式二：环境变量（适合 Docker / systemd 等托管环境）
TUNNEL_KEY=<刚才生成的值>
```

2. 把 `server/relay/` 目录发布为在线应用（WorkBuddy「发布为应用」），得到分享链接；
3. 本机保持网关容器运行（`docker compose up -d`），再启动隧道客户端：

```
server\relay\start_tunnel.bat "https://xxx.workbuddy.host/?sharecode=XXXX"
# 或： python tools/relay_client.py "https://xxx.workbuddy.host/?sharecode=XXXX"
```

客户端按「环境变量 `TUNNEL_KEY` → `RELAY_SECRETS` 指向的 JSON → `server/relay/secrets.json`」
顺序取密钥，与云端中转站用同一把即可。

4. App 的「平台地址」直接粘**整条分享链接**——客户端会自动拆出
   `?sharecode=` 存为分享码，并在每个请求上带 `X-Share-Code` 头。

安全设计：
- 中转站只认 `X-Tunnel-Key`，密钥从外部注入（环境变量或 `secrets.json`），
  **绝不写进源码**——仓库是公开的，任何硬编码的密钥都等于没有密钥；
- 密钥泄露后的处置：重新生成一把 → 更新中转站与客户端 → 旧钥立即失效；
- `/admin/*`、`/v1/device` 在中转站直接 403——公网上没法白嫖赠送额度、看不到管理接口；
- 家里 IP 和 8300 端口全程不出网，App 只见到中转站域名；
- 网关自身的令牌鉴权、余额校验、发码限频照常生效。
- 中转站是测试通道，长期用仍建议自有域名 + HTTPS 直连。

**兼任每日看板**：中转站的 `/`、`/index.html`、`/card.png` 优先穿隧道取
本机 `site/` 里最新的看板（daily.bat 跑完即更新，无需重新发布），约 5 秒
拿不到就回落云端内置快照（`server/relay/index.html`、`card.png`，随发布
更新，已 gitignore）。重新发布前先 `cp site/index.html site/card.png
server/relay/` 刷新快照，并保持隧道客户端在跑——云端探活打根路径。二进制
（图片）经 base64 过隧道，文本照旧，中转站按 `b64` 标记还原。

## 七、在线支付（微信 Native，可选）

充值三条通道并存，兑换码永远可用兜底：

| 通道 | 配置 | 用途 |
|---|---|---|
| 兑换码 | 无需配置 | 人工发码，永远可用 |
| demo | `PAY_PROVIDER=demo` | 全链路联调：下单→admin/orders/pay 模拟到账→扣费 |
| wechat_native | 见下 | 正式：用户下单拉起微信支付，回调自动到账 |

开通微信 Native 支付需要：微信支付商户号（mchid）、关联的公众号/开放平台
appid、APIv3 密钥、商户 API 证书（序列号 + apiclient_key.pem）。`.env` 配置：

```
PAY_PROVIDER=wechat_native
WXPAY_MCHID=16xxxxxxxx
WXPAY_APPID=wxxxxxxxxxxxxxxxxx
WXPAY_APIV3_KEY=32位APIv3密钥
WXPAY_CERT_SERIAL=证书序列号
WXPAY_PRIVATE_KEY_PATH=/app/cert/apiclient_key.pem   # 把私钥挂载进容器
WXPAY_NOTIFY_URL=https://你的域名/v1/wechat/notify
```

compose 里加一行挂载（私钥别进仓库、别进镜像）：

```yaml
    volumes:
      - ./cert:/app/cert:ro
```

设计要点（都在 `app.py` 的"在线支付"区块）：
- **订单唯一入账口**是 `_order_set_paid`：状态 `created→paid` 原子翻转 +
  写 `topup` 流水，回调重放/并发天然幂等；
- 回调 `/v1/wechat/notify` 做完整 v3 验签（平台证书按 serial 缓存）+
  AES-256-GCM 解密，验签不过直接 401——**没有商户资料前该接口不会开启**；
- 金额 1~500 元整数，订单 30 分钟未付自动过期；对账看
  `GET /admin/orders`，人工确认到账用 `POST /admin/orders/pay`；
- 客户端 `order.create` 下单 → 轮询 `order.query` 到 `paid` 即刷新余额。

App 内拉起微信的方式（扫码 / H5 / APP 支付）取决于你开通的支付产品，
等商户资料就绪后按 `code_url`（Native 二维码）或 H5/APP 支付参数接入。

## 八、App 端接入（v0.4.5 起零配置）

App → 菜单 → 模型服务设置 → 切「平台额度」→ 输手机号或邮箱发验证码
→ 登录/注册 → 完成，余额显示在面板上。**平台地址不需要用户填**。

地址是怎么定的：

- App 出厂内置一个引导入口（域名 + 分享码，见 `garmin_cn/llm.py` 的
  `BUILTIN_PLATFORM_*`）。平台层的分享门禁连 `GET /health` 都要求
  `X-Share-Code`，首次请求没法凭空变出凭证，所以必须有这个引导值。
  它不是密钥——本来就要发给每一位用户；真正的密钥（上游 API Key、
  隧道共享密钥、SMTP 授权码）只在服务端，既不进仓库也不进 APK。
- App 会定期问 `GET /v1/client/config`，网关用 `.env` 里的
  `RC_PUBLIC_BASE` / `RC_SHARE_CODE` 回答「现在该连哪」，结果缓存 6 小时。
  **换域名、轮换分享码只改服务端 `.env` 并 `docker compose up -d`，
  不用重新发版**；两个值留空则客户端继续用内置引导地址。
  下发值必须是 https 且不带查询串，客户端校验不通过就忽略。
  接口只回公开地址，不含任何密钥，也不穿隧道以外的地方。

自建网关 / 内网直连：设置 → 高级 → 勾「使用自定义服务地址」→ 填
`http://电脑局域网IP:8300`（也可直接粘带 `?sharecode=` 的完整分享链接），
取消勾选即恢复官方服务。注意 `127.0.0.1` 在手机上指的是手机自己。

旧版 v0.4.0 的「绑定本机」（匿名设备接口 `/v1/device`）仍然兼容：匿名设备
会建一个 `phone=''` 的孤立账号，用户升级登录后用 `admin/topup` 把旧余额
手工并入新账号即可。

## 九、后续升级路径

- **微信支付**：加 `POST /v1/orders` 创建订单 + 微信回调写流水，兑换码体系完全复用。
- **多模型**：把 `UPSTREAM_MODEL` 变成按账号可配，`PRICE_PER_1M` 扩成表即可。
- **对账日报**：`ledger` + `usage_log` 都在库里，接一个日报脚本即可。
