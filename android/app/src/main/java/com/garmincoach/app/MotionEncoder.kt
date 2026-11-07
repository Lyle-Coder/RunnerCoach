package com.garmincoach.app

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.Image
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaMuxer
import java.io.File
import java.nio.ByteBuffer

/**
 * 把动画页逐帧推上来的 JPEG 编码成 H.264 MP4，并混入素材原声。
 *
 * 为什么不用 ffmpeg：APK 里塞一个要 +20MB、还得按 ABI 分发；Android 自带的
 * 硬编码器足够出 1080p。输入是 `motion.py` 的 `?export=1` 逐帧 POST 到
 * `/__frame` 的 JPEG 序列，这里逐张解码成 Bitmap、转 YUV420、喂给 MediaCodec。
 * 音轨由 `MotionAudio` 事先解码混合并压成 AAC，封装时按时间戳与视频交织写入。
 *
 * 1080×1920 不是 16 的倍数，个别编码器会拒绝或出绿边，所以失败自动降到
 * 720×1280 再来一次（对分享来说够用）。
 *
 * 性能（v42）
 * ----------
 * 旧版是「等 720 帧全部写盘 → 才开始编码」两段串行，编码侧还要对每帧做
 * 200 万次逐像素 ByteBuffer.put。现在：
 * 1. `frameAt` 惰性取帧，WebView 还在推第 N 帧时这里已经在编第 N-k 帧；
 * 2. YUV 转换改查表 + 按行批量 put，并复用像素/行缓冲，省掉每帧 8MB 分配；
 * 3. 输入缓冲暂时拿不到时重试，不再 `continue` 丢帧（旧版会悄悄少帧、时长变短）。
 */
object MotionEncoder {

    data class Result(val ok: Boolean, val message: String,
                      val width: Int = 0, val height: Int = 0,
                      val audioSegments: Int = 0)

    /** 惰性帧源：给第 i 帧的文件；返回 null 表示导出被取消或等不到帧。 */
    fun interface FrameAt {
        fun get(index: Int): File?
    }

    /** 每帧复用的一块临时内存，避免 1080×1920 × 720 次的 8MB IntArray 分配。 */
    private class Scratch(w: Int, h: Int) {
        val argb = IntArray(w * h)
        val y = ByteArray(w)
        val u = ByteArray(w / 2)
        val v = ByteArray(w / 2)
        val uv = ByteArray(w)
    }

    fun encode(framesDir: File, outFile: File, width: Int, height: Int, fps: Int,
               segments: List<MotionAudio.Segment> = emptyList(),
               frameCount: Int = 0,
               frameAt: FrameAt? = null,
               cancelled: () -> Boolean = { false },
               onProgress: ((Int) -> Unit)? = null): Result {
        val listed = if (frameAt == null) {
            framesDir.listFiles { f ->
                f.isFile && f.name.startsWith("f_") && f.name.endsWith(".jpg")
            }?.sortedBy { it.name } ?: emptyList()
        } else {
            emptyList()
        }
        val count = if (frameAt != null) frameCount else listed.size
        if (count <= 0) return Result(false, "没有收到任何帧")
        val source: FrameAt = frameAt ?: FrameAt { i -> listed.getOrNull(i) }
        // 音频只解码一次：1080p 失败降到 720p 时复用同一批 AAC 样本即可。
        // 任何一步失败都退化成无声，绝不让一段坏音频拖垮整次导出。
        val totalUs = count.toLong() * 1_000_000L / maxOf(1, fps)
        val audio = try {
            MotionAudio.build(segments, totalUs)
        } catch (t: Throwable) {
            null
        }
        var last = "未知错误"
        for ((w, h) in listOf(width to height, 720 to 1280).distinct()) {
            val r = try {
                encodeWith(count, source, outFile, w, h, fps, audio,
                    cancelled, onProgress)
            } catch (t: Throwable) {
                outFile.delete()
                Result(false, "${t.javaClass.simpleName}: ${t.message}")
            }
            if (r.ok) return r
            // 被取消或帧没送齐，降分辨率重试没有意义，反而多编一遍
            if (cancelled() || r.message.contains("没送到") || r.message.contains("取消")) {
                return r
            }
            last = r.message
        }
        return Result(false, last)
    }

    private fun encodeWith(count: Int, source: FrameAt, outFile: File,
                           width: Int, height: Int, fps: Int,
                           audio: MotionAudio.Track?, cancelled: () -> Boolean,
                           onProgress: ((Int) -> Unit)?): Result {
        val format = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, width, height)
        format.setInteger(MediaFormat.KEY_COLOR_FORMAT,
            MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible)
        format.setInteger(MediaFormat.KEY_BIT_RATE, if (width >= 1000) 8_000_000 else 4_000_000)
        format.setInteger(MediaFormat.KEY_FRAME_RATE, fps)
        format.setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1)

        val codec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
        outFile.parentFile?.mkdirs()
        outFile.delete()
        val muxer = MediaMuxer(outFile.absolutePath, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
        val info = MediaCodec.BufferInfo()
        var track = -1
        var started = false
        // MediaMuxer 必须在 start() 之前把轨道全加好；视频的 csd 要等编码器跑起来
        // 才知道，所以先把已经压好的 AAC 轨加进去。
        var audioTrack = -1
        if (audio != null) {
            // AAC 的 outputFormat 偶尔缺 csd：加轨失败就当没声音，视频照出
            audioTrack = try {
                muxer.addTrack(audio.format)
            } catch (t: Throwable) {
                -1
            }
        }
        var nextSample = 0

        // 按视频时间戳交织音频：先写视频后写音频虽然也能播，但部分播放器对
        // 非交织 MP4 会拖到整片缓冲完才出声，分享出去表现很差。
        fun writeAudio(untilUs: Long, flushAll: Boolean) {
            if (audio == null || audioTrack < 0 || !started) return
            while (nextSample < audio.samples.size) {
                val s = audio.samples[nextSample]
                if (!flushAll && s.ptsUs > untilUs) break
                val bi = MediaCodec.BufferInfo()
                bi.set(0, s.data.size, s.ptsUs, s.flags)
                muxer.writeSampleData(audioTrack, ByteBuffer.wrap(s.data), bi)
                nextSample++
            }
        }

        /** 排空输出缓冲；返回是否见到 EOS。主循环传 0（不阻塞），收尾传 10ms。 */
        fun drain(timeoutUs: Long): Boolean {
            while (true) {
                val oi = codec.dequeueOutputBuffer(info, timeoutUs)
                if (oi == MediaCodec.INFO_TRY_AGAIN_LATER) return false
                if (oi == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                    track = muxer.addTrack(codec.outputFormat)
                    muxer.start()
                    started = true
                    continue
                }
                if (oi < 0) continue
                val eos = (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0
                val buf = codec.getOutputBuffer(oi)
                if (buf != null && info.size > 0 && started &&
                    (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG) == 0) {
                    buf.position(info.offset)
                    buf.limit(info.offset + info.size)
                    writeAudio(info.presentationTimeUs, false)
                    muxer.writeSampleData(track, buf, info)
                }
                codec.releaseOutputBuffer(oi, false)
                if (eos) return true
            }
        }

        val scratch = Scratch(width, height)
        try {
            codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            codec.start()
            val frameUs = 1_000_000L / fps
            var pts = 0L
            for (i in 0 until count) {
                if (cancelled()) return Result(false, "导出已取消")
                val f = source.get(i)
                    ?: return Result(false, "第 $i 帧没送到（导出中断）")
                val bmp = BitmapFactory.decodeFile(f.absolutePath)
                    ?: return Result(false, "帧解码失败：${f.name}")
                // 输入缓冲暂时拿不到要重试。旧版直接 continue 会把这一帧丢掉，
                // pts 却不推进，成片悄悄少帧、时长也对不上。
                var idx = -1
                var spin = 0
                while (idx < 0 && spin++ < 200 && !cancelled()) {
                    idx = codec.dequeueInputBuffer(10_000)
                }
                if (idx < 0) {
                    bmp.recycle()
                    return Result(false, "编码器输入缓冲一直不可用")
                }
                val image = codec.getInputImage(idx)
                if (image == null) {
                    bmp.recycle()
                    return Result(false, "编码器不接受 Bitmap 输入")
                }
                fillYuv(image, bmp, width, height, scratch)
                codec.queueInputBuffer(idx, 0, width * height * 3 / 2, pts, 0)
                pts += frameUs
                drain(0)                 // 每帧都排空，输出缓冲堆满会卡住编码器
                if (onProgress != null && (i % 4 == 0 || i == count - 1)) {
                    onProgress(70 + (i + 1) * 30 / count)
                }
            }
            // 收尾：送一个 EOS，把编码器里剩下的帧全部取出来
            var eosIdx = -1
            var spin = 0
            while (eosIdx < 0 && spin++ < 200) {
                eosIdx = codec.dequeueInputBuffer(10_000)
            }
            if (eosIdx >= 0) {
                codec.queueInputBuffer(eosIdx, 0, 0, pts,
                    MediaCodec.BUFFER_FLAG_END_OF_STREAM)
            }
            var guard = 0
            while (!drain(10_000) && guard++ < 600) {
                if (cancelled()) return Result(false, "导出已取消")
            }
            if (!started) return Result(false, "编码器没有输出任何数据")
            // 片尾剩余的音频（静音段、比最后一帧晚几十毫秒的 AAC 帧）
            writeAudio(Long.MAX_VALUE, true)
            return Result(true, "ok", width, height,
                if (audioTrack >= 0) audio?.segments ?: 0 else 0)
        } finally {
            try { codec.stop() } catch (_: Throwable) {}
            try { codec.release() } catch (_: Throwable) {}
            if (started) {
                try { muxer.stop() } catch (_: Throwable) {}
            }
            try { muxer.release() } catch (_: Throwable) {}
        }
    }

    // YUV 系数查表：把每像素 6 次乘法换成 6 次数组取值。
    private val YR = IntArray(256) { 66 * it }
    private val YG = IntArray(256) { 129 * it }
    private val YB = IntArray(256) { 25 * it }
    private val UR = IntArray(256) { -38 * it }
    private val UG = IntArray(256) { -74 * it }
    private val UB = IntArray(256) { 112 * it }
    private val VR = IntArray(256) { 112 * it }
    private val VG = IntArray(256) { -94 * it }
    private val VB = IntArray(256) { -18 * it }

    /** ARGB → YUV420。按 Image 的 rowStride / pixelStride 写，
     *  兼容 I420（3 个 plane）与 NV12（UV 交错在 1 个 plane）两种布局。
     *  常见布局 pixelStride=1/2 走按行批量 put，比逐像素 put 快数倍。 */
    private fun fillYuv(image: Image, bmp: Bitmap, w: Int, h: Int, s: Scratch) {
        val scaled = if (bmp.width == w && bmp.height == h) bmp
        else Bitmap.createScaledBitmap(bmp, w, h, true)
        val argb = s.argb
        scaled.getPixels(argb, 0, w, 0, 0, w, h)

        val planes = image.planes
        val yp = planes[0]
        val yb = yp.buffer
        // plane buffer 的 position 可能不是 0（crop offset），按行 put 用相对位置，
        // 要把这个基准加回去，否则整幅画面会错行。
        val y0 = yb.position()
        if (yp.pixelStride == 1) {
            for (row in 0 until h) {
                val p = row * w
                for (col in 0 until w) {
                    val c = argb[p + col]
                    s.y[col] = (((YR[(c shr 16) and 255] + YG[(c shr 8) and 255] +
                        YB[c and 255] + 128) shr 8) + 16).toByte()
                }
                yb.position(y0 + row * yp.rowStride)
                yb.put(s.y, 0, w)
            }
        } else {
            for (row in 0 until h) {
                val base = row * yp.rowStride
                val p = row * w
                for (col in 0 until w) {
                    val c = argb[p + col]
                    yb.put(base + col * yp.pixelStride,
                        (((YR[(c shr 16) and 255] + YG[(c shr 8) and 255] +
                            YB[c and 255] + 128) shr 8) + 16).toByte())
                }
            }
        }

        val halfW = w / 2
        val halfH = h / 2
        if (planes.size >= 3) {
            val up = planes[1]
            val vp = planes[2]
            val ub = up.buffer
            val vb = vp.buffer
            val u0 = ub.position()
            val v0 = vb.position()
            val fast = up.pixelStride == 1 && vp.pixelStride == 1
            for (row in 0 until halfH) {
                for (col in 0 until halfW) {
                    val p = row * 2 * w + col * 2
                    val c0 = argb[p]
                    val c1 = argb[p + 1]
                    val c2 = argb[p + w]
                    val c3 = argb[p + w + 1]
                    val r = (((c0 shr 16) and 255) + ((c1 shr 16) and 255) +
                        ((c2 shr 16) and 255) + ((c3 shr 16) and 255)) / 4
                    val g = (((c0 shr 8) and 255) + ((c1 shr 8) and 255) +
                        ((c2 shr 8) and 255) + ((c3 shr 8) and 255)) / 4
                    val b = ((c0 and 255) + (c1 and 255) + (c2 and 255) + (c3 and 255)) / 4
                    val u = (((UR[r] + UG[g] + UB[b] + 128) shr 8) + 128).toByte()
                    val v = (((VR[r] + VG[g] + VB[b] + 128) shr 8) + 128).toByte()
                    if (fast) {
                        s.u[col] = u
                        s.v[col] = v
                    } else {
                        val base = row * up.rowStride + col * up.pixelStride
                        ub.put(base, u)
                        vb.put(row * vp.rowStride + col * vp.pixelStride, v)
                    }
                }
                if (fast) {
                    ub.position(u0 + row * up.rowStride)
                    ub.put(s.u, 0, halfW)
                    vb.position(v0 + row * vp.rowStride)
                    vb.put(s.v, 0, halfW)
                }
            }
        } else if (planes.size == 2) {
            // NV12：UVUV 交错，U 在偶数列、V 在奇数列
            val uvp = planes[1]
            val uvb = uvp.buffer
            val uv0 = uvb.position()
            val fast = uvp.pixelStride == 2
            for (row in 0 until halfH) {
                for (col in 0 until halfW) {
                    val p = row * 2 * w + col * 2
                    val c0 = argb[p]
                    val c1 = argb[p + 1]
                    val c2 = argb[p + w]
                    val c3 = argb[p + w + 1]
                    val r = (((c0 shr 16) and 255) + ((c1 shr 16) and 255) +
                        ((c2 shr 16) and 255) + ((c3 shr 16) and 255)) / 4
                    val g = (((c0 shr 8) and 255) + ((c1 shr 8) and 255) +
                        ((c2 shr 8) and 255) + ((c3 shr 8) and 255)) / 4
                    val b = ((c0 and 255) + (c1 and 255) + (c2 and 255) + (c3 and 255)) / 4
                    val u = (((UR[r] + UG[g] + UB[b] + 128) shr 8) + 128).toByte()
                    val v = (((VR[r] + VG[g] + VB[b] + 128) shr 8) + 128).toByte()
                    if (fast) {
                        s.uv[col * 2] = u
                        s.uv[col * 2 + 1] = v
                    } else {
                        val base = row * uvp.rowStride + col * uvp.pixelStride
                        uvb.put(base, u)
                        uvb.put(base + 1, v)
                    }
                }
                if (fast) {
                    uvb.position(uv0 + row * uvp.rowStride)
                    uvb.put(s.uv, 0, w)
                }
            }
        }
        if (scaled !== bmp) scaled.recycle()
        bmp.recycle()
    }
}
