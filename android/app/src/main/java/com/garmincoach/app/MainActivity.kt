package com.garmincoach.app

import android.app.Activity
import android.app.AlertDialog
import android.content.ContentValues
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Canvas
import android.net.Uri
import android.os.Bundle
import android.os.Environment
import android.provider.MediaStore
import android.system.Os
import android.text.InputType
import android.util.Log
import android.view.Gravity
import android.view.View
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.webkit.JavascriptInterface
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.PopupMenu
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONArray
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.concurrent.thread

/**
 * 界面：顶部一条不遮挡内容的工具栏 + 一整块 WebView（指向本机 Python 起的只读服务）。
 * 同步/刷新没有按钮：页面到顶后下拉即可，反馈由 PullRefreshLayout 给。
 *
 * 为什么不原生画界面：看板、一图流、活动图本来就是 HTML，重画一遍等于
 * 维护两套版式，而且版式改动最频繁。WebView 直接吃现有产物。
 *
 * 为什么操作放顶栏而不是悬浮按钮：常驻 FAB 会一直压住看板右下角，
 * 表格本来就要横向滚动，遮一块特别难受。顶栏是独立的一行布局，
 * WebView 只占它下面那块，任何内容都不会被盖住。同步和刷新连菜单项
 * 都不留，统一走下拉手势；⋮ 里只剩「导出图片」和「重新登录」。
 *
 * 两个原生的地方：
 *   1) 登录 / 验证码要用系统键盘，原生对话框比 HTML 表单可靠；
 *   2) 出 PNG 用 WebView 自己画——这条路绕开了 Termux 没有 Chromium 的死结。
 *
 * 与 Python 的通信全部走 appapi.call(name, jsonArgs) -> JSON 字符串。
 * 不要改成 callAttr(...).toJava(Map::class.java)：Chaquopy 不支持把 dict
 * 转成 java.util.Map 接口，会抛 "Cannot convert dict Object to java.util.Map"。
 */
class MainActivity : Activity() {

    private lateinit var web: WebView
    private lateinit var bar: ProgressBar
    private lateinit var pull: PullRefreshLayout
    private lateinit var subtitle: TextView
    private lateinit var menuBtn: TextView
    private lateinit var secrets: Secrets

    private var port = 0

    private val api: PyObject by lazy { Python.getInstance().getModule("garmin_cn.appapi") }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        secrets = Secrets(this)

        // Python 侧的数据目录：APK 里的代码在只读 assets，必须指到 filesDir。
        // 一定要在 Python.start() 之前设好，所以放在最前面。
        try {
            Os.setenv("GARMIN_HOME", filesDir.absolutePath, true)
        } catch (t: Throwable) {
            fatal("数据目录设置失败", t)
        }

        // 刻意不用 PyApplication：它在 Application.onCreate 就启动解释器，
        // 那时 GARMIN_HOME 还没设。这里自己控制顺序。
        try {
            if (!Python.isStarted()) Python.start(AndroidPlatform(this))
        } catch (t: Throwable) {
            fatal("Python 启动失败", t)
        }

        val shell = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        val toolH = dp(52)

        val toolbar = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setBackgroundColor(0xFFFAFBF8.toInt())
            setPadding(dp(18), 0, dp(6), 0)
        }
        val titles = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
        }
        titles.addView(TextView(this).apply {
            text = "Garmin Coach"
            textSize = 16f
            setTextColor(0xFF1D2A22.toInt())
            setTypeface(typeface, android.graphics.Typeface.BOLD)
        })
        subtitle = TextView(this).apply {
            text = "下拉同步 · 点活动名出图"
            textSize = 11f
            setTextColor(0xFF8A8F87.toInt())
        }
        titles.addView(subtitle)
        toolbar.addView(titles, LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
        menuBtn = TextView(this).apply {
            text = "⋮"
            textSize = 26f
            setTextColor(0xFF4A5750.toInt())
            setPadding(dp(16), dp(4), dp(16), dp(4))
            setOnClickListener { showMenu() }
        }
        toolbar.addView(menuBtn, LinearLayout.LayoutParams(WRAP_CONTENT, MATCH_PARENT))
        shell.addView(toolbar, LinearLayout.LayoutParams(MATCH_PARENT, toolH))

        // 一条极细的分割线，比阴影轻，看板滚到底时也不会糊成一片
        shell.addView(View(this).apply { setBackgroundColor(0xFFE3E8DF.toInt()) },
            LinearLayout.LayoutParams(MATCH_PARENT, 1))

        pull = PullRefreshLayout(this)
        web = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.allowFileAccess = false
            settings.allowContentAccess = false
            settings.textZoom = 100
            // 关掉 WebView 自己的越界发光，否则到顶继续下拉会先弹一下再被接管
            overScrollMode = View.OVER_SCROLL_NEVER
            // 页面里的 data-aid 行点一下就出该活动的图；桥只挂在本机页面上
            addJavascriptInterface(Bridge(), "GC")
            webViewClient = object : WebViewClient() {
                override fun onPageFinished(v: WebView, u: String) {
                    if (u.startsWith("http://127.0.0.1:$port/")) v.evaluateJavascript(BIND_JS, null)
                }

                override fun shouldOverrideUrlLoading(v: WebView, req: WebResourceRequest): Boolean {
                    val host = req.url.host
                    if (host == "127.0.0.1" || host == "localhost") return false
                    // 外链不留在 WebView 里：JS 桥绝不能暴露给外部页面
                    try {
                        startActivity(Intent(Intent.ACTION_VIEW, req.url))
                    } catch (t: Throwable) {
                        Log.w(TAG, "打不开外链 ${req.url}", t)
                    }
                    return true
                }
            }
            setBackgroundColor(0xFFF4F6F2.toInt())
        }
        pull.addView(web, FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT))
        pull.attach(web)
        // 同步不再有按钮：页面到顶后继续下拉就是「同步 + 重出看板」
        pull.onRefresh = { onSync() }

        bar = ProgressBar(this, null, android.R.attr.progressBarStyleLarge)
        pull.addView(
            bar,
            FrameLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT, Gravity.CENTER),
        )
        bar.visibility = View.GONE

        shell.addView(pull, LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
        setContentView(shell)

        thread { bootServer() }
    }

    override fun onDestroy() {
        if (this::web.isInitialized) web.destroy()
        super.onDestroy()
    }

    @Suppress("DEPRECATION", "OVERRIDE_DEPRECATION")
    override fun onBackPressed() {
        if (this::web.isInitialized && web.canGoBack()) web.goBack() else super.onBackPressed()
    }

    /** 只暴露一个方法给本机页面：点活动行 → 出那一条的图。 */
    private inner class Bridge {
        @JavascriptInterface
        fun activity(id: String) {
            if (id.isBlank()) return
            ui { exportActivity(id) }
        }
    }

    // ------------------------------------------------------------ 菜单
    private fun showMenu() {
        val pm = PopupMenu(this, menuBtn)
        val m = pm.menu
        // 同步与刷新都交给下拉手势，菜单里只剩无法用点按替代的几项
        m.add(0, 1, 0, "今日一图流（导出 PNG）")
        m.add(0, 2, 1, "跑步活动图（导出 PNG）")
        m.add(0, 3, 2, "重新登录")
        pm.setOnMenuItemClickListener { item ->
            when (item.itemId) {
                1 -> exportCard()
                2 -> chooseRun()
                3 -> askLogin()
            }
            true
        }
        pm.show()
    }

    // ------------------------------------------------------------ 启动
    private fun bootServer() {
        val r = apiCall("start_server", 0)
        if (!r.optBoolean("ok")) {
            fatal("本地服务没起来", r.optString("error"))
            return
        }
        port = r.optInt("port")
        ui { web.loadUrl("http://127.0.0.1:$port/") }

        // 首次装完是空库，直接把登录框弹出来，别让人对着一页「还没有东西」猜
        val s = apiCall("status")
        setStatus(s)
        if (!s.optBoolean("logged_in")) ui { askLogin() }
    }

    private fun setStatus(s: JSONObject) {
        val days = s.optInt("days")
        val hint = if (days > 0) "$days 天数据 · 下拉同步" else "下拉同步数据"
        ui { subtitle.text = hint }
    }

    private fun reload() {
        if (port != 0) web.loadUrl("http://127.0.0.1:$port/") else web.reload()
    }

    // ------------------------------------------------------------ 同步
    /** 下拉刷新的唯一落点：同步 + 重出看板 + 回到页面顶部。 */
    private fun onSync() {
        val email = secrets.email()
        val pwd = secrets.password()
        if (email.isNullOrBlank() || pwd.isNullOrBlank()) {
            // 手势已经拉开，但这次只是要去登录，先把指示器收回去
            pull.cancel()
            askLogin()
            return
        }
        doSync(email, pwd)
    }

    private fun doSync(email: String, pwd: String) {
        ui {
            pull.setRefreshing(true)
            subtitle.text = "正在同步…"
        }
        thread {
            val r = apiCall("sync", 30, email, pwd)
            ui {
                when {
                    r.optBoolean("ok") -> {
                        toast("同步完成：${r.optInt("days")} 天 · ${r.optInt("activities")} 条活动")
                        // 同步只写库，不出页面。这里补一步看板，否则刷新后还是空列表。
                        thread {
                            apiCall("dashboard")
                            setStatus(apiCall("status"))
                            ui {
                                pull.setRefreshing(false)
                                reload()
                            }
                        }
                    }
                    r.optBoolean("need_mfa") -> {
                        pull.setRefreshing(false)
                        toast("会话续期要验证码，请重新登录")
                        askLogin()
                    }
                    r.optBoolean("need_login") -> {
                        pull.setRefreshing(false)
                        toast("登录已失效，请重新登录")
                        askLogin()
                    }
                    else -> {
                        pull.setRefreshing(false)
                        setStatus(apiCall("status"))
                        toast("同步失败：${r.optString("error")}")
                    }
                }
            }
        }
    }

    // ------------------------------------------------------------ 登录
    private fun askLogin() {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(56, 24, 56, 0)
        }
        val eEmail = EditText(this).apply {
            hint = "Garmin 国区邮箱"
            inputType = InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS
            setText(secrets.email() ?: "")
        }
        val ePwd = EditText(this).apply {
            hint = "密码"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        box.addView(eEmail)
        box.addView(ePwd)

        AlertDialog.Builder(this)
            .setTitle("登录佳明国区")
            .setMessage("账号只加密存在本机，不会上传到任何服务器。")
            .setView(box)
            .setPositiveButton("登录") { _, _ ->
                val e = eEmail.text.toString().trim()
                val p = ePwd.text.toString()
                if (e.isBlank() || p.isBlank()) {
                    toast("邮箱和密码都要填")
                } else {
                    doLogin(e, p)
                }
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun doLogin(email: String, pwd: String) {
        busy(true)
        thread {
            val r = apiCall("login", email, pwd)
            ui {
                busy(false)
                when {
                    r.optBoolean("ok") && r.optBoolean("need_mfa") -> {
                        secrets.save(email, pwd)     // 验证码这一步可能还要重试，先存上
                        askMfa()
                    }
                    r.optBoolean("ok") -> {
                        secrets.save(email, pwd)
                        toast("登录成功")
                        doSync(email, pwd)
                    }
                    else -> toast("登录失败：${r.optString("error")}")
                }
            }
        }
    }

    private fun askMfa() {
        val input = EditText(this).apply {
            hint = "邮箱验证码"
            inputType = InputType.TYPE_CLASS_NUMBER
        }
        AlertDialog.Builder(this)
            .setTitle("需要验证码")
            .setMessage("Garmin 已把验证码发到注册邮箱（也翻一下垃圾箱）")
            .setView(input)
            .setPositiveButton("确定") { _, _ -> doMfa(input.text.toString().trim()) }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun doMfa(code: String) {
        if (code.isBlank()) return
        busy(true)
        thread {
            val r = apiCall("submit_mfa", code)
            ui {
                busy(false)
                if (r.optBoolean("ok")) {
                    toast("登录成功")
                    val e = secrets.email()
                    val p = secrets.password()
                    if (e != null && p != null) doSync(e, p) else reload()
                } else {
                    toast("验证失败：${r.optString("error")}")
                }
            }
        }
    }

    // ------------------------------------------------------------ 出图
    /** 生成一图流 HTML，再用一个离屏 WebView 画成 1080×1920 的 PNG。 */
    private fun exportCard() {
        if (port == 0) {
            toast("还在启动，稍等一下")
            return
        }
        busy(true)
        thread {
            val r = apiCall("card_html")
            ui {
                if (!r.optBoolean("ok")) {
                    busy(false)
                    toast("出图失败：${r.optString("error")}")
                    return@ui
                }
                renderToPng(
                    fileUrl(r.optString("html")),
                    r.optInt("width", 1080), r.optInt("height", 1920),
                    "garmin", "分享一图流",
                )
            }
        }
    }

    /** 菜单里选一条跑步活动 → 出图。 */
    private fun chooseRun() {
        if (port == 0) {
            toast("还在启动，稍等一下")
            return
        }
        busy(true)
        thread {
            val r = apiCall("runs", 30)
            ui {
                busy(false)
                val items = r.optJSONArray("items")
                if (!r.optBoolean("ok") || items == null || items.length() == 0) {
                    toast("还没有跑步活动，先同步一次")
                    return@ui
                }
                val labels = Array(items.length()) { items.getJSONObject(it).optString("label") }
                AlertDialog.Builder(this)
                    .setTitle("选一条跑步出图（点看板里的活动名也行）")
                    .setItems(labels) { _, which ->
                        exportActivity(items.getJSONObject(which).optString("id"))
                    }
                    .setNegativeButton("取消", null)
                    .show()
            }
        }
    }

    /** 单次活动图：先出 HTML，再截成 1080×2250 的 PNG 存相册并分享。 */
    private fun exportActivity(id: String) {
        if (port == 0) {
            toast("还在启动，稍等一下")
            return
        }
        busy(true)
        thread {
            val r = apiCall("act_card_html", id, null, secrets.email(), secrets.password())
            ui {
                if (!r.optBoolean("ok")) {
                    busy(false)
                    if (r.optBoolean("need_login")) {
                        toast("这条活动的明细需要先联网登录抓一次")
                        askLogin()
                    } else {
                        toast("活动图失败：${r.optString("error")}")
                    }
                    return@ui
                }
                renderToPng(
                    fileUrl(r.optString("html")),
                    r.optInt("width", 1080), r.optInt("height", 2250),
                    "garmin-run", "分享活动图：${r.optString("name")}",
                )
            }
        }
    }

    private fun fileUrl(path: String): String =
        "http://127.0.0.1:$port/" + path.replace('\\', '/').substringAfterLast('/')

    private fun renderToPng(url: String, w: Int, h: Int, prefix: String, shareTitle: String) {
        val view = WebView(this).apply {
            settings.javaScriptEnabled = true
            // HTML 里写死 width=1080，但 View 的 1080px 在不同 density 上等于
            // 不同的 dp。overview 会把整页按宽度缩到刚好铺满，最终画出来的
            // 位图仍是 1080×H 像素，不会因为手机是 2x/3x 屏就只截到左上角。
            settings.useWideViewPort = true
            settings.loadWithOverviewMode = true
            // 放到屏幕外面：必须挂进窗口才会真正布局和渲染，
            // 但直接 draw(canvas) 画的是它自己的内容，不受位移影响。
            translationY = 8000f
        }
        pull.addView(view, FrameLayout.LayoutParams(w, h))
        view.measure(
            View.MeasureSpec.makeMeasureSpec(w, View.MeasureSpec.EXACTLY),
            View.MeasureSpec.makeMeasureSpec(h, View.MeasureSpec.EXACTLY),
        )
        view.layout(0, 0, w, h)

        view.webViewClient = object : WebViewClient() {
            override fun onPageFinished(v: WebView, u: String) {
                // 出图比例不对时，adb logcat -s GarminCoach 能一眼看出视口宽度
                v.evaluateJavascript(
                    "(function(){return window.innerWidth+'x'+" +
                        "document.documentElement.scrollWidth})()",
                ) { Log.d(TAG, "render viewport=$it expect=$w x $h") }
                // 字体和图表要一帧才稳，800ms 是实测下来不会截到半张图的余量
                v.postDelayed({
                    try {
                        val bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
                        v.draw(Canvas(bmp))
                        val uri = saveToGallery(bmp, prefix)
                        busy(false)
                        shareImage(uri, shareTitle)
                    } catch (t: Throwable) {
                        busy(false)
                        fatal("截图失败", t)
                    } finally {
                        pull.removeView(v)
                        v.destroy()
                    }
                }, 800)
            }
        }
        view.loadUrl(url)
    }

    private fun saveToGallery(bmp: Bitmap, prefix: String): Uri {
        val name = prefix + "-" +
            SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date()) + ".png"
        val values = ContentValues().apply {
            put(MediaStore.Images.Media.DISPLAY_NAME, name)
            put(MediaStore.Images.Media.MIME_TYPE, "image/png")
            put(
                MediaStore.Images.Media.RELATIVE_PATH,
                Environment.DIRECTORY_PICTURES + "/GarminCoach",
            )
        }
        val uri = contentResolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values)
            ?: throw IllegalStateException("写不进相册")
        contentResolver.openOutputStream(uri).use { out ->
            if (out == null || !bmp.compress(Bitmap.CompressFormat.PNG, 100, out)) {
                throw IllegalStateException("压缩失败")
            }
        }
        return uri
    }

    private fun shareImage(uri: Uri, title: String) {
        val i = Intent(Intent.ACTION_SEND).apply {
            type = "image/png"
            putExtra(Intent.EXTRA_STREAM, uri)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        startActivity(Intent.createChooser(i, title))
    }

    // ------------------------------------------------------------ 小工具
    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    private fun busy(on: Boolean) {
        bar.visibility = if (on) View.VISIBLE else View.GONE
    }

    private fun toast(msg: String) {
        Toast.makeText(this, msg, Toast.LENGTH_LONG).show()
    }

    private fun ui(block: () -> Unit) = runOnUiThread(block)

    /**
     * 唯一的一条 Python 调用通路。
     *
     * 参数走 JSON 数组，结果走 JSON 对象；Python 侧的 `call()` 负责把关，
     * 失败也返回 `{"ok": false, "error": ...}`，所以这里不用再判断别的。
     */
    private fun apiCall(name: String, vararg args: Any?): JSONObject = try {
        val payload = JSONArray().apply { args.forEach { put(it ?: JSONObject.NULL) } }
        JSONObject(api.callAttr("call", name, payload.toString()).toString())
    } catch (t: Throwable) {
        Log.e(TAG, "apiCall($name) failed", t)
        JSONObject().put("ok", false).put("error", t.message ?: t.javaClass.simpleName)
    }

    /**
     * 致命错误用对话框，不用 toast——toast 三秒就没了，
     * 用户截图时早消失，远程根本对不了账。
     */
    private fun fatal(scope: String, t: Throwable) =
        fatal(scope, t.message ?: t.javaClass.simpleName)

    private fun fatal(scope: String, detail: String) {
        Log.e(TAG, "$scope: $detail")
        ui {
            AlertDialog.Builder(this)
                .setTitle(scope)
                .setMessage(detail)
                .setPositiveButton("知道了", null)
                .show()
        }
    }

    private companion object {
        const val TAG = "GarminCoach"

        /** 注入到本机看板：给活动行加可点反馈，并把点击交给原生出图。 */
        const val BIND_JS = """
            (function(){
              if (window.__gcBound) return;
              window.__gcBound = true;
              var s = document.createElement('style');
              s.textContent = 'tr[data-aid]{cursor:pointer} tr[data-aid]:active{background:#E7F0E2}';
              document.head.appendChild(s);
              document.addEventListener('click', function(e){
                var n = e.target;
                while (n && n !== document) {
                  if (n.tagName === 'TR' && n.getAttribute('data-aid')) {
                    if (window.GC) GC.activity(n.getAttribute('data-aid'));
                    return;
                  }
                  n = n.parentNode;
                }
              }, false);
            })();
        """
    }
}
