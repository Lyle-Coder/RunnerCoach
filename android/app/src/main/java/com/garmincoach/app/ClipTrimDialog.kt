package com.garmincoach.app

import android.app.Activity
import android.app.Dialog
import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Rect
import android.graphics.RectF
import android.graphics.SurfaceTexture
import android.media.AudioAttributes
import android.media.MediaExtractor
import android.media.MediaFormat
import android.media.MediaMetadataRetriever
import android.media.MediaPlayer
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.Gravity
import android.view.MotionEvent
import android.view.Surface
import android.view.TextureView
import android.view.View
import android.view.ViewGroup
import android.view.Window
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import java.io.File
import java.nio.ByteBuffer
import kotlin.concurrent.thread
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

private const val TAG = "ClipTrim"
private const val STRIP_THUMBS = 32      // 胶片条最多取多少张缩略图
private const val MIN_SEL_MS = 300L      // 选区最短多少毫秒

/**
 * 视频轨关键帧（同步帧 / I 帧）扫描。
 *
 * 为什么必须扫：素材剪辑走 MediaExtractor 搬样本，起点只能落在关键帧上
 * ——从中间切开的话，后面那些 P/B 帧没有参考帧，出来就是花屏。
 * 所以「能选的位置」= 关键帧的位置。界面上必须把这些点画出来，
 * 否则用户拖出来的时间和剪出来的内容对不上。
 */
object KeyFrames {

    /** 返回视频轨所有关键帧的时间戳（毫秒，升序）。扫不出来就返回空数组。 */
    fun scan(path: String): LongArray {
        val ex = MediaExtractor()
        val out = ArrayList<Long>()
        try {
            ex.setDataSource(path)
            var vt = -1
            for (i in 0 until ex.trackCount) {
                val f = ex.getTrackFormat(i)
                val mime = f.getString(MediaFormat.KEY_MIME) ?: continue
                if (mime.startsWith("video/")) { vt = i; break }
            }
            if (vt < 0) return LongArray(0)
            ex.selectTrack(vt)
            val buf = ByteBuffer.allocateDirect(64 * 1024)
            while (true) {
                val size = ex.readSampleData(buf, 0)
                if (size < 0) break
                if (ex.sampleFlags and MediaExtractor.SAMPLE_FLAG_SYNC != 0) {
                    out.add(ex.sampleTime / 1000)
                }
                ex.advance()
            }
        } catch (t: Throwable) {
            Log.w(TAG, "scan keyframes", t)
            out.clear()
        } finally {
            try { ex.release() } catch (_: Throwable) {}
        }
        return out.toLongArray()
    }
}

/**
 * 关键帧胶片条。
 *
 * 三层信息叠在一起：
 *   1. 底：按关键帧取的缩略图，一眼看出每段画面是什么；
 *   2. 中：选中区间高亮、区外压暗；
 *   3. 上：关键帧刻度 + 可拖的两端手柄 + 播放头。
 *
 * 手柄吸附到关键帧：落刀只能在关键帧上，手柄停在别的地方等于骗人。
 * 吸附关掉也能拖，但界面上会把实际对齐到的时间显示出来。
 */
private class TrimBar(ctx: Context) : View(ctx) {

    var durationMs = 1L
    var thumbs: List<Bitmap> = emptyList()
    var thumbTimes: List<Long> = emptyList()   // 与 thumbs 一一对应
    var keys: List<Long> = emptyList()         // 全部关键帧
    var snapEnabled = true
    var startMs = 0L
    var endMs = 0L
    var playheadMs = 0L
    var onRange: ((Long, Long) -> Unit)? = null

    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val paintLine = Paint(Paint.ANTI_ALIAS_FLAG).apply { strokeWidth = 2f }
    private var mode = 0                       // 0 无 / 1 起点 / 2 终点 / 3 整体平移
    private var dragStart = 0L
    private var dragEnd = 0L
    private var anchorTime = 0L

    private val accent = 0xFF17845F.toInt()
    private val dim = 0x99000000.toInt()

    private fun dp(v: Int) = (v * resources.displayMetrics.density).roundToInt()
    private fun dp(v: Float) = v * resources.displayMetrics.density

    private fun xOf(t: Long): Float {
        if (durationMs <= 0) return 0f
        return (t.toFloat() / durationMs) * width
    }

    private fun tOf(x: Float): Long {
        if (width <= 0) return 0L
        return ((x / width) * durationMs).toLong().coerceIn(0L, durationMs)
    }

    /** 最近的���键帧；没有关键帧或关了吸附就原样返回。 */
    private fun snap(t: Long): Long {
        if (!snapEnabled || keys.isEmpty()) return t
        var best = keys[0]
        var bd = Long.MAX_VALUE
        for (k in keys) {
            val d = abs(k - t)
            if (d < bd) { bd = d; best = k }
        }
        return best
    }

    /** 不大于 t 的最后一个关键帧；没有就返回 -1。 */
    private fun keyAtOrBefore(t: Long): Long {
        var best = -1L
        for (k in keys) {
            if (k <= t) best = k else break
        }
        return best
    }

    override fun onDraw(c: Canvas) {
        val w = width.toFloat()
        val h = height.toFloat()
        if (w <= 0 || h <= 0) return

        // ---- 底：缩略图
        paint.color = 0xFF2A2F2B.toInt()
        c.drawRect(0f, 0f, w, h, paint)
        if (thumbs.isNotEmpty()) {
            for (i in thumbs.indices) {
                val t0 = thumbTimes[i]
                val t1 = if (i + 1 < thumbTimes.size) thumbTimes[i + 1] else durationMs
                val x0 = xOf(t0)
                val x1 = max(xOf(t1), x0 + 1f)
                drawCropped(c, thumbs[i], RectF(x0, 0f, x1, h))
            }
        }

        // ---- 区外压暗
        val sx = xOf(startMs)
        val ex = xOf(endMs)
        paint.color = dim
        c.drawRect(0f, 0f, sx, h, paint)
        c.drawRect(ex, 0f, w, h, paint)

        // ---- 关键帧刻度
        paint.color = 0x66FFFFFF
        for (k in keys) {
            val x = xOf(k)
            c.drawRect(x, h - dp(7).toFloat(), x + dp(1.5f), h, paint)
        }

        // ---- 选中区边框
        paintLine.color = accent
        paintLine.strokeWidth = dp(2f).toFloat()
        c.drawRect(sx, 0f, ex, h, paintLine)

        // ---- 手柄
        paint.color = accent
        c.drawRect(sx - dp(2.5f), 0f, sx + dp(2.5f), h, paint)
        c.drawRect(ex - dp(2.5f), 0f, ex + dp(2.5f), h, paint)
        paint.color = 0xFFFFFFFF.toInt()
        c.drawRect(sx - dp(1f), dp(6f), sx + dp(1f), h - dp(6f), paint)
        c.drawRect(ex - dp(1f), dp(6f), ex + dp(1f), h - dp(6f), paint)

        // ---- 播放头
        val px = xOf(playheadMs)
        paint.color = 0xFFFF6B35.toInt()
        c.drawRect(px - dp(1.5f), 0f, px + dp(1.5f), h, paint)
    }

    /** 居中裁剪填充：缩略图比例和槽位对不上时切掉多余的那一边。 */
    private fun drawCropped(c: Canvas, bmp: Bitmap, dst: RectF) {
        if (bmp.width <= 0 || bmp.height <= 0 || dst.width() <= 0) return
        val ta = dst.width() / dst.height()
        val sa = bmp.width.toFloat() / bmp.height
        val src = Rect()
        if (sa > ta) {
            val w = bmp.height * ta
            src.set(((bmp.width - w) / 2).roundToInt(), 0,
                    ((bmp.width + w) / 2).roundToInt(), bmp.height)
        } else {
            val hh = bmp.width / ta
            src.set(0, ((bmp.height - hh) / 2).roundToInt(),
                    bmp.width, ((bmp.height + hh) / 2).roundToInt())
        }
        try {
            c.drawBitmap(bmp, src, dst, paint)
        } catch (t: Throwable) {
            Log.w(TAG, "draw thumb", t)
        }
    }

    override fun onTouchEvent(e: MotionEvent): Boolean {
        when (e.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                val sx = xOf(startMs)
                val ex = xOf(endMs)
                val hit = dp(24).toFloat()
                mode = when {
                    abs(e.x - sx) <= hit -> 1
                    abs(e.x - ex) <= hit -> 2
                    e.x > sx && e.x < ex -> 3
                    else -> if (abs(e.x - sx) <= abs(e.x - ex)) 1 else 2
                }
                dragStart = startMs
                dragEnd = endMs
                anchorTime = tOf(e.x)
                parent?.requestDisallowInterceptTouchEvent(true)
                applyDrag(e.x)
                return true
            }
            MotionEvent.ACTION_MOVE -> {
                applyDrag(e.x)
                return true
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                mode = 0
                parent?.requestDisallowInterceptTouchEvent(false)
                performClick()
                return true
            }
        }
        return super.onTouchEvent(e)
    }

    override fun performClick(): Boolean {
        super.performClick()
        return true
    }

    /**
     * 两端语义不同，不能一视同仁：
     *
     * 起点**必须**落在关键帧上——从 GOP 中间切过去，前几帧缺参考帧就是花屏。
     * 终点随意：末尾帧的参考帧都在包里，照样能解码。
     *
     * 所以起点吸附关键帧，终点自由，但胶片条上把所有关键帧都画了刻度，
     * 想切在哪儿心里有数。
     */
    private fun applyDrag(x: Float) {
        when (mode) {
            1 -> {
                var t = snap(tOf(x))
                if (t > endMs - MIN_SEL_MS) {
                    endMs = min(durationMs, t + MIN_SEL_MS)
                    t = max(0L, endMs - MIN_SEL_MS)
                }
                startMs = t
            }
            2 -> {
                var t = tOf(x)
                if (t < startMs + MIN_SEL_MS) {
                    // 终点顶到起点上了：把起点往后拉回关键帧，别让它掉出关键帧
                    val pk = keyAtOrBefore(t - MIN_SEL_MS)
                    startMs = if (pk >= 0) pk else max(0L, t - MIN_SEL_MS)
                    t = max(t, startMs + MIN_SEL_MS)
                }
                endMs = t
            }
            3 -> {
                // 整体平移：起点重新吸附关键帧，终点跟着平移
                val delta = tOf(x) - anchorTime
                var s = snap(dragStart + delta)
                if (s < 0) s = 0
                var en = dragEnd + delta
                if (en > durationMs) { en = durationMs }
                if (s > en - MIN_SEL_MS) s = max(0L, en - MIN_SEL_MS)
                startMs = s
                endMs = en
            }
        }
        invalidate()
        onRange?.invoke(startMs, endMs)
    }
}

/**
 * 素材裁剪对话框：视频预览 + 关键帧胶片条 + 两端手柄。
 *
 * 之前只有两个裸 SeekBar，用户无从知道第 3 秒拍的是什么，选出来才发现选错了。
 * 而且 ClipTrimmer 起点会吸附到上一个关键帧，「选的」和「剪出来的」不是同一段。
 * 这版把关键帧全部画出来、手柄强制吸附关键帧，做到所见即所得。
 */
class ClipTrimDialog(
    private val act: Activity,
    private val src: File,
    private val durMs: Long,
    private val onPick: (Long, Long, Int) -> Unit,
    /** 用户取消时调用：源文件是从相册拷到 cacheDir 的临时文件，得清掉。 */
    private val onCancel: () -> Unit = {},
    /** 已记录的显示旋转，进界面时先按它预览。 */
    private val rotate0: Int = 0,
) {

    private val main = Handler(Looper.getMainLooper())
    private val density = act.resources.displayMetrics.density

    private fun dp(v: Int) = (v * density).roundToInt()

    /** 视频显示宽高（已按 tkhd rotation 交换），读不到就给竖屏兜底。 */
    private fun readSize(): Pair<Int, Int> {
        val mmr = MediaMetadataRetriever()
        return try {
            mmr.setDataSource(src.absolutePath)
            val w = mmr.extractMetadata(
                MediaMetadataRetriever.METADATA_KEY_VIDEO_WIDTH)?.toIntOrNull() ?: 0
            val h = mmr.extractMetadata(
                MediaMetadataRetriever.METADATA_KEY_VIDEO_HEIGHT)?.toIntOrNull() ?: 0
            val r = mmr.extractMetadata(
                MediaMetadataRetriever.METADATA_KEY_VIDEO_ROTATION)?.toIntOrNull() ?: 0
            val sw = if (r == 90 || r == 270) h else w
            val sh = if (r == 90 || r == 270) w else h
            if (sw > 0 && sh > 0) sw to sh else 1080 to 1920
        } catch (t: Throwable) {
            1080 to 1920
        } finally {
            try { mmr.release() } catch (_: Throwable) {}
        }
    }

    private var keys: List<Long> = emptyList()
    private var thumbs: List<Bitmap> = emptyList()
    private var thumbTimes: List<Long> = emptyList()

    private var startMs = 0L
    private var endMs = durMs

    private var player: MediaPlayer? = null
    private var surface: Surface? = null
    private var prepared = false
    private var playing = false
    /** 当前显示旋转（0/90/180/270）。无损剪辑不转码，角度回传给登记端。 */
    private var curRotate = ((rotate0 % 360) + 360) % 360
    /** 已经点了确定：这时不能再删源文件，saveClip 还要拿它去剪。 */
    private var picked = false

    private lateinit var dlg: Dialog
    private lateinit var bar: TrimBar
    private lateinit var info: TextView
    private lateinit var hint: TextView
    private lateinit var still: ImageView
    private lateinit var playBtn: TextView
    private lateinit var loading: ProgressBar

    companion object {
        fun show(act: Activity, src: File, durMs: Long, rotate0: Int = 0,
                 onPick: (Long, Long, Int) -> Unit, onCancel: () -> Unit = {}) {
            ClipTrimDialog(act, src, durMs, onPick, onCancel, rotate0).show()
        }
    }

    fun show() {
        val root = LinearLayout(act).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(12), dp(16), dp(8))
        }

        // ---- 预览区
        // 按素材真实宽高比摆：竖屏给竖向的框，横屏给横向的。旧版固定 200dp 高
        // + MATCH_PARENT 宽 + CENTER_CROP，竖拍视频会被裁成一截横条。
        val (vw, vh) = readSize()
        val maxW = (act.resources.displayMetrics.widthPixels - dp(32)).toFloat()
        val maxH = dp(if (vh > vw) 300 else 200).toFloat()
        var fw = maxW
        var fh = fw * vh.toFloat() / max(1, vw)
        if (fh > maxH) {
            fh = maxH
            fw = fh * vw.toFloat() / max(1, vh)
        }
        val frame = FrameLayout(act).apply {
            setBackgroundColor(0xFF12161B.toInt())
        }
        val tex = TextureView(act)
        frame.addView(tex, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))

        still = ImageView(act).apply {
            scaleType = ImageView.ScaleType.FIT_CENTER
            visibility = View.GONE
        }
        frame.addView(still, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))

        loading = ProgressBar(act)
        frame.addView(loading, FrameLayout.LayoutParams(dp(36), dp(36), Gravity.CENTER))

        playBtn = TextView(act).apply {
            text = "▶ 播放选段"
            textSize = 12.5f
            setTextColor(0xFFFFFFFF.toInt())
            setPadding(dp(10), dp(5), dp(10), dp(5))
            setBackgroundColor(0x99000000.toInt())
            setOnClickListener { togglePlay() }
        }
        frame.addView(playBtn, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.BOTTOM or Gravity.START))

        // 旋转：无损剪辑不转码，角度随素材一起登记，回放绘制/抽帧时兑现。
        val rotBtn = TextView(act).apply {
            text = if (curRotate == 0) "⟳ 旋转" else "⟳ ${curRotate}°"
            textSize = 12.5f
            setTextColor(0xFFFFFFFF.toInt())
            setPadding(dp(10), dp(5), dp(10), dp(5))
            setBackgroundColor(0x99000000.toInt())
            setOnClickListener {
                curRotate = (curRotate + 90) % 360
                tex.rotation = curRotate.toFloat()
                still.rotation = curRotate.toFloat()
                text = "⟳ ${curRotate}°"
            }
        }
        frame.addView(rotBtn, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT,
            Gravity.TOP or Gravity.END))

        root.addView(frame, LinearLayout.LayoutParams(
            fw.roundToInt(), fh.roundToInt()).apply {
            gravity = Gravity.CENTER_HORIZONTAL
        })

        // ---- 选区信息
        info = TextView(act).apply {
            textSize = 13f
            setTextColor(0xFF1A1F1B.toInt())
            setPadding(0, dp(10), 0, dp(2))
        }
        root.addView(info)

        // ---- 胶片条
        bar = TrimBar(act).apply {
            durationMs = max(1L, durMs)
            startMs = 0L
            endMs = this@ClipTrimDialog.durMs
        }
        root.addView(bar, LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, dp(74)))

        hint = TextView(act).apply {
            textSize = 11.5f
            setTextColor(0xFF78806F.toInt())
            setPadding(0, dp(6), 0, 0)
            text = "正在读取关键帧…"
        }
        root.addView(hint)

        // ---- 底部按钮
        val row = LinearLayout(act).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.END
            setPadding(0, dp(8), 0, 0)
        }
        val cancel = TextView(act).apply {
            text = "取消"
            textSize = 14f
            setTextColor(0xFF485046.toInt())
            setPadding(dp(16), dp(10), dp(16), dp(10))
            setOnClickListener { dlg.dismiss() }
        }
        val ok = TextView(act).apply {
            text = "确定"
            textSize = 14f
            setTextColor(0xFF17845F.toInt())
            setPadding(dp(16), dp(10), dp(16), dp(10))
            setOnClickListener { confirm() }
        }
        row.addView(cancel)
        row.addView(ok)
        root.addView(row)

        dlg = Dialog(act)
        dlg.requestWindowFeature(Window.FEATURE_NO_TITLE)
        dlg.setContentView(root)
        dlg.setOnDismissListener {
            main.removeCallbacks(ticker)
            player?.setOnErrorListener(null)
            try { player?.setSurface(null) } catch (_: Throwable) {}
            try { player?.release() } catch (_: Throwable) {}
            player = null
            try { surface?.release() } catch (_: Throwable) {}
            surface = null
            if (!picked) onCancel()
        }

        bar.onRange = { s, e ->
            startMs = s; endMs = e
            updateInfo()
            seekPreview(s)
        }

        updateInfo()
        dlg.show()
        dlg.window?.let { w ->
            w.setLayout((act.resources.displayMetrics.widthPixels * 0.94f).roundToInt(),
                ViewGroup.LayoutParams.WRAP_CONTENT)
            w.setBackgroundDrawable(android.graphics.drawable.ColorDrawable(0xFFF6F7F4.toInt()))
        }

        setupPlayer(tex)
        loadFrames()
    }

    // ------------------------------------------------------------------ 关键帧 + 缩略图
    private fun loadFrames() {
        thread(name = "clip-trim-scan") {
            val ks = KeyFrames.scan(src.absolutePath)
            val picked = pickSampleTimes(ks, durMs)
            val (bmps, tms) = grabThumbs(picked)
            main.post {
                keys = ks.toList()
                thumbs = bmps
                thumbTimes = tms
                bar.keys = keys
                bar.thumbs = thumbs
                bar.thumbTimes = thumbTimes
                bar.snapEnabled = keys.isNotEmpty()
                bar.invalidate()
                loading.visibility = View.GONE
                hint.text = if (keys.isEmpty()) {
                    "没扫到关键帧，将按整帧裁剪；预览可能与结果略有出入。"
                } else {
                    "共 ${keys.size} 个关键帧 · 拖两端手柄选片段（自动吸附关键帧）· 点画面播放选段"
                }
                updateInfo()
                seekPreview(startMs)
            }
        }
    }

    /**
     * 关键帧太多时按均匀间隔抽样，避免胶片条塞满。
     * 但抽出来的仍然是真关键帧，所以手柄吸附和缩略图始终对得上。
     */
    private fun pickSampleTimes(ks: LongArray, dur: Long): LongArray {
        if (ks.isEmpty()) {
            // 一个关键帧都没有：退化成等距取样，至少还能看画面
            val n = min(12, max(4, (dur / 1000).toInt()))
            return LongArray(n) { dur * it / n }
        }
        if (ks.size <= STRIP_THUMBS) return ks
        val out = ArrayList<Long>(STRIP_THUMBS)
        val step = ks.size.toFloat() / STRIP_THUMBS
        for (i in 0 until STRIP_THUMBS) {
            out.add(ks[(i * step).roundToInt().coerceIn(0, ks.size - 1)])
        }
        return out.toLongArray()
    }

    private fun grabThumbs(times: LongArray): Pair<List<Bitmap>, List<Long>> {
        val mr = MediaMetadataRetriever()
        val bs = ArrayList<Bitmap>()
        val ts = ArrayList<Long>()
        try {
            mr.setDataSource(src.absolutePath)
            for (t in times) {
                val b = try {
                    mr.getScaledFrameAtTime(t * 1000, MediaMetadataRetriever.OPTION_CLOSEST_SYNC, 256, 256)
                } catch (e: Throwable) {
                    Log.w(TAG, "thumb $t", e); null
                }
                if (b != null) { bs.add(b); ts.add(t) }
            }
        } catch (t: Throwable) {
            Log.w(TAG, "grab thumbs", t)
        } finally {
            try { mr.release() } catch (_: Throwable) {}
        }
        return bs to ts
    }

    // ------------------------------------------------------------------ 播放器
    private fun setupPlayer(tex: TextureView) {
        tex.surfaceTextureListener = object : TextureView.SurfaceTextureListener {
            override fun onSurfaceTextureAvailable(st: SurfaceTexture, w: Int, h: Int) {
                surface = Surface(st)
                try {
                    val mp = MediaPlayer()
                    mp.setAudioAttributes(AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_MOVIE)
                        .build())
                    mp.setSurface(surface)
                    mp.setDataSource(src.absolutePath)
                    mp.setOnPreparedListener {
                        prepared = true
                        hideStill()
                        mp.seekTo(startMs, MediaPlayer.SEEK_CLOSEST)
                        main.post { updateInfo() }
                    }
                    mp.setOnErrorListener { _, _, _ ->
                        // 播放不了（HEVC 之类）：退化成静帧预览，裁剪本身照样能做
                        prepared = false
                        main.post {
                            playBtn.text = "▶ 播放选段"
                            playing = false
                            showStill(startMs)
                            hint.text = "这个视频没法在手机上预览（编码不支持），" +
                                "已改为显示关键帧静帧；裁剪结果不受影响。"
                        }
                        true
                    }
                    mp.prepareAsync()
                    player = mp
                } catch (t: Throwable) {
                    Log.w(TAG, "player", t)
                    main.post { showStill(startMs) }
                }
            }

            override fun onSurfaceTextureSizeChanged(st: SurfaceTexture, w: Int, h: Int) {}
            override fun onSurfaceTextureDestroyed(st: SurfaceTexture): Boolean = true
            override fun onSurfaceTextureUpdated(st: SurfaceTexture) {}
        }
    }

    private fun seekPreview(ms: Long) {
        bar.playheadMs = ms
        bar.invalidate()
        if (!prepared) { showStill(ms); return }
        // 播放器能用了就把静帧层收起来，否则它会盖住正在播的画面
        hideStill()
        try {
            player?.seekTo(ms, MediaPlayer.SEEK_CLOSEST)
        } catch (t: Throwable) {
            Log.w(TAG, "seek", t)
        }
    }

    /** 播放器彻底用不了时才退回静帧预览（HEVC 之类）。 */
    private fun showStill(ms: Long) {
        if (thumbs.isEmpty()) return
        var bi = 0
        var bd = Long.MAX_VALUE
        for (i in thumbTimes.indices) {
            val d = abs(thumbTimes[i] - ms)
            if (d < bd) { bd = d; bi = i }
        }
        still.setImageBitmap(thumbs[bi])
        still.visibility = View.VISIBLE
    }

    private fun hideStill() {
        still.visibility = View.GONE
    }

    private fun togglePlay() {
        val mp = player
        if (!prepared || mp == null) {
            toast("这段视频暂时放不了")
            return
        }
        if (playing) {
            mp.pause()
            playing = false
            playBtn.text = "▶ 播放选段"
            return
        }
        try {
            val cur = mp.currentPosition.toLong()
            if (cur < startMs || cur >= endMs) mp.seekTo(startMs, MediaPlayer.SEEK_CLOSEST)
            mp.start()
            playing = true
            playBtn.text = "⏸ 暂停"
            main.removeCallbacks(ticker)
            main.post(ticker)
        } catch (t: Throwable) {
            Log.w(TAG, "play", t)
        }
    }

    private val ticker = object : Runnable {
        override fun run() {
            val mp = player
            if (mp != null && playing) {
                var cur = mp.currentPosition.toLong()
                if (cur >= endMs) {
                    // 选段放完就回到起点循环，停在原地更符合「反复看这一段」的用法
                    try {
                        mp.seekTo(startMs, MediaPlayer.SEEK_CLOSEST)
                        mp.start()
                    } catch (t: Throwable) {
                        Log.w(TAG, "loop", t)
                    }
                    cur = startMs
                }
                bar.playheadMs = cur
                bar.invalidate()
            }
            main.postDelayed(this, 80)
        }
    }

    // ------------------------------------------------------------------ 收尾
    private fun updateInfo() {
        val len = endMs - startMs
        val txt = String.format("起点 %.1f 秒 → 终点 %.1f 秒　共 %.1f 秒",
            startMs / 1000.0, endMs / 1000.0, len / 1000.0)
        val extra = if (keys.isEmpty()) "" else {
            val ks = keys.filter { it in startMs..endMs }
            "　区间内 $ks.size 个关键帧"
        }
        info.text = txt + extra
    }

    private fun confirm() {
        if (endMs - startMs < MIN_SEL_MS) {
            toast("片段太短了")
            return
        }
        picked = true
        onPick(startMs, endMs, curRotate)
        dlg.dismiss()
    }

    private fun toast(msg: String) {
        android.widget.Toast.makeText(act, msg, android.widget.Toast.LENGTH_SHORT).show()
    }
}
