# 做成 APP：三条路线与取舍

> 承接《交付形态调研.md》第九节。路线选定「手机整机自跑」，且暂不考虑 iPhone。

---

## 一、结论

**能，而且不用重写。** 现有 16,615 行 Python 里有 95% 可以直接进 APK，
真正的界面部分交给 WebView——因为所有产物本来就是 HTML。

只有一件事必须承认：这条路不是"上架商店的产品"，是"给自己和朋友装的应用"。
两者的合规成本差一个数量级，见 §4。

---

## 二、三条路线

| | A1 Termux（现状） | **A2 嵌入 CPython + WebView（推荐）** | A3 原生重写 |
|---|---|---|---|
| 复用现有代码 | 100% | **~95%** | ~30%（HTML 可复用） |
| 装机包 | Termux + 一个目录 | **~20 MB APK** | ~12 MB APK |
| 出 PNG | ❌ 无 Chromium | **✅ WebView 自己截** | ✅ 自己画 |
| 密码存放 | 明文文件（现状） | **Android Keystore** | Keystore |
| 首次启动 | 装 Termux + 敲命令 | 装 APK，点开 | 同左 |
| 后续维护 | 单份代码 | **单份代码 + 一层壳** | 两套逻辑，必然漂移 |
| 工作量 | 已完成 | 中（骨架已搭好） | 大 |
| 主要风险 | 用户门槛高 | Chaquopy 授权、后台限制 | 工期、双份维护 |

### 为什么不是 A3（原生）

原生重写听起来"正确"，但代价被低估了：光是把 `mi.py`（小米云 RC4 + 双签名）、
`metrics.py`（恢复度/负荷/ACWR）、`coach.py`（规则处方）、`card.py`（一图流版式）
搬一遍就是三四个千行；而且看板版式还在改，改一次要改两处。
**除非要做成对外产品、要上应用商店，否则不值得。**

### 为什么是 A2

1. **代码零第三方依赖**——这是决定性的。纯标准库意味着 Chaquopy 打包不用
   为 wheel 和 ABI 发愁，`sqlite3 / ssl / http.server` 全都自带。
2. **产物全是 HTML**——WebView 直接吃，版式一行都不用重画。
3. **顺手解决了 Termux 的最大短板**：没有无头 Chromium 出不了 PNG。
   APK 里用 `WebView.draw(Canvas)` 就能拿到 1080×1920，比 Termux 强。
4. **顺手解决了 P0 隐私问题**：密码进 Keystore，Python 侧彻底不留。

---

## 三、已交付的内容

| 文件 | 作用 | 验证 |
|---|---|---|
| `garmin_cn/appapi.py` | App ↔ Python 的唯一接口面，12 个函数全返回 JSON 化 dict | ✅ 本地实测 |
| `android/` 工程骨架 | Gradle + Manifest + Kotlin（MainActivity / Secrets）+ 图标 | ✅ **已编译出 APK** |
| `android/build.bat` | 一键构建（便携 JDK 17 + Gradle 8.7 + SDK 34） | ✅ 实测，增量 16 秒 |
| `android/sync_python(.sh/.bat)` | 把 `garmin_cn` 同步进 source set | ✅ 32 个 .py / 593 KB |
| `dist/garmin-coach-0.1.3-debug.apk` | 成品 | ✅ 23 MB，debug 签名，arm64-v8a |

构建环境（便携、免管理员，工具链路径通过环境变量配置）：
JDK 17（清华镜像）+ Gradle 8.7（华为镜像）+ SDK 34。
部分官方源在受限网络下不可达，可选镜像地址记在 `android/README.md`。

APK 实测结构：

```
package: com.garmincoach.app  versionName 0.1.3  minSdk 29  targetSdk 34
launchable-activity: com.garmincoach.app.MainActivity
python_version: 3.13   abi: arm64-v8a
assets/chaquopy/app.imy  →  33 个 garmin_cn/*.pyc（含 appapi）
签名：Android Debug（CN=Android Debug）
```

**真机进度**：0.1.0 首次实装时 Python 返回值转换崩了（dict → java.util.Map），
0.1.1 改成 JSON 字符串桥后启动、登录、同步、看板已在真机跑通。
0.1.2 去掉常驻悬浮按钮、加了活动图导出；PNG 出图的密度自适应尚未看实机产物。
后来在手机上撞到「分享预览四边被裁」：App 把 API 的 `height=2250` 直接当
位图高度，输出非 9:16 长图，系统分享面板又按预览框 centerCrop 补了一刀。
修复见 `garmin_cn/exportspec.py`：HTML 注入只读尺寸探针，App 实测缩放系数后
按内容裁空白、contain 收进 1080×1920；桌面 `actcard.shoot()` 同步改用
`pngutil.fit_to_frame()`，两端同一套口径。

0.1.2 的界面调整：两个常驻右下角按钮改成顶部独立占位的工具栏 + ⋮ 菜单，
WebView 不再被任何控件盖住；看板活动行写入 `data-aid`，点一下直接出该活动图。

0.1.3 进一步去掉「同步」「刷新」两个菜单项：改成 WebView 到顶后下拉刷新，
`PullRefreshLayout.kt` 手写、零 AndroidX 依赖，松手即同步 30 天 + 重出看板 + 刷新。
⋮ 里只剩今日一图流 / 跑步活动图 / 重新登录。

`appapi.py` 的接口面：

```python
env_info()                      # 环境自检
status()                        # 登录态 / 数据范围 / 今日恢复度与处方
login(email, password)          # 返回 need_mfa 时转去验证码
submit_mfa(code)                # 验证码是一次性的，内部持有同一个 auth 实例
sync(days, email, password)     # 会话过期自动续一次；email/password 不落盘
dashboard(date)                 # 出网页看板
card_html(date)                 # 出一图流 HTML（PNG 交给 WebView）
act_card_html(id)               # 活动图 HTML：画布 1080×2250，成品 1080×1920
runs(limit)                     # 最近跑步活动清单（选一条出图用）
start_server(port=0)            # 127.0.0.1 只读服务，返回实际端口
recent(limit)                   # 产物清单
read_html(path)                 # 只允许读 output/ 下的文件
```

实测结果：

```
env      → python 3.13.5 / can_shot true / data 路径正确
status   → logged_in true, 数据天数 / 活动条数 / 今日恢复度与等级（示例值已隐去）
dashboard→ ok, 33 KB HTML
card     → ok, 1080×1920 HTML
runs     → ok, 最近 5 条跑步（含 activity_id）
act_card → ok, 1080×2250 HTML（成品 1080×1920），无底图时矢量轨迹正常
server   → 随机端口起来，首页可访问（6613 字节）
read_html→ 越界读 config.json 被拒（False）
```

---

## 四、代价与坑（先看清再决定）

### 4.1 Chaquopy 授权

开源项目免费；**闭源商业用途要买 license**。自己用、送朋友不涉及。
如果哪天想收费，要么买授权，要么转 A3。

### 4.2 APK 体积与 ABI

Python 运行时按 ABI 打包。只留 `arm64-v8a` 约 20 MB；要兼容老机器加 `armeabi-v7a`
再涨 8 MB。**只留 arm64 是合理的**：2018 年后的手机基本都支持。

### 4.3 后台同步

Android 的 Doze 会掐后台线程。要"每天自动同步"，正确做法是
**前台服务 + 常驻通知**，或 `WorkManager`（最小间隔 15 分钟，且不保证准时）。
现在的骨架只做了"点一下同步"，切走可能被杀，这是最大的未完成项。

### 4.4 上架与合规

- **自己装（adb / 传 APK）**：无需任何资质，这是当前定位。
- **上应用商店**：健康类目要资质、要隐私政策、要"个人信息收集清单"；
  国内商店还要软著。这一步的成本远大于开发本身。

### 4.5 佳明条款

条款禁止的是"把账号凭据交给第三方"。APK 里凭据只在用户自己手机的 Keystore 里，
和 Termux 版边界一样，没有变坏。但**不能做"帮我跑一下"的代登录服务**——那才是越线。

### 4.6 逆向接口的寿命

国区 `/gc-api` 和 小米健康云都是逆向来的。接口一变，App 也得跟着发版。
Termux 版改一行 Python 就行，APK 要重新打包分发——这是壳的固有代价。

---

## 五、还没做的（按优先级）

1. **前台服务 + 通知**：让同步在后台不被掐（决定"能不能每天自动跑"）。
2. **WorkManager 每日定时**：设一个时间点自动同步 + 生成看板 + 发通知
   （现在得手动下拉一次）。
3. **更多原生入口**：轨迹回放、简报。活动图已能从看板点行或 ⋮ 菜单导出。
4. **设置页**：体重目标（cut/maintain/gain）、高德 Key、数据目录清理。
5. **分享脱敏**：activity 图含轨迹，分享前自动裁首尾 200 m。

---

## 六、建议的推进顺序

```
骨架能编译、能装上、能看到看板     ← 下一步（只需装 JDK 17 + Android SDK 34，Python 用现有的 3.13）
        ↓
前台服务 + 每日定时              ← 让"自动"这件事成立
        ↓
活动详情 / 回放入口               ← 补齐功能面
        ↓
（可选）上架前再评估 A3 原生重写
```

**先不要动 A3。** 等这个 App 真的被朋友天天用、并且开始嫌弃 WebView 的手感了，
再谈原生重写——到那时你已经知道哪些界面真正重要，重写才有意义。
