package com.garmincoach.app

import android.media.MediaCodec
import android.media.MediaExtractor
import android.media.MediaFormat
import android.media.MediaMuxer
import java.io.File
import java.nio.ByteBuffer

/**
 * 素材片段无损剪辑：只搬样本、不解码，几秒就能完成。
 *
 * 起点吸附到最近的关键帧（否则开头会花屏），所以实际起点可能比选定的早
 * 0.5~2 秒；对「素材点缀」够用，要精确到帧就得重编码（先不做）。
 */
object ClipTrimmer {

    data class Result(val ok: Boolean, val message: String,
                      val durMs: Long = 0, val width: Int = 0, val height: Int = 0)

    fun trim(src: File, dst: File, startMs: Long, endMs: Long): Result {
        if (!src.isFile) return Result(false, "源文件不存在")
        if (endMs <= startMs) return Result(false, "结束时间要大于开始时间")
        val ex = MediaExtractor()
        try {
            ex.setDataSource(src.absolutePath)
        } catch (t: Throwable) {
            return Result(false, "打不开视频：${t.message}")
        }
        dst.parentFile?.mkdirs()
        dst.delete()
        val mux = MediaMuxer(dst.absolutePath, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
        data class Tr(val src: Int, val out: Int, val video: Boolean)
        val list = mutableListOf<Tr>()
        var vw = 0
        var vh = 0
        var lastUs = -1L
        try {
            for (i in 0 until ex.trackCount) {
                val f = ex.getTrackFormat(i)
                val mime = f.getString(MediaFormat.KEY_MIME) ?: continue
                if (!mime.startsWith("video/") && !mime.startsWith("audio/")) continue
                val isVideo = mime.startsWith("video/")
                if (isVideo && vw == 0) {
                    vw = f.getInteger(MediaFormat.KEY_WIDTH)
                    vh = f.getInteger(MediaFormat.KEY_HEIGHT)
                }
                list.add(Tr(i, mux.addTrack(f), isVideo))
            }
            if (list.none { it.video }) return Result(false, "源文件里没有视频轨")
            mux.start()
            val buf = ByteBuffer.allocateDirect(8 * 1024 * 1024)
            val info = MediaCodec.BufferInfo()
            for (t in list) {
                ex.selectTrack(t.src)
                ex.seekTo(
                    startMs * 1000,
                    if (t.video) MediaExtractor.SEEK_TO_PREVIOUS_SYNC
                    else MediaExtractor.SEEK_TO_CLOSEST_SYNC
                )
                val base = ex.sampleTime
                if (base < 0) continue
                while (true) {
                    buf.clear()
                    val size = ex.readSampleData(buf, 0)
                    if (size < 0) break
                    val pts = ex.sampleTime
                    if (pts > endMs * 1000) break
                    info.offset = 0
                    info.size = size
                    info.presentationTimeUs = pts - base
                    info.flags = ex.sampleFlags
                    mux.writeSampleData(t.out, buf, info)
                    lastUs = info.presentationTimeUs
                    ex.advance()
                }
                ex.unselectTrack(t.src)
            }
            mux.stop()
            if (lastUs < 0) return Result(false, "选中的区间里没有可用的样本")
            return Result(true, "ok", lastUs / 1000, vw, vh)
        } catch (t: Throwable) {
            dst.delete()
            return Result(false, "${t.javaClass.simpleName}: ${t.message}")
        } finally {
            try { mux.release() } catch (_: Throwable) {}
            try { ex.release() } catch (_: Throwable) {}
        }
    }
}
