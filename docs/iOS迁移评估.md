# 迁移到苹果平台：可行性与工作量评估

> 承接《APP方案.md》（那里结论是「暂不考虑 iPhone」）与《交付形态调研.md》第九节。
> 本文回答一个问题：**如果现在要做 iOS 版，要花多少、卡在哪、值不值。**
>
> 评估基准：`main` @ cc097ce（1.0.1 / versionCode 88），2026-11-10。

---

## 一、结论先行

**技术上可行，且比想象中轻——但"可行"和"该做"是两件事。**

| 判断项 | 结论 |
|---|---|
| Python 内核能否上 iOS | **能**。CPython 3.13 起 iOS 是官方 Tier 3 平台（PEP 730），本项目零第三方依赖，标准库全部可用 |
| 现有代码复用率 | **内核 ~95%**（与安卓同一份 `garmin_cn/`），**壳层 0%**（6,188 行 Kotlin 要重写成 Swift） |
| 有没有硬阻塞 | **没有**。移动端链路不依赖 `fork/exec`，也不依赖任何外部二进制 |
| 最贵的单点 | **视频导出**（MediaCodec/EGL 那套全部作废，要按 AVFoundation 重写） |
| 真正的卡点 | **不是技术**：① 手上没有 Mac，编译不了；② 分发/合规；③ iOS 后台比安卓更死 |
| 结论 | 自用可做，**34～50 人日**；要上中国区 App Store 另加 5～10 人日 + 2～4 个月日历 |

一句话：**这是一次"壳层重写"，不是"逻辑重写"。** 安卓这一层壳已经把该踩的坑踩完了，
iOS 版是把同一套结论用另一套 API 再实现一遍——难在细节，不难在架构。

---

## 二、现状盘点（评估的事实基础）

| 项 | 现状 |
|---|---|
| Python 内核 | `garmin_cn/` 46 个模块、**30,248 行**；其中 `selftest.py` 3,015 行 |
| 第三方依赖 | **0**（`playwright` 只出现在桌面 `browser_login.py` 的可选路径，手机端从不进入） |
| 安卓壳 | Kotlin **6,188 行 / 11 文件**，`MainActivity.kt` 独占 3,677 行 |
| Python 运行时 | Chaquopy 内嵌 CPython **3.13**，只打 `arm64-v8a`，APK **≈23 MB** |
| 界面 | 本机 `127.0.0.1` 只读 HTTP 服务 + WebView，产物本身就是 HTML |
| 出图 | 离屏 `WebView.draw(Canvas)` → PNG；带 `visualViewport.width` 自校正（0.1.5 的「放大一角」修复） |
| 视频 | `MediaCodec` + `MediaMuxer` + EGL/GLES2 Surface，CPU 路径兜底 |
| 密码 | Android Keystore AES-GCM；Python 侧不落密码 |
| 分享 | MediaStore 写 `相册/GarminCoach/` + `ACTION_SEND` |
| 后台 | 无 WorkManager，同步跑普通线程（已知不可靠） |

标准库用到的全部是：`sqlite3 / ssl / http.server / socket / threading / urllib / zlib /
hashlib / struct / json / csv`——**这些在 iOS 版 CPython 里都有**，这是可行性的地基。

---

## 三、分层映射：哪层能搬，哪层要重写

| 层 | 安卓实现 | iOS 等价物 | 复用率 | 难度 |
|---|---|---|---|---|
| 业务逻辑 | `garmin_cn/` 30k 行 | **同一份源码** | ~95% | 低 |
| 解释器 | Chaquopy 动态装 CPython | CPython 3.13 静态 framework（`python-apple-support` / Briefcase 预编译 xcframework），C 扩展**全部静态链接进主二进制** | 换装载方式 | 中 |
| 本机服务 | `http.server` 线程 | 同代码；`127.0.0.1` 不受 ATS 限制 | 100% | 低 |
| 页面 | `WebView` | `WKWebView` | HTML/CSS 100% | 低 |
| JS 桥 | `@JavascriptInterface` | `WKScriptMessageHandler` + `evaluateJavaScript` 回调 | 重写 ~150 行 | 低 |
| **出图** | `WebView.draw(Canvas)` | `WKWebView.takeSnapshot(with:)`，先撑满内容高度再截 | 重写 ~200 行 | **中高** |
| 下拉刷新 | 手写 `PullRefreshLayout` 174 行 | `scrollView.refreshControl` 自带 | 直接删 | 低 |
| 密码 | Android Keystore | Keychain（`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`）+ CryptoKit AES-GCM | 重写 ~120 行 | 低 |
| 写相册 | MediaStore | `PHPhotoLibrary`（仅新增权限） | 重写 ~60 行 | 低 |
| 分享 | `ACTION_SEND` | `UIActivityViewController` | 重写 ~40 行 | 低 |
| 登录 WebView | `CorosLoginActivity` / `HuaweiLoginActivity` | `WKWebView` + `WKHTTPCookieStore` 读 cookie 回灌 Python cookie jar | 重写 ~200 行 | 中 |
| **视频编码** | `MotionEncoder` 416 + `MotionGL` 257 + `MotionAudio` 286 + `MotionClips` 129 | `AVAssetWriter`（H.264）+ `CVPixelBuffer` 直出；**GLES 那层可以整体删掉** | 全重写 ~500–700 行 | **高** |
| 视频裁剪 UI | `ClipTrimDialog` 794 行 | `AVPlayer` + 自绘时间轴 | 全重写 ~500 行 | 中高 |
| 后台同步 | 普通线程（已知不可靠） | `BGAppRefreshTask`（系统决定何时跑，可能几天不跑） | 等价或更差 | 中 |
| 系统打开 / 分享文件 | `sysopen.py`（`subprocess`/`startfile`） | `UIApplication.open` | 一行 | 低 |

**被 iOS 直接判死的东西**（PEP 730 明确）：

1. **没有 `fork/exec`**——调用则进程被杀。桌面链路的 `subprocess` 共 8 处
   （`card.py` / `actcard.py` / `racecard.py` 调 Chrome 无头截图、`video.py` / `clip.py` 调 ffmpeg、
   `motion.py` 调外部命令），全靠 `ffmpeg()/can_shot()` 这类能力探测降级。
   **手机端从来没走过这些分支**，所以只需复核探测函数在 iOS 上返回 False，不是重写。
2. **设备上不能 `pip install`、不能下载代码执行**——所有 C 扩展必须编译期静态链进主二进制。
   本项目零第三方依赖，这条几乎不产生成本（这也是它比绝大多数 Python 项目好运的地方）。
3. **没有 stdin/stdout**——`print` 要重定向到系统日志，诊断面板得改成写文件再读。
4. **App 挂起即服务死**——"手机当服务器给别的设备连"这条故事在 iOS 上不成立。

---

## 四、工作量估算

按单人、含调试、不含返工的口径。参考锚点：安卓这层壳从零到真机跑通实质花了约 2 个月。

| 阶段 | 内容 | 人日 | 风险 |
|---|---|---|---|
| **P0 Spike** | 5 个验证实验（见 §6），把 80% 的不确定性打掉 | **3–5** | — |
| **P1 内核上机** | 静态 framework 集成、构建脚本、CA 证书捆绑、`GARMIN_HOME` 映射到 `Library/Application Support`、能力探测复核、在真机跑 `selftest` 子集 | **6–9** | 中 |
| **P2 壳层** | WKWebView + 消息桥 + 三段式布局 + 底部 tab + 登录/验证码/MFA + Keychain + 相册 + 分享 + 对话框 | **12–16** | 中 |
| **P3 出图** | 离屏 WKWebView 撑高 + `takeSnapshot` + 9:16 contain + 安全边；CSS px / pt / snapshotWidth 三方换算 | **4–6** | **中高** |
| **P4 视频** | AVAssetWriter 编码、音频混流、裁剪 UI、进度与取消 | **8–12** | **高** |
| **P5 打磨** | 签名、TestFlight、首启耗时、内存（jetsam）、锁屏/来电中断、真机对账 | **5–8** | 中 |
| **合计** | 功能对齐安卓 1.0.1 | **38–56 人日** | |

换算：全职 5 天/周 → **8～11 周**；业余 2 天/周 → **5～7 个月**。

若还要上中国区 App Store，追加：隐私清单 `PrivacyInfo.xcprivacy`、审核往返
（每轮 1～2 周）、被拒重做 —— **+5～10 人日，日历 +2～4 个月**。

**费用**（非人力）：

| 项 | 金额 | 说明 |
|---|---|---|
| Mac | 二手 M1 mini ¥2,600～3,800 或云 Mac $25～40/月 | **Windows 编不了 iOS，这是硬门槛** |
| Apple Developer | $99/年 | 上 TestFlight / 上架必需 |
| 免费签名 sideload | ¥0 | 但**证书 7 天过期**，得每周重签 |
| 软著 | 免费 | 已有 `tools/copyright.py` 与《软著申请.md》，30～60 工作日 |
| App 备案 | 免费 | 个人主体口径各省不一，见《软著申请.md》§9 |

GitHub Actions 的 macOS runner 可替代本机 Mac（本仓库已有双远端与 `tools/release.py`），
但私有库免费额度 2,000 分钟/月按 **10 倍折算**，实际只有约 **200 macOS 分钟**——
够出包，不够边写边调。

---

## 五、合规与分发：比技术更硬的一堵墙

| 路径 | 门槛 | 判断 |
|---|---|---|
| 免费签名 sideload（AltStore/SideStore） | 自己一台 iPhone + 一个 Apple ID；**7 天重签**，最多 3 个 App | 自用最省事，但"给朋友用"等于让对方每周折腾一次 |
| TestFlight | $99/年，需过 Beta 审核，构建 90 天过期，上限 1 万人 | **朋友内测的最优解** |
| 中国区 App Store | $99/年 + **软著** + **App 备案**（个人主体各地口径不一，多数要企业/个体户）+ 隐私政策 | 与《软著申请.md》§9 的结论一致：个人主体这里最难过 |
| 海外区 App Store | $99/年，不要备案 | 但受众不是目标用户 |

额外的审核风险，需要在投入前想清楚：

1. **5.2.1（第三方账号与数据）**：App 用用户自己的佳明/高驰/华为账号密码登录取数，
   没有官方 API。这类"个人数据看板"通常能过，但被要求提供授权证明的先例不少。
2. **4.1 / 4.3（与官方 App 重复、模板化）**：容易被判"和 Garmin Connect 功能重复"。
3. **健康类目**：表述必须是「运动数据展示与分析」，不能出现训练/医疗建议口吻——
   而现在 `coach.py` 的处方文案正是这个方向，**上架前要改口径**，这是产品层面的真成本。
4. **隐私**：现在"不收集、无账号、无遥测"是最好过的形态；一旦 2.0 加在线服务，
   就要同时触发 ICP 与 App 两条备案线。

---

## 六、先做这 5 个实验（3～5 人日，决定要不要继续）

不要先写壳。先花几天把不确定性打掉，代价最小：

| # | 实验 | 通过标准 | 打掉的风险 |
|---|---|---|---|
| 1 | `python-apple-support` 3.13 xcframework 链进空 App，按钮调用 `appapi.env_info()` | `sqlite3 / ssl / http.server / ssl.OPENSSL_VERSION` 全部就绪 | 运行时可行性（最致命的一条） |
| 2 | 起 `127.0.0.1` 服务 + WKWebView 打开 `/` + 消息桥往返一次 | 页面出得来、`Bridge.action()` 能回 JSON | 服务 + 桥 + ATS |
| 3 | 离屏 WKWebView 撑到内容全高，`takeSnapshot` 出 1080×1920 一图流 | 整卡入画、文字不糊、不出现"放大一角" | **最难的一处历史坑**（安卓在此翻车 3 次） |
| 4 | 真机跑 `selftest` 里不依赖浏览器的子集 | 通过率与桌面一致 | 内核平台耦合（顺手拿到回归网） |
| 5 | 30 秒 1080×1920 H.264 用 `AVAssetWriter` 合成 | 30 秒素材 <15 秒编完、iOS 相册能播 | 视频链路吞吐与可行 |

第 5 条不过，就把「iOS 版不带视频导出」写进范围——这是砍范围最划算的一刀。

---

## 七、三条路线，按性价比排序

| | **B 瘦客户端 + 家里中枢** | **A 内嵌 Python 全功能** | **C 原生重写** |
|---|---|---|---|
| iPhone 上能做什么 | 看板、历史、教练、出图（服务端出） | 全部，含离线同步与视频 | 全部 |
| 工作量 | **5～8 人日** | **38～56 人日** | 150+ 人日 |
| 前置条件 | 一台常开的机器（旧笔记本/NAS/树莓派） | 一台 Mac | 一台 Mac + 长期双份维护 |
| 数据在哪 | 家里 | 手机里 | 手机里 |
| 上架可行性 | 不需要上架（PWA/局域网） | 需要，且有 5.2.1/4.1 风险 | 同 A |
| 维护成本 | 低（服务端一份代码） | 中（内核一份 + 两套壳） | **高（逻辑两份，必然漂移）** |

**推荐顺序：**

1. **先补 iPhone 的"能看"**（2～4 人日）：`run.bat serve --lan` 已经有了，缺的只是
   PWA manifest、主屏图标、访问码/PIN、定时同步。这一步零风险、立刻覆盖 iPhone 用户，
   和《交付形态调研》里"先做 B"的判断一致。
2. **想要"iPhone 上自己全功能用"**，才走 A：先做 §6 的 5 个实验，全过再排 8～11 周。
3. **C 任何时候都不推荐**：除非要做成对外产品并长期维护两套逻辑。

**什么情况下别做 A：** 如果目标用户里 iPhone 占比不高、或者你并不真的需要在手机上
完成"同步"（只是想看图），那 B 已经够了——iOS 版最大的成本不是写代码，是"你得先有一台 Mac，
然后每 7 天给朋友的手机重签一次"。

---

## 八、范围建议（如果决定做 A）

按"先能用、再好用"的顺序砍：

| 优先级 | 范围 | 理由 |
|---|---|---|
| P0 | 登录 → 同步 → 看板 → 出图 → 分享 | 这是 90% 的使用场景 |
| P1 | 历史 / 教练 / 我的三个 tab、下拉刷新、补历史 | 与安卓对齐 |
| P2 | 视频导出、裁剪 | 成本最高、使用频率最低，**可以后置或不做** |
| 不做 | 后台常驻服务、给局域网其他设备提供访问 | iOS 上不成立 |

---

## 九、一句话总结

**代码不用重写，壳要重写一遍；技术不是障碍，Mac 和分发才是。**
安卓已经证明"Python 内核 + WebView 壳"这条路走得通，iOS 只是把壳换成 Swift
再实现一遍同样的结论——**38～56 人日**能拿到与安卓 1.0.1 对齐的版本，
但在此之前，先花 3～5 天做 §6 的 5 个实验，比任何估算都可靠。
