# GitHub Actions 自动封装 iOS 安装包：调研结论

> 起因：听说「GitHub 上有项目的 Action 能自动封装苹果安装包」。
> 本文核实这些 Action 是什么、能封到什么程度、对本项目能不能替代 Mac。
>
> 调研时间：2026-11-10。相关前提见《iOS迁移评估.md》。

---

## 一、结论

**传闻是真的，而且不是偏门技巧——这是 iOS 团队的标准做法。** 但要看清楚它替代的是哪一段：

| 环节 | CI 能否替代 Mac | 说明 |
|---|---|---|
| 编译出 `.ipa` | **能，且免费** | 公开仓库的 macOS runner 分钟数不计费 |
| 签名（正式证书） | **能**，但要 $99 开发者账号 | App Store Connect API Key + `-allowProvisioningUpdates`，云端按需建证书，不用手工导 p12 |
| 签名（免费 Apple ID） | **不能** | 免费账号没有 App Store Connect API 权限，必须借助电脑上的 Sideloadly/AltServer |
| 上传 TestFlight | **能** | `xcrun altool --upload-app --apiKey …` |
| **开发调试** | **不能** | 没有断点、没有真机交互、一次迭代 5–15 分钟 |

一句话：**CI 能把"每次都要一台 Mac"降成"偶尔要一台 Mac"，但拿不掉 Mac。**
真正不能省的场景是——你写 SwiftUI 外壳、调 WKWebView 桥、调出图缩放、调视频编码时，
必须看着屏幕改。这几件事恰好是本项目 iOS 迁移里最贵的三块（见《iOS迁移评估.md》§4）。

---

## 二、三条路径，看你要装到哪台手机

### 路径 A：无签名 IPA（零成本，但有安装门槛）

Flutter 社区的标准做法，也是最省的：

```bash
xcodebuild -project App.xcodeproj -scheme App -configuration Release \
  -sdk iphoneos -destination generic/platform=iOS \
  CODE_SIGN_IDENTITY="" CODE_SIGNING_REQUIRED=NO CODE_SIGNING_ALLOWED=NO

mkdir Payload && cp -R build/Release-iphoneos/App.app Payload/
zip -r App-unsigned.ipa Payload/
```

产物 `App-unsigned.ipa` 本身**装不进普通 iPhone**，只能：

- 用 **TrollStore** 直接装（靠 CoreTrust 漏洞永久签名）。限制很硬：官方支持 **iOS 14.0–16.6.1、16.7 RC、17.0**；
  苹果在 16.7 / 17.0.1 修掉了，17.1 以上（含 iOS 26）不行。**先确认你手机的 iOS 版本再谈这条路。**
- 或者交给 **Sideloadly / AltStore / SideStore** 用你自己的 Apple ID 重签后再装（见路径 B 的免费分支）。

### 路径 B：真签名（$99 账号，全自动到手机）

现代做法已经不导 p12 了。用 **App Store Connect API Key** 让 xcodebuild 自己去开发者后台建证书和描述文件：

```bash
xcodebuild archive -project App.xcodeproj -scheme App \
  -archivePath build/App.xcarchive -destination generic/platform=iOS \
  -allowProvisioningUpdates \
  -authenticationKeyPath AuthKey_XXX.p8 \
  -authenticationKeyID   $ASC_KEY_ID \
  -authenticationKeyIssuerID $ASC_ISSUER_ID
```

三个必须知道的细节（社区里踩出来的）：

1. **API Key 的角色必须是 Admin**，不能是 App Manager。云端签名要"创建分发证书"，只有 Admin 有权限；
   用 App Manager 归档能过、导出时报 `No signing certificate "iOS Distribution" found`。
2. `.p8` **只能下载一次**，当成密码对待，只放 GitHub Secrets。
3. 上传 TestFlight：`xcrun altool --upload-app --type ios -f App.ipa --apiKey … --apiIssuer …`。
   **内部测试（最多 100 人）不需要 Beta 审核**，自己用自己的 Apple ID 装，构建 90 天有效——
   这是"自己的 App 装自己手机"最省事的一条路，比每周重签舒服得多。

### 路径 C：无电脑自助签名服务（给朋友用）

**SignTools**（+ `SignTools-CI` 作为 builder）：手机浏览器传 IPA → GitHub Actions 在后台签名 → 直接 OTA 安装。
适合"我不在电脑前，但朋友要装"的场景。自建、开源、可按证书配多个 profile。

同类 `ios-signer-service`（buitim）**最后提交在 2021 年**，别用，风险大于收益。

---

## 三、现成的 Action 清单

| Action | 作用 | 备注 |
|---|---|---|
| `yukiarrr/ios-build-action@v1.12.0` | **一站式**：p12 + mobileprovision → archive → 导出 ipa，还能直传 TestFlight / DeployGate | 最省事，支持 `increment-build-number: testflight` 自动对齐构建号 |
| `apple-actions/import-codesign-certs` | 把 p12 导进临时 keychain | 官方风格，配 `set-key-partition-list` 否则 codesign 会弹窗卡住 |
| `apple-actions/download-provisioning-profiles` | 装描述文件到 `~/Library/MobileDevice/Provisioning Profiles` | 手工签名路线必备 |
| `apple-actions/upload-testflight-build` | 上传 TestFlight | 内部测试够用 |
| `maierj/fastlane-action` | 跑 fastlane `gym` + `pilot` | 想要完整 fastlane 生态时用 |
| `softprops/action-gh-release` | 把 ipa 挂到 Release | **别用 Artifacts 存 ipa**（免费额度只有 500 MB 存储） |

**强烈建议配 `XcodeGen`**：用 `project.yml` 生成 `.xcodeproj`，CI 里现生成。
否则 `.pbxproj` 是二进制味的 XML，多分支合并必然冲突——这个项目已经习惯"main 只接受合并"，
`.pbxproj` 冲突会很难受。

---

## 四、成本：公开仓库是真的免费

2026 年 1 月 GitHub 调过一次价，但**公开仓库的 standard runner 仍然完全免费、不计分钟**，
macOS 也一样（只有付费的 larger runner 按分钟算）。对本项目很关键——GitHub 上那个
`Lyle-Coder/RunnerCoach` 是公开镜像，直接白用。

私有仓库就不一样了：

| 项 | 数值 |
|---|---|
| 免费额度 | 2,000 分钟/月（Linux 等价） |
| macOS 折算 | **10 倍** → 实际约 **200 分钟/月** ≈ 10 次 20 分钟的构建 |
| 超额单价 | $0.062/分钟（2026-01 后价格） |
| 单任务上限 | 6 小时 |
| 并发 | Free 计划 5 个 macOS 任务 |

**公开仓库的两个注意点**：① 构建日志是公开的，任何 `echo` 出来的密钥都会泄露；
② Actions secrets 本身不公开，但 fork 发起的 PR 拿不到 secrets，所以 workflow 只挂
`push: branches: [main]` + `workflow_dispatch`，别挂 `pull_request`。

---

## 五、落地到本项目：一条 workflow 的骨架

前提：iOS 壳已经存在（《iOS迁移评估.md》§6 的 spike 过完）。在那之前，CI 无事可做。

```yaml
name: ios
on:
  workflow_dispatch:
  push:
    tags: ['ios/v*']

jobs:
  build:
    runs-on: macos-15
    steps:
      - uses: actions/checkout@v4

      - name: 拉 python-apple-support（3.13 xcframework）
        run: |
          curl -L -o py.tar.gz "$PY_APPLE_SUPPORT_URL"
          tar xzf py.tar.gz
      - name: 生成 Xcode 工程
        run: brew install xcodegen && xcodegen generate
      - name: 同步 Python 内核（复用安卓那套脚本思路）
        run: ./ios/sync_python.sh
      - name: 归档 + 导出
        run: |
          xcodebuild archive -project ios/RunnerCoach.xcodeproj -scheme RunnerCoach \
            -archivePath build/App.xcarchive -destination generic/platform=iOS \
            CODE_SIGNING_ALLOWED=NO
          cd build/App.xcarchive/Products/Applications && mkdir Payload && \
            mv RunnerCoach.app Payload/ && zip -r ../../../RunnerCoach-unsigned.ipa Payload/
      - uses: softprops/action-gh-release@v2
        with:
          files: build/RunnerCoach-unsigned.ipa
```

有 $99 账号就把 `CODE_SIGNING_ALLOWED=NO` 换成 §2 路径 B 的三行 `-authenticationKey*`，
末尾加 `xcrun altool --upload-app`。

配套要写的东西（都在 Windows 上能写完，不需要 Mac）：

1. `ios/project.yml`（XcodeGen 配置，纯文本，可 review、可合并）
2. `ios/sync_python.sh`（对齐 `android/sync_python.sh`，把 `garmin_cn/` 拷进 bundle 资源）
3. `ios/ExportOptions.plist`（`method: app-store-connect` / `ad-hoc`）
4. `.github/workflows/ios.yml`
5. 一个 `tools/ios_build.py`，和现有 `tools/release.py` 一个风格（零依赖、不上传密钥）

---

## 六、能替代到什么程度（说实话）

| 场景 | CI 够用吗 |
|---|---|
| 改完代码想确认"能不能编译" | **完全够**，这就是 CI 的正业 |
| 改完 UI 想看看长什么样 | **勉强**：可以在 runner 上起模拟器、`xcrun simctl io booted screenshot` 出图当 Artifact。但一次 5–15 分钟，且看不到动画、摸不到手势 |
| 桥接/出图缩放/视频编码这类"只能真机验"的活 | **不够**。安卓那 3 次"放大一角"是靠真机 `logcat` 才对完的账，iOS 同样要真机 |
| 想装到自己手机上 | **够**：unsigned + Sideloadly（每周重签），或 $99 + TestFlight 内部测试（90 天） |
| 想给朋友装 | **够**：TestFlight 最省心；免费签名这条路等于让对方每周折腾 |

---

## 七、建议

1. **先确认你那台 iPhone 的 iOS 版本。** ≥17.1 的话 TrollStore 直接出局，
   免费路线只剩"电脑重签"，那 $99 的性价比会高很多。
2. **CI 的定位要摆正**：它是"自动出包 + 编译回归"，不是"开发环境"。
   想认真做 iOS 版，二手 M1 mini（¥2.6–3.8k）仍然值得买，它省下的是几十小时的瞎猜。
3. **顺序**：iOS 壳做出来 → 接 CI → 用 unsigned IPA 打通"装到自己手机" → 再决定要不要 $99。
   不要反过来先折腾签名，那是给一个还不存在的 App 上锁。
4. **不要碰**：企业证书分发（会被吊销）、`ios-signer-service`（五年没维护）、
   把 `.p8` / p12 提交进仓库。公开仓库上尤其致命。

---

## 八、参考

- PEP 730（CPython iOS 支持）、`beeware/Python-Apple-support`（预编译 xcframework）
- `yukiarrr/ios-build-action` v1.12.0、`apple-actions/*`、`SignTools` + `SignTools-CI`
- GitHub Actions 计费：公开仓库不限分钟；私有仓库 macOS 10 倍折算（2026-01 调价后）
- TestFlight 内部测试免 Beta 审核；构建 90 天过期
- TrollStore 支持到 iOS 18.5（按项目 README 口径），17.0.1+ 需按机型/版本单独核实
