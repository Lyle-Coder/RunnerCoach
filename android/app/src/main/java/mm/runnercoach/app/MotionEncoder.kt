package mm.runnercoach.app

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.Image
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaMuxer
import android.view.Surface
import java.io.File
import java.nio.ByteBuffer

/**
 * 把动画页逐帧推上来的 JPEG 编码成 H.264 MP4，并混入素材原声。
 *
 * 性能（v43）
 * ----------
 * v42 把「渲染完才编码」改成边收边编，但每帧仍要在 Kotlin 里跑 207 万次
 * 逐像素 YUV 转换 —— 手机上这是最重的 CPU 活，跟 WebView 抢核心，并行收益
 * 被吃掉。这版改成 **MediaCodec Surface 输入**：JPEG 由 native 解码成 Bitmap，
 * 经 GLES2 贴到编码器输入 Surface，颜色转换/缩放全交给 GPU 与编码器，
 * Java 层只剩一次纹理上传（见 MotionGL）。
 *
 * 兼容性：EGL/GLES2 或 createInputSurface 在个别设备上会失败。这时**整体重建
 * codec**，退回 v42 的 fillYuv CPU 路径，绝不因为 GPU 不可用就出不了片。
 * 1080×1920 不是 16 的倍数，个别编码器会拒绝，自动降到 720×1280 再来一次。
 */
object MotionEncoder {

    data class Result(val ok: Boolean, val message: String,
                      val width: Int = 0, val height: Int = 0,
                      val audioSegments: Int = 0, val via: String = "CPU")

    /** 惰性帧源：给第 i 帧的文件；返回 null 表示导出被取消或等不到帧。 */
    fun interface FrameAt {
        fun get(index: Int): File?
    }

    /** CPU 降级路径每帧复用的一块临时内存，避免 8MB IntArray 反复分配。 */
    private class Scratch(w: Int, h: Int) {
        val argb = IntArray(w * h)
        val y = ByteArray(w)
        val u = ByteArray(w / 2)
        val v = ByteArray(w / 2)
        val uv = ByteArray(w)
    }

    /** 音视频共用一份 muxer：AAC 轨必须在 start() 前加好，视频 csd 要等编码器。 */
    private class MuxState(outFile: File, private val audio: MotionAudio.Track?) {
        private val muxer = MediaMuxer(outFile.absolutePath,
            MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
        private val audioTrack: Int = audio?.let {
            try { muxer.addTrack(it.format) } catch (_: Throwable) { -1 }
        } ?: -1
        private var videoTrack = -1
        private var nextSample = 0
        private var finished = false
        var started = false
            private set

        val audioSegments: Int get() = if (audioTrack >= 0) audio?.segments ?: 0 else 0

        fun onOutputFormat(fmt: MediaFormat) {
            if (started) return
            videoTrack = muxer.addTrack(fmt)
            muxer.start()
            started = true
        }

        private fun writeAudio(untilUs: Long, flushAll: Boolean) {
            if (audio == null || audioTrack < 0 || !started || finished) return
            while (nextSample < audio.samples.size) {
                val s = audio.samples[nextSample]
                if (!flushAll && s.ptsUs > untilUs) break
                val bi = MediaCodec.BufferInfo()
                bi.set(0, s.data.size, s.ptsUs, s.flags)
                muxer.writeSampleData(audioTrack, ByteBuffer.wrap(s.data), bi)
                nextSample++
            }
        }

        /** 按视频时间戳交织音频：非交织 MP4 在部分播放器上要整片缓冲完才出声。 */
        fun writeVideo(buf: ByteBuffer, info: MediaCodec.BufferInfo) {
            writeAudio(info.presentationTimeUs, false)
            muxer.writeSampleData(videoTrack, buf, info)
        }

        /** 幂等：成功与失败路径都会走 finally，不能 stop 两次。 */
        fun finish() {
            if (finished) return
            finished = true
            try { writeAudioBeforeFinish() } catch (_: Throwable) {}
            try { if (started) muxer.stop() } catch (_: Throwable) {}
            try { muxer.release() } catch (_: Throwable) {}
        }

        // finish() 会把 finished 置 true，直接调 writeAudio 会被挡住，
        // 所以片尾剩余音频单独走这里。
        private fun writeAudioBeforeFinish() {
            if (audio == null || audioTrack < 0 || !started) return
            while (nextSample < audio.samples.size) {
                val s = audio.samples[nextSample]
                val bi = MediaCodec.BufferInfo()
                bi.set(0, s.data.size, s.ptsUs, s.flags)
                muxer.writeSampleData(audioTrack, ByteBuffer.wrap(s.data), bi)
                nextSample++
            }
        }
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
        // 音频只解码一次：换分辨率、GPU 降级 CPU 都复用同一批 AAC 样本。
        val totalUs = count.toLong() * 1_000_000L / maxOf(1, fps)
        val audio = try {
            MotionAudio.build(segments, totalUs)
        } catch (t: Throwable) {
            null
        }
        var last = "未知错误"
        for ((w, h) in listOf(width to height, 720 to 1280).distinct()) {
            // 每个分辨率都先试 GPU；GPU 不可用才在同一分辨率下重建 codec 走 CPU。
            for (useSurface in listOf(true, false)) {
                val r = try {
                    encodeOnce(count, source, outFile, w, h, fps, audio,
                        cancelled, onProgress, useSurface)
                } catch (t: Throwable) {
                    outFile.delete()
                    Result(false, "${if (useSurface) "GPU" else "CPU"} " +
                        "${t.javaClass.simpleName}: ${t.message}")
                }
                if (r.ok) return r
                // 被取消或帧没送齐，重试/降级都没有意义，反而多编一遍
                if (cancelled() || r.message.contains("没送到") ||
                    r.message.contains("取消")) return r
                outFile.delete()
                last = r.message
            }
        }
        return Result(false, last)
    }

    private fun encodeOnce(count: Int, source: FrameAt, outFile: File,
                           width: Int, height: Int, fps: Int,
                           audio: MotionAudio.Track?, cancelled: () -> Boolean,
                           onProgress: ((Int) -> Unit)?, useSurface: Boolean): Result {
        val format = MediaFormat.createVideoFormat(
            MediaFormat.MIMETYPE_VIDEO_AVC, width, height)
        format.setInteger(MediaFormat.KEY_COLOR_FORMAT,
            if (useSurface) MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface
            else MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible)
        format.setInteger(MediaFormat.KEY_BIT_RATE,
            if (width >= 1000) 8_000_000 else 4_000_000)
        format.setInteger(MediaFormat.KEY_FRAME_RATE, fps)
        format.setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1)

        val codec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
        outFile.parentFile?.mkdirs()
        outFile.delete()
        val mux = MuxState(outFile, audio)
        val info = MediaCodec.BufferInfo()
        var gl: MotionGL? = null
        var inputSurface: Surface? = null
        var reusable: Bitmap? = null
        val scratch = if (!useSurface) Scratch(width, height) else null
        val via = if (useSurface) "GPU" else "CPU"

        try {
            codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            if (useSurface) {
                // 必须在 configure 之后、start 之前
                inputSurface = codec.createInputSurface()
                gl = MotionGL.create(inputSurface, width, height)
            }
            codec.start()

            /** 排空输出缓冲；返回是否见到 EOS。主循环传 0（不阻塞），收尾传 10ms。 */
            fun drain(timeoutUs: Long): Boolean {
                while (true) {
                    val oi = codec.dequeueOutputBuffer(info, timeoutUs)
                    if (oi == MediaCodec.INFO_TRY_AGAIN_LATER) return false
                    if (oi == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                        mux.onOutputFormat(codec.outputFormat)
                        continue
                    }
                    if (oi < 0) continue
                    val eos = (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0
                    val buf = codec.getOutputBuffer(oi)
                    if (buf != null && info.size > 0 && mux.started &&
                        (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG) == 0) {
                        buf.position(info.offset)
                        buf.limit(info.offset + info.size)
                        mux.writeVideo(buf, info)
                    }
                    codec.releaseOutputBuffer(oi, false)
                    if (eos) return true
                }
            }

            val frameUs = 1_000_000L / fps
            var pts = 0L
            for (i in 0 until count) {
                if (cancelled()) return Result(false, "导出已取消")
                val f = source.get(i)
                    ?: return Result(false, "第 $i 帧没送到（导出中断）")
                // 复用同一块 8MB 像素内存：720 帧能省掉约 5.7GB 分配与 GC 停顿。
                // 个别机型对「JPEG + inBitmap」不兼容就退回普通解码，绝不出不了片。
                val opts = BitmapFactory.Options().apply { inMutable = true }
                reusable?.let { if (!it.isRecycled) opts.inBitmap = it }
                val bmp = try {
                    BitmapFactory.decodeFile(f.absolutePath, opts)
                } catch (_: Throwable) {
                    BitmapFactory.decodeFile(f.absolutePath)
                } ?: return Result(false, "帧解码失败：${f.name}")
                reusable = bmp
                if (useSurface) {
                    // 时间戳随这一帧的 BufferQueue 一起交给编码器
                    gl!!.draw(bmp, pts)
                } else {
                    // 输入缓冲暂时拿不到要重试。旧版直接 continue 会丢帧，
                    // pts 却不推进，成片悄悄少帧、时长也对不上。
                    var idx = -1
                    var spin = 0
                    while (idx < 0 && spin++ < 200 && !cancelled()) {
                        idx = codec.dequeueInputBuffer(10_000)
                    }
                    if (idx < 0) return Result(false, "编码器输入缓冲一直不可用")
                    val image = codec.getInputImage(idx)
                        ?: return Result(false, "编码器不接受 Bitmap 输入")
                    fillYuv(image, bmp, width, height, scratch!!)
                    codec.queueInputBuffer(idx, 0, width * height * 3 / 2, pts, 0)
                }
                pts += frameUs
                drain(0)                 // 每帧都排空，输出缓冲堆满会卡住编码器
                // 报的是**已编码帧数**，不是百分比。进度由 App 端用
                // min(渲染帧, 编码帧) 统一换算，避免并行时假跳到 70%。
                if (onProgress != null && (i % 4 == 0 || i == count - 1)) {
                    onProgress(i + 1)
                }
            }

            // EOS：Surface 输入没有输入缓冲可排，只能显式通知编码器
            if (useSurface) {
                codec.signalEndOfInputStream()
            } else {
                var eosIdx = -1
                var spin = 0
                while (eosIdx < 0 && spin++ < 200) {
                    eosIdx = codec.dequeueInputBuffer(10_000)
                }
                if (eosIdx >= 0) {
                    codec.queueInputBuffer(eosIdx, 0, 0, pts,
                        MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                }
            }
            var guard = 0
            while (!drain(10_000) && guard++ < 600) {
                if (cancelled()) return Result(false, "导出已取消")
            }
            if (!mux.started) return Result(false, "编码器没有输出任何数据")
            return Result(true, "ok", width, height, mux.audioSegments, via)
        } finally {
            try { reusable?.recycle() } catch (_: Throwable) {}
            try { gl?.release() } catch (_: Throwable) {}
            if (gl == null) try { inputSurface?.release() } catch (_: Throwable) {}
            try { codec.stop() } catch (_: Throwable) {}
            try { codec.release() } catch (_: Throwable) {}
            mux.finish()
        }
    }

    // ---- CPU 降级路径：v42 的查表 + 行批量 YUV420 转换，原样保留 ----

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
     *  兼容 I420（3 个 plane）与 NV12（UV 交错在 1 个 plane）两种布局。 */
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
        // bmp 由调用方统一 recycle（GPU 路径也要复用这块 Bitmap）
        if (scaled !== bmp) scaled.recycle()
    }
}
