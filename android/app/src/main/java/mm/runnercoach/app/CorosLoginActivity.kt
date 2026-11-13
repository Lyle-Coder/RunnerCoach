package mm.runnercoach.app

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.webkit.WebResourceRequest
import android.webkit.WebResourceError
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView

/**
 * 高驰官方 MCP 的 OAuth 授权页：只为拿睡眠（REST 没有，移动端 API 会踢掉手机 App）。
 *
 * 和华为那个登录页同一个思路：独立 Activity、没有本地 Python 桥、没有文件访问，
 * 只允许在 coros.com 域内浏览；一旦跳到固定回调地址（127.0.0.1:53682/callback）
 * 就立刻取 code 返回，绝不真的去连那个端口。
 */
class CorosLoginActivity : Activity() {
    private lateinit var browser: WebView
    private lateinit var address: TextView
    private var completed = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val url = intent.getStringExtra("url") ?: ""
        if (!isCorosPage(Uri.parse(url)) ||
            Uri.parse(url).getQueryParameter("redirect_uri") != CALLBACK
        ) {
            finish()
            return
        }
        val layout = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        address = TextView(this).apply {
            text = "高驰授权 · 睡眠与 HRV / 训练导入，密码只提交给高驰"
            setPadding(24, 20, 24, 12)
        }
        layout.addView(address)
        layout.addView(Button(this).apply {
            text = "取消授权"
            setOnClickListener { finish() }
        })
        browser = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.allowFileAccess = false
            settings.allowContentAccess = false
            settings.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            webViewClient = object : WebViewClient() {
                override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                    if (!request.isForMainFrame) return !isCorosPage(request.url)
                    return navigate(request.url)
                }

                @Deprecated("Deprecated in Java")
                override fun shouldOverrideUrlLoading(view: WebView, url: String): Boolean =
                    navigate(Uri.parse(url))

                override fun onPageFinished(view: WebView, url: String) {
                    val uri = Uri.parse(url)
                    if (isCorosPage(uri)) address.text = "高驰授权 · ${uri.host}"
                }

                override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                    if (request.isForMainFrame && !completed) {
                        address.text = "授权页加载失败，请检查网络后返回重试"
                    }
                }
            }
        }
        layout.addView(browser, LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
        setContentView(layout)
        // 重建时重新加载同一授权请求，不把带票据的页面状态写进 Bundle
        browser.loadUrl(url)
    }

    /** 只放行 https 的 coros.com（含各省份网关）；别的域一律不加载。 */
    private fun isCorosPage(uri: Uri): Boolean {
        val host = uri.host?.lowercase() ?: return false
        return uri.scheme == "https" && uri.userInfo == null &&
            (uri.port == -1 || uri.port == 443) &&
            (host == "coros.com" || host.endsWith(".coros.com"))
    }

    private fun navigate(uri: Uri): Boolean {
        if (isCallback(uri)) {
            if (!completed) {
                completed = true
                setResult(RESULT_OK, Intent().putExtra("callback", uri.toString()))
                finish()
            }
            return true
        }
        if (isCorosPage(uri)) return false
        address.text = "请在授权页完成登录"
        return true
    }

    private fun isCallback(uri: Uri): Boolean =
        uri.scheme == "http" && uri.host == "127.0.0.1" &&
            uri.port == 53682 && (uri.path ?: "") == "/callback"

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (::browser.isInitialized && browser.canGoBack()) browser.goBack() else finish()
    }

    override fun onDestroy() {
        if (::browser.isInitialized) {
            browser.stopLoading()
            browser.removeAllViews()
            browser.destroy()
        }
        super.onDestroy()
    }

    companion object {
        const val CALLBACK = "http://127.0.0.1:53682/callback"
    }
}
