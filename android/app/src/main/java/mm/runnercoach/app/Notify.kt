package mm.runnercoach.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject

/**
 * 小组通知：有人评论你的动态、组里新建了赛事。
 *
 * 只由 [SyncJob] 在后台任务里调。**前台不发**——用户正盯着页面，
 * 这时候弹通知是噪音；页面自己会刷新。
 *
 * 为什么用系统 Notification 而不是 androidx 的 NotificationCompat
 * ------------------------------------------------------------
 * 依赖块是空的（见 build.gradle.kts），引 core 只为省几行兼容代码不值得。
 * 本项目 minSdk 29，`Notification.Builder(ctx, channelId)` 从 26 就有。
 *
 * 权限的坑
 * --------
 * Android 13 起 POST_NOTIFICATIONS 是运行时权限。没授权时 `notify()`
 * **不抛异常，只是静默丢弃**。所以「发了通知但看不到了」先查权限，
 * 别去查 Python 那边有没有产生 news。
 */
object Notify {
    const val CH = "group_news"
    private const val TAG = "Notify"

    /** 建渠道。幂等，重复建会被系统忽略。 */
    fun ensureChannel(ctx: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val nm = ctx.getSystemService(NotificationManager::class.java) ?: return
        if (nm.getNotificationChannel(CH) != null) return
        nm.createNotificationChannel(
            NotificationChannel(CH, "小组动态", NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = "有人评论你的动态、组里新建了赛事"
                setShowBadge(true)
            }
        )
    }

    /** 有权限才发。33 以下默认有。 */
    fun allowed(ctx: Context): Boolean =
        Build.VERSION.SDK_INT < 33 ||
            ctx.checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) ==
            PackageManager.PERMISSION_GRANTED

    /**
     * 把 bg.catch_up() 给的 news 数组按组汇总成通知。
     *
     * **一个组一条**，不按事件一条一条弹：11 个人的组一天能攒出七八条，
     * 那样通知栏会被这一个 App 占满，用户第一次就会关掉通知。
     */
    fun emit(ctx: Context, arr: JSONArray) {
        if (arr.length() == 0) return
        val byGid = LinkedHashMap<Int, MutableList<JSONObject>>()
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            val gid = o.optInt("gid")
            if (gid <= 0) continue
            byGid.getOrPut(gid) { mutableListOf() }.add(o)
        }
        for ((gid, items) in byGid) {
            val gname = items.first().optString("gname").ifBlank { "小组" }
            val text = if (items.size == 1) {
                one(items.first())
            } else {
                // 多条时先给总数，再补最新那条，点进去看全部
                items.size.toString() + " 条新消息 · " + one(items.first())
            }
            post(ctx, gid, gname, text)
        }
    }

    private fun one(o: JSONObject): String = when (o.optString("kind")) {
        "comment" -> {
            val who = o.optString("by")
            val t = o.optString("text")
            val pre = if (o.optBoolean("on_my_act")) "评论了你的动态"
            else "在你参与的动态下留言"
            if (t.isBlank()) who + " " + pre else who + " " + pre + "：" + t
        }
        "challenge" -> {
            val k = kindName(o.optString("ckind"))
            o.optString("by") + " 建了" + (if (k.isEmpty()) "" else k) +
                "赛事「" + o.optString("title") + "」"
        }
        // 队友今天跑了。没距离的活动（器械、室内）只说「完成了一次训练」，
        // 不写「0.0 km」——那看起来像数据坏了。
        "act" -> {
            val km = o.optDouble("km", 0.0)
            val who = o.optString("by")
            if (km > 0.05) {
                // 固定 Locale.US：某些系统区域设置会把小数点变成逗号
                who + " 今天跑了 " + String.format(java.util.Locale.US, "%.1f", km) + " km"
            } else {
                who + " 今天完成了一次训练"
            }
        }
        else -> "有新的小组动态"
    }

    private fun kindName(k: String): String = when (k) {
        "distance" -> "里程"
        "acts" -> "次数"
        "elevation" -> "爬升"
        "explore" -> "探索"
        "speed" -> "配速"
        else -> ""
    }

    private fun post(ctx: Context, gid: Int, title: String, text: String) {
        if (!allowed(ctx)) {
            Log.i(TAG, "没有通知权限，丢掉一条组 $gid 的通知")
            return
        }
        ensureChannel(ctx)
        // 点通知直接进这个组。extra 由 MainActivity 读，port 还没起来时会先存着。
        val it = Intent(ctx, MainActivity::class.java).apply {
            putExtra("gid", gid)
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val pi = PendingIntent.getActivity(
            ctx, gid, it,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val n = Notification.Builder(ctx, CH)
            .setSmallIcon(R.drawable.ic_tab_group)
            .setContentTitle(title)
            .setContentText(text)
            // 评论可能长，收起态截断、展开态给全文
            .setStyle(Notification.BigTextStyle().bigText(text))
            .setAutoCancel(true)
            .setContentIntent(pi)
            .build()
        try {
            // 用 gid 当通知 id：同一个组只保留最新一条，不会堆成一列
            ctx.getSystemService(NotificationManager::class.java)?.notify(gid, n)
        } catch (t: Throwable) {
            Log.w(TAG, "通知发不出去", t)
        }
    }
}
