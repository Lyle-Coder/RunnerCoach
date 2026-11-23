package mm.runnercoach.app

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.webkit.CookieManager
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import org.json.JSONArray
import org.json.JSONObject

/**
 * 佳明登录页：把登录交给一个真 WebView，人点完验证码，我们只收 Cookie。
 *
 * 为什么要这么绕（2026-11-24 实测）：`connect.garmin.cn` 的 SSO Portal 上了
 * reCAPTCHA Enterprise 可见复选框，`/portal/api/login` 必须带那个 token。
 * 程序点不了框，服务器和 App 的密码登录一律被回 `Previous captcha evaluation
 * is mandatory`。所以登录这一步只能由人在页面上做一次。
 *
 * 这个 Activity 没有本地 Python/文件/JavaScript 桥，只做三件事：加载登录页、
 * 盯住 `connect.garmin.cn`、把 `.garmin.cn` 的 Cookie 以 JSON 交回 MainActivity。
 */
class GarminLoginActivity : Activity() {
    companion object {
        const val SIGNIN = "https://sso.garmin.cn/portal/sso/zh-CN/sign-in" +
            "?clientId=GarminConnect&service=https%3A%2F%2Fconnect.garmin.cn%2Fapp"

        // 桌面 Chrome UA：Portal 对移动端 UA 会直接回 Cloudflare 403，
        // 登录页根本打不开（auth.py 里那条注释是同一个坑）。
        const val DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    }

    private lateinit var browser: WebView
    private lateinit var address: TextView
    private var completed = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val layout = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        address = TextView(this).apply {
            text = "佳明账号登录 · 密码只提交给佳明"
            textSize = 13f
            setPadding(24, 20, 24, 8)
        }
        layout.addView(address)
        val bar = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        bar.addView(Button(this).apply {
            text = "取消"
            layoutParams = LinearLayout.LayoutParams(0, MATCH_PARENT, 1f)
            setOnClickListener { finish() }
        })
        bar.addView(Button(this).apply {
            text = "登录完成"
            layoutParams = LinearLayout.LayoutParams(0, MATCH_PARENT, 1f)
            setOnClickListener { if (!completed) finishWithCookies() }
        })
        layout.addView(bar)
        browser = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.databaseEnabled = true
            settings.userAgentString = DESKTOP_UA
            settings.allowFileAccess = false
            settings.allowContentAccess = false
            settings.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            CookieManager.getInstance().setAcceptCookie(true)
            CookieManager.getInstance().setAcceptThirdPartyCookies(this, true)
            webViewClient = object : WebViewClient() {
                override fun shouldOverrideUrlLoading(
                    view: WebView, request: WebResourceRequest
                ): Boolean {
                    if (!request.isForMainFrame) return false
                    return navigate(request.url)
                }

                @Deprecated("Deprecated in Java")
                override fun shouldOverrideUrlLoading(view: WebView, url: String): Boolean =
                    navigate(Uri.parse(url))

                override fun onPageFinished(view: WebView, url: String) {
                    val uri = Uri.parse(url)
                    if (isGarminPage(uri)) {
                        address.text = "佳明账号登录 · ${uri.host}"
                        // 换票成功那一刻 JWT_WEB 就落进 CookieManager 了，
                        // 不必等某个具体页面渲染完。
                        if (hasSession()) finishWithCookies()
                    }
                }

                override fun onReceivedError(
                    view: WebView, request: WebResourceRequest, error: WebResourceError
                ) {
                    if (request.isForMainFrame && !completed) {
                        address.text = "登录页加载失败，检查网络后重开这一页"
                    }
                }
            }
        }
        layout.addView(browser, LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
        setContentView(layout)
        browser.loadUrl(SIGNIN)
    }

    private fun isGarminPage(uri: Uri): Boolean {
        val host = uri.host?.lowercase() ?: return false
        return uri.scheme == "https" && uri.userInfo == null &&
            (uri.port == -1 || uri.port == 443) &&
            (host == "garmin.cn" || host.endsWith(".garmin.cn"))
    }

    private fun navigate(uri: Uri): Boolean {
        if (isGarminPage(uri)) return false
        // 出了佳明域名就不该再走了（比如邮箱里点进来的外链）
        address.text = "请在佳明页面里完成登录"
        return true
    }

    private fun hasSession(): Boolean {
        val raw = CookieManager.getInstance().getCookie("https://connect.garmin.cn") ?: ""
        return raw.split(";").any { it.trim().startsWith("JWT_WEB=") }
    }

    /** 把两个域下的 Cookie 合成一个数组交给 Python（auth.adopt_cookies 吃这个格式）。 */
    private fun cookiesJson(): String {
        val cm = CookieManager.getInstance()
        val out = JSONArray()
        val seen = HashSet<String>()
        for (url in listOf("https://connect.garmin.cn", "https://sso.garmin.cn")) {
            val raw = cm.getCookie(url) ?: continue
            for (part in raw.split(";")) {
                val kv = part.trim()
                if (!kv.contains("=")) continue
                val name = kv.substringBefore("=").trim()
                val value = kv.substringAfter("=")
                if (name.isEmpty() || value.isEmpty()) continue
                if (!seen.add(name)) continue      // 同名只留第一次（connect 优先）
                out.put(
                    JSONObject().put("name", name).put("value", value)
                        .put("domain", "connect.garmin.cn")
                )
            }
        }
        return out.toString()
    }

    private fun finishWithCookies() {
        val raw = cookiesJson()
        if (raw == "[]") {
            address.text = "还没拿到登录态，先把佳明的账号登录做完"
            return
        }
        completed = true
        setResult(RESULT_OK, Intent().putExtra("cookies", raw))
        finish()
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
