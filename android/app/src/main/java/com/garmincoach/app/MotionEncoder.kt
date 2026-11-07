package com.garmincoach.app

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.Image
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaMuxer
import java.io.File

/**
 * 把动画页逐帧推上来的 JPEG 编码成 H.264 MP4。
 *
 * 为什么不用 ffmpeg：APK 里塞一个要 +20MB、还得按 ABI 分发；Android 自带的
 * 硬编码器足够出 1080p。输入是 `motion.py` 的 `?export=1` 逐帧 POST 到
 * `/__frame` 的 JPEG 序列，这里逐张解码成 Bitmap、转 YUV420、喂给 MediaCodec。
 *
 * 1080×1920 不是 16 的倍数，个别编码器会拒绝或出绿边，所以失败自动降到
 * 720×1280 再来一次（对分享来说够用）。
 */
object MotionEncoder {

    data class Result(val ok: Boolean, val message: String,
                      val width: Int = 0, val height: Int = 0)

    fun encode(framesDir: File, outFile: File, width: Int, height: Int, fps: Int): Result {
        val frames = framesDir.listFiles { f ->
            f.isFile && f.name.startsWith("f_") && f.name.endsWith(".jpg")
        }?.sortedBy { it.name } ?: emptyList()
        if (frames.isEmpty()) return Result(false, "没有收到任何帧")
        var last = "未知错误"
        for ((w, h) in listOf(width to height, 720 to 1280).distinct()) {
            val r = try {
                encodeWith(frames, outFile, w, h, fps)
            } catch (t: Throwable) {
                outFile.delete()
                Result(false, "${t.javaClass.simpleName}: ${t.message}")
            }
            if (r.ok) return r
            last = r.message
        }
        return Result(false, last)
    }

    private fun encodeWith(frames: List<File>, outFile: File,
                           width: Int, height: Int, fps: Int): Result {
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
        try {
            codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            codec.start()
            val frameUs = 1_000_000L / fps
            var pts = 0L
            for (f in frames) {
                val bmp = BitmapFactory.decodeFile(f.absolutePath)
                    ?: return Result(false, "帧解码失败：${f.name}")
                val idx = codec.dequeueInputBuffer(10_000)
                if (idx < 0) {
                    bmp.recycle()
                    continue
                }
                val image = codec.getInputImage(idx)
                if (image == null) {
                    bmp.recycle()
                    return Result(false, "编码器不接受 Bitmap 输入")
                }
                fillYuv(image, bmp, width, height)
                codec.queueInputBuffer(idx, 0, width * height * 3 / 2, pts, 0)
                pts += frameUs
                // 每帧都排空一次，输出缓冲堆满会直接卡住编码器
                while (true) {
                    val oi = codec.dequeueOutputBuffer(info, 0)
                    if (oi == MediaCodec.INFO_TRY_AGAIN_LATER) break
                    if (oi == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                        track = muxer.addTrack(codec.outputFormat)
                        muxer.start()
                        started = true
                    } else if (oi >= 0) {
                        val buf = codec.getOutputBuffer(oi)
                        if (buf != null && info.size > 0 && started &&
                            (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG) == 0) {
                            buf.position(info.offset)
                            buf.limit(info.offset + info.size)
                            muxer.writeSampleData(track, buf, info)
                        }
                        codec.releaseOutputBuffer(oi, false)
                    }
                }
            }
            // 收尾：送一个 EOS，把编码器里剩下的帧全部取出来
            val eosIdx = codec.dequeueInputBuffer(10_000)
            if (eosIdx >= 0) {
                codec.queueInputBuffer(eosIdx, 0, 0, pts, MediaCodec.BUFFER_FLAG_END_OF_STREAM)
            }
            var done = false
            while (!done) {
                val oi = codec.dequeueOutputBuffer(info, 10_000)
                if (oi == MediaCodec.INFO_TRY_AGAIN_LATER) continue
                if (oi == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                    track = muxer.addTrack(codec.outputFormat)
                    muxer.start()
                    started = true
                } else if (oi >= 0) {
                    if ((info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) done = true
                    val buf = codec.getOutputBuffer(oi)
                    if (buf != null && info.size > 0 && started &&
                        (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG) == 0) {
                        buf.position(info.offset)
                        buf.limit(info.offset + info.size)
                        muxer.writeSampleData(track, buf, info)
                    }
                    codec.releaseOutputBuffer(oi, false)
                }
            }
            if (!started) return Result(false, "编码器没有输出任何数据")
            return Result(true, "ok", width, height)
        } finally {
            try { codec.stop() } catch (_: Throwable) {}
            try { codec.release() } catch (_: Throwable) {}
            if (started) {
                try { muxer.stop() } catch (_: Throwable) {}
            }
            try { muxer.release() } catch (_: Throwable) {}
        }
    }

    /** ARGB → YUV420。按 Image 的 rowStride / pixelStride 写，
     *  兼容 I420（3 个 plane）与 NV12（UV 交错在 1 个 plane）两种布局。 */
    private fun fillYuv(image: Image, bmp: Bitmap, w: Int, h: Int) {
        val scaled = if (bmp.width == w && bmp.height == h) bmp
        else Bitmap.createScaledBitmap(bmp, w, h, true)
        val argb = IntArray(w * h)
        scaled.getPixels(argb, 0, w, 0, 0, w, h)
        val planes = image.planes
        val yp = planes[0]
        val yb = yp.buffer
        for (row in 0 until h) {
            val base = row * yp.rowStride
            for (col in 0 until w) {
                val c = argb[row * w + col]
                val r = (c shr 16) and 0xFF
                val g = (c shr 8) and 0xFF
                val b = c and 0xFF
                val y = (((66 * r + 129 * g + 25 * b + 128) shr 8) + 16)
                yb.put(base + col * yp.pixelStride, y.toByte())
            }
        }
        if (planes.size >= 3) {
            val up = planes[1]
            val vp = planes[2]
            val ub = up.buffer
            val vb = vp.buffer
            for (row in 0 until h / 2) {
                for (col in 0 until w / 2) {
                    var rs = 0
                    var gs = 0
                    var bs = 0
                    for (dy in 0..1) {
                        for (dx in 0..1) {
                            val c = argb[(row * 2 + dy) * w + (col * 2 + dx)]
                            rs += (c shr 16) and 0xFF
                            gs += (c shr 8) and 0xFF
                            bs += c and 0xFF
                        }
                    }
                    val r = rs / 4
                    val g = gs / 4
                    val b = bs / 4
                    val u = (((-38 * r - 74 * g + 112 * b + 128) shr 8) + 128)
                    val v = (((112 * r - 94 * g - 18 * b + 128) shr 8) + 128)
                    ub.put(row * up.rowStride + col * up.pixelStride, u.toByte())
                    vb.put(row * vp.rowStride + col * vp.pixelStride, v.toByte())
                }
            }
        } else if (planes.size == 2) {
            // NV12：UVUV 交错，U 在偶数列、V 在奇数列
            val uvp = planes[1]
            val ub = uvp.buffer
            for (row in 0 until h / 2) {
                for (col in 0 until w / 2) {
                    var rs = 0
                    var gs = 0
                    var bs = 0
                    for (dy in 0..1) {
                        for (dx in 0..1) {
                            val c = argb[(row * 2 + dy) * w + (col * 2 + dx)]
                            rs += (c shr 16) and 0xFF
                            gs += (c shr 8) and 0xFF
                            bs += c and 0xFF
                        }
                    }
                    val r = rs / 4
                    val g = gs / 4
                    val b = bs / 4
                    val u = (((-38 * r - 74 * g + 112 * b + 128) shr 8) + 128)
                    val v = (((112 * r - 94 * g - 18 * b + 128) shr 8) + 128)
                    val base = row * uvp.rowStride + col * uvp.pixelStride
                    ub.put(base, u.toByte())
                    ub.put(base + 1, v.toByte())
                }
            }
        }
        if (scaled !== bmp) scaled.recycle()
        bmp.recycle()
    }
}
