package mm.runnercoach.app

import android.graphics.Bitmap
import android.graphics.BlurMaskFilter
import android.graphics.Canvas
import android.graphics.LinearGradient
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
    enum class Size(
        val pxW: Int, val pxH: Int, val cols: Int,
        val full: Boolean, val wide: Boolean = false,
    ) {
        TALL(420, 840, 1, false),                 // 2×4 竖
        MID(630, 840, 2, false),                  // 3×4
        FULL(840, 840, 3, true),                  // 4×4
        WIDE(840, 420, 3, true, wide = true),     // 4×2 横
    }

    private const val INK = 0xFFF2F6FA.toInt()
    private const val MUTED = 0xFF9AA7B4.toInt()
    private const val ACCENT = 0xFF3B82F6.toInt()
    private const val CYAN = 0xFF22D3EE.toInt()

    /**
     * 按**容器实际宽高比**选版式，而不是只看宽度。
     *
     * 之前只看 minWidth，结果启动器给了一个 4×2 的横条，我们还在画 1:2 的
     * 竖版，`fitCenter` 一缩就变成中间一条窄图，两边全空——看着就是
     * 「信息特别少、还占了一大块位置」。
     */
    fun pick(minWidthDp: Int, minHeightDp: Int): Size {
        val w = minWidthDp.coerceAtLeast(1).toFloat()
        val h = minHeightDp.coerceAtLeast(1).toFloat()
        val ratio = w / h
        return when {
            ratio >= 1.5f -> Size.WIDE              // 宽扁：4×2
            ratio <= 0.70f -> if (w < 180) Size.TALL else Size.MID  // 窄高
            else -> Size.FULL                        // 接近方形：4×4
        }
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

        val pad = w * 0.06f
        val datePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = MUTED
            textSize = h * (if (size.wide) 0.055f else 0.036f)
            typeface = Typeface.DEFAULT_BOLD
            letterSpacing = 0.06f
        }
        val bigNum = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = INK
            textSize = h * (if (size.wide) 0.215f else 0.185f)
            typeface = Typeface.create(Typeface.DEFAULT, Typeface.BOLD)
            setShadowLayer(h * 0.03f, 0f, h * 0.008f, 0x66000000)
        }
        val lv = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = brand
            textSize = h * (if (size.wide) 0.070f else 0.040f)
            typeface = Typeface.DEFAULT_BOLD
            setShadowLayer(h * 0.02f, 0f, 0f, 0x55000000)
        }

        // 指标池：横版/方版尽量多放，窄版挑前三四个
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
        // 饮食：只有当天摄入有数才显示。历史摄入没有落库，所以这里放文字，
        // 不做成趋势柱——画一条只有一根柱的图反而误导。
        val nut = data.optJSONObject("intake")
        val eat = nut?.optInt("kcal", 0) ?: 0
        val goal = nut?.optInt("target", 0) ?: 0
        if (eat > 0) {
            items.add("饮食" to (if (goal > 0) "$eat/$goal" else eat.toString()))
        }

        if (size.wide) {
            // ---- 横版 4×2：左栏日期/主数字/等级，右栏指标两列
            val leftW = w * 0.44f
            var x = pad
            var y = pad + datePaint.textSize
            val wd = data.optString("weekday")
            val head = if (wd.isNotBlank()) data.optString("date") + "  " + wd
            else data.optString("date")
            c.drawText(head, x, y, datePaint)
            y += h * 0.10f

            if (score != null) {
                c.drawText(score.toString(), x, y + bigNum.textSize * 0.86f, bigNum)
                val nw = bigNum.measureText(score.toString())
                val kind = if (main?.optString("kind") == "sleep") " 睡眠" else " 恢复"
                datePaint.color = MUTED
                c.drawText(kind, x + nw, y + bigNum.textSize * 0.86f, datePaint)
            } else {
                bigNum.textSize = h * 0.13f
                c.drawText(level, x, y + bigNum.textSize, bigNum)
            }
            y += bigNum.textSize * 0.92f
            c.drawText(level, x, y + lv.textSize, lv)

            val show = items.take(4)
            if (show.isNotEmpty()) {
                drawGrid(c, show, leftW + pad * 0.4f, pad, (w - pad).toFloat(),
                    (h * 0.62f).toInt(), 2)
            }
            // 横版高度只有 420，指标两行之后还能塞一条很矮的趋势柱
            drawTrend(c, data, leftW + pad * 0.4f, h * 0.68f,
                (w - pad).toFloat(), h - pad, h, brand, tiny = true)
        } else {
            // ---- 竖版 / 方版：垂直堆叠
            var y = pad
            val wd = data.optString("weekday")
            c.drawText(data.optString("date"), pad, y + datePaint.textSize, datePaint)
            if (wd.isNotBlank()) {
                datePaint.textAlign = Paint.Align.RIGHT
                c.drawText(wd, w - pad, y + datePaint.textSize, datePaint)
                datePaint.textAlign = Paint.Align.LEFT
            }
            val stale = data.optInt("stale", 0)
            if (stale > 0) {
                datePaint.color = 0xFFE0A33A.toInt()
                datePaint.textAlign = Paint.Align.RIGHT
                datePaint.textSize = h * 0.030f
                c.drawText("数据 ${stale} 天前", w - pad,
                    y + datePaint.textSize + h * 0.045f, datePaint)
                datePaint.textAlign = Paint.Align.LEFT
                datePaint.textSize = h * 0.036f
            }
            y += h * 0.10f

            if (score != null) {
                c.drawText(score.toString(), pad, y + bigNum.textSize * 0.82f, bigNum)
                val nw = bigNum.measureText(score.toString())
                val kind = if (main?.optString("kind") == "sleep") "睡眠" else "恢复"
                val lbl = Paint(Paint.ANTI_ALIAS_FLAG).apply {
                    color = MUTED; textSize = h * 0.034f
                }
                c.drawText(kind, pad + nw + h * 0.018f,
                    y + bigNum.textSize * 0.82f, lbl)
            } else {
                bigNum.textSize = h * 0.085f
                c.drawText(level, pad, y + bigNum.textSize, bigNum)
            }
            y += bigNum.textSize * 0.95f
            c.drawText(level, pad, y + lv.textSize, lv)
            y += lv.textSize * 1.9f

            // 底部要留给趋势柱：把指标区的下边界收上来
            val barsH = h * 0.15f
            val gridBottom = h - pad - barsH - h * 0.055f
            val show = if (size.full) items else items.take(if (size.cols >= 2) 4 else 3)
            if (show.isNotEmpty()) {
                drawGrid(c, show, pad, y, (w - pad).toFloat(), gridBottom.toInt(), size.cols)
            }
            drawTrend(c, data, pad, gridBottom + h * 0.02f,
                (w - pad).toFloat(), h - pad, h, brand)
        }
        return bmp
    }

    /**
     * 近 7 天趋势柱：看板上那条「一周起伏」的小图。
     *
     * 优先画活动里程；如果这一周一条活动都没有（比如刚同步、或只戴口罩的日子），
     * 退回睡眠时长——总比画一排零高的小点强。
     */
    private fun drawTrend(
        c: Canvas, data: JSONObject, left: Float, top: Float,
        right: Float, bottom: Float, h: Int, color: Int, tiny: Boolean = false,
    ) {
        var vals = series(data, "km")
        var label = "近 7 天里程"
        if (vals.none { it > 0.05f }) {
            vals = series(data, "sleep")
            label = "近 7 天睡眠"
        }
        if (vals.isEmpty() || vals.none { it > 0.05f }) return

        val labW = if (tiny) 0f else h * 0.10f
        val key = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            // 必须写 this.color：函数参数也叫 color，直接写会被当成给参数赋值
            this.color = MUTED
            textSize = h * (if (tiny) 0.030f else 0.028f)
        }
        if (!tiny) {
            c.drawText(label, left, top + key.textSize * 0.9f, key)
        }
        val barTop = top + (if (tiny) 0f else key.textSize * 1.4f)
        drawBars(c, vals, left + labW, barTop, right, bottom, color)
    }

    private fun drawBars(
        c: Canvas, vals: List<Float>, left: Float, top: Float,
        right: Float, bottom: Float, color: Int,
    ) {
        if (vals.isEmpty() || bottom <= top) return
        val n = vals.size
        val slot = (right - left) / n
        val bw = slot * 0.58f
        val maxV = maxOf(0.05f, vals.maxOrNull() ?: 1f)
        vals.forEachIndexed { i, v ->
            val x = left + i * slot + (slot - bw) / 2f
            // 零值也留一个 2% 的矮块，让「那天没动」看得见，而不是一片空白
            val hh = (bottom - top) * (v / maxV).coerceIn(0.02f, 1f)
            val rect = RectF(x, bottom - hh, x + bw, bottom)
            val p = Paint(Paint.ANTI_ALIAS_FLAG).apply {
                shader = LinearGradient(
                    0f, bottom - hh, 0f, bottom,
                    alpha(color, 0.92f), alpha(color, 0.26f), Shader.TileMode.CLAMP,
                )
            }
            c.drawRoundRect(rect, bw * 0.32f, bw * 0.32f, p)
        }
    }

    private fun series(data: JSONObject, key: String): List<Float> {
        val arr = data.optJSONArray("days") ?: return emptyList()
        val out = ArrayList<Float>(arr.length())
        for (i in 0 until arr.length()) {
            out.add(arr.optJSONObject(i)?.optDouble(key, 0.0)?.toFloat() ?: 0f)
        }
        return out
    }

    /** 指标网格：按列数切行，左标签右数值，中间用极淡的分隔线。 */
    private fun drawGrid(
        c: Canvas, items: List<Pair<String, String>>,
        left: Float, top: Float, right: Float, h: Int, cols: Int,
    ) {
        val colW = (right - left) / cols
        val bottom = h.toFloat()
        val rows = (items.size + cols - 1) / cols
        // 行高上限给到 0.16h：横版画布只有 420 高，按 0.085h 算出来的行距
        // 比字号还小，三行会挤成一团。
        val rowH = min((bottom - top) / maxOf(rows, 1), h * 0.16f)
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
            val cx = left + (i % cols) * colW
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
