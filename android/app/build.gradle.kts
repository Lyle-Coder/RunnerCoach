// Gradle Kotlin DSL 里 `java` 是 JavaPluginExtension，会遮住 java.* 包名，
// 所以 Properties 必须在文件顶部 import，不能写全限定名。
import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// ---------------------------------------------------------------- 正式签名
// 口令、别名、keystore 路径一律放在 local.properties（已在 .gitignore 里），
// 仓库只保留这段读取逻辑。没配就退回 debug 签名，本地照样能构建、
// 但 release.py check 在打 1.0 之后会直接拦下来。
//
// local.properties 里要写四行（路径可以是绝对路径，也可以相对 android/）：
//   rc.storeFile=../keys/runnercoach.jks
//   rc.storePassword=...
//   rc.keyAlias=runnercoach
//   rc.keyPassword=...
// 也可以用 -Prc.storePassword=... 临时覆盖，适合 CI，不落盘。
val rcLocal = Properties().apply {
    val f = rootProject.file("local.properties")
    if (f.isFile) f.inputStream().use { load(it) }
}

fun rcProp(key: String): String? =
    (findProperty(key) as String?)?.takeIf { it.isNotBlank() }
        ?: rcLocal.getProperty(key)?.takeIf { it.isNotBlank() }

val rcStoreFile = rcProp("rc.storeFile")?.let { rootProject.file(it) }
val rcReady = rcStoreFile != null && rcStoreFile.isFile &&
    rcProp("rc.storePassword") != null && rcProp("rc.keyAlias") != null

android {
    namespace = "mm.runnercoach.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "mm.runnercoach.app"
        // 29 起 MediaStore 才有 RELATIVE_PATH，分享一图流不用再写一堆分支
        minSdk = 29
        targetSdk = 34
        versionCode = 128
        versionName = "1.1.9"

        // 只留 arm64：Python 3.12+ 就不再支持 32 位 ABI，
        // 而且多带一个 ABI 就多 8MB。
        ndk { abiFilters += listOf("arm64-v8a") }
    }

    signingConfigs {
        // 只有四要素齐全才建 release 配置；否则这个块保持为空，
        // buildTypes.release 会退回 debug（见下面的 if）。
        if (rcReady) {
            create("release") {
                storeFile = rcStoreFile
                storePassword = rcProp("rc.storePassword")
                keyAlias = rcProp("rc.keyAlias")
                keyPassword = rcProp("rc.keyPassword") ?: rcProp("rc.storePassword")
            }
        }
    }

    buildTypes {
        release {
            // R8：dex 从 2.2MB 压到约 1.1MB，启动时类加载也少一截。
            // 规则见 proguard-rules.pro —— 网页桥是反射调用的，必须显式保留。
            isMinifyEnabled = true
            proguardFiles(
                getDefaultProguardFile("proguard-android.txt"),
                "proguard-rules.pro",
            )
            // 配了正式 keystore 就用它；没配退回 debug，只在本地调试用。
            // 正式发版由 release.py check 把关：版本 >= 1.0 且仍是 debug 直接报错。
            signingConfig = if (rcReady) {
                signingConfigs.getByName("release")
            } else {
                signingConfigs.getByName("debug")
            }
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
