// 顶层构建脚本：只声明插件版本，具体配置在 app/build.gradle.kts。
//
// 版本说明：
//   AGP 8.5 需要 JDK 17（这台机器现在是 JDK 8，必须先装）；
//   Chaquopy 17.2 支持 Python 3.10~3.15，本机已有的 3.13 直接可用，
//   但构建机的 Python 必须与 App 目标版本一致（见 app/build.gradle.kts 的 version）。
plugins {
    id("com.android.application") version "8.5.2" apply false
    id("org.jetbrains.kotlin.android") version "2.0.20" apply false
    id("com.chaquo.python") version "17.2.0" apply false
}
