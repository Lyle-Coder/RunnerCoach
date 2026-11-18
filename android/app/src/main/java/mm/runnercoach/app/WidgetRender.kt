package mm.runnercoach.app

import android.graphics.Bitmap
import android.graphics.BlurMaskFilter
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Path
import android.graphics.PorterDuff
import android.graphics.RadialGradient
import android.graphics.RectF
import android.graphics.Shader
import android.graphics.Typeface
import org.json.JSONObject
import java.util.Locale
import kotlin.math.max
import kotlin.math.min

/**
 * 桌面小组件的「今日数据看板」：透明底 + 多张玻璃卡，每张一种图表。
 *
 * 为什么不用 WebView 截 HTML：桌面小组件没有 Activity 窗口，离屏 WebView
 * 必须挂进窗口才会真正渲染（`renderToPng` 里那段注释就是踩过的坑），
 * 所以只能用原生 Canvas 重画一遍。
 *
 * 版式按容器宽高比分四档，卡片数量不变、密度随尺寸调整：
 *   WIDE 4×2   3 列 × 2 行 + 底部建议条
 *   TALL 2×4   单列 6 张卡（物理高度有限，去掉趋势柱换更大的字）
 *   MID  3×4 / FULL 4×4   2 列 × 3 行 + 底部建议条
 *
 * 字号 deliberately 以画布绝对像素给（25~31px），不按卡片高度百分比算：
 * 小组件最终会按 fitCenter 缩到 ~0.4 倍显示，百分比字号在横版 4×2 上
 * 会缩到 5dp 根本看不清。
 */
object WidgetRender {
    enum class Size(val pxW: Int, val pxH: Int, val sp: Float, val wide: Boolean = false) {
        TALL(420, 840, 24f),                  // 2×4 竖（窄，少放一张趋势柱换字更大）
        MID(630, 840, 28f),                   // 3×4
        FULL(840, 840, 31f),                  // 4×4
        WIDE(840, 420, 25f, wide = true),     // 4×2 横
    }

    private enum class Type { READINESS, NUTRITION, PLAN, ACTIVITY, RACE, TREND, AI }

    private class B(val l: Float, val t: Float, val r: Float, val bot: Float) {
        val w: Float get() = r - l
        val h: Float get() = bot - t
    }

    private const val INK = 0xFFF2F6FA.toInt()
    private const val MUTED = 0xFF9AA7B4.toInt()
    private const val ACCENT = 0xFF3B82F6.toInt()
    private const val CYAN = 0xFF22D3EE.toInt()
    private const val GREEN = 0xFF4FD6A6.toInt()
    private const val YELLOW = 0xFFD6A75A.toInt()
    private const val RED = 0xFFE4574D.toInt()
    private const val PROTEIN = 0xFF56D6A8.toInt()
    private const val CARB = 0xFF5FA8E0.toInt()
    private const val FAT = 0xFFE0B363.toInt()

    /**
     * 按**容器实际宽高比**选版式，而不是只看宽度。
     * 之前只看 minWidth，启动器给 4×2 横条时还在画 1:2 竖版，fitCenter
     * 一缩就变成中间一条窄图，两边全空。
     */
    fun pick(minWidthDp: Int, minHeightDp: Int): Size {
        val w = minWidthDp.coerceAtLeast(1).toFloat()
        val h = minHeightDp.coerceAtLeast(1).toFloat()
        val ratio = w / h
        return when {
            ratio >= 1.5f -> Size.WIDE
            ratio <= 0.70f -> if (w < 180) Size.TALL else Size.MID
            else -> Size.FULL
        }
    }

    fun render(data: JSONObject, size: Size): Bitmap {
        val w = size.pxW
        val h = size.pxH
        val bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
        val c = Canvas(bmp)
        // 透明底：壁纸直接透出来，卡片只压一层很淡的暗玻璃
        c.drawColor(Color.TRANSPARENT, PorterDuff.Mode.CLEAR)

        val rd = data.optJSONObject("readiness") ?: data.optJSONObject("main")
        val brand = parseColor(rd?.optString("color"), ACCENT)
        drawGlow(c, w, h, brand)

        val pad = min(w, h) * 0.042f
        val area = B(pad, pad, w - pad, h - pad)
        // 库里没数据 / Python 还没起来时，别摆一屏空卡片
        if (!data.optBoolean("ok", true)) {
            drawEmpty(c, area, size.sp, data.optString("error"))
            return bmp
        }
        // 单张卡出错不能让整个小组件留白：其余卡片照常出
        for ((type, box) in layout(size, area)) {
            try {
                drawCard(c, data, type, box, size.sp, brand)
            } catch (t: Throwable) {
                // 忽略这一张，继续画下一张
            }
        }
        return bmp
    }

    private fun drawCard(c: Canvas, data: JSONObject, type: Type, b: B, sp: Float, brand: Int) {
        when (type) {
            Type.READINESS -> drawReadiness(c, data, b, sp, brand)
            Type.NUTRITION -> drawNutrition(c, data, b, sp)
            Type.PLAN -> drawPlan(c, data, b, sp)
            Type.ACTIVITY -> drawActivity(c, data, b, sp)
            Type.RACE -> drawRace(c, data, b, sp)
            Type.TREND -> drawTrend(c, data, b, sp, brand)
            Type.AI -> drawAI(c, data, b, sp)
        }
    }

    /** 卡片背景 + 标题行，返回内容区。 */
    private fun panel(
        c: Canvas, b: B, sp: Float, title: String, tint: Int,
        note: String, noteTint: Int = MUTED,
    ): B {
        val rr = RectF(b.l, b.t, b.r, b.bot)
        val rad = min(b.w, b.h) * 0.10f
        val bg = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            this.color = Color.argb(58, 8, 14, 22)
        }
        c.drawRoundRect(rr, rad, rad, bg)
        val edge = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            style = Paint.Style.STROKE
            strokeWidth = max(1f, sp * 0.045f)
            this.color = alpha(INK, 0.10f)
        }
        c.drawRoundRect(rr, rad, rad, edge)

        val ins = min(b.w, b.h) * 0.10f
        val tf = sp * 0.74f
        val base = b.t + ins + tf
        val dot = Paint(Paint.ANTI_ALIAS_FLAG).apply { this.color = tint }
        c.drawCircle(b.l + ins + tf * 0.14f, base - tf * 0.32f, tf * 0.15f, dot)
        val titleP = tp(tf, INK, true)
        val noteP = tp(tf * 0.88f, noteTint)
        val tx = b.l + ins + tf * 0.48f
        // 标题最多占卡片一半多一点，剩下的宽度才给右侧角标。
        // 之前两者各自按 b.w*0.46 截断，在 4×2 的 1/3 宽卡上会实打实压在一起。
        val titleW = min(titleP.measureText(title), b.w * 0.58f)
        val noteMax = (b.r - ins - tx - titleW - sp * 0.24f).coerceAtLeast(0f)
        val noteS = if (note.isBlank()) "" else ellip(noteP, note, noteMax)
        txt(c, ellip(titleP, title, titleW), tx, base, titleP)
        if (noteS.isNotEmpty()) {
            txt(c, noteS, b.r - ins, base, noteP, Paint.Align.RIGHT)
        }
        // 标题下缘约在 base + 0.20*tf，内容区再让出 0.28*tf 才不会贴脸
        return B(b.l + ins, base + ins * 0.34f + tf * 0.28f,
            b.r - ins, b.bot - ins * 0.70f)
    }

    // ---------------------------------------------------------------- 恢复度：环形仪表 + 分项条
    private fun drawReadiness(c: Canvas, data: JSONObject, b: B, sp: Float, brand: Int) {
        val rd = data.optJSONObject("readiness") ?: data.optJSONObject("main")
        val tint = parseColor(rd?.optString("color"), brand)
        val sc = rd?.optDouble("score", Double.NaN) ?: Double.NaN
        val level = rd?.optString("level")?.takeIf { it.isNotBlank() } ?: "待同步"
        val body = panel(c, b, sp, "今日恢复度", tint, "")

        val parts = rd?.optJSONArray("parts")
        // 横版卡矮，四条分项会挤成一根线：少画一条换可读性
        val n = if (parts != null) min(parts.length(), if (body.h < sp * 4.6f) 3 else 4) else 0
        val ringR = min(body.h * 0.40f, if (n > 0) body.w * 0.21f else body.w * 0.33f)
            .coerceAtLeast(sp * 0.8f)
        val cx = body.l + ringR + if (n > 0) sp * 0.10f else (body.w - 2f * ringR) / 2f
        val cy = (body.t + body.bot) / 2f
        ring(c, cx, cy, ringR, if (sc.isNaN()) 0f else (sc / 100.0).toFloat().coerceIn(0f, 1f), tint)

        val big = tp(ringR * 0.78f, INK, true)
        big.setShadowLayer(sp * 0.24f, 0f, sp * 0.10f, 0x66000000)
        // 分数往上提、等级往下放：按字形高度算，两行之间留 0.15r 的净空
        txt(c, if (sc.isNaN()) "—" else sc.toInt().toString(), cx,
            cy + big.textSize * 0.16f, big, Paint.Align.CENTER)
        val lv = tp(min(sp * 0.58f, ringR * 0.30f), alpha(tint, 0.98f), true)
        txt(c, level, cx, cy + ringR * 0.70f, lv, Paint.Align.CENTER)

        if (n == 0 || parts == null) return
        val px = body.l + 2f * ringR + sp * 0.55f
        if (px >= body.r - sp * 2f) return
        val rowH = body.h / n
        val fs = min(sp * 0.60f, rowH * 0.40f)
        val lp = tp(fs, MUTED)
        val vp = tp(fs, INK, true)
        for (i in 0 until n) {
            val p = parts.optJSONObject(i) ?: continue
            val v = p.optDouble("score", 0.0).toFloat()
            val top = body.t + i * rowH + rowH * 0.04f
            pair(c, p.optString("name"), v.toInt().toString(),
                px, body.r, top + fs, lp, vp, fs * 0.40f)
            bar(c, px, top + fs * 1.35f, body.r - px,
                min(sp * 0.20f, rowH * 0.20f).coerceAtLeast(3f),
                v / 100f, scoreColor(v))
        }
    }

    // ---------------------------------------------------------------- 营养：三条完成度进度条
    private fun drawNutrition(c: Canvas, data: JSONObject, b: B, sp: Float) {
        val m = data.optJSONObject("macro")
        val goal = m?.optString("goal")?.takeIf { it.isNotBlank() } ?: ""
        val body = panel(c, b, sp, "三大营养素", PROTEIN, goal)
        if (m == null) {
            txt(c, "还没有体重数据", body.l, body.t + sp, tp(sp * 0.8f, MUTED))
            return
        }
        val kcal = m.optInt("kcal", 0)
        val kt = m.optInt("kcal_target", 0)
        val fs = min(sp * 0.80f, body.h * 0.17f)
        val kcalP = tp(fs, INK, true)
        val stateP = tp(fs * 0.82f, MUTED)
        pair(c, "$kcal / $kt kcal",
            if (m.optBoolean("logged", false)) "已记录饮食" else "未记录",
            body.l, body.r, body.t + fs, kcalP, stateP, fs * 0.35f)

        val top = body.t + fs * 1.45f
        val rowH = (body.bot - top) / 3f
        val lab = min(sp * 0.66f, rowH * 0.42f)
        val lp = tp(lab, MUTED)
        val vp = tp(lab, INK, true)
        val rows = arrayOf(
            Triple("蛋白质", m.optJSONArray("protein"), PROTEIN),
            Triple("碳水", m.optJSONArray("carb"), CARB),
            Triple("脂肪", m.optJSONArray("fat"), FAT),
        )
        rows.forEachIndexed { i, row ->
            val (label, arr, tint) = row
            val v = arr?.optDouble(0, 0.0)?.toFloat() ?: 0f
            val tgt = arr?.optDouble(1, 0.0)?.toFloat() ?: 0f
            val ratio = if (tgt > 0f) v / tgt else 0f
            // 与网页看板同口径：蛋白/碳水是下限（超 10% 才红），脂肪是上限（超了就红）
            val col = when {
                tgt <= 0f -> MUTED
                i == 2 && ratio > 1.001f -> RED
                ratio > 1.10f -> RED
                ratio >= 1f -> GREEN
                else -> YELLOW
            }
            val y = top + i * rowH
            pair(c, label, "${f0(v.toDouble())} / ${f0(tgt.toDouble())} g",
                body.l, body.r, y + lab, lp, vp, lab * 0.50f)
            bar(c, body.l, y + lab * 1.30f, body.w,
                min(sp * 0.24f, rowH * 0.30f).coerceAtLeast(3f), ratio, col)
        }
    }

    // ---------------------------------------------------------------- 训练计划：7 格周历 + 课名
    private fun drawPlan(c: Canvas, data: JSONObject, b: B, sp: Float) {
        val p = data.optJSONObject("plan")
        val done = p?.optBoolean("done", false) ?: false
        val rest = p?.optBoolean("rest", false) ?: false
        val note = when {
            done -> "已完成"
            rest -> "休息"
            else -> "待完成"
        }
        val noteTint = if (done) GREEN else if (rest) MUTED else YELLOW
        val body = panel(c, b, sp, "今日训练计划", CARB, note, noteTint)

        val title = p?.optString("title")?.takeIf { it.isNotBlank() } ?: "自主训练"
        val t1 = body.t + sp * 0.92f
        val big = tp(sp * 0.92f, INK, true)
        txt(c, ellip(big, title, body.w), body.l, t1, big)
        val wd = p?.optInt("week_done", 0) ?: 0
        val wp = p?.optInt("week_planned", 0) ?: 0
        val mins = p?.optInt("minutes", 0) ?: 0
        val sub = "本周完成 $wd/$wp 次" + if (mins > 0) " · $mins 分钟" else ""
        val sp0 = tp(sp * 0.62f, MUTED)
        // 课名是大字，行距按两个字号算；固定 0.78sp 时大字下缘会压住副标题
        val t2 = t1 + big.textSize * 0.42f + sp0.textSize * 1.10f
        txt(c, ellip(sp0, sub, body.w), body.l, t2, sp0)

        val arr = p?.optJSONArray("week")
        val n = arr?.length() ?: 0
        if (n == 0 || arr == null) return
        val dotH = min(sp * 0.72f, body.h * 0.20f).coerceAtLeast(6f)
        val dotTop = max(t2 + sp * 0.36f, body.bot - dotH - sp * 0.72f)
        val slot = body.w / n
        val dw = slot * 0.70f
        val dp = tp(min(sp * 0.54f, dotH * 0.58f), MUTED)
        for (i in 0 until n) {
            val o = arr.optJSONObject(i)
            val st = o?.optString("state") ?: "rest"
            val col = when (st) {
                "done" -> GREEN
                "missed" -> RED
                "extra" -> CYAN
                "plan" -> ACCENT
                else -> alpha(INK, 0.16f)
            }
            val x = body.l + i * slot + (slot - dw) / 2f
            val rr = RectF(x, dotTop, x + dw, dotTop + dotH)
            val fill = Paint(Paint.ANTI_ALIAS_FLAG).apply {
                this.color = alpha(col, if (st == "rest") 0.9f else 0.94f)
            }
            c.drawRoundRect(rr, dotH * 0.28f, dotH * 0.28f, fill)
            if (st == "done") {
                val ck = Paint(Paint.ANTI_ALIAS_FLAG).apply {
                    style = Paint.Style.STROKE
                    strokeWidth = max(1.5f, dotH * 0.10f)
                    strokeCap = Paint.Cap.ROUND
                    this.color = 0xFF0B1A14.toInt()
                }
                val path = Path()
                path.moveTo(x + dw * 0.25f, dotTop + dotH * 0.52f)
                path.lineTo(x + dw * 0.45f, dotTop + dotH * 0.72f)
                path.lineTo(x + dw * 0.78f, dotTop + dotH * 0.30f)
                c.drawPath(path, ck)
            }
            txt(c, o?.optString("d") ?: "", x + dw / 2f,
                dotTop + dotH + dp.textSize * 0.95f, dp, Paint.Align.CENTER)
        }
    }

    // ---------------------------------------------------------------- 今日活动：横条 + 步数进度
    private fun drawActivity(c: Canvas, data: JSONObject, b: B, sp: Float) {
        val steps = data.optDouble("steps", 0.0).toFloat()
        val goal = data.optDouble("step_goal", 0.0).toFloat()
        val pct = if (goal > 0f) (steps / goal * 100f).coerceAtMost(100f) else 0f
        val body = panel(c, b, sp, "今日活动", CYAN, "步数 ${pct.toInt()}%")

        val acts = data.optJSONArray("acts")
        val n = min(acts?.length() ?: 0, 3)
        val footH = sp * 1.35f
        val areaBot = body.bot - footH
        if (n == 0 || acts == null) {
            val ep = tp(sp * 0.78f, alpha(INK, 0.68f))
            txt(c, "今天还没有训练记录", body.l,
                (body.t + areaBot) / 2f + ep.textSize * 0.3f, ep)
        } else {
            var maxKm = 0.01f
            for (i in 0 until n) {
                maxKm = max(maxKm, acts.optJSONObject(i)?.optDouble("km", 0.0)?.toFloat() ?: 0f)
            }
            val rowH = (areaBot - body.t) / n
            val fs = min(sp * 0.68f, rowH * 0.42f)
            val lp = tp(fs, MUTED)
            val vp = tp(fs, INK, true)
            for (i in 0 until n) {
                val a = acts.optJSONObject(i) ?: continue
                val km = a.optDouble("km", 0.0).toFloat()
                val y = body.t + i * rowH
                pair(c, a.optString("name").trim(),
                    "${f1(km.toDouble())} km · ${a.optInt("min", 0)}′",
                    body.l, body.r, y + fs, lp, vp, fs * 0.40f)
                bar(c, body.l, y + fs * 1.32f, body.w,
                    min(sp * 0.22f, rowH * 0.26f).coerceAtLeast(3f),
                    km / maxKm, CYAN)
            }
        }
        val fs2 = min(sp * 0.60f, footH * 0.40f)
        val lp2 = tp(fs2, MUTED)
        val base = areaBot + fs2
        txt(c, "步数 ${steps.toInt()} / ${goal.toInt()}", body.l, base, lp2)
        bar(c, body.l, base + fs2 * 0.55f, body.w,
            min(sp * 0.20f, footH * 0.18f).coerceAtLeast(3f), pct / 100f, ACCENT)
    }

    // ---------------------------------------------------------------- 备赛：倒计时大数 + 周量进度
    private fun drawRace(c: Canvas, data: JSONObject, b: B, sp: Float) {
        val r = data.optJSONObject("race")
        val days = r?.optInt("days", -1) ?: -1
        val phase = r?.optString("phase")?.takeIf { it.isNotBlank() } ?: ""
        val note = if (phase.isNotBlank()) "W${r?.optInt("week_no", 0) ?: 0} · $phase" else ""
        val body = panel(c, b, sp, "备赛", FAT, note)

        val leftW = body.w * 0.46f
        val bigFs = min(sp * 2.0f, body.h * 0.60f)
        val big = tp(bigFs, INK, true)
        big.setShadowLayer(sp * 0.28f, 0f, sp * 0.10f, 0x66000000)
        val num = if (days > 0) days.toString() else if (days == 0) "今日" else "—"
        txt(c, num, body.l, body.t + bigFs * 0.90f, big)
        if (days > 0) {
            txt(c, "天", body.l + big.measureText(num) + sp * 0.12f,
                body.t + bigFs * 0.90f, tp(sp * 0.72f, FAT))
        }
        val sub = if (days >= 0) "距 ${r?.optString("name") ?: ""}" else "未设置赛事"
        val sp0 = tp(sp * 0.62f, MUTED)
        txt(c, ellip(sp0, sub, leftW), body.l, body.t + bigFs * 1.55f, sp0)

        val x = body.l + leftW + sp * 0.25f
        if (x >= body.r - sp * 2f) return
        val wk = r?.optDouble("week_km", 0.0)?.toFloat() ?: 0f
        val tgt = r?.optDouble("week_target", 0.0)?.toFloat() ?: 0f
        val fs = min(sp * 0.62f, body.h * 0.22f)
        val head = if (tgt > 0f) "本周 ${f1(wk.toDouble())} / ${f0(tgt.toDouble())} km"
        else "本周 ${f1(wk.toDouble())} km"
        val lp = tp(fs, MUTED)
        txt(c, ellip(lp, head, body.r - x), x, body.t + fs * 1.10f, lp)
        val ratio = if (tgt > 0f) wk / tgt else 0f
        bar(c, x, body.t + fs * 1.85f, body.r - x,
            min(sp * 0.26f, body.h * 0.14f).coerceAtLeast(3f),
            ratio, if (ratio >= 1f) GREEN else ACCENT)
        val foot = if (tgt > 0f) "完成 ${(ratio * 100f).toInt()}%" else "按计划推进"
        txt(c, foot, x, body.t + fs * 3.45f, tp(fs * 0.92f, alpha(INK, 0.82f)))
    }

    // ---------------------------------------------------------------- 近 7 天柱状图
    private fun drawTrend(c: Canvas, data: JSONObject, b: B, sp: Float, brand: Int) {
        var vals = series(data, "km")
        var label = "近 7 天里程"
        if (vals.none { it > 0.05f }) {
            vals = series(data, "sleep")
            label = "近 7 天睡眠"
        }
        val avg = if (vals.isEmpty()) 0f else vals.average().toFloat()
        val body = panel(c, b, sp, label, ACCENT,
            if (avg > 0f) "平均 ${f1(avg.toDouble())}" else "")
        if (vals.isEmpty() || vals.none { it > 0.05f }) {
            txt(c, "暂无数据", body.l, body.t + sp, tp(sp * 0.78f, MUTED))
            return
        }
        val labelH = sp * 0.70f
        drawBars(c, vals, body.l, body.t, body.r, body.bot - labelH, brand)
        val arr = data.optJSONArray("days")
            if (arr != null && arr.length() > 0) {
                val dp = tp(min(sp * 0.55f, labelH * 0.8f), MUTED)
                val db = body.bot - dp.textSize * 0.20f
                txt(c, arr.optJSONObject(0)?.optString("d") ?: "", body.l, db, dp)
                txt(c, arr.optJSONObject(arr.length() - 1)?.optString("d") ?: "",
                    body.r, db, dp, Paint.Align.RIGHT)
            }
    }

    private fun drawBars(
        c: Canvas, vals: List<Float>, left: Float, top: Float,
        right: Float, bottom: Float, tint: Int,
    ) {
        if (vals.isEmpty() || bottom <= top) return
        val n = vals.size
        val slot = (right - left) / n
        val bw = slot * 0.56f
        val maxV = maxOf(0.05f, vals.maxOrNull() ?: 1f)
        val mean = vals.average().toFloat()
        // 均值虚线：一眼看出哪天在平均水平之上
        if (mean > 0f) {
            val y = bottom - (bottom - top) * (mean / maxV).coerceIn(0f, 1f)
            val dp = Paint(Paint.ANTI_ALIAS_FLAG).apply {
                style = Paint.Style.STROKE
                strokeWidth = 2f
                this.color = alpha(INK, 0.22f)
                pathEffect = android.graphics.DashPathEffect(floatArrayOf(8f, 8f), 0f)
            }
            c.drawLine(left, y, right, y, dp)
        }
        vals.forEachIndexed { i, v ->
            val x = left + i * slot + (slot - bw) / 2f
            // 零值留一个 3% 矮块，让「那天没动」看得见，而不是一片空白
            val hh = (bottom - top) * (v / maxV).coerceIn(0.03f, 1f)
            val rr = RectF(x, bottom - hh, x + bw, bottom)
            val p = Paint(Paint.ANTI_ALIAS_FLAG).apply {
                shader = LinearGradient(
                    0f, bottom - hh, 0f, bottom,
                    alpha(tint, 0.94f), alpha(tint, 0.24f), Shader.TileMode.CLAMP,
                )
            }
            c.drawRoundRect(rr, bw * 0.32f, bw * 0.32f, p)
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

    // ---------------------------------------------------------------- AI 建议 / 今日提醒
    private fun drawAI(c: Canvas, data: JSONObject, b: B, sp: Float) {
        val ai = data.optJSONObject("ai")
        val kind = ai?.optString("kind") ?: "none"
        val text = (ai?.optString("text") ?: "").trim()
        val title = if (kind == "ai") "✦ AI 教练" else "今日提醒"
        val stale = data.optInt("stale", 0)
        val body = panel(c, b, sp, title, CYAN,
            if (stale > 0) "数据 $stale 天前" else "", if (stale > 3) YELLOW else MUTED)
        // 按内容区实际高度决定字号与行数：矮卡（4×2 底部通栏）只放一行，
        // 之前字号被 coerceAtLeast 顶到 0.55sp，标题下缘和正文上缘几乎贴在一起。
        val fs = min(sp * 0.82f, body.h * 0.30f).coerceAtLeast(sp * 0.52f)
        val p = tp(fs, alpha(INK, 0.94f))
        val lineH = fs * 1.32f
        val avail = (body.bot - body.t).coerceAtLeast(0f)
        val maxLines = (avail / lineH).toInt().coerceIn(1, 2)
        val plain = text.replace(Regex("\\s+"), " ")
        val lines = if (plain.isBlank()) listOf("打开 App 同步后，这里会有当天建议")
        else wrap(p, plain, body.w, maxLines)
        val first = body.t + fs * 1.02f
        val fit = min(lines.size, maxOf(1, ((body.bot - first) / lineH).toInt() + 1))
        for (i in 0 until fit) txt(c, lines[i], body.l, first + i * lineH, p)
    }

    /** 还没同步过：一块提示卡，不摆一屏「待同步」的空图表。 */
    private fun drawEmpty(c: Canvas, b: B, sp: Float, msg: String) {
        val rr = RectF(b.l, b.t, b.r, b.bot)
        val p = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            this.color = Color.argb(58, 8, 14, 22)
        }
        c.drawRoundRect(rr, min(b.w, b.h) * 0.09f, min(b.w, b.h) * 0.09f, p)
        val cx = (b.l + b.r) / 2f
        val cy = (b.t + b.bot) / 2f
        val t1 = tp(sp * 1.15f, INK, true)
        txt(c, "今日数据看板", cx, cy - sp * 0.3f, t1, Paint.Align.CENTER)
        val line = msg.takeIf { it.isNotBlank() } ?: "打开 App 同步一次就有数据了"
        val t2 = tp(sp * 0.78f, MUTED)
        txt(c, ellip(t2, line, b.w * 0.85f), cx,
            cy + sp * 1.1f, t2, Paint.Align.CENTER)
    }

    // ---------------------------------------------------------------- 版式
    private fun layout(size: Size, area: B): List<Pair<Type, B>> {
        val gap = min(area.w, area.h) * 0.020f
        fun cell(x0: Float, y0: Float, x1: Float, y1: Float): B = B(
            area.l + area.w * x0 + gap / 2f,
            area.t + area.h * y0 + gap / 2f,
            area.l + area.w * x1 - gap / 2f,
            area.t + area.h * y1 - gap / 2f,
        )
        return when (size) {
            // 底部通栏要留 20% 给建议文字，否则标题和正文会挤成一行
            Size.WIDE -> listOf(
                Type.READINESS to cell(0f, 0f, 1f / 3f, 0.40f),
                Type.NUTRITION to cell(1f / 3f, 0f, 2f / 3f, 0.40f),
                Type.PLAN to cell(2f / 3f, 0f, 1f, 0.40f),
                Type.ACTIVITY to cell(0f, 0.40f, 1f / 3f, 0.80f),
                Type.RACE to cell(1f / 3f, 0.40f, 2f / 3f, 0.80f),
                Type.TREND to cell(2f / 3f, 0.40f, 1f, 0.80f),
                Type.AI to cell(0f, 0.80f, 1f, 1f),
            )
            Size.TALL -> listOf(
                Type.READINESS to cell(0f, 0f, 1f, 0.180f),
                Type.NUTRITION to cell(0f, 0.180f, 1f, 0.380f),
                Type.PLAN to cell(0f, 0.380f, 1f, 0.570f),
                Type.ACTIVITY to cell(0f, 0.570f, 1f, 0.740f),
                Type.RACE to cell(0f, 0.740f, 1f, 0.870f),
                Type.AI to cell(0f, 0.870f, 1f, 1f),
            )
            else -> listOf(                       // MID / FULL：2 列 × 3 行 + 通栏建议
                Type.READINESS to cell(0f, 0f, 0.5f, 0.30f),
                Type.NUTRITION to cell(0.5f, 0f, 1f, 0.30f),
                Type.PLAN to cell(0f, 0.30f, 0.5f, 0.58f),
                Type.ACTIVITY to cell(0.5f, 0.30f, 1f, 0.58f),
                Type.RACE to cell(0f, 0.58f, 0.5f, 0.83f),
                Type.TREND to cell(0.5f, 0.58f, 1f, 0.83f),
                Type.AI to cell(0f, 0.83f, 1f, 1f),
            )
        }
    }

    /** 右下角一团品牌色光晕：看板那道「辉光」的小组件版，压得很淡不糊文字。 */
    private fun drawGlow(c: Canvas, w: Int, h: Int, color: Int) {
        val r = min(w, h) * 0.62f
        val p = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            shader = RadialGradient(
                w * 0.82f, h * 0.06f, r,
                intArrayOf(alpha(color, 0.22f), alpha(color, 0.07f), Color.TRANSPARENT),
                floatArrayOf(0f, 0.45f, 1f), Shader.TileMode.CLAMP,
            )
            maskFilter = BlurMaskFilter(r * 0.10f, BlurMaskFilter.Blur.NORMAL)
        }
        c.drawCircle(w * 0.82f, h * 0.06f, r, p)
        val r2 = min(w, h) * 0.42f
        val p2 = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            shader = RadialGradient(
                w * 0.10f, h * 0.98f, r2,
                intArrayOf(alpha(CYAN, 0.14f), Color.TRANSPARENT),
                floatArrayOf(0f, 1f), Shader.TileMode.CLAMP,
            )
            maskFilter = BlurMaskFilter(r2 * 0.20f, BlurMaskFilter.Blur.NORMAL)
        }
        c.drawCircle(w * 0.10f, h * 0.98f, r2, p2)
    }

    // ---------------------------------------------------------------- 绘图小工具
    private fun tp(size: Float, color: Int, bold: Boolean = false): Paint =
        Paint(Paint.ANTI_ALIAS_FLAG).apply {
            // 必须写 this.color：函数参数也叫 color，直接写会被当成给参数赋值
            this.color = color
            textSize = size
            if (bold) typeface = Typeface.create(Typeface.DEFAULT, Typeface.BOLD)
        }

    private fun txt(
        c: Canvas, s: String, x: Float, y: Float, p: Paint,
        align: Paint.Align = Paint.Align.LEFT,
    ) {
        p.textAlign = align
        c.drawText(s, x, y, p)
    }

    /**
     * 同一行「左标签 + 右数值」，按右侧实际字宽给左侧留位置。
     * 之前左侧一律按 56% 截断，右侧数值一长（100、2552 g）就会撞上。
     */
    private fun pair(
        c: Canvas, left: String, right: String,
        x0: Float, x1: Float, baseline: Float,
        lp: Paint, rp: Paint, gap: Float,
    ) {
        lp.textAlign = Paint.Align.LEFT
        val rw = if (right.isEmpty()) 0f else rp.measureText(right)
        val lw = (x1 - x0 - rw - gap).coerceAtLeast(0f)
        txt(c, ellip(lp, left, lw), x0, baseline, lp)
        if (right.isNotEmpty()) txt(c, right, x1, baseline, rp, Paint.Align.RIGHT)
    }

    private fun ellip(p: Paint, s: String, maxW: Float): String {
        if (maxW <= 0f) return ""
        if (p.measureText(s) <= maxW) return s
        var end = s.length
        while (end > 0 && p.measureText(s.substring(0, end) + "…") > maxW) end--
        return s.substring(0, end) + "…"
    }

    private fun wrap(p: Paint, s: String, maxW: Float, maxLines: Int): List<String> {
        val out = ArrayList<String>(maxLines)
        var rest = s
        while (rest.isNotEmpty() && out.size < maxLines) {
            val n = p.breakText(rest, true, maxW, null)
            if (n <= 0) break
            if (n >= rest.length) {
                out.add(rest)
                break
            }
            if (out.size == maxLines - 1) {
                var k = n
                while (k > 0 && p.measureText(rest.substring(0, k) + "…") > maxW) k--
                out.add(rest.substring(0, k) + "…")
                break
            }
            out.add(rest.substring(0, n))
            rest = rest.substring(n)
        }
        return out
    }

    private fun bar(c: Canvas, x: Float, y: Float, w: Float, h: Float, ratio: Float, tint: Int) {
        if (w <= 0f || h <= 0f) return
        val rad = h / 2f
        val bg = Paint(Paint.ANTI_ALIAS_FLAG).apply { this.color = alpha(INK, 0.13f) }
        c.drawRoundRect(RectF(x, y, x + w, y + h), rad, rad, bg)
        val fw = (w * ratio.coerceIn(0f, 1f)).coerceAtLeast(if (ratio > 0f) h * 0.8f else 0f)
        if (fw <= 0f) return
        val fg = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            shader = LinearGradient(x, y, x + fw, y,
                alpha(tint, 0.55f), tint, Shader.TileMode.CLAMP)
        }
        c.drawRoundRect(RectF(x, y, x + fw, y + h), rad, rad, fg)
    }

    private fun ring(c: Canvas, cx: Float, cy: Float, r: Float, ratio: Float, tint: Int) {
        val sw = r * 0.20f
        val oval = RectF(cx - r, cy - r, cx + r, cy + r)
        val bg = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            style = Paint.Style.STROKE
            strokeWidth = sw
            this.color = alpha(INK, 0.13f)
        }
        c.drawArc(oval, 0f, 360f, false, bg)
        val fg = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            style = Paint.Style.STROKE
            strokeWidth = sw
            strokeCap = Paint.Cap.ROUND
            this.color = tint
            setShadowLayer(r * 0.14f, 0f, 0f, alpha(tint, 0.60f))
        }
        c.drawArc(oval, -90f, 360f * ratio.coerceIn(0f, 1f), false, fg)
    }

    private fun scoreColor(v: Float): Int = when {
        v >= 75f -> GREEN
        v >= 55f -> YELLOW
        else -> RED
    }

    private fun f0(v: Double): String = String.format(Locale.US, "%.0f", v)
    private fun f1(v: Double): String = String.format(Locale.US, "%.1f", v)

    private fun alpha(color: Int, a: Float): Int =
        Color.argb((a * 255).toInt().coerceIn(0, 255),
            Color.red(color), Color.green(color), Color.blue(color))

    private fun parseColor(s: String?, fallback: Int): Int = try {
        if (s.isNullOrBlank()) fallback else Color.parseColor(s)
    } catch (t: Throwable) {
        fallback
    }
}
