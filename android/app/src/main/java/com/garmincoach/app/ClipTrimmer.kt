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
 * 0.5~2 秒。裁剪界面（ClipTrimDialog）会把关键帧画出来、手柄直接吸附到
 * 关键帧上，所以选中的那个点就是真正落刀的点。
 *
 * 所有轨道共用同一个时间原点：早期版本让每条轨各自 seek 各自算 base，
 * 视频对齐到「前一个关键帧」、音频对齐到「最近的可解码点」，两者差个零点
 * 几秒就会音画错位。现在统一到视频关键帧，音画才是同步的。
 */
object ClipTrimmer {

    data class Result(val ok: Boolean, val message: String,
                      val durMs: Long = 0, val width: Int = 0, val height: Int = 0,
                      /** 实际落刀点（毫秒）。吸附到关键帧时可能早于请求的起点。 */
                      val startMs: Long = 0)

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

            // 时间原点取视频轨里 startMs 之前最近的关键帧。所有轨都按它对齐，
            // 音画才不会各走各的。
            val videoTr = list.first { it.video }
            ex.selectTrack(videoTr.src)
            ex.seekTo(startMs * 1000, MediaExtractor.SEEK_TO_PREVIOUS_SYNC)
            val baseUs = if (ex.sampleTime >= 0) ex.sampleTime else startMs * 1000
            ex.unselectTrack(videoTr.src)

            mux.start()
            val buf = ByteBuffer.allocateDirect(8 * 1024 * 1024)
            val info = MediaCodec.BufferInfo()
            for (t in list) {
                ex.selectTrack(t.src)
                // 音频用最近点即可：它的样本时间会再按 baseUs 统一减掉
                ex.seekTo(
                    baseUs,
                    if (t.video) MediaExtractor.SEEK_TO_PREVIOUS_SYNC
                    else MediaExtractor.SEEK_TO_CLOSEST_SYNC
                )
                while (true) {
                    buf.clear()
                    val size = ex.readSampleData(buf, 0)
                    if (size < 0) break
                    val pts = ex.sampleTime
                    if (pts > endMs * 1000) break
                    // 原点之前的一点点样本丢掉：减去 baseUs 会得到负时间戳
                    if (pts >= baseUs) {
                        info.offset = 0
                        info.size = size
                        info.presentationTimeUs = pts - baseUs
                        info.flags = ex.sampleFlags
                        mux.writeSampleData(t.out, buf, info)
                        if (info.presentationTimeUs > lastUs) lastUs = info.presentationTimeUs
                    }
                    ex.advance()
                }
                ex.unselectTrack(t.src)
            }
            mux.stop()
            if (lastUs < 0) return Result(false, "选中的区间里没有可用的样本")
            return Result(true, "ok", lastUs / 1000, vw, vh, baseUs / 1000)
        } catch (t: Throwable) {
            dst.delete()
            return Result(false, "${t.javaClass.simpleName}: ${t.message}")
        } finally {
            try { mux.release() } catch (_: Throwable) {}
            try { ex.release() } catch (_: Throwable) {}
        }
    }
}
