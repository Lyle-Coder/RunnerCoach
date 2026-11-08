package com.garmincoach.app

import android.graphics.Bitmap
import android.media.MediaMetadataRetriever
import java.io.File
import java.io.FileOutputStream
import kotlin.math.ceil
import kotlin.math.max
import kotlin.math.min

/**
 * 原生视频抽帧（v48）。
 *
 * 为什么不在 WebView 里抽：Android WebView 的 `drawImage(<video>)` 会**同步**
 * 等解码器出帧，主线程被它占住时 `setTimeout` 兜底根本跑不了。v46/v47 两版把
 * 「逐格随机 seek」改成「整段顺序预抽」都没绕开这一步 —— 真机上一段 6 秒素材
 * 仍要 5 分钟以上，最后被编码线程的「连续 5 分钟没有新帧」判死，提示
 * 「第 N 帧没送到（导出中断）」。原生 MediaMetadataRetriever 在同一台机器上
 * 快一两个数量级，且完全不占 WebView 主线程。
 *
 * 输出：`<dir>/c<ci>/<i>.jpg`（长边 <= longEdge 的缩略图）。抽完整段再写
 * 汇总的 `_clips.json`（由调用方写），页面据此按 `ci` 取图。
 */
object MotionClips {

    /** n = 实际抽到的帧数；fps = 实际格率（长素材会被 maxFrames 压低）。 */
    data class Info(val n: Int, val fps: Double, val w: Int, val h: Int)

    fun extract(src: String, outDir: File, targetFps: Int, maxFrames: Int,
                longEdge: Int,
                cancelled: () -> Boolean = { false }): Info {
        if (src.isBlank()) return Info(0, 0.0, 0, 0)
        val f = File(src)
        if (!f.isFile) return Info(0, 0.0, 0, 0)
        val mmr = MediaMetadataRetriever()
        try {
            mmr.setDataSource(src)
            val durMs = mmr.extractMetadata(
                MediaMetadataRetriever.METADATA_KEY_DURATION)?.toLongOrNull() ?: 0L
            if (durMs <= 0) return Info(0, 0.0, 0, 0)
            val w0 = mmr.extractMetadata(
                MediaMetadataRetriever.METADATA_KEY_VIDEO_WIDTH)?.toIntOrNull() ?: 0
            val h0 = mmr.extractMetadata(
                MediaMetadataRetriever.METADATA_KEY_VIDEO_HEIGHT)?.toIntOrNull() ?: 0
            val rot = mmr.extractMetadata(
                MediaMetadataRetriever.METADATA_KEY_VIDEO_ROTATION)?.toIntOrNull() ?: 0
            // 竖拍视频常常是「宽>高 + rotation=90」，要按旋转后的画幅算
            val swap = rot == 90 || rot == 270
            val cw = if (swap) h0 else w0
            val ch = if (swap) w0 else h0
            val (tw, th) = targetSize(cw, ch, longEdge)
            val n = max(1, min(maxFrames,
                ceil(durMs / 1000.0 * targetFps).toInt()))
            val fps = n * 1000.0 / durMs
            outDir.mkdirs()
            var done = 0
            var outW = tw
            var outH = th
            for (i in 0 until n) {
                if (cancelled()) break
                val us = (i * 1_000_000L / max(0.001, fps)).toLong()
                val bmp = frame(mmr, us, tw, th) ?: continue
                if (i == 0) { outW = bmp.width; outH = bmp.height }
                try {
                    FileOutputStream(File(outDir, "$i.jpg")).use {
                        bmp.compress(Bitmap.CompressFormat.JPEG, 82, it)
                    }
                    done++
                } finally {
                    bmp.recycle()
                }
            }
            return if (done == 0) Info(0, 0.0, 0, 0)
                   else Info(done, fps, outW, outH)
        } catch (t: Throwable) {
            return Info(0, 0.0, 0, 0)
        } finally {
            try { mmr.release() } catch (_: Throwable) {}
        }
    }

    /** 卡片显示上限 840×780，预抽长边 480 足够；读不到尺寸就按竖屏兜底。 */
    private fun targetSize(cw: Int, ch: Int, longEdge: Int): Pair<Int, Int> {
        val w = if (cw > 0) cw else 480
        val h = if (ch > 0) ch else 854
        val s = min(1.0, longEdge.toDouble() / max(w, h))
        return Pair(max(2, (w * s).toInt() / 2 * 2),
                    max(2, (h * s).toInt() / 2 * 2))
    }

    /**
     * 精确帧优先（OPTION_CLOSEST），个别编码器不认就退回最近关键帧 ——
     * 画面会跳一点，但这段素材仍在播，比整段出不了片强。
     */
    private fun frame(mmr: MediaMetadataRetriever, us: Long,
                      tw: Int, th: Int): Bitmap? {
        try {
            mmr.getScaledFrameAtTime(us, MediaMetadataRetriever.OPTION_CLOSEST, tw, th)
        } catch (_: Throwable) {
            null
        }?.let { return it }
        return try {
            mmr.getScaledFrameAtTime(
                us, MediaMetadataRetriever.OPTION_CLOSEST_SYNC, tw, th)
        } catch (_: Throwable) {
            null
        }
    }
}
