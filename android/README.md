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
5. 之后**在页面顶部下拉**即可同步 + 重出看板；⋮ 菜单里可导出一图流或跑步活动图。

数据在 `/data/data/com.garmincoach.app/files/`，卸载即清空；
密码用 Android Keystore 加密存在 SharedPreferences，Python 侧不落密码。

### 界面交互（0.1.3 起）

- **同步没有按钮**：页面滚到顶后继续下拉 →「↓ 下拉同步 / ↑ 松开立即同步」→ 松手执行
  `sync(30 天) + 重出看板 + 自动刷新`；指示器依次显示「正在同步… / √ 已更新」并回弹。
  手势由手写的 `PullRefreshLayout.kt` 处理（约 150 行，**不引 AndroidX**）：
  只在 `WebView.scrollY == 0` 且向下拖时才接管，页面中间的正常滚动完全不受影响；
  带 0.5 阻尼、36dp 触发阈值、66dp 位移上限、回弹动画。首次未登录时下拉会先
  收起指示器再弹登录框，登录成功自动接着同步。
- **没有悬浮按钮**。顶部是一条独立占位的 52dp 工具栏（标题 + 状态 + ⋮），
  WebView 只占它下面那块，任何内容都不会被盖住。
- ⋮ 菜单只剩三项：今日一图流 / 跑步活动图 / 重新登录。刷新页面也不单列——
  下拉刷新本身就是「同步 + 刷新」，再放一个按钮反而让人分不清哪个联网。
- **看板里的活动表可以直接点**：`report.py` 给每条活动行写了 `data-aid`，
  App 注入一小段 JS 把点击转给 `GC.activity(id)`，随即出该活动的图。
  桌面版看板里这个属性不绑事件，完全无副作用。
- 两种出图都走同一条链路：`appapi` 生成 HTML → 离屏 WebView 画成 PNG →
  存到 `相册/GarminCoach/` → 弹系统分享（可直接发微信）。
- 活动图是 **1080×2250**（一图流是 1080×1920），没有高德底图时用纯 SVG
  矢量轨迹回落，所以断网也能出图。首次出某条活动需要联网抓一次明细，
  之后明细落库就一直离线可用。
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

离屏 WebView 必须开 `useWideViewPort` + `loadWithOverviewMode`：HTML 里写死
`width=1080`，而 View 的 1080px 在 2x/3x 屏上等于 540/360dp，不开 overview
会只截到左上角。开完不管 density 多少，整页都按宽度铺满，输出仍是 1080×H 像素。
比例万一不对，`adb logcat -s GarminCoach` 里有 `render viewport=...` 可对照。

**密码为什么能存**：佳明的 JWT 只有 2 小时，想"点一下就同步"就必须能自动续期，
也就必须留密码。这里用 Android Keystore 里的 AES-GCM，密钥不出 TEE，
拷走 xml 也解不开，换机自动失效。**Python 侧不落密码**（`appapi.login()` 只留会话）。

## 已知限制（按优先级）

1. **出图比例未在真机复核**：0.1.1 起 Python 启动、登录、同步、看板已在真机跑通；
   PNG 出图的密度自适应是按 WebView 视口规则推的，还没看到实机产物。
   若图片发虚或只有左上角，把 `adb logcat -s GarminCoach` 的
   `render viewport=` 一行发出来即可定位。
2. **后台同步不可靠**：现在同步跑在普通线程里，切后台可能被系统掐。
   要稳得上前台服务 + 常驻通知，或 WorkManager 定时。
3. **没有每日自动同步**：`WorkManager` 还没接，得手动下拉一次。
4. **轨迹回放、简报没有原生入口**：本机服务首页里能点开，只是没做按钮。
   活动图已有入口（看板点行 + ⋮ 菜单）。
5. **首次启动慢 1~2 秒**：Chaquopy 要解压 Python 运行时，只在第一次。
6. **分享不脱敏**：一图流不含轨迹，但活动图会，分享前要自己看一眼。

## 和 Termux 版的关系

两套并行、不冲突：Termux 版给愿意折腾的人（零构建、100% 代码复用），
APK 给不想碰命令行的人。Python 代码是同一份，`appapi.py` 是唯一的接口面。

改了 `garmin_cn/` 之后记得 `sync_python.bat`，否则 APK 里还是旧代码
（这是唯一容易踩的坑，`.gitignore` 里已经把那份复制品排除掉了）。
