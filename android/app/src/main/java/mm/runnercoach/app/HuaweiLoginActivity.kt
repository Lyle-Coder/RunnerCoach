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

/** 独立华为登录页：没有本地 Python/文件/JavaScript 桥。 */
class HuaweiLoginActivity : Activity() {
    private lateinit var browser: WebView
    private lateinit var address: TextView
    private lateinit var redirect: Uri
    private var completed = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val url = intent.getStringExtra("url") ?: ""
        redirect = Uri.parse(intent.getStringExtra("redirect_uri") ?: "")
        if (!isHuaweiPage(Uri.parse(url)) || redirect.scheme != "hms" ||
            redirect.authority != "redirect_url" || !redirect.path.isNullOrEmpty() ||
            redirect.query != null || redirect.fragment != null ||
            Uri.parse(url).getQueryParameter("redirect_uri") != redirect.toString()) {
            finish()
            return
        }
        val layout = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        address = TextView(this).apply {
            text = "华为账号登录 · 密码仅提交给华为"
            setPadding(24, 20, 24, 12)
        }
        layout.addView(address)
        layout.addView(Button(this).apply {
            text = "取消登录"
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
                    if (!request.isForMainFrame) return !isHuaweiPage(request.url)
                    return navigate(request.url)
                }

                @Deprecated("Deprecated in Java")
                override fun shouldOverrideUrlLoading(view: WebView, url: String): Boolean =
                    navigate(Uri.parse(url))

                override fun onPageFinished(view: WebView, url: String) {
                    val uri = Uri.parse(url)
                    if (isHuaweiPage(uri)) address.text = "华为账号登录 · ${uri.host}"
                }

                override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                    if (request.isForMainFrame && !completed) {
                        address.text = "登录页加载失败，请检查网络后返回重试"
                    }
                }
            }
        }
        layout.addView(browser, LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
        setContentView(layout)
        // 重新创建时重新加载同一授权请求，不把含授权票据的页面状态写入 Bundle。
        browser.loadUrl(url)
    }

    private fun isHuaweiPage(uri: Uri): Boolean {
        val host = uri.host?.lowercase() ?: return false
        return uri.scheme == "https" && uri.userInfo == null &&
            (uri.port == -1 || uri.port == 443) &&
            (host == "huawei.com" || host.endsWith(".huawei.com"))
    }

    private fun navigate(uri: Uri): Boolean {
        if (uri.scheme == redirect.scheme && uri.authority == redirect.authority &&
            (uri.path.isNullOrEmpty() || uri.path == "/") && uri.fragment == null) {
            if (!completed) {
                completed = true
                setResult(RESULT_OK, Intent().putExtra("callback", uri.toString()))
                finish()
            }
            return true
        }
        if (isHuaweiPage(uri)) return false
        address.text = "请在华为网页中完成登录"
        return true
    }

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
}
