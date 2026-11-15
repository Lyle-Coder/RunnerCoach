package mm.runnercoach.app

import android.app.Application
import android.system.Os
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

/**
 * 让 Python 在**任何进程/组件**里可用，而不只是主界面。
 *
 * 以前 `Python.start()` 写在 MainActivity 里，桌面小组件被系统单独拉起时
 * （App 主界面没开过，进程是新的）拿不到 Python，取不到任何数据。
 *
 * ⚠️ 这里有一个顺序上的坑，MainActivity 里那段注释就是原话：
 * `GARMIN_HOME` 必须在 `Python.start()` **之前**设好，否则 Python 侧的数据
 * 目录会退回默认位置，读不到 filesDir 里的用户库 —— 表现就是「更新后
 * 用户信息全没了」。v1.3.13 第一版 RCApp 漏了这一句，就是从那一版开始丢的。
 */
class RCApp : Application() {
    override fun onCreate() {
        super.onCreate()
        try {
            Os.setenv("GARMIN_HOME", filesDir.absolutePath, true)
        } catch (t: Throwable) {
            // 设不上也别拦着 App 启动：MainActivity 里还会再设一次
            Log.w(TAG, "GARMIN_HOME 设置失败", t)
        }
        try {
            if (!Python.isStarted()) Python.start(AndroidPlatform(this))
        } catch (t: Throwable) {
            Log.w(TAG, "python start failed", t)
        }
    }

    private companion object {
        const val TAG = "RCApp"
    }
}
