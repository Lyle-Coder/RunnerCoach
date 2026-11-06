package com.garmincoach.app

import android.app.Activity
import android.app.AlertDialog
import android.content.ContentValues
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.os.Environment
import android.provider.MediaStore
import android.system.Os
import android.util.Base64
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
import android.widget.ImageView
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
import kotlin.math.abs
import kotlin.math.ceil
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

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
 * 都不留，统一走下拉手势；活动图直接点看板里的活动行，⋮ 里是
 * 「今日一图流」「设置」（预留模型接口配置）「小米体重」和「账号登录」
 * （佳明 / 高驰双图标选择，数据源可分别登）。
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
            text = "RunnerCoach"
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
            ui { askPrivacy(id) }
        }
    }

    // ------------------------------------------------------------ 菜单
    private fun showMenu() {
        val pm = PopupMenu(this, menuBtn)
        val m = pm.menu
        // 同步与刷新都交给下拉手势；活动图直接点看板里的活动行出，
        // 菜单里不再重复入口。一图流导出、设置、小米体重、重新登录。
        m.add(0, 1, 0, "今日一图流")
        m.add(0, 5, 1, "AI 教练")
        m.add(0, 2, 2, "模型服务设置")
        m.add(0, 3, 3, "小米体重")
        m.add(0, 4, 4, "账号登录")
        pm.setOnMenuItemClickListener { item ->
            when (item.itemId) {
                1 -> exportCard()
                // AI 教练与设置都在本机服务的另一个页面里：同源，桥的规则不变
                5 -> if (port != 0) web.loadUrl("http://127.0.0.1:$port/ai")
                2 -> if (port != 0) web.loadUrl("http://127.0.0.1:$port/ai#settings")
                3 -> showMiPanel()
                4 -> showLoginPicker()
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
        val c = apiCall("coros_status")
        if (!s.optBoolean("logged_in") && !c.optBoolean("logged_in")) {
            ui { showLoginPicker() }
        }
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
    /**
     * 下拉刷新的唯一落点：同步 + 重出看板 + 回到页面顶部。
     * 两个数据源独立拉取（佳明 session / 高驰 token 各过各的），
     * 只要有一个成功就算成功；两边都没配就弹账号选择。
     */
    private fun onSync() {
        val gEmail = secrets.email()
        val gPwd = secrets.password()
        val cEmail = secrets.corosEmail()
        val hasG = !gEmail.isNullOrBlank() && !gPwd.isNullOrBlank()
        val hasC = !cEmail.isNullOrBlank()
        if (!hasG && !hasC) {
            // 手势已经拉开，但这次只是要去登录，先把指示器收回去
            pull.cancel()
            showLoginPicker()
            return
        }
        doSync()
    }

    private fun doSync() {
        val gEmail = secrets.email()
        val gPwd = secrets.password()
        val cEmail = secrets.corosEmail()
        val cPwd = secrets.corosPassword() ?: ""
        ui {
            pull.setRefreshing(true)
            subtitle.text = "正在同步…"
        }
        thread {
            val msgs = mutableListOf<String>()
            var okAny = false
            var garminDead = false
            var corosDead = false
            var err = ""

            if (!gEmail.isNullOrBlank() && !gPwd.isNullOrBlank()) {
                val r = apiCall("sync", 30, gEmail, gPwd)
                when {
                    r.optBoolean("ok") -> {
                        // 增量同步：多数时候只拉今天/昨天，这里把「跳过」也报出来，
                        // 免得看到「30 天」以为是全量重拉了一遍
                        okAny = true
                        val fetched = r.optInt("fetched", r.optInt("days"))
                        val skipped = r.optInt("skipped")
                        var m = "佳明 $fetched 天"
                        if (skipped > 0) m += "（$skipped 已缓存）"
                        msgs.add(m)
                        val b = r.optJSONObject("body")
                        if (b != null && b.optInt("count") > 0) msgs.add("体重 ${b.optInt("count")} 条")
                    }
                    r.optBoolean("need_mfa") || r.optBoolean("need_login") -> garminDead = true
                    else -> err = "佳明：" + r.optString("error")
                }
            }
            if (!cEmail.isNullOrBlank()) {
                val r = apiCall("coros_sync", 30, cEmail, cPwd)
                when {
                    r.optBoolean("ok") -> {
                        okAny = true
                        msgs.add("高驰 ${r.optInt("daily")} 天 · ${r.optInt("activities")} 条活动")
                    }
                    r.optBoolean("need_login") -> corosDead = true
                    else -> err = (if (err.isEmpty()) "" else "$err · ") + "高驰：" + r.optString("error")
                }
            }

            ui {
                pull.setRefreshing(false)
                if (okAny) {
                    toast("同步完成：" + msgs.joinToString(" · "))
                    // 同步只写库，不出页面。这里补一步看板，否则刷新后还是空列表。
                    thread {
                        apiCall("dashboard")
                        setStatus(apiCall("status"))
                        ui {
                            if (garminDead) toast("佳明登录已失效，请在「账号登录」里重登")
                            else if (corosDead) toast("高驰登录已过期，请在「账号登录」里重登")
                            reload()
                        }
                    }
                } else {
                    setStatus(apiCall("status"))
                    when {
                        garminDead && corosDead -> {
                            toast("两个账号的登录都失效了，请重新登录")
                            showLoginPicker()
                        }
                        garminDead -> {
                            toast("会话续期要验证码或已失效，请重新登录")
                            askGarminLogin()
                        }
                        corosDead -> {
                            toast("高驰登录已过期，请重新登录")
                            askCorosLogin()
                        }
                        else -> toast("同步失败：$err")
                    }
                }
            }
        }
    }

    // ------------------------------------------------------------ 登录
    /**
     * 账号选择：两行各带数据源图标，佳明 / 高驰各走各的登录接口。
     * 两个都能登（数据写进同一套库），按行下的小字提示当前登没登。
     */
    private fun showLoginPicker() {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(10), dp(16), 0)
        }

        fun row(icon: Int, title: String, sub: String, onClick: () -> Unit) {
            val r = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                setPadding(dp(8), dp(12), dp(8), dp(12))
                setOnClickListener { onClick() }
            }
            r.addView(ImageView(this).apply {
                setImageResource(icon)
                layoutParams = LinearLayout.LayoutParams(dp(46), dp(46))
            })
            r.addView(LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(dp(14), 0, 0, 0)
                addView(TextView(this@MainActivity).apply {
                    text = title
                    textSize = 15f
                    setTextColor(0xFF1D2A22.toInt())
                    setTypeface(typeface, android.graphics.Typeface.BOLD)
                })
                addView(TextView(this@MainActivity).apply {
                    text = sub
                    textSize = 11.5f
                    setTextColor(0xFF8A8F87.toInt())
                })
            }, LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
            box.addView(r)
        }

        val g = secrets.email()
        val c = secrets.corosEmail()
        row(R.drawable.ic_logo_garmin, "Garmin 佳明国区",
            if (g.isNullOrBlank()) "邮箱密码 + 邮箱验证码" else "已登录 $g · 点击可重新登录"
        ) { askGarminLogin() }
        box.addView(View(this).apply { setBackgroundColor(0xFFE3E8DF.toInt()) },
            LinearLayout.LayoutParams(MATCH_PARENT, 1))
        row(R.drawable.ic_logo_coros, "COROS 高驰",
            if (c.isNullOrBlank()) "邮箱密码（Training Hub）· token 自动续期" else "已登录 $c · 点击可重新登录"
        ) { askCorosLogin() }

        AlertDialog.Builder(this)
            .setTitle("账号登录")
            .setMessage("选择手表数据来源；两个账号可以都登，数据写进同一套看板。凭据只加密存在本机。")
            .setView(box)
            .setNegativeButton("关闭", null)
            .show()
    }

    private fun askGarminLogin() {
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
                        doSync()
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
                    doSync()
                } else {
                    toast("验证失败：${r.optString("error")}")
                }
            }
        }
    }

    /**
     * 高驰登录：账号密码直登训练平台（Training Hub）。
     * token 约 24 小时过期，密码存 Keystore 后每次同步可静默重登；
     * 区域要和注册账号时选的一致（国内账号走 asia），选错会被拒。
     */
    private fun askCorosLogin() {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(56, 24, 56, 0)
        }
        val eEmail = EditText(this).apply {
            hint = "高驰账号邮箱"
            inputType = InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS
            setText(secrets.corosEmail() ?: "")
        }
        val ePwd = EditText(this).apply {
            hint = "密码"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        // 区域：默认国内；用单选对话框选，避免 Spinner 样式各机型不一致
        val regions = arrayOf("国内", "欧洲", "美国")
        val regionKeys = arrayOf("asia", "eu", "us")
        var regionIdx = 0
        val vRegion = TextView(this).apply {
            text = "账号区域：国内"
            textSize = 14f
            setTextColor(0xFF4A5750.toInt())
            setPadding(0, dp(14), 0, dp(6))
            setOnClickListener {
                AlertDialog.Builder(this@MainActivity)
                    .setTitle("账号区域")
                    .setSingleChoiceItems(regions, regionIdx) { _, which ->
                        regionIdx = which
                        text = "账号区域：" + regions[which]
                    }
                    .setPositiveButton("确定", null)
                    .show()
            }
        }
        box.addView(eEmail)
        box.addView(ePwd)
        box.addView(vRegion)
        AlertDialog.Builder(this)
            .setTitle("登录 COROS 高驰")
            .setMessage("与高驰手表 App 同一个账号。凭据只加密存在本机；token 过期后自动续登。")
            .setView(box)
            .setNegativeButton("取消", null)
            .setPositiveButton("登录") { _, _ ->
                val e = eEmail.text.toString().trim()
                val p = ePwd.text.toString()
                if (e.isBlank() || p.isBlank()) toast("邮箱和密码都要填")
                else doCorosLogin(e, p, regionKeys[regionIdx])
            }
            .show()
    }

    private fun doCorosLogin(email: String, pwd: String, region: String) {
        busy(true)
        thread {
            val r = apiCall("coros_login", email, pwd, region)
            ui {
                busy(false)
                if (r.optBoolean("ok")) {
                    secrets.saveCoros(email, pwd)
                    toast("高驰登录成功（${r.optString("user_id")}）")
                    doSync()
                } else {
                    toast("登录失败：${r.optString("error")}")
                }
            }
        }
    }

    // ------------------------------------------------------------ 小米体重
    /**
     * 小米秤不走佳明云：秤只做蓝牙广播，「小米运动健康」App 把算好的体重/
     * 体成分传到小米自己的云。小米没有给第三方任何授权接口，所以不存在
     * 「唤醒小米运动健康做授权」这条路——这里直连小米健康云：
     * 账号密码登录一次（新设备会要求图形验证码 + 短信验证），passToken
     * 半年量级长效，之后每次同步/出图 Python 侧自动补拉体重，无需再进面板。
     */
    private fun showMiPanel() {
        thread {
            val s = apiCall("mi_status")
            ui {
                if (!s.optBoolean("ok")) {
                    toast("状态读取失败：${s.optString("error")}")
                    return@ui
                }
                val logged = s.optBoolean("logged_in")
                val uid = s.optString("user_id")
                val dlg = AlertDialog.Builder(this)
                    .setTitle("小米体重")
                    .setMessage(
                        if (logged)
                            "已登录小米账号（$uid）。\n\n" +
                                "「同步体重」拉取最近的称重记录并回填看板；\n" +
                                "日常下拉刷新也会自动带一次，一般不用手动。"
                        else
                            "还没登录小米账号。\n\n" +
                                "秤的数据存在「小米运动健康」的云端，佳明云里没有；\n" +
                                "登录一次（可能需要短信验证），之后同步全自动。\n" +
                                "凭据只存本机（不含密码）。"
                    )
                    .setNegativeButton("关闭", null)
                if (logged) {
                    dlg.setPositiveButton("同步体重") { _, _ -> doMiSync() }
                    dlg.setNeutralButton("退出登录") { _, _ -> doMiLogout() }
                } else {
                    dlg.setPositiveButton("登录小米账号") { _, _ -> askMiLogin() }
                }
                dlg.show()
            }
        }
    }

    private fun doMiLogout() {
        thread {
            val r = apiCall("mi_logout")
            ui {
                toast(if (r.optBoolean("ok")) "已退出小米账号"
                      else "退出失败：${r.optString("error")}")
            }
        }
    }

    private fun askMiLogin() {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(56, 24, 56, 0)
        }
        val eUser = EditText(this).apply {
            hint = "小米账号（手机号 / 邮箱 / 小米 ID）"
            inputType = InputType.TYPE_CLASS_TEXT
        }
        val ePwd = EditText(this).apply {
            hint = "密码"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        box.addView(eUser)
        box.addView(ePwd)
        AlertDialog.Builder(this)
            .setTitle("登录小米健康云")
            .setMessage("秤的数据在「小米运动健康」绑定的账号下；账号密码只用于这一次登录，不会保存。")
            .setView(box)
            .setNegativeButton("取消", null)
            .setPositiveButton("登录") { _, _ ->
                val u = eUser.text.toString().trim()
                val p = ePwd.text.toString()
                if (u.isBlank() || p.isBlank()) toast("账号和密码都要填")
                else doMiLogin(u, p)
            }
            .show()
    }

    private fun doMiLogin(user: String, pwd: String) {
        busy(true)
        thread {
            val r = apiCall("mi_login", user, pwd)
            ui {
                busy(false)
                when {
                    r.optBoolean("ok") && r.optBoolean("need_verify") ->
                        askMiCaptcha(r.optString("masked"))
                    r.optBoolean("ok") -> {
                        toast("小米账号登录成功")
                        doMiSync()
                    }
                    else -> toast("登录失败：${r.optString("error")}")
                }
            }
        }
    }

    /**
     * 图形验证码一关：先看图认字，交上去换短信。小米这里「发送短信」必须带
     * 图形码，而且码错一次就作废，所以发送失败时自动换一张图让用户重认，
     * 对话框不关——整条验证链路的状态都在 Python 侧同一个实例上，关了重开
     * 反而要重新输账号密码。
     */
    private fun askMiCaptcha(masked: String) {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(56, 24, 56, 0)
        }
        val iv = ImageView(this).apply {
            setAdjustViewBounds(true)
            setPadding(0, dp(8), 0, dp(8))
        }
        val eCode = EditText(this).apply {
            hint = "图里的字符（看不清点图片换一张）"
            inputType = InputType.TYPE_CLASS_TEXT
            setSingleLine()
        }
        box.addView(iv)
        box.addView(eCode)
        val dlg = AlertDialog.Builder(this)
            .setTitle("安全验证 · 图形验证码")
            .setMessage("短信会发到 $masked")
            .setView(box)
            .setNegativeButton("取消", null)
            .setPositiveButton("发送短信", null)
            .create()
        iv.setOnClickListener { loadMiCaptcha(iv) }
        dlg.setOnShowListener {
            dlg.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                val code = eCode.text.toString().trim()
                if (code.isBlank()) {
                    toast("先填图里的字符")
                    return@setOnClickListener
                }
                busy(true)
                thread {
                    val r = apiCall("mi_send_code", code)
                    ui {
                        busy(false)
                        if (r.optBoolean("ok")) {
                            dlg.dismiss()
                            askMiSms()
                        } else {
                            val why = r.optString("desc").ifEmpty { r.optString("error") }
                            toast("短信没发出去：$why，已换一张图")
                            eCode.setText("")
                            loadMiCaptcha(iv)
                        }
                    }
                }
            }
        }
        dlg.show()
        loadMiCaptcha(iv)
    }

    private fun loadMiCaptcha(iv: ImageView) {
        iv.setImageDrawable(null)
        thread {
            val r = apiCall("mi_captcha")
            ui {
                if (!r.optBoolean("ok")) {
                    toast("验证码取不到：${r.optString("error")}")
                    return@ui
                }
                try {
                    val bytes = Base64.decode(r.optString("jpeg_b64"), Base64.DEFAULT)
                    iv.setImageBitmap(BitmapFactory.decodeByteArray(bytes, 0, bytes.size))
                } catch (t: Throwable) {
                    Log.w(TAG, "captcha decode", t)
                    toast("验证码解码失败：${t.message}")
                }
            }
        }
    }

    private fun askMiSms() {
        val input = EditText(this).apply {
            hint = "短信验证码"
            inputType = InputType.TYPE_CLASS_NUMBER
            setSingleLine()
        }
        val dlg = AlertDialog.Builder(this)
            .setTitle("安全验证 · 短信验证码")
            .setMessage("验证码已发出，查一下手机短信（也翻一下拦截）")
            .setView(input)
            .setNegativeButton("取消", null)
            .setPositiveButton("确定", null)
            .create()
        dlg.setOnShowListener {
            dlg.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                val code = input.text.toString().trim()
                if (code.isBlank()) return@setOnClickListener
                busy(true)
                thread {
                    val r = apiCall("mi_verify_code", code)
                    ui {
                        busy(false)
                        if (r.optBoolean("ok")) {
                            dlg.dismiss()
                            toast(if (r.optBoolean("partial"))
                                      "验证没走完，但凭据已可用"
                                  else "验证通过（${r.optString("user_id")}）")
                            doMiSync()
                        } else if (r.optBoolean("retry")) {
                            // 码是一次性的，错了可以重输；别让用户再等一条短信
                            toast("验证码不对：${r.optString("error")}")
                            input.setText("")
                        } else {
                            toast("验证失败：${r.optString("error")}")
                            dlg.dismiss()
                        }
                    }
                }
            }
        }
        dlg.show()
    }

    /** 拉体重入库 + 回填 daily，然后重出看板让页面上能立刻看到数。 */
    private fun doMiSync() {
        busy(true)
        ui { subtitle.text = "正在拉取小米体重…" }
        thread {
            val r = apiCall("mi_sync", 60)
            ui {
                busy(false)
                subtitle.text = "下拉同步 · 点活动名出图"
                if (r.optBoolean("ok")) {
                    val n = r.optInt("count")
                    val msg = if (n > 0)
                        "体重同步完成：" +
                            String.format(Locale.US, "%.1f", r.optDouble("weight", 0.0)) +
                            " kg（最新 ${r.optString("latest")}，$n 条）"
                    else
                        "云端这段时间没有新称重记录——上秤称一次，打开「小米运动健康」让它上传后再试"
                    toast(msg)
                    thread {
                        apiCall("dashboard")
                        ui { reload() }
                    }
                } else if (r.optBoolean("need_login")) {
                    toast("小米登录已失效，请重新登录")
                    askMiLogin()
                } else {
                    toast("体重同步失败：${r.optString("error")}")
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
                    r.optInt("export_width", 1080), r.optInt("export_height", 1920),
                    r.optString("export_bg", "#12161B"),
                    r.optBoolean("export_trim", false),
                )
            }
        }
    }

    /**
     * 分享前脱敏：轨迹起终点能反推住址，所以出图前先问一次档位。
     * 这一步同时充当「分享前确认」——点「生成并分享」才会真的出图并弹分享面板。
     * 选择存在 gc_prefs（与存密码的 gc_secrets 分开），默认 trim。
     */
    private fun askPrivacy(id: String) {
        val prefs = getSharedPreferences("gc_prefs", MODE_PRIVATE)
        val saved = prefs.getString("share_privacy", "trim") ?: "trim"
        val found = PRIVACY_MODES.indexOf(saved)
        var checked = if (found >= 0) found else 1
        AlertDialog.Builder(this)
            .setTitle("分享前脱敏")
            .setMessage(
                "这张活动图含你的 GPS 轨迹，起终点可能暴露住址或常跑路线。\n" +
                    "选好档位后才会生成图片并分享；原图会先存进相册。"
            )
            .setSingleChoiceItems(PRIVACY_LABELS, checked) { _, which -> checked = which }
            .setNegativeButton("取消", null)
            .setPositiveButton("生成并分享") { _, _ ->
                val mode = PRIVACY_MODES[checked]
                prefs.edit().putString("share_privacy", mode).apply()
                Log.d(TAG, "share privacy=$mode")
                exportActivity(id, mode)
            }
            .show()
    }

    /** 单次活动图：先出 HTML（画布 1080×2250），再截成 1080×1920 的 PNG 分享。 */
    private fun exportActivity(id: String, privacy: String = "trim") {
        if (port == 0) {
            toast("还在启动，稍等一下")
            return
        }
        busy(true)
        thread {
            // 第 5 个参数是分享脱敏档位；旧内核只有 4 个形参，
            // 所以这个 APK 必须与新 Python 内核一起打包（sync_python.bat）。
            val r = apiCall("act_card_html", id, null, secrets.email(),
                            secrets.password(), privacy)
            ui {
                if (!r.optBoolean("ok")) {
                    busy(false)
                    if (r.optBoolean("need_login")) {
                        toast("这条活动的明细需要先联网登录抓一次")
                        askGarminLogin()
                    } else {
                        toast("活动图失败：${r.optString("error")}")
                    }
                    return@ui
                }
                val mode = r.optString("privacy", "none")
                val tag = if (mode == "none") "" else "（已脱敏）"
                if (mode != "none") toast(r.optString("privacy_note"))
                renderToPng(
                    fileUrl(r.optString("html")),
                    r.optInt("width", 1080), r.optInt("height", 2250),
                    "garmin-run", "分享活动图$tag：${r.optString("name")}",
                    r.optInt("export_width", 1080), r.optInt("export_height", 1920),
                    r.optString("export_bg", "#0F1319"),
                    r.optBoolean("export_trim", true),
                )
            }
        }
    }

    private fun fileUrl(path: String): String =
        "http://127.0.0.1:$port/" + path.replace('\\', '/').substringAfterLast('/')

    /**
     * 离屏 WebView 出图。原先直接把 API 的 height 当位图高度：density、
     * overview 缩放和父容器重测量一叠加，页面会被放大后按固定画布裁掉四边；
     * 而 1080×2250 这种非 9:16 长图又会被系统分享面板二次 centerCrop。
     *
     * 更隐蔽的一层：HTML 写死了 `width=1080,initial-scale=1` 的 viewport，
     * `useWideViewPort` 于是把布局视口设成 1080 CSS px，可控件本身只有
     * 1080 **物理像素**（≈360dp）。`initial-scale=1` 让 1 CSS px 摊到
     * density 个物理像素，整页按 2~4 倍画到画布之外，`draw(Canvas)` 只截到
     * 左上角一块——分享预览里就成了「只剩一角、数字特别大」。旧自校正拿
     * `window.innerWidth`（布局视口）当可视宽度，所以完全没发现这件事。
     *
     * 现在改成三段自校正，不再猜 density：
     *   1) load 前给 100/density 的初始缩放，尽量让 1 CSS px = 1 物理 px；
     *   2) 探针读 `visualViewport.width`，算出该给画布套的 transform scale(f)，
     *      把 1080 CSS px 的版心恰好铺满控件宽度（f = 可视 CSS 宽 / 画布宽）；
     *   3) 按内容真实高度加高 View，draw(Canvas) 后裁掉空白，再 contain
     *      收进 9:16，四周留同色安全带，防分享面板再做一次 centerCrop。
     * 成品恒为 exportW×exportH，且内容永远完整。
     */
    private fun renderToPng(
        url: String, w: Int, h: Int, prefix: String, shareTitle: String,
        exportW: Int, exportH: Int, bgHex: String, trimSrc: Boolean = true,
    ) {
        val bg = try {
            Color.parseColor(bgHex)
        } catch (t: Throwable) {
            0xFF12161B.toInt()
        }
        val density = resources.displayMetrics.density.coerceAtLeast(1f)
        val view = WebView(this).apply {
            settings.javaScriptEnabled = true
            // 布局视口必须是 HTML 写死的 1080 CSS px，否则固定版心会重排。
            settings.useWideViewPort = true
            // overview 只在「内容比控件宽」时生效；这里布局视口宽恰好等于
            // 内容宽，它反而不缩，还会和下面的 initialScale 抢控制权。
            settings.loadWithOverviewMode = false
            // 系统字体缩放会撑破固定版心（主 WebView 也是这么设的）。
            settings.textZoom = 100
            // 100% 时 1 CSS px = 1dp = density 个物理像素；降到 100/density
            // 就约等于 1 CSS px = 1 物理像素。探针还会实测并二次校正。
            setInitialScale((100.0 / density).roundToInt().coerceIn(10, 100))
            // 硬件加速下离屏 WebView 的 RenderNode 常画不进 Canvas，
            // 软件层才能让 draw(Canvas) 拿到完整内容，代价只是出图慢一点。
            setLayerType(View.LAYER_TYPE_SOFTWARE, null)
            setBackgroundColor(bg)
            // 放到屏幕外：必须挂进窗口才会真正布局和渲染，
            // 但 draw(canvas) 画的是它自己的内容，不受位移影响。
            translationY = 8000f
        }
        pull.addView(view, FrameLayout.LayoutParams(w, h))
        forceSize(view, w, h)

        var done = false
        var measured = false
        var probed = false
        // 父容器每次重测量都可能把高度改回去，截图前统一按这个值再钉一次。
        var targetH = h

        fun cleanup(v: WebView) {
            busy(false)
            try {
                pull.removeView(v)
            } catch (t: Throwable) {
                Log.w(TAG, "remove render view", t)
            }
            v.destroy()
        }

        fun capture(v: WebView) {
            if (done) return
            done = true
            try {
                // 父容器可能重新测量过：截图前按自校正后的高度再钉一次尺寸。
                forceSize(v, v.width.coerceAtLeast(1), targetH)
                val vw = v.width.coerceAtLeast(1)
                val vh = v.height.coerceAtLeast(1)
                val raw = Bitmap.createBitmap(vw, vh, Bitmap.Config.ARGB_8888)
                v.draw(Canvas(raw))
                // 四边裁掉页面自带的底色留白（全部贴边）：页面 padding 经
                // contain 缩放后仍是成品最大的「边框」来源，只有先裁掉，
                // 内容才能贴着安全带排，四边只剩安全带和 contain 的微差。
                // 一图流画幅本来就是 9:16、页面 padding 就是设计留白，
                // 裁掉反而让上下被撑大，所以由 export_trim 决定裁不裁。
                val corners = if (trimSrc) contentBounds(raw, bg) else null
                if (trimSrc && corners == null) {
                    throw IllegalStateException("页面没有渲染出内容（整张都是底色）")
                }
                val left = if (corners != null) max(0, corners[0] - 2) else 0
                val top = if (corners != null) max(0, corners[1] - 2) else 0
                val right = if (corners != null) min(vw, corners[2] + 2) else vw
                val bottom = if (corners != null) min(vh, corners[3] + 2) else vh
                if (right - left < 64 || bottom - top < 64) {
                    throw IllegalStateException(
                        "内容区域过小（${right - left}x${bottom - top}），疑似没渲染完整")
                }
                val cropped = Bitmap.createBitmap(raw, left, top, right - left, bottom - top)
                val out = fitToFrame(cropped, exportW, exportH, bg)
                Log.d(TAG, "render view=${vw}x${vh} " +
                    "crop=[$left,$top ${right - left}x${bottom - top}] " +
                    "out=${out.width}x${out.height} bg=$bgHex targetH=$targetH")
                val uri = saveToGallery(out, prefix)
                shareImage(uri, shareTitle)
                toast("原图已存相册/GarminCoach")
            } catch (t: Throwable) {
                fatal("截图失败", t)
            } finally {
                cleanup(v)
            }
        }

        fun handleProbe(v: WebView, value: String?) {
            if (done || probed) return
            probed = true
            val raw = (value ?: "").trim().trim('"')
            val parts = raw.split("|")
            val cssW = parts.getOrNull(0)?.toIntOrNull() ?: 0
            val cssH = parts.getOrNull(1)?.toIntOrNull() ?: 0
            val innerW = parts.getOrNull(2)?.toIntOrNull() ?: 0
            val vvW = parts.getOrNull(4)?.toIntOrNull() ?: 0
            val dpr = parts.getOrNull(5)?.toDoubleOrNull() ?: 0.0
            val vw = v.width.coerceAtLeast(1)

            // innerWidth 是布局视口，不是「屏幕上能看见多少 CSS px」。
            // visualViewport.width 才和 draw(Canvas) 的真实缩放一一对应：
            // density=3 且 initial-scale=1 时它只有 360，而画布是 1080。
            val visibleCss = when {
                vvW > 0 -> vvW
                innerW > 0 -> innerW          // 旧探针：保守不缩放
                else -> (vw / density).roundToInt()
            }
            val f = if (cssW > 0 && visibleCss > 0)
                (visibleCss.toDouble() / cssW).coerceIn(0.1, 4.0) else 1.0
            // 归一之后内容高度 = cssH * vw / cssW，与 density、页面缩放无关。
            val needed = if (cssW > 0)
                ceil(cssH * vw.toDouble() / cssW).toInt() + 32 else 0
            if (needed > targetH) targetH = needed
            Log.d(TAG, "render probe='$raw' view=${vw}x${v.height} " +
                "visibleCss=$visibleCss f=$f needed=$needed dpr=$dpr")

            if (targetH > v.height + 2) {
                val lp = v.layoutParams ?: FrameLayout.LayoutParams(vw, targetH)
                lp.height = targetH
                v.layoutParams = lp
                forceSize(v, vw, targetH)
            }
            // 把版本号和实测缩放系数显示出来：真机测试时一眼能确认装的是新包，
            // 也能把 ×0.xx 直接报回来对账，不用插 adb。
            val ver = try {
                packageManager.getPackageInfo(packageName, 0).versionName ?: "?"
            } catch (t: Throwable) {
                "?"
            }
            toast("$ver 出图缩放 ×" + String.format(Locale.US, "%.2f", f))
            if (abs(f - 1.0) > 0.02) {
                // 画布比可视视口宽：套 transform 把版心缩进控件，等一帧再截。
                // 这一步是修复「分享预览只剩一角」的关键，绝不靠裁图补救。
                val js = "window.__gcFit ? window.__gcFit($f) : ''"
                v.evaluateJavascript(js) { _ ->
                    v.postVisualStateCallback(
                        EXPORT_FIT_FRAME_ID,
                        object : WebView.VisualStateCallback() {
                            override fun onComplete(requestId: Long) { capture(v) }
                        },
                    )
                    // 离屏 WebView 某些版本不回调 VisualStateCallback，定时兜底。
                    v.postDelayed({ capture(v) }, 700)
                }
            } else {
                v.post { capture(v) }
            }
        }

        fun measure(v: WebView) {
            if (done || measured) return
            measured = true
            var answered = false
            try {
                v.evaluateJavascript(EXPORT_PROBE_JS) { value ->
                    answered = true
                    handleProbe(v, value)
                }
            } catch (t: Throwable) {
                Log.w(TAG, "probe failed", t)
                answered = true
                handleProbe(v, null)
            }
            // 探针回调丢了也得出图：按兜底尺寸截，最终仍归一到 9:16。
            v.postDelayed({ if (!answered && !done) handleProbe(v, null) }, 1500)
        }

        view.webViewClient = object : WebViewClient() {
            override fun onPageFinished(v: WebView, u: String) {
                // 首帧真正画完再量，autofit 脚本和字体此时才生效。
                v.postVisualStateCallback(
                    EXPORT_FRAME_ID,
                    object : WebView.VisualStateCallback() {
                        override fun onComplete(requestId: Long) { measure(v) }
                    },
                )
                // 离屏 WebView 某些版本不回调 VisualStateCallback，定时兜底。
                v.postDelayed({ measure(v) }, 900)
            }
        }
        // 页面加载不出来时别让进度圈一直转。
        view.postDelayed({
            if (!done) {
                done = true
                toast("出图超时，请重试")
                cleanup(view)
            }
        }, 10000)
        view.loadUrl(url)
    }

    /** 手工 measure/layout：addView 之后父容器会再测一次，截图前再钉一次尺寸。 */
    private fun forceSize(v: View, w: Int, h: Int) {
        v.measure(
            View.MeasureSpec.makeMeasureSpec(w, View.MeasureSpec.EXACTLY),
            View.MeasureSpec.makeMeasureSpec(h, View.MeasureSpec.EXACTLY),
        )
        v.layout(0, 0, w, h)
    }

    /** 四边扫描内容边界 [l, t, r, b)：整行/整列都是底色的边缘才裁，绝不碰内容像素。 */
    private fun contentBounds(bmp: Bitmap, bg: Int): IntArray? {
        val w = bmp.width
        val h = bmp.height
        val px = IntArray(w * h)
        bmp.getPixels(px, 0, w, 0, 0, w, h)
        val br = Color.red(bg)
        val bgc = Color.green(bg)
        val bb = Color.blue(bg)
        fun isBg(c: Int) = abs(Color.red(c) - br) <= 6 &&
            abs(Color.green(c) - bgc) <= 6 && abs(Color.blue(c) - bb) <= 6
        var top = 0
        outer@ while (top < h) {
            var x = 0
            while (x < w) {
                if (!isBg(px[top * w + x])) break@outer
                x += 3
            }
            top++
        }
        if (top == h) return null
        var bottom = h
        outerBottom@ while (bottom > top) {
            var x = 0
            while (x < w) {
                if (!isBg(px[(bottom - 1) * w + x])) break@outerBottom
                x += 3
            }
            bottom--
        }
        var left = 0
        outerLeft@ while (left < w) {
            var y = top
            while (y < bottom) {
                if (!isBg(px[y * w + left])) break@outerLeft
                y += 3
            }
            left++
        }
        var right = w
        outerRight@ while (right > left) {
            var y = top
            while (y < bottom) {
                if (!isBg(px[y * w + right - 1])) break@outerRight
                y += 3
            }
            right--
        }
        return intArrayOf(left, top, right, bottom)
    }

    /**
     * contain 收进标准 9:16：只缩不裁。四周留一圈与卡片同色的安全带，
     * 因为各家分享面板的预览框未必是 9:16，centerCrop 时会先切掉边框，
     * 不会碰到日期、轨迹和表格。安全带与底色同色，肉眼看不出来。
     *
     * 安全带定为画幅高的 1.6%（1920 → 31px）：配合出图前的四边 trim
     * （裁掉页面自带底色留白，Kotlin 与桌面 pngutil.trim_bg 同一套规则），
     * 活动图与一图流的四边都只剩安全带和 contain 的微差 ≈ 2~3%。
     */
    private fun fitToFrame(src: Bitmap, dw: Int, dh: Int, bg: Int): Bitmap {
        val pad = max(12, (dh * 0.016).roundToInt())
        val boxW = (dw - 2 * pad).coerceAtLeast(1)
        val boxH = (dh - 2 * pad).coerceAtLeast(1)
        val s = min(boxW.toDouble() / src.width, boxH.toDouble() / src.height)
        val tw = max(1, (src.width * s).roundToInt())
        val th = max(1, (src.height * s).roundToInt())
        val scaled = if (tw == src.width && th == src.height) src
        else Bitmap.createScaledBitmap(src, tw, th, true)
        val out = Bitmap.createBitmap(dw, dh, Bitmap.Config.ARGB_8888)
        out.eraseColor(bg)
        Canvas(out).drawBitmap(scaled, (dw - tw) / 2f, (dh - th) / 2f, null)
        return out
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

        /** 与 garmin_cn/privacy.py 的 MODES 一一对应，顺序不能改。 */
        val PRIVACY_MODES = arrayOf("none", "trim", "hide")
        val PRIVACY_LABELS = arrayOf<CharSequence>(
            "原图（含完整 GPS 轨迹）",
            "裁首尾各 200 m（推荐）",
            "完全隐藏轨迹与起跑时间",
        )

        /** postVisualStateCallback 的请求号，非零即可，两个阶段各用一个。 */
        const val EXPORT_FRAME_ID = 77L
        const val EXPORT_FIT_FRAME_ID = 78L

        /**
         * 读 HTML 里的导出探针；旧版 Python 没有探针时返回空串，走尺寸兜底。
         * 返回 "画布CSS宽|画布CSS高|innerWidth|scrollHeight|visualViewport.width|dpr"。
         */
        const val EXPORT_PROBE_JS =
            "window.__gcExportInfo ? window.__gcExportInfo() : ''"

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
