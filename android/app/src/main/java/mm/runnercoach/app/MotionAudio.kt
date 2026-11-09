package mm.runnercoach.app

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaExtractor
import android.media.MediaFormat
import java.io.File
import java.nio.ByteOrder
import kotlin.math.max
import kotlin.math.min

/**
 * 回放成片的音轨：把各段素材的原声按时间轴叠到一条 48kHz 立体声 PCM 上，
 * 再压成 AAC-LC 交给 MediaMuxer。
 *
 * 为什么不用「只搬压缩样本」：素材可能来自不同设备，AAC 的 csd / 采样率 /
 * 声道数都不一样，直接把样本拼进一条轨会花屏般地爆音；中间还得留静音段。
 * 解码 → 混音 → 重编码是唯一能对齐任意素材的做法，而且 APK 里不用塞 ffmpeg。
 *
 * 整条 PCM 常驻内存：60 秒约 11 MB，10 分钟约 115 MB，所以超过 10 分钟直接
 * 不出音轨（宁可没声音，也不要在编码视频时把进程 OOM 掉）。
 */
object MotionAudio {

    private const val RATE = 48_000
    private const val CHANNELS = 2
    private const val AAC_FRAME = 1024            // AAC-LC 每帧样本数
    private const val MAX_SEC = 600
    private const val TIMEOUT_US = 10_000L

    data class Segment(val file: String, val atSec: Double, val durSec: Double)
    data class Sample(val data: ByteArray, val ptsUs: Long, val flags: Int)
    data class Track(val format: MediaFormat, val samples: List<Sample>, val segments: Int)

    /** 返回 null 表示没有可用原声，调用方按「无音轨」继续编码即可。 */
    fun build(segments: List<Segment>, totalUs: Long): Track? {
        if (segments.isEmpty() || totalUs <= 0) return null
        val totalFrames = totalUs * RATE / 1_000_000L
        if (totalFrames < AAC_FRAME || totalFrames > RATE.toLong() * MAX_SEC) return null
        return try {
            val mix = ShortArray((totalFrames * CHANNELS).toInt())
            var used = 0
            for (s in segments) {
                if (decodeInto(s, mix, totalFrames)) used++
            }
            if (used == 0) null else encodeAac(mix, totalFrames, used)
        } catch (t: Throwable) {
            null
        }
    }

    /** 解一段素材的音轨并叠加到 mix；没有音轨 / 打不开时返回 false。 */
    private fun decodeInto(seg: Segment, mix: ShortArray, totalFrames: Long): Boolean {
        val file = File(seg.file)
        if (!file.isFile) return false
        val startFrame = max(0L, (seg.atSec * RATE).toLong())
        val endFrame = min(totalFrames, startFrame + (seg.durSec * RATE).toLong())
        if (startFrame >= endFrame) return false

        val ex = MediaExtractor()
        try {
            ex.setDataSource(file.absolutePath)
        } catch (t: Throwable) {
            ex.release()
            return false
        }
        var codec: MediaCodec? = null
        try {
            var audioIdx = -1
            var srcFmt: MediaFormat? = null
            for (i in 0 until ex.trackCount) {
                val f = ex.getTrackFormat(i)
                if ((f.getString(MediaFormat.KEY_MIME) ?: "").startsWith("audio/")) {
                    audioIdx = i
                    srcFmt = f
                    break
                }
            }
            val fmt = srcFmt ?: return false
            codec = MediaCodec.createDecoderByType(fmt.getString(MediaFormat.KEY_MIME)!!)
            codec.configure(fmt, null, null, 0)
            codec.start()
            ex.selectTrack(audioIdx)

            var srcRate = if (fmt.containsKey(MediaFormat.KEY_SAMPLE_RATE))
                fmt.getInteger(MediaFormat.KEY_SAMPLE_RATE) else RATE
            var srcCh = if (fmt.containsKey(MediaFormat.KEY_CHANNEL_COUNT))
                fmt.getInteger(MediaFormat.KEY_CHANNEL_COUNT) else 1
            if (srcRate <= 0) srcRate = RATE
            if (srcCh <= 0) srcCh = 1

            val info = MediaCodec.BufferInfo()
            var inputDone = false
            var outputDone = false
            var idle = 0
            // 用累计解码样本数定位，别信各片段自己的 pts：裁剪/重封装过的
            // 文件 pts 常带偏移，按 pts 摆位会让声音整体挪动半秒。
            var decoded = 0L
            var wrote = false

            while (!outputDone && idle < 300) {
                if (!inputDone) {
                    val ii = codec.dequeueInputBuffer(TIMEOUT_US)
                    if (ii >= 0) {
                        val ib = codec.getInputBuffer(ii)
                        if (ib == null) {
                            idle++
                        } else {
                            ib.clear()
                            val size = ex.readSampleData(ib, 0)
                            if (size < 0) {
                                codec.queueInputBuffer(ii, 0, 0, 0,
                                    MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                                inputDone = true
                            } else {
                                val pts = ex.sampleTime
                                codec.queueInputBuffer(ii, 0, size, max(0L, pts), 0)
                                ex.advance()
                                idle = 0
                            }
                        }
                    }
                }
                val oi = codec.dequeueOutputBuffer(info, TIMEOUT_US)
                if (oi == MediaCodec.INFO_TRY_AGAIN_LATER) {
                    // 输入侧没进展时也要走超时，否则碰到不给输出又不报错的
                    // 解码器会在这里空转到底
                    idle++
                    continue
                }
                idle = 0
                if (oi == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                    val of = codec.outputFormat
                    if (of.containsKey(MediaFormat.KEY_SAMPLE_RATE))
                        srcRate = of.getInteger(MediaFormat.KEY_SAMPLE_RATE)
                    if (of.containsKey(MediaFormat.KEY_CHANNEL_COUNT))
                        srcCh = of.getInteger(MediaFormat.KEY_CHANNEL_COUNT)
                    if (srcRate <= 0) srcRate = RATE
                    if (srcCh <= 0) srcCh = 1
                } else if (oi >= 0) {
                    val ob = codec.getOutputBuffer(oi)
                    val codecConfig =
                        (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG) != 0
                    if (ob != null && info.size > 0 && !codecConfig) {
                        val dup = ob.duplicate().order(ByteOrder.LITTLE_ENDIAN)
                        dup.position(info.offset)
                        dup.limit(info.offset + info.size)
                        val sb = dup.asShortBuffer()
                        val nFrames = sb.remaining() / srcCh
                        if (nFrames > 0) {
                            val base = startFrame + decoded * RATE / srcRate
                            val nOut = ((nFrames.toLong() * RATE + srcRate - 1) /
                                srcRate).toInt()
                            for (k in 0 until nOut) {
                                val target = base + k
                                if (target >= endFrame) break
                                if (target < 0) continue
                                val pos = k.toDouble() * srcRate / RATE
                                val i0 = pos.toInt().coerceIn(0, nFrames - 1)
                                val i1 = (i0 + 1).coerceIn(0, nFrames - 1)
                                val frac = (pos - i0).toFloat()
                                for (ch in 0 until CHANNELS) {
                                    val sc = if (srcCh == 1) 0 else min(ch, srcCh - 1)
                                    val v0 = sb.get(i0 * srcCh + sc).toFloat()
                                    val v1 = sb.get(i1 * srcCh + sc).toFloat()
                                    val v = (v0 + (v1 - v0) * frac).toInt()
                                    val at = (target * CHANNELS + ch).toInt()
                                    val sum = mix[at].toInt() + v
                                    mix[at] = sum.coerceIn(-32768, 32767).toShort()
                                }
                                wrote = true
                            }
                            decoded += nFrames.toLong()
                        }
                    }
                    codec.releaseOutputBuffer(oi, false)
                    if ((info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) {
                        outputDone = true
                    }
                }
            }
            return wrote
        } catch (t: Throwable) {
            return false
        } finally {
            try { codec?.stop() } catch (_: Throwable) {}
            try { codec?.release() } catch (_: Throwable) {}
            try { ex.release() } catch (_: Throwable) {}
        }
    }

    /** PCM → AAC-LC。先攒成样本列表，封装时才能按视频 pts 交织写入。 */
    private fun encodeAac(mix: ShortArray, totalFrames: Long, used: Int): Track? {
        val fmt = MediaFormat.createAudioFormat(
            MediaFormat.MIMETYPE_AUDIO_AAC, RATE, CHANNELS)
        fmt.setInteger(MediaFormat.KEY_BIT_RATE, 128_000)
        fmt.setInteger(MediaFormat.KEY_AAC_PROFILE,
            MediaCodecInfo.CodecProfileLevel.AACObjectLC)
        fmt.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, AAC_FRAME * CHANNELS * 2 * 4)
        val codec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_AAC)
        val samples = ArrayList<Sample>()
        var outFmt: MediaFormat? = null
        try {
            codec.configure(fmt, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            codec.start()
            val info = MediaCodec.BufferInfo()
            var frame = 0L
            var inputDone = false
            var outputDone = false
            var idle = 0
            while (!outputDone && idle < 300) {
                if (!inputDone) {
                    val ii = codec.dequeueInputBuffer(TIMEOUT_US)
                    if (ii >= 0) {
                        val ib = codec.getInputBuffer(ii)
                        if (ib == null) {
                            idle++
                        } else {
                            ib.clear()
                            ib.order(ByteOrder.LITTLE_ENDIAN)
                            val sb = ib.asShortBuffer()
                            val first = frame * AAC_FRAME
                            // 最后一块不足一帧也补零送进去，AAC 帧长固定
                            for (k in 0 until AAC_FRAME) {
                                val f = first + k
                                val l = if (f < totalFrames) mix[(f * CHANNELS).toInt()]
                                        else 0.toShort()
                                val r = if (f < totalFrames)
                                        mix[(f * CHANNELS + 1).toInt()] else 0.toShort()
                                sb.put(k * CHANNELS, l)
                                sb.put(k * CHANNELS + 1, r)
                            }
                            val pts = first * 1_000_000L / RATE
                            if (first >= totalFrames) {
                                codec.queueInputBuffer(ii, 0, 0, pts,
                                    MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                                inputDone = true
                            } else {
                                codec.queueInputBuffer(ii, 0,
                                    AAC_FRAME * CHANNELS * 2, pts, 0)
                                frame++
                                idle = 0
                            }
                        }
                    }
                }
                val oi = codec.dequeueOutputBuffer(info, TIMEOUT_US)
                if (oi == MediaCodec.INFO_TRY_AGAIN_LATER) {
                    idle++
                    continue
                }
                idle = 0
                if (oi == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                    outFmt = codec.outputFormat
                } else if (oi >= 0) {
                    val ob = codec.getOutputBuffer(oi)
                    val skip =
                        (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG) != 0
                    if (ob != null && info.size > 0 && !skip) {
                        val dup = ob.duplicate()
                        dup.position(info.offset)
                        dup.limit(info.offset + info.size)
                        val data = ByteArray(info.size)
                        dup.get(data)
                        samples.add(Sample(data, info.presentationTimeUs,
                            info.flags and MediaCodec.BUFFER_FLAG_KEY_FRAME))
                    }
                    if ((info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) {
                        outputDone = true
                    }
                    codec.releaseOutputBuffer(oi, false)
                }
            }
        } catch (t: Throwable) {
            return null
        } finally {
            try { codec.stop() } catch (_: Throwable) {}
            try { codec.release() } catch (_: Throwable) {}
        }
        val f = outFmt
        // 编码器没给出带 csd-0 的 outputFormat 就别加音轨：缺 csd 的 AAC 轨
        // 多数播放器直接解不出声，比没有音轨更糟。
        if (f == null || !f.containsKey("csd-0") || samples.isEmpty()) return null
        return Track(f, samples, used)
    }
}
