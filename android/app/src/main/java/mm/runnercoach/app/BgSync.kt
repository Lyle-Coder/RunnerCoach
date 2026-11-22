package mm.runnercoach.app

import android.app.job.JobInfo
import android.app.job.JobScheduler
import android.content.ComponentName
import android.content.Context
import android.util.Log

/**
 * 后台补传的排期入口。
 *
 * 为什么是 JobScheduler 而不是 WorkManager
 * ----------------------------------------
 * 本项目的依赖块是故意空的（见 build.gradle.kts 顶部那段注释），
 * WorkManager 会拖进 androidx.work + lifecycle + core 一整套 aar，
 * APK 体积和 R8 规则都要重估。而这个需求的全部要求——周期执行、
 * 只在有网时跑、重启后还生效——JobScheduler 原生就有；
 * WorkManager 在 Android 上的底层本来也是 JobScheduler。
 *
 * 两种触发
 * --------
 * - 周期任务：每 30 分钟跑一次。系统会按 Doze 维护窗口合并唤醒，
 *   所以「30 分钟」是下限不是准点；但每轮真正打佳明网络还有一道 25 分钟的闸
 *   （bg.py 的 BG_SYNC_GAP），不会因为改短了就翻十倍流量。
 * - nudge：App 回到前台 30 秒后跑一次，接住「跑完就切走、没点组页」。
 *   延迟 30 秒是为了不和首屏渲染抢带宽；Python 侧还有 20 分钟节流，
 *   来回切前后台不会反复上报。
 *
 * 系统层面的限制（改代码解决不了，只能让用户知道）
 * ------------------------------------------------
 * - 用户「强行停止」App 后，所有 job 都被取消且不再重排，直到再次点开 App。
 * - 国产 ROM 的省电策略会把 App 冻住，job 要等用户把 App 加入白名单才按时跑。
 */
object BgSync {
    const val JOB_PERIODIC = 8801
    const val JOB_NUDGE = 8802

    // 30 分钟，原先是 6 小时。改短是为了「跑完不久就能收到一条」：
    // 佳明不给我们 Push，只能靠周期去问。JobScheduler 最小 15 分钟，
    // 30 分钟是耗电与及时性的折中；每轮只在「距上次拉佳明 25 分钟以上」
    // 时才真的打网络（见 bg.py 的 BG_SYNC_GAP），所以并不会翻十倍流量。
    private const val PERIOD_MS = 30 * 60 * 1000L
    // flex 必须小于 period（setPeriodic 会校验），给 10 分钟窗口让系统
    // 自己挑省电的时机跑——窗口越小越准点，也越耗电。
    private const val FLEX_MS = 10 * 60 * 1000L
    private const val NUDGE_DELAY_MS = 30 * 1000L
    private const val TAG = "BgSync"

    /**
     * 排周期任务。幂等：已经排过就不再动它。
     *
     * 每次启动都调一次，是为了兜「job 被系统或用户清掉」的情况——
     * getPendingJob 为 null 说明确实没了，重排一次。
     */
    fun schedule(ctx: Context) {
        val js = js(ctx) ?: return
        try {
            if (js.getPendingJob(JOB_PERIODIC) != null) return
            val info = JobInfo.Builder(JOB_PERIODIC, svc(ctx))
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .setPeriodic(PERIOD_MS, FLEX_MS)
                // 重启后还在：否则用户三天不重启也一样丢，重启一次就彻底没了
                .setPersisted(true)
                .build()
            js.schedule(info)
        } catch (t: Throwable) {
            Log.w(TAG, "周期任务排不上", t)
        }
    }

    /**
     * 回前台补一次。同一个 id 排新的会替换掉旧的，不会堆积。
     *
     * 不做额外节流：Python 侧记着「上次真正跑完的时间」，20 分钟内会
     * 直接 skip，代价只有一次读本地库，比在这儿维护第二份时间戳可靠。
     */
    fun nudge(ctx: Context) {
        val js = js(ctx) ?: return
        try {
            if (js.getPendingJob(JOB_NUDGE) != null) return
            val info = JobInfo.Builder(JOB_NUDGE, svc(ctx))
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .setMinimumLatency(NUDGE_DELAY_MS)
                // 有网就尽快跑，别等到用户切走之后
                .setOverrideDeadline(5 * 60 * 1000L)
                .build()
            js.schedule(info)
        } catch (t: Throwable) {
            Log.w(TAG, "前台补传排不上", t)
        }
    }

    /** 全部取消。留给将来做「后台补传」开关用。 */
    fun cancel(ctx: Context) {
        val js = js(ctx) ?: return
        try {
            js.cancel(JOB_PERIODIC)
            js.cancel(JOB_NUDGE)
        } catch (t: Throwable) {
            Log.w(TAG, "取消失败", t)
        }
    }

    private fun js(ctx: Context): JobScheduler? = try {
        ctx.getSystemService(Context.JOB_SCHEDULER_SERVICE) as? JobScheduler
    } catch (t: Throwable) {
        null
    }

    private fun svc(ctx: Context) = ComponentName(ctx, SyncJob::class.java)
}
