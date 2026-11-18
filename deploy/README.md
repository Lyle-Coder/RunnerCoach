# 部署：无域名（自签证书 + IP 直连）

> 适用场景：**没买域名、也不打算备案**的内测期。产品还没定调时，域名是一笔
> 纯支出——备案要主体、要等，改一次产品定位就得换一次。先把服务跑起来。

## 一、方案与取舍

| | 做法 | 代价 |
|---|---|---|
| 地址 | `https://<服务器IP>:8443` | IP 变了 App 要重新烧地址（一次脚本） |
| 证书 | 自签，10 年，subjectAltName 写 IP | 客户端要内置这张证书当根 |
| 端口 | 8443（不用 80/443） | 未备案的 80/443 容易被拦，8443 一般不碰 |
| DNS | 完全不需要 | 没有域名可记，只能发一个 IP 给朋友 |

**为什么不走 HTTP**：令牌（登录态）明文经过公网，同网段或链路上一抓就是别人的
账号。这比"没域名"严重得多，所以哪怕没有域名也要自签证书。

**为什么不关掉证书校验**：那是最常见的"绕过去"做法，写一行
`_create_unverified_context()` 就通了——但等于把 TLS 变成摆设，还不如 HTTP
诚实。这里的做法是**只多认一张证书**（`garmin_cn/tlstrust.py`），其它主机照旧
走系统根证书。

## 二、服务器

- **配置**：2 核 2G 起（4G 更稳）。纯 Python 标准库 + SQLite，没有常驻渲染任务。
- **系统**：Ubuntu 22.04 / 24.04 或 Debian 12。
- **不要开 80/443**。只放行 `8443/tcp` 和 SSH。
- **地域**：国内机器不需要备案——备案约束的是"域名解析到大陆 IP"，用 IP 直连
  不涉及。但运营商可能对陌生端口做限速，所以第一步就跑 `smoke.sh` 看延迟。

## 三、装（在服务器上）

```bash
# 1. 把仓库传上去（内容池不进 git，必须单独带上）
git clone <仓库> runnercoach && cd runnercoach
scp 本地:server/content/expedition_events.json server/content/

# 2. 装：建用户 → 拷程序 → 生证书 → 写 env 模板 → 装 systemd
sudo sh deploy/install.sh <服务器IP>

# 3. 密钥：本机已经配过的会自动搬过去
#    install.sh 读 server/.env，把 SMTP_* / UPSTREAM_* / ADMIN_TOKEN 抄进
#    /etc/runnercoach/env（只搬白名单，RC_SERVICE_CLOSED 这类故意不搬）。
#    想看会搬哪些键而不动手：
python3 deploy/import_env.py --from server/.env --dry-run

sudo systemctl start runnercoach
sudo journalctl -u runnercoach -n 30 --no-pager
```

**服务会在缺 SMTP 时拒绝启动**，这是故意的：开发模式下验证码会明文回显给
客户端，绑在公网上等于宣布"任何人都能登录任何账号"。护栏在
`server/app.py` 的 `main()` 里，绕开要显式写 `RC_ALLOW_DEV_ECHO=1`。

## 四、验（在开发机上）

```bash
sh deploy/smoke.sh <IP> 8443 deploy/certs/cert.pem
```

八项检查：证书 SAN 里有这个 IP、明文打不通、`/health` 在线且 `dev_echo=false`、
`/v1/me` 无令牌 401、小组接口无令牌 401、管理接口 403、config 可读。
任何一项不过就退出码非 0，可以直接串进发版脚本。

## 五、接到 App 上

```bash
# 证书：从服务器拷回来（不要用私钥，只要 cert.pem）
scp root@<IP>:/opt/runnercoach/certs/cert.pem .

# 写进包：地址 + 证书，一次改好
python tools/set_server.py https://<IP>:8443 --cert cert.pem

# 重打包（_buildcfg.py 和证书都是烧进 APK 的）
cd android && build.bat assembleRelease
```

`set_server.py` 会先替你看证书 SAN 和地址对不对得上——对不上就报错，
不然要等装到手机上才现形（`CERTIFICATE_VERIFY_FAILED`）。

## 六、合规开关（1.5.0 起默认关）

资质没办下来之前，**平台额度与支付是关的**，两端各有一半：

| 位置 | 开关 | 关掉后的表现 |
|---|---|---|
| 服务端 env | `AI_PROXY=off` | `/v1/chat/completions` 直接 503，提示「填自己的 Key」 |
| 服务端 env | `PAY_PROVIDER=`（空） | 下单 503 |
| 服务端 env | `FREE_GRANT=0` | 新号不送额度 |
| 客户端构建 | `_buildcfg.PLATFORM_QUOTA=False` | 设置页不显示「平台额度」，老用户存的 platform 模式按 own 走 |

这里没有"半开"：不卖额度却还替人转发请求，等于无资质提供生成式 AI 服务；
收费同理（经营性）。将来资质齐了，**两处一起打开**——只改一处会变成
"界面能点、点了 503"。`deploy/smoke.sh` 会对开着的代理/支付打一行提醒。

## 七、日常

| 事 | 做法 |
|---|---|
| 备份 | `curl -H 'X-Admin-Token: xxx' https://IP:8443/admin/db -o gw.db`，或直接 tar `/opt/runnercoach/data` |
| 升级 | `git pull` → `sudo sh deploy/install.sh <IP>`（幂等，不覆盖证书与 env）→ 自动重启 |
| 看日志 | `journalctl -u runnercoach -f` |
| 发额度 | `/admin/codes` 生成兑换码，App 内输入 |
| 换 IP | 重新 `gen-cert.sh` → `set_server.py` → 重发 APK（IP 烧在包里，绕不开） |

## 八、什么时候该换成域名

出现下面任一条就该买域名 + 备案，而不是继续用 IP：

1. 要给别人一串能记住的地址（IP 没法口头传播）；
2. 服务器换机房/换 IP（每次都要重发 APK）；
3. 用户超过二三十个，客服成本开始高于备案成本。

换的时候**不用改代码**：域名 + 正规证书后，`tools/set_server.py https://域名`
即可，`tlstrust` 发现包里没有 `server-ca.pem` 就自动走系统根证书。

## 九、已知边界（写给自己看）

- **自签证书不是"更安全"**，只是"没有域名时唯一能用的 HTTPS"。它的信任来自
  你把证书烧进了 App，而不是来自任何 CA。
- 证书 10 年，但要记住：**换证书 = 重发 APK**。所以别在证书里写临时 IP。
- 现在没有速率限制之外的防护（`_rate_limit` 只覆盖上报和验证码）。公网暴露
  一个 API，被扫是常态；`ADMIN_TOKEN` 要够长，别用默认值。

## 十、Windows 开发机注意

`set_server.py` 里对证书做的 SAN 校验用的是 `openssl`（Git for Windows 自带）。
没有的话它会跳过这项检查并提示——不会静默放过，但也就少了一道保险，
装一个 OpenSSL 或者干脆在 Linux 上跑这一步。

`deploy/smoke.sh` 要在 Git Bash 里跑（`sh deploy/smoke.sh ...`），
路径用 MSYS 形式（`/tmp/...`）或相对路径，别用带盘符的 Windows 路径。
