package mm.runnercoach.app

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.os.Bundle
import android.util.Log
import android.widget.RemoteViews
import com.chaquo.python.Python
import org.json.JSONObject
import java.io.File
import java.util.concurrent.Executors

/**
 * 桌面小组件：把「辉光看板」画成透明底的图贴在桌面上。
 *
 * 取数走 Python（`appapi.widget_data`），绘制走原生 Canvas（见 WidgetRender），
 * 因为小组件没有 Activity 窗口，离屏 WebView 渲不出来。
 *
 * 三种规格由系统回调的 options 决定（2×4 / 3×4 / 4×4），改尺寸时重新画一张。
 * 刷新周期 30 分钟（`updatePeriodMillis`），这是系统允许的最快值。
 */
class TodayWidget : AppWidgetProvider() {
    companion object {
        private const val TAG = "TodayWidget"
        private const val PREFS = "gc_widget"

        /** 系统 onUpdate 只有 10 秒，取数 + 绘制放到后台，用 goAsync 续命。 */
        private val pool = Executors.newSingleThreadExecutor()

        fun refreshAll(ctx: Context) {
            val mgr = AppWidgetManager.getInstance(ctx)
            val ids = mgr.getAppWidgetIds(ComponentName(ctx, TodayWidget::class.java))
            if (ids.isEmpty()) return
            val i = Intent(ctx, TodayWidget::class.java).apply {
                action = AppWidgetManager.ACTION_APPWIDGET_UPDATE
                putExtra(AppWidgetManager.EXTRA_APPWIDGET_IDS, ids)
            }
            ctx.sendBroadcast(i)
        }
    }

    override fun onUpdate(ctx: Context, mgr: AppWidgetManager, ids: IntArray) {
        val pending = goAsync()
        pool.execute {
            try {
                ids.forEach { id -> update(ctx, mgr, id) }
            } catch (t: Throwable) {
                Log.w(TAG, "update", t)
            } finally {
                try {
                    pending.finish()
                } catch (t: Throwable) {
                }
            }
        }
    }

    override fun onAppWidgetOptionsChanged(
        ctx: Context, mgr: AppWidgetManager, id: Int, newOptions: Bundle,
    ) {
        // 用户拉伸了格子：尺寸变了就重画，内容密度跟着变
        val pending = goAsync()
        pool.execute {
            try {
                update(ctx, mgr, id, newOptions)
            } catch (t: Throwable) {
                Log.w(TAG, "resize", t)
            } finally {
                try {
                    pending.finish()
                } catch (t: Throwable) {
                }
            }
        }
    }

    private fun update(
        ctx: Context, mgr: AppWidgetManager, id: Int,
        opts: Bundle? = null,
    ) {
        val o = opts ?: mgr.getAppWidgetOptions(id)
        val minW = o.getInt(AppWidgetManager.OPTION_APPWIDGET_MIN_WIDTH, 140)
        val minH = o.getInt(AppWidgetManager.OPTION_APPWIDGET_MIN_HEIGHT, 280)
        val size = WidgetRender.pick(minW, minH)

        val views = RemoteViews(ctx.packageName, R.layout.widget_card)
        val bmp = try {
            WidgetRender.render(loadData(ctx), size)
        } catch (t: Throwable) {
            Log.w(TAG, "render", t)
            // 画不出来也不能留白：退回上一次的图，没有就提示打开 App
            loadCached(ctx, size) ?: fallback(ctx, size)
        }
        views.setImageViewBitmap(R.id.widget_img, bmp)

        // 点一下打开 App
        val open = ctx.packageManager.getLaunchIntentForPackage(ctx.packageName)
        if (open != null) {
            open.addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP)
            views.setOnClickPendingIntent(
                R.id.widget_img,
                PendingIntent.getActivity(
                    ctx, id, open,
                    PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
                ),
            )
        }
        try {
            mgr.updateAppWidget(id, views)
        } catch (t: Throwable) {
            Log.w(TAG, "updateAppWidget", t)
        }
        saveCached(ctx, size, bmp)
    }

    /** Python 取数；失败返回空对象，绘制那边会显示「待同步」。 */
    private fun loadData(ctx: Context): JSONObject {
        return try {
            if (!Python.isStarted()) {
                return JSONObject().put("ok", false)
            }
            val mod = Python.getInstance().getModule("garmin_cn.appapi")
            val raw = mod.callAttr("widget_data").toString()
            JSONObject(raw)
        } catch (t: Throwable) {
            Log.w(TAG, "widget_data", t)
            JSONObject().put("ok", false)
        }
    }

    private fun cacheFile(ctx: Context, size: WidgetRender.Size): File =
        File(ctx.filesDir, "widget_${size.name}.png")

    private fun loadCached(ctx: Context, size: WidgetRender.Size): Bitmap? = try {
        val f = cacheFile(ctx, size)
        if (f.isFile && f.length() > 0) {
            android.graphics.BitmapFactory.decodeFile(f.absolutePath)
        } else null
    } catch (t: Throwable) {
        null
    }

    private fun saveCached(ctx: Context, size: WidgetRender.Size, bmp: Bitmap) {
        try {
            cacheFile(ctx, size).outputStream().use {
                bmp.compress(Bitmap.CompressFormat.PNG, 100, it)
            }
        } catch (t: Throwable) {
            Log.w(TAG, "save cache", t)
        }
    }

    /** 连缓存都没有时的兜底：一块透明画布，不显示假数据。 */
    private fun fallback(ctx: Context, size: WidgetRender.Size): Bitmap =
        Bitmap.createBitmap(size.pxW, size.pxH, Bitmap.Config.ARGB_8888)
}
