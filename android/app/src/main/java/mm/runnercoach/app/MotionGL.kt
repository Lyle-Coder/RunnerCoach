package mm.runnercoach.app

import android.graphics.Bitmap
import android.opengl.EGL14
import android.opengl.EGLConfig
import android.opengl.EGLContext
import android.opengl.EGLDisplay
import android.opengl.EGLExt
import android.opengl.EGLSurface
import android.opengl.GLES20
import android.opengl.GLUtils
import android.view.Surface
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer

/**
 * 把逐帧 JPEG 贴到 MediaCodec 的**输入 Surface** 上。
 *
 * v43 之前每帧都要在 Kotlin 里跑 1080×1920 = 207 万次逐像素 YUV 转换，
 * 这是手机端导出最重的 CPU 活。改成 Surface 输入后，编码器自己从
 * BufferQueue 取 GPU 渲染好的帧：Bitmap 解码交给 native Skia，颜色空间
 * 转换与缩放交给 GPU/编码器，Java 层只剩一次纹理上传。
 *
 * 时间戳不能靠 `queueInputBuffer`（Surface 模式没有输入缓冲可排），要用
 * `eglPresentationTimeANDROID` 在 `eglSwapBuffers` 之前打到 EGL surface 上，
 * 否则所有帧的 pts 都是 0，成片会变成一帧静止画面或时长错乱。
 *
 * 这套 EGL/GLES2 在极少数设备上会初始化失败，调用方负责整体降级回 CPU 路径。
 */
class MotionGL private constructor(
    private val display: EGLDisplay,
    private val eglSurface: EGLSurface,
    private val context: EGLContext,
    private val inputSurface: Surface,
    private val width: Int,
    private val height: Int,
    private val program: Int,
    private val posLoc: Int,
    private val texLoc: Int,
    private val samplerLoc: Int,
    private val textureId: Int,
    private val vbo: Int,
    private val verts: FloatBuffer,
) {
    private var allocated = false

    /** 画一帧并送进编码器。`ptsUs` 是这帧在成片里的微秒时间戳。 */
    fun draw(bitmap: Bitmap, ptsUs: Long) {
        GLES20.glUseProgram(program)
        GLES20.glActiveTexture(GLES20.GL_TEXTURE0)
        GLES20.glBindTexture(GLES20.GL_TEXTURE_2D, textureId)
        // 第一次分配纹理存储，之后只替换内容，避免每帧重新申请 8MB GPU 内存
        if (!allocated) {
            GLUtils.texImage2D(GLES20.GL_TEXTURE_2D, 0, bitmap, 0)
            allocated = true
        } else {
            GLUtils.texSubImage2D(GLES20.GL_TEXTURE_2D, 0, 0, 0, bitmap)
        }
        checkGl("upload")

        GLES20.glUniform1i(samplerLoc, 0)
        GLES20.glBindBuffer(GLES20.GL_ARRAY_BUFFER, vbo)
        GLES20.glEnableVertexAttribArray(posLoc)
        GLES20.glVertexAttribPointer(posLoc, 2, GLES20.GL_FLOAT, false, STRIDE, 0)
        GLES20.glEnableVertexAttribArray(texLoc)
        GLES20.glVertexAttribPointer(texLoc, 2, GLES20.GL_FLOAT, false, STRIDE, 8)
        GLES20.glViewport(0, 0, width, height)
        GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, 4)
        checkGl("draw")

        // 必须在 swap 之前：这一帧的 pts 随 BufferQueue 一起交给编码器
        EGLExt.eglPresentationTimeANDROID(display, eglSurface, ptsUs * 1000L)
        if (!EGL14.eglSwapBuffers(display, eglSurface)) {
            throw IllegalStateException("eglSwapBuffers 失败：0x" +
                Integer.toHexString(EGL14.eglGetError()))
        }
    }

    fun release() {
        try {
            EGL14.eglMakeCurrent(display, eglSurface, eglSurface, context)
            GLES20.glDeleteProgram(program)
            GLES20.glDeleteTextures(1, intArrayOf(textureId), 0)
            GLES20.glDeleteBuffers(1, intArrayOf(vbo), 0)
        } catch (_: Throwable) {
        }
        try {
            EGL14.eglMakeCurrent(display, EGL14.EGL_NO_SURFACE,
                EGL14.EGL_NO_SURFACE, EGL14.EGL_NO_CONTEXT)
            EGL14.eglDestroySurface(display, eglSurface)
            EGL14.eglDestroyContext(display, context)
            EGL14.eglTerminate(display)
        } catch (_: Throwable) {
        }
        try { inputSurface.release() } catch (_: Throwable) {}
    }

    private fun checkGl(where: String) {
        val e = GLES20.glGetError()
        if (e != GLES20.GL_NO_ERROR) {
            throw IllegalStateException("GL 错误($where)：0x" + Integer.toHexString(e))
        }
    }

    companion object {
        // EGL_RECORDABLE_ANDROID：告诉 EGL 这个 surface 要被 MediaCodec 录制。
        // 有设备不认这个扩展，create() 会去掉它再试一次。
        private const val EGL_RECORDABLE_ANDROID = 0x3142

        // x, y, u, v；TRIANGLE_STRIP 顺序：左下 / 右下 / 左上 / 右上。
        // GLUtils 把 Bitmap 第一行放在纹理 v=0，而 v=0 在屏幕上对应底部，
        // 所以顶点着色器里把 v 取反，画面才不会上下颠倒。
        private val VERTS = floatArrayOf(
            -1f, -1f, 0f, 0f,
            1f, -1f, 1f, 0f,
            -1f, 1f, 0f, 1f,
            1f, 1f, 1f, 1f,
        )
        private const val STRIDE = 4 * 4

        private const val VS =
            "attribute vec2 aPos; attribute vec2 aTex; varying vec2 vTex; " +
                "void main() { vTex = vec2(aTex.x, 1.0 - aTex.y); " +
                "gl_Position = vec4(aPos, 0.0, 1.0); }"
        private const val FS =
            "precision mediump float; uniform sampler2D uTex; varying vec2 vTex; " +
                "void main() { gl_FragColor = texture2D(uTex, vTex); }"

        fun create(inputSurface: Surface, width: Int, height: Int): MotionGL {
            var display: EGLDisplay? = null
            var context: EGLContext? = null
            var eglSurface: EGLSurface? = null
            try {
                display = EGL14.eglGetDisplay(EGL14.EGL_DEFAULT_DISPLAY)
                val version = IntArray(2)
                require(EGL14.eglInitialize(display, version, 0, version, 1)) {
                    "eglInitialize 失败"
                }
                val config = chooseConfig(display)
                context = EGL14.eglCreateContext(
                    display, config, EGL14.EGL_NO_CONTEXT,
                    intArrayOf(EGL14.EGL_CONTEXT_CLIENT_VERSION, 2, EGL14.EGL_NONE), 0)
                require(context != EGL14.EGL_NO_CONTEXT) { "eglCreateContext 失败" }
                eglSurface = EGL14.eglCreateWindowSurface(
                    display, config, inputSurface, intArrayOf(EGL14.EGL_NONE), 0)
                require(eglSurface != EGL14.EGL_NO_SURFACE) { "eglCreateWindowSurface 失败" }
                require(EGL14.eglMakeCurrent(display, eglSurface, eglSurface, context)) {
                    "eglMakeCurrent 失败"
                }

                val vs = compile(GLES20.GL_VERTEX_SHADER, VS)
                val fs = compile(GLES20.GL_FRAGMENT_SHADER, FS)
                val program = GLES20.glCreateProgram()
                GLES20.glAttachShader(program, vs)
                GLES20.glAttachShader(program, fs)
                GLES20.glLinkProgram(program)
                val linked = IntArray(1)
                GLES20.glGetProgramiv(program, GLES20.GL_LINK_STATUS, linked, 0)
                require(linked[0] == GLES20.GL_TRUE) {
                    "link 失败：" + GLES20.glGetProgramInfoLog(program)
                }
                GLES20.glDeleteShader(vs)
                GLES20.glDeleteShader(fs)
                val posLoc = GLES20.glGetAttribLocation(program, "aPos")
                val texLoc = GLES20.glGetAttribLocation(program, "aTex")
                val samplerLoc = GLES20.glGetUniformLocation(program, "uTex")
                require(posLoc >= 0 && texLoc >= 0) { "找不到顶点属性" }

                val textureId = IntArray(1)
                GLES20.glGenTextures(1, textureId, 0)
                GLES20.glBindTexture(GLES20.GL_TEXTURE_2D, textureId[0])
                GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D,
                    GLES20.GL_TEXTURE_MIN_FILTER, GLES20.GL_LINEAR)
                GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D,
                    GLES20.GL_TEXTURE_MAG_FILTER, GLES20.GL_LINEAR)
                GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D,
                    GLES20.GL_TEXTURE_WRAP_S, GLES20.GL_CLAMP_TO_EDGE)
                GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D,
                    GLES20.GL_TEXTURE_WRAP_T, GLES20.GL_CLAMP_TO_EDGE)

                val vbo = IntArray(1)
                GLES20.glGenBuffers(1, vbo, 0)
                GLES20.glBindBuffer(GLES20.GL_ARRAY_BUFFER, vbo[0])
                val fb = ByteBuffer.allocateDirect(VERTS.size * 4)
                    .order(ByteOrder.nativeOrder()).asFloatBuffer()
                fb.put(VERTS).position(0)
                GLES20.glBufferData(GLES20.GL_ARRAY_BUFFER, VERTS.size * 4,
                    fb, GLES20.GL_STATIC_DRAW)
                val err = GLES20.glGetError()
                require(err == GLES20.GL_NO_ERROR) {
                    "GL 初始化错误：0x" + Integer.toHexString(err)
                }

                return MotionGL(display, eglSurface, context, inputSurface,
                    width, height, program, posLoc, texLoc, samplerLoc,
                    textureId[0], vbo[0], fb)
            } catch (t: Throwable) {
                // 半初始化的资源也要收干净，否则重试时 EGL 上下文会泄漏
                try {
                    if (display != null) {
                        EGL14.eglMakeCurrent(display, EGL14.EGL_NO_SURFACE,
                            EGL14.EGL_NO_SURFACE, EGL14.EGL_NO_CONTEXT)
                        if (eglSurface != null && eglSurface != EGL14.EGL_NO_SURFACE) {
                            EGL14.eglDestroySurface(display, eglSurface)
                        }
                        if (context != null && context != EGL14.EGL_NO_CONTEXT) {
                            EGL14.eglDestroyContext(display, context)
                        }
                        EGL14.eglTerminate(display)
                    }
                } catch (_: Throwable) {
                }
                try { inputSurface.release() } catch (_: Throwable) {}
                throw t
            }
        }

        private fun chooseConfig(display: EGLDisplay): EGLConfig {
            for (recordable in listOf(true, false)) {
                val a = mutableListOf(
                    EGL14.EGL_RED_SIZE, 8,
                    EGL14.EGL_GREEN_SIZE, 8,
                    EGL14.EGL_BLUE_SIZE, 8,
                    EGL14.EGL_ALPHA_SIZE, 8,
                    EGL14.EGL_RENDERABLE_TYPE, EGL14.EGL_OPENGL_ES2_BIT,
                )
                if (recordable) {
                    a += EGL_RECORDABLE_ANDROID
                    a += 1
                }
                a += EGL14.EGL_NONE
                val out = arrayOfNulls<EGLConfig>(1)
                val n = IntArray(1)
                if (EGL14.eglChooseConfig(display, a.toIntArray(), 0,
                        out, 0, 1, n, 0) && n[0] > 0 && out[0] != null) {
                    return out[0]!!
                }
            }
            throw IllegalStateException("找不到可用于录制的 EGLConfig")
        }

        private fun compile(type: Int, src: String): Int {
            val s = GLES20.glCreateShader(type)
            GLES20.glShaderSource(s, src)
            GLES20.glCompileShader(s)
            val ok = IntArray(1)
            GLES20.glGetShaderiv(s, GLES20.GL_COMPILE_STATUS, ok, 0)
            if (ok[0] != GLES20.GL_TRUE) {
                val log = GLES20.glGetShaderInfoLog(s)
                GLES20.glDeleteShader(s)
                throw IllegalStateException("shader 编译失败：$log")
            }
            return s
        }
    }
}
