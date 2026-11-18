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

### 装完之后：关掉 SSH 密码登录

```bash
sudo sh deploy/harden.sh
```

它写一个 `/etc/ssh/sshd_config.d/99-runnercoach.conf`（不动主配置，升级不冲突）：
`PasswordAuthentication no` + `PermitRootLogin prohibit-password`，校验通过才 reload。

**会失去什么**：以后只能拿密钥登。云控制台的 Workbench 如果走的是密码认证，
也会连不上——那时用控制台的 **VNC** 通道进去（VNC 不经过 sshd）。
所以脚本开头会先确认 `authorized_keys` 里至少有一把公钥，没有就拒绝执行。

撤销：删掉那个文件 → `systemctl reload ssh`。

## 六点五、素材按需下发（字体 / 结局插画）

远征页的衬线字体和五张结局插画不进 APK 了，改成首次用到时从
`/v1/content/` 取，缓存在 `GARMIN_HOME/content/`。

- 服务端：素材放 `server/assets/`（**入库**，要版本控制），由
  `/v1/content/index`（清单 + sha256）和 `/v1/content/<name>` 下发
- 客户端：`contentpack.py` 负责补；App 启动时后台预取，结算页最多等 1.2 秒
- 拿不到也不影响页面：字体回落系统衬线，结局图退化到纯 SVG
- 收益：APK 23.9 → 21.65 MB；改插画/字体不用发版

**素材改动后要重新部署 `server/assets/`**，否则 `/v1/content/index` 里的
sha256 和实际文件对不上，客户端会反复重下（校验不过会丢弃）。

顺带修了一个老问题：字体原来「叫 woff2、内容是未压缩 TTF」，因为
`opts.flavor` 不会影响 `font.save()`。加上 `font.flavor = "woff2"` 之后
1.54 MB → 714 KB，而且这回浏览器真的会用上它（以前解码失败、静默回落）。

## 六点八、代码包：发版不用重下 21 MB

APK 里始终带完整代码；**改代码时只下发差异包**，客户端解压到私有目录、
挂在 `garmin_cn.__path__` 最前面，同名模块被盖掉。

```bash
# 整包（正常发版）——86 个模块，约 1.3 MB
python tools/publish_code.py --host <IP> --version 1.5.0-7 --notes "结算页改版"

# 只发一个模块（**仅用于验证机制**，正式发版别用）
python tools/publish_code.py --host <IP> --version 1.5.0-8 --only easteregg --probe
```

- `--probe` 会塞一个 `_packprobe.py`：只有代码包真的接管了导入才 import 得到。
  App 的「我的 → 诊断日志」顶部会显示 `包外代码 <版本>`，用来确认这件事。
- **`codepack.py` 永不进包**：它是装载器，必须始终来自 APK。否则一个坏包会
  把装载逻辑自己换掉，下一次连回退能力都没有。
- 生效时机是**下次启动**，运行中不热替换。所以发完之后用户要**开两次**：
  第一次下载（诊断显示「未挂载（已下载 X，重启后生效）」），第二次才挂上。
- 下载失败 / 校验不过 / 目录损坏 → 一律回落到包里那份，行为与今天完全一致。

### 写代码时的硬约束（踩过，别再犯）

代码包只覆盖 `garmin_cn/*.py`，**`codepack.py` 永远来自 APK**。所以会出现
「新业务代码 + 旧装载器」的组合，规则两条：

1. **基础能力不能依赖装载器的新 API**。路径解析、证书定位这类必须在模块内
   自己实现（`tlstrust` / `contentpack` / `reading` 各自内联了 `_pkg_file`）。
   写过一版调 `codepack.pkg_file` 的，结果是旧 APK 上 `AttributeError` 被
   `except` 吞掉 → 证书找不到 → 所有 HTTPS 失败。表现像「装完就登不上」。
2. **`Path(__file__)` 不能用来定位数据**。挂载后所有模块都从
   `HOME/code/<ver>/garmin_cn/` 加载，`__file__` 指向下载目录：
   - 包内资源（证书 / 条款 / 插画）→ 顺 `garmin_cn.__path__` 往后找
   - 用户数据（缓存 / 手册 / 记录）→ 跟 `GARMIN_HOME`
   两类混了就会「发一次版丢一次数据」，而且不报错，只是空的。

### 为什么不能把 APK 做到 2 MB

Android 10+ 明确禁止从可写目录 `dlopen()` / `execve()`（W^X）：
「应用应只加载嵌入在应用 APK 文件中的二进制代码」。我们 `targetSdk = 34`，
所以 libpython / OpenSSL / sqlite 这 11.75 MB 必须在 APK 的 `lib/` 里。
带 Python 的 APK 下限约 13 MB；要到 2–3 MB 只能放弃本地 Python，那是重写。

## 七、发版：安装包走自己的服务器

源码转私有之后，Gitee Release 就不能匿名下载了，App 的更新入口会断。
所以安装包改由这台服务器分发：

```bash
# 打好的包在 android/app/build/outputs/apk/release/app-release.apk
python tools/publish_apk.py --host <IP> --notes "这一版改了什么"
```

它读出 `build.gradle.kts` 里的版本号 → 传 APK 到 `/opt/runnercoach/apk/`
→ 写 `latest.json`（版本、大小、SHA256、说明）。App 启动时读
`https://<IP>:8443/v1/app/latest`，再去 `http://<IP>:8088/apk/<文件>` 下载。

**App 内部更新走 8443，不走 8088**（1.5.2 起）：

| 路径 | 谁在用 | 为什么 |
|---|---|---|
| `https://<IP>:8443/v1/apk/<名>` | App 内的「下载更新」 | Python + 内置证书；能过 networkSecurityConfig |
| `http://<IP>:8088/apk/<名>` | 浏览器手动下载（兜底） | 浏览器不拦明文，用户复制链接就能装 |

这条路是为了修一个实测到的问题：**用系统 DownloadManager 下 apk 会立刻失败
（0.00 B）**。两个原因叠在一起——

1. DownloadManager 只认系统根证书，自签证书握手直接失败；
2. 退回明文 http 又被 `networkSecurityConfig` 拦住（targetSdk 34 默认禁明文，
   这条策略同样覆盖 DownloadManager，且 `domain` 里的 IP 不做通配）。

所以改由 Python 下载（代码包本来就这么干），下完用自写的 `ApkProvider`
把 `content://` 交给系统安装器。8088 留着是为了「浏览器兜底」和旧客户端。

**防火墙仍要放行 8088**（浏览器兜底用）；8443 本来就得开。

## 八、日常

| 事 | 做法 |
|---|---|
| 备份 | `curl -H 'X-Admin-Token: xxx' https://IP:8443/admin/db -o gw.db`，或直接 tar `/opt/runnercoach/data` |
| 升级 | `git pull` → `sudo sh deploy/install.sh <IP>`（幂等，不覆盖证书与 env）→ 自动重启 |
| 看日志 | `journalctl -u runnercoach -f` |
| 发额度 | `/admin/codes` 生成兑换码，App 内输入 |
| 换 IP | 重新 `gen-cert.sh` → `set_server.py` → 重发 APK（IP 烧在包里，绕不开） |

## 九、什么时候该换成域名

出现下面任一条就该买域名 + 备案，而不是继续用 IP：

1. 要给别人一串能记住的地址（IP 没法口头传播）；
2. 服务器换机房/换 IP（每次都要重发 APK）；
3. 用户超过二三十个，客服成本开始高于备案成本。

换的时候**不用改代码**：域名 + 正规证书后，`tools/set_server.py https://域名`
即可，`tlstrust` 发现包里没有 `server-ca.pem` 就自动走系统根证书。

## 十、已知边界（写给自己看）

- **自签证书不是"更安全"**，只是"没有域名时唯一能用的 HTTPS"。它的信任来自
  你把证书烧进了 App，而不是来自任何 CA。
- 证书 10 年，但要记住：**换证书 = 重发 APK**。所以别在证书里写临时 IP。
- 现在没有速率限制之外的防护（`_rate_limit` 只覆盖上报和验证码）。公网暴露
  一个 API，被扫是常态；`ADMIN_TOKEN` 要够长，别用默认值。

## 十一、Windows 开发机注意

`set_server.py` 里对证书做的 SAN 校验用的是 `openssl`（Git for Windows 自带）。
没有的话它会跳过这项检查并提示——不会静默放过，但也就少了一道保险，
装一个 OpenSSL 或者干脆在 Linux 上跑这一步。

`deploy/smoke.sh` 要在 Git Bash 里跑（`sh deploy/smoke.sh ...`），
路径用 MSYS 形式（`/tmp/...`）或相对路径，别用带盘符的 Windows 路径。
