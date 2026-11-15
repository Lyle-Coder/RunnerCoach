package mm.runnercoach.app

import android.app.Application
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

/**
 * 让 Python 在**任何进程/组件**里可用，而不只是主界面。
 *
 * 以前 `Python.start()` 写在 MainActivity 里，桌面小组件被系统单独拉起时
 * （App 主界面没开过，进程是新的）拿不到 Python，取不到任何数据。
 * 放到 Application 里，widget / service 都能直接用；MainActivity 那边
 * 的 `if (!Python.isStarted())` 保留着，两边幂等，不会重复启动。
 */
class RCApp : Application() {
    override fun onCreate() {
        super.onCreate()
        try {
            if (!Python.isStarted()) Python.start(AndroidPlatform(this))
        } catch (t: Throwable) {
            // 启动失败不能让整个 App 起不来：主界面自己还会再试一次
            android.util.Log.w("RCApp", "python start failed", t)
        }
    }
}
