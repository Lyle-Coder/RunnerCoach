plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "com.garmincoach.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.garmincoach.app"
        // 29 起 MediaStore 才有 RELATIVE_PATH，分享一图流不用再写一堆分支
        minSdk = 29
        targetSdk = 34
        versionCode = 9
        versionName = "0.1.8"

        // 只留 arm64：Python 3.12+ 就不再支持 32 位 ABI，
        // 而且多带一个 ABI 就多 8MB。
        ndk { abiFilters += listOf("arm64-v8a") }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            // 朋友之间互传，用 debug 签名就够；要正经分发再换自己的 keystore
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions { jvmTarget = "17" }
}

// Chaquopy 的配置走顶层 chaquopy 块——Kotlin DSL 下没有 android.defaultConfig.python，
// 那是旧版 Groovy DSL 的写法。
// 源码目录不用声明：默认就是 src/main/python，sync_python 正好往那儿复制。
chaquopy {
    defaultConfig {
        version = "3.13"
        // 构建机上的解释器，必须与上面同小版本。
        // Windows 上默认会依次试 py -3.13 / py -3 / python；
        // 用 -PbuildPython=... 可以显式指定。
        buildPython(project.findProperty("buildPython")?.toString() ?: "python3")
        // 项目零第三方依赖，不需要 pip 块
    }
}

dependencies {
    // 刻意不引 androidx：界面用系统 Activity + WebView 拼，
    // 少一个依赖就少一处版本冲突。
}
