package mm.runnercoach.app

import android.graphics.Bitmap
import android.graphics.BlurMaskFilter
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RadialGradient
import android.graphics.RectF
import android.graphics.Shader
import android.graphics.Typeface
import org.json.JSONObject
import kotlin.math.min

/**
 * 把「辉光看板」画成小组件用的透明 PNG。
 *
 * 为什么不用 WebView 截 HTML：桌面小组件没有 Activity 窗口，离屏 WebView
 * 必须挂进窗口才会真正渲染（`renderToPng` 里那段注释就是踩过的坑），
 * 所以小组件这条路只能用原生 Canvas 重画一遍。好处是又快又小，
 * 代价是版式要跟网页版手工对齐。
 *
 * 三种规格共用一套版式，按宽度决定排几列、多放几行：
 *   2×4 (420×840)  竖排：日期 / 主数字 / 等级 / 三行指标
 *   3×4 (630×840)  加今日活动，指标两列
 *   4×4 (840×840)  指标三列，底部加周里程条
 */
object WidgetRender {
    enum class Size(val pxW: Int, val pxH: Int, val cols: Int, val full: Boolean) {
        W2(420, 840, 1, false),
        W3(630, 840, 2, false),
        W4(840, 840, 3, true),
    }

    private const val INK = 0xFFF2F6FA.toInt()
    private const val MUTED = 0xFF9AA7B4.toInt()
    private const val ACCENT = 0xFF3B82F6.toInt()
    private const val CYAN = 0xFF22D3EE.toInt()

    fun pick(minWidthDp: Int, minHeightDp: Int): Size = when {
        minWidthDp >= 250 -> Size.W4
        minWidthDp >= 170 -> Size.W3
        else -> Size.W2
    }

    fun render(data: JSONObject, size: Size): Bitmap {
        val w = size.pxW
        val h = size.pxH
        val bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
        val c = Canvas(bmp)
        // 透明底：桌面壁纸直接透出来，只留辉光和文字
        c.drawColor(Color.TRANSPARENT, android.graphics.PorterDuff.Mode.CLEAR)

        val main = data.optJSONObject("main")
        val score = main?.let { nullableInt(it, "score") }
        val level = main?.optString("level")?.takeIf { it.isNotBlank() } ?: "待同步"
        val brand = parseColor(main?.optString("color"), ACCENT)

        drawGlow(c, w, h, brand)

        val pad = w * 0.075f
        var y = pad

        // ---- 顶部：日期 + 星期（数据不是今天的会标出来）
        val top = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = MUTED
            textSize = h * 0.036f
            typeface = Typeface.DEFAULT_BOLD
            letterSpacing = 0.06f
        }
        c.drawText(data.optString("date"), pad, y + top.textSize, top)
        val wd = data.optString("weekday")
        if (wd.isNotBlank()) {
            top.textAlign = Paint.Align.RIGHT
            c.drawText(wd, w - pad, y + top.textSize, top)
            top.textAlign = Paint.Align.LEFT
        }
        val stale = data.optInt("stale", 0)
        if (stale > 0) {
            top.color = 0xFFE0A33A.toInt()
            top.textAlign = Paint.Align.RIGHT
            top.textSize = h * 0.030f
            c.drawText("数据 ${stale} 天前", w - pad, y + top.textSize + h * 0.045f, top)
            top.textAlign = Paint.Align.LEFT
        }
        y += h * 0.10f

        // ---- 主数字：恢复度（没有就睡眠分），带辉光
        val bigNum = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = INK
            textSize = h * 0.185f
            typeface = Typeface.create(Typeface.DEFAULT, Typeface.BOLD)
            setShadowLayer(h * 0.03f, 0f, h * 0.008f, 0x66000000)
        }
        val label = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = MUTED
            textSize = h * 0.034f
        }
        if (score != null) {
            c.drawText(score.toString(), pad, y + bigNum.textSize * 0.82f, bigNum)
            val nw = bigNum.measureText(score.toString())
            val kind = if (main?.optString("kind") == "sleep") "睡眠" else "恢复"
            c.drawText(kind, pad + nw + h * 0.018f,
                y + bigNum.textSize * 0.82f, label)
        } else {
            bigNum.textSize = h * 0.085f
            c.drawText(level, pad, y + bigNum.textSize, bigNum)
        }
        y += bigNum.textSize * 0.95f

        val lv = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = brand
            textSize = h * 0.040f
            typeface = Typeface.DEFAULT_BOLD
            setShadowLayer(h * 0.02f, 0f, 0f, 0x55000000)
        }
        c.drawText(level, pad, y + lv.textSize, lv)
        y += lv.textSize * 2.0f

        // ---- 指标区
        val items = ArrayList<Pair<String, String>>()
        nullableInt(data, "sleep_score")?.let { items.add("睡眠" to it.toString()) }
        data.optDouble("sleep_h", 0.0).takeIf { it > 0 }?.let {
            items.add("睡眠时长" to String.format("%.1fh", it))
        }
        nullableInt(data, "rhr")?.let { items.add("静息" to it.toString()) }
        nullableInt(data, "hrv")?.let { items.add("HRV" to it.toString()) }
        nullableInt(data, "bb")?.let { items.add("电量" to it.toString()) }
        data.optDouble("dist_km", 0.0).takeIf { it > 0 }?.let {
            items.add("今日" to String.format("%.1fkm", it))
        }
        nullableInt(data, "steps")?.let { items.add("步数" to it.toString()) }

        val show = if (size.full) items else items.take(if (size.cols >= 2) 4 else 3)
        if (show.isNotEmpty()) drawGrid(c, show, pad, y, w, h, size.cols, h - pad)

        // ---- 4×4 再加一条周里程，否则底部会空一大块
        if (size.full) {
            val wk = data.optDouble("week_km", 0.0)
            if (wk > 0) {
                val p = Paint(Paint.ANTI_ALIAS_FLAG).apply {
                    color = MUTED
                    textSize = h * 0.032f
                }
                c.drawText(String.format("本周 %.1f km", wk), pad, h - pad * 0.9f, p)
            }
        }
        return bmp
    }

    /** 指标网格：按列数切行，左标签右数值，中间用极淡的分隔线。 */
    private fun drawGrid(
        c: Canvas, items: List<Pair<String, String>>,
        left: Float, top: Float, w: Int, h: Int, cols: Int, bottom: Float,
    ) {
        val pad = left
        val colW = (w - pad * 2) / cols
        val rows = (items.size + cols - 1) / cols
        val rowH = min((bottom - top) / maxOf(rows, 1), h * 0.085f)
        val key = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = MUTED
            textSize = h * 0.030f
        }
        val val_ = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = INK
            textSize = h * 0.042f
            typeface = Typeface.DEFAULT_BOLD
        }
        items.forEachIndexed { i, (k, v) ->
            val cx = pad + (i % cols) * colW
            val cy = top + (i / cols) * rowH
            c.drawText(k, cx, cy + key.textSize, key)
            c.drawText(v, cx, cy + key.textSize + val_.textSize * 1.12f, val_)
        }
    }

    /** 右下角一团品牌色光晕，就是看板上那道「辉光」的小组件版。 */
    private fun drawGlow(c: Canvas, w: Int, h: Int, color: Int) {
        val r = min(w, h) * 0.62f
        val p = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            shader = RadialGradient(
                w * 0.82f, h * 0.06f, r,
                intArrayOf(alpha(color, 0.34f), alpha(color, 0.10f), Color.TRANSPARENT),
                floatArrayOf(0f, 0.45f, 1f), Shader.TileMode.CLAMP,
            )
            maskFilter = BlurMaskFilter(r * 0.10f, BlurMaskFilter.Blur.NORMAL)
        }
        c.drawCircle(w * 0.82f, h * 0.06f, r, p)
        // 第二道青色光，压在左下，跟看板的双辉光呼应
        val r2 = min(w, h) * 0.42f
        val p2 = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            shader = RadialGradient(
                w * 0.10f, h * 0.98f, r2,
                intArrayOf(alpha(CYAN, 0.20f), Color.TRANSPARENT),
                floatArrayOf(0f, 1f), Shader.TileMode.CLAMP,
            )
            maskFilter = BlurMaskFilter(r2 * 0.20f, BlurMaskFilter.Blur.NORMAL)
        }
        c.drawCircle(w * 0.10f, h * 0.98f, r2, p2)
    }

    private fun alpha(color: Int, a: Float): Int =
        Color.argb((a * 255).toInt().coerceIn(0, 255),
            Color.red(color), Color.green(color), Color.blue(color))

    private fun parseColor(s: String?, fallback: Int): Int = try {
        if (s.isNullOrBlank()) fallback else Color.parseColor(s)
    } catch (t: Throwable) {
        fallback
    }

    private fun nullableInt(o: JSONObject?, k: String): Int? {
        if (o == null || o.isNull(k)) return null
        val d = o.optDouble(k, Double.NaN)
        return if (d.isNaN()) null else d.toInt()
    }
}
