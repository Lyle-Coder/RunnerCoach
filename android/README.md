# 安卓 App（Python 内核 + WebView 壳）

这不是把逻辑重写了一遍，而是给现有的 `garmin_cn/` 套了个壳：
APK 里跑的就是同一份 Python，界面是指向本机 `127.0.0.1` 只读服务的 WebView。

```
┌─────────────── APK ───────────────┐
│  MainActivity (Kotlin)            │
│    ├─ 顶栏 ⋮ 菜单（不遮挡内容）      │
│    ├─ 下拉刷新 → appapi.sync()      │
│    ├─ 起始 → appapi.start_server() │
│    ├─ WebView → 127.0.0.1:随机端口  │
│    ├─ 点活动行 → act_card_html+截图  │
│    ├─ 一图流 → card_html + 截图     │
│    └─ Secrets (Keystore AES-GCM)   │
│  Chaquopy: CPython 3.11 + garmin_cn│
│  数据: /data/data/<pkg>/files/      │
└───────────────────────────────────┘
```

## 构建前要装的两样

| 需要 | 为什么 | 备注 |
|---|---|---|
| **JDK 17** | AGP 8.5 要求 | 用 Android Studio 自带的即可 |
| Android SDK 34 + Build-Tools | 编译 | 装 Android Studio 最省事，自带 |

**Python 不用另装**：Chaquopy 17.2 支持 3.10~3.15，本机已有的 3.13 直接能用。
唯一要求是构建机的 Python 与 App 目标版本同小版本（`app/build.gradle.kts` 里
`version = "3.13"`，两边一起改）。

```bat
winget install --id EclipseAdoptium.Temurin.17.JDK -e
:: Android Studio：https://developer.android.com/studio
```

## 构建

推荐直接用 Android Studio（自带 JDK 17 与 SDK）。若使用便携工具链，
把路径写进 `android/toolchain.local.bat`（已加入 `.gitignore`，不会提交），
或设置环境变量 `ANDROID_TOOLCHAIN`；`build.bat` 会自动解析，不改系统 JAVA_HOME：

```
<toolchain-dir>\
├─ jdk-17.0.20.1+1\        JDK 17（Temurin）
├─ gradle-8.7\             Gradle 8.7
└─ sdk\                    Android SDK：platforms;android-34 / build-tools;34.0.0 / platform-tools
```

装法是便携包解压，不需要管理员权限。若官方源不可达，可改用下列镜像：

| 组件 | 源 | 备注 |
|---|---|---|
| JDK 17 | `mirrors.tuna.tsinghua.edu.cn/Adoptium/...` | api.adoptium.net 不可达时的备选 |
| Gradle 8.7 | `mirrors.huaweicloud.com/gradle/...` | services.gradle.org 不可达时的备选 |
| cmdline-tools / SDK | `dl.google.com` | 官方源 |

一键构建（推荐）：

```bat
cd android
build.bat                  :: debug 包
build.bat assembleRelease  :: release 包
```

它做两件事：`sync_python.bat` 把 `..\garmin_cn` 复制进 `src/main/python`，
然后调用工具链里的 Gradle。产物在
`app\build\outputs\apk\debug\app-debug.apk`。

装了 Android Studio 的话也可以直接打开 `android/` 目录点 Run——它会生成
gradle wrapper 并自己下载依赖（Studio 内置 JDK 17，不必用上面的便携版）。

PATH 里的 `python3` 指不到 3.13 时（Windows 上很常见，可能是 Store 的占位符），
显式指定：

```bat
build.bat -PbuildPython="<path-to-python3.13>"
```

产物体积 **约 23 MB**（只打 `arm64-v8a`，Python 3.13 运行时已含）。
2020 年之后的手机基本都能装。

### 装到手机

```bat
:: 数据线连上、开 USB 调试
adb install -r dist\garmin-coach-<version>-debug.apk
```

没有 adb 也行：把 APK 用微信/网盘传到手机，点开安装（需要允许"安装未知来源应用"）。

### 首次启动会发生什么

1. 解压 Python 运行时（1~2 秒，只有第一次）；
2. 起一个只听 127.0.0.1 的只读服务，WebView 打开它；
3. 检测到没登录 → 直接弹登录框；
4. 登录成功（可能要邮箱验证码）→ 自动同步 30 天 → 自动出看板；
5. 之后**在页面顶部下拉**即可同步最近 30 天 + 重出看板；⋮ 菜单里可导出一图流，
   或进「历史数据」补拉更早的区间 / 按日期查看板 / 查历史活动。

数据在 `/data/data/com.garmincoach.app/files/`，卸载即清空；
密码用 Android Keystore 加密存在 SharedPreferences，Python 侧不落密码。

### 界面交互（下拉同步 / ⋮ 菜单 / 历史数据）

- **同步没有按钮**：页面滚到顶后继续下拉 →「↓ 下拉同步 / ↑ 松开立即同步」→ 松手执行
  `sync(30 天) + 重出看板 + 自动刷新`；指示器依次显示「正在同步… / √ 已更新」并回弹。
  手势由手写的 `PullRefreshLayout.kt` 处理（约 150 行，**不引 AndroidX**）：
  只在 `WebView.scrollY == 0` 且向下拖时才接管，页面中间的正常滚动完全不受影响；
  带 0.5 阻尼、36dp 触发阈值、66dp 位移上限、回弹动画。首次未登录时下拉会先
  收起指示器再弹登录框，登录成功自动接着同步。
- **没有悬浮按钮**。顶部是一条独立占位的 52dp 工具栏（标题 + 状态 + ⋮），
  WebView 只占它下面那块，任何内容都不会被盖住。
- ⋮ 菜单只放低频动作：今日一图流 / **历史数据** / AI 教练 / 模型服务设置 /
  小米体重 / 账号登录。日常同步与刷新仍不单列——下拉刷新本身就是
  「同步 + 刷新」，再放一个按钮反而让人分不清哪个联网。
- **「历史数据」补的是下拉刷新覆盖不到的两件事**（下拉固定只拉近 30 天）：
  - **拉取更多历史数据**：90 / 180 / 365 天或自定义日期区间（一次最多 3 年），
    走 `appapi.sync_range()` → `sync(days, end, activity_pages)`。仍是增量，
    已稳定的日子跳过；活动列表按 60 条一页自动翻到区间起点
    （`sync.py: sync_activities(max_pages, oldest)`），否则补了 365 天日汇总、
    活动却还只有最近 60 条。
  - **查询历史**：`days_index()` 先报出已覆盖的日期范围，然后可选
    「按日期查看看板」（`dashboard(date, live=false)` 直接加载本地 HTML）、
    「历史活动列表」（`activities()` 按日期/类型/分页查本地库，点一条走
    分享前脱敏三档再出活动图）；另有「返回今日看板」回到首页。
  - 桌面等价命令：`run.bat sync --start 2026-01-01 --end 2026-11-06 --no-cards`。
- **看板里的活动表可以直接点**：`report.py` 给每条活动行写了 `data-aid`，
  App 注入一小段 JS 把点击转给 `GC.activity(id)`，随即出该活动的图。
  桌面版看板里这个属性不绑事件，完全无副作用。
- 两种出图都走同一条链路：`appapi` 生成 HTML → 离屏 WebView 画成 PNG →
  存到 `相册/GarminCoach/` → 弹系统分享（可直接发微信）。
- 两种**成品**都统一为 1080×1920（9:16），四周留一圈与卡片同色的安全带，
  系统分享面板按自己的预览框 centerCrop 也切不到内容。活动图 HTML 的布局
  基准仍是 1080×2250，App 截完按底色找内容底边裁掉空白，再等比收进 9:16
  （只缩不裁），与桌面 `pngutil.fit_to_frame()` 同一套规则。没有高德底图时
  用纯 SVG 矢量轨迹回落，所以断网也能出图。首次出某条活动需要联网抓一次
  明细，之后明细落库就一直离线可用。
- **0.1.5 修复「分享预览只剩放大一角」**：HTML 写死
  `width=1080,initial-scale=1`，布局视口是 1080 CSS px，而离屏控件只有
  1080 物理像素（≈360dp），页面按屏幕密度被放大到画布之外，`draw(Canvas)`
  只截到左上角。现在 App 先用 `setInitialScale(100/density)` 争取 1:1，
  再由 HTML 探针读 `visualViewport.width` 实测「能看见多少 CSS px」，给版心
  套 `transform: scale(f)`（f = 可视 CSS 宽 / 画布 CSS 宽）后等一帧再截。
  发生校正时会套 `transform` 并按上面的公式出图；出图时会 toast 一次
  「0.1.5 出图缩放 ×0.xx」，可据此确认新包是否生效。
- **点活动出图会先弹「分享前脱敏」（0.1.4 起）**：原图（含完整 GPS 轨迹）/
  裁首尾各 200 m（默认）/ 完全隐藏轨迹与起跑时间。选择存在 `gc_prefs`，下次沿用。
  这一问同时是「分享前确认」，点「生成并分享」才会出图并弹系统分享。
  档位作为 `act_card_html` 的第 5 个参数传给 Python，由 `garmin_cn/privacy.py`
  在生成 HTML 之前处理；脱敏版不叠高德底图、不含原始折线，连 `output/*.html`
  都是干净的。桌面等价命令：`run.bat act --card --privacy trim|hide`。
- 外链不会留在 WebView 里打开，统一交给系统浏览器——JS 桥只暴露给
  `127.0.0.1` 的本机页面。

## 踩过的坑（都是真机上撞出来的）

**1. `TypeError: Cannot convert dict Object to java.util.Map`**

Chaquopy 不支持把 Python dict 转成 `java.util.Map` 接口：

```kotlin
api.callAttr("status").toJava(Map::class.java)   // ✗ 运行时抛 TypeError
```

现在全部走 JSON 字符串桥：

```kotlin
api.callAttr("call", "status", "[]").toString()  // → JSON 文本
JSONObject(...)                                   // 用 Android 自带的 org.json 解
```

好处是嵌套结构（`sync` 返回的 `errors` 列表、`body` 子对象）也不会丢，
而且 Python 侧 `call()` 统一兜异常，Kotlin 只需要看 `ok`。

**2. Kotlin DSL 里没有 `android.defaultConfig.python {}`**

那是旧版 Groovy DSL 的写法，KTS 下会 Unresolved reference。必须用顶层块：

```kotlin
chaquopy {
    defaultConfig {
        version = "3.13"
        buildPython("python3")
    }
}
```

**3. `.bat` 里不能写中文注释**

`cmd.exe` 按 OEM 代码页（GBK）解析批处理文件，UTF-8 中文会被拆成乱码，
把 `rem` 后面的命令一起吞掉，报一堆 "不是内部或外部命令"。
`build.bat` 和 `sync_python.bat` 因此改成纯 ASCII + `chcp 65001`。

**4. 不用 `PyApplication`**

它在 `Application.onCreate` 就启动解释器，早于我们设置 `GARMIN_HOME`，
结果是数据写到只读的 assets 目录。现在在 `MainActivity.onCreate` 里
先 `Os.setenv` 再 `Python.start(AndroidPlatform(this))`。

## 关键设计决定

**为什么用 WebView 而不是原生界面**：看板、一图流、活动图本来就是 HTML，
而且改版最频繁。原生重画等于维护两套版式，迟早走形。WebView 直接吃现有产物，
Python 侧一行都不用改。

**为什么用 Chaquopy**：项目零第三方依赖，打包不用为 wheel 发愁；
纯标准库（`sqlite3` / `ssl` / `http.server`）Chaquopy 全都带了。

**为什么出图能成**：Termux 的死结是"没有无头 Chromium"。APK 里不需要浏览器，
WebView 自己就能 `draw(Canvas)` 出 PNG——这条比 Termux 强。
截图前会 `translationY = 8000f` 挪到屏幕外：必须挂进窗口才会真正布局渲染，
但 `draw(canvas)` 画的是它自己的内容，不受位移影响。

离屏 WebView 只开 `useWideViewPort`，**关掉 `loadWithOverviewMode`**：HTML
写死 `width=1080,initial-scale=1`，布局视口恒为 1080 CSS px，而控件只有
1080 物理像素（≈360dp），overview 认为「内容没比视口宽」反而不缩，页面就按
`density` 倍画到画布外，只截到左上角。现在先 `setInitialScale(100/density)`
争取 1 CSS px = 1 物理 px，再由 HTML 末尾的只读探针
`window.__gcExportInfo()` 回报 `visualViewport.width`（可视视口 CSS 宽，
不是 `innerWidth` 那个布局视口），App 按 `f = 可视宽 / 画布宽` 调
`window.__gcFit(f)` 给版心套 `transform: scale(f)`，等一帧重绘后才截。
最后按底色找内容底边裁空白、contain 收进 9:16 并留同色安全带。
另外设 `textZoom=100`（挡系统字体缩放）和 `LAYER_TYPE_SOFTWARE`（硬件加速下
离屏 WebView 常画不进 Canvas）。真机对账看 `adb logcat -s GarminCoach` 的
`render probe=...`（含 `visibleCss` / `f`）与 `render view=... out=...` 两行；
出图时还会 toast 一次「0.1.5 出图缩放 ×0.xx」。

**密码为什么能存**：佳明的 JWT 只有 2 小时，想"点一下就同步"就必须能自动续期，
也就必须留密码。这里用 Android Keystore 里的 AES-GCM，密钥不出 TEE，
拷走 xml 也解不开，换机自动失效。**Python 侧不落密码**（`appapi.login()` 只留会话）。

## 已知限制（按优先级）

1. **仍需一张真机产物复核**：0.1.1 起 Python 启动、登录、同步、看板已在真机
   跑通。分享预览曾出现「只剩放大一角」（日期缺字、轨迹只剩一角），0.1.3
   统一 9:16 后仍复现，0.1.5 才定位到是 `initial-scale=1` 与控件物理像素
   宽度不匹配导致的 WebView CSS 缩放错位，改成 `visualViewport.width` 自校正。
   本机 Chromium 已用窄视口 + 高 dpr 复现并验证修复前后差异；真机若仍异常，
   把 `adb logcat -s GarminCoach` 的 `render probe=` / `render view=` 发出来定位。
2. **后台同步不可靠**：现在同步跑在普通线程里，切后台可能被系统掐。
   要稳得上前台服务 + 常驻通知，或 WorkManager 定时。
3. **没有每日自动同步**：`WorkManager` 还没接，得手动下拉一次。
4. **轨迹回放、简报没有原生入口**：本机服务首页里能点开，只是没做按钮。
   活动图已有入口（看板点行 + ⋮ 菜单）。
5. **首次启动慢 1~2 秒**：Chaquopy 要解压 Python 运行时，只在第一次。
6. ~~分享不脱敏~~：0.1.4 起活动图出图前会问档位，默认裁首尾 200 m。
7. **补历史没有逐日进度**：`sync_range()` 是同步 JSON 调用，桥里没有回调，
   界面只能显示顶部进度条与「正在补历史 …」。365 天可能要几分钟，期间
   不能取消；`sync_range` 已限制单次最多 3 年，且默认走增量跳过缓存。
   剩下「家附近 500 m 模糊化」和轨迹回放 MP4 导出前的自动脱敏还没做；
   选「原图」或发回放视频前仍需自己看一眼。

## 和 Termux 版的关系

两套并行、不冲突：Termux 版给愿意折腾的人（零构建、100% 代码复用），
APK 给不想碰命令行的人。Python 代码是同一份，`appapi.py` 是唯一的接口面。

改了 `garmin_cn/` 之后记得 `sync_python.bat`，否则 APK 里还是旧代码
（这是唯一容易踩的坑，`.gitignore` 里已经把那份复制品排除掉了）。
