package mm.runnercoach.app

import android.app.DownloadManager
import android.content.Context
import android.net.Uri
import android.os.Environment
import java.util.UUID

/** Gitee may serve APKs as application/zip. Pin both MIME and destination so
 * the system keeps an installable .apk instead of a browser appending .zip. */
object ApkDownload {
    const val MIME = "application/vnd.android.package-archive"
    enum class State { QUEUED, RUNNING, DOWNLOADED }

    @Synchronized
    fun enqueue(context: Context, url: String, assetName: String): State {
        val uri = Uri.parse(url)
        // https 为主。允许 http 是因为自建分发走的是明文端口：系统下载管理器
        // 只信系统根证书，认不了我们的自签证书（握手直接失败）。安装包本身
        // 是公开物，装的时候 Android 还会校验签名，明文只影响"下载到什么"。
        // 地址来自服务端下发的元数据，不是用户输入，仍要求有主机名。
        require((uri.scheme == "https" || uri.scheme == "http") &&
            !uri.host.isNullOrBlank()) {
            "安装包下载地址无效"
        }
        require(Regex("[A-Za-z0-9][A-Za-z0-9._-]{0,150}\\.apk", RegexOption.IGNORE_CASE)
            .matches(assetName) && !assetName.contains("debug", ignoreCase = true)) {
            "未找到可安装的正式 APK 附件"
        }
        val manager = context.getSystemService(Context.DOWNLOAD_SERVICE) as? DownloadManager
            ?: throw IllegalStateException("系统下载管理器不可用")
        val prefs = context.getSharedPreferences("apk_download", Context.MODE_PRIVATE)
        val previous = prefs.getLong("id", -1L)
        if (previous > 0 && prefs.getString("url", "") == url &&
            prefs.getString("asset", "") == assetName) {
            manager.query(DownloadManager.Query().setFilterById(previous))?.use { cursor ->
                if (cursor.moveToFirst()) {
                    when (cursor.getInt(cursor.getColumnIndexOrThrow(DownloadManager.COLUMN_STATUS))) {
                        DownloadManager.STATUS_PENDING, DownloadManager.STATUS_RUNNING,
                        DownloadManager.STATUS_PAUSED -> return State.RUNNING
                        DownloadManager.STATUS_SUCCESSFUL -> {
                            // A completed row can outlive a file deleted in Downloads.
                            // Reuse it only if the file is still accessible.
                            val exists = try {
                                manager.openDownloadedFile(previous)?.use { true } ?: false
                            } catch (_: java.io.FileNotFoundException) {
                                false
                            }
                            if (exists) return State.DOWNLOADED
                        }
                        else -> Unit  // Failed jobs may be retried with a new filename.
                    }
                }
            }
        }
        // A unique basename permits retrying a failed/deleted download without
        // overwriting an existing user file. The extension is always literal .apk.
        val filename = assetName.dropLast(4) + "-" + UUID.randomUUID().toString().take(8) + ".apk"
        val request = DownloadManager.Request(uri)
            .setMimeType(MIME)
            .setTitle(assetName)
            .setDescription("RunnerCoach 更新安装包")
            .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, "RunnerCoach/$filename")
            .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
        val id = manager.enqueue(request)
        check(id > 0) { "未能创建下载任务" }
        prefs.edit().putLong("id", id).putString("url", url)
            .putString("asset", assetName).apply()
        return State.QUEUED
    }
}
