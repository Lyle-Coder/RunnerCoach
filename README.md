# RunnerCoach

> 本地优先的佳明国区训练看板与电子教练。账号凭据、健康数据和 GPS 轨迹只留在你自己的设备上。

RunnerCoach 把 Garmin Connect **中国区**（`connect.garmin.cn`）的数据同步到本机 SQLite，
用纯规则引擎算出恢复度、训练负荷、营养目标与今日处方，再产出**可离线打开**的 HTML 看板、
竖版图片、单次活动图和轨迹回放。可选的安卓 App 只是这套 Python 内核的查看与出图前端。

本项目是**个人自用的本地工具**，不是佳明官方产品，也不提供任何托管服务。

---

## 设计原则

| 原则 | 具体做法 |
|---|---|
| **本地优先** | 没有服务端、没有账号体系、没有遥测。凭据从输入到会话全程留在本机，不代他人登录。 |
| **断网可用** | 只有「同步」这一步需要联网；指标计算、出图、看板、浏览全部离线。`demo` 用仿真数据，连账号都不需要。 |
| **零第三方依赖** | 全部 Python 标准库（`http.client` / `sqlite3` / `ssl` / `http.server`），`pip install` 那一步不存在。 |
| **大模型不是内核** | 代码里**没有任何模型 API 调用**。恢复度、处方、结论全由规则计算；大模型只可能用于外部生成一段可选的点评文字（`note`），删掉不影响任何判断。 |
| **产物自包含** | 看板 HTML 无外部请求、无 CDN、无字体外链，可以直接当文件发给别人。 |

> 一个重要的合规边界：佳明服务条款不允许向第三方共享账号凭据。因此本项目**永不**做成
> 「你把账号填进来、我帮你拉数据」的网站或小程序——那属于结构性违约，不是注意一下就行。

---

## 功能一览

标注说明：**联网** = 只有这一步需要网络；**模型** = 是否依赖大语言模型。

| 分类 | 功能 | 联网 | 模型 |
|---|---|:---:|:---:|
| 接入 | 国区 SSO 登录、邮箱/图形验证码、Cookie 导入、会话自动续期 | 仅同步 | ❌ |
| 接入 | 日汇总 / 睡眠 / HRV / 压力 / 身体电量 / 活动列表与 1Hz 明细 / 历史补齐 | 仅同步 | ❌ |
| 接入 | 体成分 CSV 导入、小米健康云体重（可选）、照片 EXIF 归组 | 部分 | ❌ |
| 计算 | 恢复度、睡眠债、CTL / ATL / TSB / ACWR、步频步幅重算 | ❌ | ❌ |
| 计算 | 体重体成分趋势与 RED-S 联合判断、营养目标、今日处方、简报结论 | ❌ | ❌ |
| 产出 | 自包含每日看板 HTML、1080×1920 一图流、单次活动图 1080×1920（9:16） | ❌ | ❌ |
| 产出 | 轨迹回放网页 / MP4、纯文本微信简报、教练上下文 Markdown | ❌ | ❌ |
| 产出 | 高德静态底图（可选，需自己的 Key）；无 Key 时回落纯 SVG 矢量轨迹 | 仅取图 | ❌ |
| 集成 | 佳明训练计划读取、本地课表推送、饮食 / 小睡 / 睡眠矫正、MCP 工具、离线自检 | 部分 | ❌ |
| 集成 | `note` 写入一段教练点评文字 | ❌ | 可选* |

\* 点评文字可以由任何外部工具（包括大模型）生成后写入；仓库里不含生成逻辑，也不含任何 API Key。

完整清单与口径见 [`docs/交付形态调研.md`](docs/交付形态调研.md) 与 [`docs/使用手册.md`](docs/使用手册.md)。

手机端（APK）出图不走无头浏览器，而是离屏 WebView 自绘；
[`garmin_cn/exportspec.py`](garmin_cn/exportspec.py) 统一了两端的 1080×1920 画幅，
并按 `visualViewport.width` 校正 WebView 的 CSS 缩放，避免分享预览只剩放大后的一角。

---

## 快速开始

### 环境

- **Python 3.11+**（Windows 用 `py -3`，POSIX 用 `python3`）
- Windows：`run.bat`；Linux / macOS / Termux：`./run.sh`
- 出 PNG 需要本机 Chrome/Edge（无头截图）；导出 MP4 还需要 `ffmpeg`。**没有它们也能用**：出图会自动退化为 HTML，页面自带视口声明，手机打开即可整张查看。
- 仅适用于**佳明国区账号**，国际区接口不同。

```bash
git clone https://gitee.com/liyufei-lyle/RunnerCoach.git
cd RunnerCoach
```

### 先看效果（不需要账号）

```bat
run.bat doctor          :: 环境自检
run.bat demo --open     :: 用 60 天仿真数据生成看板
```

```bash
./run.sh doctor
./run.sh demo
```

产物在 `output/`，打开 `output/demo.html` 即可。`selftest` 是完全离线的回归自检，改完代码先跑它。

### 正式使用

```bat
run.bat login                    :: 交互输入账号密码，可能需要邮箱验证码
run.bat sync --days 30           :: 同步最近 30 天
run.bat report --open            :: 生成看板 output/latest.html
run.bat card                     :: 生成 1080×1920 一图流
```

POSIX 把 `run.bat` 换成 `./run.sh`。`daily.bat` / `daily.sh` 把「同步 + 看板 + 一图流」串成一条命令。

> **首次登录前请读一遍隐私说明**：`config.json`、 `data/session.json` 等都等同于账号凭据，
> 它们已被 `.gitignore` 排除，**不要**把整个项目目录打包发给别人。

### 常用命令

| 命令 | 作用 |
|---|---|
| `doctor` / `status` / `selftest` | 环境自检 / 数据状态 / 离线回归 |
| `sync --days N` / `backfill` | 增量同步 / 补齐历史明细 |
| `report` / `card` / `brief` | 看板 / 一图流 / 可发微信的纯文本简报 |
| `act --id <activity_id> --card` | 单次活动明细与活动图（分圈、曲线、轨迹） |
| `today` / `plan` / `push` / `note` | 今日处方 / 佳明计划 / 推送课表 / 写点评 |
| `body` / `nutri` / `meal` / `nap` / `sleepfix` | 体成分 / 营养目标 / 饮食 / 小睡 / 睡眠矫正 |
| `motion` / `ingest` / `photo` | 轨迹回放 / 素材归组 / EXIF 检查 |
| `serve [--lan]` | 把 `output/` 挂成只读页面，手机同 WiFi 可看 |
| `mi` / `key` | 小米健康云（可选） / 高德 Key 配置（可选） |

完整参数见 `run.bat <命令> --help`，详细说明见 [`docs/使用手册.md`](docs/使用手册.md)。

---

## 三种使用形态

| 形态 | 适合谁 | 出 PNG | 门槛 |
|---|---|:---:|---|
| **桌面 CLI + 本地网页** | 有电脑、想自己改代码 | ✅ | 装 Python，敲命令 |
| **安卓 APK**（[`android/`](android/)） | 只用手机、不想碰命令行 | ✅ WebView 截图 | 装 APK，下拉同步 |
| **Termux / 局域网只读** | 愿意折腾 / 只想看结果 | HTML 版 | `setup-termux.sh` 或 `serve --lan` |

`serve` 默认只监听 `127.0.0.1`；加 `--lan` 才暴露到局域网，并自动生成访问码，
日志不记录查询串。它是**只读**的，没有任何写入口。

APK 版通过 [Chaquopy](https://chaquo.com/chaquopy/) 内嵌 CPython，界面是 WebView，
密码存 Android Keystore；构建与设计取舍见 [`android/README.md`](android/README.md)
和 [`docs/APP方案.md`](docs/APP方案.md)。APK 不进仓库，通过 Release 或朋友间互传分发。

---

## 目录结构

```
RunnerCoach/
├─ cli.py                 命令行入口（所有子命令）
├─ run.bat / run.sh       Windows / POSIX 启动器（自动挑可用的解释器）
├─ daily.bat / daily.sh   同步 + 看板 + 一图流
├─ setup-termux.sh        Termux 一次性安装
├─ mcp_server.py          MCP 服务（供支持 MCP 的 AI 工具调用）
├─ config.example.json    配置模板（复制为 config.json，后者不提交）
├─ garmin_cn/             核心包：接入 / 计算 / 产出（见下）
├─ android/               Kotlin + WebView 壳、Gradle 工程、构建脚本
├─ tools/                 开发期小工具（版面审计、图片分带、EXIF 等）
├─ docs/                  使用手册、接口字段清单、架构与交付方案
├─ data/                  SQLite 与会话（本机生成，不提交）
└─ output/                所有产物（本机生成，不提交）
```

`garmin_cn/` 的分层：

```
接入层   net.py / auth.py / client.py / sync.py / store.py / mi.py
计算层   metrics.py / activity.py / body.py / nutrition.py / coach.py / raceplan.py
产出层   report.py / card.py / actcard.py / motion.py / video.py / basemap.py / brief.py
集成层   plan.py / workout.py / manual.py / serve.py / appapi.py / sysopen.py
```

`appapi.py` 是安卓 App 与 Python 之间**唯一**的稳定接口面，全部函数返回 JSON 化的 dict，
不打印、不交互。改 Python 逻辑时尽量不要破坏它的签名。

---

## 隐私与安全

这是本项目最需要协作者共同守住的边界。

1. **绝不提交真实数据。** `.gitignore` 已排除 `config.json`、`data/`、`output/`、`site/`、
   `dist/`、`*.db`、`mi_login.json`、`session.json`、APK 与本地工具链配置。
   PR 里若出现任何真实账号、活动 ID、GPS 坐标、体重睡眠数值或设备 ID，一律要求重做。
2. **凭据不落盘是首选。** 登录成功后只保留会话；若配置了自动续期所需的密码，
   它只存在本机 `config.json`（安卓版存 Keystore），从不进入版本控制或日志。
3. **不要代登录。** 不写「用户填账号 → 服务端代拉」的功能，不收集中枢式健康数据。
4. **轨迹 = 精确行踪。** 活动图含 GPS 轨迹，起终点几乎等于家庭住址。0.1.4 起内置
   分享脱敏（`garmin_cn/privacy.py`）：桌面 `act --card --privacy trim|hide`，App 出图前
   弹档位确认、默认首尾各裁 200 m。脱敏在生成 HTML **之前**完成，产物里不留原始折线、
   底图或精确起跑时间；「家附近模糊化」仍属待办，回放 MP4 导出前也仍需自己看一眼。
5. **高德 Key 不内置。** 没有 Key 时功能回落为矢量轨迹，保持这个设计。
6. **发现泄漏**：不要直接在公开 Issue 里贴敏感内容，先私下联系维护者，
   必要时改写历史并轮换凭据。

更多背景见 [`docs/交付形态调研.md`](docs/交付形态调研.md) 第五节。

---

## 协作开发

欢迎一起改进。几条约定：

- **先开 Issue 再动大改**，尤其是接口协议、指标口径和产物版式——这三层被文档与 App 同时依赖。
- **保持零第三方依赖**。要用新库，先在 Issue 里说明理由；标准库能做的就别引。
- **不产生外部请求**。看板和图片必须能 `file://` 离线打开，禁止 CDN、远程字体、统计脚本。
- **改完跑自检**：`run.bat selftest`（或 `./run.sh selftest`）+ `py -3 -m py_compile` 关键模块。
  涉及 App 接口时再冒烟一次 `python -m garmin_cn.appapi env`。
- **改了 `garmin_cn/` 要重新构建 APK**：先跑 `android/sync_python.bat`（或 `.sh`），
  那份复制品不进仓库。
- **示例数据只用 `demo`**。测试里不要写真实活动 ID、日期、坐标或健康数值。
- 提交信息用简短中文或英文均可，说明「改了什么、为什么」。

### 待办路线

- [ ] 后台每日自动同步（前台服务 / WorkManager），目前需手动下拉
- [x] 分享前轨迹脱敏（首尾各裁 200 m / 完全隐藏轨迹）；「家附近模糊化」仍待办
- [ ] 纯离线导入模式：直接吃佳明导出的 FIT / TCX / CSV，全程不登录
- [ ] `config.json` 密码改为系统凭据库（DPAPI / keyring），与代码目录分离
- [ ] App 内补齐轨迹回放、简报等入口；真机复核 9:16 出图产物

---

## 文档索引

| 文档 | 内容 |
|---|---|
| [`docs/使用手册.md`](docs/使用手册.md) | 全部命令、登录排错、指标口径、底图、回放与素材流程 |
| [`docs/activity-data-inventory.md`](docs/activity-data-inventory.md) | 国区接口分层与字段清单（已去除全部真实样本数值） |
| [`docs/交付形态调研.md`](docs/交付形态调研.md) | 为什么选本地形态、功能清单、隐私检查项 |
| [`docs/APP方案.md`](docs/APP方案.md) | 安卓 App 的路线取舍、接口面与踩坑记录 |
| [`docs/发布流程.md`](docs/发布流程.md) | 版本与 Tag 规范、打包与校验、Gitee Release 上传、APK 分发与回滚 |
| [`android/README.md`](android/README.md) | APK 构建、交互设计与已知限制 |

> `tools/make_plan.py` / `make_card.py` 是个人备赛课表生成器，含真实赛事与训练史，**不在仓库中**。
> 缺少它们时 `today` / `push` 会自动回退到佳明教练计划，其他功能不受影响。

---

## 许可与免责

许可：见 [LICENSE](LICENSE)。

- 本项目与 Garmin、佳明、小米、高德均无关联，属个人自用工具，使用官方接口的风险由使用者自行承担。
- 接口可能随服务端调整而失效，欢迎提交 Issue / PR 一起维护。
- 请勿将本项目用于代他人登录、收集他人健康数据或任何商业托管服务。
