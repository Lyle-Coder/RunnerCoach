package mm.runnercoach.app

import android.content.ContentProvider
import android.content.ContentValues
import android.database.Cursor
import android.net.Uri
import android.os.ParcelFileDescriptor
import java.io.File
import java.io.FileNotFoundException

/**
 * 只读的安装包出口：把 filesDir 下 updates 目录里的 apk 暴露成 content:// URI。
 *
 * 注意这里别写「斜杠加星号加 .apk」那种路径通配——Kotlin 的块注释**支持嵌套**，
 * 一个 `/*` 会再开一层，末尾的 `*/` 只关掉内层，编译报「Unclosed comment」。
 * 这个坑刚踩过。
 *
 * 为什么自己写、不用 androidx 的 FileProvider —— 这个项目刻意不引 androidx
 * （见 build.gradle.kts 里的说明），为了一件小事多带一个依赖不划算，而这里
 * 只要实现 openFile 和 getType 两个方法就够，三十行。
 *
 * 为什么必须有它 —— Android 7 (API 24) 起禁止把 `file://` 的 URI 交给别的
 * 应用（FileUriExposedException）。系统安装器要的是 content://，所以更新包
 * 下载完必须从这里出去。
 *
 * 为什么只认 updates 目录、只认 .apk —— 这个 provider 是 exported="false" 的，
 * 只有本 App 授权的 URI 才能被外部读；但保险起见仍然只映射一个目录，
 * 免得哪天 authority 配错把整个私有目录送出去。
 */
class ApkProvider : ContentProvider() {

    override fun onCreate(): Boolean = true

    private fun fileFor(uri: Uri): File? {
        val name = uri.lastPathSegment ?: return null
        // 名字里不许出现路径分隔符或上跳，否则就是路径穿越
        if (name.contains('/') || name.contains('\\') || name.contains("..")) return null
        if (!name.endsWith(".apk", ignoreCase = true)) return null
        val dir = File(context?.filesDir ?: return null, "updates")
        val f = File(dir, name)
        return if (f.isFile) f else null
    }

    override fun openFile(uri: Uri, mode: String): ParcelFileDescriptor {
        if (mode != "r") throw SecurityException("ApkProvider 只读")
        val f = fileFor(uri) ?: throw FileNotFoundException("没有这个安装包")
        return ParcelFileDescriptor.open(f, ParcelFileDescriptor.MODE_READ_ONLY)
    }

    override fun getType(uri: Uri): String = "application/vnd.android.package-archive"

    override fun query(
        uri: Uri,
        projection: Array<out String>?,
        selection: String?,
        selectionArgs: Array<out String>?,
        sortOrder: String?,
    ): Cursor? = null

    override fun insert(uri: Uri, values: ContentValues?): Uri? = null

    override fun delete(uri: Uri, selection: String?, selectionArgs: Array<out String>?): Int = 0

    override fun update(
        uri: Uri,
        values: ContentValues?,
        selection: String?,
        selectionArgs: Array<out String>?,
    ): Int = 0
}
