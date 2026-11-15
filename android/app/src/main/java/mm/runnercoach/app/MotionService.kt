package mm.runnercoach.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder

/**
 * 回放生成期间的前台服务。
 *
 * 生成 = 一个离屏 WebView 逐帧渲染 + MediaCodec 编码，全部跑在本进程的线程里。
 * 用户切到别的 App 或锁屏后，进程掉进 cached 状态会被系统冻结，渲染线程跟着
 * 停住 —— 表现就是「必须停在进度条那一页，切走就不动了」。
 *
 * 挂一个前台服务把进程钉在 foreground 优先级，切走也能继续跑；通知里同步真实
 * 进度，回来时界面上的进度条仍然对得上。
 *
 * 只做保活与进度展示，不承载 WebView：渲染依赖 Activity 的窗口，搬到 Service
 * 里反而拿不到合成器。任务本身的取消/收尾仍由 MainActivity 负责。
 */
class MotionService : Service() {
    companion object {
        private const val CH = "motion_export"
        private const val ID = 0x4D4F              // 'MO'
        @Volatile private var alive = false

        fun start(ctx: Context, title: String) {
            if (alive) return
            val i = Intent(ctx, MotionService::class.java).putExtra("title", title)
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    ctx.startForegroundService(i)
                } else {
                    ctx.startService(i)
                }
                alive = true
            } catch (t: Throwable) {
                // Android 12+ 在后台直接 startForegroundService 会抛异常；
                // 生成一定是前台触发的，这里只是兜底，失败就退化成普通后台跑。
                alive = false
            }
        }

        fun update(ctx: Context, pct: Int) {
            if (!alive) return
            try {
                ctx.getSystemService(NotificationManager::class.java)
                    .notify(ID, build(ctx, "正在生成回放 ${pct.coerceIn(0, 100)}%", pct))
            } catch (t: Throwable) {
                // 通知失败不该影响生成
            }
        }

        fun stop(ctx: Context) {
            alive = false
            try {
                ctx.stopService(Intent(ctx, MotionService::class.java))
            } catch (t: Throwable) {
            }
        }

        private fun build(ctx: Context, text: String, pct: Int): Notification {
            val b = Notification.Builder(ctx, CH)
                .setContentTitle("RunnerCoach")
                .setContentText(text)
                .setSmallIcon(android.R.drawable.stat_sys_download)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
            if (pct > 0) b.setProgress(100, pct.coerceIn(0, 100), false)
            // 点通知回到 App：生成期间用户可能已经切走，回来还能看进度
            val open = ctx.packageManager.getLaunchIntentForPackage(ctx.packageName)
            if (open != null) {
                open.addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP)
                b.setContentIntent(
                    android.app.PendingIntent.getActivity(
                        ctx, 0, open,
                        android.app.PendingIntent.FLAG_IMMUTABLE
                            or android.app.PendingIntent.FLAG_UPDATE_CURRENT
                    )
                )
            }
            return b.build()
        }
    }

    override fun onCreate() {
        super.onCreate()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            getSystemService(NotificationManager::class.java).createNotificationChannel(
                NotificationChannel(CH, "回放生成", NotificationManager.IMPORTANCE_LOW)
            )
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // 必须在 5 秒内 startForeground，否则系统会直接杀进程
        startForeground(ID, build(this, "正在生成回放…", 0))
        return START_NOT_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
