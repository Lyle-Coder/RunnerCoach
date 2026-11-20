package mm.runnercoach.app

import android.app.job.JobParameters
import android.app.job.JobService
import android.util.Log
import com.chaquo.python.Python
import java.util.concurrent.atomic.AtomicBoolean

/**
 * 后台补传的实际执行体：在后台线程里调 Python 的 `bg.catch_up()`。
 *
 * 为什么不在主线程调
 * ------------------
 * 它要发几个 HTTP 请求（拉组列表、每组一次上报），几百毫秒到十几秒；
 * onStartJob 在主线程，直接跑会把界面卡住，超过 10 秒还会被系统判 ANR。
 *
 * 为什么返回 true
 * --------------
 * JobService 的约定：onStartJob 返回 true 表示「我还在干活，别回收这个 job，
 * 也别放掉唤醒锁」。所以必须等后台线程真正结束再 jobFinished，否则系统
 * 可能在网络请求中途把进程挂起。
 *
 * Python 运行时不用在这儿 start：RCApp.onCreate 已经在 Application 层面
 * 起过了，JobService 与它同进程，直接 getModule 就能用。
 */
class SyncJob : JobService() {

    override fun onStartJob(params: JobParameters): Boolean {
        // 上一轮还没跑完（周期任务与前台 nudge 撞上了）：本次直接放过。
        // 不加锁的话两个线程会同时改上报队列，白耗流量还可能撞写库锁。
        if (!RUNNING.compareAndSet(false, true)) return false

        Thread({
            var msg = ""
            try {
                val api = Python.getInstance().getModule("garmin_cn.appapi")
                // nudge 是用户刚回到前台，跳过 20 分钟节流；周期任务按规矩来
                val force = params.jobId == BgSync.JOB_NUDGE
                val arg = if (force) "[true]" else "[false]"
                msg = api.callAttr("call", "bg_catch_up", arg).toString()
            } catch (t: Throwable) {
                // 走到这里说明 Python 层没兜住（导入失败、运行时没起来）。
                // 记一条 logcat 就行：Python 侧的正常失败已经有 diag.log 了。
                msg = "调用失败：" + (t.message ?: t.javaClass.simpleName)
                Log.e(TAG, "补传调用异常", t)
            } finally {
                RUNNING.set(false)
                Log.i(TAG, "job=" + params.jobId + " " + msg)
                jobFinished(params, false)
            }
        }, "rc-bgsync").apply { isDaemon = true }.start()

        return true
    }

    override fun onStopJob(params: JobParameters): Boolean {
        // 系统要收回资源（省电、切网、用户清理）。正在发的请求掐不断，
        // 让它自己跑完就行——都是短请求。
        // nudge 返回 true 请求重排，免得这次前台补传就这么丢了；
        // 周期任务由系统按周期再来，不用重排。
        return params.jobId == BgSync.JOB_NUDGE
    }

    private companion object {
        const val TAG = "BgSync"

        /** 同一时刻只允许一轮补传。 */
        val RUNNING = AtomicBoolean(false)
    }
}
